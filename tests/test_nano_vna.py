"""NanoVNA 协议层测试：真机抓到的字节当 fixture，设备对象用假传输，不碰硬件。"""
import struct
import unittest
from unittest.mock import patch

from nano import vna

# 真机 NanoVNA-H 4 / firmware 1.2.14 的 info 正文（2026-10-18 抓）
INFO_TEXT = """Board: NanoVNA-H 4
2019-2022 Copyright NanoVNA.com
based on  @DiSlord @edy555 ... source
Licensed under GPL.
Version: 1.2.14 [p:401, IF:12k, ADC:384k, Lcd:480x320]
Build Time: Aug 31 2022 - 13:39:49
Architecture: ARMv7E-M Core Variant: Cortex-M4F
Platform: STM32F303xC Analog & DSP"""


def _frame(points, mask=0x83, **over):
    """按固件 main.c 的布局造一帧 scan_bin 回复（可注入坏值）。"""
    out = struct.pack("<HH", mask, points)
    for i in range(points):
        out += struct.pack("<I", 1_000_000 + i * 10_000)
        out += struct.pack("<ff", 0.9995 + i * 1e-6, 0.0025)
        if mask & 0x04:
            out += struct.pack("<ff", 0.0, 0.0)
    return over.get("body", out)


class FakeTransport:
    """命令 → 回复字节。记录收到的命令，方便断言。"""

    def __init__(self, replies=None, binary=None):
        self.replies = replies or {}
        self.binary = binary
        self.sent = []
        self.closed = False

    def request(self, cmd, timeout=2.0):
        self.sent.append(cmd)
        return self.replies.get(cmd, "")

    def request_binary(self, cmd, timeout=4.0):
        self.sent.append(cmd)
        if isinstance(self.binary, list):
            return self.binary.pop(0)
        return self.binary if self.binary is not None else b""

    def close(self):
        self.closed = True


class EchoStripTest(unittest.TestCase):
    """回显行必须整行去掉 —— 只去掉 CRLF 会把 `data 0` 变成一行"数据"。"""

    def test_echo_line_is_dropped(self):
        self.assertEqual(vna.drop_echo_line(b"data 0\r\n1.0 0.0\r\n2.0 0.0\r\n"),
                         b"1.0 0.0\r\n2.0 0.0\r\n")

    def test_reply_without_echo_is_kept(self):
        self.assertEqual(vna.drop_echo_line(b"1.2.14"), b"1.2.14")


class InfoParseTest(unittest.TestCase):
    def test_board_version_points_build_platform(self):
        d = vna.parse_info(INFO_TEXT)
        self.assertEqual(d.board, "NanoVNA-H 4")
        self.assertEqual(d.version, "1.2.14")
        self.assertEqual(d.points_max, 401)
        self.assertEqual(d.build, "Aug 31 2022 - 13:39:49")
        self.assertIn("STM32F303", d.platform)

    def test_version_only_firmware_still_parses(self):
        d = vna.parse_info("Board: NanoVNA-H\nVersion: 0.8.0")
        self.assertEqual(d.version, "0.8.0")
        self.assertEqual(d.points_max, 0)


class VersionVbatFreqTest(unittest.TestCase):
    def test_parse_version(self):
        self.assertEqual(vna.parse_version(b"1.2.14" + vna.PROMPT), "1.2.14")
        self.assertEqual(vna.parse_version(b"\r\n0.8.0\r\n"), "0.8.0")

    def test_parse_version_rejects_garbage(self):
        with self.assertRaises(vna.NanoError):
            vna.parse_version(b"NanoVNA Shell")

    def test_parse_vbat(self):
        self.assertEqual(vna.parse_vbat("3627 mV"), 3627)
        self.assertIsNone(vna.parse_vbat("no battery"))

    def test_parse_frequencies(self):
        text = "\r\n".join(["50000", "9049500", "18049000"]) + "\r\n"
        self.assertEqual(vna.parse_frequencies(text), [50000, 9049500, 18049000])


class FrameParseTest(unittest.TestCase):
    def test_real_header_from_hardware(self):
        # 真机抓到的前 4 字节：mask=0x0087(135), points=0x0191(401)
        mask, points = struct.unpack("<HH", bytes([0x87, 0x00, 0x91, 0x01]))
        self.assertEqual(mask, 0x87)
        self.assertEqual(points, 401)
        self.assertEqual(vna.frame_length(points, mask), 4 + 401 * 20)

    def test_roundtrip_s11_only(self):
        sweep = vna.parse_scan_bin(_frame(401))
        self.assertEqual(len(sweep.freqs), 401)
        self.assertEqual(sweep.freqs[0], 1_000_000)
        self.assertAlmostEqual(sweep.s11[0][0], 0.9995, places=4)
        self.assertIsNone(sweep.s21)

    def test_roundtrip_with_s21(self):
        sweep = vna.parse_scan_bin(_frame(51, mask=0x87))
        self.assertEqual(len(sweep.s21), 51)
        self.assertEqual(vna.frame_length(51, 0x87), 4 + 51 * 20)

    def test_truncated_frame_is_rejected_whole(self):
        with self.assertRaises(vna.FrameError):
            vna.parse_scan_bin(_frame(51)[:-5])

    def test_trailing_bytes_are_rejected_whole(self):
        with self.assertRaises(vna.FrameError):
            vna.parse_scan_bin(_frame(51) + b"\x00\x00")

    def test_echo_and_trailing_prompt_are_stripped(self):
        raw = b"scan_bin 1 2 51 131\r\n" + _frame(51) + vna.PROMPT
        self.assertEqual(len(vna.parse_scan_bin(raw).freqs), 51)

    def test_prompt_before_payload_is_tolerated(self):
        raw = b"scan_bin 1 2 51 131\r\n" + vna.PROMPT + _frame(51)
        self.assertEqual(len(vna.parse_scan_bin(raw).freqs), 51)


class ScanCommandTest(unittest.TestCase):
    def test_mask_and_arguments(self):
        self.assertEqual(vna.scan_command(1_000_000, 54_000_000, 401),
                         "scan_bin 1000000 54000000 401 131")

    def test_s21_sets_bit_two(self):
        self.assertEqual(vna.scan_command(1_000_000, 54_000_000, 101, s21=True),
                         "scan_bin 1000000 54000000 101 135")

    def test_rejects_bad_ranges(self):
        for args in ((54_000_000, 1_000_000, 401), (0, 1_000_000, 101),
                     (1_000_000, 2_000_000, 500)):
            with self.assertRaises(ValueError):
                vna.scan_command(*args)


class DeviceTest(unittest.TestCase):
    def _device(self, **kw):
        t = FakeTransport(**kw)
        return vna.NanoVna("/dev/fake", transport=t), t

    def test_open_verifies_identity(self):
        dev, t = self._device(replies={"version": "1.2.14"})
        dev.open()
        self.assertIn("version", t.sent)

    def test_open_rejects_non_vna(self):
        dev, _ = self._device(replies={"version": "NanoVNA Shell"})
        with self.assertRaises(vna.NanoError):
            dev.open()

    def test_scan_returns_sweep_and_elapsed(self):
        dev, t = self._device(binary=_frame(101))
        sweep = dev.scan(1_000_000, 54_000_000, 101)
        self.assertEqual(len(sweep.freqs), 101)
        self.assertGreaterEqual(sweep.elapsed_ms, 0)
        self.assertTrue(t.sent[0].startswith("scan_bin "))

    def test_scan_retries_once_then_succeeds(self):
        dev, t = self._device(binary=[_frame(51)[:-3], _frame(51)])
        sweep = dev.scan(1_000_000, 2_000_000, 51) if False else None
        # 51 点不在白名单里，所以这里直接用 101 点做两次尝试
        dev2, t2 = self._device(binary=[_frame(101)[:-3], _frame(101)])
        sweep = dev2.scan(1_000_000, 2_000_000, 101)
        self.assertEqual(len(sweep.freqs), 101)
        self.assertEqual(len([c for c in t2.sent if c.startswith("scan_bin")]), 2)

    def test_scan_raises_after_two_bad_frames(self):
        dev, _ = self._device(binary=[_frame(101)[:-3], _frame(101)[:-3]])
        with self.assertRaises(vna.FrameError):
            dev.scan(1_000_000, 2_000_000, 101)

    def test_restore_sweep_swallows_errors(self):
        dev, t = self._device()
        t.request = lambda cmd, timeout=2.0: (_ for _ in ()).throw(vna.NanoError("boom"))
        self.assertFalse(dev.restore_sweep(50_000, 900_000_000, 101))

    def test_read_settings_collects_numbers(self):
        dev, _ = self._device(replies={
            "edelay s11": "1234.5",
            "edelay s21": "0.000000",
            "smooth": "0",
            "power": "2",
            "offset": "0",
            "s21offset": "0.000000",
            "bandwidth": "bandwidth 3 (500Hz)",
        })
        s = dev.read_settings()
        self.assertEqual(s["edelay_s11_ps"], 1234.5)
        self.assertEqual(s["power"], 2)
        self.assertEqual(s["bandwidth"], 3)
        self.assertEqual(s["bandwidth_hz"], 500)

    def test_set_setting_whitelist(self):
        dev, t = self._device()
        dev.set_setting("smooth", 3)
        self.assertIn("smooth 3", t.sent)
        with self.assertRaises(ValueError):
            dev.set_setting("tcxo", 26000000)          # 永远不在白名单里
        with self.assertRaises(ValueError):
            dev.set_setting("smooth", 99)

    def test_cal_slots(self):
        dev, t = self._device()
        dev.recall_cal(2)
        dev.save_cal(4)
        self.assertEqual(t.sent, ["recall 2", "save 4"])
        with self.assertRaises(ValueError):
            dev.recall_cal(9)


class PortDiscoveryTest(unittest.TestCase):
    def test_explicit_env_wins(self):
        got = vna.discover_port({"MRRC_NANO_PORT": "/dev/ttyACM7"})
        self.assertEqual(got, "/dev/ttyACM7")

    def test_by_id_chibios_is_preferred(self):
        with patch.object(vna.Path, "is_dir", return_value=True), \
             patch.object(vna.Path, "glob",
                          side_effect=lambda pat: [vna.Path("/dev/serial/by-id/usb-ChibiOS_400-if00")]
                          if "ChibiOS" in pat else []), \
             patch.object(vna.glob, "glob", return_value=["/dev/ttyACM0"]):
            self.assertEqual(vna.discover_port({}),
                             "/dev/serial/by-id/usb-ChibiOS_400-if00")

    def test_radio_cat_port_is_never_selected(self):
        real = "/dev/serial/by-id/usb-Silicon_Labs_CP2105_0121DB3A-if00-port0"
        with patch.object(vna.Path, "is_dir", return_value=True), \
             patch.object(vna.Path, "glob",
                          side_effect=lambda pat: [vna.Path(real)] if "ChibiOS" in pat else []), \
             patch.object(vna.glob, "glob", return_value=[]):
            self.assertIsNone(vna.discover_port({}, cat_port=real))


if __name__ == "__main__":
    unittest.main()
