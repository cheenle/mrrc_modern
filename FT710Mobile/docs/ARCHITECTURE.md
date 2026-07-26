# FT710Mobile — iOS App 架构文档

**适用项目**: Yaesu FT-710 远程控制(Python FastAPI 服务端 + SwiftUI iOS 客户端)
**版本**: 2026-07-20(推倒重写;此前版本描述的是另一个项目 SunsdrMobile,全部作废)
**目标读者**: 需要修改本 App 的高级开发 / 架构师
**权威依据**: 本文所有结论经 2026-07-20 全量代码审计核对,详见 `../docs/IOS_APP_ANALYSIS.md`(下文简称"分析报告",以 §x.y 引用其章节)。文中所有 `file:line` 锚点均以当前代码为准。

> **文档可信度警告**: `FT710Mobile/CLAUDE.md`、`FT710Mobile/README.md` 同样是 SunsdrMobile 时代的腐化产物(/WSCTRX 端点、cmd:val 文本协议、16kHz TX、512-bin 频谱),不可信。`docs/diagrams/` 下的 6 个 SVG 也是旧文档遗留,保留未动,其内容不代表当前架构。一切以本文 + 分析报告为准。

---

## 1. 总览

### 1.1 系统定位

iOS App 是 FT-710 远程控制系统的**第二客户端**(第一客户端是 `static/` 下的浏览器 SPA)。它不直接与电台通信,而是连接仓库根的 Python FastAPI 服务端(`server.py`),由服务端通过串口 CAT(`cat_controller.py`)和 USB 音频(`audio_handler.py`)控制电台。因此 App 的行为必须始终与服务端协议、web 端语义对照理解。

### 1.2 总览图

```
┌─────────────────────────────────────────────────────────────────┐
│ FT710MobileApp (@main, Sources/App/FT710MobileApp.swift)        │
│   LoginView → Keychain 密码 → 创建 RadioViewModel                │
└──────────────────────────┬──────────────────────────────────────┘
                           │ @EnvironmentObject
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│ RadioViewModel  @MainActor  (ViewModel/RadioViewModel.swift)    │
│ 中枢协调器:认证、bindSockets() 回调接线、sendSet() 命令出口        │
└───┬──────────┬──────────┬──────────┬──────────┬────────────────┘
    │          │          │          │          │
    ▼          ▼          ▼          ▼          ▼
┌────────┐ ┌────────┐ ┌─────────┐ ┌────────┐ ┌─────────────────┐
│Radio-  │ │Connec- │ │Audio-   │ │Audio-  │ │Spectrum-        │
│State   │ │tion-   │ │Playback │ │Capture │ │Processor        │
│(@Main- │ │Manager │ │Manager  │ │Manager │ │(串行队列)        │
│Actor,  │ │(@Main- │ │(RX 链路)│ │(TX 链路│ │                 │
│状态镜像)│ │Actor)  │ │         │ │ +Opus  │ │                 │
└────────┘ └───┬────┘ └────┬────┘ └───┬────┘ └───────┬─────────┘
               │           │          │              │
        ┌──────┴──────┐    │          │              │
        ▼      ▼      ▼    ▼          ▼              ▼
     ┌──────────────────────────────────────────────────────┐
     │ 4× WebSocketConnection (URLSessionWebSocketTask, wss) │
     │  /WSradio(控制) /WSaudioRX /WSaudioTX /WSspectrum    │
     └──────────────────────┬───────────────────────────────┘
                            │ wss://<host>?token=...
                            ▼
     ┌──────────────────────────────────────────────────────┐
     │ Python FastAPI 服务端 (server.py)                     │
     │  cat_controller.py ──► 串口 CAT ──► Yaesu FT-710      │
     │  audio_handler.py  ──► USB 音频 ◄──► (44.1kHz 原生)   │
     │  scope_handler.py  ──► FT4222 SPI 频谱               │
     └──────────────────────────────────────────────────────┘

另有:MemoryChannelsManager(本地频道槽,Model/MemoryChannelsManager.swift)
     OpusDecoder/OpusEncoder + OpusBridge.c(C 桥 → 静态 libopus.a,Audio/)
```

### 1.3 关键事实速览

| 维度 | 现状 |
|---|---|
| 部署目标 | iOS 17,Swift 5.9,纯 Apple 框架 + 静态 `libopus.a`(仅 arm64 真机 slice,模拟器链接失败) |
| 中枢 | `RadioViewModel`(@MainActor),所有子管理器由它持有并接线 |
| 下行状态 | 服务端 `fullState` / `stateUpdate` JSON → `RadioState.applyFullState` 手工键映射 |
| 上行命令 | `{"type":"set","field":...,"value":...}` 文本 JSON,fire-and-forget |
| 音频 | 48kHz/20ms(960 样本)帧,1 字节 codec tag;RX 支持 Opus/PCM,TX 实际只发 PCM |
| 频谱 | 1701 字节二进制帧 @ 5fps → 850×100 瀑布 UIImage + 850 点 FFT 线 |
| 工程健康 | UI 层 25 个文件中 14 个是死代码;测试未接入工程,有效覆盖率 0%(分析报告 §7、§8) |

---

## 2. 分层职责

### 2.1 App 层 — `Sources/App/`

- **`FT710MobileApp.swift`**(@main,66 行):scene 生命周期仅有登录态切换,**无 `scenePhase` 监听**(PTT 后台保护缺失,见 §5)。
  - 登录入口:`LoginView` 回调无条件 `isLoggedIn = true`(`FT710MobileApp.swift:20-25`)——密码错误也会进入主界面并被 Keychain 记住,App 内无改密码入口(分析报告 §2.4)。
  - Keychain:`kSecClassGenericPassword`,`kSecAttrServer = host`,account 固定 `"ft710_mobile"`(`:39-65`);下次启动 `onAppear` 自动登录(`:27-32`)。
  - 主机地址存 `@AppStorage("serverHost")`,默认 `radio.vlsc.net:8888`(`:7`)。
  - 主界面期间禁用息屏(`:17-18`)。
- **`Colors.swift`**:暗色琥珀主题色板(`radioBg`/`radioAccent`/`radioRed` 等),被全部活 UI 引用。

### 2.2 ViewModel 层 — `Sources/ViewModel/RadioViewModel.swift`(471 行)

唯一的协调者,@MainActor。职责:

1. **持有子对象**(`:8-13`):`RadioState`、`ConnectionManager`、`AudioPlaybackManager`、`AudioCaptureManager`、`SpectrumProcessor`、`MemoryChannelsManager`。
2. **认证**:`powerOnAsync()`(`:113-170`)与 `reconnect()`(`:47-110`)实现 §3.1 的认证序列。
3. **回调接线**:`bindSockets()`(`:384-470`)——把 4 路 socket 回调接到 state/音频/频谱;`updateCredentials` 会重建 socket 对象,**必须重新调用 `bindSockets()`**(`:158` 注释)。
4. **命令出口**:全部控制方法收束到 `sendSet(_:_:)`(`:215-221`)。
5. **心跳**:2 秒一次 `{"type":"ping"}`(`:33,:369-377`),不校验 pong(web 端是 15s)。
6. **状态中继**:`state.objectWillChange` / `memChannels.objectWillChange` 转发到自身(`:388-394`)——这使整个 ContentView 视图树随任何字段变化重算 body,是 30Hz 渲染风暴的来源(分析报告 §6)。

死代码:`powerOn()`(`:36-42`)、`setupErrorObservers()`(`:182-192`)无调用方。

### 2.3 Model 层 — `Sources/Model/`

- **`RadioState.swift`**(292 行):@MainActor 的 `ObservableObject`,约 50 个 `@Published`。**它是服务端 `radio_state.py` 的手工镜像**——`applyFullState`(`:206-244`)逐键映射 `to_dict()`(`radio_state.py:245-315`)的非派生字段;`applyStateUpdate`(`:247-249`)直接复用同一映射(增量与全量同构)。
  - 静态常量表(mode/band/filter/scopeSpan/S 表,`:114-201`)是 `config.py` 的第二份拷贝,已出现 3 处漂移(见 §6)。
  - 派生属性(`:84-110`):`activeFreq`、`modeName`、`bandName`、`powerWatts` 等;仪表换算见 §6 的标定差异。
- **`MemoryChannelsManager.swift`**(40 行):本地 10 槽数组(`:7`),`loadFromServer`(`:21-33`)在 fullState/广播时整体覆盖。与服务端协议多处脱节(6 槽、`label` 键、null 空槽、`memSave`/`memRecall` 未使用),详见分析报告 §3.1。

### 2.4 Networking 层 — `Sources/Networking/`

- **`ConnectionManager.swift`**(93 行):@MainActor。持有 4 个 `WebSocketConnection`(`/WSradio`、`/WSaudioRX`、`/WSaudioTX`、`/WSspectrum`,`createSockets()` `:57-67`);`updateCredentials`(`:27-31`)销毁旧 socket 并按新 token 重建;`sendControl`(`:51-53`)只走 ctrl 通道。
- **`WebSocketConnection.swift`**(179 行):通用 `URLSessionWebSocketTask` 封装,`@unchecked Sendable`(`:7`)。
  - URL:`wss://<host><endpoint>?token=<urlencoded>`(`:83-89`),并带 `Origin`/`User-Agent` 头(`:72-75`)。`wss`/`https` 硬编码,无法连 `--no-ssl` 服务端。
  - 收包:链式 `task.receive`(`:101-121`),用 `session === expectSession` 过滤失效 session 的迟到回调(`:104`,`:138`,`:147`,`:167`)。
  - 重连:固定 3 秒(`:10`,`:123-129`),无退避;`didCloseWith` 4001 停止重连(`:152-155`)——**该分支是死代码**,服务端在 `accept()` 前 close(4001)实际表现为 HTTP 403 握手拒绝,走 `didCompleteWithError`(分析报告 §2.3)。
  - `isConnected`(`:24-26`)、`updatePassword`(`:35-37`)无调用方。

### 2.5 Audio 层 — `Sources/Audio/`

| 文件 | 职责 |
|---|---|
| `AudioPlaybackManager.swift` | RX 链路:tag 分发 → Opus 解码 → vDSP Int16→Float32 → `AVAudioPlayerNode` @48kHz;10× 增益补偿;RMS 电平;录音(无调用方且 WAV 头错位,分析报告 §4.3) |
| `AudioCaptureManager.swift` | TX 链路:常驻引擎 + inputNode tap → 20ms/960 样本组帧 → PCM tag 0x00;`prepare()` 预热使首次 PTT 零延迟(`:36-41`,`:69-92`) |
| `AudioSessionManager.swift` | 单例。`.playAndRecord`/`.voiceChat`,`defaultToSpeaker + allowBluetooth + allowBluetoothA2DP`(`:17-18`),5ms IO buffer(`:19`);**未设 `preferredSampleRate(48000)`**,未申请麦克风权限 |
| `OpusDecoder.swift` / `OpusEncoder.swift` | `@_silgen_name` 绑定 C 桥;48kHz 单声道;解码 `decodeFEC=0` 且无 PLC |
| `OpusBridge.c/h` | libopus 薄封装(`my_*` 句柄式 API);编码器 28kbps CBR(`OpusBridge.c:26-28`)。注意头文件错误码与真实 libopus 不符(`OpusBridge.h:13-15`,埋雷) |

### 2.6 Spectrum 层 — `Sources/Spectrum/SpectrumProcessor.swift`(155 行)

`@unchecked Sendable`,全部 CPU 工作在自己的串行队列(`spectrum.processor`,`.userInteractive`,`:41`)上,只有最终 `UIImage` / `[Float]` 跨回主线程。帧解析、瀑布渲染细节见 §3.5。

### 2.7 UI 层 — `Sources/UI/`(25 个文件)

**`ContentView.swift` 是唯一活着的容器**:登录后由 `FT710MobileApp.swift:14` 加载,单屏紧凑布局直接内嵌 HeaderView、FFTLineView、WaterfallView、SMeterView、MeterBarView、QuickControlsRow、DSP 行、音量条、调谐行、VFO 行、频道网格(6 格,`:196-208`)、PTT 底栏(`:214-246`),以及 `SettingsView` sheet(`:249-254`)。

**活的 11 个文件**:ContentView、LoginView、HeaderView、FrequencyDisplay、FFTLineView、WaterfallView、SMeterView、MeterBarView、QuickControlsRow、SettingsView、ErrorAlertView。

**死的 14 个文件**(分析报告 §8,grep 全库无引用):MainRXView(连带内部 GainSlider/AudioLevelBar)、DSPPanelView、PTTFooter、PTTButtonView、TunerView、PerformanceMonitorView、DSPQuickButtons、MemoryChannelsGrid、MemoryChannelsView、ModeSelectorView、BandSelectorView、FilterSelectorView、VFOButtons、TuningControls;另有 `ContentView.swift:269-307` 的 `PTTBar`/`PTTPressStyle`。危害:死文件里有一套语义已漂移的平行实现(不同步进表、AGC 标签、PTT 逻辑),极易误导维护者。**改 UI 前先确认目标文件在活文件清单内。**

---

## 3. 关键数据流

### 3.1 认证序列

```
LoginView(host, pass)
  │  FT710MobileApp.swift:20-25  存 Keychain + 创建 RadioViewModel
  ▼
RadioViewModel.powerOnAsync()                RadioViewModel.swift:113
  │  POST https://<host>/api/auth/login {"password": pass}   :118-128
  ▼
200 OK → 从 Set-Cookie 取 ft710_auth        :143-148
         (兜底:JSON body 的 "token" 字段)    :150-153
  ▼
ConnectionManager.updateCredentials(password: token)
         ConnectionManager.swift:27-31  —— 销毁并重建 4 条 WebSocketConnection
  ▼
bindSockets() 重新接线  RadioViewModel.swift:158
connectAll()            ConnectionManager.swift:33-38
  ▼
每条 socket: wss://<host>/<endpoint>?token=<token>   WebSocketConnection.swift:83-89
  ▼
服务端校验 token ∈ _auth_tokens(server.py:1625-1628 等),/WSradio accept 后
立即推送 fullState(server.py:1636-1645)
```

要点与陷阱:

- **登录后 `connection.password` 被 token 覆盖**(`RadioViewModel.swift:157`),`reconnect()`(`:47-110`)拿该字段当密码重新登录必然 401 → 服务端重启后 App 永远无法恢复,只能杀进程(分析报告 §2.3)。
- 认证失败时服务端在 `accept()` **之前** `close(code=4001)`(`server.py:1626-1628`),ASGI 语义下变成 HTTP 403 握手拒绝 → iOS 走 `didCompleteWithError`(`WebSocketConnection.swift:165-178`)无限 3s 重连,无"认证过期"提示;`didCloseWith` 里的 4001 分支(`:152-155`)永远不会触发。
- 认证失败时 `state.powerOn` 复位(`:163`),但 `Task.detached { audioPlayback.start() }`(`:166-168`)**无条件执行**——音频引擎在登录失败后照样启动。
- 密码错误:`FT710MobileApp.swift:24` 无条件置登录态,错误密码进 Keychain,下次启动照旧失败,App 内无修改入口(分析报告 §2.4)。

### 3.2 控制面(JSON 文本协议)

**上行(App → 服务端)**——所有命令收束于一处:

```swift
// RadioViewModel.swift:215-221
{"type": "set", "field": "<field>", "value": <value>}
```

fire-and-forget:发送后**不做本地乐观更新**,UI 完全等服务端回显。字段全集见 `RadioViewModel.swift:226-365`(`freq`/`vfo_b_freq`/`mode`/`ptt`/`tune`/`tuner`/`af_gain`/`rf_gain`/`rf_power`/`squelch`/`mic_gain`/`filter`/`preamp`/`att`/`ipo`/`nb`/`nr`/`an`/`comp`/`agc`/`vfo`/`split`/`scope_span`)。服务端分发:`server.py:792-793` → `_execute_set_command`。另有 2s 心跳 `{"type":"ping"}`(`RadioViewModel.swift:369-377`)。

**下行(服务端 → App)**——`/WSradio` 文本帧,分发在 `bindSockets()` 的 `ctrl.onText`(`RadioViewModel.swift:419-450`):

| `type` | 载荷 | 处理 |
|---|---|---|
| `fullState` | `data`(全量)+ `bands` + `modes` + `memChannels` | `state.applyFullState(data)`(`:427-430`);`memChannels` → 频道管理器(`:431-433`);**`bands`/`modes` 被丢弃**(服务端其实已下发,`server.py:1642-1643`,iOS 仍用本地硬编码表) |
| `stateUpdate` | `fields`(脏字段增量) | `state.applyStateUpdate(fields)`(`:434-437`)——复用全量映射 |
| `memChannels` | `channels` | 全量覆盖本地(`:442-445`) |
| `pong` | — | 忽略(`:438-439`) |
| `error` | `message` | 只写 `connectionError`(`:440-441`),主界面不可见(仅 offState 展示) |

**键映射是手工镜像**:`RadioState.applyFullState`(`RadioState.swift:206-244`)逐键对应 `radio_state.py:252-315` 的 `to_dict()`。已确认缺口:服务端 18 个字段(`nr_level`、`nb_level`、`vox`、`break_in`、`scope_on`、`scope_speed`、`dnr_level`、`contour_level`、`hi_swr`、`recording_status`、`rx_tx_status`、`tuner_tuning`、`scan_status`、`squelch_open`、`meter_display`、`amc_level`、`rx_audio_silent` 等)未映射——Swift 侧虽已声明对应 `@Published`,但永远是假默认值(分析报告 §5)。`applyFullState` 末尾 `lastUpdate = Date()…`(`RadioState.swift:242-243`)是无条件覆盖的死赋值。

### 3.3 音频 RX(服务端 → App)

```
服务端:audio_handler.py 采集 FT-710 USB 音频 44.1kHz
  → resample_441_to_48(audio_handler.py:246)→ 48kHz/20ms
  → Opus 编码 → 帧 = [0x01] + opus 包(opus_rx.py:38-39 定义 tag)
  → /WSaudioRX 广播
        │
        ▼
WebSocketConnection.onBinary → RadioViewModel.swift:453-455
  → AudioPlaybackManager.enqueue(int16Data:)   AudioPlaybackManager.swift:110
        │ 读首字节 tag:
        ├─ 0x01 → OpusDecoder.decode(:114;48kHz 单声道,decodeFEC=0,无 PLC)
        └─ 其他 → 按 Int16 LE PCM 直送(:126-127)
        ▼
processPCM(:131-181):
  vDSP_vflt16 + vDSP_vsmul(×1/32768)→ [Float]   :137-142
  vDSP RMS → 主线程 rmsLevel                     :151-154
  ioQueue 上逐帧 AVAudioPCMBuffer → playerNode.scheduleBuffer
  ※ 缓冲按 playerNode.outputFormat(forBus: 0) 的实时格式构造:
    单声道源按需上混到总线声道数、总线采样率 ≠48kHz 时线性重采样。
    iOS 可能把 player→mixer 总线协商成硬件格式(如 .voiceChat 下立体声),
    按固定单声道格式构造缓冲会触发 ScheduleBuffer channelCount 断言崩溃
    (2026-07-21 真机:登录后首帧即崩,已修复)。
```

**与 web 端的关键差异:iOS 无 jitter buffer。** 每帧到达即 `scheduleBuffer`,无预缓冲、无重排、无丢弃——网络抖动后积压音频永久滞后、越积越多(音画/电平不同步),丢包即爆音。web 端 `static/rx_worklet_processor.js:33-35` 是 time-based 水位线(prebuffer 220ms、underrun 恢复 90ms、上限 800ms 丢最旧)。分析报告 §4.2。

增益:本地 `appVolume`(0–1)× `audioGainBoost`(10×)封顶 10(`AudioPlaybackManager.swift:37-46`),对齐 web 端 `AUDIO_GAIN_BOOST` 补偿 FT-710 USB 音频偏小;与电台侧 `af_gain` 独立。

~~已知崩溃隐患:`stop()` 不 detach `playerNode`,`start()` 每次 `engine.attach` → 重复 attach 抛 NSException~~ **已修复(2026-07-21)**:`start/stop` 序列化到 `ioQueue`(消除 powerOn 与登录回调 `Task.detached` 的双 start 竞态),`isAttached` 标志保证 attach 仅一次。

### 3.4 音频 TX(App → 服务端)

```
setPTT(true)  RadioViewModel.swift:269-278
  → audioPlayback.isMuted = true + audioCapture.start()(仅翻转 isCapturing)
        │
常驻引擎(prepare() 预热,AudioCaptureManager.swift:36-41,:69-92)
inputNode.installTap(原生采样率,bufferSize 1024):77
        ▼ tap 回调(实时线程!)
processBuffer(:94-141):
  原生率 ≠ 48kHz → resample()(:100-106)  ⚠ 见下方缺陷
  × micGain → vDSP RMS → 主线程 txLevel
  accumulator 攒够 960 样本(20ms @48k):118-120
        ├─ useOpus==true → OpusEncoder → [0x01]+opus   :122-129(死路径)
        └─ useOpus==false → Int16 LE → [0x00]+1920B PCM :130-139(实际唯一路径)
        ▼
onFrame → connection.audioTX.send(binary:)   RadioViewModel.swift:458-460
        ▼
服务端 server.py:1770-1792:仅 TX-owner 的帧被受理(单 owner 制,server.py:1745-1747,:1758-1760),
tag 0x00 → audio.feed_tx_audio → resample_48_to_441(audio_handler.py:533)→ FT-710 USB
```

要点与缺陷:

- **整条 Opus TX 是死代码**:`useOpus: Bool = false`(`AudioCaptureManager.swift:20`)全项目无置 true 路径,TX 永远 PCM ≈768kbps(web 端是 64kbps Opus)。`OpusEncoder.swift` 全链路(28kbps CBR,`OpusBridge.c:26-28`)仅被死路径引用,且码率约定已与 web 分叉(分析报告 §4.4)。
- **重采样只在降采样生效**:`resample()` 开头 `guard inRate > outRate else { return input }`(`AudioCaptureManager.swift:157`)——原生输入率 <48kHz(蓝牙 HFP 8/16kHz、部分 44.1k 路由;session 显式 `.allowBluetooth` 且未设 `preferredSampleRate`)时原样返回,帧仍按 48kHz/960 组帧;服务端无条件 48→44.1k 重采样 → 音高上移变调甚至不可辨(分析报告 §4.1)。web 端 `static/tx_capture_worklet.js` 是相位累加器双向重采样。
- tap 回调在**实时线程**上做数组拷贝、重采样、960 次逐样本 `Data.append`(`:134-137`)与 O(n) `removeFirst`(`:120`)→ glitch 风险(分析报告 §4.5)。
- `engine.start()` 失败后 `enginePrimed` 保持 false,下次 `prepare()` 对同一 inputNode 重复 `installTap` → NSException(`:77-91`);全 App 无 `requestRecordPermission`。
- 服务端 TX 单 owner:若浏览器先连 `/WSaudioTX`,iOS 按 PTT 无声且无任何提示。
- **iOS 从不发送 `'s:'` 停止帧**(服务端 `server.py:1796-1801` 已有处理;web 端 PTT 释放时会发)——已批准的 PTT 设计将补上(§5)。

### 3.5 频谱(服务端 → App)

```
服务端 _spectrum_broadcast_loop:5fps(server.py:285;docstring 写 ~30fps 已过时)
FT4222 真实 FFT 或 S-meter 高斯回退(scope_handler.py)
帧 = 1B version(0x01)+ 850B wf1 + 850B wf2 = 1701B → /WSspectrum
        │
        ▼
RadioViewModel.swift:463-469 → SpectrumProcessor.feed(data:onImage:onFFT:)
  feed(:50-99):校验 version==0x01;取 wf1(bytes 1...850;wf2 丢弃):52-61
  串行队列(spectrum.processor,.userInteractive):
    累加 → 30fps 节流(:83;wfDecimate=1 即每帧处理,:8)
    自适应噪底:35 百分位 + 2(:108-110)
    对比度:v = bias(40) + (avg-floor) × gain(12),clamp 0-255 → 256 色 LUT
           (深蓝→青→黄→红,与 web 一致,:14-35)
    滚动:850×100×4B 像素缓冲 memmove 下移一行 + memcpy 新行(:125-133)
    CGDataProvider → CGImage → UIImage(:136-148)
  每 2 帧回调一次 FFT 线 [Float](:92-95)
  主线程回调 → state.waterfallImage / state.fftData(RadioViewModel.swift:464-468)
        ▼
WaterfallView(纯展示 UIImage)+ FFTLineView(Canvas 画线)
```

性能注意点:

- 服务端实际 5fps,而 `SpectrumProcessor.swift:73-76` 的 "Low FPS" 告警阈值是 14fps → **永久误报刷屏**;FFT 线每 2 帧发一次 → 实际 ~2.5fps,panadapter 迟钝(分析报告 §6)。
- 瀑布状态 + FFT + 音频 RMS 全部经 `@Published` 中继进 SwiftUI,整个 ContentView 以高频率重算 body;`SpectrumProcessor` 每帧还有 340KB 像素缓冲的 COW 拷贝(`:125`)(分析报告 §6)。
- 瀑布误触即 QSY:`WaterfallView.swift:70-74` 用 `DragGesture(minimumDistance: 0)` 直接 `setFrequency`,任何滑动都改频率(web 端瀑布无点击调谐)——已批准改为 `SpatialTapGesture`(§5)。
- iOS 完全忽略服务端下发的 `scope_start_freq`,频率标尺自行推算。

---

## 4. 线程 / 并发模型

| 组件 | 隔离域 | 说明 |
|---|---|---|
| `RadioViewModel`、`RadioState`、`ConnectionManager`、`MemoryChannelsManager` | `@MainActor` | UI 状态单一事实来源;所有 `state.*` 读写必须在主线程 |
| `WebSocketConnection` | `@unchecked Sendable`(`WebSocketConnection.swift:7`) | 实际并发面:`URLSession(delegateQueue: nil)` 回调落在系统委托队列;类内可变状态(`task`/`session`/`isActive`/`shouldReconnect`)**无任何锁**,依赖"回调串行 + 主线程调用 connect/disconnect"的隐性约定。回调里用 `session === self.session` 识别并丢弃失效 session 的迟到事件(`:104` 等),这是重建 socket 后避免串台的唯一防线 |
| `AudioPlaybackManager` | `@unchecked Sendable`(`AudioPlaybackManager.swift:6`) | `enqueue` 在 WS 回调线程执行(vDSP 转换、RMS),`scheduleBuffer` 切到自建 `ioQueue`(`:9`,`:163-171`);`start/stop` 从主线程/`Task.detached` 调用;`@Published`(rmsLevel 等)切主线程赋值(`:154`)。隐性约定:引擎生命周期方法与数据路径不同步,无锁 |
| `AudioCaptureManager` | `@unchecked Sendable`(`AudioCaptureManager.swift:9`) | tap 回调跑在 **CoreAudio 实时线程**;`isCapturing`/`accumulator` 与主线程的 `start()/stop()` 之间无同步——依赖 Bool 原子性与"最坏丢一帧"的容忍。`onFrame` 从实时线程直出到 `WebSocketConnection.send` |
| `SpectrumProcessor` | `@unchecked Sendable`(`SpectrumProcessor.swift:5`) | 内部状态只许碰自己的串行队列(`:41`);入口 `feed` 可从任意线程调,交付结果经 `DispatchQueue.main.async` |
| `OpusDecoder` / `OpusEncoder` | 无标注 | 非线程安全;实际只分别从 RX 回调线程 / tap 线程单线程访问 |

隐性约定汇总(改动时容易踩):

1. `ConnectionManager.updateCredentials` 重建 socket 后必须 `bindSockets()` 重接回调,否则 4 路数据全部进黑洞(`RadioViewModel.swift:158`)。
2. URLSession 登录回调先到系统队列,代码显式 `DispatchQueue.main.async` 回主线程(`RadioViewModel.swift:131`);`Task.detached` 启音频引擎(`:166-168`)是有意的离主线程操作,但它同时绕过了 @MainActor 对 `audioPlayback` 的访问约定。
3. 频谱/音频/RMS 三路高频回调各自切主线程写 `@Published`,合流成 §3.5 所述的渲染压力;任何"再加一个高频 @Published"的改动都会直接加剧。

---

## 5. PTT 安全:现状与已批准设计(待实施)

### 5.1 现状(存在 P0 安全缺陷)

PTT 手势在 `ContentView.swift:227-235`:

```swift
DragGesture(minimumDistance: 0)
  .onChanged { if viewModel.state.txStatus == 0 { viewModel.setPTT(true)  } }  // :230
  .onEnded   { if viewModel.state.txStatus > 0  { viewModel.setPTT(false) } }  // :233
```

- **释放竞态**:`txStatus` 完全依赖服务端 stateUpdate 回显。WAN 延迟下快速点按,松开瞬间回显未到 → `ptt:false` 被静默丢弃,**电台持续发射**。web 端 `static/ft710_ui.js:889-897` 是无条件发送 + 本地乐观置零,iOS 两者皆无(分析报告 §2.1)。
- **无看门狗、无后台保护**:无 `scenePhase` 监听、无最大 TX 时长;手势被系统手势/来电中断时 `onEnded` 可能不触发;iOS 也不发 `'s:'` TX 音频收尾帧。对照 SDD 第 15 章 7 层模型,iOS 仅有残缺的 Layer 1/2(分析报告 §2.2)。
- 服务端兜底仅在断连时生效:最后一个 ctrl 客户端断开强制 RX(`server.py:1664-1673`)、TX-owner 断开强制 RX(`server.py:1814-1825`);多客户端在线时失效。`config.py:289 PTT_SAFETY_TIMEOUT` 被定义/引用但从未实现。
- 相关项:瀑布误触 QSY(§3.5)、主界面 TUNE 按钮实际发 `"tuner"`(天调开关)而非 `"tune"`(`ContentView.swift:236` → `toggleTuner()`,`RadioViewModel.swift:286-289`)、首屏假开机(`RadioState.swift:40` `powerOn` 默认 true)。

### 5.2 已批准设计 —— **待实施,代码尚不存在**

设计稿:`docs/superpowers/specs/2026-07-20-ios-ptt-safety-design.md`(2026-07-20,用户已批准"方案 A:纯 SwiftUI + PTTManager 状态机";不改服务端、不新增 AD;多客户端仲裁 I6 明确不解决)。实施前**不要**把本节当作现状描述。

- 新建 `Sources/PTT/PTTManager.swift`(@MainActor):状态机 `idle → keyed → releasing → idle`;`press()` 仅 idle 受理且 ctrl 已连接(未连接直接拒绝);`release()` **无条件**发 `ptt:false` + 停采集 + 发 `'s:'` 帧 + 启动看门狗;`forceRelease()` 任意状态幂等(供 scenePhase/错误路径)。
- 看门狗:release 后每 500ms 查服务端回显 `txStatus`,仍 TX 则重发,最多 3 次,耗尽报 `onStuckTX`。
- UI 的 TX 指示改读 `pttManager.phase`,**不再读 `RadioState.txStatus`**(回显延迟正是竞态根源);`txStatus` 只服务看门狗与仪表。
- `FT710MobileApp` 增加 `scenePhase != .active → forceRelease()`;`WaterfallView` 改 `SpatialTapGesture`;`RadioState.powerOn` 默认改 false。

SDD 第 15 章七层对应(设计稿 §6):

| SDD ch15 Layer | iOS 现状 | 设计实施后 |
|---|---|---|
| 1 Touch-and-hold UX | DragGesture,释放依赖回显 | 无条件 release(修复) |
| 2 WS command | `sendSet("ptt", v)` fire-and-forget | 保持 |
| 3 PTT watchdog | 无 | PTTManager 500ms×3(新增) |
| 4 Server dead-man switch | `server.py:1664-1673` / `1814-1825` 已有 | 不动 |
| 5/6 unload/pagehide 等价 | 无 | scenePhase → forceRelease(新增) |
| 7 TX 音频收尾 `'s:'` | 未发 | 停采集 + `'s:'` 文本帧(新增) |

---

## 6. 与服务端的耦合点清单

改动任何一侧时,必须同步检查另一侧。服务端文件均在仓库根。

### 6.1 端点与认证

| 耦合点 | iOS 侧 | 服务端侧 |
|---|---|---|
| `POST /api/auth/login` → `ft710_auth` cookie(兜底 JSON `token`) | `RadioViewModel.swift:118-153` | `server.py:1514` |
| `wss://…?token=` 四端点 `/WSradio` `/WSaudioRX` `/WSaudioTX` `/WSspectrum` | `ConnectionManager.swift:57-67`、`WebSocketConnection.swift:83-89` | `server.py:1621,1678,1711,1740` |
| 4001 认证拒绝(accept 前 close,实为 HTTP 403) | `WebSocketConnection.swift:152-155`(死分支) | `server.py:1626-1628` 等 |
| `Origin` / `User-Agent` 头 | `WebSocketConnection.swift:72-75` | — |

### 6.2 消息 schema(/WSradio 文本 JSON)

| 方向 | 消息 | iOS 侧 | 服务端侧 |
|---|---|---|---|
| 上行 | `{"type":"set","field","value"}` | `RadioViewModel.swift:215-221` | `server.py:792-793` → `_execute_set_command` |
| 上行 | `{"type":"ping"}`(2s) | `RadioViewModel.swift:369-377` | `server.py:718-719`(pong 应答) |
| 下行 | `fullState`(`data`+`bands`+`modes`+`memChannels`) | `RadioViewModel.swift:427-433`(`bands`/`modes` 被丢弃) | `server.py:1636-1645` |
| 下行 | `stateUpdate`(`fields` 增量) | `RadioViewModel.swift:434-437` | `_broadcast_state`,`server.py:187` 起 |
| 下行 | `memChannels` / `error` / `pong` | `RadioViewModel.swift:438-445` | `server.py` |
| 键映射 | `RadioState.applyFullState` 手工镜像 `to_dict()` | `RadioState.swift:206-244` | `radio_state.py:245-315`(18 字段未映射) |
| 未用子协议 | `memRecall`(原子 recall,含频率先行 + SSB ±1400Hz 二次校频 + 2s 轮询跳过窗)、`memSave` | 未使用(iOS 用两条独立 set + 只写本地) | `server.py:734-790`,`:798-801` |

### 6.3 二进制格式

| 通道 | 格式 | iOS 侧 | 服务端侧 |
|---|---|---|---|
| `/WSaudioRX` 下行 | 1B tag(`0x00`=PCM Int16 LE,`0x01`=Opus)+ 48kHz/20ms 帧 | `AudioPlaybackManager.swift:109-128` | `opus_rx.py:38-39`、`audio_handler.py:293-298` |
| `/WSaudioTX` 上行 | 同上 tag;文本 `'s:'` 停采收尾、`'m:'` 设置 | `AudioCaptureManager.swift:122-139`(只发 0x00;**不发 `'s:'`**) | `server.py:1770-1808`(单 owner,`:1745-1747`) |
| TX 采样率约定 | 服务端无条件 48→44.1k 重采样 | `AudioCaptureManager.swift:11,157`(上采样缺失缺陷) | `audio_handler.py:525-533` |
| `/WSspectrum` 下行 | 1701B = 1B version(0x01)+ 850B wf1 + 850B wf2 @5fps | `SpectrumProcessor.swift:50-61`(只用 wf1) | `server.py:275-285`、`scope_handler.py`/`scope_frame.py` |

### 6.4 常量表双份镜像(config.py ↔ RadioState.swift)

以下表在两端各硬编码一份,**已发生 3 处漂移**,改动必须双侧同步(或按 §7 方向改由服务端下发):

| 表 | iOS 侧 | 服务端侧 | 已知漂移 |
|---|---|---|---|
| 模式号→名称 | `RadioState.swift:114-118` | `config.py`(`MODE_NUM_TO_NAME`,含 `0x0F: "DATA-FM-N"`,`config.py:84`) | iOS 缺 `0x0F DATA-FM-N` → 该模式显示成 "USB" |
| 滤波器宽度表(voice/narrow) | `RadioState.swift:156-188` | `config.py` | 须一致,否则带宽显示错 |
| scope spans | `RadioState.swift:130-138` | `config.py` | — |
| bands | `RadioState.swift:140-153` | `config.py` BANDS(fullState 已下发但被丢弃) | — |
| S-meter 标定 | `RadioState.swift:192-201` | `config.py:221-274` | 一致(无问题) |
| 功率/SWR/电压/电流换算 | `RadioState.swift:276-291`(**线性近似**) | `config.py` 非线性标定表(功率上限 110W、SWR 上限 9.9) | 同一时刻 iOS 与 web 显示不同;iOS 功率显示随设定值漂移 |
| 存储频道槽数 | `MemoryChannelsManager.swift:7`(**10**) | `config.py:293` `MEM_CHANNEL_COUNT = 6`,空槽补 `null`(`server.py:166-178`) | 槽位数不一致 + `as? [[String:Any]]` 遇 NSNull 整体失败 → 频道列表恒空(常态) |
| 音频增益补偿 | `AudioPlaybackManager.swift:37-38`(10×) | web `AUDIO_GAIN_BOOST` | 一致 |

### 6.5 服务端 PTT 兜底(客户端设计的前提)

- 最后一个 ctrl 客户端断开且电台在 TX → 强制 RX:`server.py:1664-1673`。
- TX-owner(TX 音频 socket 持有者)断开且电台在 TX → 强制 RX:`server.py:1814-1825`。
- `config.py:289 PTT_SAFETY_TIMEOUT = 2.0`:已定义、被引用,**从未实现**。
- 多客户端仲裁是已知开放问题 I6(last-writer-wins),PTT 设计明确不试图解决。

---

## 7. 已知架构级缺陷与演进方向

完整清单、证据与优先级见 `docs/IOS_APP_ANALYSIS.md`(P0-P2);修复路线见 `docs/IOS_APP_FIX_GUIDE.md`。架构层面必须知道的:

**P0(安全/崩溃/可用性)**

1. PTT 释放竞态 + 无看门狗/后台保护 + 瀑布误触 QSY —— §5,设计已批准待实施。
2. 认证失败路径断裂:4001 死分支、reconnect 拿 token 当密码、密码输错即锁死 —— §3.1。
3. 音频引擎崩溃隐患:playerNode 重复 attach 已修复(`isAttached` + start/stop 序列化到 ioQueue,2026-07-21);installTap 重试(`AudioCaptureManager.swift:77-91`)仍为静态分析结论,建议真机验证。另:登录后 ScheduleBuffer channelCount 断言崩溃已修复(按实时输出格式构造缓冲 + 单声道自动上混)。

**P1(功能实际残废)**

4. mem 频道子协议全线脱节(null 解析 / `label` 键 / 6 槽 / 未用 `memSave`/`memRecall`)—— §6.4、分析报告 §3.1。
5. TX 重采样拒绝上采样(蓝牙/44.1k 变调)、RX 无 jitter buffer/PLC、录音功能整体失效、TX Opus 死链 —— §3.3、§3.4。
6. `setIPO` 发 `"ipo"` 服务端无此分支;主界面 TUNE 发 `"tuner"` 而非 `"tune"`;"A=B" 三端三种行为 —— 分析报告 §3.2。

**P2(工程健康)**

7. 主线程渲染风暴(高频 @Published 合流)+ 频谱 Low FPS 永久误报 —— §3.5。
8. UI 14 个死文件 + 逻辑死代码(§2.7、§2.2)—— 误导维护者,应删除。
9. 测试有效覆盖率 0%:未接入工程(`project.yml` 无 test target),且现有测试写的是另一套不存在的 API,编译即失败 —— 分析报告 §7。
10. 构建配置:`libopus.a` 仅 arm64(模拟器链接失败)、`Info.plist` armv7 声明错误、硬编码 https/wss 与默认主机 —— 分析报告 §7。

**演进方向**(按分析报告 §9 优先级):先落 PTT 安全设计与认证修复(P0)→ 对齐 mem 协议、修 TX 重采样、补 RX jitter buffer、接线错误消息展示(P1)→ 删死代码、重建测试并接入工程、常量表改由服务端 fullState 下发(消除双份镜像)、仪表标定对齐服务端非线性表(P2)。

---

## 8. 构建与依赖

- 零三方依赖管理(CocoaPods/SPM/Carthage 均无);纯 Apple 框架(SwiftUI/Combine/AVFoundation/Accelerate)+ checked-in 静态库 `Sources/Audio/opus-ios/lib/libopus.a`(仅 arm64 真机 slice)。
- `project.yml` 为 XcodeGen 规格,生成 `FT710Mobile.xcodeproj`;部署目标 iOS 17.0,Swift 5.9。
- `SunsdrMobile.xcodeproj/`、`project.pbxproj.bak` 是旧项目陈旧产物,与当前工程无关。
- 真机构建/部署步骤见 `docs/IOS_BUILD_GUIDE.md`(注意其中 `xcodebuild test` 指引因测试未接入工程而必然失败,分析报告 §7)。
