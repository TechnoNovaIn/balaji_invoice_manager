"""
Django settings for Balaji InvoiceHub project.
Internal Operations & Invoice Management System for Balaji Transports.
"""

from pathlib import Path
import os
import sys
from dotenv import load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Add apps folder to Python path
sys.path.insert(0, str(BASE_DIR / 'apps'))

# Load environment variables from .env
load_dotenv(BASE_DIR / '.env')

SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', 'default-insecure-secret-key-change-in-env')

DEBUG = os.getenv('DJANGO_DEBUG', 'True').lower() in ('true', '1', 't')

ALLOWED_HOSTS = [host.strip() for host in os.getenv('DJANGO_ALLOWED_HOSTS', '127.0.0.1,localhost,testserver').split(',') if host.strip()]
if 'testserver' not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append('testserver')

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

# Security Headers
X_FRAME_OPTIONS = 'DENY'
SECURE_CONTENT_TYPE_NOSNIFF = True

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Application Specific Constants
WHATSAPP_SANDBOX_MODE = os.getenv('WHATSAPP_SANDBOX_MODE', 'True').lower() in ('true', '1', 't')
MAX_FILE_SIZE_BYTES = int(os.getenv('MAX_FILE_SIZE_BYTES', 104857600))  # 100MB
MAX_SUBMISSION_SIZE_BYTES = int(os.getenv('MAX_SUBMISSION_SIZE_BYTES', 104857600))  # 100MB

# External Backup Destination (Strictly outside application directory)
BACKUP_STORAGE_DIR = Path(os.getenv('BACKUP_STORAGE_PATH', 'E:\\Balaji_Backups\\InvoiceHub'))
BACKUP_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
# Permanent retention policy: 3650 days (10+ years / Permanent Archive) for statutory GST & tax compliance
BACKUP_RETENTION_DAYS = int(os.getenv('BACKUP_RETENTION_DAYS', 3650))
