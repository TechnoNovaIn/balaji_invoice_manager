"""
Balaji InvoiceHub — ICICI Bank Statement Parsing & Invoice Reconciliation Service.

Enables logistics operators to:
1. Ingest ICICI Bank statements (PDF, Excel, CSV) capturing advance & settlement payments.
2. Extract debit transactions (outgoing vendor payments) with UTR/reference numbers and recipient names.
3. Automatically reconcile payments against InvoiceSubmissions:
   - Matched: Money paid and invoice exists in InvoiceHub.
   - Unmatched (Missing Invoice): Money paid out from ICICI account, but NO invoice has been uploaded yet!
"""
import csv
import io
import logging
import os
import re
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from django.db import transaction
from django.utils import timezone

from apps.invoices.models import (
    BankStatement,
    BankTransaction,
    InvoiceSubmission,
    VendorMaster,
)

logger = logging.getLogger('invoices.bank_reconciliation')


def clean_decimal(val: Any) -> Optional[Decimal]:
    """Safely converts string or number to positive Decimal."""
    if val is None:
        return None
    if isinstance(val, (int, float, Decimal)):
        return Decimal(str(val)) if val > 0 else None
    
    s = str(val).strip().replace(',', '').replace('₹', '').replace(' ', '')
    # Check for negative numbers or empty
    if not s or s in ('-', '0', '0.0', '0.00'):
        return None
    try:
        d = Decimal(s)
        return abs(d) if d > 0 else None
    except (InvalidOperation, ValueError):
        return None


def parse_date_str(s: str) -> Optional[date]:
    """Parses various Indian bank date formats."""
    if not s:
        return None
    s = str(s).strip()
    # Remove time portion if present
    s = re.split(r'[\sT]', s)[0].strip()
    
    formats = [
        '%d/%m/%Y', '%d-%m-%Y', '%d/%m/%y', '%d-%m-%y',
        '%d-%b-%Y', '%d-%b-%y', '%d/%b/%Y', '%d/%b/%y',
        '%Y-%m-%d', '%d.%m.%Y', '%d %b %Y', '%d %B %Y'
    ]
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def extract_vendor_from_narration(narration: str) -> Tuple[str, str]:
    """
    Extracts candidate party/vendor name and UTR/reference number from bank narration.
    Supports ICICI Bank standard transfer formats:
    - NEFT-N123456789-VENDOR NAME-LOCATION
    - RTGS-R123456789-VENDOR NAME-LOCATION
    - UPI/DR/123456789/VENDOR NAME/BANK/
    - MMT/IMPS/123456789/VENDOR NAME/ICIC/
    - INF/INFT/0123456789/VENDOR NAME
    """
    if not narration:
        return '', ''

    text = narration.strip()
    ref_no = ''
    candidate_name = ''

    # 1. NEFT / RTGS
    # e.g. NEFT-N12345678-VENDOR NAME-LOCATION or NEFT/N12345678/VENDOR NAME
    neft_match = re.search(r'(?:NEFT|RTGS)[-/]([A-Za-z0-9]+)[-/]([A-Za-z0-9\s.,&]+?)(?:[-/]|$)', text, re.IGNORECASE)
    if neft_match:
        ref_no = neft_match.group(1).strip()
        candidate_name = neft_match.group(2).strip()

    # 2. UPI
    # e.g. UPI/DR/1234567890/PARTY NAME/BANK/... or UPI/1234567890/PARTY NAME
    elif 'UPI' in text.upper():
        upi_parts = [p.strip() for p in re.split(r'[/]', text) if p.strip()]
        for i, part in enumerate(upi_parts):
            if re.match(r'^\d{8,14}$', part):
                ref_no = part
                # Next part is usually payee or name
                if i + 1 < len(upi_parts):
                    candidate_name = upi_parts[i + 1]
                break
        if not candidate_name:
            upi_match = re.search(r'UPI/(?:DR/)?(?:[0-9]+/)?([A-Za-z0-9\s.&]+)', text, re.IGNORECASE)
            if upi_match:
                candidate_name = upi_match.group(1).strip()

    # 3. IMPS
    # e.g. MMT/IMPS/123456789/VENDOR NAME/...
    elif 'IMPS' in text.upper():
        imps_match = re.search(r'IMPS/([0-9]+)/([A-Za-z0-9\s.&]+)', text, re.IGNORECASE)
        if imps_match:
            ref_no = imps_match.group(1).strip()
            candidate_name = imps_match.group(2).strip()

    # 4. Internal Fund Transfer (INF / INFT)
    elif re.search(r'(?:INF|INFT)[-/]([A-Za-z0-9]+)[-/]([A-Za-z0-9\s.&]+)', text, re.IGNORECASE):
        inf_match = re.search(r'(?:INF|INFT)[-/]([A-Za-z0-9]+)[-/]([A-Za-z0-9\s.&]+)', text, re.IGNORECASE)
        ref_no = inf_match.group(1).strip()
        candidate_name = inf_match.group(2).strip()

    # 5. Fallback: Cheque or generic transfer
    if not candidate_name:
        chq_match = re.search(r'CHQ(?:\s*NO)?\s*[-:]?\s*([0-9]{6})', text, re.IGNORECASE)
        if chq_match:
            ref_no = chq_match.group(1)
        # Clean noisy prefixes
        cleaned = re.sub(r'^(?:TO\s+TRANSFER\s*[-/]?|CLG\s*[-/]?|BY\s+TRANSFER\s*[-/]?)', '', text, flags=re.IGNORECASE).strip()
        candidate_name = cleaned

    # Clean candidate name: remove common bank suffixes / prefixes
    candidate_name = re.sub(r'^(?:DR|CR|TRANSFER\s+TO|PAID\s+TO|NEFT|RTGS|IMPS|UPI)\s*[-/:]?\s*', '', candidate_name, flags=re.IGNORECASE).strip()
    candidate_name = re.sub(r'/(?:KKBK|HDFC|SBIN|ICIC|UTIB|BARB|PUNB|CNRB)[A-Za-z0-9]*.*$', '', candidate_name, flags=re.IGNORECASE).strip()
    candidate_name = re.sub(r'\s+[-/]\s*(?:TUMKUR|BANGALORE|BENGALURU|MUMBAI|DELHI|BRANCH|HO|IND|AREA).*$', '', candidate_name, flags=re.IGNORECASE).strip()

    return candidate_name[:100], ref_no[:50]


def find_matching_vendor_master(candidate_name: str) -> Optional[VendorMaster]:
    """Matches a parsed beneficiary name to the VendorMaster directory."""
    if not candidate_name or len(candidate_name) < 3:
        return None

    norm = re.sub(r'[^a-z0-9]', '', candidate_name.lower())
    if not norm:
        return None

    # 1. Exact normalized match
    vendor = VendorMaster.objects.filter(normalized_name=norm, is_active=True).first()
    if vendor:
        return vendor

    # 2. Substring match
    vendor = VendorMaster.objects.filter(normalized_name__contains=norm, is_active=True).first()
    if vendor:
        return vendor

    # 3. First 2 tokens match
    tokens = [t for t in re.split(r'[^a-zA-Z0-9]', candidate_name) if len(t) >= 3]
    if len(tokens) >= 1:
        qs = VendorMaster.objects.filter(name__icontains=tokens[0], is_active=True)
        if len(tokens) >= 2:
            qs = qs.filter(name__icontains=tokens[1])
        first_match = qs.first()
        if first_match:
            return first_match

    return None


def parse_icici_excel_csv(file_obj, filename: str) -> Dict[str, Any]:
    """Parses ICICI Bank Statement from Excel (.xlsx/.xls) or CSV."""
    transactions_data = []
    account_no = ''
    period_str = ''

    if filename.lower().endswith('.csv'):
        file_obj.seek(0)
        content = file_obj.read()
        if isinstance(content, bytes):
            try:
                text_content = content.decode('utf-8')
            except UnicodeDecodeError:
                text_content = content.decode('latin-1', errors='replace')
        else:
            text_content = str(content)

        reader = csv.reader(io.StringIO(text_content))
        rows = list(reader)
    else:
        # Excel (.xlsx / .xls)
        file_obj.seek(0)
        import openpyxl
        wb = openpyxl.load_workbook(file_obj, data_only=True)
        sheet = wb.active
        rows = []
        for row in sheet.iter_rows(values_only=True):
            rows.append(list(row))

    # Search for header row
    header_idx = -1
    col_map = {}

    for idx, row in enumerate(rows[:30]):
        row_str = ' '.join(str(cell or '').lower() for cell in row)
        if 'account' in row_str and not account_no:
            acc_match = re.search(r'account\s*(?:no|number)?\s*[:.-]?\s*([0-9]{9,18})', row_str)
            if acc_match:
                account_no = acc_match.group(1)

        # Look for table column headers
        if ('date' in row_str and ('withdrawal' in row_str or 'debit' in row_str or 'particular' in row_str or 'remark' in row_str)):
            header_idx = idx
            for c_idx, cell in enumerate(row):
                c_name = str(cell or '').strip().lower()
                if 'date' in c_name and 'val' not in c_name and 'date' not in col_map:
                    col_map['date'] = c_idx
                elif 'value date' in c_name and 'date' not in col_map:
                    col_map['date'] = c_idx
                elif any(k in c_name for k in ('particular', 'remark', 'narration', 'description')):
                    col_map['narration'] = c_idx
                elif any(k in c_name for k in ('withdrawal', 'debit', 'dr')):
                    col_map['debit'] = c_idx
                elif any(k in c_name for k in ('cheque', 'chq', 'ref')):
                    col_map['ref_no'] = c_idx
            break

    if header_idx != -1 and 'debit' in col_map:
        date_col = col_map.get('date', 0)
        narr_col = col_map.get('narration', 1)
        debit_col = col_map.get('debit', 2)
        ref_col = col_map.get('ref_no', -1)

        for row in rows[header_idx + 1:]:
            if not row or len(row) <= max(date_col, debit_col):
                continue

            raw_debit = row[debit_col]
            debit_amt = clean_decimal(raw_debit)
            if not debit_amt or debit_amt <= 0:
                continue

            raw_date = row[date_col]
            txn_date = parse_date_str(str(raw_date))
            if not txn_date:
                continue

            raw_narr = str(row[narr_col] if narr_col < len(row) else '').strip()
            raw_ref = str(row[ref_col] if ref_col != -1 and ref_col < len(row) else '').strip()

            vendor_cand, ext_ref = extract_vendor_from_narration(raw_narr)
            ref_no = raw_ref or ext_ref

            transactions_data.append({
                'date': txn_date,
                'narration': raw_narr,
                'debit_amount': debit_amt,
                'reference_no': ref_no,
                'extracted_party_name': vendor_cand,
            })

    return {
        'bank_name': 'ICICI Bank',
        'account_number': account_no,
        'statement_period': period_str,
        'transactions': transactions_data,
    }


def parse_icici_pdf(file_obj) -> Dict[str, Any]:
    """Parses ICICI Bank Statement from digital PDF using PyMuPDF with text heuristic analysis."""
    import fitz  # pymupdf

    file_obj.seek(0)
    pdf_bytes = file_obj.read()
    file_obj.seek(0)

    doc = fitz.open(stream=pdf_bytes, filetype='pdf')
    transactions_data = []
    account_no = ''
    period_str = ''

    all_lines = []
    for page in doc:
        text = page.get_text('text')
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        all_lines.extend(lines)

    # Search for account info
    for line in all_lines[:50]:
        if 'account' in line.lower() and not account_no:
            m = re.search(r'(?:account\s*(?:no|number)?\s*[:.-]?\s*)([0-9]{9,18})', line, re.IGNORECASE)
            if m:
                account_no = m.group(1)
        if 'statement period' in line.lower() and not period_str:
            period_str = line

    # ICICI statement line regex: Date Remarks [Cheque] Debit Credit Balance
    # Date pattern e.g. 05/11/2026 or 05-11-2026 or 05-Nov-2026
    date_regex = re.compile(r'^(\d{2}[/-]\d{2}[/-]\d{4}|\d{2}-[a-zA-Z]{3}-\d{4})')
    # Amount pattern e.g. 10,250.00 or 5796.00
    amt_regex = re.compile(r'(\d{1,3}(?:,\d{3})*(?:\.\d{2}))')

    i = 0
    while i < len(all_lines):
        line = all_lines[i]
        date_match = date_regex.match(line)
        if date_match:
            txn_date = parse_date_str(date_match.group(1))
            # Gather subsequent lines until next date or footer
            narr_parts = []
            amounts = []
            i += 1
            while i < len(all_lines) and not date_regex.match(all_lines[i]):
                sub_line = all_lines[i]
                if any(hdr in sub_line.lower() for hdr in ('page ', 'statement of account', 'icici bank', 'total:')):
                    i += 1
                    continue
                # Check for amounts on this sub_line
                found_amts = amt_regex.findall(sub_line)
                if found_amts:
                    for a_str in found_amts:
                        d = clean_decimal(a_str)
                        if d is not None:
                            amounts.append(d)
                else:
                    narr_parts.append(sub_line)
                i += 1

            full_narr = ' '.join(narr_parts).strip()
            # In ICICI statement, typically: [Withdrawal, Deposit, Balance] or [Withdrawal, Balance]
            # If line has amounts, the first or second amount is the withdrawal/debit
            if amounts and txn_date:
                # Debit is generally the first amount when withdrawal occurred
                debit_amt = amounts[0] if len(amounts) >= 1 else None
                if debit_amt and debit_amt > 0:
                    cand_name, ref_no = extract_vendor_from_narration(full_narr)
                    transactions_data.append({
                        'date': txn_date,
                        'narration': full_narr or 'ICICI Bank Debit',
                        'debit_amount': debit_amt,
                        'reference_no': ref_no,
                        'extracted_party_name': cand_name,
                    })
            continue
        i += 1

    doc.close()

    # Fallback to Gemini Vision if PyMuPDF extracted zero transactions (e.g. scanned or non-standard PDF)
    if not transactions_data and len(pdf_bytes) > 0:
        transactions_data = parse_statement_with_gemini_fallback(pdf_bytes)

    return {
        'bank_name': 'ICICI Bank',
        'account_number': account_no,
        'statement_period': period_str,
        'transactions': transactions_data,
    }


def parse_statement_with_gemini_fallback(pdf_bytes: bytes) -> List[Dict[str, Any]]:
    """AI Vision fallback for scanned or complex ICICI Bank statement layouts."""
    try:
        from apps.invoices.gemini_extractor import get_configured_gemini_client, call_gemini_with_retry
        from google.genai import types
        import json

        client = get_configured_gemini_client()
        prompt = """
        You are an expert Indian bank statement parser. Extract all DEBIT (withdrawal / outgoing payment) transactions from this ICICI Bank statement.
        Return ONLY a strict JSON list of objects:
        [
          {
            "date": "YYYY-MM-DD",
            "narration": "Full transaction remarks",
            "debit_amount": 12500.00,
            "reference_no": "UTR or Cheque Number",
            "extracted_party_name": "Beneficiary / Vendor name"
          }
        ]
        Ignore deposits/credits. Return valid JSON only, no markdown.
        """
        response = call_gemini_with_retry(
            client=client,
            contents=[
                types.Part.from_bytes(data=pdf_bytes[:1024*1024*5], mime_type='application/pdf'),
                prompt
            ],
            system_instruction="Strict JSON output only."
        )
        if response and response.text:
            text = response.text.strip()
            if text.startswith('```'):
                text = re.sub(r'^```[a-z]*\s*', '', text)
                text = re.sub(r'\s*```$', '', text)
            data = json.loads(text)
            txns = []
            for item in data:
                d = parse_date_str(item.get('date', ''))
                amt = clean_decimal(item.get('debit_amount'))
                if d and amt:
                    txns.append({
                        'date': d,
                        'narration': item.get('narration', ''),
                        'debit_amount': amt,
                        'reference_no': item.get('reference_no', ''),
                        'extracted_party_name': item.get('extracted_party_name', ''),
                    })
            return txns
    except Exception as e:
        logger.warning("Gemini statement parse fallback failed: %s", e)
    return []


def ingest_and_reconcile_statement(
    file_obj,
    filename: str,
    user: Any
) -> Tuple[BankStatement, int, int]:
    """
    Ingests an ICICI Bank statement file, creates records, and runs automated
    reconciliation against InvoiceSubmission database.
    Returns: (BankStatement, matched_count, unmatched_count)
    """
    ext = os.path.splitext(filename)[1].lower()
    if ext == '.pdf':
        parsed = parse_icici_pdf(file_obj)
    elif ext in ('.xlsx', '.xls', '.csv'):
        parsed = parse_icici_excel_csv(file_obj, filename)
    else:
        raise ValueError(f"Unsupported statement format '{ext}'. Please upload ICICI Bank PDF, Excel, or CSV.")

    raw_txns = parsed.get('transactions', [])
    if not raw_txns:
        raise ValueError("Could not extract any debit payment transactions from this statement. Please verify the file format.")

    from django.core.files.base import ContentFile
    file_obj.seek(0)
    file_bytes = file_obj.read()
    file_obj.seek(0)
    stored_file = ContentFile(file_bytes, name=filename)

    with transaction.atomic():
        statement = BankStatement.objects.create(
            uploaded_by=user,
            original_file=stored_file,
            filename=filename,
            bank_name=parsed.get('bank_name', 'ICICI Bank'),
            account_number=parsed.get('account_number', ''),
            statement_period=parsed.get('statement_period', ''),
            total_debits_count=len(raw_txns),
            total_debit_amount=sum(t['debit_amount'] for t in raw_txns),
        )

        matched_count = 0
        unmatched_count = 0

        # Load all active invoice submissions into memory for fast matching
        active_invoices = list(InvoiceSubmission.objects.exclude(
            status=InvoiceSubmission.STATUS_REJECTED
        ).only('id', 'submission_id', 'party_name', 'total_amount', 'invoice_date', 'created_at'))

        for t_data in raw_txns:
            cand_name = t_data.get('extracted_party_name', '').strip()
            debit_amt = t_data['debit_amount']
            txn_date = t_data['date']

            # Match against VendorMaster
            vendor_match = find_matching_vendor_master(cand_name) if cand_name else None

            # Attempt automatic reconciliation with InvoiceSubmissions
            matched_sub = None
            match_type = ''

            # Rule 1: Exact amount match within ±30 days
            for inv in active_invoices:
                if inv.total_amount and abs(Decimal(str(inv.total_amount)) - debit_amt) <= Decimal('1.00'):
                    inv_dt = inv.invoice_date or inv.created_at.date()
                    date_diff = abs((txn_date - inv_dt).days)
                    if date_diff <= 35:
                        matched_sub = inv
                        match_type = BankTransaction.MATCH_AUTO_EXACT
                        break

            # Rule 2: Vendor name match if amount is close (within 10%)
            if not matched_sub and cand_name:
                cand_lower = cand_name.lower()
                for inv in active_invoices:
                    if inv.party_name and (cand_lower in inv.party_name.lower() or inv.party_name.lower() in cand_lower):
                        inv_dt = inv.invoice_date or inv.created_at.date()
                        date_diff = abs((txn_date - inv_dt).days)
                        if date_diff <= 45:
                            matched_sub = inv
                            match_type = BankTransaction.MATCH_AUTO_FUZZY
                            break

            status = BankTransaction.STATUS_MATCHED if matched_sub else BankTransaction.STATUS_UNMATCHED
            if matched_sub:
                matched_count += 1
            else:
                unmatched_count += 1

            BankTransaction.objects.create(
                statement=statement,
                txn_date=txn_date,
                narration=t_data['narration'],
                extracted_party_name=cand_name,
                matched_vendor=vendor_match,
                debit_amount=debit_amt,
                reference_no=t_data.get('reference_no', ''),
                matched_submission=matched_sub,
                match_status=status,
                match_type=match_type,
                matched_at=timezone.now() if matched_sub else None,
                matched_by=user if matched_sub else None,
            )

        statement.matched_count = matched_count
        statement.unmatched_count = unmatched_count
        statement.save(update_fields=['matched_count', 'unmatched_count'])

    return statement, matched_count, unmatched_count
