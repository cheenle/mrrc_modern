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


class MacLauncherFtdiDirTests(unittest.TestCase):
    """A relative MRRC_FTDI_LIB_DIR must resolve into the data tree (2026-10-04).

    The shipped default.env says ``MRRC_FTDI_LIB_DIR=vendor/ftdi/macos``.  The signed
    .app keeps that tree in Contents/Resources and only links it as Contents/MacOS/_internal,
    so anchoring the relative value on app_dir() pointed at a directory that does not
    exist: scope_pipe exited with "FTDI libraries not found" and the UI fell back to the
    S-meter spectrum curve on every macOS install.
    """

    def test_relative_ftdi_dir_resolves_through_the_internal_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp) / "Contents" / "MacOS"
            data_root = Path(tmp) / "Contents" / "Resources"
            (data_root / "vendor" / "ftdi" / "macos").mkdir(parents=True)
            app_root.mkdir(parents=True)
            # build.sh keeps the data tree in Resources and links it into MacOS, which is
            # what makes the app signable; recreate that layout rather than a plain dir.
            (app_root / "_internal").symlink_to(data_root, target_is_directory=True)
            config = Path(tmp) / "mrrc_modern.env"
            config.write_text("MRRC_FTDI_LIB_DIR=vendor/ftdi/macos\n", encoding="utf-8")
            with patch.object(launcher, "app_dir", return_value=app_root):
                env = launcher.load_env(config)
            resolved = Path(env["MRRC_FTDI_LIB_DIR"])
            self.assertTrue(resolved.is_dir(), f"the FTDI dir must exist: {resolved}")
            # It goes through the link into the data tree; the pre-fix value pointed at
            # Contents/MacOS/vendor/..., where nothing lives.
            self.assertEqual(resolved.resolve(),
                             (data_root / "vendor" / "ftdi" / "macos").resolve())
            self.assertNotEqual(resolved, app_root / "vendor" / "ftdi" / "macos")

    def test_relative_ftdi_dir_prefers_a_sibling_copy(self):
        """Source checkouts and a vendor tree next to the exe still win over _internal."""
        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp)
            (app_root / "vendor" / "ftdi" / "macos").mkdir(parents=True)
            config = Path(tmp) / "mrrc_modern.env"
            config.write_text("MRRC_FTDI_LIB_DIR=vendor/ftdi/macos\n", encoding="utf-8")
            with patch.object(launcher, "app_dir", return_value=app_root):
                env = launcher.load_env(config)
        self.assertEqual(Path(env["MRRC_FTDI_LIB_DIR"]),
                         app_root / "vendor" / "ftdi" / "macos")

    def test_absolute_ftdi_dir_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            chosen = Path(tmp) / "my-ca" / "ftdi"
            config = Path(tmp) / "mrrc_modern.env"
            config.write_text(f"MRRC_FTDI_LIB_DIR={chosen}\n", encoding="utf-8")
            with patch.object(launcher, "app_dir", return_value=Path(tmp) / "app"):
                env = launcher.load_env(config)
        self.assertEqual(Path(env["MRRC_FTDI_LIB_DIR"]), chosen)


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


class RuntimePathTests(unittest.TestCase):
    """The frozen app keeps its data tree in Contents/Resources, reached through
    the Contents/MacOS/_internal symlink: data under MacOS made codesign refuse to
    sign the bundle, which shipped "damaged" apps (2026-09-17)."""

    def test_prefers_the_file_next_to_the_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "macos").mkdir()
            (root / "macos" / "default.env").write_text("A=1", encoding="utf-8")
            with patch.object(launcher, "app_dir", return_value=root):
                self.assertEqual(launcher.runtime_path("macos", "default.env"),
                                 root / "macos" / "default.env")

    def test_falls_back_to_the_internal_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "_internal" / "macos").mkdir(parents=True)
            (root / "_internal" / "macos" / "default.env").write_text("A=1", encoding="utf-8")
            with patch.object(launcher, "app_dir", return_value=root):
                self.assertEqual(launcher.runtime_path("macos", "default.env"),
                                 root / "_internal" / "macos" / "default.env")

    def test_returns_the_primary_location_when_nothing_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(launcher, "app_dir", return_value=root):
                self.assertEqual(launcher.runtime_path("version.txt"), root / "version.txt")

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


class MacSchemeProbeTests(unittest.TestCase):
    """macOS shared the Windows bug: the launcher opened the scheme *it* chose.

    ``macos/launcher.py`` had the same blind ``webbrowser.open(url)`` after a TLS decision
    the server could not keep up with, so a Mac install whose server fell back to plain
    HTTP produced the same blank-tab protocol error. Both launchers now probe.
    """

    def test_falls_back_to_the_scheme_that_answers(self):
        with patch("launcher_net.answers",
                   lambda url, proc=None, timeout_s=2.0, secure=None:
                   not url.startswith("https://")):
            self.assertEqual(launcher.url_to_open("https://127.0.0.1:8888"),
                             "http://127.0.0.1:8888")

    def test_keeps_the_preferred_scheme(self):
        with patch("launcher_net.answers", return_value=True):
            self.assertEqual(launcher.url_to_open("https://127.0.0.1:8888"),
                             "https://127.0.0.1:8888")

    def test_neither_answering_opens_the_original_url(self):
        with patch("launcher_net.answers", return_value=False):
            self.assertEqual(launcher.url_to_open("http://127.0.0.1:8888"),
                             "http://127.0.0.1:8888")

    def test_both_schemes_are_asked_before_spawning(self):
        """An instance already on the port must be reused, not duplicated."""
        with patch("launcher_net.first_answering",
                   return_value="http://127.0.0.1:8888") as probe:
            self.assertEqual(launcher.running_instance_url("https://127.0.0.1:8888"),
                             "http://127.0.0.1:8888")
        self.assertEqual(probe.call_args.args[0],
                         ["https://127.0.0.1:8888", "http://127.0.0.1:8888"])

    def test_wait_for_server_delegates_to_the_shared_probe(self):
        with patch("launcher_net.answers", return_value=True) as answers:
            self.assertTrue(launcher.wait_for_server("https://127.0.0.1:8888",
                                                    secure=True, timeout_s=1))
        self.assertEqual(answers.call_args.args[0], "https://127.0.0.1:8888")
        self.assertTrue(answers.call_args.kwargs["secure"])
