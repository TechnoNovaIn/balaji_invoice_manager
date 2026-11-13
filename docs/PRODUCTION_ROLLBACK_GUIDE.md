# Balaji InvoiceHub — Production Rollback Guide

Use this procedure if a deployment introduces an unexpected regression.

---

## 1. Create an Immediate Safety Snapshot

```powershell
cd C:\Apps\balaji_invoice_manager
.\.venv\Scripts\Activate.ps1
python manage.py run_system_backup --trigger=MANUAL
```

## 2. Stop the Service

```powershell
Stop-Service BalajiInvoiceHub
```

## 3. Revert Application Code

```powershell
cd C:\Apps\balaji_invoice_manager
git log -n 5 --oneline
git checkout <previous-known-good-commit-or-tag>
pip install -r requirements.txt
python manage.py collectstatic --noinput
```

## 4. Database Rollback (Only if a Migration Must Be Reversed)

- If only additive columns were added (such as `invoices.0013`), code rollback alone is normally safe or you may reverse the migration:
  ```powershell
  python manage.py migrate invoices 0012
  ```
- If a full database rollback is required, follow `docs/SQLITE_BACKUP_AND_RESTORE.md` using the pre-deployment backup snapshot.

## 5. Restart & Verify

```powershell
Start-Service BalajiInvoiceHub
Get-Service BalajiInvoiceHub
```
Check `https://invoicehub.balajitransports.com/health/` and `logs\waitress_service_stderr.log`.

