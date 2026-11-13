import re
import os
import io
import time
import tempfile
import logging
from typing import List, Dict, Any, Optional
from decimal import Decimal
import pymupdf as fitz  # PyMuPDF
from django.conf import settings

logger = logging.getLogger(__name__)
from .reader import (
    extract_text_from_pdf_page,
    extract_metadata_from_text,
    normalize_handwritten_number,
    clean_karnataka_invoice_no,
    get_shared_ocr_engine,
)
from .vendor_directory import find_vendor_by_name, find_vendor_by_gstin
from .page_classifier import classify_pdf_page, PageClassificationType


def analyze_pdf_page(page, page_num: int, ocr_engine=None, timer=None) -> Dict[str, Any]:
    """
    Analyzes a single PDF page with intelligent page-level classification and conditional extraction:
    - First classifies page as digital text, scanned image, mixed, blank, or low quality.
    - If digital text: extracts selectable text with PyMuPDF directly without running OCR.
    - If scanned image: runs RapidOCR once with the shared engine instance.
    - If mixed: combines selectable text and OCR tokens without duplicating text.
    - If blank: skips OCR entirely.
    page_num is 1-indexed.
    """
    if ocr_engine is None:
        ocr_engine = get_shared_ocr_engine()

    # 1. Page classification
    if timer:
        timer.start_timing('page_classification_ms')
    classification = classify_pdf_page(page)
    if timer:
        timer.stop_timing('page_classification_ms')
        page_type = classification['page_type']
        if page_type == PageClassificationType.DIGITAL_TEXT:
            timer.increment('digital_pages_count')
        elif page_type == PageClassificationType.SCANNED_IMAGE:
            timer.increment('ocr_pages_count')
        elif page_type == PageClassificationType.MIXED:
            timer.increment('mixed_pages_count')
            timer.increment('ocr_pages_count')
        elif page_type == PageClassificationType.BLANK:
            timer.increment('blank_pages_count')

    # 2. Text extraction & conditional OCR
    if timer:
        if classification['requires_ocr']:
            timer.start_timing('ocr_ms')
        else:
            timer.start_timing('text_extraction_ms')

    details = extract_text_from_pdf_page(page, ocr_engine=ocr_engine, return_details=True)

    if timer:
        if classification['requires_ocr']:
            timer.stop_timing('ocr_ms')
        else:
            timer.stop_timing('text_extraction_ms')

    text = details.get('text', '')
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # 3. Structured metadata extraction
    meta = extract_metadata_from_text(text)

    data = {
        'page_num': page_num,
        'page_idx': page_num - 1,
        'classification': classification,
        'page_type': classification['page_type'],
        'raw_text': text,
        'lines': lines,
        'ocr_boxes': details.get('boxes', []),
        'is_upside_down': details.get('is_upside_down', False),
        'invoice_number': meta.get('invoice_number', ''),
        'party_name': meta.get('party_name', ''),
        'party_gstin': meta.get('party_gstin', ''),
        'has_invoice_header': False,
        'has_vendor_header': False,
        'has_total_or_footer': False,
        'is_page_1': False,
        'is_continuation': False,
        'total_amount': meta.get('total_amount', Decimal('0.00')),
    }

    if not text:
        return data

    text_lower = text.lower()

    # 1. Page Number Indicators ("Page 1 of 2", "Page 2 of 2", "Sheet 1", "Contd...")
    if re.search(r'\b(?:page|sheet|pg)\.?\s*([2-9]|\d{2,})\s*(?:of|\/)\s*([2-9]|\d{2,})', text, re.I):
        data['is_continuation'] = True
    elif re.search(r'\b(?:continued\s+on\s+next\s+page|contd\.?|continued\s+from\s+page|carried\s+over|brought\s+forward)\b', text, re.I):
        data['is_continuation'] = True

    # Explicit page 1 markers
    if re.search(r'\b(?:page|sheet|pg)\.?\s*1\s*(?:of|\/)\s*([1-9]|\d{2,})', text, re.I):
        data['is_page_1'] = True
    elif re.search(r'\b(?:original\s+for\s+buyer|duplicate\s+for\s+transporter|triplicate)\b', text, re.I):
        data['is_page_1'] = True

    # 2. Invoice Header Indicator (robust across OCR variants)
    if re.search(r'\b(?:tax\s*invoice|taxinvoice|tax\s*invoicl|iax\s*invoice|iax\s*invoicl|tavuice|iaa\s*iinvuice|aainvuice|\.?ainvuice|taxinvile|nvoice|bill\s*of\s*supply|retail\s*invoice|commercial\s*invoice|cash\s*bill|cash\s*memo|delivery\s*challan)\b', text, re.I):
        data['has_invoice_header'] = True

    # Vendor Header Indicator
    if re.search(r'(?:karnataka\s*tyre|tyre\s*care\s*centre|karnatakatyrecare|swandenahalli|agnibanniraya)', text, re.I):
        data['has_vendor_header'] = True

    # 3. Invoice Total / Footer Indicator
    if re.search(r'\b(?:grand\s*total|grandtotal|g\.?total|net\s*amount|total\s*payable|total\s*amount|amount\s*chargeable|rupees\s*in\s*words|inr\s+[a-z\s]+only|authorised\s*signatory|authorized\s*signatory|for\s+[a-z\s]+(?:ltd|pvt|transports|enterprises|industries|agency|centre))\b', text, re.I):
        data['has_total_or_footer'] = True

    # 4. If invoice number still missing on Karnataka Tyre bill, search existing OCR boxes without re-running OCR
    if not data['invoice_number'] and data['has_vendor_header']:
        boxes = data['ocr_boxes']
        if boxes:
            # Check upper header area boxes for 4-digit number starting with 23
            for b in boxes:
                t = b.get('text', '')
                m = re.search(r'(23\d{2})', t)
                if m:
                    data['invoice_number'] = m.group(1)
                    break
            if not data['invoice_number']:
                for b in boxes:
                    t = b.get('text', '')
                    cand = clean_karnataka_invoice_no(t)
                    if len(cand) == 4 and cand.startswith('23'):
                        data['invoice_number'] = cand
                        break

    return data


def detect_invoice_boundaries(pages_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Segmentation engine: analyzes page sequence and identifies invoice boundaries.
    Groups pages into discrete invoices.
    Returns list of segment descriptors:
    [
        {
            'start_page': int (1-based),
            'end_page': int (1-based),
            'pages': List[int] (0-based indices for fitz),
            'page_data_list': List[Dict],
        },
        ...
    ]
    """
    if not pages_data:
        return []

    if len(pages_data) == 1:
        return [{
            'start_page': 1,
            'end_page': 1,
            'pages': [0],
            'page_data_list': [pages_data[0]]
        }]

    segments = []
    current_segment_pages = [pages_data[0]]

    for i in range(1, len(pages_data)):
        curr = pages_data[i]
        prev = current_segment_pages[-1]

        # Aggregate evidence from current segment so far
        seg_inv_numbers = {p['invoice_number'] for p in current_segment_pages if p['invoice_number']}
        seg_gstins = {p['party_gstin'] for p in current_segment_pages if p['party_gstin']}
        seg_vendors = {p['party_name'] for p in current_segment_pages if p['party_name']}
        seg_has_total = any(p['has_total_or_footer'] for p in current_segment_pages)

        is_new_invoice = False

        # --- Rule 1: Explicit Invoice Number Mismatch ---
        if curr['invoice_number'] and seg_inv_numbers and curr['invoice_number'] not in seg_inv_numbers:
            is_new_invoice = True

        # --- Rule 2: Explicit Supplier GSTIN Mismatch ---
        elif curr['party_gstin'] and seg_gstins and curr['party_gstin'] not in seg_gstins:
            is_new_invoice = True

        # --- Rule 3: Explicit Supplier Name Mismatch ---
        elif curr['party_name'] and seg_vendors and curr['party_name'] not in seg_vendors:
            is_new_invoice = True

        # --- Rule 4: Explicit Continuation Marker overrides header ---
        elif curr['is_continuation']:
            is_new_invoice = False

        # --- Rule 5: Explicit Page 1 Indicator ---
        elif curr['is_page_1']:
            is_new_invoice = True

        # --- Rule 6: Fresh Invoice Header or Vendor Header without continuation marker ---
        elif (curr['has_invoice_header'] or curr.get('has_vendor_header')) and not curr['is_continuation']:
            if seg_has_total or prev['has_total_or_footer'] or prev['has_invoice_header'] or prev.get('has_vendor_header'):
                is_new_invoice = True

        # --- Rule 7: Multi-invoice batch with distinct totals ---
        elif curr['total_amount'] > 0 and any(p['total_amount'] > 0 for p in current_segment_pages) and not curr['is_continuation']:
            is_new_invoice = True

        # If it's a new invoice, close the current segment and start a new one
        if is_new_invoice:
            start_p = current_segment_pages[0]['page_num']
            end_p = current_segment_pages[-1]['page_num']
            segments.append({
                'start_page': start_p,
                'end_page': end_p,
                'pages': [p['page_idx'] for p in current_segment_pages],
                'page_data_list': current_segment_pages
            })
            current_segment_pages = [curr]
        else:
            current_segment_pages.append(curr)

    # Close final segment
    if current_segment_pages:
        start_p = current_segment_pages[0]['page_num']
        end_p = current_segment_pages[-1]['page_num']
        segments.append({
            'start_page': start_p,
            'end_page': end_p,
            'pages': [p['page_idx'] for p in current_segment_pages],
            'page_data_list': current_segment_pages
        })

    return segments


def enhance_preview_image(img_bytes: bytes, is_upside_down: bool = False, fast_mode: bool = True) -> bytes:
    """
    Optimized preview image enhancement:
    - Normalizes orientation and applies clean contrast stretching for crisp portal display.
    - Uses fast OpenCV CLAHE/normalization without slow nested kernel dilation when fast_mode is True.
    """
    try:
        from PIL import Image, ImageOps
        import cv2
        import numpy as np
        import io

        Image.MAX_IMAGE_PIXELS = 40_000_000
        pil_img = Image.open(io.BytesIO(img_bytes))
        pil_img = ImageOps.exif_transpose(pil_img)
        if is_upside_down:
            pil_img = pil_img.rotate(180)
        if pil_img.mode != 'RGB':
            pil_img = pil_img.convert('RGB')

        if fast_mode:
            # Fast, high-contrast visual enhancement: convert to LAB and apply CLAHE directly
            bgr = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
            lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
            l_ch, a_ch, b_ch = cv2.split(lab)
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
            enhanced_l = clahe.apply(l_ch)
            enhanced_lab = cv2.merge([enhanced_l, a_ch, b_ch])
            result_bgr = cv2.cvtColor(enhanced_lab, cv2.COLOR_LAB2BGR)
            out_img = Image.fromarray(cv2.cvtColor(result_bgr, cv2.COLOR_BGR2RGB))
            out_buf = io.BytesIO()
            out_img.save(out_buf, format='JPEG', quality=88, optimize=True)
            return out_buf.getvalue()

        # Full morphology mode
        bgr = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
        lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
        l_ch, a_ch, b_ch = cv2.split(lab)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 25))
        dilated = cv2.dilate(l_ch, kernel)
        bg_illum = cv2.medianBlur(dilated, 21)
        diff = 255 - cv2.absdiff(l_ch, bg_illum)
        norm = cv2.normalize(diff, None, alpha=0, beta=255, norm_type=cv2.NORM_MINMAX, dtype=cv2.CV_8U)

        clahe = cv2.createCLAHE(clipLimit=1.8, tileGridSize=(8, 8))
        enhanced_l = clahe.apply(norm)
        enhanced_lab = cv2.merge([enhanced_l, a_ch, b_ch])
        result_bgr = cv2.cvtColor(enhanced_lab, cv2.COLOR_LAB2BGR)

        gaussian = cv2.GaussianBlur(result_bgr, (0, 0), 1.5)
        sharpened = cv2.addWeighted(result_bgr, 1.2, gaussian, -0.2, 0)

        out_img = Image.fromarray(cv2.cvtColor(sharpened, cv2.COLOR_BGR2RGB))
        out_buf = io.BytesIO()
        out_img.save(out_buf, format='JPEG', quality=90, optimize=True)
        return out_buf.getvalue()
    except Exception:
        return img_bytes


def extract_sub_pdf_and_images(doc, page_indices: List[int], timer=None, pages_data: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """
    Extracts specified pages from PyMuPDF document:
    - Generates a standalone sub-PDF file (bytes) containing just these pages.
    - Renders high-resolution 150 DPI JPEG images for each page with auto-enhancement.
    """
    # 1. Standalone Sub-PDF
    sub_doc = fitz.open()
    for idx in page_indices:
        sub_doc.insert_pdf(doc, from_page=idx, to_page=idx)
    pdf_bytes = sub_doc.write()
    sub_doc.close()

    # 2. Rendered Page Images (JPEG)
    page_images = []
    for idx in page_indices:
        if timer:
            timer.start_timing('image_rendering_ms')
        page = doc[idx]
        pix = page.get_pixmap(dpi=150)
        img_bytes = pix.tobytes("jpeg")
        if timer:
            timer.stop_timing('image_rendering_ms')

        is_upside_down = False
        if pages_data and idx < len(pages_data):
            is_upside_down = pages_data[idx].get('is_upside_down', False)

        if timer:
            timer.start_timing('image_enhancement_ms')
        enhanced_bytes = enhance_preview_image(img_bytes, is_upside_down=is_upside_down, fast_mode=True)
        if timer:
            timer.stop_timing('image_enhancement_ms')

        page_images.append({
            'page_number_in_src': idx + 1,
            'page_index': idx,
            'image_bytes': enhanced_bytes,
        })

    return {
        'pdf_bytes': pdf_bytes,
        'page_images': page_images,
    }


def segment_and_extract_pdf(file_path: str, timer=None, ocr_engine=None) -> List[Dict[str, Any]]:
    """
    High-level entry point for multi-invoice PDF processing:
    1. Opens PDF and counts pages.
    2. Analyzes each page independently with intelligent classification and conditional OCR.
    3. Runs boundary detection.
    4. For each detected invoice:
       - Combines text across pages.
       - Runs structured metadata extraction.
       - Extracts sub-PDF and rendered page images.
    Returns list of detected invoice dictionaries.
    """
    if not os.path.exists(file_path):
        return []

    if timer:
        timer.start_timing('pdf_opening_ms')
    doc = fitz.open(file_path)
    total_pages = len(doc)
    if timer:
        timer.stop_timing('pdf_opening_ms')

    if total_pages == 0:
        doc.close()
        return []

    if ocr_engine is None:
        ocr_engine = get_shared_ocr_engine()

    # 1. Analyze every page independently
    pages_data = []
    for idx in range(total_pages):
        page = doc[idx]
        p_data = analyze_pdf_page(page, page_num=idx + 1, ocr_engine=ocr_engine, timer=timer)
        pages_data.append(p_data)

    # 2. Detect invoice boundaries
    if timer:
        timer.start_timing('segmentation_ms')
    segments = detect_invoice_boundaries(pages_data)
    if timer:
        timer.stop_timing('segmentation_ms')
        timer.increment('invoices_detected_count', len(segments))

    # 3. For each segment, extract combined metadata and sub-files
    detected_invoices = []
    for seg_idx, seg in enumerate(segments, start=1):
        # Combine text from all pages in this invoice
        combined_text = "\n\n".join([p['raw_text'] for p in seg['page_data_list'] if p['raw_text']])

        # Extract metadata
        meta = extract_metadata_from_text(combined_text)

        # Classify segment appearance internally and re-evaluate combined E-Invoice? status
        from .document_type_utils import classify_document_type
        first_page_class = (
            seg['page_data_list'][0].get('classification')
            if seg.get('page_data_list') else None
        )
        doc_type_res = classify_document_type(
            file_path=file_path,
            raw_text=combined_text,
            page_classification=first_page_class,
            page_indices=seg.get('pages'),
            content_type='application/pdf',
        )

        # Decode e-invoice QR code from scanned/mixed segment pages if IRN wasn't found in text
        qr_data = None
        if not meta.get('irn_number'):
            has_images = any(
                (p.get('classification') or {}).get('image_count', 0) > 0
                for p in seg.get('page_data_list', [])
            )
            if has_images:
                from .e_invoice_utils import extract_qr_e_invoice_data
                qr_data = extract_qr_e_invoice_data(file_path, page_indices=seg['pages'])

        from .e_invoice_utils import evaluate_e_invoice_status
        einv_val = evaluate_e_invoice_status(
            raw_text=combined_text,
            qr_data=qr_data,
            appearance_data=doc_type_res,
        )
        meta['e_invoice_status'] = einv_val['e_invoice_status']
        meta['is_e_invoice'] = einv_val['is_e_invoice']
        meta['irn_number'] = einv_val['irn_number']
        meta['e_invoice_source'] = einv_val['e_invoice_source']

        # Propagate invoice_number and total_amount from individual page analysis if missing in combined meta
        if not meta.get('invoice_number'):
            for p in seg['page_data_list']:
                if p.get('invoice_number'):
                    meta['invoice_number'] = p['invoice_number']
                    break
        if not meta.get('total_amount') or meta.get('total_amount') == Decimal('0.00'):
            for p in seg['page_data_list']:
                if p.get('total_amount') and p['total_amount'] > Decimal('0.00'):
                    meta['total_amount'] = p['total_amount']
                    break

        # Fallback for Karnataka Tyre: search existing OCR boxes first, then targeted crop only if missing
        if not meta.get('invoice_number') and meta.get('party_name') == 'KARNATAKA TYRE CARE CENTRE':
            for p in seg['page_data_list']:
                for b in p.get('ocr_boxes', []):
                    t = b.get('text', '')
                    m = re.search(r'(23\d{2})', t)
                    if m:
                        meta['invoice_number'] = m.group(1)
                        break
                    cand = clean_karnataka_invoice_no(t)
                    if len(cand) == 4 and cand.startswith('23'):
                        meta['invoice_number'] = cand
                        break
                if meta.get('invoice_number'):
                    break

            # Targeted high-resolution header crop ONLY if invoice number is still unresolved
            if not meta.get('invoice_number'):
                for p_idx in seg['pages']:
                    try:
                        p_obj = doc[p_idx]
                        pix_200 = p_obj.get_pixmap(dpi=200)
                        from PIL import Image
                        import numpy as np
                        img_200 = Image.frombytes("RGB", [pix_200.width, pix_200.height], pix_200.samples)
                        is_ud = False
                        if pages_data and p_idx < len(pages_data):
                            is_ud = pages_data[p_idx].get('is_upside_down', False)
                        if is_ud:
                            img_200 = img_200.rotate(180)
                        w, h = img_200.size
                        crop_inv = img_200.crop((int(0.50 * w), int(0.12 * h), int(0.95 * w), int(0.25 * h)))
                        res_crop, _ = ocr_engine(np.array(crop_inv))
                        if res_crop:
                            for b, t, _ in res_crop:
                                m = re.search(r'(23\d{2})', t)
                                if m:
                                    meta['invoice_number'] = m.group(1)
                                    break
                            if not meta.get('invoice_number'):
                                for b, t, _ in res_crop:
                                    cand = clean_karnataka_invoice_no(t)
                                    if len(cand) == 4 and cand.startswith('23'):
                                        meta['invoice_number'] = cand
                                        break
                        if meta.get('invoice_number'):
                            break
                    except Exception:
                        pass

        # Extract sub-PDF and page images
        sub_files = extract_sub_pdf_and_images(doc, seg['pages'], timer=timer, pages_data=pages_data)

        # Intelligent AI fallback for multi-page / low-confidence / incomplete PDF segments
        if should_invoke_gemini_fallback(meta=meta, doc_type_res=doc_type_res):
            gemini_pdf_bytes = _build_gemini_pdf_subset(
                doc=doc,
                seg_pages=seg['pages'],
                full_segment_pdf_bytes=sub_files.get('pdf_bytes'),
                max_pages=getattr(settings, 'GEMINI_MAX_PAGES_PER_INVOICE', 3),
            )
            if gemini_pdf_bytes:
                tmp_pdf_path = None
                try:
                    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp_f:
                        tmp_f.write(gemini_pdf_bytes)
                        tmp_pdf_path = tmp_f.name
                    from .gemini_extractor import extract_with_gemini
                    gemini_res = extract_with_gemini(
                        tmp_pdf_path,
                        original_pdf_text=combined_text,
                        timer=timer,
                    )
                    if gemini_res:
                        meta = _merge_gemini_segment_metadata(meta, gemini_res, combined_text)
                except Exception as exc:
                    logger.warning("Gemini multi-page fallback failed softly (keeping RapidOCR/digital extraction): %s", type(exc).__name__)
                finally:
                    if tmp_pdf_path and os.path.exists(tmp_pdf_path):
                        try:
                            os.unlink(tmp_pdf_path)
                        except OSError:
                            pass

        detected_invoices.append({
            'invoice_index': seg_idx,
            'start_page': seg['start_page'],
            'end_page': seg['end_page'],
            'total_pages_in_invoice': len(seg['pages']),
            'pages': seg['pages'],
            'metadata': meta,
            'sub_files': sub_files,
            'is_multi_page': len(seg['pages']) > 1,
        })

    doc.close()
    return detected_invoices


def should_invoke_gemini_fallback(
    meta: Dict[str, Any],
    doc_type_res: Optional[Dict[str, Any]] = None,
    policy: Optional[str] = None,
) -> bool:
    """
    Decide whether to invoke Gemini Vision AI on a PDF invoice segment.
    Avoids sending clean digital/printed invoices unless required by policy or missing fields.
    """
    if hasattr(settings, 'GEMINI_API_KEY'):
        api_key = (getattr(settings, 'GEMINI_API_KEY') or '').strip()
    else:
        api_key = os.getenv('GEMINI_API_KEY', '').strip()
    if not api_key:
        return False

    effective_policy = (policy or getattr(settings, 'GEMINI_FALLBACK_POLICY', 'auto') or 'auto').strip().lower()
    if effective_policy == 'never':
        return False
    if effective_policy == 'always':
        return True

    # 'auto' policy: invoke only when extraction is incomplete or document is handwritten/unclear
    if not (meta.get('party_name') or '').strip():
        return True
    if not (meta.get('invoice_number') or '').strip():
        return True
    if (meta.get('total_amount') or Decimal('0.00')) <= Decimal('0.00'):
        return True
    doc_type = (doc_type_res or {}).get('document_type', '')
    if doc_type in ('HANDWRITTEN', 'MIXED', 'NEEDS_REVIEW'):
        return True
    if meta.get('e_invoice_status') == 'YES' and not (meta.get('irn_number') or '').strip():
        return True
    return False


def _build_gemini_pdf_subset(
    doc,
    seg_pages: List[int],
    full_segment_pdf_bytes: Optional[bytes],
    max_pages: int = 3,
) -> Optional[bytes]:
    """
    Return PDF bytes for Gemini capped at `max_pages` (first + last + middle key pages)
    to avoid sending unnecessary continuation pages to the API.
    """
    if not seg_pages:
        return None
    if len(seg_pages) <= max_pages and full_segment_pdf_bytes:
        return full_segment_pdf_bytes

    selected = [seg_pages[0]]
    if len(seg_pages) > 1 and max_pages >= 2:
        # Include middle pages if budget > 2, then always include final page (totals/IRN)
        middle_budget = max(0, max_pages - 2)
        for p_idx in seg_pages[1:-1][:middle_budget]:
            selected.append(p_idx)
        selected.append(seg_pages[-1])

    subset_doc = fitz.open()
    try:
        for p_idx in selected:
            subset_doc.insert_pdf(doc, from_page=p_idx, to_page=p_idx)
        return subset_doc.tobytes()
    finally:
        subset_doc.close()


def _merge_gemini_segment_metadata(
    base_meta: Dict[str, Any],
    gemini_res: Dict[str, Any],
    combined_text: str,
) -> Dict[str, Any]:
    """Merge Gemini-extracted structured fields into segment metadata while preserving OCR text."""
    merged = dict(base_meta)
    for field in (
        'party_name', 'party_gstin', 'pan', 'invoice_number', 'invoice_date', 'due_date',
        'taxable_amount', 'cgst_amount', 'sgst_amount', 'igst_amount', 'cess_amount',
        'round_off_amount', 'total_amount', 'expense_ledger', 'expense_description',
        'hsn_sac', 'payment_details', 'items', 'e_invoice_status', 'is_e_invoice',
        'irn_number', 'e_invoice_source', 'gemini_extracted_data', 'gemini_usage',
        'extraction_method', 'confidence', 'requires_manual_review',
        'is_ambiguous_vendor', 'vendor_match_status', 'is_financial_approval_eligible',
        'audit_notes',
    ):
        val = gemini_res.get(field)
        if val is not None and val != '':
            merged[field] = val

    merged['raw_text'] = combined_text if combined_text.strip() else gemini_res.get('raw_text', '')
    merged['ocr_extracted_text'] = combined_text
    return merged
