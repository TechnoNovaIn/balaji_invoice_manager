from django.test import TestCase, Client
from django.urls import reverse
from apps.accounts.models import User, UserRole, Department


class BrowserEndpointsTest(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name="Operations", code="OPS")
        self.accounts = {
            'employee': User.objects.create_user(username='employee', password='BalajiEmp@2026', role=UserRole.EMPLOYEE, department=self.dept),
            'naveen': User.objects.create_user(username='naveen', password='BalajiNaveen@2026', role=UserRole.NAVEEN, department=self.dept),
            'admin': User.objects.create_user(username='admin', password='BalajiAdmin@2026', role=UserRole.ADMIN, department=self.dept, is_staff=True),
            'management': User.objects.create_user(username='management', password='BalajiMgmt@2026', role=UserRole.MANAGEMENT, department=self.dept),
        }

    def test_unauthenticated_user_redirected_to_login(self):
        client = Client()
        resp = client.get('/')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/accounts/login/', resp.headers['Location'])

    def test_login_page_renders_cleanly(self):
        client = Client()
        resp = client.get('/accounts/login/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Balaji InvoiceHub")
        self.assertContains(resp, "Sign In")

    def test_all_roles_login_and_routing(self):
        destinations = {
            'employee': '/dashboard/employee/',
            'naveen': '/dashboard/naveen/',
            'admin': '/dashboard/admin/',
            'management': '/dashboard/management/',
        }
        passwords = {
            'employee': 'BalajiEmp@2026',
            'naveen': 'BalajiNaveen@2026',
            'admin': 'BalajiAdmin@2026',
            'management': 'BalajiMgmt@2026',
        }

        for role_name, expected_url in destinations.items():
            client = Client()
            login_resp = client.post('/accounts/login/', {
                'username': role_name,
                'password': passwords[role_name]
            })
            self.assertEqual(login_resp.status_code, 302)
            
            # Root dashboard router redirect
            router_resp = client.get('/')
            self.assertEqual(router_resp.status_code, 302)
            self.assertEqual(router_resp.headers['Location'], expected_url)

            # Dashboard page render
            dash_resp = client.get(expected_url)
            self.assertEqual(dash_resp.status_code, 200)

    def test_admin_backup_page_and_user_manager_renders(self):
        client = Client()
        client.login(username='admin', password='BalajiAdmin@2026')
        
        backup_resp = client.get('/backup/')
        self.assertEqual(backup_resp.status_code, 200)
        self.assertContains(backup_resp, "Backup & Disaster Recovery Center")

        user_list_resp = client.get('/accounts/manage/users/')
        self.assertEqual(user_list_resp.status_code, 200)
        self.assertContains(user_list_resp, "User & Role Administration")

    def test_health_check_endpoint(self):
        client = Client()
        resp = client.get('/health/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn("CONNECTED", resp.json().get('database', ''))

    def test_phase3_employee_and_naveen_browser_pages(self):
        # 1. Employee access to upload and my-submissions
        emp_client = Client()
        emp_client.login(username='employee', password='BalajiEmp@2026')

        upload_page = emp_client.get('/invoices/upload/')
        self.assertEqual(upload_page.status_code, 200)
        self.assertContains(upload_page, "Invoice Receipt Capture")
        self.assertContains(upload_page, "Take Photo")
        self.assertContains(upload_page, "Upload PDF / Files")

        submissions_page = emp_client.get('/invoices/my-submissions/')
        self.assertEqual(submissions_page.status_code, 200)
        self.assertContains(submissions_page, "My Sent Invoices")

        # 2. Naveen dashboard renders live queue
        naveen_client = Client()
        naveen_client.login(username='naveen', password='BalajiNaveen@2026')
        naveen_dash = naveen_client.get('/dashboard/naveen/')
        self.assertEqual(naveen_dash.status_code, 200)
        self.assertContains(naveen_dash, "Incoming Invoices & Receipts")

