@echo off
rem MRRC Modern - connect this installation to the Cloud Hub. Double-click this file.
rem It asks for your callsign and the enrolment secret the operator sent you, then does the rest:
rem enrols the certificate, restarts the app and prints your entry address.
setlocal
set PS1=%TEMP%\MRRC-Modern-Connect.ps1
set URL=https://www.vlsc.net/mrrc_modern/downloads/MRRC-Modern-Connect.ps1

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo   Asking for administrator rights - approve the prompt, then wait...
    echo.
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "Invoke-WebRequest -UseBasicParsing -Uri '%URL%' -OutFile '%PS1%'; Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File','%PS1%') -Verb RunAs"
    echo   If a window opened and closed, its result is above.
    echo.
    pause
    exit /b
)

echo.
echo   Downloading the connect helper...
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
pause
