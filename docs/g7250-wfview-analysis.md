# G7250 的网口到底是什么：wfview 现场分析（2026-11-12）

> 来源：BG7YQV 的 Windows 机器上装了 **wfview 2.24**（`C:\Program Files\wfview`），
> 用它对这台 G7250 **有频谱、有声音**。分析在远程协助会话里完成（管理员 shell，
> `pktmon` 抓包 + 读 wfview 自己的日志与厂商 rig 文件）。
> 这份记录推翻的是我们此前基于第三方逆向的整条设计前提（`docs/superpowers/specs/2026-11-07-xiegu-g7250-design.md` §D3）。

## 1. wfview 的日志说明了什么

`%TEMP%\wfview-20261112114450.log`：

```
INF udp: Starting icomUdpHandler user: "G7250"  rx latency: 200  tx latency: 200
         rx sample rate: 16000  rx codec: 4   tx sample rate: 16000  tx codec: 4
INF udp: UDP Stream bound to local port: 62825  remote port: 50001
INF system: "Skipping automatic CIV, using user-supplied value of 0xa0"
INF udp: icomUdpHandler : Received I am here from:  "::ffff:192.168.110.199"
INF udp: icomUdpHandler : Received I am ready
INF udp: icomUdpHandler : Sending login packet
INF udp: Got connection type: "WFVIEW"
INF udp: icomUdpHandler : Received matching token response to our request
INF udp: "Received radio capabilities, Name: IC-7300, Audio: ICOM_VAUDIO, CIV: a0,
         MAC: 00:00:00:00:00:00 CAPF: 5001"
INF udp: UDP Stream bound to local port: 62826  remote port: 50002     ← CI-V 隧道
INF udp: UDP Stream bound to local port: 62827  remote port: 50003     ← 音频
INF rig: "Loading Rig: G7250 from C:/Program Files/wfview/rigs/G7250.rig"
```

**结论：G7250 的网口是 Icom 的「网络电台」协议（RS-BA1 那套），不是裸 IQ 流。**

| 端口 | 用途 |
| --- | --- |
| UDP 50001 | 控制/发现：电台主动广播 `I am here` → 登录包 → token → capabilities |
| UDP 50002 | CI-V 隧道（CAT 帧透传，含频谱 `0x27`） |
| UDP 50003 | 音频（16 kHz，codec 4 = 16-bit LPCM） |

- 电台在 capabilities 里**自称 IC-7300**，CIV 地址 **0xA0**，`CAPF: 5001`。
- 因此「50002 收 14 字节调谐帧、50001 推 768 字节 IQ」那套（第三方逆向）**与本机固件不符**：
  我们从客户机上用 8 种帧变体打 50001/50002、又听了 50000/50003/50004，**零响应**；
  而 wfview 用的是完全不同的握手（它先收到电台的 `I am here`）。

## 2. 厂商 rig 文件：`docs/wfview-g7250.rig`（62,315 B，随仓库留存）

wfview 的机型文件是**厂商提供的权威表**（`Version=2.10`）。要点：

```
CIVAddress=160            ; 0xA0（网口的 CI-V 地址；USB 口我们实测应答 0xA4）
SpectrumSeqMax=11
SpectrumAmpMax=160
SpectrumLenMax=475        ; ← 与 IC-7300 的 0x27 完全一致（475 点 / 11 段）
HasSpectrum=true          ; ← 这台电台**有** CI-V 频谱
Memories=101, MemStart=1
HasTransmit=true
```

命令表（`Commands\N\`）逐条给出了 CI-V 形状，全部是**标准 Icom**：

| 功能 | 命令 | 取值 |
| --- | --- | --- |
| 频率 set/get | `05` / `03` | — |
| 模式 set/get | `06` / `04` | — |
| 记忆信道 | `08` | 1…101 |
| Split | `0F` | 0…1 |
| 衰减器 | `11` | 0…20 |
| 前置放大 | `16 02` | 0…2 |
| AF / RF 增益 | `14 01` / `14 02` | 0…255 |
| S 表 | `15 02` | 0…255 |
| 功率 / SWR / ALC | `15 11` / `15 12` / `15 13` | 0…255 |
| **Vd / Id** | `15 15` / `15 16` | 0…255 |
| NB / NR / **AN** | `16 22` / `16 40` / `16 41` | 0…1 |

## 3. 我们 profile 里被这份表证伪的六条

`backends/xiegu/xiegu_profiles.py` 现在的写法 → 实际：

1. **「没有 CI-V 0x27」→ 错。** `HasSpectrum=true`，475 点 / 11 段 / amp 160，正是 IC-7300 的形状；
   wfview 就是靠 CI-V 0x27 画瀑布的。⇒ `scope_type` 应是 `civ27`（复用我们已有的
   `civ_scope.py` + `CivScopeProducer`），而不是 `lan_iq`。
2. **`has_vd_id_meters=False` → 错。** `15 15` / `15 16` 都在。
3. **`has_auto_notch=False` → 错。** `16 41` 在。
4. **CI-V 地址候选 `0xA4/0x88/0x70`** 少了网口的 `0xA0`；USB 口实测应答 `0xA4`，两者并存。
5. **`scope_on` 从轮询表删掉** 的前提（「没有 0x27」）不成立 ⇒ 应恢复。
6. **`set_scope_on/span/speed/mode` 返回 False** 同样不成立 ⇒ 交由共享的 CI-V 频谱实现。

## 4. 音频

- 网口音频：16 kHz、codec 4（LPCM）在 UDP 50003，前面还有登录/token/keepalive。
  实现它等于实现一个**精简版 RS-BA1 客户端**（握手 + CI-V 隧道 + 音频流）。
- USB 内置声卡那条路我们已有；客户机器上那一路采集三天都是 `rms=2`（见
  `docs/G7250_USER_GUIDE.md` §1 的现场注记），wfview 的日志里也有
  `CRT audio: Input PortAudio open input failed` —— 两边都指向「USB 声音这条路在这台机器上不可用」。

## 5. 三条可选的下一步（按代价排序）

| 方案 | 代价 | 能得到什么 |
| --- | --- | --- |
| **A. profile 改 `civ27`** | 很小（profile + 能力位 + 两条测试） | **真频谱**（USB CI-V，已验证 CAT 通）；音频仍取决于 USB 声卡 |
| **B. 实现 Icom 网络音频（50003）** | 中大（握手 + 音频解码 + 重连） | 网口一根线同时给频谱与声音，彻底绕开 USB 声卡 |
| **C. 完整 RS-BA1 客户端**（控制+CI-V 隧道+音频） | 大 | 与 wfview 等价的能力；也支持别的 Icom 网络机型 |

A 的前提是**一次真机确认**：USB CI-V 口也答 `0x27`（wfview 走的是网口隧道，
严格说只能证明「网口这一侧有 0x27」）。最省的确认方式：让客户把 wfview 的 rig
连接方式从 Network 改成 **USB/Serial（COM3, 115200, 0xA4）**，看瀑布还在不在。

## 6. 现场已验证到的排除项（同一会话，供后续对照）

- 客户机 `192.168.110.50/24`（WLAN），网关 `192.168.110.1`，ping 电台 2–9 ms；
- **Windows 防火墙三个配置全部关闭**（不是防火墙问题）；
- MRRC 的 socket 绑在 `0.0.0.0:50001`；`pktmon` 抓到那 14 字节确实出网卡
  （`192.168.110.50.50001 > 192.168.110.199.50002 UDP length 14`）；
- 电台 MAC `54-6C-50-AB-52-1D` = **WCH 南京沁恒**（CH9121 这类串口↔网口桥的 OUI）；
- 电台不回答任何我们发的 UDP（8 种帧变体、50000/50003/50004 各听 10 秒、50000 单播+广播探测），
  也没有任何 TCP 端口开放 —— **因为它等的是 Icom 那套握手，而不是一句调谐帧。**
