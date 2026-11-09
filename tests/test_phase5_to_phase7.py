"""
Automated Test Suite for Balaji InvoiceHub Phases 5, 6, and 7.
Validates:
- Phase 5: Centralized Purchase Register, Two-Panel Verification Desk, Field Audit Trails, GSTR-2A Recon.
- Phase 6: Tally Structured Excel Export, Ledger Mapping, Export Audit Logs.
- Phase 7: Live Financial KPIs, Monthly Trends, Top Suppliers, Executive Excel Reports.
- Core Constraints: Non-blocking duplicate uploads, Financial safety gates, RBAC isolation.
"""
from decimal import Decimal
import io
import openpyxl
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.accounts.models import User, UserRole, Department
from apps.invoices.models import (
    InvoiceSubmission,
    InvoiceDocument,
    PurchaseAuditTrail,
    TallyLedgerMapping,
    ExportAuditLog,
)
from apps.invoices.purchase_service import filter_purchase_register, record_field_corrections, compare_with_gstr2a
from apps.invoices.tally_excel_exporter import generate_tally_structured_excel
from apps.invoices.analytics_service import (
    get_executive_kpis,
    get_monthly_trends,
    get_top_suppliers,
    get_ledger_distribution,
    generate_executive_excel_report,
)


class Phase5To7MasterTestSuite(TestCase):
    def setUp(self):
        self.dept, _ = Department.objects.get_or_create(code="OPS", defaults={"name": "Logistics"})
        
        self.employee = User.objects.create_user(
            username='emp_test',
            password='TestEmp@2026',
            role=UserRole.EMPLOYEE,
            department=self.dept
        )
        self.naveen = User.objects.create_user(
            username='naveen_test',
            password='TestNaveen@2026',
            role=UserRole.NAVEEN,
            department=self.dept
        )
        self.management = User.objects.create_user(
            username='mgmt_test',
            password='TestMgmt@2026',
            role=UserRole.MANAGEMENT,
            department=self.dept
        )
        self.admin = User.objects.create_user(
            username='admin_test',
            password='TestAdmin@2026',
            role=UserRole.ADMIN,
            is_staff=True,
            is_superuser=True,
            department=self.dept
        )

        # Seed Invoice 1: Diesel from verified vendor
        self.sub1 = InvoiceSubmission.objects.create(
            submission_id='BTI-202611-00101',
            employee=self.employee,
            party_name='MURUGAN DIESEL SERVICE',
            party_gstin='29AAWFB0395H1ZX',
            invoice_number='INV-8891',
            invoice_date=timezone.now().date(),
            taxable_amount=Decimal('10000.00'),
            cgst_amount=Decimal('900.00'),
            sgst_amount=Decimal('900.00'),
            igst_amount=Decimal('0.00'),
            total_amount=Decimal('11800.00'),
            expense_ledger='Diesel Expenses',
            status=InvoiceSubmission.STATUS_SUBMITTED,
            is_entered_in_tally=False
        )

        # Seed Invoice 2: Interstate spares
        self.sub2 = InvoiceSubmission.objects.create(
            submission_id='BTI-202611-00102',
            employee=self.employee,
            party_name='TVS AUTO SPARES',
            party_gstin='33AABCT1234A1Z1',  # Tamil Nadu (33)
            invoice_number='TVS-404',
            invoice_date=timezone.now().date(),
            taxable_amount=Decimal('5000.00'),
            cgst_amount=Decimal('0.00'),
            sgst_amount=Decimal('0.00'),
            igst_amount=Decimal('900.00'),
            total_amount=Decimal('5900.00'),
            expense_ledger='Purchase Spares & Consumables',
            status=InvoiceSubmission.STATUS_VERIFIED,
            is_entered_in_tally=True,
            tally_voucher_no='VCH-2026-091'
        )

    # ==========================================================================
    # PHASE 5 TESTS: Purchase Register & Verification
    # ==========================================================================

    def test_01_purchase_register_filtering(self):
        """Test multi-criteria filtering on purchase register."""
        # Query search
        res1 = filter_purchase_register({'q': 'MURUGAN'})
        self.assertEqual(res1.count(), 1)
        self.assertEqual(res1.first().submission_id, 'BTI-202611-00101')

        # Status filter
        res2 = filter_purchase_register({'status': InvoiceSubmission.STATUS_VERIFIED})
        self.assertEqual(res2.count(), 1)
        self.assertEqual(res2.first().submission_id, 'BTI-202611-00102')

        # Tally status filter
        res3 = filter_purchase_register({'tally_status': 'PENDING'})
        self.assertEqual(res3.count(), 1)
        self.assertEqual(res3.first().submission_id, 'BTI-202611-00101')

    def test_02_field_corrections_and_audit_trail(self):
        """Test recording corrections into PurchaseAuditTrail."""
        new_data = {
            'party_name': 'MURUGAN FUEL LOGISTICS',
            'total_amount': Decimal('12000.00'),
        }
        updated = record_field_corrections(self.sub1, new_data, self.naveen, reason="Corrected vendor brand name")
        self.assertIn('party_name', updated)
        self.assertIn('total_amount', updated)

        audits = PurchaseAuditTrail.objects.filter(submission=self.sub1)
        self.assertEqual(audits.count(), 2)
        party_audit = audits.get(field_name='Party Name')
        self.assertEqual(party_audit.old_value, 'MURUGAN DIESEL SERVICE')
        self.assertEqual(party_audit.new_value, 'MURUGAN FUEL LOGISTICS')
        self.assertEqual(party_audit.changed_by, self.naveen)

    def test_03_verification_workflow(self):
        """Test verification action approves invoice and assigns verified_by."""
        c = Client()
        c.force_login(self.naveen)

        url = reverse('invoices:verification_workspace', kwargs={'submission_id': self.sub1.submission_id})
        res = c.post(url, {
            'action': 'verify',
            'party_name': self.sub1.party_name,
            'party_gstin': self.sub1.party_gstin,
            'invoice_number': self.sub1.invoice_number,
            'expense_ledger': 'Diesel Expenses',
            'taxable_amount': '10000.00',
            'cgst_amount': '900.00',
            'sgst_amount': '900.00',
            'igst_amount': '0.00',
            'total_amount': '11800.00',
            'audit_reason': 'Verified against physical seal'
        })
        self.assertEqual(res.status_code, 302)

        self.sub1.refresh_from_db()
        self.assertEqual(self.sub1.status, InvoiceSubmission.STATUS_VERIFIED)
        self.assertEqual(self.sub1.verified_by, self.naveen)
        self.assertIsNotNone(self.sub1.verified_at)
        self.assertFalse(self.sub1.requires_manual_review)

    def test_04_gstr2a_reconciliation(self):
        """Test GSTR-2A comparison support identifies matches and discrepancies."""
        recon = compare_with_gstr2a()
        self.assertGreaterEqual(recon['total_invoices'], 2)
        self.assertIn('matched', recon)
        self.assertIn('tax_discrepancies', recon)

    # ==========================================================================
    # PHASE 6 TESTS: Excel-to-Tally Automation & Ledger Mappings
    # ==========================================================================

    def test_05_tally_structured_excel_export(self):
        """Test generation of structured Excel workbook for Tally accounting."""
        qs = InvoiceSubmission.objects.all()
        excel_buffer = generate_tally_structured_excel(qs, user=self.naveen)
        self.assertIsNotNone(excel_buffer)

        wb = openpyxl.load_workbook(excel_buffer)
        ws = wb.active
        self.assertEqual(ws.title, "Tally Purchase Vouchers")
        # Check header row (row 4)
        self.assertEqual(ws.cell(row=4, column=1).value, "Voucher Date")
        self.assertEqual(ws.cell(row=4, column=3).value, "Supplier / Party Name")
        self.assertEqual(ws.cell(row=4, column=8).value, "Purchase Expense Ledger")

        # Check export audit log was created
        log = ExportAuditLog.objects.filter(export_type='TALLY_EXCEL').first()
        self.assertIsNotNone(log)
        self.assertEqual(log.exported_by, self.naveen)
        self.assertEqual(log.record_count, 2)

    def test_06_tally_ledger_mapping(self):
        """Test ledger mappings CRUD."""
        m, created = TallyLedgerMapping.objects.get_or_create(
            category_name="Testing Category",
            defaults={
                'tally_expense_ledger': 'Custom Test Ledger',
                'cgst_ledger': 'Input CGST',
                'sgst_ledger': 'Input SGST',
                'igst_ledger': 'Input IGST',
            }
        )
        self.assertTrue(TallyLedgerMapping.objects.filter(category_name="Testing Category").exists())

    # ==========================================================================
    # PHASE 7 TESTS: Executive Analytics & Downloadable Reports
    # ==========================================================================

    def test_07_executive_analytics_kpis(self):
        """Test live database calculation of financial KPIs."""
        kpis = get_executive_kpis()
        self.assertEqual(kpis['total_invoices'], 2)
        self.assertEqual(kpis['total_value'], Decimal('17700.00'))
        self.assertEqual(kpis['total_taxable'], Decimal('15000.00'))
        self.assertEqual(kpis['verified_count'], 1)
        self.assertEqual(kpis['tally_done_count'], 1)
        self.assertEqual(kpis['tally_pending_count'], 1)
        self.assertEqual(kpis['tally_rate'], 50.0)

    def test_08_monthly_trends_and_suppliers(self):
        """Test monthly spend trends and top supplier aggregations."""
        trends = get_monthly_trends()
        self.assertIsInstance(trends, list)
        self.assertGreaterEqual(len(trends), 1)

        suppliers = get_top_suppliers(limit=5)
        self.assertIsInstance(suppliers, list)
        self.assertGreaterEqual(len(suppliers), 1)

    def test_09_downloadable_executive_reports(self):
        """Test generation of executive reports for Management."""
        for r_type in ['monthly_summary', 'supplier_summary', 'compliance_audit']:
            buf = generate_executive_excel_report(report_type=r_type, user=self.management)
            self.assertIsNotNone(buf)
            wb = openpyxl.load_workbook(buf)
            self.assertGreater(len(wb.sheetnames), 0)

    # ==========================================================================
    # RBAC & SECURITY TESTS
    # ==========================================================================

    def test_10_financial_safety_and_rbac(self):
        """Test strict role-based isolation."""
        c = Client()

        # 1. Employee cannot view Analytics Dashboard
        c.force_login(self.employee)
        res_emp = c.get(reverse('invoices:analytics_dashboard'))
        self.assertEqual(res_emp.status_code, 403)

        # 2. Management cannot perform POST modifications on Verification Desk
        c.force_login(self.management)
        res_mgmt = c.post(
            reverse('invoices:verification_workspace', kwargs={'submission_id': self.sub1.submission_id}),
            {'action': 'verify'}
        )
        self.assertEqual(res_mgmt.status_code, 403)

        # 3. Naveen can view and operate Verification Workspace
        c.force_login(self.naveen)
        res_nav = c.get(reverse('invoices:verification_workspace', kwargs={'submission_id': self.sub1.submission_id}))
        self.assertEqual(res_nav.status_code, 200)

    # ==========================================================================
    # DUPLICATE UPLOAD PERMISSIBILITY TEST
    # ==========================================================================

    def test_11_duplicate_upload_permissibility(self):
        """
        Verify that duplicate uploads are permitted, receive their own tracking IDs,
        retain independent records, and show non-blocking informational alerts.
        """
        c = Client()
        c.force_login(self.employee)

        # Upload first file
        f1 = SimpleUploadedFile("bill_copy.jpg", b"\xFF\xD8\xFF\xE0\x00\x10JFIF" + b"X" * 200, content_type="image/jpeg")
        res1 = c.post(reverse('invoices:upload'), {'files': [f1], 'remarks': 'Initial upload'})
        self.assertEqual(res1.status_code, 302)
        sub_id_1 = res1.url.split('/')[-2]

        # Upload identical file second time
        f2 = SimpleUploadedFile("bill_copy.jpg", b"\xFF\xD8\xFF\xE0\x00\x10JFIF" + b"X" * 200, content_type="image/jpeg")
        res2 = c.post(reverse('invoices:upload'), {'files': [f2], 'remarks': 'Duplicate copy uploaded'})
        self.assertEqual(res2.status_code, 302)
        sub_id_2 = res2.url.split('/')[-2]

        # Both must exist and have unique tracking IDs
        self.assertNotEqual(sub_id_1, sub_id_2)
        s1 = InvoiceSubmission.objects.get(submission_id=sub_id_1)
        s2 = InvoiceSubmission.objects.get(submission_id=sub_id_2)
        self.assertIsNotNone(s1)
        self.assertIsNotNone(s2)

        # Second submission flags informational warning only without blocking
        self.assertTrue(s2.is_duplicate_suspected)
        self.assertEqual(s2.status, InvoiceSubmission.STATUS_SUBMITTED)

