import io
from decimal import Decimal
from django.test import TestCase
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.invoices.models import VendorMaster
from apps.invoices.vendor_validator import (
    validate_vendor_and_financials,
    is_valid_gstin,
    extract_pan_from_gstin,
    clean_gstin,
    normalize_vendor_name,
)
from apps.invoices.vendor_directory import (
    find_vendor_by_gstin,
    find_vendor_by_name,
    find_vendor_by_pan,
    import_vendors_from_excel_or_csv,
    VENDOR_DIRECTORY,
)


class VendorValidatorAndMasterTestSuite(TestCase):
    """
    Test suite verifying the Authoritative Vendor Master database,
    vendor validation rules (A, B, C, D, E), and Excel/CSV import capabilities.
    """

    def setUp(self):
        # Create sample verified vendor
        self.vm1 = VendorMaster.objects.create(
            name="TEST LOGISTICS SUPPLIES",
            gstin="29ABCDE1234F1Z5",
            pan="ABCDE1234F",
            state="Karnataka",
            state_code="29",
            default_expense_ledger="Purchase Spares & Consumables",
            trade_aliases=["TEST LOGISTICS", "TLS KARNATAKA"],
            is_active=True,
            is_verified=True,
        )

    def test_gstin_helper_functions(self):
        """Verify statutory Indian GSTIN format validation and PAN extraction."""
        self.assertTrue(is_valid_gstin("29AAACJ6716F1ZK"))
        self.assertFalse(is_valid_gstin("INVALID_GSTIN"))
        self.assertFalse(is_valid_gstin("29AAACJ6716F1"))  # 13 chars
        self.assertEqual(clean_gstin(" 29-AAACJ6716F1ZK "), "29AAACJ6716F1ZK")
        self.assertEqual(extract_pan_from_gstin("29AAACJ6716F1ZK"), "AAACJ6716F")
        self.assertEqual(normalize_vendor_name("M/s J.K. Tyre & Industries Ltd."), "msjktyreindustriesltd")

    def test_rule_a_known_vendor_matching_gstin(self):
        """Rule A: Known vendor with matching GSTIN -> MATCHED_VERIFIED, financial approval eligible."""
        val = validate_vendor_and_financials(
            party_name="TEST LOGISTICS SUPPLIES",
            party_gstin="29ABCDE1234F1Z5"
        )
        self.assertEqual(val['vendor_match_status'], 'MATCHED_VERIFIED')
        self.assertFalse(val['is_ambiguous_vendor'])
        self.assertFalse(val['requires_manual_review'])
        self.assertTrue(val['is_financial_approval_eligible'])
        self.assertEqual(val['confidence'], 0.98)
        self.assertEqual(val['expense_ledger'], 'Purchase Spares & Consumables')

    def test_rule_b_known_vendor_no_gstin(self):
        """Rule B: Known vendor without GSTIN -> KNOWN_VENDOR_NO_GSTIN, requires manual review."""
        val = validate_vendor_and_financials(
            party_name="TEST LOGISTICS SUPPLIES",
            party_gstin=""
        )
        self.assertEqual(val['vendor_match_status'], 'KNOWN_VENDOR_NO_GSTIN')
        self.assertFalse(val['is_ambiguous_vendor'])
        self.assertTrue(val['requires_manual_review'])
        self.assertFalse(val['is_financial_approval_eligible'])
        self.assertEqual(val['party_gstin'], "29ABCDE1234F1Z5")

    def test_rule_c_unknown_vendor_no_gstin(self):
        """Rule C: Unknown vendor without GSTIN -> UNKNOWN_VENDOR_NO_GSTIN, ambiguous, review strictly required."""
        val = validate_vendor_and_financials(
            party_name="Roadside Dhaba & Puncture Shop",
            party_gstin=""
        )
        self.assertEqual(val['vendor_match_status'], 'UNKNOWN_VENDOR_NO_GSTIN')
        self.assertTrue(val['is_ambiguous_vendor'])
        self.assertTrue(val['requires_manual_review'])
        self.assertFalse(val['is_financial_approval_eligible'])
        self.assertLessEqual(val['confidence'], 0.85)

    def test_rule_d_unknown_vendor_with_valid_gstin(self):
        """Rule D: Unknown vendor with statutory GSTIN -> UNREGISTERED_VENDOR_VALID_GSTIN."""
        val = validate_vendor_and_financials(
            party_name="New Highway Spares Store",
            party_gstin="29XYZAB5678C1Z9"
        )
        self.assertEqual(val['vendor_match_status'], 'UNREGISTERED_VENDOR_VALID_GSTIN')
        self.assertFalse(val['is_ambiguous_vendor'])
        self.assertTrue(val['requires_manual_review'])
        self.assertFalse(val['is_financial_approval_eligible'])
        self.assertEqual(val['confidence'], 0.90)

    def test_rule_e_gstin_mismatch(self):
        """Rule E: GSTIN Mismatch -> GSTIN_MISMATCH, never silently overwrite registered GSTIN."""
        # Voucher has vendor name TEST LOGISTICS SUPPLIES (registered 29ABCDE1234F1Z5),
        # but voucher has different GSTIN 29AAACJ6716F1ZK (JK Tyre)
        val = validate_vendor_and_financials(
            party_name="TEST LOGISTICS SUPPLIES",
            party_gstin="29AAACJ6716F1ZK"
        )
        self.assertEqual(val['vendor_match_status'], 'GSTIN_MISMATCH')
        self.assertTrue(val['is_ambiguous_vendor'])
        self.assertTrue(val['requires_manual_review'])
        self.assertFalse(val['is_financial_approval_eligible'])
        self.assertIn("GSTIN Mismatch", val['audit_notes'])
        # Original bill GSTIN is preserved without silent overwrite
        self.assertEqual(val['party_gstin'], "29AAACJ6716F1ZK")

    def test_database_vendor_master_lookups(self):
        """Test database VendorMaster lookup by GSTIN, name, and PAN."""
        v_gstin = find_vendor_by_gstin("29ABCDE1234F1Z5")
        self.assertIsNotNone(v_gstin)
        self.assertEqual(v_gstin['name'], "TEST LOGISTICS SUPPLIES")

        v_name = find_vendor_by_name("Test Logistics Supplies")
        self.assertIsNotNone(v_name)
        self.assertEqual(v_name['gstin'], "29ABCDE1234F1Z5")

        v_pan = find_vendor_by_pan("ABCDE1234F")
        self.assertIsNotNone(v_pan)
        self.assertEqual(v_pan['name'], "TEST LOGISTICS SUPPLIES")

    def test_import_vendors_from_csv(self):
        """Test CSV import of vendor master records with duplicate detection."""
        csv_content = (
            "vendor_name,gstin,pan,state,ledger,address\n"
            "SRI GANESH BATTERIES,29AAIFG1234K1Z2,AAIFG1234K,Karnataka,Purchase Spares & Consumables,Hubli Road\n"
            "TEST LOGISTICS SUPPLIES,29ABCDE1234F1Z5,ABCDE1234F,Karnataka,Purchase Spares & Consumables,Bangalore\n"  # Duplicate
            "BAD GSTIN VENDOR,INVALID_GST_CODE,,,General Office Expenses,\n"  # Invalid GSTIN
        )

        csv_file = io.StringIO(csv_content)
        report = import_vendors_from_excel_or_csv(csv_file, filename="test_vendors.csv", update_existing=False)

        self.assertEqual(report['total_rows'], 3)
        self.assertEqual(report['created_count'], 1)  # SRI GANESH BATTERIES
        self.assertEqual(report['skipped_count'], 1)  # TEST LOGISTICS SUPPLIES (duplicate, preserved)
        self.assertEqual(report['error_count'], 1)    # BAD GSTIN VENDOR

        # Verify SRI GANESH BATTERIES was persisted
        self.assertTrue(VendorMaster.objects.filter(gstin="29AAIFG1234K1Z2").exists())

