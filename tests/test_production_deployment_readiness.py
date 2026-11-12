import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image
from django.conf import settings
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import RequestFactory, override_settings
from django.urls import reverse

from apps.accounts.models import AuditLog, User, UserRole
from apps.accounts.security import get_client_ip
from apps.invoices.models import InvoiceSubmission, UploadedDocument
from apps.invoices.tasks import (
    _ACTIVE_DOCUMENT_IDS,
    _ACTIVE_LOCK,
    _release_worker_lock,
    _write_worker_lock,
    get_incomplete_documents,
    recover_incomplete_documents,
)


pytestmark = pytest.mark.django_db


class TestStaticAndLoggingConfiguration:
    def test_whitenoise_and_waitress_configured(self):
        assert 'whitenoise.middleware.WhiteNoiseMiddleware' in settings.MIDDLEWARE
        assert settings.MIDDLEWARE.index('whitenoise.middleware.WhiteNoiseMiddleware') == (
            settings.MIDDLEWARE.index('django.middleware.security.SecurityMiddleware') + 1
        )
        assert 'app_file' in settings.LOGGING['handlers']
        assert 'security_file' in settings.LOGGING['handlers']
        assert settings.LOGGING['handlers']['app_file']['class'] == 'logging.handlers.RotatingFileHandler'

        req_text = Path(settings.BASE_DIR / 'requirements.txt').read_text(encoding='utf-8')
        assert 'waitress==' in req_text
        assert 'whitenoise==' in req_text
        assert 'gunicorn==' not in req_text

        serve_text = Path(settings.BASE_DIR / 'serve_production.py').read_text(encoding='utf-8')
        assert 'trusted_proxy=' in serve_text
        assert 'x-forwarded-proto' in serve_text
        assert 'clear_untrusted_proxy_headers=True' in serve_text

    def test_staticfiles_served_when_debug_false(self, client):
        # Ensure collected static files exist on disk and WhiteNoise serves them with DEBUG=False
        call_command('collectstatic', '--noinput', verbosity=0)
        with override_settings(DEBUG=False, WHITENOISE_AUTOREFRESH=False, WHITENOISE_USE_FINDERS=False):
            css_resp = client.get('/static/css/enterprise.css')
            assert css_resp.status_code == 200
            manifest_resp = client.get('/static/manifest.json')
            assert manifest_resp.status_code == 200


class TestCloudflareProxyAndRateLimiting:
    def test_cloudflare_connecting_ip_trusted_only_from_trusted_proxy(self):
        rf = RequestFactory()

        # Trusted loopback proxy (cloudflared) -> accepts CF-Connecting-IP
        trusted_req = rf.get(
            '/',
            REMOTE_ADDR='127.0.0.1',
            HTTP_CF_CONNECTING_IP='203.0.113.77',
            HTTP_X_FORWARDED_FOR='1.1.1.1, 203.0.113.77',
        )
        with override_settings(
            TRUSTED_PROXY_COUNT=1,
            TRUSTED_PROXY_IPS=['127.0.0.1', '::1'],
            TRUST_CF_CONNECTING_IP=True,
        ):
            assert get_client_ip(trusted_req) == '203.0.113.77'

        # Untrusted peer attempting to spoof CF-Connecting-IP & X-Forwarded-For -> ignored completely
        spoofed_req = rf.get(
            '/',
            REMOTE_ADDR='198.51.100.9',
            HTTP_CF_CONNECTING_IP='8.8.8.8',
            HTTP_X_FORWARDED_FOR='8.8.4.4',
        )
        with override_settings(
            TRUSTED_PROXY_COUNT=1,
            TRUSTED_PROXY_IPS=['127.0.0.1', '::1'],
            TRUST_CF_CONNECTING_IP=True,
        ):
            assert get_client_ip(spoofed_req) == '198.51.100.9'

    def test_distinct_external_users_behind_tunnel_do_not_collide_rate_limits(self, client):
        cache.clear()
        User.objects.create_user(username='ext_cf_1', password='GoodPassword#2026!', role=UserRole.EXTERNAL_SUBMITTER)
        User.objects.create_user(username='ext_cf_2', password='GoodPassword#2026!', role=UserRole.EXTERNAL_SUBMITTER)
        login_url = reverse('accounts:login')

        with override_settings(
            TRUSTED_PROXY_COUNT=1,
            TRUSTED_PROXY_IPS=['127.0.0.1', '::1'],
            TRUST_CF_CONNECTING_IP=True,
            LOGIN_MAX_ATTEMPTS=2,
            LOGIN_ATTEMPT_WINDOW_SECONDS=300,
            LOGIN_COOLDOWN_SECONDS=120,
        ):
            # Attacker on IP 203.0.113.10 triggers cooldown on ext_cf_1
            r1 = client.post(
                login_url,
                {'username': 'ext_cf_1', 'password': 'WrongPassword!'},
                REMOTE_ADDR='127.0.0.1',
                HTTP_CF_CONNECTING_IP='203.0.113.10',
            )
            assert r1.status_code == 200
            r2 = client.post(
                login_url,
                {'username': 'ext_cf_1', 'password': 'WrongPassword!'},
                REMOTE_ADDR='127.0.0.1',
                HTTP_CF_CONNECTING_IP='203.0.113.10',
            )
            assert r2.status_code == 429

            # Second user on distinct real IP 203.0.113.20 is NOT locked out behind shared loopback proxy
            ok_resp = client.post(
                login_url,
                {'username': 'ext_cf_2', 'password': 'GoodPassword#2026!'},
                REMOTE_ADDR='127.0.0.1',
                HTTP_CF_CONNECTING_IP='203.0.113.20',
            )
            assert ok_resp.status_code == 302
            assert AuditLog.objects.filter(action='USER_LOGIN_FAILED', ip_address='203.0.113.10').count() == 2
        cache.clear()


class TestPWAManifestServiceWorkerAndNoCache:
    def test_pwa_manifest_and_icons_valid(self, client):
        call_command('collectstatic', '--noinput', verbosity=0)
        manifest_path = Path(settings.BASE_DIR) / 'static' / 'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))

        assert manifest['name'] == 'Balaji InvoiceHub'
        assert manifest['start_url'] == '/'
        assert manifest['scope'] == '/'
        assert manifest['display'] == 'standalone'

        icon_sizes = {icon['sizes']: icon['src'] for icon in manifest['icons']}
        assert '192x192' in icon_sizes
        assert '512x512' in icon_sizes

        for expected_size, src in [('192x192', 'icon-192.png'), ('512x512', 'icon-512.png')]:
            icon_file = Path(settings.BASE_DIR) / 'static' / 'img' / src
            assert icon_file.exists()
            with Image.open(icon_file) as im:
                w, h = im.size
                assert f"{w}x{h}" == expected_size

        # HTML includes manifest + SW registration
        login_html = client.get(reverse('accounts:login')).content.decode('utf-8')
        assert 'manifest.json' in login_html
        assert "register('/sw.js'" in login_html

    def test_service_worker_and_offline_route_strict_no_sensitive_cache(self, client):
        sw_resp = client.get('/sw.js')
        assert sw_resp.status_code == 200
        assert 'javascript' in sw_resp['Content-Type']
        assert sw_resp['Service-Worker-Allowed'] == '/'
        assert 'no-store' in sw_resp['Cache-Control']

        sw_code = sw_resp.content.decode('utf-8')
        for sensitive_prefix in ['/accounts/', '/invoices/', '/dashboard/', '/backup/', '/media/']:
            assert sensitive_prefix in sw_code
        assert "request.mode === 'navigate'" in sw_code

        offline_resp = client.get('/offline/')
        assert offline_resp.status_code == 200
        offline_html = offline_resp.content.decode('utf-8')
        assert 'Internet Connection Required' in offline_html
        assert 'GSTIN' not in offline_html

    def test_authenticated_pages_emit_no_store_headers(self, client):
        ext = User.objects.create_user(username='ext_nocache', password='ValidPassword#2026!', role=UserRole.EXTERNAL_SUBMITTER)
        client.force_login(ext)
        for url in [reverse('core:employee_dashboard'), reverse('invoices:my_submissions')]:
            resp = client.get(url)
            assert resp.status_code == 200
            cc = resp.get('Cache-Control', '')
            assert 'no-store' in cc
            assert 'private' in cc
            assert resp.get('Pragma') == 'no-cache'


class TestInterruptedOCRRecoverySafety:
    def test_recovery_skips_active_worker_and_resumes_crashed_job(self):
        user = User.objects.create_user(username='ext_ocr_rec', password='ValidPassword#2026!', role=UserRole.EXTERNAL_SUBMITTER)
        pdf_bytes = b'%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n'

        active_doc = UploadedDocument.objects.create(
            uploader=user,
            original_file=SimpleUploadedFile('active.pdf', pdf_bytes, content_type='application/pdf'),
            file_name='active.pdf',
            file_sha256='1' * 64,
            file_size_bytes=len(pdf_bytes),
            content_type='application/pdf',
            processing_status=UploadedDocument.STATUS_PROCESSING,
        )
        crashed_doc = UploadedDocument.objects.create(
            uploader=user,
            original_file=SimpleUploadedFile('crashed.pdf', pdf_bytes, content_type='application/pdf'),
            file_name='crashed.pdf',
            file_sha256='2' * 64,
            file_size_bytes=len(pdf_bytes),
            content_type='application/pdf',
            processing_status=UploadedDocument.STATUS_PROCESSING,
        )

        # Simulate active_doc owned by an external live OS process (parent PID != os.getpid(), NOT in _ACTIVE_DOCUMENT_IDS)
        active_lock_file = Path(settings.MEDIA_ROOT) / '.worker_locks' / f'doc_{active_doc.id}.lock'
        active_lock_file.parent.mkdir(parents=True, exist_ok=True)
        active_lock_file.write_text(
            json.dumps({'pid': os.getppid(), 'doc_id': active_doc.id}),
            encoding='utf-8',
        )
        # Simulate crashed_doc left with a dead worker PID lock
        lock_file = Path(settings.MEDIA_ROOT) / '.worker_locks' / f'doc_{crashed_doc.id}.lock'
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock_file.write_text(json.dumps({'pid': 99999999, 'doc_id': crashed_doc.id}), encoding='utf-8')

        try:
            with _ACTIVE_LOCK:
                _ACTIVE_DOCUMENT_IDS.discard(active_doc.id)

            incomplete_ids = list(get_incomplete_documents(stale_minutes=0).values_list('id', flat=True))
            assert active_doc.id not in incomplete_ids
            assert crashed_doc.id in incomplete_ids

            with patch('apps.invoices.tasks.dispatch_background_processing') as mock_dispatch:
                summary = recover_incomplete_documents(stale_minutes=0, requeue_only=False)

            assert crashed_doc.id in summary['recovered_ids']
            assert active_doc.id not in summary['recovered_ids']
            mock_dispatch.assert_called_once_with(uploaded_document_id=crashed_doc.id, force_recover=True)

            active_doc.refresh_from_db()
            crashed_doc.refresh_from_db()
            assert active_doc.processing_status == UploadedDocument.STATUS_PROCESSING
            assert crashed_doc.processing_status == UploadedDocument.STATUS_QUEUED
            assert not lock_file.exists()
        finally:
            _release_worker_lock(active_doc.id)
            _release_worker_lock(crashed_doc.id)
