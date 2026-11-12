from django.core.management.base import BaseCommand
from apps.invoices.tasks import get_incomplete_documents, recover_incomplete_documents


class Command(BaseCommand):
    help = "Identifies and safely recovers UploadedDocument jobs stuck in QUEUED or PROCESSING states after a server restart."

    def add_arguments(self, parser):
        parser.add_argument(
            '--stale-minutes',
            type=int,
            default=0,
            help='Only target incomplete records untouched for at least this many minutes (default: 0 = all non-active).'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report stuck QUEUED/PROCESSING records without modifying or dispatching anything.'
        )
        parser.add_argument(
            '--requeue-only',
            action='store_true',
            help='Reset orphaned PROCESSING records back to QUEUED without immediately spawning background threads.'
        )

    def handle(self, *args, **options):
        stale_minutes = options['stale_minutes']
        if options['dry_run']:
            docs = list(get_incomplete_documents(stale_minutes=stale_minutes))
            self.stdout.write(f"Found {len(docs)} incomplete document(s):")
            for doc in docs:
                self.stdout.write(
                    f" - UploadedDocument #{doc.id}: '{doc.file_name}' "
                    f"[{doc.processing_status}, {doc.progress_percent}%, updated={doc.updated_at.isoformat()}]"
                )
            return

        summary = recover_incomplete_documents(
            stale_minutes=stale_minutes,
            requeue_only=options['requeue_only'],
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Recovery complete: found={summary['total_found']}, "
                f"recovered={summary['recovered_ids']}, "
                f"already_completed={summary['already_completed_ids']}, "
                f"failed_missing={summary['failed_missing_ids']}, "
                f"skipped_active={summary.get('skipped_active_ids', [])}"
            )
        )

