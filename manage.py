#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys


def main():
    """Run administrative tasks."""
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'invoicehub.settings')
    if len(sys.argv) > 1 and sys.argv[1] == 'runserver':
        # Django's built-in runserver speaks HTTP only (no TLS/HTTPS), so disable
        # HTTPS redirect/secure cookies during local runserver without altering
        # production .env, WSGI, or `check --deploy` settings.
        os.environ.setdefault('DJANGO_ENABLE_SSL', 'False')
        if '--insecure' not in sys.argv and '--nostatic' not in sys.argv and '-h' not in sys.argv and '--help' not in sys.argv:
            sys.argv.append('--insecure')
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
