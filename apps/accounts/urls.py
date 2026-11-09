from django.urls import path
from apps.accounts import views

app_name = 'accounts'

urlpatterns = [
    path('login/', views.UserLoginView.as_view(), name='login'),
    path('logout/', views.UserLogoutView.as_view(), name='logout'),
    path('profile/', views.profile_view, name='profile'),
    path('change-password/', views.change_password_view, name='change_password'),

    # Admin User Management Routes (RBAC enforced)
    path('manage/users/', views.UserManagementListView.as_view(), name='user_list'),
    path('manage/users/new/', views.UserCreateView.as_view(), name='user_create'),
    path('manage/users/<int:user_id>/toggle/', views.toggle_user_active, name='user_toggle_active'),
]

