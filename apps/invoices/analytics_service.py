"""
Balaji InvoiceHub - Executive Analytics & Reporting Service (Phase 7)
Computes real database-backed financial metrics, monthly trends, supplier turnover,
and downloadable executive Excel audit reports.
"""
import io
from decimal import Decimal
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from django.db.models import Sum, Count, Avg, Q
from django.utils import timezone
from datetime import date, datetime
from .models import InvoiceSubmission, ExportAuditLog
from .export_security import safe_xlsx_value


def get_executive_kpis():
    """
    Computes live database-backed financial KPIs across all invoice submissions.
    Zero simulated or hardcoded values.
    """
    qs = InvoiceSubmission.objects.all()
    total_invoices = qs.count()

    sums = qs.aggregate(
        total_value=Sum('total_amount'),
        total_taxable=Sum('taxable_amount'),
        total_cgst=Sum('cgst_amount'),
        total_sgst=Sum('sgst_amount'),
        total_igst=Sum('igst_amount'),
    )

    total_value = sums['total_value'] or Decimal('0.00')
    total_taxable = sums['total_taxable'] or Decimal('0.00')
    total_cgst = sums['total_cgst'] or Decimal('0.00')
    total_sgst = sums['total_sgst'] or Decimal('0.00')
    total_igst = sums['total_igst'] or Decimal('0.00')
    total_tax = total_cgst + total_sgst + total_igst

    # Verification Pipeline
    verified_qs = qs.filter(status=InvoiceSubmission.STATUS_VERIFIED)
    verified_count = verified_qs.count()
    verified_value = verified_qs.aggregate(val=Sum('total_amount'))['val'] or Decimal('0.00')

    pending_qs = qs.filter(status__in=[InvoiceSubmission.STATUS_SUBMITTED, InvoiceSubmission.STATUS_RESUBMITTED])
    pending_count = pending_qs.count()
    pending_value = pending_qs.aggregate(val=Sum('total_amount'))['val'] or Decimal('0.00')

    returned_count = qs.filter(status=InvoiceSubmission.STATUS_RETURNED).count()

    # Tally Accounting Pipeline
    tally_done_qs = qs.filter(is_entered_in_tally=True)
    tally_done_count = tally_done_qs.count()
    tally_done_value = tally_done_qs.aggregate(val=Sum('total_amount'))['val'] or Decimal('0.00')

    tally_pending_qs = qs.filter(is_entered_in_tally=False)
    tally_pending_count = tally_pending_qs.count()
    tally_pending_value = tally_pending_qs.aggregate(val=Sum('total_amount'))['val'] or Decimal('0.00')

    tally_rate = (tally_done_count / total_invoices * 100) if total_invoices > 0 else 0.0

    # GST Filing Pipeline
    gst_done_count = qs.filter(is_filed_in_gst=True).count()
    gst_pending_count = qs.filter(is_filed_in_gst=False).count()

    # Compliance & Exception Metrics
    missing_gstin_count = qs.filter(Q(party_gstin__isnull=True) | Q(party_gstin='')).count()
    ambiguous_vendor_count = qs.filter(is_ambiguous_vendor=True).count()
    duplicate_count = qs.filter(is_duplicate_suspected=True).count()

    return {
        'total_invoices': total_invoices,
        'total_value': total_value,
        'total_taxable': total_taxable,
        'total_cgst': total_cgst,
        'total_sgst': total_sgst,
        'total_igst': total_igst,
        'total_tax': total_tax,
        'verified_count': verified_count,
        'verified_value': verified_value,
        'pending_count': pending_count,
        'pending_value': pending_value,
        'returned_count': returned_count,
        'tally_done_count': tally_done_count,
        'tally_done_value': tally_done_value,
        'tally_pending_count': tally_pending_count,
        'tally_pending_value': tally_pending_value,
        'tally_rate': round(tally_rate, 1),
        'gst_done_count': gst_done_count,
        'gst_pending_count': gst_pending_count,
        'missing_gstin_count': missing_gstin_count,
        'ambiguous_vendor_count': ambiguous_vendor_count,
        'duplicate_count': duplicate_count,
    }


def get_monthly_trends():
    """
    Aggregates invoice volumes and values month-by-month for the current financial year.
    """
    from django.db.models.functions import TruncMonth

    monthly_data = (
        InvoiceSubmission.objects
        .annotate(month=TruncMonth('created_at'))
        .values('month')
        .annotate(
            count=Count('id'),
            total_val=Sum('total_amount'),
            taxable_val=Sum('taxable_amount'),
            cgst_val=Sum('cgst_amount'),
            sgst_val=Sum('sgst_amount'),
            igst_val=Sum('igst_amount'),
        )
        .order_by('-month')[:12]
    )

    results = []
    for item in monthly_data:
        m_date = item['month']
        if m_date:
            results.append({
                'month_label': m_date.strftime('%B %Y'),
                'count': item['count'],
                'total_val': item['total_val'] or Decimal('0.00'),
                'taxable_val': item['taxable_val'] or Decimal('0.00'),
                'tax_val': (item['cgst_val'] or Decimal('0.00')) + (item['sgst_val'] or Decimal('0.00')) + (item['igst_val'] or Decimal('0.00'))
            })

    return results


def get_top_suppliers(limit=10):
    """
    Returns the top suppliers ranked by total expenditure.
    """
    top = (
        InvoiceSubmission.objects
        .exclude(party_name='')
        .values('party_name', 'party_gstin', 'expense_ledger')
        .annotate(
            invoice_count=Count('id'),
            total_spend=Sum('total_amount'),
            taxable_spend=Sum('taxable_amount'),
        )
        .order_by('-total_spend')[:limit]
    )
    return list(top)


def get_ledger_distribution():
    """
    Returns expenditure grouped by Tally Expense Ledger.
    """
    return list(
        InvoiceSubmission.objects
        .values('expense_ledger')
        .annotate(
            count=Count('id'),
            total_val=Sum('total_amount'),
            taxable_val=Sum('taxable_amount'),
        )
        .order_by('-total_val')
    )


def get_employee_upload_stats():
    """
    Returns invoice capture volume per employee collector.
    """
    return list(
        InvoiceSubmission.objects
        .values('employee__first_name', 'employee__last_name', 'employee__username')
        .annotate(
            count=Count('id'),
            total_val=Sum('total_amount'),
            tally_entered_count=Count('id', filter=Q(is_entered_in_tally=True))
        )
        .order_by('-count')
    )


def generate_executive_excel_report(report_type="monthly_summary", user=None):
    """
    Builds a presentation-ready Excel report for Management & Naveen.
    Report types:
    - 'monthly_summary'
    - 'supplier_summary'
    - 'compliance_audit'
    """
    wb = openpyxl.Workbook()
    ws = wb.active

    FONT_TITLE = Font(name="Calibri", size=14, bold=True, color="1E3A8A")
    FONT_HEADER = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
    FONT_DATA = Font(name="Calibri", size=10)
    FONT_TOTAL = Font(name="Calibri", size=10, bold=True, color="000000")

    FILL_HEADER = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
    FILL_TOTAL = PatternFill(start_color="E2E8F0", end_color="E2E8F0", fill_type="solid")
    BORDER_THIN = Border(
        left=Side(style='thin', color='CBD5E1'),
        right=Side(style='thin', color='CBD5E1'),
        top=Side(style='thin', color='CBD5E1'),
        bottom=Side(style='thin', color='CBD5E1')
    )
    BORDER_DOUBLE = Border(
        left=Side(style='thin', color='CBD5E1'),
        right=Side(style='thin', color='CBD5E1'),
        top=Side(style='thin', color='CBD5E1'),
        bottom=Side(style='double', color='000000')
    )

    if report_type == "supplier_summary":
        ws.title = "Supplier Turnover & Tax"
        ws['A1'] = "BALAJI TRANSPORTS — SUPPLIER-WISE PURCHASE TURNOVER REPORT"
        ws['A2'] = f"Generated by: {user.username if user else 'System'} on {timezone.now().strftime('%d-%b-%Y')} | Registered GSTIN: 29AAWFB0395H1ZX"
        ws['A1'].font = FONT_TITLE

        headers = [
            "Supplier / Vendor Name",
            "Supplier GSTIN",
            "Primary Ledger",
            "Invoice Count",
            "Taxable Turnover (₹)",
            "Total Purchase Value (₹)"
        ]
        ws.row_dimensions[4].height = 24
        for c, h in enumerate(headers, 1):
            cell = ws.cell(row=4, column=c, value=h)
            cell.font = FONT_HEADER
            cell.fill = FILL_HEADER
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = BORDER_THIN

        suppliers = get_top_suppliers(limit=100)
        curr_row = 5
        for s in suppliers:
            ws.cell(row=curr_row, column=1, value=safe_xlsx_value(s['party_name'] or 'Unregistered')).alignment = Alignment(horizontal="left", vertical="center")
            ws.cell(row=curr_row, column=2, value=safe_xlsx_value(s['party_gstin'] or '')).alignment = Alignment(horizontal="center", vertical="center")
            ws.cell(row=curr_row, column=3, value=safe_xlsx_value(s['expense_ledger'])).alignment = Alignment(horizontal="left", vertical="center")
            ws.cell(row=curr_row, column=4, value=s['invoice_count']).alignment = Alignment(horizontal="right", vertical="center")
            
            c5 = ws.cell(row=curr_row, column=5, value=float(s['taxable_spend'] or 0.0))
            c5.number_format = '#,##0.00'
            c5.alignment = Alignment(horizontal="right", vertical="center")

            c6 = ws.cell(row=curr_row, column=6, value=float(s['total_spend'] or 0.0))
            c6.number_format = '#,##0.00'
            c6.alignment = Alignment(horizontal="right", vertical="center")

            for col in range(1, 7):
                ws.cell(row=curr_row, column=col).border = BORDER_THIN
            curr_row += 1

        # Totals
        ws.row_dimensions[curr_row].height = 22
        for col in range(1, 7):
            ws.cell(row=curr_row, column=col).border = BORDER_DOUBLE
            ws.cell(row=curr_row, column=col).fill = FILL_TOTAL
            ws.cell(row=curr_row, column=col).font = FONT_TOTAL

        ws.cell(row=curr_row, column=3, value="TOTAL:")
        ws.cell(row=curr_row, column=4, value=f"=SUM(D5:D{curr_row - 1})")
        ws.cell(row=curr_row, column=5, value=f"=SUM(E5:E{curr_row - 1})")
        ws.cell(row=curr_row, column=5).number_format = '#,##0.00'
        ws.cell(row=curr_row, column=6, value=f"=SUM(F5:F{curr_row - 1})")
        ws.cell(row=curr_row, column=6).number_format = '#,##0.00'

    elif report_type == "compliance_audit":
        ws.title = "GST Compliance Audit"
        ws['A1'] = "BALAJI TRANSPORTS — GST COMPLIANCE & EXCEPTION AUDIT"
        ws['A2'] = f"Generated by: {user.username if user else 'System'} on {timezone.now().strftime('%d-%b-%Y')}"
        ws['A1'].font = FONT_TITLE

        headers = [
            "Tracking ID",
            "Bill No",
            "Bill Date",
            "Party Name",
            "GSTIN",
            "Total Amount (₹)",
            "Compliance Issue / Audit Flag",
            "Verification Status",
            "Tally Status"
        ]
        ws.row_dimensions[4].height = 24
        for c, h in enumerate(headers, 1):
            cell = ws.cell(row=4, column=c, value=h)
            cell.font = FONT_HEADER
            cell.fill = FILL_HEADER
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = BORDER_THIN

        exceptions = InvoiceSubmission.objects.filter(
            Q(party_gstin__isnull=True) | Q(party_gstin='') | Q(is_ambiguous_vendor=True) | Q(is_duplicate_suspected=True)
        ).order_by('-created_at')

        curr_row = 5
        for inv in exceptions:
            issue = []
            if not inv.party_gstin:
                issue.append("Missing GSTIN")
            if inv.is_ambiguous_vendor:
                issue.append("Ambiguous Vendor Match")
            if inv.is_duplicate_suspected:
                issue.append("Duplicate Hash Detected")

            ws.cell(row=curr_row, column=1, value=safe_xlsx_value(inv.submission_id)).alignment = Alignment(horizontal="center", vertical="center")
            ws.cell(row=curr_row, column=2, value=safe_xlsx_value(inv.invoice_number or '-')).alignment = Alignment(horizontal="center", vertical="center")
            ws.cell(row=curr_row, column=3, value=inv.invoice_date.strftime('%d-%m-%Y') if inv.invoice_date else '').alignment = Alignment(horizontal="center", vertical="center")
            ws.cell(row=curr_row, column=4, value=safe_xlsx_value(inv.party_name or 'Unregistered')).alignment = Alignment(horizontal="left", vertical="center")
            ws.cell(row=curr_row, column=5, value=safe_xlsx_value(inv.party_gstin or 'MISSING')).alignment = Alignment(horizontal="center", vertical="center")
            
            c6 = ws.cell(row=curr_row, column=6, value=float(inv.total_amount))
            c6.number_format = '#,##0.00'
            c6.alignment = Alignment(horizontal="right", vertical="center")

            ws.cell(row=curr_row, column=7, value=", ".join(issue)).alignment = Alignment(horizontal="left", vertical="center")
            ws.cell(row=curr_row, column=8, value=inv.get_status_display()).alignment = Alignment(horizontal="center", vertical="center")
            ws.cell(row=curr_row, column=9, value="Entered" if inv.is_entered_in_tally else "Pending").alignment = Alignment(horizontal="center", vertical="center")

            for col in range(1, 10):
                ws.cell(row=curr_row, column=col).border = BORDER_THIN
            curr_row += 1

    else:
        # Default: Monthly Summary
        ws.title = "Monthly Purchase Summary"
        ws['A1'] = "BALAJI TRANSPORTS — MONTHLY PURCHASE & TAX BREAKDOWN"
        ws['A2'] = f"Generated by: {user.username if user else 'System'} on {timezone.now().strftime('%d-%b-%Y')}"
        ws['A1'].font = FONT_TITLE

        headers = [
            "Month",
            "Invoices Count",
            "Taxable Purchases (₹)",
            "Total GST Tax (₹)",
            "Total Purchase Value (₹)"
        ]
        ws.row_dimensions[4].height = 24
        for c, h in enumerate(headers, 1):
            cell = ws.cell(row=4, column=c, value=h)
            cell.font = FONT_HEADER
            cell.fill = FILL_HEADER
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = BORDER_THIN

        trends = get_monthly_trends()
        curr_row = 5
        for t in trends:
            ws.cell(row=curr_row, column=1, value=t['month_label']).alignment = Alignment(horizontal="left", vertical="center")
            ws.cell(row=curr_row, column=2, value=t['count']).alignment = Alignment(horizontal="right", vertical="center")
            
            c3 = ws.cell(row=curr_row, column=3, value=float(t['taxable_val']))
            c3.number_format = '#,##0.00'
            c3.alignment = Alignment(horizontal="right", vertical="center")

            c4 = ws.cell(row=curr_row, column=4, value=float(t['tax_val']))
            c4.number_format = '#,##0.00'
            c4.alignment = Alignment(horizontal="right", vertical="center")

            c5 = ws.cell(row=curr_row, column=5, value=float(t['total_val']))
            c5.number_format = '#,##0.00'
            c5.alignment = Alignment(horizontal="right", vertical="center")

            for col in range(1, 6):
                ws.cell(row=curr_row, column=col).border = BORDER_THIN
            curr_row += 1

        # Totals
        ws.row_dimensions[curr_row].height = 22
        for col in range(1, 6):
            ws.cell(row=curr_row, column=col).border = BORDER_DOUBLE
            ws.cell(row=curr_row, column=col).fill = FILL_TOTAL
            ws.cell(row=curr_row, column=col).font = FONT_TOTAL

        ws.cell(row=curr_row, column=1, value="TOTAL:")
        if curr_row > 5:
            ws.cell(row=curr_row, column=2, value=f"=SUM(B5:B{curr_row - 1})")
            ws.cell(row=curr_row, column=3, value=f"=SUM(C5:C{curr_row - 1})")
            ws.cell(row=curr_row, column=3).number_format = '#,##0.00'
            ws.cell(row=curr_row, column=4, value=f"=SUM(D5:D{curr_row - 1})")
            ws.cell(row=curr_row, column=4).number_format = '#,##0.00'
            ws.cell(row=curr_row, column=5, value=f"=SUM(E5:E{curr_row - 1})")
            ws.cell(row=curr_row, column=5).number_format = '#,##0.00'

    # Auto-adjust column widths
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            if cell.row < 4:
                continue
            max_len = max(max_len, len(str(cell.value or '')))
        ws.column_dimensions[col_letter].width = max(max_len + 4, 14)

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    if user:
        ExportAuditLog.objects.create(
            export_type='EXECUTIVE_REPORT_EXCEL',
            exported_by=user,
            record_count=0,
            total_taxable=Decimal('0.00'),
            total_amount=Decimal('0.00'),
            filename=f"executive_report_{report_type}_{timezone.now().strftime('%Y%m%d_%H%M')}.xlsx",
            filters_applied={'report_type': report_type}
        )

    return buffer

