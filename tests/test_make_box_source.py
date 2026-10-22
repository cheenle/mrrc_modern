"""盒子源码包生成器：版本号与修复包编号都必须是**从 CHANGELOG 读出来的**。

2026-10-22 起盒子线多了一个修复包编号（FP1、FP2…），它写在
`## [镜像线 v1.28.5 · FP1]` 这样的标题里。两个值都要机器可读：读错了，包里自称的版本
与清单里写的版本就会分叉，盒子升级完又「发现新版本」（这正是 2026-10-19 把镜像线
独立编号的原因）。

设计：`docs/superpowers/specs/2026-10-22-box-fixpack-design.md`。
"""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "make_box_source", ROOT / "dev_tools" / "make_box_source.py")
assert _spec is not None and _spec.loader is not None
maker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(maker)


def _tree(tmp: str, heading: str) -> Path:
    root = Path(tmp)
    (root / "CHANGELOG.md").write_text(f"# 变更\n\n{heading}\n\n正文\n", encoding="utf-8")
    return root


class VersionOfTests(unittest.TestCase):
    def test_reads_the_box_line_heading(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = _tree(tmp, "## [镜像线 v1.28.5] — 2026-10-22 — 某次修复")
            self.assertEqual(maker.version_of(tree), ("1.28.5", ""))

    def test_reads_the_fix_pack_when_the_heading_carries_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = _tree(tmp, "## [镜像线 v1.28.5 · FP1] — 2026-10-22 — 某次修复")
            self.assertEqual(maker.version_of(tree), ("1.28.5", "FP1"))

    def test_the_desktop_heading_is_the_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = _tree(tmp, "## [v1.28.2] — 2026-10-21 — 桌面版")
            self.assertEqual(maker.version_of(tree), ("1.28.2", ""))

    def test_a_heading_with_nothing_to_read_aborts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = _tree(tmp, "## 没有版本号的标题")
            with self.assertRaises(SystemExit):
                maker.version_of(tree)


class StampTests(unittest.TestCase):
    def test_version_and_fix_pack_both_get_stamped(self):
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp)
            maker.stamp_tree(staged, "1.28.5", "FP1")
            self.assertEqual((staged / "version.txt").read_text(encoding="utf-8").strip(), "1.28.5")
            self.assertEqual((staged / "VERSION").read_text(encoding="utf-8").strip(), "1.28.5")
            self.assertEqual((staged / "fixpack.txt").read_text(encoding="utf-8").strip(), "FP1")

    def test_without_a_fix_pack_no_file_is_written(self):
        """空文件也会被设备端读成「有 FP」然后把编号显示成空——所以两件事都不能做。"""
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp)
            maker.stamp_tree(staged, "1.28.5", "")
            self.assertFalse((staged / "fixpack.txt").exists())


if __name__ == "__main__":
    unittest.main()
