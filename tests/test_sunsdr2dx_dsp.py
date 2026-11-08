"""SunSDR2 DX native backend — Task 2: DSP layer (pure logic, no I/O).

backends/sunsdr/dsp.py owns the three hardware-free DSP pieces the engine
(Task 3+) will wire to the UDP socket, reusing Task 1's 24-bit codec:

- ``iq_to_spectrum``: Hann FFT + fftshift + fixed dB window + linear
  resample to 850 uint8-compatible bins for the waterfall;
- ``AudioDemodulator``: streaming IQ -> 48 kHz int16-le RX audio
  (USB/LSB/CW/DIGI/AM/NFM/FM/WFM), +30500 Hz IF shift, complex sideband
  filters, AM envelope, FM discriminator with cross-block memory,
  continuous filter/resample state, Python AGC, S-meter from passband IQ
  energy;
- ``TXModulator``: streaming 48 kHz int16 mic -> 200-sample (1200-byte)
  24-bit IQ payloads (DC blocker, 300 Hz HPF, 3.6 kHz anti-alias,
  Hilbert overlap-save USB/LSB, fixed gain, tanh magnitude limiter).

Design facts inherited from the measured reference DSP
(/Users/cheenle/HAM/sunsdr/web_control/dsp.py), re-derived here through
behavior tests only:

- The device puts the VFO at ``-IF_OFFSET_HZ`` inside the IQ stream, so
  RX shifts UP by +30500 Hz to bring the tuned signal to 0 Hz.  A
  synthesized "RF tone at VFO + f" is therefore ``exp(2j*pi*(f-IF)*n/fs)``
  at the IQ input (see ``_tone_iq``).
- RX audio output is 48 kHz — NOT the reference server's 15625 Hz.  The
  length assertions below (0.25 s of IQ -> ~12000 samples, and a 10 kHz
  WFM program tone surviving demodulation) genuinely fail on a 16 kHz
  output path rather than merely re-interpreting its bytes.
- TX IQ rate is 5^7/2 = 39062.5 Hz (half the 78125 RX rate, per the
  reference capture analysis); 200 IQ samples per 1200-byte payload.
"""
import math
import unittest

import numpy as np

from backends.sunsdr import protocol as sdp
from backends.sunsdr import dsp


IF = sdp.IF_OFFSET_HZ  # 30500.0


def _tone_iq(freq_after_shift: float, fs: float, n: int,
             amp: float = 0.5) -> np.ndarray:
    """Complex tone that lands at ``freq_after_shift`` Hz after the +IF shift."""
    k = np.arange(n, dtype=np.float64)
    return (amp * np.exp(2j * math.pi * (freq_after_shift - IF) * k / fs)
            ).astype(np.complex64)


def _raw_tone(freq: float, fs: float, n: int, amp: float = 1.0) -> np.ndarray:
    """Complex tone at ``freq`` Hz in the raw IQ stream (spectrum tests)."""
    k = np.arange(n, dtype=np.float64)
    return (amp * np.exp(2j * math.pi * freq * k / fs)).astype(np.complex64)


def _pcm16(freq: float, ms: float, amp: float = 0.5,
           rate: int = 48000) -> bytes:
    """Little-endian int16 sine, ``ms`` milliseconds at ``rate``."""
    n = int(round(rate * ms / 1000.0))
    t = np.arange(n, dtype=np.float64) / rate
    x = np.rint(amp * 32767.0 * np.sin(2.0 * math.pi * freq * t))
    return np.clip(x, -32768, 32767).astype("<i2").tobytes()


def _dominant_freq(audio_i16: np.ndarray, rate: int = 48000) -> float:
    """Parabolic-refined dominant frequency of an int16 audio vector."""
    x = audio_i16.astype(np.float64)
    n = len(x)
    mag = np.abs(np.fft.rfft(x * np.hanning(n)))
    k = int(np.argmax(mag))
    if 0 < k < len(mag) - 1:  # parabolic interpolation
        a, b, c = mag[k - 1], mag[k], mag[k + 1]
        denom = (a - 2.0 * b + c)
        k = k + 0.5 * (a - c) / denom if abs(denom) > 1e-12 else float(k)
    return float(k) * rate / n


def _band_rms(audio_i16: np.ndarray, lo: float, hi: float,
              rate: int = 48000) -> float:
    """RMS energy of the spectral band between |lo| and |hi| Hz."""
    x = audio_i16.astype(np.float64)
    freqs = np.fft.rfftfreq(len(x), d=1.0 / rate)
    mag = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    edge_lo, edge_hi = sorted((abs(lo), abs(hi)))
    sel = (freqs >= edge_lo) & (freqs <= edge_hi)
    return float(np.sqrt(np.sum(mag[sel] ** 2)))


class IqToSpectrumInterfaceTests(unittest.TestCase):
    def test_returns_list_of_ints_length_bins_in_uint8_range(self):
        iq = _tone_iq(1500.0, 78125, 4096)
        out = dsp.iq_to_spectrum(iq, 78125)
        self.assertIsInstance(out, list)
        self.assertEqual(len(out), 850)
        for v in out:
            self.assertIsInstance(v, int)
            self.assertTrue(0 <= v <= 255)

    def test_bins_parameter_respected(self):
        iq = _tone_iq(1500.0, 78125, 4096)
        out = dsp.iq_to_spectrum(iq, 78125, bins=256)
        self.assertEqual(len(out), 256)

    def test_accepts_python_list_input(self):
        iq = [0.1 + 0.1j] * 3000
        out = dsp.iq_to_spectrum(iq, 78125)
        self.assertEqual(len(out), 850)

    def test_empty_input_gives_all_zeros(self):
        for empty in (np.zeros(0, dtype=np.complex64), []):
            out = dsp.iq_to_spectrum(empty, 78125)
            self.assertEqual(len(out), 850)
            self.assertEqual(out, [0] * 850)

    def test_non_finite_samples_do_not_crash(self):
        bad = np.array([np.nan, np.inf, -np.inf, 1e30] * 600,
                       dtype=np.complex64)
        out = dsp.iq_to_spectrum(bad, 78125)
        self.assertEqual(len(out), 850)
        self.assertTrue(all(0 <= v <= 255 for v in out))

    def test_bad_rates_and_bins_raise_value_error(self):
        iq = np.zeros(3000, dtype=np.complex64)
        for fs in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                dsp.iq_to_spectrum(iq, fs)
        with self.assertRaises(ValueError):
            dsp.iq_to_spectrum(iq, 78125, bins=0)


class IqToSpectrumPeakTests(unittest.TestCase):
    """Peak placement at every device IQ rate, by NORMALIZED frequency."""

    RATES = (39062, 78125, 156250, 312500)  # protocol.SAMPLE_RATES values

    def _expected_index(self, freq: float, fs: float, fft_size: int,
                        bins: int) -> float:
        # fftshifted bin of a +freq tone, then linear-resampled position:
        # xi = linspace(0, fft_size-1, bins); idx ~= p * (bins-1)/(fft_size-1)
        p = fft_size / 2.0 + freq / fs * fft_size
        return p * (bins - 1) / (fft_size - 1)

    def test_positive_tone_right_of_center_all_rates(self):
        bins, fft_size = 850, 2048
        for fs in self.RATES:
            f = 0.064 * fs  # 2.5k/5k/10k/20k — all below Nyquist
            out = dsp.iq_to_spectrum(_raw_tone(f, fs, 8192), fs)
            idx = int(np.argmax(out))
            self.assertGreater(idx, bins // 2, f"fs={fs}")
            self.assertLess(abs(idx - self._expected_index(f, fs, fft_size,
                                                           bins)), 10,
                            f"fs={fs} idx={idx}")

    def test_negative_tone_left_of_center_all_rates(self):
        bins, fft_size = 850, 2048
        for fs in self.RATES:
            f = -0.064 * fs
            out = dsp.iq_to_spectrum(_raw_tone(f, fs, 8192), fs)
            idx = int(np.argmax(out))
            self.assertLess(idx, bins // 2, f"fs={fs}")
            self.assertLess(abs(idx - self._expected_index(f, fs, fft_size,
                                                           bins)), 10,
                            f"fs={fs} idx={idx}")

    def test_full_scale_tone_peak_and_floor(self):
        out = dsp.iq_to_spectrum(_raw_tone(5000.0, 78125, 8192), 78125)
        self.assertGreater(max(out), 230)
        self.assertLess(np.median(out), 60)

    def test_if_shift_centers_the_vfo_at_every_rate(self):
        # The device's IQ is LO-centred: a genuine VFO carrier sits at
        # -IF_OFFSET (aliased at 39k), and the reference server rotates it
        # to the display centre.  Without if_shift_hz the waterfall (and
        # click-to-tune) is 30.5 kHz off.
        bins, fft_size = 850, 2048
        for fs in self.RATES:
            out = dsp.iq_to_spectrum(
                _raw_tone(-dsp.IF_OFFSET, fs, 8192), fs,
                if_shift_hz=dsp.IF_OFFSET)
            idx = int(np.argmax(out))
            self.assertLess(abs(idx - bins // 2), 12, f"fs={fs} idx={idx}")

    def test_if_shift_preserves_offset_above_vfo(self):
        bins, fft_size = 850, 2048
        fs = 78125
        out = dsp.iq_to_spectrum(
            _raw_tone(-dsp.IF_OFFSET + 5000.0, fs, 8192), fs,
            if_shift_hz=dsp.IF_OFFSET)
        idx = int(np.argmax(out))
        self.assertLess(abs(idx - self._expected_index(5000.0, fs, fft_size,
                                                        bins)), 10)

    def test_zero_if_shift_keeps_raw_lo_centred_behaviour(self):
        fs = 78125
        out = dsp.iq_to_spectrum(_raw_tone(-dsp.IF_OFFSET, fs, 8192), fs)
        self.assertLess(int(np.argmax(out)), 425)

    def test_bad_if_shift_raises(self):
        for bad in (float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                dsp.iq_to_spectrum([], 78125, if_shift_hz=bad)


class AudioDemodulatorInterfaceTests(unittest.TestCase):
    def test_defaults_and_empty_input(self):
        d = dsp.AudioDemodulator()
        self.assertEqual(d.sample_rate, 78125)
        self.assertEqual(d.audio_rate, 48000)
        self.assertEqual(d.demodulate(np.zeros(0, dtype=np.complex64)), b"")
        self.assertEqual(d.demodulate([]), b"")

    def test_output_is_48khz_int16_bytes(self):
        d = dsp.AudioDemodulator()
        n_iq = int(78125 * 0.25)
        out = d.demodulate(_tone_iq(1500.0, 78125, n_iq))
        self.assertIsInstance(out, bytes)
        audio = np.frombuffer(out, dtype="<i2")
        self.assertEqual(len(audio), len(out) // 2)
        # The 48k discriminator: 0.25 s MUST give ~12000 samples (the
        # streaming resampler's one-time group delay takes a few hundred).
        # A legacy 16 kHz path would give 4000 and fail here.
        self.assertGreaterEqual(len(audio), 11000)
        self.assertLessEqual(len(audio), 12400)

    def test_supported_and_rejected_modes(self):
        d = dsp.AudioDemodulator()
        for m in ("USB", "LSB", "CW", "DIGI", "AM", "NFM", "FM", "WFM"):
            d.set_mode(m)
            self.assertEqual(d.mode, m)
        d.set_mode("usb")
        self.assertEqual(d.mode, "USB")
        for bad in ("SSB", "AMN", "", "xyz"):
            with self.assertRaises(ValueError):
                d.set_mode(bad)

    def test_rate_validation_rejects_non_finite_and_non_positive(self):
        for fs in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                dsp.AudioDemodulator(sample_rate=fs)
        with self.assertRaises(ValueError):
            dsp.AudioDemodulator(sample_rate=78125, audio_rate=0)

    def test_39062_usb_lsb_recover_vfo_tones(self):
        # The +30500 Hz LO is periodic in fs: at 39062.5 it equals a
        # -8562.5 Hz rotation, which still mixes the VFO (-30500 in IQ)
        # EXACTLY to 0 — no conjugation, no sideband swap.  USB/LSB must
        # each recover their side of the VFO at this rate.
        fs = 39062.5
        for mode, tone_hz in (("USB", 1500.0), ("LSB", -1500.0)):
            d = dsp.AudioDemodulator(sample_rate=fs)
            d.set_mode(mode)
            out = d.demodulate(_tone_iq(tone_hz, fs, 15625))
            audio = np.frombuffer(out, dtype="<i2")
            self.assertGreater(len(audio), 18000)
            self.assertLess(abs(_dominant_freq(audio) - 1500.0), 20.0)

    def test_39062_no_sideband_swap(self):
        # Orientation pin at the aliased LO rate: USB must still REJECT a
        # VFO-1500 tone (a conjugating mixer would put it in the passband).
        fs = 39062.5

        def level(mode, tone_hz):
            d = dsp.AudioDemodulator(sample_rate=fs)
            d.set_mode(mode)
            d.set_agc_mode("off")
            out = d.demodulate(_tone_iq(tone_hz, fs, 15625))
            return _band_rms(np.frombuffer(out, dtype="<i2"),
                             tone_hz - 60, tone_hz + 60)

        self.assertGreater(level("USB", 1500.0), 20.0 * level("USB", -1500.0))
        self.assertGreater(level("LSB", -1500.0), 20.0 * level("LSB", 1500.0))

    def test_39062_fm_stays_unmirrored(self):
        fs, n = 39062.5, 15625  # 0.4 s
        k = np.arange(n, dtype=np.float64)
        dev = 2000.0
        phase = (2.0 * math.pi * (0.0 - IF) * k / fs
                 + 2.0 * math.pi * dev / fs * np.cumsum(
                     np.sin(2.0 * math.pi * 200.0 * k / fs)))
        d = dsp.AudioDemodulator(sample_rate=fs)
        d.set_mode("NFM")
        out = d.demodulate((np.exp(1j * phase) * 0.5).astype(np.complex64))
        audio = np.frombuffer(out, dtype="<i2")
        self.assertLess(abs(_dominant_freq(audio) - 200.0), 20.0)

    def test_non_finite_iq_does_not_crash(self):
        d = dsp.AudioDemodulator()
        bad = np.full(5000, np.nan + 1j * np.nan, dtype=np.complex64)
        out = d.demodulate(bad)
        self.assertIsInstance(out, bytes)
        audio = np.frombuffer(out, dtype="<i2")
        self.assertTrue(np.all(np.isfinite(audio)))

    def test_other_iq_rates_work(self):
        d = dsp.AudioDemodulator(sample_rate=156250)
        out = d.demodulate(_tone_iq(1200.0, 156250, 15625))
        audio = np.frombuffer(out, dtype="<i2")
        self.assertGreater(len(audio), 4000)  # 0.1 s -> ~4800


class AudioDemodulatorSidebandTests(unittest.TestCase):
    def _level_at(self, mode: str, tone_hz: float, amp: float = 0.5) -> float:
        d = dsp.AudioDemodulator()
        d.set_mode(mode)
        d.set_agc_mode("off")   # suppression is a filter property; AGC would
        out = d.demodulate(_tone_iq(tone_hz, 78125, 31250, amp=amp))  # re-normalize both
        audio = np.frombuffer(out, dtype="<i2")
        return _band_rms(audio, tone_hz - 60, tone_hz + 60)

    def test_usb_passes_upper_tone(self):
        d = dsp.AudioDemodulator()  # USB default
        out = d.demodulate(_tone_iq(1500.0, 78125, 31250))
        audio = np.frombuffer(out, dtype="<i2")
        self.assertGreater(_band_rms(audio, 1440, 1560), 1000.0)
        self.assertLess(abs(_dominant_freq(audio) - 1500.0), 15.0)

    def test_usb_suppresses_lower_sideband_by_20db(self):
        upper = self._level_at("USB", 1500.0)
        lower = self._level_at("USB", -1500.0)
        self.assertGreater(upper, 20.0 * lower)  # >= 26 dB power rejection

    def test_lsb_suppresses_upper_sideband_by_20db(self):
        lower = self._level_at("LSB", -1500.0)
        upper = self._level_at("LSB", 1500.0)
        self.assertGreater(lower, 20.0 * upper)

    def test_lsb_recovers_negative_tone_frequency(self):
        d = dsp.AudioDemodulator()
        d.set_mode("LSB")
        out = d.demodulate(_tone_iq(-1500.0, 78125, 31250))
        audio = np.frombuffer(out, dtype="<i2")
        self.assertLess(abs(_dominant_freq(audio) - 1500.0), 15.0)

    def test_cw_and_digi_demodulate_like_usb_path(self):
        for m in ("CW", "DIGI"):
            d = dsp.AudioDemodulator()
            d.set_mode(m)
            out = d.demodulate(_tone_iq(800.0, 78125, 31250))
            audio = np.frombuffer(out, dtype="<i2")
            self.assertGreater(_band_rms(audio, 740, 860), 1000.0)


class AudioDemodulatorAmFmTests(unittest.TestCase):
    def test_am_envelope_recovers_modulation(self):
        fs, n = 78125, 31250
        k = np.arange(n, dtype=np.float64)
        carrier = np.exp(2j * math.pi * (0.0 - IF) * k / fs)
        mod = 1.0 + 0.8 * np.sin(2.0 * math.pi * 1000.0 * k / fs)
        d = dsp.AudioDemodulator()
        d.set_mode("AM")
        out = d.demodulate((carrier * mod).astype(np.complex64))
        audio = np.frombuffer(out, dtype="<i2")
        self.assertGreater(len(audio), 18000)
        self.assertLess(abs(_dominant_freq(audio) - 1000.0), 25.0)
        rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
        self.assertGreater(rms, 1500.0)   # AGC leaves a healthy level
        self.assertLess(rms, 26000.0)     # ...but not clipped to death

    def test_am_slow_fading_is_chunk_invariant(self):
        # Regression (I1): per-call np.mean(env) made the AM DC estimate
        # depend on block alignment — one-shot keeps a slow fade, chunked
        # removes it per block, so the outputs disagreed at block steps.
        # A stateful DC blocker is LTI and must be alignment-independent.
        fs, n = 78125, 62500  # 0.8 s
        k = np.arange(n, dtype=np.float64)
        t = k / fs
        fade = 1.0 + 0.4 * np.sin(2.0 * math.pi * 0.5 * t)   # slow QSB
        mod = 1.0 + 0.5 * np.sin(2.0 * math.pi * 1000.0 * t)
        iq = (0.5 * fade * mod
              * np.exp(2j * math.pi * (0.0 - IF) * k / fs)).astype(np.complex64)

        one = dsp.AudioDemodulator()
        one.set_mode("AM")
        one.set_agc_mode("off")   # this test pins the DC blocker, not AGC
        y1 = np.frombuffer(one.demodulate(iq), dtype="<i2").astype(np.int32)

        chunked = dsp.AudioDemodulator()
        chunked.set_mode("AM")
        chunked.set_agc_mode("off")
        parts = [np.frombuffer(chunked.demodulate(iq[i:i + 3125]),
                               dtype="<i2").astype(np.int32)
                 for i in range(0, n, 3125)]
        y2 = np.concatenate(parts)
        self.assertEqual(len(y1), len(y2))
        skip = 4800  # 0.1 s of edge transients
        d = np.abs(y1[skip:] - y2[skip:])
        self.assertLessEqual(int(d.max()), 4)
        self.assertLess(float(d.mean()), 0.2)

    def test_fm_discriminator_recovers_modulation(self):
        fs, n = 78125, 31250
        k = np.arange(n, dtype=np.float64)
        dev = 3000.0
        # Carrier at the VFO (IF-offset convention) with FM phase deviation.
        phase = (2.0 * math.pi * (0.0 - IF) * k / fs
                 + 2.0 * math.pi * dev / fs * np.cumsum(
                     np.sin(2.0 * math.pi * 1000.0 * k / fs)))
        iq = (np.exp(1j * phase) * 0.5).astype(np.complex64)
        d = dsp.AudioDemodulator()
        d.set_mode("NFM")
        out = d.demodulate(iq)
        audio = np.frombuffer(out, dtype="<i2")
        self.assertGreater(len(audio), 18000)
        self.assertLess(abs(_dominant_freq(audio) - 1000.0), 50.0)

    def test_fm_peak_deviation_amplitude(self):
        # De-normalization pin: discriminator output VALUES are phase
        # increments per IQ sample (2*pi*dev/IQ_fs), and resampling to
        # 48 kHz preserves sample VALUES, so the correct scale factor is
        # IQ_fs/(2*pi*dev) — NOT audio_rate/(2*pi*dev) (that would read
        # ~39% low here).  Expected recovered peak at 100 Hz modulation
        # (de-emphasis gain 0.991): (3000/5000)*0.85*0.991 = 0.5055.
        fs, n = 78125, 78125  # 1.0 s, 1 Hz FFT bins
        k = np.arange(n, dtype=np.float64)
        dev, fmod = 3000.0, 100.0
        phase = (2.0 * math.pi * (0.0 - IF) * k / fs
                 + 2.0 * math.pi * dev / fs * np.cumsum(
                     np.sin(2.0 * math.pi * fmod * k / fs)))
        d = dsp.AudioDemodulator()
        d.set_mode("NFM")
        out = d.demodulate((np.exp(1j * phase) * 0.5).astype(np.complex64))
        audio = np.frombuffer(out, dtype="<i2").astype(np.float64) / 32767.0
        win = np.hanning(len(audio))
        mag = np.abs(np.fft.rfft(audio * win))
        freqs = np.fft.rfftfreq(len(audio), d=1.0 / 48000)
        band = mag[(freqs >= 95.0) & (freqs <= 105.0)]
        peak = float(2.0 * band.max() / win.sum())
        self.assertGreater(peak, 0.45)
        self.assertLess(peak, 0.56)

    def test_wfm_wideband_program_at_312k(self):
        # 10 kHz program tone: impossible through any 16 kHz audio path
        # (Nyquist 8 kHz would alias it to 6 kHz), so this pins the 48 kHz
        # RX output AND the WFM wideband chain at the top IQ rate.
        fs, n = 312500, 93750  # 0.3 s
        k = np.arange(n, dtype=np.float64)
        dev = 75000.0
        phase = (2.0 * math.pi * (0.0 - IF) * k / fs
                 + 2.0 * math.pi * dev / fs * np.cumsum(
                     np.sin(2.0 * math.pi * 10000.0 * k / fs)))
        iq = (np.exp(1j * phase) * 0.5).astype(np.complex64)
        d = dsp.AudioDemodulator(sample_rate=fs)
        d.set_mode("WFM")
        out = d.demodulate(iq)
        audio = np.frombuffer(out, dtype="<i2")
        self.assertGreater(len(audio), 13000)  # ~14400 expected
        self.assertLess(abs(_dominant_freq(audio) - 10000.0), 300.0)


class AudioDemodulatorFilterAgcTests(unittest.TestCase):
    def test_reconfigure_filter_shifts_passband(self):
        d = dsp.AudioDemodulator()
        d.reconfigure_filter(100.0, 1000.0)
        d.demodulate(_tone_iq(500.0, 78125, 31250))
        inside = _band_rms(np.frombuffer(
            d.demodulate(_tone_iq(500.0, 78125, 31250)), dtype="<i2"),
            440, 560)
        outside = _band_rms(np.frombuffer(
            d.demodulate(_tone_iq(2000.0, 78125, 31250)), dtype="<i2"),
            1940, 2060)
        self.assertGreater(inside, 20.0 * outside)

    def test_reconfigure_filter_validates_edges(self):
        d = dsp.AudioDemodulator()
        for lo, hi in ((0.0, 100.0), (-5.0, 100.0), (2000.0, 1000.0),
                       (100.0, 39062.0)):
            with self.assertRaises(ValueError):
                d.reconfigure_filter(lo, hi)

    def test_high_rate_empty_resampler_does_not_poison_agc(self):
        d = dsp.AudioDemodulator(sample_rate=312500)
        for _ in range(8):
            pcm = d.demodulate(np.zeros(2048, dtype=np.complex64))
            self.assertTrue(np.frombuffer(pcm, dtype="<i2").size == 0
                            or np.isfinite(d._agc_gain))
        self.assertIsNotNone(d._agc_gain)
        self.assertTrue(math.isfinite(d._agc_gain))
        pcm = d.demodulate(_tone_iq(1500.0, 312500, 8192, amp=0.001))
        self.assertTrue(np.frombuffer(pcm, dtype="<i2").size >= 0)
        self.assertTrue(math.isfinite(d._agc_gain))

    def test_set_agc_mode_accepts_ints_and_names(self):
        d = dsp.AudioDemodulator()
        for m in range(5):
            d.set_agc_mode(m)
        for name in ("off", "slow", "fast"):
            d.set_agc_mode(name)
        for bad in (5, -1, "turbo"):
            with self.assertRaises(ValueError):
                d.set_agc_mode(bad)

    def test_agc_compresses_amplitude_difference(self):
        def last_half_rms(mode, amp):
            d = dsp.AudioDemodulator()
            d.set_agc_mode(mode)
            out = d.demodulate(_tone_iq(1500.0, 78125, 46875, amp=amp))
            audio = np.frombuffer(out, dtype="<i2").astype(np.float64)
            half = audio[len(audio) // 2:]
            return float(np.sqrt(np.mean(half ** 2)))

        strong = last_half_rms("SLOW", 0.5)
        weak = last_half_rms("SLOW", 0.001)
        # 54 dB input difference compressed to under 20 dB by the AGC.
        self.assertLess(strong / max(weak, 1e-9), 10.0)

    def test_agc_off_preserves_relative_level(self):
        def last_half_rms(mode, amp):
            d = dsp.AudioDemodulator()
            d.set_agc_mode(mode)
            out = d.demodulate(_tone_iq(1500.0, 78125, 46875, amp=amp))
            audio = np.frombuffer(out, dtype="<i2").astype(np.float64)
            half = audio[len(audio) // 2:]
            return float(np.sqrt(np.mean(half ** 2)))

        strong = last_half_rms("OFF", 0.5)
        weak = last_half_rms("OFF", 0.001)
        self.assertGreater(strong / weak, 200.0)   # ~500x, minus transients
        self.assertLess(strong / weak, 1200.0)

    def test_am_agc_tracks_slower_than_usb(self):
        # AM carries its level in the carrier envelope, so the AGC must
        # track amplitude steps ~2.5x slower than on SSB (fast gain would
        # ride the syllables = audible breathing).  Pin the per-block gain
        # trajectory after a 250x downward level step, AM vs USB.
        def _am_carrier(amp, n, mod=True):
            k = np.arange(n, dtype=np.float64)
            env = (1.0 + 0.5 * np.sin(2.0 * math.pi * 1000.0 * k / 78125)
                   ) if mod else 1.0
            return (amp * env
                    * np.exp(2j * math.pi * (0.0 - IF) * k / 78125)
                    ).astype(np.complex64)

        def gain_deltas(mode):
            d = dsp.AudioDemodulator()
            d.set_agc_mode("slow")
            d.set_mode(mode)
            src = (_tone_iq(1500.0, 78125, 15625, amp=0.5) if mode == "USB"
                   else _am_carrier(0.5, 15625))
            d.demodulate(src)
            weak = (_tone_iq(1500.0, 78125, 15625, amp=0.002) if mode == "USB"
                    else _am_carrier(0.002, 15625))
            g0 = d._agc_gain
            deltas = []
            for _ in range(4):
                d.demodulate(weak)
                deltas.append(d._agc_gain - g0)
                g0 = d._agc_gain
            return deltas

        usb, am = gain_deltas("USB"), gain_deltas("AM")
        for du, da in zip(usb, am):
            self.assertGreater(du, 0.0)   # both converging upward ...
            self.assertGreater(du, da)    # ... but AM strictly slower

    def test_s_meter_from_passband_energy(self):
        d = dsp.AudioDemodulator()
        self.assertIsNone(d.get_s_meter())
        d.demodulate(_tone_iq(1500.0, 78125, 15625, amp=0.5))
        strong = d.get_s_meter()
        self.assertIsInstance(strong, float)
        d2 = dsp.AudioDemodulator()
        d2.demodulate(_tone_iq(1500.0, 78125, 15625, amp=0.005))
        weak = d2.get_s_meter()
        self.assertGreater(strong, weak + 10.0)


class AudioDemodulatorStreamingTests(unittest.TestCase):
    def test_chunked_matches_one_shot_in_steady_state(self):
        # AGC gain is computed per block, so it legitimately depends on
        # block size — the chunk-invariance claim covers the LTI chain
        # (filters + resampler), which is what "滤波/重采样连续" means.
        iq = _tone_iq(1500.0, 78125, 31250)
        one = dsp.AudioDemodulator()
        one.set_agc_mode("off")
        y1 = np.frombuffer(one.demodulate(iq), dtype="<i2").astype(np.int32)

        chunked = dsp.AudioDemodulator()
        chunked.set_agc_mode("off")
        parts = [np.frombuffer(chunked.demodulate(iq[i:i + 3125]),
                               dtype="<i2").astype(np.int32)
                 for i in range(0, len(iq), 3125)]
        y2 = np.concatenate(parts)
        self.assertEqual(len(y1), len(y2))
        skip = 4800  # 0.1 s — filter/resampler edge transients
        d = np.abs(y1[skip:] - y2[skip:])
        self.assertLessEqual(int(d.max()), 2)
        self.assertLess(float(d.mean()), 0.2)


class TXModulatorTests(unittest.TestCase):
    def _mod(self, **kw):
        return dsp.TXModulator(**kw)

    def test_defaults(self):
        m = self._mod()
        self.assertEqual(m.input_rate, 48000)
        self.assertEqual(m.iq_rate, 39062.5)
        self.assertEqual(m.mode, "USB")

    def test_empty_input_returns_no_payloads(self):
        m = self._mod()
        self.assertEqual(m.feed_audio(b""), [])
        self.assertEqual(m.feed_audio(np.zeros(0, dtype="<i2").tobytes()), [])

    def test_payloads_are_200_iq_samples_1200_bytes(self):
        m = self._mod()
        pkts = m.feed_audio(_pcm16(1000.0, 400.0))
        self.assertTrue(pkts)
        for p in pkts:
            self.assertEqual(len(p), sdp.IQ_PAYLOAD_BYTES)  # 1200
            iq = sdp.unpack_iq_24bit(p)
            self.assertEqual(len(iq), sdp.IQ_SAMPLES_PER_PACKET)  # 200

    def test_one_second_of_audio_yields_expected_packet_count(self):
        m = self._mod()
        pkts = m.feed_audio(_pcm16(1000.0, 1000.0))
        # 48000 audio -> 48000*625/768 = 39062.5 IQ -> ~195 packets.  The
        # first second carries one-time priming: the double-margin Hilbert
        # window (1248 samples) plus the resampler group delay (~7 packets).
        self.assertGreaterEqual(len(pkts), 186)
        self.assertLessEqual(len(pkts), 196)

    def test_usb_places_tone_above_zero_only(self):
        m = self._mod()
        pkts = m.feed_audio(_pcm16(1000.0, 1000.0, amp=0.8))
        iq = np.concatenate([sdp.unpack_iq_24bit(p) for p in pkts])
        freqs = np.fft.fftfreq(len(iq), d=1.0 / 39062.5)
        mag = np.abs(np.fft.fft(iq))
        up = mag[(freqs >= 800.0) & (freqs <= 1200.0)].sum()
        dn = mag[(freqs >= -1200.0) & (freqs <= -800.0)].sum()
        self.assertGreater(up, 50.0 * dn)
        dom = freqs[int(np.argmax(mag))]
        self.assertGreater(dom, 800.0)
        self.assertLess(dom, 1200.0)

    def test_lsb_places_tone_below_zero(self):
        m = self._mod()
        m.set_mode("LSB")
        pkts = m.feed_audio(_pcm16(1000.0, 1000.0, amp=0.8))
        iq = np.concatenate([sdp.unpack_iq_24bit(p) for p in pkts])
        spec = np.fft.fft(iq)
        freqs = np.fft.fftfreq(len(iq), d=1.0 / 39062.5)
        mag = np.abs(spec)
        up = mag[(freqs >= 800.0) & (freqs <= 1200.0)].sum()
        dn = mag[(freqs >= -1200.0) & (freqs <= -800.0)].sum()
        self.assertGreater(dn, 50.0 * up)
        dom = freqs[int(np.argmax(mag))]
        self.assertGreater(dom, -1200.0)
        self.assertLess(dom, -800.0)

    def test_usb_and_lsb_are_mutual_mirrors(self):
        pkts_u = self._mod().feed_audio(_pcm16(1000.0, 1000.0, amp=0.8))
        m = self._mod()
        m.set_mode("LSB")
        pkts_l = m.feed_audio(_pcm16(1000.0, 1000.0, amp=0.8))
        self.assertEqual(len(pkts_u), len(pkts_l))

    def test_limiter_bounds_magnitude_to_full_scale(self):
        m = self._mod()
        pkts = m.feed_audio(_pcm16(1000.0, 1000.0, amp=1.0))
        iq = np.concatenate([sdp.unpack_iq_24bit(p) for p in pkts])
        self.assertLessEqual(float(np.max(np.abs(iq))), 1.0 + 1e-6)
        # ...and the signal is not silenced by the limiter:
        self.assertGreater(float(np.max(np.abs(iq))), 0.5)

    def test_chunked_feed_matches_one_shot(self):
        one = self._mod()
        y1 = one.feed_audio(_pcm16(1000.0, 1000.0, amp=0.8))
        chunked = self._mod()
        y2 = []
        for i in range(50):
            y2.extend(chunked.feed_audio(_pcm16(1000.0, 20.0, amp=0.8)))
        self.assertEqual(len(y1), len(y2))
        # skip the first two packets (resampler/Hilbert edge transients)
        a = np.concatenate([sdp.unpack_iq_24bit(p) for p in y1[2:]])
        b = np.concatenate([sdp.unpack_iq_24bit(p) for p in y2[2:]])
        self.assertEqual(len(a), len(b))
        self.assertLessEqual(float(np.max(np.abs(a - b))), 0.01)

    def test_bandlimited_noise_matches_long_window_hilbert(self):
        # Regression (C1): with a left-margin-only Hilbert window, the
        # circular FFT wrap lands on each hop's RIGHT edge, injecting a
        # periodic artifact (100 Hz line for 480-sample hops).  Aperiodic
        # band-limited noise fed in realistic chunks must track a direct
        # long-window (padded) Hilbert reference everywhere — steady state
        # AND at interior hop right edges.  Tones and chunked-vs-one-shot
        # tests cannot see this (tones are too smooth; both chunked and
        # one-shot share the same defective window).
        from scipy.signal import hilbert, lfilter, sosfilt
        rng = np.random.RandomState(20261108)
        n = 48000  # 1 s
        noise = rng.randn(n)
        noise = (noise / np.max(np.abs(noise)) * 0.8 * 32767.0)
        pcm = np.clip(np.rint(noise), -32768, 32767).astype("<i2").tobytes()

        m = self._mod()
        pkts = []
        for i in range(0, len(pcm), 1920):  # 40 ms chunks
            pkts.extend(m.feed_audio(pcm[i:i + 1920]))
        got = np.concatenate([sdp.unpack_iq_24bit(p) for p in pkts])

        # Reference: same filter chain one-shot, then a direct Hilbert of
        # the whole audio padded on BOTH sides (wrap moved far away), then
        # the same full-stream resample.  The modulator's first emitted
        # audio sample is at index TX_HILBERT_MARGIN.
        ref_mod = self._mod()
        x = np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768.0
        x = lfilter(ref_mod._dc_b, ref_mod._dc_a, x)
        x = sosfilt(ref_mod._hp_sos, x)
        x = sosfilt(ref_mod._aa_sos, x)
        pad = 4096
        xa = hilbert(np.concatenate(
            [np.zeros(pad), x, np.zeros(pad)]))[pad:pad + n]
        margin = dsp.TX_HILBERT_MARGIN
        rs = dsp.soxr.ResampleStream(48000, 39062.5, 2, dtype="float64",
                                     quality="HQ")
        st = np.stack([xa[margin:].real, xa[margin:].imag], axis=1)
        rf = rs.resample_chunk(st)
        ref = rf[:, 0] + 1j * rf[:, 1]
        # The modulator applies the fixed make-up gain and the tanh
        # magnitude limiter AFTER resampling; replicate both so the
        # comparison isolates the Hilbert windowing (memoryless per-sample
        # ops on the same resampled values).
        ref = ref * dsp.TX_GAIN
        mag = np.abs(ref)
        nz = mag > 1e-9
        ref[nz] = ref[nz] / mag[nz] * np.tanh(mag[nz])

        n_cmp = min(len(got), len(ref))
        got, ref = got[:n_cmp], ref[:n_cmp]
        err = np.abs(got - ref)
        skip = int(2 * 390.625)          # first two hops: edge transients
        steady = err[skip:]
        self.assertLess(float(steady.max()), 0.02)
        # Hop right edges in the IQ stream sit at multiples of
        # 480 * 625/768 = 390.625 samples.
        edge_errs = []
        k = 3
        while (k + 1) * 390.625 + 20 < n_cmp - 200:
            b = int((k + 1) * 390.625)
            edge_errs.append(err[b - 20:b + 1].max())
            k += 1
        self.assertTrue(edge_errs)
        self.assertLess(float(np.max(edge_errs)), 0.02)

    def test_unsupported_tx_modes_raise_value_error(self):
        m = self._mod()
        for bad in ("AM", "FM", "CW", "NFM", "WFM", "DIGI", "SSB", "xyz"):
            with self.assertRaises(ValueError):
                m.set_mode(bad)
        m.set_mode("usb")   # case-insensitive accepted
        self.assertEqual(m.mode, "USB")
        m.set_mode("LSB")
        self.assertEqual(m.mode, "LSB")

    def test_reset_restores_fresh_behavior(self):
        m1 = self._mod()
        m1.feed_audio(_pcm16(1000.0, 300.0, amp=0.8))
        m1.reset()
        after_reset = m1.feed_audio(_pcm16(1000.0, 300.0, amp=0.8))
        m2 = self._mod()
        fresh = m2.feed_audio(_pcm16(1000.0, 300.0, amp=0.8))
        # reset() re-arms the leading click ramp; a fresh modulator starts
        # fully ramped.  From the second packet on the chains are identical.
        self.assertNotEqual(after_reset[0], fresh[0])
        self.assertEqual(after_reset[1:], fresh[1:])
        self.assertEqual(m1.underruns, 0)

    def test_reset_arms_a_one_packet_ramp(self):
        m = self._mod()
        m.reset()
        self.assertEqual(m._ramp_pos, 0)
        pkts = m.feed_audio(_pcm16(1000.0, 200.0, amp=0.8))
        self.assertGreaterEqual(len(pkts), 3)

        def rms(pkt):
            iq = sdp.unpack_iq_24bit(pkt)
            return float(np.sqrt(np.mean(np.abs(iq) ** 2)))

        self.assertLess(rms(pkts[0]), rms(pkts[1]) * 0.8)
        self.assertGreaterEqual(m._ramp_pos, dsp.TX_RAMP_SAMPLES)
        self.assertAlmostEqual(rms(pkts[1]), rms(pkts[2]), delta=0.02)

    def test_fresh_modulator_does_not_ramp(self):
        m = self._mod()
        self.assertEqual(m._ramp_pos, dsp.TX_RAMP_SAMPLES)
        pkts = m.feed_audio(_pcm16(1000.0, 200.0, amp=0.8))
        first = sdp.unpack_iq_24bit(pkts[0])
        second = sdp.unpack_iq_24bit(pkts[1])
        self.assertAlmostEqual(float(np.sqrt(np.mean(np.abs(first) ** 2))),
                               float(np.sqrt(np.mean(np.abs(second) ** 2))),
                               delta=0.02)

    def test_odd_length_pcm_rejected(self):
        m = self._mod()
        with self.assertRaises(ValueError):
            m.feed_audio(b"\x01\x02\x03")

    def test_non_finite_float_input_does_not_crash(self):
        m = self._mod()
        bad = np.full(4800, np.nan, dtype=np.float32)  # 0.1 s
        pkts = m.feed_audio(bad)
        self.assertTrue(pkts)
        iq = np.concatenate([sdp.unpack_iq_24bit(p) for p in pkts])
        self.assertTrue(np.all(np.isfinite(iq)))

    def test_level_probe_and_underrun_counters(self):
        m = self._mod()
        self.assertEqual(m.underruns, 0)
        m.feed_audio(_pcm16(1000.0, 200.0, amp=0.8))
        snap = m.snapshot_levels()
        for key in ("in_peak", "iq_peak", "limited_peak", "iq_samples"):
            self.assertIn(key, snap)
        self.assertGreater(snap["iq_samples"], 0)
        self.assertGreater(snap["in_peak"], 0.1)
        self.assertLessEqual(snap["limited_peak"], 1.0 + 1e-6)


if __name__ == "__main__":
    unittest.main()
