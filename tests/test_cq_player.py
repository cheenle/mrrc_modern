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


class _FakeAudio:
    """Minimal device stand-in: counts fed chunks, never backs up."""

    def __init__(self):
        self.frames = []

    def feed_tx_audio(self, pcm):
        self.frames.append(pcm)

    def tx_queue_frames(self):
        return 0


class CQPlaybackTests(unittest.IsolatedAsyncioTestCase):
    def _player(self, tmp, seconds=0.1, transmitting=True):
        calls = {"key": 0, "unkey": [], "broadcasts": []}
        state = {"tx": transmitting}

        async def key(token):
            calls["key"] += 1
            state["tx"] = True
            return True

        async def unkey(graceful):
            calls["unkey"].append(graceful)
            state["tx"] = False

        async def on_change(snap):
            calls["broadcasts"].append(snap["state"])

        audio = _FakeAudio()
        p = make_wav(Path(tmp) / "cq.wav", seconds)
        player = CQPlayer(asset_path=p, audio=audio, key=key, unkey=unkey,
                          is_transmitting=lambda: state["tx"], on_change=on_change)
        self.assertTrue(player.load())
        return player, audio, calls, state

    async def test_one_shot_call_keys_plays_and_unkeys(self):
        with tempfile.TemporaryDirectory() as tmp:
            player, audio, calls, state = self._player(tmp, seconds=0.1)   # 5 frames
            await player.start(started_by="c1", owner_token="t1")
            self.assertTrue(player.is_calling)
            await asyncio.wait_for(player.wait_finished(), timeout=3.0)
            self.assertEqual(calls["key"], 1)
            self.assertEqual(len(audio.frames), 5)
            self.assertEqual(calls["unkey"], [True])           # graceful drain
            self.assertFalse(state["tx"])
            self.assertEqual(player.status()["state"], "complete")
            self.assertEqual(player.status()["frames_sent"], 5)
            self.assertIn("calling", calls["broadcasts"])
            self.assertIn("complete", calls["broadcasts"])

    async def test_start_is_refused_while_calling(self):
        with tempfile.TemporaryDirectory() as tmp:
            player, _a, _c, _s = self._player(tmp, seconds=5.0)
            await player.start(started_by="c1", owner_token="t1")
            with self.assertRaises(CQUnavailable):
                await player.start(started_by="c1", owner_token="t1")
            await player.abort("aborted_by_user")

    async def test_start_is_refused_without_an_asset(self):
        player = CQPlayer(asset_path=None)
        with self.assertRaises(CQUnavailable):
            await player.start(started_by="c1", owner_token=None)

    async def test_abort_stops_immediately_without_graceful_drain(self):
        with tempfile.TemporaryDirectory() as tmp:
            player, audio, calls, state = self._player(tmp, seconds=20.0)
            await player.start(started_by="c1", owner_token="t1")
            await asyncio.sleep(0.1)                           # a few frames go out
            await player.abort("aborted_by_user")
            fed = len(audio.frames)
            await asyncio.sleep(0.1)
            self.assertEqual(player.status()["state"], "aborted")
            self.assertEqual(player.status()["reason"], "aborted_by_user")
            self.assertEqual(calls["unkey"], [False])
            self.assertFalse(state["tx"])
            self.assertEqual(len(audio.frames), fed)           # nothing after abort

    async def test_external_unkey_aborts_the_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            player, _a, _c, state = self._player(tmp, seconds=20.0)
            await player.start(started_by="c1", owner_token="t1")
            await asyncio.sleep(0.1)
            state["tx"] = False                                # watchdog / TUNE / other client
            await asyncio.wait_for(player.wait_finished(), timeout=3.0)
            self.assertEqual(player.status()["state"], "aborted")
            self.assertEqual(player.status()["reason"], "unkeyed")

    async def test_backpressure_waits_for_the_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = {"unkey": []}
            state = {"tx": True}
            audio = _FakeAudio()
            audio.tx_queue_frames = lambda: 99                 # device is far behind
            feeding = {"waited": False}

            async def key(token):
                return True

            async def unkey(graceful):
                calls["unkey"].append(graceful)
                state["tx"] = False

            p = make_wav(Path(tmp) / "cq.wav", 0.1)
            player = CQPlayer(asset_path=p, audio=audio, key=key, unkey=unkey,
                              is_transmitting=lambda: state["tx"])
            player.load()
            await player.start(started_by="c1", owner_token=None)
            await asyncio.sleep(0.15)
            self.assertEqual(audio.frames, [])                 # nothing fed yet
            self.assertTrue(player.is_calling)
            audio.tx_queue_frames = lambda: 0
            await asyncio.wait_for(player.wait_finished(), timeout=3.0)
            self.assertEqual(len(audio.frames), 5)             # then all of it
