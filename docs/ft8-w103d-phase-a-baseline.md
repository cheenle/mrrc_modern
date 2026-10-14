# FT8 Phase A 基线 —— W103D 现场读数（2026-10-14 起）

> 这是 Task 8 的产出（计划 `docs/superpowers/plans/2026-10-13-ft8-phase-a-unblock-deep-path.md`），
> 也是决定 **Phase B（增量移植 ft8_lib）投入产出比**的依据（spec
> `2026-10-13-ft8-fast-path-recall-port-design.md` §13 O-1）。
>
> **状态：测量那一半已完成，现场那一半待补。** 每一节都标明自己的状态；
> 没有数字的地方写"未测"，**不写推测**（这是计划 Task 8 Step 4 的明确要求）。

## 1. 采集窗口与版本

| 项 | 值 |
| --- | --- |
| 安装时刻 | 2026-10-14 12:04:06 CST（盒子本机时钟） |
| 安装方式 | `MRRC_UPDATE_SRC=<本地目录> mrrc-update`，源为开发工作树（`feat/ft8`） |
| 升级前版本 | `1.25.5`（回滚备份 `/opt/mrrc_modern.bak-1.25.5-20261014-0023`） |
| 升级后 `version.txt` | **仍然是 1.25.5** —— 见下 |
| 生效判据 | 不用 `version.txt`：`DEFAULT_DECISION_CUTOFF_SECONDS = 4.0`、`_COUNTERS_EVERY_SLOTS = 20`、`protocol.py` 与 `worker.py` 各含 `cpu_seconds` —— 四条在**装好的树上**逐条 grep 通过 |
| 服务 | 升级后 `active`；矩阵测量期间停约 1–3 min，由 `trap` 拉起，收尾 `active` |

**版本字符串没有跟着走（照实记）。** `version.txt` 是**构建产物**，不在仓库里
（由 `packaging/*/build-*.sh` 生成），而 `mrrc-update` 从不碰它
（`linux/mrrc_update.sh` 里没有这个文件名）。所以用 `MRRC_UPDATE_SRC` 指向源码树做的这次
升级，代码是新的、`version.txt` 还是 1.25.5 —— 支持包
（`support_bundle.detect_version()` 的第一顺位就是 `version.txt`）在这个盒子上也会一直报
1.25.5，直到下一次镜像构建。这不是本次引入的缺陷，但它决定了**新版判据不能依赖
`version.txt`**：Task 7 的验收因此改用内容 grep。

**QSO 库未受影响**（顺带修掉的静默数据丢失风险）：`/opt/mrrc_modern/ft8/qso.db`
升级前后指纹逐字节一致 —— `size=32768 mtime=1791913065 audit_event=54`。
`stage-tree.sh` 现已排除 `qso.db`（`6d1647a`），部署脚本另有一道防线
（暂存树里出现 `ft8/qso.db` 即拒绝安装）。

## 2. 深路径的真实成本（已完成）

A53 全矩阵，停服务测量，profile 3，cycles 1，每格 1 预热 + 3 测量，每格 `known 5/5`：

| threads | wall 均值 s | CPU 均值 s | CPU/native |
| --- | --- | --- | --- |
| 1 | 3.131 | 3.127 | 1.00× |
| 2 | 2.450 | 4.118 | 1.68× |
| **3** | **2.138** | **5.284** | **2.47×** |
| 4 | 2.417 | 6.455 | 2.67× |
| 6 | 2.539 | 8.141 | 3.21× |
| 8 | 3.122 | 10.209 | 3.27× |
| 12 | 4.134 | 14.260 | 3.45× |

生产配置就是 3 线程（日志 `FT8 engine started (profile 3, threads 3)`），
而它**正是曲线的最低点**。分析与复现命令见
`docs/ft8-w103d-decode-perf-analysis.md` 第五轮。

**一处对账 —— spec 的 6.71 core-s 应视为被取代。** spec
`2026-10-13-ft8-fast-path-recall-port-design.md:21` 记的深路径成本是 **6.71 core-s**
（threads=3）；本次在 **Worker 进程内**量到的是 **5.284 core-s**，低 27%。
新数更可信，理由与 `SDD/09` §9.9 记录 `cpu_seconds` 的动机相同：老数只能从**外面**量
（父进程 RUSAGE / `ps`），而父进程的 CPU 里混着音频与频谱线程；`decode_ok.cpu_seconds`
是**只属于这一次解码**的进程 CPU。本节据此按 5.284 计算，spec 正文留待 Phase B 设计时
一并更新（见 §5 第 4 条）。

## 3. 升级率 —— 决定 Phase B 去留的数字（待补）

**修复前（已知事实）**：2.5 s 的截止算术错误让深路径完全空转 —— 一个 ~2.5 h 现场窗口里
**263 次拒绝升级 / 0 次成功**（spec §1 事实一）。到本次升级前，日志里累计
**1417 次 `budget too tight`**。也就是说升级率在修复前**恒等于 0**，
"深路径到底多解了几条"这件事在生产上从未被回答过。

**修复后**：Task 7b 的计数器行（`decode counters after N slot(s): …`，
每 `_COUNTERS_EVERY_SLOTS = 20` 槽一行）**已经在盒子上产出**。
读法是操作员在盒子上执行一条 grep：

```bash
sudo grep "decode counters after" /opt/mrrc_modern/logs/server-stdout.log | tail -6
```

拿到后填在这里：**升级率 = `escalations` / `slots`**（行内自带两个计数）。

> **采集方式与计划不同（Ruling）**：计划 Task 8 Step 1 的
> `dev_tools/ft8_baseline.sh` 是一个 3 天 / 15 分钟一次的轮询环，需要在盒子上放一份
> **有效会话 cookie**（`/opt/mrrc_modern/data/baseline-cookie`）才能打自己的 API。
> Task 7b 之后这条路不再必要：同一批计数（`slots`/`decodes`/`escalations`/
> `deadline_misses`/`decode_errors`）已经由引擎自己**定期写进应用日志**，
> 读它不需要任何凭据、不需要在盒子上常驻进程、也不需要在 URL 里放令牌（AD-024）。
> 代价：**分辨率从 15 分钟降到 20 槽（5 分钟）**，且拿不到 `worker_core_s` 的独立采样
> —— 后者由 §4 的推算替代。因此本报告不引入 cookie，也不新增常驻脚本。
> 若 3 天后仍需更高分辨率的曲线，再按计划步骤建那个环。

## 4. 每槽 core-s 与 CPU 余量（已可推算）

快路径 ~0.1 core-s/槽（spec §1 事实二）；深路径 5.284 core-s/次（§2）。

- 单次深路径就占 `DEFAULT_DECISION_CUTOFF_SECONDS = 4.0` 的 **53%**。
- **极端假设"每槽都升级"**：`0.1 + 5.284 = 5.384` core-s/槽；
  一个 15 s 时隙 × 4 核 = 60 core-s，即 **~9%**。
  （按 spec 的 6.71 算是 11.3% —— 方向的结论不变，量级修正为 9%。）
- 也就是说：**即使每一槽都跑满深路径，这台盒子仍有约 9 成的 CPU 是空的。**
  "CPU 闲置"不是修辞，是这次测量给出的余量；Phase B 要花的正是这笔余量。
- `cycles=3` 在 A53 上约 **6.5 s**（`2.138 × 3.03`），**超过 4.0 s 截止**，
  而它在开发机上只要 0.630 s。**A53 上买灵敏度不能靠加 cycles** —— 只能从这 2.138 s 里省。

## 5. 结论

1. **深路径此前在生产上从未运行过**；本次升级按 spec §5 A1 的算术把它打开了
   （截止 2.5 → 4.0 s：`cutoff − FAST_PATH_FINISH_DELAY_SECONDS(0.48) − safety_margin(0.05)
   = 3.47 ≥ DEFAULT_DEEP_LOWER_BOUND_SECONDS(2.3)`，spec:120-121）。
2. **深路径一次 2.138 s wall / 5.284 core-s**，占决策截止的 53%；生产线程数已是最优点。
3. **CPU 余量约 9 成**（每槽都升级的极端情况下），这就是 Phase B 的预算。
4. **深路径相对快路径多解了多少条报文：未测。** 这需要人工标注的音频，
   也就是 Phase B 的召回工作台。**这里不填推测值。** 在它被测出来之前，
   Phase B 的优先级只能按"spec §1 事实三那个 ~6 dB 召回缺口"论证，
   不能按"多解了 N 条"论证 —— 那个 N 目前**不存在**。
5. **待补两件读数**（都需要盒子上的读权限，见 §3 的采集方式）：
   `decode counters after` 的最近几行（升级率），以及 3 天窗口后的同一批行（趋势）。
