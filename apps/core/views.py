from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.db import connection
from django.db.models import Q
from django.core.paginator import Paginator
from django.views.decorators.http import require_GET
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
    if user.is_employee:
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
    Employee Mobile-First Dashboard (Simple & Minimal).
    """
    user_submissions = InvoiceSubmission.objects.filter(employee=request.user)
    recent_submissions = user_submissions.prefetch_related('documents').order_by('-created_at')[:10]
    total_count = user_submissions.count()

    return render(request, 'core/employee_dashboard.html', {
        'page_title': 'Employee Invoice Portal',
        'recent_submissions': recent_submissions,
        'total_count': total_count,
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
    })


@admin_required
def admin_dashboard(request):
    """
    System Administration Dashboard (Phase 1 Baseline).
    """
    return render(request, 'core/admin_dashboard.html', {
        'page_title': 'Administration & System Config',
    })


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
def health_check(request):
    """
    Verifies operational status and PostgreSQL 18 database connectivity.
    """
    db_ok = False
    db_version = "Unknown"
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT version();")
            row = cursor.fetchone()
            if row:
                db_version = row[0]
                db_ok = True
    except Exception as e:
        return JsonResponse({
            'status': 'DOWN',
            'database': 'ERROR',
            'detail': str(e)
        }, status=503)

    return JsonResponse({
        'status': 'UP',
        'application': 'Balaji InvoiceHub',
        'database': 'CONNECTED',
        'engine': 'PostgreSQL 18',
        'db_version': db_version,
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

