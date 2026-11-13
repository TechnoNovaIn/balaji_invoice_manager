# Balaji InvoiceHub — Windows 11 Production Deployment Guide

**Important:** This guide covers local/on-premise Windows 11 server deployment using SQLite (WAL mode) and Waitress behind Cloudflare Tunnel (or approved reverse proxy). Do **not** run development `runserver` in production.

---

## 1. Windows Server Preparation Checklist

- **Python:** Python `3.13.4` (64-bit) installed system-wide (`C:\Python313\python.exe`).
- **Project Directory:** Local NTFS volume only (e.g., `C:\Apps\balaji_invoice_manager`). **Never** place `db.sqlite3` on a network share, OneDrive, or Google Drive folder.
- **Virtual Environment:**
  ```powershell
  cd C:\Apps\balaji_invoice_manager
  python -m venv .venv
  .\.venv\Scripts\Activate.ps1
  pip install -r requirements.txt
  ```
- **Directories:** Ensure local NTFS directories exist with least-privilege permissions for the service account (`NETWORK SERVICE` or dedicated service user) + Administrators:
  - `C:\Apps\balaji_invoice_manager\media`
  - `C:\Apps\balaji_invoice_manager\staticfiles`
  - `C:\Apps\balaji_invoice_manager\logs`
  - External backup directory outside the app folder (e.g., `D:\BalajiBackups\invoicehub`)
- **NSSM:** Extract `nssm.exe` (64-bit) to `C:\Tools\nssm\nssm.exe`.
- **Windows Firewall:**
  - Waitress binds to `127.0.0.1:8000` (loopback only).
  - Do **not** open ports `8000`, `5432`, or `3389` (RDP) to the public internet.

---

## 2. Production Environment Configuration (`.env`)

Verify the following in `C:\Apps\balaji_invoice_manager\.env` (never commit `.env` or print secrets):

```ini
DEBUG=False
SECRET_KEY=<strong-random-64+-char-secret>
ALLOWED_HOSTS=invoicehub.balajitransports.com,localhost,127.0.0.1
CSRF_TRUSTED_ORIGINS=https://invoicehub.balajitransports.com
DB_ENGINE=django.db.backends.sqlite3
DB_NAME=db.sqlite3

# Waitress & Trusted Reverse Proxy
WAITRESS_HOST=127.0.0.1
WAITRESS_PORT=8000
WAITRESS_THREADS=8
WAITRESS_CHANNEL_TIMEOUT=180
WAITRESS_TRUSTED_PROXY=127.0.0.1
DJANGO_TRUSTED_PROXY_COUNT=1

# HTTPS / Cookies (when behind TLS proxy / Cloudflare Tunnel)
SECURE_SSL_REDIRECT=True
SESSION_COOKIE_SECURE=True
CSRF_COOKIE_SECURE=True
SECURE_HSTS_SECONDS=31536000

# Bounded OCR & Gemini Fallback Policy
OCR_MAX_WORKERS=2
OCR_MAX_QUEUE_SIZE=50
OCR_MAX_RETRIES=3
GEMINI_API_KEY=<your-gemini-api-key>
GEMINI_MODEL=gemini-2.5-flash
GEMINI_FALLBACK_POLICY=auto
GEMINI_MAX_PAGES_PER_INVOICE=3

# External Backup Root (outside application directory)
BACKUP_ROOT=D:\BalajiBackups\invoicehub
BACKUP_RETENTION_DAYS=3650
```

---

## 3. Pre-Flight & Static Files (Run on Server)

```powershell
cd C:\Apps\balaji_invoice_manager
.\.venv\Scripts\Activate.ps1
python manage.py check
python manage.py check --deploy
python manage.py migrate --noinput
python manage.py collectstatic --noinput
```

---

## 4. Install & Start Windows Service

Run in an **Administrator PowerShell** window:

```powershell
cd C:\Apps\balaji_invoice_manager
powershell -ExecutionPolicy Bypass -File .\scripts\windows\install_windows_services.ps1 -ProjectRoot "C:\Apps\balaji_invoice_manager"
Start-Service BalajiInvoiceHub
Get-Service BalajiInvoiceHub
```

---

## 5. Network, HTTPS & PWA Verification

- Confirm `https://invoicehub.balajitransports.com/health/` returns HTTP `200`.
- Confirm Cloudflare Tunnel (or reverse proxy) forwards `X-Forwarded-Proto: https` and `CF-Connecting-IP` from `127.0.0.1` only.
- Open `https://invoicehub.balajitransports.com` in Chrome -> verify the PWA install prompt / icon appears and `/sw.js` registers cleanly without caching `/accounts/`, `/invoices/`, or `/dashboard/`.

