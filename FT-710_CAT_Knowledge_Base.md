# FT-710 CAT Protocol Knowledge Base

> 从 Yaesu FT-710 CAT Operation Reference Manual 提取并整理
> 版本: 2306-C | 适用于 mrrc_ft710 Python 项目

---

## 1. 通信参数 (Communication Parameters)

| 参数 | CAT-1 (Enhanced) | CAT-2 (Standard) | CAT-3 (TUNER/LINEAR) |
|------|------------------|-------------------|-----------------------|
| 默认波特率 | 38400 bps | 4800 bps | 38400 bps |
| 数据位 | 8 | 8 | 8 |
| 停止位 | 1 or 2 | 1 (固定) | 1 or 2 |
| 奇偶校验 | None | None | None |
| 流控 | None | None | None |
| 电平 | USB UART | USB UART | 5V TTL |

### 波特率选项: 4800 / 9600 / 19200 / 38400 / 115200 bps
### 超时定时器选项: 10 / 100 / 1000 / 3000 msec

---

## 2. CAT 命令格式 (Command Format)

```
命令结构: [2字母命令] + [参数...] + [;(终止符)]
示例:     FA014250000;
          ↑↑ ↑↑↑↑↑↑↑↑↑ ↑
          命令  参数      终止符
```

### 三种命令类型:
- **Set (设置)**: 发送给电台 — 改变设置
- **Read (读取)**: 发送给电台 — 查询当前值
- **Answer (应答)**: 电台返回 — 返回当前值

---

## 3. CAT 命令完整列表 (Complete Command Reference)

### 图例: O=支持, X=不支持

---

### 3.1 核心频率/模式控制 (Core Frequency/Mode)

| 命令 | 功能 | Set | Read | Ans | AI | 格式 |
|------|------|-----|------|-----|----|------|
| **FA** | VFO-A 频率 | O | O | O | O | `FA` + 9位Hz (000030000-075000000) |
| **FB** | VFO-B 频率 | O | O | O | O | `FB` + 9位Hz |
| **MD** | 操作模式 | O | O | O | O | `MD` + P1(0=MAIN,1=SUB) + P2(模式号) |
| **VS** | VFO 选择 | O | O | O | O | `VS` + 0(VFO-A) / 1(VFO-B) |
| **IF** | VFO-A 信息 | X | O | O | O | 组合信息包 |
| **OI** | VFO-B 信息 | X | O | O | O | 组合信息包 |
| **FT** | 发射 VFO | O | O | O | O | 0=MAIN TX / 1=SUB TX |
| **ST** | Split | O | O | O | O | 0=OFF / 1=ON |
| **SV** | 交换 VFO | O | X | X | X | 无参数 |
| **VM** | V/M 键 | O | X | X | X | 无参数 |

### 模式编号 (Mode Numbers):

| 编号 | 模式 | 编号 | 模式 | 编号 | 模式 | 编号 | 模式 |
|------|------|------|------|------|------|------|------|
| 0x01 | LSB | 0x05 | AM | 0x09 | RTTY-U | 0x0D | AM-N |
| 0x02 | USB | 0x06 | RTTY-L | 0x0A | DATA-FM | 0x0E | PSK |
| 0x03 | CW-U | 0x07 | CW-L | 0x0B | FM-N | 0x0F | DATA-FM-N |
| 0x04 | FM | 0x08 | DATA-L | 0x0C | DATA-U | | |

---

### 3.2 仪表读数 (Meter Readings) ⭐ 关键

#### RM — READ METER (读取仪表)

```
Read:  RM P1 ;
Answer: RM P1 P2 P2 P2 P3 P3 P3 ;

P1 选择仪表类型:
  0: - (未使用)
  1: S (S表, Main Band)
  2: - (未使用)
  3: COMP (压缩)
  4: ALC
  5: PO (功率, Power)
  6: SWR (驻波比)
  7: IDD (漏极电流, Drain Current)
  8: VDD (漏极电压, Drain Voltage)

P2: 原始值 000-255 (3位)
P3: 固定 000 (3位)

响应示例:
  RM5 150 000 ; → PO原始值=150 (对应~50W, 见校准表)
  RM6 052 000 ; → SWR原始值=52 (对应~1.5:1)
  RM8 192 000 ; → VDD原始值=192 (对应~13.8V)
```

**RM 响应解析规则:**
- 总长度: 9 字符 (RM + 1位P1 + 3位P2 + 3位P3)
- P2 有效数据在 `resp[3:6]` (3位数字)
- P3 (`resp[6:9]`) 始终为 "000"

#### SM — S-METER READING (S表读数)

```
Read:  SM P1 ;  (P1=0, 固定)
Answer: SM P1 P2 P2 P2 ;  (P2=000-255)
```

#### MS — METER SW (仪表显示切换)

```
Set:   MS P1 P2 ;
Read:  MS ;
Answer: MS P1 P2 ;

P1 选择仪表显示类型:
  0: PO (功率)
  1: COMP (压缩)
  2: ALC
  3: VDD (电压)
  4: ID (电流)
  5: SWR (驻波比)

P2: 0 (固定)
```

---

### 3.3 发射控制 (TX Control)

| 命令 | 功能 | Set | Read | Ans | AI | 格式 |
|------|------|-----|------|-----|----|------|
| **TX** | TX 设置 | O | O | O | O | 0=OFF / 1=CAT TX / 2=RADIO TX |
| **MX** | MOX 设置 | O | O | O | O | 0=OFF / 1=ON |
| **PC** | 功率控制 | O | O | O | O | `PC` + 005-100 (瓦) |
| **BI** | Break-In | O | O | O | O | 0=OFF / 1=ON |
| **VX** | VOX 状态 | O | O | O | O | 0=OFF / 1=ON |
| **FT** | 发射功能 | O | O | O | O | 0=MAIN TX / 1=SUB TX |

---

### 3.4 音频/DSP 设置 (Audio/DSP Settings)

| 命令 | 功能 | 范围 | 格式 |
|------|------|------|------|
| **AG** | AF 增益 | 000-255 | `AG0` + 3位 |
| **RG** | RF 增益 | 000-255 | `RG0` + 3位 |
| **MG** | MIC 增益 | 000-100 | `MG` + 3位 |
| **SQ** | 静噪级别 | 000-100 | `SQ0` + 3位 |
| **ML** | 监听级别 | P1=0(ON/OFF), 1(Level) | `ML` + P1 + 3位 |
| **AO** | AMC 输出级别 | 001-100 | `AO` + 3位 |
| **PL** | 语音处理器级别 | 001-100 (设置), 000(OFF)-100 (应答) | `PL` + 3位 |
| **PR** | 语音处理器 | P1=0(SP),1(PME); P2=1(OFF),2(ON) | `PR` + P1 + P2 |
| **VG** | VOX 增益 | 000-100 | `VG` + 3位 |
| **VD** | VOX 延迟时间 | 00-33 (30-3000ms) | `VD` + 4位 |
| **AV** | 防VOX级别 | 001-100 | `AV` + 3位 |

#### PR 命令详解 (重要 ⚠️ PDF 有误):
```
Set: PR P1 P2 ;
P1: 0 = Speech Processor (语音处理器)
    1 = Parametric Microphone Equalizer (参量麦克风均衡器)
P2: ⚠️ PDF 写 1=OFF, 2=ON — 这是手册笔误!
    实际: 0 = OFF, 1 = ON (与 FT-710 所有其他开关命令一致)

正确用法 (经实机验证):
  PR00; → 语音处理器 OFF
  PR01; → 语音处理器 ON
  PR10; → 参量均衡器 OFF
  PR11; → 参量均衡器 ON

❌ 不要使用 PR02 (会关掉语音处理器,导致发射无音频!)

---

### 3.5 滤波器/DSP (Filter/DSP)

| 命令 | 功能 | 范围 | 格式 |
|------|------|------|------|
| **SH** | 宽度 | 00-23 (见表3) | `SH` + P1(0) + P2(0) + 2位 → `SH00<NN>` |
| **NA** | 窄带 | 0=OFF / 1=ON | `NA0` + P2 |
| **NB** | 噪声抑制器 | 0=OFF / 1=ON | `NB0` + P2 |
| **NL** | 噪声抑制器级别 | 000-010 | `NL0` + 3位 |
| **NR** | 降噪(DNR) | 0=OFF / 1=ON | `NR0` + P2 |
| **RL** | DNR 级别 | 01-15 | `RL` + 2位 |
| **BC** | 自动陷波(DNF) | 0=OFF / 1=ON | `BC0` + P2 |
| **BP** | 手动陷波 | P2=0(ON/OFF),1(Freq) | `BP0` + P2 + 3位 |
| **CO** | 轮廓/APF | P2=0(CONTOUR),1(FREQ),2(APF),3(APF FREQ) | `CO0` + P2 + 4位 |
| **IS** | IF SHIFT | +/- 0-1200Hz (20Hz步进) | `IS00` + +/- + 4位 |
| **CF** | CLAR | 复杂参数 | 见PDF |
| **GT** | AGC 功能 | 见下方 | `GT` + P1(0) + P2 |

#### GT (AGC) 命令详解:
```
Set: GT P1 P2 ;  (P1=0, 固定)
P2: 0=AGC OFF
    1=AGC FAST
    2=AGC MID
    3=AGC SLOW
    4=AGC AUTO

Answer: GT P1 P3 ;
P3: 0=AGC OFF
    1=AGC FAST
    2=AGC MID
    3=AGC SLOW
    4=AGC AUTO-FAST
    5=AGC AUTO-MID
    6=AGC AUTO-SLOW
```

---

### 3.6 天线/前置放大/衰减 (Antenna/Preamp/Attenuator)

| 命令 | 功能 | 范围 | 格式 |
|------|------|------|------|
| **AC** | 天线调谐器 | 见下方 | `AC` + P1(0) + P2 + P3 |
| **PA** | 前置放大(IPO) | 0=IPO, 1=AMP1, 2=AMP2 | `PA0` + P2 |
| **RA** | RF 衰减器 | 0=OFF, 1=6dB, 2=12dB, 3=18dB | `RA0` + P2 |
| **AN** | 天线选择 | 1-3 | `AN` + 1位 |

#### AC (天线调谐器) 命令详解 ⚠️ 关键:
```
Set: AC P1 P2 P3 ;
P1: 0 (固定)
P2: 0 = 内部/外部天调
    1 = - (未使用)
    2 = ATAS
P3 (P2=0, 标准天调):
    0 = 天调 OFF (停止调谐)
    1 = 天调 ON
    3 = 开始调谐

正确用法:
  AC000; → 天调 OFF
  AC001; → 天调 ON
  AC003; → 开始调谐

错误用法 (来自旧代码):
  AC010; → P2=1 是无效值! 
  AC011; → P2=1, P3=1 组合无效!
```

---

### 3.7 状态信息 (Status Information)

#### RI — RADIO INFORMATION (电台信息)

```
Read:  RI P1 ;  (P1=0, 固定)
Answer: RI P1 P2 P3 P4 P5 P6 P7 P8 ;

P2: 0=Normal / 1=Hi-SWR (高驻波比警告)
P3: 0=Stop / 1=Recording / 2=Playing (录音/播放状态)
P4: 0=RX / 1=TX / 2=TX INHIBIT (收发状态)
P5: 0 (固定)
P6: 0=天调停止 / 1=天调调谐中
P7: 0=扫描停止 / 1=扫描中 / 2=扫描暂停
P8: 0=静噪关闭 / 1=静噪打开(忙)
```

#### ID — 电台识别

```
Read:  ID ;
Answer: ID 0800 ;  (FT-710 固定返回 0800)
```

#### VE — 固件版本

```
Read:  VE P1 ;  
P1: 0=MAIN CPU / 1=DISPLAY CPU / 2=SDR / 3=DSP
Answer: VE P1 P2 P2 P2 P2 ;  (P2=XX-XX, BCD格式)
```

---

### 3.8 波段/频道 (Band/Channel)

| 命令 | 功能 | 格式 |
|------|------|------|
| **BS** | 波段选择 | `BS` + 00-11 (见波段表) |
| **BD** | 波段下调 | `BD` + 0(MAIN) / 1(SUB) |
| **BU** | 波段上调 | `BU` + 0(MAIN) / 1(SUB) |
| **CH** | 频道上下 | `CH` + 0(UP) / 1(DOWN) |
| **MC** | 记忆频道 | `MC` + 3位频道号 |
| **MA** | 记忆→VFO-A | `MA;` |
| **MB** | 记忆→VFO-B | `MB;` |
| **MW** | 记忆写入 | 复杂格式 |
| **MR** | 记忆读取 | 复杂格式 |

#### BS 波段编号:
```
00=1.8MHz   01=3.5MHz   02=5MHz     03=7MHz
04=10MHz    05=14MHz    06=18MHz    07=21MHz
08=24.5MHz  09=28MHz    10=50MHz    11=70MHz/GEN
```

---

### 3.9 扫频/频谱 (Scope/Spectrum)

#### SS — SPECTRUM SCOPE

```
Set: SS P1 P2 P3 P4 P5 P6 P7 ;  (P1=0, 固定)

P2=0 (SPEED): P3=0(SLOW1)-5(STOP)
P2=1 (PEAK):  P3=0(LV1)-4(LV5)
P2=2 (MARKER): P3=0(OFF)/1(ON)
P2=3 (COLOR): P3=0(COLOR-1)-A(COLOR-11)
P2=4 (LEVEL): P3-P7=-30.0 to +30.0 dB (0.5dB步进, 5位)
P2=5 (SPAN):  P3=0(1kHz)-9(1MHz)
P2=6 (MODE):  P3=0(3DSS CENTER)-A(W/F FIX NORMAL)
P2=7 (AF-FFT): P3=AF-FFT ATT, P4=OSC Level ATT, P5=OSC Time
```

---

### 3.10 其他命令 (Other Commands)

| 命令 | 功能 | 格式 |
|------|------|------|
| **AI** | 自动信息 | `AI` + 0(OFF) / 1(ON) |
| **PS** | 电源开关 | `PS` + 0(OFF) / 1(ON) — ⚠️ 实测警告（2026-07-27/28）：PS0 关机可靠；**PS1 开机不可靠**（待机时电台可能忽略，3 次重试 36 秒未唤醒）；**启动中途发 PS0 会打挂 CAT MCU**（串口/声卡/FT4222 均在线但 CAT 永久不应答，需物理断电恢复）。网页 UI 不提供电源开关；仅维护脚本使用，且必须遵守：PS1 后 15 秒内不发 PS0、PS1 重试 ≤3 次并以 `FA;` 回读验证 |
| **LK** | 锁定 | `LK` + 0(OFF) / 1(ON) |
| **SC** | 扫描 | `SC` + 0(OFF) / 1(UP) / 2(DOWN) |
| **KM** | 键控记忆 | `KM` + P1(1-5) + 最多50字符 |
| **KP** | 键控音调 | `KP` + 00-75 (300-1050Hz, 10Hz步进) |
| **KR** | 键控器 | `KR` + 0(OFF) / 1(ON) |
| **KS** | 键控速度 | `KS` + 004-060 (WPM) |
| **KY** | CW 键控 | `KY` + P1(0=TEXT,1=MESSAGE) + P2(0-5) |
| **CS** | CW SPOT | `CS` + 0(OFF) / 1(ON) |
| **ZI** | 归零(CW) | `ZI` + 0(固定) |
| **DA** | 显示调光 | `DA` + 对比度(00-20) + 亮度(00-20) + LED(00-20) |
| **DT** | 日期时间 | P1=0(日期:yyyymmdd) / 1(时间:hhmmss UTC) |
| **EX** | 菜单 | P1(01-06) + P2(01-05) + P3(01-26) + P4(参数) |
| **OS** | 中继偏移 | P1=0(MAIN)/1(SUB) + P2=0(Simplex)/1(+)/2(-) |
| **CN** | CTCSS 频率号 | P1=0(MAIN)/1(SUB) + P2=0(固定) + 000-049 |
| **CT** | CTCSS 开关 | P1=0(MAIN)/1(SUB) + P2=0-2 |
| **SF** | 子拨盘功能 | P1=0(FUNC)/1(DSP) + P2(功能号) |
| **GP** | GP 输出 | P1-P4 各 0(LOW)/1(HIGH) (5V TTL, 最大3mA) |
| **LM** | 加载消息 | P1=0(MESSAGE)/1(RECORD) + P2(频道) |

---

### 3.11 ⚠️ 危险命令 (Dangerous Commands)

| 命令 | 实际功能 | 误用风险 |
|------|----------|----------|
| **DN** | MIC DOWN / 步进下调 (~20Hz) | **不是 DNR!** 发送 `DN;` 会改变频率! |
| **UP** | MIC UP / 步进上调 | 改变频率 |

---

## 4. 校准数据 (Calibration Data)

### 4.1 S-Meter (SM0 / RM1)

| 原始值 | dBm | S-单位 | 原始值 | dBm | S-单位 |
|--------|-----|--------|--------|-----|--------|
| 0 | -54 | S0 | 130 | 0 | S9 |
| 12 | -48 | S1 | 150 | +10 | +10 |
| 27 | -42 | S2 | 172 | +20 | +20 |
| 40 | -36 | S3 | 190 | +30 | +30 |
| 55 | -30 | S4 | 220 | +40 | +40 |
| 65 | -24 | S5 | 240 | +50 | +50 |
| 80 | -18 | S6 | 255 | +60 | +60 |
| 95 | -12 | S7 | | | |
| 112 | -6 | S8 | | | |

### 4.2 Power Meter (RM5)

| 原始值 | 功率(W) | 原始值 | 功率(W) |
|--------|---------|--------|---------|
| 0 | 0.0 | 147 | 50.0 |
| 27 | 0.0 | 176 | 75.0 |
| 94 | 25.0 | 205 | 100.0 |
| | | 255 | 110.0 |

### 4.3 SWR Meter (RM6)

| 原始值 | SWR | 原始值 | SWR |
|--------|-----|--------|-----|
| 0 | 1.0 | 126 | 3.0 |
| 26 | 1.2 | 173 | 4.0 |
| 52 | 1.5 | 236 | 5.0 |
| 89 | 2.0 | 255 | 9.9 |

### 4.4 Voltage Meter (RM8)

| 原始值 | 电压(V) | 原始值 | 电压(V) |
|--------|---------|--------|---------|
| 0 | 0.0 | 192 | 13.8 |
| 255 | 15.0 | | |

### 4.5 Current Meter (RM7)

| 原始值 | 电流(A) | 原始值 | 电流(A) |
|--------|---------|--------|---------|
| 0 | 0.0 | 86 | 8.0 |
| 53 | 5.0 | 98 | 9.0 |
| 65 | 6.0 | 107 | 10.0 |
| 78 | 7.0 | 255 | 26.0 |

---

## 5. IF 信息包解析 (IF Information Packet)

```
IF 应答格式 (30字节):
IF P1 P1 P1 P2 P2 P2 P2 P2 P2 P2 P2 P2 P3 P3 P3 P3 P3 P4 P5 P6 P7 P8 P9 P9 P10 ;

字段:
  P1 (3位):   000=VFO/MT/QMB, 001-099=记忆频道, P1L-P9U=PMS, 5xx=5MHz, EMG=紧急
  P2 (9位):   VFO-A 频率 (Hz)
  P3 (5位):   +/- + 4位 Clarifier 偏移 (0000-9990 Hz)
  P4 (1位):   RX CLAR: 0=OFF / 1=ON
  P5 (1位):   TX CLAR: 0=OFF / 1=ON
  P6 (1位):   模式 (同MD命令的模式编号, 0-F)
  P7 (1位):   0=VFO / 1=记忆频道 / 2=记忆调谐 / 3=QMB / 5=PMS
  P8 (1位):   0=OFF / 1=CTCSS ENC/DEC / 2=CTCSS ENC
  P9 (2位):   00 (固定)
  P10 (1位):  0=Simplex / 1=Plus Shift / 2=Minus Shift
```

---

## 6. 已知陷阱与注意事项 (Known Pitfalls)

### 6.1 PR 命令 — Yaesu PDF 手册笔误
- PDF 写 P2: 1="OFF", 2="ON" ❌ **这是错的!**
- **实际**: P2: 0=OFF, 1=ON (与 FT-710 所有其他开关命令 NB/NR/BC/NA/BI/VX 一致)
- 发送 `PR02` 会关掉语音处理器，导致发射无音频!

### 6.2 AC 天调命令格式
- P2=0 用于标准天调, P2=1 无效
- P3=0(OFF), 1(ON), 3(Tuning Start) — 不是 2!
- 错误的旧代码使用 "010"(ON) 和 "011"(Tuning), 实际应为 "001" 和 "003"

### 6.3 DN 命令
- `DN;` 是 MIC DOWN / 步进下调命令, 不是 DNR!
- 发送 `DN;` 会导致频率降低约20Hz

### 6.4 命令间延迟
- FT-710 CAT 处理器需要命令间至少 20ms 延迟
- 参考 Hamlib post_write_delay = 20ms

### 6.5 硬件流控
- FT-710 的 CP210x USB-UART 不支持 RTS/CTS 硬件流控
- 必须使用 NoFlowControl

### 6.6 RM 响应格式
- 响应为 9 字符: RM + 1位P1 + 3位P2 + 3位P3
- P2 是有效数据 (000-255)
- P3 始终为 "000"

---

## 7. 实现检查清单 (Implementation Checklist)

### ✅ 已实现:
- [x] FA/FB — VFO 频率读写
- [x] MD — 模式读写
- [x] VS — VFO 选择
- [x] TX — PTT 控制
- [x] SM0 — S表读取
- [x] RM4/RM5/RM6 — ALC/PO/SWR 仪表 (TX期间轮询)
- [x] RM7/RM8 — IDD/VDD 仪表 (慢速轮询)
- [x] PC — 功率控制
- [x] AG/RG — AF/RF 增益
- [x] PA/RA — 前置放大/衰减
- [x] NB/NR/BC — 噪声抑制/降噪/自动陷波
- [x] SH — 滤波器宽度
- [x] SQ — 静噪
- [x] MG — MIC 增益
- [x] ST — Split
- [x] VX — VOX
- [x] BI — Break-In
- [x] BS — 波段选择
- [x] AN — 天线选择
- [x] GT — AGC
- [x] SS — 频谱相关命令
- [x] PS — 电源开关
- [x] ID — 电台识别
- [x] IF — 信息查询

### ❌ 待修复/待实现:
- [ ] **PR 命令**: set_compressor 发送值反转 (P2=1→OFF, 应为 P2=2→ON)
- [ ] **PR 轮询解析**: resp.endswith("1") 反转 (1=OFF, 2=ON)
- [ ] **AC 命令**: tuner_map 值错误 ({0:"000",1:"010",2:"011"} → 应为 {0:"000",1:"001",2:"003"})
- [ ] **AC 轮询解析**: 错误地将 P3=1 当作 Tuning (应为 ON), P3=3 才是 Tuning
- [ ] **RM3 (COMP)**: TX期间未轮询 COMP 仪表
- [ ] **RI 命令**: 未轮询电台信息 (Hi-SWR 警告等)
- [ ] **MS 命令**: 未实现仪表显示切换
- [ ] **AO 命令**: 未实现 AMC 输出级别控制
- [ ] **RadioState**: 缺少 RI 相关状态字段

---

## 8. 参考链接 (References)

- FT-710 CAT Operation Reference Manual (Yaesu 2306-C)
- Hamlib FT-710 实现: `hamlib/rigs/yaesu/ft710.c`
- Hamlib 4.5.5 变更日志: post_write_delay=20ms, 无串口握手
- 本项目的 `cat_controller.py` — Python CAT 协议实现
- 本项目的 `radio_state.py` — 状态管理
- 本项目的 `poll_scheduler.py` — 轮询调度
- SDD/FT-710-Hamlib-Gap-Analysis.md — 差距分析文档
