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
