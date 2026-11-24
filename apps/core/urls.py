from django.urls import path
from apps.core import views

app_name = 'core'

urlpatterns = [
    path('', views.dashboard_router, name='dashboard_router'),
    path('dashboard/employee/', views.employee_dashboard, name='employee_dashboard'),
    path('dashboard/naveen/', views.naveen_dashboard, name='naveen_dashboard'),
    path('dashboard/admin/', views.admin_dashboard, name='admin_dashboard'),
    path('dashboard/admin/integrations/ai-engine/', views.ai_engine_config, name='ai_engine_config'),
    path('dashboard/admin/gemini-api-key/update/', views.update_gemini_api_key_view, name='update_gemini_api_key'),
    path('dashboard/admin/gemini-api-key/test/', views.test_gemini_api_key_view, name='test_gemini_api_key'),
    path('dashboard/management/', views.management_dashboard, name='management_dashboard'),
    path('health/', views.health_check, name='health_check'),
    path('offline/', views.offline_view, name='offline'),
    path('sw.js', views.service_worker_view, name='service_worker'),
]

