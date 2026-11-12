"""
Document Appearance Classification (HANDWRITTEN / PRINTED / MIXED / NEEDS_REVIEW).

Strictly independent from GST E-Invoice status (`is_e_invoice`, `irn_number`):
- Appearance never implies statutory GST e-invoice applicability.
- Uses existing PyMuPDF, OpenCV, Pillow, RapidOCR, and optional Gemini Vision data.
- Falls back conservatively to NEEDS_REVIEW when evidence or image quality is insufficient.
"""

import logging
import os
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DOC_TYPE_HANDWRITTEN = 'HANDWRITTEN'
DOC_TYPE_PRINTED = 'PRINTED'
DOC_TYPE_MIXED = 'MIXED'
DOC_TYPE_NEEDS_REVIEW = 'NEEDS_REVIEW'

VALID_DOCUMENT_TYPES = {
    DOC_TYPE_HANDWRITTEN,
    DOC_TYPE_PRINTED,
    DOC_TYPE_MIXED,
    DOC_TYPE_NEEDS_REVIEW,
}

DOCUMENT_TYPE_LABELS = {
    DOC_TYPE_HANDWRITTEN: 'HANDWRITTEN',
    DOC_TYPE_PRINTED: 'PRINTED',
    DOC_TYPE_MIXED: 'MIXED',
    DOC_TYPE_NEEDS_REVIEW: 'NEEDS REVIEW',
}


def normalize_document_type(value: Optional[str]) -> Optional[str]:
    """
    Normalize a candidate document type string to one of the 4 canonical values.
    Returns None if empty or unrecognized.
    """
    if not value:
        return None
    cleaned = re.sub(r'[\s\-]+', '_', str(value).strip().upper())
    aliases = {
        'HANDWRITTEN': DOC_TYPE_HANDWRITTEN,
        'HAND_WRITTEN': DOC_TYPE_HANDWRITTEN,
        'MANUAL': DOC_TYPE_HANDWRITTEN,
        'PRINTED': DOC_TYPE_PRINTED,
        'COMPUTER_PRINTED': DOC_TYPE_PRINTED,
        'MACHINE_PRINTED': DOC_TYPE_PRINTED,
        'DIGITAL': DOC_TYPE_PRINTED,
        'MIXED': DOC_TYPE_MIXED,
        'HYBRID': DOC_TYPE_MIXED,
        'NEEDS_REVIEW': DOC_TYPE_NEEDS_REVIEW,
        'NEED_REVIEW': DOC_TYPE_NEEDS_REVIEW,
        'NEEDS_VERIFICATION': DOC_TYPE_NEEDS_REVIEW,
        'UNCERTAIN': DOC_TYPE_NEEDS_REVIEW,
        'UNKNOWN': DOC_TYPE_NEEDS_REVIEW,
    }
    return aliases.get(cleaned)


def parse_document_type_choice(value: Any, default: str = DOC_TYPE_NEEDS_REVIEW) -> Optional[str]:
    """Parse strict user-submitted document_type value; returns None if invalid."""
    normalized = normalize_document_type(str(value or ''))
    if normalized in VALID_DOCUMENT_TYPES:
        return normalized
    if not str(value or '').strip():
        return default
    return None


def format_document_type_label(doc_type: Optional[str]) -> str:
    """Return human/audit display string ('HANDWRITTEN', 'PRINTED', 'MIXED', 'NEEDS REVIEW')."""
    return DOCUMENT_TYPE_LABELS.get(doc_type or DOC_TYPE_NEEDS_REVIEW, 'NEEDS REVIEW')


def _analyze_cv_image_appearance(
    img_bgr,
    ocr_res: Optional[List[Any]] = None,
    raw_text: str = '',
) -> Dict[str, Any]:
    """
    Inspect visual stroke geometry, printed table/grid structure, and OCR signals
    on an image array using OpenCV + numpy (offline, deterministic).
    """
    out: Dict[str, Any] = {
        'document_type': DOC_TYPE_NEEDS_REVIEW,
        'confidence': 0.5,
        'is_unreadable': False,
        'has_handwritten_strokes': False,
        'has_printed_structure': False,
        'reason': 'Insufficient visual evidence.',
    }

    if img_bgr is None:
        out['is_unreadable'] = True
        out['reason'] = 'Image could not be decoded.'
        return out

    try:
        import cv2
        import numpy as np
    except ImportError:
        return out

    h, w = img_bgr.shape[:2]
    if min(h, w) < 120:
        out['is_unreadable'] = True
        out['reason'] = f'Image resolution too low ({w}x{h}).'
        return out

    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY) if len(img_bgr.shape) == 3 else img_bgr
    if float(np.std(gray)) < 8.0:
        out['is_unreadable'] = True
        out['reason'] = 'Blank or nearly uniform image.'
        return out

    # Sharpness & foreground ink check
    lap_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    ink_ratio = float(np.count_nonzero(binary)) / float(h * w)

    if ink_ratio < 0.0012 or ink_ratio > 0.70:
        out['is_unreadable'] = True
        out['reason'] = 'Extreme ink density or unreadable scan.'
        return out

    # Detect crisp horizontal & vertical printed lines/boxes (bill books & laser invoices)
    horiz_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(w // 12, 25), 1))
    vert_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(h // 15, 25)))
    horiz_lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN, horiz_kernel)
    vert_lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN, vert_kernel)
    line_pixels = np.count_nonzero(horiz_lines) + np.count_nonzero(vert_lines)
    has_grid_lines = (line_pixels / float(h * w)) > 0.0015

    # Connected component glyph analysis (height uniformity, aspect variance, stroke thickness)
    text_mask = cv2.subtract(binary, cv2.bitwise_or(horiz_lines, vert_lines))
    num_labels, _, stats, _ = cv2.connectedComponentsWithStats(text_mask, connectivity=8)

    glyph_heights: List[int] = []
    glyph_widths: List[int] = []
    large_cursive_blobs = 0
    for idx in range(1, num_labels):
        bw = int(stats[idx, cv2.CC_STAT_WIDTH])
        bh = int(stats[idx, cv2.CC_STAT_HEIGHT])
        area = int(stats[idx, cv2.CC_STAT_AREA])
        if area < 8 or bh < 5:
            continue
        if bh <= 140 and bw <= int(w * 0.95):
            glyph_heights.append(bh)
            glyph_widths.append(bw)
        if (bw > 45 and bh > 20) or (bh > 32 and bw / max(bh, 1) > 1.8):
            large_cursive_blobs += 1

    # OCR signals (if provided by caller)
    ocr_scores: List[float] = []
    ocr_text_parts: List[str] = []
    top_scores: List[float] = []
    body_scores: List[float] = []
    if ocr_res:
        for item in ocr_res:
            try:
                box, txt, score = item[0], str(item[1] or '').strip(), float(item[2])
                if not txt:
                    continue
                ocr_scores.append(score)
                ocr_text_parts.append(txt)
                y_center = sum(pt[1] for pt in box) / 4.0 if box else h * 0.5
                if y_center <= h * 0.35:
                    top_scores.append(score)
                else:
                    body_scores.append(score)
            except Exception:
                continue

    text_str = raw_text.strip() if raw_text else " ".join(ocr_text_parts).strip()
    text_len = len(text_str)
    mean_ocr = sum(ocr_scores) / len(ocr_scores) if ocr_scores else 0.0
    high_conf_ratio = (
        sum(1 for s in ocr_scores if s >= 0.85) / len(ocr_scores)
        if ocr_scores else 0.0
    )

    if (len(glyph_heights) + large_cursive_blobs) < 4 and text_len < 10:
        if lap_var < 20.0:
            out['is_unreadable'] = True
        out['reason'] = 'Too few legible glyphs/words to classify reliably.'
        return out

    height_std = float(np.std(glyph_heights)) if glyph_heights else 0.0
    height_mean = float(np.mean(glyph_heights)) if glyph_heights else 1.0
    height_cv = height_std / max(height_mean, 1.0)

    # Row-level vertical separation regularity (printed text has crisp zero-ink gutters)
    row_density = np.count_nonzero(text_mask, axis=1) / float(w)
    gutter_ratio = float(np.count_nonzero(row_density < 0.002)) / float(h)

    printed_score = 0.0
    handwritten_score = 0.0

    if ocr_scores:
        if mean_ocr >= 0.87 and high_conf_ratio >= 0.70:
            printed_score += 2.2
        elif mean_ocr >= 0.76:
            printed_score += 1.0
        elif mean_ocr < 0.65:
            handwritten_score += 1.8

    if height_cv <= 0.38 and len(glyph_heights) >= 20:
        printed_score += 1.5
    elif height_cv >= 0.52 or large_cursive_blobs >= 3:
        handwritten_score += 1.7

    if gutter_ratio >= 0.55 and text_len >= 40:
        printed_score += 0.8

    if has_grid_lines:
        # Printed invoices and pre-printed handwritten bill books both frequently have grid borders
        out['has_printed_structure'] = True

    # Explicit bimodal check: strong printed header (or printed grid) + weak/cursive body
    bimodal_mixed = False
    if top_scores and body_scores:
        if (sum(top_scores) / len(top_scores)) >= 0.85 and (sum(body_scores) / len(body_scores)) < 0.72:
            bimodal_mixed = True
    if out['has_printed_structure'] and (large_cursive_blobs >= 2 or (ocr_scores and high_conf_ratio < 0.75 and any(s >= 0.85 for s in ocr_scores))):
        bimodal_mixed = True

    if bimodal_mixed or (printed_score >= 1.4 and handwritten_score >= 1.4):
        out['document_type'] = DOC_TYPE_MIXED
        out['has_handwritten_strokes'] = True
        out['has_printed_structure'] = True
        out['confidence'] = 0.82
        out['reason'] = 'Detected printed layout/header elements combined with handwritten entries.'
        return out

    if printed_score >= 2.2 and handwritten_score < 1.0:
        out['document_type'] = DOC_TYPE_PRINTED
        out['has_printed_structure'] = True
        out['confidence'] = min(0.95, 0.70 + printed_score * 0.07)
        out['reason'] = 'Uniform glyph geometry and high OCR legibility indicate machine-printed document.'
        return out

    if handwritten_score >= 1.8 and printed_score < 1.2:
        out['document_type'] = DOC_TYPE_HANDWRITTEN
        out['has_handwritten_strokes'] = True
        out['confidence'] = min(0.90, 0.68 + handwritten_score * 0.07)
        out['reason'] = 'Irregular stroke/glyph geometry and low machine-print uniformity indicate handwritten document.'
        return out

    # Uncertain -> conservative NEEDS_REVIEW
    out['document_type'] = DOC_TYPE_NEEDS_REVIEW
    out['confidence'] = 0.45
    out['reason'] = 'Document appearance signals are ambiguous; flagged for manual review.'
    return out


def classify_document_type(
    file_path: str = '',
    raw_text: str = '',
    page_classification: Optional[Dict[str, Any]] = None,
    page_indices: Optional[List[int]] = None,
    ocr_res: Optional[List[Any]] = None,
    gemini_data: Optional[Dict[str, Any]] = None,
    content_type: str = '',
) -> Dict[str, Any]:
    """
    Classify an uploaded invoice or PDF segment into one of:
    - HANDWRITTEN
    - PRINTED
    - MIXED
    - NEEDS_REVIEW

    Uses PyMuPDF, OpenCV/Pillow, RapidOCR signals, and validated Gemini Vision output.
    """
    gemini_type = normalize_document_type((gemini_data or {}).get('document_type'))
    has_handwritten_amounts = bool((gemini_data or {}).get('has_handwritten_amounts'))

    # 1. PyMuPDF Page Classification check for PDF documents
    is_pdf = (content_type == 'application/pdf') or (bool(file_path) and file_path.lower().endswith('.pdf'))
    if is_pdf and file_path and os.path.exists(file_path):
        try:
            import pymupdf as fitz
            from .page_classifier import classify_pdf_page, PageClassificationType

            doc = fitz.open(file_path)
            target_pages = page_indices if page_indices is not None else [0]
            first_idx = target_pages[0] if target_pages else 0
            if 0 <= first_idx < len(doc):
                p_info = page_classification or classify_pdf_page(doc[first_idx])
                p_type = p_info.get('page_type')

                if p_type == PageClassificationType.DIGITAL_TEXT:
                    doc.close()
                    # Respect MIXED if Gemini visually identified handwritten annotations on digital form
                    if gemini_type == DOC_TYPE_MIXED or has_handwritten_amounts:
                        return {
                            'document_type': DOC_TYPE_MIXED,
                            'confidence': 0.88,
                            'reason': 'Digital PDF text combined with handwritten amounts/entries.',
                        }
                    return {
                        'document_type': DOC_TYPE_PRINTED,
                        'confidence': 0.96,
                        'reason': 'Pure digital selectable PDF text indicates computer-generated printed document.',
                    }

                if p_type in (PageClassificationType.BLANK, PageClassificationType.LOW_QUALITY):
                    doc.close()
                    return {
                        'document_type': DOC_TYPE_NEEDS_REVIEW,
                        'confidence': 0.30,
                        'reason': 'Low-quality or blank PDF page requires manual document-type review.',
                    }

                # SCANNED_IMAGE or MIXED PDF page -> render page for OpenCV/PIL inspection
                try:
                    import cv2
                    import numpy as np
                    pix = doc[first_idx].get_pixmap(dpi=150)
                    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
                    if pix.n == 4:
                        img_bgr = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
                    elif pix.n == 3:
                        img_bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
                    else:
                        img_bgr = img
                    doc.close()

                    visual = _analyze_cv_image_appearance(img_bgr, ocr_res=ocr_res, raw_text=raw_text)
                    if visual['is_unreadable']:
                        return {
                            'document_type': DOC_TYPE_NEEDS_REVIEW,
                            'confidence': 0.30,
                            'reason': visual['reason'],
                        }
                    if gemini_type and gemini_type != DOC_TYPE_NEEDS_REVIEW:
                        resolved = DOC_TYPE_MIXED if (gemini_type == DOC_TYPE_PRINTED and has_handwritten_amounts) else gemini_type
                        return {
                            'document_type': resolved,
                            'confidence': 0.90,
                            'reason': 'Classified via Gemini Vision AI corroborated with readable raster scan.',
                        }
                    if p_type == PageClassificationType.MIXED and visual['document_type'] == DOC_TYPE_HANDWRITTEN:
                        return {
                            'document_type': DOC_TYPE_MIXED,
                            'confidence': 0.84,
                            'reason': 'Digital PDF text combined with handwritten raster image content.',
                        }
                    return {
                        'document_type': visual['document_type'],
                        'confidence': visual['confidence'],
                        'reason': visual['reason'],
                    }
                except Exception:
                    doc.close()
            else:
                doc.close()
        except Exception as exc:
            logger.debug("PDF document appearance inspection skipped softly: %s", exc)

    # 2. Direct Image Inspection (OpenCV + Pillow + RapidOCR signals)
    if file_path and os.path.exists(file_path) and not is_pdf:
        try:
            import cv2
            img_bgr = cv2.imread(file_path)
            if img_bgr is None:
                # Try Pillow fallback (handles some WEBP/EXIF images cleanly)
                from PIL import Image
                import numpy as np
                Image.MAX_IMAGE_PIXELS = 40_000_000
                with Image.open(file_path) as pil_img:
                    rgb = pil_img.convert('RGB')
                    img_bgr = cv2.cvtColor(np.array(rgb), cv2.COLOR_RGB2BGR)

            visual = _analyze_cv_image_appearance(img_bgr, ocr_res=ocr_res, raw_text=raw_text)
            if visual['is_unreadable']:
                return {
                    'document_type': DOC_TYPE_NEEDS_REVIEW,
                    'confidence': 0.30,
                    'reason': visual['reason'],
                }
            if gemini_type and gemini_type != DOC_TYPE_NEEDS_REVIEW:
                resolved = DOC_TYPE_MIXED if (gemini_type == DOC_TYPE_PRINTED and has_handwritten_amounts) else gemini_type
                return {
                    'document_type': resolved,
                    'confidence': 0.90,
                    'reason': 'Classified via Gemini Vision AI with readable image verification.',
                }
            return {
                'document_type': visual['document_type'],
                'confidence': visual['confidence'],
                'reason': visual['reason'],
            }
        except Exception as exc:
            logger.debug("Image document appearance inspection failed softly: %s", exc)
            return {
                'document_type': DOC_TYPE_NEEDS_REVIEW,
                'confidence': 0.30,
                'reason': 'Could not inspect image reliably; flagged for review.',
            }

    # 3. Gemini Vision fallback when file inspection wasn't applicable
    if gemini_type:
        resolved = DOC_TYPE_MIXED if (gemini_type == DOC_TYPE_PRINTED and has_handwritten_amounts) else gemini_type
        return {
            'document_type': resolved,
            'confidence': 0.85,
            'reason': 'Classified from Gemini Vision inspection.',
        }

    return {
        'document_type': DOC_TYPE_NEEDS_REVIEW,
        'confidence': 0.40,
        'reason': 'Insufficient evidence for automatic document type classification.',
    }


def audit_submission_financials(submission_or_meta: Any) -> list[str]:
    """
    Return explicit, human-readable financial integrity warnings for an invoice record or metadata dict.
    Checks: missing/zero total, Taxable + Taxes != Total, asymmetrical CGST/SGST, and Rupees-in-words mismatch.
    """
    warnings: list[str] = []
    if isinstance(submission_or_meta, dict):
        taxable = Decimal(str(submission_or_meta.get('taxable_amount') or '0.00'))
        cgst = Decimal(str(submission_or_meta.get('cgst_amount') or '0.00'))
        sgst = Decimal(str(submission_or_meta.get('sgst_amount') or '0.00'))
        igst = Decimal(str(submission_or_meta.get('igst_amount') or '0.00'))
        total = Decimal(str(submission_or_meta.get('total_amount') or '0.00'))
        gemini_data = submission_or_meta.get('gemini_extracted_data') or {}
    else:
        taxable = getattr(submission_or_meta, 'taxable_amount', None) or Decimal('0.00')
        cgst = getattr(submission_or_meta, 'cgst_amount', None) or Decimal('0.00')
        sgst = getattr(submission_or_meta, 'sgst_amount', None) or Decimal('0.00')
        igst = getattr(submission_or_meta, 'igst_amount', None) or Decimal('0.00')
        total = getattr(submission_or_meta, 'total_amount', None) or Decimal('0.00')
        gemini_data = getattr(submission_or_meta, 'gemini_extracted_data', None) or {}

    if total <= Decimal('0.00'):
        warnings.append("Total invoice amount is ₹0.00 or could not be extracted.")
        return warnings

    tax_sum = cgst + sgst + igst
    if tax_sum > Decimal('0.00') and taxable > Decimal('0.00'):
        expected = taxable + tax_sum
        if abs(expected - total) > Decimal('2.00'):
            warnings.append(
                f"Arithmetic mismatch: Taxable (₹{taxable:,.2f}) + Taxes (₹{tax_sum:,.2f}) = ₹{expected:,.2f}, "
                f"which differs from Total (₹{total:,.2f})."
            )

    if (cgst > Decimal('0.00')) != (sgst > Decimal('0.00')):
        warnings.append(
            f"Incomplete intra-state GST split: CGST is ₹{cgst:,.2f} while SGST is ₹{sgst:,.2f}."
        )
    elif cgst > Decimal('0.00') and sgst > Decimal('0.00') and abs(cgst - sgst) > Decimal('1.00'):
        warnings.append(
            f"Asymmetrical GST split: CGST (₹{cgst:,.2f}) does not equal SGST (₹{sgst:,.2f})."
        )

    words_raw = (gemini_data or {}).get('amount_in_words')
    if words_raw:
        try:
            from .gemini_extractor import _parse_indian_amount_words
            words_amt = _parse_indian_amount_words(words_raw)
            if words_amt and words_amt > Decimal('0.00'):
                tol = max(Decimal('5.00'), words_amt * Decimal('0.01'))
                if abs(words_amt - total) > tol:
                    warnings.append(
                        f"Words vs Total discrepancy: Bill words '{words_raw}' (≈ ₹{words_amt:,.2f}) "
                        f"differs from Total (₹{total:,.2f}) — verify digits & trailing zeros."
                    )
        except Exception:
            pass

    return warnings


def has_unclear_financial_values(submission_or_meta: Any) -> bool:
    """
    Check if extracted financial amounts are missing, zero, asymmetrical, or arithmetically inconsistent,
    requiring explicit manual verification by Naveen.
    """
    return len(audit_submission_financials(submission_or_meta)) > 0


def apply_document_type_to_submission(submission, metadata: Optional[Dict[str, Any]] = None) -> None:
    """
    Safely apply document_type and handwritten financial review safeguards to an InvoiceSubmission
    WITHOUT modifying `is_e_invoice` or `irn_number`.
    """
    if metadata is not None and 'document_type' in metadata:
        norm = normalize_document_type(metadata.get('document_type'))
        submission.document_type = norm or DOC_TYPE_NEEDS_REVIEW
    elif not getattr(submission, 'document_type', None):
        submission.document_type = DOC_TYPE_NEEDS_REVIEW

    # Enforce handwritten / unclear financial verification safeguards
    if submission.document_type in (DOC_TYPE_HANDWRITTEN, DOC_TYPE_NEEDS_REVIEW):
        submission.requires_manual_review = True
    elif submission.document_type == DOC_TYPE_MIXED and (
        bool((metadata or {}).get('has_handwritten_amounts'))
        or has_unclear_financial_values(submission)
    ):
        submission.requires_manual_review = True
    elif has_unclear_financial_values(submission):
        submission.requires_manual_review = True

