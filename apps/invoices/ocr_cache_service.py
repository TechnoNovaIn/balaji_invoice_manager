"""
Persistent Database-Backed OCR Extraction Cache Service
Balaji InvoiceHub
"""
import uuid
import time
import logging
from typing import Optional, Dict, Any, Tuple
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from .models import OcrExtractionCache

logger = logging.getLogger(__name__)

PROMPT_VERSION = 'v2.0'
SCHEMA_VERSION = 'v1.0'
DEFAULT_LEASE_SECONDS = 120
MAX_RETRIES = 3


def claim_or_get_cached_ocr(
    file_sha256: str,
    model_name: str = 'gemini-2.5-flash',
    prompt_version: str = PROMPT_VERSION,
    schema_version: str = SCHEMA_VERSION,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[OcrExtractionCache]]:
    """
    Checks persistent OCR cache and claims a lease if new extraction is required.

    Returns:
    (cached_data, lease_token, cache_record)
    - If cached_data is not None: hit exists, use without API calls (lease_token is None).
    - If cached_data is None and lease_token is not None: worker successfully claimed lease to perform extraction.
    - If both are None: another worker is actively holding a fresh lease (concurrent suppression).
    """
    try:
        cache_key = OcrExtractionCache.compute_cache_key(
            file_sha256=file_sha256,
            model_name=model_name,
            prompt_version=prompt_version,
            schema_version=schema_version,
        )
        now = timezone.now()
        lease_expires = now + timedelta(seconds=lease_seconds)
        token = str(uuid.uuid4())

        with transaction.atomic():
            cache_entry, created = OcrExtractionCache.objects.select_for_update().get_or_create(
                cache_key=cache_key,
                defaults={
                    'file_sha256': file_sha256,
                    'model_name': model_name,
                    'prompt_version': prompt_version,
                    'schema_version': schema_version,
                    'status': OcrExtractionCache.STATUS_PROCESSING,
                    'lease_token': token,
                    'lease_expires_at': lease_expires,
                    'retry_count': 1,
                    'max_retries': MAX_RETRIES,
                }
            )

            if created:
                return None, token, cache_entry

            # Check if already completed
            if cache_entry.status == OcrExtractionCache.STATUS_COMPLETED and cache_entry.extraction_data:
                logger.info("OcrExtractionCache HIT: %s (model=%s, v=%s)", file_sha256[:12], model_name, prompt_version)
                return cache_entry.extraction_data, None, cache_entry

            # Check active lease
            is_lease_active = (
                cache_entry.status == OcrExtractionCache.STATUS_PROCESSING
                and cache_entry.lease_expires_at
                and cache_entry.lease_expires_at > now
            )

            if is_lease_active:
                logger.info("OcrExtractionCache: Concurrent processing in progress for %s. Suppressing duplicate call.", file_sha256[:12])
                return None, None, cache_entry

            # Stale lease recovery or retry attempt
            if cache_entry.retry_count >= cache_entry.max_retries:
                # Terminal failure retry limit reached; reset only if stale by > 5 minutes
                if cache_entry.updated_at and (now - cache_entry.updated_at).total_seconds() < 300:
                    logger.warning("OcrExtractionCache: Max retries (%d) reached recently for %s. Last error: %s",
                                   cache_entry.max_retries, file_sha256[:12], cache_entry.last_error)
                    return None, None, cache_entry

            # Claim lease for stale/retry job
            cache_entry.status = OcrExtractionCache.STATUS_PROCESSING
            cache_entry.lease_token = token
            cache_entry.lease_expires_at = lease_expires
            cache_entry.retry_count += 1
            cache_entry.save(update_fields=['status', 'lease_token', 'lease_expires_at', 'retry_count', 'updated_at'])
            return None, token, cache_entry

    except Exception as e:
        logger.error("OcrExtractionCache claim error (non-blocking failsafe): %s", str(e), exc_info=True)
        # Failsafe: never allow cache failure to block actual OCR execution
        return None, str(uuid.uuid4()), None


def record_ocr_cache_success(
    cache_record: Optional[OcrExtractionCache],
    lease_token: Optional[str],
    extraction_data: Dict[str, Any]
) -> None:
    """Records completed OCR extraction payload atomically."""
    if not cache_record or not extraction_data:
        return
    try:
        with transaction.atomic():
            record = OcrExtractionCache.objects.select_for_update().filter(id=cache_record.id).first()
            if record:
                # Verify lease token if provided
                if lease_token and record.lease_token and record.lease_token != lease_token:
                    logger.warning("OcrExtractionCache: Lease token mismatch on success save for %s. Overwriting with completed data.", record.cache_key[:12])
                record.status = OcrExtractionCache.STATUS_COMPLETED
                record.extraction_data = extraction_data
                record.completed_at = timezone.now()
                record.lease_token = ''
                record.lease_expires_at = None
                record.last_error = ''
                record.save(update_fields=['status', 'extraction_data', 'completed_at', 'lease_token', 'lease_expires_at', 'last_error', 'updated_at'])
                logger.info("OcrExtractionCache: Persisted extraction for %s", record.file_sha256[:12])
    except Exception as e:
        logger.error("OcrExtractionCache success save error (non-blocking): %s", str(e))


def record_ocr_cache_failure(
    cache_record: Optional[OcrExtractionCache],
    lease_token: Optional[str],
    error_message: str
) -> None:
    """Records extraction failure and releases lease."""
    if not cache_record:
        return
    try:
        with transaction.atomic():
            record = OcrExtractionCache.objects.select_for_update().filter(id=cache_record.id).first()
            if record:
                status = OcrExtractionCache.STATUS_FAILED if record.retry_count >= record.max_retries else OcrExtractionCache.STATUS_PENDING
                record.status = status
                record.last_error = str(error_message)[:1000]
                record.lease_token = ''
                record.lease_expires_at = None
                record.save(update_fields=['status', 'last_error', 'lease_token', 'lease_expires_at', 'updated_at'])
                logger.warning("OcrExtractionCache: Recorded failure for %s (status=%s): %s",
                               record.file_sha256[:12], status, error_message[:120])
    except Exception as e:
        logger.error("OcrExtractionCache failure save error (non-blocking): %s", str(e))
