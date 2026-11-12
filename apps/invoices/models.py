import os
import uuid
from django.db import models, transaction
from django.conf import settings
from django.utils import timezone


def invoice_upload_path(instance, filename):
    """
    Generate secure and unique file path for uploaded invoice documents.
    Preserves file extension safely while preventing name collisions and directory traversal.
    Path pattern: invoices/originals/YYYY/MM/<uuid4>.<ext>
    """
    now = timezone.now()
    _, ext = os.path.splitext(filename)
    safe_ext = ext.lower().strip()
    if safe_ext not in ['.jpg', '.jpeg', '.png', '.pdf']:
        safe_ext = '.bin'
    unique_name = f"{uuid.uuid4().hex}{safe_ext}"
    return f"invoices/originals/{now.strftime('%Y/%m')}/{unique_name}"


class InvoiceSequence(models.Model):
    """
    Concurrency-safe sequence counter for tracking ID generation.
    Formats sequence IDs monthly: BTI-YYYYMM-XXXXX
    """
    year_month = models.CharField(max_length=6, primary_key=True, help_text="YYYYMM format, e.g. 202611")
    last_number = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Invoice Monthly Sequence'
        verbose_name_plural = 'Invoice Monthly Sequences'

    @classmethod
    def get_next_submission_id(cls) -> str:
        """
        Thread-safe and transaction-safe monthly tracking ID generator.
        Uses select_for_update inside an atomic transaction.
        """
        now = timezone.now()
        ym_str = now.strftime('%Y%m')

        with transaction.atomic():
            seq_record, _ = cls.objects.select_for_update().get_or_create(
                year_month=ym_str,
                defaults={'last_number': 0}
            )
            seq_record.last_number += 1
            seq_record.save(update_fields=['last_number', 'updated_at'])
            seq_num = seq_record.last_number

        return f"BTI-{ym_str}-{seq_num:05d}"


class UploadedDocument(models.Model):
    """
    Source document file uploaded by an employee (e.g. multi-invoice PDF or batch).
    Preserves original uploaded files with cryptographic SHA-256 integrity hash.
    Can contain one or multiple independently detected invoices.
    """
    STATUS_UPLOADED = 'UPLOADED'
    STATUS_QUEUED = 'QUEUED'
    STATUS_PROCESSING = 'PROCESSING'
    STATUS_DETECTION_COMPLETED = 'DETECTION_COMPLETED'
    STATUS_NEEDS_REVIEW = 'NEEDS_REVIEW'
    STATUS_COMPLETED = 'COMPLETED'
    STATUS_FAILED = 'FAILED'

    STATUS_CHOICES = [
        (STATUS_UPLOADED, 'Uploaded'),
        (STATUS_QUEUED, 'Queued'),
        (STATUS_PROCESSING, 'Processing'),
        (STATUS_DETECTION_COMPLETED, 'Invoice Detection Completed'),
        (STATUS_NEEDS_REVIEW, 'Needs Manual Review'),
        (STATUS_COMPLETED, 'Completed'),
        (STATUS_FAILED, 'Failed'),
    ]

    uploader = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='uploaded_source_documents',
        help_text="Employee or user who uploaded this document."
    )
    original_file = models.FileField(
        upload_to=invoice_upload_path,
        help_text="Stored original source document file"
    )
    file_name = models.CharField(
        max_length=255,
        help_text="Original client-side filename"
    )
    file_sha256 = models.CharField(
        max_length=64,
        db_index=True,
        help_text="SHA-256 checksum of source document"
    )
    file_size_bytes = models.PositiveIntegerField(
        help_text="File size in bytes"
    )
    content_type = models.CharField(
        max_length=50,
        help_text="MIME type of document"
    )
    total_pages = models.PositiveIntegerField(
        default=1,
        help_text="Total number of pages in this document"
    )
    pages_processed = models.PositiveIntegerField(
        default=0,
        help_text="Number of pages successfully processed by OCR/segmentation"
    )
    invoices_detected = models.PositiveIntegerField(
        default=0,
        help_text="Number of distinct invoices identified in this document"
    )
    processing_status = models.CharField(
        max_length=30,
        choices=STATUS_CHOICES,
        default=STATUS_UPLOADED,
        db_index=True
    )
    current_stage = models.CharField(
        max_length=200,
        blank=True,
        default='Document Received',
        help_text="Current processing stage description"
    )
    progress_percent = models.PositiveIntegerField(
        default=0,
        help_text="Processing progress percentage (0 - 100)"
    )
    processing_metrics = models.JSONField(
        default=dict,
        blank=True,
        help_text="Detailed performance metrics and stage execution timings"
    )
    error_message = models.TextField(
        blank=True,
        default='',
        help_text="Details of any processing error"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Uploaded Source Document'
        verbose_name_plural = 'Uploaded Source Documents'
        indexes = [
            models.Index(fields=['file_sha256']),
            models.Index(fields=['processing_status', '-created_at']),
        ]

    def __str__(self):
        return f"{self.file_name} ({self.total_pages} pgs, {self.invoices_detected} invs) - {self.get_processing_status_display()}"

    @property
    def file_size_display(self):
        kb = self.file_size_bytes / 1024.0
        if kb >= 1024:
            return f"{kb / 1024.0:.2f} MB"
        return f"{kb:.1f} KB"

    @property
    def is_pdf(self):
        return self.content_type == 'application/pdf'


class InvoiceSubmission(models.Model):
    """
    Core invoice submission entity created by employees.
    Categorization is explicitly removed per business requirements.
    Employees provide documents and optional remarks.
    """
    STATUS_SUBMITTED = 'SUBMITTED'
    STATUS_RETURNED = 'RETURNED'
    STATUS_RESUBMITTED = 'RESUBMITTED'
    STATUS_VERIFIED = 'VERIFIED'
    STATUS_REJECTED = 'REJECTED'

    STATUS_CHOICES = [
        (STATUS_SUBMITTED, 'Submitted'),
        (STATUS_RETURNED, 'Returned for Correction'),
        (STATUS_RESUBMITTED, 'Resubmitted'),
        (STATUS_VERIFIED, 'Verified'),
        (STATUS_REJECTED, 'Rejected'),
    ]

    EINV_STATUS_YES = 'YES'
    EINV_STATUS_NO = 'NO'
    EINV_STATUS_HANDWRITTEN = 'HANDWRITTEN'
    EINV_STATUS_NEEDS_VERIFICATION = 'NEEDS_VERIFICATION'

    E_INVOICE_STATUS_CHOICES = [
        (EINV_STATUS_YES, 'YES'),
        (EINV_STATUS_NO, 'NO'),
        (EINV_STATUS_HANDWRITTEN, 'HANDWRITTEN'),
        (EINV_STATUS_NEEDS_VERIFICATION, 'NEEDS VERIFICATION'),
    ]

    submission_id = models.CharField(
        max_length=30,
        unique=True,
        db_index=True,
        help_text="Tracking ID in format BTI-YYYYMM-XXXXX"
    )
    source_document = models.ForeignKey(
        UploadedDocument,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='invoices',
        help_text="Original source document containing this invoice."
    )
    start_page = models.PositiveIntegerField(
        default=1,
        help_text="Starting page in source document (1-indexed)"
    )
    end_page = models.PositiveIntegerField(
        default=1,
        help_text="Ending page in source document (inclusive)"
    )
    batch_id = models.CharField(
        max_length=50,
        blank=True,
        default='',
        db_index=True,
        help_text="Grouping ID for uploads processed in the same batch"
    )
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='invoice_submissions',
        help_text="Employee who captured/uploaded this invoice. Protected from cascading deletion."
    )
    remarks = models.TextField(
        blank=True,
        default='',
        help_text="Optional remarks from employee (trip, vehicle, route, loading, etc.)"
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_SUBMITTED,
        db_index=True
    )
    return_reason = models.TextField(
        blank=True,
        default='',
        help_text="Reason provided by Naveen if invoice was returned for correction"
    )
    is_duplicate_suspected = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Flagged if any uploaded document matches another existing document hash"
    )
    duplicate_notes = models.TextField(
        blank=True,
        default='',
        help_text="Audit details explaining which files matched existing submissions"
    )
    # Tally Accounting & GST Portal Tracking
    is_entered_in_tally = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Whether this invoice has been entered as a voucher in Tally"
    )
    tally_voucher_no = models.CharField(
        max_length=50,
        blank=True,
        default='',
        help_text="Tally Voucher No. / Ref No."
    )
    tally_entered_at = models.DateTimeField(null=True, blank=True)

    is_filed_in_gst = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Whether this invoice has been filed/reconciled in GST Portal (GSTR-2B / GSTR-3B)"
    )
    gst_filing_period = models.CharField(
        max_length=20,
        blank=True,
        default='',
        help_text="GST filing return period, e.g. Nov-2026"
    )
    gst_filed_at = models.DateTimeField(null=True, blank=True)

    # Core Invoice Details for Tally & GST Entry
    party_name = models.CharField(max_length=255, blank=True, default='')
    party_gstin = models.CharField(max_length=15, blank=True, default='')
    invoice_number = models.CharField(max_length=100, blank=True, default='')
    invoice_date = models.DateField(null=True, blank=True)
    taxable_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    cgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    sgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    igst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    expense_ledger = models.CharField(
        max_length=100,
        blank=True,
        default='Diesel Expenses',
        help_text="Tally expense ledger (e.g. Diesel Expenses, Vehicle Repairs, Toll & Parking)"
    )
    hsn_sac_code = models.CharField(
        max_length=20,
        blank=True,
        default='',
        help_text="HSN / SAC Code"
    )
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='verified_invoices',
        help_text="Accounts operator (Naveen) who verified this purchase invoice"
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    tally_entered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='tally_entered_invoices',
        help_text="User who entered or marked voucher in Tally"
    )
    tally_entry_remarks = models.TextField(
        blank=True,
        default='',
        help_text="Remarks entered upon marking in Tally"
    )
    ocr_confidence = models.FloatField(
        default=0.0,
        help_text="OCR extraction overall confidence score 0.0 - 1.0"
    )
    is_ambiguous_vendor = models.BooleanField(
        default=False,
        help_text="Flagged if vendor matching had multiple possibilities"
    )
    requires_manual_review = models.BooleanField(
        default=True,
        help_text="Financial safety gate: manual verification required"
    )
    e_invoice_status = models.CharField(
        max_length=24,
        choices=E_INVOICE_STATUS_CHOICES,
        default=EINV_STATUS_NEEDS_VERIFICATION,
        db_index=True,
        help_text="Combined E-Invoice? display status: YES, NO, HANDWRITTEN, or NEEDS_VERIFICATION"
    )
    e_invoice_manually_verified = models.BooleanField(
        default=False,
        help_text="True when operator manually updated E-Invoice? status; prevents silent OCR/Gemini overwrites."
    )
    is_e_invoice = models.BooleanField(
        null=True,
        blank=True,
        default=None,
        db_index=True,
        help_text="E-Invoice classification: True=YES, False=NO, None=NEEDS VERIFICATION"
    )
    irn_number = models.CharField(
        max_length=64,
        blank=True,
        default='',
        db_index=True,
        help_text="64-character Invoice Reference Number (IRN) when invoice is an e-invoice"
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Invoice Submission'
        verbose_name_plural = 'Invoice Submissions'
        indexes = [
            models.Index(fields=['employee', '-created_at']),
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['is_entered_in_tally', 'is_filed_in_gst']),
        ]

    def __str__(self):
        return f"{self.submission_id} - {self.employee.get_full_name() or self.employee.username}"

    @property
    def accounting_status(self):
        if self.is_filed_in_gst:
            return 'GST_FILED'
        if self.is_entered_in_tally:
            return 'IN_TALLY'
        return 'PENDING_TALLY'

    @property
    def accounting_status_display(self):
        if self.is_filed_in_gst:
            return 'Filed in GST'
        if self.is_entered_in_tally:
            return 'In Tally (Pending GST)'
        return 'Pending Tally'

    @property
    def active_documents(self):
        return self.documents.filter(is_active_version=True).order_by('page_number')

    @property
    def total_active_pages(self):
        return self.active_documents.count()

    @property
    def can_be_edited(self):
        """Only returned submissions can be edited / resubmitted by the employee."""
        return self.status == self.STATUS_RETURNED

    @property
    def is_irn_valid(self):
        """True only when irn_number is a valid 64-character alphanumeric IRN."""
        from .e_invoice_utils import is_valid_irn
        return is_valid_irn(self.irn_number)

    @property
    def e_invoice_status_label(self):
        if self.e_invoice_status == self.EINV_STATUS_HANDWRITTEN:
            return 'HANDWRITTEN'
        if self.e_invoice_status == self.EINV_STATUS_YES or self.is_e_invoice is True:
            return 'YES'
        if self.e_invoice_status == self.EINV_STATUS_NO or self.is_e_invoice is False:
            return 'NO'
        return 'NEEDS VERIFICATION'

    @property
    def irn_display(self):
        """
        Safe presentation value for IRN column:
        - HANDWRITTEN or confirmed NO -> 'N/A'
        - Valid IRN -> complete 64-char hex IRN
        - Marked YES without valid IRN -> 'IRN Not Found'
        - Insufficient info (NEEDS VERIFICATION) -> '' (pending check)
        """
        if self.e_invoice_status in (self.EINV_STATUS_NO, self.EINV_STATUS_HANDWRITTEN) or self.is_e_invoice is False:
            return 'N/A'
        if self.is_irn_valid:
            return self.irn_number.strip().upper()
        if self.e_invoice_status == self.EINV_STATUS_YES or self.is_e_invoice is True:
            return 'IRN Not Found'
        return ''


class InvoiceDocument(models.Model):
    """
    Immutable document file record attached to an invoice submission.
    Preserves original uploaded files with cryptographic SHA-256 integrity hash.
    Versioned to retain audit trails if a submission is returned and resubmitted.
    """
    submission = models.ForeignKey(
        InvoiceSubmission,
        on_delete=models.CASCADE,
        related_name='documents'
    )
    source_document = models.ForeignKey(
        UploadedDocument,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='extracted_documents',
        help_text="Parent source document if extracted from a multi-invoice document"
    )
    start_page = models.PositiveIntegerField(
        default=1,
        help_text="Starting page in source document"
    )
    end_page = models.PositiveIntegerField(
        default=1,
        help_text="Ending page in source document"
    )
    page_number = models.PositiveSmallIntegerField(
        default=1,
        help_text="Page / document order in the submission"
    )
    original_file = models.FileField(
        upload_to=invoice_upload_path,
        help_text="Stored original document file"
    )
    original_filename = models.CharField(
        max_length=255,
        help_text="Original client-side filename before storage sanitization"
    )
    file_sha256 = models.CharField(
        max_length=64,
        db_index=True,
        help_text="SHA-256 checksum for deduplication and tamper detection"
    )
    file_size_bytes = models.PositiveIntegerField(
        help_text="File size in bytes"
    )
    content_type = models.CharField(
        max_length=50,
        help_text="MIME type verified by magic-byte inspection (e.g. image/jpeg, application/pdf)"
    )
    version = models.PositiveSmallIntegerField(
        default=1,
        help_text="Version number. Incremented if returned invoice is resubmitted with replacement docs."
    )
    is_active_version = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Whether this document belongs to the active working version"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['submission', 'version', 'page_number']
        verbose_name = 'Invoice Document'
        verbose_name_plural = 'Invoice Documents'
        indexes = [
            models.Index(fields=['file_sha256']),
            models.Index(fields=['submission', 'is_active_version']),
        ]

    def __str__(self):
        return f"{self.submission.submission_id} - Pg {self.page_number} (v{self.version}) - {self.original_filename}"

    @property
    def file_size_display(self):
        kb = self.file_size_bytes / 1024.0
        if kb >= 1024:
            return f"{kb / 1024.0:.2f} MB"
        return f"{kb:.1f} KB"

    @property
    def is_image(self):
        return self.content_type in ['image/jpeg', 'image/png']

    @property
    def is_pdf(self):
        return self.content_type == 'application/pdf'


class InvoiceStatusHistory(models.Model):
    """
    Immutable audit trail recording all lifecycle transitions of an invoice submission.
    """
    submission = models.ForeignKey(
        InvoiceSubmission,
        on_delete=models.CASCADE,
        related_name='status_history'
    )
    old_status = models.CharField(max_length=20, blank=True, default='')
    new_status = models.CharField(max_length=20)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='invoice_status_changes'
    )
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        verbose_name = 'Invoice Status History'
        verbose_name_plural = 'Invoice Status Histories'

    def __str__(self):
        return f"{self.submission.submission_id}: {self.old_status} -> {self.new_status} at {self.created_at}"


class PurchaseAuditTrail(models.Model):
    """
    Detailed audit log of financial and metadata corrections made to an invoice.
    Tracks previous value, updated value, editor, timestamp, and explanation.
    """
    submission = models.ForeignKey(
        InvoiceSubmission,
        on_delete=models.CASCADE,
        related_name='audit_trails'
    )
    field_name = models.CharField(max_length=50, help_text="e.g. party_name, invoice_number, total_amount")
    old_value = models.TextField(blank=True, default='')
    new_value = models.TextField(blank=True, default='')
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='purchase_audits'
    )
    changed_at = models.DateTimeField(auto_now_add=True)
    reason = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        ordering = ['-changed_at']
        verbose_name = 'Purchase Audit Trail'
        verbose_name_plural = 'Purchase Audit Trails'

    def __str__(self):
        return f"{self.submission.submission_id}: {self.field_name} modified at {self.changed_at}"


class TallyLedgerMapping(models.Model):
    """
    Configurable ledger mapping between Transport expense categories and Tally company accounts.
    Allows Naveen and Admin to manage standard Purchase ledgers and Tax ledgers.
    """
    category_name = models.CharField(max_length=100, unique=True, help_text="Category, e.g. Diesel, Spares, Tyres, Toll")
    tally_expense_ledger = models.CharField(max_length=100, help_text="Exact Tally Expense Ledger name")
    cgst_ledger = models.CharField(max_length=100, default='Input CGST')
    sgst_ledger = models.CharField(max_length=100, default='Input SGST')
    igst_ledger = models.CharField(max_length=100, default='Input IGST')
    description = models.CharField(max_length=255, blank=True, default='')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['category_name']
        verbose_name = 'Tally Ledger Mapping'
        verbose_name_plural = 'Tally Ledger Mappings'

    def __str__(self):
        return f"{self.category_name} -> {self.tally_expense_ledger}"


class ExportAuditLog(models.Model):
    """
    Audit log of all accounting and tax exports (Tally Excel, Tally XML, GST Portal Excel).
    Records user, timestamp, file type, record counts, and total values.
    """
    EXPORT_TYPE_CHOICES = [
        ('TALLY_EXCEL', 'Tally Structured Excel'),
        ('TALLY_XML', 'TallyPrime XML Voucher'),
        ('GST_PORTAL_EXCEL', 'GST Portal B2B Excel'),
        ('GST_CSV', 'GST Portal ITC CSV'),
        ('PURCHASE_REGISTER_EXCEL', 'Purchase Register Excel'),
        ('EXECUTIVE_REPORT_EXCEL', 'Executive Analytics Report'),
    ]
    export_type = models.CharField(max_length=40, choices=EXPORT_TYPE_CHOICES, db_index=True)
    exported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='export_logs'
    )
    record_count = models.PositiveIntegerField(default=0)
    total_taxable = models.DecimalField(max_digits=14, decimal_places=2, default=0.00)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0.00)
    filename = models.CharField(max_length=255, blank=True, default='')
    filters_applied = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Export Audit Log'
        verbose_name_plural = 'Export Audit Logs'

    def __str__(self):
        return f"{self.get_export_type_display()} ({self.record_count} records) by {self.exported_by} at {self.created_at}"


class VendorMaster(models.Model):
    """
    Authoritative Vendor Master Database for Balaji Transports.
    Stores verified vendor profiles with GSTIN, PAN, trade aliases, and default Tally expense ledgers.
    Supports Excel/CSV batch imports and audit tracking.
    """
    name = models.CharField(max_length=255, db_index=True, help_text="Legal or primary trade name")
    normalized_name = models.CharField(max_length=255, db_index=True, help_text="Alphanumeric lowercase for deterministic fuzzy matching")
    gstin = models.CharField(max_length=15, blank=True, default='', db_index=True, help_text="15-character statutory GSTIN")
    pan = models.CharField(max_length=10, blank=True, default='', db_index=True, help_text="10-character Income Tax PAN")
    state = models.CharField(max_length=100, blank=True, default='', help_text="Indian State / UT")
    state_code = models.CharField(max_length=2, blank=True, default='', help_text="2-digit GST state code")
    address = models.TextField(blank=True, default='', help_text="Vendor operating address")
    default_expense_ledger = models.CharField(
        max_length=100,
        default='Purchase Spares & Consumables',
        help_text="Standard Tally Expense Ledger"
    )
    trade_aliases = models.JSONField(
        default=list,
        blank=True,
        help_text="List of alternate trade names / bill headers"
    )
    is_active = models.BooleanField(default=True, db_index=True)
    is_verified = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        verbose_name = 'Vendor Master'
        verbose_name_plural = 'Vendor Masters'
        indexes = [
            models.Index(fields=['gstin', 'is_active']),
            models.Index(fields=['normalized_name', 'is_active']),
            models.Index(fields=['pan', 'is_active']),
        ]

    def __str__(self):
        return f"{self.name} ({self.gstin or 'No GSTIN'})"

    def save(self, *args, **kwargs):
        import re
        if self.gstin:
            self.gstin = self.gstin.strip().upper()
            if not self.pan and len(self.gstin) >= 12 and re.match(r'^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}', self.gstin):
                self.pan = self.gstin[2:12]
            if not self.state_code and len(self.gstin) >= 2 and self.gstin[:2].isdigit():
                self.state_code = self.gstin[:2]
        if self.name:
            self.normalized_name = re.sub(r'[^a-z0-9]', '', self.name.lower())
        super().save(*args, **kwargs)



