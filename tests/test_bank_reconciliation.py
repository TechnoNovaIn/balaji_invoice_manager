"""
Test suite for ICICI Bank Statement Parsing and Invoice Reconciliation.
Tests:
- ICICI Narration parsing (NEFT, RTGS, UPI, IMPS)
- CSV / Excel statement ingestion
- Automated invoice matching vs missing invoice (unbilled) flagging
- Manual linking and ignore actions
- Role-based permissions
"""
import io
import csv
from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.accounts.models import User, UserRole
from apps.invoices.models import (
    BankStatement,
    BankTransaction,
    InvoiceSubmission,
    VendorMaster,
)
from apps.invoices.bank_reconciliation import (
    extract_vendor_from_narration,
    ingest_and_reconcile_statement,
    clean_decimal,
)


class BankReconciliationTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.naveen = User.objects.create_user(
            username='naveen_accounts',
            email='naveen@balajitransports.com',
            password='TestPassword123!',
            role=UserRole.NAVEEN,
        )
        self.driver = User.objects.create_user(
            username='driver_ramesh',
            email='ramesh@balajitransports.com',
            password='TestPassword123!',
            role=UserRole.EMPLOYEE,
        )

        # Setup sample vendor
        self.vendor = VendorMaster.objects.create(
            name='SRINIDHI ENGINEERING INDUSTRIES',
            gstin='29ACKFS0217R1ZI',
            default_expense_ledger='Workshop Repairs & Maintenance',
        )

        # Setup sample existing invoice in the system (Total: ₹6,839.23)
        self.inv1 = InvoiceSubmission.objects.create(
            submission_id='BTI-202611-00001',
            employee=self.driver,
            party_name='SRINIDHI ENGINEERING INDUSTRIES',
            party_gstin='29ACKFS0217R1ZI',
            invoice_number='048',
            invoice_date=date(2026, 11, 5),
            total_amount=Decimal('6839.23'),
            taxable_amount=Decimal('5796.00'),
            cgst_amount=Decimal('521.64'),
            sgst_amount=Decimal('521.64'),
            status=InvoiceSubmission.STATUS_SUBMITTED,
        )

    def test_clean_decimal(self):
        self.assertEqual(clean_decimal('6,839.23'), Decimal('6839.23'))
        self.assertEqual(clean_decimal('₹ 15,000.00'), Decimal('15000.00'))
        self.assertIsNone(clean_decimal('0.00'))
        self.assertIsNone(clean_decimal('-'))
        self.assertIsNone(clean_decimal(''))

    def test_extract_vendor_from_narration(self):
        # NEFT
        name, ref = extract_vendor_from_narration('NEFT-N948201827-SRINIDHI ENGINEERING-TUMKUR')
        self.assertIn('SRINIDHI ENGINEERING', name.upper())
        self.assertEqual(ref, 'N948201827')

        # RTGS
        name, ref = extract_vendor_from_narration('RTGS-R849201928-KST LUBRICANTS-BANGALORE')
        self.assertIn('KST LUBRICANTS', name.upper())
        self.assertEqual(ref, 'R849201928')

        # UPI
        name, ref = extract_vendor_from_narration('UPI/DR/1234567890/MARUTHI HARDWARE/KKBK/test')
        self.assertIn('MARUTHI HARDWARE', name.upper())

        # IMPS
        name, ref = extract_vendor_from_narration('MMT/IMPS/987654321/NATIONAL TRAVELS/ICIC/')
        self.assertIn('NATIONAL TRAVELS', name.upper())
        self.assertEqual(ref, '987654321')

    def test_ingest_and_reconcile_csv_statement(self):
        """Simulate an ICICI Bank CSV export with 1 matched payment and 1 missing invoice payment."""
        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(['Account No: 123456789012', 'ICICI Bank Statement'])
        writer.writerow(['Date', 'Particulars / Narration', 'Cheque/Ref No', 'Withdrawal (Dr)', 'Deposit (Cr)', 'Balance'])
        # 1. Matching Srinidhi Engineering (₹6,839.23)
        writer.writerow(['05/11/2026', 'NEFT-N948201827-SRINIDHI ENGINEERING-TUMKUR', 'N948201827', '6839.23', '', '450000.00'])
        # 2. Advance to Tyre vendor without invoice (₹15,000.00)
        writer.writerow(['10/11/2026', 'UPI/DR/8371902847/RAMESH TYRES TUMKUR/KKBK', '8371902847', '15000.00', '', '435000.00'])

        file_obj = io.BytesIO(csv_buffer.getvalue().encode('utf-8'))
        stmt, matched, unmatched = ingest_and_reconcile_statement(
            file_obj=file_obj,
            filename='ICICI_Statement_Nov2026.csv',
            user=self.naveen
        )

        self.assertEqual(stmt.total_debits_count, 2)
        self.assertEqual(matched, 1)
        self.assertEqual(unmatched, 1)

        # Check transactions in DB
        txns = list(stmt.transactions.order_by('txn_date'))
        # First transaction should be auto-matched to inv1
        self.assertEqual(txns[0].match_status, BankTransaction.STATUS_MATCHED)
        self.assertEqual(txns[0].matched_submission, self.inv1)
        self.assertEqual(txns[0].debit_amount, Decimal('6839.23'))

        # Second transaction should be flagged as UNMATCHED (Missing Invoice)
        self.assertEqual(txns[1].match_status, BankTransaction.STATUS_UNMATCHED)
        self.assertIsNone(txns[1].matched_submission)
        self.assertEqual(txns[1].debit_amount, Decimal('15000.00'))

    def test_reconciliation_dashboard_view(self):
        self.client.force_login(self.naveen)
        url = reverse('invoices:bank_reconciliation')
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'ICICI Bank vs Invoice Reconciliation')
        self.assertContains(resp, 'Missing Invoices')

    def test_driver_cannot_access_reconciliation(self):
        self.client.force_login(self.driver)
        url = reverse('invoices:bank_reconciliation')
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 403)

    def test_manual_link_transaction(self):
        stmt = BankStatement.objects.create(
            uploaded_by=self.naveen,
            original_file=SimpleUploadedFile('test.csv', b'test'),
            filename='test.csv',
            total_debits_count=1,
            total_debit_amount=Decimal('5000.00'),
            unmatched_count=1,
        )
        txn = BankTransaction.objects.create(
            statement=stmt,
            txn_date=date(2026, 11, 12),
            narration='NEFT-12345-MANUAL LINK VENDOR',
            debit_amount=Decimal('5000.00'),
            match_status=BankTransaction.STATUS_UNMATCHED,
        )

        self.client.force_login(self.naveen)
        link_url = reverse('invoices:link_bank_transaction', args=[txn.id])
        resp = self.client.post(link_url, {'submission_id': self.inv1.submission_id})
        self.assertEqual(resp.status_code, 302)

        txn.refresh_from_db()
        self.assertEqual(txn.match_status, BankTransaction.STATUS_MATCHED)
        self.assertEqual(txn.matched_submission, self.inv1)
        self.assertEqual(txn.match_type, BankTransaction.MATCH_MANUAL)

    def test_ignore_bank_transaction(self):
        stmt = BankStatement.objects.create(
            uploaded_by=self.naveen,
            original_file=SimpleUploadedFile('test.csv', b'test'),
            filename='test.csv',
            total_debits_count=1,
            total_debit_amount=Decimal('150.00'),
            unmatched_count=1,
        )
        txn = BankTransaction.objects.create(
            statement=stmt,
            txn_date=date(2026, 11, 15),
            narration='ICICI SMS ALERT CHARGES',
            debit_amount=Decimal('150.00'),
            match_status=BankTransaction.STATUS_UNMATCHED,
        )

        self.client.force_login(self.naveen)
        ignore_url = reverse('invoices:ignore_bank_transaction', args=[txn.id])
        resp = self.client.post(ignore_url, {'ignore_reason': 'Bank Charges / Annual Fee'})
        self.assertEqual(resp.status_code, 302)

        txn.refresh_from_db()
        self.assertEqual(txn.match_status, BankTransaction.STATUS_IGNORED)
        self.assertEqual(txn.ignore_reason, 'Bank Charges / Annual Fee')
