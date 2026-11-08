# SunSDR2 DX 使用指南

MRRC Modern 的 `sunsdr2dx` 是 Expert Electronics **SunSDR2 DX** 的原生网络后端。控制、频谱、接收音频和发射音频均通过 UDP，不需要串口、USB 声卡或旧 SunMRRC 服务。

当前机型按**硬件未完全验证**发布：2026-11-08 已完成基础 RX/TX 验收，但 SSB 主观音质、S 表校准和 CQ/FT8 实际 RF 尚未签收，因此默认只能接收，PTT/TUNE 仍受 `MRRC_ALLOW_UNVERIFIED_TX` 门禁保护。

## 1. 连接

1. 电台和运行 MRRC 的电脑/盒子接入同一网络。
2. 打开 ☰ →「连接设置」，选择 **Expert Electronics SunSDR2 DX**。
3. 填「电台 IP / 主机名」。「本机绑定地址」通常留空；多网卡时可指定通往电台的网卡地址。
4. 保存并重启。网络机型不要求串口、波特率或 USB 音频设备。

电台协议使用 UDP 50001（控制）和 50002（IQ）。一次只允许一个控制端占用设备；不要同时运行 ExpertSDR3、旧 SunMRRC 或第二个 MRRC 实例。端口被占用时，新实例会明确报连接失败，而不是与旧服务共享数据流。

对应环境变量：

```bash
MRRC_RADIO_MODEL=sunsdr2dx
MRRC_SUNSDR_HOST=电台地址
MRRC_SUNSDR_LOCAL_HOST=                 # 可选，默认自动
MRRC_SUNSDR_CONTROL_PORT=50001          # 通常不要改
MRRC_SUNSDR_IQ_PORT=50002               # 通常不要改
MRRC_SUNSDR_SAMPLE_RATE=78125          # 156250/312500 也可，见 §7
MRRC_WDSP=off                          # 仅实验性 IQ 路径；NR/AN 走音频后处理，默认关
MRRC_ALLOW_UNVERIFIED_TX=0
```

主机名在启动时解析一次并缓存。若电台 DHCP 地址发生变化，请重新连接/重启服务；固定地址或 DHCP 保留更适合长期使用。

## 2. 接收与频谱

- 瀑布图使用标准 MRRC 850 点格式；四档 IQ 速率/半宽为 39062/78125/156250/312500 S/s，对应约 ±19.5/±39.1/±78.1/±156.25 kHz。设备 IQ 里的 30.5 kHz IF 偏移已在服务端旋转掉，瀑布中心就是 VFO；频率标尺与点击瀑布调谐使用同一坐标。
- 切换带宽会重新初始化设备流，发射中会被拒绝。
- USB、LSB、CW、DIGI、AM、NFM、FM 由服务端软件解调，统一输出 48 kHz PCM/Opus。
- **NR（NR2）和 AN（自动陷波，ANF）** 来自随包的 WDSP 原生库
  （`backends/sunsdr/vendor/libwdsp.so` / macOS `.dylib`），在基带上先做
  NR2/ANF 再解调；库缺失时这两个按钮自动隐藏，Python 解调继续工作。
  **NR/AN 按钮默认关闭，但保留在页面上**。默认音频完全走 Python 解调；
  只有点开 NR/AN 时才创建 48 kHz 音频后处理（NR2/ANF），不会默认处理。
  `MRRC_WDSP=on` 只开启实验性的 IQ 前处理路径（目前 W103D 上仍有可闻失真，
  不建议）。NR Level 的 1–15 会映射到 WDSP 的 0–100 强度。
- **NB（噪声消除）** 用 MRRC 自己的软件脉冲抑制，不启用 WDSP 的 SNB：
  后者的 `snb.c` 有 use-after-free，开启会让整个服务进程崩溃。NB Level
  0–10 直接控制脉冲门限（0 最保守，10 最激进）。
- COMP、SQL、RF Gain、VFO-B、A/B、SPLIT、电台电源在本后端没有真实实现，
  Web/Android 会按能力位隐藏；server 也会拒绝，不再把本地状态改成“已开”。
- 浏览器、iOS/Android、录音、只听页面和 FT8 使用同一音频链，不需要额外声卡。
- S 表来自通带 IQ 能量，尚未用标准信号源校准；它适合比较相对强弱，不应当作经过校准的 dBm 仪器。

## 3. 发射、功率与遥测

完成真机检查后才可显式设置 `MRRC_ALLOW_UNVERIFIED_TX=1`。首次发射必须使用假负载，并用另一台接收机监听。

- SSB 语音、TUNE、CQ 和 FT8 共用 MRRC 的 PTT 所有权、断线释放和 TX 队列。键控后若 2 秒内没有收到设备 `0x1F01` 的 TX 确认，服务端会自动重发一次 PTT ON；释放路径不受这个重发影响。
- 当前发射只开放 USB/LSB（DIGI-U 按 USB 发射）。AM/FM/CW 键控会被显式拒绝，不会静默沿用上一次的 SSB 边带；TUNE 在这些模式下同样拒绝。
- 「RF Power」映射到设备 DRIVE。软件麦克风电平只调整数字音频，不应拿来追求射频功率；过度放大只会触发限幅并增加失真。
- TUNE 是有 30 秒上限的 700 Hz 测试音，不代表设备有内置 ATU。
- 遥测可显示前向功率、平均功率、电源电压和 PA 温度。设备没有反向功率字段，因此 MRRC **不会计算或虚构 SWR/ALC**。
- AM/FM/CW 发射暂未开放；接收支持不等于这些发射方式已经验证。

## 4. 现场诊断

只读检查：

```bash
.venv/bin/python _diag_sunsdr.py --host 电台地址
```

报告包含绑定地址、端口、采样率、包计数、频谱范围、S 表、遥测、队列丢包和 PTT 确认。把完整输出附到诊断反馈中。

仅在假负载就位、允许发射时：

```bash
.venv/bin/python _diag_sunsdr.py --host 电台地址 --tx-check --allow-tx
```

该命令发出 200 ms、700 Hz 测试音，并在退出路径释放 PTT。单独使用 `--tx-check` 不会发射。

## 5. 范围

本后端只覆盖 SunSDR2 DX 的现有 UDP 协议。它不声明支持 SunSDR2 PRO、MB1 或 ExpertSDR3 的 TCI 机型；这些设备需要独立协议实现和验收。

## 6. 本次真机验收记录

> **2026-11-09 补（WDSP）**：NR2/ANF 作为可选原生库随包，NR/NB/AN 真正可用；
> NB 走软件脉冲抑制（WDSP `snb.c` 的 use-after-free 不可用）。NR/NB/AN/COMP/
> SQL/RF Gain/VFO-B/SPLIT/电台电源按能力位显示或隐藏，server 不再乐观更新
> 不支持的字段。参考实现的 per-band power 表仍未移植：MRRC 仍使用全局
> RF Power 滑块。

2026-11-08 已在 SunSDR2 DX 上完成：四档 IQ 速率均有有效 PCM 和频谱，0 坏包/0 RX 丢帧；USB/LSB/CW/DIGI/AM/NFM/FM 接收、设备重连、发射中拒绝切档、PTT 释放、真实 WS 音频和 FT8 连续三个时隙解码。2 秒 700 Hz 发射为 0 underrun/0 drop。高档位曾暴露「空重采样块使 AGC 变 NaN」并已修复回归。

当前仍保留默认 TX 门禁：SSB 语音的主观音质、S 表校准和 AM/FM/CW TX 不在本次已验证范围内。FM 无载波时可能有较高噪声。

## 7. W103D 盒子

**结论：78k 稳；156k 已实测可跑（整机约 66% 单核，无持续 lag）；312k 不建议默认。**

- W103D 是 Amlogic S905L3A（4×Cortex-A53 @1.8 GHz、2 GB）。SunSDR 后端没有 USB 声卡，
  RX 解调、850-bin 频谱和 TX 调制全部是 CPU 软件 DSP。
- **WDSP 把 RX 解调从 Python 换成了原生 C**，这是 156k 能跑的关键。实测
  （2026-11-09，盒子本机、5 s 音频、`resource.getrusage`）：
  - 78k：WDSP 5 s 音频用 **0.64 s CPU ≈ 12.7% 单核**（Python 基线 20.9%）；
  - 156k：**15.4% 单核**；整机服务进程约 **66% 单核**，事件循环无持续 15–29 ms 卡顿；
  - 312k：WDSP 约 **20.6% 单核**，但整机/链路余量小，**不建议默认**（本次 312k
    测试中 eth1 掉线后出现音频中断，不能把原因全归给 DSP，但也没有理由把它当默认档）。
- 因此盒子上：
  - **默认 78k**（`MRRC_SUNSDR_SAMPLE_RATE=78125`，span 1）；
  - 需要更宽瀑布时切 **156k**（span 2）；先观察 `/Perf` 的 CPU/事件循环延迟；
  - 312k 只作实验，不建议长期开；
  - 发射会再叠加 TX 调制负载，默认门禁关，首次发射必须假负载。
- 依赖：`scipy>=1.12,<1.15`（A53 上 1.17.x 会 SIGSEGV）、`soxr`、`libfftw3`（WDSP）。
  1.28.38 盒子线已因 FT8 带前两个依赖；更老的盒子请走 `mrrc-update`（它会
  `pip install -r requirements.txt`），不要只用网页热修包——`box_update.apply`
  不会装 pip 依赖。WDSP 的 `.so` 与源码随 FP36 源码包分发，不需要在盒子上另装编译器。
- 盒子线 FP41 的源码包带 `profiles/sunsdr2dx.env`（含 `MRRC_WDSP=off`），升级后
  `mrrc-radio list` 应能看到 `sunsdr2dx`；也可以直接在 ☰ →「连接设置」选择。
- 真机验收前保持 `verified=False`；S 表未校准，不虚构 SWR/ALC。盒子上的复测命令：

```bash
scp dev_tools/bench_sunsdr.py mrrc@盒子:/tmp/
ssh mrrc@盒子 '/opt/mrrc_modern/venv/bin/python /tmp/bench_sunsdr.py --rate 78125'
```

判据：`total` 明显低于 100% 单核，且 78k 下 `rx_drops` 不持续增长；若接近/超过
100%，把带宽降回 78k、减少瀑布客户端，或不要在盒子上同时跑 FT8。
