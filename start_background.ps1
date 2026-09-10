# =============================================================
# Start the AI Hedge Fund server as a detached background process.
#
# Uses WMI's Win32_Process.Create rather than Start-Process. The
# difference matters: a process spawned by Start-Process is a child of
# this PowerShell session and can be torn down with it when the
# terminal, the SSH session or the RDP connection closes. WMI creates
# the process under WmiPrvSE.exe instead, so it has no parent
# relationship to the shell that asked for it and survives logout.
#
# That is what makes 24/7 operation actually 24/7.
# =============================================================

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BatchFile  = Join-Path $ProjectDir "run_server.bat"

if (-not (Test-Path $BatchFile)) {
    Write-Host "[SYSTEM] run_server.bat not found at $BatchFile" -ForegroundColor Red
    exit 1
}

# Refuse to start a second engine. Two loops on one account would
# double every position and race each other's duplicate guard.
$Existing = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like "*main.py*" }

if ($Existing) {
    Write-Host "[SYSTEM] Already running (PID $($Existing.ProcessId))." -ForegroundColor Yellow
    Write-Host "         Run stop_background.ps1 first if you want to restart it."
    exit 0
}

Write-Host "[SYSTEM] Starting server detached from this session..." -ForegroundColor Cyan

$Result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine      = "cmd.exe /c `"$BatchFile`""
    CurrentDirectory = $ProjectDir
}

if ($Result.ReturnValue -eq 0) {
    Write-Host "[SYSTEM] Started. PID $($Result.ProcessId)" -ForegroundColor Green
    Write-Host "[SYSTEM] Dashboard: http://127.0.0.1:8000"
    Write-Host "[SYSTEM] Log:       $(Join-Path $ProjectDir 'server.log')"
    Write-Host ""
    Write-Host "         The engine starts STOPPED. Open the dashboard and"
    Write-Host "         press Start Engine when you want it trading."
}
else {
    # 2 = access denied, 9 = path not found, 21 = invalid parameter
    Write-Host "[SYSTEM] Failed to start. WMI return code: $($Result.ReturnValue)" -ForegroundColor Red
    exit 1
}
