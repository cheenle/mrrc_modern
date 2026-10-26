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
from unittest import mock

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
        self.assertEqual(run.calls[0][0], "timeout")   # 每半轮至少 4 秒
        self.assertIn("btmgmt", run.calls[0])

        fell_back = runner_for(SCAN_OUT, rc=0)
        devices = net_bt.scan(seconds=3, runner=fell_back)
        self.assertEqual(fell_back.calls[0][0], "timeout")
        bluetoothctl_calls = [c for c in fell_back.calls if c[0] == "bluetoothctl"]
        self.assertEqual(bluetoothctl_calls[0][:2], ["bluetoothctl", "--timeout"])
        self.assertEqual(devices[0]["rssi"], -63,
                         "bluetoothctl 自己会发 [CHG] … RSSI: -63，这条路也得把它带出来")
        self.assertEqual(devices[0]["type"], "", "bluetoothctl 不给地址类型")

    def test_the_rich_scan_asks_for_both_radio_kinds(self):
        """``btmgmt find -l`` 是 **LE only**，不是"全部"。

        实测（2026-10-17，盒子 .217）：`find -l` 一轮 58 个 LE、**0 个 BR/EDR**；
        `find -b` 才看得见经典蓝牙设备。遥控器是经典蓝牙，只传 `-l` 等于永远扫不到它。
        """
        run = runner_for(BTMGMT_OUT)
        net_bt.scan(seconds=3, runner=run)
        flags = [c[-1] for c in run.calls if c[0] == "timeout"]
        self.assertEqual(flags[:2], ["-b", "-l"],
                         "`find` 的 -b/-l 互斥，必须各扫一轮（只扫一种就会漏掉另一类遥控器）")

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


class NameMatchingTests(unittest.TestCase):
    """按**名字**认遥控器，不按 MAC。

    来料批次会换地址，而"这台遥控器叫什么"是能一眼看见的事实；名字对上几台就配几台
    （多配一台的代价远小于"这台不认"）。`MRRC_BT_REMOTE_NAME` 可改，逗号分隔。
    """

    def test_the_default_is_the_measured_remote(self):
        self.assertIn("CMCC_Voice_Remote", net_bt.remote_name_patterns())

    def test_matching_is_case_insensitive_and_substring(self):
        self.assertTrue(net_bt.matches_remote("CMCC_Voice_Remote"))
        self.assertTrue(net_bt.matches_remote("cmcc_voice_remote"))
        self.assertTrue(net_bt.matches_remote("CMCC_Voice_Remote (2)"))
        self.assertFalse(net_bt.matches_remote("CASL 39982"))
        self.assertFalse(net_bt.matches_remote(""))

    def test_the_pattern_list_can_be_overridden(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"MRRC_BT_REMOTE_NAME": "Foo, Bar "}):
            self.assertEqual(net_bt.remote_name_patterns(), ("Foo", "Bar"))
            self.assertTrue(net_bt.matches_remote("the-bar-remote"))
            self.assertFalse(net_bt.matches_remote("CMCC_Voice_Remote"))

    def test_the_panel_pairs_every_name_match(self):
        page = (Path(__file__).resolve().parents[1] / "static" / "settings.html").read_text(
            encoding="utf-8")
        self.assertIn("remoteNames", page)
        self.assertIn("pairInTurn", page)
        self.assertIn("nameMatches", page)


class PairScriptTests(unittest.TestCase):
    """配对必须走交互式会话（pty）——这条是现场一步步试出来的。

    一次性 `bluetoothctl --timeout 25 pair` 不注册 agent，bluez 发出的
    Pairing Request 里 bonding 位是 0（`0x28`），BLE HID 遥控器直接回
    SMP `Invalid parameters`（0x0a）；同样的命令打进 pty 就是 `0x29`，
    `Pairing successful`，随后 bluez 建出 `CMCC_Voice_Remote Keyboard`。
    """

    def test_the_script_scans_then_pairs_then_trusts(self):
        lines = [line for line, _ in net_bt.pair_script("0C:F3:CE:B0:88:75", 25)]
        self.assertEqual(lines[0], "power on")
        self.assertIn("scan on", lines)
        self.assertLess(lines.index("scan on"), lines.index("pair 0C:F3:CE:B0:88:75"),
                        "要先发现，才轮得到配对")
        self.assertIn("trust 0C:F3:CE:B0:88:75", lines)
        self.assertEqual(lines[-1], "quit")

    def test_the_pair_wait_is_bounded_but_not_zero(self):
        waits = dict(net_bt.pair_script("X", 25))
        self.assertGreater(waits["pair X"], 0)
        self.assertLessEqual(waits["pair X"], 60)


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



class PairRetryTests(unittest.TestCase):
    """配对不再以「等广播」为前置条件。

    2026-10-19 实测：这台遥控器的广播**没有 RSSI**（只出 `[NEW] Device … CMCC_Voice_Remote`，
    别的设备才有 `RSSI:` 行）。所以按 RSSI 判「在不在广播」会永远判成不在——操作员
    把按键按烂也走不到配对。改成直接配、失败隔几秒重试。
    """

    def _run(self, outcomes, paired_when=None, keys=False, cure=True, attempts=3,
             restart_ok=True, keys_after_restart=False):
        """outcomes: 每次 `_pty_session` 的输出；paired_when: 第几次之后 info 报 paired。"""
        calls = {"pty": 0, "remove": 0, "cycle": 0, "restart": 0}
        seq = list(outcomes)
        keys_now = {"value": keys}

        def fake_pty(lines, argv=None):
            calls["pty"] += 1
            return seq[min(calls["pty"] - 1, len(seq) - 1)]

        def fake_info(address, runner=None):
            done = paired_when if paired_when is not None else 10 ** 6
            paired = calls["pty"] >= done
            return {"paired": paired, "bonded": paired, "trusted": True}

        def fake_restart(runner=None):
            calls["restart"] += 1
            if restart_ok and keys_after_restart:
                keys_now["value"] = True
            return restart_ok

        with mock.patch.object(net_bt, "_pty_session", fake_pty), \
             mock.patch.object(net_bt, "info", fake_info), \
             mock.patch.object(net_bt, "restart_bluetoothd", fake_restart), \
             mock.patch.object(net_bt, "remove",
                               lambda runner=None: calls.__setitem__("remove", calls["remove"] + 1)), \
             mock.patch.object(net_bt, "power_cycle",
                               lambda runner=None: calls.__setitem__("cycle", calls["cycle"] + 1)):
            state = net_bt.pair("0C:F3:CE:B0:88:75", keyboard=lambda a: keys_now["value"],
                                cure=cure, attempts=attempts, retry_delay=0)
        return state, calls

    def test_retries_while_the_remote_is_unavailable(self):
        state, calls = self._run(["Device 0C:F3:CE:B0:88:75 not available"] * 2
                                 + ["Pairing successful"], paired_when=3, keys=True)
        self.assertEqual(calls["pty"], 3)          # 前两次失败后重试，第三次成功
        self.assertEqual(state["error"], "")

    def test_gives_up_with_an_actionable_message(self):
        state, calls = self._run(["Device 0C:F3:CE:B0:88:75 not available"] * 3, attempts=3)
        self.assertEqual(calls["pty"], 3)
        self.assertIn("OK + 返回", state["error"])

    def test_cure_restarts_bluetoothd_first_and_stops_when_keys_come_back(self):
        """bluetoothd 里的残库是这条路上最常见的坏法，而它**不需要动 bond**。

        2026-10-19 现场：清 bond + 控制器上下电 + 再配，连做三次都救不回来；
        `systemctl restart bluetooth` 一次就成——因为坏的是守护进程内存里的 GATT
        数据库，磁盘上的 bond 是好的。所以补救必须先走这条无破坏性的路。
        """
        state, calls = self._run(["Pairing successful"] * 3, paired_when=1,
                                 keys=False, keys_after_restart=True)
        self.assertEqual(calls["restart"], 1)
        self.assertEqual(calls["remove"], 0, "重启能救回来时不该清 bond")
        self.assertEqual(calls["cycle"], 0, "重启能救回来时不该动控制器")
        self.assertEqual(state["error"], "")

    def test_cure_falls_back_to_rebonding_when_restart_does_not_help(self):
        (_state, calls) = self._run(["Pairing successful"] * 3, paired_when=1,
                                    keys=False, keys_after_restart=False)
        self.assertEqual(calls["restart"], 1)
        self.assertGreaterEqual(calls["remove"], 1)
        self.assertGreaterEqual(calls["cycle"], 1)

    def test_cure_survives_a_missing_helper(self):
        """桌面/源码树跑没有 root helper——补救不可用不等于抛异常。"""
        (_state, calls) = self._run(["Pairing successful"] * 3, paired_when=1,
                                    keys=False, restart_ok=False)
        self.assertEqual(calls["restart"], 1)
        self.assertGreaterEqual(calls["remove"], 1)

    def test_cure_can_be_disabled(self):
        _state, calls = self._run(["Pairing successful"] * 3, paired_when=1,
                                  keys=False, cure=False)
        self.assertEqual(calls["remove"], 0)


class RestartBluetoothdTests(unittest.TestCase):
    """补救通道本身：拿不到 root helper 也不能把这条链带崩。"""

    def test_reports_failure_instead_of_raising(self):
        def boom():
            raise OSError("helper unavailable")
        self.assertFalse(net_bt.restart_bluetoothd(runner=boom))
        self.assertFalse(net_bt.restart_bluetoothd(runner=lambda: False))
        self.assertTrue(net_bt.restart_bluetoothd(runner=lambda: True))


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
        # 判据是「bluez 说是输入设备」+「这次扫描有信号强度」，不是随便一台有名字的。
        # 引号风格不算判据：biome 把这张页面排成双引号后，写死单引号的那一版在 main
        # 上已经红了（2026-10-26），而它要挡的是「拿名字当判据」。
        self.assertRegex(self.html, r"""indexOf\((["'])input-\1\)\s*===\s*0""")
        self.assertRegex(self.html, r"""typeof\s+cand\.rssi\s*===\s*(["'])number\1""")


if __name__ == "__main__":
    unittest.main()

class KeyboardStabilityTests(unittest.TestCase):
    """键盘设备必须**连续存在**才算配上。

    现场（2026-10-18 深夜）：链路没加密时 bluez 把键盘设备建出来、读属性失败
    （ATT 0x0E）再拆掉，如此循环；单点采样正好撞上"存在"那一刻，页面于是写
    「已配对，按键可用 ✓」，而每个键都是死的。
    """

    def test_absent_at_any_sample_is_unstable(self):
        seq = [True, False, True]
        with mock.patch.object(net_bt, "keyboard_device_present",
                               side_effect=lambda a: seq.pop(0)):
            self.assertFalse(net_bt._keyboard_stable("X", samples=3, interval=0))

    def test_present_throughout_is_stable(self):
        with mock.patch.object(net_bt, "keyboard_device_present", return_value=True):
            self.assertTrue(net_bt._keyboard_stable("X", samples=3, interval=0))

    def test_att_evidence_is_read_from_the_bluetooth_journal(self):
        fake = mock.Mock(stdout="hog-lib.c: Read Report Reference descriptor failed: "
                                "Request attribute has encountered an unlikely error")
        with mock.patch("subprocess.run", return_value=fake):
            self.assertTrue(net_bt._att_errors_recent())
        fake2 = mock.Mock(stdout="nothing to see here")
        with mock.patch("subprocess.run", return_value=fake2):
            self.assertFalse(net_bt._att_errors_recent())

    def test_error_names_the_unencrypted_link(self):
        with mock.patch.object(net_bt, "_pty_session", lambda *a, **k: "Pairing successful"), \
             mock.patch.object(net_bt, "info",
                               return_value={"paired": True, "bonded": True}), \
             mock.patch.object(net_bt, "_att_errors_recent", return_value=True):
            state = net_bt.pair("X", keyboard=lambda a: False, cure=False, attempts=1)
        self.assertIn("ATT 0x0E", state["error"])

