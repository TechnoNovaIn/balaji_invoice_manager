# Run as Administrator on the target Windows 11 production server.
# Registers BalajiInvoiceHub (Waitress) via NSSM and schedules daily backups,
# weekly sandbox restore verification drills, and startup OCR recovery tasks.
param(
    [string]$ProjectRoot = "C:\Apps\balaji_invoice_manager",
    [string]$NssmPath = "C:\Tools\nssm\nssm.exe"
)

$ErrorActionPreference = "Stop"
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$ServeScript = Join-Path $ProjectRoot "serve_production.py"
$LogDir = Join-Path $ProjectRoot "logs"

if (-not (Test-Path $PythonExe)) {
    throw "Virtual environment Python not found at $PythonExe. Create .venv first."
}
if (-not (Test-Path $NssmPath)) {
    throw "NSSM not found at $NssmPath. Extract nssm.exe before installing the Windows service."
}
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

# 1. Install / Update Waitress Windows Service
& $NssmPath stop BalajiInvoiceHub 2>$null | Out-Null
& $NssmPath remove BalajiInvoiceHub confirm 2>$null | Out-Null
& $NssmPath install BalajiInvoiceHub $PythonExe "`"$ServeScript`""
& $NssmPath set BalajiInvoiceHub AppDirectory $ProjectRoot
& $NssmPath set BalajiInvoiceHub DependOnService postgresql-x64-18
& $NssmPath set BalajiInvoiceHub AppStdout (Join-Path $LogDir "waitress_service_stdout.log")
& $NssmPath set BalajiInvoiceHub AppStderr (Join-Path $LogDir "waitress_service_stderr.log")
& $NssmPath set BalajiInvoiceHub AppRotateFiles 1
& $NssmPath set BalajiInvoiceHub AppRotateBytes 10485760
& $NssmPath set BalajiInvoiceHub Start SERVICE_DELAYED_AUTO_START
& $NssmPath set BalajiInvoiceHub AppExit Default Restart
& $NssmPath set BalajiInvoiceHub AppRestartDelay 5000

# 2. Daily Backup Scheduled Task (02:00 AM)
$BackupAction = New-ScheduledTaskAction -Execute $PythonExe -Argument "manage.py run_system_backup --trigger=SCHEDULED" -WorkingDirectory $ProjectRoot
$BackupTrigger = New-ScheduledTaskTrigger -Daily -At 2:00AM
Register-ScheduledTask -TaskName "BalajiInvoiceHub-DailyBackup" -Action $BackupAction -Trigger $BackupTrigger -RunLevel Highest -Force

# 3. Weekly Sandbox Restore Drill (Sunday 03:00 AM)
$RestoreAction = New-ScheduledTaskAction -Execute $PythonExe -Argument "manage.py run_sandbox_restore" -WorkingDirectory $ProjectRoot
$RestoreTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 3:00AM
Register-ScheduledTask -TaskName "BalajiInvoiceHub-WeeklyRestoreDrill" -Action $RestoreAction -Trigger $RestoreTrigger -RunLevel Highest -Force

# 4. Boot-time OCR Orphaned Job Recovery Safety Check (2 min stale guard)
$OcrAction = New-ScheduledTaskAction -Execute $PythonExe -Argument "manage.py recover_incomplete_uploads --stale-minutes=2" -WorkingDirectory $ProjectRoot
$OcrTrigger = New-ScheduledTaskTrigger -AtStartup
Register-ScheduledTask -TaskName "BalajiInvoiceHub-StartupOcrRecovery" -Action $OcrAction -Trigger $OcrTrigger -RunLevel Highest -Force

Write-Host "Service and scheduled tasks registered. Start service with: Start-Service BalajiInvoiceHub"

