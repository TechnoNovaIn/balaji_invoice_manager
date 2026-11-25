"""
Regression tests for the pre-deployment blockers:
open redirects, delete/status locks, Tally marking rules, and Gemini retry behaviour.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import Department, User, UserRole
from apps.invoices import gemini_extractor
from apps.invoices.models import InvoiceSubmission
from apps.invoices.services import create_invoice_submission
from tests.test_tally_gst import create_dummy_jpeg


class WorkflowGuardTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name="Fleet", code="FLEET")
        self.emp = User.objects.create_user(
            username="emp_guard", password="TestPassword@123", role=UserRole.EMPLOYEE, department=self.dept,
        )
        self.naveen = User.objects.create_user(
            username="naveen_guard", password="TestPassword@123", role=UserRole.NAVEEN, department=self.dept,
        )
        self.submission = create_invoice_submission(
            user=self.emp, files=[create_dummy_jpeg("guard.jpg", content_tag="guard_bill")]
        )

    def _set(self, **fields):
        InvoiceSubmission.objects.filter(pk=self.submission.pk).update(**fields)
        self.submission.refresh_from_db()

    # --- transition rules -------------------------------------------------
    def test_transition_map(self):
        sub = self.submission
        self.assertTrue(sub.can_transition_to(InvoiceSubmission.STATUS_VERIFIED))
        sub.status = InvoiceSubmission.STATUS_REJECTED
        self.assertFalse(sub.can_transition_to(InvoiceSubmission.STATUS_VERIFIED))
        sub.status = InvoiceSubmission.STATUS_VERIFIED
        self.assertTrue(sub.can_transition_to(InvoiceSubmission.STATUS_RETURNED))
        sub.is_entered_in_tally = True
        self.assertFalse(sub.can_transition_to(InvoiceSubmission.STATUS_RETURNED))
        self.assertFalse(sub.can_be_deleted)

    def test_cannot_return_invoice_already_in_tally(self):
        self._set(status=InvoiceSubmission.STATUS_VERIFIED, is_entered_in_tally=True)
        self.client.force_login(self.naveen)
        self.client.post(
            reverse('invoices:naveen_return', kwargs={'submission_id': self.submission.submission_id}),
            {'return_reason': 'Try to reopen'},
        )
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.status, InvoiceSubmission.STATUS_VERIFIED)

    def test_cannot_verify_rejected_invoice(self):
        self._set(status=InvoiceSubmission.STATUS_REJECTED)
        self.client.force_login(self.naveen)
        self.client.post(
            reverse('invoices:naveen_verify', kwargs={'submission_id': self.submission.submission_id}),
            {'confirm_manual_review': 'on'},
        )
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.status, InvoiceSubmission.STATUS_REJECTED)

    # --- delete lock ------------------------------------------------------
    def test_employee_cannot_delete_verified_invoice(self):
        self._set(status=InvoiceSubmission.STATUS_VERIFIED)
        self.client.force_login(self.emp)
        self.client.post(reverse('invoices:delete_invoice', kwargs={'submission_id': self.submission.submission_id}))
        self.assertTrue(InvoiceSubmission.objects.filter(pk=self.submission.pk).exists())

    def test_employee_can_delete_pending_invoice(self):
        self.client.force_login(self.emp)
        self.client.post(reverse('invoices:delete_invoice', kwargs={'submission_id': self.submission.submission_id}))
        self.assertFalse(InvoiceSubmission.objects.filter(pk=self.submission.pk).exists())

    # --- open redirects ---------------------------------------------------
    def test_delete_ignores_external_next(self):
        self.client.force_login(self.emp)
        resp = self.client.post(
            reverse('invoices:delete_invoice', kwargs={'submission_id': self.submission.submission_id}),
            {'next': 'https://evil.example.com/login'},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn('evil.example.com', resp['Location'])

    def test_toggle_tally_ignores_external_referer(self):
        self._set(status=InvoiceSubmission.STATUS_VERIFIED)
        self.client.force_login(self.naveen)
        resp = self.client.post(
            reverse('invoices:toggle_tally', kwargs={'submission_id': self.submission.submission_id}),
            HTTP_REFERER='https://evil.example.com/phish',
        )
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn('evil.example.com', resp['Location'])

    def test_toggle_tally_keeps_internal_next(self):
        self._set(status=InvoiceSubmission.STATUS_VERIFIED)
        self.client.force_login(self.naveen)
        resp = self.client.post(
            reverse('invoices:toggle_tally', kwargs={'submission_id': self.submission.submission_id}),
            {'next': '/dashboard/naveen/?tab=completed'},
        )
        self.assertEqual(resp['Location'], '/dashboard/naveen/?tab=completed')

    # --- locked edits -----------------------------------------------------
    def test_quick_update_blocked_once_in_tally(self):
        self._set(status=InvoiceSubmission.STATUS_VERIFIED, is_entered_in_tally=True, party_name='Original Vendor')
        self.client.force_login(self.naveen)
        self.client.post(
            reverse('invoices:quick_update_metadata', kwargs={'submission_id': self.submission.submission_id}),
            {'party_name': 'Changed Vendor'},
        )
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.party_name, 'Original Vendor')


class _FakeAPIError(Exception):
    def __init__(self, code, details=None):
        super().__init__(f"{code} error")
        self.code = code
        self.details = details or {}


@override_settings(GEMINI_API_KEY='test-key', GEMINI_API_KEYS='', GEMINI_MODEL='primary-model', GEMINI_FALLBACK_MODELS='',
                   GEMINI_MAX_ATTEMPTS=3)
class GeminiRetryTests(TestCase):
    def test_retry_delay_honours_server_hint(self):
        err = _FakeAPIError(429, {'error': {'details': [{'@type': 'RetryInfo', 'retryDelay': '17s'}]}})
        self.assertEqual(gemini_extractor._gemini_retry_delay(err, 0), 17.0)

    def test_retry_delay_is_capped(self):
        err = _FakeAPIError(429, {'error': {'details': [{'retryDelay': '900s'}]}})
        with override_settings(GEMINI_MAX_RETRY_DELAY_SECONDS=60):
            self.assertEqual(gemini_extractor._gemini_retry_delay(err, 0), 60.0)

    def test_only_configured_models_are_used(self):
        self.assertEqual(gemini_extractor._configured_gemini_models(), ['primary-model'])
        with override_settings(GEMINI_FALLBACK_MODELS='backup-a, primary-model ,backup-b'):
            self.assertEqual(gemini_extractor._configured_gemini_models(), ['primary-model', 'backup-a', 'backup-b'])

    def test_rate_limit_then_success_retries_same_model(self):
        import tempfile, os
        good = SimpleNamespace(parsed=None, text='{"party_name": "Test Vendor", "total_amount": 100}')
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = [_FakeAPIError(429), good]
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp:
            tmp.write(b'\xff\xd8\xff' + b'0' * 400)
            path = tmp.name
        try:
            with patch('apps.invoices.gemini_service.get_available_gemini_keys', return_value=['test-key']), \
                    patch.object(gemini_extractor, '_get_gemini_client', return_value=mock_client), \
                    patch.object(gemini_extractor.time, 'sleep') as mock_sleep:
                gemini_extractor.extract_with_gemini(path)
        finally:
            os.unlink(path)
        calls = mock_client.models.generate_content.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(c.kwargs['model'] == 'primary-model' for c in calls))
        mock_sleep.assert_called_once()

    def test_non_retryable_error_does_not_retry(self):
        import tempfile, os
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = _FakeAPIError(400)
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp:
            tmp.write(b'\xff\xd8\xff' + b'0' * 400)
            path = tmp.name
        try:
            with patch('apps.invoices.gemini_service.get_available_gemini_keys', return_value=['test-key']), \
                    patch.object(gemini_extractor, '_get_gemini_client', return_value=mock_client), \
                    patch.object(gemini_extractor.time, 'sleep') as mock_sleep:
                result = gemini_extractor.extract_with_gemini(path)
        finally:
            os.unlink(path)
        self.assertIsNone(result)
        self.assertEqual(mock_client.models.generate_content.call_count, 1)
        mock_sleep.assert_not_called()
