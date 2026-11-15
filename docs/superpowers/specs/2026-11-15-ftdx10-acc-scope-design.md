# FTDX10 / FTDX101 ACC 频谱源设计记录（转接板路线，不依赖 SCU-LAN10）

> **状态（2026-11-15）**：设计定稿，**尚未有真机，一行实现代码都还没写**。
> 起因：`2026-11-15-scu-lan-backend-design.md` 的开头把处境写死了 ——「瀑布只有两种拿法：
> 背后 13-pin ACC 的 SPI（要自己做转接板，每台一份硬件），或客户已经买下的 SCU-LAN10」。
> 那条线选了后者，走到「TX 音频内层头没解，不能发话，只值一半」。这条线走另一条：
> **只拿频谱**，硬件是一块几十块钱的转接板。
>
> 协议事实来源：`SLTPLAN/FTDX10_WF`（作者在 FTDX10 上的实测记录）、
> `SLTPLAN/FTDX10_WF_HUB`（ESP32-S3 固件 `DX10_WF.ino` 1009 行 + host 参考实现 `remote.py`
> 480 行）、以及本仓已有的 `backends/ft710/scope_frame.py`（4096 帧布局，来自 wfview）。
> 两个上游仓库**都没有 LICENSE 声明**，因此只取协议事实、代码一句不抄 —— 与本仓处理
> wfview（GPL-3.0）的既有政策一致（见 §10）。

## 1. 定位：与 SCU-LAN 线是什么关系

两条线都在解决「Yaesu 新机型没有瀑布」，但选的是两个不同的东西：

| | SCU-LAN 线 | 本线 |
| --- | --- | --- |
| 拿什么 | CAT + 音频 + 频谱，全搬到客户已买的 SCU-LAN10 | **只有频谱** |
| 客户硬件成本 | 已有 SCU-LAN10（约 ¥3000，没有的人要买） | 一块转接板（P2 自制后的目标价：几十元） |
| 现状 | 卡在 TX 音频内层头 → 不能发话 | 未真机验证，但底座全在 |
| 依赖 | 客户买了 SCU-LAN10 | 客户愿意接 5 根线到 ACC |

三条前提，用户已确认，写进设计避免以后被重新讨论：

1. **拓扑：盒子与电台永远在同一处。** 所以转接板只做频谱，CAT 继续走 USB 串口、音频继续走
   电台自带 USB 声卡（两条都已跑通）。不做「电台侧小 SCU-LAN」（Wi-Fi 全套桥），那是另一个
   产品形态，工作量约为本设计的 3–4 倍。
2. **不做全套不只是因为工作量，还有一条硬件事实。** FTDX101MP 的后部音频路由由菜单
   `Rear Select` 在 **USB** 与 **DATA / ACC** 之间二选一（Yaesu 手册 `FTDX101MP_D_OM`；
   现场排障文档把这个当成「没声音」的头号原因）。也就是说，转接板去 ACC 取音频的那一刻，
   我们已经跑通的 USB 音频就哑了 —— 那不是「多拿一路」，那是「换一路更差的」。
3. **频谱是 USB 给不了的唯一一样东西。** 所以 ACC 这个口必须用，而它只需要搬字节。

## 2. 协议事实（实现的就是这两张表）

### 2.1 电台侧（ACC 13-pin）

| 事实 | 值 | 来源 |
| --- | --- | --- |
| 心跳 UART | Pin11（CNT RX）/ Pin12（CNT TX），115200 8N1 | `FTDX10_WF` README |
| 心跳内容 | `alive\0` = `61 6C 69 76 65 00`，**电台每秒发** | 同上 |
| 握手 | 完整收到后 **10 ms 内**回同样 6 字节；2–3 次后电台开始推数据 | 同上 |
| 心跳维持 | 必须持续回；停止后 2–3 s 电台停止推流 | 同上 |
| 回环捷径 | Pin11 ↔ Pin12 直接短接也能完成握手（作者实测；作者的 STM32 版就靠它，不接 UART） | 同上 + `STM32/README.md` |
| SPI 角色 | **电台是 Master**，Mode 1（CPOL=0 / CPHA=1），2 MHz | 同上 |
| SPI 引脚 | Pin4 = MOSI（电台→转接板）、Pin5 = CLK、Pin6 = CS（低有效） | 同上 |
| 帧边界 | CS 拉低 = 一包开始，发完 CS 拉高数毫秒，再开下一包 | 同上 |
| 帧长 | 4096 字节；固件用逻辑分析仪确认一圈 CS 低电平 = **32768 个时钟** | `DX10_WF.ino:829` |
| 危险点 | **Pin8 = 13.8 V，内部 3.15 A 一次性保险丝**；作者短接过一次 → 拆机更换 | `FTDX10_WF` README |

4096 字节布局（与 FT-710 相同，`backends/ft710/scope_frame.py` 已经在解析这一套）：

| 偏移 | 内容 |
| --- | --- |
| 0–849 | ch1 瀑布（850 bin，**取反**：0 = 最强） |
| 850–1699 | ch2 瀑布（给 FTDX101 系列预留） |
| 1700–1899 | ch1 音频 FFT |
| 1900–2299 | ch1 音频波形 |
| 2300–2499 | ch2 音频 FFT（101 预留） |
| 2500–2899 | ch2 音频波形（101 预留） |
| 2900–3049 | 元数据（span / mode / preamp / att / S-meter / VFOA 频率 …） |
| 4092–4095 | sync tail `FF 01 EE 01`（**待真机确认，见 §7**） |

> 「与 FT-710 相同」这句话里，本设计真正复用到的只有两件：**前 850 字节取反的约定**和
> **元数据偏移**。前者由 `parse_scope_frame` 完成（`~b & 0xFF`），后者是它 `DATA_OFFSET = 2900`
> 那一块。其余字段（音频 FFT / 波形）本线一律不看。

### 2.2 转接板侧（`66 CC FF` 帧）

| 源 | 线格式 | 端口 / 握手 |
| --- | --- | --- |
| USB-CDC | 流式：`66 CC FF` + 2B 大端长度 + 1B XOR + payload | 无握手；CDC 的波特率是名义值 |
| TCP | 同上（同一条流） | `:51234`，无握手，断线重连 |
| UDP | 分片：`66 CC FF` + **1B seq + 1B part(0..2)** + 分片 | `:51235`；先发 `HELLO` 注册，固件记住源地址后单播 |

- `MAX_PAYLOAD_LEN = 4600`：略高于 4096，用来让「误匹配帧头」付出最多丢 1 帧的代价。
- 对齐规则（作者文档原文的流程）：搜 `66 CC FF` → 读长度 L → `L > 4600` 则前移 1 字节重搜 →
  否则等 `L + 6` 字节到齐 → 对 payload 算 XOR → 通过则输出，失败则丢弃并继续搜。
- payload 恰好 4096 字节；UDP 三片按 `part` 拼回，`seq` 变了就整帧丢弃（缺片不猜）。
- 帧率（作者固件注释的实测值）：UDP 满速 **25 fps**，TCP 约 1 帧/ACK。

**本设计加的一条诊断（不是门禁）**：成功解出的 payload 若末 4 字节是 `FF 01 EE 01`，
计数 +1；不符则计数并在第一帧时 `WARNING` 一次，**但帧照收**。理由见 §6 最后一条。

### 2.3 从固件里学到的三条非显然约束（P2 做板子的输入，此处先记账）

1. `spi_bus_config_t.max_transfer_sz` 默认 **4092 < 4096**，会把每帧切掉 4 字节 —— 必须显式设大。
2. 事务完成时上报的字节数是 **4097**，要钳到 4096。
3. **布局是硬约束**：CS 必须上拉 + GPIO 毛刺滤波（否则噪声把 4096 字节切碎）；
   UART1 必须放在芯片对侧（GPIO38/39），因为 GPIO1/2 与 SPI(10/11/13) 相邻时串扰会让
   瀑布乱码；UART TX 驱动强度要从 20 mA 降到 5 mA。

## 3. 目录与职责

```
backends/yaesu/acc_scope/
  __init__.py
  frames.py     FrameDecoder（流式）+ UdpReassembler（seq/part 归组）
                纯字节逻辑：无 I/O、无 socket、无 app import —— 可以单独测
  sources.py    SerialSource / TcpSource / UdpSource
                每个都是一个 async generator，yield 4096 字节 payload；
                设备/连接全部可注入（假串口、真 socketpair、假 UDP socket）
  producer.py   AccScopeProducer（实现 backends.base.ScopeProducer）
                start/stop/notify_tx/set_on_frame；读 → 解 → parse_scope_frame → ScopeHandler
```

为什么两层（frames / producer）而不是一个文件：解码器是**唯一**一处需要逐字节推理的东西，
它必须能在没有 socket、没有事件循环的地方被测穿；而 producer 关心的是生命周期、重连、上电顺序。

为什么不放 `backends/ft710/`：这是 Yaesu 家族的第二种频谱来源，和 `scu_lan` 同一个理由 ——
放家族内部，依赖方向一眼可见（`backends/yaesu/backend.py` → `acc_scope` → `backends/ft710/scope_frame.py`）。
**4096 帧的解析器继续留在 `backends/ft710/scope_frame.py`**，因为这个仓里已经有一份，抄一份就是
两份会漂的实现。

## 4. 数据流

```
source（serial/tcp/udp）            → 4096 字节 payload
  ↓ AccScopeProducer
parse_scope_frame(payload, require_sync=False)      ← 复用，不改
  ↓ 10 个字段 + _frame_count += 1 + last_update
ScopeHandler（不改）
  ↓ on_frame 回调
server._on_scope_frame（不改）       → 合并 s_meter / span / mode / start_freq 并广播
  ↓ 30 Hz
_broadcast_spectrum_loop（不改）     → 按 spectrum_profile 出 1701 / 851 字节帧
  ↓
/WSspectrum → 前端（不改）
```

`AccScopeProducer` 写 `ScopeHandler` 的那 10 个字段，与 `FT710ScopeProducer._read_pipe`
**逐字段同形**（`spectrum_rx1/rx2`、`scope_mode`、`scope_span`、`preamp`、`attenuator`、`mode`、
`s_meter`、`vfoa_freq`、`scope_start_freq`、`_frame_count`、`last_update`）。这样服务端、前端、
`spectrum_profile`、`session_metrics`、`listen` / `sdr` 两个角色全部零改动 —— 这是本设计
最重要的形状要求：**新来源不得让任何下游长出新分支**。

## 5. 配置与开关

| 变量 | 值 | 默认 |
| --- | --- | --- |
| `MRRC_ACC_SCOPE` | `off` \| `serial` \| `tcp` \| `udp` | `off` |
| `MRRC_ACC_SCOPE_SERIAL` | 设备路径（`/dev/ttyACM0`、`COM7`） | 空 = 自动找 `/dev/ttyACM*`（Windows 必须显式给，不做猜测） |
| `MRRC_ACC_SCOPE_HOST` | 转接板 IP（tcp/udp 用） | 空 |
| `MRRC_ACC_SCOPE_TCP_PORT` | | `51234` |
| `MRRC_ACC_SCOPE_UDP_PORT` | | `51235` |
| `MRRC_ACC_SCOPE_BAUD` | USB-CDC 名义波特率 | `115200` |

- 只在 `YaesuBackend` 上接线；`capabilities.scope_type` 为 `"acc"`（今天是 `"scu_lan"` / `"none"`）。
- **`off` 时必须与今天逐字节相同**：`create_scope_producer()` 返回 `None`，S-meter 合成瀑布照旧。
- **串口守卫（安全约束）**：拒绝打开与 `MRRC_SERIAL_PORT` 相同的设备 —— CAT 端口不可抢
  （NanoVNA 与 G7250 两条线都立过这条规矩）。判断在打开之前做，不是打开之后。

## 6. 失效模式与守卫

| 失效 | 表现 | 守卫 |
| --- | --- | --- |
| 盒子读得比转接板推得慢 | 队列越积越旧，瀑布永远滞后 | `asyncio.Queue(maxsize=2)`，满则丢最旧；计 `frames_dropped` |
| USB 重枚举 / 转接板重启 | 串口消失、TCP 断开 | 串口与 TCP 1 s 退避重连（换设备路径也要能接上） |
| UDP 注册丢失 | 有连接但零数据 | 每 5 s 重发一次 `HELLO`；3 s 无帧 → `_connected = False` + `WARNING` 一次 |
| 假帧头（payload 里巧合出现 `66 CC FF`） | 解码走偏 | 作者规则：`len > 4600` 前移 1；`len + 6` 到齐后 XOR 不符则丢弃并前移 1；计 `decode_errors` |
| UDP 缺片 / 乱序 | 拼不出 4096 | `seq` 变化即整帧丢弃，不猜、不补 |
| **tail 不匹配** | — | **不丢帧**，只计数 + 第一次 `WARNING` |
| 抢走 CAT 口 | CAT 与频谱同时坏 | §5 的串口守卫 |
| 事件循环被阻塞 | 瀑布掉帧 / 全站卡顿 | 串口读在独立线程，payload 经 `call_soon_threadsafe` 进队列；解码是纯字节操作（4096 B × 25/s 的 XOR 不构成负载） |
| 重复启动 / 停止 | 端口占用、线程泄漏 | `start()` / `stop()` 幂等；`stop()` 之后不占端口、不留线程 |

**为什么 tail 只做诊断不做门禁**：如果 FTDX10 的帧尾与 FT-710 不同，把 tail 当门禁的结果是
零频谱且没有任何信息；而把它当诊断，第一帧就会在日志里说「帧能解，但尾巴不是 FT-710 那条」——
这恰好是「元数据偏移也许也不敢信」的同一句话。无真机时，日志是唯一的信息通道。
同理 `parse_scope_frame` 一律用 `require_sync=False`（它自己的 tail 校验会直接抛异常）。

## 7. 风险与未知（只有真机能定）

1. **tail 是否 `FF 01 EE 01`** —— 决定元数据偏移是否可信。
2. **元数据块偏移是否与 FT-710 一致** —— 若不一致，`scope_span` 走 CAT `SS;` 兜底（span 只接受
   FT-710 表的 `0…9`，超出写 `-1`，利用 `_on_scope_frame` 既有的 `>= 0` 门跳过）。
3. **真帧率** —— 作者固件注释说 UDP 25 fps、TCP 约 1 帧/ACK；我们的广播是 30 Hz，只要「有新帧才发」
   就自动适配。
4. **FTDX101 的 wf2 是否真有数据** —— v1 不新增双通道 UI，但解析器会给 `wf2` 值。
5. **W103D 内核有 `cdc_acm` 吗** —— Armbian 一般有；启动日志必须能看见设备出现（否则客户只看到
   「没有频谱」）。
6. **发射时 ACC 是否停流** —— FT-710 会；DX10 未知。v1 只计数，不做暂停（由固件决定是否停）。

真机到手后的 5 条验收（写进手册，客户/协助窗口可以照着做）：

1. 接上转接板 → 启动日志出现 source 与首帧（`acc_scope: source=serial /dev/ttyACM0`、fps）
2. `/WSspectrum` 是真实瀑布，不是 S-meter 合成（对比：强信号峰随天线动）
3. 扫一次 span：`SS;` 与帧内 span 一致（不一致 → §7 第 2 条兜底生不生效）
4. 取反方向对：强信号峰朝上（若朝下 = 前 850 字节没取反，与 FT-710 假设不符）
5. 发射一次：瀑布停/恢复、无 `decode_errors` 尖峰

## 8. 测试（分层，全部不需要硬件）

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_acc_scope_frames.py` | magic 重同步；帧被读边界切开；坏 XOR；`len > 4600`；payload 里出现假 magic；UDP seq/part 乱序、缺片、重复；`MAX_PAYLOAD_LEN` 边界 |
| `tests/test_acc_scope_sources.py` | 假串口（BytesIO + 注入断线）；真 `socketpair` 的 TCP（注入半包、断开、重连）；假 UDP socket（注入 `HELLO`、分片）；退避与重发用**假时钟**，不 sleep |
| `tests/test_acc_scope_producer.py` | 假 `ScopeHandler`：一帧合成数据 → 验 10 个字段 + `_frame_count`；队列丢旧帧；`start/stop` 幂等；tail 不匹配只告警不丢帧 |
| `tests/test_acc_scope_contract.py` | `MRRC_ACC_SCOPE=off` → `create_scope_producer()` 返回 `None`；串口守卫拒绝 CAT 端口；`scope_type == "acc"`；env 名与 `config.py` 一致 |

配套一个**合成帧生成器**（测试专用）：按 §2.1 的布局造 4096 字节（wf1/wf2 + 元数据 + tail）。
无真机时它是唯一的离线真值 —— 所有上游测试都建立在「这份布局是对的」之上，而这一点由
`backends/ft710/scope_frame.py` 的既有测试替我们背书（FT-710 真机验证过的解析器）。

## 9. 不做的事（YAGNI）

- 不做 Wi-Fi 配网界面（用作者固件的命令 / BOOT 键；盒子侧只连 IP）
- 不做固件、不做 PCB（P2；本设计只按 §2.3 给它留输入）
- 不做双通道（FTDX101）UI
- 不做 FT4222 **slave** 源（留一个 `ft4222` 枚举值与半天对照实验，另开一行；若成立，固件与板子全部可省）
- 不动 `scope_pipe.py`、`scu_lan/`、`spectrum_profile.py`
- 不给 FTDX10 / FTDX101 profile 打 `verified=True`（频谱有了不等于表头与模式码验证过）

## 10. 许可与来源

`SLTPLAN/FTDX10_WF` 与 `FTDX10_WF_HUB` 都**没有 LICENSE 文件**（默认「保留所有权利」）。
所以本设计取的是**协议事实**（引脚、波特率、SPI 模式、帧格式、常量），代码一句不抄，与本仓
处理 wfview（GPL-3.0）的既有做法一致 —— `backends/icom_net/protocol.py` 头注释就是这条政策的原文。
将来若要直接使用作者的固件或板子，先取得他的同意；或者直接采用他发布的板子（P1 的验证路径就是它）。

## 11. 分期

| 期 | 内容 | 能否在无硬件下完成 |
| --- | --- | --- |
| **P1（本设计）** | `acc_scope/` 三个模块 + backend 接线 + env + 操作手册 + 4 个测试模块 | **能**：可交付给作者/会动手的客户直接试 |
| P2 | 自制 ESP32-S3 板 + 自研固件（输入见 §2.3）。可加：心跳门控（没人看瀑布就让电台停流）、只发 0–849 + 元数据（带宽 1/4） | 否（要打样 + 真机） |
| P3 | FTDX101 双通道 UI；FT4222 slave 对照实验 | 否 |

## 变更记录

| 日期 | 变更 |
| --- | --- |
| 2026-11-15 | 初稿。拓扑（盒子贴电台）、只做频谱、三个源、tail 只做诊断、合成帧作为离线真值 |
