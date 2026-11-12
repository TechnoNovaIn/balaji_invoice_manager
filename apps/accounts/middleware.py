from django.contrib import messages
from django.contrib.auth import logout
from django.shortcuts import redirect
from django.urls import reverse


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
                ]
                path = request.path
                if not any(path.startswith(p) for p in allowed_paths) and not path.startswith('/static/'):
                    return redirect('accounts:change_password')

        return self.get_response(request)

