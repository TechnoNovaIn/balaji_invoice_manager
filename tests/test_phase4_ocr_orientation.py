"""
Phase 4 Unit Tests: OCR Orientation Detection & Safe Upright Correction
Balaji InvoiceHub
"""
import io
import pytest
from PIL import Image, ImageDraw
from apps.invoices.orientation import (
    detect_document_rotation_angle,
    detect_and_correct_image_orientation,
    compute_upright_header_score,
)


def create_sample_invoice_image(w=600, h=800, text_orientation='normal'):
    """Generates a synthetic commercial invoice with distinct header and footer statutory elements."""
    im = Image.new('RGB', (w, h), 'white')
    d = ImageDraw.Draw(im)
    # Header zone (< 40% height)
    d.text((50, 40), 'BALAJI TRANSPORTS - TAX INVOICE', fill='black')
    d.text((50, 75), 'GSTIN: 29AAWFB0395H1ZX', fill='black')
    d.text((50, 110), 'Supplier Bill No: BTI-100234 Date: 16-11-2026', fill='black')
    d.text((50, 145), 'Consignor: Murugan Auto Spares, Bangalore', fill='black')

    # Middle line items
    d.text((50, 300), '1. Heavy Vehicle Engine Oil 15W40 (20L) - 7800.00', fill='black')
    d.text((50, 340), '2. Air Filter & Fuel Filter Replacement - 2400.00', fill='black')

    # Footer zone (> 60% height)
    d.text((50, 680), 'Total Taxable Value: 10200.00', fill='black')
    d.text((50, 715), 'CGST 9%: 918.00  |  SGST 9%: 918.00', fill='black')
    d.text((50, 750), 'Total Amount in Words: Twelve Thousand Thirty Six Rupees', fill='black')
    return im


def create_handwritten_faded_receipt(w=400, h=500):
    """Generates a degraded, faded roadside repair slip without formal statutory headers."""
    im = Image.new('RGB', (w, h), '#f8f8f0')
    d = ImageDraw.Draw(im)
    # Light gray faded scribbles without standard statutory keywords
    d.text((40, 60), 'tyre puncture lorry 24', fill='#888888')
    d.text((40, 120), 'grease and bolt 4 nos', fill='#999999')
    d.text((40, 200), 'paid 450 cash', fill='#888888')
    return im


class TestPhase4OcrOrientation:

    def test_upright_photograph_not_rotated(self):
        """Upright commercial invoice photograph detects angle 0 and preserves original."""
        img = create_sample_invoice_image()
        angle, meta = detect_document_rotation_angle(img)
        assert angle == 0
        assert meta['layout'] == 'HORIZONTAL'
        assert meta['detected_angle'] == 0

        # End-to-end wrapper
        buf = io.BytesIO()
        img.save(buf, format='JPEG')
        corrected_bytes, applied_angle, _ = detect_and_correct_image_orientation(buf.getvalue(), apply_exif=False)
        assert applied_angle == 0
        assert corrected_bytes == buf.getvalue()

    def test_upside_down_photograph_rotated_180(self):
        """Upside-down (180°) invoice photograph is detected and rotated back to upright."""
        img = create_sample_invoice_image().rotate(180)
        angle, meta = detect_document_rotation_angle(img)
        assert angle == 180
        assert meta['layout'] == 'HORIZONTAL'

        buf = io.BytesIO()
        img.save(buf, format='JPEG')
        corrected_bytes, applied_angle, _ = detect_and_correct_image_orientation(buf.getvalue(), apply_exif=False)
        assert applied_angle == 180

        # Verify corrected image is now upright
        corrected_img = Image.open(io.BytesIO(corrected_bytes))
        new_angle, _ = detect_document_rotation_angle(corrected_img)
        assert new_angle == 0

    def test_sideways_photograph_rotated_90(self):
        """Sideways (90° clockwise) invoice photograph is detected as vertical and rotated."""
        img = create_sample_invoice_image().rotate(90, expand=True)
        angle, meta = detect_document_rotation_angle(img)
        assert angle in (90, 270)
        assert meta['layout'] == 'VERTICAL'

        buf = io.BytesIO()
        img.save(buf, format='JPEG')
        corrected_bytes, applied_angle, _ = detect_and_correct_image_orientation(buf.getvalue(), apply_exif=False)
        assert applied_angle in (90, 270)

        # Verify corrected image has horizontal layout
        corrected_img = Image.open(io.BytesIO(corrected_bytes))
        assert corrected_img.size[1] >= corrected_img.size[0]  # height > width for standard invoice

    def test_photograph_without_exif_orientation(self):
        """Images without EXIF metadata (e.g. sent through chat or web export) are safely processed."""
        img = create_sample_invoice_image()
        buf = io.BytesIO()
        # Save bare JPEG with zero EXIF tags
        img.save(buf, format='JPEG')
        assert 'exif' not in img.info

        corrected_bytes, angle, meta = detect_and_correct_image_orientation(buf.getvalue(), apply_exif=True)
        assert angle == 0
        assert meta['exif_rotated'] is False

    def test_faded_and_handwritten_invoice_safety_guard(self):
        """
        Faded, handwritten, or ambiguous receipts without strong statutory signals
        MUST NOT be rotated spuriously. Default to 0°.
        """
        faded_img = create_handwritten_faded_receipt()
        angle, meta = detect_document_rotation_angle(faded_img)
        # Safety guard: must preserve original 0° orientation
        assert angle == 0

        buf = io.BytesIO()
        faded_img.save(buf, format='JPEG')
        corrected_bytes, applied_angle, _ = detect_and_correct_image_orientation(buf.getvalue(), apply_exif=True)
        assert applied_angle == 0
        assert corrected_bytes == buf.getvalue()
