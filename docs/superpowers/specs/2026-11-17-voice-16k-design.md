# 16 kHz 主语音架构设计

日期：2026-11-17
状态：已获用户批准，待实施计划
相关 SDD：AD-004、AD-011、NFR-001、NFR-004、NFR-060、NFR-061、NFR-063、NFR-064、UC-002、SC3、SC4

## 1. 背景与问题

当前主语音总线是 48 kHz Int16 单声道，20 ms 一帧（960 样本）。FT-710 在声卡边界做 44.1↔48 kHz 转换，Icom USB 声卡以 48 kHz 直通；Icom LAN 和 SCU-LAN10 收到的原生 16 kHz 音频却先上采样到 48 kHz，再进入 Opus、录音、FT8 tap 和本地扬声器路径。

目标是把主电台语音 RX/TX 的业务域统一为 16 kHz，同时把主语音 Opus 码率从 64 kbps 调整为 32 kbps。选择 16 kHz 的理由是无线电语音不需要 48 kHz 的 24 kHz 音频带宽，且多个电台网络协议已经原生使用 16 kHz。

现有代码与 SDD 中“Opus 必须 48 kHz”的说法不准确。Opus API 支持 8、12、16、24、48 kHz。2026-11-17 使用本仓库的实际 libopus 做了交叉验证：

- 16 kHz 编码 → 48 kHz 解码：每 20 ms 输出 960 样本，1 kHz 测试音频率正确；
- 48 kHz 编码 → 16 kHz 解码：每 20 ms 输出 320 样本，1 kHz 测试音频率正确。

因此，Opus 包的编码输入率与解码器输出率可以不同，新旧 Opus 客户端无需双主链路即可互通。

## 2. 目标与非目标

### 目标

- 主语音服务端 PCM、增益、Opus 编解码及浏览器/Android/iOS 语音处理统一为 16 kHz Int16 单声道。
- 20 ms 主语音帧从 960 样本改为 320 样本，帧周期和包发送频率不变。
- 主语音 RX/TX Opus 统一为 32 kbps；TX 使用 wideband，保留 CBR、complexity=5、FEC/DTX 关闭的现有策略。
- 声卡及电台外部协议在明确边界转换，绝不谎报硬件采样率。
- 新旧 Opus 服务端与客户端双向互通；新服务端正确接纳旧客户端的 PCM48，并为旧客户端提供 PCM48 回退。
- 保持 PTT、TX 队列、抖动缓冲时间水位和断连强制 RX 语义不变。

### 非目标

- 不修改公共 SDR 服务 `/WSsdr` 的 48 kHz 音频契约。
- 不修改 FT8 原生编解码器的 12 kHz RX、48 kHz TX 契约，也不修改 FT8 DSP 原生源码。
- 不修改 FlexRadio 电台侧的 24 kHz 立体声 Opus TX、70 kbps 外部协议。
- 不强迫 USB 声卡、浏览器音频设备或手机音频硬件以 16 kHz 打开。
- 不在本轮引入 DTX、VBR、FEC、动态码率或新的音频编码器。

## 3. 总体决策

建立唯一的 **16 kHz 主语音总线**。采样率是每个音频接口的显式契约，不能由 PCM 字节长度猜测。

三种边界必须分开：

1. **主语音业务域**：16 kHz、Int16、单声道、20 ms。
2. **设备/外部协议域**：声卡 44.1/48 kHz、Flex 24 kHz、FT8 48 kHz 等，由所属适配器转换。
3. **历史线协议域**：PCM48 仅用于旧标签和旧客户端兼容，不再成为主语音处理路径。

直接全局替换 `48000` 为 `16000` 被明确否决：它会把设备率、FT8、Flex 和 legacy PCM 一起改错。

## 4. RX 数据流

```text
电台原生 RX
  → 后端适配器（必要时流式低通重采样）
  → 16 kHz Int16 mono 主语音总线
  ├─ 录音 tap（客户端增益之前，16 kHz 直接记录）
  ├─ FT8 tap（16 → 12 kHz，保留 UTC 时间轴）
  ├─ 本地扬声器（16 kHz → ALSA 设备率）
  └─ 客户端 RX 增益 → 16 kHz/32 kbps Opus → 浏览器/Android/iOS
```

具体要求：

- `AudioHandler.read_rx_chunk()` 返回 16 kHz 主语音 PCM；PyAudio 流仍按后端声明的原生设备率打开。
- `server.py` 的时间戳、录音输入率和 FT8 tap 输入率统一使用主语音常量，不能再从设备率推断。
- `recorder.py` 的存储域本来就是 16 kHz；主语音输入不再经过 48→16 kHz FIR 抽取。
- FT8 `RxTap` 显式以 16 kHz 输入创建，继续由 soxr 输出 12 kHz，并保持现有的缺样重锚和 UTC ring 语义。
- `LocalSpeaker` 改为从 16 kHz 输入转换到 8/48 kHz 等设备率，队列与丢帧策略不变。

## 5. TX 数据流

```text
浏览器/Android/iOS 麦克风
  → 客户端原生率到 16 kHz 的边界转换
  → 16 kHz/32 kbps CBR Opus（或带标签 PCM16）
  → 服务端 16 kHz Opus 解码
  → TX 麦克风电平 → TX 录音 tap
  → 后端 TX 适配器 → 电台原生音频/IQ
```

具体要求：

- `TxOpusDecoder` 以 16 kHz 创建；它能直接解码旧客户端的 48 kHz Opus 包并输出 16 kHz PCM。
- `AudioHandler.feed_tx_audio()` 的默认输入契约改为 16 kHz，并增加 `input_rate: int = 16000` 关键字参数；FT8 必须以 `input_rate=48000` 提交波形，不允许 FT8 波形被误当成 16 kHz。
- CQ 资产在加载时转换为 16 kHz、320 样本/帧，保留现有低水位泵和墙钟排程。
- FT8 TX 继续生成 48 kHz 波形，在设备适配器边界转换；FT8 原生库无需重编。
- Flex TX 在适配器中做 16 kHz 单声道 → 24 kHz 立体声，再使用电台要求的 24 kHz/70 kbps Opus。
- SunSDR 主语音适配器接收 16 kHz；其现有 48 kHz DSP/IQ 调制入口作为后端内部边界保留。
- 麦克风增益、录音顺序、TX 队列预缓冲/上限和 PTT 释放顺序不变。

## 6. 后端边界

| 后端/来源 | RX 边界 | TX 边界 |
| --- | --- | --- |
| FT-710 USB | 44.1 → 16 kHz | 16 → 44.1 kHz |
| IC-7300/MK2 及其他 48 kHz USB | 48 → 16 kHz | 16 → 48 kHz |
| Icom LAN | 原生 16 kHz 直通 | 仍委托声卡；按声卡原生率转换 |
| SCU-LAN10 | 原生 16 kHz 直通；若单元声明其他率则转换 | 维持未实现/禁止发射现状 |
| FlexRadio | 24 kHz 浮点立体声 → 16 kHz Int16 单声道 | 16 → 24 kHz 立体声 Opus，电台协议不变 |
| SunSDR2 DX | 现有解调输出在适配器转为 16 kHz | 16 kHz 到现有 DSP 输入边界转换 |
| FT8 | 主语音 16 → 12 kHz tap | 48 kHz 波形显式进入设备边界 |
| 公共 SDR | 不在本轮范围内 | 不在本轮范围内 |

后端能力中的 `audio_rx_rate`/`audio_tx_rate` 继续描述**设备率**，不能改成主语音率。任何后端都不能仅因主总线变成 16 kHz 就把硬件流打开为 16 kHz。

## 7. 线协议与版本兼容

### 标签

| 标签 | 含义 |
| --- | --- |
| `0x00` | legacy Int16 PCM，48 kHz 单声道 |
| `0x01` | Opus；现有标签不变，包本身不携带 API 输出率 |
| `0x02` | Int16 PCM，16 kHz 单声道 |

### RX

- 服务端始终以 16 kHz PCM 生成主语音 Opus 包，32 kbps；新旧客户端的 Opus 解码器都可接收。
- 新客户端连接 `/WSaudioRX` 后发送文本能力帧：`{"type":"audioCaps","pcmRates":[16000,48000]}`。
- 未发送能力帧的 socket 默认只支持 PCM48。该能力只决定无 Opus 时的裸 PCM 回退，不改变 Opus 主链路。
- 新客户端收到 `0x02` 后按 16 kHz 处理；旧客户端在 PCM 回退时收到由服务端转换的 `0x00` PCM48。
- 若同一批听众同时包含新旧客户端，PCM48 兼容转换每批最多做一次，不按旧客户端数量重复转换。
- 新客户端同时接受 `0x00` 与 `0x02`，因此也能连接旧服务端。

### TX

- `0x01` Opus 统一由服务端 16 kHz 解码器处理，新旧 Opus 客户端都兼容。
- `0x00` 和无标签历史帧按 PCM48 解释并转换到 16 kHz。
- `0x02` 按 PCM16 直接进入主语音总线。
- 双向跨版本保证仅针对 Opus。旧服务端不理解新 PCM16 标签，因此“新客户端 → 旧服务端”的无 Opus PCM 回退不在兼容承诺内；双方须使用 Opus或升级服务端。

认证继续使用现有 cookie/Bearer 握手；能力帧不携带 token，不新增 URL 令牌。

## 8. 浏览器与手机客户端

### 浏览器

- RX 请求 16 kHz `AudioContext`，但以浏览器实际 `sampleRate` 为播放边界；Opus 解码器输出实际所需率，PCM16 在需要时转换。
- TX 采集继续使用真实麦克风 `AudioContext` 率，在 worklet 中经过连续、带低通的转换输出 16 kHz，320 样本/20 ms。
- TX Worker 创建 16 kHz Opus encoder，32 kbps CBR、wideband；测试音生成也使用 16 kHz 相位步进。
- `/listen` 主语音页同步支持 16 kHz 与 legacy PCM48；公共 `/sdr` 页保持不变。
- `static/ft710_main.js` 和 `static/ft710_ui.js` 只能手工最小编辑，禁止 biome/pi-lens 自动重写。

### Android

- 主语音 `SAMPLE_RATE=16000`、`FRAME_SAMPLES=320`、`BITRATE=32000`。
- RX 使用 16 kHz AudioTrack；PCM 解码分支支持 `0x00` 与 `0x02`，不能把未知标签当静音或 PCM48。
- TX 从支持的麦克风原生率转换到 16 kHz；保留 20 ms 帧和现有线程/队列模型。
- 保持 JVM 可测逻辑与 JNI 边界，运行 `test assembleDebug lintDebug`。

### iOS

- Opus 编解码器、采集目标和播放源格式改为 16 kHz，20 ms 为 320 样本。
- 现有 AVAudioEngine 输出格式转换继续承担手机硬件边界。
- TX C bridge 的 28 kbps 历史值统一调整为 32 kbps。
- PCM 分支显式区分 `0x00` 与 `0x02`，保持 PCM 回退可用。

## 9. 重采样与错误处理

- 实时主路径使用有状态的流式重采样器，滤波器和分数相位跨块保存；默认采用现有低延迟 soxr QQ 档，FT8 tap 的 HQ 设置不变。
- 48→16 kHz 必须先低通再抽取，不能逐块线性抽点；44.1↔16 和 24↔16 同样不能用无状态逐块插值。
- 共享转换逻辑归 `audio_resample.py` 或现有后端适配器的明确边界，禁止在事件循环中新增另一套隐式 SRC。
- 流开始、停止、TX→RX 重开和设备重枚举时重置相应转换状态，避免旧尾帧进入新流。
- 输入尾样本保留到下一次调用；20 ms 计数按样本数计算，不按字节数猜测。
- 需要 SRC 而 soxr 不可用或创建失败时，明确记录该路音频不可用；禁止把不匹配采样率的 PCM 直通。
- 原生 16 kHz 路径不创建重采样器。
- PTT release 不增加阻塞查询或额外等待，所有现有强制 RX 路径保持不变。

## 10. 码率与质量预算

- 主语音 Opus：16 kHz、32 kbps、20 ms、50 包/秒，约为当前 64 kbps 的一半，而不是随采样率自动缩减为三分之一。
- PCM16 回退：`16000 × 2 = 256 kbps`，当前 PCM48 为 768 kbps。
- 16 kHz 的 Nyquist 频率为 8 kHz；对 SSB/CW 有足够余量，但撤销“广播级全频带音乐透明”的承诺。
- TX wideband 取代 fullband；CBR、FEC/DTX 关闭、complexity=5 不变。
- Flex 电台侧 24 kHz/70 kbps 与公共 SDR 的独立码率不受主语音常量影响。

## 11. 测试与验收

### 离线测试

- 常量与帧长：16 kHz、320 样本/20 ms、32 kbps；legacy PCM48 仍为 960 样本/20 ms。
- 实际 libopus 交叉矩阵：16→16、16→48、48→16、48→48，校验时长、频率、连续语音和解码错误。
- PCM 标签：RX 能力默认值、PCM16 声明、旧客户端 PCM48 回退、TX 的 `0x00`/`0x02`/untagged 分支。
- 流式 SRC：任意块切分与整块输入等价、跨块相位连续、长时间样本数守恒、抗混叠和重开 reset。
- 每个后端适配器验证统一 `AudioHandler` 表面及实际输出率；特别覆盖 Icom LAN/SCU 原生 16 kHz 直通和 Flex 24↔16。
- CQ、FT8 RX 时间轴、FT8 48 kHz TX、录音时长/内容、本地扬声器速率和音频电平增益。
- 浏览器协议/worklet 测试，Android `test assembleDebug lintDebug`，iOS 构建与 XCTest。
- 完整运行 `python -m unittest discover -s tests -v`，同步 `tests/README.md`。

### 真机与性能验收

- 至少覆盖 IC-7300/MK2 48 kHz USB、FT-710 44.1 kHz USB，以及一种原生 16 kHz 网络音频（Icom LAN 或 SCU-LAN10）。
- 在 W103D 对比改造前后 CPU、事件循环延迟、RX 包率、TX `queue_drops`、浏览器/手机欠载和实际网络码率。
- 正常 RX/TX 语音清晰，无变速、块边界咔哒、持续欠载或 PTT 后长尾；CQ 与 FT8 不回归。
- 新旧 Opus 客户端交叉连接实测；PCM 回退分别验证新客户端 PCM16 和旧客户端 PCM48。
- 未实际覆盖的硬件必须在交付说明中列为未验证，不能用离线测试替代真机结论。

## 12. 文档与约束同步

本设计会有意识地取代 AD-011 的 48 kHz 主语音决策，实施时必须同提交更新：

- SDD AD-004、AD-011、NFR-004、NFR-060、NFR-061、UC-002、9.2/9.3/9.4、组件模型和版本历史；
- `AGENTS.md` 的音频、录音、本地扬声器、FT8 与相关后端说明；
- SDD Guardian 的约束登记，移除“16 kHz anywhere 都是 V1.1 bug”的过时规则，改为“主语音 16 kHz，所有边界必须显式标率”；
- 代码注释和文档中“Opus 必须 48 kHz”的错误陈述；
- `tests/README.md` 的模块与测试计数。

`docs/PROJECT_MAP.md` 所要求的音频速率、协议和架构文档同步属于实现完成条件，不是发布后补记。

## 13. 实施与滚动升级顺序

1. 先建立共享采样率/标签契约、流式 SRC 和协议测试，再改服务端主语音与各后端适配器。
2. 服务端先发：旧客户端继续通过 Opus 互通，未声明能力的 socket 继续使用 PCM48 回退。
3. 浏览器、Android、iOS 随后接入 PCM16 能力和 32 kbps 编码；新客户端连接旧服务端时继续使用 Opus 跨率解码。
4. 最后完成跨版本、PCM 回退、真机与 W103D 性能验收，再更新发布文档。

实现按“共享契约 → 服务端/后端 → 浏览器 → Android/iOS → 端到端验收”划分任务，但必须在同一架构契约下完成，不能各自发明采样率或标签。
