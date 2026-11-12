from django.contrib import messages
from django.contrib.auth import logout
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.cache import patch_vary_headers


class ForcePasswordChangeMiddleware:
    """
    Enforces account lifecycle controls (blocking deactivated/suspended users immediately)
    and ensures users with must_change_password=True set a personal password before accessing portal routes.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            if not getattr(request.user, 'is_operational_active', request.user.is_active):
                logout(request)
                messages.error(
                    request,
                    "Your account is currently suspended or deactivated. Please contact Naveen (Administrator)."
                )
                return redirect('accounts:login')

            if getattr(request.user, 'must_change_password', False):
                # Whitelisted URLs that must be accessible while changing password
                allowed_paths = [
                    reverse('accounts:change_password'),
                    reverse('accounts:logout'),
                    '/sw.js',
                    '/offline/',
                ]
                path = request.path
                if not any(path.startswith(p) for p in allowed_paths) and not path.startswith('/static/'):
                    return redirect('accounts:change_password')

        return self.get_response(request)


class SensitiveResponseNoCacheMiddleware:
    """
    Prevents browsers, Service Workers, and intermediate proxies from caching
    authenticated HTML pages, login pages, or sensitive financial/invoice routes.
    """
    _SENSITIVE_PREFIXES = (
        '/accounts/',
        '/invoices/',
        '/dashboard/',
        '/backup/',
        '/admin/',
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        path = request.path or ''
        if path.startswith('/static/') or path in ('/sw.js', '/offline/'):
            return response

        is_authenticated = bool(getattr(getattr(request, 'user', None), 'is_authenticated', False))
        if is_authenticated or any(path.startswith(p) for p in self._SENSITIVE_PREFIXES):
            if not response.has_header('Cache-Control'):
                response['Cache-Control'] = 'private, no-store, no-cache, must-revalidate, max-age=0'
            if not response.has_header('Pragma'):
                response['Pragma'] = 'no-cache'
            patch_vary_headers(response, ('Cookie',))

        return response

