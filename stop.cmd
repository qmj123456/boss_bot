@echo off
REM ============================================================
REM stop.cmd - stop running BOSS automation scripts by PID
REM
REM Usage:
REM   stop.cmd            list detected processes, confirm, stop all
REM   stop.cmd /list      list PIDs only
REM   stop.cmd 1234       stop only PID 1234 (stop.cmd 1234 5678)
REM   stop.cmd /y         stop all without confirmation
REM
REM Only processes belonging to this project are matched
REM (command line contains the project root or a project script name),
REM so other python programs on the machine are left alone.
REM The dedicated Edge window on port 9222 is NOT closed.
REM ============================================================
setlocal
chcp 65001 >nul

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

set "PS1=%~dp0tools\stop_boss.ps1"
if not exist "%PS1%" (
    echo [stop] ERROR: %PS1% not found.
    exit /b 1
)

if /i "%~1"=="/list" goto :list
if /i "%~1"=="-list" goto :list
if /i "%~1"=="/y" goto :yes
if /i "%~1"=="/force" goto :yes
if "%~1"=="" goto :all
goto :byid

:list
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -Root "%ROOT%" -List
exit /b %ERRORLEVEL%

:yes
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -Root "%ROOT%" -Force
exit /b %ERRORLEVEL%

:all
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -Root "%ROOT%"
exit /b %ERRORLEVEL%

:byid
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -Root "%ROOT%" -Id %*
exit /b %ERRORLEVEL%
