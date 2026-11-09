from decimal import Decimal
from django.test import TestCase, Client
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from apps.accounts.models import User, UserRole, Department
from apps.invoices.models import InvoiceSubmission, InvoiceDocument
from apps.invoices.services import create_invoice_submission
from apps.invoices.tally_exporter import generate_tally_xml


def create_dummy_jpeg(name="receipt.jpg", content_tag="dummybody"):
    return SimpleUploadedFile(
        name=name,
        content=b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + content_tag.encode(),
        content_type="image/jpeg"
    )


class TallyGstWorkflowTests(TestCase):
    """
    Test suite for Naveen's Tally Voucher Entry and GST Portal Filing workflow.
    """

    def setUp(self):
        self.dept = Department.objects.create(name="Fleet", code="FLEET")

        self.emp = User.objects.create_user(
            username="emp_ravi",
            password="TestPassword@123",
            role=UserRole.EMPLOYEE,
            first_name="Ravi",
            last_name="Teja",
            department=self.dept
        )

        self.naveen = User.objects.create_user(
            username="naveen_acc",
            password="TestPassword@123",
            role=UserRole.NAVEEN,
            first_name="Naveen",
            last_name="Accounts",
            department=self.dept
        )

        # Create a sample invoice submitted by employee
        self.submission = create_invoice_submission(
            user=self.emp,
            files=[create_dummy_jpeg("fuel_bill.jpg")]
        )

    def test_save_tally_voucher_details(self):
        """Naveen enters party, GSTIN, invoice details, and marks as entered in Tally."""
        self.client.login(username="naveen_acc", password="TestPassword@123")

        url = reverse('invoices:tally_workspace', kwargs={'submission_id': self.submission.submission_id})
        response = self.client.post(url, {
            'party_name': 'Indian Oil Corporation',
            'party_gstin': '29ABCDE1234F1Z5',
            'invoice_number': 'IOCL-2026-889',
            'invoice_date': '2026-11-05',
            'expense_ledger': 'Diesel Expenses',
            'taxable_amount': '1000.00',
            'cgst_amount': '90.00',
            'sgst_amount': '90.00',
            'igst_amount': '0.00',
            'total_amount': '1180.00',
            'tally_voucher_no': 'PUR-2026-0042',
            'is_entered_in_tally': 'on',
        })

        self.assertEqual(response.status_code, 302)

        self.submission.refresh_from_db()
        self.assertEqual(self.submission.party_name, 'Indian Oil Corporation')
        self.assertEqual(self.submission.party_gstin, '29ABCDE1234F1Z5')
        self.assertEqual(self.submission.invoice_number, 'IOCL-2026-889')
        self.assertEqual(self.submission.taxable_amount, Decimal('1000.00'))
        self.assertEqual(self.submission.total_amount, Decimal('1180.00'))
        self.assertTrue(self.submission.is_entered_in_tally)
        self.assertIsNotNone(self.submission.tally_entered_at)
        self.assertFalse(self.submission.is_filed_in_gst)
        self.assertEqual(self.submission.accounting_status, 'IN_TALLY')

    def test_quick_toggle_tally_and_gst_status(self):
        """1-click toggle buttons directly flip Tally and GST flags."""
        self.client.login(username="naveen_acc", password="TestPassword@123")

        # Toggle Tally
        toggle_tally_url = reverse('invoices:toggle_tally', kwargs={'submission_id': self.submission.submission_id})
        resp = self.client.post(toggle_tally_url)
        self.assertEqual(resp.status_code, 302)

        self.submission.refresh_from_db()
        self.assertTrue(self.submission.is_entered_in_tally)

        # Toggle GST
        toggle_gst_url = reverse('invoices:toggle_gst', kwargs={'submission_id': self.submission.submission_id})
        resp = self.client.post(toggle_gst_url)
        self.assertEqual(resp.status_code, 302)

        self.submission.refresh_from_db()
        self.assertTrue(self.submission.is_filed_in_gst)
        self.assertEqual(self.submission.accounting_status, 'GST_FILED')

    def test_export_tally_xml_format(self):
        """Exporting Tally XML produces valid XML with vouchers and ledger entries."""
        self.submission.party_name = 'Sri Lakshmi Spares'
        self.submission.taxable_amount = Decimal('5000.00')
        self.submission.cgst_amount = Decimal('450.00')
        self.submission.sgst_amount = Decimal('450.00')
        self.submission.total_amount = Decimal('5900.00')
        self.submission.expense_ledger = 'Purchase Spares & Consumables'
        self.submission.save()

        xml_output = generate_tally_xml([self.submission])
        self.assertIn('<ENVELOPE>', xml_output)
        self.assertIn('<VOUCHERTYPENAME>Purchase</VOUCHERTYPENAME>', xml_output)
        self.assertIn('Sri Lakshmi Spares', xml_output)
        self.assertIn('Purchase Spares &amp; Consumables', xml_output)
        self.assertIn('Input CGST', xml_output)
        self.assertIn('Input SGST', xml_output)

        # Test endpoint
        self.client.login(username="naveen_acc", password="TestPassword@123")
        resp = self.client.get(reverse('invoices:export_tally_xml'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'application/xml')
        self.assertIn('attachment; filename="tally_invoices_import_', resp['Content-Disposition'])

    def test_export_gst_csv_format(self):
        """Exporting GST summary CSV returns correct headers and rows."""
        self.submission.party_name = 'Bharath Petroleum'
        self.submission.party_gstin = '33ABCDE1234F1Z1'
        self.submission.taxable_amount = Decimal('2000.00')
        self.submission.total_amount = Decimal('2360.00')
        self.submission.save()

        self.client.login(username="naveen_acc", password="TestPassword@123")
        resp = self.client.get(reverse('invoices:export_gst_csv'))
        self.assertEqual(resp.status_code, 200)
        self.assertIn('text/csv', resp['Content-Type'])
        content = resp.content.decode('utf-8')
        self.assertIn('GSTIN of Supplier', content)
        self.assertIn('Bharath Petroleum', content)
        self.assertIn('33ABCDE1234F1Z1', content)

    def test_employee_denied_access_to_tally_workspace(self):
        """Field employees cannot access Naveen's Tally workspace (403 Forbidden)."""
        self.client.login(username="emp_ravi", password="TestPassword@123")
        resp = self.client.get(reverse('invoices:tally_workspace', kwargs={'submission_id': self.submission.submission_id}))
        self.assertEqual(resp.status_code, 403)

    def test_export_gst_portal_excel(self):
        """Test downloading official GST Portal compliant .xlsx file."""
        import openpyxl
        import io

        self.submission.party_name = 'Hindustan Petroleum'
        self.submission.party_gstin = '29AABCH1234F1Z8'
        self.submission.total_amount = Decimal('3540.00')
        self.submission.taxable_amount = Decimal('3000.00')
        self.submission.save()

        self.client.login(username="naveen_acc", password="TestPassword@123")
        resp = self.client.get(reverse('invoices:export_gst_portal_excel'))
        self.assertEqual(resp.status_code, 200)
        self.assertIn('spreadsheetml.sheet', resp['Content-Type'])

        wb = openpyxl.load_workbook(io.BytesIO(resp.content))
        self.assertIn('b2b', wb.sheetnames)
        ws = wb['b2b']
        self.assertEqual(ws.cell(row=1, column=1).value, 'GSTIN of Supplier')
        self.assertEqual(ws.cell(row=2, column=1).value, '29AABCH1234F1Z8')
        self.assertEqual(ws.cell(row=2, column=2).value, 'Hindustan Petroleum')

    def test_batch_complete_and_undo_tally(self):
        """Test batch completion of multiple invoices via checkboxes and undo action."""
        sub2 = create_invoice_submission(
            user=self.emp,
            files=[create_dummy_jpeg("bill_2.jpg", content_tag="unique_bill_2")]
        )
        sub3 = create_invoice_submission(
            user=self.emp,
            files=[create_dummy_jpeg("bill_3.jpg", content_tag="unique_bill_3")]
        )

        self.client.login(username="naveen_acc", password="TestPassword@123")

        # Mark sub1 and sub2 as completed in Tally via batch endpoint
        batch_url = reverse('invoices:batch_complete_tally')
        resp = self.client.post(batch_url, {
            'selected_invoices': [self.submission.submission_id, sub2.submission_id]
        })
        self.assertEqual(resp.status_code, 302)

        self.submission.refresh_from_db()
        sub2.refresh_from_db()
        sub3.refresh_from_db()

        self.assertTrue(self.submission.is_entered_in_tally)
        self.assertIsNotNone(self.submission.tally_entered_at)
        self.assertTrue(sub2.is_entered_in_tally)
        self.assertIsNotNone(sub2.tally_entered_at)
        # sub3 was not selected, remains False
        self.assertFalse(sub3.is_entered_in_tally)
        self.assertIsNone(sub3.tally_entered_at)

        # Batch undo sub1
        undo_url = reverse('invoices:batch_undo_tally')
        resp_undo = self.client.post(undo_url, {
            'selected_invoices': [self.submission.submission_id]
        })
        self.assertEqual(resp_undo.status_code, 302)

        self.submission.refresh_from_db()
        self.assertFalse(self.submission.is_entered_in_tally)
        self.assertIsNone(self.submission.tally_entered_at)

    def test_carryover_and_fresh_feed_dashboard(self):
        """Test detection of carry-over invoices from previous days and clean fresh feed state."""
        from datetime import timedelta
        self.client.login(username="naveen_acc", password="TestPassword@123")

        # Set self.submission to 2 days ago
        InvoiceSubmission.objects.filter(pk=self.submission.pk).update(
            created_at=timezone.now() - timedelta(days=2)
        )

        resp = self.client.get(reverse('core:naveen_dashboard') + '?tab=pending')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['carryover_count'], 1)
        self.assertFalse(resp.context['is_fresh_feed'])

        # Mark self.submission completed
        self.submission.is_entered_in_tally = True
        self.submission.save()

        # Check dashboard again - should be fresh feed now!
        resp_fresh = self.client.get(reverse('core:naveen_dashboard') + '?tab=pending')
        self.assertEqual(resp_fresh.status_code, 200)
        self.assertEqual(resp_fresh.context['carryover_count'], 0)
        self.assertEqual(resp_fresh.context['today_pending_count'], 0)
        self.assertTrue(resp_fresh.context['is_fresh_feed'])

    def test_vendor_directory_and_tax_split(self):
        """Test Balaji's official vendor directory lookup and statutory tax calculation."""
        from apps.invoices.vendor_directory import (
            find_vendor_by_gstin,
            find_vendor_by_name,
            get_all_vendor_names,
            get_tax_split,
        )

        # 1. Lookup by GSTIN
        v_jk = find_vendor_by_gstin('29AAACJ6716F1ZK')
        self.assertIsNotNone(v_jk)
        self.assertEqual(v_jk['name'], 'JK TYRE & INDUSTRIES LIMITED')
        self.assertEqual(v_jk['state'], 'Karnataka')

        # 2. Fuzzy / Substring lookup by Name
        v_balaji = find_vendor_by_name('Sree Balaji Automotives')
        self.assertIsNotNone(v_balaji)
        self.assertEqual(v_balaji['gstin'], '29ADLPJ7564J1ZV')

        # 3. All vendor names list
        all_names = get_all_vendor_names()
        self.assertGreater(len(all_names), 50)
        self.assertIn('JK TYRE & INDUSTRIES LIMITED', all_names)
        self.assertIn('STEEL HOUSE', all_names)

        # 4. Intra-state Tax Split (Karnataka 29 -> CGST 9% + SGST 9%)
        split_intra = get_tax_split('29AAACJ6716F1ZK', taxable_value=10000.0, tax_rate_percent=18.0)
        self.assertFalse(split_intra['is_interstate'])
        self.assertEqual(split_intra['cgst'], 900.0)
        self.assertEqual(split_intra['sgst'], 900.0)
        self.assertEqual(split_intra['igst'], 0.0)

        # 5. Inter-state Tax Split (Maharashtra 27 -> IGST 18%)
        split_inter = get_tax_split('27AAPFJ9153L1ZL', taxable_value=10000.0, tax_rate_percent=18.0)
        self.assertTrue(split_inter['is_interstate'])
        self.assertEqual(split_inter['cgst'], 0.0)
        self.assertEqual(split_inter['sgst'], 0.0)
        self.assertEqual(split_inter['igst'], 1800.0)


