# FlexRadio（SmartSDR 协议）笔记 —— 给 MRRC 的落地依据

> 用途：若要把 FLEX-6400（及其家族）像 FT-710 那样原生驱动，按本文的字段实现即可。
> 本文只写**能指到出处**的部分，未定项单独列在 §9，不要把「推断」当「结论」。
> 证据等级：**【文档】**= 厂商公开 API 文档；**【开源事实】**= AetherSDR 源码/文档里读到的事实
> （**只取事实，未复制代码**，见 §0）；**【厂商】**= flexradio.com 规格页。

| 项 | 值 |
| --- | --- |
| 分析日期 | 2026-11-15 |
| 目标机型 | FLEX-6400 / 6400M（同族 6300/6500/6600/6700，现行 8400/8600 协议相同） |
| 协议版本 | SmartSDR TCP/IP API **v1.4.0.0**（固件 4.x） |
| 真机证据 | **无**。本文全部来自文档与开源实现 ⇒ TX 必须走 AD-019 门禁（§8.1） |

---

## 0. 参考、许可，以及 wfview 为什么不适用

| 来源 | 用途 | 许可 |
| --- | --- | --- |
| `https://github.com/flexradio/smartsdr-api-docs/wiki` | **权威**：命令、状态、发现、VITA-49 | 厂商公开 wiki |
| `https://github.com/aethersdr/AetherSDR` | 原生 Linux 客户端，把协议细节写成了文档；本仓库**只读事实** | **GPL-3.0** ⚠️ |
| `https://community.flexradio.com/discussion/8033408/` | 现场状态：哪些功能真跑了、跑在哪台机器上 | 论坛 |
| `https://www.flexradio.com/comparison/`、`/6400-6600-model-comparison` | 机型能力表 | 厂商规格 |

**wfview 帮不上忙**（已核实 `wfview.org/supported-radios`）：它的机型表是
Icom（705/7300/7610/7760/7851/9700/R8600）、Kenwood（TS-590S/TS-890S）、
Yaesu **FT-710**、Xiegu X6100/X6200 —— **没有 Flex**。论坛答复（Flex 6000 CAT 帖）：
「highly unlikely that wfview will support any radio other than Icom … would be a lot of work」。
原因是架构：wfview 整条栈围绕 CI-V + Icom 的 RS-BA1 UDP，而 Flex 是「TCP 4992 文本命令 +
VITA-49 二进制流」，两套栈没有可复用的一层。

⚠️ **许可红线**：AetherSDR 与 FlexLib 都是 GPL-3.0，本仓库没有 GPL 声明。做法与
`docs/g7250-wfview-analysis.md` §7 一致：**读事实、重新表达、不搬代码**。本文每条都是
「协议要求什么」，不是「某项目的实现」。

---

## 1. 发现：电台每秒 UDP 广播自己【文档】

- 端口 **UDP 4992**，VITA-49 **ext-data** 包，`stream_id = 0x800`，class ID **`0x534CFFFF`**。
- 载荷是一行字符串（空格在 `name` 里换成 `_`）：

```
model=%s serial=%s version=%s name=%s callsign=%s ip=%u.%u.%u.%u port=%u
```

⇒ 盒子不需要先知道 IP：**听着 4992 就能发现同网段的电台**（这与 SunSDR 的 UDP 广播发现同构，
`net_wifi`/`sdr` 已有「盒子在局域网里找设备」的先例）。

## 2. 控制通道：TCP 4992，纯文本【文档】

### 2.1 三个前缀（双向）

| 前缀 | 方向 | 含义 |
| --- | --- | --- |
| `V<d.e.a.b>` | 电台→客户端 | 协议版本，连接后第一行（实测 `V1.4.0.0`） |
| `H<handle>` | 电台→客户端 | 本连接 32-bit client handle（八位十六进制） |
| `C<seq>\|<cmd>` | 客户端→电台 | 命令，`\n` 结尾；`CD<seq>` 是带调试回显的变体 |
| `R<seq>\|<hex>\|<msg>\|…` | 电台→客户端 | 每条命令**有且只有一条**回执；`0` = 成功 |
| `S<handle>\|<object> k=v …` | 电台→客户端 | 异步状态推送；`handle=0` 表示系统/命令行引发 |
| `M<…>` | 电台→客户端 | 信息提示 |

状态对象名是**多词**的（`slice 0`、`display pan 0x40000000`、`interlock band 9`），
解析要点：`=` 之前的最后一个空格就是「对象名 / 键值对」的分界。

### 2.2 连接序列【文档 + 开源事实】

```
TCP connect :4992
  ← V1.4.0.0
  ← H<handle>
→ sub slice all          （依次：slice pan tx amplifier atu meter audio gps apd client xvtr）
→ client gui
→ client program <name>
→ client station <name>
→ UDP bind（本地随机端口），从该 socket 向 <radio>:4992 发 1 个 0x00 字节  ← 端口登记
→ client udpport <port>  （v1.4.0.0 会答错误 0x50001000，属预期；真正生效的是上一行）
→ slice list             （空的就 slice create；见 §4）
→ stream create type=remote_audio_rx compression=none   → 音频开始到达 UDP
```

**已知坑（都来自 AetherSDR 的协议笔记，属"协议行为"而非实现）**

- `client set udpport` 不存在 → 用上面那个**一字节 UDP 包**。
- **VITA-49 从 UDP `4993` 发出，不是 4992**（真机 FLEX-6600 v4.3.16，2026-11-16：
  客户端 socket 一旦 `connect((radio, 4992))`，内核按对端端口过滤，8 秒 0 个包；
  不 connect 则 8 秒 3094 个包，源地址一律 `192.168.124.16:4993`）。所以那个 UDP
  socket **必须保持未连接**，发送时每次显式给地址。
- `stream create` 的回执是**裸十六进制**（`4000008` / `84000000`），而
  `display panafall create` 的是带前缀、逗号分隔的（`0x40000002,0x42000002`）。
- 是 `client set local_ptt=1`，不是 `enforce_local_ptt=1`。
- slice 的频率状态键是 **`RF_frequency`**，不是 `freq`。
- AF 增益的状态键是 **`audio_level`**。
- 电台**从不在 transmit 状态里发 `mox=`**；判收发看 **interlock 状态机**（§3.4）。
- 点击调谐用 `slice m <MHz> pan=<panId>`，不是 `slice tune`（后者会把 pan 一起拖走）。
- `display pan` 的 `xpixels`/`ypixels` 创建后必须自己推（默认 50×20，不是 UI 尺寸）。

### 2.3 常用错误码（回执里的 `<hex>`）

| 码 | 含义 |
| --- | --- |
| `0` | OK |
| `0x50000016` | malformed / unknown command |
| `0x5000002C` | 参数个数不对 |
| `0x50000032` | 未知模式字符串 |
| `0x50000033` | 参数越界 |
| `0x50000003` | license 不允许创建 slice |
| `0x50001000` | 上面那条 `client udpport` 的预期错误 |
| `0x50000004` | 参数取值非法（如 `rxant=ANT9`）【真机】 |
| `0x5000002D` | 未知的键（如 pan 上的 `ant=`）【真机】 |
| `0x50000009` | panadapter 槽位已满（`max_panadapters`）【真机】 |
| `0x50000031` | 参数越界（回执带文字，如 `RF Gain out of range`）【真机】 |
| `0x50000059` | 无效的 stream id【真机】 |

## 3. 状态对象（我们真正要订阅的）

### 3.1 `slice <n>`【文档】

键（节选，全部以 `slice s <n> k=v` 写入、`slice t <n> <MHz>` 调谐）：

`RF_frequency`（**MHz，浮点**）、`mode`、`filter_lo`/`filter_hi`、`audio_level`、
`agc_mode`（off/slow/med/fast）、`squelch`/`squelch_level`、`nb`/`nb_level`、
`nr`/`nr_level`、`anf`/`anf_level`、`tx`（1=本 slice 发射）、`dax`、
`pan`（所属 pan 的 id）、`client_handle`（属主）、`active`、`sample_rate`。

**天线相关（2026-11-16 真机核对，详见 `docs/flexradio-antenna-protocol.md`）**：
**没有 `ant` 这个键** —— 接收天线是 **`rxant`**，发射天线是 **`txant`**；
候选由电台自己报在 **`ant_list`** / **`tx_ant_list`**（FLEX-6600：
`ANT1,ANT2,RX_A,RX_B,XVTA,XVTB` / `ANT1,ANT2,XVTA,XVTB`）；
`rfgain` 属于 **SCU × 天线** 组合；`loopa`/`loopb`/`diversity*` 是 RX A/B 环回。
`slice s <n> ant=ANT1` 会被**静默接受并忽略**（回 `0`，什么都不改）——这是本仓
1.28.69 之前"天线命令成功但没变"的成因。
另：**状态推送是增量的**（只带变化的键），解析必须逐键更新；**S 表按 slice 分开**
（`meter N.src=SLC#N.num=<slice>#N.nam=LEVEL`），取第一条就是读别人的。

### 3.2 `display pan <0x40xxxxxx>` / `display waterfall <0x42xxxxxx>`【文档+开源事实】

- 创建用 `display panafall create x=100 y=100` → 回执带出 pan id；waterfall id 由状态推送给出。
- 写：`display pan set <id> xpixels=… ypixels=… bandwidth=<MHz> min_dbm=… max_dbm=… fps=…`，
  瀑布同构（`display waterfall set <id> …`）。
- **FFT 流 id = pan id**，**瀑布流 id = waterfall id**（VITA 里就是这么分的）。
- `min_dbm`/`max_dbm` 是**电台自己的像素刻度**（典型 −135/−40），FFT 的 uint16 是像素 Y（§5.2）。

### 3.3 `meter <n>`【文档】

定义走 `S…|meter 7.src=SLC#7.num=0#7.nam=LEVEL#7.unit=dBm#7.low=-150.0#7.hi=20.0#7.fps=10`
—— **这个对象的键值分隔符是 `#`**，跟别的状态对象不一样。
订阅：`sub meter all`（或 `sub meter <index>`）；数据在 VITA PCC `0x8002`（§5.5）。

S 表：`src=SLC` 的 `LEVEL`（dBm）。发射侧常见：`FWDPWR`(dBm)、`REFPWR`、`SWR`、
`ALC`(dBFS)、`COMPPEAK`(dBFS)、`PATEMP`(degC)、`+13.8A`(Volts)。

### 3.4 `transmit` / `interlock`【文档】

- 收发：`xmit 1` / `xmit 0`；回执只是"命令已接受"，**真正的状态在 interlock**：
  `NOT_READY / PTT_REQUESTED / READY / RECEIVE / TIMEOUT / TRANSMITTING / TX_FAULT / UNKEY_REQUESTED`。
- TUNE：`transmit tune on|off`。
- 其余：`transmit set rfpower= tunepower= miclevel= met_in_rx= speech_processor_enable=
  speech_processor_level= filter_low= filter_high= dax= vox= vox_level= vox_delay= …`

### 3.5 `info`（一次问答，不是推送）

`info` 回执带出机型、序列号、**max slices / max panadapters**、波段列表。
⇒ **能力应当向电台要，不要靠型号名硬编码**（§7）。

## 4. 我们真正要发的命令（最小集）

```
sub slice all | sub pan all | sub tx all | sub meter all | sub atu all
client gui | client program MRRC | client station <box>
<UDP 0x00 登记> | client udpport <port>
slice list                                          → 空则创建
slice create freq=14.074 ant=ANT1 mode=usb          → 回执里是 slice 号
slice t <n> <MHz> [autopan=0|1]                     → 调谐
slice m <MHz> pan=<panId>                           → 点击调谐（保留 pan）
slice s <n> mode=usb filter_lo=… filter_hi=… audio_level=… agc_mode=… nb=… nr=…
slice s <n> tx=1                                    → 指定发射 slice
display panafall create x=100 y=100                 → pan/waterfall id
display pan set <panId> xpixels=850 ypixels=… bandwidth=… min_dbm=… max_dbm=… fps=…
display waterfall set <wfId> xpixels=… ypixels=… bandwidth=… min_dbm=… max_dbm=… fps=…
stream create type=remote_audio_rx compression=none → 音频流
stream create type=remote_audio_tx                  → 发射流（电台强制 Opus，§6）
xmit 1 | xmit 0 | transmit tune on|off
transmit set rfpower=… miclevel=…
```

> 注意 `slice` 的三个动作是**单字母**：`t`=tune、`s`=set、`m`=move/click-to-tune。

## 5. VITA-49 流：一个头，五种载荷【文档+开源事实】

### 5.1 头

28 字节（7 个 32-bit 大端字）。第 0 字：`type=3 (ExtDataWithStream)`, C 位, TSI/TSF,
低 16 位 = **包长（32-bit 字数）**；第 1 字 = stream id；第 2、3 字 = class id
（`ICC = 0x534C`，低位 **PCC**）。**流类型只认 PCC，不认包类型**。

| PCC | 内容 | 载荷 |
| --- | --- | --- |
| `0x8003` | panadapter FFT | 12 字节子头 + uint16 大端 bin（可分片） |
| `0x8004` | waterfall tile | 36 字节子头 + uint16 大端 bin |
| `0x03E3` | RX 音频（**不压缩**） | **float32 立体声，大端，24 kHz** |
| `0x0123` | DAX 音频 | int16 单声道，大端 |
| `0x8005` | **Opus**（发射用，§6） | Opus 包 |
| `0x8002` | 表数据 | N×(uint16 id, int16 raw) |
| `0x02E3/E4/E5/E6` | DAX IQ | 24/48/96/192 kHz |

**stream id 决定"这是谁的"**（真机 2026-11-16，同时开着 SmartSDR-Win）：FFT 的
stream id **就是 pan id**（我们的 `0x40000002`、别人的 `0x40000000` 同时到达），
瀑布 tile 用 waterfall id（`0x42000000`/`0x42000002`），RX 音频用 `stream create`
给的那个 id（`0x04000008`），表数据是固定 `0x00000700`。**订阅了 `sub pan all`
就等于订阅了所有客户端的 pan**，所以按 id 过滤不是优化，是正确性：两路不同带宽的
段混进同一个 850-bin 组装器，出来的是垃圾。实测载荷率：FFT 850 bin / 15 fps
= 两段/帧 ≈ 30 段/s；RX 音频 1024 B/包（256 帧 float32 立体声 24 kHz）≈ 94 包/s。

### 5.2 FFT（`0x8003`）

- 12 字节子头：`start_bin_index / num_bins / bin_size / total_bins_in_frame / frame_index`，
  一帧可能分多个包，要按 frame 拼。
- **bin 不是 dBm，是像素 Y**（0 = 顶 = `max_dbm`，`ypixels-1` = 底 = `min_dbm`）：
  `dBm = max_dbm − sample / (ypixels − 1) × (max_dbm − min_dbm)`

### 5.3 瀑布（`0x8004`）

36 字节子头（偏移相对载荷起点）：

| 偏移 | 类型 | 字段 |
| --- | --- | --- |
| 0 | int64 | FrameLowFreq |
| 8 | int64 | BinBandwidth |
| 16 | uint32 | LineDurationMS |
| 20 | uint16 | Width |
| 22 | uint16 | Height |
| 24 | uint32 | Timecode |
| 28 | uint32 | AutoBlackLevel |
| 32 | uint16 | TotalBinsInFrame |
| 34 | uint16 | FirstBinIndex |

强度是**任意正数刻度**，不是 dBm：`intensity = int16(raw) / 128`（实测噪声底 ~96–106，
信号峰 ~110–115），所以瀑布色标与 FFT 的 dBm 轴**各自独立**。

### 5.4 RX 音频（`0x03E3`）

- 载荷就是 PCM：**float32 立体声交错，大端，24 kHz**（载荷字节数 % 8 == 0 自检）。
- 若头的 T 位置位，尾部还有 4 字节 trailer。
- `compression=opus` 时改走 `0x8005`（给 WAN/SmartLink 用）——盒子和电台同网段，
  **用 none，省一次解码**。

### 5.5 表（`0x8002`）

- 载荷：N×(uint16 meter_id, int16 raw_value)，按 §3.3 的**动态 id** 对应到定义。
- 换算按定义的 `unit`：`dBm/dB/dBFS/SWR` → `raw/128`；`Volts/Amps` → `raw/1024`；
  `degF/degC` → `raw/64`。
- 功率：`FWDPWR` 是 dBm ⇒ 瓦特 = `10^(dBm/10)/1000`。

## 6. 发射音频：**电台强制 Opus**（这条最容易踩）【开源事实】

`stream create type=remote_audio_tx` 之后，无论客户端想发什么，**电台都只收 Opus**。
客户端→电台的包是「28 字节 VITA 头 + Opus 载荷，**无 trailer、不补齐**」：

```
word0 = (3<<28)|(1<<27)|(3<<22)|(1<<20)|sizeWords    // type=3, C=1, TSI=3, TSF=1
word1 = remote_audio_tx 的 stream id
word2 = 0x00001C2D                                    // OUI = FlexRadio
word3 = 0x534C0000 | 0x8005                           // ICC=0x534C, PCC=0x8005(Opus)
word4..6 = 0                                          // 时间戳全零
```

- Opus 参数：**24 kHz、立体声、10 ms 帧（240 样本/声道）、~70 kbps、COMPLEXITY=10**。
- 电台把它当 **PC 麦克风**（信号链入口 `SC_MIC`）；`transmit set miclevel=` 仍然有效。
- 电台端要选 PC 音频为发射源（`transmit set dax=…` / 面板 P/CW 里的选择）——
  真机上这一项落在电台的设置里，**属于验收时要确认的第一件事**（§9）。

## 7. Flex 型号谱系，以及"能力从哪来"

### 7.1 表格【厂商】

| 型号 | slice | SCU | pan | ATU | 备注 |
| --- | --- | --- | --- | --- | --- |
| 6300 | 1 | 1 | 1 | 无 | 停产 |
| **6400 / 6400M** | **2** | **1** | **2** | **选配内置** | 停产；RX 0.03–54 MHz，TX 160–6 m，100 W |
| 6500 | 4 | 2 | 2 | 无 | 停产 |
| 6600 / 6600M | 4 | 2 | 4 | **内置** | 停产；全双工、diversity |
| 6700 | 8 | 2 | 4 | 内置 | 停产；diversity |
| 8400 / 8400M | 2 | 1 | 2 | — | **现行**（Signature） |
| 8600 / 8600M | 4 | 2 | 4 | — | **现行**；diversity、SO2R、7 阶滤波 |

### 7.2 结论：**型号表只是"连上之前的估计"**

`info` 与 `radio` 状态会告诉客户端机型、序列号、`max_slices`、`max_panadapters`、
波段表；ATU 是否存在由 `atu` 状态给出。所以：

- 后端只要把「协议」写对，**6400 / 6600 / 8600 是同一份代码**；
- 硬编码的型号能力表只在**还没连上**时用于 UI 预判，写错会变成"提供了电台不接受的
  按钮"（AetherSDR 就吃过这个亏：残留的 8 会让它给 6400 提供第 3 个 pan 的创建）。
- 因此 MRRC 侧的合理形态是：**一个 `flex` 后端键 + 运行时自述能力**，而不是七个机型键。

## 8. 映射到 MRRC 的架构（这些是要落地时逐条对的）

| MRRC 面 | Flex 侧 | 说明 |
| --- | --- | --- |
| CAT（~50 个抽象方法） | TCP 4992 文本命令 + 状态推送 | 状态推送可**替代大部分轮询**（`poll_scheduler` 的 7 个任务里，频率/模式/S 表改成事件驱动） |
| 频谱（850 bin 帧） | `0x8003` FFT | 带宽/中心我们自己设 ⇒ `scope_span` 是"请求的 pan 带宽"的索引；`min/max_dbm` 决定纵轴 |
| 瀑布 | `0x8004` tile | 强度刻度独立于 dBm；可先只用 FFT 画瀑布（一帧两用），tile 作为后续增强 |
| RX 音频（48 kHz 单声道 int16） | `0x03E3` 24 kHz float32 立体声 | 混单声道 + 2:1 上采样 → 48 kHz；解码不进 Opus（用 none） |
| TX 音频（48 kHz 单声道 int16） | Opus 24 kHz 立体声 10 ms | 2:1 下采样 + 复制双声道 + Opus 编码（`opus_rx.py` 要扩成可配采样率/声道） |
| PTT / TUNE | `xmit 1/0`、`transmit tune on/off` + interlock 读回 | 读回是唯一权威，绝不信回执 |
| 表 | `0x8002` + `meter` 定义 | S 表 `SLC/LEVEL`；功率 `TX-/FWDPWR`；SWR/ALC/压缩同族 |
| 天线/ATU | `slice s <n> txant=…` / `atu start` | 6400 的 ATU 是**选配**：一切以 `atu` 状态为准 |

### 8.1 没有真机 ⇒ 三条硬规矩

1. `VERIFIED = False`、`tx_gated = True`（AD-019 / NFR-067），客户在 `/settings` 里勾确认才发。
2. 协议层每一层都要能**离线测**：VITA-49 用合成包 + 真实抓包样本；会话用假电台驱动。
3. 上线后第一件事是 `_diag_flex.py` 这类自检脚本，一条命令出证据（照 `_diag_g7250_net.py`）。

### 8.2 与其他客户端共存（Multi-Flex）

电台允许多个客户端同时连（SmartSDR/Maestro + 我们的盒子）。要点：
**按 `client_handle` 过滤状态与 VITA 包**；只操作自己创建的 slice/pan；
别的客户端的 slice 不要抢（`active`/`tx` 的写入同样受此约束）。
AetherSDR 的 v0.2.2 修的就是这个问题 —— 说明它不是可选项。

## 9. 未定项（真机才能落锤）

| 编号 | 未定项 | 怎么定 |
| --- | --- | --- |
| U1 | 电台端"PC 音频作发射源"的默认值 | 真机 P/CW 面板 / `transmit` 状态 |
| U2 | RX 音频 `0x03E3` 的实际包率（10/20 ms） | 抓包数载荷长度 |
| U3 | FFT `fps` 上限与带宽上限（6400 实测值） | `display pan set` 回执 + 帧率测量 |
| U4 | 6400 选配 ATU 的 `atu` 状态形状 | `sub atu all` 打印 |
| U5 | 语音延迟与 Opus 参数是否影响主观音质 | 现场 QSO 听感 |
| U6 | `--`（6400 已停产，8420/8600 是否完全同构） | 借一台 8400/8600 复测 |
