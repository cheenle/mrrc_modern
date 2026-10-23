# Windows 换电脑一键备份/恢复 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 一个独立发布的 Windows 脚本，把用户数据目录里的配置、证书、记忆频道、天调学习值、云隧道配置与会话库打成一个文件，在新电脑上一次恢复。

**架构：** 单个 `MRRC-Modern-Backup.ps1`（纯 PowerShell 5.1 + .NET，无外部依赖）加一个 ASCII 的 `.cmd` 自下载壳。导出把固定清单复制进 `payload/`、逐文件写 SHA-256 到 `manifest.json`、打 zip；`-Encrypt` 再套一层 AES-256-CBC + PBKDF2-SHA256 + HMAC 容器。恢复先校验条目名（zip-slip）与摘要、再把目标机现有文件备份到桌面、要求应用已关闭、逐项替换，最后改写 `mrrc_modern.env` 里指向旧数据目录的绝对路径。

**技术栈：** PowerShell 5.1（Windows 10/11 自带）、`System.IO.Compression`、`System.Security.Cryptography`、Python `unittest`（静态守卫与发布副本一致性）。

**规格：** `docs/superpowers/specs/2026-10-22-windows-backup-restore-design.md`

---

## 文件结构

| 文件 | 职责 |
| --- | --- |
| `tools/windows/MRRC-Modern-Backup.ps1`（创建） | 全部逻辑：清单、导出、恢复、加密、菜单、`-SelfTest` |
| `tools/windows/MRRC-Modern-Backup.cmd`（创建） | 双击入口 + 缺 `.ps1` 时自下载；参数原样转交 |
| `website/downloads/MRRC-Modern-Backup.ps1` / `.cmd`（创建） | 逐字节副本（发布物） |
| `tests/ps_script_guards.py`（创建） | 两个测试模块共用的三条静态守卫（BOM / ASCII .cmd / 块内括号） |
| `tests/test_windows_backup_script.py`（创建） | 源文件守卫、发布副本一致性、固定清单一致性、加密分支源码断言 |
| `tests/test_cert_doctor.py`（修改） | 改用共享守卫（删掉它自己的两份重复实现） |
| `website/guide.html` / `website/zh/guide.html`（修改） | 新增 §6.5「换电脑：一键备份/恢复」 |
| `website/index.html` / `website/zh/index.html`（修改） | 页脚在「清理脚本」旁加一行链接 |
| `docs/OPERATION_GUIDE.md`（修改） | 升级一节补一句交叉引用 |
| `tests/README.md`（修改） | 模块清单与总数 |

**规格的两处实现期修正**（先改规格再写代码，见任务 1）：

- CLI 增加 `-Restore`（交互式选文件）——`-Import` 是 `[string]`，PowerShell 不允许它不带值。
- CLI 增加 `-Encrypt`（安全地提示口令）并把 `-Password` 标成「非交互用，会留在 shell 历史里」。

---

## 任务 1：规格对齐与共享守卫（先红）

**文件：**

- 修改：`docs/superpowers/specs/2026-10-22-windows-backup-restore-design.md` §5、§10
- 创建：`tests/ps_script_guards.py`
- 修改：`tests/test_cert_doctor.py:66-95`
- 创建：`tests/test_windows_backup_script.py`

- [ ] **步骤 1：把两处 CLI 修正写回规格**

`docs/superpowers/specs/2026-10-22-windows-backup-restore-design.md`，把 §5 的三行用法改成：

```
MRRC-Modern-Backup.ps1 -Export [-Out <path>] [-Encrypt] [-Password <pwd>]
MRRC-Modern-Backup.ps1 -Import <file> [-Password <pwd>] [-Yes] [-StopApp]
MRRC-Modern-Backup.ps1 -Restore [-Password <pwd>] [-Yes] [-StopApp]
MRRC-Modern-Backup.ps1 -List <file> [-Password <pwd>]
MRRC-Modern-Backup.ps1 -SelfTest
```

参数表在 `-Password` 行后插入两行：

```
| `-Encrypt` | 导出后加密；口令用 `Read-Host -AsSecureString` 提示两遍，**不经过命令行** |
| `-Restore` | 弹出文件选择框，选中后按 `-Import` 处理 |
```

并把 `-Password` 的说明改为「非交互入口（给支持人员用）：**会留在 PowerShell 历史与进程命令行里**，本地一次性迁移优先用 `-Encrypt`」。

§10 的「新增 §6.3」改为「新增 **§6.5**」（§6.3/§6.4 已被占用，改号会打断既有锚点）。

- [ ] **步骤 2：写共享守卫（会失败，因为还没有备份脚本）**

创建 `tests/ps_script_guards.py`：

```python
"""下载类 PowerShell 工具的公共静态守卫。

三条都在现场踩过：
* **UTF-8 无 BOM** —— Windows PowerShell 5.1 按 ANSI 代码页解码无 BOM 的 UTF-8，
  中文变乱码，字符串字面量甚至会解析失败（2026-10-16，CertDoctor 现场）。
* **`.cmd` 里有非 ASCII** —— cmd.exe 用同一套代码页，中文提示会变问号，
  而 `if (...)` 块里的乱码能让整段脚本不执行。
* **`( ... )` 块内的 `echo` 带括号** —— cmd 在第一个 `)` 结束这个块，
  报 `… was unexpected at this time`，工具根本没起来。

放在共享模块里，是因为第二份拷贝晚于第一份的修复，而复制来的 bug 不会自己变旧。
"""
from __future__ import annotations

from pathlib import Path


def assert_utf8_bom(test, path, what="PowerShell 5.1") -> None:
    raw = Path(path).read_bytes()
    test.assertTrue(
        raw.startswith(b"\xef\xbb\xbf"),
        f"{what} 会把无 BOM 的 UTF-8 当 ANSI 解码：{path} 必须以 UTF-8 BOM 开头",
    )
    test.assertTrue(raw[3:].decode("utf-8").strip(), f"{path} 不能是空文件")


def assert_ascii_cmd(test, path, must_contain=()) -> None:
    raw = Path(path).read_bytes()
    raw.decode("ascii")  # 非 ASCII 直接抛 UnicodeDecodeError
    for needle in must_contain:
        test.assertIn(needle.encode("ascii"), raw, f"{path} 里少了 {needle!r}")


def assert_no_parens_in_blocks(test, path) -> None:
    """块内 echo/rem 不得含括号：cmd 在第一个 `)` 结束块。"""
    text = Path(path).read_text(encoding="ascii")
    depth = 0
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.lower().startswith(("echo", "rem")):
            if depth > 0 and ("(" in line or ")" in line):
                test.fail(
                    f"{Path(path).name}:{lineno}: 括号出现在 ( ... ) 块内的 echo/rem 里，"
                    f"cmd.exe 会提前结束这个块：{line!r}"
                )
            continue
        depth += line.count("(") - line.count(")")
        test.assertGreaterEqual(depth, 0, f"{Path(path).name}:{lineno}: 右括号多于左括号")
```

- [ ] **步骤 3：让 `test_cert_doctor.py` 改用共享守卫**

在 `tests/test_cert_doctor.py` 的 import 区（`import zipfile` 后）加：

```python
from ps_script_guards import assert_ascii_cmd, assert_no_parens_in_blocks, assert_utf8_bom
```

把它 `SourceFilesTests` 里的三个方法替换成：

```python
    def test_script_has_a_utf8_bom(self):
        assert_utf8_bom(self, SOURCE_DIR / SCRIPT)

    def test_launcher_is_ascii_only(self):
        assert_ascii_cmd(self, SOURCE_DIR / CMD, must_contain=("-Fix",))

    def test_launcher_does_not_put_parentheses_inside_blocks(self):
        assert_no_parens_in_blocks(self, SOURCE_DIR / CMD)
```

- [ ] **步骤 4：写会失败的新测试模块**

创建 `tests/test_windows_backup_script.py`：

```python
"""换电脑备份/恢复脚本的静态守卫。

本机（macOS）没有 PowerShell，所以这里只钉**源码事实**：PowerShell 5.1 读不了的东西、
cmd.exe 会崩的写法、发布副本与源不一致、以及那条最容易在维护时被改坏的固定清单。
真正的行为由脚本自己的 `-SelfTest` 在 Windows 上验（构建验收清单里有一条）。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

# 让 `python -m unittest tests.test_windows_backup_script` 与
# `python -m unittest discover -s tests` 两种跑法都能找到同目录的守卫模块。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ps_script_guards import assert_ascii_cmd, assert_no_parens_in_blocks, assert_utf8_bom

ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = ROOT / "tools" / "windows"
DOWNLOADS = ROOT / "website" / "downloads"

SCRIPT = "MRRC-Modern-Backup.ps1"
CMD = "MRRC-Modern-Backup.cmd"

#: 备份集。改脚本里的 `$Script:FixedItems` 必须同时改这里 —— 两边不一致就是
#: 「备份包悄悄少了一项」，而恢复端不会报错。
FIXED_ITEMS = (
    "mrrc_modern.env",
    "certs",
    "mem_channels.json",
    "atr1000_tuner.json",
    "fleet",
    "auth-tokens.json",
)


class SourceFilesTests(unittest.TestCase):
    def test_script_has_a_utf8_bom(self):
        assert_utf8_bom(self, SOURCE_DIR / SCRIPT)

    def test_launcher_is_ascii_only(self):
        assert_ascii_cmd(self, SOURCE_DIR / CMD, must_contain=("-Export", "-Import"))

    def test_launcher_does_not_put_parentheses_inside_blocks(self):
        assert_no_parens_in_blocks(self, SOURCE_DIR / CMD)

    def test_fixed_item_list_matches_the_tests(self):
        text = (SOURCE_DIR / SCRIPT).read_text(encoding="utf-8-sig")
        found = tuple(re.findall(r'Path\s*=\s*"([^"]+)"', text))
        self.assertEqual(found, FIXED_ITEMS,
                         "脚本的固定清单与测试里的 FIXED_ITEMS 不一致："
                         "加/删一项必须两边同时改")

    def test_encryption_branch_really_deletes_the_plain_zip(self):
        text = (SOURCE_DIR / SCRIPT).read_text(encoding="utf-8-sig")
        protect = text.find("Protect-BackupFile")
        delete = text.find("Remove-Item -LiteralPath $zipPath -Force")
        self.assertGreater(protect, 0, "找不到加密函数 Protect-BackupFile")
        self.assertGreater(delete, protect,
                           "加密之后必须删掉明文 zip（顺序：先加密、再删除）")
        for word in ("Aes", "Rfc2898DeriveBytes", "HMACSHA256"):
            self.assertIn(word, text, f"加密实现里没有 {word}")


class PublishedCopyTests(unittest.TestCase):
    def test_downloads_carry_the_current_sources(self):
        for name in (SCRIPT, CMD):
            with self.subTest(name=name):
                self.assertEqual(
                    (SOURCE_DIR / name).read_bytes(), (DOWNLOADS / name).read_bytes(),
                    f"{DOWNLOADS / name} 与源不一致：下载页是用旧副本发布的",
                )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **步骤 5：运行，确认红**

```bash
python3 -m unittest tests.test_windows_backup_script -v
```

预期：`ERROR`（`tools/windows/MRRC-Modern-Backup.ps1` 不存在，`read_bytes` 抛 `FileNotFoundError`），
且 `test_cert_doctor` 仍然全绿：

```bash
python3 -m unittest tests.test_cert_doctor -v
```

预期：`OK`（守卫重构没有改变判定）。

- [ ] **步骤 6：Commit**

```bash
git add docs/superpowers/specs/2026-10-22-windows-backup-restore-design.md \
        tests/ps_script_guards.py tests/test_cert_doctor.py tests/test_windows_backup_script.py
git commit -m "test(backup): 换电脑备份脚本的静态守卫 + 共享 PS 脚本守卫"
```

---

## 任务 2：脚本骨架、参数与常量

**文件：**

- 创建：`tools/windows/MRRC-Modern-Backup.ps1`
- 创建：`tools/windows/MRRC-Modern-Backup.cmd`

- [ ] **步骤 1：写 `.cmd`（纯 ASCII，括号只在块外）**

```bat
@echo off
rem MRRC Modern - move configuration and certificates to a new computer.
rem Double-click for a menu: 1) back up this machine  2) restore from a backup.
rem Command line: -Export [-Out FILE] [-Encrypt]  ^|  -Import FILE  ^|  -Restore  ^|  -List FILE
rem Requires no administrator rights.
setlocal
set PS1=%~dp0MRRC-Modern-Backup.ps1
if not exist "%PS1%" (
    echo.
    echo   Downloading the tool: MRRC-Modern-Backup.ps1 ...
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "Invoke-WebRequest -UseBasicParsing -Uri 'https://www.vlsc.net/mrrc_modern/downloads/MRRC-Modern-Backup.ps1' -OutFile '%PS1%'"
)
if not exist "%PS1%" (
    echo.
    echo   Could not obtain MRRC-Modern-Backup.ps1.
    echo   Download both files from:
    echo       https://www.vlsc.net/mrrc_modern/downloads/MRRC-Modern-Backup.cmd
    echo.
    pause
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
set RC=%ERRORLEVEL%
echo.
echo   exit code %RC%
pause
exit /b %RC%
```

`.cmd` 必须是 ASCII + CRLF（cmd.exe 按本地代码页读它）：

```bash
python3 - <<'EOF'
from pathlib import Path
p = Path("tools/windows/MRRC-Modern-Backup.cmd")
s = p.read_bytes().decode("ascii").replace("\r\n", "\n").replace("\n", "\r\n")
p.write_bytes(s.encode("ascii"))
print("crlf ok")
EOF
```

- [ ] **步骤 2：写 `.ps1` 的头部、参数与常量**

创建 `tools/windows/MRRC-Modern-Backup.ps1`（**保存为 UTF-8 with BOM**）。

写完立即补 BOM（本机的编辑器/写入工具默认不写 BOM，而 PowerShell 5.1 会把无 BOM 的
UTF-8 当 ANSI，中文字符串会变乱码）：

```bash
python3 - <<'EOF'
from pathlib import Path
p = Path("tools/windows/MRRC-Modern-Backup.ps1")
raw = p.read_bytes()
if not raw.startswith(b"\xef\xbb\xbf"):
    p.write_bytes(b"\xef\xbb\xbf" + raw)
print("bom ok")
EOF
```

```powershell
<#
    MRRC Modern - 换电脑：配置与证书的一键备份 / 恢复

    为什么有这个东西
    ----------------
    用户换电脑时，MRRC Modern 的用户侧状态**不在安装目录里**，而在

        %LOCALAPPDATA%\MRRC-Modern\

    覆盖面安装不受影响，但换机没有任何一条路径能把它带走。最贵的三件事是：
      * mrrc_modern.env：电台机型、串口、波特率、声卡名、网页口令、云端参数；
      * certs\：HTTPS 证书与私钥 —— **云入口钉住的是这张证书**，换机重新签一张，
        那条入口就 502（ssl_bootstrap.sign_for() 的幂等性注释记着 2026-10-15 现场）；
      * fleet\frpc-<label>.toml：云入口隧道配置。

    它做什么
    --------
      1. 导出：把固定清单（见下）复制进 payload/，逐文件算 SHA-256 写进 manifest.json，
         打成一个 zip；-Encrypt 时再套一层 AES-256 口令容器。
      2. 恢复：校验 schema / 条目名（zip-slip）/ 每个文件的 SHA-256，把目标机现有配置
         先备份到桌面，要求应用已关闭，逐项替换，最后改写 env 里指向旧数据目录的绝对路径。
      3. -SelfTest：两个假数据目录上跑一遍「导出 → 恢复 → 逐字节比对」，支持人员可自证。

    安全边界（与 MRRC-Modern-Cleanup.ps1 的破坏面完全不同）
    ------------------------------------------------------
      * 只写固定清单里的路径；**永不删除用户数据**（只覆盖，且覆盖前已备份）；
      * 解包先校验后落盘，拒绝 `..` / 绝对路径 / 盘符条目；
      * 口令、令牌、私钥的值**永不打印**，只打印存在性与 SHA-256 前 8 位；
      * 两次备份（导出包、MRRC-restore-pre-*）都不自动删除；
      * 不读写注册表、不改环境变量、不需要管理员权限。

    用法
    ----
        MRRC-Modern-Backup.cmd                     # 菜单：1 备份 / 2 恢复 / Q 退出
        .\MRRC-Modern-Backup.ps1 -Export [-Out <zip>] [-Encrypt] [-Password <pwd>]
        .\MRRC-Modern-Backup.ps1 -Import <file> [-Password <pwd>] [-Yes] [-StopApp]
        .\MRRC-Modern-Backup.ps1 -Restore [-Yes] [-StopApp]
        .\MRRC-Modern-Backup.ps1 -List <file> [-Password <pwd>]
        .\MRRC-Modern-Backup.ps1 -SelfTest

    退出码：0 成功 / 10 无事可做 / 20 校验失败或用法错误 / 30 目标机在跑且拒绝关闭。
#>
[CmdletBinding()]
param(
    [switch]$Export,
    [string]$Out = "",
    [switch]$Encrypt,
    [string]$Password = "",
    [string]$Import = "",
    [switch]$Restore,
    [string]$List = "",
    [switch]$Yes,
    [switch]$StopApp,
    [switch]$SelfTest,
    # 仅测试用：覆盖用户数据目录 / 临时与预备份的落点。正式使用不接受它们。
    [string]$DataDir = "",
    [string]$WorkDir = ""
)

$ErrorActionPreference = "Stop"
# 中文要能落进控制台：PowerShell 5.1 的默认输出编码不是 UTF-8。
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }
Add-Type -AssemblyName System.IO.Compression.FileSystem | Out-Null

$Script:ExitOk = 0
$Script:ExitNothing = 10
$Script:ExitInvalid = 20
$Script:ExitAppRunning = 30

$Script:Schema = "mrrc-modern-backup/1"
$Script:EncMagic = "MRRCBAK1"
$Script:UploadUrl = "https://www.vlsc.net/mrrc_modern/downloads"

# 固定备份集。改这里必须同时改 tests/test_windows_backup_script.py 的 FIXED_ITEMS。
$Script:FixedItems = @(
    @{ Path = "mrrc_modern.env";    Kind = "config";  Required = $true  },
    @{ Path = "certs";              Kind = "key";     Required = $false },
    @{ Path = "mem_channels.json";  Kind = "data";    Required = $false },
    @{ Path = "atr1000_tuner.json"; Kind = "data";    Required = $false },
    @{ Path = "fleet";              Kind = "key";     Required = $false },
    @{ Path = "auth-tokens.json";   Kind = "session"; Required = $false }
)

# 这些键的值是绝对路径：恢复时按「旧数据目录前缀 → 新前缀」改写。
$Script:PathKeys = @(
    "MRRC_SSL_CERT", "MRRC_SSL_KEY", "MRRC_RECORDINGS_DIR", "MRRC_LOG_DIR",
    "MRRC_MEM_FILE", "MRRC_ATR1000_STORE", "MRRC_CQ_FILE", "MRRC_CA_BUNDLE"
)

$Script:ProcessNames = @("MRRC-Modern-Launcher", "MRRC-Modern-Server", "scope_pipe", "frpc")
$Script:Excluded = @("logs", "recordings")
```

- [ ] **步骤 3：加工具函数**

在文件末尾追加：

```powershell
# ---------------------------------------------------------------- output helpers
function Say($text)  { Write-Host $text }
function Step($text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }
function Ok($text)   { Write-Host "   $text" -ForegroundColor Green }
function Warn($text) { Write-Host "   $text" -ForegroundColor Yellow }
function Bad($text)  { Write-Host "   $text" -ForegroundColor Red }

# ---------------------------------------------------------------- paths & hashes
function Get-FullPath([string]$path) { [System.IO.Path]::GetFullPath($path) }

function Get-UserDataDir {
    if ($DataDir) { return (Get-FullPath $DataDir) }
    if ($env:LOCALAPPDATA) { return (Join-Path $env:LOCALAPPDATA "MRRC-Modern") }
    return (Join-Path $HOME ".mrrc-modern")
}

function Get-DesktopDir { [Environment]::GetFolderPath("Desktop") }

function Get-TempRoot {
    if ($WorkDir) {
        New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
        return (Get-FullPath $WorkDir)
    }
    $dir = Join-Path ([System.IO.Path]::GetTempPath()) ("MRRC-backup-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    return (Get-FullPath $dir)
}

function Get-Sha256([string]$path) {
    (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Format-Size([long]$bytes) {
    if ($bytes -ge 1MB) { return ("{0:N1} MB" -f ($bytes / 1MB)) }
    if ($bytes -ge 1KB) { return ("{0:N1} KB" -f ($bytes / 1KB)) }
    return "$bytes B"
}

function Test-UnderRoot([string]$path, [string]$root) {
    $rootFull = (Get-FullPath $root).TrimEnd("\") + "\"
    (Get-FullPath $path).StartsWith($rootFull, [System.StringComparison]::OrdinalIgnoreCase)
}

function Get-AppDir {
    $roots = @(
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*",
        "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*"
    )
    foreach ($r in $roots) {
        $hit = Get-ItemProperty $r -ErrorAction SilentlyContinue |
            Where-Object { $_.DisplayName -like "*MRRC Modern*" -and $_.InstallLocation } |
            Select-Object -First 1
        if ($hit) { return $hit.InstallLocation }
    }
    if ($env:ProgramFiles) { return (Join-Path $env:ProgramFiles "MRRC Modern") }
    return ""
}

function Get-AppVersion {
    $dir = Get-AppDir
    if ($dir) {
        $file = Join-Path $dir "version.txt"
        if (Test-Path -LiteralPath $file) {
            return ((Get-Content -LiteralPath $file -Raw) -replace "`r?`n", "").Trim()
        }
    }
    return "unknown"
}

function Copy-WithRetry([string]$Source, [string]$Destination, [int]$Tries = 3) {
    # 目录只在目的地「不存在」时创建；已存在就**合并**（只覆盖同名文件，不删多余项）。
    # 恢复端因此永远删不掉目标机上清单外的东西 —— 这条与规格 §8 第 1 条对应。
    $last = $null
    for ($i = 1; $i -le $Tries; $i++) {
        try {
            if (Test-Path -LiteralPath $Source -PathType Container) {
                New-Item -ItemType Directory -Force -Path $Destination | Out-Null
                Copy-Item -Path (Join-Path $Source "*") -Destination $Destination -Recurse -Force -ErrorAction Stop
            } else {
                Copy-Item -LiteralPath $Source -Destination $Destination -Force -ErrorAction Stop
            }
            return
        } catch {
            $last = $_
            Start-Sleep -Milliseconds 200
        }
    }
    throw "复制失败（文件被占用？）：$Source -> $($last.Exception.Message)"
}
```

- [ ] **步骤 4：运行静态守卫**

```bash
python3 -m unittest tests.test_windows_backup_script.SourceFilesTests.test_script_has_a_utf8_bom \
    tests.test_windows_backup_script.SourceFilesTests.test_launcher_is_ascii_only \
    tests.test_windows_backup_script.SourceFilesTests.test_launcher_does_not_put_parentheses_inside_blocks -v
```

预期：3 个 `ok`（`test_fixed_item_list_matches_the_tests` 也应为 `ok`；`test_encryption_branch…` 仍是 `FAIL`，任务 5 才补）。

- [ ] **步骤 5：Commit**

```bash
git add tools/windows/MRRC-Modern-Backup.ps1 tools/windows/MRRC-Modern-Backup.cmd
git commit -m "feat(backup): 脚本骨架——参数、固定清单、路径与哈希工具"
```

---

## 任务 3：env 解析、清单与 `-List`

**文件：**

- 修改：`tools/windows/MRRC-Modern-Backup.ps1`（追加函数）
- 测试：`tests/test_windows_backup_script.py`（已存在，本任务不改）

- [ ] **步骤 1：追加 env 与清单函数**

```powershell
# ---------------------------------------------------------------- env file
function Read-EnvLines([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return @() }
    $text = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
    return ,($text -split "`r?`n")
}

function Get-EnvValue($lines, [string]$name) {
    $alt = ""
    if ($name.StartsWith("MRRC_")) { $alt = "FT710_" + $name.Substring(5) }
    $found = $null
    foreach ($raw in $lines) {
        $line = $raw.Trim()
        if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) { continue }
        $parts = $line.Split("=", 2)
        $key = $parts[0].Trim()
        $value = $parts[1].Trim()
        if ($key -eq $name) { $found = $value }
        elseif ($alt -and $key -eq $alt -and -not $found) { $found = $value }
    }
    return $found
}

# 把路径类键里的旧数据目录前缀换成新的；其余绝对路径不存在时只告警。
function Update-EnvPaths($lines, [string]$oldRoot, [string]$newRoot) {
    $out = New-Object System.Collections.Generic.List[string]
    $changed = New-Object System.Collections.Generic.List[string]
    $warnings = New-Object System.Collections.Generic.List[string]
    $pattern = '^(\s*)([A-Za-z0-9_]+)(\s*=\s*)(.*)$'
    foreach ($raw in $lines) {
        if ($raw.Trim().StartsWith("#") -or -not $raw.Contains("=")) { $out.Add($raw); continue }
        $m = [regex]::Match($raw, $pattern)
        if (-not $m.Success) { $out.Add($raw); continue }
        $key = $m.Groups[2].Value
        $value = $m.Groups[4].Value
        $bare = $key
        if ($bare.StartsWith("FT710_")) { $bare = "MRRC_" + $bare.Substring(6) }
        if ($Script:PathKeys -notcontains $bare) { $out.Add($raw); continue }

        $newValue = $value
        if ($value -and $oldRoot -and
            $value.StartsWith($oldRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            $newValue = $newRoot + $value.Substring($oldRoot.Length)
        }
        if ($newValue -ne $value) {
            $changed.Add("$bare : $value -> $newValue")
        } elseif ($value -and [System.IO.Path]::IsPathRooted($value) -and
                  -not (Test-Path -LiteralPath $value)) {
            $warnings.Add("$bare 指向的路径在这台机器上不存在，未改写：$value")
        }
        $out.Add($m.Groups[1].Value + $key + $m.Groups[3].Value + $newValue)
    }
    return @{ Lines = $out.ToArray(); Changed = $changed.ToArray(); Warnings = $warnings.ToArray() }
}

function Write-EnvLines([string]$path, $lines) {
    # 不带 BOM：启动器的解析器按行 Key=Value 读取，BOM 会贴到第一个键上。
    $text = ($lines -join "`r`n")
    if (-not $text.EndsWith("`r`n")) { $text += "`r`n" }
    [System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($false)))
}

# ---------------------------------------------------------------- manifest
function Get-RestoreReadme {
    @"
MRRC Modern 换电脑备份

这个文件里有：配置、HTTPS 证书与私钥、云入口隧道配置、登录会话库。
**请像对待钥匙一样对待它**：不要放公共网盘、不要发到聊天群。

怎么恢复
--------
1. 在新电脑上装好 MRRC Modern（先不要打开）；
2. 把 MRRC-Modern-Backup.cmd 与 MRRC-Modern-Backup.ps1 放到同一个文件夹，双击 .cmd；
3. 选 2（从备份恢复），选中这个包；脚本会先把新电脑现有的配置备份到桌面，再写入；
4. 恢复完成后双击桌面上的 MRRC Modern 启动。

不含：logs\（日志）与 recordings\（录音）。录音要留请自己拷走。
"@
}

function New-Manifest([string]$payload, [hashtable]$meta) {
    $items = New-Object System.Collections.Generic.List[object]
    foreach ($f in Get-ChildItem -LiteralPath $payload -Recurse -File -Force) {
        $rel = $f.FullName.Substring($payload.Length).TrimStart("\").Replace("\", "/")
        $kind = "data"
        foreach ($item in $Script:FixedItems) {
            if ($rel -eq $item.Path -or $rel.StartsWith($item.Path + "/")) { $kind = $item.Kind; break }
        }
        if ($rel.StartsWith("external-certs/")) { $kind = "key" }
        $items.Add([ordered]@{
            path   = $rel
            kind   = $kind
            size   = $f.Length
            sha256 = (Get-Sha256 $f.FullName)
        })
    }
    return [ordered]@{
        schema          = $Script:Schema
        createdAt       = (Get-Date).ToString("o")
        hostname        = $env:COMPUTERNAME
        userName        = $env:USERNAME
        appVersion      = $meta.AppVersion
        appDir          = $meta.AppDir
        dataDir         = $meta.DataDir
        containsSecrets = $true
        items           = @($items | Sort-Object { $_.path })
        excluded        = $Script:Excluded
        notes           = @($meta.Notes)
    }
}

function Write-Manifest([string]$stage, $manifest) {
    $json = $manifest | ConvertTo-Json -Depth 6
    [System.IO.File]::WriteAllText(
        (Join-Path $stage "manifest.json"), $json,
        (New-Object System.Text.UTF8Encoding($false)))
    [System.IO.File]::WriteAllText(
        (Join-Path $stage "RESTORE-README.txt"), (Get-RestoreReadme),
        (New-Object System.Text.UTF8Encoding($false)))
}

function Show-Manifest($manifest) {
    Say ""
    Say ("  来源主机 : {0}    应用版本 : {1}" -f $manifest.hostname, $manifest.appVersion)
    Say ("  生成时间 : {0}" -f $manifest.createdAt)
    Say ("  旧数据目录: {0}" -f $manifest.dataDir)
    Say ""
    Say ("  {0,-34} {1,-8} {2,10}  {3}" -f "文件", "类别", "大小", "SHA-256")
    foreach ($it in $manifest.items) {
        $sha = if ($it.sha256) { $it.sha256.Substring(0, 8) } else { "-" }
        Say ("  {0,-34} {1,-8} {2,10}  {3}" -f $it.path, $it.kind, (Format-Size $it.size), $sha)
    }
    if ($manifest.notes -and $manifest.notes.Count -gt 0) {
        Say ""
        foreach ($n in $manifest.notes) { Warn $n }
    }
}
```

- [ ] **步骤 2：语法自检（本机没有 PowerShell 时的替代）**

```bash
python3 - <<'EOF'
import io
raw = open('tools/windows/MRRC-Modern-Backup.ps1','rb').read()
assert raw.startswith(b'\xef\xbb\xbf'), 'BOM missing'
text = raw[3:].decode('utf-8')
for fn in ('Read-EnvLines','Get-EnvValue','Update-EnvPaths','Write-EnvLines',
           'New-Manifest','Write-Manifest','Show-Manifest'):
    assert f'function {fn}' in text, fn
print('ok', len(text.splitlines()), 'lines')
EOF
```

预期：`ok <N> lines`。

- [ ] **步骤 3：Commit**

```bash
git add tools/windows/MRRC-Modern-Backup.ps1
git commit -m "feat(backup): env 读写、路径改写与 manifest"
```

---

## 任务 4：zip 打包/解包与 zip-slip 校验

**文件：**

- 修改：`tools/windows/MRRC-Modern-Backup.ps1`

- [ ] **步骤 1：追加 zip 函数**

```powershell
# ---------------------------------------------------------------- zip
function New-BackupZip([string]$root, [string]$zipPath) {
    if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
    $zip = [System.IO.Compression.ZipFile]::Open($zipPath, [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($f in Get-ChildItem -LiteralPath $root -Recurse -File -Force) {
            $rel = $f.FullName.Substring($root.Length).TrimStart("\").Replace("\", "/")
            [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                $zip, $f.FullName, $rel, [System.IO.Compression.CompressionLevel]::Optimal)
        }
    } finally {
        $zip.Dispose()
    }
}

# 逐条校验后落盘：拒绝绝对路径、盘符与 `..`（zip-slip）。
function Expand-BackupZip([string]$zipPath, [string]$root) {
    $rootFull = (Get-FullPath $root).TrimEnd("\") + "\"
    New-Item -ItemType Directory -Force -Path $root | Out-Null
    $zip = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
    try {
        foreach ($entry in $zip.Entries) {
            $name = $entry.FullName
            if ([string]::IsNullOrWhiteSpace($name)) { continue }
            if ($name.EndsWith("/") -or $name.EndsWith("\")) { continue }
            if ($name -match '^[A-Za-z]:' -or $name.StartsWith("/") -or $name.StartsWith("\") -or
                ($name -split '[\\/]') -contains "..") {
                throw "备份包含非法条目（拒绝解包）：$name"
            }
            $dest = Get-FullPath (Join-Path $root ($name -replace "/", "\"))
            if (-not $dest.StartsWith($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "条目越界（拒绝解包）：$name"
            }
            $dir = Split-Path $dest -Parent
            if ($dir -and -not (Test-Path -LiteralPath $dir)) {
                New-Item -ItemType Directory -Force -Path $dir | Out-Null
            }
            [void][System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $dest, $true)
        }
    } finally {
        $zip.Dispose()
    }
}

function Test-Manifest([string]$stage, $manifest) {
    if ($manifest.schema -ne $Script:Schema) {
        throw "备份格式不认识：$($manifest.schema)（本工具支持 $($Script:Schema)）"
    }
    $payload = Join-Path $stage "payload"
    if (-not (Test-Path -LiteralPath $payload)) { throw "包里没有 payload/ —— 不是本工具的备份" }
    foreach ($item in $manifest.items) {
        $file = Join-Path $payload ($item.path -replace "/", "\")
        if (-not (Test-Path -LiteralPath $file)) { throw "清单里的文件缺失：$($item.path)" }
        $actual = Get-Sha256 $file
        if ($actual -ne $item.sha256) {
            throw "校验失败：$($item.path)（清单 $($item.sha256.Substring(0, 8))…，实际 $($actual.Substring(0, 8))…）"
        }
    }
}
```

- [ ] **步骤 2：Commit**

```bash
git add tools/windows/MRRC-Modern-Backup.ps1
git commit -m "feat(backup): zip 打包与带 zip-slip 校验的解包"
```

---

## 任务 5：口令容器（AES-256 + PBKDF2 + HMAC）

**文件：**

- 修改：`tools/windows/MRRC-Modern-Backup.ps1`

- [ ] **步骤 1：追加加密函数**

```powershell
# ---------------------------------------------------------------- encryption
function Get-PasswordValue([string]$given, [bool]$Confirm) {
    if ($given) { return $given }
    $bstr1 = [IntPtr]::Zero
    $bstr2 = [IntPtr]::Zero
    try {
        $secure = Read-Host "  请输入备份口令（不回显）" -AsSecureString
        $bstr1 = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        $first = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr1)
        if ($Confirm) {
            $again = Read-Host "  再输一次" -AsSecureString
            $bstr2 = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($again)
            if ($first -ne [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr2)) {
                throw "两次输入的口令不一致"
            }
        }
        if (-not $first) { throw "口令不能为空" }
        return $first
    } finally {
        if ($bstr1 -ne [IntPtr]::Zero) { [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr1) }
        if ($bstr2 -ne [IntPtr]::Zero) { [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr2) }
    }
}

function Get-Kdf([string]$password, [byte[]]$salt, [int]$iterations, [int]$length) {
    # PBKDF2-SHA256 需要 .NET 4.7.2+（Win10 19045 自带 4.8）。
    $ctor = [System.Security.Cryptography.Rfc2898DeriveBytes].GetConstructor(
        [type[]]@([string], [byte[]], [int], [System.Security.Cryptography.HashAlgorithmName]))
    if (-not $ctor) {
        throw "这台机器缺 .NET 4.7.2+（PBKDF2-SHA256 不可用）：请改用不带 -Encrypt 的备份"
    }
    $kdf = New-Object System.Security.Cryptography.Rfc2898DeriveBytes(
        $password, $salt, $iterations, [System.Security.Cryptography.HashAlgorithmName]::SHA256)
    return $kdf.GetBytes($length)
}

function Protect-BackupFile([string]$inPath, [string]$outPath, [string]$password) {
    $iterations = 200000
    $salt = New-Object byte[] 16
    $iv = New-Object byte[] 16
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($salt); $rng.GetBytes($iv) } finally { $rng.Dispose() }

    $keys = Get-Kdf $password $salt $iterations 64
    $encKey = $keys[0..31]
    $macKey = $keys[32..63]

    $plain = [System.IO.File]::ReadAllBytes($inPath)
    $aes = [System.Security.Cryptography.Aes]::Create()
    try {
        $aes.Mode = [System.Security.Cryptography.CipherMode]::CBC
        $aes.Padding = [System.Security.Cryptography.PaddingMode]::PKCS7
        $aes.Key = $encKey
        $aes.IV = $iv
        $encryptor = $aes.CreateEncryptor()
        $cipher = $encryptor.TransformFinalBlock($plain, 0, $plain.Length)
    } finally { $aes.Dispose() }

    $header = New-Object System.IO.MemoryStream
    $writer = New-Object System.IO.BinaryWriter($header)
    try {
        $writer.Write([System.Text.Encoding]::ASCII.GetBytes($Script:EncMagic))
        $writer.Write([uint16]1)
        $writer.Write([uint32]$iterations)
        $writer.Write($salt)
        $writer.Write($iv)
        $writer.Write([uint64]$plain.Length)
        $writer.Flush()
        $writer.Write($cipher)
        $writer.Flush()
        $all = $header.ToArray()
    } finally { $writer.Dispose(); $header.Dispose() }

    $hmac = New-Object System.Security.Cryptography.HMACSHA256(, $macKey)
    try { $tag = $hmac.ComputeHash($all) } finally { $hmac.Dispose() }

    $outBytes = New-Object byte[] ($all.Length + 32)
    [Array]::Copy($all, 0, $outBytes, 0, 54)
    [Array]::Copy($tag, 0, $outBytes, 54, 32)
    [Array]::Copy($all, 54, $outBytes, 86, $all.Length - 54)
    [System.IO.File]::WriteAllBytes($outPath, $outBytes)
}

function Test-IsEncrypted([string]$path) {
    $stream = [System.IO.File]::OpenRead($path)
    try {
        $buffer = New-Object byte[] 8
        $read = $stream.Read($buffer, 0, 8)
        return ($read -eq 8 -and
                [System.Text.Encoding]::ASCII.GetString($buffer) -eq $Script:EncMagic)
    } finally { $stream.Dispose() }
}

function Unprotect-BackupFile([string]$inPath, [string]$outPath, [string]$password) {
    $raw = [System.IO.File]::ReadAllBytes($inPath)
    if ($raw.Length -lt 87) { throw "加密备份不完整（只有 $($raw.Length) 字节）" }
    if ([System.Text.Encoding]::ASCII.GetString($raw, 0, 8) -ne $Script:EncMagic) {
        throw "不是 MRRC Modern 的加密备份"
    }
    $version = [BitConverter]::ToUInt16($raw, 8)
    if ($version -ne 1) { throw "不认识的加密格式版本：$version" }
    $iterations = [BitConverter]::ToUInt32($raw, 10)
    if ($iterations -lt 1000 -or $iterations -gt 2000000) {
        throw "加密参数不合理（迭代 $iterations）"
    }
    $salt = $raw[14..29]
    $iv = $raw[30..45]
    $expected = [BitConverter]::ToUInt64($raw, 46)
    $tag = $raw[54..85]

    $cipher = New-Object byte[] ($raw.Length - 86)
    [Array]::Copy($raw, 86, $cipher, 0, $cipher.Length)

    $keys = Get-Kdf $password $salt ([int]$iterations) 64
    $encKey = $keys[0..31]
    $macKey = $keys[32..63]

    $signed = New-Object byte[] (54 + $cipher.Length)
    [Array]::Copy($raw, 0, $signed, 0, 54)
    [Array]::Copy($cipher, 0, $signed, 54, $cipher.Length)
    $hmac = New-Object System.Security.Cryptography.HMACSHA256(, $macKey)
    try { $actual = $hmac.ComputeHash($signed) } finally { $hmac.Dispose() }
    $same = $true
    for ($i = 0; $i -lt 32; $i++) { if ($actual[$i] -ne $tag[$i]) { $same = $false; break } }
    if (-not $same) { throw "口令不对，或文件被改动过（HMAC 校验失败）" }

    $aes = [System.Security.Cryptography.Aes]::Create()
    try {
        $aes.Mode = [System.Security.Cryptography.CipherMode]::CBC
        $aes.Padding = [System.Security.Cryptography.PaddingMode]::PKCS7
        $aes.Key = $encKey
        $aes.IV = $iv
        $decryptor = $aes.CreateDecryptor()
        $plain = $decryptor.TransformFinalBlock($cipher, 0, $cipher.Length)
    } finally { $aes.Dispose() }
    if ([uint64]$plain.Length -ne $expected) { throw "解密后的长度与头部不符" }
    [System.IO.File]::WriteAllBytes($outPath, $plain)
}
```

- [ ] **步骤 2：跑静态守卫，确认加密断言转绿**

```bash
python3 -m unittest tests.test_windows_backup_script.SourceFilesTests -v
```

预期：`test_encryption_branch_really_deletes_the_plain_zip` 仍 `FAIL`（删除那一步在任务 6 的导出里，本任务只保证三个算法关键字在场）—— 若它现在是绿的，说明任务 6 已提前落地，跳过该失败照常继续。

- [ ] **步骤 3：Commit**

```bash
git add tools/windows/MRRC-Modern-Backup.ps1
git commit -m "feat(backup): AES-256 口令容器（PBKDF2-SHA256 + HMAC）"
```

---

## 任务 6：导出流程

**文件：**

- 修改：`tools/windows/MRRC-Modern-Backup.ps1`

- [ ] **步骤 1：追加导出函数**

```powershell
# ---------------------------------------------------------------- export
function Resolve-TokenDir($envLines, [string]$root) {
    $memFile = Get-EnvValue $envLines "MRRC_MEM_FILE"
    if (-not $memFile) { return $root }
    if (-not [System.IO.Path]::IsPathRooted($memFile)) { $memFile = Join-Path $root $memFile }
    return (Split-Path $memFile -Parent)
}

function Invoke-Export {
    param([string]$Root, [string]$OutPath, [string]$Password, [bool]$Encrypted)

    if (-not $Root -or -not (Test-Path -LiteralPath $Root)) {
        Warn "这台机器上还没有 MRRC Modern 的用户数据：$Root"
        Warn "（应用可能从未启动过 —— 先启动一次，再回来备份）"
        return $Script:ExitNothing
    }
    $tmp = Get-TempRoot
    try {
        $stage = Join-Path $tmp "stage"
        $payload = Join-Path $stage "payload"
        New-Item -ItemType Directory -Force -Path $payload | Out-Null

        $envPath = Join-Path $Root "mrrc_modern.env"
        $envLines = Read-EnvLines $envPath
        $tokenDir = Resolve-TokenDir $envLines $Root
        $notes = New-Object System.Collections.Generic.List[string]

        Step "收集"
        foreach ($item in $Script:FixedItems) {
            $source = if ($item.Path -eq "auth-tokens.json") {
                Join-Path $tokenDir "auth-tokens.json"
            } else { Join-Path $Root $item.Path }
            if (-not (Test-Path -LiteralPath $source)) {
                if ($item.Required) { $notes.Add("缺少必需项：$($item.Path)") }
                continue
            }
            Copy-WithRetry -Source $source -Destination (Join-Path $payload $item.Path)
            Ok ("{0,-22} {1}" -f $item.Path, $item.Kind)
        }

        # env 指向数据目录之外的证书对：收进来，恢复时改写到新路径。
        $extCert = Get-EnvValue $envLines "MRRC_SSL_CERT"
        $extKey = Get-EnvValue $envLines "MRRC_SSL_KEY"
        if ($extCert -and $extKey -and (Test-Path -LiteralPath $extCert) -and (Test-Path -LiteralPath $extKey)) {
            if (-not (Test-UnderRoot $extCert $Root)) {
                $extDir = Join-Path $payload "external-certs"
                New-Item -ItemType Directory -Force -Path $extDir | Out-Null
                Copy-WithRetry $extCert (Join-Path $extDir "external-cert.pem")
                Copy-WithRetry $extKey (Join-Path $extDir "external-key.pem")
                $notes.Add("MRRC_SSL_CERT/KEY 指向数据目录之外：已收入 external-certs/")
                Ok "external-certs          key"
            }
        }

        Step "写清单"
        $manifest = New-Manifest $payload @{
            AppVersion = (Get-AppVersion)
            AppDir     = (Get-AppDir)
            DataDir    = $Root
            Notes      = $notes.ToArray()
        }
        Write-Manifest $stage $manifest
        $totalBytes = (Get-ChildItem -LiteralPath $payload -Recurse -File -Force |
                       Measure-Object -Property Length -Sum).Sum
        Ok ("{0} 个文件，{1}" -f @($manifest.items).Count, (Format-Size ([long]$totalBytes)))

        Step "打包"
        $zipPath = $OutPath
        if (-not $zipPath) {
            $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
            $zipPath = Join-Path (Get-DesktopDir) ("MRRC-Modern-backup-" + $env:COMPUTERNAME + "-" + $stamp + ".zip")
        } elseif (Test-Path -LiteralPath $zipPath -PathType Container) {
            $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
            $zipPath = Join-Path $zipPath ("MRRC-Modern-backup-" + $env:COMPUTERNAME + "-" + $stamp + ".zip")
        }
        New-BackupZip $stage $zipPath
        Ok ("zip: {0}（{1}）" -f $zipPath, (Format-Size ((Get-Item -LiteralPath $zipPath).Length)))

        if ($Encrypted) {
            $password = Get-PasswordValue $Password $true
            $encPath = $zipPath + ".enc"
            Protect-BackupFile $zipPath $encPath $password
            Remove-Item -LiteralPath $zipPath -Force
            Ok ("加密包: {0}（{1}）" -f $encPath, (Format-Size ((Get-Item -LiteralPath $encPath).Length)))
            Warn "明文 zip 已删除（普通删除，SSD 上不保证不可恢复）。"
            Warn "口令不在文件里、也不会显示：请与这个包分开存放；丢了只能重新配置。"
            $zipPath = $encPath
        }

        Step "完成"
        Say "   备份包：$zipPath"
        Say "   里面有网页口令、HTTPS 私钥和云隧道凭据 —— 请像对待钥匙一样对待它。"
        Say "   不含 logs\ 与 recordings\（录音要留请自己拷走）。"
        Say ""
        Say "   在新电脑上（先装好 MRRC Modern）："
        Say "       MRRC-Modern-Backup.cmd  →  选 2  →  选中这个包"
        return $Script:ExitOk
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}
```

- [ ] **步骤 2：确认加密断言转绿**

```bash
python3 -m unittest tests.test_windows_backup_script.SourceFilesTests -v
```

预期：全部 `ok`（`Protect-BackupFile` 在 `Remove-Item -LiteralPath $zipPath -Force` 之前出现）。

- [ ] **步骤 3：Commit**

```bash
git add tools/windows/MRRC-Modern-Backup.ps1
git commit -m "feat(backup): 导出流程——固定清单、清单文件、可选加密"
```

---

## 任务 7：恢复流程

**文件：**

- 修改：`tools/windows/MRRC-Modern-Backup.ps1`

- [ ] **步骤 1：追加恢复函数**

```powershell
# ---------------------------------------------------------------- restore
function Get-RunningApp {
    $found = @()
    foreach ($name in $Script:ProcessNames) {
        $found += @(Get-Process -Name $name -ErrorAction SilentlyContinue)
    }
    return $found
}

function Close-RunningApp([bool]$AssumeYes, [bool]$AllowStop) {
    $procs = @(Get-RunningApp)
    if ($procs.Count -eq 0) { return $true }
    foreach ($p in $procs) { Warn ("正在运行：{0} (pid {1})" -f $p.ProcessName, $p.Id) }
    if (-not $AssumeYes -and -not $AllowStop) {
        $answer = Read-Host "   恢复前需要关闭 MRRC Modern。现在关闭吗？(y/N)"
        if ($answer -notmatch '^[Yy]') { return $false }
    }
    foreach ($p in $procs) {
        try {
            Stop-Process -Id $p.Id -Force -ErrorAction Stop
            Ok ("已关闭 {0} (pid {1})" -f $p.ProcessName, $p.Id)
        } catch {
            Warn ("关不掉 {0}：{1}" -f $p.ProcessName, $_.Exception.Message)
        }
    }
    Start-Sleep -Milliseconds 800
    return (@(Get-RunningApp).Count -eq 0)
}

function Backup-TargetFiles([string]$Root, [string]$PreDir) {
    $envLines = Read-EnvLines (Join-Path $Root "mrrc_modern.env")
    $tokenDir = Resolve-TokenDir $envLines $Root
    $count = 0
    foreach ($item in $Script:FixedItems) {
        $source = if ($item.Path -eq "auth-tokens.json") {
            Join-Path $tokenDir "auth-tokens.json"
        } else { Join-Path $Root $item.Path }
        if (-not (Test-Path -LiteralPath $source)) { continue }
        $target = Join-Path $PreDir $item.Path
        New-Item -ItemType Directory -Force -Path (Split-Path $target -Parent) | Out-Null
        try {
            Copy-Item -LiteralPath $source -Destination $target -Recurse -Force -ErrorAction Stop
            $count++
        } catch {
            Warn ("备份目标机文件失败（继续）：{0} — {1}" -f $item.Path, $_.Exception.Message)
        }
    }
    return $count
}

function Open-BackupFileDialog {
    Add-Type -AssemblyName System.Windows.Forms | Out-Null
    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    $dialog.Title = "选择 MRRC Modern 备份包"
    $dialog.Filter = "MRRC Modern 备份 (*.zip;*.enc)|*.zip;*.enc|所有文件 (*.*)|*.*"
    if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { return "" }
    return $dialog.FileName
}

function Invoke-Import {
    param(
        [string]$File,
        [string]$Password,
        [string]$Root,
        [bool]$AssumeYes,
        [bool]$AllowStop,
        # 仅测试用：预备份的落点。空 = 桌面。
        [string]$PreBackupRoot = ""
    )

    if (-not $File) {
        $File = Open-BackupFileDialog
        if (-not $File) { Warn "没有选择文件。"; return $Script:ExitNothing }
    }
    if (-not (Test-Path -LiteralPath $File)) {
        Bad "找不到备份包：$File"
        return $Script:ExitInvalid
    }

    $tmp = Get-TempRoot
    try {
        Step "校验备份包"
        $zipPath = $File
        if (Test-IsEncrypted $File) {
            $Password = Get-PasswordValue $Password $false
            $zipPath = Join-Path $tmp "payload.zip"
            Unprotect-BackupFile $File $zipPath $Password
            Ok "已解密"
        }
        $stage = Join-Path $tmp "stage"
        Expand-BackupZip $zipPath $stage
        $manifestPath = Join-Path $stage "manifest.json"
        if (-not (Test-Path -LiteralPath $manifestPath)) {
            throw "包里没有 manifest.json —— 不是 MRRC Modern 的备份"
        }
        $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
        Test-Manifest $stage $manifest
        Ok ("{0} 个文件，摘要全部一致" -f @($manifest.items).Count)
        Show-Manifest $manifest

        Step "目标机"
        Say "   数据目录：$Root"
        $installed = Get-AppVersion
        if ($installed -ne "unknown" -and $manifest.appVersion -ne $installed) {
            Warn ("包来自 {0}，这台机器是 {1} —— 继续，但请留意版本差异" -f $manifest.appVersion, $installed)
        }
        if (-not $AssumeYes) {
            $answer = Read-Host "   现在把这些文件写进这台机器吗？(y/N)"
            if ($answer -notmatch '^[Yy]') { Warn "已取消，什么都没写。"; return $Script:ExitNothing }
        }

        Step "关闭应用"
        if (-not (Close-RunningApp $AssumeYes $AllowStop)) {
            Bad "MRRC Modern 还在运行，什么都没写。关掉它再跑一次。"
            return $Script:ExitAppRunning
        }

        Step "备份目标机现有配置"
        # 注意：PowerShell 变量名不区分大小写 —— 这里的局部变量**不能**叫 $root，
        # 那会覆盖参数 $Root（数据目录）。
        $preRoot = if ($PreBackupRoot) { $PreBackupRoot } else { Get-DesktopDir }
        $preDir = Join-Path $preRoot ("MRRC-restore-pre-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
        $count = Backup-TargetFiles $Root $preDir
        if ($count -gt 0) { Ok "已备份 $count 项到 $preDir" } else { Say "   目标机没有可备份的现有配置。" }

        Step "写入"
        $payload = Join-Path $stage "payload"
        $newEnvPath = Join-Path $Root "mrrc_modern.env"
        $newLines = $null
        $rewrite = $null
        if (Test-Path -LiteralPath (Join-Path $payload "mrrc_modern.env")) {
            $oldLines = Read-EnvLines (Join-Path $payload "mrrc_modern.env")
            $rewrite = Update-EnvPaths $oldLines $manifest.dataDir $Root
            $newLines = $rewrite.Lines
        }

        foreach ($item in $Script:FixedItems) {
            $source = Join-Path $payload $item.Path
            if (-not (Test-Path -LiteralPath $source)) { continue }
            if ($item.Path -eq "mrrc_modern.env") { continue }   # 最后写，见下
            $target = if ($item.Path -eq "auth-tokens.json") {
                Join-Path (Resolve-TokenDir $newLines $Root) "auth-tokens.json"
            } else { Join-Path $Root $item.Path }
            New-Item -ItemType Directory -Force -Path (Split-Path $target -Parent) | Out-Null
            Copy-WithRetry -Source $source -Destination $target
            Ok $item.Path
        }

        $extDir = Join-Path $payload "external-certs"
        if (Test-Path -LiteralPath $extDir) {
            $certTarget = Join-Path $Root "certs\external-cert.pem"
            $keyTarget = Join-Path $Root "certs\external-key.pem"
            New-Item -ItemType Directory -Force -Path (Join-Path $Root "certs") | Out-Null
            Copy-WithRetry (Join-Path $extDir "external-cert.pem") $certTarget
            Copy-WithRetry (Join-Path $extDir "external-key.pem") $keyTarget
            Ok "external-certs -> certs\external-*.pem"
            $replacements = @{
                "MRRC_SSL_CERT" = $certTarget
                "MRRC_SSL_KEY"  = $keyTarget
                "FT710_SSL_CERT" = $certTarget
                "FT710_SSL_KEY"  = $keyTarget
            }
            $newLines = Apply-EnvValues $newLines $replacements
        }

        if ($newLines) {
            Write-EnvLines $newEnvPath $newLines
            Ok "mrrc_modern.env"
            if ($rewrite -and $rewrite.Changed.Count -gt 0) {
                foreach ($c in $rewrite.Changed) { Say "     改写 $c" }
            }
            if ($rewrite -and $rewrite.Warnings.Count -gt 0) {
                foreach ($w in $rewrite.Warnings) { Warn $w }
            }
        }

        Step "完成"
        Say "   配置、证书与学习库已恢复到：$Root"
        Say "   目标机原有配置：$preDir"
        Say ""
        Say "   下一步：双击桌面上的 MRRC Modern 启动。"
        Say "   * auth-tokens.json 是服务端那一半；浏览器里的 cookie 不会跟着走，"
        Say "     所以这台电脑仍然要输一次登录口令（口令就是恢复过来的那个）。"
        Say "   * 证书来自另一台机器：浏览器会照常对自签名证书告警一次。若云入口已接入，"
        Say "     仍然用这张证书 —— 不要为了消警告重新签，否则那条入口会 502。"
        return $Script:ExitOk
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# 把若干键的值改成给定值（保留注释与其它行）。
function Apply-EnvValues($lines, [hashtable]$values) {
    $pattern = '^(\s*)([A-Za-z0-9_]+)(\s*=\s*)(.*)$'
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($raw in $lines) {
        $m = [regex]::Match($raw, $pattern)
        if ($m.Success -and $values.ContainsKey($m.Groups[2].Value)) {
            $out.Add($m.Groups[1].Value + $m.Groups[2].Value + $m.Groups[3].Value +
                     $values[$m.Groups[2].Value])
        } else {
            $out.Add($raw)
        }
    }
    return $out.ToArray()
}
```

- [ ] **步骤 2：Commit**

```bash
git add tools/windows/MRRC-Modern-Backup.ps1
git commit -m "feat(backup): 恢复流程——校验、预备份、关应用、路径改写"
```

---

## 任务 8：菜单、参数分发、`-SelfTest`

**文件：**

- 修改：`tools/windows/MRRC-Modern-Backup.ps1`

- [ ] **步骤 1：追加入口与自测**

```powershell
# ---------------------------------------------------------------- self test
function New-FakeDataDir([string]$root, [string]$tag) {
    New-Item -ItemType Directory -Force -Path (Join-Path $root "certs") | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $root "fleet") | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $root "logs") | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $root "recordings") | Out-Null
    $envText = @(
        "# fake config for -SelfTest",
        "MRRC_RADIO_MODEL=ft891",
        "MRRC_WEB_PASSWORD=$tag-secret",
        "MRRC_SSL_CERT=$root\certs\server.crt",
        "MRRC_SSL_KEY=$root\certs\server.key"
    ) -join "`r`n"
    [System.IO.File]::WriteAllText((Join-Path $root "mrrc_modern.env"), $envText,
        (New-Object System.Text.UTF8Encoding($false)))
    [System.IO.File]::WriteAllText((Join-Path $root "certs\server.crt"), "cert-$tag",
        (New-Object System.Text.UTF8Encoding($false)))
    [System.IO.File]::WriteAllText((Join-Path $root "certs\server.key"), "key-$tag",
        (New-Object System.Text.UTF8Encoding($false)))
    [System.IO.File]::WriteAllText((Join-Path $root "mem_channels.json"), "{`"tag`":`"$tag`"}",
        (New-Object System.Text.UTF8Encoding($false)))
    [System.IO.File]::WriteAllText((Join-Path $root "atr1000_tuner.json"), "{`"tag`":`"$tag`"}",
        (New-Object System.Text.UTF8Encoding($false)))
    [System.IO.File]::WriteAllText((Join-Path $root "fleet\frpc-test.toml"), "serverAddr = `"$tag`"",
        (New-Object System.Text.UTF8Encoding($false)))
    [System.IO.File]::WriteAllText((Join-Path $root "auth-tokens.json"), "{`"tag`":`"$tag`"}",
        (New-Object System.Text.UTF8Encoding($false)))
    [System.IO.File]::WriteAllText((Join-Path $root "logs\server.log"), "keep-me",
        (New-Object System.Text.UTF8Encoding($false)))
    [System.IO.File]::WriteAllText((Join-Path $root "recordings\qso.mp3"), "keep-me",
        (New-Object System.Text.UTF8Encoding($false)))
}

function Invoke-SelfTest {
    # 自测会写目录，但**绝不**替人关应用：应用在跑就直接拒绝。
    $running = @(Get-RunningApp)
    if ($running.Count -gt 0) {
        Bad "自测需要先关掉 MRRC Modern（当前在运行），不替你杀进程。"
        return $Script:ExitAppRunning
    }
    $failures = New-Object System.Collections.Generic.List[string]
    $root = Get-TempRoot
    try {
        $a = Join-Path $root "A"
        $b = Join-Path $root "B"
        $c = Join-Path $root "C"
        New-FakeDataDir $a "alpha"
        New-FakeDataDir $b "bravo"

        Step "1) 导出 A"
        $zip = Join-Path $root "a.zip"
        $rc = Invoke-Export -Root $a -OutPath $zip -Password "" -Encrypted $false
        if ($rc -ne $Script:ExitOk) { $failures.Add("导出返回 $rc") }
        if (-not (Test-Path -LiteralPath $zip)) { $failures.Add("没有生成 zip") }

        Step "2) 恢复到 B（并检查 B 的旧文件被备份）"
        $rc = Invoke-Import -File $zip -Password "" -Root $b -PreBackupRoot $root -AssumeYes $true -AllowStop $true
        if ($rc -ne $Script:ExitOk) { $failures.Add("恢复返回 $rc") }
        foreach ($rel in @("mrrc_modern.env", "certs\server.crt", "certs\server.key",
                           "mem_channels.json", "atr1000_tuner.json",
                           "fleet\frpc-test.toml", "auth-tokens.json")) {
            $left = Join-Path $a $rel
            $right = Join-Path $b $rel
            if (-not (Test-Path -LiteralPath $right)) { $failures.Add("B 缺 $rel"); continue }
            if ((Get-Sha256 $left) -ne (Get-Sha256 $right)) { $failures.Add("B 的 $rel 与 A 不同") }
        }
        foreach ($keep in @("logs\server.log", "recordings\qso.mp3")) {
            $p = Join-Path $b $keep
            if (-not (Test-Path -LiteralPath $p)) { $failures.Add("恢复动了 $keep") ; continue }
            if ((Get-Content -LiteralPath $p -Raw) -ne "keep-me") { $failures.Add("恢复改了 $keep") }
        }
        $envB = Read-EnvLines (Join-Path $b "mrrc_modern.env")
        $certB = Get-EnvValue $envB "MRRC_SSL_CERT"
        if ($certB -ne (Join-Path $b "certs\server.crt")) {
            $failures.Add("env 的 MRRC_SSL_CERT 没改写：$certB")
        }

        Step "3) 口令往返"
        $enc = Join-Path $root "a-enc.zip.enc"
        $rc = Invoke-Export -Root $a -OutPath (Join-Path $root "a-enc.zip") -Password "test-1234" -Encrypted $true
        if ($rc -ne $Script:ExitOk) { $failures.Add("加密导出返回 $rc") }
        if (Test-Path -LiteralPath (Join-Path $root "a-enc.zip")) {
            $failures.Add("加密后明文 zip 还在")
        }
        $rc = Invoke-Import -File $enc -Password "test-1234" -Root $c -PreBackupRoot $root -AssumeYes $true -AllowStop $true
        if ($rc -ne $Script:ExitOk) { $failures.Add("加密恢复返回 $rc") }
        if ((Get-Sha256 (Join-Path $a "mrrc_modern.env")) -ne (Get-Sha256 (Join-Path $c "mrrc_modern.env"))) {
            $failures.Add("加密往返后 env 不同")
        }

        Step "4) 篡改必须被拒"
        $bad = Join-Path $root "bad.zip"
        Copy-Item -LiteralPath $zip -Destination $bad
        $bytes = [System.IO.File]::ReadAllBytes($bad)
        $bytes[$bytes.Length - 1] = $bytes[$bytes.Length - 1] -bxor 0xFF
        [System.IO.File]::WriteAllBytes($bad, $bytes)
        $d = Join-Path $root "D"
        $rc = Invoke-Import -File $bad -Password "" -Root $d -AssumeYes $true -AllowStop $true
        if ($rc -eq $Script:ExitOk) { $failures.Add("坏包被当成成功") }
        if (Test-Path -LiteralPath (Join-Path $d "mrrc_modern.env")) { $failures.Add("坏包写进了目标目录") }

        Step "结果"
        if ($failures.Count -eq 0) {
            Ok "SELFTEST PASS"
            return $Script:ExitOk
        }
        foreach ($f in $failures) { Bad "SELFTEST FAIL: $f" }
        return $Script:ExitInvalid
    } catch {
        Bad ("SELFTEST FAIL: " + $_.Exception.Message)
        return $Script:ExitInvalid
    } finally {
        Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------- entry point
function Show-Menu {
    Say ""
    Say "  MRRC Modern - 换电脑：配置与证书的备份 / 恢复"
    Say ""
    Say "   1  备份这台机器"
    Say "   2  从备份恢复（这台机器上）"
    Say "   Q  退出"
    Say ""
    $choice = Read-Host "  选择"
    switch ($choice.Trim().ToUpperInvariant()) {
        "1" { return "export" }
        "2" { return "restore" }
        default { return "quit" }
    }
}

function Invoke-Main {
    if ($SelfTest) { return (Invoke-SelfTest) }

    if ($List) {
        $tmp = Get-TempRoot
        try {
            $zipPath = $List
            if (Test-IsEncrypted $List) {
                $Password = Get-PasswordValue $Password $false
                $zipPath = Join-Path $tmp "payload.zip"
                Unprotect-BackupFile $List $zipPath $Password
            }
            $stage = Join-Path $tmp "stage"
            Expand-BackupZip $zipPath $stage
            $manifest = Get-Content -LiteralPath (Join-Path $stage "manifest.json") -Raw -Encoding UTF8 |
                ConvertFrom-Json
            Show-Manifest $manifest
            return $Script:ExitOk
        } catch {
            Bad $_.Exception.Message
            return $Script:ExitInvalid
        } finally {
            Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    if ($Import) {
        return (Invoke-Import -File $Import -Password $Password -Root (Get-UserDataDir) `
                -AssumeYes ([bool]$Yes) -AllowStop ([bool]$StopApp))
    }

    $mode = "menu"
    if ($Export) { $mode = "export" }
    elseif ($Restore) { $mode = "restore" }
    if ($mode -eq "menu") { $mode = Show-Menu }

    if ($mode -eq "export") {
        return (Invoke-Export -Root (Get-UserDataDir) -OutPath $Out -Password $Password `
                -Encrypted ($Encrypt.IsPresent -or [bool]$Password))
    }
    if ($mode -eq "restore") {
        return (Invoke-Import -File "" -Password $Password -Root (Get-UserDataDir) `
                -AssumeYes ([bool]$Yes) -AllowStop ([bool]$StopApp))
    }
    return $Script:ExitNothing
}

try {
    $code = Invoke-Main
} catch {
    Bad $_.Exception.Message
    Bad "已停止。写入前就停下的部分没有动任何文件；已经写过的文件，在桌面 MRRC-restore-pre-* 里有原来的副本。"
    $code = $Script:ExitInvalid
}
exit $code
```

- [ ] **步骤 2：静态守卫全绿**

```bash
python3 -m unittest tests.test_windows_backup_script -v
```

预期：`OK`（`PublishedCopyTests` 仍 `FAIL`，因为发布副本还没复制 —— 任务 9 处理；若希望本任务只跑 `SourceFilesTests`，用 `python3 -m unittest tests.test_windows_backup_script.SourceFilesTests -v`，预期 `OK`）。

- [ ] **步骤 3：Commit**

```bash
git add tools/windows/MRRC-Modern-Backup.ps1
git commit -m "feat(backup): 菜单、参数分发与 -SelfTest"
```

---

## 任务 9：发布副本与文档

**文件：**

- 创建：`website/downloads/MRRC-Modern-Backup.ps1`、`website/downloads/MRRC-Modern-Backup.cmd`
- 修改：`website/guide.html`、`website/zh/guide.html`、`website/index.html`、`website/zh/index.html`
- 修改：`docs/OPERATION_GUIDE.md`、`tests/README.md`

- [ ] **步骤 1：复制发布物**

```bash
cp tools/windows/MRRC-Modern-Backup.ps1 tools/windows/MRRC-Modern-Backup.cmd website/downloads/
python3 -m unittest tests.test_windows_backup_script -v
```

预期：`OK`（含 `PublishedCopyTests`）。

- [ ] **步骤 2：中文指南加 §6.5**

在 `website/zh/guide.html` 的 `<h3 id="remote-help">6.4 远程协助…` 那一节结束之后、下一节之前插入：

```html
<h3 id="move-computer">6.5 换电脑：配置与证书一键备份/恢复（Windows）</h3>
<p><strong>换机时不要把安装目录拷过去，也不要手抄配置</strong>：真正要带走的是用户数据目录 <code>%LOCALAPPDATA%\MRRC-Modern</code> —— 里面有电台机型/串口/声卡名/网页口令、HTTPS 证书与私钥、记忆频道、天调学习值、云入口隧道配置。其中<strong>证书尤其不能重签</strong>：云入口钉住的就是这张证书，换一张那条入口会 502。</p>
<p>用站上 <code>downloads/MRRC-Modern-Backup.cmd</code>（源码在仓库 <code>tools/windows/MRRC-Modern-Backup.ps1</code>）。<strong>不需要管理员权限。</strong></p>
<ol type="1">
<li><strong>旧电脑</strong>：双击 <code>MRRC-Modern-Backup.cmd</code> → 选 <code>1</code>。桌面会多一个 <code>MRRC-Modern-backup-&lt;主机名&gt;-&lt;时间&gt;.zip</code>，包里附清单（每个文件的大小与 SHA-256）。它<strong>不含</strong> <code>logs\</code> 与 <code>recordings\</code> —— 录音要留请自己拷走。</li>
<li><strong>想加口令</strong>：用 <code>-Export -Encrypt</code>（会提示输两遍，口令不经过命令行、不落盘）。加密后是一个 <code>.zip.enc</code>，<strong>丢了口令只能重新配置</strong>。</li>
<li><strong>新电脑</strong>：先装好 MRRC Modern（先不要打开），把 <code>.cmd</code> 与 <code>.ps1</code> 放同一个文件夹，双击 <code>.cmd</code> → 选 <code>2</code> → 选中那个包。脚本会先把新电脑现有的配置备份到桌面 <code>MRRC-restore-pre-&lt;时间&gt;\</code>，再逐项写入。</li>
</ol>
<p><strong>三件必须知道的事</strong>：</p>
<ul>
<li>包里同时有<strong>网页口令、TLS 私钥和云隧道凭据</strong> —— 像对待钥匙一样对待它，别放公共网盘、别发聊天群；</li>
<li>恢复 <code>auth-tokens.json</code> 只是<strong>服务端那一半</strong>，浏览器里的 cookie 不会跟着走，新电脑仍然要输一次登录口令（口令就是恢复过来的那个）；</li>
<li>证书来自另一台机器，浏览器会照常对自签名证书告警一次；<strong>不要为了消警告重新签证书</strong>，那会让已接入的云入口变成 502。</li>
</ul>
```

- [ ] **步骤 3：英文指南加同一节**

在 `website/guide.html` 对应的 §6.4 之后插入（标题与正文用英文，锚点同名 `move-computer`）：

```html
<h3 id="move-computer">6.5 Moving to a new computer: back up and restore (Windows)</h3>
<p><strong>Do not copy the install folder and do not retype the settings.</strong> What has to travel is the user data directory <code>%LOCALAPPDATA%\MRRC-Modern</code>: radio model / serial port / audio device names / web password, the HTTPS certificate and its private key, memory channels, the ATR-1000 learning file, and the Cloud Hub tunnel config. The <strong>certificate especially must not be re-signed</strong> — the cloud entry pins it, and a fresh one turns that entry into a 502.</p>
<p>Use <code>downloads/MRRC-Modern-Backup.cmd</code> from the download page (source: <code>tools/windows/MRRC-Modern-Backup.ps1</code>). <strong>No administrator rights needed.</strong></p>
<ol type="1">
<li><strong>Old machine</strong>: double-click the <code>.cmd</code> and pick <code>1</code>. A <code>MRRC-Modern-backup-&lt;host&gt;-&lt;time&gt;.zip</code> lands on the desktop, with a manifest (size and SHA-256 per file). It does <strong>not</strong> contain <code>logs\</code> or <code>recordings\</code> — copy recordings yourself if you want to keep them.</li>
<li><strong>Want a passphrase</strong>: run <code>-Export -Encrypt</code> (asked twice, never on the command line, never written to disk). The result is a <code>.zip.enc</code>; <strong>a lost passphrase means starting over</strong>.</li>
<li><strong>New machine</strong>: install MRRC Modern first (do not start it), keep the <code>.cmd</code> and <code>.ps1</code> together, double-click the <code>.cmd</code>, pick <code>2</code>, choose the archive. The tool first backs up whatever configuration the new machine already had to <code>MRRC-restore-pre-&lt;time&gt;\</code> on the desktop, then writes the files.</li>
</ol>
<p><strong>Three things to know</strong>: the archive holds your <strong>web password, TLS private key and tunnel credentials</strong> — treat it like a key; restoring <code>auth-tokens.json</code> is only the <strong>server half</strong> (the browser cookie does not travel, so you still log in once with the restored password); and the certificate comes from the other machine, so the browser warns once — <strong>do not re-sign it to silence that warning</strong>, it would break a cloud entry that is already connected.</p>
```

- [ ] **步骤 4：两处首页页脚加链接**

`website/index.html:1283` 那段页脚里，把「下载清理脚本」后面补一句：

```html
 · 换电脑：<a href="downloads/MRRC-Modern-Backup.cmd">配置与证书一键备份/恢复</a>
```

`website/zh/index.html:1182` 同样补：

```html
 · 换电脑：<a href="../downloads/MRRC-Modern-Backup.cmd">配置与证书一键备份/恢复</a>
```

- [ ] **步骤 5：操作指南补一句交叉引用**

`docs/OPERATION_GUIDE.md` §0.10（版本与更新）那段后面加一行：

```markdown
换电脑时不要重配：用下载页的 `MRRC-Modern-Backup.cmd` 把用户数据目录（配置 + 证书 + 记忆频道 + 天调学习值 + 云隧道配置）打成包，在新机器上装好应用后一次恢复。证书不能重签（云入口钉的是它），详见网站指南 §6.5。
```

- [ ] **步骤 6：`tests/README.md` 补模块条目**

在模块清单里加入：

```markdown
### test_windows_backup_script.py — 换电脑备份/恢复脚本（静态守卫）

`tools/windows/MRRC-Modern-Backup.ps1/.cmd` 的源码事实：UTF-8 BOM、`.cmd` 纯 ASCII、
块内 echo 不带括号、发布副本与源逐字节一致、固定清单与测试常量一致、加密后删明文。
行为由脚本自己的 `-SelfTest` 在 Windows 上验。
```

并把 Overview 的「N tests across M test modules」在跑完全套后改成实际数字（`unittest discover` 的权威输出）。

- [ ] **步骤 7：站点审计**

```bash
python3 dev_tools/site_audit.py
```

预期：0 failing（新链接都是真实文件；中英两节成对）。

- [ ] **步骤 8：Commit**

```bash
git add website/downloads/MRRC-Modern-Backup.ps1 website/downloads/MRRC-Modern-Backup.cmd \
        website/guide.html website/zh/guide.html website/index.html website/zh/index.html \
        docs/OPERATION_GUIDE.md tests/README.md
git commit -m "docs+release: 发布换电脑备份脚本与指南 §6.5"
```

---

## 任务 10：全量验证与收尾

**文件：**

- 修改（仅在验证暴露问题时）：上述任一文件

- [ ] **步骤 1：全套件**

```bash
python3 -m unittest discover -s tests 2>&1 | tail -5
```

预期：`OK`（无 `FAILED`）。若失败，按报错修，**不要**用 `| tail` 掩盖退出码：

```bash
if python3 -m unittest discover -s tests >/tmp/t.log 2>&1; then echo TESTS-OK; else echo TESTS-FAIL; tail -30 /tmp/t.log; fi
```

- [ ] **步骤 2：语法与编码复核**

```bash
python3 - <<'EOF'
from pathlib import Path
for p in ("tools/windows/MRRC-Modern-Backup.ps1", "website/downloads/MRRC-Modern-Backup.ps1"):
    raw = Path(p).read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), p
    t = raw[3:].decode("utf-8")
    assert t.count("function ") >= 20, (p, t.count("function "))
print("encoding ok")
EOF
```

- [ ] **步骤 3：发布状态说明**

本任务**不**部署站点（`rsync` 是发布动作，由操作者决定）。在提交信息里写清：

```
尚未部署：website/downloads 与 guide 的改动需要跑一次站点发布；
Windows 侧的行为验收（-SelfTest）需要在 VM/真机上执行，清单见下。
```

- [ ] **步骤 4：Commit（若有修正）**

```bash
git add -A tools/windows website/downloads website/guide.html website/zh/guide.html \
        website/index.html website/zh/index.html docs/OPERATION_GUIDE.md tests/
git commit -m "chore(backup): 全量验证后的修正"
```

---

## Windows 侧验收清单（构建/支持人员执行）

1. 把 `MRRC-Modern-Backup.ps1` 与 `.cmd` 放同一文件夹，双击 → 菜单出现，中文不乱码。
2. `powershell -NoProfile -ExecutionPolicy Bypass -File .\MRRC-Modern-Backup.ps1 -SelfTest`
   → 末行 `SELFTEST PASS`，退出码 0。
3. 真机导出一次：桌面出现 zip，包内 `manifest.json` 的 `items` 含 `mrrc_modern.env`、`certs/…`、
   `mem_channels.json`、`auth-tokens.json`、`fleet/…`（存在的那几项），`logs/`、`recordings/` 不在其中。
4. 在另一台干净机器（或改过名的工作目录）恢复一次：应用启动后机型/串口/口令与旧机一致，
   云入口仍能连通（证书没换）。
5. `-Encrypt` 往返一次；输错口令时提示「口令不对，或文件被改动过」，且目标目录未被写入。
6. 应用运行中恢复 → 提示关闭；拒绝 → 退出码 30、什么都没写。
