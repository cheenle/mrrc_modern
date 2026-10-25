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
