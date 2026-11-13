# ESP32 手持终端设计（MRRC Modern 客户端）

> **状态**：草案，2026-11-13，待研讨。
> **一句话**：一台拿在手上的操作终端 —— 旋钮调谐、2.4 寸瀑布、耳机听音、按键发射。
> **两条硬约束**：不动服务端协议（唯一例外是 RX 码率一个环境变量）；全部用现成模块，不打 PCB。

## 1. 目标与非目标

**要做**

| 能力 | 走哪条通道 |
| --- | --- |
| 频率 / 模式 / 滤波 / 记忆信道 / AF 音量 | `/WSradio` 控制面 |
| 真频谱瀑布 | `/WSspectrum`（先声明 `spectrumCaps` 降档） |
| RX 听音（耳机） | `/WSaudioRX`，Opus 48 kHz 单声道 |
| TX 语音（内置麦克风） | `/WSaudioTX`，Opus 48 kHz 单声道 |
| PTT（含安全释放链） | `/WSradio` 的 `ptt` + `txhb` |

**不做**（明确划掉，避免范围蔓延）

- 录音、CQ 一键呼叫、ATR1000 天调、FT8 操作 —— 这些是桌面端能力，手持终端不承担。
- 角色：只支持 `admin` 会话。`listen` 角色的只读页面已由浏览器端覆盖。
- 不做 SDR 接收页（`/WSsdr`）。

## 2. 协议事实（全部来自现有实现，非推测）

这一节是固件唯一的接口契约。所有行号对应 2026-11-13 的仓库状态。

| 事实 | 依据 |
| --- | --- |
| 登录：`POST /api/auth/login {"password":…}` → `{"ok":true,"token":…,"role":"admin"}` | `server.py:5006` |
| WebSocket 认证支持 `Authorization: Bearer <token>`，优先级高于 cookie 与 `?token=` | `server.py:1282` `_token_from_ws_handshake` |
| 控制消息：`{"type":"set","field":"freq","value":14074000}`；心跳 `{"type":"ping"}`→`{"type":"pong"}` | `server.py:2841` |
| 首帧是 `{"type":"fullState","data":{…}}`，`data` 就是 `RadioState.to_dict()` | `server.py:5737` |
| 频谱帧：`0x01 + wf1(850) + wf2(850)` = 1701 B；声明后可用 851 B 短帧 | `spectrum_profile.py:44-48` |
| 频谱默认 `high` 档；不发 `spectrumCaps` 就永远是 1701 B | `spectrum_profile.py:29-33` |
| RX 音频帧：`1 字节 tag + 包`，`0x01` = Opus，`0x00` = 裸 PCM | `opus_rx.py:53-54` |
| RX 编码器固定 48 kHz 单声道、64 kbps；**码率档位尚未实现** | `opus_rx.py:69-72`、`audio_handler.py:202` |
| TX 上行同时接受三种格式：`0x01`+Opus、`0x00`+PCM、无 tag（按 PCM） | `server.py:5970-5983` |
| TX 解码器固定 **48 kHz** 单声道（编码端不能用别的采样率） | `opus_rx.py:199-207` |
| TX PCM 契约：48 kHz、Int16、单声道；服务端自己重采样到设备率 | `audio_handler.py:1096-1108` |
| PTT 键控时按 token 重新分配 TX 上行所有权（谁按键谁拿麦克风） | `server.py:3408`、`server.py:445` |
| `txhb` 是**能力声明**：发过一次，该会话此后受活性闸门监控，直到超时或断连 | `server.py:2883-2887` |
| 闸门只盯持有 PTT 的会话；阈值 `MRRC_REMOTE_SESSION_TX_HEARTBEAT_S`，Cloud Hub 部署为 **5 s**，检测粒度 0.25 s | `server.py:286-330`、`mrrc_hub/deploy/wire_instance_env.sh:32` |
| 超时后果：强制 RX + 广播 `Link lost while transmitting`，并把该 socket 移出监控集合 | `server.py:314-328` |
| `ptt:true` 本身会刷新时钟（前提是已声明能力），首次按键不受影响 | `server.py:3425-3427` |
| 键控会话断开 → 服务端强制 RX（最后一道防线，非唯一防线） | `server.py:5803-5820` |

**由这些事实直接推出的三个设计决定**

1. **四条 WS 必须共用同一个 token**。`_claim_tx_owner_for_token` 按 token 匹配上行 socket，分两次登录会让 PTT 拿不到自己的麦克风。
2. **TX 阶段裸 PCM 作为第一台阶是合法的**：服务端原生支持 `0x00` 帧，固件只需在 48 kHz Int16 块前加一个字节。它的代价是带宽（见 §5），不是兼容性。
3. **`txhb` 不能省**：Cloud Hub 实例阈值 5 s 已写死，客户端 1.5 s 一次心跳，弱网下才不至于被打断载波。

## 3. 硬件（模块化，零 PCB）

全部件淘宝可得，用一块洞洞板做母板把模块焊死，杜邦线只用于调试期。
不做 PCB 的代价是体积和一致性，收益是一次改版不需要重新打样。

### 3.1 BOM

| # | 模块 | 规格 | 数量 | 参考价 | 备注 |
| --- | --- | --- | --- | --- | --- |
| 1 | 主控 | **ESP32-S3-DevKitC-1 N16R8** | 1 | ¥60 | 官方板，USB-C，2.54 排针 |
| 2 | 屏 | 2.4" **ST7789 320×240** SPI IPS | 1 | ¥30 | 挑带 BL 引脚的 |
| 3 | 音频 | **ES8311 模块**（I2S + I2C，板载 MIC 座 + 3.5 mm） | 1 | ¥22 | 单芯片 codec，收发一体 |
| 4 | 咪头 | 6 mm 全指向驻极体 | 1 | ¥3 | 插模块 MIC 座 |
| 5 | 编码器 | **EC11** 带按压（5 脚） | 1 | ¥5 | 必须配 2×100 nF |
| 6 | 按键 | 6×6 轻触 4 只 | 4 | ¥4 | PTT 用带帽的 |
| 7 | 电池 | 1S LiPo **2500 mAh**（带保护板 + JST-PH2.0） | 1 | ¥25 | |
| 8 | 充电 | **TP4056 Type-C**（DW01 保护版） | 1 | ¥3 | |
| 9 | 升压 | **MT3608** 5 V 模块 | 1 | ¥4 | 见 §3.3 |
| 10 | 开关 | SS12D00 拨动 | 1 | ¥2 | 总电源 |
| 11 | 母板 | 5×7 cm 双面镀锡洞洞板 | 1 | ¥3 | 模块焊在此 |
| 12 | 线材 | 20 AWG + 少量杜邦 | 1 | ¥10 | |
| | | | | **≈¥170** | |

### 3.2 引脚分配

N16R8 是 **octal PSRAM**：GPIO 26-32 归 flash、**33-37 归 PSRAM**，内部硬连线，外部不可用。
strap 脚 0/3/45/46、USB 19/20、UART0 43/44 一并避开。

| 功能 | GPIO | 说明 |
| --- | --- | --- |
| SPI2 屏 | SCLK 12 / MOSI 11 / CS 10 / DC 9 / RST 8 / BL 7 | BL 走 LEDC PWM |
| I2S0 ES8311 | MCLK 4 / BCLK 5 / LRCK 6 / DIN 16 / DOUT 15 | 单路全双工，MCLK = 12.288 MHz |
| I2C ES8311 | SDA 1 / SCL 2 | 地址 0x18（ADDR 接地） |
| EC11 | A 38 / B 39 / 按压 40 | 38-42 默认是 JTAG，用作 GPIO 后失去 JTAG |
| 按键 | PTT 41 / MODE 42 / BAND 47 / BACK 48 | |

### 3.3 电源

```
LiPo 3.7V ── TP4056(充+保护) ──┬── SS12D00 ── MT3608(升 5V) ── DevKitC 5V 脚
                               │                                 └ 板载 LDO ── 3V3 ── 屏 / ES8311 / 上拉
                               └── 电池 JST
```

走 DevKitC 的 5 V 脚、由板载 AMS1117 出 3.3 V，是零风险路径。
**不要把外部 3.3 V 灌进 `3V3` 脚**——那是 LDO 的输出端，反灌会打架。
代价是 5V→3.3V 有约 0.7 W 发热；要消除就去掉 LDO（换最小系统板）+ TPS63020 升降压，但多一个变量。

电流预算：Wi-Fi 峰值 350 mA + 屏 60 mA + ES8311 30 mA ≈ **440 mA @3.3 V**
→ 2500 mAh 约 RX 9-10 h / 持续 TX 4-5 h。

### 3.4 七个会咬人的点

1. **GPIO 33-37 不可用**。Web 上大量 ST7789 教程用 GPIO 37 做 RST，在这块板子上必然不亮。
2. **EC11 必须硬件去抖**（A/B 各 100 nF 到地）。纯软件去抖在快速旋转时丢步。
3. **PTT 键独立上拉 + 100 nF**。这是能烧功放的键，一次误触就是一次空载发射。
4. **ES8311 的 MCLK 必须给**（12.288 MHz）。省掉 MCLK 的模块音质与采样稳定性都会掉。
5. **ES8311 的 I2C 地址由 ADDR 脚决定**：接地 0x18，接 3.3 V 0x19。模块上有跳线，先量再焊。
6. **屏的 BL 一定要接**：夜里 100% 亮度刺眼，PWM 调光是必需项。
7. **板载 PCB 天线被手挡住会掉包**。手持形态下这是真实问题，优先选带 IPEX 座的模块。

## 4. 软件架构

ESP-IDF 5.x + 组件管理器。层次按"能单独测"切分，每层只依赖下一层的接口。

```
app_main
├── net_link     登录 + 四条 WS 的统一重连状态机（共用 token）
├── proto        JSON 编解码，fullState → RadioSnapshot
├── audio_rx     tag 0x01 → Opus 解码 → 抖动缓冲 → I2S DAC
├── audio_tx     I2S 麦克风 → 960 样本块 → Opus 编码 → tag 0x01
├── ptt          PTT 状态机（心跳 / 看门狗 / 释放序列）
├── ui           旋钮事件 + 局部重绘
└── state        单一快照，server → UI 单向
```

**一条硬规则**：`ui` 永远不直接发 WebSocket 消息，它只产生事件（`tune(delta)`、`ptt_press()`），
由 `ptt` / `net_link` 决定发什么。这样 UI 可以在没有网络的情况下单测。

现成组件：

| 功能 | 组件 |
| --- | --- |
| 屏驱动 | `espressif/esp_lcd_st7789` |
| GUI | `lvgl/lvgl` v9，或手写局部重绘（省约 200 KB） |
| 音频 codec | `espressif/esp_codec_dev`（含 ES8311 驱动） |
| Opus 编解码 | `espressif/esp_audio_codec` |
| WebSocket | `espressif/esp_websocket_client` |
| TLS | `esp_crt_bundle`（Cloud Hub 边缘是 LE 证书） |
| JSON | `jsmn` |
| 凭据 | NVS |

## 5. 音频管线

### 5.1 下行（RX）

```
/WSaudioRX 帧 = [0x01][Opus 包]
  → opus_decode(48k mono, 20 ms, 960 samples)
  → 抖动缓冲（目标 200 ms，弱网可调到 400 ms）
  → I2S0 TX → ES8311 → 耳机
```

丢包用 Opus PLC（`opus_decode(NULL) → 120 ms 外推`），不要填静音——静音听感上比 PLC 更糟。

### 5.2 上行（TX）

两个台阶，第一台阶先跑通"能说话"。

**台阶 A（阶段 4）**：裸 PCM

```
ES8311 ADC (I2S, 48 kHz/16 bit) → 960 样本/20 ms → [0x00] + 1920 B → /WSaudioTX
```

服务端原生支持，零编码器风险。带宽 96 KB/s，仅适合局域网与家宽。

**台阶 B（阶段 6）**：Opus

```
ES8311 ADC → 20 ms 帧 → esp_audio_codec OPUS 编码器（48 kHz mono, 20 kbps）
  → [0x01] + 包 → /WSaudioTX
```

`esp_audio_codec` 的 OPUS 编码器参数范围：采样率 8/12/16/24/48 kHz，单/双声道，
位率 **20-510 kbps**，帧长 2.5-120 ms，复杂度 0-10，支持 FEC/DTX/VBR。
服务端要求 48 kHz 单声道，取 20 kbps + 20 ms + VOIP 模式即可。

### 5.3 带宽账

| 路径 | 编码 | 码率 | 每小时 |
| --- | --- | --- | --- |
| RX（现状） | Opus 48k | 64 kbps | 28.8 MB |
| RX（降档后） | Opus 48k | 24 kbps | 10.8 MB |
| TX 台阶 A | PCM 48k/16b | 768 kbps | **345 MB** |
| TX 台阶 B | Opus 48k | 20 kbps | **9 MB** |

裸 PCM 与 Opus 差 38 倍：每天通联 30 分钟，前者 5 GB/月、后者 108 MB/月。
这就是"出门必须走台阶 B"的全部理由。

### 5.4 需要服务端配合的唯一一处

RX 下行码率是**全局单例**（`audio_handler.py:202` 一个 `RxOpusEncoder(64000)` 供所有监听者共享），
`opus_rx.py:69-72` 的注释自己写着码率档位尚未实现。
最小改动：新增 `MRRC_RX_OPUS_BITRATE`（默认 64000），语音降到 24000 对浏览器听众几乎无损。
更大改动：按客户端协商一个 `audioCaps`（对标 `spectrumCaps`），本轮不做。

## 6. PTT 安全

```
IDLE ──按下──► KEYING    : ptt:true → 100 ms 后 txhb → 每 1.5 s txhb → 开始推音频
KEYING ──松开──► RELEASING : 停推帧 → 发 "s:" → ptt:false → 200 ms → IDLE
KEYING ──180 s──► FAILSAFE : ptt:false，锁止到完全松开再按
KEYING ──心跳连续 2 次失败──► FAILSAFE : ptt:false → 触发重连
```

三条铁律：

1. **任何退出路径先发 `ptt:false`**，包括异常、看门狗复位、重连前。
2. **只有 `tx_status == 1` 才推麦克风帧**，避免未键控时把音频灌进服务端队列。
3. **服务端的拒绝要显示出来**：FT8 引擎发射中（约 13 s）、CQ 呼叫进行中，两种情况服务端都会
   回 `{"type":"error"}` 并拒绝键控，固件必须提示并回到 IDLE，不能停在"我以为在发射"的状态。

心跳参数：周期 1.5 s（阈值 5 s 的 1/3），连续 2 次发送失败即视为链路已断。

## 7. 网络与认证

| 场景 | 地址 |
| --- | --- |
| 局域网 | `ws://<ip>:8888/WSradio` 等直连 |
| Cloud Hub | `wss://www.vlsc.net/mrrc_modern/<呼号>/WSradio`（前缀路径） |

- 一次 `POST /api/auth/login` 拿 token 存 NVS，四条 WS 共用；token 失效（4001）时重登一次。
- TLS 用 `esp_crt_bundle`，不固定自签证书——Cloud Hub 边缘是真证书。
- 直连自签盒子时必须导入其证书；**不允许跳过校验**：能按 PTT 的连接被人冒充，等于把发射权交出去。
- 四条连接统一状态机：指数退避 1→30 s，**全部连上才算在线**，避免"控制连上了、音频没连上"。
- 保活：`{"type":"ping"}` 每 25 s 一次（四条共用一条定时器）。
- 频谱连上先发 `{"type":"spectrumCaps","profile":"mid"}`：850 bins 不变，广播频率减半。

## 8. 资源预算（S3 @240 MHz，双核）

| 项 | 堆 | CPU |
| --- | --- | --- |
| 4× TLS（`SSL_IN/OUT_CONTENT_LEN=2048`） | 32 KB | ~10% |
| Opus 解码 48k mono | 30 KB | ~12% |
| Opus 编码 48k mono @20 kbps | 30 KB | ~45%（独立核） |
| 抖动缓冲 400 ms | 8 KB | — |
| 瀑布历史 320×170 | PSRAM | ~15% |
| UI + 旋钮 | — | ~8% |
| **合计** | **~100 KB SRAM**（可用 320 KB） | **核心 0 ~45%，核心 1 ~45%** |

编码独占 core 1，UI/协议/解码留在 core 0。这也是"两个核"而不是"更强单核"的理由。

## 9. 分阶段交付

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| 1 | 控制面：登录、`/WSradio`、旋钮调谐、频率/模式显示 | 局域网内转旋钮，电台频率跟着变 |
| 2 | 频谱：`/WSspectrum` + 瀑布 + S 表 | 与浏览器同时观察，形状一致 |
| 3 | RX 音频：Opus 解码 + 抖动缓冲 + 耳机 | 戴耳机听 30 分钟不断续 |
| 4 | TX 音频（裸 PCM）+ PTT 状态机 + `txhb` | 对着电台说话，对方能收到；松手立刻停 |
| 5 | Cloud Hub：TLS + 前缀路径 + 重连 | 用 4G 热点连回家里的电台 |
| 6 | TX 换 Opus 20 kbps + 服务端 RX 降 24 kbps | 一小时通联流量 < 20 MB |

每阶段都能单独上机验证，不会出现"全写完才发现声音不对"。

## 10. 开放问题（研讨用）

| # | 问题 | 选项 | 倾向 |
| --- | --- | --- | --- |
| Q1 | RX 码率怎么降 | ① 全局 `MRRC_RX_OPUS_BITRATE`（一行）② 按客户端 `audioCaps` 协商（对标 spectrumCaps） | ①，②留到有人抱怨 |
| Q2 | TX 用 20 kbps 固定还是 VBR | 固定稳、VBR 省流量但抖动 | 固定 20 kbps + DTX |
| Q3 | 频谱档位 | `mid`（divider 2）还是 `low`（divider 4） | `mid`，屏只有 320 px 宽 |
| Q4 | Wi-Fi 配网 | ① SmartConfig ② 开软 AP + 网页 ③ 编译期写死 | ②，与盒子 `/settings` 同款体验 |
| Q5 | 电池 | 2500 mAh（偏重）还是 1500 mAh（轻 60 g） | 2500，TX 功耗高 |
| Q6 | 与浏览器同时在线 | 现有按 token 仲裁已覆盖，是否需要额外优先级 | 不需要 |
| Q7 | 旋钮手感 | EC11 有段落 vs 无段落（光学编码器） | 有段落，电台调谐的肌肉记忆 |
| Q8 | 是否显示 FT8 解码结果（只读） | 做 / 不做 | 不做，第一版 |

## 11. 风险

| 风险 | 影响 | 应对 |
| --- | --- | --- |
| ES8311 驱动的时钟/位宽配错 | 无声或噪音 | 先用 `esp_codec_dev` 的 ES8311 示例做通，再接业务 |
| 4 条 TLS 在 S3 上内存不足 | 连接随机断开 | `SSL_IN/OUT_CONTENT_LEN=2048`；仍不足则改方案 2（服务端 `/WSdevice` 合流） |
| 弱网抖动打断载波 | 通话被切断 | 心跳 1.5 s；抖动缓冲 200-400 ms 可调 |
| 手持挡天线 | 掉包、断线 | 选 IPEX 版本 + 外置天线 |
| 裸 PCM 阶段误用于公网 | 流量爆表 | 阶段 4 仅在局域网验证；台阶 B 之前不发公网 |
