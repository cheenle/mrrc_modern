# SCU-LAN10 网络后端设计记录（B 路线）

> **状态（2026-11-15）**：设计定稿，P0+P1 已落地 —— `backends/yaesu/scu_lan/protocol.py`
> （纯字节层）、`scope_format.py`（频谱体解码）、`client.py`（七阶段会话 + 1 Hz 心跳 +
> CAT 发两遍 + 断线重建），离线测试 `tests/test_scu_lan_{protocol,client}.py` 共 43 项，
> 全套 3848 项通过。下一步 `transport.py`（serial 外观）→ `backend.py` 接线 →
> `audio.py` / `scope.py`（producer）→ `_diag_scu_lan.py`。
> **尚未在真机验证**：TX 音频内层头、FTDX101 上的频谱边界规则、CAT 隧道延迟、
> `SS` 的 span 表，都要一个协助窗口确认（§6）。
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

`SPEC.md` 是独立逆向出来的**协议事实**（偏移、公式、常量），实现全部重写；不搬那份项目的代码，
理由与 §7 处理 wfview 的方式一致：本仓库没有 GPL 声明，复用 GPL 代码需要先改许可。
5 条硬编码初始化包是线缆常量（同一句「客户端开机就这样发」），按事实使用。

## 9. 分期

1. **P0（本次）** `protocol.py` + `scope_format.py` + 离线测试。无硬件、无网络。
2. **P1** `client.py` + 假电台测试；`_diag_scu_lan.py` 出包 → 真机确认 §6 的 1–4。
3. **P2** `transport.py` + `backend.py` 接线 → CAT/表头/PTT 走网口，瀑布点亮。
4. **P3** `audio.py`（RX）+ `scope.py`（producer）→ 有声有图。
5. **P4** TX 音频；设置页与装机前提；SDD/README/tests/README 同步。
