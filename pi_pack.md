# Raspberry Pi 镜像打包流程（rpi64）

> 用途：构建并发布 `MRRC-Modern-v<ver>-rpi64.img.xz` —— Raspberry Pi OS Lite 64-bit (Bookworm) 定制镜像，烧卡即用、首启零配置。规格见 `docs/superpowers/specs/2026-09-09-raspberry-pi-image-design.md`。
> 最新构建：**v1.14.1**（2026-09-09，构建主机 ham.vlsc.net x86_64 + qemu-user-static 交叉构建；产物 539,720,392 bytes，SHA-256 `b13778c2fe6f72e6f30229214b6837f950b551596d8c562f952b0c27001fdb43`；chroot 闸门（deps OK/scope libs OK）与 debugfs 抽查通过；真机烧卡验收留操作员。）
> 用户向的安装/使用说明见 `docs/RASPBERRY_PI_GUIDE.md`。

## 1. 环境拓扑

两条构建路径（首选本机，资源不足时用构建机）：

```
首选：本机 Mac (Apple Silicon)
   Docker Desktop 的 Linux VM 就是 aarch64 → pi-gen 原生构建，无仿真，15–30 分钟
备选：ham.vlsc.net (x86_64, Ubuntu 24.04, 2C/59G, sudo 免密)
   apt 装 qemu-user-static + binfmt-support → qemu 交叉构建，慢（约 1–2.5 小时）
```

- pi-gen 锁定 ref：`2026-06-18-raspios-bookworm-arm64`（升级 ref = 计划性变更：重跑全量构建 + 真机验收，并在本文件记录新 ref）。
- 自定义 stage 在仓库 `packaging/rpi/pi-gen-stage4/`，构建时由脚本拷入 pi-gen 工作区（`STAGE_LIST="stage0 stage1 stage2 rpi-stage4"`）。

## 2. 一次性准备

**本机路径**：Docker Desktop 已安装（`open -a Docker` 能起）；`brew install e2fsprogs`（verify 用 debugfs）；≥20GB 空闲磁盘。

**ham 路径**：`sudo apt-get install -y docker.io qemu-user-static binfmt-support`（docker daemon 需 `sudo docker info` 可用；binfmt 注册后 `ls /proc/sys/fs/binfmt_misc/ | grep -c qemu` ≥ 20）。源码用 rsync 同步过去（见 §3）。

## 3. 每次构建流程

```bash
# ── 本机路径 ──
open -a Docker; sleep 30                      # 等 daemon
packaging/rpi/build-image.sh                  # 版本号自动取自 CHANGELOG 顶部条目
packaging/rpi/verify-image.sh dist/rpi/MRRC-Modern-v<ver>-rpi64.img.xz
shasum -a 256 dist/rpi/MRRC-Modern-v<ver>-rpi64.img.xz

# ── ham 备选路径 ──
rsync -a --exclude ".git/" --exclude "dist/" --exclude "build/" --exclude "venv/" \
      --exclude ".venv/" --exclude "certs/" --exclude "promo/" --exclude "FT710Mobile/" \
      --exclude "FT710Android/" --exclude "logs/" --exclude "__pycache__/" \
      --exclude "*.pyc" ./ ham.vlsc.net:/home/cheenle/mrrc_modern_build/
ssh ham.vlsc.net "cd /home/cheenle/mrrc_modern_build && \
  MRRC_PI_WORK=/home/cheenle/build/pi-gen \
  MRRC_PI_OUT=/home/cheenle/mrrc_modern_build/dist/rpi \
  nohup sudo -E packaging/rpi/build-image.sh > /home/cheenle/rpi_build.log 2>&1 &"
ssh ham.vlsc.net "tail -5 /home/cheenle/rpi_build.log"        # 轮询进度
scp ham.vlsc.net:/home/cheenle/mrrc_modern_build/dist/rpi/*.img.xz dist/rpi/
```

**构建闸门**（自动，缺一即败）：stage4 chroot 内 `py_compile server.py/first_run.py/firstboot_wrapper.py`、venv 内 `import fastapi, uvicorn, serial, pyaudio, numpy, cryptography`、`import scope_libraries`。镜像后验：`verify-image.sh` 做 xz 完整性 + 体积界（0.4–2.0GB）+ debugfs 抽查（VERSION / service 单元 / venv python）。

## 4. 发布到网站

```bash
scp dist/rpi/MRRC-Modern-v<ver>-rpi64.img.xz www.vlsc.net:/tmp/MRRC-Modern-v<ver>-rpi64.img.xz.new
ssh www.vlsc.net "sudo -n mv /tmp/MRRC-Modern-v<ver>-rpi64.img.xz.new /var/www/vlsc.net/mrrc_modern/downloads/ && sudo -n chown www-data:www-data /var/www/vlsc.net/mrrc_modern/downloads/MRRC-Modern-v<ver>-rpi64.img.xz && sudo -n chmod 644 /var/www/vlsc.net/mrrc_modern/downloads/MRRC-Modern-v<ver>-rpi64.img.xz"
# 网站卡片（EN/ZH index.html）回填实测大小 + SHA-256 后：cd website && echo y | ./deploy.sh
curl -sI https://www.vlsc.net/mrrc_modern/downloads/MRRC-Modern-v<ver>-rpi64.img.xz   # 200 + content-length
```

同步更新的文档：`website/index.html`、`website/zh/index.html`（Pi 卡片）、`docs/RASPBERRY_PI_GUIDE.md`（版本号）、本文件头部"最新构建"行、SDD 版本历史。

## 5. stage4 结构速查

```
packaging/rpi/pi-gen-stage4/
├── prerun.sh                              # copy_previous（承接 stage2 根文件系统）
#   构建时另生成：EXPORT_IMAGE 标记（stage4 导出最终镜像）+ stage2/SKIP_IMAGES
├── 00-install-packages/00-packages       # python3-venv pip portaudio19-dev libopus0 avahi-daemon e2fsprogs
└── 01-deploy-mrrc/
    ├── 00-run.sh                          # chroot：venv、mrrc 用户(dialout/audio)、服务启用、构建闸门
    └── files/
        ├── etc/systemd/system/mrrc-modern.service     # User=mrrc, EnvironmentFile, Restart=on-failure
        ├── etc/systemd/system/mrrc-firstboot.service  # oneshot, ConditionPathExists=!firstboot-done, 输出到 console
        ├── etc/update-motd.d/10-mrrc                  # SSH 登录提示
        ├── usr/local/bin/mrrc-show-password           # sudo 查看自动生成的网页密码
        └── opt/mrrc_modern/                            # 构建时 build-image.sh 注入代码树 + VERSION
            └── linux/firstboot_wrapper.py             # 预置采纳 → first_run → 证书 → 默认值 → disable 自身
```

密码路径：烧卡前把 `mrrc.env` 放到 SD 卡 boot 分区（`/boot/firmware/mrrc.env`）→ 首启采纳（原文件改名 `.env.applied`）；未预置 → 自动生成（HDMI 控制台 + SSH motd + `sudo mrrc-show-password` 三处可见；登录页横幅保持 loopback-only）。

## 6. 故障排查

| 现象 | 原因 | 处理 |
| ------ | ------ | ------ |
| `ERROR: Docker daemon not running` | Docker Desktop 未启动 | `open -a Docker` 等 30 秒重试 |
| `need >=20GB free` | 本机磁盘不足 | 清理，或走 ham 备选路径 |
| loop-mount 失败（Docker Desktop） | privileged 容器缺 /dev/loop | 走 ham 路径（本文件 §3） |
| ham 上 pi-gen clone 慢/失败 | GitHub 网络波动 | 本机 clone 后 `rsync -a build/pi-gen ham.vlsc.net:` 再继续 |
| 构建中途 pip/apt 网络错误 | 瞬态 | 重跑（pi-gen stage 缓存：`touch` 已完成 stage 的 `.SKIP` 之外的缓存机制按提示续跑） |
| `TARGET_ARCH` 不被识别 | pi-gen ref 太老 | 确认 ref ≥ 2022 版本；本文件锁定 ref 已验证 |
| 镜像超体积界 | venv 膨胀/误入大文件 | `debugfs` 进像 du 排查；检查 rsync exclude 是否漏改 |
| 首启没生成密码（真机） | firstboot 依赖 venv 失败 | HDMI 看 console 报错；`journalctl -u mrrc-firstboot` |

## 7. 真机验收清单（操作员，每次镜像更新）

1. 烧卡（Imager → 使用自定义镜像）→ 上电 → 约 90 秒后服务起。
2. 兜底路径：不预置 → HDMI 控制台出现密码横幅；`ssh mrrc@raspberrypi.local`（密码 mrrc）→ motd 提示 → `sudo mrrc-show-password`。
3. 预置路径：boot 分区放 `mrrc.env`（含 `MRRC_WEB_PASSWORD`）→ 首启采纳、`.env.applied` 出现。
4. 浏览器 `https://raspberrypi.local:8888`（自签警告一次）→ 登录。
5. FT-710：CP210x 双口自动探测（`MRRC_PORT_CONFIRMED=1`）、CAT、USB 音频 RX/TX。
6. IC-7300：单口 CI-V 探测、CAT/频谱。
7. FT-710 真谱：手动放 FTDI aarch64 库后点亮；缺库时 S 表回落正常。
8. 断电重启：服务自启、密码不变、`mrrc-firstboot` 不再执行。
