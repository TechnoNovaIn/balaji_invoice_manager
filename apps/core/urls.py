from django.urls import path
from apps.core import views

app_name = 'core'

urlpatterns = [
    path('', views.dashboard_router, name='dashboard_router'),
    path('dashboard/employee/', views.employee_dashboard, name='employee_dashboard'),
    path('dashboard/naveen/', views.naveen_dashboard, name='naveen_dashboard'),
    path('dashboard/admin/', views.admin_dashboard, name='admin_dashboard'),
    path('dashboard/management/', views.management_dashboard, name='management_dashboard'),
    path('health/', views.health_check, name='health_check'),
]

