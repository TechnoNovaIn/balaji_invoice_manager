from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/', include('apps.accounts.urls', namespace='accounts')),
    path('backup/', include('apps.backup.urls', namespace='backup')),
    path('invoices/', include('apps.invoices.urls', namespace='invoices')),
    path('', include('apps.core.urls', namespace='core')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

handler403 = 'apps.core.views.custom_permission_denied_view'
handler404 = 'apps.core.views.custom_not_found_view'
