"""Tests for linux/first_run.py — Pi first-boot auto-configuration."""
import sys
import tempfile
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


class EnvFileEncodingTests(unittest.TestCase):
    """The Pi env file is edited elsewhere and copied in — it may not be UTF-8.

    Same field bug as the Windows/macOS launchers (2026-09-12): an ANSI (GBK)
    editor turns the template's em-dash into `e2 80 3f`, and a strict UTF-8
    read raises.  On the Pi this file is the *preseed* the operator writes on
    their own PC and drops into /boot/firmware/, so it is the likeliest place
    for a foreign encoding to arrive.
    """

    DAMAGED = (b"# mrrc_modern.env \xe2\x80?MRRC Modern launcher "
               b"configuration template.\nMRRC_WEB_PASSWORD=secret\n"
               b"MRRC_RADIO_MODEL=ft710\n")

    def _write(self, tmp, raw):
        path = Path(tmp) / "mrrc.env"
        path.write_bytes(raw)
        return path

    def test_the_field_regression_file_is_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            text, _enc = first_run.read_env_text(self._write(tmp, self.DAMAGED))
        self.assertIn("MRRC_WEB_PASSWORD=secret", text)

    def test_gbk_values_survive(self):
        name = "麦克风 (USB Audio CODEC)"
        raw = ("# 配置\nMRRC_AUDIO_RX_DEVICE=" + name + "\n").encode("cp936")
        with tempfile.TemporaryDirectory() as tmp:
            text, enc = first_run.read_env_text(self._write(tmp, raw))
        self.assertNotEqual(enc, "utf-8")
        self.assertIn(f"MRRC_AUDIO_RX_DEVICE={name}", text)

    def test_utf16_saved_file_is_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = "MRRC_WEB_PORT=8888\r\n".encode("utf-16")
            text, _enc = first_run.read_env_text(self._write(tmp, raw))
        self.assertIn("MRRC_WEB_PORT=8888", text)

    def test_utf8_files_stay_untouched(self):
        raw = "# 中文注释 — em dash\nMRRC_RADIO_MODEL=ic7300\n".encode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            text, enc = first_run.read_env_text(self._write(tmp, raw))
        self.assertEqual(enc, "utf-8")
        self.assertIn("中文注释 — em dash", text)

    def test_update_env_file_heals_a_non_utf8_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, self.DAMAGED)
            first_run.update_env_file(path, {"MRRC_WEB_PORT": "8443"})
            text = path.read_bytes().decode("utf-8")     # must be valid UTF-8
        self.assertIn("MRRC_WEB_PORT=8443", text)
        self.assertIn("MRRC_WEB_PASSWORD=secret", text)
