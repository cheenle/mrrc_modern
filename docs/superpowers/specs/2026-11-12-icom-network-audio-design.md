# G7250 网口音频（Icom 网络协议 / RS-BA1）设计记录

> **状态（2026-11-12 晚）**：§4 的最小集已经落地 —— `backends/icom_net/{protocol,client,audio}.py`
> 与 `G7250Backend.create_audio_handler()` 的接线，离线测试 30 项（协议逐字段、假电台握手→音频、
> 16k→48k 频率保持）。**尚未在真机验证**：握手包序、`rx codec=4` 是否真是 16-bit PCM、
> 电台是否按我们申报的端口推流，都要下一个协助窗口确认（§6 第 2 步）。
> 开关默认关闭（`MRRC_G7250_NET_AUDIO=1` 才启用）。
>
> 2026-11-12。起因：BG7YQV 的 G7250 用 wfview 有声音，用我们的软件没有；而那台机器的
> 内置 USB 声卡采集三天都是 `rms=2`（≈ −70 dBFS），所以音频只能走网口。
> 协议事实来源：wfview 的 Icom UDP 实现（GPL-3.0，公开源码）——**只取协议事实
> （字段偏移、类型码、握手顺序），不搬代码**，因为本仓库没有 GPL 许可证声明；
> 真要复用 wfview 的代码，本仓库得先改成 GPL-3.0 兼容。
> 现场证据：`docs/g7250-wfview-analysis.md`（wfview 2.24 在客户机上的日志 + 厂商 rig 文件）。

## 1. 三条 UDP 流

| 端口 | 角色 | 谁发起 |
| --- | --- | --- |
| **50001** | 控制：发现、登录、token、能力、连接状态 | PC → 电台（电台回同一条连接） |
| 50002 起 | **CI-V 隧道**（CAT 帧透传，也承载 `0x27` 频谱） | 端口由电台在 status 包里告知 |
| 50003 起 | **音频**（16 kHz，16-bit PCM） | 同上 |

PC 侧三条流各自绑定**临时本地端口**，然后把自己的端口写进「请求串流」包告诉电台；
电台在 status 包里回它自己那一对端口。

## 2. 包结构（字节偏移，单位 byte）

通用头部（**前 16 字节一律小端**）：`len:u32 @0x00`、`type:u16 @0x04`、`seq:u16 @0x06`、
`sentid:u32 @0x08`（我方随机 id）、`rcvdid:u32 @0x0c`（对方 id，由对方包里的 `sentid` 学到）。
后续字段标注了 `BE` 的按大端。

| 包 | `len`/`type` | 关键字段 |
| --- | --- | --- |
| **控制** | 0x10 / `0x03`=你是不是在、`0x04`=我在、`0x06`=我准备好了、`0x00`=idle、`0x05`=… | 无 |
| **ping** | 0x15 / `0x07`，`@0x10 reply:u8`，`@0x11 time:u32` | 500 ms 一次 |
| **登录** | 0x80 / `0x01` | `payloadsize=len-0x10` BE@0x10、`requestreply=1`@0x14、`requesttype=0`@0x15、`innerseq` BE@0x16、`tokrequest`@0x1a、`token`@0x1c、`username[16]`@0x40、`password[16]`@0x50、`name[16]`@0x60 |
| **登录应答** | 0x60 / `0x01` | `tokrequest`@0x1a 必须等于我们发的；`token`@0x1c；`error`@0x30（`0xfeffffff` = 用户名/口令错）；`connection[16]`@0x40 |
| **token** | 0x40 / `0x01` | `requesttype`@0x15（`0x02`=首次、`0x05`=续期）、`requestreply=1`@0x14、`tokrequest`@0x1a、`token`@0x1c、`resetcap` BE@0x24；应答 `requestreply=2` + `response`@0x30 |
| **能力** | 0xA8 / `0x01` | `numradios` BE@0x40；其后每台 0x64：`name[32]`@0x10、`audio[32]`@0x30、`conntype`@0x50、`civ:u8`@0x52、`rxsample` BE@0x53、`txsample` BE@0x55、`enablea/b/c`@0x57-59、`baudrate` BE@0x5a、`capf` BE@0x5e |
| **连接状态** | 0x90 / `0x01` | `busy:u32`@0x60、`computer[16]`@0x64、`ipaddress`@0x84 |
| **请求串流** | 0x90 / `0x01` | `requesttype=3`@0x15、`commoncap`@0x27、`macaddress[6]`@0x2a、`name[32]`@0x40、`username[16]`@0x60、`rxenable=1`@0x70、`txenable`@0x71、`rxcodec`@0x72、`txcodec`@0x73、`rxsample` BE@0x74、`txsample` BE@0x78、`civport` BE@0x7c、`audioport` BE@0x80、`txbuffer` BE@0x84、`convert=1`@0x88 |
| **状态应答** | 0x50 / `0x01` | `error`@0x30、`disc:u8`@0x40、`civport` BE@0x42、`audioport` BE@0x46 |
| **音频** | 0x18+ / `0x01` | `ident`@0x10、`sendseq`@0x12、`datalen` BE@0x16，**PCM 从 0x18 起**（16-bit 小端、单声道、`rxsample` Hz） |

**口令/用户名不是明文**：每个字符按 `(c + index)` 查一张固定的 128 字节置换表
（wfview 里叫 `passcode`），最长 16 字符。表本身是协议常量，随实现一起放。

## 3. 握手顺序（状态机）

```
重复[100 ms]  控制 0x03 "are you there"      ──►  电台
                 ◄── 控制 0x04 "I am here"    （学到 rcvdid）
                 ◄── 控制 0x06 "I am ready"
              登录包（随机 tokrequest）        ──►  电台
                 ◄── 登录应答（tokrequest 匹配）→ 记下 token、rcvdid
              token 请求 type=0x02             ──►  电台
                 ◄── 能力包（name/audio/civ/rxsample…）
                 ◄── 连接状态（busy?）
              请求串流（requesttype=3，带 civport/audioport） ──► 电台
                 ◄── 状态应答（error=0, civport, audioport）
              打开两条本地 socket（绑我们之前申报的端口）←→ 电台推流
每 60 s       token 续期 type=0x05             ──►  电台
每 500 ms     ping 0x07                        ──►  电台
每 100 ms     idle 0x00                        ──►  电台
```

- 电台拒绝（用户名/口令错）时登录应答 `error = 0xfeffffff`；被占用时连接状态 `busy=1` 且
  `computer` 里有别人的名字。
- 电台断电/重启会让流断掉而不发 FIN（UDP）——客户端必须靠「ping 无应答 / 音频静默」判活并重连。

## 4. 我们要实现的最小集

只做**接收音频**（发射暂时仍走内置 USB 声卡，若它在某台机器上可用）：

1. `backends/icom_net/protocol.py` — 纯编解码（本文 §2 的每一个包），无 socket、无 asyncio。
2. `backends/icom_net/client.py` — asyncio：控制流状态机（§3）、音频流解析、token 续期、
   静默看门狗与重连退避；音频输出成 **16 kHz mono PCM 块**，交给调用方。
3. `backends/icom_net/audio.py` — `AudioHandler` 适配器：把 16 kHz 上采样到 48 kHz
   （复用 `audio_resample` 的帧对齐 SRC 思路，或 numpy 线性插值），TX 侧委托给 PyAudio
   （与 G7250 现在的形状一致）。
4. `G7250Backend.create_audio_handler()`：当 `MRRC_G7250_NET_AUDIO=1` 且配置了
   `MRRC_G7250_NET_USER` / `MRRC_G7250_NET_PASS`（电台网络用户名/口令，出厂 `G7250` /
   `beta_tester`）时返回这个适配器，否则维持现状（PyAudio）。

配置项（都进 `config.py`）：

| 变量 | 默认 | 含义 |
| --- | --- | --- |
| `MRRC_G7250_NET_AUDIO` | `0` | 用网口音频（实验性） |
| `MRRC_G7250_NET_USER` | 空 | 电台网络用户名（出厂 `G7250`） |
| `MRRC_G7250_NET_PASS` | 空 | 电台网络口令（出厂 `beta_tester`） |
| `MRRC_G7250_NET_HOST` | `MRRC_G7250_LAN_IP` | 电台地址 |

## 5. 风险与未知（真机才能定）

| 项 | 现状 | 怎么定 |
| --- | --- | --- |
| 音频 codec 编号 | wfview 日志显示 `rx codec: 4`、`rxsample: 16000` | 真机抓包确认；`codec >= 0x40` 表示 Opus（16 kHz PCM 才是 4） |
| 音频负载是否单声道、字节序 | 按 16-bit 小端单声道实现 | 听声音即可判定（噪声 vs 正常） |
| `ident` 字段含义 | 音频头里的 `0x0080`/`0x9781` 等，只与 TX 分包有关 | 接收侧不依赖它，先忽略 |
| 断线判活阈值 | ping 500 ms、3 次无应答重连 | 现场调 |
| G7250 是否支持 TX 音频 | wfview 日志 `tx sample rate: 16000`，`txSampleRates>1` 才发 `txenable=1` | 先只做 RX |

## 6. 验收（分层）

1. **离线**：`protocol.py` 的每个包对着 wfview 的 C 结构体逐字段断言（偏移、字节序、长度）；
   用一台**假电台**（脚本化 UDP 对端）跑完整握手 + 100 个音频包，断言适配器吐出 48 kHz PCM
   且峰值/频率正确。
2. **真机（下一窗口）**：在客户机上用 PowerShell 先打「are you there」拿到 0x04，验证地址/端口/口令；
   再用装好的新版本跑 `MRRC_G7250_NET_AUDIO=1`，看日志里的握手阶段与音频包计数。
3. 声音主观验收：与 wfview 对同一信号对比。

## 6.5 CI-V 隧道（50002）—— 现场把它变成了主线

1.28.52 的现场日志（BG7YQV，2026-11-12 15:35）把问题定死了：

```
Scope-init CI-V commands complete            ← 我们确实发了 27 10 01 / 27 11 01
CI-V scope consumer started
Spectrum broadcast active: S-meter fallback  ← 瀑布退回合成曲线
CI-V scope stream stalled — no complete waveform for >3s
```

USB 侧（电台自报 `ID 00A4`，G90 的地址）对 `0x27` **完全不应答**；而 wfview 的瀑布
来自网口隧道（电台自称 IC-7300、CIV `0xA0`）。同一台机器两套 CI-V：`0xA4` 是 G90 子集，
`0xA0` 是 IC-7300 仿真，频谱只在后者。**所以这个机型的正确架构是 CAT 也走网口**，
而不是「USB 拿控制 + 网口拿音频」。

隧道是第三种包（`packettypes.h` 的 `data_packet_t` / `openclose_packet`，`#pragma pack(1)`）：

| 包 | 长度 | 布局 |
| --- | --- | --- |
| 数据 | `0x15 + frame` | len(u32) type(u16)=**0x01** seq(u16) sentid(u32) rcvdid(u32) reply(u8)=**0xC1** datalen(u16) sendseq(u16) + CI-V 帧 |
| 开/关 | `0x16` | 同上头 + data(u16)=0x01C0 unused(u8) sendseq(u16) magic(u8)：**0x04 开 / 0x00 关** |

三个坑：

1. **`type` 字节撞车**：隧道数据包的 `0x01` 与登录/token/conninfo/status **同一个值**，
   只按 type 分流会把握手吃掉（`protocol.looks_like_civ()` 因此改用「`reply==0xC1` 且
   `datalen+0x15==len`」判别）。
2. **端序不统一**：`datalen` 按本机序（小端）写、接收端也按小端校验，而内层 `sendseq`
   是显式大端。混用不会报错，只会被静默丢弃。
3. **隧道要先「打开」**：状态应答里拿到端口后必须发 open 包，并且**每 100 ms 重发直到真的
   收到 CI-V 数据**（参考实现的发送定时器就是这么做的）。

`client.py` 因此增加 `open_civ()` / `send_civ(frame)` / `on_civ` 回调；`_diag_g7250_net.py`
是现场用的自检工具（握手 → 0x27 探测 → 音频统计 + 可选 WAV），可打包成单文件推到没有
仓库的机器上运行。


## 7. 许可

本仓库没有 GPL 声明（README「Personal / hobby project」）。wfview 是 **GPL-3.0**，
所以这里**不复制其代码**，只按协议事实重写（字段偏移、类型码、状态机顺序都是接口事实）。
若将来想直接用 wfview 的实现（例如它的 Opus 解码器），本仓库必须先声明 GPL-3.0 兼容的许可证。
