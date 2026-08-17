$ErrorActionPreference = "Stop"
$isccDir = "${env:ProgramFiles(x86)}\Inno Setup 6"
if (Test-Path "$isccDir\iscc.exe") { $env:Path = "$isccDir;" + $env:Path }
Set-Location C:\mrrc_modern
.\venv\Scripts\Activate.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\windows\build.ps1
Write-Host "BUILD_DONE"
