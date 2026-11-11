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


def is_valid_gstin(gstin: str) -> bool:
    """Validates statutory Indian GSTIN 15-character structure."""
    if not gstin:
        return False
    return bool(GSTIN_REGEX.match(clean_gstin(gstin)))


def extract_pan_from_gstin(gstin: str) -> str:
    """Extracts 10-character PAN embedded within statutory GSTIN (chars 3 to 12)."""
    cleaned = clean_gstin(gstin)
    if is_valid_gstin(cleaned):
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
    gstin_valid = is_valid_gstin(cleaned_gstin_str)
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
        'audit_notes': '',
    }

    # Case A: Known vendor with matching GSTIN
    if vendor_by_gstin and gstin_valid:
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

        # Normal Case A
        result['vendor_match_status'] = 'MATCHED_VERIFIED'
        result['party_name'] = vendor_by_gstin.get('name', cleaned_name)
        result['party_gstin'] = cleaned_gstin_str
        result['expense_ledger'] = vendor_by_gstin.get('ledger', default_expense_ledger)
        result['is_ambiguous_vendor'] = False
        result['requires_manual_review'] = False
        result['confidence'] = 0.98
        result['is_financial_approval_eligible'] = True
        result['audit_notes'] = f"Verified: Matched registered vendor '{result['party_name']}' via GSTIN."
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

