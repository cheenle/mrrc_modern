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

    def test_a_frozen_build_never_keeps_state_next_to_its_own_code(self):
        """macOS: the bundle IS writable, and writing into it breaks the signature.

        Measured on the frozen v1.24.6 ``.app``: a single file in
        ``Contents/MacOS/recordings/`` turns ``codesign --verify`` into
        "a sealed resource is missing or invalid", so recording one QSO would leave the
        app failing signature checks on its next launch. Writability is therefore not the
        right test for a packaged build — it goes to the per-user directory even when the
        bundle would happily have accepted the file.
        """
        with _temp_dirs() as (install, user):
            with patch.object(server.sys, "frozen", True, create=True), \
                    patch.object(server, "_runtime_dir", return_value=install), \
                    patch.object(server, "default_user_dir", return_value=user):
                self.assertTrue(os.access(install, os.W_OK),
                                "the point is that the install dir IS writable here")
                self.assertEqual(server._writable_runtime_dir(), user)

    def test_a_frozen_build_does_not_consult_writability_at_all(self):
        with _temp_dirs() as (install, user):
            probe = MagicMock(return_value=True)
            with patch.object(server.sys, "frozen", True, create=True), \
                    patch.object(server, "_runtime_dir", return_value=install), \
                    patch.object(server, "default_user_dir", return_value=user), \
                    patch.object(server.os, "access", probe):
                self.assertEqual(server._writable_runtime_dir(), user)
            probe.assert_not_called()

    def test_a_source_checkout_keeps_its_files_next_to_the_code(self):
        """The frozen rule must not reach source checkouts or the Linux/Pi install."""
        with _temp_dirs() as (install, user):
            with patch.object(server.sys, "frozen", False, create=True), \
                    patch.object(server, "_runtime_dir", return_value=install), \
                    patch.object(server, "default_user_dir", return_value=user):
                self.assertEqual(server._writable_runtime_dir(), install)


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
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            try:
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
                # Close the handlers BEFORE the TemporaryDirectory goes away.
                # This cleanup placed outside the with-block is green on
                # macOS/Linux (they delete an open file happily) and red on
                # Windows: PermissionError [WinError 32] inside cleanup() takes
                # down the whole build gate. Never leak a live handler.
                for handler in list(self._root_handlers()):
                    if handler not in saved:
                        logging.getLogger().removeHandler(handler)
                        handler.close()

    def test_a_different_log_file_still_gets_its_handler(self):
        """Idempotence is per file: a relocated LOG_DIR must start logging there."""
        import logging

        saved = list(self._root_handlers())
        with tempfile.TemporaryDirectory() as tmp:
            dir_a, dir_b = Path(tmp) / "a", Path(tmp) / "b"
            try:
                with patch.object(server, "LOG_DIR", dir_a):
                    server._setup_file_logging()
                count_a = len(self._root_handlers())
                with patch.object(server, "LOG_DIR", dir_b):
                    path_b = server._setup_file_logging()
                self.assertEqual(len(self._root_handlers()), count_a + 1)
                self.assertEqual(path_b, dir_b / "server.log")
            finally:
                # inside the with-block: Windows cannot delete an open file
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

class ExplicitHostBindTests(unittest.TestCase):
    """An explicit ``MRRC_WEB_HOST`` must be guarded exactly like the wildcard.

    The 2026-10-02 field machine ran with ``MRRC_WEB_HOST=127.0.0.1``, which took the
    ``else: uvicorn.run(host=...)`` branch and so skipped ``SO_EXCLUSIVEADDRUSE``, the
    restart retry and the actionable message. Measured on the packaged v1.24.6 exe with a
    clean per-user state: a second instance logged ``Server ready!`` (the app's startup
    event fires before uvicorn binds) and only then died on uvicorn's bare
    ``ERROR: [Errno 10048] error while attempting to bind on address ('127.0.0.1', 18892)``
    — the OS refused it, nothing explained it.
    """

    def test_explicit_ipv4_host_binds_that_address_only(self):
        made: list[RecordingSocket] = []
        with _socket_patch(made), patch.object(server, "time", MagicMock()):
            returned = server._bind_listener_socket("127.0.0.1", 18892)
        self.assertIs(returned, made[0])
        self.assertEqual(made[0].family, _socket.AF_INET)
        self.assertEqual(made[0].bound, ("127.0.0.1", 18892))
        self.assertEqual(made[0].backlog, 2048)

    def test_explicit_host_consults_the_platform_exclusivity_guard(self):
        """The whole point: Windows asks for exclusivity on EVERY host, not just ``::``."""
        seen: list[bool] = []
        with patch.object(server, "_set_bind_exclusion", lambda sock, windows: seen.append(windows)), \
                _socket_patch([]), patch.object(server, "time", MagicMock()):
            server._bind_listener_socket("127.0.0.1", 18892)
        self.assertEqual(seen, [os.name == "nt"])

    def test_wildcard_host_still_binds_dual_stack(self):
        made: list[RecordingSocket] = []
        with _socket_patch(made), patch.object(server, "time", MagicMock()):
            server._bind_listener_socket("::", 8888)
        self.assertEqual(made[0].family, _socket.AF_INET6)
        self.assertEqual(made[0].bound, ("::", 8888))
        self.assertIn((_socket.IPPROTO_IPV6, _socket.IPV6_V6ONLY, 0), made[0].options,
                      "V6ONLY=0 is what lets the launcher's 127.0.0.1 reach the :: listener")

    def test_an_empty_host_is_the_wildcard(self):
        made: list[RecordingSocket] = []
        with _socket_patch(made), patch.object(server, "time", MagicMock()):
            server._bind_listener_socket("", 8888)
        self.assertEqual(made[0].bound, ("::", 8888))

    def test_explicit_ipv6_host_stays_v6only(self):
        made: list[RecordingSocket] = []
        with _socket_patch(made), patch.object(server, "time", MagicMock()):
            server._bind_listener_socket("::1", 8888)
        self.assertEqual(made[0].family, _socket.AF_INET6)
        self.assertEqual(made[0].bound, ("::1", 8888))
        self.assertIn((_socket.IPPROTO_IPV6, _socket.IPV6_V6ONLY, 1), made[0].options,
                      "a specific address must not also claim the IPv4 wildcard")

    def test_contention_on_an_explicit_host_is_retried_then_explained(self):
        made: list[RecordingSocket] = []
        boom = OSError(10048, "only one usage of each socket address")
        with _socket_patch(made, boom), patch.object(server, "time", MagicMock()) as slept:
            with self.assertRaises(RuntimeError) as caught:
                server._bind_listener_socket("127.0.0.1", 18892, retries=4)
        text = str(caught.exception)
        self.assertIn("18892", text)
        self.assertIn("Another MRRC Modern", text)
        self.assertEqual([s.bind_attempts for s in made], [1] * 4,
                         "every attempt must use a fresh socket")
        self.assertTrue(all(s.closed for s in made), "failed sockets must not leak")
        self.assertEqual(slept.sleep.call_count, 3, "the last attempt does not wait")

    def test_a_permission_error_on_an_explicit_host_is_not_retried(self):
        made: list[RecordingSocket] = []
        with _socket_patch(made, OSError(errno.EACCES, "denied")), \
                patch.object(server, "time", MagicMock()):
            with self.assertRaises(OSError) as caught:
                server._bind_listener_socket("127.0.0.1", 18892, retries=4)
        self.assertNotIsInstance(caught.exception, RuntimeError)
        self.assertEqual(len(made), 1)

    def test_main_never_hands_a_host_to_uvicorn_to_bind(self):
        """Guard the call graph, not the text: ``uvicorn.run(host=...)`` was the branch
        that skipped every guard above.

        AST rather than a substring, because this module's own docstrings quote
        ``uvicorn.run(host=...)`` when explaining the defect - a text search matches the
        explanation and reports a healthy tree as broken (it did, on first run).
        """
        import ast

        source = (Path(__file__).resolve().parent.parent / "server.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                base = node.func.value
                if isinstance(base, ast.Name):
                    calls.append(f"{base.id}.{node.func.attr}")
                elif isinstance(base, ast.Call):
                    continue
                else:
                    calls.append(node.func.attr)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                calls.append(node.func.id)

        self.assertNotIn("uvicorn.run", calls,
                         "every start-up path must bind through _bind_listener_socket")
        self.assertIn("_bind_listener_socket", calls)
        self.assertEqual(calls.count("_bind_dual_stack_socket"), 1,
                         "the dual-stack bind is reached only via _bind_listener_socket")
