# Changelog

All notable changes to the MRRC Web Control project.

## [Unreleased] — 2026-10-26 — 盒子级音频电平：TX 麦克风与 RX 下发各一个 dB 旋钮（AD-032）

**触发**：现场两个抱怨其实是同一件事 —— 电平没有主人。FT-710 的 USB 音频偏轻，网页端的
`AUDIO_GAIN_BOOST = 10.0` 拉满就是上限，再往上没有旋钮；而麦克风的软件增益归**浏览器**
（🎙 Vol，cookie，0–200 线性 = 0…+6 dB，一台设备一份），偏轻的耳机麦克风加 6 dB 也不够，
手机与电脑还各是一个声音。

**改了什么**（两个数字，各只有一个所有者）：

- **盒子级 TX 麦克风增益**（−20…+25 dB，默认 0），只作用于 `_feed_tx_from_uplink` 的麦克风上行：
  CQ 播放与 FT8 波形不经过它（它们的电平不该被“人声”这个旋钮带着走）。TX 录音 tap 移进同一函数
  并放在增益之后 —— 录下来的是**电台实际收到的信号**，也顺手统一了三种上行格式（此前只有 Opus
  那支被录进 TX 录音）。
- **盒子级 RX 下发电平**（−20…+30 dB，默认 0），只加在 `encode_rx_audio()` 之前：录音、FT8 解码、
  盒子本地扬声器全部拿原生电平。一个软件增益不能把电台没发出来的信息变出来，把解码链路顶到削顶
  只会换来解不出的信号。
- **🎙 Vol 上限 2×（+6 dB）→ +25 dB**，滑块改成 dB（−20…+25，0 dB = 原样），旧 cookie
  `ft710_micVol`（0–200 线性）一次性迁移。
- 网页端 🔊 的 10× boost **一个字节都没动**：两个盒子电平默认 0 dB，所以 day-one 的流与按钮前
  逐字节相同，安卓 / iOS / listen / SDR 四端不需要同版本发布。代价是 **盒子 RX 电平与浏览器 🔊
  相乘**，这句话写在卡片上。

**接口**：`GET/POST /api/audio/levels`（部分更新；越界/非数字/未知键 **400 + 允许区间**，不静默
夹取），落盘 `audio-levels.json`（原子写，`MRRC_AUDIO_LEVELS_FILE` 可覆盖），变更后广播
`{"type":"audioLevels",…}`、`fullState` 带同一形状。削顶按方向计数，比例 >1% 时每 30 s 最多
一条警告 —— 是盒子把信号顶掉的，要让日志说出来。

**入口**：☰ 菜单「音频电平（盒子）」两条滑块（别的设备改了这里跟着动）、`settings.html` 的
「音频电平」卡片（带复位与三句说明）、电台参数弹窗里的 🎙 Vol。

测试 2945（+46：`test_audio_levels.py` 29、`test_audio_levels_server.py` 17），模块 152；
磁盘上的数字由 `tests/test_spectrum_profile_docs.py` 与 `tests/README.md` 对账。

## [Unreleased] — 2026-10-25 — 已发货盒子的 DNS 修复：Windows 一条命令

**触发**：现场两台盒子「连不上门户」，其中一台（MRRC-BG1SB）的真因是 `/etc/resolv.conf`
指向**做镜像那台机器**的 DNS（Docker Desktop 的 `192.168.65.7`），在任何客户网络上都不存在：
glibc 每次解析撞 5 秒超时后失败，而 `ping <IP>` 一切正常。修它要 root，可客户多半按指南改过
root 口令 —— 所以修复不能靠 root 口令。

**新增三条通路，共用一个脚本**（`linux/fix_resolv_conf.sh`，幂等、备份为
`/etc/resolv.conf.mrrc-bak`、并重启 resolved 以清掉它从旧文件吸进全局列表的那条死地址）：

- **root 助手新动词** `fix-resolv-conf` → `POST /api/box/fix-dns`（`box_ops.fix_dns`）：只要
  **网页登录口令**，不需要 SSH / root；只认常量脚本路径、不收参数，`OUTPUT_VERBS` 把脚本输出
  原样带回（"改了什么"是操作员唯一能核对的证据）。
- **`dev_tools/mrrc-fix-dns.cmd`**（另有站上副本 `website/downloads/MRRC-FixDNS.cmd`）：
  双击 → 输 IP 与网页口令 → 打印查到的旧内容与新链接 → **再跑一遍证明已幂等**
  （第二遍必须出现 `already a symlink`）。比这个修复早的盒子没有该接口，脚本给两条出路
  （先升级，升级自己会修；或有 root 口令手工三行）。
- **指南页** `website/fix-dns.html` + `website/zh/fix-dns.html`，并进 W103D 指南索引的两个
  页面（表格一行 + 一张卡片）；`docs/W103D_FAQ.md` 的 DNS 一节交叉链接过去。
  顺手把 zh 索引里缺的那条「常见问题」也补上（它落后了上一版）。

测试 2894（+17：`test_fix_dns_cmd.py` 10、`test_box_helper.py` 3、`test_server_box_actions.py` 3、
`test_box_update_cmd.py` 的固定拷贝对 1 项），模块 150。

## [Unreleased] — 2026-10-25 — 远程协助面板显示「端口」

**触发**：现场开了一个协助窗口，面板上只有 用户名 / 口令 / 剩余 / 已连接 —— 支持者要先问
「端口是多少」才能连（`ssh hub -W 127.0.0.1:<port>`），而这是唯一猜不到的字段。
`/api/help/status` 一直就返回 `port`（`help_session.status()`），是面板没渲染。

**改了什么**：`static/support.html` 的协助卡片加一行「端口」（未分配时显示 `—` 而不是 `0`），
「复制口令」改成「复制连接信息」，一次复制 用户名 / 口令 / 端口 三行；接口未改动。
测试 +4（`tests/test_support_frontend.py::HelpPanelTests`，含「`0` 不许当成端口」与
「口令仍然只作为文本渲染、不拼进链接」）。

## [Unreleased] — 2026-10-25 — 盒子出厂默认带两条备用 DNS（114.114.114.114 / 8.8.8.8）

**触发**：同一天两台盒子报「连不上门户」，其中一台的真因是**系统时间慢了 24 天**
（`certificate is not yet valid`）。往下追一层：开机时 Wi-Fi 比 DHCP 早，`/etc/resolv.conf`
是空的 → chrony 解析不到 `pool 2.debian.pool.ntp.org` → 时钟停在镜像日期 → 之后所有 HTTPS
都失败。DNS 是这条链的根，所以把它补成**出厂默认**。

**改了什么**：

- `net_wifi.FALLBACK_DNS`（`114.114.114.114`、`8.8.8.8`）：建 profile 时写入
  （`write_profile`，新连的网一次到位）；已保存的 profile 由 `ensure_fallback_dns()` 在**每次
  启动**补一次（幂等，只在真的改了什么时记一行 INFO）——这是**升级路径**，已经发出去的盒子
  重启后自动获得。**操作员自己配的 DNS 不覆盖**，备用项追加在后面；`ipv4.ignore-auto-dns`
  保持 `no`，路由器下发的 DNS 照旧可用。
- `packaging/common/preflash.sh`：救急 Wi-Fi profile 与**基底自带**的每个 `.nmconnection`
  在构建期补上（`MRRC_PREFLASH_FALLBACK_DNS` 可覆盖这两条）。
- 测试 2852 → **2865**（`tests/test_net_wifi.py` +7、`tests/test_preflash.py` +3、新增
  `tests/test_server_boot_dns.py` 3），模块 147 → **148**；`tests/README.md` 的总数与模块数
  顺手对齐到实测值（原来写着 2819 / 145，已经漂了 33 个用例）。

**顺带挖到的、更靠底的一层**（2026-10-25 现场复验，BG7SB）：镜像构建时为了让 chroot 里的
apt/pip 能解析，`packaging/box/build-image.sh` 把**容器的 `/etc/resolv.conf`** 拷进了 rootfs，
而基底原本是 `-> /run/systemd/resolve/stub-resolv.conf` 的符号链接 —— 这份拷贝留在了成品里。
后果实测：盒子上 `resolvectl status` 报 **`resolv.conf mode: foreign`**，文件里只有一条
`nameserver`，而 `resolvectl dns wlan0` 已经有 `114.114.114.114 8.8.8.8 192.168.31.1` 三条；
glibc 读的是那个文件，所以**任何写进网络 profile 的 `ipv4.dns` 都是空转**。已在构建期还原符号
链接（`--preflash` 与普通镜像都走这条）。

**存量盒子怎么救**（新文件 `linux/fix_resolv_conf.sh`，由 `linux/mrrc_update.sh` 以 root 调用）：
幂等、可单跑（`sudo bash /opt/mrrc_modern/linux/fix_resolv_conf.sh [--dry-run]`）、原文件备份为
`/etc/resolv.conf.mrrc-bak`，没有 systemd-resolved 的机器一律不动。**它多做了一步**：换完符号
链接还要重启 resolved —— 实测（MRRC-BG1SB，2026-10-25）换完之后 `resolvectl status` 的
**Global 仍是 `192.168.65.7`**，因为 resolved 在 `foreign` 模式下把旧文件的服务器吸进了全局
列表，只有重启才会重新读文件；全局条目会被链路口径不认领的查询用到，所以那条死地址必须清掉。
测试 8 个（真跑脚本 + 临时目录，含"没有 resolved 时不动"、"stub 不存在时放弃"、"幂等"）。

**验证**：`python -m unittest discover -s tests` → 2865 tests, OK（1 skip：macOS 上的
fake-radio 可选用例）。

## [Unreleased] — 2026-10-25 — 从 Windows 电脑远控升级盒子；顺带修掉一个「每次升级都回滚」的看门狗缺陷

**触发**：一台盒子只在局域网里够得着（客户没有 SSH、也没有桌面），要给它升级。
`dev_tools/mrrc-diag.cmd`（2026-10-24）已经证明「Windows 电脑 + curl + 盒子自己的 API」
这条路可行，升级只是同一个形状的另一半。

**顺带找到的缺陷**（写这个工具时撞上的）：`box_update.watchdog_command()` 用
`curl -skf …/api/health` 判活，而 `/api/health` **不是公开路径**（`PUBLIC_PATHS` 里没有它），
盒子从回环问也一样要令牌 —— 它答的是 **401**，而 `-f` 把 401 当失败（实测退出码 22）。
后果是：新树已经起来、页面能用，看门狗 90 秒后照样把备份翻回来 —— **每次自升级都静默
回滚**，页面上只看到「完成」和一句重启。判据改成「有没有 HTTP 应答」
（`%{http_code}` != `000`），与 `launcher_net.answers()` 已经用过的那条一致。

**新增** `dev_tools/mrrc-update.cmd`（不是安装包的一部分，与取包脚本一样由仓库提供）：
问一次 IP 与口令 → 读 `/api/update/check` 并把「当前 / 线上 / 包大小 / 更新说明」摆出来
→ 要一次 `YES` → `POST /api/update/apply` → 每 4 秒跟 `/api/update/state` → 等盒子回来 →
再读一次版本，**并明确说出它是升上去了还是被回滚了**。升级本身仍然全在盒子上做
（下载、校验、备份、覆盖、重启、失败自回滚），电脑只是替用户点那一下。

- 发射门禁不绕过：PTT/TUNE/CQ/FT8 与磁盘不足都是盒子自己拒，脚本只把 `reason` 原文转给操作者。
- 口令只进 `%TEMP%` 的一份 JSON（`--data-binary @file`），**不进命令行**（任务管理器与
  `ps` 都看得见 argv），收尾时删掉；输出目录里只有版本、状态与原始应答。
- 老构建（< 2026-10-18）没有升级接口：脚本把 404 与「请求本身失败」分开说，并指出那条
  盒子走 USB / `mrrc-update` 的路。
- 文件保持纯 ASCII：cmd.exe 按当前代码页读批处理，中文注释在 936 上有过吃掉下一行的
  历史；控制台上的中文靠 `chcp 65001` + PowerShell 的 `OutputEncoding`。

**老盒子（1.24 ~ 1.27）也能"只换程序、不刷机"** —— 但走**另一个工具**：
`dev_tools/mrrc-update-ssh.cmd`（`MRRC-Update-SSH.cmd`）。分开的理由是职责与权限：
那条路要在客户机上开 SSH、以 root 覆盖 `/opt/mrrc_modern`，它不该躺在一个双击就跑、
只调盒子 API 的工具里；两份 batch 也一定会漂。

- `mrrc-update.cmd` 只负责**指路**：确认前先探一次 `/api/update/state`，老构建当场停下，
  说明它没有远程升级器、去用 `MRRC-Update-SSH.cmd`，并点名 ≤1.25.5 那个会删数据的 `mrrc-update`。
- `mrrc-update-ssh.cmd`：按清单（`box` / `boxLine`）下载发布包 → 核对 SHA-256 → `scp` →
  在盒子上跑随包发布的 `mrrc-remote-update.sh`（先备份、只覆盖不删除、pip、重启）→
  等 `/api/health` 应答。**不需要网页口令**；不带 `-f` 判活（401 也算活着）。
- `mrrc-remote-update.sh` 是 `box_update.apply()` 的 shell 双胞胎：同一个 keep 列表、
  同样的"绝不删除"、`version.txt`/`fixpack.txt` 取自包里；pip 失败就不重启（旧代码继续跑）。

**测试**：`tests/test_box_update_cmd.py`（15 条）把脚本与盒子的契约钉住——端点与 HTTP 方法
对得上 `server.py`、等的状态名来自 `box_update` 的常量、确认在动手之前、口令不进 argv、
探活用状态码而不是 `-f`、文件纯 ASCII、老盒子那条路从**清单**取包（盒子的 `check` 在旧构建上
给的是桌面版 installer）、`%HOST%` 真的被赋值；`tests/test_box_update.py` 新增一条盯住
「看门狗必须把 401 当成活着」；`tests/test_remote_update_script.py`（7 条）在假树上跑安装器：
运行期目录一个字节不动、包外文件不被删、先留回滚包、无 `version.txt` 的包直接拒绝。

## [镜像线 v1.28.6 · FP2] — 2026-10-26 — 盒子级音频电平进菜单；另含 FP1 以来合入的解析链修复

**这是盒子线的第 2 个 Fix Pack**（版本 `1.28.6`，编号 `FP2`）。FP 号全局递增、不复用，
**不参与版本比较**：现场盒子的解析器只认三段版本号。包里还带着 FP1 之后合入、但盒子
**一直没拿到**的几笔现场修复，见下面「一并到达」。

**主线：盒子级音频电平（AD-032）** —— 电平归盒子，一个量一个所有者。

- **TX 麦克风增益**（−20…+25 dB，默认 0）：只作用于**浏览器麦克风**上行
  （`_feed_tx_from_uplink`），CQ 播放与 FT8 波形不经过它——让"人声的旋钮"推动 FT8 的 ALC
  是事故面。TX 录音 tap 移到增益之后，于是录音录的是**电台实际收到的信号**。
- **RX 下发电平**（−20…+30 dB，默认 0）：只加在下发编码之前，**录音、FT8 解码、盒子本地
  扬声器保持原生电平**——软件增益补不出电台没发出来的信息，把解码链路顶到削顶只换来
  解不出的信号。
- **两个入口**：☰ 菜单「音频电平（盒子）」两条滑块（一台盒子一个值，别的设备改了这里跟着动）、
  `settings.html` 的新卡片（带复位与三句说明，其中一句是**盒子 RX 电平与浏览器 🔊 相乘**）。
- 网页端 🎙 Vol 上限从 2×（+6 dB）提到 **+25 dB**（现场一支偏轻的耳机麦克风 6 dB 不够），
  滑块改 dB，旧 cookie 一次性迁移。
- **升级后声音一个字节都不变**：两个默认值都是 0 dB，网页端 🔊 的 10× boost 没动，所以安卓 /
  iOS / `/listen` / `/sdr` 无需同版本发布。坏值**拒绝**（400 + 区间）而不是静默夹取；
  削顶按方向计数，单块比例 >1% 时每 30 s 一条警告——盒子自己造出来的失真必须由盒子说出来。

**一并到达（FP1 之后合入，盒子此前没有）**：

- **解析链三层修复**（2026-10-25 两台现场盒子）：备用 DNS（114.114.114.114 / 8.8.8.8）写进
  每个 NM profile 并在启动时补齐、构建期把 `/etc/resolv.conf` 还原成 systemd-resolved 的
  stub 链接（`resolvectl status` 报 `foreign` 时每 ipv4.dns 都是死配置）、`fix-resolv-conf`
  这个 root 助手动词 + `POST /api/box/fix-dns`（只要网页口令，不需要 SSH / root）。
- **root 助手**：升级后重启它（它自己也是被这次升级覆盖的文件）；`fix-dns` 的 404 分支改成
  优先提示 SSH。
- **远程协助面板**补「端口」一行（此前支持者只能先问端口号），「复制口令」改成复制
  用户名 / 口令 / 端口三行。

**记录与守卫**：`CHANGELOG` 的 `[镜像线 vX.Y.Z · FPn]` 标题是版本号与 FP 号的唯一来源
（`make_box_source.version_of()` 一次读两个值）；`tests/test_release_artifacts.py::BoxLineFixPackTests`
把「CHANGELOG 最新镜像线条目 ↔ 清单 `boxLine`」钉在一起；`dev_tools/make_latest_json.py`
现在**只写 `boxLine`**（此前写的是 `box`——盒子线比安装包线新时，老盒子的解析器会因
`box.version != latest` 直接报错），并新增 `--box-only` 只换盒子段、不去重新哈希一个不在
本机的 80 MB 安装包。

**验收**：`unittest` 2945 项绿（1 skip）、`release_check` 离线 0 failing；本机真起服务实测
`GET/POST /api/audio/levels` 回环、越界 400、落盘、两个入口的滑块；线上 tarball 大小与
SHA-256 逐字节复核。

## [镜像线 v1.28.5 · FP1] — 2026-10-22 — 隧道没起来时不再沉默：缺什么就点名什么

**这是盒子线的第 1 个 Fix Pack。** 从这一版起，盒子线的每次修复交付在版本号之外多一个
编号（`FP1`、`FP2`…），它出现在页脚、更新卡与诊断包里——为的是一句「你装的是哪个 FP」
就能指认一次交付。编号**不参与版本比较**：现场盒子的解析器只认三段版本号，把 FP 拼成
`1.28.5.1` 会让那一版在它眼里等于 `1.28.5`，等于没发
（设计：`docs/superpowers/specs/2026-10-22-box-fixpack-design.md`）。

**修复本体**（2026-10-22 现场，`bh7kyz.mrrc.vlsc.net`）：公网入口连续四天 502，而盒子
自己的设置页写着「已接通」。hub 侧逐层无异常——路由、证书、信任包、端口都就位，
nginx 只有一句 `connect() failed (111: Connection refused) … 127.0.0.1:18816`，
frps 四天零登录记录——断点在盒子的 frpc 隧道上。

- `_cloud_start_tunnel()` 在 `fleet/frpc` 或 `fleet/frpc-<label>.toml` 缺失时原本**静默
  `return`**：不写日志、不留状态；而设置页只在 `tunnel_error` 非空时才提隧道。两处沉默
  正好对齐 ⇒ 页面上只剩一个绿色的「已接通」。现在缺什么就点名什么，并按内容去重打一条
  WARNING（该端点每 30 秒被轮询一次），隧道起来后自动清空。
- 盒子设置页的「隧道」行改为**无条件显示**（运行中 / 错误原文 / 未运行），隧道不在跑时
  连「已接通」那个绿标一起变红（`已接通 · 隧道中断`）。

**配套**（这次一起到的）：盒子线修复包编号的整条链——清单 `boxLine.fixpack`（老解析器
忽略这个新键）、tarball 里的 `fixpack.txt`、`/api/update/check` 的
`current_fixpack`/`latest_fixpack`、更新卡与页脚的 `版本 · FPn` 显示、
`make_box_source.py --fixpack` 与 `make_latest_json.py --box-fixpack`、
`CHANGELOG` 的 `[镜像线 vX.Y.Z · FPn]` 标题（`version_of()` 一次读两个值）。

**验收**：`unittest` 全套绿；`release_check.py` 离线 0 failing；
`tests/test_release_artifacts.py` 新增守卫把「CHANGELOG 最新镜像线条目 ↔ 线上清单的
`boxLine`」钉在一起（此前这条对齐全靠人）。

## [v1.28.2] — 2026-10-21 — 启动器不再把用户选的机型改回 ft710；FT-891 自己找 CAT 波特率；FT8 深路径不再每槽杀死 DSP worker

### 栈：`sync8var` 的工作数组放在**调用线程**的栈上

同一份包（`20261021-123324-81ab`，Windows 10 + 1.28.1）里还有第二个独立缺陷：
`ft8-worker.log` 之外，主日志里是 **50 条**

```
[WARNING] ft8.engine.cascade: deep decode path failed; keeping the fast results
ft8.core.supervisor.WorkerFault: worker_exit: worker process exited with status None
                                （generation 35, restarts 34）
```

—— 每一次**深路径**解码都让 worker 进程消失，且没有任何 Fortran 提示（`stop` 会打印消息，
静默死亡说明是硬崩溃）。macOS 上同一份代码、同一套 `tests/ft8` 全是绿的，所以这不是解码逻辑。

**根因**：`sync8var` 把工作数组声明在**栈**上 ——
`real s(NH1,NHSYM)` = 1920 × 372 × 4 B ≈ **2.7 MB**，加 `sync2d`/`syncq` 的 ±62 dt 窗口
（`patched/ft8_decodevar.f90:199-251`）又是 ~1.2 MB，**单个栈帧 ≈ 4 MB**。
Windows 给线程的是 exe 的 PE 预留（**2 MB**），溢出即进程当场消失；macOS 主线程 8 MB，
所以本地永远复现不出来。`OMP_STACKSIZE` 管的是 OpenMP 的**工作线程**，而解码器是从
请求循环所在的线程进入的 —— 它救不了。

本机实测（源码构建的库，子进程）：

```
2 MB 栈 → 退出码 138（SIGBUS）   3 MB → 138   4 MB → 存活   8 MB → 存活   64 MB → 存活
```

- `ft8/core/worker.py` 新增 `WORKER_STACK_BYTES = 64 MB` 与 `_run_with_large_stack()`：
  请求循环跑在一个显式设过大栈的线程上（`threading.stack_size` 只影响其后创建的线程，
  所以设完 `start()` 立刻还原，进程里其他线程不受影响）。64 MB 是**保留**地址空间，
  物理页按需提交，小盒子上不占内存。
- 守卫：`tests/ft8/core/test_worker.py` 新增 4 项 —— 线程确实按 `WORKER_STACK_BYTES` 创建、
  异常回到调用线程、`worker_main` 不再自己持有 `while` 循环（结构化断言，避免有人把它挪回去）、
  以及一条**真跑子进程**的测试：2 MB 栈必须死、我们的栈必须活。

### 呼号库：`ALLCALL7.TXT` 从来没进过安装包

同一条 worker 日志的第一行是

```
ALLCALL7.TXT is too short or broken?             d
```

Fortran 用 `share_dir = '.'`（`prog_args.f90:4`）**相对当前工作目录**打开它，而且**没有 setter**。
源码 checkout 的 cwd 正好是仓库根，所以本地一辈子看不见；装好之后 cwd 是启动器所在目录，
呼号库于是静默变成空（`ldbvalid = .false.`，深路径的呼号搜索直接 bail out）。

- `packaging/pyinstaller/mrrc_modern_server.spec`：把仓库根的 `ALLCALL7.TXT` 收到
  PyInstaller 的 `_internal` 根（与 DSP 库同目录），Windows 与 macOS 共用这份 spec。
- `ft8/core/worker.py`：新增 `share_dir()` 与 `worker_main` 里的 `os.chdir(share_dir())`
  （在加载 DSP 库、第一次解码之前；该进程没有别的相对路径 I/O）。
- 守卫：`tests/test_ft8_packaging.py::CallSignDatabaseTests` 3 项（仓库里有这个文件 /
  spec 的落点是 `"."` / worker 会 chdir 到含该文件的目录）。

**边界**：`ft8/dsp/` 一个字节没动，所以四条 DSP 构建路径不需要重编；但**两个安装包必须重出**
才会有正确行为（盒子镜像本来就带这个文件，chdir 对它是无操作的兼容）。

### 启动器：机型白名单来自注册表（FT-891 / FTX-1 都是受害者），探测结果不再覆盖用户的选择

一台 **FT-891** 上报「设备不工作」（包 `20261021-123324-81ab`，Windows 1.28.1），日志里三行说明一切：

```
12:27:18  Saving connection settings: model=ft891 port=COM6 baud=38400   ← 用户在界面上改成 ft891
12:30:00  Radio model: ft710, serial port: COM6 @ 38400 baud             ← 重启后又变回 ft710
12:30:02  No ID response from radio on COM6. Check connection.
```

根因在 `macos/first_run.py`，**两个缺陷叠加**：

- `VALID_MODELS` 是手写的三个机型（ft710/ic7300/ic7300mk2）。凡是后来加入的机型
  （FT-891、FTX-1、FTDX10/101、IC-705/7610/7760）都不在白名单里 ⇒ `needs_first_run()`
  **每次启动**都判「未配置」⇒ 每次都重新探测。
- `probe_ft710()` 只验应答前缀（`ser.read(64).startswith(b"ID")`），而 FT-891 回的是
  `ID0680;` —— 以 `ID` 开头 ⇒ **任何 Yaesu ASCII-CAT 电台都会被认成 ft710**；就算电台
  不应答（波特率不对），也会落到 `RADIO_MODEL_FALLBACK = "ft710"`。两条路都通向 ft710。

于是 FT-710 后端（38400、要求 FT4222 频谱）去驱动 FT-891，界面自然「不工作」。
这与 2026-10-17 那三条 FTX-1 上报（`20261017-225339/225416/225430`）是**同一个缺陷**，
当时无人值守实施了三次都因补丁格式失败，而受害者已经排到第二个。

- `VALID_MODELS` 改为从 `backends.known_models()` 派生（注册表是唯一真相源，静态集合只在
  启动器打包时缺 `backends` 树的情况下兜底）。
- `apply_first_run()`：只有「机型还没选过」时才采纳探测结果；已选机型只填端口，并打印
  `Radio answers like … keeping the configured model …`。
- 守卫测试：`tests/test_first_run.py::RegisteredModelWhitelistTests` —— 每个注册机型都必须
  算「已配置」（否则重启就重探）；已选 `ft891` 时探测不得改写它；机型为空时探测照旧生效。

### FT-891 的 CAT 波特率：默认按手册的 4800，并且能自己找到

同一份现场：`MRRC_BAUD_RATE=38400`，而 FT-891 的菜单 **05-06 CAT RATE** 出厂值是
**4800**（Yaesu FT-891 操作手册，西班牙语/俄语版同表：4800/9600/19200/38400）。电台对
错误波特率是**完全沉默**，所以现场只看到一句 `No ID response` —— 这是界面上唯一看不出来的
配置错误：线缆、端口、电台都「看起来正常」。

- `config.py` 与 `backends/yaesu/yaesu_profiles.py` 的 FT-891 默认速率 38400 → **4800**。
  此前那个 38400 的依据是 2026-09-17 的现场记录，而那台机器的用户**已经**把菜单改成 38400
  （DigiRig 的 FT-891 数字模式指南也推荐 38400），不能当出厂值。
- `YaesuCatController`：配置速率下 `ID;` 一个字节都不回时，按菜单的四个档位
  （`CAT_RATE_LADDER`）依次试，命中后**本会话**采用它并留一条可照做的告警；四个都不应时
  端口仍按原速率打开，日志给出「菜单 / 线缆 / 端口名」三件事。**不写配置文件** —— 环境文件
  属于用户，静默改写比一条告警更糟。
- 守卫测试：`tests/test_yaesu_baud_autodetect.py` —— 速率正常时不得多开一次端口；沉默时
  采用真正应答的速率；全沉默时回到配置值交给重连。

## [Unreleased] — W103D 发货版（预置）镜像：固定口令、零初始化、Wi-Fi 与公网入口都在设置页

卖家代刷一批 W103D 快递给用户，「开箱即用」这条链上的三处硬拦一次拆掉（设计
`docs/superpowers/specs/2026-10-17-w103d-preflash-design.md`、计划
`docs/superpowers/plans/2026-10-17-w103d-preflash.md`、SDD V2.92 / AD-027 / NFR-069）。

### 盒子自更新：`/settings` 里点一次就升级（失败自动回滚）

用户没有桌面、没有 SSH、也没有 Linux 能力 —— 所以升级必须是一页里的一个按钮，而不是
`ssh` + `mrrc-update`。新的 `box_update.py` 把这件事做成一条链路，全部在盒子本地判断：

- **检查**：`GET /api/update/check` 现在认清单里的 `box` 段（盒子跑的是源码树，不是安装包）；
  没有这一段就退回原来的桌面语义，页面不用分叉。
- **升级**：`POST /api/update/apply` → 下载 tar.gz → **校验 size + sha256** → 解包（拒绝绝对
  路径与 `..`）→ 备份当前树 → 覆盖（**不删任何文件**，`venv`/`logs`/`env`/`.local` 一概不碰）
  → 写 `version.txt`/`VERSION` → 重启服务。
- **三道闸门在动手之前**：发射中（`radio.tx_status` 非零，PTT/TUNE/CQ/FT8 都算）、磁盘不足
  400 MB、已有一次在跑。它们的语义是「不能开始」，不是「开始了再失败」。
- **失败能自己退回去**：覆盖前把旧树打成 `rollback-<ver>.tar.gz`；重启前拉起一个**脱离进程**
  的看门狗（90 秒后探 `/api/health`，探不到就还原 + 重启）。重启用 `pkill` 自己，不需要 root ——
  服务的单元是 `Restart=on-failure`，所以这条链路上**没有给服务端加任何提权通道**。
- **页面**：`/settings` 新增「软件更新」卡片（当前/可升级版本、说明、进度、失败原因），
  重启阶段自动重载页面。
- **发布侧**：`dev_tools/make_box_source.py` 产出 `mrrc-modern-src-<ver>.tar.gz` 并打印
  可直接贴进 `latest.json` 的 `box` 片段；`dev_tools/make_latest_json.py` 加 `--box`，
  版本必须等于 `latest`（网页上「最新」只有一个）。
- 真机（`.217`）：跑通整条链路 —— 下载/校验/解包/备份/覆盖/写版本号，`version.txt` 变成包里的值，
  回滚包可解出旧树；`/api/update/state` 与 `/api/update/check` 都按预期回答。
- 测试 **+19**（`tests/test_box_update.py`：清单 `box` 段、size/SHA、越界路径、不覆盖 env/venv、
  不删文件、版本号来源、整链路 + 坏哈希不动手、看门狗命令）。

### 刷机镜像改为对外下载版：预置（`--preflash`）即发布

- 操作员决策（2026-10-18）：老的公开镜像要先走一遍首启引导（生成 Web 口令、探测串口与声卡、
  `/setup`），对大多数用户**太复杂**；而代刷发货用的预置版开机即可登录。于是**对外下载的
  就是预置版**：Web `abcd!1234`、`root` `abcd1234`、`mrrc` `abcd1234`，时区 `Asia/Shanghai`、
  区域 `zh_CN.UTF-8`，不需要任何初始化。
- 代价写清楚：下载页上每一份都是同一组**公开口令**，且镜像的 `sshd` 默认允许 root 口令登录。
  两张下载卡都改成「**第一件事是改掉这三个口令**」并链到用户指南；`packaging/box/README.md`
  的发布说明同步改写（原来那句「发货版不该上传下载页」作废，理由与替代约束写在同一段）。
- 产物（本机真构建 + 四步验证全绿）：`MRRC-Modern-1.28.1-w103d.img.gz`，
  **1,205,308,280 B**，SHA-256 `83cd9df1…`。验证含：代码身份逐字节、`bluez` 已装、
  `input:x:102:mrrc`、五个 systemd 单元 + logrotate 配置、11 份 profile、frpc 是 aarch64 ELF、
  SDRplay API/`libsdr_dsp`/udev 规则在位、`/tmp` 零残留、D-11 双指向、chroot 真跑导入
  （含 `paramiko` 与 `sdr_frames/sdr_hub/net_bt`）。

### 遥控器配对必须在 pty 里跑：`No bonding` 是配不上的真正原因

- 现场（盒子 `.217`，蓝牙遥控器 `CMCC_Voice_Remote`）：配对**看起来**成功过，`btmon` 却给出
  `SMP: Pairing Request … Authentication requirement: No bonding … (0x28)` →
  `SMP: Pairing Failed: Invalid parameters (0x0a)`；链路上来了、属性全拒（`hog-lib.c: HID
  Information read failed`，ATT `0x0E`），HID 设备建不出来，`keys_work` 永远是 false。
- 根因不是遥控器：**一次性 `bluetoothctl --timeout N pair` 不注册配对 agent**，bluez 于是请求
  「不绑定」；而 BLE HID 遥控器必须存一把密钥才能重连，只能回 `Invalid parameters`。
  把同一串命令打进 **pty**（`--agent NoInputNoOutput`）后请求变成 `Bonding (0x29)`，
  `Pairing successful`，bluez 随后建出 `CMCC_Voice_Remote Keyboard`，键立刻能调频。
- `net_bt.pair()` 现在跑交互式会话（`pty` + 脚本：`power on` → `scan on` → `pair` → `trust`），
  没有 pty 的平台退回一次性调用。面板文案改成「先扫 12 秒再配，全程约 30–45 秒」。
- **顺带补上第三个缺口**：`mrrc` 不在 `input` 组。`/dev/input/eventN` 是 `root:input 0660`，
  缺组时 `mrrc-localctl` 每次 open 都是 `Permission denied`，而界面上没有任何地方会报
  「缺组」——遥控器看着配好了、按键却毫无反应。两条镜像线（`packaging/box/box-overlay.sh`、
  pi-gen 的 `00-run-chroot.sh`）都加上 `input`，并有一条 parity 测试钉住。
- **按名字配对，不认 MAC**：`CMCC_Voice_Remote`（可用 `MRRC_BT_REMOTE_NAME` 改，逗号分隔）
  的名字对上一台就配一台，对上几台就配几台（串行，最后给「N/M 台成功」的汇总）。
  来料批次会换地址，而「这台叫什么」是能一眼看见的事实；多配一台的代价远小于「这台不认」。
- **固化进镜像，新盒子不再逐个适配**：`packages.txt` 加 `bluez`（缺它时面板永远扫到
  0 个设备，界面上却只说「遥控器在配对模式吗？」）、服务层 `enable bluetooth.service`、
  两条镜像线把 `mrrc` 加进 `input` 组、配对走 pty+agent。四条都有测试钉住。
- 测试 +3（配对脚本顺序与等待上限、镜像 `input` 组）。

### 遥控器音量键只能推到 39 %：百分比当成了 0–255 的寄存器

- 现象：按住「音量 +」到底，声音仍然不够大。
- 根因是一个**单位错**，和配对无关：`local_ctl.Controller.volume` 是 **0–100 百分比**，
  却被原样写进 `af_gain`，而电台的 AF 增益是 **0–255 寄存器**（`radio_state.af_gain`、
  Yaesu `AG0xxx`、Icom `LVL_AF`；网页上同一个量就是 0–255，`ft710_ui.js` 用
  `/255*100` 显示百分比）。满格只到 `100/255` ≈ **39 %**。
- 现在按 `round(pct / 100 * 255)` 换算（`gain_from_volume` / `volume_from_gain`），
  每次按键仍是 5 %（≈13 个寄存器台阶），第 20 下就是 255。
- **同一个面上的第二个缺陷**：服务器的 `state` 首行只带 `freq`/`mode`，守护进程的音量
  从自己的 50 起算——第一次按「音量 +」会把电台从 200 直接改成 55（往下走）。现在
  `state` 带 `af_gain`，**每条命令的回复也带回当前增益**（操作员也可能直接拧电台旋钮），
  `_absorb_gain` 回读成百分比，按键从电台当前值开始加减。
- `linux/mrrc_update.sh` 现在还会 `systemctl try-restart mrrc-localctl`：那个守护进程
  跑的是**自己进程里**的 `local_ctl.py`，只重启 `mrrc-modern` 会让改好的换算一直躺在
  `/opt` 里不生效（`try-restart`：没装这个 unit 的老盒子不会让升级失败）。
- 测试 2560 → 2568（满量程 / 步进百分比 / 到底静音 / `state` 回读 / 回复回读 /
  脏值忽略 / 两边 `af_gain` 在场 / 热升级重启遥控守护进程）。

### 遥控器「频道 + / −」改为七个固定落点，模式跟着走

- 现场：遥控器上有「频道 + / −」两个键，而原来的实现是「跳到相邻波段里同样的相对
  位置」——落在哪儿取决于你从哪儿来，而遥控器没有显示屏，按完只能看电台。
- 现在按 `PRESET_CHANNELS` 逐个跳：**3.850 LSB → 7.050 LSB → 10.100 USB →
  14.270 USB → 21.240 USB → 28.500 USB → 29.600 FM**，到底回绕；当前频点不在这七个
  上时先归到最近的那个，再走一步。模式规则写死在表里（10 MHz 以下 LSB、以上 USB、
  29.600 FM），改一处就够。
- **频率和模式成对发**（先频率后模式）：电台换波段时可能自己重挑模式，预设的模式必须
  是最后一句。`next_band()` 与它的「相对位置」测试一并删除。
- 真机（`.217`）：七个落点逐个下发均 `ok`，`29.600 → FM`、`7.050 → LSB`、
  `10.100 → USB` 都被电台接受。
- 测试 2705（频道走表 / 回绕 / 就近归一 / 模式随行 / 半途数字输入被清掉）。

### 遥控器面板：发现就直接配，失败要说出原因

- 现场（盒子 `.217`，00:10–00:16）：操作员连点三次「配对」，服务端三次
  `POST /api/box/bt/pair → 400`，日志只有 `keys_work=false`；其中两次打的是
  `44:3E:8A:10:DB:F8`（一个叫 `CASL 39982` 的 LE 信标）。真正的原因——遥控器已经退出
  配对模式——只出现在终端里，页面上一个字都没有。
- **不再点第二次**：扫描结束后，如果只有**一台**「bluez 认得出是 HID 输入设备
  （`input-*`）**且**这次扫描有信号强度（= 它正在广播，也就是在配对模式）」的设备，
  页面直接调用配对，并说明「找到遥控器「X」，正在配对…」。多于一台、或一台都没有时
  仍然列出清单，不替操作员猜。
- **失败要说出原因**：`net_bt.pair()` 现在把 `bluetoothctl` 的那一行带回来
  （`Failed to pair: …` / `Device … not available` / `Authentication*` / `org.bluez.Error.*`），
  页面显示「没有配上：`<原话>`」。上一版对任何原因都只说「确认遥控器在配对模式」。
- 测试 2477 → 2481（`tests/test_net_bt.py` +4：自动配对判据、错误原话提取、成功无错误）。

### 遥控器面板：给信号强度，让它能被找出来

- 现场（盒子 `.217`）：BR/EDR 扫出 **23 个**设备，页面只给「地址 + 名字」——操作员没法判断
  该点哪一个（绝大部分是无名 LE 信标，而且 `bluetoothctl` 还会重放 bluez 的**缓存**）。
- 三处改动：① 扫描改用 `btmgmt find -l`（有 RSSI 与地址类型），服务器没有 CAP_NET_ADMIN 时
  退回 `bluetoothctl`，并从它的 `[CHG] … RSSI:` 行里取信号强度；② 既没名字也没信号强度的条目
  直接丢掉（那是缓存回声），列表按「像遥控器的在前 → 当场扫到的在前、强的在前」排序；
  ③ 每个设备查一次 `bluetoothctl info` 取 `Icon`，`input-*` 的直接在页面上写「遥控器/键盘」。
  实测结果是遥控器排在第一行，带 `input-keyboard` 标记。
- 两个解析陷阱（都是实测出来的）：`bluetoothctl` 给每个彩色 token 套了 `\x01`/`\x02` 字节，
  只剥 ANSI 会让 `^[(NEW|CHG|DEL)]` 全部匹配失败（18 行 RSSI 解析出 0 个）；`[CHG]` 行是属性流，
  把它的尾部当名字会让设备**被叫做** `TxPower: 7`。名字只从 `[NEW]` 取，RSSI 只从 `[CHG]` 取。
- 测试 2468 → 2477（`tests/test_net_bt.py` +9：信号强度解析/去重取最强/排序/缓存回声丢弃/
  加粗字节与 `input-*` 排序）。

### 热升级装不上单元，这一条写进指南

- 真机（盒子 `.217`）把 1.25.5 升到 1.28.1 之后，`/api/box/status` 仍报 `helper_available: false`：
  `mrrc-update` 只换代码，**systemd 单元与系统集成只在镜像构建时装**（`mrrc-box-helper` /
  `mrrc-localctl` / `mrrc-cpufreq` / `mrrc-logrotate.timer`）。表现是设置页那几个按钮是灰的、
  蓝牙遥控器不来、CPU 不锁频、日志无轮转 —— 都不会报错，只是「新版本没有新能力」。
- `docs/W103D_GUIDE.md` 的「升级」小节写清楚了这条边界，以及补装时要连
  `RuntimeDirectory=mrrc` 一起加（否则 `/run/mrrc` 建不出来，两个套接字无处安放）。
- 实测（`.217`）：三个单元 + polkit 规则装上后重启，`helper_available: true`、写动词通
  （`/api/box/timezone` 幂等提交）、governor `schedutil → performance`、`/run/mrrc` 两个套接字
  权限正确、云入口 302、电台与频谱都在。

### 热升级不再留下旧版本号（`mrrc-update`）

- 盒子上「热升级」走的是 `linux/mrrc_update.sh`：stage 树 → pip → 重编 FT8 → 重启。
  它**从来没写过 `version.txt`**，所以 1.25.5 的盒子升到 1.28.1 的树之后，诊断包
  manifest、`/api/update/check` 的「当前版本」和 ☰ 菜单页脚仍然说 1.25.5 ——
  「升级了但行为没变」这类报告因此无从分辨（2026-10-17 实测，盒子 `.217`）。
- 现在 updater 会盖版本号：`MRRC_UPDATE_VERSION` → 源树 `CHANGELOG.md` 的第一条
  `## [vX.Y.Z]` → 源树 `version.txt`，三个来源按可信度排；第二个在**喂一棵已 stage
  过的树**时不存在（`stage-tree.sh` 的 `/*.md` 排除），第三个就是为那种情况留的。
  `version.txt` 与 `VERSION` 一起写，和镜像同一条规则。
- 同一轮还发现：`maxsize 5M` 只在 logrotate **跑的时候**才被看见，日更等于把上限变成
  「一天里能涨多少」。`mrrc-logrotate.timer` 改成**每小时**（`OnCalendar=hourly`，
  `Persistent=true`），没到尺寸时是一次毫秒级空转。

### 远程协助在盒子上接通：两个真缺陷，一个在配置里、一个在接线里

用盒子 `192.168.1.217`（v1.25.5，Cloud Hub `ba4eg`）跑完整条链路时拓出来的，两个都不是协议问题：

- **「开启协助」每次都失败，报 `frpc admin API unreachable on 127.0.0.1:0`。** 端口 **0** 是线索：
  它表示这份隧道配置里**没有 `webServer` 段**。实例是在管理段进入生成器（2026-10-12）之前入网的，
  而 `connect()` 不会为已入网的实例重写配置、`set_help_proxy()` 也不敢擅自铸一个（跑着的 frpc
  不认新端口）。新增 `cloud_hub.ensure_admin_segment()`：在**启动隧道之前**补一段——frpc 只在
  启动时读一次配置，那是唯一安全的时机——并在 `set_help_proxy()` 遇到无管理段时给一句能行动的话
  （「重启一次应用就会自动补上」），而不是拿 0 号端口去重试 3 秒。
- **「已连接」永远是 0；「连错 5 次自动关闭」也从未发生。** 同一形状的两条死接线： `HelpSession.note_connection()` 没有任何调用者：SSH 门
  （`SshHelpServer`）只发 `on_event`，而计数住在会话上，两边从来没接线。单测直接调
  `note_connection()`，所以这个洞一路活着。现在门在会话结束时回调 `on_disconnect`，
  `build_session()` 把它接到 `note_connection()`；新增的测试**走接线本身**（真 paramiko shell
  会话 → `connections == 1`，探针 exec 不计数），去掉那行接线它立刻变红。口令失败那条更严重：
  门的 `check()` 自己数失败次数，而决定关窗的 `HelpSession.note_auth_failure()` 没有调用者，于是
  「连续试错 5 次自动关闭」是一句没有实现的承诺。现在 `on_auth_failure` 接上，两条测试钉住
  （错一次 → `failures: 1`；第五次 → `closed / auth_failures`）。**OpenSSH 默认一次连接试 3 遍口令**，
  所以一次 typo 会吃掉 3 次预算 —— 支持者用 `-o NumberOfPasswordPrompts=1`。
- 真机验收（盒子，2026-10-17）：开窗 → 从 hub `ssh -p 18818` 交互式登入，`id -un` = **`mrrc`**
  （不提权）→ `connections: 1` → 客户「停止协助」→ hub 端口释放、配置里的协助代理摘掉、主入口
  全程 302 不抖。记录与排障表更新在 `docs/remote_help.md`。

### 硬盘不会被日志写满：轮转、journald 上限、录音拒写

- **触发它的是一次实测**：盒子上的 `local_speaker.py` 是旧版（缺「一条警告一条 streak」
  的修复），蓝牙耳机不在线时每 20 ms 写一条 `Local speaker write failed` —— 5 小时
  **14.5 万条 / 17 MB**。修掉刷屏后，这个问题还在：`logs/server-stdout.log` 是 systemd
  `StandardOutput=append:` 写的，**systemd 不轮转 `append:` 目标，服务端也不轮转它**，
  它是整个镜像里唯一没有上限的文件（`server.log` 2 MB × 2 代、`ft8-worker.log` 2 MB、
  support bundle 留 5 份，其余都有界）。6.5 GB 的根分区，一个慢一点的缺陷就能走完。
- **服务层新增 `/etc/logrotate.d/mrrc`**：`server-stdout.log` 满 5 MB 触发、留 3 代、
  压缩，**`copytruncate`**——systemd 持有 fd，rename 会把后续写入留在一个已 unlink 的
  inode 上：目录看着转了，硬盘继续涨，比不轮转更糟（它把增长藏起来）。
- **scheduler 是自己的**：`mrrc-logrotate.timer`（`OnBootSec=10min`、`OnCalendar=daily`、
  `Persistent=true`）。不押注镜像里 cron 有没有被 mask，也不押注 Debian 的
  `logrotate.timer` 在不在——两者都可能无声地不跑。
- **journald 上限**：默认是根分区 10%（这台 650 MB，整机的六分之一）。新增
  `/etc/systemd/journald.conf.d/50-mrrc-size.conf`：`SystemMaxUse=128M`、
  `SystemKeepFree=256M`。
- **录音**：新增 `MRRC_RECORDINGS_MIN_FREE_MB`（默认 200）。开始录音前测一次剩余空间，
  不够就**拒绝并在界面上说明**，**绝不删除任何一段录音**——删除是破坏性的，产品不做。
  64 kbps / 16 kHz mono ≈ **29 MB/小时**，客户自己触发的录音是这台机器上唯一无界的写入方，
  而磁盘写满不是「录音停了」：PTT、音频、配置写入一起坏。
- 测试 2449 → **2460**：`tests/test_log_rotation.py` 6 项（轮转的文件名 == 单元的
  `append:` 目标、`copytruncate`、大小触发 + 代数 + 压缩、自带 timer 并 enable、
  `packages.txt` 里有 `logrotate`、journald 有上限）；`tests/test_recorder_api.py` +5
  （够了 / 不够 / 测不出 / 关掉 / 命令层在会话创建之前就拒绝）。

### 设置页配对遥控器（客户手里没有 SSH）

- `/settings` 新增「遥控器」面板：**搜索附近设备 → 选中 → 配对**，全程网页完成。
  遥控器侧只需同时长按「OK + 返回」5 秒进入配对模式。
- 新模块 `net_bt.py`（与 `net_wifi` 同一个套路：stdlib only、每次调用可注入 runner、
  测试不需要控制器也不需要 root）。三个端点：
  `GET /api/box/bt/scan`（BR/EDR 搜索，约 12 秒）、`GET /api/box/bt/state`、
  `POST /api/box/bt/pair`、`POST /api/box/bt/forget`（可选同时给控制器上下电）。
- **页面显示的判据是"按键能用"，不是"bluez 说配上了"**：现场那次 `Paired: yes` 但链路
  没加密、bluez 建不出键盘设备、每个键都是死的。`state()` 的 `keys_work` 来自
  `/proc/bus/input/devices` 里有没有该地址的键盘设备；配对标志只做诊断。没通时页面直接
  给「重配」按钮（清旧配对 + 蓝牙上下电），这就是文档里那条修法。
- 实测（W103D）：`{"paired":true,"bonded":true,"connected":true,"keys_work":true}`。
- 测试 2437 → **2449**（`tests/test_net_bt.py` 12 项）；`net_bt` 进镜像导入门禁。

### 省 CPU 两处：锁频调速器 + 只给扬声器时不编码 Opus

- **调速器 `schedutil → performance`**（服务层新增 `mrrc-cpufreq.service`）：这台的负载是
  碎而密的（音频 50 Hz、频谱 30 Hz、FT8 时隙），`schedutil` 一直在变频，每次都要付延迟和
  governor 自身的开销（`sugov:0` 累计占一核的 17.8%）。实测（W103D，vmstat 5 s ×3，三次复现）：
  整机忙 **27% → 14%**，**SoC 温度 48.2–48.4 °C 完全不变**。盒子是市电供电，"降频省电"在这里
  换不来什么，反而赔掉半个核。
- **没有浏览器时不编码 Opus**：录音、FT8 tap、本地扬声器要的都是 **PCM**，而 RX 循环原来
  只要有"人"在听就编码。现在编码门只认 `audio_rx_clients / 录音 / FT8 tap`，纯扬声器盒子
  不再每 20 ms 白烧一次 numpy + libopus。
- 测试：`test_audio` 的两条源码守卫随之更新（编码门 + `None if (_radio_txing or not _needs_frames)`）。

### ☰ 菜单里的 FT8 引擎开关（省 CPU）

- FT8 引擎设计上**开机就在后台解码**，在小盒子上那是实打实的一个核（那个
  `multiprocessing.spawn` 的 DSP worker）。设置区现在有一个开关：关掉 → 引擎停、
  DSP worker 退出、RX tap 释放；开回来 → 立刻恢复解码。
- 走的是**已有的**设置路径 `PUT /api/ft8/station {"engine_always_on": …}`
  （它本来就落盘并启停 runtime），没有第二条命令通道；开关状态读 `GET /api/ft8/station`。
  既有的单向「切到 FT8」菜单项不动——它要移动电台并跳转，回答的是另一个问题。
- 实测（W103D）：关 → `engine_always_on:false`，DSP worker 2 → **1**；开 → 回到 2。
- `ft710_ui.js?v=37 → 38`（否则已装用户的浏览器不会重新拉）。
- 测试：pytest `tests/ft8/web/test_ft8_api.py` +2（开关驱动 runtime、状态可读）；
  unittest `tests/test_ft8_engine_switch.py` 6 项（菜单位置、绑定的端点、状态来源、
  不劫持单向入口）；计数 2430 → **2436**。

### 盒子本地的耳朵：RX 音频进蓝牙耳机（AD-028 续）

- 新模块 `local_speaker.py`：把服务端已经在编码的 RX 音频再分一路给本机 ALSA 设备，
  队列有界、满了就丢（计数），播放走 `aplay` 子进程（蓝牙设备是 **ALSA PCM 名**
  而不是 PortAudio 的枚举索引），设备掉线自动重启子进程。
- 配置 `MRRC_LOCAL_SPEAKER_DEVICE`（ALSA 设备名）、`_RATE`、`_CHANNELS`，默认关闭；
  关着时浏览器路径逐字节不变。RX 循环的空闲门把本地扬声器算作「有人在听」
  （FT8 tap 已经上过这一课）。
- **实测（W103D + 荣耀亲选耳机X5e）**：A2DP 48 kHz 立体声**可用**，电台的 RX 音频
  稳定进耳机；**HFP/SCO 两条都不通** —— 播放无声音、采集 0 字节（`hfpag/source`
  打开后不送数据）。所以耳机麦克风在这颗 MT7663 上用不了，发射的本地音源要走 USB 麦克风。
- 测试 2420 → **2430**（`tests/test_local_speaker.py` 10 项）。

### 本地操作台：蓝牙遥控器直接调电台（AD-028）

现场操作员站在盒子旁边，用一支普通蓝牙遥控器调频、切模式、按住语音键发射。

- 新模块 `local_ctl.py` + systemd 单元 `mrrc-localctl`（`User=mrrc`）：读遥控器的
  `/dev/input/eventN`，按 **HID 扫描码**查键位表，把命令写成一行 JSON 发给服务端的
  **本机控制 socket** `/run/mrrc/localctl.sock`（0600，与服务同一用户，字段白名单）。
  **socket 的权限就是认证**：物理按键没有网页口令。最初那版让守护进程走网页登录，
  几分钟就把「5 次 / 5 分钟」的登录闸门撞成 429 并彻底失联；换成 socket 后没有登录、
  没有令牌、没有 TLS，命令与浏览器走同一条执行路径。没有遥控器时它空转等待；
  蓝牙断开后节点消失会自动重开。
- **键位**（W103D 上实测：中国移动语音遥控器 `CMCC_Voice_Remote`）：语音键 = 按住发射；
  ▲▼ = 频率 ± 步进；◀▶ = 步进档；频道 ± = 波段上下；数字 0–9 + 确定 = 直接输入 kHz；
  设置 = 模式循环；菜单/返回 = 取消输入；音量 ± = AF 增益；电源 = 交给我们（不再关机）。
- **必须先加密才认键**：那次配对报了成功，但 `btmon` 显示链路未加密，遥控器于是拒绝
  所有 GATT 读、bluez 建不出 HID 设备。删除设备 + 控制器上下电重配后
  `Encryption: Enabled with AES-CCM`，输入设备立刻出现。运维步骤写进 `docs/W103D_USER_GUIDE.md`。
- 电源键：`/etc/systemd/logind.conf` 的 `HandlePowerKey` 由服务层改为 `ignore`
  （此前按一下就真的关机，现场看起来像"盒子自己重启了"）。
- **按住不放要真的连续**：遥控器按住时发 `value 2`，而 evdev 里重复事件的 `pressed`
  是 `False`——最初的重复分支要求 `pressed=True`，于是所有重复被自己丢掉，按住只有
  第一下生效（"不灵敏"的真因，日志里 `key … []` 一串）。
- 测试 2369 → **2410**（`tests/test_local_ctl.py` 45 项 + `tests/test_local_control_socket.py` 6 项，无硬件）。
- **未做**：遥控器麦克风（厂商私有 BLE 通道 + 服务端目前没有"本地麦 → TX"通路）；
  红外（这块板注册了 `meson-ir`，但两次抓取 0 脉冲——接收头没焊）。
- 文档：新增 **`docs/W103D_REMOTE_GUIDE.md`《W103D 盒子遥控器指南》**（配对、按键表、
  kHz 输入、排障、换遥控器），站点页 `w103d-remote.html` 由 `build_w103d_guide.py` 生成，
  用户指南 §9.12 链过去。

### 出厂预置：`build-image.sh --preflash`（默认关）

- 新脚本 `packaging/common/preflash.sh`：env 里写固定 Web 口令（默认 `abcd!1234`）并显式清空
  `MRRC_AUTO_PASSWORD`、`chpasswd` 设 `root`（`abcd1234`）/ `mrrc`（`abcd!1234`）、时区 `Asia/Shanghai`、
  区域 `zh_CN.UTF-8`。按批次可用 `MRRC_PREFLASH_*` 覆盖。**不预置电台型号/串口**。
- **不传开关的镜像逐字不变**（口令照旧每台现生成）；`preflash.sh` 自己还有一道「开关没置 1 就拒绝写入」的闸门。
- 于是发货版**不需要任何初始化**：上电 → 浏览器用固定口令登录 → 完事。代价写进指南 §7：
  **固定口令就是公开知识**（Web 可在页面改，root/mrrc 需 SSH/HDMI）。

### 设置页：Wi-Fi 可手输、公网入口可申请

- Wi-Fi 面板：SSID 变成可手输的文本框（点扫描结果只是把它**带入**这个框）—— 隐藏网络与「芯片刚重启、
  扫描返回空表」这两种情况从此有出路；密码框加显示/隐藏（默认仍掩码）；空表时给一句可读提示；面板写明
  只支持 2.4 GHz。
- 新增「公网入口」面板：填呼号 → 申请 → 状态（等批准 / 已接通 + 入口地址 / 被拒绝），只调既有三个
  `/api/cloud/*` 端点；**已有令牌时按钮变「查询状态」**（门户对同呼号重复申请是查重拒绝的）。
- 已保存的 Wi-Fi 密码仍然**不回显**（NFR-068 / AD-021 不动）；该页 7 处 `innerHTML` 清空换成
  `replaceChildren()`、扫描行改 `createElement`，该页不再有 innerHTML。

### 首启探测：从「跑过一次」改成「跑到定型为止」

- `MRRC_PRESEED=1` 不再短路：它只表示「别发明口令」。**2026-10-17 对 1.28.1 产物的只读检查证实：
  镜像上的串口/声卡探测从未运行过**（口令是服务启动时生成的），`/setup` 的免密引导窗口也因此从未打开过 ——
  后者不在本次范围，已在指南 §6/§9.8 标注现状。
- 完成判据改成「配置已定型」（`needs_first_run` 为假 **且** 记下的串口节点此刻在场）：没探到电台的盒子
  每次开机再探一次。卖家工作台上那次开机（U 盘、没电台）因此不会把「已完成」克隆给用户。
- 顺带补齐 `linux/first_run.py` 相对 macOS 那份漏掉的两条：口令在探测**之前**落盘、探测循环之前先打印
  一行（卡住时 HDMI 不再全黑）。

### 记录在案的现状（本次不改）

- **`/setup` 的免密引导窗口在镜像上从未打开过**（`MRRC_AUTO_PASSWORD` 永远为空）：改它等于重新开一个
  免口令窗口，是安全决策，需要单独一次设计与验收。

### 真构建与产物验证（2026-10-17 实测）

两份镜像都真构建并跑了四步验证（预置版与对照版）：

| | 字节数 | SHA-256 |
| --- | --- | --- |
| **发货版**（`--preflash`） | 1,212,223,182 | `17f3fe797a3d6a8dabaae0d5612dfcff2305aa3f9397c05c258bfac3c744adc5` |
| 标准版（不带开关） | 1,191,513,586 | `8f743cf30a214ed20cdc5f8b14b6f8f023c98dd37ca87be75867daf288268a0b` |

预置版：固定口令 + `Asia/Shanghai` + `zh_CN.UTF-8` 均落入产物（`crypt` 复算 root/mrrc 口令为 `True`）、
型号/串口**未被预置**、D-11 单元身份、11 份 profile、`/tmp` 零残留、chroot 导入门禁全绿；
对照版全部相反（时区 `Etc/UTC`、locale `en_US`、shadow 不符）。两版都在产物里真跑了一次首启包装器：
**无电台 ⇒ 不盖 `firstboot-done`**（下次开机再探）而证书照生成。两份产物的 `gzip -t` 通过、
`gzip -dc` 的内容哈希等于各自构建树的哈希（产物与树互证）。

构建过程中被产出的两个真缺陷：
① **脚本对基底的 NTP 实现做了假定**（无条件 `systemctl enable systemd-timesyncd`，而基底是 chrony
且已 enable）—— 改成"不挑实现、但没有 NTP 单元就构建失败"（`d7af424`）；
② **两个会话并发构建同一个产物路径**，交错写入产生 `trailing garbage` 的坏包 —— 本次把验收门
加严为"`gzip -t` 通过 **且** 日志 SHA == 文件 SHA"，并只保留一份干净构建。

### 未验/边界

- **真机验收（指南 A1–A8）待做**：三条口令实测登录、`/setup` 302、手输 SSID、呼号申请与审批、
  插上电台后重探、`timedatectl` 同步 —— 本机没有盒子，读数必须回填指南与 SDD，不得以"应该可以"代替。
- **无 AP 兜底**：用户既没网线、也没人替他配 Wi-Fi、盒子又连不上他家 Wi-Fi 时，盒子够不着。
- **发货版镜像不上传下载页**：它带的是公开固定口令，公开下载等于把同批盒子的口令发给所有人。

### Wi-Fi 加入不再依赖扫描：AP 不在场也能预置（`net_wifi.connect_wifi`）

给客户代刷盒子时，客户的 AP 在工作台上**必然扫不到**，而这一点正好把旧实现打穿：`connect_wifi` 一直在发
`nmcli device wifi connect <SSID> password <PSK>`，而 nmcli 这条快捷方式是**基于扫描**的——SSID 不在列表里
就报「未发现 SSID 为 … 的网络」并**一个 profile 都不写**。实测（1.28.1 / nmcli 1.42.4，真机）：

```
$ nmcli device wifi connect ZZZ-NOT-VISIBLE-9871 password testpass123 ifname wlan0
错误：未发现 SSID 为 "ZZZ-NOT-VISIBLE-9871" 的网络。
profile 数：5 -> 5
```

同一句判断也解释了第二个症状：**隐藏 AP 永远不在扫描列表里**，哪怕它就在隔壁。`linux/wifi_preseed.py`
（开机从 `/boot/mrrc-wifi.txt` 连网）形状相同——它的全部目的就是「在看不见的时候连上」。

改为**写 profile 再 up**：`connection add` + `connection modify`（`802-11-wireless.hidden yes`、
`802-11-wireless.band bg`、`connection.autoconnect yes`、`connection.autoconnect-retries 0`）+ `-w 25 connection up`。
`hidden yes` 对广播 AP 无损（真机实测：同形态 profile **5 秒内**连上可见 AP，`freq 2422`），对隐藏 AP 是唯一出路；
`band bg` 把 2.4 GHz 钉死（MT7663S 连 5 GHz 会「关联成功但数据面不通」，~100 s 后固件不再应答扫描）；
`autoconnect-retries 0` 是 nmcli 的「无限重试」，让「AP 以后才出现」这件事成立；`-w 25` 是有界的——SSID 不在场时
nmcli 会等它自己 90 s 的默认值，而 `perform_wifi_switch` 后面只有 30 s 的 DHCP 预算。

顺带把一条既有守卫的语义钉紧：`test_the_server_never_asks_nm_for_a_secret` 原先按字面量禁掉整个
`wireless-security.psk`，于是**一个合法的写入**撞红了它。现在禁的是**读**的四种形状（`--show-secrets`、
`-g` 查 psk、`connection export`、直接读 NM 的 keyfile 目录），并把「密码只在 join 里写一次」做成断言。

测试 2327 → **2336**（`test_net_wifi.ConnectTests` 4 → 13：不碰扫描、先删后建、hidden/band/autoconnect/retries
四个字段、密码只出现一次、开放网络不写安全、`add`/`modify` 失败不下到设备、`up` 有界、失败带原因、默认接口；
`test_box_ops_wifi_switch` 的 17 处桩键由 `device wifi connect` 改为 `connection up NewNet`，语义不变）。
**真机验证**（W103D，eMMC 固化后）：同形态 profile 冷启动 5 秒自连可见 AP；客户 AP（扫不到）的 `con up`
报错落在「SSID 找不到」而不是「属性无效」——即 profile 合法、只缺那个 AP。

**还有下半段（同日第二次现场）**：面板能把「扫不到的 AP」存下来了，却**立刻把它禁用**——
`box_ops._roll_back()` 在切换失败时无条件写 `connection.autoconnect no`（旧 Hotspot 的教训：失败的网络
不该每次开机重试）。于是操作员看到的是「已保存 HG888（不自动连接）」：到了客户家里它永远不会连。
现在按**失败类别**分开：「不在范围内」（超时 / 未发现 SSID）→ 保留 `autoconnect=yes`，并把提示改成
「已保存，这个网络出现在范围内时自动连接」；「凭据被拒」（`Secrets were required`）→ 仍然禁用，因为重试
一万次也不会成功。判断只有一处出处：`net_wifi.access_point_absent()`。面板也不再把这个结果染成红色：`/api/box/wifi/status` 多一个布尔 `saved`，页面据此用绿色显示「已保存…自动连接」，而不是一条失败红。测试 +7，2327 → **2343**。

### 进不去之后还能救回来：面板改系统口令 + 预置救急 Wi-Fi

两件都是"客户把盒子弄到一个进不去的状态"之后的退路。

**系统口令可以在面板里改**（`/settings` →「系统账号口令」）：root 与 `mrrc` 两条 SSH/HDMI 口令，
走 root helper 的新动词 `set-password`（允许表里只有这两个账号，自由用户名会让这个套接字变成"变成任何人"）。
口令**只走 `chpasswd` 的 stdin**——不进 argv（`/proc/<pid>/cmdline` 全世界可读）、不进日志、不进状态字典。
真机验证：面板改完立刻用新口令 SSH 登录成功，再改回去；短口令被 400 拦在前面；
`journalctl -u mrrc-box-helper` 里只有 `set-password ok for 'root'`，没有口令本身。

**预置一条救急 Wi-Fi**（`preflash.sh`，发货通道）：SSID `rescue` / 口令 `abcd1234`
（`MRRC_PREFLASH_RESCUE_SSID` / `MRRC_PREFLASH_RESCUE_PSK` 可按批覆盖），
`autoconnect=true`、`priority=-10`、`retries=0`、`hidden=true`、`band=bg`。
**方向是盒子当客户端**——这块 MT7663S 做不了 AP（2026-10-11 真机：软件能开、信标发不出去，
手机与 Mac 都扫不到，`tx_packets` 60 秒 +2），所以救急只能是"客户用手机开一个叫 `rescue` 的热点，
盒子自己连上去"。优先级 -10 保证客户家里的网在的时候它绝不抢。

`--wifi-only` 是给单测**真跑**用的入口（配 `MRRC_PREFLASH_WIFI_DIR` 指到临时目录）：keyfile 内容、
0600 权限、口令长度与换行的拒绝，都是真跑一次脚本验出来的，不是在源码上 grep。

## [镜像线 v1.28.4] — 2026-10-19 — ☰ 菜单里的「性能负载」：一眼分清是盒子不够快，还是服务端自己卡住

☰ 菜单新增「性能负载」：点开能看到盒子与服务的 CPU、负载、内存、磁盘 IO、网络、温度/主频，
以及**服务端自己的症状**——事件循环延迟、页面连接数、TX 队列与丢弃、RX 静音时长。

判据只有一条：**先分清「盒子不够快」和「服务端自己卡住」**。CPU 不高而事件循环延迟 900 ms
是后者，换个更强的盒子不解决任何问题。所以页面顶上是一句判定（`host_perf.verdict`），
下面才是数字。

技术要点（都是这类页面最容易做错的地方）：

- **速率只能来自两次采样**：第一次返回 `available: false`，不返回 0——「0% CPU」与「还没测出来」
  在页面上长得一样的话，正好在故障时误导人。
- **进程名取自 `cmdline`**：我们的进程全叫 `python`，不看 cmdline 就分不清 `server.py`、
  `scope_pipe`、`sdr_pipe` 与 FT8 worker，而"哪个在吃 CPU"正是这一页要回答的。
- **分区不计入磁盘速率**（`mmcblk2p1` 的扇区已经算在 `mmcblk2` 里，再加一遍会得到「磁盘忙 140%」）；
  `lo` 与 `sit0`/`ip6tnl0` 不出现在网络表（服务自己的频谱与音频走的就是 `lo`）。
- **读不到的字段是缺失而不是 0**：没有温控区时不报 0 °C——那在这台盒子上读作「很凉快，继续跑」。
- 判定优先看**事件循环延迟**：它在进程内测（250 ms 的 `sleep` 实际睡了多久），外部工具看不见，
  而它正是"卡一下"的手感来源。

页面 `/Perf`（`listen`/`sdr` 会话 403，接口 401/403 双重把关），接口 `GET /api/perf`，
数据源 `host_perf.py`（stdlib only、可注入读取器、22 项单测）；SDD §9.12 与 SDD/11 已记录。

## [镜像线 v1.28.3] — 2026-10-19 — 蓝牙 HID 的两处真缺陷：残库要重启 daemon，helper 的 socket 被自己的 unit 删掉

两个缺陷都是同一晚在现场被逼出来的，且都不是「配置问题」——是代码/打包的错误。

### 蓝牙遥控器：配上、链路也通，键盘设备却建了又拆

判据是 `/proc/bus/input/devices` 里键盘设备**连续几秒都在**，不是 bluez 的 `Paired: yes`。

- **残库在内存里，不在磁盘上。** 现场抓到 bluez 自己那句
  `No attribute found within the given range`：bond 文件里 `[LongTermKey]` /
  `[PeripheralLongTermKey]` / `[SlaveLongTermKey]` 全在、加密也正常，但守护进程拿一份
  残缺的 GATT 数据库去读，读不到就拆设备（`dmesg` 里 `…0300.0002 → …0004` 一路翻号）。
  **清 bond + 控制器上下电 + 再配，连做三次都没用**；`systemctl restart bluetooth`
  一次就成，bond 原样重读。
- 所以 `net_bt.pair()` 的补救**改了顺序**：配上但按键不来 → **先**重启 bluetoothd
  （无破坏性）→ 仍然不行才清 bond 重配。这条通道走 root helper 的新固定动词
  `restart-bluetooth`（零参数，唯一 argv 是 `systemctl restart bluetooth`，
  `SO_PEERCRED` 只认 `mrrc`），服务用户自己没有 root。
- 残库是**操作造成的**：连接会话只能有一条。两个会话同时连 → `le-connection-abort-by-local`
  → 服务发现做到一半被掐断 → 内存里留半份库，之后每次重连都拆设备。文档与 skill 都记了这条。
- 订正：`ATT 0x0E` **不能**单独判成「链路没加密」——残库时它也刷，而重启后同一把密钥就正常。
  分界线是 bluez 那句 `No attribute found`。

### root helper 的 socket 会被服务端重启删掉（长期潜伏）

`mrrc-box-helper` 与 `mrrc-modern` **都声明了 `RuntimeDirectory=mrrc`**，而 systemd 在带这个
指令的 unit 停止时会**删掉整个目录**。于是一次服务端重启就把 `/run/mrrc/box-helper.sock`
删了，而 helper 继续绑着一个已 unlink 的 socket：**主机名、时区、重启、avahi 全部静默失效**
（页面只显示一句「不可用」），只有重启一次 helper 才能恢复。

- 一个目录一个主人：helper 搬到 `/run/mrrc-box/`（0770 root:mrrc），`/run/mrrc` 归服务端独占。
- 新增回归测试：没有任何两个 unit 声明同一个 `RuntimeDirectory`。

## [镜像线 v1.28.2] — 2026-10-19 — NanoVNA 天线诊断台进盒子；蓝牙遥控器配对的两个真缺陷

> **这是盒子镜像线（W103D / 树莓派）的版本，与桌面安装包线分开编号。**
> 桌面线（DMG / Setup.exe）仍是 v1.28.1：Windows 构建机尚未重建，而 `latest.json` 的
> 校验器要求 `installer.version == latest`，所以这条线不通过清单发布，只发镜像下载卡。
> 桌面代码本身已包含本条目里的内容，下一次桌面发版会一并带出。

这一版的主角是**盒子上的天线工具**与**蓝牙遥控器**，两者都是在真机上被现场问题逼出来的。

### NanoVNA 天线诊断台（`/Nano`，AD-030）

插在盒子 USB 上的 NanoVNA-H/H4 变成一台天线诊断台：量的是天线本身的**复数 S11**，
不是拿反射功率反推。1–54 MHz / 401 点约 1 秒。

- **判定卡**：段内最高 SWR → 免调可用 / 天调可修 / 勉强 / 不建议；并按「谐振频率与波段
  中心的相对偏差 ≈ 振子长度的相对偏差」直接给出修剪量（每 10 m 剪/加多少 cm）。
- **野外独立使用**：一键把设备自己配成「1–54 MHz / 401 点、只留一条 S11 的 SWR 迹线、
  网格数值、标记在中点」并写入 SAVE 槽 4。实测发现**迹线格式不在 config 里**——
  `saveconfig` 甚至固件的「记住关机前状态」都留不住，只有 `save`/`recall` 存整套状态，
  所以带出去用是「开机 → RECALL 4」。
- **基准配置**：`docs/nano/field-baseline.json` 从真机倒出，`/api/nano/baseline?compare=1`
  逐项比对扫描范围/迹线/标记/测量设置。
- **固件升级**：ROM DFU 路径（`reset dfu` → `0483:df11` → `dfu-util`），变体看机器标签
  （MS/SI/ZK），成功判据是刷后设备自报的版本；默认上锁（`MRRC_NANO_ALLOW_FLASH=1`）。
- **真机上修掉的六个缺陷**：`marker` 命令是 1 基的（传 0 不报错也不动）；状态轮询与扫描
  并发读同一个 pyserial 句柄会抢帧字节、且忙时把 `connected` 报成 false（页面每扫一次就喊
  「设备掉线」）；死句柄装在线直到下次扫描才炸；`fitCanvas` 让 Retina 屏每次刷新把画布
  翻一倍；`server.py` 的日志器叫 `logger` 而 nano 块写成 `log.`（只有真连上设备才炸）；
  `disable_all_traces` 只有调用没有定义。
- 操作手册 `docs/nano_vna.md`；SDD §9.11、SDD/11 组件清单、AD-030。

### 蓝牙遥控器：配不上 / 配上了但按键全死

- **配不上**的真因是遥控器没进配对模式，而 bluez 会把它的**名字**留在列表里（缓存）；
  更糟的是**这台遥控器的广播里没有 RSSI**，所以"等它带 RSSI 出现"这个判据**永远为假**——
  操作员把按键按烂也走不到配对。改为**直接配 + 每 5 秒重试 6 轮**（约 3 分钟窗口），
  失败时给出 bluez 原话 + 「按住 OK+返回 5 秒」。
- **配上了但按键全死**：`Paired/Bonded/Connected` 全 yes，但链路没加密，遥控器对每次
  GATT 读回 ATT `0x0E`，bluez 把键盘设备**建了拆、拆了建**（dmesg 里 `…0300.0006/0007/0008`
  序号一路涨）。判据从"bluez 说配对成功"改为**键盘设备连续 3×1 秒稳定存在**；配上但
  不稳定时自动执行文档里的补救（清 bond → 控制器上下电 → 再配一次），并把 ATT 0x0E 写进
  错误文案。`/api/box/bt/pair` 现在把 `pair()` 的原因透传给页面（此前被覆盖成一句"配不上"）。
- 现场分诊沉淀为 `w103d-bt-remote` skill；`docs/W103D_REMOTE_GUIDE.md` §2 订正。

## [v1.28.1] — 2026-10-17 — Claude Hub 云证书不再被孤儿化；FT8 的 DSP worker 不再自己杀掉自己

两个都是**已经落地但从未到达用户**的修复：云证书那一笔在 10-15 就写完并验证，却一直躺在
未合并的分支上；FT8 那一笔是上一包发完当天才定位到的。

### Claude Hub：实例证书不会再有第二把（`ssl_bootstrap.sign_for()` 变幂等）

登记流程原先**每次重签一把一次性密钥对**，而 `connect()` 只登记刚签的那一把。只要
第二次签署之后登记没成功（重试、双击「连接」、网络抖动），hub 钉住的与服务的就是两份
不同的证书——而且**重启治不好**，进程只会继续服务盘上那份更新的。现场
`https://bg7kxr.mrrc.vlsc.net/` 整夜 502 就是这个形态，nginx 报
`upstream SSL certificate verify error: (18:self-signed certificate)`。

现在同名且证书可用（CN/SAN 匹配、距到期 > 30 天、私钥与证书配对、文件可解析）就**复用且不重写文件**：
「签了但登记没成功」变成无害，之后每一次登记发的都是同一份字节。轮换成了显式动作（删掉证书
文件）。**已经坏掉的实例点一次「连接」即可自愈**——它会重新登记盘上那把，正好等于 hub 将钉住的。

### FT8：DSP worker 不再被 Fortran 的 `stop` 当场杀掉

FT8 深路径（WSJT-X Improved）每个时隙都以 `worker_exit: worker process exited with status None`
收场，快路径的结果保住了、深路径一次也没落地。根因是原生代码里有三处 Fortran `stop` 会直接
终止**宿主进程**，而 worker 是服务器的子进程：

- `wsjt_improved.f90` 的 FFTW 线程初始化 `error stop` → 改为返回 `WSJT_E_INTERNAL`，**绝不 abort**；
- 另两处（`four2avar.f90`、`packjt.f90`）仍在，但现在**能看见**了：

DSP worker 自己的 stdout/stderr 现在落到有界的 `logs/ft8-worker.log`（此前 launcher 的启动 tee
在服务器答 HTTP 后就不再写盘，Fortran 的死亡信息与未捕获 traceback 一律被丢弃，诊断包里只剩
一句 `status None`）；诊断包新增收集这个文件（含上一代 `.1`）。`worker_exit` 的日志同时带上
**第几代 + 重启次数**，崩溃循环不再看起来像一次偶发。

### 静态资源：不再每次重新加载（实例 `Cache-Control` + 条件请求；hub 边缘 gzip + 缓存）

实例此前只发 `ETag`/`Last-Modified`、**不实现条件 GET**：带 `If-None-Match` 仍返回 200 与**完整
534 KB** 的 `modules/opus_wasm.js`，所以"重新加载"等于"把 534 KB 重新推过一遍隧道"；而 hub 边缘
只压 `text/html`（nginx 默认），首屏 13 个引用合计 816 KB 原样过隧道。现在：

- 文档类（HTML/JSON/`sw.js`）`Cache-Control: no-cache` —— 部署后新版必须立刻可见；
- 其余静态件 `public, max-age=3600` —— `?v=` 是手工 bump 的，1 小时是漏 bump 的最长窗口；
  **刻意不写 `immutable`**，那会把这个代价放大成一年；
- `If-None-Match`（含 `W/` 弱校验）与 `If-Modified-Since` 命中回 **304**，不再重传文件；
- 有扩展名但文件不存在 → **404**（`/favicon.png` 此前拿到 40 KB 的 `index.html`，浏览器把它当图标
  收下），无扩展名的 SPA 路由仍回落 `index.html`。

hub 边缘（`mrrc_hub` V0.37）按同一套值加了 gzip 与 1 小时边缘缓存：只缓存 200（未登录的重定向
一旦带上 `max-age`，已登录用户会在一小时内反复被打回登录页），硬刷新带 `Cache-Control: no-cache`
时穿透回源。实测 `opus_wasm.js` 534,520 → 170,402 B（gzip），第二次起由边缘 304 作答。

设计：`docs/superpowers/specs/2026-10-17-static-asset-delivery-design.md`。
套件 2280 → **2296**（新模块 `tests/test_static_cache_headers.py`）。

### Installers

- 版本号、`version.txt`、下载页与指南全部对齐 v1.28.1。

## [v1.28.0] — 2026-10-16 — 电台电源回到菜单；出站 HTTPS 只信随包的根；FT8 的深路径第一次真正跑起来

四条各自已经落地、又各自值得单独写下来的工作合成这一次发布：**电台电源**回到 ☰ 菜单
（「重开」与「关机」是两种不可混淆的意图，唤醒改成有界可取消的持续尝试，真机 7 场景验收）；
**出站 HTTPS** 不再把信任判定外包给本机证书库；**FT8 级联**的决策截止算术错误修掉 ——
在那之前深路径在生产里一次也没跑过；以及**支持流水线**停摆六天的两个静默失效。

### 电台电源回到界面：菜单里的「重开电台…」与「关机…」

顶栏 **⏻** 依旧只管 Web 连接；电台电源放在 ☰ 菜单里，而且**分成两种意图**——
`重启电台（关闭后自动尝试唤醒）…` 关机后每 30 秒再试一次（最多 15 分钟、随时可在顶栏 `PWR` 上取消）；
`关机（保持关闭）…` 先取消救援，然后只关机、**绝不**自己上电（确认框写明远程将不可用、需到现场开机）。

这条路径 2026-07 被撤回过一次（`PS1;` 唤醒不可靠、`PS0;` 落启动窗口会把 FT-710 的 CAT MCU 卡死），
所以守卫一条不少：发射中（**含 FT8 正在发射**——原分支只查 `radio.is_transmitting`，是本次补掉的真空档）拒绝、
`PS1` 后 15 秒内拒绝 `PS0`（带剩余秒数）、`PS0` 双发、端口未连时一轮都不写。
唤醒改成有界可取消的持续尝试（每轮复用原有的 `PS1`≤3 + `FA;`/CI-V 回读校验），15 分钟没醒就明说
「需要到现场按面板开机」。协议向后兼容：`power` 的 `true|false` 语义未变，新增 `"restart"` 与 `"cancelRescue"`。

**真机验收（2026-10-16，W103D 盒子 + FT-710）**：

| # | 场景 | 实测（2026-10-16，真机：W103D 盒子 + FT-710，干净室实例 + `/dev/serial/by-id/…`） | 结论 |
| --- | --- | --- | --- |
| V1 | 关机 | `power_on=false`，电台熄灭 | ✅ |
| **V2** | **重开** | **第 1 轮醒来，耗时 4–8 秒**（日志 `Power rescue woke the radio on round 1 after 5 s`）；三次独立运行全部第 1 轮 | ✅ |
| V3 | 救援中取消 | `cancelled`，之后未再唤醒、未上电 | ✅ |
| V4 | 救援中点「关机」 | 被拒：`电台正在启动，请 15 秒后再关机`（正是防 CAT MCU 卡死的那条守卫） | ✅ |
| V4b | 窗口过后关机 | 能关掉（`power_on=false`） | ✅ |
| V5 | TX / FT8 发射中 | 未测（没抓到发射窗口） | — |
| V6 | 拔电后重开 | 未测（没有拔电；但运行期间电台 USB 真的重新枚举过一次，服务端按设计等端口回来才发 `PS1`） | — |
| V7 | listen 角色 | `power` 的四个取值全部被角色门拒绝（日志 `Listen-only client refused set power` ×4） | ✅ |
| 附加 | 单次开机（`true`） | `Power-on verified (PS1 attempt 1)` —— `FA;` 回读路径在真机上通 | ✅ |

**结论：2026-07 那条「`PS1` 唤醒不可靠（3 次/36 秒全失败）」在真机上不复现** —— 第 1 轮、4–8 秒就醒，三次运行皆然。最可能的解释正是设计时那条待复核的怀疑：旧脚本拿一个过期的 `/dev/ttyUSB*` 路径去发 `PS1`，而电台关机后 USB 会重新枚举、节点名会变；救援循环改成「等端口回来再写」，所以第一次写就落在活着的端口上。逃生舱**未触发**，两项都发布。

**真机跑出来的两个真缺陷（已修 + 测试）**：① 救援路径唤醒成功后没有更新状态 —— 日志说醒了，界面还是「关」；② `_power_on_radio` 回读确认后没有跳过一次 `power_on` 轮询，紧跟其后的 `PS;` 会把已确认的开机读回成关（单次开机那条路径本来就跳过，救援路径漏了）。

**顺带一段现场事实（与本功能无关，但设备侧值得记住）**：测试期间电台 USB 重新枚举了一次，CP2105 从 `ttyUSB0/1` 变成 `ttyUSB1/2` —— 固定写 `MRRC_SERIAL_PORT=/dev/ttyUSB0` 的生产配置当场失效（CAT 永久不通，直到改配置或重启）。已把该盒子的串口改成 by-id 稳定路径（`/dev/serial/by-id/usb-Silicon_Labs_CP2105_…-if00-port0`），并确认云入口恢复 401。

### 出站 HTTPS 只信随包的根证书：一个持续了一个夏天的缺陷的另一张脸

接入云端在一台 Windows 上报 `certificate verify failed: certificate has expired`，而同一台机器的浏览器打开同一个门户完全正常。
真相不在门户、也不在网络：**这台机器的证书库里有一份与被服务链条同名同密钥、却已经过期的副本**（Windows 会缓存见过的中间证书，
Let's Encrypt 的中间证书换代重签，缓存那份到期后正是这个形态）。OpenSSL 把库里那张放进链里判它过期，**不会**回退到服务器送来的有效副本；
浏览器用自己那套根，所以照常能开。

应用侧的错误是**把信任判定外包给了机器**：`net_tls` 只在本机库**为空**时才装随包 CA —— 与 2026-10-04 那次（macOS 库为空、
所有外发 HTTPS 全挂）是同一个缺陷的两张脸。现在改成**只信我们自己的根**（`MRRC_CA_BUNDLE` → 随包 `vendor/ca/cacert.pem`），
不再读本机库、也不读 `SSL_CERT_FILE/DIR`；只有在没有可用 bundle 时才回退平台库并告警。部署在 TLS 解密环境里的运维用
`MRRC_CA_BUNDLE` 指自己的 bundle。

**证据**：`tests/test_tls_trust_store.py` 用合成 PKI + **真握手**把这个场景离线复刻 —— 受污染的库那一侧必须真的以 expired 失败
（反向对照），我们构造的上下文必须握手成功；另加一条策略断言（结果里的根数 = 我们 bundle 的条数）。
未升级的机器继续用 `MRRC-CertDoctor.zip`（同日的免安装体检/修复工具）自救。

### FT8 级联的深路径此前从未运行过：决策截止的算术错误

**修一个让功能整体空转的算术错误**：级联解码器按策略判据升级到深路径的那条路，
在生产里**一次也没走通过** —— 决策截止 `DEFAULT_DECISION_CUTOFF_SECONDS` 是照着
**单条**解码路径量的 2.5 s，而级联需要它覆盖「快路径结果落地延迟 + 最深那条路径的
耗时」。预算守卫实际拿到的余额是 2.5 − 0.48 − 0.05 = **1.97 s**，低于深路径下界
**2.3 s** ⇒ 每一次升级都被拒绝。现场一个约 2.5 小时的窗口里累计 **263 次 `deadline_risk`、
0 次升级**，盒子一直只跑快路径；而升级率当时没有任何出口，界面上完全看不出来。

- **截止 2.5 → 4.0 s**，并把守卫减掉的那一项（快路径结果落地 **+0.48 s**）从注释里的
  散文提成具名常量 `FAST_PATH_FINISH_DELAY_SECONDS`。新增守卫测试断言
  `截止 − 快路径落地 − 安全余量 ≥ 深路径实测下界`，并做反向验证：把截止改回 2.5
  必须让守卫变红（不是装饰性断言）。
- **顺带修掉一处同源的不一致**：`LatencyHistogram` 自称桶对齐决策截止，但最后一个
  边界停在 2.5 —— 截止抬到 4.0 之后，2.5~4.0 s 的解码全掉进溢出桶、彼此不可分辨
  （一个 2.6 s 与一个 3.9 s 的读数完全一样，而它们离截止的远近截然不同）。边界只在
  **尾部**增长到 4.0（0.5…2.5 不动，历史分桶语义不变），并用「常量对常量」的测试
  把桶边界与截止钉在一起，下次改截止不会再漏。
- **观测**：新增只读 `GET /api/ft8/engine/stats`（与其它 `/api/ft8` 同一道会话门，
  只读所以不占 lease），FT8 页面状态栏每 30 s 轮询显示**升级率**与计数
  （时隙/跳过/快路径/升级/超时/错误）。升级率是判断这次修复有没有生效的**唯一**读数：
  修之前恒为 0，修之后必须非 0。用浮点而不是百分比整数 —— 0.4% 的升级率显示成 0
  就白测了。`runtime.cascade_stats` 同时增 `escalations`/`fast_decodes`。
- **文档同步**：截止的 2.5 逐字写在 `SDD/09`、`SDD/13`、级联 spec 与由它生成的网站上，
  四处全部改为 4.0 并注明理由；`SDD/09:104` 与版本历史里同名但无关的 2.5
  （ATR1000 的 meter wait）刻意不动。
- **测试**：`tests/ft8` 754 → **770** 项（767 passed / 2 skipped / 1 xfailed）。

**本次未包含**：Phase B（把 `ft8_lib` 的快路径内核按 ROI 顺序增量移植：参数 → OSD →
多趟+减除 → 精修）。它的优先级由 Phase A 的现场基线决定，计划另开。

### 自动分诊停摆六天：轮询被一个参数短路，发布失败还被记成「已发布」

答复页从 10-11 起不再更新，用户点 `#编号` 直达只看到空白。两个静默失效叠在一起，各自都能让这条链停摆。

**轮询从未执行。** cron 每 10 分钟都在跑，但命令行里那个裸 `--implement` 在 argparse 里取 `const="ALL"`——真值。
`main()` 的分支顺序把它排在 `--once` 前面，于是每轮都在「实施看板」分支直接 `return`，接收端一次都没被轮询。
`dist/support_autopilot/run.log` 从 10-10 21:20 到 10-16 21:10 共 1728 行，**没有一行**「没有新包」或「处理中」。
这六天里的五份上报（10-10 22:04、10-11、10-13、10-16 两份）全部原地堆积；再往前的 10-01、10-02、10-04、10-07
四天五份也没进过 state —— 这条链在更早的时候就已经不稳。

**发布失败被记成成功。** 修好轮询后的第一轮 cron 立刻撞上第二张脸：`cron_line()` 的 PATH 里没有 `/usr/bin`，
而 `git`／`rsync`／`ssh` 都住在那里（现场报 `FileNotFoundError: 'git'`）。更糟的是 `publish()` 先写 state 再发货——
卡片没上线，state 已是 `published=True`，而 `run_once` 只按「编号在不在 state 里」取新包，这条于是永远不会重试。
第一轮就吃掉一条：`20261001-192738-30c2` 有结论、有草稿、本地页面也写了，线上什么也没有。

**修法**：`main()` 里 `--once` 先于 `--implement` 判定（原地留注释说明为什么不能换回来）；cron 命令行删掉多余的
`--implement`；`cron_line()` 的 PATH 补回 `/usr/bin:/bin:/usr/sbin:/sbin`；`publish()` 改为发货不成功就撤回 state
记录、下一轮重试；`git_commit()` 连 `OSError`（二进制根本不在）一起当失败。

**守卫**：`tests/test_support_autopilot.py` 新增四条 —— cron 命令必须仍调用 `run_once`、cron 行不许出现裸
`--implement`、PATH 必须含系统目录、发货失败不许写「已发布」。事故当场撤回被误标的 `20261001-192738-30c2`，
其余积压交给修好的 cron 逐轮消化。

### 按发射之后进程消失、电台永远停在发射 —— PortAudio 重初始化不得在 RX 流仍开着时 terminate

现场（诊断包 `20261016-230940-40c1`，Windows 11 + FT-710）：**手机上按发射，电台一直发射不再停下来，同时服务端进程消失**。
两件事互为因果 —— 进程死了，而**所有放键路径都在进程里**：没有谁能再去写那个 `TX1;`。

根因是音频侧的进程级崩溃：TX→RX 转换要重建采集流（全双工 codec 恢复），而重建时的 PortAudio
`terminate`/`initialize` 与**仍在读的 RX 流**撞在一起 ⇒ 原生访问违例，Python 层没有异常可捕获，
进程直接消失。修法是把重初始化的时机收进一条不变量：**RX 流仍在被读取时不得 terminate PortAudio**
（先停读、再重初始化、再重开），并把这条不变量钉进测试（`tests/test_audio.py` +95 行，
另加 `tests/test_server_setup.py` 的接线守卫）。详见 SDD V2.88。

**发版当天落进来的修复，因此两个桌面产物重建过**：盒子镜像在它之后暂存，而 macOS 与 Windows 的
第一次构建在它之前 —— 四份产物最终出自同一修订（`124fb63`），并逐文件核对过镜像内的
`audio_handler.py` / `server.py` 与工作区一致。

### 打包：发版当天在产物里抓到并修掉的三个缺陷

三条都是**产物**里抓出来的，不是源码评审能发现的 —— 这正是「洁净室真跑」和「构建门禁」存在的理由。

- **干净安装里的 DXCC 库一直是空的。** 冻结包第一次真跑就在日志里留下 `cty.dat unreadable` 的回溯：
  `get_cty_database()` 走 `app_root()`（源码树是仓库根，冻结后是 `_MEIPASS`），而 PyInstaller 的
  datas 只装了 FT8 的**原生库**，没有 `ft8/data` —— 打包版的数据库因此解析到一个不存在的位置，
  loader 记一条带回溯的 ERROR 后降级为空库：**国家名与「新 DXCC」高亮静默失效**。挂载 v1.27.0 的 DMG
  复核，同样的树、同样的缺失 ⇒ 既存缺口。修法是一行 datas；守卫把 spec 的目的地与
  `dxcc.py` 里那句 `app_root() / "ft8" / "data" / "cty.dat"` 钉在一起（**反向验证**：删掉那行必须变红）。
- **镜像差点把并行会话的 venv 一起装进去。** 盒子镜像构建在 `frpc: Cannot write: No space left on device`
  处死掉，日志里一个字没提空间：暂存树里多了一份 `.venv-ml`（并行会话的 macOS venv，torch 一家 596 MB）
  —— 排除清单只有 `venv/` 与 `.venv/` 两个**名字**。修法：`.venv*/`（任一兄弟 venv），并加一条**真跑
  stager** 的守卫（临时源里放三个 `.venv*`，断言一个都没进目标树；反向验证过）。实测暂存树
  16,352 文件 / 619 MB → **430 文件 / 23 MB**。顺带记一条操作纪律：`build/w103d/w103d.img` 是**上一版叠过的树**，
  为发布构建前必须删掉，否则是在上一版之上再叠一遍（实测根分区只剩 638 MiB，`apt-get update` 当场以
  `GPG error … Splitting up … signature failed` 死掉，且不提空间）。
- **三条只在本机 POSIX 上成立的测试从未在 Windows 上跑过。** VM 门禁 3 红，全是
  `ModuleNotFoundError: No module named 'fcntl'`（autopilot 的单飞锁）与缺 bash/rsync（stage-tree 的
  真跑测试）。生产代码是**按设计**只在 POSIX 上跑（autopilot 是维护者在接收端用的工具，注释就写在
  `import fcntl` 旁），所以修的是测试：按仓库既有惯例加平台 skip（VM 上 33 → 36 个 skip），
  而不是把锁降级成假锁 —— 那等于放回它修掉的并发覆写。

**产物（四平台，2026-10-16）**：macOS `MRRC-Modern-v1.28.0-arm64.dmg` **85,038,564 bytes** `4c9f2d3c…`；
Windows `MRRC-Modern-v1.28.0-Windows-x64-Setup.exe` **79,373,225 bytes** `e27021e8…`；
W103D `MRRC-Modern-1.28.0-w103d.img.gz` **1,205,181,718 bytes** `c90dbcf1…`。

## [v1.27.0] — 2026-10-13 — FT8 走进四个平台；解码默认走快路径、按需级联

**一台电台，两种用法**：主菜单多了一项「FT8 模式」——点一下，电台切到 FT8 频点与 DATA-U，
FT8 引擎接管接收与发射；FT8 界面右上角点「←」回到 SSB，台况（频率/模式/滤波/功率/AGC/ATT/PREAMP）
逐字段恢复。两个模式共用同一条 TX 门控，**结构上不可能同时键控**。

**四个平台都拿得到**：W103D 盒子与树莓派镜像在镜像构建期就地编译原生 DSP 库，
macOS 与 Windows 安装包在打包时编译并把运行期依赖一起装进包里——刷机或装包之后直接用，
不再需要登进设备手动装编译器、手动编一遍。

- **复用时的一切**：FT8 用平台的声卡（RX 从既有 44.1 kHz 流分出 12 kHz，TX 波形走 CQ 播放同一条队列）、
  平台的 CAT（`CatController`，不装 hamlib/rigctld）、平台的登录与角色（`operator` 全权 / `listener` 只读）。
- **独立的设置**：呼号、Grid、射频模式（`DATA-U`/`USB`，默认 DATA-U）在 FT8 页面内单独设置，
  与 Cloud Hub 呼号无关。
- **真机验收**：20m 六时隙 4 解码；SSB↔FT8 往返无 PTT 残留（`docs/ft8-m5-acceptance.md`）。
- **解码默认走快路径、按需级联**：新增 `kgoba/ft8_lib` 快路径（C，无 AP/OSD/单趟，0.15 s 量级）
  作为默认；命中升级判据（通联中「AP 上下文」/ 疑似漏解 / 手动「深度解码本槽」）时对
  **同一时隙**补跑 WSJT-X 深路径并取并集，**解出条数不低于"始终走深路径"**，预算不足时
  宁可不升级。W103D 真机：解码完成时刻中位从 **+4.0 s**（仅深路径）降到 **−0.1 s**
  （快路径直出）或 **+1.2 s**（升级槽）——见 `docs/ft8-w103d-decode-perf-analysis.md` §9。
- **dt 显示与「升级过的时隙显示两遍」都修了**：快路径报的 dt 带着一个符号周期（0.16 s）与
  0.5 s 名义信号起点的偏差——页面上几乎每一行都显示 ≈ **+0.6 s**，而真值应接近 0.0
  （这是五秒就能自查的判据）；同一个偏差还让级联合并的 `(frequency, dt)` 键对不上，
  **升级过的时隙把每条信号显示两遍**。两条路径现在对齐到同一帧（含真实解码回归）。
- **快路径的信号强度是真 dB**：修掉快路径照抄上游、把归一化的 Costas 同步分当 SNR 的写法
  （≥ −6 dB 会一直显示 +17，而它会作为「收你多少 dB」发到空中）；改为从水声图量信噪比、
  并与深路径逐点标定（残差 0 dB）。
- **首版不做**：FT4、桌面客户端、band hunter、JTDX ADIF 同步、auto-call-new-DXCC。
- **升级注意（重要）**：≤1.25.5 镜像自带的 `mrrc-update` 会用 `--delete` 同步，
  升级时会删掉盒子上的 `env/`、`profiles/` 与录音——升级前先替换该脚本（或先整目录备份）。

### 打包：原生 DSP 库进入四个平台的产物

以前只有 W103D 上那份**手工编译**的库能让 FT8 跑起来：镜像、安装包里都没有它，刷机或升级之后
FT8 点了就是「库缺失」。这一版把它变成产物的一个组成部分。设计记录：
`docs/superpowers/specs/2026-10-13-ft8-packaging-design.md`。

- **两条镜像线在构建期编译**：`packaging/common/packages.txt` 增加
  gfortran / g++ / cmake / pkg-config / libfftw3-dev；`mrrc-service-layer.sh` 编完
  立刻断言 `libwsjt_core.so` 存在——编不出来是**构建失败**，而不是刷出一个 FT8 点了没反应的镜像。
  实测编译 **7.7 秒**（9 核构建机；W103D 自身 2 分 02 秒）。
- **`mrrc-update` 每次升级重编**（源码摘要未变时秒退），因此升级后不会出现「新 Python + 旧 .so」。
  这一步**不阻断升级**：1.27.0 之前的镜像没有编译器，SSB 照常升级完，FT8 单独退化并在日志里告警。
- **Boost 头文件 vendored**：`<boost/crc.hpp>` 是整棵树唯一的 Boost 用途，而 Debian 只把它装在
  `libboost1.74-dev`（138 MB 头文件）里。改为把 83 个头文件的包含闭包（720 KB，带许可证）
  落进 `ft8/dsp/vendor-boost/`：两个镜像、两台桌面构建机都不再需要装 Boost。
- **Windows**：用 MSYS2 的 mingw-w64 编译 `wsjt_core.dll`，连同 8 个运行期 DLL（FFTW、gfortran、
  gomp、quadmath、libstdc++、libgcc、winpthread，约 16 MB）一起进包；依赖闭包是**走出来的**
  而不是写死的，MSYS2 换代多出一个依赖会是构建失败而不是装机后打不开。顺带修掉两处可移植性缺陷：
  mingw-w64 根本没有 `stpcpy`（GCC 14 起隐式声明是错误，且 x64 上会截断指针），
  以及 MinGW 默认不导出任何符号（DLL 里就找不到那 5 个 `wsjt_*` 入口）。
- **macOS**：编译 `libwsjt_core.dylib`，把 MacPorts 的依赖（FFTW、gfortran、gomp、quadmath、
  libgcc_s）一起收进 app，并把每个 load command 改写成 `@loader_path/`；打包前断言
  **没有任何残留的 `/opt/local` 引用**，有就构建失败——那正是"用户机器上 `dlopen` 失败"的形状。
- **代价**：镜像安装后 +约 70 MB（构建工具链），`.img.xz` 约 +20–25 MB；Windows 安装包 +约 16 MB。
- **验收工具同步**：盒子 `verify.sh` 与树莓派 `verify-image.sh` 都加了一项——
  不只查文件在不在，而是**真加载一次**并核对 ABI 与能力掩码。
- **验证**：`unittest` **2200 项**（macOS 实测；FT8 另有独立的 pytest 套件 751 项）；
  Windows VM 门禁 **2197 项 OK（33 skip）**；macOS 与 Windows 的冻结包**都做了洁净室真跑**——
  这一步抓出两个真缺陷（全新安装没有 `<runtime>/ft8/` 数据目录、冻结包缺
  `multiprocessing.freeze_support()`，后者会让 FT8 worker 永远起不来），两侧修好后日志里都能看到
  `FT8 engine started at boot`。四个平台的原生库都**真加载并核对 ABI**（abb：`abi=2`、`decode_paths=0x7`）。

## [v1.26.0] — 2026-10-12 — 盒子会自己配网了；树莓派镜像追上 W103D；客户按一下就能叫人来修

这一版把三件互不相干的事送到你手里：**W103D 盒子刷完机不再需要 HDMI 与键盘**——接上网线，在局域网里用
浏览器把盒子自己配好（Wi-Fi、名字、时区、重启），引导热点那条路已在真机上被判死（见下）；
**树莓派镜像与盒子共用同一套服务层**，于是它一次性拿到 11 型号切换、开箱即用的真频谱、Cloud Hub 与
一键升级；以及一条**客户主动开启、限时、随时可关**的远程协助通道——客户在自己的界面上按一次，
支持者从 hub 用原生 `ssh` 进去，口令从头到尾只在客户手里。

### 远程协助（新功能）

- **客户开、客户关、限时**：应用「遇到问题」页上的一张卡，选 30/60/180 分钟开一个窗口；到点自动关、
  **不自动续期**，窗口里随时可以按「停止协助」。三条关闭路径（客户 / 到期 / 门户强制）走的是同一个
  `close(reason)`，所以关得干净——协助代理、监听端口、内存里的口令一起消失。
- **口令不在门户手里**：用户名与口令由客户端生成（也可以自己设），只存在于那个进程的内存里；
  门户的记录**结构上没有口令字段**，接口里也没有这个槽位。客户把它念给支持者，仅此而已。
- **两端都不碰公网**：SSH 只绑客户端回环，而 hub 侧的 frp 代理口本来就只绑 `127.0.0.1`，
  支持者先登 hub 再 `ssh -p <协助端口> <用户名>@127.0.0.1`。公网上没有任何 SSH 端口。
- **不提权**：会话拿到的是 MRRC Modern 自己的权限（Windows/macOS 是当前用户，盒子/Pi 是 `mrrc`）；
  盒子上需要 root 的动词仍走既有的 `box_helper`。
- 会话内是平台自带的 shell（`powershell` / `zsh -l` / `bash -l`）；认证失败 5 次即关闭整个窗口，
  连接与失败都写审计并对客户可见。操作与排障见 `docs/remote_help.md`。
- **实测**（2026-10-12，客户机 macOS + 生产门户）：门户分配端口 → 隧道内拿到 `SSH-2.0-paramiko` →
  真口令登录并跑通交互式会话 → 主隧道全程不抖 → 门户强制结束后 20 秒内断开、两端端口关闭。

### 打包：两条镜像线共用一套服务层（能力对齐）

- **排除清单从 3 份收敛到 1 份**（`packaging/common/stage-tree.sh`）：Pi 构建器在宿主侧、box overlay 在
  chroot 里、就地升级器在设备上，三处都调它。过去三份靠测试逐字节比对维持，两份分歧的后果是
  **运行期 ImportError**——现在结构上不可能分歧。
- **服务层从 2 份实现收敛到 1 份**（`packaging/common/mrrc-service-layer.sh`）：装包清单、venv、
  FTDI + udev 规则、frpc、`/usr/local/bin/*`、两个 systemd 单元、env 默认值、导入门禁、空间回收
  只有一处实现，平台差异只剩两个参数（`MRRC_WEB_HOST`、`MRRC_BOOT_DIR`），且被测试逐字钉住。
- **树莓派镜像因此拿到**：11 型号画像、内置 aarch64 FTDI 库（**真频谱开箱即得**，不再需要用户手动放库）、
  Cloud Hub 隧道客户端、`mrrc-update` 一键升级、`mrrc-radio` 型号切换、登录横幅，以及盒子那套
  首启体验（口令/串口/声卡/证书仍在首启解析，不猜硬件）。
- **树莓派构建有了国内网络开关**：pi-gen 的四个上游点（debootstrap、Debian 源、Raspberry Pi 源、
  容器自身的 apt）可在构建时改写，另有 pip 与 GitHub 代理两个开关，置空即回上游。
- 顺带修：隧道配置文件里一直有 frps 的 `auth.token`，而权限是 umask 默认（通常 0644）——本版起
  写为 **0600**。
- Pi 镜像与 W103D 镜像的四步产物验证（代码身份 / 装机内容 / D-11 接线 / arm64 原生 chroot 真跑）
  由 `packaging/rpi/verify-image.sh` 与 `docs/w103d_pack.md` 的配方分别覆盖。

### W103D 盒子（2026-10-11 真机定案）

- 镜像内置 **MT7663S 出厂 2021 版 N9 固件**（`vendor/mediatek/mt7663_n9_v3.bin`，SHA-256 构建期校验 +
  `dpkg-divert` 防 linux-firmware 升级覆盖）：基底自带的 2020 版在这块板上渐进式挂死（每 ~30 秒掉线、
  扫描楔死、~16 分钟 MCU 无响应、软重启不复位）。修复后实测 2.4G 200 包 0.5% 丢包 / 平均 6.3 ms /
  零掉线。溯源见 `packaging/box/vendor-mt7663.md`。
- 真机边界记录：MT7663S 的 AP 模式**不发信标**（初始化热点在此板不可用，首配走有线/HDMI）；
  5GHz 可关联但数据面不可用。`verify.sh` 新增第 12 项（N9 固件钉版校验）。

### Added

- **新增了局域网配置页与两条上线路**：盒子接入你的局域网后，用任意设备打开网页即可配置盒子 OS ——
  引导页 `/setup`（首次上线：设登录口令 + 选 Wi-Fi）与设置页 `/settings`（Wi-Fi 切换、有线口
  DHCP/静态、盒子名字与时区、重启），共用 `static/box_api.js`。**这两条路是本次新增的，尚待下次
  刷机验证**：eth0 能否 DHCP 到地址（R-1）与 polkit 是否真的对 `mrrc` 放行（R-2）都只能在真机闭环
  （见 spec §13），所以本条目**不声明"两条上线路均可用"**。
- **首次窗口只有密码一个键宽**：无密码写面只放行 `MRRC_WEB_PASSWORD`；判据是**状态**（盒子从未被
  用户设过口令 **且** 请求来自盒子自己的任一子网），不是标志位。
- **刷机时预置 Wi-Fi**：`/boot/mrrc-wifi.txt`（`SSID=`/`PSK=`）由独立的 `mrrc-wifi-preseed.service`
  读一次、连上后删除。**Wi-Fi 凭据与 `MRRC_*` env 分开两个文件**——env 会被复制进 `0640` 的配置层、
  可能进支持包，PSK 不能和配置键混在一起（设计 D-4）。
- **盒子名字与 mDNS**：默认主机名改为 **`MRRC-Modern`**，`avahi-daemon` 进镜像并 enable，
  `<名字>.local` 可解析（同网段撞名时 avahi 自称 `MRRC-Modern-2.local`）；DHCP 主机名作为对所有
  客户端都有效的退路。`verify.sh` 第 11 项改为查这套身份 + 提权通道。
- **提权通道**：polkit 规则放行 `mrrc` 用户写 NetworkManager（`packaging/common/49-mrrc-nm.rules`），
  外加只管四个系统动词（reboot / set-hostname / set-timezone / avahi-reload）的小 root helper
  （`linux/box_helper.py`，固定动词表、argv 数组、`SO_PEERCRED` 只认 `mrrc`）。

### Changed

- `net_wifi.py` 收缩为 **STA 半边**（扫描 / 连接 / 地址 / `-t` 转义解析）；其上的纯逻辑——网络体检、
  Wi-Fi 切换状态机、有线口 DHCP/静态、密码校验、helper 客户端——归 `box_ops.py`，两个页面共用。
  `AGENTS.md` 与 `README.md` 的静态清单同步改写。

### Removed

- **退役 setup 热点引导**：删掉 `linux/setup_ap.py`、`mrrc-setup-ap.service`、`static/manage.html`、
  向导端点与 `/run/mrrc/setup-ap/{state.json,wizard.json}` 两个跨用户信箱。原因是**硬件结论**：
  2026-10-11 真机验证**失败**——MT7663S 的驱动与 NetworkManager 都报 AP 已激活，但**信标不上空**
  （手机与 Mac 都扫不到）；换 `prefer_offload_fw=0` 会改载 `_rebb` 固件而芯片**根本不初始化**
  （`MCU initialization failed: -110`）。**AP 模式在这块板子上不成立**，一个谁也扫不到的热点比没有
  引导更糟，所以引导改走局域网页面。`net_wifi.start_hotspot()` 与相关既有条目一并作废。

### 真机验收（2026-10-12，W103D 1.26.0 首次完整走查）

U 盘路线从零刷到能用：刷盘 → 逐字节核对（`cmp` = `IMAGE_MATCH_OK`）→ 扩盘（`growpart` + `resize2fs`，
2.9 G → 227 G）→ 首启 → 口令 → 局域网配网页。结论与缺口都收进文档（指南 §4.1/§7/§9.8/§20.5、
`packaging/box/README.md`、`w103d-box` 技能的陷阱表）。

- **局域网配网页全链路跑通**：`/settings` 里扫描列出 11 个网络 → 选 `CMCC-cnham` → 输密码 → 连上。
  服务端日志 `box: switching to WiFi 'CMCC-cnham'` + `POST /api/box/wifi` 200 + 两次
  `/api/box/wifi/status` 200；NM 落地 `CMCC-cnham.nmconnection`（**0600**、`autoconnect=yes**）
  ——设计 D-4 的“口令只走 argv、不落日志”在产物里成立。
- **2.4 GHz 全绿**：关联 → DHCP `192.168.1.217` → 网关与外网通 → 拿到全局 IPv6 → `NTPSynchronized=yes`；
  全程 `mailbox 报错 0 / MCU 超时 0`。钉版固件确实生效：`N9 Firmware Version: 7663mp1827,
  Build Time: 20210308205639`，`dpkg-divert` 记录在。
- **5 GHz 是坑（新证据）**：连 `…-5G`（ch149）能关联、DHCP 地址也拿到，但**数据面是死的**；
  约 100 秒后 `Message 000500c0 (seq 7) timeout` 起、随后 `W103D mailbox write … failed: -110`
  连发，**扫描从此返回空表**。结论：**只用 2.4 GHz**。
- **“挂死必须断电”修正为“软重启即可”**：钉版固件下 `systemctl reboot` 后 **47 秒**复位
  （固件重载、报错归零、扫描恢复）。老结论来自 2020 版固件年代。
- **已知缺口（记录在案，未修）**：① 盒子线 `MRRC_WEB_HOST=0.0.0.0` 让网页只听 IPv4——
  全局 IPv6 拿得到但 `https://[<v6>]:8888` 打不开（SSH 的 `[::]:22` 正常），要 IPv6 直连网页须改 `::`；
  ② 镜像不带 DDNS 组件，重刷后 machine-id 变 → IID 变 → `w103d.vlsc.net` 的 AAAA 陈旧；
  ③ 扫描空表时 `/settings` 不给任何提示（空白列表）；④ Wi-Fi 面板没有手输 SSID 入口，
  芯片挂死时只能靠 `/boot/mrrc-wifi.txt` 预置或控制台。
- **运维教训（非代码）**：那根 U 盘上的 `/ddbr/BACKUP-arm-64-emmc.img.gz`（2.3 G，原厂 eMMC 全盘）
  是全机唯一一份回原厂路径，而重刷镜像会把它一起冲掉——**刷前先拷走**。

## [v1.25.5] — 2026-10-09 — 快捷行回答「现在在哪」；频谱带宽成为每连接可协商的档位；配置文件的写入只剩一个作者

这一版把三件互不相干的事送到用户手里：主界面那一行按钮从「按下去会变成什么」改成「电台现在在哪」；
频谱带宽从一次性的连接决定变成每个连接可协商的档位，服务端按档位分频；配置文件的写入收敛到一个作者、
读取收敛到一个读者。

### Web：快捷行显示当前值（MODE / BAND / FILTER / ATT / PRE）

五个按钮原先显示的是**按下去之后的值**，现在显示**当前值**，点击仍然轮转到下一个：

- `MODE` / `BAND` / `FILTER` 显示当前模式、当前波段、当前滤波带宽（此前显示的是各自的下一个）；
- `ATT` 显示完整档位 `OFF / 6dB / 12dB / 18dB`，`PRE` 显示 `OFF / AMP1 / AMP2` —— 此前是三字符缩写
  （`OF / 6d / 12 / 18`、`A1 / A2`）；
- 标签由服务端下发的 capabilities（`att_steps` / `preamp_steps`）派生，所以换机型不需要改前端
  （IC-7300 会显示 `OFF / 20dB`）；caps 缺失时回退 FT-710 的常量表。

语义与 Android 客户端一致：按钮回答「现在在哪」，点击回答「下一档」。点击时的乐观更新保留 —— 按下即显示新值，
服务端状态回推后再算一遍。静态资源版本随之抬升（`ft710_ui.js?v=35`、Service Worker `mrrc-v48`）：
**装完第一次打开如果还看到旧标签，强刷一次**（macOS 上 ⌘⇧R）。

### 频谱带宽档位（AD-025）：Settings 里可选 full / mid / low

高频段一直有个被当成常态的浪费：真频谱按 tick 组帧，S-meter 的回退路径每个 tick 重造一整帧 1701 B
（≈408 kbps）—— **坏消息比好消息还贵**。本版把「省带宽」从一次性的格式决定变成**每个 socket 可协商的档位**：
客户端 `onopen` 声明 `spectrumCaps`，服务端按 socket 存档并在两条路径共用的扇出闸门处分频。

- 三档 = 形状 × 分频：`full`（1701 B，每帧）/ `mid`（851 B，每 2 帧）/ `low`（851 B，每 4 帧），
  payload 口径 **151 → 38 → 19 kbps**；切换即时生效，不需要重连。
- **兼容性靠服务端闸门**：没声明 caps 的连接永远拿 `full`，逐字节等于改动前的流 —— 已装的 Android/iOS
  不受影响（Android 的解析器同时硬判帧长 1701 与首字节 `0x01`，是唯一吃不下短帧的客户端）。
- 遥测行改标 `payload≈` 口径，并新增按档位的帧计数：只看 kbps 分不清是哪一档产生的字节。
- 修掉一处静默缺陷：S-meter 回退路径原先没有帧计数闸门、在 `except Exception` 里做计量，会把健康 socket
  误判成「客户端走了」而静默摘掉。

### 配置层：env 文件一个写者、一个读者

`mrrc_modern.env` 此前有 **7 条各写各的**路径（两个平台的 `first_run`、Cloud Hub 的连接、两份 firstboot、
`install.sh`、box overlay）。它们的分歧只有现场报告才看得见：一条会把注释全丢并重排键，一条在盒子上把
`0640` 放宽成 `0644`，而没有一条能发现「自己读之后、写之前」有别的进程写了新键。

本版把它们收敛到 `env_store.py`：容错读（BOM / UTF-8 / cp936 / latin-1）+ 保注释保序的合并 +
跨平台文件锁下的**原子替换**（保留原文件的 mode 与 owner），并带两道围栏（禁写键、安全门禁）。
对策是「写不坏」而不是「写得对」：**写入不再可能截断文件或放宽权限**。Cloud Hub 的连接改走这条通道 ——
这修的是「设置页显示已连接、而服务端读不到证书路径与心跳设置」那一类静默失配。

### 服务端新增只读页 `/manage`

一眼看到两件平时看不到的事：**热点是否在跑**（在此之前答案只活在状态文件和 journal 里），以及
**三个安全闸门当前值** —— 发射门禁、PTT 上限、心跳超时，每个数字旁边写着它的后果（没有后果的数字没人会尊重）。
这一页**只读**：不提供任何网页开关 —— 这几个门禁是本系统的少量不变量，把不变量做成网页开关就是把它做成事故。

### 盒子侧（随包提供，桌面使用不受影响）

- **`/setup` 向导**：自包含、免口令只走热点网段（活心跳 + 网段双重判定），先扫描 Wi-Fi，
  AP→STA 切换**先回响应再切**、失败必回热点并带上原因；设口令复用既有写入通道、只写两个键、不重启服务。
- **`net_wifi` / `mrrc-setup-ap`**：NetworkManager 适配层与热点生命周期 —— 无上行才开热点、开放热点是有意的、
  30 分钟自关且本次开机不重开、PSK 只进 argv。

### 站点

W103D 指南独立成页（含规格图与语言切换修好）、Android 下载卡追平线上 **v1.11.30**（仓库那一半从此不再每次
全站部署都把线上卡片降级）、首页 promo 海报更新。

> 本次到达用户的方式：快捷行是纯静态资源（本可走静态补丁）；而频谱档位的服务端、`env_store`、`/manage`
> 与向导都在 **Python 侧**（`server.py` 是冻结入口脚本）—— 热修通道覆盖不到，只能随安装包到达用户。

### 边界

- **W103D 盒子镜像与树莓派镜像本次不发**（`packaging/box/`、`packaging/rpi/` 的产物留给各自的独立发布）。
- 真机验收项仍待现场：Windows 物理机上的 TX 音频（KVM 的 USB 透传破坏同步 OUT 调度，构建 VM 上**永远**
  验不了）、以及真实电台上的通联/录音验收。

## [v1.25.4] — 2026-10-06 — 隧道残留修的是 Windows 那一半；macOS 这一半的清理代码从来没跑过

v1.25.2 修「每启动一次就多留一个 `frpc`」时，改的是**枚举方式**（`wmic` → `Get-CimInstance`）。
而 `_kill_stale_frpc()` 开头那句 `if os.name != "nt": return`（2026-10-02 `697d9d2`，为「同机上的
第二个实例不受影响」加的）把整段清理关在了 Windows 之内 —— 于是 macOS 上**根本没有清理**：
启动时它立刻返回，什么都没做。

2026-10-06 在 macOS 上实测到同一个故障的第二个平台样本，而且它是**自我循环**的：一次开机之后启动
6 次，留下 6 个孤儿 `frpc`（PPID 全为 1）；当晚 21:39 清场之后只剩一个干净子进程拿到代理名，
21:40 应用再启动一次，新子进程**从那一刻起就再也注册不上** —— 挡住它的正是上一次留下的那个孤儿。
现场日志 86 行里 **67 行**是 33 秒一次的重试：

```
21:39:38 [f86c288674aa3ed1] [bg1sb] start proxy success          ← 孤儿（PID 5023）拿到名字
21:40:24 [45e8ede3fa7d80a3] [bg1sb] start error: already exists  ← 新实例从此每 33 秒重试一次
```

### 修好：macOS 上启动时真的会回收上一个隧道进程

- 枚举改走 `ps -eo pid=,command=`。**只认命令行第一个词的 basename 是 `frpc` 的进程**：用编辑器
  打开着那份 `frpc-bg1sb.toml` 的 `vim`，命令行里也有这个路径，不能把它当成 frpc 杀掉。
- 终止改走 `SIGTERM`，让 frpc 自己关掉控制连接、hub 当场释放代理名；Windows 仍然只有 `taskkill /F`。
- **永不抛出**：`ps` 的快照与 kill 之间进程可能已自己退出（`ProcessLookupError`），那是「无事可做」
  而不是失败；Windows 侧 `taskkill` 超时抛的是 `SubprocessError` 而**不是** `OSError`，两种都得接住。
  调用方是 `TunnelProcess.__init__`，异常从那里冒出去会把隧道建立、连同触发它的那个接口一起带走。

### 修好：关停时停掉隧道子进程

`server.py` 的 `lifespan` 关停半段从来没停过 `_cloud_tunnel`。frpc 是服务进程的**子进程**，父进程没了
它照样活着，把代理名占给下一次启动 —— 上面那段日志就是它造成的。崩溃或被 kill 会跳过这一步，
所以启动时那道清理必须同时成立：两层都在，缺一个故障就会回来。

> 本次修复在 `cloud_hub.py`（冻结包内的 hiddenimports）与 `server.py`（**入口脚本**）：
> 热修通道覆盖不到，只能随安装包到达用户。

### 同批到达：这一个包也是 FT-891 和「键控了却没有声音」到达用户的那个包

上面那句「只能随安装包到达用户」，说的就是这一次 —— `server.py` 与 `cloud_hub.py` 在此之前已经
改过两轮，两轮都不是热修通道够得着的东西：

- **FT-891**（SDD V2.72）：第 11 个注册键、Yaesu 家族第 5 个 profile。**实验性、仅接收** ——
  `MRRC_RADIO_MODEL=ft891` 可选，无真机验证（AD-019），发信另需 `MRRC_ALLOW_UNVERIFIED_TX=1`，
  不出频谱流。
- **发射时「键控了却没有声音」**（SDD V2.71）：会话令牌是 30 天 Cookie，一个令牌名下可以同时有
  多条 `/WSaudioTX`（页面重载留下的半开旧 socket、同一登录的两个标签页），而认领取的是**迭代顺序
  里的第一条** —— 可能正是那条已经不再送音频的旧 socket。现在按会话取**最后连接**的那条，并在收到
  第一个语音帧时把上行交给同 token 的活 socket。

判据是两条链都钉住，不是靠版本号推断：本版构建用的源码包 `dist/mrrc_modern_src.zip` 里的
`server.py`、`cloud_hub.py` 与工作树**逐字节相同**（`_claim_tx_owner_for_token`、
`_same_session_uplink_takeover` 都在里面），`backends/yaesu/yaesu_profiles.py` 也在包里；
构建机上再把安装包内 `server` 入口与源码比对，`lifespan` 的 `co_code`、`co_consts`、
`co_firstlineno` 全部一致。

### 测试

`CertificateReloadClockTests` 那两条一直是**空转**的，`tests/README.md` 还把它记成「pre-existing 失败」。
`_configured_ssl_cert()` 先读配置文件、只在文件里没有该键时才回退 `os.environ`，而它们只 patch 了
环境变量 —— 判的是开发机真实的 `mrrc_modern.env`。把 `time.monotonic()` 那个 bug 塞回去，其中一条
照样通过，而拦住它正是这条测试存在的理由。现在两条都把 `MRRC_CONFIG_FILE` 钉在临时文件上，
只判自己写下的那张证书。套件 **1642 项全绿**（1 skip）。

同一轮里还有**第二条**这样的闸门，它是被 Windows 门禁叫醒的：`CallSiteGuardTests`（v1.25.1 加的，
拦「新加的 `urlopen` 忘了带 `context=`」）用 `assertGreater(scanned, 50)` 来证明自己「确实在看出货
树」。那个 50 是照开发机 macOS 的 **53** 个模块挑的下界，而按 `win_pack.md` 的排除表装到构建机上
只有 **50** 个 —— 同一份代码在 macOS 上绿、在**出货树**上红。这不是缺陷：被排除的三个文件
（`FT710Android/` 的 meson 脚本、`promo/build.py`）里一个 `urlopen` 都没有。是闸门自己标错了，
而它红在边界上（`50 not greater than 50`）谁也看不出真伪。改成内容对照 —— 出货树里必定存在的
模块必须在扫描集里 —— 并让 `_SKIP_DIRS` 与源码包排除表对齐。变异验证：`REPO_ROOT` 指到空目录
→ 红（`'server.py' not found in set()`）；往 `session_metrics.py` 塞一处裸 `urlopen` → 红
（点名 `session_metrics.py:217`）。

## [v1.25.3] — 2026-10-05 — 按键之后的 20–70 毫秒里，自己的存活闸门可能把这次发射掐掉

Cloud Hub 打开的远端会话存活闸门（V2.63，`MRRC_REMOTE_SESSION_TX_HEARTBEAT_S`）是为了兜住
「连接没断、但人已经不在了」的半开链路。它的心跳是**按键时**发的：浏览器先发 `ptt:true`，
再回一条 `txhb`；而服务端的接收循环要等按键处理（CAT 写 + `start_tx`，几十毫秒）返回之后才会读到
那条心跳 —— 落在这一小段窗口里的检查看到的是一个「既是持键者、又已经沉默了很久」的会话，于是把
操作者**刚刚按下的这一键**当成链路失效放开。2026-10-04 的 hub 实例现场日志里记了 **7 次**这样的
误放开，每次都发生在按键后 **20–70 ms**，间隔 9.3–654 秒 —— 那些间隔恰好是那位操作者**上一次按键**
以来的时间（所以它只在相隔较久的两次发射之间出现，越像真实通联越容易撞上）。

### 修好：按键本身就是活着的证明

- `ptt:true` 的处理里，对**已经声明过能力**（发过 `txhb`）的会话刷新其存活时钟，然后再去做 CAT 写。
  这样窗口内的检查看到的是刚更新的时间，不会误判成「持键者已失联」。
- **按键仍然不等于声明能力**：从未发过 `txhb` 的会话不会被写进 `_tx_hb_capable`，保持不被门控 ——
  这是 V2.63 有意留下的负例（旧原生客户端与自带 watchdog 的 LAN 客户端不该被误杀）。
- 心跳阈值、释放路径（单次 fire-and-forget `set_ptt(False)`、无 verify 循环）与释放提示均未改动。

> 本次修复在 `server.py` —— **冻结入口脚本**，热修通道覆盖不到，只能随安装包到达用户。

## [v1.25.2] — 2026-10-04 — Windows 上每启动一次就残留一个隧道进程（hub 侧表现为日志刷屏）

装了 MRRC Modern 的 Windows 机器，**每启动一次就多留一个 `frpc` 进程**：回收上一个的代码用的是
`wmic`，而 **Windows 11 已经不再自带它**，加上那段 `except` 什么都不说，这一步就静默失效了。
两天里累积到 **六个** 进程（五个是孤儿），全都去注册同一个代理名 —— frps 只允许一个，其余每 30 秒
重试一次，于是 hub 的日志一天被刷了 **7143 行** `proxy [ba4eg] already exists`，而**这台机器
自己看不到任何异常**（症状只出现在 hub 那一侧）。

### 修好：启动时真的会回收上一个隧道进程

- 枚举改用 PowerShell 的 `Get-CimInstance Win32_Process`（强制 UTF-8 输出、不弹控制台窗口、
  非零退出即抛），不再依赖已被移除的 `wmic`。
- 路径比较按 Windows 的规则：**大小写不敏感、正反斜杠等价**（原来是大小写敏感的子串判断，
  仅大小写不同的路径会匹配不到）；且只看命令行里属于**本实例自己那份配置**的进程，
  同一台机器上的第二个实例不受影响。
- **查不到就出声**：枚举失败会记一条 WARNING，不再静默返回 —— 这个故障漫了两天没人发现，
  唯一原因就是它当时什么都不说。

### 修好：两个启动器不会再各起一个服务器

启动器原有的「端口已有人在听就不启动」探测只看得见**已经启动完成**的服务器；两次启动落在它
0.4 秒的窗口内时，双方都会认为端口是空的（现场那台机器上正好有 **2 个 launcher、2 个 server**，
各带一个 frpc）。现在启动器持有一个命名互斥（内核在进程退出时自动释放，不会像 pid 文件那样
把用户锁在外面）；探测失败或互斥不可用时**照常启动**，不会因为守卫本身坏了而拒绝服务。

> 本次修复在**冻结包内**（`cloud_hub` 在 PyInstaller 的 hiddenimports 里、启动器是入口脚本），
> 因此只能随安装包到达用户，热修通道覆盖不到。

## [v1.25.1] — 2026-10-04 — 打包版两处「只有构建机看不见」：信任库为空、真频谱找不到库

同一台 Mac 报了两件事，形状一样：**构建机上有、用户机上没有的东西被当成了常驻假设**。
两处都不影响源码运行，也不影响 Windows，因此只有「拿打包产物到别的机器上跑」才会暴露。

### 修好：所有外发 HTTPS 都在失败（接入云端 / 🐞 诊断包上传 / 软件更新）

现场报错 `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate (_ssl.c:1032)`，
而 portal 侧完全正常（`curl` 200、证书链完整）。根因在包里那份 OpenSSL：它的**编译期默认 CA
路径是构建机的 MacPorts 目录** `/opt/local/libexec/openssl3/etc/openssl/cert.pem`，用户机没有
`/opt/local` ⇒ 信任库为空。同一个原因还让 🐞 诊断包上传失败三次（日志
`Support bundle upload failed: …`）—— 那份报告包是人工递过来的，本身就是这个故障的证据。

- 新增 `net_tls.py`：默认 store 为空时依次加载 `MRRC_CA_BUNDLE` → 随包 `vendor/ca/cacert.pem`
  （Mozilla 派生，119 根）→ 平台系统 bundle；Windows 走系统证书库，源码运行行为不变。
- `cloud_hub`（接入云端）、`upgrade_core`（升级检查）、诊断包上传三处统一走 `net_tls.urlopen`，
  并有 AST 守卫：**任何直接 `urlopen` 必须带 `context=`**，否则套件红。
- 两个 build 脚本随包 `vendor/ca/cacert.pem`（缺文件即构建失败），并跑新的构建闸门
  `dev_tools/tls_trust_gate.py` —— 它**故意把 CA 环境打空**（构建机是唯一看不见这个故障的地方），
  先断言未修法确实为空，再要求与 portal / `latest.json` 真握手。

### 修好：真 FT4222 频谱从未启动，界面一直画 S 表合成曲线

现场日志 42 次 `scope_pipe exited (frames=0, connected=False)`、`Spectrum broadcast active:
S-meter fallback`，而 `scope_pipe: first frame received — spectrum active` **一次都没有** ⇒
自安装起真频谱从未工作。硬件与库都没问题（FT4222 在 USB 总线上、两个 arm64 dylib 在包内，
手工用正确目录运行立刻得到 94 帧 / 11 fps）。

- 根因：`macos/launcher.py` 只放行绝对路径 —— 随包 `default.env` 的相对值 `vendor/ftdi/macos`
  被锚到 `Contents/MacOS/`，而 v1.18.0 为可签名把数据树搬进了 `Contents/Resources/`
  （只留 `MacOS/_internal` 链接）。同一函数上方那行 `setdefault` 用的正是会回退 `_internal`
  的 `runtime_path()`，**只有这一处漏了** ⇒ scope_pipe 报 `FTDI libraries not found` 退出，
  而父进程把非 `STATUS:` 的 stderr 只记 DEBUG，现场只看得到「无输出地退出」。
- 相对值改走 `runtime_path(*parts)`，三个回归测试（`_internal` 解析 / 同级副本优先 / 绝对路径不动）。

### 别的东西

- 答复页新增**已知问题卡**（`support_answers.KNOWN_ISSUES`）：按现象检索、不带编号、仍过同一套
  隐私过滤，本次两个故障各一张。
- 套件 **1587 → 1604**（+17）；`support_bundle.detect_version()` 的 `resource_dir` 类型标注顺带修掉。

**边界**：macOS 侧只随新版 DMG 到达用户（本轮修的是应用代码 + 构建闸门）；Windows 与源码运行不受影响。

## [v1.25.0] — 2026-10-03 — 装完就能用：首次探测不会再卡死，批准之后自己接入

三条现场报障（装完黑屏、云入口 404、刷新按钮像禁用）各自追到根因；另外两处判据缺陷也是实测出来的。

- **首次启动的串口探测不再可能永久卡死。** 现场机（Windows，主机名 `MRRC`）装完 1.24.8 后启动器
  开着一块**全黑的窗口**：没有服务、没有证书、没有 `launcher.log`，进程却活着并**持着 COM3**。
  实测根因三段：① `_key()` 想靠关键词 `"cp210x"`/`"slab"` 把电台口排前面，而电台自报的描述是
  `Silicon Labs Dual CP2105 USB to UART Bridge` —— **两个都不命中**（cp2105 不是 cp210x；
  "silicon labs" 里没有 slab），于是候选保持注册表顺序 COM3/COM4/COM5；
  ② COM3 是 `Intel(R) Active Management Technology - SOL`：**open 正常、却一个字节也写不进去**
  （持口进程被杀掉后实测写入超时）；③ `probe_ft710()` 用 pyserial 的默认 `write_timeout=None`
  写 `AI0;` —— **永久阻塞**，而第一个被探测的恰恰是这个口。现在：探测传 `write_timeout=1.0`
  （写不动就跳过）；关键词改成硬件真正会写的字（`cp210`/`silicon labs`/`ftdi`/`prolific`/`ch340`/
  `usb vid:pid`…）；Intel AMT SOL 与 PnP 标识为 PCI 的口**直接不进候选**（电台一定是 USB 转串口）；
  自动生成的口令**在探测之前就落盘**（实测那次留下的配置是原封不动的模板，口令只存在于内存里）；
  探测前先打印要探哪些口（黑屏之所以无解，正是因为探测之前一行输出都没有）。
- **批准之后客户端自己接入。** 客户报障"批准了但客户端还停在'已提交申请'"：hub 侧一切就绪（用它自己
  的令牌打 `/status` 直接返回 `granted` 与全部接入信息），而 nginx 日志显示客户端**提交之后一次都没再问过**
  —— 当时唯一的轮询循环住在设置对话框里（`open()` 起 20 秒定时、`close()` 就清掉），而等批准的人不是给
  批准的人，没有理由把那个窗口开上几小时。现在轮询搬到服务端：有申请令牌就每 30 秒自己问一次，批准后
  当场完成登记（签证书 → 登记 → 写 frpc 配置 → 起隧道），不需要对话框、不需要点击；`/api/cloud/state`
  新增 `autoconnect` 字段报告"上次何时问的、结果如何"，让"根本没问"和"问了没批"不再长得一样。
- **证书判据按事实判断：进程正在服务的那张 vs 配置现在要求的那张。** 实测登记成功后
  `/api/cloud/refresh` 返回 `cert_reload_required=false`，而配置里已经是 `fullchain.pem`、进程服务的
  却是启动时的 `server.crt`。原因：`_cloud_settings()` 的键白名单里没有 `MRRC_SSL_CERT`（恒为 `None`），
  另一处读的是 `os.environ` —— 而环境是**进程启动那一刻**的，登记发生在启动之后。后果不是少一句提示：
  hub 拿登记证书校验上游，**入口会一直 502 而没有任何东西会去重启**。顺带修掉同一条判据里的时钟混用
  （`st_mtime` 的 epoch 秒与 `time.monotonic()` 直接比较，导致任何已配置证书都判成"刚写入"）。
- **「刷新状态」不再看起来像禁用。** 租户原话"这个是灰色的"，于是没点 —— 它其实能点（无 `disabled`、
  有 `cursor:pointer`），但底色与正上方的输入框相同（`#333`），读起来像不可操作的字段；而 hub 日志显示
  这个按钮发出的 `/status` 从来没到过。该面板**没有别的出路**（v1.24.0 起接入只在应用里完成，租户没有
  命令可跑）。改成描边强调色；并给 `api()` 加上 `.catch()`：非 JSON 响应现在带着**地址、状态码与重定向
  目标**报错，而不是一句没有地址的 `Uncaught (in promise) SyntaxError`。
- **裸路由不再让 API 打到 SPA 回退上。** 上面那句 SyntaxError 的根因：`basePath()` 把裸路由当成目录
  （注释却写着"与根同底"），于是页在 `/login` 时所有接口变成 `/login/api/...`，而 SPA 回退**对任何路径
  都回 index.html**（实测 200 `text/html` 36483 字节 = index.html 本身），`JSON.parse` 必然失败。
  更糟的是它会自我加深：登录页的 `next=` 能把浏览器停在 `/login/api/cloud/state`，此后每次调用多嵌一层。
  现在裸路由解析为根（顺带拆掉那个陷阱）。
- **Windows 默认监听所有接口（`0.0.0.0`）。** 原先随包模板与启动器兜底写的是 `127.0.0.1`，与
  SDD/12 的记载（`Uvicorn on 0.0.0.0:8888`）、`config.py` 的 `::`、macOS 模板和 Linux 安装脚本**全都相反**
  ⇒ 装完只有本机能打开（实测：Mac → `192.168.1.53:8888` 一直 refused，改完立刻通）。代价写在模板注释里：
  绑所有接口时 **Web 口令是唯一一道门**，而这个应用能按下发射键 —— 想只本机可达就写回 `127.0.0.1`。

## [v1.24.8] — 2026-10-03 — 重启后入口自己就通；已申请的人在界面里也能填口令

两条都是端到端实测/客户反馈逼出来的用户可见修复。

- **隧道在应用启动时就拉起来，不再等有人打开设置对话框。** 端到端验收时发现：`_cloud_start_tunnel`
  全仓只有一个调用点（`/api/cloud/state`，即打开设置对话框时），于是一台已接入的机器**重启之后**，
  入口会一直 502，直到有人点一下设置。现在应用启动时若已接入就直接把隧道拉起来（失败只记日志、
  不阻塞服务，对话框仍会重试）。
- **待批面板里补上"登记口令"输入与「用登记口令直接接入」按钮。** 客户反馈"找不到登记口令的页面"：
  口令框原先只在申请表单里，而表单一旦"已申请过"就隐藏 ⇒ 已经点过申请的人只剩一个刷新按钮，
  没地方粘运维发的口令。现在待批面板里也有（这条今天先用静态补丁 p1 下发过，1.24.8 把它焊进包里，
  新装的用户直接就有）。
- 附带：`tests/test_writable_defaults.py` 闸门进了套件 —— 任何"运行时可写"的默认路径落在程序目录里
  就让测试变红，封死 v1.24.5 那类"只在构建机上成立的默认值"。

**验证**：干净 VM 上从线上包装 → 走启动器 → 应用启动即自拉隧道（不再需要手点设置）→ 入口 302/200/401 ✓。

## [v1.24.7] — 2026-10-02 — 重启提示按事实判断（证书写入时间 vs 进程启动时间）

端到端实测（撤销 → 申请 → 批准 → 接入 → 重启）时抓到：**应用先重启、之后才完成登记**这种顺序下，
`cert_reload_required` 报 false ✗ —— 因为它比的是"导入时的文件身份快照"，而那条路径上文件确实变了、
进程也确实还在服务旧证书 ⇒ **入口 502 却没人提示重启** ✓。

改成按**事实**判断：证书文件的写入时间晚于本进程启动时间 ⇒ 必须重启才能启用 ✓。
（本地全量测试通过；线上 1.24.6 里仍是旧判据，本版修正。）

## [v1.24.6] — 2026-10-02 — 黑屏不止一个原因：启动器改为「问服务器要哪个地址」

v1.24.5 修的是「缺证书就自己签一张」。现场那台机器（主机名 `MRRC`，操作员账号 `cheen`，装的是 1.24.5）**仍然黑屏**。这次先把远程通道打开（`install_remote_access.ps1`：OpenSSH Server + RDP，公钥进 `C:\ProgramData\ssh\administrators_authorized_keys`），再拿发布产物本身取证，结论是：**同一个"黑屏"现象下叠着四条独立成因**，而 v1.24.5 那条已经修好了 —— 证据是 `C:\Users\cheen\AppData\Local\MRRC-Modern\logs\server.log` 与用包内 `MRRC-Modern-Server.exe` 的实跑：

- HTTP `/login` **200**（`text/html; charset=utf-8`，1726 B）—— 应用是**健康**的；
- 对同一端口做 TLS 握手 ⇒ uvicorn `Invalid HTTP request received` —— 浏览器拿到的是协议错误空白页；
- `could not save the generated password ([Errno 13] Permission denied: \'C:\\Program Files\\MRRC Modern\\mrrc_modern.env.tmp\')`；
- `Recording disabled: C:\\Program Files\\MRRC Modern\\recordings is not writable`；
- `open http://:::8888/ in the browser (NOT https://)`，而用户配置文件明明写着 `MRRC_WEB_HOST=127.0.0.1`；
- 日志**每一行都成对**出现，相差 1–10 ms。

**① 启动器不再猜，改成问。** 启动器按**自己**能不能生成证书决定 `https://`，服务器按**自己**能加载到哪些文件决定 TLS —— 两次独立决策、一个 socket，不一致时浏览器就被送到没人监听的 scheme 上。新增 `launcher_net.py`（探 `/api/health`，TLS 由 URL scheme 决定，`served_url()` 先问首选再问另一个），`windows/launcher.py` 与 `macos/launcher.py` **共用**（macOS 有完全相同的盲 `webbrowser.open(url)`）；切换 scheme 必须打印/通知，绝不静默 —— 证书坏了是运维需要知道的事。

**② 端口被占不再"照样成功"。** Windows 的 `SO_REUSEADDR` **不是** POSIX 那个含义：它允许第二个进程绑到**正在监听**的端口上，于是两个 MRRC Modern 同时 `Server ready!`，由 OS 把连接分给它们 —— 浏览器可能落到那个**没拿到电台**的实例。日志成对正是这个画面（两套栈各自枚举音频设备、各自开 CAT 口）。现在 Windows 用 `SO_EXCLUSIVEADDRUSE`（POSIX 保留 `SO_REUSEADDR`，启动器改配置就地重启要靠它），冲突重试约 3 s 后**抛错并说清"另一个 MRRC Modern 还在跑"**；启动器 spawn **之前**先探一次（两种 scheme 都问），已有实例就复用并说明，不再叠第二个。**这一条曾被洁净室验收抓出漏网**：守卫最初只在 `host in ("::", "")` 的预绑双栈 socket 那条路上，而显式 host 走的是 `uvicorn.run(host=...)`，于是 `MRRC_WEB_HOST=127.0.0.1`（**正是现场机的配置**）完全没有守卫 —— 打包实测第二个实例先打 `Server ready!`（app 的 startup 事件在 uvicorn 绑定之前跑）、再死于 uvicorn 的裸 `[Errno 10048]`。现在**所有 host 都走 `_bind_listener_socket()`**，绑定发生在 uvicorn 启动之前 ⇒ 绑不上端口的服务器不可能再自称就绪。

**③ 裸启动不再往安装目录写。** `_runtime_dir()` 在打包版就是 `C:\\Program Files\\MRRC Modern`（普通用户只读）。启动器会下发 `MRRC_MEM_FILE`/`MRRC_RECORDINGS_DIR`，但服务器 exe **自己也是入口**（安装菜单里就有 "MRRC Modern Server" 快捷方式，且 `_ensure_strong_password` 的帮助文本还教用户这么干）—— 于是它把可写默认值全指向自己装身的目录：每次启动生成一个新密码且存不下（⇒ **"换了新包还是登不上"**），录音整体关闭。新增 `_writable_runtime_dir()`：不可写就回落 `default_user_dir()`（`LOG_DIR`/`certs` 早就这么做了 ✓），`MEM_FILE`/`RECORDINGS_DIR`/`RECORDINGS_INDEX` 走它，`_config_file_path()` 随 `MEM_FILE` 一起搬走 ⇒ 密码真的落盘了，也终于读得到用户自己那份配置。**验收又逼出这条规则的后半截：「可写」不等于「该写」。** macOS 洁净室实测发现，裸启动会把 `recordings/` 建在 `MRRC-Modern.app/Contents/MacOS/` 里 —— 属主当然写得进去，而**往里放一个文件就让代码签名失效**（`a sealed resource is missing or invalid` / `file added: …/Contents/MacOS/recordings/probe.mp3`，删掉后又恢复 `valid on disk`）⇒ 录一段 QSO 就会让应用下次启动过不了签名校验，与 v1.18.1「数据文件放进 `Contents/MacOS` 导致 codesign 拒签」同一类事故。Windows 是镜像问题：装到可写前缀下时，状态会被重装或漫游配置文件悄悄丢掉。所以现在 **`sys.frozen` 时一律走 `default_user_dir()`，连 `os.access` 都不问**；源码检出与 Linux/Pi 安装不受影响（仍放在代码旁边）。这也消掉了裸启动与启动器启动**对同一批文件位置各执一词**的最后一种可能 —— 两个启动器本来就 `setdefault` 了 `MRRC_MEM_FILE`/`MRRC_RECORDINGS_DIR` 指向用户目录（`windows/launcher.py:153-156`、`macos/launcher.py:208-210`）。

**④ 裸启动开始读用户配置。** `config.load_user_config_into_environ()` 在任何常量计算之前跑（为此把 `default_user_dir()` 上移）。**只在** `sys.frozen` 或显式 `MRRC_CONFIG_FILE` 时生效，且只做 `setdefault` —— 显式环境变量仍然赢（Pi/systemd/CI 不受影响），`MRRC_NO_CONFIG_FILE=1` 可整个关掉。这条限制同时保住测试隔离：开发者 home 里那份真实配置**不可能**改变套件看到的值。

**⑤ 文件日志幂等。** root logger 是进程级的，冻结包会把本模块当 `__main__` 与 `server` 各加载一次，两次 `_setup_file_logging()` 就是两个 handler 指向同一个文件 ⇒ 每行写两遍（上面"成对"的另一半成因）。新增 `_already_logging_to()` 按**已解析路径**比对，同一文件只挂一个 handler；换 `MRRC_LOG_DIR` 仍照常新建。

**⑥ 常设守卫落地**（v1.24.5 条目里"建议但本次未加"的那条）。`tests/test_undefined_app_module_names.py`：纯标准库 **AST** 检查「应用模块被 `name.attr` 使用却在文件里从未绑定」，覆盖根模块与 `windows/`、`macos/`、`backends/`。它**不是**泛化的 pyflakes（名字限定在应用模块集合内，避免误报被关掉），也**故意不引入** `pyflakes`/`ruff` —— `requirements-build.txt` 现在只有 PyInstaller，加依赖会迫使构建 VM 重装 venv，而发版中途不引入新构建依赖。有效性是**实测**的：把 9f03868（装进 1.24.5 包的那版 `server.py`）喂给它 ⇒ `\'ssl_bootstrap\'`，喂 HEAD ⇒ 干净；v1.24.1 的 `cloud_hub` 同样命中。**这是同一类缺陷第二次进发布包**，守卫必须在门禁里，不能只靠 review。

**验收（本轮真正的把关环节）**：两端都**拿打包出的可执行文件跑洁净室**，而不是走查文件清单 —— 因为 v1.24.5 的教训正是"符号在包里、却没被 import"。空的 per-user 状态、不给启动器环境变量（等同安装菜单里那个 "MRRC Modern Server" 快捷方式的裸启动）：

- **证书真的签出来了**：`signed a self-signed certificate for 127.0.0.1` → `SSL enabled with a self-signed certificate just generated: …/MRRC-Modern/certs/fullchain.pem`；原始 TLS 握手报 **TLS 1.3 / Aes256 / 证书 `CN=127.0.0.1`**；`https://…/login` **200**、`/api/health` **401**，而**对同一端口发明文 HTTP 返回 0 字节** —— 这一条就是"黑屏"的机制复现。
- **裸启动读到了用户配置**：绑 `127.0.0.1:18892`（用户文件里写的），而不是默认的 `:::8888`。
- **写的是用户目录**：`Recording ready: …/MRRC-Modern/recordings`（修前 macOS 是 `…/MRRC-Modern.app/Contents/MacOS/recordings`、Windows 是 `…\dist\windows\MRRC-Modern\recordings`）；安装目录/bundle 里**没有**多出 `mrrc_modern.env`、`.tmp`、`certs\`、`recordings\`，`.app` 一次运行后 **0 个文件被改动**、签名仍 `valid on disk`。
- **日志不再每行写两遍**：单实例 26 行 / 25 distinct（Windows 19/18），唯一的 x2 是 `Opening serial port`——代码本来就打两次（初次连接 + scope-init），**不是重复 handler**。
- **第二个实例明确失败**：退出码 1，traceback 穿过 `main → _bind_listener_socket → _bind_with_retry`，报 `RuntimeError: cannot listen on port 18892: it is already taken …  Another MRRC Modern is most likely still running — close its server window (or exit it from the tray) and start it again.`，**且不再打 `Server ready!`**；`netstat`/`lsof` 上只剩 **1 个**监听者。
- **符号级走查**（`grep`/`strings` 看不进压缩的 PYZ，只能反序列化归档）：server 入口的 `co_names` 里有 `_writable_runtime_dir`/`_already_logging_to`/`_is_addr_in_use`/`_already_running_hint`/`_bind_listener_socket`/`_bind_with_retry`/`_set_bind_exclusion`/**`ssl_bootstrap`**，常量池里有 `SO_EXCLUSIVEADDRUSE`（它经 `getattr` 读取，所以是字符串常量而不是名字）；PYZ 里 `config` 含 `load_user_config_into_environ`/`boot_config_file`/`MRRC_NO_CONFIG_FILE`；launcher 入口含 `url_to_open`/`running_instance_url`/`served_url`/`other_scheme`，其 PYZ 含新模块 **`launcher_net`**（`answers`/`first_answering`/`served_url`/`other_scheme`/`/api/health`）。

**本版一共作废了三个构建**，每次都不是编译失败、而是"某一步静默地不对"：b248 把 macOS 元数据打进了包（`_internal\static\.DS_Store` + AppleDouble `._.DS_Store`，是历次源码 tar 留在 VM 树上的残渣；spec 把 `static/`、`vendor/` **整目录**塞进 `datas`，所以它们真会发给用户，且 FastAPI 的静态处理器会照请求发出去）；b249 干净了，但洁净室暴露**显式 host 绕过端口守卫**；b250 又因 macOS 洁净室暴露**冻结包往签名 bundle 里写状态**。**`Successful compile` 三次都没说明任何问题。** 顺带清掉的还有构建机上的历史遗留：VM 的 `C:\mrrc_modern` 里躺着操作员的 `certs\radio.vlsc.net.key`、含网页密码的 `.env`、**63 个真实 QSO 录音（125.9 MB）**和运行日志（都是以前的 tar 带过去的，对构建毫无用处）—— 已全部删除并核实，源码 tar 现在排除 `certs/`、`.env`、`recordings/`、`logs/`、`promo/` 与 macOS 元数据（tar 从 855 MB 降到 36.6 MB），产物内**私钥形状文件 0 个**。

**发布决定（重要）**：修复后的那次 Windows 构建（54,087,234 B `28ac7743…`）**已经以 1.24.5 的号在线上**，`latest.json` 也指过去。升级通道比的是版本串 ⇒ 那台 04:03 装了 1.24.5 的机器**永远拿不到它**，点"升级"只会反复重放同一版本。所以本轮改动**必须以 v1.24.6 发**，不能只补文档 —— 顺带说明：`server.py`/`windows/launcher.py` 是冻结入口脚本，**不在热修通道能覆盖的范围内**（`mrrc-release` 技能里那条判据），重建安装包是唯一出路。

**测试** macOS 1467 → **1533**（+66：三个新文件 47 = `test_launcher_net.py` 13 + `test_server_startup_guards.py` 25 + `test_undefined_app_module_names.py` 9；既有文件 +19 = `test_windows_launcher.py` 7、`test_macos_launcher.py` 5、`test_config.py` 7）。Windows VM 门禁 **1530 项 OK（17 skip）**。**十三处变异验证**（逐条把修复改回坏行为，对应测试必须红）：config 装载器停用 ⇒ 3 红；不可写目录不回落 ⇒ 2 红；换回 `SO_REUSEADDR` ⇒ 1 红；允许重复 handler ⇒ 1 红；启动器改回盲开自己的 scheme ⇒ win 2 / mac 1 / net 1 红；守卫的应用模块集合缩小 ⇒ 2 红；`main()` 改回 `uvicorn.run` 的 else 分支 ⇒ 1 红；显式 host 不问平台独占语义 ⇒ 1 红；显式 host 不重试不给提示 ⇒ 1 红；显式 IPv6 不设 `V6ONLY` ⇒ 1 红；去掉 `sys.frozen` 分支 ⇒ 2 红；把 frozen 判断取反 ⇒ 4 红（连两个既有可写性测试一起红，说明这组测试互相咬合而不是各守一行）；frozen 时仍按可写性返回安装目录 ⇒ 2 红。

**边界与未做**：① 安装菜单里那条 "MRRC Modern Server" 快捷方式**保留**（裸启动现在已经是正确的，但它仍不是该递给普通用户的入口；删它要动 `.iss` 的 `[Icons]`，与站点清单/文档一致后再做）；② `certs` 命名仍不统一 —— `config.py` 期望 `fullchain.pem`/`localhost.key`，`ssl_bootstrap.ensure_self_signed` 的默认是 `server.crt`/`server.key`（`sign_for()` 默认才是 `fullchain.pem`），本版**未改**，因为它不影响当前自签路径，但值得统一，已在 `win_pack.md` 记录；③ KVM 构建机无声卡，**TX 话音仍须物理 Windows 机验收**；④ 本机黑屏的真机复验（洁净安装 + 真实浏览器加载 UI）在 v1.24.6 装完后执行，结果回填本条目。

## [v1.24.5] — 2026-10-02 — 没有证书就自己签一张，而不是悄悄退回 HTTP

现场报障："装完之后黑屏、不启动"。复现出来了，是两个"只在构建机上成立"的默认值凑在一起：

- **`config.py` 把证书指向打包目录**（普通用户只读 ✗），文件名还是开发机上的
  `radio.vlsc.net.key` ✗ —— 换句话说，包里带的是一条**在任何人机器上都不存在**的路径。
- **`server.py` 找不到证书就静默降级为纯 HTTP** ✗，而启动器打开的是 **https://** 地址
  ⇒ 浏览器报协议错误 ⇒ 界面一片黑 ✓。

修法：证书目录改为**用户可写的运行目录**（Windows = `%LOCALAPPDATA%\MRRC-Modern\certs`，
macOS = `~/Library/Application Support/MRRC-Modern/certs`，Linux = `$XDG_DATA_HOME` 或
`~/.local/share`），**缺证书时当场签一张自签证书**（首次运行本来就这么做 ✓），纯 HTTP 只保留给
显式的 `--no-ssl`，并且在日志里说清楚。

- 另外：`cert_reload_required` 现在按**文件身份**（路径+时间戳+大小）比较，并且
  "启动时没有证书、之后才有"也算需要重启 —— 这正是重新登记后的情形 ✓。

**发布验收时抓到：这个修复本身在冻结包里是坏的。** `server.py` 调用
`ssl_bootstrap.sign_for()` 却**从未 import `ssl_bootstrap`** ⇒ 打包版每次启动都是 `NameError`，
被那段宽泛的 `except Exception` 吞成一条日志后**照样退回纯 HTTP** —— 也就是本版要修的黑屏
原封不动地留在了发布包里。它通过了所有既有门禁：VM 上 1460 项测试 OK、三个 PyInstaller 目标、
`Successful compile`、`version.txt` 对、SHA-256 三处一致；模块本来就在
`mrrc_modern_server.spec` 的 hiddenimports 里（**在包里，只是没被 import**），而启动器自带
一份能用的同款逻辑，所以装好的应用看起来是健康的。唯一暴露它的手段是**拿发布产物跑洁净室**：
干净的 `LOCALAPPDATA` + 不存在的证书路径启动打包出的 `MRRC-Modern-Server.exe`。

修法：补 import，并把这段逻辑从 `main()` 抽成 `_resolve_ssl_kwargs()`（内联在 `main()` 时测试
根本够不着），钉住四条契约：缺证书必须签出来（除 `--no-ssl` 外绝不返回空）、通配绑定地址要签给
`localhost`、既有证书（运维自装或 hub 登记的）绝不替换、`--no-ssl` 是唯一的纯 HTTP 入口。
套件 macOS 1463→1467、Windows 1460→1464；两端产物**重建**后才发布。

> ⚠️ 这是同一文件里**第二次**出现同一类缺陷 —— 上一版（v1.24.1）修的是「`server.py` 从未导入
> `cloud_hub`」⇒ 打包版按「申请」即 500。两次都是「模块在包里、名字没绑定」，且都被宽 `except`
> 或 SPA 兜底掩盖。**建议对入口模块常设一遍未定义名检查（pyflakes/ruff）**；本版未加，因为
> venv 里没有该依赖，而改 `requirements-build.txt` 会迫使构建 VM 重装依赖。

顺带修掉两处：`config.default_user_dir()` 在**被清空的环境**下会抛（`tests/test_config.py` 用
`patch.dict(clear=True)` reload 本模块，Windows 上 `Path.home()` 抛
`RuntimeError: Could not determine home directory`，已在 VM 实测复现）—— 路径助手不该让启动失败，
改为直接解析 `LOCALAPPDATA`/`USERPROFILE` 并兜底到 `<repo>/user-data`；
`tests/test_ssl_bootstrap.py` 用了 `unittest.mock` 却没显式 import，只在**整套跑**时靠别的模块
顺带导入才不报（单跑即 `AttributeError`）。

## [v1.24.4] — 2026-10-02 — 一个 hub、一个地址、入口就是呼号

hub 与 <www.vlsc.net> 合并到同一台机器（hub.vlsc.net）之后，"两条路"的设计没有存在理由了。

- **门户只有一个地址**：`https://portal.mrrc.vlsc.net/`（根，不再带 `/mrrc_portal` 路径）。
  配置里写着旧写法（`portal…:8899`、`www.vlsc.net/mrrc_portal`、或带路径的同名地址）的实例
  会被**自动改道**到新地址，不必逐台改配置。实测：`:8899` 其实**从未**对公网开放过——这正是
  "申请能提交、状态一直刷新不出来"的根因（申请走了边缘兜底，其余请求直连 8899 全部失败）。
- **入口就是呼号，不再挂产品后缀**：`https://bg9aaa.mrrc.vlsc.net/`。
  一个呼号 = 一台设备 = 一个入口。此前应用把产品名发成 `mrrc_modern`，与主产品名 `modern` 不等，
  于是每个入口都长了一截（`bg9aaa-mrrc-modern`）；现在两种写法都归一到裸呼号。
- **入口不再带 `:9988`**：实例 vhost 与其余服务一起在 **443** 上（`https://<呼号>.mrrc.vlsc.net/`）。
- **启动前清理本实例残留的 frpc**：实测中被杀死的应用会留下 frpc 占着同名代理，下一次启动
  注册不上，界面却看不出任何异常。
- hub 模板、部署脚本与文档同步：`:9988`/`:8899` 与 `/mrrc_portal` 全部收敛。

**验证**：干净 VM 上从线上包安装 → 申请 → 运维批准 → 刷新 → 应用自己起隧道 → 入口 302/200/401 ✓。

## [v1.24.3] — 2026-10-02 — 走 443 边缘；接入后如实告诉你何时重启；同呼号不再换入口

三处在真机上查出来的问题，都是"看起来完成了、其实没有"的那一类。

- **默认 portal 改成 `https://www.vlsc.net/mrrc_portal`（443 边缘），`:8899` 退为第二选择。**
  实测：某些链路上到 `:8899` 的 **TLS 握手会被干扰**（TCP 连得上、握手死掉），于是应用
  卡在超时里。之前"申请"能成功只是因为代码里对 8899 有回退，而状态轮询/接入同样走这条路
  却一直失败——界面就永远停在"等待运维批准"。
- **接入成功后如实告知需要重启。** TLS 上下文是 uvicorn 启动时建好的，新证书签出来也**不会**
  被正在服务的进程采用 ⇒ hub 按登记证书校验上游 ⇒ 入口 **502**（实测：服务中的指纹在重启前
  一直是启动时那张自签）。现在回执与状态里带 `cert_reload_required`，界面显示
  「还需重启应用以启用新证书」并给出**重启按钮**（`POST /api/cloud/restart`，退出后由启动器
  以新配置重新拉起——启动器本来就会在配置变化/进程退出时重启）。
- **同一呼号再次申请复用原 label 与端口。** 注册表是 `label -> port`，hub 的 nginx 路由由它
  生成；原先无条件取下一个空闲端口 ⇒ 重新申请会把已有入口指到没人监听的端口。
- **hub 模板修正**：入口到上游的证书校验必须用 `/etc/mrrc-hub/trust-bundle.pem`（系统 CA +
  各实例证书），而不是只有系统 CA —— 否则每个实例都在上游握手上报 error 18。

**验证**：干净 VM 上从线上包安装 → 申请 → 运维批准 → 刷新 → 应用自己起隧道（父进程 = server，
无任何计划任务）→ 重启后服务已登记证书 → 入口 302/200/401 ✓。

## [v1.24.2] — 2026-10-02 — 界面接入真的会把隧道起来；TOML 里的 Windows 路径

v1.24.1 让设置里的接入走到了"签证书、登记、写配置"，但**隧道从界面接入时永远不启动** —— 现场
（VM 实测）表现为入口一直 502，而租户侧看起来一切都对。

- **端点里第一次用 `TunnelProcess` 是 `None`，而没人创建它** ⇒ `connect()` 拿到 None 就跳过启动。
  现在 refresh 前先确保它存在，接着由 `connect()` 启动并托管（退出自动重启）。
- **隧道 TOML 里的 Windows 路径写了反斜杠**：TOML 的双引号串把 `\` 当转义 ⇒ `\U`（`C:\Users`）
  被当成 Unicode 转义 ⇒ frpc 报 `toml: line 7, column 15: non-hex character` 并拒绝启动 ⇒
  入口 502，而**别处没有任何错误**。改为写**正斜杠**（frpc 在 Windows 上接受，且租户脚本一直这么写）；
  新增测试断言 TOML 里不得出现未转义的反斜杠。
- **实测补充**：同一配置在"计划任务里跑脚本、由脚本拉起 frpc"的形态下会被 job object 回收
  （任务结束 ⇒ 子进程死）——应用自己托管就没有这个问题，这正是本版的做法。

**验证**：VM 上手工把这两处按同样方式修正后，`frpc verify` 通过、`login to server success`、
`proxy added`、`start proxy success`，入口 `https://bg9aaa-mrrc-modern.mrrc.vlsc.net:9988/` 返回
**401/302/200** ✓。

## [v1.24.1] — 2026-10-02 — 接入云端第一次真按就 500：那个模块谁也没导入

v1.24.0 把接入搬进了设置菜单，但**真按「申请」只会得到一个 500**。现场（打包版）的日志：

    File "server.py", line 4035, in api_cloud_apply
    File "server.py", line 3987, in _cloud_portal
    NameError: name 'cloud_hub' is not defined

`server.py` 从 `_cloud_portal()` 到 `api_cloud_refresh()` 一路调用 `cloud_hub.*`（并在 refresh 路径上
引用 `config.WEB_PORT`），却既没有 `import cloud_hub`，也只绑定了裸名 `WEB_PORT`。单测没拦住的原因是
**测试自己把那一行导入写在了开头** —— `tests/test_cloud_hub.py` 顶部 `import cloud_hub`，恰好就是
server 缺的那一行；它只驱动模块，从不驱动端点。本版因此新增 `tests/test_cloud_endpoints.py`：直接调
`api_cloud_apply` / `api_cloud_refresh`（伪造 request、打桩 portal），并把 `cloud_hub` 写进
`mrrc_modern_server.spec` 的 `hiddenimports`，让冻结包不可能再缺这个模块。

- **默认入口改回 hub 主路** `https://portal.mrrc.vlsc.net:8899`，海外边缘降为**退化路**。2026-10-01 晚
  实测（国内家宽）：主路 3/3 通、0.30–1.40 s；边缘 6 次里 3 次挂到客户端放弃 —— 这正是 hub SDD §12.8
  记录的间歇故障（边缘 → hub:9988 的 IPv6 上游超时），而应用此前默认就走它。
- **只有"没送达"才换路**：DNS/TCP/TLS 失败、超时、代理 502/504 → 回落边缘；portal 只要答复过（含拒绝）
  绝不重发 —— 重试不会把一次重复申请变成两条。第一跳预算 8 s（可达时 ~1.4 s 即答），因此只放行
  80/443 的网络不会把整个超时耗在主路上。
- **`GET /api/cloud/state` 被 SPA 兜底吞掉**：`@app.get("/{path:path}")` 注册在云端点之前，于是这条 GET 被
  首页接走（200 + HTML）—— 设置对话框因此永远读不到自己的状态；POST 不受兜底影响，所以只有"申请"报 500。
  改为在所有 API 路由**之后**用 `app.add_api_route` 注册兜底，并加两条路由顺序守卫（一条针对 state，
  一条对任意 `/api/` 路由通用）。**这条是冻结包真跑冒烟抓到的**：包起来后 `GET /api/cloud/state` 回了 HTML。
- `connect()` 把 portal 发来的非数字端口变成一句可显示的错误，而不是未捕获的 ValueError。

**hub 侧同一批修复（`../mrrc_hub`，已部署）**：`POST /apply` 的应答此前**从不交出申请令牌**，
于是应用拿不到它、`/status` 也就无从开始 —— 真机联调时在这一步报 "portal did not return a request
token"。原因与本次客户端同源：portal 的测试从 store 直读令牌，绕过了唯一真实的取令牌路径（HTTP 应答）。
修法见 hub SDD V0.18（含部署与公网实测记录）。

**验证**：套件 1461 项全绿（本版新增：端点 2、路由顺序 2、路径回退 4，另把 `_RecordingTunnel` 改成真继承
`TunnelProcess` 以消掉三条类型告警）；`release_check.py` 离线 0 failing；**macOS 产物级**：`codesign --verify`
通过、`version.txt = 1.24.1`、麦克风权限键在、DMG 经典布局，且**走查冻结包字节码**证明 `server` 入口引用
`cloud_hub`（PYZ 1070 个模块里 `cloud_hub` 在）—— 最后把包**真跑起来**：登录后 `GET /api/cloud/state`
回 200 + JSON（修复前回的是首页 HTML）、server 输出 0 个 Traceback。

## [v1.24.0] — 2026-10-02 — 接入云端搬进应用：设置里申请 → 后台批准 → 应用自己接好

租户侧过去是一份 shell 脚本：把令牌和一次性口令粘进终端、跑脚本、再把一条 root 命令交给运维。
本版把它变成设置菜单里的三个按钮 —— **填呼号 → 申请 → 等批准**，批准一到应用自己完成接入。

- **设置 → 接入云端（Cloud Hub）**：填呼号（+ 可选联系方式）提交申请；界面显示等待状态并每 20 秒
  轮询；批准后自动完成，界面给出入口地址与隧道状态。
- **应用自己做这些**（`cloud_hub.py`，只用标准库、可单测）：用内置 `cryptography` 签
  `<标签>.mrrc.vlsc.net` 自签证书 → 登记公钥 → **把证书路径写进启动器每次都会读的那个配置文件**
  （`MRRC_SSL_CERT/KEY`，因此重启后依然生效）→ 写隧道配置并托管 frpc（退出自动重启）。
- **Portal**：新增 `POST /status`（申请方凭申请令牌查询自己的申请）；批准后随接入信息一并下发
  **frps 令牌**（租户拿不到运维密钥；令牌来自服务账号可读的一份副本，服务读不到 root 的文件）。
  未批准时接入字段一律为空，不泄露半个字段。
- 顺带把今晚在真机上查出来的客户端问题一并发布：登记默认走 **443 边缘**（实测国内家宽到 hub IP 的
  TLS 在所有端口都失败）、脚本的 **UTF-8 BOM**（无 BOM 时 PS 5.1 按 GBK 读会吃掉续行）、
  应用未运行时由脚本用带环境的方式启动、以及"变量名紧跟全角字符"的两处 shell 修复。

**验证**：新增 4 个单测覆盖"证书名字必须是入口名""隧道 TOML 不许有 BOM""配置文件要保留原有行"
"未批准前什么都不许写"；本仓全量测试在本机通过。

## [v1.23.3] — 2026-10-02 — 接入脚本对"真租户"友好：重启应用、重启隧道、说清权限错误

三处都是"装完真跑"才会遇到的，来自一次真实的**重装**验证：

- **脚本写完环境变量后重启应用**。已经在运行的应用继承的是启动时的环境块（从开始菜单启动 =
  Explorer 的环境）⇒ 它继续用自己那张 `localhost` 证书 ⇒ hub 报
  `upstream SSL certificate verify error`、入口永远 502，而租户侧一切"看起来正常"。
- **重跑脚本会重启隧道任务**。升级应用时安装器会关掉 frpc（它住在应用目录里），而任务是登录时
  启动 ⇒ 不自己回来。脚本本来就承诺"可重跑"，现在这句承诺成立。
- **`$FleetDir` 不再取 param 默认值里的 `$PSScriptRoot`**：从 PowerShell 窗口里调用它时该变量为空
  ⇒ `Join-Path $FleetDir "frpc.exe"` 报"参数 Path 为空字符串"，指向一行看起来无辜的代码。
  改为在脚本体里逐级回退（`$PSScriptRoot` → `$MyInvocation` → 当前目录）。

**hub 侧**：登记端点的 `PermissionError` 不再返回 **409**（会被理解成"你提交的东西有冲突"），
改为 **500** 并在响应体里写明是 hub 侧写入失败 —— 这个误导在 2026-10-01 的排障里花掉两轮。

## [v1.23.2] — 2026-10-02 — macOS 安装包也带上接入件（与 Windows 的 fleet\ 对等）

v1.23.1 的 Windows 包里有 `fleet\`（frpc + openssl + 接入脚本 + openssl.cnf），macOS 的 DMG 里
**什么都没有** —— 也就是说 macOS 租户**拿不到**接入脚本，除非自己去 hub 取。本版把它补齐：

- `build.sh` 现在把 `packaging/payload/darwin-arm64/` 装配进 `Contents/Resources/payload/`
  （**不放 `Contents/MacOS`**：那里只许有可执行文件和符号链接，否则 codesign 会把数据目录当作
  未签名的代码对象而拒绝签整个 bundle —— 这是本仓实测过的红线）。
- 缺 payload 即**拒绝出包**（`MRRC_ALLOW_MISSING_PAYLOAD=1` 可显式放行，与 Windows 同规矩）。
- 目录里附一份 `README.txt`，直接告诉租户跑什么命令。
- 用户指南新增 §11.5「接入 Cloud Hub」，写明接入件在包内的位置与幂等重跑。

验证：装配后四个脚本与 `mrrc_hub/deploy/` 的源**逐字节一致**，且 `codesign --verify` 仍为
`valid on disk`（数据放 `Resources/` 没有破坏签名）。

## [v1.23.1] — 2026-10-02 — 一键接入真机上跑通：5 个只在"装完真跑"才现形的修复

v1.23.0 的包**装得上，但接不进任何入口** —— 单元测试、构建门禁、产物哈希当时全绿。以下 5 个
缺陷全部由"在干净 Windows VM 上装线上包、按租户的方式跑一遍"发现，并已在真机验证修复：

- **内置 openssl 找不到自己的配置**（`Can't open …/etc/ssl/openssl.cnf`）：msys2 的 openssl 按
  **编译前缀**找默认配置，租户机上没有那个前缀 ⇒ 签不出实例证书。现在随包带 `openssl.cnf` 并由
  安装脚本显式 `-config`；缺文件时明确报错，不再回退。
- **shell 侧用了 `-addext`**：OpenSSL 3 有、**LibreSSL 没有**，而 stock macOS 的 `/usr/bin/openssl`
  就是 LibreSSL ⇒ 改用两边都认的 `-config` 形式。
- **PowerShell 5.1 把原生程序的 stderr 当致命错误**（openssl 的进度点 `+++…`）⇒ 脚本在签名一步
  中断。原生调用周围降为首选项 Continue，成败仍由 `$LASTEXITCODE` 判定。
- **计划任务主体写成 `USERDOMAIN\USERNAME`**：非域机器上等于 `WORKGROUP\user` ⇒
  `HRESULT 0x80070534`。改用 `COMPUTERNAME\USERNAME`。
- **隧道配置带 BOM**：PowerShell 5.1 的 `Set-Content -Encoding utf8` 会写 BOM，而 frpc 的 TOML
  解析器直接拒绝（`invalid character at start of key`）⇒ 隧道永远起不来、入口永远 502，且**别处
  没有任何错误**。改用 `-Encoding ascii`（TOML 本就是纯 ASCII）。

**同时修复的还有 hub 侧一处配置默认值**（写错会让每个新租户都 502）：`gen_hub_routes.py` 过去在
注册表未写 `tls_name` 时按固定旧名 `radio.vlsc.net` 校验，而新式自签租户的证书是签给**自己的入口名**的
⇒ nginx 报 `upstream SSL certificate does not match`。现在缺列即用自己的入口名，老实例显式写明旧名。

**验收证据**（在一台干净 VM 上，从零开始）：签出 `CN=<呼号>.mrrc.vlsc.net` → 登记 200 → hub 落盘 →
信任包与 map 正确 → frpc `login success` / `start proxy success` → 应用 TLS 出示的证书指纹与登记的一致
→ 公网入口 `401`（= hub 认了这张自签证书）。前 7 项全绿也**不足以**说明包能用，这一条已写进
`packaging/README.md` 的验收表。

## [v1.23.0] — 2026-10-02 — 多客户端 PTT 仲裁 + Cloud Hub 前置件齐备（一键接入可用）

- **修复两个全控客户端互相掐键**：现场日志（2026-09-25 20:00–20:05）显示 5 分钟内 75 次 TX 会话、
  16 次一个麦克风帧都没有、三次释放间隔仅 30ms —— 每个浏览器标签页的 PTT 看门狗只看本地
  `tx_status`，分不清「我的释放没生效」和「别的客户端刚合法键控」，空闲标签页的看门狗会把
  正在发射的标签页 unkey 掉。现在服务端记录键控者（`_ptt_key_ws`)：持键期间只接受它自己的释放，
  外来 `ptt:false` 被忽略并回推权威 `tx_status` + `ptt_keyed_by_other`，浏览器看门狗收到即停
  （`PTTManager.cancelWatchdog()`)；键控者掉线时即使还有其他客户端在线也立即强制 RX（防僵尸键控）。
  接管仍然允许：任何客户端 `ptt:true` 即成为新键控者。
- 缓存版本：`ft710_main.js?v=34`、`ptt_manager.js?v=14`、sw `mrrc-v37`。

## [v1.22.0] — 2026-09-30 — Cloud Hub 前置能力（路径前缀 / 令牌传输 / 会话遥测 / PTT 活性闸门）

### ☁️ Cloud Hub 前置能力（实例可被云端入口接入）

- **路径前缀**：`FT710Settings.basePath`/`.url` 成为唯一前缀来源；`index.html` 资源相对化、内联 API/WS 前缀化，
  以及 **TX 编码 Worker 的前缀逃逸修复**（此前症状是 `frames=0`，表现为"按下不发射"，很容易误判成音频问题）。
  守卫：`tests/test_path_prefix.py`（断言规则：文档资源相对、`sw.js` 预缓存绝对）。
- **令牌不再进 URL**（AD-024）：cookie/Bearer 优先，query 形式仅兼容并会告警；URL 会进访问日志与浏览器历史。
- **会话遥测**（AD-023）：`GET /api/session_metrics` + 周期日志，供 Hub 侧容量决策（RX 扇出）用真实数据而非估计。
- **PTT 活性闸门**：`MRRC_REMOTE_SESSION_TX_HEARTBEAT_S`（默认 0 = 关闭；Hub 模式建议 3–5s），与
  `MRRC_PTT_MAX_TX_SECONDS` 构成两条独立防线，用于客户端失联/假死时自动释放 PTT。
- **打包注意**：新增了 Python 模块（遥测）并改动 `server.py`/`config.py`，**落在 PYZ 里 ⇒ 必须重发安装包**，
  不能只发热修；若希望它可热修，需把新模块加进 `packaging/pyinstaller/mrrc_server.spec` 的 `_APP_MODULES`。
- **缓存版本**：`index.html` 各资源 `?v=` 全部 +1、`listen.html` +1、sw `mrrc-v42`（前端有改动，不升缓存号则已装用户看不到变化）。

## [v1.21.0] — 2026-09-25 — iPhone 开机锁屏竞态修复 + 收听页增强（FFT 迹线 / 频率步进 / 公网缓冲）

**修掉 iPhone 开机后仍约 30 秒黑屏的最后一处竞态；收听页补上 FFT 迹线、频率步进与公网抖动缓冲。**

### 📱 iPhone 主控界面锁屏竞态修复

- 修复开机后 wake lock 被麦克风权限框冲掉：开机时 iOS 系统级权限弹窗会遮挡页面并
  释放刚拿到的屏幕锁，而无手势的自动重申请在 iOS 上必被拒（表现为开机 ~30 s 仍黑屏，
  手动点 ☀ 后才正常）。现在权限框结束后立即用残余激活窗口重申请，并新增 window
  `focus` 事件兜底重申请；`WakeLockMgr.enable()` 已持锁时直接返回不再重复申请。

### 🎧 收听页增强（`/listen`）

- **FFT 迹线**：瀑布上方新增频谱曲线（网格 + EMA 平滑 + 琥珀迹线与渐变填充，无频率刻度——
  收听角色本就不能改 span）。
- **频率步进**：◀/▶ 单步、◀◀/▶▶ 五倍，步进钮循环 10 Hz–25 kHz（30 kHz–75 MHz 边界内）。
- **公网抖动缓冲**：RX jitter buffer 由局域网取向的 120/300 ms 加大到 500/250/1500 ms，
  公网链路收听不再卡顿。
- 公网入口后端由 IPv6 字面量改为 DNS 名 `radio.vlsc.net`（AAAA）：nginx 每 300 s 自动
  重解析，家中 IPv6 变化不再断服务（`deploy_listen_proxy.sh` 重跑即生效，已部署）。

## [v1.20.0] — 2026-09-25 — 收听专用界面（独立收听密码）+ iPhone 主控锁屏修复

**给访客开一个「只能听」的入口：可调频率与模式、能听音频看瀑布，但发射与设备设置全部被服务端拒绝；同时修掉 iPhone 用主控界面约 30 秒自动锁屏。**

### 📱 iPhone / iOS 主控界面修复

- 修复 iPhone 主控界面约 30 秒自动锁屏：自动申请 Wake Lock 之前只监听
  `touchstart`/`pointerdown`/`keydown`，而 WebKit 不把这些算作有效用户激活，
  `wakeLock.request()` 被静默拒绝（`/listen` 正常是因为它在「开始收听」的 click 里申请）。
  自动申请增加 `click`/`touchend` 监听（点 ⏻ 开机即触发），请求被拒时用静音媒体兜底，
  `visibilitychange` 与 30 s 周期刷新路径同样补兜底。
- 诊断主屏幕图标（PWA）模式防锁屏失效：WebKit bug 254545 —— 主屏幕 Web App 里
  `wakeLock.request()` 必被拒绝，直到 iOS/iPadOS 18.4 才修复。申请被拒时控制台记录
  具体错误名/原因；iOS <18.4 的主屏幕模式下弹出一次性提示，建议改用 Safari 直接打开
  或升级系统（静音视频兜底在该模式下也不可靠，见 bug 内 2025 年现场报告）。
- 主控界面开机即激活 iOS 音频会话：手机端点 ⏻ 开机时借一次麦克风权限（立即释放，不录音）
  把 iOS Safari 的音频会话切到 play-and-record，RX 不再需要等第一次 PTT 才出声；
  同时提前拿到麦克风权限，首次发射不会再中途弹权限框。仅 iOS 触发，带 10 s 超时防卡死。

### 🎧 Listen-only 模式 `/listen`

- 新增可选环境变量 `MRRC_LISTEN_PASSWORD`（默认空 = 不启用）：用收听密码登录后进入独立的
  `/listen` 收听界面 —— 大频率显示、直接输频/频段快捷键、模式切换、记忆频道调用、S 表、
  频谱瀑布、RX 音频与浏览器端音量。
- **只允许调整频率和模式**：发射（PTT/TUNE/CQ）、录音、设备设置、记忆写入等全部被
  **服务端**拒绝（`/WSradio` 角色门 + `/WSaudioTX`/`/WSatr1000` 对收听 token 直接断开 +
  REST API 只读），不依赖前端隐藏按钮。
- 收听密码与操作员密码互不影响；两密码相同时按操作员（全控）处理。
- 公网入口：`https://www.vlsc.net/mrrc_modern/listen` —— www 主机 nginx 反代到电台服务器
  （DNS 名 `radio.vlsc.net` 走 AAAA/IPv6，无 SSH 隧道），由 `deploy_listen_proxy.sh` 幂等部署；
  nginx 每 300s 自动重解析，家中 IPv6 变化不再影响服务。
- **iPhone/iOS 支持**：点击「开始收听」激活音频；iOS 上借一次麦克风权限激活扬声器输出
  （不录音）；Wake Lock 防止收听中自动锁屏断音；页脚显示在线人数；
  单屏紧凑布局，记忆频道 3×2 网格一键直达。

## [v1.19.0] — 2026-09-23 — ATR-1000 驻波超阈值自动调谐 + QRP 学习（对齐兄弟项目 mrrc V5.8.0/V5.8.5）

**大驻波天线上发射时，天调会自己动手了。** 之前只有手动 TUNE 按钮；现在操作者不必停下正在进行的 QSO。

### ⚡ 驻波超阈值自动完整调谐

- 发射中（**实测功率 ≥5 W**）SWR 严格 >2.0 持续 ≥1.5 s → 自动向 ATR-1000 发一次完整调谐（`mode=2`）；
  30 s 冷却；同一频点连续 3 次无改善则放弃，直到频率变化或 SWR 回落到阈值以下。
- **绝不自行键控电台**：守卫只发天调帧，除操作者已在发射外不做任何动作（模块内不存在 CAT/PTT 引用，
  有源码护栏测试钉住）。
- 调谐完成后比对：**SWR 改善 ≥0.02 且终值 ≤1.8** → 把继电器值写回学习库（下次回该频点直接可用）；
  无改善**不回滚**（回滚到已知差的匹配比调谐结果更糟）。
- 前端复用既有 toast 通道：触发时「ATR: SWR x.x 自动调谐中…」，结束报成功/无改善/超时/已放弃/中断；
  期间 TUNE 按钮显示 `···` 并禁点，与手动 TUNE 互斥。

### 🎯 学习门限改为实测功率（QRP 友好）

- 学习入口由「服务端知道 TX」改为「**实测功率 ≥3 W**」（旧门限 5 W）—— 用**面板直发 PTT 或外部软件发射**
  也会学习；idle 漏读约 1–2 W 不会误学。

### 🔧 其他

- 服务端：自动调谐事件转发到 `/WSatr1000`；手动 TUNE 增加「天调正在调谐」前置检查（关掉 `tx_status`
  轮询 ≤500 ms 滞后的竞态窗口）。
- 前端 `atr1000.js` 缓存版本 +1（`?v=2`）。
- 测试 1299 → 1330（+31）；SDD §9.8 新增第 4 条联动行为、第 15 章补不键控边界、版本历史 V2.57。

## [v1.18.1] — 2026-09-18 — macOS 装机两处致命修复：「已损坏」与 RX 无声

**这是一个应当升级的补丁版本。** v1.18.0 的 macOS 包有两个只在**安装后**才暴露的问题：应用报
**「已损坏，无法打开」**（右键打开也绕不过去），以及装好之后**远程接收完全没声音**。
两者都不是电台或配置问题，而是**打包层面的缺陷**，本版修掉并加了防回归门禁。

### Installers / macOS

- **签名从来没成功过**：`Contents/_CodeSignature` 始终缺失，`codesign --verify` 报
  `code has no resources but signature indicates they must be present`。根因是 codesign 会把
  `Contents/MacOS`、`Contents/Frameworks` 下的**数据文件与目录**当成"必须已签名的嵌套代码"，从而拒绝签整个 bundle；
  而构建脚本把这次失败写成了**警告**（`non-fatal`），于是坏签名一路发出去 —— **v1.17.0 的 DMG 实测同样如此**。
  现在数据树放在 `Contents/Resources/`（资源位置会被封存而非被当作代码检查），只保留
  `Frameworks -> Resources` 与 `MacOS/_internal -> ../Resources` 两条符号链接；**签名/校验失败即中止构建**，
  `spctl` 报 `damaged` 也中止（对 ad-hoc 包，`rejected（无 Developer ID）`是正常结果 —— 右键打开即可）。
- **音频输入权限缺失 → RX 静音**：打包的 `Info.plist` 没有任何权限说明键，系统日志原文
  `tccd: Refusing authorization request for service kTCCServiceMicrophone … without NSMicrophoneUsageDescription key`。
  macOS 把**音频输入**（电台 USB CODEC）归入麦克风权限类，缺键时 **CoreAudio 照常让捕获流打开成功、却把缓冲填零** ——
  日志里只有 `peak=0.0%` 和"检查电台 AF 增益"，最难察觉的一类故障。现在 `Info.plist` 带
  `NSMicrophoneUsageDescription`（构建期断言，缺键即中止），近静默警告也会在打包版 macOS 上直接指向
  `系统设置 → 隐私与安全性 → 麦克风`。
- **首次启动需要你点一次「允许」**：旧包连询问都不会弹，所以**必须换新包**。ad-hoc 签名下授权绑定当次构建，
  今后每次升级都会重新询问（要免除需 Developer ID 签名）。
- **两个启动器**新增 `runtime_path()`（先 `app_dir()`，再回退 `_internal/`），使上述运行时文件可以住在资源树里。
  Windows 包同步重建（启动器改动跨平台共享）。

### Fixed

### Fixed — macOS 签名（发布阻断级）

- **打包版 macOS 应用签名实际从未成功**：`Contents/_CodeSignature` 缺失，Gatekeeper 因此对下载到的应用报
  **「已损坏，无法打开」**（该提示**不能**用右键打开绕过）。**v1.17.0 的 DMG 实测同样如此**，即这不是本版引入，
  而是长期存在、随每次发布发出的缺陷。根因是 codesign 会把 `Contents/MacOS`/`Frameworks` 下的**数据文件与目录**
  当嵌套代码，从而拒绝签署整个 bundle，而 `build.sh` 把这次失败写成了警告。
- **已定位可用布局**：数据树放 `Contents/Resources/`，仅保留 `Frameworks -> Resources` 与
  `MacOS/_internal -> ../Resources` 两个符号链接；该布局下签名通过、`spctl` 只报"无 Developer ID"（可右键打开）、
  冻结服务实跑正常。**剩余一步**：启动器对 `macos/`、`version.txt`、`mem_channels.json`、`vendor/`
  需回退到 `_internal/` 查找，之后重建 DMG。
- **build.sh 已加硬门禁**：签名/校验失败或 `spctl` 报 damaged 即 `exit 1`（此前是静默警告）。
- **用户侧立刻解封**（v1.17.0/v1.18.0 均适用）：`sudo xattr -dr com.apple.quarantine "/Applications/MRRC Modern.app"`。

### Fixed — macOS 装机后 RX 无声（音频输入权限缺失）

- **症状**：本地装好应用后，网页里"打开远程接收"**完全没声音**。日志里 RX 捕获流**打开成功**（`RX audio started: [3] USB Audio Device … 44100Hz`），
  却报 `peak=0.0%`，并提示"检查电台 AF 增益 / USB 连接"——把人引向电台，而电台是好的。
- **根因（系统日志原话）**：`tccd: Refusing authorization request for service kTCCServiceMicrophone … without NSMicrophoneUsageDescription key`。
  打包的 `Info.plist` **没有任何权限说明键**。macOS 的"麦克风"权限管理的正是**音频输入**（电台 USB CODEC 在系统里就是输入设备）；
  缺键时 CoreAudio **照常允许打开流、但把缓冲填零** —— 不报错、只是静音，所以最难察觉。
- **修复**：`packaging/macos/Info.plist` 加 `NSMicrophoneUsageDescription`；`build.sh` 加断言（缺键即 `exit 1`，防止回归）；
  `server.py` 的近静默警告在打包版 macOS 上追加提示"请在 系统设置 → 隐私与安全性 → 麦克风 中允许 MRRC Modern"。
- **需要用户动作**：首次启动会弹一次权限询问，**必须点"允许"**（旧版连询问都不会弹，所以必须换新包）；
  ad-hoc 签名下授权绑定当次构建的 cdhash，**今后每次升级都会重新询问**（要免除需 Developer ID 签名）。

## [v1.18.0] — 2026-09-17 — 支持链路（诊断包 + 自动分诊 + 更新检查）

**一条链：开发发版 → 问题诊断 → AI 分析 → 回复解决。** 本版把这条链从"人工来回追问"做成产品能力：
用户点三下拿到脱敏诊断包，维护者侧自动分诊把日志现象对到代码位置并发布结论，客户端只读检查新版本。支持链路第 4 期（热修通道）与第 3 期的下载/安装半片仍在路上。

### New — 「🐞 遇到问题」

- **菜单新增「🐞 遇到问题」**（新标签页打开，不打断当前电台连接）：写下现象 → **生成诊断包** →
  **上传给维护者**（拿到一个编号），或 **只保存到本地**（脱网机器/树莓派走这条路）。
  包由**服务端**生成：日志、脱敏配置、环境/电台/音频状态快照、浏览器侧上下文，
  以及一份 `diagnostics/summary.txt` 自动体检结论（先看它）。
- **永不入包**：登录密码、证书私钥、任何令牌、录音、记忆频道、天调学习值 —— 按 env 键白名单裁剪 +
  值替换，替换次数写进 `manifest.json`。
- **服务端终于有日志文件**：`MRRC_LOG_DIR` 下的轮转 `server.log`（2 MB × 2）覆盖所有启动方式；
  启动器另写 `server-stdout.log`（只在"日志系统起来之前就死了"这段窗口写文件，之后仅排空管道）。
  桌面版路径在用户数据目录（Windows `%LOCALAPPDATA%\MRRC-Modern\logs`，macOS
  `~/Library/Application Support/MRRC-Modern/logs`）。
- **专属接收端**：同机独立实例（systemd `support-receiver-modern`，端口 8098，存储
  `/var/www/support-modern`，nginx `/mrrc_modern/support/`），`./deploy_support_receiver.sh` 幂等部署。
- **`version.txt`** 随三平台产物一起打包（macOS `Contents/MacOS`、Windows 安装目录、
  rpi64 `/opt/mrrc_modern`；旧镜像的 `VERSION` 仍被识别），诊断包 manifest 与后续一键升级都读它。

### Documentation

- SDD：**AD-021**、§5 **NFR-068**（用户数据仅在脱敏且由操作员发起的包中离开本机）、§10 `SupportService`、
  §12.5.1（如何部署接收端 / 如何读包）、§12.6 日志与产物清单、§13 **R13**（脱敏尽力而为 + 接收端
  create/PUT 设计上不鉴权）、版本表 V2.51；SDD 落地页与产品页同步到 V2.51。
- 操作指南新增 §6「遇到问题怎么报（诊断包）」；菜单编号图补画「🐞 遇到问题」一行（仍为 40–52 编号体系），
  并修正菜单表此前的三处不一致（缺 `连接设置…`/`录音` 两行、44 号标成了 Logout）。
- `docs/PROJECT_MAP.md`、`README.md`、`AGENTS.md`（测试数由陈旧的 1055/53 修正为实测值）、
  `tests/README.md`、`constraints.json`（新增隐私守卫 `support-bundle-privacy`）、
  发布 skill 与三个打包手册（产物抽查须含 `version.txt`）。

### New — 自动分诊与答复页（支持链路第 2 期）

- **维护者侧 autopilot**：`dev_tools/support_autopilot.py` 每 10 分钟轮询接收端，取包解包、组装摘要，
  交给 `pi`（只读工具、仓库为工作目录）按固定 JSON 契约产出结论。默认**只出草稿**
  （`dist/support_answers/`），`--publish` 才更新答复页；`need_more_info` **不发布**。
- **答复页**：`website/answers/index.html` 由结论卡生成（可搜编号/关键词、`#编号` 直达），
  公开页对**邮箱/手机号/设备路径/串口/内网地址/本机与临时路径**做过滤。
- 运维命令：`--inspect <编号>`（不调模型的预检）、`--status`、`--force <编号>`、`--install-cron`。
- **答复页上线方式**：docroot 属 `www-data`，所以发布是两步（传到家目录 → `sudo install -o www-data`）。
  直接 rsync 会被拒绝，**且在文件未变化时返回 0** —— 这一点差点让"发布已通"的错误结论通过，已用带标记的真实文件验证过。

### New — 更新通道（支持链路第 3 期 slice 1：只读检查）

- **生成式清单**：`dev_tools/make_latest_json.py` 从**真实产物**算出 size/SHA-256 生成
  `website/downloads/latest.json`；installer 版本必须等于 CHANGELOG 顶版本、`previous` 必须更旧，
  缺任一条件**拒绝生成**（并用与客户端同一套 `parse_manifest` 复验）。
- **客户端只读检查**：`GET /api/update/check`（需登录）返回 `{available, current, latest, url, sha256, size, …}`；
  清单拉取失败只返回原因，不会 500。
- **成功判据（为 slice 2 定死）**：`state.json.lastResult.status == "ok"` 只能由**新版本启动时**写下；
  `installing` 明确不等于成功（测试直接断言旧版本无法宣称胜利）。
- **为何分两片**：下载/安装会修改运行中的机器，半成品升级器比没有升级器更糟；slice 2（下载 → SHA 校验 →
  发射/录音中拒绝 423 → 交接 → 自证）单独一轮交付。

### Verification

- 套件 **1260 项全绿**（基线 1103：第 1 期 +83、第 2 期 +40、第 3 期 slice 1 +34）。
- `release_check.py`：**29 ok / 0 failing**（含全部图与生成副本规则）。
- 第 3 期 slice 1 的清单生成器与客户端共用同一套 `parse_manifest` 校验，因此发布一个客户端不接受的清单在测试里就会被拦住。
- **第 2 期真机冒烟**：合成诊断包 → 接收端 → `--inspect`（摘要正确命中 3 条录音证据）→ 真调模型
  **4m12s** 跑完并出草稿卡，结论引用了 `server.py:_rec_enqueue`（`REC_QUEUE_MAX=200`）与
  `recorder.py:383` 的补静音逻辑；合成包与其接收端副本事后已删除。冒烟抓到 3 个 mock 测不出的缺陷
  （提示词字段名带空格、`pi` 继承 stdin 等 EOF、无预算探查导致超时）与 2 个展示缺陷（标题取到 markdown 头、
  `/var/folders` 临时路径进入公开页），均已修并有负例测试。
- **接收端已上线并端到端验收**：真机上传 → 清单可见（`product=mrrc_modern`）→ 下载 SHA-256 与本地一致
  （本地 `e31fba81…` == 远端）；本地/公网 `/api/list` 无口令均为 401；验收用的测试包已从接收端删除，清单回到空。
- **规格 §13 本地验收**（自动化，含负例数据）：包内 `logs/server.log` 非空且 `summary.txt` 含
  `启动次数：1 次`；`LEAK-CHECK`（密码）、`PRIVATE-KEY-MARKER`（私钥）、`QSO-AUDIO`（录音）三者
  **在 zip 字节流中均无命中**，记忆频道/天调文件不在清单里；无网络路径下包完整落在磁盘
  （2602 B / 7 个文件 / 脱敏 2 处）。

### Platform Status

- **macOS**：`MRRC-Modern-v1.18.0-arm64.dmg` — **55,639,405 bytes，SHA-256 `799efeb9…`**（重新构建：修复了①长期存在的签名失败、②**音频输入权限缺失导致 RX 无声**；签名 `valid on disk`、冻结服务实跑正常）
- **Windows**：`MRRC-Modern-v1.18.0-Windows-x64-Setup.exe` — 45,990,451 bytes，SHA-256 `938384ad…`（VM 内 1261 项测试绿、三个 PyInstaller 目标 + Inno Setup 编译成功；包内 FTDI DLL、`static/support.html`、`cq.wav`、`version.txt=1.18.0` 均在位；服务器侧 SHA 与本地一致）。
- **rpi64**：**未重建** —— 外部构建卷（`/Volumes/MRRCBuild`）在构建中途消失，脚本要求 ≥20 GB 而根盘只剩 3.8 GB；命令已备（见验证边界）。
- **更新通道已上线**：`https://www.vlsc.net/mrrc_modern/downloads/latest.json`（`latest=1.18.0`，installer size/SHA 与产物一致，`previous=1.17.0`），线上文件与本地逐字节一致；1.17.0 客户端检查即得 `available: true`。

### Verification Boundary

- **rpi64 镜像未重建**（外部卷消失）：树莓派实机验收仍待执行。
- **端到端升级未验证**：slice 2（下载 → 校验 → 发射门禁 → 安装 → 自证）尚未实现，因此 `latest.json` 目前只支撑只读检查。
- **三个安装包与本版镜像未重建**：本次只到源码与网站，因此「从已安装的桌面版点一次生成/上传」、
  树莓派实机、以及 Windows 真机路径尚未执行（`version.txt` 的写入代码已就位并有源码级测试守护）。
- 答复页**首次上线需一次站点部署**（autopilot 发布时只 rsync 该页；全站部署仍是交互式的）；cron 连续运行与真实用户包的结论质量待观察（第 2 期的质量结论目前只来自一个合成包）。
- 脱敏是**尽力而为**且被明确记录为风险（§13 R13）：白名单 + 值替换 + 负例测试，但不断言"绝无遗漏"。
- 接收端 `api/create` 与 `api/<id>/bundle` **设计上不鉴权**（上传端无法持有服务器密钥），
  靠限速、不可猜 ID、清单 Basic 鉴权与人工删除兜底。

## [v1.17.0] — 2026-09-13 — 一键 CQ 按键（服务端一次性自动化发射）

### New — CQ 键

- **底部 PTT 区新增 CQ 键**：单击即由**服务端**按下 PTT、播送内置 CQ 录音、播完自动松开；
  按钮转为绿色 **STOP**，再点一次立即中止（不排空尾音，直接松载波）。发射期间按钮显示播放进度，
  所有已连接客户端（浏览器 / iOS / Android）看到同一个 `cqState`。
- **音频从磁盘直接进声卡**：整段录音不走网络，网络抖动与丢包不影响发出去的内容；
  只在设备队列低于 4 帧时投喂下一帧（20 ms/帧），因此呼叫本身**不会**制造 `queue_drops`。
- **替换录音**：`MRRC_CQ_FILE=/path/to/cq.wav`（任意 16-bit WAV，单/双声道、任意采样率，
  启动时归一化为 48 kHz 单声道，上限 30 秒）。默认资产 `static/audio/cq.wav`（≈6.1 秒）。
  文件缺失/损坏/超长时启动日志打印 `CQ key disabled: <原因>`，按键失效但**绝不静默**。
- **每次发射都有明确终点**：播完即止；任何外部松键（`MRRC_PTT_MAX_TX_SECONDS` 看门狗、TUNE、
  另一端松 PTT）都会被识别为 `aborted/unkeyed` 并立即终止。

### Safety — CQ 与既有 PTT 链路的互锁

- 一次呼叫**只有一个音频来源**：CQ 进行中浏览器麦克风帧被丢弃（日志 `mic_drops`），
  PTT 与 TUNE 的请求被服务端拒绝并给出可操作提示。
- **发起端断开即中止**（`client_gone`）：不会留下"别人的载波"；最后一个控制端断开、
  服务端退出同样强制结束。
- 未验证机型（TX 门禁）下 CQ 与 PTT 一样被拒绝，且**先报门禁原因**再考虑资产是否可用。
- 新增 `tests/test_cq_player.py` + `tests/test_cq_server.py`（45 项）：资产解析/长度上限/损坏文件、
  键控→投喂→排空→松键全链路、背压、中止语义、麦克风互斥、断线中止、`cqState` 广播、
  前端契约（按钮/渲染/门禁禁用）与**真实 player + 真实回调 + 真实打包资产**的端到端用例。

### Fixed

- **启动崩溃（本次实现期发现）**：CQ 资产就绪日志对帧数误用 `len()`，在资产有效时反而让
  `Application startup failed`（单元测试全绿也发现不了，真机启动才暴露）。已改为 `frames`/`duration_s`
  属性并补回归测试。
- **门禁提示顺序**：TX 门禁检查原先排在资产检查之后，未验证机型会看到"资产未加载"而非门禁说明。
- `config._env_int/_env_float`：环境变量写错时回退默认值并打印警告，不再让服务**起不来**。
- 界面编号图与 CSS 不一致（底部 TUNE/REC 画成上下叠放，实际是横排）：本次随 CQ 键一并重绘，
  菜单编号顺延 40–52；操作指南中仍是"浏览器 lame.js 128 kbps"的旧 REC 行也已更正为服务端录音。

### Docs

- SDD：**AD-020**（服务端一次性自动化发射的选型对比与后果）、§9.2 `cqState` 协议行、
  §15 第 8 层防线（唯一自动化发射源）、§12 运维流程（含"如何替换 CQ 录音"）、
  版本表 V2.49；12 张设计图版本戳同步，`audio-chains` 与 `ptt-safety` 补画 CQ。
- README / AGENTS / 操作指南 / tests/README（1103 项、55 模块）与网站指南页（中英）同步。

### Platform Status

- **macOS**：`MRRC-Modern-v1.17.0-arm64.dmg` — 56,298,417 bytes，SHA-256 `5bbb363d6db36d354b4570dfcf2c1d1cadd10dd6aa27bb562e7418475603f270`。
- **Windows**：`MRRC-Modern-v1.17.0-Windows-x64-Setup.exe` — 45,970,364 bytes，SHA-256 `f378ab6d1a7ca6d8b93a928376ed3e69429b3d8fd45f1a52d0fa943fee2b371d`。
- **树莓派 rpi64**：`MRRC-Modern-v1.17.0-rpi64.img.xz` — 551,282,340 bytes，SHA-256 `a56e5658e200d2c8fa0eac147299b8533757934009b776f3f2340cb55944dc1b`。

### Upgrade Notes

- 依赖无变化；覆盖安装即可。**静态资源缓存版本已 bump**（`ft710_main.js?v=30`、`ft710_ui.js?v=32`、
  `ft710.css?v=25`、Service Worker `mrrc-v33`），因此旧安装不会继续使用没有 CQ 逻辑的缓存脚本。
- 首次使用前建议按操作指南「一键 CQ 呼叫」一节核对：真机点 CQ → 起载波、播放、自动解除。

### Verification Boundary

- 本版 CQ 功能的具体验证记录见 `SDD/14-version-history.md` V2.49 条目；真机（电台 + 监听手段）
  的端到端验收在发布后由现场执行，结果回填该条目。

## [v1.16.0] — 2026-09-13 — Yaesu SDR 机型族（FTDX10 / FTDX101D / FTDX101MP / FTX-1F）+ 发布工程化

### New — Yaesu SDR 机型族（实验性，仅接收）

- **四款新机型**：`MRRC_RADIO_MODEL=ftdx10 | ftdx101d | ftdx101mp | ftx1`。与 FT-710 同族的
  八重洲 ASCII-CAT，但**每一处差异都由 profile 表驱动**（`backends/yaesu/yaesu_profiles.py`）：
  模式寄存器与**独立的 CAT 字符表**（FTX-1 的 `H`/`I` 是 C4FM 码，不是十六进制）、滤波槽位、
  频段、衰减/前置步进、功率格式（`PC1`/`PC2`/auto；FTX-1 自动识别 Field 头与 SPA-1 形态）、
  S 表曲线（取自 Hamlib `newcat.c` / `ftdx101.h` / `ftx1.h`，逐项标注溯源）。
- **传输层是移植而非新写**：`cat_core.py` 沿用 FT-710 已验证的 `;` 帧、AI 帧前缀过滤、写-only set、
  PTT/TUNE 优先级抢占、ENXIO 与瞬时错误分类、重连，并按 profile 参数化；`backends/ft710/` **零改动**（AD-018）。
- **默认只收不发**：四款均为 `verified=False`，PTT/TUNE 被服务端拒绝并回给可操作原因；在真机核对 `ID;` 后
  设 `MRRC_ALLOW_UNVERIFIED_TX=1` 解开（与三款 Icom 预览机型同一门禁）。
- **没有真机频谱数据源**（这些机型不输出 FT4222/CI-V 波形）：UI 继续用既有的 S 表合成频谱，不伪造瀑布数据。
- 测试 926 → **1055**（机型注册、profile 表、CAT 字符表、TX 门禁、抽象面守卫等）。

### New — 发布工程化

- `release-artifacts.json`（产物登记表）+ `harness/release_check.py`（离线版本一致性；`--online` 校验线上页面/下载/字节数，
  `--deep` 下载比对 SHA-256），并由 `tests/test_release_artifacts.py` 在套件内强制执行 —— 文档版本号漂移会让套件直接失败
  （v1.13.0「文档落后一整轮」教训的工具化）。
- `docs/PROJECT_MAP.md`：改什么该同步哪些文档的单一入口。
- 部署 `prune`：`sdd/`、`images/` 中已从仓库删除的页面不再永久残留在线上；`downloads/` 与 `videos/` 仍归服务器管理。

### Docs

- 网站落地页补齐 v1.15.0 的功能内容：服务端 QSO 录音功能卡、IC-705 / IC-7610 / IC-7760 预览机型、
  技术栈 `lameenc`、测试指标修正；指南 intro 由 v1.11.x 升到当前版本，并新增预览机型与 TX 门禁说明、
  以及「装好双击没反应」的 FAQ（`launcher.log`）。
- SDD 手写落地页（`sdd.html` / `zh/sdd.html`）同步 SDD 版本；设计图校对（V2.47）。

### Platform Status

- **macOS**：`MRRC-Modern-v1.16.0-arm64.dmg` — 56,001,051 bytes，SHA-256 `a3b35f4843fc178306e67fc8e2bd732dd05bfecff6bea9a3c3d1b1f508cdfe38`。
- **Windows**：`MRRC-Modern-v1.16.0-Windows-x64-Setup.exe` — 45,532,589 bytes，SHA-256 `e453446bfc82072be703279c69edc8c34cc40ff99841e3a0239dc83fc97ceae4`。
- **树莓派 rpi64**：`MRRC-Modern-v1.16.0-rpi64.img.xz` — 543,966,776 bytes，SHA-256 `c8f3c3fd2e0f9dd050d998c8351a9cba0999f21e6994e4b9fe2caea09fd8dcb8`。

### Upgrade Notes

- 依赖无变化（沿用 v1.15.0 的 `lameenc`）；覆盖安装或 `pip install -r requirements.txt` 后重启即可。
- 新机型要发信必须显式设 `MRRC_ALLOW_UNVERIFIED_TX=1`（默认拒绝），并先在真机核对身份与 CAT 行为。

## [v1.15.0] — 2026-09-12 — 服务端 QSO 录音 + 录音面板（修复回放颤抖）

### New — 录音改在服务端进行

- **服务端录制**：RX 取声卡的**设备域 PCM**（44.1 kHz FT-710 / 48 kHz Icom），TX 取服务端
  已解码的**麦克风 PCM**（48 kHz），落在**一条单调时钟轴**上 —— 用时间戳定位、50 ms 连续性
  容差吸收调度抖动、真实停顿补静音；发射期间不录 RX（避免自监听重复）。
- **增量 MP3（`lameenc`）**：每个 20 ms 块立即编码并 `flush()` 落盘 —— 内存恒定（编码器
  实测约 1900 倍实时），**进程崩溃/断电时磁盘上仍有可播放的前缀**。16 kHz 单声道、
  默认 64 kbps（≈28.8 MB/小时）。
- **「录音 Recordings」面板**（☰ 菜单）：列表含频率/日期/时长/大小与总占用、**内嵌播放器
  （可拖动 seek，服务端响应 Range 请求）**、下载、删除（正在录制的那份返回 409 且按钮禁用）。
  REC 按钮显示**服务端会话状态**，所有客户端一致；**电台 CAT 断线时依然可录**（此时文件名
  频率段为 `00000kHz`，与兄弟项目 `mrrc` 一致）。
- 文件命名 `<频率>kHz_<日期>_<时间>.mp3`，存放于 `MRRC_RECORDINGS_DIR`；索引
  `recordings.json` 与目录合并，**用户自己放进目录的 mp3 也会出现在列表里**。
- 新增环境变量：`MRRC_RECORDINGS_DIR`、`MRRC_RECORDINGS_BITRATE`（64）、
  `MRRC_RECORDINGS_MAX_SESSION_MIN`（240；0 = 不限）。
- **移除浏览器录音器与 `lame.js`（530 KB）** —— 编码只发生在服务端一次。

### Fixed

- **装好却完全打不开（启动器崩溃且无任何提示）**：启动器按 UTF-8 严格读取用户可编辑的
  `mrrc_modern.env`，被 ANSI/GBK 编辑器保存过的文件（模板的 `—` 变成 `e2 80 3f`）会让它抛
  `UnicodeDecodeError` 并立刻退出，控制台一闪而过 —— 现场（Win11 VM）只能报「装了运行不起来」。
  现在 `macos/first_run.py: read_env_text` 按 BOM → UTF-8 → 本地代码页(cp936) → latin-1 兜底解码，
  `update_env_file` 回写时统一为 UTF-8（下次启动即自愈）；两个启动器都加了 `guarded_main()` +
  `report_fatal()`：失败会写 `<用户数据目录>/launcher.log` 并弹消息框，不再静默消失。
- **树莓派首启可能直接失败（同类编码坑）**：`/boot/firmware/mrrc.env` 预置文件是操作者在**自己 PC 上**
  写好拷进 SD 卡的，用 ANSI/GBK 编辑器保存即非 UTF-8 字节，而 `linux/first_run.py` 与
  `firstboot_wrapper.py` 都以 UTF-8 严格读取 —— 会抛 `UnicodeDecodeError` 让 `mrrc-firstboot.service`
  在**第一次上电**就失败。现在两者共用 `read_env_text()`（BOM → UTF-8 → cp936 → latin-1 兜底），
  预置文件被规范化写成 UTF-8；+7 项回归测试。
- **同一进程内第二次录音全静音**：MP3 writer 任务是**每会话一次性**的（它在 MP3 收尾后返回，
  这正是关停时能 `await` 到它的原因），但原先**只在进程启动时创建一次**。因此首次 REC/STOP 之后
  任务已结束、无人消费队列 → 200 块积压瞬间灌满、后续每次录音的音频**全部被丢弃**，`stop()`
  再按时间轴补出静音 —— 得到一个长度正确、内容 100% 静音的 MP3（现场 22:26 报告
  "Recording dropped 950 block(s) so far"）。现在 `_ensure_rec_writer()` 在每次 `add_audio`、
  stop 入队与每次 REC 时幂等重建 writer（并在发现 writer 已死时告警），队列满时改为丢弃最旧块
  以腾出位置给 stop 哨兵（而不是在事件循环里同步 stop）。3 个新测试（含启动器修复，本轮发布共 919 项）。
- **回放"颤抖/哆嗦"（录音根本问题）**：旧浏览器录音器把到达的帧**无时间戳顺序拼接**，
  网络抖动与播放端 jitter buffer 的补帧被永久写进文件。服务端录制从结构上消除了这一类故障。
- **按 REC 导致 WebSocket 1006 重连循环**：单条命令的异常会拆掉整个控制通道（接收循环用了
  一个 `except` 包住整圈）。现在每条消息独立隔离、失败回一条 `error` 消息并保留通道；
  缺编码器或目录不可写会给出**可操作**的错误（含 `sys.executable` 与确切 pip 命令），
  启动日志也会打印 `Recording ready: <dir>` 或明确原因。
- **录音文件 100% 静音**：写入端原用共享默认线程池，CAT 重连风暴时被串口/音频占满，
  writer 抢不到线程 → 队列灌满、所有音频被丢弃。现在录音**自备单线程池**；丢弃计数改为
  **每会话**并在 REC 提示与面板行显示（`⚠ 丢失 N 块`）。
- **CAT 掉线后恢复要 62 秒**：watchdog 重连后跑的 24 条全量状态同步会占住串口锁约 62 秒
  （实测 17:10:23→17:11:25，期间日志空白、用户命令排队）。这些字段全部由轮询分层在数秒内
  覆盖，现在重连只恢复连接标志并重跑频谱初始化；未读字段保留上次已知值。
- **日志噪声**：重连尝试首次 INFO、后续 DEBUG；掉线超 15 秒只发一条带硬件提示的 WARNING；
  `connect()` 失败仅首次打印可用端口列表；ENXIO（macOS "Device not configured"）明确提示
  "USB 串口桥重枚举，检查线缆/供电/勿用无源 HUB"；2026-08-15 的 `serial_connected` 诊断降为 DEBUG。

### Upgrade Notes

- **必须同步依赖**（新增 `lameenc`）：`venv/bin/pip install -r requirements.txt` 后重启。
  缺少时启动日志会明确告警，REC 会拒绝并给出命令，**不影响 CAT/音频/频谱**。
- 已烧录的树莓派镜像不含该依赖：设备上执行 `venv/bin/pip install lameenc && sudo systemctl restart mrrc-modern`。
- 录音目录**默认不自动清理**（可在面板看到总占用）；单次录音超过 240 分钟会自动停止（只停，不删）。

### Platform Status

- **macOS v1.15.0 已发布**（DMG 55,976,374 bytes，SHA-256 `d66cb2adff34a5f0a4d85c64d20d97d448eed7eb278a58acffdb4852aa55099e`）。
- **Windows v1.15.0 已发布**（Setup.exe 45,494,587 bytes，SHA-256 `74d04b84ab7b3d0b85314efff304fccb1494f60d95f07c24c0758bee6f13ee0c`）：打包宿主恢复后
  从同一 commit 重建（919 项测试 + 3 个 PyInstaller 目标 + Inno Setup 全部通过），并且**按字节码
  校验**包内确实含本次修复（`MRRC-Modern-Server.exe` 的 `server` 脚本条目含 `_ensure_rec_writer`；
  `strings`/grep 看不见压缩的 PYZ，不能作为证据）。
- **树莓派 rpi64 镜像 v1.15.0 已发布**（`MRRC-Modern-v1.15.0-rpi64.img.xz`，546,128,812 bytes，
  SHA-256 `c9936a3befb780c17133851330d901d43885c181eb86d68e9eb903f1ee3a0773`）：本机 Docker Desktop
  原生 aarch64 重建，**镜像内首次自带 `lameenc` aarch64**（v1.14.3 早于录音功能，完全没有服务端录音），
  并含 Linux 首启 env 容错修复。`verify-image.sh` + `debugfs` 包内抽查（VERSION=1.15.0、
  `_ensure_rec_writer`、`read_env_text`、`lameenc.cpython-311-aarch64-linux-gnu.so`）通过。
- **产物来源**：Windows/macOS 安装包由 919 项测试的树构建；树莓派镜像由最终 926 项测试的树构建
  （+7 为 Linux 首启 env 容错测试；该修复只影响 `linux/`，不在 Windows/macOS 包的运行路径内）。

### Verification Boundary

- 已在真机（FT-710 + macOS）验证：录制可回放、有声音、CAT 掉线后恢复不再有 62 秒空窗。
- **未验证**：长时间连续录制（>8 h）、树莓派 SD 卡上的长时间录制；Windows 真机上的 **TX 音频**
  （KVM 等时 OUT 限制，见 `windows-installer` gotcha 4）仍需物理机验收。

## [v1.14.3] — 2026-09-12 — 树莓派镜像重建（含 v1.14.2 全部修复）

### Raspberry Pi

- **rpi64 镜像重建**：`MRRC-Modern-v1.14.3-rpi64.img.xz`（588,677,000 bytes，
  SHA-256 `2894c895685c72d27e6c97d997d51cf2835ddb018e813375c15950da733a2509`）
  自 `main`（含 v1.14.2 全部修复：IC-7300 波特率联动、音频重复恢复、
  S 表闪烁哨兵）重建，并包含 v1.14.1 的电台设置面板与 RX 录音质量修复。
  构建/验证/发布流程见 `pi_pack.md`（本机 Docker Desktop 原生 aarch64，
  约 8 分钟）。

## [v1.14.2] — 2026-09-10 — IC-7300 baud linkage + audio duplicate recovery

### Fixed

- **IC-7300 频谱不工作（S-meter 合成回退）**：连接设置保存与首启探测现在自动按型号
  对齐 `MRRC_BAUD_RATE`（IC-7300/MK2 = 115200；FT-710 = 38400）。旧安装模板预填的
  38400 曾静默覆盖型号默认值并饿死 CI-V 频谱数据流（日志特征：
  `CI-V scope stream stalled` → S-meter fallback）；安装模板 `MRRC_BAUD_RATE`
  改为留空（按型号自动）。电台菜单 CI-V 波特率请保持 Auto（或与服务器一致）。
- **RX/TX 音频 -9999 打不开（每次 PTT 重复 6 次失败后放弃）**：设备名匹配改为
  收集全部 host-API 重复条目并优先非 WDM-KS（同名声卡在 MME/WASAPI 下可正常
  打开，WDM-KS 独占模式在部分 Windows 机器上 -9999）；-9999 重试改用排除列表
  轮换候选，WDM-KS 锁定的配置名会回退到同硬件的 MME 条目。设备选择日志现在
  附带 host API 与其余候选。
- 无 CAT/PTT/协议变更；电台侧验收（IC-7300 频谱 @115200、现场机音频恢复）
  留待操作员检查。
- **IC-7300 S 表闪烁跳跃**：IC-7300 的 CI-V 频谱段不带 S 表字节，`ScopeHandler`
  初始值 0（合法的 S0）被 `_on_scope_frame` 的 `>= 0` 门槛当成真实值，每个
  频谱帧（~30 fps）把 `radio.s_meter` 强推回 0，与 10 Hz CAT 轮询的真实读数
  交替打架 → UI S 表疯狂闪烁。无数据哨兵改为 -1（`>= 0` 门槛自动跳过；
  FT-710 逐帧真实值与合成回退路径不变）。

## [v1.14.1] — 2026-09-09 — Radio settings panel with RF power slider (web UI)

### Web UI

- **Radio Settings panel**: the ☰ menu "Settings" action now opens a
  "Radio Settings" modal instead of scrolling to the hidden DSP sliders
  block. It exposes an **RF PWR slider (5–100)** — FT-710 = watts (`PC`),
  IC-7300 = percent (CI-V level 0x0A, pct↔raw) — reusing the existing
  `rf_power` server route (no server changes). `input` updates the readout
  only; `change` (release) sends the command so a drag cannot flood the
  serial port, and the poll echo does not fight an in-flight drag.
- Panel and Memory Manager are DOM-built (`createElement`/`textContent`,
  no `innerHTML`); the two scope-selector clears use `replaceChildren()` —
  memory-channel labels come from external JSON, so this also closes an
  injection vector.
- Cache-bust: `ft710_ui.js?v=29`, service worker `mrrc-v30`
  (`ft710_main.js?v=27` unchanged).

### Installers

- Windows installer version bumped to **1.14.1**; macOS DMG rebuilt as
  `MRRC-Modern-v1.14.1-arm64.dmg`. Frontend-only release: no
  CAT/audio/PTT/scope/protocol changes.

## [v1.14.0] — 2026-09-07 — RX recording quality: capture restart + no dropped frames (SDD V2.31/V2.32)

### Audio

- **RX capture reopened after every TX→RX transition (all platforms)**
  (SDD V2.31): field analysis of 2026-09-06 `mrrc-qso-*` browser recordings
  showed intermittent 20 ms RX frames with low-frequency/attenuated energy
  while TX mic frames stayed clean. The field-consistent failure mode is a
  long-lived PortAudio RX capture stream degraded after TX playback on the
  same USB codec (previously documented only for Windows).
  `AudioHandler.restart_rx()` now reopens the RX capture stream on every
  TX→RX transition, still off the asyncio loop; extends the full-duplex
  recovery guard to macOS/CoreAudio-like stacks.
- **RX broadcast no longer drops catch-up frames** (SDD V2.32):
  `_audio_rx_loop` trimmed multi-chunk bursts to the newest 2 Opus frames,
  leaving permanent holes in the client jitter buffer (audible flutter) and
  in browser recordings. The trim is removed — every encoded frame is sent;
  the burst stays bounded by the read-side cap (~80 ms) and the client's
  time-based jitter buffer absorbs it.

### Installers

- Windows installer version bumped to **1.14.0**; macOS DMG rebuilt as
  `MRRC-Modern-v1.14.0-arm64.dmg`. Both packages carry the two audio fixes
  above; no CAT/PTT/scope/protocol changes.

## [v1.13.0] — 2026-08-29 — macOS zero-config installer + security hardening (SDD V2.28)

### Security

- **I8 — static path traversal fixed**: `serve_static` now resolves the
  requested path and rejects anything that escapes `STATIC_DIR`
  (`GET /../server.py`, absolute request paths, symlink escapes → 404).
- **I9 (part 1) — constant-time login comparison**: password check uses
  `hmac.compare_digest` (`_password_matches`); non-ASCII/None input can no
  longer raise. A loud startup WARNING now fires while the well-known default
  password is active. (Forced first-login change remains future work.)

### Reliability

- **I10 — web subchannel self-heal**: `/WSspectrum`, `/WSaudioRX` and
  `/WSaudioTX` each reconnect independently with exponential backoff
  (1 s→30 s) after a transient drop; a transient no longer leaves controls
  alive but audio/spectrum dead until reload. Power-button OFF suppresses
  self-heal via a dedicated flag.
- **I12 — opt-in stuck-keyup watchdog**: new `MRRC_PTT_MAX_TX_SECONDS`
  (default 0 = off) forces RX after continuous transmit beyond the limit,
  covering zombie-but-connected clients that client watchdogs and the
  disconnect dead-man switch cannot see.

### iOS

- **I11 (part 1) — PTT release race fixed** (docs/IOS_APP_ANALYSIS.md §2.1):
  release is sent unconditionally on gesture end with optimistic local state
  (same pattern as the TUNE button), so WAN-latency fast taps can no longer
  leave the radio keyed up. Device verification pending; watchdog/scenePhase
  layers remain open.

### Housekeeping

- Removed stale `.bak` files from the repo root and Xcode project.
- Fixed an uptime-dependent power-script test (fixed monotonic threshold
  expired on long-running hosts).

### macOS Installer

- **First-run zero-config**: the menu-bar launcher auto-generates a random web
  password, scans `/dev/cu.*` for the radio's serial port, and probes FT-710
  (ASCII `ID;`) vs Icom CI-V (0x19) to pick the radio model — no terminal, no
  config editing. The generated password is shown in a login-page banner and a
  menu-bar **Show Password…** item.
- **FT4222 true spectrum bundled**: `libft4222.dylib` / `libftd2xx.dylib` now
  ship inside the installer, so FT-710 gets a real FFT waterfall out of the box.
- Ad-hoc signed (no Developer ID) — first launch is right-click → Open once.
- Package renamed to `MRRC-Modern-v1.13.0-arm64.dmg`.
- **HTTPS by default (matches Windows)**: the launcher now resolves a TLS
  cert/key pair via `ssl_material` — honours explicit `MRRC_SSL_CERT` /
  `MRRC_SSL_KEY`, otherwise auto-generates a self-signed certificate
  (`ssl_bootstrap.ensure_self_signed`, stored in
  `~/Library/Application Support/MRRC-Modern/certs/`); `MRRC_SSL=off` reverts
  to plain HTTP. Browsers show a one-time "untrusted" warning (Advanced →
  Continue) on first visit.
- **Installer-layout fix**: the DMG now ships the classic "drag MRRC Modern
  onto Applications" layout (the bare-app DMG had no Applications shortcut).
  Rebuilt for the corrected layout + HTTPS: SHA-256
  `4dfd0a63360b0d9f671d2faaf5fe665664f5a126767f13c3c3490bc0bcf698cb`
  (55,881,750 bytes).

### Windows Installer (v1.13.0, aligned with macOS)

- First-run zero-config now on Windows too: the launcher auto-generates a web
  password and auto-detects the radio model and serial port (COM) — no manual
  config edit needed.
- Web **连接设置** (Connection Settings) dialog (radio / serial / RX+TX audio /
  password + save-and-restart) and the login-page auto-password banner are
  shared with macOS; the Windows launcher auto-restarts the server on config
  change (exit code 42).
- Built on Win11: 681 tests, three PyInstaller targets, Inno Setup — installer
  `MRRC-Modern-v1.13.0-Windows-x64-Setup.exe`, 45,433,215 bytes, SHA-256
  `15ab8f9b6ddbabda0308cff041f1f5e48547d91c000724198a0b903549a808b0`.

### Tests

- Merged suite **651 tests** across 33 modules (this work adds 18): static-path containment,
  constant-time compare, default-password warning, max-TX watchdog, and a
  subchannel-reconnect contract test.

## [v1.12.1] — 2026-08-28 — Chronological RX+TX QSO recording (web UI)

### Added

- The web MP3 recorder now captures the whole QSO in time order instead of
  RX audio only. While PTT is held, the recorder mutes the RX feed (the radio
  only returns sidetone/duplex audio during TX, which the UI already dims to
  silence) and feeds the local microphone into the same lamejs mono 48 kHz
  stream. Mic frames are tapped in all three TX capture paths — AudioWorklet
  (copied before the zero-copy transfer to the Opus worker), ScriptProcessor
  fallback, and Int16 PCM fallback. Downloads are named
  `mrrc-qso-<timestamp>.mp3` (was `mrrc-rx-`); the REC button tooltip notes
  the new behavior.

### Tests

- Suite remains **633 tests across 31 modules**, green on macOS. Cache-bust
  guard assertions re-pinned: `ft710_main.js?v=26`, `ft710_ui.js?v=28`,
  service worker `mrrc-v28`.

### Packaging

- Windows installer version bumped to **1.12.1**
  (`packaging/windows/MRRC-Modern.iss`). All three PyInstaller targets and Inno
  Setup 6.7.3 passed on the Windows 11 build VM; the 45,329,503-byte installer
  has SHA-256 `ba5fb7a9fd952e9c92508cf6b159c1d92b9292a3b03925f855f55166d5954e47`.

## [v1.12.0] — 2026-08-26 — IC-7300 runtime reliability and official CI-V conformance + Windows installer

### Added

- Official IC-7300/IC-7300MK2 CI-V byte vectors and virtual-serial regression
  coverage for scope initialization, SCROLL-C metadata, model-specific
  Transceive settings, power commands, ALC calibration, and scope speed limits.
- Audio startup/open diagnostics now include device index and name, PortAudio
  host API, default rate, actual rate, and channel count.

### Fixed

- Backend-aware serial defaults now keep FT-710 at 38400 baud while selecting
  115200 baud for IC-7300 and IC-7300MK2 unless explicitly overridden.
- CI-V scope data uses a bounded 44-segment newest-data queue; spectrum
  scheduling runs at 30 Hz and sends real data only when the hardware frame
  counter advances, avoiding stale-frame catch-up and duplicate broadcasts.
- `_diag_ic7300_scope.py` now reuses the production checksum-free codec/parser,
  validates baud/address input, reads PTT without keying the radio, and disables
  scope data output on exit.
- Scope initialization sends display ON (`27 10 01`) before data output ON
  (`27 11 01`), with SCROLL-C decoded as low/high frequency edges.
- IC-7300 and MK2 select their documented Transceive items (`0071` / `0089`)
  by model rather than CI-V address. Online health uses documented frequency
  query `03`; power-on `18 01` uses the baud-dependent Icom `FE` preamble.
- IC ALC raw value 120 now maps to 100%; FT-710 keeps its raw/255 mapping.
- IC scope speed capabilities and UI are limited to FAST/MID/SLOW.

### Tests

- Hardware-independent suite expanded to **633 tests across 31 modules** and
  passes on macOS and the Windows 11 build VM. These tests verify software
  protocol and state behavior; real USB enumeration, radio ACK timing, RF
  operation, tuner behavior, power cycling, and RX/TX audio quality still
  require physical-radio acceptance.

### Packaging

- Windows installer version bumped to **1.12.0**
  (`packaging/windows/MRRC-Modern.iss`). All three PyInstaller targets and Inno
  Setup 6.7.3 passed; the 45,339,501-byte installer has SHA-256
  `e7d1e460c408a6da2c0f66f23002d48429fa0b46bfd933305b4f150cbcefade2`.

## [v1.11.0] — 2026-08-23 — User-configurable spectrum and waterfall heights + v1.11.0 Windows installer

### Added

- Off-canvas menu Settings now has `Spec H` and `WF H` sliders to adjust the
  FFT spectrum plot and waterfall canvas heights independently. Values are
  persisted in cookies (`ft710_fftHeight`, `ft710_wfHeight`) and applied to
  both the CSS display height and the canvas drawing buffer. Responsive
  defaults remain: mobile 22 px FFT / 45 px waterfall, desktop 40 px / 80 px.
- Drawing buffers are cleared when the height changes to avoid waterfall scroll
  artifacts.

### Frontend

- `static/index.html`: added slider rows and bumped cache-busters
  (`ft710.css?v=24`, `ft710_ui.js?v=26`).
- `static/ft710_ui.js`: `_getFftHeight()`, `_getWfHeight()`,
  `applyScopeHeights()` helpers; resize handler respects user settings.

### Fixed

- `tests/test_ft710_power.py`: `test_off_rejected_during_boot_window` was
  flaky/host-dependent because it compared `time.monotonic()` against a
  hardcoded 1_000_000 s boot window. Now `time.monotonic()` is mocked in
  `PowerOffTests.setUp` and the window is set relative to the mocked value,
  so the test passes regardless of host uptime.

### Packaging

- Windows installer version bumped to **1.11.0** (`packaging/windows/MRRC-Modern.iss`).

## [v1.10.1] — 2026-08-17 — Add MRRC_RADIO_MODEL to launcher config template

### Fixed

- The `windows/default.env` and `macos/default.env` launcher config templates
  were missing the `MRRC_RADIO_MODEL` key that selects the radio backend
  (`ft710` / `ic7300` / `ic7300mk2`). The installer copy and docs referenced
  it, but the template generated on first run did not expose it. Now included
  at the top with a comment; default remains `ft710`.

## [v1.10.0] — 2026-08-17 — MRRC Modern rebrand + MRRC_* env migration + macOS packaging fix

### Changed

- **Environment variables `FT710_*` → `MRRC_*`** with automatic backward
  compatibility: `config.py` now reads `MRRC_*` first and falls back to the
  legacy `FT710_*` prefix, so existing deployments and config files keep
  working unchanged. Affected vars: serial/baud, web host/port/password,
  SSL cert/key, audio device, scope, FTDI lib dir, clock divider, ATR1000,
  memory file. Launchers (`windows/`, `macos/`) write `MRRC_*` keys and read
  both.
- **Branding cleanup**: server title/login/argparse, `AUTH_COOKIE`
  (`ft710_auth`→`mrrc_auth`, frontend synced), logger namespaces
  (`ft710.*`→`mrrc.*`), static frontend fallback strings, `ft710-rx-`→`mrrc-rx-`
  recording filenames, scripts/installer/README/website copy — all updated to
  MRRC Modern. `static/ft710_main.js` etc. filenames kept for stability.
- **macOS packaging fixed and rebranded**: `packaging/macos/build.sh` was
  broken (referenced renamed specs); now builds `MRRC-Modern-Server` +
  `MRRC-Modern-Launcher` + `MRRC-Modern.app`/dmg; launcher/config/Info.plist
  rebranded to MRRC Modern.
- **Web**: guide/fde/SDD pages + env references updated; website redeployed to
  `https://www.vlsc.net/mrrc_modern/`.

### Tests

- Suite **596 tests** (592 + 4 new env-compat fallback tests).

## [v1.9.0] — 2026-08-17 — Multi-Radio Backend: FT-710 + IC-7300/IC-7300MK2

### Added

- **Pluggable radio backends** (`backends/ft710/` and `backends/ic7300/`):
  - `RadioBackend` ABC in `backends/base.py` with `RadioCapabilities` exposure.
  - Backend factory in `backends/__init__.py` registered for `ft710`, `ic7300`, and `ic7300mk2`.
- **Icom IC-7300 / IC-7300MK2 support**:
  - New `civ_codec.py` CI-V framing/BCD/scope-segment codec.
  - New `civ_controller.py` async CI-V demux with 3-tier priority and reconnect.
  - New `civ_scope.py` CI-V `0x27` 475-bin scope producer scaled/upsampled to 850 points.
  - New `config_ic7300.py` for Icom-specific mode/band/filter tables.
- **New environment variables**:
  - `MRRC_RADIO_MODEL` selects the backend (`ft710` default).
  - `IC7300_CIV_ADDR` sets the IC-7300 CI-V address (`0x94` default).
- **Per-backend audio rates**:
  - FT-710: 44.1 kHz USB audio with 960→882 resample before TX.
  - IC-7300: 48 kHz USB audio native, no resample.
- **Capability-driven frontend**: full state message now includes `radioModel`, `radioDisplayName`, and `capabilities`; UI adapts controls (e.g., hides AN/Vd-Id on IC-7300, cycles FIL1–FIL3 filters).

### Changed

- `server.py`, `poll_scheduler.py`, and `radio_state.py` are now backend-agnostic.
- Moved FT-710-specific modules into `backends/ft710/`: `cat_controller.py`, `scope_pipe.py`, `scope_frame.py`, `scope_libraries.py`, `config_ft710.py`.
- Updated root compatibility shims to point to the new backend locations.

### Tests

- Suite expanded to **592 tests** covering backend factory, CI-V codec, controller, and existing FT-710 regressions.

## [v1.8.1] — 2026-08-16 — 界面精简（紧凑瀑布 + 页面滚动 + RF Gain 移入菜单）

### Changed

- **瀑布/FFT 画布紧凑化**：移动端 67→45 px / 33→22 px，桌面 120→80 / 60→40 px。
- **页面随内容滚动**：移除 `body {height:100%; overflow:hidden}` 固定视口，`.app-container` 由 `height` 改为 `min-height:100dvh`，内容超屏时可滚动。
- **RF Gain 滑块移入菜单**：从主控制区（RF PWR 旁）移入菜单「设置」（原「Scope Display」更名「Settings」）。
- **缓存版本同步**：css v23 / main v23 / ui v24，sw.js CACHE `ft710-v24` + ASSETS 列表同步；钉住缓存版本的测试同步更新。

### Tests

- 套件 439 项全绿。

## [v1.8.0] — 2026-08-15 — RF Gain 滑块 + 稳定性修复

### Added

- **RF Gain 滑块（UI）**：FT-710 射频增益（RG 0-255）映射为 0-100% 滑块，置于 RF PWR 旁。
  拖动实时跟随、松开生效；外部改动经 `rf_gain` 脏集合回同步到滑块。

### Fixed

- **消除串口超时导致的"电台未连接"误报**：调谐/切频等指令争用下 IF 轮询超时
  曾被每 2-6s 锁存为 `serial_connected=False`，频谱闪烁"电台未连接"横幅，实则电台健康。
  现在 `serial_connected` 仅由看门狗（`cat.connected`）置 off，IF 轮询失败不再直接判离线，
  轮询成功仍恢复。
- **switch/stop 脚本大小写不敏感匹配 + 串口持有者兜底（AD-008）**：MacPorts 解释器命令行
  大写 `'Python server.py'` 导致 `pgrep 'python.*server\.py'` 匹配不到 → 误判未运行 → 切换漏停、
  与 ft8 侧 rigctld 抢同一 CAT 串口 4 小时。`pgrep` 改 `-if` 大小写不敏感；串口被非 rigctld
  进程持有即视为残留运行（切换前必须清理）；`stop.sh` 增加串口持有者释放段。
- **website 部署改为 tmpfs 外暂存**：stage 目录移出 tmpfs，避免重启丢文件。

### Tests

- 本机与 VM 上各 439 项全绿。

### Release

- 构建于 Windows 11（Python 3.12.4 / PyInstaller 6.21.0 / Inno Setup 6.7.3），提交 `4ce4d26`。
- 产物 `MRRC-FT710-v1.8.0-Windows-x64-Setup.exe`，36,888,086 bytes，SHA-256
  `36a48a5f3f325d112937751bddcdebc581039d0484a40c95c1b00fd4bcc170ea`。
- 构建级验证完成：VM 上 439 测试、三个 PyInstaller 目标、Inno Setup 编译均通过，产物打包内容
  （FTDI DLL / opus.dll / static / mem_channels.json）与哈希核对无误。未在 VM 上重跑静默安装冒烟
  （避免干扰常驻 HTTPS 服务）；射频语音验收仍为操作员现场确认项。

## [v1.7.8] — 2026-07-31 — Stable — Windows TX Restored to 44.1 kHz Device Audio

### Fixed

- **Windows TX now keeps the FT-710 USB device domain at 44.1 kHz**
  (SDD V2.14). Browser capture and Opus remain correctly fixed at 48 kHz
  with 960 samples per 20 ms; every decoded frame is now unconditionally
  resampled to 882 samples before PyAudio opens/writes the radio at
  44.1 kHz. The same rule applies after PortAudio reinitialization.
- Removed the Windows policy that promoted a same-name WASAPI endpoint's
  advertised 48 kHz `defaultSampleRate` into the FT-710 device rate and
  bypassed SRC. That value is the Windows shared-mode mix rate, not proof
  of the radio's USB hardware clock. The earlier KVM pacing result remains
  useful incident evidence but is not a valid hardware-rate decision.
- Preserved the Windows TX→RX capture reopen workaround and the v1.7.7
  PortAudio terminate/reinitialize/device-name re-resolution recovery.

### Tests

- Replaced the obsolete WASAPI-selection contract with regressions for
  fixed 44.1 kHz stream opening, exact 960→882 conversion, 44.1 kHz byte
  budgets, and rate preservation after device re-enumeration.

### Follow-up — 2026-07-31 TX Audit

- Fixed the frontend intentional-disconnect flag (`const` → `let`), which
  could throw before closing the audio sockets and leave a stale TX owner.
- A replacement `/WSaudioTX` connection from the same authenticated session
  now takes ownership; a different session still cannot steal on connect.
- TX jitter-buffer oldest-frame drops are now counted and logged as
  `queue_drops` on PTT release, exposing Windows output pacing regressions.
- Added four regressions and made source-contract tests formatting-agnostic;
  suite is 439 tests.

### Stable Release Verification — 2026-07-31

- Built from commit `8629f0c` on Windows 11 with Python 3.12.4,
  PyInstaller 6.21.0, and Inno Setup 6.7.3. All 439 Windows tests passed and
  all three PyInstaller targets plus the installer compiled successfully.
- Silent install, required bundled-file inspection, server-listen check,
  authenticated health-boundary check (expected HTTP 401), and silent
  uninstall all passed.
- Published artifact: `MRRC-FT710-v1.7.8-Windows-x64-Setup.exe`,
  36,820,041 bytes, SHA-256
  `c1e474b58f9948206990efbc9f8bdb5b183d03e4f462828b56d2d3f8c0b493bb`.
- Classified as Stable for public distribution. The release VM did not provide
  an authoritative physical FT-710 RF speech/noise path, so over-the-air audio
  monitoring remains an operator acceptance check rather than claimed build
  evidence.

## [v1.7.7] — 2026-07-28 — Audio Survives Radio Power Cycles (Power Switch Withdrawn)

### Fixed

- **TX/RX audio survives radio power cycles** (SDD V2.12): every
  power-off/on re-enumerates the FT-710's USB sound card, invalidating
  the CoreAudio device IDs cached inside PortAudio at `Pa_Initialize`
  time — every subsequent stream open failed with `-9999` until the
  server was restarted (field report: "TX audio device unavailable" after
  radio restarts). `audio_handler` now re-initializes PortAudio once and
  retries with a freshly resolved device index when TX/RX stream opens
  fail. Note: index-locked `FT710_AUDIO_RX/TX_DEVICE=<n>` configs are
  inherently fragile across re-enumeration — lock by **name**
  (e.g. `USB Audio Device`) instead.

### Added

- `PS;` is polled in the Tier-3 settings loop, so power changes made at
  the radio's front panel are reflected in `power_on` state.

### Withdrawn (after field reliability testing, SDD V2.13)

- **The header power switch (CAT `PS0;`/`PS1;`) was removed before
  release.** Two days of live testing proved the FT-710's CAT power
  control too fragile for a remote UI button: (1) a `PS0;` landing
  seconds after `PS1;` (mid-boot) wedged the radio's CAT MCU — serial/
  audio/scope USB all enumerated but CAT permanently deaf until a
  physical power cycle; (2) `PS1;` wake-up proved unreliable even with
  retry+verify (3 attempts over 36 s failed to wake a healthy radio).
  The `power` WS command remains for the maintenance scripts
  (`_power_cycle*.py`), hardened with the lessons learned: 15 s boot
  window rejecting `PS0` after `PS1`, `PS1` retry ≤3× with `FA;`
  read-back verification, `PS0` double-send, power-off refused while TX.

### Tests

- New `tests/test_power_switch.py` (9 tests: boot-window rejection,
  TX-while-off rejection, PS0 double-send, PS1 retry/verify/give-up,
  error reporting) and `PortAudioReinitTests` in `tests/test_audio.py`
  (3 tests: reinit-then-succeed for RX and TX, give-up path). Cache-bust
  css v20 / main v23 / ui v22 / sw `ft710-v23`. Suite 435 tests.

## [v1.7.6] — 2026-07-26 — HTTPS by Default on Windows (Self-Signed Bootstrap)

### Changed

- **The Windows app now starts on HTTPS by default** (SDD V2.10): the
  launcher no longer hardcodes `--no-ssl`. On first run it generates a
  throwaway self-signed certificate (ECDSA P-256, 10-year, SANs for
  localhost / hostname / LAN IPs) into `%LOCALAPPDATA%\MRRC-FT710\certs\`
  via the new `ssl_bootstrap.py`, and starts `ft710-server` with
  `--ssl-cert/--ssl-key`. HTTPS matters off-localhost: plain HTTP on a
  LAN address is not a browser secure context, which disables
  AudioWorklet and `getUserMedia` (RX/TX audio). The browser shows an
  "untrusted" warning once — accept it, or point
  `FT710_SSL_CERT`/`FT710_SSL_KEY` at a real certificate. Escape hatch:
  `FT710_SSL=off` restores the old HTTP behaviour. Launcher URL probe
  skips TLS verification for the self-signed bootstrap cert.

### Tests

- New `tests/test_ssl_bootstrap.py` (6 tests) and launcher SSL tests
  (6 tests); suite 421 tests. `cryptography>=41` is now a hard
  dependency (was commented out).

## [v1.7.5] — 2026-07-26 — Hotfix: NameError in start_tx (v1.7.4 Regression)

### Fixed

- **`name 'sys' is not defined` on PTT** (v1.7.4 regression): the
  WASAPI selection branch in `start_tx()` referenced `sys.platform`
  without importing `sys` at module level, so every PTT on the v1.7.4
  build failed with NameError. Added the import plus two end-to-end
  `start_tx` regression tests (`StartTxWindowsTests`: win32 opens the
  WASAPI 48 kHz entry, darwin stays at 44.1 kHz) — the previous tests
  only exercised `_wasapi_tx_variant` in isolation. Suite 409 tests.

## [v1.7.4] — 2026-07-26 — Windows TX Crackle Fix (WASAPI 48 kHz Output)

### Fixed

- **TX audio crackles into noise on Windows** (SDD V2.9): the C-Media
  codec's MME 44.1 kHz playback path paces ~1.4× slow (measured on the
  Win11 KVM rig: 50×20 ms frames block 1.36–1.42 s instead of 1.00 s),
  so the TX drain loop falls behind, the 400 ms queue cap drops 24–34 %
  of voice frames, and the transmitted audio is chopped into crackle.
  The codec's WASAPI entry at its native 48 kHz mix rate paces
  correctly (ratio 0.96). `start_tx()` now prefers the same-name WASAPI
  entry on Windows and opens the stream at that entry's native rate;
  `feed_tx_audio()` passes 48 kHz PCM through unchanged at 48 kHz
  (no 48→44.1 resample) and uses per-rate byte budgets for the
  pre-buffer/cap/graceful-drain. macOS behavior is unchanged (CoreAudio
  device domain stays at 44.1 kHz). AD-011 amended: the device-domain
  rate is host-API-dependent, not universally 44.1 kHz.

### Tests

- New `WindowsWasapiTxTests` (5 tests: WASAPI variant selection,
  other-device/WASAPI-absent guards, 48 k feed passthrough, 44.1 k feed
  resample); suite 407 tests.

## [v1.7.3] — 2026-07-26 — Windows RX Audio Dies After First PTT (Full-Duplex Wedge)

### Fixed

- **RX audio silent after the first PTT on Windows** (SDD V2.8): opening
  the TX playback stream on the FT-710's C-Media USB codec silently
  wedges the RX capture stream (MME/DirectSound full-duplex driver
  quirk — the stream stays open and error-free but delivers silence;
  macOS CoreAudio is unaffected). `AudioHandler.restart_rx()` now
  reopens the capture stream on every TX→RX transition (hooked in
  `_broadcast_state` on `tx_status`, covers PTT/TUNE/physical PTT),
  Windows-only, no-op elsewhere. Field symptom: audio perfect after
  server restart, gone after one PTT; hardware capture verified healthy
  (max 43% FS) with the server stopped.
- **Scope resync actually works now** (SDD V2.8): the byte-by-byte
  `sync_stream` resync could never succeed on the FT4222 — every 1-byte
  `SingleRead` is its own SPI transaction (CS toggles per call), so a
  contiguous multi-byte sync pattern is unobservable. It only consumed
  the stream and churned recovery into `fatal:too_many_reinits` after
  every PTT (even with the V2.7 TX pause, whose resume used it).
  Replaced with `resync_device()`: close → 1 s idle-bus settle → reopen
  — the pattern that reliably realigns (same as a pipe restart).

### Tests

- New `RestartRxTests` (4 tests: Windows stop→start order, non-Windows
  no-op, RX-not-running guard, failed-reopen path); suite 402 tests.

## [v1.7.2] — 2026-07-26 — TX-Safe Spectrum (Scope Pipe TX Pause)

### Fixed

- **Spectrum wrecked for 30–45 s after every PTT** (SDD V2.7): the
  FT-710 garbles its scope stream during TX, but `scope_pipe` kept
  reading it — sync_lost → stall reinit → more sync failures →
  `fatal:too_many_reinits`, then a full pipe restart before real FFT
  data returned. The pipe is now TX-aware: the server pushes `TX:1` /
  `TX:0` over the pipe's stdin on every `tx_status` transition
  (PTT/TUNE, any source); while TX is active the pipe pauses SPI reads
  and freezes all sync/stall recovery counters, and runs one clean
  re-sync when RX resumes. Post-PTT recovery: ~40 s → ~1 frame.
- **Zombie scope_pipe held the FT4222 on Windows**: killing the
  PyInstaller onefile bootloader (`proc.terminate()`) never reached the
  real worker, so the next pipe failed `FT_OpenEx` with
  FT_DEVICE_NOT_FOUND for ~10–15 s. The server now kills the pipe via
  `taskkill /PID <pid> /T /F` on Windows; the pipe additionally treats
  stdin EOF (parent died) as a shutdown signal.
- **Waterfall during TX**: now shows "TX 发射中 — 频谱暂停" instead of
  stale/garbled fallback rows (`ft710_ui.js`).
- **Windows package now ships libopus**: `vendor/opus/windows/bin/x64/opus.dll`
  (x64, from the PyOgg wheel) added to the repo and to `ft710_server.spec`
  datas. Previously the installer carried no libopus, killing server-side
  Opus (RX fell back to raw PCM, TX audio dead) and requiring a manual
  DLL drop into the install dir after every reinstall.

### Tests

- New `tests/test_scope_pipe_tx.py` (13 tests: control-line parsing,
  server TX-notify transitions/force/dead-pipe guard, Windows taskkill
  vs POSIX SIGTERM); suite 398 tests.

## [v1.7.1] — 2026-07-26 — Windows Audio Device Lock & Installer Diagnostics

### Fixed

- **Windows RX/TX audio broken after install** (SDD V2.6): the FT-710's
  built-in USB sound card enumerates on Windows under generic names —
  `USB Audio CODEC` or `USB Audio Device` depending on driver/OS build —
  with no "FT-710"/"YAESU" substring, so auto-detection fell through to
  the channel heuristics and grabbed the laptop microphone (RX) or PC
  speakers (TX). `audio_handler.py` adds a generic USB-audio name tier
  (`USB_AUDIO_NAME_HINTS`) to both RX and TX device selection, ranked
  below the FT-710-specific names and above the mono/full-duplex
  heuristics; multi-match (per-host-API duplicates) warns and points at
  the env-var lock.
- **CAT connect failure now logs the serial ports actually visible**
  (SDD V2.5), so a wrong default `COM3` is diagnosable from the console.
- **Launcher health probe**: `FT710_WEB_HOST=::` now maps to
  `http://localhost:<port>` instead of a false 15 s startup warning.
- **Frozen-app Opus**: `opus_rx.py` searches packaged `opus.dll`
  locations (`opus.dll`, `_internal\opus.dll`,
  `vendor\opus\windows\bin\x64\opus.dll`); `build.ps1` warns when
  `vendor\opus\windows` is absent; PyInstaller specs ship the
  platform-matched FTDI tree.

### Changed

- **`windows/default.env` pre-locks the audio devices**:
  `FT710_AUDIO_RX_DEVICE` / `FT710_AUDIO_TX_DEVICE` = `USB Audio` (the
  common substring of both Windows enumeration forms; name locking is
  reboot-stable, indices are not).
- **New "Audio (RX/TX) Setup" section** in the Windows installer guide:
  device identification from the startup PortAudio list, env locking,
  sound-panel guidance (never make the radio's USB audio the default
  playback device), and the radio-side modulation routing
  (`RADIO SETTING` → per-mode `MOD SOURCE` = `USB`) verified against the
  FT-710 Operation Manual; four new troubleshooting rows.

## [v1.7.0] — 2026-07-26 — ATR1000 Tuner Linkage & Frontend Settings in Cookies

### Added

- **Optional ATR1000 tuner linkage** (SDD V2.4; default disabled, enable
  with `FT710_ATR1000_HOST`/`FT710_ATR1000_PORT`): asyncio-native client
  for the networked ATR1000 (`atr1000_client.py`) with the LC-learning
  store ported from the mrrc project (`atr1000_tuner.py`). Three linkage
  behaviors: frequency change auto-applies learned relay LC; TX state sync
  (device push mode + stability-window learning); server-side tune assist
  (TX2 carrier → skip when SWR ≤ 1.6 → full tune → rollback when no
  improvement, carrier always dropped). New token-gated `/WSatr1000`
  channel; compact ATR meter row (power/SWR/relay/tuning) with an ATR TUNE
  button appears in the web UI only when the feature is enabled and
  connected. Radio-internal TUNE is unchanged. When disabled there is no
  client, no task, no network — zero impact on installs without the tuner.
- **Device-side mic gain (🎙 Vol)**: software GainNode on the browser mic
  capture graph (0–200 slider → 0–2×, 100 = unity), persisted in a cookie;
  independent of the radio's CAT mic gain.

### Changed

- **All frontend settings now persist in cookies** (SDD V2.3): unified the
  previous localStorage/sessionStorage mix behind `settings_manager.js`
  cookie helpers with a one-time legacy migration. Memory channels now
  survive browser restarts; the radio-side mic gain slider persists and is
  re-applied to the radio on every connect.
- Frozen Windows app: the ATR1000 learned-data store honors
  `FT710_ATR1000_STORE` (the launcher points it at the user data dir so
  learned data stays writable and survives reinstalls); the PyInstaller
  spec lists the new modules explicitly.

### Fixes (previously uncommitted, SDD V2.1/V2.2)

- TX uplink ownership promotion/claim + per-session TX observability.
- Windows packaging chain: frozen `_internal` resource resolution,
  scope_pipe stdout heartbeat, hardened `build.ps1`, launcher
  self-spawn guard.

Suite: 373 tests.

## [v1.6.3] — 2026-07-25 — Windows Installer Robustness Fixes

Deep audit of the Windows packaging chain (`windows/`, `packaging/`); all
fixes verified hardware-free, suite now 271 tests.

### Fixes

- **Packaged web UI 404 (P0)**: PyInstaller 6 onedir puts datas under
  `_internal/`, but `STATIC_DIR` pointed next to the exe — the installed app
  served only the inline fallback login page and "Static files not found".
  `server.py` now resolves bundled resources via `_resource_dir()`
  (`sys._MEIPASS`-aware); the launcher's starter-channel seeding also checks
  `_internal/mem_channels.json`.
- **scope_pipe orphan holding FT4222 (P1)**: on Windows, terminating the
  onefile bootloader never reached the real child process, which kept the
  FT4222 device open forever. `scope_pipe.py` now emits the documented len=0
  stdout heartbeat every 1 s — a dead parent closes the pipe, the next write
  raises EPIPE, and the pipe exits cleanly instead of orphaning.
- **Silent build failures (P1)**: `build.ps1` relied on
  `$ErrorActionPreference`, which does not cover native commands — failed
  tests or PyInstaller runs no longer slip through into an installer
  (`Invoke-Checked` wrapper checks `$LASTEXITCODE`).
- **Launcher self-spawn chain (P2)**: with `ft710-server.exe` missing (e.g.
  antivirus quarantine), the frozen launcher used to "fall back" to
  `[sys.executable, server.py]` — i.e. spawn another launcher, recursively.
  It now reports the missing exe and exits.
- **Version drift (P2)**: installer `AppVersion` 1.6.0 → 1.6.3; new
  `packaging/windows/requirements-build.txt` pins `pyinstaller==6.21.0`
  (PyInstaller 6 already changed the onedir layout once).
- Installer no longer ships `windows/__pycache__`; stale cache-bust test
  assertions updated (v17 → v18).

## [v1.6.2] — 2026-07-21 — Spectrum Freeze After USB Reconnect

### Fixes

- **Spectrum survives serial hiccups**: the connection watchdog's reconnect
  path now re-runs the scope-init CAT sequence (`EX040101`/`EX040200`) via a
  new `PollScheduler(on_reconnected=...)` hook wired to `_init_scope_cat`.
  Previously a USB re-enumeration reset the radio's scope output, CAT
  reconnected fine, but no FFT frames ever resumed — the waterfall sat frozen
  on stale data (and, with `serial_connected=true`, without even the
  "radio disconnected" hint). Hook failures are logged and non-fatal.

## [v1.6.1] — 2026-07-21 — Web Frontend Safety & UX Overhaul

### Safety

- **PTT watchdog actually armed**: PTT/TUNE buttons and Space-bar PTT now
  route through `PTTManager` (previously bypassed — watchdog, pagehide
  force-RX and unload beacon were dead code); broken `sendBeacon` removed
- **TUNE is press-and-hold** (was latch-on-click — accidental TX path)
- **Keyboard guards**: ignore `e.repeat` and events from inputs

### Fixes

- Silent `renderFreqScale` crash (missing `range` arg) that skipped
  VFO/PTT renders every update cycle
- Server `error` messages now show a toast banner
- 🔊 Vol slider is browser-local volume (localStorage), no longer fought
  by the CAT `af_gain` poll
- S-meter label: relative `dB` (S9=0) instead of misleading `dBm`

### Features

- Waterfall/FFT click-to-QSY (8 px drag threshold)
- Desktop layout ≥768 px (720 px container, 120 px waterfall, larger controls)
- Filter tables server-authoritative (`fullState.filterTables`)
- `lame.js` lazy-loaded on first REC click; asset cache bust v17

## [v1.6.0] — 2026-07-21 — Windows Desktop Installer

### Windows Package

- **Desktop installer**: `MRRC-FT710-Setup.exe` (28.3 MB, x64) built with
  PyInstaller 6.21 + Inno Setup 6.7.3 — embedded Python 3.12 runtime, no
  manual Python install required
- **Launcher app** (`MRRC-FT710.exe`): seeds `%LOCALAPPDATA%\MRRC-FT710\ft710.env`,
  starts the server, waits for `/api/health`, opens the browser;
  CTRL_BREAK-based graceful stop
- **Frozen scope worker**: `scope_pipe.exe` bundled for FT4222 true
  spectrum (requires `vendor\ftdi\windows\bin\x64` FTDI DLLs, otherwise
  S-meter fallback)
- Download: <https://www.vlsc.net/mrrc_ft710/downloads/MRRC-FT710-Setup.exe>
  or GitHub Releases

### Build Fixes

- **PyInstaller specs**: `ROOT = Path(SPECPATH).parents[1]` — `SPECPATH`
  is the spec directory in PyInstaller 6, `parents[2]` escaped the repo
  root and broke the build entirely
- **Frozen server start**: `uvicorn.run(app)` with the app object instead
  of the `"server:app"` import string, which a frozen exe cannot import

### Verified

- End-to-end on a clean Windows 11 VM: silent install → launcher →
  server start → web login → `/api/health` + 4 WebSocket channels OK

## [v1.1.0] — 2026-07-14 — iOS App Enhancement

### iOS App Features

- **Complete Opus Codec Implementation**: Full libopus integration via C bridge
- **Unified Audio Session Management**: Consistent TX/RX audio handling
- **Error Handling UI**: User-friendly error alerts and recovery options
- **Performance Monitoring**: Real-time connection and audio quality metrics
- **Comprehensive Testing**: Unit tests for core components
- **Documentation**: Complete iOS development guides

### Technical Improvements

- Optimized spectrum rendering with performance monitoring
- Enhanced PTT button implementation (removed duplication)
- Added audio session route change handling
- Improved error propagation and user feedback
- Pre-allocated buffers for memory efficiency

### Security & Stability

- Robust error handling for audio operations
- Graceful degradation on connection failures
- Thread-safe audio processing
- Memory leak prevention through proper cleanup

## [v2.0.0] — 2026-07-14 — Stability & Security Hardening

### Security

- **Login rate limiting**: Max 5 attempts per 5 minutes per IP (`_check_login_rate_limit`)
- **Strong default password**: Changed from `ft710` to `changeme_please_use_strong_password!`
- **Password strength warnings**: Client-side feedback for weak passwords
- **Health check endpoint**: `/api/health` returns uptime, radio connection status, and degraded state
- **Startup time tracking**: Monitored via health endpoint

### Critical Fixes

- **Race condition fix**: `_cancel_polls` changed from `bool` to `asyncio.Event` in `cat_controller.py` and `poll_scheduler.py` — eliminates TOCTOU race between priority commands (PTT/Tune) and background pollers
- **Python 3.10+ compatibility**: Added `from __future__ import annotations` to `config.py` and `server.py`; removed `asyncio.Lock` from `radio_state.py` dataclass field (was causing RuntimeError on Python 3.9)
- **Removed duplicate `rf_gain` handler** in `server.py`

### Performance Optimizations

- **Initial sync speed**: `initial_state_sync` sleep reduced from 50ms to 20ms (60% faster connection)
- **Log noise reduction**: IF poll debug threshold raised from 50→1000 consecutive errors; TX meter logging throttled to first 5 seconds
- **Class-level state cleanup**: `_tx_meter_first_logged` moved from class-level to instance-level in `poll_scheduler.py`
- **Module-level `import time`**: Added missing import in `poll_scheduler.py`

### Code Quality

- **Debug cleanup**: Removed verbose `_dbg_*` flags and associated logging from `audio_handler.py`
- **Docstring fix**: Corrected misleading sample rate description in `opus_rx.py` (16kHz → 48kHz)
- **Test compatibility**: Updated `FakeCat` mock in `tests/test_poll_scheduler.py` to use `asyncio.Event()` for `_cancel_polls`

### Testing

- **206/206 tests passing** (was 35 failing before fixes)
- Full pytest and unittest coverage maintained

### Documentation

- Created `SECURITY_GUIDE.md` — complete security configuration guide
- Created `QUICKSTART.md` — step-by-step setup guide
- Created `FIXES_SUMMARY.md` — detailed fix documentation
- Created `FINAL_VERIFICATION.md` — verification report
- Created `EXECUTIVE_SUMMARY.md` — executive summary (Chinese)
- Created `COMPLETION_REPORT.md` — completion report (Chinese)
- Updated `DEPENDENCIES.md` — Python version requirement clarified
- Updated `README.md` — reflects current state
- Created `docs/TX_LINK_ANALYSIS.md` — TX audio chain deep analysis report

---

## [v2.1.0] — 2026-07-14 — TX Link Analysis Complete

### Analysis Completed

- **TX audio chain deep review**: Full stack analysis from browser → WebSocket → radio
- **Issues identified**: 2 high-risk, 3 medium-risk, 3 low-risk
- **Key findings**:
  - PTT control path inconsistency (high risk)
  - TX meter polling condition error (high risk)
  - AudioWorklet SAB path not implemented (medium risk)
  - TX Opus availability not checked (medium risk)
  - Unused TxJitterBuffer (medium risk)

### Recommendations

- Fix PTT button to use PTTManager immediately
- Clean up AudioWorklet SAB code within 1 week
- Add TX Opus availability check within 1 week
- Upgrade frontend Opus library within 1 month
- Develop TX end-to-end tests within 1 month

**Status**: Analysis complete, fixes pending implementation

---

## [v2.2.0] — 2026-07-14 — iOS App Analysis Complete

### Analysis Completed

- **FT710Mobile iOS app deep review**: Full stack analysis from SwiftUI → WebSocket → radio
- **Issues identified**: 2 high-risk, 3 medium-risk, 3 low-risk
- **Key findings**:
  - Opus encoder/decoder not implemented (high risk)
  - Dual PTT button implementation (high risk)
  - Audio session configuration incomplete (medium risk)
  - Error handling insufficient (medium risk)
  - Memory management risks (medium risk)

### Recommendations

- Implement Opus codec support immediately
- Unify PTT button implementation
- Add comprehensive error handling
- Implement unit tests
- Consider internationalization

**Status**: Analysis complete, fixes pending implementation

---

## [v1.2.0] — Previous Release

### Features

- Bidirectional Opus audio (RX/TX) with jitter buffers
- Real-time FFT spectrum + waterfall (FT4222 SPI + S-meter fallback)
- PTT safety: dead-man switch, triple verify, forced RX on disconnect
- Graceful TX audio drain before RF drop
- Mobile-first responsive UI (iPhone/iOS Safari optimized)
- Multi-meter telemetry (PWR/ALC/SWR/Id/Vd)
- Memory channels with persistent storage
- 5-tier adaptive background polling
- Dirty-state broadcasting (only changed fields sent to clients)
- PWA support (manifest + service worker)

---

## [v1.0.0] — Initial Release

- Basic FT-710 web control server
- Serial CAT communication via pyserial
- WebSocket-based real-time state updates
- S-meter display
- Frequency/mode/band control
