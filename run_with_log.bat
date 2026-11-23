@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM BOSS automation with logging (config-driven)
REM jobs/count/max_greetings are controlled by config\app.yaml scheduler.jobs
REM stdout+stderr to screen and logs\scheduled_<timestamp>.log

REM build timestamp log filename
for /f "tokens=2 delims==" %%a in ('wmic OS Get localdatetime /value') do set "dt=%%a"
set TIMESTAMP=%dt:~0,8%_%dt:~8,6%
set LOG_DIR=%CD%\logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set LOG_FILE=%LOG_DIR%\scheduled_%TIMESTAMP%.log

echo ============================================================
echo  BOSS automation (config\app.yaml, with log)
echo  cwd: %CD%
echo  log: %LOG_FILE%
echo  start: %date% %time%
echo ============================================================
echo.

REM PowerShell Tee-Object: stdout+stderr to screen and file
powershell -Command "python -X utf8 tools\scheduled_boss_run.py --allow-auto-confirm 2>&1 | Tee-Object -FilePath '%LOG_FILE%'"

set EXIT_CODE=%ERRORLEVEL%
echo.
echo ============================================================
echo  done, exit code: %EXIT_CODE%
echo  log saved: %LOG_FILE%
echo  end: %date% %time%
echo ============================================================

if "%1"=="--no-pause" goto :eof
pause
