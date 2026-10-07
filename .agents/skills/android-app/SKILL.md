---
name: android-app
description: MRRC Modern 安卓客户端（FT710Android/）的构建、真机分诊与 APK-only 发布——JVM 单测/Release 门槛、release.sh --apk-only 与 publish-card.sh 的边界（绝不跑全站 deploy）、真机反馈按状态行诊断行（A/F/D/J/G/T/W/E/Dr/Un/ch/TX/pk）分段定位、以及固定陷阱（fullState 真实形状、s_unit 字符串、scope_span 值域、四路聚合闸门、Opus JNI 帧长、Compose collectAsState 必须读出、抖动缓冲水位、麦克风采集源/AEC、edge-to-edge insets）。当需要出安卓包、修安卓真机问题（没声音/没频谱/没功率/延迟涨/滑块调不了/进设置出不来）、判断"是 App 还是服务端"、更新官网安卓下载卡、或改动 FT710Android/ 下任何代码时使用。
license: GPL-3.0
metadata:
  repo: HAM/hub/mrrc_modern（安卓线：worktree .worktrees/android-v1.0.0，分支 feat/android-1.0.0）
  design: docs/superpowers/specs/2026-10-05-android-client-v1.1.0-parity-design.md
  plan: docs/superpowers/plans/2026-10-05-android-client-v1.1.0-parity.md
  app-doc: FT710Android/CLAUDE.md（协议事实/不变量全文）
  build-guide: FT710Android/BUILD_GUIDE.md（签名/密钥/命令）
---

# MRRC Modern 安卓客户端

## 何时用 / 何时不用

**用**：要给安卓出包或修安卓问题；用户报"真机上没声音/没频谱/没功率/延迟越来越大/某个滑块调不了/设置退不出来"；要判断症状落在 App 还是服务端；要更新官网安卓下载卡；要改 `FT710Android/` 下任何代码。

**不用**：服务端/Web 本体 → 仓库根 `AGENTS.md` 与 `sdd-guardian`；Windows/macOS 安装包 → `windows-installer` / `macos-installer`；发版全链路与热修通道 → `mrrc-release`。

## 30 秒分诊：先读状态行诊断行

主屏状态行下方有一行等宽小字（每秒刷新），把安卓链路分段。**先要这一行，再动手**（`FT710Android/CLAUDE.md` 有完整表）：

```
A:on F:1234 D:1184640 J:180 G:5.02 T:3 W:6200 E:0 Dr:0 Un:2 S:120 ch:R+ A+ T+ S+ TX[off mic:ok 48000 src:1997 pk:812 R:0 X:0] tx:0
```

| 读到 | 断点 |
| --- | --- |
| `ch:` 里有 `-` | 那一路 WS 没连上（`R`=radio `A`=audioRX `T`=audioTX `S`=spectrum）；`A-` → 播放器不会启动，`R-` → PTT 会被拒 |
| `A:off` | RX 播放器没跑（`/WSaudioRX` 未在线） |
| `A:on F:0` | 音频帧没到（服务端停发或通道假连） |
| `F>0 D:0` | 解码失败（JNI/帧长） |
| `D>0 J:0` | 播放循环没消费（AudioTrack 起不来） |
| `G:0.00` 非发射 | 被 TX 静音卡住（`tx_status` 非 0） |
| `T:-1` / `T:1` | AudioTrack 未创建 / 未在播 |
| `E:` 涨 | 写 AudioTrack 失败 |
| `Dr:` 涨 | 延迟被 800ms 上限夹住（正常自愈）；长期 `J:`>500 才是问题 |
| `TX[mic:NO ...]` | 麦克风权限没给（TX 采集静默失败 → 没调制 = 没功率） |
| `TX[..., pk:0]` 按住说话 | 采集到了但全是 0：采集源/音量问题（`src:` 看采集源） |
| `TX[vol:100 ... pk:300]` | 电平偏低：调大 🎙 Vol（0..400）或电台 Mic Gain |
| `TX[..., pk:几十]` | 调制度太小：换 `UNPROCESSED`/调 `🎙 Vol`（默认 150，可到 400）/电台 `mic_gain` |
| `tx:1` 但没功率 | 电台在发射、缺调制（SSB 无话音 = 无功率） |

## 构建与门槛

```bash
cd FT710Android
export JAVA_HOME=$(/usr/libexec/java_home -v 17)   # AGP 8.x 需 JDK 17
export ANDROID_HOME="$HOME/Library/Android/sdk"
./gradlew test assembleDebug lintDebug              # 日常门槛
./gradlew test assembleDebug lintDebug assembleRelease   # 发版前四连
```

- 纯逻辑必须 JVM 单测（协议/能力表/频率解析/S 表/重采样/增益/调色板/Cloud REST/ChannelFlags）；UI 靠 build + 真机验收。
- 提交前 `python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged` 必须 clean；每任务一次 commit。
- 工具链/签名/密钥见 `BUILD_GUIDE.md`；缺 `keystore.properties` 时 `assembleRelease` 直接失败（故意）。

## 发布：两条命令，边界明确

```bash
cd FT710Android
./release.sh --apk-only     # 构建 → 签名核对 → 只上传 APK（版本化 + 稳定别名）→ 线上 SHA-256 复核
./publish-card.sh           # 抓线上页面 → 只改 Android 标记块与 hero 安卓按钮 → 传回 → reload nginx → 复核
```

**铁律**

- **绝不跑不带 `--apk-only` 的 `release.sh`，绝不跑站点全站 `deploy.sh`**：站点树（`~/HAM/website/mrrc_modern` → `/Users/cheenle/HAM/mrrc_modern/website`）与 Windows/macOS 发布波共用，全站部署会把对方的下载卡片回退（2026-10-05 实测：一次 Android 发版把线上 v1.25.3 刷回 v1.25.0）。
- 卡片改动只允许动 `<!-- android-download:start/end -->` 标记块与 hero 的 `<i class="fab fa-android">` 版本号；`publish-card.sh` 会 diff 复核"除 Android 行外逐字节不变"。
- 站点 nginx 开了 `open_file_cache`（valid 60s）：换文件后**必须** `systemctl reload nginx`，否则最长 60 秒仍在服务旧 inode（表现为"服务器文件已是新版、外网还回旧版"）。
- 稳定别名 `MRRC-Modern-Android.apk` 永远指向最新 APK，卡片文字滞后不影响下载。
- 版本号：`app/build.gradle.kts` 的 `versionCode`/`versionName` 递增（**当前 v1.1.9 / versionCode 12**）；`CHANGELOG.md` 顶部加条目（中文，写"症状→根因→修法"）。

## 服务端协议事实（会咬人的那些）

- **`fullState` 真实形状**：`bands` 是**对象数组** `[{name,start,end,bsr,default_freq}]`；`filterTables` 是 `[[idx,hz]…]` **数对**；顶层还有 `radioDisplayName`、`capabilities`。DTO 按简单类型写会**整条解析失败**（事件被丢，表现为"连上但无状态"）。
- **`capabilities`**：`scope_spans` 值域决定 `scope_span`；`civ27` 的 `freq` 是半幅要 ×2；无 capabilities 时回退 FT-710 表。`filter_model` 决定滤波循环（`width_table` 策划表 / `fil123` 1→2→3），ATT/PRE 用 `att_steps/preamp_steps` 长度。
- **S 表**：`s_unit` 是**字符串**（`"S9"`/`"+20"`/`"+60"`）；`s_meter_dbm` 是**相对 S9 的 dB**（不是 dBm）；raw 0..255。
- **PTT 仲裁**（服务端）：按键会话成为 `_ptt_key_ws`（只有它能释放）；`_claim_tx_owner_for_token` 在按键时把 **TX 上行**判给按键会话——所以 App 与浏览器并存时，App 按键后其音频才会被采纳（多客户端本身不是故障）。
- **可选选件（ATR-1000）判定权在服务端配置**：`config.py` 读 `MRRC_ATR1000_HOST`（`_env` 带 legacy 回退 `MRRC_*` → `FT710_*`）→ 空则 `atr = None`（server.py L162/2785/2884）→ `fullState.atr1000Enabled = atr is not None`（L3645）。App 侧两条都要门控：**渲染**（`if (atrEnabled)` 才出 ATR 行）与**连接**（`ConnectionManager.setAtrEnabled()` 由 VM 在 fullState 时调用，启用才连 `/WSatr1000`；未启用时服务端 accept 后立刻以 **4000 "ATR1000 disabled"** 关闭，白握手）。紧凑档一屏已塞满，一行 26dp 很贵，没配就别渲染。加可选通道后用 `ConnectionManager.openedPaths` 断言"只连了该连的"。
- **ATU ≠ ATR**：`ATU` 芯片是电台**内置**天调，门控 `caps.has_atu`（FT-710 backend 为 True，有真实 `AC`/`set_tuner` CAT 命令）；ATR-1000 是**外接可选选件**。用户说"ATR"时先分清是哪一个。
- **只读登录**：`/WSaudioTX`、`/WSatr1000` 以 **4003** 关闭 → `listenOnly`（隐藏发射类 UI，连接判据去掉 TX 通道）。
- **ATR1000 / Cloud Hub REST**：见 `FT710Android/CLAUDE.md` 的 v1.1.0 增量一节（字段与阶段枚举逐字对齐）。

## 客户端不变量（改代码前必须知道）

1. **通道标志必须线程安全**：`Network/ChannelFlags.kt`（`ConcurrentHashMap.newKeySet`）。4+1 路 OkHttp 回调线程并发写，普通 `mutableSetOf` 丢一个 add 就永不补齐。
2. **播放器/PTT 各自只看自己的通道**：`onAudioRxChange` → `rxPlayer.start()/stop()`（只看 `/WSaudioRX`）；`PTTManager.isCtrlConnected` 只看 `/WSradio`。**禁止**把新功能挂到"四路全齐"的聚合上（聚合曾同时门控播放器与 PTT，造成"控制通、频谱通、无声、不键控"）。
3. **频谱帧要推到 UI 流**：`onSpectrumFrame` 解析后写 `_waterfall/_fft`，只喂 `SpectrumProcessor` 不会更新界面。
4. **Compose 订阅**：`collectAsState()` 的返回值**必须被读出**；`RadioState` 是普通可变类，靠 `vm.version` 驱动 → `val v by vm.version.collectAsState()`，服务端侧取值放 `remember(v) { … }`。
5. **抖动缓冲=时间水位**：220ms 冷启动 / 90ms 欠载恢复 / **800ms 硬上限丢最旧**，进 TX `flush()`（对齐 `static/rx_worklet_processor.js`）。无上限队列会让延迟永久增长。
6. **RX 增益** = `min(10, afVol/255 × audio_gain_boost)`，TX/TUNE 期间 0；**TX 采集源**优先 `UNPROCESSED` → `MIC`（**不要 `VOICE_COMMUNICATION`**：强制 AGC/降噪把话音压小），每个候选先探 700ms 无数据就换下一个；手机麦弱，`🎙 Vol` 默认 150、上限 400（4×）。
7. **JNI `frame_size` 是样本数**：编码/解码都传 960（20ms@48k）；`GetArrayLength(jshortArray)` 不是字节数。
8. **PTT 安全铁律不退化**：`release()` 无条件发 `ptt:false`；手势 `finally`；`onStop` → `forceRelease()`；看门狗 500ms×3；txhb 按键即发、每 500ms。
9. **权限**：登录后申请 `RECORD_AUDIO`（+13 的 `POST_NOTIFICATIONS`）；采集错误必须接到界面（`tx.onError`），禁止静默失败。
10. **平台外壳**：根布局 `WindowInsets.safeDrawing`（Android 15 强制 edge-to-edge）；状态栏浅色图标；设置页要有 `onBack` + `RootScreen` 的 `BackHandler`。
11. **瀑布必须增量绘制**：`SpectrumProcessor` 每帧 `addLast+removeFirst`（列表整体位移一格），所以"每帧重建整幅位图"= 850×120＝102,000 次查表 + 408KB 分配 + 整幅 `setPixels`，全在主线程 draw → 中端机（荣耀）卡到把音频线程饿死。用 `WaterfallRingBuffer`（环形位图只写新行）+ `WaterfallRing`（纯函数下标数学，有测试）。
12. **频谱流只能在子组件订阅**：`waterfall`/`fft` 每帧都变；在主屏顶层 `collectAsState()` 会让整屏（FlowRow / S 表弧 / 仪表 / 记忆格）以 20~30Hz 重组。订阅点必须在 `SpectrumPanel` 内。
13. **音频线程要设优先级**：`playLoop()` 第一行 `Process.setThreadPriority(THREAD_PRIORITY_URGENT_AUDIO)`（必须在该线程内调用）。UI 卡顿时这是"声音出不来"的最后一道防线。draw 阶段不要分配（位图/LUT/文本 layout 全部 `remember`）。
14. **尺寸单一数据源**：dp 只从 `Data/ScreenFit.kt` 出，经 `UI/ScreenMetrics.kt`（`LocalScreenMetrics`）下发；Composable 里不许就地写死。档位按 `Configuration.screenHeightDp ≤ 900` = 紧凑（大陆主流直板机），否则标准（平板/折叠屏）。卡片内节奏由 `Panel(spacing=…)` 统一给，卡内别再写 `padding(top=…)`。M3 的 48dp 最小交互尺寸已在根部关掉，否则音量行会吃掉预算。改任何高度/间距 → 跑 `ScreenFitTest`（真机档位断言"一屏放得下"）。
- **适配下限 = 360×728**（`ScreenFit.SUPPORTED_MIN_HEIGHT_DP`，5.5" 直板机 + 三键导航）。**5" 及以下不适配**（用户 2026-10-06 明确"不用考虑"），滚动兜底即可；别再为它压尺寸。
- **屏幕余量给频谱，不留白**：`bonus = min(可用高度 − 固定预算, 80dp)` 叠加到频谱高度（`m.spectrumBonus(...)` → `SpectrumPanel(bonusDp)`）。按构造不会超一屏。加新卡片/新行之前先看这张表还剩多少余量：小米三键 360×728 只剩 **0dp**，手势 360×744 剩 0dp，荣耀 400×832 剩 24dp，华为 432×880 剩 65dp —— 也就是说**紧凑档已经塞满**，再加东西就会开始滚动。

## 真机事故档案（症状 → 根因 → 修法）

| # | 症状 | 根因 | 修法 / 版本 |
| --- | --- | --- | --- |
| 1 | 登录后"连上但没状态" | DTO 把 `bands`/`filterTables` 按简单类型解析 → 整条 `fullState` 解码失败 | 真实形状 DTO + 真实 fixture 测试（v1.1.0） |
| 2 | 没声音 + 没瀑布，控制正常 | `RxAudioPlayer.start()` 无调用点；`_waterfall/_fft` 无写入点 | 连接回调启停播放器；`onSpectrumFrame` 推流（v1.1.1） |
| 3 | 无声 + PTT 不键控（控制/频谱正常） | 播放器与 PTT 都挂在"四路聚合"上，且聚合标志非线程安全 | `ChannelFlags` + 各自通道门控（v1.1.3） |
| 4 | 设置页进得去出不来 | 无返回入口，系统返回键直接退出 App | `onBack` + `BackHandler`（v1.1.5） |
| 5 | S 表永远 `S0`、`dBm`、一有信号就顶满 | `s_unit` 是字符串却按 Int 解析；`s_meter_dbm` 是相对 dB；填充写成 `raw/32` | 字符串直显、`X dB`、`raw/255` + Web 刻度（v1.1.7） |
| 6 | 发射没功率 / 话音极小 | ①从未申请 `RECORD_AUDIO` 且采集错误未接线；②`VOICE_COMMUNICATION` 的 AGC/NS 压小声 | 登录申请权限 + `tx.onError` 接界面；采集源改 `UNPROCESSED/MIC`（v1.1.8/v1.1.9） |
| 7 | 跑一段时间比 Web 延迟几秒 | 抖动缓冲无上限：卡过一次就永远落后 | Web 水位 + 800ms 丢最旧 + TX flush（v1.1.9） |
| 8 | 设置页四个滑块"调不了" | `vm.version.collectAsState()` 返回值被丢弃 → 从不重组，松手被旧值弹回 | 读出版本 + `remember(stateVersion)` 重算（v1.1.9） |
| 9 | 官网卡片更新了、外网仍是旧版 | nginx `open_file_cache` 仍服务旧 inode | 上传后 `systemctl reload nginx`（`publish-card.sh` 内置） |
| 15 | 带着红灯发版（v1.1.20 重发一次） | `./gradlew … \| grep \| head && ./release.sh`：管道退出码取自 `head`，测试失败被吞；同时 Compose 测试放在 `src/test` 导致 release 变体必红 | 测试移到 `src/testDebug`；发版不再用 `--skip-tests`（靠 `release.sh` 的 `set -euo pipefail` 自守）；门槛命令分开跑并逐个查 `$?` |
| 14 | 荣耀等中端机**卡顿 + 声音几乎出不来** | ①瀑布每帧重建整幅位图（102,000 查表 + 408KB 分配/帧，主线程）；②`waterfall/fft` 在主屏顶层订阅 → 每帧重组整屏；③音频线程默认优先级被饿死 → AudioTrack 欠载 | 环形增量绘制（`WaterfallRing`）+ 拆 `SpectrumPanel` 订阅 + `THREAD_PRIORITY_URGENT_AUDIO`（v1.1.18） |
| 13 | **装上打开就退出**（启动即崩） | 顶栏 `height(IntrinsicSize.Min)` 包住了含 `BoxWithConstraints` 的子项 → Compose 抛"SubcomposeLayout 不支持 intrinsic 测量" | 改显式高度 `height(meterH)`；加 `UiLayoutSafetyTest` 门槛（v1.1.17） |
| 12 | v1.1.14 主屏控件整片消失（模式/波段/滤波/ATT/PRE、NR/NB/AN/COMP/ATU、音量、步进、VFO 行） | 脚本按"注释区间"整段替换 ATR 行，区间跨过了所有控制行；UI 结构无测试覆盖 | 从 v1.1.13 取回整段重插；此后发版前强制**结构 diff**（v1.1.15） |
| 11 | 频谱下频率标注不对 | 标尺用了滞后的 `scope_start_freq` + 固定 6 等分 + 统一 `%.3f` | `Data/FreqScale.kt`：VFO±span/2、自适应步进、按位置绘制（v1.1.11） |
| 10 | 键控了但"极微弱"（二次反馈） | 链路是 unity（服务端/Web 均无增益），差在手机麦电平；且部分机型 `UNPROCESSED` 初始化后不产数据 | 采集源 700ms 探测回退；🎙 Vol 上限 4×、默认 1.5×；状态行显示 `TX pk:`（v1.1.10） |

## 改 UI 的硬规矩

- **`IntrinsicSize` 绝不能包住 `BoxWithConstraints` / `Lazy*` / `TabRow`**（都是 `SubcomposeLayout`，不支持 intrinsic 测量）→ 布局阶段抛 `IllegalStateException` = **装上打开就退出**（v1.1.16 事故）。要跟固定尺寸的兄弟等高就**显式给高度**。异常原文可在 `~/.gradle/caches` 的 `ui-release.aar` 里字节级搜到（`LayoutNodeSubcompositionsState`）——**查崩溃先拿证据，别猜**。门槛：`UiLayoutSafetyTest`。
  - 写这类源码级门槛测试有三个静默失效坑：① 提取函数体要先配对跳过参数列表（`() -> Unit = {}` 的 `{}` 会被当成整个体）；② 扫描前剥注释（否则注释里提一嘴就误报）；③ Gradle 要给 Test 任务声明 `inputs.dir("src/main/java")`，否则改源码后判 UP-TO-DATE **跳过测试**。

- **对"参数默认值里带 lambda"的函数做文本插入，必须核对插入点**：`private fun connect(..., onClosedCode: (Int) -> Unit = {})` 之后第一个 `{` 是**默认参数的 lambda**、不是函数体。脚本按"第一个 `{`"插语句会把代码塞进默认 lambda → 语义全变（只有回调触发时才执行），而**编译照样通过**。这类改动只能靠测试兜住（本次是 `openedPaths` 恒空被断言抓到）。
- **UI 冒烟门槛**：`app/src/testDebug/…/UI/MainScreenComposeTest.kt`（Robolectric + `ui-test-junit4`）用真实 `MainViewModel` + 真实 fullState 把整屏 measure/layout 一遍，专治"build 全绿但装上打开就退出"。四条固定坑：① `@Config(application = Application::class)`，否则 `FT710App.onCreate` 会去 `loadLibrary("opus_jni")` 直接 `UnsatisfiedLinkError`；② 测试必须放 `src/testDebug`（`ui-test-manifest` 只给了 debug 变体，`gradlew test` 会连 release 变体一起跑 → 必红）；③ VM 要在 `setContent` **外面**建；④ fixture 用服务端真实形状（`scope_spans` 是键控字典、模式名是独立的 `mode_name`）——形状错了 `parseWsEvent` **静默**变 `Unknown`、整条 fullState 被丢，只表现为"频率是 00.000.00"。断言用 `assertExists`（滚动视口外的节点"存在但不可见"），重复文案（`7.117`、`TUNE`）用 `assertCountEquals`。
- **发版别加 `--skip-tests`**：`release.sh` 有 `set -euo pipefail`，会自己跑 `test lintDebug assembleRelease` 并在失败时中止。**绝不**写 `./gradlew … | grep | head && ./release.sh`：管道退出码取自 `head`，gradle 的红灯会被吞掉（2026-10-06 就这样把 v1.1.20 带着红灯重发了一次）。要过滤输出就分开跑、逐个查 `$?`。
- **发版前必做结构 diff**（血的教训：v1.1.14 用"从 A 注释到 B 注释整段替换"改 ATR 行，把五键行/芯片行/音量/步进/VFO 行整段吞掉，残缺包发上了官网；`gradlew test`+`lint` 全绿也拦不住，因为 UI 结构没有测试）。做法：`git show <上一个好版本>:…/MainScreen.kt` 取参照，比对 `PadBtn("…")`/`SmallChip("…")`/`MeterCell("…")` 标签、`vm.*(` 调用、`state.*`/`prefs.*` 字段、`*Dialog`/`*Panel` 的集合——**少任何一项就不发版**。大块改动用"精确锚点 + 重插"，别用大范围区间替换。
- **设计令牌只在两处**：`UI/Theme.kt`（`MrrcColors`，对齐 web `ft710.css :root`）与 `UI/Surfaces.kt`（`MrrcSurfaces` 三层表面 + `Panel`/`DisplayBezel`/`SectionLabel`/`Gap`）。就地写死颜色会让后续美化改不动。

- **顶栏布局**：第一行 = 主频显示屏（`DisplayBezel`，上沿左 `波段 · 模式`、右可点 `VFO-A/B`，中间 `FreqText`）+ **S 表独立区域**（宽=页宽×44% 即 140–240dp，高=宽×0.64 即 96–168dp），用 `height(IntrinsicSize.Min)`+`fillMaxHeight()` 做成等高；第二行 = **全宽状态行**（字体 8.5~9sp、`StatusItem` 行高 26dp、间距 5dp、`FlowRow` 仅兜底）。尺寸只看屏幕，**不要拿行高约束仪表**（用户要求过"不要受限于频率的字高度"）；按钮别塞回主频行，否则主频掉到 20sp 以下。
- **状态行必须一行装下**（用户明确要求）：加项前先估宽——当前 269dp、页面 344dp；放不下就并进已有项（如发射时速率段换成 `TX pk:`）或移到设置页。波段/模式/VFO 是"读数"，归显示屏上沿，不占状态行。
- **面板 S 表示例**：弧用贝塞尔几何（`SMeter.arcX/arcY`），刻度/标签沿用 Web 的 `MARKERS/LABELS`，内部字号/线宽按区域短边比例缩放（手机与平板同一份代码）；画弧这种几何要抽纯函数并单测（`SMeterTest` 的逐标签宽度断言当场抓出 `+20/+40` 压字）。**仪表类 UI 一律按屏幕给独立区域**，不要挤在文字行高里。
- **频谱标尺**：范围恒 `VFO ± span/2`，**绝不用 `scope_start_freq`**（服务端恒 CENTER，调谐后滞后）；步进/格式用 `Data/FreqScale.kt`（对齐 web `_freqStep`/`_formatFreqLabel`），刻度按真实位置画；QSY 必须同一公式。
- **主屏不放说明性文字**（用户 2026-10-06 两次强调）：分区标题**全档位取消**（`ScreenFit.showSectionLabels()` 恒 false）、频谱下不重复 VFO 频率（标尺已有红线+三角）、状态行只留 `☰ ⏺ RX/TX 速率 状态点 ⛶ ⏻`（内容估宽 210dp）、PTT 无副标、音量行无 `Vol` 标签、记忆格无「空」字。诊断数字（RTT/J）进设置页诊断行，不占状态行。机型名/徽章/诊断行也都在设置页。
- **任何入口不得藏在分区标题的 `trailing` 里**：v1.1.18 把记忆「管理」写在 `SectionLabel(trailing=…)`，紧凑档标题一隐藏 → 手机上 `MemoryManagerDialog` 彻底打不开（`showMemManager=true` 只有一个触发点）。要入口就给图标按钮（现在是记忆格行末的 `⋯`，26dp 无文字；标准档第二行用等宽 `Spacer` 保持列对齐），并在 Compose 冒烟测试里断言它存在。
- 记忆格列数由 `m.memoryColumns` 决定（紧凑 1×6 / 标准 2×3），索引必须 `row * m.memoryColumns + col`，别写死 3。
- **字号**：`☰` 15sp、模式徽章 9sp、状态行 VFO 芯片 9sp（`PadBtn(fontSize=…)`）；用户嫌顶栏字大。
- **对齐手机端 Web，不发明**：令牌唯一来源 `static/ft710.css :root`（`UI/Theme.kt` 的 `MrrcColors`）；瀑布配色 = `WF_PALETTES`；S 表刻度 = `renderSMeter`；QSY 公式 = `wireScopeQSY`；频率输入解析 = `commitFreq`。
- 大字号（如主频）要**按可用宽度自适应**（`BoxWithConstraints` 算 `maxWidth/字符数/0.62`），手机上不溢出、平板吃满。
- 松手才提交的滑块：本地 `local` 值显示中、`onValueChangeFinished` 提交，服务端值由版本号驱动重算——三者缺一就会"拖完弹回"。
- 新页面/新面板：`onBack`、insets、错误可见（禁止静默失败）三件套。

## 验收清单（真机，发布后）

1. 官网下载 + SHA-256 与卡片一致；安装后版本号在设置页可见
2. 登录（LAN / Hub），频率·波段·模式·记忆**立即出现**
3. RX 有声音；按 PTT 有话音（`pk` 有读数）、功率/SWR 表动；松手回 RX
4. 频谱滚动 + 点击 QSY；配色/量程/SPD 生效
5. 设置里 RF PWR/RF Gain/Mic Gain/NR/NB 拖动**留在新值**；能返回主屏
6. 连续跑 10 分钟以上：`J:` 稳定（100~300ms），无秒级累积
7. 后台接收：退后台仍有声、通知常驻可断开；TX 在退后台时立即释放
8. 录音面板：启停/合计/播放拖动/导出/删除
9. Cloud Hub 向导 / ATR 行 / 只读登录降级（有则验）

## 常用文件速查

| 关注点 | 文件 |
| --- | --- |
| 协议/DTO/ATR 文案 | `Network/Protocol.kt`、`Network/CloudApi.kt`、`Network/RecordingsApi.kt` |
| 通道/统计/门控 | `Network/ConnectionManager.kt`、`Network/ChannelFlags.kt`、`Network/NetworkStats.kt` |
| 状态镜像/偏好 | `Data/RadioState.kt`、`Data/SettingsStore.kt`、`Data/Capabilities.kt`、`Data/SMeter.kt`、`Data/FreqInput.kt` |
| 音频 | `Audio/RxAudioPlayer.kt`（水位）、`Audio/TxAudioCapture.kt`（采集源）、`Audio/RxGain.kt`、`Audio/Resampler.kt`、`Audio/TxFraming.kt`、`cpp/opus_jni.c` |
| UI | `UI/MainScreen.kt`、`UI/SettingsScreen.kt`、`UI/Dialogs.kt`、`UI/CloudHubDialog.kt`、`UI/UiPrefs.kt`、`App/RootScreen.kt`、`App/RxForegroundService.kt` |
| 发布 | `release.sh`、`publish-card.sh`、`app/build.gradle.kts`、`CHANGELOG.md` |
