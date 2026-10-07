# FT710Android Changelog

App 版本独立于服务端版本；全功能需服务端 ≥ v1.22（txhb 闸门），更低版本自动降级。

## [1.1.20] — 2026-10-06

- **ATR-1000（可选选件）没配置时，App 连碰都不碰它**：
  - 显示侧**本来就是对的**（已核实全链路）：`server.py` 里 `atr = None`（L162），只有 `if ATR1000_HOST:`（L2785）才建 `ATR1000Client`，否则保持 None（L2884）→ `fullState.atr1000Enabled = atr is not None`（L3645）→ App `if (atrEnabled)` 不渲染 ATR 行。配置读取还带 legacy 前缀回退（`config.py:_env`：`MRRC_*` → `FT710_*`）。
  - **连接侧有漏**：`ConnectionManager.start()` 无条件连 `/WSatr1000`。没装 ATR 的部署，服务端会 `accept` 后立刻以 **4000 "ATR1000 disabled"** 关闭 —— 每次会话白握手一次，还制造无意义的关闭事件。
  - 改为**惰性连接**：新增 `setAtrEnabled(Boolean)`（幂等、`@Synchronized`），由 VM 在 fullState 到达时调用；启用才 `connectAtr()`，禁用则关掉；`stopAll()` 清标志，新会话等 fullState 再决定（重连场景下 `start()` 只在"上次已知启用"时补连）。
  - 新增 `openedPaths`（本次会话请求打开过的通道）作为可观测点 + 测试断言面。
- **测试 +3 → 140 项全绿**：
  - `ATR channel stays closed until the server says it is enabled`：四路核心通道之外不多连；`setAtrEnabled(true)` 才出现 `/WSatr1000`；重复调用**幂等**；`stopAll` 后新会话不补连
  - `ATR tune is a no-op while the optional channel is absent`：没连 ATR 时 `sendAtrTune()` 返回 false 且不抛
  - `atr1000Enabled defaults to false when the server omits it`：fullState 缺该键 → false（否则会渲染出一条没用的 ATR 行，白占 26dp —— 紧凑档一屏已经塞满）
- **顺带修掉一个自己埋的坑**：给 `connect()` 加日志时，脚本把语句插进了 `onClosedCode` 的**默认 lambda**（`private fun connect(` 之后第一个 `{` 是默认参数的 lambda 而不是函数体），导致只有连接关闭时才记录、`openedPaths` 恒为空。**编译能过，是测试抓到的** —— 按参数默认值里带 lambda 的函数做文本插入时必须核对插入点。
- 说明：本机 `.env` 里 `FT710_ATR1000_HOST=192.168.1.63` 经 legacy 回退**是生效的**，且该设备 60001 端口实测可达 → 所以你自己这台看到 ATR 行属于正确显示。要在本机隐藏，把 `.env` 那两行注释掉再重启服务端即可（运行期配置，我没动）。

## [1.1.19] — 2026-10-06

- **5" 小屏（360×584）移出适配范围**（用户明确"不用考虑"）。适配下限写进代码：`ScreenFit.SUPPORTED_MIN_HEIGHT_DP = 728`（5.5" 直板机 + 三键导航），更小的屏滚动兜底；对应测试从"小屏超出可接受"改成"支持下限必须一屏放得下"。
- **屏幕余量不再留白，全部给频谱**：紧凑档算完后主流机型普遍还剩 25~145dp，原先只是在记忆卡和底栏之间空着。现在按 `bonus = min(可用高度 − 固定预算, 80dp)` 加到频谱显示屏高度上 —— 电台里最值钱的实时区域，瀑布行数固定 120，越高每行越清楚。

  | 机型档位 | 频谱高 | 其中余量 | 合计/可用 | 底部留白 |
  | --- | --- | --- | --- | --- |
  | 小米/红米 360×744（手势） | 139 → **164dp** | +25 | 744/744 | 0 |
  | 小米/红米 360×728（三键） | 139 → **148dp** | +9 | 728/728 | 0 |
  | 荣耀 400×832 | 139 → **219dp** | +80 | 808/832 | 24 |
  | 华为 Mate 432×880 | 139 → **219dp** | +80 | 815/880 | 65 |
  | 小米12 393×817 | 139 → **219dp** | +80 | 806/817 | 11 |
  | 平板 800×1200（标准档） | 187 → **267dp** | +80 | 1149/1200 | 51 |

  按构造安全：`bonus ≤ 可用 − 固定预算`，所以**加了余量也绝不会超出一屏**（`ScreenFitTest` 对全部档位断言）。用户在设置里调的 Spec H / WF H 仍是基准值，余量是叠加项，比例（FFT:瀑布）不变。
- 测试 **137 项全绿**（新增 2 组余量分配测试）

## [1.1.18] — 2026-10-06

### 🔴 修复：荣耀等中端机卡顿 + 声音几乎出不来（真机反馈）

三条根因，全部有代码证据：

1. **瀑布每帧重建整幅位图**（主线程 draw 阶段）：`850×120 = 102,000` 次查表 + 每帧 `IntArray(102000)` 分配（408KB）+ 整幅 `setPixels` + 缩放绘制。频谱 20~30fps → 每秒约 300 万次查表、12MB 垃圾。
   → 改**环形位图增量绘制**：位图固定 850×120，每帧只写新到的那一行（850 像素 + 一次单行 `setPixels`），再用两段 `drawImage` 展开成"最旧在上、最新在下"。**每帧成本降约 120 倍**。
   → 环形下标数学抽成纯函数 `Spectrum/WaterfallRing.kt`（`newRowCount` / `firstRingIndex` / `segments`）+ **7 组 JVM 测试**（含"整周期每格恰好写一次"、"回绕段行数之和 = filled"）。
2. **频谱流在主屏顶层订阅** → 每帧（20~30Hz）重组**整个主屏**（状态行 FlowRow、S 表弧、仪表、记忆格…）。
   → 拆出子组件 `SpectrumPanel` 自己 `collectAsState()`，每帧只重组这一块。
3. **音频线程是默认优先级**：主线程被打满时 `rx-player` 被饿死 → AudioTrack 欠载 → "声音几乎出不来"。
   → `Process.setThreadPriority(THREAD_PRIORITY_URGENT_AUDIO)`（必须在该线程内调用才生效）。

### 屏幕适配：大陆主流机型（小米/红米/华为/荣耀）一屏放得下

- 新增 `Data/ScreenFit.kt`（纯算术，**尺寸的单一数据源**）+ `UI/ScreenMetrics.kt`（CompositionLocal 下发）。按 `Configuration.screenHeightDp` 分档：**紧凑档**（≤900dp，覆盖直板机）/ **标准档**（平板、折叠屏展开）。
- 紧凑档收紧：顶栏 S 表 0.50 比例（74~116dp）、频谱按 **0.68** 压一档（仍尊重设置里的 Spec H / WF H 滑条）、仪表格 30→26、按键 34→32、芯片 28→26、音量行 →34、记忆格 **3×2 → 1×6**（46→40）、PTT 96→74、CQ/TUNE 66→58、卡片内边距 10/9→8/7、卡片间距 8→4dp、**不显示分区标题**（靠卡片分组区分）。
- 卡片内节奏统一由 `Panel(spacing=…)` 给，删掉所有 ad-hoc `padding(top=…)` —— 否则预算和真实布局会脱节。
- 关掉 M3 的 48dp 最小交互尺寸（`LocalMinimumInteractiveComponentSize provides 0.dp`），否则音量行被撑到 48dp、直接吃掉一屏预算。
- **预算可测**（`ScreenFitTest`，9 组）：用真机可用高度断言"一屏放得下"——

  | 机型档位 | 合计 | 可用 | 结果 |
  | --- | --- | --- | --- |
  | 小米/红米 360×744（手势导航） | 719dp | 744 | ✅ |
  | 小米/红米 360×728（三键导航） | 719dp | 728 | ✅ |
  | 荣耀 400×832 | 728dp | 832 | ✅ |
  | 华为 Mate 432×880 | 735dp | 880 | ✅ |
  | 小米12/Pixel 393×817 | 726dp | 817 | ✅ |
  | 412×892 | 731dp | 892 | ✅ |
  | 平板 800×1200（标准档） | 1069dp | 1200 | ✅ |
  | 小屏 5" 360×584 | 719dp | 584 | ⚠️ 超 135dp → 滚动兜底 |

- 测试 **135 项全绿**（新增 WaterfallRing 7 + ScreenFit 9）

## [1.1.17] — 2026-10-05

### 🔴 修复：v1.1.16 启动即崩溃（装上打开就退出）

- **根因**：为了把主频显示屏与 S 表做成等高，顶栏写了 `Row(Modifier.height(IntrinsicSize.Min))`，而这一行里两个子项都含 `BoxWithConstraints` —— Compose 的 `SubcomposeLayout` 家族**不支持 intrinsic 测量**，布局阶段直接抛 `IllegalStateException`：
  > Asking for intrinsic measurements of SubcomposeLayout layouts is not supported. This includes components that are built on top of SubcomposeLayout, such as lazy lists, BoxWithConstraints, TabRow, etc.
- **证据（不是猜）**：从 `~/.gradle/caches` 的 `ui-release.aar`（compose-ui 1.7.6）拆出 class，字节级搜到上面这条原文，出处 `LayoutNodeSubcompositionsState`
- **修法**：等高改用**显式高度** `Row(Modifier.height(meterH))`（`meterH` 本来就由屏宽算出），删除 `IntrinsicSize`；视觉效果不变（显示屏与 S 表仍等高）

### 新增门槛：`UiLayoutSafetyTest`（这类崩溃以后在构建阶段就拦住）

静态扫描 `src/main/java`：某个函数体里**同时**出现 `IntrinsicSize.Min/Max/Fixed` 与 SubcomposeLayout 家族（`BoxWithConstraints` / `Lazy*` / `TabRow` / `SubcomposeLayout`）即失败。已验证：把 bug 放回去 → 测试红；改回来 → 绿。

顺带修掉三个会让门槛**静默失效**的坑（都是实测踩到的）：
1. 函数体提取必须先**配对跳过参数列表** —— `onToggleFullscreen: () -> Unit = {}` 的 `{}` 会被当成整个函数体，扫描范围变成空
2. 扫描前**剥注释**（保持字符偏移）—— 否则我自己在代码里写的"不能用 IntrinsicSize"注释会被判违规（第一版就误报了）
3. `app/build.gradle.kts` 给 Test 任务声明 `inputs.dir("src/main/java")` —— 否则改源码后 Gradle 判 UP-TO-DATE **直接跳过测试**，门槛等于没有

测试 119 项全绿。

## [1.1.16] — 2026-10-05

- **状态行改成全宽单行**（真机反馈"字体小些/最好一行能装下"）：
  - 原先它挤在左列（约 187dp）里，被折成三行。现在移出左列、占满页面宽度
  - 字体降到 8.5~9sp、行高 32→26dp、项间距 7→5dp；`⛶/⏻` 改 26dp 小按钮（最小宽 30dp）
  - 内容瘦身：`Serial` 文字去掉只留状态点（点按出说明）；速率与 RTT 合并成紧凑格式 `↓128↑128`、`RTT23·J180`；发射时该段自动换成 `TX pk:`（此时 RX 本来没有码率）
  - 估宽 **269dp**，344dp 页面余量 75dp；即使叠加"只读 + 无声 + TX pk"也 ≤343dp → **一行装下**（FlowRow 仅作极窄屏兜底）
- **波段 / 模式 / VFO 移进主频显示屏上沿**（真机面板的层次：左 `15m · USB`，右 `VFO-A`）
  - 点显示屏上沿的 `VFO-A/B` 仍可切换；波段/模式的选择器仍在"控制"卡片的五键行（长按）
  - 主频显示屏与 S 表**等高**（`IntrinsicSize.Min` + `fillMaxHeight`），顶栏看起来是一台机器而不是两块拼图
- 结构 diff 命中一项（`VFO-${activeVfo}` 芯片消失）→ 已核实为有意迁移，`vm.sendSet("vfo", …)` 仍有两处调用点，能力未丢
- 测试 118 项全绿

## [1.1.15] — 2026-10-05

### 🔴 修复：v1.1.14 误删主屏控制行（严重回归）

v1.1.14 的包**缺少**：五键行（模式 / 波段 / 滤波 / ATT / PRE）、芯片行（NR / NB / AN / COMP / ATU）、音量滑条、步进（◀◀ ◀ step ▶ ▶▶）、VFO 行（VFO-A / VFO-B / A=B / SPLIT）。

- 原因：改 ATR 行时脚本替换的范围一直划到"录音入口"，中间所有控制行被一并吞掉；JVM 单测与 lint 都覆盖不到 UI 结构，所以没被拦住
- 处置：从 v1.1.13 取回整段控制行并插回原位；**如果你装了 v1.1.14，请直接升级到 v1.1.15**
- 防线：此后 UI 结构性改动一律做**结构 diff**（PadBtn/SmallChip/MeterCell 标签、`vm.*` 调用、`state.*`/`prefs.*` 字段、组件定义的集合比对），少了任何一项就不发版

### 界面美化（本轮）

- **设计令牌与表面体系**：新增 `UI/Surfaces.kt` —— 三层表面（内凹显示屏 `Inset` / 卡片 `Panel` / 按键 `Key`）+ 顶部高光 `Hairline` + 淡描边 `Stroke` + 统一圆角，颜色不再各处写死
- **主频显示屏**：内凹玻璃罩 + 琥珀辉光（`Shadow`），末两位（10Hz）淡化缩小 86%，做出真机读数的层次；字号仍按可用宽度自适应
- **频谱一体化**：瀑布 + 标尺 + VFO 红标装进同一块内凹显示屏（中间 1px 高光分隔），不再是三块散件
- **分区卡片**：仪表（PWR/ALC/SWR/Id/Vd + ATR）、控制（五键 + 芯片）、调谐（音量 + 步进 + VFO）、记忆频道 —— 四组各自装进圆角卡片，带小字距分区标题
- **按键质感**：`PadBtn` / `SmallChip` / 记忆格 / CQ / TUNE 统一为「凸起面 + 顶部高光 + 描边」，按下**缩放 0.95~0.97 + 变暗 + 涟漪**；芯片改药丸形
- **仪表条**：圆角胶囊 + 暗轨道 + 同色渐变填充（原来是方角平涂）
- **记忆格**：已存频道=琥珀微光底 + 琥珀描边，空=暗面；46dp 高、10dp 圆角
- **PTT**：常态暗红渐变凸起键 + "按住说话"副标；**发射时外发光（红晕 shadow）+ 提亮 + 白描边 + 字号微增**，松手动画 120ms 回落（`release()` 安全铁律未动）
- **底部栏**：竖向渐变面 + 顶部 1px 高光，与内容区分层
- 测试 118 项全绿

## [1.1.14] — 2026-10-05

真机反馈的一轮"瘦身 + 精修"：

- **S 表精修**：轨道改成弧形导轨（暗底 + 已走部分铺 Web 同款绿→黄→橙→红渐变）、主/次刻度长短分明、指针为红点压弧 + 白芯红线、COMP 条改圆角细条、面板底改极淡竖向渐变
- **删掉两行**：① 机型名 + 「实验性」徽章行；② 设备诊断行（A:/F:/D:… 那行）→ 都挪到 **设置页「设备 / 诊断」**（排查时照旧能取到，主屏不再有杂讯）
- **删掉频谱下方的 S 表直条与数字**（`SMeterBar` 整个删除；右上角弧形表已覆盖该信息）
- **录音入口不再独占一行**：改成状态行里的紧凑芯片（录音中显示红点 + 时长）
- **天调参数不再单独一行**：`CL/LC L=… C=…`（离线时"ATR 离线"）并进 ATR 行，成为第 3 格，ATR 行 4 格
- **字号降两号**：`☰` 19→15sp、模式徽章 11→9sp、状态行 `VFO-A/B` 芯片 11→9sp（`PadBtn` 新增 `fontSize` 参数）
- 顶栏状态行重写为干净的 FlowRow 结构；测试 118 项全绿

## [1.1.13] — 2026-10-05

- **S 表改成右上角独立区域，尺寸按屏幕自适应**（真机反馈"太小了、不要受限于频率的字高度"）：
  - 顶栏改两列：左列 = 主频 + 状态行（`FlowRow`，☰ 挪到这里）；右列整块给 S 表
  - S 表宽 = 页面宽 × 44%（夹在 140–240dp），高 = 宽 × 0.64（夹在 96–168dp）→ 手机 158×101dp、平板 240×154dp（旧版 116×34dp）
  - 弧内部全部随区域缩放：标签字号（8–12sp）、S 值读数（10.5–17sp）、刻度高、线宽、COMP 条厚，按短边比例算
  - 读数升级：左上大号 S 值 + 右上相对 S9 的 `dB`，底部 COMP 条 + 标签
  - 主频因此降为 ~32sp（344dp 宽手机）；把 ☰ 移出主频行就是为了让它吃满左列
- 测试 118 项全绿

## [1.1.12] — 2026-10-05

- **主频右侧新增 FT-710 面板样式的 S 表**（用户要求"按照这个样子"）：
  - 弧形刻度（二次贝塞尔）+ 16 条刻度线 + 8 个标签 `1 3 5 7 9 +20 +40 +60 dB`，刻度/标签沿用 Web 的 `SMeter.MARKERS/LABELS`（只是画在弧上）
  - 红针（当前位置的线与圆点）+ 左上角实时 S 值读数
  - 底部 COMP 细条（面板上那根压缩表）：发射时用 ALC 驱动
  - 几何（`SMeter.arcX/arcY/labelXPositions`）为纯函数，新增 4 组 JVM 测试（端点/对称/单调 + 逐标签不重叠——这条测试当场抓到 `+20/+40` 在 100dp 内会压字，据此把面板加宽到 116dp 并把弧区让出 `dB`）
- **顶栏重排**：第 1 行只留 `☰ · 主频（自适应） · S 表`，`⛶/⏻/VFO` 与波段/模式/统计一起进状态行，状态行改 `FlowRow`（窄屏自动折行，不再溢出）；行内小项统一 32dp 行高垂直居中
- 测试 118 项全绿

## [1.1.11] — 2026-10-05

- **修频谱下方频率标注**（真机反馈）。旧实现有三个问题，逐条对齐 Web `renderFreqScale` / `_freqStep` / `_formatFreqLabel`：
  - **用了 `scope_start_freq`**：服务端恒为 CENTER 模式，该字段调谐后滞后 → 标尺读数与瀑布/点击位置对不上。改为 **VFO ± span/2** 计算
  - **固定 6 等分刻度** → 改为自适应步进（200/500/1k/2.5k/5k/10k/25k/50k/100k，屏幕上约 8~12 个）
  - **标签位置与格式**：改为按真实频率位置绘制（Canvas），格式随步进变化（`7050.0k` / `7050k` / `7.05` / `7.050`）
- 新增 `Data/FreqScale.kt` + 5 组 JVM 测试（步进表、格式、居中范围、刻度数量与位置）
- 测试 114 项全绿

## [1.1.10] — 2026-10-05

- **继续修发射"极微弱/没功率"**（二次真机反馈）。核对过服务端 TX 链路（`_feed_tx_from_uplink` 无软件增益）与 Web TX 链路（`_txMicGainNode` 默认 1.0=unity，上限 2×）——差异来自**采集器件电平**（手机麦比 PC 耳麦低十几 dB），不是链路丢包。三项改动：
  - **采集源探测 + 自动回退**：每个候选先探 700ms，没数据就换下一个（`UNPROCESSED@48k → MIC@48k → UNPROCESSED@44.1k → MIC@44.1k`，**不含 VOICE_COMMUNICATION**）。某些机型 `UNPROCESSED` 能 `STATE_INITIALIZED` 却永远不给样本，旧实现会静默无声
  - **🎙 Vol 上限 200 → 400（4×），默认 100 → 150**：手机麦需要补偿；Web 的 unity 默认在手机上就是"极微弱"
  - **发射时状态行显示 `TX pk:N`**（本帧峰值）：按住 PTT 一眼看到调制度，现场直接调 🎙 Vol / 电台 Mic Gain
- 诊断行 TX 段补 `vol:`（当前软件增益）
- 测试 109 项全绿

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
