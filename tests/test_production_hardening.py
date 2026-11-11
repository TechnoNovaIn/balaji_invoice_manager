from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
import pymupdf as fitz
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Department, UserRole
from apps.backup.services import _resolve_pg_binary
from apps.invoices.models import (
    InvoiceSubmission,
    PurchaseAuditTrail,
    UploadedDocument,
    VendorMaster,
)
from apps.invoices.purchase_service import compare_with_gstr2a, filter_purchase_register
from apps.invoices.tasks import (
    get_incomplete_documents,
    process_uploaded_document_task,
    recover_incomplete_documents,
)
from apps.invoices.vendor_directory import (
    VENDOR_DIRECTORY,
    find_vendor_by_gstin,
    find_vendor_by_name,
    get_all_vendor_names,
    register_or_update_vendor,
)

User = get_user_model()


def _make_pdf_bytes(text: str) -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 72), text)
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


class SecurityHardeningTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.dept, _ = Department.objects.get_or_create(code="OPS", defaults={"name": "Operations"})
        self.emp = User.objects.create_user(
            username="sec_emp",
            password="StrongPassword@2026",
            role=UserRole.EMPLOYEE,
            department=self.dept,
        )
        self.naveen = User.objects.create_user(
            username="naveen",
            password="NaveenStrongPass@2026",
            role=UserRole.NAVEEN,
            department=self.dept,
        )

    def test_login_page_has_no_plaintext_credentials(self):
        resp = self.client.get(reverse('accounts:login'))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Quick 1-Tap Login")
        self.assertNotContains(resp, "BalajiEmp@2026")
        self.assertNotContains(resp, "BalajiNaveen@2026")
        self.assertNotContains(resp, "BalajiAdmin@2026")
        self.assertNotContains(resp, "fillLogin")

    @override_settings(DEBUG=True)
    def test_weak_fallback_passwords_rejected_even_in_debug(self):
        for bad_pw in ("123456", "naveen", "naveen123", "balaji123"):
            self.assertIsNone(authenticate(username="naveen", password=bad_pw))
        self.assertEqual(
            authenticate(username="NAVEEN", password="NaveenStrongPass@2026"),
            self.naveen,
        )

    def test_logout_is_post_only(self):
        self.client.force_login(self.emp)
        logout_url = reverse('accounts:logout')
        get_resp = self.client.get(logout_url)
        self.assertEqual(get_resp.status_code, 405)
        self.assertIn('_auth_user_id', self.client.session)

        post_resp = self.client.post(logout_url)
        self.assertEqual(post_resp.status_code, 302)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_change_password_enforces_validators(self):
        self.client.force_login(self.emp)
        url = reverse('accounts:change_password')

        # Common / short / numeric passwords must be rejected
        for weak in ("1234567", "password", "12345678"):
            resp = self.client.post(url, {'new_password': weak, 'confirm_password': weak})
            self.assertEqual(resp.status_code, 200)
            self.emp.refresh_from_db()
            self.assertFalse(self.emp.check_password(weak))

        # Strong password accepted
        resp_ok = self.client.post(url, {
            'new_password': 'VeryStrongBalajiPass#2026',
            'confirm_password': 'VeryStrongBalajiPass#2026',
        })
        self.assertEqual(resp_ok.status_code, 302)
        self.emp.refresh_from_db()
        self.assertTrue(self.emp.check_password('VeryStrongBalajiPass#2026'))

    def test_reset_invoice_data_requires_yes_flag(self):
        InvoiceSubmission.objects.create(
            submission_id="BTI-202611-90001",
            employee=self.emp,
            party_name="KEEP SAFE",
        )
        with self.assertRaises(CommandError):
            call_command('reset_invoice_data')
        self.assertTrue(InvoiceSubmission.objects.filter(submission_id="BTI-202611-90001").exists())

    def test_health_check_hides_version_and_db_exceptions(self):
        ok_resp = self.client.get('/health/')
        self.assertEqual(ok_resp.status_code, 200)
        ok_data = ok_resp.json()
        self.assertEqual(ok_data['database'], 'CONNECTED')
        self.assertNotIn('db_version', ok_data)

        with patch('apps.core.views.connection.cursor', side_effect=RuntimeError("password authentication failed for postgres")):
            err_resp = self.client.get('/health/')
            self.assertEqual(err_resp.status_code, 503)
            err_data = err_resp.json()
            self.assertEqual(err_data, {'status': 'DOWN', 'database': 'ERROR'})
            self.assertNotIn('detail', err_data)

    @pytest.mark.no_http_test_overrides
    def test_production_settings_and_env_example_sync(self):
        self.assertFalse(settings.DEBUG)
        self.assertFalse(settings.SECRET_KEY.startswith('django-insecure-'))
        self.assertGreaterEqual(len(settings.SECRET_KEY), 50)
        self.assertNotIn('*', settings.ALLOWED_HOSTS)
        self.assertTrue(settings.SECURE_SSL_REDIRECT)
        self.assertTrue(settings.SESSION_COOKIE_SECURE)
        self.assertTrue(settings.CSRF_COOKIE_SECURE)
        self.assertGreaterEqual(settings.SECURE_HSTS_SECONDS, 31536000)

        env_example = Path(settings.BASE_DIR / '.env.example').read_text()
        for required_key in (
            'DJANGO_SECRET_KEY',
            'DJANGO_DEBUG',
            'DJANGO_ALLOWED_HOSTS',
            'DJANGO_ENABLE_SSL',
            'SECURE_HSTS_SECONDS',
            'BACKUP_STORAGE_PATH',
            'PG_BIN_DIR',
            'GEMINI_API_KEY',
            'GEMINI_MODEL',
        ):
            self.assertIn(required_key, env_example)

        pg_tool = _resolve_pg_binary("pg_dump")
        self.assertTrue(pg_tool.endswith("pg_dump.exe") or pg_tool.endswith("pg_dump"))


class DataIntegrityAndValidationTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.dept, _ = Department.objects.get_or_create(code="FLEET", defaults={"name": "Fleet"})
        self.emp = User.objects.create_user(
            username="int_emp",
            password="StrongPassword@2026",
            role=UserRole.EMPLOYEE,
            department=self.dept,
        )
        self.naveen = User.objects.create_user(
            username="int_naveen",
            password="StrongPassword@2026",
            role=UserRole.NAVEEN,
            department=self.dept,
        )

    def test_duplicate_gstin_fixed_and_vendormaster_persisted(self):
        # BASANNA JANARDHAN owns 29AVZPJ3961B1Z4 in directory & VendorMaster DB
        self.assertEqual(VENDOR_DIRECTORY['29AVZPJ3961B1Z4']['name'], 'BASANNA JANARDHAN')
        v_db = find_vendor_by_gstin('29AVZPJ3961B1Z4')
        self.assertIsNotNone(v_db)
        self.assertEqual(v_db['name'], 'BASANNA JANARDHAN')

        # DB-only VendorMaster record (with trade alias) must be matched everywhere
        VendorMaster.objects.create(
            name="SRI KRISHNA PETROLEUM AGENCIES",
            gstin="29ABCPK1111M1Z2",
            default_expense_ledger="Diesel & Lubricants",
            trade_aliases=["KRISHNA BUNK"],
            is_active=True,
            is_verified=True,
        )
        self.assertIn("SRI KRISHNA PETROLEUM AGENCIES", get_all_vendor_names())
        alias_hit = find_vendor_by_name("KRISHNA BUNK")
        self.assertIsNotNone(alias_hit)
        self.assertEqual(alias_hit['gstin'], "29ABCPK1111M1Z2")

        # compare_with_gstr2a must query VendorMaster DB, not only static dict
        InvoiceSubmission.objects.create(
            submission_id="BTI-202611-90002",
            employee=self.emp,
            party_name="SRI KRISHNA PETROLEUM AGENCIES",
            party_gstin="29ABCPK1111M1Z2",
            taxable_amount=Decimal("1000.00"),
            cgst_amount=Decimal("90.00"),
            sgst_amount=Decimal("90.00"),
            total_amount=Decimal("1180.00"),
        )
        recon = compare_with_gstr2a()
        matched_ids = [m['invoice'].submission_id for m in recon['matched']]
        self.assertIn("BTI-202611-90002", matched_ids)

    def test_register_or_update_vendor_persists_to_postgres(self):
        res = register_or_update_vendor(
            gstin="29NEWDB9999K1Z3",
            name="NEW PERSISTED TRANSPORT CO",
            ledger="Transportation & Freight Charges",
            user=self.naveen,
        )
        self.assertEqual(res['status'], 'CREATED')
        db_row = VendorMaster.objects.filter(gstin="29NEWDB9999K1Z3").first()
        self.assertIsNotNone(db_row)
        self.assertEqual(db_row.name, "NEW PERSISTED TRANSPORT CO")

    def test_task_saves_canonical_party_name_and_gstin_keys(self):
        pdf_bytes = _make_pdf_bytes(
            "TAX INVOICE\nJK TYRE & INDUSTRIES LIMITED\n"
            "GSTIN: 29AAACJ6716F1ZK\nInvoice No: JKT-777\nTotal Amount: 11800.00"
        )
        up = UploadedDocument.objects.create(
            uploader=self.emp,
            original_file=SimpleUploadedFile("jkt_task.pdf", pdf_bytes, "application/pdf"),
            file_name="jkt_task.pdf",
            file_sha256="sha_jkt_task_777",
            file_size_bytes=len(pdf_bytes),
            content_type="application/pdf",
            processing_status=UploadedDocument.STATUS_QUEUED,
        )
        subs = process_uploaded_document_task(up.id)
        self.assertEqual(len(subs), 1)
        sub = subs[0]
        self.assertEqual(sub.party_name, "JK TYRE & INDUSTRIES LIMITED")
        self.assertEqual(sub.party_gstin, "29AAACJ6716F1ZK")
        self.assertFalse(sub.is_ambiguous_vendor)
        self.assertFalse(sub.requires_manual_review)

    def test_approval_cannot_bypass_ambiguity_or_gstin_mismatch(self):
        self.client.force_login(self.naveen)

        # Case E mismatch blocked even with confirm_manual_review
        mismatch_sub = InvoiceSubmission.objects.create(
            submission_id="BTI-202611-90003",
            employee=self.emp,
            party_name="JK TYRE & INDUSTRIES LIMITED",
            party_gstin="29WRONG9999Z1Z1",
            total_amount=Decimal("5000.00"),
            is_ambiguous_vendor=True,
            requires_manual_review=True,
        )
        resp_mismatch = self.client.post(
            reverse('invoices:verification_workspace', kwargs={'submission_id': mismatch_sub.submission_id}),
            {
                'action': 'verify',
                'party_name': mismatch_sub.party_name,
                'party_gstin': mismatch_sub.party_gstin,
                'invoice_number': 'INV-MISMATCH',
                'expense_ledger': 'Tyre & Tube Purchase',
                'taxable_amount': '5000.00',
                'total_amount': '5000.00',
                'confirm_manual_review': 'true',
            },
        )
        self.assertEqual(resp_mismatch.status_code, 302)
        mismatch_sub.refresh_from_db()
        self.assertEqual(mismatch_sub.status, InvoiceSubmission.STATUS_SUBMITTED)

        # Ambiguous vendor without GSTIN blocked unless confirm_manual_review checked
        ambig_sub = InvoiceSubmission.objects.create(
            submission_id="BTI-202611-90004",
            employee=self.emp,
            party_name="Roadside Dhaba Repairs",
            party_gstin="",
            total_amount=Decimal("850.00"),
            is_ambiguous_vendor=True,
            requires_manual_review=True,
        )
        url_verify = reverse('invoices:verification_workspace', kwargs={'submission_id': ambig_sub.submission_id})
        self.client.post(url_verify, {
            'action': 'verify',
            'party_name': 'Roadside Dhaba Repairs',
            'party_gstin': '',
            'invoice_number': 'SLIP-1',
            'expense_ledger': 'Labour Charges / Vehicle Service',
            'taxable_amount': '850.00',
            'total_amount': '850.00',
        })
        ambig_sub.refresh_from_db()
        self.assertEqual(ambig_sub.status, InvoiceSubmission.STATUS_SUBMITTED)

        # With explicit confirm_manual_review, approval succeeds and writes PurchaseAuditTrail
        self.client.post(url_verify, {
            'action': 'verify',
            'party_name': 'Roadside Dhaba Repairs',
            'party_gstin': '',
            'invoice_number': 'SLIP-1',
            'expense_ledger': 'Labour Charges / Vehicle Service',
            'taxable_amount': '850.00',
            'total_amount': '850.00',
            'confirm_manual_review': 'true',
            'audit_reason': 'Confirmed petty cash bill with driver',
        })
        ambig_sub.refresh_from_db()
        self.assertEqual(ambig_sub.status, InvoiceSubmission.STATUS_VERIFIED)
        self.assertTrue(
            PurchaseAuditTrail.objects.filter(submission=ambig_sub, field_name='Verification Approval').exists()
        )

    def test_tally_workspace_parses_date_records_audit_and_validates_vendor(self):
        self.client.force_login(self.naveen)
        sub = InvoiceSubmission.objects.create(
            submission_id="BTI-202611-90005",
            employee=self.emp,
            party_name="",
            total_amount=Decimal("0.00"),
        )
        resp = self.client.post(
            reverse('invoices:tally_workspace', kwargs={'submission_id': sub.submission_id}),
            {
                'party_name': 'JK TYRE & INDUSTRIES LIMITED',
                'party_gstin': '29AAACJ6716F1ZK',
                'invoice_number': 'JK-2026-11',
                'invoice_date': '2026-11-05',
                'expense_ledger': 'Tyre & Tube Purchase',
                'taxable_amount': '1000.00',
                'cgst_amount': '90.00',
                'sgst_amount': '90.00',
                'igst_amount': '0.00',
                'total_amount': '1180.00',
            },
        )
        self.assertEqual(resp.status_code, 302)
        sub.refresh_from_db()
        self.assertIsInstance(sub.invoice_date, date)
        self.assertEqual(sub.invoice_date, date(2026, 11, 5))
        self.assertFalse(sub.is_ambiguous_vendor)
        self.assertFalse(sub.requires_manual_review)
        self.assertTrue(PurchaseAuditTrail.objects.filter(submission=sub).exists())

    def test_purchase_register_ignores_malformed_date_filters(self):
        qs = filter_purchase_register({'from_date': 'not-a-date', 'to_date': '99-99-9999'})
        self.assertIsNotNone(list(qs))


class BackgroundReliabilityTests(TestCase):
    def setUp(self):
        self.dept, _ = Department.objects.get_or_create(code="OPS", defaults={"name": "Operations"})
        self.emp = User.objects.create_user(
            username="bg_emp",
            password="StrongPassword@2026",
            role=UserRole.EMPLOYEE,
            department=self.dept,
        )

    def test_duplicate_processing_prevented_and_failure_recorded(self):
        pdf_bytes = _make_pdf_bytes(
            "TAX INVOICE\nJK TYRE & INDUSTRIES LIMITED\n"
            "GSTIN: 29AAACJ6716F1ZK\nInvoice No: DUP-1\nTotal Amount: 1000.00"
        )
        up = UploadedDocument.objects.create(
            uploader=self.emp,
            original_file=SimpleUploadedFile("dup_guard.pdf", pdf_bytes, "application/pdf"),
            file_name="dup_guard.pdf",
            file_sha256="sha_dup_guard",
            file_size_bytes=len(pdf_bytes),
            content_type="application/pdf",
            processing_status=UploadedDocument.STATUS_QUEUED,
        )
        subs1 = process_uploaded_document_task(up.id)
        subs2 = process_uploaded_document_task(up.id)
        self.assertEqual(len(subs1), 1)
        self.assertEqual(len(subs2), 1)
        self.assertEqual(InvoiceSubmission.objects.filter(source_document=up).count(), 1)

        # Corrupt/missing file must mark STATUS_FAILED, never COMPLETED
        bad_up = UploadedDocument.objects.create(
            uploader=self.emp,
            original_file=SimpleUploadedFile("corrupt.pdf", b"%PDF-1.4 corrupt", "application/pdf"),
            file_name="corrupt.pdf",
            file_sha256="sha_corrupt_doc",
            file_size_bytes=16,
            content_type="application/pdf",
            processing_status=UploadedDocument.STATUS_QUEUED,
        )
        with self.assertRaises(Exception):
            process_uploaded_document_task(bad_up.id)
        bad_up.refresh_from_db()
        self.assertEqual(bad_up.processing_status, UploadedDocument.STATUS_FAILED)
        self.assertTrue(bool(bad_up.error_message))

    def test_crash_recovery_identifies_and_recovers_orphaned_documents(self):
        pdf_bytes = _make_pdf_bytes(
            "TAX INVOICE\nJK TYRE & INDUSTRIES LIMITED\n"
            "GSTIN: 29AAACJ6716F1ZK\nInvoice No: REC-1\nTotal Amount: 2500.00"
        )
        stuck = UploadedDocument.objects.create(
            uploader=self.emp,
            original_file=SimpleUploadedFile("stuck_after_crash.pdf", pdf_bytes, "application/pdf"),
            file_name="stuck_after_crash.pdf",
            file_sha256="sha_stuck_doc",
            file_size_bytes=len(pdf_bytes),
            content_type="application/pdf",
            processing_status=UploadedDocument.STATUS_PROCESSING,
            current_stage="Interrupted mid-OCR",
            progress_percent=50,
        )
        # Age updated_at for stale check
        UploadedDocument.objects.filter(pk=stuck.pk).update(updated_at=timezone.now() - timedelta(minutes=5))

        incomplete = list(get_incomplete_documents(stale_minutes=1))
        self.assertEqual([d.id for d in incomplete], [stuck.id])

        # Requeue safely without losing file, then run task with force_recover=True
        summary = recover_incomplete_documents(stale_minutes=1, requeue_only=True)
        self.assertEqual(summary['recovered_ids'], [stuck.id])
        stuck.refresh_from_db()
        self.assertEqual(stuck.processing_status, UploadedDocument.STATUS_QUEUED)
        self.assertTrue(stuck.original_file.storage.exists(stuck.original_file.name))

        subs = process_uploaded_document_task(stuck.id, force_recover=True)
        self.assertEqual(len(subs), 1)
        stuck.refresh_from_db()
        self.assertEqual(stuck.processing_status, UploadedDocument.STATUS_DETECTION_COMPLETED)

