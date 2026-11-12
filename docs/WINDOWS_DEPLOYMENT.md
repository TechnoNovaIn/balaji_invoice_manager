# Balaji InvoiceHub — Windows 11 Production Deployment Guide

This guide covers deploying **Balaji InvoiceHub** on the company's Windows 11 server using **Waitress**, **WhiteNoise**, **PostgreSQL 18**, and **Cloudflare Tunnel**.

> **Important:** Database restore/cutover must be verified separately before pointing users to the production server. Never run destructive migrations or overwrite an active database.

---

## 1. Prerequisites
- Windows 11 Pro / Enterprise (power sleep disabled while plugged in: `powercfg /change standby-timeout-ac 0`)
- Python 3.13 (64-bit)
- PostgreSQL 18 (64-bit Windows, listening on `127.0.0.1:5432` only)
- NSSM (`nssm.exe` placed at `C:\Tools\nssm\nssm.exe`)
- `cloudflared` (`winget install Cloudflare.cloudflared`)
- Domain managed on Cloudflare DNS (e.g. `invoicehub.companydomain.com`)

---

## 2. Database Backup & Separate Verification Procedure (Before Cutover)

### A. On Source Machine — Create Verified Backup
```powershell
cd D:\balaji_invoice_manager
python manage.py run_system_backup --trigger=MANUAL
```
Verify both `.dump` and `.zip` files and SHA-256 checksums are reported as `Integrity: VERIFIED`.

### B. On Target Windows 11 Server — Restore & Verify Separately
1. Keep PostgreSQL bound exclusively to `127.0.0.1:5432`. Do **not** open port `5432` in Windows Firewall.
2. Restore the `.dump` archive into `balaji_invoicehub_db`:
```powershell
$env:PGPASSWORD = "<DB_PASSWORD>"
& "C:\Program Files\PostgreSQL\18\bin\createdb.exe" -h 127.0.0.1 -p 5432 -U postgres balaji_invoicehub_db
& "C:\Program Files\PostgreSQL\18\bin\pg_restore.exe" -h 127.0.0.1 -p 5432 -U postgres -d balaji_invoicehub_db --no-owner -v "E:\Balaji_Backups\InvoiceHub\<BKP_ID>\<BKP_ID>_postgres.dump"
Remove-Item Env:\PGPASSWORD
```
3. Extract `<BKP_ID>_documents.zip` into `C:\Apps\balaji_invoice_manager\media\`.
4. Verify table row counts (`balaji_users`, `invoices_invoicesubmission`, `invoices_vendormaster`) and test `python manage.py run_sandbox_restore` before switching users over.

---

## 3. Application Setup on Target Server
```powershell
cd C:\Apps\balaji_invoice_manager
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Create `C:\Apps\balaji_invoice_manager\.env` using `.env.example`:
- Set `DJANGO_DEBUG=False`
- Generate a fresh 64+ char `DJANGO_SECRET_KEY`
- Set `DJANGO_ALLOWED_HOSTS=invoicehub.companydomain.com,127.0.0.1,localhost`
- Set `DJANGO_CSRF_TRUSTED_ORIGINS=https://invoicehub.companydomain.com`
- Set `DJANGO_ENABLE_SSL=True`, `DJANGO_TRUSTED_PROXY_COUNT=1`, `DJANGO_TRUSTED_PROXY_IPS=127.0.0.1,::1`, `DJANGO_TRUST_CF_CONNECTING_IP=True`

Then run non-destructive schema & static checks:
```powershell
python manage.py migrate --plan
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py check --deploy
```

---

## 4. Start Waitress & Register Windows Services
Run Administrator PowerShell:
```powershell
C:\Apps\balaji_invoice_manager\scripts\windows\install_windows_services.ps1 -ProjectRoot "C:\Apps\balaji_invoice_manager"
Start-Service BalajiInvoiceHub
```
Verify Waitress locally via simulated proxy HTTPS header:
```powershell
Invoke-WebRequest -Uri "http://127.0.0.1:8000/health/" -Headers @{ "X-Forwarded-Proto" = "https" }
```

---

## 5. Cloudflare Tunnel Setup
```powershell
cloudflared tunnel login
cloudflared tunnel create balaji-invoicehub
cloudflared tunnel route dns balaji-invoicehub invoicehub.companydomain.com
```
Create `C:\ProgramData\cloudflared\config.yml` routing `invoicehub.companydomain.com` -> `http://127.0.0.1:8000`, then install service:
```powershell
cloudflared --config C:\ProgramData\cloudflared\config.yml service install
sc.exe start Cloudflared
```
Keep inbound ports `8000` and `5432` blocked on Windows Firewall. Set Cloudflare SSL/TLS mode to **Full (Strict)**.

