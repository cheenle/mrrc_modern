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
