# FT-710 Web Control — 把短波电台搬进浏览器

## 这是什么？

FT-710 Web Control 是一个开源项目，让你可以用**手机、平板或电脑的浏览器**远程操控 [Yaesu FT-710](https://www.yaesu.com/) 短波电台。不需要安装 App，打开网页就能操作——调频率、切换模式、看频谱瀑布图、收发音频，全在浏览器里完成。

![FT-710 Web Control 界面截图](../IMG_8888.PNG)

> **说人话版：** 你的 FT-710 电台接在书房电脑上，你躺在客厅沙发上用手机就能听短波、调频率。或者电台在老家，你出差在外也能远程操作。

## 为什么要做这个？解决了什么问题？

Yaesu FT-710 是一台优秀的 HF/50MHz 电台，但它的操作方式有两个痛点：

1. **必须坐在电台前操作。** 旋钮、按钮都在机器面板上，你得待在电台旁边。想在阳台晒太阳的时候扫扫频？不行。想躺在床上用手机看看 20 米波段有没有传播？不行。

2. **原厂远程方案不够灵活。** Yaesu 官方的 SCU-LAN10 远程套件需要额外购买硬件（约 $200），而且配套软件只支持 Windows。Mac 用户、Linux 用户、手机用户呢？对不起，不支持。

这个项目就是来解决这两个问题的：

| 痛点 | 解决方案 |
|------|---------|
| 只能坐在电台前 → | **任何有浏览器的设备都能操控**：手机、平板、笔记本 |
| 原厂方案贵且只支持 Windows → | **免费、跨平台**：macOS / Linux / Windows / 树莓派都能跑 |
| 没有频谱显示 → | **实时 FFT 频谱折线图 + 瀑布图**：看信号强度一目了然 |
| 没有音频传输 → | **双向音频**：既能听收音，也能对着手机话筒通联 |

## 核心功能一览

### 📡 电台操控
- **频率调节**：8 位数字频率显示，支持步进调节（10Hz ~ 25kHz），点击频率可直接输入
- **模式切换**：USB / LSB / CW / AM / FM / RTTY / DATA，一键循环或弹出列表选择
- **波段切换**：160m ~ 4m 共 12 个业余波段，一键跳转
- **VFO 双通道**：A/B 切换、A=B 复制、Split 异频操作
- **滤波器**：根据模式自适应，UI 循环常用语音/窄带带宽（后端 CAT 支持完整 23 档语音 / 21 档窄带索引）
- **ATT / PRE**：衰减器和前置放大器逐档循环
- **DSP 功能**：NR（降噪）、NB（消脉冲）、AN（自动陷波）、COMP（语音压缩）、ATU（天调），一键开关
- **PTT 发射**：按住说话，松手停止——带安全看门狗保护，不会卡发射

### 📊 仪表与可视化
- **FFT 频谱折线图**：实时显示当前频谱幅度，青色折线带网格参考线，信号峰高度直观、突变平滑（EMA 平滑 + 2× 幅度增益）
- **瀑布图**：850 点实时频谱，120 行历史，6 种配色可选（Jet / Hot / Cold / Thermal / Night / Gray），Floor/Ceil 滑块独立调节显示范围
- **频率刻度**：自动适应跨度，显示 MHz/kHz 刻度
- **S 表**：S0 ~ S9+60dB 条状表，dBm 数字读数
- **多功能表**：PWR（功率 W）、ALC、SWR（驻波比）、Id（漏极电流 A）、Vd（漏极电压 V）

### 🎙️ 音频传输
- **RX 接收音频**：电台声卡采集 → Opus 压缩（64kbps，原始 PCM 768kbps 的 1/12）→ 浏览器实时播放
- **TX 发射音频**：手机/电脑麦克风采集 → Opus 编码 → 送至电台调制输出
- **Opus 编解码**：带宽低、音质好，弱网环境也不卡顿。无 Opus 库时自动降级为 PCM 传输

### 🔒 安全
- 登录密码保护
- PTT 松手即停（dead-man switch），三重 CAT 确认发射已停止
- 关闭网页自动强制停止发射
- WebSocket 连接断开时服务器自动切回接收

## 安装指南

> **你只需要一台连着 FT-710 的电脑（macOS 或 Linux），以及一点命令行的耐心。** 跟着下面步骤走，大部分情况 10 分钟搞定。

### 前置条件

1. 一台安装了 **macOS 13+** 或 **Linux**（Debian/Ubuntu/Fedora/Arch/树莓派）的电脑
2. FT-710 电台，用 USB 线连接到这台电脑
3. 电脑和手机/平板在**同一个局域网**（如果你想用手机操作的话）

### 超简易安装：一行命令脚本

如果你用的是 macOS 或 Linux，项目自带了一个 **全自动安装脚本**，它会帮你搞定一切——检测系统、装好所有依赖、找到电台的串口和声卡、生成配置文件。

```bash
# 进入项目目录
cd FT710

# 运行安装脚本
./install.sh
```

脚本会自动完成以下步骤：
1. 检测操作系统和架构（macOS arm64/Intel、Debian/Ubuntu/Fedora/Arch）
2. 安装 Homebrew（macOS 缺失时）、Python 3.11+、PortAudio、libopus
3. 创建 Python 虚拟环境，安装 `requirements.txt`
4. 扫描并识别 FT-710 的串口（CAT 控制端口和 Scope 频谱端口）
5. 检测 USB 声卡设备
6. 生成 `.env` 配置文件（含随机登录密码）
7. 验证所有模块是否能正常导入

脚本跑完后，你会看到类似这样的输出：

```
  Configuration:
    Server URL:    https://localhost:8888
                   (只监听 TLS —— 首次访问浏览器会提示自签名证书，点「继续」即可)
    Login password: a1b2c3d4e5f6g7h8
    Serial port:   /dev/cu.usbserial-0121DB3A0
```

记住这个**密码**，稍后登录需要用到。

### 不想用自动脚本？手把手手动安装

#### macOS 用户

```bash
# 1. 安装 Homebrew（如果还没有的话）
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 2. 安装 Python 和音频库
brew install python@3.12 portaudio opus

# 3. 进入项目目录，创建虚拟环境
cd FT710
python3.12 -m venv venv
source venv/bin/activate

# 4. 安装 Python 包
pip install -r requirements.txt

# 5. 找到 FT-710 串口（插上 USB 线后）
ls /dev/cu.SLAB_USBtoUART*
# 会看到类似 /dev/cu.SLAB_USBtoUART 的设备

# 6. 启动服务器
FT710_SERIAL_PORT=/dev/cu.SLAB_USBtoUART python server.py
```

#### Linux（Debian / Ubuntu）用户

```bash
# 1. 装系统依赖
sudo apt update
sudo apt install -y python3.12 python3.12-venv portaudio19-dev libopus0

# 2. 把自己加入 dialout 组（否则没法访问串口）
sudo usermod -a -G dialout $USER
# 注销重新登录，或者：
newgrp dialout

# 3. 进入项目，创建虚拟环境
cd FT710
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 4. 启动（串口一般是 /dev/ttyUSB0）
FT710_SERIAL_PORT=/dev/ttyUSB0 python server.py
```

#### Windows 用户

Windows 11/12 推荐使用桌面安装包。安装包内置 Python 运行时，双击启动本地服务器并打开浏览器；关闭启动窗口即停止服务器。若构建安装包时放入 `FT4222.dll` 与 `ftd2xx.dll`，Windows 也可使用 FT4222 真频谱；缺少 DLL 或设备初始化失败时自动降级为 S 表推算频谱。

```powershell
# 构建安装包（在 Windows 上）
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt pyinstaller

# 可选但是真频谱需要：
# 将 FT4222.dll 和 ftd2xx.dll 放到 vendor\ftdi\windows\bin\x64

packaging\windows\build.ps1
dist\windows\MRRC-FT710-Setup.exe
```

如果要用开发模式手工运行：

```powershell
cd mrrc_ft710
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt

# 在设备管理器里找到 Enhanced COM 端口号（如 COM3），然后：
set FT710_SERIAL_PORT=COM3
python server.py
```

### 设置为开机自启（可选）

服务器跑起来之后你可能想让它开机自动启动：

**macOS：**
```bash
./install.sh --install-service
# 会在 ~/Library/LaunchAgents/ 下创建 launchd 配置
```

**Linux：**
```bash
./install.sh --install-service
# 会在 /etc/systemd/system/ 下创建 systemd 服务
```

## 如何使用

### 第一步：启动服务器

```bash
cd FT710
source venv/bin/activate     # 激活虚拟环境（如果还没激活）
FT710_SERIAL_PORT=/dev/cu.SLAB_USBtoUART python server.py
```

看到以下日志说明启动成功：
```
Uvicorn running on https://0.0.0.0:8888
```

### 第二步：打开浏览器

在**连到同一个局域网的任何设备**上打开浏览器，访问：

```
http://<电脑的IP地址>:8888
```

比如电脑 IP 是 `192.168.1.100`，就在手机浏览器里输入 `http://192.168.1.100:8888`。

> 💡 **怎么知道电脑的 IP？**
> - macOS：系统设置 → 网络 → Wi-Fi → 详细信息，找到 IP 地址
> - Linux：终端输入 `ip addr show | grep inet`
> - 或者启动服务器后，看日志里的 Uvicorn 地址

### 第三步：登录

输入安装脚本给你的密码（或者你自己在 `.env` 文件里设的密码），点击登录。

### 第四步：开始操作！

- **看频谱**：页面顶端是 FFT 频谱折线图和瀑布图，可以直观看到频段上的信号活动
- **调频率**：点击底部的 ◀ ▶ 按钮调谐，或者直接**点击频率数字**手动输入
- **切模式**：点击模式按钮（USB/LSB）循环切换
- **发射**：按住红色 PTT 按钮说话，松手停止

### 常用操作小贴士

| 你想要的 | 怎么做 |
|---------|-------|
| 换个波段 | 点击顶部 Band 按钮循环切换 |
| 扫频看看有没有信号 | 打开菜单（左上角汉堡图标）→ 调大 SPAN 到 200k 或 500k |
| 瀑布图太暗看不清 | 菜单 → Scope Display → 调低 Floor 滑块 |
| 信号太亮一片红 | 菜单 → 调高 Floor，或调低 Ceil |
| 换瀑布图颜色 | 菜单 → Color 选 Night（夜间友好）或 Thermal（暖色） |
| 存一个频率 | 长按 M1-M6 记忆按钮（约 1 秒，有振动反馈） |
| 调用记忆频率 | 点击 M1-M6 按钮 |
| 手机全屏模式 | 点击右上角 ⛶ 按钮 |
| 保持屏幕不灭 | 点击右上角 ☀ 按钮 |

### 手机上的效果

这套界面专门针对手机优化过：
- **iPhone Safari**：支持安全区域（刘海/灵动岛不遮挡内容）、PWA 添加到主屏幕
- **Android Chrome**：全屏沉浸式体验
- PTT 按钮底部固定，单手操作方便

## 常见问题

### Q: 启动时报错 "Serial port not found"

**A:** 确认 FT-710 已经开机且 USB 线已连接。用以下命令确认串口名称：

```bash
# macOS
ls /dev/cu.SLAB_USBtoUART*

# Linux
ls /dev/ttyUSB*
```

如果看不到设备，换一根 USB 线试试——有些充电线不带数据功能。

### Q: 音频没声音

**A:** 检查几点：
1. 电台的 AF Gain（音量）旋钮是否调到了合适位置
2. macOS：系统偏好设置 → 隐私 → 麦克风 → 允许终端 / Python
3. 浏览器是否允许了音频自动播放

### Q: 频谱图不显示（只有一条线）

**A:** 这是正常的。频谱数据需要 FT4222 芯片（电台内部 SPI 接口）。如果 FT4222 库没装好，服务器会自动用 S 表读数推算频谱——虽然没有真实 FFT 精确，但也能反映频段上的信号活动。

### Q: 我怎么在外网访问家里的电台？

**A:** 出于安全考虑，不建议直接把服务器暴露到公网。推荐方案：
1. 在家里的路由器上配置 **WireGuard VPN** 或 **Tailscale**（免费，超简单）
2. 出门在外时手机连上 VPN，就跟在家一样访问了

### Q: 可以同时多人操作吗？

**A:** 可以多人同时打开网页**看**频谱和 S 表数据。但 CAT 控制（调频率等）是共享的——谁调了大家都看到变化。音频 TX 通道同一时间只有一个客户端能送话，防止两个人同时按 PTT 导致音频混乱。

## 技术架构（给想深入了解的朋友）

```
浏览器（手机/平板/电脑）
  ↕ HTTP + WebSocket（4 个独立通道）
Python FastAPI 服务器
  ├── Serial CAT 协议 ↔ FT-710 Enhanced COM Port（38400bps）
  ├── FT4222 SPI 频谱采集 ↔ 电台内部 DSP（850 点 FFT，~21fps）
  └── USB Audio 双向传输 ↔ 电台声卡（44.1kHz 单声道）
```

- **后端**：Python 3.11+ / FastAPI / Uvicorn
- **前端**：单页 HTML + 原生 Canvas 2D + Web Audio API + WebSocket
- **音频编码**：Opus 64kbps（自动降级 PCM）
- **CAT 协议**：Yaesu 标准串口指令集，7 层轮询策略（100ms ~ 5s）
- **频谱**：FT4222 硬件直读（~21fps）或 S 表推算作为降级方案

## 更新日志

近期改进：
- **2026-07**：新增 FFT 频谱折线图（瀑布图上方 33px，青色折线 + 网格），EMA 平滑衰减让信号峰更容易观察
- **2026-06**：频谱频率刻度从 CAT 同步的 VFO 中心频率推算，不再依赖异步的 scope 帧数据
- **2026-05**：音频增益 5× boost 补偿 FT-710 USB 音频偏小的问题；iOS Safari AudioWorklet 优化
- **2026-04**：安装脚本 `install.sh` 支持全平台自动检测，一键部署

## 开源协议

MIT License。欢迎提 Issue 和 PR，一起把短波电台的远程操控做得更好。

---

**项目地址：** [GitHub](https://github.com/your-repo)

**有问题？** 在 GitHub 上提 Issue，或者发邮件到项目维护者。
