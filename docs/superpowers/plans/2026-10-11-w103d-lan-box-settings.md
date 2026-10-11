# W103D 局域网盒子设置页 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把「盒子接入 LAN → 打开网页 → 配 Wi-Fi 与口令 → 用 `<盒子名>.local` 找回它」这条上线路径做出来，取代真机验证失败的 setup 热点方案。

**Architecture:** `box_ops.py` 是唯一后端（纯逻辑、可注入 runner、无 FastAPI），`server.py` 只做 `/api/box/*` 的薄壳；提权拆成两半 —— NetworkManager 由 polkit 按 `subject.user=="mrrc"` 放行给 server 进程自己写（Wi-Fi 密码因此仍在 `net_wifi` 一条 argv 通道里），四个系统动词（reboot / set-hostname / set-timezone / avahi-reload）走一个 root helper 的 Unix socket。引导页 `/setup` 与设置页 `/settings` 共用同一套后端，只有 HTML 不同。

**Tech Stack:** Python 3.11（stdlib only：`socket`、`subprocess`、`ipaddress`、`json`）、FastAPI/uvicorn、NetworkManager 1.42 `nmcli`、systemd、polkit（JS 规则）、avahi、bash 打包脚本、`unittest` + pytest。

**Spec:** `docs/superpowers/specs/2026-10-11-w103d-lan-box-settings-design.md`

## Global Constraints

- **不用真机验证。** 盒子 2026-10-11 起处于生产使用状态，本期所有操作只落在仓库与打包脚本里。不 ssh、不 `nmcli`、不动运行中的盒子。
- **R-1 / R-2 / R-3 / R-4 / R-8 排在下次刷机后的第一件事**（spec §13）。在它们有结果之前，任何文档、CHANGELOG、提交信息都不得宣称「两条上线路都可用」。
- **只跑 Python 层测试**：`/Users/cheenle/HAM/hub/mrrc_modern/.venv/bin/pytest tests/ -q`。全套约 110 个文件，**每个 task 结束后必须全绿**。
- **工作树是脏的**：`git status` 里有约 39 个未提交文件，全是刚被否掉的 setup 热点工作。Task 1 的 Step 1 先把它们合成**一个**提交，之后每个 task 的 diff 才归因得清。
- **每个 task 只 `git add` 它自己列出的文件**。不 `git add -A`、不 `git add .`。有未跟踪文件（`fleet/`、`vendor/mediatek/`、`mrrc_modern.env.lock` 等）不得顺手带上。
- **不 push。** 所有提交留在本地 `main`。
- **`box_ops.py` 与 `net_wifi.py` 只许 stdlib**，不得 import `server`、`first_run`、`backends` 或任何本应用模块。两个模块都会被 PyInstaller 冻进 Windows/macOS 桌面版，那里没有 `nmcli`、没有 helper：import 必须成功，调用必须降级成失败结果（spec R-6）。
- **Wi-Fi 密码只走 `net_wifi` 的 argv。** 不落日志、不落 env 文件、不落任何状态字典、不进 helper socket、不进异常消息。任何要回传给页面的 nmcli 文本必须过 `net_wifi.scrub(text, psk)`。
- **任何新写的 NM profile 必须显式交代 `connection.autoconnect`。** 这是 `start_hotspot()` 留下 `autoconnect=yes` 的 AP profile、让盒子每次开机自杀 WiFi 的教训。
- **helper 绝不 `shell=True`，绝不拼接命令字符串**，一律 argv 数组。
- **页面文案用简体中文**，与 `static/` 下既有页面一致。
- **默认盒子名 `MRRC-Modern`**（大小写敏感），`set-hostname` 的校验正则必须允许大写。
- **提交信息**用 `type: 中文描述` 风格（与仓库既有历史一致），结尾附：
  `Co-Authored-By: Claude Code <noreply@anthropic.com>`

## Review Focus

以下是 spec 隐含、但没有哪个 task 的验收测试会自然覆盖，却最可能咬到真人的五类输入。每一条都在它所属 task 的步骤里配了测试。

1. **盒子枚举不出自己的子网时，首次窗口必须关闭而不是打开。** 开机瞬间 `nmcli` 还没给出 IPv4、NM 没起来、或者命令失败 —— 一个「判断不出来就放行」的写法会把整台盒子无密码地挂在网上。期望行为：拿不到任何本地子网 ⇒ 窗口关。
2. **SSID 里有 `:`、单引号、空格、emoji 或换行。** `nmcli -t` 用反斜杠转义冒号，`parse_row` 是唯一正确解；把 SSID 拼进 shell 字符串、或把它当成不会出事的标识符，都会在这里断。
3. **helper 不在（桌面版、unit 挂了、socket 权限不对）。** 页面还在开着，用户点「重启盒子」/「改名字」。期望行为：拿到一个明确的失败，面板置灰并说明原因，**其余面板照常**，绝不 500。
4. **已配置的盒子打开 `/setup`。** 用户把书签存成了 `/setup`，或者刚设完口令又被浏览器退回。期望行为：302 → `/settings`，且 `/setup` 这条路径上不存在任何无密码写入。
5. **`nmcli` 的 state 用非英语 locale 打印（`是`/`否`、`已连接`）。** 盒子上的 locale 不是我们能钉住的。只认 `yes` 的解析会把「自动连接」全读成 `False`，用户看到的是自己明明开了的开关显示成关。

---

### Task 1: 退役 server.py 的热点向导面

先把 `server.py` 里那条无密码路径整条拿掉。这一步之后 `net_wifi` 的 AP 半边变成死代码，Task 2 才敢删它。

**Files:**
- Modify: `server.py:68`（`import net_wifi`）、`server.py:3035-3055`（四个常量）、`server.py:3076-3084`（中间件放行）、`server.py:4924-5347`（管理页 + 向导整块）、`server.py:5349-5367`（`/setup` 路由）
- Delete: `static/manage.html`、`tests/test_server_manage.py`、`tests/test_server_setup_wizard.py`
- Create: `tests/test_setup_retired.py`

**Interfaces:**
- Consumes: 无（起点）
- Produces: 无新增。`server.py` 在本 task 后不再 import `net_wifi`；`/setup` 路由暂时改成无条件渲染 `static/setup.html`（Task 12 再补重定向）。

- [ ] **Step 1: 把工作树里既有的一坨提交掉**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
git status --short | wc -l          # 期望 ~40
git add -u
git commit -m "wip: setup 热点的仓库侧工作（本计划将退役其中大半）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
git status --short                  # 期望只剩未跟踪文件
```

- [ ] **Step 2: 先写退役断言测试**

创建 `tests/test_setup_retired.py`：

```python
"""The setup hotspot is gone, and this is what keeps it gone.

The feature was retired on 2026-10-11 after failing its own hardware
acceptance test: the board's MT7663S brings a hotspot up and never beacons it
(spec §1). An onboarding path that cannot be reached is worse than none, so the
new path is a LAN page — and a half-removed hotspot leaves exactly the thing
this file forbids: a passwordless write route nobody is testing.

The list below grows as each artifact retires. A file still carrying the old
vocabulary is not a style problem; it means the route is still registered.
"""
from __future__ import annotations

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: (path, forbidden substrings). Grows one entry per retirement task.
RETIRED = [
    ("server.py", ("setup_ap", "hotspot", "Hotspot", "SETUP_GATE_PATHS",
                   "SETUP_WRITABLE_KEYS", "/api/manage", "wizard")),
]

#: Files that must be gone from the tree entirely.
GONE = [
    "linux/setup_ap.py",
    "static/manage.html",
    "tests/test_setup_ap.py",
    "tests/test_server_manage.py",
    "tests/test_server_setup_wizard.py",
]


class RetiredVocabularyTests(unittest.TestCase):
    def test_no_surviving_file_speaks_the_old_vocabulary(self):
        for relative, words in RETIRED:
            text = (REPO / relative).read_text(encoding="utf-8")
            for word in words:
                with self.subTest(path=relative, word=word):
                    self.assertNotIn(
                        word, text,
                        f"{relative} still mentions {word!r} — the retired "
                        f"path is still wired up")

    def test_the_wizard_modules_are_deleted(self):
        for relative in GONE:
            with self.subTest(path=relative):
                self.assertFalse((REPO / relative).exists(),
                                 f"{relative} should have been deleted")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: 跑它，确认它红**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
.venv/bin/pytest tests/test_setup_retired.py -q
```

期望：FAIL。`server.py still mentions 'setup_ap'` 等，以及五个文件都还在。

- [ ] **Step 4: 删掉 server.py 里那条无密码路径**

四处编辑，全部用精确文本匹配（不要按行号，行号会在第一次编辑后失效）。

**4a. 删 `import net_wifi`（`server.py:68` 那一行）。** 本 task 之后 `server.py` 不再直接调 `net_wifi`：扫描与切换都从 `box_ops` 走（Task 4 起）。确认没有别处用到：

```bash
grep -n "net_wifi" server.py
```

期望只输出第 68 行那一处 import。删掉它。

**4b. 删四个常量。** 从下面这段的开头注释第一行，删到 `MIN_WIFI_PSK_LEN = 8`：

```python
# Setup-wizard paths: the middleware lets these through *unauthenticated* and
# the handlers decide. That split is forced — the client on the hotspot has never
# logged in, which is the whole chicken-and-egg the wizard exists to solve
# (design D-6). It is safe only because the set is closed, compared by equality,
# and every handler calls _setup_access() first.
SETUP_GATE_PATHS = frozenset({
    "/setup",
    "/api/setup/wizard",
    "/api/setup/wizard/password",
    "/api/setup/wizard/wifi",
})

# The only keys the passwordless window may write. Not a config-file path
# (design D-7: MRRC_CONFIG_FILE is D-11's payload), not the transmit gate
# (AD-019 / NFR-067), not a port or a serial device.
SETUP_WRITABLE_KEYS = frozenset({"MRRC_WEB_PASSWORD", "MRRC_AUTO_PASSWORD"})

# 8 is the floor the existing POST /api/setup enforces. The wizard must not be
# the softer door into the same key.
MIN_SETUP_PASSWORD_LEN = 8
# A WPA-PSK shorter than 8 is not a valid passphrase, so this is a typo check
# that saves a 20-second round trip through "AP down → join fails → AP back up".
MIN_WIFI_PSK_LEN = 8
```

替换为：

```python
# The passwordless onboarding window is judged per-request by
# _first_window_open(), not by a path set: /setup plus two /api/box endpoints
# pass the middleware unauthenticated and refuse themselves (design §5, §10).
# The write surface is one key — MRRC_WEB_PASSWORD — and nothing else.
FIRST_WINDOW_PATHS = frozenset({
    "/setup",
    "/api/box/onboarding",
    "/api/box/password",
})
```

> 说明：`FIRST_WINDOW_PATHS` 的消费者要到 Task 9 才写出来，所以 Step 5 会先跑一次全套确认没有 NameError —— 它是模块级常量，没有消费者也不会报错。

**4c. 改中间件放行块。** 把这段：

```python
    # Setup wizard: pass through so an unauthenticated hotspot client can reach
    # the handlers. It must come BEFORE the auth check (that is the point) and it
    # therefore also skips the listen-only 403 below — so _setup_access() refuses
    # a listen token itself. Equality, not startswith: a prefix here would widen
    # the hole to every path under it.
    if path in SETUP_GATE_PATHS:
        return await call_next(request)
```

替换为：

```python
    # The onboarding window: pass through so a client that has never logged in
    # can set the box's first password. Before the auth check (that is the
    # point), and therefore also before the listen-only 403 below — so each
    # handler calls _first_window_open() itself (design §5.3). Equality, not
    # startswith: a prefix here would widen the hole to every path under it.
    if path in FIRST_WINDOW_PATHS:
        return await call_next(request)
```

**4d. 删掉管理页与向导整块（`server.py:4924-5347`）。** 从这一行：

```python
# ── Management page (read-only) ────────────────────────────────────────
```

到向导块的最后一行：

```python
    return JSONResponse({"ok": True, "switching": True, "nonce": nonce})
```

之间的一切，全部删除（连同结尾的空行）。删除后，`POST /api/restart` 的 `return JSONResponse({"restarting": True})` 之后应当**直接**跟上 `@app.get("/setup", ...)`。

> 这一刀删掉了：管理页区块注释、`SAFETY_FIELDS`、`_safety_fields()`、`GET /api/manage`、`GET /manage`，以及整个向导块（`_setup_ap_state_dir`、`_setup_gate_open`、`_setup_access`、`_setup_denial`、`_setup_auto_password`、`GET /api/setup/wizard`、`WIFI_SWITCH_LEAD_S`、`WIFI_SWITCH_HEARTBEAT_S`、`_wifi_failure_reason`、`_claim_heartbeat`、`_perform_wifi_switch`、`GET/POST /api/setup/wizard/wifi`）。安全字段搬到 `box_ops.SAFETY_FIELDS`（Task 4），由 `GET /api/box/status` 提供（Task 10）。

**4e. 改 `/setup` 路由。** 把整个 `@app.get("/setup", ...)` 处理器替换为：

```python
@app.get("/setup", include_in_schema=False)
async def setup_page(request: Request):
    """The onboarding page.

    Passwordless only while the box has never had a user-set password; from
    anywhere else it is an ordinary authenticated route. `no-store` because the
    page renders a window that closes the moment a password lands — a cached
    copy would tell the operator the box is still open when it is not.
    """
    if not _first_window_open(request) and not _verify_auth(request):
        return RedirectResponse("/login?next=/setup", status_code=302)
    page = STATIC_DIR / "setup.html"
    if not page.exists():
        return HTMLResponse("<h1>404 Not Found</h1>", status_code=404)
    response = FileResponse(page, media_type="text/html")
    response.headers["Cache-Control"] = "no-store"
    return response
```

> `_first_window_open` 到 Task 9 才存在。为了这一步能跑，**临时**在这一行的上方加一个占位实现，并在 Task 9 用真实现替换它：

```python
def _first_window_open(request: Request) -> bool:
    """Placeholder — replaced by the real predicate in Task 9."""
    return False
```

（这是唯一一处允许的临时代码，且**在同一个计划里**由 Task 9 替换掉；Task 9 的验收测试会正面证明它已不是恒假。）

**4f. 删三个文件：**

```bash
git rm -q static/manage.html tests/test_server_manage.py \
          tests/test_server_setup_wizard.py
```

> `tests/test_server_setup_wizard.py` 的 `FakeRequest` / `GateCase` 那套不用留 —— 新的端点测试（Task 9 起）用同一个 `asyncio.run(server.<handler>(FakeRequest(...)))` 模式，但 `FakeRequest` 要重建（旧的带 `MRRC_SETUP_AP_STATE_DIR` 夹具，新的一点也不需要）。

- [ ] **Step 5: 跑测试**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
.venv/bin/pytest tests/test_setup_retired.py -q
```

期望：`test_the_wizard_modules_are_deleted` PASS，`test_no_surviving_file_speaks_the_old_vocabulary` PASS。

```bash
.venv/bin/pytest tests/ -q
```

期望：全绿。如果 `tests/test_static_cache_busting.py` 或 `tests/test_sdd_docs_consistency.py` 红了，先看清楚是不是本 Step 引入的（前者检查 `static/` 下文件是否都被引用；`manage.html` 被删是预期内的变化，按测试的规则处理）。

- [ ] **Step 6: 提交**

```bash
git add server.py tests/test_setup_retired.py
git commit -m "retire: 删掉 server.py 的热点向导面与管理页

无密码路径从「热点四路径」换成 FIRST_WINDOW_PATHS（/setup + 两个
/api/box 端点），判据改为每请求求值。向导块、/api/manage、/manage、
SAFETY_FIELDS 与四个常量一并删除，manage.html 及两个测试文件删除。
/net_wifi 的 AP 半边在本步之后成为死代码，下一步删除。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 2: 把 net_wifi.py 收缩到 STA 半边

**Files:**
- Modify: `net_wifi.py`（删约一半）
- Modify: `tests/test_net_wifi.py`（删对应的类）
- Modify: `tests/test_setup_retired.py`（`RETIRED` 加一行）

**Interfaces:**
- Consumes: Task 1（`server.py` 已不再 import `net_wifi`）
- Produces: `net_wifi.py` 保留的确切公开面 —— `NmResult`、`Runner`、`subprocess_runner`、`nmcli`、`parse_row`、`parse_rows`、`_int_or`、`devices`、`nm_running`、`wifi_interface`、`scan_wifi`、`connect_wifi`、`ipv4_address`、`scrub`、`DEFAULT_IFNAME`、`NMCLI_TIMEOUT_S`、`REDACTED`。`box_ops.py`（Task 4）只依赖这些名字。

- [ ] **Step 1: 先给退役断言加一行**

在 `tests/test_setup_retired.py` 的 `RETIRED` 列表里追加：

```python
    ("net_wifi.py", ("hotspot", "Hotspot", "wizard", "ApState", "has_uplink",
                     "ap_settings", "gate_is_open", "10.42.0")),
```

- [ ] **Step 2: 跑它，确认它红**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
.venv/bin/pytest tests/test_setup_retired.py -q
```

期望：FAIL，`net_wifi.py still mentions 'hotspot'`。

- [ ] **Step 3: 删函数**

从 `net_wifi.py` 删掉这些顶层定义（**整个函数体，含 docstring**）：

| 组 | 成员 |
|---|---|
| AP 生命周期 | `start_hotspot`、`stop_hotspot`、`hotspot_active`、`active_ap_connections`、`_connection_is_ap`、`_active_uuids`、`_profiles_named` |
| 无密码门 | `gate_is_open`、`client_ip`、`state_is_live`、`ApState`、`read_state`、`write_state`、`ensure_state_dir`、`_resolve_dir` |
| 两个 JSON 信箱 | `WizardClaim`、`read_wizard`、`write_wizard`、`claim_is_fresh`、`_read_json`、`_write_json`、`_pick` |
| 「有没有上行」 | `has_uplink`、`any_device_connecting`、`_is_usable_uplink` |
| 热点专用 | `ap_network`、`setup_url`、`ap_settings` |

注意：这一段里 `# ── access point ──` / `# ── state file ──` 之类的分节注释一起删；`connect_wifi` 的 docstring 里那句 "this *replaces* the hotspot, it does not run beside it. The caller owns the ordering — see ``server._perform_wifi_switch``" 要改，因为 `_perform_wifi_switch` 已经不存在了：

```python
    """Join an infrastructure network.

    Single radio: this replaces whatever the interface was doing, it does not run
    beside it. The caller owns the ordering — see ``box_ops.perform_wifi_switch``,
    which is also where rollback lives.

    ``password`` travels in argv and nowhere else. It is never logged, never
    written to the env file (WiFi is NetworkManager's domain, design D-4) and is
    removed from any error text that goes back to a page, by ``scrub()``.
    """
```

- [ ] **Step 4: 删常量与被孤立的名字**

从常量区删掉：`DEFAULT_SSID`、`DEFAULT_WEB_PORT`、`DEFAULT_STATE_DIR`、`STATE_NAME`、`WIZARD_NAME`、`HOTSPOT_PROFILE`、`NM_SHARED_SUBNET`、`DEFAULT_GATEWAY`、`MODE_OFF`、`MODE_HOTSPOT`、`MODE_STA`、`WIZARD_SWITCHING`、`WIZARD_OK`、`WIZARD_FAILED`、`HEARTBEAT_MAX_AGE_S`、`CLAIM_MAX_AGE_S`、`STATE_GROUP`、`CONNECTED_WORDS`、`TRANSITIONAL_STATES`、`UPLINK_TYPES`。

保留：`DEFAULT_IFNAME`、`NMCLI_TIMEOUT_S`、`REDACTED`、`Runner`、`NmResult`。

**然后清掉被孤立的 import。** 逐个 grep 确认没有消费者再删：

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
for name in ipaddress json "os\." "Path" "Any" "Mapping"; do
  echo "--- $name ---"; grep -n "$name" net_wifi.py
done
```

预期结论：`ipaddress`（只被 `ap_network`/`gate_is_open` 用）、`json`（只被两个信箱用）、`os`（只被 `ensure_state_dir`/`ap_settings` 用）、`Path`（只被 `DEFAULT_STATE_DIR`/`_resolve_dir` 用）、`Any`/`Mapping`（只被 `ap_settings` 签名用）全部可以删。**保留** `subprocess`（`subprocess_runner` 用）、`dataclass`（`NmResult` 用）、`Callable`/`Optional`（`Runner` 与各签名用）。

- [ ] **Step 5: 改模块文档**

`net_wifi.py:1-48` 的 docstring 整段替换：

```python
#!/usr/bin/env python3
"""NetworkManager adapter: the station half.

Why this module exists
----------------------
The box has one radio, and the page that configures it needs three things from
NetworkManager: what networks are visible, join one, and read back the address
that resulted. This module is the only code in the repository that talks to
``nmcli`` (design D-4: the WiFi domain has exactly one writer, and it is *not*
the ``MRRC_*`` env file, so a WiFi credential can never meet a config key).

It used to be twice this size. The access-point half — raising an open hotspot
for onboarding — was retired on 2026-10-11 after failing its own hardware test:
this board's MT7663S brings the hotspot up, reports it correctly through ``iw``,
and never beacons it (spec §1). Onboarding is a LAN page now, so the AP
lifecycle, the passwordless gate it published and the two JSON mailboxes that
carried the wizard's requests went with it. ``box_ops.perform_wifi_switch`` owns
the ordering and the rollback that used to live in a root supervisor.

Three rules, all load-bearing
-----------------------------
1. **Stdlib only, no application imports.** ``box_ops`` and ``server.py``
   import this, and PyInstaller's root-module analysis therefore freezes it into
   *every* desktop build. Windows and macOS have no ``nmcli``, so the import has
   to succeed and every call has to degrade to a plain failure result rather
   than raise inside a request handler.
2. **A WiFi password never leaves this module's argv.** Not into a log line,
   not into the env file, not into a status dict (SDD ``support-bundle-privacy``,
   AD-021 / NFR-068). ``scrub()`` exists for the one place a secret could still
   ride along: nmcli's own error text.
3. **Everything that shells out takes an injectable ``runner``**, so the whole
   module is testable without NetworkManager, without Linux and without a
   radio.

Two nmcli facts that cost real debugging time
---------------------------------------------
* NM prints a state as ``connected`` in some builds and ``activated`` in others,
  and as ``connected (site only)`` when there is link-local only (a cable into a
  dead switch). Anything parsing device state has to accept all of them; the
  retired supervisor learned this the expensive way.
* ``nmcli -t`` escapes a colon inside a value as ``\\:``. SSIDs contain colons.
  ``parse_row`` is the only correct way to read a terse row back.
"""
```

- [ ] **Step 6: 收缩 tests/test_net_wifi.py**

删掉这些顶层测试类（连同它们的 docstring）：

- `UsableUplinkTests`（87）
- `UplinkTests`（113）
- `TransitionalTests`（178）
- `ApModeTests`（248）
- `StartHotspotTests`（275）
- `StopHotspotTests`（384）
- `StateFileTests`（581）
- `WizardFileTests`（664）
- `ClientIpTests`（726）
- `GateTests`（752）
- `EnsureStateDirTests`（846）
- `SettingsTests`（537）

保留：`FakeNmcli`、`ParseTests`、`GeneralStatusTests`、`DeviceQueryTests`、`NmResultTests`、`ScanTests`、`ConnectTests`、`AddressTests`、`ScrubTests`、`SubprocessRunnerTests`。

> `SettingsTests` 整类删：六个方法每一行都在断言 `ap_settings()` 的返回值（`test_defaults` / `test_environment_overrides` / `test_the_web_port_can_be_overridden_on_its_own` / `test_an_empty_value_falls_back_to_the_default` / `test_a_non_numeric_port_falls_back` / `test_it_reads_the_process_environment_by_default`），而这个函数在本次改动里整个消失，没有一条属于还活着的半边。`DEFAULT_IFNAME` 确实留下，但它出现在 `ScanTests`（380）与 `ConnectTests`（476）里，与这个类无关。
>
> `FakeNmcli` 的 `table`/`default`/`calls`/`count`/`joined` 一个都不能少：`box_ops` 的测试（Task 7、8）要 import 它。

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
.venv/bin/pytest tests/test_net_wifi.py -q
```

期望：全绿。若报 `AttributeError: module 'net_wifi' has no attribute ...`，说明有个测试用的函数没删干净或删多了 —— 按报错逐个处理。

- [ ] **Step 7: 跑全套 + 提交**

```bash
.venv/bin/pytest tests/ -q
git add net_wifi.py tests/test_net_wifi.py tests/test_setup_retired.py
git commit -m "retire: net_wifi.py 收缩到 STA 半边

删除 AP 生命周期、无密码门、两个 JSON 信箱、上行判断一族与热点常量；
保留 scan_wifi/connect_wifi/devices/ipv4_address/nmcli 传输层/parse_* 与
scrub，这些正是网页选 Wi-Fi 要用的零件。模块文档重写。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 3: 删掉 linux/setup_ap.py 并修好打包与镜像门禁

**Files:**
- Delete: `linux/setup_ap.py`、`tests/test_setup_ap.py`
- Modify: `packaging/box/box-overlay.sh`（apt 列表、unit heredoc、enable 行、mask 行、import gate）
- Modify: `packaging/box/verify.sh`（变量、第 11/12 项）
- Modify: `packaging/rpi/pi-gen-stage4/01-deploy-mrrc/00-run-chroot.sh:43`
- Modify: `tests/test_box_profiles.py`（`SetupApPackagingTests`、`VerifyScriptTests`、`test_debians_dnsmasq_unit_is_masked`）
- Modify: `tests/test_image_imports.py:119`
- Modify: `tests/test_setup_retired.py`（`RETIRED` 加 `box-overlay.sh` / `verify.sh`；`GONE` 已含 `linux/setup_ap.py`、`tests/test_setup_ap.py`）
- Modify: `tests/README.md:801-811`

**Interfaces:**
- Consumes: Task 1、Task 2
- Produces: `box-overlay.sh` 里不再有 `mrrc-setup-ap.service`。Task 13 与 Task 14 会往同一个文件里加 `mrrc-box-helper.service` / `mrrc-wifi-preseed.service`，落在本 task 留下的空位上。

- [ ] **Step 1: 删除两个 Python 文件**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
git rm -q linux/setup_ap.py tests/test_setup_ap.py
```

- [ ] **Step 2: 改 box-overlay.sh**

**2a. apt 列表：删 `dnsmasq`，删它上面那条注释。** 把：

```bash
apt-get update -qq
# dnsmasq is for the setup hotspot, not for DNS: NetworkManager's
# ipv4.method=shared spawns it to hand out addresses on 10.42.0.0/24, and the
# pinned base image does not carry it (design D-2). Without it the hotspot
# appears, a phone associates, and gets no address — with no error anywhere,
# which is the most confusing failure this feature can have.
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
	python3.11-venv python3-dev \
	portaudio19-dev libportaudio2 libasound2-dev \
	libopus0 libopus-dev \
	dnsmasq
```

替换为：

```bash
apt-get update -qq
# dnsmasq (the daemon) is gone with the setup hotspot: it was there because
# NetworkManager's ipv4.method=shared spawns it to hand out addresses, and
# nothing uses shared mode any more. dnsmasq-base stays — that is the package
# NM actually links against, and removing it breaks Wi-Fi.
# avahi-daemon is new: the box now advertises <hostname>.local so it can be
# found again after the operator switches networks (design §8). The pinned base
# image carries only avahi-autoipd, which is 169.254 link-local and unrelated.
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
	python3.11-venv python3-dev \
	portaudio19-dev libportaudio2 libasound2-dev \
	libopus0 libopus-dev \
	avahi-daemon
```

> **不要动 `systemctl mask dnsmasq.service` 那一行**（在 `log "enable services"` 之后）。`dnsmasq-base` 不带 unit，但 Debian 的 `dnsmasq` 包可能被别的途径拉进来，掩码仍然是对的防线 —— `test_debians_dnsmasq_unit_is_masked` 会继续钉住它。

**2b. 删 `mrrc-setup-ap.service` 的 unit heredoc（`box-overlay.sh:198-230`）。** 从：

```bash
cat >/etc/systemd/system/mrrc-setup-ap.service <<'UNIT'
```

到它自己的：

```bash
UNIT
```

整段删除（含结尾空行）。

**2c. 改 enable 行。** 把：

```bash
log "enable services"
# mrrc-setup-ap first: it is the only way in for an operator who has a phone
# and nothing else, and it has to be up before the box is handed to anybody.
systemctl enable mrrc-setup-ap.service mrrc-firstboot.service mrrc-modern.service
```

替换为（helper 与 preseed 两个 unit 在 Task 13 / Task 14 加进来时再续这一行）：

```bash
log "enable services"
systemctl enable mrrc-firstboot.service mrrc-modern.service
```

**2d. 改 import gate。** 把：

```bash
	PYTHONDONTWRITEBYTECODE=1 "$VENV/bin/python" -c "import server; import sys; sys.path.insert(0, '$MRRC_HOME/linux'); import first_run, mrrc_radio, setup_ap"
```

替换为：

```bash
	PYTHONDONTWRITEBYTECODE=1 "$VENV/bin/python" -c "import server; import sys; sys.path.insert(0, '$MRRC_HOME'); sys.path.insert(0, '$MRRC_HOME/linux'); import net_wifi, box_ops; import first_run, mrrc_radio"
```

> `sys.path.insert(0, '$MRRC_HOME')` 是新增的：`box_ops` 在仓库根，`server.py` 因为 `cd "$MRRC_HOME"` 才恰好 import 得到它 —— 显式插进去，别靠 cwd 的巧合。`box_ops` 到 Task 4 才存在；**本 Step 先把 `box_ops` 从这行里拿掉**，Task 4 的 Step 5 再把它加回去。

本 Step 实际写：

```bash
	PYTHONDONTWRITEBYTECODE=1 "$VENV/bin/python" -c "import server; import sys; sys.path.insert(0, '$MRRC_HOME/linux'); import first_run, mrrc_radio"
```

**2e. 改 `box-overlay.sh` 顶部那段描述脚本目的一句话**（若它提到了热点）。先看：

```bash
grep -n "hotspot\|热点\|setup" packaging/box/box-overlay.sh
```

凡是描述「热点引导」的注释改成对应的新事实，不要留下与代码不符的注释。

- [ ] **Step 3: 改 verify.sh**

**3a. 删两个变量（`verify.sh:24-25`）：**

```bash
SETUP_UNIT=mrrc-setup-ap.service
AP_STATE=/run/mrrc/setup-ap/state.json
```

**3b. 把第 11 项换成「盒子身份与提权通道」。** 从：

```bash
head_ "11/12 setup hotspot (onboarding with no cable and no keyboard)"
```

到该项的 `fi`（在 `head_ "12/12 WiFi N9 firmware ..."` 之前），整段替换为：

```bash
head_ "11/12 box identity and the privilege channel"
# The onboarding path is a LAN page now (design §9), so what has to be true
# post-flash is not "a hotspot exists" but "the operator can find this box and
# the helpers it needs are installed".
if [ ! -e /etc/polkit-1/rules.d/49-mrrc-nm.rules ]; then
	bad "the NetworkManager polkit rule is missing" \
		"the web pages cannot write NM without it; add it to box-overlay.sh"
else
	ok "polkit rule present (mrrc may reconfigure NetworkManager)"
fi
if ! systemctl is-active --quiet avahi-daemon; then
	bad "avahi-daemon is not running" \
		"<hostname>.local will not resolve; systemctl status avahi-daemon"
elif ! command -v avahi-resolve >/dev/null 2>&1; then
	printf '  – avahi-resolve not available; skipped the name check\n'
elif avahi-resolve -4 -n "$(hostname).local" >/dev/null 2>&1; then
	ok "$(hostname).local resolves"
else
	bad "$(hostname).local does not resolve" \
		"avahi-daemon is up but not answering; check for a name collision in journalctl -u avahi-daemon"
fi
if [ ! -S /run/mrrc/box-helper.sock ]; then
	bad "/run/mrrc/box-helper.sock is missing or not a socket" \
		"reboot / change-name / change-timezone will be greyed out; journalctl -u mrrc-box-helper"
else
	mode="$(stat -c '%a %U %G' /run/mrrc/box-helper.sock 2>/dev/null)"
	ok "box helper socket ${mode:-present}"
fi
```

> `verify.sh` 的头部注释与 `head_ "N/12"` 的编号：本项仍算 1 项，总数仍是 12，所以 `test_the_section_numbers_are_contiguous_and_agree_on_a_total` 不用改。若 Step 5 的测试说编号不连续，数一遍再改总数。

**3c. 改文件头注释里的分工描述**（`verify.sh:1-9`）—— 若它提到热点，改成 LAN 页面。

- [ ] **Step 4: 改 rpi 的 chroot import gate**

`packaging/rpi/pi-gen-stage4/01-deploy-mrrc/00-run-chroot.sh:43`：

```bash
import server, first_run, mrrc_radio, setup_ap
```

改成：

```bash
import server, first_run, mrrc_radio
```

- [ ] **Step 5: 改测试**

**5a. `tests/test_box_profiles.py`**

- 删掉整个 `SetupApPackagingTests` 类（364-530）。
- 删掉 `VerifyScriptTests` 里的 `test_it_checks_the_setup_hotspot`(551) 与 `test_a_box_that_already_has_a_network_is_not_failed_for_having_no_hotspot`(555)，**换成**在同一个类里：

```python
    def test_it_checks_the_onboarding_channel(self):
        """The three things that make the LAN onboarding path work, and whose
        absence is invisible until an operator is standing at a box they cannot
        reach: the polkit rule that lets the server write NetworkManager, the
        mDNS name that is the only way back after a network switch, and the root
        helper socket for the four system verbs (design §7, §8)."""
        self.assertIn("49-mrrc-nm.rules", self.text)
        self.assertIn("avahi-daemon", self.text)
        self.assertIn("/run/mrrc/box-helper.sock", self.text)

    def test_it_no_longer_checks_a_hotspot(self):
        """The feature failed its hardware test on 2026-10-11 (spec §1). A check
        for machinery that no longer ships is a check that fails every good
        box."""
        self.assertNotIn("mrrc-setup-ap", self.text)
        self.assertNotIn("/run/mrrc/setup-ap", self.text)
```

保留 `VerifyScriptTests` 的其余方法（`test_the_section_numbers_are_contiguous_and_agree_on_a_total`、`test_no_pipe_into_a_late_exiting_matcher`、`test_the_failure_branches_are_about_the_machinery_not_the_mode`）。后者的注释里若提到热点，只改注释。

- `BoxFirstBootHardwareTests.test_debians_dnsmasq_unit_is_masked`(667) **不动** —— 掩码那行留着。
- 检查 `BoxBuildIntegrityTests` 里有没有断言 apt 列表内容的方法：`SetupApPackagingTests.apt_packages()` 与 `test_dnsmasq_is_in_the_apt_install_list`、`test_the_package_list_is_what_the_overlay_actually_installs`、`test_dnsmasq_joins_the_existing_install_rather_than_a_second_apt_run` 都随类一起删。若 `BoxBuildIntegrityTests` 也断言了 apt 列表，补一条：

```python
    def test_avahi_is_installed_and_dnsmasq_is_not(self):
        """`dnsmasq` was there for the hotspot's shared mode; `avahi-daemon` is
        there for <hostname>.local. Swapping them is the whole packaging change
        for the new onboarding path, and a stray `dnsmasq` would keep pulling a
        daemon that collides with systemd-resolved for port 53."""
        packages = self.text.split("--no-install-recommends", 1)[1].split("\n\n", 1)[0]
        self.assertIn("avahi-daemon", packages)
        self.assertNotIn("dnsmasq", packages)
```

（先确认 `BoxBuildIntegrityTests` 有能定位 apt 列表的写法；没有就用上面这段的字符串切法，它是自足的。）

**5b. `tests/test_image_imports.py:119`** —— 删掉这一行：

```python
    probe("linux/setup_ap.py", lambda: script("linux/setup_ap.py"))
```

> 现在探测点是三处（`server`、`linux/firstboot_wrapper.py`、`linux/mrrc_radio.py`）。Task 4 会加 `box_ops` 的探测（它是根目录模块，用 `probe("box_ops", lambda: importlib.import_module("box_ops"))` 的形状 —— 照抄 `server` 那一行）。

**5c. `tests/README.md:801-811`** —— 删掉描述 `test_setup_ap.py`（801-805）与 `test_server_setup_wizard.py`（806-811）的两段，替换为一段描述 `test_setup_retired.py`：

```markdown
### `test_setup_retired.py`

Setup 热点方案的退役守卫。热点在 2026-10-11 的真机验证里失败（板子的
MT7663S 起得来 AP 但不发 beacon），引导改走 LAN 网页，本文件钉住旧词汇不再
回来：`server.py` 不得出现 `setup_ap` / `hotspot` / `wizard` / `SETUP_GATE_PATHS`
/ `/api/manage`，`net_wifi.py` 不得出现 `ApState` / `has_uplink` / `ap_settings`
/ `10.42.0`，`box-overlay.sh` 与 `verify.sh` 不得出现 `mrrc-setup-ap`。名单随每个
退役 task 增长，`GONE` 列表里的文件必须确实不存在。
```

- [ ] **Step 6: 跑测试**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
.venv/bin/pytest tests/test_setup_retired.py tests/test_box_profiles.py \
   tests/test_image_imports.py -q
```

期望：全绿。

```bash
.venv/bin/pytest tests/ -q
```

期望：全绿。

- [ ] **Step 7: 提交**

```bash
git add packaging/box/box-overlay.sh packaging/box/verify.sh \
  packaging/rpi/pi-gen-stage4/01-deploy-mrrc/00-run-chroot.sh \
  tests/test_box_profiles.py tests/test_image_imports.py tests/README.md \
  tests/test_setup_retired.py
git commit -m "retire: 删除 linux/setup_ap.py 与打包侧的 setup 热点

box-overlay.sh 去掉 unit heredoc、apt 列表的 dnsmasq 换成 avahi-daemon、
enable 行与两个 import gate 去掉 setup_ap；verify.sh 的第 11 项从「热点」
换成「polkit 规则 + avahi + helper socket」。装箱测试同步。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 4: box_ops.py —— 只读体检、身份与安全字段

`box_ops.py` 是整个功能的后端，本 task 建出它的只读半边：网络体检、Wi-Fi 列表、系统状态、盒子身份、安全字段、helper 可达性。全部不需要提权。

**Files:**
- Create: `box_ops.py`
- Create: `tests/test_box_ops_reports.py`
- Modify: `packaging/box/box-overlay.sh`（import gate 加回 `box_ops`）
- Modify: `tests/test_image_imports.py`（加 `box_ops` 探测点）

**Interfaces:**
- Consumes: `net_wifi.NmResult`、`net_wifi.subprocess_runner`、`net_wifi.nmcli`、`net_wifi.parse_rows`、`net_wifi._int_or`、`net_wifi.devices`、`net_wifi.nm_running`、`net_wifi.scan_wifi`、`net_wifi.DEFAULT_IFNAME`
- Produces（后续 task 依赖的确切名字）：
  - `DEFAULT_HOSTNAME: str = "MRRC-Modern"`
  - `HELPER_SOCKET: str = "/run/mrrc/box-helper.sock"`
  - `_run(argv: list, runner=None) -> net_wifi.NmResult`
  - `_values(field: str, runner=None) -> list[str]`
  - `local_subnets(runner=None) -> list[ipaddress.IPv4Network]`
  - `client_in_local_subnet(host: Optional[str], runner=None) -> bool`
  - `scan_networks(runner=None) -> list[dict]`（`{"ssid","signal","security","protected"}`）
  - `wifi_status(runner=None) -> dict`（`{"ssid","signal"}`）
  - `connections_report(runner=None) -> list[dict]`（`{"name","type","autoconnect"}`）
  - `network_report(runner=None) -> dict`
  - `services_report(runner=None) -> list[dict]`（`{"unit","active"}`）
  - `system_report(runner=None) -> dict`（`{"timezone","uptime_s","services"}`）
  - `identity_report(runner=None) -> dict`（`{"hostname","mdns","addresses","suggested_name"}`）
  - `safety_report(environ=None) -> list[dict]`（`{"key","value","read_only","note"}`）
  - `helper_available(path: str = HELPER_SOCKET) -> bool`
  - `SAFETY_FIELDS: tuple`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_box_ops_reports.py`：

```python
"""The read-only half of box_ops: everything a page shows and nothing it sets.

Two things this file exists to keep true.

The **locale trap**: nmcli prints AUTOCONNECT as `yes`/`no` in the C locale and
as `是`/`否` under a Chinese one, and the box's locale is not ours to pin. A
parser that only knows `yes` shows a user's own autoconnect switches as off.

The **degradation contract** (spec R-6): this module is frozen into the Windows
and macOS builds, where there is no `nmcli` and no helper. Every function here
has to return an empty/neutral value rather than raise, because the alternative
is a 500 in a request handler.
"""
from __future__ import annotations

import unittest

import box_ops
import net_wifi
from tests.test_net_wifi import FakeNmcli


class LocalSubnetTests(unittest.TestCase):
    def test_it_reads_every_address_the_box_holds(self):
        runner = FakeNmcli({"IP4.ADDRESS": net_wifi.NmResult(
            0, "192.168.16.217/24\n192.168.1.217/24\n", "")})
        self.assertEqual(
            [str(n) for n in box_ops.local_subnets(runner)],
            ["192.168.16.0/24", "192.168.1.0/24"],
        )

    def test_no_addresses_means_no_subnets(self):
        """An empty list closes the onboarding window (see the Review Focus)."""
        runner = FakeNmcli({"IP4.ADDRESS": net_wifi.NmResult(0, "", "")})
        self.assertEqual(box_ops.local_subnets(runner), [])

    def test_a_failed_nmcli_also_means_no_subnets(self):
        runner = FakeNmcli({"IP4.ADDRESS": net_wifi.NmResult(
            1, "", "Error: NetworkManager is not running.")})
        self.assertEqual(box_ops.local_subnets(runner), [])

    def test_junk_lines_are_skipped_not_fatal(self):
        runner = FakeNmcli({"IP4.ADDRESS": net_wifi.NmResult(
            0, "192.168.1.217/24\n\nnot-an-address\n", "")})
        self.assertEqual(
            [str(n) for n in box_ops.local_subnets(runner)], ["192.168.1.0/24"])


class ClientSubnetTests(unittest.TestCase):
    def setUp(self):
        self.runner = FakeNmcli({"IP4.ADDRESS": net_wifi.NmResult(
            0, "192.168.16.217/24\n", "")})

    def test_a_client_on_my_subnet_is_inside(self):
        self.assertTrue(
            box_ops.client_in_local_subnet("192.168.16.50", self.runner))

    def test_a_client_elsewhere_is_outside(self):
        self.assertFalse(
            box_ops.client_in_local_subnet("10.42.0.57", self.runner))

    def test_the_ipv4_mapped_form_is_the_same_client(self):
        """Starlette hands us `::ffff:192.168.16.50` when the request arrived
        over a v4-mapped socket. Treating that as "not my subnet" closes the
        window on exactly the client it was opened for."""
        self.assertTrue(box_ops.client_in_local_subnet(
            "::ffff:192.168.16.50", self.runner))

    def test_no_client_at_all_is_outside(self):
        for host in ("", None, "not-an-ip", "fe80::1"):
            with self.subTest(host=host):
                self.assertFalse(
                    box_ops.client_in_local_subnet(host, self.runner))


class ConnectionsReportTests(unittest.TestCase):
    def rows(self, text):
        return FakeNmcli({"connection show": net_wifi.NmResult(0, text, "")})

    def test_it_reads_name_type_and_autoconnect(self):
        report = box_ops.connections_report(self.rows(
            "home:802-11-wireless:yes\n有线连接 2:802-3-ethernet:no\n"))
        self.assertEqual(report, [
            {"name": "home", "type": "802-11-wireless", "autoconnect": True},
            {"name": "有线连接 2", "type": "802-3-ethernet",
             "autoconnect": False},
        ])

    def test_a_chinese_locale_spelling_is_understood(self):
        """See the module docstring. This is the whole reason the check is not
        `== "yes"`."""
        report = box_ops.connections_report(self.rows(
            "home:802-11-wireless:是\n有线连接 2:802-3-ethernet:否\n"))
        self.assertEqual([r["autoconnect"] for r in report], [True, False])

    def test_a_colon_inside_a_name_survives(self):
        """nmcli escapes it as `\\:`; parse_row is the only correct reader."""
        report = box_ops.connections_report(self.rows(
            "my\\:net:802-11-wireless:yes\n"))
        self.assertEqual(report[0]["name"], "my:net")

    def test_a_failed_nmcli_reports_no_connections(self):
        self.assertEqual(box_ops.connections_report(
            FakeNmcli({"connection show": net_wifi.NmResult(1, "", "boom")})),
            [])


class WifiStatusTests(unittest.TestCase):
    def test_the_active_row_is_the_station_link(self):
        runner = FakeNmcli({"device wifi list": net_wifi.NmResult(
            0, "no:邻居家:wpa2:44\nyes:CMCC-cnham:wpa2:61\nno:别的:wpa2:30\n",
            "")})
        self.assertEqual(box_ops.wifi_status(runner),
                         {"ssid": "CMCC-cnham", "signal": 61})

    def test_no_active_row_is_an_empty_link_not_an_error(self):
        runner = FakeNmcli({"device wifi list": net_wifi.NmResult(
            0, "no:邻居家:wpa2:44\n", "")})
        self.assertEqual(box_ops.wifi_status(runner), {"ssid": "", "signal": 0})

    def test_a_failure_is_also_an_empty_link(self):
        self.assertEqual(
            box_ops.wifi_status(FakeNmcli(
                {"device wifi list": net_wifi.NmResult(1, "", "boom")})),
            {"ssid": "", "signal": 0})


class ScanTests(unittest.TestCase):
    def test_it_is_the_same_scan_the_wizard_used(self):
        """No second implementation: the AP-era wizard's scan was already the
        strongest-first, colon-safe one."""
        runner = FakeNmcli({"device wifi list": net_wifi.NmResult(
            0, "weak:wpa2:10\nstrong:wpa2:90\nweak:wpa2:20\n", "")})
        self.assertEqual([n["ssid"] for n in box_ops.scan_networks(runner)],
                         ["strong", "weak"])
        self.assertEqual(box_ops.scan_networks(runner)[0]["signal"], 90)


class NetworkReportTests(unittest.TestCase):
    def test_it_collects_the_five_things_that_say_where_it_broke(self):
        runner = FakeNmcli({
            "IP4.ADDRESS": net_wifi.NmResult(0, "192.168.16.217/24\n", ""),
            "IP4.GATEWAY": net_wifi.NmResult(0, "192.168.1.1\n", ""),
            "IP4.DNS": net_wifi.NmResult(0, "192.168.1.1\n", ""),
            "device status": net_wifi.NmResult(
                0, "wlan0:wifi:connected:CMCC-cnham\n", ""),
            "connection show": net_wifi.NmResult(
                0, "home:802-11-wireless:yes\n", ""),
            "device wifi list": net_wifi.NmResult(
                0, "yes:CMCC-cnham:wpa2:61\n", ""),
        })
        report = box_ops.network_report(runner)
        self.assertEqual(report["addresses"], ["192.168.16.217/24"])
        self.assertEqual(report["gateways"], ["192.168.1.1"])
        self.assertEqual(report["dns"], ["192.168.1.1"])
        self.assertEqual(report["wifi"]["ssid"], "CMCC-cnham")
        self.assertEqual(len(report["devices"]), 1)
        self.assertEqual(len(report["connections"]), 1)

    def test_no_network_manager_is_a_report_not_a_crash(self):
        runner = FakeNmcli(default=net_wifi.NmResult(1, "", "not running"))
        report = box_ops.network_report(runner)
        self.assertEqual(report["addresses"], [])
        self.assertEqual(report["devices"], [])
        self.assertEqual(report["wifi"], {"ssid": "", "signal": 0})


class SystemReportTests(unittest.TestCase):
    def test_it_reports_the_three_units_that_matter(self):
        runner = FakeNmcli({
            "is-active mrrc-modern.service": net_wifi.NmResult(
                0, "active\n", ""),
            "is-active NetworkManager.service": net_wifi.NmResult(
                0, "active\n", ""),
            "is-active avahi-daemon.service": net_wifi.NmResult(
                3, "inactive\n", ""),
        })
        report = box_ops.system_report(runner)
        self.assertEqual(
            [(s["unit"], s["active"]) for s in report["services"]],
            [("mrrc-modern.service", True),
             ("NetworkManager.service", True),
             ("avahi-daemon.service", False)],
        )

    def test_no_systemctl_is_not_fatal(self):
        """The desktop builds have no systemctl either."""
        report = box_ops.system_report(
            FakeNmcli(default=net_wifi.NmResult(127, "", "not found")))
        self.assertEqual([s["active"] for s in report["services"]],
                         [False, False, False])


class IdentityReportTests(unittest.TestCase):
    def test_it_gives_both_ways_back(self):
        """spec §8: `<name>.local` is perfect on iOS, needs Bonjour on Windows
        and is a lottery on Android — so the page shows the address too."""
        runner = FakeNmcli({"IP4.ADDRESS": net_wifi.NmResult(
            0, "192.168.16.217/24\n", "")})
        report = box_ops.identity_report(runner)
        self.assertTrue(report["hostname"])
        self.assertEqual(report["mdns"], report["hostname"] + ".local")
        self.assertEqual(report["addresses"], ["192.168.16.217"])
        self.assertEqual(report["suggested_name"], "MRRC-Modern")


class SafetyReportTests(unittest.TestCase):
    def test_every_field_is_read_only_and_explains_itself(self):
        report = box_ops.safety_report({"MRRC_PTT_MAX_TX_SECONDS": "300"})
        self.assertEqual(
            {f["key"] for f in report},
            {"MRRC_ALLOW_UNVERIFIED_TX", "MRRC_PTT_MAX_TX_SECONDS",
             "MRRC_REMOTE_SESSION_TX_HEARTBEAT_S"},
        )
        for field in report:
            with self.subTest(key=field["key"]):
                self.assertTrue(field["read_only"])
                self.assertTrue(field["note"])

    def test_the_transmit_gate_shows_what_the_radio_would_use(self):
        """AD-019 / NFR-067 — the one number that must never be guessed at."""
        report = {f["key"]: f["value"]
                  for f in box_ops.safety_report({"MRRC_ALLOW_UNVERIFIED_TX": "1"})}
        self.assertEqual(report["MRRC_ALLOW_UNVERIFIED_TX"], "1")

    def test_an_unset_key_falls_back_to_its_documented_default(self):
        report = {f["key"]: f["value"] for f in box_ops.safety_report({})}
        self.assertEqual(report["MRRC_ALLOW_UNVERIFIED_TX"], "0")
        self.assertEqual(report["MRRC_PTT_MAX_TX_SECONDS"], "0")
        self.assertEqual(report["MRRC_REMOTE_SESSION_TX_HEARTBEAT_S"], "")


class HelperAvailableTests(unittest.TestCase):
    def test_it_is_a_path_check_and_nothing_more(self):
        """`helper_available` is allowed to be wrong in one direction only: it
        may say False when the helper would have answered. Saying True when
        there is no socket puts a button on a page that cannot work."""
        self.assertFalse(box_ops.helper_available("/definitely/not/here.sock"))
        self.assertFalse(box_ops.helper_available(""))


class TimezoneTests(unittest.TestCase):
    def test_a_real_zone_resolves_to_its_name(self):
        report = box_ops.timezone()
        self.assertIsInstance(report, str)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑它，确认它红**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
.venv/bin/pytest tests/test_box_ops_reports.py -q
```

期望：`ModuleNotFoundError: No module named 'box_ops'`。

- [ ] **Step 3: 写 box_ops.py 的只读半边**

创建 `box_ops.py`：

```python
#!/usr/bin/env python3
"""The box's own operating system, as the web pages need to talk about it.

Why this module exists
----------------------
`/setup` (the onboarding page) and `/settings` (the settings page) show the same
facts and drive the same actions. One backend, two front ends: a second
implementation of "switch the Wi-Fi" is how the two pages start disagreeing about
what happened.

Two rules, both inherited from `net_wifi.py` and load-bearing for the same
reasons:

1. **Stdlib only, no application imports.** `server.py` imports this, and
   PyInstaller freezes its root modules into every desktop build. Windows and
   macOS have no NetworkManager and no helper, so the import must succeed and
   every call must degrade to a plain failure (spec R-6) — the pages grey their
   buttons out instead of returning a 500.
2. **A Wi-Fi password never leaves a function's locals.** It reaches an argv
   that `net_wifi` builds and nothing else: not a status dict, not a log line,
   not an exception message, and not the helper socket. `switch_state()` is what
   the pages read, and it has no field a PSK could occupy.

Everything that shells out takes an injectable `runner` with `net_wifi.NmResult`'s
shape, and the Wi-Fi switch additionally takes an injectable `sleep` and clock —
the same bet `net_wifi.py` made, and the reason its state machine can be tested
here without NetworkManager, a radio or a network.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import time
from typing import Any, Callable, Optional

import net_wifi

# ── constants ───────────────────────────────────────────────────────
#: The name the box ships with (design §8). Not `armbian`. Case is deliberate
#: and is why the hostname validator allows uppercase.
DEFAULT_HOSTNAME = "MRRC-Modern"

#: The root helper's socket. Its absence is a normal state, not an error: the
#: desktop builds have no helper and the box's helper may be restarting.
HELPER_SOCKET = "/run/mrrc/box-helper.sock"

#: The units the settings page reports. `mrrc-modern` is this server; the other
#: two are the ones a broken network is usually hiding behind.
SERVICES = ("mrrc-modern.service", "NetworkManager.service",
            "avahi-daemon.service")

#: nmcli prints a boolean as yes/no in the C locale and 是/否 in a Chinese one.
#: The box's locale is not ours to pin, and the symptom of getting this wrong is
#: a user's own autoconnect switch rendering as off.
YES_WORDS = ("yes", "是")
NO_WORDS = ("no", "否")

#: The safety invariants shown on the settings page. Every one is read-only:
#: that page reports, it does not set. The notes travel with the values because a
#: number without its consequence beside it is a number nobody respects.
SAFETY_FIELDS = (
    ("MRRC_ALLOW_UNVERIFIED_TX", "0",
     "发射门禁。为 1 时允许对未验证机型发射，指令表来自 Hamlib/手册而非真机实测；"
     "8 个未验证机型必须为 0（AD-019 / NFR-067）。改它要两步显式确认。"),
    ("MRRC_PTT_MAX_TX_SECONDS", "0",
     "PTT 最长连续发射秒数，0 = 关闭看门狗。调高或清空会削弱防线②。"),
    ("MRRC_REMOTE_SESSION_TX_HEARTBEAT_S", "",
     "远程会话的发射心跳超时，由 Cloud Hub 写入（防线①）。本页不改。"),
)


# ── transport ───────────────────────────────────────────────────────
def _run(argv: list, runner: Optional[Callable] = None) -> net_wifi.NmResult:
    """Run argv, or hand it to the injected runner. Same shape either way.

    Everything here that is not `nmcli` (systemctl, hostnamectl, timedatectl)
    goes through this too, so a test can answer all of it from one fake.
    """
    if runner is not None:
        return runner(argv)
    return net_wifi.subprocess_runner(argv)


def _values(field: str, runner: Optional[Callable] = None) -> list:
    """One nmcli `-g` field across every device, blanks dropped."""
    result = net_wifi.nmcli(["-t", "-g", field, "device", "show"], runner)
    if not result.ok:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


# ── the box's own networks ──────────────────────────────────────────
def local_subnets(runner: Optional[Callable] = None) -> list:
    """Every IPv4 network this box holds an address on.

    The onboarding window asks "is the client on one of my own subnets". The
    retired hotspot design could ask that about a single known network because it
    had created it; a box that arrives over a cable or a Wi-Fi somebody else
    configured has to ask itself.

    An empty list is the failure-safe answer, not an error: no addresses means no
    window (spec §5.1).
    """
    out = []
    for text in _values("IP4.ADDRESS", runner):
        try:
            out.append(ipaddress.ip_network(text, strict=False))
        except ValueError:
            continue
    return out


def client_in_local_subnet(host: Optional[str],
                           runner: Optional[Callable] = None) -> bool:
    """Whether `host` is on a subnet this box itself holds an address on.

    Accepts the IPv4-mapped form Starlette produces for a v4 client on a v6
    socket (`::ffff:192.168.16.50`) — refusing that form would close the window
    on exactly the client it was opened for.
    """
    text = (host or "").strip()
    if not text:
        return False
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.version != 4:
        return False
    return any(address in network for network in local_subnets(runner))


# ── read-only network view ──────────────────────────────────────────
def scan_networks(runner: Optional[Callable] = None) -> list:
    """Visible Wi-Fi, strongest first. The retired wizard's scan, unchanged."""
    return net_wifi.scan_wifi(runner)


def wifi_status(runner: Optional[Callable] = None) -> dict:
    """The station link, if there is one: SSID and signal."""
    result = net_wifi.nmcli(
        ["-t", "-f", "ACTIVE,SSID,SIGNAL", "device", "wifi", "list"], runner)
    if result.ok:
        for row in net_wifi.parse_rows(result.stdout):
            if len(row) >= 3 and row[0].strip() in YES_WORDS:
                return {"ssid": row[1], "signal": net_wifi._int_or(row[2])}
    return {"ssid": "", "signal": 0}


def connections_report(runner: Optional[Callable] = None) -> list:
    """Every saved connection, with the one field a user has to be able to see.

    `autoconnect` is here because its mishandling is a field-verified failure
    mode: a profile that brings itself up at boot is how a box takes its own
    Wi-Fi down — the retired hotspot's `Hotspot` profile did exactly that, and
    every profile this module writes states the value explicitly.
    """
    result = net_wifi.nmcli(
        ["-t", "-f", "NAME,TYPE,AUTOCONNECT", "connection", "show"], runner)
    if not result.ok:
        return []
    out = []
    for row in net_wifi.parse_rows(result.stdout):
        if len(row) < 3:
            continue
        out.append({"name": row[0], "type": row[1],
                    "autoconnect": row[2].strip() in YES_WORDS})
    return out


def network_report(runner: Optional[Callable] = None) -> dict:
    """The network health card: where it broke, in one glance.

    Every call is unprivileged — this is the half of the retired `/api/manage`
    worth keeping, plus the two facts (default route holder, per-profile
    autoconnect) that a user actually needs when the box is unreachable.
    """
    return {
        "addresses": _values("IP4.ADDRESS", runner),
        "gateways": _values("IP4.GATEWAY", runner),
        "dns": _values("IP4.DNS", runner),
        "devices": net_wifi.devices(runner),
        "connections": connections_report(runner),
        "wifi": wifi_status(runner),
        "nm_running": net_wifi.nm_running(runner),
    }


# ── read-only system view ───────────────────────────────────────────
def _unit_active(unit: str, runner: Optional[Callable] = None) -> bool:
    result = _run(["systemctl", "is-active", unit], runner)
    return result.ok and result.stdout.strip() == "active"


def services_report(runner: Optional[Callable] = None) -> list:
    """Read-only: `mrrc` may ask systemd about a unit without any helper."""
    return [{"unit": unit, "active": _unit_active(unit, runner)}
            for unit in SERVICES]


def timezone() -> str:
    """The IANA zone name, or "" when it cannot be read.

    /etc/localtime is a symlink into /usr/share/zoneinfo everywhere Debian-ish;
    when it is a plain file (somebody copied it) the name is simply unknown, and
    an empty string is the honest answer.
    """
    marker = "/usr/share/zoneinfo/"
    try:
        target = os.path.realpath("/etc/localtime")
    except OSError:
        return ""
    index = target.find(marker)
    return target[index + len(marker):] if index >= 0 else ""


def uptime_seconds() -> float:
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as handle:
            return float(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def system_report(runner: Optional[Callable] = None) -> dict:
    return {
        "timezone": timezone(),
        "uptime_s": uptime_seconds(),
        "services": services_report(runner),
    }


def identity_report(runner: Optional[Callable] = None) -> dict:
    """The box's name, and both ways a client can reach it afterwards.

    `<name>.local` is perfect on iOS, needs Bonjour installed on Windows and is a
    lottery on Android, so the addresses travel with it — after a network switch
    they are the fallback that always works (design §8).
    """
    name = socket.gethostname() or ""
    return {
        "hostname": name,
        "mdns": f"{name}.local" if name else "",
        "addresses": [value.split("/")[0]
                      for value in _values("IP4.ADDRESS", runner)],
        "suggested_name": DEFAULT_HOSTNAME,
    }


def safety_report(environ: Optional[Any] = None) -> list:
    """The transmit gates, reported and never set.

    A web switch is the shape that turns an invariant into an incident, so these
    are read-only here; changing one belongs with the config layer and its two
    confirmations.
    """
    env = os.environ if environ is None else environ
    return [{"key": key, "value": env.get(key, default),
             "read_only": True, "note": note}
            for key, default, note in SAFETY_FIELDS]


# ── the root helper ─────────────────────────────────────────────────
def helper_available(path: str = HELPER_SOCKET) -> bool:
    """Whether the helper's socket is there. Deliberately just a path check.

    It is allowed to be wrong in one direction only: it may say False when the
    helper would in fact have answered. Saying True with no socket behind it puts
    a button on a page that cannot work.
    """
    return bool(path) and os.path.exists(path)
```

- [ ] **Step 4: 跑测试**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
.venv/bin/pytest tests/test_box_ops_reports.py -q
```

期望：全绿。

若有 `FakeNmcli` 的匹配失败（它的 `table` 是按子串、按插入顺序扫的），检查注入的键是不是被更短的键抢先匹配了 —— 例如 `"connection show"` 会先于 `"device status"` 命中 `nmcli device show`。必要时把更具体的键放前面。

- [ ] **Step 5: 把 box_ops 加回打包与镜像门禁**

`packaging/box/box-overlay.sh` 的 import gate（Task 3 改过的那行）：

```bash
	PYTHONDONTWRITEBYTECODE=1 "$VENV/bin/python" -c "import server; import sys; sys.path.insert(0, '$MRRC_HOME/linux'); import first_run, mrrc_radio"
```

改成：

```bash
	PYTHONDONTWRITEBYTECODE=1 "$VENV/bin/python" -c "import server; import sys; sys.path.insert(0, '$MRRC_HOME'); sys.path.insert(0, '$MRRC_HOME/linux'); import net_wifi, box_ops; import first_run, mrrc_radio"
```

`tests/test_image_imports.py` 的 `PROBE` 里，在 `probe("server", ...)` 那一行之后加：

```python
    probe("box_ops", lambda: importlib.import_module("box_ops"))
```

（`importlib` 在 `PROBE` 里已被 `ImageTree` 使用；若没有 `import importlib`，按已有的 `importlib.abc` 写法补。）

- [ ] **Step 6: 跑全套 + 提交**

```bash
.venv/bin/pytest tests/ -q
git add box_ops.py tests/test_box_ops_reports.py \
  packaging/box/box-overlay.sh tests/test_image_imports.py
git commit -m "feat: box_ops 只读半边（网络体检 / 系统 / 身份 / 安全字段）

新增 box_ops.py：可注入 runner 的纯逻辑后端，两个页面共用。locale 无关的
布尔解析（yes/是）、IPv4-mapped 客户端地址、拿不到子网即空集（窗口因此关闭）。
box-overlay.sh 与 test_image_imports.py 加上 box_ops 的可导入性断言。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 5: box_ops.py —— helper 客户端与四个系统动词

**Files:**
- Modify: `box_ops.py`（追加 helper 客户端一节）
- Create: `tests/test_box_ops_helper_client.py`

**Interfaces:**
- Consumes: Task 4 的 `HELPER_SOCKET`、`helper_available`
- Produces:
  - `call_helper(verb: str, *args, sender=None, timeout: float = 10.0) -> dict`（永远返回 `{"ok": bool, ...}`，不抛）
  - `hostname_problem(name: str) -> str`
  - `timezone_problem(zone: str) -> str`
  - `set_hostname(name: str, *, sender=None) -> dict`
  - `set_timezone(zone: str, *, sender=None) -> dict`
  - `reboot(*, sender=None) -> dict`
  - `reload_avahi(*, sender=None) -> dict`
  - `MIN_PASSWORD_LEN = 8`、`password_problem(password, confirm) -> str`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_box_ops_helper_client.py`：

```python
"""The client half of the root helper, tested without a helper.

`sender` is the injection point: it takes the request dict and a timeout and
returns one line of JSON. That is the whole transport, so everything below —
including the failure modes that matter — runs on the desktop builds too.

Two contracts this file exists for:

* **Nothing raises.** The helper may be absent (desktop, unit restarting, socket
  permissions wrong) and the page may still be open in front of a user. Every
  one of those is a `{"ok": False}` value, never an exception, because the
  alternative is a 500 in a request handler and a panel that hangs.
* **Nothing is concatenated.** The request is one JSON object with a verb and an
  argv-shaped list. There is no code path here that builds a command string.
"""
from __future__ import annotations

import json
import unittest

import box_ops


def answering(payload: dict = None, text: str = None):
    """A sender that records what it was asked and returns a canned reply."""
    calls = []

    def sender(request, timeout):
        calls.append(request)
        if text is not None:
            return text
        return json.dumps(payload if payload is not None else {"ok": True})

    sender.calls = calls
    return sender


def exploding(kind=OSError):
    def sender(request, timeout):
        raise kind("no such file or directory")
    return sender


class CallHelperTests(unittest.TestCase):
    def test_a_request_is_one_json_object_with_a_verb_and_a_list(self):
        sender = answering()
        box_ops.call_helper("set-hostname", "MRRC-Modern", sender=sender)
        self.assertEqual(
            json.loads(json.dumps(sender.calls[0])),
            {"verb": "set-hostname", "args": ["MRRC-Modern"]},
        )

    def test_arguments_are_stringified_so_the_line_never_fails_to_encode(self):
        sender = answering()
        box_ops.call_helper("set-timezone", "Asia/Shanghai", sender=sender)
        self.assertEqual(sender.calls[0]["args"], ["Asia/Shanghai"])

    def test_a_reply_is_parsed(self):
        sender = answering(payload={"ok": True, "note": "done"})
        self.assertEqual(box_ops.call_helper("reboot", sender=sender),
                         {"ok": True, "note": "done"})

    def test_an_unreachable_helper_is_a_value(self):
        result = box_ops.call_helper("reboot", sender=exploding())
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])

    def test_a_permission_error_is_also_a_value(self):
        result = box_ops.call_helper("reboot", sender=exploding(PermissionError))
        self.assertFalse(result["ok"])

    def test_garbage_from_the_helper_is_a_value(self):
        for text in ("", "not json", "[1, 2, 3]", "null", '{"ok":'):
            with self.subTest(text=text):
                result = box_ops.call_helper(
                    "reboot", sender=answering(text=text))
                self.assertFalse(result["ok"])
                self.assertTrue(result["error"])


class HostnameValidationTests(unittest.TestCase):
    def test_the_shipped_default_is_valid(self):
        """`MRRC-Modern` has an uppercase M. A lowercase-only regex — the shape
        every example on the internet uses — would reject the box's own name."""
        self.assertEqual(box_ops.hostname_problem("MRRC-Modern"), "")

    def test_ordinary_names_are_valid(self):
        for name in ("mrrc", "mrrc-modern", "box2", "a", "B" * 63):
            with self.subTest(name=name):
                self.assertEqual(box_ops.hostname_problem(name), "")

    def test_what_is_not_a_hostname(self):
        for name in ("", "   ", "-leading", "trailing-", "has space",
                     "has_underscore", "dot.name", "a" * 64, "名字",
                     "with/slash", "with:colon"):
            with self.subTest(name=name):
                self.assertTrue(box_ops.hostname_problem(name), name)


class TimezoneValidationTests(unittest.TestCase):
    def test_a_real_zone_passes(self):
        for zone in ("Asia/Shanghai", "UTC", "Europe/London"):
            with self.subTest(zone=zone):
                if zone == "UTC" or __import__("os").path.exists(
                        "/usr/share/zoneinfo/" + zone):
                    self.assertEqual(box_ops.timezone_problem(zone), "")

    def test_a_path_traversal_or_a_made_up_zone_does_not(self):
        for zone in ("", "  ", "..", "../../etc/passwd", "/etc/passwd",
                     "Asia/../../etc/passwd", "Not/AZone"):
            with self.subTest(zone=zone):
                self.assertTrue(box_ops.timezone_problem(zone), zone)


class VerbTests(unittest.TestCase):
    def test_set_hostname_validates_before_it_asks(self):
        """A rejected name must never reach the socket: the helper validates too,
        but a page that round-trips to find out is a page that reports the
        helper's error wording instead of its own."""
        sender = answering()
        result = box_ops.set_hostname("-bad", sender=sender)
        self.assertFalse(result["ok"])
        self.assertEqual(sender.calls, [])

    def test_set_hostname_forwards_a_good_name(self):
        sender = answering()
        box_ops.set_hostname("MRRC-Modern", sender=sender)
        self.assertEqual(sender.calls[0],
                         {"verb": "set-hostname", "args": ["MRRC-Modern"]})

    def test_set_timezone_validates_before_it_asks(self):
        sender = answering()
        result = box_ops.set_timezone("../etc", sender=sender)
        self.assertFalse(result["ok"])
        self.assertEqual(sender.calls, [])

    def test_reboot_and_avahi_reload_take_no_arguments(self):
        sender = answering()
        box_ops.reboot(sender=sender)
        box_ops.reload_avahi(sender=sender)
        self.assertEqual([c["verb"] for c in sender.calls],
                         ["reboot", "avahi-reload"])
        self.assertEqual([c["args"] for c in sender.calls], [[], []])


class PasswordValidationTests(unittest.TestCase):
    def test_a_short_password_is_refused(self):
        self.assertTrue(box_ops.password_problem("short", "short"))

    def test_a_mismatch_is_refused(self):
        self.assertTrue(
            box_ops.password_problem("longenough1", "longenough2"))

    def test_an_empty_password_is_refused(self):
        self.assertTrue(box_ops.password_problem("", ""))

    def test_eight_characters_is_the_floor(self):
        """The same floor the existing POST /api/setup enforces — the onboarding
        page must not be the softer door into the same key."""
        self.assertEqual(
            box_ops.password_problem("12345678", "12345678"), "")

    def test_surrounding_spaces_are_part_of_the_password(self):
        """A password is used byte-for-byte. Trimming it here would set a
        password the user did not type, and they would not be able to log in."""
        self.assertTrue(
            box_ops.password_problem(" 12345678", "12345678"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑它，确认它红**

```bash
.venv/bin/pytest tests/test_box_ops_helper_client.py -q
```

期望：`AttributeError: module 'box_ops' has no attribute 'call_helper'`。

- [ ] **Step 3: 写实现**

在 `box_ops.py` 的 `helper_available` 下方追加（文件末尾）：

```python
# ── the root helper, client side ────────────────────────────────────
def _send_over_helper_socket(payload: dict, timeout: float) -> str:
    """One line of JSON out, one line back, over the helper's unix socket.

    The socket is 0660 root:mrrc, so the kernel refuses anyone else at connect()
    time; the helper checks SO_PEERCRED on top of that, because a mode is a check
    made once and the helper would rather make its own.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(HELPER_SOCKET)
        client.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        chunks = []
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
    return b"".join(chunks).decode("utf-8", "replace").split("\n", 1)[0]


def call_helper(verb: str, *args, sender: Optional[Callable] = None,
                timeout: float = 10.0) -> dict:
    """Ask the root helper to do one of its four verbs. Never raises.

    Absent helper, refused connection, an unreadable reply: all of them are
    `{"ok": False, "error": ...}`. The settings page has to grey a panel out and
    keep the other panels working, and it cannot do that from an exception.
    """
    payload = {"verb": verb, "args": [str(arg) for arg in args]}
    if sender is None:
        sender = _send_over_helper_socket
    try:
        reply_text = sender(payload, timeout)
    except OSError as exc:
        return {"ok": False,
                "error": f"helper unavailable ({exc.__class__.__name__})"}
    try:
        reply = json.loads(reply_text)
    except (TypeError, ValueError):
        return {"ok": False, "error": "helper sent an unreadable reply"}
    if not isinstance(reply, dict) or "ok" not in reply:
        return {"ok": False, "error": "helper sent an unreadable reply"}
    return reply


# ── the four system verbs, validated before they are asked for ──────
#: Case-open on purpose: the shipped default is `MRRC-Modern`. systemd accepts a
#: static hostname in either case and DNS is case-insensitive, so
#: `mrrc-modern.local` still resolves (design §8).
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")

ZONEINFO = "/usr/share/zoneinfo"


def hostname_problem(name: str) -> str:
    """Why this name cannot be the box's hostname, or "" when it can."""
    text = (name or "").strip()
    if not text:
        return "名字不能为空"
    if not HOSTNAME_RE.match(text):
        return ("名字只能用字母、数字和连字符，不能以连字符开头或结尾，"
                "最长 63 个字符")
    return ""


def timezone_problem(zone: str) -> str:
    """Why this zone cannot be set, or "" when it can.

    The helper checks all of this again before running anything as root. This
    copy exists so the page reports its own wording rather than a round trip's,
    not as a substitute for the one that guards the privileged side.
    """
    text = (zone or "").strip()
    if not text or text != zone or text.startswith("/") or ".." in text:
        return "时区名不合法"
    if not os.path.isfile(os.path.join(ZONEINFO, text)):
        return f"没有这个时区：{text}"
    return ""


def set_hostname(name: str, *, sender: Optional[Callable] = None) -> dict:
    problem = hostname_problem(name)
    if problem:
        return {"ok": False, "error": problem}
    return call_helper("set-hostname", (name or "").strip(), sender=sender)


def set_timezone(zone: str, *, sender: Optional[Callable] = None) -> dict:
    problem = timezone_problem(zone)
    if problem:
        return {"ok": False, "error": problem}
    return call_helper("set-timezone", (zone or "").strip(), sender=sender)


def reboot(*, sender: Optional[Callable] = None) -> dict:
    """Restart the box, not the service. `POST /api/restart` is the other one."""
    return call_helper("reboot", sender=sender)


def reload_avahi(*, sender: Optional[Callable] = None) -> dict:
    """Nudge avahi after a rename, so the new name is advertised immediately."""
    return call_helper("avahi-reload", sender=sender)


# ── the password ────────────────────────────────────────────────────
#: The floor the existing POST /api/setup enforces. The onboarding page must not
#: be the softer door into the same key.
MIN_PASSWORD_LEN = 8


def password_problem(password: str, confirm: str) -> str:
    """Why this password cannot be set, or "" when it can.

    Nothing is trimmed: a password is used byte-for-byte, and silently removing a
    leading space would set a password the user did not type — which they would
    discover at the login page.
    """
    if not password:
        return "口令不能为空"
    if len(password) < MIN_PASSWORD_LEN:
        return f"口令至少 {MIN_PASSWORD_LEN} 位"
    if password != confirm:
        return "两次输入的口令不一致"
    return ""
```

并把顶部的 import 补全：

```python
import ipaddress
import json
import os
import re
import socket
import time
from typing import Any, Callable, Optional
```

- [ ] **Step 4: 跑测试**

```bash
.venv/bin/pytest tests/test_box_ops_helper_client.py tests/test_box_ops_reports.py -q
```

期望：全绿。

- [ ] **Step 5: 提交**

```bash
git add box_ops.py tests/test_box_ops_helper_client.py
git commit -m "feat: box_ops 的 helper 客户端与四个系统动词

call_helper 一行 JSON 进出，任何失败都是 {"ok": false} 而不是异常（桌面版
必须靠这个降级）。hostname/时区在发出去之前先本地校验，这样页面报自己的文案；
校验管的是「请求」，真正的守卫在 root 侧。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 6: linux/box_helper.py —— root helper

**Files:**
- Create: `linux/box_helper.py`
- Create: `tests/test_box_helper.py`

**Interfaces:**
- Consumes: 无（纯 stdlib，不 import `box_ops` 或 `net_wifi`）
- Produces:
  - `SOCKET_PATH = "/run/mrrc/box-helper.sock"`、`SOCKET_MODE = 0o660`、`PEER_USER = "mrrc"`
  - `argv_for(verb: str, args: list) -> Optional[list]`
  - `zone_ok(zone: str) -> bool`
  - `handle(request) -> dict`
  - `peer_is_allowed(peer_uid: int, expected: int) -> bool`
  - `sync_hosts(name: str, path: str = "/etc/hosts") -> bool`
  - `serve(socket_path: str = SOCKET_PATH, peer_uid: Optional[int] = None) -> None`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_box_helper.py`：

```python
"""The root helper's verb table, tested as a table.

This process runs as root and answers a socket. Everything that makes that
acceptable is in `argv_for`: four verbs, each with its own argument check, all of
them returning an argv *list* that a test can compare to an exact expected value.
There is no string concatenation anywhere, so there is no shell to inject into —
and the assertions below are written to keep it that way.

The peer check is tested as a pure function for the same reason: `SO_PEERCRED`
comes from the kernel, and the decision made from it is the part that can be
wrong.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "linux"))

import box_helper  # noqa: E402


class VerbTableTests(unittest.TestCase):
    def test_reboot_takes_no_arguments(self):
        self.assertEqual(box_helper.argv_for("reboot", []),
                         ["systemctl", "reboot"])
        for args in (["now"], ["--force"], [""]):
            with self.subTest(args=args):
                self.assertIsNone(box_helper.argv_for("reboot", args))

    def test_avahi_reload_takes_no_arguments(self):
        self.assertEqual(box_helper.argv_for("avahi-reload", []),
                         ["systemctl", "reload", "avahi-daemon"])
        self.assertIsNone(box_helper.argv_for("avahi-reload", ["x"]))

    def test_set_hostname_accepts_the_shipped_default(self):
        """`MRRC-Modern` is the name in the image. A lowercase-only validator
        would leave the box unable to keep its own hostname."""
        self.assertEqual(
            box_helper.argv_for("set-hostname", ["MRRC-Modern"]),
            ["hostnamectl", "set-hostname", "--static", "MRRC-Modern"])

    def test_set_hostname_refuses_anything_that_is_not_a_hostname(self):
        for args in ([], ["-a"], ["--help"], ["a b"], ["a/b"], ["a:b"],
                     [".."], ["a" * 64], ["名字"], ["$(reboot)"],
                     ["a;reboot"], ["`reboot`"], ["a\nreboot"]):
            with self.subTest(args=args):
                self.assertIsNone(box_helper.argv_for("set-hostname", args))

    def test_set_timezone_needs_a_real_zone(self):
        if os.path.isfile("/usr/share/zoneinfo/UTC"):
            self.assertEqual(box_helper.argv_for("set-timezone", ["UTC"]),
                             ["timedatectl", "set-timezone", "UTC"])

    def test_set_timezone_refuses_a_path_or_a_made_up_zone(self):
        for args in ([], [".."], ["../../etc/passwd"], ["/etc/passwd"],
                     ["-x"], ["Not/AZone"], ["Asia/Shanghai/x"],
                     ["Asia/Shanghai\nreboot"]):
            with self.subTest(args=args):
                self.assertIsNone(box_helper.argv_for("set-timezone", args))

    def test_an_unknown_verb_is_refused(self):
        for verb in ("", "sh", "exec", "install", "rm", "systemctl", None):
            with self.subTest(verb=verb):
                self.assertIsNone(box_helper.argv_for(verb, []))

    def test_no_verb_can_ever_reach_a_shell(self):
        """The argv is a list, and its first element is a fixed absolute-looking
        program name. There is no `sh -c` in the table, so there is nothing for a
        metacharacter to be interpreted by."""
        cases = [("reboot", []), ("avahi-reload", []),
                 ("set-hostname", ["box"]),
                 ("set-timezone", ["UTC"])]
        for verb, args in cases:
            argv = box_helper.argv_for(verb, args)
            if argv is None:
                continue
            with self.subTest(verb=verb):
                self.assertIsInstance(argv, list)
                self.assertNotIn("sh", argv)
                self.assertNotIn("-c", argv)
                self.assertTrue(all(isinstance(part, str) for part in argv))


class HandleTests(unittest.TestCase):
    def test_a_malformed_request_is_refused_without_running_anything(self):
        for request in (None, [], "reboot", 7,
                        {"verb": "reboot", "args": "now"},
                        {"verb": "reboot", "args": [1, 2]},
                        {"args": []}, {}):
            with self.subTest(request=request):
                reply = box_helper.handle(request)
                self.assertFalse(reply["ok"])
                self.assertTrue(reply["error"])

    def test_the_reply_is_always_a_dict_with_ok(self):
        for request in ({}, {"verb": "nope"}, {"verb": "reboot"}):
            with self.subTest(request=request):
                reply = box_helper.handle(request)
                self.assertIsInstance(reply, dict)
                self.assertIn("ok", reply)


class PeerTests(unittest.TestCase):
    def test_only_the_expected_uid_is_allowed(self):
        self.assertTrue(box_helper.peer_is_allowed(1001, 1001))
        for uid in (0, 1000, 1002, 65534):
            with self.subTest(uid=uid):
                self.assertFalse(box_helper.peer_is_allowed(uid, 1001))


class SyncHostsTests(unittest.TestCase):
    def test_it_replaces_the_127_0_1_1_line(self):
        """`hostnamectl set-hostname` stopped touching /etc/hosts in systemd 240.
        Without this, the box answers to its new name on mDNS and to nothing at
        all locally — and Debian's `hostname -f` starts failing, which `sudo`
        and some mailers use."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hosts"
            path.write_text("127.0.0.1\tlocalhost\n"
                            "127.0.1.1\tarmbian\n"
                            "::1\tlocalhost ip6-localhost\n",
                            encoding="utf-8")
            box_helper.sync_hosts("MRRC-Modern", str(path))
            text = path.read_text(encoding="utf-8")
            self.assertIn("127.0.1.1\tMRRC-Modern", text)
            self.assertNotIn("armbian", text)
            self.assertIn("127.0.0.1\tlocalhost", text)
            self.assertIn("::1\tlocalhost ip6-localhost", text)

    def test_it_adds_the_line_when_there_is_none(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hosts"
            path.write_text("127.0.0.1\tlocalhost\n", encoding="utf-8")
            box_helper.sync_hosts("MRRC-Modern", str(path))
            self.assertIn("127.0.1.1\tMRRC-Modern",
                          path.read_text(encoding="utf-8"))

    def test_a_missing_file_is_reported_not_fatal(self):
        self.assertFalse(box_helper.sync_hosts("box", "/no/such/hosts"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑它，确认它红**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
.venv/bin/pytest tests/test_box_helper.py -q
```

期望：`ModuleNotFoundError: No module named 'box_helper'`（`linux/` 里还没有它）。

- [ ] **Step 3: 写实现**

创建 `linux/box_helper.py`：

```python
#!/usr/bin/env python3
"""The four system verbs the web pages may ask for, and nothing else.

Why a separate root process (design §7.3): `mrrc` may *read* NetworkManager, but
`hostnamectl`, `timedatectl` and `systemctl reboot` are root's. Shrinking that
authority to a fixed verb table is cheaper to audit than a sudoers file, and it
keeps the Wi-Fi password out of a second channel — `mrrc` writes NetworkManager
itself (the polkit rule installed beside this file is what lets it), so a PSK
never comes near this socket.

Transport: one line of JSON in, one line out, over /run/mrrc/box-helper.sock
(0660 root:mrrc; the directory comes from the unit's RuntimeDirectory= with
Group=mrrc). The peer's uid comes from SO_PEERCRED and must be `mrrc` — a socket
mode is a check the kernel makes at connect() time, and this is the check it does
not make.

No shell, ever: every verb is an argv array built here from a validated argument,
so there is nothing for a metacharacter to be interpreted by. Nothing a client
sends is ever concatenated into a command string.

Runs as root under mrrc-box-helper.service. stdlib only — it is copied into the
image beside the other linux/ entry points and must import cleanly with no venv
site-packages beyond what the image has.
"""
from __future__ import annotations

import json
import os
import re
import socket
import struct
import subprocess
import sys
from pathlib import Path
from typing import Optional

SOCKET_PATH = "/run/mrrc/box-helper.sock"
SOCKET_MODE = 0o660
#: The one uid that may ask. server.py runs as this user (User=mrrc in
#: mrrc-modern.service); nothing else on the box has any business here.
PEER_USER = "mrrc"
MAX_LINE = 4096
COMMAND_TIMEOUT_S = 30

HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
ZONEINFO = "/usr/share/zoneinfo"


def log(message: str) -> None:
    """journald picks this up (`journalctl -u mrrc-box-helper`). No verb here
    takes a sensitive argument, so there is nothing to redact — and nothing is
    logged beyond the verb and its program's own error text."""
    print(f"box-helper: {message}", flush=True)


# ── the verb table ──────────────────────────────────────────────────
def zone_ok(zone: str) -> bool:
    """A real zone under /usr/share/zoneinfo, with no way out of it.

    `timedatectl` would reject a bad name anyway. The check is here because this
    argument reaches a root process, and "it fails safely downstream" is not the
    same as "it was checked".
    """
    if not zone or zone != zone.strip() or zone.startswith("/"):
        return False
    if ".." in zone or "\n" in zone or "\r" in zone:
        return False
    root = os.path.realpath(ZONEINFO)
    target = os.path.realpath(os.path.join(root, zone))
    return (target == root or target.startswith(root + os.sep)) \
        and os.path.isfile(target)


def argv_for(verb: str, args: list) -> Optional[list]:
    """The command for this verb, or None when the verb or its arguments lie.

    Validation lives here rather than at the call site so there is exactly one
    place to read when asking "what can this socket make the box do".
    """
    if verb == "reboot" and not args:
        return ["systemctl", "reboot"]
    if verb == "avahi-reload" and not args:
        return ["systemctl", "reload", "avahi-daemon"]
    if verb == "set-hostname" and len(args) == 1 and HOSTNAME_RE.match(args[0]):
        return ["hostnamectl", "set-hostname", "--static", args[0]]
    if verb == "set-timezone" and len(args) == 1 and zone_ok(args[0]):
        return ["timedatectl", "set-timezone", args[0]]
    return None


def sync_hosts(name: str, path: str = "/etc/hosts") -> bool:
    """Keep the 127.0.1.1 line in step with the hostname.

    `hostnamectl set-hostname` has not touched /etc/hosts since systemd 240, so
    without this the box answers to its new name on mDNS and to nothing at all
    locally — and Debian's `hostname -f`, which sudo and some mailers use, starts
    failing. The hostname is already set by the time this runs, so a failure here
    is reported and not fatal.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError as exc:
        log(f"could not read {path}: {exc}")
        return False
    out, replaced = [], False
    for line in lines:
        fields = line.split()
        if fields and fields[0] == "127.0.1.1":
            out.append(f"127.0.1.1\t{name}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"127.0.1.1\t{name}")
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(out) + "\n")
    except OSError as exc:
        log(f"could not write {path}: {exc}")
        return False
    return True


def handle(request) -> dict:
    """Answer one request. Never raises: a crash here would take the socket with
    it, and the page would show a dead panel instead of an error."""
    if not isinstance(request, dict):
        return {"ok": False, "error": "malformed request"}
    verb = request.get("verb")
    args = request.get("args")
    if args is None:
        args = []
    if not isinstance(verb, str) or not isinstance(args, list) \
            or not all(isinstance(arg, str) for arg in args):
        return {"ok": False, "error": "malformed request"}
    argv = argv_for(verb, args)
    if argv is None:
        log(f"refused verb {verb!r} with {len(args)} argument(s)")
        return {"ok": False, "error": f"refused: {verb}"}
    try:
        completed = subprocess.run(argv, capture_output=True, text=True,
                                   timeout=COMMAND_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"{verb}: {exc}")
        return {"ok": False, "error": f"{argv[0]} could not run"}
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        log(f"{verb} failed ({completed.returncode}): {detail}")
        return {"ok": False, "error": detail or f"{verb} failed"}
    if verb == "set-hostname":
        sync_hosts(args[0])
    log(f"{verb} ok")
    return {"ok": True}


# ── the socket ──────────────────────────────────────────────────────
def peer_is_allowed(peer_uid: int, expected: int) -> bool:
    """SO_PEERCRED comes from the kernel; this is the decision made from it."""
    return peer_uid == expected


def _peer_uid(conn) -> Optional[int]:
    try:
        raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                              struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw)
        return uid
    except OSError:
        return None


def _expecting_uid() -> Optional[int]:
    try:
        import pwd
        return pwd.getpwnam(PEER_USER).pw_uid
    except (ImportError, KeyError):
        return None


def _answer(conn) -> None:
    chunks = []
    while True:
        chunk = conn.recv(MAX_LINE)
        if not chunk:
            break
        chunks.append(chunk)
        if b"\n" in chunk:
            break
    line = b"".join(chunks).split(b"\n", 1)[0]
    try:
        request = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        reply = {"ok": False, "error": "malformed request"}
    else:
        reply = handle(request)
    try:
        conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
    except OSError as exc:
        log(f"could not answer: {exc}")


def serve(socket_path: str = SOCKET_PATH,
          peer_uid: Optional[int] = None) -> None:
    expected = _expecting_uid() if peer_uid is None else peer_uid
    if expected is None:
        log(f"no `{PEER_USER}` user on this box; refusing to open the socket")
        return
    # systemd makes the directory (RuntimeDirectory= with Group=mrrc, 0770);
    # this is for a manual run, where it may not exist yet.
    os.makedirs(os.path.dirname(socket_path), exist_ok=True)
    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(socket_path)
    os.chmod(socket_path, SOCKET_MODE)
    server.listen(4)
    log(f"listening on {socket_path} for uid {expected}")
    while True:
        try:
            conn, _ = server.accept()
        except OSError as exc:
            log(f"accept failed: {exc}")
            continue
        with conn:
            uid = _peer_uid(conn)
            if uid is None or not peer_is_allowed(uid, expected):
                log(f"refused a peer with uid {uid}")
                continue
            _answer(conn)


if __name__ == "__main__":
    serve()
```

- [ ] **Step 4: 跑测试**

```bash
.venv/bin/pytest tests/test_box_helper.py -q
```

期望：全绿。

- [ ] **Step 5: 跑全套 + 提交**

```bash
.venv/bin/pytest tests/ -q
git add linux/box_helper.py tests/test_box_helper.py
git commit -m "feat: linux/box_helper.py —— 四个系统动词的 root helper

固定动词表逐条校验参数、argv 数组、绝不 shell=True；SO_PEERCRED 只认 mrrc。
set-hostname 之后同步 /etc/hosts 的 127.0.1.1 行（systemd 240 起
hostnamectl 不再管它）。纯 stdlib，不 import 本应用任何模块。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 7: box_ops.py —— Wi-Fi 切换状态机

这是整个功能最难的一段。单射频，切错了盒子就找不着了。

**Files:**
- Modify: `box_ops.py`（追加切换一节）
- Create: `tests/test_box_ops_wifi_switch.py`

**Interfaces:**
- Consumes: Task 4 的 `_run`、`wifi_status`；`net_wifi.connect_wifi`、`net_wifi.ipv4_address`、`net_wifi.nmcli`、`net_wifi.parse_rows`、`net_wifi.scrub`
- Produces:
  - `PHASE_IDLE = "idle"`、`PHASE_SWITCHING = "switching"`、`PHASE_OK = "ok"`、`PHASE_FAILED = "failed"`
  - `SWITCH_BUDGET_S = 30.0`、`SWITCH_POLL_S = 1.0`
  - `class SwitchState`，含 `phase`、`ssid`、`reason`、`address`、`since`、`snapshot() -> dict`、`reset()`
  - `switch_state(state=None) -> dict`
  - `perform_wifi_switch(ssid, password="", *, runner=None, state=None, ifname=None, budget=SWITCH_BUDGET_S, poll=SWITCH_POLL_S, sleep=time.sleep, now=time.monotonic) -> dict`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_box_ops_wifi_switch.py`：

```python
"""The Wi-Fi switch: the one action that can lose the box.

The board has a single radio, so joining a network is not an addition, it is a
replacement — and if the operator is reaching the box *over* that radio, the
replacement takes their own connection away mid-request. Everything in this file
is about what happens on the other side of that.

Two lessons from the retired supervisor are pinned here, because both were paid
for on real hardware:

* a profile this module writes must state `connection.autoconnect` explicitly.
  The hotspot's profile defaulted to yes, came up on every boot, and took the
  board's Wi-Fi down with it.
* nmcli returns as soon as it has *associated*, which is well before DHCP has
  landed. Believing that return value is how a switch reports success and leaves
  a box with no address.

`sleep` and `now` are injected, so a thirty-second budget costs no wall clock.
"""
from __future__ import annotations

import unittest

import box_ops
import net_wifi
from tests.test_net_wifi import FakeNmcli


class Fake( object ):
    """A runner plus the address nmcli will report, changing over time."""

    def __init__(self, table=None, default=None, addresses=None):
        self.inner = FakeNmcli(table, default)
        #: One entry per `IP4.ADDRESS` poll. The last entry repeats.
        self.addresses = list(addresses or [""])
        self.polls = 0

    def __call__(self, argv):
        argv = list(argv)
        if "IP4.ADDRESS" in argv:
            index = min(self.polls, len(self.addresses) - 1)
            self.polls += 1
            return net_wifi.NmResult(0, self.addresses[index], "")
        return self.inner(argv)

    def count(self, key):
        return self.inner.count(key)

    def joined(self):
        return self.inner.joined()


def clock():
    """A monotonic clock that only moves when `sleep` is called."""
    state = {"t": 0.0}

    def now():
        return state["t"]

    def sleep(seconds):
        state["t"] += seconds

    return now, sleep


class StateTests(unittest.TestCase):
    def test_a_fresh_state_is_idle(self):
        snapshot = box_ops.switch_state(box_ops.SwitchState())
        self.assertEqual(snapshot["phase"], box_ops.PHASE_IDLE)
        self.assertEqual(snapshot["ssid"], "")
        self.assertEqual(snapshot["reason"], "")

    def test_the_snapshot_has_nowhere_to_put_a_password(self):
        """The whole reason the state is a small fixed dict rather than a
        free-form one: a PSK must have no key to land in."""
        snapshot = box_ops.switch_state(box_ops.SwitchState())
        self.assertEqual(set(snapshot),
                         {"phase", "ssid", "reason", "address", "since"})


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.now, self.sleep = clock()

    def run_switch(self, runner, **kwargs):
        return box_ops.perform_wifi_switch(
            "NewNet", "hunter2secret", runner=runner, state=box_ops.SwitchState(),
            sleep=self.sleep, now=self.now, **kwargs)

    def test_a_successful_switch_reports_the_address_it_got(self):
        runner = Fake({"device wifi connect": net_wifi.NmResult(0, "", ""),
                       "connection show": net_wifi.NmResult(
                           0, "home:802-11-wireless:yes\n", "")},
                      addresses=["", "192.168.1.217/24"])
        result = self.run_switch(runner)
        self.assertTrue(result["ok"])
        self.assertEqual(result["address"], "192.168.1.217/24")

    def test_it_polls_because_connecting_is_not_getting_an_address(self):
        """nmcli's success means associated, not addressed. The first poll here
        is empty and the switch must keep waiting rather than report success
        with no address."""
        runner = Fake({"device wifi connect": net_wifi.NmResult(0, "", "")},
                      addresses=["", "", "192.168.1.217/24"])
        result = self.run_switch(runner)
        self.assertTrue(result["ok"])
        self.assertEqual(runner.polls, 3)

    def test_it_does_not_believe_association_on_its_own(self):
        runner = Fake({"device wifi connect": net_wifi.NmResult(0, "", "")},
                      addresses=[""])
        result = self.run_switch(runner)
        self.assertFalse(result["ok"])

    def test_the_password_never_appears_in_the_result(self):
        """It travels in nmcli's argv and nowhere else — and the failure reason
        is exactly where it would leak, because nmcli's own error text is the one
        place a credential can ride along."""
        runner = Fake({
            "device wifi connect": net_wifi.NmResult(
                1, "", "Error: Secrets were required, but not provided. "
                       "hunter2secret rejected."),
        })
        result = self.run_switch(runner)
        self.assertFalse(result["ok"])
        self.assertNotIn("hunter2secret", result["reason"])
        self.assertNotIn("hunter2secret", str(result))

    def test_the_password_reaches_nmcli_and_only_there(self):
        runner = Fake({"device wifi connect": net_wifi.NmResult(0, "", "")},
                      addresses=["192.168.1.217/24"])
        self.run_switch(runner)
        self.assertIn("hunter2secret", runner.joined())

    def test_a_failure_is_reported_with_its_reason(self):
        runner = Fake({
            "device wifi connect": net_wifi.NmResult(
                1, "", "Error: No network with SSID 'NewNet' found."),
        })
        result = self.run_switch(runner)
        self.assertFalse(result["ok"])
        self.assertIn("NewNet", result["reason"])

    def test_the_state_moves_through_running_to_a_terminal_phase(self):
        runner = Fake({"device wifi connect": net_wifi.NmResult(0, "", "")},
                      addresses=["192.168.1.217/24"])
        state = box_ops.SwitchState()
        seen = []
        original = runner.__call__

        def watching(argv):
            seen.append(box_ops.switch_state(state)["phase"])
            return original(argv)

        runner.__call__ = watching
        box_ops.perform_wifi_switch("NewNet", "pw", runner=runner, state=state,
                                    sleep=self.sleep, now=self.now)
        self.assertIn(box_ops.PHASE_SWITCHING, seen)
        self.assertEqual(box_ops.switch_state(state)["phase"], box_ops.PHASE_OK)

    def test_a_second_switch_is_refused_while_one_is_running(self):
        state = box_ops.SwitchState()
        state.phase = box_ops.PHASE_SWITCHING
        result = box_ops.perform_wifi_switch(
            "Other", "pw", runner=Fake({}), state=state,
            sleep=self.sleep, now=self.now)
        self.assertFalse(result["ok"])


class RollbackTests(unittest.TestCase):
    def setUp(self):
        self.now, self.sleep = clock()

    def test_a_timeout_puts_the_old_network_back(self):
        """The budget is spent, no address ever arrived, and the profile we came
        from is what the box has to end up on — otherwise it is off the network
        with nobody able to reach it."""
        runner = Fake({
            "device wifi connect": net_wifi.NmResult(0, "", ""),
            "connection show": net_wifi.NmResult(
                0, "home:802-11-wireless:yes\n", ""),
        }, addresses=[""])
        result = box_ops.perform_wifi_switch(
            "NewNet", "pw", runner=runner, state=box_ops.SwitchState(),
            sleep=self.sleep, now=self.now)
        self.assertFalse(result["ok"])
        self.assertIn("connection up home", runner.joined())

    def test_the_failed_profile_does_not_come_back_on_its_own(self):
        """`autoconnect yes` on a network that just failed is a box that loses
        its Wi-Fi at every boot. The retired hotspot's profile did the mirror
        image of this."""
        runner = Fake({
            "device wifi connect": net_wifi.NmResult(0, "", ""),
            "connection show": net_wifi.NmResult(
                0, "home:802-11-wireless:yes\n", ""),
        }, addresses=[""])
        box_ops.perform_wifi_switch("NewNet", "pw", runner=runner,
                                    state=box_ops.SwitchState(),
                                    sleep=self.sleep, now=self.now)
        self.assertIn("connection.autoconnect no", runner.joined())

    def test_the_budget_is_actually_spent_not_ignored(self):
        runner = Fake({"device wifi connect": net_wifi.NmResult(0, "", "")},
                      addresses=[""])
        box_ops.perform_wifi_switch("NewNet", "pw", runner=runner,
                                    state=box_ops.SwitchState(), budget=5.0,
                                    poll=1.0, sleep=self.sleep, now=self.now)
        self.assertEqual(self.now(), 5.0)

    def test_the_reason_says_which_old_network_it_went_back_to(self):
        runner = Fake({
            "device wifi connect": net_wifi.NmResult(0, "", ""),
            "connection show": net_wifi.NmResult(
                0, "home:802-11-wireless:yes\n", ""),
        }, addresses=[""])
        result = box_ops.perform_wifi_switch(
            "NewNet", "pw", runner=runner, state=box_ops.SwitchState(),
            sleep=self.sleep, now=self.now)
        self.assertIn("home", result["reason"])

    def test_no_old_network_is_said_out_loud(self):
        runner = Fake({"device wifi connect": net_wifi.NmResult(0, "", "")},
                      addresses=[""])
        result = box_ops.perform_wifi_switch(
            "NewNet", "pw", runner=runner, state=box_ops.SwitchState(),
            sleep=self.sleep, now=self.now)
        self.assertFalse(result["ok"])
        self.assertTrue(result["reason"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑它，确认它红**

```bash
.venv/bin/pytest tests/test_box_ops_wifi_switch.py -q
```

期望：`AttributeError: module 'box_ops' has no attribute 'SwitchState'`。

- [ ] **Step 3: 写实现**

在 `box_ops.py` 的 `password_problem` 下方追加：

```python
# ── the Wi-Fi switch ────────────────────────────────────────────────
PHASE_IDLE = "idle"
PHASE_SWITCHING = "switching"
PHASE_OK = "ok"
PHASE_FAILED = "failed"

#: How long a new network has to produce an IPv4 before we put the old one back.
#: A first guess, to be calibrated on the real board (spec R-4).
SWITCH_BUDGET_S = 30.0
SWITCH_POLL_S = 1.0

#: What nmcli calls a Wi-Fi connection. Older and newer builds disagree.
WIFI_TYPES = ("802-11-wireless", "wifi")


class SwitchState:
    """The one switch this process is running, or the last one it ran.

    In memory, not on disk. The retired design published state through a JSON
    file because a root supervisor and the server were two processes; the switch
    now happens inside the server, so there is no second reader. The cost is that
    a server restart loses the progress screen, and the page says so rather than
    pretending (design §6.2).

    The field list is closed on purpose: `snapshot()` is what a page renders, and
    a PSK must have no key it could occupy.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.phase = PHASE_IDLE
        self.ssid = ""
        self.reason = ""
        self.address = ""
        self.since = 0.0

    def snapshot(self) -> dict:
        with self.lock:
            return {"phase": self.phase, "ssid": self.ssid,
                    "reason": self.reason, "address": self.address,
                    "since": self.since}

    def reset(self) -> None:
        with self.lock:
            self.phase = PHASE_IDLE
            self.ssid = ""
            self.reason = ""
            self.address = ""
            self.since = 0.0


#: The process-wide instance the endpoints read. Tests build their own.
_state = SwitchState()


def switch_state(state: Optional[SwitchState] = None) -> dict:
    return (state or _state).snapshot()


def _active_wifi_profiles(runner: Optional[Callable] = None) -> list:
    """The Wi-Fi connections that are up right now — the rollback targets."""
    result = net_wifi.nmcli(
        ["-t", "-f", "NAME,TYPE", "connection", "show", "--active"], runner)
    if not result.ok:
        return []
    return [row[0] for row in net_wifi.parse_rows(result.stdout)
            if len(row) >= 2 and row[1] in WIFI_TYPES]


def _finish_switch(state: SwitchState, ok: bool, reason: str,
                   address: str = "") -> dict:
    with state.lock:
        state.phase = PHASE_OK if ok else PHASE_FAILED
        state.reason = reason
        state.address = address
        state.since = time.time()
        ssid = state.ssid
    return {"ok": ok, "ssid": ssid, "reason": reason, "address": address}


def _roll_back(state: SwitchState, ssid: str, previous: list, password: str,
               budget: float, runner: Optional[Callable] = None) -> dict:
    """Put the box back on the network it came from, then say so.

    The failed profile is left in place but with `autoconnect no`, so a reboot
    does not retry a network that just failed. That is the retired hotspot's
    mistake in the other direction — its profile defaulted to yes, came up at
    every boot, and took the board's Wi-Fi down with it.
    """
    net_wifi.nmcli(["connection", "modify", ssid,
                    "connection.autoconnect", "no"], runner)
    for name in previous:
        net_wifi.nmcli(["connection", "up", name], runner)
    back_to = "、".join(previous) if previous else "（没有旧网络可回退）"
    reason = f"「{ssid}」在 {int(budget)} 秒内没有拿到地址，已回退到 {back_to}"
    return _finish_switch(state, False, net_wifi.scrub(reason, password))


def perform_wifi_switch(ssid: str, password: str = "", *,
                        runner: Optional[Callable] = None,
                        state: Optional[SwitchState] = None,
                        ifname: Optional[str] = None,
                        budget: float = SWITCH_BUDGET_S,
                        poll: float = SWITCH_POLL_S,
                        sleep: Callable = time.sleep,
                        now: Callable = time.monotonic) -> dict:
    """Join `ssid`, and fall back to whatever we were on if it does not take.

    The ordering, and why each step is where it is (design §6.2):

    1. Note the profiles that are up *before* touching anything. They are the
       rollback targets, and after `nmcli device wifi connect` they are no longer
       the active ones.
    2. Hand off to `net_wifi.connect_wifi`, which writes the new profile and
       brings it up. The password travels in its argv and nowhere else.
    3. Poll for an IPv4. nmcli returns once it has associated, which is well
       before DHCP has landed — believing that return value is how a switch looks
       successful and leaves the box with no address.
    4. On timeout, bring the old profile back and report why, scrubbed.

    Blocking on purpose: `server.py` runs this on a worker thread via
    `asyncio.to_thread`, so the state machine stays plain synchronous Python and
    this file keeps needing no event loop to be tested.
    """
    target = state or _state
    with target.lock:
        if target.phase == PHASE_SWITCHING:
            return {"ok": False, "ssid": ssid,
                    "reason": "已经有一次切换在进行中", "address": ""}
        target.phase = PHASE_SWITCHING
        target.ssid = ssid
        target.reason = ""
        target.address = ""
        target.since = time.time()

    previous = _active_wifi_profiles(runner)

    result = net_wifi.connect_wifi(ssid, password, runner=runner, ifname=ifname)
    if not result.ok:
        detail = net_wifi.scrub(result.stderr or result.stdout, password)
        return _finish_switch(target, False,
                              detail.strip() or f"无法连接「{ssid}」")

    address = ""
    deadline = now() + budget
    while now() < deadline:
        address = net_wifi.ipv4_address(ifname, runner)
        if address:
            break
        sleep(poll)

    if address:
        return _finish_switch(target, True, "", address)
    return _roll_back(target, ssid, previous, password, budget, runner)
```

顶部 import 补 `threading`：

```python
import ipaddress
import json
import os
import re
import socket
import threading
import time
from typing import Any, Callable, Optional
```

- [ ] **Step 4: 跑测试**

```bash
.venv/bin/pytest tests/test_box_ops_wifi_switch.py -q
```

期望：全绿。

- [ ] **Step 5: 提交**

```bash
.venv/bin/pytest tests/ -q
git add box_ops.py tests/test_box_ops_wifi_switch.py
git commit -m "feat: box_ops 的 Wi-Fi 切换状态机（含超时回滚）

先记录在用的 profile 作回滚目标 → net_wifi.connect_wifi → 轮询 IPv4（nmcli
的返回只代表关联成功）→ 超时则恢复旧 profile 并把新 profile 的 autoconnect
关掉。状态在进程内存里，字段封闭，密码无处可落。阻塞式实现，由 server 用
asyncio.to_thread 调，好让这个文件继续不需要事件循环就能测。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 8: box_ops.py —— 有线口的 DHCP / 静态

复用 Task 7 的状态与回滚思路，换掉「施加」那半步。

**Files:**
- Modify: `box_ops.py`
- Create: `tests/test_box_ops_link.py`

**Interfaces:**
- Consumes: Task 7 的 `SwitchState`、`PHASE_*`、`SWITCH_BUDGET_S`、`SWITCH_POLL_S`、`_finish_switch`、`_values`
- Produces:
  - `LINK_METHODS = ("auto", "manual")`
  - `link_problem(method: str, address: str) -> str`
  - `perform_link_change(profile, method, address="", gateway="", *, runner=None, state=None, budget=SWITCH_BUDGET_S, poll=SWITCH_POLL_S, sleep=time.sleep, now=time.monotonic) -> dict`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_box_ops_link.py`：

```python
"""Changing the wired profile, which can take the operator's own path away.

Same hazard as the Wi-Fi switch (design §6.4) and the same safety net: save the
old values, apply, wait for the address to actually change, and put the old
values back if it does not. The `apply` half differs; nothing else does.
"""
from __future__ import annotations

import unittest

import box_ops
import net_wifi
from tests.test_net_wifi import FakeNmcli


def clock():
    state = {"t": 0.0}
    return (lambda: state["t"]), (lambda seconds: state.__setitem__(
        "t", state["t"] + seconds))


class ValidationTests(unittest.TestCase):
    def test_the_two_methods_are_auto_and_manual(self):
        self.assertEqual(box_ops.LINK_METHODS, ("auto", "manual"))

    def test_a_manual_method_needs_an_address_with_a_prefix(self):
        self.assertEqual(
            box_ops.link_problem("manual", "192.168.16.217/24"), "")
        for address in ("", "192.168.16.217", "not-an-address",
                        "192.168.16.217/33", "192.168.16.217/0"):
            with self.subTest(address=address):
                self.assertTrue(box_ops.link_problem("manual", address))

    def test_an_auto_method_needs_no_address(self):
        self.assertEqual(box_ops.link_problem("auto", ""), "")

    def test_an_unknown_method_is_refused(self):
        for method in ("", "static", "dhcp", "AUTO", "disabled"):
            with self.subTest(method=method):
                self.assertTrue(box_ops.link_problem(method, ""))


class ChangeTests(unittest.TestCase):
    def setUp(self):
        self.now, self.sleep = clock()

    def change(self, runner, **kwargs):
        return box_ops.perform_link_change(
            "有线连接 2", "manual", "192.168.16.217/24", runner=runner,
            state=box_ops.SwitchState(), sleep=self.sleep, now=self.now,
            **kwargs)

    def test_a_successful_change_reports_the_new_address(self):
        runner = FakeNmcli({
            "ipv4.method": net_wifi.NmResult(0, "auto\n", ""),
            "ipv4.addresses": net_wifi.NmResult(0, "\n", ""),
            "IP4.ADDRESS": net_wifi.NmResult(0, "192.168.16.217/24\n", ""),
            "connection up": net_wifi.NmResult(0, "", ""),
        })
        result = self.change(runner)
        self.assertTrue(result["ok"])
        self.assertEqual(result["address"], "192.168.16.217/24")

    def test_it_writes_the_method_and_the_address(self):
        runner = FakeNmcli({
            "ipv4.method": net_wifi.NmResult(0, "auto\n", ""),
            "ipv4.addresses": net_wifi.NmResult(0, "\n", ""),
            "IP4.ADDRESS": net_wifi.NmResult(0, "192.168.16.217/24\n", ""),
            "connection up": net_wifi.NmResult(0, "", ""),
        })
        self.change(runner)
        self.assertIn("ipv4.method manual", runner.joined())
        self.assertIn("192.168.16.217/24", runner.joined())

    def test_the_old_values_are_read_before_they_are_overwritten(self):
        """Without this the rollback has nothing to restore, and the box is left
        on settings nobody chose."""
        runner = FakeNmcli({
            "ipv4.method": net_wifi.NmResult(0, "auto\n", ""),
            "ipv4.addresses": net_wifi.NmResult(0, "\n", ""),
            "IP4.ADDRESS": net_wifi.NmResult(0, "", ""),
            "connection up": net_wifi.NmResult(0, "", ""),
        })
        self.change(runner)
        self.assertIn("ipv4.method auto", runner.joined())

    def test_a_change_that_never_takes_is_rolled_back(self):
        runner = FakeNmcli({
            "ipv4.method": net_wifi.NmResult(0, "auto\n", ""),
            "ipv4.addresses": net_wifi.NmResult(0, "\n", ""),
            "IP4.ADDRESS": net_wifi.NmResult(0, "", ""),
            "connection up": net_wifi.NmResult(0, "", ""),
        }, default=net_wifi.NmResult(0, "", ""))
        result = self.change(runner)
        self.assertFalse(result["ok"])
        self.assertTrue(result["reason"])

    def test_a_failed_modify_does_not_leave_the_state_running(self):
        runner = FakeNmcli({
            "ipv4.method": net_wifi.NmResult(0, "auto\n", ""),
            "ipv4.addresses": net_wifi.NmResult(0, "\n", ""),
            "connection modify": net_wifi.NmResult(
                1, "", "Error: unknown connection."),
        })
        state = box_ops.SwitchState()
        result = box_ops.perform_link_change(
            "有线连接 2", "manual", "192.168.16.217/24", runner=runner,
            state=state, sleep=self.sleep, now=self.now)
        self.assertFalse(result["ok"])
        self.assertNotEqual(box_ops.switch_state(state)["phase"],
                            box_ops.PHASE_SWITCHING)

    def test_an_invalid_request_never_reaches_nmcli(self):
        runner = FakeNmcli({})
        result = box_ops.perform_link_change(
            "有线连接 2", "manual", "nonsense", runner=runner,
            state=box_ops.SwitchState(), sleep=self.sleep, now=self.now)
        self.assertFalse(result["ok"])
        self.assertEqual(runner.calls, [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑它，确认它红**

```bash
.venv/bin/pytest tests/test_box_ops_link.py -q
```

期望：`AttributeError: module 'box_ops' has no attribute 'LINK_METHODS'`。

- [ ] **Step 3: 写实现**

在 `box_ops.py` 的 `perform_wifi_switch` 下方追加：

```python
# ── the wired profile: DHCP or static ───────────────────────────────
#: `auto` is NM's name for DHCP; `manual` is its name for a static address.
LINK_METHODS = ("auto", "manual")


def link_problem(method: str, address: str) -> str:
    """Why this profile change cannot be made, or "" when it can."""
    if method not in LINK_METHODS:
        return "方式只能是 auto（自动获取）或 manual（静态地址）"
    if method == "auto":
        return ""
    text = (address or "").strip()
    try:
        network = ipaddress.ip_interface(text)
    except ValueError:
        return "静态地址要写成 地址/前缀，例如 192.168.16.217/24"
    if network.network.prefixlen == 0:
        return "前缀 /0 不是一台设备能用的地址"
    return ""


def _profile_value(profile: str, key: str,
                   runner: Optional[Callable] = None) -> str:
    """One setting of one saved connection, or "" when it is unset."""
    result = net_wifi.nmcli(
        ["-t", "-g", key, "connection", "show", profile], runner)
    if not result.ok:
        return ""
    for line in result.stdout.splitlines():
        if line.strip():
            return line.strip()
    return ""


def perform_link_change(profile: str, method: str, address: str = "",
                        gateway: str = "", *,
                        runner: Optional[Callable] = None,
                        state: Optional[SwitchState] = None,
                        budget: float = SWITCH_BUDGET_S,
                        poll: float = SWITCH_POLL_S,
                        sleep: Callable = time.sleep,
                        now: Callable = time.monotonic) -> dict:
    """Switch a saved connection between DHCP and a static address, with rollback.

    Same hazard as the Wi-Fi switch and for the same reason (design §6.4): if the
    operator is reaching the box over the profile being edited, the edit takes
    their own path away. So the shape is shared — read the old values first,
    apply, wait for the address to actually land, and restore on failure. Only
    the middle step differs.

    `nmcli connection up` is what makes the change take effect; `modify` alone
    changes the file and leaves the running device as it was, which is why the
    verify step can fail here even though every command returned 0.
    """
    problem = link_problem(method, address)
    if problem:
        return {"ok": False, "ssid": profile, "reason": problem, "address": ""}

    target = state or _state
    with target.lock:
        if target.phase == PHASE_SWITCHING:
            return {"ok": False, "ssid": profile,
                    "reason": "已经有一次网络改动在进行中", "address": ""}
        target.phase = PHASE_SWITCHING
        target.ssid = profile
        target.reason = ""
        target.address = ""
        target.since = time.time()

    old_method = _profile_value(profile, "ipv4.method", runner) or "auto"
    old_addresses = _profile_value(profile, "ipv4.addresses", runner)

    argv = ["connection", "modify", profile, "ipv4.method", method]
    if method == "manual":
        argv += ["ipv4.addresses", address.strip()]
        if gateway:
            argv += ["ipv4.gateway", gateway.strip()]
    result = net_wifi.nmcli(argv, runner)
    if not result.ok:
        detail = (result.stderr or result.stdout).strip()
        return _finish_switch(target, False,
                              detail or f"改不动「{profile}」")

    net_wifi.nmcli(["connection", "up", profile], runner)

    expected = address.strip().split("/")[0]
    landed = _expected_address(expected, runner, budget, poll, sleep, now)
    if landed:
        return _finish_switch(target, True, "", landed)

    _restore_profile(profile, old_method, old_addresses, runner)
    reason = (f"「{profile}」改了之后没拿到地址，已恢复为 {old_method}"
              + (f" {old_addresses}" if old_addresses else ""))
    return _finish_switch(target, False, reason)


def _expected_address(expected: str, runner, budget: float, poll: float,
                      sleep: Callable, now: Callable) -> str:
    """Wait for the address we asked for, or any address when we asked for DHCP.

    DHCP has no expected value to compare against, so "did it get one" is the
    question — which is exactly the question the Wi-Fi switch asks too.
    """
    deadline = now() + budget
    while now() < deadline:
        addresses = [value.split("/")[0] for value in _values("IP4.ADDRESS",
                                                             runner)]
        if expected:
            if expected in addresses:
                return f"{expected}/"
        elif addresses:
            return addresses[0]
        sleep(poll)
    return ""


def _restore_profile(profile: str, method: str, addresses: str,
                     runner: Optional[Callable] = None) -> None:
    argv = ["connection", "modify", profile, "ipv4.method", method]
    if method == "manual" and addresses:
        argv += ["ipv4.addresses", addresses]
    net_wifi.nmcli(argv, runner)
    net_wifi.nmcli(["connection", "up", profile], runner)
```

> `_expected_address` 在成功时返回 `"192.168.16.217/"`（带斜杠）是为了让「拿到的地址」这个字段与其它路径的形状一致（`net_wifi.ipv4_address` 返回的是带前缀的 `地址/前缀`）。真实前缀读不出来时，页面显示 `192.168.16.217` 也仍然正确 —— 别为了凑格式去解析 netmask。

- [ ] **Step 4: 跑测试**

```bash
.venv/bin/pytest tests/test_box_ops_link.py -q
```

期望：全绿。

- [ ] **Step 5: 跑全套 + 提交**

```bash
.venv/bin/pytest tests/ -q
git add box_ops.py tests/test_box_ops_link.py
git commit -m "feat: box_ops 的有线口 DHCP/静态切换

与 Wi-Fi 切换共用状态与回滚：先读旧值 → modify → connection up → 等地址
真的落下来 → 失败恢复旧值。仅「施加」那半步不同。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 9: server.py —— 首次窗口与两个引导端点

**Files:**
- Modify: `server.py`（替换 Task 1 里那个占位的 `_first_window_open`；新增 `GET /api/box/onboarding`、`POST /api/box/password`；`/setup` 路由改成「已配置就跳 /settings」）
- Create: `tests/test_server_box_window.py`

**Interfaces:**
- Consumes: Task 4/5 的 `box_ops.identity_report`、`box_ops.scan_networks`、`box_ops.MIN_PASSWORD_LEN`、`box_ops.password_problem`、`box_ops.client_in_local_subnet`；既有 `server._verify_auth`、`_make_auth_token`、`_auth_tokens`、`AUTH_COOKIE`、`WEB_PASSWORD`、`DEFAULT_WEB_PASSWORD`、`first_run.update_env_file`、`_config_file_path`
- Produces:
  - `_password_is_untouched() -> bool`
  - `_first_window_open(request) -> bool`
  - `_write_password(password: str) -> bool`（**全产品唯一**写 `MRRC_AUTO_PASSWORD` 的地方）
  - `GET /api/box/onboarding`、`POST /api/box/password`（响应体：`{"ok", "role", "token", "hostname", "mdns"}` 或 `{"ok": False, "error"}`）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_server_box_window.py`：

```python
"""The only unauthenticated write surface in the product, and its two guards.

The retired wizard's window was "the hotspot I created is up and you are on it".
The box is now reached over a LAN somebody else configured, so the window is
"nobody has ever set a password, and you are on a subnet I am also on" (design
§5.1). Two consequences this file exists to pin:

* **The predicate is the state.** There is no flag to clear, so there is no flag
  to forget to clear. Setting the password closes the window because setting the
  password is what the predicate reads.
* **An unknown network is a closed window.** If `nmcli` cannot say which subnets
  this box is on, the answer must be "not you" — the opposite reading puts an
  unterminated box on a shared LAN with no password at all.

Handlers are called directly (`asyncio.run(handler(FakeRequest(...)))`) rather
than through a TestClient: that is the pattern the retired wizard's tests used,
and it keeps the fake small enough to read.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import unittest
from unittest import mock

import box_ops
import server


class FakeUrl:
    def __init__(self, path, query=""):
        self.path = path
        self.query = query


class FakeClient:
    def __init__(self, host):
        self.host = host


class FakeRequest:
    def __init__(self, path: str = "/api/box/onboarding", method: str = "GET",
                 client: str = "192.168.16.50", cookies: dict = None,
                 body: dict = None, headers: dict = None):
        self.url = FakeUrl(path)
        self.method = method
        self.client = FakeClient(client) if client else None
        self.cookies = cookies or {}
        self.headers = headers or {}
        self.query_params = {}
        self._body = body if body is not None else {}

    async def json(self):
        return self._body


def run(coro):
    return asyncio.run(coro)


class UntouchedTests(unittest.TestCase):
    def test_the_auto_flag_means_untouched(self):
        with mock.patch.dict(os.environ, {"MRRC_AUTO_PASSWORD": "1"}):
            self.assertTrue(server._password_is_untouched())

    def test_a_cleared_flag_with_a_real_password_means_taken(self):
        with mock.patch.dict(os.environ, {"MRRC_AUTO_PASSWORD": ""}):
            with mock.patch.object(server, "WEB_PASSWORD", "a-real-password"):
                self.assertFalse(server._password_is_untouched())

    def test_the_factory_default_means_untouched(self):
        """A box whose first run never happened is still open: the password in
        the source is not a password anybody chose."""
        with mock.patch.dict(os.environ, {"MRRC_AUTO_PASSWORD": ""}):
            with mock.patch.object(server, "WEB_PASSWORD",
                                   server.DEFAULT_WEB_PASSWORD):
                self.assertTrue(server._password_is_untouched())

    def test_an_empty_password_means_untouched(self):
        with mock.patch.dict(os.environ, {"MRRC_AUTO_PASSWORD": ""}):
            with mock.patch.object(server, "WEB_PASSWORD", ""):
                self.assertTrue(server._password_is_untouched())


class WindowPredicateTests(unittest.TestCase):
    def open_box(self):
        return mock.patch.dict(os.environ, {"MRRC_AUTO_PASSWORD": "1"})

    def test_a_client_on_my_subnet_in_an_untouched_box_gets_in(self):
        with self.open_box(), \
                mock.patch.object(box_ops, "local_subnets",
                                  return_value=[__import__("ipaddress")
                                                .ip_network("192.168.16.0/24")]):
            self.assertTrue(server._first_window_open(
                FakeRequest(client="192.168.16.50")))

    def test_a_client_elsewhere_does_not(self):
        with self.open_box(), \
                mock.patch.object(box_ops, "local_subnets",
                                  return_value=[__import__("ipaddress")
                                                .ip_network("192.168.16.0/24")]):
            self.assertFalse(server._first_window_open(
                FakeRequest(client="10.42.0.57")))

    def test_an_unknown_local_network_closes_the_window(self):
        """Review Focus #1. nmcli down, or no address yet: "I cannot tell" must
        read as "not you"."""
        with self.open_box(), \
                mock.patch.object(box_ops, "local_subnets", return_value=[]):
            self.assertFalse(server._first_window_open(
                FakeRequest(client="192.168.16.50")))

    def test_no_peer_at_all_is_refused(self):
        with self.open_box(), \
                mock.patch.object(box_ops, "local_subnets",
                                  return_value=[__import__("ipaddress")
                                                .ip_network("192.168.16.0/24")]):
            self.assertFalse(server._first_window_open(FakeRequest(client=None)))

    def test_a_configured_box_is_closed_even_from_its_own_subnet(self):
        with mock.patch.dict(os.environ, {"MRRC_AUTO_PASSWORD": ""}), \
                mock.patch.object(server, "WEB_PASSWORD", "chosen-by-a-human"), \
                mock.patch.object(box_ops, "local_subnets",
                                  return_value=[__import__("ipaddress")
                                                .ip_network("192.168.16.0/24")]):
            self.assertFalse(server._first_window_open(
                FakeRequest(client="192.168.16.50")))


class OnboardingEndpointTests(unittest.TestCase):
    def call(self, request=None):
        return run(server.api_box_onboarding(request or FakeRequest()))

    def test_it_refuses_when_the_window_is_closed(self):
        with mock.patch.object(server, "_first_window_open",
                               return_value=False):
            response = self.call()
        self.assertEqual(response.status_code, 401)

    def test_it_answers_the_four_things_the_page_needs(self):
        with mock.patch.object(server, "_first_window_open", return_value=True), \
                mock.patch.object(server, "_password_is_untouched",
                                  return_value=True), \
                mock.patch.object(box_ops, "scan_networks",
                                  return_value=[{"ssid": "home", "signal": 61,
                                                 "security": "wpa2",
                                                 "protected": True}]), \
                mock.patch.object(box_ops, "identity_report",
                                  return_value={"hostname": "MRRC-Modern",
                                                "mdns": "MRRC-Modern.local",
                                                "addresses": ["192.168.16.217"],
                                                "suggested_name": "MRRC-Modern"}):
            payload = __import__("json").loads(self.call().body)
        self.assertTrue(payload["needs_password"])
        self.assertEqual(payload["hostname"], "MRRC-Modern")
        self.assertEqual(payload["mdns"], "MRRC-Modern.local")
        self.assertEqual(payload["networks"][0]["ssid"], "home")
        self.assertEqual(payload["min_password_len"], box_ops.MIN_PASSWORD_LEN)


class PasswordEndpointTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(server, "first_run")
        self.first_run = patcher.start()
        self.addCleanup(patcher.stop)
        self.path = mock.patch.object(server, "_config_file_path",
                                      return_value=__import__("pathlib").Path("/tmp/x"))
        self.path.start()

    def post(self, body, request=None):
        return run(server.api_box_password(
            request or FakeRequest(method="POST", body=body)))

    def open_window(self):
        return mock.patch.object(server, "_first_window_open", return_value=True)

    def test_it_writes_the_two_keys_and_clears_the_auto_flag(self):
        with self.open_window():
            self.post({"password": "a-good-one", "confirm": "a-good-one"})
        updates = self.first_run.update_env_file.call_args[0][1]
        self.assertEqual(updates, {"MRRC_WEB_PASSWORD": "a-good-one",
                                   "MRRC_AUTO_PASSWORD": ""})

    def test_it_makes_the_value_live_without_a_restart(self):
        """Restarting would drop the operator in the middle of onboarding, and
        the next step takes their network away — the retired wizard's comment,
        still true."""
        with self.open_window():
            self.post({"password": "a-good-one", "confirm": "a-good-one"})
        self.assertEqual(server.WEB_PASSWORD, "a-good-one")
        self.assertEqual(os.environ.get("MRRC_AUTO_PASSWORD"), "")

    def test_it_signs_a_session_so_the_operator_is_not_asked_twice(self):
        with self.open_window():
            payload = __import__("json").loads(
                self.post({"password": "a-good-one",
                           "confirm": "a-good-one"}).body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["role"], "admin")
        self.assertIn(payload["token"], server._auth_tokens)

    def test_a_short_password_is_refused_before_anything_is_written(self):
        with self.open_window():
            response = self.post({"password": "short", "confirm": "short"})
        self.assertEqual(response.status_code, 400)
        self.first_run.update_env_file.assert_not_called()

    def test_a_mismatch_is_refused(self):
        with self.open_window():
            response = self.post({"password": "a-good-one",
                                  "confirm": "a-good-two"})
        self.assertEqual(response.status_code, 400)
        self.first_run.update_env_file.assert_not_called()

    def test_a_closed_window_without_a_session_is_refused(self):
        with mock.patch.object(server, "_first_window_open",
                               return_value=False), \
                mock.patch.object(server, "_verify_auth", return_value=False):
            response = self.post({"password": "a-good-one",
                                  "confirm": "a-good-one"})
        self.assertEqual(response.status_code, 401)
        self.first_run.update_env_file.assert_not_called()

    def test_a_logged_in_session_may_change_the_password_too(self):
        """The settings page has to be able to, and it does not need the
        window: it has a session."""
        with mock.patch.object(server, "_first_window_open",
                               return_value=False), \
                mock.patch.object(server, "_verify_auth", return_value=True):
            response = self.post({"password": "a-good-one",
                                  "confirm": "a-good-one"})
        self.assertEqual(response.status_code, 200)

    def test_a_failed_write_is_an_error_not_a_quiet_success(self):
        self.first_run.update_env_file.side_effect = OSError("read-only fs")
        before = server.WEB_PASSWORD
        with self.open_window():
            response = self.post({"password": "a-good-one",
                                  "confirm": "a-good-one"})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(server.WEB_PASSWORD, before)


class SingleWriterTests(unittest.TestCase):
    def test_there_is_exactly_one_place_that_clears_the_auto_flag(self):
        """spec R-7. The auto-password flag is what closes the onboarding
        window, so a second writer is a second chance to close it early — or a
        second window that stays open. One writer, and the test in this file
        proves the reader agrees with it."""
        source = inspect.getsource(server)
        self.assertEqual(
            source.count('os.environ["MRRC_AUTO_PASSWORD"]'), 1,
            "the auto-password flag is written in more than one place")


class SetupRouteTests(unittest.TestCase):
    def test_a_configured_box_goes_to_the_settings_page(self):
        """Review Focus #4: a bookmarked /setup on a finished box must land on
        /settings, and must not render the onboarding page again."""
        with mock.patch.object(server, "_first_window_open",
                               return_value=False), \
                mock.patch.object(server, "_verify_auth", return_value=True), \
                mock.patch.object(server, "_password_is_untouched",
                                  return_value=False):
            response = run(server.setup_page(FakeRequest(path="/setup")))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/settings")

    def test_an_open_window_renders_the_page(self):
        with mock.patch.object(server, "_first_window_open", return_value=True):
            response = run(server.setup_page(FakeRequest(path="/setup")))
        self.assertEqual(getattr(response, "status_code", 200), 200)

    def test_a_closed_window_without_a_session_goes_to_login(self):
        with mock.patch.object(server, "_first_window_open",
                               return_value=False), \
                mock.patch.object(server, "_verify_auth", return_value=False):
            response = run(server.setup_page(FakeRequest(path="/setup")))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/login?next=/setup")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑它，确认它红**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
.venv/bin/pytest tests/test_server_box_window.py -q
```

期望：`AttributeError: module 'server' has no attribute '_password_is_untouched'`。

- [ ] **Step 3: 用真实现替换占位**

把 Task 1 Step 4e 留下的占位：

```python
def _first_window_open(request: Request) -> bool:
    """Placeholder — replaced by the real predicate in Task 9."""
    return False
```

替换为：

```python
def _password_is_untouched() -> bool:
    """Whether no human has ever chosen this box's web password.

    The auto-password flag is the honest signal: first_run sets it when it
    generates a password nobody was told, and every user-driven write clears it.
    The two fallbacks catch a box where first run never happened at all.
    """
    if os.environ.get("MRRC_AUTO_PASSWORD") == "1":
        return True
    if not WEB_PASSWORD:
        return True
    return hmac.compare_digest(str(WEB_PASSWORD).encode("utf-8"),
                               str(DEFAULT_WEB_PASSWORD).encode("utf-8"))


def _first_window_open(request: Request) -> bool:
    """Whether this request may set the box's first password with no session.

    Two facts have to agree, both read live (design §5.1):

    1. Nobody has set a password yet. This is why there is no flag to clear and
       no timer to expire: the predicate *is* the state, so the window cannot be
       left open by forgetting to close it.
    2. The client is on a subnet this box itself holds an address on. An
       unreadable set of local subnets closes the window rather than opening it —
       "I cannot tell" has to read as "not you" (spec §5.4 for the residual
       weakness this still leaves on a shared LAN).
    """
    if not _password_is_untouched():
        return False
    host = request.client.host if request.client else ""
    return box_ops.client_in_local_subnet(host)


def _write_password(password: str) -> bool:
    """Persist the web password, and make it live without a restart.

    The one place in server.py that clears MRRC_AUTO_PASSWORD, which is what
    closes the onboarding window — so this is the function that closes it. The
    restart the wizard deliberately did not do is still deliberately not done:
    the caller is mid-onboarding and the next step takes their network away.
    """
    global WEB_PASSWORD
    updates = {"MRRC_WEB_PASSWORD": password, "MRRC_AUTO_PASSWORD": ""}
    try:
        first_run.update_env_file(_config_file_path(), updates)
    except OSError as exc:
        logger.error("could not write the web password: %s", exc)
        return False
    WEB_PASSWORD = password
    os.environ["MRRC_WEB_PASSWORD"] = password
    os.environ["MRRC_AUTO_PASSWORD"] = ""
    return True
```

并确保 `import box_ops` 在 `server.py` 顶部（Task 1 删了 `import net_wifi`，那里正好放它）。确认 `hmac`、`os`、`logger`、`first_run` 都已在 `server.py` 里。

- [ ] **Step 4: 加两个端点，改 `/setup`**

把 Step 3 加的三个函数**紧接在** `/setup` 路由之前，并把 `/setup` 处理器换成：

```python
@app.get("/setup", include_in_schema=False)
async def setup_page(request: Request):
    """The onboarding page.

    Passwordless only while the box has never had a user-set password; from
    anywhere else it is an ordinary authenticated route. A box that is already
    configured sends the operator to the settings page rather than showing them
    a page they cannot use. `no-store` because the window this page renders
    closes the moment a password lands — a cached copy would tell the operator
    the box is still open when it is not.
    """
    if not _first_window_open(request):
        if not _verify_auth(request):
            return RedirectResponse("/login?next=/setup", status_code=302)
        if not _password_is_untouched():
            return RedirectResponse("/settings", status_code=302)
    page = STATIC_DIR / "setup.html"
    if not page.exists():
        return HTMLResponse("<h1>404 Not Found</h1>", status_code=404)
    response = FileResponse(page, media_type="text/html")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/box/onboarding", include_in_schema=False)
async def api_box_onboarding(request: Request):
    """The four things the onboarding page needs, and nothing else.

    The page renders Wi-Fi and a password box; it does not get the network
    report, the system state or the safety fields — those are a logged-in
    surface. Keeping this payload small is the point (design §5.3).
    """
    if not _first_window_open(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    identity = await asyncio.to_thread(box_ops.identity_report)
    return JSONResponse({
        "needs_password": _password_is_untouched(),
        "hostname": identity["hostname"],
        "mdns": identity["mdns"],
        "networks": await asyncio.to_thread(box_ops.scan_networks),
        "min_password_len": box_ops.MIN_PASSWORD_LEN,
    })


@app.post("/api/box/password", include_in_schema=False)
async def api_box_password(request: Request):
    """Set the web password. The window's only write, and it is one key wide.

    Two ways in: a session (the settings page) or an open first window (the
    onboarding page). Success signs a session and hands it back, so the operator
    is not asked to log in with the password they just typed.
    """
    if not _verify_auth(request) and not _first_window_open(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    password = (body or {}).get("password", "") or ""
    confirm = (body or {}).get("confirm", password) or ""
    problem = box_ops.password_problem(password, confirm)
    if problem:
        return JSONResponse({"ok": False, "error": problem}, status_code=400)
    if not _write_password(password):
        return JSONResponse({"ok": False, "error": "写入配置失败"},
                            status_code=500)

    identity = await asyncio.to_thread(box_ops.identity_report)
    token = _make_auth_token()
    _auth_tokens.add(token)
    response = JSONResponse({"ok": True, "role": "admin", "token": token,
                             "hostname": identity["hostname"],
                             "mdns": identity["mdns"]})
    response.set_cookie(
        AUTH_COOKIE, token,
        max_age=30 * 24 * 3600,  # 30 days, same as /api/auth/login
        httponly=False,          # JS needs to read it for WebSocket
        samesite="lax",
    )
    logger.warning("box: the web password was set — the onboarding window is "
                   "now closed")
    return response
```

> 放在 `/setup` 之前还是之后由 `serve_static` 的位置决定：两者都必须在文件末尾那个 `app.add_api_route("/{path:path}", serve_static, ...)` **之前**。`tests/test_cloud_endpoints.py` 会抓住放错位置的路由。

- [ ] **Step 5: 跑测试**

```bash
.venv/bin/pytest tests/test_server_box_window.py -q
.venv/bin/pytest tests/ -q
```

期望：全绿。

- [ ] **Step 6: 提交**

```bash
git add server.py tests/test_server_box_window.py
git commit -m "feat: 首次窗口与 /api/box/onboarding + POST /api/box/password

窗口判据 = 从未设过口令 且 客户端在盒子自己的某个子网内；子网读不出来即关。
写入仍是唯一一处（_write_password），不重启，成功后直接签发会话。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 10: server.py —— 聚合只读 `GET /api/box/status`

**Files:**
- Modify: `server.py`
- Create: `tests/test_server_box_status.py`

**Interfaces:**
- Consumes: Task 4 的 `box_ops.network_report` / `system_report` / `identity_report` / `safety_report` / `helper_available`；Task 9 的 `_first_window_open`
- Produces: `GET /api/box/status` → `{"network", "system", "identity", "safety", "helper_available", "first_window_open"}`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_server_box_status.py`：

```python
"""One read that fills the whole settings page.

The retired `/api/manage` reported two things behind a login; this reports those
plus the network and system state, and it is the settings page's only source.
It must be a *logged-in* surface — the safety fields alone (the transmit gate
among them) are not something an unauthenticated caller gets to enumerate.
"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

import box_ops
import server
from tests.test_server_box_window import FakeRequest


def run(coro):
    return asyncio.run(coro)


class StatusEndpointTests(unittest.TestCase):
    def status(self, **patches):
        defaults = {
            "network_report": {"addresses": [], "gateways": [], "dns": [],
                              "devices": [], "connections": [],
                              "wifi": {"ssid": "", "signal": 0},
                              "nm_running": False},
            "system_report": {"timezone": "Asia/Shanghai", "uptime_s": 12.5,
                              "services": []},
            "identity_report": {"hostname": "MRRC-Modern",
                                "mdns": "MRRC-Modern.local",
                                "addresses": ["192.168.16.217"],
                                "suggested_name": "MRRC-Modern"},
            "safety_report": [{"key": "MRRC_ALLOW_UNVERIFIED_TX", "value": "0",
                               "read_only": True, "note": "发射门禁"}],
            "helper_available": True,
        }
        defaults.update(patches)
        # return_value=, not a replacement object: the code under test *calls*
        # these. Replacing the module attribute wholesale would work by accident
        # here and break the moment one of them is called with an argument.
        stack = [mock.patch.object(box_ops, name, return_value=value)
                 for name, value in defaults.items()]
        for patcher in stack:
            patcher.start()
            self.addCleanup(patcher.stop)
        return run(server.api_box_status(FakeRequest(path="/api/box/status")))

    def test_a_request_without_a_session_is_refused(self):
        with mock.patch.object(server, "_verify_auth", return_value=False):
            response = run(server.api_box_status(FakeRequest()))
        self.assertEqual(response.status_code, 401)

    def test_it_hands_back_every_panel_in_one_read(self):
        payload = json.loads(self.status().body)
        self.assertEqual(set(payload),
                         {"network", "system", "identity", "safety",
                          "helper_available", "first_window_open"})

    def test_the_safety_fields_come_with_their_values_and_notes(self):
        payload = json.loads(self.status().body)
        self.assertEqual(payload["safety"][0]["key"],
                         "MRRC_ALLOW_UNVERIFIED_TX")
        self.assertTrue(payload["safety"][0]["read_only"])

    def test_a_missing_helper_is_reported_not_raised(self):
        """Review Focus #3: the page greys the system panel out and keeps the
        other five working."""
        payload = json.loads(self.status(helper_available=False).body)
        self.assertFalse(payload["helper_available"])
        self.assertEqual(payload["identity"]["hostname"], "MRRC-Modern")

    def test_it_reports_whether_the_window_is_still_open(self):
        """The settings page shows that banner; it is the operator's only hint
        that the box is still open to its own subnet."""
        with mock.patch.object(server, "_first_window_open", return_value=True):
            payload = json.loads(self.status().body)
        self.assertTrue(payload["first_window_open"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑它，确认它红**

```bash
.venv/bin/pytest tests/test_server_box_status.py -q
```

期望：`AttributeError: module 'server' has no attribute 'api_box_status'`。

- [ ] **Step 3: 写实现**

在 `server.py` 的 `POST /api/box/password` 之后加：

```python
@app.get("/api/box/status", include_in_schema=False)
async def api_box_status(request: Request):
    """Everything the settings page shows, in one read.

    Four reads behind one request because the page is one screen: six panels
    each fetching separately is six chances to be half-loaded, and the network
    report and the identity report are both `nmcli device show` anyway.

    `asyncio.to_thread` because every one of these shells out, and a blocking
    subprocess call inside an async handler stops the whole server — including
    the WebSocket feeds a user may be mid-QSO on.
    """
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    network, system, identity = await asyncio.gather(
        asyncio.to_thread(box_ops.network_report),
        asyncio.to_thread(box_ops.system_report),
        asyncio.to_thread(box_ops.identity_report),
    )
    return JSONResponse({
        "network": network,
        "system": system,
        "identity": identity,
        "safety": box_ops.safety_report(),
        "helper_available": box_ops.helper_available(),
        "first_window_open": _first_window_open(request),
    })
```

> `asyncio.gather` + `to_thread` 让三个读数并行；它们各自是一次 `nmcli`，串行会把这一屏拖到秒级。

- [ ] **Step 4: 跑测试 + 提交**

```bash
.venv/bin/pytest tests/test_server_box_status.py -q
.venv/bin/pytest tests/ -q
git add server.py tests/test_server_box_status.py
git commit -m "feat: GET /api/box/status —— 设置页的聚合只读

网络体检 + 系统 + 身份 + 安全字段 + helper 可达性 + 窗口是否还开着。全部
经 asyncio.to_thread，三个读数并行。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 11: server.py —— 五个动作端点

**Files:**
- Modify: `server.py`
- Create: `tests/test_server_box_actions.py`

**Interfaces:**
- Consumes: Task 4/5/7/8 的 `box_ops.scan_networks`、`switch_state`、`perform_wifi_switch`、`perform_link_change`、`set_hostname`、`reload_avahi`、`set_timezone`、`reboot`、`PHASE_SWITCHING`
- Produces：
  | 方法 | 路径 | 成功 | 拒绝 |
  |---|---|---|---|
  | GET | `/api/box/wifi` | `{"networks": [...]}` | 401 |
  | POST | `/api/box/wifi` | `{"ok": true, "switching": true, "ssid": ...}` | 400 空 SSID / 409 已在切换 |
  | GET | `/api/box/wifi/status` | `{"phase", "ssid", "reason", "address", "since"}` | 401 |
  | POST | `/api/box/hostname` | `{"ok": true}` | 400 非法名 / helper 失败 |
  | POST | `/api/box/timezone` | `{"ok": true}` | 400 |
  | POST | `/api/box/link` | `{"ok": true, "changing": true}` | 400 / 409 |
  | POST | `/api/box/system/reboot` | `{"ok": true}` | 400 未确认 |

- [ ] **Step 1: 写失败测试**

创建 `tests/test_server_box_actions.py`：

```python
"""The settings page's buttons, at the handler boundary.

Two things every test here is really about:

* **The switch is ordered, not awaited.** `POST /api/box/wifi` returns before the
  network moves, because the request may be travelling over that very network
  (design §5.4 of the old design, §6.2 of the new one). A handler that awaited
  the switch would hang the operator's browser on a socket that is about to
  disappear.
* **A destructive action needs a second word.** Restarting the box drops
  everybody on it, so it takes an explicit `confirm` value rather than a button
  that fires on one click.
"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

import box_ops
import server
from tests.test_server_box_window import FakeRequest


def run(coro):
    return asyncio.run(coro)


def authed():
    return mock.patch.object(server, "_verify_auth", return_value=True)


class ScanTests(unittest.TestCase):
    def test_a_scan_is_the_same_list_the_onboarding_page_gets(self):
        with authed(), mock.patch.object(
                box_ops, "scan_networks",
                return_value=[{"ssid": "home", "signal": 61,
                               "security": "wpa2", "protected": True}]):
            payload = json.loads(run(server.api_box_wifi_scan(FakeRequest())).body)
        self.assertEqual(payload["networks"][0]["ssid"], "home")

    def test_it_needs_a_session(self):
        with mock.patch.object(server, "_verify_auth", return_value=False):
            response = run(server.api_box_wifi_scan(FakeRequest()))
        self.assertEqual(response.status_code, 401)


class SwitchRequestTests(unittest.TestCase):
    def setUp(self):
        box_ops._state.reset()

    def post(self, body):
        return run(server.api_box_wifi_connect(
            FakeRequest(path="/api/box/wifi", method="POST", body=body)))

    def test_it_hands_the_work_to_a_thread_and_returns_immediately(self):
        started = []

        def slow(ssid, password=""):
            started.append(ssid)
            return {"ok": True}

        with authed(), mock.patch.object(box_ops, "perform_wifi_switch",
                                         side_effect=slow):
            response = self.post({"ssid": "NewNet", "password": "pw"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(json.loads(response.body)["switching"])

    def test_an_empty_ssid_is_refused(self):
        with authed():
            response = self.post({"ssid": "  "})
        self.assertEqual(response.status_code, 400)

    def test_a_second_switch_is_refused_while_one_is_running(self):
        box_ops._state.phase = box_ops.PHASE_SWITCHING
        with authed():
            response = self.post({"ssid": "NewNet"})
        self.assertEqual(response.status_code, 409)

    def test_the_password_never_appears_in_a_log_line(self):
        """A PSK in the journal is a PSK in every support bundle."""
        with authed(), mock.patch.object(box_ops, "perform_wifi_switch"), \
                mock.patch.object(server, "logger") as log:
            self.post({"ssid": "NewNet", "password": "hunter2secret"})
        for call in log.method_calls:
            self.assertNotIn("hunter2secret", str(call))

    def test_it_needs_a_session(self):
        with mock.patch.object(server, "_verify_auth", return_value=False):
            response = self.post({"ssid": "NewNet"})
        self.assertEqual(response.status_code, 401)


class SwitchStatusTests(unittest.TestCase):
    def test_it_reports_the_state_the_page_reconnects_to(self):
        box_ops._state.reset()
        box_ops._state.phase = box_ops.PHASE_FAILED
        box_ops._state.reason = "没拿到地址"
        with authed():
            payload = json.loads(run(server.api_box_wifi_status(FakeRequest())).body)
        self.assertEqual(payload["phase"], box_ops.PHASE_FAILED)
        self.assertEqual(payload["reason"], "没拿到地址")
        self.assertNotIn("password", payload)

    def test_it_needs_a_session(self):
        with mock.patch.object(server, "_verify_auth", return_value=False):
            response = run(server.api_box_wifi_status(FakeRequest()))
        self.assertEqual(response.status_code, 401)


class HostnameTests(unittest.TestCase):
    def post(self, body):
        return run(server.api_box_hostname(
            FakeRequest(path="/api/box/hostname", method="POST", body=body)))

    def test_a_good_name_is_applied_and_avahi_is_nudged(self):
        """avahi-daemon reads the hostname at start and on reload; it does not
        follow hostnamectl on its own, so without the nudge the new name is
        advertised whenever avahi next feels like it."""
        with authed(), \
                mock.patch.object(box_ops, "set_hostname",
                                  return_value={"ok": True}) as rename, \
                mock.patch.object(box_ops, "reload_avahi",
                                  return_value={"ok": True}) as reload_:
            response = self.post({"hostname": "MRRC-Modern"})
        self.assertEqual(response.status_code, 200)
        rename.assert_called_once()
        reload_.assert_called_once()

    def test_a_bad_name_is_refused_with_the_validation_message(self):
        with authed():
            response = self.post({"hostname": "-nope"})
        self.assertEqual(response.status_code, 400)
        self.assertTrue(json.loads(response.body)["error"])

    def test_a_failed_rename_does_not_nudge_avahi(self):
        with authed(), \
                mock.patch.object(box_ops, "set_hostname",
                                  return_value={"ok": False,
                                                "error": "helper unavailable"}), \
                mock.patch.object(box_ops, "reload_avahi") as reload_:
            response = self.post({"hostname": "MRRC-Modern"})
        self.assertEqual(response.status_code, 400)
        reload_.assert_not_called()


class TimezoneTests(unittest.TestCase):
    def test_a_real_zone_goes_through(self):
        with authed(), mock.patch.object(box_ops, "set_timezone",
                                         return_value={"ok": True}):
            response = run(server.api_box_timezone(
                FakeRequest(method="POST", body={"timezone": "Asia/Shanghai"})))
        self.assertEqual(response.status_code, 200)

    def test_a_made_up_zone_is_refused_before_the_helper(self):
        with authed(), mock.patch.object(box_ops, "set_timezone") as call:
            response = run(server.api_box_timezone(
                FakeRequest(method="POST", body={"timezone": "Not/AZone"})))
        self.assertEqual(response.status_code, 400)
        call.assert_not_called()


class LinkTests(unittest.TestCase):
    def setUp(self):
        box_ops._state.reset()

    def post(self, body):
        return run(server.api_box_link(
            FakeRequest(path="/api/box/link", method="POST", body=body)))

    def test_a_link_change_is_ordered_like_the_switch(self):
        with authed(), mock.patch.object(box_ops, "perform_link_change",
                                         return_value={"ok": True}):
            response = self.post({"profile": "有线连接 2", "method": "manual",
                                  "address": "192.168.16.217/24"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(json.loads(response.body)["changing"])

    def test_an_invalid_request_is_refused(self):
        with authed():
            response = self.post({"profile": "x", "method": "manual",
                                  "address": "nonsense"})
        self.assertEqual(response.status_code, 400)

    def test_a_missing_profile_is_refused(self):
        with authed():
            response = self.post({"method": "auto"})
        self.assertEqual(response.status_code, 400)


class RebootTests(unittest.TestCase):
    def post(self, body):
        return run(server.api_box_system_reboot(
            FakeRequest(path="/api/box/system/reboot", method="POST",
                        body=body)))

    def test_it_takes_an_explicit_confirmation(self):
        with authed(), mock.patch.object(box_ops, "reboot") as reboot:
            response = self.post({})
        self.assertEqual(response.status_code, 400)
        reboot.assert_not_called()

    def test_the_confirmation_is_the_word_reboot(self):
        with authed(), mock.patch.object(box_ops, "reboot",
                                         return_value={"ok": True}) as reboot:
            response = self.post({"confirm": "reboot"})
        self.assertEqual(response.status_code, 200)
        reboot.assert_called_once()

    def test_a_helper_that_cannot_do_it_is_reported(self):
        with authed(), mock.patch.object(
                box_ops, "reboot",
                return_value={"ok": False, "error": "helper unavailable"}):
            response = self.post({"confirm": "reboot"})
        self.assertEqual(response.status_code, 400)
        self.assertTrue(json.loads(response.body)["error"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑它，确认它红**

```bash
.venv/bin/pytest tests/test_server_box_actions.py -q
```

期望：`AttributeError: module 'server' has no attribute 'api_box_wifi_scan'`。

- [ ] **Step 3: 写实现**

在 `server.py` 的 `api_box_status` 之后加：

```python
async def _box_body(request: Request) -> dict:
    """The JSON body, or an empty dict. A malformed body is a 400 from the
    endpoint's own validation, not a 500 from the parser."""
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _box_busy() -> bool:
    """Whether a network change is already in flight.

    Both the Wi-Fi switch and the wired change move the box's own connectivity,
    so they share one slot: two at once would leave the rollback of one fighting
    the apply of the other.
    """
    return box_ops.switch_state()["phase"] == box_ops.PHASE_SWITCHING


@app.get("/api/box/wifi", include_in_schema=False)
async def api_box_wifi_scan(request: Request):
    """Visible networks. Unprivileged: `mrrc` may read NetworkManager."""
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse(
        {"networks": await asyncio.to_thread(box_ops.scan_networks)})


@app.post("/api/box/wifi", include_in_schema=False)
async def api_box_wifi_connect(request: Request):
    """Order a Wi-Fi switch. Returns before the network moves, on purpose.

    The request may be arriving over the very interface this takes down, so
    waiting for the result would wait on a socket that is already gone. The page
    polls `/api/box/wifi/status` after it reconnects (design §6.2).
    """
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = await _box_body(request)
    ssid = (body.get("ssid") or "").strip()
    password = body.get("password") or ""
    if not ssid:
        return JSONResponse({"ok": False, "error": "请选择一个 Wi-Fi"},
                            status_code=400)
    if _box_busy():
        return JSONResponse({"ok": False, "error": "已经有一次切换在进行中"},
                            status_code=409)
    asyncio.create_task(asyncio.to_thread(
        box_ops.perform_wifi_switch, ssid, password))
    logger.warning("box: switching to WiFi %r — if the operator reached the box "
                   "over WiFi, their own session is about to drop", ssid)
    return JSONResponse({"ok": True, "switching": True, "ssid": ssid})


@app.get("/api/box/wifi/status", include_in_schema=False)
async def api_box_wifi_status(request: Request):
    """How the switch went. Read after reconnecting, possibly from the new
    network, possibly after a rollback."""
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse(box_ops.switch_state())


@app.post("/api/box/hostname", include_in_schema=False)
async def api_box_hostname(request: Request):
    """Rename the box, then tell avahi before it notices on its own.

    avahi-daemon reads the hostname when it starts and when it is reloaded; it
    does not follow `hostnamectl`. Without the nudge the new name is advertised
    at whatever moment avahi next decides to, which is not a thing to make an
    operator wait for.
    """
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = await _box_body(request)
    result = await asyncio.to_thread(box_ops.set_hostname,
                                     (body.get("hostname") or "").strip())
    if result.get("ok"):
        await asyncio.to_thread(box_ops.reload_avahi)
    return JSONResponse(result,
                        status_code=200 if result.get("ok") else 400)


@app.post("/api/box/timezone", include_in_schema=False)
async def api_box_timezone(request: Request):
    """Set the box's timezone, through the helper: `timedatectl` is root's."""
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = await _box_body(request)
    result = await asyncio.to_thread(box_ops.set_timezone,
                                     (body.get("timezone") or "").strip())
    return JSONResponse(result,
                        status_code=200 if result.get("ok") else 400)


@app.post("/api/box/link", include_in_schema=False)
async def api_box_link(request: Request):
    """Order a change to a wired profile: DHCP or a static address.

    Ordered, not awaited, for the same reason as the Wi-Fi switch: the operator
    may be reaching the box over the profile being edited (design §6.4).
    """
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = await _box_body(request)
    profile = (body.get("profile") or "").strip()
    method = (body.get("method") or "").strip()
    address = (body.get("address") or "").strip()
    gateway = (body.get("gateway") or "").strip()
    if not profile:
        return JSONResponse({"ok": False, "error": "没有指定要改的连接"},
                            status_code=400)
    problem = box_ops.link_problem(method, address)
    if problem:
        return JSONResponse({"ok": False, "error": problem}, status_code=400)
    if _box_busy():
        return JSONResponse({"ok": False, "error": "已经有一次网络改动在进行中"},
                            status_code=409)
    asyncio.create_task(asyncio.to_thread(
        box_ops.perform_link_change, profile, method, address,
        gateway=gateway))
    logger.warning("box: changing %r to %s — if the operator reached the box "
                   "over this link, their session is about to drop",
                   profile, method)
    return JSONResponse({"ok": True, "changing": True, "profile": profile})


@app.post("/api/box/system/reboot", include_in_schema=False)
async def api_box_system_reboot(request: Request):
    """Restart the box, not the service.

    `POST /api/restart` restarts the server and everything comes back; this one
    takes the whole machine down for about a minute and drops every session on
    it. Two buttons on one page with that much difference between them need
    different friction, so this one takes an explicit confirmation value.
    """
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = await _box_body(request)
    if body.get("confirm") != "reboot":
        return JSONResponse({"ok": False, "error": "重启盒子需要显式确认"},
                            status_code=400)
    result = await asyncio.to_thread(box_ops.reboot)
    logger.warning("box: reboot requested")
    return JSONResponse(result,
                        status_code=200 if result.get("ok") else 400)
```

- [ ] **Step 4: 跑测试 + 提交**

```bash
.venv/bin/pytest tests/test_server_box_actions.py -q
.venv/bin/pytest tests/ -q
git add server.py tests/test_server_box_actions.py
git commit -m "feat: /api/box/* 的五个动作端点

Wi-Fi 切换与有线口改动都「下单不等结果」（请求可能正走在那条要断的路上），
状态由 /api/box/wifi/status 在重连后读；改名字之后 nudge avahi；重启盒子要
显式 confirm，与服务重启（/api/restart）区分。日志里只有 SSID。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 12: 两个页面与共用的 API 客户端

**Files:**
- Create: `static/box_api.js`
- Rewrite: `static/setup.html`（保留 `<style>`，换掉 `<body>`）
- Create: `static/settings.html`
- Modify: `server.py`（加 `GET /settings`）
- Create: `tests/test_server_box_pages.py`

**Interfaces:**
- Consumes: Task 9/10/11 的所有 `/api/box/*`；既有 `GET/POST /api/setup`、`GET /api/devices`、`POST /api/restart`、`POST /api/auth/logout`
- Produces:
  - `window.Box` = `{AUTH_COOKIE, api, on, setText, show, message, disabled, signalText}`
  - `GET /settings` → `static/settings.html`（认证由中间件负责）
  - `static/setup.html` 的元素 id：`#st-name`、`#st-mdns`、`#st-addr`、`#st-pass`、`#pw1`、`#pw2`、`#pw-btn`、`#pw-msg`、`#card-wifi`、`#scan-btn`、`#net-list`、`#pick`、`#pick-name`、`#psk`、`#join-btn`、`#wifi-msg`、`#banner`、`#errbox`、`#overlay`、`#ov-title`、`#ov-text`、`#ov-mdns`

- [ ] **Step 1: 写 `static/box_api.js`**

```javascript
/* Shared client for /setup and /settings.
 *
 * One backend, two pages: this file is where the two agree about how a request
 * is made, how an error is rendered, and — the part that is expensive to get
 * wrong twice — how a page behaves when the box's own network is about to move
 * under it.
 *
 * A plain script, no modules and no build step, exactly like listen.js: the
 * pages are served straight off disk by the same FileResponse route.
 */
(function (global) {
  'use strict';

  // Deliberately not httpOnly — the WebSocket client reads it, so the page can
  // read it too and send it explicitly on fetch().
  var AUTH_COOKIE = 'mrrc_auth';

  function readCookie(name) {
    var match = document.cookie.match(
      new RegExp('(?:^|;\\s*)' + name + '=([^;]*)'));
    return match ? decodeURIComponent(match[1]) : '';
  }

  function authHeaders(extra) {
    var out = extra || {};
    var token = readCookie(AUTH_COOKIE);
    if (token) out['Authorization'] = 'Bearer ' + token;
    return out;
  }

  /* Resolves with the parsed body on 2xx; rejects with an Error whose `message`
   * is the server's own wording. A page never invents an error message and
   * never parses one — `error.status` is there for the two cases that need
   * different behaviour (401 → go to /login, 409 → something is already
   * running).
   *
   * `cache: 'no-store'` on every call: these pages report live network state,
   * and a cached /api/box/status would show an operator the network the box was
   * on before they changed it. */
  function api(path, options) {
    var opts = options || {};
    var init = {
      method: opts.method || 'GET',
      cache: 'no-store',
      headers: authHeaders(opts.body ? { 'Content-Type': 'application/json' } : null)
    };
    if (opts.body !== undefined) init.body = JSON.stringify(opts.body);
    return fetch(path, init).then(function (response) {
      return response.json().catch(function () { return {}; })
        .then(function (payload) {
          if (!response.ok) {
            var error = new Error(payload.error || ('HTTP ' + response.status));
            error.status = response.status;
            error.payload = payload;
            throw error;
          }
          return payload;
        });
    });
  }

  function on(selector, event, handler) {
    var node = document.querySelector(selector);
    if (node) node.addEventListener(event, handler);
    return node;
  }

  function setText(selector, text) {
    var node = document.querySelector(selector);
    if (node) node.textContent = text;
  }

  function show(selector, visible) {
    var node = document.querySelector(selector);
    if (node) node.classList.toggle('hidden', !visible);
  }

  function message(selector, text, kind) {
    var node = document.querySelector(selector);
    if (!node) return;
    node.textContent = text || '';
    node.className = 'msg' + (kind ? ' ' + kind : '');
  }

  function disabled(selector, yes) {
    var node = document.querySelector(selector);
    if (node) node.disabled = !!yes;
  }

  /* Five bars out of five, as text: the same scale the retired wizard used, and
   * it needs no images. */
  function signalText(signal) {
    var filled = Math.max(0, Math.min(5, Math.round((signal || 0) / 20)));
    return '▮▮▮▮▮'.slice(0, filled) + '▯▯▯▯▯'.slice(0, 5 - filled);
  }

  /* Which route this page is on decides what a dropped connection means: on
   * /setup the operator is about to lose the box on purpose, and the words that
   * say so have to be on screen *before* they press the button, not in the
   * error box after. */
  function secondsLabel(seconds) {
    var n = Math.max(0, Math.round(seconds || 0));
    return Math.floor(n / 60) + ' 分 ' + ('0' + (n % 60)).slice(-2) + ' 秒';
  }

  global.Box = {
    AUTH_COOKIE: AUTH_COOKIE,
    api: api,
    on: on,
    setText: setText,
    show: show,
    message: message,
    disabled: disabled,
    signalText: signalText,
    secondsLabel: secondsLabel
  };
})(window);
```

- [ ] **Step 2: 重写 `static/setup.html` 的 body**

`<head>` 到 `</style>` **原样不动**（那一块是这套页面的调色板与卡片样式，`settings.html` 也要用）。把 `<body>` 整个换掉：

```html
<body>
<div class="wrap">

  <header>
    <h1>MRRC Modern</h1>
    <p class="sub">盒子初始化</p>
  </header>

  <div id="banner" class="banner hidden"></div>
  <div id="errbox" class="errbox hidden"></div>

  <div class="card" id="card-status">
    <div class="kv"><span>盒子名字</span><b id="st-name">读取中…</b></div>
    <div class="kv"><span>mDNS 名字</span><b id="st-mdns">—</b></div>
    <div class="kv"><span>本机地址</span><b id="st-addr">—</b></div>
    <div class="kv"><span>登录口令</span><b id="st-pass">—</b></div>
  </div>

  <section class="card" id="card-password">
    <h2><span class="step-no">1</span>设置登录口令</h2>
    <p class="hint">这个口令用于之后打开盒子的控制界面。至少 <span id="pw-min">8</span> 位。</p>
    <label for="pw1">新口令</label>
    <input id="pw1" type="password" autocomplete="new-password"
           inputmode="text" placeholder="至少 8 位">
    <label for="pw2">再输入一次</label>
    <input id="pw2" type="password" autocomplete="new-password"
           inputmode="text" placeholder="确认口令">
    <button id="pw-btn" type="button">保存口令</button>
    <div id="pw-msg" class="msg"></div>
  </section>

  <section class="card locked" id="card-wifi">
    <h2><span class="step-no">2</span>连接你的 Wi-Fi</h2>
    <p class="hint" id="wifi-lock-hint">请先设置登录口令，再连接 Wi-Fi。</p>
    <p class="hint" id="wifi-warn">如果你现在是通过 Wi-Fi 访问这台盒子的，
      保存后这一页会断开 —— 请到 <b id="warn-mdns">盒子的 mDNS 名字</b> 继续。</p>
    <button id="scan-btn" type="button" class="ghost" disabled>扫描附近的 Wi-Fi</button>
    <ul class="rows" id="net-list"></ul>
    <div id="pick" class="hidden">
      <label for="psk">“<span id="pick-name"></span>” 的密码</label>
      <input id="psk" type="password" autocomplete="off"
             inputmode="text" placeholder="Wi-Fi 密码（开放网络可留空）">
      <button id="join-btn" type="button">连接并切换到这个 Wi-Fi</button>
    </div>
    <div id="wifi-msg" class="msg"></div>
  </section>

  <section class="card">
    <h2>接下来会发生什么</h2>
    <div class="note">
      <p><b>如果你是插着网线来的</b>（盒子自己开的 Wi-Fi 无关紧要）：切换 Wi-Fi
         不会打断你。切成功后这台盒子会多一条无线连接，你可以继续用现在的地址，
         也可以拔掉网线改用 <code id="note-mdns"></code>。</p>
      <p><b>如果你是通过 Wi-Fi 来的</b>：保存之后这一页会立刻断开，这是正常的，
         不是失败。请把设备连回你刚才选的那个 Wi-Fi，然后在浏览器里打开
         <code id="note-mdns2"></code>。</p>
      <p>iOS 上 <code>.local</code> 直接可用；Windows 需要装 Bonjour；
         Android 视机型而定。都不行时，到路由器后台的客户端列表里找
         <b id="note-hostname"></b> 的地址，用
         <code>https://那个地址:8888</code> 打开。</p>
      <p style="margin-top:10px">如果 Wi-Fi 口令输错了，盒子会在 30 秒内
         <b>自动退回原来的网络</b>，这一页刷新后就能看到失败原因，可以直接重试。</p>
      <p>本页用的是盒子自签的 HTTPS 证书，浏览器会警告一次，选「继续访问」即可。</p>
      <p><b>请在你自己信任的网络里完成这一步</b>：在口令设好之前，和这台盒子同网段的
         设备都能打开这个页面。</p>
    </div>
  </section>

  <footer>MRRC Modern · 初始化</footer>
</div>

<div id="overlay" class="overlay hidden">
  <div class="box">
    <div class="spinner"></div>
    <h2 id="ov-title">正在切换网络…</h2>
    <p class="big" id="ov-text"></p>
    <div class="note">
      <ol>
        <li>这一页<b>马上会断开</b> —— 这是正常的，不是失败。</li>
        <li>请把设备连回 <b id="ov-ssid"></b>。</li>
        <li>然后打开 <code id="ov-mdns"></code> 看结果。
            盒子会在 30 秒内确认拿到地址；没拿到就退回原来的网络。</li>
      </ol>
    </div>
  </div>
</div>

<script src="box_api.js?v=1"></script>
<script>
(function () {
  'use strict';
  var state = { min: 8, mdns: '', unlocked: false, picked: '' };

  function unlockWifi() {
    state.unlocked = true;
    document.querySelector('#card-wifi').classList.remove('locked');
    document.querySelector('#wifi-lock-hint').classList.add('hidden');
    Box.disabled('#scan-btn', false);
  }

  function renderNetworks(networks) {
    var list = document.querySelector('#net-list');
    list.innerHTML = '';
    networks.forEach(function (net) {
      var item = document.createElement('li');
      var button = document.createElement('button');
      button.type = 'button';
      button.innerHTML =
        '<span class="ssid"></span>' +
        '<span class="lock">' + (net.protected ? '🔒' : '开放') + '</span>' +
        '<span class="sig">' + Box.signalText(net.signal) + '</span>';
      button.querySelector('.ssid').textContent = net.ssid;
      button.addEventListener('click', function () {
        state.picked = net.ssid;
        Array.prototype.forEach.call(
          list.querySelectorAll('button'),
          function (other) { other.classList.remove('picked'); });
        button.classList.add('picked');
        document.querySelector('#pick-name').textContent = net.ssid;
        Box.show('#pick', true);
        Box.disabled('#psk', !net.protected);
        document.querySelector('#psk').placeholder =
          net.protected ? 'Wi-Fi 密码' : '开放网络，留空即可';
      });
      item.appendChild(button);
      list.appendChild(item);
    });
    if (!networks.length) {
      Box.message('#wifi-msg', '没扫到任何 Wi-Fi。盒子离路由器太远，或者无线网卡没起来。', 'bad');
    }
  }

  function load() {
    Box.api('/api/box/onboarding').then(function (data) {
      state.min = data.min_password_len || 8;
      state.mdns = data.mdns || '';
      document.querySelector('#pw-min').textContent = state.min;
      Box.setText('#st-name', data.hostname || '—');
      Box.setText('#st-mdns', state.mdns || '（不可用）');
      Box.setText('#st-addr', location.hostname);
      Box.setText('#st-pass', data.needs_password ? '还没设置' : '已设置');
      ['#warn-mdns', '#note-mdns', '#note-mdns2', '#ov-mdns'].forEach(
        function (selector) {
          var node = document.querySelector(selector);
          if (node) node.textContent = state.mdns || ('https://' + location.hostname + ':8888');
        });
      Box.setText('#note-hostname', data.hostname || '');
      if (!data.needs_password) unlockWifi();
      renderNetworks(data.networks || []);
    }).catch(function (error) {
      if (error.status === 401) {
        document.querySelector('#errbox').textContent =
          '这台盒子已经完成初始设置了，或者你现在不在它自己的网段里。' +
          '请直接打开 https://' + location.hostname + ':8888/settings 登录。';
        Box.show('#errbox', true);
        return;
      }
      Box.message('#pw-msg', error.message, 'bad');
    });
  }

  Box.on('#pw-btn', 'click', function () {
    var password = document.querySelector('#pw1').value;
    var confirm = document.querySelector('#pw2').value;
    Box.message('#pw-msg', '正在保存…', '');
    Box.disabled('#pw-btn', true);
    Box.api('/api/box/password', {
      method: 'POST', body: { password: password, confirm: confirm }
    }).then(function () {
      Box.message('#pw-msg', '口令已保存。', 'good');
      Box.setText('#st-pass', '已设置');
      Box.show('#banner', false);
      unlockWifi();
    }).catch(function (error) {
      Box.message('#pw-msg', error.message, 'bad');
    }).then(function () {
      Box.disabled('#pw-btn', false);
    });
  });

  Box.on('#scan-btn', 'click', function () {
    Box.disabled('#scan-btn', true);
    Box.message('#wifi-msg', '正在扫描…', '');
    Box.api('/api/box/wifi').then(function (data) {
      renderNetworks(data.networks || []);
      Box.message('#wifi-msg', '', '');
    }).catch(function (error) {
      Box.message('#wifi-msg', error.message, 'bad');
    }).then(function () {
      Box.disabled('#scan-btn', false);
    });
  });

  Box.on('#join-btn', 'click', function () {
    if (!state.picked) return;
    Box.setText('#ov-ssid', state.picked);
    Box.show('#overlay', true);
    Box.api('/api/box/wifi', {
      method: 'POST',
      body: { ssid: state.picked, password: document.querySelector('#psk').value }
    }).catch(function (error) {
      Box.show('#overlay', false);
      Box.message('#wifi-msg', error.message, 'bad');
    });
  });

  load();
})();
</script>
</body>
```

> 页面上那条「请在你自己信任的网络里完成这一步」是 spec §5.4 要求写进用户提示的原话所对应的东西 —— 窗口判据比旧设计弱（同子网 ≠ 独占网络），这句提示是它唯一的缓解。

- [ ] **Step 3: 写 `static/settings.html`**

`<style>` 块与 `setup.html` 完全相同（**从 `static/setup.html` 的 `<style>...</style>` 整段复制**，两页同一套调色板），额外加一条：

```css
.panel{margin:0 0 14px}
.panel h2{margin-bottom:8px}
.grid{display:grid;grid-template-columns:auto 1fr;gap:4px 14px;font-size:13px}
.grid .k{color:var(--dim)}
.grid .v{text-align:right;word-break:break-all}
.bad-text{color:var(--bad)}
.good-text{color:var(--good)}
select{
  width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;
  background:#111;color:var(--text);font-size:16px;
}
```

`<body>`：

```html
<body>
<div class="wrap">

  <header>
    <h1>MRRC Modern</h1>
    <p class="sub">盒子设置</p>
  </header>

  <div id="banner" class="banner hidden">
    <b>这台盒子还没有人设过口令。</b>在你设置之前，和它同网段的设备都能打开
    <code>/setup</code>。请先完成 <a href="/setup">初始化</a>。
  </div>
  <div id="errbox" class="errbox hidden"></div>

  <section class="card panel">
    <h2>盒子身份</h2>
    <div class="grid">
      <span class="k">名字</span><span class="v" id="id-name">—</span>
      <span class="k">mDNS 名字</span><span class="v" id="id-mdns">—</span>
      <span class="k">当前地址</span><span class="v" id="id-addr">—</span>
      <span class="k">时区</span><span class="v" id="id-tz">—</span>
      <span class="k">已开机</span><span class="v" id="id-uptime">—</span>
    </div>
    <label for="name-input">改名字（会影响 mDNS 地址，改完请用新名字重新打开本页）</label>
    <input id="name-input" type="text" placeholder="MRRC-Modern" spellcheck="false">
    <button id="name-btn" type="button">保存名字</button>
    <div id="name-msg" class="msg"></div>
  </section>

  <section class="card panel">
    <h2>网络体检 <span class="pill" id="net-pill">读取中…</span></h2>
    <div class="grid" id="net-grid"></div>
  </section>

  <section class="card panel">
    <h2>Wi-Fi</h2>
    <div class="grid" id="wifi-grid"></div>
    <button id="scan-btn" type="button" class="ghost">扫描附近的 Wi-Fi</button>
    <ul class="rows" id="net-list"></ul>
    <div id="pick" class="hidden">
      <label for="psk">“<span id="pick-name"></span>” 的密码</label>
      <input id="psk" type="password" autocomplete="off" placeholder="Wi-Fi 密码（开放网络可留空）">
      <button id="join-btn" type="button">连接并切换到这个 Wi-Fi</button>
      <p class="note" id="wifi-warn">如果你现在正是通过 Wi-Fi 访问这台盒子，
        <b>本页会断开</b>。稍后用 <code id="warn-mdns"></code> 打开本页看结果。</p>
    </div>
    <div id="wifi-msg" class="msg"></div>
  </section>

  <section class="card panel">
    <h2>有线口</h2>
    <p class="hint">改的如果正是你现在走的这条路，本页同样会断开。</p>
    <label for="link-profile">连接</label>
    <select id="link-profile"></select>
    <label for="link-method">方式</label>
    <select id="link-method">
      <option value="auto">自动获取（DHCP）</option>
      <option value="manual">静态地址</option>
    </select>
    <div id="link-manual" class="hidden">
      <label for="link-address">地址/前缀</label>
      <input id="link-address" type="text" placeholder="192.168.16.217/24" spellcheck="false">
      <label for="link-gateway">网关（可留空）</label>
      <input id="link-gateway" type="text" placeholder="192.168.16.1" spellcheck="false">
    </div>
    <button id="link-btn" type="button">应用</button>
    <div id="link-msg" class="msg"></div>
  </section>

  <section class="card panel" id="card-system">
    <h2>盒子系统</h2>
    <div class="grid" id="sys-grid"></div>
    <div id="helper-note" class="note hidden">
      这台设备上没有 root helper（桌面版就是这样），所以重启与改时区不可用。
      网络相关的操作不受影响。
    </div>
    <label for="tz-select">时区</label>
    <select id="tz-select"></select>
    <button id="tz-btn" type="button">保存时区</button>
    <hr style="border:none;border-top:1px solid var(--line);margin:16px 0">
    <button id="svc-btn" type="button" class="ghost">重启服务（MRRC Modern 自己）</button>
    <button id="reboot-btn" type="button" class="ghost">重启盒子（整台机器）</button>
    <div id="sys-msg" class="msg"></div>
  </section>

  <section class="card panel">
    <h2>登录口令</h2>
    <label for="pw1">新口令</label>
    <input id="pw1" type="password" autocomplete="new-password">
    <label for="pw2">再输入一次</label>
    <input id="pw2" type="password" autocomplete="new-password">
    <button id="pw-btn" type="button">保存口令</button>
    <div id="pw-msg" class="msg"></div>
  </section>

  <section class="card panel">
    <h2>电台</h2>
    <p class="hint">机型与串口的配置在控制界面的设置里，本页不重复一套。</p>
    <a href="/"><button type="button" class="ghost">打开控制界面</button></a>
  </section>

  <section class="card panel">
    <h2>安全门禁 <span class="tag">只读</span></h2>
    <p class="hint">这里只显示、不修改。改它们要走配置层和它的两步确认。</p>
    <div id="safety"></div>
  </section>

  <footer>MRRC Modern · 盒子设置</footer>
</div>

<script src="box_api.js?v=1"></script>
<script>
(function () {
  'use strict';
  var state = { mdns: '', picked: '', helper: false };

  function kv(container, key, value, kind) {
    var k = document.createElement('span');
    k.className = 'k';
    k.textContent = key;
    var v = document.createElement('span');
    v.className = 'v' + (kind ? ' ' + kind : '');
    v.textContent = value === '' || value === null || value === undefined
      ? '（无）' : String(value);
    container.appendChild(k);
    container.appendChild(v);
  }

  function renderNetwork(net) {
    var grid = document.querySelector('#net-grid');
    grid.innerHTML = '';
    var pill = document.querySelector('#net-pill');
    if (!net.nm_running) {
      pill.textContent = 'NetworkManager 没在跑';
      pill.className = 'pill bad';
    } else if (!net.addresses.length) {
      pill.textContent = '没有地址';
      pill.className = 'pill bad';
    } else {
      pill.textContent = '已联网';
      pill.className = 'pill good';
    }
    kv(grid, '地址', net.addresses.join('、'));
    kv(grid, '网关', net.gateways.join('、'));
    kv(grid, 'DNS', net.dns.join('、'));
    kv(grid, 'Wi-Fi', net.wifi.ssid
      ? net.wifi.ssid + '  ' + Box.signalText(net.wifi.signal) : '未连接');
    net.devices.forEach(function (device) {
      kv(grid, '网卡 ' + device.interface,
         device.state + (device.connection ? '  →  ' + device.connection : ''),
         device.state === 'connected' ? 'good-text' : '');
    });
  }

  function renderWifi(net) {
    var grid = document.querySelector('#wifi-grid');
    grid.innerHTML = '';
    if (!net.wifi.ssid) {
      kv(grid, '当前', '没有连接任何 Wi-Fi');
    } else {
      kv(grid, '当前', net.wifi.ssid + '  ' + Box.signalText(net.wifi.signal));
    }
    var saved = net.connections.filter(function (c) {
      return c.type === '802-11-wireless' || c.type === 'wifi';
    });
    saved.forEach(function (c) {
      kv(grid, '已保存', c.name + (c.autoconnect ? '（开机自动连接）' : '（不自动连接）'));
    });
  }

  function renderLink(net) {
    var select = document.querySelector('#link-profile');
    var previous = select.value;
    select.innerHTML = '';
    net.connections.forEach(function (c) {
      var option = document.createElement('option');
      option.value = c.name;
      option.textContent = c.name + '（' + c.type + '）';
      select.appendChild(option);
    });
    if (previous) select.value = previous;
  }

  function renderSystem(system, helper) {
    var grid = document.querySelector('#sys-grid');
    grid.innerHTML = '';
    system.services.forEach(function (service) {
      kv(grid, service.unit, service.active ? '运行中' : '未运行',
         service.active ? 'good-text' : 'bad-text');
    });
    Box.setText('#id-uptime', Box.secondsLabel(system.uptime_s));
    Box.setText('#id-tz', system.timezone || '（未知）');
    Box.show('#helper-note', !helper);
    ['#name-btn', '#tz-btn', '#reboot-btn'].forEach(function (selector) {
      Box.disabled(selector, !helper);
    });
  }

  function renderSafety(fields) {
    var box = document.querySelector('#safety');
    box.innerHTML = '';
    fields.forEach(function (field) {
      var row = document.createElement('div');
      row.className = 'field';
      var key = document.createElement('div');
      key.className = 'k';
      key.textContent = field.key;
      var value = document.createElement('div');
      value.className = 'v';
      value.textContent = field.value === '' ? '（未设置）' : field.value;
      var note = document.createElement('div');
      note.className = 'note';
      note.textContent = field.note;
      row.appendChild(key); row.appendChild(value); row.appendChild(note);
      box.appendChild(row);
    });
  }

  function load() {
    Box.api('/api/box/status').then(function (data) {
      state.mdns = data.identity.mdns || '';
      state.helper = !!data.helper_available;
      Box.setText('#id-name', data.identity.hostname || '—');
      Box.setText('#id-mdns', state.mdns || '（不可用）');
      Box.setText('#id-addr', data.identity.addresses.join('、') || '（无）');
      var warn = document.querySelector('#warn-mdns');
      if (warn) warn.textContent = state.mdns || ('https://' + location.hostname + ':8888');
      var input = document.querySelector('#name-input');
      if (input && !input.value) input.value = data.identity.hostname || '';
      Box.show('#banner', !!data.first_window_open);
      renderNetwork(data.network);
      renderWifi(data.network);
      renderLink(data.network);
      renderSystem(data.system, state.helper);
      renderSafety(data.safety);
    }).catch(function (error) {
      if (error.status === 401) {
        location.href = '/login?next=/settings';
        return;
      }
      document.querySelector('#errbox').textContent = error.message;
      Box.show('#errbox', true);
    });
  }

  function reload() { load(); }

  Box.on('#name-btn', 'click', function () {
    var name = document.querySelector('#name-input').value;
    Box.message('#name-msg', '正在改名…', '');
    Box.api('/api/box/hostname', { method: 'POST', body: { hostname: name } })
      .then(function () {
        Box.message('#name-msg',
          '已改名为 ' + name + '，mDNS 也重新广播了。请用新名字重新打开本页。', 'good');
        reload();
      })
      .catch(function (error) { Box.message('#name-msg', error.message, 'bad'); });
  });

  Box.on('#tz-btn', 'click', function () {
    Box.api('/api/box/timezone', {
      method: 'POST', body: { timezone: document.querySelector('#tz-select').value }
    }).then(function () { Box.message('#sys-msg', '时区已保存。', 'good'); reload(); })
      .catch(function (error) { Box.message('#sys-msg', error.message, 'bad'); });
  });

  Box.on('#scan-btn', 'click', function () {
    Box.disabled('#scan-btn', true);
    Box.api('/api/box/wifi').then(function (data) {
      var list = document.querySelector('#net-list');
      list.innerHTML = '';
      (data.networks || []).forEach(function (net) {
        var item = document.createElement('li');
        var button = document.createElement('button');
        button.type = 'button';
        button.innerHTML = '<span class="ssid"></span><span class="lock">'
          + (net.protected ? '🔒' : '开放') + '</span><span class="sig">'
          + Box.signalText(net.signal) + '</span>';
        button.querySelector('.ssid').textContent = net.ssid;
        button.addEventListener('click', function () {
          state.picked = net.ssid;
          document.querySelector('#pick-name').textContent = net.ssid;
          Box.show('#pick', true);
        });
        item.appendChild(button);
        list.appendChild(item);
      });
    }).catch(function (error) {
      Box.message('#wifi-msg', error.message, 'bad');
    }).then(function () { Box.disabled('#scan-btn', false); });
  });

  Box.on('#join-btn', 'click', function () {
    if (!state.picked) return;
    Box.message('#wifi-msg', '正在切换。如果本页断开，请用 ' +
      (state.mdns || '盒子的新地址') + ' 重新打开。', '');
    Box.api('/api/box/wifi', {
      method: 'POST',
      body: { ssid: state.picked, password: document.querySelector('#psk').value }
    }).then(function () { poll(); })
      .catch(function (error) { Box.message('#wifi-msg', error.message, 'bad'); });
  });

  function poll() {
    Box.api('/api/box/wifi/status').then(function (status) {
      if (status.phase === 'switching') {
        setTimeout(poll, 2000);
        return;
      }
      if (status.phase === 'ok') {
        Box.message('#wifi-msg', '已连上 ' + status.ssid +
          '，地址 ' + status.address + '。', 'good');
      } else if (status.phase === 'failed') {
        Box.message('#wifi-msg', status.reason, 'bad');
      }
      reload();
    }).catch(function () { setTimeout(poll, 3000); });
  }

  Box.on('#link-method', 'change', function (event) {
    Box.show('#link-manual', event.target.value === 'manual');
  });

  Box.on('#link-btn', 'click', function () {
    Box.api('/api/box/link', {
      method: 'POST',
      body: {
        profile: document.querySelector('#link-profile').value,
        method: document.querySelector('#link-method').value,
        address: document.querySelector('#link-address').value,
        gateway: document.querySelector('#link-gateway').value
      }
    }).then(function () { Box.message('#link-msg', '已提交，正在生效…', ''); poll(); })
      .catch(function (error) { Box.message('#link-msg', error.message, 'bad'); });
  });

  Box.on('#svc-btn', 'click', function () {
    Box.api('/api/restart', { method: 'POST' }).then(function () {
      Box.message('#sys-msg', '服务正在重启，几秒后刷新本页。', 'good');
    }).catch(function (error) { Box.message('#sys-msg', error.message, 'bad'); });
  });

  Box.on('#reboot-btn', 'click', function () {
    if (!window.confirm('重启整台盒子？所有连接都会断开，大约一分钟后才回来。')) return;
    Box.api('/api/box/system/reboot',
            { method: 'POST', body: { confirm: 'reboot' } })
      .then(function () { Box.message('#sys-msg', '盒子正在重启，约一分钟后回来。', 'good'); })
      .catch(function (error) { Box.message('#sys-msg', error.message, 'bad'); });
  });

  Box.on('#pw-btn', 'click', function () {
    Box.api('/api/box/password', {
      method: 'POST',
      body: { password: document.querySelector('#pw1').value,
              confirm: document.querySelector('#pw2').value }
    }).then(function () { Box.message('#pw-msg', '口令已保存。', 'good'); })
      .catch(function (error) { Box.message('#pw-msg', error.message, 'bad'); });
  });

  // The timezone list is the browser's own: no endpoint needed, and it is the
  // list of the device the operator is holding rather than of the box.
  (function fillTimezones() {
    var select = document.querySelector('#tz-select');
    var zones = [];
    try {
      zones = Intl.supportedValuesOf('timeZone');
    } catch (ignored) {
      zones = ['Asia/Shanghai', 'Asia/Tokyo', 'UTC', 'Europe/London',
               'America/New_York', 'America/Los_Angeles'];
    }
    zones.forEach(function (zone) {
      var option = document.createElement('option');
      option.value = zone;
      option.textContent = zone;
      select.appendChild(option);
    });
    select.value = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  })();

  load();
})();
</script>
</body>
```

- [ ] **Step 4: 加 `/settings` 路由**

`server.py` 的 `setup_page` 之后加：

```python
@app.get("/settings", include_in_schema=False)
async def settings_page(request: Request):
    """The box settings page. Behind login like everything else — the
    middleware enforces it, and this route only has to serve the file."""
    page = STATIC_DIR / "settings.html"
    if not page.exists():
        return HTMLResponse("<h1>404 Not Found</h1>", status_code=404)
    return FileResponse(page, media_type="text/html")
```

- [ ] **Step 5: 写测试**

创建 `tests/test_server_box_pages.py`：

```python
"""The two pages, and the one rule they share.

`/setup` and `/settings` are separate HTML by decision (the onboarding page has
to work with no session at all, and the settings page must never be reachable
without one). They share a backend and a client script — this file keeps the
backend from growing a second implementation for one of them, and keeps the
onboarding page from acquiring anything a passwordless caller should not read.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


class SourceTests(unittest.TestCase):
    def test_both_pages_load_the_shared_client(self):
        for name in ("setup.html", "settings.html"):
            text = (STATIC / name).read_text(encoding="utf-8")
            with self.subTest(page=name):
                self.assertIn("box_api.js", text)

    def test_the_shared_client_is_not_versionless(self):
        """sw.js is cache-first, and a script served under an unchanged key is
        corrected on disk while the browser runs the old one."""
        for name in ("setup.html", "settings.html"):
            text = (STATIC / name).read_text(encoding="utf-8")
            with self.subTest(page=name):
                self.assertRegex(text, r"box_api\.js\?v=\d+")

    def test_the_onboarding_page_reads_only_these_three_endpoints(self):
        """spec §5.3. Two of the three are in the window; the third —
        /api/box/wifi — is not, and is reachable only because saving the
        password signs a session on the spot. So the Wi-Fi card must stay locked
        until that write succeeds, which is what `Box.disabled('#scan-btn',
        false)` inside `unlockWifi()` is for.

        Nothing else: the safety fields and the service list are not things an
        open page gets to enumerate."""
        text = (STATIC / "setup.html").read_text(encoding="utf-8")
        called = set(re.findall(r"Box\.api\('([^']+)'", text))
        self.assertEqual(called, {"/api/box/onboarding", "/api/box/password",
                                  "/api/box/wifi"})

    def test_the_settings_page_does_not_read_the_onboarding_endpoint(self):
        """That endpoint is the one thing a passwordless caller may reach. A page
        that already has a session has no business reading it."""
        text = (STATIC / "settings.html").read_text(encoding="utf-8")
        self.assertNotIn("/api/box/onboarding", text)

    def test_the_reboot_button_carries_its_confirmation(self):
        """A box reboot drops every connection and takes a minute to come back,
        and it sits directly above a button that restarts the service instead.
        Two buttons that read almost the same, doing very different things — so
        the page asks twice, and the second ask is the one that reaches the
        server (which checks for the literal string)."""
        text = (STATIC / "settings.html").read_text(encoding="utf-8")
        self.assertIn("confirm: 'reboot'", text)
        self.assertIn("window.confirm", text)

    def test_the_onboarding_page_warns_about_a_shared_network(self):
        """spec §5.4: the window's predicate is weaker than the retired
        hotspot's, and the honest mitigation is to say so where the operator can
        read it before they rely on it."""
        text = (STATIC / "setup.html").read_text(encoding="utf-8")
        self.assertIn("信任的网络", text)


class RouteTests(unittest.TestCase):
    def test_the_settings_page_is_served(self):
        import asyncio

        import server
        response = asyncio.run(server.settings_page(None))
        self.assertEqual(getattr(response, "status_code", 200), 200)

    def test_both_page_routes_sit_above_the_spa_fallback(self):
        """A GET route registered below the catch-all is answered with
        index.html instead of its own handler — that is how /api/cloud/state came
        back as a web page in v1.24.0."""
        source = (ROOT / "server.py").read_text(encoding="utf-8")
        fallback = source.index('app.add_api_route("/{path:path}", serve_static')
        for route in ('@app.get("/setup"', '@app.get("/settings"'):
            with self.subTest(route=route):
                self.assertLess(source.index(route), fallback)

    def test_no_document_names_the_retired_manage_page(self):
        for relative in ("server.py", "static/index.html", "static/listen.html"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            with self.subTest(path=relative):
                self.assertNotIn('"/manage"', text)
                self.assertNotIn("manage.html", text)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 6: 跑测试 + 提交**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
.venv/bin/pytest tests/test_server_box_pages.py -q
.venv/bin/pytest tests/ -q
git add static/box_api.js static/setup.html static/settings.html \
  server.py tests/test_server_box_pages.py
git commit -m "feat: 引导页 /setup 重写 + 新设置页 /settings + 共用 box_api.js

两页共用一套后端与一个客户端脚本；/setup 只读窗口内的两个端点，/settings
六面板（身份/网络体检/Wi-Fi/有线口/系统/安全门禁）。页面在动手之前就把
「切 Wi-Fi 会断开、30 秒内会回退、去哪个地址看结果」写在屏幕上。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 13: 打包 —— polkit 规则、helper unit、默认名字、avahi

**Files:**
- Create: `packaging/box/49-mrrc-nm.rules`
- Modify: `packaging/box/box-overlay.sh`（装规则、写 helper unit 并 enable、设 hostname、enable avahi）
- Modify: `tests/test_box_profiles.py`（新增一个装箱测试类）

**Interfaces:**
- Consumes: Task 6 的 `linux/box_helper.py`、Task 3 已在 `verify.sh` 里写好的四项检查
- Produces: `/etc/polkit-1/rules.d/49-mrrc-nm.rules`、`/etc/systemd/system/mrrc-box-helper.service`、镜像里 `hostname` 为 `MRRC-Modern`

- [ ] **Step 1: 写 polkit 规则**

创建 `packaging/box/49-mrrc-nm.rules`：

```javascript
// Let the MRRC server reconfigure NetworkManager without a session.
//
// The board's own rule (/usr/share/polkit-1/rules.d/
// org.freedesktop.NetworkManager.rules) requires `subject.local &&
// subject.active`, and a systemd system service has no session, so
// `subject.active` is false and adding `mrrc` to the netdev group does not help
// (measured on the W103D 2026-10-11: `npmcli connection add` returns
// "Insufficient privileges" for the mrrc user while `device show` works).
//
// Matching subject.user instead does not need a session, which is the whole
// point. The cost is honest and worth writing down: this grants the server all
// of NetworkManager's modify.system, not a verb whitelist. What limits the blast
// radius is that this file is the complete, auditable list of what was granted —
// see the design's §7.2, which rejected routing every nmcli write through the
// root helper precisely because that would put the Wi-Fi PSK on a second
// channel.
polkit.addRule(function (action, subject) {
    if (action.id.indexOf("org.freedesktop.NetworkManager.") === 0 &&
        subject.user === "mrrc") {
        return polkit.Result.YES;
    }
});
```

- [ ] **Step 2: `box-overlay.sh` 装规则**

在「写入 unit」那一段（`cat >/etc/systemd/system/mrrc-firstboot.service` 之前）之前加入：

```bash
log "polkit: let the server user reconfigure NetworkManager (design §7.2)"
install -d -m 0755 /etc/polkit-1/rules.d
install -m 0644 "$REPO_SRC/packaging/box/49-mrrc-nm.rules" \
	/etc/polkit-1/rules.d/49-mrrc-nm.rules
```

> 落在 `/etc/polkit-1/rules.d/` 而不是 `/usr/share/polkit-1/rules.d/`：前者优先，而且发行版升级包不会把它覆盖掉。

- [ ] **Step 3: `box-overlay.sh` 写 helper unit**

在 `mrrc-firstboot.service` 的 heredoc 之后加：

```bash
cat >/etc/systemd/system/mrrc-box-helper.service <<'UNIT'
[Unit]
Description=MRRC Modern box helper (the four system verbs the web pages may ask for)
# NetworkManager first only because the pages that use this helper also use NM:
# nothing here talks to it. No network-online.target — this must be up when
# there is no network, which is exactly when an operator is configuring one.
After=NetworkManager.service

[Service]
Type=simple
# Root for hostnamectl / timedatectl / systemctl reboot. Group=mrrc is what makes
# systemd create RuntimeDirectory=/run/mrrc owned by root:mrrc, so the socket
# inside it can be 0660 root:mrrc without this script chowning anything — /run is
# tmpfs, so the directory is recreated clean at every boot.
User=root
Group=mrrc
RuntimeDirectory=mrrc
RuntimeDirectoryMode=0770
ExecStart=/opt/mrrc_modern/venv/bin/python /opt/mrrc_modern/linux/box_helper.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=mrrc-box-helper

[Install]
WantedBy=multi-user.target
UNIT
```

并把 enable 行改成：

```bash
systemctl enable mrrc-box-helper.service mrrc-firstboot.service mrrc-modern.service
```

- [ ] **Step 4: `box-overlay.sh` 设默认名字并开 avahi**

在 `chown "$MRRC_USER:$MRRC_USER" "$ENV_FILE"` 之前加：

```bash
log "box identity: the image must not ship as 'armbian' (design §8)"
# static — not --pretty: the pretty hostname is the display name, the static one
# is what avahi advertises and what DHCP option 12 carries, and only the static
# one needs to be a legal hostname.
hostnamectl set-hostname --static MRRC-Modern || true
# hostnamectl stopped touching /etc/hosts in systemd 240. Debian's own image
# carries a 127.0.1.1 line naming the old hostname, and `hostname -f` (which
# sudo and some mailers use) goes through it.
if grep -q '^127\.0\.1\.1' /etc/hosts; then
	sed -i 's/^127\.0\.1\.1.*/127.0.1.1\tMRRC-Modern/' /etc/hosts
else
	printf '127.0.1.1\tMRRC-Modern\n' >>/etc/hosts
fi
```

在 enable 那一段之后加：

```bash
# avahi-daemon is what answers <hostname>.local. It ships enabled in the
# package, but this image masks units by default in several places, so say it
# out loud rather than trusting the package's postinst (design §8).
systemctl enable avahi-daemon.service || true
```

- [ ] **Step 5: 写装箱测试**

在 `tests/test_box_profiles.py` 里新增一个类：

```python
class BoxHelperPackagingTests(unittest.TestCase):
    """The two files that make the LAN onboarding path work on a box.

    Both are invisible when they are missing: no polkit rule means the Wi-Fi
    panel reports "Insufficient privileges", and no helper unit means the reboot
    and rename buttons are greyed out with no explanation. Neither failure shows
    up until an operator is standing at a box.
    """

    OVERLAY = REPO / "packaging" / "box" / "box-overlay.sh"
    RULE = REPO / "packaging" / "box" / "49-mrrc-nm.rules"
    UNIT = "mrrc-box-helper.service"

    def setUp(self):
        self.text = self.OVERLAY.read_text(encoding="utf-8")

    def test_the_polkit_rule_ships_and_is_installed(self):
        self.assertTrue(self.RULE.is_file())
        self.assertIn("49-mrrc-nm.rules", self.text)

    def test_the_rule_matches_the_user_and_not_a_session(self):
        """`subject.active` is the board's own rule, and it is exactly what a
        systemd system service never has — copying it here would make the same
        mistake the measured failure was made of."""
        rule = self.RULE.read_text(encoding="utf-8")
        self.assertIn('subject.user === "mrrc"', rule)
        self.assertNotIn("subject.active", rule)

    def test_the_rule_grants_only_networkmanager(self):
        rule = self.RULE.read_text(encoding="utf-8")
        self.assertIn("org.freedesktop.NetworkManager.", rule)
        self.assertNotIn('action.id === "', rule)

    def test_the_helper_unit_runs_as_root_with_the_mrrc_group(self):
        """Root for the verbs; Group=mrrc is what makes systemd hand out
        /run/mrrc as root:mrrc so the socket can be 0660."""
        body = self.text.split(f"/etc/systemd/system/{self.UNIT}", 1)[1]
        body = body.split("UNIT\n", 1)[0]
        self.assertIn("User=root", body)
        self.assertIn("Group=mrrc", body)

    def test_the_helper_unit_does_not_wait_for_a_network(self):
        """The helper is most needed when there is no network — it is how an
        operator renames a box and reboots it while standing at the console."""
        body = self.text.split(f"/etc/systemd/system/{self.UNIT}", 1)[1]
        body = body.split("UNIT\n", 1)[0]
        directives = "\n".join(line for line in body.splitlines()
                               if not line.lstrip().startswith("#"))
        self.assertNotIn("network-online.target", directives)

    def test_the_helper_unit_creates_the_runtime_directory(self):
        body = self.text.split(f"/etc/systemd/system/{self.UNIT}", 1)[1]
        body = body.split("UNIT\n", 1)[0]
        self.assertIn("RuntimeDirectory=mrrc", body)
        self.assertIn("RuntimeDirectoryMode=0770", body)

    def test_the_helper_is_enabled(self):
        enabled = [line for line in self.text.splitlines()
                   if "systemctl enable" in line]
        self.assertTrue(enabled)
        self.assertTrue(any(self.UNIT in line for line in enabled))

    def test_the_image_does_not_ship_as_armbian(self):
        self.assertIn("hostnamectl set-hostname --static MRRC-Modern", self.text)
        self.assertIn("/etc/hosts", self.text)

    def test_avahi_is_enabled_for_mdns(self):
        self.assertIn("systemctl enable avahi-daemon.service", self.text)

    def test_the_helper_script_reaches_the_image(self):
        """It rides the existing rsync — `linux/` is not in the exclusion list.
        Worth asserting because the symptom is a unit that dies on a box nobody
        can reach."""
        self.assertTrue((REPO / "linux" / "box_helper.py").is_file())
        excludes = ExclusionParityTests.EXCLUDE_RE.findall(self.text)
        self.assertNotIn("linux/", excludes)


class PolkiteRulesAreNotExecutable(unittest.TestCase):
    """0644, not 0755: polkit reads rules, it does not run them, and an
    executable file in rules.d is a thing an auditor has to stop and check."""

    def test_the_rule_is_installed_read_only(self):
        text = (REPO / "packaging" / "box" / "box-overlay.sh").read_text(
            encoding="utf-8")
        self.assertIn("install -m 0644", text)
```

- [ ] **Step 6: 跑测试 + 提交**

```bash
.venv/bin/pytest tests/test_box_profiles.py -q
.venv/bin/pytest tests/ -q
git add packaging/box/49-mrrc-nm.rules packaging/box/box-overlay.sh \
  tests/test_box_profiles.py
git commit -m "feat(packaging): polkit 规则 + box-helper unit + 默认名字 + avahi

polkit 按 subject.user==\"mrrc\" 放行 NM（板上那条默认规则要求 subject.active，
systemd 系统服务没有会话）；helper unit 用 User=root + Group=mrrc 让 systemd
把 /run/mrrc 建成 root:mrrc 0770；镜像落 hostnamectl set-hostname MRRC-Modern
并同步 /etc/hosts；enable avahi-daemon。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 14: 刷机时预置 Wi-Fi

**Files:**
- Create: `linux/wifi_preseed.py`
- Create: `tests/test_wifi_preseed.py`
- Modify: `packaging/box/box-overlay.sh`（写 `mrrc-wifi-preseed.service` 并 enable）
- Modify: `packaging/box/verify.sh`（第 11 项加一条 preseed unit 的检查）
- Modify: `tests/test_box_profiles.py`（补两条断言）

**Interfaces:**
- Consumes: `net_wifi.connect_wifi`、`net_wifi.nmcli`、`net_wifi.scrub`
- Produces:
  - `PRESEED_FILE = "/boot/mrrc-wifi.txt"`、`read_preseed(path=PRESEED_FILE) -> dict`
  - `main(argv=None) -> int`（0 = 已处理或无需处理，1 = 连不上，文件保留）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_wifi_preseed.py`：

```python
"""The Wi-Fi a user writes onto the card before the first boot.

Why a file of its own rather than a key in /boot/mrrc.env (design D-4): that
env file is copied into the 0640 env the server reads and a support bundle may
carry, and a Wi-Fi credential must never meet a config key. This one is read
once, applied, and deleted — vfat has no permission bits, so a plaintext PSK must
not be left lying on the boot partition.

The unit that runs this is separate from mrrc-firstboot.service for a reason
worth a test: that unit is ordered After=network-online.target, and joining the
Wi-Fi is the thing that brings the network online (spec R-8).
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import net_wifi
from tests.test_net_wifi import FakeNmcli

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "linux"))

import wifi_preseed  # noqa: E402


class ReadTests(unittest.TestCase):
    def write(self, text, encoding="utf-8"):
        handle = tempfile.NamedTemporaryFile("wb", suffix=".txt", delete=False)
        handle.write(text.encode(encoding))
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_it_reads_the_two_keys(self):
        path = self.write("SSID=我的WiFi\nPSK=hunter2secret\n")
        self.assertEqual(wifi_preseed.read_preseed(path),
                         {"SSID": "我的WiFi", "PSK": "hunter2secret"})

    def test_a_windows_editor_is_expected_not_a_surprise(self):
        """The user writes this on their own computer, with whatever that
        computer's editor defaults to. A BOM must not become part of the SSID."""
        for encoding in ("utf-8-sig", "cp936"):
            with self.subTest(encoding=encoding):
                path = self.write("SSID=home\nPSK=secret123\n", encoding)
                self.assertEqual(
                    wifi_preseed.read_preseed(path)["SSID"], "home")

    def test_comments_and_blanks_are_skipped(self):
        path = self.write("# my wifi\n\nSSID=home\n\nPSK=secret123\n")
        self.assertEqual(wifi_preseed.read_preseed(path)["SSID"], "home")

    def test_a_missing_file_is_an_empty_dict_not_an_error(self):
        self.assertEqual(wifi_preseed.read_preseed("/no/such/file"), {})

    def test_the_key_is_case_insensitive_but_the_value_is_not(self):
        """`ssid=` from a user who typed it in lowercase is the same request.
        The *value* is not touched: an SSID or PSK with a leading space is a
        different network from one without."""
        path = self.write("ssid= home \npsk= secret123 \n")
        values = wifi_preseed.read_preseed(path)
        self.assertEqual(values["SSID"], "home")
        self.assertEqual(values["PSK"], "secret123")


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "mrrc-wifi.txt"

    def run_main(self, text, runner=None):
        self.path.write_text(text, encoding="utf-8")
        with mock.patch.object(wifi_preseed, "PRESEED_FILE", str(self.path)), \
                mock.patch.object(net_wifi, "nmcli") as nmcli:
            nmcli.side_effect = runner if runner else lambda argv: \
                net_wifi.NmResult(0, "", "")
            code = wifi_preseed.main()
        return code

    def test_a_successful_join_deletes_the_file(self):
        """vfat has no permissions. The credential has to stop existing."""
        code = self.run_main("SSID=home\nPSK=secret123\n")
        self.assertEqual(code, 0)
        self.assertFalse(self.path.exists())

    def test_the_new_profile_is_told_to_autoconnect(self):
        """A Wi-Fi the user asked for should come back after a reboot. It is
        stated explicitly because the retired hotspot's profile defaulted to yes
        by accident, not by decision — and 'by default' is not a design."""
        self.run_main("SSID=home\nPSK=secret123\n")
        # 通过 mock 拿不到 argv，改为直接断言源码里那一行存在
        source = (REPO / "linux" / "wifi_preseed.py").read_text(encoding="utf-8")
        self.assertIn("connection.autoconnect", source)
        self.assertIn("yes", source)

    def test_a_failed_join_keeps_the_file(self):
        """A typo should cost a reboot, not a reflash."""
        code = self.run_main("SSID=home\nPSK=wrong\n",
                             runner=lambda argv: net_wifi.NmResult(
                                 1, "", "Error: Secrets were required."))
        self.assertEqual(code, 1)
        self.assertTrue(self.path.exists())

    def test_the_failure_is_logged_without_the_password(self):
        import io
        import contextlib
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            self.run_main("SSID=home\nPSK=hunter2secret\n",
                          runner=lambda argv: net_wifi.NmResult(
                              1, "", "Error: hunter2secret rejected."))
        self.assertNotIn("hunter2secret", captured.getvalue())

    def test_no_ssid_means_nothing_to_do(self):
        code = self.run_main("PSK=secret123\n")
        self.assertEqual(code, 0)

    def test_a_missing_file_is_not_a_failure(self):
        with mock.patch.object(wifi_preseed, "PRESEED_FILE", "/no/such/file"):
            self.assertEqual(wifi_preseed.main(), 0)


class PackagingTests(unittest.TestCase):
    OVERLAY = REPO / "packaging" / "box" / "box-overlay.sh"
    UNIT = "mrrc-wifi-preseed.service"

    def setUp(self):
        self.text = self.OVERLAY.read_text(encoding="utf-8")

    def body(self):
        return self.text.split(f"/etc/systemd/system/{self.UNIT}", 1)[1] \
            .split("UNIT\n", 1)[0]

    def test_it_is_conditional_on_the_file(self):
        self.assertIn("ConditionPathExists=/boot/mrrc-wifi.txt", self.body())

    def test_it_is_oneshot(self):
        self.assertIn("Type=oneshot", self.body())

    def test_it_runs_before_firstboot_rather_than_after_the_network(self):
        """R-8: mrrc-firstboot.service waits for network-online.target, and
        joining the Wi-Fi is what brings the network online. Putting this in that
        unit, or after it, is a circle."""
        body = self.body()
        self.assertIn("Before=mrrc-firstboot.service", body)
        directives = "\n".join(line for line in body.splitlines()
                               if not line.lstrip().startswith("#"))
        self.assertNotIn("network-online.target", directives)
        self.assertIn("After=NetworkManager.service", directives)

    def test_it_is_enabled(self):
        enabled = [line for line in self.text.splitlines()
                   if "systemctl enable" in line]
        self.assertTrue(any(self.UNIT in line for line in enabled))

    def test_vfat_has_no_permissions_so_the_file_is_deleted(self):
        body = self.body()
        self.assertIn("/boot/mrrc-wifi.txt", body)
        source = (REPO / "linux" / "wifi_preseed.py").read_text(encoding="utf-8")
        self.assertIn("os.unlink", source)

    def test_verify_sh_reports_whether_the_unit_is_installed(self):
        verify = (REPO / "packaging" / "box" / "verify.sh").read_text(
            encoding="utf-8")
        self.assertIn(self.UNIT, verify)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑它，确认它红**

```bash
.venv/bin/pytest tests/test_wifi_preseed.py -q
```

期望：`ModuleNotFoundError: No module named 'wifi_preseed'`。

- [ ] **Step 3: 写 `linux/wifi_preseed.py`**

```python
#!/usr/bin/env python3
"""Join the Wi-Fi named in /boot/mrrc-wifi.txt, then delete the file.

Flashing a card is the one moment a user can put something on this box before it
boots, and /boot is a vfat partition they can open on their own computer — so
that is where the credentials go:

    SSID=我的WiFi
    PSK=密码

Kept separate from /boot/mrrc.env on purpose (design D-4). That file carries
MRRC_* config keys and is copied into the 0640 env that support bundles may carry;
a Wi-Fi credential must never meet a config key. This file is read once, applied,
and deleted — vfat has no permission bits, so a plaintext PSK must not be left
lying there.

Its own unit rather than part of mrrc-firstboot.service (spec R-8): that unit is
ordered After=network-online.target, and joining this Wi-Fi is the thing that
brings the network online. Folding them together is a circle.

Runs as root under mrrc-wifi-preseed.service, but only ever calls `nmcli device
wifi connect` through net_wifi — the same argv the interactive path uses, so a
PSK here has the same one channel out. This is the only place it is ever written
down.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# The repo root is net_wifi's home; this script lives one level down in linux/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import net_wifi  # noqa: E402  # type: ignore[import-not-found]

PRESEED_FILE = "/boot/mrrc-wifi.txt"


def log(message: str) -> None:
    """journald, and console: on a headless box the console is where an operator
    with an HDMI monitor is watching for exactly this line."""
    print(f"mrrc-wifi-preseed: {message}", flush=True)


def _decode(raw: bytes) -> str:
    """The user wrote this on their own computer, with whatever that computer's
    editor defaults to. utf-8-sig eats a BOM; cp936 catches a Chinese Windows
    Notepad from before it defaulted to UTF-8. Same ladder first_run uses."""
    for encoding in ("utf-8-sig", "cp936", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", "replace")


def read_preseed(path: str = PRESEED_FILE) -> dict:
    """SSID and PSK from the file. Comments and blanks skipped.

    The *key* is matched case-insensitively — `ssid=` is the same request. The
    value is not touched: an SSID or a PSK with a leading space is a different
    network from one without, and trimming it would join something the user did
    not ask for.
    """
    try:
        with open(path, "rb") as handle:
            text = _decode(handle.read())
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        out[key.strip().upper()] = value.strip()
    return out


def main(argv=None) -> int:
    settings = read_preseed()
    ssid = settings.get("SSID", "")
    if not ssid:
        log("no SSID in the preseed file; nothing to do")
        return 0
    psk = settings.get("PSK", "")

    result = net_wifi.connect_wifi(ssid, psk)
    if not result.ok:
        # Keep the file: a typo should cost a reboot, not a reflash. The reason
        # is scrubbed because nmcli's own error text is the one place a PSK can
        # ride along into a journal.
        detail = net_wifi.scrub(result.stderr or result.stdout, psk).strip()
        log(f"could not join {ssid!r}: {detail or 'nmcli failed'}")
        return 1

    # Stated explicitly, not left to nmcli's default. A Wi-Fi the user asked for
    # should come back after a reboot — and the retired hotspot's profile is the
    # lesson in what an unexamined default costs.
    net_wifi.nmcli(["connection", "modify", ssid,
                    "connection.autoconnect", "yes"])

    try:
        os.unlink(PRESEED_FILE)
    except OSError as exc:
        log(f"joined {ssid!r}, but could not remove {PRESEED_FILE}: {exc}")
        return 0
    log(f"joined {ssid!r}; the preseed file has been removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

> `net_wifi` 的 import 在 `sys.path.insert` 之后，所以 `linux/wifi_preseed.py` 只能从镜像里（`/opt/mrrc_modern/linux/wifi_preseed.py`，父目录就是仓库根）正确运行 —— 这正是它被 install 到的位置。`linux/first_run.py` 用同样的形状找它的兄弟模块。

- [ ] **Step 4: `box-overlay.sh` 写 unit 并 enable**

在 helper unit 的 heredoc 之后加：

```bash
cat >/etc/systemd/system/mrrc-wifi-preseed.service <<'UNIT'
[Unit]
Description=MRRC Modern Wi-Fi preseed (/boot/mrrc-wifi.txt, applied once)
# NetworkManager, not network-online.target: joining this Wi-Fi is what brings
# the network online, so waiting for it would be a circle (design §9.2, R-8).
After=NetworkManager.service
Wants=NetworkManager.service
ConditionPathExists=/boot/mrrc-wifi.txt
# Before firstboot, which *does* need the network: it is what fetches nothing
# today but is ordered on network-online.target, and it is the unit that decides
# whether this box has ever been configured.
Before=mrrc-firstboot.service

[Service]
Type=oneshot
# Root because it is the only thing running this early and nmcli's system
# connections are root's until the polkit rule is loaded. The file is deleted on
# success, so a second boot runs nothing at all.
ExecStart=/opt/mrrc_modern/venv/bin/python /opt/mrrc_modern/linux/wifi_preseed.py
StandardOutput=journal+console
StandardError=journal+console
SyslogIdentifier=mrrc-wifi-preseed

[Install]
WantedBy=multi-user.target
UNIT
```

enable 行改成：

```bash
systemctl enable mrrc-box-helper.service mrrc-wifi-preseed.service mrrc-firstboot.service mrrc-modern.service
```

- [ ] **Step 5: `verify.sh` 第 11 项加一条**

在第 11 项的 `fi`（polkit 那条 `ok` 之后、avahi 那条 `if` 之前）插入：

```bash
if ! systemctl is-enabled --quiet mrrc-wifi-preseed.service 2>/dev/null; then
	bad "mrrc-wifi-preseed.service is not enabled" \
		"a card preseeded with /boot/mrrc-wifi.txt would be ignored; add it to box-overlay.sh"
else
	ok "Wi-Fi preseed unit is installed (inactive after a successful join)"
fi
```

- [ ] **Step 6: 跑测试 + 提交**

```bash
.venv/bin/pytest tests/test_wifi_preseed.py -q
.venv/bin/pytest tests/ -q
git add linux/wifi_preseed.py tests/test_wifi_preseed.py \
  packaging/box/box-overlay.sh packaging/box/verify.sh
git commit -m "feat: 刷机时预置 Wi-Fi（/boot/mrrc-wifi.txt + 独立 oneshot）

读 → 应用 → 删；键大小写不敏感、值原样保留；连不上就保留文件（改个错别字
只需重启，不必重刷）。独立 unit 且 Before=mrrc-firstboot.service，因为后者
等 network-online.target 而连 Wi-Fi 正是让网络上线的那件事。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 15: 文档与网站同步

**Files:**
- Modify: `SDD/08-architecture-decisions.md`（AD-026 摘要表行）
- Modify: `docs/W103D_GUIDE.md`（§7、§9 导语、§9.1 表格、§9.3、§9.6）
- Modify: `AGENTS.md:40`
- Modify: `CHANGELOG.md`
- Modify: `README.md:349,351`
- Modify: `docs/windows_installer_guide.md`（或 `win_pack.md`，看哪一份存在）、`docs/MACOS_INSTALLER_GUIDE.md`（若存在）
- Modify: `packaging/box/README.md`、`packaging/box/vendor-mt7663.md`
- Regenerate: `website/sdd/*.html`、`website/sdd.html`、`website/guide.html`（用仓库自己的生成器）
- Modify: `tests/test_sdd_docs_consistency.py`（若 AD-026 的措辞变了）

**Interfaces:**
- Consumes: 前 14 个 task 的全部产出
- Produces: 文档与代码一致；网站页面由生成器重出

- [ ] **Step 1: SDD 第 8 章**

`SDD/08-architecture-decisions.md:284` 的摘要表行现在写的是 `Implemented (V2.76)`，而同一个 AD 的**正文**（425-439）已经写着 2026-10-11 真机验收失败。两边不一致本身就是缺陷，先对齐：

摘要表的 Status 单元格改成与正文一致的措辞 —— 正文说的是「真机验收（V2.80，2026-10-11）：MT7663S 起 AP 的边界答案是否定的」，所以摘要行写成：

```
| AD-026 | Setup hotspot and the password-less onboarding window | Superseded (V2.80, 2026-10-11) — 真机验收失败，见正文与 2026-10-11 LAN 设计 |
```

正文末尾追加一段（**不要删掉验收失败的记录** —— 那是这个决策唯一有价值的遗产）：

```markdown
**接替方案（2026-10-11）**：热点这条上线路被放弃，改为「盒子接入 LAN → 打开网页
配置盒子 OS」。设计见 `docs/superpowers/specs/2026-10-11-w103d-lan-box-settings-design.md`，
实现见 `docs/superpowers/plans/2026-10-11-w103d-lan-box-settings.md`。

沿用下来的三条：**无密码面必须只有一个键宽**（只有 `MRRC_WEB_PASSWORD`）、
**判据是状态而不是标志**（旧的是「热点活着」，新的是「从未设过口令」）、
**Wi-Fi 凭据只走 `net_wifi` 的 argv**（为此拒绝了「全部 nmcli 写都过 root helper」
的路线，那会让 PSK 经第二条通道）。

没有沿用的：AP 生命周期、`/run/mrrc/setup-ap/state.json` 与 `wizard.json` 两个
跨用户信箱、`mrrc-setup-ap.service`。提权从「root supervisor 管整条热点」换成
「polkit 放行 `mrrc` 写 NM + 只管四个系统动词的小 root helper」。
```

- [ ] **Step 2: `docs/W103D_GUIDE.md`**

| 位置 | 现状 | 改成 |
|---|---|---|
| §7（212-214） | 推介初始化热点 | 「首次上线」：插网线，或往 `/boot/mrrc-wifi.txt` 写 SSID/PSK；两个都不可用时用 HDMI + `nmtui` |
| §9 导语（257-258） | 「这条已经实现了」 | 「这条的第一版（热点）在真机上失败了，现在的版本是局域网页面」，并指向本 spec |
| §9.1 表格第 4 行（262-267） | 手机热点引导 `✅ 能用` | 改成 `❌ 已退役（2026-10-11 真机验证失败）`，备注指向新路径 |
| §9.3（269-271） | 「预置只能写口令与电台设置，写不了 Wi-Fi 密码」 | 这条限制**已经不存在**：Wi-Fi 凭据现在走 `/boot/mrrc-wifi.txt`，独立于 env 文件（D-4 仍然成立，且正是它要求分开两个文件） |
| §9.6（340-393） | 详细描述热点向导流程 | 换成退役说明 + 新的两条上线路 + 「首次开机请在你信任的网络里完成设置」（spec §5.4） |
| 全文新增一节 | —— | 「盒子名字与 mDNS」：默认 `MRRC-Modern`，多台同网段会撞名、avahi 自称 `MRRC-Modern-2.local`、iOS/Windows/Android 的差异、DHCP 主机名作为退路、`/settings` 可以改 |

- [ ] **Step 3: `AGENTS.md:40`**

那一段现在描述 `net_wifi.py`/`linux/setup_ap.py`、热点、D-4 不变式与已验证的硬件边界。重写成：

```markdown
- **网络域**：`net_wifi.py` 是唯一与 `nmcli` 说话的地方（STA 半边：扫描、连接、
  地址、`-t` 转义解析）；`box_ops.py` 是它上面的一层纯逻辑（网络体检、Wi-Fi 切换
  状态机、有线口 DHCP/静态、密码校验、helper 客户端），两个页面共用它。
  **Wi-Fi 凭据只走 `net_wifi` 的 argv**，不落日志、不落 env、不进 helper socket
  （设计 D-4 / AD-021 / NFR-068），任何要回传给页面的 nmcli 文本必须过 `scrub()`。
  **任何新写的 NM profile 必须显式交代 `connection.autoconnect`** —— 曾经的
  `Hotspot` profile 默认 `yes`，让盒子每次开机自杀 WiFi。
  `linux/box_helper.py` 是 root 的四个系统动词（reboot / set-hostname /
  set-timezone / avahi-reload），固定动词表、argv 数组、`SO_PEERCRED` 只认 `mrrc`。
  2026-10-11 的 setup 热点（`linux/setup_ap.py`）已退役：板子的 MT7663S 起得来 AP
  但不发 beacon，真机验证失败；引导改走局域网页面 `/setup` + `/settings`。
```

- [ ] **Step 4: 其余文本**

- `README.md:349,351`：静态目录清单里 `manage.html` 换成 `settings.html`、`box_api.js` 加上去（`setup.html` 那一条留着，措辞改成「首次上线引导」）。
- `CHANGELOG.md`：在 `[Unreleased]` 下写 Added / Changed / Removed 三节。**措辞纪律**：写「新增了局域网配置页与两条上线路」，**不写**「两条上线路均可用」—— R-1（eth0 DHCP）与 R-2（polkit 放行）要到下次刷机才验证（spec §13）。Removed 一节写清退役了什么，以及 AP 模式在这块板子上不成立这一条硬件结论。
- `win_pack.md` / `docs/WINDOWS_INSTALLER_GUIDE.md`：只改提到 `manage.html` 与初始化的那句，说明桌面版没有 helper、系统面板会置灰。
- `packaging/box/README.md`：unit 清单加 `mrrc-box-helper.service` 与 `mrrc-wifi-preseed.service`，删 `mrrc-setup-ap.service`。
- `packaging/box/vendor-mt7663.md`：补一句 —— vendored 的 N9 是**工厂 STA 固件**，只解决 STA 稳定性（200 包 0–0.5% 丢包）；**AP 模式在这块板子上真机验证失败**（软件层全对、射频不发 beacon；`prefer_offload_fw=0` 会改载 `_rebb` 而芯片根本不初始化：`MCU initialization failed: -110`）。指向 spec §1。

- [ ] **Step 5: 重新生成网站页面**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
.venv/bin/python website/build_sdd.py
.venv/bin/python website/build_w103d_guide.py
git status --short website/
```

期望：`website/sdd/08-architecture-decisions.html`、`website/sdd/index.html`、`website/sdd.html` 被重写。**不部署**：`website/deploy.sh` 是发布动作，本计划不做，也不 push（用户的既有事实：线上文档落后一版是预期的）。

- [ ] **Step 6: 跑测试 + 提交**

```bash
.venv/bin/pytest tests/test_sdd_docs_consistency.py tests/test_website_links.py -q
.venv/bin/pytest tests/ -q
git add SDD/08-architecture-decisions.md docs/W103D_GUIDE.md AGENTS.md \
  CHANGELOG.md README.md packaging/box/README.md \
  packaging/box/vendor-mt7663.md website/
git commit -m "docs: 同步 SDD / W103D 指南 / AGENTS / CHANGELOG 与网站页面

AD-026 的摘要表行本来是 Implemented(V2.76)，而正文已写明真机验收失败 ——
对齐并补上接替方案与沿用/未沿用的清单。W103D 指南 §9 从「热点向导」改成
「局域网页面 + 两条上线路」，并补「盒子名字与 mDNS」一节。CHANGELOG 刻意
不写「两条上线路均可用」：R-1/R-2 要等下次刷机验。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## 刷机后的第一件事（不在本计划的执行范围内）

本计划**不碰运行中的盒子**。下次刷机完成后，按顺序验证下面五项，任何一项没结果之前不得对外宣称两条上线路可用：

```bash
# R-1：eth0 插网线能否 DHCP 到地址（整条「网线」路径的地基）
ip -4 addr show eth0 ; nmcli device status ; journalctl -u NetworkManager -n 50

# R-2：polkit 规则对 mrrc 是否真的生效 —— 必须用真服务验
#       （`sudo -u mrrc nmcli ...` 测出来的不算：那样起的进程可能还挂在 SSH 会话上）
systemctl restart mrrc-modern
sudo -u mrrc --login sh -c 'echo no | nmcli connection add type dummy con-name probe-polkit ifname dummy0' \
  && echo "R-2 通过" || echo "R-2 失败 → 回退路线甲（helper 承担 nmcli 写），并把 PSK 的隐私重新论证"
sudo nmcli connection delete probe-polkit 2>/dev/null || true

# R-3：avahi 与 dnsmasq 共存（5353 vs 53）
systemctl is-active avahi-daemon dnsmasq ; ss -lunp | grep 5353 ; avahi-resolve -4 -n "$(hostname).local"

# R-4：Wi-Fi 切换的回滚时间预算（初值 SWITCH_BUDGET_S = 30.0）
#       换一个 SSID，掐表量到「拿到地址」或「退回原网络」为止；按实测值改 box_ops.SWITCH_BUDGET_S

# R-8：无网线 + 有预置 Wi-Fi 的开机时序
#       往 /boot/mrrc-wifi.txt 写 SSID/PSK → 重启 → journalctl -u mrrc-wifi-preseed -b
#       文件应已删除，且 `systemctl status mrrc-firstboot` 显示它拿到了网络
```

---

## Self-Review

**1. Spec 覆盖**

| Spec 章节 | 落在哪个 task |
|---|---|
| §4.1 `net_wifi.py` 收缩 | Task 2 |
| §4.2 `box_ops.py` | Task 4、5、7、8 |
| §4.3 helper | Task 6、13 |
| §4.4 `server.py` 端点与中间件 | Task 1、9、10、11 |
| §4.5 两个页面 + `box_api.js` | Task 12 |
| §4.6 退役 | Task 1、2、3 |
| §4.7 电台配置不搬家 | Task 12（`/settings` 只放一个指向控制界面的入口；第二期才渲染控件） |
| §5.1 窗口判据 | Task 9（`_password_is_untouched` + `client_in_local_subnet`） |
| §5.2 关闭 | Task 9（`_write_password`） |
| §5.3 可写面收窄 | Task 1（`FIRST_WINDOW_PATHS`）、Task 9（两个端点）、Task 12（页面只读这两个） |
| §5.4 已知取舍写进用户提示 | Task 12（页面上那句「请在你自己信任的网络里完成这一步」） |
| §5.5 密码落地 | Task 9 |
| §6.1 Wi-Fi 列表 | Task 4（`scan_networks`）、Task 11、12 |
| §6.2 Wi-Fi 切换 | Task 7、11、12 |
| §6.3 网络体检 | Task 4、10、12 |
| §6.4 有线口 | Task 8、11、12 |
| §7.1/7.2 polkit | Task 13 |
| §7.3 helper 形状 | Task 5、6、10（`helper_available`）、12（置灰） |
| §8 身份与 mDNS | Task 4、6（`sync_hosts`）、11（nudge avahi）、13（默认名 + avahi）、12（页面两个都给） |
| §9.1 网线 | Task 13（`有线连接 1` 是 `auto` 的核对放在刷机后 R-1） |
| §9.2 预置 Wi-Fi | Task 14 |
| §9.3 与窗口的关系 | Task 9（`/setup` 重定向） |
| §10 路由与 API | Task 9、10、11、12 |
| §11 退役清单 | Task 1、2、3 |
| §12 打包改动 | Task 3、13、14 |
| §13 风险 | 风险表最后那一节；R-6 在 Task 4 的降级测试，R-7 在 Task 9 的 `SingleWriterTests` |
| §14 测试策略 | 每个 task 的测试文件 |
| §15 第一期 / 第二期 | 本计划 = 第一期；第二期见 `§4.7` 那一条 |

**2. 占位符扫描**

逐条查过：没有 `TBD` / `TODO` / 「稍后补」。**允许出现的两处「稍后由同一个计划替换」**，各自指名了替换它的 task 与 step：

- Task 1 的 `_first_window_open` 返回恒假的占位 —— Task 9 Step 3 替换，且 Task 9 的 `WindowPredicateTests` 正面证明它不再是恒假（否则那条测试和占位一起绿，占位就活下来了）。
- Task 4 的 `box_ops` import gate —— Task 4 Step 5 加回。

**三处写完第一版后回头改掉的，都是计划自身的缺陷而非执行时要做的决定：**

- `tests/test_server_box_status.py` 的 `status()` 辅助曾同时留着写错的草稿与正确版本，并附一句「只保留第二个」—— 现在只留 `return_value=` 那一版。
- `tests/test_server_box_pages.py` 的 `test_the_settings_page_never_offers_a_passwordless_write` 曾是一句同义反复（把一个字符串替换成它自己）。现在拆成 `test_the_settings_page_does_not_read_the_onboarding_endpoint` 与 `test_the_reboot_button_carries_its_confirmation` 两条真断言。
- Task 2 关于 `tests/test_net_wifi.py::SettingsTests` 的「逐方法看，不要整类盲删」是错的。回查后确认：那个类的六个方法**每一行**都在断言 `ap_settings()`，没有一条测还活着的常量，整类删即可。原话把一个不存在的分支列为可能的判据，等于让执行者去核对一件已经能确定的事。

**3. 类型与命名一致性**

跨 task 核对过：`box_ops.MinResult` 形状统一（`.ok` / `.stdout` / `.stderr`）；`SwitchState.snapshot()` 的五个键（`phase`/`ssid`/`reason`/`address`/`since`）在 Task 7 定义、Task 8 复用、Task 11 返回、Task 12 渲染，四处一致；`PHASE_*` 四个常量名一致；`perform_wifi_switch` 的签名在 Task 7 定义、Task 11 用 `asyncio.to_thread(func, ssid, password)` 调（位置参数对得上）；`perform_link_change` 的 `gateway` 是关键字参数，Task 11 用 `gateway=gateway` 传；`set_hostname`/`set_timezone`/`reboot`/`reload_avahi` 都返回带 `ok` 的 dict，Task 11 统一按 `result.get("ok")` 判 200/400。

一处**有意的偏离**：spec §7.3 说 `/run/mrrc` 由 systemd 建、**不靠脚本 chown**，但没给目录的组怎么办 —— 一个 `User=root` 的 unit 建出来的 `RuntimeDirectory` 是 `root:root`，socket 就没法是 `root:mrrc`。Task 13 用 `Group=mrrc` 解决（systemd 按 `User=`/`Group=` 给目录定属主，进程 uid 仍是 0），一个 unit 行、不 chown、不改 `RuntimeDirectoryMode=0770`。这比 spec 的原文更具体，不是相反。

**4. Review Focus**

五条都落到了具体测试：`tests/test_server_box_window.py::WindowPredicateTests::test_an_unknown_local_network_closes_the_window`（第 1 条）、`tests/test_box_ops_reports.py::ConnectionsReportTests::test_a_colon_inside_a_name_survives` 与 `test_a_chinese_locale_spelling_is_understood`（第 2、5 条）、`tests/test_server_box_status.py::test_a_missing_helper_is_reported_not_raised` 与 `tests/test_server_box_actions.py::RebootTests::test_a_helper_that_cannot_do_it_is_reported`（第 3 条）、`tests/test_server_box_window.py::SetupRouteTests::test_a_configured_box_goes_to_the_settings_page`（第 4 条）。
