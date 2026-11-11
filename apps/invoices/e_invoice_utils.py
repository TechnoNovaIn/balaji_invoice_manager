"""
E-Invoice & IRN (Invoice Reference Number) Extraction, QR Decoding, and Validation.
Uses PyMuPDF, RapidOCR/OCR text, and OpenCV QRCodeDetector (no external paid APIs).
Never fabricates IRNs or trusts unverified LLM output without validation.
"""

import base64
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Valid IRN in Indian GST e-Invoicing is a 64-character SHA-256 hex/alphanumeric string.
IRN_FORMAT_REGEX = re.compile(r'^[A-Z0-9]{64}$')

# Explicit negative indicators proving invoice is NOT an e-invoice
NON_EINVOICE_PATTERNS = [
    re.compile(r'\bnot\s+(?:an?\s+)?e[-\s]?invoice\b', re.I),
    re.compile(r'\bnon[-\s]+e[-\s]?invoice\b', re.I),
    re.compile(r'\be[-\s]?invoice\s*(?:applicable|status|generated)?\s*[:=\-]?\s*no\b', re.I),
    re.compile(r'\be[-\s]?invoice\s+not\s+(?:applicable|required|generated)\b', re.I),
    re.compile(r'\bexempted\s+from\s+e[-\s]?invoic(?:e|ing)\b', re.I),
    re.compile(r'\birn\s*[:=\-]?\s*(?:n/?a|not\s+applicable|nil|none)\b', re.I),
]

# Explicit positive indicators showing the document is an e-invoice
POSITIVE_EINVOICE_PATTERNS = [
    re.compile(r'\be[-\s]?invoic(?:e|ing)\b', re.I),
    re.compile(r'\binvoice\s+reference\s+number\b', re.I),
    re.compile(r'\back(?:nowledgement)?\.?\s*(?:no|number|dt|date)\b', re.I),
    re.compile(r'\bsigned\s+qr\b', re.I),
    re.compile(r'\bnic\s+irp\b', re.I),
]


def normalize_irn(value: Optional[str]) -> str:
    """Strip whitespace/hyphens/newlines and uppercase a candidate IRN string."""
    if not value:
        return ''
    return re.sub(r'[\s\r\n]+', '', str(value)).upper()


def is_valid_irn(value: Optional[str]) -> bool:
    """
    Validate an Indian GST IRN string:
    - Strictly 64 alphanumeric characters (A-Z, 0-9)
    - Rejects trivial repeated/fabricated filler strings (e.g. '0'*64, 'A'*64)
    - Rejects short, truncated, or punctuated strings
    """
    cleaned = normalize_irn(value)
    if not IRN_FORMAT_REGEX.match(cleaned):
        return False
    # Reject obviously fabricated single/two-character filler patterns
    if len(set(cleaned)) <= 2:
        return False
    # Must contain at least one digit (standard SHA-256 hex IRNs always include digits)
    if not any(ch.isdigit() for ch in cleaned):
        return False
    return True


def parse_e_invoice_choice(raw_value: Any) -> Optional[bool]:
    """Convert form/API value ('true'/'yes'/'1', 'false'/'no'/'0', '') to Optional[bool]."""
    if isinstance(raw_value, bool):
        return raw_value
    val = str(raw_value or '').strip().lower()
    if val in ('true', 'yes', '1', 'on'):
        return True
    if val in ('false', 'no', '0'):
        return False
    return None


def format_e_invoice_status(is_e_invoice: Optional[bool]) -> str:
    """Return canonical audit/display status label."""
    if is_e_invoice is True:
        return 'YES'
    if is_e_invoice is False:
        return 'NO'
    return 'NEEDS VERIFICATION'


def extract_irn_from_text(raw_text: str) -> Dict[str, Any]:
    """
    Inspect extracted PDF/OCR text for:
    - Valid 64-character IRN values (including wrapped across lines after an IRN label)
    - Explicit IRN labels whose value is missing or invalid
    - Explicit positive or negative e-invoice declarations
    """
    info: Dict[str, Any] = {
        'valid_irn': '',
        'invalid_irn_candidate': False,
        'explicit_irn_label': False,
        'explicit_positive_einvoice': False,
        'explicit_negative_einvoice': False,
    }
    if not raw_text or not raw_text.strip():
        return info

    text = raw_text.strip()

    # Check explicit non-e-invoice phrases first
    for pat in NON_EINVOICE_PATTERNS:
        if pat.search(text):
            info['explicit_negative_einvoice'] = True
            break

    # Check explicit positive e-invoice phrases (excluding matched negation)
    for pat in POSITIVE_EINVOICE_PATTERNS:
        if pat.search(text) and not info['explicit_negative_einvoice']:
            info['explicit_positive_einvoice'] = True
            break

    # 1. Search explicit IRN labels (allowing whitespace/line break wrapping inside the 64-char token)
    label_pattern = re.compile(
        r'(?:\bI\.?\s*R\.?\s*N\.?\b|\bInvoice\s+Reference\s+Number\b)'
        r'(?:\s*(?:No\.?|Number|#))?\s*[:=\-]?\s*([^\n]{0,80}(?:\n[^\n]{0,80})?)',
        re.I,
    )
    for m in label_pattern.finditer(text):
        trailing = m.group(1).strip()
        if not trailing:
            info['explicit_irn_label'] = True
            continue

        # Check if explicitly N/A or None
        if re.match(r'^(?:n/?a|not\s+applicable|nil|none)\b', trailing, re.I):
            info['explicit_negative_einvoice'] = True
            continue

        # Stop before next unrelated field label if present
        trailing_head = re.split(
            r'\b(?:Ack\.?\s*(?:No|Date)|Date|Invoice|Bill|GSTIN|Total|Vehicle|Buyer|Seller|Party|Qty)\b',
            trailing,
            maxsplit=1,
            flags=re.I,
        )[0].strip()

        if not trailing_head:
            info['explicit_irn_label'] = True
            continue

        info['explicit_irn_label'] = True
        info['explicit_positive_einvoice'] = True

        # Strip separators/spaces to handle OCR line breaks across a 64-char hash
        compact = re.sub(r'[\s\r\n]+', '', trailing_head).upper()
        # Look for a strict 64-char alphanumeric token
        tok_match = re.match(r'^([A-Z0-9]{64})(?:[^A-Z0-9]|$)', compact)
        if tok_match and is_valid_irn(tok_match.group(1)):
            info['valid_irn'] = tok_match.group(1)
            return info

        # Also check space-separated first token before concluding it's invalid
        first_line_tok = re.split(r'[\s,;]+', trailing_head)[0]
        if is_valid_irn(first_line_tok):
            info['valid_irn'] = normalize_irn(first_line_tok)
            return info

        # Explicit label followed by a non-empty token that isn't a valid 64-char IRN
        if len(re.sub(r'[^A-Za-z0-9]', '', first_line_tok)) >= 2:
            info['invalid_irn_candidate'] = True

    # 2. Fallback: standalone 64-char hex/alphanumeric token only in e-invoice / GST tax invoice context
    has_tax_context = bool(re.search(r'(?:GSTIN|Tax\s+Invoice|e[-\s]?invoice|Ack|IRN|CGST|SGST|IGST)', text, re.I))
    if has_tax_context:
        for hex_match in re.finditer(r'\b([0-9a-fA-F]{64})\b', text):
            cand = hex_match.group(1).upper()
            # Ensure it looks like a SHA-256 hex digest (contains both digits and a-f letters)
            if is_valid_irn(cand) and any(c in 'ABCDEF' for c in cand) and any(c.isdigit() for c in cand):
                info['valid_irn'] = cand
                info['explicit_positive_einvoice'] = True
                return info

    return info


def _decode_jwt_or_json_qr(qr_payload: str) -> Dict[str, Any]:
    """
    Parse decoded QR payload text from an Indian GST e-Invoice QR code.
    Supports:
    - Standard signed IRP JWT (<header>.<payload>.<sig>) containing `data` JSON with `Irn`
    - Plain JSON containing `Irn` / `IRN`
    - Key-value / plain text containing 64-char IRN
    """
    res = {'valid_irn': '', 'has_qr_einvoice_data': False, 'raw_payload': qr_payload or ''}
    if not qr_payload or not isinstance(qr_payload, str):
        return res

    payload_str = qr_payload.strip()

    # 1. Signed JWT (3 base64url parts separated by '.')
    jwt_parts = payload_str.split('.')
    if len(jwt_parts) == 3 and all(len(p) >= 8 for p in jwt_parts[:2]):
        try:
            b64_body = jwt_parts[1]
            padded = b64_body + '=' * (-len(b64_body) % 4)
            decoded_bytes = base64.urlsafe_b64decode(padded.encode('ascii'))
            outer = json.loads(decoded_bytes.decode('utf-8', errors='ignore'))
            inner = outer.get('data') if isinstance(outer, dict) else None
            if isinstance(inner, str):
                try:
                    inner = json.loads(inner)
                except Exception:
                    pass
            search_obj = inner if isinstance(inner, dict) else outer
            if isinstance(search_obj, dict):
                irn_cand = search_obj.get('Irn') or search_obj.get('IRN') or search_obj.get('irn') or ''
                if is_valid_irn(irn_cand):
                    res['valid_irn'] = normalize_irn(irn_cand)
                    res['has_qr_einvoice_data'] = True
                    return res
                if any(k in search_obj for k in ('Irn', 'IRN', 'SellerGstin', 'DocNo', 'IrnDt')):
                    res['has_qr_einvoice_data'] = True
        except Exception:
            pass

    # 2. Raw JSON QR payload
    if payload_str.startswith('{'):
        try:
            obj = json.loads(payload_str)
            if isinstance(obj, dict):
                irn_cand = obj.get('Irn') or obj.get('IRN') or obj.get('irn') or ''
                if is_valid_irn(irn_cand):
                    res['valid_irn'] = normalize_irn(irn_cand)
                    res['has_qr_einvoice_data'] = True
                    return res
                if any(k in obj for k in ('Irn', 'IRN', 'SellerGstin', 'DocNo')):
                    res['has_qr_einvoice_data'] = True
        except Exception:
            pass

    # 3. URL / text containing IRN
    txt_res = extract_irn_from_text(payload_str)
    if txt_res.get('valid_irn'):
        res['valid_irn'] = txt_res['valid_irn']
        res['has_qr_einvoice_data'] = True

    return res


def _decode_qr_from_cv_image(img_bgr) -> List[str]:
    """Detect and decode one or more QR codes from an OpenCV numpy image."""
    decoded_strings: List[str] = []
    if img_bgr is None:
        return decoded_strings
    try:
        import cv2
        detector = cv2.QRCodeDetector()
        # Try multi-QR first, fall back to single-QR
        try:
            ok, decoded_info, _, _ = detector.detectAndDecodeMulti(img_bgr)
            if ok and decoded_info:
                decoded_strings.extend([s for s in decoded_info if s])
        except Exception:
            pass

        if not decoded_strings:
            val, _, _ = detector.detectAndDecode(img_bgr)
            if val:
                decoded_strings.append(val)

        # Retry with grayscale + Otsu threshold if color decode found nothing
        if not decoded_strings and len(img_bgr.shape) >= 2:
            gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY) if len(img_bgr.shape) == 3 else img_bgr
            val, _, _ = detector.detectAndDecode(gray)
            if val:
                decoded_strings.append(val)
            else:
                _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                val2, _, _ = detector.detectAndDecode(thresh)
                if val2:
                    decoded_strings.append(val2)
    except Exception as exc:
        logger.debug("QR decode failed softly: %s", exc)
    return decoded_strings


def extract_qr_e_invoice_data(file_path: str, page_indices: Optional[List[int]] = None) -> Dict[str, Any]:
    """
    Attempt offline e-invoice QR decoding using OpenCV QRCodeDetector on:
    - Direct image uploads (JPEG/PNG/WEBP)
    - PDF pages rendered via PyMuPDF (first page or specified segment pages)
    """
    empty_res = {'valid_irn': '', 'has_qr_einvoice_data': False, 'raw_payloads': []}
    if not file_path or not os.path.exists(file_path):
        return empty_res

    try:
        import cv2
        import numpy as np
    except ImportError:
        return empty_res

    ext = os.path.splitext(file_path)[1].lower()
    decoded_payloads: List[str] = []

    if ext == '.pdf':
        try:
            import pymupdf as fitz
            doc = fitz.open(file_path)
            target_pages = page_indices if page_indices is not None else list(range(min(len(doc), 3)))
            for p_idx in target_pages[:3]:
                if p_idx < 0 or p_idx >= len(doc):
                    continue
                page = doc[p_idx]
                # Render only when QR decode makes sense cleanly at 200 DPI
                pix = page.get_pixmap(dpi=200)
                img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
                if pix.n == 4:
                    img_bgr = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
                elif pix.n == 3:
                    img_bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
                else:
                    img_bgr = img
                decoded_payloads.extend(_decode_qr_from_cv_image(img_bgr))
                if decoded_payloads:
                    break
            doc.close()
        except Exception as exc:
            logger.debug("PDF QR extraction skipped: %s", exc)
    else:
        try:
            img_bgr = cv2.imread(file_path)
            decoded_payloads.extend(_decode_qr_from_cv_image(img_bgr))
        except Exception as exc:
            logger.debug("Image QR extraction skipped: %s", exc)

    out = {'valid_irn': '', 'has_qr_einvoice_data': False, 'raw_payloads': decoded_payloads}
    for payload in decoded_payloads:
        parsed = _decode_jwt_or_json_qr(payload)
        if parsed['has_qr_einvoice_data']:
            out['has_qr_einvoice_data'] = True
        if parsed['valid_irn']:
            out['valid_irn'] = parsed['valid_irn']
            return out
    return out


def _text_supports_irn(irn: str, raw_text: str) -> bool:
    """Confirm an LLM-supplied IRN appears in the source OCR/PDF text (handles whitespace wraps)."""
    if not raw_text or not raw_text.strip():
        return False
    compact_text = re.sub(r'[\s\r\n]+', '', raw_text).upper()
    return normalize_irn(irn) in compact_text


def evaluate_e_invoice_status(
    raw_text: str = '',
    qr_data: Optional[Dict[str, Any]] = None,
    gemini_data: Optional[Dict[str, Any]] = None,
    has_unreadable_text: bool = False,
) -> Dict[str, Any]:
    """
    Authoritative backend e-invoice classification & IRN validation (Rules 1-5):
    1. Valid 64-char IRN verified -> is_e_invoice = True, irn_number = valid_irn.
    2. Explicit non-e-invoice declaration -> is_e_invoice = False, irn_number = ''.
    3. Insufficient evidence -> is_e_invoice = None (NEEDS VERIFICATION), manual review required.
    4. E-invoice identified but IRN missing or invalid -> is_e_invoice = True, irn_number = '', manual review required.
    5. Gemini cannot invent an IRN or bypass verification without supporting validation.
    """
    text_info = extract_irn_from_text(raw_text or '')
    qr_irn = normalize_irn((qr_data or {}).get('valid_irn'))
    has_qr_einv = bool((qr_data or {}).get('has_qr_einvoice_data'))

    # 1. Validated IRN from QR or extracted PDF/OCR text takes highest precedence
    if is_valid_irn(qr_irn):
        return {
            'is_e_invoice': True,
            'irn_number': qr_irn,
            'e_invoice_manual_review': False,
            'e_invoice_source': 'QR_CODE',
            'notes': 'Verified IRN decoded from e-invoice QR code.',
        }

    if is_valid_irn(text_info.get('valid_irn')):
        return {
            'is_e_invoice': True,
            'irn_number': text_info['valid_irn'],
            'e_invoice_manual_review': False,
            'e_invoice_source': 'TEXT_EXTRACTION',
            'notes': 'Verified 64-character IRN extracted from invoice text.',
        }

    # 2. Gemini candidate evaluation (strictly validated — never trust fabricated values)
    gemini_irn_raw = normalize_irn((gemini_data or {}).get('irn_number'))
    gemini_flag = (gemini_data or {}).get('is_e_invoice') if isinstance(gemini_data, dict) else None
    gemini_irn_invalid = bool(gemini_irn_raw and not is_valid_irn(gemini_irn_raw))

    if is_valid_irn(gemini_irn_raw):
        # If digital/OCR text exists on the page, require textual/QR corroboration
        # so Gemini cannot invent an IRN not present in the document.
        has_rich_text = len((raw_text or '').strip()) >= 30
        if not has_rich_text or _text_supports_irn(gemini_irn_raw, raw_text):
            return {
                'is_e_invoice': True,
                'irn_number': gemini_irn_raw,
                'e_invoice_manual_review': False if has_rich_text else True,
                'e_invoice_source': 'GEMINI_VISION_VERIFIED',
                'notes': 'Valid 64-character IRN verified from Gemini Vision extraction.',
            }
        # Gemini suggested an IRN that contradicts digital/OCR text -> treat as unverified!
        return {
            'is_e_invoice': True if (text_info['explicit_positive_einvoice'] or has_qr_einv) else None,
            'irn_number': '',
            'e_invoice_manual_review': True,
            'e_invoice_source': 'REJECTED_UNVERIFIED_IRN',
            'notes': 'Gemini IRN candidate was rejected because it could not be corroborated in document text.',
        }

    # 3. Explicit Non-E-Invoice declaration (Rule 2)
    if text_info['explicit_negative_einvoice'] and not (has_qr_einv or text_info['explicit_irn_label']):
        return {
            'is_e_invoice': False,
            'irn_number': '',
            'e_invoice_manual_review': False,
            'e_invoice_source': 'TEXT_NEGATIVE',
            'notes': 'Document explicitly indicates it is not an e-invoice.',
        }

    # Also allow explicit non-e-invoice classification ONLY when Gemini says False AND text confirms
    # (never let Gemini alone mark False on ambiguous/blank/unclear scans)
    if gemini_flag is False and text_info['explicit_negative_einvoice']:
        return {
            'is_e_invoice': False,
            'irn_number': '',
            'e_invoice_manual_review': False,
            'e_invoice_source': 'GEMINI_CONFIRMED_NEGATIVE',
            'notes': 'Confirmed non-e-invoice.',
        }

    # 4. E-Invoice identified (positive cue, label, QR cue, or invalid IRN candidate) but valid IRN missing
    if (
        text_info['explicit_positive_einvoice']
        or text_info['explicit_irn_label']
        or text_info['invalid_irn_candidate']
        or has_qr_einv
        or gemini_flag is True
        or gemini_irn_invalid
    ):
        return {
            'is_e_invoice': True,
            'irn_number': '',
            'e_invoice_manual_review': True,
            'e_invoice_source': 'MISSING_OR_INVALID_IRN',
            'notes': 'E-invoice indicators detected, but a valid 64-character IRN was not found. Manual verification required.',
        }

    # 5. Insufficient information -> NEEDS VERIFICATION (Rule 3)
    return {
        'is_e_invoice': None,
        'irn_number': '',
        'e_invoice_manual_review': True,
        'e_invoice_source': 'INSUFFICIENT_EVIDENCE',
        'notes': 'Insufficient e-invoice evidence in document; requires Naveen verification.',
    }


def apply_e_invoice_to_submission(submission, metadata: Optional[Dict[str, Any]] = None) -> None:
    """
    Safely applies and enforces e-invoice/IRN invariants on an InvoiceSubmission instance
    without saving yet:
    - Rejects/clears invalid or incomplete IRNs
    - Clears IRN when is_e_invoice is False
    - Enforces requires_manual_review = True if identified as e-invoice without valid IRN
      or if e-invoice status is undetermined (None)
    """
    if metadata is not None:
        if 'is_e_invoice' in metadata:
            submission.is_e_invoice = metadata['is_e_invoice']
        if 'irn_number' in metadata:
            cand = normalize_irn(metadata.get('irn_number'))
            submission.irn_number = cand if is_valid_irn(cand) else ''

    # Enforce strict storage invariants regardless of source
    if submission.is_e_invoice is False:
        submission.irn_number = ''
    elif submission.irn_number:
        norm = normalize_irn(submission.irn_number)
        if is_valid_irn(norm):
            submission.irn_number = norm
            submission.is_e_invoice = True
        else:
            submission.irn_number = ''
            submission.requires_manual_review = True

    if submission.is_e_invoice is True and not is_valid_irn(submission.irn_number):
        submission.requires_manual_review = True
