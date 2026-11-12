import io
import os
import tempfile
from decimal import Decimal
from unittest.mock import patch

import cv2
import numpy as np
import pymupdf as fitz
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from PIL import Image, ImageDraw

from apps.accounts.models import Department, UserRole
from apps.invoices.document_type_utils import (
    DOC_TYPE_HANDWRITTEN,
    DOC_TYPE_MIXED,
    DOC_TYPE_NEEDS_REVIEW,
    DOC_TYPE_PRINTED,
    classify_document_type,
)
from apps.invoices.e_invoice_utils import (
    EINV_STATUS_HANDWRITTEN,
    EINV_STATUS_NEEDS_VERIFICATION,
    EINV_STATUS_NO,
    EINV_STATUS_YES,
    apply_e_invoice_to_submission,
    evaluate_e_invoice_status,
)
from apps.invoices.gemini_extractor import extract_with_gemini
from apps.invoices.models import InvoiceSubmission, PurchaseAuditTrail
from apps.invoices.reader import extract_invoice_metadata
from apps.invoices.services import create_invoice_submissions

User = get_user_model()

VALID_IRN = "9F86D081884C7D659A2FEAA0C55AD015A3BF4F1B2B0B822CD15D6C15B0F00A08"


def _make_digital_pdf(text: str) -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 72), text, fontsize=11)
    raw = doc.tobytes()
    doc.close()
    return raw


def _make_printed_bill_image(path: str) -> None:
    """Create a clean, sharp machine-printed style image with uniform rows."""
    img = np.full((700, 550, 3), 255, dtype=np.uint8)
    for row in range(60, 620, 42):
        for col in range(40, 490, 18):
            cv2.rectangle(img, (col, row), (col + 10, row + 16), (0, 0, 0), -1)
    cv2.imwrite(path, img)


def _make_handwritten_bill_image(path: str) -> None:
    """Create a synthetic image mimicking large curved/irregular handwritten strokes."""
    img = Image.new("RGB", (600, 750), "white")
    draw = ImageDraw.Draw(img)
    # Irregular cursive loops & varying heights
    for idx, y_base in enumerate(range(90, 650, 75)):
        pts = [
            (45 + i * 28, y_base + ((i * (idx + 3)) % 24) - 10)
            for i in range(16)
        ]
        draw.line(pts, fill="black", width=4 + (idx % 3))
        draw.ellipse((50 + idx * 12, y_base - 18, 125 + idx * 12, y_base + 18), outline="black", width=4)
    img.save(path, format="PNG")


def _make_mixed_bill_image(path: str) -> None:
    """Create a pre-printed bordered bill book form with handwritten curved entries."""
    img = Image.new("RGB", (600, 750), "white")
    draw = ImageDraw.Draw(img)
    # Crisp printed borders & horizontal header rules
    draw.rectangle((30, 30, 570, 700), outline="black", width=3)
    for y in (120, 210, 560):
        draw.line((30, y, 570, y), fill="black", width=3)
    draw.line((380, 210, 380, 560), fill="black", width=2)
    # Handwritten large strokes in body & amount box
    for idx, y_base in enumerate((260, 350, 440, 595)):
        pts = [(60 + i * 24, y_base + ((i * 7 + idx * 5) % 22) - 8) for i in range(10)]
        draw.line(pts, fill="black", width=4)
        draw.ellipse((410, y_base - 16, 520, y_base + 16), outline="black", width=4)
    img.save(path, format="PNG")


def _make_blank_unclear_image(path: str) -> None:
    """Create a tiny/low-variance unreadable image."""
    img = np.full((80, 80, 3), 245, dtype=np.uint8)
    cv2.imwrite(path, img)


class DocumentTypeClassificationTests(TestCase):
    def setUp(self):
        self.dept, _ = Department.objects.get_or_create(code="OPS", defaults={"name": "Operations"})
        self.emp = User.objects.create_user(
            username="dtype_emp",
            password="StrongPassword@2026",
            role=UserRole.EMPLOYEE,
            department=self.dept,
            must_change_password=False,
        )
        self.naveen = User.objects.create_user(
            username="dtype_naveen",
            password="StrongPassword@2026",
            role=UserRole.NAVEEN,
            department=self.dept,
            must_change_password=False,
        )
        self.mgmt = User.objects.create_user(
            username="dtype_mgmt",
            password="StrongPassword@2026",
            role=UserRole.MANAGEMENT,
            department=self.dept,
            must_change_password=False,
        )

    def test_01_fully_handwritten_invoice(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            img_path = os.path.join(tmpdir, "handwritten.png")
            _make_handwritten_bill_image(img_path)
            fake_ocr = [([[40, 80], [200, 80], [200, 120], [40, 120]], "Cash", 0.42)]
            res = classify_document_type(
                file_path=img_path,
                raw_text="Cash 450",
                ocr_res=fake_ocr,
                content_type="image/png",
            )
            self.assertEqual(res["document_type"], DOC_TYPE_HANDWRITTEN)

    def test_02_fully_printed_invoice(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            img_path = os.path.join(tmpdir, "printed.png")
            _make_printed_bill_image(img_path)
            fake_ocr = [
                ([[40, 60], [450, 60], [450, 76], [40, 76]], "TAX INVOICE", 0.96),
                ([[40, 102], [450, 102], [450, 118], [40, 118]], "JK TYRE & INDUSTRIES LIMITED", 0.95),
                ([[40, 144], [450, 144], [450, 160], [40, 160]], "GSTIN 29AAACJ6716F1ZK TOTAL 11800.00", 0.94),
            ]
            res_img = classify_document_type(
                file_path=img_path,
                raw_text="TAX INVOICE\nJK TYRE & INDUSTRIES LIMITED\nGSTIN 29AAACJ6716F1ZK TOTAL 11800.00",
                ocr_res=fake_ocr,
                content_type="image/png",
            )
            self.assertEqual(res_img["document_type"], DOC_TYPE_PRINTED)

            pdf_path = os.path.join(tmpdir, "printed.pdf")
            with open(pdf_path, "wb") as f:
                f.write(_make_digital_pdf("TAX INVOICE\nJK TYRE & INDUSTRIES LIMITED\nTotal Amount: 11800.00"))
            res_pdf = classify_document_type(file_path=pdf_path, content_type="application/pdf")
            self.assertEqual(res_pdf["document_type"], DOC_TYPE_PRINTED)

    def test_03_mixed_handwritten_and_printed_invoice(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            img_path = os.path.join(tmpdir, "mixed.png")
            _make_mixed_bill_image(img_path)
            fake_ocr = [
                ([[40, 55], [300, 55], [300, 75], [40, 75]], "SHREE MOTORS - CASH BILL", 0.94),
                ([[60, 260], [280, 260], [280, 290], [60, 290]], "Diesel hose repair", 0.51),
            ]
            res = classify_document_type(
                file_path=img_path,
                raw_text="SHREE MOTORS - CASH BILL\nDiesel hose repair 850",
                ocr_res=fake_ocr,
                content_type="image/png",
            )
            self.assertEqual(res["document_type"], DOC_TYPE_MIXED)

    def test_04_unclear_document_marks_needs_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tiny_path = os.path.join(tmpdir, "tiny.png")
            _make_blank_unclear_image(tiny_path)
            res = classify_document_type(file_path=tiny_path, raw_text="", content_type="image/png")
            self.assertEqual(res["document_type"], DOC_TYPE_NEEDS_REVIEW)

            # Empty/blank PDF -> NEEDS_REVIEW
            blank_pdf = os.path.join(tmpdir, "blank.pdf")
            doc = fitz.open()
            doc.new_page(width=595, height=842)
            doc.save(blank_pdf)
            doc.close()
            res_pdf = classify_document_type(file_path=blank_pdf, content_type="application/pdf")
            self.assertEqual(res_pdf["document_type"], DOC_TYPE_NEEDS_REVIEW)

    def test_05_handwritten_invoice_without_einvoice_evidence_displays_handwritten_and_na(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            img_path = os.path.join(tmpdir, "hw_bill.png")
            _make_handwritten_bill_image(img_path)
            with open(img_path, "rb") as f:
                img_bytes = f.read()

        upload = SimpleUploadedFile("hw_bill.png", img_bytes, content_type="image/png")
        mock_gemini = {
            "party_name": "Roadside Mechanic",
            "invoice_number": "105",
            "total_amount": Decimal("850.00"),
            "taxable_amount": Decimal("850.00"),
            "cgst_amount": Decimal("0.00"),
            "sgst_amount": Decimal("0.00"),
            "igst_amount": Decimal("0.00"),
            "document_type": DOC_TYPE_HANDWRITTEN,
            "has_handwritten_amounts": True,
            "is_e_invoice": None,
            "irn_number": "",
            "confidence": 0.65,
            "requires_manual_review": True,
            "is_ambiguous_vendor": True,
            "expense_ledger": "Vehicle Repairs & Maintenance",
        }
        with patch("apps.invoices.gemini_extractor.extract_with_gemini", return_value=mock_gemini):
            subs = create_invoice_submissions(user=self.emp, files=[upload])

        self.assertEqual(len(subs), 1)
        sub = InvoiceSubmission.objects.get(pk=subs[0].pk)
        self.assertEqual(sub.e_invoice_status, EINV_STATUS_HANDWRITTEN)
        # Handwritten invoice is NOT assumed legally non-e-invoice
        self.assertIsNone(sub.is_e_invoice)
        self.assertEqual(sub.irn_number, "")
        self.assertEqual(sub.irn_display, "N/A")
        self.assertTrue(sub.requires_manual_review)

        # Handwritten 1-click verify without manual review confirmation is blocked on safeguard, NOT IRN
        self.client.force_login(self.naveen)
        self.client.post(reverse("invoices:naveen_verify", kwargs={"submission_id": sub.submission_id}))
        sub.refresh_from_db()
        self.assertNotEqual(sub.status, InvoiceSubmission.STATUS_VERIFIED)

        # Confirming manual review on Verification Desk allows approval WITHOUT requiring an IRN
        resp = self.client.post(
            reverse("invoices:verification_workspace", kwargs={"submission_id": sub.submission_id}),
            {
                "action": "verify",
                "party_name": "Roadside Mechanic",
                "party_gstin": "",
                "invoice_number": "105",
                "expense_ledger": "Vehicle Repairs & Maintenance",
                "taxable_amount": "850.00",
                "cgst_amount": "0.00",
                "sgst_amount": "0.00",
                "igst_amount": "0.00",
                "total_amount": "850.00",
                "e_invoice_status": EINV_STATUS_HANDWRITTEN,
                "irn_number": "",
                "confirm_manual_review": "on",
            },
        )
        self.assertEqual(resp.status_code, 302)
        sub.refresh_from_db()
        self.assertEqual(sub.status, InvoiceSubmission.STATUS_VERIFIED)
        self.assertEqual(sub.e_invoice_status, EINV_STATUS_HANDWRITTEN)
        self.assertIsNone(sub.is_e_invoice)
        self.assertEqual(sub.irn_display, "N/A")

    def test_06_printed_einvoice_with_valid_irn_and_contradictory_hw_with_irn(self):
        pdf_bytes = _make_digital_pdf(
            "TAX INVOICE (e-Invoice)\n"
            "JK TYRE & INDUSTRIES LIMITED\n"
            "GSTIN: 29AAACJ6716F1ZK\n"
            "Invoice No: PRINT-EINV-01\n"
            f"IRN: {VALID_IRN}\n"
            "Total Amount: 11800.00"
        )
        pdf_file = SimpleUploadedFile("printed_einv.pdf", pdf_bytes, content_type="application/pdf")
        with patch("apps.invoices.gemini_extractor.extract_with_gemini", return_value=None):
            subs = create_invoice_submissions(user=self.emp, files=[pdf_file])

        sub = InvoiceSubmission.objects.get(pk=subs[0].pk)
        self.assertEqual(sub.e_invoice_status, EINV_STATUS_YES)
        self.assertIs(sub.is_e_invoice, True)
        self.assertEqual(sub.irn_number, VALID_IRN)
        self.assertEqual(sub.irn_display, VALID_IRN)

        # Handwritten appearance + contradictory IRN/QR evidence MUST display YES, NOT HANDWRITTEN
        hw_with_irn = evaluate_e_invoice_status(
            raw_text=f"Cash Bill\nIRN: {VALID_IRN}",
            appearance_data={"document_type": DOC_TYPE_HANDWRITTEN, "confidence": 0.90},
        )
        self.assertEqual(hw_with_irn["e_invoice_status"], EINV_STATUS_YES)
        self.assertIs(hw_with_irn["is_e_invoice"], True)
        self.assertEqual(hw_with_irn["irn_number"], VALID_IRN)

    def test_07_printed_non_einvoice_and_missing_irn_and_conflicting_evidence(self):
        pdf_bytes = _make_digital_pdf(
            "RETAIL INVOICE\n"
            "JK TYRE & INDUSTRIES LIMITED\n"
            "GSTIN: 29AAACJ6716F1ZK\n"
            "E-Invoice Applicable: No\n"
            "Invoice No: PRINT-REG-02\n"
            "Total Amount: 4200.00"
        )
        pdf_file = SimpleUploadedFile("printed_non_einv.pdf", pdf_bytes, content_type="application/pdf")
        with patch("apps.invoices.gemini_extractor.extract_with_gemini", return_value=None):
            subs = create_invoice_submissions(user=self.emp, files=[pdf_file])

        sub = InvoiceSubmission.objects.get(pk=subs[0].pk)
        self.assertEqual(sub.e_invoice_status, EINV_STATUS_NO)
        self.assertIs(sub.is_e_invoice, False)
        self.assertEqual(sub.irn_number, "")
        self.assertEqual(sub.irn_display, "N/A")

        # E-Invoice missing IRN -> YES with manual review flag & IRN Not Found
        missing_irn_eval = evaluate_e_invoice_status(
            raw_text="TAX INVOICE (e-Invoice)\nSigned QR Code present, Ack No: 123456",
            appearance_data={"document_type": DOC_TYPE_PRINTED, "confidence": 0.90},
        )
        self.assertEqual(missing_irn_eval["e_invoice_status"], EINV_STATUS_YES)
        self.assertIs(missing_irn_eval["is_e_invoice"], True)
        self.assertEqual(missing_irn_eval["irn_number"], "")

        # Conflicting evidence (explicitly marked Non-e-invoice yet QR/explicit e-invoice cues) -> NEEDS VERIFICATION
        conflicting = evaluate_e_invoice_status(
            raw_text="Tax Invoice E-Invoice Applicable: No",
            gemini_data={"is_e_invoice": True, "irn_number": ""},
            appearance_data={"document_type": DOC_TYPE_PRINTED, "confidence": 0.85},
        )
        self.assertEqual(conflicting["e_invoice_status"], EINV_STATUS_NEEDS_VERIFICATION)
        self.assertIsNone(conflicting["is_e_invoice"])

    def test_08_09_10_dashboard_manual_correction_audit_and_non_overwrite(self):
        sub = InvoiceSubmission.objects.create(
            submission_id="BTI-202611-88001",
            employee=self.emp,
            party_name="JK TYRE & INDUSTRIES LIMITED",
            party_gstin="29AAACJ6716F1ZK",
            invoice_number="MIX-88001",
            total_amount=Decimal("5900.00"),
            e_invoice_status=EINV_STATUS_NEEDS_VERIFICATION,
            is_e_invoice=None,
            irn_number="",
        )
        self.client.force_login(self.naveen)

        # Invalid combined status must be rejected without mutating DB
        bad_resp = self.client.post(
            reverse("invoices:quick_update_metadata", kwargs={"submission_id": sub.submission_id}),
            {"e_invoice_status": "INVALID_STATE"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(bad_resp.status_code, 400)
        sub.refresh_from_db()
        self.assertEqual(sub.e_invoice_status, EINV_STATUS_NEEDS_VERIFICATION)

        # Conflicting HANDWRITTEN + IRN must be rejected
        conflict_resp = self.client.post(
            reverse("invoices:quick_update_metadata", kwargs={"submission_id": sub.submission_id}),
            {"e_invoice_status": EINV_STATUS_HANDWRITTEN, "irn_number": VALID_IRN},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(conflict_resp.status_code, 400)

        # Naveen corrects E-Invoice? status to HANDWRITTEN directly from Accounts Dashboard modal
        ok_resp = self.client.post(
            reverse("invoices:quick_update_metadata", kwargs={"submission_id": sub.submission_id}),
            {"e_invoice_status": EINV_STATUS_HANDWRITTEN, "irn_number": ""},
        )
        self.assertEqual(ok_resp.status_code, 302)

        persisted = InvoiceSubmission.objects.get(submission_id=sub.submission_id)
        self.assertEqual(persisted.e_invoice_status, EINV_STATUS_HANDWRITTEN)
        self.assertIsNone(persisted.is_e_invoice)
        self.assertTrue(persisted.e_invoice_manually_verified)
        self.assertEqual(persisted.irn_display, "N/A")

        # Audit trail captures old value, new value, user, and timestamp
        audit = PurchaseAuditTrail.objects.filter(
            submission=persisted,
            field_name="E-Invoice Status",
        ).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.old_value, "NEEDS VERIFICATION")
        self.assertEqual(audit.new_value, "HANDWRITTEN")
        self.assertEqual(audit.changed_by, self.naveen)
        self.assertIsNotNone(audit.changed_at)

        # Subsequent OCR/Gemini re-evaluation must NOT overwrite Naveen's manual correction!
        apply_e_invoice_to_submission(
            persisted,
            metadata={"e_invoice_status": EINV_STATUS_NO, "is_e_invoice": False, "irn_number": ""},
        )
        persisted.save()
        persisted.refresh_from_db()
        self.assertEqual(persisted.e_invoice_status, EINV_STATUS_HANDWRITTEN)
        self.assertTrue(persisted.e_invoice_manually_verified)

    def test_11_rbac_permissions_for_combined_e_invoice_status(self):
        sub = InvoiceSubmission.objects.create(
            submission_id="BTI-202611-88002",
            employee=self.emp,
            party_name="Vendor X",
            invoice_number="RBAC-DT-1",
            total_amount=Decimal("1000.00"),
            e_invoice_status=EINV_STATUS_NO,
            is_e_invoice=False,
        )
        self.client.force_login(self.emp)
        self.assertEqual(
            self.client.post(
                reverse("invoices:quick_update_metadata", kwargs={"submission_id": sub.submission_id}),
                {"e_invoice_status": EINV_STATUS_HANDWRITTEN},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                reverse("invoices:tally_workspace", kwargs={"submission_id": sub.submission_id}),
                {"e_invoice_status": EINV_STATUS_HANDWRITTEN},
            ).status_code,
            403,
        )

        self.client.force_login(self.mgmt)
        self.assertEqual(
            self.client.post(
                reverse("invoices:verification_workspace", kwargs={"submission_id": sub.submission_id}),
                {"e_invoice_status": EINV_STATUS_HANDWRITTEN},
            ).status_code,
            403,
        )
        sub.refresh_from_db()
        self.assertEqual(sub.e_invoice_status, EINV_STATUS_NO)

    def test_12_accounts_dashboard_combines_handwritten_into_einvoice_column_and_removes_invoice_type(self):
        for idx, (einv_status, einv_bool, irn) in enumerate(
            [
                (EINV_STATUS_HANDWRITTEN, None, ""),
                (EINV_STATUS_YES, True, VALID_IRN),
                (EINV_STATUS_NO, False, ""),
                (EINV_STATUS_NEEDS_VERIFICATION, None, ""),
                (EINV_STATUS_YES, True, ""),
            ],
            start=1,
        ):
            InvoiceSubmission.objects.create(
                submission_id=f"BTI-202611-8810{idx}",
                employee=self.emp,
                party_name=f"Vendor {einv_status} {idx}",
                invoice_number=f"INV-COMB-{idx}",
                total_amount=Decimal("1200.00"),
                e_invoice_status=einv_status,
                is_e_invoice=einv_bool,
                irn_number=irn,
            )

        self.client.force_login(self.naveen)
        resp = self.client.get(reverse("core:naveen_dashboard"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        # Separate "Invoice Type" column & dropdown must NOT exist anywhere
        self.assertNotIn("Invoice Type", html)
        self.assertNotIn('name="document_type"', html)
        # Combined E-Invoice? column and all 4 statuses + IRN behaviors must render
        self.assertIn("E-Invoice?", html)
        self.assertIn("IRN Number", html)
        self.assertIn("HANDWRITTEN", html)
        self.assertIn("YES", html)
        self.assertIn("NO", html)
        self.assertIn("NEEDS VERIFICATION", html)
        self.assertIn("N/A", html)
        self.assertIn(VALID_IRN, html)
        self.assertIn("IRN Not Found", html)
        self.assertIn("copyIrnToClipboard", html)
        self.assertIn("openEInvoiceEditModal", html)
        # Normal invoice numbers must remain separate from IRN
        self.assertIn("INV-COMB-1", html)

        # Verification & Tally workspaces also expose combined E-Invoice? without Invoice Type
        verify_html = self.client.get(
            reverse("invoices:verification_workspace", kwargs={"submission_id": "BTI-202611-88101"})
        ).content.decode("utf-8")
        self.assertNotIn("Invoice Type", verify_html)
        self.assertNotIn('name="document_type"', verify_html)
        self.assertIn('name="e_invoice_status"', verify_html)

        tally_html = self.client.get(
            reverse("invoices:tally_workspace", kwargs={"submission_id": "BTI-202611-88101"})
        ).content.decode("utf-8")
        self.assertNotIn("Invoice Type", tally_html)
        self.assertNotIn('name="document_type"', tally_html)
        self.assertIn('name="e_invoice_status"', tally_html)

    def test_13_financial_accuracy_words_and_reconciliation_safeguards(self):
        from apps.invoices.document_type_utils import audit_submission_financials, has_unclear_financial_values
        from apps.invoices.gemini_extractor import _parse_indian_amount_words, _reconcile_extracted_amounts

        # Indian Rupees in words parsing
        self.assertEqual(
            _parse_indian_amount_words("Four Lakh Twenty Nine Thousand Five Hundred Twenty Rupees Only"),
            Decimal("429520.00"),
        )

        # Recover 10x dropped trailing zeros using amount_in_words and flag for verification
        amounts, warns = _reconcile_extracted_amounts({
            "taxable_amount": 36400.0,
            "cgst_amount": 3276.0,
            "sgst_amount": 3276.0,
            "igst_amount": 0.0,
            "total_amount": 42952.0,
            "amount_in_words": "Four Lakh Twenty Nine Thousand Five Hundred Twenty Rupees Only",
            "items": [{"quantity": 14, "unit_price": 26000, "amount": 364000}],
        })
        self.assertEqual(amounts["taxable_amount"], Decimal("364000.00"))
        self.assertEqual(amounts["cgst_amount"], Decimal("32760.00"))
        self.assertEqual(amounts["sgst_amount"], Decimal("32760.00"))
        self.assertEqual(amounts["total_amount"], Decimal("429520.00"))
        self.assertTrue(any("10x" in w for w in warns))

        # Flag asymmetrical GST and arithmetic mismatches in backend audit
        bad_sub = InvoiceSubmission.objects.create(
            submission_id="BTI-202611-88999",
            employee=self.emp,
            party_name="S.F. & SONS",
            invoice_number="228-ERR",
            taxable_amount=Decimal("1000.00"),
            cgst_amount=Decimal("90.00"),
            sgst_amount=Decimal("0.00"),
            total_amount=Decimal("1200.00"),
        )
        sub_warns = audit_submission_financials(bad_sub)
        self.assertTrue(has_unclear_financial_values(bad_sub))
        self.assertTrue(len(sub_warns) >= 2)
        self.client.force_login(self.naveen)
        ws_html = self.client.get(
            reverse("invoices:verification_workspace", kwargs={"submission_id": bad_sub.submission_id})
        ).content.decode("utf-8")
        self.assertIn("Financial Discrepancies Detected", ws_html)

    def test_14_mixed_preprinted_handwritten_bill_without_gemini_sets_handwritten(self):
        # Pre-printed bill books with handwritten entries classify as MIXED in local OCR (gemini_data=None)
        res = evaluate_e_invoice_status(
            raw_text="KARNATAKA TYRE CARE CENTRE\nBill No 2305\nTotal 1062.00",
            qr_data={},
            gemini_data=None,
            appearance_data={"document_type": DOC_TYPE_MIXED, "confidence": 0.82},
        )
        self.assertEqual(res["e_invoice_status"], EINV_STATUS_HANDWRITTEN)
        self.assertIsNone(res["is_e_invoice"])
        self.assertEqual(res["irn_number"], "")
        self.assertTrue(res["e_invoice_manual_review"])

