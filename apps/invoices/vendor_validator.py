"""
Authoritative Vendor Validation & Financial Safety Engine for Balaji InvoiceHub.
Implements statutory Indian GSTIN format checks, PAN extraction, and deterministic
vendor identity matching rules (A, B, C, D, E) to protect Balaji Transports' books.
"""

import re
from typing import Dict, Any, Optional
from decimal import Decimal

# Statutory Indian GSTIN regex: 2 digits state code + 10 chars PAN + 1 char entity + 1 char Z + 1 checksum char
GSTIN_REGEX = re.compile(r'^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$')
PAN_REGEX = re.compile(r'^[A-Z]{5}[0-9]{4}[A-Z]{1}$')

# Valid Indian GST State & Jurisdiction Codes (01-38, 97 Other Territory, 99 Centre)
VALID_GST_STATE_CODES = {f"{i:02d}" for i in range(1, 39)} | {"97", "99"}

# Statutory 4th character of Indian PAN indicating entity status
PAN_ENTITY_TYPES = {'C', 'P', 'H', 'F', 'A', 'T', 'B', 'L', 'J', 'G'}

# Character alphabet used in statutory Indian GSTIN Modulo 36 checksum
GSTIN_ALPHABET = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
GSTIN_CHAR_MAP = {c: i for i, c in enumerate(GSTIN_ALPHABET)}


def calculate_gstin_checksum(gstin_14: str) -> Optional[str]:
    """
    Calculates the official statutory Indian GSTIN 15th character checksum
    using the official weighted Modulo 36 algorithm.
    Returns the single-character checksum (0-9 or A-Z), or None if input length is invalid.
    """
    cleaned = re.sub(r'[^0-9A-Za-z]', '', str(gstin_14 or '')).upper().strip()
    if len(cleaned) < 14:
        return None

    total = 0
    for idx, char in enumerate(cleaned[:14]):
        val = GSTIN_CHAR_MAP.get(char)
        if val is None:
            return None
        weight = 1 if (idx % 2 == 0) else 2
        product = val * weight
        quotient = product // 36
        remainder = product % 36
        total += quotient + remainder

    check_val = (36 - (total % 36)) % 36
    return GSTIN_ALPHABET[check_val]


def verify_gstin_detailed(gstin: Optional[str]) -> Dict[str, Any]:
    """
    Performs exhaustive statutory validation on an Indian GSTIN:
    1. Format & Structure (15 chars, alphanumeric pattern)
    2. Valid State Jurisdiction Code (01-38, 97, 99)
    3. Embedded PAN structure & entity classification (chars 3 to 12)
    4. Official statutory Modulo 36 checksum calculation

    Returns standardized dictionary with explicit display and portal status:
    - display_status: 'GSTIN Structure Verified', 'GSTIN Checksum Invalid', 'GSTIN Format Invalid', or ''
    - portal_status: 'GSTIN Registration Not Verified' (never claims portal verification without authorized GSP)
    """
    if not gstin or not str(gstin).strip():
        return {
            'is_valid': False,
            'clean_gstin': '',
            'status_code': 'EMPTY',
            'display_status': '',
            'portal_status': 'GSTIN Registration Not Verified',
            'error_message': None,
            'state_code': '',
            'pan': '',
            'is_valid_format': False,
            'is_valid_state': False,
            'is_valid_pan': False,
            'is_valid_checksum': False,
            'expected_checksum': None,
            'actual_checksum': None,
        }

    cleaned = clean_gstin(gstin)

    # 1. Structure length and alphanumeric check
    if len(cleaned) != 15 or not GSTIN_REGEX.match(cleaned):
        return {
            'is_valid': False,
            'clean_gstin': cleaned,
            'status_code': 'FORMAT_INVALID',
            'display_status': 'GSTIN Format Invalid',
            'portal_status': 'GSTIN Registration Not Verified',
            'error_message': 'GSTIN must be exactly 15 alphanumeric characters matching statutory format.',
            'state_code': cleaned[:2] if len(cleaned) >= 2 else '',
            'pan': cleaned[2:12] if len(cleaned) >= 12 else '',
            'is_valid_format': False,
            'is_valid_state': False,
            'is_valid_pan': False,
            'is_valid_checksum': False,
            'expected_checksum': None,
            'actual_checksum': cleaned[-1] if cleaned else None,
        }

    state_code = cleaned[:2]
    pan = cleaned[2:12]
    actual_checksum = cleaned[14]

    # 2. State Code validity
    is_valid_state = state_code in VALID_GST_STATE_CODES
    if not is_valid_state:
        return {
            'is_valid': False,
            'clean_gstin': cleaned,
            'status_code': 'FORMAT_INVALID',
            'display_status': 'GSTIN Format Invalid',
            'portal_status': 'GSTIN Registration Not Verified',
            'error_message': f"State code '{state_code}' is not a valid Indian GST jurisdiction code (01-38, 97, 99).",
            'state_code': state_code,
            'pan': pan,
            'is_valid_format': True,
            'is_valid_state': False,
            'is_valid_pan': True,
            'is_valid_checksum': False,
            'expected_checksum': None,
            'actual_checksum': actual_checksum,
        }

    # 3. PAN Entity validation
    pan_entity = pan[3] if len(pan) >= 4 else ''
    is_valid_pan = bool(PAN_REGEX.match(pan)) and (pan_entity in PAN_ENTITY_TYPES)
    if not is_valid_pan:
        return {
            'is_valid': False,
            'clean_gstin': cleaned,
            'status_code': 'FORMAT_INVALID',
            'display_status': 'GSTIN Format Invalid',
            'portal_status': 'GSTIN Registration Not Verified',
            'error_message': f"Embedded PAN '{pan}' has invalid entity type '{pan_entity}'. Expected one of {sorted(PAN_ENTITY_TYPES)}.",
            'state_code': state_code,
            'pan': pan,
            'is_valid_format': False,
            'is_valid_state': True,
            'is_valid_pan': False,
            'is_valid_checksum': False,
            'expected_checksum': None,
            'actual_checksum': actual_checksum,
        }

    # 4. Modulo 36 Checksum verification
    expected_checksum = calculate_gstin_checksum(cleaned[:14])
    is_checksum_valid = (actual_checksum == expected_checksum)

    if not is_checksum_valid:
        return {
            'is_valid': False,
            'clean_gstin': cleaned,
            'status_code': 'CHECKSUM_INVALID',
            'display_status': 'GSTIN Checksum Invalid',
            'portal_status': 'GSTIN Registration Not Verified',
            'error_message': f"Modulo 36 checksum mismatch: expected '{expected_checksum}', got '{actual_checksum}'. Optical OCR error suspected.",
            'state_code': state_code,
            'pan': pan,
            'is_valid_format': True,
            'is_valid_state': True,
            'is_valid_pan': is_valid_pan,
            'is_valid_checksum': False,
            'expected_checksum': expected_checksum,
            'actual_checksum': actual_checksum,
        }

    return {
        'is_valid': True,
        'clean_gstin': cleaned,
        'status_code': 'STRUCTURE_VERIFIED',
        'display_status': 'GSTIN Structure Verified',
        'portal_status': 'GSTIN Registration Not Verified',
        'error_message': None,
        'state_code': state_code,
        'pan': pan,
        'is_valid_format': True,
        'is_valid_state': True,
        'is_valid_pan': is_valid_pan,
        'is_valid_checksum': True,
        'expected_checksum': expected_checksum,
        'actual_checksum': actual_checksum,
    }


def clean_gstin(gstin: Optional[str]) -> str:
    """Sanitizes and normalizes GSTIN string."""
    if not gstin:
        return ''
    cleaned = re.sub(r'[^0-9A-Za-z]', '', str(gstin)).upper().strip()
    return cleaned if len(cleaned) == 15 else cleaned


def clean_pan(pan: Optional[str]) -> str:
    """Sanitizes and normalizes PAN string."""
    if not pan:
        return ''
    cleaned = re.sub(r'[^0-9A-Za-z]', '', str(pan)).upper().strip()
    return cleaned if len(cleaned) == 10 else cleaned


def is_valid_gstin(gstin: Optional[str], check_checksum: bool = True) -> bool:
    """
    Validates statutory Indian GSTIN.
    If check_checksum is True (default), verifies format, state code, PAN, and Mod-36 checksum.
    If check_checksum is False, verifies format and statutory pattern only.
    """
    if not gstin:
        return False
    details = verify_gstin_detailed(gstin)
    if check_checksum:
        return details['is_valid']
    return details['is_valid_format'] and details['is_valid_state']


def extract_pan_from_gstin(gstin: str) -> str:
    """Extracts 10-character PAN embedded within statutory GSTIN (chars 3 to 12)."""
    cleaned = clean_gstin(gstin)
    if is_valid_gstin(cleaned, check_checksum=False):
        return cleaned[2:12]
    return ''


def normalize_vendor_name(name: str) -> str:
    """Normalizes vendor business name to alphanumeric lowercase for fuzzy comparison."""
    if not name:
        return ''
    return re.sub(r'[^a-z0-9]', '', str(name).lower())


def validate_vendor_and_financials(
    party_name: Optional[str],
    party_gstin: Optional[str],
    default_expense_ledger: str = 'Purchase Spares & Consumables',
    pan: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Core Backend Financial Safety Gate (Rules A through E).
    Gemini or OCR suggestions MUST pass through this independent validator.

    Rules:
    A. Known vendor with matching GSTIN:
       - Matched and verified.
       - Eligible for normal processing / approval.

    B. Known vendor without GSTIN on voucher:
       - Matches registered vendor name, but voucher lacks GSTIN.
       - Requires manual verification per accounting policy before financial approval.

    C. Unknown vendor without GSTIN:
       - Unknown entity with no GSTIN (e.g. roadside repair slip).
       - Flagged as ambiguous vendor.
       - Requires manual review. Automatic financial approval blocked.

    D. Unknown vendor with valid GSTIN:
       - Statutory GSTIN is valid, but vendor is not in master directory.
       - Marked as new candidate unregistered vendor requiring verification.

    E. GSTIN Mismatch:
       - Bill shows GSTIN G_A, but registered vendor has GSTIN G_B.
       - Flagged as mismatch. Registered GSTIN is never silently replaced.
    """
    from .vendor_directory import (
        find_vendor_by_gstin,
        find_vendor_by_name,
        find_vendor_by_pan,
    )

    cleaned_name = (party_name or '').strip()
    cleaned_gstin_str = clean_gstin(party_gstin)
    gstin_details = verify_gstin_detailed(cleaned_gstin_str) if cleaned_gstin_str else None
    gstin_valid = gstin_details['is_valid'] if gstin_details else False
    derived_pan = clean_pan(pan) or extract_pan_from_gstin(cleaned_gstin_str)

    # 1. Lookups against Balaji Authoritative VendorMaster DB
    vendor_by_gstin = find_vendor_by_gstin(cleaned_gstin_str) if cleaned_gstin_str else None
    vendor_by_name = find_vendor_by_name(cleaned_name) if cleaned_name else None
    if not vendor_by_name and derived_pan and not cleaned_gstin_str:
        vendor_by_name = find_vendor_by_pan(derived_pan)

    # Base result structure
    result = {
        'party_name': cleaned_name,
        'party_gstin': cleaned_gstin_str,
        'pan': derived_pan,
        'expense_ledger': default_expense_ledger,
        'vendor_match_status': 'UNEVALUATED',
        'is_ambiguous_vendor': False,
        'requires_manual_review': True,
        'confidence': 0.80,
        'is_financial_approval_eligible': False,
        'is_valid_gstin': gstin_valid,
        'gstin_details': gstin_details,
        'gstin_status': gstin_details['display_status'] if gstin_details else '',
        'gstin_portal_status': gstin_details['portal_status'] if gstin_details else '',
        'audit_notes': '',
    }

    # Case A: Known vendor with matching GSTIN
    if vendor_by_gstin:
        # Check if name also matches or if GSTIN is registered to this vendor
        if vendor_by_name:
            norm_gstin_vendor = normalize_vendor_name(vendor_by_gstin.get('name', ''))
            norm_name_vendor = normalize_vendor_name(vendor_by_name.get('name', ''))

            if norm_gstin_vendor != norm_name_vendor and vendor_by_name.get('gstin') != cleaned_gstin_str:
                # Case E: GSTIN Mismatch
                result['vendor_match_status'] = 'GSTIN_MISMATCH'
                result['is_ambiguous_vendor'] = True
                result['requires_manual_review'] = True
                result['confidence'] = 0.60
                result['is_financial_approval_eligible'] = False
                result['audit_notes'] = (
                    f"GSTIN Mismatch: Voucher GSTIN '{cleaned_gstin_str}' belongs to "
                    f"'{vendor_by_gstin.get('name')}', but voucher name matches '{vendor_by_name.get('name')}' "
                    f"(registered GSTIN: '{vendor_by_name.get('gstin')}')."
                )
                return result

        # Normal Case A (preserves existing VendorMaster records even if optical entry mismatch exists)
        result['vendor_match_status'] = 'MATCHED_VERIFIED'
        result['party_name'] = vendor_by_gstin.get('name', cleaned_name)
        result['party_gstin'] = cleaned_gstin_str
        result['expense_ledger'] = vendor_by_gstin.get('ledger', default_expense_ledger)
        result['is_ambiguous_vendor'] = False
        result['requires_manual_review'] = False
        result['confidence'] = 0.98 if gstin_valid else 0.95
        result['is_financial_approval_eligible'] = True
        if gstin_valid:
            result['audit_notes'] = f"Verified: Matched registered vendor '{result['party_name']}' via GSTIN."
        else:
            result['audit_notes'] = (
                f"Verified: Matched registered vendor '{result['party_name']}' via GSTIN. "
                f"Note: Master record GSTIN has checksum discrepancy ({gstin_details.get('error_message') if gstin_details else ''}); manual record review advised."
            )
        return result

    # Case E check: Name matches a registered vendor, but voucher provides a different GSTIN
    if vendor_by_name and cleaned_gstin_str:
        registered_gstin = vendor_by_name.get('gstin', '')
        if registered_gstin and registered_gstin != cleaned_gstin_str:
            result['vendor_match_status'] = 'GSTIN_MISMATCH'
            result['is_ambiguous_vendor'] = True
            result['requires_manual_review'] = True
            result['confidence'] = 0.60
            result['is_financial_approval_eligible'] = False
            result['audit_notes'] = (
                f"GSTIN Mismatch: Vendor '{vendor_by_name.get('name')}' is registered with GSTIN "
                f"'{registered_gstin}', but voucher has '{cleaned_gstin_str}'."
            )
            return result

    # Case B: Known vendor without GSTIN on voucher
    if vendor_by_name and not cleaned_gstin_str:
        result['vendor_match_status'] = 'KNOWN_VENDOR_NO_GSTIN'
        result['party_name'] = vendor_by_name.get('name', cleaned_name)
        result['party_gstin'] = vendor_by_name.get('gstin', '')
        result['expense_ledger'] = vendor_by_name.get('ledger', default_expense_ledger)
        result['is_ambiguous_vendor'] = False
        result['requires_manual_review'] = True  # Accounting policy: GSTIN missing on paper requires check
        result['confidence'] = 0.85
        result['is_financial_approval_eligible'] = False
        result['audit_notes'] = (
            f"Voucher lacks GSTIN. Matched registered vendor '{result['party_name']}' by name. "
            f"Registered GSTIN '{result['party_gstin']}' suggested for operator verification."
        )
        return result

    # Case D: Unknown vendor with GSTIN
    if cleaned_gstin_str:
        if gstin_valid:
            result['vendor_match_status'] = 'UNREGISTERED_VENDOR_VALID_GSTIN'
            result['is_ambiguous_vendor'] = False
            result['requires_manual_review'] = True
            result['confidence'] = 0.90
            result['is_financial_approval_eligible'] = False
            result['audit_notes'] = (
                f"New vendor with valid statutory GSTIN '{cleaned_gstin_str}' (State code {cleaned_gstin_str[:2]}). "
                f"Requires accounts operator verification before ledger creation."
            )
        elif gstin_details and gstin_details.get('status_code') == 'CHECKSUM_INVALID':
            result['vendor_match_status'] = 'UNREGISTERED_VENDOR_CHECKSUM_INVALID'
            result['is_ambiguous_vendor'] = True
            result['requires_manual_review'] = True
            result['confidence'] = 0.65
            result['is_financial_approval_eligible'] = False
            result['audit_notes'] = (
                f"Unregistered vendor GSTIN '{cleaned_gstin_str}' has invalid Mod-36 checksum "
                f"(expected '{gstin_details.get('expected_checksum')}', got '{gstin_details.get('actual_checksum')}'). Flagged for manual review."
            )
        else:
            result['vendor_match_status'] = 'INVALID_GSTIN_FORMAT'
            result['is_ambiguous_vendor'] = True
            result['requires_manual_review'] = True
            result['confidence'] = 0.50
            result['is_financial_approval_eligible'] = False
            result['audit_notes'] = f"Invalid GSTIN format '{cleaned_gstin_str}'. Flagged for manual review."
        return result

    # Case C: Unknown vendor without GSTIN
    result['vendor_match_status'] = 'UNKNOWN_VENDOR_NO_GSTIN'
    result['is_ambiguous_vendor'] = True
    result['requires_manual_review'] = True
    result['confidence'] = 0.70  # Explicitly <= 0.85 per financial safety policy
    result['is_financial_approval_eligible'] = False
    result['audit_notes'] = (
        "Unknown vendor with no GSTIN on voucher. Flagged as ambiguous vendor. "
        "Manual accounts review strictly required before financial approval."
    )
    return result


def apply_vendor_validation_to_submission(submission, raw_confidence=None) -> Dict[str, Any]:
    """
    Applies backend vendor & GSTIN validation rules (A-E) to an InvoiceSubmission instance
    without saving yet, ensuring vendor identity and review flags cannot be bypassed.
    """
    default_ledger = submission.expense_ledger or 'Purchase Spares & Consumables'
    backend_val = validate_vendor_and_financials(
        party_name=submission.party_name,
        party_gstin=submission.party_gstin,
        default_expense_ledger=default_ledger,
    )

    if backend_val.get('party_name'):
        submission.party_name = backend_val['party_name']
    if backend_val.get('party_gstin'):
        submission.party_gstin = backend_val['party_gstin']
    if backend_val.get('expense_ledger'):
        submission.expense_ledger = backend_val['expense_ledger']

    submission.is_ambiguous_vendor = backend_val['is_ambiguous_vendor']
    submission.requires_manual_review = backend_val['requires_manual_review']

    if raw_confidence is not None:
        submission.ocr_confidence = min(float(raw_confidence), float(backend_val['confidence']))
    elif not submission.ocr_confidence:
        submission.ocr_confidence = float(backend_val['confidence'])

    return backend_val

