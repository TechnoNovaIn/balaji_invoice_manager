"""
Test Suite for:
1. Nearby Object Elimination & Document Boundary Auto-Crop Engine.
2. Intelligent Gemini Free API Quota Saver & Zero-Rate-Limit Tiering.
"""
import io
import hashlib
import numpy as np
import pytest
from decimal import Decimal
from PIL import Image
import cv2

from django.test import TestCase, override_settings
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.cache import cache

from apps.accounts.models import User, UserRole, Department
from apps.invoices.models import InvoiceSubmission, InvoiceDocument, UploadedDocument
from apps.invoices.services import create_invoice_submission
from apps.invoices.image_processing import auto_crop_invoice_image, four_point_transform
from apps.invoices.reader import extract_invoice_metadata
from apps.invoices.gemini_extractor import extract_with_gemini


class TestNearbyObjectsAndApiSaver(TestCase):
    def setUp(self):
        cache.clear()
        self.dept = Department.objects.create(name="Operations", code="OPS")
        self.employee = User.objects.create_user(
            username="emp_kishore",
            password="TestPassword@2026",
            role=UserRole.EMPLOYEE,
            first_name="Kishore",
            last_name="Kumar",
            department=self.dept,
        )
        self.naveen = User.objects.create_user(
            username="naveen_evaluator",
            password="TestPassword@2026",
            role=UserRole.NAVEEN,
            first_name="Naveen",
            last_name="Accounts",
            department=self.dept,
        )

        # Generate a simulated photo: invoice on dark desk surrounded by nearby objects
        # Dimensions: 1200 x 1600
        canvas = np.full((1200, 1600, 3), (35, 45, 55), dtype=np.uint8)  # dark desk
        # Add peripheral objects: coffee mug, pen, mobile phone
        cv2.circle(canvas, (200, 200), 90, (20, 20, 130), -1)  # coffee mug
        cv2.rectangle(canvas, (1350, 120), (1550, 150), (10, 10, 10), -1)  # pen
        cv2.rectangle(canvas, (100, 850), (280, 1150), (60, 60, 60), -1)  # phone

        # Invoice sheet placed angled in the center (occupies ~32% of frame)
        paper_pts = np.array([[450, 250], [1200, 210], [1260, 990], [420, 1040]], dtype=np.int32)
        cv2.fillPoly(canvas, [paper_pts], (245, 248, 250))
        cv2.putText(canvas, "KARNATAKA TYRE CARE CENTRE", (500, 360), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (10, 10, 10), 2)
        cv2.putText(canvas, "GSTIN: 29AQOPB7117M1ZE", (500, 440), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (10, 10, 10), 2)
        cv2.putText(canvas, "INVOICE NO: 191", (500, 520), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (10, 10, 10), 2)
        cv2.putText(canvas, "DATE: 2026-11-10", (500, 600), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (10, 10, 10), 2)
        cv2.putText(canvas, "TOTAL AMOUNT: Rs. 1062.00", (500, 680), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (10, 10, 10), 2)

        pil_img = Image.fromarray(cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB))
        buf = io.BytesIO()
        pil_img.save(buf, format="JPEG", quality=95)
        self.invoice_with_clutter_bytes = buf.getvalue()

    def test_01_nearby_objects_eliminated_and_only_invoice_detected(self):
        """Verify that desks, cups, pens, and nearby objects are trimmed away, leaving only the invoice."""
        processed_bytes, was_cropped, meta = auto_crop_invoice_image(self.invoice_with_clutter_bytes)
        self.assertTrue(was_cropped)
        self.assertEqual(meta.get("method"), "perspective_quad")

        # Original dimensions were 1600x1200
        self.assertEqual(meta.get("original_dims"), [1600, 1200])

        # Cropped image must be isolated to paper boundaries (~800x800)
        out_img = Image.open(io.BytesIO(processed_bytes))
        self.assertEqual(out_img.info.get("dpi"), (300, 300))
        self.assertLess(out_img.width, 1000)
        self.assertLess(out_img.height, 950)

        # Output mean brightness must be bright paper (>200), not the dark desk (which was ~45)
        out_np = np.array(out_img)
        self.assertGreater(float(np.mean(out_np)), 200.0)

    def test_02_already_full_coverage_invoice_preserves_without_clipping(self):
        """Verify an invoice already framed/cropped (>94% coverage) is preserved without re-clipping."""
        clean_paper = np.full((1000, 800, 3), (250, 250, 250), dtype=np.uint8)
        cv2.putText(clean_paper, "ALREADY CROPPED VOUCHER", (100, 200), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (20, 20, 20), 2)
        buf = io.BytesIO()
        Image.fromarray(clean_paper).save(buf, format="JPEG", quality=95)

        processed_bytes, was_cropped, meta = auto_crop_invoice_image(buf.getvalue())
        self.assertFalse(was_cropped)
        self.assertEqual(meta.get("method"), "already_full_coverage")

    def test_03_local_ocr_first_saves_gemini_api_calls(self):
        """Verify local RapidOCR extracts recognized vendor without calling Gemini API."""
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
            tf.write(self.invoice_with_clutter_bytes)
            tmp_path = tf.name

        try:
            # Run extraction. Local RapidOCR will identify KARNATAKA TYRE CARE CENTRE from vendor directory
            meta = extract_invoice_metadata(tmp_path, content_type="image/jpeg")
            self.assertIn("KARNATAKA TYRE", meta.get("party_name", "").upper())
            self.assertEqual(meta.get("party_gstin"), "29AQOPB7117M1ZE")
            self.assertEqual(meta.get("total_amount"), Decimal("1062.00"))
            self.assertGreaterEqual(meta.get("confidence", 0), 0.75)
            # Gemini was NOT called because local extraction was confident and verified
            self.assertIsNone(meta.get("gemini_extracted_data"))
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_04_gemini_response_caching_by_sha256(self):
        """Verify Gemini responses are cached by file SHA-256 to prevent duplicate quota consumption."""
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
            tf.write(self.invoice_with_clutter_bytes)
            tmp_path = tf.name

        try:
            file_sha256 = hashlib.sha256(self.invoice_with_clutter_bytes).hexdigest()
            cache_key = f"gemini_ocr_sha256_{file_sha256}"

            # Pre-seed cache with verified metadata
            cached_dummy = {
                "party_name": "CACHED SREE BALAJI LOGISTICS",
                "party_gstin": "29ABCDE1234F1Z5",
                "invoice_number": "INV-CACHED-99",
                "total_amount": Decimal("5000.00"),
                "confidence": 0.95,
            }
            cache.set(cache_key, cached_dummy, timeout=3600)

            # extract_with_gemini should return cached result instantly with 0 API calls
            res = extract_with_gemini(tmp_path)
            self.assertIsNotNone(res)
            self.assertEqual(res.get("party_name"), "CACHED SREE BALAJI LOGISTICS")
            self.assertEqual(res.get("invoice_number"), "INV-CACHED-99")
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_05_pro_escalation_disabled_by_default(self):
        """Verify GEMINI_ENABLE_PRO_ESCALATION is False by default to protect 2 RPM free limits."""
        from django.conf import settings
        enable_pro = getattr(settings, 'GEMINI_ENABLE_PRO_ESCALATION', False)
        self.assertFalse(enable_pro)
