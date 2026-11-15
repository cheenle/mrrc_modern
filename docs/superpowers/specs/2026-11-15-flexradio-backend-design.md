# FlexRadio（SmartSDR）原生后端设计记录

> **状态（2026-11-15）**：设计定稿，**未开始实现**。
> **没有真机**。协议依据是厂商公开的 SmartSDR TCP/IP API 与一个 GPL 客户端里读到的
> **事实**（未复制代码），分析记录见 `docs/flexradio-smartsdr-analysis.md`。
> 交付口径：**原生后端，一次做全**（控制 + 频谱 + RX 音频 + TX 语音 + 表）。
> 验收方式：**种子用户到手后远程协助过验收清单**（§11）。在那之前 `VERIFIED=False`，
> PTT/TUNE 一律走 AD-019 / NFR-067 门禁。
>
> 起因：客户问「我这台 FLEX-6400 能不能用」。Flex 没有串口 CAT、没有 USB 声卡，
> 整条链路走网络（TCP 4992 文本命令 + VITA-49 UDP 流），与 SunSDR2 DX 同属
> 「盒子里什么都不插、只吃一根网线」的形态，因此照 `backends/sunsdr/` 的架构落地。

## 1. 目标与非目标

**目标**：`MRRC_RADIO_MODEL=flex` 时，浏览器端与 FT-710 的体验对齐 —— 频率/模式/滤波器/
AGC/NB/NR/ANF/功率/PTT/语音收发/瀑布/表/记忆频道全部可用；iOS、Android、`/listen`
不用改一行协议代码。

**非目标（YAGNI，写死不做）**：

| 不做 | 理由 |
| --- | --- |
| SmartLink / WAN 远程接入 | 盒子就在电台旁边；盒子自己就是远程接入层（与 FT-710 同构） |
| DAX 声卡桥（Windows 虚拟声卡） | 那要求客户留一台 Windows 常开，盒子方案就失去意义 |
| CW 键控（netcw / CWX） | v1 不含；`xmit`/TUNE 之外的键控是另一套流 |
| 多 slice / 多 pan 界面 | v1 只用一个 slice + 一个 pan；协议层已按 `client_handle` 隔离，将来加是纯前端 |
| Flex 瀑布 tile（`0x8004`） | 我们的 1701 B 帧里 wf1/wf2 都来自 `spectrum_rx1`，FFT 一帧两用（§6） |
| 全双工、diversity、SO2R | 6400 本来没有；6600/8600 有，但我们的音频管线是单声道 |
| ATR-1000 天调联动 | 不改既有可选模块，Flex 侧用 `atu`/`transmit tune` |

## 2. 协议依据与许可

| 依据 | 用途 |
| --- | --- |
| `github.com/flexradio/smartsdr-api-docs/wiki` | **权威**：命令、状态、发现、VITA-49 |
| `github.com/aethersdr/AetherSDR`（GPL-3.0） | 只读事实：连接序列、PCC 表、音频/Opus 包布局、interlock 行为 |
| `flexradio.com` 规格页 | 型号能力（slice/pan/ATU/SCU） |

⚠️ AetherSDR 与 FlexLib 是 GPL-3.0，本仓库没有 GPL 声明。落地时沿用
`docs/g7250-wfview-analysis.md` §7 的做法：**读事实、重新表达、不搬代码**；
每一段实现都要能指回 `docs/flexradio-smartsdr-analysis.md` 的条款号。

## 3. 架构

`backends/flex/`，六个文件，依赖**单向**：

```
vita.py      （VITA-49 纯字节：头/FFT/音频/表/Opus 发射包）
protocol.py  （文本：C/R/S 行、多词对象名、meter 的 # 特例、发现载荷）
     ↓
session.py   （TCP 4992 + UDP：握手、序号、回执、订阅、client_handle 过滤、interlock、重连）
     ↓
engine.py    （唯一所有者：两条 socket、pan 参数、表定义与最新值、RX 环形队列、TX 节拍器、info 缓存）
     ↓
audio.py     （FlexAudioHandler：AudioHandler 同款方法面）   scope.py（FlexScopeProducer）
     ↓
backend.py   （FlexBackend + FlexController：~50 个抽象 CAT 方法的翻译层）
```

- `vita`/`protocol` 不知道 `session` 存在；`session` 不知道 `engine`；`audio`/`scope`
  **只共享 engine**，不各自开连接（SunSDR 已踩过"两处各开一条流"）。
- 只有 `backend.py` 碰 `config`/`RadioCapabilities`；**不 import `server`/`radio_state`**。
- 电台自发的状态变更经 `backend.set_broadcast_callback()` 回流到 `RadioState` 的
  dirty 广播（与 CI-V transceive 同一条路）。

## 4. 模块规格

### 4.1 `vita.py`（纯函数，可离线全测）

- 头：28 字节七字大端；解析 type/C/T/TSI/TSF/packet count/size（**size 单位是 32-bit 字**）。
  T 位置位 ⇒ 末尾 4 字节 trailer 先剥掉再取载荷。
- 分派只认 **PCC**（`0x8003` FFT、`0x8004` 瀑布、`0x03E3` RX 音频、`0x0123` DAX、
  `0x8005` Opus、`0x8002` 表、`0x02E3..E6` DAX IQ）。
- FFT：12 字节子头 + 分片拼帧（`frame_index`/`total_bins_in_frame`），缺段丢整帧并计数。
- 音频：`float32` 大端交错 → `numpy.float32`，校验 `len % 8 == 0`。
- 表：`N × (uint16 id, int16 raw)`。
- **Opus 发射包**：`word0=(3<<28)|(1<<27)|(3<<22)|(1<<20)|sizeWords`、`word1=stream_id`、
  `word2=0x00001C2D`、`word3=0x534C0000|0x8005`、`word4..6=0`，载荷**不补齐**。

### 4.2 `protocol.py`

- `build_command(seq, cmd)` → `C<seq>|<cmd>\n`；`parse_response` → `(seq, code, body, debug)`；
  `parse_status` → `(handle, object_name, {k: v})`，切分点是 **`=` 之前的最后一个空格**
  （对象名是多词的：`slice 0`、`display pan 0x40xxxxxx`、`interlock band 9`）。
- `parse_meter_definition`：`meter` 对象的键值分隔符是 **`#`**，要单独走。
- `parse_info`、`parse_slice_list`、`parse_discovery_payload`（`model=… serial=… ip=… port=…`，
  `name` 里的 `_` 还原成空格）。
- 错误码表（`0`、`0x50000003/16/2C/32/33`、`0x50001000`）。

### 4.3 `session.py`

- 连接：TCP → 读 `V<ver>` → `H<handle>` → `sub {slice,pan,tx,atu,meter,audio} all` →
  `client gui` / `client program` / `client station` → UDP bind 并从该 socket 向
  `<radio>:4992` 发 **1 个 `0x00` 字节** → `client udpport <port>`（答 `0x50001000`
  属预期，记 debug 日志，不算失败）。
- 序号：32-bit 递增；`{seq: Future}` 表；**回执超时默认 2 s**。`xmit` 的确认是另一条时间轴：
  回执（≤2 s）只表示命令被接受，之后另有 **1.5 s** 等 interlock 进 `TRANSMITTING`（§7.2）。
- 订阅 → 按对象名分派给 engine；`client_handle` 不等于自己的状态**只用于观察**，
  不写进我们的模型（§9.1）。
- interlock 状态机：保留最近状态与迁移时间；`TRANSMITTING` 是唯一"真的在发"。
- 重连：指数退避 1→2→4→8 s 上限 30 s；重连后重放 §4.3 连接序列并重建 pan/stream。

### 4.4 `engine.py`

- 持有全部网络资源；对外暴露同步读取（`freq`, `mode`, `s_meter`, `meters`）与
  队列操作（`rx_audio`, `feed_tx_audio`, `start_tx/stop_tx`）。
- RX：4 包重排窗口（VITA packet count **只有 4 bit**，16 个一循环，所以只做相邻 4 包），
  环形队列上限 1 s，超出丢最旧并计 `rx_drops`。
- TX：10 ms 节拍器（`240` 样本/帧 @24 kHz）；队列上限 200 ms，溢出丢最旧并计
  `tx_drops`；节拍器只搬包，不做编码（编码在 `audio.py`）。
- 表：定义表（id → src/nam/unit/low/hi/fps）+ 最新值；S 表取 `src=SLC` & `nam=LEVEL`，
  功率取 `src=TX-` & `nam=FWDPWR`，**一律按名字查，不写死 id**。
- 能力缓存：`info` 的 `model/serial/max_slices/max_panadapters/bands`，以及 `atu` 状态是否存在。

### 4.5 `audio.py`（`FlexAudioHandler`）

对 `server.py` 暴露与 `backends/sunsdr/audio.py` **完全相同**的方法面：`start_rx`、
`stop_rx`、`restart_rx`、`read_rx_chunk`、`note_rx_chunk`、`rx_silent_seconds`、
`encode_rx_audio`、`start_tx`、`stop_tx`、`feed_tx_audio`、`write_tx_chunk`（no-op）、
`device_rates`、`tx_stats`、`tx_queue_frames`、`has_pending_tx_audio`、`close`。
线程边界同 SunSDR：`start_tx`/`stop_tx`/`close` 由 server 经 `asyncio.to_thread` 调用，
用 `run_coroutine_threadsafe` 桥到事件循环。

### 4.6 `scope.py`（`FlexScopeProducer`）

`ScopeProducer` 契约，`start/stop` 只管自己的发射任务（engine 归 backend）；
`notify_tx` 是 no-op（VITA 流不因发射而中断，与 FT-710 的 FT4222 不同）。

### 4.7 `backend.py`

`FlexBackend` 实现 `capabilities/create_audio_handler/create_scope_producer/cat/
set_broadcast_callback/boot_verify/bands/ui_modes/state_tables/poll 表`；
`FlexController`（即 `backend.cat`）把抽象面翻译成命令 + 状态读取，**没有命令的能力
一律返回 `False`/`None` 且不进轮询表**（不伪造 CAT 帧）。

## 5. 采样率账与延迟预算

### 5.1 RX

```
UDP 0x03E3  24 kHz 立体声 float32 大端  (192 KB/s)
 vita.py    字节→numpy、len%8 校验、T 位 trailer
 engine.py  立体声→单声道(均值)、4 包重排、环形队列
 audio.py   2:1 上采样 24k→48k（流式半带 FIR 31 抽头，跨包保状态）、限幅、int16
 read_rx_chunk()  20 ms = 960 样本（24 kHz 侧每块 480 样本上采成 960；不足留 carry）  (96 KB/s)
 server.py  audio_levels.rx_db 在 fan-out 时加（不在 handler 里）
```

**已知取舍**：取均值 ⇒ 丢掉 `audio_pan` 的左右定位。我们的管线是单声道，写进文档。

### 5.2 TX

```
server 48k int16 单声道 960 样本
 audio.py   2:1 下采样（同族半带 FIR，必须抗混叠）、复制 L=R、攒 240 样本帧
 opus       Opus(24 kHz, stereo, 10 ms, 70 kbps, complexity 10, APPLICATION_AUDIO)
 vita.py    28 字节头 + 载荷（不补齐、无 trailer）
 engine.py  10 ms 节拍器发送
```

`opus_rx.py` 就地扩参：`RxOpusEncoder(rate=48000, channels=1)` 默认值不变（现有路径
字节不变），新增 `TxOpusEncoder`（24 kHz 立体声）。不新开第二份 libopus ctypes 绑定。

### 5.3 延迟预算（不含公网）

| 段 | RX | TX |
| --- | --- | --- |
| 滤波群延迟 | ~0.7 ms | ~0.7 ms |
| 缓冲 | 抖动 40–60 ms | Opus 帧 10 ms + 节拍器 10–20 ms |
| 块/编码 | 20 ms 块 | 编码 1–3 ms |
| **合计** | **60–80 ms** | **~25 ms** |

### 5.4 丢样本算谁的账

| 情形 | 行为 | 计数 |
| --- | --- | --- |
| RX 环形队列满 | 丢最旧 | `engine.rx_drops` |
| TX 队列满 | 丢最旧 | `tx_stats()["queue_drops"]` |
| VITA 乱序 | 4 包窗口 | 序列错误计数 |
| VITA 丢包 | 不重传，补静音保持时间轴 | 丢包计数 |

## 6. 频谱映射

- **只请求 FFT，不请求瀑布流**：`scope_handler.get_spectrum_binary()` 的 wf1/wf2
  都来自 `spectrum_rx1`，所以 `0x8004` 在 v1 完全不需要。
- 请求参数刻意对齐：`xpixels=850`（电台拒绝则线性重采样到 850）、`ypixels=256`、
  `min_dbm=-120`、`max_dbm=0`。代入
  `dBm = max − s/(ypixels−1) × (max−min)` 得 **`v_u8 = 255 − s`**：
  一次减法，无浮点、无舍入漂移。**这条是本节的核心不变量，测试要盯住。**
- `scope_spans = {idx: {name, freq}}`（与 FT-710 同形状），索引 ↔ `bandwidth=<MHz>`；
  默认 15 fps。
- CENTER 模式：pan 中心 = slice 频率，`scope_start_freq = center − span/2`。
- 点击调谐：`slice m <MHz> pan=<panId>`（**不用 `slice tune`**，它会把 pan 一起拖走）。

## 7. TX 安全链

### 7.1 门禁

`sunsdr/backend.py::_tx_allowed()` 同款：**每次按键**重读 `config.ALLOW_UNVERIFIED_TX`，
`capabilities.tx_gated = not (ALLOW or VERIFIED)`，一进程一条警告。**释放路径永不拦。**

### 7.2 按键序列

```
1. _tx_allowed()                    False → 返回 False，一个字节都不发
2. TX 音频流在不在（连接时创建、id 缓存）  不在 → 重建一次；仍失败 → 拒绝
3. slice s <n> tx=1                等状态推送确认，不信回执
4. engine.start_tx()               开 10 ms 节拍器（先有音频后有载波）
5. xmit 1
6. interlock: PTT_REQUESTED→TRANSMITTING，1.5 s 超时＝失败；TX_FAULT/TIMEOUT 立即释放
```

### 7.3 读回是唯一权威

`get_ptt()` 返回 **interlock 缓存**（`TRANSMITTING` 才是 1）。`R<seq>|0` 只代表命令被接受。
电台**从不**在 transmit 状态里发 `mox=`。

### 7.4 释放矩阵（每条幂等 + 有界）

| 触发 | Flex 侧动作 |
| --- | --- |
| `set_ptt(False)` / `set_tune(False)` | `xmit 0` / `transmit tune off` + 停节拍器 |
| 按键 socket 断开（`_ptt_key_ws`） | 同上 |
| lifespan Force-RX / `_schedule_restart()` | 同上（`os._exit` 之前） |
| 心跳超时 / `MRRC_PTT_MAX_TX_SECONDS` | 同上 |
| interlock 离开 `TRANSMITTING`（电台超时、另一端抢发、fault） | 停节拍器 + `broadcast("tx_status", …)` 让浏览器放下 PTT |
| 会话/TCP 断 | 什么都发不出去 —— 只有电台自己的 TX timeout 能救场（§7.5） |

### 7.5 电台 TX timeout

连接时读 interlock/transmit 里的 TX timeout；**> 60 s 或未启用** ⇒ 启动日志警告 +
`/settings` 卡片 + `_diag_flex.py` 红项。文档建议值 **≤ 30 s**。这是"PTT 双防线"
在 Flex 上的第三条腿。

### 7.6 `tx_audio_path_unconfirmed`（U1）

按键后 1 s 窗口内看电台自己的 `TX-/SC_MIC` 表（按名字匹配）：有能量 ⇒ 音频路通；
一直是底噪而我们的队列非空 ⇒ **立刻 `xmit 0`**、置 latch、**后续按键直接拒绝**，
日志与界面给出「去电台面板把 PC 音频选成发射源」。`/settings` 可清 latch
（与 unverified-tx 开关同一张卡）。**宁可拒绝发射，也不发静音载波。**

### 7.7 TUNE

`atu` 状态存在且确认 → `transmit tune on/off`；没有 ATU（6400 选配）→ 我们自己的
700 Hz 音经 TX 路出真实载波（SunSDR 同款），**绝不伪造天调**。两者都在门禁之下。

### 7.8 只警告不自动放开

`FWDPWR`/`SWR` 按键 1 s 后仍 0 W / 1.0 只警告（假负载、调谐功率都可能合法）；
`TX_FAULT` 一定自动释放。

## 8. 注册、配置与用户可见行为

### 8.1 注册

- `backends/__init__.py::_BACKENDS["flex"] = ("backends.flex.backend", "FlexBackend")`
- **一个键覆盖全族**：型号从 `info`/`radio` 状态自述，能力位（`max_slices`、
  `max_panadapters`、`atu`）按自述填写；连不上时**保守全关**（界面不出现电台不接受的按钮）。
- `model_connection("flex") → {"transport": "network", "host_required": True}`，
  与 `sunsdr2dx` 同一条设置页/引导页路径。
- `model_verification()`：`flex` 的 `verified` 由后端模块的 `VERIFIED` 常量给出
  （与 `sunsdr` 同一分支）。
- `profiles/flex.env`：`MRRC_RADIO_MODEL=flex`、`MRRC_ALLOW_UNVERIFIED_TX=0`。

### 8.2 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MRRC_FLEX_HOST` | 空 | 空 = **自动发现**：监听电台每秒发出的 UDP 4992 广播（VITA ext-data，class `0x534CFFFF`）；填 IP 则跳过发现 |
| `MRRC_FLEX_TX_TIMEOUT_WARN_S` | 60 | 电台 TX timeout 警告阈值 |
| `MRRC_FLEX_SPECTRUM_FPS` | 15 | 请求的 pan fps |

### 8.3 用户可见

- `/setup` 与「连接设置」：机型选 `FlexRadio (SmartSDR)`，主机地址**可留空**（自动发现）。
- **发现到多台**时的规则：全部记进日志，连**第一台应答 `info` 的**；要指定就填 IP
  （`_diag_flex.py` 会把同一网段发现到的每一台都打印出来，供操作员挑）。
- 顶部状态栏显示 `info` 出的机型与序列号（连上之前显示 `FlexRadio (SmartSDR)`）。
- 不支持的能力不渲染：ATU（无）、split/VFO-B（单 slice 时）、多 pan。

## 9. 与既有功能的交互

### 9.1 多客户端（Multi-Flex）

SmartSDR/Maestro 可以同时连着。规则：**按 `client_handle` 过滤**状态与 VITA 包；
只操作我们自己创建的 slice/pan；别人的 slice **不写、不抢 `active`/`tx`**。
AetherSDR v0.2.2 修的就是这个 —— 不是可选项。

### 9.2 既有音频消费者

FT8（12 kHz tap）、录音、本地喇叭、CQ 播放、`audio_levels`（TX 麦克风增益只作用于浏览器
麦克风）全部**不改**：它们看到的是同一条 48 kHz 单声道 int16 流。

### 9.3 角色与接口

`/listen` 只读角色、`session_metrics`、`/api/perf`、频谱带宽档位（AD-025）
全部沿用，不需要 Flex 专属分支。

## 10. 离线测试策略

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_flex_protocol.py` | `C/R/S` 解析与构造、多词对象名切分、`meter` 的 `#`、`info`/`slice list`、发现载荷、错误码、畸形输入 |
| `tests/test_flex_vita.py` | 头逐字段、PCC 分派、FFT 分片拼帧、**`v = 255 − s` 恒等式**、音频 `len%8`/trailer、表单位换算、**Opus 包黄金字节** |
| `tests/test_flex_session.py` | 握手顺序、序号回绕、回执超时、状态推送、重连退避、interlock 全迁移、`client_handle` 过滤、`xmit` 确认超时 |
| `tests/test_flex_audio.py` | 24k↔48k 60 秒不漂、48k→24k 抗混叠、240 样本攒帧、节拍器假时钟、溢出计数 |
| `tests/test_flex_scope.py` | 850 直通/重采样回退、span→bandwidth、`scope_start_freq` |
| `tests/test_flex_backend.py` | 6400 自述 2/2 与 6600 自述 4/4 同一份代码、`tx_gated`、不支持能力返回 False 且不进轮询、broadcast 接线、`initial_state_sync` 形状 |

`tests/flex_fake_radio.py`：脚本化 SmartSDR 服务端（TCP 4992 + 我们的 UDP），能扮演
「只接受 850 像素」「interlock 卡在 PTT_REQUESTED」「另一端客户端持有 slice」。

**必然会红的既有测试**（一起改）：`test_backend_factory.py::test_known_models_covers_every_backend`、
`test_box_profiles.py`（profiles 一致性 + `UNVERIFIED` 集合 + 推导）、`tests/README.md` 的计数。

## 11. 真机验收（种子用户 + 远程协助）

盒子在电台旁边，所以远程协助（`help_session.py` 那个窗口）登进盒子即到达电台。
**客户动手清单**（会话前 10 分钟）：

1. 盒子与电台同一交换机/路由器（同网段）
2. 电台开机、接假负载或天线，功率设 5 W
3. 面板里把 **PC 音频选成发射源**（U1）
4. 电台菜单里把 **TX timeout 设到 30 s**
5. 准备好 SmartSDR 客户端（用于并排对照频谱与 S 表）

**会话内跑**：`python _diag_flex.py`（单文件可打包，无仓库依赖）—— 发现 → 握手 →
`info` → pan 3 秒抓帧（bin 统计）→ RX 音频 2 秒（RMS/速率）→ 表清单 → interlock 只读；
`--tx-check --allow-tx` 加 200 ms 音 + PTT 读回。**`finally` 里恢复 pan/stream 状态、释放 PTT。**

**验收项（九条，逐条记"过/不过/证据"）**：

1. 不填 IP 也能发现并连上，`info` 的机型/序列号出现在界面
2. `max_slices`/`max_panadapters` 与机型规格一致
3. 850 bin；与 SmartSDR 自己的显示**对照刻度**（不只是"有信号"）；点击调谐落点准
4. RX 语音可懂、无咔哒、S 表跟随 SmartSDR
5. TX（假负载、5 W）：`SC_MIC` 有能量、`FWDPWR`/`SWR` 合理、USB/LSB **不反边**、延迟 ~25 ms
6. 四条释放路径都真的停发：正常放手 / 关浏览器 / **拔网线**（等电台 TX timeout）/ 配置保存重启
7. 同时开 SmartSDR：我们的盒子**不抢**别人的 slice
8. ATU（若选配）：TUNE 真的动作
9. 回归：FT8 解码、录音、CQ 播放

**离线永远证明不了**：主观音质、USB/LSB 反边、PC 音频源行为、fps 与带宽上限、
选配 ATU 的 `atu` 形状。

## 12. 开放项

| 编号 | 未定 | 怎么定 |
| --- | --- | --- |
| U1 | PC 音频作发射源的默认值 | 真机面板 / `SC_MIC` 自证（§7.6） |
| U2 | RX 音频实际包率（10/20 ms） | 抓包数载荷长度 |
| U3 | 6400 的 fps / 带宽 / `xpixels` 上限 | `display pan set` 回执 + 帧率测量 |
| U4 | 选配 ATU 的 `atu` 状态形状 | `sub atu all` 打印 |
| U5 | Opus 参数对主观音质的影响 | 现场 QSO 听感 |
| U6 | 8400/8600（现行线）是否完全同构 | 借一台复测；协议层按同构设计 |

## 13. 风险与对策

| 风险 | 对策 |
| --- | --- |
| 没有真机 ⇒ 首版可能"连不上" | 协议层纯函数 + 假电台；`_diag_flex.py` 一条命令出证据；失败信息点名到命令与错误码 |
| TX 音频源未确认 ⇒ 静音载波 | §7.6 自证 + latch 拒绝 |
| 多客户端互踩 | §9.1 三层过滤 + 测试用例扮演"另一端客户端" |
| 6400 已停产，现行是 8400/8600 | 一个键 + 自述能力，不写型号分支（U6） |
| GPL 污染 | 只读事实、重新表达；实现评审时逐条对照分析文档 |

## 14. 实现切分（交付口径仍是"一次做全"）

1. **P1 纯层**：`vita.py` + `protocol.py` + 两个测试文件（离线全绿）
2. **P2 会话**：`session.py` + `tests/flex_fake_radio.py` + 会话测试
3. **P3 音频/频谱**：`engine.py` + `audio.py` + `scope.py` + `opus_rx.py` 扩参 +
   `test_flex_audio.py` + `test_flex_scope.py`
4. **P4 契约与集成**：`backend.py` + `test_flex_backend.py` + 注册/配置/UI +
   `_diag_flex.py`（仓库根，照 `_diag_civ.py`）+ 文档同步

## 15. 文档同步清单

`docs/flexradio-smartsdr-analysis.md`（已有）、本文件、`backends/__init__.py` 文档串、
`AGENTS.md` 的模块表与机型白名单、`README.md`、`docs/DOCUMENT_INDEX.md`、
`tests/README.md` 计数、`docs/PROJECT_MAP.md` 若有新文档位、`SDD/` 的机型章节与 NFR/AD
引用（AD-019 门禁、AD-025 频谱档位、AD-032 音频增益）。

## 16. 状态与下一步

设计到此为止。下一步：`writing-plans` 出实现计划（P1→P4，每批可独立验收），
按 §10 的测试先写后做。真机，则等种子用户（§11）。
