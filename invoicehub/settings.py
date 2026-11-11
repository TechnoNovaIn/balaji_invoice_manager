"""
Django settings for Balaji InvoiceHub project.
Internal Operations & Invoice Management System for Balaji Transports.
"""

from pathlib import Path
import os
import sys
import secrets
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Add apps folder to Python path
sys.path.insert(0, str(BASE_DIR / 'apps'))

# Load environment variables from .env
load_dotenv(BASE_DIR / '.env')

DEBUG = os.getenv('DJANGO_DEBUG', 'False').lower() in ('true', '1', 't')

SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', '').strip()
if not SECRET_KEY or SECRET_KEY.startswith('django-insecure-') or SECRET_KEY == 'default-insecure-secret-key-change-in-env' or len(SECRET_KEY) < 50:
    if not DEBUG:
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY must be set to a secure random string (>= 50 chars, without 'django-insecure-' prefix) when DEBUG=False."
        )
    SECRET_KEY = secrets.token_urlsafe(50)

# Configurable Allowed Hosts via environment (never allow wildcard '*' in production)
allowed_hosts_env = os.getenv('DJANGO_ALLOWED_HOSTS', '')
if allowed_hosts_env:
    ALLOWED_HOSTS = [h.strip() for h in allowed_hosts_env.split(',') if h.strip()]
elif not DEBUG:
    ALLOWED_HOSTS = ['invoicehub.balajitransports.com', 'localhost', '127.0.0.1']
else:
    ALLOWED_HOSTS = ['localhost', '127.0.0.1']

if not DEBUG:
    ALLOWED_HOSTS = [h for h in ALLOWED_HOSTS if h != '*']
    if not ALLOWED_HOSTS:
        ALLOWED_HOSTS = ['invoicehub.balajitransports.com', 'localhost', '127.0.0.1']

csrf_origins_env = os.getenv('DJANGO_CSRF_TRUSTED_ORIGINS', '')
if csrf_origins_env:
    CSRF_TRUSTED_ORIGINS = [o.strip() for o in csrf_origins_env.split(',') if o.strip()]
elif not DEBUG:
    CSRF_TRUSTED_ORIGINS = [
        'https://invoicehub.balajitransports.com',
        'https://localhost',
        'https://127.0.0.1',
    ]
else:
    CSRF_TRUSTED_ORIGINS = [
        'http://localhost:8888',
        'http://localhost:8000',
        'http://127.0.0.1:8888',
        'http://127.0.0.1:8000',
    ]

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Third-party packages
    'rest_framework',

    # Local Balaji InvoiceHub applications
    'apps.accounts.apps.AccountsConfig',
    'apps.core.apps.CoreConfig',
    'apps.backup.apps.BackupConfig',
    'apps.invoices.apps.InvoicesConfig',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'apps.accounts.middleware.ForcePasswordChangeMiddleware',
]

ROOT_URLCONF = 'invoicehub.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'apps.core.context_processors.app_info',
            ],
        },
    },
]

WSGI_APPLICATION = 'invoicehub.wsgi.application'

# Database Configuration (Strictly PostgreSQL 18 - SQLite is prohibited in production)
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.getenv('DB_NAME', 'balaji_invoicehub_db'),
        'USER': os.getenv('DB_USER', 'postgres'),
        'PASSWORD': os.getenv('DB_PASSWORD', ''),
        'HOST': os.getenv('DB_HOST', '127.0.0.1'),
        'PORT': os.getenv('DB_PORT', '5432'),
        'CONN_MAX_AGE': 600,
        'OPTIONS': {
            'connect_timeout': 10,
        }
    }
}

# Custom User Model definition (Mandatory before initial migration)
AUTH_USER_MODEL = 'accounts.User'

AUTHENTICATION_BACKENDS = [
    'apps.accounts.backends.CaseInsensitiveOrPhoneAuthBackend',
    'django.contrib.auth.backends.ModelBackend',
]

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
        'OPTIONS': {'min_length': 8},
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization & Timezone
LANGUAGE_CODE = 'en-us'
TIME_ZONE = os.getenv('TIME_ZONE', 'Asia/Kolkata')
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'

# Media files (Protected Invoices & Receipts)
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Session Security & Authentication Rules
LOGIN_URL = 'accounts:login'
LOGIN_REDIRECT_URL = 'core:dashboard_router'
LOGOUT_REDIRECT_URL = 'accounts:login'

SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE = 'Lax'
# Persistent 1-year mobile session: Employees stay logged in without remembering passwords
SESSION_COOKIE_AGE = 31536000  # 365 days
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
SESSION_SAVE_EVERY_REQUEST = True

# Generous upload limits for high-resolution mobile photos and multi-page PDFs
DATA_UPLOAD_MAX_MEMORY_SIZE = 104857600   # 100 MB
FILE_UPLOAD_MAX_MEMORY_SIZE = 104857600   # 100 MB

# Security Headers & Environment-based SSL Protection
X_FRAME_OPTIONS = 'DENY'
SECURE_CONTENT_TYPE_NOSNIFF = True

# Environment-based Production SSL Controls (Enabled by default when DEBUG=False)
ENABLE_SSL = os.getenv('DJANGO_ENABLE_SSL', 'False' if DEBUG else 'True').lower() in ('true', '1', 't')
if ENABLE_SSL:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.getenv('SECURE_HSTS_SECONDS', 31536000))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
else:
    SECURE_SSL_REDIRECT = False
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SECURE = False

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Application Specific Constants
WHATSAPP_SANDBOX_MODE = os.getenv('WHATSAPP_SANDBOX_MODE', 'True').lower() in ('true', '1', 't')
MAX_FILE_SIZE_BYTES = int(os.getenv('MAX_FILE_SIZE_BYTES', 104857600))  # 100MB
MAX_SUBMISSION_SIZE_BYTES = int(os.getenv('MAX_SUBMISSION_SIZE_BYTES', 104857600))  # 100MB

# External Backup Destination (Cross-platform path outside application directory)
_default_backup_dir = (
    Path('E:\\Balaji_Backups\\InvoiceHub')
    if os.name == 'nt' and Path('E:\\').exists()
    else BASE_DIR.parent / 'backups'
)
BACKUP_STORAGE_DIR = Path(
    os.getenv('BACKUP_STORAGE_PATH') or os.getenv('BACKUP_DIR') or str(_default_backup_dir)
)
BACKUP_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
# Permanent retention policy: 3650 days (10+ years / Permanent Archive) for statutory GST & tax compliance
BACKUP_RETENTION_DAYS = int(os.getenv('BACKUP_RETENTION_DAYS', 3650))

# Gemini Vision AI for High-Accuracy Autonomous Invoice Extraction (Google AI Studio Free Tier)
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '').strip()
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-3.5-flash-lite').strip()
