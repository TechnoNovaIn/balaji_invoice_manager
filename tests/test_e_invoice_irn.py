import base64
import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pymupdf as fitz
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from apps.accounts.models import Department, UserRole
from apps.invoices.e_invoice_utils import (
    _decode_jwt_or_json_qr,
    evaluate_e_invoice_status,
    extract_irn_from_text,
    is_valid_irn,
)
from apps.invoices.gemini_extractor import extract_with_gemini
from apps.invoices.models import InvoiceSubmission, PurchaseAuditTrail, UploadedDocument
from apps.invoices.reader import extract_invoice_metadata, extract_metadata_from_text
from apps.invoices.services import create_invoice_submissions
from apps.invoices.tasks import process_uploaded_document_task

User = get_user_model()

VALID_IRN_1 = "9F86D081884C7D659A2FEAA0C55AD015A3BF4F1B2B0B822CD15D6C15B0F00A08"
VALID_IRN_2 = "A1B2C3D4E5F60718293A4B5C6D7E8F90123456789ABCDEF0123456789ABCDEF0"


def _make_pdf_bytes(text: str) -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 72), text, fontsize=11)
    raw = doc.tobytes()
    doc.close()
    return raw


class EInvoiceAndIRNTests(TestCase):
    def setUp(self):
        self.dept, _ = Department.objects.get_or_create(code="OPS", defaults={"name": "Operations"})
        self.emp = User.objects.create_user(
            username="einv_emp",
            password="StrongPassword@2026",
            role=UserRole.EMPLOYEE,
            department=self.dept,
            must_change_password=False,
        )
        self.naveen = User.objects.create_user(
            username="einv_naveen",
            password="StrongPassword@2026",
            role=UserRole.NAVEEN,
            department=self.dept,
            must_change_password=False,
        )
        self.mgmt = User.objects.create_user(
            username="einv_mgmt",
            password="StrongPassword@2026",
            role=UserRole.MANAGEMENT,
            department=self.dept,
            must_change_password=False,
        )

    def test_01_einvoice_with_valid_irn_extraction_and_qr(self):
        pdf_bytes = _make_pdf_bytes(
            "TAX INVOICE (e-Invoice)\n"
            "JK TYRE & INDUSTRIES LIMITED\n"
            "GSTIN: 29AAACJ6716F1ZK\n"
            "Invoice No: JKT-EINV-101\n"
            f"IRN: {VALID_IRN_1}\n"
            "Ack No: 112610998877\n"
            "Total Amount: 11800.00"
        )
        pdf_file = SimpleUploadedFile("einv_valid.pdf", pdf_bytes, content_type="application/pdf")
        with patch("apps.invoices.gemini_extractor.extract_with_gemini", return_value=None):
            subs = create_invoice_submissions(user=self.emp, files=[pdf_file])

        self.assertEqual(len(subs), 1)
        sub = InvoiceSubmission.objects.get(pk=subs[0].pk)
        self.assertTrue(sub.is_e_invoice)
        self.assertEqual(sub.irn_number, VALID_IRN_1)
        self.assertEqual(sub.e_invoice_status_label, "YES")
        self.assertEqual(sub.irn_display, VALID_IRN_1)

        # Signed GST JWT QR payload decoding check (offline structural decode, no trusted IRP public key)
        jwt_header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').decode().rstrip("=")
        inner = json.dumps({"Irn": VALID_IRN_2.lower(), "SellerGstin": "29AAACJ6716F1ZK"})
        jwt_body = base64.urlsafe_b64encode(json.dumps({"data": inner}).encode()).decode().rstrip("=")
        qr_parsed = _decode_jwt_or_json_qr(f"{jwt_header}.{jwt_body}.signature")
        self.assertTrue(qr_parsed["has_qr_einvoice_data"])
        self.assertEqual(qr_parsed["valid_irn"], VALID_IRN_2)
        self.assertTrue(qr_parsed["qr_data_extracted"])
        self.assertTrue(qr_parsed["qr_payload_structurally_valid"])
        self.assertFalse(qr_parsed["qr_signature_cryptographically_verified"])
        self.assertEqual(qr_parsed["qr_authenticity_status"], "STRUCTURALLY_VALIDATED")

    def test_02_non_einvoice_explicit_classification(self):
        pdf_bytes = _make_pdf_bytes(
            "CASH / CREDIT BILL\n"
            "JK TYRE & INDUSTRIES LIMITED\n"
            "GSTIN: 29AAACJ6716F1ZK\n"
            "E-Invoice Applicable: No\n"
            "IRN: N/A\n"
            "Invoice No: NON-EINV-202\n"
            "Total Amount: 2500.00"
        )
        pdf_file = SimpleUploadedFile("non_einv.pdf", pdf_bytes, content_type="application/pdf")
        with patch("apps.invoices.gemini_extractor.extract_with_gemini", return_value=None):
            subs = create_invoice_submissions(user=self.emp, files=[pdf_file])

        sub = InvoiceSubmission.objects.get(pk=subs[0].pk)
        self.assertIs(sub.is_e_invoice, False)
        self.assertEqual(sub.irn_number, "")
        self.assertEqual(sub.e_invoice_status_label, "NO")
        self.assertEqual(sub.irn_display, "N/A")

    def test_03_einvoice_missing_irn_flags_manual_verification(self):
        pdf_bytes = _make_pdf_bytes(
            "TAX INVOICE (e-Invoice)\n"
            "JK TYRE & INDUSTRIES LIMITED\n"
            "GSTIN: 29AAACJ6716F1ZK\n"
            "Ack No: 18260000112233\n"
            "Invoice No: EINV-MISSING-303\n"
            "Total Amount: 4500.00"
        )
        pdf_file = SimpleUploadedFile("einv_missing_irn.pdf", pdf_bytes, content_type="application/pdf")
        with patch("apps.invoices.gemini_extractor.extract_with_gemini", return_value=None):
            subs = create_invoice_submissions(user=self.emp, files=[pdf_file])

        sub = InvoiceSubmission.objects.get(pk=subs[0].pk)
        self.assertIs(sub.is_e_invoice, True)
        self.assertEqual(sub.irn_number, "")
        self.assertTrue(sub.requires_manual_review)
        self.assertEqual(sub.irn_display, "IRN Not Found")

        # Attempting 1-click verify without manual review confirmation must be gated
        self.client.force_login(self.naveen)
        resp = self.client.post(reverse("invoices:naveen_verify", kwargs={"submission_id": sub.submission_id}))
        self.assertEqual(resp.status_code, 302)
        sub.refresh_from_db()
        self.assertNotEqual(sub.status, InvoiceSubmission.STATUS_VERIFIED)

    def test_04_invalid_irn_rejected_and_gemini_cannot_hallucinate(self):
        self.assertFalse(is_valid_irn(""))
        self.assertFalse(is_valid_irn("12345ABC"))
        self.assertFalse(is_valid_irn("A" * 64))
        self.assertFalse(is_valid_irn("0" * 64))
        self.assertFalse(is_valid_irn(VALID_IRN_1[:-1]))  # 63 chars
        self.assertFalse(is_valid_irn(VALID_IRN_1 + "A"))  # 65 chars
        self.assertTrue(is_valid_irn(VALID_IRN_1.lower()))

        # Text with explicit IRN label containing invalid short value
        meta = extract_metadata_from_text(
            "Tax Invoice\n"
            "JK TYRE & INDUSTRIES LIMITED\n"
            "GSTIN: 29AAACJ6716F1ZK\n"
            "IRN: 12345-TRUNCATED-IRN\n"
            "Total Amount: 1000.00"
        )
        self.assertIs(meta["is_e_invoice"], True)
        self.assertEqual(meta["irn_number"], "")

        # Gemini hallucinating an IRN absent from digital PDF text is rejected
        eval_res = evaluate_e_invoice_status(
            raw_text="Tax Invoice\nJK TYRE & INDUSTRIES LIMITED\nInvoice No: 500\nTotal Amount: 1200.00",
            gemini_data={"is_e_invoice": True, "irn_number": VALID_IRN_1},
        )
        self.assertEqual(eval_res["irn_number"], "")
        self.assertTrue(eval_res["e_invoice_manual_review"])

    def test_05_uncertain_einvoice_marks_needs_verification(self):
        pdf_bytes = _make_pdf_bytes(
            "TAX INVOICE\n"
            "JK TYRE & INDUSTRIES LIMITED\n"
            "GSTIN: 29AAACJ6716F1ZK\n"
            "Invoice No: PLAIN-404\n"
            "Total Amount: 3200.00"
        )
        up = UploadedDocument.objects.create(
            uploader=self.emp,
            original_file=SimpleUploadedFile("uncertain_bill.pdf", pdf_bytes, "application/pdf"),
            file_name="uncertain_bill.pdf",
            file_sha256="sha_uncertain_einv_404",
            file_size_bytes=len(pdf_bytes),
            content_type="application/pdf",
            processing_status=UploadedDocument.STATUS_QUEUED,
        )
        subs = process_uploaded_document_task(up.id)
        sub = InvoiceSubmission.objects.get(pk=subs[0].pk)
        self.assertIsNone(sub.is_e_invoice)
        self.assertEqual(sub.irn_number, "")
        self.assertEqual(sub.e_invoice_status_label, "NEEDS VERIFICATION")

    def test_06_manual_irn_correction_by_naveen_and_audit_trail(self):
        sub = InvoiceSubmission.objects.create(
            submission_id="BTI-202611-77001",
            employee=self.emp,
            party_name="JK TYRE & INDUSTRIES LIMITED",
            party_gstin="29AAACJ6716F1ZK",
            invoice_number="INV-EDIT-01",
            total_amount=Decimal("5000.00"),
            is_e_invoice=True,
            irn_number="",
            requires_manual_review=True,
        )
        self.client.force_login(self.naveen)
        verify_url = reverse("invoices:verification_workspace", kwargs={"submission_id": sub.submission_id})

        # Invalid IRN submitted manually must be rejected without saving garbage
        self.client.post(verify_url, {
            "action": "save_only",
            "party_name": "JK TYRE & INDUSTRIES LIMITED",
            "party_gstin": "29AAACJ6716F1ZK",
            "invoice_number": "INV-EDIT-01",
            "expense_ledger": "Tyre & Tube Purchase",
            "taxable_amount": "5000.00",
            "total_amount": "5000.00",
            "is_e_invoice": "true",
            "irn_number": "SHORT_FAKE_IRN",
        })
        sub.refresh_from_db()
        self.assertEqual(sub.irn_number, "")

        # Valid 64-char IRN correction saves to PostgreSQL and records PurchaseAuditTrail
        resp = self.client.post(verify_url, {
            "action": "save_only",
            "party_name": "JK TYRE & INDUSTRIES LIMITED",
            "party_gstin": "29AAACJ6716F1ZK",
            "invoice_number": "INV-EDIT-01",
            "expense_ledger": "Tyre & Tube Purchase",
            "taxable_amount": "5000.00",
            "total_amount": "5000.00",
            "is_e_invoice": "true",
            "irn_number": VALID_IRN_1.lower(),
            "audit_reason": "Entered full IRN from original printed invoice",
        })
        self.assertEqual(resp.status_code, 302)

        persisted = InvoiceSubmission.objects.get(submission_id=sub.submission_id)
        self.assertIs(persisted.is_e_invoice, True)
        self.assertEqual(persisted.irn_number, VALID_IRN_1)
        self.assertFalse(persisted.requires_manual_review)
        self.assertTrue(
            PurchaseAuditTrail.objects.filter(
                submission=persisted,
                field_name="IRN Number",
                new_value=VALID_IRN_1,
            ).exists()
        )

    def test_07_and_08_db_persistence_and_pre_migration_existing_invoices(self):
        # Simulate pre-migration existing invoice record (nullable defaults untouched)
        existing_sub = InvoiceSubmission.objects.create(
            submission_id="BTI-202611-77002",
            employee=self.emp,
            party_name="JK TYRE & INDUSTRIES LIMITED",
            party_gstin="29AAACJ6716F1ZK",
            invoice_number="LEGACY-001",
            total_amount=Decimal("2200.00"),
        )
        reloaded = InvoiceSubmission.objects.get(pk=existing_sub.pk)
        self.assertIsNone(reloaded.is_e_invoice)
        self.assertEqual(reloaded.irn_number, "")
        self.assertEqual(reloaded.e_invoice_status_label, "NEEDS VERIFICATION")
        self.assertNotEqual(reloaded.is_e_invoice, False)

    def test_09_rbac_permissions_employee_management_naveen(self):
        sub = InvoiceSubmission.objects.create(
            submission_id="BTI-202611-77003",
            employee=self.emp,
            party_name="JK TYRE & INDUSTRIES LIMITED",
            party_gstin="29AAACJ6716F1ZK",
            invoice_number="RBAC-01",
            total_amount=Decimal("1000.00"),
        )
        # Employee denied on Naveen dashboard and IRN editing endpoints
        self.client.force_login(self.emp)
        self.assertEqual(self.client.get(reverse("core:naveen_dashboard")).status_code, 403)
        self.assertEqual(
            self.client.post(
                reverse("invoices:verification_workspace", kwargs={"submission_id": sub.submission_id}),
                {"is_e_invoice": "false"},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                reverse("invoices:quick_update_metadata", kwargs={"submission_id": sub.submission_id}),
                {"is_e_invoice": "false"},
            ).status_code,
            403,
        )

        # Management strictly read-only on IRN edits
        self.client.force_login(self.mgmt)
        self.assertEqual(
            self.client.post(
                reverse("invoices:verification_workspace", kwargs={"submission_id": sub.submission_id}),
                {"is_e_invoice": "true", "irn_number": VALID_IRN_1},
            ).status_code,
            403,
        )

    def test_10_accounts_dashboard_displays_all_einvoice_and_irn_states(self):
        InvoiceSubmission.objects.create(
            submission_id="BTI-202611-77010",
            employee=self.emp,
            party_name="Vendor Valid EInv",
            invoice_number="INV-E-1",
            total_amount=Decimal("1500.00"),
            is_e_invoice=True,
            irn_number=VALID_IRN_1,
        )
        InvoiceSubmission.objects.create(
            submission_id="BTI-202611-77011",
            employee=self.emp,
            party_name="Vendor Non EInv",
            invoice_number="INV-N-2",
            total_amount=Decimal("600.00"),
            is_e_invoice=False,
            irn_number="",
        )
        InvoiceSubmission.objects.create(
            submission_id="BTI-202611-77012",
            employee=self.emp,
            party_name="Vendor Missing IRN",
            invoice_number="INV-M-3",
            total_amount=Decimal("900.00"),
            is_e_invoice=True,
            irn_number="",
        )
        InvoiceSubmission.objects.create(
            submission_id="BTI-202611-77013",
            employee=self.emp,
            party_name="Vendor Legacy Uncertain",
            invoice_number="INV-U-4",
            total_amount=Decimal("400.00"),
            is_e_invoice=None,
            irn_number="",
        )

        self.client.force_login(self.naveen)
        resp = self.client.get(reverse("core:naveen_dashboard"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        self.assertIn("E-Invoice?", html)
        self.assertIn("IRN Number", html)
        self.assertIn("bg-success", html)
        self.assertIn("bg-secondary", html)
        self.assertIn("bg-warning", html)
        self.assertIn("YES", html)
        self.assertIn("NO", html)
        self.assertIn("NEEDS VERIFICATION", html)
        self.assertIn(VALID_IRN_1, html)
        self.assertIn("copyIrnToClipboard", html)
        self.assertIn("N/A", html)
        self.assertIn("IRN Not Found", html)

        # Search filter matches IRN
        search_resp = self.client.get(reverse("core:naveen_dashboard"), {"q": VALID_IRN_1[:16]})
        self.assertEqual(search_resp.status_code, 200)
        search_html = search_resp.content.decode("utf-8")
        self.assertIn("INV-E-1", search_html)
        self.assertNotIn("INV-N-2", search_html)

    def test_11_irn_hex_zero_and_boundary_validation(self):
        # Valid SHA-256 hex IRN containing many '0' digits (including leading zeros)
        zero_heavy_irn = ("0" * 30) + "1234567890ABCDEF" + ("0" * 18)
        self.assertEqual(len(zero_heavy_irn), 64)
        self.assertTrue(is_valid_irn(zero_heavy_irn))
        text_zero = extract_irn_from_text(f"Tax Invoice (e-Invoice)\nIRN: {zero_heavy_irn}\nTotal: 5000")
        self.assertEqual(text_zero["valid_irn"], zero_heavy_irn)

        # Invalid IRNs containing G-Z characters must be rejected
        irn_with_g = VALID_IRN_1[:-1] + "G"
        irn_with_z = "Z" + VALID_IRN_1[1:]
        self.assertFalse(is_valid_irn(irn_with_g))
        self.assertFalse(is_valid_irn(irn_with_z))
        gz_extract = extract_irn_from_text(f"Tax Invoice\nIRN: {irn_with_g}\nTotal: 500")
        self.assertEqual(gz_extract["valid_irn"], "")
        self.assertTrue(gz_extract["invalid_irn_candidate"])

        # Fewer than 64 characters rejected
        self.assertFalse(is_valid_irn(VALID_IRN_1[:63]))
        self.assertFalse(is_valid_irn(VALID_IRN_1[:32]))

        # More than 64 characters rejected (both via validator and text extractor without truncation)
        self.assertFalse(is_valid_irn(VALID_IRN_1 + "0"))
        self.assertFalse(is_valid_irn(VALID_IRN_1 + "G"))
        overlong_extract = extract_irn_from_text(f"Tax Invoice\nIRN: {VALID_IRN_1}AB\nTotal: 500")
        self.assertEqual(overlong_extract["valid_irn"], "")
        self.assertTrue(overlong_extract["invalid_irn_candidate"])

        # Repeated-character filler values rejected
        self.assertFalse(is_valid_irn("0" * 64))
        self.assertFalse(is_valid_irn("A" * 64))
        self.assertFalse(is_valid_irn("F" * 64))
        self.assertFalse(is_valid_irn("01" * 32))
        self.assertFalse(is_valid_irn("AB" * 32))

    def test_12_qr_authenticity_tiers_and_crypto_verification(self):
        # 1. Non-GST plain text QR: extracted only, NOT structurally valid, NOT cryptographically verified
        plain_qr = _decode_jwt_or_json_qr("https://example.com/invoice/12345")
        self.assertTrue(plain_qr["qr_data_extracted"])
        self.assertFalse(plain_qr["qr_payload_structurally_valid"])
        self.assertFalse(plain_qr["qr_signature_cryptographically_verified"])
        self.assertEqual(plain_qr["qr_authenticity_status"], "DATA_EXTRACTED")

        # 2. Valid JWT QR without trusted public key: structurally validated ONLY, NOT cryptographically verified
        jwt_header = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').decode().rstrip("=")
        inner = json.dumps({"Irn": VALID_IRN_1, "SellerGstin": "29AAACJ6716F1ZK"})
        jwt_body = base64.urlsafe_b64encode(json.dumps({"data": inner}).encode()).decode().rstrip("=")
        unverified_jwt = f"{jwt_header}.{jwt_body}.fakesig"
        res_unverified = _decode_jwt_or_json_qr(unverified_jwt)
        self.assertTrue(res_unverified["qr_data_extracted"])
        self.assertTrue(res_unverified["qr_payload_structurally_valid"])
        self.assertFalse(res_unverified["qr_signature_cryptographically_verified"])
        self.assertEqual(res_unverified["qr_authenticity_status"], "STRUCTURALLY_VALIDATED")

        eval_unverified = evaluate_e_invoice_status(raw_text="", qr_data=res_unverified)
        self.assertTrue(eval_unverified["qr_data_extracted"])
        self.assertTrue(eval_unverified["qr_payload_structurally_valid"])
        self.assertFalse(eval_unverified["qr_signature_cryptographically_verified"])
        self.assertIn("not cryptographically verified", eval_unverified["notes"].lower())

        # 3. Genuine RS256 signed JWT with matching vs wrong trusted public key
        import jwt as pyjwt
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        priv_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        priv_pem = priv_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        pub_pem = priv_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")

        wrong_pub_pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")

        signed_jwt = pyjwt.encode({"data": inner}, priv_pem, algorithm="RS256")
        verified = _decode_jwt_or_json_qr(signed_jwt, trusted_public_key=pub_pem)
        self.assertTrue(verified["qr_data_extracted"])
        self.assertTrue(verified["qr_payload_structurally_valid"])
        self.assertTrue(verified["qr_signature_cryptographically_verified"])
        self.assertEqual(verified["qr_authenticity_status"], "CRYPTOGRAPHICALLY_VERIFIED")

        wrong_key_res = _decode_jwt_or_json_qr(signed_jwt, trusted_public_key=wrong_pub_pem)
        self.assertTrue(wrong_key_res["qr_data_extracted"])
        self.assertTrue(wrong_key_res["qr_payload_structurally_valid"])
        self.assertFalse(wrong_key_res["qr_signature_cryptographically_verified"])
        self.assertEqual(wrong_key_res["qr_authenticity_status"], "STRUCTURALLY_VALIDATED")

    def test_13_missing_or_na_irn_does_not_mark_non_einvoice(self):
        # Missing IRN (blank field) with no explicit non-e-invoice statement -> NEEDS VERIFICATION (None)
        blank_irn_res = evaluate_e_invoice_status(
            raw_text="TAX INVOICE\nJK TYRE & INDUSTRIES LIMITED\nGSTIN: 29AAACJ6716F1ZK\nIRN:\nTotal Amount: 5000.00"
        )
        self.assertIsNone(blank_irn_res["is_e_invoice"])
        self.assertEqual(blank_irn_res["irn_number"], "")
        self.assertTrue(blank_irn_res["e_invoice_manual_review"])

        # "IRN: N/A" or "IRN: Nil" alone -> NEEDS VERIFICATION (None), NOT Non-E-Invoice (False)
        for na_label in ("IRN: N/A", "IRN: NA", "IRN: Nil", "IRN: None", "IRN: Not Applicable"):
            na_res = evaluate_e_invoice_status(
                raw_text=f"TAX INVOICE\nJK TYRE & INDUSTRIES LIMITED\n{na_label}\nTotal Amount: 2000.00"
            )
            self.assertIsNone(na_res["is_e_invoice"], f"{na_label} must yield NEEDS VERIFICATION (None), got {na_res}")
            self.assertEqual(na_res["irn_number"], "")
            self.assertTrue(na_res["e_invoice_manual_review"])

        # Uncertain text + Gemini attempting to guess False must still yield NEEDS VERIFICATION (None)
        uncertain_res = evaluate_e_invoice_status(
            raw_text="TAX INVOICE\nSupplier: Local Workshop\nIRN: N/A\nTotal Amount: 1200.00",
            gemini_data={"is_e_invoice": False, "irn_number": None},
        )
        self.assertIsNone(uncertain_res["is_e_invoice"])
        self.assertTrue(uncertain_res["e_invoice_manual_review"])


