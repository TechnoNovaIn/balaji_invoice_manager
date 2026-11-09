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

    data = {
        'page_num': page_num,
        'page_idx': page_num - 1,
        'raw_text': text,
        'lines': lines,
        'invoice_number': '',
        'party_name': '',
        'party_gstin': '',
        'has_invoice_header': False,
        'has_total_or_footer': False,
        'is_page_1': False,
        'is_continuation': False,
        'total_amount': Decimal('0.00'),
    }

    if not text:
        return data

    text_lower = text.lower()

    # 1. Page Number Indicators ("Page 1 of 2", "Page 2 of 2", "Sheet 1", "Contd...")
    # Explicit continuation markers
    if re.search(r'\b(?:page|sheet|pg)\.?\s*([2-9]|\d{2,})\s*(?:of|\/)\s*([2-9]|\d{2,})', text, re.I):
        data['is_continuation'] = True
    elif re.search(r'\b(?:continued\s+on\s+next\s+page|contd\.?|continued\s+from\s+page|carried\s+over|brought\s+forward)\b', text, re.I):
        data['is_continuation'] = True

    # Explicit page 1 markers
    if re.search(r'\b(?:page|sheet|pg)\.?\s*1\s*(?:of|\/)\s*([1-9]|\d{2,})', text, re.I):
        data['is_page_1'] = True
    elif re.search(r'\b(?:original\s+for\s+buyer|duplicate\s+for\s+transporter|triplicate)\b', text, re.I):
        data['is_page_1'] = True

    # 2. Invoice Header Indicator
    if re.search(r'\b(?:tax\s*invoice|retail\s*invoice|commercial\s*invoice|bill\s*of\s*supply|cash\s*bill|cash\s*memo|delivery\s*challan)\b', text, re.I):
        data['has_invoice_header'] = True

    # 3. Invoice Total / Footer Indicator
    if re.search(r'\b(?:grand\s*total|g\.?total|net\s*amount|total\s*payable|total\s*amount|amount\s*chargeable|rupees\s*in\s*words|inr\s+[a-z\s]+only|authorized\s*signatory|for\s+[a-z\s]+(?:ltd|pvt|transports|enterprises|industries|agency))\b', text, re.I):
        data['has_total_or_footer'] = True

    # 4. Extract Invoice Number candidate on this page
    for line_str in lines:
        if re.match(r'^(?:Tax\s+)?Invoice$', line_str, re.I):
            continue
        if re.search(r'\b(?:building|road|street|floor|cross|nagar|lane|plot|opp|near|dist|pin)\b', line_str, re.I):
            if not re.search(r'\b(?:Invoice|Bill\s*No|INV\b)', line_str, re.I):
                continue
        inv_match = re.search(
            r'\b(?:Tax\s*Invoice|Invoice|INV|Bill|Cash\s*Memo|Challan|Receipt|Voucher)\s*(?:\b(?:Number|Num|No)\b|#|\.)\s*[:.-]*\s*([A-Za-z0-9][A-Za-z0-9\/-]{1,24})',
            line_str,
            re.I
        )
        if not inv_match:
            inv_match = re.search(
                r'\b(?:Tax\s*Invoice|Invoice|INV|Bill|Cash\s*Memo|Challan|Receipt|Voucher)\s*[:.-]+\s*([A-Za-z0-9][A-Za-z0-9\/-]{1,24})',
                line_str,
                re.I
            )
        if inv_match:
            candidate = inv_match.group(1).strip(':.- \t')
            if candidate.lower() not in ['date', 'dated', 'total', 'to', 'from', 'gstin', 'for', 'amount', 'tax', 'no']:
                data['invoice_number'] = normalize_handwritten_number(candidate)
                break

    # 5. Extract GSTIN on this page
    all_gstins = re.findall(r'\b([0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1})\b', text)
    for g in all_gstins:
        if g.upper() != '29AAWFB0395H1ZX':  # Exclude buyer GSTIN
            data['party_gstin'] = g.upper()
            break

    # 6. Extract Vendor Name candidate
    for gstin_key, v_info in VENDOR_DIRECTORY.items():
        v_name = v_info['name'].lower()
        if len(v_name) >= 4 and v_name in text_lower:
            data['party_name'] = v_info['name']
            if not data['party_gstin']:
                data['party_gstin'] = gstin_key
            break

    # 7. Check for Total Amount on this page
    amt_matches = re.findall(r'(?:Grand\s*Total|G\.?TOTAL|Net\s*Amount|Total\s*Payable|Total\s*Amount|Total)\s*[:.-]?\s*₹?\s*([0-9,]+(?:\.[0-9]{2})?)', text, re.I)
    for m in amt_matches:
        try:
            val = Decimal(m.replace(',', ''))
            if val > 0:
                data['total_amount'] = val
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
        if curr['invoice_number'] and seg_inv_numbers:
            if curr['invoice_number'] not in seg_inv_numbers:
                is_new_invoice = True

        # --- Rule 2: Explicit Supplier GSTIN Mismatch ---
        elif curr['party_gstin'] and seg_gstins:
            if curr['party_gstin'] not in seg_gstins:
                is_new_invoice = True

        # --- Rule 3: Explicit Supplier Name Mismatch ---
        elif curr['party_name'] and seg_vendors:
            if curr['party_name'] not in seg_vendors:
                is_new_invoice = True

        # --- Rule 4: Explicit Continuation Marker overrides header ---
        elif curr['is_continuation']:
            is_new_invoice = False

        # --- Rule 5: Explicit Page 1 Indicator ---
        elif curr['is_page_1']:
            is_new_invoice = True

        # --- Rule 6: Fresh Invoice Header following an invoice that already has Total/Footer ---
        elif curr['has_invoice_header'] and seg_has_total:
            is_new_invoice = True

        # --- Rule 7: Previous page had both Header and Grand Total, and Current page has Header or Total ---
        elif prev['has_invoice_header'] and prev['has_total_or_footer'] and (curr['has_invoice_header'] or curr['has_total_or_footer'] or curr['invoice_number']):
            is_new_invoice = True

        # --- Rule 8: Multi-invoice batch with distinct totals ---
        elif curr['total_amount'] > 0 and any(p['total_amount'] > 0 for p in current_segment_pages):
            # Both pages have distinct non-zero totals and no continuation marker
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


def extract_sub_pdf_and_images(doc, page_indices: List[int]) -> Dict[str, Any]:
    """
    Extracts specified pages from PyMuPDF document:
    - Generates a standalone sub-PDF file (bytes) containing just these pages.
    - Renders high-resolution 150 DPI JPEG images for each page.
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
        page_images.append({
            'page_number_in_src': idx + 1,
            'page_index': idx,
            'image_bytes': img_bytes,
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

