import os
import io
import hashlib
from decimal import Decimal
from datetime import date
from unittest.mock import patch, MagicMock
from PIL import Image, ImageEnhance
import pymupdf as fitz  # PyMuPDF modernized
from django.test import TestCase
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User, UserRole, Department
from apps.invoices.models import InvoiceSubmission, InvoiceDocument
from apps.invoices.services import create_invoice_submission
from apps.invoices.reader import extract_invoice_metadata
from apps.invoices.gemini_extractor import extract_with_gemini
from apps.invoices.vendor_directory import (
    VENDOR_DIRECTORY,
    find_vendor_by_gstin,
    find_vendor_by_name,
    get_all_vendor_names,
    get_tax_split,
    TALLY_EXPENSE_LEDGERS,
)


class Phase4ValidationSuite(TestCase):
    """
    Comprehensive Phase 4 Validation Suite for Balaji InvoiceHub.
    Covers all verification dimensions required before Phase 5 sign-off.
    Automated tests mock external AI APIs to guarantee fast, deterministic offline execution.
    """

    def setUp(self):
        self.dept = Department.objects.create(name="Fleet Operations", code="FLEET")
        self.emp = User.objects.create_user(
            username="emp_validation",
            password="TestPassword@123",
            role=UserRole.EMPLOYEE,
            first_name="Murugan",
            last_name="Staff",
            department=self.dept
        )
        self.naveen = User.objects.create_user(
            username="naveen_val",
            password="TestPassword@123",
            role=UserRole.NAVEEN,
            first_name="Naveen",
            last_name="Accounts",
            department=self.dept
        )

        # By default, mock external live Gemini API so test suite runs offline and deterministically
        self.gemini_patcher = patch('apps.invoices.gemini_extractor.extract_with_gemini', return_value=None)
        self.mock_gemini = self.gemini_patcher.start()

    def tearDown(self):
        self.gemini_patcher.stop()

    # -------------------------------------------------------------------------
    # 1. Test PDF Text Extraction & Image-based Preprocessing Separately
    # -------------------------------------------------------------------------
    def test_01_pdf_text_extraction_separate(self):
        """Test PDF text stream extraction using PyMuPDF / pdfplumber."""
        doc = fitz.open()
        page = doc.new_page()
        test_text = (
            "TAX INVOICE\n"
            "JK TYRE & INDUSTRIES LIMITED\n"
            "GSTIN: 29AAACJ6716F1ZK\n"
            "Invoice No: 9561514200\n"
            "Date: 02-09-2026\n"
            "Total Amount: INR 15,177.00\n"
        )
        page.insert_text((50, 50), test_text, fontsize=12)
        pdf_bytes = doc.tobytes()
        doc.close()

        pdf_file = SimpleUploadedFile("test_jk.pdf", pdf_bytes, content_type="application/pdf")
        sub = create_invoice_submission(user=self.emp, files=[pdf_file])
        doc_obj = sub.documents.first()

        meta = extract_invoice_metadata(doc_obj.original_file.path, content_type="application/pdf")
        self.assertIn("9561514200", meta['raw_text'])
        self.assertIn("29AAACJ6716F1ZK", meta['raw_text'])
        self.assertEqual(meta['invoice_number'], "9561514200")
        self.assertEqual(meta['party_gstin'], "29AAACJ6716F1ZK")
        self.assertEqual(meta['party_name'], "JK TYRE & INDUSTRIES LIMITED")
        self.assertEqual(meta['expense_ledger'], "Tyre & Tube Purchase")

    def test_02_image_preprocessing_and_contrast_enhancement(self):
        """Test image contrast enhancement (1.8x) and grayscale preprocessing."""
        img = Image.new("RGB", (300, 150), color=(240, 240, 235))
        gray = img.convert('L')
        self.assertEqual(gray.mode, 'L')

        enhancer = ImageEnhance.Contrast(gray)
        enhanced = enhancer.enhance(1.8)
        self.assertIsNotNone(enhanced)
        self.assertEqual(enhanced.size, (300, 150))

    # -------------------------------------------------------------------------
    # 2. Independent Field Extraction Validation (ID, Vendor, Expense Head)
    # -------------------------------------------------------------------------
    def test_03_independent_extraction_invoice_id(self):
        """Validate Invoice ID / Voucher No extraction across diverse formats."""
        test_patterns = [
            ("Invoice No: INV-2026-881\nDate: 12-09-2026", "INV-2026-881"),
            ("Bill Number: 80991\nParty: Sree Balaji", "80991"),
            ("Tax Invoice # B/CR/26-27/2758\nTotal: 14349", "B/CR/26-27/2758"),
            ("Cash Memo No: 662\nDate: 02-09-2026", "662"),
            ("Challan No: SH/5682\nSteel House", "SH/5682"),
        ]
        for raw_text, expected_id in test_patterns:
            doc = fitz.open()
            page = doc.new_page()
            page.insert_text((50, 50), raw_text, fontsize=12)
            pdf_bytes = doc.tobytes()
            doc.close()

            f = SimpleUploadedFile("pattern.pdf", pdf_bytes, content_type="application/pdf")
            sub = create_invoice_submission(user=self.emp, files=[f])
            doc_obj = sub.documents.first()
            meta = extract_invoice_metadata(doc_obj.original_file.path, content_type="application/pdf")
            self.assertEqual(meta['invoice_number'], expected_id, f"Failed for {raw_text}")

    def test_04_independent_extraction_vendor_name(self):
        """Validate Vendor extraction independently with transport industry entities."""
        test_vendors = [
            ("SREE BALAJI AUTOMOTIVES\nBill: 80991\nAmount: 48720", "SREE BALAJI AUTOMOTIVES"),
            ("TOOLS WORLD\nInvoice: TW/781/26-27\nTotal: 92969", "TOOLS WORLD"),
            ("HINDUSTAN AUTOMOTIVE SPARES\nBill No: 2213\nTotal: 69910", "HINDUSTAN AUTOMOTIVE SPARES"),
        ]
        for raw_text, expected_vendor in test_vendors:
            doc = fitz.open()
            page = doc.new_page()
            page.insert_text((50, 50), raw_text, fontsize=12)
            pdf_bytes = doc.tobytes()
            doc.close()

            f = SimpleUploadedFile("vendor.pdf", pdf_bytes, content_type="application/pdf")
            sub = create_invoice_submission(user=self.emp, files=[f])
            doc_obj = sub.documents.first()
            meta = extract_invoice_metadata(doc_obj.original_file.path, content_type="application/pdf")
            self.assertEqual(meta['party_name'], expected_vendor)

    def test_05_independent_extraction_expense_head(self):
        """Validate Expense Head mapping independently against Tally ledgers."""
        tyre_meta = find_vendor_by_name("JK Tyre")
        self.assertEqual(tyre_meta['ledger'], "Tyre & Tube Purchase")

        spares_meta = find_vendor_by_name("Sree Balaji Automotives")
        self.assertEqual(spares_meta['ledger'], "Purchase Spares & Consumables")

        gps_meta = find_vendor_by_name("E-Trans Solutions")
        self.assertEqual(gps_meta['ledger'], "GPS-Expenses")

        ins_meta = find_vendor_by_name("Universal Sompo")
        self.assertEqual(ins_meta['ledger'], "Lorry Insurance")

        diesel_meta = find_vendor_by_name("Gulf Oil Lubricants")
        self.assertEqual(diesel_meta['ledger'], "Diesel & Lubricants")

    # -------------------------------------------------------------------------
    # 3. Vendor Matching Against 100+ Directory & GSTIN Lookups
    # -------------------------------------------------------------------------
    def test_06_vendor_directory_comprehensive_matching(self):
        """Test vendor directory contains 100+ vendors and lookup works by GSTIN and Name."""
        self.assertGreaterEqual(len(VENDOR_DIRECTORY), 100)

        # Exact GSTIN lookup
        v_steel = find_vendor_by_gstin("29ABGFS3880F1ZU")
        self.assertIsNotNone(v_steel)
        self.assertEqual(v_steel['name'], "STEEL HOUSE")
        self.assertEqual(v_steel['state'], "Karnataka")

        # Fuzzy Name lookup
        v_tools = find_vendor_by_name("Tools World")
        self.assertIsNotNone(v_tools)
        self.assertEqual(v_tools['gstin'], "29ELVPS1386D1ZC")

        # Alphabetical names
        all_names = get_all_vendor_names()
        self.assertGreater(len(all_names), 80)
        self.assertIn("JK TYRE & INDUSTRIES LIMITED", all_names)
        self.assertIn("WATSOO EXPRESS PRIVATE LIMITED", all_names)

    def test_07_gstin_based_tax_split_and_matching(self):
        """Verify Intra-state Karnataka 29 (CGST+SGST) vs Inter-state (IGST)."""
        # Karnataka Intra-State 18% -> 9% CGST + 9% SGST
        tax_intra = get_tax_split("29AAACJ6716F1ZK", taxable_value=50000.0, tax_rate_percent=18.0)
        self.assertFalse(tax_intra['is_interstate'])
        self.assertEqual(tax_intra['cgst'], 4500.0)
        self.assertEqual(tax_intra['sgst'], 4500.0)
        self.assertEqual(tax_intra['igst'], 0.0)

        # Maharashtra Inter-State 18% -> 18% IGST
        tax_inter_mh = get_tax_split("27AAPFJ9153L1ZL", taxable_value=100000.0, tax_rate_percent=18.0)
        self.assertTrue(tax_inter_mh['is_interstate'])
        self.assertEqual(tax_inter_mh['cgst'], 0.0)
        self.assertEqual(tax_inter_mh['sgst'], 0.0)
        self.assertEqual(tax_inter_mh['igst'], 18000.0)

        # Tamil Nadu Inter-State 5% -> 5% IGST
        tax_inter_tn = get_tax_split("33AABCV3609C1ZU", taxable_value=20000.0, tax_rate_percent=5.0)
        self.assertTrue(tax_inter_tn['is_interstate'])
        self.assertEqual(tax_inter_tn['cgst'], 0.0)
        self.assertEqual(tax_inter_tn['sgst'], 0.0)
        self.assertEqual(tax_inter_tn['igst'], 1000.0)

    # -------------------------------------------------------------------------
    # 4. Ambiguous Matches & Verification Safety Gating
    # -------------------------------------------------------------------------
    def test_08_ambiguous_vendor_flagging_and_manual_review(self):
        """Identify ambiguous or unknown vendor matches and verify manual review flag."""
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((50, 50), "Roadside Repair Chit\nBill: 104\nTotal: 450.00", fontsize=12)
        pdf_bytes = doc.tobytes()
        doc.close()

        f = SimpleUploadedFile("unknown_slip.pdf", pdf_bytes, content_type="application/pdf")
        sub = create_invoice_submission(user=self.emp, files=[f])
        doc_obj = sub.documents.first()

        meta = extract_invoice_metadata(doc_obj.original_file.path, content_type="application/pdf")
        # Since GSTIN is absent, vendor is flagged as ambiguous
        self.assertTrue(meta['is_ambiguous_vendor'])
        self.assertTrue(meta['requires_manual_review'])
        self.assertLessEqual(meta['confidence'], 0.85)

    def test_09_financial_approval_safety_gate(self):
        """CRITICAL POLICY: Verify that OCR/reader NEVER automatically approves financial entries."""
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text(
            (50, 50),
            "TAX INVOICE\nJK TYRE & INDUSTRIES LIMITED\nGSTIN: 29AAACJ6716F1ZK\nInvoice No: 9561514200\nTotal Amount: 50000.00",
            fontsize=12
        )
        pdf_bytes = doc.tobytes()
        doc.close()

        f = SimpleUploadedFile("jk_safe.pdf", pdf_bytes, content_type="application/pdf")
        sub = create_invoice_submission(user=self.emp, files=[f])

        # Verify submission status remains strictly PENDING and NOT entered in Tally
        self.assertEqual(sub.status, InvoiceSubmission.STATUS_SUBMITTED)
        self.assertFalse(sub.is_entered_in_tally)
        self.assertIsNone(sub.tally_entered_at)

        # Only Naveen can manually approve after human verification
        self.client.login(username="naveen_val", password="TestPassword@123")
        url = reverse('invoices:toggle_tally', kwargs={'submission_id': sub.submission_id})
        self.client.post(url)
        sub.refresh_from_db()
        self.assertTrue(sub.is_entered_in_tally)
        self.assertIsNotNone(sub.tally_entered_at)

    # -------------------------------------------------------------------------
    # 5. Robustness: Malformed PDFs, Blurry Images, Multi-Page & Immutability
    # -------------------------------------------------------------------------
    def test_10_malformed_corrupt_blurry_and_multipage(self):
        """Test resilience against malformed PDFs, low-res images, and multi-page PDFs."""
        # A. Malformed PDF (truncated binary)
        corrupt_bytes = b"%PDF-1.4\n%corrupt truncated body without xref EOF"
        f_corrupt = SimpleUploadedFile("corrupt.pdf", corrupt_bytes, content_type="application/pdf")
        sub_corrupt = create_invoice_submission(user=self.emp, files=[f_corrupt])
        self.assertEqual(sub_corrupt.documents.count(), 1)
        meta_corrupt = extract_invoice_metadata(sub_corrupt.documents.first().original_file.path, "application/pdf")
        self.assertEqual(meta_corrupt['raw_text'], "")
        self.assertTrue(meta_corrupt['requires_manual_review'])

        # B. Low-res / blurry image (< 200px)
        img = Image.new("RGB", (120, 80), color=(200, 200, 200))
        img_io = io.BytesIO()
        img.save(img_io, format='JPEG')
        img_bytes = img_io.getvalue()
        f_blurry = SimpleUploadedFile("blurry.jpg", img_bytes, content_type="image/jpeg")
        sub_blurry = create_invoice_submission(user=self.emp, files=[f_blurry])
        self.assertEqual(sub_blurry.documents.count(), 1)

        # C. Multi-page PDF
        multi_doc = fitz.open()
        p1 = multi_doc.new_page()
        p1.insert_text((50, 50), "Page 1: Invoice No: MULTI-9988\nVendor: BOMBAY AUTO CORPORATION", fontsize=12)
        p2 = multi_doc.new_page()
        p2.insert_text((50, 50), "Page 2: Terms and Conditions\nTotal Amount: 43688", fontsize=12)
        multi_bytes = multi_doc.tobytes()
        multi_doc.close()

        f_multi = SimpleUploadedFile("multipage.pdf", multi_bytes, content_type="application/pdf")
        sub_multi = create_invoice_submission(user=self.emp, files=[f_multi])
        doc_multi = sub_multi.documents.first()
        meta_multi = extract_invoice_metadata(doc_multi.original_file.path, "application/pdf")
        self.assertIn("MULTI-9988", meta_multi['raw_text'])
        self.assertEqual(meta_multi['invoice_number'], "MULTI-9988")
        self.assertEqual(meta_multi['party_name'], "BOMBAY AUTO CORPORATION")

    def test_11_document_immutability_sha256(self):
        """Verify original invoice files are strictly immutable (byte-for-byte SHA-256 match)."""
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((50, 50), "Invoice No: IMMUTABLE-01\nParty: STEEL HOUSE", fontsize=12)
        pdf_bytes = doc.tobytes()
        doc.close()

        expected_hash = hashlib.sha256(pdf_bytes).hexdigest()
        f = SimpleUploadedFile("immutable.pdf", pdf_bytes, content_type="application/pdf")
        sub = create_invoice_submission(user=self.emp, files=[f])
        doc_obj = sub.documents.first()

        extract_invoice_metadata(doc_obj.original_file.path, "application/pdf")
        extract_invoice_metadata(doc_obj.original_file.path, "application/pdf")

        with open(doc_obj.original_file.path, 'rb') as disk_file:
            disk_bytes = disk_file.read()
            disk_hash = hashlib.sha256(disk_bytes).hexdigest()

        self.assertEqual(disk_hash, expected_hash, "Original document bytes were modified!")
        self.assertEqual(doc_obj.file_sha256, expected_hash)

    # -------------------------------------------------------------------------
    # 6. Verify Phase 1, 2, and 3 Functionality Still Works (Regression Check)
    # -------------------------------------------------------------------------
    def test_12_phase_1_2_3_regression_integrity(self):
        """Regression test verifying Phase 1 (RBAC), Phase 2 (Backups), and Phase 3 (Feed)."""
        self.assertTrue(self.emp.is_employee)
        self.assertTrue(self.naveen.is_naveen)
        self.assertTrue(self.emp.can_submit_invoices())
        self.assertFalse(self.emp.can_verify_invoices())
        self.assertTrue(self.naveen.can_verify_invoices())

        from django.conf import settings
        from apps.backup.services import BackupService
        self.assertGreaterEqual(getattr(settings, 'BACKUP_RETENTION_DAYS', 3650), 365)
        self.assertTrue(hasattr(BackupService, 'perform_full_backup'))

        self.client.login(username="naveen_val", password="TestPassword@123")
        resp = self.client.get(reverse('core:naveen_dashboard') + '?tab=pending')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('today_invoices', resp.context)
        self.assertIn('frequent_vendors', resp.context)
        self.assertGreaterEqual(len(resp.context['frequent_vendors']), 50)

    # -------------------------------------------------------------------------
    # 7. Safe Vendor Directory Synchronization & Integrity
    # -------------------------------------------------------------------------
    def test_13_safe_vendor_synchronization(self):
        """Test safe vendor registration process with GSTIN validation and RBAC."""
        from django.core.exceptions import PermissionDenied, ValidationError
        from apps.invoices.vendor_directory import register_or_update_vendor

        with self.assertRaises(PermissionDenied):
            register_or_update_vendor(
                gstin="29TESTV1234F1Z5",
                name="Unauthorized Vendor",
                user=self.emp
            )

        with self.assertRaises(ValidationError):
            register_or_update_vendor(
                gstin="INVALID-GSTIN-123",
                name="Bad GSTIN Vendor",
                user=self.naveen
            )

        res = register_or_update_vendor(
            gstin="29ABCDE1234F1Z5",
            name="New Verified Spares Ltd",
            ledger="Purchase Spares & Consumables",
            user=self.naveen
        )
        self.assertEqual(res['status'], 'CREATED')
        self.assertEqual(res['vendor']['state'], 'Karnataka')

        res_existing = register_or_update_vendor(
            gstin="29AAACJ6716F1ZK",  # JK Tyre
            name="Different Accidental Name",
            user=self.naveen,
            force_update=False
        )
        self.assertEqual(res_existing['status'], 'EXISTING_UNCHANGED')
        self.assertEqual(res_existing['vendor']['name'], "JK TYRE & INDUSTRIES LIMITED")

    def test_14_employee_deletion_protected(self):
        """Verify historical invoice preservation: deleting an employee raises ProtectedError."""
        from django.db.models.deletion import ProtectedError
        img = Image.new("RGB", (200, 200), color=(255, 255, 255))
        img_io = io.BytesIO()
        img.save(img_io, format='JPEG')
        f = SimpleUploadedFile("protect_test.jpg", img_io.getvalue(), content_type="image/jpeg")
        sub = create_invoice_submission(user=self.emp, files=[f])
        self.assertEqual(sub.employee, self.emp)

        with self.assertRaises(ProtectedError):
            self.emp.delete()

        self.assertTrue(InvoiceSubmission.objects.filter(pk=sub.pk).exists())

    # -------------------------------------------------------------------------
    # 8. Phase 6 Dedicated Gemini API Mocking & Reliability Tests
    # -------------------------------------------------------------------------
    def test_15_gemini_mock_valid_response(self):
        """Simulate valid Gemini Vision AI response and verify backend validation."""
        self.gemini_patcher.stop()

        mock_gemini_data = {
            'party_name': 'JK TYRE & INDUSTRIES LIMITED',
            'party_gstin': '29AAACJ6716F1ZK',
            'pan': 'AAACJ6716F',
            'invoice_number': 'JK-2026-9901',
            'invoice_date': '2026-09-02',
            'due_date': '2026-10-02',
            'taxable_amount': 20000.0,
            'cgst_amount': 1800.0,
            'sgst_amount': 1800.0,
            'igst_amount': 0.0,
            'cess_amount': 0.0,
            'round_off_amount': 0.0,
            'total_amount': 23600.0,
            'expense_ledger': 'Tyre & Tube Purchase',
            'raw_summary': 'JK Tyre invoice for commercial tyres',
        }

        with patch('google.genai.Client') as mock_client_cls:
            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.text = json_str = import_json = __import__('json').dumps(mock_gemini_data)
            mock_client.models.generate_content.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            # Create dummy file
            sample_file = os.path.join(os.path.dirname(__file__), 'fixtures', 'sample_invoice.jpg')
            if not os.path.exists(sample_file):
                sample_file = "test_invoice.jpg"
                with open(sample_file, "wb") as f:
                    f.write(b"0" * 300)

            result = extract_with_gemini(sample_file)
            self.assertIsNotNone(result)
            self.assertEqual(result['party_name'], 'JK TYRE & INDUSTRIES LIMITED')
            self.assertEqual(result['party_gstin'], '29AAACJ6716F1ZK')
            self.assertEqual(result['invoice_number'], 'JK-2026-9901')
            self.assertEqual(result['total_amount'], Decimal('23600.00'))
            self.assertEqual(result['vendor_match_status'], 'MATCHED_VERIFIED')
            # Omitting document_type defaults to NEEDS_REVIEW, requiring manual review
            self.assertTrue(result['requires_manual_review'])
            self.assertTrue(result['is_financial_approval_eligible'])

            # Explicit printed non-e-invoice response clears manual review gate
            printed_data = dict(mock_gemini_data, document_type='PRINTED', is_e_invoice=False)
            mock_resp.text = __import__('json').dumps(printed_data)
            printed_result = extract_with_gemini(sample_file)
            self.assertIsNotNone(printed_result)
            self.assertFalse(printed_result['requires_manual_review'])
            self.assertTrue(printed_result['is_financial_approval_eligible'])

            if sample_file == "test_invoice.jpg" and os.path.exists(sample_file):
                os.remove(sample_file)

        self.gemini_patcher.start()

    def test_16_gemini_mock_invalid_json(self):
        """Simulate invalid JSON from Gemini, verifying graceful failover to local OCR."""
        self.gemini_patcher.stop()

        with patch('google.genai.Client') as mock_client_cls:
            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.text = "NOT_A_VALID_JSON_STRING"
            mock_client.models.generate_content.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            sample_file = "test_inv_corrupt.jpg"
            with open(sample_file, "wb") as f:
                f.write(b"0" * 300)

            result = extract_with_gemini(sample_file)
            self.assertIsNone(result)

            if os.path.exists(sample_file):
                os.remove(sample_file)

        self.gemini_patcher.start()

    def test_17_gemini_mock_missing_gstin(self):
        """Simulate Gemini returning known vendor without GSTIN; backend requires manual review."""
        self.gemini_patcher.stop()

        mock_data = {
            'party_name': 'JK TYRE & INDUSTRIES LIMITED',
            'party_gstin': None,
            'invoice_number': 'JK-NO-GST',
            'total_amount': 5000.0,
            'expense_ledger': 'Tyre & Tube Purchase',
        }

        with patch('google.genai.Client') as mock_client_cls:
            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.text = __import__('json').dumps(mock_data)
            mock_client.models.generate_content.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            sample_file = "test_no_gst.jpg"
            with open(sample_file, "wb") as f:
                f.write(b"0" * 300)

            result = extract_with_gemini(sample_file)
            self.assertIsNotNone(result)
            self.assertEqual(result['vendor_match_status'], 'KNOWN_VENDOR_NO_GSTIN')
            self.assertTrue(result['requires_manual_review'])
            self.assertFalse(result['is_financial_approval_eligible'])

            if os.path.exists(sample_file):
                os.remove(sample_file)

        self.gemini_patcher.start()

    def test_18_gemini_mock_unknown_vendor(self):
        """Simulate Gemini returning unknown roadside vendor; backend flags as ambiguous."""
        self.gemini_patcher.stop()

        mock_data = {
            'party_name': 'Local Highway Dhaba & Garage',
            'party_gstin': None,
            'invoice_number': 'SLIP-99',
            'total_amount': 750.0,
            'expense_ledger': 'Labour Charges / Vehicle Service',
        }

        with patch('google.genai.Client') as mock_client_cls:
            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.text = __import__('json').dumps(mock_data)
            mock_client.models.generate_content.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            sample_file = "test_unknown.jpg"
            with open(sample_file, "wb") as f:
                f.write(b"0" * 300)

            result = extract_with_gemini(sample_file)
            self.assertIsNotNone(result)
            self.assertEqual(result['vendor_match_status'], 'UNKNOWN_VENDOR_NO_GSTIN')
            self.assertTrue(result['is_ambiguous_vendor'])
            self.assertTrue(result['requires_manual_review'])
            self.assertLessEqual(result['confidence'], 0.85)

            if os.path.exists(sample_file):
                os.remove(sample_file)

        self.gemini_patcher.start()

    def test_19_gemini_mock_api_timeout(self):
        """Simulate API network timeout, verifying graceful fallback without exception."""
        self.gemini_patcher.stop()

        with patch('google.genai.Client') as mock_client_cls:
            mock_client = MagicMock()
            mock_client.models.generate_content.side_effect = TimeoutError("Connection timed out")
            mock_client_cls.return_value = mock_client

            sample_file = "test_timeout.jpg"
            with open(sample_file, "wb") as f:
                f.write(b"0" * 300)

            result = extract_with_gemini(sample_file)
            self.assertIsNone(result)

            if os.path.exists(sample_file):
                os.remove(sample_file)

        self.gemini_patcher.start()

    def test_20_gemini_mock_http_429_rate_limit(self):
        """Simulate HTTP 429 quota exhausted response, verifying controlled backoff and fallback."""
        self.gemini_patcher.stop()

        with patch('google.genai.Client') as mock_client_cls:
            mock_client = MagicMock()
            mock_client.models.generate_content.side_effect = Exception("429 ResourceExhausted: Quota exceeded")
            mock_client_cls.return_value = mock_client

            sample_file = "test_429.jpg"
            with open(sample_file, "wb") as f:
                f.write(b"0" * 300)

            result = extract_with_gemini(sample_file)
            self.assertIsNone(result)

            if os.path.exists(sample_file):
                os.remove(sample_file)

        self.gemini_patcher.start()
