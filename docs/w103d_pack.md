# W103D 镜像构建手册

对标 `mac_pack.md` / `win_pack.md`。要**操作**这台盒子看 `packaging/box/README.md`；
这份文档只讲**怎么出镜像**。

## 一键

```bash
packaging/box/build-image.sh --check    # 只查前置，不动任何东西
packaging/box/build-image.sh            # 全流程
```

产物：`dist/w103d/MRRC-Modern-<ver>-w103d.img.gz`（末尾打印 SHA-256）。

## 前置

| 项 | 要求 | 为什么 |
| --- | --- | --- |
| Docker Desktop | **必须在运行** | macOS 不能原生 loop 挂载 ext4；构建在 `--privileged` 的 arm64 容器里做 |
| 架构 | Apple Silicon（arm64） | 目标镜像也是 arm64 → **chroot 原生，不需要 qemu-user-static**。与 `packaging/rpi/build-image.sh` 同源的理由 |
| 空闲磁盘 | ≥12 GB | 镜像 3.4 GB（解压）+ 压缩产物（约 1 GB）+ 中间文件 |
| 工具 | `curl` `rsync` `gzip` | `--check` 会逐个验 |

`--check` 就是为"Docker 忘了开"这个最常见的失败准备的——它会明确说 `Docker daemon is not running`
而不是在半小时后崩在 `losetup`。

## 基底镜像（钉死）

```
Armbian_26.11.0_amlogic_s905l3a-w103d_bookworm_6.18.54_server_2026.10.01.img.gz
  SHA-256  998d5244ac2274077c091050b9db620c22a8412989766fbd728b045a0fed1ae9
  来源     github.com/ophub/amlogic-s9xxx-armbian  release Armbian_bookworm_arm64_server_2026.10
  解压后   3.4 GB
```

**文件名里四个标记都不能换**：

| 标记 | 换错的后果 |
| --- | --- |
| `amlogic` | 平台 |
| **`s905l3a-w103d`** | 下成通用 `s905l3a` → 没有这块板的 DTS 与内核补丁 → **MT7663S WiFi/蓝牙静默不工作** |
| `bookworm` | 换 noble/trixie → Python 不是 3.11 |
| `6.18.54` | W103D 的 SDIO 路由补丁 + 原厂 N9 固件在这个系列里 |

脚本会在解压前校验 SHA；不符即中止，绝不带着半个镜像往下走。

## 为什么不自建 Armbian 镜像

W103D 的板级补丁（SDIO 路由到 SD_EMMC_B、MT7663S 传输层、固件 overlay）活在
ophub/unifreq 的内核树里（PR #3658 / #3659）。自建意味着长期跟进那套补丁栈，
收益不抵维护成本。

**代价是已知的**：依赖一个第三方预构建镜像。缓解手段是钉 SHA-256 + 只从官方 release 取。
这也是为什么 `vendor-ftdi.md` 里把每个外部二进制的来源都写清楚了——供应链要能追溯。

## 镜像内部（实测，不是估算）

| 项 | 值 |
| --- | --- |
| 分区表 | MBR（不是 GPT——第一版设计假设过 GPT，实测纠正） |
| p1 | FAT32，511 MiB，卷标 `BOOT`（挂到 `/boot`） |
| p2 | ext4，3000 MiB，卷标 `ROOTFS`（挂到 `/`） |
| rootfs 可用 | **963.7 MiB**（首启后 eMMC 会扩到 32 GB，所以这只影响构建期） |
| 镜像已带 | Python 3.11、libopus0、portaudio19-dev、libportaudio2、python3-venv/pip/dev、libasound2-dev、gcc、git、rsync、sudo、NetworkManager/nmcli、**mt7663 驱动** |

所以我们加的东西只有：代码（175.4 MiB，用 Pi 构建器的排除清单实测）、
venv（≈90–120 MiB）、frpc（≈10 MiB）、FTDI 库（≈1 MiB）。**apt 增量 ≈ 0。**

## 空间断言

容器里在铺 overlay **之前**量一次 p2 可用空间，低于 400 MiB 直接失败：

```
rootfs free before overlay: 963 MiB
```

这样将来换个基底镜像时，症状是**构建失败**，而不是半个 rootfs 写进去之后在真机上才发现。

**不够时的两条路**（改脚本之前先想清楚选哪条）：

1. **精简内容**——`box-overlay.sh` 里去掉不必要的部分（比如 `install.sh --dev`），清 pip/apt 缓存。
   首选，因为首启后空间本来就有 29 GB。
2. **扩容 p2**——loop 设备上 `growpart` + `resize2fs`，把 3000 MiB 加到 6000 MiB。
   代价：镜像下载体积翻倍。

> **运行时不会自己扩（2026-10-12 实测修正）**：镜像里带着 `/root/.no_rootfs_resize`，首启**不**做自动
> 扩容——U 盘上根分区就停在 2.9 G，用户得自己 `apt-get install -y cloud-guest-utils && growpart /dev/sda 2
> && resize2fs /dev/sda2`（步骤见指南 §4.1，ext4 在线可扩、UUID 不变）。**运维后果**：`/ddbr` 里那份
> 2.3 G 的 eMMC 全盘备份在 2.9 G 的根分区上**放不下**。想改这个行为就把那个 marker 从镜像里去掉，
> 并实测一次首启扩盘。

## 外部二进制

| 东西 | 来源 | 校验 |
| --- | --- | --- |
| frpc | frp 官方 release，**版本钉死 0.71.0**（与 hub 的 frps 对齐） | 对照官方 `frp_sha256_checksums.txt`，不符即拒、且不装任何文件 |
| `libft4222.so`（aarch64） | FTDI `libft4222-linux-1.4.4.232`，入库到 `vendor/ftdi/` | 构建期不校验（已在仓库里）；测试断言它是 `e_machine=183` 的 ELF |
| `libftd2xx.so` | **指向同一个 ELF 的符号链接** | 测试断言它确实是符号链接且指向 `libft4222.so` |
| `mt7663_n9_v3.bin`（N9 WiFi 固件） | **W103D 出厂 Android 镜像**（`WIFI_RAM_CODE_MT7663.bin`，字节未改），入库到 `vendor/mediatek/` | 构建期 `sha256sum -c` 钉死 `223f73f1…`；镜像里加 `dpkg-divert` 防 linux-firmware 升级覆盖。**不能用基底自带的 linux-firmware 2020 版**——它能初始化但会在真机上渐进式挂死（2026-10-11 现场：~30 秒一次掉线、扫描楔死、~16 分钟 MCU mailbox 全停）。溯源：`packaging/box/vendor-mt7663.md` |

**frpc 为什么钉版本**：客户端比服务端新会握手失败，而症状是"隧道永远起不来"，
不是一句版本抱怨。

**FTDI 为什么只有一个文件**：FTDI 的 Linux 版 libft4222 静态链入了 libftd2xx 并重新导出其符号，
所以一个 ELF 能填满 `find_ftdi_libraries()` 要求的那一对。证据与复现命令见
`packaging/box/vendor-ftdi.md`。

## 与树莓派构建器的关系

两条镜像线共用 `packaging/common/`：`stage-tree.sh` 是**排除清单唯一的出处**（Pi 构建器在宿主侧调它、
本 overlay 在 chroot 里调它、`linux/mrrc_update.sh` 在设备上调用拉下来那份），`mrrc-service-layer.sh`
是**服务层唯一的实现**（apt 清单、venv、FTDI + udev、frpc、单元、env 默认值、导入门禁、回收）。
平台壳只负责各自板子的事。

两份构建器对"什么该进 `/opt/mrrc_modern`"产生分歧的后果是**运行期 ImportError**，不是构建错误——
所以这条约束由 `tests/test_packaging_parity.py` 来守：清单多出第二个载体就红。
**要改清单，改 `packaging/common/stage-tree.sh`**（早先那句"改三个地方"已作废）。
平台差异只允许两处：`MRRC_WEB_HOST` 与 `MRRC_BOOT_DIR`，两者都被同一份测试逐字钉住。

## 构建产物与发布

`MRRC-Modern-<ver>-w103d.img.gz` 属于**发布制品**。真要发布时按 `dual-platform-release` 技能处理：
版本号、`release-artifacts.json` 的规则、网站下载卡、以及带 SHA 的线上复核。
本手册不重复那套流程。

**当前对外发布（2026-10-18）**：`MRRC-Modern-1.28.1-w103d.img.gz`，**预置（`--preflash`）即发布**——
三条口令固定、时区与区域已设，用户开箱不需要任何初始化（见 `W103D_GUIDE.md` §7）。

```
https://www.vlsc.net/mrrc_modern/downloads/MRRC-Modern-1.28.1-w103d.img.gz
1,221,443,860 bytes   SHA-256 ee1fba1e5e5057eed7603e636547299ae5cf82bd321d0202e72ad9a8e0cde473
```

**历史记录（2026-10-11，第二次构建，已下架）**：

```
https://www.vlsc.net/mrrc_modern/downloads/MRRC-Modern-1.25.5-w103d.img.gz
1,154,545,271 bytes   SHA-256 5d74f7378b64bd7ebff16528aa4b5222097de22b9f3b214fe1ccebc62dc8ffc1
```

这一版**重建**以纳入真机首启拓出的全部修复（SDD V2.79）：RX 采集缓冲/静默看门狗、
首启证书归属、FT4222 udev 规则、构建机残留值剥离、`dnsmasq.service` mask、
`journal+console`；另有三个**只有镜像布局才暴露**的缺陷（commit `52820e2`）：`server.py`
在模块层 import 被排除的 `macos/`、`mrrc-radio` 从 `__file__` 解析树、同一命令没有 shebang。
同名的前一版（`1,280,233,862 bytes`，SHA `81cf1f9e…`）已被本版**替换**，不要按旧 SHA 校验。

> ⚠️ **它之前的那一版（2026-10-07，`MRRC-Modern-1.25.4-w103d.img.gz`）烧上去服务起不来** ——
> 同第一个缺陷。线上那份文件仍在 `downloads/` 里留档（版本化文件不撤），但**不要再烧 1.25.4**。
> 判定方法：挂载镜像后 `chroot` 里跑
> `/opt/mrrc_modern/venv/bin/python -c "import server"` —— 1.25.4 报
> `ModuleNotFoundError: No module named 'macos'`。

发布路径与桌面安装包一致（`dual-platform-release` 技能第 5/6 步）：`scp` 到 `www.vlsc.net:/var/tmp/`
（**不是 `/tmp`——那是 958 MB 的 tmpfs**）→ **服务端独立算 SHA 核对后才 `sudo -n mv`** 进
`/var/www/vlsc.net/mrrc_modern/downloads/` → `website/deploy.sh` 部署 HTML（它**排除 `downloads/`**，
所以镜像不会被 `rsync --delete` 清掉；HTML 部署**必须只跑一次**）。

卡片写在 `website/index.html` 与 `website/zh/index.html` 的树莓派卡片之后，
`release-artifacts.json` 的 `artifact_facts` 多了 `website-w103d-card` 一条——
它按**字节数与 SHA 前 8 位子串**核对卡片是否描述了真正的产物，所以**重新构建后必须同时更新卡片上的两个值**，
否则 `release_check` 会红（这正是设计意图：卡片指向的必须是真正上传的那份字节）。

## v1.26.0 发布构建（2026-10-12）

设置页那条线落地之后的**发布态**构建：镜像里第一次同时带上局域网配置页那套（polkit 规则、
`mrrc-box-helper.service`、Wi-Fi 预置单元、`box_helper.py`、`settings.html`/`box_api.js`）
与远程协助那条线（`help_session.py` + `requirements.txt` 里的 paramiko，镜像 venv 内可导入 5.0.0）。

```
dist/w103d/MRRC-Modern-1.26.0-w103d.img.gz
1,008,925,741 bytes   SHA-256 9913752e6f366f6944869b9bfc70813743aafdd819d9b9416d11ec585f4abfef
```

构建从**干净基底**重解（先删掉 `build/w103d/w103d.img`，不让上一轮的 overlay 叠上来），
约 **6 分钟**；四步产物验证 `VERIFY_OK`（代码身份逐字节、11 份 profiles、`/usr/local/bin` 三件、
frpc 是 aarch64 ELF、FTDI + udev 规则、`mrrc` 账号、`version.txt = 1.26.0`、`/opt/mrrc-build`
与 `__pycache__` 零残留、D-11 两行同一文件、arm64 原生 chroot 导入通过）。

**这一轮拓出并修掉的一处**（由"Pi 必须拿到盒子那套"这条要求逼出来的）：

服务层生成的 `mrrc-wifi-preseed.service` 与 `linux/wifi_preseed.py` 都把启动分区**写死成
`/boot/mrrc-wifi.txt`**。Armbian 在 `/boot`、Raspberry Pi OS 在 `/boot/firmware`——写死的后果
不是报错，而是 systemd 的 `ConditionPathExists` 永不成立，**这条能力在 Pi 上静默哑掉**。
这正是 `MRRC_BOOT_DIR` 参数存在的理由，而共用服务层的第一天就把它漏在了这一处。
现在单元用 `$MRRC_BOOT_DIR`（并 `Environment=` 传给脚本）、脚本从 `MRRC_BOOT_DIR` 取路径；
两条守卫进 `tests/test_packaging_parity.py`（预置那对文件必须用参数；碰启动分区的三个文件里
不许出现字面量 `"/boot/…"`）。

## 结构统一后的真构建（2026-10-11）

构建器改为"平台壳 + `packaging/common/` 服务层"之后的第一轮真跑。

**结果**：从干净基底跑到底，四步产物验证 `VERIFY_OK`。

```
dist/w103d/MRRC-Modern-1.25.5-w103d.img.gz
1,038,463,404 bytes   SHA-256 577362651eff4e20279ba5dd7ecbffd69f3f3bca84d1da05383e7cc34478f600
```

| 项 | 实测 |
| --- | --- |
| 总耗时 | **约 16 分钟**（23:20 → 23:36 CST），其中 gzip -9 占大半 |
| 基底 | 缓存的 852 MB `.gz`，重新解压成 `w103d.img`（3.7 GB） |
| apt / pip | 清华源，秒级完成（对比：上游 `deb.debian.org` 实测 65 KB/s） |
| frpc | **最慢的一段**：gh-proxy 只有 ~27 KB/s，curl 在 300 s 上限处超时（`(28) ... 8090045 out of 12375716 bytes`），续传循环接上并跑完 |
| 服务层 | 旧 overlay 的 17 个步骤全部执行；`/opt/mrrc_modern` 回收后 **150 MB** |

**产物验证（四步）**：代码身份（`server.py`/`requirements.txt`/`static/ft710_ui.js`/`scope_handler.py` 与工作区逐字节相同）、
装机内容（11 份 profiles、`/usr/local/bin` 三件、登录横幅、frpc 是 aarch64 ELF、FTDI 库 + 符号链接 + udev 规则、
`mrrc` 账号、`version.txt`、`/opt/mrrc-build` 与 `__pycache__` 零残留、无被烘进镜像的口令/串口）、
D-11（两行指向同一文件，`MRRC_BOOT_DIR=/boot`、`MRRC_WEB_HOST=0.0.0.0` 两个平台参数取值正确）、
arm64 原生 chroot 真跑（依赖导入 + `libopus.so.0` + 五个入口点）全部 `OK`。

**只有真跑才会暴露的东西（本轮两个，都在服务层重写时引入）**：

1. `install SRC DST` 源与目标同路径 → `install: ... are the same file`，`set -e` 直接终止构建。
   重写时"顺手"把源从 `$REPO_SRC/vendor/ftdi/…` 改成了 `$MRRC_HOME/vendor/ftdi/…`（因为树已经落好了），
   但目标也是它。修法：树已经由 `stage-tree.sh` 拷好（模式一起带过来），这里只需**断言存在 + chmod**。
2. 重写时**漏掉了 `radio profiles` 那一步** —— 11 份型号画像不会进镜像、型号切换器无数据。
   是靠"把旧 overlay 的 `log` 步骤逐条与新服务层做机械比对"找出来的，不是靠读代码。

**一处需要注意的构建期现象**：构建期间仓库工作树又落了两个提交，所以产物里的 `box_ops.py` 是构建快照那一刻的版本
（`c8b48ba1…` = `cdb9498`），与当时的工作树不同。这类差异不是缺陷，但验证时**要按修订核对而不是默认为工作区**。

## 首次真构建：九个 bug 与实测结果（2026-10-07）

在 macOS 26.3 (arm64) + Docker Desktop 24.0.6 上首次跑通 `build-image.sh`。**镜像从未真跑过**这件事本身
就是这一节存在的理由：下面九个缺陷全部通过了代码走查、单测和 `release-check`，只有真跑才暴露。

| # | 症状 | 根因 | 修法（守卫） |
| --- | --- | --- | --- |
| 1 | `mount: special device /dev/loop1p2 does not exist` | `losetup -P` 让内核扫描分区表，但**建 /dev 节点是 udev 的活**，容器里没有 udev（Docker 用 tmpfs 盖掉 /dev） | 新增 `part-nodes.sh`：从 sysfs 读 `major:minor` 自己 `mknod`；挂载前断言节点存在 |
| 2 | `No such file or directory` 指向明明存在的 `/src/…` | `chroot` 换根后容器里的 `/src` 不可达（解析成 `/mnt/src`） | `mount --bind /src /mnt/src` 并 remount ro |
| 3 | chroot 里 apt 全部超时 | rootfs 的 `/etc/resolv.conf` 是指向 `/run/systemd/resolve/stub-resolv.conf` 的**悬空符号链接**（chroot 里没跑 systemd-resolved） | 把容器的 resolv.conf 复制进去 |
| 4 | `bad interpreter: Permission denied` | `box-overlay.sh` 提交时是 **0644**（全仓唯一一个非 755 的 .sh） | `chmod +x`；加守卫测试：`packaging/box/*.sh` 必须可执行 |
| 5 | `apt-get update` 卡 4–6 分钟 | 本网络到 `deb.debian.org` 容器内实测 **65 KB/s**（清华 829 KB/s） | `MRRC_BOX_APT_MIRROR`（默认清华），只改写 Debian 源、不动 Armbian 源 |
| 6 | `pip install` 连接established却**零下载 7 分钟** | 到 PyPI 实测 **36.9 KB/s 且 25 秒超时**（清华 2.9 MB/s、阿里 5.0 MB/s） | `MRRC_BOX_PIP_INDEX`（默认清华）；经 chroot 继承的环境变量传 `PIP_INDEX_URL` |
| 7 | `curl: (18)` 下 frp 失败；另一次**卡住不动** | github.com 实测 **33 KB/s**；且**卡死不报错**时 `--retry` 永远不触发 | `MRRC_BOX_FRP_PROXY`（默认 gh-proxy.com，实测 2.3 MB/s）；`fetch-frpc.sh` 改为**显式续传重试循环** + `--speed-limit/--speed-time` 把"慢到卡死"变成可重试的错误 |
| 8 | `install.sh` STEP 4b：`INSTALL_DEV: unbound variable`<br>STEP 9：`USER: unbound variable` | 脚本头部有 `set -euo pipefail`，但 `INSTALL_DEV` 只在 `--dev` 分支被赋值；`USER` 是**登录 shell 变量**，chroot 里根本没有 | `INSTALL_DEV=false` 进全局区；`USER` 用 `: "${USER:=$(id -un)}"` 兜底。**两个都影响所有平台的手动安装**，不只是盒子 |
| 9 | 产物里没有 `version.txt` | `version.txt` **不在仓库里**，它是构建产物（由 CHANGELOG 顶版本生成）；照抄 Pi 脚本时漏了这层。而诊断包 manifest 与升级通道都要读它 | `build-image.sh` 从 CHANGELOG 取版本（含产物命名）；`box-overlay.sh` 写 `version.txt` + `VERSION` |

**三条镜像开关**（都可置空回到上游；`fetch-frpc.sh` 仍校验 frp 官方 checksum，所以代理只能加速、不能换货）：

```bash
MRRC_BOX_APT_MIRROR=https://mirrors.tuna.tsinghua.edu.cn   # 默认；置空用 deb.debian.org
MRRC_BOX_PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple # 默认；置空用 PyPI
MRRC_BOX_FRP_PROXY=https://gh-proxy.com/                    # 默认；置空用 github.com
```

**实测结果**（首次成功构建）：

- 基底镜像下载 **852,336,261 字节**，SHA-256 与钉死值一致
- 产物 `dist/w103d/MRRC-Modern-<ver>-w103d.img.gz`（`<ver>` 取自 CHANGELOG 顶版本）
- rootfs 可用 **948 MiB → 905 MiB**（净增仅 43 MiB；`/opt/mrrc_modern` 145 MiB，回收步骤收回约 100 MB 缓存）
- 全流程耗时取决于网络（三个源都慢时最久的是 frpc；配好镜像后 apt+pip 合计约 2 分钟）
- **产物 .gz 体积会因构建历史浮动几个百分点**（实测同内容两次构建：1.067 GB vs 1.132 GB）。差异全部来自
  ext4 空闲块里残留数据的多少（被删掉的 apt lists / pip 缓存 / `/tmp` 留在已释放块里），**不是内容差异**——
  两次构建挂载后 `df` 与各目录 `du` 逐项相同。要发布就发布**那一次**的产物并记下它的 SHA，别拿两次的
  体积互相比对

**产物验证**（`dist/` 里那份真产物，不是工作区）：

1. **代码身份**：`server.py` / `linux/mrrc_radio.py` / `firstboot_wrapper.py` / `profiles/ft710.env` 与工作区**逐字节相同**
2. **装机内容**：11 份 profile、`mrrc-radio` + `mrrc-update` + `mrrc-show-password`、frpc **ARM64 ELF 14.8 MB**、FTDI 库与 `libftd2xx.so` 符号链接、`mrrc` 用户、`/tmp` 零残留
3. **D-11 陷阱**：`EnvironmentFile` 与 `Environment=MRRC_CONFIG_FILE` 指向**同一文件**、`User=mrrc`、两个服务已 enable、`serial-getty@ttyFIQ0` 已 mask
4. **真功能**（arm64 原生 chroot，无需 qemu）：**Python 3.11.2 导入全部关键依赖成功**（fastapi / uvicorn / numpy 2.4.6 / pyaudio / lameenc / cryptography / websockets / pyserial）、`libopus.so.0` 可加载

**仍未验证**（等硬件）：U 盘启动、首启口令与自签证书、FT-710 真 FFT、电台切换、Cloud Hub 公网接入、盒子上的 `bench_mrrc.py`。

## 真机首启：四处镜像缺陷 + 一处音频缺陷（2026-10-11）

1.25.5 镜像第一次真正跑在 W103D 上（U 盘启动、FT-710 三根 USB 接口全插），
逐项记录在 SDD V2.79。这里只留**操作层能看到的判定方法**与新烙进镜像的设置：

| 现象 | 判定（一行） | 根因 | 镜像里的修法 |
| --- | --- | --- | --- |
| 网页打不开，`mrrc-modern` 每 3 秒重启一次 | `sudo -u mrrc head -c 20 /var/lib/mrrc/certs/server.key` | 首启以 root 生成证书，`server.key` 是 `0600 root:root`，服务跑在 `mrrc` 下 | `firstboot_wrapper.ensure_cert()` 生成后 `chown -R mrrc:mrrc` + `chmod 600/644` |
| 真实频谱永远起不来（CAT/音频正常） | `sudo -u mrrc test -w /dev/bus/usb/001/00N && echo ok` | FT4222 没有自己的 /dev 节点，libft4222 直接开 USB 设备，节点 `root:root 0664` | overlay 写 `/etc/udev/rules.d/99-mrrc-ftdi.rules`（`0403:601c`、`0660`、`dialout`） |
| 声音卡顿、听一会儿彻底安静 | `cat /proc/asound/card1/stream0` 看 Capture 是否 `Status: Stop`；日志里 `capture stalled` | **宿主缓冲只有 1–2 个 20 ms 周期**，溢出后 ALSA 采集接口静默停摆（PyAudio 不报错） | `MRRC_RX_BUFFER_MS` 默认 **100**（Linux）+ `server._audio_rx_loop` 的静默看门狗（3 秒无 PCM 则重开采集流） |
| HDMI 上看不到首启横幅（带口令） | `journalctl -u mrrc-firstboot \| grep "output specifier"` | `StandardOutput=console` **不是合法 systemd 语法** | 改成 `journal+console`（overlay + rpi stage） |
| env 里带着构建机的串口/口令 | `grep MRRC_SERIAL_PORT /opt/mrrc_modern/env/mrrc.env` | overlay 把 `install.sh` 在构建主机上写的 `.env` 原样搬进镜像 | 搬移后 `sed` 掉 `MRRC_SERIAL_PORT` 与 `MRRC_WEB_PASSWORD`，交回首启 |
| 新盒子上 `dnsmasq.service` 是红灯 | `systemctl --failed` | Debian 的单元与 systemd-resolved 抢 53 端口（热点用 NM 自带实例，不需要它） | `systemctl mask dnsmasq.service` |

**音频那条的实测数据**（服务自己的 `AudioHandler`，服务外三组对照）：

| 读法 | 覆盖 |
| --- | --- |
| 服务原样（20 ms tick + 882 门槛） | **4%**，约 60 秒后**永久停摆** |
| 同代码、读得激进（有任何数据就读） | 100% |
| 同代码、缓冲 400 ms（每秒只读 2.5 次） | 99.7% |
| **修后（缓冲 100 ms）** | **100%，50.0 fps / 51.7 kbps，稳定 3 分钟以上** |

采样率域没动：设备 44.1 kHz、Opus 48 kHz、882↔960 的 20 ms 桥一行未改。

## 真机 eMMC 固化（2026-10-11）

U 盘系统验收通过后，用 ophub 自带安装器**就地**固化进 eMMC —— 不需要线刷、不需要重打镜像。
完整操作步骤在 `docs/W103D_GUIDE.md` §17（同款流程已沉淀为 `w103d-emmc` 技能）。这里只留构建/取证角度值得记的事实：

- **备份**：`armbian-ddbr` 全盘 29 GB → 2.2 GB（`pigz -t` 通过）。单线程 gzip 实测 10 MiB/s（≈40 分钟）；
  先装 `pigz` 用 `dd bs=4M | pigz -p 3` 只要 ≈9 分钟，产物是标准 gzip 流（恢复路径不变）。备份留在 U 盘 = 救砖盘。
- **分区**：`ampart` dclone 成功（`data::-1:4`），前 117 MiB 保留；`p1` 512 MB FAT（LABEL `BOOT_EMMC`）
  - `p2` 28.3 GB ext4（新 UUID，`fstab` 与 `uEnv.txt` 同源）。
- **原厂引导**：安装器先把前 4 MiB 存到 `/usr/lib/u-boot/mybox-bootloader.img`，回写用
  `bs=1 count=444` + `bs=512 skip=1 seek=1 count=8191` —— **故意跳过 `[444,512)`**，那 68 B 正是 MBR 分区表。
  所以"首 4 MiB 的 md5 变了"不等于损坏：`cmp` 验证 `[0,444)` 与 `[512,4 MiB)` 与备份逐字节一致。
- **首启**：根在 `/dev/mmcblk2p2`、`/boot` 在 `p1`；全部 systemd 单元（含 `w103d-ddns.timer` 的 enable 链接）
  随 tar 拷贝过去；`/dev/ttyUSB0`、FTDI 库、`*:8888` 双栈、Cloud Hub 入口全部照常。
- **副作用**：machine-id 重生成 → **IPv6 IID 漂移**（DDNS 开机 90 秒内自动跟随，权威 NS 双查确认）；
  IPv4 由 DHCP 按 MAC 发放，不变；口令/`env/mrrc.env`/证书/录音全在原处。
- **`verify.sh` 判读**（那一版镜像）：第 12 项**绿**（2021 版 N9 固件）；第 11 项红是**镜像版本差**
  （构建于 10-11 中午，早于 `box_helper`/polkit 规则/`avahi-daemon` 落地）；第 9 项红是脚本判定偏严
  （入口未登录返回 302 是正常应答，只认 200/401 待放宽）。
