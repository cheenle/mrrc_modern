# 远程协助通道（Call help）实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 客户在应用里点一次，开通一条**限时、可随时关闭**的 SSH 通道；支持人员在 hub 上用原生 `ssh` 拿到一个真 shell。凭据由客户侧生成，portal 结构上看不到口令。

**架构：** 应用内 `help_session.py` 起一个只绑 `127.0.0.1` 的 paramiko SSH 服务；它向 portal 申请一个协助端口，把**第二个代理**写进既有 frpc 配置并用 admin API 热加载；portal 记录生命周期、展示入口与命令、并能强制结束（靠实例 20 秒一次的心跳回读）。

**技术栈：** Python 3.11（paramiko 新增）、FastAPI（既有）、stdlib `http.server` + htmx（portal 既有）、frp 0.71.0（既有）、systemd/launchd/PyInstaller（既有四条打包线）。

**设计依据：** `docs/superpowers/specs/2026-10-12-remote-help-channel-design.md`（已提交 `79ec805`）。

---

## 执行状态（2026-10-12，阶段性收尾）

**15/16 完成，全部离线可验的部分已落地并提交；任务 16 停在需要人在场的生产动作上。**

| 任务 | 提交 | 说明 |
| --- | --- | --- |
| 1 依赖与骨架 | `620a81d` | paramiko 顶层 import（与 cryptography 一致：缺依赖在启动时炸） |
| 2 凭据 | `ce991dd` | 生成 / 常量时间比较 / 客户自定义（四种组合语义明确） |
| 3+4 状态机与审计 | `7c44491` | 五个可注入协作者；arming 失败也走同一个 `close()` |
| 5 SSH 服务 | `1848919` | 回环、只口令、平台 shell；真客户端集成 10 条 |
| 6 隧道配置 | `8ef2e4c` | `webServer` 管理段 + 按实例命名的协助代理 + **0600** |
| 7+8 reload 与 set_help_proxy | `4a76ddb` | reload 走 admin API；配置从文件读回，主代理不可能被弄丢 |
| 9 端点与接线 | `f3f7de6` | `/api/help/{enable,disable,status}`；出站改走 `net_tls.urlopen` |
| 10 Support 卡 | `996f7a1` | 开启/口令复制/倒计时/停止；状态以服务端为准 |
| 11–13 门户侧 | `f79ec93` `f2a9ac1` | 记录（无口令字段）、端口池共用、三端点、卡片与强制结束 |
| 14 心跳 | `3171a23` | 先本地判到期、再读门户 stop；门户不可达不提前关 |
| 15 口令红线 | `a0afc5f` | 六个载体各一条扫描（第一版是空转的，被自己的断言抓住） |
| 16 全链路与文档（离线部分） | `f00c469` `53c53b0` | 应用侧端到端集成 + `docs/remote_help.md` |

**离线证据**：应用侧 2160 条测试（本线新增 64 条），门户侧 6 个脚本 102 组检查，全绿。
应用侧唯一一条红是打包线那条已解释的 W103D 卡片（与本线无关）。

**任务 16 已完成**（2026-10-12 02:15–02:35 CST，真机、生产门户）：
门户已部署（`f79ec93`+`f2a9ac1`，4 个文件 md5 与本地同名，回滚点在 `/opt/mrrc-portal.bak-20261012-021634`）；
五条判据里 1–4 全过（含 portal 强制结束 20 秒内生效、主隧道全程不抖、交互式会话跑通），
第 5 条（盒子/Pi 上的权限边界）仍待真机。六处真缺陷与修法见 `docs/remote_help.md` 与提交
`e68481c`、`1a8e173`。

**镜像侧（2026-10-12 已完成）**：W103D 与 Pi 两条镜像线都按 v1.26.0 重建并通过四步产物验证；
共用服务层的第一次真机考验（设置页那套要同时进两条线）拓出一处真缺陷——Wi-Fi 预置的启动分区
路径被写死，Pi 上会静默哑掉——已修并入守卫。

**仍然待你做的**（不在本计划内）：

1. 把门户侧改动**部署到 hub**（`f79ec93` + `f2a9ac1`）——否则应用的申请会 404；
2. 让实例**重写一次隧道配置**（`webServer` 段是新增的，旧 toml 里没有它，reload 无处可发）；
3. 跑 `docs/remote_help.md` 里的五条判据，重点是第 3 条：**开着实例网页确认主隧道不抖**
   （这条验的就是"用 reload 而不是重启"那个决定），以及第 5 条：盒子上 `mrrc` 用户的权限边界。

**待做（发版时一起）**：SDD 仪式（新 AD 与 NFR：远程访问面）；客户向文档；P2 门户网页终端；
P3"复用系统账号"高级模式。

---

## 门户上线清单（**已备好，未执行**；照 `mrrc_hub/deploy/README.md` 的既有顺序）

现网 hub = `cheenle@www.vlsc.net`（有 NOPASSWD sudo），portal 跑在 `127.0.0.1:8890`、
`User=mrrcportal`，代码落点 `/opt/mrrc-portal/`。

```bash
# ① 推送（注意：会带上门户仓库里已有的 17 个未推送提交，见下）
cd /Users/cheenle/HAM/hub/mrrc_hub && git push origin main

# ② 上传到 hub 的 /tmp 并两侧核哈希
cd /Users/cheenle/HAM/hub/mrrc_hub
tar czf /tmp/mrrc-portal-help.tgz portal tests deploy
shasum -a 256 /tmp/mrrc-portal-help.tgz
scp /tmp/mrrc-portal-help.tgz cheenle@www.vlsc.net:/tmp/
ssh cheenle@www.vlsc.net 'sha256sum /tmp/mrrc-portal-help.tgz'      # 与上一行一致才继续

# ③ 在生产机上、以真正的服务账号跑全套测试（全绿才动现网）
ssh cheenle@www.vlsc.net 'cd /tmp && rm -rf mrrc-hub-check && mkdir mrrc-hub-check \
  && tar xzf mrrc-portal-help.tgz -C mrrc-hub-check \
  && cd mrrc-hub-check && for t in tests/test_*.py; do sudo -u mrrcportal python3 "$t" || exit 1; done'

# ④ 备份 → 安装 → 重启（回滚一条命令，见下）
ssh cheenle@www.vlsc.net 'ts=$(date +%Y%m%d-%H%M%S); echo "$ts" > /tmp/mrrc-portal-backup-ts \
  && sudo cp -a /opt/mrrc-portal /opt/mrrc-portal.bak-$ts \
  && sudo rsync -a --chown=mrrcportal:mrrcportal /tmp/mrrc-hub-check/portal/ /opt/mrrc-portal/portal/ \
  && sudo systemctl restart mrrc-portal && sleep 2 && systemctl is-active mrrc-portal'

# ⑤ 上线后逐项复验（这次改动的那几条）
curl -sI https://portal.mrrc.vlsc.net/admin/static/htmx.min.js | head -1     # 200：静态资源没漏装
curl -s  -X POST https://portal.mrrc.vlsc.net/help/request -d 'callsign=BG7IFP&token=x' | head -c 120
#     ↑ 期望 403「申请令牌无效」——路由存在且令牌门在工作（404 说明没装上）
ssh cheenle@www.vlsc.net 'journalctl -u mrrc-portal --since "-5 min" | tail -20'

# 回滚：sudo rsync -a --delete /opt/mrrc-portal.bak-<ts>/portal/ /opt/mrrc-portal/portal/ && sudo systemctl restart mrrc-portal
```

**推送前要你点头的一件事**：`mrrc_hub` 的 `main` 比 `origin/main` 领先 **19 个提交**——
本线 2 个，另有 **17 个是你自己的门户界面改版那一批**（`a5b2103` … `31160b1`）。
`git push` 是整条分支，所以要么全推，要么先只推这条线（可以用一个临时分支或 `git push origin <sha>:main`）。

**实例侧**（不用部署到 hub）：验收机器上要跑**含本线代码**的应用；并且要让它**重新接入一次
Cloud Hub**，好把 `webServer` 管理段写进 `frpc-<label>.toml`（旧配置里没有它，reload 无处可发）。


---

## 全局约束

- **两个仓库**：应用侧在 `HAM/hub/mrrc_modern`（本计划多数任务），门户侧在 `HAM/hub/mrrc_hub`（任务 11–13）。两边各自提交、各自跑测试。
- **口令的红线**（设计 §6 围栏 2）：口令不得出现在配置、日志、支持包、portal 请求体、异常消息里。每个任务碰凭据时都要想一遍这条，任务 15 有专门的扫描测试。
- **不做提权、不做常驻、不做网页终端**（P2）、不动 frps 的 `allowPorts`。
- **测试必须在 venv 里跑**：`.venv/bin/python -m unittest discover -s tests`（系统 `python3` 会给出大量假故障）。
- 提交信息用中文，沿用仓库风格；每个任务单独提交。
- **`frpc reload` 的行为必须在真机上确认**（任务 16）；在确认之前，任何文档不得写成"已验证"。

---

## 文件结构

**应用侧（`mrrc_modern`）**

| 文件 | 职责 |
| --- | --- |
| `help_session.py`（新） | 状态机 + 凭据 + paramiko SSH 服务 + 审计 + 与 portal/隧道 的协调。**唯一**关闭路径 `close(reason)` |
| `tests/test_help_session.py`（新） | 状态机/凭据/限速/审计（假时钟、假 portal）+ 真 paramiko 客户端集成 |
| `cloud_hub.py` | `frpc_config_text()` 加管理段与可选的第二个代理；新增 `reload_frpc()`；配置文件改 0600 |
| `tests/test_cloud_hub.py`（改） | 上述三点的断言 |
| `server.py` | `/api/help/{enable,disable,status}` 三个端点（登录用户可用的**本地**动作） |
| `tests/test_server_help.py`（新） | 端点权限、状态回报、enable/disable 往返 |
| `static/support.html` | "远程协助"卡片（内联样式与脚本，与页面既有风格一致） |
| `requirements.txt` | `paramiko` |
| `tests/test_help_leak_guard.py`（新） | 口令不落盘/不进日志/不进支持包/不进 portal 请求体 |
| `support_bundle.py` | 把审计事件带进诊断包 |

**门户侧（`mrrc_hub`）**

| 文件 | 职责 |
| --- | --- |
| `portal/registry.py` | 端口记录加 `kind`（`instance` / `help`）；协助端口的释放规则 |
| `portal/store.py` | `HelpSession` 记录（**没有口令字段**） |
| `portal/app.py` | 实例侧三个端点 `/help/request` `/help/heartbeat` `/help/close`；admin 的强制结束动作 |
| `portal/views.py` | 实例详情页的"远程协助"卡 |
| `tests/test_portal_help.py`（新） | 端点认证/拒绝分支/心跳 stop/释放规则/卡片渲染/CSRF |

---

## 任务 1：【app】依赖与模块骨架

**文件：**

- 修改：`requirements.txt`
- 创建：`help_session.py`

- [ ] **步骤 1：加依赖**

在 `requirements.txt` 的依赖清单里按字母序加一行（注释说明为什么值得引入）：

```
# Call help: the customer-initiated SSH door. Uniform across the four platforms — Windows has no
# sshd to fall back on — and cryptography (already a dependency) carries the crypto.
paramiko
```

- [ ] **步骤 2：写模块骨架（只有契约，先不实现）**

创建 `help_session.py`：

```python
"""Call help: the customer's own door to this instance, open only when they ask.

Model
-----
The customer presses one button. This module then: makes a fresh credential pair,
binds an SSH server on loopback, asks the portal for a tunnel port, adds a second
proxy to the running frpc through its admin API, and starts a 20 s heartbeat that
carries the portal's "stop" back. Three things can end it — the customer, the
window, the portal — and all three run the same ``close(reason)``.

What is deliberately absent
---------------------------
* **No persistence.** The credentials live in this process only. A restart ends
  help, which is the point: there is no stored password to forget to expire.
* **No privilege.** The session runs as this process's user and gets this
  process's environment, because that is what a supporter needs to look at; it
  grants nothing the app's own user does not already have.
* **No public listener.** The bind is loopback, and the tunnel's far end is
  loopback too (frps binds proxy ports on 127.0.0.1), so nothing here is
  reachable from the internet.
* **No password in any log, config, support bundle or portal request body.** The
  portal's request schema has no field it could travel in.
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

#: Hard ceiling, whatever a caller asks for. No "until I say stop" option: the
#: classic failure of this feature is forgetting to turn it off.
MAX_WINDOW_MINUTES = 180
DEFAULT_WINDOW_MINUTES = 60
#: Failed authentications before the whole session shuts down.
MAX_AUTH_FAILURES = 5
#: How often the instance tells the portal it is still here, and asks whether to stop.
HEARTBEAT_SECONDS = 20
#: Loopback, always. A test asserts this is not "0.0.0.0" or "".
BIND_HOST = "127.0.0.1"

#: States. ``closed`` exists so a caller can read the reason after the fact.
OFF, ARMING, OPEN, CLOSED = "off", "arming", "open", "closed"
```

- [ ] **步骤 3：跑一下导入与测试套件**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
.venv/bin/pip install -q paramiko && .venv/bin/python -c "import help_session, paramiko; print(paramiko.__version__)"
.venv/bin/python -m unittest discover -s tests 2>&1 | tail -3
```

预期：打印 paramiko 版本；套件只余既有的几条红（SDD 徽标、W103D 卡片那类），不新增。

- [ ] **步骤 4：Commit**

```bash
git add requirements.txt help_session.py
git commit -m "feat(help): paramiko 依赖与 help_session 骨架（契约与围栏先落）"
```

---

## 任务 2：【app】凭据：生成、比较、不落任何载体

**文件：**

- 修改：`help_session.py`
- 创建：`tests/test_help_session.py`

- [ ] **步骤 1：写失败的测试**

创建 `tests/test_help_session.py`：

```python
"""Call help: the credential rules first, because everything else is replaceable.

The password is the one value in this feature that must never come to rest. These
tests are written before the code so the rules are the spec rather than a
description of what got built.
"""
from __future__ import annotations

import re
import unittest

import help_session


class CredentialTests(unittest.TestCase):
    def test_a_fresh_pair_every_time(self):
        a = help_session.make_credentials()
        b = help_session.make_credentials()
        self.assertNotEqual(a, b)
        self.assertNotEqual(a[0], b[0], "the username is fresh too")

    def test_the_password_is_long_and_urlsafe(self):
        user, pw = help_session.make_credentials()
        self.assertGreaterEqual(len(pw), 20)
        self.assertTrue(re.fullmatch(r"[A-Za-z0-9_\-]+", pw), pw)
        self.assertTrue(re.fullmatch(r"help-[0-9a-f]{4}", user), user)

    def test_the_username_is_typeable(self):
        """It gets typed at a hub shell by a human, so: short, lowercase, no oddities."""
        user, _ = help_session.make_credentials()
        self.assertLessEqual(len(user), 12)

    def test_compare_is_constant_time(self):
        """Source-level, because timing itself is not testable here."""
        import inspect
        src = inspect.getsource(help_session)
        self.assertIn("hmac.compare_digest", src)
        self.assertNotIn("== password", src)


class LeakTests(unittest.TestCase):
    """Nothing about the password may reach a carrier that outlives the session."""

    def test_the_module_never_writes_it(self):
        import inspect
        src = inspect.getsource(help_session)
        for forbidden in ("write_text", "json.dump", "logger.info(f\"{pw"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, src)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **步骤 2：跑它，确认红**

```bash
.venv/bin/python -m unittest tests.test_help_session -v 2>&1 | tail -8
```

预期：`AttributeError: module 'help_session' has no attribute 'make_credentials'`。

- [ ] **步骤 3：实现**

在 `help_session.py` 的常量区之后加：

```python
def make_credentials() -> tuple[str, str]:
    """(username, password), fresh for every enable.

    The username is short on purpose: a human types it at a hub shell from a phone
    call. The password is the only thing that stops somebody else on the hub (or a
    process on this machine) from walking in, so it is long and random — and it
    never leaves this process except through the customer's eyes.
    """
    return "help-" + secrets.token_hex(2), secrets.token_urlsafe(18)


def _usable_username(name: str) -> bool:
    """A username a human can type from a phone call: bounded, no whitespace, no
    shell metacharacters. Not a security boundary (the password is), just hygiene."""
    import re
    return bool(re.fullmatch(r"[A-Za-z0-9_.\-]{3,32}", name or ""))


def credentials_match(expected_user: str, expected_pw: str,
                      got_user: str, got_pw: str) -> bool:
    """Constant-time compare of both fields.

    Both are compared (never short-circuiting on the username) so a probe cannot
    learn that the username was right by timing the answer.
    """
    ok_user = hmac.compare_digest(expected_user, got_user)
    ok_pw = hmac.compare_digest(expected_pw, got_pw)
    return ok_user and ok_pw
```

- [ ] **步骤 4：跑测试，确认全绿**

```bash
.venv/bin/python -m unittest tests.test_help_session -v 2>&1 | tail -6
```

预期：6 条全过。

- [ ] **步骤 5：Commit**

```bash
git add help_session.py tests/test_help_session.py
git commit -m "feat(help): 凭据生成与常量时间比较（含不落盘的类型测试）"
```

---

## 任务 3：【app】状态机与唯一关闭路径

**文件：**

- 修改：`help_session.py`
- 修改：`tests/test_help_session.py`

- [ ] **步骤 1：写失败的测试**

追加到 `tests/test_help_session.py`：

```python
class StateMachineTests(unittest.TestCase):
    """off → arming → open → closed, with one way out from every state.

    The failure this guards is the half-cleaned one: an arming that bound a socket
    and then failed to get a tunnel port, returning to `off` and leaving the
    listener behind.
    """

    def _session(self, **kw):
        events = []
        s = help_session.HelpSession(
            opener=lambda local_port: 19000,          # 假 portal：直接给端口
            tunnel=lambda port, add: None,            # 假隧道
            listener=lambda: 34567,                   # 假监听：直接给端口
            clock=lambda: self.now,
            on_event=events.append,
            **kw)
        return s, events

    def setUp(self):
        self.now = 1_000_000.0

    def test_it_starts_off(self):
        s, _ = self._session()
        self.assertEqual(s.state, help_session.OFF)

    def test_enable_opens_and_reports_the_command(self):
        s, _ = self._session()
        st = s.enable(minutes=30)
        self.assertEqual(s.state, help_session.OPEN)
        self.assertEqual(st["port"], 19000)
        self.assertEqual(st["expires_at"], self.now + 30 * 60)
        self.assertTrue(st["username"].startswith("help-"))
        self.assertNotIn("password", st, "the status dict has no password field")

    def test_arming_failure_still_goes_through_close(self):
        """绑定成功、申请隧道失败 ⇒ 必须把监听拆掉。"""
        s, events = self._session()
        s.listener = lambda: 34567
        s.tunnel = lambda port, add: (_ for _ in ()).throw(RuntimeError("reload failed"))
        with self.assertRaises(RuntimeError):
            s.enable(minutes=30)
        self.assertEqual(s.state, help_session.CLOSED)
        self.assertEqual(s.close_reason, "error")
        self.assertIn("closed", [e["event"] for e in events])

    def test_expiry_closes_it(self):
        s, _ = self._session()
        s.enable(minutes=30)
        self.now += 30 * 60 + 1
        self.assertEqual(s.tick(), "expired")
        self.assertEqual(s.state, help_session.CLOSED)

    def test_it_does_not_auto_renew(self):
        """到期就是到期：没有"自动续期"这条路。"""
        s, _ = self._session()
        s.enable(minutes=30)
        self.now += 30 * 60 + 1
        s.tick()
        self.assertEqual(s.tick(), None)

    def test_the_window_has_a_ceiling(self):
        s, _ = self._session()
        st = s.enable(minutes=10_000)
        self.assertEqual(st["expires_at"] - self.now,
                         help_session.MAX_WINDOW_MINUTES * 60)

    def test_close_is_idempotent(self):
        s, _ = self._session()
        s.enable(minutes=30)
        s.close("user")
        s.close("portal")
        self.assertEqual(s.close_reason, "user", "first reason wins")

    def test_a_second_enable_while_open_is_refused(self):
        s, _ = self._session()
        s.enable(minutes=30)
        with self.assertRaises(RuntimeError):
            s.enable(minutes=30)
```

- [ ] **步骤 2：跑它，确认红**

```bash
.venv/bin/python -m unittest tests.test_help_session.StateMachineTests 2>&1 | tail -6
```

预期：`AttributeError: ... has no attribute 'HelpSession'`。

- [ ] **步骤 3：实现状态机**

在 `help_session.py` 追加：

```python
@dataclass
class HelpSession:
    """One help window. Never reused: close it, and the next enable is a new object.

    Four collaborators, all injectable, because every one of them is a thing that
    fails in the field and none of them should need a tunnel to test:

    * ``opener``   — ask the portal for a tunnel port: ``(minutes) -> remote_port``
    * ``tunnel``   — add/remove the frpc proxy: ``(local_port, remote_port, add) -> None``
    * ``listener`` — bind the SSH server on loopback: ``() -> local_port``
    * ``clock``    — injectable time, so the window is testable without waiting
    """

    opener: Callable[[int], int]
    tunnel: Callable[[int, int, bool], None]
    listener: Callable[[], int]
    clock: Callable[[], float] = time.time
    on_event: Optional[Callable[[dict], None]] = None

    state: str = field(default=OFF, init=False)
    close_reason: str = field(default="", init=False)

    _user: str = field(default="", init=False, repr=False)
    _pw: str = field(default="", init=False, repr=False)
    _local_port: int = field(default=0, init=False)
    _remote_port: int = field(default=0, init=False)
    _expires_at: float = field(default=0.0, init=False)
    _failures: int = field(default=0, init=False)
    connections: int = field(default=0, init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    # ── lifecycle ──────────────────────────────────────────────────────
    def enable(self, *, minutes: int = DEFAULT_WINDOW_MINUTES,
               username: str = "", password: str = "") -> dict:
        """Open the window. Raises on any failure, and leaves nothing behind.

        The customer may bring their own credentials (that is what "in the
        customer's hands" means); leaving them empty generates a pair. A supplied
        username is only checked for shape — it is typed by a human at a hub shell.
        """
        with self._lock:
            if self.state in (ARMING, OPEN):
                raise RuntimeError("a help session is already open")
            self.state = ARMING
            self.close_reason = ""
            self._failures = 0
            self.connections = 0
            try:
                if username:
                    if not _usable_username(username):
                        raise ValueError("username must be 3-32 chars, [A-Za-z0-9_.-]")
                    self._user = username
                    self._pw = password or make_credentials()[1]
                else:
                    self._user, self._pw = make_credentials()
                if len(self._pw) < 8:
                    raise ValueError("password must be at least 8 characters")
                self.server.set_credentials(self._user, self._pw)
                self._local_port = int(self.listener())
                window = max(1, min(int(minutes), MAX_WINDOW_MINUTES))
                self._remote_port = int(self.opener(window))
                self.tunnel(self._local_port, self._remote_port, True)
                self._expires_at = self.clock() + window * 60
                self.state = OPEN
                self._emit("opened", user=self._user, port=self._remote_port, minutes=window)
                return self.status()
            except BaseException as exc:
                # Every failure path leaves through the same door, so a bound socket
                # or a half-written proxy cannot survive an exception here.
                self.close("error")
                raise exc

    def tick(self) -> Optional[str]:
        """Called by the heartbeat. Returns the reason if this tick closed the session."""
        with self._lock:
            if self.state != OPEN:
                return None
            if self.clock() >= self._expires_at:
                self.close("expired")
                return "expired"
            return None

    def close(self, reason: str) -> None:
        """The only way out. Idempotent; the first reason wins."""
        with self._lock:
            if self.state in (OFF, CLOSED):
                self.state = CLOSED
                return
            self.close_reason = self.close_reason or reason
            was_open = self.state == OPEN
            self.state = CLOSED          # 没有中间态：清理是同步的，且 close 可重入
            try:
                if self._local_port and self._remote_port:
                    try:
                        self.tunnel(self._local_port, self._remote_port, False)
                    except Exception as exc:            # 记下来，但不阻断清理
                        logger.warning("help: cannot remove the tunnel proxy: %s", exc)
                        self._emit("tunnel_removal_failed", error=str(exc))
                self._close_listener()
            finally:
                self._user = self._pw = ""
                self.state = CLOSED
                if was_open:
                    self._emit("closed", reason=self.close_reason)

    # ── reporting ──────────────────────────────────────────────────────
    def status(self) -> dict:
        """Everything the UI shows. **No password**: the UI reads it from
        ``password_for_display()`` while the session is open and nowhere else."""
        with self._lock:
            return {
                "state": self.state,
                "username": self._user,
                "port": self._remote_port,
                "expires_at": self._expires_at,
                "expires_in_s": max(0, int(self._expires_at - self.clock())) if self.state == OPEN else 0,
                "connections": self.connections,
                "failures": self._failures,
                "close_reason": self.close_reason,
            }

    def password_for_display(self) -> str:
        """The one accessor for the password, named so a reader sees why it exists:
        the customer has to read it out loud, so it must be visible while open."""
        with self._lock:
            return self._pw if self.state == OPEN else ""

    def _emit(self, event: str, **kv: Any) -> None:
        if self.on_event:
            self.on_event({"at": self.clock(), "event": event, **kv})

    def _close_listener(self) -> None:
        """Overridden by the SSH server implementation (task 5)."""
        return None
```

- [ ] **步骤 4：跑测试**

```bash
.venv/bin/python -m unittest tests.test_help_session -v 2>&1 | tail -8
```

预期：StateMachineTests 8 条全过（`CLOSING` 那行是为了让"正在关"在日志里可辨，若显得多余就删掉它并保留 `CLOSED`——实现者自行取舍，测试不依赖它）。

- [ ] **步骤 5：Commit**

```bash
git add help_session.py tests/test_help_session.py
git commit -m "feat(help): 状态机与唯一关闭路径（arming 失败也走 close，不留孤儿监听）"
```

---

## 任务 4：【app】审计事件

**文件：**

- 修改：`help_session.py`
- 修改：`tests/test_help_session.py`

- [ ] **步骤 1：写失败的测试**

```python
class AuditTests(unittest.TestCase):
    """What the customer can see, and what the support bundle carries."""

    def setUp(self):
        self.now = 1_000_000.0
        self.events = []

    def _session(self):
        return help_session.HelpSession(
            opener=lambda minutes: 19000, tunnel=lambda lp, rp, add: None,
            listener=lambda: 34567, clock=lambda: self.now, on_event=self.events.append)

    def test_open_and_close_are_recorded(self):
        s = self._session()
        s.enable(minutes=30)
        s.close("user")
        kinds = [e["event"] for e in self.events]
        self.assertEqual(kinds, ["opened", "closed"])
        self.assertEqual(self.events[1]["reason"], "user")

    def test_no_event_carries_the_password(self):
        s = self._session()
        s.enable(minutes=30)
        pw = s.password_for_display()
        s.close("user")
        self.assertTrue(pw)
        self.assertNotIn(pw, repr(self.events))

    def test_auth_failures_are_counted_and_evented(self):
        s = self._session()
        s.enable(minutes=30)
        for _ in range(help_session.MAX_AUTH_FAILURES):
            s.note_auth_failure()
        self.assertEqual(s.state, help_session.CLOSED)
        self.assertEqual(s.close_reason, "auth_failures")
        self.assertIn("auth_fail", [e["event"] for e in self.events])
```

- [ ] **步骤 2：跑它，确认红**（`note_auth_failure` 还不存在）

- [ ] **步骤 3：实现**

在 `HelpSession` 里加：

```python
    def note_auth_failure(self) -> None:
        """One failed authentication. The fifth closes the window.

        A live help session is exactly the moment somebody would probe a port they
        noticed, and loopback-only is not a defence against a local process.
        """
        with self._lock:
            if self.state != OPEN:
                return
            self._failures += 1
            self._emit("auth_fail", failures=self._failures)
            if self._failures >= MAX_AUTH_FAILURES:
                self.close("auth_failures")

    def note_connection(self, *, duration_s: float) -> None:
        with self._lock:
            self.connections += 1
            self._emit("disconnect", user=self._user, duration_s=round(duration_s, 1))
```

并在 `status()` 里已经报了 `failures` 与 `connections`（任务 3 已写）。

- [ ] **步骤 4：跑测试**（全绿，含"事件里没有口令"那条）

- [ ] **步骤 5：Commit**

```bash
git commit -am "feat(help): 审计事件与认证失败限速（5 次即关）"
```

---

## 任务 5：【app】paramiko SSH 服务

**文件：**

- 修改：`help_session.py`
- 修改：`tests/test_help_session.py`

- [ ] **步骤 1：写集成测试（真 paramiko 客户端 → 真服务端）**

追加：

```python
class SshServerTests(unittest.TestCase):
    """A real paramiko client against the real server, on loopback.

    No tunnel, no portal, no hardware: this is the part of the feature that must
    work on every platform, so it runs everywhere.
    """

    def setUp(self):
        import help_session as hs
        self.events = []
        self.server = hs.SshHelpServer(on_event=self.events.append)
        self.port = self.server.start()          # 绑 127.0.0.1:0，返回端口
        self.user, self.pw = hs.make_credentials()
        self.server.set_credentials(self.user, self.pw)

    def tearDown(self):
        self.server.stop()

    def _client(self, user=None, pw=None):
        import paramiko
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect("127.0.0.1", port=self.port, username=user or self.user,
                  password=pw or self.pw, allow_agent=False, look_for_keys=False,
                  timeout=5)
        return c

    def test_the_listener_is_loopback_only(self):
        """设计围栏 1：不是 0.0.0.0。"""
        self.assertEqual(self.server.bind_host, "127.0.0.1")

    def test_a_wrong_password_is_refused(self):
        import paramiko
        with self.assertRaises(paramiko.AuthenticationException):
            self._client(pw="wrong")

    def test_a_wrong_username_is_refused(self):
        import paramiko
        with self.assertRaises(paramiko.AuthenticationException):
            self._client(user="help-0000")

    def test_public_key_auth_is_refused(self):
        """只允许口令认证（围栏 6）。"""
        import paramiko
        key = paramiko.RSAKey.generate(2048)
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        with self.assertRaises(paramiko.AuthenticationException):
            c.connect("127.0.0.1", port=self.port, username=self.user,
                      pkey=key, allow_agent=False, look_for_keys=False, timeout=5)

    def test_a_command_returns_its_output(self):
        c = self._client()
        try:
            chan = c.get_transport().open_session()
            chan.exec_command(help_session.probe_command())
            out = chan.makefile().read().decode()
        finally:
            c.close()
        self.assertIn(help_session.PROBE_MARK, out)

    def test_failed_auths_reach_the_session(self):
        import paramiko
        from unittest import mock
        for _ in range(2):
            with self.assertRaises(paramiko.AuthenticationException):
                self._client(pw="wrong")
        self.assertEqual(len([e for e in self.events if e["event"] == "auth_fail"]), 2)

    def test_stop_closes_the_port(self):
        import socket
        self.server.stop()
        s = socket.socket()
        self.assertRaises(OSError, s.connect, ("127.0.0.1", self.port))
        s.close()
```

- [ ] **步骤 2：跑它，确认红**

- [ ] **步骤 3：实现 `SshHelpServer`**

```python
#: A command that proves a session works without running anything the supporter
#: did not ask for. Chosen to exist on all three shells.
PROBE_MARK = "mrrc-help-ok"


def probe_command() -> str:
    if os.name == "nt":
        return f"Write-Output {PROBE_MARK}"
    return f"echo {PROBE_MARK}"


def platform_shell() -> list[str]:
    """The interactive shell a support session gets on this platform."""
    if os.name == "nt":
        return ["powershell.exe", "-NoLogo"]
    if sys.platform == "darwin":
        return ["zsh", "-l"]
    return ["bash", "-l"]


class _ServerInterface(paramiko.ServerInterface):
    def __init__(self, owner: "SshHelpServer") -> None:
        self._owner = owner

    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL if self._owner.check(username, password) \
            else paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        return paramiko.AUTH_FAILED                 # 围栏 6：只允许口令

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, *a, **kw):
        return True

    def check_channel_shell_request(self, channel):
        self._owner.spawn_shell(channel)
        return True

    def check_channel_exec_request(self, channel, command):
        return self._owner.run_exec(channel, command.decode())


class SshHelpServer:
    """The SSH door itself: bind, authenticate, hand out a shell.

    Deliberately not a general SSH server. One user, one password, one shell per
    channel, loopback only, and it exists for the length of one help window.
    """

    bind_host = BIND_HOST

    def __init__(self, *, on_event: Optional[Callable[[dict], None]] = None) -> None:
        self.on_event = on_event
        self._user = self._pw = ""
        self._failures = 0
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._children: list = []

    # ---- lifecycle ----------------------------------------------------
    def start(self) -> int:
        import paramiko                          # noqa: F401  (import 即依赖检查)
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.bind_host, 0))     # 0 = 让内核挑一个空闲端口
        self._sock.listen(8)
        self._thread = threading.Thread(target=self._accept_loop,
                                        name="mrrc-help-ssh", daemon=True)
        self._thread.start()
        return self._sock.getsockname()[1]

    def set_credentials(self, user: str, password: str) -> None:
        """Injected, not generated here: the session owns the pair, and the server
        only checks it. Two generators would be two things to keep in step."""
        self._user, self._pw = user, password
        self._failures = 0

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass
        self._sock = None
        for child in list(self._children):
            try:
                child.close()
            except Exception:
                pass
        self._children.clear()
        self._user = self._pw = ""

    # ---- authing ------------------------------------------------------
    def check(self, username: str, password: str) -> bool:
        ok = credentials_match(self._user, self._pw, username, password)
        if not ok:
            self._failures += 1
            if self.on_event:
                self.on_event({"event": "auth_fail", "failures": self._failures})
        return ok

    # ---- the shell ----------------------------------------------------
    def _accept_loop(self) -> None:
        import paramiko
        while not self._stop.is_set():
            try:
                client, _ = self._sock.accept()
            except OSError:
                return                            # stop() 关了监听
            t = paramiko.Transport(client)
            try:
                t.add_server_key(self._host_key())
                t.start_server(server=_ServerInterface(self))
            except Exception as exc:
                logger.warning("help: SSH handshake failed: %s", exc)
                t.close()

    def spawn_shell(self, channel) -> None:
        """Start the platform shell on this channel and pump both ways."""
        import pty
        import select
        import struct
        import subprocess

        env = dict(os.environ, TERM=channel.get_terminal_type() or "xterm-256color")
        master, slave = pty.openpty()
        try:
            size = channel.get_terminal_size()
            import fcntl
            import termios
            fcntl.ioctl(slave, termios.TIOCSWINSZ,
                        struct.pack("HHHH", size[1], size[0], 0, 0))
        except Exception:
            pass
        proc = subprocess.Popen(platform_shell(), stdin=slave, stdout=slave,
                                stderr=slave, env=env, close_fds=True)
        os.close(slave)
        started = time.time()
        try:
            while True:
                r, _, _ = select.select([master, channel], [], [], 1.0)
                if master in r:
                    data = os.read(master, 4096)
                    if not data:
                        break
                    channel.sendall(data)
                if channel in r:
                    data = channel.recv(4096)
                    if not data:
                        break
                    os.write(master, data)
        except Exception as exc:
            logger.info("help: shell session ended: %s", exc)
        finally:
            try:
                proc.terminate()
            except Exception:
                pass
            os.close(master)
            channel.close()
            if self.on_event:
                self.on_event({"event": "disconnect", "duration_s": round(time.time() - started, 1)})

    def run_exec(self, channel, command: str) -> bool:
        """Only the probe command is allowed as exec; a shell is the supported path."""
        if command.strip() != probe_command().strip():
            return False
        channel.sendall((PROBE_MARK + "\n").encode())
        channel.send_exit_status(0)
        channel.close()
        return True

    @staticmethod
    def _host_key():
        import paramiko
        return paramiko.RSAKey.generate(2048)
```

**注意两处实现细节**（写进注释，别让后人踩）：

1. `_host_key()` 每次进程都新生成——支持者会看到 `Host key changed` 警告，这是**有意的**：不落盘的密钥才符合"关闭即消失"，而 pin 住一把长期密钥会带来另一个需要保管的秘密。
2. 动态窗口缩放（`channel.resize_pty`）本期不做；初始尺寸转发（上面那段 `TIOCSWINSZ`）够用。

- [ ] **步骤 4：跑测试**

```bash
.venv/bin/python -m unittest tests.test_help_session -v 2>&1 | tail -12
```

预期：全绿。若 `test_a_command_returns_its_output` 在 macOS 上因为 `zsh -l` 的启动输出（问候语）导致匹配不到，把断言放宽为 `assertIn(PROBE_MARK, out)`（它本来就允许前面的额外输出）。

- [ ] **步骤 5：把 `HelpSession` 与它接起来**

```python
    def _close_listener(self) -> None:
        if self._server is not None:
            self._server.stop()
            self._server = None
```

并在 `enable()` 里把 `self.listener` 的默认实现改为 `self._start_ssh()`（返回端口并把 server 存进 `self._server`）。对应补一条测试：`test_close_stops_the_listener`。

- [ ] **步骤 6：Commit**

```bash
git commit -am "feat(help): 应用内 paramiko SSH 服务（回环、口令认证、平台 shell、真客户端集成测试）"
```

---

## 任务 6：【app】隧道配置：管理段 + 第二个代理 + 0600

**文件：**

- 修改：`cloud_hub.py`
- 修改：`tests/test_cloud_hub.py`

- [ ] **步骤 1：写失败的测试**

```python
class HelpTunnelConfigTests(unittest.TestCase):
    def test_the_admin_block_is_loopback_and_authenticated(self):
        text = cloud_hub.frpc_config_text("bg7ifp", "hk", 8888, 18802,
                                          admin_port=7400, admin_password="s3cret")
        self.assertIn('webServer.addr = "127.0.0.1"', text)
        self.assertIn("webServer.port = 7400", text)
        self.assertIn('webServer.user = "mrrc"', text)
        self.assertIn("webServer.password = \"s3cret\"", text)
        self.assertNotIn("0.0.0.0", text)

    def test_no_help_proxy_by_default(self):
        text = cloud_hub.frpc_config_text("bg7ifp", "hk", 8888, 18802,
                                          admin_port=7400, admin_password="s3cret")
        self.assertNotIn("mrrc-help-", text)

    def test_the_help_proxy_is_named_per_instance(self):
        text = cloud_hub.frpc_config_text("bg7ifp", "hk", 8888, 18802,
                                          admin_port=7400, admin_password="s3cret",
                                          help_local_port=41000, help_remote_port=18877)
        self.assertIn('name = "mrrc-help-bg7ifp"', text)
        self.assertIn("localPort = 41000", text)
        self.assertIn("remotePort = 18877", text)
        self.assertEqual(text.count("[[proxies]]"), 2)

    def test_the_config_file_is_not_world_readable(self):
        """它装着 frps 的 token 与管理段口令（既有缺陷，本设计一并修）。"""
        import inspect
        src = inspect.getsource(cloud_hub)
        self.assertIn("0o600", src)
```

- [ ] **步骤 2：跑它，确认红**

- [ ] **步骤 3：实现**

`frpc_config_text` 增加两个可选参数（**保持既有调用点不变**）：

```python
def frpc_config_text(name: str, token: str, local_port: int, remote_port: int,
                     hub_host: str = "tunnel.mrrc.vlsc.net", control_port: int = 8989,
                     log_file: str = "", *, admin_port: int = 0,
                     admin_password: str = "", help_local_port: int = 0,
                     help_remote_port: int = 0) -> str:
```

在 `log.*` 之后、第一个 `[[proxies]]` 之前插入管理段（仅当 `admin_port` 给了值）：

```python
    if admin_port:
        # Loopback + Basic auth, always both: an unauthenticated frpc admin API is
        # a local port that can add proxies. The password lives in this file, which
        # is why the file is 0600 (see _write_frpc_config).
        lines += [
            'webServer.addr = "127.0.0.1"',
            f"webServer.port = {admin_port}",
            'webServer.user = "mrrc"',
            f'webServer.password = "{admin_password}"',
            "",
        ]
```

在末尾追加第二个代理（仅当两个端口都给了值）：

```python
    if help_local_port and help_remote_port:
        # Named per instance: two instances behind one frps sharing a proxy name is
        # how the tunnel ends up claiming a name somebody else holds (2026-10-11).
        lines += [
            "",
            "[[proxies]]",
            f'name = "mrrc-help-{name}"',
            'type = "tcp"',
            'localIP = "127.0.0.1"',
            f"localPort = {help_local_port}",
            f"remotePort = {help_remote_port}",
        ]
```

配置文件写入改成显式 0600（`cloud_hub.connect()` 里那段 `conf.write_text(...)` 之后）：

```python
    try:
        os.chmod(conf, 0o600)       # Windows 上无效，但也不报错
    except OSError as exc:
        logger.warning("cloud hub: cannot tighten %s: %s", conf, exc)
```

- [ ] **步骤 4：跑测试**

```bash
.venv/bin/python -m unittest tests.test_cloud_hub -v 2>&1 | tail -8
```

预期：新 4 条过，既有的 cloud_hub 用例不受影响（默认参数不变）。

- [ ] **步骤 5：Commit**

```bash
git commit -am "feat(help): 隧道配置加管理段与按实例命名的协助代理；配置文件改 0600"
```

---

## 任务 7：【app】热加载：admin API reload

**文件：**

- 修改：`cloud_hub.py`
- 修改：`tests/test_cloud_hub.py`

- [ ] **步骤 1：写失败的测试（假 HTTP 服务端，不打真隧道）**

```python
class ReloadTests(unittest.TestCase):
    def test_reload_posts_to_the_admin_api_with_basic_auth(self):
        import base64, http.server, threading, json
        seen = {}

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen["path"] = self.path
                seen["auth"] = self.headers.get("Authorization", "")
                self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
            def log_message(self, *a): pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            cloud_hub.reload_frpc(srv.server_port, "mrrc", "s3cret", timeout=3)
        finally:
            srv.shutdown()
        self.assertEqual(seen["path"], "/api/reload")
        want = base64.b64encode(b"mrrc:s3cret").decode()
        self.assertEqual(seen["auth"], f"Basic {want}")

    def test_a_non_200_is_an_error(self):
        import http.server, threading
        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(500); self.end_headers(); self.wfile.write(b'{"error":"bad"}')
            def log_message(self, *a): pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(cloud_hub.CloudHubError) as ctx:
                cloud_hub.reload_frpc(srv.server_port, "mrrc", "s3cret", timeout=3)
        finally:
            srv.shutdown()
        self.assertIn("500", str(ctx.exception))
```

- [ ] **步骤 2：跑它，确认红**

- [ ] **步骤 3：实现**

```python
def reload_frpc(admin_port: int, user: str, password: str, *, timeout: float = 5.0) -> None:
    """Ask the running frpc to re-read its config, over its admin API.

    Not `frpc reload -c …`: that needs the binary on PATH and a shell, and this
    runs inside a frozen app. Not a restart either — a restart drops the web
    tunnel for a few seconds, and "the customer pressed a button and their own
    remote session blipped" is not something to make routine. A failure here is
    raised, never silently escalated to a restart (design §4.2).
    """
    import base64
    import urllib.error
    import urllib.request

    cred = base64.b64encode(f"{user}:{password}".encode()).decode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{int(admin_port)}/api/reload", data=b"", method="POST",
        headers={"Authorization": f"Basic {cred}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise CloudHubError(f"frpc reload returned HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        raise CloudHubError(f"frpc reload returned HTTP {exc.code}: {exc.read()[:200]!r}") from exc
    except OSError as exc:
        raise CloudHubError(f"frpc admin API unreachable on 127.0.0.1:{admin_port}: {exc}") from exc
```

- [ ] **步骤 4：跑测试**（两条过）

- [ ] **步骤 5：Commit**

```bash
git commit -am "feat(help): frpc reload 走 admin API（失败即失败，不退回重启）"
```

---

## 任务 8：【app】把 help_session 接到 cloud_hub 与 portal

**文件：**

- 修改：`cloud_hub.py`
- 创建：`tests/test_help_wiring.py`

- [ ] **步骤 1：测试**

```python
class WiringTests(unittest.TestCase):
    """The three collaborators help_session needs, in the shapes it expects."""

    def test_the_help_proxy_write_keeps_the_web_proxy(self):
        """加代理不能把主代理挤掉——那是"协助一开，客户网页就断"的经典写法。"""
        import tempfile, pathlib
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            conf = pathlib.Path(tmp) / "frpc-bg7ifp.toml"
            conf.write_text(cloud_hub.frpc_config_text("bg7ifp", "hk", 8888, 18802,
                                                       admin_port=7400, admin_password="x"),
                            encoding="ascii")
            with mock.patch.object(cloud_hub, "reload_frpc") as reload_mock, \
                 mock.patch.object(cloud_hub, "_write_frpc_config") as write_mock:
                cloud_hub.set_help_proxy(conf, "bg7ifp", "hk", 8888, 18802, 41000, 18877,
                                         admin_port=7400, admin_password="x")
            text = write_mock.call_args.args[1]
            self.assertIn('name = "bg7ifp"', text)
            self.assertIn('name = "mrrc-help-bg7ifp"', text)
            reload_mock.assert_called_once()
```

- [ ] **步骤 2：实现 `set_help_proxy`（加/删都走它）**

```python
def set_help_proxy(conf: Path, label: str, token: str, local_port: int, remote_port: int,
                   help_local_port: int, help_remote_port: int, *,
                   admin_port: int, admin_password: str, remove: bool = False) -> None:
    """Add (or drop) the help proxy in the tunnel config and hot-reload frpc.

    Rewrites the whole file from the same generator that wrote it, so the web
    proxy can never be lost by an edit that only meant to add a second one.
    """
    text = frpc_config_text(
        label, token, local_port, remote_port,
        admin_port=admin_port, admin_password=admin_password,
        help_local_port=0 if remove else help_local_port,
        help_remote_port=0 if remove else help_remote_port)
    _write_frpc_config(conf, text)
    reload_frpc(admin_port, "mrrc", admin_password)
```

（若 `cloud_hub.py` 里还没有独立的 `_write_frpc_config`，在本任务里把 `connect()` 里那段 `conf.write_text` + `chmod` 抽成它——两处调用，一处实现。）

- [ ] **步骤 3：跑测试**

```bash
.venv/bin/python -m unittest tests.test_help_wiring tests.test_cloud_hub -v 2>&1 | tail -8
```

- [ ] **步骤 4：Commit**

```bash
git commit -am "feat(help): set_help_proxy —— 加/删协助代理都从同一个生成器重写配置"
```

---

## 任务 9：【app】server.py 的三个端点

**文件：**

- 修改：`server.py`
- 创建：`tests/test_server_help.py`

- [ ] **步骤 1：测试**

```python
class HelpEndpointTests(unittest.TestCase):
    """Local endpoints: whoever can use the web UI can use these. That is the
    design — the customer is the one who decides — but the *anonymous* case must
    not work, and the status must never carry the password."""

    def test_enable_requires_the_session(self): ...
    def test_status_shape(self): ...
    def test_disable_is_idempotent(self): ...
    def test_status_never_carries_the_password(self):
        """口令只经 `password_for_display()` 在"已开启"时给 UI。"""
        payload = client.get("/api/help/status", ...).json()
        self.assertNotIn("password", payload)
```

（测试写法照抄 `tests/test_support_bundle.py` 的 client 夹具。）

- [ ] **步骤 2：实现**

```python
@app.post("/api/help/enable", include_in_schema=False)
async def api_help_enable(request: Request):
    """Open a help window. Body: {"minutes": 60, "username": "", "password": ""}."""
    _require_session(request)                      # 既有闸门，匿名一律拒
    body = await _json_body(request)
    minutes = int(body.get("minutes") or help_session.DEFAULT_WINDOW_MINUTES)
    pair = _help().enable(minutes=minutes,
                          username=str(body.get("username") or ""),
                          password=str(body.get("password") or ""))
    return JSONResponse({**pair.status(), "password": pair.password_for_display()})


@app.post("/api/help/disable", include_in_schema=False)
async def api_help_disable(request: Request):
    _ensure_session_auth(request)
    _help().close("user")
    return JSONResponse(_help().status())


@app.get("/api/help/status", include_in_schema=False)
async def api_help_status(request: Request):
    _ensure_session_auth(request)
    status = _help().status()
    if status["state"] == help_session.OPEN:
        status = {**status, "password": _help().password_for_display()}
    return JSONResponse(status)
```

`_help()` 是一个进程级单例（`help_session.HelpSession` 的实例），在 lifespan 里创建、在关机钩子里 `close("shutdown")`——**关机即断**是"不常驻"的一部分，要有测试。

- [ ] **步骤 3：跑测试**

```bash
.venv/bin/python -m unittest tests.test_server_help -v 2>&1 | tail -8
```

- [ ] **步骤 4：Commit**

```bash
git commit -am "feat(help): /api/help/{enable,disable,status}（本地动作、状态不含口令字段除非开启）"
```

---

## 任务 10：【app】Support 页的"远程协助"卡片

**文件：**

- 修改：`static/support.html`
- 修改：`tests/test_static_cache_busting.py`（或等价的静态守卫）

- [ ] **步骤 1：写卡片**

在 `static/support.html` 的诊断包区块之后加一块（用页面既有的内联样式变量）：

```html
<section class="card" id="help">
  <h2>远程协助</h2>
  <p class="hint">
    开启后，支持人员可以从 hub 连进来检修。口令只显示在这里，请口头告知他。
    到期自动关闭，你随时可以按"停止协助"。
  </p>
  <div id="help-off">
    <label>窗口
      <select id="help-minutes">
        <option value="30">30 分钟</option>
        <option value="60" selected>60 分钟</option>
        <option value="180">3 小时</option>
      </select>
    </label>
    <button id="help-start" class="primary">开启协助</button>
  </div>
  <div id="help-on" hidden>
    <dl>
      <dt>用户名</dt><dd><code id="help-user"></code></dd>
      <dt>口令</dt><dd><code id="help-pw"></code> <button id="help-copy">复制</button></dd>
      <dt>剩余</dt><dd id="help-left"></dd>
      <dt>连接</dt><dd id="help-conn"></dd>
    </dl>
    <button id="help-stop" class="danger">停止协助</button>
  </div>
</section>
<script>
  // 每 5 秒轮一次 /api/help/status：既刷新倒计时与连接数，也发现"已被关闭"
  // （到期、portal 强制、认证失败都由服务端状态反映，页面不做自己的判断）。
  async function refreshHelp() {
    const r = await fetch('/api/help/status');
    const s = await r.json();
    const on = s.state === 'open';
    document.getElementById('help-on').hidden = !on;
    document.getElementById('help-off').hidden = on;
    if (!on) return;
    document.getElementById('help-user').textContent = s.username;
    document.getElementById('help-pw').textContent = s.password || '';
    document.getElementById('help-left').textContent = Math.floor(s.expires_in_s / 60) + ' 分钟';
    document.getElementById('help-conn').textContent = s.connections + ' 次';
  }
  document.getElementById('help-start').onclick = async () => {
    const minutes = +document.getElementById('help-minutes').value;
    const r = await fetch('/api/help/enable', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({minutes})});
    if (!r.ok) { alert('开启失败：' + await r.text()); }
    refreshHelp();
  };
  document.getElementById('help-stop').onclick = async () => {
    await fetch('/api/help/disable', {method: 'POST'});
    refreshHelp();
  };
  document.getElementById('help-copy').onclick = () => {
    navigator.clipboard.writeText(document.getElementById('help-pw').textContent);
  };
  refreshHelp();
  setInterval(refreshHelp, 5000);
</script>
```

- [ ] **步骤 2：bump 静态缓存键**

`static/sw.js` 的 `CACHE` 抬一位（仓库规则：改静态资源必须 bump，四处 pin 一起改——照 `test_static_cache_busting` 的提示）。

- [ ] **步骤 3：跑测试**

```bash
.venv/bin/python -m unittest tests.test_static_cache_busting -v
```

- [ ] **步骤 4：Commit**

```bash
git commit -am "feat(help): Support 页的远程协助卡（开启/倒计时/口令复制/停止）"
```

---

## 任务 11：【portal】端口种类与协助记录

**文件：**

- 修改：`portal/registry.py`、`portal/store.py`
- 创建：`tests/test_portal_help.py`

- [ ] **步骤 1：测试**

```python
class HelpPortTests(unittest.TestCase):
    def test_a_help_port_never_collides_with_an_instance_port(self): ...
    def test_the_release_rule_needs_a_close_or_a_stale_heartbeat(self):
        """设计 §4.3：只有①实例确认关闭或②心跳消失 5 分钟才释放。"""
    def test_the_record_has_no_password_field(self):
        s = portal.store.HelpSession(callsign="BG7IFP", port=18877, username="help-ab12")
        self.assertNotIn("password", dataclasses.asdict(s))
```

- [ ] **步骤 2：实现**

`registry.py`：端口记录带 `kind`，`free_port()` 与实例共用池，`release(label, kind)` 各自释放。

`store.py`：

```python
@dataclass
class HelpSession:
    """One help window as the portal knows it.

    **There is no password field, on purpose** (design §4.3): the portal is not a
    password custodian, and a field that does not exist cannot be logged,
    displayed or subpoenaed.
    """
    callsign: str
    port: int
    username: str = ""
    state: str = "pending"                 # pending | open | closed
    started_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    last_heartbeat: float = 0.0
    connections: int = 0
    closed_reason: str = ""
```

- [ ] **步骤 3：跑测试**

```bash
cd /Users/cheenle/HAM/hub/mrrc_hub && python3 -m unittest tests.test_portal_help -v
```

- [ ] **步骤 4：Commit**（portal 仓库自己的提交）

```bash
cd /Users/cheenle/HAM/hub/mrrc_hub
git add portal/registry.py portal/store.py tests/test_portal_help.py
git commit -m "feat(portal): 协助端口的种类与记录（结构上没有口令字段）"
```

---

## 任务 12：【portal】三个端点

**文件：**

- 修改：`portal/app.py`
- 修改：`tests/test_portal_help.py`

- [ ] **步骤 1：测试**（认证、拒绝分支、心跳 stop、释放）

```python
class HelpEndpointTests(unittest.TestCase):
    def test_request_needs_a_valid_token(self): ...
    def test_request_refuses_a_second_window(self): ...
    def test_request_clamps_the_window(self): ...
    def test_heartbeat_reports_stop_after_an_admin_revoke(self): ...
    def test_close_frees_the_port(self): ...
    def test_the_request_body_has_no_password_slot(self):
        """请求体白名单：多余字段一律忽略（像 WizardClaim 那样结构上不可能）。"""
```

- [ ] **步骤 2：实现**（`app.py` 的实例侧分派里加三个分支，与 `/status` 同一套 `callsign + token` 校验）

```python
                if route == "/help/request":
                    return self._help_request(body)
                if route == "/help/heartbeat":
                    return self._help_heartbeat(body)
                if route == "/help/close":
                    return self._help_close(body)
```

`_help_request` 的要点：校验 token → 该实例已有进行中的协助则拒 → 窗口取 min(请求值, 180) →
`registry` 分配一个空闲端口并记 `kind="help"` → 写记录并回 `{port, expires_at}`。
`_help_heartbeat`：刷新 `last_heartbeat`、更新 `connections`，并回 `{"stop": bool}`（admin 置过 revoke 就为 true）。
`_help_close`：标 `closed`、释放端口、写审计。

- [ ] **步骤 3：跑测试**

```bash
python3 -m unittest tests.test_portal_help -v 2>&1 | tail -10
```

- [ ] **步骤 4：Commit**

```bash
git commit -am "feat(portal): /help/{request,heartbeat,close} 三个端点（认证、拒绝、强制停止回读）"
```

---

## 任务 13：【portal】实例详情页的卡片与"强制结束"

**文件：**

- 修改：`portal/views.py`、`portal/app.py`（admin 动作）

- [ ] **步骤 1：卡片**（在 `view_instance` 里，照既有 `ui.pill` / `ui.grid_table` 的写法）

内容按设计 §4.3：状态、到期倒计时、`ssh -p <port> <username>@127.0.0.1`（可复制）、心跳新鲜度（客户在线/失联）、连接次数、结束原因；开启中给"强制结束"。卡片底部固定一行：**口令不在门户里，客户口头/短信告知**。

- [ ] **步骤 2：强制结束动作**（走既有 `_action_actor` + CSRF 那条路，与 `/revoke` 同级）

```python
                if route == "/help/revoke":
                    # 破坏性动作的服务端两阶段确认与 /revoke 一致：没带 confirm=yes 就回确认页。
```

- [ ] **步骤 3：测试**

```python
class HelpCardTests(unittest.TestCase):
    def test_the_card_never_prints_a_password(self): ...
    def test_the_command_line_is_shown_when_open(self): ...
    def test_revoke_needs_csrf(self): ...
```

- [ ] **步骤 4：跑测试 + Commit**

---

## 任务 14：【app】心跳回路与三条关闭路径的端到端

**文件：**

- 修改：`help_session.py`（心跳线程）、`server.py`（lifespan 接线）
- 修改：`tests/test_help_session.py`

- [ ] **步骤 1：测试**

```python
class HeartbeatTests(unittest.TestCase):
    def test_the_heartbeat_posts_the_expected_fields(self):
        """callsign/token/port/connections —— 没有口令。"""
    def test_a_stop_reply_closes_the_session(self):
        """portal 的强制结束就是这样到达的。"""
    def test_a_dead_portal_does_not_close_it_early(self):
        """门户不可达只是这一跳失败：窗口到期才是本地判据（否则一次网络抖动就断人）。"""
    def test_shutdown_closes_the_session(self): ...
    def test_expiry_stops_the_heartbeat(self): ...
```

- [ ] **步骤 2：实现**

心跳线程：每 `HEARTBEAT_SECONDS` 一次 `tick()`（到期自关）+ 一次 portal 心跳（`stop` 即 `close("portal")`）；线程随 `close()` 退出（用 `threading.Event`，不用 join 阻塞）。

- [ ] **步骤 3：跑测试 + Commit**

```bash
git commit -am "feat(help): 20 秒心跳回路（到期、portal 强制、失联容忍）"
```

---

## 任务 15：【app】口令红线扫描与全量测试

**文件：**

- 创建：`tests/test_help_leak_guard.py`

- [ ] **步骤 1：写扫描测试**

```python
class PasswordLeakGuard(unittest.TestCase):
    """One test per carrier the password must never reach.

    Written as a scan rather than a unit assertion because the risk is not a
    function returning the wrong value — it is a value appearing somewhere nobody
    looked: a log line, the support bundle, the portal request body.
    """

    def test_the_portal_request_has_no_password_field(self):
        """白名单：允许出现的字段恰好这几个。"""
        allowed = {"callsign", "token", "port", "minutes", "username", "connections"}
        self.assertTrue(set(help_session.HELP_REQUEST_FIELDS) <= allowed)

    def test_the_support_bundle_redacts_it(self):
        """把口令塞进环境/配置再生成一次支持包，断言包里没有它。"""

    def test_the_log_lines_from_a_full_session_never_contain_it(self):
        """跑一遍假 session（listen/连接/失败/关闭），用 assertLogs 抓全部记录再扫。"""
```

- [ ] **步骤 2：跑全量**

```bash
.venv/bin/python -m unittest discover -s tests 2>&1 | tail -4
cd /Users/cheenle/HAM/hub/mrrc_hub && python3 -m unittest discover -s tests 2>&1 | tail -4
```

预期：两边的红都只剩**既存**项（与本次改动无关的那些），不新增。

- [ ] **步骤 3：Commit**

```bash
git commit -am "test(help): 口令红线扫描（日志/支持包/portal 请求体/配置）"
```

---

## 任务 16：【app】真机端到端 + 文档

**文件：**

- 修改：`docs/`（新的操作手册章节）、`docs/superpowers/specs/2026-10-12-remote-help-channel-design.md`（把真机结论填进 §9 的边界表）

- [ ] **步骤 1：真机跑一遍（Mac + 盒子或 Pi）**

```bash
# 在客户机上（Mac 或盒子）：开启协助，记下端口/用户名/口令
# 在 hub 上：
ssh www.vlsc.net
ssh -p <协助端口> <用户名>@127.0.0.1
```

逐项记录：**frpc reload 是否让主隧道不抖**（开着网页隧道观察 30 秒）、shell 是否可交互（换行/颜色/`Ctrl-C`）、
Windows 上 `powershell.exe` 的 pty 行为、macOS 上 launchd 启动的会话环境、盒子上 `mrrc` 用户能做什么。

- [ ] **步骤 2：把结论写回设计文档的 §9 边界表**（哪条已确认、哪条仍有保留），并把操作路径写进操作手册（客户怎么开、支持者怎么连、三条关闭路径怎么触发）。

- [ ] **步骤 3：Commit**

```bash
git commit -am "docs(help): 真机端到端结论与操作手册（frpc reload / shell / 权限边界）"
```

---

## 自检

**规格覆盖度**

| 设计章节 | 落地任务 |
| --- | --- |
| §4.1 状态机 + 唯一 close | 3（+14 的到期/心跳/关机） |
| §4.1 凭据（内存/常量时间/可自己设置） | 2、9（`username`/`password` 入参）、10（UI） |
| §4.1 paramiko 服务（回环/只口令/平台 shell） | 5 |
| §4.1 审计 + 限速 | 4、15 |
| §4.2 webServer + 第二个代理 + 0600 | 6 |
| §4.2 reload（失败即失败） | 7 |
| §4.3 端口种类/记录/释放规则 | 11 |
| §4.3 三个端点 | 12 |
| §4.3 portal 卡片 + 强制结束 | 13 |
| §4.4 应用界面卡片 | 10 |
| §5 三条数据流 | 3（开启）、5（连接）、14（关闭） |
| §6 十一条围栏 | 2/3/4/5/6/11/15 各自的测试 |
| §7 失败模式 | 3（arming）、7（reload）、11（失联释放）、14（门户不可达） |
| §9 P1 范围 | 1–16 |
| §11 配置文件 0600 | 6 |

**占位符扫描**：无 TODO/待定。任务 9/11/13 里以 `...` 出现的测试名，是**已命名但未展开**的用例（名字即断言意图），实现时按同一文件里既有夹具补齐——这是本仓库既有的计划写法（见 W103D 设置页计划的任务 5）。

**类型一致性**：`HelpSession.enable(minutes)` / `close(reason)` / `tick()` / `status()` / `password_for_display()` 在任务 3、4、9、14 中同名同形；`set_help_proxy(conf, label, token, …, remove=)` 在任务 8 定义、任务 14 的心跳不涉及；portal 的 `HelpSession`（store.py）与应用侧的 `HelpSession`（help_session.py）**同名不同物** —— 两边是不同仓库，各自的测试都按本仓库的导入路径解析，不跨仓引用。
