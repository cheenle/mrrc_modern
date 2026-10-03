# CLAUDE.md — FT710Android

> 面向 AI 编码代理。原生 Android 遥控客户端（Kotlin + Jetpack Compose），通过 4 路 WSS 连仓库根 Python FastAPI 服务端（`server.py`），功能全量对齐 Web 前端。设计 spec：`docs/superpowers/specs/2026-08-16-ft710-android-app-design.md`；实施计划：`docs/superpowers/plans/2026-08-16-ft710-android-app.md`。

## 构建与测试

```bash
cd FT710Android
export JAVA_HOME=$(/usr/libexec/java_home -v 17)   # AGP 8.x 需要 JDK 17
export ANDROID_HOME="$HOME/Library/Android/sdk"
./gradlew test assembleDebug lintDebug   # CI 门槛：JVM 单测 + 编译 + lint
./gradlew installDebug                   # 真机安装（USB 调试）
```
- 工具链安装步骤见 `BUILD_GUIDE.md`（本机已验证：JDK 17 Corretto + SDK 35 + NDK 27.2.12479018 + Gradle 8.9 wrapper）。
- 仪器测试（`app/src/androidTest`，OpusBridge round-trip）需真机/模拟器：`./gradlew connectedDebugAndroidTest`。

## 架构速览

```
FT710App (Application) → ServiceLocator.assemble() 构造依赖闭环
  └─ MainViewModel (普通类, MainViewModelHolder/ViewModel 持有)
      ├─ ConnectionManager → 4+1 路 WebSocketConnection (OkHttp)
      │    /WSradio /WSaudioRX /WSaudioTX /WSspectrum (+ 可选 /WSatr1000)
      ├─ RadioState          (服务端字段镜像, apply 增量)
      ├─ RxAudioPlayer       (Opus/PCM → AudioTrack, 抖动缓冲)
      ├─ TxAudioCapture      (AudioRecord → Opus 编码 → WS)
      ├─ SpectrumProcessor   (1701B 帧 → 瀑布环 + FFT)
      └─ PTTManager          (安全状态机 + 看门狗)
```
纯逻辑类（协议/PTT/频谱/记忆频道/RadioState）不依赖 Android SDK，JVM 可测。
Compose 重组：`RadioState` 是可变普通类，UI 订阅 `MainViewModel.version`（`StateFlow<Long>`，每次 apply 后 +1）触发重组，随后读 `vm.state.*`。

## 协议事实（逐字对齐 server.py）

- 登录：`POST /api/auth/login` → `{"ok":true,"token"}` + `Set-Cookie: ft710_auth`；401 密码错 / 429 限流 → 回登录页并**停止重连**（`AuthApi.login` 返回 `AuthResult`）。WS URL 拼 `wss://<host>/WSxxx?token=<token>`。
- 控制通道 `/WSradio`（JSON）：下行 `fullState`（data+bands+modes+memChannels+filterTables+atr1000Enabled）、`stateUpdate`（fields 增量+dirty）、`memChannels`、`error`、`pong`；上行 `{"type":"set","field","value"}`、`{"type":"ping"}` 每 2s（`ConnectionManager` 心跳）、`{"type":"get","field":"fullState"}`、`{"type":"memSave","channels":[...]}`。
- **可设字段**：`freq` `vfo_a_freq` `vfo_b_freq` `mode` `ptt` `tune` `filter`/`filter_width` `af_gain` `rf_power` `preamp` `att`/`attenuator` `nb`/`noise_blanker` `nr`/`noise_reduction` `an`/`auto_notch` `comp`/`compressor` `tuner` `vfo` `split` `power` `squelch` `mic_gain` `scope_span` `scope_speed` `scope_mode` `nb_level` `nr_level` `comp_level`/`compressor_level` `monitor` `vox` `break_in` `key_speed` `cw_pitch` `rit` `rit_freq` `xit`。字段名以 `server.py:_execute_set_command` 为准，**禁止自创**。
- 音频：帧 = 1B tag（`0x00` PCM Int16 LE / `0x01` Opus）+ payload；48k 单声道 20ms（960 样本）。TX 恒 Opus CBR 64kbps；RX 解码 Opus 或直通 PCM。TX 文本帧 `s:` 停止、`m:` 设置。
- 频谱：`/WSspectrum` 二进制 1701B = 1B version(0x01) + 850B wf1 + 850B wf2；实际 ~5fps（`server.py:285`）。
- 记忆频道：**6 槽** + `null` 补空，键 `label`（`MemoryChannels.parse/toJson`）。
- ATR1000：`/WSatr1000` 可选，服务端禁用时 close 4000；用 `fullState.atr1000Enabled` 决定是否显示天调 UI。
- **服务端录音（AD-017，v1.15.0 起）**：`{"type":"set","field":"recording","value":true/false}` 启停；下行 `recordingState`（recording/freq_hz/started_at/duration/name/bytes/dropped，录制中 1 Hz），`fullState.recording` 给快照。**本客户端没有录音 UI 与回放**（列表/可 seek 播放/下载/删除在 Web「录音」面板）。
- **机型 key**：`MRRC_RADIO_MODEL` 共 10 个（`ft710` `ic7300` `ic7300mk2` `ic705` `ic7610` `ic7760` `ftdx10` `ftdx101d` `ftdx101mp` `ftx1`）；后六个默认拒绝发射，需服务端 `MRRC_ALLOW_UNVERIFIED_TX=1`。机型由服务端 env 决定，客户端不选。

## 安全铁律（PTT，spec §7）

- `PTTManager.release()` 无条件发 `ptt:false` + `s:`（不等回显）；`forceRelease()` 任意状态幂等。
- 手势 `finally` 兜底（`PTTButton.detectTapGestures` onPress→finally release）——修掉 iOS `onEnded` 竞态。
- `onStop`（`AppSetup` + `MainActivity`）→ `forceRelease()`；看门狗 500ms×3，重试耗尽 `onStuckTX`。
- `press()` 仅 Idle/Releasing 受理且要求控制通道已连接——避免"发不出去的乐观 TX"。

## UI 主题（唯一来源 = 手机端 Web）

界面**必须与 Web 前端一致**，令牌唯一来源是 `static/ft710.css :root`：

| 令牌 | 值 | 用途 |
| --- | --- | --- |
| `--bg-primary/secondary/tertiary/card` | `#1a1a1a / #242424 / #2a2a2a / #333` | 页面底 / 卡片 / 芯片 / 块 |
| `--accent` | `#f59e0b`（淡底 `rgba(245,158,11,.2)`） | 频率、激活态、ATU、步进钮、记忆标签 |
| `--danger` / `--success` / `--warning` | `#ef4444 / #22c55e / #eab308` | PTT / 连接点 / TUNE（黑字） |
| 文字 / 边框 / 圆角 | `#eee·#999·#666` / `#444` / 6·10·14 | — |

- 实现：`UI/Theme.kt` 的 `MrrcColors` + `AppTheme`（Material3 深色方案），**不要在页面里写死颜色**。
- 频率格式 `fmtMhz` = `%02d.%03d.%02d`（`07.013.50`）；瀑布配色 = `ft710_ui.js` 的 `WF_PALETTES.jet`（`WaterfallCanvas.jetArgb` 逐像素复刻，有 JVM 锚点测试）；波段循环 = `DEFAULT_BAND_CYCLE`（`Data/BandCycle.kt`）。
- Web 改动后要同步本 App，否则两边界面会分叉。

## 坑

- `ConnectionManager.onRadioEvent` 已解析为 `WsEvent`，`MainViewModel.onWsEvent(ev: WsEvent)` 直接消费（别传原始文本）。
- 自签 TLS：`AuthApi.selfSignedOkHttpClient()` 接受任意证书；`network_security_config.xml` 默认拒绝明文，`--no-ssl` 仅调试。
- `--no-ssl` 时 baseUrl 用 `http://`，`ConnectionManager.wsUrl` 自动转 `ws://`。
- 后台 RX 播放未实现（v1 决策）；退后台即停 TX。44.1k 设备采集重采样留作后续增强。
- `RadioState` 字段与 `radio_state.py:to_dict` 的 key 一一对应，新增字段两端同步。

## v1.0.0 协议增量（逐字对齐 server.py）

- **txhb**：控制通道 `/WSradio` 文本消息 `{"type":"txhb"}`；`PTTManager` 按键即发第一次、每 500ms 一次，`release()/forceRelease()` 停发。服务端 `server.py:1768` 以此声明活性闸门能力（`MRRC_REMOTE_SESSION_TX_HEARTBEAT_S`，只对按键方生效；TUNE 不在闸门内，客户端不接）。
- **录音（AD-017）**：控制通道 `set recording true/false`；下行 `recordingState`；REST `GET /api/recordings`（列表）、`GET /api/recordings/{name}`（MP3，支持 Range）、`DELETE`（录制中 409）；认证 Cookie `ft710_auth`（见 `RecordingsApi`）。播放/导出先经 App 的 OkHttp 下载到本地（自签证书下平台播放器不适用）。
- **CQ（AD-020）**：控制通道 `set cq true/false`；下行 `cqState{state,duration_s,elapsed_s,...}`；拒绝以 `type:error` 回复。
- **fullState 顶层新键**：`recording`、`cq`、`radioModel`（客户端按存在性做能力检测，见 `WsEvent.FullState`）。
- **只读登录**：`/WSaudioTX`、`/WSatr1000` 以 4003 关闭 → `ConnectionManager` 标记 `listenOnly`，UI 隐藏发射类入口。
- 新文件：`Network/RecordingsApi.kt`、`UI/RecordingPanel.kt`、`UI/Format.kt`、`release.sh`；签名配置在 `app/build.gradle.kts`（缺 `keystore.properties` 时 `assembleRelease` 直接失败）。
