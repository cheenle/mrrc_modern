<#
    MRRC Modern - 本机证书信任体检 / 修复（"接入云端"报 CERTIFICATE_VERIFY_FAILED 时用）

    为什么有这个东西（2026-10-16 现场）
    ----------------------------------
    应用访问门户走的是固定一条路径：Python -> ssl.create_default_context() -> OpenSSL。
    **在 Windows 上，那条路径只认本机证书库**（CPython 的 load_default_certs 读 Windows 的
    ROOT/CA 存储），随包的 vendor/ca/cacert.pem 只在"库为空"时才会被启用（net_tls.py 里
    `store_size() > 0` 的提前返回）。于是本机库里只要有一份**与被服务链条同名同密钥、却已过期**
    的证书（Windows 会把见过的中间证书缓存进 CA 存储；Let's Encrypt 的中间证书会换代重签，
    缓存那份到期后就是这个形态），OpenSSL 就会把它放进链里，判
    `certificate verify failed: certificate has expired`；而浏览器（Chromium/Edge）用自己那套
    根集合与链构建，同一台机器、同一张证书照样能开。现场表现因此是"浏览器能上、应用不能"。

    这个脚本只做三件事：
      1. 读事实：时钟/时区/校时、代理环境、DNS 与 TCP、应用日志里那条报错原文、
         门户实际下发的证书链（逐张 subject/有效期/指纹）、本机四张证书库里 LE/ISRG 家族清单；
      2. 复现判定：把本机证书库导出成 PEM，用同一族的 openssl 对"门户下发的那条链"做
         `openssl verify` —— 给出的就是应用那条路径的判词（OK / certificate has expired /
         unable to get local issuer certificate），并指出是哪一张、在哪个库；
      3. 只有加了 -Fix 才动手：把**能证明就是它**的过期证书（去掉它判定就转 OK）备份后从库里删掉；
         若删完变成"找不到签发者"，再问你是否导入当前的两张 Let's Encrypt 根。
         删除前一律备份到报告目录，并打印恢复命令。

    安全边界：
      * 不带 -Fix 时**完全只读**（只往自己的报告目录里写）。
      * 只动证书库里的证书：不碰配置、不碰应用目录、不碰代理设置、不碰注册表其它键。
      * 每条改动都要按 Y（除非同时给了 -Yes），改完立刻重跑判定并把结果写进报告。
      * 无法证明是它导致的证书，只报告不删除。

    用法（在装了 MRRC Modern 的这台 Windows 机器上）：
        .\MRRC-CertDoctor.ps1                  # 只体检，出报告
        .\MRRC-CertDoctor.ps1 -Fix             # 体检 +（逐项确认后）修复
        .\MRRC-CertDoctor.ps1 -Fix -Yes        # 不逐项询问（给支持人员用）
        .\MRRC-CertDoctor.ps1 -TargetHost portal.mrrc.vlsc.net

    退出码：0 = 本机信任库对这条链健康；10 = 判定为坏（含已修好）；
            20 = 体检没做完（缺 openssl / 网络不通）—— 报告照样会写出来。

    报告：桌面 MRRC-CertDoctor-<时间戳>\report.txt，同时打一个同名 .zip 方便发回支持。
#>
[CmdletBinding()]
param(
    [string]$TargetHost = "portal.mrrc.vlsc.net",
    [int]$TargetPort = 443,
    # 指定 openssl.exe（默认：脚本旁边的 -> 应用自带的 -> PATH 上的）
    [string]$OpenSSL = "",
    # 测试钩子：跳过网络抓取，直接对这一份 PEM（第一张叶证书，其余为链）判定
    [string]$ChainFile = "",
    # 只有给了它才允许改本机证书库
    [switch]$Fix,
    # 不逐项询问
    [switch]$Yes,
    [string]$WorkDir = ""
)

$ErrorActionPreference = "Continue"
# 中文要能落进报告：PowerShell 5.1 的控制台输出编码默认不是 UTF-8。
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }

$Script:Lines = New-Object System.Collections.Generic.List[string]
$Script:ExitCode = 0
function Say([string]$m) { $Script:Lines.Add($m) | Out-Null; Write-Host $m }
function Log([string]$m) { $Script:Lines.Add($m) | Out-Null }
function Bang([string]$m) { $Script:Lines.Add($m) | Out-Null; Write-Host $m -ForegroundColor Yellow }
function Bad([string]$m) { $Script:Lines.Add($m) | Out-Null; Write-Host $m -ForegroundColor Red }

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
if (-not $WorkDir) { $WorkDir = Join-Path ([Environment]::GetFolderPath("Desktop")) "MRRC-CertDoctor-$stamp" }
$ReportPath = Join-Path $WorkDir "report.txt"
$BackupDir = Join-Path $WorkDir "removed-certs"
$ChainDir = Join-Path $WorkDir "chain"

# 只在这台机器"缺少有效根"时才会用到的两张根（Windows 的自动根证书更新本该送来它们）。
# 内容与随包的 vendor/ca/cacert.pem 一致。
$RootPems = @{
    "ISRG Root X1" = "-----BEGIN CERTIFICATE-----
MIIFazCCA1OgAwIBAgIRAIIQz7DSQONZRGPgu2OCiwAwDQYJKoZIhvcNAQELBQAw
TzELMAkGA1UEBhMCVVMxKTAnBgNVBAoTIEludGVybmV0IFNlY3VyaXR5IFJlc2Vh
cmNoIEdyb3VwMRUwEwYDVQQDEwxJU1JHIFJvb3QgWDEwHhcNMTUwNjA0MTEwNDM4
WhcNMzUwNjA0MTEwNDM4WjBPMQswCQYDVQQGEwJVUzEpMCcGA1UEChMgSW50ZXJu
ZXQgU2VjdXJpdHkgUmVzZWFyY2ggR3JvdXAxFTATBgNVBAMTDElTUkcgUm9vdCBY
MTCCAiIwDQYJKoZIhvcNAQEBBQADggIPADCCAgoCggIBAK3oJHP0FDfzm54rVygc
h77ct984kIxuPOZXoHj3dcKi/vVqbvYATyjb3miGbESTtrFj/RQSa78f0uoxmyF+
0TM8ukj13Xnfs7j/EvEhmkvBioZxaUpmZmyPfjxwv60pIgbz5MDmgK7iS4+3mX6U
A5/TR5d8mUgjU+g4rk8Kb4Mu0UlXjIB0ttov0DiNewNwIRt18jA8+o+u3dpjq+sW
T8KOEUt+zwvo/7V3LvSye0rgTBIlDHCNAymg4VMk7BPZ7hm/ELNKjD+Jo2FR3qyH
B5T0Y3HsLuJvW5iB4YlcNHlsdu87kGJ55tukmi8mxdAQ4Q7e2RCOFvu396j3x+UC
B5iPNgiV5+I3lg02dZ77DnKxHZu8A/lJBdiB3QW0KtZB6awBdpUKD9jf1b0SHzUv
KBds0pjBqAlkd25HN7rOrFleaJ1/ctaJxQZBKT5ZPt0m9STJEadao0xAH0ahmbWn
OlFuhjuefXKnEgV4We0+UXgVCwOPjdAvBbI+e0ocS3MFEvzG6uBQE3xDk3SzynTn
jh8BCNAw1FtxNrQHusEwMFxIt4I7mKZ9YIqioymCzLq9gwQbooMDQaHWBfEbwrbw
qHyGO0aoSCqI3Haadr8faqU9GY/rOPNk3sgrDQoo//fb4hVC1CLQJ13hef4Y53CI
rU7m2Ys6xt0nUW7/vGT1M0NPAgMBAAGjQjBAMA4GA1UdDwEB/wQEAwIBBjAPBgNV
HRMBAf8EBTADAQH/MB0GA1UdDgQWBBR5tFnme7bl5AFzgAiIyBpY9umbbjANBgkq
hkiG9w0BAQsFAAOCAgEAVR9YqbyyqFDQDLHYGmkgJykIrGF1XIpu+ILlaS/V9lZL
ubhzEFnTIZd+50xx+7LSYK05qAvqFyFWhfFQDlnrzuBZ6brJFe+GnY+EgPbk6ZGQ
3BebYhtF8GaV0nxvwuo77x/Py9auJ/GpsMiu/X1+mvoiBOv/2X/qkSsisRcOj/KK
NFtY2PwByVS5uCbMiogziUwthDyC3+6WVwW6LLv3xLfHTjuCvjHIInNzktHCgKQ5
ORAzI4JMPJ+GslWYHb4phowim57iaztXOoJwTdwJx4nLCgdNbOhdjsnvzqvHu7Ur
TkXWStAmzOVyyghqpZXjFaH3pO3JLF+l+/+sKAIuvtd7u+Nxe5AW0wdeRlN8NwdC
jNPElpzVmbUq4JUagEiuTDkHzsxHpFKVK7q4+63SM1N95R1NbdWhscdCb+ZAJzVc
oyi3B43njTOQ5yOf+1CceWxG1bQVs5ZufpsMljq4Ui0/1lvh+wjChP4kqKOJ2qxq
4RgqsahDYVvTH9w7jXbyLeiNdd8XM2w9U/t7y0Ff/9yi0GE44Za4rF2LN9d11TPA
mRGunUHBcnWEvgJBQl9nJEiU0Zsnvgc/ubhPgXRR4Xq37Z0j4r7g1SgEEzwxA57d
emyPxgcYxn/eR44/KJ4EBs+lVDR3veyJm+kXQ99b21/+jh5Xos1AnX5iItreGCc=
-----END CERTIFICATE-----"
    "ISRG Root X2" = "-----BEGIN CERTIFICATE-----
MIICGzCCAaGgAwIBAgIQQdKd0XLq7qeAwSxs6S+HUjAKBggqhkjOPQQDAzBPMQsw
CQYDVQQGEwJVUzEpMCcGA1UEChMgSW50ZXJuZXQgU2VjdXJpdHkgUmVzZWFyY2gg
R3JvdXAxFTATBgNVBAMTDElTUkcgUm9vdCBYMjAeFw0yMDA5MDQwMDAwMDBaFw00
MDA5MTcxNjAwMDBaME8xCzAJBgNVBAYTAlVTMSkwJwYDVQQKEyBJbnRlcm5ldCBT
ZWN1cml0eSBSZXNlYXJjaCBHcm91cDEVMBMGA1UEAxMMSVNSRyBSb290IFgyMHYw
EAYHKoZIzj0CAQYFK4EEACIDYgAEzZvVn4CDCuwJSvMWSj5cz3es3mcFDR0HttwW
+1qLFNvicWDEukWVEYmO6gbf9yoWHKS5xcUy4APgHoIYOIvXRdgKam7mAHf7AlF9
ItgKbppbd9/w+kHsOdx1ymgHDB/qo0IwQDAOBgNVHQ8BAf8EBAMCAQYwDwYDVR0T
AQH/BAUwAwEB/zAdBgNVHQ4EFgQUfEKWrt5LSDv6kviejM9ti6lyN5UwCgYIKoZI
zj0EAwMDaAAwZQIwe3lORlCEwkSHRhtFcP9Ymd70/aTSVaYgLXTWNLxBo1BfASdW
tL4ndQavEi51mI38AjEAi/V3bNTIZargCyzuFJ0nN6T5U6VR5CmD1/iQMVtCnwr1
/q4AaOeMSQ+2b1tbFvAC
-----END CERTIFICATE-----"
}

# ---------------------------------------------------------------- 小工具

function Have-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

# 跑一个原生命令并拿回 stdout/stderr/退出码。用 .NET 的 Process 而不是管道，有两处实测理由：
# ① s_client 需要 stdin 的 EOF（PowerShell 5.1 没有 `< NUL` 重定向），这里把 stdin 直接关掉；
# ② 路径经过 .NET 字符串传参，含中文的用户名/解压目录不会被 cmd 的代码页毁掉（写成 .cmd 会）。
function Invoke-Captured {
    param([string]$Exe, [string[]]$ArgList, [int]$TimeoutMs = 45000)
    $res = [pscustomobject]@{ ExitCode = -1; StdOut = ""; StdErr = ""; TimedOut = $false }
    $quoted = @($ArgList | ForEach-Object { if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ } })
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Exe
    $psi.Arguments = ($quoted -join ' ')
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    try {
        $p = [System.Diagnostics.Process]::Start($psi)
        $outTask = $p.StandardOutput.ReadToEndAsync()
        $errTask = $p.StandardError.ReadToEndAsync()
        $p.StandardInput.Close()                       # 立刻给 s_client 一个 EOF
        if (-not $p.WaitForExit($TimeoutMs)) {
            $res.TimedOut = $true
            try { $p.Kill() } catch { }
        } else {
            $res.ExitCode = $p.ExitCode
        }
        $res.StdOut = $outTask.Result
        $res.StdErr = $errTask.Result
    } catch {
        $res.StdErr = "start failed: $($_.Exception.Message)"
    } finally {
        if ($p) { try { $p.Dispose() } catch { } }
    }
    return $res
}

function Pem-Of([System.Security.Cryptography.X509Certificates.X509Certificate2]$Cert) {
    $b64 = [Convert]::ToBase64String($Cert.RawData, 'InsertLineBreaks')
    "-----BEGIN CERTIFICATE-----`n$b64`n-----END CERTIFICATE-----`n"
}

function Find-OpenSSL {
    $cands = New-Object System.Collections.Generic.List[string]
    if ($OpenSSL) { $cands.Add($OpenSSL) }
    if ($PSScriptRoot) { $cands.Add((Join-Path $PSScriptRoot "openssl.exe")) }        # 解压出来的包里自带
    $cands.Add((Join-Path ${env:ProgramFiles} "MRRC Modern\fleet\openssl.exe"))       # 应用自带
    $cands.Add((Join-Path ${env:ProgramFiles(x86)} "MRRC Modern\fleet\openssl.exe"))
    $cands.Add((Join-Path $env:LOCALAPPDATA "MRRC-Modern\fleet\openssl.exe"))
    foreach ($c in $cands) { if ($c -and (Test-Path $c)) { return $c } }
    $onPath = Get-Command openssl.exe -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    return ""
}

# 四张库全读：Windows 的链构建会用 CurrentUser 与 LocalMachine 的 ROOT（根）与 CA（中间证书）。
function Get-StoreCert {
    $specs = @(
        @{ Name = "CurrentUser\Root"; Store = "Root"; Location = "CurrentUser" },
        @{ Name = "LocalMachine\Root"; Store = "Root"; Location = "LocalMachine" },
        @{ Name = "CurrentUser\CA"; Store = "CA"; Location = "CurrentUser" },
        @{ Name = "LocalMachine\CA"; Store = "CA"; Location = "LocalMachine" }
    )
    $all = New-Object System.Collections.Generic.List[object]
    foreach ($s in $specs) {
        try {
            $store = New-Object System.Security.Cryptography.X509Certificates.X509Store($s.Store, $s.Location)
            $store.Open("ReadOnly")
            foreach ($c in $store.Certificates) {
                $all.Add([pscustomobject]@{
                    Id = ($s.Name + "|" + $c.Thumbprint)
                    Where = $s.Name; Store = $s.Store; Location = $s.Location
                    Cert = $c; Subject = $c.Subject; Issuer = $c.Issuer
                    NotBefore = $c.NotBefore.ToUniversalTime(); NotAfter = $c.NotAfter.ToUniversalTime()
                    Thumbprint = $c.Thumbprint
                })
            }
            $store.Close()
        } catch {
            Log "  (读 $($s.Name) 失败：$($_.Exception.Message))"
        }
    }
    return $all
}

function Is-FamilyCert([string]$s) {
    # 与 Let's Encrypt 相关的家族（现在的 ISRG/YE 与老的 DST 交叉签名都算）。
    # 刻意不写 "LE Root"：实测 "Symantec Enterprise Mobi*le Root* for Microsoft" 会被它误命中。
    return ($s -match "(?i)ISRG|\bLet's Encrypt\b|CN=YE|DST Root CA")
}

# 判词分类
function Classify([string]$Text, [int]$ExitCode) {
    if ($ExitCode -eq 0 -and $Text -match '(?m)OK\s*$' -and $Text -notmatch 'error \d+ at') { return "ok" }
    if ($Text -match 'certificate has expired') { return "expired" }
    if ($Text -match 'unable to get local issuer certificate') { return "no-issuer" }
    if ($Text -match 'unable to get issuer certificate') { return "no-intermediate" }
    if ($Text -match 'self-signed certificate') { return "self-signed" }
    if ($Text -match 'not yet valid') { return "not-yet-valid" }
    if ($Text -match 'hostname mismatch|does not match') { return "name-mismatch" }
    return "other"
}

function Test-ChainVerify {
    param([string]$Ossl, [string]$StorePem, [string]$LeafPem, [string]$UntrustedPem)
    if (-not (Test-Path $StorePem) -or ((Get-Content $StorePem -Raw -ErrorAction SilentlyContinue) -notmatch 'BEGIN CERTIFICATE')) {
        return [pscustomobject]@{ Ok = $false; Why = "no-store"; Text = "(本机这类存储里一张证书都没有)" }
    }
    $argList = @("verify", "-show_chain", "-CAfile", $StorePem)
    if ($UntrustedPem -and (Test-Path $UntrustedPem)) { $argList += @("-untrusted", $UntrustedPem) }
    $argList += $LeafPem
    $r = Invoke-Captured -Exe $Ossl -ArgList $argList -TimeoutMs 30000
    $text = ("" + $r.StdOut + "`n" + $r.StdErr).Trim()
    $why = Classify -Text $text -ExitCode $r.ExitCode
    return [pscustomobject]@{ Ok = ($why -eq "ok"); Why = $why; Text = $text }
}

function Split-ChainPem([string]$PemText, [string]$LeafPath, [string]$UntrustedPath) {
    $blocks = [regex]::Matches($PemText, "(?s)-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----")
    if ($blocks.Count -eq 0) { return 0 }
    Set-Content -Path $LeafPath -Value ($blocks[0].Value + "`n") -Encoding ascii
    if ($blocks.Count -gt 1) {
        $rest = ($blocks | Select-Object -Skip 1 | ForEach-Object { $_.Value + "`n" }) -join ""
        Set-Content -Path $UntrustedPath -Value $rest -Encoding ascii
    } else {
        Remove-Item $UntrustedPath -Force -ErrorAction SilentlyContinue
    }
    return $blocks.Count
}

# ---------------------------------------------------------------- 体检项

function Show-ClockAndTime {
    $now = Get-Date
    Say ("  本机时间   : {0}   时区 {1}" -f $now.ToString("yyyy-MM-dd HH:mm:ss"), (Get-TimeZone).Id)
    $w32 = Invoke-Captured -Exe (Join-Path $env:SystemRoot "System32\w32tm.exe") -ArgList @("/query", "/status") -TimeoutMs 15000
    foreach ($line in ($w32.StdOut -split "`r?`n")) {
        if ($line -match '^\s*(Source|Last Successful Sync Time)') { Say ("  校时       : " + $line.Trim()) }
    }
    # 独立对照：门户的 HTTP Date 头。Schannel 走这条路（浏览器同族），Python 那条路径坏了它也照样通。
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri "https://$TargetHost/" -Method Head -TimeoutSec 20
        $h = $r.Headers["Date"]
        if ($h) {
            $server = [DateTime]::Parse($h, [Globalization.CultureInfo]::InvariantCulture,
                                        [Globalization.DateTimeStyles]::AdjustToUniversal)
            $drift = [Math]::Round(($now.ToUniversalTime() - $server).TotalSeconds)
            Say ("  与服务器差 : $drift 秒")
            if ([Math]::Abs($drift) -gt 300) {
                Bang "  ** 时钟差了 $drift 秒。证书有效期判的就是这个时钟：请先在 设置->时间和语言 里打开「自动设置时间」，再重试。"
            }
        }
    } catch {
        Log "  (取服务器 Date 头失败：$($_.Exception.Message))"
    }
}

function Show-ProxyFacts {
    $names = @("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "NO_PROXY", "no_proxy")
    $found = $false
    foreach ($scope in @("User", "Machine")) {
        foreach ($n in $names) {
            $v = [Environment]::GetEnvironmentVariable($n, $scope)
            if ($v) { Say "  代理环境   : [$scope] $n = $v"; $found = $true }
        }
    }
    try {
        $k = Get-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings" -ErrorAction Stop
        if ($k.ProxyEnable -eq 1) { Say ("  IE 代理    : 启用 -> " + $k.ProxyServer); $found = $true }
    } catch { }
    if (-not $found) { Say "  代理环境   : 无（系统级）" }
    else { Bang "  ** 上面这些代理：Python(urllib) 会走，浏览器不一定走。只要代理做了 HTTPS 解密(MITM) 或它的根证书过期，就会正好只坑应用这一侧。" }
}

function Show-NetworkFacts {
    $ips = @()
    try {
        $ips = [System.Net.Dns]::GetHostAddresses($TargetHost) | ForEach-Object { $_.IPAddressToString }
        Say ("  DNS        : $TargetHost -> " + ($ips -join ", "))
    } catch {
        Bang "  ** DNS 解析失败：$($_.Exception.Message)"
    }
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect($TargetHost, $TargetPort, $null, $null)
        if ($iar.AsyncWaitHandle.WaitOne(8000) -and $client.Connected) { Say "  TCP        : $TargetPort 通" }
        else { Bang "  ** TCP $TargetPort 不通 —— 连不上就谈不上证书，先解决网络/防火墙。" }
    } catch {
        Bang "  ** TCP $TargetPort 异常：$($_.Exception.Message)"
    } finally { try { $client.Close() } catch { } }
}

function Show-AppLogLines {
    $dirs = @((Join-Path $env:LOCALAPPDATA "MRRC-Modern\logs"), (Join-Path $env:APPDATA "MRRC-Modern\logs"))
    $hit = 0
    foreach ($d in $dirs) {
        if (-not (Test-Path $d)) { continue }
        foreach ($f in (Get-ChildItem $d -File -ErrorAction SilentlyContinue |
                        Where-Object { $_.Name -like "*.log*" } |
                        Sort-Object LastWriteTime -Descending | Select-Object -First 3)) {
            # 只认"校验失败"这一类；写成 "SSL: " 会把日志器名 mrrc.ssl: 的每一行都捞进来（实测）。
            $lines = Get-Content $f.FullName -Tail 500 -ErrorAction SilentlyContinue |
                     Where-Object { $_ -match "(?i)CERTIFICATE_VERIFY_FAILED|certificate verify failed|SSLCertVerificationError|certificate has expired|unable to get (local )?issuer certificate|\[SSL: " }
            foreach ($l in $lines) { Say ("     [$($f.Name)] " + $l.Trim()); $hit++ }
        }
    }
    if ($hit -eq 0) { Say "  应用日志   : 没找到与证书校验相关的行（可能已轮转）" }
}

function Get-ServedChain {
    param([string]$Ossl)
    $leaf = Join-Path $ChainDir "leaf.pem"
    $unt = Join-Path $ChainDir "intermediates.pem"
    $raw = Join-Path $ChainDir "s_client.txt"
    if ($ChainFile) {
        $text = Get-Content $ChainFile -Raw
        Set-Content -Path $raw -Value $text -Encoding ascii
    } else {
        $r = Invoke-Captured -Exe $Ossl -TimeoutMs 40000 -ArgList @(
            "s_client", "-connect", ($TargetHost + ":" + $TargetPort), "-servername", $TargetHost,
            "-showcerts", "-verify_return_error")
        $text = ("" + $r.StdOut + "`n" + $r.StdErr)
        Set-Content -Path $raw -Value $text -Encoding utf8
    }
    $count = Split-ChainPem -PemText $text -LeafPath $leaf -UntrustedPath $unt
    return [pscustomobject]@{ Leaf = $leaf; Untrusted = $unt; Raw = $raw; Count = $count; Text = $text }
}

function Show-CertPeek {
    param([string]$Ossl, [string]$Pem, [string]$Label)
    $r = Invoke-Captured -Exe $Ossl -TimeoutMs 15000 -ArgList @(
        "x509", "-in", $Pem, "-noout", "-subject", "-issuer", "-dates", "-fingerprint", "-sha256")
    Say "  [$Label]"
    foreach ($line in ($r.StdOut -split "`r?`n")) { if ($line.Trim()) { Say ("    " + $line.Trim()) } }
}

function Confirm([string]$Question) {
    if ($Yes) { Say "  $Question -> 已按 -Yes 自动确认"; return $true }
    $a = Read-Host "  $Question [y/N]"
    return ($a -eq "y" -or $a -eq "Y" -or $a -eq "yes" -or $a -eq "YES")
}

# ---------------------------------------------------------------- 修复动作

function Remove-StoreCert {
    param($Entry, [string]$BackupDir)
    if ($Entry.Location -eq "LocalMachine" -and -not (Have-Admin)) {
        Bad "  ** 需要管理员才能改 $($Entry.Where)：请右键用管理员身份重开本脚本。"
        return $false
    }
    $pem = Join-Path $BackupDir (($Entry.Where -replace '\\', '_') + "-" + $Entry.Thumbprint + ".pem")
    Set-Content -Path $pem -Value (Pem-Of $Entry.Cert) -Encoding ascii
    try {
        $store = New-Object System.Security.Cryptography.X509Certificates.X509Store($Entry.Store, $Entry.Location)
        $store.Open("ReadWrite")
        $store.Remove($Entry.Cert)
        $store.Close()
        Say "  已删除：$($Entry.Where)  $($Entry.Thumbprint)"
        Say "          备份 -> $pem"
        Log  "  恢复：certutil -f -addstore $($Entry.Store) `"$pem`"   (LocalMachine 需管理员)"
        return $true
    } catch {
        Bad "  ** 删除失败（$($Entry.Where) $($Entry.Thumbprint)）：$($_.Exception.Message)"
        return $false
    }
}

function Import-Root {
    param([string]$Ossl, [string]$Name, [string]$PemText, [string]$BackupDir)
    $safe = ($Name -replace '[^A-Za-z0-9]', '-')
    $pemFile = Join-Path $BackupDir ("added-" + $safe + ".pem")
    $der = Join-Path $BackupDir ("added-" + $safe + ".der")
    Set-Content -Path $pemFile -Value $PemText -Encoding ascii
    $r = Invoke-Captured -Exe $Ossl -TimeoutMs 15000 -ArgList @("x509", "-in", $pemFile, "-outform", "DER", "-out", $der)
    if (-not (Test-Path $der)) { Bad "  ** 读不出这张根证书：$($r.StdErr)"; return $false }
    $certutil = Join-Path $env:SystemRoot "System32\certutil.exe"
    # 先装"当前用户"：够用（应用的 Python 读的就是当前用户+本机两套），且不需要管理员。
    # 用 certutil -f 而不是 .NET 的 Store.Add：后者会弹一个系统同意对话框，在非交互
    # 会话里直接失败（实测），而 -f 是确定的，还能拿退出码。
    $ok = $false
    $scopeArgs = @("-user", "-f", "-addstore", "Root", $der)
    $out = Invoke-Captured -Exe $certutil -ArgList $scopeArgs -TimeoutMs 30000
    Log ("  certutil -user -f -addstore Root：" + ($out.StdOut + $out.StdErr).Trim())
    if ($out.ExitCode -eq 0) { $ok = $true; Say ("  已导入（当前用户）: {0}" -f $Name) }
    elseif (Have-Admin) {
        $out2 = Invoke-Captured -Exe $certutil -ArgList @("-f", "-addstore", "Root", $der) -TimeoutMs 30000
        Log ("  certutil -f -addstore Root：" + ($out2.StdOut + $out2.StdErr).Trim())
        if ($out2.ExitCode -eq 0) { $ok = $true; Say ("  已导入（所有用户）: {0}" -f $Name) }
        else { Bad ("  ** 导入 {0} 失败：{1}" -f $Name, ($out2.StdOut + $out2.StdErr).Trim()) }
    } else {
        Bad ("  ** 导入 {0} 失败：{1}" -f $Name, ($out.StdOut + $out.StdErr).Trim())
    }
    try {
        $der2 = Get-Content $der -Encoding Byte -ReadCount 0
        $c = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2([byte[]]$der2)
        Say ("    指纹 {0}   有效期至 {1}" -f $c.Thumbprint, $c.NotAfter.ToUniversalTime().ToString("yyyy-MM-dd"))
        Log ("  撤销导入：certutil -user -f -delstore Root " + $c.Thumbprint)
    } catch { }
    return $ok
}

function Write-StorePem {
    param($Certs, [string]$Path, [string]$OnlyStore = "")
    $sb = New-Object System.Text.StringBuilder
    foreach ($c in $Certs) {
        if ($OnlyStore -and $c.Store -ne $OnlyStore) { continue }
        [void]$sb.Append((Pem-Of $c.Cert))
    }
    Set-Content -Path $Path -Value $sb.ToString() -Encoding ascii
    return $Path
}

# ---------------------------------------------------------------- main

function Main {
    Say "=============================================================="
    Say " MRRC Modern - 本机证书信任体检"
    Say " （「接入云端」/ 软件更新 / 诊断包上传 走的是同一条 HTTPS 路径）"
    Say "=============================================================="
    Say "  时间戳     : $stamp"
    Say "  体检对象   : $TargetHost`:$TargetPort"
    Say "  报告目录   : $WorkDir"
    if ($Fix) { Say "  模式       : 体检 + 修复（逐项确认、先备份）" } else { Say "  模式       : 只读体检（不改任何东西）" }
    Say ("  管理员     : " + $(if (Have-Admin) { "是" } else { "否（修复机器级证书库时要）" }))
    Say ""

    New-Item -ItemType Directory -Path $WorkDir, $BackupDir, $ChainDir -Force | Out-Null

    $os = Find-OpenSSL
    if (-not $os) {
        Bad "找不到 openssl.exe：请把下载的压缩包**整个解压**后再运行（包里自带一个），或确认 MRRC Modern 装在默认目录。"
        $Script:ExitCode = 20
        Break-Out
        return
    }
    Say "  openssl    : $os"
    $ver = Invoke-Captured -Exe $os -ArgList @("version") -TimeoutMs 10000
    Say ("  openssl    : " + ($ver.StdOut -replace "`r?`n", " ").Trim())
    Say ""

    Say "[1/6] 时钟 / 时区 / 校时"
    Show-ClockAndTime
    Say ""
    Say "[2/6] 代理设置"
    Show-ProxyFacts
    Say ""
    Say "[3/6] 网络"
    Show-NetworkFacts
    Say ""
    Say "[4/6] 应用日志里的证书报错（原文，最有用的证据）"
    Show-AppLogLines
    Say ""

    Say "[5/6] 门户下发的证书链（应用收到的就是这一条）"
    $chain = Get-ServedChain -Ossl $os
    if ($chain.Count -eq 0) {
        Bang "  ** 没抓到证书链（网络不通或抓取超时）。报告照样写出来，请把它发给我们。"
        $Script:ExitCode = 20
        Break-Out
        return
    }
    Say ("  链上共 {0} 张：" -f $chain.Count)
    Show-CertPeek -Ossl $os -Pem $chain.Leaf -Label "leaf"
    if (Test-Path $chain.Untrusted) { Show-CertPeek -Ossl $os -Pem $chain.Untrusted -Label "intermediates" }
    Say ""

    Say "[6/6] 本机证书库 vs 这条链（应用的判词）"
    $certs = @(Get-StoreCert)
    Say ("  四张库合计 {0} 张证书（CurrentUser/LocalMachine 的 Root 与 CA）" -f $certs.Count)
    $family = @($certs | Where-Object { (Is-FamilyCert $_.Subject) -or (Is-FamilyCert $_.Issuer) })
    Say ("  其中 Let's Encrypt / ISRG / DST 家族 {0} 张：" -f $family.Count)
    $nowUtc = (Get-Date).ToUniversalTime()
    foreach ($e in ($family | Sort-Object NotAfter)) {
        $flag = ""
        if ($e.NotAfter -lt $nowUtc) { $flag = "   <== 已过期" }
        Say ("    {0,-18} {1,-52} 至 {2:yyyy-MM-dd}{3}" -f $e.Where, $e.Subject, $e.NotAfter, $flag)
    }

    if ($certs.Count -eq 0) {
        Bad "  ** 四张证书库一张都读不到（权限或库本身有问题）：这就是「找不到签发者」的直接原因。"
    }
    $storeAll = Write-StorePem -Certs $certs -Path (Join-Path $ChainDir "store-all.pem")
    $storeRoot = Write-StorePem -Certs $certs -Path (Join-Path $ChainDir "store-root.pem") -OnlyStore "Root"

    $vAll = Test-ChainVerify -Ossl $os -StorePem $storeAll -LeafPem $chain.Leaf -UntrustedPem $chain.Untrusted
    $vRoot = Test-ChainVerify -Ossl $os -StorePem $storeRoot -LeafPem $chain.Leaf -UntrustedPem $chain.Untrusted
    Say ""
    Say ("  判定（ROOT + CA，Windows 上应用真正会加载的那一堆）: {0}" -f $vAll.Why)
    foreach ($l in ($vAll.Text -split "`r?`n")) { if ($l.Trim()) { Say ("      " + $l.Trim()) } }
    Say ("  判定（只算 ROOT）                                  : {0}" -f $vRoot.Why)
    foreach ($l in ($vRoot.Text -split "`r?`n")) { if ($l.Trim()) { Say ("      " + $l.Trim()) } }

    if ($vAll.Why -eq "ok") {
        Say ""
        Say "  => 本机信任库对这条链是**健康**的。"
        Say "     如果应用仍然报同样的错，请把这个报告发给我们 —— 那说明问题不在证书库，"
        Say "     而在代理/MITM 或应用版本上（报告里已经有代理与日志两项事实）。"
        $Script:ExitCode = 0
        Break-Out
        return
    }

    Say ""
    Say ("  => 判定为**坏**：{0}" -f $vAll.Why)
    if ($vAll.Why -eq "expired") {
        Say "     这正是「库里有一份与被服务链条同名同密钥、但已过期的证书」的形态："
        Say "     OpenSSL 会把库里那份放进链里，于是判它过期；浏览器不吃这套，所以浏览器能开。"
    } elseif ($vAll.Why -eq "no-issuer") {
        Say "     本机没有任何一张能当这条链锚点的**有效**根证书（自动根证书更新被关过、或从没同步上）。"
    }

    if (-not $Fix) {
        Say ""
        Say "  加 -Fix 可以让本脚本把它修掉：先备份、再删/导入，最后重跑上面的判定。"
        $Script:ExitCode = 10
        Break-Out
        return
    }

    # ---- 修复 1：找出"去掉它就好了"的过期证书，逐张确认后删除
    #
    # 每删一张都必须**重新读一遍库**再判定：store-all.pem 是快照，拿旧快照复验会把
    # "其实已经修好"误判成仍然坏（2026-10-16 在构建 VM 上就是这样误报的），
    # 而且会对着已经删掉的证书再删第二次。
    $expired = @($certs | Where-Object { $_.NotAfter -lt $nowUtc })
    Say ""
    Say ("  修复 1/2：在 {0} 张已过期证书里找罪魁（判据：去掉它，上面那条判定就转 OK）" -f $expired.Count)
    $verdictNow = $vAll.Why
    $removed = 0
    foreach ($e in $expired) {
        if ($verdictNow -eq "ok") { break }
        $sb = New-Object System.Text.StringBuilder
        foreach ($o in $certs) { if ($o.Id -ne $e.Id) { [void]$sb.Append((Pem-Of $o.Cert)) } }
        $probe = Join-Path $ChainDir ("probe-without-" + $e.Thumbprint + ".pem")
        Set-Content -Path $probe -Value $sb.ToString() -Encoding ascii
        $v = Test-ChainVerify -Ossl $os -StorePem $probe -LeafPem $chain.Leaf -UntrustedPem $chain.Untrusted
        if ($v.Ok) {
            Say "  找到罪魁：去掉它就通过 -> $($e.Where)  $($e.Subject)  (至 $($e.NotAfter.ToString('yyyy-MM-dd')))"
            if (Confirm "删除这**一张**证书？（会先备份到报告目录）") {
                if (Remove-StoreCert -Entry $e -BackupDir $BackupDir) { $removed++ }
            }
            $certs = @(Get-StoreCert)
            $storeAll = Write-StorePem -Certs $certs -Path (Join-Path $ChainDir "store-all.pem")
            $verdictNow = (Test-ChainVerify -Ossl $os -StorePem $storeAll -LeafPem $chain.Leaf -UntrustedPem $chain.Untrusted).Why
        }
    }

    if ($verdictNow -ne "ok" -and $verdictNow -ne "no-issuer" -and $verdictNow -ne "self-signed") {
        # 单张不够：把家族里已过期的整批处理掉（Windows 上最常见的陈旧残留形态）
        $famExpired = @($certs | Where-Object { (Is-FamilyCert $_.Subject) -or (Is-FamilyCert $_.Issuer) } |
                                  Where-Object { $_.NotAfter -lt $nowUtc })
        if ($famExpired.Count -gt 0) {
            Say ("  单张不够。家族里已过期的共 {0} 张。" -f $famExpired.Count)
            if (Confirm "把这批已过期的 Let's Encrypt / ISRG / DST 证书全部删除？（逐张备份）") {
                foreach ($e in $famExpired) { if (Remove-StoreCert -Entry $e -BackupDir $BackupDir) { $removed++ } }
                $certs = @(Get-StoreCert)
                $storeAll = Write-StorePem -Certs $certs -Path (Join-Path $ChainDir "store-all.pem")
                $verdictNow = (Test-ChainVerify -Ossl $os -StorePem $storeAll -LeafPem $chain.Leaf -UntrustedPem $chain.Untrusted).Why
            }
        }
        if ($verdictNow -eq "expired") {
            $expiredNow = @($certs | Where-Object { $_.NotAfter -lt $nowUtc })
            if ($expiredNow.Count -gt 0) {
                Say ("  仍有其它已过期证书在起作用（{0} 张）。已过期的证书不参与任何正常校验，删掉是安全的（备份都在报告目录）。" -f $expiredNow.Count)
                if (Confirm ("删除全部 {0} 张已过期证书？" -f $expiredNow.Count)) {
                    foreach ($e in $expiredNow) { if (Remove-StoreCert -Entry $e -BackupDir $BackupDir) { $removed++ } }
                    $certs = @(Get-StoreCert)
                    $storeAll = Write-StorePem -Certs $certs -Path (Join-Path $ChainDir "store-all.pem")
                    $verdictNow = (Test-ChainVerify -Ossl $os -StorePem $storeAll -LeafPem $chain.Leaf -UntrustedPem $chain.Untrusted).Why
                }
            }
        }
    }
    Say ("  修复 1 之后：{0}" -f $verdictNow)

    # ---- 修复 2：删完变成"找不到签发者" => 缺的是有效根，问过再导入
    if ($verdictNow -eq "no-issuer" -or $verdictNow -eq "self-signed") {
        Say ""
        Say "  修复 2/2：本机缺当前的有效根。要导入的是 Let's Encrypt 现在在用的两张："
        foreach ($k in @("ISRG Root X1", "ISRG Root X2")) { Say ("    - " + $k) }
        Say "  （Windows 的自动根证书更新本该送来它们；这台机器上显然没有/没更新。）"
        if (Confirm "把这两张根导入本机（所有用户）的受信任根存储？") {
            foreach ($k in @("ISRG Root X1", "ISRG Root X2")) {
                Import-Root -Ossl $os -Name $k -PemText $RootPems[$k] -BackupDir $BackupDir | Out-Null
            }
            $certs = @(Get-StoreCert)
            $storeAfter = Write-StorePem -Certs $certs -Path (Join-Path $ChainDir "store-after.pem")
            $verdictNow = (Test-ChainVerify -Ossl $os -StorePem $storeAfter -LeafPem $chain.Leaf -UntrustedPem $chain.Untrusted).Why
            Say ("  修复 2 之后：{0}" -f $verdictNow)
        }
    }

    Say ""
    if ($verdictNow -eq "no-intermediate") {
        Say ""
        Say "  判定是「链上缺一张中间证书」—— 门户下发的链不完整，或本机把该中间证书删掉了。"
        Say "  这不在本机证书库能修好的范围里：请把报告发给我们（报告里有链上每一张证书与库里清单）。"
    }
    if ($verdictNow -eq "ok") {
        Say ("  => 修好了（共删除 {0} 张）。接下来：" -f $removed)
        Say "     1) 从托盘退出 MRRC Modern，再启动它；"
        Say "     2) 浏览器里点 ☰ -> 接入云端 (Cloud Hub) -> 呼号 + 你手上那条一次性口令 -> 申请入口/接入；"
        Say "     3) 若仍失败，把 report.txt（或整个 zip）发给我们。"
        $Script:ExitCode = 10
    } else {
        Bad ("  仍未通过（{0}）。这不是你操作的问题 —— 请把报告发给我们，我们按报告里的证书清单继续判断。" -f $verdictNow)
        $Script:ExitCode = 10
    }
    Break-Out
}

function Break-Out {
    $Script:Lines.Add("") | Out-Null
    $Script:Lines.Add("==============================================================") | Out-Null
    $Script:Lines.Add(" 把 report.txt（或整个 zip）发给支持即可。") | Out-Null
    $Script:Lines.Add(" 备份的证书在 removed-certs\；恢复命令写在报告里，可随时装回。") | Out-Null
    $Script:Lines.Add("==============================================================") | Out-Null
    try { $Script:Lines | Out-File -FilePath $ReportPath -Encoding utf8 } catch { }
    try {
        $zip = "$WorkDir.zip"
        if (Test-Path $zip) { Remove-Item $zip -Force }
        Compress-Archive -Path (Join-Path $WorkDir "*") -DestinationPath $zip -Force -ErrorAction Stop
        Write-Host ""
        Write-Host "  报告：$ReportPath"
        Write-Host "  打包：$zip   （把这个 zip 发给我们）"
    } catch {
        Write-Host ""
        Write-Host "  报告：$ReportPath   （自动打包失败：$($_.Exception.Message)，把这个文件夹发给我们也行）"
    }
    Write-Host ""
    if (-not $Yes) { Read-Host "  按回车关闭" | Out-Null }
}

# 被 dot-source 时只加载函数（供测试/复用），不执行体检。
if ($MyInvocation.InvocationName -ne ".") { Main; exit $Script:ExitCode }
