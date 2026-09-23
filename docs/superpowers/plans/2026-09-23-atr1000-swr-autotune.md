# ATR-1000 驻波超阈值自动完整调谐 + QRP 学习门限 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 操作员在大驻波天线上发射时不必再按任何按钮 —— ATR-1000 报告 SWR>2.0 且实测发射功率 ≥5 W 持续 1.5 s 时，服务端自动让天调做一次完整调谐（`mode=2`），把原因告诉操作员；调谐确实改善了匹配且落在学习门限内（改善 ≥0.02 且终值 ≤1.8）时，把继电器值写回 `atr1000_tuner.json`，下次回到该频率直接用调好的值。

**架构：** 守卫住在 `atr1000_client.py`（可选组件自身的 worker 内），由设备主动推送的 METER 流驱动，**实例状态、无模块级全局、无锁**；调谐命令走已有的 `_pending_freq`/`_wake` worker 模式（不新增任务/端点/协议）；调谐后的比对挂在已有的 `_poll_loop` 1 s 心跳上。学习入口从「服务端知道 TX」改为「实测功率 ≥3 W」，覆盖面板/外部软件发射（sibling mrrc V5.8.5 的根因 2）。前端复用既有 `atrTuneResult` toast 通道，只加 6 个 auto 阶段。**绝不键控电台**：守卫只发一帧 `FF 04 01 02`，模块内不存在 CAT/PTT 引用。

**技术栈：** Python 3.12 asyncio + `websockets`（既有）、`unittest`（既有测试风格：直接实例化 `ATR1000Client`、`_handle_frame()` 驱动、`AsyncMock` 假 socket、`asyncio.run()`）、原生 JS（`static/modules/atr1000.js`）。

**规格：** `docs/superpowers/specs/2026-09-23-atr1000-swr-autotune-design.md`（本计划实现其 §3–§8；§9 文档同步在任务 6；§10 非目标不变）。

**参照实现（sibling）：** `../mrrc/atr1000_proxy.py:359` `check_swr_retune()`、`../mrrc/atr1000_proxy.py:172-184` 常量块、`../mrrc/docs/superpowers/plans/2026-08-09-atr1000-swr-autotune.md`。

---

## 工作约定（每个任务都适用）

- 测试运行：`cd /Users/cheenle/HAM/mrrc_modern && venv/bin/python -m unittest discover -s tests`（`.venv/bin/python` 亦可）。单模块：`venv/bin/python -m unittest tests.test_atr1000_client -v`。
- **基线：改动前 1299 tests / 66 modules 全绿（OK, skipped=1, ≈24 s）** —— 已实测。每个任务结束时必须全绿。
- **每次 commit 前**：`python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged` 必须 `clean`。
- 提交信息用本仓既有风格（`feat(...)` / `test(...)` / `docs(...)`，短祈使句）。
- 工作树里有**运行时数据** `atr1000_tuner.json` / `mem_channels.json` 处于 modified 状态：**不要 `git add -A`**，每个 commit 只显式 add 本任务的文件。
- `static/ft710_main.js` / `static/ft710_ui.js` 受工具护栏保护，**本计划不修改它们**（本次只改 `static/modules/atr1000.js` 与 `static/sw.js` 的一行版本号）。
- 不碰音频路径、CAT 路径、频谱路径、`/WSatr1000` 协议与 `atr1000_tuner.py` 语义。

---

## 文件结构（先锁定分解，再写任务）

| 路径 | 职责 | 本计划动作 |
| --- | --- | --- |
| `atr1000_client.py` | 可选天调客户端：帧编解码、连接/刷新、学习缓冲、**新增**守卫与调谐后比对 | 修改（任务 1–3） |
| `server.py` | ATR 联动钩子、手动 TUNE assist、`/WSatr1000` 广播 | 修改（任务 4，两处小改） |
| `static/modules/atr1000.js` | 前端天调面板：`atrTuneResult` toast 文案 | 修改（任务 5） |
| `static/sw.js` | Service Worker 缓存清单（`/modules/atr1000.js?v=1`） | 修改 1 行（任务 5） |
| `tests/test_atr1000_client.py` | 客户端单测（帧/学习/调谐启发/回调） | 修改（任务 1–3） |
| `tests/test_atr1000_server.py` | 服务端集成 + 源码护栏 | 修改（任务 4–5） |
| `docs/superpowers/specs/2026-09-23-atr1000-swr-autotune-design.md` | 规格 | 补 1 处（任务 2） |
| SDD §9.8 / §15 / §14 / README、`AGENTS.md`、`README.md`、`CHANGELOG.md`、`tests/README.md`、`website/**` | 文档同步 | 修改（任务 6） |

---

## 任务 1：守卫核心（常量 + 实例状态 + `_check_swr_retune`）

**文件：**
- 修改：`atr1000_client.py`（常量块 66-71 行附近；`__init__` 197-241 行附近；`_maybe_learn` 之后、`# ── Internals` 之前）
- 测试：`tests/test_atr1000_client.py`（新增 `SwrRetuneGuardTests`、`GuardSourceTests`）

- [ ] **步骤 1：编写失败的测试**

在 `tests/test_atr1000_client.py` 顶部 import 块加入新常量与 `Path`：

```python
from pathlib import Path
from unittest.mock import AsyncMock
```

```python
from atr1000_client import (
    ATR1000Client,
    LearningBuffer,
    SCMD_FLAG,
    SCMD_SYNC,
    SCMD_TUNE_MODE,
    SCMD_RELAY_STATUS,
    RELAY_MIN_INTERVAL,
    SWR_RETUNE_COOLDOWN,
    SWR_RETUNE_DEBOUNCE,
    SWR_RETUNE_MAX_FAILS,
    build_sync_frame,
    build_set_relay_frame,
    build_tune_frame,
)
```

在 `if __name__ == "__main__":` 之前追加两个测试类：

```python
class SwrRetuneGuardTests(unittest.TestCase):
    """High-SWR auto-retune guard (sibling mrrc V5.8.0 parity)."""

    def setUp(self):
        self.client = ATR1000Client("127.0.0.1", 1234, storage=FakeStorage())
        self.client.notify_freq(7_074_000)
        self.client._handle_frame(make_relay_frame(1, 10, 20))
        self.client._relay_changed_at = time.monotonic() - 5.0

    def _meter(self, swr_raw, power):
        self.client._handle_frame(make_meter_frame(swr_raw, power))

    def _aged_run(self):
        """Prime a run at the current frequency, then age it past the debounce."""
        self._meter(240, 50)                       # starts the run (sets the freq)
        self.client._swr_high_since = time.monotonic() - SWR_RETUNE_DEBOUNCE - 0.1

    def _fire_run(self):
        """A run primed at the current frequency and older than the debounce."""
        self._aged_run()
        self._meter(240, 50)                       # fires on this frame

    def test_debounce_blocks_until_the_run_is_long_enough(self):
        self._meter(240, 50)                       # starts a run
        self.assertIsNone(self.client._pending_tune_mode)
        self.client._swr_high_since = time.monotonic() - SWR_RETUNE_DEBOUNCE - 0.1
        self._meter(240, 50)
        self.assertEqual(self.client._pending_tune_mode, 2)
        self.assertIsNotNone(self.client._auto_tune)
        self.assertAlmostEqual(self.client._auto_tune["swr_before"], 2.4)

    def test_low_power_never_fires(self):
        self.client._swr_high_since = time.monotonic() - 10.0
        self._meter(240, 2)                        # idle leakage / tuner scan
        self.assertIsNone(self.client._pending_tune_mode)
        self.assertEqual(self.client._swr_high_since, 0.0)

    def test_no_fire_while_the_tuner_is_busy(self):
        self.client._tuning = True
        self.client._swr_high_since = time.monotonic() - 10.0
        self._meter(240, 50)
        self.assertIsNone(self.client._pending_tune_mode)
        self.assertEqual(self.client._swr_high_since, 0.0)

    def test_frequency_change_restarts_the_run(self):
        self.client._swr_high_since = time.monotonic() - 10.0
        self.client.notify_freq(14_100_000)        # QSY
        self._meter(240, 50)
        self.assertIsNone(self.client._pending_tune_mode)   # run restarted

    def test_relay_change_restarts_the_run(self):
        self.client._swr_high_since = time.monotonic() - 10.0
        self.client._relay_changed_at = time.monotonic()
        self._meter(240, 50)
        self.assertIsNone(self.client._pending_tune_mode)

    def test_cooldown_blocks_the_next_run(self):
        self._fire_run()
        self.client._auto_tune = None              # ignore the pending compare
        self.client._pending_tune_mode = None
        self.client._swr_high_since = time.monotonic() - SWR_RETUNE_DEBOUNCE - 0.1
        self._meter(240, 50)                       # inside the 30 s cooldown
        self.assertIsNone(self.client._pending_tune_mode)
        self.client._last_retune_at = time.monotonic() - SWR_RETUNE_COOLDOWN - 1
        self.client._swr_high_since = time.monotonic() - SWR_RETUNE_DEBOUNCE - 0.1
        self._meter(240, 50)
        self.assertEqual(self.client._pending_tune_mode, 2)

    def test_start_event_and_failure_count(self):
        events = []
        self.client.on_tune_event = events.append
        self._fire_run()
        self.assertEqual(events[0]["phase"], "auto_start")
        self.assertEqual(events[0]["attempt"], 1)
        self.assertEqual(events[0]["freq"], 7_074_000)
        self.assertAlmostEqual(events[0]["swr_before"], 2.4)
        self.assertEqual(self.client._retune_fail_count[7074], 1)

    def test_recovery_clears_the_failure_count(self):
        for _ in range(SWR_RETUNE_MAX_FAILS):
            self._fire_run()
            self.client._auto_tune = None
            self.client._pending_tune_mode = None
            self.client._last_retune_at = time.monotonic() - SWR_RETUNE_COOLDOWN - 1
        self.assertEqual(self.client._retune_fail_count[7074], SWR_RETUNE_MAX_FAILS)
        self._meter(120, 50)                       # SWR 1.20 — matched again
        self.assertNotIn(7074, self.client._retune_fail_count)

    def test_give_up_after_three_failures_announces_once(self):
        events = []
        self.client.on_tune_event = events.append
        for _ in range(SWR_RETUNE_MAX_FAILS):
            self._fire_run()
            self.client._auto_tune = None
            self.client._pending_tune_mode = None
            self.client._last_retune_at = time.monotonic() - SWR_RETUNE_COOLDOWN - 1
        self.assertEqual(self.client._swr_high_since, 0.0)
        events.clear()
        self._meter(240, 50)                       # first frame of the next run
        self.assertIsNone(self.client._pending_tune_mode)   # no 4th tune
        self.assertEqual([e["phase"] for e in events], ["auto_giveup"])
        self._meter(240, 50)                       # announce once per run only
        self.assertEqual([e["phase"] for e in events], ["auto_giveup"])

    def test_failure_count_is_per_frequency(self):
        self._fire_run()
        self.client._auto_tune = None
        self.client._pending_tune_mode = None
        self.client._last_retune_at = time.monotonic() - SWR_RETUNE_COOLDOWN - 1
        self.client.notify_freq(14_100_000)        # QSY keeps 7074's count
        self.assertEqual(self.client._retune_fail_count[7074], 1)
        self._aged_run()                           # a QSY restarts the run
        self._meter(240, 50)
        self.assertEqual(self.client._pending_tune_mode, 2)   # new freq fires
        self.assertEqual(self.client._retune_fail_count[14100], 1)
        self.assertEqual(self.client._retune_fail_count[7074], 1)

    def test_tune_event_callback_exception_is_contained(self):
        self.client.on_tune_event = lambda event: 1 / 0
        self._fire_run()                           # must not raise
        self.assertEqual(self.client._pending_tune_mode, 2)


class GuardSourceTests(unittest.TestCase):
    """The auto-retune guard must be structurally unable to key the radio."""

    def test_client_has_no_cat_or_ptt_reference(self):
        src = Path("atr1000_client.py").read_text(encoding="utf-8")
        for forbidden in ("set_ptt", "set_tune(", "cat_controller", "import server"):
            self.assertNotIn(forbidden, src)

    def test_guard_section_only_queues_a_tune_frame(self):
        src = Path("atr1000_client.py").read_text(encoding="utf-8")
        guard = src.split("# ── High-SWR auto-retune guard", 1)[1]
        guard = guard.split("# ── Internals", 1)[0]
        self.assertIn("self._pending_tune_mode = 2", guard)
        self.assertNotIn("set_relay", guard)
```

- [ ] **步骤 2：运行测试确认失败**

运行：`venv/bin/python -m unittest tests.test_atr1000_client -v`
预期：collection error / `ImportError: cannot import name 'SWR_RETUNE_COOLDOWN'`（常量尚不存在）。

- [ ] **步骤 3：实现常量与实例状态**

在 `atr1000_client.py` 的 `LEARN_FREQ_STEP = 1000` 行之后、`# ── Connection / polling parameters ──` 之前插入：

```python
# ── High-SWR auto-retune parameters (sibling mrrc V5.8.0/V5.8.5) ───
SWR_RETUNE_THRESHOLD = 2.0        # strictly greater → too high
SWR_RETUNE_MIN_POWER = 5          # W measured power proving "really transmitting"
SWR_RETUNE_DEBOUNCE = 1.5         # s continuously above the threshold
SWR_RETUNE_COOLDOWN = 30.0        # s between two auto tunes
SWR_RETUNE_MAX_FAILS = 3          # consecutive no-improvement tunes per frequency
SWR_RETUNE_COMPARE_SETTLE = 0.8   # s after tuning clears before comparing SWR
SWR_RETUNE_IMPROVED = 0.02        # minimum improvement required to write back
```

在 `__init__` 的 `self.on_change` 之后加入事件回调：

```python
        # Optional sync callable invoked with read_state() on state changes;
        # the caller (server) schedules broadcasts from it.
        self.on_change: Optional[Callable[[dict], None]] = None
        # Optional sync callable invoked with an auto-tune event dict
        # {"phase": "auto_start"|"auto_success"|"auto_no_improve"|
        #  "auto_timeout"|"auto_aborted"|"auto_giveup", "freq", "swr_before",
        #  "swr_after", "attempt", "message"}; the server broadcasts it to
        # /WSatr1000 clients as atrTuneResult (auto=true). Never raises out.
        self.on_tune_event: Optional[Callable[[dict], None]] = None
```

在 `__init__` 的 `self._last_learned: dict = {}` 之后加入守卫状态（**实例属性，不是模块全局**）：

```python
        # Learn dedup: (freq_khz, sw, ind, cap) → {"swr", "time"}
        self._last_learned: dict = {}

        # High-SWR auto-retune guard (design 2026-09-23-atr1000-swr-autotune)
        self._swr_high_since = 0.0        # monotonic start of the run (0 = none)
        self._swr_high_freq = 0           # frequency the run belongs to (Hz)
        self._last_retune_at = 0.0        # last auto tune (cooldown)
        self._retune_fail_count: dict = {}  # freq_khz → consecutive no-improvement
        self._pending_tune_mode = None    # worker-side request, consumed by _poll_loop
        self._auto_tune = None            # pending post-tune comparison snapshot
        self._auto_tune_compare_at = 0.0  # monotonic deadline (0 = none)
```

- [ ] **步骤 4：实现 `_check_swr_retune`、`_emit_tune_event`，并接入 `_handle_meter`**

在 `_maybe_learn()` 方法之后、`# ── Internals ──` 之前插入：

```python
    # ── High-SWR auto-retune guard ────────────────────────────────

    def _check_swr_retune(self, swr: float, power: float) -> None:
        """Queue one full tune when the operator transmits into a high SWR.

        Runs on the METER stream and only decides — the frame itself is sent
        by the worker (`_flush_auto_tune`), so all device I/O stays in one
        place. The operator's own transmission is the precondition: below
        SWR_RETUNE_MIN_POWER measured power nothing happens, which is why
        this guard can never key the radio. Ported from the sibling project
        (mrrc V5.8.0 `check_swr_retune`), with instance state instead of
        module globals so no locking is needed.
        """
        now = time.monotonic()
        if self._tuning or power < SWR_RETUNE_MIN_POWER or self._freq <= 0:
            self._swr_high_since = 0.0
            return
        if swr <= SWR_RETUNE_THRESHOLD:
            # Matched again — this frequency is eligible for a retune later.
            self._swr_high_since = 0.0
            self._retune_fail_count.pop(self._freq // 1000, None)
            return
        if (self._swr_high_freq == 0
                or abs(self._freq - self._swr_high_freq) > LEARN_FREQ_STEP):
            self._swr_high_since = 0.0        # QSY (or first sight) — new run
        self._swr_high_freq = self._freq
        if (self._relay_changed_at > 0
                and now - self._relay_changed_at < LEARN_IGNORE_WINDOW):
            self._swr_high_since = 0.0        # relays just moved, not settled
            return

        key = self._freq // 1000
        if self._retune_fail_count.get(key, 0) >= SWR_RETUNE_MAX_FAILS:
            if self._swr_high_since == 0.0:
                # Announce once per run, then stay quiet until the frequency
                # changes or the SWR recovers.
                self._swr_high_since = now
                logger.info(
                    "SWR %.2f still high, auto tune given up after %d tries "
                    "(%.1f kHz)", swr, SWR_RETUNE_MAX_FAILS, self._freq / 1000)
                self._emit_tune_event({
                    "phase": "auto_giveup", "freq": self._freq,
                    "swr_before": round(swr, 2), "swr_after": None,
                    "attempt": self._retune_fail_count[key], "message": "",
                })
            return

        if self._swr_high_since == 0.0:
            self._swr_high_since = now
        if (now - self._swr_high_since < SWR_RETUNE_DEBOUNCE
                or now - self._last_retune_at < SWR_RETUNE_COOLDOWN):
            return

        # Fire: queue ONE full-tune frame for the worker.
        self._retune_fail_count[key] = self._retune_fail_count.get(key, 0) + 1
        self._last_retune_at = now
        self._swr_high_since = 0.0
        self._auto_tune = {
            "freq": self._freq,
            "swr_before": swr,
            "relays": (self._sw, self._ind, self._cap),
            "count": self._retune_fail_count[key],
            "reason": "",
        }
        self._pending_tune_mode = 2
        self._wake.set()
        logger.info("SWR %.2f > %.1f at %.1f kHz, auto full tune (try %d)",
                    swr, SWR_RETUNE_THRESHOLD, self._freq / 1000,
                    self._auto_tune["count"])
        self._emit_tune_event({
            "phase": "auto_start", "freq": self._freq,
            "swr_before": round(swr, 2), "swr_after": None,
            "attempt": self._auto_tune["count"], "message": "",
        })

    def _emit_tune_event(self, event: dict) -> None:
        """Dispatch an auto-tune phase to the server callback (contained)."""
        cb = self.on_tune_event
        if cb is None:
            return
        try:
            cb(event)
        except Exception:
            logger.exception("on_tune_event callback failed")
```

然后在 `_handle_meter()` 里接入（`_emit_change()` 之后、学习分支之前 —— 守卫必须**先于**学习的提前返回执行）：

```python
        self._emit_change()

        # High-SWR auto-retune guard (decides only; the worker sends the frame)
        self._check_swr_retune(swr, power)

        # Stable-window learning on the METER stream during TX
        if not (self._tx and not self._tuning and power > 0 and self._freq > 0):
            return
```

- [ ] **步骤 5：运行测试确认通过**

运行：`venv/bin/python -m unittest tests.test_atr1000_client -v`
预期：全部 PASS（既有 `MeterLearningFlowTests` 此时仍全绿 —— 本任务不动学习门限）。

- [ ] **步骤 6：Commit**

```bash
git add atr1000_client.py tests/test_atr1000_client.py
git commit -m "feat(atr1000): 驻波超阈值自动完整调谐守卫（实例状态、不键控）"
```

---

## 任务 2：worker 消费 + 调谐后比对 + 断开中止

**文件：**
- 修改：`atr1000_client.py`（`_poll_loop` 的 `_pending_freq` 块之后；`_handle_tune`；`_clear_tuning`；`_run` 的断开 finally；`_flush_auto_tune` 等新方法）
- 修改：`docs/superpowers/specs/2026-09-23-atr1000-swr-autotune-design.md`（规格补登 `auto_aborted` 阶段 —— 计划阶段发现：断开连接时前端 `tuneInProgress` 会永久卡在 `···`，必须有一条终态事件）
- 测试：`tests/test_atr1000_client.py`（新增 `AutoTuneCompletionTests`）

- [ ] **步骤 0：补规格（`auto_aborted`）**

在规格 §6 第 1 点里把「four existing paths」改成「every tuning-clear path」（`_handle_tune` 的设备显式 `TUNE_STATUS=0` 是第 5 条），并在 §6 第 2 点的分支列表后追加一段：

```markdown
   - 连接断开（`_run` 的 drop 路径）→ 丢弃待比对快照并发 `auto_aborted`
     （"tuner disconnected"）；否则前端 `tuneInProgress` 会永久停在 `···`
     —— `atrTuneResult` 的 `auto_start` 之后必须有且只有一条终态事件。
```

并把 §7 的前端阶段表补一行：

```markdown
| `auto_aborted` | `ATR 自动调谐中断: <message>` | 回到 `TUNE` |
```

- [ ] **步骤 1：编写失败的测试**

在 `tests/test_atr1000_client.py` 追加：

```python
class AutoTuneCompletionTests(unittest.TestCase):
    """Queued-frame flush + post-tune comparison (write back, never roll back)."""

    def setUp(self):
        self.storage = FakeStorage()
        self.client = ATR1000Client("127.0.0.1", 1234, storage=self.storage)
        self.client.notify_freq(7_074_000)
        self.client._handle_frame(make_relay_frame(1, 10, 20))
        self.client._relay_changed_at = time.monotonic() - 5.0
        self.events = []
        self.client.on_tune_event = self.events.append

    def _flush(self):
        """Worker step with the comparison deadline reached."""
        now = max(time.monotonic(), self.client._auto_tune_compare_at + 0.001)
        asyncio.run(self.client._flush_auto_tune(now))

    def _pending(self, swr_before=2.4):
        self.client._auto_tune = {
            "freq": 7_074_000, "swr_before": swr_before,
            "relays": (1, 10, 20), "count": 1, "reason": "",
        }
        self.client._auto_tune_compare_at = time.monotonic() - 0.1

    def test_worker_flushes_a_queued_tune_frame(self):
        self.client._ws = AsyncMock()
        self.client._pending_tune_mode = 2
        asyncio.run(self.client._flush_auto_tune(time.monotonic()))
        self.assertEqual(self.client._ws.send.await_count, 1)
        self.assertEqual(self.client._pending_tune_mode, None)
        self.assertTrue(self.client._tuning)

    def test_improved_swr_writes_back_with_force_update(self):
        self._pending()
        # The tuner landed on better relays and the SWR improved.
        self.client._handle_frame(make_relay_frame(1, 40, 60))
        self.client._handle_frame(make_meter_frame(135, 50))   # SWR 1.35
        self._flush()
        self.assertEqual(self.storage.learned,
                         [(7_074_000, 1, 40, 60, 1.35, True)])
        self.assertEqual(self.events[-1]["phase"], "auto_success")
        self.assertIsNone(self.client._auto_tune)

    def test_no_improvement_writes_nothing_and_keeps_the_relays(self):
        self._pending()
        self.client._handle_frame(make_relay_frame(1, 40, 60))
        self.client._handle_frame(make_meter_frame(240, 50))   # still 2.40
        self._flush()
        self.assertEqual(self.storage.learned, [])
        self.assertEqual(self.events[-1]["phase"], "auto_no_improve")
        self.assertEqual((self.client._sw, self.client._ind, self.client._cap),
                         (1, 40, 60))          # what the tuner chose stays

    def test_improvement_outside_the_learn_gate_is_not_stored(self):
        self._pending(swr_before=3.4)
        self.client._handle_frame(make_relay_frame(1, 40, 60))
        self.client._handle_frame(make_meter_frame(200, 50))   # SWR 2.00 > 1.8
        self._flush()
        self.assertEqual(self.storage.learned, [])
        self.assertEqual(self.events[-1]["phase"], "auto_no_improve")

    def test_hard_timeout_is_reported_as_timeout(self):
        self._pending()
        self.client._tuning = True
        self.client._tuning_started_at = time.monotonic()
        self.client._clear_tuning("45s hard timeout")
        self.client._handle_frame(make_meter_frame(240, 50))
        self._flush()
        self.assertEqual(self.events[-1]["phase"], "auto_timeout")

    def test_tune_status_zero_arms_the_comparison(self):
        self._pending()
        self.client._handle_frame(make_tune_frame(0))          # device says done
        self.assertEqual(self.client._auto_tune["reason"], "tune status 0")
        self.client._handle_frame(make_meter_frame(120, 50))   # SWR 1.20
        self._flush()
        self.assertEqual(self.events[-1]["phase"], "auto_success")

    def test_disconnect_aborts_a_pending_comparison(self):
        self._pending()
        self.client._abort_auto_tune("tuner disconnected")
        self.assertIsNone(self.client._auto_tune)
        self.assertEqual(self.client._auto_tune_compare_at, 0.0)
        self.assertEqual(self.events[-1]["phase"], "auto_aborted")
        self.assertEqual(self.events[-1]["message"], "tuner disconnected")

    def test_abort_without_pending_state_is_silent(self):
        self.client._abort_auto_tune("tuner disconnected")
        self.assertEqual(self.events, [])
```

- [ ] **步骤 2：运行测试确认失败**

运行：`venv/bin/python -m unittest tests.test_atr1000_client.AutoTuneCompletionTests -v`
预期：FAIL / ERROR，报 `AttributeError: 'ATR1000Client' object has no attribute '_flush_auto_tune'`。

- [ ] **步骤 3：实现 worker 侧消费与比对**

在 `_maybe_learn` 之后新增的守卫段内（`_emit_tune_event` 之后）追加：

```python
    async def _flush_auto_tune(self, now: float) -> None:
        """Worker-side auto-tune step: send a queued full tune, then run the
        deferred post-tune comparison once its settle time has passed."""
        mode, self._pending_tune_mode = self._pending_tune_mode, None
        if mode is not None:
            try:
                await self.start_tune(mode)
            except Exception as e:
                logger.warning("auto tune send failed: %s", e)
                self._abort_auto_tune("send failed")
        if (self._auto_tune is not None and self._auto_tune_compare_at > 0
                and now >= self._auto_tune_compare_at):
            self._finish_auto_tune()

    def _schedule_auto_tune_compare(self, reason: str) -> None:
        """The first tuning-clear after an auto tune arms the deferred compare
        (later clears keep the original reason)."""
        if self._auto_tune is None or self._auto_tune_compare_at > 0:
            return
        self._auto_tune["reason"] = reason
        self._auto_tune_compare_at = (
            time.monotonic() + SWR_RETUNE_COMPARE_SETTLE)

    def _finish_auto_tune(self) -> None:
        """Compare SWR after an auto tune and write the relays back to the
        learned store when the match improved and landed inside the learn
        gate. The relays are never rolled back (design decision 3)."""
        snap, self._auto_tune = self._auto_tune, None
        self._auto_tune_compare_at = 0.0
        if snap is None:
            return
        final = self._swr
        improved = snap["swr_before"] - final
        if snap.get("reason") == "45s hard timeout":
            phase, message = "auto_timeout", "tune timeout (45s)"
        elif improved >= SWR_RETUNE_IMPROVED and 0 < final <= LEARN_SWR_MAX:
            phase, message = "auto_success", ""
            storage = self._get_storage()
            if storage is not None:
                try:
                    storage.learn(freq=snap["freq"], sw=self._sw,
                                  ind=self._ind, cap=self._cap,
                                  swr=final, force_update=True)
                except Exception as e:
                    logger.warning("auto-tune learn failed: %s", e)
        else:
            phase, message = "auto_no_improve", ""
        logger.info(
            "ATR-1000 auto tune %s: SWR %.2f → %.2f at %.1f kHz (try %d)",
            phase, snap["swr_before"], final, snap["freq"] / 1000, snap["count"])
        self._emit_tune_event({
            "phase": phase, "freq": snap["freq"], "message": message,
            "swr_before": round(snap["swr_before"], 2),
            "swr_after": round(final, 2), "attempt": snap["count"],
        })

    def _abort_auto_tune(self, reason: str) -> None:
        """Discard a pending auto-tune comparison (device gone / send failed)
        and close the UI's in-progress state with a terminal event."""
        snap, self._auto_tune = self._auto_tune, None
        self._auto_tune_compare_at = 0.0
        if snap is None:
            return
        logger.info("ATR-1000 auto tune aborted (%s) at %.1f kHz",
                    reason, snap["freq"] / 1000)
        self._emit_tune_event({
            "phase": "auto_aborted", "freq": snap["freq"], "message": reason,
            "swr_before": round(snap["swr_before"], 2), "swr_after": None,
            "attempt": snap["count"],
        })
```

在 `_poll_loop()` 的 `_pending_freq` 块之后接入：

```python
            # 频率联动 auto-apply scheduled by notify_freq()
            if self._pending_freq is not None:
                await self._apply_pending_freq()

            # Auto-retune: queued full-tune frame + deferred SWR comparison
            await self._flush_auto_tune(now)
```

`_handle_tune()` 的收尾改为（设备显式报 `TUNE_STATUS=0` 也是一条清调谐路径）：

```python
        self._tuning = tuning
        self._tuning_started_at = time.monotonic() if tuning else 0.0
        if not tuning:
            self._tuning_relay_stable_since = 0.0
            self._schedule_auto_tune_compare("tune status 0")
        self._emit_change()
```

`_clear_tuning()` 改为：

```python
    def _clear_tuning(self, reason: str) -> None:
        self._tuning = False
        self._tuning_started_at = 0.0
        self._tuning_relay_stable_since = 0.0
        self._schedule_auto_tune_compare(reason)
        logger.info("tuning cleared (%s)", reason)
```

`_run()` 的断开 `finally` 里补一行（设备状态未知 → 丢弃待比对）：

```python
                    # Drop clears tuning (device state unknown after reconnect).
                    self._tuning = False
                    self._tuning_started_at = 0.0
                    self._tuning_relay_stable_since = 0.0
                    self._abort_auto_tune("tuner disconnected")
                    self._emit_change()
```

- [ ] **步骤 4：补一条源码护栏，运行测试确认通过**

在 `GuardSourceTests` 内追加（把断开中止的接线钉住，避免将来重构时静默丢失）：

```python
    def test_disconnect_wires_the_abort(self):
        src = Path("atr1000_client.py").read_text(encoding="utf-8")
        self.assertIn('self._abort_auto_tune("tuner disconnected")', src)
```

运行：`venv/bin/python -m unittest tests.test_atr1000_client -v`
预期：全部 PASS（含任务 1 的守卫测试与既有 `TuningHeuristicTests`、`MeterLearningFlowTests`）。

- [ ] **步骤 5：Commit**

```bash
git add atr1000_client.py tests/test_atr1000_client.py docs/superpowers/specs/2026-09-23-atr1000-swr-autotune-design.md
git commit -m "feat(atr1000): 调谐后比对写回学习库，断开中止并上报 UI"
```

---

## 任务 3：学习门限改为按实测功率判定（QRP 友好）

**文件：**
- 修改：`atr1000_client.py`（`LEARN_MIN_POWER` 常量；`_handle_meter` 学习分支）
- 测试：`tests/test_atr1000_client.py`（改写 `test_no_learning_when_not_tx`，新增低功率用例）

- [ ] **步骤 1：编写失败的测试**

把 `MeterLearningFlowTests.test_no_learning_when_not_tx` **整段替换**为：

```python
    def test_learns_without_the_server_tx_signal(self):
        """V5.8.5 parity: a panel/external transmission learns too — measured
        power, not notify_tx(), is the gate."""
        self.client.notify_tx(False)
        for _ in range(4):
            self.client._handle_frame(make_meter_frame(120, 50))
        self.assertEqual(len(self.storage.learned), 1)

    def test_no_learning_below_min_power(self):
        for _ in range(6):
            self.client._handle_frame(make_meter_frame(120, 2))   # 2 W idle read
        self.assertEqual(len(self.storage.learned), 0)

    def test_qrp_level_power_still_learns(self):
        for _ in range(4):
            self.client._handle_frame(make_meter_frame(120, 4))   # 4 W ≥ 3 W
        self.assertEqual(len(self.storage.learned), 1)
```

- [ ] **步骤 2：运行测试确认失败**

运行：`venv/bin/python -m unittest tests.test_atr1000_client.MeterLearningFlowTests -v`
预期：`test_learns_without_the_server_tx_signal` 与 `test_qrp_level_power_still_learns` FAIL（学到 0 次，旧门限要求 `_tx` 为真）。

- [ ] **步骤 3：改常量与学习分支**

常量：

```python
LEARN_MIN_POWER = 3          # minimum power (W) — QRP friendly; idle reads ~1-2 W
```

`_handle_meter()` 的学习分支入口：

```python
        # Stable-window learning on the METER stream while transmitting.
        # Measured power — not the server-side TX signal — decides this, so a
        # transmission from the radio panel or external software learns too
        # (sibling mrrc V5.8.5 fix). _tx still drives SYNC suppression only.
        if not (not self._tuning and power >= LEARN_MIN_POWER
                and self._freq > 0):
            return
```

- [ ] **步骤 4：运行测试确认通过**

运行：`venv/bin/python -m unittest tests.test_atr1000_client -v`
预期：全部 PASS（含 `test_learns_after_stable_meter_stream`、`test_no_learning_during_ignore_window`、去重用例）。

- [ ] **步骤 5：Commit**

```bash
git add atr1000_client.py tests/test_atr1000_client.py
git commit -m "feat(atr1000): 学习门限改为实测功率 ≥3W（覆盖面板/外部软件发射）"
```

---

## 任务 4：server 接线（事件转发 + 手动 assist 互斥）

**文件：**
- 修改：`server.py`（`_on_atr_change` 之后；`_start_atr_tune_assist`；lifespan 的 `atr.on_change = _on_atr_change` 之后）
- 测试：`tests/test_atr1000_server.py`（新增 `AutoTuneEventTests`、`TuneAssistBusyTests`）

- [ ] **步骤 1：编写失败的测试**

在 `tests/test_atr1000_server.py` 顶部 import 加 `import json`，并追加：

```python
class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(text)


class AutoTuneEventTests(unittest.TestCase):
    """_on_atr_tune_event → atrTuneResult(auto=true) on the tuner channel."""

    def tearDown(self):
        server.atr_clients.clear()

    def test_event_is_broadcast_with_the_auto_flag(self):
        ws = _FakeWS()

        async def run():
            server.atr_clients.add(ws)
            server._on_atr_tune_event({
                "phase": "auto_success", "freq": 7_074_000,
                "swr_before": 2.4, "swr_after": 1.35, "attempt": 1, "message": "",
            })
            await asyncio.sleep(0)          # let the scheduled broadcast run

        asyncio.run(run())
        self.assertEqual(len(ws.sent), 1)
        msg = json.loads(ws.sent[0])
        self.assertEqual(msg["type"], "atrTuneResult")
        self.assertTrue(msg["auto"])
        self.assertEqual(msg["phase"], "auto_success")
        self.assertEqual(msg["swr_after"], 1.35)

    def test_no_clients_is_a_noop(self):
        server._on_atr_tune_event({"phase": "auto_start"})   # must not raise

    def test_lifespan_wires_the_callback(self):
        self.assertIn("atr.on_tune_event = _on_atr_tune_event", SERVER_SOURCE)


class TuneAssistBusyTests(unittest.TestCase):
    """The manual assist must refuse while an auto tune is running."""

    def setUp(self):
        self.saved = (server.cat, server.atr, server._atr_tune_task)
        self.addCleanup(self._restore)
        server.cat = _FakeCat()
        server.radio.update(tx_status=0)

    def _restore(self):
        server.cat, server.atr, server._atr_tune_task = self.saved
        server.radio.update(tx_status=0)

    def _fake_atr(self, tuning):
        atr = _FakeATR()

        def read_state():
            state = _FakeATR.read_state(atr)
            state["tuning"] = tuning
            return state

        atr.read_state = read_state
        return atr

    def test_refuses_while_the_tuner_is_tuning(self):
        server.atr = self._fake_atr(tuning=True)
        ws = _FakeWS()
        asyncio.run(server._start_atr_tune_assist(ws))
        self.assertIsNone(server._atr_tune_task)
        self.assertIn("already tuning", ws.sent[0])

    def test_launches_when_the_tuner_is_idle(self):
        saved_assist = server._atr_tune_assist

        async def fake_assist():
            return None

        server._atr_tune_assist = fake_assist
        self.addCleanup(setattr, server, "_atr_tune_assist", saved_assist)
        server.atr = self._fake_atr(tuning=False)
        ws = _FakeWS()
        asyncio.run(server._start_atr_tune_assist(ws))
        self.assertIsNotNone(server._atr_tune_task)
        self.assertEqual(ws.sent, [])
```

- [ ] **步骤 2：运行测试确认失败**

运行：`venv/bin/python -m unittest tests.test_atr1000_server -v`
预期：FAIL / ERROR —— `AttributeError: module 'server' has no attribute '_on_atr_tune_event'`，且 `test_refuses_while_the_tuner_is_tuning` 未拒绝。

- [ ] **步骤 3：实现服务端接线**

在 `_on_atr_change()` 之后插入：

```python
def _on_atr_tune_event(event: dict):
    """Sync callback from ATR1000Client: forward an auto-tune phase to the
    tuner channel. Same message type as the manual tune assist, plus
    auto=true so the UI can label it."""
    try:
        asyncio.get_running_loop().create_task(_broadcast_atr(
            {"type": "atrTuneResult", **event, "auto": True}))
    except Exception:
        pass
```

在 `_start_atr_tune_assist()` 的 `if radio.is_transmitting:` 块之后插入（轮询 `tx_status` 有 ≤500 ms 滞后，这条检查关掉该窗口）：

```python
    if atr.read_state().get("tuning"):
        await ws.send_text(json.dumps({
            "type": "error", "message": "Tuner is already tuning"}))
        return
```

lifespan 中 `atr.on_change = _on_atr_change` 之后：

```python
            atr.on_change = _on_atr_change
            atr.on_tune_event = _on_atr_tune_event
```

- [ ] **步骤 4：运行测试确认通过**

运行：`venv/bin/python -m unittest tests.test_atr1000_server -v`
预期：全部 PASS（含既有 `TuneAssist*Tests` 与 `SourceGuardTests`）。

- [ ] **步骤 5：Commit**

```bash
git add server.py tests/test_atr1000_server.py
git commit -m "feat(atr1000): 自动调谐事件转发到 /WSatr1000 + 与手动 TUNE 互斥"
```

---

## 任务 5：前端 6 个 auto 阶段 + 缓存版本

**文件：**
- 修改：`static/modules/atr1000.js`（`onTuneResult` 及其文案）
- 修改：`static/sw.js`（`/modules/atr1000.js?v=1` → `?v=2`）
- 测试：`tests/test_atr1000_server.py`（`SourceGuardTests` 新增前端阶段断言）

- [ ] **步骤 1：编写失败的测试**

在 `SourceGuardTests` 内追加：

```python
    def test_frontend_handles_the_auto_phases(self):
        src = Path("static/modules/atr1000.js").read_text(encoding="utf-8")
        for phase in ("auto_start", "auto_success", "auto_no_improve",
                      "auto_timeout", "auto_aborted", "auto_giveup"):
            self.assertIn(phase, src)
        self.assertIn("自动调谐中", src)
        self.assertIn("已放弃该频点自动调谐", src)

    def test_frontend_cache_bust_for_the_new_module(self):
        sw_src = Path("static/sw.js").read_text(encoding="utf-8")
        self.assertIn("'/modules/atr1000.js?v=2',", sw_src)
```

- [ ] **步骤 2：运行测试确认失败**

运行：`venv/bin/python -m unittest tests.test_atr1000_server.SourceGuardTests -v`
预期：两个新用例 FAIL（阶段名与 `?v=2` 尚不存在）。

- [ ] **步骤 3：实现前端**

`static/modules/atr1000.js` 里把 `onTuneResult` 整个替换为下面两个函数：

```js
    // ── Tune assist ───────────────────────────────────────────────
    function tuneResultText(msg) {
        const before = msg.swr_before, after = msg.swr_after;
        switch (msg.phase) {
        case 'skipped':
            return 'ATR: SWR ' + (before || '?') + ' 已达标,无需调谐';
        case 'success':
            return 'ATR 调谐完成: SWR ' + before + ' → ' + after;
        case 'rollback':
            return 'ATR 调谐无改善,已回滚 (SWR ' + before + ')';
        case 'auto_success':
            return 'ATR 自动调谐完成: SWR ' + before + ' → ' + after;
        case 'auto_no_improve':
            return 'ATR 自动调谐无改善 (SWR ' + before + ' → ' + after + ')';
        case 'auto_timeout':
            return 'ATR 自动调谐超时 (SWR ' + before + ')';
        case 'auto_aborted':
            return 'ATR 自动调谐中断: ' + (msg.message || '天调断开');
        case 'auto_giveup':
            return 'ATR 连续 3 次无改善,已放弃该频点自动调谐';
        default:
            return 'ATR 调谐失败: ' + (msg.message || msg.phase);
        }
    }

    function onTuneResult(msg) {
        const starting = msg.phase === 'start' || msg.phase === 'auto_start';
        if (starting) {
            tuneInProgress = true;
            if (msg.phase === 'auto_start' && typeof showToast === 'function') {
                showToast('ATR: SWR ' + (msg.swr_before || '?') + ' 自动调谐中…');
            }
        } else {
            tuneInProgress = false;
            if (typeof showToast === 'function') {
                showToast(tuneResultText(msg));
            }
        }
        const btn = $('btn-atr-tune');
        if (btn) {
            btn.classList.toggle('tuning', tuneInProgress);
            btn.textContent = tuneInProgress ? '···' : 'TUNE';
        }
    }
```

`static/sw.js` 第 11 行：

```js
    '/modules/atr1000.js?v=2',
```

- [ ] **步骤 4：运行测试确认通过**

运行：`venv/bin/python -m unittest tests.test_atr1000_server.SourceGuardTests -v`
预期：全部 PASS。

- [ ] **步骤 5：手工核对文案与阶段一一对应**

对照规格 §7 表格逐行检查 `tuneResultText()` 的 6 个 auto 阶段文案（含 `auto_giveup` 的「连续 3 次」与参数 `SWR_RETUNE_MAX_FAILS` 一致）。

- [ ] **步骤 6：Commit**

```bash
git add static/modules/atr1000.js static/sw.js tests/test_atr1000_server.py
git commit -m "feat(atr1000): 前端展示自动调谐阶段与结果 toast"
```

---

## 任务 6：文档同步（SDD / 网站 / 模块表 / CHANGELOG / 测试计数）

**文件：**
- 修改：`SDD/09-architecture-overview.md`（§9.8）、`SDD/15-ptt-safety-architecture.md`、`SDD/14-version-history.md`、`SDD/README.md`
- 修改：`website/index.html`、`website/zh/index.html`、`website/sdd.html`、`website/zh/sdd.html`、`website/sdd/*.html`（生成物）
- 修改：`AGENTS.md`、`README.md`、`CHANGELOG.md`、`tests/README.md`

- [ ] **步骤 1：SDD §9.8 增加第 4 条联动行为**

在 `SDD/09-architecture-overview.md` 的 §9.8 「Three linkage behaviors」列表末尾追加第 4 条，并把标题改为 `Four linkage behaviors`：

```markdown
4. **High-SWR auto full tune** — the client's METER path runs a guard (instance state, no module globals): while the operator transmits (measured power ≥ 5 W) and SWR stays > 2.0 for ≥ 1.5 s, it queues ONE full-tune frame (`mode=2`, sent by the worker via the existing `_pending_*`/`_wake` channel). 30 s cooldown; 3 consecutive no-improvement tunes per frequency then `auto_giveup` until the frequency changes or the SWR recovers. After tuning clears (relay-stable / same-relay / TX end / 45 s timeout), a 0.8 s-later comparison writes the relays back with `learn(force_update=True)` **only when SWR improved ≥ 0.02 and the result is ≤ 1.8** — the relays are never rolled back. Auto-tune phases reach the UI as `atrTuneResult` with `auto=true`. **The guard never keys the radio**: it acts only inside an existing transmission and sends nothing but a tuner frame (SDD ch15).
```

- [ ] **步骤 2：SDD ch15 补安全边界一句**

在 `SDD/15-ptt-safety-architecture.md` 的 ATR1000 tune assist 段落后追加：

```markdown
**ATR1000 auto full tune (no self-keying):** the high-SWR guard (`atr1000_client.py`)
never keys the radio — it only acts while the operator is already transmitting (measured
power ≥ 5 W) and emits nothing but an ATR-1000 tune frame. The manual assist's TX2 carrier
path is unchanged and remains the only ATR-initiated keying, with its `finally` drop rule.
```

- [ ] **步骤 3：SDD 版本历史与 Quick Facts**

`SDD/14-version-history.md` 表格顶部新增一行（版本号 = 当前最新 V2.56 + 1 → **V2.57**，日期 2026-09-23），内容描述：ATR-1000 SWR>2 自动完整调谐守卫（实例状态、绝不键控、30 s 冷却、同频 3 次放弃、0.8 s 比对后仅"改善且 ≤1.8"写回）、学习门限改按实测功率 ≥3 W（覆盖面板/外部软件发射）、前端 6 个 auto 阶段 toast、新增测试与修改文件、sibling 参照（mrrc V5.8.0/V5.8.5）。

`SDD/README.md` Quick Facts：`SDD Version` 改 `V2.57`，`Baseline Date` 改 `2026-09-23`。

- [ ] **步骤 4：重新生成网站 SDD 页面并同步版本载体**

```bash
python3 website/build_sdd.py
```

然后手工更新四处版本字符串（与上一次 V2.56 升版一致）：`website/index.html`、`website/zh/index.html`（`<strong>V2.57</strong>`）、`website/sdd.html`、`website/zh/sdd.html`（`SDD V2.57`）。

- [ ] **步骤 5：AGENTS.md 模块表**

`AGENTS.md` 中 `atr1000_client.py` 行（在根目录模块表中）追加行为描述：

```markdown
| `atr1000_client.py` | Optional asyncio WS client for networked ATR1000 tuner: binary frame protocol, 5s reconnect, 55-min refresh, TX-no-SYNC watchdog, learning, throttled relay writes, `notify_freq`/`notify_tx` sync hooks, **high-SWR auto full tune** (SWR>2.0 for ≥1.5s at ≥5W measured power → one `mode=2` frame, 30s cooldown, 3 tries per frequency, never keys the radio) with post-tune write-back only when improved ≥0.02 and ≤1.8; learning gate is measured power ≥3W (panel/external PTT learns too) |
```

- [ ] **步骤 6：README.md 行为句**

在 `README.md` 的 ATR 环境变量表下方（`MRRC_ATR1000_PORT` 行之后）补一行说明：

```markdown
When the tuner is enabled, a transmission into SWR > 2.0 (measured power ≥ 5 W, 1.5 s)
triggers one automatic full tune ("ATR 自动调谐中…" toast; 30 s cooldown, 3 tries per
frequency) and the resulting relays are stored when the match improves to ≤ 1.8. The
auto tune never keys the radio — it only acts while you are already transmitting.
```

- [ ] **步骤 7：CHANGELOG 顶部条目**

`CHANGELOG.md` 在 `# Changelog` 之后、`## [v1.18.1]` 之前插入新的 `## [未发布] — 2026-09-23 — ATR-1000 驻波超阈值自动调谐 + QRP 学习` 段，列出 4 条用户可见变化（自动完整调谐、写回学习库、按功率学习覆盖面板发射、前端提示）与「不键控」安全边界。

- [ ] **步骤 8：tests/README.md 计数**

跑一次全套，把 `tests/README.md` 的模块数/测试数与 `~NNs` 耗时更新为实测值：

```bash
venv/bin/python -m unittest discover -s tests 2>&1 | grep -E "^Ran |^OK|^FAILED"
```

- [ ] **步骤 9：运行文档一致性测试**

运行：`venv/bin/python -m unittest tests.test_sdd_docs_consistency -v`
预期：PASS（Quick Facts 与版本历史一致、生成页与落地页都写着 V2.57）。

- [ ] **步骤 10：Commit**

```bash
git add SDD/09-architecture-overview.md SDD/14-version-history.md SDD/15-ptt-safety-architecture.md SDD/README.md \
        website/index.html website/zh/index.html website/sdd.html website/zh/sdd.html website/sdd \
        AGENTS.md README.md CHANGELOG.md tests/README.md
git commit -m "docs(atr1000): SDD §9.8/§15、版本历史 V2.57、模块表与 CHANGELOG 同步"
```

---

## 任务 7：全量验证与收尾

**文件：** 无代码改动（只跑验证；如发现问题回到对应任务修）

- [ ] **步骤 1：全套测试**

运行：`venv/bin/python -m unittest discover -s tests 2>&1 | grep -E "^Ran |^OK|^FAILED"`
预期：`OK`，测试数 = 基线 1299 + 本次新增（约 +25）。

- [ ] **步骤 2：语法检查**

运行：`venv/bin/python -m py_compile atr1000_client.py server.py`
预期：无输出（退出码 0）。

- [ ] **步骤 3：sdd-guardian 约束检查**

运行：`python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged`
预期：`clean`（若还有未提交改动，先 `git add` 本次涉及的文件；**不要 add** `atr1000_tuner.json` / `mem_channels.json`）。

- [ ] **步骤 4：对照规格逐条核对**

打开规格 §3–§8，逐条指出实现位置（文件:行）。任何一条找不到实现即回到对应任务补齐，不许静默放行。

- [ ] **步骤 5：确认工作树**

运行：`git status --porcelain`
预期：只剩 `M atr1000_tuner.json`、`M mem_channels.json`（运行时数据，故意不提交）。

---

## 附：与 sibling 的对照表（排障用）

| 项 | sibling `../mrrc` | 本仓实现 | 差异原因 |
| --- | --- | --- | --- |
| 守卫位置 | `atr1000_proxy.py` 模块函数 + 全局状态 + `state_lock` | `ATR1000Client` 实例方法 + 实例状态 | client 单 task；避免 V5.8.5 的 `global` 漏声明 bug 类 |
| 触发判定 | 实测功率 ≥5 W | 同 | — |
| 阈值/去抖/冷却/放弃 | 2.0 / 1.5 s / 30 s / 3 | 同 | 照搬 |
| 调谐后写回 | 靠稳定窗自然学习 | 显式 0.8 s 比对 → 改善 ≥0.02 且 ≤1.8 时 `force_update` | 让"更新参数"确定发生，同时把坏参数挡在库外 |
| 学习门槛 | 实测功率 ≥3 W（V5.8.5） | 同 | — |
| 失败后回滚 | 无 | 无 | 语音 QSO 中途回滚有害 |
| 前端 | 代理日志 | 6 个 auto 阶段 toast `atrTuneResult` | 本仓有现成 toast 通道且无 4 层代理 |

---

## 执行偏差记录

### 任务 1（2026-09-23，已完成）

1. **守卫的频率归属判定补 `_swr_high_freq == 0` 分支**。计划原写法 `if (self._swr_high_freq and abs(...) > LEARN_FREQ_STEP)`
   漏了"首次见到该频率"的情形 —— `test_frequency_change_restarts_the_run` 抓到：`_swr_high_freq` 仍为 0 时
   不会重置连续段，于是旧连续段会跨 QSY 触发。生产语义不变（≤1 kHz 抖动仍不重置），新条件只把"首次见到"
   显式化。`atr1000_client.py` 与本文件上方代码块已一致。
2. **守卫测试助手改为按生产时序**：新增 `_aged_run()`（先发一帧起段、同时写入 `_swr_high_freq`，再老化
   `_swr_high_since`），`_fire_run()` = `_aged_run()` + 一帧触发。原计划直接预老化会构造出生产不可能出现的
   状态（有连续段但频率未知：连续段只在 `_swr_high_freq` 被赋值的同一次调用里建立）。
3. `test_failure_count_is_per_frequency` 相应改为 `_aged_run()` 起段后触发 —— QSY 会重启去抖（设计语义，
   不是缺陷）。
4. 任务 1 实测：`Ran 63 tests`（该模块）、全套 `Ran 1312 tests ... OK (skipped=1)`，13 个新增用例。

### 任务 2（2026-09-23，已完成）

1. **规格已更正**（计划要求的步骤 0）：§6.1 由「四条清调谐路径」改为「每条清调谐路径」并补上
   `_handle_tune()` 的设备显式 `TUNE_STATUS=0`（第 5 条）；§6.2 补「连接断开 → 丢弃快照 + `auto_aborted`」
   及其理由（`auto_start` 之后必须有且只有一条终态事件，否则前端 `tuneInProgress` 永久卡在 `···`）；
   §7 补 `auto_aborted` 文案行；§8 第 16 条由「五个 auto 阶段」改为「六个」。
2. **测试脚手架改为照生产时序**：`AutoTuneCompletionTests._pending()` 不再预置
   `_auto_tune_compare_at`（生产里快照建立时它必然是 0，由清调谐路径首次挂接并写入 reason）——
   需要比对的用例显式调用 `_clear_tuning("relay stable >5s")`；`_flush()` 负责把截止时间推到过去。
   原计划写法会让「首次挂接记录 reason」这条逻辑无法被覆盖（早退于 `compare_at > 0`）。
3. 任务 2 实测：该模块 `Ran 72 tests`、全套 `Ran 1321 tests ... OK (skipped=1)`，9 个新增用例。

### 任务 3（2026-09-23，已完成）

1. 计划遗漏的连带修改：`LearningBufferTests.test_rejects_low_power` / `test_accepts_min_power_boundary`
   把旧默认门限写死成字面量 4.9 W / 5.0 W，`LEARN_MIN_POWER` 降到 3 W 后前者会假通过（4.9 W 现在可学习）、
   后者仍通过但语义已错。改为从 `LEARN_MIN_POWER` 派生（`LEARN_MIN_POWER - 0.1` / `LEARN_MIN_POWER`），
   以后再调门限不会静默失效。
2. 任务 3 实测：全套 `Ran 1323 tests ... OK (skipped=1)`（净增 2 个用例：改写 1 个为 3 个）。
