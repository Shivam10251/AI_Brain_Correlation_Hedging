# =============================================================
# Stop the AI Hedge Fund server.
#
# Finds it two ways, because either can miss on its own:
#   1. python.exe processes whose command line mentions main.py
#   2. whatever is listening on port 8000
#
# Asks politely with CloseMainWindow first and only escalates to a
# hard kill if the process ignores it. An open MT5 order request is
# better allowed to finish than interrupted mid-flight.
# =============================================================

$ErrorActionPreference = "SilentlyContinue"

$Stopped = @()

# --- 1. python.exe running main.py ---------------------------
$PythonProcs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like "*main.py*" }

foreach ($Proc in $PythonProcs) {
    Write-Host "[SYSTEM] Stopping python.exe PID $($Proc.ProcessId)..." -ForegroundColor Cyan

    $Handle = Get-Process -Id $Proc.ProcessId

    if ($Handle) {
        $Handle.CloseMainWindow() | Out-Null

        if (-not $Handle.WaitForExit(5000)) {
            Write-Host "         Did not exit; forcing." -ForegroundColor Yellow
            Stop-Process -Id $Proc.ProcessId -Force
        }

        $Stopped += $Proc.ProcessId
    }
}

# --- 2. the cmd.exe wrapper ----------------------------------
$CmdProcs = Get-CimInstance Win32_Process -Filter "Name = 'cmd.exe'" |
    Where-Object { $_.CommandLine -like "*run_server.bat*" }

foreach ($Proc in $CmdProcs) {
    Write-Host "[SYSTEM] Stopping cmd.exe PID $($Proc.ProcessId)..." -ForegroundColor Cyan
    Stop-Process -Id $Proc.ProcessId -Force
    $Stopped += $Proc.ProcessId
}

# --- 3. anything still holding port 8000 ---------------------
$Listeners = Get-NetTCPConnection -LocalPort 8000 -State Listen

foreach ($Conn in $Listeners) {
    if ($Stopped -notcontains $Conn.OwningProcess) {
        $Owner = Get-Process -Id $Conn.OwningProcess

        if ($Owner) {
            Write-Host "[SYSTEM] Port 8000 still held by $($Owner.ProcessName) PID $($Owner.Id); stopping." -ForegroundColor Yellow
            Stop-Process -Id $Owner.Id -Force
            $Stopped += $Owner.Id
        }
    }
}

if ($Stopped.Count -eq 0) {
    Write-Host "[SYSTEM] Nothing was running." -ForegroundColor Yellow
}
else {
    Write-Host "[SYSTEM] Stopped $($Stopped.Count) process(es)." -ForegroundColor Green
    Write-Host "         Any open MT5 positions are UNTOUCHED and still live."
}
