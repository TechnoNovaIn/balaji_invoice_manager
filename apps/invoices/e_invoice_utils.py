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

# Valid IRN in Indian GST e-Invoicing is strictly a 64-character hexadecimal SHA-256 hash.
IRN_FORMAT_REGEX = re.compile(r'^[A-F0-9]{64}$')

# Explicit negative indicators proving invoice is NOT an e-invoice.
# NOTE: "IRN: N/A", "IRN: Nil", "IRN: None", or missing IRN are NOT included here because
# missing/N/A IRN alone does not prove an invoice is a non-e-invoice (use NEEDS VERIFICATION).
NON_EINVOICE_PATTERNS = [
    re.compile(r'\bnot\s+(?:an?\s+)?e[-\s]?invoice\b', re.I),
    re.compile(r'\bnon[-\s]+e[-\s]?invoice\b', re.I),
    re.compile(r'\be[-\s]?invoice\s*(?:applicable|status|generated)?\s*[:=\-]?\s*no\b', re.I),
    re.compile(r'\be[-\s]?invoice\s+not\s+(?:applicable|required|generated)\b', re.I),
    re.compile(r'\bexempted\s+from\s+e[-\s]?invoic(?:e|ing)\b', re.I),
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
    """Strip whitespace/newlines and uppercase a candidate IRN string."""
    if not value:
        return ''
    return re.sub(r'[\s\r\n]+', '', str(value)).upper()


def is_valid_irn(value: Optional[str]) -> bool:
    """
    Validate an Indian GST IRN string:
    - Strictly 64 hexadecimal SHA-256 characters: ^[A-F0-9]{64}$ (rejects G-Z, wrong length, punctuation)
    - Accepts valid SHA-256 hashes containing multiple '0' digits
    - Rejects trivial repeated single/two-character filler patterns (e.g. '0'*64, 'A'*64, '01'*32)
    """
    cleaned = normalize_irn(value)
    if not IRN_FORMAT_REGEX.match(cleaned):
        return False
    # Reject obviously fabricated single/two-character repeated filler patterns
    if len(set(cleaned)) <= 2:
        return False
    return True


EINV_STATUS_YES = 'YES'
EINV_STATUS_NO = 'NO'
EINV_STATUS_HANDWRITTEN = 'HANDWRITTEN'
EINV_STATUS_NEEDS_VERIFICATION = 'NEEDS_VERIFICATION'

VALID_EINV_STATUSES = {
    EINV_STATUS_YES,
    EINV_STATUS_NO,
    EINV_STATUS_HANDWRITTEN,
    EINV_STATUS_NEEDS_VERIFICATION,
}


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


def parse_e_invoice_status(raw_value: Any) -> Optional[str]:
    """
    Parse user/API input into one of the 4 canonical combined statuses:
    'YES', 'NO', 'HANDWRITTEN', 'NEEDS_VERIFICATION'.
    Returns None ONLY for invalid/unrecognized inputs.
    """
    if raw_value is True:
        return EINV_STATUS_YES
    if raw_value is False:
        return EINV_STATUS_NO
    if raw_value is None:
        return EINV_STATUS_NEEDS_VERIFICATION

    val = re.sub(r'[\s\-]+', '_', str(raw_value).strip().upper())
    mapping = {
        '': EINV_STATUS_NEEDS_VERIFICATION,
        'YES': EINV_STATUS_YES,
        'TRUE': EINV_STATUS_YES,
        '1': EINV_STATUS_YES,
        'ON': EINV_STATUS_YES,
        'NO': EINV_STATUS_NO,
        'FALSE': EINV_STATUS_NO,
        '0': EINV_STATUS_NO,
        'HANDWRITTEN': EINV_STATUS_HANDWRITTEN,
        'HAND_WRITTEN': EINV_STATUS_HANDWRITTEN,
        'NEEDS_VERIFICATION': EINV_STATUS_NEEDS_VERIFICATION,
        'NEEDS_REVIEW': EINV_STATUS_NEEDS_VERIFICATION,
        'NONE': EINV_STATUS_NEEDS_VERIFICATION,
        'NULL': EINV_STATUS_NEEDS_VERIFICATION,
    }
    return mapping.get(val)


def e_invoice_status_to_bool(status: Optional[str]) -> Optional[bool]:
    """Map canonical 4-state e_invoice_status to persistent nullable is_e_invoice boolean."""
    if status == EINV_STATUS_YES:
        return True
    if status == EINV_STATUS_NO:
        return False
    # Both HANDWRITTEN and NEEDS_VERIFICATION keep is_e_invoice = None
    # because handwriting alone does not establish non-e-invoice legal status.
    return None


def format_e_invoice_status(value: Any) -> str:
    """Return canonical audit/display status label ('YES', 'NO', 'HANDWRITTEN', 'NEEDS VERIFICATION')."""
    if value == EINV_STATUS_HANDWRITTEN or value == 'HANDWRITTEN':
        return 'HANDWRITTEN'
    if value is True or value == EINV_STATUS_YES:
        return 'YES'
    if value is False or value == EINV_STATUS_NO:
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
        'irn_marked_na': False,
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

    # 1. Search explicit IRN labels (allowing whitespace/line break wrapping inside the 64-char hex token)
    label_pattern = re.compile(
        r'(?:\bI\.?\s*R\.?\s*N\.?\b|\bInvoice\s+Reference\s+Number\b)'
        r'(?:\s*(?:No\.?|Number|#))?\s*[:=\-]?\s*([^\n]{0,80}(?:\n[^\n]{0,80})?)',
        re.I,
    )
    for m in label_pattern.finditer(text):
        trailing = m.group(1).strip()
        if not trailing:
            # Blank IRN placeholder alone does not prove YES or NO; rely on explicit indicators or NEEDS VERIFICATION.
            continue

        # IRN explicitly marked N/A / Nil / None: never treat alone as Non-E-Invoice (NO);
        # keep insufficient-evidence status (NEEDS VERIFICATION) unless another explicit declaration exists.
        if re.match(r'^(?:n/?a|not\s+applicable|nil|none)\b', trailing, re.I):
            info['irn_marked_na'] = True
            continue

        # Stop before next unrelated field label if present
        trailing_head = re.split(
            r'\b(?:Ack\.?\s*(?:No|Date)|Date|Invoice|Bill|GSTIN|Total|Vehicle|Buyer|Seller|Party|Qty)\b',
            trailing,
            maxsplit=1,
            flags=re.I,
        )[0].strip()

        if not trailing_head:
            # No token after IRN label (followed immediately by another label)
            continue

        info['explicit_irn_label'] = True
        info['explicit_positive_einvoice'] = True

        # Strip separators/spaces to handle OCR line breaks across a 64-char hex hash
        compact = re.sub(r'[\s\r\n]+', '', trailing_head).upper()
        # Strict 64-char hexadecimal token (rejecting G-Z and rejecting >64 alphanumeric tokens)
        tok_match = re.match(r'^([A-F0-9]{64})(?:[^A-Z0-9]|$)', compact)
        if tok_match and is_valid_irn(tok_match.group(1)):
            info['valid_irn'] = tok_match.group(1)
            return info

        # Also check space-separated first token before concluding it's invalid
        first_line_tok = re.split(r'[\s,;]+', trailing_head)[0]
        if is_valid_irn(first_line_tok):
            info['valid_irn'] = normalize_irn(first_line_tok)
            return info

        # Explicit label followed by a non-empty token that isn't a valid 64-char hex IRN
        if len(re.sub(r'[^A-Za-z0-9]', '', first_line_tok)) >= 2:
            info['invalid_irn_candidate'] = True

    # 2. Fallback: standalone 64-char hex token only in e-invoice / GST tax invoice context
    has_tax_context = bool(re.search(r'(?:GSTIN|Tax\s+Invoice|e[-\s]?invoice|Ack|IRN|CGST|SGST|IGST)', text, re.I))
    if has_tax_context:
        for hex_match in re.finditer(r'(?<![A-Za-z0-9])([0-9a-fA-F]{64})(?![A-Za-z0-9])', text):
            cand = hex_match.group(1).upper()
            # Ensure standalone unlabeled hex token looks like a SHA-256 digest (has digits and A-F)
            if is_valid_irn(cand) and any(c in 'ABCDEF' for c in cand) and any(c.isdigit() for c in cand):
                info['valid_irn'] = cand
                info['explicit_positive_einvoice'] = True
                return info

    return info


def _get_configured_irp_public_key() -> Optional[str]:
    """Return configured trusted NIC/IRP public key PEM/key if explicitly set in Django settings."""
    try:
        from django.conf import settings
        key = getattr(settings, 'E_INVOICE_IRP_PUBLIC_KEY', None)
        return key or None
    except Exception:
        return None


def _verify_jwt_signature(jwt_token: str, trusted_public_key: Optional[str]) -> bool:
    """
    Cryptographically verify a signed JWT ONLY when an explicit trusted public key is supplied.
    Never returns True without genuine cryptographic verification via PyJWT.
    """
    if not trusted_public_key or not jwt_token:
        return False
    try:
        import jwt
        jwt.decode(
            jwt_token,
            trusted_public_key,
            algorithms=['RS256', 'ES256'],
            options={'verify_aud': False},
        )
        return True
    except Exception:
        return False


def _decode_jwt_or_json_qr(
    qr_payload: str,
    trusted_public_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Parse decoded QR payload text from an Indian GST e-Invoice QR code.
    Explicitly distinguishes:
    1. qr_data_extracted: QR string decoded from image/PDF.
    2. qr_payload_structurally_valid: Payload conforms to GST JWT/JSON structure with valid 64-char hex IRN.
    3. qr_signature_cryptographically_verified: Signature verified against an explicit trusted IRP public key.
    """
    pub_key = trusted_public_key or _get_configured_irp_public_key()
    extracted = bool(qr_payload and isinstance(qr_payload, str) and qr_payload.strip())
    res: Dict[str, Any] = {
        'valid_irn': '',
        'has_qr_einvoice_data': False,
        'raw_payload': qr_payload or '',
        'qr_data_extracted': extracted,
        'qr_payload_structurally_valid': False,
        'qr_signature_cryptographically_verified': False,
        'qr_authenticity_status': 'DATA_EXTRACTED' if extracted else 'NONE',
    }
    if not extracted:
        return res

    payload_str = qr_payload.strip()

    # 1. Signed JWT (3 non-empty base64url parts separated by '.')
    jwt_parts = payload_str.split('.')
    if len(jwt_parts) == 3 and all(len(p) >= 1 for p in jwt_parts):
        try:
            def _b64_json(seg: str) -> Any:
                padded = seg + '=' * (-len(seg) % 4)
                raw = base64.urlsafe_b64decode(padded.encode('ascii'))
                return json.loads(raw.decode('utf-8', errors='strict'))

            header = _b64_json(jwt_parts[0])
            outer = _b64_json(jwt_parts[1])
            if isinstance(header, dict) and isinstance(outer, dict) and ('alg' in header or 'typ' in header):
                inner = outer.get('data')
                if isinstance(inner, str):
                    try:
                        inner = json.loads(inner)
                    except Exception:
                        pass
                search_obj = inner if isinstance(inner, dict) else outer
                if isinstance(search_obj, dict):
                    irn_cand = search_obj.get('Irn') or search_obj.get('IRN') or search_obj.get('irn') or ''
                    if any(k in search_obj for k in ('Irn', 'IRN', 'SellerGstin', 'DocNo', 'IrnDt')):
                        res['has_qr_einvoice_data'] = True
                    if is_valid_irn(irn_cand):
                        res['valid_irn'] = normalize_irn(irn_cand)
                        res['has_qr_einvoice_data'] = True
                        res['qr_payload_structurally_valid'] = True
                        crypto_ok = _verify_jwt_signature(payload_str, pub_key)
                        res['qr_signature_cryptographically_verified'] = crypto_ok
                        res['qr_authenticity_status'] = (
                            'CRYPTOGRAPHICALLY_VERIFIED' if crypto_ok else 'STRUCTURALLY_VALIDATED'
                        )
                        return res
        except Exception:
            pass

    # 2. Unsigned Raw JSON QR payload
    if payload_str.startswith('{'):
        try:
            obj = json.loads(payload_str)
            if isinstance(obj, dict):
                irn_cand = obj.get('Irn') or obj.get('IRN') or obj.get('irn') or ''
                if any(k in obj for k in ('Irn', 'IRN', 'SellerGstin', 'DocNo')):
                    res['has_qr_einvoice_data'] = True
                if is_valid_irn(irn_cand):
                    res['valid_irn'] = normalize_irn(irn_cand)
                    res['has_qr_einvoice_data'] = True
                    res['qr_payload_structurally_valid'] = True
                    res['qr_signature_cryptographically_verified'] = False
                    res['qr_authenticity_status'] = 'STRUCTURALLY_VALIDATED'
                    return res
        except Exception:
            pass

    # 3. URL / unstructured text containing IRN (extracted, NOT structurally validated as GST JWT/JSON)
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


def extract_qr_e_invoice_data(
    file_path: str,
    page_indices: Optional[List[int]] = None,
    trusted_public_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Attempt offline e-invoice QR decoding using OpenCV QRCodeDetector on:
    - Direct image uploads (JPEG/PNG/WEBP)
    - PDF pages rendered via PyMuPDF (first page or specified segment pages)

    Always distinguishes between:
    - qr_data_extracted
    - qr_payload_structurally_valid
    - qr_signature_cryptographically_verified
    """
    empty_res: Dict[str, Any] = {
        'valid_irn': '',
        'has_qr_einvoice_data': False,
        'raw_payloads': [],
        'qr_data_extracted': False,
        'qr_payload_structurally_valid': False,
        'qr_signature_cryptographically_verified': False,
        'qr_authenticity_status': 'NONE',
    }
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

    out: Dict[str, Any] = {
        'valid_irn': '',
        'has_qr_einvoice_data': False,
        'raw_payloads': decoded_payloads,
        'qr_data_extracted': bool(decoded_payloads),
        'qr_payload_structurally_valid': False,
        'qr_signature_cryptographically_verified': False,
        'qr_authenticity_status': 'DATA_EXTRACTED' if decoded_payloads else 'NONE',
    }
    for payload in decoded_payloads:
        parsed = _decode_jwt_or_json_qr(payload, trusted_public_key=trusted_public_key)
        if parsed.get('has_qr_einvoice_data'):
            out['has_qr_einvoice_data'] = True
        if parsed.get('qr_payload_structurally_valid'):
            out['qr_payload_structurally_valid'] = True
            out['qr_authenticity_status'] = 'STRUCTURALLY_VALIDATED'
        if parsed.get('qr_signature_cryptographically_verified'):
            out['qr_signature_cryptographically_verified'] = True
            out['qr_authenticity_status'] = 'CRYPTOGRAPHICALLY_VERIFIED'
        if parsed.get('valid_irn'):
            out['valid_irn'] = parsed['valid_irn']
            return out
    return out


def _text_supports_irn(irn: str, raw_text: str) -> bool:
    """Confirm an LLM-supplied IRN appears in the source OCR/PDF text (handles whitespace wraps)."""
    if not raw_text or not raw_text.strip():
        return False
    compact_text = re.sub(r'[\s\r\n]+', '', raw_text).upper()
    return normalize_irn(irn) in compact_text


def _build_qr_authenticity_fields(qr_data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract normalized 3-stage QR authenticity flags from decoded QR metadata."""
    data = qr_data or {}
    extracted = bool(data.get('qr_data_extracted') or data.get('raw_payloads') or data.get('raw_payload'))
    structural = bool(data.get('qr_payload_structurally_valid'))
    crypto = bool(data.get('qr_signature_cryptographically_verified'))
    if crypto:
        status = 'CRYPTOGRAPHICALLY_VERIFIED'
    elif structural:
        status = 'STRUCTURALLY_VALIDATED'
    elif extracted:
        status = 'DATA_EXTRACTED'
    else:
        status = 'NONE'
    return {
        'qr_data_extracted': extracted,
        'qr_payload_structurally_valid': structural,
        'qr_signature_cryptographically_verified': crypto,
        'qr_authenticity_status': status,
    }


def evaluate_e_invoice_status(
    raw_text: str = '',
    qr_data: Optional[Dict[str, Any]] = None,
    gemini_data: Optional[Dict[str, Any]] = None,
    appearance_data: Optional[Dict[str, Any]] = None,
    has_unreadable_text: bool = False,
) -> Dict[str, Any]:
    """
    Authoritative backend combined E-Invoice? classification & IRN validation:
    1. Valid 64-char hexadecimal SHA-256 IRN (^[A-F0-9]{64}$) -> e_invoice_status='YES', is_e_invoice=True.
       QR authenticity flags distinguish extraction vs structural validation vs cryptographic signature verification.
    2. Conflicting signals (e.g. e-invoice phrases alongside explicit non-e-invoice text) -> 'NEEDS_VERIFICATION'.
    3. Explicit non-e-invoice declaration -> e_invoice_status='NO', is_e_invoice=False, irn_number=''.
       ('IRN: N/A' or missing IRN alone NEVER marks an invoice as Non-E-Invoice.)
    4. E-invoice identified without valid/readable IRN -> e_invoice_status='YES', is_e_invoice=True, irn_number='', manual review.
    5. Confidently handwritten without contradictory e-invoice evidence -> e_invoice_status='HANDWRITTEN', is_e_invoice=None.
    6. Insufficient evidence -> e_invoice_status='NEEDS_VERIFICATION', is_e_invoice=None, manual review.
    """
    text_info = extract_irn_from_text(raw_text or '')
    qr_irn = normalize_irn((qr_data or {}).get('valid_irn'))
    has_qr_einv = bool((qr_data or {}).get('has_qr_einvoice_data'))
    qr_auth = _build_qr_authenticity_fields(qr_data)
    detected_appearance = (
        (appearance_data or {}).get('document_type')
        or (gemini_data or {}).get('document_type')
        or ''
    ).strip().upper()
    gemini_hw = (gemini_data or {}).get('has_handwritten_amounts') if isinstance(gemini_data, dict) else None
    has_hw_strokes = bool((appearance_data or {}).get('has_handwritten_strokes'))
    is_handwritten = (
        detected_appearance == 'HANDWRITTEN'
        or (
            detected_appearance == 'MIXED'
            and (bool(gemini_hw) or (gemini_hw is None and (has_hw_strokes or bool(appearance_data))))
        )
    )

    # 1. Validated 64-char hex IRN from QR or extracted PDF/OCR text overrides appearance (including handwritten)
    if is_valid_irn(qr_irn):
        if qr_auth['qr_signature_cryptographically_verified']:
            note = 'QR data extracted, payload structurally validated, and digital signature cryptographically verified.'
        elif qr_auth['qr_payload_structurally_valid']:
            note = (
                'QR data extracted and payload structurally validated (64-char hex IRN); '
                'digital signature not cryptographically verified (no trusted IRP public key configured).'
            )
        else:
            note = (
                '64-char hex IRN extracted from QR text payload; '
                'payload not structurally validated and digital signature not cryptographically verified.'
            )
        return {
            'e_invoice_status': EINV_STATUS_YES,
            'is_e_invoice': True,
            'irn_number': qr_irn,
            'e_invoice_manual_review': False,
            'e_invoice_source': 'QR_CODE',
            'notes': note,
            **qr_auth,
        }

    if is_valid_irn(text_info.get('valid_irn')):
        return {
            'e_invoice_status': EINV_STATUS_YES,
            'is_e_invoice': True,
            'irn_number': text_info['valid_irn'],
            'e_invoice_manual_review': False,
            'e_invoice_source': 'TEXT_EXTRACTION',
            'notes': '64-character hexadecimal IRN extracted from invoice text (format-validated; not cryptographically verified).',
            **qr_auth,
        }

    # 2. Gemini candidate evaluation (strictly validated — never trust fabricated values)
    gemini_irn_raw = normalize_irn((gemini_data or {}).get('irn_number'))
    gemini_flag = (gemini_data or {}).get('is_e_invoice') if isinstance(gemini_data, dict) else None
    gemini_irn_invalid = bool(gemini_irn_raw and not is_valid_irn(gemini_irn_raw))

    if is_valid_irn(gemini_irn_raw):
        has_rich_text = len((raw_text or '').strip()) >= 30
        if not has_rich_text or _text_supports_irn(gemini_irn_raw, raw_text):
            return {
                'e_invoice_status': EINV_STATUS_YES,
                'is_e_invoice': True,
                'irn_number': gemini_irn_raw,
                'e_invoice_manual_review': False if has_rich_text else True,
                'e_invoice_source': 'GEMINI_VISION_CORROBORATED',
                'notes': '64-character hexadecimal IRN extracted via Gemini Vision (format-validated; not cryptographically verified).',
                **qr_auth,
            }
        # Gemini suggested an IRN that contradicts digital/OCR text -> conflicting -> NEEDS VERIFICATION!
        return {
            'e_invoice_status': EINV_STATUS_NEEDS_VERIFICATION,
            'is_e_invoice': None,
            'irn_number': '',
            'e_invoice_manual_review': True,
            'e_invoice_source': 'REJECTED_UNVERIFIED_IRN',
            'notes': 'Gemini IRN candidate was rejected because it could not be corroborated in document text.',
            **qr_auth,
        }

    has_positive_einv_cue = bool(
        text_info['explicit_positive_einvoice']
        or text_info['explicit_irn_label']
        or text_info['invalid_irn_candidate']
        or has_qr_einv
        or gemini_flag is True
        or gemini_irn_invalid
    )

    # Conflict check: both negative declaration and positive e-invoice/QR evidence -> NEEDS VERIFICATION
    if text_info['explicit_negative_einvoice'] and has_positive_einv_cue:
        return {
            'e_invoice_status': EINV_STATUS_NEEDS_VERIFICATION,
            'is_e_invoice': None,
            'irn_number': '',
            'e_invoice_manual_review': True,
            'e_invoice_source': 'CONFLICTING_EVIDENCE',
            'notes': 'Conflicting e-invoice and non-e-invoice indicators detected; requires manual verification.',
            **qr_auth,
        }

    # 3. Explicit Non-E-Invoice declaration ONLY (never based solely on missing or N/A IRN)
    if text_info['explicit_negative_einvoice'] and not (has_qr_einv or text_info['explicit_irn_label']):
        return {
            'e_invoice_status': EINV_STATUS_NO,
            'is_e_invoice': False,
            'irn_number': '',
            'e_invoice_manual_review': False,
            'e_invoice_source': 'TEXT_NEGATIVE',
            'notes': 'Document explicitly states it is not an e-invoice.',
            **qr_auth,
        }

    # 4. E-Invoice identified (positive cue, label, QR cue, or invalid IRN candidate) but valid IRN missing
    if has_positive_einv_cue:
        return {
            'e_invoice_status': EINV_STATUS_YES,
            'is_e_invoice': True,
            'irn_number': '',
            'e_invoice_manual_review': True,
            'e_invoice_source': 'MISSING_OR_INVALID_IRN',
            'notes': 'E-invoice indicators detected, but a valid 64-character hexadecimal IRN was not found. Manual verification required.',
            **qr_auth,
        }

    # 5. Confidently handwritten without contradictory e-invoice/QR/IRN evidence -> HANDWRITTEN
    if is_handwritten:
        return {
            'e_invoice_status': EINV_STATUS_HANDWRITTEN,
            'is_e_invoice': None,
            'irn_number': '',
            'e_invoice_manual_review': True,
            'e_invoice_source': 'HANDWRITTEN_DETECTION',
            'notes': 'Handwritten invoice detected with no e-invoice IRN/QR evidence (statutory status unassumed; manual financial review required).',
            **qr_auth,
        }

    # 6. Insufficient information (including missing IRN or "IRN: N/A" alone) -> NEEDS VERIFICATION
    return {
        'e_invoice_status': EINV_STATUS_NEEDS_VERIFICATION,
        'is_e_invoice': None,
        'irn_number': '',
        'e_invoice_manual_review': True,
        'e_invoice_source': 'INSUFFICIENT_EVIDENCE',
        'notes': 'Insufficient e-invoice evidence in document; requires Naveen verification.',
        **qr_auth,
    }


def apply_e_invoice_to_submission(
    submission,
    metadata: Optional[Dict[str, Any]] = None,
    force: bool = False,
) -> None:
    """
    Safely applies and enforces combined E-Invoice? & IRN invariants on an InvoiceSubmission instance
    without saving yet:
    - Never overwrites an operator's manual correction (`e_invoice_manually_verified=True`) from OCR/Gemini metadata
    - Rejects/clears invalid or incomplete IRNs
    - Clears IRN when status is NO or HANDWRITTEN
    - Keeps `is_e_invoice` synchronized with `e_invoice_status`
    - Flags `requires_manual_review = True` if marked YES without valid IRN or if HANDWRITTEN
    """
    if metadata is not None:
        if getattr(submission, 'e_invoice_manually_verified', False) and not force:
            return
        if 'e_invoice_status' in metadata and metadata['e_invoice_status'] in VALID_EINV_STATUSES:
            submission.e_invoice_status = metadata['e_invoice_status']
            submission.is_e_invoice = e_invoice_status_to_bool(submission.e_invoice_status)
        elif 'is_e_invoice' in metadata:
            submission.is_e_invoice = metadata['is_e_invoice']
            doc_hint = str(metadata.get('document_type') or '').strip().upper()
            hw_hint = bool(metadata.get('has_handwritten_amounts'))
            if submission.is_e_invoice is True:
                submission.e_invoice_status = EINV_STATUS_YES
            elif submission.is_e_invoice is False:
                submission.e_invoice_status = EINV_STATUS_NO
            elif doc_hint == 'HANDWRITTEN' or (doc_hint == 'MIXED' and hw_hint):
                submission.e_invoice_status = EINV_STATUS_HANDWRITTEN
            elif getattr(submission, 'e_invoice_status', None) not in (EINV_STATUS_HANDWRITTEN, EINV_STATUS_NEEDS_VERIFICATION):
                submission.e_invoice_status = EINV_STATUS_NEEDS_VERIFICATION

        if 'irn_number' in metadata:
            cand = normalize_irn(metadata.get('irn_number'))
            submission.irn_number = cand if is_valid_irn(cand) else ''

    # Synchronize & enforce strict storage invariants regardless of caller
    if getattr(submission, 'e_invoice_status', None) == EINV_STATUS_NO or submission.is_e_invoice is False:
        submission.e_invoice_status = EINV_STATUS_NO
        submission.is_e_invoice = False
        submission.irn_number = ''
    elif getattr(submission, 'e_invoice_status', None) == EINV_STATUS_HANDWRITTEN:
        submission.e_invoice_status = EINV_STATUS_HANDWRITTEN
        submission.is_e_invoice = None
        submission.irn_number = ''
        submission.requires_manual_review = True
    elif submission.irn_number:
        norm = normalize_irn(submission.irn_number)
        if is_valid_irn(norm):
            submission.irn_number = norm
            submission.is_e_invoice = True
            submission.e_invoice_status = EINV_STATUS_YES
        else:
            submission.irn_number = ''
            submission.requires_manual_review = True
    elif submission.is_e_invoice is True or getattr(submission, 'e_invoice_status', None) == EINV_STATUS_YES:
        submission.is_e_invoice = True
        submission.e_invoice_status = EINV_STATUS_YES
        submission.requires_manual_review = True
    else:
        submission.is_e_invoice = None
        submission.e_invoice_status = EINV_STATUS_NEEDS_VERIFICATION
