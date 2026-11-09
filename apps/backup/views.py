import os
from pathlib import Path
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.views.generic import TemplateView
from django.views import View
from django.http import FileResponse, Http404
from django.utils.decorators import method_decorator

from apps.accounts.permissions import admin_required
from apps.accounts.models import AuditLog
from apps.accounts.views import get_client_ip
from apps.backup.models import BackupHistory, BackupStatus, TriggerType, RestoreHistory
from apps.backup.services import BackupService, RestoreService


@method_decorator(admin_required, name='dispatch')
class BackupDashboardView(TemplateView):
    """
    Admin Backup & Disaster Recovery Center displaying real backup histories and restore drills.
    """
    template_name = 'backup/dashboard.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        backups = BackupHistory.objects.select_related('created_by').order_by('-created_at')[:25]
        restores = RestoreHistory.objects.select_related('backup', 'executed_by').order_by('-created_at')[:10]

        successful_backups = BackupHistory.objects.filter(status=BackupStatus.SUCCESS)
        total_storage_bytes = sum(b.total_size_bytes for b in successful_backups)

        context['backups'] = backups
        context['restores'] = restores
        context['total_backups'] = BackupHistory.objects.count()
        context['successful_count'] = successful_backups.count()
        context['last_backup'] = successful_backups.first()
        context['latest_restore'] = restores.first() if restores.exists() else None
        context['total_storage_kb'] = round(total_storage_bytes / 1024, 1)
        return context


@method_decorator(admin_required, name='dispatch')
class TriggerBackupView(View):
    """
    Triggers an on-demand full system backup (PostgreSQL dump + document files).
    """
    def post(self, request):
        try:
            record = BackupService.perform_full_backup(
                created_by=request.user,
                trigger_type=TriggerType.MANUAL
            )
            AuditLog.objects.create(
                user=request.user,
                action="ADMIN_TRIGGER_BACKUP",
                ip_address=get_client_ip(request),
                details={"backup_id": record.backup_id, "status": record.status, "duration": record.duration_seconds}
            )
            messages.success(
                request,
                f"Full system backup '{record.backup_id}' completed successfully in {record.duration_seconds}s. "
                f"Database and documents SHA-256 verified."
            )
        except Exception as e:
            messages.error(request, f"Manual backup execution failed: {e}")
        return redirect('backup:dashboard')


@method_decorator(admin_required, name='dispatch')
class TriggerRestoreTestView(View):
    """
    Triggers an isolated sandbox disaster recovery test into 'balaji_test_restore'.
    """
    def post(self, request, backup_id):
        backup_record = get_object_or_404(BackupHistory, backup_id=backup_id)
        try:
            restore_record = RestoreService.run_sandbox_restore_test(
                backup_record=backup_record,
                executed_by=request.user
            )
            AuditLog.objects.create(
                user=request.user,
                action="ADMIN_SANDBOX_RESTORE_TEST",
                ip_address=get_client_ip(request),
                details={
                    "restore_id": restore_record.restore_id,
                    "backup_id": backup_record.backup_id,
                    "target_db": restore_record.target_database,
                    "records_verified": restore_record.records_verified_count,
                    "files_recovered": restore_record.files_verified_count
                }
            )
            messages.success(
                request,
                f"Disaster Recovery Sandbox Test PASSED! Restored into '{restore_record.target_database}' in {restore_record.duration_seconds}s. "
                f"Verified records: {restore_record.records_verified_count}. Recovered files: {restore_record.files_verified_count}."
            )
        except Exception as e:
            messages.error(request, f"Disaster recovery sandbox test failed: {e}")
        return redirect('backup:dashboard')


@method_decorator(admin_required, name='dispatch')
class DownloadBackupFileView(View):
    """
    Secure admin-only download of verified backup archive files with path traversal prevention.
    """
    def get(self, request, backup_id, file_type):
        backup_record = get_object_or_404(BackupHistory, backup_id=backup_id)
        if file_type == 'db':
            file_path = Path(backup_record.db_file_path)
            content_type = 'application/octet-stream'
        elif file_type == 'media':
            file_path = Path(backup_record.media_file_path)
            content_type = 'application/zip'
        else:
            raise Http404("Invalid backup file type.")

        # Path traversal guard: verify path resides strictly within BACKUP_STORAGE_DIR
        try:
            file_path.resolve().relative_to(settings.BACKUP_STORAGE_DIR.resolve())
        except ValueError:
            raise Http404("Unauthorized file path.")

        if not file_path.exists():
            raise Http404("Backup archive file does not exist on disk.")

        AuditLog.objects.create(
            user=request.user,
            action="ADMIN_DOWNLOAD_BACKUP",
            ip_address=get_client_ip(request),
            details={"backup_id": backup_id, "file_type": file_type}
        )

        return FileResponse(open(file_path, 'rb'), as_attachment=True, filename=file_path.name, content_type=content_type)

