import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock
from django.test import TestCase, Client
from django.urls import reverse
from django.conf import settings
from apps.accounts.models import User, UserRole, AuditLog
from apps.invoices.gemini_service import (
    mask_api_key,
    parse_gemini_keys,
    get_configured_gemini_keys,
    mark_key_exhausted,
    mark_key_healthy,
    get_key_health,
    get_available_gemini_keys,
    save_gemini_key_pool,
    test_gemini_connection,
    test_all_gemini_keys,
    _KEY_EXHAUSTED_UNTIL,
)


class GeminiMultiKeyPoolTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.password = "StrongPass@2026"

        self.admin = User.objects.create_user(
            username="admin_test",
            password=self.password,
            role=UserRole.ADMIN,
            is_active=True
        )
        self.naveen = User.objects.create_user(
            username="naveen_test",
            password=self.password,
            role=UserRole.NAVEEN,
            is_active=True
        )
        self.employee = User.objects.create_user(
            username="emp_test",
            password=self.password,
            role=UserRole.EMPLOYEE,
            is_active=True
        )
        self.management = User.objects.create_user(
            username="mgmt_test",
            password=self.password,
            role=UserRole.MANAGEMENT,
            is_active=True
        )

        self.config_url = reverse('core:ai_engine_config')
        self.update_url = reverse('core:update_gemini_api_key')
        self.test_url = reverse('core:test_gemini_api_key')

    def tearDown(self):
        _KEY_EXHAUSTED_UNTIL.clear()

    def test_mask_api_key(self):
        self.assertEqual(mask_api_key(""), "")
        self.assertEqual(mask_api_key(None), "")
        self.assertEqual(mask_api_key("ab"), "••••••••")
        masked = mask_api_key("AQ.MockSyntheticKeyForTestingOnlyZeroRealData1234567eyQ")
        self.assertTrue(masked.endswith("eyQ"))
        self.assertIn("••••••••", masked)
        self.assertFalse(masked.startswith("AQ.Mock"))

    def test_parse_gemini_keys(self):
        raw = "key1, key2 \n key3;key4\r\nkey1\n"
        parsed = parse_gemini_keys(raw)
        self.assertEqual(parsed, ["key1", "key2", "key3", "key4"])

        # Deduplication preserving order
        self.assertEqual(parse_gemini_keys(["k1", "k2", "k1", "k3"]), ["k1", "k2", "k3"])

    def test_key_health_and_cooldown_tracking(self):
        k1 = "API_KEY_ALPHA_001"
        k2 = "API_KEY_BETA_002"

        mark_key_healthy(k1)
        h1 = get_key_health(k1)
        self.assertFalse(h1['is_exhausted'])
        self.assertEqual(h1['status'], 'HEALTHY')

        mark_key_exhausted(k1, cooldown_seconds=3600)
        h1_after = get_key_health(k1)
        self.assertTrue(h1_after['is_exhausted'])
        self.assertEqual(h1_after['status'], 'EXHAUSTED')
        self.assertGreater(h1_after['cooldown_remaining_seconds'], 3500)

        # Ordering check: healthy keys come before exhausted keys
        with patch('apps.invoices.gemini_service.get_configured_gemini_keys', return_value=[k1, k2]):
            ordered = get_available_gemini_keys()
            self.assertEqual(ordered[0], k2)
            self.assertEqual(ordered[1], k1)

        # Mark k1 healthy again
        mark_key_healthy(k1)
        self.assertFalse(get_key_health(k1)['is_exhausted'])

    def test_unauthenticated_access_denied(self):
        resp_config = self.client.get(self.config_url)
        self.assertEqual(resp_config.status_code, 302)
        self.assertIn('login', resp_config.url)

        resp = self.client.post(self.update_url, {'api_key': 'some_key'})
        self.assertEqual(resp.status_code, 302)
        self.assertIn('login', resp.url)

        resp2 = self.client.post(self.test_url, {})
        self.assertEqual(resp2.status_code, 302)
        self.assertIn('login', resp2.url)

    def test_non_admin_roles_strictly_forbidden(self):
        for user in [self.employee, self.naveen, self.management]:
            self.client.login(username=user.username, password=self.password)

            resp_config = self.client.get(self.config_url)
            self.assertEqual(resp_config.status_code, 403, f"User {user.role} was not blocked with 403 on config page")

            resp_update = self.client.post(self.update_url, {'api_key': 'some_test_key_1234567'})
            self.assertEqual(resp_update.status_code, 403, f"User {user.role} was not blocked with 403 on update")

            resp_test = self.client.post(self.test_url, {})
            self.assertEqual(resp_test.status_code, 403, f"User {user.role} was not blocked with 403 on test")

            self.client.logout()

    def test_ai_config_page_renders_key_pool(self):
        self.client.login(username=self.admin.username, password=self.password)
        resp = self.client.get(self.config_url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Failover Key Pool Runtime")
        self.assertContains(resp, "Zero-Error Failover")
        self.assertContains(resp, "Unlock Multi-Key Editor")

    @patch('apps.invoices.gemini_service.test_gemini_connection')
    def test_admin_can_test_entire_key_pool(self, mock_test):
        mock_test.return_value = {
            'success': True,
            'message': 'API active (200 OK)',
            'latency_ms': 120,
            'model': 'gemini-3.5-flash-lite',
            'masked_key': '••••••••••••••••••••eyQ',
        }
        self.client.login(username=self.admin.username, password=self.password)
        resp = self.client.post(self.test_url, {'test_all': '1'}, headers={'x-requested-with': 'XMLHttpRequest'})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['success'])
        self.assertIn('healthy_count', data)

    @patch('apps.invoices.gemini_service.test_gemini_connection')
    @patch('apps.invoices.gemini_service.save_gemini_key_pool')
    def test_admin_update_multi_key_pool(self, mock_save, mock_test):
        mock_test.return_value = {
            'success': True,
            'message': 'API active (200 OK)',
            'latency_ms': 150,
            'model': 'gemini-3.5-flash-lite'
        }
        mock_save.return_value = True

        self.client.login(username=self.admin.username, password=self.password)
        keys_input = "AQ.TestKeyOneValidLength_202611\nAQ.TestKeyTwoValidLength_202612\nAQ.TestKeyThreeValidLength_202613"

        resp = self.client.post(
            self.update_url,
            {
                'api_keys_text': keys_input,
                'action': 'replace',
                'model': 'gemini-3.5-flash-lite',
            },
            headers={'x-requested-with': 'XMLHttpRequest'}
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['pool_count'], 3)

        mock_save.assert_called_once_with(
            keys=[
                "AQ.TestKeyOneValidLength_202611",
                "AQ.TestKeyTwoValidLength_202612",
                "AQ.TestKeyThreeValidLength_202613",
            ],
            model_name='gemini-3.5-flash-lite'
        )

        # Audit log verified
        log = AuditLog.objects.filter(action='CONFIG_GEMINI_API_KEY_UPDATED').first()
        self.assertIsNotNone(log)
        self.assertEqual(log.details['pool_count'], 3)

    @patch('apps.invoices.gemini_service.get_configured_gemini_keys')
    @patch('apps.invoices.gemini_service.save_gemini_key_pool')
    def test_admin_remove_single_key_from_pool(self, mock_save, mock_get_keys):
        mock_get_keys.return_value = [
            "AQ.KeyAlpha1234567890",
            "AQ.KeyBeta1234567890",
            "AQ.KeyGamma1234567890"
        ]
        mock_save.return_value = True

        self.client.login(username=self.admin.username, password=self.password)
        resp = self.client.post(
            self.update_url,
            {'action': 'remove', 'key_index': '2'},
            headers={'x-requested-with': 'XMLHttpRequest'}
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['remaining_count'], 2)

        # Verify key #2 was removed
        mock_save.assert_called_once_with(
            ["AQ.KeyAlpha1234567890", "AQ.KeyGamma1234567890"],
            model_name='gemini-3.5-flash-lite'
        )

        log = AuditLog.objects.filter(action='CONFIG_GEMINI_KEY_REMOVED').first()
        self.assertIsNotNone(log)

    @patch('apps.invoices.gemini_service.get_available_gemini_keys')
    @patch('apps.invoices.gemini_extractor._get_gemini_client')
    def test_extractor_seamless_failover_on_429(self, mock_get_client, mock_get_keys):
        """
        Verify that if Key 1 encounters HTTP 429 (Resource Exhausted), the extractor
        immediately fails over to Key 2 without raising an error or failing the invoice.
        """
        key1 = "AQ.ExhaustedKey1_123456789"
        key2 = "AQ.HealthyKey2_123456789"
        mock_get_keys.return_value = [key1, key2]

        from apps.invoices.gemini_extractor import extract_with_gemini, InvoiceSchema

        # Mock mock_error for Key 1 (HTTP 429)
        class Mock429Error(Exception):
            code = 429

        client1 = MagicMock()
        client1.models.generate_content.side_effect = Mock429Error("429 Resource Exhausted")

        # Mock success for Key 2
        mock_response = MagicMock()
        mock_response.parsed = InvoiceSchema(
            party_name="SRI BALAJI SPARES",
            invoice_number="INV-999",
            invoice_date="2026-11-20",
            total_amount=15000.0,
            cgst_amount=1350.0,
            sgst_amount=1350.0,
            taxable_amount=12300.0,
        )
        client2 = MagicMock()
        client2.models.generate_content.return_value = mock_response

        def get_client_side_effect(k):
            if k == key1:
                return client1
            return client2

        mock_get_client.side_effect = get_client_side_effect

        # Create dummy image file > 200 bytes
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
            tf.write(b"X" * 500)
            tf_path = tf.name

        try:
            result = extract_with_gemini(tf_path)
            self.assertIsNotNone(result)
            self.assertEqual(result['party_name'], "SRI BALAJI SPARES")
            self.assertEqual(result['invoice_number'], "INV-999")
            self.assertEqual(float(result['total_amount']), 15000.0)

            # Key 1 must be marked exhausted
            h1 = get_key_health(key1)
            self.assertTrue(h1['is_exhausted'])

            # Key 2 must be marked healthy
            h2 = get_key_health(key2)
            self.assertFalse(h2['is_exhausted'])
        finally:
            if os.path.exists(tf_path):
                os.remove(tf_path)
