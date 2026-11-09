from django.contrib import admin
from apps.backup.models import BackupHistory, RestoreHistory


@admin.register(BackupHistory)
class BackupHistoryAdmin(admin.ModelAdmin):
    list_display = ('backup_id', 'backup_type', 'trigger_type', 'status', 'total_size_bytes', 'integrity_verified', 'created_at')
    list_filter = ('status', 'backup_type', 'trigger_type', 'integrity_verified')
    search_fields = ('backup_id', 'db_sha256', 'media_sha256')
    readonly_fields = [f.name for f in BackupHistory._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(RestoreHistory)
class RestoreHistoryAdmin(admin.ModelAdmin):
    list_display = ('restore_id', 'backup', 'target_database', 'status', 'duration_seconds', 'created_at')
    list_filter = ('status', 'target_database')
    search_fields = ('restore_id', 'backup__backup_id')
    readonly_fields = [f.name for f in RestoreHistory._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

