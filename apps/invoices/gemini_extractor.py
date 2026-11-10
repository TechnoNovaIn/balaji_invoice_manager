"""
Gemini Vision AI Invoice Extractor for Balaji InvoiceHub.
Uses Google Gemini 1.5/2.5 Flash Vision API (Permanent Free Tier: up to 1,500 requests/day).
Extracts printed, handwritten, skewed, carbon-copy, and complex Indian transport invoices
with human-level comprehension and zero regex rules.
"""

import os
import json
import logging
from typing import Dict, Any, Optional
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class InvoiceSchema(BaseModel):
    party_name: Optional[str] = Field(
        default=None,
        description="Name of the vendor/supplier business issuing the bill. Do NOT extract 'BALAJI TRANSPORTS' as party_name because Balaji Transports is the buyer/recipient."
    )
    party_gstin: Optional[str] = Field(
        default=None,
        description="15-character statutory Indian GSTIN of the supplier if present on the bill. Exclude Balaji's GSTIN (29AAWFB0395H1ZX)."
    )
    invoice_number: Optional[str] = Field(
        default=None,
        description="Invoice number, Bill number, Cash Memo number, or pre-printed voucher number (e.g. '191', '2305', 'KA/2026/01')."
    )
    invoice_date: Optional[str] = Field(
        default=None,
        description="Invoice or bill date in YYYY-MM-DD format if visible, or null if blank/unreadable."
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
    total_amount: float = Field(
        default=0.0,
        description="Grand Total, Net Payable, or Bill Amount. Verify against line items and words if written on bill."
    )
    expense_ledger: Optional[str] = Field(
        default="Purchase Spares & Consumables",
        description="Appropriate Tally expense ledger (e.g. Purchase Spares & Consumables, Tyre & Tube Purchase, Tyre Retread, Labour Charges / Vehicle Service, GPS-Expenses, Lorry Insurance, Transportation & Freight Charges, Canteen & Staff Welfare Expenses, Building Repairs, Hardware & Paints, Printing & Stationery, Computer & Printer Expenses, Telephone & Mobile Expenses, Diesel & Lubricants, Welding Gases & Industrial Spares, Rent & Terminal Charges, General Office Expenses)."
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

Return strictly the structured JSON object.
"""


def extract_with_gemini(image_path: str) -> Optional[Dict[str, Any]]:
    """
    Extracts invoice metadata using Google Gemini Vision API.
    Returns standard dictionary matching reader.py format, or None if API key is not configured or on failure.
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

        preferred_model = getattr(settings, 'GEMINI_MODEL', 'gemini-3.5-flash-lite') or 'gemini-3.5-flash-lite'
        candidate_models = [preferred_model]
        for m in ['gemini-3.5-flash-lite', 'gemini-3.5-flash', 'gemini-3.8-flash']:
            if m not in candidate_models:
                candidate_models.append(m)

        response = None
        used_model = None
        for m_name in candidate_models:
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
                logger.info("Gemini model %s retry with next: %s", m_name, m_err)
                continue

        if not response or not response.text:
            logger.warning("Gemini Extractor: All candidate models failed or returned empty response.")
            return None

        data = json.loads(response.text)

        # Build output structure
        result: Dict[str, Any] = {
            'party_name': (data.get('party_name') or '').strip(),
            'party_gstin': (data.get('party_gstin') or '').strip().upper(),
            'invoice_number': (data.get('invoice_number') or '').strip(),
            'invoice_date': None,
            'taxable_amount': Decimal('0.00'),
            'cgst_amount': Decimal('0.00'),
            'sgst_amount': Decimal('0.00'),
            'igst_amount': Decimal('0.00'),
            'total_amount': Decimal('0.00'),
            'expense_ledger': (data.get('expense_ledger') or 'Purchase Spares & Consumables').strip(),
            'raw_text': data.get('raw_summary') or '',
            'confidence': 0.98,
            'requires_manual_review': False,
            'is_ambiguous_vendor': False,
        }

        # Date parsing
        date_str = data.get('invoice_date')
        if date_str:
            try:
                result['invoice_date'] = datetime.strptime(date_str[:10], '%Y-%m-%d').date()
            except (ValueError, TypeError):
                result['invoice_date'] = None

        # Amount parsing
        def to_decimal(val) -> Decimal:
            if val is None:
                return Decimal('0.00')
            try:
                d = Decimal(str(val)).quantize(Decimal('0.01'))
                return d if d >= 0 else Decimal('0.00')
            except (InvalidOperation, ValueError, TypeError):
                return Decimal('0.00')

        result['taxable_amount'] = to_decimal(data.get('taxable_amount'))
        result['cgst_amount'] = to_decimal(data.get('cgst_amount'))
        result['sgst_amount'] = to_decimal(data.get('sgst_amount'))
        result['igst_amount'] = to_decimal(data.get('igst_amount'))
        result['total_amount'] = to_decimal(data.get('total_amount'))

        # If total is 0 but taxable has value
        if result['total_amount'] == Decimal('0.00') and result['taxable_amount'] > 0:
            result['total_amount'] = result['taxable_amount'] + result['cgst_amount'] + result['sgst_amount'] + result['igst_amount']

        # If taxable is 0 but total has value and no tax
        if result['taxable_amount'] == Decimal('0.00') and result['total_amount'] > 0:
            result['taxable_amount'] = result['total_amount']

        # Cross-reference with Balaji Vendor Directory for official statutory consistency
        try:
            from .vendor_directory import find_vendor_by_gstin, find_vendor_by_name

            matched_vendor = None
            if result['party_gstin']:
                matched_vendor = find_vendor_by_gstin(result['party_gstin'])

            if not matched_vendor and result['party_name']:
                matched_vendor = find_vendor_by_name(result['party_name'])

            if matched_vendor:
                result['party_name'] = matched_vendor['name']
                if not result['party_gstin']:
                    result['party_gstin'] = matched_vendor.get('gstin', '')
                if matched_vendor.get('ledger'):
                    result['expense_ledger'] = matched_vendor['ledger']
        except Exception as v_err:
            logger.debug("Vendor directory lookup in Gemini extractor: %s", v_err)

        logger.info(
            "Gemini Vision Extracted: %s | Inv #%s | ₹%s | GSTIN: %s",
            result['party_name'], result['invoice_number'], result['total_amount'], result['party_gstin']
        )
        return result

    except Exception as exc:
        logger.warning("Gemini Vision AI extraction failed (falling back to RapidOCR): %s", exc)
        return None

