@echo off
rem MRRC Modern - move configuration and certificates to a new computer.
rem Double-click for a menu: 1) back up this machine  2) restore from a backup.
rem Command line: -Export [-Out FILE] [-Encrypt] ^| -Import FILE ^| -Restore ^| -List FILE
rem Requires no administrator rights.
setlocal
set PS1=%~dp0MRRC-Modern-Backup.ps1
if not exist "%PS1%" (
    echo.
    echo   Downloading the tool: MRRC-Modern-Backup.ps1 ...
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "Invoke-WebRequest -UseBasicParsing -Uri 'https://www.vlsc.net/mrrc_modern/downloads/MRRC-Modern-Backup.ps1' -OutFile '%PS1%'"
)
if not exist "%PS1%" (
    echo.
    echo   Could not obtain MRRC-Modern-Backup.ps1.
    echo   Download both files from:
    echo       https://www.vlsc.net/mrrc_modern/downloads/MRRC-Modern-Backup.cmd
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
