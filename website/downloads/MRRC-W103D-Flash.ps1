<#
    MRRC Modern - 把 W103D 刷机镜像写到 U 盘（Windows）

    为什么有这个工具
    ----------------
    官网下载页给的是 MRRC-Modern-<ver>-w103d.img.gz（约 1.2 GB，解开 3.43 GiB）。
    macOS / Linux 上一条 `gzip -dc ... | dd of=/dev/rdiskN` 就完了；Windows 上
    没有 dd，而图形化工具（Win32DiskImager / Rufus / balenaEtcher）都要求用户
    先解压出 .img 再自己认盘 —— 下载页上的三步说明因此长得不像给普通人看的。

    这个脚本把那三步收成一次双击：
        下载 → 校验哈希 → 选盘 → 解压 + 写入 → 回读比对

    它的破坏面是"整块盘"，所以规则写得比功能多：
      * 只列 USB / 可移动介质；内置盘**没有**任何覆盖选项（不给"我确定"的口子）；
      * 系统盘、引导盘硬性拒绝，即使它插在 USB 上；
      * 盘容量必须装得下镜像；
      * 选定后要手输 ERASE 才动第一笔；
      * 写完回读整份镜像比对 SHA-256 —— 假容量盘、坏盘、写一半断掉都会在这一步露出来。

    用法
    ----
      双击 MRRC-W103D-Flash.cmd（它会自己请求管理员权限）
      或：powershell -ExecutionPolicy Bypass -File MRRC-W103D-Flash.ps1 [-ImagePath D:\x.img.gz]
          [-DownloadOnly] [-KeepImage 0]

    参数
    ----
      -ImagePath    已有的 .img.gz（默认：脚本同目录的 MRRC-Modern-w103d.img.gz）
      -ImageUrl     镜像地址，默认官网的稳定别名 MRRC-Modern-w103d.img.gz
      -DownloadOnly 只下载 + 校验，不碰任何磁盘
      -KeepImage 0  写完后删掉本地镜像（默认保留，下次刷机不用重下）
#>

[CmdletBinding()]
param(
    [string]$ImageUrl  = 'https://www.vlsc.net/mrrc_modern/downloads/MRRC-Modern-w103d.img.gz',
    [string]$ImagePath = '',
    [switch]$DownloadOnly,
    [int]$KeepImage = 1
)

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'Continue'

# ── 小工具 ──────────────────────────────────────────────────────────────
function Write-Step([string]$Text) { Write-Host ""; Write-Host "== $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text)   { Write-Host "   OK  $Text" -ForegroundColor Green }
function Write-Bad([string]$Text)  { Write-Host "   !!  $Text" -ForegroundColor Red }
function Fail([string]$Text) {
    Write-Host ""
    Write-Host "失败：$Text" -ForegroundColor Red
    Write-Host ""
    exit 1
}

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p  = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Format-Size([double]$Bytes) {
    if ($Bytes -ge 1GB) { return ('{0:N1} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N0} MB' -f ($Bytes / 1MB)) }
    return ('{0:N0} KB' -f ($Bytes / 1KB))
}

function Get-GzipUncompressedSize([string]$Path) {
    # gzip 的尾 4 字节是解压后长度（mod 2^32）。镜像 3.43 GiB，够用；
    # 万一将来超过 4 GiB，写盘循环里还有实际计数的兜底（绝不靠这个值截断）。
    $fs = [IO.File]::OpenRead($Path)
    try {
        [void]$fs.Seek(-4, [IO.SeekOrigin]::End)
        $b = New-Object byte[] 4
        [void]$fs.Read($b, 0, 4)
        return [int64]([BitConverter]::ToUInt32($b, 0))
    } finally { $fs.Dispose() }
}

# ── 0. 前置 ────────────────────────────────────────────────────────────
if (-not (Test-Admin)) {
    Fail "需要管理员权限。请运行 MRRC-W103D-Flash.cmd（它会自己请求提权），或在管理员 PowerShell 里运行本脚本。"
}
if (-not (Get-Command Get-Disk -ErrorAction SilentlyContinue)) {
    Fail "这台 Windows 没有 Storage 模块（Get-Disk）。需要 Windows 8 / Server 2012 或更新。"
}
if (-not $ImagePath) { $ImagePath = Join-Path $PSScriptRoot 'MRRC-Modern-w103d.img.gz' }

Write-Host ""
Write-Host "MRRC Modern - W103D 刷机盘制作" -ForegroundColor Yellow
Write-Host "镜像：$ImageUrl"

# ── 1. 下载 ────────────────────────────────────────────────────────────
Write-Step "镜像"
if (Test-Path -LiteralPath $ImagePath) {
    Write-Ok "已有本地文件：$ImagePath"
    Write-Ok "大小：$(Format-Size (Get-Item -LiteralPath $ImagePath).Length)"
} else {
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if (-not $curl) {
        Fail "缺少 curl.exe（Windows 10 1809+ / Windows 11 自带）。也可以用浏览器把镜像下载到 $ImagePath 再重跑。"
    }
    Write-Host "   正在下载（约 1.2 GB，可断点续传；中断后重跑会接着下）…"
    & $curl.Source -L --fail --retry 3 --retry-delay 2 --continue-at - -o $ImagePath $ImageUrl
    if ($LASTEXITCODE -ne 0) {
        Fail "下载失败（curl 退出码 $LASTEXITCODE）。检查网络后重跑，已下载的部分会保留并续传。"
    }
    Write-Ok "下载完成：$(Format-Size (Get-Item -LiteralPath $ImagePath).Length)"
}

# ── 2. 哈希 ────────────────────────────────────────────────────────────
Write-Step "校验哈希"
$shaLocal = "$ImagePath.sha256"
if (-not (Test-Path -LiteralPath $shaLocal)) {
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
        & $curl.Source -L --fail -s -o $shaLocal "$ImageUrl.sha256" 2>$null
    }
}
if (-not (Test-Path -LiteralPath $shaLocal)) {
    Write-Bad "拿不到 $ImageUrl.sha256 —— 跳过哈希校验（gzip 自带 CRC 仍能发现下载损坏，但无法证明文件没被换过）。"
} else {
    $expected = ((Get-Content -LiteralPath $shaLocal -TotalCount 1) -split '\s+')[0].ToLower()
    Write-Host "   计算中（1.2 GB，约几秒）…"
    $actual = (Get-FileHash -LiteralPath $ImagePath -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne $expected) {
        Write-Bad "期望 $expected"
        Write-Bad "实际 $actual"
        Fail "镜像哈希不符。删除 $ImagePath 后重下（可能是下载损坏或代理改写了文件）。"
    }
    Write-Ok "SHA-256 一致：$actual"
}

$imageBytes = Get-GzipUncompressedSize $ImagePath
Write-Ok ("解压后大小：" + (Format-Size $imageBytes))

if ($DownloadOnly) {
    Write-Host ""
    Write-Host "只下载模式：镜像已就绪，未接触任何磁盘。" -ForegroundColor Green
    exit 0
}

# ── 3. 选盘 ────────────────────────────────────────────────────────────
Write-Step "可写磁盘"

$systemDisk = $null
try {
    $systemDisk = (Get-Partition -DriveLetter ($env:SystemDrive.Substring(0, 1))).DiskNumber
} catch {
    Write-Bad "无法确定系统盘编号（$($_.Exception.Message)）—— 下面每一块盘都会被当作可疑对象。"
}

$all = @(Get-Disk | Sort-Object Number)
$writable = @()
Write-Host ""
Write-Host ("  {0,-3} {1,-34} {2,-11} {3,-12} {4}" -f '#', '型号', '容量', '总线/介质', '状态')
Write-Host ("  {0,-3} {1,-34} {2,-11} {3,-12} {4}" -f '---', '---------------------------------', '----------', '----------', '----')
foreach ($d in $all) {
    $isSys  = ($null -ne $systemDisk -and $d.Number -eq $systemDisk) -or $d.IsSystem -or $d.IsBoot
    $isMov  = ($d.BusType -eq 'USB') -or ($d.MediaType -eq 'Removable')
    $enough = $d.Size -ge $imageBytes
    $state  = if ($isSys) { '系统盘（禁止）' }
              elseif (-not $isMov) { '内置盘（禁止）' }
              elseif (-not $enough) { "太小（需 $(Format-Size $imageBytes)）" }
              else { '✓ 可写' }
    if ($state -eq '✓ 可写') { $writable += $d.Number }
    $model = $d.FriendlyName
    if ($model.Length -gt 34) { $model = $model.Substring(0, 34) }
    Write-Host ("  {0,-3} {1,-34} {2,-11} {3,-12} {4}" -f $d.Number, $model,
                (Format-Size $d.Size), "$($d.BusType)/$($d.MediaType)", $state)
}

if ($writable.Count -eq 0) {
    Write-Host ""
    Fail ("没有可写的盘。请把 U 盘（至少 $(Format-Size $imageBytes)）插上再重跑。" +
          "系统盘与内置盘永远不出现在可写列表里 —— 这是刻意设计，不是 bug。")
}

Write-Host ""
Write-Host "可写：$(($writable | ForEach-Object { "#$_" }) -join '  ')" -ForegroundColor Yellow
$answer = Read-Host "输入要写入的磁盘编号（其余键退出）"
$number = 0
if (-not $answer -or -not [int]::TryParse($answer.Trim(), [ref]$number)) {
    Fail "没有输入有效编号，已退出（没有写入任何东西）。"
}
if ($writable -notcontains $number) { Fail "磁盘 #$number 不在可写列表里，拒绝执行。" }

$disk = Get-Disk -Number $number

# ── 4. 二次确认 ────────────────────────────────────────────────────────
Write-Host ""
Write-Host "即将抹掉这一块盘上的全部数据：" -ForegroundColor Red
Write-Host ("   #{0}  {1}  {2}  ({3})" -f $disk.Number, $disk.FriendlyName,
            (Format-Size $disk.Size), "$($disk.BusType)/$($disk.MediaType)") -ForegroundColor Red
$confirm = Read-Host "确认请输入 ERASE（大小写均可，其它任何输入都会取消）"
if (-not $confirm -or $confirm.Trim() -notin @('ERASE', 'erase')) {
    Fail "已取消，没有写入任何东西。"
}

# ── 5. 写盘 ────────────────────────────────────────────────────────────
Write-Step "写入"

try { Set-Disk -Number $number -IsReadOnly $false -ErrorAction SilentlyContinue } catch { }
Write-Host "   清除分区表与卷…"
try {
    Clear-Disk -Number $number -RemoveData -RemoveOEM -Confirm:$false -ErrorAction Stop
} catch {
    Fail ("清盘失败：$($_.Exception.Message)" +
          " 常见原因：盘被别的程序占用（资源管理器窗口、杀毒软件、另一个刷写工具）。关掉后重跑。")
}
Update-HostStorageCache

$devPath = "\\.\PhysicalDrive$number"
$device  = $null
try {
    $device = [IO.File]::Open($devPath, [IO.FileMode]::Open,
                               [IO.FileAccess]::Write, [IO.FileShare]::None)
} catch {
    Fail "打不开 $devPath：$($_.Exception.Message)"
}

$in  = [IO.File]::OpenRead($ImagePath)
$gz  = New-Object IO.Compression.GZipStream($in, [IO.Compression.CompressionMode]::Decompress)
$buf = New-Object byte[] (4 * 1MB)
$sha = [Security.Cryptography.IncrementalHash]::CreateHash(
           [Security.Cryptography.HashAlgorithmName]::SHA256)
$total = [int64]0
$clock = [DateTime]::Now
$start = [DateTime]::Now

try {
    while (($n = $gz.Read($buf, 0, $buf.Length)) -gt 0) {
        $device.Write($buf, 0, $n)
        $sha.AppendData($buf, 0, $n)
        $total += $n
        if (([DateTime]::Now - $clock).TotalMilliseconds -ge 500) {
            $pct = 0
            if ($imageBytes -gt 0) { $pct = [int][Math]::Min(100, (100.0 * $total / $imageBytes)) }
            Write-Progress -Activity "写入镜像" -Status (
                "{0} / {1}  ({2:N1} MB/s)" -f (Format-Size $total), (Format-Size $imageBytes),
                (($total / 1MB) / [Math]::Max(0.001, ([DateTime]::Now - $start).TotalSeconds))
            ) -PercentComplete $pct
            $clock = [DateTime]::Now
        }
    }
    $device.Flush($true)
} catch {
    Fail "写入中断：$($_.Exception.Message)。这块盘现在是不完整状态，重新跑一次即可（会先清盘）。"
} finally {
    $device.Dispose(); $gz.Dispose(); $in.Dispose()
    Write-Progress -Activity "写入镜像" -Completed
}

$writeHash = [BitConverter]::ToString($sha.GetHashAndReset()).Replace('-', '').ToLower()
$seconds = ((Get-Date) - $start).TotalSeconds
Write-Ok ("写入完成：{0}，用时 {1:N0} 秒" -f (Format-Size $total), $seconds)
Write-Ok "写入内容 SHA-256：$writeHash"

if ($total -ne $imageBytes) {
    Write-Bad "写入字节数（$total）与镜像长度（$imageBytes）不一致。"
}

# ── 6. 回读校验 ────────────────────────────────────────────────────────
Write-Step "回读校验（这一步会真的从 U 盘读回来比对）"
Update-HostStorageCache
Start-Sleep -Seconds 2

$read = $null
try {
    $read = [IO.File]::Open($devPath, [IO.FileMode]::Open,
                            [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
} catch {
    Fail "写完了，但打不开盘做回读校验：$($_.Exception.Message)。请手工用另一台机器比对，或重新刷一次。"
}

$buf2 = New-Object byte[] (4 * 1MB)
$sha2 = [Security.Cryptography.IncrementalHash]::CreateHash(
            [Security.Cryptography.HashAlgorithmName]::SHA256)
$got  = [int64]0
$clock = [DateTime]::Now
$start = [DateTime]::Now
try {
    while ($got -lt $total) {
        $want = [int][Math]::Min([int64]$buf2.Length, ($total - $got))
        $n = $read.Read($buf2, 0, $want)
        if ($n -le 0) { break }
        $sha2.AppendData($buf2, 0, $n)
        $got += $n
        if (([DateTime]::Now - $clock).TotalMilliseconds -ge 500) {
            Write-Progress -Activity "回读校验" -Status (
                "{0} / {1}" -f (Format-Size $got), (Format-Size $total)
            ) -PercentComplete ([int][Math]::Min(100, (100.0 * $got / [Math]::Max(1, $total))))
            $clock = [DateTime]::Now
        }
    }
} finally {
    $read.Dispose()
    Write-Progress -Activity "回读校验" -Completed
}

$readHash = [BitConverter]::ToString($sha2.GetHashAndReset()).Replace('-', '').ToLower()

if ($got -ne $total) {
    Write-Bad "只读回 $got 字节，期望 $total —— 这块盘装不下或中途掉线。"
}
if ($readHash -ne $writeHash) {
    Write-Bad "写入 $writeHash"
    Write-Bad "回读 $readHash"
    Write-Host ""
    Fail ("回读内容与写入内容不一致。最常见的两个原因：① 这块 U 盘是扩容盘（标称容量是假的）；" +
          "② 盘本身有坏块。换一根盘重刷；不要把这张盘拿去刷盒子。")
}
Write-Ok "回读一致：盘上这份 == 镜像那份"

# ── 7. 收尾 ────────────────────────────────────────────────────────────
if ($KeepImage -eq 0) {
    try { Remove-Item -LiteralPath $ImagePath -Force; Write-Ok "已删除本地镜像" }
    catch { Write-Bad "删除本地镜像失败（$($_.Exception.Message)），可手工删。" }
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host " 刷机盘做好了：磁盘 #$number（$($disk.FriendlyName)）" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Write-Host " 下一步（在盒子上，不在电脑上）："
Write-Host "   1. 盒子断电。"
Write-Host "   2. 插上这张 U 盘。"
Write-Host "   3. 用回形针按住 AV 孔里的复位键不放，然后通电，继续按住约 10 秒再松手。"
Write-Host "   4. 接 HDMI 能看到滚动的内核日志 = 进了 U 盘系统。"
Write-Host "   5. 起来后按 docs/W103D_USER_GUIDE.md 走（固化 eMMC 用 armbian-install）。"
Write-Host ""
Write-Host " 出厂口令（预置版镜像）：网页 abcd!1234   root/mrrc abcd1234" -ForegroundColor Yellow
Write-Host " 第一次开机后请立刻改掉这三个口令。" -ForegroundColor Yellow
Write-Host ""
exit 0
