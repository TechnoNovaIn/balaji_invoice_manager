import os
import csv
from decimal import Decimal
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError, PermissionDenied
from django.contrib import messages
from django.http import FileResponse, Http404, HttpResponseForbidden, HttpResponse, JsonResponse
from datetime import datetime
from django.utils import timezone
from django.db.models import Q, Sum

from apps.accounts.models import UserRole
from apps.accounts.permissions import role_required, check_user_role
from .models import (
    InvoiceSubmission,
    InvoiceDocument,
    InvoiceStatusHistory,
    PurchaseAuditTrail,
    TallyLedgerMapping,
    ExportAuditLog,
    UploadedDocument,
)
from .tally_exporter import generate_tally_xml
from .gst_converter import create_gst_portal_excel, convert_uploaded_excel_to_gst_format
from .purchase_service import (
    filter_purchase_register,
    record_field_corrections,
    compare_with_gstr2a,
)
from .e_invoice_utils import (
    is_valid_irn,
    normalize_irn,
    parse_e_invoice_choice,
    apply_e_invoice_to_submission,
)
from .tally_excel_exporter import generate_tally_structured_excel
from .analytics_service import (
    get_executive_kpis,
    get_monthly_trends,
    get_top_suppliers,
    get_ledger_distribution,
    get_employee_upload_stats,
    generate_executive_excel_report,
)
from .vendor_validator import validate_vendor_and_financials, apply_vendor_validation_to_submission
from django.core.paginator import Paginator
import uuid
from .tasks import dispatch_background_processing, get_active_processing_ids
from .services import (
    create_invoice_submission,
    create_invoice_submissions,
    validate_uploaded_files,
    check_duplicates,
    resubmit_returned_invoice,
    split_invoice_submission,
    merge_invoice_submissions,
    MAX_FILES_PER_SUBMISSION,
    MAX_FILE_SIZE_BYTES,
    MAX_TOTAL_SIZE_BYTES,
)


@login_required
def upload_invoice(request):
    """
    Mobile-first invoice upload interface for Employees.
    Allows capturing photos or selecting PDF/image files, entering optional remarks,
    and submitting directly to Naveen's queue.
    Creates an independent invoice submission for each uploaded document.
    """
    # Restrict to Employees and Admins (Admins permitted for operational testing)
    if not (request.user.is_employee or request.user.is_admin_role):
        raise PermissionDenied("Access Denied: Only field employees can submit invoice documents.")

    if request.method == 'POST':
        files = request.FILES.getlist('files')
        remarks = request.POST.get('remarks', '')

        # Check for background vs synchronous processing
        # Multi-page PDFs (e.g. 33-page PDF) are processed asynchronously in the background
        # Single images or requests explicitly setting sync=true process synchronously
        is_pdf = any(f.name.lower().endswith('.pdf') for f in files)
        sync_requested = request.POST.get('sync', '').lower() == 'true' or not is_pdf

        if not sync_requested and is_pdf and len(files) == 1:
            try:
                validated_files = validate_uploaded_files(files)
                is_suspected, duplicate_notes = check_duplicates(request.user, validated_files)
                item = validated_files[0]
                uploaded_doc = UploadedDocument.objects.create(
                    uploader=request.user,
                    original_file=item['file'],
                    file_name=item['filename'],
                    file_sha256=item['sha256'],
                    file_size_bytes=item['size'],
                    content_type=item['content_type'],
                    processing_status=UploadedDocument.STATUS_QUEUED,
                    current_stage="Document Queued for Background Processing",
                    progress_percent=5,
                )
                batch_id = f"BATCH-{timezone.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
                dispatch_background_processing(
                    uploaded_document_id=uploaded_doc.id,
                    remarks=remarks,
                    is_suspected=is_suspected,
                    duplicate_notes=duplicate_notes,
                    batch_id=batch_id
                )
                return redirect('invoices:upload_progress', document_id=uploaded_doc.id)
            except ValidationError as e:
                messages.error(request, e.message if hasattr(e, 'message') else str(e))
                return render(request, 'invoices/upload.html', {
                    'remarks': remarks,
                    'max_files': MAX_FILES_PER_SUBMISSION,
                    'page_title': 'Upload Invoice Receipt',
                })
            except Exception as e:
                messages.error(request, f"An unexpected error occurred while processing your upload: {str(e)}")
                return render(request, 'invoices/upload.html', {
                    'remarks': remarks,
                    'max_files': MAX_FILES_PER_SUBMISSION,
                    'page_title': 'Upload Invoice Receipt',
                })

        try:
            submissions = create_invoice_submissions(
                user=request.user,
                files=files,
                remarks=remarks
            )
            count = len(submissions)
            if count == 1:
                sub = submissions[0]
                messages.success(
                    request,
                    f"Invoice successfully submitted! Tracking ID: {sub.submission_id}"
                )
                return redirect('invoices:submission_success', submission_id=sub.submission_id)
            else:
                ids = [s.submission_id for s in submissions]
                messages.success(
                    request,
                    f"✓ Successfully submitted {count} separate invoices to Naveen! Tracking IDs: {', '.join(ids)}"
                )
                return redirect('invoices:submission_success', submission_id=submissions[0].submission_id)
        except ValidationError as e:
            messages.error(request, e.message if hasattr(e, 'message') else str(e))
            return render(request, 'invoices/upload.html', {
                'remarks': remarks,
                'max_files': MAX_FILES_PER_SUBMISSION,
                'page_title': 'Upload Invoice Receipt',
            })
        except Exception as e:
            messages.error(request, f"An unexpected error occurred while processing your upload: {str(e)}")
            return render(request, 'invoices/upload.html', {
                'remarks': remarks,
                'max_files': MAX_FILES_PER_SUBMISSION,
                'page_title': 'Upload Invoice Receipt',
            })

    return render(request, 'invoices/upload.html', {
        'max_files': MAX_FILES_PER_SUBMISSION,
        'page_title': 'Upload Invoice Receipt',
    })


@login_required
def upload_progress(request, document_id):
    """
    Real-time progress dashboard for multi-page PDF processing.
    """
    doc = get_object_or_404(UploadedDocument, id=document_id)
    if request.user.is_employee and doc.uploader != request.user:
        raise PermissionDenied("Access Denied: You cannot view this upload progress.")
    return render(request, 'invoices/upload_progress.html', {
        'document': doc,
        'page_title': f"Processing Document - {doc.file_name}",
    })


@login_required
def document_status_api(request, document_id):
    """
    JSON endpoint returning live progress percentage, stage, and detected invoices.
    """
    doc = get_object_or_404(UploadedDocument, id=document_id)
    if request.user.is_employee and doc.uploader != request.user:
        return JsonResponse({'error': 'Unauthorized'}, status=403)

    subs = InvoiceSubmission.objects.filter(source_document=doc).order_by('id')
    redirect_url = ""
    if doc.processing_status in (UploadedDocument.STATUS_DETECTION_COMPLETED, UploadedDocument.STATUS_COMPLETED):
        if subs.exists():
            redirect_url = reverse('invoices:submission_success', kwargs={'submission_id': subs.first().submission_id})
        else:
            redirect_url = reverse('invoices:my_submissions')

    active_ids = get_active_processing_ids()
    is_stale = (
        doc.processing_status in (UploadedDocument.STATUS_QUEUED, UploadedDocument.STATUS_PROCESSING)
        and doc.id not in active_ids
        and (timezone.now() - doc.updated_at).total_seconds() > 120
    )

    return JsonResponse({
        'id': doc.id,
        'status': doc.processing_status,
        'stage': doc.current_stage,
        'progress_percent': doc.progress_percent,
        'total_pages': doc.total_pages,
        'pages_processed': doc.pages_processed,
        'invoices_detected': doc.invoices_detected,
        'error_message': doc.error_message,
        'redirect_url': redirect_url,
        'metrics': doc.processing_metrics,
        'is_stale': is_stale,
    })


@login_required
def submission_success(request, submission_id):
    """
    Confirmation screen displayed immediately after a successful upload.
    Shows the tracking ID, timestamp, and document overview.
    """
    submission = get_object_or_404(
        InvoiceSubmission.objects.select_related('employee'),
        submission_id=submission_id
    )

    # Ownership check: Employee can view own, Naveen/Admin/Management can view all
    if request.user.is_employee and submission.employee != request.user:
        raise PermissionDenied("Access Denied: You cannot view another employee's submission receipt.")

    return render(request, 'invoices/success.html', {
        'submission': submission,
        'page_title': f"Submission Receipt - {submission.submission_id}",
    })


@login_required
def my_submissions(request):
    """
    List of invoice submissions.
    - Employees can strictly view ONLY their own submissions.
    - Naveen, Admin, and Management can view all submissions with employee filtering.
    """
    query = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status', '').strip()

    if request.user.is_employee:
        submissions = InvoiceSubmission.objects.filter(employee=request.user)
    else:
        # Operator / Management / Admin view
        submissions = InvoiceSubmission.objects.all().select_related('employee')

    if query:
        submissions = submissions.filter(
            Q(submission_id__icontains=query) |
            Q(remarks__icontains=query) |
            Q(employee__first_name__icontains=query) |
            Q(employee__last_name__icontains=query) |
            Q(employee__username__icontains=query)
        )

    if status_filter:
        submissions = submissions.filter(status=status_filter)

    submissions = submissions.prefetch_related('documents').order_by('-created_at')

    # Status counts for filter chips
    if request.user.is_employee:
        base_qs = InvoiceSubmission.objects.filter(employee=request.user)
    else:
        base_qs = InvoiceSubmission.objects.all()

    counts = {
        'all': base_qs.count(),
        'submitted': base_qs.filter(status=InvoiceSubmission.STATUS_SUBMITTED).count(),
        'returned': base_qs.filter(status=InvoiceSubmission.STATUS_RETURNED).count(),
        'resubmitted': base_qs.filter(status=InvoiceSubmission.STATUS_RESUBMITTED).count(),
        'verified': base_qs.filter(status=InvoiceSubmission.STATUS_VERIFIED).count(),
    }

    return render(request, 'invoices/my_submissions.html', {
        'submissions': submissions,
        'query': query,
        'status_filter': status_filter,
        'counts': counts,
        'page_title': 'My Submissions' if request.user.is_employee else 'Invoice Submissions Registry',
    })


@login_required
def submission_detail(request, submission_id):
    """
    Detailed view of an invoice submission with active and historical documents,
    status audit log, and return reasons.
    Strictly isolated: An employee can only view their own submissions.
    """
    submission = get_object_or_404(
        InvoiceSubmission.objects.select_related('employee'),
        submission_id=submission_id
    )

    # RBAC Isolation Check
    if request.user.is_employee and submission.employee != request.user:
        raise PermissionDenied("Access Denied: You are not authorized to view another employee's invoice.")

    active_docs = submission.documents.filter(is_active_version=True).order_by('page_number')
    historical_docs = submission.documents.filter(is_active_version=False).order_by('-version', 'page_number')
    status_history = submission.status_history.select_related('changed_by').order_by('-created_at')

    # Sequential stream navigation for Naveen
    prev_sub = None
    next_sub = None
    if request.user.is_naveen or request.user.is_admin_role or request.user.is_management:
        prev_sub = InvoiceSubmission.objects.filter(created_at__gt=submission.created_at).order_by('created_at').first()
        next_sub = InvoiceSubmission.objects.filter(created_at__lt=submission.created_at).order_by('-created_at').first()

    return render(request, 'invoices/submission_detail.html', {
        'submission': submission,
        'active_docs': active_docs,
        'historical_docs': historical_docs,
        'status_history': status_history,
        'prev_sub': prev_sub,
        'next_sub': next_sub,
        'can_edit': submission.can_be_edited and (submission.employee == request.user or request.user.is_admin_role),
        'page_title': f"Invoice {submission.submission_id}",
    })


@login_required
def resubmit_invoice(request, submission_id):
    """
    Allows employee to edit and resubmit an invoice returned by Naveen.
    Gated strictly to returned status and the owning employee.
    """
    submission = get_object_or_404(InvoiceSubmission, submission_id=submission_id)

    # Permission checks
    if submission.employee != request.user and not request.user.is_admin_role:
        raise PermissionDenied("Access Denied: You cannot edit another employee's submission.")

    if not submission.can_be_edited:
        messages.error(request, "This invoice is not in a return status and cannot be edited.")
        return redirect('invoices:submission_detail', submission_id=submission.submission_id)

    if request.method == 'POST':
        new_files = request.FILES.getlist('files')
        new_remarks = request.POST.get('remarks', '')

        try:
            resubmit_returned_invoice(
                submission=submission,
                user=request.user,
                new_files=new_files if new_files else None,
                new_remarks=new_remarks
            )
            messages.success(
                request,
                f"Invoice {submission.submission_id} has been resubmitted to Naveen for review."
            )
            return redirect('invoices:submission_detail', submission_id=submission.submission_id)
        except ValidationError as e:
            messages.error(request, e.message if hasattr(e, 'message') else str(e))
        except Exception as e:
            messages.error(request, f"Error resubmitting invoice: {str(e)}")

    active_docs = submission.documents.filter(is_active_version=True).order_by('page_number')

    return render(request, 'invoices/resubmit.html', {
        'submission': submission,
        'active_docs': active_docs,
        'max_files': MAX_FILES_PER_SUBMISSION,
        'page_title': f"Resubmit Invoice - {submission.submission_id}",
    })


@login_required
def serve_document(request, doc_id):
    """
    Secure authenticated document serving endpoint.
    Verifies user has permission to view the document:
    - Must be the submitting employee OR
    - Operator (Naveen), Admin, or Management.
    Prevents unauthorized direct file access.
    """
    doc = get_object_or_404(
        InvoiceDocument.objects.select_related('submission', 'submission__employee'),
        pk=doc_id
    )

    # Access control check
    user = request.user
    if user.is_employee and doc.submission.employee != user:
        raise PermissionDenied("Access Denied: You do not have permission to view this document.")

    # Check file existence on disk
    if not doc.original_file or not doc.original_file.storage.exists(doc.original_file.name):
        raise Http404("Document file not found on server storage.")

    try:
        file_handle = doc.original_file.open('rb')
    except Exception as e:
        raise Http404(f"Could not open document file: {str(e)}")

    # Return response inline (for viewing in browser modal/tab)
    response = FileResponse(file_handle, content_type=doc.content_type)
    # Inline disposition allows viewing in browser PDF reader or image viewer
    response['Content-Disposition'] = f'inline; filename="{os.path.basename(doc.original_filename)}"'
    return response


@login_required
def naveen_return_invoice(request, submission_id):
    """
    Endpoint for Naveen (or Admin) to return an invoice to the employee for correction.
    Requires a non-empty return reason.
    """
    if not (request.user.is_naveen or request.user.is_admin_role):
        raise PermissionDenied("Access Denied: Only Naveen or Administrator can return invoices.")

    if request.method != 'POST':
        return HttpResponseForbidden("POST method required.")

    submission = get_object_or_404(InvoiceSubmission, submission_id=submission_id)
    reason = request.POST.get('return_reason', '').strip()

    if not reason:
        messages.error(request, "Please provide a specific reason for returning this invoice to the employee.")
        return redirect('invoices:submission_detail', submission_id=submission.submission_id)

    old_status = submission.status
    submission.status = InvoiceSubmission.STATUS_RETURNED
    submission.return_reason = reason
    submission.save(update_fields=['status', 'return_reason', 'updated_at'])

    InvoiceStatusHistory.objects.create(
        submission=submission,
        old_status=old_status,
        new_status=InvoiceSubmission.STATUS_RETURNED,
        changed_by=request.user,
        notes=f"Returned to employee: {reason}"
    )

    messages.warning(
        request,
        f"Invoice {submission.submission_id} has been returned to {submission.employee.get_full_name() or submission.employee.username} for correction."
    )
    return redirect('core:naveen_dashboard')


@login_required
def naveen_verify_invoice(request, submission_id):
    """
    Endpoint for Naveen (or Admin) to mark an invoice as verified.
    Runs authoritative vendor & financial validation first so manual-review
    and ambiguity restrictions cannot be bypassed.
    """
    if not (request.user.is_naveen or request.user.is_admin_role):
        raise PermissionDenied("Access Denied: Only Naveen or Administrator can verify invoices.")

    if request.method != 'POST':
        return HttpResponseForbidden("POST method required.")

    submission = get_object_or_404(InvoiceSubmission, submission_id=submission_id)

    backend_val = validate_vendor_and_financials(
        party_name=submission.party_name,
        party_gstin=submission.party_gstin,
        default_expense_ledger=submission.expense_ledger or 'Purchase Spares & Consumables',
    )
    confirm_manual = request.POST.get('confirm_manual_review', '').lower() in ('on', 'true', '1', 'yes')

    has_blocking_gstin_issue = backend_val['vendor_match_status'] in ('GSTIN_MISMATCH', 'INVALID_GSTIN_FORMAT')
    has_missing_financials = not (submission.party_name or '').strip() or submission.total_amount <= Decimal('0.00')
    has_missing_einv_irn = submission.is_e_invoice is True and not is_valid_irn(submission.irn_number)
    needs_review_gate = (
        submission.is_ambiguous_vendor
        or backend_val['is_ambiguous_vendor']
        or submission.requires_manual_review
        or not backend_val['is_financial_approval_eligible']
        or has_missing_einv_irn
    )

    if has_blocking_gstin_issue or has_missing_financials or (needs_review_gate and not confirm_manual):
        messages.warning(
            request,
            "Manual review required: Complete vendor, GSTIN, and amount verification on the Verification Desk before approving this invoice."
        )
        return redirect('invoices:verification_workspace', submission_id=submission.submission_id)

    old_status = submission.status
    submission.status = InvoiceSubmission.STATUS_VERIFIED
    submission.verified_by = request.user
    submission.verified_at = timezone.now()
    submission.requires_manual_review = False
    submission.save(update_fields=['status', 'verified_by', 'verified_at', 'requires_manual_review', 'updated_at'])

    PurchaseAuditTrail.objects.create(
        submission=submission,
        field_name='Verification Approval',
        old_value=old_status,
        new_value=InvoiceSubmission.STATUS_VERIFIED,
        changed_by=request.user,
        reason=request.POST.get('audit_reason', 'Verified and approved after vendor & financial validation.').strip()
    )

    InvoiceStatusHistory.objects.create(
        submission=submission,
        old_status=old_status,
        new_status=InvoiceSubmission.STATUS_VERIFIED,
        changed_by=request.user,
        notes="Invoice documents verified by Naveen."
    )

    messages.success(request, f"Invoice {submission.submission_id} successfully verified!")
    return redirect('core:naveen_dashboard')


@login_required
def download_source_document(request, doc_id):
    """
    Direct download endpoint for the original unsegmented source document (e.g. multi-invoice PDF).
    Ensures complete document provenance and permanent data integrity.
    """
    source_doc = get_object_or_404(UploadedDocument, pk=doc_id)

    # Access control: uploader or Naveen / Admin / Management
    if request.user.is_employee and source_doc.uploader != request.user:
        raise PermissionDenied("Access Denied: You cannot download another employee's source document.")

    if not source_doc.original_file or not source_doc.original_file.storage.exists(source_doc.original_file.name):
        raise Http404("Original source file not found on storage.")

    try:
        fh = source_doc.original_file.open('rb')
    except Exception as e:
        raise Http404(f"Could not open source file: {str(e)}")

    response = FileResponse(fh, content_type=source_doc.content_type)
    response['Content-Disposition'] = f'attachment; filename="{source_doc.file_name}"'
    return response


@login_required
def split_invoice_view(request, submission_id):
    """
    Facility for Naveen to split an incorrectly grouped invoice record at a specified page.
    """
    if not (request.user.is_naveen or request.user.is_admin_role):
        raise PermissionDenied("Access Denied: Only Naveen or Administrator can adjust invoice boundaries.")

    if request.method != 'POST':
        return HttpResponseForbidden("POST required.")

    submission = get_object_or_404(InvoiceSubmission, submission_id=submission_id)
    split_page_str = request.POST.get('split_at_page', '').strip()

    try:
        split_page = int(split_page_str)
        sub_a, sub_b = split_invoice_submission(submission, split_page, request.user)
        messages.success(
            request,
            f"Successfully split invoice into {sub_a.submission_id} (pages {sub_a.start_page}–{sub_a.end_page}) "
            f"and {sub_b.submission_id} (pages {sub_b.start_page}–{sub_b.end_page})."
        )
    except (ValueError, ValidationError) as e:
        messages.error(request, f"Split failed: {str(e)}")

    return redirect('invoices:verification_workspace', submission_id=submission.submission_id)


@login_required
def merge_invoices_view(request, submission_id):
    """
    Facility for Naveen to merge another submission that belongs to the same invoice into this one.
    """
    if not (request.user.is_naveen or request.user.is_admin_role):
        raise PermissionDenied("Access Denied: Only Naveen or Administrator can adjust invoice boundaries.")

    if request.method != 'POST':
        return HttpResponseForbidden("POST required.")

    target_sub = get_object_or_404(InvoiceSubmission, submission_id=submission_id)
    source_id = request.POST.get('source_submission_id', '').strip()
    source_sub = get_object_or_404(InvoiceSubmission, submission_id=source_id)

    try:
        merged = merge_invoice_submissions(target_sub, source_sub, request.user)
        messages.success(
            request,
            f"Successfully merged {source_id} into {merged.submission_id} (new page range: {merged.start_page}–{merged.end_page})."
        )
    except ValidationError as e:
        messages.error(request, f"Merge failed: {str(e)}")

    return redirect('invoices:verification_workspace', submission_id=target_sub.submission_id)


@login_required
def tally_workspace(request, submission_id):
    """
    Side-by-side accounting workspace for Naveen.
    Left: Document Viewer (high-res images / PDF)
    Right: Tally Voucher Data Form + GST status checkoff
    """
    if not (request.user.is_naveen or request.user.is_admin_role or request.user.is_management):
        raise PermissionDenied("Access Denied: Only Naveen or Administrator can access the Tally Workspace.")

    submission = get_object_or_404(
        InvoiceSubmission.objects.select_related('employee'),
        submission_id=submission_id
    )

    if request.method == 'POST':
        if request.user.is_management:
            raise PermissionDenied("Management has read-only privileges.")

        party_name = request.POST.get('party_name', '').strip()
        party_gstin = request.POST.get('party_gstin', '').strip().upper()
        invoice_number = request.POST.get('invoice_number', '').strip()
        invoice_date_str = request.POST.get('invoice_date', '').strip()
        expense_ledger = request.POST.get('expense_ledger', 'Diesel Expenses').strip()
        tally_voucher_no = request.POST.get('tally_voucher_no', '').strip()
        gst_filing_period = request.POST.get('gst_filing_period', '').strip()

        # Numeric amounts
        def parse_decimal(val):
            try:
                return Decimal(val.strip()) if val and val.strip() else Decimal('0.00')
            except Exception:
                return Decimal('0.00')

        taxable_amount = parse_decimal(request.POST.get('taxable_amount', '0'))
        cgst_amount = parse_decimal(request.POST.get('cgst_amount', '0'))
        sgst_amount = parse_decimal(request.POST.get('sgst_amount', '0'))
        igst_amount = parse_decimal(request.POST.get('igst_amount', '0'))
        total_amount = parse_decimal(request.POST.get('total_amount', '0'))

        # Auto-calculate total if 0 and components exist
        if total_amount == 0 and taxable_amount > 0:
            total_amount = taxable_amount + cgst_amount + sgst_amount + igst_amount

        # Checkbox toggles
        is_in_tally = 'is_entered_in_tally' in request.POST
        is_in_gst = 'is_filed_in_gst' in request.POST

        new_data = {
            'party_name': party_name,
            'party_gstin': party_gstin,
            'invoice_number': invoice_number,
            'expense_ledger': expense_ledger,
            'taxable_amount': taxable_amount,
            'cgst_amount': cgst_amount,
            'sgst_amount': sgst_amount,
            'igst_amount': igst_amount,
            'total_amount': total_amount,
        }
        if 'is_e_invoice' in request.POST or 'irn_number' in request.POST:
            einv_val = (
                parse_e_invoice_choice(request.POST.get('is_e_invoice'))
                if 'is_e_invoice' in request.POST
                else submission.is_e_invoice
            )
            irn_val = (
                normalize_irn(request.POST.get('irn_number'))
                if 'irn_number' in request.POST
                else submission.irn_number
            )
            if irn_val and not is_valid_irn(irn_val):
                messages.error(request, "Invalid IRN: IRN must be a valid 64-character alphanumeric value.")
                return redirect('invoices:tally_workspace', submission_id=submission.submission_id)
            if einv_val is False and irn_val:
                messages.error(request, "Conflict: Clear the IRN before marking an invoice as Non E-Invoice (NO), or select YES.")
                return redirect('invoices:tally_workspace', submission_id=submission.submission_id)
            if einv_val is None and is_valid_irn(irn_val):
                einv_val = True
            new_data['is_e_invoice'] = einv_val
            new_data['irn_number'] = '' if einv_val is False else irn_val

        if invoice_date_str:
            try:
                new_data['invoice_date'] = datetime.strptime(invoice_date_str, '%Y-%m-%d').date()
            except ValueError:
                messages.warning(request, f"Invalid invoice date format '{invoice_date_str}'; expected YYYY-MM-DD.")

        record_field_corrections(submission, new_data, request.user, reason="Tally Workspace update")
        submission.refresh_from_db()

        apply_vendor_validation_to_submission(submission)
        apply_e_invoice_to_submission(submission)
        submission.tally_voucher_no = tally_voucher_no
        submission.gst_filing_period = gst_filing_period

        now = timezone.now()
        if is_in_tally and not submission.is_entered_in_tally:
            submission.tally_entered_at = now
            submission.tally_entered_by = request.user
        submission.is_entered_in_tally = is_in_tally

        if is_in_gst and not submission.is_filed_in_gst:
            submission.gst_filed_at = now
        submission.is_filed_in_gst = is_in_gst

        submission.save()
        messages.success(request, f"Tally & GST details for invoice {submission.submission_id} saved successfully!")
        return redirect('invoices:tally_workspace', submission_id=submission.submission_id)

    active_docs = submission.documents.filter(is_active_version=True).order_by('page_number')

    return render(request, 'invoices/tally_workspace.html', {
        'submission': submission,
        'active_docs': active_docs,
        'page_title': f"Tally Entry - {submission.submission_id}",
    })


@login_required
def toggle_tally_status(request, submission_id):
    """
    1-click toggle to flip 'is_entered_in_tally'.
    Can be triggered from dashboard table or directly from the invoice viewer.
    """
    if not (request.user.is_naveen or request.user.is_admin_role):
        raise PermissionDenied("Access Denied.")

    if request.method != 'POST':
        return HttpResponseForbidden("POST required")

    submission = get_object_or_404(InvoiceSubmission, submission_id=submission_id)
    submission.is_entered_in_tally = not submission.is_entered_in_tally
    if submission.is_entered_in_tally:
        submission.tally_entered_at = timezone.now()
    else:
        submission.tally_entered_at = None
    submission.save(update_fields=['is_entered_in_tally', 'tally_entered_at', 'updated_at'])

    status_str = "marked as Entered in Tally ✓" if submission.is_entered_in_tally else "marked as Pending Tally"
    messages.success(request, f"Invoice {submission.submission_id} {status_str}.")

    next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or '/dashboard/naveen/'
    return redirect(next_url)


@login_required
def quick_update_metadata(request, submission_id):
    """
    Rapid inline update for Vendor/Party Name, Invoice Number, and Date.
    Designed for Naveen to correct or complete details on handwritten or faint receipts
    directly from the dashboard table or bill viewer in 2 seconds.
    """
    if not (request.user.is_naveen or request.user.is_admin_role):
        raise PermissionDenied("Access Denied.")

    if request.method != 'POST':
        return HttpResponseForbidden("POST required")

    submission = get_object_or_404(InvoiceSubmission, submission_id=submission_id)
    party_name = request.POST.get('party_name', '').strip()
    invoice_number = request.POST.get('invoice_number', '').strip()
    invoice_date_str = request.POST.get('invoice_date', '').strip()

    expense_ledger = request.POST.get('expense_ledger', '').strip()

    new_data = {}
    if party_name:
        new_data['party_name'] = party_name
        try:
            from apps.invoices.vendor_directory import find_vendor_by_name
            v_match = find_vendor_by_name(party_name)
            if v_match:
                new_data['party_name'] = v_match['name']
                if v_match.get('gstin'):
                    new_data['party_gstin'] = v_match['gstin']
                if v_match.get('ledger') and not expense_ledger:
                    new_data['expense_ledger'] = v_match['ledger']
        except Exception:
            pass
    if invoice_number:
        new_data['invoice_number'] = invoice_number
    if expense_ledger:
        new_data['expense_ledger'] = expense_ledger
    if invoice_date_str:
        try:
            new_data['invoice_date'] = datetime.strptime(invoice_date_str, '%Y-%m-%d').date()
        except ValueError:
            pass

    if 'is_e_invoice' in request.POST or 'irn_number' in request.POST:
        einv_val = (
            parse_e_invoice_choice(request.POST.get('is_e_invoice'))
            if 'is_e_invoice' in request.POST
            else submission.is_e_invoice
        )
        irn_val = (
            normalize_irn(request.POST.get('irn_number'))
            if 'irn_number' in request.POST
            else submission.irn_number
        )
        if irn_val and not is_valid_irn(irn_val):
            return JsonResponse({'status': 'error', 'message': 'Invalid 64-character IRN.'}, status=400)
        if einv_val is False and irn_val:
            return JsonResponse({'status': 'error', 'message': 'Clear IRN before marking Non E-Invoice.'}, status=400)
        if einv_val is None and is_valid_irn(irn_val):
            einv_val = True
        new_data['is_e_invoice'] = einv_val
        new_data['irn_number'] = '' if einv_val is False else irn_val

    if new_data:
        record_field_corrections(submission, new_data, request.user, reason="Quick dashboard inline update")
        submission.refresh_from_db()
        apply_vendor_validation_to_submission(submission)
        apply_e_invoice_to_submission(submission)
        submission.save(update_fields=['is_ambiguous_vendor', 'requires_manual_review', 'ocr_confidence', 'is_e_invoice', 'irn_number', 'updated_at'])

    # Return JSON if requested via AJAX
    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('ajax'):
        return JsonResponse({
            'status': 'ok',
            'submission_id': submission.submission_id,
            'party_name': submission.party_name,
            'party_gstin': submission.party_gstin,
            'invoice_number': submission.invoice_number,
            'expense_ledger': submission.expense_ledger
        })

    messages.success(request, f"Updated {submission.submission_id}: Vendor '{submission.party_name}', Bill #{submission.invoice_number}.")
    next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or '/dashboard/naveen/'
    return redirect(next_url)


@login_required
def batch_complete_tally(request):
    """
    Batch mark selected invoices as entered in Tally.
    Designed for Naveen's end-of-day workflow: selects multiple checkboxes on the right side
    and completes them in a single click.
    """
    if not (request.user.is_naveen or request.user.is_admin_role):
        raise PermissionDenied("Access Denied: Only Naveen or Admins can mark invoices as entered in Tally.")

    if request.method != 'POST':
        return HttpResponseForbidden("POST required")

    selected_ids = request.POST.getlist('selected_invoices')
    if not selected_ids:
        messages.warning(request, "Please select at least one invoice using the checkboxes before clicking Complete.")
        return redirect('core:naveen_dashboard')

    now = timezone.now()
    updated_count = InvoiceSubmission.objects.filter(
        submission_id__in=selected_ids
    ).update(
        is_entered_in_tally=True,
        tally_entered_at=now,
        updated_at=now
    )

    messages.success(
        request,
        f"✓ Successfully marked {updated_count} invoice{'s' if updated_count != 1 else ''} as Completed in Tally!"
    )
    return redirect('core:naveen_dashboard')


@login_required
def batch_undo_tally(request):
    """
    Batch unmark selected invoices from completed back to pending.
    """
    if not (request.user.is_naveen or request.user.is_admin_role):
        raise PermissionDenied("Access Denied.")

    if request.method != 'POST':
        return HttpResponseForbidden("POST required")

    selected_ids = request.POST.getlist('selected_invoices')
    if not selected_ids:
        messages.warning(request, "Please select at least one invoice to move back to pending.")
        return redirect(f"{reverse('core:naveen_dashboard')}?tab=completed")

    now = timezone.now()
    updated_count = InvoiceSubmission.objects.filter(
        submission_id__in=selected_ids
    ).update(
        is_entered_in_tally=False,
        tally_entered_at=None,
        updated_at=now
    )

    messages.info(
        request,
        f"Moved {updated_count} invoice{'s' if updated_count != 1 else ''} back to Pending feed."
    )
    return redirect(f"{reverse('core:naveen_dashboard')}?tab=completed")


@login_required
def toggle_gst_status(request, submission_id):
    """
    1-click toggle directly from Naveen's dashboard table to flip 'is_filed_in_gst'.
    """
    if not (request.user.is_naveen or request.user.is_admin_role):
        raise PermissionDenied("Access Denied.")

    if request.method != 'POST':
        return HttpResponseForbidden("POST required")

    submission = get_object_or_404(InvoiceSubmission, submission_id=submission_id)
    submission.is_filed_in_gst = not submission.is_filed_in_gst
    if submission.is_filed_in_gst:
        submission.gst_filed_at = timezone.now()
    submission.save(update_fields=['is_filed_in_gst', 'gst_filed_at', 'updated_at'])

    status_str = "marked as Filed in GST" if submission.is_filed_in_gst else "marked as Pending GST Filing"
    messages.info(request, f"Invoice {submission.submission_id} {status_str}.")
    return redirect('core:naveen_dashboard')


@login_required
def export_tally_xml(request):
    """
    Exports invoices to standard Tally XML format (compatible with TallyPrime & Tally.ERP 9).
    """
    if not (request.user.is_naveen or request.user.is_admin_role or request.user.is_management):
        raise PermissionDenied("Access Denied.")

    filter_type = request.GET.get('filter', 'pending_tally')
    submissions = InvoiceSubmission.objects.all().select_related('employee').prefetch_related('documents')

    if filter_type == 'pending_tally':
        submissions = submissions.filter(is_entered_in_tally=False)
    elif filter_type == 'in_tally':
        submissions = submissions.filter(is_entered_in_tally=True)

    xml_content = generate_tally_xml(submissions)
    now_str = timezone.now().strftime('%Y%m%d_%H%M')

    response = HttpResponse(xml_content, content_type='application/xml')
    response['Content-Disposition'] = f'attachment; filename="tally_invoices_import_{now_str}.xml"'
    return response


@login_required
def export_gst_csv(request):
    """
    Exports invoices into CSV formatted for GST portal input tax credit (ITC) reconciliation.
    """
    if not (request.user.is_naveen or request.user.is_admin_role or request.user.is_management):
        raise PermissionDenied("Access Denied.")

    submissions = InvoiceSubmission.objects.all().select_related('employee').order_by('-created_at')

    now_str = timezone.now().strftime('%Y%m%d_%H%M')
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="gst_portal_itc_summary_{now_str}.csv"'

    writer = csv.writer(response)
    writer.writerow([
        'GSTIN of Supplier',
        'Trade / Legal Name',
        'Invoice Number',
        'Invoice Date',
        'Invoice Value (Total)',
        'Taxable Value',
        'Integrated Tax (IGST)',
        'Central Tax (CGST)',
        'State Tax (SGST)',
        'Expense Ledger',
        'Tally Voucher Ref',
        'Entered in Tally',
        'Filed in GST',
        'Tracking ID',
        'Collected By'
    ])

    for sub in submissions:
        writer.writerow([
            sub.party_gstin or '',
            sub.party_name or 'Unregistered Supplier',
            sub.invoice_number or sub.submission_id,
            sub.invoice_date.strftime('%d-%b-%Y') if sub.invoice_date else sub.created_at.strftime('%d-%b-%Y'),
            f"{float(sub.total_amount):.2f}",
            f"{float(sub.taxable_amount):.2f}",
            f"{float(sub.igst_amount):.2f}",
            f"{float(sub.cgst_amount):.2f}",
            f"{float(sub.sgst_amount):.2f}",
            sub.expense_ledger,
            sub.tally_voucher_no,
            'YES' if sub.is_entered_in_tally else 'NO',
            'YES' if sub.is_filed_in_gst else 'NO',
            sub.submission_id,
            sub.employee.get_full_name() or sub.employee.username
        ])

    return response


@login_required
def export_gst_portal_excel(request):
    """
    Downloads invoices in official GST Portal B2B Excel (.xlsx) format.
    """
    if not (request.user.is_naveen or request.user.is_admin_role or request.user.is_management):
        raise PermissionDenied("Access Denied.")

    submissions = InvoiceSubmission.objects.all().select_related('employee').order_by('-created_at')
    data = []
    for s in submissions:
        data.append({
            'party_gstin': s.party_gstin,
            'party_name': s.party_name or f"Supplier ({s.employee.username})",
            'invoice_number': s.invoice_number or s.submission_id,
            'invoice_date': s.invoice_date or s.created_at.date(),
            'total_amount': float(s.total_amount or 0.0),
            'taxable_amount': float(s.taxable_amount or s.total_amount or 0.0),
        })

    excel_buffer = create_gst_portal_excel(data)
    now_str = timezone.now().strftime('%Y%m%d_%H%M')
    response = HttpResponse(
        excel_buffer.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f'attachment; filename="GST_Portal_B2B_Compliant_{now_str}.xlsx"'
    return response


@login_required
def gst_converter_tool(request):
    """
    Excel to GST Platform Converter Tool for Naveen.
    Uploads an Excel file from Tally or supplier bills, converts it to the official GST Portal format.
    """
    if not (request.user.is_naveen or request.user.is_admin_role or request.user.is_management):
        raise PermissionDenied("Access Denied.")

    if request.method == 'POST' and request.FILES.get('excel_file'):
        uploaded_file = request.FILES['excel_file']
        try:
            converted_buffer = convert_uploaded_excel_to_gst_format(uploaded_file)
            now_str = timezone.now().strftime('%Y%m%d_%H%M')
            response = HttpResponse(
                converted_buffer.getvalue(),
                content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            )
            response['Content-Disposition'] = f'attachment; filename="GST_Portal_Compliant_{now_str}.xlsx"'
            return response
        except Exception as e:
            messages.error(request, f"Could not convert Excel file: {str(e)}")

    return render(request, 'invoices/gst_converter.html', {
        'page_title': 'GST Portal Excel Converter',
    })


# ==============================================================================
# PHASE 5 — CENTRALIZED PURCHASE MANAGEMENT & VERIFICATION DESK
# ==============================================================================

@login_required
def purchase_register(request):
    """
    Phase 5.1: Centralized Purchase Register.
    Complete audit table for all transport purchase transactions.
    Supports comprehensive multi-field filtering, search, sorting, and Excel export.
    """
    if not (request.user.is_naveen or request.user.is_admin_role or request.user.is_management):
        raise PermissionDenied("Access Denied: Only Accounts Operators and Management can view the Purchase Register.")

    # Apply filters
    filtered_qs = filter_purchase_register(request.GET)

    # Check for Excel Export Trigger
    if request.GET.get('export') == 'excel':
        excel_buffer = generate_tally_structured_excel(
            filtered_qs,
            user=request.user,
            filter_info=dict(request.GET.items())
        )
        now_str = timezone.now().strftime('%Y%m%d_%H%M')
        response = HttpResponse(
            excel_buffer.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        response['Content-Disposition'] = f'attachment; filename="Balaji_Purchase_Register_{now_str}.xlsx"'
        return response

    # Aggregate summaries for currently filtered view
    filtered_aggregates = filtered_qs.aggregate(
        total_val=Sum('total_amount'),
        total_taxable=Sum('taxable_amount'),
        total_cgst=Sum('cgst_amount'),
        total_sgst=Sum('sgst_amount'),
        total_igst=Sum('igst_amount'),
    )

    # Pagination: 40 invoices per page
    paginator = Paginator(filtered_qs, 40)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)

    # Distinct vendors for dropdown
    active_vendors = InvoiceSubmission.objects.exclude(party_name='').values('party_name', 'party_gstin').distinct().order_by('party_name')[:100]

    return render(request, 'invoices/purchase_register.html', {
        'page_obj': page_obj,
        'total_count': filtered_qs.count(),
        'aggregates': filtered_aggregates,
        'active_vendors': active_vendors,
        'params': request.GET,
        'page_title': 'Centralized Purchase Register',
    })


@login_required
def verification_workspace(request, submission_id):
    """
    Phase 5.2: Naveen's Two-Panel Verification Interface.
    Left: Document Viewer with high-res zoom, 1:1 pixel rendering, and rotation controls.
    Right: Candidate OCR extraction fields, confidence indicators, interactive validation alerts,
           Save Corrections button, Verify Invoice button, and complete field modification audit history.
    """
    if not (request.user.is_naveen or request.user.is_admin_role or request.user.is_management):
        raise PermissionDenied("Access Denied: Only Naveen or Administrator can access the Verification Desk.")

    submission = get_object_or_404(
        InvoiceSubmission.objects.select_related('employee', 'verified_by', 'tally_entered_by'),
        submission_id=submission_id
    )

    if request.method == 'POST':
        if request.user.is_management:
            raise PermissionDenied("Management has read-only privileges.")

        action = request.POST.get('action', 'save_only')
        reason = request.POST.get('audit_reason', 'Manual review and verification update').strip()

        # Parse Decimal fields safely
        def to_dec(val):
            try:
                clean_v = str(val or '').strip().replace(',', '')
                return Decimal(clean_v) if clean_v else Decimal('0.00')
            except Exception:
                return Decimal('0.00')

        new_data = {
            'party_name': request.POST.get('party_name', '').strip(),
            'party_gstin': request.POST.get('party_gstin', '').strip().upper(),
            'invoice_number': request.POST.get('invoice_number', '').strip(),
            'expense_ledger': request.POST.get('expense_ledger', 'Diesel Expenses').strip(),
            'hsn_sac_code': request.POST.get('hsn_sac_code', '').strip(),
            'taxable_amount': to_dec(request.POST.get('taxable_amount')),
            'cgst_amount': to_dec(request.POST.get('cgst_amount')),
            'sgst_amount': to_dec(request.POST.get('sgst_amount')),
            'igst_amount': to_dec(request.POST.get('igst_amount')),
            'total_amount': to_dec(request.POST.get('total_amount')),
        }

        if 'is_e_invoice' in request.POST or 'irn_number' in request.POST:
            einv_val = (
                parse_e_invoice_choice(request.POST.get('is_e_invoice'))
                if 'is_e_invoice' in request.POST
                else submission.is_e_invoice
            )
            irn_val = (
                normalize_irn(request.POST.get('irn_number'))
                if 'irn_number' in request.POST
                else submission.irn_number
            )
            if irn_val and not is_valid_irn(irn_val):
                messages.error(
                    request,
                    "Invalid IRN format: An Indian GST IRN must be a valid 64-character alphanumeric string. "
                    "Incomplete or fabricated IRNs cannot be saved."
                )
                return redirect('invoices:verification_workspace', submission_id=submission.submission_id)
            if einv_val is False and irn_val:
                messages.error(
                    request,
                    "Conflicting E-Invoice selection: Clear the IRN if marking as Non E-Invoice (NO), "
                    "or set E-Invoice Status to YES."
                )
                return redirect('invoices:verification_workspace', submission_id=submission.submission_id)
            if einv_val is None and is_valid_irn(irn_val):
                einv_val = True
            new_data['is_e_invoice'] = einv_val
            new_data['irn_number'] = '' if einv_val is False else irn_val

        # Handle Date
        inv_date_str = request.POST.get('invoice_date', '').strip()
        if inv_date_str:
            try:
                new_data['invoice_date'] = datetime.strptime(inv_date_str, '%Y-%m-%d').date()
            except ValueError:
                messages.warning(request, f"Invalid date format '{inv_date_str}'; expected YYYY-MM-DD.")

        # Validate vendor identity & GSTIN before persisting/approving
        backend_val = validate_vendor_and_financials(
            party_name=new_data['party_name'],
            party_gstin=new_data['party_gstin'],
            default_expense_ledger=new_data['expense_ledger'],
        )
        confirm_manual = request.POST.get('confirm_manual_review', '').lower() in ('on', 'true', '1', 'yes')

        # Record field corrections in PurchaseAuditTrail
        record_field_corrections(submission, new_data, request.user, reason=reason)
        submission.refresh_from_db()

        if action == 'verify':
            if not new_data['party_name']:
                messages.error(request, "Supplier / Party Name is required before approval.")
                return redirect('invoices:verification_workspace', submission_id=submission.submission_id)

            if new_data['total_amount'] <= Decimal('0.00'):
                messages.error(request, "Total Invoice Amount must be greater than ₹0.00 before approval.")
                return redirect('invoices:verification_workspace', submission_id=submission.submission_id)

            if backend_val['vendor_match_status'] in ('GSTIN_MISMATCH', 'INVALID_GSTIN_FORMAT'):
                messages.error(request, f"Approval blocked: {backend_val['audit_notes']}")
                return redirect('invoices:verification_workspace', submission_id=submission.submission_id)

            einv_missing_irn = submission.is_e_invoice is True and not is_valid_irn(submission.irn_number)
            if (backend_val['is_ambiguous_vendor'] or einv_missing_irn) and not confirm_manual:
                messages.error(
                    request,
                    "Manual review confirmation required: Confirm missing IRN or ambiguous vendor details "
                    "(tick the manual-review confirmation box) before approving."
                )
                return redirect('invoices:verification_workspace', submission_id=submission.submission_id)

            old_status = submission.status
            submission.status = InvoiceSubmission.STATUS_VERIFIED
            submission.verified_by = request.user
            submission.verified_at = timezone.now()
            submission.is_ambiguous_vendor = backend_val['is_ambiguous_vendor']
            submission.requires_manual_review = False
            apply_e_invoice_to_submission(submission)
            # If manually confirmed with missing IRN, preserve verified status without silent approval
            if einv_missing_irn and confirm_manual:
                submission.requires_manual_review = False
            submission.save(update_fields=[
                'status', 'verified_by', 'verified_at',
                'is_ambiguous_vendor', 'requires_manual_review',
                'is_e_invoice', 'irn_number', 'updated_at'
            ])

            PurchaseAuditTrail.objects.create(
                submission=submission,
                field_name='Verification Approval',
                old_value=old_status,
                new_value=InvoiceSubmission.STATUS_VERIFIED,
                changed_by=request.user,
                reason=reason
            )

            InvoiceStatusHistory.objects.create(
                submission=submission,
                old_status=old_status,
                new_status=InvoiceSubmission.STATUS_VERIFIED,
                changed_by=request.user,
                notes=f"Verified and approved by {request.user.get_full_name() or request.user.username}."
            )
            messages.success(request, f"✓ Invoice {submission.submission_id} verified and ready for Tally entry!")
            return redirect('core:naveen_dashboard')

        # Save-only path: keep backend vendor, e-invoice & manual review flags authoritative
        submission.is_ambiguous_vendor = backend_val['is_ambiguous_vendor']
        submission.requires_manual_review = backend_val['requires_manual_review']
        apply_e_invoice_to_submission(submission)
        submission.save(update_fields=[
            'is_ambiguous_vendor', 'requires_manual_review',
            'is_e_invoice', 'irn_number', 'updated_at'
        ])

        messages.success(request, f"Corrections for invoice {submission.submission_id} saved successfully!")
        return redirect('invoices:verification_workspace', submission_id=submission.submission_id)

    # GET: Prepare verification workspace
    active_docs = submission.documents.filter(is_active_version=True).order_by('page_number')
    audit_trails = submission.audit_trails.select_related('changed_by').order_by('-changed_at')
    ledger_mappings = TallyLedgerMapping.objects.filter(is_active=True).order_by('category_name')

    # Stream Navigation (prev/next)
    prev_sub = InvoiceSubmission.objects.filter(created_at__gt=submission.created_at).order_by('created_at').first()
    next_sub = InvoiceSubmission.objects.filter(created_at__lt=submission.created_at).order_by('-created_at').first()

    return render(request, 'invoices/verification_workspace.html', {
        'submission': submission,
        'active_docs': active_docs,
        'audit_trails': audit_trails,
        'ledger_mappings': ledger_mappings,
        'prev_sub': prev_sub,
        'next_sub': next_sub,
        'page_title': f"Verify Purchase - {submission.submission_id}",
    })


@login_required
def gst_reconciliation(request):
    """
    Phase 5.7: GSTR-2A/2B Comparison Support Tool.
    Reconciles recorded purchase invoices against the 104 verified GSTR-2A supplier database.
    Categorizes matches, missing records, and tax discrepancies without automatic filing.
    """
    if not (request.user.is_naveen or request.user.is_admin_role or request.user.is_management):
        raise PermissionDenied("Access Denied.")

    recon_results = compare_with_gstr2a()

    return render(request, 'invoices/gst_reconciliation.html', {
        'results': recon_results,
        'page_title': 'GSTR-2A / 2B Tax Reconciliation Desk',
    })


# ==============================================================================
# PHASE 6 — EXCEL-TO-TALLY AUTOMATION & LEDGER MAPPING
# ==============================================================================

@login_required
def export_tally_excel(request):
    """
    Phase 6.3: Structured Accounting Excel Export for Tally.
    Exports verified purchase records in a styled, transport-specific Excel workbook.
    """
    if not (request.user.is_naveen or request.user.is_admin_role or request.user.is_management):
        raise PermissionDenied("Access Denied.")

    filter_type = request.GET.get('filter', 'verified_only')
    base_qs = InvoiceSubmission.objects.all().select_related('employee').order_by('-invoice_date')

    if filter_type == 'verified_only':
        base_qs = base_qs.filter(status=InvoiceSubmission.STATUS_VERIFIED)
    elif filter_type == 'pending_tally':
        base_qs = base_qs.filter(is_entered_in_tally=False)
    elif filter_type == 'entered_tally':
        base_qs = base_qs.filter(is_entered_in_tally=True)

    excel_buffer = generate_tally_structured_excel(
        base_qs,
        user=request.user,
        filter_info={'filter': filter_type}
    )

    now_str = timezone.now().strftime('%Y%m%d_%H%M')
    response = HttpResponse(
        excel_buffer.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f'attachment; filename="Tally_Purchase_Export_{filter_type}_{now_str}.xlsx"'
    return response


@login_required
def ledger_mappings_view(request):
    """
    Phase 6.2: Controlled Ledger Mapping Management.
    Enables Naveen and Admin to manage standard Purchase ledgers and Tax ledgers.
    """
    if not (request.user.is_naveen or request.user.is_admin_role or request.user.is_management):
        raise PermissionDenied("Access Denied.")

    mappings = TallyLedgerMapping.objects.all().order_by('category_name')

    if request.method == 'POST':
        if request.user.is_management:
            raise PermissionDenied("Management has read-only privileges.")

        action = request.POST.get('action')
        if action == 'add':
            cat = request.POST.get('category_name', '').strip()
            exp_ledger = request.POST.get('tally_expense_ledger', '').strip()
            cgst = request.POST.get('cgst_ledger', 'Input CGST').strip()
            sgst = request.POST.get('sgst_ledger', 'Input SGST').strip()
            igst = request.POST.get('igst_ledger', 'Input IGST').strip()
            desc = request.POST.get('description', '').strip()

            if cat and exp_ledger:
                TallyLedgerMapping.objects.create(
                    category_name=cat,
                    tally_expense_ledger=exp_ledger,
                    cgst_ledger=cgst,
                    sgst_ledger=sgst,
                    igst_ledger=igst,
                    description=desc,
                    is_active=True
                )
                messages.success(request, f"Created ledger mapping '{cat}' -> '{exp_ledger}'.")
        elif action == 'toggle':
            mapping_id = request.POST.get('mapping_id')
            m = get_object_or_404(TallyLedgerMapping, pk=mapping_id)
            m.is_active = not m.is_active
            m.save()
            messages.info(request, f"Updated mapping status for '{m.category_name}'.")

        return redirect('invoices:ledger_mappings')

    return render(request, 'invoices/ledger_mappings.html', {
        'mappings': mappings,
        'page_title': 'Tally Chart of Accounts & Ledger Mapping',
    })


@login_required
def export_history_view(request):
    """
    Phase 6.5: Export Audit History Log.
    Maintains full accountability of who generated what export file and when.
    """
    if not (request.user.is_naveen or request.user.is_admin_role or request.user.is_management):
        raise PermissionDenied("Access Denied.")

    logs = ExportAuditLog.objects.all().select_related('exported_by').order_by('-created_at')[:100]

    return render(request, 'invoices/export_history.html', {
        'logs': logs,
        'page_title': 'Accounting & Tax Export Audit Log',
    })


# ==============================================================================
# PHASE 7 — EXECUTIVE ANALYTICS & DOWNLOADABLE REPORTS
# ==============================================================================

@login_required
def analytics_dashboard(request):
    """
    Phase 7.1: Live Financial Analytics Dashboard.
    Database-calculated KPIs, monthly spend trends, top supplier breakdown,
    and ledger distribution. Accessible to Naveen, Admin, and Management.
    """
    if not (request.user.is_naveen or request.user.is_admin_role or request.user.is_management):
        raise PermissionDenied("Access Denied.")

    kpis = get_executive_kpis()
    monthly_trends = get_monthly_trends()
    top_suppliers = get_top_suppliers(limit=10)
    ledger_dist = get_ledger_distribution()
    employee_stats = get_employee_upload_stats()

    return render(request, 'invoices/analytics_dashboard.html', {
        'kpis': kpis,
        'monthly_trends': monthly_trends,
        'top_suppliers': top_suppliers,
        'ledger_dist': ledger_dist,
        'employee_stats': employee_stats,
        'page_title': 'Executive Purchase Analytics & Insights',
    })


@login_required
def download_executive_report(request, report_type):
    """
    Phase 7.4: Downloadable Executive Excel Reports.
    Types: 'monthly_summary', 'supplier_summary', 'compliance_audit'.
    """
    if not (request.user.is_naveen or request.user.is_admin_role or request.user.is_management):
        raise PermissionDenied("Access Denied.")

    excel_buffer = generate_executive_excel_report(report_type=report_type, user=request.user)
    now_str = timezone.now().strftime('%Y%m%d_%H%M')

    response = HttpResponse(
        excel_buffer.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f'attachment; filename="Balaji_Executive_Report_{report_type}_{now_str}.xlsx"'
    return response




