import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User, UserRole
from apps.invoices.models import InvoiceDocument, InvoiceSubmission, UploadedDocument
from apps.invoices.services import sanitize_filename, validate_uploaded_files


class Phase3RBACMatrixTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="p3_admin", password="StrongPassword#2026!", role=UserRole.ADMIN)
        self.naveen = User.objects.create_user(username="p3_naveen", password="StrongPassword#2026!", role=UserRole.NAVEEN)
        self.emp = User.objects.create_user(username="p3_emp", password="StrongPassword#2026!", role=UserRole.EMPLOYEE)
        self.ext1 = User.objects.create_user(username="p3_ext1", password="StrongPassword#2026!", role=UserRole.EXTERNAL_SUBMITTER)
        self.ext2 = User.objects.create_user(username="p3_ext2", password="StrongPassword#2026!", role=UserRole.EXTERNAL_SUBMITTER)
        self.mgmt = User.objects.create_user(username="p3_mgmt", password="StrongPassword#2026!", role=UserRole.MANAGEMENT)

        pdf_bytes = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
        self.ext1_source = UploadedDocument.objects.create(
            uploader=self.ext1,
            original_file=SimpleUploadedFile("ext1_bill.pdf", pdf_bytes, content_type="application/pdf"),
            file_name="ext1_bill.pdf",
            file_sha256="e" * 64,
            file_size_bytes=len(pdf_bytes),
            content_type="application/pdf",
            processing_status=UploadedDocument.STATUS_FAILED,
        )
        self.ext1_sub = InvoiceSubmission.objects.create(
            submission_id="BTI-202611-93001",
            employee=self.ext1,
            source_document=self.ext1_source,
            party_name="EXT1 VENDOR",
            invoice_number="EXT1-99",
            total_amount=Decimal("500.00"),
        )
        self.ext1_doc = InvoiceDocument.objects.create(
            submission=self.ext1_sub,
            source_document=self.ext1_source,
            page_number=1,
            original_file=SimpleUploadedFile("ext1_page1.pdf", pdf_bytes, content_type="application/pdf"),
            original_filename="ext1_page1.pdf",
            file_sha256="f" * 64,
            file_size_bytes=len(pdf_bytes),
            content_type="application/pdf",
        )

    def test_external_submitter_cannot_access_other_submitter_or_internal_endpoints(self):
        self.client.force_login(self.ext2)

        # Direct IDOR attempts on ext1's records must return 403
        idor_urls = [
            reverse("invoices:submission_detail", args=[self.ext1_sub.submission_id]),
            reverse("invoices:upload_progress", args=[self.ext1_source.id]),
            reverse("invoices:document_status_api", args=[self.ext1_source.id]),
            reverse("invoices:serve_document", args=[self.ext1_doc.id]),
            reverse("invoices:download_source_document", args=[self.ext1_source.id]),
        ]
        for url in idor_urls:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 403, f"Expected 403 for {url}, got {resp.status_code}")

        self.assertEqual(
            self.client.post(reverse("invoices:retry_failed_upload", args=[self.ext1_source.id])).status_code,
            403,
        )

        # Internal dashboards, Tally exports, and backups must be denied to external submitters
        internal_urls = [
            reverse("core:naveen_dashboard"),
            reverse("core:admin_dashboard"),
            reverse("core:management_dashboard"),
            reverse("invoices:tally_workspace", args=[self.ext1_sub.submission_id]),
            reverse("invoices:export_tally_excel"),
            reverse("invoices:export_tally_xml"),
            reverse("invoices:export_gst_csv"),
            reverse("backup:dashboard"),
        ]
        for url in internal_urls:
            resp = self.client.get(url)
            self.assertIn(resp.status_code, (302, 403), f"Expected 302/403 for {url}, got {resp.status_code}")
            if resp.status_code == 302:
                self.assertNotEqual(resp.url, url)

    def test_owner_can_access_own_records_and_naveen_can_access_accounts_but_not_admin_backup(self):
        self.client.force_login(self.ext1)
        self.assertEqual(self.client.get(reverse("invoices:submission_detail", args=[self.ext1_sub.submission_id])).status_code, 200)
        self.assertEqual(self.client.get(reverse("invoices:document_status_api", args=[self.ext1_source.id])).status_code, 200)

        self.client.force_login(self.naveen)
        self.assertEqual(self.client.get(reverse("core:naveen_dashboard")).status_code, 200)
        self.assertIn(self.client.get(reverse("backup:dashboard")).status_code, (302, 403))

        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("core:admin_dashboard")).status_code, 200)
        self.assertEqual(self.client.get(reverse("backup:dashboard")).status_code, 200)


class Phase3UploadAndPWASecurityTests(TestCase):
    def test_upload_rejects_spoofed_exe_extension_mismatch_and_sanitizes_traversal(self):
        # 1. Path traversal filename sanitization
        cleaned = sanitize_filename("../../etc/passwd/../../invoice.pdf")
        self.assertNotIn("..", cleaned)
        self.assertNotIn("/", cleaned)
        self.assertNotIn("\\", cleaned)

        # 2. Windows PE executable disguised as .pdf
        spoofed_exe = SimpleUploadedFile("evil.pdf", b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 100, content_type="application/pdf")
        with self.assertRaises(ValidationError):
            validate_uploaded_files([spoofed_exe])

        # 3. PDF magic bytes with .jpg extension
        mismatched = SimpleUploadedFile("fake.jpg", b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF\n", content_type="image/jpeg")
        with self.assertRaises(ValidationError):
            validate_uploaded_files([mismatched])

        # 4. Empty file rejected
        empty = SimpleUploadedFile("empty.pdf", b"", content_type="application/pdf")
        with self.assertRaises(ValidationError):
            validate_uploaded_files([empty])

    def test_pwa_manifest_and_service_worker_exclusions(self):
        manifest_path = Path(settings.BASE_DIR) / "static" / "manifest.json"
        sw_path = Path(settings.BASE_DIR) / "static" / "js" / "sw.js"

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        icons = manifest.get("icons", [])
        self.assertTrue(any(i.get("sizes") == "192x192" for i in icons))
        self.assertTrue(any(i.get("sizes") == "512x512" and i.get("purpose") == "any" for i in icons))
        self.assertTrue(any(i.get("sizes") == "512x512" and i.get("purpose") == "maskable" for i in icons))

        sw_js = sw_path.read_text(encoding="utf-8")
        for prefix in ("'/accounts/'", "'/invoices/'", "'/dashboard/'", "'/backup/'", "'/admin/'", "'/media/'"):
            self.assertIn(prefix, sw_js)
        self.assertIn("cache: 'no-store'", sw_js)

