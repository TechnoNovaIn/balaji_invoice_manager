from django.test import TestCase
from django.db import connection


class DatabaseConnectionTest(TestCase):
    def test_database_connection(self):
        """
        Verifies that Django is connected to the configured database (SQLite or PostgreSQL).
        """
        with connection.cursor() as cursor:
            if connection.vendor == 'sqlite':
                cursor.execute("SELECT sqlite_version();")
                version = cursor.fetchone()[0]
                self.assertTrue(bool(version))
            else:
                cursor.execute("SELECT version();")
                version = cursor.fetchone()[0]
                self.assertIn("PostgreSQL", version)
                cursor.execute("SELECT current_database();")
                db_name = cursor.fetchone()[0]
                self.assertIsNotNone(db_name)
