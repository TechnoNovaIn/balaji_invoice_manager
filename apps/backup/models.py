from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _


class BackupType(models.TextChoices):
    FULL = 'FULL', _('Full System (Database + Document Files)')
    DATABASE = 'DATABASE', _('PostgreSQL Database Only')
    DOCUMENTS = 'DOCUMENTS', _('Invoice Documents Only')


class TriggerType(models.TextChoices):
    SCHEDULED = 'SCHEDULED', _('Automated Scheduled Job')
    MANUAL = 'MANUAL', _('Admin Manual Trigger')
    TEST_RUN = 'TEST_RUN', _('Disaster Recovery Verification Run')


class BackupStatus(models.TextChoices):
    PENDING = 'PENDING', _('Backup in Progress')
    SUCCESS = 'SUCCESS', _('Completed & Verified')
    FAILED = 'FAILED', _('Backup Failed')
    INTEGRITY_FAILED = 'INTEGRITY_FAILED', _('Checksum Verification Failed')


class BackupHistory(models.Model):
    """
    Historical log of all database and document backups with SHA-256 integrity proofs.
    """
    backup_id = models.CharField(max_length=64, unique=True, db_index=True)
    backup_type = models.CharField(max_length=20, choices=BackupType.choices, default=BackupType.FULL)
    trigger_type = models.CharField(max_length=20, choices=TriggerType.choices, default=TriggerType.MANUAL)
    
    # Database archive details
    db_file_path = models.CharField(max_length=512, blank=True, default='')
    db_file_size = models.BigIntegerField(default=0, help_text="Size of database dump in bytes")
    db_sha256 = models.CharField(max_length=64, blank=True, default='', help_text="SHA-256 hash of database dump")

    # Document archive details
    media_file_path = models.CharField(max_length=512, blank=True, default='')
    media_file_size = models.BigIntegerField(default=0, help_text="Size of invoice documents archive in bytes")
    media_sha256 = models.CharField(max_length=64, blank=True, default='', help_text="SHA-256 hash of documents archive")

    total_size_bytes = models.BigIntegerField(default=0)
    status = models.CharField(max_length=20, choices=BackupStatus.choices, default=BackupStatus.PENDING, db_index=True)
    integrity_verified = models.BooleanField(default=False, help_text="Whether cryptographic checksums were verified")
    verified_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.FloatField(default=0.0)
    error_log = models.TextField(blank=True, default='')

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='triggered_backups'
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'balaji_backup_history'
        ordering = ['-created_at']
        verbose_name = 'Backup History'
        verbose_name_plural = 'Backup Histories'

    def __str__(self):
        return f"{self.backup_id} [{self.get_backup_type_display()}] - {self.get_status_display()}"


class RestoreStatus(models.TextChoices):
    SUCCESS = 'SUCCESS', _('Restoration & Verification Succeeded')
    FAILED = 'FAILED', _('Restoration Failed')


class RestoreHistory(models.Model):
    """
    Log of sandbox disaster recovery executions into isolated staging databases.
    """
    restore_id = models.CharField(max_length=64, unique=True, db_index=True)
    backup = models.ForeignKey(BackupHistory, on_delete=models.CASCADE, related_name='restorations')
    target_database = models.CharField(max_length=128, default='balaji_test_restore')
    target_media_dir = models.CharField(max_length=512, blank=True, default='')
    is_sandbox_test = models.BooleanField(default=True, help_text="True if isolated sandbox restoration test")
    status = models.CharField(max_length=20, choices=RestoreStatus.choices, default=RestoreStatus.SUCCESS)
    records_verified_count = models.JSONField(default=dict, help_text="Row counts verified per table in restored DB")
    files_verified_count = models.IntegerField(default=0, help_text="Count of document files recovered and verified")
    error_log = models.TextField(blank=True, default='')
    duration_seconds = models.FloatField(default=0.0)

    executed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='executed_restores'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'balaji_restore_history'
        ordering = ['-created_at']
        verbose_name = 'Disaster Recovery Log'
        verbose_name_plural = 'Disaster Recovery Logs'

    def __str__(self):
        return f"{self.restore_id} (From {self.backup.backup_id}) - {self.get_status_display()}"

