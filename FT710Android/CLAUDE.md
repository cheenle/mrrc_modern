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

## 发布纪律（2026-10-05 起）

- **Android 发版一律用 `./release.sh --apk-only`**：只构建/签名/上传 APK + 线上 SHA-256 复核；**不碰站点页面、不在站点仓库提交、不跑全站 `deploy.sh`**。
- **卡片文字用 `./publish-card.sh`**：抓线上当前页面 → 只在本地改写 Android 标记块与 hero 安卓按钮 → 原样传回并复核（除 Android 行外逐字节不变）。**绝不跑全站 deploy**。
- 原因：站点树（`~/HAM/website/mrrc_modern` → `/Users/cheenle/HAM/mrrc_modern/website`）与 Windows/macOS 发布波共用，全站 deploy 会把对方的下载卡片回退（实测：一次 Android 发版把线上 v1.25.3 卡片短暂刷回 v1.25.0）。
- 下载卡片的版本文字由发布协调方跟进（另一条发布波会带上 Android 卡片）；需要改卡片时先确认没有并发发版，只改 `<!-- android-download -->` 标记块与 hero 安卓按钮，**不要自己跑 deploy**。
- 稳定别名 `MRRC-Modern-Android.apk` 永远指向最新 APK，所以卡片文字滞后不影响下载。
- `publish-card.sh` 上传后会 `systemctl reload nginx`：nginx 开了 `open_file_cache`（valid 60s / inactive 30s），`mv` 换文件后不 reload 会继续用旧 inode 服务最长 60 秒（实测"文件已是新版、外网仍回旧版"）。

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
  └─ RxForegroundService      (后台 RX：mediaPlayback 前台服务 + 常驻通知)
```
纯逻辑类（协议/PTT/频谱/记忆频道/RadioState/能力表/频率解析/S 表标尺/重采样/增益/调色板）不依赖 Android SDK，JVM 可测。
关键辅助模块：`Network/ChannelFlags.kt`（通道在线标志，**必须线程安全**）、`Data/Capabilities.kt`、`Data/FreqInput.kt`、`Data/SMeter.kt`、`UI/UiPrefs.kt`、`UI/Dialogs.kt`、`UI/CloudHubDialog.kt`。
Compose 重组：`RadioState` 是可变普通类，UI 订阅 `MainViewModel.version`（`StateFlow<Long>`，每次 apply 后 +1）触发重组，随后读 `vm.state.*`。

## 协议事实（逐字对齐 server.py）

- 登录：`POST /api/auth/login` → `{"ok":true,"token"}` + `Set-Cookie: ft710_auth`；401 密码错 / 429 限流 → 回登录页并**停止重连**（`AuthApi.login` 返回 `AuthResult`）。WS URL 拼 `wss://<host>/WSxxx?token=<token>`。
- 控制通道 `/WSradio`（JSON）：下行 `fullState`（data+bands+modes+memChannels+filterTables+atr1000Enabled）、`stateUpdate`（fields 增量+dirty）、`memChannels`、`error`、`pong`；上行 `{"type":"set","field","value"}`、`{"type":"ping"}` 每 2s（`ConnectionManager` 心跳）、`{"type":"get","field":"fullState"}`、`{"type":"memSave","channels":[...]}`。
- **可设字段**：`freq` `vfo_a_freq` `vfo_b_freq` `mode` `ptt` `tune` `filter`/`filter_width` `af_gain` `rf_power` `preamp` `att`/`attenuator` `nb`/`noise_blanker` `nr`/`noise_reduction` `an`/`auto_notch` `comp`/`compressor` `tuner` `vfo` `split` `power` `squelch` `mic_gain` `scope_span` `scope_speed` `scope_mode` `nb_level` `nr_level` `comp_level`/`compressor_level` `monitor` `vox` `break_in` `key_speed` `cw_pitch` `rit` `rit_freq` `xit`。字段名以 `server.py:_execute_set_command` 为准，**禁止自创**。
- 音频：帧 = 1B tag（`0x00` PCM Int16 LE / `0x01` Opus）+ payload；48k 单声道 20ms（960 样本）。TX 恒 Opus CBR 64kbps；RX 解码 Opus 或直通 PCM。TX 文本帧 `s:` 停止、`m:` 设置。
- 频谱：`/WSspectrum` 二进制 1701B = 1B version(0x01) + 850B wf1 + 850B wf2；实际 ~5fps（`server.py:285`）。
- 记忆频道：**6 槽** + `null` 补空，键 `label`（`MemoryChannels.parse/toJson`）。
- ATR1000：`/WSatr1000` 可选，服务端禁用时 close 4000；用 `fullState.atr1000Enabled` 决定是否显示天调 UI。
- **服务端录音（AD-017，v1.15.0 起）**：`{"type":"set","field":"recording","value":true/false}` 启停；下行 `recordingState`（recording/freq_hz/started_at/duration/name/bytes/dropped，录制中 1 Hz），`fullState.recording` 给快照。App 有完整录音面板（启停/列表合计/本地下载播放+seek/导出/删除）。
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
- 前台服务后台 RX 已实现（`RxForegroundService`，mediaPlayback，设置页可关）；退后台仍强制释放 TX。44.1k 设备采集已做 882↔960 重采样兜底。
- `RadioState` 字段与 `radio_state.py:to_dict` 的 key 一一对应，新增字段两端同步。
- **播放器/PTT 各自只看自己的通道**：`onAudioRxChange(true/false)` 驱动 `rxPlayer.start()/stop()`（只看 `/WSaudioRX`）；`PTTManager.isCtrlConnected` 只看 `/WSradio`。**不要**再挂到"四路全齐"的聚合上（2026-10-05：聚合曾同时门控播放器与 PTT，且 `connectedFlags` 非线程安全，丢一个 add 就表现为"控制通、频谱通、无声、不键控"）。
- **`/WSspectrum` 帧必须经 `onSpectrumFrame` 推到 `_waterfall/_fft` 两个流**，只喂 `SpectrumProcessor` 不会更新 UI（曾有整个频谱空白）。
- **S 表语义**：`s_unit` 是字符串（`"S9"`/`"+20"`/`"+60"`）；`s_meter_dbm` 是**相对 S9 的 dB**（不是 dBm）；raw 0..255，填充 `raw/255`，刻度位置见 `Data/SMeter.kt`。

## 设备侧诊断行（真机反馈的第一手证据）

主屏状态行下方有一行等宽小字，每秒刷新（`MainViewModel.startStats` → `vm.diag`），用于把"安卓链路"分段：

```
A:on F:1234 D:1184640 J:180 G:5.02 T:3 W:6200 E:0 Dr:0 Un:2 S:120 ch:R+ A+ T+ S+ TX[off mic:ok 48000 src:1997 pk:812 R:0 X:0] tx:0
```

| 字段 | 含义 | 判读 |
| --- | --- | --- |
| `A:on/off` | `RxAudioPlayer.running` | `off` = 播放器没启动（`/WSaudioRX` 未在线） |
| `F:` / `D:` | 收到的音频帧 / 解码样本 | `F=0` → 帧没到；`F>0,D=0` → 解码失败 |
| `J:` | 抖动缓冲 ms（web 水位 220/90/800） | 长期 >500 说明在丢帧追赶（`Dr` 会涨） |
| `G:` | 当前播放增益（=min(10, vol/255×boost)，TX 时 0） | `0.00` 且非发射 → 被 TX 状态卡住 |
| `T:` | AudioTrack `playState`（3=PLAYING，1=STOPPED，-1=未创建） | `-1/1` → 播放器没跑起来 |
| `W:` / `E:` | 写成功 / 写失败次数 | `E` 增长 → 音频设备异常 |
| `Dr:` / `Un:` | 超上限丢帧 / 欠载次数 | `Dr` 涨=延迟被夹住；`Un` 涨=网抖动大 |
| `ch:` | 五路在线标志（radio/audioRX/audioTX/spectrum） | 哪个是 `-` 就是哪路没连上 |
| `TX[...]` | 采集在跑否 / 权限 / 采样率 / `src:` 采集源 / `pk:` 本帧峰值 / `R:` 样本 / `X:` 帧 | `mic:NO`=没权限；`src:1997`=UNPROCESSED，`1`=MIC，`6`=旧 VOICE_COMMUNICATION；按 PTT 时 `pk` 应上千 |
| `tx:` | `tx_status`（0=RX，1=TX，2=TUNE） | 按 PTT 后应到 1 |

## 频谱/标尺不变量

- **主屏只放操作**：机型名/「实验性」徽章/设备诊断行都在**设置页「设备 / 诊断」**（主屏保持干净，用户明确要求过）；录音入口是状态行芯片、天调参数并进 ATR 行——**不要新增独占一行的小信息条**。
- **顶栏布局（两列）**：`BoxWithConstraints` 取页面宽 → 左列（`weight(1f)`）= 主频行 + 状态行 `FlowRow`（行高 32dp 用 `StatusItem` 包裹，`☰/⛶/⏻/VFO` 都在这里，窄屏自动折行）；右列 = **S 表独立区域**，宽 = 页宽×44%（140–240dp）、高 = 宽×0.64（96–168dp）。S 表尺寸**只看屏幕**，不受主频行高限制；改动顶栏时别把按钮塞回主频行，否则主频会掉到 20sp 以下。
- **面板 S 表**：弧是贝塞尔（`SMeter.arcX/arcY`），刻度/标签同 Web 的 `MARKERS/LABELS`；字号/线宽/COMP 条厚都由区域短边按比例算（`labelFs`/`readFs`），所以同一份代码在手机与平板都合适。标签不重叠由 `SMeterTest` 的逐标签宽度断言守着（改宽度/字号要跑它）。

- **绝不用 `scope_start_freq` 当显示范围**：服务端恒 CENTER 模式（EX040200），调谐后该字段滞后。范围恒为 `VFO ± span/2`（`Data/FreqScale.kt`，对齐 web `_computeFreqRange`）。点击 QSY 同样用 VFO 居中公式（`FreqInput.qsy`），两者必须同源。
- 标尺步进/格式随 span 自适应（`FreqScale.step/label`，对齐 web `_freqStep`/`_formatFreqLabel`）；刻度按真实频率位置绘制，不做等分摆放。

## 音频/Compose 不变量（改这块必看）

- **抖动缓冲是时间水位**：冷启动 220ms、欠载恢复 90ms、**硬上限 800ms 丢最旧帧**，进入 TX 时 `flush()`。逐字对齐 `static/rx_worklet_processor.js`；无上限队列会让延迟永久增长（真机"跑一段时间比 Web 慢几秒"）。
- **RX 播放增益** = `min(10, afVol/255 × capabilities.audio_gain_boost)`，TX/TUNE 期间 0（web `AUDIO_TX_DIM_FACTOR`）；FT-710 需要 10× 提升。
- **TX 采集源**：优先 `UNPROCESSED` → `MIC`（Web 明确关掉 AEC/NS/AGC）；**不要用 `VOICE_COMMUNICATION`**（强制降噪把话音压小 = "功率非常小"）。48k 优先、44.1k 走 882→960。每个候选先**探测 700ms**，没样本就换下一个（部分机型 `UNPROCESSED` 能初始化但不产数据）。
- **TX 电平**：服务端 TX 无软件增益、Web 也只多一个 0..2× 本机增益（默认 unity）→ 差异全在采集器件；手机麦默认给 1.5×（`🎙 Vol` 上限 4×），发射时状态行显示 `TX pk:` 便于现场校准。
- **JNI 的 `frame_size` 是样本数**：`GetArrayLength(jshortArray)` 不是字节数；编码/解码都必须传 960（20ms@48k）。曾误传 `len/2` → 每包只算 10ms（RX 只播一半、TX 半速）。
- **Compose：`collectAsState()` 的返回值必须被读出**，否则不建立快照订阅。`RadioState` 是普通可变类，靠 `MainViewModel.version` 驱动：`val v by vm.version.collectAsState()`，服务端侧取值放 `remember(v) { ... }`（设置页曾因丢弃返回值导致四个滑块"调不了"）。
- **权限**：`RECORD_AUDIO` 必须在登录后主动申请（否则 TX 采集静默失败：电台键控但无调制 = 没功率）；`POST_NOTIFICATIONS`（13+）用于后台 RX 通知。
- **edge-to-edge**：targetSdk 35 在 Android 15 强制全屏画到状态栏下；根布局用 `WindowInsets.safeDrawing`，状态栏/导航条图标设浅色。
- **返回键**：设置页要有 `onBack` + `RootScreen` 的 `BackHandler`（曾出现"进了设置只能退出登录"）。

## v1.0.0 协议增量（逐字对齐 server.py）

- **txhb**：控制通道 `/WSradio` 文本消息 `{"type":"txhb"}`；`PTTManager` 按键即发第一次、每 500ms 一次，`release()/forceRelease()` 停发。服务端 `server.py:1768` 以此声明活性闸门能力（`MRRC_REMOTE_SESSION_TX_HEARTBEAT_S`，只对按键方生效；TUNE 不在闸门内，客户端不接）。
- **录音（AD-017）**：控制通道 `set recording true/false`；下行 `recordingState`；REST `GET /api/recordings`（列表）、`GET /api/recordings/{name}`（MP3，支持 Range）、`DELETE`（录制中 409）；认证 Cookie `ft710_auth`（见 `RecordingsApi`）。播放/导出先经 App 的 OkHttp 下载到本地（自签证书下平台播放器不适用）。
- **CQ（AD-020）**：控制通道 `set cq true/false`；下行 `cqState{state,duration_s,elapsed_s,...}`；拒绝以 `type:error` 回复。
- **fullState 顶层新键**：`recording`、`cq`、`radioModel`（客户端按存在性做能力检测，见 `WsEvent.FullState`）。
- **只读登录**：`/WSaudioTX`、`/WSatr1000` 以 4003 关闭 → `ConnectionManager` 标记 `listenOnly`，UI 隐藏发射类入口。
- 新文件：`Network/RecordingsApi.kt`、`UI/RecordingPanel.kt`、`UI/Format.kt`、`release.sh`；签名配置在 `app/build.gradle.kts`（缺 `keystore.properties` 时 `assembleRelease` 直接失败）。

## v1.1.0 协议增量（逐字对齐 server.py）

- **`fullState` 真实形状**（D0）：`bands` = `[{name,start,end,bsr,default_freq}]`；`filterTables` = `{voice:[[idx,hz]…], narrow:[[idx,hz]…], narrowModes:[…]}`（Icom 还有 `model:"fil123"`、`filDefaults:{mode:[hz,hz,hz]}`）；新增顶层 `radioDisplayName`、`capabilities`。缺键/老服务端必须仍能解析（DTO 全默认值）。
- **`capabilities`（AD-016）消费子集**：`model_name/display_name/verified/tx_gated/has_atu/has_auto_notch/has_vd_id_meters/filter_model/att_steps/preamp_steps/scope_type/scope_spans/scope_speeds/audio_gain_boost`。量程：`civ27` 的 `freq` 是半幅，要 ×2；无 capabilities 时按 FT-710 表回退（`Data/Capabilities.kt`）。
- **filter/ATT/PRE 循环**：`width_table` → voice `[9,13,17,20,23]`、narrow `[3,6,10,13,17,21]`（窄带集合看 `narrowModes`）；`fil123` → 1→2→3。ATT/PRE 用 `att_steps/preamp_steps` 的长度（`Capabilities.nextFilter/…`）。
- **ATR1000（/WSatr1000）**：下行 `atrState{connected,power,swr,sw,ind,cap,ind_uh,cap_pf,tuning,tx,freq,last_update}`、`atrTuneResult{phase,swr_before,swr_after,message,auto}`（阶段：start/skipped/success/rollback/error + auto_*）；上行 `{"type":"atrTune"}`。文案见 `Network/Protocol.kt:AtrText`。
- **Cloud Hub REST**（认证 Cookie）：`GET /api/cloud/state`（connected/callsign/has_token/entry/cert/portal/tunnel_running/tunnel_error/cert_reload_required/autoconnect{status,at,error}）、`POST /api/cloud/apply {callsign,contact,secret}`、`POST /api/cloud/refresh`、`POST /api/cloud/restart`（返回 `{restarting:true}`，重启后需重连）。见 `Network/CloudApi.kt`、`UI/CloudHubDialog.kt`。
- **支持页**：`<baseUrl>/support.html`（服务端静态目录，AD-021），App 只负责用系统浏览器打开。
- **音频增益**：RX 播放增益 = `min(10, vol/255 × capabilities.audio_gain_boost)`，TX/TUNE 时 0；TX 采集 48k 优先、44.1k 时 882→960 后编码（`Audio/RxGain.kt`、`Audio/Resampler.kt`、`Audio/TxFraming.kt`）。
- **本地偏好（DataStore）**：`afVol 0..255=128`、`micVol 0..400=150`、`micGain 0..100`（收到 fullState 且与服务端不一致时回推一次，web `_applySavedMicGain` 同义）、`scopeTheme=jet`、`scopeFloor=5`、`scopeCeil=220`、`fftHeight=40`、`wfHeight=110`、`keepScreenOn=true`、`backgroundRx=true`（`Data/SettingsStore.kt`）。
