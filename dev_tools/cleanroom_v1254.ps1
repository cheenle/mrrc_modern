# v1.25.4 洁净室真跑（Windows）—— 第 4 层，唯一能抓到「包是坏的」的那一层。
#
# 用【打包出来的】冻结 Launcher，在【全新的用户状态】+【空闲端口】下起一次。
#
# 【第一版栽的坑，记在这里】只设环境变量不管用：**配置文件优先于 os.environ**。首次运行
# 会从模板 windows\default.env 生成 %LOCALAPPDATA%\MRRC-Modern\mrrc_modern.env，里面写着
# 0.0.0.0:8888 —— 于是 MRRC_WEB_PORT=18896 完全没生效，服务器绑到了 8888 上，而 curl 去
# 18896 当然连不上。所以这里**先把配置写出来**，环境变量只是第二道。
#
# 判据全部对应真实缺陷，不是形式检查：
#   https /login 200        —— 证书模块在包里却没被 import、NameError 被宽 except 吞掉，
#                              就会静默退回纯 HTTP，启动器打开 https:// 是黑屏
#   同端口明文必须失败       —— 反过来的那一面：明文请求能拿到任何 HTTP 码就说明它在裸听
#   /api/health 401         —— 未认证的接口不能被放开
#   SAN 含 127.0.0.1        —— 浏览器校验看 SAN 不看 CN；签给 0.0.0.0 就会不匹配
#   只绑 127.0.0.1          —— 现场配置就是显式 host，默认 :: 那条路全是绿的
#   应用目录 163→163        —— "装了新包还是登不上" = 配置被写进了安装目录
#   第二实例退出非零        —— Windows 的 SO_REUSEADDR 允许两个进程绑同一端口
#   日志行数 ≈ distinct     —— root logger 是进程级的，模块被当 __main__ 和 server 各加载一次就写两遍
$ErrorActionPreference = 'Continue'
$app  = 'C:\mrrc_modern\dist\windows\MRRC-Modern'
$tmp  = 'C:\tmp\cleanroom_v1254'
$port = 18896
$fail = New-Object System.Collections.ArrayList

function Check($name, $cond, $detail) {
    if ($cond) { Write-Host ('PASS  ' + $name.PadRight(30) + ' ' + $detail) }
    else       { Write-Host ('FAIL  ' + $name.PadRight(30) + ' ' + $detail); [void]$script:fail.Add($name) }
}

Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path "$tmp\certs", "$tmp\appdata\MRRC-Modern" -Force | Out-Null

# 配置先落地 —— 它比环境变量优先级高，这是本脚本第一版失手的真正原因。
$cfgPath = "$tmp\appdata\MRRC-Modern\mrrc_modern.env"
@(
  'MRRC_RADIO_MODEL=ft710',
  'MRRC_SERIAL_PORT=COM1',
  'MRRC_BAUD_RATE=38400',
  'MRRC_WEB_HOST=127.0.0.1',
  "MRRC_WEB_PORT=$port",
  "MRRC_SSL_CERT=$tmp\certs\fullchain.pem",
  "MRRC_SSL_KEY=$tmp\certs\localhost.key"
) | Set-Content -Path $cfgPath -Encoding ASCII

$snap      = (Get-ChildItem $app -Recurse -File | ForEach-Object { $_.FullName + '|' + $_.Length + '|' + $_.LastWriteTimeUtc.Ticks } | Sort-Object) -join "`n"
$snapCount = (Get-ChildItem $app -Recurse -File).Count
Write-Host ("== 洁净室 v1.25.4 ==  应用目录快照 " + $snapCount + " 个文件，端口 " + $port)

$env:LOCALAPPDATA     = "$tmp\appdata"
$env:MRRC_CONFIG_FILE = $cfgPath
$env:MRRC_WEB_HOST    = '127.0.0.1'
$env:MRRC_WEB_PORT    = "$port"

$launcher = Join-Path $app 'MRRC-Modern-Launcher.exe'
$p = Start-Process $launcher -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput "$tmp\out.log" -RedirectStandardError "$tmp\err.log"
Write-Host ('   冻结 Launcher pid=' + $p.Id)

$code = ''
$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 3
    $code = (& curl.exe -sk -o NUL -w '%{http_code}' "https://127.0.0.1:$port/login" 2>$null)
    if ("$code" -eq '200') { break }
}
Check 'https /login 200' ("$code" -eq '200') ("code=$code")

# 明文必须连不上：拿到任何 HTTP 码都说明这个端口在裸听
$plain = (& curl.exe -s -o NUL -w '%{http_code}' "http://127.0.0.1:$port/login" 2>$null)
Check 'plaintext cannot connect' ("$plain" -eq '000') ("code='$plain'")

$health = (& curl.exe -sk -o NUL -w '%{http_code}' "https://127.0.0.1:$port/api/health" 2>$null)
Check '/api/health 401' ("$health" -eq '401') ("code=$health")

$proto = 0; $cert = $null
try {
    $tcp = New-Object System.Net.Sockets.TcpClient('127.0.0.1', $port)
    $cb  = [System.Net.Security.RemoteCertificateValidationCallback] { param($a,$b,$c,$d) $true }
    $ssl = New-Object System.Net.Security.SslStream($tcp.GetStream(), $false, $cb)
    $ssl.AuthenticateAsClient('127.0.0.1')
    $proto = [int]$ssl.SslProtocol
    $cert  = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($ssl.RemoteCertificate)
    $ssl.Close(); $tcp.Close()
} catch { Write-Host ('   TLS 握手异常: ' + $_.Exception.Message) }
Check 'TLS >= 1.2' ($proto -ge 3072) ("raw=$proto")

$san = ''
if ($cert) {
    foreach ($e in $cert.Extensions) { if ($e.Oid.Value -eq '2.5.29.17') { $san = $e.Format($false) } }
}
Check 'SAN contains 127.0.0.1' ($san -like '*127.0.0.1*') ($san)

# 证书落在【用户数据目录】，不是安装目录 —— 这一条才是"写在哪"的真正判据。
# 不要去找 "signed a self-signed certificate" 那行：走启动器这条路时证书是【启动器】
# 先签好的（windows/launcher.py:293 ssl_bootstrap.ensure_self_signed(user_data_dir()/certs)），
# 服务器拿到的是一对已存在的文件，因此它打的是 "SSL enabled" 而不是现签现报。
$pem = @(Get-ChildItem "$tmp\appdata" -Recurse -File -Include *.crt,*.key -ErrorAction SilentlyContinue)
Check 'cert in user data dir' ($pem.Count -ge 2) ('files=' + (($pem | ForEach-Object { $_.Name }) -join ','))
$pemInApp = @(Get-ChildItem $app -Recurse -File -Include *.crt,*.key -ErrorAction SilentlyContinue)
Check 'no cert in install dir' ($pemInApp.Count -eq 0) ('found=' + $pemInApp.Count)

$ns = ((netstat -ano | Select-String ":$port\s") | Select-String 'LISTENING') -join ' / '
Check 'bound to 127.0.0.1 only' (($ns -like "*127.0.0.1:$port*") -and ($ns -notlike "*0.0.0.0:$port*")) $ns

$log = ((Get-Content "$tmp\out.log","$tmp\err.log" -ErrorAction SilentlyContinue) -join "`n")
Check 'log: SSL enabled' ($log -like '*SSL enabled*') ''
Check 'log: https in banner' ($log -like "*https://127.0.0.1:$port*") ''

$lines = @(Get-Content "$tmp\out.log","$tmp\err.log" -ErrorAction SilentlyContinue)
$distinct = ($lines | Sort-Object -Unique).Count
Check 'log 行数 == distinct' ($distinct -ge [int]($lines.Count * 0.95)) ("lines=$($lines.Count) distinct=$distinct")

$cfg = Get-ChildItem "$tmp\appdata" -Recurse -Filter 'mrrc_modern.env' -ErrorAction SilentlyContinue | Select-Object -First 1
Check 'config in user dir' ($null -ne $cfg) $(if ($cfg) { $cfg.FullName } else { '' })
if ($cfg) {
    $t = Get-Content $cfg.FullName -Raw
    $m = [regex]::Match($t, 'MRRC_WEB_PASSWORD=(\S+)')
    Check 'password present' ($m.Success -and $m.Groups[1].Value.Length -eq 22) ("len=" + $m.Groups[1].Value.Length)
}

# 第二实例：同步跑，这才拿得到真实退出码。Start-Process -PassThru 的 .ExitCode
# 在 PS 5.1 上会读到 null（对象没带退出码缓存），上一版就卡在这：exited=True 而 code 空。
#
# **退出码是有意设计成 0 的**（windows/launcher.py:344 "Already running …" → return 0）：
# 它把已有服务器在浏览器里打开，属于"你要的结果达成了"，不是错误。要判的是它**没有**
# 起第二个服务器 —— "第二实例退出非零"那条旧断言对应的是更早一版启动器，早就不成立了。
$log2 = (& $launcher 2>&1 | Out-String)
$rc = $LASTEXITCODE
Check 'second exits' ($null -ne $rc) ("code=$rc")
Check 'second names the port' ($log2 -like "*$port*") ''
Check 'second did not serve' ($log2 -notlike '*Server ready*') ''
$nsAfter = @((netstat -ano | Select-String ":$port\s") | Select-String 'LISTENING')
Check 'still exactly one listener' ($nsAfter.Count -eq 1) ("listeners=" + $nsAfter.Count)

Get-Process MRRC-Modern*,scope_pipe* -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -like "$app*" } | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 4

$snapAfter = (Get-ChildItem $app -Recurse -File | ForEach-Object { $_.FullName + '|' + $_.Length + '|' + $_.LastWriteTimeUtc.Ticks } | Sort-Object) -join "`n"
Check 'app dir untouched' ($snapAfter -eq $snap) ("count=$((Get-ChildItem $app -Recurse -File).Count) was $snapCount")
Check 'no env written to install dir' (-not (Test-Path "$app\mrrc_modern.env")) ''

Write-Host ''
Write-Host '---- out.log 末尾 ----'
Get-Content "$tmp\out.log" -Tail 10 -ErrorAction SilentlyContinue

Write-Host ''
if ($fail.Count -gt 0) {
    Write-Host ("CLEANROOM FAILED (" + $fail.Count + "): " + ($fail -join ' | '))
    exit 1
}
Write-Host 'CLEANROOM ALL PASSED'
