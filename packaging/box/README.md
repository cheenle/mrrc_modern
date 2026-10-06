# W103D 电台盒子 — 操作单

一台 ZTE 云电脑 W103D（Amlogic S905L3A）跑 MRRC Modern 服务端的完整流程：
烧镜像 → 首启 → 接电台 → 公网接入 → 验收。

设计文档：`docs/superpowers/specs/2026-10-06-w103d-radio-box-design.md`
构建手册（要出镜像才看）：`docs/w103d_pack.md`

---

## 1. 你需要什么

- **W103D**（Amlogic S905L3A / G12A）。认准芯片，别拿国科 GK6323 或 RK3566 的盒子。
- **≥8 GB U 盘**（先走 U 盘路线，eMMC 一个字节不动）
- **网线**（这台盒子默认走有线：W103D 的 WiFi 是 MT7663S，能用，但有线对实时音频更稳）
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

## 3. 首次启动（U 盘路线，零风险）

1. U 盘插盒子，**网线插上**。
2. 盒子通电，**立刻**在安卓里执行 `reboot update`（用遥控器的终端应用，或 adb）。
   这个动作让固件从 U 盘启动；**eMMC 完全没动**，拔掉 U 盘就回安卓。
3. 第一次开机要几分钟：扩容 rootfs、生成 SSH 密钥。
4. 登录路由器查盒子的 IP（DHCP 拿的）。

> 失败回滚：断电、拔 U 盘、重新上电 = 回到原厂安卓，没有任何副作用。

## 4. 拿 Web 口令

```bash
ssh root@<盒子IP>        # 首次口令 1234（ophub Armbian 的出厂值）
                          # ⚠️ 第一件事就改掉它：passwd root
mrrc-show-password       # 打印 Web 登录口令
```

首启时 HDMI 控制台的横幅里也印了口令（`linux/first_run.py` 打的）。
根文件是 `0640 root:root`，所以这个命令必须用 root 跑。

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
4. 之后用 **`https://<呼号>.mrrc.vlsc.net/`** 访问。

防线②（时长上限）镜像里已经写好 `MRRC_PTT_MAX_TX_SECONDS=120`。
两条防线互相独立，都要有。

## 8. 验收

```bash
scp packaging/box/verify.sh root@<盒子IP>:/tmp/
ssh root@<盒子IP> 'bash /tmp/verify.sh'
```

10 项，每项失败都给出排查命令。**先跑这个再动手调别的**。

## 9. 升级

```bash
MRRC_UPDATE_SRC=<git url 或本地目录> mrrc-update
```

只做三件事：拉代码 → 装 `requirements.txt` → 重启。**没有回滚**（自动升级切片明确未开工，不在这里发明第二套状态机）。
配置env文件**不会被碰**。

## 10. 写进 eMMC（可选，验收通过之后再做）

U 盘路线跑通、验收过了，再考虑固化：

```bash
armbian-ddbr        # 备份原厂安卓整盘（先做这个！）
armbian-install     # 把 Armbian 写进 eMMC
```

之后盒子不再依赖 U 盘，32 GB eMMC 全用上。

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
|---|---|---|
| 打不开网页 | `ip a`；`systemctl status mrrc-modern` | 网线/服务；`journalctl -u mrrc-modern -n 50` |
| 连上了但没网口地址 | 路由器 DHCP | 换网线口；`nmcli device status` |
| 没电台串口 | `ls /dev/ttyUSB* /dev/ttyACM*` | 电台 USB 线；FT-710 要插**上方 Enhanced 口**；电台菜单 `MOD SOURCE = USB` |
| 有串口但连不上 | `mrrc-radio show` 里的串口对不对 | `mrrc-radio use <model> --port /dev/ttyXXX` |
| 没声音 | `arecord -l` 有没有电台声卡 | 电台 USB 声卡没枚举 → 换线/换口；服务日志里找 PyAudio 设备列表 |
| 瀑布是合成谱 | 见第 11 节 | FT-710：查 FTDI 库与 `0403:601c` |
| **Cloud Hub 显示"已连接"但公网 502** | `verify.sh` 第 8、9 项 | 多半是 `fleet/frpc` 缺失（`connect()` 会返回 `tunnel_started:false`） |
| 公网证书报错 | `verify.sh` 第 7 项 | `EnvironmentFile` 与 `MRRC_CONFIG_FILE` 必须指向**同一个文件**（设计 D-11） |
| 服务反复重启 | `journalctl -u mrrc-modern -n 100` | 看 Python traceback；`mrrc-radio show` 核对型号与串口 |
| 发射没功率 | 状态行的 `TX[... pk:...]` | 采集源/音量；FT-710 的 `mic_gain`；SSB 无话音即无功率 |
| 延迟越来越大 | 状态行 `J:` | 正常在 100–300 ms；长期 >500 说明抖动缓冲在追，查网络 |
