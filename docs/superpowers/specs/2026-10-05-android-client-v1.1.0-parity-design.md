# MRRC Modern Android 客户端 v1.1.0 — 全量对齐手机端 Web 设计文档 (Design Spec)

**日期**: 2026-10-05
**状态**: 设计已批准（用户 2026-10-05 圈定全量范围"全面搞"），待规格评审
**前置**: `FT710Android/` v1.0.1（v1.0.0/v1.0.1 已发布、官网分发；真机验收尚未完成）；对齐基准 = 手机端 Web `static/` @ `cd36dab`；服务端协议权威 `server.py` @ `62f9be7`（产品 v1.25.2）；工作区 `.worktrees/android-v1.0.0`（分支 `feat/android-1.0.0`）
**SDD 追溯**: AD-007 / SDD15 §15.6（txhb、PTT 安全——本轮不动，只保不退化）、AD-017（服务端录音）、AD-020（一键 CQ）、AD-021（诊断包——S7 入口）、SDD12 §12.9（Cloud Hub 模式——S6）、AD-016（capabilities 驱动——D0/D6/D7）、SDD V2.57（ATR1000 自动调谐事件——M3）、NFR-012 / SC8（PTT 不粘滞）、V2.63（TX 活性闸门）
**前序文档**: `docs/superpowers/specs/2026-10-03-android-client-v1.0.0-release-design.md`、`docs/superpowers/plans/2026-10-03-android-client-v1.0.0-release.md`

---

## 1. 背景

v1.0.1（2026-10-04）把界面按手机端 Web 重做完毕并发布，但用户对照手机端 Web 逐项比对后，App 仍有一批**功能、交互与机型自适应**差距；更关键的是规划期逐字核对协议时发现 **一个阻断级缺陷：App 对真实 `fullState` 的解析必然失败**（见 D0），这意味着 v1.0.1 在真机上登录后是"连上但无状态"。

本轮按用户决定「全面搞」：把 D（缺陷）/ M（主屏）/ S（设置与菜单）/ L（三个大项）四组全部补齐，App 版本升 **1.1.0**，仍走「我发布 → 用户官网下载自装 → 真机验收 → 问题回报」的通道。

## 2. 目标与非目标

### 2.1 目标

1. **D 组（缺陷）**：修复 D0（fullState 解析）与 D1–D7 与 Web 语义不一致处
2. **M 组（主屏）**：点击 QSY、频率输入、ATR1000 行、音频统计、无声告警、全屏/常亮、连接开关、记忆 label
3. **S 组（设置/菜单）**：Scope 设置面板、RF Gain/Mic Gain/Mic Vol、NR/NB 电平、波段/模式选择器、Memory Manager、Cloud Hub 向导、🐞 支持入口、机型显示、连接设置（浏览器）
4. **L 组（大项）**：后台 RX（前台服务）、44.1 kHz↔48 kHz 重采样、录音 seek 与合计
5. 纯逻辑全部带 JVM 单测；门槛 `test + assembleDebug + lintDebug + assembleRelease` 全绿
6. 发布 v1.1.0：签名 APK + 官网（en/zh）下载卡 + CHANGELOG/README/CLAUDE/BUILD_GUIDE + UI mockup

### 2.2 非目标

- 不改服务端（`server.py`/`backends/`）与 Web（`static/`）；协议以现有实现为唯一权威
- 不改 iOS（`FT710Mobile/`）
- 不上 Play 商店；不做应用内自动升级；不做混淆
- **不原生重做服务端管理页**（机型/串口/音频设备/改密码）：S9 只提供「浏览器打开」
- 不重做 `release.sh`（v1.0.0 建立的发布链路直接复用）
- PTT 安全铁律与 txhb 行为**零改动**（只做不退化回归）

## 3. 缺陷与差距清单（本轮范围基准）

| 编号 | 项目 | 现状 | 目标（对齐 Web） |
| --- | --- | --- | --- |
| **D0** | **fullState 解析阻断** | `FullStateDto.bands: List<String>` 与服务端 `list[dict]` 不符；`FilterTables.voice: List<Int>` 与服务端 `list[(idx,hz)]` 不符 → 每次 fullState 解码异常 → `WsEvent.Unknown`，App 永远无状态 | 按真实形状重构 DTO + **真实形状 fixture** 单测（服务端仍逐字核对） |
| D1 | Scope 量程值错 | 设置页「50k/100k/1M」发 `scope_span`=0/1/2（服务端 0/1/2 = 1k/2k/5k）；主屏 `spanHz` 换算表也错 | 量程表由 `capabilities.scope_spans` 构建（`civ27` 半幅×2），fallback FT-710 3..9；主屏标尺/QSY 共用同一表 |
| D2 | 🔊 Vol 语义 + 缺 RX 增益 | App 调电台 `af_gain`；无 `audio_gain_boost`（FT-710 需 10×）、TX 期间不静音 | 本机播放音量（0..255，持久化）；增益 = `min(10, vol/255 × boost)`；TX/TUNE 期间 RX 播放静音（防自噪回声） |
| D3 | RF PWR 拖动风暴 | 设置页每拖一格都发 `rf_power` | 松手（`onValueChangeFinished`）才发一次 |
| D4 | 记忆清除无入口 | `clearMemory()` 已写、UI 未接 | S5 Memory Manager 提供清除 |
| D5 | TUNE 状态显示 | `tx_status==2` 显示 TX | 显示 **TUNE**（`1`→TX，`0`→RX） |
| D6 | filter/ATT/PRE 循环硬编码 | `%23 / %4 / %3` | filter：voice `[9,13,17,20,23]` / narrow `[3,6,10,13,17,21]` / `fil123` 1→2→3；ATT/PRE 用 `capabilities.att_steps/preamp_steps` 长度；标签按 `filterTables` |
| D7 | capabilities 未消费 | `radioDisplayName`/`capabilities` 丢弃 | 能力驱动布局（隐藏无 ATU/AN/Vd-Id 的控件、未验证机型"实验性"徽章）+ 机型名显示 |
| M1 | 点击 QSY | 无 | 点瀑布/FFT 任意位置调频（`f = vfo − span/2 + x/w·span`，clamp 30k..75M；A→`freq`，B→`vfo_b_freq`；位移 >8px 视为滚动忽略） |
| M2 | 频率输入 | 无 | 点频率数字弹出 MHz 输入（解析规则同 Web：无小数点且 <100000 视 kHz；<1000 视 MHz；clamp） |
| M3 | ATR1000 行 | WS 连了但事件丢弃（`onAtrEvent={}`） | PWR/SWR/L-C 三表 + TUNE；`atrState`/`atrTune`/`atrTuneResult`（含 auto 阶段文案，Snackbar 提示） |
| M4 | 音频统计 | 无 | 状态行 `↓xK ↑xK`（每秒字节计数）+ `RTT xxms Jxxms`（ping/pong + 抖动缓冲） |
| M5 | 无声告警 | `rx_audio_silent` 已解析未显示 | 状态行红色「无声」；点按给出解释（Snackbar） |
| M6 | 全屏/常亮 | 恒常亮、无全屏 | 顶栏 ⛶ 切换沉浸式；设置页「保持屏幕常亮」开关（默认开） |
| M7 | 连接开关 | 无 | 顶栏 ⏻ 断开/重连全部通道（关闭态显示"已断开"遮罩 + 一键重连） |
| M8 | 记忆 label | 固定 `M1..M6` | 显示频道 label（Web title 语义） |
| S1 | Scope 设置面板 | 只有 3 档 span（还是错的） | SPAN 全量程 + SPD + 6 配色 + Floor/Ceil + Spec H/WF H（本地偏好） |
| S2 | 三个滑条 | 无 | RF Gain（0..100% ↔ 0..255）、Mic Gain（0..100，本地持久化并在每次 fullState 差异回推）、Mic Vol（0..200 本机软件增益） |
| S3 | NR/NB 电平 | 无 | NR 1..15、NB 0..10，松手提交 |
| S4 | 波段/模式选择器 | 只有循环按钮 | 弹窗网格选择（数据来自 `fullState.bands/modes`） |
| S5 | Memory Manager | 无 | 6 行列表（频率/标签）+ 清除（写回 `memSave`） |
| S6 | Cloud Hub 向导 | 无 | `GET /api/cloud/state` → 呼号/密钥 `apply` → 轮询 `refresh` → `restart`（`cert_reload_required` 流程同 Web） |
| S7 | 🐞 支持入口 | 无 | 浏览器打开 `<baseUrl>/support.html` |
| S8 | 机型/版本显示 | 只显示客户端版本 | 设置页显示 `radioDisplayName` + 客户端版本；未验证机型徽章（D7） |
| S9 | 连接设置 | 无 | 菜单项「连接设置（浏览器）」打开服务端首页 |
| L1 | 后台 RX | 退后台即停（v1 决策） | 前台服务 + 常驻通知，后台继续接收；TX 仍在 `onStop` 无条件释放；设置页「后台接收」开关（默认开） |
| L2 | 44.1k 重采样 | TX 只按 48k 打开，设备只给 44.1k 时失败 | TX 采集 44.1k→48k（882↔960 线性插值）；设备侧兜底 |
| L3 | 录音 seek/合计 | 只有播放/暂停/导出/长按删 | 进度条拖动 seek + 列表合计（`count`/`total_bytes`） |

## 4. 协议事实（实现依据，规划期逐字核对）

### 4.1 `fullState`（`server.py:_full_state_message` @ 3540）

键：`type/data/bands/modes/recording/cq/radioModel/radioDisplayName/capabilities/memChannels/filterTables/atr1000Enabled`。

- `bands`: **`list[dict]`**，每项 `{name, start, end, bsr, default_freq}`（`backends/ft710/config_ft710.py:BANDS`；Icom/Yaesu profile 同形）。当前 App 按字符串列表解析 → **必失败（D0）**
- `modes`: `list[str]`（`ui_modes`）
- `filterTables`: `{voice: [[idx,hz],…], narrow: [[idx,hz],…], narrowModes: [str,…]}`；Icom 额外 `model:"fil123"`、`filDefaults:{mode:[hz,hz,hz]}`。当前 App 按 `List<Int>` 解析 → **必失败（D0）**
- `memChannels`: `list[dict|null]`（`JsonElement?`，现状正确）
- `recording`: `{recording,freq_hz,started_at,duration,name,bytes,dropped}`（recorder.py:453）
- `cq`: `{state,duration_s,elapsed_s,frames_total,frames_sent,started_by,reason,ready}`（cq_player.py:223）
- `radioModel`/`radioDisplayName`: str；`atr1000Enabled`: bool

### 4.2 `capabilities`（`backends/base.py:RadioCapabilities.to_dict`）

本轮消费字段：`model_name`, `display_name`, `verified`, `tx_gated`, `has_atu`, `has_auto_notch`, `has_vd_id_meters`, `vfo_b_direct`, `filter_model`（`"width_table"|"fil123"`）, `att_steps`（dB 数组）, `preamp_steps`, `scope_type`（`ft4222|civ27|none`）, `scope_spans`（`{idx:{name,freq}}`）, `scope_speeds`（str 数组）, `audio_gain_boost`（FT-710=10.0，其余=1.0）。后端缺失（老服务端）时全部按 FT-710 fallback，行为与现状一致。

### 4.3 频率/量程/QSY

- 量程表：`capabilities.scope_spans` 的 `freq`；`scope_type=="civ27"` 时为半幅 → **×2**（Web `_rebuildSpanTable`）。fallback `SCOPE_SPANS`（1k/2k/5k/10k/20k/50k/100k/200k/500k/1M）
- 服务器恒为 CENTER 模式（EX040200）：`f = vfoFreq − spanHz/2 + (x/width)·spanHz`，clamp 30 000..75 000 000；A 发 `freq`、B 发 `vfo_b_freq`
- 频率输入解析（Web `commitFreq`）：`parseFloat`；无数点且 <100000 → kHz；<1000 → MHz；clamp 同上

### 4.4 filter / ATT / PRE 循环（Web `getNextFilter`）

- `filter_model=="width_table"`：voice `[9,13,17,20,23]`，narrow `[3,6,10,13,17,21]`（narrow 模式集合由 `filterTables.narrowModes` 判定）；标签查 `filterTables` 对应表，4000 Hz ⇒ "无"
- `filter_model=="fil123"`：FIL1→2→3→1；标签 `FIL{n} {filDefaults[mode][n-1]}`（k/Hz 格式）
- ATT/PRE：`(current+1) % n`，n = `att_steps.length` / `preamp_steps.length`（fallback 4 / 3）；标签继续用服务端 `attenuator_label`/`preamp_label`

### 4.5 ATR1000（`/WSatr1000`，`atr1000_client.py:read_state` / `server.py:3892`）

- 下行 `{"type":"atrState", connected, power, swr, sw, ind, cap, ind_uh, cap_pf, tuning, tx, freq, last_update}`
- 上行 `{"type":"atrTune"}`（服务端自带 TX2 载波 → 调谐 → 比对 → 无改善回滚）
- 下行 `{"type":"atrTuneResult", phase, swr_before, swr_after, message, auto?}`；阶段：`start|skipped|success|rollback|error`；auto：`auto_start|auto_success|auto_no_improve|auto_timeout|auto_aborted|auto_giveup`
- 中文文案逐条对齐 `static/modules/atr1000.js:tuneResultText`；进行中 TUNE 显示 `···` 并禁点

### 4.6 Cloud Hub REST（`server.py:4317+`，认证 = 登录 Cookie）

- `GET /api/cloud/state` → `{connected,callsign,has_token,label,entry,cert,portal,autoconnect:{status,at,error},tunnel_running,tunnel_error,cert_reload_required}`
- `POST /api/cloud/apply` `{callsign,contact,secret}` → `{submitted,status,callsign}` 或连接结果；错误 `{error}`（400/502）
- `POST /api/cloud/refresh` → `{connected,status}` / 连接结果 / `{error}`
- `POST /api/cloud/restart` → 重启进程（随后连接断开，需重连）
- 交互流程与 `static/modules/cloud_hub.js` 一致：未申请→表单；已申请未接入→轮询（20 s）或粘贴登记口令 claim；已接入→显示入口/证书/隧道状态；`cert_reload_required` → 显示"重启以启用新证书"按钮；restart 后提示并延时自动重连

### 4.7 支持页

`static/support.html` 由服务端自身提供（`server.py` 静态目录）；菜单入口打开 `<baseUrl>/support.html`（系统浏览器，上传的包才来自当前实例）。

### 4.8 音频

- RX 帧：`1B tag (0x00 PCM Int16 LE / 0x01 Opus) + payload`，恒 48 kHz 单声道 20 ms；播放增益 = `min(10, vol/255 × audio_gain_boost)`，TX/TUNE 时 `×0`（Web `AUDIO_TX_DIM_FACTOR=0`）
- TX：Opus CBR 64 kbps；采集 48 kHz 单声道 960 样本/20 ms；设备只给 44.1 kHz 时按 882↔960 线性插值（`audio_resample.py` 同算法），Mic Vol 在编码前按 `v/100` 缩放
- 统计：`↓/↑ kbps` = 5 条 WS 每秒收发字节 ×8/1000；RTT = ping 发出到 pong 收到；J = `jitter.size × 20 ms`

### 4.9 txhb / PTT（不变式，只回归不修改）

按键即发 `{"type":"txhb"}`、每 500 ms 一次；`release()` 无条件发 `ptt:false`；手势 `finally`；`onStop` → `forceRelease()`；看门狗 500 ms×3。后台 RX（L1）**不改变**这套释放行为。

## 5. 模块设计

### 5.1 网络/数据层

| 文件 | 动作 | 职责 |
| --- | --- | --- |
| `Network/Protocol.kt` | 改 | `BandDto{name,start,end,bsr,default_freq}`、`FilterTables`（`List<Pair<Int,Int>>` 语义：`List<List<Int>>` + 容错解析）、`CapabilitiesDto`、`AtrStateDto`、`AtrTuneResultDto`、Cloud DTO、`FullStateDto` 补 `radioDisplayName/capabilities`；**真实形状 fixture 测试** |
| `Network/ConnectionManager.kt` | 改 | ATR 事件路由与 `sendAtrTune()`；ping 时间戳→RTT；每秒字节计数（5 通道）；`disconnect()/reconnect()`（M7）；不改 WS 重连策略 |
| `Network/CloudApi.kt` | 新增 | 4 个 cloud 端点的 OkHttp 实现 + 纯解析函数（JVM 可测） |
| `Network/RecordingsApi.kt` | 改 | `list()` 返回 `RecordingsListDto`（含 `count/total_bytes`）供 L3 合计 |
| `Data/RadioState.kt` | 改 | 无新字段；`apply` 已够用（`rx_audio_silent`/`tx_status` 已在） |
| `Data/SettingsStore.kt` | 改 | 本地偏好扩展（见 §5.5） |
| `Data/FreqInput.kt` | 新增 | Web `commitFreq` 解析规则（纯函数，单测） |
| `Data/Capabilities.kt` | 新增 | 量程表构建（civ27 ×2）、filter 循环、ATT/PRE 计数（纯函数，单测） |

### 5.2 音频层

| 文件 | 动作 | 职责 |
| --- | --- | --- |
| `Audio/Resampler.kt` | 新增 | 纯 Kotlin 线性插值 `resamplePcm(ShortArray, inRate, outRate)`；882↔960 帧对齐；JVM 单测锚定（含边界：空、单样本、奇数长度） |
| `Audio/RxAudioPlayer.kt` | 改 | 增益（vol×boost，TX 静音，静音平滑/直接置零）；`bufferMs` 暴露；`setVolume(0..255)` / `setBoost(f)` / `setTransmitting(b)` |
| `Audio/TxAudioCapture.kt` | 改 | 采集率探测（48k 优先，失败/系统改率则 44.1k + 重采样到 48k）；Mic Vol 缩放；保持 20 ms 960 样本输出 |

### 5.3 平台层

| 文件 | 动作 | 职责 |
| --- | --- | --- |
| `App/RxForegroundService.kt` | 新增 | 前台服务（`mediaPlayback` 类型）：常驻通知「MRRC Modern · 正在接收」，点击回 App，动作「断开」；连接建立且「后台接收」开时启动，断开/关闭开关时停止 |
| `App/MainActivity.kt` | 改 | Android 13+ `POST_NOTIFICATIONS` 运行时申请（登录成功后一次）；沉浸式全屏切换入口；把 `onStop` 的 TX 释放保持 |
| `App/AppSetup.kt` | 改 | keepScreenOn 由偏好驱动（默认开）；全屏状态不持久化 |
| `AndroidManifest.xml` | 改 | `FOREGROUND_SERVICE`、`FOREGROUND_SERVICE_MEDIA_PLAYBACK`、`POST_NOTIFICATIONS`、`<service>` 声明 |

### 5.4 UI 层

| 文件 | 动作 | 职责 |
| --- | --- | --- |
| `UI/MainScreen.kt` | 改 | M1/M2/M3/M4/M5/M6/M7/M8；顶栏加 ⛶/⏻；状态行加统计与无声；仪表区加 ATR 行；断开遮罩 |
| `UI/SettingsScreen.kt` | 改 | S1（Scope 面板）/S2/S3/S8；常亮/后台接收开关；重连/退出保留 |
| `UI/CloudHubDialog.kt` | 新增 | S6 向导（状态机：未申请/已申请/已接入/需重启） |
| `UI/Dialogs.kt` | 新增 | S4 波段/模式选择器、M2 频率输入、S5 Memory Manager（复用 Web 弹窗语义） |
| `Spectrum/Palettes.kt` | 新增 | 6 套配色纯函数（逐段对齐 `WF_PALETTES`）+ 256 级 LUT（floor/ceil 映射）；JVM 单测锚点 |
| `Spectrum/WaterfallCanvas.kt` | 改 | 配色/floor/ceil 生效；FFT 区（Spec H）与瀑布区（WF H）分区渲染；点击回调 `onQsy(fraction)` |
| `UI/RecordingPanel.kt` | 改 | L3 进度条 seek + 合计行 |
| `UI/Format.kt` | 改 | 补格式化（kbps、时长已有） |

### 5.5 本地偏好（SettingsStore 扩展，DataStore）

| key | 类型/范围 | 默认 | 语义 |
| --- | --- | --- | --- |
| `afVol` | int 0..255 | 128 | 本机播放音量（Web `ft710_afVol` 同义） |
| `micVol` | int 0..200 | 100 | 本机麦克风软件增益（Web `ft710_micVol` 同义） |
| `micGain` | int 0..100 | 未设 | 电台 mic_gain 持久化；每次 fullState 差异时回推（Web `_applySavedMicGain`） |
| `scopeTheme` | string | `jet` | 瀑布配色：jet/hot/cold/thermal/night/gray |
| `scopeFloor` | int 0..200 | 5 | 瀑布地板（Web 同默认） |
| `scopeCeil` | int 50..255 | 220 | 瀑布天花板 |
| `fftHeight` | int 20..120 | 40 | FFT 区高度（dp 语义，保当前观感） |
| `wfHeight` | int 30..200 | 110 | 瀑布区高度（dp 语义，保当前观感） |
| `keepScreenOn` | bool | true | 屏幕常亮 |
| `backgroundRx` | bool | true | 后台接收（前台服务） |

> FFT/瀑布高度默认取 40/110（合计 150 dp，与当前单画布观感一致）；滑条值域照 Web（20..120 / 30..200），不照搬 Web 默认像素（22/45），避免 App 频谱区突然缩水。

## 6. 测试与验收

### 6.1 JVM 单测（新增/加强）

- **D0**：真实形状 `fullState` fixture（`bands` 对象数组 + `filterTables` 数对 + capabilities + recording/cq）解析成功；旧 fixture 回归
- 量程表：FT-710 3..9、Icom `civ27` ×2、capabilities 缺失 fallback
- filter/ATT/PRE 循环：voice/narrow 轮转、fil123 轮转、capabilities 计数；频率输入解析（MHz/kHz/Hz/非法/clamp）
- QSY 公式（含 B VFO）；ATR/Cloud/Recordings DTO 与错误分支解析；palette 锚点（6 套各 2 点 + floor/ceil 映射）；resampler（882↔960 长度与插值锚点）；增益公式（vol/boost/TX 静音）
- PTT 回归（既有 52 项必须全绿）

### 6.2 门槛

```bash
cd FT710Android
export JAVA_HOME=$(/usr/libexec/java_home -v 17); export ANDROID_HOME="$HOME/Library/Android/sdk"
./gradlew test assembleDebug lintDebug assembleRelease
```

### 6.3 真机验收清单（用户执行，我待命修）

| # | 项目 | 要点 |
| --- | --- | --- |
| 1 | 下载/安装 | 官网 v1.1.0 文件名/大小/SHA-256；允许未知来源；图标/名称 |
| 2 | 登录 | LAN 自签 + Hub 子域 |
| 3 | **状态同步** | 登录后频率/波段/模式/记忆立即出现（D0 验证） |
| 4 | 控制 | 步进/模式/波段（含弹窗选择）/滤波/ATT/PRE/DSP/记忆存取与清除 |
| 5 | QSY/输入 | 点瀑布任意位置调频；点频率输入 MHz 直改 |
| 6 | RX/TX | 出声（音量正确、TX 时静音）；PTT 说话/松手回 RX；退后台立即回 RX 且 TX 释放 |
| 7 | 后台 RX | 退后台仍能听到 RX；通知常驻、可断开；关闭开关后无后台音频 |
| 8 | 频谱 | 瀑布/FFT/配色切换/Floor-Ceil/高度/量程/SPD；点击 QSY |
| 9 | ATR | 天调行数据 + TUNE（含自动调谐 toast），无天调时整行隐藏 |
| 10 | 录音 | 启停/列表合计/播放 seek/导出/删除 |
| 11 | CQ | 进度与中止 |
| 12 | Cloud Hub | 向导三步（apply/等批准/restart）与 `cert_reload_required` |
| 13 | 只读登录 | 4003 降级提示（回归） |
| 14 | 能力适配 | 非 FT-710 机型（如有）不显示 ATU/Vd/Id、实验性徽章 |

## 7. 发布

- `versionName 1.1.0` / `versionCode 3`（覆盖升级：keystore 与 v1.0.0/1.0.1 相同）
- 产物：`MRRC-Modern-v1.1.0-Android.apk` + 稳定别名；`release.sh` 一条命令（构建→签名核对→上传→线上 SHA-256 复核→更新 en/zh 下载块与 hero 按钮）
- 文档：`FT710Android/CHANGELOG.md`（1.1.0 条目）、`README.md` 下载区、`FT710Android/CLAUDE.md`（新协议事实与偏好表）、`BUILD_GUIDE.md`（如版本引用）、UI mockup 更新（ATR 行/统计/新设置页）
- 不动 `downloads/latest.json`（Windows 升级通道）
- 站点仅改 `<!-- android-download:start/end -->` 标记块与 hero 安卓按钮（`release.sh` 的既有护栏）

## 8. 风险

| # | 风险 | 影响 | 缓解 |
| --- | --- | --- | --- |
| R1 | L1 前台服务被系统策略拒绝（FGS 后台启动限制 / Doze） | 后台 RX 不工作 | 连接建立即启动（前台用户操作触发，合规）；失败时降级为仅前台并在通知/设置页说明；回退路径 = 关闭开关，其余功能不受影响 |
| R2 | 44.1k 重采样引入 TX 音质/延迟变化 | 发射可听质量 | 线性插值（服务端同款）JVM 锚点测试；48k 可用时零改动 |
| R3 | fullState DTO 重构牵动既有解析路径 | 回归 | 真实 fixture + 原有 52 项测试双门禁 |
| R4 | 范围大（D0–L3 共 28 项）实施期拉长 | 交付节奏 | 按 §9 顺序小组提交，每组门槛全绿；任何一项阻塞可单独推迟并记 CHANGELOG |
| R5 | keystore/签名链路已就绪但真机从未验收 | 首装问题 | 发布后用户验收清单（§6.3），1.0.x/1.1.x 迭代通道已备 |

## 9. 实施顺序（供实施计划展开）

1. 基线证明（现有 52 项测试 + `assembleDebug` 全绿）
2. **D0** 协议 DTO 重构（真实 fixture）+ capabilities/量程/filter/频率输入纯函数（含单测）
3. D1/D5/D6/D7 接入 UI（量程选择、TUNE 状态、循环、能力布局/徽章）
4. M1/M2 QSY + 频率输入
5. M3 ATR 行 + 协议路由
6. M4/M5 音频统计 + 无声告警
7. D2/D3/M6/M7/M8 Vol/增益/RF PWR 提交/全屏/常亮/连接开关/记忆 label
8. S1 Scope 设置 + 6 配色
9. S2/S3 滑条 + mic gain 回推
10. S4/S5/D4 选择器 + Memory Manager
11. L2 重采样 + mic vol
12. L1 前台服务后台 RX + 通知权限
13. S6 Cloud Hub 向导
14. S7/S8/S9 支持入口/机型显示/连接设置
15. L3 录音 seek + 合计
16. 文档/CHANGELOG/README/CLAUDE/mockup
17. 发布 v1.1.0 + 线上复核 + 交付验收清单

## 10. 追溯表

| 设计项 | 依据 |
| --- | --- |
| D0 fullState 形状 | `server.py:_full_state_message` @3540、`backends/ft710/config_ft710.py:BANDS/FILTER_WIDTHS_*` |
| D1/D6/D7 capabilities | `backends/base.py:RadioCapabilities.to_dict`、`static/ft710_ui.js:_rebuildSpanTable/_rebuildSpanSelect/getNextFilter/applyRadioCapabilities/applyCapabilityBadges` |
| D2/M4 音频增益与统计 | `static/ft710_main.js:AUDIO_GAIN_BOOST/AUDIO_TX_DIM_FACTOR/_applyAfGainToAudioNode`、带宽统计段 |
| M1/M2 | `static/ft710_ui.js:wireScopeQSY`、`commitFreq` |
| M3 ATR | `static/modules/atr1000.js`、`atr1000_client.py:read_state`、`server.py:3892`、SDD V2.57 |
| S6 Cloud | `static/modules/cloud_hub.js`、`server.py:4317+`、SDD12 §12.9 |
| S7 支持 | AD-021、`static/support.html`、`server.py:SUPPORT_URL` |
| L1/L2 | AD-011（48k 域 + 设备率桥）、`audio_resample.py`、Android FGS 平台约束 |
| L3 | AD-017、`recorder.py:list_recordings`、Web 录音面板 |
| PTT/txhb 不变 | AD-007、SDD15 §15.6、NFR-012、SC8 |
