# FT710 Android App — 设计文档 (Design Spec)

**日期**: 2026-08-16
**状态**: 已实施（2026-08-16 完成 19 个任务的实施计划，全部提交至 main；技术栈 Kotlin + Jetpack Compose；范围全量对齐 Web 功能；方案 A 单模块分层包结构）
**前置**: 仓库根 `server.py`（Python FastAPI，4 路 WebSocket + REST 认证）、`static/` Web 前端（协议权威实现）、`FT710Mobile/` iOS App（已知坑清单见 `docs/IOS_APP_ANALYSIS.md`）
**SDD 追溯**: AD-007(PTT Release as Safety-Critical Flow)· SC8(PTT cannot stick)· NFR-008(PTT <100ms)· NFR-012(release safety)· UC-005(PTT and Tune Control)· SDD/15(PTT Safety Architecture, 7 层模型)· R4(TX release command lost)

---

## 1. 概述

为 Yaesu FT-710 短波电台新建原生 Android 遥控客户端子项目 `FT710Android/`，与 `FT710Mobile/`（iOS）平级。通过 4 路 WSS 连到仓库根的 Python FastAPI 服务端，功能**全量对齐 Web 前端**：控制（频率/模式/波段/VFO/滤波/增益/DSP/天调）、RX/TX 音频（Opus）、频谱瀑布 + FFT、S-meter + 多路仪表、记忆频道、可选 ATR1000 天调联动。

**协议基准**: 以 `server.py` + Web 前端 `ft710_main.js` 为权威参照。吸取 iOS 端教训（协议漂移、平行常量表、PTT 竞态、死代码），**从头写干净**。

**首发安全原则**: PTT 安全（状态机 + 看门狗 + 生命周期兜底）从第一版就做对，直接规避 iOS 端已确认的 P0 竞态（`docs/IOS_APP_ANALYSIS.md` §2.1/§2.2/§2.6）。

## 2. 技术栈与决策

| 项 | 决策 | 理由 |
|---|---|---|
| 语言/UI | Kotlin + Jetpack Compose（Material3） | 原生，与 iOS SwiftUI 对等；用户已确认 |
| 网络 | OkHttp（HTTP + WebSocket）+ kotlinx.serialization | 成熟稳定，CookieJar 支持认证 cookie |
| 异步 | Coroutines / Flow | 标准 |
| 偏好 | DataStore | 标准；敏感凭据加密后落 Keystore |
| 音频 | NDK/CMake 构建 libopus（RX 解码 + TX 编码） | Android 无系统级流式 Opus；iOS 同构捆绑 libopus |
| 工程 | 单 Gradle 模块 `app/` + 分层包（方案 A） | 结构简单，与 iOS `Sources/` 布局同构 |
| minSdk | 26（Android 8.0） | 覆盖绝大多数业余无线电用户设备 |
| 分发 | 签名 release APK 侧载（本地 keystore） | 沿项目现有官网下载分发方式；Play Store 留待后续 |

## 3. 工程结构

```
FT710Android/
├── settings.gradle.kts / build.gradle.kts / gradle/libs.versions.toml
├── BUILD_GUIDE.md
├── CLAUDE.md
├── app/
│   ├── build.gradle.kts
│   └── src/main/
│       ├── AndroidManifest.xml
│       ├── res/xml/network_security_config.xml   # 调试明文 HTTP 白名单
│       ├── jniLibs/<abi>/libopus.so              # NDK 构建 libopus
│       └── java/com/hamradio/ft710android/
│           ├── App/FT710App.kt              # 入口、Material3 暗色琥珀主题
│           ├── Data/RadioState.kt           # 服务端字段镜像 + 增量 dirty 追踪
│           ├── Data/MemoryChannels.kt       # 6 槽记忆频道（服务端格式）
│           ├── Data/Settings.kt             # DataStore：host/port/偏好/凭据
│           ├── Network/AuthApi.kt           # POST /api/auth/login|logout|check
│           ├── Network/WebSocketConnection.kt # OkHttp WS 封装（重连/心跳/二进制+文本帧）
│           ├── Network/ConnectionManager.kt   # 4 路 socket + 可选 /WSatr1000
│           ├── Audio/OpusBridge.kt          # JNI → libopus：RX 解码 / TX 编码
│           ├── Audio/RxAudioPlayer.kt       # AudioTrack 播放 + 抖动缓冲
│           ├── Audio/TxAudioCapture.kt      # AudioRecord 采集 → 帧 → WS
│           ├── Spectrum/SpectrumProcessor.kt # 1701B 帧 → 瀑布位图 + FFT 折线
│           ├── PTT/PTTManager.kt            # PTT 状态机（纯 Kotlin）
│           └── UI/LoginScreen.kt / MainScreen.kt / SettingsScreen.kt
```

**组件协调**: `MainViewModel`（对标 iOS `RadioViewModel`）为总协调器，持有 `ConnectionManager`、`RadioState`、`RxAudioPlayer`、`TxAudioCapture`、`SpectrumProcessor`、`PTTManager`。纯逻辑类（协议解析、PTT 状态机、频谱帧解析、记忆频道序列化、Opus 编解码）不依赖 Android SDK，可跑 JVM 单测。

**UI 主题**: MRRC 暗色琥珀品牌色（对齐 Web `ft710.css` 与官网 MRRC amber 主题）。

## 4. 界面

- **登录页**: 主机地址（默认 `radio.vlsc.net:8888`）+ 密码；凭据加密存储（Keystore），自动登录；自签证书提示；`--no-ssl` 明文仅调试。
- **主控页**（竖屏手机优先，`FLAG_KEEP_SCREEN_ON`）:
  - 顶栏: 连接状态点（CTRL/SPECT/RX/TX）+ VFO A/B + 频率大数字（±1k/±5k 步进、步进循环）
  - 瀑布 + FFT 折线（含频率刻度）
  - S-meter（S1–S9+60 渐变 + dBm 数字）+ 5 路仪表（PWR/ALC/SWR/Id/Vd）
  - 模式/波段/滤波行、ATT/PRE 循环
  - DSP 开关: NB/NR/AN/COMP/AGC；增益滑杆: AF/Mic/RF Power
  - 记忆频道 6 槽网格（点选调出、长按保存）
  - 底部: TUNE 按钮 + 大 PTT 按钮（触按保持）
- **设置页**: 主机/重连、RF 功率、scope span、证书校验开关（预留）、关于、退出登录。

## 5. 协议对照（对 `server.py` + Web 前端逐字段对齐）

**核心原则**: 消费服务端权威数据（`fullState` 下发的 `bands`/`modes`、记忆频道服务端格式），不自建平行常量表。

### 5.1 认证流
1. `POST https://<host>/api/auth/login`，JSON `{"password":"..."}`。
2. 成功 → `{"ok":true,"token":"..."}` + `Set-Cookie: ft710_auth`。
3. 4 路 WS 统一拼 `wss://<host>/WSradio?token=<token>`（每 socket 各带 token）。
4. 401 密码错 / 429 限流（5 次/5 分钟）→ 回到登录页，**停止重连**（规避 iOS 无限重连锁死）。

### 5.2 控制通道 `/WSradio`（JSON 文本）
- **下行**: `fullState`（`data` + `bands` + `modes` + `memChannels`）、`stateUpdate`（`fields` 增量 + `dirty`）、`memChannels`、`error`、`pong`。
- **上行**: `{"type":"set","field":...,"value":...}` 全部控制命令；`{"type":"ping"}` 每 2s；`{"type":"get","field":"fullState"}` 请求全量；`{"type":"memSave","channels":[...]}` 保存记忆。

### 5.3 可设字段
`freq / vfo_a_freq / vfo_b_freq / mode / ptt / tune / filter / af_gain / rf_gain / rf_power / preamp / att / nb / nr / an / comp / tuner / vfo / split / power / squelch / mic_gain / scope_span`

- 字段名以 `server.py` set 链分支逐字对齐（iOS 发 `"ipo"` 被静默吞掉的前车之鉴）。
- 注意区分 `tuner`（天调开关）与 `tune`（调谐载波）。
- 实现时逐个核对 `server.py` + `ft710_main.js` 实测。

### 5.4 音频通道（二进制）
| 通道 | 帧格式 | 方向 |
|---|---|---|
| `/WSaudioRX` | `[1B codec tag] + payload`：`0x00`=PCM Int16 LE、`0x01`=Opus 48k | 下行 → AudioTrack |
| `/WSaudioTX` | 同 tag 格式，TX 用 Opus 48k CBR 64kbps（960 样本/20ms）；另有文本帧 `s:` 停止、`m:` 设置 | 上行 ← 麦克风 |

### 5.5 频谱通道 `/WSspectrum`（二进制）
帧 = `1B version + 850B wf1 + 850B wf2`（1701B）。实际 ~5fps 广播（`server.py:285`）。

### 5.6 可选 `/WSatr1000`（JSON 文本）
ATR1000 天调状态 + 调谐辅助；服务端 `FT710_ATR1000_HOST` 为空时 close 4000 → App 隐藏天调 UI，与 Web 一致。

### 5.7 记忆频道
服务端 **6 槽** + `null` 补空槽，键名 `label`。按服务端格式实现（iOS 写死 10 槽 + 读 `name` 是全线脱节根源）。

## 6. 音频管线

### 6.1 RX 下行
```
/WSaudioRX 帧 → tag 分发
  Opus → libopus 解码（48k，960样本/20ms）→ Int16 PCM
  PCM  → Int16 LE 直通
→ 抖动缓冲（~180ms 预缓冲，吸收网络抖动）→ AudioTrack（48k 单声道 STREAM，专用 HandlerThread）
→ RMS 电平 → 仪表条
```
- 低延迟 AudioTrack 路径 + 缓冲不足静音填充；切走/来电（音频焦点丢失）→ 暂停 RX，恢复后继续；频谱丢帧冻结最后一帧。

### 6.2 TX 上行
```
AudioRecord（请求 48k 单声道；设备仅 44.1k → 线性插值重采样到 48k，服务端 TX 为 48k 域）
→ 攒 960 样本/20ms 帧 → libopus 编码（48k CBR 64kbps）→ [tag 0x01 + payload] 发 /WSaudioTX
PTT 松开 → 停采集 + 发文本帧 "s:"（SDD ch15 Layer 7 音频收尾）
```
- TX 恒 Opus（对齐 Web）；libopus 同一份同时服务 RX/TX。
- 服务端 `/WSaudioTX` 单 owner：App 侧 PTT 未按下就不开上传。

### 6.3 后台策略（v1 决策）
v1 不做后台 RX 持续播放：退后台即暂停 RX 音频并释放 TX。后台播放需前台服务 + 通知，属后续增强。

## 7. PTT 安全

### 7.1 状态机 `PTTManager`（纯 Kotlin，依赖注入闭包，JVM 可单测）
```
Phase: Idle → Keying → Keyed → Releasing
press()        仅 Idle 受理；要求 ctrl 已连接（否则拒绝，避免"发不出去的乐观 TX"）；
               sendPTT(true) + 开 TX 采集；乐观进入 Keyed（不等回显）
release()      无条件 sendPTT(false) + 停 TX 采集 + 发 "s:" + 启动看门狗
forceRelease() 任意状态幂等；断连/退后台时调用
```
依赖注入：`sendPTT(Bool)`、`sendTXAudioStop()`、`start/stopTxAudio()`、`serverTXStatus()`（回显）、`isCtrlConnected()`、`onStuckTX()`。看门狗参数 `interval=500ms`、`maxRetries=3` 为 var 可测。

**看门狗**: release 后每 500ms 读回显 `txStatus`——RX → Idle 停止；仍 TX → 重发 TX0，≤3 次；耗尽仍 TX → `onStuckTX()`（提示"电台未回 RX"）+ Idle。看门狗期间 `press()` → 取消看门狗、回 Keyed（合法再发射）。

**乐观状态与回显关系**: UI TX 指示读 `PTTManager.phase`，不读 `RadioState.txStatus`（回显延迟正是 iOS 竞态根源）；`txStatus` 仅服务看门狗判定与仪表显示。

### 7.2 手势绑定（根除 iOS `onEnded` 竞态）
```kotlin
pointerInput(Unit) {
  detectTapGestures(onPress = {
    ptt.press()
    try { tryAwaitRelease() } finally { ptt.release() }  // finally 保证手势被系统取消也一定 release
  })
}
```

### 7.3 生命周期兜底
`onStop`（退后台/来电/挂起）→ `forceRelease()`（只动 TX 态）。服务端 Layer 4（socket 断开强制 TX0）为最后防线。

### 7.4 瀑布流误触 QSY
默认点击才 QSY；拖动仅浏览不改频（Web 端瀑布流不支持拖动调频）。

### 7.5 SDD ch15 七层对照
| Layer | 实现 |
|---|---|
| 1 Touch-and-hold UX | `detectTapGestures(onPress … finally release)` |
| 2 WS command | `sendPTT` fire-and-forget（NFR-008 <100ms） |
| 3 PTT watchdog | `PTTManager` 500ms×3 |
| 4 Server dead-man switch | `server.py` 已有，不动 |
| 5/6 unload/pagehide | `onStop` → `forceRelease()` |
| 7 TX 音频收尾 | 停采集 + `"s:"` 帧 |

## 8. 连接生命周期 & 错误处理

- **重连**: 指数退避 1s→30s；2s 心跳；4 路 socket 一起重建。
- **令牌失效/401**: 停止重连、回登录页。
- **鉴权**: OkHttp CookieJar 存 `ft710_auth`；WS 拼 `?token=`；令牌内存 + Keystore 加密存储。
- **电台掉线**: `fullState.serial_connected` / `/api/health` → "Radio not connected" 提示，自动重试。
- **Android 自签 TLS**: 服务端默认自签（`ssl_bootstrap.py`），Android 默认拒绝自签 + API 28+ 禁明文。设计：
  - 默认接受自签证书（OkHttp 自定义 TrustManager），登录/设置页明示"服务端证书未验证"。
  - `network_security_config.xml` 允许明文 HTTP/WS（仅调试）。
  - 预留"校验服务端证书"开关（后续强化）。

## 9. 测试策略

- **JVM 单测**（纯逻辑，不碰 Android SDK）:
  - 协议 JSON 解析（fullState/stateUpdate/set 序列化）
  - `PTTManager` 全状态机（press 未连接拒绝 / press→release 快速连续必发 TX0 / 看门狗重试第 3 次停止并触发 onStuckTX / 看门狗期间 press 回 Keyed / forceRelease 幂等）
  - 1701B 频谱帧解析
  - 记忆频道 6 槽 + `null` 序列化
  - Opus 编解码 round-trip（可行则做）
- **仪器测试**: v1 从简；用手工验收清单在真机 + 真实服务端 + FT-710 走查。
- **仓库约定**: 编辑前 `python3 .agents/skills/sdd-guardian/harness/sdd_context.py brief <files>`；提交前 `... check --staged` 干净；行为变更同步 SDD/README 文档。

## 10. 构建与分发

- **工具链准备**（本机暂无 Android SDK / JDK 17）: `BUILD_GUIDE.md` 含 JDK 17 安装、Android cmdline-tools + SDK（`sdkmanager`）、`gradle wrapper`、模拟器可选。AGP 8.x 需要 JDK 17（本机当前 JDK 11）。
- **minSdk 26**、targetSdk 最新、Kotlin 2.x + Compose BOM。
- **分发**: 签名 release APK 侧载（本地 keystore），沿项目官网下载分发方式；Play Store 后续。
- **包名** `com.hamradio.ft710android`；显示名 "FT-710 Control"。
- **子项目文档**: `FT710Android/CLAUDE.md`（对标 iOS 份）+ 仓库根 `README.md` / `AGENTS.md` 各加子项目条目。

## 11. 验收 / 验证

1. 本机无 Android 真机：先保证 JVM 单测通过 + `assembleDebug` 编译通过 + lint 干净。
2. 真机验收清单（用户自备 Android 设备 + FT-710 + 服务端）:
   - 登录/自动登录/错误密码回到登录页
   - 频率/模式/波段/VFO/滤波/增益/DSP 全部控制生效
   - RX 音频（Opus）流畅播放；TX 发射对方可抄收（Opus）
   - PTT 快速点按 10 次每次必回 RX（原 iOS 竞态复现场景）
   - PTT 中退后台 → 电台立即回 RX
   - 瀑布实时滚动，点击 QSY，拖动不改频
   - 记忆频道 6 槽存取；ATR1000 联动（若启用）
   - 断网重连；自签证书连接成功

## 12. 影响文件 / 文档同步

| 文件 | 变更 |
|---|---|
| `FT710Android/` | 新建子项目（骨架 + 源码 + BUILD_GUIDE + CLAUDE.md） |
| `README.md` | 文档表 + 项目结构加 `FT710Android/` |
| `AGENTS.md` | 加 Android 子项目说明 |
| `docs/superpowers/plans/2026-08-16-ft710-android-app.md` | 实施计划（writing-plans 阶段产出） |

服务端零改动 → SDD 各章不变，`SDD/14-version-history.md` 不新增条目。

## 13. 开放项 / 范围边界

- 后台 RX 播放（前台服务）: 明确不在 v1。
- Play Store 分发、正式证书校验: 后续增强。
- 真机验证: 依赖用户提供 Android 设备。
- iOS 端已知坑清单（PTT/记忆频道/协议）不在本 spec 范围，但本 App 全部从第一版规避。
