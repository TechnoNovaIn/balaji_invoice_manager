from django.core.management.base import BaseCommand
from apps.backup.services import RestoreService
from apps.backup.models import BackupHistory, BackupStatus, RestoreStatus


class Command(BaseCommand):
    help = "Executes an isolated disaster recovery drill into PostgreSQL test database 'balaji_test_restore'."

    def add_arguments(self, parser):
        parser.add_argument(
            '--backup-id',
            type=str,
            required=False,
            help='Specific backup ID to test. If omitted, uses the most recent successful backup.'
        )

    def handle(self, *args, **options):
        backup_id = options.get('backup_id')
        if backup_id:
            backup_record = BackupHistory.objects.filter(backup_id=backup_id).first()
            if not backup_record:
                self.stderr.write(self.style.ERROR(f"Backup with ID '{backup_id}' not found."))
                return
        else:
            backup_record = BackupHistory.objects.filter(status=BackupStatus.SUCCESS).order_by('-created_at').first()
            if not backup_record:
                self.stderr.write(self.style.ERROR("No successful backup records available to test restore."))
                return

        self.stdout.write(self.style.NOTICE(f"Initiating sandbox disaster recovery test from: {backup_record.backup_id}..."))
        self.stdout.write("Target Isolated Database: balaji_test_restore (Production is isolated and protected)")

        try:
            restore_record = RestoreService.run_sandbox_restore_test(backup_record=backup_record)
            if restore_record.status == RestoreStatus.SUCCESS:
                self.stdout.write(self.style.SUCCESS(
                    f"Disaster Recovery Test PASSED: {restore_record.restore_id}\n"
                    f" - Restored Database: {restore_record.target_database}\n"
                    f" - Verified Table Row Counts: {restore_record.records_verified_count}\n"
                    f" - Verified Media Document Files Recovered: {restore_record.files_verified_count}\n"
                    f" - Sandbox Output Directory: {restore_record.target_media_dir}\n"
                    f" - Recovery Duration: {restore_record.duration_seconds}s"
                ))
            else:
                self.stderr.write(self.style.ERROR(f"Restoration failed: {restore_record.error_log}"))
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Disaster recovery test failed with exception: {e}"))
            raise

