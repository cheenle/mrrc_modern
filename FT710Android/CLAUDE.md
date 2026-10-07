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
- 🔴 **对方的全站部署会删掉 Android 别名并回退卡片**（2026-10-07 23:39 实测）：APK 在 `.gitignore` 里、不在他们的 checkout，`rsync --delete` 式部署会把 `downloads/MRRC-Modern-Android.apk` **整个删掉**（下载变 404），同时把页面卡片打回他们那份的旧版本号；而我上传的版本化文件 `MRRC-Modern-vX.Y.Z-Android.apk` 反而留着。
  - `publish-card.sh` 现在**开头自带别名自愈**：用 `ssh sha256sum` 比对线上别名与本地 dist 产物，不符就从服务器上的版本化文件 `cp -p` 回来（版本化文件也不对才从本地重传）。所以**发现被覆盖，重跑一次 `./publish-card.sh` 即可完全恢复**。
  - ⚠️ 验证时别信 `curl` 的 200：别名被删后 nginx `open_file_cache` 仍会用**已删除文件的旧 inode** 回 200 + 旧内容（只要有人一直在访问，缓存条目就不失效）。判断真伪要用 `ssh sha256sum` 看真实文件，或 curl 后核对 SHA。
  - 该自愈分支已做过真实演练（把别名 `mv` 走 → 跑脚本 → 自动复制回来 → 外网复核一致）。

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
- 频谱：`/WSspectrum` 二进制帧有两种长度（服务端 AD-025 起按**档位**发）：满帧 1701B = 1B version(0x01) + 850B wf1 + 850B wf2；短帧 851B = 1B version(0x01) + 850B wf1（服务端的 `full[:851]` 切片，所以 wf1 逐元素相同）。**版本字节两种帧都是 0x01**（服务端故意不把满帧改标 0x02：iOS `guard version == 0x01`，改了会静默丢帧）。
  - 档位 = 帧形状 × 帧率分频：`high` 1701B 每 tick、`mid` 851B 每 2 tick、`low` 851B 每 4 tick；只读会话的服务端内部默认档是 `listen`（1701B 每 3 tick）。客户端只能声明 `high`/`mid`/`low`（`SpectrumTiers.NAMES`，与服务端白名单逐字对齐）。
  - **不发 caps 就永远拿 high**：服务端把兼容性做成了闸门而不是客户端升级。所以 App 连接后必须主动发 `{"type":"spectrumCaps","profile":"…"}`（`ConnectionManager.connectSpectrum()`），否则手机侧一点流量也省不下来。
  - 帧率：广播 tick 是 30 Hz（`SPECTRUM_BROADCAST_FPS = 30`，硬编码无 env 覆盖），但**真频谱只在硬件帧计数前进时才出帧——实测 11.1 fps ≈ 151 kbps ≈ 68 MB/小时**（high 档）。S-meter 回退态才是每 tick 重造一帧（≈408 kbps ≈ 180 MB/小时）。旧文档写的“实际 30fps ≈ 51KB/s ≈ 180MB/小时”把两个态揉成了一个数字。
  - ✅ `_broadcast_spectrum_loop` 的 docstring **已修对**（服务端 v1.26 起写的就是 30 Hz tick / 实测 11.1 fps / 851 与 1701 两种帧长）。旧版本它曾写着 "Runs at 5 fps (200ms interval)"，当时确实不能信；现在可以直接信，不必再绕开它去读常量。只读会话的 `LISTEN_SPECTRUM_DIVIDER = 3` 现在从服务端档位表取值（仍为 3），它是**比例**而不是绝对帧率：真频谱态 ≈3.7 Hz，回退态 ≈10 Hz。
- 记忆频道：**6 槽** + `null` 补空，键 `label`（`MemoryChannels.parse/toJson`）。
- **记忆格只显示频率**（2026-10-07 用户定案）：格内单个 `%.3f`（空槽 `M1`…`M6`），标签只在 `⋯` 管理对话框里看。字号上限 11sp（标准档 13sp），格高 34dp（标准档 40dp）。
- **记忆格文字必须按格宽自适应**（紧凑档一行 6 格 + `⋯`，360dp 屏上每格只剩 47dp）：
  - 字号 = `ScreenFit.memoryFreqFontSize(文本, 格宽, compact)`，公式 `格宽/(字符数×0.62)`（等宽数字恒半角），夹在 5.5sp ~ 档位上限
  - 必须 `maxLines = 1` + `softWrap = false` + `TextOverflow.Ellipsis`：放不下就省略号，**绝不换行**（格高固定，换行会把整格顶变形）
  - 守卫：`ScreenFitTest` 的不变量（要么放得下、要么已到下限交给省略号）+ `MainScreenComposeTest` 的实测断言（逐格：只有一个文本节点、内容等于预期频率、宽度 ≤ 格宽、单行；长/中文标签在主屏一处都不出现）
  - ⚠️ 若将来恢复显示标签：`memoryLabelFontSize` 已随标签删除，需重新实现并按 **CJK 1.0em / 半角 0.62em** 估宽（中文按半角算必溢出）
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

## 可选选件（ATR-1000）

- **判定权在服务端配置**：`config.py` 读 `MRRC_ATR1000_HOST`（`_env` 带 legacy 回退 `FT710_*`）→ 空则 `atr = None` → `fullState.atr1000Enabled = false`。App 侧：`if (atrEnabled)` 才渲染 ATR 行，且 `setAtrEnabled()` 才连 `/WSatr1000`（未启用时服务端会 accept 后立刻以 **4000** 关闭）。
- 未启用时**不许**去连 ATR 通道，也不许渲染 ATR 行（紧凑档一屏已塞满，一行 26dp 很贵）。
- `ATU` 芯片是**另一回事**：电台内置天调，门控是 `caps.has_atu`（FT-710 backend 为 True，有真实 `AC`/`set_tuner` CAT 命令）。别把 ATU 和 ATR 混为一谈。
- 加了可选通道后，用 `ConnectionManager.openedPaths` 断言"只连了该连的"。

## 性能不变量（中端机实测踩过）

- **瀑布必须增量绘制**：`SpectrumProcessor` 每帧 `addLast(new)+removeFirst()`，列表整体位移一格，所以旧写法每帧重建整幅（850×120＝102,000 次查表 + 408KB 分配 + 整幅 setPixels，全在主线程 draw）→ 荣耀这类中端机卡到把音频线程饿死。现用 `WaterfallRingBuffer`（环形位图，只写新行）+ `WaterfallRing`（纯函数下标数学，有测试）。**不要改回"每帧重建"**。
- **频谱流只能在子组件里订阅**：`waterfall`/`fft` 每帧都变，在主屏顶层 `collectAsState()` 会让整屏（FlowRow/S 表弧/仪表/记忆格）以 20~30Hz 重组。订阅点在 `SpectrumPanel` 内。
- **后台必须停频谱通道**：high 档真频谱实测 ≈151 kbps ≈ **68 MB/小时**（S-meter 回退态 ≈408 kbps ≈ 180 MB/小时），退后台看不见瀑布还一直收纯属浪费流量与 CPU。链路：`MainActivity.onStart/onStop` → `vm.onAppForeground()` → `ConnectionManager.setSpectrumPaused()`。档位（AD-025）能把 high 降到 mid/low（≈38 / ≈19 kbps），**但后台一律直接停**：省流量最狠的一档是“根本不连”。
  - 🔒 **暂停期间 `/WSspectrum` 不能计入连接判据**（纯函数 `requiredChannels(listenOnly, spectrumPaused)`，有测试）：否则 `isConnected` 变 false → `syncBackground()` 依赖它 → **后台接收被自己关掉**，用户以为在收音其实早断了。
  - 后台期间的会话重连不能把频谱又拉起来（`start()` 要尊重暂停状态）；诊断摘要用 `S(p)` 区分"主动暂停"与"断了"。
  - 不影响音频与 PTT：RX 播放只看 `/WSaudioRX`、PTT 只看 `/WSradio`（通道分离原则）。
- **音频线程必须设优先级**：`playLoop()` 第一行 `Process.setThreadPriority(THREAD_PRIORITY_URGENT_AUDIO)`（在该线程内调用才生效）。UI 卡顿时它是"声音出不来"的最后一道防线。
- **draw 阶段不做分配**：Canvas 里别 `IntArray(...)`/`Path()` 每帧新建；位图、LUT、文本 layout 都 `remember` 住。

## 屏幕适配（单一数据源）

- 尺寸**只从 `Data/ScreenFit.kt` 出**，经 `UI/ScreenMetrics.kt`（`LocalScreenMetrics`）下发；Composable 里不许就地写死 dp，否则 `ScreenFitTest` 的"一屏放得下"预算就和真实布局脱节。
- 档位：`screenHeightDp ≤ 900` = 紧凑（大陆主流直板机），否则标准（平板/折叠屏）。`screenHeightDp` 已扣除状态栏与导航栏，正好是可用高度。
- 卡片内节奏由 `Panel(spacing=…)` 统一给，**不要**在卡内再写 `padding(top=…)`。
- M3 的 48dp 最小交互尺寸已在根部关掉（`LocalMinimumInteractiveComponentSize provides 0.dp`），密集仪表盘按档位高度走。
- **适配下限 = 360×728**（`ScreenFit.SUPPORTED_MIN_HEIGHT_DP`，5.5" 直板机 + 三键导航）。5" 及以下不做保证（用户 2026-10-06 明确"不用考虑"），滚动兜底。
- **频谱高度是确定值，没有"余量自动填充"**：`round((SpecH + WfH) × spectrumScale) + 16`，紧凑档 `spectrumScale = 0.78` → 默认设置下 **133dp**；标准档（平板）`1.0` → 166dp。
  - 曾经有过 `spectrumBonus`（把屏幕余量灌给频谱，封顶 80dp），**2026-10-07 已移除**：它会让"把瀑布调矮"这类需求失效 —— 省下的空间立刻被填回去。要改频谱高度就改 `spectrumScale`，别再引入自动填充。
  - 副作用：高屏机型底部会留白（荣耀 400×832 约 131dp、411×892 约 189dp）。滚动列内容顶对齐，不会被裁切。
  - `spectrumHeight` 用 `round` 不用 `toInt`：`150 × 0.78f` 在 Float 下是 `116.99999…`，截断会白丢 1dp。
- 改了任何高度/间距 → 跑 **`OneScreenFitTest`**（权威：Robolectric 在 7 个真机档位上真 measure/layout，读 `VerticalScrollAxisRange.maxValue`，>0 就是要滚动）+ `ScreenFitTest`（算术模型）。两者不一致时**以实测为准**，回去改 `ScreenFit` 的常量。
- **`ScreenFit` 是模型，不是真相**：v1.1.18~v1.1.21 期间 `MainScreen` 里留着内联的 `(maxWidth*0.44f)`/`(meterW*0.64f)`，`ScreenMetrics.headerHeight` 是死代码 —— 手机上顶栏一直用标准档比例，比模型高 22~26dp，360dp 宽的机器实际超出 20dp 要滚动，而 `ScreenFitTest` 全绿（模型自己跟自己一致）。**任何尺寸都必须从 `LocalScreenMetrics` 取，禁止在 Composable 里就地算比例。**
- 分区都带 `testTag`（`secToolRow`/`secHeader`/`secSpectrum`/`secMeters`/`secControls`/`secTuning`/`secMemory`，Panel 走 `tag=` 参数），量高度靠它们；`OneScreenFitTest` 逐区打印到 `build/screenshots/one-screen-fit.txt`。
- 想看设计不装 APK：`./gradlew :app:testDebugUnitTest --tests "*ScreenshotTest*"` → `app/build/screenshots/main-rx-*.png`、`main-tx-*.png`（Robolectric NATIVE 渲染）。

## 频谱/标尺不变量

- **`IntrinsicSize` 绝不能包住 `BoxWithConstraints` / `Lazy*` / `TabRow`**：它们是 `SubcomposeLayout`，不支持 intrinsic 测量，布局阶段抛 `IllegalStateException` → **启动即崩**（v1.1.16 事故：顶栏用 `height(IntrinsicSize.Min)` 让显示屏与 S 表等高）。要和固定尺寸的兄弟等高，就**显式给高度**（本仓：`Row(Modifier.height(meterH))`，`meterH` 由屏宽算出）。门槛：`UiLayoutSafetyTest`（静态扫描，命中即失败）。
- **Python/脚本改代码后必须验证替换生效**：`str.replace` 锚点不匹配会**静默失效**，编译照过、测试照绿（本仓已踩三次：v1.1.18 的 header 内联公式、ATR 行 TUNE 的 `height(30.dp)`、测试断言更新）。脚本里一律 `assert old in s` 再 replace，改完 `grep` 复核。
- **UI 冒烟门槛（Compose 能组合、能布局）**：`app/src/testDebug/java/.../UI/MainScreenComposeTest.kt`（Robolectric + `ui-test-junit4`）。布局期异常在 CI 上原本是隐形的，只会在真机上表现为"装完打开就退出"（v1.1.16）。踩过的坑，改这条测试时照抄：
  - `@Config(application = Application::class)` —— 否则 Robolectric 会跑真的 `FT710App.onCreate` → `ServiceLocator` → `RxAudioPlayer` → `OpusBridge.<clinit>` → `System.loadLibrary("opus_jni")` → `UnsatisfiedLinkError`
  - 测试必须放在 **`src/testDebug`**：`ui-test-manifest` 只给了 `debugImplementation`，release 变体没有那个 Activity，`./gradlew test` 会两个变体都跑 → release 变体必红
  - **VM 要在 `setContent { }` 外面建**：写在里面每次重组都新建一个
  - fixture 必须用服务端**真实形状**（`scope_spans` 是键控字典 `{"6":{"name":"100 kHz","freq":100000}}` 不是数组；模式名是独立字段 `mode_name`，`mode` 只是索引）。形状不对时 `parseWsEvent` **静默**回退成 `Unknown`、整条 fullState 被丢 → 表现为"频率是 00.000.00"，很难查（`ProtocolTest` 里有一条专门的回归守护）
  - 冒烟测试用 `assertExists()` 而不是 `assertIsDisplayed()`：滚动列视口外的节点是"存在但不可见"
  - 文案可能重复：`7.117` 两处（标尺红标 + 记忆格）、`TUNE` 两处（ATR 行 + 底栏）→ 用 `assertCountEquals`
- **发版别加 `--skip-tests`**：`release.sh` 有 `set -euo pipefail`，不带该参数时会自己跑 `./gradlew test lintDebug assembleRelease` 并在失败时中止。另外**绝不**写 `./gradlew … | grep | head && ./release.sh` —— 管道退出码取自 `head`，gradle 的失败会被吞掉（2026-10-06 就这样带着红灯把 v1.1.20 重发了一次：同版本号、不同字节 SHA）。要过滤输出就分开跑并逐个查 `$?`。
- **绝不在同一工程并行跑两条 gradle / 发布命令**（2026-10-07 踩了三次）：
  - 两个 `gradlew` 并发写同一个 `build/test-results/` → 报 `Could not write XML test results`，**看着像测试失败其实是构建互踩**；已写出的 XML 仍是全绿。
  - `publish-card.sh` 与 `release.sh` 并发 → 卡片脚本读到上传前的旧页面，**卡片版本号落后一版**（v1.1.26 发完卡片还写 v1.1.25）。发布必须严格串行：`release.sh` 跑完 → 再 `publish-card.sh`。
  - 卡片更新后 nginx `open_file_cache`（valid 60s）会让旧版本号多存活最多 60 秒 → **等 ~65 秒再 curl 复核**中英文页。
- **文档改完必须 grep 已删符号**：脚本改文件要"改一处写一处"，且发版前 `grep` 一遍本次删除的函数/常量名，确认 CLAUDE.md 与 skill 里没有残留指向（v1.1.25 曾提交出"文档教人用已删函数"，v1.1.26 发现 CLAUDE.md 还在写崩溃用的 `IntrinsicSize.Min`）。
- **UI 结构改动必须做结构 diff**（2026-10-05 事故：v1.1.14 改 ATR 行时脚本替换范围划到"录音入口"，把五键行/芯片行/音量/步进/VFO 行整段吞掉并发到官网；JVM 单测与 lint 都盖不住 UI 结构）。发版前跑：
  ```bash
  git show <上一个好版本>:FT710Android/app/src/main/java/com/hamradio/ft710android/UI/MainScreen.kt > /tmp/good.kt
  # 比对集合：PadBtn("…") / SmallChip("…") / MeterCell("…") 标签、vm.*( 调用、state.* / prefs.* 字段、*Dialog/*Panel
  ```
  **少任何一项就不许发版**。大块替换优先用"精确锚点 + 重插"，不要用"从 A 注释到 B 注释整段替换"。
- **设计令牌与表面**：颜色/圆角/表面层级只在 `UI/Theme.kt`（`MrrcColors`）与 `UI/Surfaces.kt`（`MrrcSurfaces` + `Panel`/`DisplayBezel`/`SectionLabel`/`Gap`）里定义；就地写死颜色 = 以后改不动。三层表面：内凹显示屏 `Inset`（主频/频谱）< 卡片 `Panel` < 按键 `Key`。
- **主屏不放说明性文字**（用户 2026-10-06 明确要求）：分区标题全档位取消、频谱下不再重复 VFO 频率、状态行只留 `☰ ⏺ RX/TX 速率 状态点 ⛶ ⏻`、PTT 无副标、音量行无 `Vol` 标签。**任何入口不得藏在标题的 trailing 里**（v1.1.18 的「管理」入口就是这样在紧凑档丢的）——需要入口就给图标按钮（如记忆格行末的 `⋯`）。诊断类数字（RTT/J）放设置页诊断行，不占状态行。
- **主屏只放操作**：机型名/「实验性」徽章/设备诊断行都在**设置页「设备 / 诊断」**（主屏保持干净，用户明确要求过）；录音入口是状态行芯片、天调参数并进 ATR 行——**不要新增独占一行的小信息条**。
- **顶栏布局（v1.1.28）**：`BoxWithConstraints` 取页面宽 →
  1. **工具行**（全宽独立一行，`testTag("secToolRow")`，高度 = `ScreenFit.headerRowHeight` = 图标点击区 22/26dp）：
     `☰`(最左) ｜ `波段 · 模式` ｜ `RX/TX · 速率|TXpk · 录音时长 · 只读/无声 · 串口点` ｜ `⏺ ⛶ ⏻ VFO-A/B`(右侧动作区)
     - 用户明确要求：**频率上方只此一行**，RX/TX 紧跟波段·模式之后，所有状态图标与信息都在这一行
     - 必须**横跨页宽**（344dp）：塞回显示屏内部只有 `bezelContentWidth`≈167dp，装不下约 246dp 内容（`TOOL_ROW_CONTENT_W`）
     - **抗挤压**：信息组装在 `weight(1f)` 的嵌套 Row 里，组内只有"波段·模式"是 `weight(1f, fill=false)` → 挤不下时**它先出省略号**，右侧图标不会被裁
     - 这一行**不折行**（定高 Row）→ 加项前必须先跑 `ScreenFitTest` 的宽度断言
  2. **主频显示屏 + S 表**（等高，`m.headerHeight`，宽 = 页宽×44%＝132–240dp）
     - ⚠️ 等高靠**显式高度**，**绝不用 `IntrinsicSize.Min`**（S 表内部是 `BoxWithConstraints`=SubcomposeLayout，问 intrinsic 会启动即崩）
     - 显示屏里**只有主频**（`weight(1f)` 居中），字号 = `min(byWidth, byHeight)`，`byWidth=(宽−8)/6.3`、`byHeight=(高−4)/1.32`，夹 **16~62sp**
  - **图标一律 Canvas 自绘（`HeaderIconButton`），禁用字体符号**：`☰ ⛶ ⏻ ⏺`（U+2630/26F6/23FB/23FA）在不少机型没有字形会走**字体回退** → 粗细不一、缺笔画（用户报的"关闭 icon 变形"）。自绘线宽按图形尺寸比例算（`g×0.13`），任何 dpi/字体设置下一致；尺寸 = `ScreenFit.headerIconTap`(22/26dp) + `headerIconGlyph`(11/13dp)；带 `contentDescription`（无障碍 + 测试可查）；录音态用 `filled=true` 画实心红圆。
  - 工具行**不在** `DisplayBezel` 内 → 语义未被合并，`onNodeWithTag("secToolRow")` 不需要 `useUnmergedTree`（v1.1.27 那条坑已随之消失）。
- **S 表**：按用户给的参考图 `smeter-s7.svg` 画（**SVG 是文本，可直接读**；PNG 截图当前模型看不了）。
  - 弧线**两段两色**：S1–S9 近白、+20/+40/+60 用 `Danger` 红，分界 = `SMeter.MARKERS[9]`（参考图注释里的 "S9+10 mark"）
  - 刻度从弧线**向外（向上）辐射**、两端略外倾；主刻度长且亮、次刻度短且暗；dB 区刻度红
  - 数字 `1 3 5 7 9 +20 +40 +60` 在刻度**上方**并跟着弧度走（不是等分摆放）
  - 指针 = 一条橘色细线从弧上当前位置垂到面板底部；**没有彩色填充带**（参考图就是静态双色刻度 + 指针）
  - 与参考图**有意不同**（用户要求）：去掉 `S7` 的 lime 黄圆角底牌（`#c9d92b`），读数用本 App 橘色 `Accent`；参考图角落的大 `S`/`dB` 由**实际读数**取代（`S9` + `+12 dB`）
  - 弧几何是纯函数（`SMeter.arcX/arcY`），字号/线宽按区域短边比例算（`labelFs`/`readFs`），手机与平板同一份代码；标签不重叠由 `SMeterTest` 的逐标签宽度断言守着。ALC/COMP 归仪表卡那一格，S 表不重复。

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
