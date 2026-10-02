@echo off
setlocal
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" -m anghami_session login
set "session_exit_code=%errorlevel%"
pause
exit /b %session_exit_code%
