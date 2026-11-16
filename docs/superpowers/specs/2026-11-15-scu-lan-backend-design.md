# SCU-LAN10 网络后端设计记录（B 路线）

> **状态（2026-11-15）**：P0–P3 落地，**TX 按设计拒绝**（§10.5-1 / §6-1）。
> `protocol.py`（线缆层）、`scope_format.py`（4096 B → 850 bin）、`client.py`（七阶段会话）、
> `transport.py`（serial 外观）、`scope.py`（ScopeProducer，频率轴走 `SS`/`FA`）、
> `audio.py`（16 kHz 立体声 → 48 kHz 单声道），`MRRC_SCU_LAN=1` 把它接进 `YaesuBackend`，
> 自检脚本 `_diag_scu_lan.py` 可跑真机。离线测试 180 模块 / 3914 项全绿。
> **TX 在这条路上是关的**：单元 668 字节 TX 音频帧的内层头尚未解出，按键只会得到静音载波，
> 所以 `_tx_allowed()` 在网络模式下拒绝 key-up（释放永不拦）。
> **尚未在真机验证**：TX 音频内层头、FTDX101 上的频谱边界规则（上游只有 FTDX10 数据）、
> `SS` 的 span 表与应答形状、PTT 读回 `TX;`（§6、§10.6）。
>
> 2026-11-15。起因：FTDX101MP/D、FTDX10、FT-710 的瀑布只有两种拿法 —— 背后 13-pin ACC 的
> SPI（要自己做转接板，每台一份硬件）或客户已经买下的 SCU-LAN10（网线一根）。选了后者：
> 有 SCU-LAN10 的客户当场可用，盒子侧不增加任何硬件。
> 协议事实来源：`CarrierWaveApp/sculan10-protocol` 的 `SPEC.md`（独立逆向，约 76k 抓包，
> 已由一个独立的 Swift 客户端在 FTDX10 上跑通）+ Yaesu 自己的 CAT Operation Reference。

## 1. 为什么是「换传输层」，不是新开电台家族

SCU-LAN10 改变的是**怎么跟电台说话**，不是电台本身：频段表、滤波器、模式码、表头曲线、
`SS`/`FA`/`MD` 全是 profile 的事，一行都不用动。所以新代码只做传输，形状照抄 G7250 网口的先例：

```
YaesuBackend（profile 驱动，不动）
  └── YaesuCatController（`;` 组帧、AI 过滤、PTT 抢占、重连，不动）
        └── ScuLanCatTransport   ← 新：serial 外观，背后是 UDP 隧道
```

`backends/icom_net/transport.py::NetworkCivTransport` 已经证明这个形状可行（CivController 一行
不改跑在网络隧道上），`MRRC_G7250_NET=1` 就是那个开关。本设计沿用：`MRRC_SCU_LAN=1` 把
**CAT + 音频 + 频谱**一起搬到 SCU-LAN10，默认关闭。

目录放在 `backends/yaesu/scu_lan/` 而不是顶层 `backends/scu_lan/`：这是 Yaesu 专有协议
（和 `icom_net` 属于 Icom 家族同一个道理），放家族内部让依赖方向一眼可见。

## 2. 协议事实（`protocol.py` 实现的就是这张表）

四个 UDP 口，默认基址 50000（可配 49990–50009），**全部 UDP，没有 TCP**：

| 偏移 | 端口 | 通道 | 通道 id | 方向 |
| --- | --- | --- | --- | --- |
| +0 | 50000 | CTRL | `0xFC` | 双向：认证、各通道 setup、心跳 |
| +1 | 50001 | CAT | `0xFD` | 双向：雅司 CAT 命令 |
| +2 | 50002 | AUDIO | `0xFE` | 双向：RX/TX 音频 |
| +3 | 50003 | SCOPE | `0xFA` | 电台→客户端，客户端只发心跳 |

**帧**：`5A 5A | len u16 LE | 00 01 | header[8] | body[N]`，`len = N + 10`。
**头**（除 `seq` 外每个字节都与 `key` 异或）：

| 字节 | 公式 |
| --- | --- |
| b0 | `seq`（每通道每方向独立计数，0xFF 回绕） |
| b1 | `key ^ seq ^ 0xFC` |
| b2 | `key ^ channel_id` |
| b3 | `key ^ 0x03` |
| b4 | `key ^ msg_type` |
| b5 | `key`（每包随机） |
| b6 | `key ^ (body_len & 0xFF)` |
| b7 | `key ^ (0x07 ^ (body_len >> 8))` |

**体加密**：`enc[i] = plain[i] ^ key ^ gTable[i]`，`gTable[i] = (4*(i//4) + 6 + (0, 0xFF, 0xFE, 5)[i%4]) & 0xFF`。
不是密码学，只是混淆 + 头一致性校验；头对不上就静默丢弃。

**消息类型**：`0x01` setup、`0x03` auth、`0x05` keepalive、`0x06` port-init、`0x07` data；
应答置 bit7（`0x81/0x83/0x85/0x86/0x87`）。

**会话七阶段**（全在 CTRL，除注明）：5 条硬编码初始化包（seq 01–05，body 四个零字节）→
auth（seq 6，固定 key `0x3A`，体 68 B：`01 00` + user[33] + pass[33]）→ 应答给出
`session_id` + 5 个能力字节 → continuation（`51 C3 00 96`）→ CAT/AUDIO/SCOPE 三组
port-init + setup（每组间隔 150–200 ms，打快了电台会静默漏掉）→ 四通道各一条心跳 →
第一条 CAT（`FA;`，每条 CAT 发两遍，靠 `cmd_seq` 去重）。之后 **1 Hz 心跳不可断**，
断了电台几秒内终止会话，重连要整个重来。

**CAT**：客户端体 `sid, 00 00 00, cmd_seq, 00 00 00, ASCII`；服务端体 `resp_seq, 00 00 00, ASCII`。
电台还会主动推 `RM1xxx;`–`RM6xxx;`，不是我们问的，按普通应答解析。

**音频**：RX 体 664 B = 24 B 内层头 + 640 B（160 帧 × 立体声 Int16 LE @16 kHz，10 ms 一包）。
0 通道是扬声器音频，1 通道正常 RX 时接近静音。TX 体 668 B，**内层头未解**（§6 第 1 条）。

**频谱**：4096 B 一行，≈25 行/秒；幅度**取反**（0 = 最强，255 = 最弱）；有效区间由
「从第 500 个 bin 起第一个 `<= 1` 的值」标定，`usable = marker // 2`（规范自己标注这是
实测出来的经验规则，原因未解）。

### 2.1 规范里的一处笔误（已用参考向量证伪）

`SPEC.md` § Phase 1 说那 5 条硬编码初始化包「解密后是 `[0x04,0x04,0x04,0x04]`」。按它自己
的公式和 gTable 解出来是 `00 00 00 00`——与 § Keepalives 写的「payload = 四个 00」一致，
也与我自己按公式验算的结果一致（第 1 条：key=`0x5D`，`5B^5D^06=00`）。`tests/test_scu_lan_protocol.py`
把「解出四个零字节」当黄金断言，免得后来者照抄那句话。

## 3. 目录与职责

| 文件 | 职责 | 规模 |
| --- | --- | --- |
| `protocol.py` | 帧/头/gTable/通道/消息类型/auth/CAT 负载/序号计数器。**无 socket、无状态** | ~250 行 |
| `scope_format.py` | 4096 B 频谱体 → 850 bin 行（找边界、取反、重采样） | ~120 行 |
| `client.py` | 七阶段会话、四条 UDP 流、1 Hz 心跳、CAT 收发与去重、主动 `RM*` 分拣、重连 | ~600 行 |
| `transport.py` | serial 外观（`open/read/write/in_waiting/reset_input_buffer/close`） | ~150 行 |
| `audio.py` | 16 kHz 立体声 → 单声道 → 48 kHz（soxr），有界队列，TX 反向重采样 | ~300 行 |
| `scope.py` | `ScuScopeProducer`：解 4096 体 → `ScopeHandler` 的 850 bin + 起频 + span | ~200 行 |

`backend.py` 的改动只有三处：`capabilities` 里 `scope_type="scu_lan"`、
`create_audio_handler()` 在网络模式下返回 `ScuLanAudioHandler`、`init_scope()` 启动 producer。
一个开关 `MRRC_SCU_LAN=1` + `MRRC_SCU_LAN_HOST/_USER/_PASS` 同时点亮 FTDX101D、
FTDX101MP、FTDX10、FT-710 四台（profile 共享）。

## 4. 频谱：和 FT-710 相同的是格式，不同的是分槽

相同的照用：4096 B、每字节取反、25 行/秒 —— `spectrum_profile.py` 的三档带宽、30 Hz 按帧号
推进的调度、WS 广播、前端渲染全都不动。

不同的必须新写：**没有固定的 850+850 分槽**（那是 FT-710 的帧布局）。这里 `usable = marker // 2`，
然后重采样到 850 —— 和 IC-7300 的 475→850 是同一种处理，`upsample_bins()` 复用同一份实现，
避免第二个重采样器漂移。频率轴不从帧里猜：`scope_span` 与 `scope_mode` 走 CAT 的 `SS;`
（`P2=5` 取 span、`P2=6` 取 mode），`scope_start_freq` 由 `FA;` + span 推。
顺带解锁 FT-710 上做不到的事：FTDX101 的 `SS P1=0/1` 分 MAIN/SUB，**双瀑布有数据来源**。

## 5. 安全与运维

- **凭证**：SCU-LAN10 的 user/pass 只进 env 文件 / 运行期存储，日志与支持包一律脱敏
  （与 Wi-Fi PSK 同一套规则，NFR-068）。
- **会话独占**：同一时刻只允许一个客户端。被雅司 FT-Control 顶掉时，`client.py` 必须在
  日志里点名（「SCU-LAN10 会话被其他客户端接管」），否则现场只会看到「盒子突然不动了」。
- **不要暴露到公网**：协议没有认证、没有抗重放、没有加密。远程一律走 hub 隧道。
- **IPv4 only**：所有观察到的抓包都没有 IPv6；我们的云入口是 IPv6，出口到 LAN 的这段必须
  显式用 IPv4。
- 装机前提（要写进设置页）：电台菜单 **CAT RATE = 38400**、CAT TIMEOUT 10 ms、CAT RTS ON。

## 6. 风险与未知（只有真机能定）

1. **TX 音频内层头未解**（668 B 体）。这是 B 路线唯一的硬伤：不能发话，这个后端只值一半。
   处方：抓一次 `FT-Control` 的 TX（Wireshark），按 RX 的 24 B 头反推，并在
   `_diag_scu_lan.py --tx` 里逐字节打印我们发出的包与 RX 包的差。
2. **频谱边界规则只在 FTDX10 上验证过**。FTDX101 的幅度编码、marker 位置、是否 `//2` 都要实测；
   `_diag_scu_lan.py --spans` 会逐档设置 `SS` span 并打印每档的 marker 位置与可用 bin 数。
3. **CAT 隧道延迟**能否扛住 100 ms 轮询 + PTT 抢占（UDP 隧道，每条 CAT 发两遍）。
4. **`SS` 的 span 表**（`0..9` → 1k…1M）只从 OCR 的日文手册里读到，值域有歧义；实测后写进
   `yaesu_profiles.py` 并标 provenance。
5. **能力字节**（`01 25 01 3F 51`）含义未知 —— 只当常量回显，不解释。

## 7. 验收（分层，每层都能独立跑）

| 层 | 判据 |
| --- | --- |
| 字节层（离线） | 5 条初始化包解出四个零字节；gTable 256 项与公开表一致；篡改头任一字节 → 丢包；auth 体 68 B；CAT 体往返 |
| 会话层（离线） | 假电台脚本跑完七阶段 → 收到 scope/audio；心跳断 3 s → 判定掉线并重连 |
| 真机（`_diag_scu_lan.py`） | 握手、`FA;`/`ID;` 回应、20 帧频谱存 CSV/PNG、RX 存 WAV、TX 实测、往返延迟 |
| 服务器 | `/api/state` 频率/模式/S 表正确、瀑布流动、RX 有声、PTT+TUNE 通、录音/FT8 不解体 |

## 8. 许可

**更正（2026-11-15 核对仓库本体）**：`CarrierWaveApp/sculan10-protocol` 是 **MIT** 许可
（`LICENSE`：MIT License, Copyright (c) 2026 The Carrier Wave team），不是 GPL —— 也就是说
复用它的文字与代码都不需要本仓库改许可。原设计里「只取事实不搬代码」的约束**不再成立**；
本次实现仍然是独立写的（`protocol.py` 按公式重述、`scope_format.py` 按规则重写），
但以后要引用它的参考代码片段（SPEC §Reference Code）没有任何许可障碍。

仓库内容只有三个文件：`LICENSE`、`README.md`、`SPEC.md`（没有客户端实现），已按 MIT 原样收进 `docs/vendor/sculan10-protocol/` 作为实现依据（`docs/` 不进安装包）。README 声明的
客户端是作者自己的 macOS 应用「CW Sweep」，据其所述已在生产环境用这份协议跑通 FTDX10 的
认证、频谱、音频与 CAT 隧道。

## 9. 分期

1. **P0（本次）** `protocol.py` + `scope_format.py` + 离线测试。无硬件、无网络。
2. **P1** `client.py` + 假电台测试；`_diag_scu_lan.py` 出包 → 真机确认 §6 的 1–4。
3. **P2**（**已落地**）`transport.py` + `cat_core` 传输注入 + `backend.py` 接线 →
   CAT/表头走网口；PTT 读回确认是 TX 的前置，目前 key-up 被拒。
4. **P3**（**已落地**）`audio.py`（RX 16k 立体声 → 48k 单声道）+ `scope.py`（producer，
   频率轴来自 `SS`/`FA`，`keeps_running()` 保证关网页不会掐断会话）+ `_diag_scu_lan.py`。
5. **P4**（待）TX 音频（需一次 `FT-Control` 抓包）；PTT 读回 + 换 `cmd_seq` 重发；
   设置页与装机前提；SDD/README 同步。

## 10. 网络路径复核（2026-11-15，实施前）

「盒子走网络跟 SCU-LAN10 说话」这句话本身要先证明，否则后面全是白做。逐段核。

### 10.1 谁连谁

盒子是**客户端**：主动向单位的 50000–50003 各发 UDP，四个口分别走自己的源端口；
单位按「上次收到包的源 IP:端口」回包，所以只要**盒子里到单位的出向**可达 ——
不做端口映射、不需要任何入向规则、NAT 后面照样能穿。

已核：

- 盒子镜像**没有任何防火墙规则**（`packaging/`、`linux/` 里搜不到 nft/iptables/ufw），
  出向 UDP 不受限；
- 客户端只绑 **IPv4**（`AF_INET`）——单位不支持 IPv6（规范：全部抓包无 IPv6）；
- 电台的 USB 口由单位持有，盒子既不打开串口也不打开声卡 ⇒ 「盒子插 USB」与
  「盒子走网络」是**两条互斥的接线**（同一台电台只有一个 USB 口）。

### 10.2 一条链路的四段，各归谁

| 段 | 载体 | 谁保证 |
| --- | --- | --- |
| 盒子 → 单位 | 以太网或 Wi-Fi，UDP 50000–50003 | 客户的路由；盒子出向无限制 |
| 单位 → 电台 | 13-pin ACC + USB（FT-710 只有 USB + 外接电源） | 雅司原厂 |
| 电台 CAT | 电台侧 38400 8N1，单位到 LAN 115200 | **装机前提**：CAT RATE=38400 / TIMEOUT 10 ms / RTS ON |
| 电台音频 | 电台内置 USB 声卡 → 单位采样 16 kHz | 单位固定（PCM 16 kHz 立体声） |

### 10.3 带宽与抖动（按规范的实测值算，不是估）

- 频谱 4096+14 B × 25/s ≈ **103 KB/s ≈ 822 kbit/s**
- 音频 664+14 B × 100/s ≈ **68 KB/s ≈ 542 kbit/s**
- CAT + 心跳 ≈ 10 kbit/s

合计 **≈ 1.4 Mbit/s 持续下行到盒子**，而盒子同时还要把 Opus 与频谱转发给浏览器/App。
W103D 的 MT7663S 是 1×1 2.4 GHz —— 这是这条路上最弱的一环。
⇒ 用 SCU-LAN10 时装机建议走**网线**；出问题先查这一段，而不是先查代码。

### 10.4 延迟

CAT 单条往返 = LAN 往返（<5 ms）+ 电台内部；单位到电台是 115200（比串口 38400 更快），
隧道本身不排队 ⇒ 与我们现有 100 ms 轮询同一量级，不会把轮询节奏打乱。
音频单向 ≈ 10 ms 包 + 单位 100 ms 缓冲 + 我们的 Opus/Worklet 抖动缓冲 ≈ 150–250 ms；
SSB 可接受，FT8 不受影响（时隙按墙钟、RX tap 有 0.5 s 重锚）。

### 10.5 六个必须由实现守住的失效模式（分析的主要产出）

1. **发不出去 = 按住的发射机（最危险）**：`TX0;` 可能丢包。而单位按 `cmd_seq` 去重，
   **字节相同的重发会被当成重复丢掉** ⇒ 重发必须换新的 `cmd_seq`。
   方案：释放后读回 `TX;`，未确认为 RX 就换新 `cmd_seq` 重发，最多 N 次；仍失败 → 断开 +
   Force-RX，并依赖电台自己的 **TX TIME OUT**（另一条装机前提）。
2. **心跳丢失 = 会话终止**：单位几秒收不到心跳就终止，且没有恢复机制。方案：心跳 2 Hz
   （规范说 ≈1 s，加密只会有益）、断线即全量七阶段重握手、重连后必须重跑状态同步与
   `init_scope()`（沿用 `poll_scheduler` 的恢复路径，不要另起一套）。
3. **会话被顶替**：雅司 FT-Control 一连上，我们这条就停。方案：掉线时日志点名
   「SCU-LAN10 会话被其他客户端接管」，并反映到 UI 状态，不许表现为「盒子卡住」。
4. **`in_waiting` 饿死**：`cat_core._read_until` 每 10 ms 看一次 `in_waiting`、总超时 1 s。
   传输层必须**整帧及时投递**，否则命令表现为「电台不回话」而这种症状会被误判成串口问题。
5. **ack 当数据**：`0x86` port-init 的 ack 落在数据口上，4 个字节会被画成一条频谱行。
   已按消息类型分流并测试盯住（`_dispatch` / `_on_ack`）。
6. **异常分类错位**：`cat_core._is_device_fatal` 把 `serial.SerialException` 判成
   「设备丢失」（会锁 `serial_connected=False`），把 `SerialTimeoutException` 判成瞬时。
   网络模式下会话断开是**可自愈**的 ⇒ 传输层抛 `SerialTimeoutException`（瞬时），
   只有「这个对象本身不可用」才抛 `SerialException`。

### 10.6 判定

**OK，有条件。** 网络路径本身没有拦路虎：方向正确（盒子出向）、无防火墙、带宽 1.4 Mbit/s、
延迟与串口同量级。真正的风险不在「网络能不能通」，而在两处：

- **发射安全**（10.5-1）—— 必须有读回确认 + 换 `cmd_seq` 重发 + 电台 TX TIME OUT 兜底；
- **TX 音频未解**（§6-1）—— 决定「能不能发话」，与网络无关。

因此实施顺序调整为：**传输层 + CAT（先把只读与 PTT 读回做对）→ 频谱 → 音频 → TX**，
而不是先做频谱。P2 开工前必须满足的清单就是 10.5 的六条 + 10.2 的四条装机前提。

## 11. 与上游规范的逐条对照（2026-11-15）

逐项核 `SPEC.md`（`CarrierWaveApp/sculan10-protocol`，MIT）与我们的实现。结论：
**三处不一致，两处是我们错、一处是规范自相矛盾**，都已处理。

| # | 上游条目 | 我们的实现 | 结论 |
| --- | --- | --- | --- |
| 1 | 帧 `5A5A / len LE / 00 01 / header[8] / body`，`len = body+10` | `protocol.frame` / `parse_wire` | 一致 |
| 2 | 头 b1=`key^seq^FC`、b3=`key^03`、b6/b7 长度键 | `build_header` | 一致（篡改用例逐字节） |
| 3 | `gTable[i] = (4*(i//4)+6+(0,FF,FE,5)[i%4])&FF` | `gtable()` | 一致，且与公开的 256 项逐字节比对 |
| 4 | auth：seq 06、key `0x3A`、体 `01 00`+user[33]+pass[33] | `build_auth` | 一致（68 字节断言） |
| 5 | Phase 4 continuation `51 C3 00 96`、b2 = **CAT** | `_handshake` | 一致 |
| 6 | Phase 5 三组 port-init + setup，setup 的 b2 指的是**下一个**通道 | `_handshake` 的 `steps` | 一致 |
| 7 | 各 setup 之间 150–200 ms | `SETUP_STEP_S = 0.18` | 一致 |
| 8 | 心跳体四个零字节、四通道各一条 | `_send_keepalives` | 一致（频率取 2 Hz，见 §10.5-2） |
| 9 | CAT 体 `sid,000,cmd_seq,000,ASCII`，**发两遍同 cmd_seq** | `send_cat` | 一致 |
| 10 | **两遍的线缆头序号各自递增** | —— | **我们错了**：原来发的是两份相同字节。已改成两次组帧（新序号、同 `cmd_seq`），并加测试 |
| 11 | **CTRL 计数器跨过 init(01–05)/auth(06)，post-auth 从 07 继续** | —— | **我们错了**：原来 auth 后从 01 重开，等于重放单位见过的 id（它按「见过就丢」处理）。已改成 auth 后 `reset(0x07)`，并加测试 |
| 12 | Phase 1 五条初始化包「解密后是 `04 04 04 04`」 | 实测解出 `00 00 00 00` | **规范自相矛盾**：按它自己的公式与 §Keepalives（四个 00）都对不上那句话。测试钉字节 |
| 13 | 音频 RX 664 B = 24 B 内层头 + 640 B（160 帧立体声 16 kHz） | `_audio_payload` 剥头 | 一致（立体声→单声道在 audio 适配层，P3） |
| 14 | 频谱 4096 B、幅度取反、边界 marker、`usable = marker//2` | `scope_format` | 一致 |
| 15 | 会话独占、断心跳数秒终止、重连走全流程 | `_session_loop` | 一致 |
| 16 | 端口 base+0..3，范围 49990–50009 | `MRRC_SCU_LAN_PORT` | 一致 |
| 17 | 仅 IPv4、回包到源地址 | `family=AF_INET` | 一致 |

上游**没有**覆盖、因此只能靠真机的一条都没有变（§6）：TX 音频内层头、`//2` 规则的成因、
能力字节 `01 25 01 3F 51` 的含义、以及 **FTDX101 从未被验证**（作者全部抓包来自 FTDX10，
README 明写「Captures from FTDX101MP/D or FT-710 welcome」）。我们客户的主力机型恰好是
FTDX101MP —— 那台机器的频谱编码是否与 FTDX10 同构，是这条路上最大的未知。

## 12. 实现复核（2026-11-15，第二次深度审查）

P0–P3 落地后再从头读一遍代码并跑了针对性实验。结论：**离线能证明的都证明了，剩下的
全部压在真机行为上**。下面按"已修 / 待修 / 已验证"分开写。

### 12.1 这一轮修掉的两个

| # | 问题 | 修法 |
| --- | --- | --- |
| 1 | **任何来源的包都会被接受**：协议本身没有认证，局域网里任何设备往我们的临时端口发一个 `ACK_AUTH` 就能把 `_command_done` 置起来，伪造一次"握手成功" | `client._from_peer()`：第一包学到单位的 IP，之后只认这个 IP，其它计数（`foreign_packets`）并按 100 条一次告警。用例覆盖「伪造 ACK 无法完成握手」 |
| 2 | **CAT 静默时几何刷新会抢锁**：`_refresh_geometry` 三条查询各 0.4 s 上限，电台不回话时每 2 秒占住 1.2 秒的 CAT 锁 —— 100 ms 的状态轮询会被挤成卡顿 | 一轮三条全部落空就退到 3 倍间隔（恢复后回正常），返回作答条数给循环判断 |

### 12.2 待修 / 真机才能定（按严重度）

| # | 项 | 为什么现在不动 |
| --- | --- | --- |
| A | **`send_cat_threadsafe` 乐观返回 True**：`call_soon_threadsafe` 只是排队，真正执行时若会话已断，`send_cat` 返回 False 而没人看见 —— 命令静默消失 | 对轮询无害（下一次 100 ms 就补上）。但**打开 TX 之前必须改成带回执**：`TX0;` 的重发依赖的正是这条路径，而"以为发了其实没发"在发射释放上是硬伤 |
| B | `link_timeout = 12 s` 偏长：单位静默后最多 12 秒仍显示"已连接" | 不敢再压：上游明写「数据通道的心跳**不一定会 ack**」，`_last_inbound` 只靠 CTRL 的 ack 就够短了；压到 5 s 在丢包链路上会造成重连风暴。真机跑过再定 |
| C | `SS_SPAN_HZ` 只有 0..8 九档 | 手册的 OCR 有歧义；code 9（1 MHz）现在按"未知"处理、轴保持旧值（不画编造刻度）。`_diag_scu_lan.py --spans` 会打出真表 |
| D | `/api/box/scu-lan/test` 让已登录用户可以让盒子对任意 `host:port` 做一次握手 | 与 `/api/setup` 的 `sunsdr_host` 同级：登录后可达的局域网探针。记在这里，不额外收口 |
| E | 主界面不显示链路状态（已连/会话号/被顶掉） | 只在 `/settings` 卡片与支持包里可见；要做得改 `fullState` + 前端 |
| F | TX 全关、桌面版无设置入口、零真机验证 | 见 §6 / §9 |

### 12.3 这一轮**验证掉**的（离线能做到的极限）

新增 `tests/test_scu_lan_catcore.py`：把**真的** `YaesuCatController` 接在隧道上跑
（写路径、10 ms `in_waiting` 读循环、AI 帧前缀过滤都是服务器上那份代码）：

- `query("FA")` 走完 线程 → 事件循环 → UDP → 单位 → 线程 的整条路并拿到 `FA007007000`；
- 单位每次答完 `FA` 都会**主动推一条 `RM1;…;`**（假单元就是这么写的），它不会被当成频率；
- 单位消失后 `query` 返回 None、**不抛异常、不锁 `_connected`** —— 也就是不会把一次
  网络抖动报成"USB 被拔了"；
- 会话恢复后**不需要重新打开任何东西**，下一次查询就通了。

另外两组实测（本机）：

| 项 | 实测 | 结论 |
| --- | --- | --- |
| 频谱解码 | **0.15 ms/帧** → 25 fps ≈ **0.4% 单核** | W103D 上不是 CPU 问题 |
| 音频转换（16k 立体声→48k 单声道，10 ms 包） | **0.011 ms/包** → 100 pps ≈ **0.1% 单核** | 同上 |

所以这条链路在本机只有两个真正的变量：**网络（1.4 Mbit/s + 抖动）与电台行为**。

## 13. 真机验收（2026-11-16，FTDX101MP + SCU-LAN10，BG7ZHS 的盒子）

第一次上真机。**握手成功但三条数据通道全静默**，两个缺陷串在一起，各自都"静默"：

| # | 缺陷 | 现场表现 | 修法 |
| --- | --- | --- | --- |
| 1 | **序列计数器按通道号（`b2`）计，而不是按端口计** | 会话"已连接"、`reconnects: 0`，但 `cat_frames` / `scope_packets` / `audio_packets` 全为 0，瀑布只发出去 1 帧（`session_metrics.spectrum.frames = 1`）。日志里只有 auth 成功那一行 —— 没有任何错误 | `_sequence` 改成按**发送端口**（`ctrl`/`cat`/`audio`/`scope`）计数 |
| 2 | **服务器→客户端的数据帧被当成 ack 丢掉** | 修掉 1 之后数据到了（探针在 6 s 内收到 1291 个数据报），但客户端计数器仍是 0 | `_dispatch` 的判定从 `!= MSG_DATA` 改成 `not in (MSG_DATA, MSG_ACK_DATA)` |

机理（缺陷 1）值得写下来：`b2` 里的通道号指的是**正在被配置的下一个通道**（规范原文的
quirk），而单位校验的计数器属于**数据报到达的那个端口**。两条 setup 包走 CTRL，
却按 CAT/AUDIO/SCOPE 的计数器取了号，于是三个 port-init 全部以 `seq 2` 发出（应为 `1`），
单位静默丢弃；而 CTRL 上的 auth 与 keepalive ack 让链路一直"活着"——**盒子报告已连接、
电台其实一条数据都没给**。规范其实写清了：post-auth CTRL 从 `07` 继续，data 端口的
第一包（port-init）就是该端口的 `01`。

机理（缺陷 2）：单位发出的每个报文都置方向位 `0x80`，所以数据是 `0x87`
（`MSG_ACK_DATA`）而不是客户端的 `0x07`。`_on_ack` 对不认识的 ack 只写一条 debug，
因此这个缺陷在任何日志里都不留痕迹。

真机测得的事实（这台 FTDX101MP，MAIN 侧）：

| 项 | 实测 |
| --- | --- |
| `ID;` | `ID0682;`（FTDX101MP 的机型码，与 profile 一致） |
| CAT 往返 | 中位 **9 ms**（7–19 ms），100 ms 轮询无压力 |
| `SS05;`（span） | `SS0560000;` → P3=6，命中 `SS_SPAN_HZ[6]` = 200 kHz |
| `SS06;`（mode） | `SS0650000;` → P3=5，属"VFO 在扫描中心"集合 |
| 频谱体 | 4096 B × **25.0/s**，边界标记在 **bin 1700 → 850 bin**（`marker // 2` 规则在 FTDX10 之外的机型上**成立**） |
| 音频 | 640 B/包 × **100.0/s** = 160 立体声帧 = 16 kHz 10 ms，peak 6790 / rms 1500 |
| 不请自来的 CAT | `RM0100004;` / `RM0097004;` 每帧都推（`RM` = 表头），被 `cat_ascii` 正常当文本交给 CAT 核 |

**TX 仍然关着**（单元 668 B TX 音频帧的内层头未解，按键只会有静音载波）：这次真机没有
改变这一点，`MRRC_SCU_LAN=1` 时 key-up 依旧被拒绝、释放永不拦。

## 14. 真机验收 2：没有声音（2026-11-16 下午，同一台 FTDX101MP）

§13 修完，瀑布和 CAT 都活了，但**仍然一点声音都没有**。这一次的原因和编解码无关，是
一个**启动期死锁**：

| 段 | 事实 |
| --- | --- |
| 服务端 | `audio_available: true`、`_rx_running: true`、`audio_rx` 上行 **0 字节**、`/api/audio/levels` 的 `rx.samples` **0**、我自己的 `/WSaudioRX` 订阅 6 秒 **0 帧** |
| 客户端 | `audio_packets: 154850`、`audio_age_s: 0.01` —— 单元的音频**一直在到** |
| 日志 | `16:50:26 start_rx … connected=False` → `16:50:34 [WARNING] Audio RX init timed out (8s)`。`RX audio broadcast active` / `RX capture stalled` / `Audio loop idle` 三条标记在整个进程里**各 0 次** |
| 离线 | 同一个 handler 单独跑：302 包进 / 150 块出 / peak 3578 / Opus 134 B —— 转换没问题 |

机理：`server.py` 的 lifespan 把 `audio.start_rx` 丢进 worker 线程，然后**在事件循环线程上**
`_audio_fut.result(timeout=8)` 阻塞等它（这条结构是给「open 会慢/会挂」的声卡写的）。
而 `ScuLanAudioHandler.start_rx` 在 `client.connected` 还是 False 时（启动瞬间，CAT 的会话
才起来）会 `run_coroutine_threadsafe(...).result(15)` —— **等那个正被自己堵住的循环**。
两边互等 → 8 秒超时 → `_audio_ok = False` → `_audio_rx_loop` **根本没被创建**。于是没有
广播、没有 stall 告警、也没有任何计数器能解释——只有一句把人引向 USB 声卡的超时文案。

修法三处（`tests/test_scu_lan_scope_audio.py::StartupDeadlockTests` 盯住）：

1. `ScuLanAudioHandler.start_rx`（以及 `backends/icom_net/audio.py` 的同款）**永不等循环**：
   置 `_rx_running` 后把「确保会话起来」**发出去就返回**（`_ensure_session()`），会话生命周期
   本来就归 CAT 路径。
2. `ScuLanClient.start()` 加 `_starting` 闩：CAT 与音频适配器会在同一个启动里各调一次，而
   第一次调用要 `await` 四次 `create_datagram_endpoint` 之后才有 `_tasks`；只看任务表的
   第二个调用会绑第二套 socket 并覆盖 `_sockets`（一套会话两套端口）。用例断言绑了
   **4 个**流而不是 8 个。
3. `server.py` 改用 `await asyncio.wait_for(asyncio.wrap_future(_audio_fut), 8)`：循环不再
   被阻塞，于是任何需要循环的 handler 都不可能再死锁；超时文案也改成点名 handler 类型
   并说明「这次重启内没有 RX 音频」，而不是断言是 USB 声卡。

教训（值得写在这里）：`audio_available` 只证明 `_rx_running` 被置过，**不能**证明采集循环
存在；判断「有没有音频」要看 `session_metrics.uplink_bytes_total.audio_rx` 或自己订阅
`/WSaudioRX` 数帧，而不是信那个布尔值。
