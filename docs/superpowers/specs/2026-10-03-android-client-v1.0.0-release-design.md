# MRRC Modern Android 客户端 v1.0.0 发布 — 设计文档 (Design Spec)

**日期**: 2026-10-03
**状态**: 设计已批准（两节评审通过：功能对齐 / 发布级），待实施
**前置**: `FT710Android/` 现有 Kotlin + Jetpack Compose 客户端（2026-08-16 完成 19 个任务，从未上真机）；服务端 `server.py`（协议权威，当前产品版本 v1.25.0）；`static/modules/ptt_manager.js`（txhb 参照实现）；站点仓库 `~/HAM/website/mrrc_modern/`（zh + en 两份页面）
**SDD 追溯**: AD-007 V2.63（TX 活性闸门）/ SDD/15 §15.6、AD-017（服务端录音）、AD-020（一键 CQ）、SC8（PTT 不可粘滞）、NFR-012（释放安全）、R4（释放命令丢失）
**前序文档**: `docs/superpowers/specs/2026-08-16-ft710-android-app-design.md`、`docs/superpowers/plans/2026-08-16-ft710-android-app.md`

---

## 1. 背景

`FT710Android/` 于 2026-08-16 完成全部 19 个实施任务（控制 / RX+TX 音频 / 频谱 / 记忆频道 / PTT 安全状态机），JVM 测试全绿、debug APK 可构建，但因没有真机，**从未做过设备验收**。此后服务端从 v1.16 演进到 v1.25（一键 CQ、服务端录音、Cloud Hub、PTT 活性闸门……），客户端与之产生了 4 处差距。用户决定一次到位：补齐差距 + 正式签名 + 官网分发，发布 **v1.0.0**，由用户自行从官网下载、真机验收（LAN + Cloud Hub），问题回报后迭代修复（1.0.1）。

工作方式（用户确认）：**我发布 → 用户网站下载自装 → 用户测 → 问题回报 → 我修 → 再发**。

## 2. 目标与非目标

### 2.1 目标

1. 补齐 4 项差距：txhb 发射心跳（安全项）、录音面板 UI、CQ 一键呼叫、品牌与图标
2. 顺带修复只读登录（4003）引发的 TX 通道无限重连
3. 建立正式签名链路（keystore 仓库外保管、release 构建无密钥即失败）
4. 建立一条命令的发布脚本（构建 → 签名校验 → 站点上传 → 线上 SHA-256 复核）
5. 更新站点（zh/en）与仓库文档，产出 v1.0.0 可下载产物
6. 用户执行真机验收清单（LAN + Hub），问题反馈闭环

### 2.2 非目标

- 不做应用内自动升级检查（Android 侧载生态下无意义）
- 不上 Play 商店（无账号；PTT 类应用商店合规成本高）
- 不做 GitHub Release 自动化（官网为准；README 已有 GitHub 入口）
- 不改 iOS 端（`FT710Mobile/`，不在本次范围）
- 不改服务端代码（除验收日临时开启心跳闸门的运行配置）
- 不做混淆/加固（`isMinifyEnabled=false` 保持，避免 crash 反混淆复杂度）

## 3. 功能设计

### 3.1 txhb 发射心跳（安全项）

**协议事实**（逐字核对）：

- 参照实现 `static/modules/ptt_manager.js:25-42`：按键瞬间发**第一次** `{"type":"txhb"}`（不等待 500ms），之后每 **500ms** 一次；TUNE 也启动心跳；释放即停。
- 服务端 `server.py:1768`（`/WSradio` 控制通道消息循环）：收到 `txhb` 即把该连接加入 `_tx_hb_capable` 并刷新 `_tx_hb_last`。
- 闸门（`server.py` `_tx_liveness_timeout`，AD-007 V2.63）：仅在 `MRRC_REMOTE_SESSION_TX_HEARTBEAT_S > 0` 时生效；只对**按键方连接**（`_ptt_key_ws`）且**声明过能力**的连接生效——老客户端永不被误打击。

**实现**：`PTTManager` 进入按键（Keyed）状态时经控制通道立即发一次、其后 500ms 周期发送；`release()` / `forceRelease()` / `onStuckTX` 时停发。沿用可注入 dispatcher，JVM 单测（虚拟时间）验证：0ms 首发、500ms 周期、释放即停、幂等释放。

**TUNE 不接入心跳**（规划期逐行核实）：服务端闸门只跟踪 PTT 键主（`_ptt_key_ws` 仅在 `ptt:true` 分支赋值，`tune` 分支不赋值），TUNE 本就不在闸门保护范围内；Web 的 tune 心跳对该闸门没有效果。客户端不为一个不存在的闸门增加复杂度。

**验收**：服务端临时设 `MRRC_REMOTE_SESSION_TX_HEARTBEAT_S=5`，按住 PTT 后断开手机 WiFi → 5 秒内电台自动回 RX。LAN 与 Hub 两侧分别验证（Hub 侧闸门状态验收日核查）。

### 3.2 录音面板（对齐 Web）

**协议事实**：

- 启停：控制通道 `{"type":"set","field":"recording","value":true|false}`
- 下行：`recordingState`（`{recording:{recording,freq_hz,started_at,duration,name,bytes,dropped}}`）；`fullState.recording` 给快照
- 列表：`GET /api/recordings` → `{recordings:[{name,freq_hz,started_at,duration,bytes,recording}],count,total_bytes}`
- 播放：`GET /api/recordings/{name}`，服务端 FileResponse 支持 Range seek（MP3，16 kHz 单声道）
- 删除：`DELETE /api/recordings/{name}`，录制中 409
- 认证：Cookie `ft710_auth` 或 Bearer，与登录 token 一致

**UI**（Compose，沿用深色琥珀主题）：

- 仪表区加「REC」入口按钮；录制中变红并显示已录时长 → 打开全屏面板
- 面板顶部：●/■ 启停按钮 + 当前录音状态（频率 / 已录时长 / 字节数）
- 列表：频率 + 日期时间、时长、大小；当前录音高亮且不可删
- 点条目 = 播放/暂停（先经 App 的 OkHttp 下载到本地缓存，再交 MediaPlayer 播放 + 进度条 seek）；行内「导出」= 下载完成后经 `FileProvider` 走系统分享（存文件 / 微信 / 发走）
- **规划期修正**：原设想的 DownloadManager / MediaPlayer 直连 URL 在局域网自签证书下必然失败（两者都走系统 TLS 栈，不认自签）。统一改成「OkHttp（接受自签）下载 → 本地播放/导出」，LAN 与 Hub 一条代码路径
- 长按 = 删除（确认框）；提供手动刷新按钮
- 刷新时机：进入面板时 + 每次 `recordingState` 变化 + 手动
- 降级：`fullState` 无 `recording` 字段（服务端 < v1.15）时 REC 入口隐藏

### 3.3 一键 CQ

**协议事实**：`{"type":"set","field":"cq","value":true}` 开始、`false` 中止；下行 `cqState{state,duration_s,elapsed_s,frames_total,frames_sent,started_by,reason,ready}`；拒绝条件（TUNE 中 / 正在发射 / 未验证机型发射门 / 资产不可用）以 `{"type":"error","message":...}` 返回。

**UI**：底部按钮行改为 `TUNE | CQ | PTT`。呼叫中显示进度（elapsed/duration）并再按一次中止；错误以 snackbar 显示服务端原文。降级：`fullState` 无 `cq` 字段（服务端 < v1.17）时按钮隐藏。

### 3.4 品牌、图标、版本、登录页

- 显示名：`FT-710 Control` → **`MRRC Modern`**（`strings.xml`）
- 包名/`applicationId`/`namespace`：**保持 `com.hamradio.ft710android` 不变**——用户不可见，改名要动 27 个源文件 + 全部测试，无用户收益；将来若上商店再迁移
- 图标：现状**没有图标资源**（manifest 未引用，手机显示系统默认图标）。自绘矢量自适应图标：`mipmap-anydpi-v26/ic_launcher.xml` + `ic_launcher_round.xml`（深琥珀底 + 白色电波/天线图形），manifest 补 `android:icon`/`android:roundIcon`。不依赖外部素材（站点无 MRRC logo 可用）
- 版本：`versionName 1.0.0` / `versionCode 1`（从未公开分发过，从 1 起算）
- 设置页显示「客户端 v1.0.0」，供问题回报时报版本
- 登录页主机输入提示补充示例：`192.168.1.50:8888` 或 `bg1sb.mrrc.vlsc.net`（无端口默认 443，兼容 Hub 子域名入口）

### 3.5 只读登录（4003）健壮性

用 `MRRC_LISTEN_PASSWORD` 登录时，服务端对 `/WSaudioTX`、`/WSatr1000` 以 **4003** 关闭、写操作返回错误（WS 只放行 freq/mode/memRecall，REST 非 GET → 403）。规划期核实：App **没有**自动重连（断线只置 Failed，重连仅设置页手动触发），所以问题不是重连风暴，而是**静默失败**——发射类按钮照常显示、点了没反应。

**修复**：`WebSocketConnection` 上抛关闭码；`ConnectionManager` 识别 4003 → 标记 listen-only（「已连接」判据同时剔除 TX 通道，避免只读登录永远显示未连接）；隐藏发射类 UI（PTT/TUNE/CQ/录音启停）并显示「只读登录」提示。

### 3.6 顺带修复两个现有接线缺陷（规划期发现）

1. **连接指示灯永远灰**：`ServiceLocator` 给 `ConnectionManager` 传的 `onConnectionChange` 是空 lambda，`MainViewModel._connected` 除置 false 外无任何写入点——`ConnDot` 永远显示未连接。修复：透传回调 + JVM 单测。
2. **设置页不可达**：`RootScreen.showSettings` 没有任何写入点，`MainScreen` 也没有入口——设置页（含版本显示、重连、退出登录）无法打开。修复：主屏顶栏加设置入口 + `RootScreen` 接线；客户端版本号显示在设置页。

两条都是真机一眼可见的问题，且第 2 条阻塞「版本号用于问题回报」的验收需求。

## 4. 发布设计

### 4.1 签名密钥

- keystore：`~/.android-keystores/mrrc-modern-release.jks`（**仓库外**），别名 `mrrc-modern`，RSA 4096，有效期 10000 天，`keytool` 生成
- `FT710Android/keystore.properties`（**gitignore**）：storeFile/storePassword/keyAlias/keyPassword
- Gradle `signingConfigs.release` 读取该文件；**文件缺失时 release 构建直接失败**，绝不静默产出 debug 签名的"正式版"
- SHA-256 指纹记入 `BUILD_GUIDE.md`；密钥文件 + 口令由用户另行备份（丢失则无法覆盖升级）
- 不改混淆配置

### 4.2 版本与产物

- 产物命名向 Windows/macOS 看齐：`MRRC-Modern-v1.0.0-Android.apk`（版本化）+ `MRRC-Modern-Android.apk`（稳定别名）
- 站点卡片注明：Android 8.0+（minSdk 26）、大小、SHA-256、安装说明（允许未知来源）
- 能力降级矩阵（客户端自动检测，见 §7）
- **不触碰** `downloads/latest.json`（Windows 升级通道专用）

### 4.3 发布脚本 `FT710Android/release.sh`

一条命令，八步：

1. 前置检查（JDK 17 / `ANDROID_HOME` / keystore.properties 存在）
2. `./gradlew test lintDebug assembleRelease`（失败即停）
3. `apksigner verify --print-certs` 核对签名者指纹
4. 产物落 `FT710Android/dist/`（版本名 + 稳定别名）+ 计算 `sha256`
5. 拷贝进本地站点镜像 `~/HAM/website/mrrc_modern/downloads/`
6. 替换 zh/en 页面 `<!-- android-download:start/end -->` 标记块内的版本/大小/SHA/链接；锚点缺失即报错退出（绝不瞎改页面）；重复执行幂等
7. 上传 APK：`scp` 到远端家目录再 `sudo mv` 进 `/var/www/vlsc.net/mrrc_modern/downloads/`（绕开 `/tmp` tmpfs），随后跑网站 `deploy.sh`（部署包排除 `downloads/`，只可能动页面）
8. **线上复核**：`curl` 200 + 大小 + 服务端 `sha256sum` + 下载回来逐字节比对，三处一致——失败即发布失败

支持 `--dry-run`（步骤 1–6，不触碰远端）。

### 4.4 站点与文档

- zh/en 两份页面：Android 卡片去掉"需从源码构建 / 真机射频验收待完成"，换成「下载 APK v1.0.0」+ 安装说明 + SHA-256；功能文案补录音 / CQ / 心跳；iOS 卡片不动
- `mrrc_modern/README.md`：下载区加 Android 行（与 Windows/macOS 并列）
- `FT710Android/CLAUDE.md`：新协议事实（txhb / 录音 REST / CQ / 4003）+ 新模块
- `FT710Android/BUILD_GUIDE.md`：签名、release 构建、`release.sh` 用法、密钥位置与备份
- **新建 `FT710Android/CHANGELOG.md`**：App 自己的版本历史；不往产品 CHANGELOG 顶部塞条目（避免与"产品版本权威"冲突）

## 5. 测试与验收

### 5.1 JVM 单测与构建门槛

- 新增：txhb 调度（虚拟时间：0ms 首发 / 500ms 周期 / 释放即停 / 幂等）、连接状态透传、关闭码 4003 上抛、录音与 CQ 状态解析（含 FullState 能力检测）、RadioState 新字段
- 门槛：`./gradlew test assembleDebug lintDebug assembleRelease` 全绿（无需真机）

### 5.2 服务端侧验证

- 服务端闸门语义已有脚本化测试：`tests/test_tx_liveness.py`（含三类「不该释放」反例——未声明能力的旧客户端、单次丢拍、旁观者超时）。实施阶段运行它作为服务端侧证据。
- 真机端到端（按键 → 断网 → 自动回 RX）并入验收第 11 项：验收窗口内在本机实例临时置 `MRRC_REMOTE_SESSION_TX_HEARTBEAT_S=5` 并重启；验完恢复或按 Hub 需要保留。

### 5.3 真机验收清单（用户执行，我待命修）

| # | 项目 | 要点 |
| --- | --- | --- |
| 1 | 官网下载 + 校验 | 文件名 / 大小 / SHA-256 与页面一致 |
| 2 | 安装 | 允许未知来源；启动器名 = MRRC Modern，图标正常 |
| 3 | LAN 登录 | `192.168.x.x:8888` 自签证书；登录成功 |
| 4 | 控制 | 频率/步进/VFO/模式/滤波/ATT/PRE/DSP 开关/记忆频道存取 |
| 5 | RX 音频 | 扬声器出声、无明显卡顿 |
| 6 | TX + PTT | 按住讲话（功率/SWR 表动），松手即回 RX；退后台立即回 RX |
| 7 | 频谱 | 瀑布滚动、FFT 曲线、S 表 |
| 8 | 录音 | 启→停→列表→播放/拖进度→保存到手机→删除 |
| 9 | CQ | 点按播放、进度显示、再按中止 |
| 10 | Hub 远程 | `<呼号>.mrrc.vlsc.net` 登录，重跑 4–9 |
| 11 | txhb 断网释放 | 按住 PTT 断 WiFi → 电台数秒内自动回 RX（LAN 临时开闸门；Hub 侧一并核查） |
| 12 | 问题回报 | 截图 + 设置页客户端版本 + LAN/Hub + 现象步骤；据此发 1.0.1 |

## 6. 风险与缓解

| # | 风险 | 影响 | 缓解 |
| --- | --- | --- | --- |
| R1 | 基座（音频/PTT/Opus/TLS）从未真机验证，首轮可能暴露多处设备级问题 | 首版体验与问题分拣成本 | 用户已接受"发布后验收、问题回报"；JVM + 服务端侧先尽力；1.0.1 迭代通道（release.sh 幂等 + versionCode 递增）已备好 |
| R2 | keystore 丢失 | 无法覆盖升级 | 仓库外生成；提示用户立即备份文件+口令；当前无存量用户，代价可控 |
| R3 | 线上实例心跳闸门未开启（当前 env 未设 `MRRC_REMOTE_SESSION_TX_HEARTBEAT_S`） | 验收第 11 项无法验证 | 验收日临时加变量并重启服务端；验完恢复或按 Hub 需要保留；Hub 侧闸门状态验收日核查 |
| R4 | 站点上传与部署互相破坏 | 产物 404 / 页面回退 | 已核实：部署包与备份均排除 `downloads/`；上传走 `scp → ~ → sudo mv` 绕开 `/tmp` tmpfs；发布脚本第 8 步线上复核兜底 |
| R5 | 应用无图标素材 | 首版观感差 | 自绘矢量自适应图标（§3.4），不找外部素材 |
| R6 | 仓库被多个会话并行推进 | 提交冲突 | 提交前看 `git log -5`；小步提交；只碰本任务文件 |

## 7. 能力降级矩阵（服务端版本 × 客户端功能）

| 服务端 | 客户端行为 |
| --- | --- |
| < v1.15（无 `recording`） | REC 入口隐藏 |
| < v1.17（无 `cq`） | CQ 按钮隐藏 |
| < v1.22（无 txhb 闸门服务端） | 心跳照发（无副作用），闸门由服务端决定是否生效 |
| 只读口令登录（4003） | 停止 TX 重连、隐藏发射类 UI、显示只读提示 |

## 8. 实施顺序（供实施计划展开）

1. 基线证明：确认现有代码在当机工具链下 `test assembleDebug` 全绿
2. txhb 心跳 + JVM 单测
3. 只读登录（4003）处理 + 连接指示/设置页接线修复（§3.6）
4. 录音面板
5. CQ 一键呼叫
6. 品牌 / 图标 / 版本 / 登录页提示
7. 签名链路（keystore + Gradle + 无密钥即失败）
8. `release.sh` + 站点标记块 + 页面/README/CLAUDE/BUILD_GUIDE/CHANGELOG 更新
9. 服务端侧脚本验证（txhb 闸门）
10. 发布 v1.0.0 → 线上复核 → 交付验收清单给用户
11. 用户真机验收（LAN + Hub）→ 问题回报 → 1.0.1

## 9. 追溯表

| 设计项 | SDD/依据 |
| --- | --- |
| txhb 心跳 | AD-007 V2.63、SDD/15 §15.6、`ptt_manager.js:25-42`、`server.py:1768` |
| PTT 安全不退化 | SC8、NFR-012、R4、UC-005 |
| 录音面板 | AD-017、`server.py` recordings API、`recorder.py:list_recordings` |
| CQ | AD-020、`cq_player.py:status` |
| 发布/分发 | 前序设计 §2「分发」决策（签名 APK 侧载 + 官网下载） |
| 站点事实 | `~/HAM/website/mrrc_modern/deploy.sh`（downloads 排除）、`downloads/latest.json`（Windows 专用，勿动） |
