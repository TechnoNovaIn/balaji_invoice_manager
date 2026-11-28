"""
Gemini Vision AI Invoice Extractor for Balaji InvoiceHub.
Extracts printed, handwritten, skewed, carbon-copy, and complex Indian transport invoices
using Google Gemini Vision API (Pydantic schema validated).
Enforces backend-controlled financial validation gates (Rules A through E).
"""

import os
import re
import json
import time
import logging
import threading
from typing import Dict, Any, Optional, List
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from pydantic import BaseModel, Field

from .vendor_validator import validate_vendor_and_financials, is_valid_gstin, clean_gstin
from .e_invoice_utils import evaluate_e_invoice_status, extract_qr_e_invoice_data

# Clean up any invalid GOOGLE_GENAI_HTTP_CLIENT configuration
_client_backend = os.environ.get('GOOGLE_GENAI_HTTP_CLIENT', '').strip()
if _client_backend:
    try:
        __import__(_client_backend)
    except ImportError:
        os.environ.pop('GOOGLE_GENAI_HTTP_CLIENT', None)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Gemini client + retry helpers
# ---------------------------------------------------------------------------
_CLIENT_CACHE: Dict[Any, Any] = {}
_CLIENT_LOCK = threading.Lock()

# HTTP status codes worth retrying on the same model.
_RETRYABLE_CODES = {429, 500, 502, 503, 504}
# Codes meaning "this model is unavailable for this key" -> try the next configured model.
_MODEL_UNAVAILABLE_CODES = {404}
# Codes indicating key quota exhaustion or free-tier project disabled -> fail over to next key
_KEY_EXHAUSTED_CODES = {429, 403}


def _get_gemini_client(api_key: str):
    """Return a cached google-genai client (one per key) with an HTTP timeout.

    Reusing the client avoids a new connection/TLS handshake per invoice; the timeout stops a
    single hung request from blocking an OCR worker for minutes.
    """
    from google import genai
    from google.genai import types

    cache_key = (api_key, id(genai.Client))  # id() keeps test patches of genai.Client isolated
    with _CLIENT_LOCK:
        client = _CLIENT_CACHE.get(cache_key)
        if client is None:
            timeout_ms = int(getattr(settings, 'GEMINI_TIMEOUT_MS', 60000) or 60000)
            client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=timeout_ms))
            _CLIENT_CACHE[cache_key] = client
        return client


def _gemini_error_code(err: Exception) -> Optional[int]:
    """Extract the HTTP status code from a google-genai APIError (None for transport/network errors)."""
    code = getattr(err, 'code', None)
    if isinstance(code, int):
        return code
    status_code = getattr(err, 'status_code', None)
    if isinstance(status_code, int):
        return status_code
    response = getattr(err, 'response', None)
    resp_status = getattr(response, 'status_code', None)
    return resp_status if isinstance(resp_status, int) else None


def _is_retryable_transport_error(err: Exception) -> bool:
    """Return True for httpx/httpx2/stdlib network timeouts and transient transport errors."""
    if isinstance(err, (TimeoutError, ConnectionError, OSError)):
        return True
    try:
        import httpx
        if isinstance(err, (httpx.TimeoutException, httpx.TransportError)):
            return True
    except ImportError:
        pass
    try:
        from google.genai import errors as genai_errors
        httpx2_mod = getattr(genai_errors, 'httpx2', None)
        if httpx2_mod is not None:
            transport_errs = tuple(
                cls for cls in (
                    getattr(httpx2_mod, 'TimeoutException', None),
                    getattr(httpx2_mod, 'TransportError', None),
                )
                if isinstance(cls, type)
            )
            if transport_errs and isinstance(err, transport_errs):
                return True
    except Exception:
        pass
    return False


def _gemini_retry_delay(err: Exception, attempt: int) -> float:
    """Seconds to wait before retrying: the server's RetryInfo hint if present, else exponential backoff."""
    max_delay = float(getattr(settings, 'GEMINI_MAX_RETRY_DELAY_SECONDS', 60) or 60)
    details = getattr(err, 'details', None)
    try:
        error_block = details.get('error', details) if isinstance(details, dict) else {}
        for item in error_block.get('details', []) or []:
            if isinstance(item, dict) and 'retryDelay' in item:
                hinted = float(str(item['retryDelay']).rstrip('s') or 0)
                if hinted > 0:
                    return min(hinted, max_delay)
    except (AttributeError, TypeError, ValueError):
        pass
    return min(2.0 * (2 ** attempt), max_delay)


def _configured_gemini_models() -> List[str]:
    """Primary model plus optional explicit fallbacks (GEMINI_FALLBACK_MODELS, comma-separated)."""
    primary = (getattr(settings, 'GEMINI_MODEL', '') or 'gemini-3.5-flash').strip()
    models = [primary]
    for name in str(getattr(settings, 'GEMINI_FALLBACK_MODELS', '') or '').split(','):
        name = name.strip()
        if name and name not in models:
            models.append(name)
    return models


class InvoiceItemSchema(BaseModel):
    description: Optional[str] = Field(default=None, description="Description of goods or service line item")
    hsn_sac: Optional[str] = Field(default=None, description="HSN or SAC code for line item")
    quantity: Optional[float] = Field(default=None, description="Quantity supplied")
    unit_price: Optional[float] = Field(default=None, description="Rate or unit price")
    amount: Optional[float] = Field(default=None, description="Line item taxable or gross amount")


class InvoiceSchema(BaseModel):
    invoice_type: Optional[str] = Field(
        default="REGULAR",
        description="Document category: 'VEHICLE_INSURANCE' for insurance policy certificates / schedules / premium receipts; 'VEHICLE_SERVICE' for vehicle garage / workshop / repair / maintenance bills; 'REGULAR' for general supplier invoices, diesel, parts, consumables."
    )
    policy_number: Optional[str] = Field(
        default=None,
        description="Insurance Policy Number, Certificate Number, or Cover Note Number if this is a Vehicle Insurance document (e.g. 'OG-25-1801-1802-00001234', '123456/31/2026/123')."
    )
    vehicle_number: Optional[str] = Field(
        default=None,
        description="Commercial Vehicle / Lorry / Truck Registration Number (e.g. 'KA-01-AB-1234', 'KA04D1234', 'NL01AD1234', 'MH12CD5678'). Look for labels like 'Veh No', 'Vehicle No', 'Reg No', 'Regn No', 'Lorry No', 'Truck No'."
    )
    labour_charges: Optional[float] = Field(
        default=0.0,
        description="Total labour charges, service charges, fitting charges, or repair labour amount if itemized on a vehicle service / garage bill."
    )
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
    amount_in_words: Optional[str] = Field(
        default=None,
        description="Exact transcription of 'Rupees in words' or 'Amount Chargeable (in words)' written on the bill, or null if absent."
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
        description="True if GST e-invoice (IRN, Ack No, Signed QR, or explicit e-Invoice header) is present; False ONLY if explicitly declared not an e-invoice (e.g. 'Not an e-invoice', 'E-Invoice Applicable: No'); null if uncertain or if IRN is merely missing or marked N/A."
    )
    irn_number: Optional[str] = Field(
        default=None,
        description="64-character hexadecimal SHA-256 Invoice Reference Number (IRN) using only 0-9 and A-F if clearly printed; null if missing or illegible. Never invent or guess."
    )
    document_type: Optional[str] = Field(
        default="NEEDS_REVIEW",
        description="Visual appearance classification: 'HANDWRITTEN', 'PRINTED', 'MIXED', or 'NEEDS_REVIEW'."
    )
    has_handwritten_amounts: Optional[bool] = Field(
        default=None,
        description="True if amounts, totals, or line item descriptions are handwritten."
    )
    raw_summary: Optional[str] = Field(
        default="",
        description="Brief transcription of key bill lines (vendor, bill no, date, line items, words)."
    )
    crop_box: Optional[List[int]] = Field(
        default=None,
        description="The 4 bounding coordinates [ymin, xmin, ymax, xmax] of the main physical paper receipt/invoice normalized between 0 and 1000. Identify the physical boundaries of the paper document, strictly excluding surrounding background, table surfaces, hands, or feet. If the invoice already fills the entire image (>92%), return null or [0, 0, 1000, 1000]."
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
   - First transcribe any 'Rupees in words' / 'Amount Chargeable (in words)' into `amount_in_words`.
   - Beware handwritten trailing '/-' (e.g. '26000/-', '364000/-', '32760/-'): count EVERY zero before '/-' carefully! Never drop a zero (26000/- is 26,000, NOT 2,600; 364000/- is 3,64,000, NOT 36,400).
   - Cross-check every handwritten digit against `quantity x unit_price`, tax percentages (e.g. 9% + 9%), AND `amount_in_words` (e.g. 'four lakh twenty nine thousand...' MUST be in the 4,00,000+ range, NEVER 42,952).
   - If line items + taxes sum to a total, use the mathematically and word-verified values for all fields.

4. DATES:
   - Convert any standard Indian date format (DD/MM/YYYY, DD-MM-YY, etc.) to ISO format YYYY-MM-DD.
   - If the date field on the paper is completely blank or missing, return null.

5. MISSING FIELDS:
   - Missing fields must remain null or explicitly unidentified. Never invent missing invoice information.

6. E-INVOICE & IRN:
   - Extract `irn_number` ONLY if a complete 64-character hexadecimal SHA-256 IRN (digits 0-9 and letters A-F only) is explicitly readable (if wrapped across two lines with a hyphen `-`, remove the hyphen/newline and join into one 64-char hex string). Never fabricate, guess, or pad an IRN.
   - Set `is_e_invoice` to true if IRN/Ack No/Signed QR/e-Invoice indicators appear, false ONLY if explicitly declared non-e-invoice (e.g. 'Not an e-invoice', 'E-Invoice Applicable: No'), or null if uncertain or if IRN is simply missing or marked N/A.

7. DOCUMENT APPEARANCE CLASSIFICATION (INDEPENDENT FROM E-INVOICE STATUS):
   - Set `document_type` to:
     * `HANDWRITTEN`: handwritten voucher/bill/receipt OR a pre-printed bill-book form filled out manually by hand with handwritten buyer, items, and amounts.
     * `PRINTED`: computer-generated or machine-printed invoice (even if it has minor pen ticks/signatures/notes).
     * `MIXED`: printed invoice with significant handwritten financial edits or line additions.
     * `NEEDS_REVIEW`: blurry, unclear, or uncertain visual classification.
   - Set `has_handwritten_amounts` to true ONLY if primary billed items, rates, taxes, or totals are handwritten (not just a pen signature or post-print payment remark).
   - Never classify a handwritten bill as `is_e_invoice: false` unless explicit non-e-invoice text says so.

8. PHYSICAL SPARE PARTS & TOOLS IN THE PHOTO:
   - Field employees often photograph bills alongside physical hardware/spare parts (e.g. hydraulic hoses, metallic fittings, bolts, bearings, mechanical components, filters, oil cans, or tools) resting on a table, counter, or workshop floor.
   - Completely IGNORE all physical objects, spare parts, tools, hoses, hands, background furniture, and counter surfaces.
   - Locate and focus exclusively on the paper invoice / receipt document in the photograph to extract the supplier, date, bill number, amounts, and taxes.

9. DOCUMENT BOUNDARY & CROP BOX:
   - Identify the 4-sided bounding box [ymin, xmin, ymax, xmax] of the main paper receipt or invoice, normalized between 0 and 1000.
   - Include the entire paper document from top header to bottom signature, strictly excluding surrounding background, table, desk, counter, floor, tools, hands, or feet.
   - If the receipt already covers 92%+ of the image frame, set crop_box to null or [0, 0, 1000, 1000].

10. VEHICLE INSURANCE POLICIES & INVOICES:
    - If this document is a Motor / Vehicle Insurance Policy Schedule, Certificate of Insurance, or Premium Receipt (from insurers such as New India Assurance, United India, ICICI Lombard, Tata AIG, Go Digit, HDFC ERGO, Oriental, National, Cholamandalam MS, Shriram General, etc.):
      * Set `invoice_type` to "VEHICLE_INSURANCE".
      * Set `expense_ledger` to "Lorry Insurance".
      * Extract `policy_number` from fields labeled 'Policy No.', 'Policy Number', 'Certificate No.', 'Cover Note No.'.
      * If a separate Tax Invoice / Receipt No is printed, extract that into `invoice_number`; if no separate invoice number is printed, duplicate the Policy Number into `invoice_number`.
      * Extract `vehicle_number` from 'Vehicle No.', 'Registration No.', 'Registration Mark', 'Registration Mark & Place', 'Regn. No.', 'Lorry No.' (join wrapped parts like 'KA - 19 - AB - 2209' into 'KA-19-AB-2209').
      * CRITICAL FOR INSURANCE: Do NOT extract IDV (Insured Declared Value, e.g. ₹15,00,000) or Sum Insured as the bill amount!
        - `total_amount` is the Total Premium / Gross Premium Payable (typically ₹5,000 to ₹90,000).
        - `taxable_amount` is the Net Premium before GST.

11. VEHICLE SERVICE & GARAGE INVOICES:
    - If this document is a vehicle service bill, workshop job card/invoice, garage cash memo, or mechanic repair voucher (e.g. Tata Motors, Ashok Leyland, BharatBenz, TVS, tyre retreading, local garage/mechanic, body building, lathe work):
      * Set `invoice_type` to "VEHICLE_SERVICE".
      * Set `expense_ledger` to "Labour Charges / Vehicle Service" (or "Vehicle Repairs & Maintenance").
      * Extract `vehicle_number` from labels like 'REGISTRATION', 'REGISTRATION NO', 'Vehicle No', 'Veh Reg No', 'Reg. No.', 'Lorry No', 'Truck No'.
      * Extract `labour_charges`: sum ONLY individual line items explicitly labeled as Labour, Service Charge, Fitting, Lathe Work, Washing, Greasing, or Alignment. NEVER copy physical items (e.g. glass, beeding, silicon, spares) or the full bill total into `labour_charges` just because the shop header says "Sales & Fitting". If no line item is explicitly a labour/service charge, set `labour_charges` to 0.

12. TAX AMOUNTS & GST ANTI-MIXUP RULES:
    - `taxable_amount`: Value of supply BEFORE tax (Subtotal / Basic / Taxable Value).
    - `cgst_amount` & `sgst_amount`: Central and State GST. For intra-state supply within Karnataka (Supplier GSTIN starts with 29), CGST and SGST MUST be equal in amount and rate (e.g. 9% + 9% or 14% + 14%). IGST must be 0.
    - `igst_amount`: Integrated GST. Applies ONLY when supplier is OUTSIDE Karnataka (Supplier GSTIN starts with a code other than 29, e.g. 27, 33, 06). CGST and SGST must be 0.
    - `total_amount`: Grand Total / Net Payable (Taxable + CGST + SGST + IGST + Cess + RoundOff).
    - ANTI-MIXUP RULES:
      * NEVER swap `taxable_amount` with `total_amount`. Total Amount is always greater than or equal to Taxable Amount.
      * NEVER extract Round off amounts (+/- ₹0.06, ₹0.45) as `total_amount` or `taxable_amount`. Total Amount is the final Grand Total (e.g. ₹1,767.00).
      * NEVER extract the Tax % rate (e.g. 9.00 or 18.00) as `taxable_amount`. Taxable amount is the full subtotal before tax (e.g. ₹1,497.50).
      * NEVER extract total GST or individual tax line into `taxable_amount`.
      * For Invoices with Advance / Part Payment / Balance Due (e.g. Intangles, subscriptions, hardware): NEVER extract 'Balance Due' or 'Payment Made / Advance' as `total_amount`. `total_amount` is the full Gross Total / Invoice Value before deducting advance payments (e.g. ₹4,37,190.00). `taxable_amount` is the Sub Total before taxes (e.g. ₹3,70,500.00).
      * Accurately convert dates with text months (e.g. '17 Jun 2025' or '17 June 2025') into standard 'YYYY-MM-DD' ('2025-06-17').
      * If tax is not itemized or bill is from an unregistered supplier / bill of supply, set `cgst_amount = 0`, `sgst_amount = 0`, `igst_amount = 0`, and `taxable_amount = total_amount`.

Return strictly the structured JSON object.
"""


_WORD_NUMBERS = {
    'zero': 0, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
    'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
    'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14, 'fifteen': 15,
    'sixteen': 16, 'seventeen': 17, 'eighteen': 18, 'nineteen': 19,
    'twenty': 20, 'tenty': 20, 'thirty': 30, 'forty': 40, 'fourty': 40,
    'fifty': 50, 'sixty': 60, 'seventy': 70, 'eighty': 80, 'ninety': 90,
}
_WORD_MULTIPLIERS = {
    'hundred': 100, 'thousand': 1000, 'lakh': 100000, 'lakhs': 100000,
    'lac': 100000, 'lacs': 100000, 'crore': 10000000, 'crores': 10000000,
}


def _parse_indian_amount_words(words_text: Optional[str]) -> Optional[Decimal]:
    """Conservatively parse Indian Rupee amount words into Decimal; returns None if ambiguous."""
    if not words_text:
        return None
    cleaned = re.sub(r'[^a-z\s]', ' ', words_text.lower())
    # Ignore paise/decimal portion after 'paise' or 'point'
    cleaned = re.split(r'\b(?:paise|paisa|point)\b', cleaned)[0]
    tokens = [t for t in cleaned.split() if t not in {'rupees', 'rupee', 'rs', 'inr', 'only', 'and', 'exact'}]
    if not tokens:
        return None

    total = 0
    current = 0
    matched_any = False
    for tok in tokens:
        if tok in _WORD_NUMBERS:
            current += _WORD_NUMBERS[tok]
            matched_any = True
        elif tok == 'hundred':
            if current == 0:
                current = 1
            current *= 100
            matched_any = True
        elif tok in _WORD_MULTIPLIERS:
            if current == 0:
                current = 1
            total += current * _WORD_MULTIPLIERS[tok]
            current = 0
            matched_any = True
    if not matched_any:
        return None
    total += current
    return Decimal(total).quantize(Decimal('0.01')) if total > 0 else None


def _reconcile_extracted_amounts(
    data: Dict[str, Any],
    party_gstin: Optional[str] = None
) -> tuple[Dict[str, Decimal], list[str]]:
    """
    Rigorously reconcile extracted amounts against Rupees-in-words, line items, and GST arithmetic.
    Prevents tax mix-ups, inversions, and state-mismatches between CGST/SGST and IGST.
    """
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
    round_off_raw = data.get('round_off_amount')
    try:
        round_off_amt = Decimal(str(round_off_raw or '0')).quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError, TypeError):
        round_off_amt = Decimal('0.00')
    total_amt = to_decimal(data.get('total_amount'))
    warnings: list[str] = []

    # 1. State-aware GST Alignment (Intra-State vs Inter-State)
    clean_gst = clean_gstin(party_gstin or data.get('party_gstin'))
    if clean_gst and len(clean_gst) == 15:
        if clean_gst.startswith('29'):
            # Karnataka Intra-state supply: IGST is legally 0; tax is CGST + SGST
            if igst_amt > Decimal('0.00') and cgst_amt == Decimal('0.00') and sgst_amt == Decimal('0.00'):
                half = (igst_amt / Decimal('2')).quantize(Decimal('0.01'))
                cgst_amt = half
                sgst_amt = (igst_amt - half).quantize(Decimal('0.01'))
                igst_amt = Decimal('0.00')
                warnings.append(f"Auto-aligned Karnataka intra-state tax to CGST ₹{cgst_amt:,.2f} + SGST ₹{sgst_amt:,.2f}.")
        else:
            # Inter-state supply: CGST/SGST are 0; tax is IGST
            if (cgst_amt > Decimal('0.00') or sgst_amt > Decimal('0.00')) and igst_amt == Decimal('0.00'):
                igst_amt = (cgst_amt + sgst_amt).quantize(Decimal('0.01'))
                cgst_amt = Decimal('0.00')
                sgst_amt = Decimal('0.00')
                warnings.append(f"Auto-aligned inter-state tax to IGST ₹{igst_amt:,.2f}.")

    tax_sum = cgst_amt + sgst_amt + igst_amt + cess_amt

    # 1b. Guard against Round-Off mistakenly extracted as Grand Total
    if Decimal('0.00') < total_amt < Decimal('1.00'):
        if taxable_amt >= Decimal('1.00'):
            round_off_amt = total_amt
            total_amt = (taxable_amt + tax_sum + round_off_amt).quantize(Decimal('0.01'))
            warnings.append(f"Auto-recovered Grand Total (₹{total_amt:,.2f}) after ₹{round_off_amt} was identified as Round Off.")

    # 2. Anti-Inversion Check: Grand Total cannot be strictly less than Taxable Base
    if taxable_amt > total_amt and total_amt > Decimal('0.00'):
        taxable_amt, total_amt = total_amt, taxable_amt
        warnings.append(f"Auto-corrected inverted amounts: Taxable Base set to ₹{taxable_amt:,.2f}, Total set to ₹{total_amt:,.2f}.")

    # 3. Missing Taxable with Positive Taxes: derive Taxable = Total - Taxes - RoundOff
    if taxable_amt == Decimal('0.00') and total_amt > Decimal('0.00'):
        if tax_sum > Decimal('0.00'):
            derived_taxable = max(Decimal('0.00'), total_amt - tax_sum - round_off_amt)
            taxable_amt = derived_taxable
        else:
            taxable_amt = total_amt

    # 4. Missing Total with Positive Taxable: derive Total = Taxable + Taxes + RoundOff
    if total_amt == Decimal('0.00') and taxable_amt > Decimal('0.00'):
        total_amt = (taxable_amt + tax_sum + round_off_amt).quantize(Decimal('0.01'))

    # 5. Fix Tax Amount accidentally assigned to Taxable Base
    if tax_sum > Decimal('0.00') and total_amt > tax_sum:
        calc_taxable = (total_amt - tax_sum - round_off_amt).quantize(Decimal('0.01'))
        if abs((taxable_amt + tax_sum + round_off_amt) - total_amt) > Decimal('2.00') and calc_taxable > Decimal('0.00'):
            # If taxable_amt was extracted as total_amt or as tax_sum, correct it to calc_taxable
            if abs(taxable_amt - total_amt) <= Decimal('1.00') or abs(taxable_amt - tax_sum) <= Decimal('1.00'):
                taxable_amt = calc_taxable
                warnings.append(f"Auto-reconciled Taxable Base to ₹{taxable_amt:,.2f} based on Total (₹{total_amt:,.2f}) minus Taxes (₹{tax_sum:,.2f}).")

    # 6. Word-based magnitude recovery
    words_total = _parse_indian_amount_words(data.get('amount_in_words'))
    if words_total and total_amt > 0:
        tol_scale = max(Decimal('5.00'), words_total * Decimal('0.015'))
        scaled = False
        if abs(total_amt - words_total) > tol_scale:
            for factor in (Decimal('10'), Decimal('100')):
                if abs((total_amt * factor) - words_total) <= tol_scale:
                    taxable_amt = (taxable_amt * factor).quantize(Decimal('0.01'))
                    cgst_amt = (cgst_amt * factor).quantize(Decimal('0.01'))
                    sgst_amt = (sgst_amt * factor).quantize(Decimal('0.01'))
                    igst_amt = (igst_amt * factor).quantize(Decimal('0.01'))
                    cess_amt = (cess_amt * factor).quantize(Decimal('0.01'))
                    round_off_amt = (round_off_amt * factor).quantize(Decimal('0.01'))
                    total_amt = (total_amt * factor).quantize(Decimal('0.01'))
                    warnings.append(
                        f"Recovered {int(factor)}x magnitude from Rupees-in-words (₹{words_total:,.2f}); confirm digits manually."
                    )
                    scaled = True
                    break
        if not scaled and abs(total_amt - words_total) > max(Decimal('5.00'), words_total * Decimal('0.01')):
            warnings.append(
                f"Rupees-in-words (₹{words_total:,.2f}) differs from Grand Total (₹{total_amt:,.2f})."
            )

    # 7. Check arithmetic sum including cess & round-off (with 10x missed-zero recovery on single component)
    tax_sum = cgst_amt + sgst_amt + igst_amt + cess_amt
    if tax_sum > Decimal('0.00') and taxable_amt > Decimal('0.00') and total_amt > Decimal('0.00'):
        expected_total = taxable_amt + tax_sum + round_off_amt
        if abs(expected_total - total_amt) > Decimal('2.00'):
            # Check if a single component dropped a trailing zero (e.g. IGST 6669 -> 66690)
            recovered_component = False
            if igst_amt > Decimal('0.00') and cgst_amt == Decimal('0.00') and sgst_amt == Decimal('0.00'):
                igst_10x = (igst_amt * Decimal('10')).quantize(Decimal('0.01'))
                if abs((taxable_amt + igst_10x + cess_amt + round_off_amt) - total_amt) <= Decimal('2.00'):
                    warnings.append(
                        f"Recovered missed zero on IGST (₹{igst_amt:,.2f} -> ₹{igst_10x:,.2f}) to reconcile Total ₹{total_amt:,.2f}."
                    )
                    igst_amt = igst_10x
                    recovered_component = True
            elif cgst_amt > Decimal('0.00') and sgst_amt > Decimal('0.00'):
                cgst_10x = (cgst_amt * Decimal('10')).quantize(Decimal('0.01'))
                sgst_10x = (sgst_amt * Decimal('10')).quantize(Decimal('0.01'))
                if abs((taxable_amt + cgst_10x + sgst_10x + cess_amt + round_off_amt) - total_amt) <= Decimal('2.00'):
                    warnings.append(
                        f"Recovered missed zero on CGST/SGST (₹{cgst_amt:,.2f} -> ₹{cgst_10x:,.2f}) to reconcile Total ₹{total_amt:,.2f}."
                    )
                    cgst_amt = cgst_10x
                    sgst_amt = sgst_10x
                    recovered_component = True
            if not recovered_component:
                taxable_10x = (taxable_amt * Decimal('10')).quantize(Decimal('0.01'))
                if abs((taxable_10x + tax_sum + round_off_amt) - total_amt) <= Decimal('2.00'):
                    warnings.append(
                        f"Recovered missed zero on Taxable Base (₹{taxable_amt:,.2f} -> ₹{taxable_10x:,.2f}) to reconcile Total ₹{total_amt:,.2f}."
                    )
                    taxable_amt = taxable_10x
                    recovered_component = True

            tax_sum = cgst_amt + sgst_amt + igst_amt + cess_amt
            expected_total = taxable_amt + tax_sum + round_off_amt
            if not recovered_component and abs(expected_total - total_amt) > Decimal('2.00'):
                warnings.append(
                    f"Taxable (₹{taxable_amt:,.2f}) + Taxes (₹{tax_sum:,.2f}) = ₹{expected_total:,.2f} != Total (₹{total_amt:,.2f})."
                )

    # 8. Check CGST / SGST symmetry
    if (cgst_amt > Decimal('0.00')) != (sgst_amt > Decimal('0.00')):
        warnings.append(f"Incomplete intra-state GST split: CGST ₹{cgst_amt:,.2f}, SGST ₹{sgst_amt:,.2f}.")
    elif cgst_amt > Decimal('0.00') and sgst_amt > Decimal('0.00') and abs(cgst_amt - sgst_amt) > Decimal('1.00'):
        warnings.append(f"Asymmetrical GST split: CGST ₹{cgst_amt:,.2f} vs SGST ₹{sgst_amt:,.2f}.")

    # 9. Cross-check extracted line items
    items = data.get('items') or []
    items_sum = Decimal('0.00')
    has_item_amounts = False
    for it in items:
        qty = to_decimal(it.get('quantity'))
        rate = to_decimal(it.get('unit_price'))
        amt = to_decimal(it.get('amount'))
        if amt > Decimal('0.00'):
            items_sum += amt
            has_item_amounts = True
            if qty > Decimal('0.00') and rate > Decimal('0.00'):
                line_calc = (qty * rate).quantize(Decimal('0.01'))
                if amt > line_calc + Decimal('2.00') or (abs(line_calc - amt) > max(Decimal('5.00'), amt * Decimal('0.01'))):
                    warnings.append(f"Line item qty×rate (₹{line_calc:,.2f}) != item amount (₹{amt:,.2f}).")
                    break
    if has_item_amounts and taxable_amt > Decimal('0.00'):
        taxable_diff = abs(items_sum - taxable_amt)
        total_diff = abs(items_sum - total_amt)
        tol_items = max(Decimal('5.00'), taxable_amt * Decimal('0.01'))
        if taxable_diff > tol_items and total_diff > tol_items:
            warnings.append(
                f"Line items sum (₹{items_sum:,.2f}) does not match Taxable (₹{taxable_amt:,.2f}) or Total (₹{total_amt:,.2f})."
            )

    return {
        'taxable_amount': taxable_amt,
        'cgst_amount': cgst_amt,
        'sgst_amount': sgst_amt,
        'igst_amount': igst_amt,
        'cess_amount': cess_amt,
        'round_off_amount': round_off_amt,
        'total_amount': total_amt,
    }, warnings


def extract_with_gemini(
    image_path: str,
    original_pdf_text: str = '',
    timer=None,
) -> Optional[Dict[str, Any]]:
    """
    Extracts invoice metadata using Google Gemini Vision API with Pydantic validation.
    Applies independent backend vendor validation (Rules A through E).
    Preserves original digital/OCR text separately from Gemini's interpretation.
    Returns standard dictionary or None on failure / unconfigured API.
    Supports a pool of 5 to 6 API keys with automatic, seamless failover on exhaustion.
    """
    from apps.invoices.gemini_service import (
        get_available_gemini_keys,
        mark_key_exhausted,
        mark_key_healthy,
    )

    candidate_keys = get_available_gemini_keys()
    if not candidate_keys:
        single_fallback = (getattr(settings, 'GEMINI_API_KEY', '') or os.getenv('GEMINI_API_KEY', '')).strip()
        if single_fallback:
            candidate_keys = [single_fallback]
        else:
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

        prompt_contents = [file_part]
        if original_pdf_text and original_pdf_text.strip():
            prompt_contents.append(
                f"EMBEDDED DIGITAL PDF TEXT (authoritative for machine-printed numbers, GSTINs, and IRN):\n"
                f"{original_pdf_text.strip()[:12000]}\n\n{PROMPT_INSTRUCTIONS}"
            )
        else:
            prompt_contents.append(PROMPT_INSTRUCTIONS)

        candidate_models = _configured_gemini_models()
        max_attempts = max(1, int(getattr(settings, 'GEMINI_MAX_ATTEMPTS', 3) or 3))

        def _parse_response(resp) -> Optional[Dict[str, Any]]:
            parsed = getattr(resp, 'parsed', None)
            if isinstance(parsed, InvoiceSchema):
                return parsed.model_dump()
            text = getattr(resp, 'text', None)
            if not text:
                return None
            return InvoiceSchema(**json.loads(text)).model_dump()

        data = None
        used_model = None
        used_key_suffix = None
        last_error_type = None
        gemini_start = time.perf_counter()

        for key_idx, current_key in enumerate(candidate_keys):
            has_other_keys = (key_idx < len(candidate_keys) - 1)
            key_mask_suffix = current_key[-4:] if len(current_key) >= 4 else current_key
            client = _get_gemini_client(current_key)
            key_failed_exhausted = False

            def _invoke_gemini_with_client(model_name: str):
                return client.models.generate_content(
                    model=model_name,
                    contents=prompt_contents,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=InvoiceSchema,
                        temperature=0.0,
                    ),
                )

            for m_name in candidate_models:
                for attempt in range(max_attempts):
                    is_last_attempt = attempt == max_attempts - 1
                    try:
                        if timer is not None:
                            timer.increment('gemini_requests_count')
                            timer.start_timing('gemini_api_ms')
                        try:
                            response = _invoke_gemini_with_client(m_name)
                        finally:
                            if timer is not None:
                                timer.stop_timing('gemini_api_ms')
                    except Exception as m_err:
                        last_error_type = type(m_err).__name__
                        code = _gemini_error_code(m_err)

                        # If rate-limited or quota exhausted (429 / 403)
                        if code in _KEY_EXHAUSTED_CODES:
                            mark_key_exhausted(current_key, cooldown_seconds=3600)
                            if has_other_keys:
                                logger.warning(
                                    "Gemini key ...%s exhausted (HTTP %s, %s). Seamlessly failing over to next key in pool (%d remaining).",
                                    key_mask_suffix, code, last_error_type, len(candidate_keys) - key_idx - 1
                                )
                                key_failed_exhausted = True
                                break
                            elif not is_last_attempt:
                                delay = _gemini_retry_delay(m_err, attempt)
                                logger.warning("Gemini key ...%s rate-limited (HTTP %s); retrying in %.1fs.", key_mask_suffix, code, delay)
                                time.sleep(delay)
                                continue
                            else:
                                logger.warning("Gemini key ...%s exhausted (HTTP %s) and no backup keys remain.", key_mask_suffix, code)
                                key_failed_exhausted = True
                                break

                        if code in _MODEL_UNAVAILABLE_CODES:
                            logger.warning("Gemini model %s unavailable (HTTP %s); trying next configured model.", m_name, code)
                            break
                        if (code in _RETRYABLE_CODES or (code is None and _is_retryable_transport_error(m_err))) and not is_last_attempt:
                            delay = _gemini_retry_delay(m_err, attempt)
                            logger.warning("Gemini model %s error (HTTP %s, %s); retrying in %.1fs.",
                                           m_name, code, last_error_type, delay)
                            time.sleep(delay)
                            continue
                        logger.warning("Gemini model %s failed on key ...%s (HTTP %s, %s).", m_name, key_mask_suffix, code, last_error_type)
                        break

                    # Response received: validate structure; retry once on malformed / truncated JSON.
                    try:
                        data = _parse_response(response)
                    except Exception as parse_err:
                        last_error_type = type(parse_err).__name__
                        data = None

                    if data is not None:
                        mark_key_healthy(current_key)
                        used_model = m_name
                        used_key_suffix = key_mask_suffix
                        logger.info("Gemini extraction successful using key ...%s and model %s", key_mask_suffix, m_name)
                        break
                    logger.warning("Gemini model %s returned empty or malformed JSON (%s).", m_name, last_error_type)
                    if is_last_attempt:
                        break

                if key_failed_exhausted:
                    # Switch immediately to the next key in the pool
                    break
                if data is not None:
                    break

            if data is not None:
                break

        if data is None:
            logger.warning("Gemini Extractor: no usable response from configured keys and models (last_error=%s).", last_error_type)
            return None

        raw_party_name = (data.get('party_name') or '').strip()
        raw_party_gstin = clean_gstin(data.get('party_gstin'))
        raw_pan = (data.get('pan') or '').strip().upper()
        raw_ledger = (data.get('expense_ledger') or 'Purchase Spares & Consumables').strip()

        amounts, fin_warnings = _reconcile_extracted_amounts(data, party_gstin=raw_party_gstin)

        # Date parsing with flexible fallback
        def _parse_flexible_date(val):
            if not val:
                return None
            s = str(val).strip()
            try:
                return datetime.strptime(s[:10], '%Y-%m-%d').date()
            except (ValueError, TypeError):
                pass
            for fmt in ('%d-%m-%Y', '%d/%m/%Y', '%d %b %Y', '%d-%b-%Y', '%d %B %Y', '%d-%B-%Y', '%b %d, %Y', '%b %d %Y'):
                try:
                    return datetime.strptime(s, fmt).date()
                except (ValueError, TypeError):
                    continue
            return None

        parsed_invoice_date = _parse_flexible_date(data.get('invoice_date'))
        parsed_due_date = _parse_flexible_date(data.get('due_date'))

        taxable_amt = amounts['taxable_amount']
        cgst_amt = amounts['cgst_amount']
        sgst_amt = amounts['sgst_amount']
        igst_amt = amounts['igst_amount']
        cess_amt = amounts['cess_amount']
        round_off_amt = amounts['round_off_amount']
        total_amt = amounts['total_amount']

        # --- CRITICAL BACKEND VENDOR & FINANCIAL VALIDATION (Rules A - E) ---
        # Gemini is advisory only; the backend independently calculates ambiguity,
        # manual review gating, and financial approval eligibility.
        backend_val = validate_vendor_and_financials(
            party_name=raw_party_name,
            party_gstin=raw_party_gstin,
            default_expense_ledger=raw_ledger,
            pan=raw_pan,
        )

        def to_dec(val) -> Decimal:
            if val is None:
                return Decimal('0.00')
            try:
                d = Decimal(str(val)).quantize(Decimal('0.01'))
                return d if d >= 0 else Decimal('0.00')
            except (InvalidOperation, ValueError, TypeError):
                return Decimal('0.00')

        inv_type = (data.get('invoice_type') or 'REGULAR').strip().upper()
        pol_num = (data.get('policy_number') or '').strip()
        veh_num = (data.get('vehicle_number') or '').strip().upper()
        if not veh_num and original_pdf_text:
            try:
                from .reader import extract_metadata_from_text
                veh_num = (extract_metadata_from_text(original_pdf_text).get('vehicle_number') or '').strip().upper()
            except Exception:
                pass
        labour_amt = to_dec(data.get('labour_charges'))

        # Guard: if line items are extracted and NONE are labour/service charges, do not misclassify item costs as labour
        extracted_items = data.get('items') or []
        if labour_amt > Decimal('0.00') and extracted_items:
            _labour_kws = (
                'labour', 'labor', 'service', 'fitting', 'lathe', 'wash',
                'greas', 'align', 'repair', 'dent', 'paint', 'weld', 'mechanic',
                'overhaul', 'tinkering', 'wiring', 'scanning', 'erasing',
            )
            has_any_labour_item = any(
                any(kw in str(it.get('description') or '').lower() for kw in _labour_kws)
                for it in extracted_items if isinstance(it, dict)
            )
            if not has_any_labour_item:
                labour_amt = Decimal('0.00')

        # Document Category determination:
        # Strictly default to REGULAR unless specifically proven to be Vehicle Insurance or Vehicle Service
        if pol_num or (inv_type == 'VEHICLE_INSURANCE') or ('lorry insurance' in raw_ledger.lower() or 'vehicle insurance' in raw_ledger.lower()):
            inv_type = 'VEHICLE_INSURANCE'
            if backend_val['expense_ledger'] in ('Purchase Spares & Consumables', 'Diesel Expenses'):
                backend_val['expense_ledger'] = 'Lorry Insurance'
        elif inv_type == 'VEHICLE_SERVICE' or (veh_num and (labour_amt > Decimal('0.00') or any(kw in (raw_party_name or '').lower() for kw in ['tvs', 'auto work', 'garage', 'workshop', 'motors', 'service', 'automobiles', 'body building', 'retread']))):
            inv_type = 'VEHICLE_SERVICE'
            if backend_val['expense_ledger'] in ('Purchase Spares & Consumables', 'Diesel Expenses'):
                backend_val['expense_ledger'] = 'Labour Charges / Vehicle Service'
        else:
            inv_type = 'REGULAR'

        # If vehicle insurance and invoice_number is empty, use policy_number
        inv_num = (data.get('invoice_number') or '').strip()
        if not inv_num and pol_num:
            inv_num = pol_num

        # --- INTERNAL APPEARANCE & FINANCIAL CHECK + COMBINED E-INVOICE STATUS ---
        from .document_type_utils import (
            classify_document_type,
            has_unclear_financial_values,
        )
        doc_type_res = classify_document_type(
            file_path=image_path,
            raw_text=original_pdf_text or '',
            gemini_data=data,
        )
        qr_data = extract_qr_e_invoice_data(image_path)
        einv_val = evaluate_e_invoice_status(
            raw_text=original_pdf_text or '',
            qr_data=qr_data,
            gemini_data=data,
            appearance_data=doc_type_res,
        )
        has_hw_amounts = bool(data.get('has_handwritten_amounts'))
        unclear_financials = has_unclear_financial_values({
            'taxable_amount': taxable_amt,
            'cgst_amount': cgst_amt,
            'sgst_amount': sgst_amt,
            'igst_amount': igst_amt,
            'total_amount': total_amt,
        })
        hw_manual_gate = (
            einv_val['e_invoice_status'] == 'HANDWRITTEN'
            or doc_type_res['document_type'] == 'NEEDS_REVIEW'
            or has_hw_amounts
            or unclear_financials
        )

        final_manual_review = (
            backend_val['requires_manual_review']
            or hw_manual_gate
            or bool(fin_warnings)
            or (einv_val['is_e_invoice'] is True and not einv_val['irn_number'])
        )
        final_approval_eligible = (
            backend_val['is_financial_approval_eligible']
            and not unclear_financials
            and not bool(fin_warnings)
            and not (einv_val['is_e_invoice'] is True and not einv_val['irn_number'])
        )
        combined_notes = "; ".join(
            p for p in [backend_val.get('audit_notes', ''), *fin_warnings] if p
        )

        # Assemble separate, preserved fields
        # raw_text preserves original digital PDF or OCR text; gemini summary is stored alongside
        final_raw_text = original_pdf_text if original_pdf_text.strip() else (data.get('raw_summary') or '')

        usage_meta = getattr(response, 'usage_metadata', None)
        def _safe_int(val):
            return val if isinstance(val, int) and not isinstance(val, bool) else None

        gemini_usage = {
            'model': used_model,
            'latency_ms': round((time.perf_counter() - gemini_start) * 1000.0, 2),
            'prompt_tokens': _safe_int(getattr(usage_meta, 'prompt_token_count', None)),
            'candidates_tokens': _safe_int(getattr(usage_meta, 'candidates_token_count', None)),
            'total_tokens': _safe_int(getattr(usage_meta, 'total_token_count', None)),
            'error': last_error_type,
        }

        result: Dict[str, Any] = {
            'party_name': backend_val['party_name'] or raw_party_name,
            'party_gstin': backend_val['party_gstin'] or raw_party_gstin,
            'pan': backend_val.get('pan') or raw_pan,
            'invoice_number': inv_num,
            'invoice_type': inv_type,
            'policy_number': pol_num,
            'vehicle_number': veh_num,
            'labour_charges': labour_amt,
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
            'e_invoice_status': einv_val['e_invoice_status'],
            'is_e_invoice': einv_val['is_e_invoice'],
            'irn_number': einv_val['irn_number'],
            'e_invoice_source': einv_val['e_invoice_source'],
            'raw_text': final_raw_text,
            'gemini_extracted_data': data,
            'gemini_usage': gemini_usage,
            'ocr_extracted_text': original_pdf_text,
            'extraction_method': 'GEMINI_VISION_AI',
            'confidence': backend_val['confidence'],
            'requires_manual_review': final_manual_review,
            'is_ambiguous_vendor': backend_val['is_ambiguous_vendor'],
            'vendor_match_status': backend_val['vendor_match_status'],
            'is_financial_approval_eligible': final_approval_eligible,
            'audit_notes': combined_notes,
        }

        # Keep business data (vendor, GSTIN, amounts) out of INFO logs.
        logger.info("Gemini Vision extraction complete | model=%s | match=%s", used_model, result['vendor_match_status'])
        return result

    except Exception as exc:
        logger.warning("Gemini Vision AI extraction exception (falling back to RapidOCR): %s", type(exc).__name__)
        return None
