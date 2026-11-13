# Balaji InvoiceHub — Production Security Checklist

- [ ] `DEBUG=False` in `.env`
- [ ] Unique, high-entropy `SECRET_KEY` (never committed to git)
- [ ] `ALLOWED_HOSTS` restricted to `invoicehub.balajitransports.com,localhost,127.0.0.1`
- [ ] `CSRF_TRUSTED_ORIGINS` set to `https://invoicehub.balajitransports.com`
- [ ] `SECURE_SSL_REDIRECT=True`, `SESSION_COOKIE_SECURE=True`, `CSRF_COOKIE_SECURE=True`, `SECURE_HSTS_SECONDS=31536000`
- [ ] Waitress bound to `127.0.0.1:8000` with `WAITRESS_TRUSTED_PROXY=127.0.0.1` (proxy headers ignored from untrusted clients)
- [ ] SQLite file (`db.sqlite3`), `.env`, `logs/`, and `backups/` are **not** served by WhiteNoise or `/media/`
- [ ] External submitters verified to have zero access to other submitters' invoices, `/dashboard/naveen/`, `/dashboard/admin/`, `/backup/`, or Tally exports
- [ ] `GEMINI_API_KEY` stored only in `.env` and never logged
- [ ] Service worker (`/sw.js`) excludes `/accounts/`, `/invoices/`, `/dashboard/`, `/backup/`, `/admin/`, `/media/`, `/health/`
- [ ] `python manage.py check --deploy` passes with `0` issues

