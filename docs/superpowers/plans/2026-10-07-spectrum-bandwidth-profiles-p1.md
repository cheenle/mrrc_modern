# 频谱带宽档位（P1：服务端 + Web）实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 落实"文档里写定却从未上线"的 v1=851 B 短帧，与帧率分频捆成 `high`/`mid`/`low` 三档，让频谱在**两种口径**（payload 与线上字节）上都逐级真减一半；未声明能力的 socket 逐字节等于今天。

**架构：** 新增纯标准库模块 `spectrum_profile.py`（档位表、分频闸、变体切帧、caps 解析）；`server.py` 只在三处接线——`/WSspectrum` handler 收 caps、按 socket 存档位、扇出时选变体并按**实发字节**计量。短帧靠 `full[:851]` 切片得到，因此**不动 `scope_handler.py` 与任何 backend**。`session_metrics.py` 增加按档位的帧/字节计数，遥测行标成 `payload≈`。前端在 spectrum socket 的 `onopen` 发一条 caps，菜单 Settings 段加 NET 选择器。

**技术栈：** Python 3.13.14（`.venv`）、FastAPI + Starlette + uvicorn 0.52.1、`unittest`（含 `IsolatedAsyncioTestCase`）、原生 `<script>` 全局函数式前端、Cookie 持久化、`websockets 17`（仅验收脚本）。

**设计文档：** `docs/superpowers/specs/2026-10-07-spectrum-bandwidth-profiles-design.md`（下称"规格"；§N / D-N 指其章节与决策号）

---

## 全局约束（每个任务都适用，违反即返工）

1. **基线（2026-10-07 实测）**：`.venv/bin/python -m unittest discover -s tests` ⇒ `Ran 1673 tests in 34.874s` / `OK (skipped=1)`（那 1 个 skip 是 `test_yaesu_fake_radio`）。开工前先复现。全量约 35 s，用 `unittest discover`，不用 `pytest`（`tests/README.md` 是权威）。
2. **永远不要 `git add -A` / `git commit -a`。** 开工时工作区已有 7 个无关脏文件：`atr1000_tuner.json`、`mem_channels.json`（AGENTS 明写"用户运行期文件，勿动"）、`start.sh`、`website/images/promo_poster.jpg`，以及 `website/downloads/latest.json.bak-1.24.5/.1.24.8/.1.25.0/.1.25.1`。每个 commit 步骤都逐字列出要 add 的路径。
3. **回归闸门（规格 §6.1）**：`high` 档逐字节等于今天——1701 B/帧、真频谱 ≈11 fps、`Session metrics` 的 spectrum 仍 ≈151 kbps；**且没发过 caps 的 socket 永远只能拿到 1701 B**。这条优先于任何"顺手多省一点"。
4. **禁止把全帧的版本字节改成 `0x02`。** iOS `FT710Mobile/Sources/Spectrum/SpectrumProcessor.swift:57` 是 `guard version == 0x01 else { return }`——改标会让已装 iOS 客户端**静默丢帧**。见规格 D-2。
5. **两个前端文件不得被格式化工具碰**（`AGENTS.md:98-113`）：`static/ft710_main.js` = **tab 缩进 + 双引号**，`static/ft710_ui.js` = **4 空格 + 单引号**。手写、逐字对齐各自风格，**不要**对它们跑 biome。两文件顶层名是跨文件全局（classic `<script>`）：删/改名会让浏览器侧静默坏掉而 Python 套件全绿。
6. **`/WSspectrum` handler 的源码守卫**（`tests/test_server_ws_protocol.py:334`）按字符串 `@app.websocket("/WSspectrum")` 与 `# ── Audio RX WebSocket` 切开 `server.py`，再断言块内含 `await _scope_producer.start()` 与 `await _scope_producer.stop()`。这四个串一个字不能动，也别在两标记之间新增同类标记。
7. **缓存版本联动**：改了 `static/` 任何资产 ⇒ 同改 `static/index.html` 的 `?v=`、`static/sw.js` 的 `const CACHE = 'mrrc-v46'` 与 precache 条目，并重钉 6 处：`tests/test_server_ws_protocol.py:233,237,238`、`tests/test_ws_token_transport.py:182,184,185`、`tests/test_audio.py:151`、`tests/test_tx_liveness.py:345`。动前端前先跑 `grep -rn "?v=38\|?v=33\|mrrc-v46" static tests`。
8. **不动这些**：`scope_handler.py`、`backends/**`、音频三件套、控制面 `fullState`/`stateUpdate` 语义、`SPECTRUM_BROADCAST_FPS = 30`（它是 tick 源，档位是对它的**分频**）。
9. **SDD 守门**：编辑前 `python3 .agents/skills/sdd-guardian/harness/sdd_context.py brief <文件>`；每次 commit 前 `.venv/bin/python .agents/skills/sdd-guardian/harness/sdd_context.py check --staged` 必须干净。
10. **版本号不许猜**：`CHANGELOG.md` 顶部条目是唯一真相（`release-artifacts.json` 的 `app_version_source`），一次 bump 牵动 `packaging/windows/MRRC-Modern.iss`、官网中英下载卡与指南、`SDD/14-version-history.md` 首行、`SDD/README.md:50`。以任务 7 的 `release_check.py` 为判据。
11. **`Spectrum broadcast active:` 那行日志（`server.py:1556`）不动**——运维取证已把它当证据（SDD V2.68 靠数 `S-meter fallback` 次数定性"频谱从未工作"）。档位信息放进 caps 的新日志。
12. **命名口径统一**（跨任务不许漂移）：档位名 `high` / `mid` / `low` / `listen`；形状 `full` / `wf1`；常量 `FULL_FRAME_BYTES = 1701`、`SHORT_FRAME_BYTES = 851`、`WIRE_VERSION = 0x01`。`listen` 是**服务端内部档位**，客户端不可选。

---

## 文件结构

**新建**

| 路径 | 职责 |
|---|---|
| `spectrum_profile.py` | 纯标准库、零应用依赖：档位表、分频闸、变体切帧、caps 解析。可单测、可热修 |
| `tests/test_spectrum_profile.py` | 该纯模块的单测（不 import `server`） |
| `tests/test_spectrum_profile_server.py` | 接线侧测试：按 socket 档位表、`_spectrum_fanout`、caps、回收、前端契约 |
| `dev_tools/spectrum_profile_probe.py` | 验收脚本：同一 token 分别以三档连 `/WSspectrum`，打印帧长与 kbps，把规格 §6 变成一条命令 |

**修改**

| 路径 | 改什么 |
|---|---|
| `server.py:152` | `LISTEN_SPECTRUM_DIVIDER` 改从档位表取（值仍为 3） |
| `server.py:155-158` | `_spectrum_frame_due` 走档位表；新增 `_spectrum_profiles`、`_profile_for` |
| `server.py:1558-1576` | 扇出改调 `_spectrum_fanout`；按**实发字节**计量 |
| `server.py:1577` 后 | 新增 `_spectrum_fanout()` |
| `server.py:268-276` | `Session metrics:` 抽成纯函数 `_session_metrics_line()`，加 `payload≈` 与分档帧数 |
| `server.py:3766-3800` | `/WSspectrum`：收 caps、记档位、`finally` 回收 |
| `session_metrics.py` | `add_spectrum_profile_frame()` + `snapshot()["spectrum_profiles"]` |
| `static/ft710_main.js` | `onopen` 发 caps；新增 `spectrumProfile()` / `sendSpectrumCaps()` |
| `static/ft710_ui.js:702,1131-1142,1453-1459` | `scopeProfile` 状态、`renderScopeSettings()` 回显、change 绑定 |
| `static/index.html:453-466,566,568` | NET 选择器 + 两个 `?v=` bump |
| `static/sw.js:2-12` | `CACHE` bump + precache 两条 |
| `SDD/01,03,04,05,08,09,10,11,12,14` + `SDD/README.md:50` | 见任务 7 的逐条清单 |
| `AGENTS.md`、`docs/PROJECT_MAP.md` | 新模块行 + 修正不存在的 `tests/test_ws_protocol.py` |
| `tests/README.md` | 套件计数 1673 → 新值 |
| `CHANGELOG.md`、`packaging/windows/MRRC-Modern.iss`、`website/*` | 版本 bump（任务 7） |

---

## 任务 1：`spectrum_profile.py` —— 档位表、分频闸、变体切帧、caps 解析

**文件：**
- 创建 `spectrum_profile.py`
- 创建 `tests/test_spectrum_profile.py`

- [ ] **步骤 1：写失败测试**

创建 `tests/test_spectrum_profile.py`（4 空格缩进、双引号，与仓内测试风格一致）：

```python
"""Spectrum wire-frame profile tiering (SDD AD-025).

A profile is two orthogonal factors — frame *shape* (1701 B full vs 851 B
wf1-only) and *frame-rate divider* — so each named tier roughly halves the
previous one on BOTH the payload and the on-the-wire axis.

Design: docs/superpowers/specs/2026-10-07-spectrum-bandwidth-profiles-design.md
"""

import json
import unittest

import spectrum_profile as sp


def _full_frame(wf1: bytes = b"\x11" * 850, wf2: bytes = b"\x00" * 850) -> bytes:
    return bytes([sp.WIRE_VERSION]) + wf1 + wf2


class ProfileTableTests(unittest.TestCase):
    def test_named_tiers_halve_payload_each_step(self):
        """high -> mid -> low must be 1/4 then 1/8 of high (payload axis)."""
        variants = sp.build_variants(_full_frame())

        def kbps(name: str) -> float:      # at 11 fps, the measured scope rate
            return len(variants[sp.PROFILES[name].shape]) * 11 * 8 / 1000

        hi, mid, low = kbps("high"), kbps("mid"), kbps("low")
        self.assertAlmostEqual(hi, 150.8, delta=0.5)
        self.assertAlmostEqual(mid / hi, 0.25, delta=0.01)
        self.assertAlmostEqual(low / hi, 0.125, delta=0.01)

    def test_shape_and_divider_of_every_tier(self):
        self.assertEqual((sp.PROFILES["high"].shape, sp.PROFILES["high"].divider),
                         (sp.SHAPE_FULL, 1))
        self.assertEqual((sp.PROFILES["mid"].shape, sp.PROFILES["mid"].divider),
                         (sp.SHAPE_WF1, 2))
        self.assertEqual((sp.PROFILES["low"].shape, sp.PROFILES["low"].divider),
                         (sp.SHAPE_WF1, 4))
        self.assertEqual((sp.PROFILES["listen"].shape, sp.PROFILES["listen"].divider),
                         (sp.SHAPE_FULL, 3))

    def test_defaults_and_client_whitelist(self):
        self.assertEqual(sp.DEFAULT_PROFILE, "high")
        self.assertEqual(sp.LISTEN_PROFILE, "listen")
        # "listen" is the server-side default for listener-password sockets
        # (today's /3 behaviour); a client must not be able to name it.
        self.assertEqual(sp.CLIENT_PROFILES, ("high", "mid", "low"))

    def test_frame_lengths(self):
        self.assertEqual(sp.FULL_FRAME_BYTES, 1701)
        self.assertEqual(sp.SHORT_FRAME_BYTES, 851)
        self.assertEqual(sp.WIRE_VERSION, 0x01)

    def test_divider_for_and_shape_for_fall_back_to_high(self):
        """An unknown name degrades to the byte-for-byte-compatible default."""
        self.assertEqual(sp.divider_for("nope"), 1)
        self.assertEqual(sp.shape_for("nope"), sp.SHAPE_FULL)
        self.assertEqual(sp.divider_for("low"), 4)
        self.assertEqual(sp.shape_for("mid"), sp.SHAPE_WF1)


class FrameDueTests(unittest.TestCase):
    def test_divider_one_is_every_tick(self):
        self.assertTrue(all(sp.frame_due(t, 1) for t in range(1, 13)))

    def test_divider_three_matches_the_listener_gate_today(self):
        """Same due-tick pattern server._spectrum_frame_due has had since 1.25.2."""
        self.assertEqual([t for t in range(1, 10) if sp.frame_due(t, 3)], [3, 6, 9])

    def test_divider_four_is_every_fourth_tick(self):
        self.assertEqual([t for t in range(1, 13) if sp.frame_due(t, 4)], [4, 8, 12])

    def test_non_positive_divider_is_treated_as_no_throttle(self):
        self.assertTrue(sp.frame_due(1, 0))
        self.assertTrue(sp.frame_due(1, -3))


class BuildVariantsTests(unittest.TestCase):
    def test_short_frame_is_a_slice_of_the_full_frame(self):
        variants = sp.build_variants(_full_frame())
        self.assertEqual(len(variants[sp.SHAPE_FULL]), 1701)
        self.assertEqual(len(variants[sp.SHAPE_WF1]), 851)
        self.assertEqual(variants[sp.SHAPE_FULL][:851], variants[sp.SHAPE_WF1])
        self.assertEqual(variants[sp.SHAPE_WF1][0], sp.WIRE_VERSION)

    def test_variant_for_picks_the_tier_shape(self):
        variants = sp.build_variants(_full_frame())
        self.assertEqual(len(sp.variant_for(variants, "high")), 1701)
        self.assertEqual(len(sp.variant_for(variants, "mid")), 851)
        self.assertEqual(len(sp.variant_for(variants, "low")), 851)
        self.assertEqual(len(sp.variant_for(variants, "listen")), 1701)

    def test_variant_for_falls_back_to_full_when_shape_unavailable(self):
        """A backend that hands over a non-standard frame must not lose data."""
        odd = b"\x01" + b"\x22" * 300
        variants = sp.build_variants(odd)
        self.assertNotIn(sp.SHAPE_WF1, variants)
        self.assertEqual(sp.variant_for(variants, "low"), odd)

    def test_variant_for_never_returns_none(self):
        self.assertIsNotNone(sp.variant_for({sp.SHAPE_FULL: b"\x01"}, "mid"))


class ParseCapsTests(unittest.TestCase):
    def test_accepts_each_client_tier(self):
        for name in ("high", "mid", "low"):
            with self.subTest(name):
                self.assertEqual(
                    sp.parse_caps(json.dumps({"type": "spectrumCaps", "profile": name})),
                    name)

    def test_rejects_the_internal_listen_tier(self):
        self.assertIsNone(
            sp.parse_caps(json.dumps({"type": "spectrumCaps", "profile": "listen"})))

    def test_rejects_wrong_type_and_unknown_names(self):
        self.assertIsNone(sp.parse_caps(json.dumps({"type": "ping"})))
        self.assertIsNone(sp.parse_caps(json.dumps({"type": "spectrumCaps"})))
        self.assertIsNone(sp.parse_caps('{"type":"spectrumCaps","profile":"ultra"}'))
        self.assertIsNone(sp.parse_caps(json.dumps({"type": "spectrumCaps", "profile": 12})))

    def test_never_raises_on_garbage(self):
        """A malformed text frame is a keepalive today; it must stay harmless."""
        for junk in ("", "not json", "[1,2,3]", "null", '{"type":"spectrumCaps"',
                     b"\x01binary"):
            with self.subTest(repr(junk)):
                self.assertIsNone(sp.parse_caps(junk))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **步骤 2：运行测试确认它失败**

```bash
.venv/bin/python -m unittest tests.test_spectrum_profile -v
```

预期：`ModuleNotFoundError: No module named 'spectrum_profile'`。

- [ ] **步骤 3：实现**

创建 `spectrum_profile.py`：

```python
"""Spectrum wire-frame profile tiering (SDD AD-025).

Pure stdlib, zero app imports, so it is unit-testable without a server and
patchable through the hot-fix channel (same shape as ``session_metrics``).

Two orthogonal factors make one profile:

* ``shape`` — ``full`` is today's ``0x01 + wf1(850) + wf2(850)`` (1701 B);
  ``wf1`` drops the 850 B second waterfall, which is all zeros on both the
  FT-710 and the IC-7300 path and which no client ever draws (Web reads it into
  ``window._lastWf2`` "for potential future use", iOS ignores it, Android parses
  it without rendering it).  That is exactly the ``v1 = 851 B`` frame SDD §9.2.4
  specified and which was never shipped — every server build so far sent the v1
  version byte with the v2 length.
* ``divider`` — how many of the ``SPECTRUM_BROADCAST_FPS`` (30 Hz) broadcast
  ticks actually reach the socket.

The divider is the only factor that helps a *browser* client: uvicorn 0.52.1
negotiates ``permessage-deflate`` with browsers, and measured on this payload
the 850 zero bytes of wf2 cost ~1.4 B/frame after compression (full frame
1701 B -> ~441 B on the wire, 3.9x; wf1 alone 851 B -> ~440 B, 1.9x).  Android
ships OkHttp 4.12, which does not offer the extension (its dex carries the
"Request header not permitted: 'Sec-WebSocket-Extensions'" guard), so on a phone
the shape factor is the whole saving and 1701 B really does leave the radio.
Both factors are therefore needed for "each tier halves the previous one" to
hold on either axis.

Backward compatibility (design §4 / D-3): a socket stays on ``high`` — full
1701 B, divider 1, byte-for-byte today's behaviour — until it declares
capability with a ``{"type":"spectrumCaps","profile":...}`` text frame.  The old
Android build rejects any frame whose length is not 1701
(``SpectrumFrame.kt:14-15``) and would drop short frames silently, so the server
must never volunteer them.  The full frame keeps its ``0x01`` version byte on
purpose: iOS guards ``version == 0x01`` and re-tagging it ``0x02`` would black
out already-installed iOS clients.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

WF1_BYTES = 850
WIRE_VERSION = 0x01
VERSION_BYTES = 1

FULL_FRAME_BYTES = VERSION_BYTES + 2 * WF1_BYTES   # 1701, today's frame
SHORT_FRAME_BYTES = VERSION_BYTES + WF1_BYTES      # 851, SDD §9.2.4 "v1"

SHAPE_FULL = "full"
SHAPE_WF1 = "wf1"


@dataclass(frozen=True)
class Profile:
    """One bandwidth tier: what to send, and how often."""

    name: str
    shape: str
    divider: int


PROFILES: dict[str, Profile] = {
    "high": Profile("high", SHAPE_FULL, 1),
    "mid": Profile("mid", SHAPE_WF1, 2),
    "low": Profile("low", SHAPE_WF1, 4),
    # Server-internal: the listener-password default, i.e. exactly the /3 full
    # frame rate listeners have had since v1.25.2.  Not client-selectable.
    "listen": Profile("listen", SHAPE_FULL, 3),
}

DEFAULT_PROFILE = "high"
LISTEN_PROFILE = "listen"
CLIENT_PROFILES = ("high", "mid", "low")


def divider_for(name: str) -> int:
    """Broadcast-tick divider for a profile name; unknown names get no throttle."""
    return PROFILES.get(name, PROFILES[DEFAULT_PROFILE]).divider


def shape_for(name: str) -> str:
    """Frame shape for a profile name; unknown names get the compatible full frame."""
    return PROFILES.get(name, PROFILES[DEFAULT_PROFILE]).shape


def frame_due(tick: int, divider: int) -> bool:
    """Is ``tick`` one of the ticks that may deliver a frame at this divider?

    Reproduces the pre-profile listener gate exactly (``tick % 3 == 0`` over
    ticks starting at 1 gives [3, 6, 9]) so no existing behaviour shifts.
    """
    if divider <= 1:
        return True
    return tick % divider == 0


def build_variants(full_frame: bytes) -> dict[str, bytes]:
    """Derive every sendable frame shape from one full frame.

    The short frame is a slice of the long one — same version byte, same wf1 —
    so ``scope_handler`` and the backends stay untouched and there is no second
    code path that can drift out of sync with the first.  A frame whose length
    is not the documented 1701 yields only the full variant; the fan-out then
    falls back to it rather than dropping data (see ``variant_for``).
    """
    variants = {SHAPE_FULL: full_frame}
    if len(full_frame) == FULL_FRAME_BYTES:
        variants[SHAPE_WF1] = full_frame[:SHORT_FRAME_BYTES]
    return variants


def variant_for(variants: dict[str, bytes], name: str) -> bytes:
    """The frame to send for ``name``; never ``None``.

    A client that asked for a shape we cannot build still gets data — silently
    dropping frames is precisely the failure mode this feature must not create.
    """
    frame = variants.get(shape_for(name))
    if frame is None:
        frame = variants.get(SHAPE_FULL, b"")
    return frame


def parse_caps(text: str | bytes) -> str | None:
    """Extract a client-selectable profile name from a ``spectrumCaps`` frame.

    Returns ``None`` for anything else — a keepalive, malformed JSON, an unknown
    name, or the server-internal ``listen`` tier.  Never raises: the
    ``/WSspectrum`` receive loop has always discarded its text frames, so a bad
    one must stay as harmless as silence.
    """
    if isinstance(text, (bytes, bytearray)):
        return None
    try:
        msg = json.loads(text)
    except ValueError:
        return None
    if not isinstance(msg, dict) or msg.get("type") != "spectrumCaps":
        return None
    name = msg.get("profile")
    return name if name in CLIENT_PROFILES else None
```

- [ ] **步骤 4：运行测试确认通过**

```bash
.venv/bin/python -m unittest tests.test_spectrum_profile -v
```

预期：`Ran 20 tests ... OK`。

- [ ] **步骤 5：提交**

```bash
git add spectrum_profile.py tests/test_spectrum_profile.py
.venv/bin/python .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git commit -m "feat: spectrum_profile 模块——档位表（形状×分频）、v1=851B 短帧切片、caps 白名单解析"
```

--

## 任务 2：`server.py` 接线——按 socket 的档位表与分频闸

**文件：**
- 修改 `server.py:152`（`LISTEN_SPECTRUM_DIVIDER`）、`server.py:155-158`（`_spectrum_frame_due`）、`server.py:51` 附近（import）
- 创建 `tests/test_spectrum_profile_server.py`

- [ ] **步骤 1：写失败测试**

创建 `tests/test_spectrum_profile_server.py`：

```python
"""Per-socket spectrum profile wiring in server.py (SDD AD-025).

The gate must keep two promises:

1. a socket that never declared capability stays on ``high`` — full 1701 B at
   every broadcast tick, byte-for-byte today's behaviour;
2. a listener-password socket that never declared capability keeps the /3 full
   frames it has had since v1.25.2 (tests/test_listen_only.py:391 pins that).
"""

import unittest
from pathlib import Path
from unittest import mock

import server
import session_metrics
import spectrum_profile as sp


class _FakeWS:
    """Minimal WebSocket double: records frames, never raises."""

    def __init__(self):
        self.frames: list[bytes] = []

    async def send_bytes(self, payload: bytes):
        self.frames.append(payload)


def _full_frame() -> bytes:
    return b"\x01" + b"\x11" * 850 + b"\x00" * 850


class ProfileTableTests(unittest.TestCase):
    def test_undeclared_socket_defaults_to_high(self):
        ws = _FakeWS()
        with mock.patch.object(server, "_spectrum_profiles", {}), \
             mock.patch.object(server, "_listen_spectrum_clients", set()):
            self.assertEqual(server._profile_for(ws), "high")
            self.assertTrue(server._spectrum_frame_due(ws, 7))

    def test_listener_role_default_is_the_listen_tier(self):
        ws = _FakeWS()
        with mock.patch.object(server, "_spectrum_profiles", {}), \
             mock.patch.object(server, "_listen_spectrum_clients", {ws}):
            self.assertEqual(server._profile_for(ws), "listen")
            self.assertEqual(
                [t for t in range(1, 10) if server._spectrum_frame_due(ws, t)],
                [3, 6, 9])

    def test_declared_caps_override_the_role_default(self):
        """A listener who opts into a tier must actually get fewer frames."""
        ws = _FakeWS()
        with mock.patch.object(server, "_spectrum_profiles", {ws: "low"}), \
             mock.patch.object(server, "_listen_spectrum_clients", {ws}):
            self.assertEqual(server._profile_for(ws), "low")
            self.assertEqual(
                [t for t in range(1, 13) if server._spectrum_frame_due(ws, t)],
                [4, 8, 12])

    def test_listen_divider_constant_still_reads_three(self):
        """tests/test_listen_only.py:397 reads this attribute by name."""
        self.assertEqual(server.LISTEN_SPECTRUM_DIVIDER, 3)
        self.assertEqual(server.LISTEN_SPECTRUM_DIVIDER,
                         sp.divider_for(sp.LISTEN_PROFILE))


class _FanoutTestBase(unittest.IsolatedAsyncioTestCase):
    """Shared fixture: real SessionMetrics + fake sockets + patched globals."""

    profiles: dict

    def setUp(self):
        self.metrics = session_metrics.SessionMetrics()
        self.high, self.mid, self.low = _FakeWS(), _FakeWS(), _FakeWS()
        self.clients = {self.high, self.mid, self.low}
        self.profiles = {self.mid: "mid", self.low: "low"}   # high = undeclared
        self._patches = [
            mock.patch.object(server, "metrics", self.metrics),
            mock.patch.object(server, "spectrum_clients", self.clients),
            mock.patch.object(server, "_spectrum_profiles", self.profiles),
            mock.patch.object(server, "_listen_spectrum_clients", set()),
        ]
        for p in self._patches:
            p.start()
        self.variants = sp.build_variants(_full_frame())

    def tearDown(self):
        for p in self._patches:
            p.stop()


class FanoutTests(_FanoutTestBase):
    async def test_dividers_over_twelve_ticks(self):
        for tick in range(1, 13):
            await server._spectrum_fanout(self.variants, tick)
        self.assertEqual(len(self.high.frames), 12)   # /1
        self.assertEqual(len(self.mid.frames), 6)     # /2 -> 2,4,6,8,10,12
        self.assertEqual(len(self.low.frames), 3)     # /4 -> 4,8,12

    async def test_frame_lengths_match_the_declared_shape(self):
        await server._spectrum_fanout(self.variants, 4)
        self.assertEqual({len(f) for f in self.high.frames}, {1701})
        self.assertEqual({len(f) for f in self.mid.frames}, {851})
        self.assertEqual({len(f) for f in self.low.frames}, {851})

    async def test_undeclared_socket_never_gets_a_short_frame(self):
        """The compatibility gate (D-3), stated as bytes on the wire."""
        for tick in range(1, 25):
            await server._spectrum_fanout(self.variants, tick)
        self.assertTrue(self.high.frames)
        self.assertEqual({len(f) for f in self.high.frames}, {1701})

    async def test_short_frame_is_the_prefix_of_the_full_frame(self):
        await server._spectrum_fanout(self.variants, 4)
        self.assertEqual(self.high.frames[0][:851], self.low.frames[0])

    async def test_metrics_count_the_bytes_actually_sent(self):
        for tick in range(1, 13):
            await server._spectrum_fanout(self.variants, tick)
        snap = self.metrics.snapshot()
        self.assertEqual(snap["uplink_bytes_total"]["spectrum"],
                         (12 * 1701) + (6 * 851) + (3 * 851))
        self.assertEqual(snap["spectrum_profiles"]["high"]["frames"], 12)
        self.assertEqual(snap["spectrum_profiles"]["mid"]["frames"], 6)
        self.assertEqual(snap["spectrum_profiles"]["low"]["frames"], 3)
        self.assertEqual(snap["spectrum_profiles"]["low"]["bytes"], 3 * 851)

    async def test_dead_socket_is_reported_for_removal(self):
        class _Dead(_FakeWS):
            async def send_bytes(self, payload):
                raise RuntimeError("gone")

        dead_ws = _Dead()
        self.clients.add(dead_ws)
        dead = await server._spectrum_fanout(self.variants, 1)
        self.assertEqual(dead, {dead_ws})
        self.assertEqual(len(self.high.frames), 1)   # the rest still got served


class FallbackDividerTests(_FanoutTestBase):
    """The S-meter fallback path must obey the same divider (design §5.3).

    Today it is the *expensive* path: no frame-count gate, so it regenerates
    1701 B on every one of the 30 Hz ticks.  A tier that applied only on the
    healthy path would make the bad case cost more than the good one.
    """

    async def test_low_tier_gets_a_quarter_of_the_frames(self):
        for tick in range(1, 31):
            await server._spectrum_fanout(self.variants, tick)
        self.assertEqual(len(self.high.frames), 30)
        self.assertEqual(len(self.low.frames), 7)    # 4,8,...,28


if __name__ == "__main__":
    unittest.main()
```

- [ ] **步骤 2：运行确认失败**

```bash
.venv/bin/python -m unittest tests.test_spectrum_profile_server -v
```

预期：`AttributeError: module 'server' has no attribute '_profile_for'`；`FanoutTests` / `FallbackDividerTests` 因 `_spectrum_fanout` 缺失而错。

> ⚠️ 跨任务依赖：`test_metrics_count_the_bytes_actually_sent` 需要 `SessionMetrics.add_spectrum_profile_frame`（任务 5 步骤 3）与 `snapshot()["spectrum_profiles"]`。若报错正是 `AttributeError: 'SessionMetrics' object has no attribute 'add_spectrum_profile_frame'`，先做任务 5 的步骤 3，再回到本步骤——**不要为了让它变绿而删断言**。

- [ ] **步骤 3：实现**

`server.py` import 区（紧挨 `import session_metrics`）加一行：

```python
import spectrum_profile
```

把 `server.py:149-158` 现有的（**逐字，注意 return 是折成两行的**）

```python
# Listen-role spectrum clients get every Nth frame (30 fps → ~10 fps):
# a phone on a metered link does not need full-rate waterfall (V2.59).
_listen_spectrum_clients: set[WebSocket] = set()
LISTEN_SPECTRUM_DIVIDER = 3


def _spectrum_frame_due(ws: WebSocket, tick: int) -> bool:
    """Throttle gate for the spectrum fan-out (see above)."""
    return (ws not in _listen_spectrum_clients
            or tick % LISTEN_SPECTRUM_DIVIDER == 0)
```

替换为（`_listen_spectrum_clients` 那一行不动；顺手修掉注释里“30 fps → ~10 fps”那个只在回退态成立的说法）：

```python
# Listen-role spectrum clients get every Nth frame — the "listen" tier below.
# The divider is a ratio, so the delivered rate follows the frame source:
# ~3.7 Hz on the measured 11.1 fps real scope, ~10 Hz in the 30 Hz fallback
# state (V2.59 intent: a phone on a metered link does not need full rate).
_listen_spectrum_clients: set[WebSocket] = set()
# Kept as a module attribute because tests/test_listen_only.py:397 reads it.
LISTEN_SPECTRUM_DIVIDER = spectrum_profile.divider_for(spectrum_profile.LISTEN_PROFILE)

# Tier a socket declared, keyed by the WebSocket object itself.  Absent means
# "this socket never sent spectrumCaps" => the role default (high for an
# operator token, listen for a listener token) => byte-for-byte today's stream.
_spectrum_profiles: dict[WebSocket, str] = {}


def _profile_for(ws: WebSocket) -> str:
    """The spectrum profile in force for one socket.

    Explicit caps win over the role default, and stick for the socket's life.
    """
    declared = _spectrum_profiles.get(ws)
    if declared:
        return declared
    if ws in _listen_spectrum_clients:
        return spectrum_profile.LISTEN_PROFILE
    return spectrum_profile.DEFAULT_PROFILE


def _spectrum_frame_due(ws: WebSocket, tick: int) -> bool:
    """Throttle gate for the spectrum fan-out (see above)."""
    return spectrum_profile.frame_due(tick, spectrum_profile.divider_for(_profile_for(ws)))
```

在 `_broadcast_spectrum_loop` 之后、`# ── Audio WebSocket` 那段注释之前新增：

```python
async def _spectrum_fanout(variants: dict[str, bytes], tick: int) -> set[WebSocket]:
    """Send this tick's frame to every spectrum client, honouring its profile.

    One shared per-tick fan-out is what keeps the healthy (real-scope) path and
    the S-meter fallback path on identical bytes-per-second: before profiles the
    fallback regenerated a frame on every tick with no gate at all, so the broken
    case cost more than the working one.

    Returns the sockets that failed to take the frame; the caller drops them.
    """
    dead: set[WebSocket] = set()
    for ws in spectrum_clients:
        profile = _profile_for(ws)
        # The same gate the listener throttle has always used — one source of
        # truth for "may this socket have a frame on this tick".
        if not _spectrum_frame_due(ws, tick):
            continue
        payload = spectrum_profile.variant_for(variants, profile)
        try:
            await ws.send_bytes(payload)
            # Meter what actually left the radio, not the frame we happened to
            # build: a wf1-only tier puts 851 B on the wire, and the telemetry
            # has to say so or the kbps numbers lie about the tier's effect.
            metrics.add_bytes("spectrum", len(payload))
            metrics.add_spectrum_profile_frame(profile, len(payload))
        except Exception:
            dead.add(ws)
    return dead
```

- [ ] **步骤 4：运行确认通过**

```bash
.venv/bin/python -m unittest tests.test_spectrum_profile_server -v
.venv/bin/python -m unittest tests.test_listen_only -v
.venv/bin/python -m unittest tests.test_ic7300_runtime_reliability -v
```

预期：新模块 `Ran 11 tests ... OK`；`test_listen_only`（含 `:391` 的 ÷3 守卫）与 `test_ic7300_runtime_reliability`（含 `SPECTRUM_BROADCAST_FPS == 30` 与 `_new_real_spectrum_frame` 游标语义）全绿。

- [ ] **步骤 5：提交**

```bash
git add server.py tests/test_spectrum_profile_server.py
.venv/bin/python .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git commit -m "feat: 服务端按 socket 的频谱档位表与分频闸——未声明能力的连接逐字节等于今天"
```

---

## 任务 3：`/WSspectrum` handler —— 收 caps、记档位、`finally` 回收

**文件：**
- 修改 `server.py:3766-3800`
- 追加测试到 `tests/test_spectrum_profile_server.py`

- [ ] **步骤 1：写失败测试**

在 `tests/test_spectrum_profile_server.py` 的 `FallbackDividerTests` 之后、`if __name__` 之前追加：

```python
class CapsHandlingTests(unittest.TestCase):
    """The handler's text-frame loop turns caps into a per-socket profile."""

    def _handler_block(self) -> str:
        src = Path("server.py").read_text(encoding="utf-8")
        block = src.split('@app.websocket("/WSspectrum")', 1)[1]
        return block.split("# ── Audio RX WebSocket", 1)[0]

    def test_handler_parses_caps_into_the_profile_table(self):
        block = self._handler_block()
        self.assertIn("spectrum_profile.parse_caps(await ws.receive_text())", block)
        self.assertIn("_spectrum_profiles[ws] = profile", block)

    def test_handler_drops_per_socket_state_on_disconnect(self):
        """Profile entries must go with the socket: three cleanups, not two."""
        block = self._handler_block()
        self.assertIn("spectrum_clients.discard(ws)", block)
        self.assertIn("_listen_spectrum_clients.discard(ws)", block)
        self.assertIn("_spectrum_profiles.pop(ws, None)", block)

    def test_handler_keeps_the_scope_producer_guard_markers(self):
        """tests/test_server_ws_protocol.py:334 slices on these two markers."""
        block = self._handler_block()
        self.assertIn("await _scope_producer.start()", block)
        self.assertIn("await _scope_producer.stop()", block)

    def test_handler_logs_the_negotiated_tier(self):
        """Support triage needs to see which tier a socket ended up on."""
        block = self._handler_block()
        self.assertIn('"Spectrum profile %s', block)

    def test_listen_tier_is_not_client_selectable(self):
        self.assertIsNone(sp.parse_caps('{"type":"spectrumCaps","profile":"listen"}'))
        self.assertEqual(sp.parse_caps('{"type":"spectrumCaps","profile":"mid"}'), "mid")
```

- [ ] **步骤 2：运行确认失败**

```bash
.venv/bin/python -m unittest tests.test_spectrum_profile_server.CapsHandlingTests -v
```

预期：前 4 个 FAIL（`assertIn` 找不到新代码），`test_listen_tier_is_not_client_selectable` PASS。

- [ ] **步骤 3：实现**

把 handler 尾部（当前 `server.py:3782-3800`，**注意它有 `except Exception: pass`、`metrics.close`、以及条件式的 `_scope_producer.stop()`，这三样必须原样保留**）

```python
    metrics.open(_role_for_token(token), "spectrum", token)
    logger.info("Spectrum client connected (%d total)", len(spectrum_clients))
    if _scope_producer is not None:
        await _scope_producer.start()
    try:
        while True:
            # Keep connection alive, actual data sent by broadcast loop
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        spectrum_clients.discard(ws)
        _listen_spectrum_clients.discard(ws)
        metrics.close(_role_for_token(token), "spectrum", token)
        logger.info("Spectrum client disconnected (%d remain)", len(spectrum_clients))
        if not spectrum_clients and _scope_producer is not None:
            await _scope_producer.stop()
```

改为（只改 `while True:` 循环体与 `finally` 里的清理；`await _scope_producer.start()` / `.stop()` 两行**逐字保留**——全局约束 6）：

```python
    metrics.open(_role_for_token(token), "spectrum", token)
    logger.info("Spectrum client connected (%d total)", len(spectrum_clients))
    if _scope_producer is not None:
        await _scope_producer.start()
    try:
        while True:
            # Keep connection alive, actual data sent by broadcast loop.
            # Text frames used to be discarded; they now carry this socket's
            # bandwidth tier (design §5.2).  Anything else — a ping, malformed
            # JSON, an unknown tier name — stays as harmless as silence.
            profile = spectrum_profile.parse_caps(await ws.receive_text())
            if profile and _spectrum_profiles.get(ws) != profile:
                _spectrum_profiles[ws] = profile
                tier = spectrum_profile.PROFILES[profile]
                frame_len = (spectrum_profile.SHORT_FRAME_BYTES
                             if tier.shape == spectrum_profile.SHAPE_WF1
                             else spectrum_profile.FULL_FRAME_BYTES)
                logger.info(
                    "Spectrum profile %s for %s socket: %s frame (%d B), divider %d",
                    profile,
                    "listener" if ws in _listen_spectrum_clients else "operator",
                    tier.shape, frame_len, tier.divider,
                )
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        spectrum_clients.discard(ws)
        _listen_spectrum_clients.discard(ws)
        # Per-socket tier state goes with the socket — three cleanups, not two.
        _spectrum_profiles.pop(ws, None)
        metrics.close(_role_for_token(token), "spectrum", token)
        logger.info("Spectrum client disconnected (%d remain)", len(spectrum_clients))
        if not spectrum_clients and _scope_producer is not None:
            await _scope_producer.stop()
```

- [ ] **步骤 4：运行确认通过**

```bash
.venv/bin/python -m unittest tests.test_spectrum_profile_server -v
.venv/bin/python -m unittest tests.test_server_ws_protocol -v
```

预期：全绿；`test_server_ws_protocol` 里那个按标记切源码块的守卫用例必须仍 PASS。

- [ ] **步骤 5：提交**

```bash
git add server.py tests/test_spectrum_profile_server.py
.venv/bin/python .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git commit -m "feat: /WSspectrum 收 spectrumCaps 声明档位——按 socket 存档、断开时回收、解析失败仍当保活"
```

---

## 任务 4：扇出改走 `_spectrum_fanout`（两条路径同一套字节）

**文件：**
- 修改 `server.py:1514-1576`（`_broadcast_spectrum_loop` 的 docstring 与扇出段）
- 追加测试到 `tests/test_spectrum_profile_server.py`

- [ ] **步骤 1：写失败测试**

在 `tests/test_spectrum_profile_server.py` 的 `CapsHandlingTests` 之后追加：

```python
class BroadcastLoopSourceTests(unittest.TestCase):
    """The loop must hand its frame to the shared fan-out, not send inline."""

    def _loop_block(self) -> str:
        src = Path("server.py").read_text(encoding="utf-8")
        block = src.split("async def _broadcast_spectrum_loop()", 1)[1]
        return block.split("async def _spectrum_fanout(", 1)[0]

    def test_loop_uses_the_shared_fanout(self):
        block = self._loop_block()
        self.assertIn("spectrum_profile.build_variants(binary)", block)
        self.assertIn("await _spectrum_fanout(variants, _broadcast_tick)", block)

    def test_no_inline_send_survives_in_the_loop(self):
        """An inline send is exactly how the two paths drifted apart before."""
        block = self._loop_block()
        self.assertNotIn("await ws.send_bytes(binary)", block)
        self.assertNotIn('metrics.add_bytes("spectrum", len(binary))', block)
        self.assertNotIn("_spectrum_frame_due(ws, _broadcast_tick)", block)

    def test_docstring_no_longer_claims_five_fps(self):
        """It has said 'Runs at 5 fps' since before SPECTRUM_BROADCAST_FPS=30."""
        block = self._loop_block()[:1200]
        self.assertNotIn("Runs at 5 fps", block)
        self.assertIn("SPECTRUM_BROADCAST_FPS", block)

    def test_broadcast_fps_is_still_the_tick_source(self):
        """A profile divides the tick rate; it never changes it."""
        self.assertEqual(server.SPECTRUM_BROADCAST_FPS, 30)
```

- [ ] **步骤 2：运行确认失败**

```bash
.venv/bin/python -m unittest tests.test_spectrum_profile_server.BroadcastLoopSourceTests -v
```

预期：前 3 个 FAIL，`test_broadcast_fps_is_still_the_tick_source` PASS。

- [ ] **步骤 3：实现**

把 `_broadcast_spectrum_loop` 的整段 docstring（当前 `server.py:1515-1526`）

```python
    """Periodically send spectrum data to all spectrum WebSocket clients.

    Runs at 5 fps (200ms interval) — a bandwidth/latency tradeoff for the
    1701-byte frames over WAN links.
    Sends binary frames: 1-byte version + 850 bytes wf1 + 850 bytes wf2.

    When scope_pipe is not connected (no FT4222 data), falls back to
    S-meter-based synthetic spectrum from the CAT polling data.

    Idle (0 clients): sleeps 500ms instead of 200ms, cutting ~60% of
    idle wakeups.  Synthetic Gaussian generation is also skipped.
    """
```

换成（三处陈旧说法一起修：“5 fps/200ms”、“帧长恒为 1701”、“idle 对比 200ms”）：

```python
    """Periodically send spectrum data to all spectrum WebSocket clients.

    Ticks at SPECTRUM_BROADCAST_FPS (30 Hz); what each socket actually receives
    is its profile — shape x divider, see spectrum_profile (AD-025).  Frames are
    binary: a 1-byte version (0x01) + 850 bytes wf1, plus 850 bytes wf2 on the
    full shape (1701 B) and absent on the wf1 shape (851 B).

    When scope_pipe is not connected (no FT4222 data), falls back to
    S-meter-based synthetic spectrum from the CAT polling data.  Real-scope
    frames go out only when ScopeHandler._frame_count advances (measured
    ~11.1 fps on the FT-710); the fallback regenerates one on every tick, and
    both paths now pass through the same per-profile gate.

    Idle (0 clients): sleeps 500ms instead of one tick interval, cutting ~60% of
    idle wakeups.  Synthetic Gaussian generation is also skipped.
    """
```

把扇出段（当前 `server.py:1559-1569`，**原文里没有行内注释**）

```python
                dead: set[WebSocket] = set()
                _broadcast_tick += 1
                for ws in spectrum_clients:
                    if not _spectrum_frame_due(ws, _broadcast_tick):
                        continue
                    try:
                        await ws.send_bytes(binary)
                        metrics.add_bytes("spectrum", len(binary))
                    except Exception:
                        dead.add(ws)
                spectrum_clients -= dead
```

替换为：

```python
                # One shared fan-out: the healthy path and the S-meter fallback
                # path now deliver identical bytes per second for a given
                # profile, and both meter what actually left (851 B on a wf1
                # tier, not the 1701 B built above).
                variants = spectrum_profile.build_variants(binary)
                _broadcast_tick += 1
                spectrum_clients -= await _spectrum_fanout(variants, _broadcast_tick)
```

> 缩进注意：这段在 `while True:` → `if spectrum_clients:` → `if binary:` 三层内，保持 **16 个空格**缩进，与上下行一致。

- [ ] **步骤 4：运行确认通过**

```bash
.venv/bin/python -m unittest tests.test_spectrum_profile_server -v
.venv/bin/python -m unittest tests.test_listen_only tests.test_ic7300_runtime_reliability -v
```

预期：全绿（任务 5 尚未做时，`test_metrics_count_the_bytes_actually_sent` 仍会因 `add_spectrum_profile_frame` 缺失而红——见任务 2 步骤 2 的说明）。

- [ ] **步骤 5：提交**

```bash
git add server.py tests/test_spectrum_profile_server.py
.venv/bin/python .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git commit -m "fix: 频谱扇出统一走 _spectrum_fanout——回退路径不再逃过分频，并按实发字节计量"
```

---

## 任务 5：`session_metrics` 按档位计数 + 遥测行标明 `payload≈`

**文件：**
- 修改 `session_metrics.py`（`__init__`、`add_spectrum_profile_frame`、`snapshot`）
- 修改 `server.py:265-280`（`Session metrics:` 日志行）
- 修改 `tests/test_session_metrics.py`（重钉 `:196-206` 键集 + 新用例）
- 追加测试到 `tests/test_spectrum_profile_server.py`

- [ ] **步骤 1：写失败测试**

**1a.** 在 `tests/test_session_metrics.py` 里，把 `test_snapshot_keys_are_stable` 的期望键集从

```python
            {
                "uptime_seconds",
                "window_seconds",
                "listeners",
                "operators",
                "sockets_by_kind",
                "uplink_bytes_total",
            },
```

改为

```python
            {
                "uptime_seconds",
                "window_seconds",
                "listeners",
                "operators",
                "sockets_by_kind",
                "uplink_bytes_total",
                "spectrum_profiles",
            },
```

**1b.** 在同一文件末尾（`if __name__` 之前）追加：

```python
class SpectrumProfileCountersTests(unittest.TestCase):
    """Per-tier counters are how the design's §6 numbers get proven on a real run."""

    def test_frames_and_bytes_accumulate_per_profile(self):
        m = session_metrics.SessionMetrics()
        m.add_spectrum_profile_frame("high", 1701)
        m.add_spectrum_profile_frame("high", 1701)
        m.add_spectrum_profile_frame("low", 851)
        snap = m.snapshot()["spectrum_profiles"]
        self.assertEqual(snap["high"], {"frames": 2, "bytes": 3402})
        self.assertEqual(snap["low"], {"frames": 1, "bytes": 851})
        self.assertNotIn("mid", snap)

    def test_idle_server_reports_no_tiers(self):
        """Don't invent buckets nobody used — the log line prints them all."""
        self.assertEqual(
            session_metrics.SessionMetrics().snapshot()["spectrum_profiles"], {})

    def test_snapshot_returns_copies(self):
        m = session_metrics.SessionMetrics()
        m.add_spectrum_profile_frame("mid", 851)
        m.snapshot()["spectrum_profiles"]["mid"]["frames"] = 999
        self.assertEqual(m.snapshot()["spectrum_profiles"]["mid"]["frames"], 1)

    def test_non_positive_bytes_still_count_the_frame(self):
        m = session_metrics.SessionMetrics()
        m.add_spectrum_profile_frame("high", 0)
        self.assertEqual(m.snapshot()["spectrum_profiles"]["high"],
                         {"frames": 1, "bytes": 0})

    def test_unknown_profile_name_gets_its_own_bucket(self):
        """This module keeps zero app dependencies: names come from the caller."""
        m = session_metrics.SessionMetrics()
        m.add_spectrum_profile_frame("whatever", 10)
        self.assertEqual(m.snapshot()["spectrum_profiles"]["whatever"]["frames"], 1)

    def test_empty_profile_name_is_ignored(self):
        m = session_metrics.SessionMetrics()
        m.add_spectrum_profile_frame("", 10)
        self.assertEqual(m.snapshot()["spectrum_profiles"], {})
```

**1c.** 在 `tests/test_spectrum_profile_server.py` 的 `BroadcastLoopSourceTests` 之后追加：

```python
class MetricsLineTests(unittest.TestCase):
    """The telemetry line must say which axis it reports (design §6.3)."""

    def _line(self, metrics) -> str:
        # Force a 60 s report window so the kbps math is deterministic.
        metrics._last_report = metrics._clock() - 60.0
        return server._session_metrics_line(metrics.take_report())

    def _metered(self):
        m = session_metrics.SessionMetrics()
        m.open("operator", "spectrum", "tok")
        for _ in range(12):
            m.add_bytes("spectrum", 1701)
            m.add_spectrum_profile_frame("high", 1701)
        for _ in range(3):
            m.add_bytes("spectrum", 851)
            m.add_spectrum_profile_frame("low", 851)
        return m

    def test_line_is_labelled_payload_and_lists_tier_frames(self):
        line = self._line(self._metered())
        self.assertIn("Session metrics:", line)
        self.assertIn("payload\u2248", line)
        self.assertIn("high=12", line)
        self.assertIn("low=3", line)

    def test_line_keeps_the_fields_support_triage_reads(self):
        """Prefix and the listeners/operators/kbps fields must survive the edit."""
        line = self._line(self._metered())
        for needle in ("listeners ", "operators ", "uplink spectrum ", "audio_rx ",
                       "kbps", "since last report"):
            self.assertIn(needle, line)

    def test_line_survives_an_idle_server(self):
        line = self._line(session_metrics.SessionMetrics())
        self.assertIn("Session metrics:", line)
        self.assertIn("spectrum frames -", line)
```

- [ ] **步骤 2：运行确认失败**

```bash
.venv/bin/python -m unittest tests.test_session_metrics -v
.venv/bin/python -m unittest tests.test_spectrum_profile_server.MetricsLineTests -v
```

预期：`AttributeError: 'SessionMetrics' object has no attribute 'add_spectrum_profile_frame'`、`test_snapshot_keys_are_stable` FAIL（多了新键——重钉生效的证据）、`module 'server' has no attribute '_session_metrics_line'`。

- [ ] **步骤 3：实现**

`session_metrics.py` 的 `__init__` 里（最后一个计数器初始化之后）加：

```python
        # Spectrum fan-out counters per bandwidth tier, e.g.
        # {"high": {"frames": 12, "bytes": 20412}}.  Names come from the caller
        # (spectrum_profile) so this module keeps zero app dependencies.
        self._spectrum_profiles: dict[str, dict[str, int]] = {}
```

在 `add_bytes` 方法之后加：

```python
    def add_spectrum_profile_frame(self, profile: str, nbytes: int) -> None:
        """Count one spectrum frame delivered under one bandwidth tier.

        This is the only place a tier's effect becomes visible: ``add_bytes``
        reports the total but cannot say whether it came from 12 full frames or
        24 short ones.
        """
        if not profile:
            return
        entry = self._spectrum_profiles.setdefault(profile, {"frames": 0, "bytes": 0})
        entry["frames"] += 1
        if nbytes > 0:
            entry["bytes"] += nbytes
```

在 `snapshot()` 返回字典里，`"uplink_bytes_total": dict(self._uplink_total),` 之后加：

```python
            "spectrum_profiles": {
                name: dict(entry)
                for name, entry in sorted(self._spectrum_profiles.items())
            },
```

在 `server.py` 的 `_session_metrics_report_loop` 之前新增：

```python
def _session_metrics_line(r: dict) -> str:
    """Render one Session-metrics log line (pure, so it is testable).

    The kbps figures are *payload* bytes: they are metered inside Starlette, so
    whatever permessage-deflate does downstream is invisible here (browsers
    negotiate it — measured 1701 B -> ~441 B on this payload — Android's OkHttp
    does not, so a phone really does pay the payload).  Labelling the number
    "payload\u2248" is the point: it was read as a wire figure for months.
    """
    profiles = r.get("spectrum_profiles") or {}
    by_profile = "/".join(
        f"{name}={entry.get('frames', 0)}" for name, entry in profiles.items()
    ) or "-"
    listeners = r["listeners"]
    kbps = r["kbps_since_report"]
    return (
        f"Session metrics: listeners {listeners['sessions']} "
        f"(sockets {listeners['sockets']}, peak "
        f"{int(listeners['peak_sessions_window'])}/{r['window_seconds']:.0f}s) | "
        f"operators {r['operators']['sessions']} | payload\u2248 "
        f"uplink spectrum {kbps['spectrum']:.1f} kbps, "
        f"audio_rx {kbps['audio_rx']:.1f} kbps | "
        f"spectrum frames {by_profile} | {r['elapsed_seconds']:.0f}s since last report"
    )
```

> “uplink” 这个词要留着：已有的运维记录与取证笔记都是拿 `uplink spectrum` 这个串去 grep 日志的（实测值 `uplink spectrum 150.8 kbps` 就是这么读出来的），只把口径标注 `payload≈` 加在它前面。

并把 `_session_metrics_report_loop` 里那整个 `logger.info(...)` 调用（当前 `server.py:269-277`，**原文写的是 `uplink spectrum`，且末行没有尾逗号**）

```python
        logger.info(
            "Session metrics: listeners %d (sockets %d, peak %d/%.0fs) | "
            "operators %d | uplink spectrum %.1f kbps, audio_rx %.1f kbps | "
            "%.0fs since last report",
            r["listeners"]["sessions"], r["listeners"]["sockets"],
            r["listeners"]["peak_sessions_window"], r["window_seconds"],
            r["operators"]["sessions"],
            r["kbps_since_report"]["spectrum"], r["kbps_since_report"]["audio_rx"],
            r["elapsed_seconds"])
```

替换为：

```python
        logger.info("%s", _session_metrics_line(r))
```

（循环里的 `r = metrics.take_report()`、异常处理、sleep 均不动。`Spectrum broadcast active:` 那行日志不在本任务范围内——全局约束 11。）

- [ ] **步骤 4：运行确认通过**

```bash
.venv/bin/python -m unittest tests.test_session_metrics -v
.venv/bin/python -m unittest tests.test_spectrum_profile_server -v
.venv/bin/python -m unittest discover -s tests
```

预期：`test_session_metrics` 全绿（含重钉键集）、`test_spectrum_profile_server` 全绿（任务 2 那个跨任务依赖至此解除）、**全量套件 `OK (skipped=1)`**，计数为 `1673 + 新增`（任务 1-5 合计 **+43 ⇒ `Ran 1716 tests`**；以实际输出为准，任务 7 把这个数写进 `tests/README.md`）。**如果全量有任何失败，先修完再进任务 6。**

- [ ] **步骤 5：提交**

```bash
git add session_metrics.py server.py tests/test_session_metrics.py tests/test_spectrum_profile_server.py
.venv/bin/python .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git commit -m "feat: 遥测按档位计帧计字节，Session metrics 行标明 payload≈ 口径"
```

--

## 任务 6：Web 客户端——声明 caps、Settings 里选档、即时生效

**文件：**
- 修改 `static/ft710_main.js`（**tab + 双引号**）
- 修改 `static/ft710_ui.js`（**4 空格 + 单引号**）
- 修改 `static/index.html`（NET 选择器 + 两个 `?v=`）
- 修改 `static/sw.js`（`CACHE` + precache）
- 修改 4 个测试文件里的 6 处缓存重钉（全局约束 7）
- 创建 `dev_tools/spectrum_profile_probe.py`
- 追加测试到 `tests/test_spectrum_profile_server.py`

- [ ] **步骤 1：写失败测试**

在 `tests/test_spectrum_profile_server.py` 的 `MetricsLineTests` 之后追加：

```python
class WebClientCapsTests(unittest.TestCase):
    """Frontend contract, source-asserted like the repo's other UI guards."""

    def test_main_js_sends_caps_on_open(self):
        src = Path("static/ft710_main.js").read_text(encoding="utf-8")
        self.assertIn("function sendSpectrumCaps()", src)
        self.assertIn('type: "spectrumCaps"', src)
        # onopen must declare the tier before frames start arriving, and the
        # existing self-heal call has to survive the rewrite.
        block = src.split("wsSpectrum.onopen = () => {", 1)[1].split("};", 1)[0]
        self.assertIn("sendSpectrumCaps();", block)
        self.assertIn('subchannelConnected("spectrum");', block)

    def test_main_js_reads_the_stored_tier_with_a_safe_default(self):
        src = Path("static/ft710_main.js").read_text(encoding="utf-8")
        self.assertIn('getStored("scopeProfile", "high")', src)
        # ft710_ui.js may not be loaded yet (classic scripts, shared globals).
        self.assertIn('typeof getStored === "function"', src)

    def test_ui_js_persists_and_pushes_a_live_change(self):
        src = Path("static/ft710_ui.js").read_text(encoding="utf-8")
        self.assertIn("let scopeProfile = getStored('scopeProfile', 'high');", src)
        self.assertIn("setStored('scopeProfile', scopeProfile);", src)
        # Live switch: no socket reopen — the server re-gates on the next tick.
        self.assertIn("sendSpectrumCaps();", src)
        self.assertIn("scope-profile-select", src)

    def test_index_html_offers_exactly_the_three_client_tiers(self):
        html = Path("static/index.html").read_text(encoding="utf-8")
        self.assertIn('id="scope-profile-select"', html)
        for value in ("high", "mid", "low"):
            self.assertIn(f'value="{value}"', html)
        # The server-internal "listen" tier is not a user choice.
        self.assertNotIn('value="listen"', html)

    def test_asset_versions_were_bumped(self):
        html = Path("static/index.html").read_text(encoding="utf-8")
        self.assertIn("ft710_main.js?v=39", html)
        self.assertIn("ft710_ui.js?v=34", html)
        sw = Path("static/sw.js").read_text(encoding="utf-8")
        self.assertIn("const CACHE = 'mrrc-v47';", sw)
        self.assertIn("'/ft710_main.js?v=39'", sw)
        self.assertIn("'/ft710_ui.js?v=34'", sw)

    def test_no_stale_cache_pins_anywhere(self):
        """A missed pin is how a user keeps running yesterday's bundle."""
        for path in ("static/index.html", "static/sw.js"):
            src = Path(path).read_text(encoding="utf-8")
            for stale in ("ft710_main.js?v=38", "ft710_ui.js?v=33", "mrrc-v46"):
                self.assertNotIn(stale, src, f"{path} still pins {stale}")

    def test_listen_page_is_untouched_this_phase(self):
        """P1 leaves the listen page on the server-side 'listen' tier (D-4)."""
        src = Path("static/listen.js").read_text(encoding="utf-8")
        self.assertNotIn("spectrumCaps", src)

    def test_probe_tool_exists_and_declares_caps(self):
        """The acceptance numbers in design §6 must be reproducible by command."""
        src = Path("dev_tools/spectrum_profile_probe.py").read_text(encoding="utf-8")
        self.assertIn("spectrumCaps", src)
        self.assertIn("/WSspectrum", src)
```

- [ ] **步骤 2：运行确认失败**

```bash
.venv/bin/python -m unittest tests.test_spectrum_profile_server.WebClientCapsTests -v
```

预期：除 `test_listen_page_is_untouched_this_phase` 外全部 FAIL（最后那个还会因文件不存在而 ERROR）。

- [ ] **步骤 3：实现前端**

**3a. `static/ft710_main.js`**（tab 缩进、双引号——手写，勿跑格式化工具）。在 `function connectSpectrum()` 定义之前插入：

```js
// ── Spectrum bandwidth tier (SDD AD-025) ──────────────────
// The server streams the byte-for-byte-legacy 1701 B / full-rate frames until a
// socket declares what it can take, so this runs on every (re)connect: the
// subchannel self-heal rebuilds the socket and the tier would otherwise
// silently revert to high.
function spectrumProfile() {
	return (typeof getStored === "function")
		? getStored("scopeProfile", "high")
		: "high";
}

function sendSpectrumCaps() {
	if (!wsSpectrum || wsSpectrum.readyState !== WebSocket.OPEN)
		return;
	try {
		wsSpectrum.send(JSON.stringify({
			type: "spectrumCaps",
			profile: spectrumProfile(),
		}));
	} catch (e) {
		console.debug("spectrum caps send failed:", e);
	}
}

```

并把已有的（**它已经是多行块了，只需插入一行；tab 缩进**）

```js
	wsSpectrum.onopen = () => {
		subchannelConnected("spectrum");
	};
```

改为

```js
	wsSpectrum.onopen = () => {
		sendSpectrumCaps();
		subchannelConnected("spectrum");
	};
```

> `handleSpectrumBinary()` 不用改：它已经是 `if (data.length < 851) return`，并在 `length >= 1701` 时才取 wf2（`ft710_main.js:174,188`），851 B 帧走的就是今天已经跑着的 wf1 渲染分支。

**3b. `static/ft710_ui.js`**（4 空格、单引号）。在 `let scopeTheme = getStored('scopeTheme', 'jet');`（第 702 行）之后加：

```js
// Spectrum bandwidth tier: sent to the server as spectrumCaps on connect and
// re-sent on change (server-side gating, no socket reopen needed).
let scopeProfile = getStored('scopeProfile', 'high');
```

在 `renderScopeSettings()` 里，`if (themeSelect) themeSelect.value = scopeTheme;` 之后加：

```js
    const profileSelect = document.getElementById('scope-profile-select');
    if (profileSelect) profileSelect.value = scopeProfile;
```

在颜色主题那段监听器（`themeSelect.addEventListener('change', ...)` 闭合的 `});` ）之后加：

```js

    // Spectrum bandwidth tier: shape x frame-rate divider, applied server-side.
    // high = 1701 B every frame (identical to what every old client gets),
    // mid = 851 B every 2nd frame, low = 851 B every 4th.
    const netSelect = document.getElementById('scope-profile-select');
    if (netSelect) {
        netSelect.value = scopeProfile;
        netSelect.addEventListener('change', function() {
            scopeProfile = this.value;
            setStored('scopeProfile', scopeProfile);
            renderScopeSettings();
            // Live switch: the server re-gates this socket from the next tick.
            if (typeof sendSpectrumCaps === 'function') sendSpectrumCaps();
        });
    }
```

> 两个 `const` 分属不同函数（`renderScopeSettings()` 与 UI 初始化那个函数），不冲突；同一作用域里已有 `spanSelect` / `speedSelect` / `themeSelect` 的同款命名，跟着这个习惯写。

**3c. `static/index.html`**：在 `scope-theme-select` 那个 `.menu-scope-row` 的闭合 `</div>`（第 466 行附近）之后插入新行，沿用文件里既有的多行属性风格：

```html
          <div class="menu-scope-row">
            <span class="menu-scope-label">NET</span>
            <select
              id="scope-profile-select"
              class="scope-select"
              style="flex: 1"
            >
              <option value="high" selected>Full</option>
              <option value="mid">Half</option>
              <option value="low">Quarter</option>
            </select>
          </div>
```

并把第 566 行 `ft710_main.js?v=38` → `?v=39`、第 568 行 `ft710_ui.js?v=33` → `?v=34`。

**3d. `static/sw.js`**：`const CACHE = 'mrrc-v46';` → `'mrrc-v47'`；precache 里 `'/ft710_main.js?v=38'` → `?v=39`、`'/ft710_ui.js?v=33'` → `?v=34`。

**3e. 重钉 6 处测试**（全局约束 7 的行号）：`tests/test_server_ws_protocol.py:233,237,238`、`tests/test_ws_token_transport.py:182,184,185`、`tests/test_audio.py:151`、`tests/test_tx_liveness.py:345` 里的 `?v=38`→`?v=39`、`?v=33`→`?v=34`、`mrrc-v46`→`mrrc-v47`。改完跑：

```bash
grep -rn "?v=38\|?v=33\|mrrc-v46" static tests
```

必须**零输出**（有输出就是漏了一处，用户会跑昨天的 bundle）。

**3f. 创建 `dev_tools/spectrum_profile_probe.py`**（把规格 §6 的验收表变成一条命令）：

```python
#!/usr/bin/env python3
"""Measure what one spectrum bandwidth tier actually costs (SDD AD-025).

Connects to /WSspectrum with a real token, optionally declares a profile, and
counts the frames and bytes that arrive.  Run it once per tier to reproduce the
design's acceptance table against a live server instead of arithmetic:

    python3 dev_tools/spectrum_profile_probe.py --profile high --seconds 10
    python3 dev_tools/spectrum_profile_probe.py --profile mid  --seconds 10
    python3 dev_tools/spectrum_profile_probe.py --profile low  --seconds 10
    python3 dev_tools/spectrum_profile_probe.py --no-caps --seconds 10   # legacy

Token: `--token` (or `$MRRC_TOKEN`), else `--password` (or `$MRRC_PASSWORD`) is
exchanged against `POST /api/login`, which returns `{"ok":true,"token":...}`
(server.py:3234).  Sent as `Authorization: Bearer` (server.py:893).  The dev
server's cert is self-signed `CN=localhost`, so loopback URLs skip verification.

--no-deflate reproduces the *Android* wire cost: OkHttp 4.12 does not offer
permessage-deflate, so a phone really pays the payload bytes, while a browser
sees ~441 B per full frame.  Without the flag you are measuring the browser
axis.  Both numbers are in the design; label whichever one you quote.
"""

import argparse
import json
import os
import ssl
import sys
import time
import urllib.request
from urllib.parse import urlparse

try:
    from websockets.sync.client import connect
except ImportError:  # pragma: no cover - dev tool, not part of the suite
    sys.exit("needs the 'websockets' package: .venv/bin/pip install websockets")

DEFAULT_URL = "wss://127.0.0.1:8888/WSspectrum"
DEFAULT_HTTP = "https://127.0.0.1:8888"
FULL_FRAME_BYTES = 1701
SHORT_FRAME_BYTES = 851
LOOPBACK = ("127.0.0.1", "localhost", "::1")


def ssl_context_for(host: str) -> ssl.SSLContext:
    """The dev server presents a self-signed CN=localhost cert (SDD V2.69).

    Only loopback skips verification; anything else gets a real check.  A
    context is always passed explicitly — the repo's AST guard forbids a bare
    urlopen (net_tls, SDD V2.68).
    """
    if host in LOOPBACK:
        return ssl._create_unverified_context()
    return ssl.create_default_context()


def read_token(args) -> str:
    explicit = args.token or os.environ.get("MRRC_TOKEN", "").strip()
    if explicit:
        return explicit
    password = args.password or os.environ.get("MRRC_PASSWORD", "")
    if not password:
        sys.exit("no credentials: pass --token / set MRRC_TOKEN, "
                 "or pass --password / set MRRC_PASSWORD to log in")
    base = args.http_url or DEFAULT_HTTP
    ctx = ssl_context_for(urlparse(base).hostname or "")
    req = urllib.request.Request(
        base.rstrip("/") + "/api/login",
        data=json.dumps({"password": password}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            return str(json.loads(resp.read())["token"])
    except Exception as e:
        sys.exit(f"login failed against {base}: {e}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--http-url", default=DEFAULT_HTTP,
                    help="base URL for the /api/login exchange")
    ap.add_argument("--token")
    ap.add_argument("--password", help="exchanged for a token via /api/login")
    ap.add_argument("--profile", choices=("high", "mid", "low"), default="high")
    ap.add_argument("--no-caps", action="store_true",
                    help="send no spectrumCaps: proves an undeclared socket "
                         "still gets the legacy 1701 B stream")
    ap.add_argument("--no-deflate", action="store_true",
                    help="disable permessage-deflate to measure the Android axis")
    ap.add_argument("--seconds", type=float, default=10.0)
    args = ap.parse_args()

    token = read_token(args)
    kwargs = {"additional_headers": {"Authorization": f"Bearer {token}"},
              "max_size": None,
              "open_timeout": 10}
    if args.url.startswith("wss://"):
        kwargs["ssl"] = ssl_context_for(urlparse(args.url).hostname or "")
    if args.no_deflate:
        kwargs["compression"] = None

    lengths: dict[int, int] = {}
    total = 0
    deadline = time.monotonic() + args.seconds
    with connect(args.url, **kwargs) as ws:
        if not args.no_caps:
            ws.send(json.dumps({"type": "spectrumCaps", "profile": args.profile}))
        while time.monotonic() < deadline:
            try:
                msg = ws.recv(timeout=max(deadline - time.monotonic(), 0.1))
            except TimeoutError:
                break
            if not isinstance(msg, (bytes, bytearray)):
                continue
            lengths[len(msg)] = lengths.get(len(msg), 0) + 1
            total += len(msg)

    label = "no caps (legacy)" if args.no_caps else args.profile
    axis = "payload (no deflate)" if args.no_deflate else "browser wire (deflate on)"
    frames = sum(lengths.values())
    fps = frames / args.seconds
    print(f"profile      : {label}")
    print(f"axis         : {axis}")
    print(f"window       : {args.seconds:.1f}s  url {args.url}")
    print(f"frames       : {frames}  ({fps:.1f} fps)")
    print(f"frame lengths: " + ", ".join(
        f"{n}B x{c}" for n, c in sorted(lengths.items())))
    print(f"bytes        : {total}  ({total * 8 / args.seconds / 1000:.1f} kbps)")

    # The acceptance gates, stated where they cannot be misread.
    ok = True
    if args.no_caps or args.profile == "high":
        ok = set(lengths) <= {FULL_FRAME_BYTES} and frames > 0
    else:
        ok = set(lengths) == {SHORT_FRAME_BYTES} and frames > 0
    print(f"gate         : {'PASS' if ok else 'FAIL'} "
          f"(expected {'1701B only' if args.no_caps or args.profile == 'high' else '851B only'})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **步骤 4：运行确认通过**

```bash
.venv/bin/python -m unittest tests.test_spectrum_profile_server -v
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -c "import ast,pathlib;ast.parse(pathlib.Path('dev_tools/spectrum_profile_probe.py').read_text())" && echo "probe 语法 OK"
```

预期：`WebClientCapsTests` 8 个全绿；全量套件 `OK (skipped=1)`。

- [ ] **步骤 5：提交**

```bash
git add static/ft710_main.js static/ft710_ui.js static/index.html static/sw.js \
        dev_tools/spectrum_profile_probe.py \
        tests/test_spectrum_profile_server.py tests/test_server_ws_protocol.py \
        tests/test_ws_token_transport.py tests/test_audio.py tests/test_tx_liveness.py
.venv/bin/python .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git commit -m "feat: Web 端声明频谱档位——onopen 发 caps、Settings 选 NET、切换即时生效（缓存版本 39/34/v47）"
```

--

## 任务 7：文档同步与版本 bump（规格 §8 的逐条落实）

**文件：**
- 创建 `tests/test_spectrum_profile_docs.py`（把"文档已对齐"变成可执行断言）
- 修改 `SDD/05-non-functional-requirements.md:9`、`SDD/09-architecture-overview.md:47-56`、`SDD/08-architecture-decisions.md`（新 AD-025 + 改 AD-023 范围段）、`SDD/01,03,04,10,11,12`、`SDD/14-version-history.md`、`SDD/README.md:50`
- 修改 `AGENTS.md`、`docs/PROJECT_MAP.md`、`tests/README.md`、`opus_rx.py:64-68` 注释、`docs/IOS_OPUS_INTEGRATION.md:64`
- 修改 `CHANGELOG.md`、`packaging/windows/MRRC-Modern.iss`、`website/index.html`、`website/zh/index.html`

> 全局约束 8 说“不动 `scope_handler.py`”，本任务遵守：**已核实 `scope_handler.py:434-437` 并没有“帧长恒为 1701”之类的陈旧注释**，所以它不在本任务的文件清单里（规格 §8 里那一行据此作废）。真正带着陈旧帧长注释的是客户端：iOS `FT710Mobile/Sources/Spectrum/SpectrumProcessor.swift:49,51`（“Expect 1701 bytes”）与 Android `SpectrumFrame.kt:12`、`SpectrumProcessor.kt:5`、`ConnectionManager.kt:227`、`MainViewModel.kt:209`（后两个还写着“~30fps × 1701B ≈ 51KB/s ≈ 180MB/小时”）——**属于 P2/P3，本阶段一律不碰**（已记在末尾“P1 之后”）。`opus_rx.py` 只改注释，不改任何语句。

- [ ] **步骤 1：写失败测试**

创建 `tests/test_spectrum_profile_docs.py`：

```python
"""Doc-truth guards for the spectrum profile feature (SDD AD-025).

These are the claims that were demonstrably false before this change.  Pinning
them as tests is what stops the next reader from re-deriving the same wrong
number: "~51 KB/s" and "~851 bytes/frame fallback" both survived several
releases because nothing checked them.
"""

import re
import unittest
from pathlib import Path


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


class SpectrumBandwidthClaimsTests(unittest.TestCase):
    def test_nfr003_no_longer_claims_30fps_or_an_851_byte_fallback(self):
        row = [ln for ln in _read("SDD/05-non-functional-requirements.md").splitlines()
               if ln.startswith("| NFR-003 ")][0]
        self.assertNotIn("~30fps", row)
        self.assertNotIn("~851 bytes/frame fallback", row)
        # Both axes must be named: the measured real-scope rate and the tiers.
        self.assertIn("11", row)
        self.assertIn("high", row)
        self.assertIn("851", row)

    def test_frame_format_section_marks_v2_as_never_shipped(self):
        sec = _read("SDD/09-architecture-overview.md")
        sec = sec.split("### 9.2.4 /WSspectrum", 1)[1].split("###", 1)[0]
        self.assertIn("never shipped", sec)
        self.assertIn("spectrumCaps", sec)
        self.assertIn("0x01", sec)
        # Re-tagging the full frame 0x02 would black out installed iOS clients.
        self.assertIn("0x02", sec)

    def test_ad025_records_the_decision(self):
        dec = _read("SDD/08-architecture-decisions.md")
        self.assertIn("## AD-025", dec)
        block = dec.split("## AD-025", 1)[1]
        for needle in ("permessage-deflate", "OkHttp", "spectrumCaps", "1701", "851"):
            self.assertIn(needle, block)

    def test_ad023_scope_paragraph_says_payload(self):
        dec = _read("SDD/08-architecture-decisions.md")
        block = dec.split("## AD-023", 1)[1].split("## AD-024", 1)[0]
        self.assertNotIn("408 kbps", block)

    def test_no_stale_fps_claim_in_current_sdd_pages(self):
        """'~10 Hz' for the listener divider was only true in the fallback state.

        14-version-history.md is excluded on purpose: it is an immutable log, and
        it legitimately quotes the old figure inside a past release's entry.
        """
        for path in sorted(Path("SDD").glob("*.md")):
            if path.name.startswith("14-"):
                continue
            self.assertNotIn("~10 Hz", _read(str(path)), str(path))

    def test_agents_lists_the_new_module_and_the_real_test_filename(self):
        agents = _read("AGENTS.md")
        project_map = _read("docs/PROJECT_MAP.md")
        self.assertIn("spectrum_profile.py", agents)
        for text, name in ((agents, "AGENTS.md"), (project_map, "PROJECT_MAP.md")):
            # Both files point at tests/test_ws_protocol.py, which does not exist;
            # the real file is tests/test_server_ws_protocol.py.
            self.assertIn("test_server_ws_protocol.py", text, name)
            self.assertNotIn("test_ws_protocol.py", text, name)

    def test_phantom_setopusbitrate_is_gone_or_marked(self):
        """It was documented as a runtime command that does not exist."""
        for path in ("opus_rx.py", "docs/IOS_OPUS_INTEGRATION.md"):
            text = _read(path)
            for line in text.splitlines():
                if "setOpusBitrate" in line:
                    self.assertTrue(
                        re.search(r"not implemented|TODO|never shipped|\u4e0d\u5b58\u5728|\u672a\u5b9e\u73b0", line),
                        f"{path}: {line!r} still claims it exists")

    def test_server_loop_docstring_documents_both_shapes(self):
        """It claimed a fixed 1701-byte frame and a 5 fps rate for years."""
        block = _read("server.py").split(
            "async def _broadcast_spectrum_loop()", 1)[1][:1400]
        self.assertNotIn("Runs at 5 fps", block)
        self.assertIn("851", block)
        self.assertIn("SPECTRUM_BROADCAST_FPS", block)


class VersionConsistencyTests(unittest.TestCase):
    def test_changelog_top_entry_is_the_new_feature(self):
        top = [ln for ln in _read("CHANGELOG.md").splitlines()
               if ln.startswith("## [v")][0]
        self.assertIn("v1.26.0", top)

    def test_sdd_version_history_and_readme_agree(self):
        first = [ln for ln in _read("SDD/14-version-history.md").splitlines()
                 if ln.startswith("| SDD V")][0]
        self.assertIn("V2.75", first)
        self.assertIn("V2.75", _read("SDD/README.md"))

    def test_tests_readme_matches_the_actual_count(self):
        """Filled in from the real discover output at the end of task 5/6."""
        text = _read("tests/README.md")
        self.assertNotIn("1673", text)


if __name__ == "__main__":
    unittest.main()
```

> `test_tests_readme_matches_the_actual_count` 的断言要改成**任务 6 步骤 4 里 `discover` 输出的真实数字**（预期 1716 + 本任务新增的 11 个 = 1727）。先写成 `self.assertIn("1727", text)`，跑完全量后用实际值修正——**不得为了绿而把断言改成通配**。

- [ ] **步骤 2：运行确认失败**

```bash
.venv/bin/python -m unittest tests.test_spectrum_profile_docs -v
```

预期：除 `test_phantom_setopusbitrate_is_gone_or_marked`（取决于现有注释文字）外全红。

- [ ] **步骤 3：改文档**

**3a. `SDD/05-non-functional-requirements.md:9`** —— 把 NFR-003 整行换成：

```markdown
| NFR-003 | Spectrum bandwidth | Tiered by `spectrum_profile` (AD-025): `high` = 1701 B/frame at every broadcast tick, `mid` = 851 B every 2nd tick, `low` = 851 B every 4th. Broadcast ticks at 30 Hz but real scope frames go out only when the hardware counter advances — **measured 11.1 fps on the FT-710**, i.e. ~151 kbps payload for `high`, ~38 for `mid`, ~19 for `low`. The S-meter fallback regenerates a frame on *every* tick (~30 fps ⇒ ~408 kbps for `high`) and passes through the same divider. A socket that never sends `spectrumCaps` always gets `high`. Payload axis: browsers see ~441 B/full frame after `permessage-deflate`, Android (OkHttp, no extension) sees the full payload | Medium | `dev_tools/spectrum_profile_probe.py` + `session_metrics` `spectrum_profiles` |
```

**3b. `SDD/09-architecture-overview.md` §9.2.4** —— 把两行格式定义换成：

```markdown
**On the wire there is one version byte, `0x01`, and two lengths.** `full` = `0x01` + 850 B wf1 + 850 B wf2 = 1701 B; `wf1` = `0x01` + 850 B wf1 = 851 B. The 851 B frame is what this section used to call "v1": it was specified and **never shipped** — every server build sent the `0x01` byte with the 1701 B length. The "v2" naming is retired rather than implemented: re-tagging the full frame `0x02` would break installed iOS clients (`SpectrumProcessor.swift:57` guards `version == 0x01`), and no client draws wf2 anyway (it is all zeros on both radio families).

Which length a socket gets, and how often, is its **profile** (`spectrum_profile.py`, AD-025): `high` = full every tick, `mid` = wf1 every 2nd tick, `low` = wf1 every 4th. A socket declares its tier with a text frame `{"type":"spectrumCaps","profile":"mid"}`; **until it does, the server sends `high`, byte-for-byte the pre-AD-025 stream** — the old Android parser rejects any length that is not 1701 (`SpectrumFrame.kt:14-15`). Listener-password sockets default to the internal `listen` tier (full, every 3rd tick), which is the ÷3 behaviour they have had since v1.25.2.
```

保留紧跟其后的分频段，但把 "listener throttled to every Nth frame (~10 Hz)" 改成 "every 3rd broadcast tick — about **3.7 Hz** on the measured 11.1 fps real-scope source, or 10 Hz in the 30 Hz fallback state"。

**3c. `SDD/08-architecture-decisions.md`** —— 在 AD-024 之后新增：

```markdown
## AD-025: 频谱带宽档位——形状 × 分频，且以 caps 协商为闸门

**状态：** 已采纳（v1.26.0）

**背景：** 公网出口带宽与移动流量账单的主项是频谱。实测（打包版日志 `Session metrics`）：单 operator `spectrum ≈ 151 kbps` payload、`audio_rx ≈ 0–52 kbps`。两个事实使"砍 wf2"单独不够：① uvicorn 0.52.1 与浏览器协商 `permessage-deflate`，实测 30 帧×1701 B 的 payload 上线后只剩 13224 B（≈441 B/帧），而其中 850 字节的 wf2 零值压缩后≈1.4 B/帧——**对浏览器砍 wf2 几乎不省钱**；② Android 带的是 OkHttp 4.12，其 dex 里带着 `Request header not permitted: 'Sec-WebSocket-Extensions'` 守卫，**不提供该扩展**，所以手机上 1701 B 真的逐个走出去。

**决策：** 档位 = 两个正交因子（`shape` ∈ {full 1701, wf1 851} × `divider` ∈ {1,2,3,4}），包成具名预设 `high`/`mid`/`low` + 服务端内部的 `listen`。短帧 = `full[:851]` 切片，所以 `scope_handler` 与 backends 一行不改。客户端通过 `/WSspectrum` 上的文本帧 `{"type":"spectrumCaps","profile":…}` 声明能力；**未声明则永远 `high`**（逐字节兼容）。全帧的版本字节保持 `0x01`，**不得改 `0x02`**（iOS `guard version == 0x01`）。回退（S-meter）路径与真频谱路径共用同一个 `_spectrum_fanout`，因此两者每档字节数相同——修掉了"坏消息比好消息贵"（回退态无帧计数闸门，30 Hz × 1701 B）。

**后果：** 正面——payload 轴 high→mid→low = 151→38→19 kbps（×¼、×⅛）；Android 线上轴同比例；浏览器线上轴 ≈39→20→10 kbps。遥测 `session_metrics.spectrum_profiles` 按档计帧计字节，`Session metrics` 行改标 `payload≈` 以免再把 payload 当线上字节读。负面——多了一个按 socket 的状态（断开时必须随 socket 回收，否则泄漏），以及一个前端选择器。音频码率档位**不在本阶段**：`setOpusBitrate` 只在注释与 iOS 集成文档里存在，服务端从未实现。

**证据：** `docs/superpowers/specs/2026-10-07-spectrum-bandwidth-profiles-design.md`；验收工具 `dev_tools/spectrum_profile_probe.py`。
```

同时把 AD-023 范围段里的 "对比频谱 408 kbps" 改为 "对比频谱 `high` 档 151 kbps（payload 口径，真频谱 11.1 fps；408 kbps 只在 S-meter 回退态成立）"。

**3d. `SDD/01 / 03 / 04 / 10 / 11 / 12`** —— 逐处把 "v1=851B wf1, v2=1701B wf1+wf2" 与 "~30fps" 改成与 3b 一致的口径（一行描述 + 指向 AD-025）。用下面这条命令找齐所有待改点，**一处不得漏**：

```bash
grep -rn "v2=1701\|v1=851\|1701B wf1\|~30fps\|30 fps\|1701 bytes" SDD/*.md
```

**3e. `SDD/14-version-history.md`** —— 在表头下新增一行 `| SDD V2.75 | 2026-10-07 | pi | … |`（新行在最上，表是 newest-first）；内容包含：为何两个因子缺一不可（deflate 实测数据）、caps 闸门、回退路径修复、测试数 1673→新值、**边界**（Android/iOS 尚未发 caps ⇒ 它们继续拿 `high`，行为不变；省带宽要等 P2/P3）。同时把 `SDD/README.md:50` 的 `| SDD Version | V2.74 |` 改为 `V2.75`。

**3f. `AGENTS.md`** —— ① 在模块清单里加一行 `spectrum_profile.py`（职责：频谱档位表/分频闸/变体切帧/caps 解析；纯标准库）；② 在 server.py 那一行补 `/WSspectrum` 的档位语义；③ 把 `:104` 的 `tests/test_ws_protocol.py` 改为 `tests/test_server_ws_protocol.py`。

**3g. `docs/PROJECT_MAP.md:39`** —— 同样把 `test_ws_protocol.py` 改成 `test_server_ws_protocol.py`，并在“修改 `static/**` 要连带改什么”那一格里补上“频谱档位选择器 ⇒ 同时动 `ft710_main.js` 与 `ft710_ui.js`”。

**3h. `tests/README.md`** —— 套件计数改成任务 6 步骤 4 的真实输出。

**3i. `opus_rx.py:64-68` 与 `docs/IOS_OPUS_INTEGRATION.md:64`** —— 两处都把不存在的运行时命令写成了已实现。`opus_rx.py` 那段注释的末两行原文是：

```python
# so a remote link stops underrunning (the PCM stutter). Runtime-adjustable via
# the setOpusBitrate control command (48/64/96/128 kbps presets on the client).
```

改为：

```python
# so a remote link stops underrunning (the PCM stutter).  NOT runtime-adjustable
# today: `setOpusBitrate` appears in this comment and in
# docs/IOS_OPUS_INTEGRATION.md, but no server or client implements it — the
# codec's own set_bitrate() is only reachable at construction time.  Bitrate
# tiers wait for that command to exist (AD-025 puts them out of P1 scope).
```

`docs/IOS_OPUS_INTEGRATION.md:64` 的“RX Opus 码率”行同理：把“默认 64kbps(运行时 `setOpusBitrate` 可调 8–128kbps,按 `max_data_bytes` 截帧实现)”改成“默认 64kbps；**`setOpusBitrate` 仅存在于文档，服务端与客户端均未实现**（`opus_rx.py` 内部 `set_bitrate()` 只在构造时生效）；码率档位待后续阶段”。只改文字，不改代码。

**3k. 版本 bump** —— 当前：`CHANGELOG.md` 顶部 = `v1.25.4`，SDD = `V2.74`。本次是新功能 ⇒ `v1.26.0` + `V2.75`。先写 CHANGELOG 新条目（标题句式跟仓内习惯：一句话说清“修了什么真问题”），再跑校验器拿到待改清单：

```bash
.venv/bin/python .agents/skills/dual-platform-release/harness/release_check.py
```

按它的输出把 `packaging/windows/MRRC-Modern.iss`、`website/index.html`、`website/zh/index.html`（以及它点名的其他卡/指南）对齐到 `1.26.0`。**以校验器为判据，不要手工猜还有哪些文件带版本号。**

CHANGELOG 条目必备要素（本仓的风格是拿实测数字说话）：三档定义与实测 kbps（151/38/19 payload）、caps 闸门与“未声明 = 逐字节等于今天”、回退路径修复、`Session metrics` 改标 `payload≈`、测试数变化、**边界**（Android/iOS 本阶段不变）。

- [ ] **步骤 4：运行确认通过**

```bash
.venv/bin/python -m unittest tests.test_spectrum_profile_docs -v
.venv/bin/python .agents/skills/dual-platform-release/harness/release_check.py
.venv/bin/python -m unittest discover -s tests
```

预期：文档守卫全绿；`release_check.py` 零不一致；全量套件 `OK (skipped=1)`，计数 = 任务 6 的数 + 11。

> `.agents/skills/sdd-guardian/harness/constraints.json` 的 `sdd_version` 字段目前写的是 `V2.62`，而 SDD 已到 `V2.74`——这个镜像早就落后了。顺手把它改成 `V2.75`；改完跑 `.venv/bin/python .agents/skills/sdd-guardian/harness/sdd_context.py check --staged` 确认没弄坏 harness（若报错就改回去，并在 commit message 里记下这个遗留项）。

- [ ] **步骤 5：提交**

```bash
git add tests/test_spectrum_profile_docs.py SDD/ AGENTS.md docs/PROJECT_MAP.md \
        docs/IOS_OPUS_INTEGRATION.md tests/README.md opus_rx.py \
        CHANGELOG.md packaging/windows/MRRC-Modern.iss website/index.html website/zh/index.html \
        .agents/skills/sdd-guardian/harness/constraints.json
.venv/bin/python .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git commit -m "docs: 频谱档位落文档——SDD V2.75 / AD-025、NFR-003 与 9.2.4 纠错、v1.26.0 版本链"
```

> 这一步的 `git add` 里没有任何 `atr1000_tuner.json` / `mem_channels.json` / `start.sh` / `website/images/*` / `latest.json.bak-*`。提交前用 `git status --short` 确认它们仍是未暂存。

---

## 任务 8：真机验收（P1 的 DoD）

**文件：** 无代码改动；只跑验收并把结果贴回规格 §6。任何一项不过就回到对应任务，**不得降标准**。

- [ ] **步骤 1：启服务端**

```bash
.venv/bin/python server.py &   # 或仓内惯用的启动方式；监听 127.0.0.1:8888
sleep 6 && tail -5 ~/Library/Application\ Support/MRRC-Modern/logs/server.log
```

- [ ] **步骤 2：拿一个 token**

两种方式任选（探针两种都支持）：

```bash
# a) 已有会话：从浏览器的 mrrc_auth cookie 里拷（DevTools → Application → Cookies）
export MRRC_TOKEN=…

# b) 直接拿密码换：POST /api/login 返回 {"ok":true,"token":…}（server.py:3234）
export MRRC_PASSWORD='…'
```

> 开发服务器用的是自签 `CN=localhost` 证书，所以探针对 loopback URL 跳过证书校验（非 loopback 仍走正常校验）。

- [ ] **步骤 3：三档 + 无 caps 各测一轮（两个轴）**

```bash
for p in high mid low; do
  .venv/bin/python dev_tools/spectrum_profile_probe.py --profile $p --seconds 10 --no-deflate
done
.venv/bin/python dev_tools/spectrum_profile_probe.py --no-caps --seconds 10 --no-deflate
.venv/bin/python dev_tools/spectrum_profile_probe.py --profile low --seconds 10   # 浏览器轴
```

必达：
- `--no-caps` 与 `high`：**只有 1701 B 帧**，且两者 kbps 在 ±15% 内相等（真频谱态下都该≈151 kbps payload）。
- `mid`：只有 851 B，帧数≈`high` 的 1/2，kbps 37.8 ±15%。
- `low`：只有 851 B，帧数≈`high` 的 1/4，kbps 18.9 ±15%。
- 浏览器轴 `low`：≈9–10 kbps（deflate 把 850 零字节压掉了，所以两个轴的数字**差很多**，引用时必须标明口径）。

- [ ] **步骤 4：回退态也要分频**

拔掉/停掉 scope 数据源（或按 `SDD V2.68` 里的方法把 `MRRC_FTDI_LIB_DIR` 指到空目录）让服务端进 S-meter 回退，重跑步骤 3：

- 日志出现 `Spectrum broadcast active: S-meter fallback`（这行文本必须与改动前逐字相同——全局约束 11）。
- `high` 帧数≈回退态的 30 fps（比真频谱态高很多，**这是预期**）；`low` 仍是 `high` 的 ≈1/4。旧行为里回退态比真频谱态贵 ≈2.7 倍且不受任何节流——现在两个态的比例关系必须一致。

- [ ] **步骤 5：浏览器里手动验三件事**

1. 菜单 → Settings → 新增的 **NET** 选择器可见，默认 Full。
2. 切到 Quarter：DevTools → Network → WS → `/WSspectrum` 的 Messages 里帧长从 1701 变成 851、帧率变稀；频谱/瀑布**仍在画**（wf1 分支），控制台无新错。
3. 刷新页面：选择器仍是 Quarter（cookie `ft710_scopeProfile`），且**不需手动重连**就继续拿 851 B 帧（onopen 重发 caps）。
4. 服务端日志应出现 `Spectrum profile low for operator socket: wf1 frame (851 B), divider 4`，且 `Session metrics:` 行带 `payload≈` 与 `spectrum frames high=…/low=…`。

- [ ] **步骤 6：老客户端兼容（不可跳）**

- 已装版 Android（不带 caps）连新服务端：频谱正常、帧长 1701、无断帧。没有旧包就用 `--no-caps` 探针代替（步骤 3 已覆盖字节层），并在报告里标明“未做 APK 真机回归，P2 补”。
- 已装版 iOS 同理（它本来就只读 wf1，851 B 帧它也能吃，但它不会发 caps ⇒ 拿到的仍是 1701 B）。
- **本阶段不得发安卓包**：安卓侧无代码改动；若后续要做 P2，走 APK-only 通道（`release.sh --apk-only` + `publish-card.sh`），**绍不跑全站 deploy**。

- [ ] **步骤 7：把实测数字回写规格**

把步骤 3–5 的输出贴进 `docs/superpowers/specs/2026-10-07-spectrum-bandwidth-profiles-design.md` 的 §6（验收）下方一个“实测结果”小节，包含：每档的帧长分布、fps、kbps（**标明 payload / 浏览器线上两个轴**）、回退态对比、以及任何不达项与原因。

```bash
git add docs/superpowers/specs/2026-10-07-spectrum-bandwidth-profiles-design.md
.venv/bin/python .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git commit -m "docs: 频谱档位 P1 真机验收实测数字回写规格 §6"
```

---

## P1 之后（不在本计划内，各自走 spec → plan）

- **P2 Android**：放宽 `SpectrumFrame.kt` 的 `size != 1701` 硬判、发 caps、Settings 里加档位；同时清掉 `SpectrumFrame.kt:12`、`SpectrumProcessor.kt:5`、`ConnectionManager.kt:227`、`MainViewModel.kt:209` 里“~30fps × 1701B ≈ 51KB/s”这类陈旧注释——**必须走 APK-only 发布**（`release.sh --apk-only` + `publish-card.sh`，绍不跑全站 deploy）。
- **P3 iOS**：发 caps + 设置项（它已能吃 851 B 帧，但按“无 caps 不变”的不变量，不发就永远拿 `high`）；同时改 `SpectrumProcessor.swift:49,51` 那两行“Expect 1701 bytes”注释。
- **跨仓 `mrrc_hub`**：`NFR-H007`（0.48 Mbps）与 `AD-H14`（频谱占 86%）建在 30 fps 假设上；实测真频谱 151 kbps payload、占比 72%，且现在可按档配。hub 扇出门槛的**输入数据**需修正（在 hub 仓单独走一轮）。
- **音频码率档**：先得真的实现 `setOpusBitrate`（目前只在注释与文档里）。
