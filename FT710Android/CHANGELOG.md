# FT710Android Changelog

App 版本独立于服务端版本；全功能需服务端 ≥ v1.22（txhb 闸门），更低版本自动降级。

## [1.1.9] — 2026-10-05

- **修复设置页滑块"调不了"**（用户反馈：RF PWR / RF Gain / NR Level / NB Level 在菜单里拖完就弹回）：设置页写了 `vm.version.collectAsState()` 但**没有读出返回值**，Compose 因而从不因 `stateUpdate` 重组——`state.rfPower/rfGain/nrLevel/nbLevel` 永远停留在首次打开时的值，松手即被旧值弹回。现在真正订阅状态版本，并把四个服务端值放进 `remember(stateVersion)` 重算（拖动中的本地值仍由滑块自己记住）。Floor/Ceil/Vol 这些"能调"的滑块其实只是因为写 DataStore 顺带触发了整页重组，掩盖了此 bug
- **修复发射"出不去功率"/话音极小**：`AudioSource.VOICE_COMMUNICATION` 会强制 AGC/降噪把话音压下去；Web 是明确 `echoCancellation/noiseSuppression/autoGainControl: false`。改为优先 `UNPROCESSED` → `MIC`（48k 优先、44.1k 兜底），并在诊断行加 `src:` 与 `pk:`（本帧峰值），方便看清调制度
- **修复 RX 延迟随时间涨到秒级**：抖动缓冲改为 Web `rx_worklet_processor.js` 的时间水位（冷启动 220ms / 欠载恢复 90ms / **硬上限 800ms 丢最旧帧**），并在进入 TX 时 flush；早期实现是无上限队列，卡过一次后永远落后（到达速率=消费速率，差额再也追不回来）。诊断行加 `Dr:`（丢帧）/`Un:`（欠载）
- 麦克风权限：登录后主动申请 `RECORD_AUDIO`（缺权限时 TX 采集静默失败 = 无调制无功率），并把采集错误接到界面错误条
- 测试 109 项全绿

## [1.1.8] — 2026-10-05

- **修复"发射经常出不去功率"**（用户反馈；Web 浏览器同机正常）。根因是 **App 从未申请过 `RECORD_AUDIO` 运行时权限**，而 `TxAudioCapture.start()` 在无权限时**静默返回**、`onError` 又没接线——结果：PTT 命令发到服务端、电台键控，但一个麦克风采样都没有，SSB 无调制 = 没有功率。浏览器有麦克风权限，所以 Web 正常
  - 登录成功后一次性申请麦克风权限（拒绝时给出可操作提示：系统设置 → 应用 → 权限 → 麦克风）
  - `tx.onError` 接到界面错误条（采集失败不再静默）
  - 诊断行 TX 段加权限状态：`TX[rec mic:ok 48000 R:… X:…]`，`mic:NO` 一眼可见
- 服务端无需改动：`_claim_tx_owner_for_token` 在按键时把 TX 上行判给按键会话（浏览器 + App 并存也正确）
- 测试 109 项全绿

## [1.1.7] — 2026-10-05

- **修复 S 表显示与数字**（用户反馈），三处都对着服务端/Web 校准过：
  - `s_unit` 服务端是**显示字符串**（`"S9"` / `"+20"` / `"+60"`），App 之前按 `Int` 解析 → 永远显示 `S0`；现在原样显示
  - `s_meter_dbm` 是**相对 S9 的 dB**（S9 = 0 dB 参考），之前标成 `dBm`；现在显示 `X dB`
  - 填充比例改为 `raw/255`（旧实现 `raw/32`，一有信号就顶满）；刻度补上 `+60`；条改为 Web `renderSMeter` 的横向渐变（绿→黄→橙→红）+ 16 条刻度线（`[0,12,27,…,255]`）
- 测试 105 → 109 项（新增 S 表标尺 3 项 + `s_unit` 字符串解析 1 项）

## [1.1.6] — 2026-10-05

- **PTT 按钮高度 +50%**（用户反馈）：64 → 96dp，标签 18 → 22sp；同一行的 CQ / TUNE 保持 64dp 垂直居中
- 测试 105 项全绿

## [1.1.5] — 2026-10-05

- **修复设置页无法返回**（用户反馈：进去后只能退出登录）：设置页加顶部「← 返回」与底部「返回主屏」，并用 `BackHandler` 接管系统返回键/手势——在设置页按返回回主屏，不再直接退出 App
- 测试 105 项全绿

## [1.1.4] — 2026-10-05

- **尊重系统窗口插入区**：targetSdk 35 在 Android 15 上强制 edge-to-edge，之前顶部内容画在系统状态栏底下（用户反馈"手机/平板原来的显示没保留"）。现在根布局应用 `WindowInsets.safeDrawing`：顶部给状态栏留出空间、底部避开导航/手势条；并把状态栏/导航条图标设为浅色（深色背景上可读）
- **主频字号翻倍**：32 → 64sp，并按可用宽度自适应（手机不溢出、平板拿满 64sp）
- 测试 105 项全绿

## [1.1.3] — 2026-10-05

- **修复聚合闸门单点故障**（真机：控制通、频谱通，但 RX 无声且 PTT 不键控 → 无功率/无话音）：`connectedFlags` 是非线程安全的 `mutableSetOf`，被 4+1 路 OkHttp 回调线程并发 add/remove，丢一个 add 就再也不会补齐 → 四路聚合永不成立；而 `rxPlayer.start()` 与 `PTTManager.press()` 都挂在这个聚合上，频谱与控制却不挂 → 症状完全吻合。改用 `ChannelFlags`（`ConcurrentHashMap.newKeySet`）+ `@Synchronized updateConnected`
- **拆掉单点闸门**：RX 播放只看 `/WSaudioRX` 自己的在线状态（`onAudioRxChange`）；PTT 的 `isCtrlConnected` 只看 `/WSradio`（与 Web `ptt_manager.js` 语义一致，不再等音频通道）
- 诊断行扩展：补每路通道标志 `ch:R+ A+ T+ S+` 与 TX 侧计数 `TX[rec 48000 R:样本 X:Opus帧] tx:<tx_status>`，一条截图即可定位 RX/发射各自的断点
- 测试 101 → 105 项全绿（新增 ChannelFlags 并发不变量 4 项）

## [1.1.2] — 2026-10-05

- **修复 JNI 的 Opus 帧长错误**：`GetArrayLength` 对 `jshortArray` 返回的是**样本数**，编码/解码却都传了 `len/2` → 每包只按 10ms 处理（RX 只播一半、TX 半速）。现按 960 样本（20ms@48k）收发
- **新增设备侧音频诊断行**（状态行下方等宽小字），定位“控制通、频谱通、没声音”卡在哪一段：
  - `A:on/off` 播放器是否在跑（`on` 说明连接聚合已齐、`start()` 已调）
  - `F:` 收到的 `/WSaudioRX` 帧数 · `D:` 解码样本总数 · `S:` 频谱行数
  - `J:` 抖动缓冲毫秒 · `G:` 当前播放增益（`0.00` = 被 TX 静音卡住）
  - `T:` AudioTrack 状态（3=PLAYING / 1=STOPPED / -1=未创建）· `W/E:` 写成功/失败次数
- 测试 101 项全绿

## [1.1.1] — 2026-10-05

- **修复：真机上无 RX 声音、无频谱**（控制正常）。两个 v1.0.x 遗留接线缺口，规划期与 JVM 单测都看不到（测试注入的是 null 播放器/处理器）：
  - `RxAudioPlayer.start()` **无任何调用点**：`running=false` 时 `onFrame` 直接丢弃所有音频帧 → 永远静音。现在音频播放随连接聚合启停（`MainViewModel.onConnectionChange`），`start/stop` 自身幂等
  - `MainViewModel._waterfall/_fft` **无任何写入点**：频谱帧只喂了 `SpectrumProcessor`，UI 流始终为空 → 瀑布/FFT 空白。现在 `onSpectrumFrame` 解析后推送两个流
- 新增回归测试：频谱帧能到达 UI 流；音频播放器随连接聚合启停（虚拟播放器计数）
- 测试 99 → 101 项全绿

## [1.1.0] — 2026-10-05

- **修复阻断缺陷**：v1.0.1 无法解析真实 `fullState`——服务端 `bands` 是对象数组（`name/start/end/bsr/default_freq`）、`filterTables` 是 `[idx,hz]` 数对，而 DTO 写成了 `List<String>/List<Int>`，每次解码抛异常、事件被丢弃，登录后一直“连上但无状态”。现按真实形状解析并用与服务端同形的 fixture 锁死（D0）
- **量程修复**：Scope SPAN 改用服务端 `capabilities.scope_spans`（`civ27` 半幅 ×2），旧版“50k/100k/1M”实际发的是 1k/2k/5k；标尺/点击 QSY 与设置页共用同一张表
- **主屏对齐手机端 Web**：点击瀑布/FFT QSY、点击频率数字输入 MHz、ATR1000 行（PWR/SWR/L-C + TUNE，含自动调谐阶段提示）、状态行码率 ↓↑/RTT/抖动、无声告警、TUNE 状态、连接开关 ⏻、全屏 ⛶、记忆频道 label、未验证机型“实验性”徽章
- **设置对齐**：Scope 面板（全量程/SPD/6 套配色/Floor/Ceil/Spec H/WF H）、RF Gain（0-100%↔0-255）、Mic Gain（本地持久化并在重连后回推）、🎙 Vol、🔊 Vol、NR/NB 电平、波段/模式选择器、Memory Manager（含清除）、机型与版本显示
- **新增**：Cloud Hub 接入向导（`/api/cloud/state|apply|refresh|restart`，含 `cert_reload_required` 的重启流程）、「🐞 遇到问题」与「连接设置」浏览器入口
- **音频**：本机播放音量（web 🔊 Vol 语义）+ 每种机型的 `audio_gain_boost`（FT-710 = 10×）+ TX/TUNE 期间静音防自噪回声；TX 采集 48k 优先、只给 44.1k 时 882↔960 重采样；Mic Vol 本机软件增益
- **后台接收**：`mediaPlayback` 前台服务 + 常驻通知（可断开），退后台继续听；TX 仍在 `onStop` 无条件释放
- **录音面板**：进度拖动 seek + 列表合计（条数/字节）
- **测试**：JVM 单测 52 → 99（协议真实形状、能力表、频率解析/QSY、ATR、重采样、增益、6 套调色板锚点、Cloud REST）；`test + assembleDebug + lintDebug + assembleRelease` 全绿
- 发版：`versionCode 3`；APK 与官网（en/zh）下载卡同步更新

## [1.0.1] — 2026-10-03

- **界面按手机端 Web 全量重做**（用户要求「根据手机端 WEB 更新」）：设计令牌逐字取自 `static/ft710.css :root`（`#1a1a1a` 底 / `#f59e0b` 琥珀 / `#ef4444` 红 / 圆角 6·10·14 / 等宽频率），新增 `UI/Theme.kt` 挂成全局 Material3 深色主题，登录/设置/录音面板一并继承
- 主屏布局对齐 Web：`☰ + 07.013.50` 大号琥珀频率 + VFO 徽标 → 状态行（波段 / 琥珀模式徽章 / RX·TX / Serial 点）→ **蓝色 jet 瀑布 + 青色 FFT + 红色中心标记 + 琥珀频率标尺** → 分段 S 表（S1…+40 刻度）→ 仪表 PWR·ALC / SWR·Id(青)·Vd(紫) → 五键行 → NR·NB·AN·COMP + ATU 芯片 → 音量滑条 → `◀◀ ◀ [1k] ▶ ▶▶` 步进 → VFO-A/B·A=B·SPLIT → 录音入口 → 记忆格；底部固定**红 PTT + CQ + 黄 TUNE**
- 瀑布配色改为实现 Web 的 `WF_PALETTES.jet` 分段函数，逐像素一次成图（Bitmap + 256 色查表），替代原逐行 drawRect
- 波段循环表移植 Web `DEFAULT_BAND_CYCLE`（12 波段含默认频率，未知波段→80m、末位→160m）
- 新增 JVM 测试：波段循环 4 条、jet 调色板锚点 2 条（共 52 条全绿）
- 发版：`versionCode 2`；APK 与官网下载卡同步更新
- `release.sh` 修复：同步刷新官网 hero 安卓按钮上的版本文字（旧版只改下载卡，导致按钮写着 v1.0.0 而链接已指 v1.0.1）；找不到该按钮时直接报错，不再静默放过

## [1.0.0] — 2026-10-03

- 首个公开发布：官网签名 APK（LAN + Cloud Hub 接入），显示名 MRRC Modern，应用图标，minSdk 26
- 新增：txhb 发射心跳（声明服务端活性闸门能力，AD-007 V2.63 / SDD15 §15.6）
- 新增：录音面板——启停、列表、本地播放 + 拖动进度、导出（系统分享）、删除（AD-017）
- 新增：一键 CQ 呼叫，含进度与中止（AD-020）
- 修复：连接指示灯永远灰；设置页不可达；只读登录（4003）静默失败
- 发布：`release.sh` 一键构建/签名/上传/线上 SHA-256 复核；keystore 仓库外保管
