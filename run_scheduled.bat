@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM BOSS automation (config-driven)
REM jobs/count/max_greetings are controlled by config\app.yaml scheduler.jobs

echo ============================================================
echo  BOSS automation (config\app.yaml)
echo  cwd: %CD%
echo  start: %date% %time%
echo ============================================================
echo.

python -X utf8 tools\scheduled_boss_run.py --allow-auto-confirm

set EXIT_CODE=%ERRORLEVEL%
echo.
echo ============================================================
echo  done, exit code: %EXIT_CODE%
echo  end: %date% %time%
echo ============================================================

if "%1"=="--no-pause" goto :eof

pause
