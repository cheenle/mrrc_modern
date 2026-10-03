<#
    MRRC Modern - finish the Cloud Hub connection from the command line.

    Why this exists: the settings dialog only offered the enrolment-secret box *before* an application
    was submitted. Anyone who had already clicked Apply - which is what a user does first - was left
    with a Refresh button and no way to paste the one-time secret the operator sends. This script does
    the same thing the dialog cannot: it claims the approved entry with that secret, and restarts the
    app so the new certificate is actually served.

    Usage (on the machine running MRRC Modern, in an elevated PowerShell):

        .\MRRC-Modern-Connect.ps1 -Callsign BG7ZHS -Secret KLvbhKRLPlVqOQUPhj_25WzpHVzu0EaU

    Read the entry it prints; it looks like https://bg7zhs.mrrc.vlsc.net/ - no port, no path.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Callsign,
    [Parameter(Mandatory = $true)][string]$Secret,
    [int]$Port = 8888
)

$ErrorActionPreference = "Continue"
$curl = Join-Path $env:SystemRoot "System32\curl.exe"
if (-not (Test-Path $curl)) { $curl = "curl.exe" }

function Say($t) { Write-Host $t }
function Fail($t) { Write-Host "  ERROR: $t" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------- find the config
$candidates = @(
    (Join-Path $env:LOCALAPPDATA "MRRC-Modern\mrrc_modern.env"),
    (Join-Path $env:APPDATA "MRRC-Modern\mrrc_modern.env"),
    "C:\Program Files\MRRC Modern\windows\mrrc_modern.env"
)
$cfg = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $cfg) { Fail "could not find mrrc_modern.env - is MRRC Modern installed for this user?" }
Say "  config: $cfg"

$password = ""
foreach ($line in Get-Content $cfg) {
    if ($line -match "^MRRC_WEB_PASSWORD=(.*)$") { $password = $Matches[1].Trim() }
}
if (-not $password) { Fail "no MRRC_WEB_PASSWORD in the config - start the app once, then retry" }

# ---------------------------------------------------------------- is the app up?
$health = & $curl -sk -o NUL -w "%{http_code}" --max-time 8 "https://127.0.0.1:$Port/api/health"
if ($health -eq "000") {
    Fail "nothing answers on https://127.0.0.1:$Port - start MRRC Modern (or MRRC Modern Server) first"
}
Say "  app is up (HTTP $health on port $Port)"

# ---------------------------------------------------------------- log in
$json = '{"password":"' + $password + '"}'
Set-Content -Path "$env:TEMP\mrrc-pw.json" -Value $json -Encoding ascii
$jar = "$env:TEMP\mrrc-cookies.txt"
Remove-Item $jar -Force -ErrorAction SilentlyContinue
& $curl -sk -c $jar -o NUL -X POST "https://127.0.0.1:$Port/api/auth/login" `
    -H "Content-Type: application/json" --data-binary "@$env:TEMP\mrrc-pw.json"
if ($LASTEXITCODE -ne 0) { Fail "login request failed" }

# ---------------------------------------------------------------- claim (apply with the secret)
$callsignUpper = $Callsign.Trim().ToUpper()
$body = '{"callsign":"' + $callsignUpper + '","contact":"cli-connect","secret":"' + $Secret.Trim() + '"}'
Set-Content -Path "$env:TEMP\mrrc-claim.json" -Value $body -Encoding ascii
Say "  claiming $callsignUpper ..."
$reply = & $curl -sk -b $jar -X POST "https://127.0.0.1:$Port/api/cloud/apply" `
    -H "Content-Type: application/json" --data-binary "@$env:TEMP\mrrc-claim.json" --max-time 60
Say "  reply: $reply"

# ---------------------------------------------------------------- state + restart
Start-Sleep -Seconds 3
$state = & $curl -sk -b $jar "https://127.0.0.1:$Port/api/cloud/state" --max-time 30
Say "  state: $state"

if ($state -match '"connected":true') {
    if ($state -match '"cert_reload_required":true') {
        Say "  restarting so the new certificate is served ..."
        & $curl -sk -b $jar -X POST "https://127.0.0.1:$Port/api/cloud/restart" -o NUL --max-time 20 | Out-Null
    } else {
        Say "  (no restart needed)"
    }
    $entry = ""
    if ($state -match '"entry":"([^"]+)"') { $entry = $Matches[1] }
    if (-not $entry) { $entry = "https://$callsignUpper.ToLower().mrrc.vlsc.net/" }
    Say ""
    Say "  CONNECTED. Your entry is:"
    Say "      $entry"
    Say "  (no port, no path - open it in a browser; the certificate warning appears once)"
} else {
    Say ""
    Say "  Not connected yet. Send this output to support. Common causes:"
    Say "    - the secret is wrong or already used"
    Say "    - the operator has not approved the application yet"
    Say "    - this machine cannot reach https://portal.mrrc.vlsc.net/ (check in a browser)"
    exit 1
}
