import re
import os
from typing import Dict, Any, Optional
from datetime import datetime
from decimal import Decimal
from pathlib import Path


def normalize_handwritten_number(cand: str) -> str:
    """
    Normalizes optical and handwriting character recognition confusions on numeric voucher/bill IDs.
    In handwritten transport bills (e.g. '0hs' -> '048'):
    'h' -> '4' (vertical stroke with cross loop)
    's' / 'S' -> '8' (stacked loops)
    'B' -> '8'
    'O' / 'o' -> '0'
    'I' / 'l' -> '1'
    'z' / 'Z' -> '2'
    """
    if not cand:
        return cand
    cand_clean = cand.strip(':.- \t')
    if re.match(r'^[0-9hsSOBlIzZ\/-]{1,12}$', cand_clean) and any(c in 'hsSOBlIzZ' for c in cand_clean):
        trans = str.maketrans({'h': '4', 's': '8', 'S': '8', 'B': '8', 'O': '0', 'o': '0', 'I': '1', 'l': '1', 'z': '2', 'Z': '2'})
        return cand_clean.translate(trans)
    return cand_clean


def extract_text_from_pdf_page(page, ocr_engine=None) -> str:
    """
    Extracts text from a PyMuPDF (fitz) page.
    Attempts digital text extraction first.
    If digital text has fewer than 25 characters (scanned page or image PDF),
    renders a 150 DPI pixmap and runs RapidOCR.
    """
    text = page.get_text()
    if text and len(text.strip()) >= 25:
        return text.strip()

    # Scanned page: render pixmap and run OCR
    try:
        if ocr_engine is None:
            from rapidocr_onnxruntime import RapidOCR
            ocr_engine = RapidOCR()
        pix = page.get_pixmap(dpi=150)
        img_bytes = pix.tobytes("png")
        ocr_res, _ = ocr_engine(img_bytes)
        if ocr_res:
            ocr_text = "\n".join([line[1] for line in ocr_res if line and len(line) > 1])
            return ocr_text.strip()
    except Exception:
        pass
    return text.strip() if text else ""


def extract_metadata_from_text(raw_text: str) -> Dict[str, Any]:
    """
    Extracts structured invoice metadata from text (OCR or digital):
    - invoice_number (Voucher / Invoice No for Tally)
    - party_name (Vendor / Party Name)
    - party_gstin (Supplier GSTIN)
    - expense_ledger (Tally Purchase Expense Head)
    - invoice_date (Bill Date)
    - taxable_amount (Taxable base amount)
    - cgst_amount (Central GST)
    - sgst_amount (State GST)
    - igst_amount (Integrated GST)
    - total_amount (Invoice Gross Amount / Grand Total)
    - raw_text (Complete text)
    - confidence (Advisory confidence score)
    """
    result = {
        'invoice_number': '',
        'party_name': '',
        'party_gstin': '',
        'expense_ledger': 'Purchase Spares & Consumables',
        'invoice_date': None,
        'taxable_amount': Decimal('0.00'),
        'cgst_amount': Decimal('0.00'),
        'sgst_amount': Decimal('0.00'),
        'igst_amount': Decimal('0.00'),
        'total_amount': Decimal('0.00'),
        'raw_text': raw_text.strip() if raw_text else '',
        'confidence': 0.0,
        'requires_manual_review': True,
        'is_ambiguous_vendor': True,
    }

    if not raw_text or not raw_text.strip():
        return result

    raw_text = raw_text.strip()

    if raw_text:
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]

        # --- A. Invoice Number Extraction ---
        # Pass 1: Explicit invoice header with number prefix and separator
        for line_str in lines:
            if re.match(r'^(?:Tax\s+)?Invoice$', line_str, re.I):
                continue

            # Skip address lines that might contain 'No.' (e.g., 'No.41, Parameswara Building')
            if re.search(r'\b(?:building|road|street|floor|cross|nagar|lane|plot|opp|near|dist|pin|hosur|bangalore|tumkur)\b', line_str, re.I):
                # Only skip if it doesn't explicitly say 'Invoice No'
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
                if candidate.lower() not in [
                    'date', 'dated', 'total', 'to', 'from', 'gstin', 'for', 'amount',
                    'tax', 'invoice', 'bill', 'particulars', 'no', 'num', 'rate', 'qty'
                ]:
                    result['invoice_number'] = normalize_handwritten_number(candidate)
                    break

        # Pass 2: Standalone invoice voucher format (e.g. F26-27/225 or 2026-27/001 or B-4091)
        if not result['invoice_number']:
            for line_str in lines:
                m_standalone = re.match(
                    r'^([A-Za-z]{1,4}[0-9]{2}[-\/][0-9]{2,4}[-\/]?[0-9]{1,6}|[0-9]{2,4}[-\/][0-9]{2,4}[-\/][0-9]{1,6})$',
                    line_str
                )
                if m_standalone:
                    result['invoice_number'] = m_standalone.group(1)
                    break

        # Pass 3: Fallback prefix (e.g. B.No: 048 or Sl.No: 1205) excluding address lines
        if not result['invoice_number']:
            for line_str in lines:
                if re.search(r'\b(?:building|road|street|floor|cross|nagar|lane|plot|opp|near|dist|pin)\b', line_str, re.I):
                    continue
                inv_match = re.search(r'\b(?:B\.?\s*No|Sl\.?\s*No|No)\s*[:.-]+\s*([A-Za-z0-9][A-Za-z0-9\/-]{1,24})', line_str, re.I)
                if inv_match:
                    candidate = inv_match.group(1).strip(':.- \t')
                    if candidate.lower() not in ['date', 'dated', 'total', 'to', 'from', 'gstin', 'for', 'amount', 'tax', 'no']:
                        result['invoice_number'] = normalize_handwritten_number(candidate)
                        break

        # --- B. Party / Vendor Name Extraction ---
        # 1. Match against official Master Vendor Directory
        try:
            from .vendor_directory import VENDOR_DIRECTORY
            raw_lower = raw_text.lower()
            raw_no_space = re.sub(r'[^a-z0-9]', '', raw_lower)
            sorted_vendors = sorted(VENDOR_DIRECTORY.items(), key=lambda x: len(x[1]['name']), reverse=True)
            
            # Exact substring match
            for gstin, info in sorted_vendors:
                v_name = info['name'].lower()
                if len(v_name) >= 4 and v_name in raw_lower:
                    result['party_name'] = info['name']
                    result['party_gstin'] = gstin
                    if info.get('ledger'):
                        result['expense_ledger'] = info['ledger']
                    break

            # Token match (robust against OCR whitespace / hyphen insertion like 'Kwa lity')
            if not result['party_name']:
                stop_tokens = {
                    'transport', 'transports', 'india', 'automobiles', 'motors', 'services', 'corporation', 
                    'enterprises', 'trading', 'petroleum', 'general', 'insurance', 'company', 'limited', 
                    'pvt', 'ltd', 'private', 'solutions', 'systems', 'house', 'world', 'centre', 'center', 
                    'links', 'group', 'industries', 'associates', 'agency', 'agencies', 'products', 
                    'commercial', 'supplies', 'supply', 'fuels', 'fuel', 'petrol', 'diesel', 'diesels',
                    'auto', 'mobiles', 'road', 'express', 'national', 'south', 'north', 'bharat'
                }
                for gstin, info in sorted_vendors:
                    name = info['name'].lower()
                    tokens = [
                        t for t in re.split(r'[^a-z0-9]', name) 
                        if len(t) >= 5 and t not in stop_tokens
                    ]
                    for t in tokens:
                        if t in raw_no_space:
                            result['party_name'] = info['name']
                            result['party_gstin'] = gstin
                            if info.get('ledger'):
                                result['expense_ledger'] = info['ledger']
                            break
                    if result['party_name']:
                        break
        except Exception:
            pass

        # 2. Check for explicit 15-character Indian GSTIN (excluding buyer Balaji Transports: 29AAWFB0395H1ZX)
        if not result['party_name']:
            all_gstins = re.findall(r'\b([0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1})\b', raw_text)
            for g in all_gstins:
                if g.upper() != '29AAWFB0395H1ZX':
                    result['party_gstin'] = g.upper()
                    try:
                        from .vendor_directory import find_vendor_by_gstin
                        v_info = find_vendor_by_gstin(result['party_gstin'])
                        if v_info:
                            result['party_name'] = v_info['name']
                            if v_info.get('ledger'):
                                result['expense_ledger'] = v_info['ledger']
                    except Exception:
                        pass
                    break

        # 3. Company signature / Header indicator (e.g. "For Kwality Furnishings", "For Srinidhi Engineering")
        if not result['party_name']:
            sig_match = re.search(r'\b(?:For|lor|M/s|Messrs)[:.\s]+([A-Za-z0-9\s.,&-]{3,50})', raw_text, re.I)
            if sig_match:
                cand = sig_match.group(1).strip().splitlines()[0]
                cand = re.sub(r'[\(\[].*?[\)\]]', '', cand).strip()
                if len(cand) >= 3 and not re.search(r'\b(?:Balaji|Invoice|Authorized|Signatory)\b', cand, re.I):
                    result['party_name'] = cand

        # 4. Check for vendor name keyword indicators in top lines
        if not result['party_name']:
            for line in lines[:10]:
                if re.search(
                    r'(?:Indian\s*Oil|Bharat\s*Petroleum|Hindustan\s*Petroleum|Reliance|Nayara|'
                    r'IOCL|BPCL|HPCL|M/s|Messrs|Petroleum|Fuels|Corporation|Enterprises|Agency|'
                    r'Automobiles|Motors|Services|Logistics|Transports|Spares|Tyres|Toll|Plaza|'
                    r'Furnishings|Hardware|Paints|Pvt|Ltd|Limited)',
                    line,
                    re.IGNORECASE
                ):
                    cleaned = re.sub(r'^(?:Tax\s*Invoice|Invoice|Bill|Cash\s*Memo|From[:.\s]*|M/s[:.\s]*)', '', line, flags=re.IGNORECASE).strip()
                    if len(cleaned) >= 3 and not re.search(r'^(GSTIN|Date|Invoice|Phone|Mobile|Tel|Email|To\b)', cleaned, re.IGNORECASE):
                        if not re.search(r'\bBalaji\b', cleaned, re.I):
                            result['party_name'] = cleaned[:100]
                            break

            # Fallback for Party Name: Top header line if not matched
            if not result['party_name'] and lines:
                for cand_line in lines[:3]:
                    c_clean = cand_line.strip()
                    if c_clean == result.get('invoice_number'):
                        continue
                    if len(c_clean) > 3 and len(c_clean) < 60 and not re.search(r'(Invoice|Bill|Tax|Date|GSTIN|Balaji)', c_clean, re.IGNORECASE):
                        result['party_name'] = c_clean
                        break

        # Match vendor name against directory to fill GSTIN and expense ledger if missing
        if result['party_name']:
            try:
                from .vendor_directory import find_vendor_by_name
                matched = find_vendor_by_name(result['party_name'])
                if matched:
                    if not result['party_gstin']:
                        result['party_gstin'] = matched['gstin']
                    result['party_name'] = matched['name']
                    if matched.get('ledger'):
                        result['expense_ledger'] = matched['ledger']
            except Exception:
                pass

        # --- C. Invoice Date Extraction ---
        date_match = re.search(
            r'(?:Date|Dated|Dt)?[:.\s]*(\d{1,2}[-\/.]\d{1,2}[-\/.](?:20)?\d{2})',
            raw_text,
            re.IGNORECASE
        )
        if date_match:
            date_str = date_match.group(1).replace('/', '-').replace('.', '-')
            for fmt in ('%d-%m-%Y', '%d-%m-%y', '%Y-%m-%d', '%m-%d-%Y'):
                try:
                    result['invoice_date'] = datetime.strptime(date_str, fmt).date()
                    break
                except ValueError:
                    continue

        # --- D. Financial & Tax Breakdown Extraction (Taxable, CGST, SGST, IGST, Grand Total) ---
        taxable_val = Decimal('0.00')
        cgst_val = Decimal('0.00')
        sgst_val = Decimal('0.00')
        igst_val = Decimal('0.00')
        gtotal_val = Decimal('0.00')

        def parse_amt_str(s: str) -> Optional[Decimal]:
            if not s:
                return None
            clean = s.replace('-', '.').replace(',', '').strip('., ')
            parts = clean.split('.')
            if len(parts) > 2:
                clean = ''.join(parts[:-1]) + '.' + parts[-1]
            try:
                d = Decimal(clean)
                return d if d > 0 else None
            except Exception:
                return None

        # 1. Words-to-Amount Helper for Indian Invoices (statutory Rupees / INR in words)
        words_amount = None
        words_match = re.search(r'(?:INR|Rupees|Rs\.?)\s*([A-Za-z\s]+?)\s*(?:Only|\.)', raw_text, re.I)
        if words_match:
            raw_words = words_match.group(1)
            spaced = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', raw_words)
            spaced = re.sub(r'([a-z])([A-Z])', r'\1 \2', spaced).lower()
            units = {
                'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9,
                'ten': 10, 'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14, 'fifteen': 15,
                'sixteen': 16, 'seventeen': 17, 'eighteen': 18, 'nineteen': 19, 'twenty': 20, 'thirty': 30,
                'forty': 40, 'fifty': 50, 'sixty': 60, 'seventy': 70, 'eighty': 80, 'ninety': 90
            }
            scales = {'crore': 10000000, 'lakh': 100000, 'lac': 100000, 'thousand': 1000, 'hundred': 100}
            tokens = re.findall(r'[a-z]+', spaced)
            tot, cur, has_word_tokens = 0, 0, False
            for tok in tokens:
                if tok in units:
                    cur += units[tok]
                    has_word_tokens = True
                elif tok in scales:
                    has_word_tokens = True
                    mult = scales[tok]
                    if cur == 0:
                        cur = 1
                    if mult >= 1000:
                        tot += cur * mult
                        cur = 0
                    else:
                        cur *= mult
            tot += cur
            if has_word_tokens and tot > 0:
                words_amount = Decimal(str(tot))

        # 2. Extract CGST amount
        for i, line in enumerate(lines):
            if re.search(r'(?:CGST|C\.?GST|COST)', line, re.I):
                for off in [0, -1, 1, -2, 2]:
                    idx = i + off
                    if 0 <= idx < len(lines):
                        m = re.search(r'([0-9]+(?:[.,-][0-9]+)+|[0-9]{4,6})', lines[idx])
                        if m and m.group(1) not in ['9', '14', '18']:
                            val_raw = m.group(1).replace('-', '.')
                            if '.' not in val_raw and len(val_raw) >= 4:
                                val_raw = val_raw[:-2] + '.' + val_raw[-2:]
                            d = parse_amt_str(val_raw)
                            if d and d > 0:
                                cgst_val = d
                                break
                if cgst_val > 0:
                    break

        # 3. Extract SGST amount
        for i, line in enumerate(lines):
            if re.search(r'(?:SGST|S\.?GST|UTGST)', line, re.I):
                for off in [0, -1, 1, -2, 2]:
                    idx = i + off
                    if 0 <= idx < len(lines):
                        m = re.search(r'([0-9]+(?:[.,-][0-9]+)+|[0-9]{4,6})', lines[idx])
                        if m and m.group(1) not in ['9', '14', '18']:
                            val_raw = m.group(1).replace('-', '.')
                            if '.' not in val_raw and len(val_raw) >= 4:
                                val_raw = val_raw[:-2] + '.' + val_raw[-2:]
                            d = parse_amt_str(val_raw)
                            if d and d > 0:
                                sgst_val = d
                                break
                if sgst_val > 0:
                    break

        # 4. Extract IGST amount (for interstate suppliers)
        is_interstate = bool(result.get('party_gstin') and not result['party_gstin'].startswith('29'))
        if is_interstate or (cgst_val == 0 and sgst_val == 0):
            for i, line in enumerate(lines):
                if re.search(r'(?:IGST|I\.?GST|Integrated\s*Tax)', line, re.I):
                    for off in range(1, 10):
                        idx = i + off
                        if idx < len(lines):
                            cand_l = lines[idx]
                            if not re.search(r'\b(?:G\.?TOTAL|Grand\s*Total|Account|IFSC|Mob)\b', cand_l, re.I):
                                m = re.search(r'([0-9]+(?:[.,-][0-9]{2})+)', cand_l)
                                if m and m.group(1) not in ['18.00', '12.00', '5.00', '28.00']:
                                    d = parse_amt_str(m.group(1))
                                    if d and d > 0 and (gtotal_val == 0 or d < gtotal_val):
                                        igst_val = d
                                        break
                    if igst_val > 0:
                        break

        # 5. Extract Grand Total (G.TOTAL / Net Amount / Total)
        for i, line in enumerate(lines):
            if re.search(r'\b(?:G\.?TOTAL|Grand\s*Total|Net\s*Amount|Net\s*Payable|Total\s*Payable|Invoice\s*Total|Bill\s*Amount)\b', line, re.I):
                for off in [0, -1, 1, -2, -3, 2]:
                    idx = i + off
                    if 0 <= idx < len(lines):
                        cand_l = lines[idx]
                        m = re.search(r'([0-9]+(?:[.,][0-9]+)+)', cand_l)
                        if m:
                            d = parse_amt_str(m.group(1))
                            if d and d > 0:
                                gtotal_val = d
                                break
                if gtotal_val > 0:
                    break

        # Words amount cross-validation
        if words_amount:
            if gtotal_val == 0 or abs(gtotal_val - words_amount) > 1000:
                # Check leading '7' Rupee symbol OCR artifact
                if gtotal_val > 0 and str(int(gtotal_val)).startswith('7') and str(int(gtotal_val))[1:] == str(int(words_amount)):
                    gtotal_val = Decimal(str(gtotal_val)[1:])
                else:
                    gtotal_val = words_amount

        # 6. Extract Taxable Base
        for i, line in enumerate(lines):
            # Check same line first: e.g. "Taxable Amount: 5796.00"
            m_same = re.search(r'\b(?:Taxable\s*Amount|Taxable\s*Value|Taxable)\s*[:.-]?\s*₹?\s*([0-9,]+(?:\.[0-9]+)?)', line, re.I)
            if m_same:
                d = parse_amt_str(m_same.group(1))
                if d and d > 0 and (gtotal_val == 0 or d < gtotal_val):
                    taxable_val = d
                    break

            if re.search(r'\b(?:Taxable\s*Amount|Taxable\s*Value|Taxable)\b', line, re.I) or re.match(r'^Total$', line.strip(), re.I):
                for off in [1, 2, -1, 3]:
                    idx = i + off
                    if 0 <= idx < len(lines):
                        cand_l = lines[idx]
                        if not re.search(r'(?:G\.?TOTAL|Grand\s*Total|GST|Tax|CGST|SGST|Round|Date|Dated|Phone|Mobile)', cand_l, re.I):
                            m = re.search(r'([0-9]+(?:[.,][0-9]+)+|[0-9]{3,7})', cand_l)
                            if m and m.group(1) not in ['9', '18', '29', '33']:
                                d = parse_amt_str(m.group(1))
                                if d and d > 0 and (gtotal_val == 0 or d < gtotal_val):
                                    taxable_val = d
                                    break
                if taxable_val > 0:
                    break

        # 7. Fallback search for numeric total candidates if gtotal not found
        if gtotal_val == 0:
            numeric_candidates = []
            same_line_matches = re.findall(
                r'(?:Net\s*Amount|Grand\s*Total|G\.?TOTAL|Total\s*Payable|Total\s*Amount|Gross\s*Amount|Invoice\s*Total|Bill\s*Amount|Total)\s*[:.-]?\s*₹?\s*([0-9.,]+)',
                raw_text,
                re.IGNORECASE
            )
            for raw_num in same_line_matches:
                d = parse_amt_str(raw_num)
                if d and d > 0:
                    numeric_candidates.append(d)

            for i, line in enumerate(lines):
                if re.search(r'\b(?:Net\s*Amount|Grand\s*Total|G\.?TOTAL|Total\s*Payable|Total\s*Amount|Total|Gross\s*Amount)\b', line, re.I):
                    for offset in [1, -1, 2, -2]:
                        idx = i + offset
                        if 0 <= idx < len(lines):
                            num_matches = re.findall(r'([0-9]+(?:[.,][0-9]+)+)', lines[idx])
                            for raw_num in num_matches:
                                d = parse_amt_str(raw_num)
                                if d and d > 0:
                                    numeric_candidates.append(d)

            if numeric_candidates:
                decimal_cands = [n for n in numeric_candidates if (n % 1) != 0]
                gtotal_val = decimal_cands[0] if decimal_cands else numeric_candidates[0]

        # 8. Statutory GST Rules & Parity Normalization
        if not is_interstate:
            # Intrastate supplies (Karnataka): CGST & SGST must be equal (e.g. both 9% or both 14%)
            if cgst_val > 0 and (sgst_val == 0 or abs(sgst_val - cgst_val) > 10):
                sgst_val = cgst_val
            elif sgst_val > 0 and (cgst_val == 0 or abs(cgst_val - sgst_val) > 10):
                cgst_val = sgst_val
            igst_val = Decimal('0.00')
        else:
            # Interstate supplies: IGST applies, CGST and SGST are zero
            cgst_val = Decimal('0.00')
            sgst_val = Decimal('0.00')

        # 9. GST Mathematical Reconciliation
        if taxable_val > 0 and cgst_val > 0 and sgst_val > 0 and gtotal_val == 0:
            gtotal_val = (taxable_val + cgst_val + sgst_val).quantize(Decimal('0.01'))
        elif gtotal_val > 0 and cgst_val > 0 and sgst_val > 0 and taxable_val == 0:
            taxable_val = (gtotal_val - cgst_val - sgst_val).quantize(Decimal('0.01'))
        elif gtotal_val > 0 and igst_val > 0 and taxable_val == 0:
            taxable_val = (gtotal_val - igst_val).quantize(Decimal('0.01'))
        elif taxable_val > 0 and gtotal_val > taxable_val and cgst_val == 0 and sgst_val == 0 and igst_val == 0:
            diff = gtotal_val - taxable_val
            if is_interstate:
                igst_val = diff.quantize(Decimal('0.01'))
            else:
                half = (diff / Decimal('2')).quantize(Decimal('0.01'))
                cgst_val = half
                sgst_val = half
        elif gtotal_val == 0 and taxable_val > 0:
            gtotal_val = taxable_val
        elif taxable_val == 0 and gtotal_val > 0:
            taxable_val = gtotal_val

        result['taxable_amount'] = taxable_val
        result['cgst_amount'] = cgst_val
        result['sgst_amount'] = sgst_val
        result['igst_amount'] = igst_val
        result['total_amount'] = gtotal_val

        # --- E. Confidence Scoring & Verification Gating ---
        score = 0.0
        if result['invoice_number']:
            score += 0.35
        if result['party_name']:
            score += 0.35
        if result['party_gstin']:
            score += 0.15
            result['is_ambiguous_vendor'] = False
        else:
            result['is_ambiguous_vendor'] = True

        if result['total_amount'] > Decimal('0.00'):
            score += 0.15

        result['confidence'] = round(score, 2)
        # Financial policy: OCR is advisory only. Naveen's approval is always required before Tally posting.
        result['requires_manual_review'] = True

    return result


def extract_invoice_metadata(file_path: str, content_type: str = '') -> Dict[str, Any]:
    """
    Intelligent document reader for photographed receipts and digital/scanned invoice documents.
    Extracts text and applies extract_metadata_from_text().
    """
    if not os.path.exists(file_path):
        return extract_metadata_from_text("")

    raw_text = ""

    # 1. Digital or scanned PDF Document
    if content_type == 'application/pdf' or file_path.lower().endswith('.pdf'):
        try:
            import fitz
            doc = fitz.open(file_path)
            for page in doc:
                text = extract_text_from_pdf_page(page)
                if text:
                    raw_text += text + "\n"
            doc.close()
        except Exception:
            pass

        if not raw_text.strip():
            try:
                import pdfplumber
                with pdfplumber.open(file_path) as pdf:
                    for page in pdf.pages[:10]:
                        text = page.extract_text()
                        if text:
                            raw_text += text + "\n"
            except Exception:
                pass

    # 2. Photographed Receipt & Image Reader (Deep Learning OCR)
    else:
        try:
            from rapidocr_onnxruntime import RapidOCR
            engine = RapidOCR()
            ocr_res, _ = engine(file_path)
            if ocr_res:
                raw_text = "\n".join([line[1] for line in ocr_res if line and len(line) > 1])
        except Exception:
            pass

        if not raw_text.strip():
            try:
                from PIL import Image, ImageEnhance
                img = Image.open(file_path)
                if img.mode != 'L':
                    img_gray = img.convert('L')
                else:
                    img_gray = img
                enhancer = ImageEnhance.Contrast(img_gray)
                enhanced_img = enhancer.enhance(1.8)

                try:
                    import pytesseract
                    raw_text = pytesseract.image_to_string(enhanced_img, timeout=10)
                except Exception:
                    pass
            except Exception:
                pass

    return extract_metadata_from_text(raw_text)
