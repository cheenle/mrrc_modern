import asyncio
import struct
import unittest
from pathlib import Path
from unittest.mock import patch

import backends.ft710.scope_producer as scope_producer
from backends.ft710.scope_producer import FT710ScopeProducer


class FakePipeStream:
    async def read(self, _n):
        return b""

    async def readline(self):
        return b""


class FakePipeProcess:
    pid = 12345
    returncode = 1

    def __init__(self):
        self.stdout = FakePipeStream()
        self.stderr = FakePipeStream()


class ScopePipeRestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_exited_scope_pipe_can_restart_while_current_reader_task_is_finishing(self):
        started = []

        async def no_sleep(_seconds):
            return None

        async def fake_create_subprocess_exec(*_args, **_kwargs):
            started.append(True)
            return FakePipeProcess()

        proc = FakePipeProcess()
        producer = FT710ScopeProducer(Path("."), scope=None)
        producer._active = True     # as if spectrum clients are connected
        producer._read_task = None
        producer._proc = proc

        async def run_reader_as_tracked_task():
            producer._read_task = asyncio.current_task()
            await producer._read_pipe(proc)

        with (
            patch.object(scope_producer, "pipe_command", return_value=["scope_pipe"]),
            patch.object(scope_producer.asyncio, "sleep", no_sleep),
            patch.object(scope_producer.asyncio, "create_subprocess_exec", fake_create_subprocess_exec),
        ):
            await run_reader_as_tracked_task()

        self.assertEqual(len(started), 1)

    async def test_inactive_producer_does_not_restart(self):
        """stop() (last spectrum client gone) must disable auto-restart."""
        started = []

        async def fake_create_subprocess_exec(*_args, **_kwargs):
            started.append(True)
            return FakePipeProcess()

        proc = FakePipeProcess()
        producer = FT710ScopeProducer(Path("."), scope=None)
        producer._active = False
        producer._read_task = None
        producer._proc = proc

        with (
            patch.object(scope_producer, "pipe_command", return_value=["scope_pipe"]),
            patch.object(scope_producer.asyncio, "create_subprocess_exec", fake_create_subprocess_exec),
        ):
            await producer._read_pipe(proc)

        self.assertEqual(started, [])


class HeartbeatStream:
    """Yields one len=0 heartbeat header, then EOF."""

    def __init__(self):
        self._chunks = [struct.pack(">I", 0), b""]

    async def read(self, _n):
        return self._chunks.pop(0) if self._chunks else b""

    async def readline(self):
        return b""


class HeartbeatPipeProcess:
    pid = 12346
    returncode = 0

    def __init__(self):
        self.stdout = HeartbeatStream()
        self.stderr = HeartbeatStream()


class ScopePipeHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_length_heartbeat_is_accepted_silently(self):
        """len=0 frames keep the pipe alive; they must not log warnings."""
        proc = HeartbeatPipeProcess()
        producer = FT710ScopeProducer(Path("."), scope=None)
        warnings = []

        def fake_warning(msg, *args):
            warnings.append(msg % args if args else msg)

        with patch.object(scope_producer.logger, "warning", fake_warning):
            await producer._read_pipe(proc)

        self.assertFalse(
            any("bad frame length" in w for w in warnings),
            f"heartbeat triggered warnings: {warnings}",
        )

    def test_scope_pipe_writes_stdout_heartbeat(self):
        """scope_pipe must emit the len=0 heartbeat so a dead parent is
        detected via EPIPE instead of orphaning the FT4222 device."""
        source = (
            Path(__file__).resolve().parents[1]
            / "backends" / "ft710" / "scope_pipe.py"
        ).read_text(encoding="utf-8")
        self.assertIn("struct.pack('>I', 0)", source)
        self.assertIn("STDOUT_HEARTBEAT_S", source)


if __name__ == "__main__":
    unittest.main()
