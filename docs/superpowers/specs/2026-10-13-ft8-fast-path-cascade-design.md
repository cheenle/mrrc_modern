# FT8 快路径 + 自适应级联解码 设计

- **日期**：2026-10-13
- **状态**：已实现（2026-10-13；A53 spike 结论与真机前后对照见
  `docs/ft8-w103d-decode-perf-analysis.md` §9。正文的实现订正已回填：
  §5.4 `clock` 基准与必填参数、§6 合并键与 SNR、§9 合并键、§10 观测维度与计数、§13 的 O-1/O-2 状态）
- **范围**：W103D 盒子（aarch64，4×Cortex-A53 @1.8 GHz）为主验收目标；代码结构保持跨平台可移植（macOS / Windows / rpi）
- **配套**：`2026-10-12-ft8-work-mode-design.md`（FT8 工作模式本体）——本文件是其**解码路径**的增量，不覆盖、不修改它
- **依据**：`docs/ft8-w103d-decode-perf-analysis.md` §7（反汇编）与 §8（A/B 实测）
- **相关代码**：`ft8/core/binding.py`、`ft8/core/models.py`、`ft8/core/supervisor.py`、
  `ft8/core/worker.py`、`ft8/engine/dsp_decode.py`、`ft8/engine/orchestrator.py`、
  `ft8/engine/latency.py`、`ft8/dsp/`（CMake / vendor / shim）
- **相关文档**：SDD `09-architecture-overview.md`、`12-operational-model.md`、`13-*`（性能）；
  `docs/PROJECT_MAP.md`、`docs/DOCUMENT_INDEX.md`

---

## 1. 背景与动机

`docs/ft8-w103d-decode-perf-analysis.md` 三轮分析得到三个**实测**结论（W103D，同机同输入）：

1. **真正的闸门不是 8 s 判据，而是决策截止**（`ft8/engine/orchestrator.py` 的
   `DEFAULT_DECISION_CUTOFF_SECONDS`）。本 spec 写作时是 **2.5 s**，生产默认
   profile3/threads3 实测 **2218 ms**，只剩 **~1.15×** 余量；2026-10-13 的
   Phase A 把它改成 **4.0 s**（级联的预算守卫证明 2.5 s 连自己那条深路径都放不下，
   现场 263 次拒绝升级 / 0 次升级），改后同一实测的余量是 **~1.80×**。理由与算术见
   `2026-10-13-ft8-fast-path-recall-port-design.md` §5 A1。
2. **加核买不到时间**：3 线程比 1 线程多烧 **1.6× 总 CPU**，wall 只快 1.46×——
   sync 阶段被每线程原样复制。
3. **换轻量解码器能省下巨量算力**：`kgoba/ft8_lib`（C、kiss-fft、无 OSD / 无 AP / 单趟）
   在同一台机器、同一输入上 **145 ms wall / 0.144 s CPU**，对生产配置是
   **1/15 的 wall、约 1/41 的单核工作量**，而 ≥ −24 dB 档两者都 5/5。

代价则集中在一个约 6 dB 宽的弱信号窗口（实测 −30 档：本仓 3/5，ft8_lib 0/5），
**且 ft8_lib 没有 AP**（先验呼号辅助解码）——而后者的增益在合成信号 A/B 里**根本没被测到**
（分析文档 §8.5 第 3 条），真实通联中损失只会更大。

**结论**：全量替换不可取（砍掉弱信号 + AP，与 FT8 的存在理由相反）。
正确做法是**自适应级联**——默认走轻量快路径，只在需要时对该时隙补跑深度路径。

## 2. 目标与非目标

### 目标

1. **默认快路径**：每个 FT8 时隙默认只用 `ft8_lib` 完成解码，wall 目标从 ~2.2 s 降到 **≤ 0.3 s**。
2. **按需深度**：命中升级判据时，**同一时隙**补跑现有 WSJT-X 路径，保住弱信号与 AP。
3. **能力零回退**：任何情况下解出的条数 **不低于**"始终走深度路径"（是超集，不是近似）。
4. **不迟到**：最坏情况（升级命中）end-to-end 仍 **≤ 4.0 s 决策截止**；预算不足时**宁可不升级**。
5. **可关**：三个判据全关 = 纯快路径档；级联是可选能力，不是强制行为。

### 非目标

- 不替换、不删除现有 WSJT-X 内核；`improved` / `standard` 两条既有路径行为不变。
- 不改 FT8 的音频链路、QSO 序列、TX 门控、日志、UI 主流程。
- 不在本期做"把 ft8_lib 的优化搬进 WSJT-X 内核"（分析文档 §8.6 指出的另一条路线）。
- 不改决策截止本身（本 spec 编写时是 2.5 s；它后来确实被改了 —— 2026-10-13 的
  Phase A 抬到 4.0 s，因为 2.5 s 放不下级联自己的第二条路径，见
  `2026-10-13-ft8-fast-path-recall-port-design.md` §5 A1）。

## 3. 既有集成面（只读勘察的事实，作为设计约束）

| 事实 | 位置 |
|---|---|
| **唯一的 ctypes 边界**（仓库明写的不变量） | `ft8/core/binding.py:1`（"Sole locked ctypes boundary"） |
| ABI 版本与能力掩码 | `binding.py:34` `_ABI_VERSION = 1`、`:36` `_IMPROVED_PROFILE_MASK = 0x1F` |
| 启动期硬校验 ABI | `binding.py:322-335` `_EXPECTED_ABI`、`:356` `_validate_abi` |
| 两条解码路径枚举 | `ft8/core/models.py:29-33` `DecodePath.STANDARD/IMPROVED`，`:45` 默认 `IMPROVED` |
| **AP 上下文是一等公民** | `_DecodeConfig` 带 `my_call/dx_call/dx_grid/ap_width/qso_progress`（`binding.py:78-88`）；结果带 `ap_type`（`:101`） |
| 唯一解码咽喉 | `ft8/engine/dsp_decode.py:85` `SupervisorDecoder.decode()`（shm → 一次 supervisor 往返 → `DecodeBatch`） |
| supervisor 串行化请求、单 worker | `ft8/core/supervisor.py` |
| 截止判定 | `ft8/engine/orchestrator.py:258` `late = finished > boundary + cutoff` |
| C ABI 头 | `ft8/dsp/wsjt_core.h:91` `wsjt_get_abi_info`、`:99` `wsjt_ft8_decode_standard`、`:107` `wsjt_ft8_decode_improved` |
| vendor 子集由 CMake 清单派生 + SHA-256 | `tools/ft8/extract_wsjtx_subset.sh`、`ft8/dsp/vendor.sha256`（AGENTS.md「FT8 铁律」） |
| 测试面 | `tests/ft8/` 共 **419** 用例（core 75 / dsp 43 / engine 242 / web 26 / platform 21 / 顶层 12） |

**关键算术（来自 §8.2 实测，决定级联可行性）**：

- 深度路径 threads1 = **3.23 s** → 快路径 + 它 = 3.38 s，**超 2.5 s 预算**。
- 深度路径 threads3 = **2.22 s** → 快路径(0.15 s) + 它 = **2.37 s < 2.5 s**，余量 ~130 ms。

→ 当时（截止 2.5 s）的结论是「升级时必须用 **threads ≥ 3**，且必须有预算守卫」。
守卫的必要性不变；但 2026-10-13 Phase A 把截止抬到 **4.0 s** 之后，threads1 的
3.38 s 在**窗口**里已能放下 —— 真正把 `deep_threads` 钉死在 3 的理由因此变成了
另一条：预算守卫的 `deep_lower_bound_s`（2.3 s）是在 threads3 上量的，拿它去给
threads1 打分本身就是量错了尺子。见 `cascade.py` 里那段注释。

## 4. 架构

三层，各守一条既有不变量：

```
engine 层  ── CascadingDecoder ──  纯 Python；级联策略在这里（可脱原生代码单测）
                    │  (接口与 SupervisorDecoder 完全一致)
contract 层 ── binding.py ────────  仍是唯一锁定的 ctypes 边界（不变量 #1：唯一边界）
                    │
native 层  ── libwsjt_core ───────  + 新 path `wsjt_ft8_decode_fast`（ft8_lib 编进同一 target）
```

- **不变量 #1（唯一 ctypes 边界）**：不新增第二个 `CDLL`。ft8_lib 的 C 编进现有 `libwsjt_core`，
  由**同一个** `binding.py` 暴露。
- **不变量 #2（平台耦合只在边缘）**：级联策略是**纯 Python**，不进 Fortran/C。
  native 侧只负责"解一个时隙"，不负责"决定用哪条路径"。
- `DecodePath` 增加 `FAST`；`_ABI_VERSION` **递增**；`wsjt_abi_info` 增加 `decode_paths` 能力掩码。
  binding 启动时照 `_EXPECTED_ABI` 既有方式**硬校验**——旧库配新 binding 直接拒绝启动，
  **绝不静默走错路径**。
- 级联策略**不进** `DecodeConfig`：`DecodeConfig` 是原生请求的镜像，级联是 engine 的决定。
  新增 `CascadePolicy` dataclass 住在 engine 层。

## 5. 组件与接口

### 5.1 native：新增 fast path

- **源码来源**：`kgoba/ft8_lib`（钉一个 commit，记进 provenance）。
- **vendor 化**：按本仓既有做法放入 `ft8/dsp/vendor/fast/` 编译子集，
  由 `tools/ft8/` 下的脚本从源清单派生，并产出 **SHA-256 记录**（对齐 `vendor.sha256`）。
  `tests/ft8/dsp/test_vendor_policy.py` 覆盖新子树。
- **构建**：编进现有 CMake target（`ft8/dsp/CMakeLists.txt`），
  `build.sh` 幂等构建不变；新符号进 `wsjt_core.unexports` / 导出映射，
  `test_cmake_export_map.py` 覆盖。
- **C ABI**：新增
  ```c
  int32_t wsjt_ft8_decode_fast(const int16_t *samples,
                               const struct wsjt_decode_config *config,
                               int64_t slot_id,
                               struct wsjt_decode_result *results,
                               int32_t capacity,
                               int32_t *count,
                               int32_t *overflow);
  ```
  语义与 `wsjt_ft8_decode_standard/improved` 一致：写 `results`、置 `count/overflow`、返回 `wsjt_status`。
  输入仍是**同一** 12 kHz int16 时隙、同一个 `wsjt_decode_config`。
- **`wsjt_abi_info` 扩展**：追加 `int32_t decode_paths`（位掩码；bit0=standard、bit1=improved、bit2=fast）。
  结构体尾部追加字段、`struct_size` 随之变化，`_EXPECTED_ABI` 的 `struct_size` 校验天然挡住老库。
- **OpenMP 契约**：ft8_lib 是纯 C、单线程，**不**在栈上分配大工作数组。
  既有 `OMP_STACKSIZE` 契约（`binding.py:9`，AGENTS.md「FT8 铁律」）必须原样保留、
  执行顺序不得改动。

### 5.2 contract：`ft8/core/binding.py`

- `_ABI_VERSION` 递增；`_AbiInfo` 增 `decode_paths`；`_EXPECTED_ABI` 增
  `decode_paths`（固定为期望位掩码）。
- `_CtypesAdapter.__init__` 绑定 `wsjt_ft8_decode_fast` 的 `argtypes/restype`。
- `_CtypesAdapter.decode()` 的路径分派增 `DecodePath.FAST` 分支。
- **向后兼容**：新增字段只追加不改语义；`improved`/`standard` 两条路径的调用逐字节不变。

### 5.3 contract：`ft8/core/models.py`

- `DecodePath` 增 `FAST`。
- `DecodeBatch` 增两个字段：`escalated: bool = False`、`fast_elapsed_seconds: float = 0.0`
  （`elapsed_seconds` 语义改为**两段之和**，保持"这次解码总共花了多久"）。
- `DecodeResult` **不变**（合并后的结果仍用同一结构；`ap_type` 对 fast 结果为 0）。

### 5.4 engine：`ft8/engine/dsp_decode.py` — `CascadingDecoder`

```python
class CascadingDecoder:
    def __init__(
        self,
        inner: SlotDecoder,                 # 生产里就是 SupervisorDecoder
        policy: CascadePolicy,
        *,
        boundary_epoch_for: Callable[[int], float],   # 必填：本时隙结束的 epoch（§8 预算守卫）
        cutoff: float,                      # 必填：决策截止，唯一来源是 orchestrator 的那一个
        clock: Callable[[], float] = time.time,       # 必须是 epoch 秒，见下
        deep_threads: int = 3,              # §3 算术：deep 必须 >= 3 线程
        deep_lower_bound_s: float = 2.3,    # 预算守卫用的实测下界
        safety_margin_s: float = 0.05,
        ap_context: Callable[[], tuple[str, int]] | None = None,   # 判据 (a) 的输入
        requested: Callable[[], bool] | None = None,               # 判据 (c) 的输入
    ) -> None: ...

    async def decode(self, slot_id: int, samples: bytes) -> DecodeBatch: ...
```

- 接口与 `SupervisorDecoder.decode` **完全一致** → engine 无感替换。
- `inner` 与 `clock` 均可注入 → 策略测试**不碰原生库**。
- **实现订正（Task 10）**：`clock` 默认是 **`time.time`**（epoch 秒），不是 `time.monotonic`
  —— `orchestrator.py` 的 `clock` 与 `boundary_epoch` 都是 epoch 秒，混用会让
  `clock() − boundary` 恒为约 −1.7e9、预算守卫的余量永远为正、**在真实运行里永不触发**。
  `boundary_epoch_for` 与 `cutoff` 因此都改成**必填、不给默认值**：`slot * 15.0` 这种
  "默认" 会把单调时钟和时隙号混算，忘了注入的后果是预算判断静默出错而不是报错；
  截止值也不在本模块另设一份默认 —— `orchestrator.py` 的
  `DEFAULT_DECISION_CUTOFF_SECONDS` 是它唯一的来源，两处各自写死只会在有人改了
  orchestrator 之后悄悄分叉（守卫被静默重新标定，且没有任何测试会红）——
  2026-10-13 的 2.5 → 4.0 正是这种改动。

```python
@dataclass(frozen=True, slots=True)
class CascadePolicy:
    escalate_on_ap_context: bool = True    # 判据 (a)
    escalate_on_weak: bool = True          # 判据 (b)
    min_decodes: int = 1                   # K_MIN
    min_peak_snr: int = -26                # S_MIN（由第三轮 A/B 的 -24/-30 崖夹出，见 §13）
    escalate_on_request: bool = False      # 判据 (c) 由引擎按槽置位
```

### 5.5 engine：接线

- `ft8/engine/orchestrator.py`（或现有装配点）构造 `CascadingDecoder(SupervisorDecoder(...), policy)`
  并传给引擎；`improved`/`standard` 的既有装配路径保留。
- 判据 (c) 的"深度解码本槽"开关走 engine 的逐槽状态，不进 `DecodeConfig`。

## 6. 数据流与合并

```
slot ─▶ CascadingDecoder.decode(slot_id, samples)
          │
          ├─ 1. inner.decode(path=FAST)                      → batch_fast     (目标 ≤0.3 s)
          │
          ├─ 2. should_escalate(batch_fast, ap_ctx, remaining_budget)?
          │        否 ─▶ return batch_fast
          │
          └─ 3. 是 ─▶ inner.decode(path=IMPROVED, threads=deep_threads)
                              → batch_deep
                              merge(batch_fast, batch_deep) ─▶ return（escalated=True）
```

**合并规则**（合并后是两批的**并集**，故"能力零回退"）：

1. 以 **`(frequency, dt)` 带容差**（`_FREQ_TOL=5.0` Hz / `_DT_TOL=0.15` s）为键去重；
   **deep 胜出**（同键保留 deep 那条）。**不把 `text` 当键**：快路径把没还原的呼号写成
   `<...>`、深路径还原成真呼号，同一信号的文本本来就会不同，拿文本当键等于把一条信号数两遍
   （Task 9 的 `test_hash_placeholder_is_merged_not_duplicated` 钉住了这一点）。
   **`dt` 的帧约定（2026-10-13 终审修正）**：键里的 `dt` 是 **WSJT-X 帧** —— `dt = 0` 是
   信号的名义起点（时隙内 0.5 s 处）。两条路径都必须先按这个基准上报 `dt`，它们的行才能被
   合并；快路径若照 ft8_lib 的时隙起点上报，`dt` 恒差 0.62…0.68 s、合并键永远配不上
   （`tests/ft8/dsp/test_ft8_fast_decode.py::test_fast_and_deep_dt_agree_on_the_same_slot` 钉住）。
2. `ap_type` 取 deep（fast 恒为 0）。
3. `snr` 取 deep —— 深路径用的是 WSJT-X 那套**标定过的**估计器，而快路径的 SNR 是更粗的
   水声图测量（见下），故同键取深路径那一条；**只有 fast 独有条目才用它的值** ——
   这一句现在才真正安全（见 D-7 修订）。
4. `overflow` 取两批的逻辑或；`slot_id` 取被采纳者的。
5. `path` 取**最终决定结果的那一批**：deep 非空取 `IMPROVED`；
   deep 返回 0 条而 fast 非空时取 `FAST`（并集里全是 fast 的条目）。**任何情况下不丢条目。**

## 7. 升级判据

`should_escalate` 为真当**任一**成立（各自可单独关闭）：

| 判据 | 条件 | 依据 |
|---|---|---|
| **(a) AP 上下文激活** | `my_call` 非空 **且** `qso_progress > 0` | 通联中要 AP，而 fast 路径根本没有 AP —— 这是刚需 |
| **(b) 疑似漏解** | `len(fast) < min_decodes` **或** `peak_snr < min_peak_snr` | 弱信号场，正是 §8.3 实测 fast 掉队处 |
| **(c) 用户显式** | 解码面板「深度解码本槽」一次性开关 | 操作者覆盖 |

- (a) 的效果是**通联中自动退回深度路径**，省下的时间集中在空闲 / 扫频——这是刻意的取舍，见 §12 决策记录 D-3。
- (b) 的两个阈值 `K_MIN`/`S_MIN` 是**可调参数**；`K_MIN=1` 是承重判据，
  `S_MIN=-26` 由第三轮 A/B 的实测崖（-24 完整 / -30 崩塌）夹出，见 §13。

## 8. 时间预算与安全

升级前计算：

```
remaining = cutoff − (now − slot_boundary) − elapsed_fast − safety_margin
```

- 若 `remaining < deep_lower_bound_s` → **不升级**，返回 `batch_fast`，
  标 `escalation_skipped = "budget"`，计一次 `deadline_risk`（**不算 error**）。
- 理由（§3 算术）：deep 在 threads1 要 3.23 s，升级必然迟到；**宁可不升级，也不迟到**。
- `deep_lower_bound_s` 默认 2.3 s，来源为 §8.2 实测的 threads3 = 2.22 s 加上余量；
  它是**配置项**，随硬件/配置可调。
- **截止判定不变**：`orchestrator.py:258` 的 `late` 逻辑照旧生效，
  级联**不**试图绕过它，只是在它之前先做一次自保。

## 9. 错误处理

原则：**快路径坏了绝不少解码**。

| 情形 | 处理 |
|---|---|
| fast 路径失败（非零状态）/ ABI 缺 `decode_paths` 位 | 记日志 + **直接回落 improved**（走既有整条路径） |
| deep 升级失败 | 返回 `batch_fast`（已有结果不丢），记 error |
| 超预算跳过升级 | 不算 error，计 `deadline_risk` |
| `-ffast-math`（或任何编译开关）改变解出条数 | 直接禁用该开关 —— 正确性门，由测试盯（§11） |
| 合并时 `(freq, dt)` 键冲突（带容差，见上 §6 规则 1） | deep 胜出，fast 那条丢弃（不报错） |

## 10. 观测

- `ft8/engine/latency.py` 的 `LatencyHistogram` 增加一个 **path 维度**：
  `record(path, profile, threads, elapsed_seconds)`，桶键 `<path>/p<profile>/t<threads>`，
  其中 `path ∈ {fast, improved}`（`DecodePath` 的两个取值）。`path` 是必需的维度、不是修饰：
  默认的 fast 路径约 0.15 s、深路径约 2.2 s，混在一个桶里会让快路径把深路径的尾部分布整个
  冲淡，而 NFR-002 要回答的恰恰是深路径离决策截止还有多远。**没有 `escalated` 维度，
  也不该有**：直方图唯一的记录点在 `SupervisorDecoder.decode_path` 内部，跑在
  `CascadingDecoder` **决定是否升级之前**，那一行永远不知道本槽最终升没升 —— 真按早稿的
  写法加一个"升级"桶，它会永远空着（"看着有、从不出现"）。**升级率由另一个量回答**：
  下面的 `escalations` 计数，不是直方图维度。
- 新增轻量计数（复用 `orchestrator.counters` 风格）：`fast_decodes`、`escalations`；
  `CascadingDecoder` 另带 `deadline_risk`（超预算跳过升级的次数，由 `runtime.cascade_stats`
  暴露）。**不设 `escalation_skipped_budget`**：它与 `deadline_risk` 在当前实现里每次都是
  同一个事件、同一个自增量，两个名字数一个数只会让读的人以为漏了一个；等真的出现第二种
  跳过原因（例如 deep 路径超时）再拆。注意 `fast_decodes + escalations <= decodes` 是
  **有意的**：快路径整个失败而补跑深路径的批次两者都不算，所以它们是 `decodes` 的互斥子集，
  不是"相加等于 decodes"。
- `escalated` 是 `DecodeBatch` 上的一个**布尔字段**（不是 path 的一种）；`DecodeBatch.elapsed_seconds`
  是快路径 + 深路径之和、`fast_elapsed_seconds` 是升级前的快路径耗时 —— 单槽总耗时与
  "慢在哪一段"由这两个字段回答。
- 与 Cloud Hub 会话打点（`session_metrics.py`）无关，**不打点**任何逐会话内容。

## 11. 测试策略

### 纯 Python（主体，脱原生代码）

`CascadingDecoder` + fake supervisor + 注入 `clock` + 假 batch：

- 判据 (a)/(b)/(c) 各自的触发与不触发边界；
- 预算守卫：剩余不足时**不升级**且返回 `batch_fast`；
- 合并去重：同键 deep 胜出、`snr/ap_type` 取值正确、deep 空而 fast 非空时不丢；
- 容错：fast 失败回落、deep 失败返回 fast；
- 三个判据全关 = 纯 fast（不调用 deep）。

### 原生

- ABI smoke：`wsjt_ft8_decode_fast` 存在、`decode_paths` 掩码正确、旧版本被拒。
- **一致性**：同输入下 `FAST` path 直连 vs `decode_ft8` 参考二进制 → **同条数**。
- 构建映射：`test_cmake_export_map.py` 覆盖新符号；`test_vendor_policy.py` 覆盖新 vendor 子树。

### 端到端

- 扩 I9 矩阵：断言 fast 档 headline ≤ 截止。
- 构造触发升级的弱信号（复用分析文档 §8.3 的合成方法），
  断言**级联最坏 case** end-to-end ≤ 4.0 s，并断言**解出条数 ≥ 始终走 deep**（零回退）。

### 回归

- `tests/ft8/` 现有 **419** 用例全绿；
- `improved` / `standard` 两条既有路径行为不变（同输入同结果）。

## 12. 决策记录

- **D-1 分层混合而非全量替换**：全量替换会永久失去弱信号（实测 −30 档 3/5 → 0/5）
  与 AP（未测，真实损失更大），与 FT8 的存在理由相反。
- **D-2 单一库 + 新增 path（方案 A）**：唯一同时满足仓内两条已写死的架构不变量
  （唯一 ctypes 边界、平台耦合只在边缘）。方案 B（两个 CDLL）与 C（策略进 Fortran）各打破其一。
- **D-3 自适应级联；通联中无条件升级**：升级时两段**叠加**——
  不升级 0.15 s，升级最坏 0.15 + 2.22 = **2.37 s**（当时在 2.5 s 内，余量 ~130 ms；
  截止 2026-10-13 抬到 4.0 s 后余量 ~1.63 s）。
  通联时优先保 AP（判据 a），把时间收益让给空闲 / 扫频场景。
  这是**刻意的**取舍：选它而非"上下文二选一"，是为了不依赖"能否事先判断本槽弱不弱"，
  代价就是最坏 case 余量很薄，所以 §8 的预算守卫是**必需**而非可选。
- **D-4 深度路径必须 threads ≥ 3**：当时的理由是 threads1 的 3.23 s 超 2.5 s 预算
  （§3 算术）。截止抬到 4.0 s 后这条算术不再成立，但结论保留，理由是预算守卫本身：
  `deep_lower_bound_s` 只在 threads3 上量过，让它去给别的线程数打分是量错了尺子。
- **D-5 预算不足宁可不升级**：截止是硬约束；"升级反而迟到"是净损失。
- **D-6 合并取并集、deep 胜出**：保证解出条数不低于纯 deep 基线（目标 3）。
- **D-7 fast 的 SNR 不采用**：上游是 TODO 估计（`snr = score * 0.5f`），不可信；
  只有 fast 独有条目才用它的值。
  **2026-10-13 修订（现场缺陷 B）**：这个判断认错了量的性质 —— 那个估计不是"差一点的
  dB"，而是**饱和**（合成扫频实测：≥ −6 dB 的整个区间压在 17 不动，见
  `ft8/dsp/wsjt_fast.c` 的注释）。而且它不只是显示问题：orchestrator 把 snr 喂给
  sequencer，sequencer 把它编进**实际发射的报文**（`sequencer.py:180/234/287`），
  所以发到空中、写进 QSO 日志的"收你多少 dB"曾是一个饱和的同步分。修法是快路径直接
  从 ft8_lib 的水声图（本身就是功率 dB）在解出的 79 个音位置上量信噪比；标定工具
  `tools/ft8/calibrate_fast_snr.py`，在 −18…+24 dB 扫频上与 standard 路径**逐点 0 dB
  残差**。因此 fast 结果**不再置** `WSJT_FLAG_SNR_PROVISIONAL`，峰值判据
  `min_peak_snr = -26` 对 fast 批次重新生效 —— 正是 `cascade.py:_snr_is_trustworthy`
  里"未来某天快路径算出真 SNR 就把这一条重新打开"那句注释描述的情形；位与机制保留给
  任何未标定的 SNR 来源。原 D-7 的**合并**规则（第 3 条，同键 deep 胜出）不变。

## 13. 风险与开放问题

- **已解决 O-1（原暂定 -20 → 定为 -26）**：`S_MIN`。第三轮 A/B 已经把 fast 的崖夹出来了
  ——0…-24 dB 与深路径同为 5/5，-30 dB 崩成 0/5。阈值设在 -24 只会对着一个
  「实测已知解码完整」的时隙每次白烧 2.2 s，所以取 **-26**（崖上方一点）。
  注意：`min_decodes=1` 才是**承重**判据（fast 返回 0 条是明确信号，-30 dB 那档
  已被它单独覆盖）；`S_MIN` 是"宁可信其有"的补充。真实场次数据回来后可再收紧。
- **开放问题 O-2**：`deep_lower_bound_s` 默认 2.3 s 是**实测 + 余量**的估计；
  不同 SD 卡 / 负载下会漂移，首期先保守，靠 §10 的升级率观测校准。
  **状态**：实现已按 2.3 s 出厂（`cascade.py` 的 `DEFAULT_DEEP_LOWER_BOUND_SECONDS`），
  仍是配置项；真实场次数据回来前不改它。
- **结论（2026-10-13 A53 spike）—— ft8_lib 本机深度优化空间很小，且大多不可用**：

  | 杠杆 | 实测/估算 | 判定 |
  |---|---|---|
  | `-ffast-math` | 153→149 ms（−2.6%） | **不采用**：为 4 ms 放松解码器 FP 语义不值 |
  | `-mcpu=cortex-a53 -mtune=cortex-a53` | 153→**162 ms（负收益）** | 禁用：objdump 显示向量化完全相同，慢在 A53 顺序调度 |
  | `-march=armv8-a+simd` | 152 ms（中性） | 无用：aarch64 早已默认 NEON |
  | 同步搜索手工 SIMD | `ft8/decode.c` 在任何开关下都是标量，0 条整数 SIMD 指令 | 需自己写 intrinsics，超出本设计范围 |
  | 并行 LDPC 相位（占 50.7%） | ~99 ms（1.55×，4 线程，**Amdahl 估算**） | 不值得：快路径 0.15 s 已在预算内 |
  | 降 `kLDPC_iterations` / 升 `kMin_score` | 25→12 省 24%；10→20 省 45% | **不采用**：全部拿弱信号鲁棒性换时间，而弱信号正是级联要兜的那段 |

  **结论**：快路径就以上游默认参数的 ft8_lib 定稿（实测中位 153 ms）；
  「深入优化」的答案是否定的——省下的毫秒数买不回任何弱信号解码，
  而级联已经把 15× 的墙钟收益拿到了。

- **风险 R-1**：升级率若长期偏高（比如弱信号是常态），级联的净收益会缩水到"只省了 fast 那 0.15 s"。
  → 由 §10 观测定量，阈值可调；必要时对 (b) 收紧。
- **风险 R-2**：ft8_lib 与 WSJT-X 对**同一信号**可能给出**不同文本**（解析歧义），
  去重键 `(freq, dt, text)` 会把它们当成两条 → 显示重复。
  → 需在 P2 用一个真实场次样本验证；必要时对 `freq/dt` 加容差。
- **风险 R-3**：三平台构建（macOS / Windows MinGW / aarch64）都要编过 ft8_lib 的 C；
  Windows 上需注意 MinGW 运行时 DLL 打包（对齐现有 `binding.py:187-191` 的做法）。
- **风险 R-4**：`decode_paths` 追加进 `wsjt_abi_info` 会改变 `struct_size`，
  任何**外部**按旧结构体读写该 info 的消费者会失败——仓库内只有 `binding.py` 一个消费者，已确认。

## 14. 文档同步（Phase 5，随实现逐期完成）

- `docs/ft8-w103d-decode-perf-analysis.md`：回填 spike 数字与 final S_MIN。
- SDD：`13-*`（性能/架构）加一条决策；`09-architecture-overview.md` 的解码路径描述更新。
- `docs/PROJECT_MAP.md`、`docs/DOCUMENT_INDEX.md`：登记本文件。
- `AGENTS.md`：`ft8/` 结构表增补 fast path 与 `CascadingDecoder`。
- `tests/README.md`：用例/模块计数按实际更新。
- commit 前 `python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged` 必须干净。
