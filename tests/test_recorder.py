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

from recorder import (
    RECORDING_RATE,
    _StreamingDecimator,
    list_recordings,
    load_index,
    parse_recording_name,
    recording_name,
    save_index,
)


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


class RecordingNameTests(unittest.TestCase):
    def test_generates_mrrc_compatible_name(self):
        name = recording_name(14_270_000, datetime(2026, 9, 12, 21, 4, 5))
        self.assertEqual(name, "14270kHz_20260912_210405.mp3")

    def test_zero_frequency_matches_mrrc(self):
        # CAT offline: mrrc writes 00000kHz; the panel shows "—".
        self.assertEqual(
            recording_name(0, datetime(2026, 9, 12, 21, 4, 5)),
            "00000kHz_20260912_210405.mp3")

    def test_parses_its_own_names(self):
        parsed = parse_recording_name("07050kHz_20260912_210405.mp3")
        self.assertEqual(parsed["freq_hz"], 7_050_000)
        self.assertEqual(parsed["date"], "20260912")
        self.assertEqual(parsed["time"], "210405")

    def test_rejects_other_names(self):
        for bad in ("x.mp3", "14270kHz_20260912_210405.wav",
                    "../14270kHz_20260912_210405.mp3",
                    "14270kHz_20260912_210405.mp3.mp3",
                    "/etc/passwd", "", "1427kHz_20260912_210405.mp3"):
            with self.subTest(name=bad):
                self.assertIsNone(parse_recording_name(bad))

    def test_rejects_non_string_input(self):
        self.assertIsNone(parse_recording_name(None))


class IndexTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.index = self.dir / "recordings.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_round_trip_and_prune(self):
        (self.dir / "07050kHz_20260912_210405.mp3").write_bytes(b"x" * 100)
        save_index(self.index, {"07050kHz_20260912_210405.mp3": {
            "freq_hz": 7_050_000, "started_at": "2026-09-12T21:04:05",
            "duration": 3.0, "bytes": 100}})
        loaded = load_index(self.index)
        self.assertIn("07050kHz_20260912_210405.mp3", loaded)

        # An entry whose file vanished is dropped from the listing.
        (self.dir / "07050kHz_20260912_210405.mp3").unlink()
        rows = list_recordings(self.dir, loaded, bitrate=64)
        self.assertEqual(rows, [])

    def test_corrupt_index_is_an_empty_index(self):
        self.index.write_text("{not json")
        self.assertEqual(load_index(self.index), {})

    def test_listing_sorts_newest_first_and_sums_bytes(self):
        older = "07050kHz_20260912_200000.mp3"
        newer = "14270kHz_20260912_210000.mp3"
        (self.dir / older).write_bytes(b"x" * 200)
        (self.dir / newer).write_bytes(b"x" * 400)
        index = {
            older: {"freq_hz": 7_050_000, "started_at": "2026-09-12T20:00:00",
                    "duration": 1.0, "bytes": 200},
            newer: {"freq_hz": 14_270_000, "started_at": "2026-09-12T21:00:00",
                    "duration": 2.0, "bytes": 400},
        }
        rows = list_recordings(self.dir, index, bitrate=64)
        self.assertEqual([r["name"] for r in rows], [newer, older])
        self.assertEqual(rows[0]["freq_hz"], 14_270_000)
        self.assertEqual(rows[0]["duration"], 2.0)

    def test_duration_falls_back_to_size_for_unknown_files(self):
        # A file the operator dropped in themselves: no index entry, so
        # duration comes from the size and the configured CBR bitrate.
        name = "07050kHz_20260912_210405.mp3"
        (self.dir / name).write_bytes(b"x" * 8000)   # 8000*8/64000 = 1.0 s
        rows = list_recordings(self.dir, {}, bitrate=64)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["duration"], 1.0, places=3)
        self.assertEqual(rows[0]["freq_hz"], 7_050_000)
        # Timestamp comes from the file name when the index has no entry.
        self.assertEqual(rows[0]["started_at"], "2026-09-12T21:04:05")

    def test_listing_ignores_foreign_files(self):
        (self.dir / "notes.txt").write_text("x")
        (self.dir / "random.mp3").write_bytes(b"x")
        self.assertEqual(list_recordings(self.dir, {}, bitrate=64), [])

    def test_missing_directory_is_empty_not_an_error(self):
        self.assertEqual(list_recordings(self.dir / "nope", {}, 64), [])


if __name__ == "__main__":
    unittest.main()
