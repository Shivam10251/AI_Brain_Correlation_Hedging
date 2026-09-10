@echo off
REM ---------------------------------------------------------------
REM Launch the AI Hedge Fund server, appending to server.log.
REM Called by start_background.ps1; can also be run directly to
REM watch the output in a console.
REM ---------------------------------------------------------------

cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" main.py >> server.log 2>&1
) else if exist "venv\Scripts\python.exe" (
    "venv\Scripts\python.exe" main.py >> server.log 2>&1
) else (
    echo [SYSTEM] No virtual environment found ^(.venv\ or venv\^). >> server.log
    echo         Create one with:  python -m venv .venv           >> server.log
    exit /b 1
)
