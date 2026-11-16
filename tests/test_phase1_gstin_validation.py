"""
Phase 1 Unit Tests: Statutory Indian GSTIN Validation & Mod-36 Checksum Verification
Balaji InvoiceHub
"""
import pytest
from apps.invoices.vendor_validator import (
    calculate_gstin_checksum,
    verify_gstin_detailed,
    is_valid_gstin,
    validate_vendor_and_financials,
    VALID_GST_STATE_CODES,
    PAN_ENTITY_TYPES,
)
from apps.invoices.models import VendorMaster, InvoiceSubmission


@pytest.mark.django_db
class TestPhase1GstinValidation:

    def test_mod36_checksum_algorithm_known_vectors(self):
        """Test statutory Modulo 36 checksum calculation against official known GSTINs."""
        # 1. 29APFPN9602K1Z -> F
        assert calculate_gstin_checksum('29APFPN9602K1Z') == 'F'

        # 2. 29AAXFA9222F1Z -> 1
        assert calculate_gstin_checksum('29AAXFA9222F1Z') == '1'

        # 3. 29AALPA1711H1Z -> 2
        assert calculate_gstin_checksum('29AALPA1711H1Z') == '2'

        # 4. 29AJKPD8139L2Z -> N
        assert calculate_gstin_checksum('29AJKPD8139L2Z') == 'N'

        # 5. 08AAXCA1693H1Z -> 4
        assert calculate_gstin_checksum('08AAXCA1693H1Z') == '4'

        # 6. 08AABCA0522B1Z -> K
        assert calculate_gstin_checksum('08AABCA0522B1Z') == 'K'

        # 7. 29AAIFA2641P1Z -> 2
        assert calculate_gstin_checksum('29AAIFA2641P1Z') == '2'

        # 8. 29ABCPE1234F1Z -> 7
        assert calculate_gstin_checksum('29ABCPE1234F1Z') == '7'

        # Invalid length or non-alphanumeric should return None
        assert calculate_gstin_checksum('29ABCDE1234F1') is None
        assert calculate_gstin_checksum('29ABCDE1234F1$') is None
        assert calculate_gstin_checksum('') is None
        assert calculate_gstin_checksum(None) is None

    def test_verify_gstin_detailed_structure_verified(self):
        """Valid statutory GSTIN returns 'GSTIN Structure Verified' and registration disclaimer."""
        res = verify_gstin_detailed('29APFPN9602K1ZF')
        assert res['is_valid'] is True
        assert res['status_code'] == 'STRUCTURE_VERIFIED'
        assert res['display_status'] == 'GSTIN Structure Verified'
        assert res['portal_status'] == 'GSTIN Registration Not Verified'
        assert res['expected_checksum'] == 'F'
        assert res['actual_checksum'] == 'F'
        assert res['state_code'] == '29'
        assert res['pan'] == 'APFPN9602K'

    def test_verify_gstin_detailed_checksum_invalid(self):
        """GSTIN with wrong check digit returns 'GSTIN Checksum Invalid'."""
        # 29APFPN9602K1ZF has checksum F. Let's provide 9 instead.
        res = verify_gstin_detailed('29APFPN9602K1Z9')
        assert res['is_valid'] is False
        assert res['status_code'] == 'CHECKSUM_INVALID'
        assert res['display_status'] == 'GSTIN Checksum Invalid'
        assert res['portal_status'] == 'GSTIN Registration Not Verified'
        assert res['expected_checksum'] == 'F'
        assert res['actual_checksum'] == '9'
        assert 'checksum mismatch' in res['error_message'].lower()

    def test_verify_gstin_detailed_invalid_format(self):
        """GSTIN with invalid format/length returns 'GSTIN Format Invalid'."""
        res_short = verify_gstin_detailed('29APFPN9602K')
        assert res_short['is_valid'] is False
        assert res_short['status_code'] == 'FORMAT_INVALID'
        assert res_short['display_status'] == 'GSTIN Format Invalid'

        res_chars = verify_gstin_detailed('29APFPN9602K1Z$')
        assert res_chars['is_valid'] is False
        assert res_chars['status_code'] == 'FORMAT_INVALID'

    def test_verify_gstin_detailed_invalid_state_code(self):
        """GSTIN with invalid state code (e.g. 00, 39, 98) returns format error."""
        res = verify_gstin_detailed('00APFPN9602K1ZF')
        assert res['is_valid'] is False
        assert res['status_code'] == 'FORMAT_INVALID'
        assert res['display_status'] == 'GSTIN Format Invalid'
        assert 'state code' in res['error_message'].lower()

    def test_verify_gstin_detailed_invalid_pan_entity(self):
        """GSTIN with invalid 4th PAN entity character (must be CPHFATBLJG) is rejected."""
        # 4th char of PAN is 'X', not a valid Indian entity type
        res = verify_gstin_detailed('29ABXDE1234F1Z7')
        assert res['is_valid'] is False
        assert res['status_code'] == 'FORMAT_INVALID'
        assert res['display_status'] == 'GSTIN Format Invalid'
        assert 'entity type' in res['error_message'].lower()

    def test_is_valid_gstin_helper(self):
        """Helper returns boolean, enforcing checksum by default."""
        assert is_valid_gstin('29APFPN9602K1ZF') is True
        assert is_valid_gstin('29APFPN9602K1Z9') is False
        # If check_checksum=False, format check passes even with wrong checksum
        assert is_valid_gstin('29APFPN9602K1Z9', check_checksum=False) is True
        assert is_valid_gstin(None) is False
        assert is_valid_gstin('') is False

    def test_validate_vendor_and_financials_checksum_invalid_unregistered(self):
        """Unknown vendor with invalid checksum is flagged as UNREGISTERED_VENDOR_CHECKSUM_INVALID."""
        val = validate_vendor_and_financials(
            party_name='Unknown Hardware Traders',
            party_gstin='29ABCPE1234F1Z9',  # valid PAN entity 'P', but invalid checksum (expected 7, got 9)
        )
        assert val['vendor_match_status'] == 'UNREGISTERED_VENDOR_CHECKSUM_INVALID'
        assert val['is_ambiguous_vendor'] is True
        assert val['requires_manual_review'] is True
        assert val['is_financial_approval_eligible'] is False
        assert val['confidence'] == 0.65
        assert 'invalid Mod-36 checksum' in val['audit_notes']
        assert val['gstin_status'] == 'GSTIN Checksum Invalid'
        assert val['gstin_portal_status'] == 'GSTIN Registration Not Verified'

    def test_validate_vendor_and_financials_valid_unregistered(self):
        """Unknown vendor with valid checksum is flagged as UNREGISTERED_VENDOR_VALID_GSTIN."""
        val = validate_vendor_and_financials(
            party_name='Brand New Vendor Logistics',
            party_gstin='29ABCPE1234F1Z7',  # valid statutory GSTIN with calculated checksum 7
        )
        assert val['vendor_match_status'] == 'UNREGISTERED_VENDOR_VALID_GSTIN'
        assert val['is_ambiguous_vendor'] is False
        assert val['requires_manual_review'] is True
        assert val['is_financial_approval_eligible'] is False
        assert val['confidence'] == 0.90
        assert val['gstin_status'] == 'GSTIN Structure Verified'
        assert val['gstin_portal_status'] == 'GSTIN Registration Not Verified'

    def test_audit_all_existing_vendormaster_records(self):
        """
        Audit all VendorMaster records populated from database migrations against Mod-36 checksum.
        Ensures existing vendor master is preserved without modifications.
        Documents known records with optical discrepancies (e.g. Kwality Furnishings).
        """
        all_vendors = list(VendorMaster.objects.all())
        assert len(all_vendors) >= 237, f"Expected at least 237 VendorMaster records, found {len(all_vendors)}"

        valid_count = 0
        empty_count = 0
        mismatch_records = []

        for v in all_vendors:
            gstin = (v.gstin or '').strip().upper()
            if not gstin:
                empty_count += 1
                continue

            details = verify_gstin_detailed(gstin)
            if details['is_valid']:
                valid_count += 1
            else:
                mismatch_records.append({
                    'id': v.id,
                    'name': v.name,
                    'gstin': gstin,
                    'expected': details.get('expected_checksum'),
                    'actual': details.get('actual_checksum'),
                    'error': details.get('error_message')
                })

        # Exactly 1 discrepancy in canonical vendor master (KWALITY FURNISHINGS)
        assert len(mismatch_records) == 1
        kwality = mismatch_records[0]
        assert kwality['name'] == 'KWALITY FURNISHINGS'
        assert kwality['gstin'] == '29AAQPF8511J1ZX'
        assert kwality['expected'] == 'B'
        assert kwality['actual'] == 'X'

        # Verify that matching Kwality Furnishings by registered GSTIN preserves vendor mapping
        match_result = validate_vendor_and_financials(
            party_name='Kwality Furnishings',
            party_gstin='29AAQPF8511J1ZX'
        )
        assert match_result['vendor_match_status'] == 'MATCHED_VERIFIED'
        assert match_result['party_name'] == 'KWALITY FURNISHINGS'
        assert 'checksum discrepancy' in match_result['audit_notes']
