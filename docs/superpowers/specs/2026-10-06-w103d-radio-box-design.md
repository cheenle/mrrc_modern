# W103D 通用电台服务端镜像（含 Cloud Hub 公网接入）设计

> 状态：待实施
> 日期：2026-10-06
> 目标平台：ZTE 云电脑 W103D（Amlogic S905L3A / G12A）
> 上游：`ophub/amlogic-s9xxx-armbian`（型号库条目 ID 307 `ZTE-W103D`）

## 1. 目标与边界

**目标**：把一台 W103D 做成**通用电台服务端**——11 个已注册电台型号可一键切换，经现有 Cloud Hub 暴露到公网且允许发射。

**边界（刻意不做）**：

- 不发明新协议、不改服务端行为、不动 CAT/音频/PTT 代码路径。本设计只做**部署层**。
- **不自建 Armbian 镜像**。W103D 的板级补丁（SDIO→SD_EMMC_B 路由、MT7663S 传输层、固件 overlay）活在 ophub/unifreq 的内核树里；自建等于长期跟进那套补丁栈，收益不抵维护成本。依赖 ophub 是**刻意的取舍**，缓解手段是钉 SHA-256 + 只从官方 release 取。
- 不实现 `upgrade_core.py` 的 slice 2（自动升级）。更新走最小 `mrrc-update`。
- 不移植预建 venv。镜像自带 Python 3.11，`install.sh` 在 chroot 内自建即可。

## 2. 基线事实（全部实测，非估算）

### 2.1 目标镜像

```
Armbian_26.11.0_amlogic_s905l3a-w103d_bookworm_6.18.54_server_2026.10.01.img.gz
  大小      852,336,261 B（812.9 MiB），解压后 3.4 GB
  SHA-256   998d5244ac2274077c091050b9db620c22a8412989766fbd728b045a0fed1ae9  ✅ 已下载校验通过
  Release   Armbian_bookworm_arm64_server_2026.10
```

文件名里四个标记都不可替换：`amlogic`（平台）、`s905l3a-w103d`（**专属板级配置**，换通用 `s905l3a` 会丢 WiFi/BT）、`bookworm`（**Python 3.11**）、`6.18.54`（MT7663S 补丁 + 原厂 N9 固件所在系列）。

### 2.2 镜像内部分区与容量（实测）

| 项 | 值 |
| --- | --- |
| 分区表 | **MBR**（`FDisk_partition_scheme`，非 GPT） |
| p1 | FAT32，511 MiB，偏移 4,194,304，卷标 `BOOT` |
| p2 | ext4，3000 MiB，偏移 541,065,216，卷标 `ROOTFS` |
| rootfs 总量 / 已用 / **可用** | 3000.0 / 2036.3 / **963.7 MiB（32.1%）** |
| inode | 58,656 / 192,000 |
| 构建者挂载点 | `/builder/build/t`（ophub 构建主机） |

### 2.3 镜像内已有的运行时（实测，决定还要装什么）

| 组件 | 状态 | 结论 |
| --- | --- | --- |
| Python | **3.11** | 对齐 Pi 镜像，无需换发行版 |
| systemd / **sudo** | 已装 | systemd 管服务；chroot 里 `install.sh` 的 `sudo tee` 可用 |
| libasound.so.2 + **libasound2-dev** | 只有运行时 | **libasound2-dev 缺** —— PyAudio 要就地编译才用得上 |
| **portaudio19-dev + libportaudio2** | **都缺** | PyAudio 的运行时库与头文件都不在 |
| **libopus0** | **缺** | 没有它服务端**静默退回 PCM**（带宽成倍），必须装 |
| **python3-venv / python3-pip / python3-dev** | **都缺** | `python3 -m venv` 直接失败（没有 `ensurepip`）—— 见下方更正 |
| **gcc / make** | 已装 | 无 wheel 的包也能就地编译 |
| **git / rsync / curl / sudo** | 已装 | `mrrc_update.sh` 与镜像同步可用 |
| NetworkManager / nmcli / wpa_supplicant | 已装 | 有线+无线配网可用 |
| **mt7663 驱动** | **已装** | WiFi 支持的镜像内直接证据 |

> **更正（2026-10-07，首次真构建）**：上面五行原写「已装（dpkg 确认）」，**是错的**。用同一个方法（在 rootfs 的 dpkg 数据库里逐个查 `Package: <name>`）重测，实际如上表：`libopus0`、`portaudio19-dev`、`libportaudio2`、`libasound2-dev`、`python3.11-venv`、`python3-pip`、`python3-pip-whl`、`python3-setuptools-whl`、`python3-dev` **全部不存在**；在的是 `libasound2`（运行时）、`gcc`、`make`、`rsync`、`git`、`curl`、`sudo`、Python 3.11。
>
> **后果不是「多装几个包」，而是构建直接失败**：`install.sh` 在**第 2 步**建 venv（需要 `ensurepip`，来自 `python3.11-venv`），而系统包在**第 3 步**才装 —— venv 一失败就 FATAL 退出，apt 根本没机会跑。`libopus0` 那条更隐蔽：它不会让构建失败，只会让盒子交付后**静默退回 PCM**。
>
> **修法**：`box-overlay.sh` 在调 `install.sh` **之前**显式装齐 `python3.11-venv python3-dev portaudio19-dev libportaudio2 libasound2-dev libopus0 libopus-dev`，不再依赖 `install.sh` 的步骤顺序。apt 增量因此**不是 ≈0，而是约 80 MiB**（已计入 §2.4）。

### 2.4 增量占用估算（对照 963.7 MiB 可用）

| 项 | 大小 | 依据 |
| --- | --- | --- |
| 代码本体 | **≈ 13 MiB** | 构建产物内实测（`du`，已含 backends/ 与全部源码，533 文件的原估 **175.4 MiB 高估了约 13 倍**） |
| venv | **≈ 115 MiB** | 产物内实测（原估 90–120 MiB，准） |
| frpc | **≈ 15 MiB** | 产物内实测（frp 0.71.0 linux-arm64） |
| static/（前端） | ≈ 1.5 MiB | 产物内实测 |
| **FTDI 库（aarch64）** | ≈ 0.75 MiB | 一个 ELF + 一个符号链接（D-9） |
| apt 增量 | **≈ 80 MiB** | 七个包（§2.3 更正） |
| **/opt/mrrc_modern 合计** | **≈ 145 MiB** | 产物内 `du -sh` 实测 |

**结论（2026-10-07 实测推翻原估算）**：首次真构建完成后的实测数据 —— `df` 显示 rootfs 可用空间 **948 MiB → 905 MiB**，即**净增只有 43 MiB**；`/opt/mrrc_modern` 为 **145 MiB**。两者之差来自 overlay 末尾的回收步骤（`apt-get clean` + 清 apt lists + 清 `/root/.cache` 与 `/tmp`，回收了约 100 MB 缓存）。

原估「合计 280–310 MiB、装后余 650–680 MiB」**过于悲观**；实际装后余 **905 MiB**。**不需要 growpart，不需要扩容镜像**，且余量比原估多出 200 MiB 以上。（首启后 eMMC 自动扩到 32 GB，运行期空间另有约 29 GB。）

### 2.5 服务端自身负载（实测 + 交叉验证）

用已入库的 `dev_tools/bench_mrrc.py`（走真实模块：`resample_pcm` / `RxOpusEncoder` / `TxOpusDecoder` / `parse_scope_frame` / 1701 字节打包）在 M2 上实测：RX(50/s 重采样+峰值+Opus 编码) 0.96% + spectrum(30/s 解析+打包) 0.37% + TX(50/s 解码+重采样) 0.19% + state(10/s JSON) 0.01% = **1.53% of one core**。

> 这个数字由工具定义，不由文档定义：在盒子上跑同一个脚本就能得到真实值（README 的验收清单里就是这么用的）。跑法：`.venv/bin/python dev_tools/bench_mrrc.py`（或盒子上的 `/opt/mrrc_modern/venv/bin/python`）。

外推（A53@1.8 ≈ 0.57× Pi 4 单核）：Pi 4 ≈ 17% → **W103D ≈ 30%**，含真实开销（ws 逐帧 send、GIL 争用、GC）**35–45% of one core**。

**交叉验证**：项目自身性能指南的 SLO 是「CPU 使用率 ~15%」，与实测外推的 17% 吻合（两个独立来源）。CPU 不是瓶颈。

### 2.6 11 个已注册电台型号（`known_models()` 实测）

`ft710` `ic7300` `ic7300mk2` `ic705` `ic7610` `ic7760` `ftdx10` `ftdx101d` `ftdx101mp` `ftx1` `ft891`

其中 `ic705` `ic7610` `ic7760` `ftdx10` `ftdx101d` `ftdx101mp` `ftx1` `ft891` 共 **8 个是未验证型号**（`verified=false`，受 AD-019 / NFR-067 管辖）。

## 3. 架构：两段式

构建期把一切不需要硬件的东西烘焙进镜像；首启只做必须碰硬件的探测。

```text
【构建期】macOS（Apple M2 / arm64 → 原生 aarch64，无需 qemu）+ Docker --privileged
  ① 下载 ophub 镜像 → 校验 SHA-256（998d5244…）
  ② 解压 → losetup 挂载 MBR 分区（p1 BOOT / p2 ROOTFS）
  ③ 测量并断言 rootfs 可用空间 ≥ 阈值（实测 963.7 MiB；不满足则中止并报原因）
  ④ chroot 进 p2 铺 overlay
  ⑤ 卸载 → 重新压缩 → 出 SHA-256
  ⑥ 产物：MRRC-Modern-<ver>-w103d.img.gz

【运行期】W103D 真机
  首启：mrrc-firstboot → 探测串口/声卡 → 生成口令 + 自签 HTTPS 证书 → 启服务
  然后：手机浏览器 https://<盒子IP>:8888 → 设置页 Cloud Hub apply/connect
```

### 3.1 刷写路线（先 U 盘，后可选 eMMC）

镜像产出后**不直接刷 eMMC**。两条路线按顺序走：

| | 路线 1：U 盘试跑（先做，必须） | 路线 2：写 eMMC（验收通过后可选） |
| --- | --- | --- |
| 操作 | 写 U 盘/SD → 插盒子 → 盒子执行 `reboot update` | 先 `armbian-ddbr` 备份原厂整盘 → `armbian-install` |
| eMMC | **一个字节不动** | 被覆盖，全部 32 GB 可用 |
| 回滚 | 拔 U 盘即回安卓 | 用操作者手上的**线刷固件包** + MaskROM 短接 |
| 代价 | 占 1 个 USB 口；U 盘速度 | 无 |

**为什么必须按这个顺序**：U 盘路线让"刷坏"这件事在里程碑 1 验收之前概率为零；而镜像二次构建引入的变量（overlay 是否破坏了首启自动扩容、引导链是否完好）恰好都能在 U 盘路线上先看到。

**变砖兜底不依赖厂商内容**：Amlogic 的 MaskROM 在 SoC BootROM 里，短接可重新进入刷写模式。

### 3.2 构建期能做 / 不能做（chroot 内无硬件）

| 构建期写入镜像 | 必须留到首启 |
| --- | --- |
| `mrrc` 用户 + `dialout,audio` 组 | 串口探测（`/dev/ttyUSB*` / `ttyACM*`） |
| `/opt/mrrc_modern`（代码 + venv + apt/pip 依赖） | ALSA 声卡探测（电台 USB 声卡） |
| `fleet/frpc`（v0.71.0 linux-arm64） | 生成 Web 口令 |
| `mrrc-radio` → `/usr/local/bin` | 自签 HTTPS 证书（SAN 必须含盒子实际 IP） |
| `mrrc-modern.service` + `mrrc-firstboot.service`（enable） | 电台型号探测 |
| `packaging/box/profiles/*.env`（无串口、无声卡名） | |
| `mrrc.env` 默认值：`MRRC_WEB_HOST=0.0.0.0`、`MRRC_WEB_PORT=8888`、`MRRC_PTT_MAX_TX_SECONDS=120` | |
| **FTDI aarch64 库 + ft710 profile 的两个显式路径变量**（D-9） | |

## 4. 组件清单

| # | 路径 | 职责 |
| --- | --- | --- |
| 1 | `packaging/box/build-image.sh` | 构建期总入口：下载 → 校验 → 挂载 → chroot overlay → 卸载 → 压缩 → 出 SHA |
| 2 | `packaging/box/box-overlay.sh` | **在 chroot 内执行**的 overlay 脚本（用户/组、代码、venv、frpc、FTDI aarch64 库、mrrc-radio、systemd、motd） |
| 3 | `packaging/box/fetch-frpc.sh` | 取 frpc v0.71.0 linux-arm64 + 官方 checksums 校验 |
| 4 | `packaging/box/profiles/<model>.env` | 11 份 profile |
| 5 | `packaging/box/verify.sh` | 真机验收（10 项） |
| 6 | `packaging/box/README.md` | 一页操作单（刷写 → 首启 → Cloud Hub → 排障） |
| 7 | `linux/mrrc_radio.py` | `list` / `show` / `use <model> [--port <dev>]` |
| 8 | `linux/mrrc_update.sh` | 最小更新路径（拉代码 → pip → 重启） |
| 9 | `dev_tools/bench_mrrc.py` | 服务端热路径基准（终审 CPU 假设） |
| 10 | `tests/test_box_profiles.py`、`tests/test_mrrc_radio.py` | 无硬件单测 |
| 11 | `docs/w103d_pack.md` | 镜像构建手册（对标 `mac_pack.md` / `win_pack.md`） |
| 12 | 文档同步 | `SDD/12`（新增 §12.10）、`SDD/14` 版本行、`SDD/README` Quick Facts、`AGENTS.md` 模块表、`README.md`、`tests/README.md` 计数 |

**为什么放 `packaging/box/`**：与 `packaging/rpi/` 平行（rpi 造 Pi 镜像，box 在 ophub 镜像上二次构建）。根目录的 `deploy_*.sh` 是"部署到站点/Hub"，语义不同。

**复用而非重写**：

- `install.sh` 仍在 chroot 里被调用（`--yes`），负责依赖、Python 包、配置生成（DRY，不重写它的 10 步）
- 源码同步**照抄** `build-image.sh:38-45` 的排除清单
- 首启探测**复用** `linux/first_run.py` 的 `detect_serial_ports()` / `probe_radio_model()` / `update_env_file()`

## 5. 数据流

```text
构建期:
  ophub .img.gz ──校验──> .img ──losetup──> p2/rootfs
    ├── rsync 代码（权威排除清单） ──> /opt/mrrc_modern
    ├── install.sh --yes（chroot 内） ──> venv + 依赖 + 配置模板
    ├── fetch-frpc.sh ──> /opt/mrrc_modern/fleet/frpc
    ├── profiles/*.env ──> /opt/mrrc_modern/profiles/
    ├── mrrc_radio.py ──> /opt/mrrc_modern/linux/ + /usr/local/bin/mrrc-radio
    └── systemd units（enable mrrc-firstboot）

运行期:
  mrrc-firstboot ──> /opt/mrrc_modern/env/mrrc.env（口令 + 串口 + 声卡 + 证书路径）
                  └─> systemctl start mrrc-modern

  电台切换:
  profiles/<model>.env ──mrrc-radio use──> env/mrrc.env ──systemd restart──> server.py

  公网:
  设置页 ──POST /api/cloud/apply──> portal ──(批准)──> /api/cloud/state → connect()
    ├── ssl_bootstrap.sign_for("<呼号>.mrrc.vlsc.net") ──> certs/
    ├── POST /enroll（上交公钥半）
    ├── 写 <user_dir>/fleet/frpc-<label>.toml（ASCII 无 BOM）
    ├── 写 env: MRRC_SSL_CERT / MRRC_SSL_KEY / MRRC_WEB_PORT / MRRC_REMOTE_SESSION_TX_HEARTBEAT_S=5
    └── 启 frpc 子进程（fleet_dir/frpc）
```

## 6. 关键设计决策

### D-1：`mrrc-radio` 不做串口 I/O（约束 `cat-direct-serial-io` [block]）

`brief` 对 `linux/mrrc_radio.py` 报了 block 级约束：**所有电台串口 I/O 必须经 CatController/CivController**（AD-002）。

因此 `mrrc-radio` 自身**不打开任何串口**：

- 给了 `--port` → 直接写配置，零探测
- 未给 `--port` → **调用 `linux/first_run.py` 既有函数**（既有代码，不新增裸串口访问）

### D-2：profile 只写差异，不重复默认值

三条刻意的"不写"：

1. **不写 baud** —— `config._DEFAULT_BAUD_BY_MODEL` 已按注册表驱动（FT-710 与 Yaesu 系 38400；Icom 系 115200）
2. **不写串口** —— 由探测或操作者提供
3. **不写音频设备名** —— `audio_handler` 已有名字正则 + 半双工启发式自动探测

profile 语法（示例）：

```ini
# ft710.env —— 唯一需要 FTDI 库的型号，而库在默认搜索路径里（见 D-9），
# 所以它和其余型号一样只写型号本身。
MRRC_RADIO_MODEL=ft710

# ic7300.env
MRRC_RADIO_MODEL=ic7300

# ftdx10.env —— 未验证型号，TX 门禁显式关闭
MRRC_RADIO_MODEL=ftdx10
MRRC_ALLOW_UNVERIFIED_TX=0
```

> `IC7300_CIV_ADDR` / `IC7300MK2_CIV_ADDR` 也**不写**：它们的默认值 (0x94 / 0xB6) 就是对的值，只有操作者改过电台地址时才需要覆盖。

### D-3：未验证型号的 TX 门禁（AD-019 / NFR-067）

- 8 个未验证型号的 profile **必须显式写 `MRRC_ALLOW_UNVERIFIED_TX=0`**
- **任何 profile 都不得出现 `=1`**（由单测强制）
- `mrrc-radio use` 对未验证型号**打印警告但不自动开启**门禁
- 文档/UI 不得把这些型号呈现为已验证

### D-4：PTT 双防线（SDD §12.9）

`connect()` 自动写 `MRRC_REMOTE_SESSION_TX_HEARTBEAT_S=5`（防线①）。防线② `MRRC_PTT_MAX_TX_SECONDS` 默认 `0.0` = 关闭，**必须显式打开**。构建期写入 `MRRC_PTT_MAX_TX_SECONDS=120`（仅当键不存在时）。

### D-5：frpc 必须来自 ophub 内核 + 版本钉死

- **版本钉死 `0.71.0`**：客户端比服务端新可能握手失败
- 取法：frp 官方 release `frp_0.71.0_linux_arm64.tar.gz`，对照官方 `frp_sha256_checksums.txt` 校验（不硬编码哈希，避免与我们无法控制的上游漂移）
- 取法与校验都是**确定性**的：URL 里含版本号，checksums 文件同样按版本钉 URL，因此结果可复现
- 落点：`fleet_dir`（`server.py:_cloud_fleet_dir()` 依次找 `<runtime_dir>/fleet`、`<resource_dir>/payload`、`<resource_dir>/fleet`）
- **缺失的后果是最隐蔽的坑**：`connect()` 返回 `tunnel_started: false` —— **UI 显示"已连接"，但公网入口 502**

### D-6：内核变体保护

W103D 的 WiFi 完全依赖专属内核补丁 + 固件 overlay（PR #3658/#3659）。模型库把其内核 tag 锁在 `stable/6.18.y`。

**规则**：升级走 ophub 自己的 `armbian-update`（按 BOARD 选对内核）；**禁止**在 chroot 内 `apt upgrade` 内核，**禁止**手工替换成通用内核——换了会静默丢 WiFi。

### D-7：更新路径（最小实现，YAGNI）

镜像刷一次，但 MRRC 会升级。`upgrade_core.py` 只有 slice 1（只读检查），自动升级 slice 2 未开工。

`linux/mrrc_update.sh` 只做三件事：拉代码（`git pull` 或 `rsync` 指定源）→ `pip install -r requirements.txt` → `systemctl restart mrrc-modern`。**不做自动回滚**。

### D-8：不预置串口/口令

盲猜串口会在首启产生一个"看起来能用但连不上电台"的配置。首启探测是唯一可靠来源。

### D-9：FTDI 库构建期预置——可做到开箱真 FFT

FT-710 的真 FFT 需要 FTDI 库。代码要求 `find_ftdi_libraries()` 返回**一对**路径，而 `scope_pipe.py:213-214` 会分别 `CDLL()` 两个路径，并调用 `d2xx.FT_OpenEx` / `FT_Close` / `FT_SetTimeouts` / `FT_SetLatencyTimer` 与 `f4.FT4222_*`。

**关键事实（实测）**：FTDI 的 Linux LibFT4222 包**静态链入 libftd2xx 并把它的符号全部导出**——

- aarch64 的 `libft4222.so.1.4.4.232` 导出 **81 个 `FT_*` 符号**，`scope_pipe` 需要的 4 个（`FT_OpenEx` `FT_Close` `FT_SetTimeouts` `FT_SetLatencyTimer`）全部在内
- 其 ELF `DT_NEEDED` 里**没有** `libftd2xx`，SONAME 为 `libft4222.so`
- 包内 `ReadMe.txt` 明写 “The Linux version of libft4222 **includes (statically links to)** FTDI's libftd2xx”，且 `install4222.sh` 只安装 `libft4222.so`
- 包内**不含任何 `libftd2xx.so`**（只有 `ftd2xx.h` 头文件）

**因此一个 ELF 可以同时充当两个角色**。两种落法：

| | 做法 | 评价 |
| --- | --- | --- |
| **A（采用）** | `vendor/ftdi/` 下放真身 `libft4222.so` + 符号链接 `libftd2xx.so` → 同一 ELF | **零 env**：命中 `get_candidate_library_dirs()` 的默认目（4）；profile 因此只需写型号（与 D-2 一致） |
| B（备选） | 同一文件，用 `MRRC_FT4222_LIB` 与 `MRRC_FTD2XX_LIB` 两个显式变量指向它 | 走第一优先级分支，更确定；但把部署细节写进了 profile，且手动 `python server.py` 时会丢 |

**决定（A）**：构建期把本机已有的 aarch64 构建预置进镜像的 `vendor/ftdi/`（来源：FTDI `libft4222-linux-1.4.4.232` 的 `build-arm-v8/`；ELF 已确认 `e_machine=183` = AArch64），并建一个同名指向的 `libftd2xx.so` 符号链接以满足代码的"成对"要求。**无人工步骤，开箱真 FFT。**

操作者若把库搬到别处，仍然可以用既有的 `MRRC_FTDI_LIB_DIR` 这个口（B 的记录保留在此）。

**与既有先例一致**：`vendor/ftdi/macos/` 与 `vendor/ftdi/windows/` 已在库内。

**USB 权限不由这条解决**：FT4222H 的裸 USB 访问靠 D-10 的 udev 规则。

> 只影响 FT-710；其余 10 个型号不需要 FTDI（与 D-2 一致）。

### D-10：udev 规则复用 `install.sh` 既有能力，不重复实现

`install.sh:699-724` 已经装 `/etc/udev/rules.d/99-ft710.rules`：

```
SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", MODE="0666", SYMLINK+="ft710-cat"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="601c", MODE="0666"
```

- `0403:601c`（FT4222H）`MODE="0666"` → **解决 D-9 那条路的裸 USB 访问**，服务以 `mrrc` 用户运行也能开设备
- `10c4:ea60`（CP210x）`MODE="0666"` + `SYMLINK+="ft710-cat"` → 串口免 sudo

**chroot 内的两个已知行为**（不阻塞，但要知道）：`sudo` 镜像里已装（§2.3），所以 `sudo tee` 可用；`udevadm control/trigger` 在 chroot 里无效，但脚本用 `2>/dev/null || true` 兜住，规则文件照样落进镜像、在真机首次插拔时生效。

**一个既有局限（记录，不改）**：FT-710 的两个 CP210x **同 VID:PID**，所以 `SYMLINK+="ft710-cat"` 由枚举顺序决定归属，有歧义。这不是本设计的缺陷，也不影响正确性——`linux/first_run.py` 的 `probe_radio_model()` 才是 CAT 口的权威判据（发包探测），`mrrc-radio` 复用它（D-1）。

### D-11：`MRRC_CONFIG_FILE` 必须与 systemd 的 `EnvironmentFile` 指向**同一个文件**

这是本次研究里最隐蔽的一个陷阱，**不处理会让里程碑 3 静默失败**。

**事实（实测）**：

- Pi 镜像的单元读 `EnvironmentFile=/opt/mrrc_modern/env/mrrc.env`
- 但 `cloud_hub.connect()` 写的是 `server.py:_config_file_path()`——它先看 `MRRC_CONFIG_FILE` 环境变量，没设就回退到 `MEM_FILE.parent / "mrrc_modern.env"`
- `packaging/` 里**没有任何地方设 `MRRC_CONFIG_FILE`**

**后果**：在镜像（WorkingDirectory=`/opt/mrrc_modern`、未设 `MRRC_MEM_FILE`）上，`_config_file_path()` 会解成 `/opt/mrrc_modern/mrrc_modern.env`，而 systemd 读的是 `/opt/mrrc_modern/env/mrrc.env`。`connect()` 写下的 `MRRC_SSL_CERT` / `MRRC_SSL_KEY` / `MRRC_REMOTE_SESSION_TX_HEARTBEAT_S` **服务端永远读不到**——公网入口证书校验失败、PTT 活性闸门不生效，而 UI 上显示"已连接"。

这也解释了为什么 Cloud Hub 至今只在 Windows/macOS 实例上跑：那两边的启动器会自己设 `MRRC_CONFIG_FILE`。

**决定**：box 镜像的 systemd 单元同时钉死两者——

```ini
EnvironmentFile=/opt/mrrc_modern/env/mrrc.env
Environment=MRRC_CONFIG_FILE=/opt/mrrc_modern/env/mrrc.env
```

并在 `verify.sh` 里加一项断言（两个值相等），让它以后不能静默漂走。

## 7. 错误处理

| 位置 | 策略 |
| --- | --- |
| `build-image.sh` | 每步 fail-fast，打印原因 + 修复命令，非 0 退出；**绝不"部分成功"后静默继续** |
| rootfs 空间断言 | 铺 overlay 前断言可用空间 ≥ 400 MiB；不足则中止并给出"精简内容或扩容"两条建议 |
| `box-overlay.sh` | chroot 内任一步失败立即退出（`set -e`），并打印是哪一步 |
| `fetch-frpc.sh` | 校验和不符 → **拒绝使用并退出**（与 hub 侧 `install_instance_tunnel.sh` 同策略） |
| `mrrc-radio` | 非法型号用 `known_models()` 校验后拒绝；profile 缺失报错；写 env 前备份 `.env.bak`，restart 失败时打印恢复命令 |
| `verify.sh` | 每项独立判定、失败给排查命令，最后统一汇总（一项失败不中断其余） |
| Cloud Hub | `tunnel_started: false` → `verify.sh` 必须报 **FAIL**（唯一能自动发现 frpc 缺失的地方） |

## 8. 测试（无硬件）

| 文件 | 断言 |
| --- | --- |
| `tests/test_box_profiles.py` | ① profile 的 `MRRC_RADIO_MODEL` 取值集合与 `known_models()` **完全相等**（多一个少一个都 FAIL）② 每份只含白名单键 ③ **任何 profile 不得出现 `MRRC_ALLOW_UNVERIFIED_TX=1`** ④ 8 个未验证型号必须显式写 `=0` ⑤ `packaging/box/box-overlay.sh` 里的 rsync 排除清单与 `packaging/rpi/build-image.sh:38-45` **逐字一致**（防止两处漂移） |
| `tests/test_mrrc_radio.py` | 注入临时 env 文件 + fake `systemctl`：`list` 标注验证状态、`show` 只读、`use` 切换后键正确、备份存在、未验证型号不被放行、失败不破坏原 env |
| 门禁 | `python -m unittest discover -s tests`（1369 基线不回归）+ `sdd_context.py check --staged` clean |

## 9. 验收（真机 10 项）

```text
【能跑】
 1. ip a                             有线网口 up 且有 IP
 2. ls /dev/ttyUSB* /dev/ttyACM*     串口枚举（FT-710: 2×CP210x；IC-7300: ttyACM0）
 3. arecord -l; aplay -l             电台 USB 声卡成 ALSA card
 4. python3 --version                3.11
 5. systemctl status mrrc-modern     active (running)，日志无报错
 6. https://<盒子IP>:8888             能登录、状态正常、频谱在动
【多电台】
 7. mrrc-radio list / use             至少 FT-710 + IC-7300 两型号切换后各自连通
 8. FT-710 真 FFT                     放了 FTDI 库后瀑布为真 FFT；未放则回落 S 表
【公网】
 9. https://<呼号>.mrrc.vlsc.net/     公网可打开、可听、可发；tunnel_started=true
10. 断线释放                          关闭浏览器 → PTT 在心跳超时后释放
【性能】dev_tools/bench_mrrc.py        输出真实 % of one core
```

## 10. 风险与缓解

| # | 风险 | 缓解 |
| --- | --- | --- |
| R1 | **Linux 上 stale frpc 无自动清理**——`_enumerate_frpc`/`_kill_stale_frpc` 是 Windows-only（PowerShell + `taskkill`）。代码注释写明后果：下次启动无法注册同名 proxy，隧道保持 down 而 UI 无提示 | **必须用 systemd 管**（默认 `KillMode=control-group`，停/重启单元时清掉整个 cgroup，连带 frpc）。runbook 明写"只用 `systemctl` 停服务，别 `kill` 进程" |
| R2 | 这是**第一个跑 Cloud Hub 的 Linux 实例**（Pi 镜像完全不处理 Cloud Hub，`grep` 为空），Linux 路径未被验证 | 计划含"验证 Linux 路径"任务；`verify.sh` 第 9 项覆盖 |
| R3 | 镜像二次构建破坏引导链或首启自动扩容 | 只改 p2 内容，绝不写 p1 或镜像前 4 MiB；不预先扩容；构建后校验 MBR + 分区表未变 |
| R4 | rootfs 空间不足（已实测 963.7 MiB 可用 vs 约需 310 MiB） | R4 已降级为低风险；仍保留构建期断言（§7） |
| R5 | Cloud Hub 登录限流退化（SDD §12.9）：经隧道时所有登录共享一个来源 IP，`5 次失败/300 秒`是**全局桶**，误锁影响**全部用户** | 写进 runbook 的运维警告；口令从一开始就用强口令降低触发概率 |
| R6 | 依赖第三方预构建镜像（供应链） | 钉 SHA-256 + 只从官方 release 取 + 记录"这是唯一让 WiFi 工作的上游" |

## 11. 不在范围内

- `upgrade_core.py` slice 2（自动升级、自证、回滚）
- 多实例/多盒子编排
- 未验证型号的硬件验证（8 个型号的 `verified` 翻转需真机证据，属独立工作）
- 发布注册表（`release-artifacts.json`）与网站下载卡：等到真要发布 `MRRC-Modern-<ver>-w103d.img.gz` 时按 `dual-platform-release` 技能处理
