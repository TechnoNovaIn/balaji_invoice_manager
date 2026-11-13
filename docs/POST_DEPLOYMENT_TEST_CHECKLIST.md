# Balaji InvoiceHub — Post-Deployment Test Checklist

Execute immediately after starting `BalajiInvoiceHub` on the Windows 11 server:

1. **Service & Health:**
   - `Get-Service BalajiInvoiceHub` shows `Running`.
   - `https://invoicehub.balajitransports.com/health/` returns HTTP `200`.
   - `logs\waitress_service_stdout.log` shows a single `Startup OCR recovery check` line on boot.
2. **Authentication & RBAC:**
   - Log in as an External Submitter -> lands on `/dashboard/employee/`.
   - Confirm direct navigation to `/dashboard/naveen/` or `/backup/` is denied (`403` / redirect).
   - Log in as Naveen -> lands on `/dashboard/naveen/`.
3. **Upload & Bounded Background OCR:**
   - Upload a multi-page PDF as an External Submitter -> progress bar advances (`QUEUED` -> `PROCESSING` -> `Completed`).
   - Confirm the invoice appears in Naveen's queue with correct vendor/GSTIN/E-Invoice (`YES`/`NO`)/IRN fields.
   - Confirm `Upload Processing Status` is visible on both Submitter and Naveen dashboards.
4. **PWA & Security:**
   - Confirm `https://invoicehub.balajitransports.com/manifest.json` and `/sw.js` load with HTTP `200`.
   - Log out and press Browser Back -> confirm cached authenticated invoice data is not shown (`Cache-Control: no-store`).
5. **Backup Drill:**
   - Run `python manage.py run_sandbox_restore` and confirm `integrity_check: ok`.

