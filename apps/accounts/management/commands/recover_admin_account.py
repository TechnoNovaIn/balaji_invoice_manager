"""
Controlled server-side Administrator Recovery command for authorized IT/management personnel.

No hardcoded passwords and no HTTP recovery endpoints are exposed.
Usage:
    python manage.py recover_admin_account --username <admin_username> --operator "<IT Officer Name>" --reason "<Ticket Ref>"
"""
import getpass
import secrets
import string

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.accounts.models import AccountStatus, UserRole
from apps.accounts.security import (
    clear_login_rate_limit,
    invalidate_user_sessions,
    record_audit_log,
)

User = get_user_model()


def _generate_strong_temp_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    required = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%^&*"),
    ]
    rest = [secrets.choice(alphabet) for _ in range(max(12, length) - len(required))]
    chars = required + rest
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


class Command(BaseCommand):
    help = (
        "Controlled server-side administrator recovery: reactivates a locked/inaccessible "
        "administrator account, sets a validated new password (no hardcoded defaults), clears "
        "rate-limit cooldowns, and writes an immutable AuditLog entry."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--username',
            required=True,
            help="Username of the existing administrator account to recover.",
        )
        parser.add_argument(
            '--operator',
            required=True,
            help="Name/identifier of the authorized IT or management operator executing recovery.",
        )
        parser.add_argument(
            '--reason',
            required=True,
            help="Audit reason or ticket reference authorizing this recovery.",
        )
        parser.add_argument(
            '--generate-temp-password',
            action='store_true',
            help="Generate a cryptographic one-time temporary password printed once to stdout.",
        )
        parser.add_argument(
            '--grant-accounts-access',
            action='store_true',
            help="Also ensure the recovered administrator has Accounts Operations capability.",
        )

    def handle(self, *args, **options):
        username = options['username'].strip()
        operator = options['operator'].strip()
        reason = options['reason'].strip()
        if not operator or not reason:
            raise CommandError("Both --operator and --reason are required for audit accountability.")

        user = User.objects.filter(username__iexact=username).first()
        if not user:
            raise CommandError(f"User '{username}' does not exist.")
        if not (user.role in (UserRole.ADMIN, UserRole.NAVEEN) or user.is_superuser or user.has_admin_access):
            raise CommandError(
                f"Refused: '{user.username}' is not an Administrator account (role={user.role})."
            )

        generated = False
        if options['generate_temp_password']:
            new_password = _generate_strong_temp_password()
            generated = True
        else:
            pw1 = getpass.getpass(f"Enter new strong password for '{user.username}': ")
            pw2 = getpass.getpass("Confirm new password: ")
            if pw1 != pw2:
                raise CommandError("Passwords do not match. Recovery aborted.")
            new_pw = pw1.strip()
            try:
                validate_password(new_pw, user=user)
            except ValidationError as exc:
                raise CommandError("Password rejected: " + "; ".join(exc.messages))
            new_password = new_pw

        user.set_password(new_password)
        user.is_active = True
        user.account_status = AccountStatus.ACTIVE
        user.suspended_at = None
        user.suspended_reason = ''
        user.has_admin_access = True
        if options['grant_accounts_access'] or user.role == UserRole.NAVEEN:
            user.has_accounts_access = True
        user.must_change_password = True
        user.save()

        invalidate_user_sessions(user)
        clear_login_rate_limit(None, user.username)

        record_audit_log(
            user,
            "ADMIN_RECOVERY_EXECUTED",
            request=None,
            details={
                "recovered_username": user.username,
                "executed_by_operator": operator,
                "reason": reason,
                "password_method": "generated_one_time" if generated else "interactive_prompt",
                "executed_at": timezone.now().isoformat(),
            },
        )

        self.stdout.write(self.style.SUCCESS(f"Administrator account '{user.username}' recovered cleanly."))
        self.stdout.write("Active sessions invalidated, cooldowns cleared, and must_change_password=True enforced.")
        if generated:
            self.stdout.write(self.style.WARNING(f"One-time temporary password (deliver securely out-of-band): {new_password}"))

