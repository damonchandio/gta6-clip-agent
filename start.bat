@echo off
title GTA 6 Clip Agent
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run setup.bat first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m gta6 %*
if errorlevel 1 pause
