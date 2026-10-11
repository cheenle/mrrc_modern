# W103D 局域网盒子设置页 · 设计

- **日期**：2026-10-11
- **状态**：待评审（本文件通过后进入实现计划）
- **取代**：`2026-10-08-w103d-setup-ap-design.md`（其 R-1 真机验证失败，见 §1）
- **相关**：`docs/W103D_GUIDE.md`、`SDD/08-architecture-decisions.md` AD-026、`packaging/box/`

---

## 1. 背景与动机

2026-10-08 的 setup 热点设计用「刷完机没有网络 → 盒子自己开热点 → 手机连热点进向导」解决首次配置。2026-10-11 在 W103D 真机上验证，**该设计的 R-1 失败**：

- 热点在软件层**完全正常**：`nmcli connection up` 一次成功，`iw dev wlan0 info` 给出 `ssid MRRC-Setup / type AP / channel 6 (2437 MHz) / txpower 18.00 dBm`，`ip -4 addr` 给出 `10.42.0.1/24`，`dmesg` 干净。
- 但**没有任何客户端能扫到它**。三条独立证据：
  1. Mac 定向 join：`networksetup -setairportnetwork en1 MRRC-Setup` → `Could not find network`（该探针已用对照实验标定，能区分"找到"与"没找到"）；
  2. 热点"在线"后 60 秒，`/sys/class/net/wlan0/statistics/tx_packets` 只 +2（正常 AP 在 ch6 每 100ms 一个 beacon，60 秒该有约 600）；
  3. 用户手机同样扫不到。
- 已排除两类解释：**固件未被篡改**（盒子、本地仓库、`vendor-mt7663.md` 钉死的 SHA-256 三方一致）；**换固件无解** —— 按 `prefer_offload_fw=0` 载入非 offload 的 `mt7663_n9_rebb.bin` 后，芯片**根本不初始化**（`query whisr timeout`、`Invalid MAC address`、`MCU initialization failed: -110`），说明 offload 固件是这颗芯片在**这块板子上唯一能跑的**。

**结论**：MT7663S 在 W103D 上不做 AP。热点的整套机械（root supervisor、无密码门、两个 JSON 信箱、`static/setup.html` 向导、`mrrc-setup-ap.service`）全部作废。

**操作者决定**：放弃热点引导，盒子改走 LAN 配置。

> 真机验证还附带发现两个独立缺陷，本设计必须一并处理：
> 1. `net_wifi.start_hotspot()` 建出的 AP profile 默认 `connection.autoconnect=yes`，NM 开机自播开放热点 → 进 AP mode **把固件搞崩**（`W103D mailbox write … failed: -110`），此后整个 uptime wlan0 全死。本次三次开机中了两次。退役后这条路径消失，但**先落盘再切换**的教训保留（见 §6.2）。
> 2. `/etc/NetworkManager/system-connections/` 里出现过 0 字节的 `Hotspot.nmconnection`，导致 NM 每次启动报 `invalid connection`。本设计新增 NM profile 时不再有匿名遗留，但"profile 写入必须原子且可回滚"同样保留（见 §6.2）。

---

## 2. 目标

1. 盒子刷完后，**不需要 HDMI、键盘、串口**，在局域网里用浏览器把盒子本身配好。
2. 覆盖六件事：**Wi-Fi 连接、网页密码、盒子名字与 mDNS、网络体检、盒子系统（重启/服务状态/时区）、电台配置**。
3. 引导（首次）与日常管理**分成两个页面**，但**共用同一套后端 API** —— 重复的只有 HTML 布局，不是逻辑。
4. 完整退役热点向导的机械，并把它的两条教训（先落盘再切换、可回滚）继承下来。

### 非目标

- 不改电台本身的功能面（CAT / 音频 / 频谱 / 录制）。
- 不做日志浏览器，不做系统在线更新。
- 不改变 Windows / macOS 桌面版的功能面；新面板在桌面上**优雅降级**（相关面板置灰并说明原因）。
- 不做 WPA3 / WPA-Enterprise / 隐藏 SSID 的图形化配置（见 §6.1）。

---

## 3. 术语

| 词 | 含义 |
|---|---|
| **引导页** | `/setup`，盒子**未配置**时的一次性页面，只有 Wi-Fi 与密码两块 |
| **设置页** | `/settings`，已验证登录后的完整管理页，六个面板 |
| **首次窗口** | 盒子从未被用户设过密码时，同子网客户端可以不登录访问引导页的那段状态（§5） |
| **helper** | `mrrc-box-helper.service`，以 root 跑的、只认固定动词的服务（§7） |
| **上线路径** | 盒子出现在 LAN 上、可被浏览器访问的方式：网线（eth0）或预置 Wi-Fi（§9） |

---

## 4. 总体结构与边界

```
                        ┌─────────────────────────────┐
   浏览器 ──HTTPS──►    │  server.py (用户 mrrc)      │
   /setup  (引导页)     │   ├ /api/box/*   新增        │
   /settings (设置页)   │   ├ 认证中间件（+首次窗口）  │
                        │   └ 静态页 setup/settings    │
                        └──────┬───────────┬──────────┘
                               │           │
                     NM D-Bus / nmcli   本地 socket
                     (polkit 放行 mrrc)    │
                     ┌─────────▼──┐   ┌────▼──────────────────┐
                     │net_wifi.py │   │ box-helper (root)     │
                     │（收缩到 STA）│   │ 固定动词表：           │
                     └────────────┘   │  reboot / set-hostname│
                                      │  / set-timezone /     │
                                      │  avahi-reload         │
                                      └───────────────────────┘
```

**七个边界**

1. **`net_wifi.py`（收缩，不删除）** —— 只留 STA / NM 半边。
   - **保留**：`scan_wifi`、`connect_wifi`、`devices`、`wifi_interface`、`ipv4_address`、`nm_running`、`nmcli`/`subprocess_runner` 传输层、`parse_row`/`parse_rows` 转义解析、`NmResult`、`_int_or`、`scrub`。
   - **删除 · AP 生命周期**：`start_hotspot`、`stop_hotspot`、`hotspot_active`、`active_ap_connections`、`_connection_is_ap`、`_active_uuids`、`_profiles_named`。
   - **删除 · 无密码门**：`gate_is_open`、`client_ip`、`state_is_live`、`ApState`、`read_state`、`write_state`、`ensure_state_dir`。
   - **删除 · 两个 JSON 信箱**：`WizardClaim`、`read_wizard`、`write_wizard`、`claim_is_fresh`、`_read_json`、`_write_json`、`_pick`。
     *（信箱随 supervisor 一起消失 —— 新版没有跨用户共享目录，`box_ops` 的切换状态是进程内的，见 §6.2。）*
   - **删除 · "有没有上行"这一族**：`has_uplink`、`any_device_connecting`、`_is_usable_uplink`、`CONNECTED_WORDS`、`TRANSITIONAL_STATES`、`UPLINK_TYPES`。
     *（它们唯一的消费者是"该不该起热点"这个决定；网络体检直接表达 `devices()` 的状态，不需要一个布尔判断。）*
   - **删除 · 热点专用常量**：`DEFAULT_SSID`、`HOTSPOT_PROFILE`、`NM_SHARED_SUBNET`、`DEFAULT_GATEWAY`、`MODE_OFF`/`MODE_HOTSPOT`/`MODE_STA`、`WIZARD_SWITCHING`/`WIZARD_OK`/`WIZARD_FAILED`、`ap_settings`、`ap_network`、`setup_url`、`DEFAULT_STATE_DIR`、`STATE_NAME`、`WIZARD_NAME`、`STATE_GROUP`、`HEARTBEAT_MAX_AGE_S`、`CLAIM_MAX_AGE_S`。`DEFAULT_IFNAME`/`DEFAULT_WEB_PORT` 若 `box_ops` 仍用到则留。
   - **保留 `scrub()`** —— Wi-Fi 切换的失败原因仍要过它。
   - 模块文档里「唯一写者」「密码不离开 argv」两条规矩**继续有效**，并且本设计不得破坏它们（这是选路线乙的直接理由，见 §7）。
   
   > 说明：本设计**撤回**先前"硬删 `net_wifi.py`"的决定。该文件 748 行里过半是 STA 与 nmcli 基础设施，正是"网页选 Wi-Fi"所需要的；删掉它等于把同样这些零件重写一遍。

2. **新增 `box_ops.py`（纯逻辑，无 FastAPI 依赖）** —— Wi-Fi 切换的编排与状态机、网络体检采样、密码落盘、helper 客户端。**这是两个页面共用的唯一后端**，用于消掉"两个页面 = 逻辑写两遍"的风险。可注入 runner，可脱离 Linux/NM 单测（照抄 `net_wifi.py` 的成功经验）。

3. **新增 `linux/box_helper.py` + `mrrc-box-helper.service`（root）** —— 只认固定动词，逐动词校验参数。见 §7。

4. **`server.py`** —— 新增 `/api/box/*` 一组端点与 `/settings` 页面路由；`/setup` 路由保留但页面**重写**；认证中间件新增「首次窗口」判据。见 §5、§10。

5. **两个页面** —— `static/setup.html`（**重写**为引导页）、`static/settings.html`（新，六面板），共用 `static/box_api.js`（API 客户端与首次窗口跳转逻辑）。

6. **退役** —— 见 §11。

7. **电台配置不搬家** —— `/settings` 直接调现有 `GET/POST /api/setup` 与 `GET /api/devices`。这避免同时改动应用内 Settings 对话框与其测试。

---

## 5. 认证与首次窗口

### 5.1 判据

```
首次窗口开着  ⟺  ① 盒子从未被用户设过密码
             且  ② 请求的 client_ip 落在盒子自己的任意一个 IPv4 子网内
```

- **①「从未被用户设过密码」**：`os.environ["MRRC_AUTO_PASSWORD"] == "1"`（首启自动生成、用户未接管），或 `WEB_PASSWORD` 为空 / 仍等于出厂默认 `changeme_please_use_strong_password!`。
  这个标志是**现成的**：`POST /api/setup` 与旧向导的密码端点都在用户接管时把它写成 `""`。
- **②「自己任意一个子网」**：新写 `local_subnets()`，从 `nmcli` / `ip` 收盒子的全部 IPv4 地址并取网络号。**这与旧设计不同**：旧的是"热点那一个子网"，新的是"我自己所有的子网"。

### 5.2 关闭

**设密码的那一次写盘成功即关**。没有开关可被忘记清 —— 判据是状态本身，不是标志。这正是旧设计 D-6 的教训，只是判据从"热点活着"换成"密码还是出厂值"。

### 5.3 可写面收窄

首次窗口**只能写密码这一个键**。这是旧向导 `SETUP_WRITABLE_KEYS = {"MRRC_WEB_PASSWORD", "MRRC_AUTO_PASSWORD"}` 的同一收窄思路，必须继承：窗口所在的面是产品的**唯一无密码写面**。

窗口内放行的端点**只有两个**：
- `GET /api/box/onboarding` —— 引导页渲染所需的最小信息（是否需要设密码、Wi-Fi 列表、盒子名字）
- `POST /api/box/password` —— 唯一的写

页面路由 `/setup` 本身放行（否则会被重定向到 `/login`）。`/settings` 与其余 `/api/box/*` **一律要求登录**。

### 5.4 已知的取舍（不是缺陷，是明知的弱化）

窗口判据里"同子网"**比旧设计弱**：旧的热点网络外人进不去；LAN 是共享的。缓解是窗口只在"从未设过密码"时存在，而网线/预置 Wi-Fi 这两种上线方式通常发生在家里的私网里。**宿舍网 / 公司网 / 咖啡馆网确实存在"抢先后到"的风险。** 这一条写进 `W103D_GUIDE.md` 的用户提示：*首次开机请在你信任的网络里完成设置*。

### 5.5 密码写入的落地

复用旧向导已验证的模式（`server.py:5133-5166`）：

```python
WEB_PASSWORD = password
os.environ["MRRC_WEB_PASSWORD"] = password
os.environ["MRRC_AUTO_PASSWORD"] = ""
# 经 first_run.update_env_file(_config_file_path(), updates) 落盘
```

**不重启**。旧向导的注释说清了理由：重启会把操作者扔在向导中间，而下一步本来就要把网络拿走。改密码**不能**复用 `POST /api/setup` —— 那个端点先校验 `radio_model` + `serial_port`，语义是"电台配置"，不是"设密码"。

窗口内设密码成功后**直接签发一个会话**，前端跳 `/settings`，不让用户再登一次。

---

## 6. 网络域

### 6.1 Wi-Fi 列表

`net_wifi.scan_wifi()`（`mrrc` 可读，无需提权）。信号从强到弱、按 SSID 去重、隐藏网络丢弃（`nmcli` 的 `-t` 对 SSID 里的冒号有转义，`parse_row` 已正确处理）。

前端只显示 SSID、信号、是否加密。**不支持** WPA3 / WPA-Enterprise / 隐藏 SSID 的图形化配置：企业网络要 802.1X 凭据与 CA，隐藏网络要显式 SSID 输入，两者都超出本期范围。

### 6.2 Wi-Fi 切换（本功能最难的一段）

**单射频**：盒子只有一块 MT7663。切换分两种情况，页面必须自己判断并在动手前明确说清：

| 你从哪条路访问盒子 | 切 Wi-Fi 的后果 |
|---|---|
| **eth0 网线** | 会话不受影响。安全 |
| **wlan0（当前就是它）** | **你自己这条连接会断**。页面必须先明确警告：*保存后本页会断开，约 60 秒后到 `<新地址>` 看结果* |

流程（两种情况同一套，只是提示不同）：

1. **先落盘、后切换** —— 新 profile 写进 NM 并设 `autoconnect yes`，**旧 profile 保留不删**（回滚要靠它）。
   *继承的教训*：`start_hotspot()` 当年就是因为没管 `autoconnect` 而让盒子开机自杀 WiFi；任何新写的 profile 都必须显式交代 `autoconnect` 的取值。
2. **异步执行** —— `POST /api/box/wifi` 只"下单"，真正切换在后台 asyncio 任务里跑（`box_ops` 的状态机），`GET /api/box/wifi/status` 供页面重连后读结果。
   **状态放在 `box_ops` 的进程内存里**，不落盘 —— 老设计落盘是因为 root supervisor 与 `mrrc` server 是两个进程、要共享状态；现在切换就发生在 server 进程内，没有第二个读者。代价是 **server 重启会丢掉切换进度**，可以接受（页面此时应提示"盒子重启过，请刷新查看当前网络"）。
3. **超时回滚** —— 新 SSID 在预算内（初值 30 秒，真机调）没拿到 IPv4 → `nmcli connection up <旧 profile>`，盒子退回原网络，并把失败原因回传。失败原因必须过 `net_wifi.scrub()` 洗掉密码。
4. **告知新地址** —— 切成功盒子就在别的网段，旧地址失效。**这是 mDNS 不是"锦上添花"而是刚需的地方**：`<盒子名>.local` 是切换后唯一可靠的回头路（见 §8）。

### 6.3 网络体检（只读）

当前 IPv4 / 网关 / DNS / 默认路由走哪个口 / wlan0 的 SSID 与信号 / 有线口状态 / 每个 NM 连接的 `autoconnect`。全部 `nmcli` 读，**不需要提权**。出问题时一眼看出断在哪一段。这些内容**吸收**旧 `/api/manage` 的只读价值。

### 6.4 有线口 DHCP ↔ 静态

改 NM profile。如果你改的正是**当前这条路**，同样会断 —— 复用 §6.2 的异步 + 回滚 + 回告模式。

---

## 7. 提权：polkit 规则 + root helper

### 7.1 事实

- `mrrc-modern.service` 以 **`mrrc`** 用户跑（`User=mrrc`），**不在 sudo 组**。
- 实测：`mrrc` **能读** `nmcli`，**不能写** —— `nmcli connection add` 返回 `Insufficient privileges`。
- 板上的 polkit 规则（`/usr/share/polkit-1/rules.d/org.freedesktop.NetworkManager.rules`）要求 `subject.local && subject.active`。**systemd 起的系统用户进程没有会话**，`subject.active` 为假，所以**把 `mrrc` 加进 `netdev` 组也大概率不成立**。

> 注意：用 `sudo -u mrrc` 测出来的结果**不算数** —— 那样起的进程可能仍挂在操作者的 SSH 会话上。R-2 要求**用真服务**验。

### 7.2 决定：polkit 放行 `mrrc` + 小 helper（路线乙）

打包时落一个约 6 行的 `/etc/polkit-1/rules.d/49-mrrc-nm.rules`：

```js
polkit.addRule(function(action, subject) {
    if (action.id.indexOf("org.freedesktop.NetworkManager.") === 0 &&
        subject.user === "mrrc") {
        return polkit.Result.YES;
    }
});
```

按 `subject.user` 匹配，**不要求活跃会话**，正好治 §7.1 的问题。`mrrc` 于是直接调 `nmcli`，**Wi-Fi 密码只走 `net_wifi` 一条路**，现有的 `scrub()` 继续管用。

**代价（明写）**：这条规则给的是**整个 NM 的 `modify.system`**，不是逐动词白名单。放宽的范围仅限"网络重配置"，而且那个文件本身就是可审计的全部。**被否掉的备选路线甲**（helper 承担全部 nmcli 写）虽然只有一条提权通道，但要求 Wi-Fi 密码经过第二条通道（socket），会破坏 `net_wifi.py` 模块文档里"密码离开这个模块的 argv，不落日志、不落 env、不落信箱"的规矩，需要重新论证隐私 —— 不值得。

### 7.3 helper 的形状

- **传输**：Unix domain socket `/run/mrrc/box-helper.sock`，`0660 root:mrrc`。一行 JSON 进、一行 JSON 出。
  目录由 unit 的 `RuntimeDirectory=mrrc` + `RuntimeDirectoryMode=0770` 交给 systemd 建（`/run` 是 tmpfs，每次开机干净，且它的属主/权限由 systemd 保证，不靠脚本 chown）。
- **对端校验**：helper 用 `SO_PEERCRED` 取对端 uid，**必须是 `mrrc`**，否则拒绝。
- **动词表**（四个，全部无敏感参数）：

  | 动词 | 参数校验 |
  |---|---|
  | `reboot` | 无参数 |
  | `set-hostname <name>` | `^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$`（大小写都允许，见 §8） |
  | `set-timezone <tz>` | 必须在 `/usr/share/zoneinfo/` 下真实存在，且不含 `..` |
  | `avahi-reload` | 无参数（`systemctl reload avahi-daemon`） |

- **实现约束**：绝不 `shell=True`，绝不用字符串拼接命令，一律 argv 数组。动词失败写 journal（这些动词都没有敏感参数，但仍不记录多余内容）。
- **只读的不走 helper**：`systemctl is-active`、`hostname`、`timedatectl` 展示，`mrrc` 自己读得到。
- **失败降级**：helper 不在 → 只读 `GET /api/box/status` 里标记 `helper_available: false`，**相关面板置灰并说明原因**，其余面板照常。桌面版（Windows/macOS）就是这个状态。
- **打包**：`box-overlay.sh` 写 unit 并 enable；`verify.sh` 检查 helper 在跑、socket 存在且权限正确。

---

## 8. 盒子身份与 mDNS

- **打包加 `avahi-daemon` 并 enable**。板上现在只有 `avahi-autoipd`（169.254 链路本地，与 mDNS 无关）。
- **默认 hostname 不再是 `armbian`** —— 产品上不能叫这个。默认 **`MRRC-Modern`**，**页面可改**（`hostnamectl set-hostname`，走 helper）。多台盒子同网段会撞名：avahi 撞名时会自称 `MRRC-Modern-2.local`，页面**显示当前实际可达的名字**。多台盒子同网段时建议改名。
  - **大小写**：`MRRC-Modern` 含大写，因此 `set-hostname` 的校验正则必须允许大小写（§7.3）。systemd 接受大写的静态 hostname；DNS 名字本身大小写不敏感，所以 `mrrc-modern.local` 一样解析得到。
  - **打包负责落下这个名字**：`box-overlay.sh` 在首次写入时 `hostnamectl set-hostname MRRC-Modern`（并同步 `/etc/hosts`），否则镜像里仍是 `armbian`。
- **`<名字>.local` 不是万能的**，必须写进页面与文档：iOS 完美，Windows 需装 Bonjour，Android 看机型。因此同时保证 **DHCP 主机名**（NM 的 `dhcp-send-hostname`，默认开）作为**对所有客户端都有效**的退路 —— 路由器后台的客户端列表里能看到盒子名。**页面上两个都给**：mDNS 名字 + 当前 IPv4。

---

## 9. 首次上线的两条路

### 9.1 网线（eth0）

- 板上 `eth0` 是 **Amlogic Meson 板载 MAC**（`ff3f0000.ethernet`，altname `end0`，maxmtu 3712），驱动在，现在 `NO-CARRIER` 只是没插线。
- eth0 的 NM profile 默认 `ipv4.method=auto` → 插线即 DHCP。实现时要确认盘中那个 `有线连接 1`（当前未绑设备）是 `auto` 且 `autoconnect yes`。
- **这是新版 R-1，必须插线实测**（见 §13）。

### 9.2 刷机时预置 Wi-Fi

- 板上 `/dev/sda1` 是 **511M vfat**，挂在 `/boot`，LABEL `BOOT` → 用户在自己电脑上直接能写文件。
- **已有的邻居**：`packaging/box/firstboot_wrapper.py:adopt_preseed()` 已经在读 `/boot/mrrc.env`，落盘到 `$ENV_DIR/mrrc.env`，把原件改名 `mrrc.env.applied` 再删掉。新机制**照抄这个形状**（读 → 应用 → 删），但**必须用一个独立的文件**：
  - 约定文件名 **`/boot/mrrc-wifi.txt`**，内容：

    ```
    SSID=我的WiFi
    PSK=密码
    ```

  - **为什么不写成 `/boot/mrrc.env` 里的键**：设计 **D-4** 规定「Wi-Fi 是 NetworkManager 的领域，不是 `MRRC_*` env 文件」，理由是 env 文件会被复制到 `/opt/mrrc_modern/env/mrrc.env`（0640）、可能进支持包 —— 一条 Wi-Fi 密码一旦落进配置层，就和配置键混在一起了。`mrrc.env` 那套是给**配置键**用的；Wi-Fi 凭据走独立文件，用完即删。文件名用 `.txt` 而不是 `.env`，正是为了不让人把它和那个 preseed 弄混。
- **放进一个独立的小 unit，不是并进 `mrrc-firstboot.service`**：`mrrc-firstboot.service` 有 `After=network-online.target`，而"连上 Wi-Fi"**正是让网络上线的那件事**，塞进去会绕成一个圈。新建 `mrrc-wifi-preseed.service`（`Type=oneshot`、`After=NetworkManager.service`、`ConditionPathExists=/boot/mrrc-wifi.txt`、`Before=mrrc-firstboot.service`），跑完 `nmcli device wifi connect` 后**删除该文件**（FAT 没有权限位，明文 PSK 不能一直躺在上面）。R-8。
- **边界（写进文档）**：这条**只在"镜像刷到 SD 卡"时成立**。刷到 eMMC 时没有用户能碰到的 FAT 分区。R-5。

### 9.3 与首次窗口的关系

§9 讲"盒子怎么在 LAN 上"，§5 讲"进了 LAN 之后谁可以开那个网页"。两件事独立。上线之后访问 `https://<地址>:8888/` 时，未配置的盒子应落到 `/setup`。

---

## 10. 路由与 API 清单

### 页面

| 路由 | 文件 | 认证 |
|---|---|---|
| `GET /setup` | `static/setup.html`（重写） | 放行；**盒子已配置时 302 → `/settings`** |
| `GET /settings` | `static/settings.html`（新） | **必须登录** |

### 新增 `/api/box/*`

| 方法 | 路径 | 认证 | 作用 |
|---|---|---|---|
| GET | `/api/box/onboarding` | 首次窗口 | 引导页要的全部：`needs_password` + Wi-Fi 列表 + 盒子名字 |
| POST | `/api/box/password` | 窗口 **或** 登录 | 改密码（窗口内唯一可写的键）。成功后签发会话 |
| GET | `/api/box/status` | 登录 | 聚合只读：网络体检 + 系统状态 + 身份 + 安全字段 + `helper_available` |
| GET | `/api/box/wifi` | 登录 | 扫描 |
| POST | `/api/box/wifi` | 登录 | **下单**切换（异步） |
| GET | `/api/box/wifi/status` | 登录 | 切换进度/结果（断线重连后来读） |
| POST | `/api/box/hostname` | 登录 | 改名字 → helper |
| POST | `/api/box/timezone` | 登录 | 改时区 → helper |
| POST | `/api/box/link` | 登录 | 有线口 DHCP/静态 |
| POST | `/api/box/system/reboot` | 登录 | 重启**盒子** → helper |

### 原样复用（不改动）

- `GET/POST /api/setup` + `GET /api/devices` → 电台配置面板
- `POST /api/restart` → 重启**服务**（不是盒子；系统面板上两个按钮并排但语义分明，前端文案要说清）

### 中间件改动

放行白名单从「`SETUP_GATE_PATHS`（热点向导四路径）」换成：**`/setup` 页面** + **`/api/box/onboarding`** + **`POST /api/box/password`**，且后两个在 handler 里经 `_first_window_open(request)` 判定。其余 `/api/box/*` 与 `/settings` 走正常认证。

---

## 11. 退役清单

### 删除

| 目标 | 说明 |
|---|---|
| `linux/setup_ap.py` | 热点 root supervisor（371 行） |
| `static/manage.html` | 只读管理页（107 行），价值并入 `settings.html` |
| `net_wifi.py` 的 AP 半边 + 门 + 信箱 | §4.1 列出的函数与常量 |
| `server.py` 向导区块 | `_setup_ap_state_dir` / `_setup_gate_open` / `_setup_access` / `_setup_denial` / `_setup_auto_password` / `GET /api/setup/wizard` / `POST /api/setup/wizard/password` / `_wifi_failure_reason` / `_claim_heartbeat` / `_perform_wifi_switch` / `WIFI_SWITCH_LEAD_S` / `WIFI_SWITCH_HEARTBEAT_S` / `GET,POST /api/setup/wizard/wifi` |
| `server.py` 常量 | `SETUP_GATE_PATHS` / `SETUP_WRITABLE_KEYS` / `MIN_SETUP_PASSWORD_LEN` / `MIN_WIFI_PSK_LEN` |
| `server.py` 管理页 | `GET /api/manage` / `GET /manage` / `SAFETY_FIELDS` / `_safety_fields`（安全字段改由 `/api/box/status` 提供） |
| `mrrc-setup-ap.service` | 它的 unit heredoc 在 `box-overlay.sh` |

### 重写

- `static/setup.html` —— 从「热点向导」改成「引导页（Wi-Fi + 密码）」。

### 新增

- `box_ops.py`、`linux/box_helper.py`、`packaging/box/` 下的 polkit 规则、`static/settings.html`、`static/box_api.js`。

---

## 12. 打包改动

| 文件 | 改动 |
|---|---|
| `packaging/box/box-overlay.sh` | 删 `mrrc-setup-ap.service` 的 unit heredoc；apt 列表**删 `dnsmasq`**（保留 `dnsmasq-base`，NM 需要）、**加 `avahi-daemon`**；写 `mrrc-box-helper.service` 与 `mrrc-wifi-preseed.service` 两个 unit 并 enable；落 polkit 规则文件；`hostnamectl set-hostname MRRC-Modern`（同步 `/etc/hosts`）；import gate 去掉 `setup_ap`（当前 `import server; …; import first_run, mrrc_radio, setup_ap`） |
| `packaging/box/verify.sh` | 把 setup 热点检查项（`SETUP_UNIT` / `AP_STATE`）换成：**helper 在跑且 socket 权限正确**、**avahi-daemon 在跑**、**polkit 规则文件存在**、**`<盒子名>.local` 可解析** |
| `packaging/rpi/pi-gen-stage4/01-deploy-mrrc/00-run-chroot.sh` | import gate 去掉 `setup_ap` |
| `packaging/box/vendor-mt7663.md` | 补一句：vendored N9 是**工厂 STA 固件**，只解决 STA 稳定性；AP 模式真机验证**失败**（指向本 spec §1） |

---

## 13. 风险与未验证项

> **验证时机（2026-10-11 操作者决定）**：盒子当前**处于生产使用状态**，不接受会打断它的真机操作。
> 因此 **R-1、R-2、R-3、R-4 的真机验证一律推迟到下一次刷机**（届时新镜像本来就要求重刷）。
> 本期的所有改动**只落在仓库与打包脚本里**，不触碰运行中的盒子；效果在下次刷机时一并生效。
> 计划里必须把这几条排在**刷机后的第一件事**，并且在它们有结果之前，不得对外宣称"两条上线路都可用"。

| # | 风险 | 处理 |
|---|---|---|
| **R-1** | **eth0 插网线能不能 DHCP 到地址** —— 整条"网线"路径的地基 | 真机实测（**实现第一步**）。没有它，"两条上线路径"少一条 |
| **R-2** | **polkit 规则能否真的让 `mrrc` 写 NM** | 真机实测，**必须用真服务**（不是 `sudo -u mrrc`）。若失败 → 回退路线甲（helper 承担 nmcli 写，并把密码隐私重新论证） |
| **R-3** | avahi 与已装的 dnsmasq 共存（avahi 5353 / dnsmasq 53） | 真机实测；理论上无冲突 |
| **R-4** | Wi-Fi 切换的回滚时间预算（初值 30s） | 真机标定 |
| **R-5** | eMMC 刷机时没有用户可写的 FAT 分区 | 写进文档，预置 Wi-Fi 只对 SD 成立 |
| **R-6** | 桌面版没有 `nmcli`、没有 helper | 相关面板置灰并说明；`net_wifi`/`box_ops` 的 import 必须成功、调用降级成失败结果（照抄 `net_wifi.py` 现有规矩） |
| **R-7** | `/api/box/*` 与旧 `/api/setup` 的边界漂移，长出第二个密码写入者 | **测试钉住**：断言只有一处写 `MRRC_WEB_PASSWORD` |
| **R-8** | `mrrc-firstboot.service` 的 `After=network-online.target` 与"Wi-Fi 预置让网络上线"互为因果 | `mrrc-wifi-preseed.service` 独立成 unit 并 `Before=mrrc-firstboot.service`；真机验"无网线 + 有预置 Wi-Fi"的开机时序 |

---

## 14. 测试策略

- **`box_ops.py`**：纯逻辑 + 可注入 runner → 单测全铺，不需要 Linux / NM / 电台。覆盖：Wi-Fi 状态机的每条迁移、超时回滚、scrub、helper 不可用时的降级。
- **`tests/test_box_helper.py`**：动词表逐条校验（含非法 hostname、`../` 时区路径、未知动词）、对端 uid 校验、参数不落 argv 拼接。
- **`tests/test_server_box_api.py`**：新端点用 `TestClient`；**首次窗口**单独一组（开/关、子网内/外、窗口内只能写密码、窗口内读端点只有 onboarding）。
- **退役断言**：扫描 `server.py` 与 `net_wifi.py` 不得再出现 `setup_ap` / `hotspot` / `wizard.json` / `SETUP_GATE_PATHS`。
- **打包断言**：`box-overlay.sh` 含 `avahi-daemon` + polkit 规则 + helper unit 且不含 `dnsmasq`；`verify.sh` 检查项齐全。
- **需改的既有测试**：`tests/test_box_profiles.py`（`SetupApPackagingTests`、`VerifyScriptTests`、`test_debians_dnsmasq_unit_is_masked`）、`tests/test_image_imports.py`、`tests/test_sdd_docs_consistency.py`（AD-026）、`tests/README.md`。
- **删除的既有测试**：`tests/test_setup_ap.py`、`tests/test_server_setup_wizard.py`、`tests/test_server_manage.py`；`tests/test_net_wifi.py` 保留并裁到 STA 半边。

---

## 15. 分期

- **第一期**：除「电台配置渲染进 `settings.html`」以外的全部 —— helper + polkit + 两个页面 + 首次窗口 + Wi-Fi 切换 + 改密码 + 名字/mDNS + 网络体检 + 系统面板 + 上线两条路 + 打包与退役。
  完成后主线成立：**插网线 → 打开网页 → 配 Wi-Fi → 拔线 → 用 `<盒子名>.local` 找到它**。
- **第二期**：电台配置统一（把现有 `/api/setup` + `/api/devices` 的控件渲染进 `settings.html`）。单独放，因为它会**同时动应用内 UI 与其测试**，混进第一期会让第一期的验证变脏。

---

## 16. 与旧设计（setup 热点）的对照

| 维度 | 旧（2026-10-08） | 新（本设计） |
|---|---|---|
| 首次可达性 | 盒子开热点，手机连热点 | 网线 DHCP / 预置 Wi-Fi |
| 无密码面的判据 | 热点活着（心跳）**且** 客户端在该热点子网 | 从未设过密码 **且** 客户端在盒子任一子网 |
| 无密码面的写面 | `MRRC_WEB_PASSWORD` + `MRRC_AUTO_PASSWORD` | 同（只有密码这一个键） |
| 提权 | root supervisor `setup_ap.py` 管整条热点生命周期 | polkit 放行 `mrrc` 的 NM + 小 helper 管四个系统动词 |
| Wi-Fi 切换 | AP→STA（把热点拆掉换成 station） | STA→STA（另一条路上访问则完全无感） |
| 页面 | 一套向导页，配置完成后继续当管理页 | 引导页 `/setup` + 设置页 `/settings`，共用后端 |
| 密码怎么走 | `net_wifi` argv，`scrub()` | 同（路线乙的直接理由） |
| 已知硬件风险 | R-1：AP 模式是否可用（**实测失败**） | R-1：eth0 DHCP 是否可用（待测）、R-2：polkit 是否放行（待测） |
