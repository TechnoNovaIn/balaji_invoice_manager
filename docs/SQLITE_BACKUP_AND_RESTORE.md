# Balaji InvoiceHub — SQLite Backup & Restore Guide

---

## Backup Architecture

- **Online SQLite Backup:** Uses `sqlite3.Connection.backup()` (`apps/backup/services.py`) for WAL-consistent snapshots without stopping Waitress.
- **Media Archive:** Archives `media/` into a ZIP file alongside the SQLite snapshot.
- **Integrity Verification:** Computes and verifies SHA-256 checksums for both the database snapshot and media ZIP.
- **Storage Location:** Configured via `BACKUP_ROOT` (must be outside `C:\Apps\balaji_invoice_manager`, e.g. `D:\BalajiBackups\invoicehub`).
- **Retention:** Controlled by `BACKUP_RETENTION_DAYS` (default `3650` days).

---

## Scheduled Tasks

- **Daily Backup (02:00 AM):** `python manage.py run_system_backup --trigger=SCHEDULED`
- **Weekly Sandbox Restore Drill (Sunday 03:00 AM):** `python manage.py run_sandbox_restore` (restores into an isolated temporary directory, runs `PRAGMA integrity_check;`, and never touches the live database).

---

## Manual Backup & Verification Commands

```powershell
cd C:\Apps\balaji_invoice_manager
.\.venv\Scripts\Activate.ps1

# Run an immediate full backup
python manage.py run_system_backup --trigger=MANUAL

# Run a non-destructive sandbox restore verification drill
python manage.py run_sandbox_restore
```

---

## Full Disaster Recovery Procedure (Live Restore)

1. **Stop the service** (prevents active writes during file replacement):
   ```powershell
   Stop-Service BalajiInvoiceHub
   ```
2. **Preserve the current database & WAL files** before replacing:
   ```powershell
   Copy-Item C:\Apps\balaji_invoice_manager\db.sqlite3* C:\Temp\pre_restore_snapshot\
   ```
3. **Verify the backup SHA-256** and run `PRAGMA integrity_check;` on the backup `.sqlite3` file.
4. **Replace `db.sqlite3`** (and remove stale `-wal` / `-shm` sidecar files from the replaced DB) and extract the media ZIP into `media\`.
5. **Run migrations & checks:**
   ```powershell
   python manage.py check
   python manage.py migrate --noinput
   ```
6. **Start the service:**
   ```powershell
   Start-Service BalajiInvoiceHub
   ```

