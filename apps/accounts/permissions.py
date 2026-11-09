"""
Role-Based Access Control (RBAC) Enforcers for Balaji InvoiceHub.
Enforced at the Django view and API level — never relying only on hiding frontend buttons.
"""

from functools import wraps
from typing import Sequence, Union
from django.core.exceptions import PermissionDenied
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import AccessMixin
from apps.accounts.models import UserRole


def check_user_role(user, allowed_roles: Union[Sequence[str], str]) -> bool:
    """
    Evaluates whether the given user possesses any of the permitted roles.
    Superusers automatically pass Admin role checks.
    """
    if not user or not user.is_authenticated or not user.is_active:
        return False

    if isinstance(allowed_roles, str):
        allowed_roles = [allowed_roles]

    if user.is_superuser and (UserRole.ADMIN in allowed_roles):
        return True

    return user.role in allowed_roles


def role_required(allowed_roles: Union[Sequence[str], str], require_safe_method: bool = False):
    """
    Decorator for views that checks that the user is logged in and possesses
    at least one of the allowed roles, raising PermissionDenied (HTTP 403) otherwise.
    If require_safe_method is True, mutations (POST/PUT/DELETE) are strictly blocked.
    """
    def decorator(view_func):
        @wraps(view_func)
        @login_required
        def _wrapped_view(request, *args, **kwargs):
            if not check_user_role(request.user, allowed_roles):
                raise PermissionDenied(
                    f"Access Denied: Your assigned role '{request.user.get_role_display()}' "
                    f"does not have permission to access this module."
                )
            
            # Management role strict read-only enforcement
            if request.user.role == UserRole.MANAGEMENT or require_safe_method:
                if request.method not in ('GET', 'HEAD', 'OPTIONS'):
                    raise PermissionDenied(
                        "Access Denied: Management role has strictly READ-ONLY privileges. "
                        "State-modifying operations (POST, PUT, DELETE) are prohibited."
                    )
            return view_func(request, *args, **kwargs)
        return _wrapped_view
    return decorator


# Strict Segregated Role Decorators
employee_required = role_required([UserRole.EMPLOYEE])
naveen_required = role_required([UserRole.NAVEEN])
admin_required = role_required([UserRole.ADMIN])
management_required = role_required([UserRole.MANAGEMENT, UserRole.NAVEEN, UserRole.ADMIN], require_safe_method=True)


class RoleRequiredMixin(AccessMixin):
    """
    CBV mixin that verifies the current user has one of the allowed roles.
    Raises PermissionDenied (403) on unauthorized access attempts.
    """
    allowed_roles: Sequence[str] = ()
    require_safe_method: bool = False

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()

        if not check_user_role(request.user, self.allowed_roles):
            raise PermissionDenied(
                f"Access Denied: Your assigned role '{request.user.get_role_display()}' "
                f"is not authorized to perform this operation."
            )

        if (request.user.role == UserRole.MANAGEMENT or self.require_safe_method) and request.method not in ('GET', 'HEAD', 'OPTIONS'):
            raise PermissionDenied("Access Denied: Read-only access policy prevents data modification.")

        return super().dispatch(request, *args, **kwargs)
