import logging
from django.shortcuts import render, redirect
from django.contrib import messages
from django.core.exceptions import PermissionDenied

logger = logging.getLogger(__name__)
from pathlib import Path
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.db import connection
from django.db.models import Q
from django.core.paginator import Paginator
from django.views.decorators.http import require_GET, require_POST
from django.utils import timezone

from apps.accounts.models import UserRole
from apps.accounts.permissions import (
    employee_required,
    naveen_required,
    admin_required,
    management_required,
)
from apps.invoices.models import InvoiceSubmission


@login_required
def dashboard_router(request):
    """
    Directs authenticated users to their designated role dashboard.
    """
    user = request.user
    if user.is_submitter:
        return redirect('core:employee_dashboard')
    elif user.is_naveen:
        return redirect('core:naveen_dashboard')
    elif user.is_admin_role:
        return redirect('core:admin_dashboard')
    elif user.is_management:
        return redirect('core:management_dashboard')
    # Default fallback
    return redirect('accounts:profile')


@employee_required
def employee_dashboard(request):
    """
    Submitter Dashboard (External Submitters and Field Employees).
    Strictly isolated to request.user's own invoice submissions.
    """
    from django.db.models import Q
    query = request.GET.get('q', '').strip()
    user_submissions = InvoiceSubmission.objects.filter(employee=request.user)
    filtered_submissions = user_submissions
    if query:
        filtered_submissions = user_submissions.filter(
            Q(submission_id__icontains=query) |
            Q(invoice_number__icontains=query) |
            Q(policy_number__icontains=query) |
            Q(party_name__icontains=query) |
            Q(vehicle_number__icontains=query) |
            Q(remarks__icontains=query) |
            Q(source_document__file_name__icontains=query)
        )
    recent_submissions = filtered_submissions.select_related('source_document').prefetch_related('documents').order_by('-created_at')[:15]
    total_count = user_submissions.count()
    pending_count = user_submissions.filter(
        status__in=[InvoiceSubmission.STATUS_SUBMITTED, InvoiceSubmission.STATUS_RESUBMITTED]
    ).count()
    returned_count = user_submissions.filter(status=InvoiceSubmission.STATUS_RETURNED).count()
    verified_count = user_submissions.filter(status=InvoiceSubmission.STATUS_VERIFIED).count()
    rejected_count = user_submissions.filter(status=InvoiceSubmission.STATUS_REJECTED).count()

    portal_title = 'External Submitter Portal' if request.user.is_external_submitter else 'Employee Invoice Portal'

    from django.conf import settings
    from apps.invoices.models import UploadedDocument
    recent_uploads = UploadedDocument.objects.filter(uploader=request.user).order_by('-created_at')[:15]
    ocr_max_retries = max(1, int(getattr(settings, 'OCR_MAX_RETRIES', 3) or 3))

    return render(request, 'core/employee_dashboard.html', {
        'page_title': portal_title,
        'recent_submissions': recent_submissions,
        'recent_uploads': recent_uploads,
        'query': query,
        'ocr_max_retries': ocr_max_retries,
        'total_count': total_count,
        'pending_count': pending_count,
        'returned_count': returned_count,
        'verified_count': verified_count,
        'rejected_count': rejected_count,
    })


@naveen_required
def naveen_dashboard(request):
    """
    Naveen Centralized Accounts Feed.
    Direct chronological stream of invoice photos and PDFs sent by employees.
    Supports:
    1. Daily Fresh Feed: Clean zero-backlog start each day if all prior invoices are done.
    2. Carry-Over Alert: Missed/unentered invoices from previous days highlighted at the top.
    3. Batch Checkboxes: Select invoices on the right side and complete all at end of day.
    """
    query = request.GET.get('q', '').strip()
    tab = request.GET.get('tab', 'pending').strip()

    base_qs = InvoiceSubmission.objects.all().select_related('employee').prefetch_related('documents')

    today = timezone.localdate()
    today_date_str = today.strftime('%d %B %Y')

    # Status counts across all submissions
    total_received = base_qs.count()
    carryover_base = base_qs.filter(is_entered_in_tally=False, created_at__date__lt=today)
    today_pending_base = base_qs.filter(is_entered_in_tally=False, created_at__date=today)
    completed_base = base_qs.filter(is_entered_in_tally=True)

    carryover_count = carryover_base.count()
    today_pending_count = today_pending_base.count()
    total_pending_count = carryover_count + today_pending_count
    completed_count = completed_base.count()

    from apps.invoices.models import InvoiceDocument
    total_pages = InvoiceDocument.objects.count()

    def apply_search(qs):
        if not query:
            return qs
        return qs.filter(
            Q(submission_id__icontains=query) |
            Q(party_name__icontains=query) |
            Q(party_gstin__icontains=query) |
            Q(invoice_number__icontains=query) |
            Q(irn_number__icontains=query) |
            Q(employee__first_name__icontains=query) |
            Q(employee__last_name__icontains=query) |
            Q(employee__username__icontains=query)
        )

    # Invoices in arrival order (oldest first for daily sequential entry)
    carryover_invoices = apply_search(carryover_base.order_by('created_at'))
    today_invoices = apply_search(today_pending_base.order_by('created_at'))
    completed_invoices = apply_search(completed_base.order_by('-tally_entered_at', '-created_at'))
    all_invoices = apply_search(base_qs.order_by('-created_at'))

    # High-volume pagination (40 invoices per page for archive tabs)
    page_num = request.GET.get('page', 1)
    if tab == 'completed':
        paginator_completed = Paginator(completed_invoices, 40)
        completed_page = paginator_completed.get_page(page_num)
    else:
        completed_page = completed_invoices

    if tab == 'all':
        paginator_all = Paginator(all_invoices, 40)
        all_page = paginator_all.get_page(page_num)
    else:
        all_page = all_invoices

    # Top frequent vendors for instant autocomplete suggestions (from database & official GSTR-2A directory)
    db_vendors = list(
        base_qs.exclude(party_name='')
               .values_list('party_name', flat=True)
               .distinct()[:30]
    )
    try:
        from apps.invoices.vendor_directory import get_all_vendor_names
        dir_vendors = get_all_vendor_names()
    except Exception:
        dir_vendors = []
    
    # Combine and deduplicate
    combined = list(dict.fromkeys(db_vendors + dir_vendors))[:50]
    frequent_vendors = combined

    # Unique submitter names for header filter
    submitter_raw = list(
        base_qs.values_list('employee__first_name', 'employee__last_name', 'employee__username')
               .distinct()[:30]
    )
    submitter_names = []
    for fn, ln, un in submitter_raw:
        full = f"{fn or ''} {ln or ''}".strip() or un
        if full and full not in submitter_names:
            submitter_names.append(full)

    is_fresh_feed = (carryover_count == 0 and today_pending_count == 0)

    from django.conf import settings
    from apps.invoices.models import UploadedDocument
    processing_uploads = (
        UploadedDocument.objects
        .filter(processing_status__in=[
            UploadedDocument.STATUS_UPLOADED,
            UploadedDocument.STATUS_QUEUED,
            UploadedDocument.STATUS_PROCESSING,
            UploadedDocument.STATUS_FAILED,
        ])
        .select_related('uploader')
        .order_by('-created_at')[:20]
    )
    ocr_max_retries = max(1, int(getattr(settings, 'OCR_MAX_RETRIES', 3) or 3))

    return render(request, 'core/naveen_dashboard.html', {
        'page_title': "Naveen's Invoice Hub",
        'tab': tab,
        'query': query,
        'today_date_str': today_date_str,
        'carryover_invoices': carryover_invoices,
        'today_invoices': today_invoices,
        'completed_invoices': completed_page,
        'all_invoices': all_page,
        'carryover_count': carryover_count,
        'today_pending_count': today_pending_count,
        'total_pending_count': total_pending_count,
        'completed_count': completed_count,
        'total_received': total_received,
        'total_pages': total_pages,
        'is_fresh_feed': is_fresh_feed,
        'frequent_vendors': frequent_vendors,
        'submitter_names': submitter_names,
        'processing_uploads': processing_uploads,
        'ocr_max_retries': ocr_max_retries,
    })


@admin_required
def admin_dashboard(request):
    """
    System Administration Dashboard with read-only security & user governance overview.
    """
    from django.conf import settings
    from apps.accounts.models import AccountStatus, AuditLog, User

    external_users_count = User.objects.filter(
        role__in=[UserRole.EXTERNAL_SUBMITTER, UserRole.EMPLOYEE]
    ).count()
    active_users_count = User.objects.filter(
        is_active=True, account_status=AccountStatus.ACTIVE
    ).count()
    suspended_users_count = User.objects.filter(
        account_status=AccountStatus.SUSPENDED
    ).count()
    recent_failed_logins = AuditLog.objects.filter(
        action__in=['USER_LOGIN_FAILED', 'USER_LOGIN_RATE_LIMITED']
    ).count()
    recent_audits = AuditLog.objects.select_related('user').order_by('-timestamp')[:8]

    return render(request, 'core/admin_dashboard.html', {
        'page_title': 'Administration & System Config',
        'external_users_count': external_users_count,
        'active_users_count': active_users_count,
        'suspended_users_count': suspended_users_count,
        'recent_failed_logins': recent_failed_logins,
        'recent_audits': recent_audits,
        'debug_mode': settings.DEBUG,
        'ssl_enabled': settings.ENABLE_SSL,
        'privileged_session_hours': round(getattr(settings, 'PRIVILEGED_SESSION_AGE', 28800) / 3600, 1),
        'submitter_session_days': round(getattr(settings, 'EXTERNAL_SUBMITTER_SESSION_AGE', 1209600) / 86400, 1),
        'login_max_attempts': getattr(settings, 'LOGIN_MAX_ATTEMPTS', 5),
    })


@admin_required
def ai_engine_config(request):
    """
    Dedicated, restricted Administrative view for AI Vision Engine & API configuration.
    Requires intentional multi-step navigation (not openly displayed on main dashboard).
    Strictly restricted to Admin role.
    Supports multi-key pool failover (5 to 6 keys) for high-volume free tier usage.
    """
    if not (request.user.is_authenticated and request.user.is_admin_role):
        raise PermissionDenied("Access Denied: Only Administrators can configure system integrations.")

    from apps.invoices.gemini_service import (
        get_configured_gemini_keys,
        get_key_health,
        mask_api_key,
    )

    configured_keys = get_configured_gemini_keys()
    gemini_model = (getattr(settings, 'GEMINI_MODEL', 'gemini-3.5-flash-lite') or 'gemini-3.5-flash-lite').strip()
    gemini_fallbacks_raw = str(getattr(settings, 'GEMINI_FALLBACK_MODELS', '') or '')
    gemini_fallbacks = [m.strip() for m in gemini_fallbacks_raw.split(',') if m.strip()]

    key_pool = []
    healthy_count = 0
    for idx, k in enumerate(configured_keys):
        health = get_key_health(k)
        if not health.get('is_exhausted'):
            healthy_count += 1
        key_pool.append({
            'index': idx + 1,
            'masked': mask_api_key(k),
            'key_suffix': k[-4:] if len(k) >= 4 else k,
            'health': health,
            'is_primary': idx == 0,
        })

    pool_count = len(key_pool)
    daily_quota_estimate = pool_count * 1500

    return render(request, 'core/ai_config.html', {
        'page_title': 'AI Vision Engine & Security Config',
        'key_pool': key_pool,
        'pool_count': pool_count,
        'healthy_count': healthy_count,
        'daily_quota_estimate': daily_quota_estimate,
        'gemini_key_configured': pool_count > 0,
        'gemini_key_masked': key_pool[0]['masked'] if key_pool else '',
        'gemini_model': gemini_model,
        'gemini_fallback_models': gemini_fallbacks,
    })


@admin_required
@require_POST
def update_gemini_api_key_view(request):
    """
    Dedicated Administrator endpoint to manage Google Gemini API key pool and vision model.
    Supports adding, replacing, or removing keys from the multi-key failover pool.
    Updates the .env file, hot-reloads runtime settings in memory, and records an audit log.
    Strictly restricted to Admin role.
    """
    if not (request.user.is_authenticated and request.user.is_admin_role):
        raise PermissionDenied("Access Denied: Only Administrators can configure the AI Vision API key.")

    from apps.invoices.gemini_service import (
        get_configured_gemini_keys,
        parse_gemini_keys,
        save_gemini_key_pool,
        test_gemini_connection,
    )
    from apps.accounts.models import AuditLog

    action = request.POST.get('action', 'replace').strip().lower()
    new_model = request.POST.get('model', 'gemini-3.5-flash-lite').strip() or 'gemini-3.5-flash-lite'
    skip_test = request.POST.get('skip_test') == '1'
    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.POST.get('format') == 'json'

    # Action 1: Remove a specific key from the pool
    if action == 'remove':
        target_idx_str = request.POST.get('key_index')
        current_keys = get_configured_gemini_keys()
        try:
            target_idx = int(target_idx_str) - 1
            if 0 <= target_idx < len(current_keys):
                removed_key = current_keys.pop(target_idx)
                save_gemini_key_pool(current_keys, model_name=new_model)

                client_ip = request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() or request.META.get('REMOTE_ADDR')
                AuditLog.objects.create(
                    user=request.user,
                    action='CONFIG_GEMINI_KEY_REMOVED',
                    ip_address=client_ip,
                    user_agent=request.META.get('HTTP_USER_AGENT', '')[:500],
                    details={
                        'removed_key_suffix': removed_key[-4:],
                        'remaining_keys_count': len(current_keys),
                    }
                )

                msg = f"Key ...{removed_key[-4:]} successfully removed from pool. ({len(current_keys)} keys remaining)."
                if is_ajax:
                    return JsonResponse({'success': True, 'message': msg, 'remaining_count': len(current_keys)})
                messages.success(request, msg)
                return redirect('core:ai_engine_config')
            else:
                err_msg = "Invalid key index specified for removal."
                if is_ajax:
                    return JsonResponse({'success': False, 'error': err_msg}, status=400)
                messages.error(request, err_msg)
                return redirect('core:ai_engine_config')
        except (ValueError, TypeError):
            err_msg = "Invalid key index provided."
            if is_ajax:
                return JsonResponse({'success': False, 'error': err_msg}, status=400)
            messages.error(request, err_msg)
            return redirect('core:ai_engine_config')

    # Action 2: Add or Replace keys
    raw_keys_input = request.POST.get('api_keys_text') or request.POST.get('api_key', '')
    submitted_keys = parse_gemini_keys(raw_keys_input)

    if not submitted_keys:
        error_msg = "No API keys provided. Please paste at least one valid Gemini API key from Google AI Studio."
        if is_ajax:
            return JsonResponse({'success': False, 'error': error_msg}, status=400)
        messages.error(request, error_msg)
        return redirect('core:ai_engine_config')

    # Validate length of each key
    invalid_keys = [k for k in submitted_keys if len(k) < 15]
    if invalid_keys:
        error_msg = f"One or more keys appear too short (<15 characters). Please check your input."
        if is_ajax:
            return JsonResponse({'success': False, 'error': error_msg}, status=400)
        messages.error(request, error_msg)
        return redirect('core:ai_engine_config')

    if action == 'add':
        existing = get_configured_gemini_keys()
        final_keys = parse_gemini_keys(existing + submitted_keys)
    else:  # 'replace'
        final_keys = submitted_keys

    # Optional pre-save validation
    test_results = []
    if not skip_test:
        at_least_one_working = False
        failed_errors = []
        for k in submitted_keys:
            res = test_gemini_connection(api_key=k, model_name=new_model)
            test_results.append(res)
            if res.get('success'):
                at_least_one_working = True
            else:
                failed_errors.append(f"...{k[-4:]}: {res.get('error')}")

        if not at_least_one_working:
            err = f"Validation Failed for submitted key(s): {'; '.join(failed_errors)}"
            if is_ajax:
                return JsonResponse({'success': False, 'error': err, 'test_results': test_results}, status=400)
            messages.error(request, err)
            return redirect('core:ai_engine_config')

    # Atomically persist key pool to .env and hot-reload runtime
    success = save_gemini_key_pool(keys=final_keys, model_name=new_model)
    if not success:
        error_msg = "Failed to write to .env file. Please check file permissions on the server."
        if is_ajax:
            return JsonResponse({'success': False, 'error': error_msg}, status=500)
        messages.error(request, error_msg)
        return redirect('core:ai_engine_config')

    # Record security audit log
    client_ip = request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() or request.META.get('REMOTE_ADDR')
    AuditLog.objects.create(
        user=request.user,
        action='CONFIG_GEMINI_API_KEY_UPDATED',
        ip_address=client_ip,
        user_agent=request.META.get('HTTP_USER_AGENT', '')[:500],
        details={
            'action': action,
            'pool_count': len(final_keys),
            'key_suffixes': [k[-4:] for k in final_keys],
            'model': new_model,
            'tested': not skip_test,
            'reason': 'Multi-key pool failover configuration from Admin Console',
        }
    )

    masked_summary = f"{len(final_keys)} keys active (Primary: ...{final_keys[0][-4:]})"
    success_msg = f"✓ Google Gemini Key Pool updated successfully ({masked_summary}, Model: {new_model}). Stored securely in server-side .env."

    if is_ajax:
        return JsonResponse({
            'success': True,
            'message': success_msg,
            'pool_count': len(final_keys),
            'model': new_model,
            'test_results': test_results,
        })

    messages.success(request, success_msg)
    return redirect('core:ai_engine_config')


@admin_required
@require_POST
def test_gemini_api_key_view(request):
    """
    Administrator endpoint to run live ping tests against Google Gemini API.
    Can test:
      1. All keys in the configured server pool (test_all=1 or action=test_pool).
      2. A batch of candidate keys entered in the form.
      3. A single key.
    Strictly restricted to Admin role.
    """
    if not (request.user.is_authenticated and request.user.is_admin_role):
        raise PermissionDenied("Access Denied: Only Administrators can test API keys.")

    from apps.invoices.gemini_service import (
        get_configured_gemini_keys,
        parse_gemini_keys,
        test_all_gemini_keys,
        test_gemini_connection,
    )

    test_all = request.POST.get('test_all') == '1' or request.POST.get('action') == 'test_pool'
    raw_keys_input = request.POST.get('api_keys_text') or request.POST.get('api_key', '')
    model = (request.POST.get('model', '').strip() or getattr(settings, 'GEMINI_MODEL', 'gemini-3.5-flash-lite') or 'gemini-3.5-flash-lite').strip()

    # Case A: Batch test all configured keys in pool or submitted candidate list
    if test_all or ('\n' in raw_keys_input or ',' in raw_keys_input):
        target_keys = parse_gemini_keys(raw_keys_input) if raw_keys_input.strip() else get_configured_gemini_keys()
        if not target_keys:
            return JsonResponse({'success': False, 'error': 'No Gemini API keys are configured to test.'}, status=400)

        results = test_all_gemini_keys(keys=target_keys, model_name=model)
        healthy_count = sum(1 for r in results if r.get('success'))
        return JsonResponse({
            'success': healthy_count > 0,
            'total_tested': len(results),
            'healthy_count': healthy_count,
            'results': results,
            'message': f"Pool Test Complete: {healthy_count} of {len(results)} keys responding OK."
        })

    # Case B: Single key test
    api_key = raw_keys_input.strip()
    if not api_key:
        configured = get_configured_gemini_keys()
        api_key = configured[0] if configured else ''

    if not api_key:
        return JsonResponse({
            'success': False,
            'error': 'No Gemini API key is currently configured to test.',
        }, status=400)

    result = test_gemini_connection(api_key=api_key, model_name=model)
    status_code = 200 if result.get('success') else 400
    return JsonResponse(result, status=status_code)


@management_required
def management_dashboard(request):
    """
    Phase 7: Real-Time Executive Management Overview.
    Live database-computed KPIs, monthly purchase spend trends, top supplier breakdown,
    and direct access to downloadable audit reports (Strictly Read-Only).
    """
    from apps.invoices.analytics_service import (
        get_executive_kpis,
        get_monthly_trends,
        get_top_suppliers,
        get_ledger_distribution,
    )
    kpis = get_executive_kpis()
    monthly_trends = get_monthly_trends()
    top_suppliers = get_top_suppliers(limit=5)
    ledger_dist = get_ledger_distribution()[:6]

    return render(request, 'core/management_dashboard.html', {
        'page_title': 'Executive Management Overview',
        'kpis': kpis,
        'monthly_trends': monthly_trends,
        'top_suppliers': top_suppliers,
        'ledger_dist': ledger_dist,
    })


@require_GET
def offline_view(request):
    """Non-sensitive static offline fallback page for PWA navigation requests."""
    response = render(request, 'core/offline.html')
    response['Cache-Control'] = 'public, max-age=300'
    return response


@require_GET
def service_worker_view(request):
    """
    Serves the PWA Service Worker from root '/' scope with no-cache headers
    so browser updates apply immediately and scope covers the full application.
    """
    sw_path = Path(settings.BASE_DIR) / 'static' / 'js' / 'sw.js'
    content = sw_path.read_text(encoding='utf-8')
    response = HttpResponse(content, content_type='application/javascript; charset=utf-8')
    response['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response['Service-Worker-Allowed'] = '/'
    return response


@require_GET
def health_check(request):
    """
    Verifies operational status and PostgreSQL database connectivity without exposing internal details.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1;")
            row = cursor.fetchone()
            if not row or row[0] != 1:
                raise RuntimeError("Unexpected database health query result.")
    except Exception:
        logger.exception("Database health check failed")
        return JsonResponse({
            'status': 'DOWN',
            'database': 'ERROR',
        }, status=503)

    return JsonResponse({
        'status': 'UP',
        'application': 'Balaji InvoiceHub',
        'database': 'CONNECTED',
    }, status=200)


def custom_permission_denied_view(request, exception=None):
    """
    Custom 403 Forbidden handler with clean security context.
    """
    return render(request, 'errors/403.html', {
        'exception_message': str(exception) if exception else "Access denied by role authorization policy."
    }, status=403)


def custom_not_found_view(request, exception=None):
    """
    Custom 404 Not Found handler.
    """
    return render(request, 'errors/404.html', status=404)

