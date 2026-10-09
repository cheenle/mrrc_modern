# W103D 初始化热点 + 最小向导 实现计划（规格 D-7.5 第 1 项）

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 刷完盘、不插网线、不接键盘，只拿一台手机 —— 开机后能在 Wi-Fi 列表里看到盒子开的**开放热点**，连上后打开页面**无需口令**即可设口令并选 Wi-Fi，成功后盒子切到 STA 并关掉热点。

**架构：** 三个新部件 + 一条既有通道。
① `net_wifi.py`（仓库根，**纯标准库、零应用 import**）是**全仓唯一**碰 `nmcli` 的地方：设备/上行判定、开/关热点、扫描、连网、地址读取，外加两份 JSON 邮箱文件的读写与**免口令闸门**判据。
② `linux/setup_ap.py` 是 `mrrc-setup-ap.service` 的常驻进程：按**网络状态**（不是"首次开机"）决定起不起热点、30 分钟无人引导就自己关掉并且**不再重开**（只有断电重启才重开）、向导切网期间**让位**给服务端。
③ `server.py` 加 `/setup` 页面与 4 个向导端点；闸门 = **热点此刻真的开着**（心跳新鲜）**且请求来自热点网段**；"设口令"**复用既有写入通道**（`first_run.update_env_file(_config_file_path(), …)`，与 `/api/setup` 同函数同文件），因此**不依赖 D-8 配置层重构**（规格 D-7.5）。
两份文件、两个唯一写入者：守护进程只写 `state.json`，服务端只写 `wizard.json`——**Wi-Fi 密码永不落盘**（只进 `nmcli` 的 argv）。

**技术栈：** Python 3.13（主仓 `.venv`，见约束 1）、纯标准库（`ipaddress`/`json`/`subprocess`/`dataclasses`/`threading`）、FastAPI（既有）、`unittest`、NetworkManager `nmcli`（`device wifi hotspot` / `device wifi list` / `device wifi connect`）、`dnsmasq`（NM shared 模式的 DHCP+DNS，规格 D-2）、systemd、bash（`box-overlay.sh` 在 `set -euo pipefail` 下）。

**设计文档：** `docs/superpowers/specs/2026-10-08-w103d-setup-ap-design.md`（下称"规格"；§N / D-N / R-N 指其章节、决策号与需求号）。本计划**只做**规格 D-7.5 交付表的**第 1 项**（热点 + 最小向导）；配置层统一（D-8）与完整管理页 `/manage` 是第 2、3 项，**不在本计划内**。

---

## 全局约束（每个任务都适用，违反即返工）

1. **测试环境与基线（2026-10-08 实测）**：本 worktree **没有自己的 venv**，用主仓那个：
   ```bash
   cd /Users/cheenle/HAM/hub/mrrc_modern/.worktrees/w103d-box
   PY=/Users/cheenle/HAM/hub/mrrc_modern/.venv/bin/python
   $PY -m unittest discover -s tests
   ```
   基线 ⇒ `Ran 1766 tests` / `FAILED (failures=1, skipped=1)`。那**唯一一条红是 worktree 路径造成的，不是产品缺陷、也不是你弄的**：`test_tls_trust_store.CallSiteGuardTests.test_every_call_site_sets_a_context` 按**绝对路径**的 parts 过滤隐藏目录，而本 worktree 在 `.worktrees/` 下 ⇒ 扫描集为空 ⇒ 它自己的"扫描不许空转"断言失败。修它属于 `2026-10-08-config-layer-unification.md` 的任务 1，**本计划不动它**。
   所以本计划里**每一步"预期全绿"的确切含义**是：
   ```bash
   $PY -m unittest discover -s tests 2>&1 | tail -4
   ```
   输出 `FAILED (failures=1, skipped=1)`，且唯一的 `FAIL:` 行是 `test_tls_trust_store`。**出现第 2 条红就是你弄坏的。** 单模块用 `$PY -m unittest tests.test_net_wifi -v`（`tests/` 没有 `__init__.py`，但命名空间包让这个形式可用；已实测）。用 `unittest`，不用 `pytest`（`tests/README.md` 是权威）。

2. **永远不要 `git add -A` / `git commit -a`。** 仓里有与本任务无关的脏文件（`atr1000_tuner.json`、`mem_channels.json`——AGENTS.md 明写"用户运行期文件，勿动"——以及 `website/downloads/latest.json.bak-*`）。每个 commit 步骤都逐字列出要 add 的路径。

3. **`net_wifi.py` 必须是纯标准库、零应用 import。** 三个硬理由：① 它被 `server.py` import，而 PyInstaller 的根模块分析会把它冻进**每一个**桌面包（Windows/macOS 上没有 `nmcli`，import 就必须照样成功，调用必须退化成"失败结果"而不是异常）；② `linux/setup_ap.py` 在 systemd 单元里跑，那时电台栈完全不相关；③ 与 `support_bundle.py` / `session_metrics.py` / `spectrum_profile.py` 同一口径（AGENTS.md 明写"Stdlib only and no app imports"）。
   **不要**在里面 `import config` / `import cloud_hub` / `import server`。
   **`grp` 与 `os.chown` 是 POSIX-only**，必须在函数**内部**延迟 import 并吞掉 `ImportError`/`AttributeError`/`OSError`，否则 Windows 上 `import net_wifi` 直接炸 ⇒ 桌面包起不来。

4. **Wi-Fi 密码（PSK）永不落盘、永不进日志**（SDD `support-bundle-privacy`，AD-021 / NFR-068）。它只出现在 `nmcli` 的 argv 里。具体三条：
   - `wizard.json` 只存 `action` / `ssid` / `state` / `error` / `address` / `nonce` / `heartbeat`，**没有 password 字段**（`WizardClaim` 的字段表本身就是守卫，任务 3 有测试直接断言字段集里没有 `password`）。
   - 任何日志行都不得包含它；`nmcli` 的错误文本在写进 `error` 之前必须过 `net_wifi.scrub(text, password)`。
   - 任务 7 有一条测试**直接断言**：跑完整个切换流程后，PSK 字符串既不在任何 log record 里，也不在 `state.json` / `wizard.json` 的文本里。

5. **免口令闸门必须同时成立两条**（规格 D-6 / R5）：
   ```
   免口令  ⟺  state.json 说 mode=="hotspot" 且心跳新鲜（≤45 s）  且  客户端 IP 在热点网段内
   ```
   第二条（"热点此刻真的开着"）不是装饰：它把"某个局域网客户端恰好是 10.42.0.x"这种网段撞车也关掉了。**闸门只放行 `/setup` 与 `/api/setup/wizard*` 这 4 条路径**，其余一律照旧要 token——任务 5 有测试钉住这一点。

6. **listen-only token 必须在处理函数里被拒**。中间件对 `SETUP_GATE_PATHS` 提前放行（否则未鉴权的热点客户端根本进不来），于是**跳过了**中间件那条"listen 角色不得写"的 403。所以 `_setup_access()` 必须写 `_verify_auth(request) and not _is_listen_request(request)`，并且任务 6 有一条测试：拿 listen token POST 设口令 ⇒ 403。**漏了这条就是权限提升。**

7. **`/api/setup/wizard/wifi` 必须拒绝"还没设口令就切网"**（409）。理由：盒子的口令在首启时是**自动生成的随机值**（规格 D-7.5 那条 ⚠️，`server.py` 的 `_ensure_strong_password()`，以及 `linux/first_run.py` 的 `apply_first_run()` 会写 `MRRC_AUTO_PASSWORD=1`），操作者并不知道它。若允许先切网，热点一关、闸门随之消失，操作者就被**锁在自己盒子外面**（只能靠 HDMI 或 `mrrc-show-password`）。判据用既有键：`MRRC_AUTO_PASSWORD == "1"` ⇒ 口令还是自动生成的 ⇒ 拒绝并说明。

8. **路由顺序**：新的 GET 路由必须注册在文件末尾那条 SPA 兜底 `app.add_api_route("/{path:path}", serve_static, …)`（`server.py:4846`）**之前**。既有守卫 `tests/test_cloud_endpoints.py::ServerRouteOrderTests::test_no_api_route_is_registered_after_the_spa_fallback` 会自动检查所有 `/api/` 路由；`/setup`（不以 `/api/` 开头）要在任务 5 手动加一条断言。

9. **`static/setup.html` 不进 `sw.js` 的 ASSETS、也不从 `index.html` 引用**，所以 `test_static_cache_busting.py` 不受影响（它只比对 index.html 的引用与 sw.js 的预缓存表——已核对源码）。反过来，`/setup` 的响应**必须带 `Cache-Control: no-store`**：向导页陈旧 = 操作者看到的窗口状态是假的。

10. **SDD 守门**：编辑前
    ```bash
    python3 ~/.pi/agent/skills/sdd-guardian/harness/sdd_context.py brief <要改的文件>
    ```
    每次 commit 前必须干净：
    ```bash
    $PY .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
    ```
    与本计划相关的 block 级约束：`secrets-hardcoded`（口令只从环境变量/操作者输入来，源码里不写字面量）与 `support-bundle-privacy`（见约束 4）。

11. **单射频，一次只能一件事**（规格 D-3）。MT7663S 是一颗射频，AP 与 STA **不并发**。所以切换期间守护进程必须**让位**（靠 `wizard.json` 的 claim），否则它会在服务端刚拆掉热点、还没连上新网的那几秒里看到"没有上行"而把热点重新拉起来，两边对打。

12. **热点自己不算上行**（本设计最容易写错、错了就无限自拆的一处）。`nmcli device status` 里 AP 模式的 `wlan0` 状态就是 `connected`（连接名 `Hotspot`）。`has_uplink()` 必须**排除**处于 AP 模式的连接，否则守护进程起了热点、下一个 tick 就把它拆了。任务 1 有一条专门的测试。

13. **`nmcli` 的状态词汇有两套**，跨 NM 版本不一致：`device status` 的人类列写 `connected`，而某些版本/terse 输出写 `activated`；"链路本地无网关"写作 `connected (site only)`，那**不是**可用上行。`_is_usable_uplink()` 两个词都认、并且把 `site only` 排除掉。任务 1 有测试覆盖全部拼法。

14. **不动这些**：`config.py`（只读常量的常量层）、`cloud_hub.py`、`backends/**`、`static/index.html`、`static/sw.js`、11 个 `packaging/box/profiles/*.env`、三份 `--exclude` 清单（`ExclusionParityTests` 钉着逐字相同；本计划新增的都是根模块或 `linux/` 下的文件，**自动**被三个 copier 带上，不需要改排除表）、`install.sh`（热点是**盒子镜像**的能力，桌面 Linux 安装手边有键盘，规格 §7 的组件表也只点了 `box-overlay.sh`）、`linux/mrrc_update.sh`（明写"env file left untouched"）。

15. **`/api/setup` 既有端点一个字都不改**。新端点全部在 `/api/setup/wizard*` 之下，与既有 `GET|POST /api/setup` 路径不冲突（FastAPI 精确匹配）。

16. **命名口径统一（跨任务不许漂移）**：
    - 模块 `net_wifi`；守护 `linux/setup_ap.py` 里的类 `Supervisor`；页面 `static/setup.html`；单元 `mrrc-setup-ap.service`。
    - 模式常量 `MODE_OFF` / `MODE_HOTSPOT` / `MODE_STA`；claim 状态 `WIZARD_SWITCHING` / `WIZARD_OK` / `WIZARD_FAILED`。
    - 数据类 `NmResult`（`returncode`/`stdout`/`stderr`，属性 `ok`/`detail`）、`ApState`（`mode`/`ssid`/`gateway`/`network`/`url`/`reason`/`since`/`heartbeat`/`deadline`）、`WizardClaim`（`action`/`ssid`/`state`/`error`/`address`/`nonce`/`heartbeat`）。
    - 环境变量 `MRRC_SETUP_AP_SSID` / `_IFNAME` / `_STATE_DIR` / `_TIMEOUT_MIN` / `_POLL_S` / `_SETTLE_S`。
    - 服务端符号 `SETUP_GATE_PATHS` / `SETUP_WRITABLE_KEYS` / `_setup_ap_state_dir` / `_setup_gate_open` / `_setup_access` / `_perform_wifi_switch` / `_wifi_failure_reason` / `WIFI_SWITCH_LEAD_S`。

17. **本机测不出真 Wi-Fi**（macOS 上没有 `nmcli`、更没有 MT7663S）。所以**所有**测试都用注入的假 runner（`FakeNmcli`）驱动；真机行为是规格 §10 明列的**未验证边界**，只能在任务 12 的真机验收里判。

---

## 摸底结论（写计划前实跑出来的五条，每条都改变了实现）

| # | 发现 | 后果 |
| --- | --- | --- |
| 1 | 规格 D-7.5 说 `server.py:3216` 经 `cloud_hub._write_config` 落盘。**实测 3216 行是 `updates["MRRC_WEB_PASSWORD"] = password`**，真正落盘的调用在它下面几行：`first_run.update_env_file(_config_file_path(), updates)`（`server.py:34` 的 `from macos import first_run`）。`cloud_hub._write_config` 是**另一处**写入者，只被 `_ensure_strong_password()`（`server.py:909`）用。 | 向导设口令**照抄 `/api/setup` 那条**：`first_run.update_env_file(_config_file_path(), …)`。这才是"既有写入通道"，也才与既有测试 `tests/test_server_setup.py`（patch `server.first_run.update_env_file`）同一口径。任务 11 把这条更正写回规格。 |
| 2 | `nmcli device status` 里 **AP 模式的 wlan0 也是 `connected`**；而 `connection show --active` 的 TYPE 对 AP 与 STA **都是** `802-11-wireless`。 | 见约束 12。区分 AP 只能再问一次 `802-11-wireless.mode`。 |
| 3 | 中间件的 listen-only 403 在 `_verify_auth` **之后**（`server.py:3010-3022`）。要让未鉴权的热点客户端进来，就得在它之前放行。 | 见约束 6。listen 排除必须搬进 `_setup_access()`。 |
| 4 | 守护进程是 root、服务端是 `mrrc`（`box-overlay.sh` 的 `User=mrrc`），两者要在同一个目录里各写一个文件。 | `state.json` 归守护、`wizard.json` 归服务端；目录 `0775` + group `mrrc`；`_write_json` 在 `replace()` **之前**显式 `chmod 0644`（root 的 umask 会留下 0600，服务端就读不到 ⇒ 闸门永久关闭且不报错）。 |
| 5 | `nmcli device wifi hotspot` 每次都会**新建**一个 id 叫 `Hotspot` 的 profile（NM 的 id 不唯一，uuid 才唯一），反复起停会攒出一堆同名孪生。 | `start_hotspot()` 先删**非活动**的同名 profile 再建；**活动的**那个绝不删（删了就把正在用向导的人踢下线）。任务 2 两条测试分别钉住这两个方向。 |

---

## 文件结构

| 文件 | 动作 | 职责（一个文件一件事） |
| --- | --- | --- |
| `net_wifi.py` | 创建 | **唯一**的 nmcli 适配层 + 两份邮箱文件的读写 + 免口令闸门判据。纯标准库、零应用 import、全部可注入 runner。 |
| `linux/setup_ap.py` | 创建 | `mrrc-setup-ap.service` 的常驻进程：判定 → 起/停热点 → 30 min 计时器（带"不重开"闩）→ 向导期间让位 → 每 tick 发布 `state.json`。 |
| `static/setup.html` | 创建 | 向导页（单文件自包含，内联 CSS+JS，移动优先，无外部资源）。口令 → Wi-Fi 两步，第二步在第一步完成前禁用。 |
| `server.py` | 修改 | `import net_wifi`；中间件放行 4 条闸门路径；闸门 + 审计助手；`GET /setup`；4 个向导端点；后台切换线程。 |
| `packaging/box/box-overlay.sh` | 修改 | 装机清单加 `dnsmasq`；写 `mrrc-setup-ap.service`；`systemctl enable` 加上它。 |
| `tests/test_net_wifi.py` | 创建 | 解析/判定/AP 生命周期/扫描/连网/状态文件/闸门（含 IPv4-mapped 与陈旧心跳的负例）。 |
| `tests/test_setup_ap.py` | 创建 | 守护进程决策：起/不起、**热点不算上行**、超时闩、让位、失败重开窗口（按 nonce 一次）、stale claim 被忽略。 |
| `tests/test_server_setup_wizard.py` | 创建 | 闸门开/关、listen token 被拒、口令写入的**确切键集**、"先设口令"409、切换失败**必须**回到热点并带原因、**PSK 永不落盘/进日志**。 |
| `tests/test_box_profiles.py` | 修改 | 新类 `SetupApPackagingTests`：`dnsmasq` 在装机清单里、单元被写且被 enable、单元**不得**等 `network-online.target`。 |
| `docs/W103D_GUIDE.md` | 修改 | §9.1 表格第 4 条 ⏳→✅；§9.6 从"尚未实现"改写成真实操作单（含证书告警怎么点过去）。 |
| `AGENTS.md` | 修改 | 模块表加 `net_wifi.py` 一行（`linux/setup_ap.py` 属于 `linux/` 部署脚本，写在同一行的说明里）。 |
| `tests/README.md` | 修改 | 总数与三个新模块的小节（数字取**实跑**结果，不许编）。 |
| `SDD/08-architecture-decisions.md`<br>`SDD/14-version-history.md`<br>`SDD/README.md`<br>`website/{index,zh/index,sdd,zh/sdd}.html`<br>`website/sdd/*.html` | 修改/重生成 | docs-sync：AD-026 + V2.76 + 四张手写页面的版本串与 AD 索引 + 跑 `website/build_sdd.py`。 |
| `docs/superpowers/specs/2026-10-08-w103d-setup-ap-design.md` | 修改 | D-7.5 交付表第 1 行标记为已交付，并把摸底发现 1 的更正记进去。 |

**任务分解依据**：任务 1–3 把 `net_wifi.py` 按职责分三段长出来（传输/解析/上行判定 → AP 生命周期 → 状态文件与闸门），每段都能独立测；任务 4 是它唯一的常驻调用者（守护进程）；任务 5–7 是服务端三段（闸门与页面 → 口令 → Wi-Fi 切换）；任务 8 是页面本身；任务 9–11 是打包与文档；任务 12 是构建、真机验收与变异验证。


---

## 任务 1：`net_wifi.py` — nmcli 传输、`-t` 解析、上行判定

**文件：**
- 创建：`net_wifi.py`
- 测试：`tests/test_net_wifi.py`

- [ ] **步骤 1：编写失败的测试**

创建 `tests/test_net_wifi.py`：

```python
"""The nmcli adapter: parsing, the uplink decision, and what must not count.

Everything here runs without NetworkManager, without Linux and without a radio:
a canned runner answers every call and records the argv it was handed.

The single most important assertion in this file is
`UplinkTests.test_our_own_hotspot_is_not_an_uplink`. In `nmcli device status`
an AP-mode wlan0 reports state `connected` exactly like a station does, so a
supervisor that trusts that column raises the hotspot and tears it down again
on the very next tick — a box that broadcasts an open network for a few seconds
every five seconds, forever.
"""
from __future__ import annotations

import unittest

import net_wifi
from net_wifi import NmResult


class FakeNmcli:
    """Canned nmcli.

    `table` maps a *substring of the joined argv* to a result and is scanned in
    insertion order, so put the more specific key first
    ("connection show --active" before "connection show").
    """

    def __init__(self, table=None, default=None):
        self.calls: list = []
        self.table = list((table or {}).items())
        self.default = default if default is not None else NmResult(0, "", "")

    def __call__(self, argv):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for key, value in self.table:
            if key in joined:
                return value
        return self.default

    def count(self, key: str) -> int:
        return sum(1 for call in self.calls if key in " ".join(call))

    def joined(self) -> str:
        return "\n".join(" ".join(call) for call in self.calls)


def device_rows(*rows) -> str:
    """Terse `nmcli device status` output from (device, type, state, conn)."""
    return "\n".join(":".join(row) for row in rows) + "\n"


class ParseTests(unittest.TestCase):
    def test_splits_on_unescaped_colons(self):
        self.assertEqual(net_wifi.parse_row("wlan0:wifi:connected:Home"),
                         ["wlan0", "wifi", "connected", "Home"])

    def test_keeps_an_escaped_colon_inside_a_field(self):
        """An SSID may contain ':' — nmcli escapes it, a naive split loses it
        and shifts every following column into the wrong field."""
        self.assertEqual(net_wifi.parse_row(r"Cafe\:Net:72:WPA2"),
                         ["Cafe:Net", "72", "WPA2"])

    def test_keeps_an_escaped_backslash(self):
        self.assertEqual(net_wifi.parse_row(r"back\\slash:1"), ["back\\slash", "1"])

    def test_empty_trailing_field_survives(self):
        """A device with no connection prints an empty last column."""
        self.assertEqual(net_wifi.parse_row("eth0:ethernet:unavailable:"),
                         ["eth0", "ethernet", "unavailable", ""])

    def test_rows_skips_blank_lines(self):
        self.assertEqual(net_wifi.parse_rows("a:b\n\n   \nc:d\n"),
                         [["a", "b"], ["c", "d"]])

    def test_rows_of_nothing_is_nothing(self):
        self.assertEqual(net_wifi.parse_rows(""), [])
        self.assertEqual(net_wifi.parse_rows(None), [])


class UsableUplinkTests(unittest.TestCase):
    def test_the_pretty_vocabulary_counts(self):
        self.assertTrue(net_wifi._is_usable_uplink("connected"))

    def test_the_terse_vocabulary_also_counts(self):
        """Some NM builds print `activated` where others print `connected`."""
        self.assertTrue(net_wifi._is_usable_uplink("activated"))

    def test_site_only_is_not_an_uplink(self):
        """Link-local only — a cable into a dead switch. No gateway, no DNS,
        so this is precisely the situation the hotspot exists for."""
        self.assertFalse(net_wifi._is_usable_uplink("connected (site only)"))

    def test_the_states_that_mean_no_network(self):
        for state in ("unavailable", "disconnected", "failed", "unmanaged",
                      "unknown", "prepare", "ip-config", "need-auth", ""):
            with self.subTest(state=state):
                self.assertFalse(net_wifi._is_usable_uplink(state))

    def test_case_and_padding_do_not_matter(self):
        self.assertTrue(net_wifi._is_usable_uplink("  Connected  "))

    def test_none_does_not_raise(self):
        self.assertFalse(net_wifi._is_usable_uplink(None))


class UplinkTests(unittest.TestCase):
    def test_wired_link_is_an_uplink(self):
        run = FakeNmcli({
            "connection show --active": NmResult(0, ""),
            "device status": NmResult(0, device_rows(
                ("eth0", "ethernet", "connected", "Wired connection 1"),
                ("wlan0", "wifi", "disconnected", ""))),
        })
        self.assertTrue(net_wifi.has_uplink(run))

    def test_nothing_connected_is_not_an_uplink(self):
        run = FakeNmcli({
            "connection show --active": NmResult(0, ""),
            "device status": NmResult(0, device_rows(
                ("eth0", "ethernet", "unavailable", ""),
                ("wlan0", "wifi", "disconnected", ""))),
        })
        self.assertFalse(net_wifi.has_uplink(run))

    def test_our_own_hotspot_is_not_an_uplink(self):
        """The bug this module exists to avoid (see the file docstring)."""
        run = FakeNmcli({
            "connection show --active": NmResult(0, "Hotspot:802-11-wireless\n"),
            "802-11-wireless.mode": NmResult(0, "ap\n"),
            "device status": NmResult(0, device_rows(
                ("eth0", "ethernet", "unavailable", ""),
                ("wlan0", "wifi", "connected", "Hotspot"))),
        })
        self.assertFalse(net_wifi.has_uplink(run))

    def test_a_station_wifi_is_an_uplink(self):
        run = FakeNmcli({
            "connection show --active": NmResult(0, "Home:802-11-wireless\n"),
            "802-11-wireless.mode": NmResult(0, "infrastructure\n"),
            "device status": NmResult(0, device_rows(
                ("eth0", "ethernet", "unavailable", ""),
                ("wlan0", "wifi", "connected", "Home"))),
        })
        self.assertTrue(net_wifi.has_uplink(run))

    def test_a_cable_with_no_dhcp_is_not_an_uplink(self):
        run = FakeNmcli({
            "connection show --active": NmResult(0, ""),
            "device status": NmResult(0, device_rows(
                ("eth0", "ethernet", "connected (site only)", "Wired connection 1"))),
        })
        self.assertFalse(net_wifi.has_uplink(run))

    def test_nmcli_failing_means_no_uplink_not_a_crash(self):
        run = FakeNmcli(default=NmResult(7, "", "nmcli broke"))
        self.assertFalse(net_wifi.has_uplink(run))

    def test_non_network_interfaces_are_ignored(self):
        """A loopback or a docker bridge being 'connected' says nothing about
        whether a phone can reach the box."""
        run = FakeNmcli({
            "connection show --active": NmResult(0, ""),
            "device status": NmResult(0, device_rows(
                ("lo", "loopback", "connected", "lo"),
                ("docker0", "bridge", "connected", "docker0"))),
        })
        self.assertFalse(net_wifi.has_uplink(run))


class TransitionalTests(unittest.TestCase):
    def test_still_working_counts(self):
        for state in ("prepare", "config", "need-auth", "ip-config",
                      "connecting", "deactivating"):
            with self.subTest(state=state):
                run = FakeNmcli({"device status": NmResult(
                    0, device_rows(("wlan0", "wifi", state, "")))})
                self.assertTrue(net_wifi.any_device_connecting(run))

    def test_settled_does_not_count(self):
        run = FakeNmcli({"device status": NmResult(0, device_rows(
            ("eth0", "ethernet", "unavailable", ""),
            ("wlan0", "wifi", "disconnected", "")))})
        self.assertFalse(net_wifi.any_device_connecting(run))


class GeneralStatusTests(unittest.TestCase):
    def test_running(self):
        run = FakeNmcli({"general": NmResult(0, "running\n")})
        self.assertTrue(net_wifi.nm_running(run))

    def test_not_running(self):
        run = FakeNmcli({"general": NmResult(0, "starting\n")})
        self.assertFalse(net_wifi.nm_running(run))

    def test_a_failure_is_not_running(self):
        run = FakeNmcli({"general": NmResult(7, "", "no daemon")})
        self.assertFalse(net_wifi.nm_running(run))


class DeviceQueryTests(unittest.TestCase):
    def test_the_wifi_interface_is_found_by_type_not_by_name(self):
        """A board may call it wlan1; the TYPE column is the authority."""
        run = FakeNmcli({"device status": NmResult(0, device_rows(
            ("eth0", "ethernet", "unavailable", ""),
            ("wlan1", "wifi", "disconnected", "")))})
        self.assertEqual(net_wifi.wifi_interface(run), "wlan1")

    def test_no_wifi_device_returns_empty(self):
        run = FakeNmcli({"device status": NmResult(0, device_rows(
            ("eth0", "ethernet", "connected", "wired")))})
        self.assertEqual(net_wifi.wifi_interface(run), "")


class NmResultTests(unittest.TestCase):
    def test_ok_follows_the_return_code(self):
        self.assertTrue(NmResult(0, "", "").ok)
        self.assertFalse(NmResult(1, "", "").ok)

    def test_detail_prefers_stderr_then_stdout_then_the_code(self):
        self.assertEqual(NmResult(1, "out", "err").detail, "err")
        self.assertEqual(NmResult(1, "  out\n", "").detail, "out")
        self.assertEqual(NmResult(3, "", "").detail, "exit 3")


class SubprocessRunnerTests(unittest.TestCase):
    def test_a_missing_nmcli_is_a_result_not_an_exception(self):
        """macOS and Windows have no nmcli, and this module is frozen into both
        builds, so a call must degrade instead of raising inside a handler."""
        result = net_wifi.subprocess_runner(["definitely-not-a-real-binary-xyz"])
        self.assertFalse(result.ok)
        self.assertIn("not found", result.detail.lower())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **步骤 2：运行测试验证失败**

运行：`$PY -m unittest tests.test_net_wifi -v`
预期：ERROR — `ModuleNotFoundError: No module named 'net_wifi'`

- [ ] **步骤 3：编写实现**

创建 `net_wifi.py`：

```python
#!/usr/bin/env python3
"""NetworkManager adapter for the W103D setup access point (design D-2/D-3/D-4).

Why this module exists
----------------------
A freshly flashed box may have no cable, no keyboard and no saved WiFi, which
makes it unreachable — while configuring it needs it to be reachable. The box
answers by opening its own hotspot. This module is the only code in the
repository that talks to ``nmcli`` about that (design D-4: the WiFi domain has
exactly one writer, and it is *not* the ``MRRC_*`` env file, so a WiFi
credential can never meet a config key).

Three rules, all load-bearing
-----------------------------
1. **Stdlib only, no application imports.** ``server.py`` imports this, and
   PyInstaller's root-module analysis therefore freezes it into *every*
   desktop build. Windows and macOS have no ``nmcli``, so the import has to
   succeed and every call has to degrade to a plain failure result rather than
   raise inside a request handler. For the same reason ``grp`` and
   ``os.chown`` (POSIX-only) are imported lazily inside the one function that
   uses them.
2. **A WiFi password never leaves this module's argv.** Not into a log line,
   not into the env file, not into either JSON mailbox (SDD
   ``support-bundle-privacy``, AD-021 / NFR-068). ``scrub()`` exists for the
   one place a secret could still ride along: nmcli's own error text.
3. **Everything that shells out takes an injectable ``runner``**, so the whole
   module is testable without NetworkManager, without Linux and without a
   radio.

Two nmcli facts that cost real debugging time
---------------------------------------------
* In ``nmcli device status`` an **AP-mode** wlan0 reports state ``connected``
  exactly like a station does, and ``connection show --active`` reports TYPE
  ``802-11-wireless`` for both. Our own hotspot is therefore *not* an uplink,
  and ``has_uplink()`` subtracts it — otherwise the supervisor raises the
  hotspot and tears it down again on the next tick, forever.
* NM prints the state as ``connected`` in some builds and ``activated`` in
  others, and as ``connected (site only)`` when there is link-local only (a
  cable into a dead switch) — which is *not* a usable uplink. Guessing one
  spelling is how a box ends up with no hotspot when it should have one, or
  with an open hotspot when it should not.
"""
from __future__ import annotations

import ipaddress
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

# ── constants ───────────────────────────────────────────────────────
DEFAULT_IFNAME = "wlan0"
DEFAULT_SSID = "MRRC-Setup"
DEFAULT_WEB_PORT = 8888
DEFAULT_STATE_DIR = Path("/run/mrrc/setup-ap")

STATE_NAME = "state.json"       # written by linux/setup_ap.py (root) only
WIZARD_NAME = "wizard.json"     # written by server.py (user mrrc) only

# The connection id `nmcli device wifi hotspot` gives the profile it creates.
# NM ids are not unique (uuids are), so every raise would leave one more twin
# behind; start_hotspot() removes an *inactive* profile of this name first.
HOTSPOT_PROFILE = "Hotspot"

# NetworkManager's `ipv4.method=shared` default subnet. The gate trusts the
# value read back from the running device and falls back to this one.
NM_SHARED_SUBNET = "10.42.0.0/24"
DEFAULT_GATEWAY = "10.42.0.1"

MODE_OFF = "off"
MODE_HOTSPOT = "hotspot"
MODE_STA = "sta"

WIZARD_SWITCHING = "switching"
WIZARD_OK = "ok"
WIZARD_FAILED = "failed"

# A supervisor that stopped heartbeating must not keep the passwordless window
# open (design D-6: the gate is a *live* network path, not a stored flag).
HEARTBEAT_MAX_AGE_S = 45.0
# A finished claim ("ok"/"failed") stays actionable long enough for the
# supervisor to react to it, then expires so normal evaluation resumes.
CLAIM_MAX_AGE_S = 600.0

NMCLI_TIMEOUT_S = 60.0
REDACTED = "<redacted>"
STATE_GROUP = "mrrc"

# "Has a network", in NM's two vocabularies.
CONNECTED_WORDS = ("connected", "activated")
# "Still trying" — i.e. a `has_uplink() == False` that is not yet a final answer.
TRANSITIONAL_STATES = (
    "prepare", "config", "need-auth", "need authentication", "ip-config",
    "ip-check", "secondaries", "connecting", "activating", "deactivating",
)
# Which interface types can carry the box's uplink.
UPLINK_TYPES = ("ethernet", "wifi")


# ── the nmcli transport ─────────────────────────────────────────────
@dataclass(frozen=True)
class NmResult:
    """One nmcli invocation's outcome. Never an exception."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def detail(self) -> str:
        """The most informative single-line reason, for logs and for the page."""
        for text in (self.stderr, self.stdout):
            cleaned = " ".join((text or "").split())
            if cleaned:
                return cleaned
        return f"exit {self.returncode}"


Runner = Callable[[list], NmResult]


def subprocess_runner(argv: list) -> NmResult:
    """The real runner. A missing nmcli or a timeout is a result, not a raise."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=NMCLI_TIMEOUT_S)
    except FileNotFoundError:
        return NmResult(127, "", "nmcli not found")
    except OSError as exc:
        return NmResult(126, "", f"could not run nmcli: {exc}")
    except subprocess.TimeoutExpired:
        return NmResult(124, "", f"nmcli timed out after {NMCLI_TIMEOUT_S:.0f}s")
    return NmResult(proc.returncode, proc.stdout or "", proc.stderr or "")


def nmcli(args: list, runner: Optional[Runner] = None) -> NmResult:
    """Run one nmcli command through the injectable runner."""
    return (runner or subprocess_runner)(["nmcli", *[str(a) for a in args]])


# ── `-t` output parsing ─────────────────────────────────────────────
def parse_row(line: str) -> list:
    """Split one ``nmcli -t`` row on *unescaped* colons.

    NM escapes ``:`` and ``\\`` with a backslash in terse mode, so an SSID
    containing a colon survives. A plain ``line.split(":")`` would shift every
    following column into the wrong field — a wrong SIGNAL is cosmetic, a wrong
    SECURITY column tells the wizard to ask for a password an open network
    does not have.
    """
    out: list = []
    buf: list = []
    escaped = False
    for char in (line or ""):
        if escaped:
            buf.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            out.append("".join(buf))
            buf = []
        else:
            buf.append(char)
    out.append("".join(buf))
    return out


def parse_rows(text: Optional[str]) -> list:
    """Every non-blank row of a terse listing."""
    return [parse_row(line) for line in (text or "").splitlines() if line.strip()]


def _int_or(value, default: int = 0) -> int:
    """Coerce an untyped nmcli/env field to int; never raises."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


# ── what counts as "has a network" ──────────────────────────────────
def _is_usable_uplink(state: Optional[str]) -> bool:
    """Whether an interface state means real reachability (see module docstring)."""
    text = (state or "").strip().lower()
    if "site only" in text:
        return False
    return any(text.startswith(word) for word in CONNECTED_WORDS)


def devices(runner: Optional[Runner] = None) -> list:
    """``nmcli device status`` as a list of dicts; empty when nmcli fails."""
    result = nmcli(["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION",
                    "device", "status"], runner)
    if not result.ok:
        return []
    out = []
    for row in parse_rows(result.stdout):
        if len(row) < 3 or not row[0]:
            continue
        out.append({
            "device": row[0],
            "type": row[1].strip().lower(),
            "state": row[2],
            "connection": row[3] if len(row) > 3 else "",
        })
    return out


def any_device_connecting(runner: Optional[Runner] = None) -> bool:
    """Whether NM is still working, so "no uplink" is not yet a final answer."""
    return any((entry["state"] or "").strip().lower() in TRANSITIONAL_STATES
               for entry in devices(runner))


def has_uplink(runner: Optional[Runner] = None) -> bool:
    """Whether the box can already be reached (design D-6's criterion).

    Our own hotspot is subtracted: it reports ``connected`` too.
    """
    ap_names = set(active_ap_connections(runner))
    for entry in devices(runner):
        if entry["type"] not in UPLINK_TYPES:
            continue
        if not _is_usable_uplink(entry["state"]):
            continue
        if entry["connection"] and entry["connection"] in ap_names:
            continue
        return True
    return False


def nm_running(runner: Optional[Runner] = None) -> bool:
    """Whether the NetworkManager daemon answers at all."""
    result = nmcli(["-t", "-f", "RUNNING", "general"], runner)
    return result.ok and result.stdout.strip().lower() == "running"


def wifi_interface(runner: Optional[Runner] = None) -> str:
    """The WiFi device name, by type rather than by an assumed ``wlan0``."""
    for entry in devices(runner):
        if entry["type"] == "wifi" and entry["device"]:
            return entry["device"]
    return ""


def _connection_is_ap(name: str, runner: Optional[Runner] = None) -> bool:
    """Whether one connection's ``802-11-wireless.mode`` is ``ap``."""
    if not name:
        return False
    result = nmcli(["-t", "-g", "802-11-wireless.mode", "connection", "show", name],
                   runner)
    return result.ok and result.stdout.strip().lower() == "ap"


def active_ap_connections(runner: Optional[Runner] = None) -> list:
    """Names of the active connections that are access points.

    ``connection show --active`` reports TYPE ``802-11-wireless`` for a station
    and for an AP alike, so the mode has to be asked for separately. Getting
    this wrong is what makes the box count its own hotspot as an uplink — which
    is why this lives with the uplink decision rather than with the AP lifecycle.
    """
    result = nmcli(["-t", "-f", "NAME,TYPE", "connection", "show", "--active"],
                   runner)
    if not result.ok:
        return []
    names = [row[0] for row in parse_rows(result.stdout)
             if len(row) >= 2 and row[1].strip() == "802-11-wireless" and row[0]]
    return [name for name in names if _connection_is_ap(name, runner)]
```

> **执行时的更正（2026-10-08）**：本计划原先把 `_connection_is_ap` / `active_ap_connections`
> 排在**任务 2**（"AP 生命周期"），并让任务 1 先放一个返回 `[]` 的占位。
> **这个边界是错的**，实跑证实：占位会让任务 1 里**最重要**的那条测试
> （`test_our_own_hotspot_is_not_an_uplink`）直接变红——因为 `has_uplink()` 减掉的
> `ap_names` 永远是空集，自己的热点就被算成了上行。
> "什么是上行"这个问题**本身就包含**"哪个连接是 AP"，所以这两个函数属于任务 1，
> 任务 2 只加**剩下的** AP 生命周期与 station 侧函数。
> 占位式的"最少实现"只有在它**不改变被测行为**时才成立；这里它恰好改变的就是被测行为。

- [ ] **步骤 4：运行测试验证通过**

运行：`$PY -m unittest tests.test_net_wifi -v`
预期：`OK`（29 项）

若 `test_our_own_hotspot_is_not_an_uplink` 红：说明 `has_uplink()` 忘了减掉 `ap_names`，
或者 `active_ap_connections()` 没按 `802-11-wireless.mode` 区分 AP 与 STA。

- [ ] **步骤 5：确认没有弄坏别的**

运行：`$PY -m unittest discover -s tests 2>&1 | tail -4`
预期：`FAILED (failures=1, skipped=1)`，唯一 `FAIL:` 是 `test_tls_trust_store`

- [ ] **步骤 6：SDD 检查 + Commit**

```bash
$PY .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git add net_wifi.py tests/test_net_wifi.py
git commit -m "feat: net_wifi 的 nmcli 传输与上行判定——自己的热点不算上行"
```


---

## 任务 2：`net_wifi.py` — 热点生命周期、扫描、连网、地址

**文件：**
- 修改：`net_wifi.py`（在任务 1 的 `active_ap_connections()` 之后追加其余函数）
- 测试：`tests/test_net_wifi.py`（在 `SubprocessRunnerTests` **之前**追加）

- [ ] **步骤 1：编写失败的测试**

```python
# `FakeNmcli` matches by substring, and both of these queries contain
# "connection show --active", so the keys carry the `-f` column list to keep
# them apart: `_active_uuids` asks for UUID, `active_ap_connections` for NAME,TYPE.
ACTIVE_AP_TABLE = {
    "-f NAME,TYPE connection show --active": NmResult(0, "Hotspot:802-11-wireless\n"),
    "-f UUID connection show --active": NmResult(0, "aaaa-1111\n"),
    "802-11-wireless.mode": NmResult(0, "ap\n"),
}
ACTIVE_STA_TABLE = {
    "-f NAME,TYPE connection show --active": NmResult(0, "Home:802-11-wireless\n"),
    "-f UUID connection show --active": NmResult(0, "bbbb-2222\n"),
    "802-11-wireless.mode": NmResult(0, "infrastructure\n"),
}


class ApModeTests(unittest.TestCase):
    def test_an_ap_connection_is_recognised(self):
        run = FakeNmcli(ACTIVE_AP_TABLE)
        self.assertEqual(net_wifi.active_ap_connections(run), ["Hotspot"])
        self.assertTrue(net_wifi.hotspot_active(run))

    def test_a_station_connection_is_not_an_ap(self):
        """TYPE is 802-11-wireless for both; only the mode tells them apart."""
        run = FakeNmcli(ACTIVE_STA_TABLE)
        self.assertEqual(net_wifi.active_ap_connections(run), [])
        self.assertFalse(net_wifi.hotspot_active(run))

    def test_nothing_active(self):
        run = FakeNmcli({"-f NAME,TYPE connection show --active": NmResult(0, "")})
        self.assertFalse(net_wifi.hotspot_active(run))

    def test_an_empty_connection_name_is_not_queried(self):
        """A row whose NAME column is blank must not trigger a mode lookup with
        an empty name — that asks NM for a connection called "" and fails."""
        run = FakeNmcli({
            "-f NAME,TYPE connection show --active": NmResult(
                0, ":802-11-wireless\n"),
        })
        self.assertEqual(net_wifi.active_ap_connections(run), [])
        self.assertEqual(run.count("802-11-wireless.mode"), 0)


class StartHotspotTests(unittest.TestCase):
    def test_asks_nmcli_for_an_open_hotspot(self):
        """Design D-2: `nmcli device wifi hotspot`, and *no* password argument.

        Omitting `password` is what makes the network open; passing one would
        create a WPA network whose key nobody has been told.
        """
        run = FakeNmcli({
            "-f NAME,TYPE connection show --active": NmResult(0, ""),
            "-f UUID connection show --active": NmResult(0, ""),
            "-f UUID,NAME connection show": NmResult(0, ""),
            "device wifi hotspot": NmResult(0, "ok", ""),
        })
        result = net_wifi.start_hotspot("MRRC-Setup", "wlan0", run)
        self.assertTrue(result.ok)
        self.assertEqual(run.count("device wifi hotspot"), 1)
        self.assertIn("ifname wlan0 ssid MRRC-Setup", run.joined())
        self.assertNotIn("password", run.joined())

    def test_an_already_running_hotspot_is_left_alone(self):
        """Restarting a live AP would drop the operator mid-wizard."""
        run = FakeNmcli(dict(ACTIVE_AP_TABLE))
        result = net_wifi.start_hotspot("MRRC-Setup", "wlan0", run)
        self.assertTrue(result.ok)
        self.assertEqual(run.count("device wifi hotspot"), 0)

    def test_a_stale_profile_of_the_same_name_is_removed_first(self):
        """NM ids are not unique: without this, every raise leaves a twin behind
        and `nmcli connection show` fills up with dead Hotspot entries."""
        run = FakeNmcli({
            "-f NAME,TYPE connection show --active": NmResult(0, ""),
            "-f UUID connection show --active": NmResult(0, "live-9999\n"),
            "-f UUID,NAME connection show": NmResult(
                0, "aaaa-1111:Hotspot\nbbbb-2222:Hotspot\ncccc-3333:Home\n"),
            "device wifi hotspot": NmResult(0, "ok", ""),
        })
        net_wifi.start_hotspot("MRRC-Setup", "wlan0", run)
        self.assertEqual(run.count("connection delete aaaa-1111"), 1)
        self.assertEqual(run.count("connection delete bbbb-2222"), 1)
        self.assertEqual(run.count("connection delete cccc-3333"), 0,
                         "only the hotspot's own id may be touched")

    def test_an_active_profile_is_never_deleted(self):
        """Deleting the profile that carries the live AP cuts the operator off.
        The early return has to come first — and it has to come before the
        profile listing, not merely before the delete."""
        run = FakeNmcli(dict(ACTIVE_AP_TABLE, **{
            "-f UUID,NAME connection show": NmResult(0, "aaaa-1111:Hotspot\n"),
            "device wifi hotspot": NmResult(0, "ok", ""),
        }))
        net_wifi.start_hotspot("MRRC-Setup", "wlan0", run)
        self.assertEqual(run.count("connection delete"), 0)
        self.assertEqual(run.count("-f UUID,NAME connection show"), 0)

    def test_a_profile_that_is_up_under_a_different_query_is_still_spared(self):
        """Belt and braces: even if the AP check somehow missed it, a uuid that
        `_active_uuids` reports as live is never deleted."""
        run = FakeNmcli({
            "-f NAME,TYPE connection show --active": NmResult(0, ""),
            "-f UUID connection show --active": NmResult(0, "aaaa-1111\n"),
            "-f UUID,NAME connection show": NmResult(
                0, "aaaa-1111:Hotspot\nbbbb-2222:Hotspot\n"),
            "device wifi hotspot": NmResult(0, "ok", ""),
        })
        net_wifi.start_hotspot("MRRC-Setup", "wlan0", run)
        self.assertEqual(run.count("connection delete aaaa-1111"), 0)
        self.assertEqual(run.count("connection delete bbbb-2222"), 1)

    def test_a_failure_is_reported_not_raised(self):
        run = FakeNmcli({
            "-f NAME,TYPE connection show --active": NmResult(0, ""),
            "-f UUID connection show --active": NmResult(0, ""),
            "-f UUID,NAME connection show": NmResult(0, ""),
            "device wifi hotspot": NmResult(
                1, "", "Error: Device not suitable for hotspot mode"),
        })
        result = net_wifi.start_hotspot("MRRC-Setup", "wlan0", run)
        self.assertFalse(result.ok)
        self.assertIn("not suitable", result.detail)

    def test_the_defaults_are_used_when_nothing_is_passed(self):
        run = FakeNmcli({
            "-f NAME,TYPE connection show --active": NmResult(0, ""),
            "-f UUID connection show --active": NmResult(0, ""),
            "-f UUID,NAME connection show": NmResult(0, ""),
            "device wifi hotspot": NmResult(0, "ok", ""),
        })
        net_wifi.start_hotspot(runner=run)
        self.assertIn(f"ifname {net_wifi.DEFAULT_IFNAME} ssid {net_wifi.DEFAULT_SSID}",
                      run.joined())


class StopHotspotTests(unittest.TestCase):
    def test_takes_the_ap_down_by_name(self):
        run = FakeNmcli(dict(ACTIVE_AP_TABLE))
        result = net_wifi.stop_hotspot(run)
        self.assertTrue(result.ok)
        self.assertEqual(run.count("connection down Hotspot"), 1)

    def test_nothing_up_is_success(self):
        """Idempotent: a stop on an already-stopped AP must not look like an
        error, or the supervisor would log a failure on every tick."""
        run = FakeNmcli({"-f NAME,TYPE connection show --active": NmResult(0, "")})
        self.assertTrue(net_wifi.stop_hotspot(run).ok)
        self.assertEqual(run.count("connection down"), 0)

    def test_a_station_connection_is_not_torn_down(self):
        """Stopping the hotspot must never drop the operator's real WiFi."""
        run = FakeNmcli(dict(ACTIVE_STA_TABLE))
        net_wifi.stop_hotspot(run)
        self.assertEqual(run.count("connection down"), 0)


class ScanTests(unittest.TestCase):
    LISTING = "\n".join([
        r"Home\:5G:82:WPA2",     # an SSID containing a colon, escaped by nmcli
        "Office:41:WPA2",
        "Guest::",               # open network: empty SECURITY
        ":60:WPA2",              # hidden network: empty SSID
        r"Home\:5G:55:WPA2",    # the same SSID again, weaker
    ]) + "\n"

    def test_parses_dedupes_and_sorts_by_strength(self):
        run = FakeNmcli({"device wifi list": NmResult(0, self.LISTING)})
        found = net_wifi.scan_wifi(run)
        self.assertEqual([n["ssid"] for n in found], ["Home:5G", "Office", "Guest"])
        self.assertEqual(found[0]["signal"], 82,
                         "the stronger of two beacons for one SSID wins")

    def test_hidden_networks_are_dropped(self):
        """The wizard cannot offer a network it cannot name; an empty row would
        be a button that does nothing."""
        run = FakeNmcli({"device wifi list": NmResult(0, self.LISTING)})
        self.assertTrue(all(n["ssid"] for n in net_wifi.scan_wifi(run)))
        self.assertEqual(len(net_wifi.scan_wifi(run)), 3)

    def test_an_open_network_is_marked_unprotected(self):
        run = FakeNmcli({"device wifi list": NmResult(0, self.LISTING)})
        by_ssid = {n["ssid"]: n for n in net_wifi.scan_wifi(run)}
        self.assertFalse(by_ssid["Guest"]["protected"])
        self.assertTrue(by_ssid["Office"]["protected"])
        self.assertEqual(by_ssid["Office"]["security"], "WPA2")

    def test_a_scan_asks_for_a_rescan(self):
        """The AP just came up, so NM's cache is empty; without --rescan the
        operator sees a blank list and concludes the radio is broken."""
        run = FakeNmcli({"device wifi list": NmResult(0, "")})
        net_wifi.scan_wifi(run)
        self.assertIn("--rescan yes", run.joined())

    def test_a_rescan_can_be_suppressed(self):
        run = FakeNmcli({"device wifi list": NmResult(0, "")})
        net_wifi.scan_wifi(run, rescan=False)
        self.assertNotIn("--rescan", run.joined())

    def test_a_failure_yields_an_empty_list(self):
        run = FakeNmcli({"device wifi list": NmResult(1, "", "no device")})
        self.assertEqual(net_wifi.scan_wifi(run), [])


class ConnectTests(unittest.TestCase):
    def test_argv_carries_the_ssid_and_the_psk_exactly_once(self):
        run = FakeNmcli({"device wifi connect": NmResult(0, "", "")})
        net_wifi.connect_wifi("Home", "hunter2hunter2", run, ifname="wlan0")
        joined = run.joined()
        self.assertEqual(joined.count("hunter2hunter2"), 1)
        self.assertIn("device wifi connect Home password hunter2hunter2", joined)
        self.assertIn("ifname wlan0", joined)

    def test_an_open_network_passes_no_password_argument(self):
        run = FakeNmcli({"device wifi connect": NmResult(0, "", "")})
        net_wifi.connect_wifi("Guest", "", run, ifname="wlan0")
        self.assertNotIn("password", run.joined())

    def test_a_wrong_psk_comes_back_as_a_failure_with_a_reason(self):
        run = FakeNmcli({"device wifi connect": NmResult(
            1, "", "Error: Connection activation failed: (7) Secrets were required")})
        result = net_wifi.connect_wifi("Home", "wrongwrong", run)
        self.assertFalse(result.ok)
        self.assertIn("Secrets were required", result.detail)

    def test_the_default_interface_is_used_when_none_is_given(self):
        run = FakeNmcli({"device wifi connect": NmResult(0, "", "")})
        net_wifi.connect_wifi("Home", "", run)
        self.assertIn(f"ifname {net_wifi.DEFAULT_IFNAME}", run.joined())


class AddressTests(unittest.TestCase):
    def test_reads_the_interface_address_with_its_prefix(self):
        run = FakeNmcli({"IP4.ADDRESS": NmResult(0, "10.42.0.1/24\n")})
        self.assertEqual(net_wifi.ipv4_address("wlan0", run), "10.42.0.1/24")

    def test_takes_the_first_line_when_several_come_back(self):
        run = FakeNmcli({"IP4.ADDRESS": NmResult(0, "10.0.0.7/24\n10.0.0.8/24\n")})
        self.assertEqual(net_wifi.ipv4_address("wlan0", run), "10.0.0.7/24")

    def test_no_address_yet(self):
        run = FakeNmcli({"IP4.ADDRESS": NmResult(0, "\n")})
        self.assertEqual(net_wifi.ipv4_address("wlan0", run), "")

    def test_a_failure_is_an_empty_address(self):
        run = FakeNmcli({"IP4.ADDRESS": NmResult(1, "", "no such device")})
        self.assertEqual(net_wifi.ipv4_address("wlan9", run), "")

    def test_network_of_an_address(self):
        self.assertEqual(net_wifi.ap_network("10.42.0.1/24"), "10.42.0.0/24")

    def test_garbage_falls_back_to_the_nm_shared_subnet(self):
        """The gate compares against this; an unparseable address must not turn
        into an exception inside a request handler."""
        self.assertEqual(net_wifi.ap_network(""), net_wifi.NM_SHARED_SUBNET)
        self.assertEqual(net_wifi.ap_network("not-an-address"),
                         net_wifi.NM_SHARED_SUBNET)
        self.assertEqual(net_wifi.ap_network(None), net_wifi.NM_SHARED_SUBNET)

    def test_setup_url_strips_the_prefix_and_defaults_the_gateway(self):
        self.assertEqual(net_wifi.setup_url("10.42.0.1/24", 8888),
                         "https://10.42.0.1:8888/setup")
        self.assertEqual(net_wifi.setup_url("", 8888),
                         f"https://{net_wifi.DEFAULT_GATEWAY}:8888/setup")
        self.assertEqual(net_wifi.setup_url("192.168.9.1/24"),
                         "https://192.168.9.1:8888/setup")


class ScrubTests(unittest.TestCase):
    def test_removes_every_occurrence_of_every_secret(self):
        text = "tried hunter2hunter2 then failed (psk=hunter2hunter2)"
        cleaned = net_wifi.scrub(text, "hunter2hunter2")
        self.assertNotIn("hunter2hunter2", cleaned)
        self.assertEqual(cleaned.count(net_wifi.REDACTED), 2)

    def test_an_empty_secret_cannot_blank_the_whole_string(self):
        """str.replace(x, "") with x == "" would rebuild the string between
        every character; the guard is the `if secret`."""
        self.assertEqual(net_wifi.scrub("keep me", ""), "keep me")

    def test_none_is_tolerated(self):
        self.assertEqual(net_wifi.scrub(None, "x"), "")

    def test_several_secrets(self):
        cleaned = net_wifi.scrub("a=one b=two", "one", "two")
        self.assertNotIn("one", cleaned)
        self.assertNotIn("two", cleaned)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`$PY -m unittest tests.test_net_wifi -v 2>&1 | tail -10`
预期：一片 FAIL/ERROR — `AttributeError: module 'net_wifi' has no attribute 'hotspot_active'`（以及 `start_hotspot` / `stop_hotspot` / `scan_wifi` / `connect_wifi` / `ipv4_address` / `ap_network` / `setup_url` / `scrub`）。

> `ApModeTests`（认 AP / 不认 STA / 空名字不多问一次）在**任务 1 就已经绿了**，因为
> `active_ap_connections` 已经在那里实现。把它放在任务 2 的测试文件里只是因为它是
> "AP 生命周期"这一组语义的一部分；**不要**因为它已经绿了就删掉它——它是任务 1 那条
> 上行判定的直接下游，两处都需要它钉着。

- [ ] **步骤 3：编写实现**

在 `net_wifi.py` 里紧跟任务 1 的 `active_ap_connections()` 之后追加（`_connection_is_ap`
与 `active_ap_connections` **已经在任务 1 写好了**，这里不要重写）：

```python
# ── AP lifecycle ────────────────────────────────────────────────────
def _active_uuids(runner: Optional[Runner] = None) -> set:
    """UUIDs of the connections that are up right now."""
    result = nmcli(["-t", "-f", "UUID", "connection", "show", "--active"], runner)
    if not result.ok:
        return set()
    return {row[0] for row in parse_rows(result.stdout) if row and row[0]}


def _profiles_named(name: str, runner: Optional[Runner] = None) -> list:
    """``[(uuid, name), …]`` for every profile carrying this id (ids repeat)."""
    result = nmcli(["-t", "-f", "UUID,NAME", "connection", "show"], runner)
    if not result.ok:
        return []
    return [(row[0], row[1]) for row in parse_rows(result.stdout)
            if len(row) >= 2 and row[1] == name]


def hotspot_active(runner: Optional[Runner] = None) -> bool:
    """Whether an AP is up on this box right now."""
    return bool(active_ap_connections(runner))


def start_hotspot(ssid: Optional[str] = None, ifname: Optional[str] = None,
                  runner: Optional[Runner] = None) -> NmResult:
    """Raise an **open** hotspot (design D-2: ``nmcli device wifi hotspot``).

    Idempotent in the two ways that matter:
    * an AP that is already up is left alone — tearing it down and rebuilding
      it would drop the operator in the middle of the wizard;
    * a *stale* profile of the same id is deleted first, because NM ids are not
      unique and every raise would otherwise leave one more twin behind. A
      profile that is currently active is never deleted.

    No ``password`` argument is passed. That omission *is* the open network:
    there is no way to hand the operator a key they have not been told.
    """
    if hotspot_active(runner):
        return NmResult(0, "already up", "")
    live = _active_uuids(runner)
    for uuid, _name in _profiles_named(HOTSPOT_PROFILE, runner):
        if uuid in live:
            continue
        nmcli(["connection", "delete", uuid], runner)
    return nmcli(["device", "wifi", "hotspot",
                  "ifname", ifname or DEFAULT_IFNAME,
                  "ssid", ssid or DEFAULT_SSID], runner)


def stop_hotspot(runner: Optional[Runner] = None) -> NmResult:
    """Take the AP down. Idempotent: nothing up is success, not an error.

    Only AP-mode connections are considered, so this can never drop the
    operator's real WiFi.
    """
    names = active_ap_connections(runner)
    if not names:
        return NmResult(0, "not up", "")
    last = NmResult(0, "not up", "")
    for name in names:
        last = nmcli(["connection", "down", name], runner)
    return last


# ── station side ────────────────────────────────────────────────────
def scan_wifi(runner: Optional[Runner] = None, rescan: bool = True) -> list:
    """Visible networks, strongest first, one entry per SSID.

    Hidden networks arrive with an empty SSID and are dropped. ``rescan`` is on
    by default because the AP has just taken the radio over: NM's cache is
    whatever it saw before, and an empty list reads as "the radio is broken".
    """
    args = ["-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list"]
    if rescan:
        args += ["--rescan", "yes"]
    result = nmcli(args, runner)
    if not result.ok:
        return []
    best: dict = {}
    for row in parse_rows(result.stdout):
        if len(row) < 3 or not row[0]:
            continue
        entry = {
            "ssid": row[0],
            "signal": _int_or(row[1]),
            "security": row[2].strip(),
            "protected": bool(row[2].strip()),
        }
        previous = best.get(entry["ssid"])
        if previous is None or entry["signal"] > previous["signal"]:
            best[entry["ssid"]] = entry
    return sorted(best.values(), key=lambda item: -item["signal"])


def connect_wifi(ssid: str, password: str = "",
                 runner: Optional[Runner] = None,
                 ifname: Optional[str] = None) -> NmResult:
    """Join an infrastructure network.

    Single radio (design D-3): this *replaces* the hotspot, it does not run
    beside it. The caller owns the ordering — see
    ``server._perform_wifi_switch``.

    ``password`` travels in argv and nowhere else. It is never logged, never
    written to the env file (WiFi is NetworkManager's domain, design D-4) and
    never written to either JSON mailbox.
    """
    args = ["device", "wifi", "connect", ssid]
    if password:
        args += ["password", password]
    args += ["ifname", ifname or DEFAULT_IFNAME]
    return nmcli(args, runner)


def ipv4_address(ifname: Optional[str] = None,
                 runner: Optional[Runner] = None) -> str:
    """The interface's IPv4 address with its prefix, or ``""`` (10.42.0.1/24)."""
    result = nmcli(["-t", "-g", "IP4.ADDRESS", "device", "show",
                    ifname or DEFAULT_IFNAME], runner)
    if not result.ok:
        return ""
    for line in result.stdout.splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned
    return ""


def ap_network(address: Optional[str]) -> str:
    """``10.42.0.1/24`` → ``10.42.0.0/24``: the subnet the gate trusts."""
    try:
        return str(ipaddress.ip_network((address or "").strip(), strict=False))
    except ValueError:
        return NM_SHARED_SUBNET


def setup_url(gateway: str, port: Optional[int] = None) -> str:
    """The address to print on the HDMI console and to show on the page."""
    host = (gateway or "").strip().split("/")[0] or DEFAULT_GATEWAY
    return f"https://{host}:{port or DEFAULT_WEB_PORT}/setup"


def scrub(text: Optional[str], *secrets: str) -> str:
    """Remove every literal secret from a string that is about to be stored.

    nmcli does not echo a PSK back today, but its error text is the one place a
    credential could ride along into ``wizard.json`` and from there into a
    support bundle — so the removal is unconditional rather than trusted
    (SDD ``support-bundle-privacy``).
    """
    out = text or ""
    for secret in secrets:
        if secret:
            out = out.replace(secret, REDACTED)
    return out
```

- [ ] **步骤 4：运行测试验证通过**

运行：`$PY -m unittest tests.test_net_wifi -v 2>&1 | tail -6`
预期：`OK`（约 65 项）

排障提示：
- 任何"delete 计数不对"的红 ⇒ 先看 `FakeNmcli` 的表键。两条查询都含 `connection show --active`，所以键**必须**带上 `-f` 的列名（`-f UUID …` vs `-f NAME,TYPE …` vs `-f UUID,NAME …`）才能分开；用裸 `connection show --active` 当键会让两条查询拿到同一份假输出。
- `test_an_active_profile_is_never_deleted` 红 ⇒ `start_hotspot()` 里 `hotspot_active()` 的早退必须在删除循环**之前**。

- [ ] **步骤 5：确认没有弄坏别的 + Commit**

```bash
$PY -m unittest discover -s tests 2>&1 | tail -4     # 仍只有 test_tls_trust_store 一条红
$PY .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git add net_wifi.py tests/test_net_wifi.py
git commit -m "feat: net_wifi 的 AP 生命周期、扫描与连网——开放热点、幂等起停、PSK 只进 argv"
```


---

## 任务 3：`net_wifi.py` — 配置、两份邮箱文件、免口令闸门

**文件：**
- 修改：`net_wifi.py`（在 `scrub()` 之后追加）
- 测试：`tests/test_net_wifi.py`（在 `SubprocessRunnerTests` **之前**追加；并在文件顶部 import 区加 `import json`、`import os`、`import tempfile`、`from pathlib import Path`）

**为什么是两个文件、两个写入者**：守护进程是 root，服务端是 `mrrc`。各写各的文件就没有并发写者，也就不需要锁；目录 `0775` + group `mrrc` 让两边都能在自己的文件上落笔（摸底发现 4）。`state.json` 是闸门的**依据**，`wizard.json` 是服务端对射频的**占用声明**。

- [ ] **步骤 1：编写失败的测试**

```python
class SettingsTests(unittest.TestCase):
    def test_defaults(self):
        cfg = net_wifi.ap_settings({})
        self.assertEqual(cfg["ssid"], net_wifi.DEFAULT_SSID)
        self.assertEqual(cfg["ifname"], net_wifi.DEFAULT_IFNAME)
        self.assertEqual(cfg["web_port"], net_wifi.DEFAULT_WEB_PORT)
        self.assertEqual(cfg["state_dir"], net_wifi.DEFAULT_STATE_DIR)

    def test_environment_overrides(self):
        cfg = net_wifi.ap_settings({
            "MRRC_SETUP_AP_SSID": "My-Box",
            "MRRC_SETUP_AP_IFNAME": "wlan1",
            "MRRC_SETUP_AP_STATE_DIR": "/tmp/x",
            "MRRC_WEB_PORT": "9999",
        })
        self.assertEqual(cfg["ssid"], "My-Box")
        self.assertEqual(cfg["ifname"], "wlan1")
        self.assertEqual(cfg["state_dir"], Path("/tmp/x"))
        self.assertEqual(cfg["web_port"], 9999)

    def test_the_web_port_can_be_overridden_on_its_own(self):
        cfg = net_wifi.ap_settings({"MRRC_WEB_PORT": "8888",
                                    "MRRC_SETUP_AP_WEB_PORT": "9001"})
        self.assertEqual(cfg["web_port"], 9001)

    def test_an_empty_value_falls_back_to_the_default(self):
        cfg = net_wifi.ap_settings({"MRRC_SETUP_AP_SSID": "   ",
                                    "MRRC_SETUP_AP_STATE_DIR": ""})
        self.assertEqual(cfg["ssid"], net_wifi.DEFAULT_SSID)
        self.assertEqual(cfg["state_dir"], net_wifi.DEFAULT_STATE_DIR)

    def test_a_non_numeric_port_falls_back(self):
        self.assertEqual(net_wifi.ap_settings({"MRRC_WEB_PORT": "abc"})["web_port"],
                         net_wifi.DEFAULT_WEB_PORT)

    def test_it_reads_the_process_environment_by_default(self):
        """The server and the supervisor both call this with no argument, so the
        default has to be os.environ and not an empty mapping."""
        import unittest.mock as mock
        with mock.patch.dict(os.environ, {"MRRC_SETUP_AP_SSID": "FromEnv"},
                             clear=False):
            self.assertEqual(net_wifi.ap_settings()["ssid"], "FromEnv")


class StateFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_round_trip(self):
        state = net_wifi.ApState(mode=net_wifi.MODE_HOTSPOT, ssid="MRRC-Setup",
                                 gateway="10.42.0.1/24", network="10.42.0.0/24",
                                 url="https://10.42.0.1:8888/setup",
                                 reason="", since=100.0, heartbeat=200.0,
                                 deadline=300.0)
        net_wifi.write_state(state, self.dir)
        self.assertEqual(net_wifi.read_state(self.dir), state)

    def test_a_missing_file_reads_as_off(self):
        """No supervisor, no file ⇒ the gate must be closed, not an exception.
        This is the state of every desktop install."""
        state = net_wifi.read_state(self.dir / "nowhere")
        self.assertEqual(state.mode, net_wifi.MODE_OFF)
        self.assertFalse(net_wifi.state_is_live(state, 1.0))

    def test_garbage_reads_as_off(self):
        (self.dir / net_wifi.STATE_NAME).write_text("not json at all",
                                                    encoding="utf-8")
        self.assertEqual(net_wifi.read_state(self.dir).mode, net_wifi.MODE_OFF)

    def test_a_list_instead_of_an_object_reads_as_off(self):
        (self.dir / net_wifi.STATE_NAME).write_text("[1,2,3]", encoding="utf-8")
        self.assertEqual(net_wifi.read_state(self.dir).mode, net_wifi.MODE_OFF)

    def test_unknown_keys_are_ignored_and_missing_keys_defaulted(self):
        """A newer supervisor must not break an older server (or the reverse)."""
        (self.dir / net_wifi.STATE_NAME).write_text(
            json.dumps({"mode": "hotspot", "a_field_from_the_future": 1}),
            encoding="utf-8")
        state = net_wifi.read_state(self.dir)
        self.assertEqual(state.mode, net_wifi.MODE_HOTSPOT)
        self.assertEqual(state.ssid, "")

    def test_a_wrong_type_is_defaulted_not_raised(self):
        (self.dir / net_wifi.STATE_NAME).write_text(
            json.dumps({"mode": "hotspot", "heartbeat": "soon", "deadline": None}),
            encoding="utf-8")
        state = net_wifi.read_state(self.dir)
        self.assertEqual(state.heartbeat, 0.0)
        self.assertEqual(state.deadline, 0.0)

    def test_the_file_is_world_readable(self):
        """root writes it, `mrrc` reads it. A 0600 file from root's umask would
        make the gate silently unreadable — i.e. permanently closed, with
        nothing anywhere reporting why."""
        net_wifi.write_state(net_wifi.ApState(mode=net_wifi.MODE_HOTSPOT), self.dir)
        mode = os.stat(self.dir / net_wifi.STATE_NAME).st_mode & 0o777
        self.assertEqual(mode, 0o644)

    def test_the_write_is_atomic(self):
        """A half-written state.json read by a concurrent request would be
        garbage, and garbage reads as `off` — the gate would flicker shut."""
        net_wifi.write_state(net_wifi.ApState(mode=net_wifi.MODE_HOTSPOT), self.dir)
        self.assertFalse(list(self.dir.glob("*.tmp")),
                         "the temp file must be renamed away, not left behind")

    def test_an_unwritable_directory_does_not_raise(self):
        net_wifi.write_state(net_wifi.ApState(), Path("/definitely/not/writable"))

    def test_liveness_follows_the_heartbeat(self):
        state = net_wifi.ApState(mode=net_wifi.MODE_HOTSPOT, heartbeat=100.0)
        self.assertTrue(net_wifi.state_is_live(state, 120.0))
        self.assertFalse(net_wifi.state_is_live(state, 100.0 + 46.0))

    def test_a_dead_supervisor_is_not_live_even_though_it_says_hotspot(self):
        state = net_wifi.ApState(mode=net_wifi.MODE_HOTSPOT, heartbeat=0.0)
        self.assertFalse(net_wifi.state_is_live(state, 1.0))

    def test_any_other_mode_is_not_live(self):
        for mode in (net_wifi.MODE_OFF, net_wifi.MODE_STA, "nonsense", ""):
            with self.subTest(mode=mode):
                state = net_wifi.ApState(mode=mode, heartbeat=100.0)
                self.assertFalse(net_wifi.state_is_live(state, 100.0))


class WizardFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_round_trip(self):
        claim = net_wifi.WizardClaim(action="connect", ssid="Home",
                                     state=net_wifi.WIZARD_FAILED,
                                     error="Secrets were required",
                                     address="", nonce="abc123", heartbeat=50.0)
        net_wifi.write_wizard(claim, self.dir)
        self.assertEqual(net_wifi.read_wizard(self.dir), claim)

    def test_the_claim_has_no_password_field(self):
        """The PSK must have nowhere to be written, so "never on disk" is a
        property of the schema rather than a promise every caller keeps."""
        fields = set(net_wifi.WizardClaim.__dataclass_fields__)
        self.assertNotIn("password", fields)
        self.assertNotIn("psk", fields)
        self.assertNotIn("secret", fields)
        self.assertEqual(fields, {"action", "ssid", "state", "error", "address",
                                  "nonce", "heartbeat"})

    def test_a_missing_file_reads_as_no_claim(self):
        claim = net_wifi.read_wizard(self.dir)
        self.assertEqual(claim.action, "")
        self.assertFalse(net_wifi.claim_is_fresh(claim, 1.0))

    def test_garbage_reads_as_no_claim(self):
        (self.dir / net_wifi.WIZARD_NAME).write_text("{oops", encoding="utf-8")
        self.assertEqual(net_wifi.read_wizard(self.dir).action, "")

    def test_a_switch_in_flight_is_fresh(self):
        claim = net_wifi.WizardClaim(action="connect",
                                     state=net_wifi.WIZARD_SWITCHING,
                                     heartbeat=100.0)
        self.assertTrue(net_wifi.claim_is_fresh(claim, 130.0))

    def test_a_switch_that_stopped_heartbeating_goes_stale(self):
        """The server died mid-switch; the supervisor must take the radio back
        or the box is left with neither an AP nor an uplink, forever."""
        claim = net_wifi.WizardClaim(action="connect",
                                     state=net_wifi.WIZARD_SWITCHING,
                                     heartbeat=100.0)
        self.assertFalse(net_wifi.claim_is_fresh(claim, 100.0 + 46.0))

    def test_a_finished_claim_stays_actionable_longer(self):
        """It needs no heartbeat, and the supervisor must still see the failure
        in order to reopen the window."""
        claim = net_wifi.WizardClaim(action="connect",
                                     state=net_wifi.WIZARD_FAILED,
                                     heartbeat=100.0)
        self.assertTrue(net_wifi.claim_is_fresh(claim, 400.0))
        self.assertFalse(net_wifi.claim_is_fresh(claim, 100.0 + 601.0))

    def test_an_empty_action_is_not_a_claim(self):
        claim = net_wifi.WizardClaim(action="", state=net_wifi.WIZARD_OK,
                                     heartbeat=100.0)
        self.assertFalse(net_wifi.claim_is_fresh(claim, 100.0))


class ClientIpTests(unittest.TestCase):
    def test_a_plain_v4_address(self):
        import ipaddress
        self.assertEqual(net_wifi.client_ip("10.42.0.57"),
                         ipaddress.ip_address("10.42.0.57"))

    def test_an_ipv4_mapped_address_is_unwrapped(self):
        """uvicorn on a dual-stack socket reports an IPv4 client as
        `::ffff:10.42.0.57`. Compared against a v4 network that is False, so on
        a box bound to `::` the gate would never open — and nothing would log
        why, because every check "correctly" returned False."""
        import ipaddress
        self.assertEqual(net_wifi.client_ip("::ffff:10.42.0.57"),
                         ipaddress.ip_address("10.42.0.57"))

    def test_a_real_v6_address_is_preserved(self):
        import ipaddress
        self.assertEqual(net_wifi.client_ip("2001:db8::1"),
                         ipaddress.ip_address("2001:db8::1"))

    def test_nonsense_is_none(self):
        for host in ("", None, "   ", "not-an-ip", "10.42.0.57:8888"):
            with self.subTest(host=host):
                self.assertIsNone(net_wifi.client_ip(host))


class GateTests(unittest.TestCase):
    """Design D-6: passwordless ⟺ live hotspot AND the request came from it."""

    NOW = 1_000.0

    def live_state(self, network="10.42.0.0/24"):
        return net_wifi.ApState(mode=net_wifi.MODE_HOTSPOT, ssid="MRRC-Setup",
                                gateway="10.42.0.1/24", network=network,
                                heartbeat=self.NOW - 5.0)

    def test_open_from_inside_the_hotspot_subnet(self):
        self.assertTrue(net_wifi.gate_is_open(self.live_state(), "10.42.0.57",
                                              self.NOW))

    def test_open_from_the_gateway_itself(self):
        self.assertTrue(net_wifi.gate_is_open(self.live_state(), "10.42.0.1",
                                              self.NOW))

    def test_closed_from_another_subnet(self):
        """The case that matters: the box gets a cable plugged in while a LAN
        client happens to be addressed 10.42.0.x."""
        self.assertFalse(net_wifi.gate_is_open(self.live_state(), "192.168.1.50",
                                               self.NOW))

    def test_closed_from_a_sibling_subnet_of_the_same_class(self):
        self.assertFalse(net_wifi.gate_is_open(self.live_state(), "10.43.0.57",
                                               self.NOW))

    def test_closed_when_the_supervisor_stopped_heartbeating(self):
        state = net_wifi.ApState(mode=net_wifi.MODE_HOTSPOT,
                                 network="10.42.0.0/24", heartbeat=self.NOW - 99.0)
        self.assertFalse(net_wifi.gate_is_open(state, "10.42.0.57", self.NOW))

    def test_closed_when_the_mode_is_not_hotspot(self):
        for mode in (net_wifi.MODE_OFF, net_wifi.MODE_STA, ""):
            with self.subTest(mode=mode):
                state = net_wifi.ApState(mode=mode, network="10.42.0.0/24",
                                         heartbeat=self.NOW)
                self.assertFalse(net_wifi.gate_is_open(state, "10.42.0.57",
                                                       self.NOW))

    def test_closed_for_an_unknown_client(self):
        self.assertFalse(net_wifi.gate_is_open(self.live_state(), "", self.NOW))
        self.assertFalse(net_wifi.gate_is_open(self.live_state(), None, self.NOW))

    def test_closed_for_an_ipv6_client_against_a_v4_hotspot(self):
        """Must be False, and must not raise: `in` across versions is the kind
        of thing that turns a gate into a 500."""
        self.assertFalse(net_wifi.gate_is_open(self.live_state(), "2001:db8::1",
                                               self.NOW))

    def test_open_for_an_ipv4_mapped_client(self):
        self.assertTrue(net_wifi.gate_is_open(self.live_state(),
                                              "::ffff:10.42.0.57", self.NOW))

    def test_the_network_comes_from_the_state_not_from_a_constant(self):
        """NM hands out 10.42.0.0/24 by default but not by contract; a state
        that says otherwise must be believed."""
        state = self.live_state(network="10.99.0.0/24")
        self.assertFalse(net_wifi.gate_is_open(state, "10.42.0.57", self.NOW))
        self.assertTrue(net_wifi.gate_is_open(state, "10.99.0.7", self.NOW))

    def test_a_garbage_network_closes_the_gate(self):
        """Fail closed, not open.

        `ap_network()` normalises on the write side, so a garbage `network` can
        only mean a corrupt or hand-edited file — and malformed evidence must
        never be the thing that opens a passwordless door. (An *empty* one is
        different: that is a supervisor that has not read its gateway back yet,
        and the next test covers it.)
        """
        state = self.live_state(network="not-a-network")
        self.assertFalse(net_wifi.gate_is_open(state, "10.42.0.57", self.NOW))
        self.assertFalse(net_wifi.gate_is_open(state, "10.99.0.7", self.NOW))

    def test_an_unparseable_prefix_closes_the_gate(self):
        state = self.live_state(network="10.42.0.1/99")
        self.assertFalse(net_wifi.gate_is_open(state, "10.42.0.57", self.NOW))

> **执行时的更正（2026-10-08）**：本计划原先这里写的是
> `test_a_garbage_network_falls_back_to_the_nm_default`（期望**开门**）。实跑后判定
> **测试错了、实现对了**：`ap_network()` 已经在**写入侧**做过归一化，所以 `state.json`
> 里出现垃圾 `network` 只可能是文件损坏或被手改——而**损坏的证据不该成为打开免口令门的理由**。
> 因此改成 fail-closed，并补一条 `/99` 这种"能解析成字符串但不是合法前缀"的负例。
> 注意与**空** `network` 区分开：空是"守护进程还没回读到网关"的正常状态，仍回落到 NM 默认网段
> （下一条测试守着）。

    def test_an_empty_network_falls_back_to_the_nm_default(self):
        state = self.live_state(network="")
        self.assertTrue(net_wifi.gate_is_open(state, "10.42.0.57", self.NOW))

    def test_a_missing_state_file_closes_the_gate(self):
        self.assertFalse(net_wifi.gate_is_open(net_wifi.read_state(Path("/nope")),
                                               "10.42.0.57", self.NOW))

    def test_the_max_age_can_be_tightened(self):
        state = net_wifi.ApState(mode=net_wifi.MODE_HOTSPOT,
                                 network="10.42.0.0/24", heartbeat=self.NOW - 10.0)
        self.assertFalse(net_wifi.gate_is_open(state, "10.42.0.57", self.NOW,
                                               max_age=5.0))


class EnsureStateDirTests(unittest.TestCase):
    def test_creates_the_directory_group_writable(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "setup-ap"
            net_wifi.ensure_state_dir(target)
            self.assertTrue(target.is_dir())
            mode = os.stat(target).st_mode & 0o777
            self.assertEqual(mode, 0o775,
                             "root writes state.json, `mrrc` writes wizard.json")

    def test_an_existing_directory_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            net_wifi.ensure_state_dir(Path(tmp))
            net_wifi.ensure_state_dir(Path(tmp))

    def test_a_directory_it_cannot_create_does_not_raise(self):
        """A desktop install has no /run/mrrc and no `mrrc` group; the gate just
        stays closed there rather than taking the server down."""
        net_wifi.ensure_state_dir(Path("/definitely/not/writable"))
```

- [ ] **步骤 2：运行测试验证失败**

运行：`$PY -m unittest tests.test_net_wifi -v 2>&1 | tail -10`
预期：一片 ERROR — `AttributeError: module 'net_wifi' has no attribute 'ap_settings'`（以及 `ApState` / `read_state` / `write_state` / `state_is_live` / `WizardClaim` / `read_wizard` / `write_wizard` / `claim_is_fresh` / `client_ip` / `gate_is_open` / `ensure_state_dir`）

- [ ] **步骤 3：编写实现**

在 `net_wifi.py` 末尾（`scrub()` 之后）追加：

```python
# ── configuration both owners must agree on ─────────────────────────
def ap_settings(env: Optional[dict] = None) -> dict:
    """The WiFi-domain settings, read in exactly one place.

    The supervisor and the server both need the SSID and the state directory,
    and a drift between them is silent: the server would read a state file
    nobody writes, so the gate would stay shut and the box would look like it
    never opened a hotspot.
    """
    source = os.environ if env is None else env
    port = _int_or((source.get("MRRC_SETUP_AP_WEB_PORT") or "").strip()
                   or (source.get("MRRC_WEB_PORT") or "").strip(),
                   DEFAULT_WEB_PORT)
    return {
        "ssid": (source.get("MRRC_SETUP_AP_SSID") or "").strip() or DEFAULT_SSID,
        "ifname": (source.get("MRRC_SETUP_AP_IFNAME") or "").strip() or DEFAULT_IFNAME,
        "web_port": port,
        "state_dir": Path((source.get("MRRC_SETUP_AP_STATE_DIR") or "").strip()
                          or str(DEFAULT_STATE_DIR)),
    }


# ── the shared directory and its two mailboxes ──────────────────────
def _resolve_dir(state_dir) -> Path:
    return Path(state_dir) if state_dir is not None else DEFAULT_STATE_DIR


def ensure_state_dir(state_dir=None) -> None:
    """Create the directory so BOTH owners can write their own file.

    The supervisor runs as root and the server as ``mrrc``, so the directory is
    0775 with group ``mrrc``: root writes ``state.json``, ``mrrc`` writes
    ``wizard.json``, and each is the only writer of its own file — which is why
    there is no lock here.

    Every failure is swallowed. A desktop install has no ``/run/mrrc`` and no
    ``mrrc`` group, and the correct behaviour there is a permanently closed
    gate, not a server that will not start.
    """
    directory = _resolve_dir(state_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    try:
        os.chmod(directory, 0o775)
    except OSError:
        pass
    try:
        import grp          # POSIX-only. A deferred import is load-bearing:
        os.chown(directory, -1, grp.getgrnam(STATE_GROUP).gr_gid)
    except (ImportError, KeyError, OSError, AttributeError):
        pass


def _write_json(path: Path, payload: dict) -> None:
    """Atomic, world-readable JSON. Never raises.

    World-readable on purpose and safe: neither file carries a secret, and that
    is precisely what lets a root daemon and an ``mrrc`` server share one
    directory. The chmod happens *before* the rename because ``write_text``
    creates the temp file under the process umask — root's umask would leave a
    0600 file the server cannot read, i.e. a gate that is permanently closed
    and reports nothing.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        try:
            os.chmod(tmp, 0o644)
        except OSError:
            pass
        tmp.replace(path)
    except OSError:
        pass


def _read_json(path: Path):
    """The parsed object at ``path``, or ``None`` for anything unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _pick(raw: dict, key: str, default, cast):
    """One typed field of a JSON object; anything unparseable falls back.

    A newer writer must not break an older reader (or the reverse), and a
    truncated file must read as "gate closed" rather than raise inside a
    request handler.
    """
    value = raw.get(key, default)
    if value is None:
        return default
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class ApState:
    """What the supervisor last published (``state.json``).

    ``mode`` is the gate's first half: only ``MODE_HOTSPOT`` can open the
    passwordless window, so a supervisor that died, a box with a cable in it and
    a box that finished onboarding all close it without anyone having to
    remember to (design D-6: the window disappears *with the network*, so there
    is no "is it configured yet" flag that could fail to be written).
    """

    mode: str = MODE_OFF
    ssid: str = ""
    gateway: str = ""
    network: str = ""
    url: str = ""
    reason: str = ""
    since: float = 0.0
    heartbeat: float = 0.0
    deadline: float = 0.0

    def to_json(self) -> dict:
        return {
            "mode": self.mode, "ssid": self.ssid, "gateway": self.gateway,
            "network": self.network, "url": self.url, "reason": self.reason,
            "since": self.since, "heartbeat": self.heartbeat,
            "deadline": self.deadline,
        }

    @classmethod
    def from_json(cls, raw) -> "ApState":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            mode=_pick(raw, "mode", MODE_OFF, str),
            ssid=_pick(raw, "ssid", "", str),
            gateway=_pick(raw, "gateway", "", str),
            network=_pick(raw, "network", "", str),
            url=_pick(raw, "url", "", str),
            reason=_pick(raw, "reason", "", str),
            since=_pick(raw, "since", 0.0, float),
            heartbeat=_pick(raw, "heartbeat", 0.0, float),
            deadline=_pick(raw, "deadline", 0.0, float),
        )


def read_state(state_dir=None) -> ApState:
    """The supervisor's last word; ``ApState()`` (mode off) when there is none."""
    return ApState.from_json(_read_json(_resolve_dir(state_dir) / STATE_NAME))


def write_state(state: ApState, state_dir=None) -> None:
    """Publish one heartbeat's worth of truth. Called by the supervisor only."""
    _write_json(_resolve_dir(state_dir) / STATE_NAME, state.to_json())


def state_is_live(state: ApState, now: float,
                  max_age: float = HEARTBEAT_MAX_AGE_S) -> bool:
    """Whether the supervisor is demonstrably running the hotspot *now*.

    A stale file means a dead supervisor: trusting it would leave the
    passwordless window open on a box whose hotspot is long gone, which is the
    one failure mode D-6's "no stored flag" rule exists to prevent.
    """
    if state.mode != MODE_HOTSPOT:
        return False
    if state.heartbeat <= 0:
        return False
    return (now - state.heartbeat) <= max_age


@dataclass(frozen=True)
class WizardClaim:
    """The server's mailbox entry (``wizard.json``).

    There is deliberately **no password field**: the WiFi PSK has nowhere to be
    written, which makes "never on disk" a property of the schema rather than a
    promise every future caller has to remember to keep.
    """

    action: str = ""        # "connect" — the only action today
    ssid: str = ""
    state: str = ""         # WIZARD_SWITCHING | WIZARD_OK | WIZARD_FAILED
    error: str = ""
    address: str = ""
    nonce: str = ""
    heartbeat: float = 0.0

    def to_json(self) -> dict:
        return {
            "action": self.action, "ssid": self.ssid, "state": self.state,
            "error": self.error, "address": self.address,
            "nonce": self.nonce, "heartbeat": self.heartbeat,
        }

    @classmethod
    def from_json(cls, raw) -> "WizardClaim":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            action=_pick(raw, "action", "", str),
            ssid=_pick(raw, "ssid", "", str),
            state=_pick(raw, "state", "", str),
            error=_pick(raw, "error", "", str),
            address=_pick(raw, "address", "", str),
            nonce=_pick(raw, "nonce", "", str),
            heartbeat=_pick(raw, "heartbeat", 0.0, float),
        )


def read_wizard(state_dir=None) -> WizardClaim:
    """The server's current claim on the radio; empty when there is none."""
    return WizardClaim.from_json(_read_json(_resolve_dir(state_dir) / WIZARD_NAME))


def write_wizard(claim: WizardClaim, state_dir=None) -> None:
    """Claim or report. Called by the server only."""
    _write_json(_resolve_dir(state_dir) / WIZARD_NAME, claim.to_json())


def claim_is_fresh(claim: WizardClaim, now: float) -> bool:
    """Whether the supervisor should still stand down for this claim.

    Two budgets, because the two situations differ. A switch *in flight*
    heartbeats every few seconds, so 45 s means the server died mid-switch and
    the supervisor must take the radio back — otherwise the box is left with
    neither an AP nor an uplink and nobody to fix it. A *finished* claim needs
    no heartbeat and stays actionable for ten minutes, which is how the
    supervisor learns that a join failed and reopens the window.
    """
    if not claim.action or claim.heartbeat <= 0:
        return False
    age = now - claim.heartbeat
    if claim.state == WIZARD_SWITCHING:
        return age <= HEARTBEAT_MAX_AGE_S
    return age <= CLAIM_MAX_AGE_S


# ── the passwordless gate ───────────────────────────────────────────
def client_ip(host: Optional[str]):
    """A request's client host as an address object, or ``None``.

    The IPv4-mapped unwrap is the load-bearing line. uvicorn on a dual-stack
    socket reports an IPv4 client as ``::ffff:10.42.0.57``, and that compared
    against a v4 network is ``False`` — so on a box bound to ``::`` the gate
    would never open, with nothing logging why, because every check
    "correctly" returned False.
    """
    try:
        addr = ipaddress.ip_address((host or "").strip())
    except ValueError:
        return None
    return getattr(addr, "ipv4_mapped", None) or addr


def gate_is_open(state: ApState, client_host: Optional[str], now: float,
                 max_age: float = HEARTBEAT_MAX_AGE_S) -> bool:
    """Design D-6's rule — the only passwordless door in the product.

    Both halves must hold:

    * the hotspot must be demonstrably up **now** (a live heartbeat, not a
      stored flag), and
    * the request must arrive from **that hotspot's own subnet**.

    The subnet half alone is a collision waiting to happen — 10.42.0.0/24 is a
    perfectly ordinary LAN range. The heartbeat half alone would let any client
    on the box's real network in. Together they give D-6's property: when the
    hotspot goes away the subnet goes away with it, so the branch cannot be
    left open by a flag that failed to be cleared.
    """
    if not state_is_live(state, now, max_age):
        return False
    addr = client_ip(client_host)
    if addr is None:
        return False
    try:
        network = ipaddress.ip_network(state.network or NM_SHARED_SUBNET,
                                       strict=False)
    except ValueError:
        return False
    return addr in network
```

- [ ] **步骤 4：运行测试验证通过**

运行：`$PY -m unittest tests.test_net_wifi -v 2>&1 | tail -6`
预期：`OK`（约 115 项）

排障提示：
- `test_the_file_is_world_readable` 红 ⇒ `_write_json` 的 `chmod` 必须在 `replace()` **之前**（对 tmp 文件做），对已 replace 的目标做也行，但必须在同一个 try 里且失败被吞掉。
- `test_creates_the_directory_group_writable` 红在 mode 上 ⇒ 检查 `os.chmod(directory, 0o775)` 是否被 `mkdir` 的 exist_ok 分支跳过；它必须无条件执行。
- `test_an_ipv4_mapped_client_*` 红 ⇒ `client_ip` 漏了 `getattr(addr, "ipv4_mapped", None) or addr`。这条是**双栈绑定下闸门永久关闭**的那个静默故障。

- [ ] **步骤 5：确认没有弄坏别的 + Commit**

```bash
$PY -m unittest discover -s tests 2>&1 | tail -4     # 仍只有 test_tls_trust_store 一条红
$PY .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git add net_wifi.py tests/test_net_wifi.py
git commit -m "feat: net_wifi 的两份邮箱文件与免口令闸门——活心跳 + 热点网段，双栈地址要解包"
```


---

## 任务 4：`linux/setup_ap.py` — 常驻判定进程（测试）

**文件：**
- 创建：`linux/setup_ap.py`
- 测试：`tests/test_setup_ap.py`

**这个文件里有三个决策，写错任何一个都会静默地坏：**

1. **热点自己不算上行**（约束 12）。`net_wifi.has_uplink()` 已经减掉了它，但守护进程还必须在"热点已经开着"时**不再重复起**，否则每 tick 都在重配射频。
2. **超时之后不重开**（规格 D-6）。`_timed_out` 是**进程生命期**的闩，不是持久状态。这正是 D-6 的恢复路径：断电重启 ⇒ 新进程 ⇒ 闩是干净的 ⇒ 若仍然没有上行，窗口重开一次。**把它写成持久文件就等于把盒子变砖**（错过窗口就永远进不去，除非重刷）。
3. **重启后要"领养"已经在跑的热点并给它补一个窗口**。服务在窗口期内被 restart 时，新进程的 `_deadline` 是 0，而超时只在 `_deadline` 非零时才触发 ⇒ **一个永远不关的开放网络**。这是写测试时才发现的缺口，实现里用"发现热点在跑但没有 deadline 就当场武装一个"补上。

- [ ] **步骤 1：编写失败的测试**

创建 `tests/test_setup_ap.py`：

```python
"""The resident supervisor: when the open window exists, and when it must not.

Three decisions here fail silently, so each is asserted from several directions:

* our own hotspot reports `connected` in `nmcli device status`, so a supervisor
  that trusts that column raises the AP and tears it down again every tick;
* the timeout latch lives for the *process*, not on disk. Persisting it would
  turn "you missed the 30-minute window" into "the box is unreachable until you
  reflash it" — the design's recovery path is a power cycle, and a power cycle
  only helps if the latch dies with the process;
* a supervisor restarted mid-window finds an AP it did not raise and has no
  deadline for it, so without adopting one the open network never closes.

`FakeRadio` models the fact every decision turns on — whether an AP is up — and
flips it when the supervisor asks nmcli to raise or lower one, so a supervisor
that does them in the wrong order is caught rather than tolerated.

`Harness`'s fake sleep **advances the fake clock**: a sleep that does not is how
a settle-window test turns into a hang.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import net_wifi
from linux import setup_ap
from net_wifi import NmResult


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeRadio:
    """A fake nmcli that tracks whether the AP is up and answers accordingly."""

    def __init__(self, wired: bool = False, sta: bool = False,
                 ap_up: bool = False, hotspot_fails: bool = False,
                 connecting: bool = False):
        self.wired = wired
        self.sta = sta
        self.ap_up = ap_up
        self.hotspot_fails = hotspot_fails
        self.connecting = connecting
        self.calls: list = []
        self.raise_count = 0
        self.down_count = 0

    def __call__(self, argv):
        joined = " ".join(argv)
        self.calls.append(joined)

        if "-f RUNNING general" in joined:
            return NmResult(0, "running\n")

        if "-f NAME,TYPE connection show --active" in joined:
            rows = []
            if self.ap_up:
                rows.append("Hotspot:802-11-wireless")
            if self.sta:
                rows.append("Home:802-11-wireless")
            return NmResult(0, "".join(row + "\n" for row in rows))

        if "-f UUID connection show --active" in joined:
            return NmResult(0, "")

        if "802-11-wireless.mode" in joined:
            is_ap = self.ap_up and argv[-1] == "Hotspot"
            return NmResult(0, ("ap" if is_ap else "infrastructure") + "\n")

        if "-f UUID,NAME connection show" in joined:
            return NmResult(0, "")

        if "-f DEVICE,TYPE,STATE,CONNECTION device status" in joined:
            eth = "connected" if self.wired else "unavailable"
            if self.connecting:
                wifi = "ip-config:"
            elif self.ap_up:
                wifi = "connected:Hotspot"
            elif self.sta:
                wifi = "connected:Home"
            else:
                wifi = "disconnected:"
            return NmResult(0, f"eth0:ethernet:{eth}:\nwlan0:wifi:{wifi}\n")

        if "device wifi hotspot" in joined:
            if self.hotspot_fails:
                return NmResult(1, "", "Error: Device not suitable for hotspot mode")
            self.ap_up = True
            self.raise_count += 1
            return NmResult(0, "Successfully activated a Hotspot network\n", "")

        if "connection down" in joined:
            self.ap_up = False
            self.down_count += 1
            return NmResult(0, "Connection successfully deactivated\n", "")

        if "-g IP4.ADDRESS device show" in joined:
            if self.ap_up:
                return NmResult(0, "10.42.0.1/24\n")
            if self.wired or self.sta:
                return NmResult(0, "192.168.1.77/24\n")
            return NmResult(0, "")

        return NmResult(0, "", "")

    def count(self, key: str) -> int:
        return sum(1 for call in self.calls if key in call)


class CapturingLog:
    """Collects (level, rendered message) so log *volume* can be asserted."""

    def __init__(self):
        self.records: list = []

    def _record(self, level, fmt, *args):
        self.records.append((level, fmt % args if args else str(fmt)))

    def info(self, fmt, *args):
        self._record("info", fmt, *args)

    def warning(self, fmt, *args):
        self._record("warning", fmt, *args)

    def error(self, fmt, *args):
        self._record("error", fmt, *args)

    def exception(self, fmt, *args):
        self._record("error", fmt, *args)

    def messages(self, level=None):
        return [text for lvl, text in self.records if level is None or lvl == level]


class Harness:
    """One supervisor, wired to a fake radio, a fake clock and a temp dir."""

    def __init__(self, radio=None, timeout_min=30, env=None, clock=None,
                 stop_after=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name)
        self.radio = radio if radio is not None else FakeRadio()
        self.clock = clock or FakeClock()
        self.sleeps: list = []
        self.log = CapturingLog()
        self._stop_after = stop_after
        base = {"MRRC_SETUP_AP_STATE_DIR": str(self.state_dir),
                "MRRC_SETUP_AP_TIMEOUT_MIN": str(timeout_min)}
        base.update(env or {})
        self.sup = setup_ap.Supervisor(
            setup_ap.settings(base), runner=self.radio, clock=self.clock,
            sleep=self._sleep, log=self.log)

    def _sleep(self, seconds: float) -> None:
        """Record it, advance the clock, and stop the loop when asked.

        Advancing the clock is not a convenience: `_wait_for_nm` loops on
        `clock() < deadline`, so a sleep that does not move time never returns.
        """
        self.sleeps.append(seconds)
        self.clock.advance(seconds)
        if self._stop_after is not None and len(self.sleeps) >= self._stop_after:
            self.sup.stop()

    def cleanup(self):
        self.tmp.cleanup()

    def state(self) -> net_wifi.ApState:
        return net_wifi.read_state(self.state_dir)

    def write_claim(self, **fields):
        fields.setdefault("action", "connect")
        fields.setdefault("heartbeat", self.clock())
        net_wifi.write_wizard(net_wifi.WizardClaim(**fields), self.state_dir)

    def clear_claim(self):
        """What a finished switch does: the mailbox entry goes away."""
        try:
            (self.state_dir / net_wifi.WIZARD_NAME).unlink()
        except OSError:
            pass


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.cleanup)

    def test_no_network_at_all_opens_the_window(self):
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_HOTSPOT)
        self.assertEqual(self.h.radio.raise_count, 1)
        state = self.h.state()
        self.assertEqual(state.mode, net_wifi.MODE_HOTSPOT)
        self.assertEqual(state.ssid, net_wifi.DEFAULT_SSID)
        self.assertEqual(state.gateway, "10.42.0.1/24")
        self.assertEqual(state.network, "10.42.0.0/24")
        self.assertEqual(state.url, "https://10.42.0.1:8888/setup")
        self.assertEqual(state.deadline, self.h.clock() + 30 * 60)
        self.assertEqual(state.heartbeat, self.h.clock())

    def test_the_published_state_is_what_opens_the_gate(self):
        """Tasks 3 and 4 joined: the file the supervisor writes must be the file
        the server's gate believes. If these ever diverge the wizard is
        unreachable with both halves looking correct."""
        self.h.sup.tick()
        now = self.h.clock()
        self.assertTrue(net_wifi.gate_is_open(self.h.state(), "10.42.0.57", now))
        self.assertFalse(net_wifi.gate_is_open(self.h.state(), "192.168.1.50", now))

    def test_a_cable_means_no_hotspot(self):
        self.h.radio.wired = True
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_OFF)
        self.assertEqual(self.h.radio.raise_count, 0)
        self.assertEqual(self.h.state().reason, "uplink")

    def test_a_saved_wifi_that_connected_means_no_hotspot(self):
        self.h.radio.sta = True
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_OFF)
        self.assertEqual(self.h.radio.raise_count, 0)

    def test_a_cable_with_no_dhcp_still_gets_a_hotspot(self):
        """`connected (site only)`: link-local, no gateway. Reachable by nobody,
        which is exactly what the hotspot is for."""
        original = self.h.radio.__call__

        def site_only(argv):
            joined = " ".join(argv)
            if "-f DEVICE,TYPE,STATE,CONNECTION device status" in joined:
                return NmResult(0, "eth0:ethernet:connected (site only):\n"
                                   "wlan0:wifi:disconnected:\n")
            return original(argv)

        self.h.sup.runner = site_only
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_HOTSPOT)

    def test_its_own_hotspot_is_not_an_uplink(self):
        """The infinite self-teardown bug (constraint 12).

        The AP is up and nothing else is connected: the supervisor must conclude
        "still no uplink" and leave it alone. Counting the AP as an uplink would
        take it down; failing to recognise it as up would re-raise it.
        """
        self.h.radio.ap_up = True
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_HOTSPOT)
        self.assertEqual(self.h.radio.raise_count, 0, "must not re-raise a live AP")
        self.assertEqual(self.h.radio.down_count, 0, "must not tear down its own AP")

    def test_plugging_a_cable_in_while_the_hotspot_is_up_takes_it_down(self):
        """Design R1's second half, and D-6's fence 1."""
        self.h.sup.tick()
        self.assertTrue(self.h.radio.ap_up)
        self.h.radio.wired = True
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_OFF)
        self.assertEqual(self.h.radio.down_count, 1)
        self.assertFalse(self.h.radio.ap_up)

    def test_a_saved_wifi_associating_also_takes_it_down(self):
        self.h.sup.tick()
        self.h.radio.sta = True
        self.h.radio.ap_up = False       # NM dropped the AP in order to associate
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_OFF)
        self.assertEqual(self.h.state().reason, "uplink")

    def test_waiting_for_networkmanager_is_reported_not_guessed(self):
        original = self.h.radio.__call__

        def no_nm(argv):
            if "-f RUNNING general" in " ".join(argv):
                return NmResult(0, "starting\n")
            return original(argv)

        self.h.sup.runner = no_nm
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_OFF)
        self.assertEqual(self.h.state().reason, "waiting for NetworkManager")
        self.assertEqual(self.h.radio.raise_count, 0,
                         "raising an AP before NM is up just fails")


class TimeoutTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(timeout_min=1)        # 60 s, so the test stays short
        self.addCleanup(self.h.cleanup)

    def test_the_window_closes_on_its_own(self):
        self.h.sup.tick()
        self.assertTrue(self.h.radio.ap_up)
        self.h.clock.advance(61)
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_OFF)
        self.assertEqual(self.h.radio.down_count, 1)
        self.assertEqual(self.h.state().reason, "timeout")
        self.assertTrue(any("power-cycle" in m for m in self.h.log.messages("warning")),
                        "the operator has to be told how to get back in")

    def test_the_window_stays_open_before_the_deadline(self):
        self.h.sup.tick()
        self.h.clock.advance(59)
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_HOTSPOT)
        self.assertEqual(self.h.radio.down_count, 0)

    def test_a_closed_window_does_not_reopen_in_the_same_boot(self):
        """D-6 fence 2. Without the latch the supervisor would re-raise it five
        seconds later and the timeout would be decoration."""
        self.h.sup.tick()
        self.h.clock.advance(61)
        self.h.sup.tick()
        self.assertEqual(self.h.radio.raise_count, 1)
        for _ in range(5):
            self.h.clock.advance(5)
            self.assertEqual(self.h.sup.tick(), net_wifi.MODE_OFF)
        self.assertEqual(self.h.radio.raise_count, 1, "the latch must hold")
        self.assertEqual(self.h.state().reason, "timeout")

    def test_a_power_cycle_reopens_it(self):
        """The documented recovery path: the latch is per-process, so a reboot
        gives the operator another window. This is why it must never be
        persisted — a persisted latch bricks the onboarding."""
        self.h.sup.tick()
        self.h.clock.advance(61)
        self.h.sup.tick()
        self.assertEqual(self.h.radio.raise_count, 1)

        self.h.radio.ap_up = False
        rebooted = Harness(radio=self.h.radio, timeout_min=1, clock=self.h.clock)
        self.addCleanup(rebooted.cleanup)
        self.assertEqual(rebooted.sup.tick(), net_wifi.MODE_HOTSPOT)
        self.assertEqual(self.h.radio.raise_count, 2)

    def test_an_uplink_clears_the_latch(self):
        """A box that got a network and then lost it again is a fresh situation,
        not a continuation of the missed window."""
        self.h.sup.tick()
        self.h.clock.advance(61)
        self.h.sup.tick()
        self.h.radio.wired = True
        self.h.sup.tick()
        self.h.radio.wired = False
        self.h.radio.ap_up = False
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_HOTSPOT)

    def test_a_supervisor_restarted_mid_window_arms_a_deadline(self):
        """It finds an AP it did not raise. Without adopting a deadline the
        timeout can never fire, and an open network stays up forever."""
        self.h.radio.ap_up = True
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_HOTSPOT)
        self.assertEqual(self.h.radio.raise_count, 0)
        self.assertEqual(self.h.state().deadline, self.h.clock() + 60)
        self.h.clock.advance(61)
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_OFF)
        self.assertEqual(self.h.radio.down_count, 1)


class WizardHandoffTests(unittest.TestCase):
    """Design D-3: one radio, so the supervisor must stand down while the server
    performs the AP→STA switch."""

    def setUp(self):
        self.h = Harness(timeout_min=1)
        self.addCleanup(self.h.cleanup)

    def test_it_stands_down_during_a_switch(self):
        self.h.sup.tick()                       # raises the AP, deadline t0+60
        self.h.clock.advance(10)
        self.h.write_claim(state=net_wifi.WIZARD_SWITCHING, ssid="Home")
        self.h.radio.calls.clear()
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_OFF)
        self.assertEqual(self.h.state().reason, "wizard switching")
        self.assertEqual(self.h.radio.calls, [],
                         "standing down means not touching nmcli at all")

    def test_the_window_is_compensated_for_the_time_spent_standing_down(self):
        """A slow switch must not eat the operator's minutes: two 10 s
        stand-downs push a 60 s window out to 80 s, so at t0+70 the AP is still
        up. Without compensation this tears it down mid-retry."""
        self.h.sup.tick()                                    # t0, deadline t0+60
        self.h.clock.advance(10)
        self.h.write_claim(state=net_wifi.WIZARD_SWITCHING, ssid="Home")
        self.h.sup.tick()                                    # +10 → deadline t0+70
        self.h.clock.advance(10)
        self.h.write_claim(state=net_wifi.WIZARD_SWITCHING, ssid="Home")
        self.h.sup.tick()                                    # +10 → deadline t0+80

        self.h.clock.advance(50)                             # t0+70 < t0+80
        self.h.clear_claim()
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_HOTSPOT)
        self.assertEqual(self.h.radio.down_count, 0,
                         "the compensated deadline must not have passed")

    def test_a_successful_switch_leaves_the_hotspot_down(self):
        self.h.sup.tick()
        self.h.radio.ap_up = False        # the server took the AP down to switch
        self.h.radio.sta = True
        self.h.write_claim(state=net_wifi.WIZARD_OK, ssid="Home",
                           address="192.168.1.77/24")
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_STA)
        self.assertEqual(self.h.radio.raise_count, 1, "must not re-raise the AP")
        self.assertEqual(self.h.state().gateway, "192.168.1.77/24")
        self.assertEqual(self.h.state().url, "https://192.168.1.77:8888/setup")

    def test_a_failed_switch_that_restored_the_ap_restarts_the_window(self):
        """Design §6: a failure returns to the hotspot *and* says why. Retrying
        must be possible without a power cycle, so one fresh window per nonce."""
        self.h.sup.tick()
        self.h.clock.advance(61)                       # let the window expire
        self.h.sup.tick()
        self.assertEqual(self.h.state().reason, "timeout")

        self.h.radio.ap_up = True                      # the server put it back
        self.h.write_claim(state=net_wifi.WIZARD_FAILED, ssid="Home",
                           error="Secrets were required", nonce="n1")
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_HOTSPOT)
        self.assertEqual(self.h.state().deadline, self.h.clock() + 60)
        self.assertEqual(self.h.radio.down_count, 1, "only the timeout's own teardown")

    def test_the_same_nonce_cannot_buy_a_second_window(self):
        """Otherwise a page that retries on a timer would keep an open network
        alive indefinitely — the exact thing fence 2 exists to prevent."""
        self.h.sup.tick()
        self.h.write_claim(state=net_wifi.WIZARD_FAILED, ssid="Home",
                           error="nope", nonce="n1")
        self.h.sup.tick()
        first_deadline = self.h.state().deadline
        self.h.clock.advance(5)
        self.h.write_claim(state=net_wifi.WIZARD_FAILED, ssid="Home",
                           error="nope", nonce="n1")
        self.h.sup.tick()
        self.assertEqual(self.h.state().deadline, first_deadline)

    def test_a_different_nonce_buys_one(self):
        self.h.sup.tick()
        self.h.write_claim(state=net_wifi.WIZARD_FAILED, ssid="Home",
                           error="nope", nonce="n1")
        self.h.sup.tick()
        first = self.h.state().deadline
        self.h.clock.advance(5)
        self.h.write_claim(state=net_wifi.WIZARD_FAILED, ssid="Home",
                           error="nope", nonce="n2")
        self.h.sup.tick()
        self.assertEqual(self.h.state().deadline, self.h.clock() + 60)
        self.assertGreater(self.h.state().deadline, first)

    def test_a_failed_switch_that_could_not_restore_the_ap_is_repaired(self):
        """The server tried to put the hotspot back and failed. Without this the
        box sits unreachable with nobody able to tell the operator why."""
        self.h.sup.tick()
        self.h.radio.ap_up = False
        self.h.write_claim(state=net_wifi.WIZARD_FAILED, ssid="Home",
                           error="nope", nonce="n1")
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_HOTSPOT)
        self.assertEqual(self.h.radio.raise_count, 2)
        self.assertTrue(self.h.radio.ap_up)

    def test_a_stale_claim_is_ignored(self):
        """The server died mid-switch. The supervisor has to take the radio back
        or the box is left with neither an AP nor an uplink, forever."""
        self.h.sup.tick()
        self.h.radio.ap_up = False
        self.h.write_claim(state=net_wifi.WIZARD_SWITCHING, ssid="Home",
                           heartbeat=self.h.clock() - 300)
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_HOTSPOT)
        self.assertEqual(self.h.radio.raise_count, 2)


class FailureTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(radio=FakeRadio(hotspot_fails=True))
        self.addCleanup(self.h.cleanup)

    def test_a_hotspot_that_will_not_start_is_reported_in_the_state(self):
        """R6: the reason has to be readable without ssh, because the whole point
        is that there may be no way to ssh in."""
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_OFF)
        state = self.h.state()
        self.assertTrue(state.reason.startswith("start failed:"), state.reason)
        self.assertIn("not suitable", state.reason)

    def test_it_keeps_trying(self):
        """Risk R-1: whether this radio can do AP mode at all is unverified on
        real hardware, so the supervisor must retry rather than give up after
        one answer."""
        for _ in range(3):
            self.h.sup.tick()
            self.h.clock.advance(5)
        self.h.sup.tick()
        self.assertEqual(self.h.radio.count("device wifi hotspot"), 4)

    def test_a_repeating_failure_does_not_fill_the_console(self):
        """The unit forwards to the HDMI console; five identical lines a minute
        would bury the one message the operator needs."""
        for _ in range(20):
            self.h.sup.tick()
            self.h.clock.advance(5)
        errors = self.h.log.messages("error")
        self.assertEqual(len(errors), 2, f"expected rate limiting, got {errors}")


class RunLoopTests(unittest.TestCase):
    def test_run_stops_when_told_and_cleans_up(self):
        h = Harness(stop_after=3)
        self.addCleanup(h.cleanup)
        h.sup.tick()                            # raise the AP so there is
        self.assertTrue(h.radio.ap_up)          # something to clean up
        self.assertEqual(h.sup.run(), 0)
        self.assertFalse(h.radio.ap_up, "stopping the service must take the AP down")
        self.assertEqual(h.state().reason, "service stopped")

    def test_the_loop_sleeps_for_the_configured_poll_interval(self):
        h = Harness(env={"MRRC_SETUP_AP_POLL_S": "7"}, stop_after=3)
        self.addCleanup(h.cleanup)
        h.sup.run()
        loop_sleeps = h.sleeps[1:]              # the first is the settle grace
        self.assertTrue(loop_sleeps)
        self.assertTrue(all(1.0 <= s <= 7.0 for s in loop_sleeps), h.sleeps)

    def test_run_survives_a_tick_that_raises(self):
        """A resident service that dies on one bad nmcli answer stops being able
        to open the window at all, and the only symptom the operator sees is
        "no hotspot ever appeared"."""
        h = Harness(stop_after=3)
        self.addCleanup(h.cleanup)

        def explode(argv):
            raise RuntimeError("nmcli exploded")

        h.sup.runner = explode
        self.assertEqual(h.sup.run(), 0)
        self.assertTrue(any("tick failed" in m for m in h.log.messages("error")))
        self.assertGreaterEqual(len(h.sleeps), 3, "and it must keep ticking")


class SettleTests(unittest.TestCase):
    """D-6's criterion is "no saved WiFi that *connects*" — and connecting takes
    time, so deciding at t=0 would open a window on a box that was about to have
    a network."""

    def test_an_existing_uplink_returns_at_once(self):
        h = Harness(radio=FakeRadio(wired=True))
        self.addCleanup(h.cleanup)
        h.sup._wait_for_nm()
        self.assertEqual(h.sleeps, [])

    def test_it_waits_while_networkmanager_is_still_working(self):
        radio = FakeRadio(connecting=True)
        h = Harness(radio=radio, env={"MRRC_SETUP_AP_SETTLE_S": "5"})
        self.addCleanup(h.cleanup)
        h.sup._wait_for_nm()
        self.assertGreaterEqual(len(h.sleeps), 2,
                                "a device in ip-config is not a settled answer")
        self.assertEqual(radio.raise_count, 0)

    def test_it_gives_one_last_grace_once_nm_has_settled(self):
        h = Harness(env={"MRRC_SETUP_AP_SETTLE_S": "30"})
        self.addCleanup(h.cleanup)
        h.sup._wait_for_nm()
        self.assertIn(setup_ap.GRACE_AFTER_IDLE_S, h.sleeps,
                      "a DHCP handshake in flight is not 'no network'")

    def test_it_stops_waiting_when_the_settle_window_expires(self):
        h = Harness(radio=FakeRadio(connecting=True),
                    env={"MRRC_SETUP_AP_SETTLE_S": "3"})
        self.addCleanup(h.cleanup)
        h.sup._wait_for_nm()
        self.assertLessEqual(sum(h.sleeps), 4.0,
                             "it must not wait out the whole window plus grace")


class SettingsTests(unittest.TestCase):
    def test_the_defaults_match_the_design(self):
        cfg = setup_ap.settings({})
        self.assertEqual(cfg["timeout_s"], 30 * 60)     # D-6: 30 minutes
        self.assertEqual(cfg["poll_s"], setup_ap.DEFAULT_POLL_S)
        self.assertEqual(cfg["settle_s"], setup_ap.DEFAULT_SETTLE_S)
        self.assertEqual(cfg["ssid"], net_wifi.DEFAULT_SSID)

    def test_the_timeout_is_configurable_in_minutes(self):
        cfg = setup_ap.settings({"MRRC_SETUP_AP_TIMEOUT_MIN": "5"})
        self.assertEqual(cfg["timeout_s"], 300)

    def test_a_zero_or_negative_timeout_falls_back(self):
        """0 would close the window the instant it opened, i.e. no onboarding at
        all — and it is exactly what a typo produces."""
        for bad in ("0", "-5", "abc", "", None):
            with self.subTest(bad=bad):
                cfg = setup_ap.settings({"MRRC_SETUP_AP_TIMEOUT_MIN": bad})
                self.assertEqual(cfg["timeout_s"], 30 * 60)

    def test_the_ssid_is_configurable(self):
        cfg = setup_ap.settings({"MRRC_SETUP_AP_SSID": "HB9XYZ-box"})
        self.assertEqual(cfg["ssid"], "HB9XYZ-box")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **步骤 2：运行测试验证失败**

运行：`$PY -m unittest tests.test_setup_ap -v 2>&1 | tail -6`
预期：ERROR — `ImportError: cannot import name 'setup_ap' from 'linux'`（或 `ModuleNotFoundError: No module named 'linux.setup_ap'`）

- [ ] **步骤 3：编写实现**

创建 `linux/setup_ap.py`：

```python
#!/usr/bin/env python3
"""mrrc-setup-ap.service: open a hotspot while the box cannot be reached.

Design: ``docs/superpowers/specs/2026-10-08-w103d-setup-ap-design.md`` (§6, D-6).

The rule is about the **network**, not about "first boot"::

    raise the hotspot  ⟺  no cable link AND no saved WiFi that connects

which is why a configured box never shows it (it has an uplink), why an
unconfigured one shows it again after every power cycle (it still has none), and
why there is no "is it configured yet" flag anywhere to drift out of sync.

Three fences around the open window (design D-6):

1. it exists only while there is no other uplink — the moment a cable is plugged
   in or a saved WiFi associates, this takes the hotspot down;
2. it closes by itself after ``MRRC_SETUP_AP_TIMEOUT_MIN`` minutes (default 30)
   and does **not** reopen until the box is power-cycled. That latch is
   per-process on purpose: persisting it would turn "you missed the window" into
   "the box is unreachable until you reflash it";
3. every raise, every close and every wizard entry is logged, and the raise goes
   to the HDMI console at WARNING, so an operator with a monitor never needs the
   hotspot at all (the mitigation for design risk R-2).

A resident service rather than a oneshot because the wizard page — and later
``/manage`` — need to ask whether the hotspot is up and how long is left. That
answer is ``state.json``, rewritten every tick, and its heartbeat is half of the
server's passwordless gate: a supervisor that stops heartbeating closes the gate
by itself, which is why "stale file" and "gate open" can never be true together.

The radio is single (design D-3), so while the server performs an AP→STA switch
this loop **stands down**; the server's claim in ``wizard.json`` is what says so.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Callable, Optional

# In the image this file is /opt/mrrc_modern/linux/setup_ap.py and net_wifi.py
# sits beside its parent — the same layout as the repository, and the same trick
# linux/mrrc_radio.py already uses.
REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import net_wifi  # noqa: E402

DEFAULT_TIMEOUT_MIN = 30
DEFAULT_POLL_S = 5
DEFAULT_SETTLE_S = 30
# Once NM has settled on "nothing", one more pause for a DHCP handshake that is
# already in flight but not yet visible in the state column.
GRACE_AFTER_IDLE_S = 5.0
# A hotspot that cannot start is retried every tick; the log is not.
ERROR_LOG_GAP_S = 60.0

logger = logging.getLogger("mrrc.setup_ap")


def _int_or(value, default: int) -> int:
    """A positive int from the environment, or the default.

    Zero and negatives fall back too: ``MRRC_SETUP_AP_TIMEOUT_MIN=0`` would close
    the window the instant it opened, which is what a typo produces and not what
    anybody meant.
    """
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return default
    return parsed if parsed > 0 else default


def settings(env: Optional[dict] = None) -> dict:
    """Everything this service reads, resolved in one place."""
    source = os.environ if env is None else env
    cfg = net_wifi.ap_settings(source)
    cfg["timeout_s"] = _int_or(source.get("MRRC_SETUP_AP_TIMEOUT_MIN"),
                               DEFAULT_TIMEOUT_MIN) * 60
    cfg["poll_s"] = _int_or(source.get("MRRC_SETUP_AP_POLL_S"), DEFAULT_POLL_S)
    cfg["settle_s"] = _int_or(source.get("MRRC_SETUP_AP_SETTLE_S"), DEFAULT_SETTLE_S)
    return cfg


class Supervisor:
    """One decision per tick. Every nmcli call goes through ``runner``.

    ``clock`` and ``sleep`` are injected for the same reason ``runner`` is: the
    interesting behaviour here is *when* things happen, and a test that waits
    thirty real minutes for a timeout is a test nobody runs.
    """

    def __init__(self, cfg: Optional[dict] = None,
                 runner: Optional[net_wifi.Runner] = None,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 log=None):
        self.cfg = dict(cfg if cfg is not None else settings())
        self.runner = runner
        self.clock = clock
        self.sleep = sleep
        self.log = log or logger
        self._mode = net_wifi.MODE_OFF
        self._since = 0.0
        self._gateway = ""
        self._url = ""
        self._deadline = 0.0
        self._timed_out = False        # per-process by design (see the docstring)
        self._granted: set = set()     # nonces that already bought a fresh window
        self._last_tick = 0.0
        self._elapsed = 0.0
        self._last_error_log = 0.0
        self._stopped = False

    def stop(self) -> None:
        """Ask the loop to finish. Called by the SIGTERM handler."""
        self._stopped = True

    # ── one tick ────────────────────────────────────────────────
    def tick(self) -> str:
        """Decide once, publish once. Returns the mode now in force."""
        now = self.clock()
        self._elapsed = max(0.0, now - self._last_tick) if self._last_tick else 0.0
        self._last_tick = now

        claim = net_wifi.read_wizard(self.cfg["state_dir"])
        if net_wifi.claim_is_fresh(claim, now):
            return self._wizard_tick(claim, now)

        if not net_wifi.nm_running(self.runner):
            # Raising an AP before NM is up just fails, and the failure would be
            # blamed on the radio instead of on the boot order.
            return self._publish(net_wifi.MODE_OFF, now,
                                 reason="waiting for NetworkManager")

        if net_wifi.has_uplink(self.runner):
            if net_wifi.hotspot_active(self.runner):
                self.log.info("an uplink came up — taking the setup hotspot down")
                net_wifi.stop_hotspot(self.runner)
            self._deadline = 0.0
            self._timed_out = False       # a box that had a network and lost it
            return self._publish(net_wifi.MODE_OFF, now, reason="uplink")

        if self._timed_out:
            return self._publish(net_wifi.MODE_OFF, now, reason="timeout")

        if net_wifi.hotspot_active(self.runner):
            if not self._deadline:
                # Found an AP this process did not raise (a service restart while
                # the window was open). Adopt a deadline: without one the timeout
                # can never fire and the open network stays up forever.
                self._deadline = now + self.cfg["timeout_s"]
                self.log.info("adopted an already-running setup hotspot — the window "
                              "closes in %d minutes", self.cfg["timeout_s"] // 60)
            if now >= self._deadline:
                self.log.warning(
                    "the %d-minute setup window closed with nobody onboard — "
                    "hotspot off (power-cycle the box to open it again)",
                    self.cfg["timeout_s"] // 60)
                net_wifi.stop_hotspot(self.runner)
                self._deadline = 0.0
                self._timed_out = True
                return self._publish(net_wifi.MODE_OFF, now, reason="timeout")
            return self._publish(net_wifi.MODE_HOTSPOT, now)

        return self._raise(now)

    def _raise(self, now: float) -> str:
        """Open the window, arm the timer, and tell the HDMI console about it."""
        result = net_wifi.start_hotspot(self.cfg["ssid"], self.cfg["ifname"],
                                        self.runner)
        if not result.ok:
            self._complain("could not start the setup hotspot: %s", result.detail)
            return self._publish(net_wifi.MODE_OFF, now,
                                 reason=f"start failed: {result.detail}"[:200])
        self._deadline = now + self.cfg["timeout_s"]
        self._gateway = net_wifi.ipv4_address(self.cfg["ifname"], self.runner)
        self._url = net_wifi.setup_url(self._gateway, self.cfg["web_port"])
        self.log.warning(
            "SETUP HOTSPOT OPEN — this box has no network. Join Wi-Fi %r with a "
            "phone or laptop and open %s — that page needs no password (accept "
            "the self-signed certificate warning). It closes in %d minutes, or "
            "the moment the box gets a network.",
            self.cfg["ssid"], self._url, self.cfg["timeout_s"] // 60)
        return self._publish(net_wifi.MODE_HOTSPOT, now)

    def _wizard_tick(self, claim, now: float) -> str:
        """Stand down while the server owns the radio (design D-3: one at a time).

        The server performs the AP→STA switch itself. Three consequences:

        * the window is *compensated* for the time spent standing down, so a slow
          switch cannot eat the operator's minutes;
        * a FAILED switch buys exactly one fresh window per nonce — that is what
          makes "try again" work without a power cycle, while a nonce that
          repeats cannot keep an open network alive forever;
        * the published mode is ``off`` while switching, because the hotspot
          genuinely is down then and the gate must not claim otherwise.
        """
        if claim.state == net_wifi.WIZARD_OK:
            self.log.info("the wizard joined %r (address %s) — hotspot stays down",
                          claim.ssid, claim.address or "not reported")
            self._deadline = 0.0
            self._timed_out = False
            self._gateway = claim.address
            self._url = (net_wifi.setup_url(claim.address, self.cfg["web_port"])
                         if claim.address else "")
            return self._publish(net_wifi.MODE_STA, now, reason="joined")

        if claim.state == net_wifi.WIZARD_FAILED:
            fresh = bool(claim.nonce) and claim.nonce not in self._granted
            if fresh:
                self._granted.add(claim.nonce)
            self._timed_out = False
            if not net_wifi.hotspot_active(self.runner):
                # The server tried to put the hotspot back and could not. Without
                # this the box sits unreachable with nobody able to say why.
                return self._raise(now)
            if fresh:
                self._deadline = now + self.cfg["timeout_s"]
                self.log.warning("joining %r failed (%s) — the hotspot is back and "
                                 "the window restarted", claim.ssid,
                                 claim.error or "no reason given")
            return self._publish(net_wifi.MODE_HOTSPOT, now, reason="retry")

        # WIZARD_SWITCHING: the server is mid-switch and heartbeating.
        if self._deadline:
            self._deadline += self._elapsed
        return self._publish(net_wifi.MODE_OFF, now, reason="wizard switching")

    # ── publishing and logging ──────────────────────────────────
    def _publish(self, mode: str, now: float, *, reason: str = "") -> str:
        """Write one heartbeat of ``state.json`` — the evidence the gate runs on.

        ``since`` survives across ticks while the mode is unchanged so the page
        can say how long the window has been open; the gateway and URL are kept
        for ``sta`` (that is the address to hand the operator next) and cleared
        for ``off`` (there is no address to advertise).
        """
        if mode != self._mode:
            self._since = now
            self._mode = mode
            if mode == net_wifi.MODE_OFF:
                self._gateway = ""
                self._url = ""
        net_wifi.write_state(net_wifi.ApState(
            mode=mode,
            ssid=self.cfg["ssid"] if mode == net_wifi.MODE_HOTSPOT else "",
            gateway=self._gateway,
            network=net_wifi.ap_network(self._gateway) if self._gateway else "",
            url=self._url,
            reason=reason,
            since=self._since,
            heartbeat=now,
            deadline=self._deadline if mode == net_wifi.MODE_HOTSPOT else 0.0,
        ), self.cfg["state_dir"])
        return mode

    def _complain(self, fmt: str, *args) -> None:
        """Log a repeating failure at most once a minute.

        A hotspot that cannot start is retried every tick, and the unit forwards
        to the HDMI console: five identical lines a minute would bury the one
        message the operator actually needs.
        """
        now = self.clock()
        if now - self._last_error_log < ERROR_LOG_GAP_S:
            return
        self._last_error_log = now
        self.log.error(fmt, *args)

    # ── start-up and shut-down ──────────────────────────────────
    def _wait_for_nm(self) -> None:
        """Give NetworkManager time to autoconnect before deciding anything.

        Deciding at t=0 is how a box that *would* have had WiFi five seconds
        later ends up broadcasting an open hotspot for half an hour: D-6's
        criterion is "no saved WiFi that connects", and "connects" needs time.
        """
        deadline = self.clock() + self.cfg["settle_s"]
        while not self._stopped and self.clock() < deadline:
            if not net_wifi.nm_running(self.runner):
                self.sleep(1.0)
                continue
            if net_wifi.has_uplink(self.runner):
                self.log.info("NetworkManager already has an uplink — no setup hotspot")
                return
            if not net_wifi.any_device_connecting(self.runner):
                # NM has settled on "nothing". One grace period, because a DHCP
                # handshake in flight does not show in the state column yet.
                self.sleep(GRACE_AFTER_IDLE_S)
                if net_wifi.has_uplink(self.runner):
                    self.log.info("an uplink came up during the settle grace")
                    return
                break
            self.sleep(1.0)

    def _shutdown(self) -> None:
        """Leave the radio the way we found it.

        A `systemctl stop` that left the hotspot up would keep an open network
        alive with no supervisor behind it. The heartbeat goes stale on its own
        (which closes the gate within 45 s); taking the AP down is the part that
        needs doing.
        """
        try:
            if net_wifi.hotspot_active(self.runner):
                net_wifi.stop_hotspot(self.runner)
            self._publish(net_wifi.MODE_OFF, self.clock(), reason="service stopped")
        except Exception as exc:                                  # noqa: BLE001
            self.log.warning("could not clean up on stop: %s", exc)

    def run(self) -> int:
        """The service body: settle, then decide once per poll interval."""
        net_wifi.ensure_state_dir(self.cfg["state_dir"])
        try:
            self._wait_for_nm()
        except Exception as exc:                                  # noqa: BLE001
            self.log.error("could not wait for NetworkManager: %s", exc)
        while not self._stopped:
            started = self.clock()
            try:
                self.tick()
            except Exception as exc:                              # noqa: BLE001
                # A resident service that dies on one bad nmcli answer stops
                # being able to open the window at all, and the only symptom the
                # operator sees is "no hotspot ever appeared".
                self.log.exception("setup-ap tick failed: %s", exc)
            nap = self.cfg["poll_s"] - (self.clock() - started)
            self.sleep(max(1.0, nap))
        self._shutdown()
        return 0


def _install_signals(supervisor: Supervisor) -> None:
    """SIGTERM must lead to a clean shutdown, not a default kill.

    systemd stops a service with SIGTERM; the default action would skip
    ``_shutdown()`` and leave the hotspot up. Installed in ``main()`` rather than
    in ``run()`` so that tests driving ``run()`` do not replace the interpreter's
    own SIGINT handling.
    """
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(signum, lambda *_args: supervisor.stop())
        except (ValueError, OSError, AttributeError, RuntimeError):
            pass        # not the main thread, or this platform lacks the signal


def main(argv: Optional[list] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] mrrc-setup-ap: %(message)s",
        stream=sys.stdout,
    )
    cfg = settings()
    supervisor = Supervisor(cfg)
    _install_signals(supervisor)
    logger.info("starting: ssid=%s ifname=%s window=%d min state_dir=%s",
                cfg["ssid"], cfg["ifname"], cfg["timeout_s"] // 60, cfg["state_dir"])
    return supervisor.run()


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **步骤 4：运行测试验证通过**

运行：`$PY -m unittest tests.test_setup_ap -v 2>&1 | tail -8`
预期：`OK`（约 35 项）

排障提示（按出现概率排）：
- `test_its_own_hotspot_is_not_an_uplink` 红在 `down_count == 1` ⇒ `has_uplink()` 没有减掉 AP 连接（回任务 1，`ap_names` 那一行）。
- `test_the_window_is_compensated_…` 红 ⇒ `_wizard_tick` 的 SWITCHING 分支漏了 `self._deadline += self._elapsed`，或者 `tick()` 顶部没有"先算 `_elapsed`、再读 claim"。
- `test_a_supervisor_restarted_mid_window_arms_a_deadline` 红在 `deadline == 0` ⇒ 缺"领养"分支（`if not self._deadline:`）。这是**写测试时才发现的缺口**，别省。
- `test_it_waits_while_networkmanager_is_still_working` **挂住不返回** ⇒ `Harness._sleep` 没有推进假时钟。`_wait_for_nm` 的循环条件是 `clock() < deadline`，sleep 不动时间就永远出不来。
- `test_a_repeating_failure_does_not_fill_the_console` 得到 20 条 ⇒ `_complain` 的限流没生效（`_last_error_log` 初值 0.0，假时钟从 1000.0 起，所以第一条一定会打，之后每 60 s 才一条）。

- [ ] **步骤 5：确认没有弄坏别的**

运行：`$PY -m unittest discover -s tests 2>&1 | tail -4`
预期：`FAILED (failures=1, skipped=1)`，唯一 `FAIL:` 是 `test_tls_trust_store`

- [ ] **步骤 6：SDD 检查 + Commit**

```bash
$PY .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git add linux/setup_ap.py tests/test_setup_ap.py
git commit -m "feat: mrrc-setup-ap 常驻判定——无上行才开热点、30 分钟自关且本次开机不重开"
```


---

## 任务 5：`server.py` — 闸门接线（常量、中间件放行、审计）

**文件：**
- 修改：`server.py`（三处：import、中间件常量区、SPA 兜底之前）
- 测试：`tests/test_server_setup_wizard.py`（创建）

**这一任务不加任何端点**，只加"谁能免口令进来"这一个判断，并且把它钉死。端点在任务 6/7，页面与它的路由在任务 8。这样拆是因为闸门是**安全边界**，它值得有自己的测试文件段落和自己的 commit。

- [ ] **步骤 1：编写失败的测试**

创建 `tests/test_server_setup_wizard.py`：

```python
"""The setup wizard's passwordless gate, and everything it must not widen.

The gate is the only unauthenticated write path this product has ever had, so
most of what is asserted here is a *negative*: which paths it covers, which
clients it accepts, and what happens when the evidence for it goes stale.

Design D-6 turns the window off by making the network disappear, so the two
halves of the gate are "the hotspot is demonstrably up right now" (a live
heartbeat in a file the supervisor owns) and "this request came from that
hotspot's own subnet". Either half alone is a hole: the subnet alone collides
with any LAN that happens to use 10.42.0.0/24, and the heartbeat alone would let
a client on the box's real network in.

Everything runs against a temp state directory. No NetworkManager, no radio, no
hotspot — the file *is* the evidence the server is allowed to use.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.responses import JSONResponse

import net_wifi
import server

HOTSPOT_CLIENT = "10.42.0.57"
LAN_CLIENT = "192.168.1.50"


class FakeUrl:
    def __init__(self, path, query=""):
        self.path = path
        self.query = query


class FakeClient:
    def __init__(self, host):
        self.host = host


class FakeRequest:
    """The slice of a Starlette request the wizard actually touches."""

    def __init__(self, path="/setup", method="GET", client=HOTSPOT_CLIENT,
                 cookies=None, body=None, headers=None):
        self.url = FakeUrl(path)
        self.method = method
        self.client = FakeClient(client) if client else None
        self.cookies = cookies or {}
        self.headers = headers or {}
        self._body = body if body is not None else {}

    async def json(self):
        return self._body


class GateCase(unittest.TestCase):
    """A temp state dir, wired into the server through the environment."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name)
        patcher = mock.patch.dict(
            os.environ, {"MRRC_SETUP_AP_STATE_DIR": str(self.state_dir)},
            clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def open_gate(self, network="10.42.0.0/24", age=0.0):
        """Publish what the supervisor publishes while the window is open."""
        net_wifi.write_state(net_wifi.ApState(
            mode=net_wifi.MODE_HOTSPOT, ssid="MRRC-Setup",
            gateway="10.42.0.1/24", network=network,
            url="https://10.42.0.1:8888/setup",
            heartbeat=time.time() - age), self.state_dir)

    def close_gate(self, mode=net_wifi.MODE_OFF):
        net_wifi.write_state(net_wifi.ApState(mode=mode), self.state_dir)


class GateHelperTests(GateCase):
    def test_open_for_a_client_on_the_hotspot(self):
        self.open_gate()
        self.assertTrue(server._setup_gate_open(FakeRequest(client=HOTSPOT_CLIENT)))

    def test_closed_for_a_client_on_another_subnet(self):
        """The collision case: 10.42.0.0/24 is an ordinary LAN range, so a box
        that also has a cable must not accept a LAN client here."""
        self.open_gate()
        self.assertFalse(server._setup_gate_open(FakeRequest(client=LAN_CLIENT)))

    def test_closed_for_a_sibling_subnet(self):
        self.open_gate()
        self.assertFalse(server._setup_gate_open(FakeRequest(client="10.43.0.57")))

    def test_closed_when_the_hotspot_is_down(self):
        self.close_gate()
        self.assertFalse(server._setup_gate_open(FakeRequest(client=HOTSPOT_CLIENT)))

    def test_closed_when_the_box_finished_onboarding(self):
        self.close_gate(mode=net_wifi.MODE_STA)
        self.assertFalse(server._setup_gate_open(FakeRequest(client=HOTSPOT_CLIENT)))

    def test_closed_when_there_is_no_state_file_at_all(self):
        """Every desktop install, and every box whose supervisor is not running."""
        self.assertFalse(server._setup_gate_open(FakeRequest(client=HOTSPOT_CLIENT)))

    def test_open_for_an_ipv4_mapped_client(self):
        """The box binds 0.0.0.0 today, but `_bind_dual_stack_socket` exists and a
        `::` bind reports an IPv4 client as `::ffff:10.42.0.57`. Without the
        unwrap every check "correctly" returns False and the wizard is
        unreachable with nothing in the log to explain it."""
        self.open_gate()
        self.assertTrue(server._setup_gate_open(
            FakeRequest(client="::ffff:10.42.0.57")))

    def test_closed_when_there_is_no_client_at_all(self):
        self.open_gate()
        self.assertFalse(server._setup_gate_open(FakeRequest(client=None)))

    def test_a_stale_heartbeat_closes_it(self):
        """A dead supervisor must not leave the door open. 45 s is the budget;
        this is five minutes."""
        self.open_gate(age=300.0)
        self.assertFalse(server._setup_gate_open(FakeRequest(client=HOTSPOT_CLIENT)))

    def test_the_state_directory_comes_from_the_environment(self):
        self.assertEqual(server._setup_ap_state_dir(), self.state_dir)


class AccessTests(GateCase):
    def test_the_gate_grants_access_and_says_so_in_the_log(self):
        """R5: every passwordless entry is logged with the address it came from.
        Without this the residual risk in D-6 (a neighbour wins the race) leaves
        no trace at all."""
        self.open_gate()
        with self.assertLogs("mrrc", level="WARNING") as captured:
            self.assertTrue(server._setup_access(FakeRequest(client=HOTSPOT_CLIENT)))
        self.assertTrue(any(HOTSPOT_CLIENT in line for line in captured.output),
                        captured.output)

    def test_an_admin_token_grants_access_without_the_gate(self):
        """An operator on the LAN with a real session can use the wizard too;
        the passwordless property comes only from the gate."""
        self.close_gate()
        with mock.patch.object(server, "_verify_auth", return_value=True), \
             mock.patch.object(server, "_is_listen_request", return_value=False):
            self.assertTrue(server._setup_access(FakeRequest(client=LAN_CLIENT)))

    def test_a_listen_only_token_is_refused(self):
        """The middleware's listen gate is skipped for these paths (an
        unauthenticated hotspot client has to get through it), so this is the
        only thing between a listen-only session and setting the box's password.
        Constraint 6 — missing it is a privilege escalation."""
        self.close_gate()
        with mock.patch.object(server, "_verify_auth", return_value=True), \
             mock.patch.object(server, "_is_listen_request", return_value=True):
            self.assertFalse(server._setup_access(FakeRequest(client=LAN_CLIENT)))

    def test_nothing_grants_access_with_no_gate_and_no_token(self):
        self.close_gate()
        with mock.patch.object(server, "_verify_auth", return_value=False):
            self.assertFalse(server._setup_access(FakeRequest(client=LAN_CLIENT)))

    def test_the_gate_wins_over_a_missing_token(self):
        self.open_gate()
        with mock.patch.object(server, "_verify_auth", return_value=False):
            self.assertTrue(server._setup_access(FakeRequest(client=HOTSPOT_CLIENT)))


class MiddlewareTests(GateCase):
    @staticmethod
    async def _pass_through(request):
        return JSONResponse({"reached": request.url.path})

    def _call(self, path, method="GET", client=LAN_CLIENT):
        return asyncio.run(server.auth_middleware(
            FakeRequest(path=path, method=method, client=client),
            self._pass_through))

    def test_the_gate_paths_reach_a_handler_with_no_cookie_at_all(self):
        """They have to: the client on the hotspot has never logged in. The
        handlers re-check the gate, so this pass-through cannot widen it."""
        self.close_gate()
        for path in sorted(server.SETUP_GATE_PATHS):
            with self.subTest(path=path):
                self.assertEqual(self._call(path).status_code, 200)

    def test_no_other_path_is_widened_by_the_gate_being_open(self):
        self.open_gate()
        for path in ("/", "/api/status", "/api/setup", "/api/health",
                     "/listen", "/api/cloud/state", "/api/recordings"):
            with self.subTest(path=path):
                self.assertNotEqual(self._call(path).status_code, 200)

    def test_the_wizard_writes_are_not_reachable_from_the_lan(self):
        """The whole point of gating by network path: with the hotspot down, a
        LAN client that never authenticated gets 401, not the handler."""
        self.close_gate()
        for path in ("/api/setup/wizard/password", "/api/setup/wizard/wifi"):
            with self.subTest(path=path):
                resp = self._call(path, method="POST")
                self.assertEqual(resp.status_code, 200,
                                 "the middleware passes it to the handler…")
        # …and the handler is what refuses it (asserted in task 6/7).


class GatePathShapeTests(unittest.TestCase):
    """The pass-through is a closed set, compared by equality."""

    def test_it_is_exactly_the_wizard(self):
        self.assertEqual(server.SETUP_GATE_PATHS, frozenset({
            "/setup",
            "/api/setup/wizard",
            "/api/setup/wizard/password",
            "/api/setup/wizard/wifi",
        }))

    def test_no_wildcard_or_prefix_form_sneaks_in(self):
        """`path.startswith("/setup")` would also admit `/setup-anything` and,
        worse, invite the next person to add a sibling route under the same
        prefix and inherit the hole."""
        for path in server.SETUP_GATE_PATHS:
            with self.subTest(path=path):
                self.assertFalse(path.endswith("/"))
                self.assertNotIn("*", path)
        source = Path(server.__file__).read_text(encoding="utf-8")
        self.assertIn("if path in SETUP_GATE_PATHS:", source)
        self.assertNotIn('path.startswith("/setup")', source)

    def test_the_writable_key_set_is_exactly_the_password(self):
        """What the passwordless window may write. Anything more — a config-file
        path, the transmit gate, a serial port — turns "let the operator set a
        password" into "let anybody on the hotspot reconfigure the box"."""
        self.assertEqual(server.SETUP_WRITABLE_KEYS,
                         frozenset({"MRRC_WEB_PASSWORD", "MRRC_AUTO_PASSWORD"}))

    def test_the_dangerous_keys_are_not_in_it(self):
        for key in ("MRRC_CONFIG_FILE", "MRRC_ALLOW_UNVERIFIED_TX",
                    "MRRC_WEB_HOST", "MRRC_WEB_PORT", "MRRC_SERIAL_PORT",
                    "MRRC_PTT_MAX_TX_SECONDS", "MRRC_SSL_CERT"):
            with self.subTest(key=key):
                self.assertNotIn(key, server.SETUP_WRITABLE_KEYS)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **步骤 2：运行测试验证失败**

运行：`$PY -m unittest tests.test_server_setup_wizard -v 2>&1 | tail -6`
预期：ERROR — `AttributeError: module 'server' has no attribute 'SETUP_GATE_PATHS'`

- [ ] **步骤 3：编写实现（三处修改）**

**3a. 加 import。** 在 `server.py` 的根模块 import 区（`import net_tls` 之后、`import spectrum_profile` 之前，保持字母序）：

```python
import net_tls
import net_wifi
import spectrum_profile
```

**3b. 加常量与中间件放行。** 找到这段（约 `server.py:2978-2984`）：

```python
# Paths that don't require authentication
PUBLIC_PATHS = {"/login", "/api/auth/login", "/favicon.png", "/manifest.json", "/sw.js"}
# WebSocket paths: must NOT be redirected — WebSocket can't follow 302.
# Let the WS handler check auth and close with proper code 4001.
WS_PATHS = {"/WSradio", "/WSspectrum", "/WSaudioRX", "/WSaudioTX"}
```

在它**之后**追加：

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
```

然后在 `auth_middleware` 里，找到这段（约 `server.py:2999-3003`）：

```python
    # WebSocket paths: pass through (WS handlers send code 4001 on auth failure
    # so the browser's handleAuthExpired() can act on it properly).
    if path in WS_PATHS:
        return await call_next(request)
```

在它**之后**、`# Check auth` 之前插入：

```python
    # Setup wizard: pass through so an unauthenticated hotspot client can reach
    # the handlers. It must come BEFORE the auth check (that is the point) and it
    # therefore also skips the listen-only 403 below — so _setup_access() refuses
    # a listen token itself. Equality, not startswith: a prefix here would widen
    # the hole to every path under it.
    if path in SETUP_GATE_PATHS:
        return await call_next(request)
```

**3c. 加闸门助手。** 在文件末尾那条 SPA 兜底注册**之前**（即 `app.add_api_route("/{path:path}", serve_static, methods=["GET"])` 上方，与它之间保留那段解释路由顺序的注释）插入：

```python
# ── Setup access point wizard (design D-6 / D-7.5) ──────────────────
#
# The box opens an *open* hotspot when it has no other way to be reached, and
# for as long as that hotspot is up a client on it may set the web password and
# pick a WiFi network without a token. This is the only unauthenticated write
# path in the product, so the gate is deliberately boring: it reads one small
# JSON file that linux/setup_ap.py rewrites every few seconds, and it needs two
# independent facts to agree before it opens.

def _setup_ap_state_dir() -> Path:
    """Where the supervisor publishes its state (shared with linux/setup_ap.py)."""
    return net_wifi.ap_settings()["state_dir"]


def _setup_gate_open(request: Request) -> bool:
    """Whether this request arrives over the box's own live open hotspot.

    Both halves come from net_wifi.gate_is_open: a heartbeat fresh enough to
    prove the supervisor is running the AP *now*, and a client address inside
    that AP's subnet. The second half is what keeps a LAN client out when
    10.42.0.0/24 happens to be somebody's real network; the first is what closes
    the door when the supervisor dies, without anyone having to clear a flag.
    """
    client = request.client.host if request.client else ""
    return net_wifi.gate_is_open(net_wifi.read_state(_setup_ap_state_dir()),
                                 client, time.time())


def _setup_access(request: Request) -> bool:
    """Gate + audit for every wizard handler (design R5).

    Two ways in and only two:

    * over the open hotspot — passwordless, and logged at WARNING with the
      address it came from, because D-6's residual risk is that a neighbour
      inside WiFi range wins the race. If that ever happens, this line is the
      only evidence there will be;
    * with a FULL admin token. A listen-only token is refused here rather than in
      the middleware, because the middleware's pass-through for
      SETUP_GATE_PATHS necessarily skips the listen-role 403 (an
      unauthenticated hotspot client cannot be gated on a role it does not
      have). Moving that check into the handler is what keeps the pass-through
      from becoming a privilege escalation.
    """
    if _setup_gate_open(request):
        logger.warning(
            "setup wizard: passwordless access from %s (the open setup hotspot is "
            "up; this window closes with it)",
            request.client.host if request.client else "?",
        )
        return True
    return _verify_auth(request) and not _is_listen_request(request)
```

> **注意 `_setup_ap_state_dir()` 用的是 `net_wifi.ap_settings()["state_dir"]`**，不是自己再读一遍环境变量。SSIDs、目录、端口三样东西两个进程必须一致，读两处就会漂（任务 3 的 `ap_settings` docstring 里写了这条理由）。

- [ ] **步骤 4：运行测试验证通过**

运行：`$PY -m unittest tests.test_server_setup_wizard -v 2>&1 | tail -6`
预期：`OK`（约 20 项）

排障提示：
- `test_the_gate_paths_reach_a_handler_with_no_cookie_at_all` 红在 302/401 ⇒ 中间件的插入位置在 `_verify_auth` **之后**了；必须在它之前。
- `test_a_listen_only_token_is_refused` 红 ⇒ `_setup_access` 的 token 分支写成了 `return _verify_auth(request)`，漏了 `and not _is_listen_request(request)`。
- `test_open_for_an_ipv4_mapped_client` 红 ⇒ 不是 server 的问题，是 `net_wifi.client_ip` 漏了 `ipv4_mapped` 解包（回任务 3）。
- `assertLogs("mrrc", …)` 抓不到 ⇒ `server.py` 的 logger 名是 `"mrrc"`（`server.py:137`），不要写成 `"server"` 或 `server.logger.name` 之外的东西。

- [ ] **步骤 5：确认没有弄坏别的**

运行：`$PY -m unittest discover -s tests 2>&1 | tail -4`
预期：`FAILED (failures=1, skipped=1)`，唯一 `FAIL:` 是 `test_tls_trust_store`

特别看住 `tests/test_listen_only.py`（它直接驱动 `server.auth_middleware`）与 `tests/test_cloud_endpoints.py::ServerRouteOrderTests`（路由顺序）。

- [ ] **步骤 6：SDD 检查 + Commit**

```bash
$PY .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git add server.py tests/test_server_setup_wizard.py
git commit -m "feat: 向导的免口令闸门——活心跳 + 热点网段，listen 令牌在处理函数里被拒"
```


---

## 任务 6：`server.py` — 向导状态 + 设口令

**文件：**
- 修改：`server.py`（在任务 5 的助手之后追加；并在 `SETUP_WRITABLE_KEYS` 旁加两个常量）
- 测试：`tests/test_server_setup_wizard.py`（追加）

**这一任务的两条硬要求：**
1. **写入口令必须走既有通道**（规格 D-7.5）：`first_run.update_env_file(_config_file_path(), updates)`，与 `/api/setup`（`server.py:3220` 附近）逐字同一个调用。不在这里长第二个写入者。
2. **写入的键集必须恰好是 `SETUP_WRITABLE_KEYS`**。免口令窗口能改的东西越多，D-6 那条"残留风险"（邻居抢着设口令）就越严重——所以除了口令与"口令不再是自动生成的"这个标记，什么都不能写。

- [ ] **步骤 1：编写失败的测试**

在 `tests/test_server_setup_wizard.py` 末尾（`if __name__` 之前）追加，并在文件顶部 import 区补 `import json`：

```python
class WizardStateTests(GateCase):
    def _get(self, client=HOTSPOT_CLIENT):
        return asyncio.run(server.api_setup_wizard(
            FakeRequest(path="/api/setup/wizard", client=client)))

    def test_401_when_the_gate_is_closed_and_there_is_no_token(self):
        self.close_gate()
        with mock.patch.object(server, "_verify_auth", return_value=False):
            self.assertEqual(self._get(client=LAN_CLIENT).status_code, 401)

    def test_the_payload_is_a_closed_schema(self):
        """A closed schema is what keeps a password out of a response body: adding
        a field here has to be a deliberate act that turns a test red."""
        self.open_gate()
        payload = json.loads(self._get().body)
        self.assertEqual(set(payload), {"gate", "hotspot", "switch",
                                        "auto_password", "radio_model", "web_port"})
        self.assertEqual(set(payload["hotspot"]),
                         {"mode", "ssid", "url", "deadline", "remaining_s",
                          "reason"})
        self.assertEqual(set(payload["switch"]),
                         {"state", "ssid", "error", "address"})

    def test_it_reports_the_window_the_page_has_to_render(self):
        self.open_gate()
        payload = json.loads(self._get().body)
        self.assertEqual(payload["gate"], "hotspot")
        self.assertEqual(payload["hotspot"]["mode"], net_wifi.MODE_HOTSPOT)
        self.assertEqual(payload["hotspot"]["ssid"], "MRRC-Setup")
        self.assertEqual(payload["hotspot"]["url"], "https://10.42.0.1:8888/setup")
        self.assertGreater(payload["hotspot"]["deadline"], 0)
        self.assertEqual(payload["switch"]["state"], "", "no switch has been asked for")

    def test_the_remaining_time_is_skew_free(self):
        """Both halves come from the box's clock, in the same publish. A phone's
        clock can be minutes off, and `deadline - Date.now()` on the client would
        then show a window that is already closed — or one that never closes."""
        self.open_gate()
        net_wifi.write_state(net_wifi.ApState(
            mode=net_wifi.MODE_HOTSPOT, ssid="MRRC-Setup",
            gateway="10.42.0.1/24", network="10.42.0.0/24",
            url="https://10.42.0.1:8888/setup",
            since=time.time() - 600, heartbeat=5000.0, deadline=6800.0),
            self.state_dir)
        payload = json.loads(self._get().body)
        self.assertEqual(payload["hotspot"]["remaining_s"], 1800.0)

    def test_a_box_that_is_not_in_a_window_reports_zero_remaining(self):
        self.close_gate()
        with mock.patch.object(server, "_verify_auth", return_value=True), \
             mock.patch.object(server, "_is_listen_request", return_value=False):
            payload = json.loads(self._get(client=LAN_CLIENT).body)
        self.assertEqual(payload["hotspot"]["remaining_s"], 0.0)

    def test_a_token_entry_is_labelled_as_such(self):
        """The page tells the operator whether it is standing in the open window
        or in an ordinary logged-in session; guessing wrong would tell a LAN
        admin that the box is still unconfigured."""
        self.close_gate()
        with mock.patch.object(server, "_verify_auth", return_value=True), \
             mock.patch.object(server, "_is_listen_request", return_value=False):
            payload = json.loads(self._get(client=LAN_CLIENT).body)
        self.assertEqual(payload["gate"], "token")

    def test_it_carries_a_failed_switch_back_to_the_page(self):
        """Design §6: a failure must not be silent. The server wrote the reason
        into the claim; this is how the page gets it after the phone rejoins."""
        self.open_gate()
        net_wifi.write_wizard(net_wifi.WizardClaim(
            action="connect", ssid="Home", state=net_wifi.WIZARD_FAILED,
            error="Secrets were required", nonce="abc",
            heartbeat=time.time()), self.state_dir)
        payload = json.loads(self._get().body)
        self.assertEqual(payload["switch"]["state"], net_wifi.WIZARD_FAILED)
        self.assertEqual(payload["switch"]["ssid"], "Home")
        self.assertEqual(payload["switch"]["error"], "Secrets were required")

    def test_it_says_whether_the_password_is_still_the_generated_one(self):
        with mock.patch.dict(os.environ, {"MRRC_AUTO_PASSWORD": "1"}, clear=False):
            self.open_gate()
            self.assertTrue(json.loads(self._get().body)["auto_password"])
        with mock.patch.dict(os.environ, {"MRRC_AUTO_PASSWORD": ""}, clear=False):
            self.assertTrue(json.loads(self._get().body)["auto_password"] is False)


class PasswordTests(GateCase):
    """The passwordless window's one and only write."""

    GOOD = "a-good-long-password"

    def setUp(self):
        super().setUp()
        self.env_path = self.state_dir / "mrrc.env"
        self._original_password = server.WEB_PASSWORD
        self._original_env = dict(os.environ)

    def tearDown(self):
        server.WEB_PASSWORD = self._original_password
        os.environ.clear()
        os.environ.update(self._original_env)

    def _post(self, body, client=HOTSPOT_CLIENT, gate=True):
        if gate:
            self.open_gate()
        else:
            self.close_gate()
        with mock.patch.object(server.first_run, "update_env_file") as writer, \
             mock.patch.object(server, "_config_file_path",
                               return_value=self.env_path):
            resp = asyncio.run(server.api_setup_wizard_password(FakeRequest(
                path="/api/setup/wizard/password", method="POST",
                client=client, body=body)))
        return resp, writer

    def test_it_writes_through_the_existing_channel(self):
        """Design D-7.5: the same function and the same file the connection
        dialog already uses. A second writer here is what D-8 exists to undo."""
        resp, writer = self._post({"password": self.GOOD, "confirm": self.GOOD})
        self.assertEqual(resp.status_code, 200)
        writer.assert_called_once()
        path, updates = writer.call_args[0]
        self.assertEqual(path, self.env_path)
        self.assertEqual(updates, {"MRRC_WEB_PASSWORD": self.GOOD,
                                   "MRRC_AUTO_PASSWORD": ""})

    def test_it_writes_no_key_outside_the_allowed_set(self):
        """Everything else in the body is ignored, not honoured: a caller on an
        open hotspot cannot smuggle a config path or a transmit gate through."""
        resp, writer = self._post({
            "password": self.GOOD,
            "radio_model": "ic7300",
            "MRRC_CONFIG_FILE": "/tmp/evil.env",
            "MRRC_ALLOW_UNVERIFIED_TX": "1",
            "MRRC_WEB_PORT": "1",
        })
        self.assertEqual(resp.status_code, 200)
        _, updates = writer.call_args[0]
        self.assertLessEqual(set(updates), server.SETUP_WRITABLE_KEYS)

    def test_the_new_password_works_without_a_restart(self):
        """A restart here would drop the operator mid-wizard, and the next step
        takes the network away anyway. Same in-process rebind
        `_ensure_strong_password` already does; the env write covers the next boot."""
        self._post({"password": self.GOOD})
        self.assertTrue(server._password_matches(self.GOOD))
        self.assertEqual(os.environ["MRRC_WEB_PASSWORD"], self.GOOD)

    def test_the_auto_password_flag_is_cleared_in_this_process_too(self):
        """Otherwise the page keeps believing the password is the generated one
        and refuses to let the operator switch networks."""
        with mock.patch.dict(os.environ, {"MRRC_AUTO_PASSWORD": "1"}, clear=False):
            self._post({"password": self.GOOD})
            self.assertEqual(os.environ["MRRC_AUTO_PASSWORD"], "")
            self.assertFalse(server._setup_auto_password())

    def test_a_short_password_is_refused(self):
        """8 is the floor the existing /api/setup already enforces; the wizard
        must not be the softer door."""
        for bad in ("", "abc", "1234567"):
            with self.subTest(bad=bad):
                resp, writer = self._post({"password": bad})
                self.assertEqual(resp.status_code, 400)
                writer.assert_not_called()

    def test_a_mismatched_confirmation_is_refused(self):
        resp, writer = self._post({"password": self.GOOD,
                                   "confirm": "a-different-password"})
        self.assertEqual(resp.status_code, 400)
        writer.assert_not_called()

    def test_a_confirmation_may_be_omitted(self):
        """The page sends one; an operator curling from the HDMI console should
        still get in with a single field."""
        resp, writer = self._post({"password": self.GOOD})
        self.assertEqual(resp.status_code, 200)
        writer.assert_called_once()

    def test_surrounding_whitespace_is_stripped(self):
        """A phone keyboard appends spaces; storing one would make the password
        the operator typed impossible to reproduce at the login page."""
        self._post({"password": f"  {self.GOOD}  "})
        self.assertTrue(server._password_matches(self.GOOD))

    def test_a_write_failure_is_a_500_not_a_silent_success(self):
        """A read-only env file is a real failure mode on a box (a full /opt
        partition), and 'password set' would be a lie the operator acts on."""
        self.open_gate()
        with mock.patch.object(server.first_run, "update_env_file",
                               side_effect=OSError("read-only file system")), \
             mock.patch.object(server, "_config_file_path",
                               return_value=self.env_path):
            resp = asyncio.run(server.api_setup_wizard_password(FakeRequest(
                method="POST", body={"password": self.GOOD})))
        self.assertEqual(resp.status_code, 500)
        self.assertFalse(server._password_matches(self.GOOD),
                         "the in-process password must not change when the write failed")

    def test_the_gate_is_required(self):
        resp, writer = self._post({"password": self.GOOD},
                                  client=LAN_CLIENT, gate=False)
        self.assertEqual(resp.status_code, 401)
        writer.assert_not_called()

    def test_a_listen_only_token_is_refused_with_403(self):
        """Constraint 6, end to end: a real listen session (token in _auth_tokens
        *and* _listen_tokens) must not be able to set the box's password."""
        self.close_gate()
        token = server._make_auth_token()
        server._auth_tokens.add(token)
        server._listen_tokens.add(token)
        self.addCleanup(server._auth_tokens.discard, token)
        self.addCleanup(server._listen_tokens.discard, token)
        with mock.patch.object(server.first_run, "update_env_file") as writer:
            resp = asyncio.run(server.api_setup_wizard_password(FakeRequest(
                path="/api/setup/wizard/password", method="POST",
                client=LAN_CLIENT, cookies={server.AUTH_COOKIE: token},
                body={"password": self.GOOD})))
        self.assertEqual(resp.status_code, 403)
        writer.assert_not_called()

    def test_the_password_never_reaches_the_log(self):
        """SDD `support-bundle-privacy`. The audit line names the address, not the
        credential; log files are collected into support bundles."""
        self.open_gate()
        with self.assertLogs("mrrc", level="DEBUG") as captured, \
             mock.patch.object(server.first_run, "update_env_file"), \
             mock.patch.object(server, "_config_file_path",
                               return_value=self.env_path):
            asyncio.run(server.api_setup_wizard_password(FakeRequest(
                method="POST", body={"password": "hunter2hunter2"})))
        for line in captured.output:
            self.assertNotIn("hunter2hunter2", line)

    def test_a_malformed_body_is_a_400_not_a_500(self):
        """A captive-portal probe or a browser preflight posts something that is
        not JSON; that is a bad request, not a server fault."""
        self.open_gate()
        request = FakeRequest(method="POST")

        async def not_json():
            raise ValueError("no json here")

        request.json = not_json
        with mock.patch.object(server.first_run, "update_env_file"):
            resp = asyncio.run(server.api_setup_wizard_password(request))
        self.assertEqual(resp.status_code, 400)


class WizardSourceGuardTests(unittest.TestCase):
    """Source-level, so the guard does not depend on which branch a test drove.

    Same shape as `test_unverified_tx_gate` and `test_tls_trust_store`: read the
    code, assert the dangerous thing is absent, and assert the scan actually
    found the block it was looking at.
    """

    @staticmethod
    def wizard_source() -> str:
        source = Path(server.__file__).read_text(encoding="utf-8")
        start = source.index("# ── Setup access point wizard")
        return source[start:]

    def test_the_scan_found_the_block(self):
        wizard = self.wizard_source()
        self.assertIn("MRRC_WEB_PASSWORD", wizard)
        self.assertIn("_setup_access", wizard)

    def test_the_wizard_never_mentions_a_forbidden_key(self):
        """D-7 (MRRC_CONFIG_FILE is D-11's payload) and D-5 (the transmit gate),
        plus the fields that belong to the management page and not to an open
        hotspot."""
        wizard = self.wizard_source()
        for key in ("MRRC_CONFIG_FILE", "MRRC_ALLOW_UNVERIFIED_TX",
                    "MRRC_WEB_HOST", "MRRC_WEB_PORT", "MRRC_SERIAL_PORT",
                    "MRRC_PTT_MAX_TX_SECONDS", "MRRC_SSL_CERT",
                    "MRRC_NO_CONFIG_FILE"):
            with self.subTest(key=key):
                self.assertNotIn(key, wizard)

    def test_the_wizard_never_restarts_the_service(self):
        """A restart drops every connected client — including the operator who is
        mid-wizard on a hotspot that is about to disappear."""
        self.assertNotIn("_schedule_restart", self.wizard_source())

    def test_the_wizard_never_touches_the_env_file_directly(self):
        """One writer (D-7.5 / D-8). Only first_run.update_env_file may appear."""
        wizard = self.wizard_source()
        self.assertNotIn("write_text", wizard)
        self.assertNotIn("_write_config", wizard)
        self.assertNotIn("os.environ[\"MRRC_CONFIG_FILE\"]", wizard)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`$PY -m unittest tests.test_server_setup_wizard -v 2>&1 | tail -6`
预期：ERROR — `AttributeError: module 'server' has no attribute 'api_setup_wizard'`（以及 `api_setup_wizard_password` / `_setup_auto_password`），并且 `WizardSourceGuardTests.test_the_scan_found_the_block` 因为块里没有 `MRRC_WEB_PASSWORD` 而红。

- [ ] **步骤 3：编写实现**

**3a. 加两个常量**，紧跟任务 5 的 `SETUP_WRITABLE_KEYS` 之后：

```python
# 8 is the floor the existing POST /api/setup enforces. The wizard must not be
# the softer door into the same key.
MIN_SETUP_PASSWORD_LEN = 8
# A WPA-PSK shorter than 8 is not a valid passphrase, so this is a typo check
# that saves a 20-second round trip through "AP down → join fails → AP back up".
MIN_WIFI_PSK_LEN = 8
```

**3b. 加端点**，紧跟任务 5 的 `_setup_access()` 之后：

```python
def _setup_auto_password() -> bool:
    """Whether the password in force is still one the box invented itself.

    `linux/first_run.py`'s apply_first_run() and server.py's own
    _ensure_strong_password() both set MRRC_AUTO_PASSWORD=1 when they generate a
    password nobody has been told. That is the state a freshly flashed box is in,
    and it is why the wizard takes a password *before* it will switch networks:
    after the switch the hotspot — and with it the passwordless window — is gone,
    and an operator who never set one is locked out of their own box with only
    HDMI or `mrrc-show-password` left.
    """
    return os.environ.get("MRRC_AUTO_PASSWORD", "") == "1"


@app.get("/api/setup/wizard", include_in_schema=False)
async def api_setup_wizard(request: Request):
    """Everything the wizard page renders: the window, the switch, the model.

    The payload is a closed schema (a test asserts the exact key set) because a
    response body is the one place a credential could leak by accident, and the
    support-bundle redaction pass never sees it.
    """
    if not _setup_access(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    state_dir = _setup_ap_state_dir()
    state = net_wifi.read_state(state_dir)
    claim = net_wifi.read_wizard(state_dir)
    return JSONResponse({
        "gate": "hotspot" if _setup_gate_open(request) else "token",
        "hotspot": {
            "mode": state.mode,
            "ssid": state.ssid,
            "url": state.url,
            "deadline": state.deadline,
            # Skew-free by construction: both values are the box's clock, taken
            # in the same publish. A phone's clock can be minutes off, and
            # `deadline - Date.now()` in the browser would then show a window
            # that is already closed, or one that never closes.
            "remaining_s": (max(0.0, state.deadline - state.heartbeat)
                            if state.deadline else 0.0),
            "reason": state.reason,
        },
        "switch": {
            "state": claim.state,
            "ssid": claim.ssid,
            "error": claim.error,
            "address": claim.address,
        },
        "auto_password": _setup_auto_password(),
        "radio_model": RADIO_MODEL,
        "web_port": WEB_PORT,
    })


@app.post("/api/setup/wizard/password", include_in_schema=False)
async def api_setup_wizard_password(request: Request):
    """Set the web login password from the open-hotspot window.

    Writes through the SAME channel the connection dialog uses —
    ``first_run.update_env_file(_config_file_path(), …)`` — because design D-7.5
    established that this channel already exists and is already the writer of
    MRRC_WEB_PASSWORD. Growing a second writer here is precisely what the
    config-layer unification (D-8) is trying to undo.

    No restart. The password is rebound in this process exactly the way
    _ensure_strong_password does, so the operator can log in immediately, and the
    env write is what the next boot reads. Restarting would drop them in the
    middle of the wizard, and the next step takes the network away anyway.
    """
    global WEB_PASSWORD
    if not _setup_access(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:                                        # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}
    password = str(body.get("password", "") or "").strip()
    confirm = str(body.get("confirm", "") or "").strip()
    if len(password) < MIN_SETUP_PASSWORD_LEN:
        return JSONResponse(
            {"error": f"password too short (min {MIN_SETUP_PASSWORD_LEN})"},
            status_code=400)
    if confirm and password != confirm:
        return JSONResponse({"error": "passwords do not match"}, status_code=400)

    updates = {"MRRC_WEB_PASSWORD": password, "MRRC_AUTO_PASSWORD": ""}
    try:
        first_run.update_env_file(_config_file_path(), updates)
    except OSError as exc:
        logger.error("setup wizard: could not write the password: %s", exc)
        return JSONResponse({"error": "write failed"}, status_code=500)

    WEB_PASSWORD = password
    os.environ["MRRC_WEB_PASSWORD"] = password
    os.environ["MRRC_AUTO_PASSWORD"] = ""
    # The audit line names the address, never the credential: log files are
    # collected into support bundles (SDD support-bundle-privacy).
    logger.warning("setup wizard: the web password was set from %s",
                   request.client.host if request.client else "?")
    return JSONResponse({"ok": True})
```

- [ ] **步骤 4：运行测试验证通过**

运行：`$PY -m unittest tests.test_server_setup_wizard -v 2>&1 | tail -6`
预期：`OK`（约 40 项）

排障提示：
- `test_the_auto_password_flag_is_cleared_in_this_process_too` 红 ⇒ 只写了 env 文件里的 `MRRC_AUTO_PASSWORD=`，忘了 `os.environ["MRRC_AUTO_PASSWORD"] = ""`。**这一条会让页面卡住**：设完口令仍被当成"口令还是自动生成的"，于是拒绝切网。
- `test_a_write_failure_is_a_500_not_a_silent_success` 红在"进程内口令也变了" ⇒ 内存重绑必须在 `update_env_file` **成功之后**。
- `test_the_wizard_never_restarts_the_service` 红 ⇒ 别调 `_schedule_restart()`。向导期间重启 = 把操作者踢下线。
- `PasswordTests` 污染了别的测试 ⇒ `tearDown` 必须还原 `server.WEB_PASSWORD` 与 `os.environ`（本任务的基类已经给了；改它之前先想清楚）。

- [ ] **步骤 5：确认没有弄坏别的**

运行：`$PY -m unittest discover -s tests 2>&1 | tail -4`
预期：`FAILED (failures=1, skipped=1)`，唯一 `FAIL:` 是 `test_tls_trust_store`

特别看住 `tests/test_server_setup.py`（既有 `/api/setup` 的写入语义）与 `tests/test_server_first_run.py`（自动口令横幅）。

- [ ] **步骤 6：SDD 检查 + Commit**

```bash
$PY .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git add server.py tests/test_server_setup_wizard.py
git commit -m "feat: 向导设口令——复用既有写入通道、只写两个键、不重启服务"
```


---

## 任务 7：`server.py` — 扫描 + 切网（AP → STA）

**文件：**
- 修改：`server.py`（在任务 6 的端点之后追加）
- 测试：`tests/test_server_setup_wizard.py`（追加）

**这一任务的形状完全由一个物理事实决定：单射频（规格 D-3）。** 切网的那一瞬间，操作者手里那台手机**就不再连着盒子了**。所以：

1. **回答必须先发出去**，切换在后台线程里做，并且留一点前置延时让响应真的上了线。
2. **失败不能静默**（规格 §6）。线程把热点**重新拉起来**，并把原因写进 `wizard.json`；手机重新连上热点后，页面从那里读出原因。
3. 切换期间要**持续心跳**那个 claim。`nmcli device wifi connect` 会阻塞到关联完成，慢的 AP 上要几十秒——超过守护进程 45 s 的 stale 预算，它就会把射频抢回去，两个进程对打，而手机跟谁都断着。
4. **"先设口令"必须在这里挡住**（约束 7）。切网之后窗口就永久消失了。

- [ ] **步骤 1：编写失败的测试**

在 `tests/test_server_setup_wizard.py` 末尾（`WizardSourceGuardTests` 之前或之后都行，`if __name__` 之前）追加：

```python
class WifiScanTests(GateCase):
    def _scan(self, client=HOTSPOT_CLIENT):
        return asyncio.run(server.api_setup_wizard_wifi_scan(
            FakeRequest(path="/api/setup/wizard/wifi", client=client)))

    def test_401_without_the_gate_or_a_token(self):
        self.close_gate()
        with mock.patch.object(server, "_verify_auth", return_value=False):
            self.assertEqual(self._scan(client=LAN_CLIENT).status_code, 401)

    def test_networks_come_from_net_wifi(self):
        self.open_gate()
        found = [{"ssid": "Home", "signal": 80, "security": "WPA2",
                  "protected": True}]
        with mock.patch.object(server.net_wifi, "scan_wifi", return_value=found):
            payload = json.loads(self._scan().body)
        self.assertEqual(payload["networks"], found)

    def test_a_scan_failure_is_an_empty_list_not_a_500(self):
        """No WiFi device (a kernel that lost the SDIO driver — design §19 red
        line 3) must read as 'nothing found', with the reason in the journal."""
        self.open_gate()
        with mock.patch.object(server.net_wifi, "scan_wifi", return_value=[]):
            self.assertEqual(json.loads(self._scan().body)["networks"], [])

    def test_the_scan_does_not_run_on_the_event_loop(self):
        """A rescan takes seconds. On the loop it would stall the spectrum and
        audio WebSockets of every client on the box for the duration."""
        source = Path(server.__file__).read_text(encoding="utf-8")
        start = source.index("async def api_setup_wizard_wifi_scan")
        self.assertIn("asyncio.to_thread", source[start:start + 900])


class WifiSwitchTests(GateCase):
    """`_perform_wifi_switch`, driven directly: it is the part that runs after the
    HTTP answer is gone, so no request object can observe it."""

    PSK = "hunter2hunter2"

    def runner(self, connect_ok=True, hotspot_ok=True,
               address="192.168.1.77/24"):
        calls = []

        def run(argv):
            joined = " ".join(argv)
            calls.append(joined)
            if "device wifi connect" in joined:
                if connect_ok:
                    return net_wifi.NmResult(0, "successfully activated", "")
                # nmcli echoing the PSK back is not something it does today; the
                # fixture says it does so that the scrubbing is actually tested.
                return net_wifi.NmResult(
                    1, "", "Error: Connection activation failed: (7) Secrets were "
                           f"required, but not provided (psk={self.PSK})")
            if "-g IP4.ADDRESS device show" in joined:
                return net_wifi.NmResult(0, address + "\n")
            if "device wifi hotspot" in joined:
                if hotspot_ok:
                    return net_wifi.NmResult(0, "activated", "")
                return net_wifi.NmResult(1, "", "rfkill is blocking it")
            if "-f NAME,TYPE connection show --active" in joined:
                return net_wifi.NmResult(0, "Hotspot:802-11-wireless\n")
            if "802-11-wireless.mode" in joined:
                return net_wifi.NmResult(0, "ap\n")
            return net_wifi.NmResult(0, "", "")

        return run, calls

    def switch(self, run, ssid="Home", psk=None):
        server._perform_wifi_switch(
            ssid, self.PSK if psk is None else psk, "nonce-1", self.state_dir,
            runner=run, lead_s=0.0, sleep=lambda _s: None)
        return net_wifi.read_wizard(self.state_dir)

    def test_a_successful_switch_reports_the_new_address(self):
        run, calls = self.runner()
        claim = self.switch(run)
        self.assertEqual(claim.state, net_wifi.WIZARD_OK)
        self.assertEqual(claim.address, "192.168.1.77/24")
        self.assertEqual(claim.error, "")
        self.assertTrue(any("device wifi connect Home password" in c for c in calls))

    def test_the_hotspot_is_taken_down_before_the_join(self):
        """One radio. Joining while the AP is still up either fails or leaves the
        box broadcasting an open network nobody needs any more."""
        run, calls = self.runner()
        self.switch(run)
        down = next(i for i, c in enumerate(calls) if "connection down" in c)
        join = next(i for i, c in enumerate(calls) if "device wifi connect" in c)
        self.assertLess(down, join)

    def test_a_failed_join_puts_the_hotspot_back(self):
        """Design §6, the negative case that must not be skipped: without this the
        box is left with no AP and no uplink, and the operator's phone is sitting
        on a network that no longer exists."""
        run, calls = self.runner(connect_ok=False)
        claim = self.switch(run)
        self.assertEqual(claim.state, net_wifi.WIZARD_FAILED)
        self.assertTrue(any("device wifi hotspot" in c for c in calls))

    def test_a_failed_join_reports_why(self):
        run, _ = self.runner(connect_ok=False)
        self.assertIn("Secrets were required", self.switch(run).error)

    def test_the_stored_reason_is_bounded(self):
        """It ends up in a JSON file and then on a phone screen."""
        run, _ = self.runner(connect_ok=False)
        self.assertLessEqual(len(self.switch(run).error), 300)

    def test_the_psk_is_scrubbed_out_of_the_stored_reason(self):
        run, _ = self.runner(connect_ok=False)
        claim = self.switch(run)
        self.assertNotIn(self.PSK, claim.error)
        self.assertIn(net_wifi.REDACTED, claim.error)

    def test_the_psk_never_lands_on_disk(self):
        """SDD support-bundle-privacy, asserted on the bytes rather than on the
        code path: neither mailbox may contain the credential, whatever nmcli did
        or whatever a future edit forwards."""
        run, _ = self.runner(connect_ok=False)
        self.switch(run)
        for name in (net_wifi.STATE_NAME, net_wifi.WIZARD_NAME):
            path = self.state_dir / name
            with self.subTest(file=name):
                if path.exists():
                    self.assertNotIn(self.PSK, path.read_text(encoding="utf-8"))

    def test_the_psk_never_reaches_the_log(self):
        run, _ = self.runner(connect_ok=False)
        with self.assertLogs("mrrc", level="DEBUG") as captured:
            self.switch(run)
        for line in captured.output:
            self.assertNotIn(self.PSK, line)

    def test_the_nonce_comes_back_so_one_window_can_be_granted(self):
        """The supervisor grants exactly one fresh window per nonce (task 4); a
        claim without one leaves the operator with no retry."""
        run, _ = self.runner(connect_ok=False)
        self.assertEqual(self.switch(run).nonce, "nonce-1")

    def test_a_hotspot_that_will_not_come_back_is_still_reported(self):
        """The worst case: no AP, no uplink. The claim is the only record, and it
        must say the join failed rather than look like nothing happened."""
        claim = self.switch(self.runner(connect_ok=False, hotspot_ok=False)[0])
        self.assertEqual(claim.state, net_wifi.WIZARD_FAILED)

    def test_a_runner_that_raises_is_a_failure_not_a_crash(self):
        """This runs on a daemon thread: an exception here dies silently and
        leaves the claim stuck at `switching`, which the supervisor reads as
        'the server is still working' for 45 seconds."""
        def explode(argv):
            raise RuntimeError("nmcli exploded")

        claim = self.switch(explode)
        self.assertEqual(claim.state, net_wifi.WIZARD_FAILED)
        self.assertIn("exploded", claim.error)

    def test_the_claim_is_heartbeated_while_the_join_blocks(self):
        """nmcli waits for the association, which on a slow AP is tens of seconds
        — longer than the supervisor's 45 s staleness budget."""
        beats = []
        real_write = net_wifi.write_wizard

        def counting_write(claim, state_dir=None):
            beats.append(claim.state)
            return real_write(claim, state_dir)

        def slow(argv):
            if "device wifi connect" in " ".join(argv):
                time.sleep(0.35)
            if "-g IP4.ADDRESS device show" in " ".join(argv):
                return net_wifi.NmResult(0, "192.168.1.77/24\n")
            return net_wifi.NmResult(0, "", "")

        with mock.patch.object(server.net_wifi, "write_wizard", counting_write), \
             mock.patch.object(server, "WIFI_SWITCH_HEARTBEAT_S", 0.05):
            server._perform_wifi_switch("Home", self.PSK, "n", self.state_dir,
                                        runner=slow, lead_s=0.0,
                                        sleep=lambda _s: None)
        self.assertGreaterEqual(beats.count(net_wifi.WIZARD_SWITCHING), 2,
                                f"expected repeated heartbeats, got {beats}")
        self.assertEqual(beats[-1], net_wifi.WIZARD_OK,
                         "the final state must be the last write, not a heartbeat")


class WifiConnectEndpointTests(GateCase):
    def _post(self, body, client=HOTSPOT_CLIENT, gate=True, auto_password=""):
        if gate:
            self.open_gate()
        else:
            self.close_gate()
        with mock.patch.dict(os.environ, {"MRRC_AUTO_PASSWORD": auto_password},
                             clear=False), \
             mock.patch.object(server.threading, "Thread") as thread:
            resp = asyncio.run(server.api_setup_wizard_wifi_connect(FakeRequest(
                path="/api/setup/wizard/wifi", method="POST", client=client,
                body=body)))
        return resp, thread

    def test_it_answers_at_once_and_switches_on_a_thread(self):
        """The answer has to be on the wire before the AP dies, so the switch
        cannot be awaited."""
        resp, thread = self._post({"ssid": "Home", "password": "hunter2hunter2"})
        self.assertEqual(resp.status_code, 200)
        payload = json.loads(resp.body)
        self.assertTrue(payload["switching"])
        self.assertTrue(payload["nonce"])
        thread.assert_called_once()
        thread.return_value.start.assert_called_once()

    def test_the_thread_is_a_daemon(self):
        """A non-daemon thread would hold the process open during a restart."""
        _, thread = self._post({"ssid": "Home"})
        self.assertTrue(thread.call_args.kwargs.get("daemon"))

    def test_it_refuses_to_switch_before_a_password_is_set(self):
        """Constraint 7: after the switch the window is gone for good, so an
        operator who never set a password is locked out of their own box."""
        resp, thread = self._post({"ssid": "Home"}, auto_password="1")
        self.assertEqual(resp.status_code, 409)
        thread.assert_not_called()

    def test_the_409_explains_itself(self):
        """A bare 409 on a phone screen is a dead end; the page shows this text."""
        resp, _ = self._post({"ssid": "Home"}, auto_password="1")
        payload = json.loads(resp.body)
        self.assertEqual(payload["error"], "set a password first")
        self.assertIn("hotspot", payload["detail"])

    def test_a_password_set_earlier_in_the_same_session_unlocks_it(self):
        """The 409 must clear without a restart, or the operator who just set a
        password is told to set one again."""
        self.open_gate()
        os.environ["MRRC_AUTO_PASSWORD"] = "1"
        self.addCleanup(os.environ.pop, "MRRC_AUTO_PASSWORD", None)
        with mock.patch.object(server.first_run, "update_env_file"), \
             mock.patch.object(server, "_config_file_path",
                               return_value=self.state_dir / "mrrc.env"):
            asyncio.run(server.api_setup_wizard_password(FakeRequest(
                method="POST", body={"password": "a-good-long-password"})))
        resp, thread = self._post({"ssid": "Home"}, gate=False)
        self.assertEqual(resp.status_code, 200)
        thread.assert_called_once()

    def test_an_empty_ssid_is_a_400(self):
        resp, thread = self._post({"ssid": "   ", "password": "hunter2hunter2"})
        self.assertEqual(resp.status_code, 400)
        thread.assert_not_called()

    def test_a_short_psk_is_a_400(self):
        """WPA-PSK is 8 characters by definition, so this is a typo check that
        saves a 20-second round trip through 'AP down → join fails → AP up'."""
        resp, thread = self._post({"ssid": "Home", "password": "short"})
        self.assertEqual(resp.status_code, 400)
        thread.assert_not_called()

    def test_an_open_network_needs_no_psk(self):
        resp, thread = self._post({"ssid": "Guest"})
        self.assertEqual(resp.status_code, 200)
        thread.assert_called_once()

    def test_the_ssid_is_trimmed_before_it_reaches_nmcli(self):
        _, thread = self._post({"ssid": "  Home  "})
        self.assertEqual(thread.call_args.args[0], "Home")

    def test_the_gate_is_required(self):
        resp, thread = self._post({"ssid": "Home"}, client=LAN_CLIENT, gate=False)
        self.assertEqual(resp.status_code, 401)
        thread.assert_not_called()

    def test_a_listen_only_token_is_refused(self):
        self.close_gate()
        token = server._make_auth_token()
        server._auth_tokens.add(token)
        server._listen_tokens.add(token)
        self.addCleanup(server._auth_tokens.discard, token)
        self.addCleanup(server._listen_tokens.discard, token)
        resp = asyncio.run(server.api_setup_wizard_wifi_connect(FakeRequest(
            path="/api/setup/wizard/wifi", method="POST", client=LAN_CLIENT,
            cookies={server.AUTH_COOKIE: token}, body={"ssid": "Home"})))
        self.assertEqual(resp.status_code, 403)

    def test_a_malformed_body_is_a_400(self):
        self.open_gate()
        request = FakeRequest(method="POST")

        async def not_json():
            raise ValueError("nope")

        request.json = not_json
        with mock.patch.dict(os.environ, {"MRRC_AUTO_PASSWORD": ""}, clear=False):
            resp = asyncio.run(server.api_setup_wizard_wifi_connect(request))
        self.assertEqual(resp.status_code, 400)

    def test_the_ssid_is_logged_but_the_psk_is_not(self):
        with self.assertLogs("mrrc", level="DEBUG") as captured:
            self._post({"ssid": "HomeNet", "password": "hunter2hunter2"})
        joined = "\n".join(captured.output)
        self.assertIn("HomeNet", joined)
        self.assertNotIn("hunter2hunter2", joined)

    def test_the_existing_route_order_guard_sees_the_wizard_routes(self):
        """tests/test_cloud_endpoints.py already asserts that no /api/ route sits
        below the SPA fallback. This duplicates it deliberately: if the wizard
        routes are ever moved, the failure should name them instead of looking
        like a Cloud Hub regression."""
        paths = [getattr(r, "path", "") for r in server.app.router.routes]
        catch_all = paths.index("/{path:path}")
        for path in ("/api/setup/wizard", "/api/setup/wizard/password",
                     "/api/setup/wizard/wifi"):
            with self.subTest(path=path):
                self.assertIn(path, paths)
                self.assertLess(paths.index(path), catch_all)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`$PY -m unittest tests.test_server_setup_wizard -v 2>&1 | tail -6`
预期：ERROR — `AttributeError: module 'server' has no attribute 'api_setup_wizard_wifi_scan'`（以及 `api_setup_wizard_wifi_connect` / `_perform_wifi_switch` / `WIFI_SWITCH_HEARTBEAT_S`）

- [ ] **步骤 3：编写实现**

在 `server.py` 里紧跟任务 6 的 `api_setup_wizard_password()` 之后追加：

```python
# ── the AP → STA switch ─────────────────────────────────────────────
# A switch takes the operator's phone off the box, so the HTTP answer has to be
# on the wire before the radio changes hands. This long, and no longer: the
# operator is staring at a spinner.
WIFI_SWITCH_LEAD_S = 1.5
# `nmcli device wifi connect` blocks until the association settles, which on a
# slow AP is tens of seconds — past the supervisor's 45 s staleness budget. So
# the claim is re-published on this interval while a switch is in flight.
WIFI_SWITCH_HEARTBEAT_S = 5.0


def _wifi_failure_reason(result, password: str) -> str:
    """One scrubbed, bounded line from an nmcli failure.

    Scrubbed because this text is stored in wizard.json and shown on a phone:
    nmcli does not echo a PSK today, but "does not today" is not a property of
    this code, and the file is one `support_bundle` glob away from a public
    upload (SDD support-bundle-privacy).
    """
    text = net_wifi.scrub(result.detail, password)
    return text[:300] or f"nmcli exited {result.returncode}"


def _claim_heartbeat(publish_once, stop: threading.Event,
                     every: float = WIFI_SWITCH_HEARTBEAT_S) -> None:
    """Re-publish the claim until asked to stop.

    Without this the supervisor's staleness budget expires mid-switch and it
    takes the radio back: two processes fighting over one radio, with the
    operator's phone connected to neither.
    """
    while not stop.wait(every):
        publish_once()


def _perform_wifi_switch(ssid: str, password: str, nonce: str, state_dir: Path,
                         runner=None, lead_s: float = WIFI_SWITCH_LEAD_S,
                         sleep=time.sleep) -> None:
    """AP → STA on one radio (design D-3). Worker thread, never the event loop.

    The claim file is the only channel back, and it works in one direction only:
    on success the hotspot is gone and the phone has already left, so nothing can
    be delivered — the page has to have told the operator what to do *before*
    this starts. On failure the hotspot comes back, the phone rejoins it, and the
    page reads the reason out of this file. That asymmetry is why "failure must
    not be silent" (§6) is implemented as "restore the AP, then explain".
    """
    settings = net_wifi.ap_settings()

    def publish(state: str, error: str = "", address: str = "") -> None:
        net_wifi.write_wizard(net_wifi.WizardClaim(
            action="connect", ssid=ssid, state=state, error=error,
            address=address, nonce=nonce, heartbeat=time.time()), state_dir)

    sleep(lead_s)                      # let the HTTP answer reach the phone
    publish(net_wifi.WIZARD_SWITCHING)

    stop = threading.Event()
    beating = threading.Thread(target=_claim_heartbeat,
                               args=(lambda: publish(net_wifi.WIZARD_SWITCHING),
                                     stop),
                               name="setup-wifi-heartbeat", daemon=True)
    beating.start()
    try:
        net_wifi.stop_hotspot(runner)
        result = net_wifi.connect_wifi(ssid, password, runner,
                                       ifname=settings["ifname"])
    except Exception as exc:                                 # noqa: BLE001
        # A daemon thread that raises dies silently and leaves the claim at
        # `switching`, which the supervisor reads as "still working".
        result = net_wifi.NmResult(1, "", f"the switch raised: {exc}")
    finally:
        # Stop the heartbeat *before* the final publish, so the authoritative
        # state is the last thing in the file and not a beat that landed after.
        stop.set()
        beating.join(timeout=2.0)

    if not result.ok:
        reason = _wifi_failure_reason(result, password)
        logger.warning("setup wizard: could not join %r (%s) — reopening the "
                       "hotspot", ssid, reason)
        back = net_wifi.start_hotspot(settings["ssid"], settings["ifname"], runner)
        if not back.ok:
            logger.error("setup wizard: the hotspot would not come back either: "
                         "%s — the box is unreachable until it is rebooted",
                         back.detail)
        publish(net_wifi.WIZARD_FAILED, error=reason)
        return

    address = net_wifi.ipv4_address(settings["ifname"], runner)
    logger.warning("setup wizard: joined %r, address %s — the hotspot stays down",
                   ssid, address or "not reported")
    publish(net_wifi.WIZARD_OK, address=address)


@app.get("/api/setup/wizard/wifi", include_in_schema=False)
async def api_setup_wizard_wifi_scan(request: Request):
    """Networks the box can see, strongest first, one row per SSID.

    Off the event loop: a rescan takes seconds, and blocking the loop would stall
    the spectrum and audio WebSockets of every client on the box.
    """
    if not _setup_access(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    networks = await asyncio.to_thread(net_wifi.scan_wifi)
    return JSONResponse({"networks": networks})


@app.post("/api/setup/wizard/wifi", include_in_schema=False)
async def api_setup_wizard_wifi_connect(request: Request):
    """Join the operator's WiFi — and lose their phone while doing it.

    The shape follows from the single radio (design D-3): answer first, switch on
    a thread behind the answer, and make the page say what is about to happen
    before it happens. `_perform_wifi_switch` carries the details.
    """
    if not _setup_access(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if _setup_auto_password():
        # Constraint 7. After this switch the passwordless window is gone for
        # good, and the password in force is one the box generated and nobody was
        # told. Switching now would lock the operator out of their own box.
        return JSONResponse(
            {"error": "set a password first",
             "detail": "This box is still using a password it generated itself. "
                       "Once it joins your WiFi this open hotspot closes and there "
                       "would be no way left to set one — so set your own password "
                       "first, then come back here."},
            status_code=409)
    try:
        body = await request.json()
    except Exception:                                        # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}
    ssid = str(body.get("ssid", "") or "").strip()
    password = str(body.get("password", "") or "")
    if not ssid:
        return JSONResponse({"error": "ssid required"}, status_code=400)
    if password and len(password) < MIN_WIFI_PSK_LEN:
        return JSONResponse(
            {"error": f"wifi password too short (min {MIN_WIFI_PSK_LEN})"},
            status_code=400)

    # One nonce per attempt: it is what lets the supervisor grant exactly one
    # fresh window if this fails, and refuse to keep granting them if the page
    # retries on a timer.
    nonce = _secrets.token_hex(8)
    threading.Thread(target=_perform_wifi_switch,
                     args=(ssid, password, nonce, _setup_ap_state_dir()),
                     name="setup-wifi-switch", daemon=True).start()
    logger.warning("setup wizard: switching to WiFi %r from %s — the open hotspot "
                   "is about to close", ssid,
                   request.client.host if request.client else "?")
    return JSONResponse({"ok": True, "switching": True, "nonce": nonce})
```

- [ ] **步骤 4：运行测试验证通过**

运行：`$PY -m unittest tests.test_server_setup_wizard -v 2>&1 | tail -6`
预期：`OK`（约 65 项）

排障提示：
- `test_the_hotspot_is_taken_down_before_the_join` 红 ⇒ `stop_hotspot()` 必须在 `connect_wifi()` **之前**。反过来写在这个假 runner 上可能"看起来也能过"，真机上就是关联失败。
- `test_the_claim_is_heartbeated_while_the_join_blocks` 红在最后一拍不是 `ok` ⇒ `finally` 里的 `stop.set()` + `join()` 必须在最终 `publish()` **之前**。
- `test_a_runner_that_raises_is_a_failure_not_a_crash` 红 ⇒ `connect_wifi` 外面那个 `except` 没了。守护线程里的异常是**静默**的，claim 会永远停在 `switching`。
- `test_a_password_set_earlier_in_the_same_session_unlocks_it` 红 ⇒ 任务 6 的口令端点漏了 `os.environ["MRRC_AUTO_PASSWORD"] = ""`（回任务 6 的排障表）。
- `test_the_thread_is_a_daemon` 红 ⇒ `Thread(...)` 的 `daemon=True` 要用**关键字**传（测试读的是 `call_args.kwargs`）。

- [ ] **步骤 5：确认没有弄坏别的**

运行：`$PY -m unittest discover -s tests 2>&1 | tail -4`
预期：`FAILED (failures=1, skipped=1)`，唯一 `FAIL:` 是 `test_tls_trust_store`

- [ ] **步骤 6：SDD 检查 + Commit**

```bash
$PY .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git add server.py tests/test_server_setup_wizard.py
git commit -m "feat: 向导的 Wi-Fi 扫描与 AP→STA 切换——先发回答再切、失败必回热点并带原因"
```


---

## 任务 8：`static/setup.html` + `GET /setup` 路由

**文件：**
- 创建：`static/setup.html`
- 修改：`server.py`（在任务 7 的端点之后、SPA 兜底之前加一个路由）
- 测试：`tests/test_server_setup_wizard.py`（追加）

**页面的三条硬要求：**
1. **自包含**：内联 CSS + JS，零外部资源。这台盒子用的是自签证书、而且操作者可能根本没有外网（热点期间盒子自己就没网），任何 CDN 字体/库都会让页面变成一片空白。
2. **第二步在第一步完成前锁住**（约束 7 的 UI 面）。服务端已经用 409 挡住了，页面必须**同样**挡住并说明为什么——否则操作者点了"连接"才吃到一个 409，而那时热点可能已经在拆了。
3. **切网前把"接下来会发生什么"说清楚**。手机会在切换瞬间掉线，所以那段说明必须在按下按钮**之后、掉线之前**就出现在屏幕上。

- [ ] **步骤 1：编写失败的测试**

在 `tests/test_server_setup_wizard.py` 末尾（`if __name__` 之前）追加：

```python
class SetupPageRouteTests(GateCase):
    def _get(self, client=HOTSPOT_CLIENT, cookies=None):
        return asyncio.run(server.setup_page(FakeRequest(
            path="/setup", client=client, cookies=cookies)))

    def test_served_without_a_token_while_the_window_is_open(self):
        self.open_gate()
        self.assertEqual(self._get().status_code, 200)

    def test_it_is_never_cached(self):
        """The page renders the state of a window that closes by itself. A cached
        copy would tell the operator the box is still reachable when it is not."""
        self.open_gate()
        self.assertEqual(self._get().headers["Cache-Control"], "no-store")

    def test_redirects_to_login_from_anywhere_else(self):
        self.open_gate()
        resp = self._get(client=LAN_CLIENT)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["location"])

    def test_redirects_when_the_window_is_closed(self):
        self.close_gate()
        self.assertEqual(self._get().status_code, 302)

    def test_redirects_when_the_supervisor_stopped_heartbeating(self):
        net_wifi.write_state(net_wifi.ApState(
            mode=net_wifi.MODE_HOTSPOT, network="10.42.0.0/24",
            heartbeat=time.time() - 300), self.state_dir)
        self.assertEqual(self._get().status_code, 302)

    def test_an_admin_token_gets_in_without_the_gate(self):
        """Useful over the LAN once the box is configured, and it must not depend
        on a hotspot that no longer exists."""
        self.close_gate()
        token = server._make_auth_token()
        server._auth_tokens.add(token)
        self.addCleanup(server._auth_tokens.discard, token)
        resp = self._get(client=LAN_CLIENT, cookies={server.AUTH_COOKIE: token})
        self.assertEqual(resp.status_code, 200)

    def test_the_route_is_registered_before_the_spa_fallback(self):
        """/setup is not under /api/, so the existing route-order guard in
        tests/test_cloud_endpoints.py does not cover it. Registered below the
        catch-all it would be answered with index.html — the SPA — and the wizard
        would be unreachable with a 200 status and no explanation."""
        paths = [getattr(r, "path", "") for r in server.app.router.routes]
        self.assertIn("/setup", paths)
        self.assertLess(paths.index("/setup"), paths.index("/{path:path}"))


class SetupPageSourceTests(unittest.TestCase):
    """The page ships as one self-contained file; these assert that stays true."""

    @classmethod
    def setUpClass(cls):
        cls.html = (Path(server.__file__).resolve().parent
                    / "static" / "setup.html").read_text(encoding="utf-8")

    def test_it_exists_and_is_a_page(self):
        self.assertIn("<!DOCTYPE html>", self.html)
        self.assertIn("</html>", self.html)

    def test_no_external_resource_is_referenced(self):
        """During onboarding the box has no uplink by definition, and its
        certificate is self-signed. A CDN font or library here is a blank page on
        a phone, with no console to read."""
        self.assertNotIn("http://", self.html)
        self.assertNotIn("https://cdn", self.html)
        self.assertNotIn("//fonts.", self.html)
        for match in re.finditer(r'(?:src|href)\s*=\s*"([^"]+)"', self.html):
            ref = match.group(1)
            with self.subTest(ref=ref):
                self.assertTrue(ref.startswith("/") or ref.startswith("#"),
                                f"{ref} is not a same-origin reference")

    def test_it_talks_only_to_the_wizard_endpoints(self):
        """The page must not reach for an endpoint the gate does not cover; that
        would be a 401 the operator cannot do anything about."""
        called = set(re.findall(r"""api\(\s*['"]([^'"]+)['"]""", self.html))
        self.assertTrue(called, "the page calls no endpoint at all — parser drift?")
        self.assertLessEqual(called, {"/api/setup/wizard",
                                      "/api/setup/wizard/password",
                                      "/api/setup/wizard/wifi"})

    def test_it_is_a_mobile_page(self):
        self.assertIn('name="viewport"', self.html)
        self.assertIn("width=device-width", self.html)

    def test_it_warns_about_what_happens_during_the_switch(self):
        """The phone leaves the box mid-switch. If the page has not already said
        so, the operator's last sight of it is a spinner that never resolves."""
        self.assertIn("热点", self.html)
        self.assertTrue("关闭" in self.html or "断开" in self.html)

    def test_the_wifi_step_explains_why_it_is_locked(self):
        """A greyed-out button with no reason reads as a broken page."""
        self.assertIn("先设置", self.html)

    def test_no_credential_like_literal_is_written_into_the_page(self):
        """SDD `secrets-hardcoded`. Placeholders are prompts, not values; a real
        password in a shipped HTML file is a credential in every image."""
        self.assertNotIn("changeme", self.html.lower())
        self.assertNotIn("value=\"", self.html.replace('value=""', ""))
        for field in re.findall(r'<input[^>]+type="password"[^>]*>', self.html):
            with self.subTest(field=field):
                self.assertNotIn("value=", field)

    def test_the_two_password_fields_are_the_same_type(self):
        """A confirm field that is not masked invites a shoulder-surf and a typo
        at the same time."""
        self.assertEqual(self.html.count('type="password"'), 3,
                         "web password, confirm, and the WiFi passphrase")
```

> 追加时记得在文件顶部 import 区补 `import re`。

- [ ] **步骤 2：运行测试验证失败**

运行：`$PY -m unittest tests.test_server_setup_wizard -v 2>&1 | tail -6`
预期：`SetupPageRouteTests` 一片 ERROR（`AttributeError: module 'server' has no attribute 'setup_page'`），`SetupPageSourceTests.setUpClass` 报 `FileNotFoundError: … static/setup.html`

- [ ] **步骤 3：编写实现**

**3a. 创建 `static/setup.html`：**

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex,nofollow">
<meta name="theme-color" content="#141414">
<title>MRRC Modern 初始化</title>
<style>
:root{
  --bg:#141414; --card:#1e1e1e; --line:#333; --amber:#f59e0b;
  --text:#eee; --dim:#9ca3af; --bad:#ef4444; --good:#22c55e;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  background:var(--bg); color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB",
              "Microsoft YaHei",sans-serif;
  font-size:16px; line-height:1.6;
  padding:16px 14px calc(28px + env(safe-area-inset-bottom));
  -webkit-text-size-adjust:100%;
}
.wrap{max-width:520px;margin:0 auto}
header{text-align:center;margin:6px 0 18px}
h1{font-size:22px;margin:0;color:var(--amber);letter-spacing:.02em}
.sub{margin:4px 0 0;color:var(--dim);font-size:13px}
.card{
  background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:16px;margin:0 0 14px;
}
.card h2{
  font-size:15px;margin:0 0 4px;display:flex;align-items:center;gap:8px;
}
.step-no{
  display:inline-flex;align-items:center;justify-content:center;
  width:22px;height:22px;border-radius:50%;background:var(--amber);
  color:#000;font-size:13px;font-weight:700;flex:0 0 auto;
}
.hint{color:var(--dim);font-size:13px;margin:0 0 12px}
label{display:block;font-size:13px;color:var(--dim);margin:12px 0 4px}
input[type=password],input[type=text]{
  width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;
  background:#111;color:var(--text);font-size:16px;
}
input:focus{outline:none;border-color:var(--amber)}
button{
  width:100%;padding:13px;margin-top:14px;border:none;border-radius:10px;
  background:var(--amber);color:#000;font-size:16px;font-weight:700;cursor:pointer;
}
button:disabled{background:#3a3a3a;color:#777;cursor:not-allowed}
button.ghost{background:transparent;color:var(--amber);border:1px solid var(--amber);font-weight:600}
.msg{font-size:13px;margin-top:10px;min-height:18px}
.msg.bad{color:var(--bad)}
.msg.good{color:var(--good)}
.rows{list-style:none;margin:10px 0 0;padding:0;border-top:1px solid var(--line)}
.rows li{border-bottom:1px solid var(--line)}
.rows button{
  width:100%;background:transparent;color:var(--text);border:none;border-radius:0;
  margin:0;padding:13px 4px;font-weight:400;font-size:15px;text-align:left;
  display:flex;align-items:center;gap:10px;
}
.rows button:active{background:#2a2a2a}
.ssid{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sig{color:var(--dim);font-size:12px;flex:0 0 auto;letter-spacing:1px}
.lock{color:var(--dim);font-size:12px;flex:0 0 auto}
.picked{background:#2a2410 !important}
.kv{display:flex;justify-content:space-between;gap:12px;font-size:13px;padding:3px 0}
.kv b{font-weight:600;color:var(--text);text-align:right;word-break:break-all}
.kv span{color:var(--dim);flex:0 0 auto}
.pill{
  display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;
  background:#2d2410;color:var(--amber);border:1px solid #6b4d0f;
}
.pill.good{background:#0f2d1a;color:var(--good);border-color:#166534}
.pill.bad{background:#2d0f0f;color:var(--bad);border-color:#7f1d1d}
.banner{
  background:#2d2410;border:1px solid #6b4d0f;border-radius:12px;
  padding:12px;margin:0 0 14px;font-size:13px;color:#fbbf24;
}
.note{font-size:12.5px;color:var(--dim)}
.note b{color:var(--text);font-weight:600}
.note ol{margin:6px 0 0;padding-left:20px}
.note li{margin:4px 0}
.locked{opacity:.55}
.overlay{
  position:fixed;inset:0;background:rgba(10,10,10,.97);z-index:10;
  display:flex;align-items:center;justify-content:center;padding:24px;
}
.overlay.hidden{display:none}
.overlay .box{max-width:460px;width:100%;text-align:left}
.spinner{
  width:34px;height:34px;margin:0 auto 18px;border-radius:50%;
  border:3px solid #3a3a3a;border-top-color:var(--amber);
  animation:spin 0.9s linear infinite;
}
@keyframes spin{to{transform:rotate(360deg)}}
.overlay h2{text-align:center;color:var(--amber);font-size:19px;margin:0 0 10px}
.big{font-size:15px;margin:0 0 14px}
code{
  background:#111;border:1px solid var(--line);border-radius:6px;
  padding:1px 6px;font-size:13px;word-break:break-all;
}
.errbox{
  background:#2d0f0f;border:1px solid #7f1d1d;border-radius:12px;
  padding:12px;margin:0 0 14px;font-size:13.5px;color:#fca5a5;
}
.hidden{display:none}
footer{text-align:center;color:#555;font-size:11.5px;margin-top:18px}
</style>
</head>
<body>
<div class="wrap">

  <header>
    <h1>MRRC Modern</h1>
    <p class="sub">电台盒子初始化</p>
  </header>

  <div id="banner" class="banner hidden"></div>
  <div id="errbox" class="errbox hidden"></div>

  <div class="card" id="card-status">
    <div class="kv"><span>状态</span><b id="st-mode">读取中…</b></div>
    <div class="kv"><span>本页来源</span><b id="st-gate">—</b></div>
    <div class="kv hidden" id="row-ssid"><span>热点名称</span><b id="st-ssid">—</b></div>
    <div class="kv hidden" id="row-left"><span>窗口剩余</span><b id="st-left">—</b></div>
    <div class="kv"><span>电台型号</span><b id="st-model">—</b></div>
    <div class="kv"><span>登录口令</span><b id="st-pass">—</b></div>
  </div>

  <section class="card" id="card-password">
    <h2><span class="step-no">1</span>设置登录口令</h2>
    <p class="hint">这个口令用于之后打开盒子的控制界面。至少 8 位。</p>
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
      <ol>
        <li>盒子连上你的 Wi-Fi 后，<b>它自己开的热点会立刻关闭</b>，你这台手机会
            与盒子断开。</li>
        <li>请把手机<b>连回你原来的 Wi-Fi</b>（就是上面选的那个）。</li>
        <li>然后在路由器后台的客户端列表里找到这台盒子，用
            <code>https://它的IP:8888</code> 打开控制界面，用刚设的口令登录。
            盒子的地址也会打印在它的 HDMI 控制台上。</li>
      </ol>
      <p style="margin-top:10px">如果 Wi-Fi 密码输错了，盒子会<b>自动把热点重新开回来</b>，
         手机重连热点后刷新本页就能看到失败原因，可以直接重试。</p>
      <p>本页用的是盒子自签的 HTTPS 证书，浏览器会警告一次，
         选「继续访问」即可 —— 这是正常的。</p>
    </div>
  </section>

  <footer>MRRC Modern · 初始化向导</footer>
</div>

<div id="overlay" class="overlay hidden">
  <div class="box">
    <div class="spinner"></div>
    <h2 id="ov-title">正在切换网络…</h2>
    <p class="big" id="ov-text"></p>
    <div class="note">
      <ol>
        <li>盒子的热点<b>马上就要关闭</b>，这台手机会与盒子断开 —— 这是正常的，
            不是失败。</li>
        <li>请把手机连回 <b id="ov-ssid"></b>。</li>
        <li>然后在路由器里找到盒子，用 <code>https://它的IP:8888</code> 打开界面，
            用你刚设的口令登录。</li>
      </ol>
    </div>
    <p class="note" id="ov-wait" style="margin-top:14px">
      如果密码不对，盒子会把热点重新开回来；这个页面会自己显示失败原因。
      请留在本页等待一会儿。</p>
    <div id="ov-err" class="errbox hidden" style="margin-top:14px"></div>
    <button id="ov-back" type="button" class="ghost hidden">返回重试</button>
  </div>
</div>

<script>
"use strict";
var $ = function (id) { return document.getElementById(id); };
var state = null;          // the last /api/setup/wizard payload
var countdown = 0;         // seconds left in the open window
var picked = null;         // the network row the operator tapped
var switching = false;

function api(path, options) {
  return fetch(path, options).then(function (resp) {
    return resp.text().then(function (text) {
      var data = null;
      try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
      if (!resp.ok) {
        var why = (data && (data.detail || data.error)) ||
                  ("请求失败（HTTP " + resp.status + "）");
        var err = new Error(why);
        err.status = resp.status;
        throw err;
      }
      return data;
    });
  });
}

function setText(id, text) { $(id).textContent = text; }

function show(id, on) {
  var node = $(id);
  if (on) { node.classList.remove("hidden"); }
  else { node.classList.add("hidden"); }
}

function msg(id, text, kind) {
  var node = $(id);
  node.textContent = text || "";
  node.className = "msg" + (kind ? " " + kind : "");
}

function fmtLeft(seconds) {
  if (!seconds || seconds <= 0) { return "—"; }
  var m = Math.floor(seconds / 60), s = Math.floor(seconds % 60);
  return m + " 分 " + (s < 10 ? "0" : "") + s + " 秒";
}

function bars(signal) {
  var n = signal >= 75 ? 4 : signal >= 55 ? 3 : signal >= 35 ? 2 : 1;
  return "▂▄▆█".slice(0, n);
}

/* ── status ─────────────────────────────────────────────── */

function render(data) {
  state = data;
  var hs = data.hotspot || {};
  var viaGate = data.gate === "hotspot";

  if (hs.mode === "hotspot") {
    setText("st-mode", "初始化热点已开启");
    show("row-ssid", true); setText("st-ssid", hs.ssid || "—");
    show("row-left", true);
    countdown = Math.max(0, Math.round(hs.remaining_s || 0));
    setText("st-left", fmtLeft(countdown));
  } else if (hs.mode === "sta") {
    setText("st-mode", "已连上 Wi-Fi，热点已关闭");
    show("row-ssid", false); show("row-left", false);
  } else {
    setText("st-mode", hs.reason === "timeout" ? "初始化窗口已超时关闭"
                                                : "盒子已有网络，热点未开启");
    show("row-ssid", false); show("row-left", false);
  }

  setText("st-gate", viaGate ? "开放热点（免口令）" : "已登录会话");
  setText("st-model", data.radio_model || "—");
  setText("st-pass", data.auto_password
        ? "盒子自动生成（你还不知道）"
        : "已由你设置 ✓");

  var banner = $("banner");
  if (viaGate) {
    banner.classList.remove("hidden");
    banner.innerHTML = "你现在是通过盒子自己开的<b>开放热点</b>访问的，" +
      "所以这一页不需要口令。窗口会在剩余时间用完、或盒子连上 Wi-Fi 时关闭。";
  } else {
    banner.classList.add("hidden");
  }

  // A failed switch is reported here, because the phone has by then rejoined the
  // hotspot and reloaded the page — this is the "must not be silent" path.
  var sw = data["switch"] || {};
  if (sw.state === "failed" && sw.error) {
    $("errbox").classList.remove("hidden");
    $("errbox").textContent = "上次连接 " + (sw.ssid || "") + " 失败：" + sw.error +
      "。热点已重新开启，可以直接重试。";
  }

  // Step 1 is done when the password is no longer the generated one.
  var passwordDone = !data.auto_password;
  $("card-wifi").classList.toggle("locked", !passwordDone);
  $("scan-btn").disabled = !passwordDone;
  setText("wifi-lock-hint", passwordDone
      ? "选一个 Wi-Fi，盒子会切过去并关掉自己的热点。"
      : "请先设置登录口令，再连接 Wi-Fi。切网之后这个免口令页面就关了，" +
        "所以必须先把口令设成你自己的 —— 否则你再也进不来。");
  if (passwordDone) {
    $("pw-btn").textContent = "修改口令";
    msg("pw-msg", "", "");
  }
}

function refresh() {
  return api("/api/setup/wizard").then(render).catch(function (err) {
    if (switching) { return; }        // expected: the AP is gone
    setText("st-mode", "读不到状态：" + err.message);
  });
}

function tick() {
  if (!countdown || countdown <= 0) { return; }
  countdown -= 1;
  setText("st-left", fmtLeft(countdown));
  if (countdown === 0) { refresh(); }
}

/* ── step 1: the password ───────────────────────────────── */

function setPassword() {
  var pw = $("pw1").value.trim();
  var again = $("pw2").value.trim();
  if (pw.length < 8) { msg("pw-msg", "口令至少 8 位。", "bad"); return; }
  if (pw !== again) { msg("pw-msg", "两次输入不一致。", "bad"); return; }
  $("pw-btn").disabled = true;
  msg("pw-msg", "保存中…", "");
  api("/api/setup/wizard/password", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: pw, confirm: again })
  }).then(function () {
    $("pw1").value = ""; $("pw2").value = "";
    msg("pw-msg", "口令已设置 ✓ 现在可以连 Wi-Fi 了。", "good");
    $("pw-btn").disabled = false;
    return refresh();
  }).catch(function (err) {
    $("pw-btn").disabled = false;
    msg("pw-msg", err.message, "bad");
  });
}

/* ── step 2: the network ────────────────────────────────── */

function scan() {
  $("scan-btn").disabled = true;
  msg("wifi-msg", "扫描中，大约需要几秒…", "");
  $("net-list").innerHTML = "";
  show("pick", false);
  api("/api/setup/wizard/wifi").then(function (data) {
    $("scan-btn").disabled = false;
    var nets = (data && data.networks) || [];
    if (!nets.length) {
      msg("wifi-msg", "没有扫到任何网络。天线接好了吗？（也可能是这块无线的 " +
                      "AP 模式还没在真机上验证过 —— 见指南 §9.5）", "bad");
      return;
    }
    msg("wifi-msg", "共 " + nets.length + " 个网络，点一个继续。", "");
    renderNetworks(nets);
  }).catch(function (err) {
    $("scan-btn").disabled = false;
    msg("wifi-msg", err.message, "bad");
  });
}

function renderNetworks(nets) {
  var list = $("net-list");
  list.innerHTML = "";
  nets.forEach(function (net) {
    var li = document.createElement("li");
    var btn = document.createElement("button");
    btn.type = "button";
    btn.innerHTML = '<span class="sig"></span><span class="ssid"></span>' +
                    '<span class="lock"></span>';
    btn.querySelector(".sig").textContent = bars(net.signal || 0);
    btn.querySelector(".ssid").textContent = net.ssid;
    btn.querySelector(".lock").textContent = net.protected ? "🔒" : "开放";
    btn.addEventListener("click", function () {
      Array.prototype.forEach.call(list.querySelectorAll("button"),
        function (b) { b.classList.remove("picked"); });
      btn.classList.add("picked");
      pick(net);
    });
    li.appendChild(btn);
    list.appendChild(li);
  });
}

function pick(net) {
  picked = net;
  setText("pick-name", net.ssid);
  show("pick", true);
  $("psk").value = "";
  $("psk").disabled = !net.protected;
  $("psk").placeholder = net.protected ? "Wi-Fi 密码" : "开放网络，无需密码";
  msg("wifi-msg", "", "");
}

function join() {
  if (!picked) { return; }
  var psk = $("psk").value;
  if (psk && psk.length < 8) {
    msg("wifi-msg", "Wi-Fi 密码至少 8 位（WPA 就是这么规定的）——大概是输错了。", "bad");
    return;
  }
  $("join-btn").disabled = true;
  api("/api/setup/wizard/wifi", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ssid: picked.ssid, password: psk })
  }).then(function (data) {
    beginSwitch(picked.ssid);
  }).catch(function (err) {
    $("join-btn").disabled = false;
    if (err.status === 409) {
      msg("wifi-msg", err.message, "bad");
      $("card-password").scrollIntoView({ behavior: "smooth" });
      return;
    }
    msg("wifi-msg", err.message, "bad");
  });
}

/* ── the switch ─────────────────────────────────────────── */

function beginSwitch(ssid) {
  // Everything the operator needs to know has to be on screen BEFORE the AP
  // dies, because after that this page cannot be reached any more.
  switching = true;
  setText("ov-ssid", ssid);
  setText("ov-text", "盒子正在连接 " + ssid + "，并关闭自己的热点。");
  show("ov-err", false);
  show("ov-back", false);
  $("overlay").classList.remove("hidden");
  pollSwitch(0);
}

function pollSwitch(attempt) {
  if (attempt > 40) {          // ~2 minutes: stop asking, keep the instructions
    setText("ov-wait", "还在等盒子的回话。如果手机已经连回了你的 Wi-Fi，" +
      "就可以关掉本页、去路由器里找盒子了。");
    return;
  }
  setTimeout(function () {
    api("/api/setup/wizard").then(function (data) {
      var sw = data["switch"] || {};
      if (sw.state === "failed") {
        // The hotspot came back, so the join did not work. Say so — design §6
        // forbids a silent return.
        switching = false;
        $("overlay").classList.add("hidden");
        $("join-btn").disabled = false;
        $("errbox").classList.remove("hidden");
        $("errbox").textContent = "连接 " + (sw.ssid || "") + " 失败：" +
          (sw.error || "未知原因") + "。热点已重新开启，可以直接重试。";
        $("errbox").scrollIntoView({ behavior: "smooth" });
        return;
      }
      if (sw.state === "ok") {
        setText("ov-title", "已连上 " + (sw.ssid || ""));
        setText("ov-text", "盒子的地址是 " +
          ((sw.address || "").split("/")[0] || "（见路由器 / HDMI 控制台）") +
          "。请把手机连回你的 Wi-Fi，然后用 https://该地址:8888 打开界面。");
        show("ov-wait", false);
        return;
      }
      pollSwitch(attempt + 1);
    }).catch(function () {
      pollSwitch(attempt + 1);   // expected while the AP is down
    });
  }, 3000);
}

/* ── wiring ─────────────────────────────────────────────── */

$("pw-btn").addEventListener("click", setPassword);
$("scan-btn").addEventListener("click", scan);
$("join-btn").addEventListener("click", join);
$("ov-back").addEventListener("click", function () {
  $("overlay").classList.add("hidden");
  switching = false;
  $("join-btn").disabled = false;
  refresh();
});
[["pw1", setPassword], ["pw2", setPassword]].forEach(function (pair) {
  $(pair[0]).addEventListener("keydown", function (ev) {
    if (ev.key === "Enter") { pair[1](); }
  });
});
$("psk").addEventListener("keydown", function (ev) {
  if (ev.key === "Enter") { join(); }
});

refresh();
setInterval(refresh, 15000);
setInterval(tick, 1000);
</script>
</body>
</html>
```

> **为什么页面不核对 nonce（一个有意的设计选择）**：`nonce` 是服务端与守护进程之间的凭证
> （守护进程按 nonce **一次性**发放新窗口，见任务 4），它**不在** `/api/setup/wizard` 的
> `switch` 载荷里——任务 6 把那个载荷钉成了闭合 schema `{state, ssid, error, address}`。
> 页面本来就只在一个时刻处于切换态，`state === "failed"` 已经足够判定，所以不去读一个
> 服务端从不返回的字段。**如果你希望页面也核对 nonce**，要改的是任务 6 的闭合 schema
> （把 `nonce` 加进 `switch`，并同步那条 `test_the_payload_is_a_closed_schema`），
> 而不是在 JS 里读一个永远是 `undefined` 的键。

**3b. 加路由。** 在 `server.py` 里紧跟任务 7 的 `api_setup_wizard_wifi_connect()` 之后追加：

```python
@app.get("/setup", include_in_schema=False)
async def setup_page(request: Request):
    """The onboarding wizard page.

    Passwordless only while the box is serving its own open hotspot and the
    request comes from that subnet; from anywhere else it is an ordinary
    authenticated route. `no-store` because the page renders a window that closes
    by itself — a cached copy would tell the operator the box is still reachable
    when it is not.
    """
    if not _setup_access(request):
        return RedirectResponse("/login?next=/setup", status_code=302)
    page = STATIC_DIR / "setup.html"
    if not page.exists():
        return HTMLResponse("<h1>404 Not Found</h1>", status_code=404)
    response = FileResponse(page, media_type="text/html")
    response.headers["Cache-Control"] = "no-store"
    return response
```

- [ ] **步骤 4：运行测试验证通过**

运行：`$PY -m unittest tests.test_server_setup_wizard -v 2>&1 | tail -6`
预期：`OK`（约 80 项）

排障提示：
- `test_no_external_resource_is_referenced` 红 ⇒ 页面里有 `http://` 或 CDN 引用。这个页面**必须**零外部依赖：热点期间盒子自己没有上行，任何外链都是一片空白。
- `test_it_talks_only_to_the_wizard_endpoints` 红 ⇒ 页面 `api()` 调了一个不在闸门集合里的路径（那样只会得到 401，而操作者无从处理）。正则找的是 `api("…")` 形式，所以页面里的调用要写成那个形状。
- `test_no_credential_like_literal_is_written_into_the_page` 红在 `value="` ⇒ 有 input 带了默认值。向导页的所有输入框都必须**空着**。
- `test_the_two_password_fields_are_the_same_type` 红 ⇒ 数一下 `type="password"`：网页口令、确认、Wi-Fi 密码，正好 3 个。

- [ ] **步骤 5：在浏览器里真看一眼（不需要盒子）**

在本机把闸门伪造出来，肉眼验收页面：

```bash
PY=/Users/cheenle/HAM/hub/mrrc_modern/.venv/bin/python
mkdir -p /tmp/mrrc-ap-demo
$PY - <<'EOF'
import time, pathlib, sys
sys.path.insert(0, ".")
import net_wifi
net_wifi.write_state(net_wifi.ApState(
    mode="hotspot", ssid="MRRC-Setup", gateway="127.0.0.1/8",
    network="127.0.0.0/8", url="https://127.0.0.1:8888/setup",
    since=time.time(), heartbeat=time.time(), deadline=time.time() + 1800),
    pathlib.Path("/tmp/mrrc-ap-demo"))
EOF
MRRC_SETUP_AP_STATE_DIR=/tmp/mrrc-ap-demo MRRC_WEB_PORT=8899 \
  $PY server.py --port 8899 --host 127.0.0.1 --no-ssl &
sleep 4
curl -s http://127.0.0.1:8899/api/setup/wizard | head -c 400; echo
open "http://127.0.0.1:8899/setup"     # macOS；Linux 用 xdg-open
```

看四件事：① 状态卡显示"初始化热点已开启"、热点名与倒计时在走；② 第 2 步是灰的，且提示文案解释了为什么；③ 设完口令后第 2 步解锁；④ 点"扫描"会得到空列表（本机没有 nmcli），提示文案是"没有扫到任何网络…"而**不是**一片空白或一个未捕获异常。
看完 `kill %1` 并 `rm -rf /tmp/mrrc-ap-demo`。

> **心跳会过期**：`state.json` 是一次性写死的，45 秒后闸门自动关闭，页面会变成 `/login`。要长时间看页面就重跑那段 `$PY - <<EOF` 把 heartbeat 刷新一次——这本身就是"闸门跟着心跳走"的一次真实验证。

- [ ] **步骤 6：确认没有弄坏别的**

运行：`$PY -m unittest discover -s tests 2>&1 | tail -4`
预期：`FAILED (failures=1, skipped=1)`，唯一 `FAIL:` 是 `test_tls_trust_store`

特别看住 `tests/test_static_cache_busting.py`（不应受影响：`setup.html` 既不在 `index.html` 的引用里，也不在 `sw.js` 的 ASSETS 里）与 `tests/test_cloud_endpoints.py::ServerRouteOrderTests`。

- [ ] **步骤 7：SDD 检查 + Commit**

```bash
$PY .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git add static/setup.html server.py tests/test_server_setup_wizard.py
git commit -m "feat: /setup 向导页——自包含、免口令只走热点、切网前先把后果说清楚"
```


---

## 任务 9：打包 —— `dnsmasq`、systemd 单元、真机验收脚本

**文件：**
- 修改：`packaging/box/box-overlay.sh`
- 修改：`packaging/box/verify.sh`
- 测试：`tests/test_box_profiles.py`（追加一个类）

**两个失败模式在真机上是"看起来成功了"，所以必须在这里钉住：**
1. **缺 `dnsmasq`**（规格 D-2 实测：钉死的那个基底镜像带 `mac80211`/`cfg80211`/`wpa_supplicant`/`iptables`，**不带** `dnsmasq`）。NM 的 `ipv4.method=shared` 靠它发 DHCP 与 DNS。缺了它：热点**会出现**、手机**能连上**、然后**拿不到地址**，任何地方都没有错误。这是这个功能可能有的最令人困惑的故障。
2. **单元等 `network-online.target`**。`mrrc-modern.service` 是这么写的（它有网线时是对的），但这个服务**存在的理由就是没有网**。照抄那两行 ⇒ 热点要等 NM 的 wait-online 超时才出现。

- [ ] **步骤 1：编写失败的测试**

在 `tests/test_box_profiles.py` 末尾（`if __name__` 之前）追加：

```python
class SetupApPackagingTests(unittest.TestCase):
    """The setup hotspot's place in the image (design 2026-10-08-w103d-setup-ap).

    Every one of these is invisible from reading the overlay and expensive in the
    field, because the box being onboarded is by definition a box nobody can ssh
    into. A missing dnsmasq means the hotspot appears and a phone joins it and
    gets no address; a unit ordered after network-online.target means the service
    that exists precisely to cover "there is no network" waits for one.
    """

    OVERLAY = REPO / "packaging" / "box" / "box-overlay.sh"
    VERIFY = REPO / "packaging" / "box" / "verify.sh"
    UNIT = "mrrc-setup-ap.service"

    def setUp(self):
        self.text = self.OVERLAY.read_text(encoding="utf-8")

    def unit_body(self) -> str:
        """The heredoc body of this unit's `cat > … <<'UNIT'` block.

        Slicing from the unit's name to the next "UNIT" does not work: the name
        appears on the `cat` line, whose own heredoc marker is the next thing on
        it, so the slice comes back empty and every assertion passes vacuously.

        Nor does searching for the literal "UNIT\n" work: the opener is *quoted*
        (`<<'UNIT'`), so the bytes there are `UNIT'\n` and the first bare
        "UNIT\n" in the file is the **closer** — slicing from it finds nothing
        and raises. Take the end of the `cat` line, then the next line that is
        exactly UNIT.
        """
        start = self.text.index(f"/etc/systemd/system/{self.UNIT}")
        opener = self.text.index("\n", start) + 1
        closer = self.text.index("\nUNIT\n", opener)
        body = self.text[opener:closer]
        self.assertIn("[Service]", body, "the slice missed the unit body")
        return body

    def apt_packages(self) -> list:
        """The package tokens of the overlay's single `apt-get install`.

        Parsed as tokens rather than grepped as a substring, because a substring
        match is satisfied by the *comment explaining why the package is there*.
        That is not hypothetical: mutating the install list to
        "# dnsmasq deliberately omitted" left a substring assertion green while
        shipping an image whose hotspot hands out no addresses.
        """
        install = self.text[self.text.index("apt-get install"):]
        install = install[:install.index("\n\n")]
        packages: list = []
        for line in install.splitlines():
            line = line.split("#", 1)[0]              # drop trailing comments
            for token in line.replace("\\", " ").split():
                if token in ("DEBIAN_FRONTEND=noninteractive", "apt-get",
                             "install", "-y", "-qq", "--no-install-recommends"):
                    continue
                packages.append(token)
        return packages

    def test_dnsmasq_is_in_the_apt_install_list(self):
        """Design D-2. NM's shared mode hands out addresses through dnsmasq; the
        pinned base image does not carry it."""
        self.assertIn("dnsmasq", self.apt_packages())

    def test_the_package_list_is_what_the_overlay_actually_installs(self):
        """Guard the guard: if this parser drifts, the assertion above goes
        vacuous instead of loudly wrong."""
        packages = self.apt_packages()
        for expected in ("python3.11-venv", "python3-dev", "portaudio19-dev",
                         "libportaudio2", "libasound2-dev", "libopus0",
                         "libopus-dev", "dnsmasq"):
            with self.subTest(package=expected):
                self.assertIn(expected, packages)
        self.assertNotIn("apt-get", packages)
        self.assertTrue(all("#" not in p for p in packages), packages)

    def test_dnsmasq_joins_the_existing_install_rather_than_a_second_apt_run(self):
        """A second `apt-get install` in a chroot costs another resolver hit and
        another chance to stall on a slow mirror (the w103d-box skill's 坑 5)."""
        self.assertEqual(self.text.count("apt-get install"), 1)

    def test_the_unit_is_written(self):
        self.assertIn(f"/etc/systemd/system/{self.UNIT}", self.text)

    def test_the_unit_runs_the_daemon_with_the_venv_python(self):
        """The system python has none of the dependencies, and the script's exec
        bit is not what starts it — the w103d-box skill's 坑 4 is exactly a script
        whose permissions the build did not set."""
        body = self.unit_body()
        self.assertIn("/opt/mrrc_modern/venv/bin/python", body)
        self.assertIn("/opt/mrrc_modern/linux/setup_ap.py", body)

    def directives(self) -> str:
        """The unit body with its comments stripped.

        Assertions about ordering have to look at directives, not prose: the unit
        carries a comment explaining *why* it does not wait for a network, and
        that comment names the target. Grepping the raw body would make the
        explanation look like the mistake it is warning against.
        """
        body = self.unit_body()
        return "\n".join(line for line in body.splitlines()
                         if not line.lstrip().startswith("#"))

    def test_the_unit_does_not_wait_for_a_network(self):
        """The whole point of this service is that there isn't one.

        mrrc-modern.service does wait for network-online.target, and copying
        those two lines here is the obvious mistake: the hotspot would then
        appear only after NM's wait-online times out, or not at all.
        """
        directives = self.directives()
        self.assertNotIn("network-online.target", directives)
        self.assertIn("After=NetworkManager.service", directives)
        self.assertIn("Wants=NetworkManager.service", directives)

    def test_the_unit_is_resident_not_oneshot(self):
        """Design §7: the wizard page — and later /manage — need to ask whether
        the hotspot is up and how long is left. A oneshot has exited by then."""
        body = self.unit_body()
        self.assertNotIn("Type=oneshot", body)
        self.assertIn("Restart=always", body)

    def test_the_unit_runs_as_root(self):
        """nmcli needs polkit authority over system connections, and the state
        directory under /run has to be creatable with group `mrrc` so the server
        can leave its switch request there. `User=mrrc` here is a hotspot that
        never comes up."""
        self.assertNotIn("User=mrrc", self.unit_body())

    def test_the_banner_reaches_the_hdmi_console(self):
        """Design D-6's mitigation for "a neighbour wins the race": the address is
        printed on the console too, so an operator with a monitor never needs the
        open hotspot at all. Journal-only output does not reach the getty."""
        self.assertIn("console", self.unit_body())

    def test_the_unit_is_enabled(self):
        enabled = [line for line in self.text.splitlines()
                   if "systemctl enable" in line]
        self.assertTrue(enabled, "the overlay no longer enables any service?")
        self.assertIn(self.UNIT, enabled[-1])

    def test_the_daemon_and_its_module_reach_the_image(self):
        """Both ride the existing rsync — `linux/` and the repo root are not in the
        exclusion list. Worth asserting because the symptom of getting it wrong is
        a unit that fails to start on a box nobody can reach."""
        excludes = ExclusionParityTests.EXCLUDE_RE.findall(self.text)
        self.assertTrue(excludes, "the parser found no excludes — drift?")
        self.assertNotIn("linux/", excludes)
        self.assertNotIn("net_wifi.py", excludes)
        self.assertTrue((REPO / "linux" / "setup_ap.py").is_file())
        self.assertTrue((REPO / "net_wifi.py").is_file())

    def test_the_overlay_leaves_the_env_file_alone(self):
        """D-4: the WiFi domain has one writer and it is not the env file. Nothing
        in the overlay may add an MRRC_SETUP_AP_* key to mrrc.env — the defaults
        live in net_wifi.ap_settings()."""
        for key in ("MRRC_SETUP_AP_SSID", "MRRC_SETUP_AP_STATE_DIR",
                    "MRRC_SETUP_AP_TIMEOUT_MIN"):
            with self.subTest(key=key):
                self.assertNotIn(key, self.text)


class VerifyScriptTests(unittest.TestCase):
    """verify.sh is the only diagnostic that works on a box nobody can reach —
    but only *after* onboarding, over the network the wizard just configured."""

    SCRIPT = REPO / "packaging" / "box" / "verify.sh"

    def setUp(self):
        self.text = self.SCRIPT.read_text(encoding="utf-8")

    def test_the_section_numbers_are_contiguous_and_agree_on_a_total(self):
        """Renumbering by hand is how a script ends up printing 3/10 in a run of
        eleven checks. The headers are the authority."""
        heads = re.findall(r'head_ "(\d+)/(\d+) ', self.text)
        self.assertTrue(heads, "the header shape changed — update this guard")
        totals = {total for _, total in heads}
        self.assertEqual(len(totals), 1, f"mixed denominators: {heads}")
        self.assertEqual(int(totals.pop()), len(heads))
        self.assertEqual([int(n) for n, _ in heads],
                         list(range(1, len(heads) + 1)))

    def test_it_checks_the_setup_hotspot(self):
        self.assertIn("mrrc-setup-ap.service", self.text)
        self.assertIn("dnsmasq", self.text)

    def test_a_box_that_already_has_a_network_is_not_failed_for_having_no_hotspot(self):
        """By design (D-6 fence 1) the hotspot is DOWN once the box has an uplink.
        A check that demands it be up fails on every healthy box, and the operator
        reads "your image is broken" about a box that is fine.

        Asserted structurally, because a substring search for a *regex* is
        vacuous — it matches nothing, ever, and so passes no matter what the
        script does (this test shipped that way once and caught nothing):
        `$mode` may be **reported**, inside an assignment or an `ok`/`printf`
        line, but must never be **tested**. A conditional on it is the bug.
        """
        section = self.text[self.text.index("setup hotspot"):]
        section = section[:section.index("printf '\\n────")]
        mentioning = [line.strip() for line in section.splitlines()
                      if "$mode" in line or "mode=" in line]
        self.assertTrue(mentioning,
                        "the section no longer reports the mode at all — "
                        "this guard has gone vacuous")
        for line in mentioning:
            with self.subTest(line=line):
                self.assertFalse(line.startswith(("if ", "elif ", "while ", "[ ")),
                                 "the published mode is being tested, not reported")
                self.assertNotIn("!=", line)

    def test_the_failure_branches_are_about_the_machinery_not_the_mode(self):
        """What may legitimately fail: the unit missing, not enabled, dnsmasq
        absent, or the daemon dead. All four are about the machinery being there,
        which is what a post-onboarding run can still verify."""
        section = self.text[self.text.index("setup hotspot"):]
        for expected in ("is not installed", "not enabled", "dnsmasq is missing",
                         "is not running"):
            with self.subTest(expected=expected):
                self.assertIn(expected, section)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`$PY -m unittest tests.test_box_profiles.SetupApPackagingTests tests.test_box_profiles.VerifyScriptTests -v 2>&1 | tail -8`
预期：FAIL/ERROR — `ValueError: substring not found`（`/etc/systemd/system/mrrc-setup-ap.service` 与 `setup hotspot` 都还不存在），以及 `assertIn("dnsmasq", …)` 红

- [ ] **步骤 3：改 `box-overlay.sh`（三处）**

**3a. 装机清单加 `dnsmasq`。** 找到：

```bash
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
  python3.11-venv python3-dev \
  portaudio19-dev libportaudio2 libasound2-dev \
  libopus0 libopus-dev
```

改成：

```bash
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

**3b. 写单元。** 在 `mrrc-firstboot.service` 那个 heredoc 的收尾 `UNIT` 之后、`mkdir -p "$MRRC_HOME/linux" …` 之前插入：

```bash
cat > /etc/systemd/system/mrrc-setup-ap.service <<'UNIT'
[Unit]
Description=MRRC Modern setup access point (onboarding with no cable and no keyboard)
# Deliberately no Documentation= line: it would have to point at
# /opt/mrrc_modern/docs/W103D_GUIDE.md, and box-overlay.sh's rsync excludes
# docs/ — a field naming a file that is not in the image is pure misdirection.
# Deliberately NOT network-online.target, which mrrc-modern.service does wait
# for: this unit exists precisely because there may be no network, and ordering it
# behind that target makes the hotspot appear only after NM's wait-online times
# out. NetworkManager itself has to be up, because everything here is nmcli.
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=simple
# Root, not mrrc: nmcli needs polkit authority over system connections, and the
# state directory under /run has to be creatable with group `mrrc` so the server
# (which runs as mrrc) can leave its switch request there.
ExecStart=/opt/mrrc_modern/venv/bin/python /opt/mrrc_modern/linux/setup_ap.py
Restart=always
RestartSec=5
# journal for support bundles and `journalctl -u mrrc-setup-ap`; console because
# the banner carries the address an operator with a HDMI monitor needs, and
# design D-6's mitigation for "a neighbour wins the race" is that they never have
# to use the open hotspot at all. The loop only logs on transitions, so this is
# not noise.
StandardOutput=journal+console
StandardError=journal+console
SyslogIdentifier=mrrc-setup-ap

[Install]
WantedBy=multi-user.target
UNIT
```

**3c. enable 它。** 找到：

```bash
systemctl enable mrrc-firstboot.service mrrc-modern.service
```

改成：

```bash
# mrrc-setup-ap first: it is the only way in for an operator who has a phone and
# nothing else, and it has to be up before the box is handed to anybody.
systemctl enable mrrc-setup-ap.service mrrc-firstboot.service mrrc-modern.service
```

- [ ] **步骤 4：改 `verify.sh`（两处）**

**4a. 把十个分节的分母改成 11**（一次改完，别手改）：

```bash
perl -pi -e 's{/10 }{/11 }g' packaging/box/verify.sh
grep -n 'head_ "' packaging/box/verify.sh      # 应看到 1/11 … 10/11
```

**4b. 在末尾那段 `printf '\n────` 汇总**之前**插入第 11 节：**

```bash
head_ "11/11 setup hotspot (onboarding with no cable and no keyboard)"
# This runs *after* onboarding, over the network the wizard just configured, so a
# healthy box reports mode 'off' or 'sta' — the hotspot being down is design D-6
# fence 1, not a failure. What must be true is that the machinery is installed and
# the daemon is alive, because the alternative is a box that can never be
# onboarded without a keyboard.
if ! command -v systemctl >/dev/null 2>&1; then
  bad "systemctl not found" "this is not a systemd host"
elif ! systemctl list-unit-files --no-legend 2>/dev/null | grep -q "^${SETUP_UNIT}\s"; then
  bad "${SETUP_UNIT} is not installed" \
      "this image predates the setup hotspot; rebuild it (design 2026-10-08)"
elif ! systemctl is-enabled --quiet "$SETUP_UNIT"; then
  bad "${SETUP_UNIT} is installed but not enabled" \
      "systemctl enable ${SETUP_UNIT}"
elif ! command -v dnsmasq >/dev/null 2>&1; then
  bad "dnsmasq is missing" \
      "the hotspot comes up but hands out no addresses (design D-2); add it to box-overlay.sh"
elif systemctl is-active --quiet "$SETUP_UNIT"; then
  mode="$(sed -n 's/^ *"mode": *"\([^"]*\)".*/\1/p' "$AP_STATE" 2>/dev/null | head -1)"
  ok "service active; published mode='${mode:-none}' (off/sta is correct once the box has a network)"
  opened="$(journalctl -u "$SETUP_UNIT" --no-pager 2>/dev/null | grep -c 'SETUP HOTSPOT OPEN' || true)"
  printf '      (the hotspot has been opened %s time(s) in the current journal)\n' "${opened:-0}"
else
  bad "${SETUP_UNIT} is not running" \
      "journalctl -u ${SETUP_UNIT} -n 50 --no-pager"
fi
```

并在文件顶部那两个变量旁边（`ENV_FILE=` / `UNIT=` 之后）加：

```bash
SETUP_UNIT=mrrc-setup-ap.service
AP_STATE=/run/mrrc/setup-ap/state.json
```

- [ ] **步骤 5：运行测试验证通过**

运行：
```bash
$PY -m unittest tests.test_box_profiles -v 2>&1 | tail -8
```
预期：`OK`，其中 `SetupApPackagingTests` 约 11 项、`VerifyScriptTests` 3 项全绿

排障提示：
- `unit_body()` 报 `ValueError: substring not found` ⇒ heredoc 的收尾标记不是单独一行的 `UNIT`（`box-overlay.sh` 里所有单元都用 `<<'UNIT'` … `UNIT`）。
- `test_dnsmasq_joins_the_existing_install_rather_than_a_second_apt_run` 红 ⇒ 你新加了一次 `apt-get install`。加到**既有那条**的清单末尾。
- `test_the_section_numbers_are_contiguous…` 红 ⇒ `perl -pi -e` 漏跑了，或者新加的节号不是 11。
- `test_a_box_that_already_has_a_network_is_not_failed…` 红 ⇒ 第 11 节里出现了"mode 不是 hotspot 就 bad"的写法。热点**关着**是正确状态。

- [ ] **步骤 6：确认没有弄坏别的**

运行：`$PY -m unittest discover -s tests 2>&1 | tail -4`
预期：`FAILED (failures=1, skipped=1)`，唯一 `FAIL:` 是 `test_tls_trust_store`

特别看住 `tests/test_box_profiles.py::BoxBuildIntegrityTests`（`test_the_mirror_swap_covers_both_debian_suites_and_not_armbian` 会解析 apt 相关的行）与 `ExclusionParityTests`（本任务不动 `--exclude`，应当不受影响）。

再跑一次 bash 语法与可执行位（w103d-box 技能的坑 4 与坑 8）：

```bash
bash -n packaging/box/box-overlay.sh && bash -n packaging/box/verify.sh && echo "syntax ok"
ls -l packaging/box/*.sh | awk '{print $1, $NF}'   # 全部应为 -rwxr-xr-x
```

- [ ] **步骤 7：SDD 检查 + Commit**

```bash
$PY .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git add packaging/box/box-overlay.sh packaging/box/verify.sh tests/test_box_profiles.py
git commit -m "box: 镜像装 dnsmasq 与 mrrc-setup-ap 单元——它不等 network-online，因为它就是为了没网时存在"
```


---

## 任务 10：文档 —— 指南、AGENTS、测试计数

**文件：**
- 修改：`docs/W103D_GUIDE.md`（§6、§9 引言、§9.1、§9.6、§12、末尾清单）
- 修改：`packaging/box/README.md`（§8 验收的项数）
- 修改：`AGENTS.md`（根模块表加一行）
- 修改：`tests/README.md`（总数 + 三个新模块小节）
- 修改：`tests/test_spectrum_profile_docs.py`（**钉住总数的那条断言**）

**这一任务里有一条是"实现过程中发现的文档错误"，不是顺带润色**：指南 §6 写着
**"网线必须插着——首启要联网"**。首启做的四件事（生成口令、自签证书、探测串口、探测声卡）
**全部在本机完成**，`firstboot_wrapper.py` 与 `ssl_bootstrap.py` 都不下载任何东西。
这句话在有了热点引导之后不只是不准确，它**直接否掉了本功能的验收场景**（"不插网线"）。

- [ ] **步骤 1：改 `docs/W103D_GUIDE.md` §6**

把这段：

```markdown
**网线必须插着**——首启要联网。开机后 `mrrc-firstboot.service` 跑完会把自己标记成"已做"，
之后不再重复。
```

改成：

```markdown
**首启不需要联网**：上面四件事全在本机完成（生成口令、自签证书、探测串口与声卡），
不下载任何东西。开机后 `mrrc-firstboot.service` 跑完会把自己标记成"已做"，之后不再重复。

**既没有网线、也没有能连上的已保存 Wi-Fi 时**，`mrrc-setup-ap.service` 会另外开一个
**开放热点**让你进去配置 —— 见 §9.6。它按**网络状态**判断，不按"是否首次开机"：
盒子一旦有了可用网络，热点就不存在了，那个免口令页面也随它一起消失。
```

- [ ] **步骤 2：改 §9 的引言与 §9.1 的表格**

把这段：

```markdown
> **目标形态**：刷完盘开机，盒子自己发一个**开放热点**，手机连上就能完成初始化。
> **这一条还没实现**（设计已批准，见 §9.6）。在那之前，按你手边有什么，用下面四条路之一。
```

改成：

```markdown
> **刷完盘开机，盒子自己发一个开放热点，手机连上就能完成初始化** —— 这条已经实现了，
> 见 §9.6。**只有一台手机也能把整个盒子配完。** 下面另外三条路仍然可用，按你手边有什么选。
```

把表格第 4 行：

```markdown
| 4 | **只有一台手机** | 热点引导：盒子自己发热点 | ⏳ **尚未实现** → §9.6 |
```

改成：

```markdown
| 4 | **只有一台手机** | 热点引导：盒子自己发热点 | ✅ **能用** → §9.6 |
```

把那条 ⚠️ 限制：

```markdown
⚠️ **先说一个限制**：第 2 条（预置）**只能写口令与电台设置，写不了 Wi-Fi 密码**——Wi-Fi 归
NetworkManager 管，不走 env 文件。所以要连 Wi-Fi，眼下只有第 3 条（有键盘）或第 1 条（有网线）。
**这正是第 4 条存在的理由。**
```

改成：

```markdown
⚠️ **一个限制**：第 2 条（预置）**只能写口令与电台设置，写不了 Wi-Fi 密码**——Wi-Fi 归
NetworkManager 管，不走 env 文件（设计 D-4：两个域各有唯一写入者）。所以要连 Wi-Fi，
用第 4 条（**一台手机，零键盘零网线**）、第 3 条（有键盘）或第 1 条（有网线）。
```

- [ ] **步骤 3：把 §9.6 整节替换掉**

原文以 `### 9.6 第 4 条：热点引导（**尚未实现**）` 开头、到 `## 10. 接电台` 之前结束。
**整节**替换为：

````markdown
### 9.6 第 4 条：热点引导（**零键盘零网线，只要一台手机**）

盒子上电后，如果**既没有网线链路、也没有能连上的已保存 Wi-Fi**，它会自己开一个
**开放热点**：

| | |
| --- | --- |
| Wi-Fi 名 | **`MRRC-Setup`**（`MRRC_SETUP_AP_SSID` 可改） |
| 密码 | **无**（开放热点，故意的——你还没有办法被告知一个密钥） |
| 页面 | **`https://10.42.0.1:8888/setup`** |
| 窗口 | **30 分钟**，用完自动关（`MRRC_SETUP_AP_TIMEOUT_MIN` 可改） |

**操作步骤：**

1. 盒子上电，等 **约 15–30 秒**，在手机 Wi-Fi 列表里找 `MRRC-Setup` 并连上（无密码）。
   手机可能提示"该网络无互联网连接"——**这是对的**，选「保持连接」。
2. 浏览器打开 **`https://10.42.0.1:8888/setup`**。
   会先弹一次**证书警告**（盒子用的是自签证书）：选「高级 / 详细信息」→「继续访问」。
   这一步绕不过去，也不该绕——它只出现在**盒子自己开的那个热点**上。
3. 页面上按顺序做两件事：
   - **第 1 步：设置登录口令**（至少 8 位）。**必须先做**：切网之后热点会关，这个免口令
     页面也随之消失。此刻盒子里生效的口令是首启自动生成的随机值、**你并不知道**，
     所以不先设成自己的就会被锁在盒子外面（服务端也会用 409 挡住"先切网"）。
   - **第 2 步：扫描并选择你的 Wi-Fi**，输入密码，点「连接并切换到这个 Wi-Fi」。
4. 点下去之后**手机会与盒子断开**——盒子只有一颗射频（MT7663S，Wi-Fi 与蓝牙同一颗芯片，
   走 SDIO），AP 与 STA **不能同时存在**（设计 D-3）。页面在断开**之前**就把后续步骤显示
   出来了，照着做：把手机连回你自己的 Wi-Fi，然后在路由器后台的客户端列表里找到盒子，
   用 `https://它的IP:8888` 打开控制界面，用刚设的口令登录。

**如果 Wi-Fi 密码输错了**：盒子会**自动把热点重新开回来**（设计上禁止静默失败，见 §6 数据流），
手机重连 `MRRC-Setup`、刷新页面，就能看到失败原因并直接重试。一次失败还会**重置那 30 分钟
窗口**，所以多试几次不会把窗口耗光。

**窗口用完了 / 错过了怎么办**：**断电重启**。那个 30 分钟的闩是**按开机次数**算的，不写在
盘上——所以只要盒子还没有可用网络，每次上电都会重新给一次机会。（这是设计 D-6 的恢复路径；
把它做成持久状态就等于把盒子变砖。）

**插上网线会怎样**：热点**立刻关闭**，免口令页面随之失效。判据是**网络状态**而不是"是否配置
过"，所以一台配好的盒子永远不会再开热点。

**有显示器的人可以完全不碰热点**：`mrrc-setup-ap` 会把地址打到 **HDMI 控制台**上
（`journalctl -u mrrc-setup-ap` 里也有同一行）。这是设计对"开放热点期间被邻居抢先设口令"
那条**残留风险**的缓解手段——你不必依赖热点。

**排查：**

| 现象 | 查什么 |
| --- | --- |
| 手机里看不到 `MRRC-Setup` | `journalctl -u mrrc-setup-ap -n 50 --no-pager`。有 `could not start the setup hotspot` ⇒ 看它后面那句 nmcli 的原因；`rfkill list` 显示软阻塞就 `rfkill unblock wifi`；再往下按 §9.5 的顺序查驱动与固件 |
| 连上了热点但打不开页面 | `nmcli device show wlan0 \| grep IP4`（盒子应有 `10.42.0.1/24`）；`command -v dnsmasq` —— **缺 dnsmasq 的话手机能连上热点但拿不到地址**，任何地方都不报错，这是设计 D-2 专门点名的那个坑 |
| 打开的是 `/login` 而不是向导 | 免口令闸门没开。`cat /run/mrrc/setup-ap/state.json` 看 `mode` 是不是 `hotspot`、`heartbeat` 是不是最近 45 秒内的；再确认你**确实**是从热点网段访问的（不是从家里 Wi-Fi） |
| 点"连接并切换"后一直转圈 | 这时手机应该已经掉线了，按页面说的连回你自己的 Wi-Fi。**如果手机又自动连回了 `MRRC-Setup`，说明切换失败**，刷新页面看红字原因 |
| 切过去了但找不到盒子 IP | 路由器客户端列表里按主机名找；盒子自己也记了一行：`journalctl -u mrrc-setup-ap \| grep joined` |
| 想改热点名 / 窗口时长 | 编辑 `/opt/mrrc_modern/env/mrrc.env`，加 `MRRC_SETUP_AP_SSID=…` 或 `MRRC_SETUP_AP_TIMEOUT_MIN=…`，然后 `systemctl restart mrrc-setup-ap` |

> **仍未在真机上验证的部分**（设计 §10）：MT7663S 这颗 **SDIO 变体能否真的起 AP 模式**、
> 切换过程中手机端的重连体验、以及 `10.42.0.0/24` 与你家网络是否冲突。
> 上表第一行就是为第一条准备的——设计上**不依赖它成立**：真起不来的时候，
> HDMI + `nmtui`（§9.4）永远可用。
````

- [ ] **步骤 4：改 §12 的验收项数与表格**

把 `**10 项，每项失败都会给出排查命令。先跑这个，再动手调别的。**` 改成
`**11 项，每项失败都会给出排查命令。先跑这个，再动手调别的。**`

把表格里 10 行的 `N/10` 全部改成 `N/11`，并在 `| 10/11 | **PTT 安全上限**…` 之后加一行：

```markdown
| 11/11 | **初始化热点**（`mrrc-setup-ap.service` 已装且已 enable、`dnsmasq` 在、守护进程活着） |
```

在那段"前 3 项就是硬件三件套"之后补一句：

```markdown
第 11 项**不要求热点此刻是开着的**——恰恰相反，一台已经有网络的盒子**应该**报 `off`
（设计 D-6 围栏①）。它查的是"这套机器装上了、守护进程活着"，因为出问题的时刻你正好
连不上盒子、没法查。
```

- [ ] **步骤 5：改末尾那份清单**

把 `- [ ] \`verify.sh\` 10 项全过` 改成 `- [ ] \`verify.sh\` 11 项全过`，
并在它**之前**插入一条（顺序上它属于"拿到盒子之后最先做的事"）：

```markdown
- [ ] （没有网线时）**只用手机**走通 §9.6：看到 `MRRC-Setup` → 连上 → 设口令 → 选 Wi-Fi → 盒子切过去、热点关掉
```

- [ ] **步骤 6：改 `packaging/box/README.md` §8**

把 `10 项，每项失败都给出排查命令。**先跑这个再动手调别的**。` 改成：

```markdown
11 项，每项失败都给出排查命令。**先跑这个再动手调别的**。

第 11 项查的是**初始化热点**那套机器（`mrrc-setup-ap.service` 装了没、enable 了没、
`dnsmasq` 在不在、守护进程活着没）。它**不要求热点此刻开着**——一台有网络的盒子
应该报 `off`，那是设计 D-6 的围栏①。真正的"只拿手机配完一台盒子"是操作单
`docs/W103D_GUIDE.md` §9.6，脚本替不了它。
```

- [ ] **步骤 7：改 `AGENTS.md` 的根模块表**

在 `session_metrics.py` 那一行**之后**插入一行（保持表格的既有风格：一句话说清职责、
钉住的那条不变量、以及"Stdlib only, no app imports"这个口径）：

```markdown
| `net_wifi.py` / `linux/setup_ap.py` | Setup access point (W103D onboarding, design `2026-10-08-w103d-setup-ap-design.md`): `net_wifi` is the **only** code that talks to `nmcli` — the uplink decision, the open-hotspot lifecycle, scan/join, and the two JSON mailboxes plus the passwordless gate predicate; `linux/setup_ap.py` is the resident `mrrc-setup-ap.service` that raises the hotspot **iff there is no usable uplink**, closes it after `MRRC_SETUP_AP_TIMEOUT_MIN` (default 30) and does not reopen until a power cycle, and stands down while the server performs the AP→STA switch. Two invariants that fail silently if broken: **our own hotspot reads `connected` in `nmcli device status`, so it is not an uplink** (else the supervisor raises and tears it down every tick), and **a WiFi password never leaves `nmcli`'s argv** — `WizardClaim` has no password field at all (SDD `support-bundle-privacy`). Both modules are stdlib-only with no app imports, and every nmcli call takes an injectable runner so the whole thing is testable on a machine with no NetworkManager |
```

- [ ] **步骤 8：跑一次全套，取真实计数**

```bash
$PY -m unittest discover -s tests 2>&1 | tail -3
$PY -m unittest discover -s tests 2>&1 | grep -c "^\(test\|ERROR\|FAIL\)" || true
ls tests/test_*.py | wc -l
```

记下两个数：**总测试数**（`Ran NNNN tests`，应当 > 1766）与**模块数**（`ls` 的结果，应当是 96）。
下一步要用**实跑出来的数**，不许沿用旧值、也不许估。

- [ ] **步骤 9：改 `tests/README.md`**

三处，全部用步骤 8 的真实数字（下面写作 `<N>` 与 `<M>`）：

1. Overview 段：`1766 tests across 93 test modules (19 skip on Windows, 1 on macOS; totals re-read from \`unittest discover\` on 2026-10-07, macOS)` → `<N> tests across <M> test modules (19 skip on Windows, 1 on macOS; totals re-read from \`unittest discover\` on <今天日期>, macOS)`；同一句里 `itemise 71 of those 93` → `itemise 71 of those <M>`（**分子不动**：下面那 71 个小节没有增减，本次新增的模块写在"本轮新增"分组里，与既有做法一致）。
2. Test Results Summary 表：`| Total tests | 1766 |` → `| Total tests | <N> |`；`| Passed | 1764 (1 skipped) |` 按 `<N>` 相应调整。
3. 在最后一个 `### 本轮新增（…）` 分组**之后**追加一个新分组：

```markdown
### 本轮新增（W103D 初始化热点与最小向导，2026-10-08）

- **`test_net_wifi.py`** — nmcli 适配层。`-t` 输出的转义解析（SSID 里带 `:`）、
  两套状态词汇（`connected` / `activated`，以及**不算**上行的 `connected (site only)`）、
  **自己的热点不算上行**、AP 与 STA 靠 `802-11-wireless.mode` 区分、开放热点的幂等起停
  （已开的不动、同名**非活动** profile 先删、活动中的绝不删）、扫描去重排序、
  连网时 PSK 在 argv 里恰好出现一次、`scrub()`；两份 JSON 邮箱的容错读、
  **0644** 与原子替换、心跳新鲜度、以及免口令闸门（含 **IPv4-mapped 地址必须解包**
  这条双栈陷阱与"陈旧心跳即关门"的负例）。
- **`test_setup_ap.py`** — 常驻守护进程。无上行才开、有网线/有 STA 就关、
  **热点自己不算上行**、30 分钟超时后**本次开机不再重开**（断电重启才重开）、
  重启后**领养**已在跑的热点并补一个窗口、向导切网期间**让位**且窗口按让位时长补偿、
  失败按 **nonce 一次性**重置窗口、stale claim 被忽略、起不来的热点**每 60 s 只抱怨一次**、
  `run()` 里 tick 抛异常不致命、以及 settle 窗口（NM 还在关联时不许下"没网"的结论）。
- **`test_server_setup_wizard.py`** — 免口令闸门与四个端点。闸门只在
  "活心跳 + 热点网段"同时成立时开、**中间件的放行集合是闭合的且按相等比较**、
  **listen-only 令牌在处理函数里被拒**（中间件那条 403 对这些路径必然被跳过）、
  设口令走**既有写入通道**且**只写两个键**、"先设口令再切网"的 409、
  切换**先发回答再动手**、失败**必须**把热点开回来并带原因、
  **PSK 既不落盘也不进日志**（直接断言字节）、以及 `/setup` 注册在 SPA 兜底之前。
```

- [ ] **步骤 10：改钉住计数的那条断言**

`tests/test_spectrum_profile_docs.py::test_tests_readme_matches_the_actual_count`
**钉死了** README 里的总数，所以改了 README 就必须同步改它，否则套件红。
按该测试自己的写法（保留旧值作为"不许留下的陈旧副本"）改成：

```python
    def test_tests_readme_matches_the_actual_count(self):
        """The number below is the real `unittest discover` output."""
        text = _read("tests/README.md")
        self.assertNotIn("1673", text)
        # 1751/92 was the count before the W103D box guards and the install.sh
        # variable scanner landed; 1766/93 was the count before the setup hotspot
        # and its minimal wizard. A stale copy must not be left behind.
        self.assertNotIn("1751", text)
        self.assertNotIn("1766", text)
        self.assertNotIn("92 test modules", text)
        self.assertNotIn("93 test modules", text)
        self.assertIn("<N>", text)
        self.assertIn("<M> test modules", text)
```

> `<N>` / `<M>` 用步骤 8 的实跑数字替换。**别把 `1766` 同时留在 `assertNotIn` 和
> `assertIn` 里**——那会让这条测试永远红。

- [ ] **步骤 11：改规格文档的交付状态**

在 `docs/superpowers/specs/2026-10-08-w103d-setup-ap-design.md` 里两处：

1. D-7.5 那张交付表的第 1 行，`| **1** | **热点 + 最小向导**（设口令 + 连 Wi-Fi） | 只依赖既有写入通道 ✅ **可以立刻开工** |`
   改成 `| **1** | **热点 + 最小向导**（设口令 + 连 Wi-Fi） | 只依赖既有写入通道 ✅ **已交付**（实现计划 `docs/superpowers/plans/2026-10-08-w103d-setup-hotspot-wizard.md`） |`
2. 在 D-7.5 那段"摸底发现"里，把 `server.py:3216 会写 updates["MRRC_WEB_PASSWORD"]，经 cloud_hub._write_config(_config_file_path(), {...}) 落盘` 这句**更正**为：

```markdown
摸底发现：**服务端已经有一条写配置的现成通道** —— `server.py:3216` 会写
`updates["MRRC_WEB_PASSWORD"]`，并在几行之后经
`first_run.update_env_file(_config_file_path(), updates)` 落盘
（**2026-10-08 实现时更正**：这一条走的是 `first_run.update_env_file`，不是
`cloud_hub._write_config`；后者是**另一处**写入者，只被 `_ensure_strong_password()`
用来保存它自己生成的随机口令。所以 D-8 要收敛的写入者数目不变，但"既有通道"指的是
`first_run` 那一条，向导照抄的也是它）。
```

- [ ] **步骤 12：跑全套 + Commit**

```bash
$PY -m unittest discover -s tests 2>&1 | tail -4     # 仍只有 test_tls_trust_store 一条红
$PY .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git add docs/W103D_GUIDE.md packaging/box/README.md AGENTS.md tests/README.md \
        tests/test_spectrum_profile_docs.py \
        docs/superpowers/specs/2026-10-08-w103d-setup-ap-design.md
git commit -m "docs: 热点引导从「尚未实现」变成操作单；首启本来就不需要联网"
```

> 排障：`test_tests_readme_matches_the_actual_count` 红 ⇒ README 与断言里的数字不一致。
> `test_website_links` / `test_release_artifacts` 不该受本任务影响；若它们红了，
> 先确认你没有顺手改到 `website/` 或 `CHANGELOG.md`（本任务不动它们，版本号在任务 11 之后
> 的发布步骤才动）。


---

## 任务 11：SDD docs-sync —— AD-026 与版本串

**文件：**
- 修改：`SDD/08-architecture-decisions.md`（新 AD）
- 修改：`SDD/14-version-history.md`（V2.76 行）
- 修改：`SDD/README.md`（Quick Facts 的 SDD Version）
- 修改：`website/index.html`、`website/zh/index.html`（`<strong>V2.75</strong>`）
- 修改：`website/sdd.html`、`website/zh/sdd.html`（`SDD V2.75 · 日期`、`AD-001 ... AD-025`、AD 标签列表）
- 重生成：`website/sdd/*.html`（`python3 website/build_sdd.py`）

**为什么必须做**：`docs-sync` 是本仓的 info 级约束，而这个功能**新增了一条无鉴权写入路径**——
它属于架构决策级别，与前几个功能（AD-020 一键 CQ、AD-021 诊断包、AD-022 升级通道、
AD-023 遥测、AD-024 令牌传输、AD-025 频谱档位）同级。
另外 `tests/test_sdd_docs_consistency.py` 会**强制**：只要 `SDD/08` 里多了一个 AD，
两张手写落地页（`website/sdd.html`、`website/zh/sdd.html`）就必须列出它；
只要 `14-version-history.md` 的顶版本变了，四张页面的版本串与 `SDD/README.md` 的
Quick Facts 就必须跟着变。**漏一处就是套件红。**

- [ ] **步骤 1：跑一次基线，确认这条链现在是绿的**

```bash
$PY -m unittest tests.test_sdd_docs_consistency -v 2>&1 | tail -5
```
预期：`OK`（当前 SDD V2.75 / AD 到 AD-025，四处一致）

- [ ] **步骤 2：在 `SDD/08-architecture-decisions.md` 末尾（AD-025 之后）追加**

先读一眼 AD-025 的小节，**照抄它的表格结构**（`| Attribute | Value |` 那套：
Type / Status / Decision / Problem / Rationale / Consequences），然后写：

```markdown
## AD-026: 初始化热点与免口令引导窗口（按网络路径开门，不按标志位）

| Attribute | Value |
| ----------- | ------- |
| Type | Architectural / Security |
| Status | Implemented |
| Decision | 盒子在**没有可用上行网络**时由 `mrrc-setup-ap.service` 开一个**开放热点**；`/setup` 向导在该热点存续期间对**来自热点网段的请求**免口令开放（设口令 + 选 Wi-Fi），成功后切 STA 并关热点。所有 nmcli 交互集中在 `net_wifi.py`（纯标准库、零应用 import、runner 可注入）。 |

**Problem**: 首次配置需要**同时**具备两样互为前提的东西：要配 Wi-Fi 得先能操作盒子，
要操作盒子得先有网（SSH）或有显示器键盘。买回来只有一台手机的人**无法开始**——
而"只发一个热点"并不解决问题：连上之后到达的仍是现有界面，它要一个操作者并不知道的口令
（首启自动生成），等于把鸡生蛋换了个位置。

**Rationale**:
1. **判据是网络状态，不是"首次开机"。** `起热点 ⟺ 无网线链路 且 无可连上的已保存 Wi-Fi`。
   配好的盒子因此永不再开热点，且**不存在**"是否已配置"的标志位——也就没有
   "标志写失败导致永久敞开"这类故障模式。
2. **闸门是网络路径，且两半都要成立**：`state.json` 的 `mode == hotspot` **且心跳新鲜（≤45 s）**
   **且**客户端 IP 在该热点网段内。网段那一半挡住"某个局域网客户端恰好是 10.42.0.x"
   （NM shared 默认网段与家用网段撞车是完全可能的）；心跳那一半挡住"守护进程死了但文件还在"。
   热点一关，那个网段**不复存在**，免口令分支自然失效。
3. **页面并进现有 FastAPI 应用**（否决独立 setup 服务器）：独立进程暴露面更小，但需要
   **第二套配置写入路径**，两个 UI 各写 env 文件会互相覆盖。设口令因此**复用既有通道**
   `first_run.update_env_file(_config_file_path(), …)`，与 `POST /api/setup` 同函数同文件；
   免口令窗口可写的键集被钉成**恰好两个**（`MRRC_WEB_PASSWORD`、`MRRC_AUTO_PASSWORD`），
   `MRRC_CONFIG_FILE`（D-11 的载荷）与 `MRRC_ALLOW_UNVERIFIED_TX`（AD-019 / NFR-067）
   在任何可写集合里都不出现。
4. **单射频 ⇒ 切换而非并发**（MT7663S 的 Wi-Fi 与蓝牙同芯片、走 SDIO）。因此切网期间
   守护进程**让位**：服务端在 `wizard.json` 里声明占用，守护进程不碰射频、并按让位时长
   **补偿**窗口。回答**先发出去**再切（手机在切换瞬间就不在盒子上了），失败则**把热点开回来**
   并把 nmcli 的原因留在 `wizard.json` 里给页面显示——**不许静默返回**。
5. **两份文件、两个唯一写入者**：守护进程（root）只写 `state.json`，服务端（`mrrc`）只写
   `wizard.json`；目录 `0775` + group `mrrc`，文件显式 `0644`（root 的 umask 会留下 0600，
   那样服务端读不到 ⇒ 闸门永久关闭且不报错）。因此**不需要锁**。
6. **Wi-Fi 密码永不离开 nmcli 的 argv**：`WizardClaim` **没有** password 字段（"不落盘"是
   schema 的性质，而不是每个调用者要记住的承诺），nmcli 的错误文本在存下来之前过
   `scrub()`。这是 `support-bundle-privacy`（NFR-068）在一个新写入面上的延伸。

**Consequences**:
- 新增 4 条路径（`/setup`、`GET|POST /api/setup/wizard*`）在中间件里被**按相等**放行到处理函数，
  由 `_setup_access()` 统一裁决并**每次免口令进入都写 WARNING 审计**（谁、什么时候、哪个 IP）。
  中间件的 listen-only 403 对这些路径必然被跳过，所以**拒绝 listen 令牌这件事搬进了处理函数**——
  漏掉它就是权限提升。
- 三条围栏压住开放窗口：① 只在无其他上行时存在；② 30 分钟（可配）无人完成引导即自动关闭，
  且**本次开机不再重开**（闩是进程内的；持久化它等于把盒子变砖）；③ 每次进入都留日志。
  恢复路径是**断电重启**。
- **残留风险（诚实记录）**：热点开放期间，同一网段内的人可以先下手设口令接管盒子。
  Wi-Fi 半径十几米，公寓楼里邻居可能在里面。围栏把窗口压到"你主动插电后、且附近没有网络"
  的那几十分钟，但**不能消除**。缓解：地址同时打在 HDMI 控制台上（有显示器的人不必用热点）。
- **未验证边界**：MT7663S 这颗 SDIO 变体能否真起 AP、切换时手机端的重连体验、
  `10.42.0.0/24` 与既有网络的冲突。设计上**不依赖它们成立**：起不来时 HDMI + `nmtui` 永远可用。
- 镜像装机清单加 `dnsmasq`（NM 的 `ipv4.method=shared` 靠它发 DHCP/DNS；缺它的症状是
  **热点出现、手机连上、拿不到地址、任何地方都不报错**）。单元**不得**排在
  `network-online.target` 之后——它存在的理由就是没有网。
```

- [ ] **步骤 3：在 `SDD/14-version-history.md` 顶部表格插入 V2.76 行**

**照抄既有行的形状**（`| SDD V2.75 | 2026-10-05 | pi | **…** … |`，作者列写 `pi`，
日期写今天）。内容要点（一段话即可，但**必须**含这几条，因为守卫测试会读它）：

- 标题句：**W103D 初始化热点 + 最小向导（AD-026）——"刷完盘、只拿手机就能配完"**。
- 判据是**网络状态**而非首次开机；闸门是**活心跳 + 热点网段**，热点一关分支自然失效。
- 设口令**复用既有写入通道**（`first_run.update_env_file`），因此**不等** D-8 配置层统一；
  免口令窗口可写键集**恰好两个**。
- 单射频 ⇒ **切换**不并发；回答先发、失败**必回热点并带原因**；守护进程按让位时长**补偿**窗口。
- **两份文件两个写入者**（root 守护 / `mrrc` 服务端），目录 0775 + 文件 0644，**不需要锁**。
- **PSK 永不落盘**：`WizardClaim` 无 password 字段 + `scrub()` 兜底（NFR-068 的延伸）。
- 镜像加 `dnsmasq`；单元**不等** `network-online.target`。
- 三条围栏与**残留风险**（邻居抢先设口令）+ HDMI 缓解；恢复路径是断电重启。
- 测试 `<N>` → `<M>`（+新增：`test_net_wifi` / `test_setup_ap` / `test_server_setup_wizard`，
  以及 `test_box_profiles` 的两个新类）；**用任务 10 步骤 8 的实跑数字**。
- **边界**：真机 Wi-Fi 行为（AP 模式、重连体验、网段冲突）**未验证**，需按
  `docs/W103D_GUIDE.md` §9.6 与 `packaging/box/verify.sh` 第 11 项在盒子上验收。
- 顺手更正指南 §6 的一处错误："首启要联网"——首启四件事全在本机完成，不下载任何东西。

> 表格里出现的 `AD-026` 与 `net_wifi` 这两个字符串是**必须**的：
> `test_spectrum_profile_docs.py` 与 `test_sdd_docs_consistency.py` 都用"某字符串必须在文里"
> 这种方式钉住版本条目不是空话。

- [ ] **步骤 4：改 `SDD/README.md` 的 Quick Facts**

`| SDD Version | V2.75 |` → `| SDD Version | V2.76 |`
（同一张表里若有 `Architecture Decisions` / `AD count` 之类的行，一并从 25 改到 26。）

- [ ] **步骤 5：改四张手写页面的版本串与 AD 索引**

```bash
# 两张主页面的版本徽章
perl -pi -e 's{<strong>V2\.75</strong>}{<strong>V2.76</strong>}g' \
  website/index.html website/zh/index.html

# 两张 SDD 落地页：版本 + 日期 + AD 范围
perl -pi -e 's{SDD V2\.75 · 2026-10-07}{SDD V2.76 · '"$(date +%F)"'}g' \
  website/sdd.html website/zh/sdd.html
perl -pi -e 's{AD-001 \.\.\. AD-025}{AD-001 ... AD-026}g' \
  website/sdd.html website/zh/sdd.html
```

然后在两张落地页的 AD 标签列表里，紧跟 AD-025 那一行**各加一行**：

```html
          <span class="tech-tag">AD-026 Setup Hotspot &amp; Passwordless Onboarding</span>
```
（`website/sdd.html`，英文）

```html
          <span class="tech-tag">AD-026 初始化热点与免口令引导</span>
```
（`website/zh/sdd.html`，中文）

- [ ] **步骤 6：重生成 `website/sdd/*.html`**

```bash
python3 website/build_sdd.py
git status --short website/sdd | head
```
预期：打印 `08-architecture-decisions.md → 08-architecture-decisions.html` 等 16 行，
并且 `website/sdd/08-…html`、`14-…html`、`index.html` 有改动。

> `build_sdd.py` 只生成 `website/sdd/*.html`；`website/sdd.html` 与 `website/zh/sdd.html`
> 是**手写**的（守卫测试的 docstring 明写"The hand-written pages are not generated"），
> 所以步骤 5 必须手改，别指望生成器。

- [ ] **步骤 7：跑守卫，确认整条链绿**

```bash
$PY -m unittest tests.test_sdd_docs_consistency tests.test_spectrum_profile_docs -v 2>&1 | tail -8
```
预期：`OK`

排障：
- `test_landing_pages_list_every_decision` 红 ⇒ 步骤 5 的 `<span class="tech-tag">AD-026 …` 少加了一张页面（**两张都要加**）。
- `test_readme_quick_facts_matches_the_version_history` 红 ⇒ `SDD/README.md` 与 `14-version-history.md` 的顶版本不一致（正则抓的是**第一个** `| SDD V… |` 行，所以新行必须插在表格**最上面**）。
- `test_main_pages_show_the_current_sdd_version` 红 ⇒ `website/index.html` / `zh/index.html` 里的 `<strong>V2.76</strong>` 没改到。
- `test_generated_pages_use_the_current_version` 红 ⇒ 忘了跑 `build_sdd.py`。

- [ ] **步骤 8：全套 + Commit**

```bash
$PY -m unittest discover -s tests 2>&1 | tail -4     # 仍只有 test_tls_trust_store 一条红
$PY .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git add SDD/08-architecture-decisions.md SDD/14-version-history.md SDD/README.md \
        website/index.html website/zh/index.html website/sdd.html website/zh/sdd.html \
        website/sdd
git commit -m "docs(sdd): AD-026 初始化热点与免口令引导窗口——按网络路径开门，不按标志位"
```

> **不要**在这个任务里动 `CHANGELOG.md`、`packaging/windows/MRRC-Modern.iss` 或
> `website/downloads/*` 的版本号：`test_spectrum_profile_docs.py::
> test_app_version_bump_is_left_to_the_release_step` 明确写着**应用版本号属于发布步骤**，
> 提前改会让下载卡片指向一个还不存在的安装包（404）。本计划到 AD-026/V2.76 为止。

---

## 任务 12：构建镜像 + 真机验收（**这一步才判得掉验收判据**）

**文件：** 无（构建与真机操作）
**必需子技能：** `w103d-box`（`~/.pi/agent/skills/w103d-box/SKILL.md`）——构建的九个坑与
产物四步验证都在那里，别凭记忆跑。

前 11 个任务全绿**只证明代码是对的**，不证明盒子能用。操作者的验收判据是：
**刷完盘、不插网线、不接键盘，只拿一台手机 —— 看到热点、连上、免口令设口令 + 选 Wi-Fi，
成功后切 STA 并关热点。** 这里面有**四件事在 macOS 上永远测不到**（规格 §10）：
MT7663S 能否起 AP、`dnsmasq` 是否真把地址发下去、切换时手机的重连体验、
以及 `10.42.0.0/24` 与现场网络是否冲突。

- [ ] **步骤 1：本机全套最后一次**

```bash
PY=/Users/cheenle/HAM/hub/mrrc_modern/.venv/bin/python
$PY -m unittest discover -s tests 2>&1 | tail -4
```
预期：`Ran <M> tests` / `FAILED (failures=1, skipped=1)`，唯一 `FAIL:` 是 `test_tls_trust_store`
（worktree 路径造成的既有红，不是本计划的）。

- [ ] **步骤 2：变异验证（**必做**，这是"测试真的在守东西"的唯一证据）**

逐条把实现改回坏行为，跑对应测试，**必须看到红**，然后 `git checkout` 还原。
本仓的既有惯例是每条修复都配一次变异验证（见 SDD V2.69/V2.66 条目里的"十三处变异验证"）。

| # | 改坏的地方 | 必须变红的测试 |
| --- | --- | --- |
| 1 | `has_uplink()` 里删掉 `if entry["connection"] in ap_names: continue` | `test_net_wifi.UplinkTests.test_our_own_hotspot_is_not_an_uplink`、`test_setup_ap.DecisionTests.test_its_own_hotspot_is_not_an_uplink` |
| 2 | `_is_usable_uplink()` 里删掉 `if "site only" in text: return False` | `test_net_wifi.UsableUplinkTests.test_site_only_is_not_an_uplink`、`test_setup_ap.DecisionTests.test_a_cable_with_no_dhcp_still_gets_a_hotspot` |
| 3 | `client_ip()` 改成 `return ipaddress.ip_address(host)`（不解包 mapped） | `test_net_wifi.GateTests.test_open_for_an_ipv4_mapped_client`、`test_server_setup_wizard.GateHelperTests.test_open_for_an_ipv4_mapped_client` |
| 4 | `gate_is_open()` 里删掉 `state_is_live(...)` 那半，只留网段判断 | `test_net_wifi.GateTests.test_closed_when_the_supervisor_stopped_heartbeating`、`test_server_setup_wizard.GateHelperTests.test_a_stale_heartbeat_closes_it` |
| 5 | `_write_json()` 里删掉 `os.chmod(tmp, 0o644)` | `test_net_wifi.StateFileTests.test_the_file_is_world_readable` |
| 6 | `Supervisor.tick()` 里删掉超时后的 `self._timed_out = True` | `test_setup_ap.TimeoutTests.test_a_closed_window_does_not_reopen_in_the_same_boot` |
| 7 | `tick()` 里删掉 `if not self._deadline:` 那个"领养"分支 | `test_setup_ap.TimeoutTests.test_a_supervisor_restarted_mid_window_arms_a_deadline` |
| 8 | `_wizard_tick()` 里删掉 `self._deadline += self._elapsed` | `test_setup_ap.WizardHandoffTests.test_the_window_is_compensated_…` |
| 9 | `_wizard_tick()` 的 FAILED 分支删掉 `claim.nonce not in self._granted` 判断（每次都重置） | `test_setup_ap.WizardHandoffTests.test_the_same_nonce_cannot_buy_a_second_window` |
| 10 | `_setup_access()` 的 token 分支改成 `return _verify_auth(request)`（不再排 listen） | `test_server_setup_wizard.AccessTests.test_a_listen_only_token_is_refused`、`PasswordTests.test_a_listen_only_token_is_refused_with_403` |
| 11 | `api_setup_wizard_wifi_connect()` 里删掉 `_setup_auto_password()` 那个 409 | `WifiConnectEndpointTests.test_it_refuses_to_switch_before_a_password_is_set` |
| 12 | `_perform_wifi_switch()` 里把 `stop_hotspot()` 移到 `connect_wifi()` **之后** | `WifiSwitchTests.test_the_hotspot_is_taken_down_before_the_join` |
| 13 | `_perform_wifi_switch()` 的失败分支删掉 `start_hotspot(...)` | `WifiSwitchTests.test_a_failed_join_puts_the_hotspot_back` |
| 14 | `_wifi_failure_reason()` 里去掉 `net_wifi.scrub(...)` | `WifiSwitchTests.test_the_psk_is_scrubbed_out_of_the_stored_reason` |
| 15 | `finally` 里把 `stop.set()`/`join()` 移到最终 `publish()` **之后** | `WifiSwitchTests.test_the_claim_is_heartbeated_while_the_join_blocks`（最后一拍不是 `ok`） |
| 16 | `setup_page()` 里删掉 `Cache-Control: no-store` | `SetupPageRouteTests.test_it_is_never_cached` |
| 17 | `box-overlay.sh` 的 apt 清单里删掉 `dnsmasq` | `SetupApPackagingTests.test_dnsmasq_is_in_the_apt_install_list` |
| 18 | 单元里加上 `After=network-online.target` | `SetupApPackagingTests.test_the_unit_does_not_wait_for_a_network` |

```bash
# 每一条的做法（以 #1 为例）：
#   改 net_wifi.py → 跑对应测试 → 确认红 → 还原
$PY -m unittest tests.test_net_wifi.UplinkTests -v
git checkout net_wifi.py
```

**18 条全红过一遍**才算这一步做完。有哪条改坏了却是绿的，说明那条测试没在守东西——
要么修测试，要么在计划里记一笔为什么它守不住。

- [ ] **步骤 3：构建镜像**

按 `w103d-box` 技能跑（**不要**凭记忆）：

```bash
packaging/box/build-image.sh --check          # 先查前置：Docker Desktop 必须开着
packaging/box/build-image.sh                  # 首次要下 852 MB 基底，之后走缓存
```

三个国内镜像源开关都有可用默认值；**卡住先看网络，别先怀疑代码**
（判据：`pip install` 连着但零下载 = 源慢；`apt-get update` 卡 4–6 分钟 = 源慢）。
构建跑着的时候**不要编辑 `packaging/box/*.sh`**——bash 增量读取脚本文件，会读坏。

- [ ] **步骤 4：产物四步验证（**别只看文件存在**）**

按 `w103d-box` 技能里那段 `docker run --rm --privileged --platform linux/arm64 …` 跑，
四步都要过（代码身份 / 装机内容 / **D-11 陷阱** / arm64 chroot 真跑 Python 导入）。
**本计划额外加四条**，都在第 ② 步"装机内容"里查：

```bash
# a) dnsmasq 真的装进去了（缺它 = 热点能连上但拿不到地址，且不报错）
chroot /mnt command -v dnsmasq

# b) 单元装好了、enable 了、且没有等 network-online
test -f /mnt/etc/systemd/system/mrrc-setup-ap.service && echo "unit present"
ls /mnt/etc/systemd/system/multi-user.target.wants/ | grep setup-ap
grep -c network-online /mnt/etc/systemd/system/mrrc-setup-ap.service   # 必须是 0

# c) 守护进程与它的模块真的在镜像里（rsync 没排除掉它们）
test -f /mnt/opt/mrrc_modern/linux/setup_ap.py && echo "daemon present"
test -f /mnt/opt/mrrc_modern/net_wifi.py && echo "adapter present"
test -f /mnt/opt/mrrc_modern/static/setup.html && echo "page present"

# d) 单元的 ExecStart 指向 venv python（系统 python 没有依赖）
grep ExecStart /mnt/etc/systemd/system/mrrc-setup-ap.service
```

再补一条**真跑**（arm64 宿主可以原生 chroot，所以这是真执行，不是看文件在不在）：

```bash
chroot /mnt /opt/mrrc_modern/venv/bin/python -c \
  "import net_wifi, sys; sys.path.insert(0,'/opt/mrrc_modern/linux'); \
   import setup_ap; print(net_wifi.DEFAULT_SSID, setup_ap.DEFAULT_TIMEOUT_MIN)"
```
预期：`MRRC-Setup 30`

- [ ] **步骤 5：烧盘、上电、**只拿手机**走一遍验收**

```bash
diskutil list                                                       # 认准容量，别烧错盘
gunzip -c dist/w103d/MRRC-Modern-<ver>-w103d.img.gz | sudo dd of=/dev/rdiskN bs=4m
```

**先 U 盘试跑，eMMC 一字节不动**（技能里的红线）。然后**拔掉网线、不接键盘显示器**，上电。

按操作者的验收判据逐条打勾：

| # | 判据 | 期望 | 不符时查 |
| --- | --- | --- | --- |
| 1 | 上电后 **15–30 秒**内，手机 Wi-Fi 列表出现 **`MRRC-Setup`** | 出现，且标注"无密码/开放" | `journalctl -u mrrc-setup-ap`（要接显示器或用第 3 条的办法）；`rfkill list`；§9.5 的驱动/固件顺序 |
| 2 | 连上热点，手机**拿到地址**（10.42.0.x） | 拿到，网关 10.42.0.1 | `command -v dnsmasq`——**缺它就是"能连上但没地址"** |
| 3 | 打开 `https://10.42.0.1:8888/setup` | 证书警告一次 → 继续 → **看到向导页**，不是 `/login` | 看到 `/login` ⇒ `cat /run/mrrc/setup-ap/state.json` 看 `mode` 与 `heartbeat`；确认是从热点网段访问的 |
| 4 | 第 2 步（Wi-Fi）在设口令前是**灰的**，且有文案解释为什么 | 灰的 + 有解释 | 页面 JS 或 `auto_password` 字段 |
| 5 | 设口令 → 提示成功 → 第 2 步解锁 | 是 | `/api/setup/wizard/password` 的响应；`grep MRRC_WEB_PASSWORD /opt/mrrc_modern/env/mrrc.env` |
| 6 | 点"扫描" → **列出你家的 Wi-Fi**（含信号与 🔒） | 列出 | `nmcli device wifi list` 手动对比 |
| 7 | 选网络、输密码、点"连接并切换" → 页面**先**显示"接下来会发生什么" | 显示，然后手机与盒子断开 | 若立刻报错 ⇒ 看响应体；若页面白屏 ⇒ 回答没来得及发出去，调大 `WIFI_SWITCH_LEAD_S` |
| 8 | 手机连回自家 Wi-Fi，在路由器里找到盒子，`https://它的IP:8888` **用刚设的口令能登录** | 能登录 | `journalctl -u mrrc-setup-ap \| grep joined` 有盒子自己记的地址 |
| 9 | 盒子上的**热点已经关掉** | `nmcli connection show --active` 里没有 `Hotspot`；`cat /run/mrrc/setup-ap/state.json` 的 `mode` 是 `off` 或 `sta` | 还开着 ⇒ 守护进程的 `has_uplink()` 判断（第 1/2 条变异） |
| 10 | **故意输错一次 Wi-Fi 密码**：热点**自动开回来**，手机重连后页面**显示失败原因** | 是（设计 §6 禁止静默返回） | `cat /run/mrrc/setup-ap/wizard.json` 的 `state` 与 `error` |
| 11 | 插上网线，热点**立刻消失**（≤ 一个 poll 周期，默认 5 s） | 消失 | 围栏①；`journalctl -u mrrc-setup-ap \| grep uplink` |
| 12 | 30 分钟无人操作，热点**自己关掉**；**断电重启后重新出现** | 是 / 是 | 围栏②与恢复路径；`MRRC_SETUP_AP_TIMEOUT_MIN=2` 可以把这一条压到 2 分钟做完 |

**做完之后**，用网线或新配的 Wi-Fi 登进盒子，跑一遍真机验收脚本：

```bash
scp packaging/box/verify.sh root@<盒子IP>:/tmp/ && ssh root@<盒子IP> 'bash /tmp/verify.sh'
```
预期：**11 项全过**，第 11 项报 `service active; published mode='off'`（**有网络的盒子
就该是 off**，那是围栏①，不是失败）。

- [ ] **步骤 6：把真机结果记回文档**

**这一步不许跳**。规格 §10 与 `w103d-box` 技能都明写"构建成功 ≠ 盒子能用"，
而 AD-026 的 Consequences 里记着四条**未验证边界**。真机跑完之后：

1. `SDD/14-version-history.md` 的 V2.76 行末尾补上实测结果（热点出现耗时、手机重连体验、
   网段有无冲突、12 条判据的通过情况）——按本仓惯例，**边界与实测结果写在同一条里**。
2. `docs/W103D_GUIDE.md` §9.6 末尾那段"仍未在真机上验证的部分"：**验证过的就删掉**，
   没验证过的留下并写清楚为什么。
3. 若 MT7663S **起不了 AP**（风险 R-1 成真）：不要静默降级。按 §9.6 排查表第一行处理，
   并在指南里把第 4 条改回"⏳ 本机不支持"、把 §9.4（HDMI + `nmtui`）提为推荐路径。
   **同时**在 AD-026 的 Status 上写清楚实际状态。

```bash
git add SDD/14-version-history.md docs/W103D_GUIDE.md website/sdd
git commit -m "docs: W103D 热点引导的真机验收实测——<一句话结论>"
```

---

## 自检（写完计划后按 writing-plans 的要求过一遍）

**1. 规格覆盖度**（逐条对到任务）

| 规格 | 覆盖 |
| --- | --- |
| R1 无网时自动起开放热点；插网线热点消失 | 任务 4（判定 + 围栏①）；真机第 1/11 条 |
| R2 引导页解掉鸡生蛋（热点网段免口令设口令） | 任务 3（闸门）+ 5（接线）+ 6（设口令）+ 8（页面） |
| R3 完整管理页 | **本期不做**（D-7.5 第 3 项）——计划头部与文件结构里都写明了 |
| R4 两个界面同一配置层 | **本期不做**（D-8，第 2 项）；本期靠**复用既有通道**满足"不新增第二个写入者"，任务 6 的 `test_it_writes_through_the_existing_channel` 与 `test_the_wizard_never_touches_the_env_file_directly` 钉住这一点 |
| R5 安全围栏（只在无上行时存在 / 只对热点网段开放 / 每次进入留日志） | 围栏①任务 4；围栏②（超时 + 不重开）任务 4；围栏③（审计日志）任务 5 的 `_setup_access`；"只对热点网段"任务 3 的 `gate_is_open` |
| R6 可诊断（热点起没起、失败原因，`/manage` 与 `journalctl` 可见） | `state.json` / `wizard.json`（任务 3）、HDMI 控制台横幅（任务 4 + 任务 9 的 `journal+console`）、`verify.sh` 第 11 项（任务 9）、失败原因回传页面（任务 7 + 8）。`/manage` 本身是第 3 项，本期由 `GET /api/setup/wizard` 的载荷顶上 |
| §4.1 引导页三件（设口令 / 连 Wi-Fi / **确认电台型号**） | 设口令 = 任务 6，连 Wi-Fi = 任务 7/8。**确认机型本期不做**：操作者给的验收判据是"免口令设口令 + 选 Wi-Fi"，而 D-7.5 的交付表第 1 行也只写这两件。首启的 `first_run.py` 已经**探测**过机型，HDMI 上 `mrrc-radio use` 仍在。页面把当前机型**只读**显示出来（任务 6 的 `radio_model` 字段），不新增长写路径 |
| D-1 页面并进现有服务器 | 任务 5–8：全部是现有 FastAPI 应用的普通路由，**没有**第二个进程 |
| D-2 nmcli + 镜像加 dnsmasq | 任务 2（`device wifi hotspot`）+ 任务 9（apt 清单） |
| D-3 单射频 ⇒ 切换不并发 | 任务 7 的 `stop_hotspot()` → `connect_wifi()` 顺序 + 任务 4 的让位 |
| D-4 唯一新写入路径 = nmcli 适配层，且**绝不碰** env 文件 | `net_wifi.py` 里没有任何 env 文件写入；任务 6 的 `test_the_wizard_never_touches_the_env_file_directly` 与任务 9 的 `test_the_overlay_leaves_the_env_file_alone` 从两侧钉住 |
| D-5 安全字段门禁 | **本期不涉及**：`MRRC_ALLOW_UNVERIFIED_TX` 不在任何可写集合里（任务 5 的 `test_the_dangerous_keys_are_not_in_it`、任务 6 的源码守卫） |
| D-6 「未配置」判据 + 闸门 + 三条围栏 + 恢复路径 + 残留风险 | 任务 3（闸门）、任务 4（判据/围栏①②/恢复路径=进程内闩）、任务 5（围栏③审计）、任务 4+9（HDMI 缓解）、AD-026（残留风险诚实记录） |
| D-7 `MRRC_CONFIG_FILE` 绝不进页面 | 任务 5/6 的键集守卫与源码守卫 |
| D-7.5 引导页不等配置层；⚠️ 空口令会被自动生成覆盖 | 全局：本期**不依赖** D-8。任务 6 的 `_setup_auto_password()` 与任务 7 的 409 正是对那条 ⚠️ 的正面处理——**不假装能有空口令**，而是让操作者在窗口内直接设一个 |
| §6 数据流（含"失败 → 回到热点 + 显示原因，不能静默回去"） | 任务 7 的失败分支 + 任务 8 的页面回显 + `test_a_failed_join_puts_the_hotspot_back` |
| §7 组件表（常驻而非 oneshot 的理由） | 任务 4 的 docstring + 任务 9 的 `test_the_unit_is_resident_not_oneshot` |
| §8 测试（闸门/引导流程负例/安全字段/热点判定） | 任务 1–9 的测试；**"不做真机 Wi-Fi 行为"** ⇒ 全部用假 runner，真机留给任务 12 |
| §8.5 交付顺序第 2 项的验收判据（操作者可判） | 任务 12 步骤 5 的 12 条判据表，逐条对着操作者的原话写 |
| §9 风险 R-1/R-2/R-3/R-4 | R-1 ⇒ 任务 4 的重试 + 任务 12 的排查路径；R-2 ⇒ 三条围栏 + HDMI 缓解（AD-026 里诚实记录）；R-3 ⇒ 向导期间**不重启服务**（任务 6 的 `test_the_wizard_never_restarts_the_service`）；R-4 ⇒ 本期只有 `/setup` 一个入口，`/manage` 是第 3 项 |
| §10 边界（未验证） | 任务 12 步骤 5/6：真机判 + 结果记回文档 |

**2. 占位符扫描**：计划里没有"待定/TODO/后续实现"。两处**看起来像**占位符的，都不是：
- `<N>` / `<M>`（任务 10 步骤 8–10、任务 11 步骤 3）是**实跑派生值**，配了取值的命令，
  并明写"不许沿用旧值、也不许估"。
- 任务 11 步骤 2 的"照抄 AD-025 的表格结构"是**格式对齐指令**，AD-026 的正文已全文给出。

每个代码步骤都给的是**完整可落盘的代码**，不是"补上错误处理"这类描述；
任务 8 的页面代码、任务 4 的守护进程、任务 1–3 的 `net_wifi.py` 都是全文。

**3. 类型一致性**（跨任务的名字逐个核过）
- `NmResult(returncode, stdout, stderr)` + `.ok` / `.detail`：任务 1 定义，任务 2/4/7 使用 ✓
- `ApState(mode, ssid, gateway, network, url, reason, since, heartbeat, deadline)`：
  任务 3 定义，任务 4 的 `_publish` 与任务 6 的载荷都按**这九个字段**用 ✓
  （`remaining_s` 是任务 6 从 `deadline - heartbeat` **派生**的，不是 `ApState` 的字段）
- `WizardClaim(action, ssid, state, error, address, nonce, heartbeat)`：任务 3 定义，
  任务 4 的 `_wizard_tick` 读 `.state/.nonce/.error/.address/.ssid`，任务 7 的 `publish()`
  写同样七个字段 ✓（**没有 password 字段**，任务 3 有测试钉住字段集）
- `MODE_OFF/MODE_HOTSPOT/MODE_STA`、`WIZARD_SWITCHING/WIZARD_OK/WIZARD_FAILED`：
  任务 3 定义，任务 4/6/7/8 一律用常量不用字面量 ✓
- `HEARTBEAT_MAX_AGE_S = 45.0` / `CLAIM_MAX_AGE_S = 600.0`：任务 3 定义，
  任务 4 的 `claim_is_fresh` 与任务 5 的闸门共用同一个 45 s ✓
- `ap_settings()` 返回 `{ssid, ifname, web_port, state_dir}`：任务 3 定义，
  任务 4 的 `settings()` **在其上加** `timeout_s/poll_s/settle_s`（不改既有键），
  任务 5 的 `_setup_ap_state_dir()` 与任务 7 的 `_perform_wifi_switch` 都只读它 ✓
- `ensure_state_dir` / `_write_json` / `_read_json` / `_pick` / `_resolve_dir`：任务 3 ✓
- `_connection_is_ap` / `active_ap_connections` 定义在**任务 1**（"什么是上行"这个问题本身
  就包含"哪个连接是 AP"），任务 2 的 `hotspot_active` / `start_hotspot` / `stop_hotspot`
  在它们之上；任务 1 的排障说明里记了这次边界更正 ✓
- 服务端：`SETUP_GATE_PATHS` / `SETUP_WRITABLE_KEYS` / `MIN_SETUP_PASSWORD_LEN` /
  `MIN_WIFI_PSK_LEN`（任务 5–6）、`WIFI_SWITCH_LEAD_S` / `WIFI_SWITCH_HEARTBEAT_S`（任务 7）、
  `_setup_ap_state_dir` / `_setup_gate_open` / `_setup_access`（任务 5）、
  `_setup_auto_password`（任务 6）、`_wifi_failure_reason` / `_claim_heartbeat` /
  `_perform_wifi_switch`（任务 7）、`setup_page`（任务 8）——每个都**先定义后使用** ✓
- `Supervisor.tick/_raise/_wizard_tick/_publish/_complain/_wait_for_nm/_shutdown/run/stop`
  与 `_int_or/settings/_install_signals/main`：任务 4 内自洽 ✓
- 测试替身：`FakeNmcli`（任务 1–3，**子串表 + 插入顺序**）与 `FakeRadio`（任务 4，
  **有状态**）是两个不同的东西，各自文件里各自定义，**不跨文件 import**
  （`tests/` 没有 `__init__.py`，跨测试模块 import 在两种运行形式下只有一种能work）✓

**3.5 一条贯穿性的教训（任务 9 实跑抓到两次）**：**断言必须能变红**。
本计划在任务 9 里写出过两条**恒真**的测试——① `assertIn("dnsmasq", install)`：一句
`# dnsmasq deliberately omitted` 的注释就能让它绿；② `assertNotIn("mode.*hotspot.*bad", section)`：
把**正则当字面串**去搜，永远搜不到，于是永远通过。两条都在变异验证里当场暴露。
所以任务 12 步骤 2 的 18 条变异验证**不是形式**——它是唯一能证明"守卫在守东西"的手段。
**写下一条断言时先问：什么样的错误代码会让它变红？答不出来就是恒真。**

**4. 与既有守卫的相容性**（都已实跑核对过源码）
- `ExclusionParityTests`：不动三份 `--exclude` 清单 ⇒ 不受影响（新增文件都是根模块或
  `linux/` 下的，自动被带上）。任务 9 有一条测试正面断言这件事。
- `ServerRouteOrderTests`：新路由全部注册在 SPA 兜底**之前** ⇒ 自动通过；任务 7/8 各加一条
  指名道姓的断言，红了才知道是向导的问题。
- `test_static_cache_busting`：`setup.html` 不在 `index.html` 引用里、不在 `sw.js` 的 ASSETS 里
  ⇒ 不受影响（已读源码确认它只比对这两处）。
- `test_undefined_app_module_names`：`net_wifi.py` 成为根模块后进入它的应用模块集合；
  `server.py` 里所有 `net_wifi.xxx` 调用都有 `import net_wifi` 配对 ⇒ 通过。
- `test_spectrum_profile_docs`：**钉住了 tests/README 的总数**，任务 10 步骤 10 同步改它。
- `test_sdd_docs_consistency`：AD 索引与版本串四处联动，任务 11 步骤 7 专门跑它。
- `sdd_context.py check --staged`：每个 commit 前都跑；本计划相关的 block 约束是
  `secrets-hardcoded` 与 `support-bundle-privacy`，两者都有对应测试（任务 6/7）。

---

## 执行交接

计划已完成并保存到 `docs/superpowers/plans/2026-10-08-w103d-setup-hotspot-wizard.md`。
两种执行方式：

**1. 子代理驱动（推荐）** —— 每个任务调度一个新的子代理，任务间进行审查，快速迭代。
适合本计划：12 个任务边界清楚，任务 1–4 是纯新文件（互不干扰），任务 5–8 都改
`server.py`（**必须串行**，且每个任务都要重跑全套）。

**2. 内联执行** —— 在当前会话中用 executing-plans 执行任务，批量执行并设有检查点。
建议的检查点：任务 4 之后（守护进程自洽）、任务 8 之后（服务端 + 页面能在本机伪造闸门跑起来）、
任务 11 之后（文档链全绿）、任务 12（真机）。

**选哪种方式？**

> 无论哪种，**任务 12 的变异验证（步骤 2）与真机验收（步骤 5）都不许跳**。
> 前 11 个任务全绿只证明代码自洽；操作者的验收判据是"只拿一台手机能不能配完"，
> 那四件在 macOS 上永远测不到的事（AP 模式、DHCP、重连体验、网段冲突）只能在盒子上判。
