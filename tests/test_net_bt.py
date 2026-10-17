"""Bluetooth pairing for the remote (AD-028): the settings-page path.

The page exists so a customer never needs a shell.  Two things decide whether
it is honest:

* the **verdict** it shows is "the keys work" (a keyboard input device from
  that address exists), not "bluez said paired" — the field defect was a remote
  that reported ``Paired: yes`` with an unencrypted link and dead keys;
* every bluez call goes through the injectable runner, so none of this needs a
  radio, a controller, or root.
"""

import unittest
from pathlib import Path

import net_bt


def runner_for(stdout="", rc=0, stderr=""):
    calls = []

    def run(argv):
        calls.append(argv)
        return rc, stdout, stderr

    run.calls = calls          # type: ignore[attr-defined]
    return run


SCAN_OUT = """\x1b[0;92m[NEW]\x1b[0m Device 0C:F3:CE:B0:88:75 CMCC_Voice_Remote
\x1b[0;92m[NEW]\x1b[0m Device AA:BB:CC:DD:EE:01 AA-BB-CC-DD-EE-01
\x1b[0;93m[CHG]\x1b[0m Device 0C:F3:CE:B0:88:75 RSSI: -63
\x1b[0;93m[CHG]\x1b[0m Device AA:BB:CC:DD:EE:01 RSSI: -77
[NEW] Device 0C:F3:CE:B0:88:75 CMCC_Voice_Remote
[NEW] Device 11:22:33:44:55:66 11-22-33-44-55-66
"""

INFO_OUT = """Device 0C:F3:CE:B0:88:75 (public)
\tName: CMCC_Voice_Remote
\tAlias: CMCC_Voice_Remote
\tPaired: yes
\tBonded: yes
\tTrusted: yes
\tConnected: no
"""


class ScanTests(unittest.TestCase):
    def test_parses_named_devices_deduped_and_strips_colour(self):
        devices = net_bt.parse_devices(SCAN_OUT)
        self.assertEqual([d["address"] for d in devices],
                         ["0C:F3:CE:B0:88:75", "AA:BB:CC:DD:EE:01"])
        self.assertEqual(devices[0]["name"], "CMCC_Voice_Remote")
        self.assertEqual(devices[1]["name"], "")     # bluez prints a dashed echo
        self.assertEqual(devices[0]["rssi"], -63)
        self.assertEqual(devices[1]["rssi"], -77)

    def test_cache_echoes_are_dropped(self):
        """既没名字也没信号强度的条目是上一次扫描的回声，不是"现在就在旁边"。

        `bluetoothctl` 会重放 bluez 的设备缓存 —— 现场就是这样把一台遥控器淹在
        二十几个无名 LE 信标里的（2026-10-17）。
        """
        addresses = [d["address"] for d in net_bt.parse_devices(SCAN_OUT)]
        self.assertNotIn("11:22:33:44:55:66", addresses)

    def test_named_devices_come_first_then_strongest(self):
        devices = net_bt.parse_devices(SCAN_OUT + "\n[CHG] Device 11:22:33:44:55:66 RSSI: -40\n")
        self.assertEqual(devices[0]["address"], "0C:F3:CE:B0:88:75")
        self.assertEqual(devices[1]["address"], "11:22:33:44:55:66")   # -40 强于 -77，但无名
        self.assertEqual(devices[2]["address"], "AA:BB:CC:DD:EE:01")

    def test_the_fallback_scan_is_a_bredr_inquiry(self):
        """``btmgmt`` 缺席时退回 bluetoothctl —— 那条路仍然只查经典蓝牙。"""
        run = runner_for(SCAN_OUT)
        net_bt.scan(seconds=3, runner=run)
        fallback = [c for c in run.calls if c[0] == "bluetoothctl"][0]
        self.assertEqual(fallback[:3], ["bluetoothctl", "--timeout", "3"])
        self.assertIn("bredr", fallback)

    def test_scan_failure_yields_no_devices(self):
        self.assertEqual(net_bt.scan(runner=runner_for("", rc=1, stderr="no controller")), [])


BTMGMT_OUT = """hci0 type 6 discovering on
Discovery started
hci0 dev_found: 6D:C8:F2:5E:08:C9 type LE Random rssi -51 flags 0x0004 
AD flags 0x1a 
eir_len 17
hci0 dev_found: AA:BB:CC:DD:EE:01 type BR/EDR rssi -88 flags 0x0000 
AD flags 0x06 
hci0 dev_found: 0C:F3:CE:B0:88:75 type BR/EDR rssi -42 flags 0x0000 
AD flags 0x06 
name CMCC_Voice_Remote
hci0 dev_found: 0C:F3:CE:B0:88:75 type BR/EDR rssi -55 flags 0x0000 
AD flags 0x06 
name CMCC_Voice_Remote
hci0 dev_found: CC:DA:B5:40:14:4A type LE Public rssi -79 flags 0x0000 
AD flags 0x06 
name GWMVA72TJ061874
"""


class SignalStrengthTests(unittest.TestCase):
    """操作员要在二十几个设备里认出手里那台遥控器 —— 信号强度是唯一的线索。

    实测（2026-10-17，W103D）：BR/EDR 扫出 **23** 个设备，页面只给地址和名字，
    没有信号强度也没有排序，操作员无法判断该点哪一个。
    """

    def test_rssi_and_type_are_parsed(self):
        devices = net_bt.parse_btmgmt(BTMGMT_OUT)
        remote = [d for d in devices if d["address"] == "0C:F3:CE:B0:88:75"][0]
        self.assertEqual(remote["rssi"], -42)          # 最强的一次
        self.assertEqual(remote["type"], "BR/EDR")
        self.assertEqual(remote["name"], "CMCC_Voice_Remote")

    def test_duplicates_keep_the_strongest_sighting(self):
        remote = [d for d in net_bt.parse_btmgmt(BTMGMT_OUT)
                  if d["address"] == "0C:F3:CE:B0:88:75"]
        self.assertEqual(len(remote), 1)

    def test_the_list_is_ordered_strongest_first(self):
        rssis = [d["rssi"] for d in net_bt.parse_btmgmt(BTMGMT_OUT)]
        self.assertEqual(rssis, sorted(rssis, reverse=True))
        self.assertEqual(net_bt.parse_btmgmt(BTMGMT_OUT)[0]["address"],
                         "0C:F3:CE:B0:88:75")

    def test_scan_prefers_btmgmt_and_falls_back_to_bluetoothctl(self):
        run = runner_for(BTMGMT_OUT)
        net_bt.scan(seconds=3, runner=run)
        self.assertEqual(run.calls[0][:2], ["timeout", "3"])
        self.assertIn("btmgmt", run.calls[0])

        fell_back = runner_for(SCAN_OUT, rc=0)
        devices = net_bt.scan(seconds=3, runner=fell_back)
        self.assertEqual(fell_back.calls[0][:2], ["timeout", "3"])
        self.assertEqual(fell_back.calls[1][:2], ["bluetoothctl", "--timeout"])
        self.assertEqual(devices[0]["rssi"], -63,
                         "bluetoothctl 自己会发 [CHG] … RSSI: -63，这条路也得把它带出来")
        self.assertEqual(devices[0]["type"], "", "bluetoothctl 不给地址类型")

    def test_the_panel_shows_signal_strength(self):
        page = (Path(__file__).resolve().parents[1] / "static" / "settings.html").read_text(
            encoding="utf-8")
        self.assertIn("dBm", page)
        self.assertIn("cand.rssi", page)


class IconOrderingTests(unittest.TestCase):
    """列表顺序要回答「哪一个是遥控器」：bluez 知道的那台是键盘（input-*）。"""

    def test_input_devices_come_first(self):
        scan_out = ("[NEW] Device 11:22:33:44:55:66 Beacon\n"
                    "[CHG] Device 11:22:33:44:55:66 RSSI: -40\n"
                    "[NEW] Device 0C:F3:CE:B0:88:75 CMCC_Voice_Remote\n")
        devices_out = ("Device 11:22:33:44:55:66 Beacon\n"
                       "Device 0C:F3:CE:B0:88:75 CMCC_Voice_Remote\n")

        def run(argv):
            if argv[0] == "timeout":
                return 1, "", "permission denied"
            if argv[1:3] == ["--timeout", "3"]:
                return 0, scan_out, ""
            if argv[1:] == ["devices"]:
                return 0, devices_out, ""
            if argv[:2] == ["bluetoothctl", "info"]:
                icon = ("\tIcon: input-keyboard\n"
                        if argv[2] == "0C:F3:CE:B0:88:75" else "")
                return 0, f"Device {argv[2]}\n{icon}", ""
            return 1, "", "?"

        devices = net_bt.scan(seconds=3, runner=run)
        self.assertEqual(devices[0]["address"], "0C:F3:CE:B0:88:75",
                         "遥控器（input-keyboard）必须排在更近的信标前面")
        self.assertEqual(devices[0]["icon"], "input-keyboard")


class BoldWrappedOutputTests(unittest.TestCase):
    """bluetoothctl 给每个彩色 token 套了 \x01/\x02（粗体/复位）字节。

    实测（2026-10-17，盒子 .217）：输出里有 18 行 RSSI，解析出来 0 个 —— 因为
    剥掉 ANSI 之后剩 `[\x01\x02CHG\x01\x02]`，`^\[(NEW|CHG|DEL)\]` 全部匹配失败。
    """

    REAL = ("[\x01\x1b[0;93m\x02CHG\x01\x1b[0m\x02] Device CC:DA:B5:40:14:4A RSSI: -87\n"
            "[\x01\x1b[0;92m\x02NEW\x01\x1b[0m\x02] Device 0C:F3:CE:B0:88:75 CMCC_Voice_Remote\n")

    def test_bold_bytes_do_not_hide_the_tag(self):
        devices = {d["address"]: d for d in net_bt.parse_devices(self.REAL)}
        self.assertEqual(devices["CC:DA:B5:40:14:4A"]["rssi"], -87)
        self.assertEqual(devices["0C:F3:CE:B0:88:75"]["name"], "CMCC_Voice_Remote")


class InfoTests(unittest.TestCase):
    def test_flags_are_booleans(self):
        info = net_bt.parse_info(INFO_OUT)
        self.assertEqual(info["name"], "CMCC_Voice_Remote")
        self.assertTrue(info["paired"])
        self.assertTrue(info["bonded"])
        self.assertFalse(info["connected"])


class PairTests(unittest.TestCase):
    def test_pair_then_trust_then_read_back(self):
        run = runner_for(INFO_OUT)
        info = net_bt.pair("0C:F3:CE:B0:88:75", runner=run)
        verbs = [call[3] if call[1] == "--timeout" else call[1] for call in run.calls]
        self.assertEqual(verbs, ["pair", "trust", "info"])
        self.assertTrue(info["paired"])

    def test_forget_power_cycles_the_controller(self):
        run = runner_for()
        net_bt.power_cycle(runner=run)
        self.assertEqual([call[2] for call in run.calls], ["off", "on"])


class PairErrorTests(unittest.TestCase):
    """配对失败要带上 bluez 的原话 —— 「没配上」本身没法行动。"""

    def test_the_reason_reaches_the_caller(self):
        def run(argv):
            if argv[0] == "bluetoothctl" and argv[1:3] == ["--timeout", "25"]:
                return 1, "Failed to pair: org.bluez.Error.AuthenticationFailed\n", ""
            return 0, "", ""

        state = net_bt.pair("0C:F3:CE:B0:88:75", runner=run)
        self.assertEqual(state["error"], "Failed to pair: org.bluez.Error.AuthenticationFailed")

    def test_not_available_is_explained(self):
        self.assertEqual(net_bt.pair_error("Device 0C:F3:CE:B0:88:75 not available"),
                         "Device 0C:F3:CE:B0:88:75 not available")

    def test_a_successful_pair_has_no_error(self):
        def run(argv):
            if argv[0] == "bluetoothctl" and argv[1:3] == ["--timeout", "25"]:
                return 0, "Pairing successful\n", ""
            if argv[0] == "bluetoothctl" and argv[1] == "info":
                return 0, INFO_OUT, ""
            return 0, "", ""

        state = net_bt.pair("0C:F3:CE:B0:88:75", runner=run)
        self.assertTrue(state["paired"])
        self.assertEqual(state["error"], "")


class VerdictTests(unittest.TestCase):
    """The judgement the page acts on: do the keys arrive?"""

    def _proc(self, tmp, block):
        path = Path(tmp) / "devices"
        path.write_text(block, encoding="utf-8")
        return str(path)

    def test_keyboard_device_from_the_remote_counts(self):
        import tempfile
        block = ('I: Bus=0005 Vendor=0416 Product=0300 Version=0001\n'
                 'U: Uniq=0c:f3:ce:b0:88:75\n'
                 'N: Name="CMCC_Voice_Remote Keyboard"\n'
                 'H: Handlers=sysrq kbd event3 leds\n\n')
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(net_bt.keyboard_device_present("0C:F3:CE:B0:88:75", self._proc(tmp, block)))

    def test_paired_but_no_device_is_the_failed_case(self):
        import tempfile
        block = ('I: Bus=0003 Vendor=0D8C Product=0013\n'
                 'N: Name="USB Audio Device"\n'
                 'H: Handlers=kbd event2\n\n')
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(net_bt.keyboard_device_present("0C:F3:CE:B0:88:75", self._proc(tmp, block)))

    def test_state_combines_flags_and_verdict(self):
        state = net_bt.state("0C:F3:CE:B0:88:75", runner=runner_for(INFO_OUT))
        self.assertTrue(state["paired"])
        self.assertIn("keys_work", state)
        self.assertIsInstance(state["keys_work"], bool)
        self.assertEqual(set(state), {"address", "name", "paired", "bonded",
                                      "connected", "trusted", "keys_work"})


class SettingsPageTests(unittest.TestCase):
    """The page is the whole point: no SSH for the customer."""

    def setUp(self):
        self.html = (Path(__file__).resolve().parent.parent
                     / "static" / "settings.html").read_text(encoding="utf-8")

    def test_the_panel_and_its_buttons_exist(self):
        for hook in ("bt-scan", "bt-list", "bt-msg"):
            self.assertIn(f'id="{hook}"', self.html)
        self.assertIn("/api/box/bt/scan", self.html)
        self.assertIn("/api/box/bt/pair", self.html)
        self.assertIn("/api/box/bt/forget", self.html)

    def test_it_shows_the_verdict_not_just_paired(self):
        self.assertIn("keys_work", self.html)

    def test_the_page_still_avoids_innerhtml(self):
        self.assertNotIn("innerHTML", self.html)

    def test_it_pairs_a_single_broadcasting_remote_without_a_second_click(self):
        """只发现一台正在广播的遥控器时直接配。

        现场（2026-10-17）：扫描要 12 秒、配对要 25 秒，操作员还要在二十几个条目里
        挑一台 —— 而他手里那台正贴着盒子、灯在快闪。
        """
        self.assertIn("looksLikeRemote", self.html)
        self.assertIn("pairNow", self.html)
        self.assertIn("candidates.length === 1", self.html)
        # 判据是「bluez 说是输入设备」+「这次扫描有信号强度」，不是随便一台有名字的
        self.assertIn("indexOf('input-') === 0", self.html)
        self.assertIn("typeof cand.rssi === 'number'", self.html)


if __name__ == "__main__":
    unittest.main()
