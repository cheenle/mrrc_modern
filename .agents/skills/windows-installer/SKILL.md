---
name: windows-installer
description: Use when building, rebuilding, verifying, or deploying the MRRC Modern Windows installer (MRRC-Modern-Setup.exe / Inno Setup), or when the Win11 KVM build VM is unreachable mid-build ("No route to host", qemu OOM-killed), the build script prints BUILD_DONE although nothing was built, PyInstaller or iscc fails on the VM, tests fail only on the VM (WinError 32 temp cleanup, GBK locale), the built exe is missing FTDI DLLs / opus.dll / static assets / mem_channels.json / lameenc / the fleet payload, the package ships macOS metadata (.DS_Store, AppleDouble ._* files), private material (certs, .env, recordings) ends up on the build VM, the VM builds the wrong product (MRRC_FT8-Setup.exe), the venv is missing (Activate.ps1 not found), an installed app will not start at all, a packaged build behaves differently from the source tree, TX audio crackles on the VM, or a website deploy deleted pages.
---

# Windows Installer Build (MRRC Modern)

## 五条铁律（每次构建前重读）

1. **退出码不是证据。** `BUILD_DONE`、`Successful compile`、`version.txt`、任务退出码 —— 任何**单独一个**都不能证明包是新的。唯一可靠组合：**mtime 是今天（带年份看）+ size + SHA ≠ 上一版 + 装完真跑**。
2. **代码定稿 → 构建 → 才写数字。** 给"还不是最终代码"的构建填字节数/SHA，等于发布一个**数字对得上、代码对不上**的包。本版就因此作废了两个已验完的产物（见下）。
3. **洁净室真跑打包出的 exe 是不可省略的一层。** 本仓三次发布事故（v1.24.1 `cloud_hub`、v1.24.5 `ssl_bootstrap`、v1.24.6 显式 host 绕过端口守卫 + 冻结包写坏签名 bundle）**全部只有这一步能抓到**：套件全绿、签名通过、SHA 三处一致，包照样是坏的。
4. **一次只做一件事。** 绝不同时发两条会改同一批文件或动同一台机器的命令。并发跑 `deploy.sh` 曾把线上 `sdd/` 与 `images/` **整棵树删光**；并发跑"变异脚本 + 全量套件"曾让套件在被改坏的文件上跑出一堆假红。
5. **构建机不是保险箱。** 源码包必须排除 `certs/`、`.env`、`recordings/`、`logs/`、`promo/`、`website/videos/`、`website/downloads/*` 与 macOS 元数据。曾经把操作员的 **TLS 私钥、含网页密码的 .env、63 个真实 QSO 录音**送到了构建 VM 上。

---

## 拓扑与实测可用的命令序列（2026-10-02 v1.24.6 全程照此跑通）

```
Mac ──ssh -o ProxyJump=cheenle@ham.vlsc.net──► cheenle@192.168.122.133（win11 VM，默认 shell = PowerShell 5.1）
                                              仓库在 C:\mrrc_modern（mrrc 是 C:\mrrc）
```

**直连用 `ProxyJump`，不要再两跳中转**（旧文档写的 "scp 到 ham:/tmp 再由 ham 转发" 已废弃：`ham` 的 `/tmp` 是 **454 MB tmpfs**，38 MB 的包能过、大一点的会炸，而且多一跳多一次静默失败的机会）。实测 38.4 MB 源码包 **6.4 秒**传完。

```bash
# ① 打源码包：排除清单一个都不能少（漏一条 = 白跑一轮或泄密）
cd <repo> && export COPYFILE_DISABLE=1 && tar czf /tmp/src.tgz \
  --exclude='./.git' --exclude='./.venv' --exclude='./venv' --exclude='./dist' \
  --exclude='./build' --exclude='./node_modules' --exclude='__pycache__' \
  --exclude='./packaging/payload' --exclude='./promo' --exclude='./recordings' \
  --exclude='./logs' --exclude='./certs' --exclude='./.env' --exclude='./FT710Android' \
  --exclude='./website/videos' --exclude='./website/downloads/*.exe' \
  --exclude='./website/downloads/*.dmg' --exclude='./website/downloads/*.xz' \
  --exclude='./.DS_Store' --exclude='._*' .

# ② 打完必须自查：关键文件在、隐私目录不在、体积合理（855 MB → 36.6 MB 是这一版的实测差）
tar tzf /tmp/src.tgz | grep -c "\._\|DS_Store"          # 必须 0
for d in promo recordings logs certs; do tar tzf /tmp/src.tgz "./$d/" | wc -l; done   # 必须全 0
tar xzf /tmp/src.tgz -O ./packaging/windows/MRRC-Modern.iss | grep -m1 'MyAppVersion "'

# ③ 送 + 解包（解包**前**清掉 VM 上的 tests\*.py：tar 只覆盖不删除，本地已删的旧测试会留在
#    VM 上被 unittest discover 收集，制造与代码无关的红）
scp -o ProxyJump=cheenle@ham.vlsc.net /tmp/src.tgz cheenle@192.168.122.133:C:/tmp/src.tgz
#    然后跑一个 .ps1（见"操作纪律"：绝不内联 PowerShell）：清 tests\*.py → tar xzf → 断言
#    venv\Scripts\python.exe 还在 → 断言 .iss 版本 → 断言新模块在 → 断言 payload 齐

# ④ 同步构建（输出进日志，一步看全；别用 Start-Process，见 gotcha 6）
ssh -o ServerAliveInterval=30 -o ServerAliveCountMax=60 -o ProxyJump=cheenle@ham.vlsc.net \
  cheenle@192.168.122.133 'cmd /c "cd /d C:\mrrc_modern && set PATH=C:\mrrc_modern\venv\Scripts;%PATH% && powershell -NoProfile -ExecutionPolicy Bypass -File C:\mrrc_modern\packaging\windows\build.ps1 > C:\tmp\bNNN.log 2>&1 && echo BUILD_CMD_OK || echo BUILD_CMD_FAIL"'

# ⑤ 取回 + 跨主机比对（ProxyJump 一步，不要经 ham 落地）
scp -o ProxyJump=cheenle@ham.vlsc.net cheenle@192.168.122.133:C:/mrrc_modern/dist/windows/MRRC-Modern-Setup.exe \
    dist/windows/MRRC-Modern-v<ver>-Windows-x64-Setup.exe
shasum -a 256 dist/windows/MRRC-Modern-v<ver>-Windows-x64-Setup.exe   # 必须 == VM 的 Get-FileHash
```

`build.ps1` 的门禁顺序（任一步失败即中止）：`py_compile` → **全量 unittest**（含 `release_check`，所以版本串必须先改完，见 `dual-platform-release`）→ `dev_tools/test_config_encoding.py` → 3 个 PyInstaller spec → 从 `.iss` 的 `MyAppVersion` 写 `version.txt` → 拷 fleet payload → `iscc`（**出到临时目录再复制**，否则 Defender 实时扫描锁住新写的 exe，报 `Error 32`）。

---

## 构建前置检查（缺一即在 VM 上白跑一轮）

| 检查 | 命令 / 判据 |
| --- | --- |
| 宿主内存 | `ssh cheenle@ham.vlsc.net 'free -m'` —— available **< 1 GB 就别开机**，会再被 OOM 杀一次（gotcha 9） |
| VM 在跑 | `sudo virsh -c qemu:///system list --all`（缺 `sudo` + `qemu:///system` 会说 domain not running） |
| venv 是**这台 VM 的** | `venv\Scripts\python.exe -V` = 3.12.4；`pyvenv.cfg` 指向 `C:\Program Files\Python312`。**别把 Mac 的 venv 解进去** |
| 依赖没漂 | `requirements*.txt` / `packaging/windows/requirements-build.txt` 本轮**没改**才能复用 venv（改了必须重装，gotcha 8） |
| fleet payload 齐 | `packaging\payload\windows-amd64\` 里 `frpc.exe`（16,708,608 B）+ `openssl.exe` + **9 个 DLL** + `install_instance_tunnel.ps1` + `openssl.cnf`。**缺 frpc 就停**：取件脚本拉不到校验和时按设计整批不放 frpc，消息只在 stderr |
| vendor 齐 | `vendor\opus\windows\bin\x64\opus.dll`、`vendor\ftdi\windows\bin\x64\{ftd2xx,FT4222}.dll` |
| **VM 树上没有历史垃圾** | `Get-ChildItem vendor,static -Recurse -Force \| ? { $_.Name -like "._*" -or $_.Name -eq ".DS_Store" }` —— 必须 0（gotcha 14） |
| **VM 树上没有隐私残留** | `certs\`、`.env`、`recordings\`、`logs\`、`promo\` 都不该在构建机上（gotcha 15） |
| **删掉上一版产物** | `Remove-Item dist\windows\MRRC-Modern-Setup.exe` —— 这样"文件存在"才只可能来自本次构建（gotcha 10 的正解） |

---

## CRITICAL Gotchas（每条都对应一次真实失败）

1. **Never use `C:\Users\cheenle\build_vm.ps1`.** 它被改成 `Set-Location C:\mrrc_ft8`（另一个项目），会静默产出坏掉的 `MRRC_FT8-Setup.exe`（2026-08-15 中过）。每次发版用当次自己的脚本，`Set-Location C:\mrrc_modern`。
2. **打包排除清单是承重的。** 排除 `./.agents/*` ⇒ `tests/test_sdd_harness.py` 在 VM 上 24 项失败（harness 就住在那儿）；排除 `./website/*` ⇒ `test_sdd_docs_consistency.py` + `test_release_artifacts.py` 的 diagram-copy 规则 14 项失败（这些测试**读着陆页和生成的 SDD 页**）。`website/` 必须进包，只排除 `website/downloads/`、`website/videos/`、`website/__pycache__/`。`./certs/*` 必须排除（TLS 私钥）。漏 `./promo/*` ⇒ ~670 MB 的包传到天荒地老。
3. **PowerShell 5.1**：`&&` 非法（用 `;`）；`2>` 重定向写出 **UTF-16LE**（grep 前 `iconv -f UTF-16LE -t UTF-8`）；**多跳 ssh 的内联引号必被逐层吃掉** —— 永远写 `.ps1` → `scp` → `-File`（见"操作纪律"，本版为此白跑 4 次）。`Start-Process -ArgumentList` **拒绝空字符串元素**（`ParameterArgumentValidationError: …ArgumentList 执行参数验证失败，因为 Null 或空`），`-ArgumentList "--port","8899","--serial-port",""` 会在进程启动前就抛 —— 传个真值（比如只想"别碰电台"就传不存在的 `COM99`）。
4. **TX 音频在这台 VM 上永远无法验证。** KVM 的 USB 透传破坏等时 OUT 调度（MME 与 WASAPI 一样乱；RX 采集与 FT4222 批量传输不受影响）。别在这儿调 TX 噼啪声 —— 只能去物理 Windows 机。
5. **电台 USB 被拔会连带 COM 口和音频设备一起消失**，直到重新附加透传设备。VM 突然只剩 COM1、没有 `USB Audio`：电台被拔/关机了；重插后重跑三条 `sudo virsh -c qemu:///system attach-device win11`（VID:PID 循环见 `win_pack.md` §6）并重启服务。
6. **SSH 起的进程随会话结束被回收**（Windows job object）。长任务**同步跑**（`cmd /c "... > log 2>&1"`），别用裸 `ssh ... Start-Process`；要常驻就用计划任务（`schtasks /create /tn X /tr <命令行> /RL HIGHEST /RU <user> /IT`，**`/tr` 里别塞引号**；带空格的路径要 `\"…\"` 正确加引号，否则 `last=2147942402`）。运行器脚本放**固定目录**（`C:\tools\`），别放 `C:\tmp`（会被清理脚本删掉）。
7. **venv 保命**：本版做法是**解包覆盖、不删仓库目录**（tar 不含 `venv/`，所以 venv 自然保住），比旧的 `Move-Item venv → 删整个 C:\mrrc_modern → Expand-Archive → 移回` 少两个失败点。真要删了 venv，就跑 `win_pack.md` §2.2 的完整四条（从 `python -m venv venv` 开始），否则 `.\venv\Scripts\Activate.ps1` 不存在，构建立刻失败。
8. **依赖漂移**：`requirements*.txt` 变过就要重装依赖（保下来的 venv 里是旧依赖）。**发版中途不要引入新构建依赖** —— 本版宁可用标准库 AST 写守卫，也不往 `requirements-build.txt` 加 pyflakes/ruff，就是为了不惊动 VM 的 venv。
9. **宿主会 OOM 杀掉整个 VM，就在构建中途**（2026-09-12：解包跑了 ~5 分钟，VM 直接没了）。`ham.vlsc.net` 上还住着重量级邻居（曾有 ~9 GB 的 java 常驻），win11 在 28 GB 宿主上要 16 GB，内核挑了最大的进程 —— `qemu-system-x86`。症状依次：`ssh` 突然 `No route to host` → `virsh -c qemu:///system domifaddr win11` 说 domain not running → `/var/log/libvirt/qemu/win11.log` 结尾 `shutting down, reason=crashed` → `sudo dmesg -T | grep -i oom` 有 `Killed process … (qemu-system-x86)`。处置：

```bash
sudo virsh -c qemu:///system setmaxmem win11 10G --config && sudo virsh -c qemu:///system setmem win11 10G --config
sudo fallocate -l 16G /swap2.img && sudo chmod 600 /swap2.img && sudo mkswap /swap2.img && sudo swapon /swap2.img
sudo virsh -c qemu:///system start win11          # 先看 free -m：available <1 GB 就还会再死一次
```

邻居缩了之后可恢复 16 GB（`setmaxmem`/`setmem 16G --config`），否则就留 10 GB。

10. **`BUILD_DONE` / `BUILD_CMD_OK` / `Successful compile` 都不是成功信号。** 包装脚本无条件打印它；`Invoke-Checked` 中止的是*子* PowerShell，外层照样往下走。v1.15.0 有一次打印了 `BUILD_DONE`，而测试门禁是红的、exe 根本不存在。**正解是让"文件存在"本身成为证据**：构建前先 `Remove-Item` 掉上一版产物。
11. **解包用 `tar -xf`，不用 `Expand-Archive`。** Windows 自带的 bsdtar 几秒解完 ~13 MB 源码包，内存/CPU 都远低于那个 cmdlet（gotcha 9 的 OOM 就砸在一次 `Expand-Archive` 上）。脚本要**自证结果**：`EXTRACT_DONE venv=True server=True`。
12. **"装了但完全不启动"是启动器启动期异常，且没有任何可见消息。** 定位：用 `Start-Process … -RedirectStandardOutput o.txt -RedirectStandardError e.txt` 起 `C:\Program Files\MRRC Modern\MRRC-Modern-Launcher.exe`（注意是 *Launcher*，不是旧的 `MRRC-Modern.exe`），然后读 `e.txt` —— 用户的控制台窗口瞬间关了，traceback 还在文件里。v1.15.0 起启动器也写 `%LOCALAPPDATA%\MRRC-Modern\launcher.log` 并弹消息框。用这招抓到过的真因：ANSI(GBK) 编辑器存过的 `mrrc_modern.env` 不是合法 UTF-8（`e2 80 3f`，出厂模板是 `e2 80 94`）⇒ `load_env` 在打印任何东西之前抛 `UnicodeDecodeError`。修在 `macos/first_run.py: read_env_text`（BOM → UTF-8 → cp936 → latin-1）+ `guarded_main()/report_fatal()`。两个启动器共用这个读取器 —— **改启动器意味着两端安装包都要重建。**
13. **只在 Windows 红的测试，几乎都是文件句柄没关。** macOS/Linux 允许删除打开的文件，Windows 报 `PermissionError: [WinError 32] … being used by another process`，而且**炸在 `TemporaryDirectory.cleanup()` 阶段**（离真正的原因隔了两层）。关闭动作必须写在 **`with tempfile.TemporaryDirectory()` 块内部** —— 写在块外的 `finally` 里，删目录时句柄还开着，照样红：

```python
with tempfile.TemporaryDirectory() as tmp:
    try:
        ...                                   # 这里挂上了 RotatingFileHandler
    finally:
        for h in list(logging.getLogger().handlers):
            if h not in saved:
                logging.getLogger().removeHandler(h); h.close()   # 必须在 with 里面
```

把它当**真实的跨平台 bug**，不要当 VM 怪癖。
14. **`COPYFILE_DISABLE=1` + 排除 `._*`/`.DS_Store`，否则 macOS 元数据会进包发给用户。** spec 把 `static/`、`vendor/` **整目录**塞进 `datas`，所以 `static/.DS_Store`、`vendor/…/._ftd2xx.dll` 会真的装到用户机器上，而且 FastAPI 的静态处理器会照请求把它们发出去。解包**只覆盖不删除**，所以 VM 树上的历史垃圾要在构建前手工清一遍（本版清了 46 个）。
    **2026-10-03 又栽一次，且这次是清得不彻底**：树里有 **53** 个（历次不带排除的源码拷贝留下的），构建把其中 **6** 个拷进了包（`_internal\static\.DS_Store`、`vendor\ftdi\windows\bin\x64\._*.dll`）⇒ 结构核对的 junk 必须为 0 ⇒ **作废整个构建**。清零后产物从 54,122,001 变成 54,115,523 字节 —— **体积差就是那 6 个文件**，可以当交叉证据用。教训：解包前的清理必须**同时覆盖旧测试和 junk**（`tests\*.py` + 递归 `._*`/`.DS_Store`），且清理后要再断言一次为 0。
    **另一面**：若某个 exe 的 mtime 比同一批构建早，先别慌 —— PyInstaller 只重建输入变了的那些；确认 Canary 是**归档本身**：读那个被复用的 exe 的 CArchive TOC，junk 条数必须是 0（本次 launcher 就是这样被确认干净的）。
15. **构建机上不该有任何密钥或个人数据。** 本版在 `C:\mrrc_modern` 上发现历次 tar 留下的 `certs\radio.vlsc.net.key`（+ `.orig` 备份）、含 `MRRC_WEB_PASSWORD` 的 1817 字节 `.env`、**63 个真实 QSO 录音（125.9 MB）**、运行日志、`promo\`（629.9 MB）。全部删除并核实，源码 tar 加上排除项。**事后还要在产物里查一遍**：`.pem/.key/.p12/.pfx/.env` 形状的文件数必须是 0。
16. **fleet payload 在应用根目录，不在 `_internal\`。** `build.ps1:106` 是 `FleetDest = $AppRoot\fleet` ⇒ `dist\windows\MRRC-Modern\fleet\`（13 个文件）。在 `_internal\fleet` 找会误判"payload 丢了"。
17. **静默安装不会拉起应用。** Inno 的 `[Run]` 带 `skipifsilent` ⇒ 装完自己起一次；隧道会在应用调 `/api/cloud/state`（打开设置对话框）时自己起来。
18. **`_APP_MODULES` 之外新增的模块不能被热修覆盖**（见 `mrrc` 那份技能）；而 `server.py`、`windows/launcher.py` 是**冻结入口脚本**，热修通道根本覆盖不到 —— 改它们**只能重建安装包**。
19. **看产物时间务必带年份。** 差点把 9-25 的旧 exe 当成当晚构建（只看了 `HH:mm:ss`）。用 `.ToString("yyyy-MM-dd HH:mm:ss")`。
20. **同一版本号不能重发。** 升级通道比的是**版本串**：已经以 vX 上线的包，修好了也**永远送不到**装了同一个 vX 的机器（点"升级"只会反复重放同一版本）。修完必须**升号**。
21. **验证脚本本身必须被验证 —— 否则它会安静地说谎。** 2026-10-03 一个小时内被自己的检查器坑了五次，每次都“看起来绿”：
    - **PowerShell 变量名大小写不敏感**：报告列表叫 `$L`，装原始日志用了 `$l` ⇒ **报告列表被进程输出覆盖**，脚本“成功退出”而写出的是别人的 stdout，整个第 1 层的检查结果全丢。命名上要刻意区分（`$report` / `$rawOut`）。
    - **裸 `000` 是整数 0**：`Check "http /login" 000` ⇒ 期望值变成 `0`，而 curl 返回的是字符串 `"000"` ⇒ 永远不等。**状态码一律加引号**。
    - **时间窗要从“运行前”算起**：`find -newermt '-3 minutes'` 在构建刚结束时会把几百个产物文件全算成“被改过”（假红）；反之窗口错过就是**空跑假绿**。正确做法：运行前 `touch` 一个基线文件，运行后 `find <dir> -newer <基线>` 必须为 0。
    - **先确认应答者是本次实例，再相信端点结果**：上一次跑留下的野实例还占着端口，于是新实例没绑上、日志只有 2 行，而 `/login` 依然 200（旧实例应的）。断言方式：**等本次实例自己写下 `starting on port <端口>`**，再往下检查。
    - **符号走查必须用子串匹配**：`print("Detecting the radio on %d …" % (...))` 在 `co_consts` 里是**整条格式化串**，用精确集合成员查会报假 MISS。`sym in consts` 之外还要 `any(sym in c for c in consts)`。
    - 一句话：**“检查通过”必须是可核对的事实，不是脚本自己说的。** 每次问一句：这条断言在坏输入上会不会也绿？
22. **洁净室要跑冻结核的 *Launcher*，不只是 Server。** Server 免检不了首次运行探测那条路（它住在 `macos/first_run.py`，由启动器调用）—— 而 v1.25.0 的头号缺陷正好在那儿（探测永久卡死）。做法：给一个**隔离的 `LOCALAPPDATA`** + **预先放一份配置在空闲端口**（这道 VM 上的 8888 是常驻租户实例，不预置端口启动器会直接说"Already running"然后什么都不做）+ `$env:BROWSER='no-such-browser'`（webbrowser 静默失败，不弹窗）。然后断言三件事：**探测前那行打出来了**、配置最终settle（口令/`MRRC_FIRST_RUN_DONE=1`）、以及它真的把 server 起起来了（`https://127.0.0.1:<空闲端口>/login` = 200）。

---

## 验证：四层，缺一层就等于没验

### 第 1 层 三证合一（不是"看起来成了"）

```powershell
$i = Get-Item C:\mrrc_modern\dist\windows\MRRC-Modern-Setup.exe
$i.LastWriteTime.ToString("yyyy-MM-dd HH:mm:ss")     # 必须是今天，带年份
$i.Length                                            # 必须 != 上一版
(Get-FileHash $i.FullName -Algorithm SHA256).Hash     # 必须 != 上一版
Get-Content C:\mrrc_modern\dist\windows\MRRC-Modern\version.txt   # 必须 == 本版
```

### 第 2 层 结构核对

三个 exe（`MRRC-Modern-Server.exe` ~8.9 MB、`MRRC-Modern-Launcher.exe` ~11.9 MB、`scope_pipe.exe` ~10.0 MB）；`_internal\` 下 `static\index.html`、`static\listen.js`、`mem_channels.json`、`windows\default.env`、`vendor\ftdi\windows\bin\x64\{ftd2xx,FT4222}.dll`、`vendor\opus\windows\bin\x64\opus.dll`；**应用根目录** `fleet\` 13 个文件；`.DS_Store`/`._*` **0 个**；`.pem/.key/.env` 形状 **0 个**。

### 第 3 层 符号走查（`grep`/`strings`/`findstr` 看不进压缩的 PYZ）

缺的符号和在的符号长得一模一样，只能反序列化归档。**两个必须记住的坑**：

- 经 `getattr(socket, "SO_EXCLUSIVEADDRUSE", 1024)` 读的名字是**字符串常量（`co_consts`）**，不在 `co_names` 里 —— 只扫 `co_names` 会误报"缺失"。
- 符号要**在它真正定义/调用的那个模块里**找：`load_user_config_into_environ` 在 `config` 的**模块体**里被调用，所以查 PYZ 里的 `config`，而不是查 `server` 入口。

```python
# C:\tools\bundle_check.py —— 用 VM 的 venv 跑（它有 PyInstaller）
import marshal, os, tempfile, types
from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

def walk(code, names, consts):
    names.update(code.co_names)
    for k in code.co_consts:
        if isinstance(k, types.CodeType): walk(k, names, consts)
        elif isinstance(k, str): consts.add(k)
    return names, consts

r = CArchiveReader(r"C:\mrrc_modern\dist\windows\MRRC-Modern\MRRC-Modern-Server.exe")
raw = r.extract("server")                       # 入口"脚本"是 CArchive 条目，不是 PYZ 模块
try:    code = marshal.loads(raw)
except Exception: code = marshal.loads(raw[8:]) # 可能带 8 字节头
names, consts = walk(code, set(), set())

pyz = next(k for k, v in (r.toc.items() if hasattr(r.toc, "items") else r.toc)
           if (getattr(v, "typecode", None) or (v[4] if len(v) > 4 else None)) == "z")
fd, tmp = tempfile.mkstemp(suffix=".pyz"); os.write(fd, r.extract(pyz)); os.close(fd)
mods = set(ZlibArchiveReader(tmp).toc.keys())   # PyInstaller 6: r.toc 是 dict，r.toc[0] 会 KeyError
```

### 第 4 层 洁净室真跑（**唯一**能抓到"包是坏的"那一层）

以**空的 per-user 状态** + **不给启动器环境变量**跑打包出的 exe —— 等同安装菜单里那个 "MRRC Modern Server" 快捷方式的裸启动。**必须用现场机的配置，不能用默认值**：本版两个缺陷（显式 host 绕过端口守卫、冻结包写坏签名 bundle）**只在 `MRRC_WEB_HOST=127.0.0.1` 这条路上**，默认 `::` 那条全是绿的。

必查清单（每条都对应一个真实缺陷）：

| 查什么 | 判据 | 抓的是哪个缺陷 |
| --- | --- | --- |
| 证书 | 日志有 `signed a self-signed certificate for 127.0.0.1` + `SSL enabled with a self-signed certificate just generated` | v1.24.5：模块在包里却没 import，`NameError` 被宽 `except` 吞掉后静默退回纯 HTTP |
| 真的在服务 TLS | `https://127.0.0.1:<port>/login` → **200**；同端口明文 HTTP → **0 字节**/失败 | "黑屏" = 浏览器被送到没人监听的 scheme |
| 证书主体 | 原始 TLS 握手报 `CN=localhost`，而 **SAN** 里含 `IP Address=127.0.0.1`（PS 5.1 里用 `SslStream.AuthenticateAsClient`，**不要**用 `Invoke-WebRequest`，见操作纪律）。2026-10-05 实测 v1.25.2：`DNS:localhost, DNS:<主机名>, DNS:<主机名>.local, IP Address=127.0.0.1, IP Address=::1, IP Address=<LAN 地址>` | 签给 0.0.0.0、或只签了 CN 而 SAN 不含 `127.0.0.1`，就会与启动器实际打开的 `https://127.0.0.1:<port>` 不匹配（浏览器校验看 **SAN**，不看 CN —— 本行原先写「报 `CN=127.0.0.1`」，那是更早一版的证书命名，属过时断言） |
| 读到用户配置 | 绑的是配置文件里的 host/port，不是默认 `:::8888` | 裸启动完全无视 `mrrc_modern.env` |
| 写在哪 | `Recording ready:` 指向**用户数据目录**；安装目录/bundle 里**没有**多出 `mrrc_modern.env`、`.tmp`、`certs\`、`recordings\` | 密码存不下 ⇒ 每次启动换一个新密码（"装了新包还是登不上"）；macOS 写进签名 bundle ⇒ `a sealed resource is missing or invalid` |
| 日志不重复 | 单实例跑，`行数 ≈ distinct 行数`（允许代码本来就打两次的那种，如 `Opening serial port`） | root logger 是进程级的，冻结包把模块当 `__main__` 和 `server` 各加载一次 ⇒ 每行写两遍 |
| 第二实例 | **退出码非 0** + 报错里点名端口和"另一个 MRRC Modern 还在跑" + **不再打 `Server ready!`** + `netstat` 只剩 1 个 LISTENING | Windows 的 `SO_REUSEADDR` 允许第二个进程绑到正在监听的端口，两个实例同时"就绪"，浏览器可能落到没拿到电台那一个 |
| 装完真跑 | 静默装 → 起应用 → 入口 302/200/401 | `[Run]` 的 `skipifsilent`（gotcha 17） |

---

## 操作纪律：反复自伤的那几类（与 Windows 无关，纯纪律）

**并发**

- **绝不把有依赖关系的两条命令放进同一个并行块。** 实测翻车四次：① 同一块里发了两次全量套件 ⇒ 两个进程抢端口，打出 3 failures + 3 errors 的**假红**；② 变异脚本（会重写 `server.py`）与全量套件并行 ⇒ 套件在被改坏的文件上跑；③ **同一条 `deploy.sh` 并行发两次 ⇒ 线上 `sdd/` 和 `images/` 被删光**；④ `git commit` 与 `git push` 同块 ⇒ push 先跑，报 "Everything up-to-date"，提交其实没推上去。
- 判据：**只要两条命令可能碰同一个文件、同一台机器、同一个远端目录，就串行。**

**远端 shell**

- **永远不要把 PowerShell 内联进 ssh。** `\"` 转义会被逐层吃掉，症状是"脚本没错、命令没跑"或 `TerminatorExpectedAtEndOfString` / `EmptyPipeElement` / `FINDSTR: 无法打开`。做法：`write` 工具落 `.ps1`（**纯 ASCII**，用 `LC_ALL=C grep -c '[^ -~]'` 自查必须 0）→ `scp` 到 `C:\tools\` → `powershell -NoProfile -ExecutionPolicy Bypass -File`。
- 需要参数化就用 `param([string]$Path=..., [string]$Pattern=..., [int]$First=...)`，一个 `readlog.ps1` / `greplog.ps1` 能覆盖所有"看日志"的需求。
- **GBK 输出**：中文 Windows 的 stderr 是 GBK，`sed`/`grep` 会报 `RE error: illegal byte sequence` ⇒ 前置 `LC_ALL=C`，或 `LC_ALL=C tr -cd '\11\12\15\40-\176\200-\377'` 过一遍再匹配。数据行仍可读，只是表头会花。

**读证据**

- **失败日志要读尾巴，不是读头。** 本版一度断定"第二个实例绑定成功了"，因为只看了 stderr 的**前 20 行**（里面确实有 `Server ready!`）；真正的 `RuntimeError` 在**最后**。用 `Get-Content -Tail` / `tail -n`。
- **`head`/`tail` 截断过的输出不能当完整事实。** 一次 `tail -14` 把关键的 `df` 三行截掉了，而那正是要判断"`/tmp` 是不是小 tmpfs"的依据。
- **"某个东西不存在"要用两种方式确认**（换路径 + 换命令）。曾报 "fleet MISSING"，实际是查错了目录（gotcha 16）。

**本地 shell**

- **`&` 的优先级低于 `&&`**：`cd X && A &` 会把**整个 `cd X && A`** 丢进后台子 shell，主 shell 的 cwd 没变，后面的相对路径全部失效。要后台跑就写成 `( cd X && A ) &` 或直接用绝对路径。
- **带引号的 heredoc（`<<'EOF'`）里反斜杠是字面量**：写 `"\\n"` 得到的是**反斜杠 + n**，不是换行（本版因此在测试文件里多出一行 `\n`，直接 SyntaxError）。
- **字符串结尾的 `\` 会转义引号**：`echo "...C:\tools\"` ⇒ 引号没闭合 ⇒ `unexpected EOF while looking for matching '"'`。
- **`bash -n` 检查不到 heredoc 里的远端脚本**（对它来说那只是一个字符串）。要把每个 `<<'EOF' … EOF` 块抽出来单独 `bash -n`，否则远端语法错会在解包之后、reload 之前断掉，把站点留在更糟的状态。
- macOS 是 BSD 工具链：`cat -A`、`du --exclude`、`sed -i`（要 `sed -i ''`）、`stat -c`（用 `stat -f`）都不可用。

**改代码**

- **改完必须做变异验证**：把修复逐条改回坏行为，对应测试**必须变红**；不变红说明测试是装饰品。变异脚本的 anchor **必须唯一**（本版 `if getattr(sys, "frozen", False):` 在文件里出现 2 次，断言正确地拦住了它），并且**变异完一定要还原并 `cmp` 核对**。
- **文档里的声明必须与实测一致，宁窄勿宽。** 本版两次在 CHANGELOG 里写过宽的声明（"端口冲突会重试并点名另一个实例"、"裸启动不再往安装目录写"），都被洁净室验收当场推翻。写声明前先问：**这条在用户实际的那条配置路径上成立吗？**
- **改 `.agents/skills/*/SKILL.md` 之后必须跑 `tests/test_skill_docs_consistency.py`。** 实测（2026-10-03）：某个条目内部含**围栏代码块**时，保存触发的 markdown 格式化器会把该条目**之后**的编号当成一个新列表，从 1 重新编号并加前导空格 —— 于是 gotcha 10–13 变成了「 1.– 4.」，编号连续性、隐藏条目、交叉引用共 6 条测试变红。内容没丢，但引用（`gotcha 13`）全部指错。修法：用**不经格式化器**的方式改回编号（脚本直接改文件），然后重跑那套测试确认。
- **源码级守卫用 AST，不用子串匹配。** `assertNotIn("uvicorn.run(", source)` 会命中**本文件 docstring 里解释这个缺陷时引用的那句话**，把健康的树报成坏的（而且 `assertNotIn` 会把整个源文件当容器打印出来，一次刷掉 50 KB）。

---

## Website Deploy

产物是**服务器侧管理**的：`website/deploy.sh` 的 tar **排除 `downloads/`**（也排除 `videos/`）。

```bash
# ① 上传到 /var/tmp —— 不是 /tmp：www.vlsc.net 的 /tmp 是 958 MB tmpfs（RAM），
#    而 /var/tmp 与 webroot 同在 /dev/vda1 ⇒ mv 原子、不占内存
scp dist/windows/MRRC-Modern-v<ver>-Windows-x64-Setup.exe cheenle@www.vlsc.net:/var/tmp/win.new
scp dist/macos/MRRC-Modern-v<ver>-arm64.dmg              cheenle@www.vlsc.net:/var/tmp/mac.new
scp website/downloads/latest.json                        cheenle@www.vlsc.net:/var/tmp/latest.new

# ② 移动前先在服务器侧算 SHA（上传被截断就会发出坏包）
ssh cheenle@www.vlsc.net 'for f in win mac; do stat -c%s /var/tmp/$f.new; sha256sum /var/tmp/$f.new; done'

# ③ 就位：版本化名单独存在（旧版本保留为归档），通用名就地替换；latest.json 最后放
#    （先放清单会出现"清单指向一个还不存在的文件"的窗口期）
ssh cheenle@www.vlsc.net 'bash -s' <<'EOS'
D=/var/www/vlsc.net/mrrc_modern/downloads
sudo -n mv /var/tmp/win.new "$D/MRRC-Modern-v<ver>-Windows-x64-Setup.exe"
sudo -n mv /var/tmp/mac.new "$D/MRRC-Modern-v<ver>-arm64.dmg"
sudo -n cp "$D/MRRC-Modern-v<ver>-Windows-x64-Setup.exe" "$D/MRRC-Modern-Setup.exe"
sudo -n cp "$D/MRRC-Modern-v<ver>-arm64.dmg"             "$D/MRRC-Modern-arm64.dmg"
sudo -n mv /var/tmp/latest.new "$D/latest.json"
sudo -n chown www-data:www-data "$D"/MRRC-Modern*; sudo -n chmod 644 "$D"/MRRC-Modern* "$D/latest.json"
EOS

# ④ HTML：cd website && echo y | ./deploy.sh   （一次！内部有备份 + nginx -t + reload）
# ⑤ 公网复核：HEAD 的 content-length 不足以证明文件完好，必须完整下载后比 SHA
curl -s -o /tmp/v.exe https://www.vlsc.net/mrrc_modern/downloads/MRRC-Modern-Setup.exe && shasum -a 256 /tmp/v.exe
curl -s https://www.vlsc.net/mrrc_modern/downloads/latest.json | python3 -m json.tool
```

**两个通用名（快速镜像）都要更新**：`MRRC-Modern-Setup.exe` 被站点 6 处链接（含两个主下载按钮），`MRRC-Modern-arm64.dmg` 虽然当前没有链接指向它，但留着旧版就是个陷阱（本版发现它停在 **v1.24.0**，落后 5 个版本）。

**`deploy.sh` 只能一次跑一个**（脚本内已有 `mkdir` 锁）。它的 prune 步骤曾把线上 `sdd/` 与 `images/` 删光：并发两次 ⇒ 先跑完的那次删掉所有 tarball ⇒ 后跑的那次 `TARBALL` 为空 ⇒ `tar -tzf ""` 失败但**管道退出码取自最后一个命令 `sort -u`（成功）**，`set -e` 拦不住 ⇒ 清单为空 ⇒ `comm -23` 把磁盘上每个文件都判成过期 ⇒ 全删。现已加三道防呆（缺包不删 / 清单空不删 / 过期数 ≥ 发布数不删）+ 并发锁。**部署完必须逐个 URL 验 200**，别只看脚本打印的 "Deployment Complete!"。

---

## Common Mistakes

| Symptom | Cause / Fix |
| --- | --- |
| Build produces `MRRC_FT8-Setup.exe`, `C:\mrrc_modern\dist` empty | Used the hijacked `build_vm.ps1` (gotcha 1); use a per-version script |
| 24 harness test failures on the VM | Source package excluded `./.agents/*` (gotcha 2) |
| 14 doc/diagram test failures on the VM | Source package excluded `./website/*` (gotcha 2) |
| `.\venv\Scripts\Activate.ps1` not found at build | venv deleted during extract (gotcha 7); re-run §2.2 full four commands |
| `&&` parse error / empty grep over redirected output | PowerShell 5.1 quirks (gotcha 3); use `;` and scripts |
| `TerminatorExpectedAtEndOfString`, `EmptyPipeElement`, `FINDSTR: 无法打开` | Inline PowerShell over ssh (gotcha 3): write `.ps1` → scp → `-File` |
| `sed: RE error: illegal byte sequence` on VM output | GBK console: prefix `LC_ALL=C` |
| TX audio crackles on the VM | KVM isochronous OUT is broken — do not debug (gotcha 4); physical hardware only |
| COM ports + USB audio vanish on the VM | Radio USB unplugged; re-attach passthrough (gotcha 5) |
| Service disappears after SSH logout | SSH-launched process killed by job object (gotcha 6); scheduled task |
| `virsh list` shows no win11 VM | Missing `sudo` + `qemu:///system` |
| VM vanishes mid-build, `No route to host`, `domifaddr` says not running | Host OOM-killed qemu (gotcha 9): shrink to 10 GB + add swap |
| Script printed `BUILD_DONE`/`BUILD_CMD_OK` but no (or an old) exe | Unconditional print (gotcha 10): delete the previous artifact **before** building, then check mtime + size + hash |
| `iscc: The output file appears to be in use (32)` and the file does not exist afterwards | Defender locked the fresh exe; `build.ps1` already compiles to a scratch dir and copies in |
| Package contains `.DS_Store` / `._*.dll` | tar without `COPYFILE_DISABLE=1` + no `._*` exclude, and the VM tree kept old junk (gotcha 14): clean the tree, rebuild |
| `certs\`, `.env`, `recordings\` found on the build VM | Earlier tarballs shipped them (gotcha 15): delete, add the excludes, and consider rotating the keys |
| "fleet payload missing" but the build log says `Fleet payload: frpc.exe, …` | Looked in `_internal\fleet`; it is at the **app root** (gotcha 16) |
| Installed app does nothing, console flashes | Launcher startup exception — reproduce with redirects / read `launcher.log` (gotcha 12); check the env file's bytes on `UnicodeDecodeError` |
| App installed silently but is not running | `[Run]` has `skipifsilent` (gotcha 17): start it yourself |
| Tests green on the Mac, `WinError 32` on the VM | A test leaked an open file **and closed it outside the `with` block** (gotcha 13) |
| Suite shows failures nobody can reproduce | Two suite runs (or a mutation script + a suite) in one parallel block, fighting over ports/files: run them serially |
| `git push` says "Everything up-to-date" but the commit is local | commit and push were in the same parallel block: push ran first |
| Bundle walk says a symbol is missing, but the code is there | It is a `getattr(..., "NAME", …)` **string constant**, or it lives in another module (Verification layer 3) |
| A "fixed" build still shows the old behaviour | The change is in a frozen entry script (`server.py`, `windows/launcher.py`) — the hotfix overlay cannot reach it (gotcha 18): rebuild |
| An installed box never receives the rebuilt package | Same version string (gotcha 20): the upgrade channel compares versions — bump it |
| Live site pages 404 after a deploy | Two concurrent `deploy.sh` runs → empty prune manifest (Website Deploy): re-run once, then verify every URL |
