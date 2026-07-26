import struct
import unittest
from unittest.mock import patch

try:
    import server
except ImportError:
    server = None


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


@unittest.skipIf(server is None, "fastapi not available in test environment")
class ScopePipeRestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_exited_scope_pipe_can_restart_while_current_reader_task_is_finishing(self):
        old_clients = server.spectrum_clients
        old_read_task = server._scope_read_task
        old_proc = server._scope_proc
        old_scope = server.scope
        old_lock = server._scope_pipe_lock
        started = []

        async def no_sleep(_seconds):
            return None

        async def fake_create_subprocess_exec(*_args, **_kwargs):
            started.append(True)
            return FakePipeProcess()

        proc = FakePipeProcess()
        server.spectrum_clients = {object()}
        server._scope_read_task = None
        server._scope_proc = proc
        server.scope = None
        server._scope_pipe_lock = None

        async def run_reader_as_tracked_task():
            server._scope_read_task = server.asyncio.current_task()
            await server._read_scope_pipe(proc)

        try:
            with (
                patch.object(server, "_scope_pipe_command", return_value=["scope_pipe"]),
                patch.object(server.asyncio, "sleep", no_sleep),
                patch.object(server.asyncio, "create_subprocess_exec", fake_create_subprocess_exec),
            ):
                await run_reader_as_tracked_task()
        finally:
            server.spectrum_clients = old_clients
            server._scope_read_task = old_read_task
            server._scope_proc = old_proc
            server.scope = old_scope
            server._scope_pipe_lock = old_lock

        self.assertEqual(len(started), 1)


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


@unittest.skipIf(server is None, "fastapi not available in test environment")
class ScopePipeHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_length_heartbeat_is_accepted_silently(self):
        """len=0 frames keep the pipe alive; they must not log warnings."""
        proc = HeartbeatPipeProcess()
        old_scope = server.scope
        server.scope = None
        warnings = []

        def fake_warning(msg, *args):
            warnings.append(msg % args if args else msg)

        try:
            with patch.object(server.logger, "warning", fake_warning):
                await server._read_scope_pipe(proc)
        finally:
            server.scope = old_scope

        self.assertFalse(
            any("bad frame length" in w for w in warnings),
            f"heartbeat triggered warnings: {warnings}",
        )

    def test_scope_pipe_writes_stdout_heartbeat(self):
        """scope_pipe must emit the len=0 heartbeat so a dead parent is
        detected via EPIPE instead of orphaning the FT4222 device."""
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "scope_pipe.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("struct.pack('>I', 0)", source)
        self.assertIn("STDOUT_HEARTBEAT_S", source)


if __name__ == "__main__":
    unittest.main()
