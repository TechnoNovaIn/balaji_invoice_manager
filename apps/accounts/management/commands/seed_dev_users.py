from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from apps.accounts.models import Department, UserRole

User = get_user_model()


class Command(BaseCommand):
    help = "Seeds development-only test accounts for Employee, Naveen, Admin, and Management."

    def handle(self, *args, **options):
        self.stdout.write("Seeding development departments and test accounts...")

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

        # 2. Test User Accounts
        test_accounts = [
            {
                "username": "admin",
                "password": "BalajiAdmin@2026",
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
                "password": "BalajiNaveen@2026",
                "role": UserRole.NAVEEN,
                "first_name": "Naveen",
                "last_name": "Accounts",
                "email": "naveen@balajitransports.com",
                "department": ops_dept,
                "is_staff": False,
                "is_superuser": False
            },
            {
                "username": "employee",
                "password": "BalajiEmp@2026",
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
                "password": "BalajiMgmt@2026",
                "role": UserRole.MANAGEMENT,
                "first_name": "Director",
                "last_name": "Balaji",
                "email": "management@balajitransports.com",
                "department": ops_dept,
                "is_staff": False,
                "is_superuser": False
            }
        ]

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
            # Ensure password is set correctly
            user.set_password(acc["password"])
            user.role = acc["role"]
            user.first_name = acc["first_name"]
            user.last_name = acc["last_name"]
            user.department = acc["department"]
            user.is_active = True
            user.is_staff = acc["is_staff"]
            user.is_superuser = acc["is_superuser"]
            user.save()

            action_label = "Created" if created else "Updated password for"
            self.stdout.write(self.style.SUCCESS(
                f" - {action_label} [{user.get_role_display()}]: Username='{user.username}' / Password='{acc['password']}'"
            ))

        self.stdout.write(self.style.SUCCESS("Seeding complete. All 4 development accounts are active."))

