@echo off
rem ===========================================================================
rem  MRRC Modern - remote update from a Windows PC (no SSH, no Linux)
rem
rem  Use this when the box answers on the local network (the web page opens)
rem  but you would rather drive its update from here than talk a customer
rem  through a page you cannot see.
rem
rem  What it does
rem    1. logs in to the box with the web password
rem    2. asks the box's own update channel what it runs and what is published
rem    3. shows the version pair and the release notes, then asks once
rem    4. tells the box to update itself (download -> verify -> back up ->
rem       stage over -> restart; a watchdog puts the old tree back if the new
rem       one never answers)
rem    5. follows the state until done/failed, waits for the box to come back,
rem       re-reads the version and says whether it actually changed
rem
rem  Everything happens ON the box: this script only speaks HTTP to it over the
rem  LAN. The internet is needed by the box itself, to fetch the package.
rem  Nothing is uploaded, and nothing changes that the box would not change
rem  from its own Settings page.
rem
rem  Usage
rem    double-click it,  or:  mrrc-update.cmd 192.168.1.50
rem
rem  Exit codes: 0 updated (or already latest) - 1 refused/failed - 2 no answer
rem ===========================================================================
setlocal DisableDelayedExpansion
title MRRC Modern - remote update

set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"
set "OUT=%HERE%\mrrc-update-%COMPUTERNAME%"
if exist "%OUT%" rd /s /q "%OUT%" >nul 2>&1
mkdir "%OUT%" >nul 2>&1
if not exist "%OUT%" goto :no_folder
set "TRACE=%OUT%\transcript.txt"

echo ============================================================
echo  MRRC Modern remote update
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
>>"%TRACE%" echo  Box   : %BASE%
echo  Box   : %BASE%
echo.

rem ----------------------------------------------------------------- login --
rem The password goes into one temporary JSON under %TEMP% (so it never shows
rem up in argv, where Task Manager and `ps` can see it) and is deleted at the
rem end of every path. It is never written into this folder.
set "MRRC_PW="
set /p "MRRC_PW=Web password (the one you open the page with): "
if not defined MRRC_PW goto :no_input

powershell -NoProfile -Command "$e=[System.Text.UTF8Encoding]::new($false); $j=@{password=$env:MRRC_PW}|ConvertTo-Json -Compress; [IO.File]::WriteAllText($env:TEMP+'\mrrc-update-login.json',$j,$e)" >nul 2>&1
curl -k -s -c "%TEMP%\mrrc-update-cookies.txt" -H "Content-Type: application/json" --data-binary "@%TEMP%\mrrc-update-login.json" "%BASE%/api/auth/login" -o "%TEMP%\mrrc-update-login-resp.json" --max-time 20
set "LOGINOK=no"
for /f "usebackq delims=" %%l in (`powershell -NoProfile -Command "try{if((Get-Content -Raw -Encoding UTF8 '%TEMP%\mrrc-update-login-resp.json'|ConvertFrom-Json).ok){'ok'}else{'no'}}catch{'no'}"`) do set "LOGINOK=%%l"
if not "%LOGINOK%"=="ok" goto :login_failed
echo  Logged in.
echo.

rem ----------------------------------------------------------------- check --
echo  Asking the box which version it runs and what is published...
curl -k -s -b "%TEMP%\mrrc-update-cookies.txt" "%BASE%/api/update/check" -o "%OUT%\check-before.json" --max-time 30
set "AVAIL=ERR"
for /f "usebackq delims=" %%a in (`powershell -NoProfile -Command "try{$j=Get-Content -Raw -Encoding UTF8 '%OUT%\check-before.json'|ConvertFrom-Json; if($j.available){'YES'}else{'NO'}}catch{'ERR'}"`) do set "AVAIL=%%a"
set "CURRENT="
for /f "usebackq delims=" %%c in (`powershell -NoProfile -Command "try{(Get-Content -Raw -Encoding UTF8 '%OUT%\check-before.json'|ConvertFrom-Json).current}catch{''}"`) do set "CURRENT=%%c"
set "LATEST="
for /f "usebackq delims=" %%c in (`powershell -NoProfile -Command "try{(Get-Content -Raw -Encoding UTF8 '%OUT%\check-before.json'|ConvertFrom-Json).latest}catch{''}"`) do set "LATEST=%%c"
powershell -NoProfile -Command "[Console]::OutputEncoding=[Text.Encoding]::UTF8; try{$j=Get-Content -Raw -Encoding UTF8 '%OUT%\check-before.json'|ConvertFrom-Json; '  current : ' + $j.current + $(if($j.current_fixpack){' / '+$j.current_fixpack}else{''}); '  latest  : ' + $j.latest + $(if($j.latest_fixpack){' / '+$j.latest_fixpack}else{''}); '  package : ' + [math]::Round($j.size/1MB,1) + ' MB'; '  notes   : ' + $j.notes}catch{'  (no update status in the answer)'}"
echo.

if /i "%AVAIL%"=="ERR" goto :check_failed
if not defined CURRENT goto :check_failed
if not defined LATEST goto :check_failed
if /i "%AVAIL%"=="NO" goto :already_latest
if /i not "%AVAIL%"=="YES" goto :check_failed

rem "There is a newer version" is not the same question as "this box can update
rem itself": /api/update/check has existed since 2026-09-17, the apply API only
rem since 2026-10-18. Ask the newer endpoint before the YES prompt, so an old box
rem stops with an explanation instead of a half-run that ends in "refused".
set "UPDAPI=000"
for /f "usebackq delims=" %%c in (`curl -k -s -b "%TEMP%\mrrc-update-cookies.txt" -o "%OUT%\state-probe.json" -w "%%{http_code}" --max-time 20 "%BASE%/api/update/state"`) do set "UPDAPI=%%c"
if not "%UPDAPI%"=="200" goto :old_build

:confirm
echo  Updating restarts the box service (about 10 seconds offline).
echo  Transmitting, tuning or a running CQ call makes the box refuse - it is
echo  checked on the box itself, right before it starts.
echo.
set "GO="
set /p "GO=Type YES to update %CURRENT% -> %LATEST%: "
if /i not "%GO%"=="YES" goto :aborted

rem The code page is switched only here, after the last `set /p`, on purpose:
rem `chcp 65001` invalidates a redirected stdin, so any prompt that follows it
rem reads an empty string (measured on Win11 cmd: two `set /p` with a file
rem redirected in, the second one empty). That is exactly the scripted/remote
rem run this tool exists for. The prompts are ASCII; the console code page only
rem matters from here on, where the box's own Chinese text gets printed.
chcp 65001 >nul 2>&1

rem ----------------------------------------------------------------- apply --
echo.
echo  Starting the update on the box...
curl -k -s -b "%TEMP%\mrrc-update-cookies.txt" -X POST "%BASE%/api/update/apply" -o "%OUT%\apply.json" --max-time 60
set "APPLYOK=no"
for /f "usebackq delims=" %%a in (`powershell -NoProfile -Command "try{if((Get-Content -Raw -Encoding UTF8 '%OUT%\apply.json'|ConvertFrom-Json).ok){'ok'}else{'no'}}catch{'no'}"`) do set "APPLYOK=%%a"
if not "%APPLYOK%"=="ok" goto :apply_refused
echo  Update started.
echo.

rem ------------------------------------------------------------------ poll --
rem Ask for the state every 4 seconds. During the restart curl cannot connect
rem at all - that is the box going down, not a failure.
set /a TICKS=0
set "LASTSTATE="
set "PREV="
:poll
ping -n 5 127.0.0.1 >nul
set /a TICKS+=1
if %TICKS% GEQ 150 goto :poll_timeout
curl -k -s -b "%TEMP%\mrrc-update-cookies.txt" "%BASE%/api/update/state" -o "%OUT%\state.json" --max-time 10
set "ST="
for /f "usebackq delims=" %%s in (`powershell -NoProfile -Command "try{(Get-Content -Raw -Encoding UTF8 '%OUT%\state.json'|ConvertFrom-Json).state}catch{''}"`) do set "ST=%%s"
if not defined ST goto :wait_back
if /i "%ST%"=="%LASTSTATE%" goto :poll
set "PREV=%LASTSTATE%"
set "LASTSTATE=%ST%"
>>"%TRACE%" echo  state: %ST%
echo  state: %ST%
powershell -NoProfile -Command "[Console]::OutputEncoding=[Text.Encoding]::UTF8; try{$d=(Get-Content -Raw -Encoding UTF8 '%OUT%\state.json'|ConvertFrom-Json).detail; if($d){'         ' + $d}}catch{}"
if /i "%ST%"=="failed" goto :apply_failed
if /i "%ST%"=="done" goto :wait_back
rem The state lives in memory: a poll that lands right after the restart sees a
rem fresh "idle" instead of the "done" it wrote a moment before. Only treat it
rem as "it restarted" once we have seen real progress - the first poll after
rem apply can legitimately be idle while the download is still starting.
if /i "%ST%"=="idle" if defined PREV goto :wait_back
goto :poll

rem -------------------------------------------- wait for the box to come back --
:wait_back
echo.
echo  Waiting for the box to come back...
set /a BACK=0
:back_loop
set /a BACK+=1
if %BACK% GEQ 60 goto :back_timeout
set "HC=000"
for /f "usebackq delims=" %%c in (`curl -k -s -o NUL -w "%%{http_code}" --max-time 6 "%BASE%/api/health"`) do set "HC=%%c"
rem Any HTTP answer means the service is up: /api/health is not a public path,
rem so a healthy box answers 401 there. 000 means nothing accepted the
rem connection yet. (Same rule the box's own watchdog uses, and the same one
rem launcher_net.answers() has always used.)
if not "%HC%"=="000" goto :verify
ping -n 4 127.0.0.1 >nul
goto :back_loop

rem ---------------------------------------------------------------- verify --
:verify
ping -n 3 127.0.0.1 >nul
rem Session tokens are persisted by the box (data/auth-tokens.json, 30 days),
rem so the cookie usually survives the restart. If it does not, log in once
rem more - "wrong password" must never be mistaken for "update failed".
curl -k -s -b "%TEMP%\mrrc-update-cookies.txt" "%BASE%/api/update/check" -o "%OUT%\check-after.json" --max-time 30
set "AFTER="
for /f "usebackq delims=" %%c in (`powershell -NoProfile -Command "try{(Get-Content -Raw -Encoding UTF8 '%OUT%\check-after.json'|ConvertFrom-Json).current}catch{''}"`) do set "AFTER=%%c"
if not defined AFTER goto :verify_relogin

echo.
echo  ============================================================
echo  Before : %CURRENT%
echo  After  : %AFTER%
echo  Target : %LATEST%
echo  ============================================================
echo.
set "VERDICT=old"
for /f "usebackq delims=" %%c in (`powershell -NoProfile -Command "if('%AFTER%' -eq '%LATEST%'){'ok'}else{'old'}"`) do set "VERDICT=%%c"
if /i "%VERDICT%"=="ok" goto :updated
echo  The box came back on %AFTER%, not %LATEST%.
echo  That is the box's own watchdog putting the previous version back: the new
echo  tree did not serve /api/health within 90 seconds of the restart.
echo  Its log says why (on the box, under the install directory):
echo.
echo    logs/server-stdout.log
echo.
echo  Files from this run: %OUT%
goto :cleanup_fail

:updated
>>"%TRACE%" echo  updated %CURRENT% -> %AFTER%
echo  Updated: %CURRENT% -^> %AFTER%. The box is serving again.
echo  Files from this run: %OUT%
goto :cleanup_ok

rem ---------------------------------------------------------------- errors --
:verify_relogin
rem Re-login at most once. Reading the status back must not turn into a loop.
if defined RETRIED goto :verify_unreadable
set "RETRIED=1"
curl -k -s -c "%TEMP%\mrrc-update-cookies.txt" -H "Content-Type: application/json" --data-binary "@%TEMP%\mrrc-update-login.json" "%BASE%/api/auth/login" -o "%TEMP%\mrrc-update-login-resp.json" --max-time 20
:verify_unreadable
curl -k -s -b "%TEMP%\mrrc-update-cookies.txt" "%BASE%/api/update/check" -o "%OUT%\check-after.json" --max-time 30
set "AFTER="
for /f "usebackq delims=" %%c in (`powershell -NoProfile -Command "try{(Get-Content -Raw -Encoding UTF8 '%OUT%\check-after.json'|ConvertFrom-Json).current}catch{''}"`) do set "AFTER=%%c"
if defined AFTER goto :verify
echo  The box answers, but the version could not be read back.
echo  Open the page and look at the version in its menu footer.
goto :cleanup_fail

:apply_refused
echo.
echo  The box refused to start the update:
powershell -NoProfile -Command "[Console]::OutputEncoding=[Text.Encoding]::UTF8; try{$j=Get-Content -Raw -Encoding UTF8 '%OUT%\apply.json'|ConvertFrom-Json; '  ' + $j.reason}catch{'  (no reason given)'}"
echo.
echo  Nothing was changed. Fix the reason above and run this again.
goto :cleanup_fail

:apply_failed
echo.
echo  The update failed on the box:
powershell -NoProfile -Command "[Console]::OutputEncoding=[Text.Encoding]::UTF8; try{(Get-Content -Raw -Encoding UTF8 '%OUT%\state.json'|ConvertFrom-Json).detail}catch{}"
echo.
echo  The box keeps running the version it had; nothing else was changed.
goto :cleanup_fail

:poll_timeout
echo.
echo  Gave up waiting for a final state after 10 minutes.
echo  Check the box's page - it may have finished after all.
goto :cleanup_fail

:back_timeout
echo.
echo  The box did not answer within 4 minutes of the restart.
echo    1. Is it still powered on and on the same network?
echo    2. Open %BASE% in a browser.
echo  If the new version cannot serve, the box restores the previous one by
echo  itself within 90 seconds of the restart.
goto :cleanup_fail

:old_build
echo.
echo  This box has no remote updater: its build is from before 2026-10-18,
echo  which is when the settings card and this API arrived. Nothing was changed.
echo.
echo  How such a box is updated instead:
echo    - reflash the image (works from any version), or
echo    - on the box over SSH:  mrrc-update
echo.
echo  Careful with the second one: builds up to 1.25.5 ship a mrrc-update that
echo  syncs with --delete, and that wipes env/, profiles/ and the recordings.
echo  Replace that script first, or copy the whole directory somewhere safe.
goto :cleanup_fail

:check_failed
echo.
echo  The box did not report a version and an update status.
echo    - A build older than 2026-10-18 has no in-browser updater at all;
echo      those boxes go through the USB / mrrc-update path instead.
echo    - Otherwise the request itself failed. The raw answer is here:
echo      %OUT%\check-before.json
goto :cleanup_fail

:already_latest
echo  Already up to date: %CURRENT%. Nothing to do.
powershell -NoProfile -Command "[Console]::OutputEncoding=[Text.Encoding]::UTF8; try{$r=(Get-Content -Raw -Encoding UTF8 '%OUT%\check-before.json'|ConvertFrom-Json).reason; if($r){'  note: ' + $r}}catch{}"
goto :cleanup_ok

:aborted
echo.
echo  Nothing was changed.
goto :cleanup_ok

:login_failed
echo.
echo  Login failed. Check the web password (it is the one the page asks for).
powershell -NoProfile -Command "[Console]::OutputEncoding=[Text.Encoding]::UTF8; try{(Get-Content -Raw -Encoding UTF8 '%TEMP%\mrrc-update-login-resp.json'|ConvertFrom-Json).error}catch{'no answer from the box'}"
goto :cleanup_fail

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

:no_box
echo.
echo  No answer from %BOX%.
echo    1. Is the box switched on, and on the same network?
echo    2. Open the web page in a browser first - if that works, the IP is right.
goto :end

:cleanup_ok
del "%TEMP%\mrrc-update-login.json" "%TEMP%\mrrc-update-cookies.txt" "%TEMP%\mrrc-update-login-resp.json" >nul 2>&1
echo.
pause
exit /b 0

:cleanup_fail
del "%TEMP%\mrrc-update-login.json" "%TEMP%\mrrc-update-cookies.txt" "%TEMP%\mrrc-update-login-resp.json" >nul 2>&1
echo.
pause
exit /b 1

:end
pause
exit /b 2
