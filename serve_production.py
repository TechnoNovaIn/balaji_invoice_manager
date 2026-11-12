"""
Production Waitress WSGI Server Entrypoint for Balaji InvoiceHub (Windows 11).

Responsibilities:
1. Initializes Django settings and validates WSGI application readiness.
2. Safely recovers any truly orphaned OCR/Gemini jobs from crashed/restarted workers
   without disturbing any live worker processes.
3. Serves Django via Waitress bound to loopback (behind Cloudflare Tunnel) with
   production thread pooling, timeouts, and 100 MB upload support.
4. Waits briefly for active OCR worker threads to finish cleanly on shutdown.
"""
import atexit
import logging
import os
import signal
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'invoicehub.settings')

import django

django.setup()

from django.core.wsgi import get_wsgi_application
from waitress import serve

from apps.invoices.tasks import recover_incomplete_documents, wait_for_active_jobs

logger = logging.getLogger('waitress')


def run_startup_recovery() -> None:
    try:
        summary = recover_incomplete_documents(stale_minutes=0, requeue_only=False)
        logger.info(
            "Startup OCR recovery check: found=%s recovered=%s already_completed=%s failed_missing=%s skipped_active=%s",
            summary['total_found'],
            summary['recovered_ids'],
            summary['already_completed_ids'],
            summary['failed_missing_ids'],
            summary.get('skipped_active_ids', []),
        )
    except Exception:
        logger.exception("Startup OCR recovery check encountered an error; continuing server boot.")


def _on_shutdown(*_args) -> None:
    remaining = wait_for_active_jobs(timeout=15.0)
    if remaining:
        logger.warning("Shutdown proceeding with %d active OCR background thread(s) still running.", remaining)


def main() -> None:
    host = os.getenv('WAITRESS_HOST', '127.0.0.1').strip()
    port = int(os.getenv('WAITRESS_PORT', '8000'))
    threads = int(os.getenv('WAITRESS_THREADS', '8'))
    channel_timeout = int(os.getenv('WAITRESS_CHANNEL_TIMEOUT', '180'))
    max_body_bytes = int(os.getenv('MAX_FILE_SIZE_BYTES', str(100 * 1024 * 1024)))
    trusted_proxy = os.getenv('WAITRESS_TRUSTED_PROXY', '127.0.0.1').strip()
    trusted_proxy_count = max(1, int(os.getenv('DJANGO_TRUSTED_PROXY_COUNT', '1') or '1'))

    run_startup_recovery()

    atexit.register(_on_shutdown)
    for sig_name in ('SIGINT', 'SIGTERM'):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, lambda *_a: sys.exit(0))
            except Exception:
                pass

    application = get_wsgi_application()
    logger.info(
        "Starting Balaji InvoiceHub Waitress server on %s:%s (threads=%s, channel_timeout=%ss, trusted_proxy=%s)",
        host,
        port,
        threads,
        channel_timeout,
        trusted_proxy or 'none',
    )
    serve_kwargs = dict(
        host=host,
        port=port,
        threads=threads,
        channel_timeout=channel_timeout,
        max_request_body_size=max_body_bytes,
        ident='BalajiInvoiceHub',
        clear_untrusted_proxy_headers=True,
    )
    if trusted_proxy:
        serve_kwargs.update(
            trusted_proxy=trusted_proxy,
            trusted_proxy_count=trusted_proxy_count,
            trusted_proxy_headers={
                'x-forwarded-for',
                'x-forwarded-proto',
                'x-forwarded-host',
                'x-forwarded-port',
            },
        )
    serve(application, **serve_kwargs)


if __name__ == '__main__':
    main()

