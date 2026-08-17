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
Invoke-Checked python -m unittest discover -s tests -v

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
}

if (Get-Command iscc -ErrorAction SilentlyContinue) {
    Invoke-Checked iscc packaging\windows\MRRC-Modern.iss
} else {
    Write-Warning "Inno Setup Compiler 'iscc' was not found. Install Inno Setup and rerun this script to create the setup EXE."
}

Write-Host "Assembled app: $AppRoot"
Write-Host "Installer output: $(Join-Path $DistRoot 'MRRC-Modern-Setup.exe')"
