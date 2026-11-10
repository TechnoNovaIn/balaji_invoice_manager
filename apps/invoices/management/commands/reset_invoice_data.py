import os
import shutil
from pathlib import Path
from django.core.management.base import BaseCommand
from django.conf import settings
from apps.invoices.models import (
    InvoiceSubmission,
    InvoiceDocument,
    UploadedDocument,
    InvoiceStatusHistory,
    InvoiceSequence,
    ExportAuditLog,
    PurchaseAuditTrail,
)


class Command(BaseCommand):
    help = "Completely clears all invoice submissions, uploaded documents, audit logs, and resets invoice IDs to 0."

    def add_arguments(self, parser):
        parser.add_argument(
            '--yes',
            action='store_true',
            help='Confirm wiping all invoice data without interactive prompt.'
        )

    def handle(self, *args, **options):
        self.stdout.write(self.style.WARNING("=== Resetting All Invoice Data & Tracking IDs ==="))

        # Count existing records
        sub_count = InvoiceSubmission.objects.count()
        doc_count = InvoiceDocument.objects.count()
        src_count = UploadedDocument.objects.count()
        hist_count = InvoiceStatusHistory.objects.count()
        export_count = ExportAuditLog.objects.count()

        # 1. Clear relational data
        InvoiceStatusHistory.objects.all().delete()
        PurchaseAuditTrail.objects.all().delete()
        ExportAuditLog.objects.all().delete()
        InvoiceDocument.objects.all().delete()
        InvoiceSubmission.objects.all().delete()
        UploadedDocument.objects.all().delete()

        # 2. Reset Sequence Counter to 0
        InvoiceSequence.objects.all().update(last_number=0)

        # 3. Clean media folder (originals & processed)
        media_root = Path(settings.MEDIA_ROOT)
        invoices_media = media_root / 'invoices'
        files_deleted = 0
        if invoices_media.exists():
            for root, dirs, files in os.walk(str(invoices_media)):
                for f in files:
                    try:
                        os.remove(os.path.join(root, f))
                        files_deleted += 1
                    except Exception:
                        pass
        
        # Ensure standard directory structure exists
        os.makedirs(str(media_root / 'invoices' / 'originals'), exist_ok=True)
        os.makedirs(str(media_root / 'invoices' / 'processed'), exist_ok=True)

        self.stdout.write(self.style.SUCCESS(f"✓ Cleared {sub_count} InvoiceSubmissions"))
        self.stdout.write(self.style.SUCCESS(f"✓ Cleared {doc_count} InvoiceDocuments"))
        self.stdout.write(self.style.SUCCESS(f"✓ Cleared {src_count} UploadedDocuments"))
        self.stdout.write(self.style.SUCCESS(f"✓ Cleared {hist_count} Status Histories & {export_count} Export Logs"))
        self.stdout.write(self.style.SUCCESS(f"✓ Removed {files_deleted} files from media/invoices/"))
        self.stdout.write(self.style.SUCCESS("✓ Reset monthly sequence counter to 0 (Next invoice will start at BTI-YYYYMM-00001)"))
        self.stdout.write(self.style.SUCCESS("All invoice data successfully wiped! Ready for fresh test uploads."))

