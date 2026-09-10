"""Tests for linux/first_run.py — Pi first-boot auto-configuration."""
import sys
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "linux"))

import first_run  # noqa: E402


class _FakePort:
    def __init__(self, device, description="", hwid=""):
        self.device = device
        self.description = description
        self.hwid = hwid


class SerialFilterTests(unittest.TestCase):
    def test_linux_globs_exclude_onboard_uarts(self):
        ports = [
            _FakePort("/dev/ttyS0", "ttyS0"),
            _FakePort("/dev/ttyS31", "ttyS31"),
            _FakePort("/dev/ttyprintk", "ttyprintk"),
            _FakePort("/dev/ttyUSB0", "CP2102 USB to UART", "usb"),
            _FakePort("/dev/ttyACM0", "IC-7300", "usb"),
            _FakePort("", "bogus"),
        ]
        got = first_run._candidates(ports)
        devices = [p.device for p in got]
        self.assertEqual(devices, ["/dev/ttyUSB0", "/dev/ttyACM0"])

    def test_usb_serial_description_sorts_first(self):
        ports = [_FakePort("/dev/ttyACM0", "Some Board"),
                 _FakePort("/dev/ttyUSB1", "CP210x UART Bridge")]
        self.assertEqual(first_run.detect_serial_ports(ports)[0], "/dev/ttyUSB1")


class FirstRunFlowTests(unittest.TestCase):
    def test_apply_generates_password_and_skips_board_uarts(self):
        # FT-710 answers on ttyUSB1; ttyS0 (onboard) is probed first but silent.
        opened = []

        def open_func(port, baudrate, timeout):
            opened.append(port)
            ser = MagicMock()
            if port == "/dev/ttyUSB1":
                ser.read.side_effect = lambda n: b"ID" if baudrate == 38400 else b""
            else:
                ser.read.return_value = b""
            return ser

        env = {}
        cfg = Path("/tmp/test-mrrc.env")
        cfg.unlink(missing_ok=True)
        with unittest.mock.patch.object(
            first_run, "detect_serial_ports",
            return_value=["/dev/ttyS0", "/dev/ttyUSB1", "/dev/ttyACM0"],
        ):
            out = first_run.apply_first_run(env, cfg, open_func=open_func)
        self.assertEqual(out["MRRC_AUTO_PASSWORD"], "1")
        self.assertGreaterEqual(len(out["MRRC_WEB_PASSWORD"]), 20)
        self.assertEqual(out["MRRC_SERIAL_PORT"], "/dev/ttyUSB1")
        self.assertEqual(out["MRRC_RADIO_MODEL"], "ft710")
        self.assertEqual(out["MRRC_PORT_CONFIRMED"], "1")
        self.assertEqual(opened, ["/dev/ttyS0", "/dev/ttyS0", "/dev/ttyUSB1"])
        self.assertIn("MRRC_WEB_PASSWORD=", cfg.read_text(encoding="utf-8"))

    def test_needs_first_run_false_when_settled(self):
        env = {"MRRC_WEB_PASSWORD": "s3cret-pass", "MRRC_RADIO_MODEL": "ic7300",
               "MRRC_SERIAL_PORT": "/dev/ttyUSB0", "MRRC_PORT_CONFIRMED": "1"}
        self.assertFalse(first_run.needs_first_run(env))
