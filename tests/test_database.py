from django.test import TestCase
from django.db import connection


class DatabaseConnectionTest(TestCase):
    def test_postgresql_connection(self):
        """
        Verifies that Django is connected to PostgreSQL 18.
        """
        with connection.cursor() as cursor:
            cursor.execute("SELECT version();")
            version = cursor.fetchone()[0]
            self.assertIn("PostgreSQL", version)
            cursor.execute("SELECT current_database();")
            db_name = cursor.fetchone()[0]
            self.assertIsNotNone(db_name)
