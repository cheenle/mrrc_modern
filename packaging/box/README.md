# W103D 电台盒子 — 操作单

> **第一次上手看完整指南：`docs/W103D_GUIDE.md`**（从开箱认机器到 MRRC 干活的逐步走查，含清单）。
> 本页是熟悉流程后的速查单。

一台 ZTE 云电脑 W103D（Amlogic S905L3A）跑 MRRC Modern 服务端的完整流程：
烧镜像 → 首启 → 接电台 → 公网接入 → 验收。

设计文档：`docs/superpowers/specs/2026-10-06-w103d-radio-box-design.md`
构建手册（要出镜像才看）：`docs/w103d_pack.md`

---

## 1. 你需要什么

- **W103D**（Amlogic S905L3A / G12A）。认准芯片，别拿国科 GK6323 或 RK3566 的盒子。
- **≥8 GB U 盘**（先走 U 盘路线；Android 的分区与数据不动，只写一次 u-boot 环境——见指南 §5）
- **网线**（这台盒子默认走有线：W103D 的 WiFi 是 MT7663S，**只连 2.4 GHz**——5 GHz 能关联但数据面不通
  还会把芯片拖到 MCU 超时；有线对实时音频也更稳）
- **电台 USB 线**（FT-710 用**上方 Enhanced 口**）
- **原厂线刷固件包**（兜底；走 U 盘路线时用不到）
- 镜像：`MRRC-Modern-<ver>-w103d.img.gz`（由 `packaging/box/build-image.sh` 产出）

## 2. 烧到 U 盘

```bash
# 找盘符（认准容量，别烧错盘）
diskutil list

gzip -dc MRRC-Modern-<ver>-w103d.img.gz | sudo dd of=/dev/rdiskN bs=4m
sync
```

`diskutil unmountDisk /dev/diskN` 之后再拔。

**烧完把根分区扩到整盘**（镜像自带 `/root/.no_rootfs_resize`，根分区只有 2.9 GB 且**不会**自动长；
`/ddbr` 那份 2.3 GB 的 eMMC 备份就是靠这一步才有地方放）。ext4 支持在线扩容，在**盒子上**跑：

```bash
apt-get install -y cloud-guest-utils      # growpart 不在镜像里
growpart /dev/sda 2 && resize2fs /dev/sda2
df -h /                                   # 2.9G → 整盘
```

（在 Linux 电脑上离线跑同一组命令也行；macOS 没有 ext4 工具，只能在盒子上做。）

## 3. 首次启动（U 盘路线，零风险）

1. U 盘插盒子，**网线插上**。
2. 盒子通电，**立刻**在安卓里执行 `reboot update`（用遥控器的终端应用，或 adb）。
   这个动作让固件从 U 盘启动；**Android 的分区与数据完全没动**（`aml_autoscript` 里的 `saveenv`
   会写一次 u-boot 启动顺序，不碰数据），拔掉 U 盘就回安卓。详见 `docs/W103D_GUIDE.md` §5。
3. 第一次开机要几分钟：生成 SSH 密钥，首启当场生成 Web 口令与自签证书。（**根分区不会自动扩容**，
   见第 2 节。）
4. 登录路由器查盒子的 IP（DHCP 拿的）。

> 失败回滚：断电、拔 U 盘、重新上电 = 回到原厂安卓。

## 4. 拿 Web 口令

```bash
ssh root@<盒子IP>        # 首次口令 1234（ophub Armbian 的出厂值）
                          # ⚠️ 第一件事就改掉它：passwd root
mrrc-show-password       # 打印 Web 登录口令
```

**口令一览**（完整表在指南 §7）：SSH root 出厂 `1234`（进去**第一件事** `passwd root`）；
Web 口令首启生成、每台不同（`mrrc-show-password`，或首启 HDMI 横幅）；收听口令空＝未启用；
Wi-Fi 没有出厂值（预置或页面里现设）。

**接上网络之后就不用回控制台了**：浏览器 `https://<盒子IP>:8888/settings` 里能改盒子名字/时区、
扫 Wi-Fi 并切换、改有线口 DHCP/静态、重启，以及**填呼号申请公网入口**（见下）。（`/setup` 只在从未
设过口令时免密开放，设过就 302 跳到 `/settings`。）**Wi-Fi 面板可以直接手输 SSID**（隐藏网络也有出路），
密码框可切显示/隐藏，但**已保存的密码不会回显**；**只连 2.4 GHz**。

```bash

首启时 HDMI 控制台的横幅里也印了口令（`linux/first_run.py` 打的）。
根文件是 `0640 root:root`，所以这个命令必须用 root 跑。

## 4.5 发货版（`--preflash`）：代刷一批、快递给用户

用户开箱插电就要能用，所以另一边是**预置过的镜像**：

```bash
bash packaging/box/build-image.sh --preflash     # 固定口令 + 时区/区域烤进镜像
```

三条固定口令（可用 `MRRC_PREFLASH_*` 环境变量按批次覆盖）：

| 用途 | 初始值 | 怎么改 |
| --- | --- | --- |
| SSH `root` | `abcd1234` | `passwd root` |
| 系统用户 `mrrc` | `abcd!1234` | `passwd mrrc` |
| Web 登录 | `abcd!1234` | `/settings` 的「登录口令」 |

外加 `Asia/Shanghai` + `zh_CN.UTF-8`。**不传 `--preflash` 时一字不改**（口令照旧每台现生成）。
**救急 Wi-Fi**（`MRRC_PREFLASH_RESCUE_SSID` / `MRRC_PREFLASH_RESCUE_PSK`，默认 `rescue` / `abcd1234`）：
镜像里预先存了一条 **STA** profile（`autoconnect=true`、优先级 `-10`、`hidden`、2.4 GHz、无限重试）。
什么网都不通的时候，让客户用手机开一个同名的热点，盒子自己连上去 —— 人就能在手机热点网段里找到它。
**方向必须是这个方向**：这块 MT7663S 做不了 AP（2026-10-11 真机实测：能开、信标发不出去），
所以别去找"盒子开热点"的做法。

**固定口令就是公开知识** —— 拿到镜像或任何一台同批盒子的人能进同批所有盒子。

发货流程（五步）：

1. 出镜像：`bash packaging/box/build-image.sh --preflash`
2. 烧 U 盘（§2）
3. 装进 eMMC（§10：U 盘启动 → `armbian-install` 答 `307` / `1`）
4. `poweroff` → 拔盘 → **重新上电**（跑的就是要发货的那套系统；这一步也顺带让 Cloud Hub 写下的证书路径生效）
5. **在工作台上把公网入口做掉**：插 LAN 线 → 浏览器开 `/settings` 用 `abcd!1234` 登录 →
   填**客户的呼号** → 点「申请公网入口」→ 你在 portal 控制台批准 → 面板显示入口地址

关机发货。客户拿到手：公网入口已就绪；Wi-Fi 到用户家自己从 `/settings` 改（**只能用 2.4 GHz**）。

> 不建议用预置文件（`/boot/mrrc-wifi.txt`，§9.3 指南）走发货路径：eMMC 路线下用户碰不到那个 FAT 分区，
> 而且预置文件会被 U 盘系统自己消费掉。能力仍在，流程不用它。

### 本批产物（2026-10-17 真构建 + 产物四步验证）

| | 字节数 | SHA-256 |
| --- | --- | --- |
| **发货版**（`--preflash`） | 1,212,223,182 | `17f3fe797a3d6a8dabaae0d5612dfcff2305aa3f9397c05c258bfac3c744adc5` |
| 标准版（不带开关，对照） | 1,191,513,586 | `8f743cf30a214ed20cdc5f8b14b6f8f023c98dd37ca87be75867daf288268a0b` |

两份都过了四步验证：代码身份逐字节一致（`server.py` / `linux/first_run.py` / `firstboot_wrapper.py` /
`static/settings.html`）、11 份 profile、`/tmp` 零残留、D-11 的单元身份（`EnvironmentFile` ==
`Environment=MRRC_CONFIG_FILE`）、chroot 真跑入口导入（含 `libopus`）。本版新增的断言也全绿：
发货版写入了固定口令与 `Asia/Shanghai` + `zh_CN.UTF-8`（用 `crypt` 复算 root/mrrc 口令为 `True`）、
**不预置型号与串口**、NTP 单元随基底（chrony）保持 enable；对照版全部相反（时区 `Etc/UTC`、
locale `en_US`、shadow 不符）。两版都在产物里真跑了一次首启包装器：**没插电台 ⇒ 不盖章**
（下次开机再探），但自签证书照生成、网页照起。

⚠️ **产物留在 `dist/` 就是官网那份**（2026-10-18 起）：发布一致性检查会把 `dist/w103d/` 里的镜像
与下载卡逐字段比对，所以两边要么一致、要么就红。

**发货版（`--preflash`）现在就是对外下载版。** 决策来自操作员 2026-10-18：老的非预置镜像要先过
一遍首启引导（生成口令、探测串口/声卡、`/setup`），对大多数用户「太复杂」；预置版开机即可登录，
代价是**下载页上的每一份都是同一组公开口令**（Web `abcd!1234`、`root` `abcd1234`、`mrrc` `abcd1234`）。
因此两件事不能省：

1. 下载卡上写明这三条口令，并指出 **第一件事是改掉它们**（`docs/W103D_USER_GUIDE.md` §2 有步骤）；
2. 这份镜像默认 `sshd` 允许 root 口令登录 —— 同一个局域网里的人拿这份公开口令就能上机。
   客户只在受信任的网络里开机，或者先改口令再开机。

⚠️ 本机没有真机验收：指南 §9.8 的 A1–A8（浏览器登录 / 三条口令 / `/setup` 302 / 手输 SSID /
呼号申请与批准 / 重探 / `timedatectl`）仍须在盒子上跑一遍，读数回填指南与 SDD。

## 5. 接电台

```bash
mrrc-radio list                      # 11 个型号，带 ✅已验证 / ⚠实验性
mrrc-radio use ft710                 # 自动探测串口后切换并重启服务
mrrc-radio use ic7300 --port /dev/ttyACM0    # 或者你直接指定
mrrc-radio show                      # 当前生效的配置
```

`use` 会：停服务 → 探测串口（发包问电台）→ 写配置 → 起服务。
换型号时**旧的串口会被清掉**（FT-710 是 ttyUSB0/1，IC-7300 是 ttyACM0，留着旧的等于死链路）。

浏览器打开 **`https://<盒子IP>:8888`**（自签证书会警告一次 → 高级 → 继续）。
手机要能用麦克风发射，**必须走 HTTPS**，别关。

> FT-710 的真 FFT 库已经预置在镜像里（`vendor/ftdi/`），不用手动放。

## 6. 换电台

```bash
mrrc-radio use <model>
```

**8 个实验性机型默认只收不发**：`ic705` `ic7610` `ic7760` `ftdx10` `ftdx101d` `ftdx101mp` `ftx1` `ft891`。

原因（AD-019）：这些机型的表来自 Hamlib 与手册，不是真机测量——在没验证过的频率/功率语义上发射，对电台和天线都不利。
要发射就得显式开：

```bash
vi /opt/mrrc_modern/env/mrrc.env     # 加 MRRC_ALLOW_UNVERIFIED_TX=1
systemctl restart mrrc-modern
```

**换回已验证机型时该键会自动重置为 0**，不会带着开着。

## 7. Cloud Hub 公网接入

浏览器 → 设置 → Cloud Hub：

1. **apply**：填呼号 + 联系方式，提交申请。
2. 等批准（门户是人工/自动审的）。
3. **connect**：自动完成——签证书、enroll、写隧道配置、起 frpc，并写进配置：
   - `MRRC_SSL_CERT` / `MRRC_SSL_KEY`（这个机型的入口证书）
   - `MRRC_REMOTE_SESSION_TX_HEARTBEAT_S=5`（PTT 活性闸门，防线①）
4. 之后用 **`https://<呼号>.mrrc.vlsc.net/`** 访问（例：`https://bg1sb.mrrc.vlsc.net/`）。

防线②（时长上限）镜像里已经写好 `MRRC_PTT_MAX_TX_SECONDS=120`。
两条防线互相独立，都要有。

## 8. 验收

```bash
scp packaging/box/verify.sh root@<盒子IP>:/tmp/
ssh root@<盒子IP> 'bash /tmp/verify.sh'
```

12 项，每项失败都给出排查命令。**先跑这个再动手调别的**。

第 11 项查的是**盒子身份与提权通道**：NetworkManager 的 polkit 规则
（`/etc/polkit-1/rules.d/49-mrrc-nm.rules`）在不在、`avahi-daemon` 活着没
（`<主机名>.local` 能否解析）、以及 root 助手 socket `/run/mrrc-box/box-helper.sock`
在不在。这套机器是新引导路径（LAN 网页）能用的前提，脚本查它而不查已退役的热点。
引导流程本身见操作单 `docs/W103D_GUIDE.md`，脚本替不了它。

## 9. 升级

```bash
MRRC_UPDATE_SRC=<git url 或本地目录> mrrc-update
```

只做三件事：拉代码 → 装 `requirements.txt` → 重启。**没有回滚**（自动升级切片明确未开工，不在这里发明第二套状态机）。
配置env文件**不会被碰**。

## 10. 写进 eMMC（推荐的最终形态）

U 盘路线跑通、验收过了就做这一步 —— 之后盒子不再依赖 U 盘（接线只剩电源 + 网线）。
**在跑着的盒子上就地做**（ophub 自带安装器，不需要线刷工具）：

```bash
armbian-ddbr        # 交互选 b：全盘（含原厂引导）备份到 /ddbr/，留在 U 盘上别删
armbian-install     # 交互：设备 ID 填 307（ZTE-W103D），根文件系统填 1（ext4）；不要加 -m yes
poweroff            # 然后拔 U 盘、重新上电 —— 从 eMMC 启动
```

- `armbian-ddbr` 单线程 gzip 约 40 分钟；先 `apt-get install -y pigz` 并用
  `dd if=/dev/mmcblk2 bs=4M \| pigz -c -p 3 > /ddbr/BACKUP-arm-64-emmc.img.gz` 手跑可压到 ~9 分钟
  （标准 gzip 流，`armbian-ddbr` 的恢复路径不变；跑完 `pigz -t` 自检）。
- 安装器自动：备份并回写原厂引导（`[0,444)` 与 `[512,4 MiB)` 逐字节保留）→ ampart 重建分区
  （前 117 MiB 保留）→ 拷 boot/rootfs → `Installation completed successfully`。
- 起起来核对：`findmnt /` = `/dev/mmcblk2p2`、`df -h /` ≈ 28 GB、服务在跑、`verify.sh` 过关。
- 副作用：machine-id 重生成 → **IPv6 IID 变化**（域名/隧道几分钟内自愈）；IPv4 与配置不受影响。
- **最终形态**：盒子 + 一根电源线 + 一根网线 —— 不再需要 U 盘；那根 U 盘留着当救砖盘（整盘备份就在里面）。
- ⚠️ **重刷镜像会把这根盘上的 `/ddbr` 整盘备份一起冲掉**（`dd` 写整盘），而那份备份往往是全机唯一一份。
  **重刷前先把它拷到别处**；根分区没扩容时（只有 2.9 G）它**装不下**这 2.3 G 的备份。

完整步骤、判据与三档回滚见 `docs/W103D_GUIDE.md` §17–§18（2026-10-11 真机实操记录）。

> ⚠️ 这一步会覆盖 eMMC。变砖兜底是 Amlogic 的 MaskROM（在 SoC BootROM 里，短接就能重进刷写模式）+
> 你手上的原厂线刷包。

## 11. FT-710 真 FFT

镜像里已预置 aarch64 的 `libft4222.so` + `libftd2xx.so`（后者是指向同一个 ELF 的符号链接，见 `vendor-ftdi.md`）。

如果瀑布图是合成的（S 表高斯谱）而不是真频谱：

```bash
systemctl status mrrc-modern
journalctl -u mrrc-modern | grep -iE 'ft4222|scope'
ls -l /opt/mrrc_modern/vendor/ftdi/
cat /sys/bus/usb/devices/*/idProduct 2>/dev/null | grep -n 601c   # FT4222H 在不在
```

规则：**没有这两个库时只有 S 表合成谱**，CAT/音频/PTT 都不受影响。

## 12. ⚠️ 三条运维红线

**① 只用 `systemctl` 停服务，不要 `kill` 进程。**

Linux 上 frpc 的残留清理是 Windows-only（`cloud_hub.py` 里那套走 PowerShell + `taskkill`）。
一个残留的 frpc 会占住 proxy 名，导致下次启动**隧道起不来而 UI 毫无提示**。
systemd 默认的 `KillMode=control-group` 会连带清掉 frpc，所以按规矩停就没事。

**② 登录限流是全局桶。**

经隧道访问时所有登录共享同一个来源 IP，限流是 `5 次失败 / 300 秒`，而且是**全局**的——
**误锁影响全部用户**，不是锁你一个人。

**③ 内核升级只走 `armbian-update`，保持 w103d 变体。**

W103D 的 WiFi 完全依赖专属内核补丁 + 固件 overlay（MT7663S）。
换成通用内核 = **静默丢 WiFi**（没有报错，只是连不上）。

## 13. 排障

| 症状 | 检查 | 修 |
| --- | --- | --- |
| 打不开网页 | `ip a`；`systemctl status mrrc-modern` | 网线/服务；`journalctl -u mrrc-modern -n 50` |
| 连上了但没网口地址 | 路由器 DHCP | 换网线口；`nmcli device status` |
| 扫描列表是空的 / 页面列不出 Wi-Fi | `dmesg \| grep -c "mailbox write"` | 计数 > 0 = 无线芯片已楔死（多半连过 5 GHz）→ **重启盒子**（钉版固件下软重启 47 秒即复位；老 2020 固件需断电） |
| Wi-Fi 连上但没网/没 IPv6 | `ip route`、`ping <网关>` | **别用 5 GHz**：能关联能拿地址但数据面不通；只连 2.4 GHz |
| IPv6 直连打不开网页 | `ss -ltn \| grep 8888` | 盒子线的网页只绑 `0.0.0.0`（IPv4）；要听 IPv6 就把 `MRRC_WEB_HOST` 改成 `::` 再重启服务 |
| 公网 AAAA 记录不对 | `dig +short AAAA w103d.vlsc.net @223.5.5.5` vs 盒子实际 IPv6 | 镜像里**没有** DDNS 组件（`/opt/w103d-ddns`）；重刷后 machine-id 变 → IID 变，记录就陈旧了 |
| 没电台串口 | `ls /dev/ttyUSB* /dev/ttyACM*` | 电台 USB 线；FT-710 要插**上方 Enhanced 口**；电台菜单 `MOD SOURCE = USB` |
| 有串口但连不上 | `mrrc-radio show` 里的串口对不对 | `mrrc-radio use <model> --port /dev/ttyXXX` |
| 没声音 | `arecord -l` 有没有电台声卡 | 电台 USB 声卡没枚举 → 换线/换口；服务日志里找 PyAudio 设备列表 |
| 声音卡顿、听一会儿彻底安静 | `cat /proc/asound/card1/stream0` 的 Capture 是不是 `Status: Stop`；`journalctl -u mrrc-modern \| grep "capture stalled"` | 采集缓冲溢出后 ALSA **静默停摆**（PyAudio 不报错）—— 服务会自己重开；反复出现就把 `MRRC_RX_BUFFER_MS` 调大（镜像默认 100） |
| 瀑布是合成谱 | 见第 11 节 | FT-710：查 FTDI 库与 `0403:601c`，**再查 `/etc/udev/rules.d/99-mrrc-ftdi.rules`**（服务用户 `mrrc` 必须能写那个 USB 节点：`sudo -u mrrc test -w /dev/bus/usb/001/00N`） |
| **Cloud Hub 显示"已连接"但公网 502** | `verify.sh` 第 8、9 项 | 多半是 `fleet/frpc` 缺失（`connect()` 会返回 `tunnel_started:false`） |
| 公网证书报错 | `verify.sh` 第 7 项 | `EnvironmentFile` 与 `MRRC_CONFIG_FILE` 必须指向**同一个文件**（设计 D-11） |
| 服务反复重启 | `journalctl -u mrrc-modern -n 100` | 看 Python traceback；`mrrc-radio show` 核对型号与串口 |
| 发射没功率 | 状态行的 `TX[... pk:...]` | 采集源/音量；FT-710 的 `mic_gain`；SSB 无话音即无功率 |
| 延迟越来越大 | 状态行 `J:` | 正常在 100–300 ms；长期 >500 说明抖动缓冲在追，查网络 |
