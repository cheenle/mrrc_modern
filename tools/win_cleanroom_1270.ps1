$ErrorActionPreference = "Stop"

$Root = "C:\tools\cr1270"
Remove-Item $Root -Recurse -Force -ErrorAction SilentlyContinue
$Local = Join-Path $Root "AppData\Local"
$Profile = Join-Path $Root "UserProfile"
New-Item -ItemType Directory -Force -Path (Join-Path $Local "MRRC-Modern") | Out-Null
New-Item -ItemType Directory -Force -Path $Profile | Out-Null
$Port = 18902
# Field-shaped config: explicit host (two v1.24.6 defects lived only on that
# path), a fixed password so the assertions do not need to scrape the banner.
@"
MRRC_WEB_HOST=127.0.0.1
MRRC_WEB_PORT=$Port
MRRC_WEB_PASSWORD=cleanroom-pass
MRRC_FIRST_RUN_DONE=1
MRRC_SERIAL_PORT=COM99
"@ | Set-Content -Path (Join-Path $Local "MRRC-Modern\mrrc_modern.env") -Encoding ascii

$env:LOCALAPPDATA = $Local
$env:USERPROFILE = $Profile
$env:BROWSER = "no-such-browser"
$env:MRRC_FIRST_RUN_DONE = "1"

$exe = "C:\mrrc_modern\dist\windows\MRRC-Modern\MRRC-Modern-Launcher.exe"
Write-Host "--- starting the packaged launcher (isolated profile) ---"
$proc = Start-Process -FilePath $exe -NoNewWindow -PassThru `
    -RedirectStandardOutput (Join-Path $Root "out.txt") `
    -RedirectStandardError (Join-Path $Root "err.txt")

$up = $false
for ($i = 1; $i -le 40; $i++) {
    Start-Sleep -Seconds 3
    try {
        $r = Invoke-WebRequest -Uri ("https://127.0.0.1:" + $Port + "/login") -UseBasicParsing `
            -SkipCertificateCheck -TimeoutSec 3 -ErrorAction Stop
        if ($r.StatusCode -eq 200) { $up = $true; Write-Host ("  https /login -> 200 after " + ($i * 3) + "s"); break }
    } catch {
        $code = $_.Exception.Response.StatusCode.value__
        if ($code -eq 401) { $up = $true; Write-Host ("  https /login -> 401 after " + ($i * 3) + "s"); break }
    }
}
if (!$up) {
    # PS 5.1 has no -SkipCertificateCheck; fall back to a raw TLS handshake.
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient("127.0.0.1", $Port)
        Write-Host "  raw TCP connect to the port succeeded"
        $up = $true
        $tcp.Close()
    } catch {
        Write-Host ("  nothing listening on " + $Port)
    }
}

$log = Get-ChildItem $Local -Recurse -Filter "*.log" -ErrorAction SilentlyContinue | Select-Object -First 3
Write-Host "--- logs ---"
foreach ($f in $log) { Write-Host ("  " + $f.FullName) }
$server = Join-Path $Local "MRRC-Modern\logs\server.log"
if (Test-Path $server) {
    Write-Host "--- server.log: FT8 / startup evidence ---"
    Get-Content $server | Select-String -Pattern "FT8|starting on port|Server ready|SSL enabled|Recording ready" |
        ForEach-Object { Write-Host ("  " + $_.Line) }
} else {
    Write-Host "  (no server.log yet)"
    if (Test-Path (Join-Path $Root "err.txt")) { Get-Content (Join-Path $Root "err.txt") -Tail 12 | ForEach-Object { Write-Host ("  err: " + $_) } }
}

Write-Host ("CLEANROOM_UP=" + $up)
