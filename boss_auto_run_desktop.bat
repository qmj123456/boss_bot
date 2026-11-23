@echo off
chcp 65001 >nul
setlocal

REM 自动定位项目目录（bat 文件所在目录）
set "PROJECT_DIR=%~dp0"
if "%PROJECT_DIR:~-1%"=="\" set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"

if not exist "%PROJECT_DIR%\tools\scheduled_boss_run.py" (
    echo [BOSS自动化] ERROR: 找不到项目目录: %PROJECT_DIR%
    echo 请确认 boss_auto_run_desktop.bat 位于项目根目录。
    pause
    exit /b 1
)

cd /d "%PROJECT_DIR%"

echo ============================================================
echo  BOSS 自动化运行（桌面启动器）
echo  项目目录: %CD%
echo  开始时间: %date% %time%
echo ============================================================
echo.

python -X utf8 tools\scheduled_boss_run.py --allow-auto-confirm

set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo ============================================================
echo  运行结束，退出码: %EXIT_CODE%
echo  结束时间: %date% %time%
echo  结果文件: %PROJECT_DIR%\data\scheduler\latest_run.json
echo ============================================================

pause
exit /b %EXIT_CODE%
