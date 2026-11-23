@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM BOSS daemon: loop each job until daily_greet_target met or work_time.end
REM Supports time_window per job in app.yaml (morning/evening shift)
REM jobs/count/max_greetings/time_window controlled by config\app.yaml scheduler.jobs

REM Use PowerShell to get timestamp (wmic is deprecated on Win11 24H2)
for /f "delims=" %%t in ('powershell -NoProfile -Command "Get-Date -Format 'yyyyMMdd_HHmmss'"') do set "TIMESTAMP=%%t"
set LOG_DIR=%CD%\logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set LOG_FILE=%LOG_DIR%\daemon_%TIMESTAMP%.log

echo ============================================================
echo  BOSS daemon (run until target met or off-work)
echo  cwd: %CD%
echo  log: %LOG_FILE%
echo  start: %date% %time%
echo ============================================================
echo.

REM Use cmd native redirection: 1> log 2>&1 avoids PS NativeCommandError wrapping
python -X utf8 tools\scheduled_boss_run.py --allow-auto-confirm --daemon 1>"%LOG_FILE%" 2>&1

set EXIT_CODE=%ERRORLEVEL%
echo.
echo ============================================================
echo  daemon ended, exit code: %EXIT_CODE%
echo  log saved: %LOG_FILE%
echo  end: %date% %time%
echo ============================================================

if "%1"=="--no-pause" goto :eof
pause
