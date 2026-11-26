import tempfile
import os
from decimal import Decimal
from unittest.mock import patch, MagicMock
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from apps.accounts.models import User, UserRole
from apps.invoices.models import InvoiceSubmission, UploadedDocument
from apps.invoices.gemini_extractor import (
    _reconcile_extracted_amounts,
    extract_with_gemini,
    InvoiceSchema,
)
from apps.invoices.reader import extract_metadata_from_text


class VehicleInvoiceAndTaxReconciliationTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.password = "StrongPass@2026"
        self.naveen = User.objects.create_user(
            username="naveen_user",
            password=self.password,
            role=UserRole.NAVEEN,
            is_active=True
        )
        self.employee = User.objects.create_user(
            username="emp_user",
            password=self.password,
            role=UserRole.EMPLOYEE,
            is_active=True
        )

    def test_tax_reconciliation_anti_inversion(self):
        """If extractor accidentally inverts taxable base and grand total, it must auto-swap."""
        data = {
            'taxable_amount': 11800.0,
            'total_amount': 10000.0,
            'cgst_amount': 900.0,
            'sgst_amount': 900.0,
            'igst_amount': 0.0,
        }
        reconciled, warnings = _reconcile_extracted_amounts(data, party_gstin="29AAWFB0395H1ZX")
        self.assertEqual(reconciled['taxable_amount'], Decimal('10000.00'))
        self.assertEqual(reconciled['total_amount'], Decimal('11800.00'))
        self.assertEqual(reconciled['cgst_amount'], Decimal('900.00'))
        self.assertEqual(reconciled['sgst_amount'], Decimal('900.00'))

    def test_tax_reconciliation_missing_taxable_with_positive_taxes(self):
        """
        When taxable_amount is 0.00 but total is 1180.00 and taxes are 180.00,
        taxable_amount must be calculated as 1000.00 (NOT 1180.00!).
        """
        data = {
            'taxable_amount': 0.0,
            'total_amount': 1180.0,
            'cgst_amount': 90.0,
            'sgst_amount': 90.0,
            'igst_amount': 0.0,
        }
        reconciled, warnings = _reconcile_extracted_amounts(data, party_gstin="29AAWFB0395H1ZX")
        self.assertEqual(reconciled['taxable_amount'], Decimal('1000.00'))
        self.assertEqual(reconciled['total_amount'], Decimal('1180.00'))

    def test_tax_reconciliation_karnataka_intrastate_alignment(self):
        """
        For a Karnataka supplier (29...), if IGST was extracted mistakenly,
        it must be converted to equal CGST + SGST.
        """
        data = {
            'taxable_amount': 5000.0,
            'total_amount': 5900.0,
            'cgst_amount': 0.0,
            'sgst_amount': 0.0,
            'igst_amount': 900.0,
        }
        reconciled, warnings = _reconcile_extracted_amounts(data, party_gstin="29ABCDE1234F1Z5")
        self.assertEqual(reconciled['igst_amount'], Decimal('0.00'))
        self.assertEqual(reconciled['cgst_amount'], Decimal('450.00'))
        self.assertEqual(reconciled['sgst_amount'], Decimal('450.00'))

    def test_tax_reconciliation_interstate_alignment(self):
        """
        For an inter-state supplier (e.g. Maharashtra 27...), if CGST/SGST was extracted,
        it must be converted to IGST.
        """
        data = {
            'taxable_amount': 5000.0,
            'total_amount': 5900.0,
            'cgst_amount': 450.0,
            'sgst_amount': 450.0,
            'igst_amount': 0.0,
        }
        reconciled, warnings = _reconcile_extracted_amounts(data, party_gstin="27ABCDE1234F1Z5")
        self.assertEqual(reconciled['cgst_amount'], Decimal('0.00'))
        self.assertEqual(reconciled['sgst_amount'], Decimal('0.00'))
        self.assertEqual(reconciled['igst_amount'], Decimal('900.00'))

    def test_rapidocr_vehicle_insurance_extraction(self):
        """RapidOCR reader extracts policy number, vehicle number, and sets Lorry Insurance ledger."""
        raw_text = """
        THE NEW INDIA ASSURANCE CO. LTD.
        POLICY SCHEDULE & CERTIFICATE OF INSURANCE
        Policy No: OG-25-1801-1802-00004567
        Insured: BALAJI TRANSPORTS
        Vehicle Reg No: KA-01-AB-1234
        Net Premium: 25000.00
        CGST 9%: 2250.00
        SGST 9%: 2250.00
        Total Amount: 29500.00
        """
        meta = extract_metadata_from_text(raw_text)
        self.assertEqual(meta['invoice_type'], 'VEHICLE_INSURANCE')
        self.assertEqual(meta['policy_number'], 'OG-25-1801-1802-00004567')
        self.assertEqual(meta['vehicle_number'], 'KA-01-AB-1234')
        self.assertEqual(meta['expense_ledger'], 'Lorry Insurance')
        self.assertEqual(meta['total_amount'], Decimal('29500.00'))

    def test_rapidocr_vehicle_service_extraction(self):
        """RapidOCR reader extracts vehicle number, labour charges, and sets Service ledger."""
        raw_text = """
        SRI MANJUNATHA AUTOMOBILES & GARAGE
        CASH / CREDIT BILL
        Vehicle No: KA-04-AK-5678
        Spares Description: Leaf Springs, Centre Bolt
        Spares Amount: 8500.00
        Labour Charges: 1500.00
        Washing Charges: 500.00
        Total Bill: 10500.00
        """
        meta = extract_metadata_from_text(raw_text)
        self.assertEqual(meta['invoice_type'], 'VEHICLE_SERVICE')
        self.assertEqual(meta['vehicle_number'], 'KA-04-AK-5678')
        self.assertEqual(meta['labour_charges'], Decimal('2000.00'))  # 1500 + 500
        self.assertEqual(meta['expense_ledger'], 'Labour Charges / Vehicle Service')

    @patch('apps.invoices.gemini_service.get_available_gemini_keys', return_value=['AQ.TestKey_123456789'])
    @patch('apps.invoices.gemini_extractor._get_gemini_client')
    def test_gemini_vehicle_insurance_extraction(self, mock_get_client, mock_keys):
        """Gemini extractor parses insurance schema with policy number and vehicle number."""
        mock_response = MagicMock()
        mock_response.parsed = InvoiceSchema(
            invoice_type="VEHICLE_INSURANCE",
            party_name="ICICI LOMBARD GENERAL INSURANCE",
            party_gstin="29AAACI7904G1Z8",
            policy_number="3001/2026/98765432",
            vehicle_number="KA-01-MJ-9999",
            taxable_amount=30000.0,
            cgst_amount=2700.0,
            sgst_amount=2700.0,
            total_amount=35400.0,
        )
        client = MagicMock()
        client.models.generate_content.return_value = mock_response
        mock_get_client.return_value = client

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
            tf.write(b"X" * 600)
            tf_path = tf.name

        try:
            res = extract_with_gemini(tf_path)
            self.assertIsNotNone(res)
            self.assertEqual(res['invoice_type'], 'VEHICLE_INSURANCE')
            self.assertEqual(res['policy_number'], '3001/2026/98765432')
            self.assertEqual(res['vehicle_number'], 'KA-01-MJ-9999')
            self.assertEqual(res['expense_ledger'], 'Lorry Insurance')
            self.assertEqual(res['total_amount'], Decimal('35400.00'))
        finally:
            if os.path.exists(tf_path):
                os.remove(tf_path)

    def test_model_property_display_document_identifier(self):
        """Verify model properties for vehicle insurance and service."""
        sub_ins = InvoiceSubmission(
            submission_id="BTI-202611-00091",
            employee=self.employee,
            invoice_type=InvoiceSubmission.INVOICE_TYPE_VEHICLE_INSURANCE,
            policy_number="POL-2026-999",
            invoice_number="INV-100",
            vehicle_number="KA-01-AB-1234",
            total_amount=Decimal('15000.00')
        )
        self.assertEqual(sub_ins.display_document_identifier, "POL-2026-999")
        self.assertTrue(sub_ins.is_vehicle_insurance)

        sub_reg = InvoiceSubmission(
            submission_id="BTI-202611-00092",
            employee=self.employee,
            invoice_type=InvoiceSubmission.INVOICE_TYPE_REGULAR,
            invoice_number="INV-200",
            total_amount=Decimal('5000.00')
        )
        self.assertEqual(sub_reg.display_document_identifier, "INV-200")
        self.assertFalse(sub_reg.is_vehicle_insurance)

    def test_verification_workspace_persists_vehicle_and_insurance_fields(self):
        """Operator can update vehicle number, policy number, labour charges in verification workspace."""
        uploaded_doc = UploadedDocument.objects.create(
            uploader=self.employee,
            file_name="test_bill.jpg",
            file_sha256="abc123sha256test",
            file_size_bytes=1024,
            content_type="image/jpeg"
        )
        sub = InvoiceSubmission.objects.create(
            submission_id="BTI-202611-00093",
            employee=self.employee,
            source_document=uploaded_doc,
            party_name="SRI BALAJI MOTORS",
            party_gstin="29AAWFB0395H1ZX",
            total_amount=Decimal('12000.00'),
            taxable_amount=Decimal('10000.00'),
            cgst_amount=Decimal('900.00'),
            sgst_amount=Decimal('900.00'),
            status=InvoiceSubmission.STATUS_SUBMITTED
        )

        self.client.login(username=self.naveen.username, password=self.password)
        resp = self.client.post(
            reverse('invoices:verification_workspace', args=[sub.submission_id]),
            {
                'action': 'save',
                'party_name': 'SRI BALAJI MOTORS',
                'party_gstin': '29AAWFB0395H1ZX',
                'invoice_number': 'INV-3001',
                'invoice_type': 'VEHICLE_SERVICE',
                'policy_number': '',
                'vehicle_number': 'KA-04-E-7777',
                'labour_charges': '2500.00',
                'expense_ledger': 'Labour Charges / Vehicle Service',
                'taxable_amount': '10000.00',
                'cgst_amount': '900.00',
                'sgst_amount': '900.00',
                'igst_amount': '0.00',
                'total_amount': '11800.00',
            }
        )
        self.assertEqual(resp.status_code, 302)

        sub.refresh_from_db()
        self.assertEqual(sub.invoice_type, 'VEHICLE_SERVICE')
        self.assertEqual(sub.vehicle_number, 'KA-04-E-7777')
        self.assertEqual(sub.labour_charges, Decimal('2500.00'))
        self.assertEqual(sub.expense_ledger, 'Labour Charges / Vehicle Service')

    def test_normal_invoice_does_not_display_vehicle_or_policy(self):
        """Normal invoice must NOT display Vehicle / Lorry number or Policy number."""
        uploaded_doc = UploadedDocument.objects.create(
            uploader=self.employee,
            file_name="hardware_bill.jpg",
            file_sha256="abc456sha256normal",
            file_size_bytes=1024,
            content_type="image/jpeg"
        )
        sub_normal = InvoiceSubmission.objects.create(
            submission_id="BTI-202611-00094",
            employee=self.employee,
            source_document=uploaded_doc,
            invoice_type=InvoiceSubmission.INVOICE_TYPE_REGULAR,
            party_name="KAVITHA HARDWARE & PAINTS",
            party_gstin="29AAWFB0395H1ZX",
            invoice_number="INV-8888",
            vehicle_number="KA-01-XX-9999",
            expense_ledger="Building Repairs, Hardware & Paints",
            total_amount=Decimal('4500.00'),
            taxable_amount=Decimal('4500.00'),
            status=InvoiceSubmission.STATUS_SUBMITTED
        )
        self.assertFalse(sub_normal.is_vehicle_service)
        self.assertFalse(sub_normal.is_vehicle_insurance)
        self.client.login(username=self.naveen.username, password=self.password)
        resp = self.client.get(reverse('invoices:submission_detail', args=[sub_normal.submission_id]))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Vehicle / Lorry No.")
        self.assertNotContains(resp, "Policy Number")
        self.assertNotContains(resp, "Service / Garage")
        self.assertContains(resp, "Invoice / Bill No.")
        self.assertContains(resp, "Bill Date")
        self.assertContains(resp, "Expense Head")

    def test_rapidocr_interstate_subtotal_and_advance_deduction(self):
        """
        Verify that printed invoices with Sub Total, IGST, advance payment deductions,
        and alphanumeric dates (e.g. Intangles Lab) are correctly parsed.
        """
        raw_text = """
        Intangles Lab Private Limited
        GSTIN 27AADCI7688G1Z3 TAX INVOICE
        Invoice# : INT/25-26/01716
        Invoice Date : 17 Jun 2025
        Due Date : 17 Jun 2025
        Place Of Supply : Karnataka (29)

        Bill To
        BALAJI TRANSPORTS KA
        GSTIN 29AAWFB0395H1ZX

        # Item & Description HSN/SAC Qty Rate Discount IGST % Amt Amount
        1 INTFG-VHMS-EDP 84715000 30.00 Nos 10,000.00 75,000.00 18% 40,500.00 2,25,000.00
        2 SUBSCRIPTION CHARGES 998319 30.00 Nos 4,800.00 0.00 18% 25,920.00 1,44,000.00

        Sub Total 3,70,500.00
        IGST18 (18%) 66,690.00
        Total ₹4,37,190.00
        Payment Made (-) 4,29,810.00
        Balance Due ₹7,380.00

        Total In Words
        Indian Rupee Four Lakh Thirty-Seven Thousand One Hundred Ninety Only
        """
        meta = extract_metadata_from_text(raw_text)
        self.assertEqual(meta['invoice_number'], "INT/25-26/01716")
        self.assertEqual(str(meta['invoice_date']), "2025-06-17")
        self.assertEqual(meta['party_gstin'], "27AADCI7688G1Z3")
        self.assertEqual(meta['taxable_amount'], Decimal('370500.00'))
        self.assertEqual(meta['igst_amount'], Decimal('66690.00'))
        self.assertEqual(meta['cgst_amount'], Decimal('0.00'))
        self.assertEqual(meta['sgst_amount'], Decimal('0.00'))
        self.assertEqual(meta['total_amount'], Decimal('437190.00'))
        self.assertEqual(meta['expense_ledger'], "GPS-Expenses")

    def test_rapidocr_mahindra_spares_invoice_with_tax_table_and_words(self):
        """
        Verify that Mahindra & Mahindra commercial vehicle spare parts invoices
        with statutory tax summary table and 'rupees in words' with paise
        extract exact taxable, CGST, SGST, grand total, date, and vehicle number.
        """
        raw_text = """
        MAHINDRA & MAHINDRA LTD.
        Spares Business Unit Truck & Bus
        SY number - 216, 217 ,50,51, 53/1B, Jaymangala Village, Lakkur Hobli - 563160
        Tax Invoice
        Issued Under Rule 46 of CGST/SGST Rules, 2017.
        GSTIN: 29AAACM3025E2ZU
        GST Invoice NO: IN01272900439489
        GST Invoice Date 19.11.2026
        Challan/DelNo. & Dt: 8609554968 / 18.11.2026

        Bill To Party : B012100185
        Balaji Transport
        TUMKUR 572106
        Vehicle No: KA51AB2686

        Total
        Grand Total 97954.00
        Whether the tax is payable on reverse charge basis: NO
        Total Tax: FOURTEEN THOUSAND NINE HUNDRED FORTY TWO RUPEES EIGHT PAISE ONLY
        Grand Total: NINETY SEVEN THOUSAND NINE HUNDRED FIFTY THREE RUPEES FIFTY EIGHT PAISE ONLY
        Rate Taxable Value CGST Amt SGST Amt IGST Amt
        18% 83011.50 7471.04 7471.04 0.00
        """
        meta = extract_metadata_from_text(raw_text)
        self.assertEqual(meta['invoice_number'], "IN01272900439489")
        self.assertEqual(str(meta['invoice_date']), "2026-11-19")
        self.assertEqual(meta['party_gstin'], "29AAACM3025E2ZU")
        self.assertEqual(meta['vehicle_number'], "KA51AB2686")
        self.assertEqual(meta['taxable_amount'], Decimal('83011.50'))
        self.assertEqual(meta['cgst_amount'], Decimal('7471.04'))
        self.assertEqual(meta['sgst_amount'], Decimal('7471.04'))
        self.assertEqual(meta['igst_amount'], Decimal('0.00'))
        self.assertEqual(meta['total_amount'], Decimal('97954.00'))
        self.assertEqual(meta['expense_ledger'], "Purchase Spares & Consumables")

    def test_rapidocr_sai_auto_works_invoice(self):
        """
        Verify that Sai Auto Works vehicle repair bill with roundoff,
        statutory tax table, and REGISTRATION number extracts exact amounts.
        """
        raw_text = """
        Sai Auto Works
        GSTIN Number : 29ADLFS2360K1ZL
        TAX INVOICE
        INVOICE NO : LCR5178260001003 INVOICE DATE : 07.10.2026
        JOB CARD NO : WTY51782627001691
        CUSTOMER : BALAJI TRANSPORTS
        REGISTRATION KA06AB1686
        Subtotal 22.50 1,497.50 134.78 134.78 1,767.06
        ROUNDOFF (INR) 0.06-
        GRAND TOTAL (INR) 1,767.00
        Tax % Taxable Amount CGST SGST
        9.00 1,497.50 134.78 134.78
        AMOUNT IN WORDS : ONE THOUSAND SEVEN HUNDRED SIXTY SEVEN RUPEES ONLY
        """
        meta = extract_metadata_from_text(raw_text)
        self.assertEqual(meta['invoice_number'], "LCR5178260001003")
        self.assertEqual(str(meta['invoice_date']), "2026-10-07")
        self.assertEqual(meta['party_gstin'], "29ADLFS2360K1ZL")
        self.assertEqual(meta['vehicle_number'], "KA06AB1686")
        self.assertEqual(meta['taxable_amount'], Decimal('1497.50'))
        self.assertEqual(meta['cgst_amount'], Decimal('134.78'))
        self.assertEqual(meta['sgst_amount'], Decimal('134.78'))
        self.assertEqual(meta['igst_amount'], Decimal('0.00'))
        self.assertEqual(meta['total_amount'], Decimal('1767.00'))
        self.assertEqual(meta['invoice_type'], "VEHICLE_SERVICE")
        self.assertEqual(meta['expense_ledger'], "Labour Charges / Vehicle Service")

    def test_rapidocr_shriram_insurance_wrapped_vehicle_and_date(self):
        """
        Ensure registration date (24/11/2015) is never mistaken for invoice_number,
        and line-wrapped vehicle numbers like 'KA - 19 - AB -\\n2209' are extracted.
        """
        raw_text = """
        SHRIRAM GENERAL INSURANCE COMPANY LIMITED
        CERTIFICATE CUM POLICY SCHEDULE
        Policy No.
        418012/31/27/004718
        GSTIN No. 29AAKCS2509K1ZZ
        PREMIUM AMOUNT
        46697.00
        REGISTRATI
        ON MARK &
        PLACE
        DATE OF REGN. /
        DELIVERY
        24/11/2015
        KA - 19 - AB -
        2209 &
        MANGALORE
        Page 1 of 2
        """
        meta = extract_metadata_from_text(raw_text)
        self.assertEqual(meta['invoice_type'], "VEHICLE_INSURANCE")
        self.assertEqual(meta['policy_number'], "418012/31/27/004718")
        self.assertEqual(meta['invoice_number'], "418012/31/27/004718")
        self.assertEqual(meta['vehicle_number'], "KA-19-AB-2209")
        self.assertEqual(meta['expense_ledger'], "Lorry Insurance")

    def test_segmentation_keeps_multipage_insurance_policy_together(self):
        """
        A 2-page insurance policy with the same Policy No / GSTIN and continuation
        markers must NOT be split into 2 separate PDFs even if vendor string differs.
        """
        from apps.invoices.segmentation import detect_invoice_boundaries
        pages_data = [
            {
                'page_num': 1,
                'page_idx': 0,
                'invoice_number': '418012/31/27/004718',
                'policy_number': '418012/31/27/004718',
                'party_gstin': '29AAKCS2509K1ZZ',
                'party_name': 'SHRIRAM GENERAL INSURANCE COMPANY LIMITED',
                'has_invoice_header': False,
                'has_vendor_header': False,
                'has_total_or_footer': True,
                'is_page_1': True,
                'is_continuation': False,
                'total_amount': Decimal('46697.00'),
            },
            {
                'page_num': 2,
                'page_idx': 1,
                'invoice_number': '418012/31/27/004718',
                'policy_number': '418012/31/27/004718',
                'party_gstin': '29AAKCS2509K1ZZ',
                'party_name': 'Shriram General Insurance Co. Ltd.',
                'has_invoice_header': False,
                'has_vendor_header': False,
                'has_total_or_footer': True,
                'is_page_1': False,
                'is_continuation': True,
                'total_amount': Decimal('0.00'),
            },
        ]
        segs = detect_invoice_boundaries(pages_data)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]['start_page'], 1)
        self.assertEqual(segs[0]['end_page'], 2)

    def test_sender_can_view_and_search_by_invoice_number_or_party_name(self):
        """
        Senders should see Party Name and Invoice/Policy Number prominently on
        employee_dashboard and my_submissions, and search by them without needing BTI ID.
        """
        sub = InvoiceSubmission.objects.create(
            submission_id="BTI-202611-77001",
            employee=self.employee,
            party_name="SHRIRAM GENERAL INSURANCE COMPANY LTD",
            invoice_number="417055/31/27/006296",
            policy_number="417055/31/27/006296",
            vehicle_number="KA-06-AC-4970",
            total_amount=Decimal('51071.00'),
            status=InvoiceSubmission.STATUS_SUBMITTED,
        )
        self.client.login(username=self.employee.username, password=self.password)

        # Dashboard shows party name and supports search
        dash_resp = self.client.get(reverse('core:employee_dashboard'), {'q': 'SHRIRAM'})
        self.assertEqual(dash_resp.status_code, 200)
        self.assertContains(dash_resp, "SHRIRAM GENERAL INSURANCE COMPANY LTD")

        # My Submissions displays party name and supports searching by party_name, invoice_number, and vehicle_number
        for q_term in ['SHRIRAM', '006296', 'KA-06-AC-4970']:
            list_resp = self.client.get(reverse('invoices:my_submissions'), {'q': q_term})
            self.assertEqual(list_resp.status_code, 200)
            self.assertContains(list_resp, "SHRIRAM GENERAL INSURANCE COMPANY LTD")

    def test_reconciliation_recovers_missed_zero_on_tax_and_words(self):
        """
        Verify both:
        1. Missed zero on total_amount is recovered via amount_in_words.
        2. Missed zero on a single tax field (e.g. IGST 6669 -> 66690) is recovered
           when Taxable + Tax = Total reconciles at 10x.
        """
        # Case 1: IGST dropped a zero (6669.00 instead of 66690.00)
        data_tax = {
            'taxable_amount': 370500.0,
            'igst_amount': 6669.0,
            'cgst_amount': 0.0,
            'sgst_amount': 0.0,
            'total_amount': 437190.0,
            'amount_in_words': 'Four Lakh Thirty Seven Thousand One Hundred Ninety Only',
        }
        rec1, w1 = _reconcile_extracted_amounts(data_tax, party_gstin='27AADCI7688G1Z3')
        self.assertEqual(rec1['igst_amount'], Decimal('66690.00'))
        self.assertEqual(rec1['total_amount'], Decimal('437190.00'))
        self.assertTrue(any('Recovered missed zero on IGST' in w for w in w1))

        # Case 2: Entire invoice dropped a zero compared to words
        data_all = {
            'taxable_amount': 1000.0,
            'cgst_amount': 90.0,
            'sgst_amount': 90.0,
            'igst_amount': 0.0,
            'total_amount': 1180.0,
            'amount_in_words': 'Eleven Thousand Eight Hundred Only',
        }
        rec2, w2 = _reconcile_extracted_amounts(data_all, party_gstin='29AAWFB0395H1ZX')
        self.assertEqual(rec2['taxable_amount'], Decimal('10000.00'))
        self.assertEqual(rec2['cgst_amount'], Decimal('900.00'))
        self.assertEqual(rec2['sgst_amount'], Decimal('900.00'))
        self.assertEqual(rec2['total_amount'], Decimal('11800.00'))

    @patch('apps.invoices.gemini_service.get_available_gemini_keys', return_value=['AQ.TestKey_123456789'])
    @patch('apps.invoices.gemini_extractor._get_gemini_client')
    def test_gemini_does_not_put_item_costs_into_labour_charges(self, mock_get_client, mock_keys):
        """
        If all extracted line items are physical parts/spares (e.g. glass, beeding, silicon),
        labour_charges must be zeroed out instead of copying the ₹6,900 item total.
        """
        mock_response = MagicMock()
        mock_response.parsed = InvoiceSchema(
            invoice_type="VEHICLE_SERVICE",
            party_name="AMG AL-MALIK GLASS",
            taxable_amount=6900.0,
            total_amount=6900.0,
            labour_charges=6900.0,
            items=[
                {"description": "Mahindra front glass", "quantity": 1.0, "unit_price": 4800.0, "amount": 4800.0},
                {"description": "Beeding", "quantity": 1.0, "unit_price": 1300.0, "amount": 1300.0},
                {"description": "Silicon", "quantity": 2.0, "unit_price": 300.0, "amount": 600.0},
                {"description": "Auto", "amount": 200.0},
            ],
        )
        client = MagicMock()
        client.models.generate_content.return_value = mock_response
        mock_get_client.return_value = client

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
            tf.write(b"X" * 600)
            tf_path = tf.name
        try:
            res = extract_with_gemini(tf_path)
            self.assertIsNotNone(res)
            self.assertEqual(res['total_amount'], Decimal('6900.00'))
            self.assertEqual(res['labour_charges'], Decimal('0.00'))
        finally:
            os.unlink(tf_path)
