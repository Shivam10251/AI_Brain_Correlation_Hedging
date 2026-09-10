@echo off
REM Double-clickable launcher. Starts the server detached so it keeps
REM running after this window closes.

cd /d "%~dp0"

powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0start_background.ps1"

echo.
echo Server starting in the background.
echo Dashboard: http://127.0.0.1:8000
echo Log:       %~dp0server.log
echo.
pause
