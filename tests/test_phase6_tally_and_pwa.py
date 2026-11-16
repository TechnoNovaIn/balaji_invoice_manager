"""
Phase 6 Unit Tests: Configurable Tally Company Name, Tally XML/Excel & PWA Verification
Balaji InvoiceHub
"""
import io
import json
import pytest
from django.conf import settings
from django.test import Client, override_settings
from django.contrib.auth import get_user_model
from apps.invoices.models import InvoiceSubmission
from apps.invoices.tally_exporter import generate_tally_xml
from apps.invoices.tally_excel_exporter import generate_tally_structured_excel

User = get_user_model()


@pytest.mark.django_db
class TestPhase6TallyAndPWA:

    @pytest.fixture(autouse=True)
    def setup_data(self):
        self.user = User.objects.create_user(
            username='operator_naveen',
            role='NAVEEN',
            is_staff=True,
        )
        self.submission = InvoiceSubmission.objects.create(
            submission_id='BTI-202611-77001',
            employee=self.user,
            party_name='SHREE BALAJI LOGISTICS',
            party_gstin='29ABCPE1234F1Z7',
            invoice_number='INV-77001',
            taxable_amount=10000.00,
            cgst_amount=900.00,
            sgst_amount=900.00,
            total_amount=11800.00,
            expense_ledger='Purchase Spares & Consumables',
        )

    def test_tally_xml_default_company_name(self):
        """Standard export includes default company name in Tally XML header."""
        xml_output = generate_tally_xml([self.submission])
        assert '<SVCURRENTCOMPANY>Balaji Transports</SVCURRENTCOMPANY>' in xml_output
        assert '<VOUCHER' in xml_output
        assert '<AMOUNT>-11800.00</AMOUNT>' in xml_output

    def test_tally_xml_configurable_company_name(self):
        """Custom TALLY_COMPANY_NAME dynamically sets SVCURRENTCOMPANY without code modification."""
        with override_settings(TALLY_COMPANY_NAME='Balaji Southern Fleet Logistics Pvt Ltd'):
            xml_output = generate_tally_xml([self.submission])
            assert '<SVCURRENTCOMPANY>Balaji Southern Fleet Logistics Pvt Ltd</SVCURRENTCOMPANY>' in xml_output
            assert '<REPORTNAME>All Accounting Vouchers</REPORTNAME>' in xml_output

    def test_tally_excel_configurable_company_name(self):
        """Custom company name appears in Tally Excel header block."""
        with override_settings(TALLY_COMPANY_NAME='Sri Balaji Transports Karnataka'):
            excel_buf = generate_tally_structured_excel([self.submission], user=self.user)
            import openpyxl
            wb = openpyxl.load_workbook(excel_buf)
            ws = wb.active
            title_text = ws['A1'].value
            assert 'SRI BALAJI TRANSPORTS KARNATAKA' in title_text

    def test_pwa_service_worker_endpoint_and_scope(self):
        """PWA Service Worker is served from root /sw.js with Service-Worker-Allowed header."""
        client = Client()
        response = client.get('/sw.js')
        assert response.status_code == 200
        assert 'javascript' in response['Content-Type']
        assert response['Service-Worker-Allowed'] == '/'
        assert 'no-cache' in response['Cache-Control']

        # Verify strict zero-cache policy on authenticated and sensitive routes
        sw_body = response.content.decode('utf-8')
        assert "NEVER_CACHE_PREFIXES" in sw_body
        for sensitive_prefix in ['/accounts/', '/invoices/', '/dashboard/', '/admin/', '/media/']:
            assert sensitive_prefix in sw_body

    def test_pwa_manifest_json(self):
        """Manifest.json is valid and configured for standalone PWA installation."""
        client = Client()
        response = client.get('/static/manifest.json')
        assert response.status_code == 200
        data = json.loads(response.content.decode('utf-8'))
        assert data['display'] == 'standalone'
        assert data['scope'] == '/'
        assert data['short_name'] == 'InvoiceHub'
        assert len(data['icons']) >= 2

    def test_pwa_offline_fallback(self):
        """PWA offline fallback endpoint returns 200 and enterprise offline message."""
        client = Client()
        response = client.get('/offline/')
        assert response.status_code == 200
        assert 'Offline' in response.content.decode('utf-8')
