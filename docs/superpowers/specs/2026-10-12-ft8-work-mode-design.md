# FT8 工作模式（mrrc_modern × MRRC-FT8 引擎）设计

- **日期**：2026-10-12
- **状态**：待评审（本文件通过后进入实现计划）
- **范围**：W103D 盒子（aarch64，呼号 BA4EG）首个交付；代码结构保持跨平台可移植
- **引擎来源**：`HAM/ft8`（MRRC-FT8 仓库）——本设计把其 DSP 与编排层移植进本仓，移植边界见 §4.1
- **相关**：`server.py`（音频循环/门控/状态广播）、`audio_handler.py`、`audio_resample.py`、
  `backends/ft710/`（CatController / config_ft710）、`static/`、`cqr_player` 先例（`cq_player.py`）、
  `packaging/common/stage-tree.sh`、`linux/mrrc_update.sh`；
  SDD `09-architecture-overview.md`、`12-operational-model.md`、`15-ptt-safety-architecture.md`

---

## 1. 背景与动机

W103D 盒子现在跑 mrrc_modern：FT-710 的远程 SSB 控制（CAT + 音频 + 频谱 + 公网 Cloud Hub）。
操作者要在**同一台电台**上增加 FT8 工作能力。两条路：

1. **盒子再跑一个独立的 MRRC-FT8 服务。** 它与 mrrc_modern 争同一个串口、同一个 USB 声卡、
   同一个 PTT。Linux 串口不独占、ALSA 可多开——两个进程的互斥无法做到可靠，
   冲突形态是"静默失灵"（CAT 命令互相交错、采样率互踩），属于最难排查的一类故障。
2. **把 FT8 做成 mrrc_modern 的一个工作模式。** FT8 引擎复用平台的电台控制、声卡、
   TX 门控与 Web 层；电台资源永远只有一个所有者（平台自己）。

本设计走第 2 条：**FT8 的 DSP 与编排逻辑从 MRRC-FT8 移植，平台耦合全部收敛在一个新的适配层**。
SSB 侧行为不变。

## 2. 目标与非目标

### 目标

1. **菜单一键切换**：主界面菜单加 "FT8" → 切换工作模式并进入 FT8 界面；FT8 界面有 "SSB" 返回。
   模式是**全局**的（服务器单例），任何已认证控制会话可切换。
2. **复用声卡**：FT8 RX 从既有 44.1 kHz RX 流 tap 出 12 kHz；TX 波形注入既有 TX 队列。
   **不新增任何声卡打开**。
3. **复用电台控制**：频率/模式/滤波/PTT 全部走 `CatController`；**不引入 hamlib/rigctld**
   （MRRC-FT8 的 rigctld 客户端及其中全部 FT-710 hamlib 兼容 hack 一并消失）。
4. **FT8 设置独立**：呼号、Grid、射频模式（`DATA-U` / `USB`，默认 `DATA-U`）在 FT8 设置内
   单独配置，与 Cloud Hub 呼号、平台设置互不耦合。
5. **首版功能完整**：解码显示（含 JTDX 风格高亮与 DXCC 判定）、TX 呼叫与回复、QSO 序列、
   CQ 循环、瀑布、日志落库。

### 非目标（首版明确不做）

- FT4；桌面客户端（`/desktop` 的 ft8web）；band hunter；JTDX ADIF 同步；
  auto-call-new-DXCC；RUMLogNG 同步；FT-710 之外的电台型号（保持 backend 抽象，但不验收）。
- 不改变 SSB 现有行为：听音、TX 上行、频谱、录音、ATR-1000、CQ 播放、Cloud Hub 全部原样。

## 3. 架构总览

```
                    ┌──────────────────────── mrrc_modern server.py ────────────────────────┐
 浏览器 SSB 界面 ◄─► │ 既有：/WSradio /WSaudioRX /WSaudioTX /WSspectrum + REST（不改协议）     │
 浏览器 FT8 界面 ◄─► │ 新增：/ft8 页面；/api/ft8/*；/WSft8；模式切换入口                       │
                    │                                                                        │
                    │ AudioHandler ─ RX(44.1k) ─┬─► Opus → SSB 听音／录音／频谱（不变）       │
                    │                           └─► [FT8] platform/rx_tap → 12k → UtcRing     │
                    │ AudioHandler ◄─ feed_tx_audio(48k) ◄─ [FT8] platform/tx_sink           │
                    │ CatController ◄─ [FT8] platform/cat（频率/模式/滤波）                   │
                    │ TX 门控（原 CQ key/unkey）◄─ [FT8] tx_sink（PTT 唯一出口）              │
                    │                                                                        │
                    │ ft8/engine（移植）：orchestrator / sequencer / safety / tx_driver / …   │
                    │ ft8/core（移植）：DSP Worker 子进程（wsjt_core.so，ctypes + 全局锁）     │
                    └────────────────────────────────────────────────────────────────────────┘
```

**数据流**

- RX：`电台 → USB 音频 44.1k → AudioHandler → [tap] → 44.1k→12k 流式重采样 → UtcRing →
  DSP Worker → 解码 → /WSft8 广播`（SSB 听音同源并存，互不影响）
- TX：`sequencer → dsp_encode（48k float32）→ safety.transmit → tx_sink → feed_tx_audio →
  设备（48k→44.1k 平台既有）→ 电台`；PTT：`safety → platform rig → 平台 TX 门控 → cat.set_ptt`

## 4. 详细设计

### 4.1 代码布局与移植边界

```
mrrc_modern/（分支 feat/ft8）
├── ft8/
│   ├── core/        ← 移植 verbatim：binding / models / paths / protocol / supervisor / worker
│   ├── engine/      ← 移植（I/O 边界见 4.2–4.4）：
│   │   verbatim:    orchestrator, sequencer, tx_driver, dsp_decode, dsp_encode, msgparse,
│   │                qso_log, repository, bands, tx_frequency, waterfall, latency, cq_loop, dxcc
│   │   小改:        safety（PTT/播放器改为注入 tx_sink）；audio_rx（保留 UtcRing 等，
│   │                44.1k→12k 转换器重写）；audio_tx（保留 validate_tx_buffer/常量）；
│   │                waterfall 本体 verbatim，其音频 tap 接线移入 platform/rx_tap
│   │   替换:        rig → platform/cat；capture_proc、device_config → 不移植
│   ├── platform/    ★ 新写（本设计唯一的原创代码层）
│   │   ├── rx_tap.py     # 44.1k→12k 流式重采样 + 带 UTC 锚定的 UtcRing 投递
│   │   ├── tx_sink.py    # 48k 波形 → feed_tx_audio；PlatformTxPlayer（替换本地声卡播放器）
│   │   ├── cat.py        # PlatformRig：CatController → 引擎期望的 rig 接口
│   │   └── settings.py   # FT8 设置读写（呼号/Grid/射频模式/频点/…）
│   ├── web/         ★ 新增：ft8_api.py（REST）、ft8_ws.py（WS）——挂到既有 FastAPI app
│   ├── mode.py      ★ 新写：模式状态机（SSB ↔ FT8）与生命周期
│   └── dsp/         ← 移植：CMake + Fortran 源 + patched/（7 个补丁副本）+
│                       vendor/（WSJT-X 3.0.2 Improved 的 FT8 子集）+ build.sh + vendor.sha256
└── static/ft8.html + static/ft8/*.js  ← 从 MRRC-FT8 PWA 移植（换认证/端点/样式）
```

**移植规则**

- `ft8/core`、`ft8/engine`、`ft8/dsp` 与原仓库保持 verbatim；文件头保留 origin 注释
  （来源仓库 + 上游路径 + 移植日期），便于将来与 `HAM/ft8` 对比。
- 所有与 mrrc_modern 的耦合只允许出现在 `ft8/platform/`、`ft8/web/`、`ft8/mode.py` 与
  `server.py` 的少量接线处。
- 移植随带原仓库的相关测试到 `tests/`（`stage-tree.sh` 已排除 `tests/`，不进设备树）。
- `tests/` 的 vendor 政策回归（逆替换后与 WSJT-X 逐字节一致）一并移植，保护 7 个补丁副本。

### 4.2 RX 音频：44.1k → 12k

- **tap 点**：`server.py::_audio_rx_loop()` 中 `pcm = audio.read_rx_chunk()` 之后，与既有录音
  tap（`_rec_tap_rx`）并列；**仅当 `work_mode == ft8` 时**投递（SSB 模式零开销）。
- **重采样**：44 100 → 12 000 的精确比是 147:40。实现为 `scipy.signal.upfirdn` 多相
  流式重采样（overlap 状态保留，块边界零断裂、零重采），抗混叠截止 ≈5.4 kHz。
  等价于把 MRRC-FT8 `RxConverter` 的 48k→12k（4:1 FIR）换成 147:40 版本；`UtcRing`、
  `SLOT_SAMPLES = 180000` 不变。
- **通用比例**：目标恒为 12 kHz；源率取 `AudioHandler._rx_dev_rate`（盒子上是 44 100）。
  非 44.1k 源按 `dev_rate : 12000` 的即时有理数比例处理。
- **UTC 锚定**：tap 按"累计样本数 + 本机时钟"给每个块附 epoch；复用 `UtcRing` 的内建
  重同步（`RESYNC_THRESHOLD_SECONDS = 0.25`）吸收时钟漂移。音频硬件时钟为主时基。
- 不新增声卡打开；SSB 听音、录音、频谱与 waterfall 完全不受影响。

### 4.3 TX：波形注入与 PTT 门控

TX 权威链（`tx_driver` → `safety.transmit`）原样保留，只替换两个注入点：

| 原实现 | 集成后 |
| --- | --- |
| `TxPlayer(device=声卡).play(pcm)`（`main.py` 组装） | `PlatformTxPlayer.play(pcm)`：48k float32 → int16 → `AudioHandler.feed_tx_audio()`（codec 域 48k；设备侧 48k→44.1k 平台既有） |
| `safety` 内 `self._rig.set_ptt(True/False)` | 注入 platform rig：转发平台 TX 门控（key/unkey），不再直连 CAT |

- **平台 TX 门控泛化**：把 `server.py` 的 `_cq_key/_cq_unkey` 提为平台级 TX 会话
  （CQ 播放器与 FT8 共用同一实现）：
  - `key()`：`tx_gated` 检查 → `_claim_tx_owner_*` → `cat.set_ptt(True)` → `audio.start_tx()`
  - `unkey(graceful)`：`audio.stop_tx(graceful)` → `cat.set_ptt(False)` → `radio.update(tx_status=0…)`
- **`PlatformTxPlayer` 的时序**：按 20 ms 帧注入（水位控制，与 `CQPlayer` 同构——不是一次性
  塞满队列）→ 等待波形时长（safety 的 deadline 看门狗并行生效）→ 由 safety 触发 unkey。
  "服务器本地音频 → 既有 TX 队列"这条路已在盒子上验证过。
- FT8 TX 期间浏览器麦克风上行按既有 `_tx_cq_mic_drops` 机制丢弃（防双源）。
- 平台看门狗（`_max_tx_watchdog`，`MRRC_PTT_MAX_TX_SECONDS=120`）对 FT8 同样生效；
  FT8 引擎 `safety` 的 aggregate budget / deadline / fault latch 保留为第二道闸。
- `radio.update(tx_status=…)` 全程同步，平台 UI、录音与频谱的 TX 感知不变。

### 4.4 电台控制适配（`platform/cat.py`）

`PlatformRig` 暴露引擎需要的方法子集，接口形状与 `engine/rig.py::RigClient` 一致
（`sequencer`/`safety`/`tx_driver` 因此零改动）：

| 引擎调用 | 平台实现 |
| --- | --- |
| `get_frequency()` / `set_frequency(hz)` | `CatController.get_frequency/set_frequency` |
| `get_mode()` / `set_mode(mode, passband)` | `get_mode/set_mode`；`FT8_RIG_MODE` 映射 `USB→0x02`、`DATA-U→0x0C`（FT-710 模式表） |
| `set_filter_width(hz)` | 用 `FILTER_WIDTHS_NARROW` 反查最接近 index（3000 Hz → 18），`set_filter_width(index)` |
| `get_level/set_level`（AF/RF/功率） | `set_af_gain / set_rf_gain / set_rf_power` 等既有方法 |
| `get_ptt()/set_ptt()` | **不走 CAT**：经平台 TX 门控（§4.3），`safety` 是唯一调用者 |

进入 FT8 模式的电台工作点：`波段→FT8 频点`（沿用引擎 `bands.py` 频点表）+ `FT8_RIG_MODE` +
3000 Hz 滤波；切回时按 §4.5 恢复快照。

### 4.5 模式状态机（`ft8/mode.py`）

- 状态：`work_mode ∈ {ssb, ft8}`，服务器单例，**运行时状态**（重启回 `ssb`——安全默认）。
  切换操作以 `asyncio.Lock` 串行化（两个会话同时点也只有一个生效）。
- **SSB → FT8**：
  1. 快照 SSB 台况（明确字段）：频率、模式号、滤波 index、RF 功率、AGC、ATT、PREAMP
  2. 禁用浏览器 TX 上行（PTT 归 FT8 引擎）
  3. 电台切 FT8 工作点（频点 + 射频模式 + 3 kHz）
  4. 启动引擎：DSP Worker 起 → RX tap 通 → orchestrator 进入 monitor 模式
  5. 广播 `work_mode=ft8`；SSB 界面显示"FT8 模式中"并禁用 TX 控件
- **FT8 → SSB**：
  1. **安全优先**：TX disarm + 等待 PTT off + 清 TX 队列（同步完成才继续）
  2. 停 DSP Worker、摘 RX tap（SSB 听音、录音、频谱全程不断流）
  3. 恢复台况快照（逐字段回写 + 回读核对）
  4. 广播 `work_mode=ssb`
- **失败处理**：任一步失败 → 回到 `ssb` 并广播错误；**绝不停留在半启动状态**。
- **模式权威**：`work_mode` 只由服务器持有；客户端只是发起请求与跟随广播
  （多会话一致，与 TX 所有权一样是全局的）。

### 4.6 DSP Worker 与 wsjt_core 构建

- Worker 模型照搬：独立子进程 + pipe/`SharedMemory` 协议（`ft8/core/protocol.py`）+
  主进程全局锁（`ft8/core/binding.py`）。Worker 入口在导入 NumPy / 加载 `wsjt_core`
  **之前** `os.environ.setdefault("OMP_STACKSIZE", "10M")`（WSJT-X Improved 的大栈铁律）。
- **生命周期**：FT8 模式启动时拉起、切回 SSB 时关闭（不常驻，盒子 CPU 留给 SSB 与 FT8 解码）。
- `wsjt_core.so` **在设备上原生编译**：
  - 盒子 apt 依赖：`gfortran`、`libfftw3-dev`、`cmake`（`libgomp` 随 gcc；全部已在
    Armbian 源中，2026-10-12 核实：gfortran 12.2 / fftw 3.3.10 / cmake 3.25）
  - `ft8/dsp/vendor/` = WSJT-X 3.0.2 Improved 的 FT8 子集（约 95 个源文件 + include 闭包，
    预计 3–4 MB），带 `vendor.sha256`；7 个补丁副本（`patched/`）沿用原仓登记的差异
  - `ft8/dsp/build.sh` 幂等：源 digest 未变则跳过；产物 `ft8/dsp/build/wsjt_core.so`
    （`stage-tree.sh` 已排除 `build/`，构建产物不进同步树）
- Python 依赖：**新增 `scipy`**（重采样与 FIR；盒子 pip 有 aarch64 wheel）；
  numpy / fastapi / uvicorn 等平台已有。

### 4.7 Web 层

- **REST `/api/ft8/*`**：解码历史、TX 控制（回复/CQ/停止/音频频率）、QSO 状态、设置读写、
  DXCC 查询——从 MRRC-FT8 `web/api.py` 移植端点定义，**认证换成平台 token**
  （`_verify_auth` 同一套）；`listen` 只读角色只能 GET 与观察。
- **WS `/WSft8`**：解码广播 + 模式状态 + TX 事件；握手认证复用平台既有 WS 逻辑。
- **模式切换入口**：`POST /api/ft8/mode {ssb|ft8}`（控制角色）；切换结果经既有
  `stateUpdate`/`fullState` 广播（新增 `work_mode` 字段）。
- **健康报告**：worker generation/重启次数、解码延迟与丢帧计入既有 status/health 暴露面
  （诊断包与 🐞 上报因此自动包含 FT8 引擎状态）。
- 既有 SSB 端点与协议**不改**（向后兼容）；音频 WS（`/WSaudioRX`/`/WSaudioTX`）在 FT8 模式下
  RX 照常、TX 由平台丢弃上行（§4.3）。

### 4.8 前端

- 新页面 `static/ft8.html`（移动横向布局）+ `static/ft8/*.js`：从 MRRC-FT8 PWA 移植解码列表、
  瀑布、TX 面板（回复/CQ/停止/音频频率）、QSO 面板、设置面板；改造点仅为：API 路径、
  WS 端点、认证（平台 token）、主题样式对齐（`ft710.css` 变量）。
- 主界面菜单（`static/index.html` 的 `main-menu`）新增 "FT8" 项 → `POST /api/ft8/mode {ft8}`
  → 跳转 `/ft8`。
- `/ft8` 顶部 "SSB" 按钮 → `POST /api/ft8/mode {ssb}` → 回主界面。
- SSB 界面在 `work_mode=ft8` 时显示模式提示条并禁用 TX/PTT 控件（防误操作）；
  `listen` 会话提示"FT8 模式中（只读）"。

### 4.9 配置与存储

- **FT8 设置**（独立）：`my_call`、`my_grid`、`rig_mode`（默认 `DATA-U`）、频点覆盖、
  TX 音频频率、解码 profile / 线程数、CQ 循环开合。存储于
  `<writable runtime>/ft8/settings.json`（走平台 `_writable_runtime_dir()` 惯例，
  Linux 盒子即 `/opt/mrrc_modern/...` 下 mrrc 用户可写处）；设置面板在 FT8 页面内。
- **QSO 库**：移植 `repository.py`（SQLite），`<writable runtime>/ft8/qso.db`；
  保留显式迁移与 7 天窗口查询语义。
- 与 `MRRC_CLOUD_CALLSIGN` 无耦合：FT8 呼号是操作者电台身份，单独设置（本盒子上
  Cloud 呼号是 BA4EG，FT8 呼号由操作者决定）。

### 4.10 安全与多会话

- TX 唯一出口是平台 TX 门控 ⇒ FT8 与 SSB **不可能同时键控**（结构性保证，非约定）。
- 任意已认证控制会话可紧急 STOP；STOP 触发 FT8 TX 立即 abort + disarm（复用引擎
  `safety.stop_tx` 语义 + 平台 PTT 释放）。
- `listen` 角色：可看 FT8 解码与状态，不能切模式、不能 TX。
- 切回 SSB 的"先停 TX 再恢复台况"顺序是硬约束（§4.5），并由往返测试钉住。

## 5. 部署（W103D 盒子）

1. 本地：`feat/ft8` 分支开发与测试（macOS：单元 + 合成信号回归，无硬件）。
2. 盒子一次性准备：`apt-get install -y gfortran libfftw3-dev cmake`；venv 安装 `scipy`
   （`mrrc-update` 会自动补 `requirements.txt` 新增项）。
3. 同步代码：rsync 本地树到盒子临时目录 → `mrrc-update <dir>`（既有机制）；
   `ft8/` 随树同步，`ft8/dsp/build/` 被 `stage-tree.sh` 排除。
4. 构建 DSP：`/opt/mrrc_modern/ft8/dsp/build.sh`（幂等）→ `ft8/dsp/build/wsjt_core.so`。
5. `systemctl restart mrrc-modern` → 登录 → 菜单切 FT8 → 按 §6 验收。
6. 长期：分支合并后走标准发布/升级通道。**本设计不改镜像构建**（DSP 编译烘焙进镜像
   留待 §7-8 评估）。

## 6. 测试与验收

**本地（无硬件）**

- 移植的 engine 测试随代码搬入 `tests/`；新增适配层测试：
  - 重采样：正弦/静音输入的块边界连续性（无断裂、无样本重采）、混叠抑制
  - `PlatformTxPlayer`：帧序列、时长、取消语义
  - `PlatformRig`：模式映射、滤波宽度反查、PTT 经门控
  - 模式状态机：往返后快照逐字段一致；启动失败回滚到 `ssb`
- 合成信号回归：`ft8sim` wav 经"44.1k 链路"（重采样后）解码结果与原生 48k 链路一致。
- vendor 政策回归：7 个补丁副本逆替换后与 WSJT-X 逐字节一致。

**真机（盒子 + FT-710）**

1. 解码：切 FT8 即收到实台解码，高亮（my_call / new DXCC / new grid）正确
2. TX：CQ 一轮 → 正确键控、功率输出、SWR 正常；波形时长 12.6 s 精确
3. QSO：完成一次真 QSO 并落库（含 73）
4. 切换往返 ×10：无 PTT 残留、SSB 台况逐字段恢复、SSB 听音/频谱不断流
5. FT8 TX 中紧急 STOP（另一会话发起）立即生效
6. 性能：单时隙解码耗时有余量（profile 3；目标 <15 s 且不饿死 SSB 音频）

## 7. 风险与未决问题

| # | 风险 / 未决 | 处置 |
| --- | --- | --- |
| 1 | **4×A53 解码性能**（最大风险） | 里程碑 M1 先做裸引擎 spike；不达标降 profile/线程或缩窄解码带宽 |
| 2 | 44.1k→12k 重采样质量影响解码率 | M1 全链路合成信号回归（要求与 48k 路径同解） |
| 3 | WSJT-X Fortran 子集在 aarch64 编译（gfortran 12 + OpenMP） | M1 真机编译验证；vendor 政策回归保护补丁副本 |
| 4 | DSP 阻塞帧与平台事件循环互扰 | worker 子进程隔离（已有）；必要时 decode 请求经 `asyncio.to_thread` |
| 5 | 模式切换状态泄漏 | §6-4 往返测试 + 真机逐字段核对 |
| 6 | 上游（HAM/ft8）继续演进导致分叉 | engine 层 verbatim + origin 注释；差异只允许在 platform/web/mode |
| 7 | FT8 瀑布与平台 scope 混淆 | 首版 FT8 瀑布来自音频 tap 的 FFT（引擎 waterfall），不混用 radio scope |
| 8 | DSP 编译是否烘焙进镜像 / mrrc-update 后自动重建 | 首版设备上手动构建；镜像化与自动重建留待 packaging 合并评估 |

## 8. 里程碑

| M | 内容 | 出口判据 |
| --- | --- | --- |
| M1 | 盒子上的 DSP spike：vendor 抽取 + 编译 + 合成信号解码（命令行） | 解出已知信号；单时隙耗时实测（风险 1/2/3 定论） |
| M2 | 引擎移植 + `platform/` 适配（无 UI，测试驱动） | 本地测试全绿；盒子命令行跑通解码与编码 |
| M3 | 模式状态机 + Web 层（REST/WS） | 切换往返无泄漏；API 集成测试 |
| M4 | FT8 前端 + 菜单集成 | 浏览器里完成一次完整 QSO 流程 |
| M5 | 真机验收 + 部署固化 | §6 真机 6 项全过 |
