$ErrorActionPreference = "Stop"

# $ErrorActionPreference does NOT apply to native commands (python, pyinstaller,
# iscc) — check $LASTEXITCODE explicitly so a failing test or build aborts the
# packaging instead of silently shipping a broken installer.
function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true, Position = 0)][string]$Command,
        [Parameter(ValueFromRemainingArguments = $true)]$Remaining
    )
    # Flatten: an array argument would otherwise arrive as ONE nested element
    # and be passed to the native command as a single space-joined string.
    $flat = @()
    foreach ($a in $Remaining) { $flat += $a }
    & $Command @flat
    if ($LASTEXITCODE -ne 0) {
        throw "$Command $($flat -join ' ') failed with exit code $LASTEXITCODE"
    }
}

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$DistRoot = Join-Path $RepoRoot "dist\windows"
$AppRoot = Join-Path $DistRoot "MRRC-Modern"
$PyInstallerRoot = Join-Path $DistRoot "_pyinstaller"

Set-Location $RepoRoot

$pyFiles = Get-ChildItem -Name *.py
Invoke-Checked python -m py_compile @pyFiles
# Tests: Start-Process with explicit redirects, NOT `python ... *> $log`.
# PowerShell 5.1 turns any native-command stderr output into a NativeCommandError,
# and `$ErrorActionPreference = "Stop"` (set above) then aborts the build — while
# unittest writes ALL of its output to stderr.  That was the "spurious errors=1"
# on the VM (2026-09-17): the suite was green, the shell was not.
$utOut = Join-Path $RepoRoot "dist\windows\unittest.stdout.txt"
$utErr = Join-Path $RepoRoot "dist\windows\unittest.stderr.txt"
New-Item -ItemType Directory -Force -Path (Split-Path $utOut) | Out-Null
$utProc = Start-Process -FilePath "python" `
    -ArgumentList "-m", "unittest", "discover", "-s", "tests", "-v" `
    -NoNewWindow -Wait -PassThru -RedirectStandardOutput $utOut -RedirectStandardError $utErr
$utExit = $utProc.ExitCode
Get-Content $utErr -Tail 6
$utBad = Select-String -Path @($utOut, $utErr) -Pattern '^(FAIL|ERROR): ' -ErrorAction SilentlyContinue
if ($utExit -ne 0 -or $utBad) {
    $utBad | Select-Object -First 5 | ForEach-Object { Write-Host $_.Line }
    throw "tests failed (exit $utExit) - logs: $utOut / $utErr"
}

$ft4222 = Join-Path $RepoRoot "vendor\ftdi\windows\bin\x64\FT4222.dll"
$d2xx = Join-Path $RepoRoot "vendor\ftdi\windows\bin\x64\ftd2xx.dll"
$opus = Join-Path $RepoRoot "vendor\opus\windows\bin\x64\opus.dll"
if (!(Test-Path $ft4222) -or !(Test-Path $d2xx)) {
    Write-Warning "FTDI DLLs are missing. The installer will build, but FT4222 true spectrum (FT-710) will fall back unless these files are added:"
    Write-Warning "  $ft4222"
    Write-Warning "  $d2xx"
}
if (!(Test-Path $opus)) {
    Write-Warning "opus.dll is missing. The installer will build, but TX/RX Opus will fall back unless this file is added:"
    Write-Warning "  $opus"
}

Invoke-Checked pyinstaller packaging\pyinstaller\scope_pipe.spec --noconfirm --distpath "$PyInstallerRoot" --workpath "build\pyinstaller"
Invoke-Checked pyinstaller packaging\pyinstaller\mrrc_modern_server.spec --noconfirm --distpath "$PyInstallerRoot" --workpath "build\pyinstaller"
Invoke-Checked pyinstaller packaging\pyinstaller\mrrc_modern_launcher.spec --noconfirm --distpath "$PyInstallerRoot" --workpath "build\pyinstaller"

if (Test-Path $AppRoot) {
    Remove-Item $AppRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $AppRoot | Out-Null

Copy-Item (Join-Path $PyInstallerRoot "MRRC-Modern-Server\*") $AppRoot -Recurse -Force
Copy-Item (Join-Path $PyInstallerRoot "scope_pipe.exe") $AppRoot -Force
Copy-Item (Join-Path $PyInstallerRoot "MRRC-Modern-Launcher.exe") $AppRoot -Force
Copy-Item (Join-Path $RepoRoot "windows") $AppRoot -Recurse -Force

# version.txt beside the exe inside the assembled app dir: $AppRoot is what the
# installer packages and what _runtime_dir() resolves to at runtime (see
# packaging/macos/build.sh for why it exists).
$appVersion = (Select-String -Path "CHANGELOG.md" -Pattern '^## \[v?([0-9]+\.[0-9]+\.[0-9]+)' |
    Select-Object -First 1).Matches[0].Groups[1].Value
Set-Content -Path (Join-Path $AppRoot "version.txt") -Value $appVersion -Encoding ascii
# Do not ship stale bytecode caches in the installer.
Remove-Item (Join-Path $AppRoot "windows\__pycache__") -Recurse -Force -ErrorAction SilentlyContinue

$VendorSource = Join-Path $RepoRoot "vendor\ftdi\windows"
if (Test-Path $VendorSource) {
    $VendorDest = Join-Path $AppRoot "vendor\ftdi\windows"
    New-Item -ItemType Directory -Path (Split-Path $VendorDest) -Force | Out-Null
    Copy-Item $VendorSource $VendorDest -Recurse -Force
}

$OpusSource = Join-Path $RepoRoot "vendor\opus\windows"
if (Test-Path $OpusSource) {
    $OpusDest = Join-Path $AppRoot "vendor\opus\windows"
    New-Item -ItemType Directory -Path (Split-Path $OpusDest) -Force | Out-Null
    Copy-Item $OpusSource $OpusDest -Recurse -Force

# Cloud Hub 接入所需的**内置件**：frpc（隧道客户端）+ openssl（签实例自签证书用）
# + 隧道安装脚本。它们由 mrrc_hub 的取件器在构建机上取好并逐个校验 SHA-256 后放在
# packaging/payload/ 下（见 mrrc_hub/deploy/fetch_installer_payload.sh）。
#
# 为什么是硬要求：实例侧的网络未必能访问 GitHub（实测国内家宽连 github.com 超时），
# 所以"让租户自己去下载 frpc"这条路不可靠；内置件缺了就装不出一个能接入的包。
# 允许用 MRRC_ALLOW_MISSING_PAYLOAD=1 显式放行（只给临时试验用）。
$PayloadSource = Join-Path $RepoRoot "packaging\payload\windows-amd64"
$FleetDest = Join-Path $AppRoot "fleet"
if (Test-Path $PayloadSource) {
    # A directory is not a payload: a package without openssl cannot sign the instance
    # certificate, one without frpc has no tunnel client, and one without the installer script
    # gives the tenant nothing to run. All three look like a successful build, so fail here.
    $need = @("frpc.exe", "install_instance_tunnel.ps1", "openssl.exe")
    $missing = @($need | Where-Object { -not (Test-Path (Join-Path $PayloadSource $_)) })
    if ($missing.Count -gt 0 -and $env:MRRC_ALLOW_MISSING_PAYLOAD -ne "1") {
        throw ("fleet payload incomplete: missing " + ($missing -join ", ") + " in " + $PayloadSource)
    }
    # Put it there. These two lines were lost when the guard above was added, which is why the
    # build failed at Get-ChildItem with "fleet does not exist": the check was rewritten and the
    # work it was checking disappeared with it.
    if (-not (Test-Path $FleetDest)) { New-Item -ItemType Directory -Path $FleetDest -Force | Out-Null }
    foreach ($f in Get-ChildItem $PayloadSource -File) { Copy-Item $f.FullName (Join-Path $FleetDest $f.Name) -Force }
    if (-not (Test-Path $FleetDest)) { throw ("fleet dir was not created: " + $FleetDest) }
    $fleetFiles = (Get-ChildItem $FleetDest -File | Select-Object -ExpandProperty Name) -join ", "
    Write-Host "Fleet payload: $fleetFiles"
} elseif ($env:MRRC_ALLOW_MISSING_PAYLOAD -eq "1") {
    Write-Warning "fleet payload missing ($PayloadSource) - building anyway because MRRC_ALLOW_MISSING_PAYLOAD=1; the installer will NOT be able to set up a tunnel offline"
} else {
    throw "fleet payload missing: $PayloadSource — run mrrc_hub/deploy/fetch_installer_payload.sh --out packaging/payload first (or set MRRC_ALLOW_MISSING_PAYLOAD=1 to build a test package without it)"
}
}

if (Get-Command iscc -ErrorAction SilentlyContinue) {
    Invoke-Checked iscc packaging\windows\MRRC-Modern.iss
} else {
    Write-Warning "Inno Setup Compiler 'iscc' was not found. Install Inno Setup and rerun this script to create the setup EXE."
}

Write-Host "Assembled app: $AppRoot"
Write-Host "Installer output: $(Join-Path $DistRoot 'MRRC-Modern-Setup.exe')"
