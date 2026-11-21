# Balaji InvoiceHub

> Internal Operations, Invoice Processing, Tally Workflow & Disaster Recovery Platform for **Balaji Transports**.

---

## 📌 Project Overview

**Balaji InvoiceHub** replaces chaotic WhatsApp messages, physical paper loss, and manual data-entry bottlenecks with a centralized, hardened operations platform for managing invoice receipts across transport logistics.

Field employees and drivers snap photos or upload PDF receipts directly from their mobile phones. Documents stream in real-time chronological order to **Naveen (Accounts)**. Naveen can inspect clear, high-resolution bills on a dual-screen setup, enter them directly into local Tally accounting software, check them off individually or in batch at end-of-day, and export official GST Portal-compliant Excel spreadsheets for monthly return filing.

---

## ⚙️ Technology Stack

* **Backend:** Python 3.13, Django 5.2
* **Production Web Server:** Waitress 3.0 (Native multi-threaded Windows WSGI server with graceful worker shutdown, 100 MB payload handling, and proxy support)
* **Database:** SQLite in WAL (Write-Ahead Logging) mode with 30-second busy timeout and lock-retry wrappers (zero external database daemon required for local on-premise deployment; PostgreSQL compatible)
* **Vision AI & OCR Engine:**
  * **Primary:** Google Gemini Vision AI (`gemini-3.5-flash-lite`, with fallback models `gemini-3.8-flash` and `gemini-3.5-flash`) for human-level handwritten transport bills and printed invoices
  * **Fallback:** Local offline `RapidOCR` (ONNX Runtime deep-learning OCR) + PyMuPDF digital text preservation
  * **Image Preprocessing:** OpenCV boundary contour detection with conservative 3% safety margin auto-cropping (zero sharpening or artificial filters)
* **Static Assets Delivery:** WhiteNoise 6.12 with pre-compressed gzip assets and cache headers
* **Frontend:** Bootstrap 5.3, Bootstrap Icons, responsive mobile offcanvas drawer + desktop sidebar
* **Spreadsheet Engine:** `openpyxl` (GST Offline Utility B2B Excel generation & converter)
* **Security & RBAC:** Role-Based Access Control (Employee, Naveen, Admin, Management), session security, authenticated document streaming, server-side MIME magic-byte validation, and SHA-256 deduplication
* **Logging:** Windows-safe rotating production logging with automatic `PermissionError` lock handling

---

## 🚀 Key Modules & Capabilities

### 1. Employee Mobile-First Invoice Capture & Auto-Crop
* **Touch-Optimized Mobile Interface:**
  * `[ Take Photo ]`: Direct access to phone camera (`accept="image/*" capture="environment"`).
  * `[ Upload PDF / Files ]`: Gallery and document picker supporting multi-page photos or PDF invoices up to **100 MB** (`DATA_UPLOAD_MAX_MEMORY_SIZE = 100MB`).
* **Intelligent Conservative Auto-Crop:**
  * Detects document boundaries on photographs and crops out backgrounds while guaranteeing zero loss of invoice contents.
  * Enforces conservative frame coverage (minimum 35%) and adds a **3% safety margin padding** so header vendor details, tax stamps, and footer grand totals are never clipped.
  * Preserves 100% of original photo colors, contrast, and high-print resolution without destructive sharpening or threshold filters.
* **Save Once, Never Forget (Password Manager Integration):**
  * Fully compliant with Google Password Manager, Apple Keychain, and Samsung Pass (`autocomplete="username"`, `autocomplete="current-password"`).
  * 1-year persistent session lifespan (`SESSION_COOKIE_AGE = 31536000`). Employees stay logged in through phone restarts.
* **Integrity & Deduplication:**
  * Concurrency-safe monthly tracking ID (`BTI-YYYYMM-XXXXX`).
  * Cryptographic SHA-256 hashing blocks duplicate uploads within 24 hours.

### 2. Naveen's Centralized Accounts Dashboard
* **Chronological Arrival Stream:** Invoices stream directly to Naveen in chronological order (#1, #2, #3...) matching physical field submissions.
* **1-Click Tally Entry & Batch Checkboxes:**
  * Single-click toggle button on any invoice row or within the viewer to immediately mark an invoice as entered in Tally.
  * Multi-select checkboxes along the right column for Naveen's end-of-day workflow: select multiple entered vouchers and click **`[ Complete Selected in Tally ]`** to batch-commit all at once.
* **Daily Fresh Feed & Carryover Backlog Alert:**
  * **Daily Fresh Feed:** When all invoices are checked and completed, the feed presents a clean zero-backlog state for the next day.
  * **Carryover Alert:** Any unentered invoices from prior days are pinned at the top under a warning section (`⚠️ Carried Over From Previous Days`) ensuring no bill is ever overlooked.
* **Organized Tabs:**
  * **Active Daily Feed (Pending):** Unentered invoices split into Carried Over and Today's incoming bills.
  * **Completed Archive:** Historical record of bills entered into Tally with batch and single Undo options.
  * **All Invoices:** Complete searchable ledger with multi-field search (tracking ID, employee, party, amount, date).

### 3. High-Clarity Document Viewer & Standardized Downloads
* **Side-by-Side Dual Screen Support:** Keeps Balaji InvoiceHub open on one monitor and local Tally software on the other.
* **Viewer Controls:**
  * Zoom In (`+`), Zoom Out (`-`), Rotate 90° (`↻`), Reset Zoom, and Fullscreen.
  * Sequential Navigation: `Previous Bill` and `Next Bill →` buttons for quick keyboard/click navigation.
* **Clean Standardized Downloads:**
  * Downloading an invoice automatically saves with a clean, standardized filename:
    `INV_<InvoiceNumber>_<Date>.jpg` (e.g. `INV_191_2026-10-26.jpg` or `INV_MHB-1989_2026-10-29.pdf`).

### 4. Hybrid Vision AI & Hardened OCR Extraction Engine
* **Primary AI Engine (Google Gemini Vision):**
  * Uses `gemini-3.5-flash-lite` (with automatic fallback to `gemini-3.8-flash` / `gemini-3.5-flash`) to parse complex handwritten transport receipts, lorry freight chits, and computerized tax invoices.
  * Automatically extracts Supplier Name, Supplier GSTIN, Invoice Number, Invoice Date, Taxable Value, CGST, SGST, IGST, and Grand Total.
* **Hardened Offline Fallback (RapidOCR):**
  * If the cloud API is offline or quota-limited, the system falls back to the local ONNX deep-learning OCR.
  * **Header Token Blacklisting:** Strictly filters out printed receipt headers (`CASH / CREDIT BILL`, `TAX INVOICE`, `MEMO`, `RECEIPT`, `ORIGINAL`, `DUPLICATE`) so offline OCR captures the true bill number.
  * **Indian Currency Notation:** Corrects OCR comma-period confusion (e.g. `3,96.890` is correctly parsed as `₹3,96,890.00` rather than `₹396.89`).
* **Authoritative Vendor Master Directory:**
  * Integrates 237 pre-loaded Balaji transport vendor records.
  * Automatically matches supplier GSTINs and names to default Tally Purchase Expense Heads (e.g. *Tyre & Tube Purchase*, *Lube Oil*, *Spare Parts*, *Vehicle Maintenance*).

### 5. Statutory GST Portal B2B Excel Tooling
* **Official GST Offline Utility Format:** Exports `.xlsx` workbooks matching the official government GST Portal B2B schema:
  `GSTIN of Supplier | Trade/Legal Name | Invoice number | Invoice date | Invoice Value | Place of Supply | Reverse Charge | Rate | Taxable Value | Cess Amount`
* **Upload Converter:** Upload external Excel sheets or Tally registers to transform and validate them into the official GST Portal format.

### 6. Automated Disaster Recovery & 10-Year Permanent Archive (`apps.backup`)
* **Automated SQLite Hot Backups:** Non-locking SQLite database snapshot paired with ZIP archives of all original uploaded invoices.
* **Cryptographic Integrity:** Computes and logs SHA-256 checksums for both database dumps and media archives.
* **Storage Location:** Saves directly to designated external/backup storage (`E:\Balaji_Backups\InvoiceHub`).
* **Permanent Retention:** Configured for 3,650 days (10-year statutory GST audit retention).
* **Automated Restore Drill:** Non-destructive sandbox test restoration verifies backup recoverability.

---

## 💻 Running the Application

### Prerequisites
* Windows 10/11 or Windows Server
* Python 3.13 (or 3.12+)
* Storage drive for backups (default: `E:\Balaji_Backups\InvoiceHub`)

### 1. Environment Configuration (`.env`)
Create or verify your `.env` file in the project root:
```env
# Django Security
DJANGO_SECRET_KEY=your-production-secret-key
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost,192.168.0.131,invoicehub.balajitransports.com
DJANGO_CSRF_TRUSTED_ORIGINS=http://127.0.0.1:8000,http://localhost:8000,http://192.168.0.131:8000,https://invoicehub.balajitransports.com

# Production Server Host (0.0.0.0 allows other PCs on the office LAN to connect)
WAITRESS_HOST=0.0.0.0
WAITRESS_PORT=8000
WAITRESS_THREADS=8

# Database (SQLite in WAL mode)
DB_ENGINE=django.db.backends.sqlite3
DB_NAME=db.sqlite3

# Google Gemini Vision AI (Free Tier: 1,500 requests/day)
GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-3.5-flash-lite
GEMINI_FALLBACK_MODELS=gemini-3.8-flash,gemini-3.5-flash

# SSL Settings:
# If accessing via Cloudflare Tunnel / HTTPS Domain:
DJANGO_ENABLE_SSL=True
SECURE_SSL_REDIRECT=True
# If accessing via local office LAN IP over plain HTTP (e.g. http://192.168.0.x:8000):
# Set SECURE_SSL_REDIRECT=False and DJANGO_ENABLE_SSL=False

# Disaster Recovery (10-Year Permanent Archive)
BACKUP_STORAGE_PATH=E:\Balaji_Backups\InvoiceHub
BACKUP_RETENTION_DAYS=3650
```

### 2. Start the Production Server
Run the production Waitress server entrypoint:
```powershell
python serve_production.py
```
Waitress will:
1. Verify database readiness and apply startup OCR recovery checks for any interrupted jobs.
2. Initialize multi-threaded production workers.
3. Serve on `http://0.0.0.0:8000`.

### 3. Start Development Server (Debugging Only)
```powershell
python manage.py runserver 127.0.0.1:8000
```

### 4. Run as a Windows Background Service (Auto-Start on PC Boot)
To ensure InvoiceHub runs automatically in the background whenever the server computer boots:
1. Download [NSSM (Non-Sucking Service Manager)](https://nssm.cc/).
2. Open PowerShell as Administrator and run:
   ```powershell
   nssm install BalajiInvoiceHub "C:\Users\darsh\AppData\Local\Programs\Python\Python313\python.exe" "d:\balaji_invoice_manager\serve_production.py"
   nssm set BalajiInvoiceHub AppDirectory "d:\balaji_invoice_manager"
   nssm set BalajiInvoiceHub Start SERVICE_AUTO_START
   nssm start BalajiInvoiceHub
   ```
3. Allow incoming traffic through the Windows Defender Firewall:
   ```powershell
   New-NetFirewallRule -DisplayName "Balaji InvoiceHub" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Allow
   ```

### 5. Run Automated Test Suite
```powershell
python manage.py test tests
```
*Current test suite: **204 of 204 tests passing (100% success)**.*

### 6. Trigger Manual Backup Drill
```powershell
python manage.py run_invoicehub_backup
```

---

## 👥 Default User Accounts

| Role | Username | Password | Purpose / Default Landing Page |
|---|---|---|---|
| **Employee (Collector)** | `employee` | `BalajiEmp@2026` | Mobile upload portal (`/dashboard/employee/`) |
| **Naveen (Accounts)** | `naveen` | `BalajiNaveen@2026` | Accounts chronological feed & Tally check-off (`/dashboard/naveen/`) |
| **Admin** | `admin` | `BalajiAdmin@2026` | User administration, audit logs & backups (`/dashboard/admin/`) |
| **Management** | `management` | `BalajiMgmt@2026` | Read-only executive oversight & analytics (`/dashboard/management/`) |

---

## 📁 Project Architecture Directory Map

```text
balaji_invoice_manager/
│
├── apps/
│   ├── accounts/              # Custom User model, RBAC middleware, authentication & login
│   │   ├── middleware.py      # Session security, SSL bypass, force password change
│   │   ├── models.py          # User, UserRole, Department
│   │   └── views.py           # Login, profile, password reset, user management
│   │
│   ├── backup/                # Automated SQLite snapshots, ZIP archives & integrity checks
│   │   ├── services.py        # Backup generator, SHA-256 verifier, retention pruner
│   │   └── management/        # 'run_invoicehub_backup' management command
│   │
│   ├── core/                  # Dashboards and Windows-safe logging handlers
│   │   ├── logging_handlers.py# SafeRotatingFileHandler (prevents WinError 32 log lock crashes)
│   │   └── views.py           # naveen_dashboard, employee_dashboard, admin_dashboard
│   │
│   └── invoices/              # Invoice processing, OCR, Vision AI, Tally & GST converter
│       ├── gemini_extractor.py# Google Gemini Vision AI integration (gemini-3.5-flash-lite)
│       ├── image_processing.py# OpenCV conservative auto-cropping (3% safe padding)
│       ├── reader.py          # Hybrid metadata extractor & hardened RapidOCR parser
│       ├── services.py        # Upload validation, MIME verification, SHA-256 deduplication
│       ├── tasks.py           # In-process bounded thread pool executor for background OCR
│       ├── vendor_directory.py# 237 authoritative Vendor Master records
│       ├── gst_converter.py   # GST Portal B2B Excel (.xlsx) generator & converter
│       ├── tally_exporter.py  # Tally XML & Excel voucher generation
│       ├── models.py          # InvoiceSubmission, UploadedDocument, InvoiceDocument, VendorMaster
│       └── views.py           # Upload, viewer, toggle Tally, batch completion, analytics
│
├── invoicehub/                # Django project settings, WSGI, URLs
│   ├── settings.py            # Hardened production settings (WhiteNoise, Logging, Security)
│   ├── urls.py                # Root URL dispatcher (118 named routes)
│   └── wsgi.py                # Django WSGI interface
│
├── templates/                 # Bootstrap 5 responsive enterprise templates (34 templates)
│   ├── accounts/              # Login, user list, profile, password change
│   ├── backup/                # Backup dashboard, restore status
│   ├── core/                  # naveen_dashboard.html, employee_dashboard.html, etc.
│   ├── invoices/              # upload.html, submission_detail.html, tally_workspace.html
│   └── base.html              # Enterprise deep navy layout with sidebar & mobile drawer
│
├── tests/                     # Automated test suite (204 test cases across all modules)
│   ├── test_authentication.py
│   ├── test_backup.py
│   ├── test_database.py
│   ├── test_extraction_accuracy.py
│   ├── test_gemini_vision.py
│   ├── test_invoices.py
│   ├── test_phase4_validation.py
│   ├── test_production_deployment_readiness.py
│   └── test_safe_pure_crop.py
│
├── serve_production.py        # Production Waitress WSGI server entrypoint
├── manage.py                  # Django management script
├── requirements.txt           # Production Python dependencies
├── pytest.ini                 # Pytest configuration
├── .env                       # Environment configuration file
└── README.md                  # Comprehensive project documentation
```

---

## 📞 Support & Maintenance
Developed for **Balaji Transports** internal operations.
For technical inquiries, module enhancements, or support, refer to the development documentation.
