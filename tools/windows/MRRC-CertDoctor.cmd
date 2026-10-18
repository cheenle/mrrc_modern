@echo off
rem MRRC Modern - certificate trust check / repair (Cloud Hub failure helper).
rem Double-click this file for a read-only check; run "MRRC-CertDoctor.cmd -Fix"
rem to also repair this machine's certificate store (it asks before every change).
rem
rem The real work is in MRRC-CertDoctor.ps1 next to this file. Keep the whole
rem folder together - it ships its own openssl.exe, which is what makes the
rem verdict here the same one the application makes.

setlocal
set PS1=%~dp0MRRC-CertDoctor.ps1
set ARG1=%1

rem Self-download fallback: a user who grabbed only this .cmd still gets the whole tool.
rem (The ZIP also carries its own openssl.exe; fetched this way the script falls back to the
rem  one MRRC Modern installed, which is the same build.)
if not exist "%PS1%" (
    echo.
    echo   Downloading the checker: MRRC-CertDoctor.ps1 ...
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "Invoke-WebRequest -UseBasicParsing -Uri 'https://www.vlsc.net/mrrc_modern/downloads/MRRC-CertDoctor.ps1' -OutFile '%PS1%'"
)
if not exist "%PS1%" (
    echo.
    echo   Could not obtain MRRC-CertDoctor.ps1.
    echo   Download and extract MRRC-CertDoctor.zip instead:
    echo       https://www.vlsc.net/mrrc_modern/downloads/MRRC-CertDoctor.zip
    echo.
    pause
    exit /b 1
)

if /i "%ARG1%"=="-Fix" (
    net session >nul 2>&1
    if %errorlevel% neq 0 (
        echo.
        echo   Repair mode needs administrator rights to change the machine-wide
        echo   certificate stores. Approve the prompt, then wait for the report...
        echo.
        powershell -NoProfile -ExecutionPolicy Bypass -Command ^
            "Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File','%PS1%','-Fix','-OpenSSL','%~dp0openssl.exe') -Verb RunAs"
        exit /b
    )
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %ARG1% -OpenSSL "%~dp0openssl.exe"
echo.
pause
