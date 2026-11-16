"""
Document Orientation Detection & Safe Upright Correction Engine
Balaji InvoiceHub - Phase 4
"""
import io
import logging
from typing import Tuple, Dict, Any, Optional
import numpy as np
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

HEADER_STATUTORY_KEYWORDS = {
    'TAX', 'INVOICE', 'GSTIN', 'BILL', 'SUPPLIER', 'PURCHASE', 'MEMO',
    'CASH', 'CREDIT', 'CONSIGNOR', 'CONSIGNEE', 'TRANSPORTS', 'BALAJI'
}

FOOTER_FINANCIAL_KEYWORDS = {
    'TOTAL', 'AMOUNT', 'SIGNATURE', 'RUPEES', 'TERMS', 'CONDITIONS',
    'RECEIVED', 'E&OE', 'ROUND', 'NET'
}


def compute_upright_header_score(boxes_with_text, image_height: int) -> int:
    """
    Scores document orientation based on spatial distribution of statutory header and footer words.
    In Indian commercial vouchers, invoice headers and GSTINs appear at the top (y < 45% height),
    while totals, signatures, and payment summaries appear at the bottom (y > 55% height).
    """
    if not boxes_with_text or image_height <= 0:
        return 0

    score = 0
    for box, text, _ in boxes_with_text:
        if not text or not str(text).strip():
            continue
        try:
            # box is [[x1,y1], [x2,y2], [x3,y3], [x4,y4]]
            mid_y = (box[0][1] + box[2][1]) / 2.0
        except (IndexError, TypeError):
            continue

        words = {w.strip('.,:;/-#') for w in text.upper().split()}
        header_hits = len(words & HEADER_STATUTORY_KEYWORDS)
        footer_hits = len(words & FOOTER_FINANCIAL_KEYWORDS)

        if mid_y < 0.45 * image_height:
            score += header_hits * 3
            score -= footer_hits * 2
        elif mid_y > 0.55 * image_height:
            score += footer_hits * 3
            score -= header_hits * 2

    return score


def analyze_text_line_orientation(boxes_with_text) -> str:
    """
    Determines whether detected text boxes are predominantly horizontal (0° / 180°)
    or predominantly vertical (90° / 270°).
    """
    if not boxes_with_text:
        return 'UNKNOWN'

    h_count = 0
    v_count = 0

    for box, _, _ in boxes_with_text:
        try:
            width = abs(box[1][0] - box[0][0])
            height = abs(box[2][1] - box[1][1])
            if width >= height:
                h_count += 1
            else:
                v_count += 1
        except (IndexError, TypeError):
            continue

    total = h_count + v_count
    if total < 2:
        return 'UNKNOWN'

    if v_count / float(total) >= 0.60:
        return 'VERTICAL'
    if h_count / float(total) >= 0.60:
        return 'HORIZONTAL'

    return 'AMBIGUOUS'


def detect_document_rotation_angle(pil_img: Image.Image) -> Tuple[int, Dict[str, Any]]:
    """
    Determines the clockwise rotation angle (0, 90, 180, or 270 degrees) needed to
    restore a document photograph to upright orientation.

    Safety Rules:
    - Never rotate spuriously: if confidence is ambiguous or document lacks decisive signal
      (such as faded, handwritten, or low-contrast notes), returns 0° (preserves original orientation).
    - Checks horizontal/vertical text box aspect ratios before evaluating spatial keyword distribution.
    """
    meta: Dict[str, Any] = {
        'initial_mode': pil_img.mode,
        'initial_size': pil_img.size,
        'detected_angle': 0,
        'reason': 'default_upright',
    }

    try:
        from rapidocr_onnxruntime import RapidOCR
        ocr = RapidOCR()
    except Exception as e:
        meta['reason'] = f'ocr_unavailable: {e}'
        return 0, meta

    # Work on a downscaled copy for rapid evaluation
    w, h = pil_img.size
    max_dim = 900
    if max(w, h) > max_dim:
        scale = max_dim / float(max(w, h))
        eval_img = pil_img.resize((int(w * scale), int(h * scale)), Image.Resampling.BILINEAR)
    else:
        eval_img = pil_img.copy()

    eval_np = np.array(eval_img.convert('RGB'))
    eval_h, eval_w = eval_np.shape[:2]

    # Run base OCR on candidate 0°
    base_res, _ = ocr(eval_np)
    if not base_res or len(base_res) < 2:
        meta['reason'] = 'insufficient_text_detected'
        return 0, meta

    layout = analyze_text_line_orientation(base_res)
    meta['base_box_count'] = len(base_res)
    meta['layout'] = layout

    # Case 1: Predominantly Horizontal (Candidate: 0° or 180°)
    if layout == 'HORIZONTAL':
        score_0 = compute_upright_header_score(base_res, eval_h)
        meta['score_0'] = score_0

        # If score is strongly negative (header keywords at bottom, totals at top), page is upside-down
        if score_0 <= -3:
            # Verify 180° candidate
            img_180 = np.array(eval_img.rotate(180).convert('RGB'))
            res_180, _ = ocr(img_180)
            score_180 = compute_upright_header_score(res_180, eval_h)
            meta['score_180'] = score_180

            if score_180 > score_0 + 2:
                meta['detected_angle'] = 180
                meta['reason'] = 'inverted_header_footer_distribution'
                return 180, meta

        # Default to 0°
        meta['detected_angle'] = 0
        meta['reason'] = 'upright_horizontal_layout'
        return 0, meta

    # Case 2: Predominantly Vertical (Candidate: Sideways 90° or 270°)
    if layout == 'VERTICAL':
        # Test 90° clockwise and 270° clockwise
        img_90 = np.array(eval_img.rotate(270, expand=True).convert('RGB'))   # rotate 270 to correct 90 CW
        img_270 = np.array(eval_img.rotate(90, expand=True).convert('RGB'))   # rotate 90 to correct 270 CW

        res_90, _ = ocr(img_90)
        res_270, _ = ocr(img_270)

        score_cor_90 = compute_upright_header_score(res_90, img_90.shape[0])
        score_cor_270 = compute_upright_header_score(res_270, img_270.shape[0])

        meta['score_cor_90'] = score_cor_90
        meta['score_cor_270'] = score_cor_270

        if score_cor_90 > score_cor_270 and score_cor_90 >= 2:
            meta['detected_angle'] = 90
            meta['reason'] = 'sideways_90_detected'
            return 90, meta
        elif score_cor_270 > score_cor_90 and score_cor_270 >= 2:
            meta['detected_angle'] = 270
            meta['reason'] = 'sideways_270_detected'
            return 270, meta
        elif len(res_90 or []) > len(res_270 or []):
            meta['detected_angle'] = 90
            meta['reason'] = 'sideways_90_density'
            return 90, meta
        else:
            meta['detected_angle'] = 270
            meta['reason'] = 'sideways_270_density'
            return 270, meta

    # Ambiguous layout (e.g. handwritten, square, or faded receipt) -> preserve 0° untouched
    meta['detected_angle'] = 0
    meta['reason'] = 'ambiguous_layout_preserved_untouched'
    return 0, meta


def detect_and_correct_image_orientation(
    image_bytes: bytes,
    apply_exif: bool = True
) -> Tuple[bytes, int, Dict[str, Any]]:
    """
    Main entry point for Phase 4:
    1. Inspects and applies EXIF orientation if present.
    2. Runs visual text orientation analysis to detect 90°, 180°, or 270° rotations.
    3. Returns (corrected_bytes, angle_applied, metadata).
    4. If no rotation needed or ambiguous, returns original image_bytes untouched.
    """
    if not image_bytes or len(image_bytes) < 100:
        return image_bytes, 0, {'status': 'empty_or_too_small'}

    try:
        pil_img = Image.open(io.BytesIO(image_bytes))
        meta: Dict[str, Any] = {'original_size': pil_img.size, 'exif_rotated': False}

        # 1. EXIF orientation
        if apply_exif:
            transposed = ImageOps.exif_transpose(pil_img)
            if transposed.size != pil_img.size or getattr(transposed, '_exif_transposed', False):
                meta['exif_rotated'] = True
                pil_img = transposed

        # 2. Text/Visual Orientation Analysis
        needed_angle, ocr_meta = detect_document_rotation_angle(pil_img)
        meta.update(ocr_meta)

        if needed_angle == 0 and not meta['exif_rotated']:
            return image_bytes, 0, meta

        # Apply needed rotation
        if needed_angle == 90:
            # Rotate 270° counter-clockwise (i.e. 90° clockwise correction)
            corrected_img = pil_img.rotate(270, expand=True)
        elif needed_angle == 180:
            corrected_img = pil_img.rotate(180, expand=True)
        elif needed_angle == 270:
            corrected_img = pil_img.rotate(90, expand=True)
        else:
            corrected_img = pil_img

        out_buf = io.BytesIO()
        fmt = pil_img.format or 'JPEG'
        if fmt.upper() not in ('JPEG', 'JPG', 'PNG', 'WEBP'):
            fmt = 'JPEG'

        save_kwargs = {}
        if fmt.upper() in ('JPEG', 'JPG'):
            save_kwargs['quality'] = 92
            save_kwargs['optimize'] = True
            if corrected_img.mode in ('RGBA', 'P'):
                corrected_img = corrected_img.convert('RGB')

        corrected_img.save(out_buf, format=fmt, **save_kwargs)
        return out_buf.getvalue(), needed_angle, meta

    except Exception as e:
        logger.error("Orientation detection failed (failsafe preserves original bytes): %s", str(e))
        return image_bytes, 0, {'status': 'error', 'error': str(e)}
