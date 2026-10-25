"""`dev_tools/mrrc-update-ssh.cmd`：老盒子（≤1.27）的就地升级，走 SSH。

这条链路单独存在的理由：它要在客户机上开一个 SSH 会话、以 root 身份覆盖
`/opt/mrrc_modern`。它不该躺在一个双击就跑的"点一下升级"工具里（那是给客户
用的，只调盒子自己的 API），也不该有两份实现。

这里的断言只钉**契约**，不钉文案：
 ① 不从盒子取包地址（旧构建的 `/api/update/check` 走回退分支，给的是桌面版
    installer），而是自己读 `latest.json` 的 `box` / `boxLine`；
 ② 下载后必须核对 SHA-256，核对不过就一个字节都不发出去；
 ③ 装的是**随工具发布**的安装器（`mrrc-remote-update.sh`），不是盒子上的 `mrrc-update`
    —— 后者在 ≤1.25.5 上会删 `env/`、`profiles/`、录音；
 ④ 动手前必须问过一次（换代码 + 重启服务）；
 ⑤ 成功判据是"服务回来了"，用 HTTP 状态码判，不是 `curl -f`；
 ⑥ 不需要网页口令 —— 它压根不登 API。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CMD_PATH = ROOT / "dev_tools" / "mrrc-update-ssh.cmd"
CMD = CMD_PATH.read_text(encoding="utf-8")


class ContractTests(unittest.TestCase):
    def test_the_package_comes_from_the_manifest(self):
        self.assertIn("latest.json", CMD)
        self.assertIn("$j.box", CMD)
        self.assertIn("$j.boxLine", CMD, "清单从 2026-10-19 起用的是 boxLine")
        self.assertNotIn("/api/update/check", CMD,
                         "旧构建的这个端点是回退分支，给的是桌面版 installer")

    def test_the_hash_is_checked_before_anything_is_sent(self):
        check = CMD.index("Get-FileHash")
        scp = CMD.index("scp -o StrictHostKeyChecking")   # 不是 `where scp`
        self.assertLess(check, scp, "先核对 SHA-256，再往盒子上发")
        self.assertIn('%GOTSHA%"=="%PKGSHA%', CMD)

    def test_it_installs_the_installer_that_ships_with_it(self):
        self.assertIn("/downloads/mrrc-remote-update.sh", CMD)
        self.assertIn("bash /tmp/mrrc-remote-update.sh /tmp/mrrc-src.tgz /opt/mrrc_modern", CMD)
        self.assertNotIn("mrrc-update\"", CMD, "不许调用盒子上那份会删数据的 mrrc-update")

    def test_it_asks_before_replacing_the_tree(self):
        ask = CMD.index('set /p "GO=Type YES')
        scp = CMD.index("scp -o StrictHostKeyChecking")   # 不是 `where scp`
        self.assertLess(ask, scp)
        self.assertIn('if /i not "%GO%"=="YES" goto :aborted', CMD)

    def test_success_is_decided_by_the_service_answering_again(self):
        self.assertNotIn("-skf", CMD)
        self.assertIn('-w "%%{http_code}"', CMD)
        self.assertIn('if not "%HC%"=="000" goto :done', CMD)

    def test_it_never_asks_for_the_web_password(self):
        self.assertNotIn("/api/auth/login", CMD)
        self.assertNotIn("Web password", CMD)


class WindowsScriptTests(unittest.TestCase):
    def test_the_file_is_pure_ascii(self):
        raw = CMD_PATH.read_bytes()
        bad = [i + 1 for i, line in enumerate(raw.splitlines())
               if any(byte > 0x7E for byte in line)]
        self.assertEqual(bad, [], f"有非 ASCII 行：{bad[:3]}")

    def test_labels_and_quotes_are_balanced(self):
        labels = re.findall(r"^:([A-Za-z_0-9]+)", CMD, re.M)
        self.assertEqual({l for l in labels if labels.count(l) > 1}, set(), "重复标签")
        self.assertEqual(sorted(set(re.findall(r"goto :([A-Za-z_0-9]+)", CMD)) - set(labels)),
                         [], "goto 指向了不存在的标签")
        odd = [i + 1 for i, line in enumerate(CMD.splitlines())
               if line.strip() and not line.strip().startswith("rem") and line.count('"') % 2]
        self.assertEqual(odd, [], "引号不成对的行")

    def test_it_uses_the_same_toolbox_as_the_others(self):
        self.assertIn("where curl >nul 2>&1", CMD)
        self.assertIn("where ssh", CMD)
        self.assertIn("where scp", CMD)
        self.assertIn("ConvertFrom-Json", CMD)
        self.assertIn("-Encoding UTF8", CMD)


class PublishedCopyTests(unittest.TestCase):
    def test_the_published_script_is_byte_identical(self):
        site = ROOT / "website" / "downloads" / "MRRC-Update-SSH.cmd"
        self.assertTrue(site.is_file(), "网站上还没有 MRRC-Update-SSH.cmd")
        self.assertEqual(site.read_bytes(), CMD_PATH.read_bytes())

    def test_the_guide_links_to_it(self):
        for rel in ("website/update-ssh.html", "website/zh/update-ssh.html"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("downloads/MRRC-Update-SSH.cmd", text, f"{rel} 的下载按钮")
            self.assertIn("mrrc-remote-update.sh", text, "指南要说明装的是哪个安装器")


if __name__ == "__main__":
    unittest.main()
