from django.core.management.base import BaseCommand
from apps.backup.services import BackupService
from apps.backup.models import TriggerType, BackupStatus


class Command(BaseCommand):
    help = "Executes an automated full backup (PostgreSQL 18 dump + media documents) with SHA-256 integrity verification."

    def add_arguments(self, parser):
        parser.add_argument(
            '--trigger',
            type=str,
            default='SCHEDULED',
            choices=['SCHEDULED', 'MANUAL', 'TEST_RUN'],
            help='Source trigger category for audit tracking.'
        )

    def handle(self, *args, **options):
        trigger = TriggerType(options['trigger'])
        self.stdout.write(self.style.NOTICE(f"Starting Balaji InvoiceHub automated full backup [{trigger}]..."))
        
        try:
            backup_record = BackupService.perform_full_backup(trigger_type=trigger)
            if backup_record.status == BackupStatus.SUCCESS:
                self.stdout.write(self.style.SUCCESS(
                    f"Backup successfully generated: {backup_record.backup_id}\n"
                    f" - Database Dump: {backup_record.db_file_path} ({backup_record.db_file_size / 1024:.1f} KB)\n"
                    f" - Database SHA-256: {backup_record.db_sha256}\n"
                    f" - Documents ZIP: {backup_record.media_file_path} ({backup_record.media_file_size / 1024:.1f} KB)\n"
                    f" - Documents SHA-256: {backup_record.media_sha256}\n"
                    f" - Integrity: VERIFIED\n"
                    f" - Execution Duration: {backup_record.duration_seconds}s"
                ))
            else:
                self.stderr.write(self.style.ERROR(
                    f"Backup finished with non-success status: {backup_record.status}. Log: {backup_record.error_log}"
                ))
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Backup job aborted with fatal exception: {e}"))
            raise

