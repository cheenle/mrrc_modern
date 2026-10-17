# 独立 SDR 接收机（对外发布的 Web 接收器） 设计

- **日期**：2026-10-18
- **状态**：设计已批准（架构/协议/打包三段逐段确认），待实现
- **范围**：新增一个**独立的对外 Web 接收器**——用自己的页面、自己的口令入口，把插在盒子上的
  SDRplay RSP1 当成一台全频段（10 kHz–2 GHz）接收机发布出去；与无线电控制面零耦合
- **相关代码（待新增）**：`sdr_pipe.py`（独占设备的子进程）、`sdr_hub.py`（调谐仲裁 + 解调池 + 扇出）、
  `server.py`（`/WSsdr`、`/sdr`、`/api/sdr/*`）、`static/sdr.html`、`static/sdr.js`
- **相关代码（复用，不改协议）**：`static/rx_worklet_processor.js`（RX 播放抖动缓冲）、
  `static/modules/opus_codec.js` + `opus_wasm.js`（浏览器解码）、`static/ft710.css`（暗琥珀主题）、
  `opus_rx.py`（ctypes 调 libopus 的先例）、`backends/ft710/scope_pipe.py`（子进程 IPC 的先例）
- **相关文档**：`AGENTS.md`（模块表、环境变量）、`README.md`、`docs/sdr_receiver.md`（新增操作手册）、
  `tests/README.md`、`SDD/07`、`SDD/08`（新增 AD-029）、`SDD/09`、`SDD/10`、`SDD/12`、`SDD/14`

---

## 1. 背景：这颗 SDR 在盒子上"插着但用不了"，原因有三层

现场：W103D 盒子 `192.168.1.217`（MRRC-BG1SB，v1.28.1，同时在跑 FT-710 轮询与 FT8 解码），
2026-10-18 00:40 插入 **SDRplay RSP1**（`1df7:2500`，`Bus 001 Dev 7`，挂在内部 hub `1-1.1`，
与电台那串（CP2105/FT4222/C-Media，挂在 `1-2` 的 Cypress TetraHub）不共用 hub）。三层障碍，
**每一层都实测过**：

**① 内核 `msi2500` 驱动抢先认领，且它驱动不了 RSP1。** 设备一插上就 `Registered as swradio0`。
实测这个内核路径是坏的：`S_FREQ` 设 7 MHz / 25 MHz / 100 MHz / 433.92 MHz，读回**一律 15.000 MHz**
（被它的 tuner range 上沿钳死）；连采 604,800 B，直方图 `602,929` 落在 0–32、其余贴 255 ——
有效 I/Q 应铺满 0–255、均值≈127。**结论：内核路径不是"限制多"，是没有信号。**

**② SDRconnect 不支持 RSP1。** 官方 ARM64 包（SDRconnect 1.0.10 headless，87 MB）能在盒子上跑起来、
WebSocket 服务正常应答（API 1.0.3），但 `valid_devices` 只有 `"IQ File"`；root 运行同样如此
（排除 USB 权限）。SDRplay 论坛官方答复：**RSP1 不会在 SDRconnect 里被支持（硬件原因）**。
⇒ **SDRconnect 的 WebSocket API 这条路对本机不存在。**

**③ 官方 Hardware API 有 arm64 版，且明确支持 RSP1。** Hardware API 3.15 的 Linux 包（511,429 B
自解压）内含 `arm64/libsdrplay_api.so.3.15`（575,952 B）+ `arm64/sdrplay_apiService`。装完实测：

```
sdrplay_api_Open -> 0        api version -> 3.150
GetDevices      -> numDevs=1  ser='0000000001'  hwVer=1 (RSP1)
Init            -> 0
2 MSPS 5 s      -> 9,967,104 samples, rms −28.0 dBFS
```

采样率实测 **2 / 4 / 6 / 7 / 8 / 10 MSPS 全部 `Init=0`**（10 MSPS 1.5 s 收 13,634,208 样本）。
全频段调谐实测（同一根天线，rms）：**198 kHz → 4361，3.5 MHz → 1431，7.074 → 1059，
14.074 → 330，28.074 → 37，50 MHz 以上只有噪底**（该机天线是 HF 的，VHF/UHF 无覆盖 —— 不是设备问题）。

**CPU 实测**（客户端 + `sdrplay_apiService`，含把 IQ 落盘）：

| 配置 | 5 s 内 CPU | 折算 |
|---|---|---|
| 2 MSPS | client 1.016 s + service 0.746 s | ≈ 0.35 核 |
| 8 MSPS | client 2.622 s + service 2.557 s | ≈ 1.04 核 |

`Init` 约 1.5 s。**设备是独占的**——一个进程 `Init` 后，第二个进程 `Init` 立即失败
（实测：并发起两次探测，后者瞬间退出）。

**信道化（2 MSPS → 每通道窄带 → AM 检波）实测**，单秒实时流的 CPU：

| 实现 | 每通道 | 8 通道 | 16 通道 |
|---|---|---|---|
| numpy 单级融合多相（2 taps/phase，无选择性） | 0.09 | 0.62 | 1.22 |
| **C 两级信道化（真滤波器）· AM 6 kHz** | **0.086** | **0.69** | **1.38** |
| **C 两级信道化（真滤波器）· USB 3 kHz** | **0.151** | **1.23** | **2.42** |

（早先一版 4-taps/phase 的单级原型量到 0.025 核/通道，但那个滤波器没有边带选择性——
换来的是 SSB 完全压不掉另一边带。真实的两级结构把每通道成本抬到 0.086（AM）/0.151（SSB），
这是"既能抗混叠、又能选边带"的代价。**结论：默认 `MAX_CHANNELS=8`（AM 0.69 核、SSB 1.23 核），
上限可配到 16。**）

同一台机上另测：numpy 的复数 FFT 在 A53 上 ~0.5 ms/次（2048 点），共享 FFT 信道化器
（0.24–0.5 核 底噪 + 每通道切片/IFFT）并不比逐通道多相更优 —— 这是选逐通道多相的理由。

**与既有系统的关系**：FT-710 的 CAT 轮询、FT8 解码、录音、频谱全部走各自的通路，本设计
**不触碰任何一条**（§4 第 3 条说明了为什么连频谱帧都不能复用）。

---

## 2. 目标与非目标

**目标**：在盒子上发布一个**独立入口的公开接收器**（用户已定的四个答案）：

| 决定 | 取值 | 含义 |
|---|---|---|
| 受众 | **自己的独立页面** | 不并入 FT-710 控制页；独立 URL、独立前端 |
| 调谐 | **完全共享** | 任何听众可改中心频率，30 s 持有锁；带内偏移各自随意 |
| 容量 | **~50 人** | 并发硬上限 + 共享解调池 + 频谱自动降档 |
| 门 | **独立口令** | `MRRC_SDR_PASSWORD`，与无线电控制完全隔离 |

**非目标（V1 明确不做，YAGNI）**：

- 聊天、spotter、RDS、数字模式解调（**SDR 喂 FT8 不做**）；
- 录音/回放/定时任务；
- 多 SDR、多设备、RSPduo 双调谐；
- "跟随 FT-710 频率" —— 只留预设位（V2 再做，届时是一个按钮）；
- 任何形式的发射能力（本接收器**没有** PTT 通路，也不可能产生 PTT）。

---

## 3. 需求（可验收的硬指标）

1. **全频段可达**：天线所及范围内，中心频率可设 10 kHz–2 GHz（受 RSP1 上限），步进 1 Hz 输入。
2. **带宽窗**：默认 2 MSPS（2 MHz 窗）；操作者可切 4 MSPS；10 MSPS 只作为手动"全带宽模式"，
   且开启时并发通道上限自动降到 8。
3. **解调**：AM / USB / LSB / CW / NFM。带宽档：AM 6/9 kHz、SSB 1.8/2.4/3.0 kHz、
   CW 200/500 Hz、NFM 12.5 kHz。**WFM(200 kHz) 移到 V1.1**——它装不进"两级抽取到 48 kHz"
   的结构（200 kHz 的调频信号必须先在中间速率上鉴频，再抽取音频），需要第三条管线。
   **AGC 属解调器内部（固定策略，不可按人调）**；**静噪与音量属订阅者**（扇出时按各自参数施加）——
   这样十个人听同一点时只开一路解调，不会因为静噪旋钮不同而各开一路。
4. **延迟**：音频端到端 ≤ 400 ms（含 220 ms 抖动缓冲，与 NFR-063 同量级）。
5. **并发**：50 个 WS 客户端；24 路解调；**同 (offset, mode, bw) 复用同一路**
   （10 人同听 7.074 USB = 1 路解调，即使各自静噪/音量不同）。
6. **出口带宽预算**（最坏情况，见 §7）：音频 ≈ 8×4 KB/s = 32 KB/s；频谱 50×10 KB/s = 500 KB/s；
   降档后频谱 ≤ 125 KB/s。**总预算上限 5 Mbps**，超过即降档。
7. **安全**：口令用 `hmac.compare_digest` 比较（I9 的既有教训）；口令不落 URL（AD-024）；
   页面与接口**前缀安全**（云入口 `<call>.mrrc.vlsc.net/` 与 `www.vlsc.net/mrrc_modern/<call>/`
   两种形态都要工作）；**不暴露任何电台状态**（频率/模式/PTT 一概不出现在 SDR 页面与接口里）。
8. **隔离**：SDR 子进程崩溃、设备掉线、被内核驱动抢走 —— 无线电控制面与 FT8 **不受任何影响**。

---

## 4. 架构

### 4.1 三层与边界

```
浏览器  static/sdr.html + static/sdr.js        （独立页，不加载 ft710_main.js / ft710_ui.js）
   │  WS /WSsdr      口令 → 独立 sdr token（与无线电 token 互不识别）
   ▼
server.py —— sdr_hub.SdrHub
   │   · 调谐仲裁（中心频率 + 30 s 持有锁 + 频段预设）
   │   · 解调池：key=(offset_hz, mode, bw_hz) → 一路解调器 + N 个订阅者（静噪/音量在扇出时施加）
   │   · 容量闸门、频谱降档、二进制帧扇出、超时回收
   │  stdin/stdout 分帧 IPC（与 scope_pipe 同一套路：len 前缀 + 文本 JSON + 二进制段）
   ▼
sdr_pipe.py —— 独占设备的子进程（ctypes → libsdrplay_api.so.3）
       · Init 2–4 MSPS、调谐、增益/AGC、过载握手、掉线检测
       · 每 20 ms 块：FFT → u8 频谱帧；每通道一次 ctypes 调用进 libsdr_dsp
       · Opus 编码（48 kHz）；控制命令从 stdin 读
   │  ctypes（每块一次调用，纯计算、无 I/O）
   ▼
libsdr_dsp.so —— 极小的 C 库（aarch64，镜像构建期编译）
       · 融合 NCO + 多相抽取（4 taps/phase）+ 解调（AM/SSB/CW/NFM/WFM）→ PCM
       · 实测 0.025 核/通道；numpy 同算法 0.075 核/通道（见 §1 表）
```

### 4.2 三个取舍及其理由

1. **解调放子进程，不放 `server.py`。** 2 MSPS 的抽取是重活；放进事件循环会把 FT-710 轮询
   （7 个任务）与 FT8 解码一起拖慢。子进程里 numpy 做 DSP，`server.py` 只做扇出——与
   `audio_handler` 的线程模型、`scope_pipe` 的进程模型一致，且**崩溃隔离**：SDR 的段错误
   不该带走一个正在发射的电台。
2. **设备用 ctypes，热路径用 C。** 设备侧用 ctypes（`opus_rx.py` 已是先例；上面的验证脚本
   本身就是 ctypes 跑通的）——那里没有性能要求，只是几次调用。但**信道化 + 解调不能留在 Python**：
   实测同一算法 numpy 融合多相是 **0.075 核/通道**（2 taps/phase），C 是 **0.025 核/通道**
   （4 taps/phase，带 AM 检波）；24 通道下 numpy 要 1.84 核、C 只要 0.59 核。这台盒子还要跑
   FT-710 轮询与 FT8 解码，1.84 核不可接受。所以：**C 只负责"2 MSPS → 每通道窄带 → PCM"**
   这一个小函数，其余（设备、IPC、Opus、扇出）全部留在 Python。C 侧无 I/O、无全局可变状态，
   可以用合成信号单测（边带、幅度、抗混叠）。
   **平台范围因此收窄为盒子（Linux/aarch64）**：RSP1 本就插在盒子上；macOS/Windows 包
   携带 `MRRC_SDR=off`，页面显示"SDR 仅盒子版支持"。镜像侧只多一个 `.so`，不动
   PyInstaller/DMG 两条线。
3. **不复用 FT-710 的频谱通路。** SDR 用自己的帧格式（§5.2）。理由不是洁癖：`ft710_main.js` /
   `ft710_ui.js` 是受保护文件（`biome.json`/`.pi-lens.json` 双侧守卫），且 Android 端
   `SpectrumFrame.kt` **硬校验 1701 B 帧长**——任何复用都会把"改一处崩一个客户端"的风险
   带进那条线。这条边界由 AD-025 的既有教训支撑。

### 4.3 模块职责

| 模块 | 做什么 | 依赖什么 |
|---|---|---|
| `sdr_dsp.c` / `sdr_dsp.h` | 纯计算：融合 NCO+多相抽取+解调（AM/SSB/CW/NFM/WFM）→ PCM | 无（只用 libm） |
| `sdr_pipe.py` | 拥有设备；块循环；频谱 FFT；每通道一次 `sdr_dsp` 调用；Opus；状态/过载；掉线退出 | `libsdrplay_api`（ctypes）、`libsdr_dsp`（ctypes）、numpy、`opus_rx` |
| `sdr_hub.py` | 仲裁、池、容量、扇出、订阅生命周期（**纯逻辑，可无硬件测试**） | 无（注入 pipe 与时钟） |
| `server.py` | `/WSsdr` 握手与鉴权、`/sdr` 静态页、`/api/sdr/*`、生命周期 | `sdr_hub` |
| `static/sdr.js` | 瀑布渲染、点选、频段预设、解调控件、音频播放 | worklet + WASM Opus |

---

## 5. 协议

### 5.1 文本帧（JSON，沿用 `event_type`/`property`/`value` 的形状，便于与既有 WS 一致）

| 方向 | `event_type` | 语义 |
|---|---|---|
| S→C | `hello` | 能力：采样率档、解调模式表、`max_clients`、`max_channels`、频谱档 |
| S→C | `state` | 中心频率、span、持有者剩余秒数、听众数、通道数、当前频谱档 |
| C→S | `tune` | `value` = 目标中心频率 Hz |
| C→S | `channel` | `value` = `{offset_hz, mode, bw_hz}`（+ 订阅者参数 `squelch` / `volume`，不进池 key） |
| S→C | `channel_ack` | `value` = `{chid, reused:bool}` |
| C→S | `channel_close` | 关闭自己的通道（引用计数归零才真拆） |
| C→S | `preset` | 跳到频段预设（`80m` / `40m` / `20m` / `mb` / `fm` / `air`…） |
| S→C | `stats` | 每 5 s：`{overload_per_min, gRdB, dropped, clients, channels}` |
| S→C | `error` | `code` + 人类可读原因（容量满、调谐被锁、通道满、设备不可用…） |

### 5.2 二进制帧（首字节定类型）

| 类型 | 布局 | 说明 |
|---|---|---|
| `0x01` | `[01][seq:u16][n:u16][u8 × n]` | 频谱：n = 1024（默认）或 512（降档） |
| `0x02` | `[02][chid:u8][seq:u16][opus…]` | 音频：48 kHz，单声道 32 kbps，20 ms 帧 |

序号用于丢帧检测（客户端重排/丢弃，不重传）。**没有任何帧携带无线电状态。**

### 5.3 鉴权

`POST /api/sdr/login`（口令）→ 下发 `mrrc_sdr` cookie（30 天，与既有 token 同一存储与生命周期，
**带角色标签**，无线电端点拒绝该角色，反之亦然）。`/WSsdr` 按 AD-024 的顺序解析：
`Authorization: Bearer` → cookie → 弃用的 query。口令比对用 `hmac.compare_digest`。
`MRRC_SDR_PASSWORD` 为空 ⇒ **功能整体关闭**（`/sdr` 返回 404，`/api/sdr/login` 返回 403，
不建子进程）。这与 `MRRC_LISTEN_PASSWORD` 的语义一致。

---

## 6. 调谐仲裁（A：完全共享）

- 状态：`center_hz`、`hold_owner`（会话序号，不对外显示）、`hold_until = now + 30 s`。
- `tune` 到达：`hold_until <= now` 或无持有者 ⇒ 接受，成为持有者，广播 `state`；
  否则拒绝，回 `error` + `retry_after` 秒。
- **改中心频率后按绝对频率重算所有听众的 offset**（不是重新居中）；越界钳到窗边并提示。
- 任何人随时可改自己的 `offset/mode/bw/squelch/volume`，**不受锁约束**——锁只管中心频率。
  （改 `offset/mode/bw` 会切换到另一个解调池条目；改静噪/音量不换池。）
- 持有者断开 ⇒ 立即释放锁（不留 30 s 空窗）。
- 公开话术只显示"有人在 23 秒前调过"，**不显示是谁**。

---

## 7. 容量与降级

| 项 | 默认 | 环境变量 | 超额行为 |
|---|---|---|---|
| 并发客户端 | 50 | `MRRC_SDR_MAX_CLIENTS` | 建连后立即 `4003` + 原因 |
| 解调通道 | **8**（上限 16） | `MRRC_SDR_MAX_CHANNELS` | 拒绝该 `channel` 请求，**不踢人** |
| 频谱档 | auto | `MRRC_SDR_SPECTRUM=auto\|max\|low` | ≤12 人 1024 bin @10 Hz；>12 人 512 bin @5 Hz |
| 采样率 | 2 MSPS | `MRRC_SDR_SAMPLE_RATE` | 10 MSPS 时通道上限强制降到 8 |

**打点**：新增的 kind 进 `session_metrics`（socket 数、订阅者数、`audio_sdr`/`spectrum_sdr` 两个扇出
路径的上行字节、峰值），与 AD-023 的既有形态一致——hub 侧的容量决策等这组数。

**带宽预算**：音频按**解调通道**计（复用后不随人数线性涨），频谱按**客户端**计。
最坏 `24×4 + 50×10 = 596 KB/s ≈ 4.8 Mbps`；降档后 `24×4 + 50×2.5 = 221 KB/s ≈ 1.8 Mbps`。

**降级路径（全部有可见提示，不静默）**：

| 情况 | 行为 |
|---|---|
| 没插设备 / 被内核驱动占用 | 页头红条 + 具体原因（含 `blacklist msi2500` 提示），不建 pipe |
| pipe 崩溃 / USB 重枚举 | 广播"重连中" → 1/2/5/10 s 退避重启 → 恢复后回到上次中心频率 |
| 过载（RSP1 常见） | overload 率超阈值 ⇒ 自动降 6 dB（下限 gRdB 20），广播"已自动降增益" |
| 音频背压 | 有界队列，满则丢帧并计 `dropped`（沿用 `queue_drops` 的既有语义），**绝不阻塞主循环** |
| 客户端慢 | 每订阅者有发送队列上限；慢客户端丢帧，不拖累他人 |

---

## 8. 前端页面

- 新文件 `static/sdr.html` + `static/sdr.js`；**不加载** `ft710_main.js`/`ft710_ui.js`。
- 布局：全宽瀑布（2–4 MHz）+ 频率刻度 + 中心/偏移双读数 + 频段预设条 + 解调区
  （模式 / 带宽 / 静噪 / AGC / 音量）+ 电平表 + 右上角 `听众 N · 调谐锁定 23 s`。
- 交互：点瀑布＝选偏移（本地立即高亮）；拖动＝平移中心（锁定时按钮变灰 + 倒计时）；
  滚轮＝步进；直接键入 kHz。移动端瀑布高度自适应。
- 复用：`rx_worklet_processor.js`（播放抖动缓冲）、WASM Opus 解码、`ft710.css` 主题变量。
- 无设备时只留状态条 + 原因；**页面永远不显示电台的频率/模式/PTT**。

---

## 9. 打包与部署

- 进 `packaging/common/mrrc-service-layer.sh`（两条镜像线共用）：
  `arm64/libsdrplay_api.so.3.15` → `/usr/local/lib`（+ `so.3`/`so` 符号链接、`ldconfig`）、
  `sdrplay_apiService` → `/opt/sdrplay_api/`、`sdrplay.service`（root）、
  `66-sdrplay.rules`（`1df7` MODE 0666）、`/etc/modprobe.d/mrrc-sdrplay.conf`
  （`blacklist msi2500` / `blacklist msi001`）。
- **许可证**：`sdrplay_license.txt` 一并进镜像；`docs/sdr_receiver.md` 注明来源、版本与限制
  （仅随 SDRplay 硬件使用）。
- `MRRC_SDR=off`（默认 on）⇒ 整块不装、不启动，给没有 SDR 的机器；`tests/test_packaging_parity.py`
  必须继续绿。
- `linux/mrrc_update.sh` 幂等写入上述文件，**不覆盖**用户对 blacklist/udev 的既有改动。
- 公开入口沿用 `deploy_listen_proxy.sh` 的模式（nginx 前缀 + 反代 `/WSsdr`），
  不做新的隧道机制。

---

## 10. 测试策略

**纯逻辑（无硬件，进 `python -m unittest`）**：

- 持有锁：首次成功 / 被锁拒绝 / 到期可抢 / 持有者断开即释放 / 剩余秒数计算（注入时钟）；
- 解调池：同 key 复用（引用计数）、不同 key 独立、全部退订才拆、通道满拒绝；
- 改中心后**绝对频率不变**的 offset 重算（含越界钳制）；
- 容量闸门与频谱降档的切换点（11→12→13 人）；
- 帧编解码（0x01/0x02 的长度与前缀）、`seq` 回绕；
- 口令 → token、角色隔离（sdr token 打不开 `/WSradio`，operator token 打不开 `/WSsdr`）；
- 降级状态机（设备缺失 / 崩溃 / 退避序列）；
- 路径前缀安全（两种入口形态）。

**假 pipe**：脚本化 `sdr_pipe`（读脚本、写帧）验证扇出、背压、重连。

**硬件手测 `_diag_sdr.py`**（仿 `_diag_civ.py`，只读 + 不长跑）：设备识别、`Init`、
2/4/8/10 MSPS、各解调模式的输出电平与 SINAD、过载降增益、拔插恢复、`blacklist` 缺失时的
报错文案。

**性能门槛**：2 MSPS + 8 通道时 pipe 的 CPU < 0.6 核（实测脚本出数，门槛写进测试）。

---

## 11. 文档同步（与代码同一 commit）

| 改动 | 同步项 |
|---|---|
| 新增 AD | `SDD/08` **AD-029：独立 SDR 接收机**（独占设备子进程 + 共享解调池 + 持有锁） |
| 新 WS 端点 / 数据流 | `SDD/07`、`SDD/09`（订阅者扇出与频谱分档的并列位置） |
| 模块职责 | `AGENTS.md` 模块表（`sdr_pipe.py` / `sdr_hub.py`）、`README.md` |
| 环境变量 | `SDD/12` §12.2 表（`MRRC_SDR`、`MRRC_SDR_PASSWORD`、`MRRC_SDR_MAX_CLIENTS`、`MRRC_SDR_MAX_CHANNELS`、`MRRC_SDR_SPECTRUM`、`MRRC_SDR_SAMPLE_RATE`） |
| 打包 | `packaging/box/README.md`、`docs/w103d_pack.md`（系统层多出来的 daemon/udev/blacklist） |
| 操作手册 | **`docs/sdr_receiver.md`**（天线、增益、过载、故障对照） |
| 测试 | `tests/README.md`（模块与用例数） |
| 版本历史 | `SDD/14` 新条目 + `SDD/README.md` Quick Facts |

---

## 12. 风险与未决

| # | 风险 | 处置 |
|---|---|---|
| R1 | **RSP1 已停产**，若作为产品卖点，货源与替换型号（RSP1B/RSPdx）要先定 | V1 只做"有 SDR 就能用"；页面与 pipe 通过 `hwVer` 报告型号，不绑定 RSP1 |
| R2 | 50 人共用一台单调谐器，某人停在死频段上会招致抱怨 | 30 s 锁 + 显示"上次调整距今"；V2 再评估投票/排队 |
| R3 | 家庭上行带宽 | 频谱自适应降档（实测档位见 §7），上限 5 Mbps |
| R4 | 闭源二进制（`libsdrplay_api` + root daemon）进镜像 | 许可证随包；`MRRC_SDR=off` 可整块关闭；升级脚本不覆盖用户定制 |
| R5 | 天线决定体验：HF 天线在 VHF/UHF 只有噪底 | 操作手册写明；页面对"只有噪底"不报警（那是物理） |
| R6 | ~~2 MSPS 抽取的开销可能高于估算~~ **已关闭** | 实测：numpy 0.075 核/通道、C 0.025 核/通道（4 taps/phase）。24 通道取 C 方案：0.59 核 + Opus ≈ 0.9 核 |
| R9 | 新增一个原生 .so（构建期编译） | 只在两条镜像线构建（`packaging/common`），与 FT8 的 `libwsjt_core` 同一套机制；桌面包 `MRRC_SDR=off`，不引入新构建路径 |
| R10 | 每通道成本 ≈ 4.5×fs_in（物理下限），8 通道已是 0.7–1.2 核 | V1 用逐通道多相（简单、已测）；**V2 若要更多通道，改共享 FFT 信道化器**：一次处理 2.4 MSPS、按听众取 bin，实测依据见 §1（C 的 FFT 比 numpy 快一个量级）。这条升级路径写进 AD-029 的"后果"里 |
| R7 | USB 重枚举在本机真实发生过（00:44 同机 `ttyUSB0` 重建） | pipe 掉线检测 + 退避重连（§7），并写入 `_diag_sdr.py` 的手测项 |
| R8 | 解调正确性（SSB 边带、WFM 去加重、CW 音调）无客观判据 | `_diag_sdr.py` 对已知信号（本地 FM 台 / WWV / 已知信标）记录 SINAD 与听感，写入手册 |

**未决（实现前需确认）**：WFM 是否要在 V1 就要立体声（当前设计是单声道）。
`MAX_CHANNELS` 默认 24 已由实测定为可行（§1 实测表、R6）。

---

## 13. SDD 引用

- **I6**（无多客户端仲裁，频率/模式仍是 last-writer-wins）：本设计的 §6 就是该缺口要求的
  「single-controller lock」形状，但**只作用在 SDR 这一个面上**，不回头改电台控制面的既有语义。
- **AD-024**（令牌传输：cookie → header → 弃用 query）：`/WSsdr` 的鉴权顺序沿用，绝不进 URL。
- **AD-025**（频谱档位：形状 × 分频，以 caps 协商为闸门）与 **NFR-003**：SDR 的频谱降档
  **借用同一思路但不复用同一通路**（Android 端硬校验 1701 B）。
- **NFR-004 / NFR-061 / NFR-063 / NFR-064 / NFR-001**：音频带宽、Opus 码率、抖动缓冲、PCM 回退、
  RX 端到端 < 500 ms 的既有目标（本设计的 §3.4 取 ≤ 400 ms，更严）。
- **AD-023 / `session_metrics`**：新端点必须打点（socket 数、订阅者数、上行字节），
  hub 侧的容量决策依赖它。
- **AD-028**（本地控制面）：**新设备面**的先例——设备相关的 I/O 与纯逻辑分离，逻辑可无硬件测试。
- **I8**（静态路径穿越）、**I9**（非常量时间口令比较）、**I10**（子通道无独立重连）：
  本设计**不得**重犯，对应位置见 §5.3、§10。
- **NFR-068**（数据所有权）：本功能不新增任何外发请求；接收到的射频内容留在本机。
