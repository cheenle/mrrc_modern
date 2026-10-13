# FT8 快路径召回增强（ft8_lib 深度移植）设计

- **日期**：2026-10-13
- **状态**：设计（待实现）
- **范围**：W103D 盒子（aarch64，4×Cortex-A53 @1.8 GHz）为主验收目标；代码结构保持跨平台可移植
- **配套**：`2026-10-13-ft8-fast-path-cascade-design.md`（快路径 + 自适应级联本体）——本文件是它的**增量**：不是重做级联，而是（a）让级联的深路径**真的跑到**，（b）把快路径本身**做得更强**
- **依据**：`docs/ft8-w103d-decode-perf-analysis.md`（§0 截止闸门、§2 根因、§7.5 OSD 单次成本、§8 A/B、§9 快路径 spike）
- **外部参考**：SDDC Lab *Optimizing the FT8 Skimmer Experience*；`github.com/RaspSDR/server` → `extensions/FT8/ft8_lib/`（MIT）
- **相关代码**：`ft8/dsp/wsjt_fast.c`、`ft8/dsp/cmake/ft8lib.cmake`、`ft8/dsp/vendor-ft8lib/`、`ft8/engine/orchestrator.py`、`ft8/engine/cascade.py`、`ft8/runtime.py`、`ft8/platform/settings.py`、`ft8/web/ft8_api.py`

---

## 1. 背景与动机

三个**实测**事实（W103D，同机同输入）合成一个结论：**这台盒子的解码能力既没用满，也没用对。**

**事实一 —— 深路径在生产里从未运行过。** 决策截止 2.5 s，而快路径结果落地在 `boundary + 0.48 s`、深路径下界
2.3 s —— `CascadingDecoder._remaining_budget` 只剩 **0.15 s** 窗口。实测一个 ~2.5 h 现场窗口里
**263 次拒绝升级 / 0 次成功**。级联代码齐全、策略齐全、AP 齐全，全部空转。

**事实二 —— CPU 闲置约 90%。** 快路径 ~0.1 core-s/槽（约一个核的 0.7%），深路径 6.71 core-s（threads=3）；
即使**每槽都升级**也只占约 11% 总 CPU，load average 0.11。

**事实三 —— 快路径与深路径之间有一个约 6 dB 宽的召回缺口。** 合成 A/B 在 −30 dB 档：本仓深路径 3/5，
kgoba/ft8_lib 快路径 0/5（分析 §8）。这个缺口目前**没有被任何东西补上** —— 因为事实一。

用户的目标是**同时**提高效率与效果，并让闲置的 CPU 干活。SDDC Lab 的文章给出了一条已验证的路：
**改进 ft8_lib 本身** —— 它的 Web-888 skimmer 用同一颗 A53 跑 12 个 12 kHz 通道。本设计把那条路搬到本仓。

## 2. 目标与非目标

### 目标

| # | 目标 | 度量 |
|---|---|---|
| G1 | **让深路径真的跑到**：修掉预算守卫的算术错误，使策略批准的升级真的发生 | 升级率 > 0；`deadline_risk` 不升 |
| G2 | **缩小快路径与深路径的召回差距**：按依赖顺序增量移植 ft8_lib 的深度改进 | 召回/F1 相对基线上升 |
| G3 | **把召回做成可测的量**：用真实现场录音建召回/精度/F1 工作台 | 每级移植都有前后数字 |
| G4 | **把富余 CPU 换成召回**：明确"用算力买召回"的档位，同时保留要回算力的开关 | 总负载仍 < 30% |
| G5 | **不牺牲已有契约** | `tests/ft8/dsp/` 全绿；ABI / 栈契约 / ctypes 唯一边界 / vendor 治理不变 |

### 非目标

- **N1 不重写深路径**。WSJT-X Fortran 路径不动（除 Phase A 的预算参数外）。分析 §2 的根因 A/B/C/D 是深路径
  *内部*的算法与调度问题，**本期不为它做优化** —— 本期的目的是让它**跑起来**，优化它是一项独立工程。
- **N2 不整体换血 ft8_lib**（见 §6.1：采纳 B-2 增量移植，不采纳 B-1）。
- **N3 不追求多通道**。文章里的 12 通道是 skimmer 场景（一个接收机喂多个频点）；本仓是单电台工作模式，一槽解一次。
- **N4 不改 FT8 协议层**（消息编解码、CRC、LDPC 编码）。
- **N5 不做 GPU/NPU 加速**。A53 上没有可用加速器，且这不是召回瓶颈。
- **N6 不改级联的升级判据**（`CascadePolicy`）。本期只修**预算**，不改**策略**。

## 3. 既有集成面（只读勘察的事实，作为设计约束）

**快路径调用链**（`ft8/dsp/wsjt_fast.c:180-335`）：
`monitor_init` / `monitor_process`（逐块 1920 样本转 float，栈数组 `float block[2048]`，**不许开整槽**）
→ `ftx_find_candidates(FAST_MAX_CANDIDATES, FAST_MIN_SCORE)` → 逐候选 `ftx_decode_candidate(FAST_LDPC_ITERATIONS)`
→ 去重（本地 `seen[]` 表，刻意不进 ABI）→ `ftx_message_decode` → 填 `wsjt_decode_result`（freq / dt / snr / text）。

**当前旋钮**（`wsjt_fast.c` 顶部 `#define`）：`FAST_MAX_CANDIDATES 140`、`FAST_MIN_SCORE 10`、
`FAST_LDPC_ITERATIONS 25`、`FAST_TIME_OSR 2`、`FAST_FREQ_OSR 2`、`FAST_SNR_CALIBRATION_DB 1.0`、
`WSJT_DT_T0_OFFSET 0.5f`。

> 文件内注释**已经记录**："kMin_score 10→5 曾导致候选饱和（154 → 281 ms）"。这正是文章解释并给出解法的那一现象
> —— 文章的做法是**同时**扩大候选池与过采样。这条注释是 §6.3 B1 的直接依据。

**vendor 治理**：`ft8/dsp/vendor-ft8lib/` = kgoba/ft8_lib @ `9fec6ca`，MIT，SHA-256 清单
`vendor-ft8lib.sha256`，README 明写"本地修改：无；任何需要改上游的地方必须走 patch 并记录"。
`cmake/ft8lib.cmake` 的 `FT8LIB_SOURCES` 与之**一一对应**，`tests/ft8/dsp/test_ft8lib_cmake_wiring.py` 盯着这层关系。
Fortran 侧已有成熟机制：`ft8/dsp/patched/*.f90` = `vendor-refs/` 原文 + **允许清单内的精确字节替换**，
`tests/ft8/dsp/test_vendor_policy.py` 的 `PATCHED_COPY_CASES` 强制可逆。**本设计把同一套机制复制到 C 侧。**

**上游来源**：`github.com/RaspSDR/server` → `extensions/FT8/ft8_lib/`（MIT）。含本仓没有的 `ft8/osd.c`（9388 B）、
`ft8/osd.h`、`ft8/fast_math.h`、`ft8/decode_internal.h`，以及重写过的 `ft8/decode.c`（14472 B vs 本仓 21189 B）
与多趟/减除驱动 `decode_ft8.cpp`（28754 B）。

> **注意它是 C++ 应用驱动 + C 内核**：我们移植的是**算法**，不是那个应用结构。

**观测面缺口**：`ft8/runtime.py:141-147` 的 `cascade_stats` 只暴露 `deadline_risk`。
`OrchestratorCounters`（`ft8/engine/orchestrator.py:134-148`）里**已经有** `fast_decodes` / `escalations` /
`deadline_misses`，但**没有任何 REST/WS 出口** —— FT8 页面看不到升级率。

**设置面**：`ft8/platform/settings.py` 的 `Ft8Settings` 已是级联策略的持久载体
（`escalate_on_*` / `min_decodes` / `min_peak_snr`），并已接 `PUT /api/ft8/station` → `runtime.set_cascade_policy()`。
新增"解码档位"应挂在同一处，而不是造第二个 store。

**栈契约**（AGENTS.md FT8 铁律）：`OMP_STACKSIZE=10M` 必须先于导入 NumPy / 加载 `libwsjt_core`；
`wsjt_fast.c:216-231` 那段 `float block[2048]` 的注释就是这条契约的现场 —— **逐块转换，绝不为整槽开栈数组**。
**新移植的代码（OSD 尤其）必须同样自查栈**：文章口径 OSD 每候选约 2.14 MB malloc + 约 4.2 MB memset
（分析 §7.5 已量过本仓 Fortran OSD 的同类成本）。

## 4. 架构

两条互相独立的轨，**有信息依赖、无代码依赖**：

```
Phase A（零 DSP 风险，先做，立刻见效）
  A1 决策截止 2.5 → 4.0            （工作树已改，未提交）
  A2 盒子版本追到 v1.27.0          （既有 linux/mrrc_update.sh）
  A3 升级率 / 截止风险 / core-s 暴露到 FT8 API + 页面
  A4 采集多日基线（解码数、升级率、core-s）

Phase B（B-2 增量移植，逐级验收）
  B1 参数档：time_osr 2→4、候选 140→256、阈值 10→5（三件一起，缺一即饱和）
  B2 OSD：移植 osd.c/h，接到 LDPC 失败回退
  B3 多趟 + 信号减除：解出的信号从水声图扣除，重扫
  B4 收尾：消息格式假信号过滤、SNR 浮点化
  每级都过：召回工作台 + tests/ft8/dsp/ 全绿 + 每槽 core-s 不越预算
```

**为什么 A 先于 B**（用户选定的路线 C）：事实一让"深路径到底比快路径多解了几个"**完全未知**。
如果基线显示深路径在运营商真实频段上几乎不多解，那么 B 的优先级要重排 —— 先花大钱改内核就不划算。
Phase A 是**便宜的**（改一个数字 + 若干打点），却决定 B 的投入产出比。这就是 A4 存在的理由。

## 5. Phase A：让深路径真的跑到

### A1 决策截止修正

`DEFAULT_DECISION_CUTOFF_SECONDS` 2.5 → **4.0**（`ft8/engine/orchestrator.py:55`，**工作树已改**）。

推导（含在代码注释里）：`cutoff − FAST_PATH_FINISH_DELAY_SECONDS(0.48) − safety_margin(0.05) = 3.47`
必须 ≥ `DEFAULT_DEEP_LOWER_BOUND_SECONDS(2.3)`；而最坏情况 `0.48 + 2.25 = 2.73 s < 4.0` 仍在窗口内。
约束：`decision_cutoff < period` 由 `Orchestrator.__init__` 强制（`ft8/engine/orchestrator.py:178`）。

**验收**：补一条回归测试，钉住"策略批准的升级必须被预算守卫放行" —— 即模拟 `boundary + 0.48 s` 到达的
快路径结果，断言 `_remaining_budget() >= DEFAULT_DEEP_LOWER_BOUND_SECONDS`。这条测试在旧值 2.5 下必须失败。

### A2 版本追齐

盒子现为 1.25.5，仓库 v1.27.0。升级走既有 `linux/mrrc_update.sh`（FT8 原生库按源码摘要重编，失败不阻断）。
**这不是可选项**：Phase B 的验收要在装得出来的版本上做，否则"改了没生效"无从分辨。

### A3 观测出口

- `runtime.cascade_stats`（`ft8/runtime.py:141`）增加 `escalations` / `fast_decodes`（从 `orchestrator.counters` 读）。
- 新增 **`GET /api/ft8/engine/stats`**（**不**并进 `GET /api/ft8/state` —— 计数器是引擎域的量，
  而 `/state` 是电台状态的量，混在一起会让 `/state` 的消费者为不需要的字段付带宽与缓存代价）。
  返回：`slots_started`、`decodes`、`fast_decodes`、`escalations`、`deadline_misses`、`deadline_risk`、
  `decode_errors`、每槽 core-s 直方图摘要。
- FT8 页面显示"本时段升级率"。
- **打点是给 Hub 用的**（AGENTS.md：新增会话级行为请打点），口径延续 `session_metrics`（计数与字节，不带令牌）。

### A4 基线采集

连续运行 **≥ 3 天**，导出：每槽解码条数、升级率、core-s/槽、`deadline_misses`。**这是 Phase B 的对照面。**

## 6. Phase B：快路径增量移植

### 6.1 源码策略：B-2 增量移植

| 方案 | 做法 | 判断 |
|---|---|---|
| B-1 整体换血 | 用 RaspSDR 的 ft8_lib 整体替换 `vendor-ft8lib` | **否**。丢掉 kgoba 的 SHA 门禁与 `9fec6ca` 血缘；一次引入全部行为变化，召回与性能**无法归因**到具体改动；那份代码还带他们自己的 C++ 应用结构（`decode_internal.h`、`fast_math.h` 是他们的重构产物） |
| **B-2 增量移植（选定）** | kgoba 基线不动；**逐个改动**经 patch 层进入，每级独立验收 | **是**。可归因、可回退、可分级；治理机制已在 Fortran 侧验证过 |
| B-3 自研 | 照论文自己实现 OSD / 减除 | **否**。重复造轮子，风险最高，且没有上游可对照 |

### 6.2 新目录与治理

新增 `ft8/dsp/patched-ft8lib/`（与 Fortran 的 `ft8/dsp/patched/` **并列**，互不干扰）：

- **替换型** —— 必须改动的 kgoba 文件（预计 `ft8/decode.c`、`ft8/ldpc.c`、`common/monitor.c` 之一或数）：
  与 Fortran 同规，= `vendor-ft8lib/` 原文 + **允许清单内的精确字节替换**。
- **新增型** —— 只有上游 fork 才有的文件（`ft8/osd.c`、`ft8/osd.h`、`ft8/fast_math.h` 等）：
  文件头写明来源（`RaspSDR/server @ <commit>`，MIT）+ 本仓的适配说明。

`cmake/ft8lib.cmake` 的 `FT8LIB_SOURCES` 指向 patched 副本并追加新文件；
**`vendor-ft8lib.sha256` 与 `vendor-ft8lib/` 保持不变**（vendor 目录零字节改动）。
`tests/ft8/dsp/test_vendor_ft8lib_policy.py` 扩展为同时强制"替换型可逆"与"新增型带来源头"，
并保持 `test_ft8lib_cmake_wiring.py` 的源列表一致性检查成立。

> **为什么不直接改 `vendor-ft8lib/`**：那会破坏它"本地修改：无"的 SHA 门禁语义 ——
> 而那条语义正是 Phase B 每一步**可归因**的前提。

### 6.3 移植顺序（每级 = 一次提交 + 一次召回验收）

#### B1 参数档（改动最小、收益最先到）

`FAST_TIME_OSR 2 → 4`、`FAST_MAX_CANDIDATES 140 → 256`、`FAST_MIN_SCORE 10 → 5`。

**三件必须一起改。** 本仓注释已记录"单独把阈值降到 5 会让候选饱和（154 → 281 ms）"；文章的解法正是
**同时**扩大候选池与过采样 —— 这是同一现象的两半。

**栈契约陷阱（本级最容易踩）**：`block_size = time_osr × subblock_size`，time_osr 翻倍使
1920 → 3840，**`float block[2048]` 不再够**。缓冲必须同步放大，且 `wsjt_fast.c:227-231` 的几何守卫
（`mon.block_size > sizeof(block)/sizeof(block[0])` → `WSJT_E_INTERNAL`）必须随之更新 —— 守卫的作用是
"几何一变就报错返回，而不是踩栈"，它必须能表达新的几何。

**dt 归一复核**：`wsjt_fast.c:305-308` 的 dt 公式（`WSJT_DT_T0_OFFSET` + `mon.symbol_period` 两项修正）
本身对 `mon.wf.time_osr` 自适应，但**级联合并键的 0.15 s 容差必须在新 osr 下重测** ——
既有实测只覆盖 `time_osr=2`（63/63 落在 0.15 s 内，最坏 0.040 s），4 档需补一轮同样的扫描。

#### B2 OSD

移植 `osd.c` / `osd.h`，接在 `ftx_decode_candidate` 的 LDPC 失败回退上（文章：OSD 是弱信号召回的主要来源）。

**栈与内存必须显式核算**：文章口径约 2.14 MB malloc + 约 4.2 MB memset/候选。移植后 OSD 的工作缓冲要么
静态分配在 worker 线程（受 `OMP_STACKSIZE=10M` 保护），要么显式堆分配 + 跨候选复用 ——
**绝不在快路径栈帧里开大数组**。每槽 core-s 与 wall 都要记，确认仍在 A1 修好的 4.0 s 截止内。

#### B3 多趟 + 信号减除

把 `wsjt_fast.c:262-322` 的单趟"逐候选解一次"改成**多趟**：
一趟解出的信号从水声图（`mon.wf`）里**扣除** → 重扫候选 → 再解，直到无新解或达到趟数上限。

这是文章召回表里最大的一块。算法参考 `decode_ft8.cpp`，实现落在 `wsjt_fast.c`（**C，不是 C++**）。

**配额**：趟数与候选上限共同决定最坏 wall，必须实测 worst-case 并写进 §8 的时间预算 —— 本级是
R1（突破截止）风险最高的一级。

#### B4 收尾

- 消息格式假信号过滤（解出但不符合 FT8 报文格式的剔除 —— 直接提**精度**）。
- SNR 浮点化（`FAST_SNR_CALIBRATION_DB` 现在是标定常数，文章改成浮点估计）。

**低优先**：B1–B3 已吃掉大部分收益。

### 6.4 档位：把富余 CPU 交给用户

`Ft8Settings` 新增 `decode_effort: str`，取值 `fast` / `balanced` / `max`：

| 档位 | 内容 | 用途 |
|---|---|---|
| `fast` | B0（现状） | 算力要回去时（盒子同时跑别的东西） |
| `balanced`（默认） | B1 + B2 | 日常 |
| `max` | B1 + B2 + B3 | 传播好、想抄收最多台时 |

经既有 `PUT /api/ft8/station` → `runtime.set_cascade_policy()` **同一路径**下发到 worker，
不造第二条设置通道。`validate()` 校验取值集合（与 `rig_mode` 同规）。

## 7. 召回验证工作台（真实录音）

**这是 Phase B 每一步的验收依据，也是本设计里唯一全新的工具链。**

### 7.1 语料采集

`MRRC_FT8_CORPUS_DIR` 环境变量：置位时，运行时把**引擎处理的每一个时隙**（**含零解码的空槽** ——
空槽是召回的负样本，缺了它们 precision 无从计算）的 12 kHz int16 原始音频 +
一行 manifest（`slot_id`、UTC、频率、模式）写到该目录。

复用既有 ring 与落盘路径（本仓已有一条"拒绝槽 dump"的先例，`runtime._dumped_slots`），
**不抢采集设备、不打断生产** —— 这是刻意的：盒子的 USB 采集设备被运行中的服务独占，
抢设备意味着中断现场通联。

- **采集窗口**：运营商现场频段、≥ 3 个不同时段（白天 / 傍晚 / 夜间的传播条件不同）、目标 **≥ 2000 槽**。
- **容量**：单槽 = 180000 × 2 B = **360 KB**；2000 槽 ≈ **700 MB**。采集完即拉回本地，
  **不留在盒子**（盒子是生产机，SD 卡容量有限）。
- **不干扰生产**：写盘走有界队列 + 异步落盘；队列满则丢采集，**绝不阻塞解码**
  （与 `recorder.py` 同一原则）。

### 7.2 基准真值

**用本仓自己的深路径（WSJT-X improved 算法）当基准**：

- 它就是我们要逼近的对象（Phase B 的目标是"缩小 fast → deep 差距"），**基准即目标**。
- 无需额外软件、无需在盒子上装 WSJT-X。
- 若开发机装有真 WSJT-X，可作**交叉校验**：真值三方不一致的槽应人工看一眼 ——
  文章也发现"有些额外 spot 其实是真的，反而是 WSJT-X 漏了"。

### 7.3 度量

`dev_tools/ft8_recall.py`（新）：

- **输入**：语料目录 + 快路径构建 + 深路径构建。
- **匹配键**：`(frequency, dt)` 容差 —— **复用** `cascade.merge_batches` 的 `_FREQ_TOL=5.0` /
  `_DT_TOL=0.15`，**不另立一套**（两套容差必然漂）+ 文本一致。
- **输出**：`recall`（快路径命中深路径的比例）、`precision`（快路径解出而深路径没有的比例 ——
  文章发现这部分**不全是误报**，需人工抽检）、`F1`、每槽 wall、每槽 core-s。
- **基线行**：Phase A 未改快路径时的数字。每级 B1–B4 出一行。

### 7.4 与既有测试的关系

工作台是**离线工具**，不进 `tests/`（语料是几百 MB 的现场录音，不进仓库）。但**每一级移植都必须让
`tests/ft8/dsp/` 全绿**：

| 测试 | 角色 |
|---|---|
| `test_ft8_fast_parity.py` | 对 `fixtures/ft8lib_reference.json` 的解码数下界 —— **B1 之后此 fixture 需重新生成**（参数变了参考值自然变），重生成时必须记录新数字来源 |
| `test_ft8_fast_decode.py` / `test_ft8_fast_snr.py` | 回归面 |
| `test_ft8lib_cmake_wiring.py` / `test_vendor_ft8lib_policy.py` | 治理面 |
| `test_i9_benchmark.py` | 性能面 |

## 8. 时间预算与安全

- 快路径 wall 必须**始终** < 决策截止。Phase A 后截止 = 4.0 s；快路径当前中位 153 ms（分析 §9）——
  B1 预计上升、B2/B3 上升更多。**每一级都要实测 worst-case，越线就回退该级。**
- 快路径是**每槽**跑的（不只升级槽），所以它的 CPU 是**恒定开销**；深路径是按需开销。两者角色不同，
  预算要分开记。
- 这台盒子的富余是约 90%，目标是 Phase B 结束后**总负载仍 < 30%**。
- **深路径的 TX 安全边界不变**：决策截止只是"显示 vs 进 sequencer"的分界，真正的"不许迟发"由
  `tx_driver.py` 的 fit guard 把守（分析 §8.3 已确认两层职责不同）。

## 9. 错误处理与回退

- **每级可回退**：B1–B4 各自独立提交；回退 = 回退该提交（删除 patched 副本 / cmake 指针回指 vendor）。
- **运行时降级**：`decode_effort` 可在现场下调；极端情况下设成 `fast` 即回到 B0 行为。
- **快路径整个失败**：既有 cascade fallback（快路径失败 → 补跑深路径）不变，本期不动它。
- **采集失败不影响解码**：见 §7.1 —— 队列满丢采集。

## 10. 观测

| 指标 | 出口 | 用途 |
|---|---|---|
| `escalations` / `fast_decodes` | `GET /api/ft8/engine/stats` | 升级率，判断阈值松紧 |
| `deadline_risk` | 同上（已有） | 预算是否又变紧 |
| 每槽 core-s（快 / 深 / 合计） | 同上 + 日志 | **G4 的核心量** |
| 召回 / 精度 / F1 | `dev_tools/ft8_recall.py` 离线报告 | Phase B 每级验收 |
| corpus 落盘计数 | 日志 | 采集完整性 |

## 11. 测试策略

- **单元 / 契约**：`tests/ft8/dsp/` 全绿（含治理面与性能面）。
- **预算回归**：A1 的新测试（旧值 2.5 下必须失败）。
- **治理**：扩展后的 `test_vendor_ft8lib_policy.py` 必须能**证明**每个 patched 文件 = vendor 原文 + 允许替换。
- **栈契约**：引入 OSD / 多趟缓冲后，补一条"快路径栈帧上界"的测试或静态断言。
- **召回**：离线工作台，每级一行数字。
- **硬件**：Phase A/B 的 wall 与 core-s 都在 **W103D 真机**上测（aarch64 A53），不在开发机外推；
  开发机（macOS / M 系）只作功能验证。按 AGENTS.md 的测试指引，提交时说明"在电台上验证了什么
  vs 只在离线验证了什么"。

## 12. 决策记录

| # | 决策 | 理由 |
|---|---|---|
| D-1 | 先 Phase A 再 Phase B（路线 C） | 深路径 0 运行让"它到底多解了多少"完全未知；先拿基线再决定 B 的投入产出比 |
| D-2 | 保留快路径 + 级联，不整体换血 | 用户明确选择；弱信号 + AP 是 FT8 的存在理由 |
| D-3 | B-2 增量移植（非 B-1 / B-3） | 可归因、可回退、可分级；复用已在 Fortran 侧验证的 patch 治理 |
| D-4 | 新增 `patched-ft8lib/`，`vendor-ft8lib/` 零改动 | 保住 vendor 的"本地修改：无"语义，即保住了可归因性 |
| D-5 | 基准真值用本仓深路径 | 它就是逼近目标；无需在盒子装 WSJT-X；真 WSJT-X 作交叉校验 |
| D-6 | 语料用真实现场录音 | 用户选择；合成信号测不到 AP 增益与真实噪声形态 |
| D-7 | B1 三个参数必须同改 | 单独降阈值会饱和（本仓注释已记录该现象） |
| D-8 | 加 `decode_effort` 档位 | G4 要在用算力买召回的同时，保留现场把算力要回去的开关 |
| D-9 | 语料不留在盒子 | 盒子是生产机且 SD 卡容量有限；采集完即拉走 |

## 13. 风险与开放问题

| # | 风险 | 缓解 |
|---|---|---|
| R1 | 移植后快路径 wall 突破 4.0 s（**B3 多趟最危险**） | 每级实测 worst-case；越线回退该级；`decode_effort` 兜底 |
| R2 | OSD 栈 / 内存踩 AGENTS.md 的 FT8 铁律 | 显式核算；堆分配 + 复用；补栈上界测试 |
| R3 | 基准真值本身不完整（深路径也漏） | 真 WSJT-X 交叉校验 + 人工抽检"快路径多解的" |
| R4 | 语料不代表运营商真实场景 | 多时段采集；记录频段与传播条件 |
| R5 | `ft8lib_reference.json` 重生成掩盖回归 | 重生成时记录新旧数字与来源，进提交信息 |
| R6 | 盒子是生产机，升级 / 采集有中断风险 | A2 走既有 update 脚本；采集不抢设备、不阻塞解码 |
| R7 | 上游 fork 与 kgoba 结构差异大，patch 不可逆 | 新增型文件走"来源头 + 本仓适配"而非字节替换；替换型只用精确替换 |
| O-1 | **Phase A 基线若显示深路径几乎不多解** | 则 B 的优先级下降，可能只做 B1（最便宜的高收益项）—— **这正是路线 C 的意义** |
| O-2 | 文章的召回数字（12/13 通道）在 A53 上能否复现 | 需实测；文章口径是 x86 还是嵌入式未明 |

## 14. 文档同步（Phase 5，随实现逐期完成）

- `docs/ft8-w103d-decode-perf-analysis.md`：新增 Phase A 基线结论与 Phase B 每级对照（本设计的实测主战场）。
- `docs/superpowers/specs/2026-10-13-ft8-fast-path-cascade-design.md`：A1 的截止修正回填到该文档
  —— 它是级联的规格，截止值是它的参数。
- `AGENTS.md` 的 FT8 铁律段：补 `patched-ft8lib/` 的治理说明；"改了 `ft8/dsp/` 就要重跑四条构建路径"
  这条现状不变。
- `SDD/`：观测面（A3）与解码档位（6.4）涉及接口 / 设置，按 SDD-Guardian 的 doc-sync 流程走。
- `tests/README.md`：新增测试文件计数。
- `CHANGELOG.md` + 版本历史。
