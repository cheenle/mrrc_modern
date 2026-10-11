# 树莓派镜像与 W103D 对齐 · 打包结构统一 · 设计

- **日期**：2026-10-11
- **状态**：待评审（本文件通过后进入实现计划）
- **相关**：`packaging/box/`、`packaging/rpi/`、`docs/superpowers/specs/2026-09-09-raspberry-pi-image-design.md`（其 §4/§8 被本文件修订）、`docs/RASPBERRY_PI_GUIDE.md`、`docs/w103d_pack.md`
- **分期**：本期只改构建侧并各自真跑一次（**不发布镜像**）；最终 Pi 镜像等 W103D 局域网设置页（`2026-10-11-w103d-lan-box-settings-design.md`，15 个 task 已完成 4 个）落地后再构建发布。

---

## 1. 背景与动机

线上树莓派镜像是 **v1.14.1**（2026-09-30），仓库已到 1.25.5 + 未发布条目。W103D 盒子镜像（1.25.5，2026-10-11 重建）已经带了一批 Pi 线完全没有的能力，Pi 线自己则停在 2026-09-09 设计批准时的状态：

| 能力 / 设置 | W103D 实发 | Pi 现状 |
| --- | --- | --- |
| 11 型号 profiles | `profiles/*.env` 装到 `/opt/mrrc_modern/profiles/` | 不拷（型号切换器没有数据） |
| FT-710 真频谱 | `vendor/ftdi/libft4222.so`（aarch64）+ `libftd2xx.so` 符号链接 + `99-mrrc-ftdi.rules` | 不装；网站卡片至今写着「需用户手动放库」 |
| Cloud Hub | `fleet/frpc`（多源校验下载） | 无 |
| 一键升级通道 | `/usr/local/bin/mrrc-update` | 无 |
| `mrrc-radio` CLI | wrapper 指向 venv | 无 |
| **D-11 陷阱** | unit 同时钉 `EnvironmentFile` 与 `Environment=MRRC_CONFIG_FILE` | 缺 `Environment=`：Cloud Hub 会「显示已连接但上游 TLS 校验失败」 |
| 服务日志落盘 | `logs/server-stdout.log`（诊断包要读） | 只进 journal |
| env 默认值 | `MRRC_PTT_MAX_TX_SECONDS=120`、`MRRC_RX_BUFFER_MS=100` | 缺（RX 缓冲是 USB 声卡在真机上实测出来的） |
| venv 构建 | `install.sh --yes --install-service --no-scope`（依赖表单一权威） | 裸 `pip install -r requirements.txt` |
| 导入门禁 / 回收 | 含 `net_wifi`、`box_ops`；清 `/tmp`、`__pycache__`、apt lists | 缺两项；且 stage4 里**提交了两个 `.pyc`** 和一份 `firstboot_wrapper.py` 副本 |
| 镜像源开关 | apt / pip / GitHub 三个开关（国内实测 65 KB/s → 829 KB/s 级） | 一个也没有 |

同时，两条构建线已经各自漂移出**具体的、可指认的**分歧：`firstboot_wrapper.py` 两份（只差 4 行：启动盘路径、绑定地址、文档串）、排除清单三份（Pi 构建器 / box overlay / 就地升级器，靠测试逐字节比对维持）、`mrrc-show-password` 放在 Pi 目录而 box overlay 反向去取、Pi 的 systemd 单元漏了 D-11 那一行。

**根因是结构性的**：两套"往 rootfs 里装同一个服务"的实现，漂移只由测试事后发现。本设计把服务层抽成唯一权威，平台差异变成少量显式参数，并用测试把"有意差异"钉成断言。

---

## 2. 目标

1. **服务层单一权威**：`/opt/mrrc_modern` 的内容、`/usr/local/bin/*`、两个 systemd 单元、env 默认值、udev 规则、frpc、导入门禁、空间回收 —— 只有一份实现，两个镜像共用。
2. **排除清单只有一处**：Pi 构建、box 构建、就地升级三处都调用同一个 `stage-tree.sh`。
3. **Pi 线补齐上表全部能力**（板级专属件除外）。
4. **Pi 构建可用国内源**：apt（debootstrap + 镜像源）/ pip / GitHub 三个开关，语义与 W103D 三件套一致（置空即回上游）。
5. **两条线各真跑一次**：本期结束时，两台机器的构建脚本都**被执行过**并留下产物证据（四步验证），而不是"测试是绿的"。
6. 有意差异**被断言**而不是被遗忘：全仓只允许两处按平台取值，且两处取值在测试里逐字写死。

### 非目标

- **本期不发布任何镜像**，不动网站卡片与 `release-artifacts.json`（下期做）。
- 不动服务端 Python（`server.py`、`box_ops.py` 等）与非打包侧脚本。
- 不做 W103D 局域网设置页的任何部分（helper / polkit / preseed / 页面）——那是另一份设计，落完后再由服务层吸收（见 §9）。
- 不做 Pi 的真机验收（需要硬件）。构建配置里的默认主机名本期就改成 `MRRC-Modern`（§6.3），但**对外文案**（网站卡片、`RASPBERRY_PI_GUIDE.md` 里的 `raspberrypi.local`）留到下期随最终镜像一起改——本期产物不发布，文案与产物的不一致只存在于构建机。
- 不统一 `install.sh` 本身（它已是跨平台单一权威），本设计只是让两条线都**通过它**建 venv。

### 成功判据

- `tests/` 全绿（含本设计新增的守卫测试；基线那 1 红见 §11）。
- `packaging/box/build-image.sh` 与 `packaging/rpi/build-image.sh` 各跑到底，产物通过 §8.2 的四步验证。
- 全仓 `--exclude` 排除清单字面量只出现在 `packaging/common/stage-tree.sh`（测试断言）。

---

## 3. 目标形态

```
packaging/
├── common/                          # 唯一权威，平台无关
│   ├── stage-tree.sh                # ★ 排除清单唯一出处 + rsync 落树（bash 3.2 兼容）
│   ├── mrrc-service-layer.sh        # ★ 服务层全部步骤（chroot 内运行，无平台分支）
│   ├── packages.txt                 # ★ 系统包清单（W103D 实测的那份超集）
│   ├── firstboot_wrapper.py         # ★ 唯一副本；MRRC_BOOT_DIR 由单元传入
│   ├── mrrc-show-password           # 从 rpi/stage4 搬来
│   ├── fetch-frpc.sh                # 从 box/ 搬来
│   └── profiles/*.env               # 11 份型号画像，从 box/ 搬来
├── box/                             # 平台壳 A（W103D / Armbian / Docker + losetup + chroot）
│   ├── build-image.sh               # 基本不动（签名与调用契约不变）
│   ├── box-overlay.sh               # 瘦身：用户/组、板级件、调服务层
│   ├── verify.sh                    # 不动
│   ├── firstboot_wrapper.py         # 删除（搬到 common/）
│   └── vendor-*.md、README.md       # 路径引用同步
└── rpi/                             # 平台壳 B（Raspberry Pi OS / pi-gen）
    ├── build-image.sh               # 宿主侧：镜像源改写、调 stage-tree、pi-gen
    ├── pi-gen-stage4/
    │   ├── 00-install-packages/00-packages   # 只留平台额外项（e2fsprogs）
    │   ├── prerun.sh                # 拷 files/ 进 rootfs + FATAL 校验
    │   └── 01-deploy-mrrc/00-run-chroot.sh   # 极薄：组、属主、调服务层
    │   └── (files/ 整个删除)
    └── verify-image.sh              # 升级为四步产物验证
```

`firstboot_wrapper.py` 的搬迁同时消掉一处重复；`profiles/` 与 `fetch-frpc.sh` 搬到 `common/` 的原因不是审美：服务层（平台无关）要用它们，留在 `box/` 下等于让共用层反向依赖某个平台目录——这正是 `mrrc-show-password` 今天已经在犯的错（放在 `rpi/` 里被 box 调用）。

**需要跟改的引用**（机械改动，计划里逐条列出）：`tests/test_box_profiles.py` 的 `PROFILE_DIR`/`IMPLEMENTERS`、`tests/test_image_imports.py` 的 `BUILDER`/文档串、`tests/test_mrrc_radio.py:123`、`tests/test_setup_retired.py:23-24`、`packaging/box/verify.sh` 与 `README.md`、`.agents/skills/dual-platform-release/SKILL.md`、`AGENTS.md`（其中 `net_wifi.py / linux/setup_ap.py` 那一行已是退役前的旧文，本次一并订正）。

---

## 4. 接口契约

### 4.1 服务层入参（环境变量）

| 变量 | 默认 | 含义 |
| --- | --- | --- |
| `REPO_SRC` | `/src` | 已落好的树（服务层只读它，不自己 rsync） |
| `MRRC_HOME` | `/opt/mrrc_modern` | 安装位置 |
| `MRRC_USER` | `mrrc` | 服务账号（必须已存在） |
| `MRRC_VERSION` | 从 `REPO_SRC/CHANGELOG.md` 顶部取 | version.txt / VERSION |
| `MRRC_WEB_HOST` | `0.0.0.0` | **有意差异 ①**：Pi 传 `::`（双栈） |
| `MRRC_BOOT_DIR` | `/boot` | **有意差异 ②**：Pi 传 `/boot/firmware`；写进单元，仅供 firstboot 读 preseed |
| `MRRC_PIP_INDEX` | 空 = PyPI | 经 chroot 传给 `PIP_INDEX_URL` |
| `MRRC_EXTRA_PACKAGES` | 空 | 空格分隔的平台额外包（与 `packages.txt` 合并后一次 apt 装完） |

服务层内**没有任何平台条件分支**；板级件留在各自壳里（见 §6）。

### 4.2 落树时机（避免 2.3 GB 的 `website/` 被搬两趟）

| 路径 | 谁调 `stage-tree.sh` | 目的地 |
| --- | --- | --- |
| W103D | `box-overlay.sh`（chroot 内，宿主仓库已 `--bind` 到 `/src`） | `/opt/mrrc_modern` |
| Pi | `build-image.sh`（**宿主侧**，rsync 到 stage4 的 `files/opt/mrrc_modern`），`prerun.sh` 拷进 rootfs 后 FATAL 校验 | `/opt/mrrc_modern` |
| 就地升级 | `linux/mrrc_update.sh` 用**拉下来的新树里那份** `packaging/common/stage-tree.sh` | `/opt/mrrc_modern` |

服务层不自己 rsync，只断言 `$MRRC_HOME/requirements.txt` 存在（缺了就 FATAL）——把"树没落好"变成构建期的即时错误，而不是开机后的 ImportError。

### 4.2.1 服务层自己的素材（`MRRC_ASSETS`）

服务层要用到的东西里有几件**不在装机树里**：`packaging/` 被排除清单挡在 `/opt/mrrc_modern` 之外，而 profiles、`fetch-frpc.sh`、`mrrc-show-password`、`firstboot_wrapper.py` 都在 `packaging/common/` 下。所以两侧构建器都必须在 chroot 里备好一份素材目录，服务层只认这一个路径：

| 变量 | 默认 | 内容 |
| --- | --- | --- |
| `MRRC_ASSETS` | `/opt/mrrc-build` | `mrrc-service-layer.sh`、`stage-tree.sh`、`packages.txt`、`fetch-frpc.sh`、`mrrc-show-password`、`firstboot_wrapper.py`、`motd-10-mrrc`、`profiles/*.env` |

**为什么不是 `/tmp`**：pi-gen 的 `on_chroot`（`scripts/common`）把 `${ROOTFS_DIR}/tmp` 挂成 tmpfs，而 `prerun.sh` 在挂载之前就往 `${ROOTFS_DIR}/tmp` 拷了东西——挂载一上就把它们盖住，chroot 里看不到。`/opt` 没有这个问题。

- Pi：`build-image.sh` 在宿主侧把 `packaging/common/` 整个拷进 stage4 的 `files/opt/mrrc-build/`（**w 与 x 位一起带**，脚本要在 chroot 里执行）。
- box：`box-overlay.sh` 在 chroot 里 `cp -a /src/packaging/common /opt/mrrc-build`，路径与 Pi 完全一致。
- 装着代码的 `vendor/ftdi/`、`vendor/mediatek/` 仍在装机树里（未被排除），服务层从 `$MRRC_HOME/vendor/` 取。

服务层结束前 `rm -rf "$MRRC_ASSETS"`（Pi 上它是构建期临时目录，box 上是仓库的一份拷贝；两边都不允许进镜像）。

### 4.3 `stage-tree.sh` 的约束

- **bash 3.2 兼容**：Pi 构建在 macOS 上调用它，macOS 的 `/bin/bash` 是 3.2 —— 禁用 `declare -A`、`${var,,}`、`mapfile`、`readarray`。测试用 grep 断言。
- 接口：`stage-tree.sh <src> <dst>`，`rsync -a`，**不带 `--delete`**：镜像构建是从空目录落树，删无可删；就地升级曾经依赖 `--delete`，其处置见 §4.4。
- 排除清单以**字面量**保留在脚本里（可读、可 grep、可被测试计数），不用外部清单文件——外部文件会让"谁定义了这份清单"变成第二次跳转，而这里的价值是**只有一处**。

### 4.4 `linux/mrrc_update.sh` 的改动（随本期落地）

它今天是排除清单的第三份拷贝。改为**驱动器**：先把源树取到磁盘，再调新树里的 `stage-tree.sh`。

- 本地目录：`SRC_TREE="$SRC"`，直接调 `"$SRC_TREE/packaging/common/stage-tree.sh" "$SRC_TREE" "$MRRC_HOME"`。
- git URL：今天的实现是 `git -C "$MRRC_HOME" fetch …` —— `/opt/mrrc_modern` 不是 git 仓库，这条路径**本来就是坏的**（保留了 `--delete` 语义的旧设计残留）。本期改为 `git clone --depth 1 --branch "$BRANCH" "$SRC" "$TMP"`，`SRC_TREE="$TMP"`，用完后清理。
- 新树里没有 `packaging/common/stage-tree.sh` 时**硬失败**并说明原因（"取到的树比打包结构变更更旧，无法确定该装哪些文件"）。这条路径只会在有人显式指定一个变更前的分支时触发。
- 保留 `--delete` 语义：`stage-tree.sh` 不删，删除由驱动器在调用前用一次独立的 `rsync --delete --exclude '*' --include …` 表达 —— **本期不实现**，改为在 `mrrc_update.sh` 里显式记录该取舍：迁移到"不删"的语义需要一次专门评估（就地升级删掉过时文件与保护每台机器的状态是同一枚硬币），本期的范围只是**让清单只有一份**。

---

## 5. 镜像源开关（Pi 新增）

pi-gen 只有 `APT_PROXY`，没有镜像开关；但四个上游点都是克隆里的普通文件，构建前幂等改写（改写发生在 `build-image.sh` 里，与 W103D 改写 `sources.list` 的做法同源）：

| 改写点 | 上游 | 默认换成 |
| --- | --- | --- |
| `stage0/prerun.sh`：`bootstrap ${RELEASE} "${ROOTFS_DIR}" http://deb.debian.org/debian/` | deb.debian.org（W103D 实测容器内 **65 KB/s**） | 清华 `mirrors.tuna.tsinghua.edu.cn/debian` |
| `stage0/00-configure-apt/files/sources.list`（3 行 deb） | 同上 | 同上 |
| `stage0/00-configure-apt/files/raspi.list` | `archive.raspberrypi.com` | 清华 `mirrors.tuna.tsinghua.edu.cn/raspberrypi` |
| `Dockerfile` 容器自身的 `apt-get -y update` | deb.debian.org | 同 APT 开关（在 `RUN` 里先写 sources.list 再 update；容器层一次构建后被 Docker 缓存） |

开关（语义与 W103D 三件套一致，置空即回上游）：

- `MRRC_PI_APT_MIRROR`（默认清华）
- `MRRC_PI_PIP_INDEX`（默认清华 pypi，经服务层传给 `PIP_INDEX_URL`）
- `MRRC_FRP_PROXY` —— 在 chroot 里生效的名字，与 box 侧一致（宿主开关叫 `MRRC_PI_FRP_PROXY`，与 `MRRC_BOX_FRP_PROXY` 同形）；`fetch-frpc.sh` 已经只认 `MRRC_FRP_PROXY`。

判据沿用 W103D 的实测口径：**"pip/apt 连着但零下载 = 源慢，不是代码坏"**；`gh-proxy.com` 之类只加速不换货，`fetch-frpc.sh` 仍逐字节比对官方 checksums。

pi-gen 的失败恢复沿用现状：`PRESERVE_CONTAINER=1`，失败后 `CONTINUE=1 IGNORE_FILE_CHANGES=1 ./build-docker.sh` 续跑；`work/` 与 `deploy/` 由 `PIGEN_DOCKER_OPTS` 挂在宿主 `build/pi-gen` 卷上。

---

## 6. 平台壳的边界

### 6.1 box 壳（`packaging/box/box-overlay.sh` 瘦身后）

保留：系统用户/组（`adduser --system`）、`dialout/audio` 附加组、MT7663 N9 固件 + `dpkg-divert`、板级 mask（`dnsmasq.service`、`serial-getty@ttyFIQ0.service`）、调用服务层。
交出：profiles、FTDI 库安装、**FT4222 的 udev 规则**（Pi 用同一个 FT4222，规则必须共用，不能只留在 box 侧）、frpc、三个 CLI、两个单元、env 默认值、version 戳、导入门禁、回收。

### 6.2 rpi 壳

- `00-packages`：**只留 `e2fsprogs`**（Pi 侧原有的一项平台额外件）。其余包进 `common/packages.txt`，由服务层统一 `apt-get install`。这样"该装哪些系统包"只有一个答案（W103D 实测的那份超集，含 `libopus0`/`libopus-dev`/`libasound2-dev`/`portaudio19-dev`/`python3-dev`/`avahi-daemon`）。
- `prerun.sh`：拷 `files/` 进 rootfs，并 FATAL 校验 `requirements.txt` 与 `version.txt` 都在（今天只校验前者的两项中的一个）。
- `01-deploy-mrrc/00-run-chroot.sh`：`usermod -aG dialout,audio mrrc` → `chown` → `. /opt/mrrc-build/build.env` → `bash /opt/mrrc-build/mrrc-service-layer.sh`。
- **删掉整个 `files/` 树**（含两个 `.pyc`）。服务层产出的东西不再有第二份。

### 6.3 有意差异白名单（测试逐字断言）

| 差异 | box | rpi | 理由 |
| --- | --- | --- | --- |
| `MRRC_WEB_HOST` | `0.0.0.0` | `::` | `::` 是超集且 `_bind_dual_stack_socket()` 已处理 V6ONLY=0；不回退能力 |
| `MRRC_BOOT_DIR` | `/boot` | `/boot/firmware` | 两个发行版挂载启动分区的位置不同 |

除此之外**不允许**存在按平台取值的行为。主机名统一为 `MRRC-Modern`（pi-gen `config` 写 `TARGET_HOSTNAME=MRRC-Modern`；`box_ops.DEFAULT_HOSTNAME` 已经是这个值，设置页落地后不会再出现"显示的名字 ≠ mDNS 名字"的静默分叉）。

`/etc/update-motd.d/10-mrrc` 进服务层（今天只在 Pi 侧），内容改成用 `$(hostname)` 动态生成，不再写死 `raspberrypi.local`；两侧的发行版都有 `update-motd.d`，文件在有它的地方自然生效、没有的地方是惰性的。

---

## 7. 数据流（一次 Pi 构建）

```
宿主 macOS
  ├─ 检查 Docker / 磁盘（沿用现状）
  ├─ 改写 pi-gen 克隆的 4 处上游源（§5，幂等）
  ├─ rsync packaging/pi-gen-stage4/ → build/pi-gen/rpi-stage4/
  ├─ stage-tree.sh <repo> rpi-stage4/01-deploy-mrrc/files/opt/mrrc_modern   ← 排除清单唯一出处
  ├─ 拷 common/{stage-tree.sh,mrrc-service-layer.sh,packages.txt,fetch-frpc.sh,mrrc-show-password,firstboot_wrapper.py,motd-10-mrrc,profiles/} → files/opt/mrrc-build/
  ├─ version.txt / VERSION（两处，来自 CHANGELOG 顶部）
  └─ build-docker.sh（PRESERVE_CONTAINER=1）
        └─ pi-gen stage0→2 原生 arm64，stage rpi-stage4：
              prerun.sh   : files/ → rootfs（FATAL 校验）
              00-run-chroot.sh : 组 → chown → bash /opt/mrrc-build/mrrc-service-layer.sh
                    └─ apt(packages.txt + EXTRA) → install.sh 建 venv → env 默认值
                       → 单元/CLI/udev/frpc/FTDI → 导入门禁 → 回收(/tmp, /opt/mrrc-build, __pycache__)
  ├─ xz -9 → dist/rpi/MRRC-Modern-v<ver>-rpi64.img.xz
  └─ verify-image.sh（四步）
```

---

## 8. 验证

### 8.1 守卫测试（新增 `tests/test_packaging_parity.py`，与 `ExclusionParityTests` 同源风格）

1. **排除清单只有一处**：全仓（`packaging/`、`linux/`）匹配 `--exclude "…"` 的文件集合恰为 `{packaging/common/stage-tree.sh}`；并断言该文件里的条目数 ≥ 现状的条目数（防"删空清单让测试通过"）。
2. **两个构建器都调用共用层**：`packaging/rpi/build-image.sh` 含 `stage-tree.sh`，`packaging/rpi/pi-gen-stage4/01-deploy-mrrc/00-run-chroot.sh` 含 `mrrc-service-layer.sh`，`packaging/box/box-overlay.sh` 两者都含。
3. **Pi 的 `files/` 不得含服务层产物**：`packaging/rpi/pi-gen-stage4/01-deploy-mrrc/files/` 下不允许出现 `mrrc-modern.service`、`mrrc-firstboot.service`、`firstboot_wrapper.py`、`mrrc-show-password`、任何 `*.pyc` —— 这是今天两处漂移的回归闸门。
4. **包清单唯一**：`packaging/rpi/pi-gen-stage4/00-install-packages/00-packages` 的条目必须全部在「允许的平台额外项」白名单里（当前只有 `e2fsprogs`），且不得与 `common/packages.txt` 有交集。
5. **有意差异被断言**：`packaging/rpi/**` 与 `packaging/box/**` 中 `MRRC_WEB_HOST=` 的取值恰为 `::` 与 `0.0.0.0`；`MRRC_BOOT_DIR` 恰为 `/boot/firmware` 与 `/boot`。（等价于"§6.3 之外没有第二处平台差异"的可检查形式。）
6. **`common/*.sh` 可执行位**（0644 陷阱，`box-overlay.sh` 犯过）。
7. `stage-tree.sh` 禁 bash 4 语法（`declare -A` / `${x,,}` / `mapfile` / `readarray`）；且 `common/mrrc-service-layer.sh` 必须同时含 `rm -rf /tmp/*` 与 `rm -rf "$MRRC_ASSETS"`（素材与 install.sh 的临时文件靠它们清掉，防被后人"顺手优化"掉）。

### 8.2 产物四步验证（`packaging/rpi/verify-image.sh` 升级）

与 `docs/w103d_pack.md` 同一套配方（Pi 镜像是 MBR + ext4 p2，与 W103D 同构）：

1. `xz -t` + 体积上下界（沿用现状）；
2. 容器内 `losetup -P` 挂 p2（复用 `packaging/box/part-nodes.sh` 的 mknod 逻辑，容器无 udev）；
3. ① **代码身份**：产物里的 `server.py` / `requirements.txt` 等与工作区 `sha256sum` 逐字节相同；② **装机内容**：11 份 profiles、`/usr/local/bin` 三件、`fleet/frpc` 是 aarch64 ELF、FTDI 库 + 符号链接、`mrrc` 用户存在、`/tmp` 与 `/opt/mrrc-build` 零残留、无 `__pycache__`；③ **D-11**：单元里 `EnvironmentFile` 与 `Environment=MRRC_CONFIG_FILE` 指向同一文件且 `User=mrrc`；④ **arm64 原生 chroot 真跑**：`venv/bin/python -c "import fastapi, numpy, pyaudio, lameenc, cryptography, serial"` 与 `ctypes.CDLL("libopus.so.0")`。

### 8.3 真跑（本期结束证据）

- `packaging/box/build-image.sh`（基底已缓存：`build/w103d/w103d.img` 在，不重下不重解压）→ 产物四步验证；
- `packaging/rpi/build-image.sh`（全量：`build/pi-gen/work` 为空，从 debootstrap 开始）→ 产物四步验证；
- 两次都不发布、不更新网站、不动 `dist/` 之外的对外产物。

---

## 9. 分期与衔接

**本期交付物**：§3–§8 全部；新建 `tests/test_packaging_parity.py`；`packaging/rpi/verify-image.sh` 升级为四步；新建 `docs/rpi_pack.md`（构建手册，与 `w103d_pack.md` 同构：前置、缓存位置、四个镜像源改写点、失败续跑、产物四步验证、本次实跑记录）；订正 `AGENTS.md` 与 `.agents/skills/dual-platform-release/SKILL.md` 里的路径与已退役描述。两侧各真跑一次；**不发布**。

**下期（W103D 设置页 15 个 task 落完后）**：

1. 服务层吸收设置页新增件（`mrrc-box-helper.service`、polkit 规则、Wi-Fi preseed unit）；
2. 最终构建 Pi 镜像 → 四步验证 → 发布：
   - 网站 Pi 卡片：改为「FTDI 内置」，主机名改 `MRRC-Modern.local`，字节数/SHA 更新；
   - `release-artifacts.json` 加 Pi 条目；
   - `docs/RASPBERRY_PI_GUIDE.md`：镜像版本、`MRRC-Modern.local`、FTDI 段落删除、写明"别用 Imager 改用户名"（`FIRST_USER_NAME=mrrc` 是镜像的服务账号前提）、写明 `mrrc-update` 的存在与"无回滚"；
   - `docs/RASPBERRY_PI_GUIDE.md` 需要出现带版本号的文件名（`release_check` 的 `[pi-guide]` 目前是 `·` 警告：模式找不到）。

**下期的判据**：`release_check` 为绿（需要先修 §11 那 1 红）。

---

## 10. 风险与未验证项

| 风险 | 处置 |
| --- | --- |
| pi-gen 全量构建在国内网络下的总时长未知（debootstrap + 三层 apt） | §5 四个改写点先落地，实测记录到 `docs/rpi_pack.md`（本期新建，与 `w103d_pack.md` 同构） |
| 容器自身的 `apt-get`（Dockerfile）不受 `config` 控制 | 已列为第 4 个改写点；若仍慢，`docker pull debian:bullseye` 预拉 + Docker Desktop 代理是备用手段 |
| `install.sh` 在 pi-gen 的 on_chroot 环境里能否跑通（OS 检测、apt、systemd 检测） | W103D 已在 Armbian chroot 里验证过同一条路径；Pi 是 Debian bookworm arm64，风险中低。真跑即验证；失败则退化为"服务层直接建 venv"，但那会重新引入第二份依赖流程——记录为**不允许**的退化 |
| 就地升级器改动（§4.4）触及已发货文件 | 改动被 `tests/test_box_profiles.py` 与新增的 parity 测试共同围住；本期不动 `--delete` 语义，缩小面 |
| `MRRC-WEB_HOST=::` 在 Pi 上的实际可达性（含 avahi 发布 AAAA） | 沿用现状（1.14.1 已这么发），本期不改行为，只把它从"隐式"变成"被断言的差异" |
| 从产物里删掉 `files/` 后，pi-gen 的 `EXPORT_IMAGE` / `SKIP_IMAGES` 逻辑是否受影响 | 二者都在 `build-image.sh` 里 `touch`，与 `files/` 无关；真跑验证 |

## 11. 已知遗留（不属本期，但会挡下期发布）

- `release_check` 基线 1 红：英文站 W103D 卡片写 `1,154,545,271 bytes / 5d74f737…`，真产物是 `1,268,146,785 / 5f0ae387…`（2026-10-11 12:08 重建后卡片未同步）。建议本期顺手修掉（下期发布前必须绿）。
- `AGENTS.md` 仍在描述已退役的 `linux/setup_ap.py` 与热点（2026-10-11 退役），本次一并订正。

## 12. 明确不做（YAGNI）

- 不合并两条构建链的**平台部分**（Docker+losetup vs pi-gen）——它们的差异来自基座镜像，强行统一会让两边都难读。
- 不引入配置生成器（模板 + YAML/TOML 渲染单元与 env）：两条线上的参数只有两个，模板机制的成本大于收益。
- 不做 Pi 的在机验收脚本（W103D 的 `verify.sh` 是 12 项真机检查）——下期发布时再定，本期不宣称硬件可用。
- 不改服务端代码；不提升级通道的自动化（`upgrade_core.py` 切片 2 仍未开工）。
