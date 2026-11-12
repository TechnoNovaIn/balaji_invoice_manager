from django.contrib.auth.backends import ModelBackend
from django.contrib.auth import get_user_model
from django.db.models import Q

User = get_user_model()


class CaseInsensitiveOrPhoneAuthBackend(ModelBackend):
    """
    Allows authentication by:
    1. Case-insensitive username (e.g. 'employee', 'Employee', 'Naveen', 'naveen')
    2. Phone number matching (as indicated by the login UI 'Username or Phone')
    Strictly verifies passwords via Django's cryptographic password hasher.
    """
    def user_can_authenticate(self, user):
        return bool(super().user_can_authenticate(user) and getattr(user, 'is_operational_active', user.is_active))

    def authenticate(self, request, username=None, password=None, **kwargs):
        if not username or not password:
            return None

        # Clean string
        query_val = str(username).strip()

        try:
            # Query either case-insensitive username or exact phone number
            user = User.objects.filter(
                Q(username__iexact=query_val) | Q(phone_number=query_val)
            ).first()
        except Exception:
            return None

        if not user or not self.user_can_authenticate(user):
            return None

        # Standard cryptographic password check
        if user.check_password(password):
            return user

        return None

