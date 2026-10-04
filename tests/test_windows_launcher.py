import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from windows import launcher


class WindowsLauncherTests(unittest.TestCase):
    def test_local_url_uses_localhost_for_ipv6_wildcard_bind(self):
        self.assertEqual(
            launcher.local_url({"MRRC_WEB_HOST": "::", "MRRC_WEB_PORT": "8888"}),
            "http://localhost:8888",
        )

    def test_local_url_uses_loopback_for_ipv4_wildcard_bind(self):
        self.assertEqual(
            launcher.local_url({"MRRC_WEB_HOST": "0.0.0.0", "MRRC_WEB_PORT": "8888"}),
            "http://127.0.0.1:8888",
        )

    def test_local_url_falls_back_to_legacy_ft710_prefix(self):
        self.assertEqual(
            launcher.local_url({"FT710_WEB_HOST": "::", "FT710_WEB_PORT": "9999"}),
            "http://localhost:9999",
        )

    def test_load_env_makes_ftdi_dir_absolute(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "mrrc_modern.env"
            config.write_text(
                "MRRC_FTDI_LIB_DIR=vendor\\ftdi\\windows\\bin\\x64\n",
                encoding="utf-8",
            )
            app_root = tmp_path / "app"
            with patch.object(launcher, "app_dir", return_value=app_root):
                env = launcher.load_env(config)

        self.assertEqual(
            Path(env["MRRC_FTDI_LIB_DIR"]),
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
        """Frozen mode without MRRC-Modern-Server.exe must NOT spawn the launcher
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


class WindowsLauncherSslTests(unittest.TestCase):
    """SDD V2.10: launcher starts the server on HTTPS by default."""

    def test_build_command_without_ssl_pair_keeps_no_ssl(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp)
            (app_root / "MRRC-Modern-Server.exe").write_text("x", encoding="utf-8")
            with patch.object(launcher, "app_dir", return_value=app_root):
                cmd = launcher.build_command(None)
            assert cmd is not None, "no cert pair does not mean no command line"
            self.assertIn("--no-ssl", cmd)

    def test_build_command_with_ssl_pair_passes_cert_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp)
            (app_root / "MRRC-Modern-Server.exe").write_text("x", encoding="utf-8")
            pair = (Path(tmp) / "server.crt", Path(tmp) / "server.key")
            with patch.object(launcher, "app_dir", return_value=app_root):
                cmd = launcher.build_command(pair)
            assert cmd is not None, "a cert pair does not mean no command line"
            self.assertNotIn("--no-ssl", cmd)
            self.assertIn("--ssl-cert", cmd)
            self.assertIn("--ssl-key", cmd)
            self.assertIn(str(pair[0]), cmd)
            self.assertIn(str(pair[1]), cmd)

    def test_local_url_secure_uses_https(self):
        self.assertEqual(
            launcher.local_url({"MRRC_WEB_PORT": "8888"}, secure=True),
            "https://127.0.0.1:8888",
        )

    def test_local_url_default_stays_http(self):
        self.assertEqual(
            launcher.local_url({"MRRC_WEB_PORT": "8888"}),
            "http://127.0.0.1:8888",
        )

    def test_ssl_material_honours_ssl_off(self):
        self.assertIsNone(launcher.ssl_material({"MRRC_SSL": "off"}))

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

    def test_ssl_material_falls_back_to_legacy_ft710_prefix(self):
        self.assertIsNone(launcher.ssl_material({"FT710_SSL": "off"}))


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


class WindowsLauncherEnvEncodingTests(unittest.TestCase):
    """Field report 2026-09-12 (Win11 VM): the installed app did not start.

    The config had gone through an ANSI (GBK) editor and was not valid UTF-8;
    `load_env` raised UnicodeDecodeError, the console closed instantly and the
    operator saw nothing at all.
    """

    DAMAGED = (b"# mrrc_modern.env \xe2\x80?MRRC Modern launcher "
               b"configuration template.\nMRRC_WEB_HOST=127.0.0.1\n"
               b"MRRC_WEB_PORT=8888\n")

    def test_load_env_survives_a_non_utf8_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "mrrc_modern.env"
            config.write_bytes(self.DAMAGED)
            app_root = Path(tmp) / "app"
            with patch.object(launcher, "app_dir", return_value=app_root):
                env = launcher.load_env(config)
        self.assertEqual(env["MRRC_WEB_HOST"], "127.0.0.1")
        self.assertEqual(env["MRRC_WEB_PORT"], "8888")

    def test_load_env_decodes_gbk_device_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "mrrc_modern.env"
            config.write_bytes("MRRC_AUDIO_RX_DEVICE=麦克风 (USB Audio CODEC)\n"
                               .encode("cp936"))
            with patch.object(launcher, "app_dir", return_value=Path(tmp)):
                env = launcher.load_env(config)
        self.assertEqual(env["MRRC_AUDIO_RX_DEVICE"], "麦克风 (USB Audio CODEC)")

    def test_a_fatal_error_is_logged_and_shown(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(launcher, "user_data_dir", return_value=Path(tmp)), \
             patch.object(launcher, "_show_message_box") as box:
            try:
                raise RuntimeError("config exploded")
            except RuntimeError as exc:
                rc = launcher.report_fatal(exc)
            log = Path(tmp) / "launcher.log"
            self.assertTrue(log.exists(), "a dying launcher must leave a log")
            text = log.read_text(encoding="utf-8")
        self.assertEqual(rc, 1)
        self.assertIn("config exploded", text)
        self.assertIn("Traceback", text)
        self.assertTrue(box.called, "and must show a message box")


@contextlib.contextmanager
def _quiet_stdout():
    """The scheme fallback prints on purpose; keep it out of the suite output."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield


class SchemeProbeTests(unittest.TestCase):
    """The launcher must open the scheme the server actually answers on (v1.24.6).

    Field case: the installed 1.24.5 server logged ``falling back to plain HTTP`` while the
    launcher opened ``https://127.0.0.1:8888``. The browser reported a protocol error on a
    blank tab and the machine was written up as "black screen, app will not start" — while
    the app was in fact running and serving HTTP one scheme away.
    """

    def _refuse_https(self):
        """Stand in for a server that ended up on plain HTTP."""
        def answers(url, proc=None, timeout_s=2.0, secure=None):
            return not url.startswith("https://")
        return answers

    def test_wait_for_server_accepts_any_http_status(self):
        """A 401 from the auth middleware still proves the listener is up."""
        with patch("launcher_net.urllib.request.urlopen",
                   side_effect=__import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
                       "http://127.0.0.1:8888/api/health", 401, "Unauthorized", {}, None)):
            self.assertTrue(launcher.wait_for_server("http://127.0.0.1:8888", timeout_s=0))

    def test_https_url_falls_back_to_the_answering_http_url(self):
        url = "https://127.0.0.1:8888"
        with patch("launcher_net.answers", self._refuse_https()), _quiet_stdout():
            self.assertEqual(launcher.url_to_open(url), "http://127.0.0.1:8888")

    def test_the_fallback_is_announced(self):
        """A silent scheme switch would hide a broken certificate from everyone."""
        with patch("launcher_net.answers", self._refuse_https()):
            import io
            import contextlib

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                launcher.url_to_open("https://127.0.0.1:8888")
        text = buf.getvalue()
        self.assertIn("WARNING", text)
        self.assertIn("plain HTTP", text)

    def test_preferred_scheme_is_kept_when_it_answers(self):
        with patch("launcher_net.answers", return_value=True), _quiet_stdout():
            self.assertEqual(launcher.url_to_open("https://127.0.0.1:8888"),
                             "https://127.0.0.1:8888")

    def test_no_fallback_invents_a_url_when_neither_scheme_answers(self):
        with patch("launcher_net.answers", return_value=False), _quiet_stdout():
            self.assertEqual(launcher.url_to_open("https://127.0.0.1:8888"),
                             "https://127.0.0.1:8888")


class AlreadyRunningTests(unittest.TestCase):
    """Starting a second server on a live port is the failure, not the fix.

    Windows ``SO_REUSEADDR`` let the second bind succeed, so the two instances shared the
    port and split connections; the 2026-10-02 log has both of them enumerating the radio's
    audio devices and opening its CAT port ~1 ms apart. The server now refuses such a bind
    and the launcher declines to make one.
    """

    def test_finds_the_instance_that_is_up(self):
        with patch("launcher_net.first_answering",
                   return_value="http://127.0.0.1:8888") as probe:
            self.assertEqual(launcher.running_instance_url("https://127.0.0.1:8888"),
                             "http://127.0.0.1:8888")
        asked = probe.call_args.args[0]
        self.assertEqual(asked, ["https://127.0.0.1:8888", "http://127.0.0.1:8888"],
                         "both schemes must be asked: the port may hold either")

    def test_none_means_start_normally(self):
        with patch("launcher_net.first_answering", return_value=None):
            self.assertIsNone(launcher.running_instance_url("http://127.0.0.1:8888"))


class SingleInstanceTests(unittest.TestCase):
    """One launcher per session - the port probe alone cannot promise that.

    Measured 2026-10-04 on a field Windows box: two launchers, two servers, six frpc.exe. The
    probe in ``AlreadyRunningTests`` only sees a server that has *finished* starting, so two
    starts inside its 0.4 s window both decide the port is free. The mutex closes that window,
    and being a kernel mutex it cannot be left behind by a crash the way a pid file can.
    """

    def setUp(self):
        self._before = launcher._single_instance_handle
        launcher._single_instance_handle = None

    def tearDown(self):
        launcher._single_instance_handle = self._before

    def test_the_first_launcher_holds_the_mutex(self):
        with patch.object(launcher.os, "name", "nt"):
            self.assertTrue(launcher.acquire_single_instance(create=lambda name: (7, 0)))
        self.assertEqual(launcher._single_instance_handle, 7,
                         "the handle has to outlive the call, or Windows frees the mutex")

    def test_a_second_launcher_is_told_so(self):
        with patch.object(launcher.os, "name", "nt"):
            self.assertFalse(launcher.acquire_single_instance(
                create=lambda name: (7, launcher._ERROR_ALREADY_EXISTS)))

    def test_nothing_is_created_off_windows(self):
        def never(name):
            raise AssertionError("no mutex may be created off Windows")

        with patch.object(launcher.os, "name", "posix"):
            self.assertTrue(launcher.acquire_single_instance(create=never))

    def test_a_guard_that_cannot_run_does_not_block_a_start(self):
        """A broken check must never be the reason the app refuses to start."""
        def boom(name):
            raise OSError("kernel32.dll missing")

        with patch.object(launcher.os, "name", "nt"):
            self.assertTrue(launcher.acquire_single_instance(create=boom))
            self.assertTrue(launcher.acquire_single_instance(create=lambda name: (0, 0)))
