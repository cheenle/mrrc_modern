# macOS 安装与使用指南（MRRC Modern v1.25.5）

本指南面向**第一次使用 MRRC Modern 的 macOS 用户**：从下载到出声，全程**不需要打开终端、不需要编辑配置文件**。
只有「故障排查」和「高级用法」两章需要命令行，且都是可选。

> **本版（v1.25.5）到的是三件事**：① 主界面快捷行从「显示下一个」改成**显示当前值** ——
> `MODE` / `BAND` / `FILTER` 显示当前模式、波段、滤波带宽，`ATT` / `PRE` 显示完整档位
> （`OFF / 6dB / 12dB / 18dB`、`OFF / AMP1 / AMP2`），点击仍然轮转到下一档（语义与 Android 客户端一致）；
> **装完第一次打开如果还看到旧标签，强刷一次（⌘⇧R）**。② 频谱带宽成为每连接可协商的档位
> （设置里可选 full / mid / low；payload 151 → 38 → 19 kbps；未声明能力的连接逐字节等于改动前的流）。
> ③ 配置文件的写入收敛到**唯一写者** `env_store.py`（文件锁 + 原子替换、保注释保序、不动 mode 与 owner），
> 读取只有一个容错读者 —— 这修的是「设置页显示已连接、而服务端读不到证书路径与心跳」那一类静默失配。
> 另有新增的只读页 `/manage`（热点是否在跑 + 三个安全闸门当前值）。
> **只在 macOS 上修过的那条隧道泄漏（v1.25.4）仍随本包生效**，本版未再改动它。
>
> **上一版（v1.25.3）修的是服务端的 PTT 存活闸门**（Cloud Hub 打开的那个）：它只应释放不再证明自己
> 活着的发射会话，但**按键时发的心跳要等按键处理（CAT 写 + 起音频，几十毫秒）返回后才能读到** ——
> 落在这段窗口里的检查会把**刚按下的健康一键**当成静默会话放开（一份现场日志里记了 7 次，每次都在
> 按键后 20–70 ms）。按键本身即活着的证明（刷新存活时钟），只有真正停止心跳的会话才被释放；
> 从未声明过该能力的会话永不被门控。
>
> **上一版（v1.25.2）的改动全在 Windows 侧**（隧道每启动一次就漏一个进程、两个启动器会各起一个服务器），
> **macOS 行为不变**。
>
> **v1.25.1 在 macOS 上修了两个「只有构建机看不见」的打包缺陷** —— 它们只在用户机器上现形，
> 源码运行与 Windows 都不受影响：
>
> **① 出站 HTTPS 校验不了证书。** 安装包把构建机的 OpenSSL 一起带走了，而它的默认 CA 路径只存在于
> 构建机（`/opt/local/...`）—— 于是信任库为空：**接入云端**、**🐞 诊断包上传**、**软件更新检查**
> 全部报 `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`（那份报告包之所以要人工
> 递过来，就是这个原因）。现在应用随包带自己的 CA（`vendor/ca/cacert.pem`），默认 store 为空时依次回退
> 到平台系统 CA；构建闸门会**故意把信任库打空**再要求真握手一次，通不过不许发版。
>
> **② 真频谱找不到 FT-4222 动态库**，界面一直画 S 表合成曲线。启动器把配置里的相对路径
> `vendor/ftdi/macos` 解析到了 `Contents/MacOS`（那里什么都没有），而签名后的包把数据树放在
> `Contents/Resources` —— 采集进程直接以 `FTDI libraries not found` 退出。已修，并补了签名包目录结构的
> 回归测试。
>
> 再往前：v1.25.0 修「装完黑屏 / 第二个实例抢端口 / 往 Program Files 写东西 / 日志写两遍」，
> v1.24.5 修「缺证书就自己签一张，而不是悄悄退回 HTTP」，v1.24.1 修「打包版按『申请』即 500」
> （`server.py` 从未导入 `cloud_hub`）。历史细节见 `CHANGELOG` 与 `SDD/14` V2.65–V2.70。
>
> 安装层面的两个历史问题已在 **v1.18.1** 修好：应用报「已损坏，无法打开」，以及装好后**接收完全没声音**。
> 两者都在打包层面，详见第 3、4 章。**v1.18.0 及更早的 macOS 包请勿使用**。

---

## 0. 三句话版本（TL;DR）

1. 下载 DMG → 把 **MRRC Modern** 拖进「应用程序」。
2. **右键 → 打开**（第一次需要这样）。
3. 弹窗问**麦克风**时点 **允许** —— 这是接收电台音频，不是录音。

---

## 1. 下载与系统要求

| 项目 | 要求 |
| --- | --- |
| 系统 | **macOS 11 (Big Sur) 或更新** |
| 芯片 | **Apple 芯片（M1/M2/M3/M4…）**；安装包为 `arm64` 构建，**Intel Mac 不受支持** |
| 磁盘 | 约 150 MB（应用本身 ~56 MB DMG，安装后展开） |
| 硬件 | 电台的 USB 线（FT-710 / Icom 系列 / Yaesu 实验机型，见第 6 章） |
| 权限 | **麦克风**（= 音频输入，必需）；局域网访问时还需要 macOS 防火墙放行 |

下载地址：<https://www.vlsc.net/mrrc_modern/> → **Download macOS**，文件名为
`MRRC-Modern-v1.25.5-arm64.dmg`（62,490,425 bytes，SHA-256 `58ad7481…`）。

**校验下载完整性（可选，命令行）**：

```bash
shasum -a 256 ~/Downloads/MRRC-Modern-v1.25.5-arm64.dmg
```

与官网下载卡片上的 SHA-256 一致即可。

---

## 2. 安装（约 1 分钟）

1. 双击打开 `MRRC-Modern-v1.25.5-arm64.dmg`。
2. 把 **MRRC Modern** 图标拖进右侧的**应用程序**文件夹快捷方式。
3. 在访达侧边栏弹出该磁盘映像（点 ⏏）。
4. 到「应用程序」里找到 **MRRC Modern**。

---

## 3. 首次打开：两种弹窗必须分清

macOS 会对"不是从 App Store 下载、也没有付费开发者签名"的应用做检查。你会遇到下面**两种之一**，
它们的含义和处置**完全不同**：

| 弹窗文字 | 含义 | 怎么做 |
| --- | --- | --- |
| **「无法打开，因为 Apple 无法检查其是否包含恶意软件」**<br>或「无法验证开发者」 | **正常**。本应用使用 ad-hoc（自签）签名，不是付费 Developer ID 签名，所以系统"不认识"它 | 在「应用程序」里**右键点击 MRRC Modern → 打开**，再点**打开**。**只需一次**，之后双击即可 |
| **「已损坏，无法打开。你应该将它移到废纸篓」** | **不正常**：应用签名无效（旧版本包的已知缺陷），这个提示**右键也绕不过去** | ① 先确认下载的是 **v1.18.1 或更新**（v1.18.0 及更早都有此缺陷，**必须重下**）；② 若已是新版仍报此错，多半是下载被中断或文件被改动，重新下载并用第 1 章的 SHA-256 校验；③ 应急解封见下 |

**应急解封（仅当你已明知旧包、且暂时无法下载新版）**：

```bash
# 移除"来自互联网"的隔离标记，系统便不再对它做启动检查
sudo xattr -dr com.apple.quarantine "/Applications/MRRC-Modern.app"
```

> 这条命令**只对信任来源的文件使用**。它不修复旧包的权限缺陷 —— 用旧包时接收仍然没有声音（见第 4 章），
> 因此正确做法始终是**换新版**。

**想确认一个包到底签得对不对（高级）**：

```bash
codesign --verify --verbose=2 "/Applications/MRRC-Modern.app"   # 应输出 valid on disk
spctl -a -vvv -t exec "/Applications/MRRC-Modern.app"           # 只应报"无 Developer ID"，不应报 damaged
```

---

## 4. 麦克风权限（**最重要的一章**）

### 为什么要它

应用要把**电台收到的声音**送到浏览器播放，因此必须读取电台 USB 声卡的**音频输入**。
macOS 把「音频输入」统一归入**麦克风权限**类（不是只有麦克风才需要它），所以：

- 首次启动会弹一次询问，标题类似「"MRRC Modern"想要访问麦克风」→ 请点 **允许**。
- 若当时点了「不允许」，或想补授：**系统设置 → 隐私与安全性 → 麦克风** → 打开 **MRRC Modern** 的开关。

### 拒绝授权的症状（很隐蔽，务必记住）

| 你会看到 | 实际上 |
| --- | --- |
| 频率、频谱、PTT 一切正常 | 控制链路与权限无关 |
| 网页里打开「远程接收」**完全没有声音** | 音频输入被系统屏蔽 |
| 应用日志写着 `RX audio started: … USB Audio Device … 44100Hz` | **流确实"打开成功"** |
| 日志 `RX audio is near-silent (peak=0.0%)` | CoreAudio 允许打开流、但把数据**全部填零**，且**不报任何错误** |

也就是说，macOS 对"没授权"的回应不是报错，而是**沉默**。v1.18.1 起，日志的这条警告会直接提示你去
**系统设置 → 隐私与安全性 → 麦克风**，不再只让你"检查电台 AF 增益"。

> **每次升级都会重新询问一次权限。** 本应用是 ad-hoc 签名，macOS 把授权绑定到"这一次构建"上，
> 因此新版本首次启动会再问一次麦克风 —— 这是正常现象，**点允许即可**（要免除需付费 Developer ID 签名）。

---

## 5. 登录（零配置）

1. 首次启动后，菜单栏（屏幕右上角）出现 **MRRC Modern** 图标，浏览器自动打开登录页。
2. 登录页顶部有橙色提示：**「首次运行已自动生成密码：XXXX」** —— 输入它即可登录。
3. 忘了密码：菜单栏图标 → **Show Password…**。

服务默认监听 **8888** 端口（菜单栏图标会显示 `MRRC Modern :8888`）。

---

## 6. 连接电台

| 机型 | 状态 | 说明 |
| --- | --- | --- |
| **FT-710** | 完整支持 | USB 插上即自动识别；**真 FFT 频谱**开箱即用 |
| **IC-7300 / IC-7300MK2** | 完整支持 | USB 即插即用；频谱走 CI-V |
| **IC-705 / IC-7610 / IC-7760** | 预览 | 接收可用；**发射默认被拒绝**，核对真机后设 `MRRC_ALLOW_UNVERIFIED_TX=1` |
| **Yaesu FTDX10 / FTDX101D / FTDX101MP / FTX-1F / FT-891** | 实验性 | 串口 ASCII-CAT（驱动同 FT-710）；默认只收不发；无真机频谱源（界面显示 S 表合成频谱）。**FT-891 例外**：USB 口只做 CAT、无 USB 声卡（音频须外接接口到 DATA/ACC 口），且无内置天调（不显示 ATU 键） |

同时插了多个串口设备时，程序优先选 CP210x/USB 串口；可在菜单栏 **Edit Configuration…** 里确认
`MRRC_SERIAL_PORT`。

macOS 自带 CP210x 驱动，插上电台即出现 `/dev/cu.usbserial-*`，**一般无需装任何驱动**。仅当串口列表里
找不到电台时，才安装 Silicon Labs CP210x 驱动（本站镜像：<https://www.vlsc.net/mrrc_modern/downloads/SiLabsUSBDriverDisk.dmg>；
安装前拔掉电台 USB 线，装完插回；10.13+ 若扩展被拦截到「隐私与安全性」放行）。FT-710 频谱兑底：
FTDI D2XX 库镜像 <https://www.vlsc.net/mrrc_modern/downloads/D2XX1.4.35.dmg>（安装见 §13）。

---

## 7. 菜单栏与网页端功能

**菜单栏图标**（随时可用）：

| 菜单项 | 作用 |
| --- | --- |
| **Open Web UI** | 重新打开控制页面 |
| **Edit Configuration…** | 图形化改配置（电台型号、串口、音频设备、密码），改完点 **Restart Server** |
| **Show Password…** | 查看自动生成的登录密码 |
| **Restart Server** | 重启后台服务（改配置后、或串口/音频卡死时用） |
| **Quit MRRC Modern** | 退出（**先松开 PTT**） |

**网页端菜单**：包含 **🐞 遇到问题** —— 见第 9 章。

---

## 8. 数据与文件位置

| 位置 | 内容 |
| --- | --- |
| `/Applications/MRRC-Modern.app` | 应用本体 |
| `~/Library/Application Support/MRRC-Modern/mrrc_modern.env` | 你的配置（密码、串口、音频设备、`MRRC_*` 变量） |
| `~/Library/Application Support/MRRC-Modern/logs/server.log` | **服务端日志**（轮转） |
| `~/Library/Application Support/MRRC-Modern/logs/server-stdout.log` | 启动窗口日志（服务端日志系统起来之前的部分） |
| `~/Library/Application Support/MRRC-Modern/certs/` | 自签名 HTTPS 证书与私钥（仅本机使用） |
| `~/Library/Application Support/MRRC-Modern/recordings/` | 服务端 QSO 录音（MP3）与索引 |
| `~/Library/Application Support/MRRC-Modern/mem_channels.json` | 记忆频道 |
| `~/Library/Application Support/MRRC-Modern/atr1000_tuner.json` | 天调学习值（启用 ATR1000 时） |

> 这些文件都在你的用户目录里，**卸载时不会自动删除**（见第 12 章）。

---

## 9. 日志与诊断包

**报告问题的最快路径**：网页端菜单 → **🐞 遇到问题** → 写下现象 → **生成诊断包** →
**上传给维护者**（会得到一个编号），或**只保存到本地**（脱网机器走这条）。

诊断包由**服务端**生成，包含：日志、脱敏后的配置、环境/电台/音频状态快照、浏览器上下文，
以及一份 `diagnostics/summary.txt`（**先看它** —— 自动体检结论）。

**包里永远不会包含**：登录密码、证书私钥、任何令牌、录音、记忆频道、天调学习值。

**想自己看日志**（命令行，可选）：

```bash
tail -n 80 ~/Library/Application\ Support/MRRC-Modern/logs/server.log
grep -i "audio\|permission\|silent" ~/Library/Application\ Support/MRRC-Modern/logs/server.log | tail -20
```

---

## 10. 升级 / 回退

- **升级方式：手动换包**。下载新版 DMG → 按第 2 章覆盖安装进「应用程序」。配置、录音、记忆频道都在用户目录，**不会丢失**。
- **应用内暂不提供一键升级**：当前版本已能查询"是否有新版本"（`GET /api/update/check`，只读接口），
  但**界面尚未展示**，下载与自动安装属于后续开发。
- 升级后**首次启动会再问一次麦克风权限**（原因见第 4 章）。
- 回退：重新安装旧版 DMG 即可。**但不要回退到 v1.18.0 或更早** —— 那些包签名无效（第 3 章），
  且缺少音频输入权限说明键（第 4 章），装了也没有声音。

---

## 11. 局域网 / 手机访问 + HTTPS

- 手机、平板在同一 Wi-Fi 下访问 `https://<Mac 的 IP>:8888`（菜单栏会显示地址）。
- 首次访问浏览器会警告**证书不受信任** —— 这是本机自签证书的正常现象，选择「继续访问」即可；
  也可以在 **Edit Configuration…** 里换成你自己的证书。
- macOS 防火墙若拦截，到 **系统设置 → 网络 → 防火墙** 允许 **MRRC Modern** 的传入连接。
- 想临时关掉 HTTPS（仅调试）：在 `mrrc_modern.env` 里设 `MRRC_SSL=off` 后 **Restart Server**。

---

## 11.5 接入 Cloud Hub（实例侧，可选）

本应用支持把电台通过隧道发布到云端入口（`https://<呼号>.mrrc.vlsc.net/`），客户侧**不需要公网 IP**。
安装包里已经带好了所需的一切，无需另装 frpc 或 openssl：

1. 打开应用（保持运行）。
2. 在访达里右键 `MRRC-Modern.app` → **显示包内容** → `Contents/Resources/payload/`。
   里面有 `frpc`、四个脚本和一份 `README.txt`。
3. 在该目录执行（把尖括号部分换成运维给的呼号、端口与**一次性登记口令**；令牌见运维）：

   ```bash
   MRRC_HUB_TOKEN=<frps 令牌> MRRC_ENROLL_SECRET=<一次性登记口令> \
     ./install_instance_tunnel.sh <呼号> <端口>
   ```

   脚本会：签一张签给 `<<呼号>>.mrrc.vlsc.net` 的证书（私钥不出本机）→ 把它登记到 hub →
   把应用侧环境（证书路径、端口、心跳）写到 `~/.config/mrrc/env.sh` 与 `environment.d`/LaunchAgent →
   装一个 launchd 常驻隧道。
4. 脚本最后会打印一条 **root 命令**，交给运维在 hub 上执行（生成路由 + reload nginx）。
5. 完成后访问 `https://<呼号>.mrrc.vlsc.net/` 应看到登录页（首次访问自签证书，浏览器会
   提示一次，点"继续"即可）。

脚本**幂等**：中途失败（例如 hub 暂时不可达）重跑同一条命令即可；证书已在磁盘上时会复用。

## 12. 卸载

1. 菜单栏图标 → **Quit MRRC Modern**。
2. 把「应用程序」里的 **MRRC Modern** 拖进废纸篓。
3. 删除数据（**会丢配置、录音、记忆频道**，请先备份需要的内容）：

```bash
rm -rf ~/Library/Application\ Support/MRRC-Modern
```

1. 清掉系统里的麦克风授权记录（可选，重装后系统会重新询问）：

```bash
tccutil reset Microphone net.vlsc.mrrc-modern
```

---

## 13. 故障排查

| 现象 | 原因 / 解决 |
| --- | --- |
| 「**已损坏，无法打开**」（右键也无效） | 旧版包签名无效 → **下载 v1.18.1+**（第 3 章）；新版仍报则重新下载并校验 SHA-256 |
| 「无法验证开发者 / 无法检查是否包含恶意软件」 | 正常，**右键 → 打开** 一次即可 |
| 控件正常但**接收没有声音** | ① **麦克风权限**（第 4 章，最常见）；② 电台 AF 增益太低；③ 音频设备选错 → Edit Configuration… |
| 权限已经允许，还是没声音 | 日志里若仍 `peak=0.0%`：确认选中的是电台 USB 声卡（`USB Audio Device`），并在 Edit Configuration… 里重新指定 RX 设备后 **Restart Server** |
| **发射没有声音** | 麦克风权限（浏览器侧还需允许网页使用麦克风）；确认电台处于正确模式与功率 |
| 每次升级都问麦克风权限 | 正常（ad-hoc 签名，第 4 章） |
| 系统设置里**找不到 MRRC Modern**（麦克风列表） | 说明当前运行的包缺少权限说明键（v1.18.0 及更早）→ 换新版 |
| 菜单栏没有图标 | 应用没启动成功：`open -a "MRRC Modern"` 重开；仍无则查 `logs/server-stdout.log` |
| 登录页打不开 | 端口被占用或服务未起：菜单栏 **Restart Server**；或在配置里换 `MRRC_WEB_PORT` |
| 频谱是假的（S 表合成） | 该机型无真机频谱源；FT-710 需确认 `MRRC_FTDI_LIB_DIR=vendor/ftdi/macos` 且使用 v1.18.1+；仍不行时安装 FTDI D2XX 库（镜像 [D2XX1.4.35.dmg](https://www.vlsc.net/mrrc_modern/downloads/D2XX1.4.35.dmg)，SHA-256 `208ea2d6…6655c`）：`sudo cp /Volumes/dmg/release/build/libftd2xx.1.4.35.dylib /usr/local/lib/` + `sudo ln -sf /usr/local/lib/libftd2xx.1.4.35.dylib /usr/local/lib/libftd2xx.dylib`，然后菜单栏 **Restart Server** |
| 串口找不到 / 电台没反应 | 确认 USB 已插；Edit Configuration… 看 `MRRC_SERIAL_PORT`（同时插多个串口时可能选错）；仍不行时安装 Silicon Labs CP210x 驱动（镜像 [SiLabsUSBDriverDisk.dmg](https://www.vlsc.net/mrrc_modern/downloads/SiLabsUSBDriverDisk.dmg)，SHA-256 `0b1d6857…bf2eda`；**安装前拔掉电台**，装完插回） |
| 首次启动卡几秒 | 系统对未认证应用做一次扫描，正常 |
| Intel Mac 上打不开 | **不支持**：安装包是 arm64 构建 |

---

## 14. 安全与隐私

- **登录密码**自动生成，只存在本机 `mrrc_modern.env`；网页端所有连接（含 WebSocket）都需要它。
- **HTTPS 默认开启**，证书是本机自签（首次浏览器会警告，属正常）。
- **诊断包已脱敏**：密码、私钥、令牌不进入包；上传是可选项，也可只保存到本地。
- **录音**存在你的用户目录，不会自动上传。
- **权限只有麦克风**（音频输入）一项，用于接收电台声音；应用不申请摄像头、通讯录、屏幕录制等权限。

---

## 15. 高级：命令行与环境变量（可选）

```bash
# 手动启动后台服务（调试用；正常使用请从「应用程序」双击）
"/Applications/MRRC-Modern.app/Contents/MacOS/MRRC-Modern-Server"

# 临时覆盖配置（不改配置文件）
MRRC_RADIO_MODEL=ic7300 MRRC_SERIAL_PORT=/dev/cu.usbserial-XXXX \
MRRC_WEB_PORT=8888 MRRC_SSL=off \
"/Applications/MRRC-Modern.app/Contents/MacOS/MRRC-Modern-Server"
```

常用变量：`MRRC_RADIO_MODEL`、`MRRC_SERIAL_PORT`、`MRRC_BAUD_RATE`、`MRRC_WEB_PORT`、
`MRRC_WEB_PASSWORD`、`MRRC_AUDIO_RX_DEVICE`、`MRRC_AUDIO_TX_DEVICE`、`MRRC_RECORDINGS_DIR`、
`MRRC_CQ_FILE`、`MRRC_ALLOW_UNVERIFIED_TX`、`MRRC_SSL`（`off` 关闭 HTTPS）、
`MRRC_LOG_DIR`。完整清单见 `README.md` 与 `docs/OPERATION_GUIDE.md`。

---

## 相关文档

- 网页版操作手册（含界面截图说明）：`docs/OPERATION_GUIDE.md`
- 打包/复现构建（开发者）：`mac_pack.md`
- 故障的深度诊断与发布记录：`SDD/14-version-history.md`
- 问题上报与答复：官网 **/mrrc_modern/answers/** 与网页端 **🐞 遇到问题**
