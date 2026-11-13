# Balaji InvoiceHub — Windows Service Management

Service Name: `BalajiInvoiceHub` (Waitress WSGI via NSSM, SQLite-backed, no PostgreSQL dependency).

---

## Common Service Commands (Administrator PowerShell)

```powershell
# Check status
Get-Service BalajiInvoiceHub

# Start service
Start-Service BalajiInvoiceHub

# Graceful stop (triggers atexit shutdown_ocr_executor up to 15s)
Stop-Service BalajiInvoiceHub

# Restart after code or .env update
Restart-Service BalajiInvoiceHub
```

## Re-Installing or Updating Service Definition

```powershell
cd C:\Apps\balaji_invoice_manager
powershell -ExecutionPolicy Bypass -File .\scripts\windows\install_windows_services.ps1 -ProjectRoot "C:\Apps\balaji_invoice_manager"
Start-Service BalajiInvoiceHub
```

The installer script:
1. Safely stops an existing `BalajiInvoiceHub` service and waits until Windows reports `Stopped`.
2. Removes the previous registration and waits until removal completes.
3. Registers `BalajiInvoiceHub` with `SERVICE_DELAYED_AUTO_START`, `AppExit Default Restart` (`5000` ms delay), and 10 MB rotating logs (`logs\waitress_service_stdout.log` and `logs\waitress_service_stderr.log`).
4. Registers `BalajiInvoiceHub-DailyBackup` (02:00 AM daily) and `BalajiInvoiceHub-WeeklyRestoreDrill` (Sunday 03:00 AM).
5. Removes any legacy `BalajiInvoiceHub-StartupOcrRecovery` scheduled task because `serve_production.py` runs `run_startup_recovery()` once at boot.

