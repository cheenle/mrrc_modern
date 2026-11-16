# ESP32-C3 PCM16 单模音频档位设计

日期：2026-11-17
状态：已获用户批准，待实施计划
相关 SDD：AD-004、AD-008、AD-011、AD-017、AD-025、NFR-001、NFR-004、NFR-060、NFR-063、NFR-064、NFR-065、UC-002、SC3、SC4、R5、R6、I10

## 1. 背景与方向修订

当前服务端主语音总线为 48 kHz Int16 单声道，20 ms 一帧（960 样本）。FT-710 在声卡边界进行 44.1↔48 kHz 转换，IC-7300/MK2 USB 声卡使用 48 kHz；Icom LAN、SCU-LAN10 等原生 16 kHz 来源也先转换到该总线。`/WSaudioRX` 与 `/WSaudioTX` 当前只定义两种帧：`0x00 = PCM48`、`0x01 = Opus48`。

最初曾设计“服务端、浏览器和手机全部改成 16 kHz，并将 Opus 降为 32 kbps”的全局方案。用户随后明确实际目标是降低 ESP32-C3 客户端资源占用，因此本文**整体取代该全局方案**：

- 服务端主总线继续维持 48 kHz；
- 浏览器、Android、iOS 的现有 Opus48/PCM48 行为保持不变；
- 新增仅供声明能力的客户端使用的 PCM16 档位；
- ESP32-C3 使用 **PCM16 单模固件**，彻底移除 libopus，不提供 Opus48 自动回退；
- 主语音 Opus 码率不在本轮调整。

该方案复用 AD-025 的核心兼容原则：能力按**音频 socket**协商，未声明能力的 socket 保持原有字节行为。

## 2. 目标与非目标

### 目标

- ESP32-C3 以 16 kHz、Int16、单声道、20 ms PCM 与服务端双向通信。
- PCM16 网络负载为每方向 256 kbps，典型帧为 `1 字节标签 + 640 字节 PCM`。
- 旧 Web、Android、iOS 客户端无需修改，默认继续使用 Opus48，Opus 不可用时使用 PCM48。
- PCM16 下行只在存在已声明听众时计算；只有 PCM16 听众时跳过 Opus 编码。
- PCM16 上行显式携带采样率，经后端边界直接转换到电台设备率。
- ESP32-C3 可移除 Opus 编解码器及 48 kHz 音频缓存；实际 RAM/Flash 收益由固件实测确认。
- 保持 TX 所有权、PTT 时序、队列水位、录音、断连强制 RX 等安全语义不变。

### 非目标

- 不把服务端内部总线、浏览器或手机客户端改成 16 kHz。
- 不调整 Opus 码率、VBR、CBR、FEC、DTX 或帧周期。
- 不为 PCM16 单模固件提供 Opus48 兼容层。
- 不修改公共 SDR `/WSsdr`、FT8 原生编解码器或 FlexRadio 外部音频协议。
- 不承诺未经固件 map、堆高水位和 Flash 统计验证的具体内存节省数字。

## 3. 方案选择

采用“**48 kHz 服务端主总线 + 按音频 socket 协商的 PCM16 档位 + 自描述 PCM16 帧**”。

否决另外两种方案：

1. **在 `/WSradio` 会话上绑定 `audioProfile`，并复用 `0x00` 表示 PCM16**：同一 30 天会话可拥有多个音频 socket、页面和客户端，控制 socket 无法可靠代表每个音频 socket；同一标签按连接改变含义也容易在日志、代理和后续客户端中误读。
2. **全局替换为 16 kHz**：超出嵌入式客户端的实际需求，会无谓改动所有后端和现有客户端。

## 4. 线协议

### 帧标签

| 标签 | 含义 | 接收方 |
| --- | --- | --- |
| `0x00` | Int16 PCM，48 kHz 单声道 | 现有及新客户端的 legacy 路径 |
| `0x01` | Opus，现有 48 kHz 解码路径 | 现有客户端 |
| `0x02` | Int16 PCM，16 kHz 单声道 | 已声明 PCM16 的客户端 |

PCM16 的典型 20 ms 帧总长为 641 字节。服务端解析时仍以 Int16 对齐和实际帧长为准，不能只用“载荷等于 640 字节”推断采样率。

### 能力请求

ESP32-C3 在 `/WSaudioRX` 和 `/WSaudioTX` 建连后分别发送文本帧：

```json
{"type":"audioCaps","profile":"pcm16"}
```

能力属于当前 socket，不属于 token 或 `/WSradio` 会话。服务端分别保存 RX/TX profile，socket 断开时清理。未发送该帧、JSON 无效或 profile 未知时，socket 维持 legacy，不改变任何现有行为。

### 能力应答

RX 支持 PCM16 时，服务端在同一 socket 应答：

```json
{"type":"audioCaps","profile":"pcm16","accepted":true,"rate":16000,"channels":1,"format":"s16le","frameMs":20}
```

TX 只有在当前音频后端支持 PCM16 输入转换时才返回同样的成功应答；否则返回：

```json
{"type":"audioCaps","profile":"pcm16","accepted":false,"reason":"pcm16-tx-unsupported"}
```

能力应答只使用当前已认证的 WebSocket，不携带令牌，也不新增 URL 参数。

建议将纯解析和应答构造放在无应用依赖的 `audio_profile.py`，与 `spectrum_profile.py` 一样可独立单测；服务端只负责 socket 状态和 fan-out。

## 5. ESP32-C3 单模状态机

### RX

1. 建连并发送 PCM16 能力请求。
2. 最长等待 2.0 秒成功应答；协商期间不进入正常播放状态，到达的任何二进制音频帧都直接丢弃。新服务端也可能在能力帧被处理前发出旧格式帧，因此早到的 `0x01`/`0x00` 不能作为判定旧服务端的依据。
3. 协商成功后收到 `0x02`，按 16 kHz Int16 单声道播放，不创建 Opus 解码器或 48 kHz 缓冲；此后若收到 `0x01`/`0x00`，按服务端协议错误处理。
4. 2.0 秒内无成功应答时报告“服务端需要升级”并停止该路音频。

能力切换前已经在途的旧帧可以丢弃，不得当作 PCM16 播放。

### TX

1. 建连并发送 PCM16 能力请求。
2. **收到 TX 成功应答之前，不允许发送麦克风帧，也不允许为该音频会话按下 PTT。**
3. 2.0 秒无应答、明确拒绝或 socket 断开时，报告“服务端需要升级/当前后端不支持 PCM16 TX”，保持 RX，不产生静音载波。
4. 协商成功后仅发送 `0x02` PCM16 帧。
5. PTT 释放、socket 断开和重连继续走现有安全路径；每次新 socket 都必须重新协商。

PCM16 单模固件不包含 Opus48 双模降级逻辑。旧服务端上的失败是明确、可诊断的兼容错误，不是自动切换编码器。

## 6. RX 服务端设计

现有 RX 主路径保持：

```text
后端原生 RX → 现有边界转换 → 48 kHz 主总线 → 录音 / FT8 / 本地扬声器
```

网络 fan-out 按 socket profile 分为两组：

### Legacy 组

- 继续调用现有 Opus48 编码路径并接收 `0x01`。
- libopus 不可用时继续接收 `0x00` PCM48。
- 帧内容和时序保持原样。

### PCM16 组

- 仅当至少存在一个 PCM16 socket 时，创建/启用一条 48→16 kHz 连续降采样器。
- 每个音频 tick 对同一份主总线 PCM 最多降采样一次，所有 PCM16 socket 共享结果。
- 发送 `0x02 + PCM16`，不发送 Opus48 或 PCM48。
- 只有 PCM16 听众时，不为网络发送执行 Opus 编码；录音和 FT8 需要音频本身并不等于需要网络 Opus。
- 混合听众时，legacy Opus48 与共享 PCM16 各生成一次，不按客户端数量重复计算。
- 最后一个 PCM16 socket 断开后停用转换；下次启用时重置状态，避免旧滤波尾帧进入新订阅。

48→16 kHz 必须先低通再 3:1 抽取，不能直接丢弃每三个样本中的两个。实时转换采用有状态流式实现，滤波历史跨块保存。

若转换器不可用或运行中失败，服务端以 WebSocket 关闭码 4004、原因 `PCM16 resampler unavailable` 关闭 PCM16 socket，并在现有 `finally` 清理中移除；不得把 48 kHz PCM 改贴 `0x02` 标签，也不得影响 legacy 组。

## 7. TX 服务端设计

### 统一输入率接口

将服务端上行入口改为显式采样率：

```python
_feed_tx_from_uplink(pcm, input_rate=48000)
audio.feed_tx_audio(pcm, input_rate=48000)
_rec_tap_tx(pcm, rate=48000)
```

默认值 48000 保持浏览器、CQ、FT8 和现有客户端行为。PCM16 分支传 `input_rate=16000`。

处理顺序保持：

```text
带率 PCM → TX 麦克风电平 → TX 录音 tap（同一输入率） → 后端设备率转换 → TX 队列
```

电平算法与采样率无关；录音模块已经接受 `source_rate=16000`，可直接写入 16 kHz 存储域。

### 后端边界

- FT-710：16→44.1 kHz 一步转换，不经过中间 48 kHz。
- IC-7300/MK2 等 48 kHz USB：16→48 kHz 插值，包含合适低通滤波。
- Icom LAN 的委托声卡 TX：把 `input_rate` 透传给实际 PyAudio handler。
- FlexRadio：16 kHz 单声道到其 24 kHz 立体声外部协议，在 Flex 适配器内完成。
- SunSDR2 DX：16 kHz 到其现有 DSP 输入率，在 SunSDR 适配器内完成。
- SCU-LAN10：维持当前禁止 TX 的行为，不应答 PCM16 TX 成功。

所有允许 TX 的音频适配器必须接受显式 `input_rate`；尚未正确实现的后端不得声明 PCM16 TX 成功。legacy 默认 48 kHz 路径必须保持原行为。

### 队列与 PTT

- TX 预缓冲 60 ms、硬上限 400 ms、释放时 50 ms 优雅排空不变。
- 队列字节水位按实际设备率计算，不按网络输入率计算。
- 上行所有权仍按 socket 仲裁，PCM16 profile 不改变 owner、PTT key socket 或同会话替换规则。
- 非 owner 的 PCM16 帧继续丢弃；owner 断开、PTT 释放、服务关闭等强制 RX 路径不变。
- 协商状态不属于 PTT 释放依据；任何 profile 下释放都必须被接受。

## 8. 重采样与故障处理

- RX 48→16、TX 16→44.1/48/24 使用有状态、带低通的流式重采样；不能逐块无状态插值或简单样本重复。
- 复用项目现有 soxr 依赖和 `audio_resample.py` 的统一边界；低延迟实时路径固定使用 soxr QQ 档，并通过块边界、群延迟和抗混叠测试。
- 每次新 RX 订阅组、TX stream、TX→RX 重开和设备重枚举时重置相应状态。
- 任意输入块长度都要保留尾样本；20 ms 时长按样本数计算。
- 需要转换但 soxr 不可用/失败时明确拒绝 PCM16 profile，绝不直通不同采样率的 PCM。
- legacy 48 kHz 路径不因新增转换器失败而改变。
- PCM16 网络带宽高于 Opus48：每方向 256 kbps，部署到公网/Cloud Hub 时需要实测 Wi-Fi、上行和隧道余量。

## 9. ESP32-C3 资源要求

固件侧只保留 PCM16 路径：

- 移除 libopus 链接、Opus 状态对象及相关伪栈/工作区。
- 移除不再需要的 48 kHz 静态 PCM 缓冲和 48↔16 本地重采样。
- RX 直接消费 640 字节/20 ms PCM，TX 直接生成相同格式。
- 只接受 `0x02`；把其他标签视为明确协议/版本错误。
- 保留有界 jitter buffer 和 TX 队列，按 16 kHz 样本数计算水位。

验收时对比同一固件构建配置下的 map 文件、静态 RAM、Flash、最小空闲堆和运行高水位。不得以“代码里删除了 Opus”代替内存实测。

## 10. 测试与验收

### 服务端离线测试

- `audioCaps` 解析、默认值、无效 JSON、未知 profile、成功/失败应答。
- 同一 token 下两个音频 socket 使用不同 profile，验证不会串扰；断连后状态清理。
- 未声明 socket 的 Opus48/PCM48 帧保持原有格式。
- PCM16-only、legacy-only、混合听众 fan-out；PCM16 每拍最多一次降采样，PCM16-only 不调用 Opus 编码。
- `0x02` TX 的 16 kHz 增益、录音和设备率转换；`0x00`、Opus48、CQ、FT8 保持 48 kHz 默认输入。
- 任意块切分、跨块连续性、长时间样本数、抗混叠、转换器 reset 和失败拒绝。
- 非 owner 丢帧、owner 断开强制 RX、PTT 释放及现有音频队列回归。
- 完整运行 `python -m unittest discover -s tests -v`，并同步 `tests/README.md`。

### ESP32 与端到端验收

- PCM16 固件不再链接 libopus，提供 map/Flash/RAM 对比和最小空闲堆数据。
- 新服务端 RX/TX 均为正确 16 kHz 时长与音调，连续运行无变速、咔哒、队列增长或明显掉帧。
- 旧服务端无能力应答时固件明确报错；TX 未协商成功时绝不 key-up。
- 旧浏览器/Android/iOS 与 ESP32 同时在线时各自收到正确格式，互不影响。
- 至少实测 FT-710 44.1 kHz 和 IC-7300/MK2 48 kHz 的 TX 边界；其他后端若未上真机必须标为未验证。
- 在目标 Wi-Fi/Cloud Hub 路径测量双向 256 kbps 下的丢包、延迟和稳定性。

## 11. 文档同步与实施顺序

实施时新增 **AD-037：ESP32-C3 PCM16 单模音频档位**，修订 AD-004 的标签表和 AD-025 的复用说明；AD-011 的 48 kHz 主总线决策继续有效。同步 NFR-004、SDD 9.2/9.3/9.4、UC-002、组件模型、版本历史、`AGENTS.md` 和测试说明。代码注释必须明确 `0x02` 的采样率，不能再写“所有 PCM 都是 48 kHz”。

实施顺序：

1. `audio_profile.py`、标签常量与协商/兼容测试。
2. RX 分组 fan-out、共享 48→16 转换和 PCM16-only 编码跳过。
3. TX 显式输入率、录音和各后端边界。
4. ESP32-C3 PCM16 单模状态机与资源实测。
5. 混合客户端、旧服务端拒绝、PTT 安全和真机端到端验收。

服务端可以先发布：旧客户端不变，未升级的 PCM16 单模固件会得到明确升级提示。不得在协商协议未实现前发布只能使用 PCM16 的固件。
