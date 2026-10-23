"""换电脑备份/恢复脚本的静态守卫。

本机（macOS）没有 PowerShell，所以这里只钉**源码事实**：PowerShell 5.1 读不了的东西、
cmd.exe 会崩的写法、发布副本与源不一致、以及那条最容易在维护时被改坏的固定清单。
真正的行为由脚本自己的 `-SelfTest` 在 Windows 上验（构建验收清单里有一条）。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from tests.ps_script_guards import (
    assert_ascii_cmd, assert_no_parens_in_blocks, assert_utf8_bom)

ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = ROOT / "tools" / "windows"
DOWNLOADS = ROOT / "website" / "downloads"

SCRIPT = "MRRC-Modern-Backup.ps1"
CMD = "MRRC-Modern-Backup.cmd"

#: 备份集。改脚本里的 `$Script:FixedItems` 必须同时改这里 —— 两边不一致就是
#: 「备份包悄悄少了一项」，而恢复端不会报错。
FIXED_ITEMS = (
    "mrrc_modern.env",
    "certs",
    "mem_channels.json",
    "atr1000_tuner.json",
    "fleet",
    "auth-tokens.json",
)


class SourceFilesTests(unittest.TestCase):
    def test_script_has_a_utf8_bom(self):
        assert_utf8_bom(self, SOURCE_DIR / SCRIPT)

    def test_launcher_is_ascii_only(self):
        assert_ascii_cmd(self, SOURCE_DIR / CMD, must_contain=("-Export", "-Import"))

    def test_launcher_does_not_put_parentheses_inside_blocks(self):
        assert_no_parens_in_blocks(self, SOURCE_DIR / CMD)

    def test_fixed_item_list_matches_the_tests(self):
        text = (SOURCE_DIR / SCRIPT).read_text(encoding="utf-8-sig")
        found = tuple(re.findall(r'Path\s*=\s*"([^"]+)"', text))
        self.assertEqual(found, FIXED_ITEMS,
                         "脚本的固定清单与测试里的 FIXED_ITEMS 不一致："
                         "加/删一项必须两边同时改")

    def test_encryption_branch_really_deletes_the_plain_zip(self):
        text = (SOURCE_DIR / SCRIPT).read_text(encoding="utf-8-sig")
        protect = text.find("function Protect-BackupFile")
        delete = text.find("Remove-Item -LiteralPath $zipPath -Force")
        self.assertGreater(protect, 0, "找不到加密函数 Protect-BackupFile")
        self.assertGreater(delete, protect,
                           "加密之后必须删掉明文 zip（顺序：先加密、再删除）")
        for word in ("Aes", "Rfc2898DeriveBytes", "HMACSHA256"):
            self.assertIn(word, text, f"加密实现里没有 {word}")


class PublishedCopyTests(unittest.TestCase):
    def test_downloads_carry_the_current_sources(self):
        for name in (SCRIPT, CMD):
            with self.subTest(name=name):
                self.assertEqual(
                    (SOURCE_DIR / name).read_bytes(), (DOWNLOADS / name).read_bytes(),
                    f"{DOWNLOADS / name} 与源不一致：下载页是用旧副本发布的",
                )


if __name__ == "__main__":
    unittest.main()
