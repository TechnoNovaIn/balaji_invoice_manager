from django.contrib.auth.backends import ModelBackend
from django.contrib.auth import get_user_model
from django.conf import settings
from django.db.models import Q

User = get_user_model()


class CaseInsensitiveOrPhoneAuthBackend(ModelBackend):
    """
    Allows authentication by:
    1. Case-insensitive username (e.g. 'employee', 'Employee', 'Naveen', 'naveen')
    2. Phone number matching (as indicated by the login UI 'Username or Phone')
    3. Seamless test logins in development environment.
    """
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

        # 1. Standard password check
        if user.check_password(password):
            return user

        # 2. Friendly development environment password tolerance
        if settings.DEBUG:
            dev_credentials = {
                'employee': ['balajiemp@2026', 'employee', 'employee123', 'balaji123', '123456'],
                'naveen': ['balajinaveen@2026', 'naveen', 'naveen123', 'balaji123', '123456'],
                'admin': ['balajiadmin@2026', 'admin', 'admin123', 'balaji123', '123456'],
                'management': ['balajimgmt@2026', 'management', 'management123', 'balaji123', '123456'],
            }
            allowed = dev_credentials.get(user.username.lower(), [])
            if password.strip().lower() in allowed:
                return user

        return None

