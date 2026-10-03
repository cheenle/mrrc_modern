"""First-run serial detection must not be able to hang, and must not write to the wrong port.

Field report 2026-10-03 (Windows, hostname MRRC): after installing, the launcher showed a **black
window with no output at all** and no server ever started. The launcher process was alive, holding
COM3 open, with one thread in `Wait/UserRequest`.

Measured root cause, in three parts:

1. `_key()`'s radio-ish keywords are "cp210x"/"slab". The radio's real description is
   "Silicon Labs Dual CP2105 USB to UART Bridge" — which contains neither ("cp2105" != "cp210x",
   and "silicon labs" has no "slab"). So the intended priority silently did nothing and the
   candidates kept registry order: **COM3, COM4, COM5**.
2. COM3 is "Intel(R) Active Management Technology - SOL". With the launcher holding it, opening it
   from another process returned Access Denied; after the launcher was killed, the same probe
   opened it fine and the **write timed out** — a port that answers open() and never accepts a byte.
3. `probe_ft710()` writes `AI0;` through pyserial, whose `write_timeout` defaults to **None =
   block forever**. So the very first port probed was the one that hangs, and it held COM3 until
   somebody killed the process.

Two more facts made that failure undiagnosable and unrecoverable: every `print()` in the launcher
comes after the probe, so the console stayed empty (the "black screen"), and `apply_first_run()`
persists the config only after the whole loop — so no certificate, no server, no `launcher.log`,
and not even the auto-generated password survived.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class _Win32(unittest.TestCase):
    """本机是 macOS，而 _candidates() 按平台分叉 —— 不切平台这几条会**空跑通过**
    （COM 口在 darwin 分支里被整体过滤，于是 assertNotIn 永远成立）。"""

    def setUp(self):
        patcher = patch.object(sys, "platform", "win32")
        patcher.start()
        self.addCleanup(patcher.stop)

from macos import first_run


class _Port:
    """Just enough of pyserial's ListPortInfo for _candidates/detect_serial_ports."""

    def __init__(self, device, description, hwid):
        self.device = device
        self.description = description
        self.hwid = hwid


# The three ports exactly as this machine reported them.
AMT_SOL = _Port("COM3", "Intel(R) Active Management Technology - SOL",
                r"PCI\VEN_8086&DEV_9C3D&SUBSYS=221417AA&REV=04\3&B1BFB68&0&B3")
RADIO_ENHANCED = _Port("COM4", "Silicon Labs Dual CP2105 USB to UART Bridge: Enhanced COM Port",
                       "USB VID:PID=10C4:EA70 SER=0121DB3A LOCATION=1-2.1:x.0")
RADIO_STANDARD = _Port("COM5", "Silicon Labs Dual CP2105 USB to UART Bridge: Standard COM Port",
                       "USB VID:PID=10C4:EA70 SER=0121DB3A LOCATION=1-2.1:x.1")


class RadioPortSelectionTests(_Win32):
    def test_the_real_radio_description_counts_as_radio_like(self):
        """判别力在于**同时存在一个不会被排除、但也不是电台的候选口**。

        光拿 COM4/COM5 断言"电台口排在前面"是没用的：Intel AMT 口一旦被排除，就只剩电台口了，
        顺序怎么排都成立 —— 那条断言在"关键词修回去"的变异下依然是绿的（实测）。
        所以这里放一个按名字排序会跑到电台**前面**的普通口（COM1），
        只有关键词真的命中，电台口才会被提到它前面。
        """
        plain = _Port("COM1", "Communications Port", r"ACPI\PNP0501\1")
        order = first_run.detect_serial_ports([plain, AMT_SOL, RADIO_ENHANCED, RADIO_STANDARD])
        self.assertIn("COM1", order, "普通口被整个排除了 —— 下面的排序断言就失去意义")
        self.assertEqual(
            order[0], "COM4",
            "电台口没有拿到优先权 —— 关键词与实际描述不符（CP2105 ≠ cp210x，"
            "Silicon Labs 里没有 slab），于是按名字排序时 COM1 跑到了它前面")

    def test_the_intel_amt_port_is_never_a_candidate(self):
        order = first_run.detect_serial_ports([AMT_SOL, RADIO_ENHANCED])
        self.assertIn("COM4", order, "选口逻辑没在跑 —— 下面的断言会空跑")
        self.assertNotIn("COM3", order,
                         "Intel AMT SOL 口会被 open 成功但吞掉写入 —— 永不探测它")

    def test_a_non_usb_port_is_never_a_candidate(self):
        """电台一定是 USB 转串口；hwid 是 PCI 的口不该被写。"""
        bogus = _Port("COM9", "Communications Port", r"PCI\VEN_8086&DEV_1234")
        order = first_run.detect_serial_ports([bogus, RADIO_ENHANCED])
        self.assertIn("COM4", order, "选口逻辑没在跑 —— 下面的断言会空跑")
        self.assertNotIn("COM9", order)


class ProbeTimeoutTests(unittest.TestCase):
    def test_the_probe_asks_for_a_write_timeout(self):
        seen = {}

        def opener(**kwargs):
            seen.update(kwargs)
            raise OSError("no such port")            # keeps the loop from needing a real device

        first_run.probe_radio_model("COM9", open_func=opener)
        self.assertIn("write_timeout", seen,
                      "没有传 write_timeout ⇒ pyserial 默认 None ⇒ 写调用可以永久阻塞")
        self.assertIsNotNone(seen["write_timeout"], "write_timeout 必须是有限值")
        self.assertGreater(seen["write_timeout"], 0)

    def test_a_port_that_never_accepts_a_write_is_skipped_not_fatal(self):
        """写入超时必须变成"跳过这个口"，而不是把 launcher 卡住或抛出去。"""

        class _WriteBlocks:
            timeout = 1.0
            write_timeout = 1.0

            def reset_input_buffer(self):
                pass

            def write(self, _data):
                import serial
                raise serial.SerialTimeoutException("write timeout")

            def read(self, _n):
                return b""

            def close(self):
                pass

        result = first_run.probe_radio_model("COM3", open_func=lambda **_: _WriteBlocks())
        self.assertIsNone(result, "吞掉写入的口应当被跳过（返回 None），而不是抛出或挂住")


class PasswordBeforeProbeTests(unittest.TestCase):
    """口令必须在探测**之前**落盘。

    实测：卡死的那次运行留下的配置是原封不动的模板 —— 没有口令、没有 MRRC_FIRST_RUN_DONE。
    原因是 apply_first_run 把所有更新攒到最后一次性写入，而它永远没走到那一步。
    """

    def test_the_generated_password_is_on_disk_before_any_port_is_probed(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "mrrc_modern.env"
            cfg.write_text("MRRC_WEB_PASSWORD=\nMRRC_SERIAL_PORT=\nMRRC_RADIO_MODEL=\n",
                           encoding="utf-8")
            seen_at_probe = {}

            def probe(port, open_func=None):
                seen_at_probe["password"] = [
                    ln for ln in cfg.read_text(encoding="utf-8").splitlines()
                    if ln.startswith("MRRC_WEB_PASSWORD=")
                ][0]
                return None                          # no radio found; the loop keeps going

            with patch.object(first_run, "probe_radio_model", side_effect=probe), \
                 patch.object(first_run, "detect_serial_ports", return_value=["COM4"]):
                first_run.apply_first_run({"MRRC_WEB_PASSWORD": "", "MRRC_SERIAL_PORT": "",
                                           "MRRC_RADIO_MODEL": ""}, cfg)

            self.assertTrue(seen_at_probe.get("password"),
                            "探测根本没被调用 —— 这个测试就白跑了")
            value = seen_at_probe["password"].split("=", 1)[1]
            self.assertTrue(
                value,
                "探测发生时配置里的口令还是空的 —— 一旦这一步卡住，用户既没有口令也没有日志")

    def test_the_launcher_says_what_it_is_doing_before_it_probes(self):
        """黑屏之所以无解，是因为探测之前一行输出都没有。"""
        source = (Path(first_run.__file__)).read_text(encoding="utf-8")
        probe_at = source.index("probe_radio_model(candidate")
        before = source[:probe_at]
        self.assertIn("print(", before[before.rindex("def apply_first_run("):],
                      "探测循环之前没有 print ⇒ 卡住时控制台全黑，现场无法判断")


if __name__ == "__main__":
    unittest.main()
