# SunSDR2 DX 局域网协议笔记（逆向记录）

> 用途：MRRC 若要原生驱动 SunSDR2 DX（像 FT-710 那样），按本文的字节直接实现即可。
> 本文只记录**可从证据复现**的部分，并把未定项单独列出，不要把「推断」当「结论」引用。

| 项 | 值 |
| --- | --- |
| 分析日期 | 2026-11-08 |
| 目标产物 | `/Applications/ExpertSDR3.app` **1.2.1**（arm64，Qt 6.11.2） |
| 主分析对象 | `Contents/MacOS/device/libSunSDR2dx.dylib`，sha256 `543d574e2c42a296a45631b653355fbd643578c85710af61d042bc64861213e1` |
| 线上取证 | W103D 盒子（192.168.16.201）eth1 上的 `AF_PACKET` 抓包，电台 192.168.16.200，固件序列 `EED06422520078` |
| 工具 | REA 6.7.0（Ghidra 12.1.4 provider；macOS 无预编译反编译器，需自建 `ghidra_opt`/`sleigh_opt`）+ 被动抓包 |

约定：**「实测」**=抓包看到；**「反编译」**=从 libSunSDR2dx 读出；**「推断」**=两者交叉但仍有缺口。

---

## 1. 发现（UDP 广播）

客户端向每个网卡的**广播地址**发 24 字节（端口 = 本地随机端口，即发给对方的同号端口）：

```
偏移  内容
0     产品码   0x32 = SunSDR2 DX（其它型号不同；抓包中 Starter 会轮询 0x32/0x03/0x01/0x42/0x22/0x01/0x12）
1     0xFF
2     0x00
3     0x1A
4..21 全 0
22..23 校验和（小端 u16）
```

校验和 = 对 24 字节按**小端 16 位字**求反码和（末尾回卷进位），对结果取反（网际校验和的同一算法）。
实测：`32 FF 00 1A` + 18×`00` + `CC E6`，手算 `0xE6CC` —— 逐位吻合。

**应答**（电台 → 客户端，24 字节）：

```
32 FF | 01 | 1A | <u16 尾号> | 00 00 | <u16> | <u32 IP> | <u32 IP> | <u16 端口> | <u16> | <u16 变化值>
        ↑方向=应答
```

- `<u16 尾号>` 实测 `0x4E6E` = 20078，是 Starter 显示序列号 `EED06422520078` 的尾数（推断：序列号低 16 位）。
- `<u32 IP>` 出现两次，实测均为 `192.168.16.200`（C0 A8 10 C8）。
- `<u16 端口>` = `0xC351` = **50001**（小端 `51 C3`）。
- 末尾两字节**每次应答都变**，与请求的校验和公式对不上 —— **不是校验和**，含义未解。
- 请求校验和验证通过；**应答不做校验和**（至少 Starter 与电台都不校验）。

---

## 2. 命令通道（UDP 50001）

### 2.1 帧格式（实测 + 构造函数取证）

```
偏移 宽  字段
0    2   魔数 0xFF32（线上字节 32 FF）
2    2   命令号 cmd
4    2   载荷长度 len
6    4   arg（参数选择子，常见 0；频率命令里是收发通道号）
10   4   序号 seq（Starter 恒为 1）
14   4   CRC32（未启用时为 0）
18   ..  载荷
```

- **CRC** = 标准 **CRC-32/ISO-HDLC**（zlib 同款）：查表法、`init=0xFFFFFFFF`、`xorout=0xFFFFFFFF`；
  校验表在 dylib 文件偏移 `0xB4838`，表头 `00000000 77073096 EE0E612C 990951BA`，与标准表逐字节相同。
- 覆盖面 = **头 + 载荷**（`crc(len + 0x12, buf)`，其中 `0x12`=18 字节头）。
- **CRC 是每个命令对象的开关**（对象 `+0x60` 字节）；ExpertStarter 全程关闭，ExpertSDR3 直连时推测开启（未验证）。

### 2.2 请求/应答

- 请求由客户端发往电台；电台回一个**回显头**（同 `cmd`/`arg`/`seq`，通常不带载荷）。
- **`len` 字段不可信**：应答里它会回显请求值，但实际数据报可能没有载荷 —— **载荷长度一律按 UDP 数据报长度算**（减去 18）。

### 2.3 重试

每个 setter 都是「**失败重试 3 次，每次等 50 ms**」（反编译 `setRxFrequency`/`setPreamp` 等可见 4 次调用）。
这就是抓包里「同一帧发两遍」的来源：第一次收到 ACK 就停。

---

## 3. 命令号表

命令号在 `HardwareSunSDR2dx` 构造函数里随对象初始化写进帧头模板（`32 FF <cmd> 00` + 载荷长度 + `seq=1`），
用 setter 的 `setValue(this+偏移, …)` 与之对齐得出。

| cmd | 载荷类型 / 长度 | 含义 | 依据 |
| --- | --- | --- | --- |
| `0x01` | StartParam / 50 B | **startIQ**（见 §5） | 构造函数 + 实测 |
| `0x02` | u32 / 4 | **stopIQ** | 行为（改带宽前必发）+ `setValue(this+0x230)` |
| `0x05` | u32 / 4 | **前放**：`(enable ? 0x80 : 0) \| PreampID` | `setPreamp` @0x5afc8 → `this+0x298` |
| `0x06` | u32 / 4 | **收发通道使能**（Trx） | `setTrx` @0x5cb34 → `this+0xa20` |
| `0x07` | ExtCtrl / 26 | ExtCTRL 输出口 | 构造函数 |
| `0x08` | i64 / 8 | **RX 频率 = Hz × 10** | `setRxFrequency` @0x5aa3c：`(long)(freq*10.0)` |
| `0x09` | i64 / 8 | **TX 频率 = Hz × 10** | `setTxFrequency` @0x5ae6c → `this+0x368` |
| `0x0e` | ManufactureCoeff / 468 | 出厂校准（读/写） | 构造函数 |
| `0x10` | ManufactureCoeff / 468 | 出厂校准（另一段） | 构造函数 |
| `0x12` | AddressParam | 地址/校准写入 | 构造函数 |
| `0x17` | u32 / 4 | **发射功率 drive**（0…255，实测见过 255） | `setDrive` @0x5bfa8 → `this+0x740` |
| `0x18` | u32 / 4 | **监听（MON）使能**（实测 1） | `setMonEnable` @0x5bdc4 → `this+0x670` |
| `0x19` | u32 / 4 | **监听音量**（实测 170） | `setMonVolume` @0x5be54 → `this+0x6d8` |
| `0x1a` | Info / 32 | 读设备信息 | 构造函数 |
| `0x1b` | u32 / 4 | **HF 低通 LPF 开关** | `setHfLpf` @0x5bb1c → `this+0x5a0` |
| `0x1d` | u32 / 4 | **外部参考（10 MHz）开关** | `setExternalReference` @0x5d2bc → `this+0xb20` |
| `0x1e` | u32 / 4 | **天线选择** | `setAntenna` @0x5c8f8 → `this+0x950` |
| `0x1f` | u32 / 4 | 未定 | 构造函数 |
| `0x20` | SdrCw / 52 | **CW 参数块**（700 Hz / 300 Hz / 30 / 7 / 100 …） | 构造函数 + 实测 |
| `0x21` | u32 / 4 | **麦克风选择** | `setMicrophone` @0x5bc70 → `this+0x608` |
| `0x22` | BandscopeParam / 12 | 瀑布参数 | 构造函数 |
| `0x23` | u32 / 4 | **键控（Keyer）** | `setKeyer` @0x5c84c → `this+0x8e8` |
| `0x24` | u32 / 4 | **PA 开关** | `setPa` @0x5c0fc → `this+0x7a8` |
| `0x27` | Vox / 16 | VOX 参数 | 构造函数 |
| `0x15` `0x26` | u32 / 4 | **未定**（候选：CW 状态 / 天调） | 待补 |

### 3.1 重要结论：**模式不是电台命令**

USB/LSB/CW 等**不下发给电台**。SunSDR2 DX 只输出 IQ，解调在 PC 侧完成
（`Contents/Frameworks/libDsp.dylib` / `dsp/libDsp.dylib`），`HardwareSunSDR2dx` 里根本没有 `setMode`。
对 MRRC 的意义：接 SunSDR **不需要实现模式命令**，本地解调即可。

### 3.2 频率单位

`i64 = 频率(Hz) × 10`（0.1 Hz 分辨率）。
实测：20 m 时 `142507500` → 14.250750 MHz；40 m 时 `70280400` → 7.028040 MHz。

---

## 4. 数据通道（UDP 50002）

端口关系在 `StreamSunSDR2dx::open` 里是硬编码的：**流端口 = 命令端口 + 1**。
客户端与电台都用同一个端口号（`ShareAddress` 绑定），并设置 1 MB 收发缓冲、低延迟、MTU 1500。

帧头 10 字节：

```
0  2   0xFF32
2  2   帧类型：0xFFFE = IQ 数据；0x1F00 = 状态/传感器上报
4  2   数据字节数 n
6  2   序号（递增）
8  2   标志（实测 1）
```

**IQ 帧**（实测 45k 包/55 s）：`n = 1200`，之后是 **200 组 (I, Q)**，各 **24 位有符号小端**：

```
I = int24_le(b[10..12])   Q = int24_le(b[13..15])
每包 200 组 = 1200 字节，总计 1210 字节
```

**状态帧**（34 字节）：载荷里含浮点数（实测 `0x42180000`=38.0f、`0x3F800000`=1.0f），推断为 S 表/电压等；
**结构未解**。

---

## 5. 起流时序（实测）

```
0x02 (u32 0)      stopIQ        ← 若正在收流，先停
0x01 (50 B)       startIQ       ← rate 在 +38/+40；rate=0 亦表示停
0x18 (u32 1)      （未定，固定跟进）
0x19 (u32 0xAA)   （未定/监听音量）
```

**StartParam（cmd 0x01）50 字节布局**：

```
[0..31]  8 × u32 默认值（构造函数模板，实测下发时多为 20 或 50）
[32..35] u32 0
[36..37] u16  IQ 通道数（1 或 2）
[38..39] u16  采样率枚举 rate      ← 实测 1 / 2；0 = 停
[40..41] u16  同上（重复写一次）
[42..49] 0
```

采样率枚举与 Hz 的映射**未解**（代码里只见到 `1 << rate`，实测一档约 156 kSa/s）。

---

## 6. 复现方法

### 6.1 静态

```bash
# Ghidra（macOS 需自建原生反编译器）
cd ghidra_*/Ghidra/Features/Decompiler/src/decompile/cpp
make -j8 ghidra_opt sleigh_opt ARCH_TYPE="-arch arm64"
mkdir -p ../../../os/mac_arm_64
cp ghidra_opt ../../../os/mac_arm_64/decompile
cp sleigh_opt ../../../os/mac_arm_64/sleigh
export GHIDRA_INSTALL_DIR=$PWD/../../../..
rea doctor --json      # ghidra: available=true

LIB=/Applications/ExpertSDR3.app/Contents/MacOS/device/libSunSDR2dx.dylib
rea inspect "$LIB" --provider ghidra --format json
rea decompile "$LIB" --procedure 0x57b98 --provider ghidra --format json   # 构造函数：命令号模板
```

关键地址：`0x57b98` 构造函数、`0x5afc8` setPreamp、`0x5aa3c` setRxFrequency、`0x5ae6c` setTxFrequency、
`0x70abc` Stream::open、`0x5a070` startIQ、`0x9b66c` CRC。

### 6.2 线上

在盒子（或任何与电台同网段的 Linux）上被动抓包，不干扰业务：

```python
import socket, struct
s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(3)); s.bind(("eth1", 0))
# 过滤 UDP 50001（命令）/ 50002（数据），打印时间、方向、长度、hex
```

---

## 7. 未解清单

1. 发现应答末尾 2 字节的含义（每次变化，非校验和）。
2. `cmd 0x15 / 0x26` 的语义（u32 类）。已定位对象偏移 `this+0x9b8` / `this+0x810`，但没有公开 setter 直接使用它们
   （`setCwState` / `setCwPtt` / `setCwExtKey` 只发 Qt 信号，真正的下发点在 `run()` 或别处）。
3. 状态帧（34 B）与 `Info`（32 B）/`PaInfo`/`AtuInfo` 的结构。
4. 采样率枚举 → Hz 的映射表。
5. ExpertSDR3 直连时 CRC 是否启用、`seq` 是否递增（Starter 恒为 1）。
6. CW 键控（`setCwState`/`setCwPtt`/`setKeyer` 未见独立命令对象）。

---

## 8. 附：ExpertStarter（1.1.9）的已知缺陷

盒子上的 `expert-starter`（Go 二进制，cgo 调 `libftd2xx`）会周期性崩：

```
fatal error: invalid pointer found on stack
  ... eesdr.com/Starter/pkg/libftd.ReadProduct
  ... eesdr.com/Starter/internal/scanner.(*Udp).sendBroadcast
```

`Restart=always` 会把它拉起，但每次断流。已把 `RestartSec` 从厂商默认 15 s 改为 2 s
（drop-in：`/etc/systemd/system/expert-starter.service.d/restartsec.conf`）。

**2026-11-08 已定位触发条件（自然实验 + 日志对齐）**：崩溃只发生在 **FT-4222 被另一个进程打开或刚重新枚举** 之后的一两分钟里。

| 触发事件 | 崩溃时刻 | 间隔 |
| --- | --- | --- |
| MRRC 启动 `scope_pipe`（首个瀑布客户端，`libs_loaded:ft4222 + ftd2xx`） | 08:32:10 → 08:33:53 | +103 s（随后 08:35:25、08:37:04 又连崩两次） |
| FT-710 USB 重新插回（FT-4222 重新枚举） | 10:20:59 → 10:21:46 | +47 s |
| MRRC 再次启动 `scope_pipe` | 10:34:34 → 10:35:58 | +84 s |

两边都加载 **同一份 `libftd2xx.so`**：MRRC 的 `scope_pipe` 用它打开 FT-4222，Starter 用它做
`ReadProduct` 枚举。枚举撞上「设备正被另一进程打开」或「设备刚枚举完」时，libftd2xx 回一个坏指针，
Go 运行时的 cgo 指针检查直接 `fatal error`（不是 panic，进程当场退出）。

**「设备存在」不是条件**：FT-710 连着跑了近 8 小时零崩溃；拔掉 55 分钟零崩溃。

缓解：`RestartSec=2` 把断流压到 2 秒。根治要么让 Starter 看不见 FT-4222（非 root 运行 + udev 规则
把该设备放进 scope_pipe 独占的用户组），要么让 MRRC 不再使用 FT-4222 —— 后者不可行（那正是 FT-710 的瀑布数据源）。
