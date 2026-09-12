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

import json
import numpy as np

import recorder
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


class RecorderExecutorTests(unittest.IsolatedAsyncioTestCase):
    """The writer must own its executor (field report 2026-09-12).

    The default thread pool is shared with serial/audio work; a CAT
    reconnect storm saturated it, the writer never ran, and every RX block
    of three recordings was dropped.
    """

    async def test_writer_never_uses_the_shared_default_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = RecordingSession(Path(tmp), bitrate=64, max_seconds=60)
            session.start(freq_hz=14_270_000)
            queue: asyncio.Queue = asyncio.Queue()
            calls = []
            real_to_thread = asyncio.to_thread

            def _forbidden(*args, **kwargs):
                calls.append(args[0].__name__ if args else "?")
                raise AssertionError("recorder must not use asyncio.to_thread")

            with mock.patch.object(asyncio, "to_thread", _forbidden):
                task = asyncio.create_task(
                    server._recording_writer_loop(queue, session))
                for _ in range(3):
                    queue.put_nowait(("rx", _block(), 48000, None))
                queue.put_nowait(("stop", b"", 0, None))
                await asyncio.wait_for(task, timeout=3.0)
            self.assertEqual(calls, [])
            self.assertFalse(session.active)
            self.assertGreater(next(Path(tmp).glob("*.mp3")).stat().st_size, 0)
            self.assertTrue(real_to_thread)               # sanity: it exists

    async def test_writer_uses_the_dedicated_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = RecordingSession(Path(tmp), bitrate=64, max_seconds=60)
            session.start(freq_hz=7_050_000)
            queue: asyncio.Queue = asyncio.Queue()
            seen = []
            real_pool = server._rec_pool
            with mock.patch.object(server, "_rec_pool") as pool:
                pool.submit.side_effect = (
                    lambda fn, *a, **kw: seen.append(fn.__name__)
                    or real_pool.submit(fn, *a, **kw))
                # run_in_executor accepts an executor; give it a wrapper that
                # routes submit() through the recording above.
                class _Proxy:
                    def submit(self, fn, *a, **kw):
                        seen.append(fn.__name__)
                        return real_pool.submit(fn, *a, **kw)

                with mock.patch.object(server, "_rec_pool", _Proxy()):
                    task = asyncio.create_task(
                        server._recording_writer_loop(queue, session))
                    queue.put_nowait(("rx", _block(), 48000, None))
                    queue.put_nowait(("stop", b"", 0, None))
                    await asyncio.wait_for(task, timeout=3.0)
            self.assertEqual(seen.count("add_audio"), 1)
            self.assertEqual(seen.count("stop"), 1)


class RecordingQueueTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.session = RecordingSession(Path(self._tmp.name), bitrate=64,
                                        max_seconds=60)
        self._saved = server._rec_session
        server._rec_session = self.session
        # Start from an empty queue regardless of other tests.
        while not server._rec_queue.empty():
            server._rec_queue.get_nowait()

    def tearDown(self):
        while not server._rec_queue.empty():
            server._rec_queue.get_nowait()
        self.session.close_without_finishing()
        server._rec_session = self._saved
        self._tmp.cleanup()

    def test_blocks_are_queued_with_their_source_and_rate(self):
        self.session.start()
        server._rec_enqueue("rx", b"\x00\x00" * 320, 48000, 12345)
        item = server._rec_queue.get_nowait()
        self.assertEqual(item, ("rx", b"\x00\x00" * 320, 48000, 12345))

    def test_nothing_is_queued_without_an_active_session(self):
        server._rec_enqueue("rx", b"\x00\x00" * 320, 48000)
        self.assertTrue(server._rec_queue.empty())

    def test_queue_is_bounded_and_counts_drops_per_session(self):
        self.session.start(freq_hz=7_050_000)
        first = self.session.status()["name"]
        for _ in range(server.REC_QUEUE_MAX + 25):
            server._rec_enqueue("rx", _block(), 48000)
        self.assertGreater(self.session.dropped, 0)
        self.assertLessEqual(server._rec_queue.qsize(), server.REC_QUEUE_MAX)
        # The count belongs to this recording, not to the process: a
        # cumulative counter made the 2026-09-12 diagnosis read wrong.
        self.assertEqual(self.session.status()["dropped"], self.session.dropped)
        self.session.close_without_finishing()
        self.session.start(freq_hz=7_050_000)
        self.assertEqual(self.session.dropped, 0)
        self.assertTrue(self.session.status()["name"].endswith(".mp3"))
        self.assertTrue(first.endswith(".mp3"))


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


class RecordingsRestTests(unittest.TestCase):
    """The three recordings routes and their path safety (spec §6)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._saved_dir = server.RECORDINGS_DIR
        self._saved_index = server.RECORDINGS_INDEX
        self._saved_session = server._rec_session
        server.RECORDINGS_DIR = self.dir
        server.RECORDINGS_INDEX = self.dir.parent / f"{self.dir.name}-index.json"
        self.name = "14270kHz_20260912_210405.mp3"
        (self.dir / self.name).write_bytes(b"ID3" + b"x" * 8000)

    def tearDown(self):
        server.RECORDINGS_DIR = self._saved_dir
        server.RECORDINGS_INDEX = self._saved_index
        server._rec_session = self._saved_session
        self._tmp.cleanup()

    def test_path_helper_accepts_our_names_only(self):
        self.assertEqual(server._recording_path(self.name),
                         (self.dir / self.name).resolve())
        for bad in ("../server.py", "/etc/passwd", "x.mp3",
                    "14270kHz_20260912_210405.mp3.bak", "",
                    "14270kHz_20260912_210405.mp3/../x.mp3"):
            with self.subTest(name=bad):
                self.assertIsNone(server._recording_path(bad))

    def test_listing_payload_shape(self):
        payload = server._recordings_payload()
        self.assertEqual(payload["count"], 1)
        row = payload["recordings"][0]
        self.assertEqual(row["name"], self.name)
        self.assertEqual(row["freq_hz"], 14_270_000)
        self.assertFalse(row["recording"])
        self.assertGreaterEqual(payload["total_bytes"], 8000)

    def test_listing_marks_the_active_recording(self):
        session = RecordingSession(self.dir, bitrate=64, max_seconds=60)
        server._rec_session = session
        session.start(freq_hz=14_270_000)
        active_name = session.status()["name"]
        payload = server._recordings_payload()
        by_name = {r["name"]: r for r in payload["recordings"]}
        self.assertTrue(by_name[active_name]["recording"])
        session.close_without_finishing()

    def test_delete_removes_the_file_and_updates_the_index(self):
        asyncio.run(server._delete_recording(self.name))
        self.assertFalse((self.dir / self.name).exists())
        self.assertEqual(server._recordings_payload()["count"], 0)

    def test_delete_refuses_a_missing_or_invalid_name(self):
        for bad in ("07050kHz_20260912_210405.mp3", "../server.py", ""):
            with self.subTest(name=bad):
                self.assertFalse(asyncio.run(server._delete_recording(bad)))

    def test_delete_refuses_the_active_recording(self):
        session = RecordingSession(self.dir, bitrate=64, max_seconds=60)
        server._rec_session = session
        session.start(freq_hz=14_270_000)
        try:
            active_name = session.status()["name"]
            self.assertFalse(asyncio.run(server._delete_recording(active_name)))
        finally:
            session.close_without_finishing()

    def test_delete_endpoint_reports_409_while_recording(self):
        session = RecordingSession(self.dir, bitrate=64, max_seconds=60)
        server._rec_session = session
        session.start(freq_hz=14_270_000)
        try:
            active_name = session.status()["name"]
            response = asyncio.run(server.api_recording_delete(active_name))
            self.assertEqual(response.status_code, 409)
        finally:
            session.close_without_finishing()

    def test_audio_stream_supports_range_requests(self):
        captured = []

        async def send(message):
            captured.append(message)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        response = server._recording_response(self.name)
        self.assertIsNotNone(response)
        assert response is not None                 # narrow for the checker
        scope = {"type": "http", "method": "GET",
                 "path": f"/api/recordings/{self.name}",
                 "headers": [(b"range", b"bytes=0-99")]}
        asyncio.run(response(scope, receive, send))
        start = captured[0]
        self.assertEqual(start["status"], 206)
        headers = {k.lower(): v for k, v in start["headers"]}
        self.assertEqual(headers[b"accept-ranges"], b"bytes")
        self.assertIn(b"content-range", headers)

    def test_audio_stream_without_range_is_200(self):
        captured = []

        async def send(message):
            captured.append(message)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        response = server._recording_response(self.name)
        assert response is not None                 # narrow for the checker
        scope = {"type": "http", "method": "GET",
                 "path": f"/api/recordings/{self.name}", "headers": []}
        asyncio.run(response(scope, receive, send))
        self.assertEqual(captured[0]["status"], 200)

    def test_unknown_name_has_no_response(self):
        self.assertIsNone(server._recording_response("../server.py"))
        self.assertIsNone(server._recording_response("07050kHz_20260912_210405.mp3"))


class _FakeWS:
    def __init__(self):
        self.messages = []

    async def send_text(self, text):
        self.messages.append(json.loads(text))

    def errors(self):
        return [m.get("message", "") for m in self.messages
                if m.get("type") == "error"]


class RecordingFailureHandlingTests(unittest.IsolatedAsyncioTestCase):
    """A failing REC must answer with a reason, not kill the control channel.

    Field report (2026-09-12): pressing REC on a deployed server closed
    /WSradio with 1006 in a reconnect loop, because an exception inside a
    set handler propagated out of the receive loop.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._saved = server._rec_session

    def tearDown(self):
        server._rec_session = self._saved
        self._tmp.cleanup()

    async def test_missing_encoder_start_raises_an_actionable_error(self):
        session = RecordingSession(self.dir, bitrate=64, max_seconds=60)
        with mock.patch.object(recorder, "lameenc", None), \
             mock.patch.object(recorder, "encoder_available",
                               staticmethod(lambda: False)):
            with self.assertRaises(recorder.RecorderError) as ctx:
                session.start(freq_hz=14_270_000)
        message = str(ctx.exception)
        self.assertIn("lameenc", message)
        self.assertIn("install", message.lower())
        self.assertFalse(session.active)

    async def test_unwritable_directory_start_raises_an_actionable_error(self):
        session = RecordingSession(self.dir, bitrate=64, max_seconds=60)
        with mock.patch("pathlib.Path.mkdir", side_effect=PermissionError("read-only")):
            with self.assertRaises(recorder.RecorderError) as ctx:
                session.start(freq_hz=14_270_000)
        self.assertIn(str(self.dir), str(ctx.exception))
        self.assertFalse(session.active)

    async def test_handler_reports_the_reason_and_stays_alive(self):
        session = RecordingSession(self.dir, bitrate=64, max_seconds=60)
        session.start = mock.MagicMock(                          # type: ignore[method-assign]
            side_effect=recorder.RecorderError("MP3 encoder (lameenc) "
                                               "is not installed on the server"))
        server._rec_session = session
        ws = _FakeWS()
        with mock.patch.object(server, "_broadcast_recording_state",
                               mock.AsyncMock()):
            await server._execute_set_command(
                "recording", True, cast(Any, ws))            # must not raise
        self.assertTrue(any("lameenc" in m for m in ws.errors()), ws.errors())

    async def test_encoder_availability_helper_reflects_reality(self):
        self.assertTrue(recorder.encoder_available())            # installed here
        with mock.patch.object(recorder, "lameenc", None):
            self.assertFalse(recorder.encoder_available())

    def test_readiness_check_is_called_at_startup(self):
        import inspect
        self.assertIn("_log_recording_readiness", inspect.getsource(server.lifespan))

    def test_readiness_check_warns_when_the_encoder_is_missing(self):
        # server.py imported the helper by value, so patch it there.
        with mock.patch.object(server, "encoder_available", lambda: False):
            with self.assertLogs("mrrc", level="WARNING") as cap:
                server._log_recording_readiness()
        self.assertTrue(any("lameenc" in line for line in cap.output), cap.output)

    def test_ws_loop_survives_a_failing_message(self):
        import inspect
        source = inspect.getsource(server.ws_radio)
        # The per-message dispatch must be guarded so one bad command cannot
        # tear down the control channel (the 1006 loop of 2026-09-12).
        guarded = source.index("try:", source.index("await ws.receive_text()"))
        self.assertIn("_handle_ws_message", source[guarded:guarded + 400])


if __name__ == "__main__":
    unittest.main()
