"""Tests for the server-side recorder (spec 2026-09-12 §4/§5).

Everything here is hardware-free: PCM blocks are synthesised, sessions
write into a temporary directory, and the MP3 is inspected as a byte
stream (no decoder needed).

This file grows with the plan's tasks: each task adds the tests for the
symbols it implements, so a red test always means "this behaviour is
missing", never "this import does not exist yet".
"""
import math
import struct
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

import numpy as np

from recorder import RECORDING_RATE, _StreamingDecimator


def sine_pcm(freq_hz: float, seconds: float, rate: int, amp: int = 12000) -> bytes:
    n = int(rate * seconds)
    return np.array(
        [int(amp * math.sin(2 * math.pi * freq_hz * i / rate)) for i in range(n)],
        dtype='<i2',
    ).tobytes()


def rms_int16(pcm: bytes) -> float:
    samples = np.frombuffer(pcm, dtype='<i2').astype(np.float64)
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples ** 2)))


class StreamingDecimatorTests(unittest.TestCase):
    """Ported expectations from mrrc's recording-session tests."""

    def test_passband_voice_survives(self):
        # A 1 kHz tone must come through nearly unchanged (level-wise).
        dec = _StreamingDecimator(48000, RECORDING_RATE)
        pcm = sine_pcm(1000, 0.5, 48000)
        out = dec.process(np.frombuffer(pcm, dtype='<i2'))
        self.assertEqual(out.dtype, np.dtype('<i2'))
        # 48k -> 16k is 1:3, so a third of the samples come out.
        self.assertAlmostEqual(out.size, len(pcm) // 2 // 3, delta=2)
        self.assertGreater(rms_int16(out.tobytes()), 0.5 * rms_int16(pcm))

    def test_stopband_tone_is_rejected(self):
        # 15 kHz is above the 5.5 kHz recording band -> must be attenuated.
        dec = _StreamingDecimator(48000, RECORDING_RATE)
        pcm = sine_pcm(15000, 0.5, 48000)
        out = dec.process(np.frombuffer(pcm, dtype='<i2'))
        self.assertLess(rms_int16(out.tobytes()), 0.1 * rms_int16(pcm))

    def test_state_is_continuous_across_blocks(self):
        # Feeding one long block and feeding it in 20 ms pieces must agree.
        pcm = sine_pcm(800, 0.4, 48000)
        whole = _StreamingDecimator(48000, RECORDING_RATE).process(
            np.frombuffer(pcm, dtype='<i2'))
        piecewise = _StreamingDecimator(48000, RECORDING_RATE)
        samples = np.frombuffer(pcm, dtype='<i2')
        chunks = [piecewise.process(samples[i:i + 960])
                  for i in range(0, len(samples), 960)]
        joined = np.concatenate(chunks)
        self.assertEqual(joined.size, whole.size)
        # Filter state continuity: no discontinuity at block boundaries.
        self.assertLess(int(np.abs(joined.astype(np.int32)
                                   - whole.astype(np.int32)).max()), 64)

    def test_rejects_non_integer_ratio(self):
        with self.assertRaises(ValueError):
            _StreamingDecimator(44100, RECORDING_RATE)

    def test_empty_block_is_harmless(self):
        dec = _StreamingDecimator(48000, RECORDING_RATE)
        out = dec.process(np.zeros(0, dtype='<i2'))
        self.assertEqual(out.size, 0)


if __name__ == "__main__":
    unittest.main()
