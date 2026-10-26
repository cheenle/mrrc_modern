"""`dev_tools/mrrc-update.cmd`：从 Windows 电脑远控盒子升级（2026-10-25）。

为什么不只是"再写一个脚本"：这条链路的两端各自在漂 —— 盒子侧改了端点名/状态名、
`server.py` 改了鉴权，脚本这边只会**安静地**跑成别的东西（客户看到的是一句
"升级失败"，我们看到的是一份没人读的 JSON）。所以这里把脚本与盒子之间的**契约**
钉成断言，而不是靠注释：

  ① 用到的四个端点必须真的在 `server.py` 里，且方法一致；
  ② 它等的状态名必须来自 `box_update` 的常量（不是手抄的字符串）；
  ③ 动手之前必须问过一次（升级会重启服务，还会被发射门禁拒绝）；
  ④ 口令**不许**出现在命令行 / 不落盘到输出目录 —— 与 `mrrc-diag.cmd` 同一条规矩；
  ⑤ 探活判据必须是 HTTP 状态码，不是 `curl -f`（2026-10-25 的看门狗回滚事故）；
  ⑥ 文件本身必须纯 ASCII：cmd.exe 按当前代码页读批处理，中文注释在 936 代码页上
     有过把下一行吃掉的历史。控制台要显示中文，靠 `chcp 65001`，不靠文件内容。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import box_update

ROOT = Path(__file__).resolve().parents[1]
CMD = (ROOT / "dev_tools" / "mrrc-update.cmd").read_text(encoding="utf-8")
SERVER = (ROOT / "server.py").read_text(encoding="utf-8")


class EndpointContractTests(unittest.TestCase):
    def test_the_endpoints_the_script_calls_exist(self):
        expected = {
            "/api/update/check": "get",
            "/api/update/apply": "post",
            "/api/update/state": "get",
            "/api/auth/login": "post",
        }
        for path, method in expected.items():
            self.assertIn(f"/api/{path.split('/api/')[1]}", CMD, f"脚本没再用 {path}")
            self.assertRegex(
                SERVER, rf'@app\.{method}\("{re.escape(path)}"',
                f"server.py 里没有 {method.upper()} {path} —— 脚本会 404")

    def test_the_states_it_waits_for_are_the_constants(self):
        for constant in (box_update.STATE_DONE, box_update.STATE_FAILED):
            self.assertIn(f'"{constant}"', CMD,
                          f"脚本不认识状态 {constant}：盒子那边改名了？")
        # 中间态只需要「不是 done/failed 就继续问」，所以断言的是那条规则。
        self.assertIn("if /i \"%ST%\"==\"failed\"", CMD)
        self.assertIn("if /i \"%ST%\"==\"done\"", CMD)


class SafetyTests(unittest.TestCase):
    def test_it_asks_before_restarting_anything(self):
        ask = CMD.index('set /p "GO=Type YES')
        apply_at = CMD.index('"%BASE%/api/update/apply"')
        self.assertLess(ask, apply_at, "确认必须在动手之前")
        self.assertIn('if /i not "%GO%"=="YES" goto :aborted', CMD)

    def test_the_password_never_reaches_a_command_line(self):
        """口令进 %TEMP% 的 JSON，用 `--data-binary @file` 带上去。

        直接写成 `-d "{\"password\":\"%MRRC_PW%\"}"` 就会出现在 argv 里
        （任务管理器 / `ps` 都看得见），而这条路最可能是在客户的机器上跑。
        """
        self.assertIn("--data-binary \"@%TEMP%\\mrrc-update-login.json\"", CMD)
        for leak in ('-d "{', "-d '{"):
            self.assertNotIn(leak, CMD, "口令被拼进了命令行")
        self.assertNotIn("%MRRC_PW%\"", CMD, "口令被拼进了命令行")
        # 三条退出路径都要删掉临时文件（成功、失败、输入缺失）
        self.assertEqual(CMD.count("del \"%TEMP%\\mrrc-update-login.json\""), 2,
                         "成功与失败两条路径都要清理临时口令文件")

    def test_a_dead_service_is_not_a_dead_network(self):
        """探活 = HTTP 状态码 != 000。

        `/api/health` 不是公开路径，盒子自己问也拿 401 —— 用 `curl -f` 判活会让
        「服务起来了」看起来像「没起来」，改成状态码之后两者才分开。
        """
        self.assertNotIn("-skf", CMD)
        self.assertIn("-w \"%%{http_code}\"", CMD)
        self.assertIn('if not "%HC%"=="000" goto :verify', CMD)

    def test_it_tells_an_old_build_apart_from_a_failed_request(self):
        self.assertIn("2026-10-18", CMD, "老构建没有升级接口，得说清楚走哪条路")
        self.assertIn("check-before.json", CMD, "请求失败时要指出原始应答在哪")


class OldBuildTests(unittest.TestCase):
    def test_it_checks_for_the_updater_before_asking_yes(self):
        """「有新版本」不等于「这台能自己升」。

        `/api/update/check` 是 2026-09-17（v1.18.0）就有的；`apply` 与
        `/api/update/state` 是 2026-10-18 才加的。所以 1.24~1.27 的盒子**会**答
        「有新版本」，然后 `apply` 404 —— 用户看到的是一句莫名其妙的「被拒绝」。
        探一次 `/api/update/state`，在问 YES 之前就把这类盒子挡住。
        """
        probe = CMD.index('"%BASE%/api/update/state"')
        ask = CMD.index('set /p "GO=Type YES')
        apply_at = CMD.index('"%BASE%/api/update/apply"')
        self.assertLess(probe, ask, "必须在确认之前就问「这台能不能自己升」")
        self.assertLess(ask, apply_at)

    def test_the_old_build_message_names_the_real_way_out(self):
        """老盒子怎么升级，得一句话说清，还包括那个会删数据的坑。

        ≤1.25.5 镜像自带的 `mrrc-update` 用 `rsync --delete`，会把 `env/`、
        `profiles/` 和录音一起删掉（CHANGELOG v1.27.0「升级注意」）。让人照着跑
        而不提这一句，等于把一次升级变成一次数据事故。
        """
        start = CMD.index("\n:old_build\n")
        block = CMD[start:CMD.index("\n:check_failed\n", start)]
        self.assertIn("2026-10-18", block)
        self.assertIn("mrrc-update", block)
        self.assertIn("1.25.5", block, "必须点名那个版本区间")
        self.assertIn("--delete", block, "必须说清旧版 mrrc-update 会删什么")


class InPlaceUpdateTests(unittest.TestCase):
    """老盒子那条路已经独立成 `MRRC-Update-SSH.cmd` —— 这里只钉住"指得对"。

    分出去的理由：那条路要在客户机上开一个 SSH 会话、以 root 身份覆盖
    `/opt/mrrc_modern`。它不该躺在一个双击就能跑工具里，也不该有两份实现
    （两份 batch 一定漂）。所以主工具只负责**说清楚去用哪一个**。
    """

    def test_the_old_build_branch_points_at_the_ssh_tool(self):
        start = CMD.index("\n:old_build\n")
        block = CMD[start:CMD.index("\n:confirm\n", start)]
        self.assertIn("MRRC-Update-SSH.cmd", block)
        self.assertIn("1.25.5", block, "必须点名那个会删数据的版本区间")
        self.assertIn("--delete", block)
        for action in ("scp ", "ssh ", "Get-FileHash"):
            self.assertNotIn(action, block, f"主工具里不该再有 {action.strip()} —— 它已经搬走")

class PromptAndCodePageTests(unittest.TestCase):
    def test_the_code_page_switch_comes_after_the_last_prompt(self):
        """`chcp 65001` 会让**后面**的 `set /p` 从重定向的 stdin 里读到空串（Win11 实测）。

        症状很难看：脚本化/远程喂两行输入，它只吃掉第一行，第二行永远为空 ——
        「Nothing was changed」，没有任何错误信息。而"远程跑它"正是这个工具存在的理由。
        所以代码页只能在最后一个 `set /p` 之后切：提示语是 ASCII，中文只出现在之后。
        """
        chcp_at = CMD.index("chcp 65001")
        prompts = [m.start() for m in re.finditer(r'set /p "', CMD)]
        self.assertTrue(prompts, "脚本至少要有口令/确认两处提示")
        self.assertGreater(chcp_at, max(prompts),
                           "chcp 在某个 set /p 之前 —— 那处提示会在重定向输入时读到空串")


class PublishedCopyTests(unittest.TestCase):
    """网站上下载到的，必须是这里测过的这一份。

    `dev_tools/` 与 `website/downloads/` 是两份文件（一份给仓库、一份给访客），
    它们之间没有任何自动同步 —— 改了仓库里那份忘了跟，访客拿到的就是旧脚本，
    而"点下载 → 双击 → 行为不对"这种问题在支持工单里极难看出来。
    """

    PAIRS = (("dev_tools/mrrc-update.cmd", "website/downloads/MRRC-Update.cmd"),
             ("dev_tools/mrrc-diag.cmd", "website/downloads/MRRC-Diag.cmd"),
             ("dev_tools/mrrc-fix-dns.cmd", "website/downloads/MRRC-FixDNS.cmd"))

    def test_the_published_scripts_are_byte_identical_to_the_repo_ones(self):
        for repo_rel, site_rel in self.PAIRS:
            repo, site = ROOT / repo_rel, ROOT / site_rel
            self.assertTrue(site.is_file(), f"网站上没有 {site_rel}")
            self.assertEqual(
                site.read_bytes(), repo.read_bytes(),
                f"{site_rel} 与 {repo_rel} 不一致 —— 重新 copy 一次")

    def test_the_guide_pages_link_to_the_published_file(self):
        for rel in ("website/update.html", "website/zh/update.html"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn('downloads/MRRC-Update.cmd', text,
                          f"{rel} 的下载按钮没指向 MRRC-Update.cmd")


class WindowsScriptTests(unittest.TestCase):
    def test_the_file_is_pure_ascii(self):
        """cmd.exe 按当前代码页读批处理：中文注释在 936 上有过吃掉下一行的记录。

        控制台上的中文（盒子的 `reason` / `detail`）靠 `chcp 65001` + PowerShell
        的 `[Console]::OutputEncoding`，与文件本身是不是 ASCII 无关。
        """
        raw = (ROOT / "dev_tools" / "mrrc-update.cmd").read_bytes()
        bad = [(i + 1, line) for i, line in enumerate(raw.splitlines())
               if any(byte > 0x7E for byte in line)]
        self.assertEqual(bad, [], f"有非 ASCII 行：{bad[:3]}")

    def test_it_uses_the_same_toolbox_as_the_collector(self):
        """curl.exe + PowerShell 5.1 的 ConvertFrom-Json —— 与 mrrc-diag.cmd 一致。

        客户机器上能指望的只有这两样；引入别的东西就得先教人装。
        """
        self.assertIn("where curl >nul 2>&1", CMD)
        self.assertIn("ConvertFrom-Json", CMD)
        self.assertIn("-Encoding UTF8", CMD, "PowerShell 5.1 默认按 ANSI 读文件，中文会乱")


if __name__ == "__main__":
    unittest.main()


class WindowsCmdHygieneTests(unittest.TestCase):
    """**没有 BOM、默认带端口** —— 两个只在别人电脑上出现的错误。

    2026-10-25 现场（BG1SB 那台修 DNS）：`MRRC-FixDNS.cmd` 被写成了带 UTF-8 BOM，
    cmd.exe 把 BOM 拼进第一行：

        C:\\Users\\Administrator\\Desktop>锘緻echo off
        '锘緻echo' 不是内部或外部命令，也不是可运行的程序或批处理文件。

    `@echo off` 因此失效（整屏回显），而它同时**没有补 8888 端口** —— 用户从浏览器
    地址栏抄来的是裸 IP，被当成 443 去打，于是第二句错是 `No answer from 192.168.30.101`。
    两个都不是逻辑错误，所以常规测试看不见它们。
    """

    def test_no_published_cmd_starts_with_a_bom(self):
        for path in sorted((ROOT / "website" / "downloads").glob("*.cmd")):
            with self.subTest(name=path.name):
                self.assertFalse(
                    path.read_bytes().startswith(b"\xef\xbb\xbf"),
                    f"{path.name} 带 UTF-8 BOM：cmd.exe 会把 BOM 当成第一行命令的一部分")

    def test_repo_copies_are_bom_free_too(self):
        for path in sorted((ROOT / "dev_tools").glob("*.cmd")):
            with self.subTest(name=path.name):
                self.assertFalse(path.read_bytes().startswith(b"\xef\xbb\xbf"),
                                 f"{path.name} 带 UTF-8 BOM")

    def test_no_cmd_carries_non_ascii_bytes(self):
        """`cmd.exe` 按**当前代码页**读批处理文件。

        写一句中文注释，就等于在 936 代码页的机器上掷一次骰子：那些 UTF-8 字节
        会被重组成任意字符 —— 包括 `&` 和引号。2026-10-25 实测，一句
        `「No answer from 192.168.30.101」` 被咬成了
        `'nswer' 不是内部或外部命令`，而报错的位置离那一行差了 60 行。
        控制台要显示中文，靠 `chcp 65001` 与运行时字符串，不靠文件内容。
        """
        for folder in ("dev_tools", "website/downloads"):
            for path in sorted((ROOT / folder).glob("*.cmd")):
                with self.subTest(name=f"{folder}/{path.name}"):
                    bad = [b for b in path.read_bytes() if b > 0x7F]
                    self.assertEqual(bad, [], f"{path.name} 含 {len(bad)} 个非 ASCII 字节")

    def test_any_non_200_answer_counts_as_an_old_build(self):
        """1.28.6 之前的盒子对 POST /api/box/fix-dns 回 **405**，不是 404。

        路由不存在时 FastAPI 落到只注册了 GET 的静态处理器 —— 只认 404 的写法
        会继续往下解析一个空 JSON，最后打出一句没有下文的 `FAILED:`，而正确
        的下一步（SSH）就在隔壁分支。
        """
        text = (ROOT / "dev_tools" / "mrrc-fix-dns.cmd").read_text(encoding="utf-8")
        self.assertIn('if not "%FIXAPI%"=="200" goto :too_old', text)
        self.assertNotIn('if "%FIXAPI%"=="404" goto :too_old', text)

    def test_box_tools_default_to_the_box_port(self):
        """用户抄给工具的是裸 IP；8888 得由工具自己补。

        桌面工具（CertDoctor / Backup / Cleanup …）不访问盒子 HTTP，不在此列。
        """
        for name in ("MRRC-FixDNS.cmd", "MRRC-Diag.cmd", "MRRC-Update.cmd",
                     "MRRC-Update-SSH.cmd"):
            text = (ROOT / "website" / "downloads" / name).read_text(
                encoding="utf-8", errors="replace")
            with self.subTest(name=name):
                self.assertIn('find ":"', text,
                              f"{name} 没有端口补全：裸 IP 会去敲 443")
                self.assertIn(":8888", text)
