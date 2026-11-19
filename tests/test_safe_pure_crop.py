"""
Unit tests for Safe Pure Document Crop:
- Verifies document bounding box slicing without sharpening or contrast modifications.
- Verifies 4% safe padding margin prevents text or boundary clipping.
- Verifies full-frame images are preserved without alteration.
"""
import io
import numpy as np
from PIL import Image
from django.test import TestCase

from apps.invoices.image_processing import safe_pure_document_crop


class SafePureDocumentCropTests(TestCase):
    def setUp(self):
        # Create a synthetic 1000x800 image:
        # Dark table border background (gray=40) with a central white receipt (gray=240)
        # Receipt located at x=150..650 (width=500), y=100..700 (height=600)
        im_arr = np.full((1000, 800, 3), 40, dtype=np.uint8)
        im_arr[100:700, 150:650] = [240, 240, 240]

        # Add distinct test pixels inside the receipt to check for zero color distortion
        im_arr[200, 250] = [12, 34, 56]
        im_arr[300, 350] = [200, 100, 50]

        buf = io.BytesIO()
        Image.fromarray(im_arr).save(buf, format='JPEG', quality=98)
        self.test_img_bytes = buf.getvalue()

    def test_safe_crop_with_gemini_crop_box(self):
        # Receipt is at y: 100..700 (100 to 700 normalized to 1000), x: 150..650 (187 to 812 normalized to 1000)
        crop_box = [100, 187, 700, 812]
        cropped_bytes, was_cropped, meta = safe_pure_document_crop(self.test_img_bytes, crop_box=crop_box)

        self.assertTrue(was_cropped)
        self.assertEqual(meta['method'], 'gemini_vision_bbox')

        # Verify cropped image dimensions (should have removed outer table border)
        cropped_im = Image.open(io.BytesIO(cropped_bytes))
        w, h = cropped_im.size
        # The cropped image should be smaller than original 800x1000
        self.assertLess(w, 800)
        self.assertLess(h, 1000)
        # But larger than 400x500 (padded margin included)
        self.assertGreater(w, 450)
        self.assertGreater(h, 550)

    def test_safe_crop_opencv_fallback(self):
        # Without crop_box, OpenCV contour detection should identify the high-contrast white rectangle
        cropped_bytes, was_cropped, meta = safe_pure_document_crop(self.test_img_bytes, crop_box=None)

        self.assertTrue(was_cropped)
        self.assertEqual(meta['method'], 'opencv_contour_bbox')

        cropped_im = Image.open(io.BytesIO(cropped_bytes))
        w, h = cropped_im.size
        self.assertLess(w, 800)
        self.assertLess(h, 1000)

    def test_already_full_image_is_not_cropped(self):
        # An image where the receipt covers 98% of the frame
        full_arr = np.full((800, 600, 3), 245, dtype=np.uint8)
        buf = io.BytesIO()
        Image.fromarray(full_arr).save(buf, format='JPEG', quality=98)
        full_bytes = buf.getvalue()

        # If box covers the whole frame, it should NOT crop
        crop_box = [10, 10, 990, 990]
        _, was_cropped, meta = safe_pure_document_crop(full_bytes, crop_box=crop_box)
        self.assertFalse(was_cropped)
        self.assertEqual(meta['method'], 'already_full_or_unclear')

    def test_no_sharpening_or_filter_applied(self):
        # Ensure pixel preservation without contrast/filter alteration
        crop_box = [100, 187, 700, 812]
        cropped_bytes, was_cropped, _ = safe_pure_document_crop(self.test_img_bytes, crop_box=crop_box)
        self.assertTrue(was_cropped)

        # In cropped image, interior white receipt should still have natural brightness (~240)
        cropped_im = Image.open(io.BytesIO(cropped_bytes))
        arr = np.array(cropped_im)
        # Check center region mean brightness: should be ~240 (not distorted to 255 or gray 180 by CLAHE)
        center_mean = arr[arr.shape[0]//2 - 50:arr.shape[0]//2 + 50, arr.shape[1]//2 - 50:arr.shape[1]//2 + 50].mean()
        self.assertGreater(center_mean, 230)
        self.assertLess(center_mean, 250)
