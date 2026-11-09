from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model
from apps.accounts.models import UserRole

User = get_user_model()


class RBACPermissionsTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.password = "SecurePassword@123"
        self.emp = User.objects.create_user(username="emp_user", password=self.password, role=UserRole.EMPLOYEE)
        self.naveen = User.objects.create_user(username="naveen_user", password=self.password, role=UserRole.NAVEEN)
        self.admin = User.objects.create_user(username="admin_user", password=self.password, role=UserRole.ADMIN)
        self.mgmt = User.objects.create_user(username="mgmt_user", password=self.password, role=UserRole.MANAGEMENT)

    def test_unauthenticated_redirects_to_login(self):
        for endpoint in ['employee_dashboard', 'naveen_dashboard', 'admin_dashboard', 'management_dashboard']:
            url = reverse(f'core:{endpoint}')
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 302)
            self.assertIn('/accounts/login/', resp.url)

    def test_employee_dashboard_access(self):
        url = reverse('core:employee_dashboard')

        # Employee: OK (200)
        self.client.force_login(self.emp)
        self.assertEqual(self.client.get(url).status_code, 200)

        # Admin: Forbidden (403) - Segregation: Admin cannot access employee submission interface directly
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(url).status_code, 403)

        # Naveen: Forbidden (403)
        self.client.force_login(self.naveen)
        self.assertEqual(self.client.get(url).status_code, 403)

        # Management: Forbidden (403)
        self.client.force_login(self.mgmt)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_naveen_dashboard_access(self):
        url = reverse('core:naveen_dashboard')

        # Naveen: OK (200)
        self.client.force_login(self.naveen)
        self.assertEqual(self.client.get(url).status_code, 200)

        # Employee: Forbidden (403)
        self.client.force_login(self.emp)
        self.assertEqual(self.client.get(url).status_code, 403)

        # Management: Forbidden (403)
        self.client.force_login(self.mgmt)
        self.assertEqual(self.client.get(url).status_code, 403)

        # Regular Admin: Forbidden (403) - Naveen's accounts tasks are segregated
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_admin_dashboard_access(self):
        url = reverse('core:admin_dashboard')

        # Admin: OK (200)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(url).status_code, 200)

        # Employee: Forbidden (403)
        self.client.force_login(self.emp)
        self.assertEqual(self.client.get(url).status_code, 403)

        # Naveen: Forbidden (403)
        self.client.force_login(self.naveen)
        self.assertEqual(self.client.get(url).status_code, 403)

        # Management: Forbidden (403)
        self.client.force_login(self.mgmt)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_management_dashboard_access(self):
        url = reverse('core:management_dashboard')

        # Management: OK (200)
        self.client.force_login(self.mgmt)
        self.assertEqual(self.client.get(url).status_code, 200)

        # Naveen: OK (200)
        self.client.force_login(self.naveen)
        self.assertEqual(self.client.get(url).status_code, 200)

        # Admin: OK (200)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(url).status_code, 200)

        # Employee: Forbidden (403)
        self.client.force_login(self.emp)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_management_strictly_read_only(self):
        url = reverse('core:management_dashboard')
        self.client.force_login(self.mgmt)
        # Attempting POST mutation must be blocked with 403
        resp = self.client.post(url, {})
        self.assertEqual(resp.status_code, 403)

    def test_user_management_access_restricted_to_admin(self):
        url = reverse('accounts:user_list')

        # Admin: OK (200)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(url).status_code, 200)

        # Employee: Forbidden (403)
        self.client.force_login(self.emp)
        self.assertEqual(self.client.get(url).status_code, 403)

        # Naveen: Forbidden (403)
        self.client.force_login(self.naveen)
        self.assertEqual(self.client.get(url).status_code, 403)

        # Management: Forbidden (403)
        self.client.force_login(self.mgmt)
        self.assertEqual(self.client.get(url).status_code, 403)
