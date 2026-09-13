# CLAUDE.md — FT710Mobile iOS App

> 本文件面向 AI 编码代理。2026-07-20 重写：旧版描述的是另一个项目(SunsdrMobile),已全部作废。
> **路径约定**: 本文所有相对路径均以仓库根 `/Users/cheenle/HAM/mrrc_ft710` 为基准。
> **权威参考**: `docs/IOS_APP_ANALYSIS.md`(2026-07-20 深度审计,附 file:line 证据)。本文与其冲突时,以分析报告和代码为准。

## 项目一句话

FT710Mobile 是 Yaesu FT-710 短波电台的 SwiftUI 遥控客户端(iOS 17,iPhone):通过 4 路 WSS 连到仓库根的 Python FastAPI 服务端(`server.py`),实现频率/模式/DSP 控制、Opus 下行音频、PCM 上行麦克风、频谱瀑布。

## 构建与测试

```bash
# 生成 Xcode 工程(已验证:xcodegen 2.45.4)
cd FT710Mobile && xcodegen generate

# 打开工程(真机构建+运行;模拟器链接失败,见下)
open FT710Mobile.xcodeproj

# 命令行无签名编译检查(不产物入库,无需开发者账号)
cd FT710Mobile && xcodebuild -project FT710Mobile.xcodeproj -scheme FT710Mobile \
  -destination 'generic/platform=iOS' CODE_SIGNING_ALLOWED=NO \
  -derivedDataPath /tmp/ft710-dd build
```

- 部署目标 iOS 17.0,Swift 5.9,bundle id `com.hamradio.ft710mobile`,仅 iPhone(`SUPPORTED_PLATFORMS: iphoneos`)。
- `DEVELOPMENT_TEAM: VQ89MM7935` 硬编码在 `FT710Mobile/project.yml`,换账号改这里。
- **只能真机构建**: `Sources/Audio/opus-ios/lib/libopus.a` 只有 arm64 真机 slice(已用 `lipo -info` 核实),模拟器链接必然失败。
- **测试**: 当前**没有 test target**(`project.yml` 里只有 application target),`xcodebuild test` 不可用。`Tests/FT710MobileTests/` 下两个旧文件(`RadioViewModelTests.swift`、`OpusCodecTests.swift`)写的是已不存在的 API,**不可编译、未接入工程、不要参考**。计划中新增的 `PTTManagerTests` target 属 PTT 安全修复计划 Task 1,**待实施**。

## 架构速览

```
FT710MobileApp (登录页 → Keychain 取密码 → RadioViewModel)
  └─ RadioViewModel  @MainActor 总协调器 (Sources/ViewModel/RadioViewModel.swift)
      ├─ ConnectionManager → 4× WebSocketConnection (URLSessionWebSocketTask, 3s 固定重连)
      │    ctrl:/WSradio  audioRX:/WSaudioRX  audioTX:/WSaudioTX  spectrum:/WSspectrum
      ├─ RadioState        (@Published 镜像服务端状态; applyFullState/applyStateUpdate)
      ├─ AudioPlaybackManager  (RX: tag 分发 → OpusDecoder → AVAudioPlayerNode @48kHz, 10×增益)
      ├─ AudioCaptureManager   (TX: inputNode tap 常驻 → 960样本/20ms 帧 → PCM tag 0x00)
      ├─ SpectrumProcessor     (1701B 帧 → wf1 → 瀑布 UIImage + FFT 线)
      └─ MemoryChannelsManager (本地 10 槽数组)
```

数据流要点:

1. 登录: `POST https://<host>/api/auth/login`(JSON `{"password"}`)→ 取 `ft710_auth` cookie(回落读 JSON body 的 `token`)→ `updateCredentials(password: token)` 重建 4 条 socket → `connectAll()`。见 `RadioViewModel.powerOnAsync()`。
2. 下行控制: `/WSradio` 文本 JSON → `bindSockets()` 里 switch `fullState/stateUpdate/memChannels/error/pong`。
3. 上行控制: 全部走 `sendSet(field, value)` → `{"type":"set","field","value"}`;另有 2 秒 `{"type":"ping"}` 心跳(从不校验 pong)。
4. 下行音频: `/WSaudioRX` 二进制 → `enqueue(int16Data:)` 按首字节 tag 分发,Opus 解码后经 vDSP 转 Float32 即达即播(无 jitter buffer)。
5. 上行音频: `audioCapture.onFrame` → `/WSaudioTX` 二进制,恒 PCM(`useOpus=false` 全项目无置 true 路径)。
6. 频谱: `/WSspectrum` 二进制 → `SpectrumProcessor.feed` → `waterfallImage`/`fftData` 回主线程。

## 协议事实表(两端逐字段核对过)

| 项 | 事实 |
|---|---|
| 认证 | `POST /api/auth/login` → 200 `{"ok":true,"token"}` + `Set-Cookie: ft710_auth`;错误密码 401,限流 429。WS URL 拼 `?token=<token>` |
| WS 端点 | `/WSradio`(控制 JSON 文本)、`/WSaudioRX`(下行音频二进制)、`/WSaudioTX`(上行音频二进制)、`/WSspectrum`(频谱二进制) |
| 控制上行 | `{"type":"set","field":<名>,"value":<值>}`;`{"type":"ping"}` 每 2s |
| 控制下行 | `fullState`(data + bands + modes + memChannels)、`stateUpdate`(fields 增量)、`memChannels`、`error`、`pong` |
| 音频帧 | 1 字节 codec tag + payload:`0x00`=PCM Int16 LE,`0x01`=Opus。48kHz 单声道 20ms 帧(960 样本)。RX 实际走 Opus,TX 恒 PCM(~768kbps) |
| 频谱帧 | 1701 字节 = `0x01` 版本 + 850B wf1 + 850B wf2;iOS 只用 wf1。服务端实际 ~5fps 广播(`server.py:285`;`/WSspectrum` docstring 写的 "~30fps" 已过时) |
| scheme | `https`/`wss` 硬编码 → **连不了 `--no-ssl` 服务端**;默认主机 `radio.vlsc.net:8888`(登录页可改,@AppStorage 持久化) |
| 服务端端口/密码 | `FT710_WEB_PORT`(默认 8888)、`FT710_WEB_PASSWORD`,见 `config.py` |
| 服务端录音(AD-017,v1.15.0 起) | `{"type":"set","field":"recording","value":true/false}` 启停;下行 `recordingState`(recording/freq_hz/started_at/duration/name/bytes/dropped,录制中 1 Hz)+ 随 `fullState.recording` 给快照。**iOS 端仍是客户端本地录音(`AudioCapture.isRecording`),尚未迁移到服务端**;列表/播放/下载/删除在 Web「录音」面板 |
| 机型 key | `MRRC_RADIO_MODEL` 共 10 个:`ft710` `ic7300` `ic7300mk2` `ic705` `ic7610` `ic7760` `ftdx10` `ftdx101d` `ftdx101mp` `ftx1`。后六个(三款 Icom 预览 + 四款 Yaesu)默认拒绝发射,需服务端 `MRRC_ALLOW_UNVERIFIED_TX=1`;机型由服务端 env 决定,客户端不选 |

服务端对照: `server.py:1514`(login)、`:1621`(/WSradio)、`:1678`(/WSspectrum)、`:1711`(/WSaudioRX)、`:1739`(/WSaudioTX)。

## 源码布局

```
FT710Mobile/
├── project.yml                 # XcodeGen 工程规格(改工程配置只动这里)
├── Resources/Info.plist
├── Sources/
│   ├── App/        FT710MobileApp(入口+Keychain)、Colors
│   ├── ViewModel/  RadioViewModel(总协调器)
│   ├── Model/      RadioState、MemoryChannelsManager
│   ├── Networking/ ConnectionManager、WebSocketConnection
│   ├── Audio/      Playback/Capture/Session + OpusBridge(C)+OpusDecoder/Encoder+opus-ios/(libopus)
│   ├── Spectrum/   SpectrumProcessor
│   └── UI/         25 个文件,其中 14 个是死代码(见下)
├── Tests/FT710MobileTests/     # 两个腐烂旧文件,未接入工程,勿参考
└── docs/ARCHITECTURE.md        # 同样腐化(SunsdrMobile),勿信
```

## 改动时的约束与坑

### 死代码警示(改代码前先查清单)

UI 目录 25 个文件里 **14 个无调用点**(分析报告 §8 已 grep 全库确认):MainRXView(连带 GainSlider/AudioLevelBar)、DSPPanelView、PTTFooter、PTTButtonView、TunerView、PerformanceMonitorView、DSPQuickButtons、MemoryChannelsGrid、MemoryChannelsView、ModeSelectorView、BandSelectorView、FilterSelectorView、VFOButtons、TuningControls,以及 ContentView.swift 尾部的 PTTBar/PTTPressStyle。**ContentView 是唯一活容器**。死文件里的步进表、AGC 标签、PTT 逻辑是另一套已漂移的语义,**不要从死文件抄模式**。逻辑层死代码:`powerOn()`、`setupErrorObservers()`、`updatePassword`、`isConnected`、`ConnectionManager.errorMessage`、`isRecording`、`useOpus`、`audioOpusDetected` 通知等。

### 双份常量表

mode/band/filter/scopeSpans/S-meter 表在 `RadioState.swift` 手工镜像 `config.py`,已漂移(如缺 `0x0F DATA-FM-N`)。服务端 fullState 其实下发了 `bands`/`modes` 但 iOS 丢弃未用。改常量必须两端同步,或改为消费 fullState 下发值。

### 服务端耦合点

- set 字段名必须与 `server.py` 的 set 链分支逐字对齐——`setIPO` 发 `"ipo"` 被服务端静默吞掉就是前车之鉴。
- mem 频道:服务端 6 槽(`config.py MEM_CHANNEL_COUNT`)且用 `null` 补空槽;iOS 写死 10 槽且 `as? [[String:Any]]` 遇 NSNull 整体为 nil → 常态列表为空;键名服务端 `label` / iOS 读 `name`。改这块先看分析报告 §3.1。
- 服务端 TX 音频单 owner 制:浏览器先占 `/WSaudioTX` 时 iOS 按 PTT 无声无提示。
- 服务端在 `ws.accept()` 之前 close(4001),实际表现为 HTTP 403 握手拒绝,**4001 永远到不了客户端**——`WebSocketConnection.swift:152` 的 4001 分支是死代码。

### 已知 P0(安全/崩溃,摘要自分析报告 §2)

1. **PTT 释放竞态**: `ContentView.swift:227-235` 的 `onEnded` 只在 `txStatus>0`(服务端回显)时才发 `ptt:false`,WAN 下快速点按会静默丢失释放命令,**电台可卡死在发射态**。
2. **无任何 PTT 看门狗/超时/scenePhase 保护**;服务端兜底仅断连时生效。
3. **认证失败死循环**: 4001 不可达 → 每 3s 无限重连无提示;`reconnect()` 拿已被 token 覆盖的 `connection.password` 重新登录 → 永远 401;密码输错即锁死(已写 Keychain,App 内无改密码入口,只能删 App 重装)。
4. **两处崩溃隐患**(静态分析,待真机验证): playerNode 重复 attach、installTap 重试。
5. **瀑布流误触即 QSY**: `WaterfallView` DragGesture 直接 setFrequency。

> **PTT 安全修复**: 设计 spec(`docs/superpowers/specs/2026-07-20-ios-ptt-safety-design.md`)与实施计划(`docs/superpowers/plans/2026-07-20-ios-ptt-safety.md`,29 个任务)已获批准但**尚未实施**(任务勾选 0/29)。相关文档一律写"待实施",不要当成已完成。

### 已知 P1(摘要自分析报告 §3-§4)

mem 频道子协议全线脱节(null/label/6 槽/memSave 不发/memRecall 非原子);主界面 TUNE 按钮发 `"tuner"`(天调开关)而非 `"tune"`(调谐载波);"A=B" 语义三端不一致;TX 重采样拒绝上采样(蓝牙/44.1k 路由变调);RX 无 jitter buffer/PLC(抖动后音频永久滞后);录音功能整体失效;TX Opus 链路死代码。

完整 P0/P1/P2 清单与 file:line 证据见 `docs/IOS_APP_ANALYSIS.md`,动手修复前必读对应章节。

### 仓库级约定

按仓库根 `AGENTS.md`:编辑前跑 `python3 .agents/skills/sdd-guardian/harness/sdd_context.py brief <files>`;行为变更需同步 SDD 文档。`FT710Mobile.xcodeproj` 已入库,`xcodegen generate` 会改动它——若无工程配置变更,不要把重新生成的 pbxproj 混进功能提交。

## 文档地图

| 文档 | 用途 | 可信度 |
|---|---|---|
| `docs/IOS_APP_ANALYSIS.md` | 2026-07-20 深度审计,问题清单+证据 | **权威** |
| `docs/superpowers/specs/2026-07-20-ios-ptt-safety-design.md` | PTT 安全修复设计(方案 A:PTTManager 状态机) | 已批准,**待实施** |
| `docs/superpowers/plans/2026-07-20-ios-ptt-safety.md` | 上述 spec 的 29 任务实施计划 | **待实施**(0/29) |
| `FT710Mobile/docs/ARCHITECTURE.md` | 腐化(SunsdrMobile) | **勿信,待重写** |
| `docs/IOS_BUILD_GUIDE.md` 等其余 `docs/IOS_*.md` | 旧文档,部分结论已过期 | 参考前对照分析报告 |
