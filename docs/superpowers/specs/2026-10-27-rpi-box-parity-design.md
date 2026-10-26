# 树莓派镜像线对齐：一键升级 + 盒子设置控制全面可用

- **日期**：2026-10-27
- **状态**：设计定稿，等待实施计划
- **范围**：**只有树莓派镜像线**（`packaging/rpi/`）。服务层
  （`packaging/common/mrrc-service-layer.sh`）与设置/升级的运行时代码
  （`box_ops.py`、`net_wifi.py`、`box_update.py`、`static/settings.html`、
  `static/setup.html`）**本设计一行不改** —— 它们本来就是两条镜像线共用的。
- **相关代码**：`packaging/rpi/build-image.sh`、`packaging/rpi/verify-image.sh`、
  `linux/fix_resolv_conf.sh`、`tests/test_rpi_packaging.py`、
  `tests/test_fix_resolv_conf.py`、`docs/RASPBERRY_PI_GUIDE.md`、`docs/rpi_pack.md`、
  `website/zh/index.html`、`website/index.html`、
  `.agents/skills/dual-platform-release/release-artifacts.json`
- **相关记录**：CHANGELOG `## [镜像线 …]` 条目族；SDD/12（打包）、SDD/14（版本史）；
  设计 `docs/superpowers/specs/2026-10-11-w103d-lan-box-settings-design.md`（设置面本体）

---

## 1. 背景：两条镜像线的能力，一边在跑一边在停

2026-10-11 起，两条镜像线共用一套服务层（`packaging/common/`），平台壳
（`packaging/box/` vs `packaging/rpi/`）只负责板级与发行版差异。于是"盒子有、树莓派没有"
从结构上应该是不可能的事。现状不是这样：

| 能力 | W103D 盒子 | 树莓派镜像（已发布） |
| --- | --- | --- |
| 出厂版本 | `1.28.11 · FP7`（`latest.json` → `boxLine`） | **`1.28.1`**，2026-10-17 08:26 构建 |
| `/settings` 设置面 | ✅ | ✅（1.26.0 起） |
| **一键升级**（`/api/update/apply` → `box_update`） | ✅ | **❌ 没有**（`box_update` 于 2026-10-18 落地，晚于那次构建） |
| 修复包编号 `FP` | ✅ | ❌（同一原因） |
| 性能负载 `/Perf`、NanoVNA `/Nano`、音频电平、SQL | ✅ | ❌（同一原因） |
| Wi-Fi 面板 / 烧卡预置 Wi-Fi | ✅ | **❌ 出厂 Wi-Fi 是关的**（§2.1） |
| 文档口径 | 站上"一键升级" | 指南写着"更新 = 烧录新版本镜像" |

证据（本轮实测，不是推测）：

- `dist/rpi/MRRC-Modern-v1.28.1-rpi64.img.xz`，`530,820,848` 字节，
  SHA-256 `a00ab22897bf3e3565fcb5b50eff9585b2885c7b831dd2ae594672d62475e5be`
  —— 与站上下载卡、`docs/RASPBERRY_PI_GUIDE.md` 里那一组数字**逐字节相同**，
  即**线上那张镜像就是这次 1.28.1 构建**。
- `build/pi-gen/deploy/build-docker.log`（同一次构建）里有
  `version: 1.28.1`、`mrrc-box-helper.service → multi-user.target.wants`、
  `entry points import OK`，而**没有任何一行** `do_wifi_country` 或 `WPA_COUNTRY`。

所以本次交付的定义是：**把树莓派镜像重出一版，让它在"版本升级/更新"和"盒子设置控制"
两件事上与盒子**同构**，并把只在 Pi 平台暴露的缺陷修掉、用门禁钉住、把文档改成真话。**

## 2. 三个只在 Pi 平台存在的缺陷

### 2.1 出厂 Wi-Fi 是软关闭的（根因：构建参数缺失）

pi-gen 在 `stage2/02-net-tweaks/01-run.sh`（锁定 ref
`2026-06-18-raspios-bookworm-arm64`）里这样决定无线状态：

```bash
if [ -v WPA_COUNTRY ]; then
    raspi-config nonint do_wifi_country "${WPA_COUNTRY}"
elif [ -d /var/lib/NetworkManager ]; then
    # NetworkManager unblocks all WLAN devices by default. Prevent that:
    echo "[main]\nWirelessEnabled=false" > /var/lib/NetworkManager/NetworkManager.state
fi
```

`build.sh` 对未设的变量只做 `export WPA_COUNTRY`（标记导出，**不赋值**），而
`[ -v ]` 判的是"有没有值"：实测 `unset X; export X; [ -v X ]` → **假**。我们的
`packaging/rpi/build-image.sh` 生成的 `config` 从来不设这个键 ⇒ 走 `elif` ⇒
镜像第一启就带着 `/var/lib/NetworkManager/NetworkManager.state` 里的
`WirelessEnabled=false`。

后果（全部落在"盒子的设置控制"上）：

1. `/settings` 的 Wi-Fi 面板扫到 **0 个网络** —— 看起来像"树莓派的 Wi-Fi 不支持"；
2. `mrrc-wifi-preseed.service`（`/boot/firmware/mrrc-wifi.txt` → `nmcli`）**连不上**，
   而这是"卡片烧好直接寄给客户"唯一的免屏配网路径；
3. 无线电管制域没有落到任何国家码，5 GHz 与部分驱动按世界域处理，
   发射权限是最保守的一档。

盒子（Armbian）没有这条路径，所以这个缺陷从来没人踩到。

### 2.2 「修复 DNS」在健康的 Pi 上给出错误结论

`linux/fix_resolv_conf.sh` 用 **stub 文件是否存在**判断"resolved 在不在跑"：

```bash
if [ ! -e "$STUB" ]; then   # /run/systemd/resolve/stub-resolv.conf
    echo "… does not exist (resolved not started?) …" >&2
    exit 1
fi
```

`/run` 是 tmpfs，只有 systemd-resolved 真跑着才有这个 stub。树莓派的解析链不是它：
pi-gen 的 `export-image/03-network/01-run.sh` 在导出阶段直接写死
`/etc/resolv.conf` = `nameserver 8.8.8.8`，之后由 NetworkManager 接管重写。
在这样一台机器上，脚本会**退出 1**，于是：

- 设置页的「修复 DNS」按钮报失败（对一台 DNS 完全正常的机器）；
- `linux/mrrc_update.sh` 每次升级都打一行 WARNING（"still points at a static file"）。

文件它确实没改（这一步是对的），但**结论是错的**——而错误的结论会让人去修不该修的东西。

### 2.3 老镜像没有一键升级，而文档还在教重烧

已发布的 1.28.1 里没有 `box_update`，`/settings` 的更新卡不存在。指南第八节因此写着
"更新 = 烧录新版本镜像（配置和密码不会保留）"——这句话在 1.28.11 之后就不再是事实，
但客户读到的仍是它。

## 3. 目标 / 非目标

**目标**

1. 新树莓派镜像里，登录后**只靠浏览器**就能完成：看到当前版本（☰ 页脚 `版本 · FPn`）→
   看到"可升级到 x.y.z" → 点「立即升级」→ 服务自重启 → 页面回来显示新版本与新的 FP；
   失败时 90 秒内自动回滚并如实报告。
2. 设置面的每一块在 Pi 上**真的能用**：Wi-Fi 扫描/切换/保存/自动连接/忘记、有线
   DHCP/静态、主机名（含 mDNS 立即生效）、时区、系统口令、重启、DNS 修复、蓝牙遥控配对。
3. 这些结论有**离线门禁**（构建产物里可判定的部分）与**真机验收记录**（§5）两重支撑，
   文档里的每一个"可用"都能指到其中一条。
4. 发布口径统一：站上两张下载卡、Pi 指南、构建手册、`latest.json` 与 CHANGELOG 讲同一件事。

**非目标**

- 不给树莓派建独立镜像线 / 独立版本号 / 独立清单段（那等于第二套实现，
  `tests/test_packaging_parity.py` 会红，且这正是过去三份排除清单漂移的老路）。
- 不动服务层与设置/升级的运行时代码。若实施中发现必须动，先改本设计。
- 不给 `box_update` 补 pip 与原生库重编（两条线共同的已知限制，另开评估）。
- 不改 `net_wifi` 的 `band bg`（2.4 GHz 强制）等既有取舍。

## 4. 设计

### 4.1 平台壳：把管制域与 Wi-Fi 使能钉进构建

`packaging/rpi/build-image.sh` 生成的 `config` 增加一行：

```sh
WPA_COUNTRY=${MRRC_PI_WPA_COUNTRY-CN}
```

- 默认 `CN`：这台产品面向国内客户出货；`CN` 允许 2.4 GHz 1–13 与 5 GHz 149–165，
  是"最不容易把客户的 AP 排除在外"的一档。构建时可覆盖（`MRRC_PI_WPA_COUNTRY=US` 等）。
- 写进注释：这**不是**偏好设置，而是"不设它，镜像出厂 Wi-Fi 是关的"这条 pi-gen 行为。
- `docs/rpi_pack.md` 的"构建期开关"表格补一行，与三个镜像源开关并列。
- **不改**服务层，也不给盒子加同名参数（盒子没有这条路径）。

### 4.2 解析链修复：先问"这台机器用不用 resolved"

`linux/fix_resolv_conf.sh` 增加一个状态判据，把"没有 resolved 的机器"从"坏机器"里分出来：

| stub 文件 | systemd-resolved 状态 | 行为 |
| --- | --- | --- |
| 存在 | 任意 | **修复**（现状不变） |
| 缺失 | `active` 或 `enabled` | 退出 1，说明"resolved 在但 stub 未出现"（现状不变） |
| 缺失 | 不存在 / 未启用 | **退出 0**，说明「这台机器不用 systemd-resolved，解析由 NetworkManager 管理，无需修复」 |
| 缺失 | 无法判定（无 `systemctl`） | 退出 1（保守：不装作修好了） |

状态判定顺序：`systemctl is-active --quiet systemd-resolved` → `is-enabled` →
都不成立则视为"不适用"；`systemctl` 本身不可用时视为"无法判定"。
测试用 `MRRC_RESOLVED_STATE=active|enabled|absent|unknown` 注入，四档全覆盖；
真实环境不注入时走上面的探测。

`linux/mrrc_update.sh` 不需要改：它只看退出码，退出 0 就不再打那行假 WARNING。

### 4.3 离线门禁：`packaging/rpi/verify-image.sh` 加六条

四条对应"盒子有、Pi 从没验过"，两条对应 §2.1/§2.2：

| 步骤 | 新增检查 | 失败意味着 |
| --- | --- | --- |
| step 2 已装 | `/usr/local/bin/mrrc-update` 存在且含 `fix_resolv_conf.sh` | 升级链的第一半缺失 |
| step 2 已装 | `mrrc-box-helper.service` 存在且已 enable（`multi-user.target.wants` 链接） | 重启/改名/时区/DNS 修复全部失效（页面会一直说"helper unavailable"） |
| step 2 已装 | `/etc/resolv.conf` 里不得出现 `192.168.65.` 或 `# DNS requests are forwarded to the host` | 镜像带着**构建机**的解析地址出门（Armbian 2026-10-25 那个形态） |
| step 2 已装 | `/var/lib/NetworkManager/NetworkManager.state` 不得含 `WirelessEnabled=false` | §2.1 又发生了一次 |
| step 4 能跑 | chroot 里 `import box_update, box_helper` 成功 | 一键升级/提权通道在镜像里根本 import 不了 |
| step 4 能跑 | chroot 里 `box_update.watchdog_command()` 生成的脚本里带 `curl`，且镜像内有 `curl` | 回滚探活永远成功（永不回滚），这是"静默回滚"的反面 |

`tests/test_rpi_packaging.py` 增加一条**源码级**断言：`build-image.sh` 生成的 `config`
必须有非空的 `WPA_COUNTRY` 赋值（默认值可读出来），且不是 `${VAR-}` 这种空默认。
这一条在构建前就生效，不等一小时的全量构建。

### 4.4 文档

**`docs/RASPBERRY_PI_GUIDE.md`**（用户向，改动最大）

- 「二、烧卡」：新产物的字节数与 SHA-256；地址从 `raspberrypi.local` 改为
  `MRRC-Modern.local`（服务层把静态主机名设成 `MRRC-Modern`，pi-gen 的
  `TARGET_HOSTNAME` 也是它 —— 旧文档那个名字**根本不会解析**）。
- 「三、首启」：并入 `/setup`（设口令 + 选 Wi-Fi）这条主路径，保留 HDMI/SSH 两条兜底。
- 新增一节「进来之后能做什么」：`/settings` 各面板清单 + ☰ 菜单页脚的 `版本 · FPn`。
- 「八、更新」重写：**`/settings` → 立即升级**（断线十几秒、失败自动回滚、发射中拒绝）。
  老镜像（≤ 1.28.1）**推荐重烧**；脚注保留 SSH 一次性过渡
  （`sudo MRRC_UPDATE_SRC=… mrrc-update`）给拿不到卡的人，并写明"这一步做完以后就再也不用 SSH"。

**`docs/rpi_pack.md`**（构建方向）

- `MRRC_PI_WPA_COUNTRY` 开关（默认 CN）+ 上面那条"不设它 Wi-Fi 是关的"的机理。
- 产物四步验证里新增的六条（§4.3）。
- 一句现状校准：上一次构建是 1.28.1（2026-10-17），本次重出。

**`AGENTS.md`**：`packaging/` 那一行补一句"Pi 平台壳另外设 pi-gen 的 `WPA_COUNTRY`
（不设则出厂 Wi-Fi 关闭）"。

### 4.5 版本与发布（**在真机验收之前**）

- CHANGELOG 新增镜像线条目（拟 `## [镜像线 v1.28.12 · FP8]`，准确号在发布时由
  `dev_tools/make_latest_json.py` / release 技能定；标题里的版本与 FP 是
  `make_box_source.version_of()` 的唯一来源）。
- 产物：`packaging/rpi/build-image.sh` → `dist/rpi/MRRC-Modern-v1.28.12-rpi64.img.xz`
  → 上传为**固定名** `website/downloads/MRRC-Modern-rpi64.img.xz`（老链接不失效）。
- 同步三处数字：`website/index.html`、`website/zh/index.html` 的下载卡
  （`531 MB (…) · SHA-256: …`）与 `docs/RASPBERRY_PI_GUIDE.md`。
- `release-artifacts.json`：rpi64 那条从 `optional: true` 改为必检
  （"一旦发布过就必须在"，否则下次谁把它撤下来没人发现）。
- 验收命令：`python3 .agents/skills/dual-platform-release/harness/release_check.py --online`
  （含下载 URL 与线上 SHA 比对）。

### 4.6 交付顺序：先发布，后真机验收（已定）

本轮的顺序是 **改代码 → 构建 → 离线门禁 → 发布 → 真机验收 → 结论回填**。
这条顺序有一个必须说清楚的代价，和一条必须守住的纪律：

**代价**：发布时我们对真机行为只有离线门禁的把握。若 §5 中某条在真机不达标，
修复走 **FP 号 +1 的下一版镜像**（下载是固定文件名，重出即覆盖）——而不是把这次发布
撤下来。真机失败不会留下一个坏掉的下载链接，只会让"已验证"这句话晚一步说。

**纪律**：发布那一刻，CHANGELOG 与下载卡**只写离线门禁通过的部分**，不写"真机可用"。
具体措辞：

> 离线门禁：产物六项检查通过（`verify-image.sh`）。真机验收矩阵见 spec §5，结论回填本条目。

真机结论回来后（无论是过还是不过），**回填到同一条 CHANGELOG 条目**，而不是另开一条——
同一次交付只该有一个可以被引用的编号。

## 5. 真机验收矩阵（**发布之后**执行，结论回填 CHANGELOG 同一条目）

| # | 项 | 判据 | 不通过时的第一嫌疑 |
| --- | --- | --- | --- |
| 1 | 首启 | `/setup` 设口令 → 自动登录进主页；`sudo mrrc-show-password` 与之一致 | `_first_window_open` 的子网判定 |
| 2 | Wi-Fi 扫描 | `/settings` 能列出至少一个可见 AP（**这是 §2.1 的回归判据**） | `NetworkManager.state` / 管制域 |
| 3 | Wi-Fi 切换 | 切换成功 → 拔网线仍在线；切到不存在的 SSID 能回滚 | NM 回滚路径 |
| 4 | 有线 | DHCP 拿到地址；改静态后可达、再改回 DHCP | R-1（v1.26.0 起未被验过） |
| 5 | 身份 | 改名后 `<新名>.local` 立即解析（avahi-reload 生效） | polkit / box_helper |
| 6 | 时区 | 改名/改时区后 `date` 与页面一致 | box_helper |
| 7 | 升级 | 「立即升级」→ 断线十几秒 → 页面回新版本 + 新 FP | box_update / 看门狗 |
| 8 | 升级拒绝 | 发射中（PTT/CQ/FT8）点升级被拒并说明原因 | `_update_refusal` |
| 9 | 回滚 | 喂一个坏包 → 90 秒后旧树回来、页面报"已回滚" | 看门狗 `curl` 探活 |
| 10 | 蓝牙 | 遥控器扫描/配对 → 按键真的换频 | 已知形态："配上了但不工作" = 链接未加密 |
| 11 | 解析链 | 「修复 DNS」在 Pi 上答"无需修复"，且外网可通 | §4.2 |
| 12 | 性能页 | `/Perf` 有温度/CPU/事件循环延迟三项真值 | `host_perf` 在 Pi 的 sysfs 路径 |

第 1–2、6–9 条**必须**在真机上做；第 3–5、10–12 条可以只做一次并记录结论。
真机不达标的项，CHANGELOG 里明确写"未验证"，不写"可用"。

验收记录写回 `docs/RASPBERRY_PI_GUIDE.md` 末尾的"验收记录"小节（日期 + 机型 + 逐条结论），
并回填该 CHANGELOG 条目 —— 两处指向同一批结论，避免"文档说能用、条目说没验"。

## 6. 风险

| 风险 | 影响 | 应对 |
| --- | --- | --- |
| R-A 全量构建一小时级，且依赖外部卷 ≥20 GB | 构建中途失败 | `PRESERVE_CONTAINER=1` + `CONTINUE=1` 续跑；先用 `tests/test_rpi_packaging.py` 的源码级断言把 §2.1 之外的错误挡在构建之前 |
| R-B `WPA_COUNTRY=CN` 对海外客户不是最优 | 5 GHz 部分信道不可用 | 构建开关可覆盖；文档写明默认值与覆盖方法 |
| R-C 一键升级在 Pi 上首次真机验证，且**验收在发布之后** | 若真机失败，线上一段时间里那几张卡描述的是未经真机确认的镜像 | 下载是固定名，重出即覆盖；结论回填同一条目（§4.6）；§5 第 9 条专门验回滚，失败形态是"页面报已回滚"而不是砖机 |
| R-D 老镜像用户不看指南 | 继续用 1.28.1 | 下载卡文案写明"本次含一键升级"，指南第八节第一句就写重烧 |

## 7. 本设计不解决的事（明确挂账）

- `box_update.apply` 不跑 `pip install`、不重编 `ft8/dsp`：一个改动了
  `requirements.txt` 或 DSP 源码的版本，一键升级后需要一次 `mrrc-update` 或重启才能完全生效。
  两条线同病，不在本次范围内。
- 树莓派与盒子同名（`MRRC-Modern`）同网段时的 mDNS 冲突由 avahi 自我介绍
  `MRRC-Modern-2.local` 兜底，本次不引入"按序列号命名"。
