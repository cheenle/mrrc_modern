# Kenwood TS-940S 接入 MRRC Modern：SDD / 架构设计

- **文档 ID**：SDD-TS940S-MRRC-001
- **日期**：2026-11-15
- **版本**：V0.4（MRRC 原生 backend 架构）
- **状态**：待评审；未授权采购定型、机内施工或打开发射
- **产品归属**：MRRC Modern 的新电台型号 `ts940s`
- **核心原则**：不引入 Hamlib/rigctld、SDR++、WSJT-X、虚拟声卡或第二个 Web 服务
- **取代**：V0.3 的“主机网关 + 外部软件集成”路线；该路线不再作为实现基线
- **相关契约**：`backends/base.py`、`backends/__init__.py`、`server.py`、`ft8/`、`scope_handler.py`、`spectrum_profile.py`、SDD 第 15 章

---

## 1. 执行摘要

TS-940S 不是另一套远程电台软件，而是一个新的 MRRC Modern backend：

```text
浏览器 / 原生客户端
        │  既有 /WSradio /WSaudioRX /WSaudioTX /WSspectrum /api/ft8/*
        ▼
MRRC Modern server
        │  RadioBackend("ts940s")
        ├─ Kenwood CAT 命令与状态机
        ├─ AudioHandler-compatible 边缘音频
        ├─ ScopeProducer + 850 点标准频谱
        ├─ FT8 Runtime / TX gate / PTT 安全
        ▼
ESP32-S3 硬件边缘（只搬运硬件信号，不拥有电台产品逻辑）
        ├─ IF-10B UART
        ├─ ACC2 RX/TX audio + 麦克风静音/路由
        ├─ 隔离 PTT GPIO
        └─ USB IF SDR（可选，由 MRRC server 驱动）
```

第一版交付的是用户熟悉的 MRRC 界面：频率、模式、瀑布、S 表、PTT、SSB 双向语音、FT8 解码与发射、录音和会话管理。电脑或 ARM 盒子运行 MRRC server；ESP32-S3 是这台电台的机内硬件边缘，不是面向用户的另一个控制台。

## 2. 为什么必须改掉 V0.3

V0.3 把功能放到 SDR++、WSJT-X、虚拟声卡和第三方 rigctl 网关中。它能工作，但不符合本产品已经建立的能力边界：

| V0.3 外部组件 | MRRC 原生替代 | 仓库证据 |
| --- | --- | --- |
| Hamlib / rigctld | `backends/ts940s/controller.py` + `RadioBackend` | AD-002 明确拒绝 Hamlib |
| SDR++ | `ScopeProducer` + `/WSspectrum` | `server.py` 已有 backend scope factory |
| WSJT-X | `ft8/runtime.py` + `/WSft8` | FT8 runtime 已集成 |
| 虚拟声卡 | `AudioHandler-compatible` backend audio | `SunSDRAudioHandler` 已是先例 |
| 第二个 Web 服务 | MRRC 现有 FastAPI 和认证 | 所有协议和 session metrics 已存在 |

因此产品逻辑不应再分散到多个桌面程序。TS-940S 接入后必须与其他型号一样通过同一 UI、同一协议、同一安全链工作。

## 3. 目标与非目标

### 3.1 目标

1. `MRRC_RADIO_MODEL=ts940s` 可被 server、连接设置、原生客户端和诊断工具识别。
2. 浏览器继续访问 `/WSradio`、`/WSaudioRX`、`/WSaudioTX`、`/WSspectrum`；协议不改。
3. 频率、模式和其他已验证控件通过 MRRC backend 映射 Kenwood CAT。
4. SSB 双向音频、录音、CQ、FT8 解码与 FT8 TX 使用既有平台路径。
5. IF SDR 提供标准 850 点瀑布和点击调谐。
6. ESP32 机内集成保持可复原，不改变原机本地操作。
7. 硬件未验证前 `verified=False`，发射默认门禁。

### 3.2 非目标

- 不让 ESP32 自己运行 MRRC、维护第二份电台状态或拥有浏览器 UI。
- 不在运行期使用 ESP32 USB CDC/UAC；原生 USB 仅烧录和维修。
- 不安装或调用 SDR++、WSJT-X、Hamlib、rigctld、虚拟声卡驱动。
- 不新增面向用户的第二套登录、PTT 或频谱协议。
- 不把原始 IQ 经 Wi-Fi 传给 MRRC server；设备在 MRRC 主机本地处理。
- 不自动启用 ATR、电源控制、天调、VFO-B 或现代遥测，除非真机证据闭合。

## 4. 系统分解

### 4.1 三层所有权

| 层 | 所有者 | 负责 | 禁止 |
| --- | --- | --- | --- |
| 产品层 | MRRC server | UI 状态、CAT 语义、Opus、FT8、PTT 会话、日志、权限 | 把状态真相交给 ESP32 |
| 边缘层 | ESP32-S3 | UART 透传、I2S PCM、GPIO、硬件心跳和看门狗 | 自行决定模式、频率、发射所有权 |
| 原机层 | TS-940S | 射频、混频、滤波、功放、本地控制 | 依赖新增模块才能独立操作 |

ESP32 的“唯一运行期网络通道”仍是 Wi-Fi，但它连接的是 **MRRC server**，不是浏览器或外部软件。

### 4.2 新增代码边界

```text
backends/ts940s/
├── backend.py          # RadioBackend + RadioCapabilities + UI tables
├── controller.py       # MRRC CatController-compatible edge controller
├── edge_protocol.py    # 版本化 WSS/二进制帧协议，无电台语义
├── audio.py            # AudioHandler-compatible adapter
├── scope.py            # ScopeProducer，输出标准 ScopeHandler 数据
├── if_pipe.py          # SDR USB 子进程，仅 backend 模式使用
├── config_ts940s.py    # Kenwood tables and calibration placeholders
└── diag_ts940s.py      # 真机针脚、CAT、音频、频谱、PTT 自动检查

static/                 # 不新增电台产品页；复用既有主 UI 和 FT8 页
server.py               # backend lifecycle、注册表、连接元数据，不复制业务逻辑
```

具体文件可在实现计划中微调，但产品逻辑不得落到 ESP32 或外部桌面程序。

## 5. 已确认的原机与软件事实

### 5.1 硬件事实

| ID | 事实 | 等级 | 架构影响 |
| --- | --- | --- | --- |
| F-001 | `IF OUT1 = 8.83 MHz`，`IF OUT2 = 100 kHz` | E1 | 背板通路只能做窄带验证 |
| F-002 | 接收第一中频 45.05 MHz，RF Unit 存在 RIF 线束 | E1/E4 候选 | 宽带候选取样点 |
| F-003 | 45.05 MHz MCF 0.5 dB 跨度约 ±3 kHz | E1/E4 | MCF 后不能宣称几百 kHz 宽带 |
| F-004 | IF-10B 需要机内板、Digital A 19/20 线束和 PROM | E1/E2 | M0 必须闭合 |
| F-005 | CAT 起点 4800 8N1、无流控 | E1/E2 | UART 桥参数，仍需 E4 |
| F-006 | ACC2-3/9/11 关联 RX AFSK、MIC control、TX AFSK | E1/E4 | 数据音频不能只接两根线 |
| F-007 | ACC2-13 是 PTT/STBY 候选 | E2 | 施工前 E4 |
| F-008 | ESP32-S3 支持 Wi-Fi、UART、I2S 和硬件看门狗 | E2 | 可作硬件边缘 |

### 5.2 MRRC 软件事实

| ID | 现有契约 | 对 TS-940S 的意义 |
| --- | --- | --- |
| N-001 | `RadioBackend.create_audio_handler()` 返回兼容 AudioHandler 时，server 不再创建 PyAudio | backend 可直接提供边缘 PCM |
| N-002 | `RadioBackend.create_scope_producer()` 可注入标准 `ScopeHandler` | 使用 `/WSspectrum`，不需要 SDR++ |
| N-003 | FT8 RX tap 接收 codec-domain 48 kHz 块并流式重采样到 12 kHz | backend audio 必须稳定输出 48 kHz/20 ms |
| N-004 | FT8 TX 通过平台 TX gate，经 `feed_tx_audio()` 进入同一队列 | 不绕开 PTT/音频所有权 |
| N-005 | `PlatformRig` 从 backend 的模式与滤波表取数 | 模式表按 TS-940S 真机，不沿用 Yaesu DATA-U |
| N-006 | `known_models()`、`model_connection()`、`model_verification()` 驱动白名单和设置页 | 注册 backend 后 UI/API 自动认识 |
| N-007 | PTT 释放是多层独立路径 | ESP32 边缘层只能增加防线，不能替代原层 |
| N-008 | AD-019 未验证机型 TX gated | TS-940S 初始仅接收，显式开关后才允许 TX |

## 6. 架构决策

### AD-MRRC-TS-001：TS-940S 作为一等 backend

**决定**：新增 `ts940s` model key，完整实现现有 `RadioBackend` 契约。  
**拒绝**：另建 Kenwood Web 服务或让 ESP32成为独立产品。  
**后果**：认证、多会话、PTT、录音、诊断、OTA、原生客户端和 metrics 全部自然继承。

### AD-MRRC-TS-002：ESP32 只做透明硬件边缘

**决定**：ESP32 不解析 Kenwood CAT，不保存频率/模式，不决定 PTT 所有权；只实现版本化硬件桥。  
**理由**：电台产品逻辑必须只有一个权威；把语义复制进 MCU 会产生两套状态和释放规则。  
**后果**：固件更薄，server 端可测试；但需要可靠的 session fencing 和硬件故障释放。

### AD-MRRC-TS-003：边缘协议使用 PCM，不让浏览器音频二次编码

**决定**：ESP32 ↔ MRRC server 的内部音频为 48 kHz、Int16、单声道、20 ms PCM；浏览器仍沿用 Opus。  
**理由**：平台已有 codec-domain 契约，本地 768 kbit/s 远低于 Wi-Fi 能力；避免 ESP32/服务器各编一次。  
**后果**：降低时序与语音质量风险；ESP32 不需要 Opus 库。

### AD-MRRC-TS-004：IF SDR 由 backend ScopeProducer 独占

**决定**：USB SDR 子进程属于 TS-940S backend，输出标准 `ScopeHandler` 850 点帧。  
**限制**：同机启用独立 `/WSsdr` 时，两个入口不得争用同一设备；server 必须按设备索引仲裁或明确拒绝同设备双订阅。  
**后果**：网页瀑布、Android 频谱和点击调谐不需要 SDR++。

### AD-MRRC-TS-005：模式与滤波能力只从真机表生成

**决定**：TS-940S 的 UI mode、band、filter、meter、VFO/split 等 capability 必须来自 profile 与 E4 回读。  
**拒绝**：猜测 Kenwood 命令，或沿用 FT-710/Icom 数字。  
**后果**：初始 profile 是 `verified=False`；UI 自动隐藏未知能力。

### AD-MRRC-TS-006：FT8 使用平台模式，DATA-U 不是前置条件

**决定**：backend 暴露实际模式；FT8 runtime 通过现有 `_resolve_mode()` 选择已验证的 USB/LSB/CW 数据工作方式。  
**修正要求**：`PlatformRig` 必须区分“backend 没有窄滤波表”与“调用失败”，不得静默退回 FT-710 表。  
**后果**：没有 WSJT-X 也完成解码、定时 TX、解码列表和 QSO 流程。

### AD-MRRC-TS-007：PTT 仍走现有平台 TX gate

**决定**：`backend.set_ptt(True/False)` 最终由 MRRC TX gate 调用，浏览器/FT8/CQ/原生客户端不得直接控制 GPIO。  
**新增层**：server → ESP32 边缘会话增加独立心跳与断链强制释放。  
**后果**：保留 SDD 第 15 章全部释放层，并把“server 进程死亡时 ESP32 本地释放”补进边界。

## 7. 端到端架构

```text
┌──────────────────────────── 用户端 ────────────────────────────┐
│ 主 SPA       FT8 页        Android/iOS        录音/日志/设置   │
└──────┬───────────┬─────────────┬───────────────────┬──────────┘
       │           │             │                   │
       ▼           ▼             ▼                   ▼
┌──────────────────────── MRRC Modern server ────────────────────┐
│ /WSradio  /WSaudioRX  /WSaudioTX  /WSspectrum  /api/ft8/*      │
│ auth / role / dirty-state broadcast / session metrics / logs   │
│                                                                │
│ backends/ts940s/backend.py                                     │
│ ├─ controller.py        Kenwood CAT state machine              │
│ ├─ audio.py             AudioHandler-compatible adapter         │
│ ├─ scope.py + if_pipe.py USB IF SDR → ScopeHandler              │
│ └─ PTT/TX gate hooks    key → priority → unkey                 │
│                                                                │
│ ft8/runtime.py          UTC ring → 12 kHz decode → TX waveform │
└───────────────────────────┬────────────────────────────────────┘
                            │ Wi-Fi / mTLS WSS
                            ▼
┌──────────────────── ESP32-S3 hardware edge ────────────────────┐
│ channel 1: transparent IF-10B UART                             │
│ channel 2: 48 kHz PCM RX from ACC2                             │
│ channel 3: 48 kHz PCM TX to ACC2                               │
│ channel 4: PTT / MIC-control / link state                      │
│ watchdog → fail-safe open                                      │
└───────┬───────────────────────┬──────────────────────┬─────────┘
        │ UART                  │ I2S                  │ GPIO
        ▼                       ▼                      ▼
   IF-10B / Digital A     ACC2 audio paths       ACC2 PTT/STBY

TS-940S 45.05 MHz IF ──高阻缓冲── USB IF SDR ──┐
                                              └─ MRRC 主机本地 USB
```

## 8. 边缘协议

### 8.1 传输

- Wi-Fi AP 直连或 STA 入网；MRRC server 是唯一运行期控制客户端。
- WSS + mTLS；服务器验证设备证书，设备验证服务器 pin。
- 一个逻辑控制会话，四条带序号 binary subchannel。
- 协议首帧协商版本；不兼容时 backend 拒绝连接，UI 明确显示，不猜测降级。

### 8.2 subchannel

| ID | 方向 | 内容 |
| --- | --- | --- |
| `0x01` | server → edge | 原始 Kenwood CAT 字节 |
| `0x02` | edge → server | 原始 CAT 字节，含 unsolicited 数据 |
| `0x03` | edge → server | RX PCM 块 |
| `0x04` | server → edge | TX PCM 块 |
| `0x05` | 双向 | JSON control/status |

PCM 帧固定头：

```text
magic u16 | version u8 | flags u8 | seq u32 | sample_count u16 | epoch_ms u64 | PCM
```

- `epoch_ms` 使用设备单调时钟，server 将其映射到本地时间轴；不使用未经校准的 RTC。
- `seq` 用于丢帧统计；断序时补静音并记录，不重复旧样本。
- control JSON 固定动词：
  `hello`、`session_fencing`、`ptt_key`、`ptt_release`、`mic_route`、`status`、`ping`、`health`。
- 不开放任意 GPIO、任意网络请求或固件裸写；OTA 单独设计且 TX 中禁止。

### 8.3 会话权威

- edge 一次只接受一个 MRRC session nonce；新会话抢占前旧会话必须立即释放 TX。
- 重连第一步永远是 `ptt_release`，然后才读取状态。
- edge 本地记录“曾键控但未见释放”事件；server 恢复时进入安全同步，不直接信任旧 UI 缓存。

## 9. CAT backend

### 9.1 控制器边界

`controller.py` 实现平台需要的查询/设置方法，不把 `CatController` 串口假设泄漏到 server：

- 每个命令带 transaction id 和分号响应边界。
- server 侧 `asyncio.Lock` 保持单写者；PTT 控制走 subchannel `0x05`，不被 UART 查询阻塞。
- timeout、frame error、unsolicited bytes 分别计数。
- `IF;`/`FA;`/`MD;` 的实际格式和回读先由 `diag_ts940s.py` 采集，再写 profile。

### 9.2 轮询

- 初始频率/模式读回 1–2 Hz，避免 4800 baud 饱和。
- 用户命令后短暂提升，命令完成恢复。
- 物理旋钮通过回读发现；backend 更新 dirty fields，所有既有 clients 自动同步。
- meter 表项只注册已验证命令；未知项不得用合成值冒充。

### 9.3 profile 初值

```text
bands: 160–10 m + 真机扩展候选
ui_modes: 仅 E4 确认可读写的模式
vfo_a: 主路径
vfo_b/split: 默认关闭
filter/attenuator/NR/COMP: 默认关闭，真机逐项开放
scope: IF SDR 或 S-meter fallback
radio power: 默认关闭
```

`verified=False`、`tx_gated=True` 是首版硬默认。

## 10. 音频 backend

### 10.1 接口

`EdgeAudioHandler` 必须实现 server 使用的兼容接口：

```text
device_rates()
read_rx_chunk()
start_tx() / stop_tx()
feed_tx_audio()
tx_stats()
tx_queue_frames()
close()
```

具体名称以当前 `AudioHandler` 契约和 `SunSDRAudioHandler` 先例为准，不以本文重新发明。

### 10.2 RX

```text
ACC2 → 电平匹配 → PCM1808/I2S → edge sequence → server jitter buffer
→ platform Opus → 浏览器
FT8 tap：同一 PCM 块 → 48k→12k → UtcRing
```

- 20 ms 块稳定进入 `_audio_rx_loop`。
- 设备重启、PTT/TX 或链路短暂中断造成空洞时，平台已有 tap re-anchor 必须生效。
- 空洞期间 FT8 slot 宁可跳过，绝不拼接旧音频。

### 10.3 TX

```text
浏览器 mic / CQ / FT8 waveform → 平台 48k PCM
→ EdgeAudioHandler.feed_tx_audio()
→ 有界队列 → I2S TX → ACC2
```

- 只有 TX gate key 成功且 `tx_status` 一致才接受帧。
- 正常释放先停流/清队列，再释放 PTT。
- queue drops、seq holes、write errors 进入每会话诊断。
- TX 音频启动失败必须回退 RX，沿用平台既有错误路径。

### 10.4 麦克风路由

ACC2-9 的语义必须由 backend `mic_route` 原子控制：

- RX：恢复原机收听。
- TX request：先确认本地麦克风不会混入，再启动 PCM TX。
- release：先静音/清队列，再恢复本地路由。
- 未知极性/时序时该 backend 不允许 TX。

## 11. 频谱 backend

### 11.1 数据链

```text
45.05 MHz IF → high-Z buffer → USB IF SDR
→ host-local FFT → ScopeHandler 850-bin frame
→ /WSspectrum profile → Web/Android
```

- `scope_type="if_usb"`。
- 输出仍是现有平台标准频谱，不新建协议。
- `scope_start_freq = vfo - half_span`，与 SunSDR backend 同构。
- span table 由实际 IF 可用宽度决定，不把模块标称采样率当可用带宽。

### 11.2 频率与方向

- SDR 调谐中心是 IF 常数；UI 中心频率是电台 VFO。
- backend 维护校准：

```text
f_rf = f_vfo + s × (f_sdr - f_if)
```

- `s`、IF 偏移、幅度 skew 和 span 上限由 `diag_ts940s.py` 产生并写入 profile。
- 点击调谐仍走标准 `/WSradio freq` 命令。

### 11.3 发射隔离

- TX 期间频谱可保留画面但标记不可信，避免把耦合当作接收信号。
- 前端和 FT8 不消费该时段的“频谱音频”。
- SDR 前端需实测电平裕量和限幅，不能只信模块最大输入。

## 12. FT8 集成

MRRC 已有 FT8 工作模式，TS-940S 只补 backend 表：

1. 用户进入既有 FT8 页面。
2. `Ft8ModeController` 快照 SSB 状态。
3. backend 设置已验证的 USB/LSB 工作模式。
4. RX tap 从 EdgeAudioHandler 接收 48k PCM。
5. 解码、瀑布、日志、CQ 和 QSO 流程使用现有 `/WSft8`。
6. TX 通过平台 TX gate 调 `backend.set_ptt(True)`。
7. 切回 SSB 前停止 TX、清队列、释放 PTT、恢复台况。

必须修正的软件假设：

- `PlatformRig` 不能因 backend 没有 FT-710 窄滤波表而静默使用 FT-710 fallback。
- 无滤波控制时应：明确跳过并可诊断，或返回“不支持此 FT8 操作”；不得发送未知 CAT 命令。
- TS-940S passband 和 RX 滤波行为以真机为准，不能假设 DATA-U 与 3 kHz。

## 13. PTT 与故障安全

### 13.1 继承既有层

保留 SDD 第 15 章全部路径：

- browser touch-and-hold/release
- WS radio release
- browser watchdog
- last control client / TX audio owner dead-man
- TX audio stream stop
- server max TX
- restart/release
- unverified TX gate

### 13.2 新增边缘层

| 层 | 机制 | 覆盖故障 |
| --- | --- | --- |
| E0 | session fencing | 两个 server/浏览器会话争用 |
| E1 | edge link 500 ms 心跳、1.5 s 无持键心跳即释放 | server 网络冻结、半开 |
| E2 | PCM TX sequence 空洞/静默超时释放 | 音频路径停摆 |
| E3 | edge MCU watchdog | 主固件死锁 |
| E4 | GPIO 复位高阻 + 光耦默认开路 | 崩溃、重启、OTA |
| E5 | 原机 TOT 或经评审硬件超时 | MCU 输出级短路且所有软件失效 |

E1–E4 是首版必测；E5 在有人值守本地场景记录能力，无人值守/公网场景才成为放行条件。

### 13.3 状态转换

```text
RX_IDLE
  → TX_REQUEST（platform TX gate）
  → TX_ARMING（mic route + silent TX stream）
  → TX_ACTIVE（edge ACK + PCM sequence）
  → TX_RELEASING（stop audio → PTT release → restore mic）
  → RX_IDLE
```

- key ACK 超时不得认为发射成功。
- release 不依赖 CAT 回读、browser、录音或日志。
- 任何重连都先释放。
- FT8、CQ、SSB、原生客户端共享唯一 TX gate，不建立第二通道。

## 14. 配置与环境变量

建议最小配置面（名称可在实现计划统一）：

```text
MRRC_RADIO_MODEL=ts940s
MRRC_TS940S_EDGE_HOST=<ESP32 IP/hostname>
MRRC_TS940S_EDGE_PORT=<wss port>
MRRC_TS940S_EDGE_CA=<pinned CA path>
MRRC_TS940S_EDGE_CERT=<client cert>
MRRC_TS940S_EDGE_KEY=<client key>
MRRC_TS940S_SDR_SERIAL=<可选设备序列>
MRRC_TS940S_IF_CENTER_HZ=<实测>
MRRC_TS940S_ALLOW_UNVERIFIED_TX=1（仅 M3 验证时）
```

连接设置页面需要提供：

- 机型：`Kenwood TS-940S（实验性）`
- 边缘地址与证书状态
- SDR 设备序列/可用状态
- CAT/音频/边缘/频谱诊断
- `verified=False` 和 TX gate 提示

`model_connection("ts940s")` 应返回网络 transport 且要求 host，不能继续按普通 serial model 推断。

## 15. 真机诊断命令

新增 `dev_tools/ts940s_diag.py` 或直接位于 `backends/ts940s/diag_ts940s.py`，一次输出：

1. edge TLS/session nonce/protocol。
2. IF-10B/PROM/UART idle level/polarity。
3. `IF;`/`FA;`/`MD;` 原始请求与响应。
4. ACC2-3/9/11/13 通断、电平、灌电流。
5. RX/TX 20 ms PCM 块率、seq hole、under/overrun。
6. SDR open、中心频率、span、FFT 更新率和 TX 耦合。
7. PTT key/release ACK、心跳超时、最长 TX、MCU reset。
8. 改装前后 MDS、杂散与 S-meter 对比。

报告必须区分 `PASS / FAIL / NOT_MEASURED`，不得用 profile 猜测补“PASS”。

## 16. 验证门

### M0：硬件与软件边界

- IF-10B/PROM 和 UART 电气确认。
- ACC2 逐针确认。
- MRRC `ts940s` backend key 注册。
- edge protocol 版本冻结。
- SDR 设备选择与独占规则。

### M1：无射频边缘台架

- server 连接 ESP32，假 UART/PCM loop。
- browser Opus 不经过二次 codec。
- PCM seq、jitter、reconnect、watchdog。
- edge key/release ACK 和 session fencing。
- 100 次 Wi-Fi/USB/进程重启无卡 TX。

### M2：原机只接收

- 主 UI 显示真实 `fullState`。
- 2 Hz CAT read，物理旋钮同步。
- RX 音频到 browser/FT8 tap 连续 2 h。
- `ScopeHandler` 标准频谱帧与点击调谐。
- 独立 `/WSsdr` 同设备冲突保护。
- MDS ≤1 dB，无可重复 +3 dB 新杂散。

### M3：假负载发射

- mic route 原子、TX PCM、queue_drops=0。
- browser PTT、FT8 TX、CQ 和另一 session STOP 全路径。
- edge heartbeat loss、server kill、browser crash、MCU reset、最长 TX。
- TX gate 与 FT8 runtime 无双重键控。
- 模式/滤波操作不发送未知命令。

### M4：机内集成

- 电源浪涌、屏蔽、IF 走线、天线净空、热和 EMC。
- 所有原机本地功能仍独立。
- 拆除后 backend 明确断开，不伪装硬件在线。

### M5：产品回归

- 主 Web、FT8、录音、Cloud Hub、Android/iOS（若协议面已覆盖）全链路。
- `session_metrics` 出现 TS-940S 正确分类。
- support bundle 包含 edge/profile 证据。
- README/SDD/version history/doc sync 完成。

## 17. 发布与文档联动

实现 backend 时必须同步：

- `backends/__init__.py` registry
- `config.py` model validation/env defaults
- server connection metadata
- SDD 第 4/7–13/15 章与新 AD
- AGENTS backend table
- README/website model list
- tests for backend contract/profile/no-Hamlib/no external integration
- FT8 runtime capability guard
- scope/device exclusivity test
- support bundle diagnostics

不能只加 ESP32 固件或只在 docs 里描述，却没有进入 `known_models()` 和 MRRC UI。

## 18. 风险

| ID | 风险 | 影响 | 控制 |
| --- | --- | --- | --- |
| TS-R1 | 把 ESP32 做成第二套控制逻辑 | 状态/PTT 双权威 | AD-MRRC-TS-002，边缘协议无产品状态 |
| TS-R2 | IF-10B/PROM/UART 与资料不符 | CAT 不可用 | M0 诊断先放行 |
| TS-R3 | 误用 FT-710 mode/filter fallback | 错误模式或未知命令 | capability + PlatformRig 修正 |
| TS-R4 | 音频二次编码/时钟漂移 | FT8 时序失败 | internal 48k PCM，seq/timestamp |
| TS-R5 | 独立 SDR 服务与 backend 争设备 | waterfall 随机不可用 | device arbitration + backend session |
| TS-R6 | edge 断连但 GPIO 卡低 | 持续发射 | local watchdog + fail-open + TOT/E5 |
| TS-R7 | 物理旋钮轮询太快 | CAT 饱和 | 1–2 Hz 起步，命令优先级 |
| TS-R8 | 未验证表被 UI 当成真值 | 危险操作 | verified=false + TX gate |

## 19. 成功标准

只有同时满足以下条件才算完成：

1. `MRRC_RADIO_MODEL=ts940s` 与现有型号一样被 server 管理。
2. 浏览器和现有原生客户端无需新协议即可控制。
3. FT8 解码/发射由 MRRC runtime 完成。
4. 瀑布由标准 ScopeHandler 输出。
5. 运行机器上没有 Hamlib/SDR++/WSJT-X/虚拟声卡依赖。
6. 所有 PTT 释放层和 edge fail-safe 通过故障注入。
7. 原机改装可拆除复原，接收性能满足验收。

## 20. 当前结论

本方案的“隐藏 ESP32、Wi-Fi 通信、机内 IF SDR、可复原”仍然成立，但产品位置彻底改变：

> **ESP32 是 MRRC 的硬件边缘；MRRC 是唯一的电台软件。**

下一步不是继续选择第三方软件，而是冻结 `backends/ts940s` 接口契约，并为 M0/M1 编写可执行的诊断与测试矩阵。

## 21. 主要资料

1. MRRC `backends/base.py` — backend、audio handler 与 ScopeProducer 契约。
2. MRRC `backends/__init__.py` — model registry 与 `model_connection()`。
3. MRRC `server.py` — audio loop、TX gate、scope producer 与 FT8 composition。
4. MRRC `ft8/platform/rx_tap.py`、`tx_sink.py`、`cat.py`、`runtime.py` — 平台 FT8 链路。
5. MRRC `backends/sunsdr/audio.py`、`scope.py` — 网络音频与 backend-owned spectrum 先例。
6. MRRC SDD 第 8/15 章与 AD-019 — backend 边界、PTT 分层和未验证 TX 门禁。
7. [Kenwood TS-940S Alignment Instructions](http://www.wb4hfn.com/KENWOOD/Docs/TS-940S%20Alignment%20Instructions%20Rev3.pdf) — IF、ACC2 与供电资料。
8. [Piexx IF-10B/C Installation](http://www.radiomanual.info/schemi/KENW_HF/TS-940S_Piexx_IF-10B_board_user_2012.pdf) — Digital A 线束、PROM 和 CAT 参数。
9. [TSP TS-940S SDR panadapter](https://www.tspelettronica.com/en/2020-12-20/panadapter-sdr-per-ts-940s/) — 45.05 MHz 取样先例。
10. [Espressif ESP32-S3 datasheet](https://www.espressif.com/sites/default/files/documentation/esp32-s3_datasheet_en.pdf) — Wi-Fi、I2S、UART、GPIO 与启动毛刺约束。
