#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys


def _is_explicit_local_runserver(argv):
    """Return True only when runserver targets loopback or RUNSERVER_ALLOW_INSECURE=1 is set."""
    if os.getenv('RUNSERVER_ALLOW_INSECURE', '').strip().lower() in ('1', 'true', 'yes'):
        return True
    addr_args = [a for a in argv[2:] if not a.startswith('-')]
    if not addr_args:
        return True  # Django runserver defaults to 127.0.0.1:8000
    target = addr_args[0].strip().lower()
    if target.isdigit():
        return True  # Port-only defaults to 127.0.0.1:<port>
    host = target.rsplit(':', 1)[0].strip('[]')
    return host in ('127.0.0.1', 'localhost', '::1')


def main():
    """Run administrative tasks."""
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'invoicehub.settings')
    if len(sys.argv) > 1 and sys.argv[1] == 'runserver':
        is_help = '-h' in sys.argv or '--help' in sys.argv
        if not is_help and _is_explicit_local_runserver(sys.argv):
            # Django's built-in runserver speaks HTTP only (no TLS/HTTPS), so disable
            # HTTPS redirect/secure cookies for explicit local loopback development.
            os.environ.setdefault('DJANGO_ENABLE_SSL', 'False')
            if '--insecure' not in sys.argv and '--nostatic' not in sys.argv:
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
