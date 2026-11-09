"""
Balaji InvoiceHub - Purchase Register & Audit Service (Phase 5)
Handles purchase register filtering, audit trail recording, and GSTR-2A reconciliation.
"""
from decimal import Decimal
from django.db.models import Q, Sum, Count
from django.utils import timezone
from .models import InvoiceSubmission, PurchaseAuditTrail, ExportAuditLog
from .vendor_directory import VENDOR_DIRECTORY, GSTIN_VALIDATION_REGEX
import re


def filter_purchase_register(params, base_qs=None):
    """
    Comprehensive multi-criteria filtering for the Centralized Purchase Register.
    Supports query terms, date ranges, verification status, Tally status, GST filing status,
    supplier filtering, employee filtering, and sorting.
    """
    if base_qs is None:
        qs = InvoiceSubmission.objects.all().select_related('employee', 'verified_by', 'tally_entered_by', 'source_document').prefetch_related('documents')
    else:
        qs = base_qs

    q = params.get('q', '').strip()
    status = params.get('status', '').strip()
    tally_status = params.get('tally_status', '').strip()
    gst_status = params.get('gst_status', '').strip()
    supplier_gstin = params.get('supplier_gstin', '').strip()
    employee_id = params.get('employee_id', '').strip()
    from_date = params.get('from_date', '').strip()
    to_date = params.get('to_date', '').strip()
    date_type = params.get('date_type', 'invoice_date').strip()  # 'invoice_date' or 'upload_date'
    sort_by = params.get('sort_by', '-invoice_date').strip()
    gst_compliance = params.get('gst_compliance', '').strip()  # 'valid', 'missing_gstin', 'discrepancy'

    if q:
        qs = qs.filter(
            Q(submission_id__icontains=q) |
            Q(party_name__icontains=q) |
            Q(party_gstin__icontains=q) |
            Q(invoice_number__icontains=q) |
            Q(employee__first_name__icontains=q) |
            Q(employee__last_name__icontains=q) |
            Q(employee__username__icontains=q) |
            Q(remarks__icontains=q)
        )

    if status and status != 'ALL':
        qs = qs.filter(status=status)

    if tally_status:
        if tally_status == 'ENTERED':
            qs = qs.filter(is_entered_in_tally=True)
        elif tally_status == 'PENDING':
            qs = qs.filter(is_entered_in_tally=False)

    if gst_status:
        if gst_status == 'FILED':
            qs = qs.filter(is_filed_in_gst=True)
        elif gst_status == 'PENDING':
            qs = qs.filter(is_filed_in_gst=False)

    if supplier_gstin:
        qs = qs.filter(party_gstin__iexact=supplier_gstin)

    if employee_id:
        qs = qs.filter(employee_id=employee_id)

    # Date Range Filter
    date_field = 'invoice_date' if date_type == 'invoice_date' else 'created_at__date'
    if from_date:
        qs = qs.filter(**{f"{date_field}__gte": from_date})
    if to_date:
        qs = qs.filter(**{f"{date_field}__lte": to_date})

    # GST Compliance Filter
    if gst_compliance == 'missing_gstin':
        qs = qs.filter(Q(party_gstin__isnull=True) | Q(party_gstin=''))
    elif gst_compliance == 'valid':
        qs = qs.exclude(party_gstin='').exclude(party_gstin__isnull=True)

    # Sorting
    allowed_sorts = {
        '-invoice_date': '-invoice_date',
        'invoice_date': 'invoice_date',
        '-created_at': '-created_at',
        'created_at': 'created_at',
        '-total_amount': '-total_amount',
        'total_amount': 'total_amount',
        'party_name': 'party_name',
        'submission_id': 'submission_id'
    }
    order_field = allowed_sorts.get(sort_by, '-created_at')
    qs = qs.order_by(order_field)

    return qs


def record_field_corrections(submission, new_data, user, reason="Manual verification update"):
    """
    Detects changes to candidate financial/supplier fields and records immutable
    entries into PurchaseAuditTrail.
    """
    fields_to_track = [
        ('party_name', 'Party Name'),
        ('party_gstin', 'Supplier GSTIN'),
        ('invoice_number', 'Invoice Number'),
        ('invoice_date', 'Invoice Date'),
        ('taxable_amount', 'Taxable Amount'),
        ('cgst_amount', 'CGST Amount'),
        ('sgst_amount', 'SGST Amount'),
        ('igst_amount', 'IGST Amount'),
        ('total_amount', 'Total Amount'),
        ('expense_ledger', 'Expense Ledger'),
        ('hsn_sac_code', 'HSN/SAC Code'),
    ]

    updated_fields = []

    for field_attr, label in fields_to_track:
        if field_attr in new_data:
            old_val = getattr(submission, field_attr)
            new_val = new_data[field_attr]

            # Format comparison for decimals / dates
            old_str = str(old_val) if old_val is not None else ''
            new_str = str(new_val) if new_val is not None else ''

            if old_str != new_str:
                PurchaseAuditTrail.objects.create(
                    submission=submission,
                    field_name=label,
                    old_value=old_str,
                    new_value=new_str,
                    changed_by=user,
                    reason=reason
                )
                setattr(submission, field_attr, new_val)
                updated_fields.append(field_attr)

    if updated_fields:
        updated_fields.append('updated_at')
        submission.save(update_fields=updated_fields)

    return updated_fields


def compare_with_gstr2a():
    """
    Reconciles all verified purchase invoices with the 104 verified GSTR-2A supplier database.
    Categorizes invoices into:
    - Verified GSTR-2A Matches (GSTIN matches verified supplier)
    - Unregistered / Unknown Suppliers (GSTIN not in GSTR-2A)
    - Missing GSTINs
    - Inter-State vs Intra-State Tax consistency checks
    """
    invoices = InvoiceSubmission.objects.all().order_by('-invoice_date')
    
    matched = []
    missing_in_2a = []
    missing_gstin = []
    tax_discrepancies = []

    balaji_state_code = '29'  # Karnataka

    for inv in invoices:
        gstin = (inv.party_gstin or '').strip().upper()
        if not gstin:
            missing_gstin.append({
                'invoice': inv,
                'issue': 'Supplier GSTIN is missing from receipt'
            })
            continue

        supplier_record = VENDOR_DIRECTORY.get(gstin)

        # Tax logic consistency check
        is_interstate = not gstin.startswith(balaji_state_code)
        has_tax_mismatch = False
        discrepancy_note = ''

        if is_interstate and (inv.cgst_amount > 0 or inv.sgst_amount > 0):
            has_tax_mismatch = True
            discrepancy_note = f'Inter-state supplier ({gstin[:2]}) has CGST/SGST charged instead of IGST'
        elif not is_interstate and inv.igst_amount > 0:
            has_tax_mismatch = True
            discrepancy_note = f'Intra-state supplier (Karnataka 29) has IGST charged instead of CGST+SGST'

        calc_total = inv.taxable_amount + inv.cgst_amount + inv.sgst_amount + inv.igst_amount
        if inv.total_amount > 0 and abs(calc_total - inv.total_amount) > Decimal('1.00'):
            has_tax_mismatch = True
            discrepancy_note = f'Sum of taxable + tax (₹{calc_total:.2f}) does not match Total (₹{inv.total_amount:.2f})'

        if has_tax_mismatch:
            tax_discrepancies.append({
                'invoice': inv,
                'issue': discrepancy_note
            })

        if supplier_record:
            matched.append({
                'invoice': inv,
                'gstr2a_vendor': supplier_record['name'],
                'gstr2a_state': supplier_record['state'],
                'ledger': supplier_record['ledger']
            })
        else:
            missing_in_2a.append({
                'invoice': inv,
                'issue': 'GSTIN valid format but not in GSTR-2A verified directory'
            })

    return {
        'total_invoices': invoices.count(),
        'matched_count': len(matched),
        'missing_in_2a_count': len(missing_in_2a),
        'missing_gstin_count': len(missing_gstin),
        'tax_discrepancies_count': len(tax_discrepancies),
        'matched': matched,
        'missing_in_2a': missing_in_2a,
        'missing_gstin': missing_gstin,
        'tax_discrepancies': tax_discrepancies
    }

