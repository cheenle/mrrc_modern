"""登录限流：默认仍是 5 次 / 5 分钟，但可以按盒子关掉。

为什么会有这个开关（现场，2026-11-16）：`_check_login_rate_limit()` 的键是
`request.client.host`，而那个地址只有在客户端**真的**连到服务端时才是客户端自己的。
`MRRC-BG1SB` 加了明文脱壳门（`docs/w103d_plain_relay.md`：`::8899` → socat → TLS
`127.0.0.1:8888`）之后，每个局域网浏览器都以 `127.0.0.1` 到达，而这个 loopback 桶
**同时**装着 Cloud Hub 的 frpc 隧道（frpc 也从 loopback 拨进来）——于是本地门上连错
5 次口令，公网入口 `ba4eg.mrrc.vlsc.net` 会一起被锁 5 分钟，报错只是
`Too many login attempts`，没有任何线索指向"另一扇门有人输错了"。

所以限流变成两个配置项（默认值一字不变），`MRRC_LOGIN_MAX_ATTEMPTS=0` 表示关掉，
并且启动日志用一句话说明这是降级而不是偏好。这里盯住三件事：

1. 默认值没被顺手改掉（5 / 300）；
2. env 真的能传进来（配置层）；
3. `0` 的语义是"永远放行"，而且**不记账**（否则那个 dict 会随每个 IP 无限长）。
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

import config
import server

REPO_ROOT = Path(__file__).resolve().parents[1]


def _config_value(name: str, **extra: str) -> int:
    """读出"全新解释器 + 给定 env"时 config 里的值（配置层的唯一可靠读法）。"""
    env = os.environ.copy()
    for key in ("MRRC_LOGIN_MAX_ATTEMPTS", "MRRC_LOGIN_WINDOW_S"):
        env.pop(key, None)
    env.update(extra)
    out = subprocess.check_output(
        [sys.executable, "-c", f"import config; print(config.{name})"],
        cwd=REPO_ROOT, env=env, text=True,
    )
    return int(out.strip())


class RateLimitDefaultTests(unittest.TestCase):
    def test_the_default_budget_is_unchanged(self):
        """默认值是一道安全闸门：动它要有人知道。"""
        self.assertEqual(_config_value("LOGIN_MAX_ATTEMPTS"), 5)
        self.assertEqual(_config_value("LOGIN_WINDOW_S"), 300)

    def test_the_env_can_relax_or_switch_it_off(self):
        self.assertEqual(_config_value("LOGIN_MAX_ATTEMPTS", MRRC_LOGIN_MAX_ATTEMPTS="0"), 0)
        self.assertEqual(_config_value("LOGIN_MAX_ATTEMPTS", MRRC_LOGIN_MAX_ATTEMPTS="20"), 20)
        self.assertEqual(_config_value("LOGIN_WINDOW_S", MRRC_LOGIN_WINDOW_S="60"), 60)

    def test_server_reads_the_config_values(self):
        """接线本身：server 的同名常量一旦写回字面量，这台盒子的开关就静默失效了。"""
        self.assertEqual(server._LOGIN_MAX_ATTEMPTS, config.LOGIN_MAX_ATTEMPTS)
        self.assertEqual(server._LOGIN_WINDOW_SECONDS, config.LOGIN_WINDOW_S)


class RateLimitBehaviourTests(unittest.TestCase):
    def setUp(self):
        self._saved = (server._LOGIN_MAX_ATTEMPTS, server._LOGIN_WINDOW_SECONDS)
        server._login_attempts.clear()

    def tearDown(self):
        server._LOGIN_MAX_ATTEMPTS, server._LOGIN_WINDOW_SECONDS = self._saved
        server._login_attempts.clear()

    def test_the_budget_is_spent_then_refused(self):
        server._LOGIN_MAX_ATTEMPTS, server._LOGIN_WINDOW_SECONDS = 2, 300
        self.assertTrue(server._check_login_rate_limit("192.168.1.50"))
        self.assertTrue(server._check_login_rate_limit("192.168.1.50"))
        self.assertFalse(server._check_login_rate_limit("192.168.1.50"))
        # 一个地址超限不该连坐另一个地址
        self.assertTrue(server._check_login_rate_limit("192.168.1.51"))

    def test_attempts_older_than_the_window_do_not_count(self):
        server._LOGIN_MAX_ATTEMPTS, server._LOGIN_WINDOW_SECONDS = 2, 0
        for _ in range(5):
            self.assertTrue(server._check_login_rate_limit("192.168.1.52"))

    def test_zero_means_always_allowed(self):
        server._LOGIN_MAX_ATTEMPTS = 0
        for _ in range(50):
            self.assertTrue(server._check_login_rate_limit("127.0.0.1"))

    def test_zero_keeps_no_bookkeeping(self):
        """关掉之后不该还在按 IP 记账——那个 dict 没有上限。"""
        server._LOGIN_MAX_ATTEMPTS = 0
        for i in range(20):
            server._check_login_rate_limit(f"10.0.0.{i}")
        self.assertEqual(server._login_attempts, {})

    def test_the_off_note_names_the_switch(self):
        """启动日志那一行必须点名 env，否则现场看不出是被关掉的。"""
        self.assertIn("MRRC_LOGIN_MAX_ATTEMPTS", server.LOGIN_LIMIT_OFF_NOTE)
        self.assertIn("OFF", server.LOGIN_LIMIT_OFF_NOTE)


if __name__ == "__main__":
    unittest.main()
