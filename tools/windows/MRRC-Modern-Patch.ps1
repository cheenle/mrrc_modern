<#
    MRRC Modern - apply a static patch without downloading the whole installer.

    What a patch can change: the files the app serves and reads at runtime that are NOT compiled in -
    the web UI (static/**), assets and default configuration templates. What it cannot change: the
    Python logic, which PyInstaller freezes into the executable; that needs a full installer.

    The patch is a zip laid out as it will be copied into the install directory, plus a manifest with
    the version it applies to and its SHA-256. This script verifies both, stops the app, backs up
    every file it replaces, copies the new ones in, clears the "pending" marker and restarts.

    Usage (elevated PowerShell):
        .\MRRC-Modern-Patch.ps1 -PatchUrl https://www.vlsc.net/mrrc_modern/downloads/patches/static-1.24.7-p1.zip
        .\MRRC-Modern-Patch.ps1 -PatchUrl <url> -DryRun     # show what would change
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PatchUrl,
    [string]$InstallDir = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Continue"
function Say($t) { Write-Host $t }
function Fail($t) { Write-Host "  ERROR: $t" -ForegroundColor Red; exit 1 }

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail "run this from an elevated PowerShell (it writes under Program Files)"
}

# ---------------------------------------------------------------- locate the install
if (-not $InstallDir) {
    foreach ($rp in @("HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*",
                      "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*")) {
        Get-ItemProperty $rp -ErrorAction SilentlyContinue |
            Where-Object { $_.DisplayName -like "*MRRC Modern*" -and $_.InstallLocation } |
            ForEach-Object { if (-not $InstallDir) { $InstallDir = $_.InstallLocation } }
    }
}
if (-not $InstallDir -or -not (Test-Path $InstallDir)) { $InstallDir = "C:\Program Files\MRRC Modern" }
if (-not (Test-Path (Join-Path $InstallDir "version.txt"))) { Fail "MRRC Modern not found at $InstallDir" }
$installed = (Get-Content (Join-Path $InstallDir "version.txt") -Raw).Trim()
$marker = Join-Path $InstallDir "patch-pending.txt"
Say "  install : $InstallDir"
Say "  version : $installed"

# ---------------------------------------------------------------- fetch manifest + patch
$base = $PatchUrl.Substring(0, $PatchUrl.LastIndexOf("/") + 1)
$name = $PatchUrl.Substring($PatchUrl.LastIndexOf("/") + 1)
$manifestUrl = "$base" + "patch.json"
$tmp = Join-Path $env:TEMP ("mrrc-patch-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$zip = Join-Path $tmp $name

Say "  fetching $PatchUrl"
try {
    Invoke-WebRequest -UseBasicParsing -Uri $PatchUrl -OutFile $zip -TimeoutSec 300
} catch { Fail "download failed: $($_.Exception.Message)" }

$sha = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
Say "  sha256  : $($sha.Substring(0,16))..."

$expected = ""
$applies = @()
try {
    $manifest = (Invoke-WebRequest -UseBasicParsing -Uri $manifestUrl -TimeoutSec 60).Content | ConvertFrom-Json
    foreach ($p in $manifest.patches) {
        if ($p.file -eq $name) { $expected = $p.sha256; $applies = $p.appliesTo }
    }
} catch { Say "  (no manifest at $manifestUrl - skipping the version check)" }

if ($expected -and ($expected.ToLower() -ne $sha)) { Fail "sha256 does not match the manifest" }
if ($applies.Count -gt 0 -and ($applies -notcontains $installed)) {
    Fail "this patch applies to version(s) $($applies -join ', '), not $installed"
}
Say "  checks  : OK"

# ---------------------------------------------------------------- look inside
$stage = Join-Path $tmp "stage"
Expand-Archive -Path $zip -DestinationPath $stage -Force
$files = Get-ChildItem $stage -Recurse -File
Say "  patch   : $($files.Count) file(s)"
foreach ($f in $files | Select-Object -First 12) {
    Say ("     " + $f.FullName.Substring($stage.Length).TrimStart("\"))
}
if ($files.Count -gt 12) { Say "     ..." }

if ($DryRun) {
    Say ""
    Say "  -DryRun: nothing was changed."
    exit 0
}

# ---------------------------------------------------------------- stop the app
foreach ($n in @("MRRC-Modern-Server", "MRRC-Modern-Launcher", "scope_pipe", "frpc")) {
    Get-Process -Name $n -ErrorAction SilentlyContinue | ForEach-Object {
        try { $_.Kill(); Say "  stopped $n (pid $($_.Id))" } catch { }
    }
}
Start-Sleep -Seconds 3

# ---------------------------------------------------------------- backup + copy
$backup = Join-Path $tmp "backup"
$copied = 0
foreach ($f in $files) {
    $rel = $f.FullName.Substring($stage.Length).TrimStart("\")
    $dst = Join-Path $InstallDir $rel
    $dstDir = Split-Path $dst -Parent
    if (-not (Test-Path $dstDir)) { New-Item -ItemType Directory -Force -Path $dstDir | Out-Null }
    if (Test-Path $dst) {
        $bdir = Join-Path $backup (Split-Path $rel -Parent)
        if ($bdir -and -not (Test-Path $bdir)) { New-Item -ItemType Directory -Force -Path $bdir | Out-Null }
        Copy-Item $dst (Join-Path $backup $rel) -Force -ErrorAction SilentlyContinue
    }
    Copy-Item $f.FullName $dst -Force
    $copied++
}
Say "  copied  : $copied file(s);  backup in $backup"
Set-Content -Path $marker -Value ("applied " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss") + " from $name") -Encoding ascii

# ---------------------------------------------------------------- restart
$launcher = Join-Path $InstallDir "MRRC-Modern-Launcher.exe"
if (Test-Path $launcher) {
    Start-Process -FilePath $launcher -WorkingDirectory $InstallDir
    Say "  restarted the app"
} else {
    Say "  start the app from the Start Menu"
}
Say ""
Say "  Patch applied. Open the web UI and reload the page (Ctrl-F5) to pick up the new files."
