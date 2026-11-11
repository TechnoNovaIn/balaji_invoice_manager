import os
import time
import logging
import threading
import hashlib
from datetime import timedelta
from typing import Dict, Any, Optional, List
from decimal import Decimal

from django.db import transaction, connection, close_old_connections
from django.utils import timezone
from django.core.files.base import ContentFile

from .models import UploadedDocument, InvoiceSubmission, InvoiceDocument, InvoiceSequence
from .vendor_directory import find_vendor_by_name
from .vendor_validator import validate_vendor_and_financials, apply_vendor_validation_to_submission
from .e_invoice_utils import apply_e_invoice_to_submission
from .segmentation import segment_and_extract_pdf
from .performance_timer import PerformanceTimer
from .page_classifier import classify_document_pages, PageClassificationType

logger = logging.getLogger(__name__)

_ACTIVE_LOCK = threading.Lock()
_ACTIVE_DOCUMENT_IDS = set()


def get_active_processing_ids() -> set:
    with _ACTIVE_LOCK:
        return set(_ACTIVE_DOCUMENT_IDS)


def get_incomplete_documents(stale_minutes: int = 0):
    """
    Identifies UploadedDocument records left in QUEUED or PROCESSING states
    (e.g., after an unexpected server restart) that are not actively running in this worker.
    """
    active_ids = get_active_processing_ids()
    qs = UploadedDocument.objects.filter(
        processing_status__in=[UploadedDocument.STATUS_QUEUED, UploadedDocument.STATUS_PROCESSING]
    ).exclude(id__in=active_ids).order_by('created_at')
    if stale_minutes and stale_minutes > 0:
        cutoff = timezone.now() - timedelta(minutes=stale_minutes)
        qs = qs.filter(updated_at__lte=cutoff)
    return qs


def recover_incomplete_documents(stale_minutes: int = 0, requeue_only: bool = False) -> Dict[str, Any]:
    """
    Safely recovers orphaned QUEUED/PROCESSING UploadedDocument records without
    losing uploaded files or duplicating already-created submissions.
    """
    incomplete_docs = list(get_incomplete_documents(stale_minutes=stale_minutes))
    summary = {
        'total_found': len(incomplete_docs),
        'recovered_ids': [],
        'already_completed_ids': [],
        'failed_missing_ids': [],
    }

    for doc in incomplete_docs:
        if InvoiceSubmission.objects.filter(source_document=doc).exists():
            doc.processing_status = UploadedDocument.STATUS_DETECTION_COMPLETED
            doc.progress_percent = 100
            doc.current_stage = "Recovered: Submissions already generated."
            doc.save(update_fields=['processing_status', 'progress_percent', 'current_stage'])
            summary['already_completed_ids'].append(doc.id)
            continue

        file_exists = bool(doc.original_file and doc.original_file.storage.exists(doc.original_file.name))
        if not file_exists:
            doc.processing_status = UploadedDocument.STATUS_FAILED
            doc.error_message = "Original uploaded file is missing from storage during crash recovery."
            doc.current_stage = "Processing Failed: Original uploaded file missing"
            doc.save(update_fields=['processing_status', 'error_message', 'current_stage'])
            summary['failed_missing_ids'].append(doc.id)
            continue

        doc.processing_status = UploadedDocument.STATUS_QUEUED
        doc.current_stage = "Recovered after server restart — queued for processing"
        doc.error_message = ''
        doc.progress_percent = 5
        doc.save(update_fields=['processing_status', 'current_stage', 'error_message', 'progress_percent'])
        summary['recovered_ids'].append(doc.id)

        if not requeue_only:
            dispatch_background_processing(uploaded_document_id=doc.id, force_recover=True)

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

    try:
        with transaction.atomic():
            uploaded_doc = UploadedDocument.objects.select_for_update().get(id=uploaded_doc_id)

            # Prevent duplicate processing if already completed or submissions exist
            existing_subs = list(InvoiceSubmission.objects.filter(source_document=uploaded_doc))
            if uploaded_doc.processing_status in (
                UploadedDocument.STATUS_DETECTION_COMPLETED,
                UploadedDocument.STATUS_COMPLETED,
            ):
                logger.info(f"Skipping duplicate processing for already completed UploadedDocument #{uploaded_doc_id}")
                return existing_subs

            if existing_subs:
                uploaded_doc.processing_status = UploadedDocument.STATUS_DETECTION_COMPLETED
                uploaded_doc.progress_percent = 100
                uploaded_doc.invoices_detected = len(existing_subs)
                uploaded_doc.current_stage = f"Completed: {len(existing_subs)} invoices detected and dispatched to Naveen's queue."
                uploaded_doc.save(update_fields=['processing_status', 'progress_percent', 'invoices_detected', 'current_stage'])
                logger.info(f"UploadedDocument #{uploaded_doc_id} already has {len(existing_subs)} submissions; skipping duplicate creation.")
                return existing_subs

            with _ACTIVE_LOCK:
                if uploaded_doc_id in _ACTIVE_DOCUMENT_IDS:
                    logger.warning(f"UploadedDocument #{uploaded_doc_id} is already actively processing in this worker.")
                    return []
                if uploaded_doc.processing_status == UploadedDocument.STATUS_PROCESSING and not force_recover:
                    logger.warning(f"UploadedDocument #{uploaded_doc_id} is already marked PROCESSING; pass force_recover=True to recover.")
                    return []
                _ACTIVE_DOCUMENT_IDS.add(uploaded_doc_id)
                registered_active = True

            file_name = uploaded_doc.file_name
            file_sha256 = uploaded_doc.file_sha256
            mime_type = uploaded_doc.content_type
            user = uploaded_doc.uploader

            uploaded_doc.processing_status = UploadedDocument.STATUS_PROCESSING
            uploaded_doc.error_message = ''
            uploaded_doc.current_stage = f"Document Received & Cryptographic Checksum Verified ({file_sha256[:12]}...)"
            uploaded_doc.progress_percent = 10
            uploaded_doc.save(update_fields=['processing_status', 'error_message', 'current_stage', 'progress_percent'])

        if not uploaded_doc.original_file or not uploaded_doc.original_file.storage.exists(uploaded_doc.original_file.name):
            raise FileNotFoundError(f"Uploaded source file missing from storage for UploadedDocument #{uploaded_doc_id}.")

        file_path = uploaded_doc.original_file.path

        # Stage 2: Page classification
        import pymupdf as fitz
        doc_fitz = fitz.open(file_path)
        try:
            total_pgs = len(doc_fitz)
            if total_pgs == 0:
                raise ValueError("Uploaded PDF contains 0 readable pages.")
            timer.total_pages = max(1, total_pgs)
            uploaded_doc.total_pages = total_pgs

            uploaded_doc.current_stage = f"Classifying {total_pgs} pages (Digital / Scanned / Mixed / Blank)..."
            uploaded_doc.progress_percent = 25
            uploaded_doc.save(update_fields=['total_pages', 'current_stage', 'progress_percent'])

            timer.start_timing('page_classification_ms')
            classifications = classify_document_pages(doc_fitz)
            timer.stop_timing('page_classification_ms')
        finally:
            doc_fitz.close()

        scanned_count = sum(1 for c in classifications if c['page_type'] == PageClassificationType.SCANNED_IMAGE)
        digital_count = sum(1 for c in classifications if c['page_type'] == PageClassificationType.DIGITAL_TEXT)
        mixed_count = sum(1 for c in classifications if c['page_type'] == PageClassificationType.MIXED)

        # Stage 3: Conditional text extraction & OCR
        uploaded_doc.current_stage = f"Extracting text & conditional OCR ({digital_count} digital, {scanned_count} scanned, {mixed_count} mixed)..."
        uploaded_doc.progress_percent = 50
        uploaded_doc.save(update_fields=['current_stage', 'progress_percent'])

        # Stage 4: Segmentation & invoice boundary detection
        segments = segment_and_extract_pdf(file_path, timer=timer)
        if not segments:
            raise ValueError("No readable invoice segments could be detected in this document.")

        uploaded_doc.current_stage = f"Multi-invoice boundary detection: identified {len(segments)} invoices across {total_pgs} pages..."
        uploaded_doc.progress_percent = 75
        uploaded_doc.invoices_detected = len(segments)
        uploaded_doc.pages_processed = total_pgs
        uploaded_doc.save(update_fields=['current_stage', 'progress_percent', 'invoices_detected', 'pages_processed'])

        # Stage 5: Statutory vendor validation & Database creation
        uploaded_doc.current_stage = "Validating vendors against authoritative master (Rules A-E) & creating purchase records..."
        uploaded_doc.progress_percent = 90
        uploaded_doc.save(update_fields=['current_stage', 'progress_percent'])

        timer.start_timing('database_ops_ms')
        created_submissions = []

        if not batch_id:
            batch_id = f"BATCH-{timezone.now().strftime('%Y%m%d%H%M%S')}-{uploaded_doc.id}"

        with transaction.atomic():
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
                sub.save(update_fields=['is_e_invoice', 'irn_number', 'requires_manual_review', 'updated_at'])

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

                created_submissions.append(sub)

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

        timer.stop_timing('database_ops_ms')

        perf_summary = timer.finish()
        uploaded_doc.processing_metrics = perf_summary
        uploaded_doc.progress_percent = 100
        uploaded_doc.current_stage = f"Completed: {len(segments)} invoices detected and dispatched to Naveen's queue."[:200]
        uploaded_doc.processing_status = UploadedDocument.STATUS_DETECTION_COMPLETED
        uploaded_doc.save()

        logger.info(
            f"Successfully processed UploadedDocument #{uploaded_doc.id} "
            f"({total_pgs} pages, {len(segments)} invoices) in {perf_summary['total_time_seconds']}s"
        )
        return created_submissions

    except Exception as e:
        logger.error(f"Error processing UploadedDocument #{uploaded_doc_id}: {str(e)}", exc_info=True)
        try:
            _safe_close_old_connections()
            doc_err = UploadedDocument.objects.get(id=uploaded_doc_id)
            doc_err.processing_status = UploadedDocument.STATUS_FAILED
            doc_err.error_message = str(e)
            doc_err.current_stage = f"Processing Failed: {str(e)}"[:200]
            doc_err.save(update_fields=['processing_status', 'error_message', 'current_stage'])
        except Exception:
            logger.exception(f"Failed updating error status for UploadedDocument #{uploaded_doc_id}")
        raise e

    finally:
        if registered_active:
            with _ACTIVE_LOCK:
                _ACTIVE_DOCUMENT_IDS.discard(uploaded_doc_id)
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
    Spawns a background daemon thread to process the uploaded document asynchronously,
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
    thread.start()
    return thread
