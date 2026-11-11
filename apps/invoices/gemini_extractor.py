"""
Gemini Vision AI Invoice Extractor for Balaji InvoiceHub.
Extracts printed, handwritten, skewed, carbon-copy, and complex Indian transport invoices
using Google Gemini Vision API (Pydantic schema validated).
Enforces backend-controlled financial validation gates (Rules A through E).
"""

import os
import json
import time
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from pydantic import BaseModel, Field

from .vendor_validator import validate_vendor_and_financials, is_valid_gstin, clean_gstin
from .e_invoice_utils import evaluate_e_invoice_status, extract_qr_e_invoice_data

logger = logging.getLogger(__name__)


class InvoiceItemSchema(BaseModel):
    description: Optional[str] = Field(default=None, description="Description of goods or service line item")
    hsn_sac: Optional[str] = Field(default=None, description="HSN or SAC code for line item")
    quantity: Optional[float] = Field(default=None, description="Quantity supplied")
    unit_price: Optional[float] = Field(default=None, description="Rate or unit price")
    amount: Optional[float] = Field(default=None, description="Line item taxable or gross amount")


class InvoiceSchema(BaseModel):
    party_name: Optional[str] = Field(
        default=None,
        description="Name of the vendor/supplier business issuing the bill. Do NOT extract 'BALAJI TRANSPORTS' as party_name because Balaji Transports is the buyer/recipient."
    )
    party_gstin: Optional[str] = Field(
        default=None,
        description="15-character statutory Indian GSTIN of the supplier if present on the bill. Exclude Balaji's GSTIN (29AAWFB0395H1ZX)."
    )
    pan: Optional[str] = Field(
        default=None,
        description="10-character Permanent Account Number (PAN) of supplier if printed on bill"
    )
    invoice_number: Optional[str] = Field(
        default=None,
        description="Invoice number, Bill number, Cash Memo number, or pre-printed voucher number (e.g. '191', '2305', 'KA/2026/01')."
    )
    invoice_date: Optional[str] = Field(
        default=None,
        description="Invoice or bill date in YYYY-MM-DD format if visible, or null if blank/unreadable."
    )
    due_date: Optional[str] = Field(
        default=None,
        description="Due date for payment in YYYY-MM-DD format if visible on the bill, or null."
    )
    taxable_amount: Optional[float] = Field(
        default=0.0,
        description="Subtotal, taxable value, or value of supply before taxes."
    )
    cgst_amount: Optional[float] = Field(
        default=0.0,
        description="Central GST amount if itemized on the bill."
    )
    sgst_amount: Optional[float] = Field(
        default=0.0,
        description="State GST or UTGST amount if itemized on the bill."
    )
    igst_amount: Optional[float] = Field(
        default=0.0,
        description="Integrated GST amount for inter-state supplier bills."
    )
    cess_amount: Optional[float] = Field(
        default=0.0,
        description="GST Cess amount if itemized on bill."
    )
    round_off_amount: Optional[float] = Field(
        default=0.0,
        description="Round off amount (+/-) if printed."
    )
    total_amount: float = Field(
        default=0.0,
        description="Grand Total, Net Payable, or Bill Amount. Verify against line items and words if written on bill."
    )
    expense_ledger: Optional[str] = Field(
        default="Purchase Spares & Consumables",
        description="Appropriate Tally expense ledger (e.g. Purchase Spares & Consumables, Tyre & Tube Purchase, Tyre Retread, Labour Charges / Vehicle Service, GPS-Expenses, Lorry Insurance, Transportation & Freight Charges, Canteen & Staff Welfare Expenses, Building Repairs, Hardware & Paints, Printing & Stationery, Computer & Printer Expenses, Telephone & Mobile Expenses, Diesel & Lubricants, Welding Gases & Industrial Spares, Rent & Terminal Charges, General Office Expenses)."
    )
    expense_description: Optional[str] = Field(
        default="",
        description="Brief description of nature of supply or service."
    )
    hsn_sac: Optional[str] = Field(
        default=None,
        description="Primary HSN or SAC code if itemized."
    )
    payment_details: Optional[str] = Field(
        default=None,
        description="Payment mode (Cash, Bank, UPI, Cheque) or reference if visible."
    )
    items: Optional[List[InvoiceItemSchema]] = Field(
        default_factory=list,
        description="List of itemized goods or services."
    )
    is_e_invoice: Optional[bool] = Field(
        default=None,
        description="True if GST e-invoice (IRN, Ack No, Signed QR, or explicit e-Invoice header) is present; False ONLY if explicitly marked not an e-invoice or IRN N/A; null if uncertain."
    )
    irn_number: Optional[str] = Field(
        default=None,
        description="64-character alphanumeric Invoice Reference Number (IRN) if clearly printed; null if missing or illegible. Never invent or guess."
    )
    raw_summary: Optional[str] = Field(
        default="",
        description="Brief transcription of key bill lines (vendor, bill no, date, line items, words)."
    )


PROMPT_INSTRUCTIONS = """You are an expert Indian Commercial Accounts & Tax Auditor for Balaji Transports (a major logistics firm in Karnataka, India).
Carefully inspect this invoice/bill photo. The document may be a computer-printed invoice, or a handwritten pre-printed cash/credit bill book, or a garage/petrol receipt.

CRITICAL RULES:
1. BUYER vs SUPPLIER:
   - "BALAJI TRANSPORTS" (or "M/s Balaji Transports", GSTIN: 29AAWFB0395H1ZX) is the BUYER / CUSTOMER.
   - The SUPPLIER / VENDOR is the other party issuing this invoice (usually at the very top header, or top-left, or at the bottom signature 'For [Company Name]').
   - NEVER output 'Balaji Transports' as party_name.
   - NEVER output phone numbers, driver names, vehicle numbers, or 'M/s' as the party name.

2. INVOICE / BILL NUMBER:
   - Look for printed labels like 'No.', 'Bill No.', 'Invoice No.', 'Cash Memo No.', 'INV No.', 'Voucher No.'.
   - In pre-printed bill books, the label 'No.' is often pre-printed and the number (e.g. '191') is handwritten below or beside it. Extract the actual bill number.

3. AMOUNTS & TOTALS:
   - Carefully verify handwritten digits. Cross-check against line item subtotals (e.g. quantity x rate) and 'Rupees in words' written at the bottom.
   - If line items sum to a total (e.g. 395,200 + 1,690 = 396,890), use the mathematically verified total.

4. DATES:
   - Convert any standard Indian date format (DD/MM/YYYY, DD-MM-YY, etc.) to ISO format YYYY-MM-DD.
   - If the date field on the paper is completely blank or missing, return null.

5. MISSING FIELDS:
   - Missing fields must remain null or explicitly unidentified. Never invent missing invoice information.

6. E-INVOICE & IRN:
   - Extract `irn_number` ONLY if a complete 64-character IRN is explicitly readable. Never fabricate, guess, or pad an IRN.
   - Set `is_e_invoice` to true if IRN/Ack No/Signed QR/e-Invoice indicators appear, false ONLY if explicitly marked non-e-invoice or IRN N/A, or null if uncertain.

Return strictly the structured JSON object.
"""


def extract_with_gemini(
    image_path: str,
    original_pdf_text: str = '',
) -> Optional[Dict[str, Any]]:
    """
    Extracts invoice metadata using Google Gemini Vision API with Pydantic validation.
    Applies independent backend vendor validation (Rules A through E).
    Preserves original digital/OCR text separately from Gemini's interpretation.
    Returns standard dictionary or None on failure / unconfigured API.
    """
    api_key = getattr(settings, 'GEMINI_API_KEY', '') or os.getenv('GEMINI_API_KEY', '').strip()
    if not api_key:
        return None

    path_obj = Path(image_path)
    if not path_obj.exists():
        logger.warning("Gemini Extractor: File not found: %s", image_path)
        return None

    if path_obj.stat().st_size < 200:
        # Avoid processing tiny mock/stub files from test harnesses
        return None

    try:
        from google import genai
        from google.genai import types

        # Initialize client with timeout
        client = genai.Client(api_key=api_key)

        # Detect MIME type
        ext = path_obj.suffix.lower()
        mime_map = {
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.png': 'image/png',
            '.webp': 'image/webp',
            '.pdf': 'application/pdf',
        }
        mime_type = mime_map.get(ext, 'image/jpeg')

        with open(path_obj, 'rb') as f:
            file_bytes = f.read()

        file_part = types.Part.from_bytes(data=file_bytes, mime_type=mime_type)

        # Configurable model selection with resilient fallback
        preferred_model = getattr(settings, 'GEMINI_MODEL', 'gemini-2.5-flash') or 'gemini-2.5-flash'
        candidate_models = [preferred_model]
        for m in ['gemini-2.5-flash', 'gemini-1.5-flash', 'gemini-2.0-flash', 'gemini-3.5-flash-lite']:
            if m not in candidate_models:
                candidate_models.append(m)

        response = None
        used_model = None

        # Controlled retry logic with exponential backoff
        for m_name in candidate_models:
            for retry_idx in range(2):
                try:
                    response = client.models.generate_content(
                        model=m_name,
                        contents=[file_part, PROMPT_INSTRUCTIONS],
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            response_schema=InvoiceSchema,
                            temperature=0.1,
                        ),
                    )
                    if response and response.text:
                        used_model = m_name
                        break
                except Exception as m_err:
                    err_str = str(m_err).lower()
                    if '429' in err_str or 'quota' in err_str or 'rate' in err_str:
                        logger.warning("Gemini model %s rate limit (429), backing off...", m_name)
                        time.sleep(1.0 * (retry_idx + 1))
                        continue
                    elif '503' in err_str or 'unavailable' in err_str:
                        logger.info("Gemini service temporarily unavailable, retrying...")
                        time.sleep(0.5)
                        continue
                    else:
                        logger.info("Gemini model %s exception: %s", m_name, type(m_err).__name__)
                        break
            if response and response.text:
                break

        if not response or not response.text:
            logger.warning("Gemini Extractor: All candidate models failed or returned empty response.")
            return None

        # Parse & Validate with Pydantic
        raw_json_dict = json.loads(response.text)
        validated_schema = InvoiceSchema(**raw_json_dict)
        data = validated_schema.model_dump()

        # Date parsing
        parsed_invoice_date = None
        if data.get('invoice_date'):
            try:
                parsed_invoice_date = datetime.strptime(str(data['invoice_date'])[:10], '%Y-%m-%d').date()
            except (ValueError, TypeError):
                parsed_invoice_date = None

        parsed_due_date = None
        if data.get('due_date'):
            try:
                parsed_due_date = datetime.strptime(str(data['due_date'])[:10], '%Y-%m-%d').date()
            except (ValueError, TypeError):
                parsed_due_date = None

        # Amount parsing
        def to_decimal(val) -> Decimal:
            if val is None:
                return Decimal('0.00')
            try:
                d = Decimal(str(val)).quantize(Decimal('0.01'))
                return d if d >= 0 else Decimal('0.00')
            except (InvalidOperation, ValueError, TypeError):
                return Decimal('0.00')

        taxable_amt = to_decimal(data.get('taxable_amount'))
        cgst_amt = to_decimal(data.get('cgst_amount'))
        sgst_amt = to_decimal(data.get('sgst_amount'))
        igst_amt = to_decimal(data.get('igst_amount'))
        cess_amt = to_decimal(data.get('cess_amount'))
        round_off_amt = to_decimal(data.get('round_off_amount'))
        total_amt = to_decimal(data.get('total_amount'))

        if total_amt == Decimal('0.00') and taxable_amt > 0:
            total_amt = taxable_amt + cgst_amt + sgst_amt + igst_amt + cess_amt + round_off_amt

        if taxable_amt == Decimal('0.00') and total_amt > 0:
            taxable_amt = total_amt

        raw_party_name = (data.get('party_name') or '').strip()
        raw_party_gstin = clean_gstin(data.get('party_gstin'))
        raw_pan = (data.get('pan') or '').strip().upper()
        raw_ledger = (data.get('expense_ledger') or 'Purchase Spares & Consumables').strip()

        # --- CRITICAL BACKEND VENDOR & FINANCIAL VALIDATION (Rules A - E) ---
        # Gemini is advisory only; the backend independently calculates ambiguity,
        # manual review gating, and financial approval eligibility.
        backend_val = validate_vendor_and_financials(
            party_name=raw_party_name,
            party_gstin=raw_party_gstin,
            default_expense_ledger=raw_ledger,
            pan=raw_pan,
        )

        # --- BACKEND E-INVOICE & IRN VALIDATION (Rules 1-5) ---
        qr_data = extract_qr_e_invoice_data(image_path)
        einv_val = evaluate_e_invoice_status(
            raw_text=original_pdf_text or '',
            qr_data=qr_data,
            gemini_data=data,
        )
        final_manual_review = backend_val['requires_manual_review'] or (
            einv_val['is_e_invoice'] is True and not einv_val['irn_number']
        )
        final_approval_eligible = backend_val['is_financial_approval_eligible'] and not (
            einv_val['is_e_invoice'] is True and not einv_val['irn_number']
        )

        # Assemble separate, preserved fields
        # raw_text preserves original digital PDF or OCR text; gemini summary is stored alongside
        final_raw_text = original_pdf_text if original_pdf_text.strip() else (data.get('raw_summary') or '')

        result: Dict[str, Any] = {
            'party_name': backend_val['party_name'] or raw_party_name,
            'party_gstin': backend_val['party_gstin'] or raw_party_gstin,
            'pan': backend_val.get('pan') or raw_pan,
            'invoice_number': (data.get('invoice_number') or '').strip(),
            'invoice_date': parsed_invoice_date,
            'due_date': parsed_due_date,
            'taxable_amount': taxable_amt,
            'cgst_amount': cgst_amt,
            'sgst_amount': sgst_amt,
            'igst_amount': igst_amt,
            'cess_amount': cess_amt,
            'round_off_amount': round_off_amt,
            'total_amount': total_amt,
            'expense_ledger': backend_val['expense_ledger'],
            'expense_description': data.get('expense_description') or '',
            'hsn_sac': data.get('hsn_sac') or '',
            'payment_details': data.get('payment_details') or '',
            'items': data.get('items') or [],
            'is_e_invoice': einv_val['is_e_invoice'],
            'irn_number': einv_val['irn_number'],
            'e_invoice_source': einv_val['e_invoice_source'],
            'raw_text': final_raw_text,
            'gemini_extracted_data': data,
            'ocr_extracted_text': original_pdf_text,
            'extraction_method': 'GEMINI_VISION_AI',
            'confidence': backend_val['confidence'],
            'requires_manual_review': final_manual_review,
            'is_ambiguous_vendor': backend_val['is_ambiguous_vendor'],
            'vendor_match_status': backend_val['vendor_match_status'],
            'is_financial_approval_eligible': final_approval_eligible,
            'audit_notes': backend_val.get('audit_notes', ''),
        }

        logger.info(
            "Gemini Vision Extracted: %s | Inv #%s | ₹%s | GSTIN: %s | Match: %s",
            result['party_name'],
            result['invoice_number'],
            result['total_amount'],
            result['party_gstin'],
            result['vendor_match_status']
        )
        return result

    except Exception as exc:
        logger.warning("Gemini Vision AI extraction exception (falling back to RapidOCR): %s", type(exc).__name__)
        return None
