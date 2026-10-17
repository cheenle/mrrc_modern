"""Local speaker tap (AD-028): the radio's audio on a device beside the box.

The parts that matter are the ones that decide whether the RX loop keeps
running: the tap never blocks, a slow device drops instead of stalling, and
the loop's idle gate counts the speaker as a listener (the FT8 tap already
taught that lesson once).
"""

import io
import time
import unittest
from pathlib import Path

import local_speaker as ls


class FakeProc:
    """Stands in for the aplay child: records what would have been played."""

    def __init__(self, fail_on_write=False):
        self.stdin = io.BytesIO()
        self.fail_on_write = fail_on_write
        self.written = 0
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminated = True

    class _Stdin:
        def __init__(self, outer):
            self.outer = outer

        def write(self, data):
            if self.outer.fail_on_write:
                raise BrokenPipeError("device gone")
            self.outer.written += len(data)
            return len(data)

        def flush(self):
            pass

        def close(self):
            pass


def _fake_proc(fail=False):
    proc = FakeProc(fail)
    proc.stdin = FakeProc._Stdin(proc)
    return proc


class TapTests(unittest.TestCase):
    def test_no_device_is_a_noop(self):
        sp = ls.LocalSpeaker("")
        self.assertFalse(sp.start())
        sp.feed(b"\x00\x01" * 960)          # must not raise
        self.assertFalse(sp.active)

    def test_feed_before_start_does_nothing(self):
        sp = ls.LocalSpeaker("bluealsa:test")
        sp.feed(b"\x00\x01" * 960)
        self.assertEqual(sp.status()["samples_in"], 0)

    def test_decimation_lands_in_the_device_band(self):
        proc = _fake_proc()
        sp = ls.LocalSpeaker("bluealsa:test", spawn=lambda: proc)
        self.assertTrue(sp.start())
        self.addCleanup(sp.stop)
        chunk = b"\x00\x00" * 4800          # 100 ms at 48 kHz
        for _ in range(10):                 # 1 s of audio
            sp.feed(chunk)
        deadline = time.time() + 2.0
        while time.time() < deadline and sp.status()["bytes_out"] < 8000:
            time.sleep(0.02)
        out = sp.status()["bytes_out"]
        # 8 kHz mono int16 → 16 000 bytes per second, minus the FIR delay
        self.assertGreater(out, 12_000)
        self.assertLessEqual(out, 16_100)

    def test_a_full_queue_drops_instead_of_blocking(self):
        """A device that plays slower than the radio delivers must cost audio,
        never the RX loop: feed() returns immediately and the drop is counted."""
        class SlowStdin:
            def __init__(self):
                self.written = 0

            def write(self, data):
                time.sleep(0.5)
                self.written += len(data)
                return len(data)

            def flush(self):
                pass

            def close(self):
                pass

        class SlowProc:
            def __init__(self):
                self.stdin = SlowStdin()
                self.terminated = False

            def poll(self):
                return 0 if self.terminated else None

            def terminate(self):
                self.terminated = True

            def wait(self, timeout=None):
                return 0

            def kill(self):
                self.terminated = True

        sp = ls.LocalSpeaker("bluealsa:test", queue_frames=1, spawn=SlowProc)
        sp.start()
        self.addCleanup(sp.stop)
        started = time.time()
        for _ in range(40):
            sp.feed(b"\x00\x00" * 4800)
        self.assertLess(time.time() - started, 2.0, "feed() must never block on the device")
        self.assertGreaterEqual(sp.status()["dropped"], 1)

    def test_write_failure_is_counted_and_retried(self):
        procs = [_fake_proc(fail=True), _fake_proc()]
        calls = []

        def spawn():
            calls.append(1)
            return procs[min(len(calls), len(procs)) - 1]

        sp = ls.LocalSpeaker("bluealsa:test", spawn=spawn)
        sp.start()
        self.addCleanup(sp.stop)
        for _ in range(5):
            sp.feed(b"\x00\x00" * 4800)
            time.sleep(0.05)
        self.assertGreaterEqual(sp.status()["failures"], 1)
        self.assertGreaterEqual(len(calls), 2, "a dead device must be respawned")

    def test_a2dp_mode_passes_the_codec_domain_and_duplicates_to_stereo(self):
        # A2DP is 44.1/48 kHz stereo while the radio's RX is 48 kHz mono: no
        # decimation, and each sample must reach both channels.
        proc = _fake_proc()
        sp = ls.LocalSpeaker("bluealsa:test", rate=48000, channels=2, spawn=lambda: proc)
        sp.start()
        self.addCleanup(sp.stop)
        for _ in range(5):
            sp.feed(b"\x01\x00" * 4800)            # 100 ms chunks
        deadline = time.time() + 2.0
        while time.time() < deadline and sp.status()["bytes_out"] < 48000:
            time.sleep(0.02)
        out = sp.status()["bytes_out"]
        self.assertGreater(out, 40_000)
        self.assertLessEqual(out, 96_100)          # 48 kHz stereo S16 = 192 kB/s

    def test_status_keys_are_stable(self):
        keys = set(ls.LocalSpeaker("x").status())
        self.assertEqual(keys, {
            "device", "rate", "active", "samples_in", "bytes_out",
            "queued", "dropped", "opens", "failures", "last_error"})


class ServerWiringTests(unittest.TestCase):
    """The loop-side rules, checked as source because they are the ones that
    decide whether audio flows at all."""

    def setUp(self):
        self.src = Path(__file__).resolve().parent.parent.joinpath("server.py").read_text(encoding="utf-8")

    def test_the_rx_loop_counts_the_speaker_as_a_listener(self):
        self.assertIn("_speaker_live = _local_speaker is not None and _local_speaker.active", self.src)
        self.assertIn("and not _ft8_tap_live and not _speaker_live", self.src)

    def test_the_rx_loop_feeds_the_speaker(self):
        self.assertIn("_local_speaker.feed(pcm)", self.src)

    def test_the_speaker_is_built_from_the_environment(self):
        self.assertIn('MRRC_LOCAL_SPEAKER_DEVICE', self.src)
        self.assertIn("_local_speaker.stop()", self.src)


if __name__ == "__main__":
    unittest.main()
