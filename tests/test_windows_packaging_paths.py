import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import backends.ft710.scope_libraries as scope_libraries
import backends.ft710.scope_producer as scope_producer
import server


class WindowsPackagingPathTests(unittest.TestCase):
    def test_resource_roots_include_pyinstaller_meipass(self):
        fake_meipass = Path("/tmp/ft710_meipass")
        with patch.object(sys, "_MEIPASS", str(fake_meipass), create=True):
            roots = scope_libraries.get_resource_roots()
        self.assertIn(fake_meipass, roots)

    def test_resource_roots_include_frozen_executable_dir(self):
        fake_exe = "/tmp/ft710_app/ft710-server.exe"
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "executable", fake_exe),
        ):
            roots = scope_libraries.get_resource_roots()
        self.assertIn(Path("/tmp/ft710_app").resolve(), roots)

    def test_windows_vendor_dir_is_searched(self):
        with patch.object(scope_libraries.sys, "platform", "win32"):
            dirs = scope_libraries.get_candidate_library_dirs()
        self.assertTrue(
            any(
                path.as_posix().endswith("vendor/ftdi/windows/bin/x64")
                for path in dirs
            )
        )

    def test_configure_windows_dll_search_path_calls_add_dll_directory(self):
        calls = []

        def fake_add_dll_directory(path):
            calls.append(Path(path))
            return object()

        with (
            patch.object(scope_libraries.sys, "platform", "win32"),
            patch.object(
                scope_libraries.os,
                "add_dll_directory",
                fake_add_dll_directory,
                create=True,
            ),
            patch.object(
                scope_libraries,
                "get_candidate_library_dirs",
                return_value=[Path("/tmp/missing"), Path("/tmp/exists")],
            ),
            patch.object(Path, "is_dir", lambda self: self.as_posix() == "/tmp/exists"),
        ):
            scope_libraries.configure_windows_dll_search_path()

        self.assertEqual(calls, [Path("/tmp/exists")])


class ScopePipeCommandTests(unittest.TestCase):
    def test_unfrozen_scope_pipe_command_uses_module_invocation(self):
        repo_root = Path(__file__).resolve().parents[1]
        cmd = scope_producer.pipe_command(repo_root)
        self.assertIsNotNone(cmd)
        assert cmd is not None
        self.assertEqual(cmd[1:], ["-m", "backends.ft710.scope_pipe"])

    def test_frozen_scope_pipe_command_uses_bundled_exe(self):
        repo_root = Path(__file__).resolve().parents[1]
        with (
            patch.object(scope_producer.sys, "frozen", True, create=True),
            patch.object(scope_producer.sys, "platform", "win32"),
            patch.object(scope_producer.sys, "executable", r"C:\MRRC-FT710\ft710-server.exe"),
            patch.object(scope_producer.Path, "exists", lambda self: self.name == "scope_pipe.exe"),
        ):
            cmd = scope_producer.pipe_command(repo_root)
        self.assertIsNotNone(cmd)
        assert cmd is not None
        self.assertEqual(Path(cmd[0]).name, "scope_pipe.exe")


class ResourceDirTests(unittest.TestCase):
    def test_resource_dir_prefers_meipass_when_frozen(self):
        fake_meipass = Path("/tmp/ft710_app/_internal")
        with (
            patch.object(server.sys, "frozen", True, create=True),
            patch.object(server.sys, "_MEIPASS", str(fake_meipass), create=True),
        ):
            self.assertEqual(server._resource_dir(), fake_meipass)

    def test_resource_dir_falls_back_to_script_dir_in_source_mode(self):
        self.assertEqual(server._resource_dir(), server.SCRIPT_DIR)


if __name__ == "__main__":
    unittest.main()
