"""SunSDR2 DX — optional WDSP NR2 / ANF contract tests.

These tests need the native libwdsp shared library (the shipped
``backends/sunsdr/vendor/libwdsp.so`` on aarch64, ``libwdsp.dylib`` on
macOS, or ``MRRC_WDSP_LIB``).  They are skipped on a machine without it;
the pure-Python DSP tests remain the always-run baseline.

The NB tests exercise MRRC's software impulse blanker, NOT WDSP's SNB:
snb.c has a use-after-free that aborts the host process when enabled
(measured 2026-11-09 on macOS arm64 and aarch64), so the backend never
calls SetRXASNBARun.
"""
import asyncio
import math
import os
import time
import unittest

import numpy as np

from backends.sunsdr import dsp, wdsp


def _tone_iq(freq_hz: float, fs: int, n: int, amp: float = 0.05):
    """IQ tone at VFO + freq_hz (the demodulator shifts +IF itself)."""
    t = np.arange(n, dtype=np.float64) / fs
    return (amp * np.exp(2j * math.pi * (freq_hz - dsp.IF_OFFSET) * t)
            ).astype(np.complex64)


@unittest.skipUnless(wdsp.available(), "libwdsp not installed")
class WDSPAudioPathTests(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("MRRC_WDSP")
        os.environ["MRRC_WDSP"] = "on"

    def tearDown(self):
        if self._old is None:
            os.environ.pop("MRRC_WDSP", None)
        else:
            os.environ["MRRC_WDSP"] = self._old

    def _run(self, demod, iq, block=3125, sleep=0.01):
        out = []
        for i in range(0, len(iq), block):
            out.append(demod.demodulate(iq[i:i + block]))
            time.sleep(sleep)
        return np.frombuffer(b"".join(out), dtype="<i2").astype(np.float64)

    def test_wdsp_channel_loads_and_reports_true_rate(self):
        d = dsp.AudioDemodulator(78125)
        try:
            self.assertTrue(d.wdsp_available())
            self.assertIsNotNone(d._wdsp_iq)
            # 1024 // 5 = 204 samples per 1024 -> ~15564 Hz, not exactly
            # 15625; downstream resampling must use the true value.
            self.assertAlmostEqual(d._wdsp_iq.out_rate, 15640.26, delta=1.0)
        finally:
            d._close_wdsp()

    def test_usb_tone_survives_wdsp_and_pitch_is_correct(self):
        fs = 78125
        iq = _tone_iq(1000.0, fs, fs * 2, amp=0.05)
        d = dsp.AudioDemodulator(fs)
        try:
            d.set_noise_reduction(False)
            audio = self._run(d, iq)
            self.assertGreater(len(audio), 80000)
            self.assertGreater(float(np.sqrt(np.mean(audio ** 2))), 1000.0)
            # Skip the resampler/WDSP warm-up; the steady-state pitch is
            # what must be exact (the wrapper's true rate, not 15625).
            seg = audio[24000:72000]
            spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
            peak = int(np.argmax(spec)) * 48000 / len(seg)
            self.assertLess(abs(peak - 1000.0), 25.0)
        finally:
            d._close_wdsp()

    def test_nr_and_anf_toggle_without_silence_or_crash(self):
        fs = 78125
        iq = _tone_iq(1200.0, fs, fs // 2, amp=0.05)
        d = dsp.AudioDemodulator(fs)
        try:
            self.assertTrue(d.set_noise_reduction(True))
            self.assertTrue(d.set_auto_notch(True))
            audio = self._run(d, iq)
            self.assertGreater(float(np.sqrt(np.mean(audio ** 2))), 500.0)
            self.assertTrue(d.set_noise_reduction(False))
            self.assertTrue(d.set_auto_notch(False))
            audio = self._run(d, iq)
            self.assertGreater(float(np.sqrt(np.mean(audio ** 2))), 500.0)
        finally:
            d._close_wdsp()

    def test_engine_sized_blocks_do_not_pad_silence(self):
        """3125-IQ blocks must not make the 1024-IQ WDSP ring underflow.

        Field report 2026-11-09: handing the IQ processor a whole 3125-sample
        engine block made `want = n * ratio` consume more audio than the
        ring produced, so every output block was half zero-padded — audible
        as a buzz/distortion.  The demodulator now feeds exact 1024-IQ
        slices and carries the remainder.
        """
        fs = 78125
        iq = _tone_iq(1000.0, fs, fs * 2, amp=0.05)
        d = dsp.AudioDemodulator(fs)
        try:
            d.set_noise_reduction(False)
            audio = self._run(d, iq)
            steady = audio[24000:72000].astype(np.int16)
            self.assertGreater(len(steady), 40000)
            silent = np.abs(steady.astype(np.int32)) < 2
            self.assertLess(float(np.mean(silent)), 0.02)
            # Longest contiguous silent run must stay well below a 20 ms
            # dropped block (960 samples at 48 kHz).
            runs = np.diff(np.flatnonzero(
                np.concatenate(([1], silent.astype(np.int8), [1])) != 1))
            self.assertLess(int(runs.max(initial=0)), 960)
        finally:
            d._close_wdsp()

    def test_nr_level_maps_ui_1_to_15_and_reads_back(self):
        d = dsp.AudioDemodulator(78125)
        try:
            self.assertTrue(d.set_nr_level(15))
            self.assertEqual(d.get_nr_level(), 15)
            self.assertEqual(d._wdsp_nr_level(), 100)
            self.assertTrue(d.set_nr_level(1))
            self.assertEqual(d._wdsp_nr_level(), 0)
        finally:
            d._close_wdsp()

    def test_software_nb_is_independent_of_wdsp(self):
        os.environ["MRRC_WDSP"] = "off"
        d = dsp.AudioDemodulator(78125)
        try:
            self.assertFalse(d.wdsp_available())
            self.assertTrue(d.set_noise_blanker(True))
            self.assertTrue(d.set_nb_level(7))
            self.assertEqual(d.get_nb_level(), 7)
            clean = np.full(400, 0.01 + 0.0j, dtype=np.complex128)
            pulse = clean.copy()
            pulse[200] = 1.0
            blanked = d._apply_noise_blanker(pulse)
            self.assertLess(abs(blanked[200]), 1e-9)
            self.assertAlmostEqual(abs(blanked[100]), 0.01)
        finally:
            d._close_wdsp()


@unittest.skipUnless(wdsp.available(), "libwdsp not installed")
class WDSPBackendContractTests(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("MRRC_WDSP")
        os.environ["MRRC_WDSP"] = "on"

    def tearDown(self):
        if self._old is None:
            os.environ.pop("MRRC_WDSP", None)
        else:
            os.environ["MRRC_WDSP"] = self._old

    def test_capabilities_and_setters(self):
        from backends.sunsdr.backend import SunSDR2DXBackend
        cat = SunSDR2DXBackend(host="192.0.2.1")
        try:
            caps = cat.capabilities
            self.assertTrue(caps.has_noise_reduction)
            self.assertTrue(caps.has_auto_notch)
            self.assertTrue(caps.has_noise_blanker)
            self.assertTrue(caps.has_nr_level)
            self.assertTrue(caps.has_nb_level)
            self.assertFalse(caps.has_compressor)
            self.assertFalse(caps.has_squelch)
            self.assertFalse(caps.has_rf_gain)
            self.assertTrue(asyncio.run(cat._engine.set_noise_reduction(True)))
            self.assertTrue(asyncio.run(cat._engine.set_auto_notch(True)))
            self.assertTrue(asyncio.run(cat._engine.set_nr_level(12)))
            self.assertEqual(
                asyncio.run(cat._engine.get_nr_level()), 12)
            self.assertTrue(asyncio.run(cat._engine.set_nb_level(9)))
            self.assertEqual(
                asyncio.run(cat._engine.get_nb_level()), 9)
        finally:
            cat._engine.demodulator._close_wdsp()


if __name__ == "__main__":
    unittest.main()
