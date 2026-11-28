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
import threading
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / '.env')

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'invoicehub.settings')

import django

django.setup()

from django.core.wsgi import get_wsgi_application
from django.db import close_old_connections
from waitress import create_server

from apps.invoices.tasks import recover_incomplete_documents, shutdown_ocr_executor

logger = logging.getLogger('waitress')
_SHUTDOWN_EVENT = threading.Event()
_SHUTDOWN_LOCK = threading.Lock()
_SHUTDOWN_STARTED = False


def run_startup_recovery() -> None:
    """Requeue incomplete OCR jobs asynchronously without blocking server startup."""
    try:
        summary = recover_incomplete_documents(stale_minutes=0, requeue_only=True)
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
    finally:
        close_old_connections()


def _on_shutdown(*_args) -> None:
    global _SHUTDOWN_STARTED
    with _SHUTDOWN_LOCK:
        if _SHUTDOWN_STARTED:
            return
        _SHUTDOWN_STARTED = True
    remaining = shutdown_ocr_executor(wait=True, timeout=15.0)
    if remaining:
        logger.warning("Shutdown proceeding with %d active OCR background job(s) still running.", remaining)


def main() -> None:
    host = os.getenv('WAITRESS_HOST', '127.0.0.1').strip()
    port = int(os.getenv('WAITRESS_PORT', '8000'))
    threads = int(os.getenv('WAITRESS_THREADS', '8'))
    channel_timeout = int(os.getenv('WAITRESS_CHANNEL_TIMEOUT', '180'))
    max_body_bytes = int(os.getenv('MAX_FILE_SIZE_BYTES', str(100 * 1024 * 1024)))
    trusted_proxy = os.getenv('WAITRESS_TRUSTED_PROXY', '127.0.0.1').strip()
    trusted_proxy_count = max(1, int(os.getenv('DJANGO_TRUSTED_PROXY_COUNT', '1') or '1'))

    atexit.register(_on_shutdown)

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

    server = create_server(application, **serve_kwargs)

    def _request_shutdown(signum, _frame) -> None:
        logger.info("Received signal %s; initiating graceful Waitress & OCR worker shutdown.", signum)
        _SHUTDOWN_EVENT.set()

    def _shutdown_watcher() -> None:
        _SHUTDOWN_EVENT.wait()
        try:
            task_dispatcher = getattr(server, 'task_dispatcher', None)
            if task_dispatcher is not None and hasattr(task_dispatcher, 'shutdown'):
                task_dispatcher.shutdown(cancel_pending=False, timeout=10)
        except Exception:
            logger.exception("Error draining Waitress worker threads during shutdown.")
        try:
            server.close()
        except Exception:
            logger.exception("Error closing Waitress server socket during shutdown.")
        _on_shutdown()

    for sig_name in ('SIGINT', 'SIGTERM', 'SIGBREAK'):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _request_shutdown)
            except Exception:
                pass

    threading.Thread(target=_shutdown_watcher, name='waitress-shutdown', daemon=True).start()
    threading.Thread(target=run_startup_recovery, name='startup-ocr-recovery', daemon=True).start()

    try:
        server.run()
    finally:
        _SHUTDOWN_EVENT.set()
        _on_shutdown()


if __name__ == '__main__':
    main()

