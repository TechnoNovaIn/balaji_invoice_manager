# Balaji InvoiceHub

> Internal Operations, Invoice Processing, Tally Workflow & Disaster Recovery Platform for **Balaji Transports**.

---

## 📌 Project Overview

**Balaji InvoiceHub** replaces chaotic WhatsApp and email chains with a clean, centralized enterprise platform for managing invoice receipts collected by employees across transport operations.

Employees simply snap photos or upload PDF receipts from their phones, which immediately stream in chronological order to **Naveen (Accounts)**. Naveen can view clear, high-resolution bills on his screen, enter them directly into his local Tally software, check them off as entered, and export official GST Portal-compliant Excel spreadsheets for monthly return filing.
-
---

## 🚦 Current Project Phase Status

| Phase | Module Name | Status | Details |
|---|---|---|---|
| **Phase 1** | **Project Foundation & RBAC** | ✅ **COMPLETED** | Custom User model, PostgreSQL 18, 4 distinct roles (Employee, Naveen, Admin, Management), session security. |
| **Phase 2** | **Disaster Recovery & Permanent Backup** | ✅ **COMPLETED** | Automated `pg_dump` custom format, ZIP document archives, SHA-256 checksum verification, 10-year permanent retention, sandbox restore drills. |
| **UI** | **Enterprise Navy & Teal UI Redesign** | ✅ **COMPLETED** | Responsive mobile drawer, desktop sidebar, professional enterprise theme, zero dummy data. |
| **Phase 3** | **Employee Mobile Capture & Naveen Feed** | ✅ **COMPLETED** | Simple touch camera/file upload up to 100 MB, chronological feed, high-clarity viewer with 1:1 pixels, 1-click Tally batch check-off, missed bill carryover. |
| **Phase 4** | **Intelligent Document Reader & GSTR-2A Engine** | ✅ **COMPLETED & STABILIZED** | Automated PDF/image parsing, Google Gemini Vision AI, independent PyMuPDF digital preservation, 237 Authoritative Vendor Master database records, Rules A–E financial validation safety gates, 104/104 passing tests. |
| **Phase 5** | **Ledger, Advance Reconciliation & Settlements**| 🚀 **NEXT UP** | Advance logging, party receivables, balance settlements. |
| **Phase 6** | **Advanced Excel-to-Tally Conversion** | 📋 **PLANNED** | Configurable mapping and XML batch generation for TallyPrime and Tally.ERP 9. |
| **Phase 7** | **WhatsApp Reminder Engine & Analytics** | 📋 **PLANNED** | Automated WhatsApp reminders and executive reports. |

> **Current Position:** We have successfully completed and stabilized **Phases 1, 2, 3, and 4** with **104/104 passing tests**, zero deprecation warnings, and an authoritative Vendor Master database. The system is hardened and ready for **Phase 5 (Advance Reconciliation & Settlements)**.

---

## ⚙️ Technology Stack

* **Backend:** Python 3.13.4, Django 5.x
* **Database:** PostgreSQL 18.6 (64-bit on Windows)
* **Frontend:** Bootstrap 5.3, Bootstrap Icons, responsive mobile offcanvas drawer + desktop sidebar
* **Spreadsheet Engine:** `openpyxl` (GST Portal B2B Excel generation & conversion)
* **Security & RBAC:** Django Custom User, session security, authenticated document streaming, server-side MIME magic-byte validation, and SHA-256 deduplication

---

## 🚀 Key Modules & Capabilities Implemented

### 1. Employee Mobile-First Invoice Capture & Seamless Login
* **First-Time Password Change:** When an employee signs in with the initial demo credentials (`employee` / `BalajiEmp@2026`), the system automatically guides them to set their own personal password with show/hide eye toggles.
* **Save Once, Never Forget (Phone Password Manager):**
  * Fully compliant with Google Password Manager, Apple Keychain, and Samsung Pass (`autocomplete="username"`, `autocomplete="current-password"`).
  * Employees tap **"Save Password"** on their mobile browser once, and their phone will auto-fill credentials for 1-tap logins.
* **Persistent Mobile Session ("Keep me signed in on this phone"):**
  * 1-year persistent session lifespan (`SESSION_COOKIE_AGE = 31536000`, `SESSION_EXPIRE_AT_BROWSER_CLOSE = False`).
  * Field employees do not need to log in repeatedly every day. Closing the browser or restarting their phone keeps them logged in.
* **Zero Crash / Break Protection:**
  * Double-submit prevention with dynamic upload progress spinner.
  * Handles up to **100 MB** high-resolution camera photos and multi-page PDFs without memory or gateway timeouts (`DATA_UPLOAD_MAX_MEMORY_SIZE = 100MB`).
  * PWA web app meta tags for full-screen "Add to Home Screen" mobile experience.
* **Zero Metadata Friction:** Employees are not required to type party names, trip details, vehicle numbers, or amounts.
* **Large Touch Controls:**
  * `[ Take Photo ]`: Direct access to phone camera (`accept="image/*" capture="environment"`).
  * `[ Upload PDF / Files ]`: Gallery and document picker supporting multi-page photos or PDF invoices.
* **Client-Side Tools:** Instant thumbnail previews, 90° image rotation, and per-page removal prior to submission.
* **Integrity & Deduplication:** Concurrency-safe monthly tracking ID (`BTI-YYYYMM-XXXXX`), cryptographic SHA-256 hashing, and duplicate re-upload blocking within 24 hours.

### 2. Naveen's Centralized Accounts Feed (No WhatsApp Chaos)
* **Direct Ordered Feed:** Invoices stream directly to Naveen in chronological arrival order (#1, #2, #3...) matching field submissions.
* **Right-Side Batch Checkboxes & End-of-Day Completion:**
  * As Naveen enters bills into his local Tally software, he checks the box on the **right side** of each invoice row.
  * Real-time selection counter (`X selected`) with row highlighting.
  * At the end of the day, clicks a single **`[ Save Day's Entries & Mark Completed ]`** button to commit all entered vouchers at once.
* **Daily Fresh Feed & Missed/Carry-Over Logic:**
  * **Daily Fresh Feed:** When all invoices are checked and completed, the feed presents a clean zero-backlog state for the next day, ready for incoming bills.
  * **Carry-Over Warning:** If Naveen missed any invoices from previous days, they are highlighted in an alert section (`⚠️ Carried Over From Previous Days`) at the top of the feed so no bill is ever overlooked.
* **Organized Tabs:**
  * **Active Daily Feed (Pending):** Unentered invoices separated into Carried Over and Today's incoming bills.
  * **Completed Archive:** Historical record of bills entered into Tally with batch/single Undo options.
  * **All Invoices:** Complete searchable ledger.
* **Instant Search:** Find any invoice by employee name, tracking ID, or date.

### 3. High-Clarity Bill Viewer
* **Side-by-Side Screen Layout:** Naveen keeps Balaji InvoiceHub open on one screen and his Tally software on the other.
* **Viewer Controls:**
  * **Zoom In (`+`) & Zoom Out (`-`)** for small receipts and tax stamps.
  * **Rotate 90° (`↻`)** for sideways mobile captures.
  * **Reset Zoom & Fullscreen** view.
  * **Sequential Navigation:** `Previous Bill` and `Next Bill →` buttons to flip through receipts quickly.

### 4. GST Portal Compliant Excel Tool
* **Official GST Portal B2B Excel Export:** Generates formatted `.xlsx` workbooks matching the exact government GST Offline Utility B2B schema:
  `GSTIN of Supplier | Trade/Legal Name | Invoice number | Invoice date | Invoice Value | Place of Supply | Reverse Charge | Rate | Taxable Value | Cess Amount`
* **Upload Converter:** Upload any external Excel purchase sheet or Tally export to convert it into the official GST Portal format.

### 5. Automated Disaster Recovery & Backup Engine (`apps.backup`)
* **pg_dump Integration:** Automated PostgreSQL database backups in native custom format (`.dump`).
* **Media Backup:** Automated ZIP archives of all original invoice documents.
* **Integrity Verification:** SHA-256 checksums computed and verified for both database dumps and media archives.
* **Retention Policy:** Configurable 30-day retention pruning keeping disk storage clean.
* **Tested Disaster Drills:** Verified non-destructive sandbox restoration into isolated PostgreSQL database (`balaji_test_restore`).

---

## 💻 Running the Application Locally

### Prerequisites
* Windows 10/11
* Python 3.13+
* PostgreSQL 18.x installed and running locally on port `5432`

### Start Web Server
```powershell
python manage.py runserver 127.0.0.1:8000
```
Open **`http://127.0.0.1:8000/`** in your browser.

### Run Automated Tests
```powershell
python manage.py test tests
```
*Current test suite: **43 of 43 tests passing (100% success)**.*

### Trigger Manual Backup Drill
```powershell
python manage.py run_invoicehub_backup
```

---

## 👥 Development Test Accounts

| Role | Username | Password | Purpose / Default Landing Page |
|---|---|---|---|
| **Employee** | `employee` | `BalajiEmp@2026` | Mobile upload portal (`/dashboard/employee/`) |
| **Naveen** | `naveen` | `BalajiNaveen@2026` | Accounts verification & Tally feed (`/dashboard/naveen/`) |
| **Admin** | `admin` | `BalajiAdmin@2026` | User administration & backup dashboard (`/dashboard/admin/`) |
| **Management** | `management` | `BalajiMgmt@2026` | Read-only executive oversight (`/dashboard/management/`) |

---

## 📁 Project Architecture Directory Map

```text
Balaji_IA_VM/
│
├── apps/
│   ├── accounts/          # Custom User model, RBAC decorators & user management
│   ├── backup/            # Automated pg_dump, SHA-256 verification & restore drills
│   ├── core/              # Role dashboards (Employee, Naveen, Admin, Management)
│   └── invoices/          # Invoice submissions, document storage, Tally & GST converter
│       ├── models.py      # InvoiceSubmission, InvoiceDocument, InvoiceSequence
│       ├── services.py    # File validation, MIME magic bytes, duplicate detection
│       ├── tally_exporter.py # TallyPrime & Tally.ERP 9 XML voucher generator
│       ├── gst_converter.py  # GST Portal B2B Excel (.xlsx) generator & converter
│       ├── urls.py
│       └── views.py
│
├── templates/             # Bootstrap 5 responsive templates
│   ├── accounts/
│   ├── backup/
│   ├── core/              # naveen_dashboard.html, employee_dashboard.html, etc.
│   ├── invoices/          # upload.html, submission_detail.html, gst_converter.html
│   └── base.html          # Enterprise deep navy layout with sidebar & drawer
│
├── tests/                 # Automated test suite (43 tests)
│   ├── test_authentication.py
│   ├── test_backup.py
│   ├── test_browser_endpoints.py
│   ├── test_database.py
│   ├── test_invoices.py
│   ├── test_models.py
│   ├── test_rbac_permissions.py
│   └── test_tally_gst.py
│
├── invoicehub/            # Django root configuration (settings.py, urls.py, wsgi.py)
├── manage.py
└── README.md
```

---

## 📞 Support & Maintenance
Developed for **Balaji Transports** internal operations.
For technical inquiries or module enhancements, refer to the development documentation.

