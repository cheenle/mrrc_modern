# FlexRadio 天线与 SCU 协议参考（写在实现之前）
> 对象模型（radio → SCU → panadapter → slice → stream）与 2026-11-16 的全部真机结论见 **[flexradio-architecture.md](flexradio-architecture.md)**；本文只讲天线/SCU 这一条线。


> **状态**：研究文档（2026-11-16）。实现在 `backends/flex/` + `static/ft710_ui.js`，
> 设计记录见 `docs/superpowers/specs/2026-11-16-flex-antenna-selection-design.md`，
> 架构决策见 `SDD/08-architecture-decisions.md` 的 **AD-036**。
>
> **证据标记**（沿用 `docs/flexradio-smartsdr-analysis.md` 的体例）：
> **【文档】** = 厂商手册（SmartSDR for Windows Software User's Guide v2）原文；
> **【实测】** = 本仓在真机 FLEX-6600（`1618-1546-6600-4131`，固件 4.3.16.42034，
> protocol 1.4.0.0）上量到的字节/回执/读数；
> **【未证】** = 没有证据，只是推断 —— 不许当结论用。

---

## 0. 一句话

**"天线选择"在 SmartSDR 里不是全局开关，也不是每台电台一个开关：它是 `slice` 上的一个
枚举 `rxant`，而这个 slice 与它所在 panadapter（背后是 SCU）**共享**同一路接收射频。**
6600/6700 有两个 SCU，因此可以同时用两路接收天线；6400/6500 只有一个。

---

## 1. 名词与硬件（先厘清，否则后面全错）

| 名词 | 含义 | 依据 |
| --- | --- | --- |
| **slice** | 一个解调通道：频率 + 模式 + 滤波器 + AGC + 天线… | 手册 |
| **panadapter** | 一块频谱/瀑布视图，有自己的带宽与中心频率 | 手册 |
| **SCU** | Spectral Capture Unit：射频采样通道。`info` 回执里是 `num_scu=2` | 【实测】 |
| **ANT1 / ANT2** | 收**发**天线口，可被任一 SCU 选用；TX 也走这里 | 【文档】 |
| **RX_A / RX_B** | 纯接收口。**硬件上分别硬接 SCU0 / SCU1**，不能互换 | 【文档】 |
| **XVTA / XVTB** | 变频器（transverter）接口。仅在 `Settings → Radio Setup → XVTR` 配过之后才有意义 | 【文档】 |
| **ATU** | 天调。本机 `info` 里 `atu_present=1` | 【实测】 |
| **loopa / loopb** | 6600/6700 的 RX A / RX B 环回（diversity 用），只能从 Panadapter 的 ANT 菜单操作 | 【文档】 |

> 本机 `info` 回执（【实测】）：
> `model="FLEX-6600" … atu_present=1, num_scu=2, num_slice=4, num_tx=1`
> —— 两个 SCU、四个 slice、一路发射。

---

## 2. 规则：天线是 panadapter（= SCU）的属性

【文档】用户指南 §27 原文（两处，短的更清楚）：

> **All Slices placed on a Panadapter must use the same receive antenna as the
> Panadapter. Changing the receive antenna for a single slice will change the
> receive antenna for its host Panadapter and all Slices within.**

> The FLEX-6700/6700R and FLEX-6600/M models allow **simultaneous reception from
> two receive antennas**. The FLEX-6500, FLEX-6400/M and FLEX-6300 operates from a
> single [SCU].

> Each SCU … has the option of a dedicated receive-only antenna designated RX A
> and RX B respectively. These inputs are **hard wired to their respective SCU**
> and may not be switched between SCUs. … This does not preclude using RX A with
> ANT2 or RX B with ANT1.

由此得三条工程结论：

1. **命令入口是 slice，作用域是 panadapter。** `slice set <n> rxant=X` 会改掉它所在的
   **整块 panadapter**（以及上面的所有 slice）。所以一个 pan 上有多条 slice 时，
   只改一条等于改全部 —— 不要去"纠正"另一条，会打架。
2. **两块 pan 可以各用各的天线**（6600 的两个 SCU）。别的客户端占着 SCU0 时，我们这块
   pan 落在 SCU1，选 ANT1 依然可以（ANT1/ANT2 是可切换的），但 RX_A 在 SCU0 上、
   RX_B 在 SCU1 上 —— 选 RX_A 时 **实际是借用了另一个 SCU 的接收口**，[文档] 明确允许
   （"does not preclude using RX A with ANT2"）。
3. **接收 RF 增益（`rfgain`）属于 "SCU × 天线" 这个组合**，不是全局值。【文档】原文：
   *"The receive RF Gain control … is tied to its respective SCU / receive antenna
   combination."* —— 也就是说换天线之后底噪/表头的变化里，可能夹着射频增益的差异，
   判读时不能只看一个数字（§6.2 的实测表就是在这种前提下取的）。

---

## 3. 状态字段（`slice <n>`，真机原样）

【实测】把电台自己的状态行抓下来，与天线相关的键是这些（长行已折行）：

```
slice 2 in_use=1 sample_rate=24000 RF_frequency=14.100000 client_handle=0x5C097BF7
        index_letter=B rit_on=0 xit_on=0 rxant=XVTA txant=ANT1            ← 本 slice 的收发天线
        pan=0x40000002 active=1 tx=0                                       ← 它属于哪块 pan、是否在解调
        rfgain=8                                                           ← SCU×天线 组合的增益
        loopa=0 loopb=0                                                    ← RX A/B 环回（diversity）
        diversity=0 diversity_parent=0 diversity_child=0 diversity_index=…
        ant_list=ANT1,ANT2,RX_A,RX_B,XVTA,XVTB                             ← 本机可选的接收口
        tx_ant_list=ANT1,ANT2,XVTA,XVTB                                    ← 本机可选的发射口
        mode_list=LSB,USB,AM,CW,DIGL,DIGU,SAM,FM,NFM,DFM,RTTY
        …
```

要点：

- **`ant_list` / `tx_ant_list` 是电台自己报的候选**，随型号/配置不同（6400/6500 只有一路
  SCU，`ant_list` 与 6600 不同；配了变频器才会出现对应的 XVTR 口）。**绝不硬编码**。
- **状态是增量的**：一次推送只带变化的键（例如 `S…|slice 2 rxant=ANT2` 只有 `rxant`）。
  【实测】所以解析必须"逐键更新、保留旧值"，不能把一行当成完整快照 —— 我们的
  `engine._update_slice()` 就是这么写的。
- `active=1` 的那条 slice 才是电台正在解调的那条；`client_handle` 是**创建**这条 slice 的
  客户端。别人的 handle ≠ 我们的，于是"按 handle 认领"在多人共用的机器上会失效
  （这就是 AD-036 里 `ensure_slice()` 存在的原因）。
- **每个 slice 有自己的 S 表**：`meter N.src=SLC#N.num=<slice 号>#N.nam=LEVEL`。
  一台有 4 条 slice 的机器上就有 4 组 `SLC/LEVEL`，**按 `num == 自己的 slice 号` 取**，
  取第一条就是读别人的表（【实测】：切片 0/1/2/3 分别 −109.7 / −112.8 / −110.4 / −109.9 dBm）。

---

## 4. 命令与错误码

### 4.1 写

| 目的 | 命令 | 作用域 |
| --- | --- | --- |
| 设接收天线 | `slice set <n> rxant=<名字>` | 该 slice **所在的整块 panadapter** |
| 设发射天线 | `slice set <n> txant=<名字>` | 该 slice（TX 归属见 §5） |
| 设射频增益 | `slice set <n> rfgain=<0..?>` | SCU × 天线组合 |

【实测】六个合法值（`ANT1 ANT2 RX_A RX_B XVTA XVTB`）逐个下发，电台回 `R|0|` 并把新值
推回状态行；`rxant` 立刻生效（§6 的 S 表随之变化）。**没有** `client ant` 之类的全局命令。

### 4.2 错误码（真机撞出来的，务必记住）

| 回执 | 含义 | 怎么撞出来的 |
| --- | --- | --- |
| `0` | 接受 | — |
| **`0x50000004`** | 参数取值非法 | `slice set 2 rxant=ANT9` 【实测】 |
| `0x50000031` | 参数越界（回执带文字：`RF Gain out of range (0.00000)`） | `slice set 2 rfgain=99` 【实测】 |
| `0x5000002D` | 未知的键/参数 | `display pan set <pan> ant=ANT1` 【实测】 |
| `0x50000009` | panadapter 槽位已满 | 4 块 pan 全被占用时 `display panafall create` 【实测】 |
| `0x50000059` | 无效的 stream id | `stream remove <不存在的 id>` 【实测】 |
| `0x50001000` | `client udpport` 的预期错误（见主分析文档） | — |

### 4.3 三个陷阱（都真的踩到了）

1. **`ant=` 是个假键。** 在 **slice** 上下发 `slice set <n> ant=ANT1`，电台回 `0`、**什么都不改**；
   在 **pan** 上下发同一个键才报 `0x5000002D`。本仓 1.28.69 之前的天线功能就是发 `ant=`，
   于是"命令成功、天线没变"——这也是"切了没反应"的第一个成因。
2. **`mode=BOGUS` 也回 `0`。** 取值校验不是所有键都有：`rxant` 这种枚举会被校验（错误 4），
   而 `mode` 这种字符串键会被静默吞掉。**不要用"回执 0"证明取值合法**。
3. **`display pan set <pan> rxant=…` 也回 0，但不是权威入口。** 天线归属的是 slice/pan 的
   绑定关系，官方菜单也在 Panadapter 的 ANT 里改 —— 我们的实现统一走 `slice set <n> rxant=`，
   不去赌 pan 上那个键的语义。【未证】pan 上的 `rxant` 是否等价。

---

## 4b. 频谱载荷的结构（实测，v4.3.16.42034）

一行 FFT = 两段：`start_bin=0 num_bins=670` + `start_bin=670 num_bins=180`，`total_bins=850`、`bin_size=2`、**子头 16 字节**，每段末尾带 2 个零 bin。详见 [flexradio-architecture.md](flexradio-architecture.md) §3.4b。

## 5. 发射天线（本文只定性，未上机验证）

【文档】

- 每条 slice 都有自己的 `txant`；**红色 TX 按钮指哪条 slice，哪条 slice 的 `txant` 生效**。
- `tx_ant_list` 不含 `RX_A/RX_B`（它们是纯接收口），这与 `ant_list` 的差别正是证据。
- QSK（CW 收发间隙接收）只有把**独立的接收天线**接到 `RX_A`/`RX_B`/`XVTR` 才能到 100+ WPM；
  `QSK is not supported with a separate receive antenna if that antenna is connected to
  ANT1 or ANT2`。

【未证】本仓的 TX 门禁（`VERIFIED=False`）没有放开，`txant` 一律未测：`set_antenna_name()`
只写 `rxant`，不碰 `txant`。

---

## 6. 真机实测（FLEX-6600 / 4.3.16.42034）

### 6.1 切换本身是生效的

同一频点、同一模式（USB）、只改 `rxant`，读**该 slice 自己的** `SLC/LEVEL`：

| 端口 | ANT1 | ANT2 | RX_A | RX_B | XVTA | XVTB |
| --- | --- | --- | --- | --- | --- | --- |
| dBm | −101.2 | −101.6 | −101.9 | −111.4 | −111.1 | −110.9 |

结论：ANT1/ANT2/RX_A 与 RX_B/XVTA/XVTB 相差约 **9–10 dB** —— 前者接着有效负载、后者是空口
（本机没配变频器，`sub xvtr all` 没有任何对象【实测】）。**切换确实改变射频路径。**

> 注意：这组数字只说明"路径变了"，不说明"有信号"。同一台机器上把 7.2–7.4 MHz、
> 9.5–9.9 MHz、10/15 MHz 授时台全扫一遍，读数一律 −110 dBm 上下（AM 下整体低 8 dB，
> 是解调器差异，不是天线差异）。**那台天线当时没有信号** —— 这是射频侧的事，不是软件。

### 6.2 "切了没反应"的两个真实成因（都在本仓代码里）

1. **S 表读错对象**：`s_meter_dbm()` 取第一个 `SLC/LEVEL`，在 4 条 slice 的机器上就是
   别人的表 → 改自己的天线，表不动。
2. **应用根本没有自己的 slice**：引擎从不创建 slice，"按 handle 认领"在共用机器上撞不上，
   `_slice_id()` 回退到 `min(slices)` —— 调频、改天线全打到别人的 slice 上。

两条都已在 AD-036 的实现里修掉（`meter_by_name(..., num=own_slice)`、`ensure_slice()`）。

### 6.3 panadapter 槽位会满

`num_panadapters` 上限 4。应用 + 别的 GUI 客户端把槽位占满之后，`display panafall create`
回 `0x50000009`。**旧代码把错误回执里的数字当成 pan id**（实测拿到 `0x40000000`，
即别人的 pan），之后每条 `display pan set` 都在动别人的瀑布。现在：拒绝 → 只认带我们
`client_handle` 的 pan → 没有就**点名失败**，不半连接。

---

## 7. 对本产品（MRRC Modern）的契约

| 层 | 契约 |
| --- | --- |
| `RadioState` | `antenna_name: str`（电台的叫法）、`antenna_list: list[str]`（电台报的候选）；`antenna: int` 保留给串口后端 |
| 后端 | `set_antenna_name(name) -> bool`、`get_antenna_name()`；候选来自 `ant_list`，校验失败**不发明命令** |
| 引擎 | `ensure_slice()` 保证驱动的是**自己 pan 上**的那条（优先 `active=1`）；`s_meter_dbm()` 按 `own_slice` 取表 |
| 服务端 | WS `{"type":"set","field":"antenna","value":"ANT1"}`（也接受整数，兼容串口语义）；`listen` 角色被服务端拒绝 |
| 前端 | DSP 行的 `ANT` 键，点一下循环一个口；`antenna_list` 为空时**整个键不出现**；标签就是电台的叫法 |

**为什么不做成"每台电台的下拉框"**：候选是电台在**连上之后**才报的（`ant_list`），
静态能力表根本不知道 6600 有 6 个口、6400 只有几个、配了变频器会多出什么。所以它只能是
**状态**，不能是能力位。【实测】两条 `ant_list` 在同一个固件上随配置而变。

---

## 8. 未证 / 待办（不许当结论）

| # | 未证的事 | 怎么证 |
| --- | --- | --- |
| A1 | 同一 pan 上多条 slice 的 `rxant` 传播（手册说会一起变） | 在一块空闲 pan 上建两条 slice，改一条看另一条 —— 本机 pan 槽位已满，需要等一个客户端退出 |
| A2 | `txant` 与 TX 归属（红 TX 按钮） | 放开 `MRRC_ALLOW_UNVERIFIED_TX`、接假负载后再验 |
| A3 | `rfgain` 与天线的耦合强度（换口后是否需要重设） | 切换后强制一次完整状态（`slice list`）对比 `rfgain` |
| A4 | `loopa`/`loopb`（diversity）在 API 里怎么写 | 手册说只能从 Panadapter 的 ANT 菜单操作，API 未找到写入口 |
| A5 | 6400/6500 的 `ant_list` 实际形状 | 借一台 6400 复测（按设计它只有一路 SCU） |
| A6 | `XVTA/XVTB` 在配置了变频器后的取值与 `xvtr` 状态形状 | 需要一台配了变频器的机器 |

---

## 9. 出处

- SmartSDR for Windows Software User's Guide v2，§27「天线选择」与 §29「变频器配置」
  （`https://www.flexradio.com/wp-content/uploads/assets/SmartSDR-Software-User-Guide-v2.pdf`）
- FLEX-6400/FLEX-6600 Hardware Reference Manual（`flexradio.com/documentation/…`）——
  **只取得目录页**，§12「Antenna Considerations」正文被站点拦下，本文未引用其内容
- 本仓：`docs/flexradio-smartsdr-analysis.md`（VITA-49 与文本协议）、
  `docs/superpowers/specs/2026-11-15-flexradio-backend-design.md`（后端设计）、
  `SDD/08-architecture-decisions.md` **AD-036**
