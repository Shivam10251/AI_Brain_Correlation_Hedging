@echo off
REM Double-clickable stopper.

cd /d "%~dp0"

powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0stop_background.ps1"

echo.
pause
