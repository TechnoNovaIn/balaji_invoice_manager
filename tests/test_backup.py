import os
import hashlib
from pathlib import Path
from django.test import TransactionTestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model
from django.conf import settings
from django.utils import timezone
from django.core.management import call_command

from apps.accounts.models import UserRole, Department
from apps.backup.models import BackupHistory, BackupStatus, TriggerType, RestoreHistory, RestoreStatus
from apps.backup.services import BackupService, RestoreService

User = get_user_model()


class BackupEngineTest(TransactionTestCase):
    def setUp(self):
        self.client = Client()
        self.password = "SecureAdmin@123"
        self.admin = User.objects.create_user(
            username="backup_admin",
            password=self.password,
            role=UserRole.ADMIN
        )
        self.emp = User.objects.create_user(
            username="emp_user_2",
            password=self.password,
            role=UserRole.EMPLOYEE
        )
        self.naveen = User.objects.create_user(
            username="naveen_user_2",
            password=self.password,
            role=UserRole.NAVEEN
        )
        self.dept = Department.objects.create(name="Logistics", code="LOG")

    def test_full_system_backup_generation_and_integrity(self):
        """
        Tests automated generation of PostgreSQL 18 dump + document ZIP with SHA-256 verification.
        """
        # Ensure sample media exists
        test_media_dir = Path(settings.MEDIA_ROOT) / 'invoices' / 'originals'
        test_media_dir.mkdir(parents=True, exist_ok=True)
        sample_file = test_media_dir / "sample_invoice.txt"
        with open(sample_file, "w") as f:
            f.write("BALAJI TRANSPORTS SAMPLE INVOICE TEST 2026")

        record = BackupService.perform_full_backup(
            created_by=self.admin,
            trigger_type=TriggerType.TEST_RUN
        )

        self.assertEqual(record.status, BackupStatus.SUCCESS)
        self.assertTrue(record.integrity_verified)
        self.assertGreater(record.db_file_size, 0)
        self.assertGreater(record.media_file_size, 0)
        self.assertEqual(len(record.db_sha256), 64)
        self.assertEqual(len(record.media_sha256), 64)

        # Verify files exist on disk outside application folder
        self.assertTrue(os.path.exists(record.db_file_path))
        self.assertTrue(os.path.exists(record.media_file_path))

        # Verify path is inside BACKUP_STORAGE_DIR
        self.assertTrue(
            Path(record.db_file_path).resolve().is_relative_to(settings.BACKUP_STORAGE_DIR.resolve())
        )

        # Cryptographic re-verification
        recalculated_db_hash = BackupService.calculate_sha256(Path(record.db_file_path))
        self.assertEqual(recalculated_db_hash, record.db_sha256)

    def test_sandbox_disaster_recovery_drill(self):
        """
        Tests full disaster recovery drill into isolated 'balaji_test_restore' database
        and verifies production database is protected.
        """
        # Create a clean backup
        backup_record = BackupService.perform_full_backup(
            created_by=self.admin,
            trigger_type=TriggerType.TEST_RUN
        )

        # Run sandbox restoration drill
        restore_record = RestoreService.run_sandbox_restore_test(
            backup_record=backup_record,
            executed_by=self.admin
        )

        self.assertEqual(restore_record.status, RestoreStatus.SUCCESS)
        self.assertEqual(restore_record.target_database, "balaji_test_restore")
        self.assertTrue(restore_record.is_sandbox_test)

        # Verify table records were restored and counted
        counts = restore_record.records_verified_count
        self.assertIn("balaji_users", counts)
        self.assertIn("balaji_departments", counts)
        self.assertGreaterEqual(counts["balaji_users"], 1)
        self.assertGreaterEqual(counts["balaji_departments"], 1)

        # Verify sandbox extracted documents
        self.assertTrue(os.path.exists(restore_record.target_media_dir))
        self.assertGreater(restore_record.files_verified_count, 0)

    def test_backup_dashboard_rbac_security(self):
        """
        Verifies only Admin can access backup center; other roles are strictly forbidden (403).
        """
        url = reverse('backup:dashboard')

        # Admin: 200 OK
        self.client.force_login(self.admin)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

        # Employee: 403 Forbidden
        self.client.force_login(self.emp)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 403)

        # Naveen: 403 Forbidden
        self.client.force_login(self.naveen)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 403)

    def test_management_command_execution(self):
        """
        Tests the automated scheduled backup command line interface.
        """
        call_command('run_system_backup', trigger='SCHEDULED')
        latest_backup = BackupHistory.objects.filter(trigger_type=TriggerType.SCHEDULED).first()
        self.assertIsNotNone(latest_backup)
        self.assertEqual(latest_backup.status, BackupStatus.SUCCESS)

    def test_permanent_retention_policy_preserves_backups(self):
        """
        Verifies that under permanent retention policy (BACKUP_RETENTION_DAYS >= 3650),
        historical backups are strictly preserved and never pruned.
        """
        from datetime import timedelta
        record = BackupService.perform_full_backup(
            created_by=self.admin,
            trigger_type=TriggerType.TEST_RUN
        )
        self.assertTrue(os.path.exists(record.db_file_path))

        # Pretend backup was created 60 days ago
        BackupHistory.objects.filter(pk=record.pk).update(
            created_at=timezone.now() - timedelta(days=60)
        )

        # Trigger pruning
        BackupService.prune_expired_backups()

        # Under permanent retention, files still exist on disk!
        self.assertTrue(os.path.exists(record.db_file_path))
        self.assertTrue(os.path.exists(record.media_file_path))
