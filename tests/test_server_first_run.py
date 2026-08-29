"""Tests for first-run banner + /api/setup status in server.py."""
import os
import unittest
from unittest.mock import patch

import server
from config import SERIAL_PORT, RADIO_MODEL


class _FakeRequest:
    def __init__(self, host):
        self.client = type("Client", (), {"host": host})()


class LoginBannerTests(unittest.TestCase):
    def test_banner_empty_when_not_auto_password(self):
        with patch.dict(os.environ, {"MRRC_AUTO_PASSWORD": ""}, clear=False):
            self.assertEqual(server._first_run_password_banner(_FakeRequest("127.0.0.1")), "")

    def test_banner_contains_password_when_auto_and_loopback(self):
        with patch.dict(os.environ, {"MRRC_AUTO_PASSWORD": "1"}, clear=False), \
             patch.object(server, "WEB_PASSWORD", "abc123"):
            html = server._first_run_password_banner(_FakeRequest("::1"))
        self.assertIn("abc123", html)
        self.assertIn("首次运行", html)

    def test_banner_hidden_for_lan_client(self):
        with patch.dict(os.environ, {"MRRC_AUTO_PASSWORD": "1"}, clear=False), \
             patch.object(server, "WEB_PASSWORD", "abc123"):
            self.assertEqual(server._first_run_password_banner(_FakeRequest("192.168.1.5")), "")


class SetupStatusTests(unittest.TestCase):
    def test_status_reports_current_config(self):
        with patch.dict(
            os.environ,
            {"MRRC_AUTO_PASSWORD": "1", "MRRC_FIRST_RUN_DONE": "1"},
            clear=False,
        ):
            status = server._setup_status()
        self.assertTrue(status["first_run_done"])
        self.assertTrue(status["auto_password"])
        self.assertEqual(status["radio_model"], RADIO_MODEL)
        self.assertEqual(status["serial_port"], SERIAL_PORT)
        self.assertIn("audio_rx_device", status)
        self.assertIn("audio_tx_device", status)


if __name__ == "__main__":
    unittest.main()
