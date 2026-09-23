@echo off
setlocal
cd /d "%~dp0"

if defined MYBOT_PYTHON goto configured_python
if exist "%~dp0.venv\Scripts\python.exe" (
    set "MYBOT_PYTHON=%~dp0.venv\Scripts\python.exe"
    goto configured_python
)
if exist "%~dp0venv\Scripts\python.exe" (
    set "MYBOT_PYTHON=%~dp0venv\Scripts\python.exe"
    goto configured_python
)
set "MYBOT_PYTHON=python"

:configured_python
"%MYBOT_PYTHON%" "%~dp0start_cli.py" %*
set "MYBOT_EXIT_CODE=%ERRORLEVEL%"
if not "%MYBOT_EXIT_CODE%"=="0" (
    echo.
    echo Startup or CLI failed. Check the error above and data\log.
    echo Install requirements.txt and configure config\.env before starting.
    pause
)
exit /b %MYBOT_EXIT_CODE%
