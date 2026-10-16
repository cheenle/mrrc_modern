# W103D 发货版（预置）镜像 · 设计

- **日期**：2026-10-17
- **状态**：已批准（2026-10-17 逐节口头确认），进入实现计划
- **范围**：部署层 + 设置页（Wi-Fi 面板 + 呼号/公网入口面板）。`packaging/box/build-image.sh`、`packaging/common/{mrrc-service-layer.sh,preflash.sh(新)}`、`packaging/common/firstboot_wrapper.py`、`linux/first_run.py`、`static/settings.html`、测试与文档。**CAT / 音频 / PTT / 协议 / WS 一行不改**；Cloud Hub 的接口与状态机也不改（设置页复用既有三个端点）。
- **相关**：
  - `docs/superpowers/specs/2026-10-06-w103d-radio-box-design.md`（D-3「构建期不烘焙猜测」、D-11 配置文件身份）
  - `docs/superpowers/specs/2026-10-11-w103d-lan-box-settings-design.md`（取代 `2026-10-08-w103d-setup-ap-design.md`）
  - `SDD/08-architecture-decisions.md` AD-019 / AD-021 / AD-026、`SDD/05-non-functional-requirements.md` NFR-067 / NFR-068
  - `packaging/box/README.md`、`docs/W103D_GUIDE.md`、`packaging/box/verify.sh`

---

## 1. 背景：卖家代刷、用户开箱即用

一批 W103D 由卖家（本仓运维者）代刷后快递给用户。用户拿到手的期待是**插电就能用**：不接 HDMI、不 SSH、不需要知道"首启生成的口令"在哪。

今天的镜像做不到，三处硬拦：

1. **Web 口令每台现生成**，而且不是首启生成的 —— 是**服务启动时** `server.py:_ensure_strong_password()` 生成并写回 env 的。用户没有显示器、不 SSH 就永远不知道口令。
2. **Wi-Fi 只能从扫描列表点选**（`static/settings.html`），密码框是掩码；隐藏网络、扫描空表都没有出路。
3. **公网入口（Cloud Hub）只在控制界面的 ☰ 对话框里申请**（呼号 → 申请 → 轮询 → 建隧道 → 登记证书）。它是整套流程里最需要“运维参与”的一步，却埋在应用里的第三层菜单下；而盒子设置页（`/settings`）没有任何入口。

还有一条**已经用真机否掉的退路**必须记住：盒子自己开热点引导在 MT7663S 上不可用（AD-026，2026-10-11 实测：NM 认为 AP 已激活，信标不上空）。因此"用户家里没有网线、也没人替他预置 Wi-Fi"这个状态下盒子是够不着的 —— **这是本设计的硬边界，不是缺陷**（§10）。

### 1.1 现场取证（2026-10-17，对 1.28.1 产物的只读检查）

对 `build/w103d/w103d.img`（1.28.1）用 `part-nodes.sh` + 只读挂载逐项读过：

| 事实 | 读数 |
| --- | --- |
| `/opt/mrrc_modern/env/mrrc.env` | **存在**（构建期由 `install.sh` 写、服务层搬过来） |
| 该文件里的 `MRRC_WEB_PASSWORD` / `MRRC_SERIAL_PORT` | **不存在**（服务层的 `sed` 有意剥掉，V2.79 ⑤） |
| `/var/lib/mrrc/` | 只有 `certs/`，**没有 `firstboot-done`** |
| `/etc/shadow` | `root` 有口令哈希（ophub 出厂 `1234`）、`mrrc:!`（锁定，无口令） |

由此推出两条**今天真实存在、而文档没写对**的事实：

1. **串口/声卡探测在镜像上从未运行过。** `firstboot_wrapper.main()` 在"env 文件存在"分支上带 `MRRC_PRESEED=1` 调 `linux/first_run.py`，而 `first_run.main()` 第一行就 `return 0` ⇒ `apply_first_run()`（探测 + 写 `MRRC_PORT_CONFIRMED`）不会被调用。盒子上电台能被认出来，靠的是操作者事后在 ☰「连接设置」里选一次，或 `mrrc-radio use`。
2. **`/setup` 的免密引导窗口在镜像上从未打开过。** 窗口的判据是 `MRRC_AUTO_PASSWORD=1`（`server.py:_password_is_untouched`），而这个键只有 `first_run.apply_first_run()` 会写 —— 它没跑过。口令是服务生成的，于是 `_password_is_untouched()` 恒假。指南 §6 描述的"首启探测串口/声卡"与"免口令引导窗口"在盒子上都不成立。

> 第 2 条**不在本次范围**：本次只让探测跑起来，不改口令/引导窗口的语义（见 D-8 与 §9）。

---

## 2. 目标 / 非目标

**目标**

| | 内容 |
| --- | --- |
| G1 | **用户零初始化**：上电即用，没有任何"首次配置"页面或步骤 |
| G2 | **固定口令**：`root`=`abcd1234`、`mrrc`=`abcd!1234`、**Web**=`abcd!1234`（初始值；Web 口令页面里仍可改） |
| G3 | **系统层对齐参考机**：TZ=`Asia/Shanghai`、`LANG=zh_CN.UTF-8`、NTP 自动同步 |
| G4 | **Wi-Fi 全在设置页配**：手输 SSID + 明文密码（含隐藏 SSID 与扫描空表）；扫码点选仍然好用 |
| G5 | **预置默认关**：不传 `--preflash` 时不写任何预置（口令照旧由服务在启动时生成、时区/locale/账号口令一律不碰） |
| G6 | **电台探测自愈**（本次追加）：探测真的运行（今天在镜像上从未运行，§1.1），且**跑到定型为止**，让"电台比盒子晚插上"也能自己好 |
| G7 | **设置页能申请公网入口**：填呼号 → 申请 → 看状态 → 批准后看到入口地址；卖家在发货前替客户做掉（客户事后也能自己重来） |

**非目标**

- 不做 AP 热点引导（真机已否，AD-026）。
- 不做已保存 PSK 回显（NFR-068 的边界，见 D-6）。
- **不在设置页重做 Cloud Hub 对话框**：联系人、批准码认领（claim）、刷新、隧道/证书细节仍留在 ☰ 里（见 D-9）。
- **不联动 FT8 的 `my_call`**：那是另一个库里的另一件事，本次各管各的。
- **不要求写 Wi-Fi 预置**：镜像里的 `/boot/mrrc-wifi.txt` 能力保留不动，但操作单不再使用它（见 D-7）。
- 不做每台不同口令（明确"先固定"）。
- 预置只作用于盒子线（Pi 不做预置，但**共享层支持该开关**，将来要发货版是白拿的）。
- **G5 只约束"预置"**：D-8 的探测自愈是**有意**改变所有镜像（含桌面启动器）的行为 —— 那是一个独立缺陷（§1.1 第 1 条），不是预置的副作用。
- 不动无线国家码 / 不改 /setup 免密窗口语义（§9 记为顺带发现）。
- 不做镜像发布与网站卡片（发版是独立变更）。

---

## 3. 需求与验收判据

| | 需求 | 可测判据 |
| --- | --- | --- |
| R1 | 出厂预置在构建期完成 | `bash packaging/box/build-image.sh --preflash` 产出镜像；产物里 `env/mrrc.env` 含固定 Web 口令 |
| R2 | 三条口令固定且可用 | 真机：`ssh root` 用 `abcd1234`、`su - mrrc` 用 `abcd!1234`、`/login` 用 `abcd!1234`（三条各验一次） |
| R3 | 系统层 | 产物：`/etc/timezone`=`Asia/Shanghai`、`/etc/localtime`→`Asia/Shanghai`、`locale -a` 含 `zh_CN.utf8`；真机：`timedatectl` 显示 `System clock synchronized: yes` |
| R4 | 用户不碰命令行 | 真机：插网线上电 → 浏览器登录 → 完成一切；全程不需要 SSH/HDMI（**验收记录里逐条写清哪些动作在浏览器里做**） |
| R5 | Wi-Fi 手输 + 明文 + 扫描 | 真机：①扫描列表点选能连；②手输 SSID（含扫描表里没有的隐藏网络）能连；③"显示密码"按钮能把掩码切成明文；④已保存的 PSK **不回显** |
| R6 | Wi-Fi 全在设置页配 | 真机：插网线进 `/settings` → 扫描点选连上 2.4 GHz；再手输一个扫描表里没有的 SSID 连上。全程不需要预置文件、不需要 SSH |
| R7 | 探测自愈 | 真机/单测：未定型的盒子每次开机重探（`journalctl -u mrrc-firstboot` 有痕迹），定型后不再重探；配置的串口节点消失时重新进入未定型 |
| R8 | 预置默认关 | 单测 + 对照构建：不传 `--preflash` 时产物里没有固定口令、shadow 不是预置哈希、`--preflash` 的代码路径不执行 |
| R9 | 呼号 → 申请 → 入口 | 真机：设置页填呼号 → 点申请 → 显示"已提交，等待批准"；在 portal 批准后，盒子自己接通并在同一面板显示 `https://<label>.mrrc.vlsc.net/`；端口断网时页面给出可读的失败原因 |

---

## 4. 设计决策

### D-1 一个构建开关，不是第二条构建线

**决定**：`build-image.sh` 加 `--preflash`（默认关）。预置逻辑走**共享服务层**，用 `MRRC_PREFLASH=1` 进入。

**备选**：①另起后处理脚本（挂载改口令再打包）：仓库会多出一条易漂移的第二条线，以后每次改服务层都要同步；②不加开关、直接把盒子线默认做成发货版：镜像从此不再"每台口令不同"，也给不出自用/开发用的标准镜像。

**理由**：出货形态应该是**显式选择**，而不是把默认行为悄悄换掉。这与本仓"构建期不许烘焙猜测、但允许烘焙明示的出厂值"（2026-10-06 设计 D-3）的边界一致：出厂口令是**声明过的值**，不是对硬件的猜测。

### D-2 预置逻辑放共享服务层，不放盒子壳

**决定**：`packaging/common/preflash.sh`（新）+ `mrrc-service-layer.sh` 里一个受开关保护的调用块。

**理由**：写固定口令、设时区/locale、改账号口令这四件事**与板子无关**；本仓的硬规矩是"服务层只有一份"（`tests/test_packaging_parity.py` 会报红）。放在共享层，Pi 线将来要出厂版是白拿的。平台差异仍然只有那两处（`MRRC_WEB_HOST`、`MRRC_BOOT_DIR`）。

### D-3 预置口令的形态：脚本默认值 + 环境变量覆盖

**决定**：默认值写在 `preflash.sh` 里，按批次可用同名环境变量覆盖，不改代码：

| 变量 | 默认值 |
| --- | --- |
| `MRRC_PREFLASH_ROOT_PASSWORD` | `abcd1234` |
| `MRRC_PREFLASH_USER_PASSWORD` | `abcd!1234` |
| `MRRC_PREFLASH_WEB_PASSWORD` | `abcd!1234` |
| `MRRC_PREFLASH_TIMEZONE` | `Asia/Shanghai` |
| `MRRC_PREFLASH_LOCALE` | `zh_CN.UTF-8` |

例：`MRRC_PREFLASH_WEB_PASSWORD='别的' bash packaging/box/build-image.sh --preflash`。

**写入方式**：env 文件里**替换式**写 `MRRC_WEB_PASSWORD=<固定值>` + `MRRC_AUTO_PASSWORD=`（显式置空），幂等。除这两个键之外**不写任何东西** —— 尤其**不写** `MRRC_RADIO_MODEL` / `MRRC_SERIAL_PORT`（D-8 让探测决定它们；也是 NFR-067 的同向：不预置未验证型号）。

### D-4 `!` 在口令里的转义面必须逐层验

口令含 `!`，会经过：构建脚本 → 容器 → chroot → env 文件 → systemd `EnvironmentFile` → `chpasswd` / `su` / `ssh` / 页面 JSON。**不做统一的"转义"处理**，而是在真机上把三条口令各用一遍（R2）—— 只在某一层出错时才发现，比在每一层都加一层转义更可靠。

### D-5 首启仍要跑，但它不再生成口令

**决定**：`mrrc-firstboot.service` 保留。预置后它的行为是：口令已是非默认值 ⇒ `apply_first_run()` 不碰它（只在**空或等于公有默认值**时才生成）；自签证书仍然**每台现生成**（`/var/lib/mrrc/certs` 不随镜像分发，保持"私钥不共享"）。

**推论**：`/setup` 的免密窗口**从第一秒起就是关的**（`_password_is_untouched()` 为假），`GET /setup` 对未登录者 302 到 `/login`。这正是"不需要初始化"在服务端的样子，也是 R4 的判据之一。

### D-6 Wi-Fi 面板：输入时明文，落盘后不回显

**决定**（用户确认"甲"）：

- SSID 变成**可手输的文本框**；点扫描结果会**填入**这个框（不是取代它）。隐藏网络只能手输 —— 这正是要解决的场景。
- 密码框加**显示/隐藏**按钮，只切换 `type`（`password` ↔ `text`），**不改值语义**。
- 扫描返回空表时给**明确提示**（现在的页面是一片空白，看起来像"没点对"）。
- 面板加一句"这台盒子只支持 2.4 GHz"。
- **不回显已保存的 PSK**：那需要 `mrrc` 向 root helper 索要 NetworkManager 里存的密码。NFR-068 / AD-021 把"Wi-Fi 密码永不离开 nmcli 的 argv"钉成不变量（`net_wifi.py` 的既有一句承诺），不回显就是**不动那条不变量**。

**服务端不改**：`POST /api/box/wifi` 本来就收任意 SSID（只 `strip()` + 空值 400），密码仍然只进 `nmcli` 的 argv。

### D-7 本次不要求预置 Wi-Fi（能力保留、操作单不用）

**背景（曾经的决定，2026-10-17 撤回）**：原设计打算让卖家在烧好的 U 盘 FAT 分区写 `mrrc-wifi.txt`（`SSID=`/`PSK=`），借 `armbian-install` 克隆 `/boot` 把凭据带进 eMMC，让用户开箱自动连网。

**决定**：**不做这一步**。卖家的实际流程是"刷完 eMMC → 上电 → 插 LAN 线 → 在设置页里改 Wi-Fi"，那已经是零键盘的一条路；再维护一条"预置文件"的工艺只多两个陷阱（U 盘系统自己会把文件消费掉；`armbian-install` 是否克隆 `/boot` 全部内容未验证）却不多一分价值。

**保留的东西**：镜像里 `mrrc-wifi-preseed.service` + `/boot/mrrc-wifi.txt` 这条能力**原样不动**（它本来就在），将来想预置直接可用 —— 只是操作单不要求、不验收它。

### D-8 探测自愈：完成判据从"跑过一次"改成"配置已定型"

**现状（取证见 §1.1）**：镜像上探测从未运行；完成判据是"写过一次 `/var/lib/mrrc/firstboot-done`"。

**决定**（用户确认"甲"）：

1. `linux/first_run.py`：`MRRC_PRESEED=1` 的语义从"**短路返回**"改成"**不生成口令、其余照常**"（预置文件是权威口令来源）。于是探测在**有 env 文件**（=所有镜像）时也会跑。
2. `firstboot_wrapper.py`：**只在配置定型时才盖 `firstboot-done`**，否则不盖（下次开机再来）。盖章判据是 `first_run.needs_first_run(env) == False`。
3. `needs_first_run()` 增加一条**设备节点存在**检查：配置的串口节点在 `/dev` 里不存在 ⇒ 视为未定型。理由：卖家工作台上测过电台的盒子会把工作台上的端口路径克隆给用户，而 FT-710 的两个 CP210x 同 VID:PID、端口归属由枚举顺序决定（仓库已记录该歧义）。节点检查让用户在用户家的枚举结果重新成为权威。
   实现上把"节点存在"做**可注入参数**（默认 `os.path.exists`），保持该函数的纯函数性质与可测性。
4. 未定型时**不阻塞开机**：探测在 `mrrc-firstboot.service` 里跑（oneshot、`After=network-online.target`），服务照常起；探不到只是"这次没探到"，不影响其它任何东西。

**代价（诚实记录）**：未定型的盒子每次开机都会枚举串口；**只有当真的存在候选串口时**才会发出一次探测包（`detect_serial_ports()` 为空就不发包）。这是与"用户最可能的情形正是电台晚插"之间的取舍，取"多探"。

**顺带修掉的东西**：桌面线（`macos/launcher.py` 已在每次启动时调 `needs_first_run`/`apply_first_run`）从此也会在"电台拔掉了"时重新探测 —— 同一函数，同一收益。

**边界**：端口**存在但设备不是电台**（枚举顺序歧义的另一半）不在本决策的射程内。残留故障的表现是"电台连不上"，退路是 ☰「连接设置」或 `mrrc-radio use`（§9）。

### D-9 设置页的"公网入口"面板：只搬最小闭环（用户选"甲"+"不联动 FT8"）

**决定**：`/settings` 新增一个面板，**四样东西**：呼号输入框、"申请公网入口"按钮、一行状态、以及只读的门户地址。

**服务端零改动**（预计）：三个既有端点已经够用 ——

| 用途 | 端点 | 返回 |
| --- | --- | --- |
| 画页面 | `GET /api/cloud/state` | `callsign` / `has_token` / `connected` / `entry` / `portal` / `tunnel_running` / `tunnel_error` / `cert_reload_required` |
| 提交申请 | `POST /api/cloud/apply` `{callsign}` | `submitted` + `status`；并顺手启动后台轮询 `_cloud_start_autoconnect()` |
| 查一次/接通 | `POST /api/cloud/refresh` | `status` 或 `connected` + `entry` |

**不搬的东西**（留在 ☰ 的 Cloud Hub 对话框）：联系人、批准码认领（`claim`）、隧道/证书细节、重连。

**必须处理好的四个状态**（否则面板会骗人）：

| 状态 | 页面显示 |
| --- | --- |
| 本机没有申请令牌 | 填呼号 + 申请按钮 |
| 已申请、等批准 | "已提交，等待批准（呼号 X）"；提示批准由运维在门户完成，**不用守着页面**（盒子后台每 30 s 轮一次，`_CLOUD_AUTOCONNECT_INTERVAL`） |
| 已批准并接通 | "入口：https://`<label>`.mrrc.vlsc.net/"；证书刚换时提示"重启盒子后生效"（`cert_reload_required`） |
| 被拒绝（portal 核验 `rejected`） | 把 portal 的原话显示出来 + 让用户联系运维，**不装作还在等** |

**两个边界写进文案**：

- **改呼号 = 新申请**：门户按呼号建申请，旧入口不会自动迁移（panel 在提交前提示）。
- **同一呼号重复申请会被门户拒**（`store.apply` 查重：已在申请/已核验/已授予就报错）。所以：**本机已有令牌时按钮改成"查询状态"**，不发新申请；真的丢了令牌（比如 env 被手改），显示门户的原话并指向 ☰ 里的**批准码认领**那条路。

**不做**：不联动 FT8 的 `my_call`（另一个库、另一件事）；不新增任何写入端点；不改 Cloud Hub 的状态机。

---

## 5. 首启数据流（改造后）

```
用户上电（eMMC 启动）
 ├─ mrrc-wifi-preseed.service   ConditionPathExists=/boot/mrrc-wifi.txt（本次不写这个文件）
 │    ├─ 连上 → nmcli 建 profile（0600）→ 删文件
 │    └─ 连不上 → 文件留着，下个开机再试（不谎报成功）
 ├─ mrrc-firstboot.service      ConditionPathExists=!/var/lib/mrrc/firstboot-done
 │    ├─ ensure_cert()（每台现生成，幂等）
 │    ├─ first_run.py（PRESEED=1：不生成口令，只探测电台/串口）
 │    ├─ 补 headless 默认值（WEB_HOST/PORT、SSL_CERT/KEY）
 │    └─ 定型了吗？是 → 盖 firstboot-done；否 → 不盖，下次开机重探
 └─ mrrc-modern.service         /login 用 abcd!1234 登录 → 直接是控制界面
```

`/setup`：因为口令已非默认（`MRRC_AUTO_PASSWORD` 非 1），**免密窗口恒关**，未登录访问 302 到 `/login`。

**公网入口那条线（设置页面板，工作台上做）**：

```
/settings 填呼号 → POST /api/cloud/apply → 得到申请令牌（写 MRRC_CLOUD_CALLSIGN/_TOKEN/_PORTAL）
                                             └─ 后台每 30 s POST /status 轮询（_cloud_start_autoconnect）
运维在 portal 控制台批准（grant）
                    └─ 下一次轮询拿到 granted → connect()：frpc 隧道 + /enroll 登记证书 + 写 _LABEL/_PORT/_ENTRY
                       └─ _cert_reload_required → 页面提示重启；发货前的关机/开机就是那次重启（V2.80 实测）
```

---

## 6. 发货工艺（操作单要落成这几步）

1. **出镜像**：`bash packaging/box/build-image.sh --preflash`（产物与今天同名同路径）
2. **烧 U 盘**：现有步骤（`gzip -dc … | sudo dd of=/dev/rdiskN bs=4m`）
3. **装进 eMMC**：盒子从 U 盘启动 → `armbian-install`（`307` / `1`）→ 见 `w103d-emmc` 技能
4. `poweroff` → 拔 U 盘 → **重新上电**（跑的就是要发货的那套系统；这一步也顺带把 Cloud Hub 写下的证书路径生效，V2.80 实测）
5. **在工作台上做掉公网入口（丙：操作单按这一步写）**：插 LAN 线 → 浏览器打开 `/settings` 用 `abcd!1234` 登录 → 填**客户的呼号** → 点申请 → 你在 portal 控制台批准 → 面板显示入口地址 `https://<label>.mrrc.vlsc.net/`
6. **关机发货**（客户拿到的盒子：公网入口已就绪；Wi-Fi 到用户家自己从设置页改）

**发货前提醒（写进操作单）**：Wi-Fi 在 `/settings` 里配，**只能用 2.4 GHz**（5 GHz 在这块芯片上能关联但数据面不通，见 `w103d-box`）。

---

## 7. 改动清单（文件级）

| 文件 | 改动 |
| --- | --- |
| `packaging/box/build-image.sh` | `--preflash` 开关（默认关）→ 容器里 `export MRRC_PREFLASH=1`（走 `MRRC_FRP_PROXY` 那条既有环境变量通道）；`--check` 行为不变 |
| `packaging/common/preflash.sh`（新） | 默认值 + `--env-only` 模式（只写 env，便于单测真跑）；写 env、`chpasswd`、时区、locale；幂等；`MRRC_PREFLASH!=1` 时**拒绝做任何事**。`set -e`：`chpasswd`/`locale-gen` 失败必须让**构建失败**，不许静默产出一个"口令没设成"的镜像；账号由平台壳先建好（服务层已有前置断言） |
| `packaging/common/mrrc-service-layer.sh` | 开关保护的一小块调用（放在 headless 默认值块之后，最后再 `chown/chmod` 一次） |
| `packaging/common/firstboot_wrapper.py` | 盖章判据改为"已定型"；未定型时打印一行、不盖章、不 disable 单元 |
| `linux/first_run.py` | `MRRC_PRESEED` 语义改为"不生成口令"；`needs_first_run(env, port_exists=...)` 增加节点存在判据 |
| `static/settings.html` | ①Wi-Fi 面板：SSID 文本框 + 显示/隐藏 + 空表提示 + 2.4 GHz 提示，扫描点选**填入**输入框；②新增"公网入口"面板：呼号 + 申请按钮 + 状态/入口地址 + 只读的门户地址 |
| `server.py` | **预计不改**：`GET /api/cloud/state`、`POST /api/cloud/apply`、`POST /api/cloud/refresh` 三个既有端点已经提供面板需要的全部读写（申请后后台轮询由 `_cloud_start_autoconnect()` 负责）。若实现时发现缺一个字段，**只允许加只读字段**，不新增写入路径 |
| `packaging/box/README.md` | 新增"发货版（`--preflash`）"一节（§6 的六步 + 口令表 + 陷阱） |
| `docs/W103D_GUIDE.md` | §7 口令一览加"发货版"列；§9.3 标注"预置能力仍在、发货流程不用它"；§9.8 Wi-Fi 面板（手输 + 明文）与公网入口面板 |
| `CHANGELOG.md` | Unreleased 条目 |
| `tests/` | 新增 `test_preflash.py`；`test_first_run*.py` 扩探测判据；Wi-Fi 面板与 NFR-068 的守卫 |
| `SDD/` | §11 的同步清单 |

**实施顺序建议**（三批，可独立验收）：①构建开关 + `preflash.sh` + 服务层接线（含系统层：口令/时区/locale）；②`settings.html` 两面板（Wi-Fi + 公网入口）；③探测自愈（`first_run` + `firstboot_wrapper`）。①②互不依赖，③改的是共享层（影响桌面启动器），单独一批便于回滚。

---

## 8. 测试与验收

### 8.1 单测（能真跑的绝不做纯文本断言）

| 测试 | 断言 |
| --- | --- |
| `tests/test_preflash.py`（新） | `bash packaging/common/preflash.sh --env-only <tmp>` **真执行**：固定口令写入、`MRRC_AUTO_PASSWORD=` 为空、其它键一个不丢、跑两次结果相同（幂等）、**`MRRC_PREFLASH` 未置 1 时零写入** |
| `tests/test_first_run*.py` | `needs_first_run()`：口令/型号/端口三者的组合；新增"端口节点不存在 ⇒ 未定型"（注入假的 `port_exists`）；`MRRC_PRESEED=1` 时**不生成口令但会探测** |
| 设置页公网入口面板 | 面板只用三个既有端点（源码守卫：`settings.html` 里出现的 `/api/cloud/*` 不出这三个）；**已有令牌时按钮走查询而不是重复申请**（允许在 JS 里静态断言，真机在 A6 验） |
| 守卫（文本级，钉住默认与不变量） | ①`build-image.sh` 的 `--preflash` 默认关；②`chpasswd`/`locale-gen`/时区三行都在 `MRRC_PREFLASH` 分支内；③`static/settings.html` 的 PSK 框默认仍是 `type="password"`；④仓库里不存在 `show-secrets` / 读 NM profile 的调用（NFR-068 同向） |
| 既有守卫 | `tests/test_packaging_parity.py`、`test_box_profiles.py`、`test_rpi_packaging.py`、`tests/test_cloud_endpoints.py` 全绿（两条线的差异仍然只有那两处；Cloud Hub 端点行为未变） |

### 8.2 产物验证（真构建之后，用真产物）

在既有的"四步验证"（见 `w103d-box` 技能）上追加两组断言：

- **预置版（`--preflash`）**：`env/mrrc.env` 含 `MRRC_WEB_PASSWORD=abcd!1234` 且 `MRRC_AUTO_PASSWORD=` 为空；`/etc/timezone`=`Asia/Shanghai`；`/etc/localtime`→`/usr/share/zoneinfo/Asia/Shanghai`；`locale -a` 含 `zh_CN.utf8`；用 `openssl passwd -6 -salt <salt>`（容器里没有 openssl 时改用 chroot 内 venv 的 `python3 -c 'import crypt'`）复算 `root` / `mrrc` 的 shadow 哈希。
- **对照版（不带开关）**：同一组断言**全反**（无固定口令、shadow 不是那个哈希、无 `/etc/timezone`）。

### 8.3 真机验收（构建绿 ≠ 盒子能用）

| | 动作 | 判据 |
| --- | --- | --- |
| A1 | 不插电台、不预置 Wi-Fi，插网线上电 | 服务起、`https://<IP>:8888` 能用 `abcd!1234` 登录（**全程不开 SSH**） |
| A2 | `ssh root@<IP>` / `su - mrrc` | 分别接受 `abcd1234` / `abcd!1234` |
| A3 | `curl -ik https://<IP>:8888/setup` | 302（不是 200 的向导） |
| A4 | 浏览器打开 `/settings` | 点"扫描"→ 点选一个网 → 密码框点"显示"看到明文 → 连上；再手输一个扫描表里没有的 SSID 连上 |
| A5 | 插上电台后**重启**盒子 | `journalctl -u mrrc-firstboot -b` 显示重探；`/settings` 或界面上型号/串口正确（R7） |
| A6 | 发货流程走到第 5 步（§6）：`/settings` 填一个真实呼号 → 申请 → 在 portal 批准 | 面板出现"已提交，等待批准"，批准后不刷新页面也能变"入口：https://…"；`ssh` 进去看 `MRRC_CLOUD_ENTRY`/`MRRC_CLOUD_LABEL` 已落 env |
| A7 | `timedatectl` | `System clock synchronized: yes`，时区 `Asia/Shanghai` |
| A8 | 断网时点申请 | 面板给出可读的失败原因（"portal did not answer…"），不是静默或 500 |

---

## 9. 风险、未决与顺带发现

| | 内容 | 处理 |
| --- | --- | --- |
| R-1 | **固定口令即公开口令**：拿到镜像或任一台同批盒子的人能进所有盒子（Web 可以在页面改；`root`/`mrrc` 只能 SSH/HDMI 改） | 明确记录在指南与操作单里；这是"初始先固定"的已知代价，不是疏漏 |
| R-2 | **呼号改了，旧入口不跟着迁移**：portal 按呼号建申请，改呼号=新申请；旧入口留在 portal 那边由运维处理 | 页面在改呼号时明确提示"这是新申请，旧入口不会自动迁移"（D-9） |
| R-3 | **申请与批准是一对人工协作**：批准由运维在 portal 控制台做，盒子只负责轮询 | 面板把"已提交、等批准"写清楚，不留"点了没反应"的观感（A6） |
| R-4 | 口令含 `!` 的转义面 | D-4：真机三条口令各用一遍（A2/A1） |
| R-5 | 用户换路由器/改 Wi-Fi 密码后得自己在设置页重配；改错了就失联 | 硬边界：无 AP 兜底，只能网线/HDMI 或重刷。写进指南，并在设置页那句 2.4 GHz 提示旁说清风险 |
| R-6 | 未定型盒子每次开机探测（D-8 的代价） | 只枚举 + 有候选才发包；不作为失败上报。观测点 `journalctl -u mrrc-firstboot` |
| R-7 | 端口"存在但设备不是电台" | D-8 边界；退路是 ☰「连接设置」/`mrrc-radio use` |
| R-8 | **设置页与 ☰ 的 Cloud Hub 对话框并存**，同一份状态两个入口 | 设置页只读状态 + 两个动作（申请、刷新），写入仍只有 `POST /api/cloud/apply` 一条路；不复制状态机（D-9） |
| **顺带发现（不在本次范围）** | **`/setup` 的免密引导窗口在镜像上从未打开过**（`MRRC_AUTO_PASSWORD` 永远为空）；指南 §6 与 §9.8 的描述与运行时不符 | 本次**只记录不改**（改它会重新打开一个免口令窗口，是安全决策，要单独走一次设计与验收）。实现时在指南相应段落加一句现状说明 |

---

## 10. 边界（明确未验证）

- **热点兜底不存在**：用户既没网线、也没人替他预置 Wi-Fi，而盒子又连不上他家 Wi-Fi 时，盒子够不着（只能 HDMI 或重刷）。
- **只支持 2.4 GHz**（芯片问题，非配置问题）。
- **locale 在 chroot 里生成的产物形态**（§8.2 会验）、**`--preflash` 产物的真机全链路**（§8.3）、**公网入口在真机上的端到端**（A6）—— 三项都要真跑过才算数。
- 公网入口的 IPv6 入站可达性取决于运营商网关策略，不在本设计范围（入口走隧道，本就不依赖入站）。
- 本次**不出镜像、不发版、不动网站卡片**。

---

## 11. 文档同步（实现完成时一并提交）

| 文件 | 内容 |
| --- | --- |
| `SDD/08-architecture-decisions.md` | 新增 **AD-027：出厂预置（发货版）镜像（`--preflash`，默认关）—— 固定口令 + 系统层预置；Wi-Fi 与公网入口都从设置页配**；注明它与 D-3「构建期不烘焙猜测」的边界（出厂值是**声明值**，不是硬件猜测）与 NFR-068 的关系（不回显 PSK） |
| `SDD/05-non-functional-requirements.md` | 新增 **NFR-069：出厂预置的可披露性** —— 发货版的三条口令是公开知识，必须写在指南与操作单里，且三条都能改 |
| `SDD/14-version-history.md` | 一条 V2.91 条目（含 R-7 与"顺带发现"） |
| `SDD/README.md` | 版本号随之 +1 |
| `SDD/10-service-model.md` | 设置页的两个新入口（Wi-Fi 手输、公网入口申请）写进 `/settings` 那一行；Cloud Hub 服务行不改（接口没变） |
| `SDD/12-operational-model.md` | 发货流程（§6 的六步）写进盒子运维节 |
| `docs/W103D_GUIDE.md` / `packaging/box/README.md` / `CHANGELOG.md` / `tests/README.md` | §7 口令一览、§9.3、§9.8（Wi-Fi 面板 + 公网入口面板）、§14（Cloud Hub 新增入口）；发货单；Unreleased；测试计数 |
| `docs/DOCUMENT_INDEX.md` | 本规格进"设计规范"表 |
