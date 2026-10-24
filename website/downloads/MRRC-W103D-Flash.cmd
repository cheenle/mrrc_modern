@echo off
rem ===========================================================================
rem  MRRC Modern - make a W103D flash USB stick on Windows
rem
rem  Double-click this file. It asks for administrator rights (writing a raw
rem  disk requires them), downloads the image if it is not here yet, verifies
rem  its SHA-256, lists the USB / removable disks, and only writes after you
rem  type ERASE. Afterwards it reads the whole image back off the stick and
rem  compares hashes.
rem
rem  The real work is in MRRC-W103D-Flash.ps1 (same folder; fetched automatically
rem  if missing). Source: https://www.vlsc.net/mrrc_modern/downloads/
rem ===========================================================================
setlocal
title MRRC Modern - W103D flash USB

set "PS1=%~dp0MRRC-W103D-Flash.ps1"

rem --- administrator? writing \\.\PhysicalDriveN needs it -------------------
net session >nul 2>&1
if errorlevel 1 (
    echo.
    echo   Requesting administrator rights...
    if "%~1"=="" (
        powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    ) else (
        powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs -ArgumentList '%*'"
    )
    exit /b
)

rem --- the script itself ----------------------------------------------------
if not exist "%PS1%" (
    echo.
    echo   Downloading MRRC-W103D-Flash.ps1 ...
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -UseBasicParsing -Uri 'https://www.vlsc.net/mrrc_modern/downloads/MRRC-W103D-Flash.ps1' -OutFile '%PS1%'"
)
if not exist "%PS1%" (
    echo.
    echo   Could not obtain MRRC-W103D-Flash.ps1.
    echo   Download both files from:
    echo       https://www.vlsc.net/mrrc_modern/downloads/MRRC-W103D-Flash.cmd
    echo.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
set RC=%ERRORLEVEL%

echo.
echo   exit code %RC%
pause
exit /b %RC%
