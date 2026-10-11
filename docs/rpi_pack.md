# 树莓派镜像构建手册

面向**构建方**（不是用户——用户向文档是 `docs/RASPBERRY_PI_GUIDE.md`）。
本手册与 `docs/w103d_pack.md` 同构：两条镜像线共用同一套服务层与同一份排除清单，
差别只在平台壳（`packaging/rpi/` vs `packaging/box/`）。

## 一键

```bash
packaging/rpi/build-image.sh          # 全流程（首次全量 1 小时级，见"实跑记录"）
```

产物：`dist/rpi/MRRC-Modern-v<ver>-rpi64.img.xz`，`<ver>` 取自 `CHANGELOG.md` 顶部的
`## [vX.Y.Z]`（与桌面安装包同一权威）。

## 前置

- **Docker Desktop 必须运行**（macOS 不能 loop-mount ext4；Apple Silicon 的 Linux VM 就是 aarch64，
  所以 pi-gen 是**原生 arm64 构建，不需要 qemu**）。
- **≥20 GB 空闲**在 `MRRC_PI_WORK`（默认 `build/pi-gen`）所在卷上——pi-gen 的 `work/` 与
  `deploy/` 都落在那里，镜像是多 GB 级。
- `python3` 在宿主上可用：四个镜像源改写点用它做幂等替换。
- 首次需要 `git clone` pi-gen（GitHub 在国内可能很慢；`MRRC_PI_WORK` 指向一个已克隆好的目录可以跳过）。

## pi-gen 锁定与缓存

| 位置 | 内容 | 说明 |
| --- | --- | --- |
| `build/pi-gen` | pi-gen 克隆，ref `2026-06-18-raspios-bookworm-arm64` | 已存在且 ref 正确时**不重新克隆** |
| `build/pi-gen/work` | stage0–2 的 rootfs 与 stage work | 失败续跑靠它 |
| `build/pi-gen/deploy` | 未压缩的 `.img` | 构建末段由脚本压成 `.xz` 并删掉 `.img` |
| `pi-gen:latest` | pi-gen 的容器镜像（`Dockerfile` 构建） | 一次构建后被 Docker 缓存；改了 Dockerfile 会重建那一层 |

## 镜像源开关（国内网络）

pi-gen **没有**任何镜像开关，而 stage0 从 `deb.debian.org` debootstrap——实测容器内
**65 KB/s**（W103D 那边的同一条实测），国内镜像同文件 700–830 KB/s。所以构建脚本在
**克隆里**幂等改写四个上游点：

| 改写点 | 上游 | 默认换成 |
| --- | --- | --- |
| `stage0/prerun.sh` 的 `bootstrap …` | `http://deb.debian.org/debian/` | 清华 debian |
| `stage0/00-configure-apt/files/sources.list`（3 行） | `deb.debian.org` | 同上 |
| `stage0/00-configure-apt/files/raspi.list` | `archive.raspberrypi.com/debian` | 清华 `/raspberrypi` |
| `Dockerfile` 的 `RUN apt-get -y update` | `deb.debian.org` | 同上（容器层先跑，也最先卡） |

三个开关（语义与 W103D 三件套一致，**置空即回上游**）：

```bash
MRRC_PI_APT_MIRROR=https://mirrors.tuna.tsinghua.edu.cn        # 置空用 deb.debian.org
MRRC_PI_PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple     # 置空用 PyPI
MRRC_PI_FRP_PROXY=https://gh-proxy.com/,https://ghfast.top/,   # 逗号列表，末尾空项 = GitHub
```

`MRRC_PI_FRP_PROXY` 的末尾空项是"最后从 GitHub 拉"——社区代理会挂，**别删它**；
`fetch-frpc.sh` 仍逐字节比对 frp 官方校验和，所以代理只加速不换货。

**判据：`apt`/`pip` 连着但零下载 = 源慢，不是代码坏。**

## 构建时数据流

```
宿主 macOS
  ├─ 检查 Docker / 磁盘
  ├─ 改写 pi-gen 克隆的 4 处上游源（幂等）
  ├─ rsync packaging/rpi/pi-gen-stage4/ → build/pi-gen/rpi-stage4/
  ├─ bash packaging/common/stage-tree.sh <仓库> rpi-stage4/01-deploy-mrrc/files/opt/mrrc_modern
  ├─ rsync packaging/common/ → files/opt/mrrc-build/   （服务层自己的素材）
  ├─ 写 files/opt/mrrc-build/build.env（版本/绑定/启动盘/两个镜像源）
  ├─ version.txt + VERSION（来自 CHANGELOG 顶部）
  └─ build-docker.sh（PRESERVE_CONTAINER=1）
        └─ pi-gen stage0→2（原生 arm64） + stage rpi-stage4：
              prerun.sh        : files/ → rootfs（缺项即 FATAL）
              00-run-chroot.sh : 组 → 属主 → . build.env → bash /opt/mrrc-build/mrrc-service-layer.sh
  ├─ xz -9 → dist/rpi/MRRC-Modern-v<ver>-rpi64.img.xz
  └─ verify-image.sh（四步）
```

**素材不放在 `/tmp`**：pi-gen 的 `on_chroot`（`scripts/common`）把 `${ROOTFS_DIR}/tmp` 挂成
tmpfs，`prerun.sh` 在挂载**之前**拷进去的东西会被挂载点**盖住**，chroot 里看不到。
`/opt/mrrc-build` 没有这个问题，服务层末段 `rm -rf` 掉它。

**rootfs 里的 `/boot/firmware`** 是 Pi 的启动分区（Armbian 那边是 `/boot`）——这个路径由
`MRRC_BOOT_DIR` 参数化，写进 `mrrc-firstboot.service`，只有 `firstboot_wrapper.py` 读它。

## 失败续跑

```bash
cd build/pi-gen
CONTINUE=1 IGNORE_FILE_CHANGES=1 ./build-docker.sh
```

`PRESERVE_CONTAINER=1` 让失败的 `pigen_work` 容器留下来，stage 的临时状态因此不丢。
**构建跑着的时候不要改脚本**：bash 增量读取脚本文件，运行中编辑会读坏。

## 产物验证（四步，必须做）

```bash
packaging/rpi/verify-image.sh dist/rpi/MRRC-Modern-v<ver>-rpi64.img.xz
```

在一个特权 arm64 容器里解开镜像、mount 第二个分区，然后：

1. **代码身份**：产物里的 `server.py` / `requirements.txt` / `static/ft710_ui.js` 等与工作区
   `sha256sum` **逐字节相同**（证明镜像装的是这次构建的代码，不是别的 checkout）；
2. **装机内容**：11 份 profiles、`/usr/local/bin` 三件（`mrrc-radio` / `mrrc-update` /
   `mrrc-show-password`）、登录横幅、`fleet/frpc` 是 aarch64 ELF、FTDI 库 + `libftd2xx.so`
   符号链接 + `99-mrrc-ftdi.rules`、`mrrc` 账号、`version.txt`、**`/opt/mrrc-build` 与
   `__pycache__` 零残留**；
3. **D-11 陷阱**：`mrrc-modern.service` 里 `EnvironmentFile` 与 `Environment=MRRC_CONFIG_FILE`
   **指向同一文件**、`User=mrrc`；env 文件里 `MRRC_WEB_HOST=::`（Pi 唯一的那处有意差异）；
4. **真跑**：bind `/dev`、`/proc`、`/sys` 后 `chroot` 进产物，用**产物自己的 venv**
   `import fastapi, numpy, pyaudio, lameenc, cryptography, serial`，再 `ctypes.CDLL("libopus.so.0")`，
   最后导入五个入口点（`server` / `net_wifi` / `box_ops` / `first_run` / `mrrc_radio`）。

第 ④ 步是真跑而不是"看文件在不在"：宿主机也是 arm64，所以不需要 qemu。

## 构建产物与发布

`MRRC-Modern-v<ver>-rpi64.img.xz` 属于**发布制品**，走 `dual-platform-release` 技能的流程
（版本号、`release-artifacts.json`、网站下载卡、线上 SHA 复核）。本手册不重复那套流程。

**线上现状（截至 2026-10-11）**：官网树莓派卡片指向的是 **v1.14.1**（2026-09-30 构建），
比仓库落后 11 个版本，且卡片仍写着"真频谱需手动放库"——`2026-10-11-rpi-w103d-parity-packaging-design.md`
之后这一条不再成立。最终镜像与卡片更新排在下期（等 W103D 局域网设置页落地）。

## 实跑记录

### v1.26.0 发布构建（2026-10-12）

设置页那条线落地后、与新版本号一起建的**发布态**镜像。这一次 Pi 与盒子真的拿到同一套东西：
`=== polkit: let the server user reconfigure NetworkManager ===`、`=== box identity … ===`
这两步在 Pi 的 chroot 里照常执行，`version: 1.26.0`、`entry points import OK`。

```
dist/rpi/MRRC-Modern-v1.26.0-rpi64.img.xz
481,145,192 bytes   SHA-256 d431c9aa1822aa4318f08eb037c91e97be99d0c0f2a7893763bfe24e45c7908b
```

四步产物验证 `VERIFY_OK`。第 ③ 步里本版新增两条**Pi 专属**的启动分区检查，验的正是共用服务层第一天
漏掉的那处：`mrrc-wifi-preseed.service` 的 `ConditionPathExists` 必须指向 `/boot/firmware`，
且脚本拿到的 `MRRC_BOOT_DIR` 与它一致——写死 `/boot` 的话这条能力在 Pi 上会**静默哑掉**（systemd
的 ConditionPathExists 不成立就干脆不跑，没有任何一行日志会说）。修法与守卫见
`docs/w103d_pack.md` 的 v1.26.0 一节。


每一次真构建的实测数据追加在本节——「只有真跑才会暴露」的东西记在这里，而不是留在聊天记录里。

### 2026-10-12：结构统一后的两轮全量构建

**产物**：`dist/rpi/MRRC-Modern-v1.25.5-rpi64.img.xz`

| 轮次 | 耗时 | 产物字节 | SHA-256 | 四步验证 |
| --- | --- | --- | --- | --- |
| ①（修前） | 23:44 → 00:49，**65 分钟** | 476,978,928 | `30d159cc…` | ❌ `env bind`（真缺陷，见下） |
| ②（修后） | 01:02 → 01:17，**15.5 分钟** | 482,166,288 | `665b7cbe…` | ✅ 四步全过（仅"工作树在动"一项） |

**两轮差 4 倍，差异不在代码**：①轮要从头重建 pi-gen 的容器镜像（Dockerfile 被镜像源改写触发了那一层），而且当时机器上并行跑着别的活；②轮容器镜像已缓存、机器空闲。`xz -9` 在两轮里分别是 ~35 分钟与 ~8 分钟。**别拿两轮的耗时互相比**。

**②轮的阶段耗时**（pi-gen 自己的时间戳）：stage0 4分11秒 / stage1 17 秒 / stage2 49 秒 / **rpi-stage4 4分20秒** / export-image 2分42秒 → pi-gen 共 **12分20秒**；剩下是宿主侧 `xz`。

**服务层在 Pi 的 chroot 里逐步跑通**（这是本设计的主要未知项，已消除）：

```
prerun: staged 117 code files
=== preconditions === / system packages / radio profiles /
python environment  → [18:03:00] Creating virtual environment: /opt/mrrc_modern/venv
=== FTDI scope libraries === / udev rule / frpc / mrrc-radio…login banner /
systemd units / version stamp (version: 1.25.5) / headless defaults / enable services /
import gate: entry points import OK / reclaim space / overlay done
```

- **`install.sh` 在 pi-gen 的 `on_chroot` 环境里跑通了**（设计 §10 里唯一的中等风险项）：它建了 venv、装完依赖，没报错。
- **frpc** 走 gh-proxy，一次过并逐字节校验通过（对比：W103D 那轮同一份文件只有 27 KB/s、超时后续传）。
- 源码注入：`stage-tree.sh` 在宿主侧落 **115 个文件**，`prerun.sh` 校验后拷入 **117 个**（多出 `version.txt`/`VERSION` 两个构建产物）。
- `work/` 在构建结束后是**空的**（pi-gen 自己清）；容器镜像 `pi-gen:latest` 保留，是下一轮快的原因。

**这一轮拓出的真缺陷（四步验证第 ③ 步拓的）**：产物里单元文件写着 `Environment=MRRC_WEB_HOST=::`，
但 `env/mrrc.env` 里是 `MRRC_WEB_HOST=0.0.0.0` —— 服务端从 env 文件读绑定地址，于是**双栈绑定被静默丢掉**。
根因：`install.sh` 的 `.env` 模板里写死了 `MRRC_WEB_HOST=0.0.0.0`（`install.sh:785`），
而服务层的默认值块是"键不存在才写"。旧 Pi 线不跑 `install.sh`（首启包装写的 `::`），所以这是结构统一引入的回归。
修法与既有那段"剥掉构建机猜测"（串口、口令）合并，并加了守卫。

**验证脚本自己的两个缺陷**（都已修，供后人参考）：

1. 配方抄自盒子（`.gz`）却直接用在 `.xz` 上 → `gunzip: not in gzip format`；
2. 容器内用通配符 `MRRC-Modern-*-rpi64.img.xz` 选镜像，而 `dist/rpi/` 里还躺着上一版 `v1.14.1` → 两个都去验证了。

现在改为**宿主侧解压 + 精确路径**；另：容器里**不要用 `file`**（精简的 `debian:bookworm` 没有它），判 AArch64 直接读 ELF 头的 `e_machine`（偏移 18，`183`）。

**一处需要记住的构建期现象**：构建期间仓库工作树继续在动，所以产物里的 `server.py` 是构建开始那一刻的版本
（②轮那份是 `5b1089c`，①轮那份更早），与验证时的工作树不同。这不是缺陷，但**验证要按修订核对，
不能默认为工作区**。要一份不带这个尾巴的产物，就在工作树静止时构建。
