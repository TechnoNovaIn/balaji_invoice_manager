import pymupdf as fitz
from typing import Dict, Any, List


class PageClassificationType:
    DIGITAL_TEXT = 'digital'
    SCANNED_IMAGE = 'scanned'
    MIXED = 'mixed'
    BLANK = 'blank'
    LOW_QUALITY = 'unreadable'


def classify_pdf_page(page: fitz.Page) -> Dict[str, Any]:
    """
    Intelligent page-level classification using PyMuPDF:
    Inspects selectable text, image presence, and raster dimensions without expensive rendering.

    Classifications:
    - DIGITAL_TEXT: Sufficient selectable text (>= 25 characters, >= 4 words) and no dominant full-page scan.
    - SCANNED_IMAGE: Negligible selectable text (< 25 characters) but contains raster scan image.
    - MIXED: Both selectable text (>= 25 characters) AND embedded raster image.
    - BLANK: Zero or whitespace-only text with no images (or empty/trivial image).
    - LOW_QUALITY: Scanned raster image is corrupt, low resolution (< 72 DPI or < 150px), or unreadable.
    """
    text = page.get_text()
    stripped_text = text.strip() if text else ""
    text_len = len(stripped_text)
    words = stripped_text.split()
    word_count = len(words)

    images = page.get_images()
    image_count = len(images)
    rect = page.rect
    page_width = rect.width
    page_height = rect.height

    # 1. Blank page detection
    if text_len == 0 and image_count == 0:
        return {
            'page_type': PageClassificationType.BLANK,
            'readable_label': 'Blank page',
            'text_len': 0,
            'word_count': 0,
            'image_count': 0,
            'has_sufficient_text': False,
            'is_scanned': False,
            'is_digital': False,
            'requires_ocr': False,
            'requires_gemini': False,
        }

    # Inspect images
    has_substantial_scan = False
    is_low_quality = False
    max_img_w = 0
    max_img_h = 0

    if image_count > 0:
        for img_info in images:
            # img_info tuple: (xref, smask, width, height, bpc, colorspace, ...)
            w = img_info[2]
            h = img_info[3]
            if w > max_img_w:
                max_img_w = w
            if h > max_img_h:
                max_img_h = h

            if w >= 250 and h >= 250:
                has_substantial_scan = True

            # Check unreadable/low quality: tiny image with no digital text
            if (w < 150 or h < 150) and text_len < 25:
                is_low_quality = True

    has_sufficient_text = (text_len >= 25 and word_count >= 4)

    # 2. Low Quality check
    if is_low_quality and not has_substantial_scan and not has_sufficient_text:
        return {
            'page_type': PageClassificationType.LOW_QUALITY,
            'readable_label': 'Low-quality or unreadable page',
            'text_len': text_len,
            'word_count': word_count,
            'image_count': image_count,
            'has_sufficient_text': False,
            'is_scanned': True,
            'is_digital': False,
            'requires_ocr': True,
            'requires_gemini': True,
        }

    # 3. Classify Page Type
    if has_sufficient_text and not has_substantial_scan:
        page_type = PageClassificationType.DIGITAL_TEXT
        readable_label = 'Digital text page'
        requires_ocr = False
        requires_gemini = False
    elif has_substantial_scan and not has_sufficient_text:
        page_type = PageClassificationType.SCANNED_IMAGE
        readable_label = 'Scanned image page'
        requires_ocr = True
        requires_gemini = False  # Only if OCR fails or handwriting/stamps detected
    elif has_sufficient_text and has_substantial_scan:
        page_type = PageClassificationType.MIXED
        readable_label = 'Mixed text and image page'
        requires_ocr = True  # Conditional/supplemental OCR
        requires_gemini = False
    else:
        # Fallback
        if image_count > 0:
            page_type = PageClassificationType.SCANNED_IMAGE
            readable_label = 'Scanned image page'
            requires_ocr = True
            requires_gemini = False
        else:
            page_type = PageClassificationType.BLANK
            readable_label = 'Blank page'
            requires_ocr = False
            requires_gemini = False

    return {
        'page_type': page_type,
        'readable_label': readable_label,
        'text_len': text_len,
        'word_count': word_count,
        'image_count': image_count,
        'max_img_dim': (max_img_w, max_img_h),
        'has_sufficient_text': has_sufficient_text,
        'is_scanned': page_type in (PageClassificationType.SCANNED_IMAGE, PageClassificationType.MIXED),
        'is_digital': page_type in (PageClassificationType.DIGITAL_TEXT, PageClassificationType.MIXED),
        'requires_ocr': requires_ocr,
        'requires_gemini': requires_gemini,
    }


def classify_document_pages(doc: fitz.Document) -> List[Dict[str, Any]]:
    """
    Classifies all pages in a PyMuPDF document quickly.
    Returns a list of page classification records (1-indexed page_num).
    """
    classifications = []
    for idx, page in enumerate(doc):
        c = classify_pdf_page(page)
        c['page_num'] = idx + 1
        c['page_index'] = idx
        classifications.append(c)
    return classifications
