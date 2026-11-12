from django.urls import path
from apps.accounts import views

app_name = 'accounts'

urlpatterns = [
    path('login/', views.UserLoginView.as_view(), name='login'),
    path('logout/', views.UserLogoutView.as_view(), name='logout'),
    path('profile/', views.profile_view, name='profile'),
    path('change-password/', views.change_password_view, name='change_password'),

    # Admin User Management & Audit Routes (RBAC strictly enforced)
    path('manage/users/', views.UserManagementListView.as_view(), name='user_list'),
    path('manage/users/new/', views.UserCreateView.as_view(), name='user_create'),
    path('manage/users/<int:user_id>/edit/', views.edit_user_view, name='user_edit'),
    path('manage/users/<int:user_id>/toggle/', views.toggle_user_active, name='user_toggle_active'),
    path('manage/users/<int:user_id>/suspend/', views.suspend_user_view, name='user_suspend'),
    path('manage/users/<int:user_id>/reset-password/', views.reset_user_password_view, name='user_reset_password'),
    path('manage/users/<int:user_id>/history/', views.user_submission_history_view, name='user_history'),
    path('manage/audit-logs/', views.audit_logs_view, name='audit_logs'),
]
