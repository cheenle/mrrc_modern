"""Tests for cq_player.py — CQ asset loading and the call state machine.

Spec: docs/superpowers/specs/2026-09-13-cq-key-design.md (§5 asset, §8 tests).
No hardware: the player takes key/unkey/feed callbacks, so the whole call is
exercised against fakes.
"""
import asyncio
import tempfile
import time
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
    """Minimal device stand-in: counts fed chunks.

    ``drain_s=0`` (the default) never backs up — the pump sees an always-empty
    queue and may feed in one burst, which is all the correctness tests need.
    A non-zero value models a real codec: it draws one frame every ``drain_s``
    seconds, so the pump's rate is measured against a device clock instead of
    the wall clock.
    """

    def __init__(self, drain_s: float = 0.0):
        self.frames = []
        self.drain_s = drain_s
        self.started = None

    def feed_tx_audio(self, pcm):
        if self.started is None:
            self.started = time.monotonic()
        self.frames.append(pcm)

    def tx_queue_frames(self) -> int:
        if not self.drain_s or self.started is None:
            return 0
        drawn = int((time.monotonic() - self.started) / self.drain_s)
        return max(0, len(self.frames) - drawn)


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

    async def test_abort_during_the_final_drain_still_releases_the_carrier(self):
        """播完那一瞬间收到 abort，载波也必须落下。

        `_finish()` 先把 state 写成 "complete"，再去 await 优雅排空（真实链路里是
        `audio.stop_tx(graceful)`，最多 TX_DRAIN_MS + 线程调度）。abort 若正好落进
        这个 await，会看到 state 已是 complete 而**跳过自己那一步释放**，于是电台
        保持按下、而没有任何人还会发 TX0 —— 断线、重复点 CQ 都能落进去。
        """
        with tempfile.TemporaryDirectory() as tmp:
            calls = {"unkey": []}
            state = {"tx": True}
            draining = asyncio.Event()
            finish_drain = asyncio.Event()

            async def key(token):
                return True

            async def unkey(graceful):
                calls["unkey"].append(("start", graceful))
                if len(calls["unkey"]) == 1:
                    # Only the graceful finish blocks: it stands in for the
                    # drain that abort is about to cancel us inside of.
                    draining.set()
                    await finish_drain.wait()
                state["tx"] = False
                calls["unkey"].append(("done", graceful))

            audio = _FakeAudio()
            p = make_wav(Path(tmp) / "cq.wav", 0.1)                     # 5 frames
            player = CQPlayer(asset_path=p, audio=audio, key=key, unkey=unkey,
                              is_transmitting=lambda: state["tx"])
            player.load()
            await player.start(started_by="c1", owner_token="t1")
            await asyncio.wait_for(draining.wait(), timeout=3.0)        # inside the drain
            self.assertEqual(player.status()["state"], "complete")
            aborting = asyncio.create_task(player.abort("client_gone"))  # ← the race
            await asyncio.sleep(0.05)                 # let the cancel land inside the drain
            finish_drain.set()
            await asyncio.wait_for(aborting, timeout=2.0)
            self.assertFalse(state["tx"], "carrier must be released")
            self.assertIn(("done", False), calls["unkey"])

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


class CQCadenceTests(unittest.IsolatedAsyncioTestCase):
    """The pump must run at the sound card's rate, not at asyncio's timer rate.

    2026-10-29 field report ("发射 CQ 的时候声音有些失真"): the feed loop slept
    a flat ``asyncio.sleep(0.02)`` per frame, which only guarantees "at least
    20 ms" — the loop body's own ~0.9 ms was never repaid, so the pump fed one
    20 ms frame every ~20.87 ms while the 44.1 kHz codec consumed one every
    20.000 ms.  With ``CQ_LOW_WATER_FRAMES`` = 4 (80 ms) of cushion the device
    ran dry about two seconds into every call, inserting ~1 ms of silence into
    every 21 ms period.  The box's log closed the arithmetic: a 10.48 s asset
    (524 frames) keyed the radio for 11.00 s, and 524 × 0.87 ms = 0.46 s.
    """

    def _player(self, tmp, seconds, audio=None):
        p = make_wav(Path(tmp) / "cq.wav", seconds)
        player = CQPlayer(asset_path=p, audio=audio or _FakeAudio(),
                          key=lambda token: _true(), unkey=lambda g: _noop(),
                          is_transmitting=lambda: True)
        self.assertTrue(player.load())
        return player

    async def test_feed_cadence_tracks_the_wall_clock(self):
        frames = 50                                           # 1.000 s of audio
        with tempfile.TemporaryDirectory() as tmp:
            device = _FakeAudio(drain_s=0.02)                 # a 20 ms codec
            player = self._player(tmp, frames * 0.02, audio=device)
            self.assertEqual(player.status()["frames_total"], frames)
            started = asyncio.get_running_loop().time()
            await player.start(started_by="c1", owner_token="t1")
            await asyncio.wait_for(player.wait_finished(), timeout=5.0)
            elapsed = asyncio.get_running_loop().time() - started
        audio_seconds = frames * 0.02
        # A counter-based pump measures ~1.050 s here (21.0 ms/frame); the
        # clocked pump measures ~1.000 s.  2.5 % is the discriminator, and it
        # is loose enough that a normally loaded machine never trips it.
        # 4.5 %: the counter-based pump measured 21.65 ms/frame (and would be
        # far worse on Windows, where `asyncio.sleep(0.02)` wakes ~31 ms late),
        # while the queue-paced pump is device-limited at 20.00 ms on every
        # platform.  Tighter than 4.5 % would fail on a loaded machine for
        # reasons that are not this defect.
        self.assertLess(elapsed, audio_seconds * 1.045,
                        f"pump ran {elapsed / frames * 1000:.2f} ms/frame — "
                        f"the device only gets {audio_seconds / frames * 1000:.0f} ms")
        self.assertGreater(elapsed, audio_seconds * 0.975,
                           "pump ran faster than real time")


async def _true():
    return True


async def _noop(*_args):
    return None
