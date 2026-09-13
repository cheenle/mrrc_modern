"""Tests for cq_player.py — CQ asset loading and the call state machine.

Spec: docs/superpowers/specs/2026-09-13-cq-key-design.md (§5 asset, §8 tests).
No hardware: the player takes key/unkey/feed callbacks, so the whole call is
exercised against fakes.
"""
import asyncio
import tempfile
import unittest
import wave
from pathlib import Path

from cq_player import CQPlayer, CQUnavailable


def make_wav(path: Path, seconds: float, rate: int = 48_000, channels: int = 1,
             value: int = 1000, width: int = 2) -> Path:
    """Write a tiny WAV with a constant tone-less value (fast, no numpy)."""
    frames = int(seconds * rate)
    payload = b"".join(value.to_bytes(width, "little", signed=True) * channels
                       for _ in range(max(frames, 0)))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(payload)
    return path


class CQAssetTests(unittest.TestCase):
    def test_mono_48k_is_loaded_frame_aligned(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_wav(Path(tmp) / "cq.wav", 0.1)               # 100 ms
            player = CQPlayer(asset_path=p)
            self.assertTrue(player.load())
            self.assertEqual(player.status()["duration_s"], 0.1)
            self.assertEqual(player.status()["frames_total"], 5)   # 5 × 20 ms

    def test_stereo_is_mixed_down(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_wav(Path(tmp) / "cq.wav", 0.02, channels=2)
            player = CQPlayer(asset_path=p)
            self.assertTrue(player.load())
            self.assertEqual(player.status()["frames_total"], 1)

    def test_44100_is_resampled_to_48k(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_wav(Path(tmp) / "cq.wav", 0.1, rate=44_100)
            player = CQPlayer(asset_path=p)
            self.assertTrue(player.load())
            self.assertAlmostEqual(player.status()["duration_s"], 0.1, places=2)

    def test_partial_tail_is_padded_to_a_full_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_wav(Path(tmp) / "cq.wav", 0.035)              # 35 ms → 2 frames
            player = CQPlayer(asset_path=p)
            self.assertTrue(player.load())
            self.assertEqual(player.status()["frames_total"], 2)

    def test_missing_asset_reports_an_actionable_reason(self):
        player = CQPlayer(asset_path=Path("/nonexistent/cq.wav"))
        self.assertFalse(player.load())
        self.assertIn("not found", player.unavailable_reason.lower())

    def test_unsupported_sample_width_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_wav(Path(tmp) / "cq.wav", 0.02, value=1, width=1)   # 8-bit
            player = CQPlayer(asset_path=p)
            self.assertFalse(player.load())
            self.assertIn("16-bit", player.unavailable_reason)

    def test_too_long_asset_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_wav(Path(tmp) / "cq.wav", 1.0)
            player = CQPlayer(asset_path=p, max_seconds=0.5)
            self.assertFalse(player.load())
            self.assertIn("too long", player.unavailable_reason)

    def test_truncated_file_is_rejected_without_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "cq.wav"
            p.write_bytes(b"RIFF\x00\x00\x00\x00WAVEjunk")
            player = CQPlayer(asset_path=p)
            self.assertFalse(player.load())
            self.assertTrue(player.unavailable_reason)

    def test_asset_without_a_path_is_unavailable(self):
        player = CQPlayer(asset_path=None)
        self.assertFalse(player.load())
        self.assertIn("not found", player.unavailable_reason.lower())
