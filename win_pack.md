# Windows 安装包打包流程（ham.vlsc.net KVM 虚拟机）

> 用途：在 ham.vlsc.net 上的 Win11 KVM 虚拟机中构建 `MRRC-FT710-Setup.exe`。
> 本文按 2026-07-25 首次成功打包（v1.6.3）的实际操作整理，照做即可复现。
> 用户向的安装/使用说明见 [docs/WINDOWS_INSTALLER_GUIDE.md](docs/WINDOWS_INSTALLER_GUIDE.md)，本文是**打包方**的操作手册。

## 1. 环境拓扑

```
本机 Mac (mrrc_ft710 工作区)
   │  ssh ham.vlsc.net          （Ubuntu 24.04 KVM 宿主机，sudo 免密）
   ▼
ham.vlsc.net
   │  ssh cheenle@192.168.122.133  （libvirt 默认 NAT 网段）
   ▼
win11 虚拟机 (desktop-ssddf0b)
   C:\mrrc_ft710            源码工作区
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
- 源码区 `C:\mrrc_ft710` 下已建 venv（`venv\Scripts\python.exe`），依赖已装齐

重建依赖（源码更新后只需重跑后两条）：

```powershell
cd C:\mrrc_ft710
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

## 3. 每次打包流程

### Step 0 — 本地检查

```bash
venv/bin/python -m unittest discover -s tests        # 必须全绿（当前 373）
```

确认版本号一致：`CHANGELOG.md` 最新条目、`packaging/windows/MRRC-FT710.iss` 的 `MyAppVersion`。

### Step 1 — 打源码包（在本机仓库根目录）

```bash
mkdir -p dist && rm -f dist/mrrc_ft710_src.zip
zip -qr dist/mrrc_ft710_src.zip . \
  -x "./.git/*" "./venv/*" "./.venv/*" "./dist/*" "./build/*" "./logs/*" "./certs/*" \
     "./FT710Mobile/*" "./website/*" "./lib/*" "./__pycache__/*" "./windows/__pycache__/*" \
     "./tests/__pycache__/*" "./.pytest_cache/*" "./.claude/*" "./.superpowers/*" \
     "./.agnes/*" "./*.pyc" "./.DS_Store" "./SDD/.DS_Store"
```

**关键**：`./.agents/*` 不能排除——`tests/test_sdd_harness.py` 依赖其中的 harness 文件，缺了会导致 VM 上 24 个测试失败。`./certs/*` 必须排除（含 TLS 私钥）。

### Step 2 — 上传到 VM（经 ham 跳板）

```bash
scp dist/mrrc_ft710_src.zip ham.vlsc.net:/tmp/
ssh ham.vlsc.net "scp /tmp/mrrc_ft710_src.zip cheenle@192.168.122.133:mrrc_ft710_src.zip"
```

### Step 3 — VM 上解压

```bash
ssh ham.vlsc.net "ssh cheenle@192.168.122.133 'powershell -NoProfile -Command \"Set-Location C:\Users\cheenle; if (Test-Path C:\mrrc_ft710) { Remove-Item C:\mrrc_ft710 -Recurse -Force }; Expand-Archive mrrc_ft710_src.zip -DestinationPath C:\mrrc_ft710\"'"
```

如 `requirements*.txt` 有变化，重跑 §2.2 的 pip 两条。

**注意**：这一步的删除会把 `C:\mrrc_ft710\venv` 一起删掉（zip 不含 venv），
之后 `build_vm.ps1` 会因找不到 `.\venv\Scripts\Activate.ps1` 直接失败——
删过目录就必须重跑 §2.2 的**完整四条**（含 `python -m venv venv`）。
若删除时报 `server_console.log` 被占用，是 VM 上 `start_ft710.ps1` 拉的
`python server.py --no-ssl` 实例持有该文件，先停掉它再解压（见 §5 表）。

### Step 4 — 构建

VM 上已有辅助脚本 `C:\Users\cheenle\build_vm.ps1`（内容是：Inno 目录入 PATH → 激活 venv → 调 `packaging\windows\build.ps1`）：

```powershell
$ErrorActionPreference = "Stop"
$isccDir = "${env:ProgramFiles(x86)}\Inno Setup 6"
if (Test-Path "$isccDir\iscc.exe") { $env:Path = "$isccDir;" + $env:Path }
Set-Location C:\mrrc_ft710
.\venv\Scripts\Activate.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\windows\build.ps1
Write-Host "BUILD_DONE"
```

远程执行（约 3-5 分钟：271 个测试 → 3 个 PyInstaller spec → iscc）：

```bash
ssh ham.vlsc.net "ssh cheenle@192.168.122.133 'powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\cheenle\build_vm.ps1'"
```

build.ps1 有 `Invoke-Checked` 闸门：测试或任何一步非零退出都会中止，不会带病出包。

### Step 5 — 验证产物（在 VM 上）

```powershell
dir C:\mrrc_ft710\dist\windows\MRRC-FT710-Setup.exe                                # ~30 MB
dir C:\mrrc_ft710\dist\windows\MRRC-FT710\vendor\ftdi\windows\bin\x64              # 两个 DLL 都在
dir C:\mrrc_ft710\dist\windows\MRRC-FT710\_internal\static\index.html              # P0 修复点：static 在 _internal
dir C:\mrrc_ft710\dist\windows\MRRC-FT710\_internal\mem_channels.json              # 初始频道种子
```

### Step 6 — 取回本机

```bash
ssh ham.vlsc.net "scp cheenle@192.168.122.133:C:/mrrc_ft710/dist/windows/MRRC-FT710-Setup.exe /tmp/"
scp ham.vlsc.net:/tmp/MRRC-FT710-Setup.exe dist/
md5 dist/MRRC-FT710-Setup.exe        # 记录，发布网页要用
```

## 4. 发布到网站（可选）

下载镜像在 **www.vlsc.net**（另一台机器，非 ham），webroot `/var/www/vlsc.net/mrrc_ft710/`，文件属 `www-data:www-data`（cheenle 有 sudo 免密）：

```bash
scp dist/MRRC-FT710-Setup.exe www.vlsc.net:/tmp/MRRC-FT710-Setup.exe.new
ssh www.vlsc.net "sudo -n cp /var/www/vlsc.net/mrrc_ft710/downloads/MRRC-FT710-Setup.exe /tmp/old.bak; \
  sudo -n mv /tmp/MRRC-FT710-Setup.exe.new /var/www/vlsc.net/mrrc_ft710/downloads/MRRC-FT710-Setup.exe; \
  sudo -n chown www-data:www-data /var/www/vlsc.net/mrrc_ft710/downloads/MRRC-FT710-Setup.exe; \
  sudo -n chmod 644 /var/www/vlsc.net/mrrc_ft710/downloads/MRRC-FT710-Setup.exe; \
  md5sum /var/www/vlsc.net/mrrc_ft710/downloads/MRRC-FT710-Setup.exe"
```

同步更新版本号/大小/MD5 的地方（共 4 个文件）：

- `website/index.html`（2 处 MD5 + 版本/大小文字）
- `website/zh/index.html`（同上）
- `docs/WINDOWS_INSTALLER_GUIDE.md`（Download 表格 + 构建说明段）
- 改完上传：`website/index.html` → webroot 根、`website/zh/index.html` → webroot `zh/`，同样 sudo + chown www-data

验证：`curl -sI https://www.vlsc.net/mrrc_ft710/downloads/MRRC-FT710-Setup.exe`（200 + content-length 正确）。

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
| Step 3 删目录后 build_vm.ps1 报 `.\venv\Scripts\Activate.ps1` 找不到 | Step 3 的 `Remove-Item C:\mrrc_ft710` 把 venv 一起删了（zip 不含 venv） | 重跑 §2.2 完整四条（含 `python -m venv venv`）再构建 |
| Step 3 删除时报 `server_console.log` 被占用 | VM 上 `start_ft710.ps1` 起的 `python server.py --no-ssl` 实例持有该文件 | 先 `Stop-Process` 掉对应 python/父 powershell 再解压；构建完成后按需重新拉起 |
| ham.vlsc.net 突然不通 | DDNS（aliddns.py）A 记录消失/更新延迟 | `dig +short ham.vlsc.net @223.5.5.5` 确认；等服务器恢复后记录自动回来 |

## 6. 常用命令速查

```bash
# VM 状态
ssh ham.vlsc.net "sudo virsh -c qemu:///system list --all"

# 一条命令进 VM 跑 PowerShell
ssh ham.vlsc.net "ssh cheenle@192.168.122.133 'powershell -NoProfile -Command \"<cmd>\"'"

# VM 上跑测试
ssh ham.vlsc.net "ssh cheenle@192.168.122.133 'cd C:\mrrc_ft710; venv\Scripts\python.exe -m unittest discover -s tests'"

# 构建
ssh ham.vlsc.net "ssh cheenle@192.168.122.133 'powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\cheenle\build_vm.ps1'"
```
