@echo off
title GTA 6 Clip Agent - setup
cd /d "%~dp0"
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 (
    echo Python 3.10 or newer is needed. Install it from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during the install. Then run setup.bat again.
    pause
    exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
    echo Making a private Python environment in .venv ...
    %PY% -m venv .venv || (echo Couldn't make the environment. & pause & exit /b 1)
)
echo Installing what the agent needs (a few minutes the first time) ...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (echo The install failed: see the messages above. & pause & exit /b 1)
if not exist ".env" copy ".env.example" ".env" >nul
echo Checking everything works ...
".venv\Scripts\python.exe" -m gta6 --selftest
if errorlevel 1 (echo The self-test failed: see the messages above. & pause & exit /b 1)
echo.
echo All set. Double-click start.bat to open the GTA 6 Clip Agent.
pause
