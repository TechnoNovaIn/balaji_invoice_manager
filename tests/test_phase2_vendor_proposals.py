"""
Phase 2 Unit Tests: Vendor Proposal & Controlled Approval Workflow
Balaji InvoiceHub
"""
import pytest
from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from apps.invoices.models import (
    VendorMaster,
    VendorProposal,
    VendorAuditLog,
    InvoiceSubmission,
)
from apps.invoices.vendor_service import (
    create_or_get_vendor_proposal,
    approve_vendor_proposal,
    reject_vendor_proposal,
)

User = get_user_model()


@pytest.mark.django_db
class TestPhase2VendorProposalWorkflow:

    @pytest.fixture(autouse=True)
    def setup_users(self):
        self.naveen = User.objects.create_user(
            username='naveen_accounts',
            role='NAVEEN',
            is_staff=True,
            first_name='Naveen',
            last_name='Kumar'
        )
        self.employee = User.objects.create_user(
            username='driver_rajesh',
            role='EMPLOYEE',
            first_name='Rajesh',
        )

    def test_create_or_get_vendor_proposal_unregistered(self):
        """Unregistered vendor detected on invoice generates a PENDING proposal with checksum status."""
        submission = InvoiceSubmission.objects.create(
            submission_id='BTI-202611-90001',
            employee=self.employee,
            party_name='New Highway Spares & Garage',
            party_gstin='29ABCPE1234F1Z7',  # valid statutory GSTIN
            total_amount=5400.00,
        )

        proposal, created = create_or_get_vendor_proposal(
            party_name=submission.party_name,
            party_gstin=submission.party_gstin,
            source_submission=submission,
            suggested_ledger='Purchase Spares & Consumables',
            created_by_user=self.employee,
        )

        assert created is True
        assert proposal.status == VendorProposal.STATUS_PENDING
        assert proposal.proposed_name == 'New Highway Spares & Garage'
        assert proposal.proposed_gstin == '29ABCPE1234F1Z7'
        assert proposal.checksum_status == 'STRUCTURE_VERIFIED'
        assert proposal.checksum_display == 'GSTIN Structure Verified'
        assert proposal.pan == 'ABCPE1234F'
        assert proposal.state_code == '29'

        # Verify initial audit log was recorded
        audit = VendorAuditLog.objects.filter(proposal=proposal).first()
        assert audit is not None
        assert audit.action == VendorAuditLog.ACTION_PROPOSAL_CREATED
        assert audit.vendor_name == 'New Highway Spares & Garage'
        assert audit.source_submission == submission

    def test_create_proposal_idempotency(self):
        """Calling create_or_get_vendor_proposal again returns existing PENDING proposal without duplicates."""
        proposal1, created1 = create_or_get_vendor_proposal(
            party_name='Kaveri Diesel Depot',
            party_gstin='29TESTA1234F1ZY',
        )
        assert created1 is True

        proposal2, created2 = create_or_get_vendor_proposal(
            party_name='Kaveri Diesel Depot',
            party_gstin='29TESTA1234F1ZY',
        )
        assert created2 is False
        assert proposal1.id == proposal2.id

    def test_registered_vendor_never_generates_proposal(self):
        """Existing VendorMaster records never trigger a proposal."""
        VendorMaster.objects.create(
            name='EXISTING BALAJI SUPPLIER',
            gstin='29AAIFG1234K1ZB',
            pan='AAIFG1234K',
            state='Karnataka',
            state_code='29',
            default_expense_ledger='Tyre & Tube Purchase',
        )

        proposal, created = create_or_get_vendor_proposal(
            party_name='EXISTING BALAJI SUPPLIER',
            party_gstin='29AAIFG1234K1ZB',
        )
        assert proposal is None
        assert created is False

    def test_authorized_approval_inserts_into_vendormaster(self):
        """Explicit approval by Naveen assigns ledger, creates VendorMaster, and logs audit."""
        submission = InvoiceSubmission.objects.create(
            submission_id='BTI-202611-90002',
            employee=self.employee,
            party_name='Shree Durga Body Works',
            party_gstin='29XYZAB5678C1Z2',
            total_amount=12500.00,
        )

        proposal, _ = create_or_get_vendor_proposal(
            party_name='Shree Durga Body Works',
            party_gstin='29XYZAB5678C1Z2',
            source_submission=submission,
        )

        initial_vendor_count = VendorMaster.objects.count()

        # Approve proposal
        vendor = approve_vendor_proposal(
            proposal_id=proposal.id,
            approved_by_user=self.naveen,
            assigned_ledger='Labour Charges / Vehicle Service',
            final_name='SHREE DURGA BODY WORKS',
            final_address='Plot 12, Tumkur Industrial Area',
        )

        assert VendorMaster.objects.count() == initial_vendor_count + 1
        assert vendor.name == 'SHREE DURGA BODY WORKS'
        assert vendor.gstin == '29XYZAB5678C1Z2'
        assert vendor.default_expense_ledger == 'Labour Charges / Vehicle Service'
        assert vendor.address == 'Plot 12, Tumkur Industrial Area'
        assert vendor.is_verified is True

        # Check proposal status
        proposal.refresh_from_db()
        assert proposal.status == VendorProposal.STATUS_APPROVED
        assert proposal.reviewed_by == self.naveen
        assert proposal.approved_vendor == vendor
        assert proposal.assigned_expense_ledger == 'Labour Charges / Vehicle Service'

        # Check audit log
        audit = VendorAuditLog.objects.filter(
            proposal=proposal,
            action=VendorAuditLog.ACTION_PROPOSAL_APPROVED
        ).first()
        assert audit is not None
        assert audit.performer == self.naveen
        assert audit.vendor == vendor
        assert audit.assigned_ledger == 'Labour Charges / Vehicle Service'
        assert audit.source_submission == submission

        # Check submission was updated with approved ledger
        submission.refresh_from_db()
        assert submission.party_name == 'SHREE DURGA BODY WORKS'
        assert submission.expense_ledger == 'Labour Charges / Vehicle Service'
        assert submission.is_ambiguous_vendor is False

    def test_prevents_simultaneous_duplicate_approvals(self):
        """Attempting to approve an already approved proposal raises ValidationError."""
        proposal, _ = create_or_get_vendor_proposal(
            party_name='Rapid Battery Store',
            party_gstin='29TESTB1234F1ZX',
        )

        approve_vendor_proposal(
            proposal_id=proposal.id,
            approved_by_user=self.naveen,
            assigned_ledger='Purchase Spares & Consumables',
        )

        # Second approval attempt must fail cleanly
        with pytest.raises(ValidationError) as exc:
            approve_vendor_proposal(
                proposal_id=proposal.id,
                approved_by_user=self.naveen,
                assigned_ledger='Purchase Spares & Consumables',
            )
        assert 'already' in str(exc.value).lower()

    def test_rejection_workflow(self):
        """Rejecting a proposal records rejection reason and audit log without modifying VendorMaster."""
        proposal, _ = create_or_get_vendor_proposal(
            party_name='Casual Tea Stall & Snacks',
            party_gstin='',
        )

        initial_vendor_count = VendorMaster.objects.count()

        rejected = reject_vendor_proposal(
            proposal_id=proposal.id,
            rejected_by_user=self.naveen,
            reason='Minor unbillable cash refreshment, not a vendor.',
        )

        assert rejected.status == VendorProposal.STATUS_REJECTED
        assert rejected.rejection_reason == 'Minor unbillable cash refreshment, not a vendor.'
        assert rejected.reviewed_by == self.naveen
        assert VendorMaster.objects.count() == initial_vendor_count

        audit = VendorAuditLog.objects.filter(
            proposal=proposal,
            action=VendorAuditLog.ACTION_PROPOSAL_REJECTED
        ).first()
        assert audit is not None
        assert audit.performer == self.naveen
