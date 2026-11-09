from django.shortcuts import redirect
from django.urls import reverse


class ForcePasswordChangeMiddleware:
    """
    Middleware that ensures users with must_change_password=True (such as employees
    logging in with the initial demo password) must set their own personal password
    before accessing any other portal features.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and getattr(request.user, 'must_change_password', False):
            # Whitelisted URLs that must be accessible while changing password
            allowed_paths = [
                reverse('accounts:change_password'),
                reverse('accounts:logout'),
            ]
            path = request.path
            if not any(path.startswith(p) for p in allowed_paths) and not path.startswith('/static/'):
                return redirect('accounts:change_password')

        return self.get_response(request)

