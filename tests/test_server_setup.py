"""Tests for the web connection-settings endpoints (devices/setup/restart)."""
import os
import tempfile
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

    def test_releases_ptt_before_exit_when_transmitting(self):
        """os._exit skips the lifespan Force-RX, so the restart path must
        drop the carrier itself — a keyed restart used to leave the radio
        transmitting with no process left to send TX0."""
        class _Cat:
            connected = True

            async def set_ptt(self, tx):
                pass

        class _Radio:
            is_transmitting = True

        made = []

        def fake_run_coro(coro, loop):
            made.append(coro)
            coro.close()

            class _F:
                def result(self, timeout=None):
                    return None

            return _F()

        with mock.patch.object(server, "cat", _Cat()), \
             mock.patch.object(server, "radio", _Radio()), \
             mock.patch("asyncio.get_running_loop", return_value=object()), \
             mock.patch("asyncio.run_coroutine_threadsafe",
                        side_effect=fake_run_coro), \
             mock.patch("threading.Timer") as timer:
            server._schedule_restart()
            fn = timer.call_args[0][1]
            with mock.patch("os._exit") as exit_mock:
                fn()
        self.assertEqual(len(made), 1, "restart must release PTT before exiting")
        exit_mock.assert_called_once_with(42)

    def test_does_not_release_when_not_transmitting(self):
        class _Cat:
            connected = True

        class _Radio:
            is_transmitting = False

        with mock.patch.object(server, "cat", _Cat()), \
             mock.patch.object(server, "radio", _Radio()), \
             mock.patch("asyncio.get_running_loop", return_value=object()), \
             mock.patch("asyncio.run_coroutine_threadsafe") as rct, \
             mock.patch("threading.Timer") as timer:
            server._schedule_restart()
            fn = timer.call_args[0][1]
            with mock.patch("os._exit") as exit_mock:
                fn()
        rct.assert_not_called()
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
        """保存到临时 env 文件，再读回来断言（写入器是真的，不是 mock）。"""
        import asyncio
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env_file = Path(tmp.name) / "mrrc.env"
        with mock.patch.object(server, "_verify_auth", return_value=True), \
             mock.patch.object(server, "_schedule_restart"), \
             mock.patch.object(server, "_config_file_path", return_value=env_file):
            resp = asyncio.run(server.api_setup_save(self._FakeRequest(body)))
        return resp, env_file

    def _saved_baud(self, body) -> str:
        import env_store
        resp, env_file = self._save(body)
        self.assertEqual(resp.status_code, 200)
        return env_store.load(env_file)["MRRC_BAUD_RATE"], env_store.load(env_file)

    def test_ic7300_aligns_baud_to_115200(self):
        baud, _file = self._saved_baud({"radio_model": "ic7300", "serial_port": "COM6"})
        self.assertEqual(baud, "115200")

    def test_ft710_aligns_baud_to_38400(self):
        baud, _file = self._saved_baud({"radio_model": "ft710", "serial_port": "COM6"})
        self.assertEqual(baud, "38400")

    def test_mk2_aligns_baud_to_115200(self):
        baud, _file = self._saved_baud({"radio_model": "ic7300mk2", "serial_port": "COM6"})
        self.assertEqual(baud, "115200")



class EmptySerialPortDiagnosticsTests(unittest.TestCase):
    """空端口必须在启动日志里点名 —— 现场那句话里端口名是空的。

    2026-10-26 包 20261026-194408-16d0：`Failed to connect to : [Errno 2] could not
    open port : ...: ''`，读起来像日志缺字，不像「配置里没有端口」。
    """

    def test_it_names_the_variable_and_the_file(self):
        with mock.patch.object(server, "SERIAL_PORT", ""), \
             mock.patch.object(server, "_config_file_path",
                               return_value=Path("/opt/mrrc_modern/env/mrrc.env")):
            message = server._serial_port_problem()
        self.assertIn("MRRC_SERIAL_PORT", message)
        # 断言的是「把配置文件路径说出来」，不是分隔符：Windows 上 str(Path(...))
        # 是反斜杠（2026-10-29 的 Windows 构建第一次跑到这条）。
        self.assertIn("opt/mrrc_modern/env/mrrc.env", message.replace("\\", "/"))

    def test_is_silent_when_a_port_is_configured(self):
        with mock.patch.object(server, "SERIAL_PORT", "/dev/ttyACM0"):
            self.assertEqual(server._serial_port_problem(), "")


class SetupSaveReadBackTests(unittest.TestCase):
    """保存之后必须**回读**：生效值不是写进去的值就是失败，不能报 ok。

    2026-10-26 现场（包 20261026-194408-16d0）：`Saving connection settings:
    model=ic7300 port=/dev/ttyUSB0` 连续出现 6 次，每一次之后重启都还是
    `Opening CI-V port  at 115200 baud`（空）——因为 env 文件里 `MRRC_SERIAL_PORT`
    有一条后置的空行，而写入器只改第一个匹配行。界面拿到 `{"ok": true}`、
    用户以为存上了，实际上 CI-V 从来没有端口可开。
    """

    class _FakeRequest:
        def __init__(self, body):
            self._body = body

        async def json(self):
            return self._body

    def _save(self, body):
        import asyncio
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env_file = Path(tmp.name) / "mrrc.env"
        with mock.patch.object(server, "_verify_auth", return_value=True), \
             mock.patch.object(server, "_schedule_restart") as restart, \
             mock.patch.object(server, "_config_file_path", return_value=env_file):
            resp = asyncio.run(server.api_setup_save(self._FakeRequest(body)))
        return resp, env_file, restart

    def test_a_damaged_file_is_repaired_and_the_save_reports_ok(self):
        import env_store
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "mrrc.env"
            env_file.write_text("MRRC_SERIAL_PORT=\nMRRC_RADIO_MODEL=ft710\n",
                                encoding="utf-8")
            with mock.patch.object(server, "_config_file_path", return_value=env_file):
                resp, path, restart = self._save({"radio_model": "ic7300",
                                                  "serial_port": "/dev/ttyACM0"})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(env_store.load(path)["MRRC_SERIAL_PORT"], "/dev/ttyACM0")
            restart.assert_called_once()

    def test_the_value_the_readers_will_see_is_the_one_that_was_saved(self):
        import env_store

        # 现场那份形状：真值在前、空值在后（后者胜）
        resp, path, _restart = self._save({"radio_model": "ic7300",
                                           "serial_port": "/dev/ttyACM0"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(env_store.load(path)["MRRC_SERIAL_PORT"], "/dev/ttyACM0")

    def test_a_save_that_could_not_take_is_a_500_not_an_ok(self):
        """写不进去就别报成功：回读与写入不一时给 500 且不重启。"""
        with mock.patch.object(server.first_run, "update_env_file",
                               return_value=None):
            resp, _path, restart = self._save({"radio_model": "ic7300",
                                               "serial_port": "/dev/ttyACM0"})
        self.assertEqual(resp.status_code, 500)
        restart.assert_not_called()


class SetupSaveG7250LanIpTests(unittest.TestCase):
    """The G7250's real spectrum comes from its LAN IQ stream, and the only
    way to set the address used to be hand-editing the env file.

    Field report 2026-11-11 (bundle 20261111-212640-e397): radio on the LAN,
    cable plugged in, `scope_type: "none"`, `Spectrum broadcast active:
    S-meter fallback` — the dialog had no field for it, the guide's "写一行
    配置" was the only path, and the operator read the synthetic curve as
    "瀑布图很卡".
    """

    class _FakeRequest:
        def __init__(self, body):
            self._body = body

        async def json(self):
            return self._body

    def _save(self, body, env_file):
        import asyncio
        with mock.patch.object(server, "_verify_auth", return_value=True), \
             mock.patch.object(server, "_schedule_restart") as restart, \
             mock.patch.object(server, "_config_file_path", return_value=env_file):
            resp = asyncio.run(server.api_setup_save(self._FakeRequest(body)))
        return resp, restart

    def _tmp_env(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name) / "mrrc.env"

    def test_g7250_saves_the_lan_iq_address(self):
        import env_store
        env_file = self._tmp_env()
        resp, restart = self._save({"radio_model": "g7250",
                                    "serial_port": "COM1",
                                    "g7250_lan_ip": "192.168.110.199"}, env_file)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(env_store.load(env_file)["MRRC_G7250_LAN_IP"],
                         "192.168.110.199")
        restart.assert_called_once()

    def test_an_empty_address_clears_it_back_to_the_synthetic_waterfall(self):
        import env_store
        env_file = self._tmp_env()
        self._save({"radio_model": "g7250", "serial_port": "COM1",
                    "g7250_lan_ip": "192.168.110.199"}, env_file)
        resp, _restart = self._save({"radio_model": "g7250", "serial_port": "COM1",
                                     "g7250_lan_ip": ""}, env_file)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(env_store.load(env_file).get("MRRC_G7250_LAN_IP", ""), "")

    def test_a_pasted_url_is_refused_instead_of_stored(self):
        # The address goes straight into a UDP sendto(); "http://…" would fail
        # at runtime with an error nobody sees, long after {"ok": true}.
        env_file = self._tmp_env()
        for bad in ("http://192.168.110.199", "192.168.110.199/", "192.168.1 .199"):
            resp, restart = self._save({"radio_model": "g7250",
                                        "serial_port": "COM1",
                                        "g7250_lan_ip": bad}, env_file)
            self.assertEqual(resp.status_code, 400, bad)
            restart.assert_not_called()

    def test_another_model_does_not_wipe_the_stored_address(self):
        import env_store
        env_file = self._tmp_env()
        self._save({"radio_model": "g7250", "serial_port": "COM1",
                    "g7250_lan_ip": "192.168.110.199"}, env_file)
        resp, _restart = self._save({"radio_model": "ft710",
                                     "serial_port": "COM3"}, env_file)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(env_store.load(env_file)["MRRC_G7250_LAN_IP"],
                         "192.168.110.199")

    def test_setup_status_exposes_the_address_for_the_dialog(self):
        with mock.patch.object(server, "G7250_LAN_IP", "10.0.0.9"):
            self.assertEqual(server._setup_status()["g7250_lan_ip"], "10.0.0.9")


class ListDevicesLoopbackFlagTests(unittest.TestCase):
    """The picker must be able to label a loopback endpoint: the 2026-11-11
    bundle chose one from this list and lost all RX audio."""

    def test_audio_entries_carry_a_loopback_flag(self):
        class _Pa:
            _devices = [
                {"name": "麦克风 (USB Audio Device)", "maxInputChannels": 1,
                 "maxOutputChannels": 0, "hostApi": 0},
                {"name": "立体声混音 (Realtek HD Audio Stereo input)",
                 "maxInputChannels": 2, "maxOutputChannels": 0, "hostApi": 0},
            ]

            def get_device_count(self):
                return len(self._devices)

            def get_device_info_by_index(self, i):
                return self._devices[i]

            def get_host_api_info_by_index(self, i):
                return {"name": "MME"}

            def terminate(self):
                pass

        with mock.patch("pyaudio.PyAudio", lambda: _Pa()):
            devices = server._list_audio_devices()
        flags = {d["name"]: d["loopback"] for d in devices["rx"]}
        self.assertFalse(flags["麦克风 (USB Audio Device)"])
        self.assertTrue(flags["立体声混音 (Realtek HD Audio Stereo input)"])


class ConnectionDialogG7250FieldTests(unittest.TestCase):
    """Source assertions over the dialog: the address must be editable where
    the radio is chosen, and the module must actually send it."""

    def _sources(self):
        root = Path(__file__).resolve().parents[1]
        return ((root / "static/index.html").read_text(encoding="utf-8"),
                (root / "static/modules/connection_settings.js").read_text(encoding="utf-8"))

    def test_the_field_is_in_the_dialog_and_hidden_by_default(self):
        html, _js = self._sources()
        self.assertIn('id="cs-g7250-lan-ip"', html)
        block = html.split('id="cs-g7250-fields"', 1)[1].split(">", 1)[0]
        self.assertIn("hidden", block)

    def test_the_module_sends_and_refills_the_address(self):
        _html, js = self._sources()
        self.assertIn("g7250_lan_ip", js)
        self.assertIn('g7250 !== "g7250"'.replace("g7250 !== ", 'model !== '), js)
        # Only the G7250 branch: every other model must keep its stored value
        # (server side) and not send a bogus key.
        self.assertIn('descriptor(dlg).transport !== "network"', js)

    def test_loopback_devices_are_labelled_in_the_picker(self):
        _html, js = self._sources()
        self.assertIn("it.loopback", js)
        self.assertIn("电脑自带，非电台", js)


if __name__ == "__main__":
    unittest.main()
