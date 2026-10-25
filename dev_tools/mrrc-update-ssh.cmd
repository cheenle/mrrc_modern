@echo off
rem ===========================================================================
rem  MRRC Modern - update the box over SSH (no reflash, no web password)
rem
rem  What it is for: a box whose build has no in-browser updater - anything
rem  released before 2026-10-18 (1.27 and earlier, including 1.24.x). Those
rem  boxes can still be updated in place: the release is installed over
rem  /opt/mrrc_modern and the service restarted. Nothing is reflashed, and
rem  config, recordings, memory channels and the tunnel are left alone.
rem
rem  Why not the box's own `mrrc-update`: builds up to 1.25.5 ship one that
rem  syncs with `rsync --delete`, which wipes env/, profiles/ and the
rem  recordings. So this tool brings its own installer (mrrc-remote-update.sh,
rem  published next to this file): it backs the current code up first and then
rem  only adds and overwrites.
rem
rem  What it needs: an SSH account on the box (root, or mrrc) and its password.
rem  It does NOT need the web login, and it does not talk to the box's API
rem  except to see whether the service came back.
rem
rem  Usage
rem    double-click it,  or:  mrrc-update-ssh.cmd 192.168.1.50 [root]
rem
rem  Exit codes: 0 updated - 1 stopped/failed - 2 no answer from the box
rem ===========================================================================
setlocal DisableDelayedExpansion
title MRRC Modern - update over SSH

set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"
set "OUT=%HERE%\mrrc-update-ssh-%COMPUTERNAME%"
if exist "%OUT%" rd /s /q "%OUT%" >nul 2>&1
mkdir "%OUT%" >nul 2>&1
if not exist "%OUT%" goto :no_folder
set "TRACE=%OUT%\transcript.txt"

echo ============================================================
echo  MRRC Modern - update over SSH
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
where ssh  >nul 2>&1
if errorlevel 1 goto :no_ssh
where scp  >nul 2>&1
if errorlevel 1 goto :no_ssh

set "SSHUSER=%~2"
if not defined SSHUSER set "SSHUSER="

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
>>"%TRACE%" echo  Box   : %BASE% (ssh %HOST%)
echo  Box   : %BASE%
echo.

rem ------------------------------------------------------------- manifest --
echo  Reading the release manifest...
set "MANIFEST=https://www.vlsc.net/mrrc_modern/downloads/latest.json"
curl -s -o "%OUT%\latest.json" "%MANIFEST%" --max-time 60
set "PKGVER="
for /f "usebackq delims=" %%v in (`powershell -NoProfile -Command "try{$j=Get-Content -Raw -Encoding UTF8 '%OUT%\latest.json'|ConvertFrom-Json; $b=if($j.box){$j.box}elseif($j.boxLine){$j.boxLine}else{$null}; if($b){$b.version}else{''}}catch{''}"`) do set "PKGVER=%%v"
set "PKGURL="
for /f "usebackq delims=" %%v in (`powershell -NoProfile -Command "try{$j=Get-Content -Raw -Encoding UTF8 '%OUT%\latest.json'|ConvertFrom-Json; $b=if($j.box){$j.box}elseif($j.boxLine){$j.boxLine}else{$null}; if($b){$b.url}else{''}}catch{''}"`) do set "PKGURL=%%v"
set "PKGSHA="
for /f "usebackq delims=" %%v in (`powershell -NoProfile -Command "try{$j=Get-Content -Raw -Encoding UTF8 '%OUT%\latest.json'|ConvertFrom-Json; $b=if($j.box){$j.box}elseif($j.boxLine){$j.boxLine}else{$null}; if($b){$b.sha256}else{''}}catch{''}"`) do set "PKGSHA=%%v"
if not defined PKGURL goto :no_manifest
if not defined PKGVER goto :no_manifest
echo  Release: %PKGVER%
echo.

rem ---------------------------------------------------------------- confirm --
echo  This installs %PKGVER% over /opt/mrrc_modern on %HOST% and restarts
echo  the service (about 10 seconds offline). The current code is backed up
echo  first; nothing else is deleted or changed.
echo.
set "GO="
set /p "GO=Type YES to continue: "
if /i not "%GO%"=="YES" goto :aborted

if not defined SSHUSER set /p "SSHUSER=SSH account on the box [root]: "
if not defined SSHUSER set "SSHUSER=root"

rem -------------------------------------------------------------- download --
echo.
echo  Downloading %PKGVER% to this PC (about 19 MB, once)...
curl -L -s -o "%TEMP%\mrrc-src.tgz" "%PKGURL%" --max-time 900
set "GOTSHA="
for /f "usebackq delims=" %%v in (`powershell -NoProfile -Command "try{(Get-FileHash -Algorithm SHA256 -LiteralPath '%TEMP%\mrrc-src.tgz').Hash.ToLower()}catch{''}"`) do set "GOTSHA=%%v"
if not defined GOTSHA goto :download_failed
if /i not "%GOTSHA%"=="%PKGSHA%" goto :hash_bad
echo  SHA-256 OK.
curl -s -o "%TEMP%\mrrc-remote-update.sh" "https://www.vlsc.net/mrrc_modern/downloads/mrrc-remote-update.sh" --max-time 60
if not exist "%TEMP%\mrrc-remote-update.sh" goto :download_failed

rem ------------------------------------------------------------------ copy --
echo.
echo  Copying to the box (it will ask for that account's password)...
scp -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=NUL "%TEMP%\mrrc-src.tgz" "%TEMP%\mrrc-remote-update.sh" %SSHUSER%@%HOST%:/tmp/
if errorlevel 1 goto :scp_failed

rem --------------------------------------------------------------- install --
echo.
echo  Installing. The box prints what it is doing; it restarts the service
echo  at the end.
ssh -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=NUL %SSHUSER%@%HOST% "bash /tmp/mrrc-remote-update.sh /tmp/mrrc-src.tgz /opt/mrrc_modern mrrc"
if errorlevel 1 goto :install_failed
>>"%TRACE%" echo  installed %PKGVER%

rem ----------------------------------------------------------------- verify --
echo.
echo  Waiting for the box to come back...
set /a BACK=0
:back_loop
set /a BACK+=1
if %BACK% GEQ 45 goto :back_timeout
set "HC=000"
for /f "usebackq delims=" %%c in (`curl -k -s -o NUL -w "%%{http_code}" --max-time 6 "%BASE%/api/health"`) do set "HC=%%c"
rem Any HTTP answer means the service is up: /api/health is not a public path,
rem so a healthy box answers 401 there. 000 means nothing accepted the
rem connection yet.
if not "%HC%"=="000" goto :done
ping -n 4 127.0.0.1 >nul
goto :back_loop

:done
echo.
echo  ============================================================
echo  Done: %PKGVER% is installed and the box is answering again.
echo  The backup is on the box, path printed above.
echo  Files from this run: %OUT%
echo  ============================================================
goto :cleanup_ok

rem ---------------------------------------------------------------- errors --
:no_manifest
echo.
echo  The release manifest has no box package (%MANIFEST%).
echo  Nothing was changed. Send us this file: %OUT%\latest.json
goto :cleanup_fail

:download_failed
echo.
echo  Could not download the release, or it did not arrive.
echo  Check this PC's connection to www.vlsc.net and run this again.
echo  Nothing was sent to the box.
goto :cleanup_fail

:hash_bad
echo.
echo  The downloaded package does not match the manifest's SHA-256.
echo  Nothing was sent to the box. Try again on a different network if it
echo  repeats.
goto :cleanup_fail

:scp_failed
echo.
echo  Copying to the box failed. Nothing was changed on it.
echo  Common causes: wrong account or password, SSH disabled on the box, or
echo  the box is not reachable from this PC.
goto :cleanup_fail

:install_failed
echo.
echo  The installer reported an error - the box printed its reason above.
echo  If it stopped at the dependency step, the previous code is still
echo  running. Nothing was deleted; the backup path is in that output.
goto :cleanup_fail

:back_timeout
echo.
echo  The installer finished, but the box did not answer within 3 minutes.
echo    1. Is it still powered on and on the same network?
echo    2. Open %BASE% in a browser.
echo  The installer prints the backup path if you need to put the old tree back.
goto :cleanup_fail

:aborted
echo.
echo  Nothing was changed.
goto :cleanup_ok

:no_input
echo.
echo  Nothing entered - aborted.
goto :end

:no_folder
echo  Cannot create the output folder next to this script.
echo  Move it to your Desktop or Downloads and run it again.
goto :end

:no_curl
echo.
echo  curl.exe is missing. It ships with Windows 10 (1809+) and Windows 11.
goto :end

:no_ssh
echo.
echo  ssh.exe / scp.exe are missing. Install "OpenSSH Client" from
echo  Settings - Apps - Optional features, then run this again.
goto :end

:no_box
echo.
echo  No answer from %BOX%.
echo    1. Is the box switched on, and on the same network?
echo    2. Open the web page in a browser first - if that works, the IP is right.
goto :end

:cleanup_ok
del "%TEMP%\mrrc-src.tgz" "%TEMP%\mrrc-remote-update.sh" >nul 2>&1
echo.
pause
exit /b 0

:cleanup_fail
del "%TEMP%\mrrc-src.tgz" "%TEMP%\mrrc-remote-update.sh" >nul 2>&1
echo.
pause
exit /b 1

:end
pause
exit /b 2
