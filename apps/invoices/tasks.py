import os
import json
import time
import logging
import threading
import hashlib
from datetime import timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List
from decimal import Decimal

from django.conf import settings
from django.db import transaction, connection, close_old_connections
from django.utils import timezone
from django.core.files.base import ContentFile

from .models import UploadedDocument, InvoiceSubmission, InvoiceDocument, InvoiceSequence
from .db_utils import run_with_sqlite_retry
from .vendor_directory import find_vendor_by_name
from .vendor_validator import validate_vendor_and_financials, apply_vendor_validation_to_submission
from .e_invoice_utils import apply_e_invoice_to_submission
from .segmentation import segment_and_extract_pdf
from .performance_timer import PerformanceTimer
from .page_classifier import classify_document_pages, PageClassificationType

logger = logging.getLogger(__name__)

_ACTIVE_LOCK = threading.Lock()
_ACTIVE_DOCUMENT_IDS = set()
_ACTIVE_THREADS: Dict[int, threading.Thread] = {}


def _worker_lock_dir() -> Path:
    lock_dir = Path(settings.MEDIA_ROOT) / '.worker_locks'
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir


def _get_process_start_token(pid: int) -> Optional[int]:
    """
    Return a process creation timestamp token on Windows to guard against PID reuse.
    Returns None on non-Windows platforms or if the process cannot be queried.
    """
    if not pid or pid <= 0 or os.name != 'nt':
        return None
    try:
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return None
        try:
            creation_time = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel_time = wintypes.FILETIME()
            user_time = wintypes.FILETIME()
            if kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation_time),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            ):
                return (creation_time.dwHighDateTime << 32) | creation_time.dwLowDateTime
            return None
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def _is_pid_alive(pid: int, expected_start_token: Optional[int] = None) -> bool:
    """
    Cross-platform check for whether a worker process PID is still running.
    When `expected_start_token` is provided on Windows, verifies the PID has not
    been reused by a different process after a crash.
    """
    if not pid or pid <= 0:
        return False
    if os.name == 'nt':
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    if exit_code.value != STILL_ACTIVE:
                        return False
            finally:
                kernel32.CloseHandle(handle)
            if expected_start_token is not None:
                actual_token = _get_process_start_token(pid)
                if actual_token is not None and actual_token != int(expected_start_token):
                    return False
            return True
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def _is_lock_file_live(lock_file: Path) -> bool:
    """
    Inspect an existing lock file and return True if a live worker owns it.
    Cleans up confirmed stale or corrupt lock files safely.
    """
    if not lock_file.exists():
        return False
    try:
        raw = lock_file.read_text(encoding='utf-8')
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("Lock payload is not a JSON object.")
        pid = int(data.get('pid', 0))
        if pid <= 0:
            raise ValueError("Lock payload missing valid PID.")
        doc_id = int(data.get('doc_id', 0))
        start_token = data.get('process_create_time')
        if start_token is not None:
            start_token = int(start_token)
    except Exception:
        # Avoid racing with a process that just opened O_CREAT|O_EXCL milliseconds ago
        try:
            age_seconds = time.time() - lock_file.stat().st_mtime
            if age_seconds < 5.0:
                return True
            lock_file.unlink(missing_ok=True)
        except Exception:
            pass
        return False

    if pid == os.getpid():
        with _ACTIVE_LOCK:
            if doc_id in _ACTIVE_DOCUMENT_IDS:
                return True
        # Stale lock from this PID (e.g., abnormal thread termination)
        try:
            lock_file.unlink(missing_ok=True)
        except Exception:
            pass
        return False

    if _is_pid_alive(pid, expected_start_token=start_token):
        return True

    # Stale lock left behind by terminated/crashed process -> clean up
    try:
        lock_file.unlink(missing_ok=True)
    except Exception:
        logger.debug("Could not remove stale worker lock %s", lock_file, exc_info=True)
    return False


def _acquire_worker_lock(doc_id: int) -> bool:
    """
    Atomically acquire an exclusive OS lock file for `doc_id` using O_CREAT | O_EXCL.
    Returns True if acquired by this process, False if held by another live worker.
    """
    lock_file = _worker_lock_dir() / f"doc_{doc_id}.lock"
    payload = json.dumps({
        'pid': os.getpid(),
        'process_create_time': _get_process_start_token(os.getpid()),
        'doc_id': doc_id,
        'started_at': timezone.now().isoformat(),
    }).encode('utf-8')

    for _attempt in range(2):
        try:
            fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            if _is_lock_file_live(lock_file):
                return False
            # Stale lock was cleaned up; retry exclusive creation once
            continue
        except Exception:
            logger.debug("Could not acquire worker lock for UploadedDocument #%s", doc_id, exc_info=True)
            return False
    return False


def _write_worker_lock(doc_id: int) -> bool:
    """Backward-compatible alias for atomic lock acquisition."""
    return _acquire_worker_lock(doc_id)


def _release_worker_lock(doc_id: int) -> None:
    """Release the lock file only if owned by the current OS process."""
    try:
        lock_file = _worker_lock_dir() / f"doc_{doc_id}.lock"
        if not lock_file.exists():
            return
        try:
            data = json.loads(lock_file.read_text(encoding='utf-8'))
            if int(data.get('pid', 0)) not in (0, os.getpid()):
                return
        except Exception:
            pass
        lock_file.unlink(missing_ok=True)
    except Exception:
        logger.debug("Could not remove worker lock for UploadedDocument #%s", doc_id, exc_info=True)


def _has_live_external_worker(doc_id: int) -> bool:
    """Return True if another live OS process holds the worker lock for doc_id."""
    try:
        lock_file = _worker_lock_dir() / f"doc_{doc_id}.lock"
        return _is_lock_file_live(lock_file)
    except Exception:
        return False


def wait_for_active_jobs(timeout: float = 15.0) -> int:
    """Wait up to `timeout` seconds for active OCR threads to finish cleanly during shutdown."""
    deadline = time.monotonic() + max(0.0, timeout)
    with _ACTIVE_LOCK:
        threads = list(_ACTIVE_THREADS.values())
    remaining = 0
    for t in threads:
        left = deadline - time.monotonic()
        if left > 0 and t.is_alive():
            t.join(timeout=left)
        if t.is_alive():
            remaining += 1
    return remaining


def get_active_processing_ids() -> set:
    with _ACTIVE_LOCK:
        return set(_ACTIVE_DOCUMENT_IDS)


def get_incomplete_documents(stale_minutes: int = 0):
    """
    Identifies UploadedDocument records left in QUEUED or PROCESSING states
    (e.g., after an unexpected server restart) that are not actively running in
    this process or another live OS worker process.
    """
    active_ids = set(get_active_processing_ids())
    qs = UploadedDocument.objects.filter(
        processing_status__in=[UploadedDocument.STATUS_QUEUED, UploadedDocument.STATUS_PROCESSING]
    ).exclude(id__in=active_ids).order_by('created_at')
    if stale_minutes and stale_minutes > 0:
        cutoff = timezone.now() - timedelta(minutes=stale_minutes)
        qs = qs.filter(updated_at__lte=cutoff)

    live_external_ids = [doc_id for doc_id in qs.values_list('id', flat=True) if _has_live_external_worker(doc_id)]
    if live_external_ids:
        qs = qs.exclude(id__in=live_external_ids)
    return qs


def recover_incomplete_documents(stale_minutes: int = 0, requeue_only: bool = False) -> Dict[str, Any]:
    """
    Safely recovers orphaned QUEUED/PROCESSING UploadedDocument records without
    losing uploaded files, hijacking live workers, or duplicating already-created submissions.
    """
    candidate_ids = list(get_incomplete_documents(stale_minutes=stale_minutes).values_list('id', flat=True))
    summary = {
        'total_found': 0,
        'recovered_ids': [],
        'already_completed_ids': [],
        'failed_missing_ids': [],
        'skipped_active_ids': [],
    }

    for doc_id in candidate_ids:
        if doc_id in get_active_processing_ids() or _has_live_external_worker(doc_id):
            summary['skipped_active_ids'].append(doc_id)
            continue

        def _recover_single_doc() -> str:
            with transaction.atomic():
                qs = UploadedDocument.objects.filter(id=doc_id)
                if connection.vendor == 'postgresql':
                    qs = qs.select_for_update(skip_locked=True)
                doc = qs.first()
                if doc is None or doc.processing_status not in (
                    UploadedDocument.STATUS_QUEUED,
                    UploadedDocument.STATUS_PROCESSING,
                ):
                    return 'skipped'

                if InvoiceSubmission.objects.filter(source_document=doc).exists():
                    updated = UploadedDocument.objects.filter(
                        id=doc_id,
                        processing_status__in=[UploadedDocument.STATUS_QUEUED, UploadedDocument.STATUS_PROCESSING],
                    ).update(
                        processing_status=UploadedDocument.STATUS_DETECTION_COMPLETED,
                        progress_percent=100,
                        current_stage="Recovered: Submissions already generated.",
                        updated_at=timezone.now(),
                    )
                    return 'already_completed' if updated else 'skipped'

                file_exists = bool(doc.original_file and doc.original_file.storage.exists(doc.original_file.name))
                if not file_exists:
                    updated = UploadedDocument.objects.filter(
                        id=doc_id,
                        processing_status__in=[UploadedDocument.STATUS_QUEUED, UploadedDocument.STATUS_PROCESSING],
                    ).update(
                        processing_status=UploadedDocument.STATUS_FAILED,
                        error_message="Original uploaded file is missing from storage during crash recovery.",
                        current_stage="Processing Failed: Original uploaded file missing",
                        updated_at=timezone.now(),
                    )
                    return 'failed_missing' if updated else 'skipped'

                updated = UploadedDocument.objects.filter(
                    id=doc_id,
                    processing_status__in=[UploadedDocument.STATUS_QUEUED, UploadedDocument.STATUS_PROCESSING],
                ).update(
                    processing_status=UploadedDocument.STATUS_QUEUED,
                    current_stage="Recovered after server restart — queued for processing",
                    error_message='',
                    progress_percent=5,
                    updated_at=timezone.now(),
                )
                return 'recovered' if updated else 'skipped'

        outcome = run_with_sqlite_retry(_recover_single_doc)
        if outcome == 'skipped':
            summary['skipped_active_ids'].append(doc_id)
        elif outcome == 'already_completed':
            summary['total_found'] += 1
            summary['already_completed_ids'].append(doc_id)
        elif outcome == 'failed_missing':
            summary['total_found'] += 1
            summary['failed_missing_ids'].append(doc_id)
        elif outcome == 'recovered':
            summary['total_found'] += 1
            summary['recovered_ids'].append(doc_id)
            if not requeue_only:
                dispatch_background_processing(uploaded_document_id=doc_id, force_recover=True)

    return summary


def _safe_close_old_connections():
    """
    Close stale DB connections before/after background worker execution
    without breaking an outer Django TestCase atomic block.
    """
    if not connection.in_atomic_block:
        close_old_connections()


def process_uploaded_document_task(
    uploaded_doc_id: int,
    remarks: str = '',
    is_suspected: bool = False,
    duplicate_notes: str = '',
    batch_id: str = '',
    force_recover: bool = False,
):
    """
    Background worker task for processing uploaded invoice documents.
    Uses close_old_connections(), row-level deduplication locking, failure persistence,
    and authoritative backend vendor validation (Rules A-E).
    """
    _safe_close_old_connections()
    timer = PerformanceTimer(total_pages=1)
    registered_active = False
    acquired_file_lock = False

    try:
        with _ACTIVE_LOCK:
            if uploaded_doc_id in _ACTIVE_DOCUMENT_IDS:
                logger.warning(f"UploadedDocument #{uploaded_doc_id} is already actively processing in this worker.")
                return []
            if not _acquire_worker_lock(uploaded_doc_id):
                logger.warning(f"UploadedDocument #{uploaded_doc_id} is locked by another live worker process.")
                return []
            _ACTIVE_DOCUMENT_IDS.add(uploaded_doc_id)
            registered_active = True
            acquired_file_lock = True

        allowed_claim_statuses = [UploadedDocument.STATUS_UPLOADED, UploadedDocument.STATUS_QUEUED]
        if force_recover:
            allowed_claim_statuses.append(UploadedDocument.STATUS_PROCESSING)

        def _claim_document():
            with transaction.atomic():
                qs = UploadedDocument.objects.filter(id=uploaded_doc_id)
                if connection.vendor == 'postgresql':
                    qs = qs.select_for_update()
                doc = qs.get()

                existing_subs = list(InvoiceSubmission.objects.filter(source_document=doc))
                if doc.processing_status in (
                    UploadedDocument.STATUS_DETECTION_COMPLETED,
                    UploadedDocument.STATUS_COMPLETED,
                ):
                    return ('already_done', existing_subs, None)

                if existing_subs:
                    UploadedDocument.objects.filter(id=uploaded_doc_id).update(
                        processing_status=UploadedDocument.STATUS_DETECTION_COMPLETED,
                        progress_percent=100,
                        invoices_detected=len(existing_subs),
                        current_stage=f"Completed: {len(existing_subs)} invoices detected and dispatched to Naveen's queue.",
                        updated_at=timezone.now(),
                    )
                    return ('already_done', existing_subs, None)

                if doc.processing_status not in allowed_claim_statuses:
                    return ('not_claimable', [], None)

                updated = UploadedDocument.objects.filter(
                    id=uploaded_doc_id,
                    processing_status__in=allowed_claim_statuses,
                ).update(
                    processing_status=UploadedDocument.STATUS_PROCESSING,
                    error_message='',
                    current_stage=f"Document Received & Cryptographic Checksum Verified ({doc.file_sha256[:12]}...)",
                    progress_percent=10,
                    updated_at=timezone.now(),
                )
                if not updated:
                    return ('not_claimable', [], None)

                doc.refresh_from_db()
                return ('claimed', [], doc)

        claim_status, existing_subs, uploaded_doc = run_with_sqlite_retry(_claim_document)
        if claim_status == 'already_done':
            logger.info(f"Skipping duplicate processing for already completed UploadedDocument #{uploaded_doc_id}")
            return existing_subs
        if claim_status != 'claimed' or uploaded_doc is None:
            logger.warning(f"UploadedDocument #{uploaded_doc_id} could not be claimed (already processing or in invalid state).")
            return []

        file_name = uploaded_doc.file_name
        file_sha256 = uploaded_doc.file_sha256
        mime_type = uploaded_doc.content_type
        user = uploaded_doc.uploader

        if not uploaded_doc.original_file or not uploaded_doc.original_file.storage.exists(uploaded_doc.original_file.name):
            raise FileNotFoundError(f"Uploaded source file missing from storage for UploadedDocument #{uploaded_doc_id}.")

        file_path = uploaded_doc.original_file.path

        def _update_progress(**fields):
            fields['updated_at'] = timezone.now()
            run_with_sqlite_retry(
                lambda: UploadedDocument.objects.filter(id=uploaded_doc_id).update(**fields)
            )

        # Stage 2: Page classification
        import pymupdf as fitz
        doc_fitz = fitz.open(file_path)
        try:
            total_pgs = len(doc_fitz)
            if total_pgs == 0:
                raise ValueError("Uploaded PDF contains 0 readable pages.")
            timer.total_pages = max(1, total_pgs)
            uploaded_doc.total_pages = total_pgs

            _update_progress(
                total_pages=total_pgs,
                current_stage=f"Classifying {total_pgs} pages (Digital / Scanned / Mixed / Blank)...",
                progress_percent=25,
            )

            timer.start_timing('page_classification_ms')
            classifications = classify_document_pages(doc_fitz)
            timer.stop_timing('page_classification_ms')
        finally:
            doc_fitz.close()

        scanned_count = sum(1 for c in classifications if c['page_type'] == PageClassificationType.SCANNED_IMAGE)
        digital_count = sum(1 for c in classifications if c['page_type'] == PageClassificationType.DIGITAL_TEXT)
        mixed_count = sum(1 for c in classifications if c['page_type'] == PageClassificationType.MIXED)

        # Stage 3: Conditional text extraction & OCR
        _update_progress(
            current_stage=f"Extracting text & conditional OCR ({digital_count} digital, {scanned_count} scanned, {mixed_count} mixed)...",
            progress_percent=50,
        )

        # Stage 4: Segmentation & invoice boundary detection
        segments = segment_and_extract_pdf(file_path, timer=timer)
        if not segments:
            raise ValueError("No readable invoice segments could be detected in this document.")

        _update_progress(
            current_stage=f"Multi-invoice boundary detection: identified {len(segments)} invoices across {total_pgs} pages...",
            progress_percent=75,
            invoices_detected=len(segments),
            pages_processed=total_pgs,
        )

        # Stage 5: Statutory vendor validation & Database creation
        _update_progress(
            current_stage="Validating vendors against authoritative master (Rules A-E) & creating purchase records...",
            progress_percent=90,
        )

        timer.start_timing('database_ops_ms')

        if not batch_id:
            batch_id = f"BATCH-{timezone.now().strftime('%Y%m%d%H%M%S')}-{uploaded_doc.id}"

        def _persist_extracted_submissions():
            local_submissions = []
            with transaction.atomic():
                existing = list(InvoiceSubmission.objects.filter(source_document_id=uploaded_doc_id))
                if existing:
                    return existing
                for seg in segments:
                    sub_id = InvoiceSequence.get_next_submission_id()
                    meta = seg['metadata']

                    # Statutory vendor validation
                    timer.start_timing('vendor_matching_ms')
                    backend_val = validate_vendor_and_financials(
                        party_name=meta.get('party_name', ''),
                        party_gstin=meta.get('party_gstin', ''),
                        default_expense_ledger=meta.get('expense_ledger', 'Purchase Spares & Consumables'),
                    )
                    timer.stop_timing('vendor_matching_ms')

                    resolved_party_name = backend_val.get('party_name') or meta.get('party_name', '')
                    resolved_party_gstin = backend_val.get('party_gstin') or meta.get('party_gstin', '')

                    einv_manual_gate = meta.get('is_e_invoice') is True and not meta.get('irn_number')
                    sub = InvoiceSubmission.objects.create(
                        submission_id=sub_id,
                        employee=user,
                        source_document=uploaded_doc,
                        start_page=seg['start_page'],
                        end_page=seg['end_page'],
                        batch_id=batch_id,
                        remarks=remarks.strip(),
                        status=InvoiceSubmission.STATUS_SUBMITTED,
                        is_duplicate_suspected=is_suspected,
                        duplicate_notes=duplicate_notes,
                        party_name=resolved_party_name,
                        party_gstin=resolved_party_gstin,
                        invoice_number=meta.get('invoice_number', ''),
                        invoice_date=meta.get('invoice_date'),
                        taxable_amount=meta.get('taxable_amount', Decimal('0.00')),
                        cgst_amount=meta.get('cgst_amount', Decimal('0.00')),
                        sgst_amount=meta.get('sgst_amount', Decimal('0.00')),
                        igst_amount=meta.get('igst_amount', Decimal('0.00')),
                        total_amount=meta.get('total_amount', Decimal('0.00')),
                        expense_ledger=backend_val.get('expense_ledger') or meta.get('expense_ledger', 'Purchase Spares & Consumables'),
                        ocr_confidence=min(meta.get('confidence', 0.8), backend_val.get('confidence', 0.8)),
                        is_ambiguous_vendor=backend_val['is_ambiguous_vendor'],
                        requires_manual_review=backend_val['requires_manual_review'] or einv_manual_gate,
                    )
                    apply_e_invoice_to_submission(sub, meta)
                    sub.save(update_fields=['e_invoice_status', 'is_e_invoice', 'irn_number', 'requires_manual_review', 'updated_at'])

                    # Fallback party name matching from remarks if not detected, then re-validate
                    if not sub.party_name and remarks.strip():
                        sub.party_name = remarks.strip()[:100]
                        matched = find_vendor_by_name(remarks.strip())
                        if matched:
                            sub.party_name = matched['name']
                            if not sub.party_gstin:
                                sub.party_gstin = matched.get('gstin', '')
                            if matched.get('ledger'):
                                sub.expense_ledger = matched['ledger']
                        apply_vendor_validation_to_submission(sub, raw_confidence=meta.get('confidence', 0.8))
                        apply_e_invoice_to_submission(sub, meta)
                        sub.save()

                    local_submissions.append(sub)

                    # Attach document records
                    sub_files = seg.get('sub_files', {})
                    page_images = sub_files.get('page_images', [])

                    if uploaded_doc.total_pages == 1 and len(segments) == 1:
                        InvoiceDocument.objects.create(
                            submission=sub,
                            source_document=uploaded_doc,
                            start_page=1,
                            end_page=1,
                            page_number=1,
                            original_file=uploaded_doc.original_file,
                            original_filename=file_name,
                            file_sha256=file_sha256,
                            file_size_bytes=uploaded_doc.file_size_bytes,
                            content_type=mime_type,
                            version=1,
                            is_active_version=True,
                        )
                    else:
                        for p_idx, p_img in enumerate(page_images, start=1):
                            p_bytes = p_img['image_bytes']
                            p_num = p_img['page_number_in_src']
                            img_name = f"{os.path.splitext(file_name)[0]}_pg{p_num}.jpg"
                            InvoiceDocument.objects.create(
                                submission=sub,
                                source_document=uploaded_doc,
                                start_page=seg['start_page'],
                                end_page=seg['end_page'],
                                page_number=p_idx,
                                original_file=ContentFile(p_bytes, name=img_name),
                                original_filename=img_name,
                                file_sha256=hashlib.sha256(p_bytes).hexdigest(),
                                file_size_bytes=len(p_bytes),
                                content_type='image/jpeg',
                                version=1,
                                is_active_version=True,
                            )

                        pdf_bytes = sub_files.get('pdf_bytes')
                        if pdf_bytes:
                            pdf_name = f"{os.path.splitext(file_name)[0]}_inv{seg['invoice_index']}.pdf"
                            InvoiceDocument.objects.create(
                                submission=sub,
                                source_document=uploaded_doc,
                                start_page=seg['start_page'],
                                end_page=seg['end_page'],
                                page_number=len(page_images) + 1,
                                original_file=ContentFile(pdf_bytes, name=pdf_name),
                                original_filename=pdf_name,
                                file_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
                                file_size_bytes=len(pdf_bytes),
                                content_type='application/pdf',
                                version=1,
                                is_active_version=True,
                            )

            return local_submissions

        created_submissions = run_with_sqlite_retry(_persist_extracted_submissions)

        timer.stop_timing('database_ops_ms')

        perf_summary = timer.finish()
        run_with_sqlite_retry(
            lambda: UploadedDocument.objects.filter(id=uploaded_doc_id).update(
                processing_metrics=perf_summary,
                progress_percent=100,
                current_stage=f"Completed: {len(segments)} invoices detected and dispatched to Naveen's queue."[:200],
                processing_status=UploadedDocument.STATUS_DETECTION_COMPLETED,
                updated_at=timezone.now(),
            )
        )

        logger.info(
            f"Successfully processed UploadedDocument #{uploaded_doc.id} "
            f"({total_pgs} pages, {len(segments)} invoices) in {perf_summary['total_time_seconds']}s"
        )
        return created_submissions

    except Exception as e:
        logger.error(f"Error processing UploadedDocument #{uploaded_doc_id}: {str(e)}", exc_info=True)
        try:
            _safe_close_old_connections()
            run_with_sqlite_retry(
                lambda: UploadedDocument.objects.filter(id=uploaded_doc_id).update(
                    processing_status=UploadedDocument.STATUS_FAILED,
                    error_message=str(e),
                    current_stage=f"Processing Failed: {str(e)}"[:200],
                    updated_at=timezone.now(),
                )
            )
        except Exception:
            logger.exception(f"Failed updating error status for UploadedDocument #{uploaded_doc_id}")
        raise e

    finally:
        if registered_active:
            with _ACTIVE_LOCK:
                _ACTIVE_DOCUMENT_IDS.discard(uploaded_doc_id)
                _ACTIVE_THREADS.pop(uploaded_doc_id, None)
            _release_worker_lock(uploaded_doc_id)
        _safe_close_old_connections()


def dispatch_background_processing(
    uploaded_document_id: int,
    remarks: str = '',
    is_suspected: bool = False,
    duplicate_notes: str = '',
    batch_id: str = '',
    force_recover: bool = False,
):
    """
    Spawns a background thread to process the uploaded document asynchronously,
    guarding against duplicate concurrent dispatches for the same document ID.
    """
    with _ACTIVE_LOCK:
        if uploaded_document_id in _ACTIVE_DOCUMENT_IDS:
            logger.info(f"Background dispatch skipped: UploadedDocument #{uploaded_document_id} already active.")
            return None

    thread = threading.Thread(
        target=process_uploaded_document_task,
        args=(uploaded_document_id, remarks, is_suspected, duplicate_notes, batch_id, force_recover),
        daemon=True,
    )
    with _ACTIVE_LOCK:
        _ACTIVE_THREADS[uploaded_document_id] = thread
    thread.start()
    return thread
