# Windows 安装包打包流程（ham.vlsc.net KVM 虚拟机）

> 用途：在 ham.vlsc.net 上的 Win11 KVM 虚拟机中构建并冒烟验证 `MRRC-Modern-Setup.exe`。软件/安装器验证不等同于真实射频验收；TX 话音质量仍需带 FT-710 USB 音频和监听接收机的物理链路确认。
> 本文按 2026-07-25 首次成功打包（v1.6.3）的实际操作整理，照做即可复现。
> 最新构建：**v1.9.0**（2026-08-17，提交 `928d248`，产物 45,324,627 bytes，SHA-256 `f4d8e2a2…90fd6734`）。
> 用户向的安装/使用说明见 [docs/WINDOWS_INSTALLER_GUIDE.md](docs/WINDOWS_INSTALLER_GUIDE.md)，本文是**打包方**的操作手册。

## 1. 环境拓扑

```
本机 Mac (mrrc_modern 工作区)
   │  ssh ham.vlsc.net          （Ubuntu 24.04 KVM 宿主机，sudo 免密）
   ▼
ham.vlsc.net
   │  ssh cheenle@192.168.122.133  （libvirt 默认 NAT 网段）
   ▼
win11 虚拟机 (desktop-ssddf0b)
   C:\mrrc_modern            源码工作区
   C:\Users\cheenle\*.ps1   辅助脚本
   dist\windows\            构建产物
```

- VM 管理：`sudo virsh -c qemu:///system list --all`（必须带 `qemu:///system` 且用 sudo，普通 `virsh list` 看不到）
- VM IP 查询：`sudo virsh -c qemu:///system domifaddr win11`
- VM 的 22 端口有 OpenSSH Server，默认 shell 是 **PowerShell 5.1**（写命令时注意，见 §6 坑列表）

## 2. 一次性准备（已完成，仅备查）

以下环境 2026-07-25 已配好，正常情况下跳到 §3。

### 2.1 SSH 免密进 VM

Windows OpenSSH 对**管理员用户**只读 `C:\ProgramData\ssh\administrators_authorized_keys`（不是用户目录下的 `authorized_keys`），且 ACL 必须只有 Administrators/SYSTEM：

```powershell
Add-Content -Path C:\ProgramData\ssh\administrators_authorized_keys -Value '<宿主机的 ssh-ed25519 公钥>'
icacls C:\ProgramData\ssh\administrators_authorized_keys /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F"
```

宿主机 ham.vlsc.net 的 `~/.ssh/id_ed25519` 公钥已加入，故从 Mac 可一条链路直连：
`ssh ham.vlsc.net` → `ssh cheenle@192.168.122.133`（宿主机上装有 `sshpass` 备用）。

### 2.2 VM 上的构建软件

- Python 3.12.4（系统级，`python` / `py` 均在 PATH）
- Inno Setup 6：`C:\Program Files (x86)\Inno Setup 6\iscc.exe`（不在 PATH，`build_vm.ps1` 会临时加）
- 源码区 `C:\mrrc_modern` 下已建 venv（`venv\Scripts\python.exe`），依赖已装齐

重建依赖（源码更新后只需重跑后两条）：

```powershell
cd C:\mrrc_modern
python -m venv venv
.\venv\Scripts\python.exe -m pip install --upgrade pip
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe -m pip install -r packaging\windows\requirements-build.txt   # pyinstaller==6.21.0 锁版
```

### 2.3 FTDI DLL（真 FFT 频谱）

需要 `vendor\ftdi\windows\bin\x64\FT4222.dll` 和 `ftd2xx.dll`，缺了也能打包（S 表回退频谱），build.ps1 只警告。
FTDI 官网对脚本下载返回 403，需在浏览器手动下载两个包放到仓库 `vendor/ftdi/`：

- `LibFT4222-v1.4.8.zip` → 其中 `imports\LibFT4222\dll\amd64\LibFT4222-64.dll` **改名为 `FT4222.dll`**
- CDM 驱动包（如 `CDM2123620_Setup.zip`）→ 里面只有一个安装器 exe，用 **bsdtar 可直接抽取**：
  `tar -xf CDM2123620_Setup.exe amd64/FTD2XX64.dll`，把 `amd64/FTD2XX64.dll` **改名为 `ftd2xx.dll`**

注意 LibFT4222 zip 内部用反斜杠当路径分隔符（Info-ZIP 解压报警告属正常），且解压后目录权限可能异常，`chmod -R u+rwX .` 修复。

> **IC-7300 / IC-7300MK2 不需要 FTDI 驱动**：这两款电台只通过单条 USB 线连接，使用系统自带的 USB-serial 驱动即可；不装 FTDI DLL 也能正常打包和运行，CI-V 频谱通过同一串口上的 0x27 命令获取。

### 2.4 USB 设备直通（真机测试，2026-07-26 已配好）

FT-710 的 USB 接在宿主机 ham.vlsc.net 上（经 Cypress TetraHub），三个设备已用
`virsh attach-device win11 <xml> --live --config` 按 VID:PID 直通进 VM（持久化，重启 VM 不失效）：

| 设备 | VID:PID | VM 内表现 |
|------|---------|-----------|
| CP2105 双串口（CAT + PTT） | `10c4:ea70` | COM3 = **Standard**（无 CAT），COM4 = **Enhanced**（CAT 用它） |
| FTDI FT4222H（频谱 SPI） | `0403:601c` | FT4222H Interface A/B，FTDIBUS 驱动 2.12.36.20（Windows 自动装） |
| C-Media USB Audio（FT-710 声卡） | `0d8c:0013` | "USB Audio Device"，自动检测可匹配 |

两个一次性动作（已完成）：VM 内装 Silicon Labs `CP210x_Universal_Windows_Driver`（`pnputil /add-driver silabser.inf /subdirs /install`，装完 COM3/COM4 才从 Error 变 OK）；
`%LOCALAPPDATA%\MRRC-Modern\mrrc_modern.env` 的 `FT710_SERIAL_PORT` 从默认 COM3 改为 **COM4**（FT-710 示例：Enhanced 口才有 CAT 响应，`FA;`→`FA007050000;` 验证过）。

远程访问（2026-07-26 v2，HTTPS 终结在 VM；v1.7.6 起安装包已原生支持默认 HTTPS（launcher 首启自动生成自签证书），**但这台 VM 的常驻服务仍走隐藏任务 + `start_mrrc_modern_https.ps1`**——原生 launcher 是控制台应用，控制台被关/会话注销服务即死（当天实测踩过），隐藏 Start-Process 路径没有这个问题）：VM 的 `%LOCALAPPDATA%\MRRC-Modern\mrrc_modern.env` 设 `FT710_WEB_HOST=0.0.0.0`、
`FT710_WEB_PORT=8443`，防火墙规则 "MRRC Modern Web 8443" 放行入站。启动不走安装包自带的 launcher（它写死
`--no-ssl`），而是用 `C:\Users\cheenle\start_mrrc_modern_https.ps1`：读 mrrc_modern.env → 设 `FT710_SSL_CERT/KEY` 指向
`C:\ProgramData\MRRC-Modern\certs\`（ham.vlsc.net 的 LE 证书）→ `Start-Process` 拉起 `MRRC-Modern-Server.exe`（日志在
`%LOCALAPPDATA%\MRRC-Modern\server_console.log(.err)`）。开机自启：计划任务 `MRRC-Modern-HTTPS`（cheenle 登录时触发）。
ham 侧是 systemd 服务 `mrrc_modern-proxy.service`（socat 纯 TCP 透传 8443→VM:8443，TLS 不过 nginx）。
外网入口 `https://ham.vlsc.net:8443`——ham.vlsc.net 只有 AAAA 记录，客户端必须有 IPv6。
**为什么必须 HTTPS**：HTTP + 公网域名不是 secure context，浏览器会禁用 AudioWorklet 和 `getUserMedia`
（音频/PTT 全废）；另注意家庭宽带对境外方向封 80/443 入站，所以证书只能走 acme.sh DNS-01（Aliyun，凭据取
自 `~/DNS/aliddns.py` 的活动 key——`~/aliddns.py` 里那把已停用），续期后 reloadcmd 自动 scp 证书到 VM 并跑
`C:\Users\cheenle\restart_mrrc_modern.ps1` 重启应用。
另：v1.7.2 起安装包自带 Windows libopus（仓库 `vendor/opus/windows/bin/x64/opus.dll`，取自 PyOgg wheel，
已加入 `mrrc_modern_server.spec` datas）；此前的包没有它，服务端 Opus 全废（RX 退 PCM、TX 无声），需手动补 DLL。

## 3. 每次打包流程

### Step 0 — 本地检查

```bash
venv/bin/python -m unittest discover -s tests        # 必须全绿（当前 593）
```

确认版本号一致：`CHANGELOG.md` 最新条目、`packaging/windows/MRRC-Modern.iss` 的 `MyAppVersion`。

### Step 1 — 打源码包（在本机仓库根目录）

```bash
mkdir -p dist && rm -f dist/mrrc_modern_src.zip
zip -qr dist/mrrc_modern_src.zip . \
  -x "./.git/*" "./venv/*" "./.venv/*" "./dist/*" "./build/*" "./logs/*" "./certs/*" \
     "./FT710Mobile/*" "./website/*" "./lib/*" "./__pycache__/*" "./windows/__pycache__/*" \
     "./tests/__pycache__/*" "./.pytest_cache/*" "./.claude/*" "./.superpowers/*" \
     "./.agnes/*" "./*.pyc" "./.DS_Store" "./SDD/.DS_Store"
```

**关键**：`./.agents/*` 不能排除——`tests/test_sdd_harness.py` 依赖其中的 harness 文件，缺了会导致 VM 上 24 个测试失败。`./certs/*` 必须排除（含 TLS 私钥）。

**建议**：另加 `./promo/*` 排除——那是 gitignored 的市场宣传视频（约 630MB），与构建/测试无关；不排除也能构建，但上传极慢（v1.8.0 起已排除）。

### Step 2 — 上传到 VM（经 ham 跳板）

```bash
scp dist/mrrc_modern_src.zip ham.vlsc.net:/tmp/
ssh ham.vlsc.net "scp /tmp/mrrc_modern_src.zip cheenle@192.168.122.133:mrrc_modern_src.zip"
```

### Step 3 — VM 上解压

```bash
ssh ham.vlsc.net "ssh cheenle@192.168.122.133 'powershell -NoProfile -Command \"Set-Location C:\Users\cheenle; if (Test-Path C:\mrrc_modern) { Remove-Item C:\mrrc_modern -Recurse -Force }; Expand-Archive mrrc_modern_src.zip -DestinationPath C:\mrrc_modern\"'"
```

如 `requirements*.txt` 有变化，重跑 §2.2 的 pip 两条。

**注意**：这一步的删除会把 `C:\mrrc_modern\venv` 一起删掉（zip 不含 venv），
之后 `build_vm.ps1` 会因找不到 `.\venv\Scripts\Activate.ps1` 直接失败——
删过目录就必须重跑 §2.2 的**完整四条**（含 `python -m venv venv`）。
若删除时报 `server_console.log` 被占用，是 VM 上 `start_mrrc_modern.ps1` 拉的
`python server.py --no-ssl` 实例持有该文件，先停掉它再解压（见 §5 表）。

### Step 4 — 构建

VM 上已有辅助脚本 `C:\Users\cheenle\build_vm.ps1`（内容是：Inno 目录入 PATH → 激活 venv → 调 `packaging\windows\build.ps1`）：

```powershell
$ErrorActionPreference = "Stop"
$isccDir = "${env:ProgramFiles(x86)}\Inno Setup 6"
if (Test-Path "$isccDir\iscc.exe") { $env:Path = "$isccDir;" + $env:Path }
Set-Location C:\mrrc_modern
.\venv\Scripts\Activate.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\windows\build.ps1
Write-Host "BUILD_DONE"
```

远程执行（约 3-5 分钟：439 个测试 → 3 个 PyInstaller spec → iscc）：

```bash
ssh ham.vlsc.net "ssh cheenle@192.168.122.133 'powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\cheenle\build_mrrc_v180.ps1'"
```

⚠️ **别用 `C:\Users\cheenle\build_vm.ps1`**——它当前 `Set-Location C:\mrrc_ft8`，指向的是**另一个项目**
（MRRC_FT8 工作区），会构建出错的 `MRRC_FT8-Setup.exe`（2026-08-15 实测踩坑，产物路径和包名都不对）。
每次发布自建一个 `build_mrrc_v<ver>.ps1`（内容同本文档 build_vm.ps1 的示例，但 `Set-Location C:\mrrc_modern`），
scp 到 VM 再执行。构建产物在 `C:\mrrc_modern\dist\windows\`。

build.ps1 有 `Invoke-Checked` 闸门：测试或任何一步非零退出都会中止，不会带病出包。

### Step 5 — 验证产物（在 VM 上）

```powershell
dir C:\mrrc_modern\dist\windows\MRRC-Modern-Setup.exe                    # v1.8.0: 36,888,086 bytes
dir C:\mrrc_modern\dist\windows\MRRC-Modern\vendor\ftdi\windows\bin\x64 # 两个 DLL 都在
dir C:\mrrc_modern\dist\windows\MRRC-Modern\_internal\static\index.html # static 在 _internal
dir C:\mrrc_modern\dist\windows\MRRC-Modern\_internal\mem_channels.json # 初始频道种子
Get-FileHash C:\mrrc_modern\dist\windows\MRRC-Modern-Setup.exe -Algorithm SHA256
```

### Step 6 — 取回本机

```bash
ssh ham.vlsc.net "scp cheenle@192.168.122.133:C:/mrrc_modern/dist/windows/MRRC-Modern-Setup.exe /tmp/MRRC-Modern-v1.8.0-Windows-x64-Setup.exe"
scp ham.vlsc.net:/tmp/MRRC-Modern-v1.8.0-Windows-x64-Setup.exe dist/windows/
shasum -a 256 dist/windows/MRRC-Modern-v1.8.0-Windows-x64-Setup.exe
# v1.8.0: 36a48a5f3f325d112937751bddcdebc581039d0484a40c95c1b00fd4bcc170ea
```

## 4. 发布到网站（可选）

下载镜像在 **www.vlsc.net**（另一台机器，非 ham），webroot `/var/www/vlsc.net/mrrc_modern/`。发布脚本会备份整个站点、上传中英文页面和两个安装包、设置权限并在 reload 前执行 `nginx -t`：

```bash
mkdir -p website/downloads
cp dist/windows/MRRC-Modern-v1.8.0-Windows-x64-Setup.exe website/downloads/MRRC-Modern-Setup.exe
cp dist/windows/MRRC-Modern-v1.8.0-Windows-x64-Setup.exe website/downloads/MRRC-Modern-v1.8.0-Windows-x64-Setup.exe
shasum -a 256 website/downloads/*.exe
cd website
./deploy.sh
```

同步更新版本号/大小/SHA-256 的地方：

- `website/index.html`（版本、大小、SHA-256 和两个下载链接）
- `website/zh/index.html`（同上）
- `docs/WINDOWS_INSTALLER_GUIDE.md`（Download 表格 + 构建说明段）
- `README.md`、`CHANGELOG.md`、`SDD/README.md`、`SDD/14-version-history.md`

验证两个 URL 都返回 200、`content-length: 36888086`，下载后的 SHA-256 都等于 `36a48a5f3f325d112937751bddcdebc581039d0484a40c95c1b00fd4bcc170ea`：

```bash
curl -sI https://www.vlsc.net/mrrc_modern/downloads/MRRC-Modern-Setup.exe
curl -sI https://www.vlsc.net/mrrc_modern/downloads/MRRC-Modern-v1.8.0-Windows-x64-Setup.exe
```

## 5. 故障排查（本次踩过的坑）

| 现象 | 原因 | 处理 |
|------|------|------|
| `virsh list` 看不到 win11 | 默认连 qemu:///session | `sudo virsh -c qemu:///system list --all` |
| 公钥加了仍 Permission denied | 管理员用户只认 `C:\ProgramData\ssh\administrators_authorized_keys` | 见 §2.1，注意 icacls 权限 |
| SSH 里 `&&` 报错 | VM 默认 shell 是 PowerShell 5.1 | 用 `;` 或把命令写成 .ps1 scp 上去执行 |
| PS 远程命令引号地狱 | 多层 ssh 转义 | 本地写脚本 → scp → 远程执行；或 `powershell -EncodedCommand <UTF16LE-Base64>` |
| 测试输出文件 grep 不到内容 | PowerShell `2>` 重定向写 UTF-16LE | `iconv -f UTF-16LE -t UTF-8` 后再处理 |
| `python -m py_compile *.py` 报 Invalid argument | PowerShell 不给原生命令展开 glob | build.ps1 已修：`Get-ChildItem -Name *.py` + splat |
| 测试失败但 build 继续（历史问题） | `$ErrorActionPreference` 不管原生命令 | build.ps1 已修：`Invoke-Checked` 检查 `$LASTEXITCODE` |
| VM 上 43 个测试 UnicodeDecodeError | 虚拟机是 GBK(c936) 中文区域，测试 `read_text()` 未指定编码 | 已修：测试统一 `encoding="utf-8"`，harness 子进程设 `PYTHONIOENCODING=utf-8` |
| VM 上 24 个 harness 测试失败 | 源码 zip 误排 `.agents/` | 见 Step 1 关键提示 |
| Step 3 删目录后 build_vm.ps1 报 `.\venv\Scripts\Activate.ps1` 找不到 | Step 3 的 `Remove-Item C:\mrrc_modern` 把 venv 一起删了（zip 不含 venv） | 重跑 §2.2 完整四条（含 `python -m venv venv`）再构建 |
| Step 3 删除时报 `server_console.log` 被占用 | VM 上 `start_mrrc_modern.ps1` 起的 `python server.py --no-ssl` 实例持有该文件 | 先 `Stop-Process` 掉对应 python/父 powershell 再解压；构建完成后按需重新拉起 |
| ham.vlsc.net 突然不通 | DDNS（aliddns.py）A 记录消失/更新延迟 | `dig +short ham.vlsc.net @223.5.5.5` 确认；等服务器恢复后记录自动回来 |
| 装了新包但 8888/COM 口行为异常，COM 口 PermissionError | 计划任务 `MRRC-Modern-Server`（`C:\start_mrrc_modern.ps1`，开发期残留）开机拉起旧 dev 服务，抢占 8888 和串口 | `Disable-ScheduledTask -TaskName "MRRC-Modern-Server"` + 停掉旧 `python server.py` 进程（2026-07-26 已禁用，勿再启用） |
| 安装包 CAT 连不上、串口打开成功但无任何响应 | VM 上 COM3 是 CP2105 **Standard** 口，CAT 在 **Enhanced** 口（COM4；FT-710 示例） | `mrrc_modern.env` 设 `FT710_SERIAL_PORT=COM4`（见 §2.4） |
| SSH 里 `Start-Process` 拉起的应用，SSH 一断进程就没了 | Windows OpenSSH 的 job object 在会话结束时杀整棵进程树 | 用交互式计划任务启动（见 §6），或直接在 VM 桌面启动 |
| **TX 音频在这台 KVM VM 上必然咔咔杂音，无法用于 TX 验证**（2026-07-26 多轮实测定论） | KVM USB 直通对等时（isochronous）**OUT** 调度有硬伤：播放节奏混沌（1 秒尺度慢 1.4×、10 秒尺度驱动 1.3 秒吞掉 10 秒音频），MME/WASAPI 全一样；iso IN（RX 采集）和 bulk（FT4222）正常 | 软件层无法修复。TX 音频验证请到**实体 Windows 机**做；或整机 xHCI 控制器 PCIe 直通。别再在这台 VM 上排查 TX 音质 |
| v1.7.4 PTT 报 `name 'sys' is not defined` | `start_tx` 用了 `sys.platform` 但模块没 import（v1.7.5 已修，补了 `start_tx` 端到端回归测试） | 装 ≥v1.7.5 |
| VM 上串口/声卡突然全消失（COM 只剩 COM1、`'USB Audio' not found`、服务端 UI-only），外网 302 正常但无音频无 CAT | **电台 USB 物理断开**（关机/拔线/被接回 Mac）——宿主机 `lsusb` 里 CP2105/FT4222/C-Media 全没了（2026-07-26 实测） | 先 `ssh ham.vlsc.net lsusb` 确认；插回电台后**直通不会自动恢复**，需重新执行三个 `virsh attach-device --live --config`（或重启 VM），再重启 MRRC-Modern-Server |
| 跑了 build_vm.ps1 却构建出 `MRRC_FT8-Setup.exe`、`C:\mrrc_modern\dist` 为空 | **`C:\Users\cheenle\build_vm.ps1` 已改成 `Set-Location C:\mrrc_ft8`**，指向另一项目（MRRC_FT8）工作区（2026-08-15 实测） | 用按版本自建的 `build_mrrc_v<ver>.ps1`（`Set-Location C:\mrrc_modern`），别用 build_vm.ps1 |
| 源码 zip 达 670MB、上传极慢 | 工作目录含 gitignored 的 `promo/` 宣传视频（约 630MB） | Step 1 zip 加 `./promo/*` 排除（v1.8.0 起已加） |
| 服务突然整个没了（8443 无监听、外网握手超时/000），socat 还在 | 原生 `MRRC-Modern.exe` launcher 是**控制台应用**：控制台窗口被关闭、或交互会话注销，服务随之被杀（2026-07-26 实测） | 常驻服务用隐藏任务路径：计划任务 `MRRC-Modern-HTTPS` → `start_mrrc_modern_https.ps1`（Start-Process 隐藏拉起）；提醒使用者不要随手关控制台窗口 |

## 6. 常用命令速查

```bash
# VM 状态
ssh ham.vlsc.net "sudo virsh -c qemu:///system list --all"

# 一条命令进 VM 跑 PowerShell
ssh ham.vlsc.net "ssh cheenle@192.168.122.133 'powershell -NoProfile -Command \"<cmd>\"'"

# VM 上跑测试
ssh ham.vlsc.net "ssh cheenle@192.168.122.133 'cd C:\mrrc_modern; venv\Scripts\python.exe -m unittest discover -s tests'"

# 构建
ssh ham.vlsc.net "ssh cheenle@192.168.122.133 'powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\cheenle\build_vm.ps1'"

# 远程重启已安装的应用（SSH 里 Start-Process 会随会话被杀，走 VM 上的现成脚本）
ssh ham.vlsc.net "ssh cheenle@192.168.122.133 'powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\cheenle\restart_mrrc_modern.ps1'"

# 电台 USB 重新插回后的直通恢复（在 ham 宿主机上执行；XML 按 VID:PID 匹配，设备重新枚举也不怕）
for dev in '10c4:ea70' '0403:601c' '0d8c:0013'; do
  vid="${dev%%:*}"; pid="${dev##*:}"
  printf '<hostdev mode="subsystem" type="usb" managed="yes"><source><vendor id="0x%s"/><product id="0x%s"/></source></hostdev>' "$vid" "$pid" > /tmp/usb.xml
  sudo virsh -c qemu:///system attach-device win11 /tmp/usb.xml --live --config
done
# 然后重启 VM 上的服务（见上一条 restart_mrrc_modern.ps1）

# 健康检查（HTTPS，8443；radio/audio/scope 三项应全 true）
#   $r = Invoke-RestMethod -Uri https://192.168.122.133:8443/api/auth/login -Method Post -ContentType "application/json" -Body (@{password="<pwd>"} | ConvertTo-Json) -SkipCertificateCheck
#   Invoke-RestMethod -Uri "https://192.168.122.133:8443/api/health?token=$($r.token)" -SkipCertificateCheck
# （VM 上是 PowerShell 5.1，无 -SkipCertificateCheck 参数时用 [System.Net.ServicePointManager]::ServerCertificateValidationCallback = {$true}）
```
