"""Support bundle core (spec 2026-09-17 §4/§6): redaction is the security boundary."""
import tempfile
import unittest
from pathlib import Path

import support_bundle as sb


class RedactEnvTextTests(unittest.TestCase):
    def test_password_key_is_dropped_by_omission(self):
        text, dropped, hits = sb.redact_env_text(
            "MRRC_WEB_PASSWORD=hunter2\nMRRC_WEB_PORT=8888\n")
        self.assertNotIn("hunter2", text)
        self.assertNotIn("MRRC_WEB_PASSWORD", text)
        self.assertIn("MRRC_WEB_PORT=8888", text)
        self.assertEqual(dropped, 1)
        self.assertEqual(hits, 0)

    def test_ssl_key_path_is_dropped(self):
        text, dropped, _ = sb.redact_env_text("MRRC_SSL_KEY=/x/privkey.pem\n")
        self.assertNotIn("privkey.pem", text)
        self.assertEqual(dropped, 1)

    def test_allowlisted_diagnostic_keys_survive(self):
        text, dropped, _ = sb.redact_env_text(
            "MRRC_RADIO_MODEL=ft710\nMRRC_SERIAL_PORT=/dev/cu.usbserial-0121DB3A0\n"
            "MRRC_BAUD_RATE=38400\nMRRC_AUDIO_RX_DEVICE=USB Audio\n")
        self.assertEqual(dropped, 0)
        self.assertIn("MRRC_SERIAL_PORT=/dev/cu.usbserial-0121DB3A0", text)
        self.assertIn("MRRC_AUDIO_RX_DEVICE=USB Audio", text)

    def test_comments_and_blank_lines_are_kept(self):
        text, dropped, _ = sb.redact_env_text("# radio\n\nMRRC_WEB_PORT=8888\n")
        self.assertIn("# radio", text)
        self.assertEqual(dropped, 0)

    def test_value_pass_cleans_a_secret_pasted_into_a_comment(self):
        text, _, hits = sb.redact_env_text("# old password=hunter2\n")
        self.assertNotIn("hunter2", text)
        self.assertEqual(hits, 1)


class RedactTextTests(unittest.TestCase):
    def test_replaces_values_and_counts(self):
        cleaned, hits = sb.redact_text(
            'GET /login?token=abc123&x=1\nMRRC_WEB_PASSWORD=x\n')
        self.assertNotIn("abc123", cleaned)
        self.assertEqual(hits, 2)

    def test_plain_text_untouched(self):
        cleaned, hits = sb.redact_text("RX open failed (-9996) — no device\n")
        self.assertEqual(hits, 0)
        self.assertIn("-9996", cleaned)


class CollectableTests(unittest.TestCase):
    def test_user_data_and_keys_are_refused(self):
        for path in ("recordings/15515kHz_20260912_222653.mp3",
                     "certs/server.key", "state/config.pem",
                     "mem_channels.json", "atr1000_tuner.json"):
            self.assertFalse(sb.is_collectable(path), path)

    def test_logs_and_diagnostics_are_allowed(self):
        for path in ("logs/server.log", "logs/server-stdout.log",
                     "logs/launcher.log", "diagnostics/env.json"):
            self.assertTrue(sb.is_collectable(path), path)

    def test_windows_separators_are_normalised(self):
        self.assertFalse(sb.is_collectable(r"C:\Users\x\certs\server.key"))


class TailLinesTests(unittest.TestCase):
    def test_small_file_is_read_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server.log"
            path.write_text("line1\nline2\n", encoding="utf-8")
            self.assertEqual(sb.tail_lines(str(path)), "line1\nline2\n")

    def test_tail_is_byte_bounded_and_line_aligned(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server.log"
            path.write_text("".join(f"line{i:05d}\n" for i in range(1000)), encoding="utf-8")
            text = sb.tail_lines(str(path), max_bytes=100)
            self.assertLessEqual(len(text.encode()), 100)
            self.assertTrue(text.startswith("line"), text[:20])
            self.assertTrue(text.endswith("\n"))

    def test_missing_file_is_empty_not_an_exception(self):
        self.assertEqual(sb.tail_lines("/nonexistent/server.log"), "")

    def test_invalid_utf8_is_replaced_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server.log"
            path.write_bytes(b"ok\n\xff\xfe bad\n")
            self.assertIn("ok", sb.tail_lines(str(path)))


class ResolveLogFilesTests(unittest.TestCase):
    def test_finds_server_stdout_and_launcher_across_two_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / "logs").mkdir()
            (data / "logs" / "server.log").write_text("s", encoding="utf-8")
            (data / "logs" / "server-stdout.log").write_text("o", encoding="utf-8")
            (data / "logs" / "server.log.1").write_text("p", encoding="utf-8")
            (data / "launcher.log").write_text("l", encoding="utf-8")
            found = sb.resolve_log_files(data / "logs", data)
        self.assertEqual(Path(found["server"]).name, "server.log")
        self.assertEqual(Path(found["server-prev"]).name, "server.log.1")
        self.assertEqual(Path(found["stdout"]).name, "server-stdout.log")
        self.assertEqual(Path(found["launcher"]).name, "launcher.log")

    def test_only_existing_files_are_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(sb.resolve_log_files(Path(tmp) / "logs", Path(tmp)), {})

    def test_source_mode_legacy_name_is_picked_up_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = Path(tmp)
            (install / "logs").mkdir()
            (install / "logs" / "ft710-server.log").write_text("x", encoding="utf-8")
            found = sb.resolve_log_files(install / "logs", "", install)
        self.assertEqual(Path(found["legacy"]).name, "ft710-server.log")
        self.assertEqual(len(set(found.values())), len(found))
