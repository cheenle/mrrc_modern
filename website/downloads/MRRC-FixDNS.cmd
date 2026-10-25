@echo off
rem ===========================================================================
rem  MRRC Modern - repair the box's DNS, from a Windows PC
rem
rem  Use this on a box that answers on the local network but cannot resolve
rem  names: the page opens, `ping <ip>` works, `ping <name>` hangs for seconds
rem  and then fails, and the box cannot reach the portal / NTP / updates.
rem
rem  Why it happens (measured 2026-10-25 on a delivered box, MRRC-BG1SB):
rem    -rw-r--r-- 1 root root 97 Oct 11 11:58 /etc/resolv.conf
rem    # DNS requests are forwarded to the host. DHCP DNS options are ignored.
rem    nameserver 192.168.65.7          <- the machine the IMAGE was built on
rem  192.168.65.7 is Docker Desktop's internal resolver. The build lends the
rem  chroot its /etc/resolv.conf so apt/pip can resolve, and the base image's
rem  `-> ../run/systemd/resolve/stub-resolv.conf` symlink is never put back, so
rem  every box carries an address that exists on no customer LAN. glibc reads
rem  that file, waits its 5 s timeout per lookup and fails - while the network
rem  itself is perfectly fine.
rem
rem  What it does
rem    1. logs in to the box with the WEB password (no SSH, no root password)
rem    2. asks the box's own root helper to run linux/fix_resolv_conf.sh
rem       (the same script `mrrc-update` runs: idempotent, backs the file up,
rem       points /etc/resolv.conf at the resolved stub and restarts resolved
rem       so the old global server is dropped)
rem    3. prints what it found, what it changed, and runs it a second time to
rem       prove it is now a no-op
rem
rem  Usage
rem    double-click it,  or:  mrrc-fix-dns.cmd 192.168.1.50
rem
rem  Exit codes: 0 repaired (or already correct) - 1 refused/failed - 2 no answer
rem ===========================================================================
setlocal DisableDelayedExpansion
title MRRC Modern - repair DNS

set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"
set "OUT=%HERE%\mrrc-fix-dns-%COMPUTERNAME%"
if exist "%OUT%" rd /s /q "%OUT%" >nul 2>&1
mkdir "%OUT%" >nul 2>&1
if not exist "%OUT%" goto :no_folder
set "TRACE=%OUT%\transcript.txt"
set "COOKIES=%OUT%\cookies.txt"

echo ============================================================
echo  MRRC Modern - repair the box's DNS
echo ============================================================
echo.

rem ---------------------------------------------------------------- target --
set "BOX=%~1"
if not defined BOX set /p "BOX=Box IP address (as in the browser, e.g. 192.168.1.50, or mrrc.local): "
if not defined BOX goto :no_input
set "BOX=%BOX:https://=%"
set "BOX=%BOX:http://=%"
set "BOX=%BOX:/=%"
if not defined BOX goto :no_input

where curl >nul 2>&1
if errorlevel 1 goto :no_curl

rem Which scheme answers? 401 counts: any HTTP answer means the endpoint lives.
set "BASE=https://%BOX%"
for /f "usebackq delims=" %%c in (`curl -k -s -o NUL -w "%%{http_code}" --max-time 6 "https://%BOX%/login"`) do set "CODE=%%c"
if "%CODE%"=="000" set "BASE=http://%BOX%"
if "%CODE%"=="000" (
    for /f "usebackq delims=" %%c in (`curl -s -o NUL -w "%%{http_code}" --max-time 6 "http://%BOX%/login"`) do set "CODE=%%c"
)
if "%CODE%"=="000" goto :no_answer
echo Box answered at %BASE% (HTTP %CODE%)
echo.

rem ----------------------------------------------------------------- login --
set "MRRC_PW="
set /p "MRRC_PW=Web password (the one you open the page with): "
if not defined MRRC_PW goto :no_input
powershell -NoProfile -Command "$e=[System.Text.UTF8Encoding]::new($false); $j=@{password=$env:MRRC_PW}|ConvertTo-Json -Compress; [IO.File]::WriteAllText($env:TEMP+'\mrrc-fixdns-login.json',$j,$e)" >nul 2>&1
curl -k -s -c "%COOKIES%" -H "Content-Type: application/json" --data-binary "@%TEMP%\mrrc-fixdns-login.json" "%BASE%/api/auth/login" -o "%OUT%\login.json" --max-time 20
del "%TEMP%\mrrc-fixdns-login.json" >nul 2>&1
for /f "usebackq delims=" %%l in (`powershell -NoProfile -Command "try{if((Get-Content -Raw -Encoding UTF8 '%OUT%\login.json'|ConvertFrom-Json).ok){'ok'}else{'no'}}catch{'no'}"`) do set "LOGINOK=%%l"
if not "%LOGINOK%"=="ok" goto :login_failed
for /f "usebackq delims=" %%r in (`powershell -NoProfile -Command "try{(Get-Content -Raw -Encoding UTF8 '%OUT%\login.json'|ConvertFrom-Json).role}catch{''}"`) do set "ROLE=%%r"
echo Logged in as %ROLE%.
if not "%ROLE%"=="admin" goto :not_admin
echo.

rem ------------------------------------------------------------------ check --
echo --- asking the box to check and repair /etc/resolv.conf ---
for /f "usebackq delims=" %%c in (`curl -k -s -b "%COOKIES%" -X POST "%BASE%/api/box/fix-dns" -o "%OUT%\fix.json" -w "%%{http_code}" --max-time 60`) do set "FIXAPI=%%c"
if "%FIXAPI%"=="404" goto :too_old
if "%FIXAPI%"=="401" goto :session_lost

powershell -NoProfile -Command "[Console]::OutputEncoding=[Text.Encoding]::UTF8; try{$j=Get-Content -Raw -Encoding UTF8 '%OUT%\fix.json'|ConvertFrom-Json; if($j.output){$j.output.TrimEnd()}; if(-not $j.ok){'FAILED: ' + $j.error}}catch{'FAILED: unreadable answer from the box'}"
echo.

for /f "usebackq delims=" %%a in (`powershell -NoProfile -Command "try{if((Get-Content -Raw -Encoding UTF8 '%OUT%\fix.json'|ConvertFrom-Json).ok){'ok'}else{'no'}}catch{'no'}"`) do set "FIXOK=%%a"
if not "%FIXOK%"=="ok" goto :fix_failed

rem --------------------------------------------------------------- re-check --
rem The second run must say "already a symlink". That is the proof the repair
rem took, without asking the operator to interpret resolvectl output.
echo --- running it once more: it must be a no-op now ---
curl -k -s -b "%COOKIES%" -X POST "%BASE%/api/box/fix-dns" -o "%OUT%\fix2.json" --max-time 60 >nul 2>&1
powershell -NoProfile -Command "[Console]::OutputEncoding=[Text.Encoding]::UTF8; try{$j=Get-Content -Raw -Encoding UTF8 '%OUT%\fix2.json'|ConvertFrom-Json; if($j.output){$j.output.TrimEnd()}}catch{}"
echo.
for /f "usebackq delims=" %%t in (`powershell -NoProfile -Command "try{$o=(Get-Content -Raw -Encoding UTF8 '%OUT%\fix2.json'|ConvertFrom-Json).output; if($o -match 'already a symlink'){'idempotent'}else{'other'}}catch{'other'}"`) do set "IDEM=%%t"

echo ============================================================
if "%IDEM%"=="idempotent" (
    echo  DONE - the box now resolves through systemd-resolved.
    echo  Check it from the box:  getent hosts portal.mrrc.vlsc.net
    echo  If a name still fails, the box is not reaching its router; that is
    echo  a different problem [see the W103D guide, DNS section].
) else (
    echo  The repair ran, but the second pass did not say "already a symlink".
    echo  Look at the two answers above - the box may have no systemd-resolved
    echo  (in which case nothing was changed on purpose).
)
echo ============================================================
rem The success path removes its scratch folder (cookies + two JSON files). The
rem failure paths keep it, because :fail prints the transcript path - that file is
rem the only thing an operator can hand back when they come to ask us.
curl -k -s -b "%COOKIES%" -X POST "%BASE%/api/auth/logout" >nul 2>&1
rd /s /q "%OUT%" >nul 2>&1
endlocal & exit /b 0

rem ------------------------------------------------------------------ exits --
:too_old
echo.
echo  This box predates the repair: it does not know /api/box/fix-dns yet.
echo  Nothing was changed on the box.
echo.
echo  Send this screen to the seller / maintainer - they have a way to repair
echo  this box that does not involve you typing any Linux commands. You do not
echo  need to do anything else here.
goto :fail

:not_admin
echo.
echo  This session is a listen-only session (MRRC_LISTEN_PASSWORD). The repair
echo  needs the admin password, the same one the Settings page asks for.
goto :fail

:fix_failed
echo  The repair was refused or failed - see the message above.
echo  Common cause: the box has no systemd-resolved, so nothing was changed.
goto :fail

:session_lost
echo  The session expired during the repair. Run the tool again.
goto :fail

:login_failed
echo.
echo  Login refused. Check the web password (case matters). If the box answers
echo  with a role other than admin, use the admin password.
goto :fail

:no_answer
echo.
echo  No answer from %BOX% on either https or http.
echo   - is the box on this network? (ping %BOX%)
echo   - is the address the one in the browser's address bar?
goto :fail

:no_curl
echo.
echo  curl was not found. It ships with Windows 10 (1803) and later; on an
echo  older system install it or run this tool on another machine.
goto :fail

:no_input
echo.
echo  Nothing entered - nothing done.
goto :fail

:no_folder
echo.
echo  Could not create a working folder next to this script.
echo  Copy it somewhere you can write (Desktop is fine) and run it again.
goto :fail

:fail
echo.
echo  Transcript kept at: %TRACE%
echo.
pause
endlocal & exit /b 1
