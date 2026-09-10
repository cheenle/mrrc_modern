# 树莓派镜像包（rpi64）设计

日期：2026-09-09 · 状态：已批准 · 范围：新增打包线（服务端代码零改动）

## 1. 目标与产物

为 MRRC Modern 提供烧录即用的树莓派系统镜像：`MRRC-Modern-v<ver>-rpi64.img.xz`。

- 基座：**Raspberry Pi OS Lite 64-bit (Bookworm)**，目标硬件 **Pi 3B+ / 4 / 5**（Zero 2 W 同架构但 512MB 内存，标注"未验证"）。不做 32 位线、不做桌面版。
- 版本规则与 `build.sh` 相同：镜像内代码版本 = 构建 时 `CHANGELOG.md` 顶部条目。
- 体验：烧卡上电 → systemd 自启 → 浏览器开 `https://raspberrypi.local:8888`（Avahi 内置解析）。首启零配置：自动探测电台型号与串口、HTTPS 默认开（`ssl_bootstrap` 自签证书）、服务端绑定 `::` 双栈。

## 2. 密码路径（用户决策：C 预置为主）

- **主路径（C）**：烧卡前把 `mrrc.env`（`KEY=value` 格式，可含 `MRRC_WEB_PASSWORD`）放到 SD 卡 boot 分区（`/boot/firmware/mrrc.env`）。首启服务读到即采纳：拷贝到 `/opt/mrrc_modern/env/mrrc.env`（属主 `mrrc`，640），原文件改名为 `mrrc.env.applied`（幂等，二次启动不重复处理）。
- **兜底（仅当未预置）**：自动生成随机密码 + HDMI 控制台显示 + SSH motd + `sudo mrrc-show-password` 三处可见。登录页自动密码横幅**保持 loopback-only 不放松**——密码不暴露给局域网（I9 安全底线）。
- 电台探测：移植 `macos/first_run.py` 为 `linux/first_run.py`（仅 stdlib+pyserial）：扫 `/dev/ttyUSB*`、`/dev/ttyACM*`，先 FT-710（ASCII `ID;` @38400）后 Icom（CI-V `0x19` @115200），写 `MRRC_RADIO_MODEL`/`MRRC_SERIAL_PORT`/`MRRC_PORT_CONFIRMED=1`。多口设备（CP2105 双口）逐口探测。

## 3. 镜像内容（pi-gen stage4）

pi-gen 标准 stage 0–2（Lite Bookworm arm64）+ 自定义 stage4（仓库内 `packaging/rpi/pi-gen-stage4/`，构建时拷入 pi-gen 克隆）：

1. `apt install python3-venv python3-pip portaudio19-dev libopus0 avahi-daemon`（Bookworm 的 avahi 已在 Lite 中则幂等）。
2. 部署仓库代码到 `/opt/mrrc_modern`（构建期从仓库工作区拷入，与 Windows 源码 zip 同一排除规则）。
3. **构建期**创建 `/opt/mrrc_modern/venv` 并 `pip install -r requirements.txt`——首次开机零网络依赖（代价约 +250MB 镜像体积）。
4. 系统用户 `mrrc`（pi-gen `FIRST_USER_NAME=mrrc`，标准 sudo 组），附加 `dialout`、`audio` 组。
5. `/etc/systemd/system/mrrc-modern.service`：`User=mrrc`、`WorkingDirectory=/opt/mrrc_modern`、`EnvironmentFile=/opt/mrrc_modern/env/mrrc.env`、`ExecStart=/opt/mrrc_modern/venv/bin/python server.py --host :: --port 8888 --ssl-cert /var/lib/mrrc/certs/server.crt --ssl-key /var/lib/mrrc/certs/server.key`、`Restart=on-failure`、`After=network-online.target`。证书由首启生成到 `/var/lib/mrrc/certs/`。
6. `/etc/systemd/system/mrrc-firstboot.service`（oneshot）：处理预置 env → 无则跑 `linux/first_run.py` → `ssl_bootstrap.ensure_self_signed` 生成证书 → 写 motd → `systemctl disable` 自身。
7. `mrrc-show-password` 辅助命令（sudo 执行，打印 env 中的密码行）；`/opt/mrrc_modern/VERSION` 写入版本号。
8. SSH 预启用；主机名保持 `raspberrypi`（Avahi → `raspberrypi.local`）。

## 4. FT-710 真频谱策略（FTDI）

仓库内 `LibFT4222-v1.4.8.zip` 仅含 Windows 二进制，FTDI 官方 Linux ARM 包脚本下载被 403。因此：

- 镜像**不内置** Linux FTDI 库；`/opt/mrrc_modern/vendor/ftdi/` 目录预留（`scope_libraries.py` 的 `get_candidate_library_dirs()` 已搜索该目录与 `/usr/lib/aarch64-linux-gnu`）。
- 用户手动放入 aarch64 的 `libft4222.so` + `libftd2xx.so`（文档给出 FTDI 官方下载页与放置命令）即点亮真 FFT；缺失自动回落 S 表频谱——与 Windows 缺 DLL 行为一致，`MRRC_FTDI_LIB_DIR` 可指定自定义路径。
- IC-7300/MK2 不受影响（CI-V 走串口，无需 FTDI）。

## 5. 构建链（本机 Mac）

- Docker Desktop（Apple Silicon 的 Linux VM 即 aarch64）→ **arm64 镜像原生构建，无 qemu 仿真**，预计 15–30 分钟；scratch 约 10GB。
- 驱动脚本 `packaging/rpi/build-image.sh`：启动检查 Docker → 克隆 pi-gen（**锁定 release commit**，构建时记录于 pi_pack.md）→ 拷入 stage4 → 设 `IMG_NAME`/`FIRST_USER_NAME=mrrc`/`ENABLE_SSH=1`/`RELEASE=bookworm` → 执行构建 → 产物压缩为 `dist/rpi/MRRC-Modern-v<ver>-rpi64.img.xz` + SHA-256。
- 源码注入沿用 Windows 源码 zip 的排除规则（`.agents/` 必须含入、`certs/`/`promo/`/`dist/` 排除）。

## 6. 验证

- **构建闸门**（自动）：stage4 chroot 内断言——venv python 可用且关键依赖可导入、`server.py` 语法检查、service/firstboot 单元文件存在、`mrrc-show-password` 可执行。
- **镜像结构检查**（自动）：`xz -t` 完整性；体积上下界（0.6–2.0GB）；e2fsprogs `debugfs` 抽查 `/opt/mrrc_modern/VERSION`、service 文件内容。
- **真机验收**（操作员）：烧卡 → 首启路径（预置/兜底各一）→ 串口探测 → 浏览器 HTTPS 登录 → FT-710 CAT/音频、IC-7300 CI-V → USB 音频（ALSA 域名匹配是已知风险点，PortAudio 设备名含 "USB Audio" 应命中现有提示词表）→ FT4222 手动放库点亮真谱。

## 7. 发布与文档

- 镜像挂 `www.vlsc.net downloads/`（约 0.8–1.2GB，磁盘空闲 5.9G 足够）；网站 EN/ZH 新增树莓派下载卡片（Imager"自定义镜像"用法 + 预置密码说明）。
- 新文档：`docs/RASPBERRY_PI_GUIDE.md`（用户向：烧录/密码/接线/两电台差异/FTDI 手动点亮）+ `pi_pack.md`（构建方：Docker/pi-gen/锁定 commit/产物校验）。
- `dual-platform-release` skill 增补 Pi 线引用；SDD V2.36 条目与 CHANGELOG 随下次发版（镜像本身可独立于版本号立即发布，文件名带当前版本 `v1.14.1`）。

## 8. 明确不做（YAGNI）

- 不做在线自更新（更新 = Imager 重烧新镜像）；不做 32 位 armhf 线；不做桌面版；不做 PyInstaller 打包（Linux 直接 venv + systemd）；不改服务端 Python 代码；不做 Linux FTDI 库的自动下载。
