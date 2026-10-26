"""Tests for linux/first_run.py — Pi first-boot auto-configuration."""
import inspect
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
        # 注入“设备节点在不在”：needs_first_run 现在会查一次文件系统，测试不能依赖
        # 开发机上真有 /dev/ttyUSB0。
        self.assertFalse(first_run.needs_first_run(env, port_exists=lambda port: True))


class ProbeSettlementTests(unittest.TestCase):
    """规格 D-8：定型 = 口令非默认 + 型号有效 + 串口已确认 + **那个节点此刻在**。"""

    BASE = {"MRRC_WEB_PASSWORD": "s3cret-pass", "MRRC_RADIO_MODEL": "ic7300",
            "MRRC_SERIAL_PORT": "/dev/ttyUSB0", "MRRC_PORT_CONFIRMED": "1"}

    def test_a_recorded_port_that_is_not_here_is_not_settled(self):
        self.assertFalse(first_run.needs_first_run(self.BASE,
                                                   port_exists=lambda port: True))
        self.assertTrue(first_run.needs_first_run(self.BASE,
                                                  port_exists=lambda port: False))

    def test_an_empty_port_is_never_settled(self):
        env = {**self.BASE, "MRRC_SERIAL_PORT": ""}
        # 不注入：空白串口由真的 port_node_exists 判定（它不碰文件系统，确定性）。
        self.assertTrue(first_run.needs_first_run(env))

    def test_a_windows_port_name_is_not_a_path(self):
        """“COM5”永远不是文件；把“我说不准”读成“不在”，Windows 每次启动都会重探。"""
        self.assertTrue(first_run.port_node_exists("COM5"))
        self.assertFalse(first_run.port_node_exists(""))
        self.assertFalse(first_run.port_node_exists("/dev/definitely-not-here-1234"))

    def test_a_preseeded_env_is_still_probed_but_keeps_its_password(self):
        cfg = Path(tempfile.mkdtemp()) / "mrrc.env"
        cfg.write_text("MRRC_WEB_PASSWORD=preseed-pass\nMRRC_SERIAL_PORT=\n"
                       "MRRC_RADIO_MODEL=\n", encoding="utf-8")

        def open_func(port, baudrate, timeout):
            ser = MagicMock()
            ser.read.side_effect = lambda n: b"ID" if baudrate == 38400 else b""
            return ser

        env = {"MRRC_WEB_PASSWORD": "preseed-pass", "MRRC_SERIAL_PORT": "",
               "MRRC_RADIO_MODEL": ""}
        with unittest.mock.patch.object(first_run, "detect_serial_ports",
                                        return_value=["/dev/ttyUSB1"]):
            out = first_run.apply_first_run(env, cfg, preseeded=True, open_func=open_func)

        self.assertEqual(out["MRRC_WEB_PASSWORD"], "preseed-pass")
        self.assertNotIn("MRRC_AUTO_PASSWORD", out, "预置通道不许发明口令")
        self.assertEqual(out["MRRC_SERIAL_PORT"], "/dev/ttyUSB1")
        self.assertEqual(out["MRRC_PORT_CONFIRMED"], "1")


class PasswordBeforeProbeTests(unittest.TestCase):
    """macOS 那份在 2026-10-03 现场修过两条，Linux 移植时漏了 —— 2026-10-17 补齐。

    ①口令必须在探测**之前**落盘（探测再也回不来时，留给运维的不是一个空口令的模板）；
    ②探测循环之前必须先出声（首启横幅在它后面，卡住时 HDMI 全黑）。
    """

    def test_the_generated_password_is_on_disk_before_any_port_is_probed(self):
        cfg = Path(tempfile.mkdtemp()) / "mrrc.env"
        cfg.write_text("MRRC_WEB_PASSWORD=\nMRRC_SERIAL_PORT=\nMRRC_RADIO_MODEL=\n",
                       encoding="utf-8")
        seen_at_probe = {}

        def probe(port, open_func=None):
            seen_at_probe["line"] = [ln for ln in cfg.read_text(encoding="utf-8").splitlines()
                                     if ln.startswith("MRRC_WEB_PASSWORD=")][0]
            return None

        with unittest.mock.patch.object(first_run, "probe_radio_model", side_effect=probe), \
             unittest.mock.patch.object(first_run, "detect_serial_ports",
                                        return_value=["/dev/ttyUSB0"]):
            first_run.apply_first_run({"MRRC_WEB_PASSWORD": "", "MRRC_SERIAL_PORT": "",
                                       "MRRC_RADIO_MODEL": ""}, cfg)

        self.assertTrue(seen_at_probe.get("line"), "探测根本没被调用 —— 这个测试就白跑了")
        self.assertTrue(seen_at_probe["line"].split("=", 1)[1],
                        "探测发生时口令还没落盘 —— 一旦这一步卡住，用户既没有口令也没有日志")

    def test_the_console_says_something_before_it_probes(self):
        # 只看这个函数本身（用 inspect 而不是 __file__：后者在类型上可能是 None，
        # 而这里的断言本来就只关心探测循环之前那几行）。
        source = inspect.getsource(first_run.apply_first_run)
        probe_at = source.index("probe_radio_model(candidate")
        self.assertIn("print(", source[:probe_at],
                      "探测循环之前没有输出 ⇒ 卡住时 HDMI 全黑")

    def test_a_preseeded_run_never_invents_a_password(self):
        cfg = Path(tempfile.mkdtemp()) / "mrrc.env"
        cfg.write_text("MRRC_WEB_PASSWORD=preseed-pass\n", encoding="utf-8")
        with unittest.mock.patch.object(first_run, "detect_serial_ports", return_value=[]):
            first_run.apply_first_run({"MRRC_WEB_PASSWORD": "preseed-pass"}, cfg,
                                      preseeded=True)
        written = cfg.read_text(encoding="utf-8")
        self.assertIn("MRRC_WEB_PASSWORD=preseed-pass", written)
        self.assertNotIn("MRRC_AUTO_PASSWORD", written)


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

    def test_the_writer_is_the_one_writer(self):
        """D-8：env 文件只有一份写入实现，首启这一份不得自己再写一套。

        2026-10-26 现场（W103D + IC-7300）：文件里 `MRRC_SERIAL_PORT` 出现两次、
        后一条为空。`/api/setup` 用的就是这里的 `update_env_file`，而它只改第一个
        匹配行 → 6 次保存 `/dev/ttyUSB0` 全部落在被遮蔽的那一行，CI-V 一次端口都
        没打开。两份实现还会各自修各自的坑（env_store 已持锁 + 保留 mode）。
        """
        import env_store
        self.assertIs(first_run.update_env_file, env_store.update_env_file)

    def test_a_duplicated_key_is_written_where_the_readers_look(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, b"MRRC_SERIAL_PORT=/dev/ttyUSB0\nMRRC_SERIAL_PORT=\n")
            first_run.update_env_file(path, {"MRRC_SERIAL_PORT": "/dev/ttyACM0"})
            lines = [l for l in path.read_text(encoding="utf-8").splitlines()
                     if l.startswith("MRRC_SERIAL_PORT=")]
            self.assertEqual(lines, ["MRRC_SERIAL_PORT=/dev/ttyACM0"])
