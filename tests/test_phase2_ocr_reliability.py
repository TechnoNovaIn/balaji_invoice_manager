import os
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pymupdf as fitz
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import OperationalError
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from apps.accounts.models import AuditLog, User, UserRole
from apps.invoices.models import InvoiceDocument, InvoiceSubmission, UploadedDocument
from apps.invoices.performance_timer import PerformanceTimer
from apps.invoices.segmentation import (
    _build_gemini_pdf_subset,
    segment_and_extract_pdf,
    should_invoke_gemini_fallback,
)
from apps.invoices.tasks import (
    dispatch_background_processing,
    get_ocr_executor,
    get_ocr_queue_stats,
    process_uploaded_document_task,
    shutdown_ocr_executor,
)


def _make_pdf_bytes(num_pages: int = 1, text_prefix: str = "TAX INVOICE") -> bytes:
    doc = fitz.open()
    for i in range(num_pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"{text_prefix} Page {i + 1} of {num_pages}")
    data = doc.tobytes()
    doc.close()
    return data


class Phase2GeminiAndSegmentationTests(TestCase):
    def test_should_invoke_gemini_fallback_policy_and_completeness(self):
        with override_settings(GEMINI_API_KEY="test-key-123", GEMINI_FALLBACK_POLICY="auto"):
            complete_meta = {
                "party_name": "JK TYRE",
                "invoice_number": "INV-100",
                "total_amount": Decimal("1500.00"),
                "e_invoice_status": "NO",
                "irn_number": "",
            }
            self.assertFalse(
                should_invoke_gemini_fallback(complete_meta, {"document_type": "PRINTED"})
            )
            self.assertTrue(
                should_invoke_gemini_fallback(complete_meta, {"document_type": "HANDWRITTEN"})
            )
            missing_inv = dict(complete_meta, invoice_number="")
            self.assertTrue(
                should_invoke_gemini_fallback(missing_inv, {"document_type": "PRINTED"})
            )
            self.assertFalse(
                should_invoke_gemini_fallback(missing_inv, {"document_type": "PRINTED"}, policy="never")
            )
            self.assertTrue(
                should_invoke_gemini_fallback(complete_meta, {"document_type": "PRINTED"}, policy="always")
            )

        with override_settings(GEMINI_API_KEY=""):
            self.assertFalse(
                should_invoke_gemini_fallback({"party_name": ""}, {"document_type": "HANDWRITTEN"}, policy="always")
            )

    def test_build_gemini_pdf_subset_caps_pages_to_max(self):
        pdf_bytes = _make_pdf_bytes(num_pages=6)
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            subset_bytes = _build_gemini_pdf_subset(doc, seg_pages=[0, 1, 2, 3, 4, 5], full_segment_pdf_bytes=pdf_bytes, max_pages=3)
            self.assertIsNotNone(subset_bytes)
            sub_doc = fitz.open(stream=subset_bytes, filetype="pdf")
            try:
                self.assertEqual(len(sub_doc), 3)
                self.assertIn("Page 1 of 6", sub_doc[0].get_text())
                self.assertIn("Page 6 of 6", sub_doc[2].get_text())
            finally:
                sub_doc.close()
        finally:
            doc.close()

    def test_segment_and_extract_pdf_invokes_gemini_fallback_and_falls_back_cleanly(self):
        pdf_bytes = _make_pdf_bytes(num_pages=2, text_prefix="HANDWRITTEN MEMO")
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            tf.write(pdf_bytes)
            tmp_path = tf.name

        try:
            timer = PerformanceTimer(total_pages=2)
            mock_gemini = {
                "party_name": "NATIONAL TRAVELS",
                "party_gstin": "29AABFN1997J2ZQ",
                "invoice_number": "NT-2026-55",
                "total_amount": Decimal("2500.00"),
                "e_invoice_status": "NO",
                "is_e_invoice": False,
                "irn_number": "",
                "e_invoice_source": "GEMINI",
                "extraction_method": "GEMINI_VISION_AI",
                "gemini_usage": {"model": "gemini-2.5-flash", "latency_ms": 120.5, "total_tokens": 500},
            }
            with override_settings(GEMINI_API_KEY="dummy-key", GEMINI_FALLBACK_POLICY="always"):
                with patch("apps.invoices.gemini_extractor.extract_with_gemini", return_value=mock_gemini) as m_gem:
                    segs = segment_and_extract_pdf(tmp_path, timer=timer)
                    self.assertGreaterEqual(len(segs), 1)
                    self.assertTrue(m_gem.called)
                    self.assertEqual(segs[0]["metadata"]["party_name"], "NATIONAL TRAVELS")
                    self.assertEqual(segs[0]["metadata"]["extraction_method"], "GEMINI_VISION_AI")

                # Graceful fallback when Gemini raises / returns None
                with patch("apps.invoices.gemini_extractor.extract_with_gemini", side_effect=RuntimeError("API down")):
                    segs_fb = segment_and_extract_pdf(tmp_path, timer=timer)
                    self.assertGreaterEqual(len(segs_fb), 1)
                    self.assertNotEqual(segs_fb[0]["metadata"].get("extraction_method"), "GEMINI_VISION_AI")
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)


class Phase2BoundedWorkerAndRollbackTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="p2_emp",
            password="StrongPassword#2026!",
            role=UserRole.EXTERNAL_SUBMITTER,
        )

    def test_bounded_executor_defaults_to_two_workers_and_shutdown(self):
        with override_settings(OCR_MAX_WORKERS=2):
            shutdown_ocr_executor(wait=False)
            # Reset _SHUTTING_DOWN for fresh pool creation
            import apps.invoices.tasks as tasks_mod
            tasks_mod._SHUTTING_DOWN = False
            executor = get_ocr_executor()
            self.assertEqual(executor._max_workers, 2)
            stats = get_ocr_queue_stats()
            self.assertEqual(stats["max_workers"], 2)
            shutdown_ocr_executor(wait=False)
            tasks_mod._SHUTTING_DOWN = False

    def test_stage5_rollback_cleans_up_orphaned_media_files(self):
        with tempfile.TemporaryDirectory() as tmp_media:
            with override_settings(MEDIA_ROOT=tmp_media):
                pdf_bytes = _make_pdf_bytes(2)
                doc = UploadedDocument.objects.create(
                    uploader=self.user,
                    original_file=SimpleUploadedFile("rollback.pdf", pdf_bytes, content_type="application/pdf"),
                    file_name="rollback.pdf",
                    file_sha256="c" * 64,
                    file_size_bytes=len(pdf_bytes),
                    content_type="application/pdf",
                    processing_status=UploadedDocument.STATUS_QUEUED,
                )
                fake_segs = [{
                    "invoice_index": 1,
                    "start_page": 1,
                    "end_page": 1,
                    "total_pages_in_invoice": 1,
                    "pages": [0],
                    "sub_files": {
                        "page_images": [{"image_bytes": b"\xff\xd8\xff\xd9", "page_number_in_src": 1}],
                        "pdf_bytes": pdf_bytes,
                    },
                    "metadata": {
                        "party_name": "ROLLBACK VENDOR",
                        "party_gstin": "",
                        "invoice_number": "RB-1",
                        "total_amount": Decimal("100.00"),
                        "raw_text": "test",
                        "e_invoice_status": "NO",
                        "is_e_invoice": False,
                        "irn_number": "",
                        "e_invoice_source": "OCR",
                    },
                }]

                # Force OperationalError on the second InvoiceDocument.objects.create (the PDF) after image was saved
                orig_create = InvoiceDocument.objects.create
                call_counter = {"n": 0}

                def flaky_create(**kwargs):
                    call_counter["n"] += 1
                    if call_counter["n"] == 2:
                        raise OperationalError("database is locked")
                    return orig_create(**kwargs)

                with patch("apps.invoices.tasks.segment_and_extract_pdf", return_value=fake_segs), \
                     patch("apps.invoices.db_utils.time.sleep"), \
                     patch.object(InvoiceDocument.objects, "create", side_effect=flaky_create):
                    subs = process_uploaded_document_task(doc.id)

                # First attempt failed on 2nd InvoiceDocument.create (n=2) and rolled back; retry succeeded (n=3,4)
                self.assertEqual(call_counter["n"], 4)
                self.assertEqual(len(subs), 1)
                self.assertEqual(InvoiceSubmission.objects.filter(source_document=doc).count(), 1)
                self.assertEqual(InvoiceDocument.objects.filter(source_document=doc).count(), 2)
                # Verify rolled-back page image file was cleaned up (only the committed pg1 jpg exists)
                inv_docs = list(InvoiceDocument.objects.filter(source_document=doc))
                jpg_doc = next(d for d in inv_docs if d.content_type == "image/jpeg")
                saved_pg_files = list(Path(jpg_doc.original_file.path).parent.glob("*.jpg"))
                self.assertEqual(len(saved_pg_files), 1)
                self.assertTrue(jpg_doc.original_file.storage.exists(jpg_doc.original_file.name))


class Phase2RetryAndVisibilityTests(TestCase):
    def setUp(self):
        self.emp1 = User.objects.create_user(
            username="p2_sub1",
            password="StrongPassword#2026!",
            role=UserRole.EXTERNAL_SUBMITTER,
        )
        self.emp2 = User.objects.create_user(
            username="p2_sub2",
            password="StrongPassword#2026!",
            role=UserRole.EXTERNAL_SUBMITTER,
        )
        self.naveen = User.objects.create_user(
            username="p2_naveen",
            password="StrongPassword#2026!",
            role=UserRole.NAVEEN,
        )
        self.mgmt = User.objects.create_user(
            username="p2_mgmt",
            password="StrongPassword#2026!",
            role=UserRole.MANAGEMENT,
        )

    def _create_failed_doc(self, owner, retry_count=0):
        pdf_bytes = _make_pdf_bytes(1)
        return UploadedDocument.objects.create(
            uploader=owner,
            original_file=SimpleUploadedFile("failed_bill.pdf", pdf_bytes, content_type="application/pdf"),
            file_name="failed_bill.pdf",
            file_sha256="d" * 64,
            file_size_bytes=len(pdf_bytes),
            content_type="application/pdf",
            processing_status=UploadedDocument.STATUS_FAILED,
            error_message="Simulated OCR timeout",
            retry_count=retry_count,
        )

    def test_retry_rbac_isolation_limits_and_audit_log(self):
        with tempfile.TemporaryDirectory() as tmp_media:
            with override_settings(MEDIA_ROOT=tmp_media, OCR_MAX_RETRIES=2):
                doc = self._create_failed_doc(self.emp1, retry_count=0)
                url = reverse("invoices:retry_failed_upload", args=[doc.id])

                # Other submitter denied
                self.client.force_login(self.emp2)
                self.assertEqual(self.client.post(url).status_code, 403)

                # Management denied
                self.client.force_login(self.mgmt)
                self.assertEqual(self.client.post(url).status_code, 403)

                # Owner can retry
                self.client.force_login(self.emp1)
                with patch("apps.invoices.views.dispatch_background_processing") as mock_disp:
                    resp = self.client.post(url)
                    self.assertEqual(resp.status_code, 302)
                    mock_disp.assert_called_once_with(uploaded_document_id=doc.id)

                doc.refresh_from_db()
                self.assertEqual(doc.processing_status, UploadedDocument.STATUS_QUEUED)
                self.assertEqual(doc.retry_count, 1)
                self.assertEqual(len(doc.last_error_history), 1)
                self.assertTrue(AuditLog.objects.filter(action="UPLOAD_RETRY_QUEUED", user=self.emp1).exists())

                # Cannot retry while QUEUED
                with patch("apps.invoices.views.dispatch_background_processing") as mock_disp2:
                    self.client.post(url)
                    mock_disp2.assert_not_called()

                # Retry limit enforced once retry_count == OCR_MAX_RETRIES
                doc.processing_status = UploadedDocument.STATUS_FAILED
                doc.retry_count = 2
                doc.save(update_fields=["processing_status", "retry_count"])
                self.client.force_login(self.naveen)
                with patch("apps.invoices.views.dispatch_background_processing") as mock_disp3:
                    self.client.post(url)
                    mock_disp3.assert_not_called()

    def test_processing_status_visible_on_submitter_and_naveen_dashboards(self):
        with tempfile.TemporaryDirectory() as tmp_media:
            with override_settings(MEDIA_ROOT=tmp_media):
                doc = self._create_failed_doc(self.emp1, retry_count=0)

                self.client.force_login(self.emp1)
                emp_html = self.client.get(reverse("core:employee_dashboard")).content.decode("utf-8")
                self.assertIn("Upload Processing Status", emp_html)
                self.assertIn("failed_bill.pdf", emp_html)
                self.assertIn("Retry (0/3)", emp_html)

                # emp2 must not see emp1's failed upload
                self.client.force_login(self.emp2)
                emp2_html = self.client.get(reverse("core:employee_dashboard")).content.decode("utf-8")
                self.assertNotIn("failed_bill.pdf", emp2_html)

                # Naveen sees centralized queue
                self.client.force_login(self.naveen)
                nav_html = self.client.get(reverse("core:naveen_dashboard")).content.decode("utf-8")
                self.assertIn("Background Upload &amp; OCR Queue", nav_html)
                self.assertIn("failed_bill.pdf", nav_html)
                self.assertIn("Retry (0/3)", nav_html)
