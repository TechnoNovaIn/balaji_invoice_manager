# Run as Administrator on the target Windows 11 production server.
# Registers BalajiInvoiceHub (Waitress) via NSSM and schedules daily backups
# and weekly sandbox restore verification drills. Startup OCR recovery is
# executed once on boot by serve_production.py.
param(
    [string]$ProjectRoot = "C:\Apps\balaji_invoice_manager",
    [string]$NssmPath = "C:\Tools\nssm\nssm.exe",
    [int]$ServiceWaitTimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"
$ServiceName = "BalajiInvoiceHub"
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$ServeScript = Join-Path $ProjectRoot "serve_production.py"
$LogDir = Join-Path $ProjectRoot "logs"

if (-not (Test-Path $PythonExe)) {
    throw "Virtual environment Python not found at $PythonExe. Create .venv first."
}
if (-not (Test-Path $ServeScript)) {
    throw "Production server entrypoint not found at $ServeScript."
}
if (-not (Test-Path $NssmPath)) {
    throw "NSSM not found at $NssmPath. Extract nssm.exe before installing the Windows service."
}
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

function Invoke-Nssm {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    & $NssmPath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "NSSM command failed (exit code $LASTEXITCODE): nssm $($Arguments -join ' ')"
    }
}

function Wait-ForServiceStopped {
    param([string]$Name, [int]$TimeoutSeconds)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $svc = Get-Service -Name $Name -ErrorAction SilentlyContinue
        if ($null -eq $svc -or $svc.Status -eq [System.ServiceProcess.ServiceControllerStatus]::Stopped) {
            return
        }
        Start-Sleep -Milliseconds 500
    }
    $finalSvc = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if ($null -ne $finalSvc -and $finalSvc.Status -ne [System.ServiceProcess.ServiceControllerStatus]::Stopped) {
        throw "Timed out after ${TimeoutSeconds}s waiting for service '$Name' to reach Stopped state (current state: $($finalSvc.Status))."
    }
}

function Wait-ForServiceRemoved {
    param([string]$Name, [int]$TimeoutSeconds)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $svc = Get-Service -Name $Name -ErrorAction SilentlyContinue
        if ($null -eq $svc) {
            return
        }
        Start-Sleep -Milliseconds 500
    }
    throw "Timed out after ${TimeoutSeconds}s waiting for previous service '$Name' removal to complete."
}

# 1. Safely stop and remove any existing Waitress Windows Service before reinstalling
$existingService = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($null -ne $existingService) {
    Write-Host "Existing service '$ServiceName' found (status: $($existingService.Status)). Stopping safely..."
    if ($existingService.Status -ne [System.ServiceProcess.ServiceControllerStatus]::Stopped) {
        & $NssmPath stop $ServiceName 2>$null | Out-Null
        Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
        Wait-ForServiceStopped -Name $ServiceName -TimeoutSeconds $ServiceWaitTimeoutSeconds
    }
    Write-Host "Removing previous '$ServiceName' service..."
    Invoke-Nssm -Arguments @("remove", $ServiceName, "confirm")
    Wait-ForServiceRemoved -Name $ServiceName -TimeoutSeconds $ServiceWaitTimeoutSeconds
}

# 2. Install and configure Waitress Windows Service (SQLite-backed; no PostgreSQL dependency)
$serviceInstalled = $false
try {
    Invoke-Nssm -Arguments @("install", $ServiceName, $PythonExe, "`"$ServeScript`"")
    $serviceInstalled = $true

    Invoke-Nssm -Arguments @("set", $ServiceName, "AppDirectory", $ProjectRoot)
    Invoke-Nssm -Arguments @("set", $ServiceName, "AppStdout", (Join-Path $LogDir "waitress_service_stdout.log"))
    Invoke-Nssm -Arguments @("set", $ServiceName, "AppStderr", (Join-Path $LogDir "waitress_service_stderr.log"))
    Invoke-Nssm -Arguments @("set", $ServiceName, "AppRotateFiles", "1")
    Invoke-Nssm -Arguments @("set", $ServiceName, "AppRotateBytes", "10485760")
    Invoke-Nssm -Arguments @("set", $ServiceName, "Start", "SERVICE_DELAYED_AUTO_START")
    Invoke-Nssm -Arguments @("set", $ServiceName, "AppExit", "Default", "Restart")
    Invoke-Nssm -Arguments @("set", $ServiceName, "AppRestartDelay", "5000")
}
catch {
    $errMsg = $_.Exception.Message
    if ($serviceInstalled) {
        Write-Warning "Service configuration failed after creation; attempting rollback removal of '$ServiceName'..."
        & $NssmPath remove $ServiceName confirm 2>$null | Out-Null
    }
    throw "Failed to install/configure Windows service '$ServiceName': $errMsg"
}

# 3. Daily Backup Scheduled Task (02:00 AM)
$BackupAction = New-ScheduledTaskAction -Execute $PythonExe -Argument "manage.py run_system_backup --trigger=SCHEDULED" -WorkingDirectory $ProjectRoot
$BackupTrigger = New-ScheduledTaskTrigger -Daily -At 2:00AM
Register-ScheduledTask -TaskName "BalajiInvoiceHub-DailyBackup" -Action $BackupAction -Trigger $BackupTrigger -RunLevel Highest -Force

# 4. Weekly Sandbox Restore Drill (Sunday 03:00 AM)
$RestoreAction = New-ScheduledTaskAction -Execute $PythonExe -Argument "manage.py run_sandbox_restore" -WorkingDirectory $ProjectRoot
$RestoreTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 3:00AM
Register-ScheduledTask -TaskName "BalajiInvoiceHub-WeeklyRestoreDrill" -Action $RestoreAction -Trigger $RestoreTrigger -RunLevel Highest -Force

# 5. Remove redundant boot-time OCR recovery task (serve_production.py runs startup recovery once on boot)
Unregister-ScheduledTask -TaskName "BalajiInvoiceHub-StartupOcrRecovery" -Confirm:$false -ErrorAction SilentlyContinue

Write-Host "Service '$ServiceName' and scheduled tasks registered cleanly. Start service with: Start-Service $ServiceName"
