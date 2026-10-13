$ErrorActionPreference = "Stop"
$App = "C:\mrrc_modern\dist\windows\MRRC-Modern"
$bad = 0
function Check([string]$label, [bool]$ok, [string]$detail) {
    if ($ok) { Write-Host ("  OK   " + $label + " " + $detail) }
    else { Write-Host ("  FAIL " + $label + " " + $detail); $script:bad++ }
}

Write-Host "--- layer 1: version / artifact identity ---"
$exe = Get-Item "C:\mrrc_modern\dist\windows\MRRC-Modern-Setup.exe"
Write-Host ("  Setup.exe mtime " + $exe.LastWriteTime.ToString("yyyy-MM-dd HH:mm:ss") + "  bytes " + $exe.Length)
$vt = (Get-Content (Join-Path $App "version.txt") -Raw).Trim()
Check "version.txt" ($vt -eq "1.27.0") $vt

Write-Host "--- layer 2: structure ---"
foreach ($name in @("MRRC-Modern-Server.exe", "MRRC-Modern-Launcher.exe", "scope_pipe.exe")) {
    $p = Join-Path $App $name
    Check $name (Test-Path $p) $(if (Test-Path $p) { [string](Get-Item $p).Length } else { "MISSING" })
}
$internal = Join-Path $App "_internal"
$ft8 = @("wsjt_core.dll", "libfftw3f-3.dll", "libfftw3f_threads-3.dll", "libgfortran-5.dll",
         "libgomp-1.dll", "libquadmath-0.dll", "libgcc_s_seh-1.dll", "libstdc++-6.dll",
         "libwinpthread-1.dll")
foreach ($dll in $ft8) {
    $p = Join-Path $internal $dll
    Check ("FT8 " + $dll) (Test-Path $p) $(if (Test-Path $p) { [string](Get-Item $p).Length } else { "MISSING" })
}
foreach ($f in @("static\index.html", "static\ft8.html", "static\ft8\main.js", "mem_channels.json")) {
    Check $f (Test-Path (Join-Path $internal $f)) ""
}
$fleet = Get-ChildItem (Join-Path $App "fleet") -File -ErrorAction SilentlyContinue
Check "fleet payload at app root" ($fleet.Count -ge 13) ($fleet.Count.ToString() + " files")
$junk = @(Get-ChildItem $App -Recurse -Force | Where-Object { $_.Name -like "._*" -or $_.Name -eq ".DS_Store" })
Check "junk files" ($junk.Count -eq 0) $junk.Count
$secret = @(Get-ChildItem $App -Recurse -Force -File | Where-Object { $_.Name -match '\.(pem|key|p12|pfx)$' -or $_.Name -eq '.env' })
Check "key-shaped files" ($secret.Count -eq 0) ($secret | ForEach-Object { $_.Name }) -join ","

Write-Host "--- layer 2b: the FT8 DLLs are real PE images with the entry points ---"
$nm = "C:\msys64\mingw64\bin\nm.exe"
$symbols = & $nm -g --defined-only (Join-Path $internal "wsjt_core.dll") | Select-String "wsjt_" | ForEach-Object { $_.Line }
Check "wsjt_* exports" ($symbols.Count -ge 5) ($symbols.Count.ToString() + " symbols")
$objdump = "C:\msys64\mingw64\bin\objdump.exe"
$deps = & $objdump -p (Join-Path $internal "wsjt_core.dll") | Select-String "DLL Name: (.*lib.*)" | ForEach-Object { $_.Matches[0].Groups[1].Value.Trim() }
Write-Host ("  deps: " + ($deps -join ", "))
foreach ($d in $deps) {
    Check ("bundled " + $d) (Test-Path (Join-Path $internal $d)) ""
}

Write-Host ("LAYERS_1_2 " + $(if ($bad -eq 0) { "PASS" } else { "FAIL($bad)" }))
