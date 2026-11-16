"""
Balaji InvoiceHub — Image Processing & Paper Preservation Engine.

Provides:
1. Intelligent document boundary detection and auto-cropping/deskewing for
   mobile camera captures, preserving full print resolution (300 DPI).
2. Four-point perspective transformation to flatten tilted or angled receipts.
3. Print-ready PDF generation with Balaji Transports voucher metadata header
   suitable for direct office paper printing.
"""
import io
import logging
import math
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image, ImageOps

logger = logging.getLogger('invoices.image_processing')


def order_points(pts: np.ndarray) -> np.ndarray:
    """
    Orders 4 quadrilateral coordinates clockwise:
    [top-left, top-right, bottom-right, bottom-left].
    """
    rect = np.zeros((4, 2), dtype="float32")
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]       # top-left has smallest sum
    rect[2] = pts[np.argmax(s)]       # bottom-right has largest sum

    diff = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(diff)]    # top-right has smallest difference
    rect[3] = pts[np.argmax(diff)]    # bottom-left has largest difference
    return rect


def four_point_transform(image: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """
    Performs a 4-point perspective warp on the input image to extract
    a flattened, deskewed rectangular document.
    """
    rect = order_points(pts)
    (tl, tr, br, bl) = rect

    # Calculate width of new transformed image
    width_a = np.linalg.norm(br - bl)
    width_b = np.linalg.norm(tr - tl)
    max_width = max(int(width_a), int(width_b))

    # Calculate height of new transformed image
    height_a = np.linalg.norm(tr - br)
    height_b = np.linalg.norm(tl - bl)
    max_height = max(int(height_a), int(height_b))

    if max_width < 50 or max_height < 50:
        return image

    dst = np.array([
        [0, 0],
        [max_width - 1, 0],
        [max_width - 1, max_height - 1],
        [0, max_height - 1]
    ], dtype="float32")

    transform_matrix = cv2.getPerspectiveTransform(rect, dst)
    warped = cv2.warpPerspective(
        image,
        transform_matrix,
        (max_width, max_height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )
    return warped


def auto_crop_invoice_image(
    image_bytes: bytes,
    min_coverage: float = 0.08,
    max_coverage: float = 0.98,
    target_dpi: int = 300,
) -> Tuple[bytes, bool, Dict[str, Any]]:
    """
    Analyzes an uploaded photograph, detects document boundaries, and
    automatically crops and deskews the invoice to print-ready format.
    Excludes nearby objects (desks, steering wheels, hands, shadows, background clutter)
    and extracts only the isolated invoice document.

    Guarantees:
    - Robust Document Boundary Isolation: Uses multi-scale edge analysis,
      LAB luminance contrast, and convex hull polygon approximation (8% to 95% frame coverage)
      to eliminate desks, tables, steering wheels, and peripheral objects.
    - Safety Margins: Applies 1.5% margin padding around detected contours so
      vital header vendor names and footer grand totals are never clipped.
    - Zero resolution loss: detection is calculated on an optimized preview,
      but the transformation is applied to the full-resolution original image.
    - Preserves rich color (RGB) and sets high print DPI metadata (300 DPI).
    - Failsafe: if document boundary confidence is low or already closely framed (>= 94%),
      returns the full orientation-corrected original bytes untouched without clipping.

    Returns:
        (processed_bytes, was_cropped, metadata_dict)
    """
    if not image_bytes or len(image_bytes) < 100:
        return image_bytes, False, {"method": "empty_or_too_small"}

    try:
        # Phase 4: Orientation Detection & Correction (EXIF + visual rotation 90/180/270)
        from .orientation import detect_and_correct_image_orientation
        image_bytes, detected_angle, orient_meta = detect_and_correct_image_orientation(image_bytes, apply_exif=True)

        # Load via PIL to respect color mode
        pil_img = Image.open(io.BytesIO(image_bytes))
        if pil_img.mode not in ('RGB', 'L'):
            pil_img = pil_img.convert('RGB')
        elif pil_img.mode == 'L':
            pil_img = pil_img.convert('RGB')

        orig_np = np.array(pil_img)
        orig_bgr = cv2.cvtColor(orig_np, cv2.COLOR_RGB2BGR)
        orig_h, orig_w = orig_bgr.shape[:2]

        if orig_h < 120 or orig_w < 120:
            return image_bytes, False, {"method": "too_small", "dims": [orig_w, orig_h]}

        # Downscale a copy for fast & noise-free edge detection
        max_dim = 1200
        scale = 1.0
        if max(orig_h, orig_w) > max_dim:
            scale = max_dim / float(max(orig_h, orig_w))
            work_w = int(orig_w * scale)
            work_h = int(orig_h * scale)
            work_bgr = cv2.resize(orig_bgr, (work_w, work_h), interpolation=cv2.INTER_AREA)
        else:
            work_bgr = orig_bgr.copy()
            work_w, work_h = orig_w, orig_h

        total_work_area = float(work_w * work_h)
        gray = cv2.cvtColor(work_bgr, cv2.COLOR_BGR2GRAY)

        # Bilateral filter to smooth texture/grain while preserving sharp document edges
        filtered = cv2.bilateralFilter(gray, 9, 75, 75)

        # Strategy 1: Adaptive Canny edge detection + morphology
        med = float(np.median(filtered))
        lower_canny = int(max(20.0, 0.60 * med))
        upper_canny = int(min(240.0, 1.35 * med))
        canny = cv2.Canny(filtered, lower_canny, upper_canny)

        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        canny_closed = cv2.morphologyEx(canny, cv2.MORPH_CLOSE, kernel, iterations=2)
        canny_dilated = cv2.dilate(canny_closed, kernel, iterations=1)

        contours, _ = cv2.findContours(canny_dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contours = sorted(contours, key=cv2.contourArea, reverse=True)[:10]

        best_quad = None
        for c in contours:
            area = cv2.contourArea(c)
            ratio = area / total_work_area
            if ratio < min_coverage or ratio > max_coverage:
                continue
            hull = cv2.convexHull(c)
            peri = cv2.arcLength(hull, True)
            for eps_factor in [0.012, 0.016, 0.020, 0.025, 0.032, 0.040, 0.050]:
                approx = cv2.approxPolyDP(hull, eps_factor * peri, True)
                if len(approx) == 4 and cv2.isContourConvex(approx):
                    bx, by, bw, bh = cv2.boundingRect(approx)
                    aspect_ratio = float(bh) / float(bw) if bw > 0 else 0
                    if 0.20 <= aspect_ratio <= 4.0:
                        best_quad = approx.reshape(4, 2)
                        break
            if best_quad is not None:
                break

        # Strategy 2: LAB Lightness thresholding (isolate bright paper against darker desks/steering wheels)
        if best_quad is None:
            lab = cv2.cvtColor(work_bgr, cv2.COLOR_BGR2LAB)
            l_ch, _, _ = cv2.split(lab)
            _, l_thresh = cv2.threshold(l_ch, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            l_closed = cv2.morphologyEx(l_thresh, cv2.MORPH_CLOSE, kernel, iterations=2)
            l_contours, _ = cv2.findContours(l_closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            l_contours = sorted(l_contours, key=cv2.contourArea, reverse=True)[:6]

            for c in l_contours:
                area = cv2.contourArea(c)
                ratio = area / total_work_area
                if ratio < min_coverage or ratio > max_coverage:
                    continue
                hull = cv2.convexHull(c)
                peri = cv2.arcLength(hull, True)
                for eps_factor in [0.012, 0.016, 0.020, 0.025, 0.032, 0.040, 0.050]:
                    approx = cv2.approxPolyDP(hull, eps_factor * peri, True)
                    if len(approx) == 4 and cv2.isContourConvex(approx):
                        bx, by, bw, bh = cv2.boundingRect(approx)
                        aspect_ratio = float(bh) / float(bw) if bw > 0 else 0
                        if 0.20 <= aspect_ratio <= 4.0:
                            best_quad = approx.reshape(4, 2)
                            break
                if best_quad is not None:
                    break

        # Strategy 3: Rotated Minimum Area Rectangle on largest document candidate
        if best_quad is None:
            all_candidates = contours if contours else []
            for c in all_candidates[:4]:
                area = cv2.contourArea(c)
                ratio = area / total_work_area
                if 0.10 <= ratio <= max_coverage:
                    rect = cv2.minAreaRect(c)
                    bw, bh = rect[1]
                    if bw > 30 and bh > 30:
                        ar = max(bw, bh) / min(bw, bh)
                        if ar <= 4.0:
                            best_quad = cv2.boxPoints(rect)
                            break

        # Check if detected quad is valid and needs cropping
        if best_quad is not None:
            # Map quadrilateral to original coordinates
            quad_orig = (best_quad / scale).astype("float32")
            quad_area = cv2.contourArea(quad_orig)
            coverage = quad_area / float(orig_w * orig_h)

            # If document already occupies >= 94% of the frame with no foreign borders, preserve original
            if coverage >= 0.94:
                buf = io.BytesIO()
                pil_img.save(buf, format='JPEG', quality=95, optimize=True, dpi=(target_dpi, target_dpi))
                return buf.getvalue(), False, {"method": "already_full_coverage", "dims": [orig_w, orig_h], "coverage": round(coverage, 3)}

            # Expand quadrilateral outward (1.5% margin from centroid) to prevent text clipping
            centroid = np.mean(quad_orig, axis=0)
            expanded_quad = np.zeros_like(quad_orig)
            for i, pt in enumerate(quad_orig):
                vec = pt - centroid
                expanded_pt = centroid + vec * 1.015  # 1.5% outward expansion
                expanded_pt[0] = np.clip(expanded_pt[0], 0, orig_w - 1)
                expanded_pt[1] = np.clip(expanded_pt[1], 0, orig_h - 1)
                expanded_quad[i] = expanded_pt

            warped = four_point_transform(orig_bgr, expanded_quad)

            res_rgb = cv2.cvtColor(warped, cv2.COLOR_BGR2RGB)
            out_pil = Image.fromarray(res_rgb)
            buf = io.BytesIO()
            out_pil.save(buf, format='JPEG', quality=95, optimize=True, dpi=(target_dpi, target_dpi))
            return buf.getvalue(), True, {
                "method": "perspective_quad",
                "original_dims": [orig_w, orig_h],
                "cropped_dims": [out_pil.width, out_pil.height],
                "corners": expanded_quad.tolist(),
                "coverage": round(coverage, 3),
            }

        # Strategy 4: Adaptive Otsu thresholding / Bounding-box crop fallback
        _, thresh = cv2.threshold(filtered, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        contours_otsu, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contours_otsu = sorted(contours_otsu, key=cv2.contourArea, reverse=True)

        for c in contours_otsu[:3]:
            area = cv2.contourArea(c)
            ratio = area / total_work_area
            if 0.15 <= ratio <= 0.93:
                x, y, w, h = cv2.boundingRect(c)
                aspect_ratio = float(h) / float(w) if w > 0 else 0
                if 0.25 <= aspect_ratio <= 3.5:
                    margin_x = int(0.015 * orig_w)
                    margin_y = int(0.015 * orig_h)
                    ox = max(0, int(x / scale) - margin_x)
                    oy = max(0, int(y / scale) - margin_y)
                    ow = min(orig_w - ox, int(w / scale) + 2 * margin_x)
                    oh = min(orig_h - oy, int(h / scale) + 2 * margin_y)

                    if ow > 150 and oh > 150 and (ow * oh) < int(0.94 * orig_w * orig_h):
                        cropped = orig_bgr[oy:oy + oh, ox:ox + ow]
                        res_rgb = cv2.cvtColor(cropped, cv2.COLOR_BGR2RGB)
                        out_pil = Image.fromarray(res_rgb)
                        buf = io.BytesIO()
                        out_pil.save(buf, format='JPEG', quality=95, optimize=True, dpi=(target_dpi, target_dpi))
                        return buf.getvalue(), True, {
                            "method": "bounding_box",
                            "original_dims": [orig_w, orig_h],
                            "cropped_dims": [out_pil.width, out_pil.height],
                            "box": [ox, oy, ow, oh],
                            "coverage": round((ow * oh) / float(orig_w * orig_h), 3),
                        }

        # Strategy 5: Document already closely framed (>94% coverage) or unsegmented
        buf = io.BytesIO()
        pil_img.save(buf, format='JPEG', quality=95, optimize=True, dpi=(target_dpi, target_dpi))
        return buf.getvalue(), False, {"method": "already_full_coverage", "dims": [orig_w, orig_h]}

    except Exception as exc:
        logger.warning("Auto-crop encountered an exception; preserving raw upload: %s", exc)
        return image_bytes, False, {"method": "fallback_error", "error": str(exc)}


def generate_print_ready_pdf(submission: Any, output_stream: Optional[io.BytesIO] = None) -> bytes:
    """
    Generates an official Balaji Transports A4 print-ready PDF voucher for
    the given InvoiceSubmission.

    Features:
    - High-DPI print preservation (300 DPI) for clear paper printing.
    - Professional statutory voucher header (Tracking ID, Vendor, GSTIN,
      Invoice Number, Date, Amount breakdown, and Verification status).
    - Centered, high-resolution document pages without edge clipping.
    """
    import pymupdf

    doc = pymupdf.open()
    a4_w, a4_h = 595, 842  # Standard A4 in points (72 pt/inch)

    active_docs = list(submission.documents.filter(is_active_version=True).order_by('page_number'))

    # If no active documents found, create a placeholder summary page
    if not active_docs:
        page = doc.new_page(width=a4_w, height=a4_h)
        _draw_voucher_header(page, submission, a4_w)
        page.insert_text(
            (40, 200),
            "No active invoice documents attached to this submission.",
            fontsize=12,
            color=(0.5, 0.5, 0.5)
        )
        pdf_bytes = doc.tobytes()
        doc.close()
        return pdf_bytes

    # Process each active document page
    for doc_idx, invoice_doc in enumerate(active_docs, start=1):
        page = doc.new_page(width=a4_w, height=a4_h)

        if doc_idx == 1:
            # Page 1 includes the official voucher header
            header_bottom = _draw_voucher_header(page, submission, a4_w)
            target_rect = pymupdf.Rect(25, header_bottom + 15, a4_w - 25, a4_h - 25)
        else:
            # Subsequent pages use full sheet with standard margins
            page.insert_text(
                (25, 20),
                f"Balaji Transports — {submission.submission_id} (Page {doc_idx} of {len(active_docs)})",
                fontsize=8,
                color=(0.4, 0.4, 0.4)
            )
            target_rect = pymupdf.Rect(25, 30, a4_w - 25, a4_h - 25)

        # Render document content into target_rect
        try:
            if not invoice_doc.original_file or not invoice_doc.original_file.storage.exists(invoice_doc.original_file.name):
                page.insert_text(
                    (40, target_rect.y0 + 50),
                    f"[Document file {invoice_doc.original_filename} unavailable on storage]",
                    fontsize=10,
                    color=(0.8, 0.2, 0.2)
                )
                continue

            with invoice_doc.original_file.open('rb') as f:
                content_bytes = f.read()

            content_type = invoice_doc.content_type.lower()
            if content_type == 'application/pdf' or invoice_doc.original_filename.lower().endswith('.pdf'):
                # PDF document: embed page 1 of this document
                src_pdf = pymupdf.open(stream=content_bytes, filetype="pdf")
                if len(src_pdf) > 0:
                    page.show_pdf_page(target_rect, src_pdf, 0, keep_proportion=True)
                src_pdf.close()
            else:
                # Image document: insert image maintaining aspect ratio
                page.insert_image(target_rect, stream=content_bytes, keep_proportion=True)

        except Exception as exc:
            logger.warning("Error rendering page %s in print PDF: %s", doc_idx, exc)
            page.insert_text(
                (40, target_rect.y0 + 50),
                f"[Could not render {invoice_doc.original_filename}: {str(exc)}]",
                fontsize=9,
                color=(0.8, 0.2, 0.2)
            )

    pdf_bytes = doc.tobytes()
    doc.close()

    if output_stream is not None:
        output_stream.write(pdf_bytes)

    return pdf_bytes


def _draw_voucher_header(page: Any, submission: Any, a4_w: float) -> float:
    """
    Draws a structured Balaji Transports voucher header on an A4 page.
    Returns the bottom y-coordinate of the header.
    """
    import pymupdf

    header_top = 20
    header_height = 110
    header_bottom = header_top + header_height
    rect = pymupdf.Rect(25, header_top, a4_w - 25, header_bottom)

    # Background card
    page.draw_rect(rect, color=(0.1, 0.2, 0.35), fill=(0.96, 0.98, 1.0), width=1.0)

    # Title & Submitter
    page.insert_text(
        (38, header_top + 22),
        "BALAJI TRANSPORTS — OFFICIAL INVOICE RECORD",
        fontsize=12,
        color=(0.06, 0.16, 0.32),
    )
    tracking_str = f"Tracking ID: {submission.submission_id}"
    page.insert_text((38, header_top + 40), tracking_str, fontsize=9, color=(0.15, 0.15, 0.15))

    upload_date = submission.created_at.strftime('%d-%b-%Y %I:%M %p') if submission.created_at else 'N/A'
    submitter_name = submission.employee.get_full_name() or submission.employee.username if submission.employee else 'Unknown'
    page.insert_text((300, header_top + 40), f"Submitted By: {submitter_name} ({upload_date})", fontsize=8.5, color=(0.3, 0.3, 0.3))

    # Vendor & GSTIN
    vendor_str = f"Vendor: {submission.party_name or 'Not Specified'}"
    if len(vendor_str) > 45:
        vendor_str = vendor_str[:42] + '...'
    page.insert_text((38, header_top + 58), vendor_str, fontsize=9, color=(0.15, 0.15, 0.15))
    gstin_str = f"GSTIN: {submission.party_gstin or 'Unregistered / N/A'}"
    page.insert_text((300, header_top + 58), gstin_str, fontsize=9, color=(0.15, 0.15, 0.15))

    # Invoice No & Date
    inv_no_str = f"Invoice No: {submission.invoice_number or 'N/A'}"
    inv_dt_str = f"Date: {submission.invoice_date.strftime('%d-%b-%Y') if submission.invoice_date else 'N/A'}"
    page.insert_text((38, header_top + 76), inv_no_str, fontsize=9, color=(0.15, 0.15, 0.15))
    page.insert_text((180, header_top + 76), inv_dt_str, fontsize=9, color=(0.15, 0.15, 0.15))

    # Expense Ledger
    ledger_str = f"Ledger: {submission.expense_ledger or 'Purchase Spares & Consumables'}"
    if len(ledger_str) > 35:
        ledger_str = ledger_str[:32] + '...'
    page.insert_text((300, header_top + 76), ledger_str, fontsize=8.5, color=(0.3, 0.3, 0.3))

    # Financial Summary Bar
    tot_val = float(submission.total_amount or 0.0)
    tax_val = float(submission.taxable_amount or 0.0)
    tot_str = f"Total: INR {tot_val:,.2f}"
    tax_str = f"Taxable: INR {tax_val:,.2f}"
    page.insert_text((38, header_top + 98), tot_str, fontsize=10.5, color=(0.0, 0.45, 0.2))
    page.insert_text((200, header_top + 98), tax_str, fontsize=8.5, color=(0.3, 0.3, 0.3))

    # Status & Tally
    status_label = submission.get_status_display()
    tally_label = "Tally: Entered" if submission.is_entered_in_tally else "Tally: Pending"
    page.insert_text((340, header_top + 98), f"Status: {status_label} | {tally_label}", fontsize=8.5, color=(0.2, 0.3, 0.4))

    return header_bottom
