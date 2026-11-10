import re
import os
import io
from typing import List, Dict, Any, Optional
from decimal import Decimal
import fitz  # PyMuPDF
from .reader import extract_text_from_pdf_page, extract_metadata_from_text, normalize_handwritten_number
from .vendor_directory import VENDOR_DIRECTORY, find_vendor_by_name, find_vendor_by_gstin


def analyze_pdf_page(page, page_num: int, ocr_engine=None) -> Dict[str, Any]:
    """
    Analyzes a single PDF page and extracts text along with key boundary detection signals.
    page_num is 1-indexed.
    """
    text = extract_text_from_pdf_page(page, ocr_engine=ocr_engine)
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # Run structured metadata extraction
    meta = extract_metadata_from_text(text)

    data = {
        'page_num': page_num,
        'page_idx': page_num - 1,
        'raw_text': text,
        'lines': lines,
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

    # 4. If invoice number still missing on pre-printed bill, do targeted header crop
    if not data['invoice_number'] and data['has_vendor_header']:
        try:
            from PIL import Image
            import numpy as np
            if ocr_engine is None:
                from rapidocr_onnxruntime import RapidOCR
                ocr_engine = RapidOCR()
            pix_200 = page.get_pixmap(dpi=200)
            img_200 = Image.frombytes("RGB", [pix_200.width, pix_200.height], pix_200.samples)
            res_quick, _ = ocr_engine(np.array(img_200))
            is_upside_down = False
            if res_quick:
                footer_y = [(b[0][1] + b[2][1]) / 2.0 for b, t_str, _ in res_quick if any(k in t_str.lower() for k in ['authorised', 'signatory', 'rupees'])]
                header_y = [(b[0][1] + b[2][1]) / 2.0 for b, t_str, _ in res_quick if any(k in t_str.lower() for k in ['karnataka', 'tyre care', 'tax invoice'])]
                if footer_y and header_y and min(footer_y) < min(header_y):
                    is_upside_down = True
            if is_upside_down:
                img_200 = img_200.rotate(180)
            w, h = img_200.size
            crop_inv = img_200.crop((int(0.50 * w), int(0.12 * h), int(0.95 * w), int(0.25 * h)))
            res_crop, _ = ocr_engine(np.array(crop_inv))
            if res_crop:
                for b, t, _ in res_crop:
                    m = re.search(r'(23\d{2})', t)
                    if m:
                        data['invoice_number'] = m.group(1)
                        break
                if not data['invoice_number']:
                    for b, t, _ in res_crop:
                        cand = clean_karnataka_invoice_no(t)
                        if len(cand) == 4 and cand.startswith('23'):
                            data['invoice_number'] = cand
                            break
        except Exception:
            pass

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


def enhance_preview_image(img_bytes: bytes, is_upside_down: bool = False) -> bytes:
    """
    Enhances rendered PDF page images for crisp display in Naveen's document viewer:
    - Normalizes lighting, evens out photocopy backgrounds, and sharpens text characters.
    """
    try:
        from PIL import Image, ImageOps
        import cv2
        import numpy as np
        import io

        pil_img = Image.open(io.BytesIO(img_bytes))
        pil_img = ImageOps.exif_transpose(pil_img)
        if is_upside_down:
            pil_img = pil_img.rotate(180)
        if pil_img.mode != 'RGB':
            pil_img = pil_img.convert('RGB')

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
        out_img.save(out_buf, format='JPEG', quality=92, optimize=True)
        return out_buf.getvalue()
    except Exception:
        return img_bytes


def extract_sub_pdf_and_images(doc, page_indices: List[int]) -> Dict[str, Any]:
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
        page = doc[idx]
        pix = page.get_pixmap(dpi=150)
        img_bytes = pix.tobytes("jpeg")
        # Auto-enhance preview image for high contrast and clean viewing
        enhanced_bytes = enhance_preview_image(img_bytes)
        page_images.append({
            'page_number_in_src': idx + 1,
            'page_index': idx,
            'image_bytes': enhanced_bytes,
        })

    return {
        'pdf_bytes': pdf_bytes,
        'page_images': page_images,
    }


def segment_and_extract_pdf(file_path: str) -> List[Dict[str, Any]]:
    """
    High-level entry point for multi-invoice PDF processing:
    1. Opens PDF and counts pages.
    2. Analyzes each page independently.
    3. Runs boundary detection.
    4. For each detected invoice:
       - Combines text across pages.
       - Runs structured metadata extraction.
       - Extracts sub-PDF and rendered page images.
    Returns list of detected invoice dictionaries.
    """
    if not os.path.exists(file_path):
        return []

    doc = fitz.open(file_path)
    total_pages = len(doc)

    if total_pages == 0:
        doc.close()
        return []

    # 1. Analyze every page independently
    pages_data = []
    for idx in range(total_pages):
        page = doc[idx]
        p_data = analyze_pdf_page(page, page_num=idx + 1)
        pages_data.append(p_data)

    # 2. Detect invoice boundaries
    segments = detect_invoice_boundaries(pages_data)

    # 3. For each segment, extract combined metadata and sub-files
    detected_invoices = []
    for seg_idx, seg in enumerate(segments, start=1):
        # Combine text from all pages in this invoice
        combined_text = "\n\n".join([p['raw_text'] for p in seg['page_data_list'] if p['raw_text']])

        # Extract metadata
        meta = extract_metadata_from_text(combined_text)

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

        # Fallback for Karnataka Tyre: if invoice_number is still missing, do targeted header crop on segment pages
        if not meta.get('invoice_number') and meta.get('party_name') == 'KARNATAKA TYRE CARE CENTRE':
            for p_idx in seg['pages']:
                try:
                    page = doc[p_idx]
                    pix_200 = page.get_pixmap(dpi=200)
                    from PIL import Image
                    import numpy as np
                    from rapidocr_onnxruntime import RapidOCR
                    eng = RapidOCR()
                    img_200 = Image.frombytes("RGB", [pix_200.width, pix_200.height], pix_200.samples)
                    res_quick, _ = eng(np.array(img_200))
                    is_upside_down = False
                    if res_quick:
                        footer_y = [(b[0][1] + b[2][1]) / 2.0 for b, t_str, _ in res_quick if any(k in t_str.lower() for k in ['authorised', 'signatory', 'rupees'])]
                        header_y = [(b[0][1] + b[2][1]) / 2.0 for b, t_str, _ in res_quick if any(k in t_str.lower() for k in ['karnataka', 'tyre care', 'tax invoice'])]
                        if footer_y and header_y and min(footer_y) < min(header_y):
                            is_upside_down = True
                    if is_upside_down:
                        img_200 = img_200.rotate(180)
                    w, h = img_200.size
                    crop_inv = img_200.crop((int(0.50 * w), int(0.12 * h), int(0.95 * w), int(0.25 * h)))
                    res_crop, _ = eng(np.array(crop_inv))
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
        sub_files = extract_sub_pdf_and_images(doc, seg['pages'])

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

