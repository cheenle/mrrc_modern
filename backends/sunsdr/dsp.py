"""SunSDR2 DX — pure DSP layer (native backend, Task 2).

Hardware-free signal processing on top of Task 1's wire codec:

- :func:`iq_to_spectrum` — Hann FFT + fftshift + fixed dB window + linear
  resample to 850 uint8 bins for the waterfall.  Fixed window (never
  auto-ranging), so a given RF level always lands on the same pixel.
- :class:`AudioDemodulator` — streaming IQ -> 48 kHz int16-le RX audio.
  The device places the VFO at ``-IF_OFFSET_HZ`` inside the IQ stream
  (RX DDS = (VFO + 30500) x 10, Task 1), so demodulation first shifts UP
  by +30500 Hz with a phase-continuous LO, then a complex FIR selects the
  sideband (USB/LSB/CW/DIGI), AM uses the |.| envelope, FM a polar
  discriminator whose last sample is carried across blocks (the reference
  server's block-edge buzz fix).  Detected audio is resampled to 48 kHz
  through a *streaming* soxr resampler — chunk boundaries introduce no
  phase step — then a small vectorized AGC (per-block gain, no per-sample
  Python loops) and int16 conversion.  S-meter is computed from the
  passband IQ energy; until the first block it is None, never fabricated.
- :class:`TXModulator` — streaming 48 kHz int16 mic -> 200-sample (1200
  byte) 24-bit IQ payloads at 5^7/2 = 39062.5 Hz (the measured TX rate:
  ExpertSDR3 transmits at half the RX rate).  Chain: continuous DC blocker
  (20 Hz IIR), 300 Hz 4th-order Butterworth HPF (SSB power efficiency,
  measured 69% -> 96% in the reference), 3.6 kHz anti-alias LPF, Hilbert
  overlap-save SSB (USB analytic / LSB conjugate), fixed make-up gain and
  a tanh magnitude limiter at full-scale ceiling (a hard clip splatters;
  verified against the ExpertSDR3 drive-sweep capture in the reference
  project), packed with Task 1's :func:`pack_iq_24bit`.

Like the reference DSP (/Users/cheenle/HAM/sunsdr/web_control/dsp.py) but
deliberately NOT copied: no server coupling, no debug-WAV capture, and the
RX audio rate is 48 kHz end-to-end (the reference's 15625 Hz cap is gone).

The hardened Python demodulator remains the always-available fallback.
When the optional WDSP shared library is present (``vendor/libwdsp.so`` on
Linux, ``vendor/libwdsp.dylib`` on macOS, or ``MRRC_WDSP_LIB``), the
RX path can run WDSP's NR2 / NB / ANF on the complex baseband *before*
demodulation — the architecture WDSP was designed for.  WDSP is never a
startup dependency: an absent or incompatible library leaves the Python
path in place and the backend reports the controls as unsupported.

Pure logic: no sockets, no asyncio — the engine task owns transport.
"""
from __future__ import annotations

import logging
import math
import os
import time
from typing import Optional, Union

import numpy as np
import soxr
from scipy.signal import butter, firwin, hilbert, lfilter, sosfilt

from . import wdsp as _wdsp_lib

logger = logging.getLogger(__name__)

from .protocol import (
    IF_OFFSET_HZ,
    IQ_SAMPLES_PER_PACKET,
    IQ_PAYLOAD_BYTES,
    pack_iq_24bit,
)

__all__ = [
    "IF_OFFSET",
    "AUDIO_RATE",
    "TX_IQ_RATE",
    "SPECTRUM_FFT_SIZE",
    "SPECTRUM_DB_FLOOR",
    "iq_to_spectrum",
    "AudioDemodulator",
    "TXModulator",
    "WDSP_AUDIO_RATE",
    "wdsp_available",
]

# RX IF shift: the LO e^{+j*2*pi*IF*n} is periodic in the sample rate, so
# it is exact at EVERY fs — at 39062.5 it equals a -8562.5 Hz rotation,
# which still mixes the VFO (at -IF in the IQ stream) exactly to 0 with
# NO conjugation and NO sideband swap (verified by the 39062.5 tests).
IF_OFFSET = IF_OFFSET_HZ                      # 30500.0 Hz
AUDIO_RATE = 48000                            # RX audio output rate
SPECTRUM_FFT_SIZE = 2048
SPECTRUM_DB_FLOOR = -120.0                    # dB, maps to 0

# TX: 5^7/2 (measured ExpertSDR3 TX rate, half the 78125 RX rate).
TX_IQ_RATE = 39062.5
TX_GAIN = 2.8         # flat make-up gain (reference-tuned; AGC here pumped
                      # the SSB envelope and made the far end hear a
                      # trembling voice, so a constant is used by design)
TX_CEILING = 1.0      # tanh soft-limit knee = 24-bit full scale
TX_HILBERT_MARGIN = 384   # context samples each side inside the Hilbert block
TX_BLOCK = 480            # audio samples emitted per Hilbert hop (10 ms)
# Linear 0->1 amplitude ramp on the first IQ after PTT.  The verified
# reference has the same 200-sample (~5.1 ms) ramp: the pacer emits silent
# packets while the mic queue primes, and without this ramp the first real
# packet steps from zero to full modulation (an audible click at every
# key-up).  Re-armed by TXModulator.reset() on each PTT assert.
TX_RAMP_SAMPLES = 200

# WDSP IQ channel.  The native processor demodulates at the IQ rate and
# emits audio at the integer-decimated rate; 1024 is the reference's tuned
# block (6.5 ms at 156 kS/s) and keeps the FFT context wide enough for NR2.
WDSP_AUDIO_RATE = 15625.0
WDSP_BUFFER_SIZE = 1024


def wdsp_available() -> bool:
    """True when the optional WDSP native library loaded successfully."""
    return bool(_wdsp_lib.available())


def _wdsp_allowed() -> bool:
    """Honor the ``MRRC_WDSP`` escape hatch (auto/on/off).

    Auto is the production default: use the library when it is present.
    Tests and a deliberately CPU-thrifty deployment set ``MRRC_WDSP=off``
    to keep the deterministic Python demodulator.
    """
    val = (os.environ.get("MRRC_WDSP") or "").strip().lower()
    if val in ("0", "off", "false", "no", "disabled"):
        return False
    if val in ("1", "on", "true", "yes", "enabled"):
        return True
    return _wdsp_lib.available()


# ── Spectrum ──────────────────────────────────────────────────────

def iq_to_spectrum(samples, sample_rate: float,
                   bins: int = 850,
                   fft_size: int = SPECTRUM_FFT_SIZE,
                   if_shift_hz: float = 0.0) -> list:
    """IQ -> ``bins`` uint8 dB magnitudes (list[int], 0..255).

    The newest ``fft_size`` samples are Hann-windowed, FFT-shifted (bin 0 =
    -fs/2, so a signal ABOVE the tuned VFO lands RIGHT of center and one
    BELOW lands LEFT), converted with a FIXED [-120, 0] dB window, then
    linearly resampled to ``bins`` points.

    ``if_shift_hz`` is applied to the samples before the FFT.  The device
    puts the VFO at ``-IF_OFFSET_HZ`` inside its IQ stream (RX DDS = VFO +
    30500 Hz), so the raw stream is LO-centred: without the shift a genuine
    VFO signal appears 30.5 kHz left of the display centre and every
    waterfall label/click-to-tune is off by that amount.  The RX
    demodulator already applies the same +IF shift; this is the spectrum
    half of the same contract (verified against the reference server's
    time-domain rotation).

    Empty input -> all zeros.  Non-finite samples are sanitized; bad
    rates/bins/shift raise ValueError.
    """
    try:
        fs = float(sample_rate)
    except (TypeError, ValueError):
        raise ValueError(f"sample_rate must be a number, got {sample_rate!r}") \
            from None
    if not math.isfinite(fs) or fs <= 0.0:
        raise ValueError(f"sample_rate must be finite and > 0, got {fs!r}")
    bins = int(bins)
    fft_size = int(fft_size)
    if bins < 1:
        raise ValueError(f"bins must be >= 1, got {bins}")
    if fft_size < 2:
        raise ValueError(f"fft_size must be >= 2, got {fft_size}")
    try:
        shift = float(if_shift_hz)
    except (TypeError, ValueError):
        raise ValueError(
            f"if_shift_hz must be a number, got {if_shift_hz!r}") from None
    if not math.isfinite(shift):
        raise ValueError(f"if_shift_hz must be finite, got {shift!r}")

    arr = np.asarray(samples, dtype=np.complex64).reshape(-1)
    if arr.size == 0:
        return [0] * bins
    re = np.nan_to_num(arr.real, nan=0.0, posinf=0.0, neginf=0.0)
    im = np.nan_to_num(arr.imag, nan=0.0, posinf=0.0, neginf=0.0)
    arr = (re + 1j * im).astype(np.complex64)

    if arr.size >= fft_size:
        frame = arr[-fft_size:]
    else:
        frame = np.zeros(fft_size, dtype=np.complex64)
        frame[:arr.size] = arr

    if shift:
        # Exact time-domain rotation.  At 39.0625 kHz the +30500 Hz LO is
        # periodic (it equals -8562.5 Hz), so the same expression also
        # centres the aliased VFO at that rate.
        n = np.arange(fft_size, dtype=np.float64)
        frame = frame * np.exp(
            2j * math.pi * shift * n / fs).astype(np.complex64)

    win = np.hanning(fft_size).astype(np.float32)
    spec = np.fft.fftshift(np.abs(np.fft.fft(frame * win)))
    db = 20.0 * np.log10(spec.astype(np.float64) + 1e-10)
    u = np.clip((db - SPECTRUM_DB_FLOOR) / (-SPECTRUM_DB_FLOOR), 0.0, 1.0)
    xi = np.linspace(0.0, float(fft_size - 1), bins)
    out = np.interp(xi, np.arange(fft_size, dtype=np.float64), u)
    return [int(v) for v in np.rint(out * 255.0)]


# ── RX demodulator ────────────────────────────────────────────────

def _fir_lowpass(cutoff: float, transition: float, fs: float) -> np.ndarray:
    """Windowed-sinc lowpass, taps scaled to unity DC gain."""
    cutoff = min(float(cutoff), 0.45 * fs)
    transition = max(float(transition), 50.0)
    ntaps = int(math.ceil(4.0 * fs / transition)) | 1
    ntaps = min(ntaps, 8191)
    return firwin(ntaps, cutoff, window="hamming", fs=fs)


def _complex_bandpass(lo: float, hi: float, fs: float) -> np.ndarray:
    """Complex FIR with passband [lo, hi] Hz (modulated lowpass prototype)."""
    width = float(hi) - float(lo)
    center = (float(hi) + float(lo)) / 2.0
    proto = _fir_lowpass(width / 2.0, max(100.0, width * 0.35), fs)
    n = np.arange(len(proto), dtype=np.float64)
    return proto * np.exp(2j * math.pi * center * n / fs)


class AudioDemodulator:
    """Streaming IQ -> 48 kHz int16-le RX audio (see module docstring)."""

    _MODES = ("USB", "LSB", "CW", "DIGI", "AM", "NFM", "FM", "WFM")
    _AGC_NAMES = {"off": 0, "long": 1, "slow": 2, "med": 3,
                  "medium": 3, "fast": 4}

    def __init__(self, sample_rate: Union[int, float] = 78125,
                 audio_rate: Union[int, float] = AUDIO_RATE):
        fs = float(sample_rate)
        if not math.isfinite(fs) or fs <= 0.0:
            raise ValueError(
                f"sample_rate must be finite and > 0, got {sample_rate!r}")
        ar = float(audio_rate)
        if not math.isfinite(ar) or ar <= 0.0:
            raise ValueError(f"audio_rate must be > 0, got {audio_rate!r}")
        self.sample_rate = sample_rate
        self.audio_rate = audio_rate
        self.mode = "USB"
        self._bp_low = 300.0
        self._bp_high = 3000.0
        self._lo_phase = 0.0
        self._agc_gain: Optional[float] = None
        self._agc_mode = 2                    # SLOW
        self._s_power_ema: Optional[float] = None
        self._fm_prev: Optional[complex] = None
        self._st_deemph = 0.0
        self._build_filters()
        self._rs = soxr.ResampleStream(float(self.sample_rate),
                                       float(self.audio_rate), 1,
                                       dtype="float32", quality="HQ")

        # ── Optional WDSP IQ channel ───────────────────────────────
        # The defaults are the UI's defaults: NR/NB/AN off, AGC SLOW.
        # Turning them on is a user action, never an invisible DSP default.
        self._nr2 = False
        self._nb = False
        self._anf = False
        self._nr_level = 8                 # UI 1..15
        self._nb_level = 5                 # UI 0..10 (no WDSP level; see below)
        self._wdsp_iq = None
        self._wdsp_rs = None
        self._wdsp_iq_tail = np.zeros(0, dtype=np.complex64)
        self._wdsp_enabled = _wdsp_allowed()
        self._wdsp_post_gain = 1.0
        self._wdsp_level_ready = False
        self._wdsp_failures = 0
        self._wdsp_blocks = 0
        self._wdsp_nr_pending = False
        self._init_wdsp()

    # ── Filter construction ──────────────────────────────────────

    def _build_filters(self):
        fs = float(self.sample_rate)
        # SSB sideband pair: bp passes [lo, hi]; its conjugate is the same
        # real prototype modulated by -fc, i.e. passes [-hi, -lo].
        bp = _complex_bandpass(self._bp_low, self._bp_high, fs)
        self._bp_usb = bp
        self._bp_lsb = np.conj(bp)
        self._st_usb = np.zeros(len(bp) - 1, dtype=np.complex128)
        self._st_lsb = np.zeros(len(bp) - 1, dtype=np.complex128)

        # AM: complex channel limit (±6 kHz) before the |.| envelope, then
        # a 5 kHz audio lowpass.  The abs() detector is nonlinear — without
        # the channel limit, adjacent signals fold in via cross-products.
        am_ch = _fir_lowpass(6000.0, 2000.0, fs)
        self._am_ch = am_ch.astype(np.complex128)
        self._st_am_ch = np.zeros(len(am_ch) - 1, dtype=np.complex128)
        am_audio = _fir_lowpass(5000.0, 1500.0, fs)
        self._am_audio = am_audio
        self._st_am_audio = np.zeros(len(am_audio) - 1, dtype=np.float64)
        # Envelope DC blocker: first-order IIR highpass @ 20 Hz with state
        # carried across calls.  (Per-call mean subtraction made the DC
        # estimate block-alignment dependent — chunked vs one-shot output
        # diverged on slow fades; the IIR has a fixed frequency response
        # regardless of how the stream is chopped.)
        r = math.exp(-2.0 * math.pi * 20.0 / fs)
        self._am_dc_b = np.array([1.0, -1.0], dtype=np.float64)
        self._am_dc_a = np.array([1.0, -r], dtype=np.float64)
        self._st_am_dc = 0.0

        # FM: complex channel limit before the discriminator.
        nfm_ch = _fir_lowpass(8000.0, 2500.0, fs)
        self._nfm_ch = nfm_ch.astype(np.complex128)
        self._st_nfm_ch = np.zeros(len(nfm_ch) - 1, dtype=np.complex128)
        nfm_audio = _fir_lowpass(3400.0, 1000.0, fs)
        self._nfm_audio = nfm_audio
        self._st_nfm_audio = np.zeros(len(nfm_audio) - 1, dtype=np.float64)

        wfm_ch = _fir_lowpass(min(100000.0, 0.45 * fs), 25000.0, fs)
        self._wfm_ch = wfm_ch.astype(np.complex128)
        self._st_wfm_ch = np.zeros(len(wfm_ch) - 1, dtype=np.complex128)
        wfm_audio = _fir_lowpass(min(15000.0, 0.46 * float(self.audio_rate)),
                                 3000.0, fs)
        self._wfm_audio = wfm_audio
        self._st_wfm_audio = np.zeros(len(wfm_audio) - 1, dtype=np.float64)

    # ── Control ──────────────────────────────────────────────────

    def set_mode(self, mode: str):
        m = str(mode).strip().upper()
        if m not in self._MODES:
            raise ValueError(
                f"unsupported RX mode {mode!r}; one of {self._MODES}")
        self.mode = m
        if self._wdsp_iq is not None:
            try:
                self._wdsp_iq.set_mode(self._wdsp_mode_for(m))
            except Exception as e:                   # noqa: BLE001
                logger.warning("SunSDR WDSP mode change failed: %s", e)

    def set_sample_rate(self, hz: Union[int, float]):
        """Change IQ rate (engine calls this on a device re-boot) and reset
        all streaming state so no stale phase/filter memory survives."""
        fs = float(hz)
        if not math.isfinite(fs) or fs <= 0.0:
            raise ValueError(f"sample_rate must be finite and > 0, got {hz!r}")
        self.sample_rate = hz
        self._lo_phase = 0.0
        self._fm_prev = None
        self._st_deemph = 0.0
        self._agc_gain = None
        self._s_power_ema = None
        self._build_filters()
        self._rs = soxr.ResampleStream(float(self.sample_rate),
                                       float(self.audio_rate), 1,
                                       dtype="float32", quality="HQ")
        # The WDSP IQ channel is rate-bound; rebuild it for the new span.
        self._init_wdsp()

    def reconfigure_filter(self, low_hz: float = 300.0, high_hz: float = 3000.0):
        """New SSB passband edges (Hz); both sideband filters are rebuilt."""
        fs = float(self.sample_rate)
        lo, hi = float(low_hz), float(high_hz)
        if not (math.isfinite(lo) and math.isfinite(hi)):
            raise ValueError("filter edges must be finite")
        if lo <= 0.0 or hi <= lo or hi > 0.45 * fs:
            raise ValueError(
                f"need 0 < low < high <= {0.45 * fs:g} Hz (0.45*fs),"
                f" got ({low_hz}, {high_hz})")
        self._bp_low, self._bp_high = lo, hi
        self._build_filters()
        if self._wdsp_iq is not None:
            try:
                self._wdsp_iq.set_bandpass(lo, hi)
            except Exception as e:                   # noqa: BLE001
                logger.warning("SunSDR WDSP bandpass update failed: %s", e)

    def set_agc_mode(self, mode):
        """0=OFF, 1=LONG, 2=SLOW, 3=MED, 4=FAST — int or name."""
        if isinstance(mode, str):
            key = mode.strip().lower()
            if key not in self._AGC_NAMES:
                raise ValueError(
                    f"unknown AGC mode {mode!r}; int 0..4 or"
                    f" {sorted(self._AGC_NAMES)}")
            mode = self._AGC_NAMES[key]
        mode = int(mode)
        if not 0 <= mode <= 4:
            raise ValueError(f"AGC mode must be 0..4, got {mode}")
        self._agc_mode = mode
        if self._wdsp_iq is not None:
            try:
                self._wdsp_iq.set_agc_mode(mode)
            except Exception as e:                   # noqa: BLE001
                logger.warning("SunSDR WDSP AGC update failed: %s", e)

    def get_s_meter(self) -> Optional[float]:
        """Passband IQ power in dB (EMA), or None before the first block."""
        if self._s_power_ema is None:
            return None
        return float(10.0 * math.log10(self._s_power_ema + 1e-15))

    # ── WDSP (optional) ──────────────────────────────────────────

    def _init_wdsp(self):
        """Create/recreate the IQ-rate WDSP channel for the current rate.

        Called from the constructor and from set_sample_rate().  Every
        failure is non-fatal: the Python demodulator remains the fallback
        and the backend capability flags turn false, so the UI never shows
        a control that cannot act.
        """
        had_wdsp = self._wdsp_iq is not None
        self._close_wdsp()
        self._wdsp_post_gain = 1.0
        self._wdsp_level_ready = False
        if had_wdsp:
            # CloseChannel wakes the WDSP thread with a 25 ms Sleep; opening
            # the same channel immediately after can leave the new chain
            # silent (measured on the 78k -> 156k span switch).
            time.sleep(0.08)
        if not self._wdsp_enabled or not _wdsp_lib.available():
            return
        try:
            mode = self._wdsp_mode_for(self.mode)
            proc = _wdsp_lib.WDSPIQProcessor(
                iq_sample_rate=int(self.sample_rate),
                buffer_size=WDSP_BUFFER_SIZE,
                mode=mode,
                # Prime with NR2 on.  A WDSP RXA channel whose EMNR, ANF
                # and SNB are all off at open time emits zeros until the
                # chain has processed one block (measured on macOS and
                # aarch64).  If the user wants NR off, the first block
                # runs with it and _finish_wdsp_prime() turns it off after
                # that block; runtime off/on toggles are immediate.
                enable_nr2=True,
                enable_nb=False,
                enable_anf=False,
                agc_mode=_wdsp_lib.WDSPAGCMode.SLOW,
            )
            proc.set_agc_mode(int(self._agc_mode))
            proc.set_nr2_level(self._wdsp_nr_level())
            # Do NOT enable WDSP's SNB: snb.c has a use-after-free that
            # aborts the process (measured on macOS arm64 and aarch64,
            # 2026-11-09).  The NB button therefore uses the safe
            # time-domain blanker below, not SetRXASNBARun.
            proc.set_anf_enabled(bool(self._anf))
            proc.set_bandpass(self._bp_low, self._bp_high)
            proc.set_mode(mode)
            # The wrapper's integer decimation emits
            # iq_rate * (buffer_size // decim) / buffer_size, not exactly
            # 15625 Hz.  Resample from the true rate so pitch is exact.
            out_rate = float(getattr(proc, "out_rate", WDSP_AUDIO_RATE))
            self._wdsp_iq = proc
            self._wdsp_iq_tail = np.zeros(0, dtype=np.complex64)
            self._wdsp_rs = soxr.ResampleStream(
                out_rate, float(self.audio_rate), 1,
                dtype="float32", quality="HQ")
            self._wdsp_blocks = 0
            self._wdsp_nr_pending = not self._nr2
            if self._nr2:
                proc.set_nr2_enabled(True)
            logger.info(
                "SunSDR WDSP ready: IQ %d Hz, audio %.1f Hz, buffer %d",
                int(self.sample_rate), out_rate, WDSP_BUFFER_SIZE)
        except Exception as e:                       # noqa: BLE001
            logger.warning("SunSDR WDSP unavailable, using Python demod: %s", e)
            self._wdsp_iq = None
            self._wdsp_rs = None
            self._wdsp_iq_tail = np.zeros(0, dtype=np.complex64)

    def _close_wdsp(self):
        proc, self._wdsp_iq = self._wdsp_iq, None
        self._wdsp_rs = None
        self._wdsp_iq_tail = np.zeros(0, dtype=np.complex64)
        if proc is not None:
            try:
                proc.close()
            except Exception:                        # noqa: BLE001
                pass

    @staticmethod
    def _wdsp_mode_for(mode: str):
        return {
            "USB": _wdsp_lib.WDSPMode.USB,
            "LSB": _wdsp_lib.WDSPMode.LSB,
            # MRRC's CW demod is USB-shaped audio (the pitch comes from the
            # operator's CW offset), so WDSP must use USB, not its CW mode.
            "CW": _wdsp_lib.WDSPMode.USB,
            "DIGI": _wdsp_lib.WDSPMode.USB,
            "AM": _wdsp_lib.WDSPMode.AM,
        }.get(mode, _wdsp_lib.WDSPMode.USB)

    def _wdsp_active(self) -> bool:
        return (self._wdsp_iq is not None
                and self._wdsp_rs is not None
                and self.mode not in ("NFM", "FM", "WFM"))

    def wdsp_available(self) -> bool:
        return self._wdsp_iq is not None

    def _wdsp_nr_level(self) -> int:
        """Map the UI's 1..15 NR level to WDSP's 0..100 strength."""
        return int(round((max(1, min(15, self._nr_level)) - 1) * 100 / 14))

    def _wdsp_agc(self) -> int:
        return max(0, min(4, int(self._agc_mode)))

    def set_noise_reduction(self, on: bool) -> bool:
        self._nr2 = bool(on)
        if self._wdsp_iq is None:
            return False
        if not self._nr2 and self._wdsp_blocks == 0:
            # Defer until the channel has produced one block; see
            # _init_wdsp().  The first block is processed with NR2 on, then
            # _finish_wdsp_prime() switches it off.
            self._wdsp_nr_pending = True
            return True
        self._wdsp_nr_pending = False
        self._wdsp_iq.set_nr2_enabled(self._nr2)
        return True

    def _finish_wdsp_prime(self):
        """Apply a pending NR-off after the WDSP chain's first live block."""
        if not self._wdsp_nr_pending or self._wdsp_iq is None:
            return
        self._wdsp_nr_pending = False
        try:
            self._wdsp_iq.set_nr2_enabled(False)
        except Exception as e:                       # noqa: BLE001
            logger.warning("SunSDR WDSP NR-off after prime failed: %s", e)

    def set_noise_blanker(self, on: bool) -> bool:
        # Software impulse blanker on the complex baseband.  It runs on
        # both the Python and WDSP paths and deliberately does not touch
        # WDSP's SNB (see _init_wdsp).  Always available.
        self._nb = bool(on)
        return True

    def set_auto_notch(self, on: bool) -> bool:
        self._anf = bool(on)
        if self._wdsp_iq is None:
            return False
        self._wdsp_iq.set_anf_enabled(self._anf)
        return True

    def set_nr_level(self, level: int) -> bool:
        self._nr_level = max(1, min(15, int(level)))
        if self._wdsp_iq is None:
            return False
        self._wdsp_iq.set_nr2_level(self._wdsp_nr_level())
        return True

    def get_nr_level(self) -> Optional[int]:
        return self._nr_level if self._wdsp_iq is not None else None

    def set_nb_level(self, level: int) -> bool:
        # 0 = least aggressive (only huge spikes), 10 = most aggressive.
        self._nb_level = max(0, min(10, int(level)))
        return True

    def get_nb_level(self) -> Optional[int]:
        return self._nb_level

    # ── Demodulation ─────────────────────────────────────────────

    def demodulate(self, iq) -> bytes:
        """One IQ block -> int16-le 48 kHz audio bytes (b'' for empty)."""
        x = np.asarray(iq, dtype=np.complex64).reshape(-1)
        if x.size == 0:
            return b""
        re = np.nan_to_num(x.real, nan=0.0, posinf=0.0, neginf=0.0)
        im = np.nan_to_num(x.imag, nan=0.0, posinf=0.0, neginf=0.0)
        x = (re + 1j * im).astype(np.complex64)

        fs = float(self.sample_rate)
        n = x.size
        # Phase-continuous +IF shift: VFO at -IF_OFFSET -> 0 Hz.
        dphi = 2.0 * math.pi * IF_OFFSET / fs
        k = np.arange(n, dtype=np.float64)
        phases = self._lo_phase + dphi * k
        self._lo_phase = float((phases[-1] + dphi) % (2.0 * math.pi))
        bb = x.astype(np.complex128) * np.exp(1j * phases)
        if self._nb:
            bb = self._apply_noise_blanker(bb)

        # S-meter from the complex baseband, independent of which demod
        # runs next.  The Python path refines it with its channel filter
        # below; the WDSP path uses this full-rate estimate (its own
        # GetRXAMeter is not calibrated to MRRC's dB scale).
        p = float(np.mean(np.abs(bb) ** 2))
        self._s_power_ema = (p if self._s_power_ema is None
                             else 0.7 * self._s_power_ema + 0.3 * p)

        mode = self.mode
        # ── Optional WDSP IQ demodulation ────────────────────────
        # WDSP receives the already-IF-shifted complex baseband and emits
        # 15.625 kHz audio.  Its internal AGC replaces the Python AGC; the
        # slow post-gain below is only a long-term leveler, never a second
        # syllabic AGC.  FM stays on the Python discriminator: WDSP's
        # NR2/ANF are tuned for SSB/CW/AM spectra.
        if self._wdsp_active():
            try:
                # The WDSP IQ processor's ring is one 1024-IQ block.  The
                # engine delivers 3125-IQ blocks; handing it 3125 at once
                # made its `want = n * ratio` underflow and pad ~half of
                # every output block with zeros (audible buzz/distortion,
                # field report 2026-11-09).  Feed exact 1024-IQ blocks and
                # carry the remainder into the next call; each call then
                # produces exactly its 205/103 audio samples.
                buf = (bb if self._wdsp_iq_tail.size == 0
                       else np.concatenate([self._wdsp_iq_tail, bb]))
                full = (buf.size // WDSP_BUFFER_SIZE) * WDSP_BUFFER_SIZE
                parts = []
                for off in range(0, full, WDSP_BUFFER_SIZE):
                    parts.append(self._wdsp_iq.process_iq(
                        buf[off:off + WDSP_BUFFER_SIZE]))
                self._wdsp_iq_tail = buf[full:].copy()
                wdsp_audio = (np.concatenate(parts) if parts
                              else np.zeros(0, dtype=np.float32))
            except Exception as e:                   # noqa: BLE001
                self._wdsp_failures += 1
                logger.warning(
                    "SunSDR WDSP IQ path failed (%d): %s — using Python demod",
                    self._wdsp_failures, e)
                if self._wdsp_failures >= 3:
                    self._close_wdsp()
                wdsp_audio = None
            if wdsp_audio is not None and wdsp_audio.size:
                self._wdsp_blocks += 1
                self._finish_wdsp_prime()
                out = self._wdsp_rs.resample_chunk(
                    wdsp_audio.astype(np.float32)).astype(np.float64)
                out = self._wdsp_level(out)
                np.clip(out, -1.0, 1.0, out=out)
                i16 = np.clip(np.rint(out * 32767.0), -32768, 32767).astype("<i2")
                return i16.tobytes()
        if mode in ("USB", "CW", "DIGI"):
            filt, self._st_usb = lfilter(self._bp_usb, [1.0], bb,
                                         zi=self._st_usb)
            band = filt
            audio = filt.real.copy()
        elif mode == "LSB":
            filt, self._st_lsb = lfilter(self._bp_lsb, [1.0], bb,
                                         zi=self._st_lsb)
            band = filt
            audio = filt.real.copy()
        elif mode == "AM":
            ch, self._st_am_ch = lfilter(self._am_ch, [1.0], bb,
                                         zi=self._st_am_ch)
            band = ch
            env = np.abs(ch)
            env, self._st_am_audio = lfilter(self._am_audio, [1.0], env,
                                             zi=self._st_am_audio)
            env, zf = lfilter(self._am_dc_b, self._am_dc_a, env,
                              zi=[self._st_am_dc])
            self._st_am_dc = float(zf[0])
            audio = env
        else:  # NFM / FM / WFM
            if mode == "WFM":
                ch, self._st_wfm_ch = lfilter(self._wfm_ch, [1.0], bb,
                                              zi=self._st_wfm_ch)
                audio_lpf, st_audio = self._wfm_audio, "st_wfm_audio"
            else:
                ch, self._st_nfm_ch = lfilter(self._nfm_ch, [1.0], bb,
                                              zi=self._st_nfm_ch)
                audio_lpf, st_audio = self._nfm_audio, "st_nfm_audio"
            band = ch
            # Polar discriminator: per-sample phase increment, wrapped to
            # (-pi, pi].  The last sample of the previous block seeds the
            # first product so the seam is continuous (the reference
            # server's ~195 Hz block-edge buzz fix).
            prev = self._fm_prev if self._fm_prev is not None else ch[0]
            shifted = np.empty_like(ch)
            shifted[0] = prev
            shifted[1:] = ch[:-1]
            audio = np.angle(ch * np.conj(shifted)).astype(np.float64)
            self._fm_prev = complex(ch[-1])
            st = getattr(self, "_" + st_audio)
            audio, new_st = lfilter(audio_lpf, [1.0], audio, zi=st)
            setattr(self, "_" + st_audio, new_st)

        # S-meter from the passband (channel-filtered) IQ energy.
        p = float(np.mean(np.abs(band) ** 2))
        self._s_power_ema = (p if self._s_power_ema is None
                             else 0.7 * self._s_power_ema + 0.3 * p)

        out = self._rs.resample_chunk(
            audio.astype(np.float32)).astype(np.float64)

        if mode in ("NFM", "FM", "WFM"):
            # Fixed de-normalization (NOT AGC — FM is constant-envelope, a
            # tracking gain only pumps).  The discriminator output VALUES
            # are phase increments per IQ sample (full deviation peaks at
            # 2*pi*dev/IQ_fs radians), and the streaming resampler above
            # preserves sample VALUES — so the scale factor must use the
            # IQ rate fs, NOT audio_rate (which would read low by the
            # fs/48000 ratio).  Pinned by
            # test_fm_peak_deviation_amplitude.
            dev = 75000.0 if mode == "WFM" else 5000.0
            out *= fs / (2.0 * math.pi * dev) * 0.85
            # De-emphasis (one-pole at the AUDIO rate, state carried).
            tau = 50e-6 if mode == "WFM" else 75e-6
            a = math.exp(-1.0 / (tau * float(self.audio_rate)))
            out, zi = lfilter([1.0 - a], [1.0, -a], out,
                              zi=[self._st_deemph])
            self._st_deemph = float(zi[0])
        elif self._agc_mode == 0:
            pass                                    # AGC off: unity gain
        else:
            out = self._apply_agc(out, mode)

        np.clip(out, -1.0, 1.0, out=out)
        i16 = np.clip(np.rint(out * 32767.0), -32768, 32767).astype("<i2")
        return i16.tobytes()

    def _apply_noise_blanker(self, x: np.ndarray) -> np.ndarray:
        """Impulse blanker on the complex baseband (NB button).

        An impulse is a short burst far above the block's median envelope.
        Zeroing a small expanded neighbourhood removes the click without
        touching ordinary signal; if more than 10 % of the block qualifies
        it is noise or modulation, not an impulse, so the block is left
        alone.  Level 0 is the least aggressive (12x median), level 10 the
        most (3x); the UI's 0..10 NB-level slider drives this directly.
        """
        if x.size < 8:
            return x
        env = np.abs(x)
        med = float(np.median(env))
        if not math.isfinite(med) or med <= 1e-9:
            return x
        ratio = 12.0 - 0.9 * max(0, min(10, self._nb_level))
        mask = env > med * ratio
        if not mask.any():
            return x
        mask = np.convolve(mask.astype(np.int8), np.ones(5, dtype=np.int8),
                           mode="same") > 0
        if float(np.mean(mask)) > 0.10:
            return x
        out = x.copy()
        out[mask] = 0.0
        return out

    def _wdsp_level(self, audio: np.ndarray) -> np.ndarray:
        """Slow output leveler for the WDSP path.

        WDSP's own AGC does the syllabic work.  This is the reference's
        ~5 s EMA leveler: it corrects band-to-band loudness drift without
        riding syllables.  AGC OFF bypasses it (the operator asked for no
        gain control).  The gain is bounded so a noise-only block cannot
        be amplified into hiss.
        """
        if audio.size == 0 or self._agc_mode == 0:
            return audio
        rms = float(np.sqrt(np.mean(audio ** 2) + 1e-12))
        target = 0.22
        if not self._wdsp_level_ready and rms > 1e-6:
            # Start at the correct level instead of letting a 5 s EMA walk
            # from unity into place (that made the first seconds loud).
            self._wdsp_post_gain = max(0.25, min(12.0, target / rms))
            self._wdsp_level_ready = True
            return audio * self._wdsp_post_gain
        if rms < 0.006:
            ideal, alpha = 1.5, 0.001
        else:
            ideal, alpha = target / max(rms, 1e-6), 0.002
        self._wdsp_post_gain = (self._wdsp_post_gain * (1.0 - alpha)
                                + ideal * alpha)
        self._wdsp_post_gain = max(0.25, min(12.0, self._wdsp_post_gain))
        return audio * self._wdsp_post_gain

    def _apply_agc(self, audio: np.ndarray, mode: str) -> np.ndarray:
        """Per-block scalar AGC (vectorized — no per-sample Python loop).

        Gain is constant within a block (>= 10 ms of audio), so there are
        no per-sample gain steps to pump; attack is faster than decay.
        AM is tracked ~5x slower than SSB at the same AGC setting: its
        level lives in the carrier envelope while the modulation swings
        the RMS, and fast gain would ride the syllables (breathing).
        """
        if audio.size == 0:
            return audio                 # resampler priming is not silence data
        rms = float(np.sqrt(np.mean(audio ** 2) + 1e-12))
        desired = 0.25 / max(rms, 1e-9)
        if mode == "AM":
            attack, decay = 0.01, 0.002
        else:
            attack, decay = {1: (0.02, 0.002), 2: (0.05, 0.005),
                             3: (0.2, 0.02), 4: (0.4, 0.1)}[self._agc_mode]
        if self._agc_gain is None:
            self._agc_gain = min(max(desired, 1e-3), 1e4)
        else:
            alpha = attack if desired < self._agc_gain else decay
            new = self._agc_gain + (desired - self._agc_gain) * alpha
            self._agc_gain = min(max(new, 1e-3), 1e4)
        return audio * self._agc_gain


# ── TX modulator ──────────────────────────────────────────────────

class TXModulator:
    """Streaming 48 kHz int16 mic -> 1200-byte 24-bit IQ payloads.

    Only USB/LSB are offered: AM/FM/CW TX were never verified on the
    hardware and an unverified mode must fail loudly, never modulate
    silently.  Queueing/pacing belongs to the engine task; this class is
    a pure function over streaming state: feed_audio() returns whatever
    payloads became complete.  ``underruns`` is an engine-owned counter
    the next task's pacer bumps; reset() clears it like everything else.
    """

    _MODES = ("USB", "LSB")

    def __init__(self, input_rate: Union[int, float] = AUDIO_RATE,
                 iq_rate: Union[int, float] = TX_IQ_RATE):
        for name, val in (("input_rate", input_rate), ("iq_rate", iq_rate)):
            v = float(val)
            if not math.isfinite(v) or v <= 0.0:
                raise ValueError(f"{name} must be > 0, got {val!r}")
        self.input_rate = input_rate
        self.iq_rate = iq_rate
        self.mode = "USB"
        self.underruns = 0
        self._init_chain()
        self._reset_level_probes()
        # Fully ramped until a PTT assert arms it (reset()).
        self._ramp_pos = TX_RAMP_SAMPLES

    # ── Chain state ──────────────────────────────────────────────

    def _init_chain(self):
        fs = float(self.input_rate)
        # Continuous DC blocker: first-order IIR highpass @ 20 Hz, state
        # carried across feeds (per-frame mean subtraction clicked at
        # frame boundaries in the reference — this converges instead).
        r = math.exp(-2.0 * math.pi * 20.0 / fs)
        self._dc_b = np.array([1.0, -1.0], dtype=np.float64)
        self._dc_a = np.array([1.0, -r], dtype=np.float64)
        self._dc_zi = np.zeros(1, dtype=np.float64)
        # 300 Hz HPF (SSB power efficiency) then 3.6 kHz anti-alias.
        self._hp_sos = butter(4, 300.0, btype="high", fs=fs, output="sos")
        self._hp_zi = np.zeros((self._hp_sos.shape[0], 2), dtype=np.float64)
        self._aa_sos = butter(4, 3600.0, btype="low", fs=fs, output="sos")
        self._aa_zi = np.zeros((self._aa_sos.shape[0], 2), dtype=np.float64)
        self._in_buf = np.zeros(0, dtype=np.float64)
        self._iq_buf = np.zeros(0, dtype=np.complex128)
        self._rs = soxr.ResampleStream(fs, float(self.iq_rate), 2,
                                       dtype="float64", quality="HQ")

    def _reset_level_probes(self):
        self._lvl_in_peak = 0.0
        self._lvl_iq_peak = 0.0
        self._lvl_limited_peak = 0.0
        self._lvl_iq_samples = 0

    # ── Control ──────────────────────────────────────────────────

    def set_mode(self, mode: str):
        m = str(mode).strip().upper()
        if m not in self._MODES:
            raise ValueError(
                f"unsupported TX mode {mode!r}; SunSDR2 DX TX is verified"
                f" for {self._MODES} only — refusing to transmit an"
                f" unverified modulation")
        self.mode = m

    def reset(self):
        """Clear all streaming state (call on PTT assert).

        Also re-arms the leading amplitude ramp: the engine calls this from
        start_tx(), after the silent priming packets, so the first real
        packet fades in instead of stepping.
        """
        self._init_chain()
        self._reset_level_probes()
        self._ramp_pos = 0
        self.underruns = 0

    def snapshot_levels(self) -> dict:
        """Return and clear the per-PTT-cycle level probe accumulators."""
        out = {
            "in_peak": self._lvl_in_peak,
            "iq_peak": self._lvl_iq_peak,
            "limited_peak": self._lvl_limited_peak,
            "iq_samples": self._lvl_iq_samples,
        }
        self._reset_level_probes()
        return out

    # ── Feed ─────────────────────────────────────────────────────

    def feed_audio(self, pcm) -> list:
        """48 kHz int16-le bytes (or float/int array) -> list of 1200-byte
        IQ payloads (200 samples each, packed via Task 1's codec)."""
        if isinstance(pcm, (bytes, bytearray, memoryview)):
            raw = bytes(pcm)
            if len(raw) % 2:
                raise ValueError(
                    f"int16 PCM must be an even number of bytes, got {len(raw)}")
            x = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
        else:
            x = np.asarray(pcm)
            if x.size == 0:
                return []
            if np.issubdtype(x.dtype, np.integer):
                x = x.astype(np.float64) / 32768.0
            else:
                x = x.astype(np.float64)
        if x.size == 0:
            return []
        np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0, copy=False)

        x, self._dc_zi = lfilter(self._dc_b, self._dc_a, x, zi=self._dc_zi)
        x, self._hp_zi = sosfilt(self._hp_sos, x, zi=self._hp_zi)
        x, self._aa_zi = sosfilt(self._aa_sos, x, zi=self._aa_zi)
        self._in_buf = np.concatenate([self._in_buf, x])

        # Hilbert overlap-save: TX_BLOCK samples per hop with
        # TX_HILBERT_MARGIN of context on BOTH sides (window 2*margin+blk,
        # emit [margin, margin+blk)).  scipy's hilbert() is a circular FFT
        # convolution — with a left-margin-only window the wrap lands on
        # the emitted region's RIGHT edge and repeats every hop (measured
        # as a ~100 Hz spectral line on band-limited noise); the right
        # margin moves it 384 samples away from anything we keep.
        margin, blk = TX_HILBERT_MARGIN, TX_BLOCK
        while self._in_buf.size >= 2 * margin + blk:
            block = self._in_buf[:2 * margin + blk]
            self._in_buf = self._in_buf[blk:]
            analytic = hilbert(block)[margin:margin + blk]
            if self.mode == "LSB":
                analytic = np.conj(analytic)
            st = np.stack([analytic.real, analytic.imag], axis=1)
            rs_out = self._rs.resample_chunk(st)
            if rs_out.size:
                self._iq_buf = np.concatenate([
                    self._iq_buf,
                    rs_out[:, 0] + 1j * rs_out[:, 1]])

        pkts = []
        per = IQ_SAMPLES_PER_PACKET
        while self._iq_buf.size >= per:
            iq = self._iq_buf[:per]
            self._iq_buf = self._iq_buf[per:]
            pkts.append(self._emit(iq))
        return pkts

    def _emit(self, iq: np.ndarray) -> bytes:
        """Fixed make-up gain -> tanh magnitude limit -> 24-bit pack."""
        driven = iq * TX_GAIN
        mag = np.abs(driven)
        self._lvl_in_peak = max(self._lvl_in_peak, float(np.max(np.abs(iq))))
        self._lvl_iq_peak = max(self._lvl_iq_peak,
                                float(np.max(mag)) if mag.size else 0.0)
        limited = driven.copy()
        nz = mag > 1e-9
        if np.any(nz):
            limited[nz] = (driven[nz] / mag[nz]
                           * (TX_CEILING * np.tanh(mag[nz] / TX_CEILING)))
        self._lvl_limited_peak = max(
            self._lvl_limited_peak,
            float(np.max(np.abs(limited))) if limited.size else 0.0)
        self._lvl_iq_samples += iq.size
        return pack_iq_24bit(self._apply_ramp(limited.astype(np.complex64)))

    def _apply_ramp(self, iq: np.ndarray) -> np.ndarray:
        """Scale the leading edge of TX IQ by a linear 0->1 gain ramp.

        No-op once TX_RAMP_SAMPLES IQ samples have been emitted.  This is
        the reference server's verified click fix: the pacer sends silent
        packets until the mic queue is primed, then real modulation starts.
        """
        n = len(iq)
        if self._ramp_pos >= TX_RAMP_SAMPLES or n == 0:
            return iq
        idx = self._ramp_pos + np.arange(n)
        gain = np.clip(idx / float(TX_RAMP_SAMPLES), 0.0, 1.0).astype(np.float32)
        self._ramp_pos += n
        return (iq * gain).astype(np.complex64)
