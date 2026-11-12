from django.apps import AppConfig
from django.db.backends.signals import connection_created


def _configure_sqlite_pragmas(sender, connection, **kwargs):
    if connection.vendor == 'sqlite':
        with connection.cursor() as cursor:
            cursor.execute('PRAGMA journal_mode=WAL;')
            cursor.execute('PRAGMA synchronous=NORMAL;')
            cursor.execute('PRAGMA foreign_keys=ON;')
            cursor.execute('PRAGMA busy_timeout=30000;')


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.core'
    verbose_name = 'Core Application Services'

    def ready(self):
        connection_created.connect(_configure_sqlite_pragmas)

