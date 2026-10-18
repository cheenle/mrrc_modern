"""mrrc-firstboot 的盖章判据：只有配置定型才写 firstboot-done。

规格 D-8。这条判据的后果是"没探到电台的盒子每次开机再探一次" —— 卖家工作台上
那次开机（U 盘引导、没插电台）因此不会把"已完成"克隆进用户家的 eMMC，用户插上
电台开机时才定型。

判据本身（`needs_first_run`）由 tests/test_linux_first_run.py 覆盖；这里测的是
**包装器怎么用它**：不定型就不盖章、定了型才盖章并关掉单元。
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
# 镜像里 firstboot_wrapper.py 与 first_run.py 是兄弟（都在 /opt/mrrc_modern/linux/）；
# 仓库里不是（packaging/common/ 与 linux/），所以这里要把 linux/ 加进 sys.path。
sys.path.insert(0, str(REPO / "linux"))

import first_run  # noqa: E402  # type: ignore[import-not-found]

SETTLED = ("MRRC_WEB_PASSWORD=abcd!1234\n"
           "MRRC_SERIAL_PORT=/dev/ttyUSB0\n"
           "MRRC_RADIO_MODEL=ft710\n"
           "MRRC_PORT_CONFIRMED=1\n")
UNSETTLED = ("MRRC_WEB_PASSWORD=abcd!1234\nMRRC_SERIAL_PORT=\nMRRC_RADIO_MODEL=ft710\n")


def load_wrapper():
    spec = importlib.util.spec_from_file_location(
        "firstboot_wrapper", REPO / "packaging" / "common" / "firstboot_wrapper.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StampTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.env_file = root / "mrrc.env"
        self.done = root / "firstboot-done"
        self.wrapper = load_wrapper()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(self.wrapper, "ENV_FILE", self.env_file).start()
        mock.patch.object(self.wrapper, "DONE", self.done).start()
        mock.patch.object(self.wrapper, "ensure_cert").start()
        mock.patch.object(self.wrapper.subprocess, "run").start()

    def test_an_unsettled_probe_leaves_the_flag_unset(self):
        self.env_file.write_text(UNSETTLED, encoding="utf-8")
        with mock.patch.object(self.wrapper, "needs_first_run",
                               return_value=True) as probe:
            self.assertEqual(self.wrapper.main(), 0)
        self.assertFalse(
            self.done.exists(),
            "没定型却盖了章 ⇒ 用户家的电台永远不会被发现")
        probe.assert_called_once()

    def test_a_settled_probe_stamps_and_disables_the_unit(self):
        self.env_file.write_text(SETTLED, encoding="utf-8")
        with mock.patch.object(self.wrapper, "needs_first_run", return_value=False):
            self.assertEqual(self.wrapper.main(), 0)
        self.assertTrue(self.done.exists())

    def test_the_real_judgement_is_what_decides(self):
        """不注入判据本身，只注入"那个设备节点在不在" —— 走真实代码路径。"""
        self.env_file.write_text(SETTLED, encoding="utf-8")
        with mock.patch.object(first_run, "port_node_exists", return_value=True):
            self.wrapper.main()
        self.assertTrue(self.done.exists())

        self.done.unlink()
        with mock.patch.object(first_run, "port_node_exists", return_value=False):
            self.wrapper.main()
        self.assertFalse(self.done.exists(), "节点不在 ⇒ 还没定型，不许盖章")

    def test_a_box_that_never_settles_still_gets_its_certificate(self):
        """不定型也得能起网页：证书那一步在盖章之前，不能被判据挡住。"""
        self.env_file.write_text(UNSETTLED, encoding="utf-8")
        with mock.patch.object(self.wrapper, "needs_first_run", return_value=True):
            self.wrapper.main()
        self.wrapper.ensure_cert.assert_called_once()


if __name__ == "__main__":
    unittest.main()
