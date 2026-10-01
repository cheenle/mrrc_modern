@echo off
rem MRRC Modern - clean removal. Double-click this file.
rem It asks for confirmation, then removes the application, its configuration and every leftover
rem from earlier versions. Your configuration is backed up to the Desktop first.
rem
rem The real work is in MRRC-Modern-Cleanup.ps1 next to this file.

setlocal
set SCRIPT=%~dp0MRRC-Modern-Cleanup.ps1
if not exist "%SCRIPT%" (
    echo.
    echo   MRRC-Modern-Cleanup.ps1 was not found next to this file.
    echo   Download both files into the same folder and try again.
    echo.
    pause
    exit /b 1
)

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo   Asking for administrator rights...
    echo.
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File','%SCRIPT%') -Verb RunAs"
    exit /b
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%"
echo.
pause
