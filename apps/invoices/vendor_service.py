"""
Controlled Vendor Proposal & Authorized Onboarding Service
Balaji InvoiceHub
"""
from typing import Optional, Dict, Any, Tuple
from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError
from .models import VendorMaster, VendorProposal, VendorAuditLog, InvoiceSubmission
from .vendor_validator import (
    clean_gstin,
    clean_pan,
    extract_pan_from_gstin,
    verify_gstin_detailed,
    normalize_vendor_name,
)
from .vendor_directory import STATE_CODE_MAP, TALLY_EXPENSE_LEDGERS


def create_or_get_vendor_proposal(
    party_name: str,
    party_gstin: Optional[str] = None,
    source_submission: Optional[InvoiceSubmission] = None,
    suggested_ledger: Optional[str] = None,
    created_by_user = None,
) -> Tuple[VendorProposal, bool]:
    """
    Creates or retrieves an active pending proposal for an unregistered vendor candidate.
    Never inserts directly into VendorMaster.
    """
    cleaned_name = (party_name or '').strip()
    cleaned_gstin_str = clean_gstin(party_gstin)
    derived_pan = extract_pan_from_gstin(cleaned_gstin_str) if cleaned_gstin_str else ''
    state_code = cleaned_gstin_str[:2] if (cleaned_gstin_str and len(cleaned_gstin_str) >= 2 and cleaned_gstin_str[:2].isdigit()) else ''
    state_name = STATE_CODE_MAP.get(state_code, '')

    # Check if already registered in VendorMaster
    if cleaned_gstin_str:
        existing_vendor = VendorMaster.objects.filter(gstin=cleaned_gstin_str).first()
        if existing_vendor:
            # Vendor already exists in master directory; do not create proposal
            return None, False

    gstin_details = verify_gstin_detailed(cleaned_gstin_str) if cleaned_gstin_str else {}
    checksum_status = gstin_details.get('status_code', 'EMPTY' if not cleaned_gstin_str else 'INVALID_FORMAT')
    checksum_display = gstin_details.get('display_status', '')

    default_ledger = suggested_ledger or 'Purchase Spares & Consumables'
    if default_ledger not in TALLY_EXPENSE_LEDGERS:
        default_ledger = 'Purchase Spares & Consumables'

    with transaction.atomic():
        # Check existing PENDING proposal by GSTIN or Name
        proposal = None
        if cleaned_gstin_str:
            proposal = VendorProposal.objects.filter(
                proposed_gstin=cleaned_gstin_str,
                status=VendorProposal.STATUS_PENDING
            ).first()

        if not proposal and cleaned_name:
            norm_name = normalize_vendor_name(cleaned_name)
            for p in VendorProposal.objects.filter(status=VendorProposal.STATUS_PENDING):
                if normalize_vendor_name(p.proposed_name) == norm_name:
                    proposal = p
                    break

        if proposal:
            # If linking to a newer submission and proposal doesn't have one
            if source_submission and not proposal.source_submission:
                proposal.source_submission = source_submission
                proposal.save(update_fields=['source_submission', 'updated_at'])
            return proposal, False

        # Create new proposal
        proposal = VendorProposal.objects.create(
            proposed_name=cleaned_name,
            proposed_gstin=cleaned_gstin_str,
            pan=derived_pan,
            state=state_name,
            state_code=state_code,
            suggested_expense_ledger=default_ledger,
            source_submission=source_submission,
            status=VendorProposal.STATUS_PENDING,
            checksum_status=checksum_status,
            checksum_display=checksum_display,
            notes=f"Detected from submission {source_submission.submission_id}" if source_submission else "Manual proposal created",
        )

        # Audit log creation
        VendorAuditLog.objects.create(
            proposal=proposal,
            action=VendorAuditLog.ACTION_PROPOSAL_CREATED,
            performer=created_by_user,
            source_submission=source_submission,
            vendor_name=cleaned_name,
            gstin=cleaned_gstin_str,
            assigned_ledger=default_ledger,
            details={
                'checksum_status': checksum_status,
                'checksum_display': checksum_display,
                'source_submission_id': source_submission.submission_id if source_submission else None,
            }
        )

        return proposal, True


def approve_vendor_proposal(
    proposal_id: int,
    approved_by_user,
    assigned_ledger: str,
    final_name: Optional[str] = None,
    final_address: Optional[str] = None,
    aliases: Optional[list] = None,
) -> VendorMaster:
    """
    Controlled authorization step to approve a proposed vendor and register into VendorMaster.
    - Prevents simultaneous duplicate approvals via select_for_update().
    - Prevents duplicate GSTIN entries in VendorMaster.
    - Creates VendorAuditLog recording approver, timestamp, source submission, and assigned ledger.
    - Updates source InvoiceSubmission if applicable.
    """
    if not assigned_ledger or assigned_ledger not in TALLY_EXPENSE_LEDGERS:
        raise ValidationError(f"Invalid Tally expense ledger '{assigned_ledger}'.")

    with transaction.atomic():
        proposal = VendorProposal.objects.select_for_update().filter(id=proposal_id).first()
        if not proposal:
            raise ValidationError(f"Vendor proposal with ID {proposal_id} does not exist.")

        if proposal.status != VendorProposal.STATUS_PENDING:
            raise ValidationError(
                f"Cannot approve proposal {proposal_id}; current status is already '{proposal.status}'."
            )

        name = (final_name or proposal.proposed_name).strip()
        gstin = proposal.proposed_gstin.strip().upper() if proposal.proposed_gstin else ''
        pan = proposal.pan or (extract_pan_from_gstin(gstin) if gstin else '')
        state_code = proposal.state_code or (gstin[:2] if (gstin and len(gstin) >= 2 and gstin[:2].isdigit()) else '')
        state_name = proposal.state or STATE_CODE_MAP.get(state_code, '')
        address = (final_address or '').strip()

        # Prevent duplicate GSTIN in VendorMaster
        vendor = None
        if gstin:
            vendor = VendorMaster.objects.filter(gstin=gstin).first()

        if vendor:
            # Vendor already exists in VendorMaster; update ledger if requested and link proposal
            if assigned_ledger and vendor.default_expense_ledger != assigned_ledger:
                vendor.default_expense_ledger = assigned_ledger
                vendor.save(update_fields=['default_expense_ledger', 'updated_at'])
        else:
            # Create new VendorMaster record
            vendor = VendorMaster.objects.create(
                name=name,
                gstin=gstin,
                pan=pan,
                state=state_name,
                state_code=state_code,
                address=address,
                default_expense_ledger=assigned_ledger,
                trade_aliases=aliases or [],
                is_active=True,
                is_verified=True,
            )

        # Mark proposal approved
        proposal.status = VendorProposal.STATUS_APPROVED
        proposal.assigned_expense_ledger = assigned_ledger
        proposal.reviewed_by = approved_by_user
        proposal.reviewed_at = timezone.now()
        proposal.approved_vendor = vendor
        proposal.save(update_fields=['status', 'assigned_expense_ledger', 'reviewed_by', 'reviewed_at', 'approved_vendor', 'updated_at'])

        # Create immutable audit log
        VendorAuditLog.objects.create(
            vendor=vendor,
            proposal=proposal,
            action=VendorAuditLog.ACTION_PROPOSAL_APPROVED,
            performer=approved_by_user,
            source_submission=proposal.source_submission,
            vendor_name=vendor.name,
            gstin=vendor.gstin,
            assigned_ledger=assigned_ledger,
            details={
                'proposal_id': proposal.id,
                'source_submission_id': proposal.source_submission.submission_id if proposal.source_submission else None,
                'state': vendor.state,
            }
        )

        # If proposal is tied to an InvoiceSubmission, update its vendor and ledger
        if proposal.source_submission:
            sub = proposal.source_submission
            sub.party_name = vendor.name
            if vendor.gstin:
                sub.party_gstin = vendor.gstin
            sub.expense_ledger = assigned_ledger
            sub.is_ambiguous_vendor = False
            sub.save(update_fields=['party_name', 'party_gstin', 'expense_ledger', 'is_ambiguous_vendor', 'updated_at'])

        return vendor


def reject_vendor_proposal(
    proposal_id: int,
    rejected_by_user,
    reason: str = '',
) -> VendorProposal:
    """Rejects a vendor proposal and records audit log."""
    with transaction.atomic():
        proposal = VendorProposal.objects.select_for_update().filter(id=proposal_id).first()
        if not proposal:
            raise ValidationError(f"Vendor proposal with ID {proposal_id} does not exist.")

        if proposal.status != VendorProposal.STATUS_PENDING:
            raise ValidationError(
                f"Cannot reject proposal {proposal_id}; current status is already '{proposal.status}'."
            )

        proposal.status = VendorProposal.STATUS_REJECTED
        proposal.rejection_reason = (reason or 'Rejected by accounting operator').strip()
        proposal.reviewed_by = rejected_by_user
        proposal.reviewed_at = timezone.now()
        proposal.save(update_fields=['status', 'rejection_reason', 'reviewed_by', 'reviewed_at', 'updated_at'])

        VendorAuditLog.objects.create(
            proposal=proposal,
            action=VendorAuditLog.ACTION_PROPOSAL_REJECTED,
            performer=rejected_by_user,
            source_submission=proposal.source_submission,
            vendor_name=proposal.proposed_name,
            gstin=proposal.proposed_gstin,
            assigned_ledger=proposal.suggested_expense_ledger,
            details={
                'proposal_id': proposal.id,
                'rejection_reason': proposal.rejection_reason,
                'source_submission_id': proposal.source_submission.submission_id if proposal.source_submission else None,
            }
        )

        return proposal
