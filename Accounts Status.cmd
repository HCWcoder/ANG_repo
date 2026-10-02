@echo off
setlocal
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" main.py accounts status
set "account_exit_code=%errorlevel%"
pause
exit /b %account_exit_code%
