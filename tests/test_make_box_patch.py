"""热修包的「差分完整性」守卫：改过的出厂文件漏带了就拒绝构建。

2026-11-17 的 FP73：补丁只带了本次修复碰过的 `server.py`，而更早一个提交
（登录限流可配置）改过 `config.py`。补丁解到 1.28.85 的树上，新的 `server.py`
在模块层读 `config.LOGIN_MAX_ATTEMPTS` ⇒ `AttributeError` ⇒ 盒子上「更新成功、
服务起不来」（没有网页、没有刷新入口）。`--base-tag` 只看得见**新增**文件，
所以这一类改过的旧文件此前完全没有守卫。
"""
from __future__ import annotations

import importlib.util
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "make_box_patch", ROOT / "dev_tools" / "make_box_patch.py")
assert _spec is not None and _spec.loader is not None
maker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(maker)


class ShipsToDeviceTests(unittest.TestCase):
    def test_shipped_prefixes(self):
        for rel in ("server.py", "config.py", "static/terminal.js",
                    "backends/yaesu/cat_core.py", "linux/mrrc_update.sh",
                    "vendor/ftdi/libft4222.so", "profiles/w103d.env"):
            with self.subTest(rel=rel):
                self.assertTrue(maker.ships_to_device(rel))

    def test_repo_only_paths(self):
        for rel in ("tests/test_ws_terminal.py", "docs/PROJECT_MAP.md",
                    "SDD/09-architecture-overview.md", "dev_tools/make_box_patch.py",
                    "website/index.html", "README.md", "tools/support_receiver/x.py",
                    # 构建期目录：stage-tree 明确排除它，永远不会上盒子
                    "packaging/common/stage-tree.sh", "packaging/box/build-image.sh"):
            with self.subTest(rel=rel):
                self.assertFalse(maker.ships_to_device(rel))


class MissingChangedFilesTests(unittest.TestCase):
    def test_a_changed_but_unlisted_file_is_reported(self):
        """`missing_changed_files` 只该报「改过、没带上、且真会出厂」的那几个。"""
        touched = {"config.py", "server.py", "tests/test_x.py", "docs/x.md",
                   "static/terminal.js"}
        included = ["server.py", "static/terminal.js"]
        # 用一个假的 git 结果替掉 shell 调用，单测不依赖仓库历史
        with mock.patch.object(maker, "_git_name_only", return_value=set(touched)):
            missing = maker.missing_changed_files("BASE", included)
        self.assertEqual(missing, ["config.py"])

    def test_nothing_missing_when_every_shipped_file_is_included(self):
        with mock.patch.object(maker, "_git_name_only",
                               return_value={"config.py", "server.py",
                                             "tests/test_x.py"}):
            self.assertEqual(
                maker.missing_changed_files("BASE", ["config.py", "server.py"]), [])

    def test_dot_slash_prefixes_are_normalised(self):
        with mock.patch.object(maker, "_git_name_only", return_value={"config.py"}):
            self.assertEqual(
                maker.missing_changed_files("BASE", ["./config.py"]), [])


class CliSurfaceTests(unittest.TestCase):
    def test_the_flag_exists(self):
        source = (ROOT / "dev_tools" / "make_box_patch.py").read_text(encoding="utf-8")
        self.assertIn('"--base-ref"', source)

    def test_the_refusal_names_the_failure(self):
        source = (ROOT / "dev_tools" / "make_box_patch.py").read_text(encoding="utf-8")
        self.assertIn("热修是差分", source)
        self.assertIn("LOGIN_MAX_ATTEMPTS", source)


if __name__ == "__main__":
    unittest.main()
