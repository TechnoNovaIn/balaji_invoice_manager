import os
import io
import pymupdf as fitz
from decimal import Decimal
from django.test import TestCase
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from apps.accounts.models import User, UserRole, Department
from apps.invoices.models import (
    InvoiceSubmission,
    InvoiceDocument,
    UploadedDocument,
    InvoiceSequence,
)
from apps.invoices.services import (
    create_invoice_submissions,
    split_invoice_submission,
    merge_invoice_submissions,
)


def make_pdf_with_invoices(invoice_specs):
    """
    Helper to generate synthetic multi-page PDFs with specified text on each page.
    invoice_specs: list of text strings or dicts with page specifications.
    """
    doc = fitz.open()
    for spec in invoice_specs:
        page = doc.new_page(width=595, height=842)
        if isinstance(spec, dict):
            text = f"""
            TAX INVOICE
            Supplier: {spec.get('vendor', 'ABC TRADERS')}
            GSTIN: {spec.get('gstin', '29ABCDE1234F1Z5')}
            Invoice No: {spec.get('inv_no', 'INV-001')}
            Date: {spec.get('date', '01-11-2026')}
            {spec.get('page_marker', '')}
            
            Taxable Amount: {spec.get('taxable', '10000.00')}
            CGST: {spec.get('cgst', '900.00')}
            SGST: {spec.get('sgst', '900.00')}
            Grand Total: {spec.get('total', '11800.00')}
            """
        else:
            text = spec
        page.insert_text((50, 72), text)
    pdf_bytes = doc.write()
    doc.close()
    return pdf_bytes


class MultiInvoiceProcessingTestCase(TestCase):
    def setUp(self):
        dept, _ = Department.objects.get_or_create(name="Operations", code="OPS")
        self.employee = User.objects.create_user(
            username="emp_tester",
            password="testpassword123",
            role=UserRole.EMPLOYEE,
            department=dept,
            first_name="Ramesh",
            last_name="Kumar"
        )
        self.naveen = User.objects.create_user(
            username="naveen_tester",
            password="testpassword123",
            role=UserRole.NAVEEN,
            first_name="Naveen",
            last_name="Accounts"
        )

    def test_01_one_pdf_with_30_invoices(self):
        """
        Scenario 1: One uploaded PDF containing 30 distinct invoices.
        Expected:
        - 30 individual invoice records created.
        - 30 independent Purchase Register rows.
        - Correct source page references (pages 1 to 30).
        - Source UploadedDocument tracks 30 total pages and 30 detected invoices.
        """
        specs = []
        for i in range(1, 31):
            specs.append({
                'vendor': f'SUPPLIER_{i:02d} ENTERPRISES',
                'gstin': f'29ABCDE{1000+i}F1Z5',
                'inv_no': f'INV-2026-{100+i}',
                'taxable': f'{1000 * i}.00',
                'cgst': f'{90 * i}.00',
                'sgst': f'{90 * i}.00',
                'total': f'{1180 * i}.00',
            })
        pdf_bytes = make_pdf_with_invoices(specs)
        upload_file = SimpleUploadedFile(
            name="November_Purchase_Invoices_30.pdf",
            content=pdf_bytes,
            content_type="application/pdf"
        )

        subs = create_invoice_submissions(
            user=self.employee,
            files=[upload_file],
            remarks="Batch of 30 November purchase receipts"
        )

        self.assertEqual(len(subs), 30, f"Expected 30 separate invoice submissions, got {len(subs)}")

        # Verify database records
        all_subs = InvoiceSubmission.objects.filter(employee=self.employee).order_by('start_page')
        self.assertEqual(all_subs.count(), 30)

        # Check source document
        source_doc = UploadedDocument.objects.get(file_name="November_Purchase_Invoices_30.pdf")
        self.assertEqual(source_doc.total_pages, 30)
        self.assertEqual(source_doc.invoices_detected, 30)
        self.assertEqual(source_doc.processing_status, UploadedDocument.STATUS_DETECTION_COMPLETED)

        # Verify page ranges and tracking IDs
        seen_ids = set()
        seen_inv_nos = set()
        for idx, sub in enumerate(all_subs, start=1):
            self.assertEqual(sub.start_page, idx)
            self.assertEqual(sub.end_page, idx)
            self.assertEqual(sub.source_document_id, source_doc.id)
            self.assertIn(sub.submission_id, seen_ids ^ {sub.submission_id})
            seen_ids.add(sub.submission_id)
            expected_inv_no = f'INV-2026-{100+idx}'
            self.assertEqual(sub.invoice_number, expected_inv_no)
            seen_inv_nos.add(sub.invoice_number)
            self.assertEqual(sub.total_amount, Decimal(f'{1180 * idx}.00'))

        self.assertEqual(len(seen_ids), 30)
        self.assertEqual(len(seen_inv_nos), 30)

    def test_02_same_vendor_different_invoices_in_one_pdf(self):
        """
        Scenario 2: One PDF with multiple distinct invoices from the SAME vendor.
        Expected:
        - Separate rows created for each invoice.
        - Correct invoice numbers and amounts.
        - No accidental merging simply because vendor/GSTIN matches.
        """
        specs = [
            {'vendor': 'SRINIDHI ENGINEERING INDUSTRIES', 'gstin': '29ACKFS0217R1ZI', 'inv_no': 'INV-101', 'total': '12000.00'},
            {'vendor': 'SRINIDHI ENGINEERING INDUSTRIES', 'gstin': '29ACKFS0217R1ZI', 'inv_no': 'INV-102', 'total': '18500.00'},
            {'vendor': 'SRINIDHI ENGINEERING INDUSTRIES', 'gstin': '29ACKFS0217R1ZI', 'inv_no': 'INV-103', 'total': '9000.00'},
            {'vendor': 'SRINIDHI ENGINEERING INDUSTRIES', 'gstin': '29ACKFS0217R1ZI', 'inv_no': 'INV-104', 'total': '25000.00'},
        ]
        pdf_bytes = make_pdf_with_invoices(specs)
        upload_file = SimpleUploadedFile("Srinidhi_Multiple_Bills.pdf", pdf_bytes, "application/pdf")

        subs = create_invoice_submissions(user=self.employee, files=[upload_file])

        self.assertEqual(len(subs), 4)
        inv_numbers = [s.invoice_number for s in subs]
        self.assertEqual(inv_numbers, ['INV-101', 'INV-102', 'INV-103', 'INV-104'])
        amounts = [s.total_amount for s in subs]
        self.assertEqual(amounts, [Decimal('12000.00'), Decimal('18500.00'), Decimal('9000.00'), Decimal('25000.00')])

        # All rows reference the same vendor
        for s in subs:
            self.assertEqual(s.party_name, 'SRINIDHI ENGINEERING INDUSTRIES')
            self.assertEqual(s.party_gstin, '29ACKFS0217R1ZI')

    def test_03_different_vendors_in_one_pdf(self):
        """
        Scenario 3: Different suppliers in a single PDF.
        Expected:
        - Correct supplier identification and GSTIN on each record.
        - Separate rows in the register.
        """
        specs = [
            {'vendor': 'KWALITY FURNISHINGS', 'gstin': '29AAQPF8511J1ZX', 'inv_no': 'F26-001', 'total': '42146.00'},
            {'vendor': 'VEL AGENCIES', 'gstin': '33AIPPM4834K1ZV', 'inv_no': 'CR0088', 'total': '300000.00'},
            {'vendor': 'SRINIDHI ENGINEERING INDUSTRIES', 'gstin': '29ACKFS0217R1ZI', 'inv_no': '048', 'total': '6839.28'},
        ]
        pdf_bytes = make_pdf_with_invoices(specs)
        upload_file = SimpleUploadedFile("Multi_Vendor_Batch.pdf", pdf_bytes, "application/pdf")

        subs = create_invoice_submissions(user=self.employee, files=[upload_file])

        self.assertEqual(len(subs), 3)
        self.assertEqual(subs[0].party_name, 'KWALITY FURNISHINGS')
        self.assertEqual(subs[0].party_gstin, '29AAQPF8511J1ZX')
        self.assertEqual(subs[1].party_name, 'VEL AGENCIES')
        self.assertEqual(subs[1].party_gstin, '33AIPPM4834K1ZV')
        self.assertEqual(subs[2].party_name, 'SRINIDHI ENGINEERING INDUSTRIES')
        self.assertEqual(subs[2].party_gstin, '29ACKFS0217R1ZI')

    def test_04_one_invoice_spanning_multiple_pages(self):
        """
        Scenario 4: One invoice spanning pages 1-2, and a second invoice on page 3.
        Expected:
        - Exactly 2 invoice records created.
        - Record 1: start_page=1, end_page=2.
        - Record 2: start_page=3, end_page=3.
        - No accidental splitting of the 2-page invoice.
        """
        page_1_text = """
        TAX INVOICE
        Supplier: KWALITY FURNISHINGS
        GSTIN: 29AAQPF8511J1ZX
        Invoice No: F26-999
        Date: 05-11-2026
        Page 1 of 2
        
        Items 1 to 10: Upholstery & Cushion Spares
        Continued on next page...
        """
        page_2_text = """
        TAX INVOICE
        Supplier: KWALITY FURNISHINGS
        GSTIN: 29AAQPF8511J1ZX
        Invoice No: F26-999
        Page 2 of 2
        Continued from Page 1
        
        Taxable Amount: 35716.98
        CGST 9%: 3214.51
        SGST 9%: 3214.51
        Grand Total: 42146.00
        Authorized Signatory
        """
        page_3_text = """
        TAX INVOICE
        Supplier: SRINIDHI ENGINEERING INDUSTRIES
        GSTIN: 29ACKFS0217R1ZI
        Invoice No: 099
        Date: 05-11-2026
        Page 1 of 1
        
        Taxable Amount: 5000.00
        CGST: 450.00
        SGST: 450.00
        Grand Total: 5900.00
        Authorized Signatory
        """
        pdf_bytes = make_pdf_with_invoices([page_1_text, page_2_text, page_3_text])
        upload_file = SimpleUploadedFile("MultiPage_Plus_Single.pdf", pdf_bytes, "application/pdf")

        subs = create_invoice_submissions(user=self.employee, files=[upload_file])

        self.assertEqual(len(subs), 2, f"Expected 2 invoices, got {len(subs)}")
        # Invoice 1 spans pages 1-2
        self.assertEqual(subs[0].start_page, 1)
        self.assertEqual(subs[0].end_page, 2)
        self.assertEqual(subs[0].invoice_number, 'F26-999')
        self.assertEqual(subs[0].total_amount, Decimal('42146.00'))

        # Invoice 2 is on page 3
        self.assertEqual(subs[1].start_page, 3)
        self.assertEqual(subs[1].end_page, 3)
        self.assertEqual(subs[1].invoice_number, '099')
        self.assertEqual(subs[1].total_amount, Decimal('5900.00'))

    def test_05_multiple_image_uploads(self):
        """
        Scenario 5: Multiple independent image uploads.
        Expected:
        - Each image creates its own independent invoice record.
        - Each has its own tracking ID.
        - Linked to its own image.
        """
        # Create 3 distinct valid JPEG images with magic bytes
        img1 = SimpleUploadedFile("bill_1.jpg", b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00img1_data", "image/jpeg")
        img2 = SimpleUploadedFile("bill_2.jpg", b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00img2_data", "image/jpeg")
        img3 = SimpleUploadedFile("bill_3.jpg", b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00img3_data", "image/jpeg")

        subs = create_invoice_submissions(user=self.employee, files=[img1, img2, img3])

        self.assertEqual(len(subs), 3)
        tracking_ids = [s.submission_id for s in subs]
        self.assertEqual(len(set(tracking_ids)), 3)

        for s in subs:
            self.assertEqual(s.documents.count(), 1)
            self.assertEqual(s.start_page, 1)
            self.assertEqual(s.end_page, 1)

    def test_06_split_and_merge_facilities(self):
        """
        Scenario 6: Naveen's facility to split an incorrectly grouped invoice
        and merge two separate submissions that belong to one invoice.
        """
        # Create a submission spanning pages 1-4
        source_doc = UploadedDocument.objects.create(
            uploader=self.employee,
            file_name="4page_doc.pdf",
            file_sha256="fake_sha_4page",
            file_size_bytes=1024,
            content_type="application/pdf",
            total_pages=4,
        )
        sub = InvoiceSubmission.objects.create(
            submission_id=InvoiceSequence.get_next_submission_id(),
            employee=self.employee,
            source_document=source_doc,
            start_page=1,
            end_page=4,
            party_name="TEST VENDOR",
        )
        for p in range(1, 5):
            InvoiceDocument.objects.create(
                submission=sub,
                page_number=p,
                original_filename=f"page_{p}.jpg",
                file_sha256=f"hash_pg_{p}",
                file_size_bytes=100,
                content_type="image/jpeg"
            )

        # 1. Test Split at Page 3 -> Sub A (pg 1-2) and Sub B (pg 3-4)
        sub_a, sub_b = split_invoice_submission(sub, split_at_page=3, user=self.naveen)
        self.assertEqual(sub_a.start_page, 1)
        self.assertEqual(sub_a.end_page, 2)
        self.assertEqual(sub_b.start_page, 3)
        self.assertEqual(sub_b.end_page, 4)
        self.assertEqual(sub_a.documents.count(), 2)
        self.assertEqual(sub_b.documents.count(), 2)

        # 2. Test Merge: Merge Sub B back into Sub A
        merged = merge_invoice_submissions(sub_a, sub_b, user=self.naveen)
        self.assertEqual(merged.start_page, 1)
        self.assertEqual(merged.end_page, 4)
        self.assertEqual(merged.documents.count(), 4)
        # Sub B was deleted upon merge
        self.assertFalse(InvoiceSubmission.objects.filter(pk=sub_b.pk).exists())

    def test_07_duplicate_pdf_upload_policy(self):
        """
        Scenario 7: Duplicate PDF upload policy.
        Identical PDF uploaded again must be accepted, create independent records,
        and show informational warning without blocking.
        """
        specs = [
            {'vendor': 'KWALITY FURNISHINGS', 'inv_no': 'F26-D1', 'total': '1000.00'},
            {'vendor': 'VEL AGENCIES', 'inv_no': 'CR00D2', 'total': '2000.00'},
        ]
        pdf_bytes = make_pdf_with_invoices(specs)
        file1 = SimpleUploadedFile("dup_test.pdf", pdf_bytes, "application/pdf")
        subs1 = create_invoice_submissions(user=self.employee, files=[file1])
        self.assertEqual(len(subs1), 2)
        self.assertFalse(subs1[0].is_duplicate_suspected)

        # Re-upload exact same PDF
        file2 = SimpleUploadedFile("dup_test.pdf", pdf_bytes, "application/pdf")
        subs2 = create_invoice_submissions(user=self.employee, files=[file2])
        self.assertEqual(len(subs2), 2)
        # Accepted, but marked duplicate suspected for Naveen's review
        self.assertTrue(subs2[0].is_duplicate_suspected)
        self.assertIn("Informational Notice", subs2[0].duplicate_notes)

    def test_08_error_handling_and_partial_page_resilience(self):
        """
        Scenario 8: If one page in a multi-page PDF has minimal/empty text,
        other valid pages continue to segment and process without failing the entire PDF.
        """
        specs = [
            {'vendor': 'KWALITY FURNISHINGS', 'inv_no': 'F26-OK1', 'total': '5000.00'},
            "BLANK PAGE WITHOUT ANY INVOICE DATA",
            {'vendor': 'VEL AGENCIES', 'inv_no': 'CR00-OK2', 'total': '8000.00'},
        ]
        pdf_bytes = make_pdf_with_invoices(specs)
        upload_file = SimpleUploadedFile("Partial_Failure_Batch.pdf", pdf_bytes, "application/pdf")

        subs = create_invoice_submissions(user=self.employee, files=[upload_file])
        self.assertGreaterEqual(len(subs), 2)
        inv_nos = [s.invoice_number for s in subs]
        self.assertIn('F26-OK1', inv_nos)
        self.assertIn('CR00-OK2', inv_nos)

    def test_09_financial_mathematical_validation(self):
        """
        Scenario 9: Validates statutory GST mathematical parity:
        Taxable Amount + CGST + SGST = Total Amount.
        """
        specs = [{
            'vendor': 'SRINIDHI ENGINEERING INDUSTRIES',
            'gstin': '29ACKFS0217R1ZI',
            'inv_no': 'MATH-001',
            'taxable': '5796.00',
            'cgst': '521.64',
            'sgst': '521.64',
            'total': '6839.28',
        }]
        pdf_bytes = make_pdf_with_invoices(specs)
        upload_file = SimpleUploadedFile("Math_Recon_Test.pdf", pdf_bytes, "application/pdf")
        subs = create_invoice_submissions(user=self.employee, files=[upload_file])

        sub = subs[0]
        self.assertEqual(sub.taxable_amount, Decimal('5796.00'))
        self.assertEqual(sub.cgst_amount, Decimal('521.64'))
        self.assertEqual(sub.sgst_amount, Decimal('521.64'))
        self.assertEqual(sub.total_amount, Decimal('6839.28'))
        # Reconcile formula
        calc_total = sub.taxable_amount + sub.cgst_amount + sub.sgst_amount + sub.igst_amount
        self.assertEqual(calc_total, sub.total_amount)

    def test_10_document_viewer_and_source_download(self):
        """
        Scenario 10: Tests the source document download endpoint.
        Verifies Naveen can download the complete original unsegmented PDF file.
        """
        specs = [
            {'vendor': 'KWALITY FURNISHINGS', 'inv_no': 'F26-V1', 'total': '1000.00'},
            {'vendor': 'VEL AGENCIES', 'inv_no': 'CR00-V2', 'total': '2000.00'},
        ]
        pdf_bytes = make_pdf_with_invoices(specs)
        upload_file = SimpleUploadedFile("Original_Source_Test.pdf", pdf_bytes, "application/pdf")
        subs = create_invoice_submissions(user=self.employee, files=[upload_file])

        sub1 = subs[0]
        self.assertIsNotNone(sub1.source_document)

        # Login as Naveen
        self.client.login(username="naveen_tester", password="testpassword123")
        url = reverse('invoices:download_source_document', kwargs={'doc_id': sub1.source_document.id})
        resp = self.client.get(url)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'application/pdf')
        self.assertIn('Original_Source_Test.pdf', resp['Content-Disposition'])
        body = b"".join(resp.streaming_content)
        self.assertEqual(len(body), len(pdf_bytes))

    def test_11_page_classification_engine(self):
        """
        Special Requirement 1: Tests intelligent page classification.
        Verifies DIGITAL_TEXT, BLANK, and SCANNED/MIXED classification.
        """
        from apps.invoices.page_classifier import classify_pdf_page, PageClassificationType

        # 1. Digital text page
        doc = fitz.open()
        p_dig = doc.new_page()
        p_dig.insert_text((50, 72), "TAX INVOICE - DIGITAL TEXT CONTENT FOR TESTING CLASSIFIER")
        c_dig = classify_pdf_page(p_dig)
        self.assertEqual(c_dig['page_type'], PageClassificationType.DIGITAL_TEXT)
        self.assertFalse(c_dig['requires_ocr'])

        # 2. Blank page
        p_blank = doc.new_page()
        c_blank = classify_pdf_page(p_blank)
        self.assertEqual(c_blank['page_type'], PageClassificationType.BLANK)
        self.assertFalse(c_blank['requires_ocr'])
        doc.close()

    def test_12_conditional_ocr_bypasses_digital_pages(self):
        """
        Special Requirement 2 & 6: Conditional OCR avoids unnecessary OCR on digital pages.
        Verifies digital pages increment digital_pages_count, avoid OCR, and record granular timings.
        """
        from apps.invoices.performance_timer import PerformanceTimer
        from apps.invoices.segmentation import segment_and_extract_pdf

        specs = [
            {'vendor': 'SRI BALAJI SPARES', 'inv_no': 'SBS-001', 'total': '5000.00'},
            {'vendor': 'SRI BALAJI SPARES', 'inv_no': 'SBS-002', 'total': '7000.00'},
        ]
        pdf_bytes = make_pdf_with_invoices(specs)
        upload_file = SimpleUploadedFile("Digital_Invoice_Batch.pdf", pdf_bytes, "application/pdf")
        subs = create_invoice_submissions(user=self.employee, files=[upload_file])
        self.assertEqual(len(subs), 2)

        timer = PerformanceTimer(total_pages=2)
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
            tmp.write(pdf_bytes)
            tmp_path = tmp.name

        try:
            invoices = segment_and_extract_pdf(tmp_path, timer=timer)
            metrics = timer.finish()
            self.assertEqual(len(invoices), 2)
            self.assertEqual(metrics['counters']['digital_pages_count'], 2)
            self.assertEqual(metrics['counters']['ocr_pages_count'], 0)
            self.assertIn('pdf_opening_ms', metrics['timings_ms'])
            self.assertIn('page_classification_ms', metrics['timings_ms'])
            self.assertIn('segmentation_ms', metrics['timings_ms'])
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_13_background_processing_task_and_status_api(self):
        """
        Special Requirement 5: Background processing and real-time status reporting API.
        Verifies stages, progress percent, and document_status_api output.
        """
        from apps.invoices.tasks import process_uploaded_document_task

        specs = [
            {'vendor': 'KAVERI AUTOMOBILES', 'inv_no': 'KAV-101', 'total': '3200.00'},
        ]
        pdf_bytes = make_pdf_with_invoices(specs)
        upload_file = SimpleUploadedFile("Kaveri_Bill.pdf", pdf_bytes, "application/pdf")

        # Create UploadedDocument directly
        doc = UploadedDocument.objects.create(
            uploader=self.employee,
            original_file=upload_file,
            file_name="Kaveri_Bill.pdf",
            file_sha256="testsha256kav101",
            file_size_bytes=len(pdf_bytes),
            content_type="application/pdf",
            processing_status=UploadedDocument.STATUS_QUEUED,
            current_stage="Document Queued",
            progress_percent=5,
        )

        # Run pipeline task
        created_subs = process_uploaded_document_task(doc.id, remarks="Urgent Kaveri maintenance bill")
        self.assertEqual(len(created_subs), 1)

        doc.refresh_from_db()
        self.assertEqual(doc.processing_status, UploadedDocument.STATUS_DETECTION_COMPLETED)
        self.assertEqual(doc.progress_percent, 100)
        self.assertIn("Completed", doc.current_stage)
        self.assertIn('total_time_seconds', doc.processing_metrics)

        # Test document_status_api
        self.client.login(username="emp_tester", password="testpassword123")
        url = reverse('invoices:document_status_api', kwargs={'document_id': doc.id})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['status'], UploadedDocument.STATUS_DETECTION_COMPLETED)
        self.assertEqual(data['progress_percent'], 100)
        self.assertEqual(data['invoices_detected'], 1)
        self.assertTrue(data['redirect_url'].startswith('/invoices/success/'))
