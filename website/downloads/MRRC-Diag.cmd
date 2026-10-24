@echo off
rem ===========================================================================
rem  MRRC Modern - LAN diagnostics collector (Windows, no SSH knowledge needed)
rem
rem  Use this when the box answers on the local network (the web page opens)
rem  but the public entry / the support upload does not work. Everything here
rem  talks to the box directly over the LAN - the internet is not involved.
rem
rem  What it does
rem    1. logs in to the box with the web password
rem    2. records the Cloud Hub / tunnel state, health and build info
rem    3. builds a diagnostics bundle ON the box
rem    4. copies that bundle into a folder next to this script
rem         - HTTP download when the box has it,
rem         - otherwise scp from the box (account password, asked once;
rem           the box's Settings page can set a new one without the old)
rem    5. zips the whole folder into ONE file you can send back
rem
rem  Usage
rem    double-click it,  or:  mrrc-diag.cmd 192.168.1.50
rem
rem  Nothing is uploaded anywhere and nothing on the box is changed.
rem ===========================================================================
setlocal DisableDelayedExpansion
chcp 65001 >nul 2>&1
title MRRC Modern - collect diagnostics

set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"

set "OUT=%HERE%\mrrc-diag-%COMPUTERNAME%"
if exist "%OUT%" rd /s /q "%OUT%" >nul 2>&1
mkdir "%OUT%" >nul 2>&1
if not exist "%OUT%" goto :no_folder

echo ============================================================
echo  MRRC Modern diagnostics collector
echo ============================================================
echo.

rem ---------------------------------------------------------------- target --
set "BOX=%~1"
if not defined BOX set /p "BOX=Box IP address (as in the browser, e.g. 192.168.1.50, or mrrc.local): "
if not defined BOX goto :no_input
set "BOX=%BOX:https://=%"
set "BOX=%BOX:http://=%"
set "BOX=%BOX:/=%"
if "%BOX%"=="" goto :no_input

echo %BOX% | find ":" >nul 2>&1
if errorlevel 1 set "BOX=%BOX%:8888"
for /f "tokens=1 delims=:" %%a in ("%BOX%") do set "HOST=%%a"

where curl >nul 2>&1
if errorlevel 1 goto :no_curl

rem ---------------------------------------------------------------- scheme --
set "CODE=000"
for /f "usebackq delims=" %%c in (`curl -k -s -o NUL -w "%%{http_code}" --max-time 6 "https://%BOX%/login"`) do set "CODE=%%c"
if not "%CODE%"=="000" goto :scheme_https
for /f "usebackq delims=" %%c in (`curl -s -o NUL -w "%%{http_code}" --max-time 6 "http://%BOX%/login"`) do set "CODE=%%c"
if "%CODE%"=="000" goto :no_box
set "BASE=http://%BOX%"
goto :scheme_ok
:scheme_https
set "BASE=https://%BOX%"
:scheme_ok
echo  Box   : %BASE%
echo.

rem ----------------------------------------------------------------- login --
set "MRRC_PW="
set /p "MRRC_PW=Web password (the one you open the page with): "
if not defined MRRC_PW goto :no_input
powershell -NoProfile -Command "$e=[System.Text.UTF8Encoding]::new($false); $j=@{password=$env:MRRC_PW}|ConvertTo-Json -Compress; [IO.File]::WriteAllText($env:TEMP+'\mrrc-login.json',$j,$e)" >nul 2>&1
curl -k -s -c "%TEMP%\mrrc-cookies.txt" -H "Content-Type: application/json" --data-binary "@%TEMP%\mrrc-login.json" "%BASE%/api/auth/login" -o "%TEMP%\mrrc-login-resp.json" --max-time 20
set "LOGINOK=no"
for /f "usebackq delims=" %%l in (`powershell -NoProfile -Command "try{if((Get-Content -Raw -Encoding UTF8 '%TEMP%\mrrc-login-resp.json'|ConvertFrom-Json).ok){'ok'}else{'no'}}catch{'no'}"`) do set "LOGINOK=%%l"
if not "%LOGINOK%"=="ok" goto :login_failed

rem ------------------------------------------------------- live diagnostics --
echo  Reading cloud / tunnel state...
curl -k -s -b "%TEMP%\mrrc-cookies.txt" "%BASE%/api/cloud/state" -o "%OUT%\cloud-state.json" --max-time 20
curl -k -s -b "%TEMP%\mrrc-cookies.txt" "%BASE%/api/health" -o "%OUT%\health.json" --max-time 20
curl -k -s -b "%TEMP%\mrrc-cookies.txt" "%BASE%/api/status" -o "%OUT%\radio-state.json" --max-time 20

echo.
echo ---- cloud / tunnel ------------------------------------------
powershell -NoProfile -Command "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $j=Get-Content -Raw -Encoding UTF8 '%OUT%\cloud-state.json' | ConvertFrom-Json; '' + 'entry         : ' + $j.entry; '' + 'label         : ' + $j.label; '' + 'tunnel_running: ' + $j.tunnel_running; '' + 'tunnel_error  : ' + $j.tunnel_error; '' + 'cert          : ' + $j.cert; '' + 'portal        : ' + $j.portal; '' + 'cert_reload   : ' + $j.cert_reload_required"
echo ---- health ---------------------------------------------------
powershell -NoProfile -Command "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $j=Get-Content -Raw -Encoding UTF8 '%OUT%\health.json' | ConvertFrom-Json; '' + 'status        : ' + $j.status; '' + 'radio         : ' + $j.radio_connected; '' + 'audio         : ' + $j.audio_available; '' + 'scope         : ' + $j.scope_connected; '' + 'uptime_s      : ' + $j.uptime_seconds"
echo --------------------------------------------------------------
echo.

rem ------------------------------------------------------- build a bundle --
echo  Building the diagnostics bundle on the box (may take a minute)...
set "MRRC_PROBLEM=Cloud entry and support upload do not work; the box is reachable on the LAN only."
powershell -NoProfile -Command "$e=[System.Text.UTF8Encoding]::new($false); $j=@{problem=$env:MRRC_PROBLEM;contact='';client=@{collector='mrrc-diag.cmd';os=$env:OS}}|ConvertTo-Json -Compress; [IO.File]::WriteAllText($env:TEMP+'\mrrc-bundle.json',$j,$e)" >nul 2>&1
curl -k -s -b "%TEMP%\mrrc-cookies.txt" -H "Content-Type: application/json" --data-binary "@%TEMP%\mrrc-bundle.json" "%BASE%/api/support/bundle" -o "%OUT%\bundle.json" --max-time 300

set "BID="
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "try{(Get-Content -Raw -Encoding UTF8 '%OUT%\bundle.json'|ConvertFrom-Json).id}catch{''}"`) do set "BID=%%i"
if not defined BID goto :bundle_failed
echo  Bundle id: %BID%

rem ------------------------------------------- where did the box put the zip --
set "SAVED="
set "MRRC_BID=%BID%"
powershell -NoProfile -Command "$e=[System.Text.UTF8Encoding]::new($false); $j=@{id=$env:MRRC_BID}|ConvertTo-Json -Compress; [IO.File]::WriteAllText($env:TEMP+'\mrrc-save.json',$j,$e)" >nul 2>&1
curl -k -s -b "%TEMP%\mrrc-cookies.txt" -H "Content-Type: application/json" --data-binary "@%TEMP%\mrrc-save.json" "%BASE%/api/support/save" -o "%OUT%\save.json" --max-time 60
for /f "usebackq delims=" %%p in (`powershell -NoProfile -Command "try{(Get-Content -Raw -Encoding UTF8 '%OUT%\save.json'|ConvertFrom-Json).path}catch{''}"`) do set "SAVED=%%p"
if defined SAVED echo  On the box : %SAVED%

rem --------------------------------------------- try the HTTP download path --
set "ISZIP="
set "ZIPFILE=%OUT%\support-%BID%.zip"
curl -k -s -b "%TEMP%\mrrc-cookies.txt" -H "Content-Type: application/json" --data-binary "@%TEMP%\mrrc-save.json" -o "%ZIPFILE%" "%BASE%/api/support/download" --max-time 300
for /f "usebackq delims=" %%z in (`powershell -NoProfile -Command "$p='%ZIPFILE%'; if(Test-Path $p){$b=[IO.File]::ReadAllBytes($p); if($b.Length -gt 4 -and $b[0] -eq 80 -and $b[1] -eq 75){'yes'}else{'no'}}else{'no'}"`) do set "ISZIP=%%z"
if "%ISZIP%"=="yes" goto :got_zip
if exist "%ZIPFILE%" del "%ZIPFILE%" >nul 2>&1
echo  This build has no HTTP download endpoint - falling back to scp.

rem ----------------------------------------------------- scp from the box --
where scp >nul 2>&1
if errorlevel 1 goto :no_scp
set "SSHUSER="
:scp_try
if not defined SSHUSER goto :scp_pick
goto :scp_go
:scp_pick
echo.
echo  Copy the bundle straight off the box over SSH.
echo.
echo  Forgot the password? On the box open Settings - System password,
echo  pick the account and set a NEW one - the old password is not needed.
echo  (That page is for SSH/HDMI accounts; it does not change the web login.)
echo.
set /p "SSHUSER=SSH account [mrrc]: "
if not defined SSHUSER set "SSHUSER=mrrc"
:scp_go
echo.
echo  Connecting as %SSHUSER% - type that account's password when asked.
echo.
if defined SAVED goto :scp_one
scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL "%SSHUSER%@%HOST%:/home/mrrc/.local/share/support-export/support-*.zip" "%OUT%\"
goto :scp_result
:scp_one
scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL "%SSHUSER%@%HOST%:%SAVED%" "%OUT%\"
:scp_result
if not errorlevel 1 goto :got_zip
echo.
echo  That did not work.
if /i "%SSHUSER%"=="root" goto :scp_giveup
set "TRYAGAIN="
set /p "TRYAGAIN=Try another account? Type it (e.g. root), or press Enter to stop: "
if not defined TRYAGAIN goto :scp_giveup
set "SSHUSER=%TRYAGAIN%"
goto :scp_try
:scp_giveup
echo.
echo  SSH copy failed. The bundle is still on the box and can be uploaded
echo  later; the text files here already carry the tunnel state, which is
echo  usually the answer on its own.
:no_scp

:got_zip

rem --------------------------------------------------------------- package --
echo.
echo  Packing everything into one file...
powershell -NoProfile -Command "Compress-Archive -Path '%OUT%\*' -DestinationPath '%OUT%.zip' -Force" >nul 2>&1
if exist "%OUT%.zip" goto :done
echo  (zip step failed - send the folder instead)

:done
echo.
echo ============================================================
echo  DONE
echo.
echo  Send this file back:
echo    %OUT%.zip
echo.
if not exist "%OUT%.zip" echo  (or the folder: %OUT%)
echo ============================================================
echo.
pause
exit /b 0

rem ----------------------------------------------------------------- errors --
:no_input
echo.
echo  Nothing entered - aborted.
goto :end

:no_folder
echo  Cannot create the output folder next to this script.
echo  Move the script to your Desktop or Downloads and run it again.
goto :end

:no_curl
echo.
echo  curl.exe is missing. It ships with Windows 10 (1809+) and Windows 11.
echo  On an older Windows, copy this file to a machine that has it.
goto :end

:no_box
echo.
echo  No answer from %BOX%.
echo    1. Is the box switched on, and on the same network?
echo    2. Open the web page in a browser first - if that works, the IP is right.
echo  (The box only needs the LAN for this; the internet is irrelevant.)
goto :end

:login_failed
echo.
echo  Login failed. Check the web password.
powershell -NoProfile -Command "[Console]::OutputEncoding=[Text.Encoding]::UTF8; try{(Get-Content -Raw -Encoding UTF8 '%TEMP%\mrrc-login-resp.json'|ConvertFrom-Json).error}catch{'no answer from the box'}"
goto :end

:bundle_failed
echo.
echo  The box did not return a bundle id. Its answer was saved here:
echo    %OUT%\bundle.json
echo  The cloud/tunnel state above is usually the important part anyway.
goto :package_anyway

:package_anyway
powershell -NoProfile -Command "Compress-Archive -Path '%OUT%\*' -DestinationPath '%OUT%.zip' -Force" >nul 2>&1
echo.
echo  Send back: %OUT%.zip
pause
exit /b 0

:end
echo.
pause
exit /b 1
