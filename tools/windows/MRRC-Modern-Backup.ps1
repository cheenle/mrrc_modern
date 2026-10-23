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
            $Password = Get-PasswordValue $Password $true
            $encPath = $zipPath + ".enc"
            Protect-BackupFile $zipPath $encPath $Password
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
