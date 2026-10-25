"""The Windows repair tool: `dev_tools/mrrc-fix-dns.cmd`.

Shipped boxes built before 2026-10-25 carry the *build machine's* /etc/resolv.conf
inside the image, because `packaging/box/build-image.sh` lends the chroot that file
so apt/pip can resolve and the base image's stub symlink is never put back. Measured
on a delivered box (MRRC-BG1SB, image built 2026-10-11): ``nameserver 192.168.65.7``
— Docker Desktop's internal resolver, an address that exists on no customer LAN.
glibc sends every lookup there, waits 5 s and fails, while ``ping <ip>`` works.

The tool must work for a box whose **root password was changed** (the user guide
tells customers to change it), so it authenticates with the web password and asks
the box's own root helper to run the repair. These tests pin the parts of that
sentence a customer would otherwise discover the hard way.
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CMD_PATH = ROOT / "dev_tools" / "mrrc-fix-dns.cmd"
CMD = CMD_PATH.read_text(encoding="utf-8")
ENDPOINT = "/api/box/fix-dns"


class IndexAndDeployTests(unittest.TestCase):
    def test_the_site_homepage_lists_it(self):
        """「整体的索引页」= 首页那个 W103D 按钮区：只挂在 W103D 专题里，别人不会走到。"""
        for rel in ("website/index.html", "website/zh/index.html"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn('href="fix-dns.html"', text, f"{rel} 没有指向 DNS 修复页")

    def test_the_deploy_script_publishes_the_tool(self):
        """`downloads/` 被排除在部署包之外，所以 .cmd 必须单独上传 —— 以前靠人记，
        结果页面承诺的文件站上可能没有（2026-10-25 本次就是手工补传的）。"""
        text = (ROOT / "website" / "deploy.sh").read_text(encoding="utf-8")
        self.assertIn("MRRC-FixDNS.cmd", text)
        self.assertIn("/tmp/mrrc-tools", text)
        # 本地先对齐仓库与站上两份拷贝（测试另有逐字节断言）。
        self.assertIn("mrrc-fix-dns.cmd:MRRC-FixDNS.cmd", text)

class EndpointContractTests(unittest.TestCase):
    def test_the_endpoint_the_script_calls_exists(self):
        server_source = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn(f'@app.post("{ENDPOINT}"', server_source)
        self.assertIn(ENDPOINT, CMD)

    def test_the_page_path_is_reachable_without_a_root_password(self):
        """The whole point: the repair goes through the box's root helper, which
        trusts the `mrrc` user over its socket. Nothing here uses ssh."""
        self.assertNotIn("ssh.exe", CMD)
        self.assertNotIn("plink", CMD)
        self.assertNotIn("abcd1234", CMD, "出厂 root 口令不该出现在这个工具里")

    def test_it_only_asks_for_the_web_password(self):
        self.assertIn('set /p "MRRC_PW=Web password', CMD)
        self.assertEqual(CMD.count("set /p"), CMD.count("set /p"), "prompt count is informational")
        # 唯一的人机输入是盒子地址与网页口令：多一个"root 口令"就是在骗人。
        self.assertNotIn("root password (", CMD.lower())

    def test_a_listen_only_session_is_refused_with_a_sentence(self):
        """MRRC_LISTEN_PASSWORD 的会话只能读：脚本必须说清楚，而不是报一个 500。"""
        self.assertIn("listen-only", CMD)


class OldBoxTests(unittest.TestCase):
    def test_a_404_sends_the_operator_to_the_seller_not_to_a_shell(self):
        """比这个修复更早的盒子没有那个接口。

        这里**刻意不印任何 Linux 命令**（2026-10-25 现场反馈）：跑这个工具的人往往
        是客户，`ln -sfn` / `systemctl` 只会把他劝退。工具做的是"如实说这不支持、
        把这句话发给卖家"；手工三行留在 `docs/W103D_GUIDE.md` §20 给会 SSH 的运维。
        """
        self.assertIn("404", CMD)
        self.assertIn("predates the repair", CMD)
        self.assertIn("Send this screen to the seller", CMD)
        for shell in ("ln -sfn", "systemctl", "cp -a", "ssh "):
            with self.subTest(shell=shell):
                self.assertNotIn(shell, CMD)


class SafetyTests(unittest.TestCase):
    def test_the_second_run_proves_idempotence(self):
        """修好之后脚本再跑一次，必须看到 "already a symlink"。这是"修没修好"的
        机器判据，不需要操作员去读 resolvectl。"""
        self.assertIn("already a symlink", CMD)
        self.assertIn("fix2.json", CMD)

    def test_the_password_never_reaches_a_command_line(self):
        """Windows 上 curl 的 argv 在同机的进程列表里可见；口令走一次性的 JSON 文件，
        用完即删（与 mrrc-update.cmd 同一手法）。"""
        self.assertIn("%TEMP%\\mrrc-fixdns-login.json", CMD)
        self.assertIn('del "%TEMP%\\mrrc-fixdns-login.json"', CMD)
        self.assertNotIn('-d "password=', CMD)

    def test_it_cleans_up_after_itself(self):
        self.assertIn("rd /s /q \"%OUT%\"", CMD)


class WindowsScriptTests(unittest.TestCase):
    def test_the_file_is_pure_ascii(self):
        """cmd.exe 按当前代码页读批处理；非 ASCII 有过吃掉下一行的记录
        （tests/test_box_update_cmd.py 里那条注释的同一原因）。"""
        bad = [(i + 1, line) for i, line in enumerate(CMD_PATH.read_bytes().splitlines())
               if any(byte > 0x7E for byte in line)]
        self.assertEqual(bad, [], f"有非 ASCII 行：{bad[:3]}")

    def test_it_uses_only_what_windows_ships(self):
        self.assertIn("where curl >nul 2>&1", CMD)
        self.assertIn("ConvertFrom-Json", CMD)
        self.assertIn("-Encoding UTF8", CMD)


class GuidePageTests(unittest.TestCase):
    def test_the_guide_page_links_to_the_published_file(self):
        for rel in ("website/fix-dns.html", "website/zh/fix-dns.html"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("downloads/MRRC-FixDNS.cmd", text, f"{rel} 的下载按钮没指向工具")

    def test_the_w103d_index_lists_it(self):
        for rel in ("website/w103d-index.html", "website/zh/w103d-index.html"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("fix-dns.html", text, f"{rel} 没有把 DNS 修复页列进索引")


if __name__ == "__main__":
    unittest.main()

class CustomerPageTests(unittest.TestCase):
    def test_the_guide_stays_on_windows(self):
        """客户不会 Linux。页面里一旦出现 `ls` / `ln` / `systemctl` / `ssh`，
        他就要么照抄出错、要么直接放弃 —— 这两种都比"看一句截图发给卖家"差。"""
        forbidden = ("ls -l", "cat /etc", "ln -sfn", "resolvectl", "systemctl", "ssh ")
        for rel in ("website/fix-dns.html", "website/zh/fix-dns.html"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            for shell in forbidden:
                with self.subTest(page=rel, shell=shell):
                    self.assertNotIn(shell, text)

    def test_the_operator_route_lives_in_the_technical_guide(self):
        """三行命令必须有个"会 SSH 的人才看"的家：从零指南的排障表。"""
        text = (ROOT / "docs" / "W103D_GUIDE.md").read_text(encoding="utf-8")
        self.assertIn("ln -sfn ../run/systemd/resolve/stub-resolv.conf", text)
        self.assertIn("try-restart systemd-resolved", text)


if __name__ == "__main__":
    unittest.main()
