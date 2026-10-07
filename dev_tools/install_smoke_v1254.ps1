# v1.25.4 装完真跑（Windows）—— 拿【真正的安装包】静默装进隔离 /DIR=，再起一次。
#
# 与洁净室的区别：洁净室跑的是构建产物目录（dist\windows\MRRC-Modern），这里跑的是
# **用户拿到手的那份 Setup.exe** 装出来的东西 —— 中间多了 Inno 的压缩/解压、[Run] 段、
# 快捷方式与卸载器。构建目录全绿而安装包坏掉是可能的（历史上有过 [Run] skipifsilent）。
#
# 隔离：装到 C:\tmp\install_v1254、用户状态放 C:\tmp\installrun_v1254、端口 18897。
# VM 上没有常驻实例在跑（2026-10-06 实测：无进程、无监听、无计划任务），但仍不敢碰默认端口。
$ErrorActionPreference = 'Continue'
$setup = 'C:\mrrc_modern\dist\windows\MRRC-Modern-Setup.exe'
$dir   = 'C:\tmp\install_v1254'
$tmp   = 'C:\tmp\installrun_v1254'
$port  = 18897
$fail  = New-Object System.Collections.ArrayList

function Check($name, $cond, $detail) {
    if ($cond) { Write-Host ('PASS  ' + $name.PadRight(30) + ' ' + $detail) }
    else       { Write-Host ('FAIL  ' + $name.PadRight(30) + ' ' + $detail); [void]$script:fail.Add($name) }
}

Remove-Item $dir, $tmp -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path "$tmp\appdata\MRRC-Modern" -Force | Out-Null

$cfgPath = "$tmp\appdata\MRRC-Modern\mrrc_modern.env"
@(
  'MRRC_RADIO_MODEL=ft710',
  'MRRC_SERIAL_PORT=COM1',
  'MRRC_BAUD_RATE=38400',
  'MRRC_WEB_HOST=127.0.0.1',
  "MRRC_WEB_PORT=$port"
) | Set-Content -Path $cfgPath -Encoding ASCII

Write-Host ('== 装完真跑 v1.25.4 ==  安装到 ' + $dir + '，端口 ' + $port)

$ip = Start-Process $setup -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/DIR=$dir" -PassThru -Wait
Check 'installer exit 0' ($ip.ExitCode -eq 0) ("code=" + $ip.ExitCode)

# 安装器 [Run] 段可能顺手把应用拉起来（历史上 skipifsilent 就是这么漏的）——先清掉再自己起
Get-Process MRRC-Modern*, scope_pipe* -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -like "$dir*" } | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3

$files = @(Get-ChildItem $dir -Recurse -File -ErrorAction SilentlyContinue)
Check 'installed file count' ($files.Count -ge 163) ("count=" + $files.Count)
$vt = "$dir\version.txt"
Check 'version.txt = 1.25.4' ((Test-Path $vt) -and ((Get-Content $vt -Raw).Trim() -eq '1.25.4')) $(if (Test-Path $vt) { (Get-Content $vt -Raw).Trim() } else { 'missing' })
Check 'launcher+server installed' ((Test-Path "$dir\MRRC-Modern-Launcher.exe") -and (Test-Path "$dir\MRRC-Modern-Server.exe")) ''
Check 'fleet at app root' ((Get-ChildItem "$dir\fleet" -Recurse -File -ErrorAction SilentlyContinue).Count -ge 13) ''
Check 'uninstaller present' (Test-Path "$dir\unins000.exe") ''

$snap = (Get-ChildItem $dir -Recurse -File | ForEach-Object { $_.FullName + '|' + $_.Length + '|' + $_.LastWriteTimeUtc.Ticks } | Sort-Object) -join "`n"
$snapCount = $files.Count

$env:LOCALAPPDATA     = "$tmp\appdata"
$env:MRRC_CONFIG_FILE = $cfgPath
$env:MRRC_WEB_HOST    = '127.0.0.1'
$env:MRRC_WEB_PORT    = "$port"

$p = Start-Process "$dir\MRRC-Modern-Launcher.exe" -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput "$tmp\out.log" -RedirectStandardError "$tmp\err.log"
$code = ''
$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 3
    $code = (& curl.exe -sk -o NUL -w '%{http_code}' "https://127.0.0.1:$port/login" 2>$null)
    if ("$code" -eq '200') { break }
}
Check 'installed app https /login 200' ("$code" -eq '200') ("code=$code")

$health = (& curl.exe -sk -o NUL -w '%{http_code}' "https://127.0.0.1:$port/api/health" 2>$null)
Check 'installed app /api/health 401' ("$health" -eq '401') ("code=$health")

$plain = (& curl.exe -s -o NUL -w '%{http_code}' "http://127.0.0.1:$port/login" 2>$null)
Check 'installed app no plaintext' ("$plain" -eq '000') ("code='$plain'")

Get-Process MRRC-Modern*, scope_pipe* -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -like "$dir*" } | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 4

$snapAfter = (Get-ChildItem $dir -Recurse -File | ForEach-Object { $_.FullName + '|' + $_.Length + '|' + $_.LastWriteTimeUtc.Ticks } | Sort-Object) -join "`n"
Check 'install dir untouched' ($snapAfter -eq $snap) ("count=$((Get-ChildItem $dir -Recurse -File).Count) was $snapCount")
Check 'no env written beside exe' (-not (Test-Path "$dir\mrrc_modern.env")) ''

# 卸载要干净
$up = Start-Process "$dir\unins000.exe" -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART' -PassThru -Wait
Start-Sleep -Seconds 5
$left = @(Get-ChildItem $dir -Recurse -File -ErrorAction SilentlyContinue)
Check 'uninstall leaves nothing' ($left.Count -eq 0) ("left=" + $left.Count)

Write-Host ''
if ($fail.Count -gt 0) {
    Write-Host ("INSTALL SMOKE FAILED (" + $fail.Count + "): " + ($fail -join ' | '))
    exit 1
}
Write-Host 'INSTALL SMOKE ALL PASSED'
