# Changelog

All notable changes to the MRRC Web Control project.

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
