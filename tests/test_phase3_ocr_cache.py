"""
Phase 3 Unit Tests: Persistent OCR Cache, Versioning, Leases & Concurrency Suppression
Balaji InvoiceHub
"""
import pytest
from datetime import timedelta
from django.utils import timezone
from apps.invoices.models import OcrExtractionCache
from apps.invoices.ocr_cache_service import (
    claim_or_get_cached_ocr,
    record_ocr_cache_success,
    record_ocr_cache_failure,
    PROMPT_VERSION,
    SCHEMA_VERSION,
)


@pytest.mark.django_db
class TestPhase3OcrCache:

    def test_cache_key_generation_and_version_invalidation(self):
        """Cache identity includes file SHA-256, model, prompt version, and schema version."""
        sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        key_base = OcrExtractionCache.compute_cache_key(sha, "gemini-2.5-flash", "v1.0", "v1.0")
        key_model_change = OcrExtractionCache.compute_cache_key(sha, "gemini-1.5-flash", "v1.0", "v1.0")
        key_prompt_change = OcrExtractionCache.compute_cache_key(sha, "gemini-2.5-flash", "v2.0", "v1.0")
        key_schema_change = OcrExtractionCache.compute_cache_key(sha, "gemini-2.5-flash", "v1.0", "v2.0")

        # Every version parameter must alter the composite cache key (automatic invalidation)
        assert len(key_base) == 64
        assert key_base != key_model_change
        assert key_base != key_prompt_change
        assert key_base != key_schema_change

    def test_atomic_lease_claiming_and_concurrent_suppression(self):
        """Worker 1 claims lease; concurrent Worker 2 is suppressed to avoid duplicate Gemini API calls."""
        sha = "a1b2c3d4e5f678901234567890abcdef1234567890abcdef1234567890abcdef"

        # Worker 1 claims
        cached_data, token1, record1 = claim_or_get_cached_ocr(sha, "gemini-2.5-flash")
        assert cached_data is None
        assert token1 is not None
        assert record1.status == OcrExtractionCache.STATUS_PROCESSING
        assert record1.lease_token == token1
        assert record1.lease_expires_at > timezone.now()

        # Worker 2 attempts concurrent claim
        cached_data2, token2, record2 = claim_or_get_cached_ocr(sha, "gemini-2.5-flash")
        assert cached_data2 is None
        assert token2 is None  # Suppressed!
        assert record2.id == record1.id

    def test_cache_success_persistence_and_subsequent_hits(self):
        """Successful extraction records data; subsequent calls immediately return cached payload."""
        sha = "fedcba0987654321fedcba0987654321fedcba0987654321fedcba0987654321"

        _, token, record = claim_or_get_cached_ocr(sha, "gemini-2.5-flash")
        payload = {
            'party_name': 'MURUGAN DIESEL SERVICE',
            'party_gstin': '29ABCPE1234F1Z7',
            'total_amount': '4500.00',
            'vendor_match_status': 'MATCHED_VERIFIED',
        }

        record_ocr_cache_success(record, token, payload)

        # Verify DB record state
        record.refresh_from_db()
        assert record.status == OcrExtractionCache.STATUS_COMPLETED
        assert record.lease_token == ''
        assert record.lease_expires_at is None
        assert record.extraction_data['party_name'] == 'MURUGAN DIESEL SERVICE'

        # Next worker queries cache -> HIT (0 API calls)
        cached_data, next_token, next_record = claim_or_get_cached_ocr(sha, "gemini-2.5-flash")
        assert cached_data == payload
        assert next_token is None
        assert next_record.status == OcrExtractionCache.STATUS_COMPLETED

    def test_stale_lease_recovery(self):
        """If a worker crashes and lease expires, a new worker safely claims lease for stale job."""
        sha = "11223344556677889900aabbccddeeff11223344556677889900aabbccddeeff"

        # Create expired lease record manually
        cache_key = OcrExtractionCache.compute_cache_key(sha, "gemini-2.5-flash", PROMPT_VERSION, SCHEMA_VERSION)
        stale_record = OcrExtractionCache.objects.create(
            cache_key=cache_key,
            file_sha256=sha,
            model_name="gemini-2.5-flash",
            prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
            status=OcrExtractionCache.STATUS_PROCESSING,
            lease_token="crashed-worker-uuid",
            lease_expires_at=timezone.now() - timedelta(seconds=60),  # expired 1 min ago
            retry_count=1,
        )

        # New worker arrives
        cached_data, new_token, record = claim_or_get_cached_ocr(sha, "gemini-2.5-flash")
        assert cached_data is None
        assert new_token is not None
        assert new_token != "crashed-worker-uuid"
        record.refresh_from_db()
        assert record.lease_token == new_token
        assert record.retry_count == 2
        assert record.lease_expires_at > timezone.now()

    def test_failure_recording_and_retry_limit(self):
        """Failures record error notes; exceeding max_retries marks terminal FAILED."""
        sha = "99887766554433221100ffeeddccbbaa99887766554433221100ffeeddccbbaa"

        _, token, record = claim_or_get_cached_ocr(sha, "gemini-2.5-flash")
        assert record.retry_count == 1

        record_ocr_cache_failure(record, token, "API Quota Exceeded (429 ResourceExhausted)")
        record.refresh_from_db()
        assert record.status == OcrExtractionCache.STATUS_PENDING
        assert "429" in record.last_error

        # Exhaust retries up to max_retries (3)
        record.retry_count = 3
        record.save(update_fields=['retry_count'])

        record_ocr_cache_failure(record, token, "Fatal upstream error")
        record.refresh_from_db()
        assert record.status == OcrExtractionCache.STATUS_FAILED
        assert "Fatal upstream error" in record.last_error
