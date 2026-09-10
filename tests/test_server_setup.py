"""Tests for the web connection-settings endpoints (devices/setup/restart)."""
import os
import unittest
from unittest import mock
from pathlib import Path

import server
from macos import first_run


class ListDevicesTests(unittest.TestCase):
    def test_shape_with_no_devices(self):
        with mock.patch("serial.tools.list_ports.comports", return_value=[]), \
             mock.patch("pyaudio.PyAudio", side_effect=Exception("no pyaudio")):
            result = server._list_devices()
        self.assertEqual(result["serial_ports"], [])
        self.assertEqual(result["audio_rx"], [])
        self.assertEqual(result["audio_tx"], [])

    def test_filters_non_cu_ports(self):
        class _P:
            def __init__(self, device, description):
                self.device = device
                self.description = description
        with mock.patch("serial.tools.list_ports.comports",
                        return_value=[_P("/dev/cu.usbserial-A1", "USB Serial"),
                                      _P("/dev/tty.Bluetooth", ""),
                                      _P("/dev/cu.Bluetooth-Incoming-Port", "Bluetooth")]), \
             mock.patch("sys.platform", "darwin"):
            result = server._list_devices()
        self.assertEqual([p["device"] for p in result["serial_ports"]], ["/dev/cu.usbserial-A1"])

    def test_windows_lists_com_ports(self):
        class _P:
            def __init__(self, device, description=""):
                self.device = device
                self.description = description
        with mock.patch("serial.tools.list_ports.comports",
                        return_value=[_P("COM3", "CP210x USB to UART Bridge"),
                                      _P("COM1", "Communications Port")]), \
             mock.patch("sys.platform", "win32"):
            result = server._list_devices()
        self.assertEqual([p["device"] for p in result["serial_ports"]], ["COM3", "COM1"])


class ConfigFilePathTests(unittest.TestCase):
    def test_uses_mrrc_config_file_env(self):
        with mock.patch.dict(os.environ, {"MRRC_CONFIG_FILE": "/tmp/x/mrrc_modern.env"}, clear=False):
            self.assertEqual(server._config_file_path(), Path("/tmp/x/mrrc_modern.env"))

    def test_falls_back_to_mem_file_parent(self):
        with mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch.object(server, "MEM_FILE", Path("/tmp/data/mem_channels.json")):
            self.assertEqual(server._config_file_path(), Path("/tmp/data/mrrc_modern.env"))


class ScheduleRestartTests(unittest.TestCase):
    def test_schedules_exit_with_code(self):
        with mock.patch("threading.Timer") as timer:
            server._schedule_restart()
        timer.assert_called_once()
        args, _ = timer.call_args
        self.assertEqual(args[0], 1.2)
        fn = args[1]
        with mock.patch("os._exit") as exit_mock:
            fn()
        exit_mock.assert_called_once_with(42)


class DualStackSocketTests(unittest.TestCase):
    def test_dual_stack_accepts_ipv4_and_ipv6(self):
        import socket as _socket
        sock = server._bind_dual_stack_socket(0)
        self.addCleanup(sock.close)
        port = sock.getsockname()[1]

        def _try(target):
            try:
                c = _socket.create_connection(target, timeout=1)
                c.close()
                return True
            except Exception:
                return False

        self.assertTrue(_try(("127.0.0.1", port)), "IPv4 connect failed on :: socket")
        self.assertTrue(_try(("::1", port)), "IPv6 connect failed on :: socket")


class SetupSaveBaudLinkageTests(unittest.TestCase):
    """V2.33: the connection dialog has no baud field; saving a model
    must align MRRC_BAUD_RATE with it. A stale 38400 from the legacy
    installer template silently broke the IC-7300 CI-V scope stream
    (requires 115200) even after the user switched the model in the UI
    (field log 2026-09-10: ic7300 @ 38400 → scope stalled → S-meter
    fallback)."""

    class _FakeRequest:
        def __init__(self, body):
            self._body = body

        async def json(self):
            return self._body

    def _save(self, body):
        import asyncio
        with mock.patch.object(server, "_verify_auth", return_value=True), \
             mock.patch.object(server, "_schedule_restart"), \
             mock.patch.object(server.first_run, "update_env_file") as uf:
            resp = asyncio.run(server.api_setup_save(self._FakeRequest(body)))
        return resp, uf

    def test_ic7300_aligns_baud_to_115200(self):
        resp, uf = self._save({"radio_model": "ic7300", "serial_port": "COM6"})
        self.assertEqual(resp.status_code, 200)
        updates = uf.call_args[0][1]
        self.assertEqual(updates["MRRC_BAUD_RATE"], "115200")

    def test_ft710_aligns_baud_to_38400(self):
        resp, uf = self._save({"radio_model": "ft710", "serial_port": "COM6"})
        self.assertEqual(resp.status_code, 200)
        updates = uf.call_args[0][1]
        self.assertEqual(updates["MRRC_BAUD_RATE"], "38400")

    def test_mk2_aligns_baud_to_115200(self):
        resp, uf = self._save({"radio_model": "ic7300mk2", "serial_port": "COM6"})
        self.assertEqual(resp.status_code, 200)
        updates = uf.call_args[0][1]
        self.assertEqual(updates["MRRC_BAUD_RATE"], "115200")


if __name__ == "__main__":
    unittest.main()
