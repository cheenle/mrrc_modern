# FT710Mobile iOS Opus 集成现状与 TX 启用指南

**基准日期**: 2026-07-20(替代 2026-07-14 旧版集成指南,旧版"待链接 libopus"等结论已过期)
**核实方法**: 全部结论经实际 Read 核对(iOS 侧 `OpusBridge.c/h`、`OpusDecoder/OpusEncoder.swift`、`AudioPlaybackManager/AudioCaptureManager.swift`、`project.yml`;服务端 `opus_rx.py`、`audio_handler.py`、`server.py`;web 侧 `tx_opus_worker.js`、`modules/opus_codec.js`),附 file:line 证据。

---

## 1. 集成现状

### 1.1 C 桥:已完成并链接进工程 ✅

- `FT710Mobile/Sources/Audio/OpusBridge.c` / `OpusBridge.h` 存在且完整:
  - `my_create_encoder`(`OpusBridge.c:15-31`):48kHz/单声道/`OPUS_APPLICATION_VOIP`,固定 28kbps CBR(`:26-27`),`frameSize` 参数从未使用(`:15`)。
  - `my_opus_encode`(`:43-48`)、`my_create_decoder`(`:50-61`)、`my_opus_decode`(`:73-78`)、`my_destroy_encoder/decoder`(`:33-41`/`:63-71`)。
- Swift 侧用 `@_silgen_name` 直接绑定 C 符号(无 bridging header):`OpusDecoder.swift:42-49`、`OpusEncoder.swift:43-50`。
- **libopus 已链接进工程**(旧文档的 P0 待办项,实际已完成):`project.yml:41-43` 配置 `HEADER_SEARCH_PATHS` / `LIBRARY_SEARCH_PATHS` / `OTHER_LDFLAGS: "-lopus"`,静态库位于 `Sources/Audio/opus-ios/lib/libopus.a`。

### 1.2 RX(服务端 → iOS):Opus 真实在用 ✅

- 服务端 `audio_handler.py:91` 实例化 `RxOpusEncoder(bitrate=DEFAULT_BITRATE)`(默认 64kbps,`opus_rx.py:67`),`encode_rx_audio`(`audio_handler.py:283-285`)输出带 1 字节 tag 的帧,经 `/WSaudioRX` 广播(`server.py:1711-1734`)。
- iOS `AudioPlaybackManager.swift:110-128`:按帧首字节 tag 分发——`0x01` 走 `opusDecoder.decode()`(`:114`)解码为 Int16 PCM 后 `processPCM` 播放;`0x00` 直通 PCM。解码失败计数并在 10 次时告警(`:117-122`)。
- 即:iOS 客户端当前的 RX 音频就是经 libopus 解码的 Opus 流,这不是"待集成"而是"已运行"。

### 1.3 TX(iOS → 服务端):恒为 PCM,Opus 链路整体死代码 ⚠️

- `AudioCaptureManager.swift:20` `var useOpus: Bool = false`,**全项目无任何置 true 的路径** → TX 永远走 `:130-139` 的 PCM 分支(tag `0x00`,~768kbps)。
- Opus 编码分支(`:122-129`,tag `0x01`)与 `OpusEncoder.swift` 整体为死代码。
- 服务端接收侧已就绪:`server.py:1782-1792` 按 tag 分发,`0x01` 经 `TxOpusDecoder`(`opus_rx.py:135-168`,48kHz 单声道)解码后与 PCM 一样进 `audio.feed_tx_audio()`。**启用 TX Opus 不需要改服务端。**

### 1.4 码率约定已分叉 ⚠️

| 端 | TX Opus 码率 | 证据 |
|----|-------------|------|
| web 端 | 64kbps CBR | `static/modules/opus_codec.js:52-53`(`OPUS_SET_BITRATE 64000`);`tx_opus_worker.js:88` 注释 "~160-byte Opus packet (64kbps CBR)" |
| iOS(死代码) | 28kbps CBR | `OpusBridge.c:26` `OPUS_SET_BITRATE(28000)` |

若直接置 `useOpus = true`,iOS 将以 28kbps 发送,与 web 端 64kbps 的既有约定分叉——服务端解码不在乎码率(Opus 自描述),但音质预期与既有调参(服务端按 64kbps 假设)不一致,启用前必须对齐(见 §3.2)。

### 1.5 埋雷:`OpusBridge.h` 错误码与真实 libopus 不符 ⚠️

`OpusBridge.h:12-15` 自定义错误码:

```c
#define OPUS_OK 0
#define OPUS_INVALID_STATE (-1)   // 真实 libopus: OPUS_INVALID_STATE = -6
#define OPUS_ALLOC_FAIL    (-2)   // 真实: OPUS_ALLOC_FAIL = -7;-2 是 OPUS_BUFFER_TOO_SMALL
#define OPUS_BAD_ARG       (-3)   // 真实: OPUS_BAD_ARG = -1;-3 是 OPUS_INTERNAL_ERROR
```

当前 Swift 侧只判断返回值 `> 0`(`OpusDecoder.swift:34`、`OpusEncoder.swift:35`),桥内部 `my_opus_encode/decode` 错误时返回的 `OPUS_INVALID_STATE`(-1)恰好与真实 `OPUS_BAD_ARG`(-1)同值,所以**现在无功能影响**;但任何人将来按这个头文件解释 `opus_encode/decode` 的负数返回值都会误判错误原因。修复方式:删除自定义定义,改为 `#include <opus/opus_defines.h>` 或按官方值更正。

---

## 2. 帧格式约定(与服务端 `opus_rx.py` 对齐)

TX(`/WSaudioTX` 上行)与 RX(`/WSaudioRX` 下行)同构:

| 项 | 约定 | 证据 |
|----|------|------|
| 帧前缀 | 1 字节 codec tag:`0x00` = Int16 PCM 裸流,`0x01` = Opus 包 | `opus_rx.py:38-39`;iOS 发送 `AudioCaptureManager.swift:126`/`:133`,接收 `AudioPlaybackManager.swift:112-113`;服务端分发 `server.py:1779-1792` |
| 采样率 | 48kHz 单声道(有线格式;服务端内部再 48↔44.1k 重采样给 FT-710 USB 声卡) | `opus_rx.py:50-57`;`audio_handler.py:533`(`feed_tx_audio` 无条件 `resample_48_to_441`) |
| 帧长 | 20ms = 960 样本/帧 | `opus_rx.py:54-55`;iOS `AudioCaptureManager.swift:12-13`(累加器按 960 切帧,`:118-120`);web `tx_opus_worker.js:83`(`OpusEncoder(48000, 1, 2048, 20)`) |
| PCM 格式 | Little-endian Int16 | iOS `floatToInt16` + 逐样本 append(`AudioCaptureManager.swift:145-154`);服务端 ctypes `c_int16` |
| RX Opus 码率 | 默认 64kbps。**`setOpusBitrate` 仅存在于文档与注释，服务端与客户端均未实现**（`opus_rx.py` 内部 `set_bitrate()` 只在构造时生效，没有运行时命令入口）；码率档位待后续阶段 | `opus_rx.py:60-69`、`:197-205` |
| TX Opus 应用类型 | VOIP(2048)——web 与 iOS 桥一致;服务端 RX 编码器用 AUDIO(2049) | `tx_opus_worker.js:83`;`OpusBridge.c:19`;`opus_rx.py:183` |

服务端对 TX 帧长容错:`TxOpusDecoder` 缓冲按 120ms(5760 样本)上限分配(`opus_rx.py:56`),单包超限才出错;但客户端应严格按 960 样本/20ms 发。

---

## 3. 启用 TX Opus 需要改什么

目标:把 `AudioCaptureManager` 的 Opus 分支从死代码变成可用路径。**前置条件:必须先修采样率守卫,否则启用即引入新的变调场景。**

### 3.1 必改①:采样率守卫(前置,阻塞项)

- 现状: `AudioCaptureManager.swift:157` `resample()` 的 `guard inRate > outRate else { return input }` 拒绝上采样——输入原生率 <48kHz 时原样返回,帧仍按 48kHz/960 组帧。
- 对 PCM 这只是变调(分析报告 §4.1);对 Opus 会更糟:喂给编码器的样本数/实际时长不匹配,蓝牙 HFP(8/16kHz,且 session 显式 `.allowBluetooth`,`AudioSessionManager.swift:17-18`)路由下输出完全不可辨。
- 改法: 把 `resample()` 改为双向(相位累加器或线性插值上采样,对照 web `tx_capture_worklet.js:82-97`);同时在 `AudioSessionManager.configureForTransceiver()`(`AudioSessionManager.swift:16-23`)加 `setPreferredSampleRate(48000)` 缩小触发面。

### 3.2 必改②:码率对齐

- `OpusBridge.c:26` `OPUS_SET_BITRATE(28000)` → 与 web 的 64kbps(`opus_codec.js:52`)对齐;或经评估后明确选择 28kbps 并同步更新 web/文档约定。二选一,不允许不声不响地分叉。
- 顺手修正: `my_create_encoder` 的 `frameSize` 参数(`OpusBridge.c:15`)从未使用,删除或实际使用;`OPUS_SET_PACKET_LOSS_PERC(0)`(`:28`)在开启 Opus 后值得重新评估(web 端同样 FEC=OFF,`opus_codec.js:71`)。

### 3.3 必改③:开关接线

- `AudioCaptureManager.swift:20` `useOpus` 改为可配置(设置项或常量),默认 true;保留 PCM 回退用于排障对照。
- 注意 `OpusEncoder.swift:32-33` 与 `OpusDecoder.swift:31-32` 的指针逃逸 `withUnsafeBytes` 闭包写法是 UB,启用前一并修(改为闭包内直接调 C 函数)。

### 3.4 验证清单(必须真机)

- **真机要求**: `libopus.a` 仅 arm64 真机 slice(见 §4),模拟器无法链接,所有验证必须在真机上做。
- 音质/延迟对照: 同一网络下 PCM vs Opus 的 TX 音质、端到端延迟;与服务端日志(`server.py:1782` 分支命中)交叉确认 tag `0x01` 到达。
- 路由切换: 内置麦 → 蓝牙 HFP → 有线,验证 3.1 的重采样在所有路由下不变调(服务端无条件按 48→44.1k 重采样,`audio_handler.py:533`,任何非 48k 输入都会变调)。
- 丢包场景: 弱网下听感(无 PLC,`OpusDecoder.swift:32` `decodeFEC=0`,丢包即爆音,TX 方向同理——这是已知限制,不是本次启用范围)。
- 多客户端: 服务端 TX 单 owner 制(`server.py:1758-1760`),浏览器先连 `/WSaudioTX` 时 iOS 按 PTT 无声且无提示——测试时确认 iOS 是 owner。

---

## 4. 构建影响:libopus.a 仅 arm64 真机 slice

- `lipo -info Sources/Audio/opus-ios/lib/libopus.a` → `Non-fat file … architecture: arm64`(真机 slice,无模拟器 slice)。
- **后果**: 模拟器构建必然链接失败(`Undefined symbols`,arm64-simulator/x86_64 无实现);真机(iphoneos arm64)构建运行正常。
- 与工程现状一致: `project.yml:36` `SUPPORTED_PLATFORMS: iphoneos`、`TARGETED_DEVICE_FAMILY: "1"` 本来就只声明真机;但任何"在模拟器里跑一下"的尝试都会撞上链接错误,需提前知情。
- 若要模拟器支持: 重编 libopus 为 xcframework(arm64-iphoneos + arm64-iphonesimulator 两个 slice),或模拟器条件编译排除 Opus 链路(TX/RX 都退 PCM)。当前无此需求,保持真机-only。
- 另注意(分析报告 §7): checked-in 的 pbxproj 把 `libopus.a` 放进了 Resources phase(白占 bundle 体积,下次重生成工程时应留意);`Info.plist:45` `UIRequiredDeviceCapabilities = armv7` 与 arm64-only 现实矛盾。

---

## 5. 速查:相关文件

| 侧 | 文件 | 角色 |
|----|------|------|
| iOS | `FT710Mobile/Sources/Audio/OpusBridge.c` / `.h` | libopus C 桥(28kbps CBR 编码器;错误码埋雷) |
| iOS | `Sources/Audio/OpusDecoder.swift` / `OpusEncoder.swift` | Swift 封装(`@_silgen_name` 绑定) |
| iOS | `Sources/Audio/AudioPlaybackManager.swift:110-128` | RX tag 分发,Opus 解码在用 |
| iOS | `Sources/Audio/AudioCaptureManager.swift:20,122-139` | TX:`useOpus=false`,Opus 分支死代码 |
| iOS | `FT710Mobile/project.yml:41-43` | libopus 链接配置 |
| 服务端 | `opus_rx.py` | `RxOpusEncoder`(64kbps 默认)/ `TxOpusDecoder`,tag 常量 |
| 服务端 | `server.py:1711-1734` / `:1739-1829` | `/WSaudioRX` 广播 / `/WSaudioTX` 单 owner 接收分发 |
| 服务端 | `audio_handler.py:283-296` / `:525-545` | RX 编码加 tag / TX 48→44.1k 重采样入队 |
| web 对照 | `static/tx_opus_worker.js`、`static/modules/opus_codec.js` | TX 64kbps CBR 约定的来源 |
