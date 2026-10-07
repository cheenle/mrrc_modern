# 频谱带宽档位（spectrum profile）设计

日期：2026-10-07
范围：`/WSspectrum` 单条链路。不动音频、不动控制面协议语义。
状态：设计已确认，待实现计划。

## 1. 目标与边界

**目标**：把频谱带宽做成可选档位，**每降一级，两种口径都真的少一半**，最终低档相对今天 −87.5%。

**为什么值得单独做**：频谱是单会话里最大的一块（实测占下行 72%，SDD/hub 的旧假设里占 86%），而其中一半字节（wf2）没有任何客户端渲染。

**非目标**（明确不做，避免顺手扩面）：

- 不做音频码率档位。`opus_rx.py:68` 与 `docs/IOS_OPUS_INTEGRATION.md:64` 声称存在 `setOpusBitrate` 运行时命令，**全仓 grep 证实它不存在**（无服务端分支、无三端调用点）。本设计不实现它，只把这条说谎的注释列进文档同步清单。
- 不做服务端自适应升降档（需要 RTT/积压闭环，验收面太大）。
- 不做 850→425 bin 降采样（Web 侧几乎不省，却要三端渲染都改）。
- 不改控制面 `fullState` 的投递策略。

## 2. 基线事实（全部实测，非估算）

### 2.1 帧格式与名义速率

```
线上 1 帧 = 1 B 版本(0x01) + 850 B wf1 + 850 B wf2 = 1701 B   scope_handler.py:434-437
广播名义 30 Hz                                              server.py:1464
```

`_broadcast_spectrum_loop` 的 docstring 写 "Runs at 5 fps (200ms interval)"（`server.py:1517`）——**陈旧**，实际 `interval = 1/30`。

### 2.2 真帧路径实测只有 ~11 fps

打包版 macOS 实例日志（`~/Library/Application Support/MRRC-Modern/logs/server.log`）：

```
Spectrum broadcast active: real scope, 1701 bytes/frame, 1 clients
Session metrics: uplink spectrum 150.8–151.0 kbps
```

151 kbps ÷ 1701 B ÷ 8 = **11.1 fps**。原因：真帧只在 `_new_real_spectrum_frame()` 看到硬件 `_frame_count` 前进时才发（`server.py:1543`），FT4222 的 scope 就是 ~11 fps。名义 30 Hz 不等于线上 30 帧。

### 2.3 合成回退路径反而贵 2.7 倍

`scope.connected == False` 时走 S-meter 合成（`server.py:1551-1554`），该分支**没有帧计数闸门**，每个 30 Hz tick 都产一帧 ⇒ 操作员（÷1）拿满速 **1701×30 = 51 KB/s ≈ 408 kbps**。即 scope_pipe 挂掉 / 无 CI-V 波形的机型，带宽暴涨。这正是 hub 侧 `AD-H14`／`NFR-H007` 那个"频谱 ≈408 kbps、单会话 0.48 Mbps"数字的来源——它描述的是**退化态**，不是常态。

### 2.4 两个口径：payload ≠ 线上字节

裸 socket 复刻浏览器握手打到同版本 uvicorn（0.52.1，`server.py` 未显式配置 `ws_per_message_deflate`，走默认 True）：

```
HTTP/1.1 101 Switching Protocols
Sec-WebSocket-Extensions: permessage-deflate; server_max_window_bits=12; client_max_window_bits=12
30 帧 × 1701 B = 51030 B 载荷  →  线上实发 ≈ 13224 B（每帧均摊 441 B）
其中 wf2 那 850 个零：整 30 帧合计 41 B（≈1.4 B/帧）
```

- **浏览器**会主动 offer `permessage-deflate` ⇒ 整帧压到 441 B（3.9×；其中 wf1 自己只压到约 1.9×，另外的收益全来自 wf2 那 850 个零），**wf2 在线上本就近乎免费**。
- **安卓 OkHttp 4.12 不 offer 该扩展**（APK dex 里 `permessage-deflate` 字符串属于 `Request header not permitted: 'Sec-WebSocket-Extensions'` 这条拒绝常量）⇒ 安卓 **payload == 线上字节**。
- `metrics.add_bytes("spectrum", len(binary))`（`server.py:1566`）记的是 **payload**，且 `session_metrics.py:40` 的 `METERED_KINDS = ("spectrum", "audio_rx")` 不含控制面与 TX 上行。

### 2.5 wf2 是死数据

| 客户端 | 对 wf2 的处置 |
|---|---|
| Web 主页 | `ft710_main.js:188-190` 只在 `length >= 1701` 时取 wf2，塞进 `window._lastWf2`，注释 "for potential future use"，不渲染 |
| Web 收听页 | `listen.js:359-360` 只画 `subarray(1, 851)` |
| iOS | `SpectrumProcessor.swift:58` 注释 "Use wf1 (bytes 1..851)" |
| Android | `SpectrumFrame.kt` 解析出 `wf2` 后全仓无引用 |
| IC-7300 家族 | `civ_scope.py:215` 直接 `spectrum_rx2 = [0] * WF_SIZE`（单收电台） |

### 2.6 基线汇总

| 端 | payload 口径 | 线上口径 |
|---|---|---|
| 操作员 · 真频谱 ·· Web | 151 kbps | **≈39 kbps** |
| 操作员 · 真频谱 ·· 安卓 | 151 kbps | **151 kbps** |
| 操作员 · 合成回退 ·· Web | 408 kbps | ≈105 kbps |
| 操作员 · 合成回退 ·· 安卓 | 408 kbps | 408 kbps |
| 收听 · 真频谱（÷3，V2.59 起） | ≈50 kbps | ≈13 kbps |

## 3. 设计：两个正交因子 + 预设档位

### D-1：档位 = (帧形状, 帧率分频) 的组合，不新增协议字段

两个因子各自独立可解释，档位只是预设组合：

- **形状 shape**：`full`（1701 B，含 wf2）｜`wf1`（851 B，砍 wf2）
- **分频 div**：对 `_broadcast_tick` 取模，`÷1｜÷2｜÷3｜÷4`

预设（`listen` 是内部预设，不在 UI 出现，用途见 D-4）：

| 档位 | shape | div | payload | 安卓线上 | Web 线上 |
|---|---|---|---|---|---|
| `high`（操作员默认） | full | ÷1 | 151 kbps | 151 kbps | 39.2 kbps |
| `mid` | wf1 | ÷2 | **37.8 kbps (−75%)** | **37.8 kbps (−75%)** | **19.5 kbps (−50%)** |
| `low` | wf1 | ÷4 | 18.9 kbps (−87.5%) | 18.9 kbps | 9.8 kbps |
| `listen`（收听默认） | full | ÷3 | 50.3 kbps | 50.3 kbps | 13.1 kbps |

合成回退态按同因子缩放：`high` 408｜`mid` 102｜`low` 51｜`listen` 136 kbps payload。

为什么不能只做"砍 wf2"：对 Web 它只值 1.4 B/帧（§2.4），每一半都必须来自分频。为什么不做纯分频：对安卓 `high→full ÷2` 是 −50% 但白送着 wf2 的复制/压缩成本。**两个因子捆绑，才让每级对两端都 ≥ 一半。**

### D-2：短帧用**已文档化却从未上线的 v1**，不引入新版本号

`SDD/09-architecture-overview.md:47-48` 早已定死两种格式：

```
v1: 1B 版本(0x01) + 850B wf1                    = 851 字节
v2: 1B 版本(0x02) + 850B wf1 + 850B wf2          = 1701 字节
```

但线上**只存在过 `0x01`**：`get_spectrum_binary()` 硬编码 `struct.pack('B', 1) + wf1 + wf2`（`scope_handler.py:437`），全仓 grep 无任何发出 `0x02` 的代码，`git log -S "struct.pack('B', 2)" -- scope_handler.py` 零命中。**今天的线上帧是"v1 的版本字节 + v2 的长度"的错配体，本设计是去补齐文档里的 v1，不是发明新格式。**

由此得两条硬结论：

1. **短帧就是 `[0x01] + wf1`（851 B），零新协议、零新常量**，内部格式那边 version 1 本来也表示"wf2 可缺、缺则补零"（`scope_frame.py:162`），语义自洽。
2. **绝不能把全帧改标 `0x02`** —— 哪怕那更贴合文档。iOS `SpectrumProcessor.swift:57` 是 `guard version == 0x01 else { return }`，一旦全帧变 0x02，**已装 iOS 客户端直接静默丢帧**。这条是把文档对齐现实时最容易踩的反向陷阱，写在这里挡后来人。

另注意命名空间不要混：`0x01` 是**线上**版本，`PIPE_PAYLOAD_VERSION = 2`（`scope_frame.py:16`）是**内部管道**版本。

### D-3：兼容闸门 = "没声明能力就不改一个字节"

已发布客户端对 851 B / `0x01` 短帧的实际反应：

| 客户端 | 闸门代码 | 收短帧 |
|---|---|---|
| Web 主页 | `ft710_main.js:174` `length < 851` 才拒 | ✅ 照常渲染 |
| Web 收听页 | `listen.js:356` 同上 | ✅ 照常渲染 |
| iOS | `SpectrumProcessor.swift:52` `count >= binCount + 1` | ✅ 照常渲染，**无需出包** |
| Android | `SpectrumFrame.kt:14-15` 同时卡 `frame.size != 1701` **和** `frame[0] != 0x01` | ❌ **静默丢帧**（频谱不动、不报错）——且它是唯一违反已文档化 v1 的客户端 |

⇒ 规则一条：**服务端在收到该 socket 的能力声明之前，一律 `high`（1701 B ÷1）**。老安卓逐字节等于今天；Web/iOS 只要服务端发版就自动进入可降档状态。

### D-4：收听角色保持现状，不做回归性变动

今天收听是 `LISTEN_SPECTRUM_DIVIDER = 3`（`server.py:152`，V2.59 应操作员要求加的）。本设计把它**折进同一套因子模型**（预设 `listen` = full ÷3），而不是用新档位覆盖：既消掉"两套闸"，又不让收听侧的既有约定漂移。`_spectrum_frame_due()` 与 `_listen_spectrum_clients` 的现有测试（`tests/test_listen_only.py:397`）据此重钉。

### D-5：能力声明走 `/WSspectrum` 自身的文本帧

```
客户端 accept 后立即发：
{"type":"spectrumCaps","profile":"mid"}
```

- 该端点现有循环就是 `await ws.receive_text()` 然后丢弃（`server.py:3789`），加解析是零结构改动。
- **profile 名走白名单**，不接受客户端自带 div/shape 数字——避免把服务端速率变成客户端可任意施加的面。
- 记在 **per-socket** 而非 per-token：令牌是 30 天 Cookie，一个会话可同时握着多条半开 `/WSspectrum`（本仓 `_register_tx_socket` 那条教训就是"按 socket 判定"）。重连后客户端重发一次即可。

## 4. 服务端改动形态

扇出循环每 tick **最多预编码 2 个变体**（`full` / `wf1`），按 socket 的档选变体 + 选是否到帧；不做每客户端重复编码。

```
for ws in spectrum_clients:
    prof = _spectrum_profile[ws]                 # 默认 high
    if not _spectrum_frame_due(ws, tick, prof):  # 形状无关的分频闸
        continue
    await ws.send_bytes(variants[prof.shape])
    metrics.add_profile_bytes(prof, len(...))
```

`_new_real_spectrum_frame()` 的帧计数闸门保持不变（真帧仍只在硬件前进时发），分频闸作用在它之上；合成回退分支同样进入分频闸——**这条是缺陷修复，不给它留档位开关**（否则最贵的一路仍然满速）。

`scope` 未连接且没有任何客户端时，现有 `idle_interval` 深睡逻辑不动。

档位表与 `_listen_spectrum_clients` 一样是**按 socket 存活的进程内状态**：连接 `finally` 里必须同时 `discard` 两处（`server.py:3795-3796` 已有前者的位置），否则每条重连都会漏一个条目——本仓 `spectrum_clients` 靠异常回收，档位表没有这层保护，必须显式清。

## 5. 客户端改动形态

| 端 | 改动 | 是否需发版 |
|---|---|---|
| Web | `ft710_main.js` / `listen.js`：accept 后发 caps；Settings 段加三档选择器，落 cookie `ft710_scopeProfile`（进 `settings_manager.js` 的键表） | 服务端发布即生效 |
| Android | `SpectrumFrame.kt` 收 851 与 1701 两种长度；`ConnectionManager` accept 后发 caps；设置页三档 + DataStore 持久化 | **需出 APK**（`release.sh --apk-only`） |
| iOS | 只需发 caps 即可选档（短帧本已容错） | 可选，P3 |

UI 入口固定放设置页，**不放状态行**（安卓状态行只剩 269/344 dp 预算，塞不进第三档指示）。

## 6. 观测与验收

**观测补齐**（否则"降一半"无法证明）：

- `session_metrics` 增按档位的帧数与 payload 字节；`METERED_KINDS` 保持只增不改语义。
- `report()` 文案把频谱/音频速率明确标成 `payload≈`，并写明"线上口径对启用了 permessage-deflate 的浏览器不可见（压缩发生在 uvicorn 内部）"。禁止再把 payload 当账单读。

**验收门槛**（同机同工况对比）：

1. `high` 档下抓包/日志逐字节等于今天：仍是 1701 B、真频谱仍 ≈11 fps、metrics 仍 ≈151 kbps。**这是回归闸门，不通过就不算发版**。
2. 未声明能力的连接（模拟老安卓：连上不发 caps）必须始终收到 1701 B。
3. `mid` 档 payload 落在 37.8 kbps ±15%，`low` 落在 18.9 kbps ±15%。
4. 拔掉 scope_pipe 造出合成回退：`high` 仍 ≈408 kbps payload，`mid/low` 分别 ≈102/51 kbps，**不再出现"坏消息比好消息贵 3 倍"**。
5. 三档切换在已连会话上即时生效（重发 caps），不需重开 socket；重连后档位保持（客户端持久化）。
6. `updateSMeterFromSpectrum()` 在 ÷4 下仍正常工作（S 表另有 CAT 10 Hz 与 scope 合并两路，档位只影响瀑布新行数）。

### 6.1 P1 实测结果（2026-10-07，服务端 + Web 已实现）

环境：本机无 FTDI 硬件 ⇒ 服务端处于 **S-meter 回退态**（`Spectrum broadcast active: S-meter fallback, 1701 bytes/frame, 1 clients`，该行与改动前逐字相同）。
工具：`dev_tools/spectrum_profile_probe.py`（每档独立连接，同一 operator token）。

| 档位 | 帧长分布 | fps | payload kbps | 相对 high | 闸门 |
|---|---|---|---|---|---|
| `high` | 1701 B ×153 | 25.4 | 345.9 | 1 | PASS（只有 1701 B） |
| `mid` | 851 B ×78 | 12.9 | 88.0 | **0.254** | PASS（只有 851 B） |
| `low` | 851 B ×53 | 6.6 | 45.1 | **0.126** | PASS（只有 851 B） |
| 不发 caps | 1701 B ×157 | 26.1 | 354.8 | 1.03 | PASS（只有 1701 B） |

读法：
- **两个因子都在生效**：`mid/high` 字节比 0.254 ≈ 851/1701 × 1/2；`low/high` 0.126 ≈ 851/1701 × 1/4。只砍 wf2 会得到 0.5，只降帧率也只会得到 0.5，这正是 §3 选择两因子的原因。
- **兼容闸门成立**：不发 caps 的连接与 `high` 在 ±15% 内相同（实测差 2.5%），且**只出现 1701 B 帧**——已装 Android 的 `size != 1701` 硬判不会被触发。
- **回退态同样受分频**（本次实测即在回退态）：`high` ≈26 fps（tick 30 Hz 减去循环开销），`low` 仍是它的 ≈1/4。修复前该路径无闸门，30 Hz × 1701 B 全额发出。
- 服务端日志逐档确认协商：`Spectrum profile mid for operator socket: wf1 frame (851 B), divider 2`。

两个执行期发现（已修，记录在此以免复现）：
1. **探针的 `--no-deflate` 不能测"浏览器线上轴"**：它统计的是 `len(msg)`，而 `websockets` 库会透明解压，所以带不带该旗标打印的都是 payload 字节（实测两次 45103 B 完全相同）。浏览器线上口径（1701 B → ~441 B/帧）只能靠 §2 里那次裸 socket 复现，工具的输出与 docstring 已改为如实说明。
2. **无硬件时背靠背连接会握手超时**：每次 `/WSspectrum` 连接都会拉起 `scope_pipe`，而本机没有 FTDI ⇒ 5 次 open 尝试（≈1.6 s）后退出、1 s 后再重启；连续探测会撞进这个 churn（第 3 次连接两次都 `timed out while waiting for handshake response`）。**在两次探测之间留 8 s 即稳定通过**，与档位逻辑无关。有真机（FTDI 在位）时不存在这个 churn。

未在本轮验证的：真频谱态（需 FT-710 + FTDI 在位，预期 `high` ≈151 kbps、`mid` ≈38、`low` ≈19）；浏览器 UI 里的手动三件事（NET 选择器可见/切换后帧长与帧率变化/刷新后 cookie 保持）；已装 Android 与 iOS 的真机回归（本阶段它们不发 caps，行为应完全不变）。

## 7. 测试计划

- ⚠️ **AGENTS.md:104 与 `docs/PROJECT_MAP.md:39` 把这个文件写成 `tests/test_ws_protocol.py`，该路径不存在** —— 真名 `tests/test_server_ws_protocol.py`（内含读前端源码做契约断言的用例，如 `:280` 断言 `wsSpectrum.readyState === WebSocket.CONNECTING`、`:334` 按 `@app.websocket("/WSspectrum")` 与 `# ── Audio RX WebSocket` 两个标记切出 handler 源码块再断言）。改这个 handler 时**必须保住那两个标记串**，否则该守卫测试会以一种很难读的方式失败。顺带把这两处文件名一起修正。
- 新增：caps 白名单解析、未知档名回退 `high`、未声明连接永不得短帧、短帧长度=851 且首字节=0x01、每档 payload 速率、合成回退受分频闸约束。
- `tests/test_listen_only.py:397`（`LISTEN_SPECTRUM_DIVIDER`）重钉为 `listen` 预设等价。
- `tests/test_session_metrics.py`（`:206` 断言 `METERED_KINDS` 集合）随新增字段更新。
- `tests/test_ic7300_runtime_reliability.py:87/93`（stub 的 `get_spectrum_binary` 与 `SPECTRUM_BROADCAST_FPS == 30`）同步。
- 安卓：`SpectrumFrameTest` 两种长度用例 + 瀑布渲染；`MainScreenComposeTest` 冒烟。
- iOS：仅单测（无真机）。

## 8. 文档同步清单（改哪必改哪）

| 位置 | 问题 |
|---|---|
| SDD **§9.2.4**（`09-architecture-overview.md:47-50`） | 定义了 v1=851 / v2=1701 两种格式，但 **v2 从未上过线**（无任何代码发 `0x02`，且改了会杀 iOS）⇒ 本设计把 v1 落实，并把 v2 标为**已废弃命名**，避免后人以为可以"切到 v2" |
| SDD **NFR-003** | 两处错：①"~1701 B @ ~30fps ≈ 51 KB/s"（真频谱实测 11.1 fps；51 KB/s 是退化态）②"~851 bytes/frame fallback" —— **回退态同样是 1701 B**（走同一个 `get_spectrum_binary()`），该数字从未为真 |
| SDD **§9.2.4 分频段** | "listener throttled to every Nth frame (~10 Hz)、省三分之二" 只在回退态成立；真频谱 11.1 fps 下 ÷3 实际是 **3.7 Hz**，省的仍是三分之二（分频按比例，与帧源无关），但 "~10 Hz" 这个绝对值是错的 |
| SDD **AD-023** 范围段（`08-architecture-decisions.md:385`） | "控制面 <10 kbps，对比频谱 408 kbps" —— 408 是回退态；且这是 payload 口径，非线上口径 |
| SDD **§01 / §03 / §04** | 三处都把 `/WSspectrum` 写成 "v1=851B wf1, v2=1701B wf1+wf2" + "~30fps"，需与上面一起对齐 |
| SDD §9.1 / §12.5.3 | 帧格式与速率描述随 shape 可选而变 |
| `AGENTS.md` server.py 行 | 补 `/WSspectrum` 档位语义 |
| `server.py:1517` docstring | "Runs at 5 fps" 陈旧 |
| `opus_rx.py:68` | 声称存在 `setOpusBitrate`，实际不存在 —— 删或标 TODO |
| `docs/IOS_OPUS_INTEGRATION.md:64` | 同上 |
| `scope_handler.py:434` / `listen.js` / `SpectrumProcessor.swift` 注释 | 帧长不再恒为 1701，注释要写"1701 或 851" |
| **跨仓 `mrrc_hub`** `NFR-H007` / `AD-H14` | 单会话 0.48 Mbps、频谱占 86% 建在 30 fps 假设上；实测真频谱 151 kbps payload / 占 72%，且改为档位可配。hub 扇出门槛的输入数据本身需修正 |
| `CHANGELOG.md` + `packaging/windows/MRRC-Modern.iss` | 版本权威链：CHANGELOG 顶部条目是**唯一真相**（`release-artifacts.json` 的 `app_version_source`），`iss-version` / `website-cards-en` / `website-cards-zh` 三条规则要求 `.iss` 与官网中英下载卡一起动；SDD 侧 `SDD/14-version-history.md` 首行是 V 号权威，`SDD/README.md:50` 镜像它。`tests/test_release_artifacts.py` 会在套件内跑这个校验器 —— **漏一处就直接红** |
| `AGENTS.md:104` / `docs/PROJECT_MAP.md:39` | 都引用了不存在的 `tests/test_ws_protocol.py`（真名 `test_server_ws_protocol.py`），见 §7 |

## 9. 风险与回滚

- **最大风险是 §6.1 那条静默丢帧**：任何未走 caps 路径就发短帧的改动，会让老安卓"连上但频谱不动"且无错误。缓解＝默认值恒为 `high` + 用例 3 钉死 + 日志打开发档时打一行档位。
- 分频闸对 `tick` 取模，与真帧的 `_frame_count` 前进时刻不同步 ⇒ 平均速率准确但瞬时抖动。可接受（瀑布本就按帧滚）。
- 回滚：删掉 caps 解析分支即回到今天形态（`high` 是逐字节兼容的默认），无需数据迁移；cookie/DataStore 里的档位键留着无害。

## 10. 分期

> 接下来的第一个实现计划只覆盖 **P1**（服务端 + Web）；P2/P3 在 P1 验收后各自开新的 spec→plan 周期，不在本计划里预写。

1. **P1 服务端 + Web**：形状/分频模型、caps 解析、回退封顶、双变体扇出、metrics 分档、Web 设置项与 caps 发送、测试与文档同步。发布后 Web 立即全端生效，iOS 不需出包。
2. **P2 安卓**：`SpectrumFrame` 双长度 + caps + 设置页三档 + DataStore → `./release.sh --apk-only` → `publish-card.sh` 更新官网下载卡。**绝不跑全站 deploy。**
3. **P3 iOS（可选）**：只加 caps 与设置项。
