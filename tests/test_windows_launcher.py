import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from windows import launcher


class WindowsLauncherTests(unittest.TestCase):
    def test_local_url_uses_localhost_for_ipv6_wildcard_bind(self):
        self.assertEqual(
            launcher.local_url({"FT710_WEB_HOST": "::", "FT710_WEB_PORT": "8888"}),
            "http://localhost:8888",
        )

    def test_local_url_uses_loopback_for_ipv4_wildcard_bind(self):
        self.assertEqual(
            launcher.local_url({"FT710_WEB_HOST": "0.0.0.0", "FT710_WEB_PORT": "8888"}),
            "http://127.0.0.1:8888",
        )

    def test_load_env_makes_ftdi_dir_absolute(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "ft710.env"
            config.write_text(
                "FT710_FTDI_LIB_DIR=vendor\\ftdi\\windows\\bin\\x64\n",
                encoding="utf-8",
            )
            app_root = tmp_path / "app"
            with patch.object(launcher, "app_dir", return_value=app_root):
                env = launcher.load_env(config)

        self.assertEqual(
            Path(env["FT710_FTDI_LIB_DIR"]),
            app_root / "vendor" / "ftdi" / "windows" / "bin" / "x64",
        )

    def test_seed_mem_channels_falls_back_to_pyinstaller_internal_dir(self):
        """PyInstaller 6 onedir keeps datas in "_internal", not next to the exe."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            app_root = tmp_path / "app"
            data_dir = tmp_path / "data"
            internal = app_root / "_internal"
            internal.mkdir(parents=True)
            data_dir.mkdir()
            (internal / "mem_channels.json").write_text(
                '{"channels": []}', encoding="utf-8"
            )
            with (
                patch.object(launcher, "app_dir", return_value=app_root),
                patch.object(launcher, "user_data_dir", return_value=data_dir),
            ):
                launcher.seed_mem_channels()

            seeded = data_dir / "mem_channels.json"
            self.assertTrue(seeded.exists())
            self.assertEqual(seeded.read_text(encoding="utf-8"), '{"channels": []}')

    def test_seed_mem_channels_keeps_existing_user_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            app_root = tmp_path / "app"
            data_dir = tmp_path / "data"
            app_root.mkdir()
            data_dir.mkdir()
            existing = data_dir / "mem_channels.json"
            existing.write_text('{"channels": [1]}', encoding="utf-8")
            (app_root / "mem_channels.json").write_text(
                '{"channels": []}', encoding="utf-8"
            )
            with (
                patch.object(launcher, "app_dir", return_value=app_root),
                patch.object(launcher, "user_data_dir", return_value=data_dir),
            ):
                launcher.seed_mem_channels()

            self.assertEqual(
                existing.read_text(encoding="utf-8"), '{"channels": [1]}'
            )

    def test_frozen_launcher_never_falls_back_to_itself(self):
        """Frozen mode without ft710-server.exe must NOT spawn the launcher
        again (sys.executable is the launcher itself) — it must give up."""
        import sys

        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp)
            (app_root / "server.py").write_text("# decoy\n", encoding="utf-8")
            with (
                patch.object(launcher, "app_dir", return_value=app_root),
                patch.object(sys, "frozen", True, create=True),
            ):
                self.assertIsNone(launcher.server_executable())
                self.assertIsNone(launcher.build_command())

    def test_source_mode_falls_back_to_server_py(self):
        import sys

        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp)
            (app_root / "server.py").write_text("# server\n", encoding="utf-8")
            with patch.object(launcher, "app_dir", return_value=app_root):
                cmd = launcher.build_command()
            self.assertIsNotNone(cmd)
            assert cmd is not None
            self.assertEqual(cmd[0], sys.executable)
            self.assertEqual(Path(cmd[1]).name, "server.py")


if __name__ == "__main__":
    unittest.main()
