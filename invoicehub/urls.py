import os
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.http import HttpResponse
from apps.accounts.views import UserLoginView

# Route Django Admin login through the hardened, rate-limited UserLoginView
admin.site.login = UserLoginView.as_view()

admin_path = os.environ.get('DJANGO_ADMIN_PATH', 'admin/').strip().lstrip('/')
if not admin_path.endswith('/'):
    admin_path += '/'

urlpatterns = [
    # Handlers to silence external browser dev-tools/extensions polling WebSocket on dev port
    path('ws', lambda r: HttpResponse(status=204)),
    path('ws/', lambda r: HttpResponse(status=204)),
    path(admin_path, admin.site.urls),
    path('accounts/', include('apps.accounts.urls', namespace='accounts')),
    path('backup/', include('apps.backup.urls', namespace='backup')),
    path('invoices/', include('apps.invoices.urls', namespace='invoices')),
    path('', include('apps.core.urls', namespace='core')),
]

# Security Hardening: NEVER serve uploaded /media/ invoices through static()
# Invoices must strictly be accessed through authenticated, RBAC-protected endpoints (serve_document).
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

handler403 = 'apps.core.views.custom_permission_denied_view'
handler404 = 'apps.core.views.custom_not_found_view'
