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
|---|---|---|
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
|---|---|
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
|---|---|
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
   代价：镜像下载体积翻倍。首启的自动扩容机制不受影响，但要实测确认。

## 外部二进制

| 东西 | 来源 | 校验 |
|---|---|---|
| frpc | frp 官方 release，**版本钉死 0.71.0**（与 hub 的 frps 对齐） | 对照官方 `frp_sha256_checksums.txt`，不符即拒、且不装任何文件 |
| `libft4222.so`（aarch64） | FTDI `libft4222-linux-1.4.4.232`，入库到 `vendor/ftdi/` | 构建期不校验（已在仓库里）；测试断言它是 `e_machine=183` 的 ELF |
| `libftd2xx.so` | **指向同一个 ELF 的符号链接** | 测试断言它确实是符号链接且指向 `libft4222.so` |

**frpc 为什么钉版本**：客户端比服务端新会握手失败，而症状是"隧道永远起不来"，
不是一句版本抱怨。

**FTDI 为什么只有一个文件**：FTDI 的 Linux 版 libft4222 静态链入了 libftd2xx 并重新导出其符号，
所以一个 ELF 能填满 `find_ftdi_libraries()` 要求的那一对。证据与复现命令见
`packaging/box/vendor-ftdi.md`。

## 与树莓派构建器的关系

三处地方共享同一份 rsync 排除清单（Pi 构建器、box overlay、就地更新脚本），
由 `tests/test_box_profiles.py::ExclusionParityTests` 强制逐字一致。

两份构建器对"什么该进 `/opt/mrrc_modern`"产生分歧的后果是**运行期 ImportError**，不是构建错误——
所以这条约束值得由测试来守，而不是靠记性。

**要改清单时，改 `packaging/rpi/build-image.sh` 的那一份**，然后把另外两处同步过去。
测试会告诉你漏了哪处。

## 构建产物与发布

`MRRC-Modern-<ver>-w103d.img.gz` 属于**发布制品**。真要发布时按 `dual-platform-release` 技能处理：
版本号、`release-artifacts.json` 的规则、网站下载卡、以及带 SHA 的线上复核。
本手册不重复那套流程——设计文档 §11 已说明发布留给独立变更。
