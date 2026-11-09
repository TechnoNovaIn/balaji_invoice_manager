from django.contrib import admin
from .models import InvoiceSubmission, InvoiceDocument, InvoiceStatusHistory, InvoiceSequence


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
    list_display = ('submission_id', 'employee', 'status', 'is_duplicate_suspected', 'total_active_pages', 'created_at')
    list_filter = ('status', 'is_duplicate_suspected', 'created_at')
    search_fields = ('submission_id', 'employee__username', 'employee__first_name', 'employee__last_name', 'remarks')
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

