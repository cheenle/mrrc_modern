"""Tests for macos/launcher.py — HTTPS-by-default parity with Windows.

SDD V2.10: the macOS launcher now resolves a TLS cert/key pair (self-signed
bootstrap, or explicit MRRC_SSL_CERT/KEY) and starts the server on HTTPS,
mirroring windows/launcher.py::ssl_material / local_url / build_command.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from macos import launcher


class MacLauncherBuildTests(unittest.TestCase):
    def test_frozen_launcher_build_command_defaults_to_https_pair(self):
        """Frozen onefile + server exe + a cert pair -> --ssl-cert/--ssl-key."""
        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp)
            (app_root / "MRRC-Modern-Server").write_text("x", encoding="utf-8")
            pair = (Path(tmp) / "server.crt", Path(tmp) / "server.key")
            with (
                patch.object(launcher, "app_dir", return_value=app_root),
                patch.object(sys, "frozen", True, create=True),
            ):
                cmd = launcher.build_command(pair)
            self.assertIsNotNone(cmd)
            assert cmd is not None
            self.assertEqual(Path(cmd[0]).name, "MRRC-Modern-Server")
            self.assertNotIn("--no-ssl", cmd)
            self.assertIn("--ssl-cert", cmd)
            self.assertIn("--ssl-key", cmd)
            self.assertIn(str(pair[0]), cmd)
            self.assertIn(str(pair[1]), cmd)

    def test_build_command_without_ssl_pair_keeps_no_ssl(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp)
            (app_root / "MRRC-Modern-Server").write_text("x", encoding="utf-8")
            with patch.object(launcher, "app_dir", return_value=app_root):
                cmd = launcher.build_command(None)
            self.assertIsNotNone(cmd)
            assert cmd is not None
            self.assertIn("--no-ssl", cmd)

    def test_source_mode_falls_back_to_server_py_with_no_ssl(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp)
            (app_root / "server.py").write_text("# server\n", encoding="utf-8")
            with patch.object(launcher, "app_dir", return_value=app_root):
                cmd = launcher.build_command(None)
            self.assertIsNotNone(cmd)
            assert cmd is not None
            self.assertEqual(cmd[0], sys.executable)
            self.assertIn("--no-ssl", cmd)

    def test_server_executable_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(launcher, "app_dir", return_value=Path(tmp)):
                self.assertIsNone(launcher.build_command())


class MacLauncherSslTests(unittest.TestCase):
    def test_local_url_secure_uses_https_and_localhost_for_dual_stack(self):
        self.assertEqual(
            launcher.local_url({"MRRC_WEB_HOST": "::", "MRRC_WEB_PORT": "8888"},
                               secure=True),
            "https://localhost:8888",
        )

    def test_local_url_default_stays_http(self):
        self.assertEqual(
            launcher.local_url({"MRRC_WEB_PORT": "8888"}),
            "http://127.0.0.1:8888",
        )

    def test_local_url_falls_back_to_legacy_ft710_prefix(self):
        self.assertEqual(
            launcher.local_url({"FT710_WEB_HOST": "0.0.0.0", "FT710_WEB_PORT": "9999"}),
            "http://127.0.0.1:9999",
        )

    def test_ssl_material_honours_ssl_off(self):
        self.assertIsNone(launcher.ssl_material({"MRRC_SSL": "off"}))
        self.assertIsNone(launcher.ssl_material({"FT710_SSL": "off"}))

    def test_ssl_material_uses_explicit_existing_cert(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert = Path(tmp) / "my.crt"
            key = Path(tmp) / "my.key"
            cert.write_text("c", encoding="utf-8")
            key.write_text("k", encoding="utf-8")
            pair = launcher.ssl_material(
                {"MRRC_SSL_CERT": str(cert), "MRRC_SSL_KEY": str(key)}
            )
            self.assertEqual(pair, (cert, key))

    def test_ssl_material_ignores_explicit_cert_when_file_missing(self):
        """Missing explicit files must fall through to the self-signed bootstrap."""
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.crt"
            with patch.object(launcher, "user_data_dir",
                              return_value=Path(tmp) / "data") as udd:
                with patch(
                    "ssl_bootstrap.ensure_self_signed",
                    return_value=(Path(tmp) / "crt", Path(tmp) / "key"),
                ) as gen:
                    pair = launcher.ssl_material(
                        {"MRRC_SSL_CERT": str(missing), "MRRC_SSL_KEY": str(missing)}
                    )
            gen.assert_called_once_with(udd() / "certs")
            self.assertEqual(pair, (Path(tmp) / "crt", Path(tmp) / "key"))

    def test_ssl_material_defaults_to_self_signed_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(launcher, "user_data_dir",
                              return_value=Path(tmp) / "data") as udd:
                with patch(
                    "ssl_bootstrap.ensure_self_signed",
                    return_value=(Path(tmp) / "server.crt", Path(tmp) / "server.key"),
                ) as gen:
                    pair = launcher.ssl_material({})
            gen.assert_called_once_with(udd() / "certs")
            self.assertIsNotNone(pair)


if __name__ == "__main__":
    unittest.main()


class MacLauncherEnvEncodingTests(unittest.TestCase):
    """Same field bug as Windows: a non-UTF-8 config killed the launcher."""

    DAMAGED = (b"# mrrc_modern.env \xe2\x80?MRRC Modern launcher "
               b"configuration template.\nMRRC_WEB_PORT=8888\n")

    def test_load_env_survives_a_non_utf8_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "mrrc_modern.env"
            config.write_bytes(self.DAMAGED)
            with patch.object(launcher, "app_dir", return_value=Path(tmp)):
                env = launcher.load_env(config)
        self.assertEqual(env["MRRC_WEB_PORT"], "8888")

    def test_a_fatal_error_is_logged(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(launcher, "user_data_dir", return_value=Path(tmp)), \
             patch.object(launcher.rumps, "alert") as alert:
            try:
                raise RuntimeError("config exploded")
            except RuntimeError as exc:
                rc = launcher.report_fatal(exc)
            log = Path(tmp) / "launcher.log"
            self.assertTrue(log.exists())
            text = log.read_text(encoding="utf-8")
        self.assertEqual(rc, 1)
        self.assertIn("config exploded", text)
        self.assertIn("Traceback", text)
        self.assertTrue(alert.called)
