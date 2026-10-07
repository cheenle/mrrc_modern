@echo off
rem MRRC Modern - apply the current static patch. Double-click this file.
rem It downloads the applier, lets it choose the patch that applies to this installation,
rem backs up every file it replaces, and restarts the app. Nothing to type.
setlocal
set PS1=%TEMP%\MRRC-Modern-Patch.ps1
set URL=https://www.vlsc.net/mrrc_modern/downloads/MRRC-Modern-Patch.ps1

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo   Asking for administrator rights - approve the prompt, then wait...
    echo.
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "Invoke-WebRequest -UseBasicParsing -Uri '%URL%' -OutFile '%PS1%'; Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File','%PS1%') -Verb RunAs"
    echo   If a window opened and closed, this one has the result above it.
    echo.
    pause
    exit /b
)

echo.
echo   Downloading the patch helper...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Invoke-WebRequest -UseBasicParsing -Uri '%URL%' -OutFile '%PS1%'"
if not exist "%PS1%" (
    echo.
    echo   Could not download %URL%
    echo   Check that this machine can open https://www.vlsc.net/ in a browser.
    echo.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
echo.
echo   Done. The app should be running again; press Ctrl-F5 in the browser once.
echo.
pause
