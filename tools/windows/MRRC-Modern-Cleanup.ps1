<#
    MRRC Modern - clean removal of the application, its configuration and its leftovers.

    Why this exists: upgrading an installation that has been through several versions leaves
    behind what the old versions wrote - a second configuration directory, MRRC_*/FT710_*
    environment variables from before they were renamed, ad-hoc scheduled tasks, a stale
    certificate and a tunnel config naming an entry that no longer exists. Those leftovers cause
    problems that look like product bugs ("the dialog sits at waiting for approval", "the entry is
    502"), and the fastest way out is to start from nothing.

    What it does, in order:
      1. backs up the configuration, certificates and logs to a folder you choose;
      2. stops MRRC Modern and its helper processes;
      3. runs the application's own uninstaller, silently;
      4. removes what the uninstaller leaves behind, from a fixed list of paths only;
      5. removes MRRC_*/FT710_* user and machine environment variables;
      6. removes scheduled tasks and shortcuts whose names start with MRRC;
      7. reports what it did and what it left alone.

    It never touches anything outside that list: no other program, no other folder, no other task.

    Usage (from an elevated PowerShell, or let it elevate itself):
        .\MRRC-Modern-Cleanup.ps1                 # asks before doing anything
        .\MRRC-Modern-Cleanup.ps1 -WhatIf        # lists what would happen, changes nothing
        .\MRRC-Modern-Cleanup.ps1 -Force         # no questions (for scripted use)

    After a cleanup, install MRRC Modern again from https://www.vlsc.net/mrrc_modern/ and use
    Settings -> Cloud Hub to connect: the instance re-enrols itself and the entry comes back.
#>

[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$WhatIf,
    # The older product (MRRC 6.x) has its own install and config directories. It is a different
    # program, not a leftover of this one, so it is reported but not touched unless asked for.
    [switch]$IncludeLegacy,
    [string]$BackupDir = ""
)

$ErrorActionPreference = "Continue"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
if (-not $BackupDir) { $BackupDir = Join-Path ([Environment]::GetFolderPath("Desktop")) "MRRC-backup-$stamp" }

# ---------------------------------------------------------------- what we are allowed to touch
# Fixed list. Nothing outside it is ever removed, so a typo in a path cannot take a user's data.
$AppDir   = Join-Path ${env:ProgramFiles} "MRRC Modern"
$LegacyAppDir = Join-Path ${env:ProgramFiles(x86)} "MRRC Modern"
#: Directories whose *contents* are skipped in the backup: recorded QSOs can be gigabytes, they are
#: not configuration, and copying them makes the backup step look frozen on a real machine.
$BackupSkip = @("recordings")

$DataDirs = @(
    (Join-Path $env:LOCALAPPDATA "MRRC-Modern"),   # current: config, certs, logs, fleet, recordings
    (Join-Path $env:APPDATA      "MRRC-Modern"),
    (Join-Path $env:LOCALAPPDATA "MRRC"),          # the name used before the Modern rename
    (Join-Path $env:APPDATA      "MRRC"),
    (Join-Path $env:USERPROFILE  ".mrrc-modern"),
    (Join-Path $env:USERPROFILE  "MRRC-Modern")
)
$ProcessNames = @("MRRC-Modern-Launcher", "MRRC-Modern-Server", "scope_pipe", "frpc", "MRRC-Modern")
$EnvPrefixes  = @("MRRC_", "FT710_")
$InstallRegPaths = @(
    "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*",
    "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*",
    "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*"
)

function Say($text) { Write-Host $text }
function Step($text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }
function Act($text)  { if ($WhatIf) { Write-Host "   would: $text" -ForegroundColor Yellow } else { Write-Host "   $text" } }

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

# ---------------------------------------------------------------- inventory
Step "What was found on this machine"

$found = @()
if (Test-Path $AppDir) { $found += "application:      $AppDir" }
if (Test-Path $LegacyAppDir) { $found += "application(x86): $LegacyAppDir" }
foreach ($d in $DataDirs) { if (Test-Path $d) { $found += "data:             $d" } }
foreach ($n in $ProcessNames) {
    $p = Get-Process -Name $n -ErrorAction SilentlyContinue
    if ($p) { $found += ("running:          {0} (pid {1})" -f $n, (($p | ForEach-Object { $_.Id }) -join ",")) }
}
$envUser = [Environment]::GetEnvironmentVariables("User").Keys    | Where-Object { $n = $_; $EnvPrefixes | Where-Object { $n -like "$_*" } }
$envMachine = [Environment]::GetEnvironmentVariables("Machine").Keys | Where-Object { $n = $_; $EnvPrefixes | Where-Object { $n -like "$_*" } }
foreach ($n in $envUser)    { $found += "env (user):       $n" }
foreach ($n in $envMachine) { $found += "env (machine):    $n" }
$tasks = Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $_.TaskName -like "MRRC*" }
foreach ($t in $tasks) { $found += "task:             $($t.TaskName) [$($t.State)]" }
$shortcuts = @()
$legacyShortcuts = @()
foreach ($root in @((Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"),
                    [Environment]::GetFolderPath("Desktop"),
                    "C:\ProgramData\Microsoft\Windows\Start Menu\Programs")) {
    if (Test-Path $root) {
        $shortcuts += Get-ChildItem $root -Recurse -Include "*MRRC Modern*.lnk" -ErrorAction SilentlyContinue
        $legacyShortcuts += Get-ChildItem $root -Recurse -Include "MRRC*.lnk" -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -notlike "*MRRC Modern*" }
    }
}
foreach ($s in $shortcuts) { $found += "shortcut:         $($s.FullName)" }
foreach ($s in $legacyShortcuts) { $found += "shortcut(other):  $($s.FullName)  <- not touched" }
$uninstallers = @()
$foreign = @()
foreach ($rp in $InstallRegPaths) {
    Get-ItemProperty $rp -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -like "*MRRC*" } | ForEach-Object {
        $isMine = $_.DisplayName -like "*MRRC Modern*"
        if ($isMine -or $IncludeLegacy) {
            $uninstallers += [pscustomobject]@{ Name = $_.DisplayName; Uninstall = $_.UninstallString }
        } else {
            $foreign += [pscustomobject]@{ Name = $_.DisplayName; Uninstall = $_.UninstallString }
        }
    }
}
foreach ($u in $uninstallers) { $found += "uninstall entry:  $($u.Name)" }
foreach ($u in $foreign)      { $found += "other product:    $($u.Name)  <- not touched (different product; add -IncludeLegacy to include it)" }

if ($found.Count -eq 0) {
    Say "   Nothing from MRRC Modern is on this machine - there is nothing to clean."
    return
}
$found | ForEach-Object { Say "   $_" }

if (-not (Test-Admin)) {
    Say ""
    Say "   This needs administrator rights (it removes files under Program Files and can stop a service)."
    if (-not $WhatIf -and -not $Force) {
        $answer = Read-Host "   Restart this script elevated now? [y/N]"
        if ($answer -match "^[Yy]") {
            $args = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $PSCommandPath, "-Force")
            Start-Process -FilePath "powershell.exe" -ArgumentList $args -Verb RunAs
        }
        return
    }
    if (-not $WhatIf) { Say "   Run it again from an elevated PowerShell."; return }
}

if (-not $Force -and -not $WhatIf) {
    Say ""
    Say "   Everything listed above will be removed. Configuration, certificates and logs are copied"
    Say "   to $BackupDir first, so you can look at them afterwards (or put them back)."
    Say ""
    Say "   >>> This window is waiting for you. If you do not see the question above, look for"
    Say "   >>> another PowerShell window (it opened when you approved the administrator prompt)."
    Say "   >>> To skip this question next time, run with -Force."
    $answer = Read-Host "   Type 'clean' and press Enter to continue (anything else cancels)"
    if ($answer -ne "clean") { Say "   Stopped. Nothing was changed."; return }
}

# ---------------------------------------------------------------- backup
Step "Backup"
if ($WhatIf) {
    Act "copy configuration, certificates and logs to $BackupDir"
} else {
    New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
    $copied = 0
    foreach ($d in $DataDirs) {
        if (-not (Test-Path $d)) { continue }
        $dest = Join-Path $BackupDir (Split-Path $d -Leaf)
        if (Test-Path $dest) { $dest = "$dest-(2)" }

        # Report the size before copying: a recordings folder can be gigabytes, and a copy with no
        # output for minutes looks like a hang (it did, the first time this ran on a real machine).
        $files = @(Get-ChildItem $d -Recurse -File -Force -ErrorAction SilentlyContinue)
        $bytes = ($files | Measure-Object -Property Length -Sum).Sum
        $mb = if ($bytes) { [math]::Round($bytes / 1MB, 1) } else { 0 }
        Say "   copying $d ($($files.Count) files, $mb MB) ..."
        try {
            foreach ($f in $files) {
                $rel = $f.FullName.Substring($d.Length).TrimStart("\")
                if ($BackupSkip | Where-Object { $rel -like "$_\*" }) { continue }
                $target = Join-Path $dest $rel
                New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
                Copy-Item $f.FullName $target -Force -ErrorAction SilentlyContinue
            }
            $copied++
            Say "     done"
        } catch {
            Say "     could not copy ($($_.Exception.Message)) - continuing anyway"
        }
    }
    Say "   backup: $BackupDir ($copied copied)"
}

# ---------------------------------------------------------------- stop
Step "Stopping MRRC Modern"
foreach ($n in $ProcessNames) {
    $procs = Get-Process -Name $n -ErrorAction SilentlyContinue
    foreach ($p in $procs) {
        if ($WhatIf) { Act "stop $n (pid $($p.Id))" }
        else {
            try { $p.Kill(); Say "   stopped $n (pid $($p.Id))" } catch { Say "   could not stop ${n}: $($_.Exception.Message)" }
        }
    }
}
if (-not $WhatIf) { Start-Sleep -Seconds 2 }

# ---------------------------------------------------------------- uninstall
Step "Running the application's uninstaller"
$ranUninstaller = $false
foreach ($u in $uninstallers) {
    $cmd = $u.Uninstall
    if (-not $cmd) { continue }
    # Inno writes:  "C:\Program Files\MRRC Modern\unins000.exe" /SILENT  - keep the quoting
    if ($WhatIf) {
        Act "run $cmd /VERYSILENT /NORESTART"
        continue
    }
    try {
        if ($cmd -match '^\s*"([^"]+)"\s*(.*)$') {
            $exe = $Matches[1]; $rest = $Matches[2]
            $proc = Start-Process -FilePath $exe -ArgumentList ($rest + " /VERYSILENT /NORESTART /SUPPRESSMSGBOXES") -PassThru
            # Bounded wait: a hidden modal dialog in the uninstaller must not stop the cleanup.
            if (-not $proc.WaitForExit(180000)) { Say "   uninstaller still running after 3 minutes - continuing without it" }
        } else {
            $exe = ($cmd -split " ")[0]
            $proc = Start-Process -FilePath $exe -ArgumentList "/VERYSILENT /NORESTART /SUPPRESSMSGBOXES" -PassThru
            if (-not $proc.WaitForExit(180000)) { Say "   uninstaller still running after 3 minutes - continuing" }
        }
        $ranUninstaller = $true
        Say "   uninstaller finished: $($u.Name)"
    } catch {
        Say "   uninstaller failed ($($_.Exception.Message)) - the folders are removed below anyway"
    }
}
if (-not $ranUninstaller -and -not $WhatIf) {
    # No entry in the registry: try the well-known path.
    $un = Join-Path $AppDir "unins000.exe"
    if (Test-Path $un) {
        $proc = Start-Process -FilePath $un -ArgumentList "/VERYSILENT /NORESTART /SUPPRESSMSGBOXES" -PassThru
        if (-not $proc.WaitForExit(180000)) { Say "   uninstaller still running after 3 minutes - continuing" }
        Say "   uninstaller finished: $un"
    } else {
        Say "   no uninstaller found (this is normal after a partial upgrade)"
    }
}
if (-not $WhatIf) { Start-Sleep -Seconds 3 }

# ---------------------------------------------------------------- folders
Step "Removing folders"
foreach ($d in @($AppDir, $LegacyAppDir) + $DataDirs) {
    if (-not (Test-Path $d)) { continue }
    if ($WhatIf) { Act "remove $d"; continue }
    try {
        Remove-Item $d -Recurse -Force -ErrorAction Stop
        if (Test-Path $d) { Say "   partially removed (something is still holding a file): $d" }
        else { Say "   removed $d" }
    } catch {
        Say "   could not remove ${d}: $($_.Exception.Message)"
        Say "     (close anything using it - Explorer window, antivirus scan - and run this again)"
    }
}

# ---------------------------------------------------------------- environment variables
Step "Removing environment variables"
foreach ($scope in @("User", "Machine")) {
    $names = [Environment]::GetEnvironmentVariables($scope).Keys | Where-Object { $n = $_; $EnvPrefixes | Where-Object { $n -like "$_*" } }
    foreach ($n in $names) {
        if ($WhatIf) { Act "clear $scope env $n" ; continue }
        try {
            [Environment]::SetEnvironmentVariable($n, $null, $scope)
            Say "   cleared $scope\$n"
        } catch { Say "   could not clear $scope\${n}: $($_.Exception.Message)" }
    }
}

# ---------------------------------------------------------------- tasks and shortcuts
Step "Removing scheduled tasks and shortcuts"
foreach ($t in $tasks) {
    if ($WhatIf) { Act "delete task $($t.TaskName)"; continue }
    try { Unregister-ScheduledTask -TaskName $t.TaskName -Confirm:$false; Say "   deleted task $($t.TaskName)" }
    catch { Say "   could not delete task $($t.TaskName): $($_.Exception.Message)" }
}
foreach ($s in $shortcuts) {
    if ($WhatIf) { Act "delete shortcut $($s.FullName)"; continue }
    if (-not (Test-Path $s.FullName)) { continue }        # the uninstaller usually gets these first
    Remove-Item $s.FullName -Force -ErrorAction SilentlyContinue
    if (Test-Path $s.FullName) { Say "   could not delete $($s.FullName)" }
    else { Say "   deleted shortcut $($s.Name)" }
}

# ---------------------------------------------------------------- report
Step "Result"
$left = @()
if (Test-Path $AppDir) { $left += $AppDir }
if (Test-Path $LegacyAppDir) { $left += $LegacyAppDir }
foreach ($d in $DataDirs) { if (Test-Path $d) { $left += $d } }
$leftProcs = @()
foreach ($n in $ProcessNames) {
    $p = Get-Process -Name $n -ErrorAction SilentlyContinue
    if ($p) { $leftProcs += "$n (pid $(($p | ForEach-Object { $_.Id }) -join ','))" }
}
if ($WhatIf) {
    Say "   -WhatIf: nothing was changed. Run again without it to clean."
} elseif ($left.Count -eq 0 -and $leftProcs.Count -eq 0) {
    Say "   Clean. Nothing from MRRC Modern is left on this machine."
    Say "   Your backup is here: $BackupDir"
    Say ""
    Say "   To come back: install from https://www.vlsc.net/mrrc_modern/ , then"
    Say "   Settings -> Cloud Hub -> enter your callsign -> Apply. The instance re-enrols itself"
    Say "   and the entry (https://<callsign>.mrrc.vlsc.net/) returns."
} else {
    foreach ($l in $left) { Say "   still present: $l" }
    foreach ($p in $leftProcs) { Say "   still running: $p" }
    Say ""
    Say "   A reboot clears anything that was locked, then run this script once more."
}
