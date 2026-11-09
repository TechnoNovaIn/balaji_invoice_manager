import hashlib
import os
import uuid
from datetime import timedelta
from typing import List, Tuple, Dict, Any, Optional
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from .models import InvoiceSubmission, InvoiceDocument, InvoiceStatusHistory, InvoiceSequence, UploadedDocument
from .segmentation import segment_and_extract_pdf
from .reader import extract_invoice_metadata, extract_metadata_from_text
from .vendor_directory import find_vendor_by_name


# Configuration constants
MAX_FILES_PER_SUBMISSION = 10
MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024      # 100 MB
MAX_TOTAL_SIZE_BYTES = 100 * 1024 * 1024     # 100 MB

ALLOWED_MIME_TYPES = {
    'image/jpeg': ['.jpg', '.jpeg'],
    'image/png': ['.png'],
    'application/pdf': ['.pdf'],
}


def detect_mime_type_from_bytes(file_header: bytes) -> str:
    """
    Inspect magic bytes to verify file type securely without relying solely on extension.
    """
    if file_header.startswith(b'%PDF-'):
        return 'application/pdf'
    if file_header.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if file_header.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    return ''


def compute_sha256(file_obj) -> str:
    """
    Calculate cryptographic SHA-256 hash of an uploaded file.
    Resets file pointer to 0 after computation.
    """
    hasher = hashlib.sha256()
    file_obj.seek(0)
    for chunk in file_obj.chunks(chunk_size=65536):
        hasher.update(chunk)
    file_obj.seek(0)
    return hasher.hexdigest()


def format_and_orient_image(file_obj, content_type: str):
    """
    Auto-formats uploaded invoice photographs:
    1. EXIF Orientation Normalization: Automatically rotates camera photos right-side up based on smartphone sensor metadata.
    2. Dynamic Range & Readability Enhancement: Normalizes contrast and sharpness for handwritten ink, carbon receipts, and dot-matrix bills.
    3. Color standardization to high-quality RGB JPEG.
    """
    if content_type not in ('image/jpeg', 'image/png'):
        return file_obj

    try:
        from PIL import Image, ImageOps, ImageEnhance
        import io
        from django.core.files.uploadedfile import SimpleUploadedFile

        file_obj.seek(0)
        img = Image.open(file_obj)

        # 1. Rotate physically according to EXIF orientation tag
        img = ImageOps.exif_transpose(img)

        # 2. Standardize color channels
        if img.mode in ('RGBA', 'P', 'LA'):
            bg = Image.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'RGBA':
                bg.paste(img, mask=img.split()[3])
            else:
                bg.paste(img.convert('RGB'))
            img = bg
        elif img.mode != 'RGB':
            img = img.convert('RGB')

        # 3. Enhance readability for written / low contrast receipts
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(1.2)
        sharpener = ImageEnhance.Sharpness(img)
        img = sharpener.enhance(1.15)

        out_buf = io.BytesIO()
        img.save(out_buf, format='JPEG', quality=95, optimize=True)
        out_buf.seek(0)

        filename = getattr(file_obj, 'name', 'invoice_photo.jpg')
        if not filename.lower().endswith(('.jpg', '.jpeg')):
            filename = os.path.splitext(filename)[0] + '.jpg'

        formatted_file = SimpleUploadedFile(
            name=filename,
            content=out_buf.getvalue(),
            content_type='image/jpeg'
        )
        return formatted_file
    except Exception:
        file_obj.seek(0)
        return file_obj


def validate_uploaded_files(files: List[Any]) -> List[Dict[str, Any]]:
    """
    Validate uploaded files against size, count, format, and magic bytes.
    Returns list of inspected metadata dicts:
    [{'file': file_obj, 'filename': str, 'sha256': str, 'size': int, 'content_type': str}]
    """
    if not files:
        raise ValidationError("At least one invoice photograph or PDF document is required.")

    if len(files) > MAX_FILES_PER_SUBMISSION:
        raise ValidationError(
            f"You can upload a maximum of {MAX_FILES_PER_SUBMISSION} pages/files per submission. (Received {len(files)})"
        )

    total_size = 0
    validated_files = []

    for idx, f in enumerate(files, start=1):
        # Size validation per file
        size = f.size
        if size <= 0:
            raise ValidationError(f"File '{f.name}' is empty.")
        if size > MAX_FILE_SIZE_BYTES:
            raise ValidationError(
                f"File '{f.name}' ({size / (1024*1024):.2f} MB) exceeds maximum allowed size of 100 MB."
            )

        # Read first 32 bytes for magic byte verification
        f.seek(0)
        header = f.read(32)
        f.seek(0)

        mime_type = detect_mime_type_from_bytes(header)
        if not mime_type:
            raise ValidationError(
                f"File '{f.name}' is not a valid JPEG, PNG, or PDF document. Executable or unsupported formats are rejected."
            )

        # Automatically format, auto-orient and enhance uploaded pictures
        if mime_type in ('image/jpeg', 'image/png'):
            f = format_and_orient_image(f, mime_type)
            mime_type = 'image/jpeg'
            size = f.size

        total_size += size

        sha256_hash = compute_sha256(f)
        validated_files.append({
            'file': f,
            'filename': f.name,
            'sha256': sha256_hash,
            'size': size,
            'content_type': mime_type,
            'page_number': idx,
        })

    # Total aggregate size validation
    if total_size > MAX_TOTAL_SIZE_BYTES:
        raise ValidationError(
            f"Total upload size ({total_size / (1024*1024):.2f} MB) exceeds maximum allowed limit of 100 MB per submission."
        )

    return validated_files


def check_duplicates(user, validated_files: List[Dict[str, Any]]) -> Tuple[bool, str]:
    """
    Non-blocking Informational Duplicate Detection:
    Per explicit company requirements, duplicate uploads are NEVER rejected or blocked.
    Employees are permitted to upload identical invoices multiple times.
    If an identical document hash exists (from same or different employee),
    it is accepted with is_duplicate_suspected=True and detailed duplicate_notes
    for Naveen to review and decide in Tally entry.
    """
    duplicate_notes_list = []
    is_suspected = False

    for item in validated_files:
        file_hash = item['sha256']
        filename = item['filename']

        # Find existing documents with identical hash
        existing_matches = InvoiceDocument.objects.filter(
            file_sha256=file_hash
        ).select_related('submission', 'submission__employee').order_by('-created_at')[:3]

        if existing_matches.exists():
            is_suspected = True
            for match in existing_matches:
                match_user = match.submission.employee.get_full_name() or match.submission.employee.username
                duplicate_notes_list.append(
                    f"Informational Notice: Page '{filename}' matches submission {match.submission.submission_id} "
                    f"uploaded by {match_user} on {match.created_at.strftime('%Y-%m-%d %H:%M')}."
                )
        else:
            existing_uploads = UploadedDocument.objects.filter(
                file_sha256=file_hash
            ).select_related('uploader').order_by('-created_at')[:3]
            if existing_uploads.exists():
                is_suspected = True
                for match_up in existing_uploads:
                    match_user = match_up.uploader.get_full_name() or match_up.uploader.username
                    duplicate_notes_list.append(
                        f"Informational Notice: File '{filename}' matches uploaded document '{match_up.file_name}' "
                        f"uploaded by {match_user} on {match_up.created_at.strftime('%Y-%m-%d %H:%M')}."
                    )

    duplicate_notes = "\n".join(duplicate_notes_list)
    return is_suspected, duplicate_notes


@transaction.atomic
def create_invoice_submissions(
    user,
    files: List[Any],
    remarks: str = ''
) -> List[InvoiceSubmission]:
    """
    Core Multi-Invoice Detection and Processing Pipeline:
    - Handles single or multiple uploaded PDFs and images.
    - If a PDF contains multiple invoices (e.g. 30 invoices in one PDF),
      the segmentation engine identifies invoice boundaries and creates 30 independent purchase records.
    - If multiple images are uploaded, each image creates an independent purchase record with its own tracking ID.
    - Multi-page invoices within a PDF (e.g. pages 1-2) are grouped into a single record spanning those pages.
    - Every identified invoice becomes an independent row in Naveen's dashboard and Purchase Register.
    """
    if not files:
        raise ValidationError("Please select at least one invoice photograph or PDF document to upload.")

    validated_files = validate_uploaded_files(files)
    is_suspected, duplicate_notes = check_duplicates(user, validated_files)
    batch_id = f"BATCH-{timezone.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"

    created_submissions = []

    for item in validated_files:
        f_obj = item['file']
        mime_type = item['content_type']
        file_sha256 = item['sha256']
        file_name = item['filename']
        file_size = item['size']

        # 1. Create UploadedDocument entity representing the immutable source document
        uploaded_doc = UploadedDocument.objects.create(
            uploader=user,
            original_file=f_obj,
            file_name=file_name,
            file_sha256=file_sha256,
            file_size_bytes=file_size,
            content_type=mime_type,
            processing_status=UploadedDocument.STATUS_PROCESSING,
        )

        # 2. PDF Processing: Multi-Invoice Boundary Detection & Segmentation
        if mime_type == 'application/pdf' or file_name.lower().endswith('.pdf'):
            segments = []
            try:
                segments = segment_and_extract_pdf(uploaded_doc.original_file.path)
            except Exception as e:
                uploaded_doc.processing_status = UploadedDocument.STATUS_FAILED
                uploaded_doc.error_message = f"Segmentation failed: {str(e)}"
                uploaded_doc.save()

            if segments:
                uploaded_doc.total_pages = max(s['end_page'] for s in segments)
                uploaded_doc.pages_processed = uploaded_doc.total_pages
                uploaded_doc.invoices_detected = len(segments)
                uploaded_doc.processing_status = UploadedDocument.STATUS_DETECTION_COMPLETED
                uploaded_doc.save()

                for seg in segments:
                    sub_id = InvoiceSequence.get_next_submission_id()
                    meta = seg['metadata']

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
                        party_name=meta.get('party_name', ''),
                        party_gstin=meta.get('party_gstin', ''),
                        invoice_number=meta.get('invoice_number', '') or sub_id,
                        invoice_date=meta.get('invoice_date'),
                        taxable_amount=meta.get('taxable_amount', Decimal('0.00')),
                        cgst_amount=meta.get('cgst_amount', Decimal('0.00')),
                        sgst_amount=meta.get('sgst_amount', Decimal('0.00')),
                        igst_amount=meta.get('igst_amount', Decimal('0.00')),
                        total_amount=meta.get('total_amount', Decimal('0.00')),
                        expense_ledger=meta.get('expense_ledger', 'Purchase Spares & Consumables'),
                        ocr_confidence=meta.get('confidence', 0.0),
                        is_ambiguous_vendor=meta.get('is_ambiguous_vendor', True),
                        requires_manual_review=True,
                    )

                    # Fallback party name matching from remarks if not detected
                    if not sub.party_name and remarks.strip():
                        sub.party_name = remarks.strip()[:100]
                        matched = find_vendor_by_name(remarks.strip())
                        if matched:
                            sub.party_name = matched['name']
                            if not sub.party_gstin:
                                sub.party_gstin = matched['gstin']
                            if matched.get('ledger'):
                                sub.expense_ledger = matched['ledger']
                        sub.save()

                    # Attach document records
                    sub_files = seg.get('sub_files', {})
                    page_images = sub_files.get('page_images', [])

                    if uploaded_doc.total_pages == 1 and len(segments) == 1:
                        # For single-page PDF, keep original file as document 1
                        InvoiceDocument.objects.create(
                            submission=sub,
                            source_document=uploaded_doc,
                            start_page=1,
                            end_page=1,
                            page_number=1,
                            original_file=uploaded_doc.original_file,
                            original_filename=file_name,
                            file_sha256=file_sha256,
                            file_size_bytes=file_size,
                            content_type=mime_type,
                            version=1,
                            is_active_version=True,
                        )
                        for p_idx, p_img in enumerate(page_images, start=2):
                            p_bytes = p_img['image_bytes']
                            p_num = p_img['page_number_in_src']
                            img_name = f"{os.path.splitext(file_name)[0]}_pg{p_num}.jpg"
                            InvoiceDocument.objects.create(
                                submission=sub,
                                source_document=uploaded_doc,
                                start_page=1,
                                end_page=1,
                                page_number=p_idx,
                                original_file=ContentFile(p_bytes, name=img_name),
                                original_filename=img_name,
                                file_sha256=hashlib.sha256(p_bytes).hexdigest(),
                                file_size_bytes=len(p_bytes),
                                content_type='image/jpeg',
                                version=1,
                                is_active_version=True,
                            )
                    else:
                        # Multi-invoice or multi-page PDF: attach rendered images and excerpt PDF
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

                    InvoiceStatusHistory.objects.create(
                        submission=sub,
                        old_status='',
                        new_status=InvoiceSubmission.STATUS_SUBMITTED,
                        changed_by=user,
                        notes=f'Extracted invoice #{seg["invoice_index"]} from {file_name} (pages {seg["start_page"]}–{seg["end_page"]}).'
                    )
                    created_submissions.append(sub)

            else:
                # Single-page or non-segmented PDF fallback
                sub_id = InvoiceSequence.get_next_submission_id()
                sub = InvoiceSubmission.objects.create(
                    submission_id=sub_id,
                    employee=user,
                    source_document=uploaded_doc,
                    start_page=1,
                    end_page=1,
                    batch_id=batch_id,
                    remarks=remarks.strip(),
                    status=InvoiceSubmission.STATUS_SUBMITTED,
                    is_duplicate_suspected=is_suspected,
                    duplicate_notes=duplicate_notes,
                    invoice_number=sub_id,
                )
                doc_rec = InvoiceDocument.objects.create(
                    submission=sub,
                    source_document=uploaded_doc,
                    start_page=1,
                    end_page=1,
                    page_number=1,
                    original_file=uploaded_doc.original_file,
                    original_filename=file_name,
                    file_sha256=file_sha256,
                    file_size_bytes=file_size,
                    content_type=mime_type,
                    version=1,
                    is_active_version=True,
                )
                meta = extract_invoice_metadata(doc_rec.original_file.path, mime_type)
                if meta.get('invoice_number'):
                    sub.invoice_number = meta['invoice_number']
                if meta.get('party_name'):
                    sub.party_name = meta['party_name']
                if meta.get('party_gstin'):
                    sub.party_gstin = meta['party_gstin']
                if meta.get('invoice_date'):
                    sub.invoice_date = meta['invoice_date']
                if meta.get('taxable_amount'):
                    sub.taxable_amount = meta['taxable_amount']
                if meta.get('cgst_amount'):
                    sub.cgst_amount = meta['cgst_amount']
                if meta.get('sgst_amount'):
                    sub.sgst_amount = meta['sgst_amount']
                if meta.get('igst_amount'):
                    sub.igst_amount = meta['igst_amount']
                if meta.get('total_amount'):
                    sub.total_amount = meta['total_amount']
                if meta.get('expense_ledger'):
                    sub.expense_ledger = meta['expense_ledger']
                if meta.get('confidence'):
                    sub.ocr_confidence = meta['confidence']
                sub.save()

                InvoiceStatusHistory.objects.create(
                    submission=sub,
                    old_status='',
                    new_status=InvoiceSubmission.STATUS_SUBMITTED,
                    changed_by=user,
                    notes='Single PDF invoice submission.'
                )
                created_submissions.append(sub)

        # 3. Image Processing: Independent invoice record per photo
        else:
            uploaded_doc.total_pages = 1
            uploaded_doc.pages_processed = 1
            uploaded_doc.invoices_detected = 1
            uploaded_doc.processing_status = UploadedDocument.STATUS_DETECTION_COMPLETED
            uploaded_doc.save()

            sub_id = InvoiceSequence.get_next_submission_id()
            sub = InvoiceSubmission.objects.create(
                submission_id=sub_id,
                employee=user,
                source_document=uploaded_doc,
                start_page=1,
                end_page=1,
                batch_id=batch_id,
                remarks=remarks.strip(),
                status=InvoiceSubmission.STATUS_SUBMITTED,
                is_duplicate_suspected=is_suspected,
                duplicate_notes=duplicate_notes,
                invoice_number=sub_id,
            )

            doc_rec = InvoiceDocument.objects.create(
                submission=sub,
                source_document=uploaded_doc,
                start_page=1,
                end_page=1,
                page_number=1,
                original_file=uploaded_doc.original_file,
                original_filename=file_name,
                file_sha256=file_sha256,
                file_size_bytes=file_size,
                content_type=mime_type,
                version=1,
                is_active_version=True,
            )

            meta = extract_invoice_metadata(doc_rec.original_file.path, mime_type)
            if meta.get('invoice_number'):
                sub.invoice_number = meta['invoice_number']
            if meta.get('party_name'):
                sub.party_name = meta['party_name']
            if meta.get('party_gstin'):
                sub.party_gstin = meta['party_gstin']
            if meta.get('invoice_date'):
                sub.invoice_date = meta['invoice_date']
            if meta.get('taxable_amount'):
                sub.taxable_amount = meta['taxable_amount']
            if meta.get('cgst_amount'):
                sub.cgst_amount = meta['cgst_amount']
            if meta.get('sgst_amount'):
                sub.sgst_amount = meta['sgst_amount']
            if meta.get('igst_amount'):
                sub.igst_amount = meta['igst_amount']
            if meta.get('total_amount'):
                sub.total_amount = meta['total_amount']
            if meta.get('expense_ledger'):
                sub.expense_ledger = meta['expense_ledger']
            if meta.get('confidence'):
                sub.ocr_confidence = meta['confidence']

            if not sub.party_name and remarks.strip():
                sub.party_name = remarks.strip()[:100]
                matched = find_vendor_by_name(remarks.strip())
                if matched:
                    sub.party_name = matched['name']
                    if not sub.party_gstin:
                        sub.party_gstin = matched['gstin']
                    if matched.get('ledger'):
                        sub.expense_ledger = matched['ledger']

            sub.save()

            InvoiceStatusHistory.objects.create(
                submission=sub,
                old_status='',
                new_status=InvoiceSubmission.STATUS_SUBMITTED,
                changed_by=user,
                notes='Photographed invoice submission.'
            )
            created_submissions.append(sub)

    return created_submissions


@transaction.atomic
def create_invoice_submission(
    user,
    files: List[Any],
    remarks: str = ''
) -> InvoiceSubmission:
    """
    Creates an InvoiceSubmission entity.
    If multiple files are passed directly to this singular function,
    attaches all validated files to this single submission (e.g. multi-page scan).
    If a single file is passed, utilizes the multi-invoice pipeline.
    """
    if not files:
        raise ValidationError("Please select at least one invoice document.")

    if len(files) == 1:
        subs = create_invoice_submissions(user=user, files=files, remarks=remarks)
        return subs[0] if subs else None

    # Multi-file single submission (manual multi-file attachment)
    validated_files = validate_uploaded_files(files)
    is_suspected, duplicate_notes = check_duplicates(user, validated_files)
    submission_id = InvoiceSequence.get_next_submission_id()

    submission = InvoiceSubmission.objects.create(
        submission_id=submission_id,
        employee=user,
        remarks=remarks.strip(),
        status=InvoiceSubmission.STATUS_SUBMITTED,
        is_duplicate_suspected=is_suspected,
        duplicate_notes=duplicate_notes,
    )

    for item in validated_files:
        InvoiceDocument.objects.create(
            submission=submission,
            page_number=item['page_number'],
            original_file=item['file'],
            original_filename=item['filename'],
            file_sha256=item['sha256'],
            file_size_bytes=item['size'],
            content_type=item['content_type'],
            version=1,
            is_active_version=True,
        )

    first_doc = submission.documents.filter(is_active_version=True).order_by('page_number').first()
    if first_doc and first_doc.original_file:
        try:
            meta = extract_invoice_metadata(first_doc.original_file.path, first_doc.content_type)
            if meta.get('invoice_number'):
                submission.invoice_number = meta['invoice_number']
            if meta.get('party_name'):
                submission.party_name = meta['party_name']
            if meta.get('party_gstin'):
                submission.party_gstin = meta['party_gstin']
            if meta.get('invoice_date'):
                submission.invoice_date = meta['invoice_date']
            if meta.get('taxable_amount'):
                submission.taxable_amount = meta['taxable_amount']
            if meta.get('cgst_amount'):
                submission.cgst_amount = meta['cgst_amount']
            if meta.get('sgst_amount'):
                submission.sgst_amount = meta['sgst_amount']
            if meta.get('igst_amount'):
                submission.igst_amount = meta['igst_amount']
            if meta.get('total_amount'):
                submission.total_amount = meta['total_amount']
            if meta.get('expense_ledger'):
                submission.expense_ledger = meta['expense_ledger']
            if meta.get('confidence'):
                submission.ocr_confidence = meta['confidence']
        except Exception:
            pass

    if not submission.party_name and remarks.strip():
        submission.party_name = remarks.strip()[:100]
        try:
            matched = find_vendor_by_name(remarks.strip())
            if matched:
                submission.party_name = matched['name']
                if not submission.party_gstin:
                    submission.party_gstin = matched['gstin']
                if matched.get('ledger'):
                    submission.expense_ledger = matched['ledger']
        except Exception:
            pass

    if not submission.invoice_number:
        submission.invoice_number = submission.submission_id

    submission.save()

    InvoiceStatusHistory.objects.create(
        submission=submission,
        old_status='',
        new_status=InvoiceSubmission.STATUS_SUBMITTED,
        changed_by=user,
        notes='Initial employee invoice submission.'
    )
    return submission


@transaction.atomic
def split_invoice_submission(
    submission: InvoiceSubmission,
    split_at_page: int,
    user
) -> Tuple[InvoiceSubmission, InvoiceSubmission]:
    """
    Facility for Naveen to split an incorrectly grouped multi-page invoice into two independent records.
    Example: Pages 1-4 split at page 3 -> Submission A (pages 1-2) and Submission B (pages 3-4).
    """
    if split_at_page <= submission.start_page or split_at_page > submission.end_page:
        raise ValidationError(f"Split page {split_at_page} must be between {submission.start_page + 1} and {submission.end_page}.")

    old_end_page = submission.end_page
    submission.end_page = split_at_page - 1
    submission.save(update_fields=['end_page', 'updated_at'])

    # Create new submission for the split portion
    new_sub_id = InvoiceSequence.get_next_submission_id()
    new_sub = InvoiceSubmission.objects.create(
        submission_id=new_sub_id,
        employee=submission.employee,
        source_document=submission.source_document,
        start_page=split_at_page,
        end_page=old_end_page,
        batch_id=submission.batch_id,
        remarks=f"Split from {submission.submission_id} (pages {split_at_page}–{old_end_page})",
        status=InvoiceSubmission.STATUS_SUBMITTED,
        invoice_number=new_sub_id,
        party_name=submission.party_name,
        party_gstin=submission.party_gstin,
        expense_ledger=submission.expense_ledger,
    )

    # Re-assign or split documents
    for doc in submission.documents.filter(is_active_version=True):
        if doc.page_number >= split_at_page:
            doc.submission = new_sub
            doc.page_number = doc.page_number - split_at_page + 1
            doc.save(update_fields=['submission', 'page_number'])

    InvoiceStatusHistory.objects.create(
        submission=submission,
        old_status=submission.status,
        new_status=submission.status,
        changed_by=user,
        notes=f"Split by Naveen: adjusted page range to {submission.start_page}–{submission.end_page}. Remaining pages moved to {new_sub_id}."
    )

    InvoiceStatusHistory.objects.create(
        submission=new_sub,
        old_status='',
        new_status=InvoiceSubmission.STATUS_SUBMITTED,
        changed_by=user,
        notes=f"Created via split from {submission.submission_id} by Naveen."
    )

    return submission, new_sub


@transaction.atomic
def merge_invoice_submissions(
    target_submission: InvoiceSubmission,
    source_submission: InvoiceSubmission,
    user
) -> InvoiceSubmission:
    """
    Facility for Naveen to merge two separate submissions that belong to the same invoice.
    Combines documents and extends the page range.
    """
    if target_submission.pk == source_submission.pk:
        raise ValidationError("Cannot merge an invoice into itself.")

    # Extend target page range
    target_submission.start_page = min(target_submission.start_page, source_submission.start_page)
    target_submission.end_page = max(target_submission.end_page, source_submission.end_page)

    # Reassign documents
    max_pg = target_submission.documents.filter(is_active_version=True).count()
    for doc in source_submission.documents.filter(is_active_version=True).order_by('page_number'):
        max_pg += 1
        doc.submission = target_submission
        doc.page_number = max_pg
        doc.save(update_fields=['submission', 'page_number'])

    target_submission.save(update_fields=['start_page', 'end_page', 'updated_at'])

    InvoiceStatusHistory.objects.create(
        submission=target_submission,
        old_status=target_submission.status,
        new_status=target_submission.status,
        changed_by=user,
        notes=f"Merged with submission {source_submission.submission_id} (pages {target_submission.start_page}–{target_submission.end_page})."
    )

    # Delete or cancel the merged source
    source_submission.delete()
    return target_submission


@transaction.atomic
def resubmit_returned_invoice(
    submission: InvoiceSubmission,
    user,
    new_files: List[Any] = None,
    new_remarks: str = ''
) -> InvoiceSubmission:
    """
    Handle resubmission of an invoice previously returned by Naveen.
    Permitted only when status is RETURNED.
    Retains previous documents in history, marks new documents as active version.
    """
    if submission.status != InvoiceSubmission.STATUS_RETURNED:
        raise ValidationError("Only returned invoices can be edited or resubmitted.")

    if submission.employee != user and not user.is_staff:
        raise ValidationError("You do not have permission to resubmit this invoice.")

    # Determine next version number
    current_max_version = submission.documents.all().order_by('-version').values_list('version', flat=True).first() or 1
    next_version = current_max_version + 1

    # If new files are provided, validate them and rotate active versions
    if new_files:
        validated_files = validate_uploaded_files(new_files)
        is_suspected, duplicate_notes = check_duplicates(user, validated_files)

        # Deactivate previous active documents (preserve in storage and db)
        submission.documents.filter(is_active_version=True).update(is_active_version=False)

        # Add new active documents with incremented version
        for item in validated_files:
            InvoiceDocument.objects.create(
                submission=submission,
                page_number=item['page_number'],
                original_file=item['file'],
                original_filename=item['filename'],
                file_sha256=item['sha256'],
                file_size_bytes=item['size'],
                content_type=item['content_type'],
                version=next_version,
                is_active_version=True,
            )

        if is_suspected:
            submission.is_duplicate_suspected = True
            if submission.duplicate_notes:
                submission.duplicate_notes += f"\n[Resubmission v{next_version}]: " + duplicate_notes
            else:
                submission.duplicate_notes = duplicate_notes

    # Update remarks if changed
    if new_remarks is not None:
        submission.remarks = new_remarks.strip()

    old_status = submission.status
    submission.status = InvoiceSubmission.STATUS_RESUBMITTED
    submission.save()

    InvoiceStatusHistory.objects.create(
        submission=submission,
        old_status=old_status,
        new_status=InvoiceSubmission.STATUS_RESUBMITTED,
        changed_by=user,
        notes=f"Resubmitted by employee (version {next_version if new_files else current_max_version})."
    )

    return submission




