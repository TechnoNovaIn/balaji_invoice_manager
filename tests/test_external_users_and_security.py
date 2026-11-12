import io
import json
import uuid
from decimal import Decimal

import openpyxl
import pytest
from PIL import Image
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import RequestFactory, override_settings
from django.urls import reverse

from apps.accounts.forms import CustomUserCreationForm
from apps.accounts.models import AccountStatus, AuditLog, User, UserRole
from apps.accounts.security import get_client_ip, invalidate_user_sessions
from apps.invoices.export_security import safe_xlsx_value, sanitize_spreadsheet_text
from apps.invoices.models import InvoiceDocument, InvoiceSubmission, UploadedDocument
from apps.invoices.services import MAX_FILE_SIZE_BYTES, sanitize_filename, validate_uploaded_files


pytestmark = pytest.mark.django_db


def _make_real_png_bytes(width=120, height=120, color=(20, 90, 180)):
    buf = io.BytesIO()
    img = Image.new('RGB', (width, height), color=color)
    img.save(buf, format='PNG')
    return buf.getvalue()


def _make_user(username, role, **extra):
    password = extra.pop('password', 'StrongPass#2026!')
    user = User.objects.create_user(username=username, password=password, role=role, **extra)
    return user


def _make_submission(owner, party_name='Test Vendor', inv_no='INV-001', status=InvoiceSubmission.STATUS_SUBMITTED):
    png_bytes = _make_real_png_bytes()
    upload = SimpleUploadedFile(f"{inv_no}.png", png_bytes, content_type='image/png')
    source_doc = UploadedDocument.objects.create(
        uploader=owner,
        original_file=upload,
        file_name=f"{inv_no}.png",
        file_sha256='b' * 64,
        file_size_bytes=len(png_bytes),
        content_type='image/png',
        processing_status=UploadedDocument.STATUS_COMPLETED,
    )
    sub = InvoiceSubmission.objects.create(
        submission_id=f"SUB-{uuid.uuid4().hex[:8].upper()}",
        employee=owner,
        source_document=source_doc,
        party_name=party_name,
        invoice_number=inv_no,
        total_amount=Decimal('1500.00'),
        status=status,
    )
    doc = InvoiceDocument.objects.create(
        submission=sub,
        source_document=source_doc,
        original_file=SimpleUploadedFile(f"doc-{inv_no}.png", png_bytes, content_type='image/png'),
        original_filename=f"{inv_no}.png",
        file_sha256='a' * 64,
        file_size_bytes=len(png_bytes),
        content_type='image/png',
    )
    return sub, source_doc, doc


class TestRolesAndExternalUserPermissions:
    def test_naveen_combined_admin_and_accounts_access(self, client):
        naveen = _make_user(
            'naveen_ops',
            UserRole.NAVEEN,
            has_admin_access=True,
            has_accounts_access=True,
            is_staff=True,
        )
        ext_user = _make_user('ext_sub_1', UserRole.EXTERNAL_SUBMITTER)
        sub, _, _ = _make_submission(ext_user, party_name='Logistics Vendor', inv_no='NV-100')

        client.force_login(naveen)

        # Dashboard router sends Naveen to Accounts Workbench
        resp_root = client.get(reverse('core:dashboard_router'))
        assert resp_root.status_code == 302
        assert resp_root.url == reverse('core:naveen_dashboard')

        # Combined ADMIN + ACCOUNTS access both work
        for url in [
            reverse('core:naveen_dashboard'),
            reverse('core:admin_dashboard'),
            reverse('accounts:user_list'),
            reverse('accounts:user_create'),
            reverse('accounts:audit_logs'),
            reverse('backup:dashboard'),
            reverse('invoices:verification_workspace', kwargs={'submission_id': sub.submission_id}),
        ]:
            resp = client.get(url)
            assert resp.status_code == 200, f"Naveen failed accessing {url} ({resp.status_code})"

        # Reject invoice workflow works and records AuditLog
        resp_reject = client.post(
            reverse('invoices:naveen_reject', kwargs={'submission_id': sub.submission_id}),
            {'rejection_reason': 'Missing GST breakup'},
        )
        assert resp_reject.status_code == 302
        sub.refresh_from_db()
        assert sub.status == InvoiceSubmission.STATUS_REJECTED
        assert sub.return_reason == 'Missing GST breakup'
        assert AuditLog.objects.filter(action='INVOICE_REJECTED', user=naveen).exists()

    def test_external_submitter_blocked_from_admin_accounts_and_backups(self, client):
        ext_a = _make_user('ext_a', UserRole.EXTERNAL_SUBMITTER)
        ext_b = _make_user('ext_b', UserRole.EXTERNAL_SUBMITTER)
        sub, _, _ = _make_submission(ext_b, inv_no='EXT-002')

        client.force_login(ext_a)

        forbidden_gets = [
            reverse('core:naveen_dashboard'),
            reverse('core:admin_dashboard'),
            reverse('core:management_dashboard'),
            reverse('accounts:user_list'),
            reverse('accounts:user_create'),
            reverse('accounts:user_edit', kwargs={'user_id': ext_b.pk}),
            reverse('accounts:user_reset_password', kwargs={'user_id': ext_b.pk}),
            reverse('accounts:user_history', kwargs={'user_id': ext_b.pk}),
            reverse('accounts:audit_logs'),
            reverse('backup:dashboard'),
            reverse('invoices:verification_workspace', kwargs={'submission_id': sub.submission_id}),
            reverse('invoices:export_tally_excel'),
            reverse('invoices:export_gst_csv'),
            reverse('invoices:export_gst_portal_excel'),
            reverse('invoices:export_tally_xml'),
            reverse('invoices:analytics_dashboard'),
        ]
        for url in forbidden_gets:
            resp = client.get(url)
            assert resp.status_code == 403, f"External user was not blocked on GET {url}: {resp.status_code}"

        for post_url, payload in [
            (reverse('invoices:naveen_verify', kwargs={'submission_id': sub.submission_id}), {}),
            (reverse('invoices:naveen_reject', kwargs={'submission_id': sub.submission_id}), {'rejection_reason': 'bad'}),
            (reverse('invoices:naveen_return', kwargs={'submission_id': sub.submission_id}), {'return_reason': 'bad'}),
            (reverse('accounts:user_toggle_active', kwargs={'user_id': ext_b.pk}), {}),
            (reverse('accounts:user_suspend', kwargs={'user_id': ext_b.pk}), {'action': 'suspend'}),
            (reverse('backup:trigger_backup'), {}),
        ]:
            resp = client.post(post_url, payload)
            assert resp.status_code == 403, f"External user was not blocked on POST {post_url}: {resp.status_code}"

    def test_no_public_self_registration_and_creation_form_restricts_roles(self, client):
        for reg_path in ['/accounts/register/', '/accounts/signup/', '/register/', '/signup/']:
            resp = client.get(reg_path)
            assert resp.status_code == 404

        form = CustomUserCreationForm(
            data={
                'username': 'ext_driver_99',
                'first_name': 'Driver',
                'last_name': 'NinetyNine',
                'email': 'driver99@example.com',
                'phone_number': '9999999999',
                'role': UserRole.EXTERNAL_SUBMITTER,
                'password1': 'UniquePassword#2026!',
                'password2': 'UniquePassword#2026!',
                'can_upload_invoices': True,
                'can_resubmit_invoices': True,
                'must_change_password': True,
            }
        )
        assert form.is_valid(), form.errors
        created = form.save()
        assert created.role == UserRole.EXTERNAL_SUBMITTER
        assert created.has_admin_access is False
        assert created.has_accounts_access is False
        assert created.is_staff is False
        assert created.is_superuser is False

        # Attempting to inject NAVEEN or ADMIN role via form must fail validation
        bad_form = CustomUserCreationForm(
            data={
                'username': 'evil_admin',
                'role': UserRole.ADMIN,
                'password1': 'UniquePassword#2026!',
                'password2': 'UniquePassword#2026!',
            }
        )
        assert not bad_form.is_valid()
        assert 'role' in bad_form.errors

    def test_admin_external_user_lifecycle_audit_and_history(self, client):
        admin = _make_user('sole_admin', UserRole.NAVEEN, has_admin_access=True, has_accounts_access=True, is_staff=True)
        client.force_login(admin)

        # Create user
        resp_create = client.post(
            reverse('accounts:user_create'),
            {
                'username': 'vendor_john',
                'first_name': 'John',
                'last_name': 'Vendor',
                'email': 'john@vendor.com',
                'phone_number': '9876543210',
                'role': UserRole.EXTERNAL_SUBMITTER,
                'password1': 'TempUserPass#2026!',
                'password2': 'TempUserPass#2026!',
                'can_upload_invoices': 'on',
                'can_resubmit_invoices': 'on',
                'must_change_password': 'on',
            },
        )
        assert resp_create.status_code == 302
        target = User.objects.get(username='vendor_john')
        assert AuditLog.objects.filter(action='ADMIN_CREATE_USER', user=admin).exists()

        # Edit permissions
        resp_edit = client.post(
            reverse('accounts:user_edit', kwargs={'user_id': target.pk}),
            {
                'first_name': 'Johnny',
                'last_name': 'Vendor',
                'email': 'john@vendor.com',
                'phone_number': '9876543210',
                'account_status': AccountStatus.ACTIVE,
                'suspended_reason': '',
                'is_active': 'on',
                'can_upload_invoices': 'on',
            },
        )
        assert resp_edit.status_code == 302
        target.refresh_from_db()
        assert target.can_resubmit_invoices is False
        assert AuditLog.objects.filter(action='ADMIN_PERMISSION_CHANGE', user=admin).exists()

        # Suspend & Unsuspend
        resp_susp = client.post(
            reverse('accounts:user_suspend', kwargs={'user_id': target.pk}),
            {'action': 'suspend', 'reason': 'Audit check'},
        )
        assert resp_susp.status_code == 302
        target.refresh_from_db()
        assert target.account_status == AccountStatus.SUSPENDED
        assert AuditLog.objects.filter(action='ADMIN_USER_SUSPENDED', user=admin).exists()

        resp_unsusp = client.post(
            reverse('accounts:user_suspend', kwargs={'user_id': target.pk}),
            {'action': 'unsuspend'},
        )
        assert resp_unsusp.status_code == 302
        target.refresh_from_db()
        assert target.account_status == AccountStatus.ACTIVE
        assert AuditLog.objects.filter(action='ADMIN_USER_UNSUSPENDED', user=admin).exists()

        # Reset password
        resp_reset = client.post(
            reverse('accounts:user_reset_password', kwargs={'user_id': target.pk}),
            {
                'new_password': 'NewVendorPass#2026!',
                'confirm_password': 'NewVendorPass#2026!',
                'must_change_password': 'on',
            },
        )
        assert resp_reset.status_code == 302
        target.refresh_from_db()
        assert target.check_password('NewVendorPass#2026!')
        assert target.must_change_password is True
        assert AuditLog.objects.filter(action='ADMIN_PASSWORD_RESET', user=admin).exists()

        # History & audit log pages render cleanly
        assert client.get(reverse('accounts:user_history', kwargs={'user_id': target.pk})).status_code == 200
        assert client.get(reverse('accounts:audit_logs')).status_code == 200


class TestOwnershipIsolation:
    def test_external_user_can_access_own_invoice_and_document(self, client):
        user_a = _make_user('ext_owner_a', UserRole.EXTERNAL_SUBMITTER)
        sub_a, src_a, doc_a = _make_submission(
            user_a, party_name='Owner A Supply', inv_no='OWN-A1', status=InvoiceSubmission.STATUS_RETURNED
        )

        client.force_login(user_a)

        assert client.get(reverse('core:employee_dashboard')).status_code == 200
        assert client.get(reverse('invoices:my_submissions')).status_code == 200
        assert client.get(reverse('invoices:submission_detail', kwargs={'submission_id': sub_a.submission_id})).status_code == 200
        assert client.get(reverse('invoices:upload_progress', kwargs={'document_id': src_a.pk})).status_code == 200
        assert client.get(reverse('invoices:document_status_api', kwargs={'document_id': src_a.pk})).status_code == 200
        assert client.get(reverse('invoices:resubmit', kwargs={'submission_id': sub_a.submission_id})).status_code == 200

        serve_resp = client.get(reverse('invoices:serve_document', kwargs={'doc_id': doc_a.pk}))
        assert serve_resp.status_code == 200
        assert serve_resp['X-Content-Type-Options'] == 'nosniff'
        assert 'no-store' in serve_resp['Cache-Control']
        assert serve_resp['X-Frame-Options'] == 'SAMEORIGIN'
        assert "script-src 'none'" in serve_resp['Content-Security-Policy']

        dl_resp = client.get(reverse('invoices:download_source_document', kwargs={'doc_id': src_a.pk}))
        assert dl_resp.status_code == 200
        assert dl_resp['X-Content-Type-Options'] == 'nosniff'

    def test_external_user_cannot_access_other_users_invoices_or_documents(self, client):
        user_a = _make_user('ext_iso_a', UserRole.EXTERNAL_SUBMITTER)
        user_b = _make_user('ext_iso_b', UserRole.EXTERNAL_SUBMITTER)
        sub_a, _, _ = _make_submission(user_a, party_name='Secret Vendor A', inv_no='ISO-A1')
        sub_b, src_b, doc_b = _make_submission(user_b, party_name='Secret Vendor B', inv_no='ISO-B1')

        client.force_login(user_a)

        # my_submissions and employee_dashboard only include A, never B
        my_resp = client.get(reverse('invoices:my_submissions'))
        assert my_resp.status_code == 200
        body = my_resp.content.decode('utf-8')
        assert sub_a.submission_id in body
        assert sub_b.submission_id not in body
        assert 'Secret Vendor B' not in body

        # Direct access attempts on User B's resources must return 403
        for url in [
            reverse('invoices:submission_detail', kwargs={'submission_id': sub_b.submission_id}),
            reverse('invoices:submission_success', kwargs={'submission_id': sub_b.submission_id}),
            reverse('invoices:upload_progress', kwargs={'document_id': src_b.pk}),
            reverse('invoices:document_status_api', kwargs={'document_id': src_b.pk}),
            reverse('invoices:serve_document', kwargs={'doc_id': doc_b.pk}),
            reverse('invoices:download_source_document', kwargs={'doc_id': src_b.pk}),
            reverse('invoices:resubmit', kwargs={'submission_id': sub_b.submission_id}),
        ]:
            resp = client.get(url)
            assert resp.status_code == 403, f"IDOR exposure on {url}: got {resp.status_code}"

    def test_naveen_can_access_all_external_submissions_and_documents(self, client):
        user_a = _make_user('ext_view_a', UserRole.EXTERNAL_SUBMITTER)
        user_b = _make_user('ext_view_b', UserRole.EXTERNAL_SUBMITTER)
        naveen = _make_user('naveen_all', UserRole.NAVEEN, has_admin_access=True, has_accounts_access=True)

        sub_a, src_a, doc_a = _make_submission(user_a, inv_no='ALL-A')
        sub_b, src_b, doc_b = _make_submission(user_b, inv_no='ALL-B')

        client.force_login(naveen)

        for url in [
            reverse('invoices:submission_detail', kwargs={'submission_id': sub_a.submission_id}),
            reverse('invoices:submission_detail', kwargs={'submission_id': sub_b.submission_id}),
            reverse('invoices:serve_document', kwargs={'doc_id': doc_a.pk}),
            reverse('invoices:serve_document', kwargs={'doc_id': doc_b.pk}),
            reverse('invoices:download_source_document', kwargs={'doc_id': src_a.pk}),
            reverse('invoices:download_source_document', kwargs={'doc_id': src_b.pk}),
        ]:
            assert client.get(url).status_code == 200


class TestSecurityHardening:
    def test_login_bruteforce_rate_limit_cooldown_and_audit(self, client):
        cache.clear()
        _make_user('target_ext', UserRole.EXTERNAL_SUBMITTER, password='CorrectPassword#2026!')

        login_url = reverse('accounts:login')
        with override_settings(LOGIN_MAX_ATTEMPTS=3, LOGIN_ATTEMPT_WINDOW_SECONDS=300, LOGIN_COOLDOWN_SECONDS=120):
            for _ in range(2):
                r = client.post(login_url, {'username': 'target_ext', 'password': 'WrongPassword!'})
                assert r.status_code == 200

            # 3rd failed attempt triggers cooldown (HTTP 429)
            r3 = client.post(login_url, {'username': 'target_ext', 'password': 'WrongPassword!'})
            assert r3.status_code == 429

            # Subsequent attempt even with valid password remains throttled with HTTP 429
            blocked = client.post(login_url, {'username': 'target_ext', 'password': 'CorrectPassword#2026!'})
            assert blocked.status_code == 429
            assert AuditLog.objects.filter(action='USER_LOGIN_FAILED').count() >= 3
            assert AuditLog.objects.filter(action='USER_LOGIN_RATE_LIMITED').exists()
        cache.clear()

    def test_client_ip_spoofing_protection(self):
        rf = RequestFactory()
        req = rf.get(
            '/',
            REMOTE_ADDR='10.0.0.5',
            HTTP_X_FORWARDED_FOR='1.2.3.4, 203.0.113.10, 198.51.100.42',
        )
        with override_settings(TRUSTED_PROXY_COUNT=0):
            assert get_client_ip(req) == '10.0.0.5'

        with override_settings(TRUSTED_PROXY_COUNT=1):
            assert get_client_ip(req) == '198.51.100.42'

        with override_settings(TRUSTED_PROXY_COUNT=2):
            assert get_client_ip(req) == '203.0.113.10'

    def test_malicious_filename_sanitization(self):
        cleaned = sanitize_filename('../../<script>alert(1)</script>bad..invoice.JPG')
        assert '/' not in cleaned
        assert '\\' not in cleaned
        assert '<' not in cleaned
        assert '>' not in cleaned
        assert '..' not in cleaned
        assert cleaned.lower().endswith('.jpg')

    def test_csv_and_xlsx_formula_injection_escaping(self, client):
        # Unit checks
        assert sanitize_spreadsheet_text('=CMD|"/C calc"!A0') == '\'=CMD|"/C calc"!A0'
        assert sanitize_spreadsheet_text('+1+1') == "'+1+1"
        assert sanitize_spreadsheet_text('@SUM(1+1)') == "'@SUM(1+1)"
        assert sanitize_spreadsheet_text('-1+1') == "'-1+1"
        assert sanitize_spreadsheet_text('  =1+1') == "'  =1+1"
        assert sanitize_spreadsheet_text('-1250.50') == '-1250.50'
        assert safe_xlsx_value('=HYPERLINK("http://evil")') == '\'=HYPERLINK("http://evil")'
        assert safe_xlsx_value(Decimal('42.50')) == Decimal('42.50')

        # Integration check via GST CSV and Tally Excel export
        naveen = _make_user('naveen_exp', UserRole.NAVEEN, has_admin_access=True, has_accounts_access=True)
        sub, _, _ = _make_submission(
            naveen,
            party_name='=cmd|" /C calc"!A0',
            inv_no='+SUM(A1:A2)',
            status=InvoiceSubmission.STATUS_VERIFIED,
        )
        sub.gstin_status = 'VALID'
        sub.party_gstin = '29AABCU9603R1ZM'
        sub.taxable_value = Decimal('1000.00')
        sub.save()

        client.force_login(naveen)
        csv_resp = client.get(reverse('invoices:export_gst_csv'))
        assert csv_resp.status_code == 200
        csv_text = csv_resp.content.decode('utf-8')
        assert "'=cmd" in csv_text
        assert "'+SUM(A1:A2)" in csv_text

        xlsx_resp = client.get(reverse('invoices:export_tally_excel'))
        assert xlsx_resp.status_code == 200
        wb = openpyxl.load_workbook(io.BytesIO(xlsx_resp.content), data_only=False)
        ws = wb.active
        cell_values = [str(cell.value or '') for row in ws.iter_rows() for cell in row]
        assert any(v.startswith("'=cmd") for v in cell_values)
        # Ensure internal summary total formula (=SUM(...)) stays active and unescaped
        assert any(v.startswith('=SUM(') for v in cell_values)

    def test_upload_rejects_oversized_decompression_bomb_and_malformed_files(self):
        # Spoofed executable disguised as JPG
        fake_jpg = SimpleUploadedFile('evil.jpg', b'MZ\x90\x00not_a_real_jpeg' * 40, content_type='image/jpeg')
        with pytest.raises(ValidationError):
            validate_uploaded_files([fake_jpg])

        # Corrupt / malformed real-size PNG (> 512 bytes)
        corrupt_png = SimpleUploadedFile('broken.png', b'\x89PNG\r\n\x1a\n' + b'\x00' * 700, content_type='image/png')
        with pytest.raises(ValidationError):
            validate_uploaded_files([corrupt_png])

        # Oversized dimensions (> 12000px) - triggers dimension/bomb protection cheaply via huge PNG header or oversized dim
        huge_buf = io.BytesIO()
        Image.new('1', (12500, 100)).save(huge_buf, format='PNG')
        huge_png = SimpleUploadedFile('bomb.png', huge_buf.getvalue(), content_type='image/png')
        with pytest.raises(ValidationError):
            validate_uploaded_files([huge_png])

        # Oversized byte size (> MAX_FILE_SIZE_BYTES = 100MB)
        too_big = SimpleUploadedFile('huge.pdf', b'%PDF-1.4\n%EOF\n', content_type='application/pdf')
        too_big.size = MAX_FILE_SIZE_BYTES + 1024
        with pytest.raises(ValidationError):
            validate_uploaded_files([too_big])

    def test_deactivated_and_suspended_accounts_blocked_and_sessions_revoked(self, client):
        cache.clear()
        ext = _make_user('ext_revoke', UserRole.EXTERNAL_SUBMITTER, password='ValidPassword#2026!')
        login_url = reverse('accounts:login')

        # Active login succeeds
        client.login(username='ext_revoke', password='ValidPassword#2026!')
        assert client.get(reverse('core:employee_dashboard')).status_code == 200

        # Suspend mid-session & invalidate
        ext.account_status = AccountStatus.SUSPENDED
        ext.save(update_fields=['account_status'])
        invalidate_user_sessions(ext)

        # Session is no longer authorized -> redirected to login
        mid_resp = client.get(reverse('core:employee_dashboard'))
        assert mid_resp.status_code == 302
        assert '/accounts/login/' in mid_resp.url

        # Subsequent login attempt blocked
        denied_resp = client.post(login_url, {'username': 'ext_revoke', 'password': 'ValidPassword#2026!'})
        assert denied_resp.status_code == 200
        assert not denied_resp.wsgi_request.user.is_authenticated

        # Deactivated user also blocked
        ext.account_status = AccountStatus.ACTIVE
        ext.is_active = False
        ext.save(update_fields=['account_status', 'is_active'])
        denied_deact = client.post(login_url, {'username': 'ext_revoke', 'password': 'ValidPassword#2026!'})
        assert denied_deact.status_code == 200
        assert not denied_deact.wsgi_request.user.is_authenticated

    def test_role_based_session_expiry_durations(self, client):
        cache.clear()
        naveen = _make_user('naveen_sess', UserRole.NAVEEN, password='ValidPassword#2026!', has_admin_access=True, has_accounts_access=True)
        ext = _make_user('ext_sess', UserRole.EXTERNAL_SUBMITTER, password='ValidPassword#2026!')
        emp = _make_user('emp_sess', UserRole.EMPLOYEE, password='ValidPassword#2026!')

        login_url = reverse('accounts:login')

        client.post(login_url, {'username': 'naveen_sess', 'password': 'ValidPassword#2026!', 'remember_me': 'on'})
        assert client.session.get_expiry_age() <= 28805
        assert client.session.get_expiry_age() >= 28700
        client.logout()

        # Without remember_me -> expire at browser close
        client.post(login_url, {'username': 'naveen_sess', 'password': 'ValidPassword#2026!'})
        assert client.session.get_expire_at_browser_close() is True
        client.logout()

        client.post(login_url, {'username': 'ext_sess', 'password': 'ValidPassword#2026!', 'remember_me': 'on'})
        assert client.session.get_expiry_age() <= 1209605
        assert client.session.get_expiry_age() >= 1209500
        client.logout()

        client.post(login_url, {'username': 'emp_sess', 'password': 'ValidPassword#2026!', 'remember_me': 'on'})
        assert client.session.get_expiry_age() >= 31535000

    def test_recover_admin_account_management_command(self):
        admin = _make_user(
            'naveen_recov',
            UserRole.NAVEEN,
            is_active=False,
            account_status=AccountStatus.DEACTIVATED,
            has_admin_access=False,
            has_accounts_access=False,
        )
        out = io.StringIO()
        call_command(
            'recover_admin_account',
            username='naveen_recov',
            operator='ops-test',
            reason='Automated recovery test',
            generate_temp_password=True,
            grant_accounts_access=True,
            stdout=out,
        )
        admin.refresh_from_db()
        assert admin.is_active is True
        assert admin.account_status == AccountStatus.ACTIVE
        assert admin.has_admin_access is True
        assert admin.has_accounts_access is True
        assert admin.must_change_password is True
        assert AuditLog.objects.filter(action='ADMIN_RECOVERY_EXECUTED', user=admin).exists()

    def test_templates_avoid_raw_innerhtml_xss_sinks(self):
        for rel_path in [
            'templates/core/naveen_dashboard.html',
            'templates/invoices/upload.html',
            'templates/invoices/resubmit.html',
        ]:
            with open(rel_path, 'r', encoding='utf-8') as fh:
                content = fh.read()
            assert 'col.innerHTML =' not in content
            assert 'row.innerHTML =' not in content
            assert 'showFileError(' not in content or 'textContent' in content

