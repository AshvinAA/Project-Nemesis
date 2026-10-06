@echo off
setlocal
rem ============================================================
rem  PROJECT NEMESIS one-click launcher
rem  Double-click me: starts the game + the live dashboard side
rem  by side and opens the dashboard in your browser. No PowerShell.
rem
rem  Modes (also usable from a terminal or extra shortcuts):
rem    (none)      game (hostile buddy) + live dashboard + browser
rem    gameonly    just the game (hostile buddy, instant respawns)
rem    normal      game with the buddy as your ALLY + dashboard
rem    agent       game + full RL training agent + its dashboard
rem    dashboard   dashboard only (replays the last session's files)
rem    reset       wipe jev's memory (buddy skill back to clueless)
rem    reset all   also wipe the RL agent's qtable
rem    kill        close the game, dashboard and agent windows
rem    help        this text
rem ============================================================
title Project Nemesis Launcher
set "ROOT=%~dp0"
cd /d "%ROOT%"

set "EXE=BuddyDoom\run\buddydoom.exe"
set "RUNDIR=%ROOT%BuddyDoom\run"
set "WAD=freedoom1.wad"
set "DASHURL=http://127.0.0.1:8787"

if "%~1"=="" goto full
if /i "%~1"=="gameonly"  goto gameonly
if /i "%~1"=="normal"    goto normal
if /i "%~1"=="agent"     goto agent
if /i "%~1"=="dashboard" goto dashboard
if /i "%~1"=="reset"     goto reset
if /i "%~1"=="kill"      goto kill
goto help

:checkexe
if not exist "%EXE%" (
    echo  [ERROR] %EXE% not found.
    echo  Build it first:  cmd /c tools\build_buddydoom.bat
    pause
    exit /b 1
)
where python >nul 2>nul
if errorlevel 1 (
    echo  [ERROR] python not found on PATH - the dashboard needs it.
    pause
    exit /b 1
)
exit /b 0

:startgame
rem %1 = extra engine flags (quoted, already expanded by caller)
start "BuddyDoom" /D "%RUNDIR%" "%ROOT%%EXE%" -iwad %WAD% -warp 1 1 -skill 3 %~1
exit /b 0

:cleanslate
rem  Kill stale games / dashboards / agents so a fresh launch owns the
rem  ports. Without this, an old --live dashboard keeps :8787 AND the
rem  engine's single :31666 observer slot - the new dashboard then
rem  serves a frozen snapshot (Windows silently double-binds the port)
rem  and the page looks dead while you play.
taskkill /F /IM buddydoom.exe >nul 2>nul
taskkill /F /T /FI "WINDOWTITLE eq Nemesis*" >nul 2>nul
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'nemesis\\.' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>nul
rem  give the OS a moment to release :31666 / :8787
timeout /t 2 /nobreak >nul
exit /b 0

:full
call :checkexe || exit /b 1
call :cleanslate
call :startgame "-nomonsters -buddyhostile -aidirector 31666"
start "Nemesis Dashboard" /MIN cmd /c "python -m nemesis.dashboard --live"
powershell -NoProfile -Command "$ok=$false; foreach($i in 1..20){ try{ Invoke-WebRequest -UseBasicParsing -TimeoutSec 1 '%DASHURL%/state' | Out-Null; $ok=$true; break }catch{ Start-Sleep -Milliseconds 300 } }; if($ok){ Start-Process '%DASHURL%' } else { Write-Host ' [WARN] dashboard did not come up on %DASHURL%' }"
echo.
echo  [OK] Game is starting - hostile buddy, instant respawns.
echo  [OK] Dashboard: %DASHURL%  (window minimized to the taskbar)
echo       Watch jev's weights drift live while you play!
echo  Tip: the dashboard window must stay open; close the game when
echo       done, or run:  Nemesis kill
timeout /t 8 >nul
exit /b 0

:gameonly
call :checkexe || exit /b 1
call :cleanslate
call :startgame "-nomonsters -buddyhostile -aidirector 31666"
echo.
echo  [OK] Game is starting - hostile buddy, instant respawns.
timeout /t 5 >nul
exit /b 0

:normal
call :checkexe || exit /b 1
call :cleanslate
call :startgame "-aidirector 31666"
start "Nemesis Dashboard" /MIN cmd /c "python -m nemesis.dashboard --live"
powershell -NoProfile -Command "$ok=$false; foreach($i in 1..20){ try{ Invoke-WebRequest -UseBasicParsing -TimeoutSec 1 '%DASHURL%/state' | Out-Null; $ok=$true; break }catch{ Start-Sleep -Milliseconds 300 } }; if($ok){ Start-Process '%DASHURL%' } else { Write-Host ' [WARN] dashboard did not come up on %DASHURL%' }"
echo.
echo  [OK] Game is starting - buddy is your ally, monsters enabled.
echo  [OK] Dashboard: %DASHURL%
timeout /t 8 >nul
exit /b 0

:agent
call :checkexe || exit /b 1
call :cleanslate
call :startgame "-nomonsters -buddyhostile -aidirector 31666"
start "Nemesis Agent" /MIN cmd /c "python -m nemesis.rl_agent"
timeout /t 3 /nobreak >nul
start "" %DASHURL%
echo.
echo  [OK] Game + RL agent starting (dashboard comes up with it).
echo       The agent owns the :31666 slot - do NOT also run the
echo       dashboard in --live mode while the agent runs.
timeout /t 8 >nul
exit /b 0

:dashboard
where python >nul 2>nul || (echo  [ERROR] python not found on PATH & pause & exit /b 1)
call :cleanslate
start "Nemesis Dashboard" cmd /c "python -m nemesis.dashboard"
timeout /t 2 /nobreak >nul
start "" %DASHURL%
echo  [OK] Dashboard replaying the last session from disk.
timeout /t 5 >nul
exit /b 0

:reset
if exist "BuddyDoom\run\nemesis_memory.dat" (
    del /q "BuddyDoom\run\nemesis_memory.dat"
    echo  [OK] Wiped BuddyDoom\run\nemesis_memory.dat - jev forgets,
    echo       buddy skill back to clueless. Restart the game to apply.
) else (
    echo  [i] No memory file present - already fresh.
)
if /i "%~2"=="all" (
    if exist "nemesis\rl\qtable.json" del /q "nemesis\rl\qtable.json"
    echo  [OK] Also wiped the RL agent's qtable.
)
timeout /t 5 >nul
exit /b 0

:kill
taskkill /F /IM buddydoom.exe >nul 2>nul
taskkill /F /T /FI "WINDOWTITLE eq Nemesis*" >nul 2>nul
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'nemesis\\.' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>nul
echo  [OK] Closed the game and any Nemesis dashboard/agent windows.
timeout /t 3 >nul
exit /b 0

:help
echo.
echo  Project Nemesis launcher - usage:
echo     Nemesis.bat            game + live dashboard + browser
echo     Nemesis.bat gameonly   just the game
echo     Nemesis.bat normal     ally buddy + monsters + dashboard
echo     Nemesis.bat agent      game + RL training agent + dashboard
echo     Nemesis.bat dashboard  dashboard only (last session's files)
echo     Nemesis.bat reset      wipe jev's memory   (reset all = + qtable)
echo     Nemesis.bat kill       close everything
echo.
timeout /t 10 >nul
exit /b 0
