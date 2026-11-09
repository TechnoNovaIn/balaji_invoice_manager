from django.urls import path
from apps.backup import views

app_name = 'backup'

urlpatterns = [
    path('', views.BackupDashboardView.as_view(), name='dashboard'),
    path('trigger/', views.TriggerBackupView.as_view(), name='trigger_backup'),
    path('restore-test/<str:backup_id>/', views.TriggerRestoreTestView.as_view(), name='trigger_restore_test'),
    path('download/<str:backup_id>/<str:file_type>/', views.DownloadBackupFileView.as_view(), name='download_backup'),
]

