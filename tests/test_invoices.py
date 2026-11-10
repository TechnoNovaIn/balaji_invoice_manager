import io
from datetime import timedelta
from django.test import TestCase
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import ValidationError
from django.db.models import ProtectedError
from django.utils import timezone

from apps.accounts.models import User, UserRole, Department
from apps.invoices.models import (
    InvoiceSubmission,
    InvoiceDocument,
    InvoiceStatusHistory,
    InvoiceSequence,
)
from apps.invoices.services import (
    validate_uploaded_files,
    check_duplicates,
    create_invoice_submission,
    resubmit_returned_invoice,
    MAX_FILES_PER_SUBMISSION,
    MAX_FILE_SIZE_BYTES,
)


def create_dummy_jpeg(name="test_invoice.jpg", content=b"fake_jpeg_body"):
    """Valid JPEG magic byte prefix \xFF\xD8\xFF"""
    return SimpleUploadedFile(
        name=name,
        content=b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + content,
        content_type="image/jpeg"
    )


def create_dummy_png(name="test_invoice.png", content=b"fake_png_body"):
    """Valid PNG magic byte prefix \x89PNG\r\n\x1a\n"""
    return SimpleUploadedFile(
        name=name,
        content=b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + content,
        content_type="image/png"
    )


def create_dummy_pdf(name="test_invoice.pdf", content=b"fake_pdf_body"):
    """Valid PDF magic byte prefix %PDF-"""
    return SimpleUploadedFile(
        name=name,
        content=b"%PDF-1.4\n%test pdf content\n" + content,
        content_type="application/pdf"
    )


def create_fake_file(name="malicious.jpg"):
    """Invalid file header disguised as JPG"""
    return SimpleUploadedFile(
        name=name,
        content=b"MZ\x90\x00\x03\x00\x00\x00This is a Windows executable disguise",
        content_type="image/jpeg"
    )


class InvoiceSubmissionEngineTests(TestCase):
    """
    Unit and integration tests for Phase 3 Employee Mobile Invoice Submission Engine.
    """

    def setUp(self):
        self.dept = Department.objects.create(name="Fleet Operations", code="FLEET")

        # Employee 1
        self.emp1 = User.objects.create_user(
            username="emp_karthik",
            password="TestPassword@123",
            role=UserRole.EMPLOYEE,
            first_name="Karthik",
            last_name="Raju",
            department=self.dept
        )

        # Employee 2
        self.emp2 = User.objects.create_user(
            username="emp_suresh",
            password="TestPassword@123",
            role=UserRole.EMPLOYEE,
            first_name="Suresh",
            last_name="Kumar",
            department=self.dept
        )

        # Naveen (Accounts Operator)
        self.naveen = User.objects.create_user(
            username="naveen_operator",
            password="TestPassword@123",
            role=UserRole.NAVEEN,
            first_name="Naveen",
            last_name="Accounts",
            department=self.dept
        )

        # Admin
        self.admin = User.objects.create_user(
            username="admin_sys",
            password="TestPassword@123",
            role=UserRole.ADMIN,
            first_name="System",
            last_name="Admin",
            department=self.dept
        )

    def test_tracking_id_sequence_generation(self):
        """Verify monthly sequence numbers follow BTI-YYYYMM-XXXXX pattern."""
        id1 = InvoiceSequence.get_next_submission_id()
        id2 = InvoiceSequence.get_next_submission_id()
        now_ym = timezone.now().strftime('%Y%m')

        self.assertTrue(id1.startswith(f"BTI-{now_ym}-"))
        self.assertTrue(id2.startswith(f"BTI-{now_ym}-"))
        self.assertEqual(id1, f"BTI-{now_ym}-00001")
        self.assertEqual(id2, f"BTI-{now_ym}-00002")

    def test_file_magic_byte_validation_rejection(self):
        """Executable disguised as JPEG must be rejected by magic-byte checking."""
        bad_file = create_fake_file("receipt.jpg")
        with self.assertRaises(ValidationError) as ctx:
            validate_uploaded_files([bad_file])
        self.assertIn("not a valid JPEG, PNG, or PDF", str(ctx.exception))

    def test_file_count_limit_enforcement(self):
        """More than 10 files in a single submission must be rejected."""
        files = [create_dummy_jpeg(f"bill_{i}.jpg", f"content_{i}".encode()) for i in range(11)]
        with self.assertRaises(ValidationError) as ctx:
            validate_uploaded_files(files)
        self.assertIn("maximum of 10 pages", str(ctx.exception))

    def test_single_file_size_limit_enforcement(self):
        """File larger than 100MB must be rejected."""
        large_content = b"\xff\xd8\xff\xe0" + b"A" * (100 * 1024 * 1024 + 100)
        oversized_file = SimpleUploadedFile(
            name="oversized.jpg",
            content=large_content,
            content_type="image/jpeg"
        )
        with self.assertRaises(ValidationError) as ctx:
            validate_uploaded_files([oversized_file])
        self.assertIn("exceeds maximum allowed size of 100 MB", str(ctx.exception))

    def test_create_valid_submission_without_remarks(self):
        """Employee submits pages without any remarks or text metadata."""
        doc1 = create_dummy_jpeg("page1.jpg", b"receipt_page_1")
        doc2 = create_dummy_pdf("page2.pdf", b"receipt_page_2")

        submission = create_invoice_submission(
            user=self.emp1,
            files=[doc1, doc2]
        )

        self.assertIsNotNone(submission.pk)
        self.assertEqual(submission.employee, self.emp1)
        self.assertEqual(submission.status, InvoiceSubmission.STATUS_SUBMITTED)
        self.assertEqual(submission.remarks, "")
        self.assertFalse(submission.is_duplicate_suspected)
        self.assertEqual(submission.documents.count(), 2)

    def test_create_valid_submission(self):
        """Employee submits 2 valid pages (JPEG and PDF) with remarks."""
        doc1 = create_dummy_jpeg("page1.jpg", b"receipt_page_1")
        doc2 = create_dummy_pdf("page2.pdf", b"receipt_page_2")

        submission = create_invoice_submission(
            user=self.emp1,
            files=[doc1, doc2],
            remarks="Lorry KA-01-AB-1234 diesel bunk receipt"
        )

        self.assertIsNotNone(submission.pk)
        self.assertEqual(submission.employee, self.emp1)
        self.assertEqual(submission.status, InvoiceSubmission.STATUS_SUBMITTED)
        self.assertEqual(submission.remarks, "Lorry KA-01-AB-1234 diesel bunk receipt")
        self.assertFalse(submission.is_duplicate_suspected)
        self.assertEqual(submission.documents.count(), 2)

        # Check status history was logged
        history = submission.status_history.first()
        self.assertIsNotNone(history)
        self.assertEqual(history.new_status, InvoiceSubmission.STATUS_SUBMITTED)
        self.assertEqual(history.changed_by, self.emp1)

    def test_block_same_employee_duplicate_within_24_hours(self):
        """Per explicit company requirement, identical duplicate uploads are permitted with an informational warning."""
        shared_content = b"identical_invoice_payload_123"
        doc1 = create_dummy_jpeg("fuel_bill.jpg", shared_content)
        sub1 = create_invoice_submission(user=self.emp1, files=[doc1], remarks="First trip")

        # Second upload with exact same file by same employee is permitted
        doc2 = create_dummy_jpeg("fuel_bill_reupload.jpg", shared_content)
        sub2 = create_invoice_submission(user=self.emp1, files=[doc2], remarks="Duplicate upload")

        self.assertIsNotNone(sub2.pk)
        self.assertNotEqual(sub1.submission_id, sub2.submission_id)
        self.assertTrue(sub2.is_duplicate_suspected)
        self.assertIn("matches submission", sub2.duplicate_notes)

    def test_cross_employee_suspected_duplicate_warning(self):
        """Same file uploaded by a DIFFERENT employee must NOT be blocked, but flagged for Naveen."""
        shared_content = b"shared_tyre_invoice_payload_999"
        doc1 = create_dummy_jpeg("tyres.jpg", shared_content)
        sub1 = create_invoice_submission(user=self.emp1, files=[doc1], remarks="Employee 1 upload")

        # Employee 2 uploads the same file
        doc2 = create_dummy_jpeg("tyres_emp2.jpg", shared_content)
        sub2 = create_invoice_submission(user=self.emp2, files=[doc2], remarks="Employee 2 upload")

        self.assertIsNotNone(sub2.pk)
        self.assertTrue(sub2.is_duplicate_suspected)
        self.assertIn("matches submission", sub2.duplicate_notes)
        self.assertIn(sub1.submission_id, sub2.duplicate_notes)

    def test_returned_invoice_resubmission_and_versioning(self):
        """
        Naveen returns invoice for blurriness -> Employee resubmits with new photo ->
        Original photo preserved as v1 (inactive), new photo active as v2.
        """
        doc_v1 = create_dummy_jpeg("blurry.jpg", b"blurry_content")
        submission = create_invoice_submission(user=self.emp1, files=[doc_v1], remarks="Original")

        # Naveen returns it
        submission.status = InvoiceSubmission.STATUS_RETURNED
        submission.return_reason = "Photo is out of focus. Please re-shoot."
        submission.save()

        # Employee resubmits with replacement photo
        doc_v2 = create_dummy_jpeg("sharp.jpg", b"sharp_content")
        resubmitted = resubmit_returned_invoice(
            submission=submission,
            user=self.emp1,
            new_files=[doc_v2],
            new_remarks="Clear sharp photo attached"
        )

        self.assertEqual(resubmitted.status, InvoiceSubmission.STATUS_RESUBMITTED)
        self.assertEqual(resubmitted.remarks, "Clear sharp photo attached")

        # Total documents = 2 (v1 and v2)
        all_docs = resubmitted.documents.all()
        self.assertEqual(all_docs.count(), 2)

        # v1 must be inactive, v2 must be active
        v1_doc = all_docs.get(version=1)
        v2_doc = all_docs.get(version=2)
        self.assertFalse(v1_doc.is_active_version)
        self.assertTrue(v2_doc.is_active_version)
        self.assertEqual(resubmitted.active_documents.count(), 1)
        self.assertEqual(resubmitted.active_documents.first(), v2_doc)

    def test_employee_account_deletion_protection(self):
        """Employees with submitted invoices cannot be deleted (PROTECT)."""
        doc = create_dummy_jpeg("sample.jpg", b"protect_test_content")
        create_invoice_submission(user=self.emp1, files=[doc], remarks="Test submission")

        with self.assertRaises(ProtectedError):
            self.emp1.delete()

    def test_employee_data_isolation(self):
        """Employee 2 must NOT be able to view Employee 1's submission detail or files."""
        doc = create_dummy_jpeg("secret_receipt.jpg", b"secret_emp1_data")
        sub1 = create_invoice_submission(user=self.emp1, files=[doc], remarks="Private to emp1")
        doc_record = sub1.documents.first()

        # Login as Employee 2
        self.client.login(username="emp_suresh", password="TestPassword@123")

        # Detail view attempt -> 403 Forbidden
        response = self.client.get(reverse('invoices:submission_detail', kwargs={'submission_id': sub1.submission_id}))
        self.assertEqual(response.status_code, 403)

        # Direct file download attempt -> 403 Forbidden
        response_doc = self.client.get(reverse('invoices:serve_document', kwargs={'doc_id': doc_record.id}))
        self.assertEqual(response_doc.status_code, 403)

        # Submissions list -> Employee 2 must not see Employee 1's submission
        list_response = self.client.get(reverse('invoices:my_submissions'))
        self.assertEqual(list_response.status_code, 200)
        self.assertNotContains(list_response, sub1.submission_id)

    def test_naveen_operator_access_and_verification(self):
        """Naveen can view any employee submission, download documents, and verify."""
        doc = create_dummy_jpeg("receipt_for_naveen.jpg", b"naveen_inspect_data")
        sub = create_invoice_submission(user=self.emp1, files=[doc], remarks="For Naveen review")
        doc_record = sub.documents.first()

        self.client.login(username="naveen_operator", password="TestPassword@123")

        # Detail view -> 200 OK
        resp = self.client.get(reverse('invoices:submission_detail', kwargs={'submission_id': sub.submission_id}))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, sub.submission_id)

        # Document serve -> 200 OK with proper headers
        resp_doc = self.client.get(reverse('invoices:serve_document', kwargs={'doc_id': doc_record.id}))
        self.assertEqual(resp_doc.status_code, 200)
        self.assertEqual(resp_doc['Content-Type'], 'image/jpeg')

        # Verify action
        verify_resp = self.client.post(reverse('invoices:naveen_verify', kwargs={'submission_id': sub.submission_id}))
        self.assertEqual(verify_resp.status_code, 302)

        sub.refresh_from_db()
        self.assertEqual(sub.status, InvoiceSubmission.STATUS_VERIFIED)

    def test_naveen_return_invoice_action(self):
        """Naveen can return an invoice with a required reason."""
        doc = create_dummy_jpeg("bad_pic.jpg", b"bad_pic_content")
        sub = create_invoice_submission(user=self.emp1, files=[doc], remarks="Check this")

        self.client.login(username="naveen_operator", password="TestPassword@123")

        return_resp = self.client.post(
            reverse('invoices:naveen_return', kwargs={'submission_id': sub.submission_id}),
            {'return_reason': 'Total amount is unreadable.'}
        )
        self.assertEqual(return_resp.status_code, 302)

        sub.refresh_from_db()
        self.assertEqual(sub.status, InvoiceSubmission.STATUS_RETURNED)
        self.assertEqual(sub.return_reason, 'Total amount is unreadable.')

    def test_reader_metadata_and_party_invoice_on_upload(self):
        """
        When employee uploads an invoice, party name and invoice number are initialized.
        If remarks mention a vendor, it populates party name.
        """
        doc = create_dummy_jpeg("fuel_hosur.jpg", b"fuel_hosur_unique_bytes")
        sub = create_invoice_submission(user=self.emp1, files=[doc], remarks="Indian Oil Corp Hosur bunk")

        sub.refresh_from_db()
        self.assertEqual(sub.party_name, "Indian Oil Corp Hosur bunk")
        # Ensure tracking IDs are NEVER substituted as invoice numbers
        self.assertEqual(sub.invoice_number, "")

    def test_naveen_dashboard_displays_party_and_invoice_columns(self):
        """Naveen dashboard table includes Party (Vendor) and Invoice No headers and data."""
        doc = create_dummy_jpeg("dash_bill.jpg", b"dash_bill_bytes")
        sub = create_invoice_submission(user=self.emp1, files=[doc], remarks="Sri Lakshmi Spares")

        self.client.login(username="naveen_operator", password="TestPassword@123")
        resp = self.client.get(reverse('core:naveen_dashboard') + '?tab=pending')

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Party (Vendor)")
        self.assertContains(resp, "Invoice No / ID")
        self.assertContains(resp, "Sri Lakshmi Spares")

    def test_gemini_vision_extractor_mock_success(self):
        """Test Gemini Vision AI extractor parses JSON response and normalizes statutory fields."""
        from unittest.mock import patch, MagicMock
        from apps.invoices.gemini_extractor import extract_with_gemini

        mock_json_response = (
            '{"party_name": "SRI RAGHAVENDRASWAMY ENTERPRISES", "party_gstin": "29ECEPK8760H1Z4", '
            '"invoice_number": "191", "invoice_date": "2026-11-06", "taxable_amount": 396890.00, '
            '"cgst_amount": 0.0, "sgst_amount": 0.0, "igst_amount": 0.0, "total_amount": 396890.00, '
            '"expense_ledger": "Purchase Spares & Consumables", "raw_summary": "Cash/Credit Bill 191"}'
        )

        dummy_path = "dummy_invoice.jpg"
        with open(dummy_path, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0dummy_jpg_content")

        try:
            with patch("django.conf.settings.GEMINI_API_KEY", "test_gemini_key_123"):
                with patch("google.genai.Client") as mock_client_cls:
                    mock_client = MagicMock()
                    mock_client_cls.return_value = mock_client
                    mock_resp = MagicMock()
                    mock_resp.text = mock_json_response
                    mock_client.models.generate_content.return_value = mock_resp

                    result = extract_with_gemini(dummy_path)
                    self.assertIsNotNone(result)
                    self.assertEqual(result["party_name"], "SRI RAGHAVENDRASWAMY ENTERPRISES")
                    self.assertEqual(result["party_gstin"], "29ECEPK8760H1Z4")
                    self.assertEqual(result["invoice_number"], "191")
                    self.assertEqual(str(result["total_amount"]), "396890.00")
                    self.assertEqual(result["expense_ledger"], "Purchase Spares & Consumables")
        finally:
            if os.path.exists(dummy_path):
                os.remove(dummy_path)

    def test_gemini_vision_extractor_fallback_on_missing_key(self):
        """Test that extractor returns None when GEMINI_API_KEY is not configured, triggering RapidOCR fallback."""
        from unittest.mock import patch
        from apps.invoices.gemini_extractor import extract_with_gemini

        with patch("django.conf.settings.GEMINI_API_KEY", ""):
            with patch("os.getenv", return_value=""):
                result = extract_with_gemini("any_dummy_file.jpg")
                self.assertIsNone(result)

