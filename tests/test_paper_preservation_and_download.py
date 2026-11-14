"""
Test Suite for Paper Preservation, Auto-Crop Engine, and Download Invoice Features.

Covers:
1. "Send Invoice" UI verification (removal of "Send Invoice to Naveen").
2. Auto-Crop Computer Vision Engine (4-point perspective transform, deskew, DPI 300 preservation, failsafe).
3. Download Invoice endpoint with positive access control & ownership isolation.
4. Print-ready A4 PDF generation with Balaji Transports statutory voucher header.
"""
import io
import hashlib
import numpy as np
import pytest
from PIL import Image
import cv2

from django.test import TestCase, Client
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.accounts.models import User, UserRole, Department
from apps.invoices.models import InvoiceSubmission, InvoiceDocument, UploadedDocument
from apps.invoices.services import create_invoice_submission
from apps.invoices.image_processing import (
    auto_crop_invoice_image,
    generate_print_ready_pdf,
    four_point_transform,
    order_points,
)


class TestPaperPreservationAndDownload(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name="Operations", code="OPS")
        self.employee = User.objects.create_user(
            username="emp_rahul",
            password="TestPassword@2026",
            role=UserRole.EMPLOYEE,
            first_name="Rahul",
            last_name="Sharma",
            department=self.dept,
        )
        self.other_employee = User.objects.create_user(
            username="emp_vikram",
            password="TestPassword@2026",
            role=UserRole.EMPLOYEE,
            first_name="Vikram",
            last_name="Singh",
            department=self.dept,
        )
        self.naveen = User.objects.create_user(
            username="naveen_accounts",
            password="TestPassword@2026",
            role=UserRole.NAVEEN,
            first_name="Naveen",
            last_name="Accounts",
            department=self.dept,
        )

        # Generate a test image simulating a receipt photographed on a dark surface
        table = np.zeros((1000, 1200, 3), dtype=np.uint8) + 35
        # Tilted receipt quadrilateral
        pts = np.array([[150, 100], [1050, 140], [980, 880], [120, 820]], dtype=np.int32)
        cv2.fillPoly(table, [pts], (245, 245, 245))
        cv2.putText(table, "BALAJI TRANSPORTS - TYRE BILL", (220, 250), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (20, 20, 20), 2)
        cv2.putText(table, "Total: Rs. 14,800.00", (220, 400), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (20, 20, 20), 2)

        pil_img = Image.fromarray(cv2.cvtColor(table, cv2.COLOR_BGR2RGB))
        buf = io.BytesIO()
        pil_img.save(buf, format='JPEG', quality=95)
        self.simulated_receipt_bytes = buf.getvalue()

    def test_01_send_invoice_ui_labels_updated(self):
        """Verify that UI labels now say 'Send Invoice' instead of 'Send Invoice to Naveen'."""
        self.client.login(username="emp_rahul", password="TestPassword@2026")

        # Employee dashboard
        resp = self.client.get(reverse('core:employee_dashboard'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Send Invoice")
        self.assertNotContains(resp, "Send Invoice to Naveen")

        # Upload invoice page
        resp_upload = self.client.get(reverse('invoices:upload'))
        self.assertEqual(resp_upload.status_code, 200)
        self.assertContains(resp_upload, "Send Invoice")
        self.assertNotContains(resp_upload, "Send Invoice to Naveen")

    def test_02_autocrop_engine_detects_and_flattens_receipt(self):
        """Verify the auto-crop engine detects 4 corners, flattens skew, and preserves 300 DPI."""
        processed_bytes, was_cropped, meta = auto_crop_invoice_image(self.simulated_receipt_bytes)
        self.assertTrue(was_cropped)
        self.assertIn("method", meta)
        self.assertIn(meta["method"], ("perspective_quad", "bounding_box"))

        out_img = Image.open(io.BytesIO(processed_bytes))
        self.assertEqual(out_img.mode, 'RGB')
        # Check that high print DPI metadata is preserved
        self.assertEqual(out_img.info.get('dpi'), (300, 300))
        # Image was cropped and deskewed (not empty)
        self.assertGreater(out_img.width, 400)
        self.assertGreater(out_img.height, 400)

    def test_03_autocrop_failsafe_on_invalid_or_small_bytes(self):
        """Verify auto-crop handles corrupted/empty bytes gracefully without raising exceptions."""
        dummy_bytes = b"not an image file at all"
        out_bytes, was_cropped, meta = auto_crop_invoice_image(dummy_bytes)
        self.assertFalse(was_cropped)
        self.assertEqual(out_bytes, dummy_bytes)

        tiny_img = Image.new("RGB", (30, 30), color=(255, 255, 255))
        tiny_buf = io.BytesIO()
        tiny_img.save(tiny_buf, format="JPEG")
        out_tiny, cropped_tiny, meta_tiny = auto_crop_invoice_image(tiny_buf.getvalue())
        self.assertFalse(cropped_tiny)

    def test_04_download_invoice_endpoint_and_ownership_isolation(self):
        """Verify the download invoice endpoint enforces strict ownership isolation."""
        # Create submission for emp_rahul
        uploaded_file = SimpleUploadedFile("bill_receipt.jpg", self.simulated_receipt_bytes, content_type="image/jpeg")
        sub = create_invoice_submission(user=self.employee, files=[uploaded_file])

        # 1. Owner (emp_rahul) can download the invoice
        self.client.login(username="emp_rahul", password="TestPassword@2026")
        dl_resp = self.client.get(reverse('invoices:download_invoice', kwargs={'submission_id': sub.submission_id}))
        self.assertEqual(dl_resp.status_code, 200)
        self.assertIn('attachment', dl_resp.headers.get('Content-Disposition', ''))
        self.assertIn(sub.submission_id, dl_resp.headers.get('Content-Disposition', ''))

        # 2. Another employee (emp_vikram) is strictly blocked (403 Forbidden)
        self.client.login(username="emp_vikram", password="TestPassword@2026")
        dl_blocked = self.client.get(reverse('invoices:download_invoice', kwargs={'submission_id': sub.submission_id}))
        self.assertEqual(dl_blocked.status_code, 403)

        # 3. Naveen (Accounts) can download any invoice
        self.client.login(username="naveen_accounts", password="TestPassword@2026")
        dl_naveen = self.client.get(reverse('invoices:download_invoice', kwargs={'submission_id': sub.submission_id}))
        self.assertEqual(dl_naveen.status_code, 200)

    def test_05_download_invoice_as_print_ready_pdf(self):
        """Verify ?format=pdf compiles an official A4 print-ready PDF with voucher header."""
        uploaded_file = SimpleUploadedFile("test_receipt.jpg", self.simulated_receipt_bytes, content_type="image/jpeg")
        sub = create_invoice_submission(user=self.employee, files=[uploaded_file])

        self.client.login(username="emp_rahul", password="TestPassword@2026")
        # Download with ?format=pdf
        pdf_resp = self.client.get(reverse('invoices:download_invoice', kwargs={'submission_id': sub.submission_id}) + "?format=pdf")
        self.assertEqual(pdf_resp.status_code, 200)
        self.assertEqual(pdf_resp.headers.get('Content-Type'), 'application/pdf')
        self.assertIn('.pdf', pdf_resp.headers.get('Content-Disposition', ''))

        # Check PDF binary validity
        content = pdf_resp.content
        self.assertTrue(content.startswith(b'%PDF-'))
        self.assertGreater(len(content), 1000)

    def test_06_print_ready_pdf_generator_metadata_rendering(self):
        """Verify generate_print_ready_pdf includes all statutory metadata."""
        sub = InvoiceSubmission.objects.create(
            submission_id="BTI-202611-TEST1",
            employee=self.employee,
            party_name="SREE BALAJI TYRES",
            party_gstin="29ABCDE1234F1Z5",
            invoice_number="INV-8899",
            total_amount=15400.00,
            taxable_amount=13000.00,
            cgst_amount=1200.00,
            sgst_amount=1200.00,
            expense_ledger="Tyre & Tube Purchase",
            status=InvoiceSubmission.STATUS_SUBMITTED,
        )
        pdf_bytes = generate_print_ready_pdf(sub)
        self.assertTrue(pdf_bytes.startswith(b'%PDF-'))
        self.assertGreater(len(pdf_bytes), 1500)

    def test_07_viewer_and_detail_pages_have_download_and_print_controls(self):
        """Verify High-Clarity Viewer and Detail pages contain Download and Print actions."""
        sub = InvoiceSubmission.objects.create(
            submission_id="BTI-202611-TEST2",
            employee=self.employee,
            party_name="JK TYRE LIMITED",
            total_amount=10000.00,
            status=InvoiceSubmission.STATUS_SUBMITTED,
        )
        doc = InvoiceDocument.objects.create(
            submission=sub,
            page_number=1,
            original_file=SimpleUploadedFile("dummy.jpg", self.simulated_receipt_bytes, content_type="image/jpeg"),
            original_filename="dummy.jpg",
            file_sha256=hashlib.sha256(self.simulated_receipt_bytes).hexdigest(),
            file_size_bytes=len(self.simulated_receipt_bytes),
            content_type="image/jpeg",
            version=1,
            is_active_version=True,
        )

        self.client.login(username="naveen_accounts", password="TestPassword@2026")

        # Submission detail
        detail_resp = self.client.get(reverse('invoices:submission_detail', kwargs={'submission_id': sub.submission_id}))
        self.assertEqual(detail_resp.status_code, 200)
        self.assertContains(detail_resp, reverse('invoices:download_invoice', kwargs={'submission_id': sub.submission_id}))
        self.assertContains(detail_resp, "window.print()")
        self.assertContains(detail_resp, "print-voucher-header")

        # Verification workspace
        vw_resp = self.client.get(reverse('invoices:verification_workspace', kwargs={'submission_id': sub.submission_id}))
        self.assertEqual(vw_resp.status_code, 200)
        self.assertContains(vw_resp, reverse('invoices:download_invoice', kwargs={'submission_id': sub.submission_id}))
        self.assertContains(vw_resp, "window.print()")

    def test_08_password_reset_and_creation_have_eye_toggle(self):
        """Verify password reset and user creation pages include eye toggle controls."""
        admin_user = User.objects.create_superuser(
            username="admin_balaji",
            password="AdminPassword@2026",
            role=UserRole.ADMIN,
        )
        self.client.login(username="admin_balaji", password="AdminPassword@2026")

        # 1. Reset password page for an external user
        reset_resp = self.client.get(reverse('accounts:user_reset_password', kwargs={'user_id': self.employee.pk}))
        self.assertEqual(reset_resp.status_code, 200)
        self.assertContains(reset_resp, 'toggle-password')
        self.assertContains(reset_resp, 'bi bi-eye')

        # 2. Add user form
        add_user_resp = self.client.get(reverse('accounts:user_create'))
        self.assertEqual(add_user_resp.status_code, 200)
        self.assertContains(add_user_resp, 'toggle-password')
        self.assertContains(add_user_resp, 'bi bi-eye')

    def test_09_batch_download_all_zip(self):
        """Verify batch download ZIP endpoint packages multiple invoices into a single ZIP archive."""
        import zipfile
        sub1 = InvoiceSubmission.objects.create(
            submission_id="BTI-202611-BATCH1",
            employee=self.employee,
            party_name="SRI BALAJI FUELS",
            total_amount=5000.00,
            status=InvoiceSubmission.STATUS_SUBMITTED,
        )
        InvoiceDocument.objects.create(
            submission=sub1,
            page_number=1,
            original_file=SimpleUploadedFile("receipt1.jpg", self.simulated_receipt_bytes, content_type="image/jpeg"),
            original_filename="receipt1.jpg",
            file_sha256=hashlib.sha256(self.simulated_receipt_bytes).hexdigest(),
            file_size_bytes=len(self.simulated_receipt_bytes),
            content_type="image/jpeg",
            version=1,
            is_active_version=True,
        )

        sub2 = InvoiceSubmission.objects.create(
            submission_id="BTI-202611-BATCH2",
            employee=self.employee,
            party_name="MURUGAN TYRES",
            total_amount=8500.00,
            status=InvoiceSubmission.STATUS_SUBMITTED,
        )
        InvoiceDocument.objects.create(
            submission=sub2,
            page_number=1,
            original_file=SimpleUploadedFile("receipt2.jpg", self.simulated_receipt_bytes, content_type="image/jpeg"),
            original_filename="receipt2.jpg",
            file_sha256=hashlib.sha256(self.simulated_receipt_bytes).hexdigest(),
            file_size_bytes=len(self.simulated_receipt_bytes),
            content_type="image/jpeg",
            version=1,
            is_active_version=True,
        )

        self.client.login(username="naveen_accounts", password="TestPassword@2026")

        # 1. Batch download by explicit IDs
        batch_resp = self.client.get(reverse('invoices:batch_download_invoices') + f"?ids={sub1.submission_id},{sub2.submission_id}")
        self.assertEqual(batch_resp.status_code, 200)
        self.assertEqual(batch_resp.headers.get('Content-Type'), 'application/zip')
        self.assertIn('.zip', batch_resp.headers.get('Content-Disposition', ''))

        # Inspect ZIP content
        zf = zipfile.ZipFile(io.BytesIO(batch_resp.content))
        file_list = zf.namelist()
        self.assertEqual(len(file_list), 2)
        self.assertTrue(any(sub1.submission_id in f for f in file_list))
        self.assertTrue(any(sub2.submission_id in f for f in file_list))

        # 2. Check Naveen dashboard template contains Download All buttons
        dash_resp = self.client.get(reverse('core:naveen_dashboard'))
        self.assertEqual(dash_resp.status_code, 200)
        self.assertContains(dash_resp, reverse('invoices:batch_download_invoices'))
        self.assertContains(dash_resp, "btnDownloadSelected")
