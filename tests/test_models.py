from django.test import TestCase
from django.core.exceptions import PermissionDenied
from django.contrib.auth import get_user_model
from apps.accounts.models import Department, UserRole, AuditLog

User = get_user_model()


class AccountsModelsTest(TestCase):
    def test_department_creation(self):
        dept = Department.objects.create(name="Fleet Maintenance", code="FLEET")
        self.assertEqual(dept.name, "Fleet Maintenance")
        self.assertEqual(dept.code, "FLEET")
        self.assertTrue(dept.is_active)
        self.assertEqual(str(dept), "Fleet Maintenance (FLEET)")

    def test_user_roles_and_capabilities(self):
        # 1. Employee
        emp = User.objects.create_user(
            username="emp_murugan",
            password="TestPassword@123",
            role=UserRole.EMPLOYEE,
            phone_number="9876543210"
        )
        self.assertTrue(emp.is_employee)
        self.assertFalse(emp.is_naveen)
        self.assertFalse(emp.is_admin_role)
        self.assertFalse(emp.is_management)
        self.assertTrue(emp.can_submit_invoices())
        self.assertFalse(emp.can_verify_invoices())
        self.assertFalse(emp.can_record_advances())
        self.assertFalse(emp.can_manage_users())

        # 2. Naveen
        naveen = User.objects.create_user(
            username="naveen_accounts",
            password="TestPassword@123",
            role=UserRole.NAVEEN
        )
        self.assertFalse(naveen.is_employee)
        self.assertTrue(naveen.is_naveen)
        self.assertFalse(naveen.is_admin_role)
        self.assertTrue(naveen.can_verify_invoices())
        self.assertTrue(naveen.can_record_advances())
        self.assertTrue(naveen.can_convert_tally())
        self.assertTrue(naveen.can_manage_reminders())
        self.assertTrue(naveen.can_view_financial_reports())
        self.assertFalse(naveen.can_manage_users())

        # 3. Admin
        admin_user = User.objects.create_user(
            username="sys_admin",
            password="TestPassword@123",
            role=UserRole.ADMIN
        )
        self.assertTrue(admin_user.is_admin_role)
        self.assertTrue(admin_user.can_manage_users())
        self.assertTrue(admin_user.can_trigger_backups())
        # Segregation of duties: Admin cannot verify invoices without Naveen role
        self.assertFalse(admin_user.can_verify_invoices())

        # 4. Management
        mgmt = User.objects.create_user(
            username="director_balaji",
            password="TestPassword@123",
            role=UserRole.MANAGEMENT
        )
        self.assertTrue(mgmt.is_management)
        self.assertTrue(mgmt.can_view_financial_reports())
        self.assertFalse(mgmt.can_verify_invoices())
        self.assertFalse(mgmt.can_record_advances())

    def test_audit_log_creation_and_immutability(self):
        user = User.objects.create_user(username="audit_test", password="TestPassword@123")
        log = AuditLog.objects.create(
            user=user,
            action="TEST_ACTION",
            ip_address="127.0.0.1",
            details={"key": "value"}
        )
        self.assertIsNotNone(log.id)
        self.assertEqual(log.action, "TEST_ACTION")
        self.assertIn("TEST_ACTION", str(log))

        # Test immutability: editing must raise PermissionDenied
        log.action = "MODIFIED_ACTION"
        with self.assertRaises(PermissionDenied):
            log.save()

        # Test immutability: deletion must raise PermissionDenied
        with self.assertRaises(PermissionDenied):
            log.delete()
