from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model
from apps.accounts.models import UserRole, AuditLog

User = get_user_model()


class AuthenticationTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.password = "ValidPassword@123"
        self.user = User.objects.create_user(
            username="test_emp",
            password=self.password,
            role=UserRole.EMPLOYEE
        )

    def test_login_success(self):
        url = reverse('accounts:login')
        response = self.client.post(url, {
            'username': 'test_emp',
            'password': self.password
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('core:dashboard_router'))
        self.assertTrue(AuditLog.objects.filter(user=self.user, action="USER_LOGIN_SUCCESS").exists())

    def test_login_failure_records_audit_log(self):
        url = reverse('accounts:login')
        response = self.client.post(url, {
            'username': 'test_emp',
            'password': 'WrongPassword@999'
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(AuditLog.objects.filter(action="USER_LOGIN_FAILED").exists())

    def test_inactive_user_cannot_login(self):
        self.user.is_active = False
        self.user.save()

        url = reverse('accounts:login')
        response = self.client.post(url, {
            'username': 'test_emp',
            'password': self.password
        })
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_logout_flushes_session(self):
        self.client.login(username="test_emp", password=self.password)
        url = reverse('accounts:logout')
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('accounts:login'))
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertTrue(AuditLog.objects.filter(user=self.user, action="USER_LOGOUT").exists())

    def test_first_time_demo_password_redirects_to_change_password(self):
        """
        Employees signing in with the default demo password 'BalajiEmp@2026'
        are automatically prompted to set their personal password.
        """
        emp_demo = User.objects.create_user(
            username="new_employee",
            password="BalajiEmp@2026",
            role=UserRole.EMPLOYEE,
            must_change_password=True
        )

        url = reverse('accounts:login')
        response = self.client.post(url, {
            'username': 'new_employee',
            'password': 'BalajiEmp@2026',
            'remember_me': 'on',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('accounts:change_password'))

        emp_demo.refresh_from_db()
        self.assertTrue(emp_demo.must_change_password)

    def test_change_password_workflow(self):
        """
        Employees can easily set a personal password and remain logged in.
        """
        emp_demo = User.objects.create_user(
            username="emp_mobile",
            password="BalajiEmp@2026",
            role=UserRole.EMPLOYEE,
            must_change_password=True
        )
        self.client.login(username="emp_mobile", password="BalajiEmp@2026")

        change_url = reverse('accounts:change_password')
        resp = self.client.post(change_url, {
            'new_password': 'MyNewPersonalPass123',
            'confirm_password': 'MyNewPersonalPass123'
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse('core:dashboard_router'))

        emp_demo.refresh_from_db()
        self.assertFalse(emp_demo.must_change_password)
        self.assertTrue(emp_demo.check_password('MyNewPersonalPass123'))
        # Verify session is still authenticated (not logged out)
        self.assertEqual(int(self.client.session['_auth_user_id']), emp_demo.pk)

    def test_remember_me_persistent_session(self):
        """
        When 'Keep me signed in on this phone' is selected, session expiry is set to 1 year.
        """
        url = reverse('accounts:login')
        response = self.client.post(url, {
            'username': 'test_emp',
            'password': self.password,
            'remember_me': 'on',
        })
        self.assertEqual(response.status_code, 302)
        # Session expiry should be 31536000 (1 year)
        self.assertEqual(self.client.session.get_expiry_age(), 31536000)
