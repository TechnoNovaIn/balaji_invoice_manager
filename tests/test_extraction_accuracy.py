"""
Regression tests for offline (non-Gemini) text extraction accuracy:
supplier GSTIN priority over fuzzy vendor names, grand-total parsing, GSTIN checksum,
correctness-aware confidence, and the verification-desk date field.
"""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import Department, User, UserRole
from apps.invoices.models import InvoiceSubmission
from apps.invoices.reader import extract_metadata_from_text
from apps.invoices.services import create_invoice_submission
from apps.invoices.vendor_validator import gstin_checksum_valid
from tests.test_tally_gst import create_dummy_jpeg

SPARES_INVOICE = """TAX INVOICE
Sri Lakshmi Auto Spares
GSTIN: 29ABCDE1234F1Z5
Invoice No: SLAS-2026-0418          Date: 15-11-2026
Bill To: Balaji Transports, Bengaluru (GSTIN 29AAWFB0395H1ZX)

Description               HSN      Qty   Rate      Amount
Brake Lining Set          8708     4     1,250.00  5,000.00
Engine Oil 15W40 (15L)    2710     2     2,500.00  5,000.00

Taxable Value                                      10,000.00
CGST @ 9%                                           900.00
SGST @ 9%                                           900.00
Total Invoice Value                                11,800.00
"""


class OfflineExtractionAccuracyTests(TestCase):
    def test_printed_supplier_gstin_beats_fuzzy_vendor_name(self):
        meta = extract_metadata_from_text(SPARES_INVOICE)
        self.assertEqual(meta['party_gstin'], '29ABCDE1234F1Z5')
        self.assertEqual(meta['party_name'], 'Sri Lakshmi Auto Spares')

    def test_buyer_name_never_becomes_vendor(self):
        meta = extract_metadata_from_text(SPARES_INVOICE)
        self.assertNotIn('BALAJI', (meta['party_name'] or '').upper())
        self.assertNotEqual(meta['party_gstin'], '29AAWFB0395H1ZX')

    def test_total_invoice_value_label_is_read_from_same_line(self):
        meta = extract_metadata_from_text(SPARES_INVOICE)
        self.assertEqual(meta['taxable_amount'], Decimal('10000.00'))
        self.assertEqual(meta['cgst_amount'], Decimal('900.00'))
        self.assertEqual(meta['sgst_amount'], Decimal('900.00'))
        self.assertEqual(meta['total_amount'], Decimal('11800.00'))

    def test_total_below_taxable_is_recomputed(self):
        text = SPARES_INVOICE.replace('Total Invoice Value                                11,800.00', 'Total')
        meta = extract_metadata_from_text(text)
        self.assertGreaterEqual(meta['total_amount'], meta['taxable_amount'])
        self.assertEqual(meta['total_amount'], Decimal('11800.00'))

    def test_date_extracted(self):
        meta = extract_metadata_from_text(SPARES_INVOICE)
        self.assertEqual(str(meta['invoice_date']), '2026-11-15')

    def test_confidence_penalises_bad_gstin_checksum(self):
        meta = extract_metadata_from_text(SPARES_INVOICE)  # sample GSTIN has an invalid check digit
        self.assertLess(meta['confidence'], 0.9)

    def test_gstin_checksum(self):
        self.assertTrue(gstin_checksum_valid('29AAWFB0395H1ZX'))
        self.assertTrue(gstin_checksum_valid('27AAPFU0939F1ZV'))
        self.assertFalse(gstin_checksum_valid('29AAWFB0395H1ZY'))
        self.assertFalse(gstin_checksum_valid('29ABCDE1234F1Z5'))
        self.assertFalse(gstin_checksum_valid(''))


class VerificationDeskDateFieldTests(TestCase):
    def setUp(self):
        dept = Department.objects.create(name="Fleet", code="FLEET")
        self.emp = User.objects.create_user(
            username="emp_date", password="TestPassword@123", role=UserRole.EMPLOYEE, department=dept)
        self.naveen = User.objects.create_user(
            username="naveen_date", password="TestPassword@123", role=UserRole.NAVEEN, department=dept)
        self.sub = create_invoice_submission(
            user=self.emp, files=[create_dummy_jpeg("date.jpg", content_tag="date_bill")])

    def _page(self):
        self.client.force_login(self.naveen)
        return self.client.get(
            reverse('invoices:verification_workspace', kwargs={'submission_id': self.sub.submission_id})
        ).content.decode()

    def test_extracted_date_is_shown(self):
        InvoiceSubmission.objects.filter(pk=self.sub.pk).update(invoice_date='2026-11-15')
        self.assertIn('id="fieldInvoiceDate" value="2026-11-15"', self._page())

    def test_missing_date_is_left_blank_for_reviewer(self):
        InvoiceSubmission.objects.filter(pk=self.sub.pk).update(invoice_date=None)
        self.assertIn('id="fieldInvoiceDate" value=""', self._page())
