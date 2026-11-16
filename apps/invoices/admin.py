from django.contrib import admin
from .models import (
    InvoiceSubmission,
    InvoiceDocument,
    InvoiceStatusHistory,
    InvoiceSequence,
    VendorMaster,
    VendorProposal,
    VendorAuditLog,
    OcrExtractionCache,
    TallyLedgerMapping,
    ExportAuditLog,
)


class InvoiceDocumentInline(admin.TabularInline):
    model = InvoiceDocument
    extra = 0
    readonly_fields = ('original_filename', 'page_number', 'version', 'is_active_version', 'file_sha256', 'file_size_bytes', 'content_type', 'created_at')
    can_delete = False


class InvoiceStatusHistoryInline(admin.TabularInline):
    model = InvoiceStatusHistory
    extra = 0
    readonly_fields = ('old_status', 'new_status', 'changed_by', 'notes', 'created_at')
    can_delete = False


@admin.register(InvoiceSubmission)
class InvoiceSubmissionAdmin(admin.ModelAdmin):
    list_display = ('submission_id', 'employee', 'status', 'e_invoice_status', 'is_e_invoice', 'irn_number', 'e_invoice_manually_verified', 'is_duplicate_suspected', 'total_active_pages', 'created_at')
    list_filter = ('status', 'e_invoice_status', 'is_e_invoice', 'e_invoice_manually_verified', 'is_duplicate_suspected', 'created_at')
    search_fields = ('submission_id', 'irn_number', 'party_name', 'party_gstin', 'employee__username', 'employee__first_name', 'employee__last_name', 'remarks')
    readonly_fields = ('submission_id', 'created_at', 'updated_at')
    inlines = [InvoiceDocumentInline, InvoiceStatusHistoryInline]


@admin.register(InvoiceDocument)
class InvoiceDocumentAdmin(admin.ModelAdmin):
    list_display = ('id', 'submission', 'page_number', 'version', 'is_active_version', 'original_filename', 'file_size_display', 'content_type')
    list_filter = ('is_active_version', 'version', 'content_type')
    search_fields = ('submission__submission_id', 'original_filename', 'file_sha256')
    readonly_fields = ('file_sha256', 'file_size_bytes', 'created_at')


@admin.register(InvoiceSequence)
class InvoiceSequenceAdmin(admin.ModelAdmin):
    list_display = ('year_month', 'last_number', 'updated_at')


@admin.register(VendorMaster)
class VendorMasterAdmin(admin.ModelAdmin):
    list_display = ('name', 'gstin', 'pan', 'state', 'default_expense_ledger', 'is_verified', 'is_active')
    list_filter = ('state', 'is_verified', 'is_active', 'default_expense_ledger')
    search_fields = ('name', 'gstin', 'pan', 'normalized_name')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(VendorProposal)
class VendorProposalAdmin(admin.ModelAdmin):
    list_display = ('id', 'proposed_name', 'proposed_gstin', 'checksum_display', 'suggested_expense_ledger', 'status', 'created_at')
    list_filter = ('status', 'checksum_status')
    search_fields = ('proposed_name', 'proposed_gstin', 'pan')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(VendorAuditLog)
class VendorAuditLogAdmin(admin.ModelAdmin):
    list_display = ('id', 'action', 'vendor_name', 'gstin', 'assigned_ledger', 'performer', 'created_at')
    list_filter = ('action',)
    search_fields = ('vendor_name', 'gstin')
    readonly_fields = ('created_at',)


@admin.register(OcrExtractionCache)
class OcrExtractionCacheAdmin(admin.ModelAdmin):
    list_display = ('id', 'file_sha256', 'model_name', 'prompt_version', 'status', 'retry_count', 'lease_expires_at', 'created_at')
    list_filter = ('status', 'model_name', 'prompt_version')
    search_fields = ('file_sha256', 'cache_key')
    readonly_fields = ('created_at', 'updated_at')


