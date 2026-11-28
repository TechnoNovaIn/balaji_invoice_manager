from unittest.mock import patch

import httpx
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

import manage as root_manage
from apps.accounts.models import AuditLog, User, UserRole
from apps.invoices.gemini_extractor import _gemini_error_code, _is_retryable_transport_error
from apps.invoices.models import InvoiceStatusHistory, InvoiceSubmission
from apps.invoices.services import create_invoice_submissions


class ServerLifecycleAndHygieneTests(TestCase):
    def test_manage_runserver_gates_insecure_to_local_or_env(self):
        with patch.dict('os.environ', {}, clear=False):
            import os
            os.environ.pop('RUNSERVER_ALLOW_INSECURE', None)
            self.assertTrue(root_manage._is_explicit_local_runserver(['manage.py', 'runserver']))
            self.assertTrue(root_manage._is_explicit_local_runserver(['manage.py', 'runserver', '127.0.0.1:8000']))
            self.assertTrue(root_manage._is_explicit_local_runserver(['manage.py', 'runserver', 'localhost:8000']))
            self.assertTrue(root_manage._is_explicit_local_runserver(['manage.py', 'runserver', '8000']))
            self.assertFalse(root_manage._is_explicit_local_runserver(['manage.py', 'runserver', '0.0.0.0:8000']))
            self.assertFalse(root_manage._is_explicit_local_runserver(['manage.py', 'runserver', '192.168.1.30:8000']))

        with patch.dict('os.environ', {'RUNSERVER_ALLOW_INSECURE': '1'}):
            self.assertTrue(root_manage._is_explicit_local_runserver(['manage.py', 'runserver', '0.0.0.0:8000']))


class GeminiAuditAndErrorTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username='audit_admin',
            password='StrongPassword!234',
            role=UserRole.ADMIN,
        )

    @override_settings(GEMINI_API_KEY='AIzaSyDummyKey1234567890', GEMINI_API_KEYS='AIzaSyDummyKey1234567890', GEMINI_MODEL='gemini-3.5-flash-lite')
    def test_resubmitting_identical_gemini_config_skips_duplicate_audit_log(self):
        self.client.force_login(self.admin)
        url = reverse('core:update_gemini_api_key')
        resp = self.client.post(url, {
            'action': 'replace',
            'api_keys_text': 'AIzaSyDummyKey1234567890',
            'model': 'gemini-3.5-flash-lite',
            'skip_test': '1',
            'format': 'json',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json().get('unchanged'))
        self.assertEqual(AuditLog.objects.filter(action='CONFIG_GEMINI_API_KEY_UPDATED').count(), 0)

    def test_gemini_transport_and_api_error_detection(self):
        self.assertTrue(_is_retryable_transport_error(httpx.ReadTimeout('timed out')))
        self.assertTrue(_is_retryable_transport_error(httpx.ConnectError('connection refused')))
        self.assertTrue(_is_retryable_transport_error(TimeoutError('timeout')))
        self.assertFalse(_is_retryable_transport_error(ValueError('bad value')))

        class _FakeRespErr(Exception):
            def __init__(self, status):
                self.response = type('Resp', (), {'status_code': status})()

        self.assertEqual(_gemini_error_code(_FakeRespErr(503)), 503)


class TallyBatchHistoryTests(TestCase):
    def setUp(self):
        self.employee = User.objects.create_user(
            username='emp_tally_hist',
            password='StrongPassword!234',
            role=UserRole.EMPLOYEE,
        )
        self.naveen = User.objects.create_user(
            username='naveen_tally_hist',
            password='StrongPassword!234',
            role=UserRole.NAVEEN,
        )

    def test_batch_complete_tally_skips_noop_status_history(self):
        file_obj = SimpleUploadedFile(
            'bill.jpg',
            b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xd9',
            content_type='image/jpeg',
        )
        with patch('apps.invoices.services.extract_invoice_metadata', return_value={}):
            subs = create_invoice_submissions(self.employee, [file_obj], 'Test')
        sub = subs[0]
        initial_history_count = InvoiceStatusHistory.objects.filter(submission=sub).count()

        self.client.force_login(self.naveen)
        resp = self.client.post(reverse('invoices:batch_complete_tally'), {
            'selected_invoices': [sub.submission_id],
        })
        self.assertEqual(resp.status_code, 302)
        sub.refresh_from_db()
        self.assertTrue(sub.is_entered_in_tally)
        self.assertEqual(InvoiceStatusHistory.objects.filter(submission=sub).count(), initial_history_count)
        self.assertTrue(AuditLog.objects.filter(action='TALLY_BATCH_COMPLETE').exists())
