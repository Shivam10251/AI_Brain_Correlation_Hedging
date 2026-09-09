@echo off
REM ---------------------------------------------------------------
REM Start the AI Hedge Fund engine on the Windows trading host.
REM
REM Phase 1 fixes (Phase 0 §2.1, §2.10):
REM   * module path was "main:app"; the package moved to backend/
REM     in commit 368355b, so this launcher had been broken since.
REM   * the install directory was hardcoded to one machine.
REM   * it bound 0.0.0.0, exposing an unauthenticated control plane.
REM     It now binds loopback unless you set API_TOKEN and
REM     ALLOW_INSECURE_BIND=true deliberately.
REM ---------------------------------------------------------------

setlocal

REM Run from this script's own directory, wherever it is checked out.
cd /d "%~dp0"

REM Support either venv layout without guessing.
if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
) else if exist "venv\Scripts\activate.bat" (
    call "venv\Scripts\activate.bat"
) else (
    echo [ERROR] No virtual environment found ^(.venv\ or venv\^).
    echo         Create one with:  python -m venv venv
    exit /b 1
)

REM MetaTrader5 is required on the trading host only.
python -c "import MetaTrader5" 2>nul
if errorlevel 1 (
    echo [ERROR] MetaTrader5 is not installed in this environment.
    echo         pip install MetaTrader5==5.0.4874
    exit /b 1
)

if "%HOST%"=="" set HOST=127.0.0.1
if "%PORT%"=="" set PORT=8000

echo Starting engine on %HOST%:%PORT%
echo Dashboards:  http://127.0.0.1:%PORT%/  and  /dashboard

python -m uvicorn backend.main:app --host %HOST% --port %PORT%

endlocal
