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
                                      _P("/dev/cu.Bluetooth-Incoming-Port", "Bluetooth")]):
            result = server._list_devices()
        self.assertEqual([p["device"] for p in result["serial_ports"]], ["/dev/cu.usbserial-A1"])


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


class DualStackDefaultTests(unittest.TestCase):
    def test_plain_dualsock_accepts_ipv4_and_ipv6(self):
        # Guards the MRRC_WEB_HOST=:: default: uvicorn binds a plain AF_INET6
        # :: socket (no IPV6_V6ONLY set), which on macOS accepts both stacks.
        import socket as _socket
        sock = _socket.socket(_socket.AF_INET6)
        sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        sock.bind(("::", 0))
        sock.listen(2048)
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


if __name__ == "__main__":
    unittest.main()
