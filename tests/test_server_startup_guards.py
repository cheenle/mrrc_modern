"""Server start-up guards: one listener per port, and never write into the install dir.

Both classes here come from the 2026-10-02 field investigation of the installed 1.24.5
build on a Windows machine that "shows a black screen after installing". The log showed
the server answering, but answering badly:

* two start-ups interleaved in one file ~1 ms apart — Windows ``SO_REUSEADDR`` let a
  second instance bind a port that was already listening, so the browser could be served
  by a copy that never got the radio;
* ``Permission denied: 'C:\\Program Files\\MRRC Modern\\mrrc_modern.env.tmp'`` and
  ``C:\\Program Files\\MRRC Modern\\recordings is not writable`` — a server started
  without its launcher resolved every writable default into the read-only install dir.
"""
from __future__ import annotations

import contextlib
import errno
import os
import shutil
import socket as _socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import server


class RecordingSocket:
    """Stands in for the OS socket so the bind guard can be inspected, not just run."""

    def __init__(self, family, kind, bind_error: OSError | None = None):
        self.family = family
        self.kind = kind
        self.options: list[tuple[int, int, int]] = []
        self.bound: tuple[str, int] | None = None
        self.backlog: int | None = None
        self.closed = False
        self.bind_attempts = 0
        self._bind_error = bind_error

    def setsockopt(self, level, option, value):
        self.options.append((level, option, value))

    def bind(self, addr):
        self.bind_attempts += 1
        self.bound = addr
        if self._bind_error is not None:
            raise self._bind_error

    def listen(self, backlog):
        self.backlog = backlog

    def close(self):
        self.closed = True


def _socket_patch(made: list, bind_error: OSError | None = None):
    """patch() argument replacing ``server.socket.socket``; appends each socket created."""
    def factory(family, kind):
        sock = RecordingSocket(family, kind, bind_error)
        made.append(sock)
        return sock

    return patch.object(server.socket, "socket", factory)


class BindExclusionTests(unittest.TestCase):
    """What the listener asks the OS about port ownership, per platform."""

    def test_windows_asks_for_exclusive_address_use(self):
        """Windows must NOT use SO_REUSEADDR.

        SO_REUSEADDR on Windows does not mean "skip the TIME_WAIT wait" the way it does on
        POSIX — it permits binding a port another process is already listening on. That is
        how two MRRC Modern servers ended up in one log file, splitting the browser.
        """
        sock = MagicMock()
        server._set_bind_exclusion(sock, windows=True)
        opts = [call.args[1] for call in sock.setsockopt.call_args_list]
        self.assertIn(getattr(_socket, "SO_EXCLUSIVEADDRUSE", 1024), opts)
        self.assertNotIn(_socket.SO_REUSEADDR, opts,
                         "SO_REUSEADDR is what let a second instance steal the port")

    def test_posix_keeps_reuseaddr_for_restart(self):
        """POSIX needs SO_REUSEADDR: the launcher restarts the server in place and the old
        listener may still be in TIME_WAIT a moment later."""
        sock = MagicMock()
        server._set_bind_exclusion(sock, windows=False)
        opts = [call.args[1] for call in sock.setsockopt.call_args_list]
        self.assertIn(_socket.SO_REUSEADDR, opts)
        self.assertNotIn(getattr(_socket, "SO_EXCLUSIVEADDRUSE", 1024), opts)

    def test_dual_stack_options_are_still_applied(self):
        """The IPv4 clients the launcher's browser link depends on must keep working."""
        made: list[RecordingSocket] = []
        with _socket_patch(made), patch.object(server, "time", MagicMock()):
            returned = server._bind_dual_stack_socket(8888)
        self.assertIs(returned, made[0])
        sock = made[0]
        self.assertEqual(sock.family, _socket.AF_INET6)
        self.assertIn((_socket.IPPROTO_IPV6, _socket.IPV6_V6ONLY, 0), sock.options,
                      "V6ONLY=0 is what lets 127.0.0.1 connect to the :: listener")
        self.assertEqual(sock.bound, ("::", 8888))
        self.assertEqual(sock.backlog, 2048)


class PortContentionTests(unittest.TestCase):
    """A taken port must fail loudly, never quietly."""

    def test_addr_in_use_recognises_windows_and_posix_codes(self):
        self.assertTrue(server._is_addr_in_use(OSError(10048, "WSAEADDRINUSE")))
        self.assertTrue(server._is_addr_in_use(OSError(errno.EADDRINUSE, "EADDRINUSE")))
        self.assertFalse(server._is_addr_in_use(OSError(errno.EACCES, "permission")))

    def test_contention_is_retried_then_raised_with_an_actionable_message(self):
        """The launcher restarts this exe on a config change; the previous listener can
        still be on its way out. Retry briefly — then stop, because a server that binds
        nothing while claiming success is worse than one that exits."""
        made: list[RecordingSocket] = []
        boom = OSError(10048, "only each usage of each socket address")
        with _socket_patch(made, boom), patch.object(server, "time", MagicMock()) as slept:
            with self.assertRaises(RuntimeError) as caught:
                server._bind_dual_stack_socket(8888, retries=4)
        text = str(caught.exception)
        self.assertIn("8888", text)
        self.assertIn("Another MRRC Modern", text)
        self.assertEqual([s.bind_attempts for s in made], [1] * 4,
                         "every attempt must use a fresh socket")
        self.assertTrue(all(s.closed for s in made),
                        "failed sockets must be closed, not leaked")
        self.assertEqual(slept.sleep.call_count, 3, "last attempt does not wait")

    def test_a_permission_error_is_not_retried_as_contention(self):
        made: list[RecordingSocket] = []
        with _socket_patch(made, OSError(errno.EACCES, "denied")), \
                patch.object(server, "time", MagicMock()):
            with self.assertRaises(OSError) as caught:
                server._bind_dual_stack_socket(8888, retries=4)
        self.assertNotIsInstance(caught.exception, RuntimeError)
        self.assertEqual(len(made), 1, "non-contention fails on the first attempt")

    def test_hint_names_the_cause_and_the_cure(self):
        text = server._already_running_hint(8888, OSError(10048, "boom"))
        self.assertIn("8888", text)
        self.assertIn("close", text.lower())
        self.assertIn("serial", text.lower(),
                      "two instances also fight over the radio's serial port")


@contextlib.contextmanager
def _temp_dirs():
    """Yield (install-like dir, user-data-like dir) under a throwaway root."""
    root = Path(tempfile.mkdtemp(prefix="mrrc-writable-"))
    install = root / "MRRC Modern"
    user = root / "user-data"
    install.mkdir()
    user.mkdir()
    try:
        yield install, user
    finally:
        try:
            install.chmod(0o700)
        except OSError:
            pass
        shutil.rmtree(root, ignore_errors=True)


@unittest.skipIf(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    "a chmod-based read-only dir is not enforceable as root, and not this shape on Windows")
class WritableRuntimeDirTests(unittest.TestCase):
    """A packaged server started without its launcher must not aim at the install dir.

    ``_runtime_dir()`` is ``C:\\Program Files\\MRRC Modern`` in a packaged build. The
    launchers set MRRC_MEM_FILE/MRRC_RECORDINGS_DIR for the children they spawn, but the
    server exe is also reachable on its own — the installer ships a shortcut for it — and
    the field log of that case is a fresh unreadable admin password on every boot
    ("could not save the generated password") plus recordings switched off.
    """

    def test_read_only_install_dir_falls_back_to_the_user_dir(self):
        with _temp_dirs() as (install, user):
            install.chmod(0o500)
            with patch.object(server, "_runtime_dir", return_value=install), \
                    patch.object(server, "default_user_dir", return_value=user):
                self.assertEqual(server._writable_runtime_dir(), user)

    def test_writable_install_dir_is_still_preferred(self):
        """Source checkouts and Linux/Pi installs keep their files next to the code."""
        with _temp_dirs() as (install, user):
            with patch.object(server, "_runtime_dir", return_value=install), \
                    patch.object(server, "default_user_dir", return_value=user):
                self.assertEqual(server._writable_runtime_dir(), install)

    def test_recordings_index_follows_the_writable_dir(self):
        with _temp_dirs() as (install, user):
            install.chmod(0o500)
            with patch.object(server, "_runtime_dir", return_value=install), \
                    patch.object(server, "default_user_dir", return_value=user), \
                    patch.dict(os.environ, {}, clear=True):
                base = server._writable_runtime_dir()
                self.assertEqual(base / "recordings.json", user / "recordings.json")

    def test_password_file_is_sought_in_the_writable_dir(self):
        """The whole "installed the new package and still cannot log in" chain.

        ``_config_file_path()`` derives from MEM_FILE, which derives from
        _writable_runtime_dir(): when it pointed into Program Files the generated password
        failed to save, so every boot minted a new one and none of them was ever readable.
        """
        with _temp_dirs() as (install, user):
            install.chmod(0o500)
            with patch.object(server, "_runtime_dir", return_value=install), \
                    patch.object(server, "default_user_dir", return_value=user), \
                    patch.object(server, "MEM_FILE", user / "mem_channels.json"), \
                    patch.dict(os.environ, {}, clear=True):
                self.assertEqual(server._config_file_path(), user / "mrrc_modern.env")


if __name__ == "__main__":
    unittest.main()


class DuplicateLogLineTests(unittest.TestCase):
    """Every line in the field log arrived twice, paired within milliseconds.

    Two explanations and both were real: the root logger is process-wide, so when a
    packaged server loads this module twice — once as ``__main__``, once as ``server`` —
    ``_setup_file_logging`` attaches a second handler to the same file and every record is
    written twice. (Two servers on one port, see above, is the other half.) A responder
    reading that log could not tell which events were real.
    """

    def _root_handlers(self):
        import logging

        return logging.getLogger().handlers

    def test_handler_attached_once_per_file(self):
        import logging

        saved = list(self._root_handlers())
        try:
            with tempfile.TemporaryDirectory() as tmp:
                log_dir = Path(tmp)
                with patch.object(server, "LOG_DIR", log_dir):
                    first = server._setup_file_logging()
                    after_one = len(self._root_handlers())
                    second = server._setup_file_logging()
                    after_two = len(self._root_handlers())
                self.assertEqual(first, log_dir / "server.log")
                self.assertEqual(second, first, "the second call must report the same file")
                self.assertEqual(after_two, after_one,
                                 "a second handler on one file doubles every log line")
        finally:
            # never leak a live file handler back into the suite
            for handler in list(self._root_handlers()):
                if handler not in saved:
                    logging.getLogger().removeHandler(handler)
                    handler.close()

    def test_a_different_log_file_still_gets_its_handler(self):
        """Idempotence is per file: a relocated LOG_DIR must start logging there."""
        import logging

        saved = list(self._root_handlers())
        try:
            with tempfile.TemporaryDirectory() as tmp:
                dir_a, dir_b = Path(tmp) / "a", Path(tmp) / "b"
                with patch.object(server, "LOG_DIR", dir_a):
                    server._setup_file_logging()
                count_a = len(self._root_handlers())
                with patch.object(server, "LOG_DIR", dir_b):
                    path_b = server._setup_file_logging()
                self.assertEqual(len(self._root_handlers()), count_a + 1)
                self.assertEqual(path_b, dir_b / "server.log")
        finally:
            for handler in list(self._root_handlers()):
                if handler not in saved:
                    logging.getLogger().removeHandler(handler)
                    handler.close()

    def test_already_logging_to_matches_by_resolved_path(self):
        import logging

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server.log"
            handler = logging.FileHandler(path)
            try:
                self.assertTrue(server._already_logging_to([handler], path))
                self.assertTrue(server._already_logging_to([handler], Path(tmp) / "./server.log"))
                self.assertFalse(server._already_logging_to([handler], Path(tmp) / "other.log"))
                self.assertFalse(server._already_logging_to([logging.StreamHandler()], path),
                                 "a stream handler has no file to compare")
            finally:
                handler.close()
