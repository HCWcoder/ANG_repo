@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo The project Python environment is missing.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m anghami_session.ui_server
if errorlevel 1 pause
