import secrets
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth import get_user_model
from apps.accounts.models import Department, UserRole

User = get_user_model()


class Command(BaseCommand):
    help = "Seeds development-only test accounts with randomized secure passwords. Forbidden in production."

    def handle(self, *args, **options):
        if not getattr(settings, 'DEBUG', False):
            raise CommandError(
                "Refusing to execute: seed_dev_users is strictly forbidden in production environments (DEBUG=False). "
                "Production accounts must be provisioned individually via 'createsuperuser' or the Admin portal."
            )

        self.stdout.write(self.style.WARNING("Seeding development departments and randomized test accounts..."))

        # 1. Departments
        fleet_dept, _ = Department.objects.get_or_create(
            code="FLEET",
            defaults={"name": "Fleet Maintenance", "description": "Vehicle repairs, tires, servicing"}
        )
        fuel_dept, _ = Department.objects.get_or_create(
            code="FUEL",
            defaults={"name": "Fuel Operations", "description": "Diesel tracking, station bills"}
        )
        ops_dept, _ = Department.objects.get_or_create(
            code="OPS",
            defaults={"name": "General Transport & Logistics", "description": "Trip operations, loading, toll bills"}
        )

        # 2. Test User Accounts (Random cryptographically generated passwords, must_change_password=True)
        test_accounts = [
            {
                "username": "admin",
                "role": UserRole.ADMIN,
                "first_name": "System",
                "last_name": "Administrator",
                "email": "admin@balajitransports.com",
                "department": ops_dept,
                "is_staff": True,
                "is_superuser": True
            },
            {
                "username": "naveen",
                "role": UserRole.NAVEEN,
                "first_name": "Naveen",
                "last_name": "Accounts",
                "email": "naveen@balajitransports.com",
                "department": ops_dept,
                "is_staff": True,
                "is_superuser": False,
                "has_admin_access": True,
                "has_accounts_access": True,
            },
            {
                "username": "employee",
                "role": UserRole.EMPLOYEE,
                "first_name": "S.",
                "last_name": "Murugan",
                "email": "murugan.employee@balajitransports.com",
                "department": fuel_dept,
                "is_staff": False,
                "is_superuser": False
            },
            {
                "username": "management",
                "role": UserRole.MANAGEMENT,
                "first_name": "Director",
                "last_name": "Balaji",
                "email": "management@balajitransports.com",
                "department": ops_dept,
                "is_staff": False,
                "is_superuser": False
            }
        ]

        generated_credentials = []

        for acc in test_accounts:
            user, created = User.objects.get_or_create(
                username=acc["username"],
                defaults={
                    "role": acc["role"],
                    "first_name": acc["first_name"],
                    "last_name": acc["last_name"],
                    "email": acc["email"],
                    "department": acc["department"],
                    "is_staff": acc["is_staff"],
                    "is_superuser": acc["is_superuser"]
                }
            )
            # Generate one-time random password and force password change on first login
            temp_password = f"Balaji!{secrets.token_urlsafe(12)}#"
            user.set_password(temp_password)
            user.must_change_password = True
            user.role = acc["role"]
            user.first_name = acc["first_name"]
            user.last_name = acc["last_name"]
            user.department = acc["department"]
            user.is_active = True
            user.is_staff = acc["is_staff"]
            user.is_superuser = acc["is_superuser"]
            user.has_admin_access = acc.get("has_admin_access", False)
            user.has_accounts_access = acc.get("has_accounts_access", False)
            user.save()

            action_label = "Created" if created else "Reset credentials for"
            generated_credentials.append((user.username, user.get_role_display(), temp_password))
            self.stdout.write(self.style.SUCCESS(
                f" - {action_label} [{user.get_role_display()}]: Username='{user.username}' (must_change_password=True)"
            ))

        self.stdout.write(self.style.SUCCESS("\nSeeding complete. One-time credentials generated (change on first login):"))
        for uname, rname, pwd in generated_credentials:
            self.stdout.write(f"  * {uname} ({rname}): {pwd}")

