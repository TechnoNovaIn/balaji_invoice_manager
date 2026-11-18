from django.contrib.auth.backends import ModelBackend
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from .security import check_login_rate_limit, record_login_failure, clear_login_rate_limit

User = get_user_model()


class CaseInsensitiveOrPhoneAuthBackend(ModelBackend):
    """
    Allows authentication by:
    1. Case-insensitive username (e.g. 'employee', 'Employee', 'Naveen', 'naveen')
    2. Phone number matching (as indicated by the login UI 'Username or Phone')
    Strictly verifies passwords via Django's cryptographic password hasher.
    Enforces brute-force lockout and rate limiting at the authentication backend level
    so ALL login surfaces (including Django Admin and APIs) are protected.
    """
    def user_can_authenticate(self, user):
        return bool(super().user_can_authenticate(user) and getattr(user, 'is_operational_active', user.is_active))

    def authenticate(self, request, username=None, password=None, **kwargs):
        if not username or not password:
            return None

        # Clean string
        query_val = str(username).strip()

        # Enforce rate limit / temporary cooldown at backend level
        if request is not None:
            is_blocked, retry_after = check_login_rate_limit(request, query_val)
            if is_blocked:
                raise PermissionDenied(
                    f"Too many failed sign-in attempts for '{query_val}'. "
                    f"Please wait {retry_after} seconds before trying again."
                )

        try:
            # Query either case-insensitive username or exact phone number
            user = User.objects.filter(
                Q(username__iexact=query_val) | Q(phone_number=query_val)
            ).first()
        except Exception:
            return None

        if not user or not self.user_can_authenticate(user):
            if request is not None:
                record_login_failure(request, query_val)
            return None

        # Standard cryptographic password check
        if user.check_password(password):
            if request is not None:
                clear_login_rate_limit(request, query_val)
            return user

        if request is not None:
            record_login_failure(request, query_val)

        return None

