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
