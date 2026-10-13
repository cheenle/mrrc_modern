# FT8 Phase A — 打开深路径 + 基线采集 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修掉让级联深路径 100% 空转的预算算术错误，把升级率暴露成可观测量，并在 W103D 上采到一份能决定 Phase B 是否值得做的基线。

**Architecture:** 三条独立的改动线。（a）`ft8/engine/orchestrator.py` 的决策截止从 2.5 s 抬到 4.0 s —— 这是唯一一行真正解锁深路径的改动，其余都是让这一步**看得见**；（b）把已经存在但无出口的 `OrchestratorCounters.escalations` / `fast_decodes` 接到 `runtime.cascade_stats` 和一条新的 `GET /api/ft8/engine/stats`，页面底部状态栏显示升级率；（c）两个运维步骤 —— 把盒子升到含本改动的版本，然后空跑 3 天收基线。**不动任何 DSP 代码**：Phase A 的全部价值是拿数据，不是拿性能。

**Tech Stack:** Python 3 / FastAPI / pytest（FT8 套件是 pytest-only）/ 原生 ES module 前端（`static/ft8/*.js`）/ systemd（W103D 盒子）。

**Spec:** `docs/superpowers/specs/2026-10-13-ft8-fast-path-recall-port-design.md`（§5 是 Phase A 的规格）

## Global Constraints

- **FT8 栈铁律**：`os.environ.setdefault("OMP_STACKSIZE", "10M")` 必须在导入 NumPy / 加载 `libwsjt_core` **之前**执行。栈不足的表现是**段错误**而不是异常。本计划不碰 DSP，但任何顺手动到 `ft8/dsp/` 或 worker 入口的改动都要先确认这条没被挪动。
- **测试命令**：`/Users/cheenle/HAM/hub/mrrc_modern/.venv/bin/python -m pytest tests/ft8 -q`（FT8 套件是 pytest-only，55 个模块；`unittest discover` 收集不到它）。非 FT8 套件用 `python -m unittest discover -s tests -v`。
- **`vendor-ft8lib/` 零改动**：`ft8/dsp/vendor-ft8lib/` 是 SHA-256 门禁的 pristine 上游，"本地修改：无"。本计划（Phase A）**不碰它**；Phase B 才引入 `ft8/dsp/patched-ft8lib/`。
- **`website/sdd/**` 是生成物**：改完 `SDD/*.md` 必须跑 `python3 website/build_sdd.py` 重新生成，否则 `tests/test_release_artifacts.py` / `tests/test_sdd_docs_consistency.py` 会红。
- **提交信息**：短祈使句摘要；结尾必须带 `Co-Authored-By: Claude Code <noreply@anthropic.com>`。
- **不要在默认分支上直接提交**：当前分支 `feat/ft8`，Phase A 的提交落在这里。
- **令牌不进 URL**（AD-024）：新接口用 cookie / Bearer。
- **真实安装的 `MRRC_WEB_HOST` 是 `0.0.0.0`**；只有隔离测试写 `127.0.0.1`。
- **盒子是生产机**：`/opt/mrrc_modern` 是活的现场服务。运维步骤要挑时间窗；任何实验留在 `/tmp`。

## Review Focus

以下五类输入/条件，spec 没有逐条写测试，但最可能咬到用这套软件的人。**每一类都必须在下面某个任务的测试里被钉住**（已在括号里指明归属）。

1. **落在旧截止与新截止之间的槽（2.5 s < 完成时刻 ≤ 4.0 s）** —— 改动前它是 display-only、永不喂 sequencer；改动后它会推进 QSO。合理预期：这种槽**应该**推进 QSO（这正是放宽截止的目的），而**超过** 4.0 s 的槽仍然必须只显示。→ Task 1 的两条测试（一条从常量推导的"不 late"，一条既有的"late 仍然 display-only"）。
2. **引擎未启动时读 `runtime.cascade_stats`** —— 合理预期：返回 `None` 或至少一个不含计数器的 dict，而不是 `AttributeError`（那会让 `/api/ft8/health` 在引擎没起来时 500）。→ Task 3。
3. **引擎未启动时 `GET /api/ft8/engine/stats`** —— 合理预期：一个合法的空/`null` 载荷 + 200，而不是 500。→ Task 4。
4. **延迟超过新截止的解码（例如 4.5 s）** —— 合理预期：落进溢出桶并被计数，而不是被丢弃或与 2.6 s 混在一个桶里。→ Task 2。
5. **页面轮询 stats 时会话已失效（401）** —— 合理预期：走既有的 `api.js` 401 路径重载到登录页，而不是留下一个坏掉的面板或未捕获的 promise rejection。→ Task 5。

---

## File Structure

| 文件 | 责任 | 动作 |
|---|---|---|
| `ft8/engine/orchestrator.py` | 决策截止常量 + 槽循环 | 改（**工作树已改**，只需验证并提交） |
| `tests/ft8/engine/test_orchestrator.py` | 截止的回归守卫 | 改（**工作树已改**，只需验证并提交） |
| `ft8/engine/latency.py` | 解码延迟直方图（量程必须覆盖截止） | 改 |
| `tests/ft8/engine/test_latency.py` | 直方图 + `cascade_stats` 属性 | 改 |
| `ft8/runtime.py` | `cascade_stats` 属性（唯一的计数器出口） | 改 |
| `ft8/web/ft8_api.py` | `_engine_stats()` 纯函数 + 新路由 | 改 |
| `tests/ft8/web/test_ft8_api.py` | 新路由的契约测试 | 改 |
| `static/ft8/api.js` | REST 客户端（加一个方法） | 改 |
| `static/ft8/main.js` | 轮询 + 渲染到 `#state-bar` | 改 |
| `static/ft8.html` | 状态栏加一个 `<span>` | 改 |
| `SDD/09-architecture-overview.md` | 截止值写在这里 | 改 |
| `SDD/13-feasibility-assessment.md` | 同上 | 改 |
| `SDD/14-version-history.md` | 新版本行 | 改 |
| `docs/superpowers/specs/2026-10-13-ft8-fast-path-cascade-design.md` | 级联的规格，截止是它的参数 | 改 |
| `website/sdd/**` | **生成物** | 重新生成 |
| `CHANGELOG.md` | 版本条目 | 改 |

**为什么新增一个端点而不是扩展 `/api/ft8/health`**：`/health` 是诊断面（`safety` / `lease` / `sessions` / `decode_latency`），今天**没有任何前端消费它**（`grep health static/ft8/` 无命中）；而升级率是**操作员要看的运行指标**，需要一个形状为显示而设计的轮询端点。两者受众不同，合成一个会让 `/health` 的每个消费者为不需要的字段付带宽。

---

### Task 1: 落盘决策截止修正（A1）

**Files:**
- Modify (工作树已改): `ft8/engine/orchestrator.py:34-63`
- Modify (工作树已改): `tests/ft8/engine/test_orchestrator.py`

**Interfaces:**
- Consumes: `ft8.engine.cascade.DEFAULT_DEEP_LOWER_BOUND_SECONDS` (2.3)、`DEFAULT_SAFETY_MARGIN_SECONDS` (0.05)
- Produces: `ft8.engine.orchestrator.DEFAULT_DECISION_CUTOFF_SECONDS = 4.0`、`ft8.engine.orchestrator.FAST_PATH_FINISH_DELAY_SECONDS = 0.48`（Task 2 会 import 前者）

**背景（写给零上下文的实现者）**：`CascadingDecoder._remaining_budget` 算的是
`cutoff − (clock() − boundary) − safety_margin`，而快路径的结果实测落在边界后 0.48 s，
所以守卫肯放行深路径的充要条件是 `cutoff − 0.48 − 0.05 ≥ 2.3`。旧值 2.5 在这条不等式上
差 0.33 s，于是 W103D 上**每一次**策略批准的升级都被「budget too tight」拒掉——
一段 2.5 h 的现场日志里 263 次拒绝、零升级。工作树里这个修正**连同它的两条回归守卫已经写好了**，
本任务只负责验证并落盘。

- [ ] **Step 1: 确认工作树的改动就是这两件事**

Run:
```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
git diff --stat
```
Expected: 恰好两个文件，`ft8/engine/orchestrator.py` 与 `tests/ft8/engine/test_orchestrator.py`。
若出现第三个文件，先弄清它是谁带来的再继续（`git diff <该文件>`）。

- [ ] **Step 2: 确认常量取值**

Run:
```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
grep -n 'DEFAULT_DECISION_CUTOFF_SECONDS = \|FAST_PATH_FINISH_DELAY_SECONDS = ' ft8/engine/orchestrator.py
grep -n 'DEFAULT_DEEP_LOWER_BOUND_SECONDS = \|DEFAULT_SAFETY_MARGIN_SECONDS = ' ft8/engine/cascade.py
```
Expected:
```
ft8/engine/orchestrator.py:55:DEFAULT_DECISION_CUTOFF_SECONDS = 4.0
ft8/engine/orchestrator.py:63:FAST_PATH_FINISH_DELAY_SECONDS = 0.48
ft8/engine/cascade.py:<n>:DEFAULT_DEEP_LOWER_BOUND_SECONDS = 2.3
ft8/engine/cascade.py:<n>:DEFAULT_SAFETY_MARGIN_SECONDS = 0.05
```
**四项缺一不可**：Task 2 的直方图量程要 import `DEFAULT_DECISION_CUTOFF_SECONDS`，测试要 import 另外三项。

- [ ] **Step 3: 跑那两条守卫，确认它们真的在守**

Run:
```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
.venv/bin/python -m pytest tests/ft8/engine/test_orchestrator.py -q \
  -k 'deep_path or leaves_room or late_result'
```
Expected: **3 passed**（`test_decision_cutoff_leaves_room_for_the_deep_path`、
`test_escalated_slot_finishing_after_the_deep_path_still_reaches_the_sequencer`、
`test_late_result_is_display_only_and_never_fed`）。

- [ ] **Step 4: 反向验证 —— 把截止改回 2.5，确认它变红**

这一步是**契约的证明**，不是形式。守卫若在旧值下依然绿，它就是装饰品。

Run:
```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
sed -i.bak 's/^DEFAULT_DECISION_CUTOFF_SECONDS = 4.0$/DEFAULT_DECISION_CUTOFF_SECONDS = 2.5/' ft8/engine/orchestrator.py
.venv/bin/python -m pytest tests/ft8/engine/test_orchestrator.py -q \
  -k 'deep_path or leaves_room' ; echo "exit=$?"
mv ft8/engine/orchestrator.py.bak ft8/engine/orchestrator.py
grep -n 'DEFAULT_DECISION_CUTOFF_SECONDS = ' ft8/engine/orchestrator.py
```
Expected: 两次测试 **failed**（`exit=1`），且最后一行打印回 `= 4.0`。
若 `mv` 那行没把文件还原，**停下来**用 `git checkout -- ft8/engine/orchestrator.py` 恢复后重做 ——
绝不能在 2.5 的状态下继续。

- [ ] **Step 5: 跑整个 FT8 套件**

Run:
```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
.venv/bin/python -m pytest tests/ft8 -q
```
Expected: 全绿（0 failed）。DSP 相关用例需要已构建的 `libwsjt_core`；若报缺库，先跑
`ft8/dsp/build.sh` 再重试，**不要**跳过失败的用例。

- [ ] **Step 6: 提交**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
git add ft8/engine/orchestrator.py tests/ft8/engine/test_orchestrator.py
git commit -m "fix(ft8): the decision cutoff never admitted the cascade's second path

The budget guard computes
\`cutoff − (clock() − boundary) − safety_margin\` and compares it against the
deep path's 2.3 s lower bound. The fast path's result lands 0.48 s after the
slot boundary, so a 2.5 s cutoff left only 0.15 s — every policy-approved
escalation was refused with \"budget too tight\" (263 refusals / 0 escalations
over one ~2.5 h W103D field window). The deep path and AP never ran.

4.0 s leaves 1.17 s of slack (4.0 − 0.48 − 0.05 = 3.47 vs 2.3) while the
worst case still finishes inside the window (0.48 + 2.25 = 2.73 s).

Two guards, both derived from the constants rather than hard-coded, pin it:
one asserts the escalated slot's result reaches the sequencer, one asserts
the budget arithmetic itself.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 2: 把延迟直方图的量程对齐到新截止

**Files:**
- Modify: `ft8/engine/latency.py:5-6, 16-19`
- Test: `tests/ft8/engine/test_latency.py:46-74`

**Interfaces:**
- Consumes: `ft8.engine.orchestrator.DEFAULT_DECISION_CUTOFF_SECONDS`（Task 1，= 4.0）
- Produces: `ft8.engine.latency.BUCKET_BOUNDS_SECONDS = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0)`

**为什么要改**：`latency.py` 的模块 docstring 写着桶"aligned with the I9 2.5 s TX decision
cutoff"，而 `BUCKET_BOUNDS_SECONDS` 的最后一个边界就是 2.5。截止已经变成 4.0，于是
**任何 2.5–4.0 s 之间的解码都落进溢出桶**——一个 2.6 s 与一个 3.9 s 的解码读数完全一样，
而它们离截止的远近截然不同。直方图的存在理由正是"在 deadline-miss 计数爬升之前看出
profile 变化威胁到截止"，所以量程必须跟着截止走。

- [ ] **Step 1: 写失败测试**

在 `tests/ft8/engine/test_latency.py` 顶部 import 区加入（保留既有的 `BUCKET_BOUNDS_SECONDS` /
`LatencyHistogram` / `bucket_index` 三个 import 不动，在它们之后追加）：

```python
from ft8.engine.latency import BUCKET_BOUNDS_SECONDS  # noqa: F811 - 见下方常量
from ft8.engine.orchestrator import DEFAULT_DECISION_CUTOFF_SECONDS
```

在文件末尾追加：

```python
def test_bucket_bounds_track_the_decision_cutoff() -> None:
    """直方图的量程必须**跟着截止走** —— 这正是它存在的理由。

    `latency.py` 的模块 docstring 说桶对齐到 I9 决策截止，那就不能只对齐一次：
    截止从 2.5 抬到 4.0 之后，若最后一个边界还停在 2.5，任何落在 2.5–4.0 s 的
    解码都掉进溢出桶 —— 一个 2.6 s 与一个 3.9 s 的解码读数完全一样，而它们
    离截止的远近截然不同。断言两个**常量**的关系，而不是写死 4.0。
    """

    assert BUCKET_BOUNDS_SECONDS[-1] == DEFAULT_DECISION_CUTOFF_SECONDS, (
        f"latency buckets top out at {BUCKET_BOUNDS_SECONDS[-1]}s but the I9 "
        f"decision cutoff is {DEFAULT_DECISION_CUTOFF_SECONDS}s; decodes in "
        f"between would be indistinguishable in the overflow bucket"
    )


def test_elapsed_between_the_old_and_new_cutoff_is_not_overflow() -> None:
    """2.5–4.0 s 之间的解码必须落在**正常桶**里，且彼此可分辨。"""

    histogram = LatencyHistogram()
    histogram.record("improved", 3, 3, 2.6)
    histogram.record("improved", 3, 3, 3.9)

    buckets = histogram.snapshot()["improved/p3/t3"]["buckets"]
    overflow_label = next(label for label in buckets if label.startswith(">="))
    assert buckets[overflow_label] == 0, f"2.6s/3.9s must not be overflow: {buckets}"
    assert bucket_index(2.6) != bucket_index(3.9)
```

- [ ] **Step 2: 跑测试确认失败**

Run:
```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
.venv/bin/python -m pytest tests/ft8/engine/test_latency.py -q -k 'bucket_bounds_track or old_and_new_cutoff'
```
Expected: 两条都 **FAIL**。`test_bucket_bounds_track_the_decision_cutoff` 报
`assert 2.5 == 4.0`；`test_elapsed_between_the_old_and_new_cutoff_is_not_overflow` 报
两个解码都进了 `>=2.5s`（`buckets[overflow_label] == 2`）。

- [ ] **Step 3: 改量程**

`ft8/engine/latency.py`，把模块 docstring 与常量改成：

```python
"""Decode-latency histograms (NFR-002, §12.8).

One :class:`LatencyHistogram` aggregates wall-clock decode duration per
(path, profile, threads) configuration in fixed buckets whose last edge is
the I9 TX decision cutoff (§13.4), so operators can see whether a profile
change threatens the cutoff before the deadline-miss counter climbs.
The orchestrator already counts actual deadline misses; this module only
measures.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

BUCKET_BOUNDS_SECONDS = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0)
"""Bucket upper edges; the last snapshot entry is the > 4.0 s overflow.

**最后一个边界必须等于** `ft8.engine.orchestrator.DEFAULT_DECISION_CUTOFF_SECONDS`
—— 由 `test_bucket_bounds_track_the_decision_cutoff` 钉住。截止一变，这里跟着变，
否则落在旧边界与新截止之间的解码全掉进溢出桶、彼此不可分辨。
边界只在尾部增长（0.5…2.5 不动），所以历史数据的分桶语义不变。
"""
```

（`bucket_index` 与 `LatencyHistogram` 的函数体**一行都不改** —— 它们已经是
`len(BUCKET_BOUNDS_SECONDS)` 驱动的，量程是唯一的事实来源。）

- [ ] **Step 4: 更新既有断言**

`tests/ft8/engine/test_latency.py`，`test_bucket_index_boundaries` 里
`bucket_index(2.5) == len(BUCKET_BOUNDS_SECONDS)` 现在错了（2.5 有了自己的桶），改成：

```python
def test_bucket_index_boundaries() -> None:
    assert bucket_index(0.0) == 0
    assert bucket_index(0.499) == 0
    assert bucket_index(0.5) == 1
    assert bucket_index(2.499) == 4
    # 2.5 不再是溢出：截止抬到 4.0 之后它是 <3s 桶的一员。
    assert bucket_index(2.5) == 5
    assert bucket_index(3.999) == len(BUCKET_BOUNDS_SECONDS) - 1
    assert bucket_index(4.0) == len(BUCKET_BOUNDS_SECONDS)  # overflow
    assert bucket_index(60.0) == len(BUCKET_BOUNDS_SECONDS)
```

`test_histogram_snapshot_per_configuration` 里 3.1 s 那条记录的期望改成新桶集：

```python
    assert series["buckets"] == {
        "<0.5s": 1,
        "<1s": 1,
        "<1.5s": 0,
        "<2s": 0,
        "<2.5s": 0,
        "<3s": 0,
        "<3.5s": 1,
        "<4s": 0,
        ">=4s": 0,
    }
    assert series["max_seconds"] == 3.1
```

（标签由 `snapshot()` 从 `BUCKET_BOUNDS_SECONDS` 生成：`f"<{bound:g}s"` × 8 个边界
＋ `f">={BUCKET_BOUNDS_SECONDS[-1]:g}s"`，共 9 项。若标签与这里不符，以
`snapshot()` 的实际输出为准并回头核对边界元组 —— 不要在测试里将就。）

- [ ] **Step 5: 跑测试确认通过**

Run:
```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
.venv/bin/python -m pytest tests/ft8/engine/test_latency.py -q
```
Expected: 全绿。

- [ ] **Step 6: 跑整个 FT8 套件**

Run:
```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
.venv/bin/python -m pytest tests/ft8 -q
```
Expected: 全绿。**若有别的模块断言了旧桶集**（例如快照的快照测试），在这一步暴露出来 —— 按
Step 4 的同一原则更新它，不要为了让测试变绿而回退量程。

- [ ] **Step 7: 提交**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
git add ft8/engine/latency.py tests/ft8/engine/test_latency.py
git commit -m "fix(ft8): latency buckets must span the decision cutoff, not stop at 2.5

The module docstring claims the buckets are aligned with the I9 decision
cutoff, but the last edge was 2.5 while the cutoff is now 4.0 — so every
decode between the two fell into the overflow bucket, and a 2.6 s decode
read the same as a 3.9 s one. That is exactly the distinction the histogram
exists to make. Edges only grow at the tail, so historical bucket semantics
are unchanged.

Pinned by a test that compares the two constants rather than hard-coding.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 3: 把升级计数接到 `runtime.cascade_stats`（A3a）

**Files:**
- Modify: `ft8/runtime.py:141-147`
- Test: `tests/ft8/engine/test_latency.py:170-190`（改）+ 新增一条

**Interfaces:**
- Consumes: `OrchestratorCounters.escalations` / `.fast_decodes`（`ft8/engine/orchestrator.py:145,148`，已存在）
- Produces: `Ft8Runtime.cascade_stats -> dict[str, int] | None`，键为
  `deadline_risk`（恒有）+ 引擎启动时的 `escalations` / `fast_decodes`

**背景**：`OrchestratorCounters` 里 `escalations` 和 `fast_decodes` 从级联上线那天起就在数，
但**没有任何出口** —— `cascade_stats` 只返回 `deadline_risk`。升级率是判断阈值松紧的主指标，
也是本次改动的唯一验证信号：截止修好之后 `escalations` 必须从 0 变成非 0。

- [ ] **Step 1: 写失败测试**

在 `tests/ft8/engine/test_latency.py` 的 `test_cascade_stats_property_covers_both_branches`
**之后**追加：

```python
def test_cascade_stats_carries_the_escalation_counters_when_the_engine_runs() -> None:
    """升级率必须能从 `cascade_stats` 读到 —— 它是本次改动唯一的验证信号。

    截止修好之前现场实测 `escalations == 0`（263 次拒绝升级）；修好之后这个数必须
    变成非 0，否则深路径仍然没跑。没有这个出口，唯一的办法是去翻日志。
    """

    from ft8.runtime import Ft8Runtime

    wired = object.__new__(Ft8Runtime)
    wired._cascade = SimpleNamespace(deadline_risk=2)
    wired.orchestrator = SimpleNamespace(
        counters=SimpleNamespace(escalations=7, fast_decodes=93)
    )
    assert wired.cascade_stats == {
        "deadline_risk": 2,
        "escalations": 7,
        "fast_decodes": 93,
    }


def test_cascade_stats_omits_counters_when_the_orchestrator_is_not_wired() -> None:
    """构造到 `start()` 之间的实例没有 orchestrator —— 必须少键，不能抛。

    `object.__new__(Ft8Runtime)` 就是这个形状（`__init__` 没跑过），
    `test_cascade_stats_property_covers_both_branches` 已有先例。
    若这里抛 AttributeError，`/api/ft8/health` 在引擎没起来时 500。
    """

    from ft8.runtime import Ft8Runtime

    unwired = object.__new__(Ft8Runtime)
    unwired._cascade = SimpleNamespace(deadline_risk=4)
    assert unwired.cascade_stats == {"deadline_risk": 4}
```

- [ ] **Step 2: 跑测试确认失败**

Run:
```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
.venv/bin/python -m pytest tests/ft8/engine/test_latency.py -q -k cascade_stats
```
Expected: `test_cascade_stats_carries_the_escalation_counters_when_the_engine_runs` **FAIL**
（`assert {'deadline_risk': 2} == {'deadline_risk': 2, 'escalations': 7, ...}`）；
`test_cascade_stats_omits_counters_when_the_orchestrator_is_not_wired` 此刻会**意外通过**
（现在的实现本来就只返回 deadline_risk）—— 这是预期的，它守的是**改完之后**不许把这条弄坏。

- [ ] **Step 3: 实现**

`ft8/runtime.py:141-147`，整个属性替换为：

```python
    @property
    def cascade_stats(self) -> dict[str, int] | None:
        """级联解码器的运行期计数；引擎未启动时为 None。

        `deadline_risk` 来自级联解码器本身，两个计数器来自编排器 —— 升级率
        = `escalations / decodes`，是判断阈值松紧、以及判断深路径**有没有在跑**
        的主指标（2026-10-13 之前现场实测恒为 0：截止 2.5 s 把每一次升级都挡在
        预算守卫外）。

        `orchestrator` 用 `getattr(..., None)` 取：构造到 `start()` 之间的实例
        还没有它，此时少两个键而不是抛 AttributeError —— 否则 `/api/ft8/health`
        在引擎没起来时 500。与 `_cascade` 同一约定。
        """

        cascade = getattr(self, "_cascade", None)
        if cascade is None:
            return None
        stats = {"deadline_risk": cascade.deadline_risk}
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is not None:
            counters = orchestrator.counters
            stats["escalations"] = counters.escalations
            stats["fast_decodes"] = counters.fast_decodes
        return stats
```

- [ ] **Step 4: 跑测试确认通过**

Run:
```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
.venv/bin/python -m pytest tests/ft8/engine/test_latency.py -q
```
Expected: 全绿 —— 含既有的 `test_cascade_stats_property_covers_both_branches`
（它构造的实例没有 `orchestrator`，所以仍只期望 `{"deadline_risk": 4}`）。

- [ ] **Step 5: 跑整个 FT8 套件**

Run:
```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
.venv/bin/python -m pytest tests/ft8 -q
```
Expected: 全绿。`tests/ft8/test_server_wiring.py` 与 `tests/ft8/web/test_ft8_api.py`
里若有替身断言了 `cascade` 的键集，在这一步暴露并按 Step 3 的约定更新。

- [ ] **Step 6: 提交**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
git add ft8/runtime.py tests/ft8/engine/test_latency.py
git commit -m "feat(ft8): expose the escalation counters through cascade_stats

OrchestratorCounters has counted escalations and fast_decodes since the
cascade landed, but cascade_stats returned only deadline_risk — so the
escalation rate, the one signal that says whether the deep path is actually
running, was reachable only by reading logs. The orchestrator is read with
getattr(..., None) so the pre-start() shape omits the keys instead of
raising, matching the _cascade convention.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 4: 新增 `GET /api/ft8/engine/stats`（A3b）

**Files:**
- Modify: `ft8/web/ft8_api.py`（新增 `_engine_stats()` 纯函数 + 一条路由）
- Test: `tests/ft8/web/test_ft8_api.py`

**Interfaces:**
- Consumes: `AppState.orchestrator`、`AppState.runtime`、`AppState.latency`
- Produces: `ft8.web.ft8_api._engine_stats(state: AppState) -> dict[str, Any]`；
  `GET /api/ft8/engine/stats` → `_ok({"engine": {...}, "decode_latency": {...}})`

**为什么是纯函数**：`_health()` 的既有形状就是"纯函数 + 路由薄壳"，测试直接喂
`SimpleNamespace` 而不起 HTTP（`test_latency.py:121` 已有先例）。照抄这个形状，
新逻辑才测得起。

**`engine` 的键集**（缺一不可，前端按名取值）：`running`、`slots_started`、`slots_skipped`、
`decodes`、`fast_decodes`、`escalations`、`deadline_misses`、`decode_errors`、`escalation_rate`。

- [ ] **Step 1: 写失败测试**

在 `tests/ft8/web/test_ft8_api.py` 末尾追加：

```python
from ft8.web.ft8_api import _engine_stats


def _counters(**overrides):
    base = dict(
        slots_started=100,
        slots_skipped=3,
        decodes=97,
        fast_decodes=90,
        escalations=7,
        deadline_misses=1,
        decode_errors=0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_engine_stats_reports_the_escalation_rate() -> None:
    """升级率 = escalations / decodes —— 判断截止修好没修好的唯一读数。"""

    state = SimpleNamespace(
        orchestrator=SimpleNamespace(counters=_counters()),
        runtime=None,
        latency=None,
    )
    engine = _engine_stats(state)["engine"]
    assert engine["escalations"] == 7
    assert engine["fast_decodes"] == 90
    assert engine["decodes"] == 97
    assert engine["slots_started"] == 100
    assert engine["slots_skipped"] == 3
    assert engine["deadline_misses"] == 1
    assert engine["decode_errors"] == 0
    # 7/97 —— 不做四舍五入到整数，否则一次 0.4% 的升级率会显示成 0。
    assert round(engine["escalation_rate"], 6) == round(7 / 97, 6)


def test_engine_stats_escalation_rate_is_zero_when_nothing_decoded() -> None:
    """decodes == 0 时不能除零 —— 引擎刚起来就是这个状态。"""

    state = SimpleNamespace(
        orchestrator=SimpleNamespace(counters=_counters(decodes=0, fast_decodes=0, escalations=0)),
        runtime=None,
        latency=None,
    )
    assert _engine_stats(state)["engine"]["escalation_rate"] == 0.0


def test_engine_stats_degrades_when_the_engine_is_not_wired() -> None:
    """引擎未启动 → 合法的空载荷，不是 500（也没有 AttributeError）。"""

    state = SimpleNamespace(orchestrator=None, runtime=None, latency=None)
    payload = _engine_stats(state)
    assert payload["engine"] is None
    assert "decode_latency" not in payload


def test_engine_stats_finds_the_orchestrator_through_the_runtime() -> None:
    """与 `_health` 同一套解析：state.orchestrator 优先，否则走 state.runtime。"""

    state = SimpleNamespace(
        orchestrator=None,
        runtime=SimpleNamespace(orchestrator=SimpleNamespace(counters=_counters(escalations=1))),
        latency=None,
    )
    assert _engine_stats(state)["engine"]["escalations"] == 1


def test_engine_stats_includes_the_latency_histogram_when_wired() -> None:
    histogram = LatencyHistogram()
    histogram.record("fast", 3, 3, 0.15)
    state = SimpleNamespace(
        orchestrator=SimpleNamespace(counters=_counters()),
        runtime=None,
        latency=histogram,
    )
    assert _engine_stats(state)["decode_latency"]["fast/p3/t3"]["count"] == 1
```

（文件顶部若没有 `from types import SimpleNamespace` 与
`from ft8.engine.latency import LatencyHistogram`，一并补上。）

- [ ] **Step 2: 跑测试确认失败**

Run:
```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
.venv/bin/python -m pytest tests/ft8/web/test_ft8_api.py -q -k engine_stats
```
Expected: 收集期即 **ERROR** —— `ImportError: cannot import name '_engine_stats'`。

- [ ] **Step 3: 实现 `_engine_stats()`**

`ft8/web/ft8_api.py`，紧接在 `_health()` 之后（`_health` 定义在 `:911` 附近）加入：

```python
def _engine_stats(state: AppState) -> dict[str, Any]:
    """FT8 引擎的运行期读数；引擎未启动时 ``engine`` 为 None。

    与 `_health` 分开是**受众**决定的：`_health` 答的是"活着吗"（safety / lease /
    sessions），这里答的是"解码干得怎么样"。升级率是 2026-10-13 那次截止修正的
    唯一验证信号 —— 修好之前它是 0，之后必须非 0。

    `escalation_rate` 用浮点而不是百分比整数：一次 0.4% 的升级率显示成 0 就白测了。
    """

    orchestrator = state.orchestrator
    if orchestrator is None and getattr(state, "runtime", None) is not None:
        orchestrator = getattr(state.runtime, "orchestrator", None)
    payload: dict[str, Any] = {}
    if orchestrator is None:
        payload["engine"] = None
    else:
        counters = orchestrator.counters
        decodes = counters.decodes
        payload["engine"] = {
            "running": True,
            "slots_started": counters.slots_started,
            "slots_skipped": counters.slots_skipped,
            "decodes": decodes,
            "fast_decodes": counters.fast_decodes,
            "escalations": counters.escalations,
            "deadline_misses": counters.deadline_misses,
            "decode_errors": counters.decode_errors,
            # 分母为 0 是引擎刚起来的正常状态，不是异常。
            "escalation_rate": (counters.escalations / decodes) if decodes else 0.0,
        }
    if state.latency is not None:
        payload["decode_latency"] = state.latency.snapshot()
    return payload
```

- [ ] **Step 4: 加路由**

`ft8/web/ft8_api.py`，紧接在 `health` 路由（`@router.get("/health")`，`:559` 附近）之后加入：

```python
    @router.get("/engine/stats")
    async def engine_stats(session: Session = Depends(require_session)) -> JSONResponse:
        """Decoder counters for the FT8 page's status bar (see ``_engine_stats``).

        Read-only and session-gated like every other ``/api/ft8`` route; it
        mutates nothing, so it takes ``require_session`` and not
        ``require_lease``.
        """

        return _ok(_engine_stats(state))
```

- [ ] **Step 5: 跑测试确认通过**

Run:
```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
.venv/bin/python -m pytest tests/ft8/web/test_ft8_api.py -q
```
Expected: 全绿。

- [ ] **Step 6: 端到端验证路由真的挂上了**

纯函数绿不等于路由存在。用既有的测试夹具起一个 app 并请求它：

Run:
```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
.venv/bin/python -m pytest tests/ft8/web/test_ft8_api.py -q -k 'route or client or require_session'
```
Expected: 全绿。若该文件里已有"未认证 → 401"的参数化用例覆盖了所有路由，
**确认新的 `/engine/stats` 也被它覆盖**；没有的话在本任务里补一条：
`assert client.get("/api/ft8/engine/stats").status_code == 401`（不带 cookie）。

- [ ] **Step 7: 跑整个 FT8 套件**

Run:
```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
.venv/bin/python -m pytest tests/ft8 -q
```
Expected: 全绿。

- [ ] **Step 8: 提交**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
git add ft8/web/ft8_api.py tests/ft8/web/test_ft8_api.py
git commit -m "feat(ft8): GET /api/ft8/engine/stats for the decoder counters

The escalation rate is the one reading that says whether the 2.5 -> 4.0 s
cutoff fix actually unblocked the deep path, and nothing served it. Kept
separate from /health because the audiences differ: /health answers \"is it
alive\" (safety / lease / sessions) and has no frontend consumer today,
while this answers \"how is decoding doing\" and is polled by the page.

The escalation rate is a float, not a percentage integer — a 0.4% rate
displayed as 0 would defeat the measurement.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 5: FT8 页面显示升级率（A3c）

**Files:**
- Modify: `static/ft8/api.js`（加一个方法）
- Modify: `static/ft8.html:96`（状态栏加一个 `<span>`）
- Modify: `static/ft8/main.js`（轮询 + 渲染）
- Test: `tests/ft8/web/test_static_assets.py`

**Interfaces:**
- Consumes: `GET /api/ft8/engine/stats`（Task 4）
- Produces: `api.engineStats()`；DOM 元素 `#engine-stats`

**背景**：`static/ft8/api.js` 的 `request()` 已经在 401 时 `location.reload()`
（见文件顶部注释与实现），所以**不要**自己再写一套错误处理 —— 直接
`api.engineStats()` 就能拿到这条安全性，Review Focus 第 5 条靠的就是它。

- [ ] **Step 1: 写失败测试**

在 `tests/ft8/web/test_static_assets.py` 末尾追加：

```python
def test_ft8_page_declares_the_engine_stats_element() -> None:
    """状态栏必须有承载升级率的元素，否则渲染代码无处可写。"""

    html = (ROOT / "static" / "ft8.html").read_text(encoding="utf-8")
    assert 'id="engine-stats"' in html


def test_ft8_api_client_exposes_engine_stats() -> None:
    """页面通过 `api.engineStats()` 取数 —— 方法名是跨文件的契约。"""

    js = (ROOT / "static" / "ft8" / "api.js").read_text(encoding="utf-8")
    assert "engineStats:" in js
    assert '"/engine/stats"' in js


def test_ft8_main_polls_the_engine_stats_endpoint() -> None:
    main = (ROOT / "static" / "ft8" / "main.js").read_text(encoding="utf-8")
    assert "engineStats()" in main
```

（`ROOT` 若在该文件里没有定义，照抄同一文件内其它用例的取法。）

- [ ] **Step 2: 跑测试确认失败**

Run:
```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
.venv/bin/python -m pytest tests/ft8/web/test_static_assets.py -q -k engine_stats
```
Expected: 三条全 **FAIL**。

- [ ] **Step 3: 加客户端方法**

`static/ft8/api.js`，在 `export const api = {` 的对象里、`state: () => request("/state"),` 之后加：

```js
  engineStats: () => request("/engine/stats"),
```

- [ ] **Step 4: 加状态栏元素**

`static/ft8.html:96`，在 `<footer id="state-bar">` 的**第一个** `state-group`
（`<div id="interlocks" ...>`）**之前**插入：

```html
      <span id="engine-stats" class="state-group" title="升级率 = 深路径解码的时隙占比">—</span>
```

- [ ] **Step 5: 轮询并渲染**

`static/ft8/main.js`，在已有的轮询/定时器建立处（`main.js:87` 附近访问
`#state-bar .actions` 的那个初始化块内）加入。**周期取 30 s**：升级率是
慢变量（一个 15 s 时隙最多贡献 14 个样本都不到），15 s 轮询只会让请求数翻倍：

```js
  // 升级率是慢变量：30 s 一次足够，而 15 s 一次会让请求数白翻一倍。
  // 401 由 api.js 的 request() 统一重载处理，这里不重复。
  const engineStatsEl = document.querySelector("#engine-stats");
  if (engineStatsEl) {
    const renderEngineStats = async () => {
      const { status, engine } = await api.engineStats();
      if (status !== 200 || !engine) {
        engineStatsEl.textContent = "—";
        return;
      }
      const pct = (engine.escalation_rate * 100).toFixed(1);
      engineStatsEl.textContent = `升级 ${pct}% · ${engine.escalations}/${engine.decodes}`;
      engineStatsEl.title =
        `时隙 ${engine.slots_started} · 跳过 ${engine.slots_skipped} · ` +
        `快路径 ${engine.fast_decodes} · 升级 ${engine.escalations} · ` +
        `超时 ${engine.deadline_misses} · 错误 ${engine.decode_errors}`;
    };
    renderEngineStats();
    setInterval(renderEngineStats, 30_000);
  }
```

（`api` 在该文件里已 import；若 import 名不同，以文件顶部实际的 import 为准。）

- [ ] **Step 6: 跑测试确认通过**

Run:
```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
.venv/bin/python -m pytest tests/ft8/web/test_static_assets.py -q
```
Expected: 全绿。

- [ ] **Step 7: 真浏览器里看一眼**

Run:
```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
MRRC_RADIO_MODEL=ft710 MRRC_SERIAL_PORT=/dev/null MRRC_WEB_PORT=8899 python server.py
```
然后在浏览器打开 `https://localhost:8899/ft8`，登录后：

Expected:
- 状态栏左侧出现形如 `升级 0.0% · 0/0` 的读数；引擎未启动时是 `—`（`engine` 为 `null`）。
- DevTools Network 里每 30 s 一次 `GET /api/ft8/engine/stats`，状态 200。
- 手动退出登录（或清 `mrrc_auth` cookie）后再等一个周期：页面**重载到登录页**，
  控制台**没有**未捕获的 promise rejection。

（这一步没有电也能做：`/dev/null` 串口下引擎起不来，正好验证 `engine: null` 的降级路径。）

- [ ] **Step 8: 提交**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
git add static/ft8/api.js static/ft8.html static/ft8/main.js tests/ft8/web/test_static_assets.py
git commit -m "feat(ft8): show the escalation rate in the page status bar

The cutoff fix is only verifiable at a glance if the rate is on screen.
Polled every 30 s rather than 15 s — the rate is a slow variable and a
faster poll would just double the request count. The 401 path is inherited
from api.js's request(), not reimplemented.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 6: 文档同步（A1 的行为变更）

**Files:**
- Modify: `SDD/09-architecture-overview.md:114,116`
- Modify: `SDD/13-feasibility-assessment.md:14`
- Modify: `SDD/14-version-history.md`（新版本行）
- Modify: `docs/superpowers/specs/2026-10-13-ft8-fast-path-cascade-design.md`
- Regenerate: `website/sdd/**`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: Tasks 1–5 的**全部**改动（本任务是它们的 doc-sync）
- Produces: 无代码产物

**背景**：AGENTS.md 的 SDD-Guardian 流程要求行为变更欠一次 doc-sync。这里的变更
是"决策截止 2.5 → 4.0"，而 2.5 被**逐字写在四处**：SDD 两章、级联 spec、以及
`website/sdd/**` 里由 SDD 生成的 HTML。`tests/test_sdd_docs_consistency.py` 与
`tests/test_release_artifacts.py` 会盯着生成物是否与源一致。

- [ ] **Step 1: 找出所有写死的 2.5**

Run:
```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
grep -rn '2\.5 s\|2\.5s' SDD/*.md docs/superpowers/specs/2026-10-13-ft8-fast-path-cascade-design.md
```
Expected: 至少命中 `SDD/09-architecture-overview.md:114,116`、
`SDD/13-feasibility-assessment.md:14`，以及级联 spec 里描述截止的段落。
**逐条判断**：只有描述 **I9 决策截止**的才改；`SDD/09:104` 与
`SDD/14-version-history.md:98` 里的 2.5 是 ATR1000 的 meter wait，**不要动**。

- [ ] **Step 2: 改 SDD 两章**

`SDD/09-architecture-overview.md:114` 的
"when the time left before the 2.5 s decision cutoff is below the deep path's
measured lower bound" → `4.0 s`。
`:116` 的 "the ``2.5 s`` ``late`` rule is unchanged" → `4.0 s`。

`SDD/13-feasibility-assessment.md:14` 的 "rather than miss the 2.5 s decision
cutoff" → `4.0 s`，并把该行结尾 "The escalation *rate* is not yet measured in
the field" 换成实测结论：**Phase A 上线后**此处填现场升级率（Task 8 的产出）。
本任务先写 `The escalation rate is served by GET /api/ft8/engine/stats and shown
on the FT8 page status bar (Phase A)`，数字由 Task 8 回填 —— **在 Task 8 完成前
不要写数字**。

- [ ] **Step 3: 改级联 spec**

`docs/superpowers/specs/2026-10-13-ft8-fast-path-cascade-design.md` 里凡把截止写成
2.5 的地方，改成 4.0，并在同段补一句指向本次改动的理由（预算守卫的算术，
见 `2026-10-13-ft8-fast-path-recall-port-design.md` §5 A1）。**级联 spec 是截止值的
规格来源**，不改它，下一个读代码的人会以为 4.0 是手滑。

- [ ] **Step 4: 重新生成 SDD 网站**

Run:
```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
python3 website/build_sdd.py
git status --short website/sdd
```
Expected: `website/sdd/09-architecture-overview.html` 与 `13-*.html` 被改写。
**若生成器报错或没有产出**，先修生成器再继续 —— 手改生成物是更坏的选择。

- [ ] **Step 5: 写版本历史与 CHANGELOG**

`SDD/14-version-history.md` 追加一行（照该文件既有格式：版本、日期、作者、正文）。
`CHANGELOG.md` 顶部按既有格式加入本次条目。版本号取**下一个**待发布号；
本次改动的定位是"修一个让功能完全空转的算术错误 + 补观测"，不是新特性。

- [ ] **Step 6: 跑受影响的测试**

Run:
```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
.venv/bin/python -m pytest tests/ft8 -q
python -m unittest tests.test_sdd_docs_consistency tests.test_release_artifacts -v 2>&1 | tail -20
```
Expected: 全绿。`test_release_artifacts` 离线档只查本地规则；若它报某个
"carries a version" 的文件过期，按它的提示更新（它指的是 `release-artifacts.json`
登记的制品）。

- [ ] **Step 7: 提交**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
git add SDD/09-architecture-overview.md SDD/13-feasibility-assessment.md \
        SDD/14-version-history.md website/sdd CHANGELOG.md \
        docs/superpowers/specs/2026-10-13-ft8-fast-path-cascade-design.md
git commit -m "docs(ft8): the I9 decision cutoff is 4.0 s, and it is measured now

2.5 was written verbatim in two SDD chapters and in the cascade spec; the
generated website follows. The feasibility line said the escalation rate
was not yet measured in the field — it is now served by
GET /api/ft8/engine/stats and shown in the page status bar.

Deliberately not touched: the 2.5 s in SDD/09:104 and the version history
is the ATR1000 meter wait, a different number that happens to match.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 7: 把 w103d 升到含本次改动的版本（A2，运维）

**Files:** 无（运维操作）

**Interfaces:**
- Consumes: Tasks 1–6 已合并到盒子会拉取的分支
- Produces: `/opt/mrrc_modern` 上的新版本，供 Task 8 采基线

**背景**：盒子现为 1.25.5，仓库是 v1.27.0。**这不是可选项** —— Phase A 的验收要在
装得出来的版本上做，否则"改了没生效"无从分辨。

- [ ] **Step 1: 确认提交已在盒子会拉的分支上**

Run:
```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
git log --oneline -8
git branch --show-current
```
Expected: 见 Task 1–6 的六个提交。若当前仍是 `feat/ft8` 而盒子跟的是 `main`，
先按仓库的既有流程合并到 `main`（**不要**为了图快让盒子跟 feature 分支 ——
那会把一个未评审的分支变成现场版本）。

- [ ] **Step 2: 记录升级前的状态**

Run:
```bash
ssh root@w103d.vlsc.net 'cat /opt/mrrc_modern/version.txt; uptime; systemctl is-active mrrc-modern'
```
Expected: 打印升级前的版本、负载、服务状态。**存档这三行** —— Task 8 的对比需要它。

- [ ] **Step 3: 升级**

Run:
```bash
ssh root@w103d.vlsc.net \
  'MRRC_UPDATE_SRC=https://github.com/cheenle/mrrc_modern.git MRRC_UPDATE_BRANCH=main mrrc-update'
```
（`mrrc-update` 是 `/usr/local/bin` 下的安装产物，来自
`packaging/common/mrrc-service-layer.sh:166`；它需要 `MRRC_UPDATE_SRC`，默认
`MRRC_BRANCH=main`、`MRRC_HOME=/opt/mrrc_modern`。它**不删除**任何文件，只增加和覆盖。）

Expected: 脚本跑完无错。**它会重启服务。**

- [ ] **Step 4: 验证升级生效，且是我们要的那一版**

Run:
```bash
ssh root@w103d.vlsc.net \
  'cat /opt/mrrc_modern/version.txt; systemctl is-active mrrc-modern; \
   grep -n "DEFAULT_DECISION_CUTOFF_SECONDS = " /opt/mrrc_modern/ft8/engine/orchestrator.py'
```
Expected: 版本号已变（不再是 1.25.5），服务 `active`，
**且 `DEFAULT_DECISION_CUTOFF_SECONDS = 4.0`**。
最后一行是真正的验收 —— 前两行绿而它还是 2.5，说明拉的树不含本次改动。

- [ ] **Step 5: 确认引擎起来了**

Run:
```bash
ssh root@w103d.vlsc.net 'journalctl -u mrrc-modern -n 40 --no-pager | grep -i "ft8\|worker\|orchestr"'
```
Expected: 见 FT8 worker / orchestrator 启动日志，无 traceback。
若报原生库 ABI 不匹配（`libwsjt_core` 是旧的 `.so`），说明 `mrrc_update.sh`
的按源码摘要重编那一步失败了 —— 看它的输出并**先解决**，不要带着旧库继续。

---

### Task 8: 采集 3 天基线并出报告（A4，运维）

**Files:**
- Create: `docs/ft8-w103d-phase-a-baseline.md`

**Interfaces:**
- Consumes: Task 7 的现场版本（截止 4.0）+ Task 4 的端点
- Produces: 一份含**升级率**的基线报告，决定 Phase B 的投入产出比（spec §13 O-1）

**背景**：spec §4 说了为什么这个任务存在：深路径从来没跑过，所以"它到底比快路径
多解了几个"是**未知**的。如果基线显示它几乎不多解，Phase B 的优先级要重排。
这个报告是路线 C 的全部理由。

- [ ] **Step 1: 建采集脚本**

Create `/Users/cheenle/HAM/hub/mrrc_modern/dev_tools/ft8_baseline.sh`：

```bash
#!/bin/bash
# 采样 W103D 上 FT8 引擎的运行读数与 CPU 消耗。
#
# 用 /proc/<pid>/stat 的 utime+stime 差算 core-seconds —— 这是**内核**给的 CPU 时间，
# 不是 wall（`DecodeBatch.elapsed_seconds` 是 wall，代码里量不到 CPU 时间）。
# 快路径是**每槽**跑的恒定开销，深路径是按需开销，两者角色不同，所以分开记：
# 每槽一次 WS 快照 + 一次 /proc 采样，差值除以时隙数就是每槽 core-s。
set -euo pipefail

HOST="${1:-root@w103d.vlsc.net}"
OUT="${2:-ft8-baseline.csv}"
INTERVAL="${INTERVAL:-900}"      # 15 min 一次，3 天 = 288 个样本
DURATION="${DURATION:-259200}"   # 3 天

echo "utc,slots,decodes,fast,escalations,deadline_misses,decode_errors,worker_core_s,load1" > "$OUT"
START=$(date +%s)
while [ $(( $(date +%s) - START )) -lt "$DURATION" ]; do
  ssh "$HOST" 'python3 - <<'"'"'PY'"'"'
import json, time, urllib.request, glob, os
# 令牌走 cookie 文件，不进 URL（AD-024）。cookie 由 mrrc-show-password 同目录的
# 会话文件取得；取不到就跳过本轮，不要让采集脚本去猜。
cookie = open("/opt/mrrc_modern/data/baseline-cookie").read().strip() \
    if os.path.exists("/opt/mrrc_modern/data/baseline-cookie") else ""
req = urllib.request.Request("https://127.0.0.1:8888/api/ft8/engine/stats",
                             headers={"Cookie": cookie})
import ssl
ctx = ssl._create_unverified_context()   # 自签证书，盒子本地回环
try:
    engine = json.load(urllib.request.urlopen(req, timeout=10, context=ctx))["engine"]
except Exception as exc:
    print(f"# stats unavailable: {exc}", flush=True); raise SystemExit(0)

def cpu_seconds(pid):
    fields = open(f"/proc/{pid}/stat").read().rsplit(") ", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")

# FT8 的 DSP 在一个子进程里（WorkerSupervisor），所以要采子进程；采不到就记 0。
pids = [p for p in glob.glob("/proc/[0-9]*")
        if "mrrc" in open(p + "/cmdline", errors="ignore").read()]
total = sum(cpu_seconds(os.path.basename(p)) for p in pids)
print(json.dumps({"engine": engine, "worker_core_s": round(total, 2),
                  "load1": open("/proc/loadavg").read().split()[0]}))
PY'
  sleep "$INTERVAL"
done
```

**注意**：脚本里的 cookie 路径是示例 —— **采集前必须确认盒子上能拿到一个有效会话**。
若拿不到，改用页面上的一次性方式（登录后从浏览器 DevTools 复制 `mrrc_auth` cookie
到盒子的该文件，权限 600），**绝不要**把令牌写进 URL 或提交进仓库。

- [ ] **Step 2: 起采集**

Run:
```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
chmod +x dev_tools/ft8_baseline.sh
nohup ./dev_tools/ft8_baseline.sh root@w103d.vlsc.net /tmp/ft8-baseline.csv >/tmp/ft8-baseline.log 2>&1 &
```
Expected: `/tmp/ft8-baseline.log` 里出现 CSV 表头 + 第一条数据行。
**这条要跑满 3 天**（`DURATION=259200`），期间不要动盒子。

- [ ] **Step 3: 中途抽查一次（约 1 小时后）**

Run:
```bash
head -5 /tmp/ft8-baseline.csv
```
Expected: 至少两行数据，且 **`escalations` 开始从 0 往上走**。
若 3 小时过去 `escalations` 仍是 0：**停下来**，回盒子查
`journalctl -u mrrc-modern | grep -i "budget too tight"` —— 若仍在拒绝升级，
说明 Task 7 Step 4 的验收漏了，回到那里。

- [ ] **Step 4: 收尾并写报告**

采集结束后：

Run:
```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
python3 - <<'PY'
import csv, statistics
rows = list(csv.DictReader(open("/tmp/ft8-baseline.csv")))
def col(name): return [float(r[name]) for r in rows if r.get(name)]
slots = col("slots"); dec = col("decodes"); esc = col("escalations")
print(f"样本 {len(rows)}")
print(f"时隙总数 {slots[-1]:.0f}  解码总数 {dec[-1]:.0f}")
print(f"升级总数 {esc[-1]:.0f}  升级率 {esc[-1]/dec[-1]*100:.2f}%")
print(f"每槽解码中位 {statistics.median(dec[i]-dec[i-1] for i in range(1,len(dec))):.2f}")
PY
```

把结果写进 `docs/ft8-w103d-phase-a-baseline.md`，**必须包含**：

1. 采集窗口（起止 UTC）与盒子版本（Task 7 Step 2/4 那几行）。
2. 升级率 —— **这是决定 Phase B 去留的数字**。
3. 每槽解码数分布、`deadline_misses`、`decode_errors`。
4. **CPU 占用**（`worker_core_s` 的增量 ÷ 时隙数 = 每槽 core-s），
   对照分析文档 §8 的 6.71 core-s（深路径 t=3）与 ~0.1 core-s（快路径）。
5. **结论一节**：深路径相对快路径多解了多少条报文（这一项若采集期没有人工标注，
   就明确写"未测，需 Phase B 的召回工作台"—— **不要用推测填这个空**）。

- [ ] **Step 5: 提交报告**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
git add dev_tools/ft8_baseline.sh docs/ft8-w103d-phase-a-baseline.md
git commit -m "docs(ft8): Phase A baseline — the escalation rate the cutoff fix bought

Three days on the W103D with the 4.0 s cutoff. Number one is the escalation
rate: it was 0 before the fix (263 refusals / 0 escalations), so anything
above zero is the deep path running for the first time in production.

Also records per-slot core-seconds against the analysis doc's 6.71 (deep,
t=3) and ~0.1 (fast), which is the headroom number Phase B spends.

Where the decode-count gain over the fast path is not measurable without
hand-labelled audio, the report says so rather than estimating.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage** — spec §5 的四项逐条落到任务：

| Spec | Task |
|---|---|
| A1 决策截止修正 | Task 1 |
| A2 版本追齐 | Task 7 |
| A3 观测出口（`cascade_stats` + 端点 + 页面 + 打点） | Tasks 3, 4, 5 |
| A4 基线采集 | Task 8 |
| §14 文档同步（SDD + 级联 spec + CHANGELOG） | Task 6 |

Task 2（直方图量程）**不在 spec 的显式清单里** —— 它是 Task 1 的常量变更**制造出来**的
一致性问题（`latency.py` 自称对齐截止，而截止变了）。spec §3 把 `latency.py` 列为
既有集成面、§5 A3 要求"每槽 core-s 直方图摘要"，这条是该要求的先决条件。

**spec §10 的"每槽 core-s"在 Phase A 里是外部采集的**（Task 8 用
`/proc/<pid>/stat`），**不是**新 API。这是刻意的：`DecodeBatch.elapsed_seconds`
在 `ft8/core/binding.py:406` 是 `time.monotonic() - started`，即 **wall 而非 CPU 时间**；
在进程内量 CPU 时间需要新机制（`RUSAGE_CHILDREN` 或 worker 侧上报），而 Phase A 的
目的是**拿数据**、不是加机制。若 Phase B 需要实时 core-s，那时再加。

**Placeholder scan**：无 TBD / TODO。Task 7 的 `MRRC_UPDATE_SRC` 与 Task 8 的 cookie
路径是**操作员环境值**（本机 git remote 已确认为 `git@github.com:cheenle/mrrc_modern.git`，
脚本里给的是 HTTPS 形式），不是"待补内容"。Task 6 Step 5 的版本号刻意留给实现者按
CHANGELOG 现状取下一个号 —— 写死会与真实发布流程打架。

**Type consistency**：`cascade_stats` 的键（`deadline_risk` / `escalations` /
`fast_decodes`）在 Task 3 定义、Task 4 的 `_engine_stats` **不读它**（直接从
`orchestrator.counters` 取，因为端点要的是全量计数器而非这个聚合视图）—— 两条路径
读同一份真值，不构成重复定义。DOM id `engine-stats`、方法名 `engineStats`、
路径 `/engine/stats` 在 Task 5 的测试与实现里逐字一致。

**Review Focus 覆盖**：第 1 条 → Task 1（两条既有守卫 + Step 4 的反向验证）；
第 2 条 → Task 3 的两条测试；第 3 条 → Task 4 的 `degrades_when_the_engine_is_not_wired`；
第 4 条 → Task 2 的 `elapsed_between_the_old_and_new_cutoff_is_not_overflow`；
第 5 条 → Task 5 Step 7 的手动验证（401 路径由 `api.js` 继承，无法在纯 JS 字符串测试里证明，
因此放在浏览器步骤里）。
