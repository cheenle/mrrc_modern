"""Server-side wiring for the recorder (spec 2026-09-12 §4/§6).

No hardware and no HTTP client: the taps are ordinary functions, the
writer is an asyncio task driven with a real session in a temp directory,
and the ASGI routes are exercised through Starlette's FileResponse.
"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest import mock

import numpy as np

import server
from recorder import RecordingSession


def _block(samples: int = 960) -> bytes:
    """One 20 ms block of 48 kHz Int16 PCM."""
    return np.zeros(samples, dtype='<i2').tobytes()


class RecordingWriterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    async def test_writer_encodes_blocks_then_stops_on_the_sentinel(self):
        session = RecordingSession(self.dir, bitrate=64, max_seconds=60)
        session.start(freq_hz=14_270_000)
        queue: asyncio.Queue = asyncio.Queue()
        task = asyncio.create_task(server._recording_writer_loop(queue, session))
        for _ in range(5):
            queue.put_nowait(("rx", _block(), 48000, None))
        queue.put_nowait(("stop", None, 0, None))
        await asyncio.wait_for(task, timeout=2.0)
        self.assertFalse(session.active)                 # the writer stopped it
        files = list(self.dir.glob("*.mp3"))
        self.assertEqual(len(files), 1)
        self.assertGreater(files[0].stat().st_size, 0)

    async def test_writer_keeps_going_after_a_failing_block(self):
        class _Boom:
            active = True

            def __init__(self):
                self.calls = 0

            def add_audio(self, *args, **kwargs):
                self.calls += 1
                raise RuntimeError("encoder exploded")

        boom = _Boom()
        queue: asyncio.Queue = asyncio.Queue()
        task = asyncio.create_task(
            server._recording_writer_loop(queue, cast(Any, boom)))
        for _ in range(3):
            queue.put_nowait(("rx", _block(), 48000, None))
        queue.put_nowait(("stop", None, 0, None))
        await asyncio.wait_for(task, timeout=2.0)        # must not propagate
        self.assertEqual(boom.calls, 3)

    async def test_stop_sentinel_finishes_the_session(self):
        session = RecordingSession(self.dir, bitrate=64, max_seconds=60)
        session.start(freq_hz=7_050_000)
        queue: asyncio.Queue = asyncio.Queue()
        task = asyncio.create_task(server._recording_writer_loop(queue, session))
        queue.put_nowait(("rx", _block(), 48000, None))
        queue.put_nowait(("stop", None, 0, None))
        await asyncio.wait_for(task, timeout=2.0)
        # stop() wrote the Xing header, so the file is bigger than the frames.
        self.assertGreater(next(self.dir.glob("*.mp3")).stat().st_size, 0)


class RecordingQueueTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.session = RecordingSession(Path(self._tmp.name), bitrate=64,
                                        max_seconds=60)
        self._saved = server._rec_session
        self._saved_dropped = server._rec_dropped
        server._rec_session = self.session
        # Start from an empty queue regardless of other tests.
        while not server._rec_queue.empty():
            server._rec_queue.get_nowait()

    def tearDown(self):
        while not server._rec_queue.empty():
            server._rec_queue.get_nowait()
        self.session.close_without_finishing()
        server._rec_session = self._saved
        server._rec_dropped = self._saved_dropped
        self._tmp.cleanup()

    def test_blocks_are_queued_with_their_source_and_rate(self):
        self.session.start()
        server._rec_enqueue("rx", b"\x00\x00" * 320, 48000, 12345)
        item = server._rec_queue.get_nowait()
        self.assertEqual(item, ("rx", b"\x00\x00" * 320, 48000, 12345))

    def test_nothing_is_queued_without_an_active_session(self):
        server._rec_enqueue("rx", b"\x00\x00" * 320, 48000)
        self.assertTrue(server._rec_queue.empty())

    def test_queue_is_bounded_and_counts_drops(self):
        self.session.start()
        before = server._rec_dropped
        for _ in range(server.REC_QUEUE_MAX + 25):
            server._rec_enqueue("rx", _block(), 48000)
        self.assertGreater(server._rec_dropped, before)
        self.assertLessEqual(server._rec_queue.qsize(), server.REC_QUEUE_MAX)


class RecorderTapTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.session = RecordingSession(Path(self._tmp.name), bitrate=64,
                                        max_seconds=60)
        self._saved = server._rec_session
        server._rec_session = self.session
        while not server._rec_queue.empty():
            server._rec_queue.get_nowait()
        self.session.start(freq_hz=14_270_000)

    def tearDown(self):
        while not server._rec_queue.empty():
            server._rec_queue.get_nowait()
        self.session.close_without_finishing()
        server._rec_session = self._saved
        self._tmp.cleanup()

    def test_rx_tap_queues_with_the_device_rate(self):
        server._rec_tap_rx(_block(882), 44100)
        source, pcm, rate, _ts = server._rec_queue.get_nowait()
        self.assertEqual((source, rate), ("rx", 44100))
        self.assertEqual(len(pcm), 882 * 2)

    def test_rx_tap_skips_while_transmitting(self):
        class _Radio:
            tx_status = 2                                # TUNE carrier running

        with mock.patch.object(server, "radio", _Radio()):
            server._rec_tap_rx(_block(882), 44100)
        self.assertTrue(server._rec_queue.empty())

    def test_tx_tap_queues_at_the_codec_rate(self):
        server._rec_tap_tx(_block(960))
        source, _pcm, rate, _ts = server._rec_queue.get_nowait()
        self.assertEqual((source, rate), ("tx", server.TX_RATE))

    def test_taps_never_raise_without_a_session(self):
        server._rec_session.close_without_finishing()
        server._rec_tap_rx(_block(), 48000)              # no active session
        server._rec_tap_tx(_block())
        server._rec_tap_rx(b"", 48000)                   # empty block

    def test_shutdown_finishes_the_active_recording(self):
        info = server._rec_session.stop()
        self.assertIsNotNone(info)
        self.assertFalse(server._rec_session.active)


class RecorderLifecycleContractTests(unittest.TestCase):
    """The writer task must be started and stopped with the other loops."""

    def test_writer_task_is_started_in_lifespan(self):
        import inspect
        source = inspect.getsource(server.lifespan)
        self.assertIn("_recording_writer_loop", source)

    def test_writer_is_cancelled_and_session_closed_on_shutdown(self):
        import inspect
        source = inspect.getsource(server.lifespan)
        self.assertIn("_rec_writer_task.cancel()", source)
        self.assertIn("close_without_finishing", source)


if __name__ == "__main__":
    unittest.main()
