"""Support bundle core (spec 2026-09-17 §4/§6): redaction is the security boundary."""
import unittest

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
