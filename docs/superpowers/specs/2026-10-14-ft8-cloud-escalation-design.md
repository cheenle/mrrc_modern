# FT8 云端深路径（cloud escalation）设计

- **日期**：2026-10-14
- **状态**：设计已确认（A1 单机通道），待实现
- **范围**：**A1 —— 单台盒子 × 单个 hub**（W103D ↔ hub.vlsc.net）。接口留 seam，**不做多租户**；
  召回增益量出来之后再决定是否推广到阵列。
- **依据（本设计全部数字都是实测，来源见下）**：
  - `docs/ft8-w103d-decode-perf-analysis.md` 第五轮（A53 全矩阵）与 §8.2（CPU 读数）
  - 2026-10-14 的 hub 现场测量（本文件 §1.2 表；`tools/ft8/i9_thread_matrix.py` 在 hub 上跑出的原始输出）
- **相关代码**：`ft8/engine/cascade.py`、`ft8/engine/dsp_decode.py`、`ft8/core/supervisor.py`、
  `ft8/core/worker.py`、`ft8/core/protocol.py`、`ft8/core/models.py`、`ft8/platform/`（新模块）、
  `server.py`（接线与配置读取）
- **相关文档**：SDD `09-architecture-overview.md`、`12-operational-model.md`、`14-version-history.md`；
  hub 仓 `mrrc_hub` 的 `deploy/README.md` 与 SDD

---

## 1. 背景与动机

### 1.1 盒子上的墙：不是 CPU，是串行深度

W103D（4×Cortex-A53 @1.8 GHz，profile 3，cycles 1，实测）：

| threads | 1 | 2 | **3（生产）** | 4 | 12 |
| --- | --- | --- | --- | --- | --- |
| wall 均值 s | **3.131** | 2.450 | **2.138** | 2.417 | 4.134 |
| CPU 均值 s | 3.127 | 4.118 | **5.284** | 6.455 | 14.260 |

决策截止 `DEFAULT_DECISION_CUTOFF_SECONDS = 4.0`，而单次深路径在最优点就是 **2.138 s（53%）**。
`cycles` 1→3 的 ~3× 落在这里是 **~6.5 s > 4.0 s** —— **A53 上 `cycles=3` 不可负担**。
加线程买不到时间（sync 阶段每线程复制，§8.2 / 第五轮）：第 4 个线程是 **+13% wall / +22% CPU**。
盒子同时长期 ~90% 空闲：**缺的不是算力，是"单核串行段的速度"**。

### 1.2 hub 上有什么（2026-10-14 实测）

| | cycles=1 | cycles=2 | **cycles=3** |
| --- | --- | --- | --- |
| hub t1（EPYC-Milan，1 vCPU） | **0.476 s** | 0.904 s | **1.208 s** |
| 同格 known | 5/5 | 5/5 | 5/5 |
| hub t2 / t3（对照） | 0.583 / 0.644 s —— 1 vCPU 上超订，**必须单线程跑** |

- hub 单核 = **6.6× 单颗 A53**（3.131 / 0.476），比盒子生产配置（t3 = 2.138 s）快 **4.5×**；
- 网络（实测）：盒子 → hub **RTT 90 ms ± 12**；上行 **16 MB / 10.33 s ≈ 12.4 Mbps**；
  单槽负载 360,000 B ⇒ **≈0.25 s**；
- 硬件：**1 vCPU / 1.9 GB / 磁盘 14 GB 已用 12 GB（剩 ~1.5 GB）**；
  常驻 nginx（全阵列 443 入口）+ frps + portal + 路由定时器。工具链与 `libwsjt_core.so` 已在位。

### 1.3 结论：买"深度"，不是买 CPU

按需上传的往返（cycles=3）：升级判定 ~+0.5 s → 上传 ~0.25 s → 解码 **1.208 s** → 回传 ~0.15 s
≈ **+2.1 s**，距 4.0 s 截止仍有 ~2× 余量。于是**盒子买不起的 `cycles=3` 变成可负担**，
而这正是提高弱信号召回率的已知旋钮。顺带的第二个收益：云端是**改解码器不需要重跑四平台原生库**的
试验场（本设计自身不改 `ft8/dsp/`，但未来的深路径改动可以先在这里量）。

## 2. 目标与非目标

### 目标

1. 升级（escalation）可以在 hub 上以 **cycles=3** 执行；合并、序列化、`late` 规则**一字不变**。
2. **云端不可用时的行为与"开关关闭"逐字一致**（断网 / 服务停 / 证书错 / 超时）。
3. **绝不迟到**：不存在"越过 `boundary + 4.0 s` 的云端深路径结果"这一状态。
4. 可观测：`cloud_ok` / `cloud_fallback` / `cloud_seconds` 进 `cascade_stats` 与日志。
5. 交付**召回测量装置**：同一批真实时隙跑 ①本地 t3 c1 ②hub t1 c1 ③hub t1 c3，输出消息集合差集。

### 非目标

- 不做多租户 / 配额 / 公平调度（A1；seam 留在 token 校验与无全局状态上）。
- 不为阵列做 onboarding（这台盒子当前**根本没接 hub**：无 frpc 单元、env 里无 `MRRC_CLOUD_*`）。
- 不改本地深路径、不改快路径、**不动 `ft8/dsp/`**（因此不触发四平台原生重编）。
- 不做常驻音频流（按需上传；隐私姿态与常态带宽都不变）。
- 不做 UI（云端来源的可视化标识留待后续）。

## 3. 既有集成面（只读勘察的事实，作为设计约束）

- **级联接缝** `CascadingDecoder.decode()`（`ft8/engine/cascade.py:208`）：
  fast → `should_escalate()` → 预算守卫（`_remaining_budget < _deep_lower_bound_s` ⇒
  `deadline_risk += 1; return batch_fast`）→ `inner.decode_path(..., IMPROVED, threads=deep_threads)`
  → `merge_batches(batch_fast, batch_deep)`。
- **解码器协议** `SupervisorDecoder.decode_path()`（`ft8/engine/dsp_decode.py:94`）：
  写共享内存 → 组装 `decode` 帧 → `supervisor.request()` → 解析 `decode_ok` → `DecodeBatch`。
- **帧 schema**（`ft8/core/protocol.py`，**严格精确 schema**，多一个少一个字段都拒）：
  - `decode` = `{type, slot_id, deadline_monotonic, shm{name,dtype,shape,nbytes}, config{21 字段}}`
  - `decode_ok` = `{type, slot_id, path, results[{slot_id,sync,snr,dt,frequency,text,ap_type,quality,flags}],
    overflow, elapsed_seconds, cpu_seconds, deadline_missed}`
- **时隙负载**：`samples: bytes`，恒为 180000×int16 = `SLOT_SAMPLES_NBYTES`（360000）。
- **AP 上下文在 config 里**（`my_call`/`dx_call`/`dx_grid`/`qso_progress`/`ap_width`/`utc_hhmmss`）——
  云端要用 AP 解码，就必须连它一起传（否则云端只能用非 AP 路径）。
- **worker 与 `WorkerSupervisor` 是平台无关的 Python**：hub 可以直接复用同一份源码树，
  因此云端解码与本地深路径**由构造一致**（同一个 `.so`、同一个 worker、同一套帧校验）。

## 4. 架构

一句话：**云端 = 同一个解码调用的远程执行**。

```
盒子                                                          hub
CascadingDecoder.decode()
 ├─ 快路径（本地）                                            
 ├─ should_escalate()        ← 策略不变                        
 ├─ 【新】CloudDecoder.decode_path()  ──WSS(443)──►  mrrc-decode 服务
 │        成功 → merge_batches(fast, deep)             └─ WorkerSupervisor（同一份源码）
 │        失败/超时/断网 → 往下                          └─ worker.py（同一个 .so）
 └─ 现状：预算守卫 → 本地深路径（一字不动，作为回退）
```

- 盒子侧新模块：`ft8/platform/cloud_decode.py` —— `CloudDecoder` 实现与 `SupervisorDecoder`
  **同一个 `decode_path()` 协议**，可直插 `CascadingDecoder(inner=…, remote=…)`。
- hub 侧新服务：`mrrc-decode` —— WS 端点 + 一个常驻 `WorkerSupervisor`（复用盒子那份 `ft8/core/`）。
- 传输：**WSS 复用现有 nginx 443**（WebSocket 升级头，`deploy_listen_proxy.sh` 里已有现成写法）。
  不新增安全组规则、不新增证书、不做证书 pin；盒子侧用产品已有的 `websockets` 依赖。

## 5. 组件与接口

### 5.1 协议（`ft8/core/protocol.py` 新增一个帧类型，双方共用）

- **`decode_remote`**（文本帧）：`decode` 去掉 `shm`、加 `pcm_bytes`：
  `{type, slot_id, deadline_monotonic, config, pcm_bytes}`。
  紧随其后**一条二进制帧**，长度必须恰好等于 `pcm_bytes`（360000）。
  校验沿用现有 `_exact_fields` 风格（缺/多字段、`pcm_bytes` 不符、超长都拒）。
- **响应复用 `decode_ok`**（已存在、已有严格校验）—— 这样"云端批次"与"本地批次"
  在盒子上走**同一条解析路径**。
- `deadline_monotonic` 仍随帧发送，但**跨机无意义**：hub 不解释它（保留只为复用 schema），
  由盒子侧用 `asyncio.wait_for` 自己兜底；这一点写进代码注释与测试。
- **认证**：WS 握手 `Authorization: Bearer <token>`（AD-024：令牌不进 URL）。

### 5.2 盒子侧 `CloudDecoder`（`ft8/platform/cloud_decode.py`）

- `decode_path(slot_id, samples, path, *, threads=None) -> DecodeBatch`：与 `SupervisorDecoder`
  **同签名、同返回**。
- **云端请求固定 `threads=1`**（`CLOUD_THREADS = 1`，具名常量 + 注释引用实测）：hub 是 1 vCPU，
  t2/t3 反而变慢（0.583 / 0.644 s vs t1 0.476 s）。这是本模块唯一对 config 的覆盖，
  与 `cycles`（来自 `MRRC_FT8_CLOUD_DECODE_CYCLES`）并列，**本地路径的 `threads=deep_threads` 不受影响**。
  hub 服务**再兜底一次** `min(threads, os.cpu_count())`（防御性，不依赖盒子端自觉）。
- 连接：惰性建连 + keep-alive + 断线指数退避重连（1 s → 30 s 上限）。
- **帧组装与结果解析必须与 `SupervisorDecoder` 共用同一份代码**：把 `dsp_decode.py` 里
  「`DecodeConfig` → config dict → 帧」与「`decode_ok` → `DecodeBatch`」两段抽成共享函数，
  两处调用。这是"两条路不会分叉"的机制保证（不是靠约定）。
- 失败一律抛 `CloudUnavailable`（新异常），由级联接住并落回本地。

### 5.3 级联接线（`ft8/engine/cascade.py`）

```python
if not should_escalate(...):
    return batch_fast

if self._remote is not None:                       # 默认 None = 今天的行为
    try:
        batch_deep = await asyncio.wait_for(
            self._remote.decode_path(slot_id, samples, DecodePath.IMPROVED),
            timeout=self._cloud_deadline_s,
        )
        self.cloud_ok += 1
        return merge_batches(batch_fast, batch_deep)
    except (TimeoutError, CloudUnavailable, DecodeError):
        self.cloud_fallback += 1                   # 然后走下面原路

# ── 以下一字不动（预算守卫 + 本地深路径）──
if self._remaining_budget(slot_id) < self._deep_lower_bound_s:
    self.deadline_risk += 1
    return batch_fast
```

- 新增构造参数全部可选（`remote=None`、`cloud_deadline_s=2.5`），不传即今天的行为。
- **云端不走预算守卫**（它的预算由 deadline 表达）；云端失败后**照常**走本地守卫 ——
  那时"预算太紧"是正确判断，只计一次 `deadline_risk`。

### 5.4 hub 服务 `mrrc-decode`

- `websockets.serve(...)`，绑 **127.0.0.1:8990**；先校验 token 再收数据。
- 部署路径：`/opt/mrrc-decode/`（同一份 `ft8/` 子树 + 已编译的 `ft8/dsp/build/libwsjt_core.so`）；
  令牌在 `/etc/mrrc-decode/token`（0600），systemd `EnvironmentFile` 注入。
- 常驻 `WorkerSupervisor` + `SupervisorDecoder`（**复用盒子那份代码**，含共享内存）。
- **串行执行**：一把锁 + 固定 `request_timeout`（10 s）；**忙时立即回 `error{code:"busy"}` 不排队**
  —— 盒子有本地兜底，排队只会把"迟到"变成常态。
- 日志：每请求一行（slot_id / cycles / 解码 ms / 结果数 / busy|error）。**PCM 不落盘**。
- systemd：`CPUQuota=70%`、`Nice=10`、`Restart=always`、专用用户。
- nginx：`location /mrrc-decode`，升级头 + `proxy_buffering off` + 覆盖解码时长的 `proxy_read_timeout`。

### 5.5 配置（全部默认关闭）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MRRC_FT8_CLOUD_DECODE_URL` | 空 | `wss://…/mrrc-decode`；空 = 完全关闭 |
| `MRRC_FT8_CLOUD_DECODE_TOKEN` | 空 | Bearer 令牌（A1 单箱单令牌） |
| `MRRC_FT8_CLOUD_DECODE_CYCLES` | **3** | 云端深度（本地不动，仍按 settings） |
| `MRRC_FT8_CLOUD_DECODE_DEADLINE_S` | **2.5** | 自升级判定起算的要回时间 |

## 6. 数据流与合并

- 云端批次就是"deep 批次"：合并规则不变（`merge_batches`，(freq, dt) 容差内 deep 胜出）。
- 差异只在 config 里（云端 `cycles`），因此 §11 的"同帧"测试可以逐字段比对。

## 7. 判据与阈值

- **什么时候走云端**：与现有 `should_escalate` **完全同一判据** —— 云端是"升级的执行方式"，
  不引入第二套策略（换地方不换判据）。
- **深度**：云端固定 `cycles=3`（可配）。依据：本地买不起（~6.5 s > 4.0 s），云端 1.208 s。
- **截止**：`2.5 s`（自升级判定）⇒ 最迟 +3.0 s 出结果，仍在 4.0 s 内。

## 8. 时间预算与不变量

- **I1**：云端批次要么在 deadline 内到达并被合并，要么**不存在**；不允许"迟到的深路径"。
- **I2**：云端失败路径的额外开销 ≤ 一个 deadline，且当次时隙 `deadline_risk` 只加一次。
- **I3**：不新增发射权限面 —— 云端结果与本地深路径走同一个序列器入口、同一条 `late` 规则。

## 9. 错误处理

| 情形 | 行为 |
| --- | --- |
| 连接失败（DNS/TCP/TLS/证书） | `CloudUnavailable` → 立即落回本地 |
| hub 忙（`error{busy}`） | 立即落回本地（不退避、不重试） |
| hub 崩/重启 | 连接断开 → 退避重连（1 s→30 s）；期间升级走本地 |
| 发出后无响应 | `asyncio.wait_for` 兜底 → 落回本地 |
| 响应 schema 违约 | `FrameError` → 计 `cloud_error` → 落回本地 |

## 10. 观测

- 盒子：`runtime.cascade_stats` 增 `cloud_ok` / `cloud_fallback` / `cloud_seconds`（最近一次云端
  墙上耗时，含网络），与 7b 的日志行同源；每槽行的路径标记允许出现 `cloud`。
- hub：每请求一行日志；`busy` 与 `error` 分开计数。
- 口径：`cloud_seconds` 是**墙上时间**（含网络），与 `cpu_seconds`（Worker 进程 CPU）区分。

## 11. 测试策略

- **协议（纯 Python）**：`decode_remote` 精确 schema —— 缺/多字段、`pcm_bytes` 与随后的二进制帧
  不符、超长、负值；沿用现有参数化风格。
- **`CloudDecoder`（假 WS 服务器，本地起）**：成功 / 超时 / 断连 / 坏帧 / busy；
  **同帧测试**：同一 `DecodeConfig` 下，`SupervisorDecoder` 与 `CloudDecoder` 产出的
  `decode` 与 `decode_remote` 帧除 `shm`/`pcm_bytes` 外**逐字段相等**。
- **级联**：云端成功 → 合并；云端超时 → 落回本地且 `deadline_risk` 只加一次；
  `remote=None` 时行为与今天**逐字一致**（回归）。
- **端到端（真机 + hub）**：一次真实升级跑通；断网/停服务演练（行为必须与关闭开关一致）。
- **不做**：不改 `ft8/dsp/`（本设计只改 Python + 新增 hub 服务）。

## 12. A1 验收：召回测量

- **装置**：`tools/ft8/cloud_recall_rig.py`（读一目录的 `.slot` 文件，跑指定解码配置，
  输出消息集合差集 / SNR 分布 / 耗时）。
- **素材**：真实时隙采集。给 `ft8/platform/rx_tap.py` 加一个**默认关闭**的落盘开关
  （`MRRC_FT8_SLOT_DUMP_DIR`；每槽 360 KB，只在设置时写；写明磁盘代价）。
- **三组**：① 本地 t3 c1（今天基线）② hub t1 c1（隔离"地点"）③ hub t1 c3（隔离"深度"）。
- **成功判据**：③ 相对 ① 的净增解码条数 > 0 且可复现（同一批时隙多次运行结果稳定）。
- **反例判据（退出条件）**：若 ③ 与 ① 无差异 ⇒ **深度不是召回瓶颈**，
  停止推进云端、回到 `ft8/dsp/` 第 2 梯队（有明确止损线）。

## 13. 决策记录

| # | 决策 | 理由 |
| --- | --- | --- |
| D-1 | 云端只执行"升级"，不改判据 | 策略已经过现场验收（4.0 s 截止 + `late=0`），换地方不换判据 |
| D-2 | 复用 `worker.py` + `WorkerSupervisor` + 帧协议 | 语义由构造一致；崩溃隔离；不需要新 ABI、不需要改 DSP |
| D-3 | WSS 走现有 nginx 443 | 无安全组改动、用现有证书、复用产品依赖 `websockets`、令牌走 header（AD-024） |
| D-4 | 按需上传而非常驻流 | 隐私姿态与常态带宽不变；常态零开销 |
| D-5 | 忙时立即回绝而非排队 | 盒子有本地兜底；排队会把"迟到"变成常态 |
| D-6 | 云端固定 `cycles=3` 起步 | 实测 1.208 s 买得起；本地 ~6.5 s 买不起 |
| D-7 | 不做多租户（A1） | 先量增益再付复杂度；seam 留在 token 校验与无全局状态 |
| D-8 | 云端固定 `threads=1`（不传 `deep_threads`） | hub 是 1 vCPU：t1 0.476 s，t2/t3 反而 0.583/0.644 s；盒子端的线程数是给 4×A53 选的 |

## 14. 风险与开放问题

| # | 风险 / 问题 | 缓解 |
| --- | --- | --- |
| R-1 | hub 是**全阵列公共入口**，解码抢 CPU | `Nice` + `CPUQuota=70%` + 串行；验收项 = 解码时 nginx 延迟仍可接受 |
| R-2 | hub 磁盘 1.5 GB 可用（89% 满） | 服务本身 <1 MB；清理/扩容是独立运维项（O-1） |
| R-3 | 信任：云端结果影响发射内容 | hub 已在信任链上（TLS 终结、可见会话 cookie），本设计**不新增能力面**；但要在 SDD 写明 |
| R-4 | 盒子走 Wi-Fi（`wlan0`），抖动/丢包 | 退避重连 + 本地兜底；验收项 = 长跑稳定性 |
| R-5 | 召回增益可能为零 | §12 的反例判据（明确的退出条件） |
| O-1 | hub 上 ~700 MB 工具链是否保留 | 若 R-5 成立则删除 |
| O-2 | 云端是否也承担 `fast`/`standard` | A1 不做 |

## 15. 文档同步（随实现完成）

- `SDD/09`（解码路径的第三段：本地快、本地深、云端深）、`SDD/12`（新服务/新端口/新日志）、
  `SDD/14`（版本行）。
- `AGENTS.md`（模块表加 `ft8/platform/cloud_decode.py`）、`tests/README.md`（计数）。
- hub 仓 `mrrc_hub`：`deploy/README.md`（新服务 + nginx route）与 SDD。
