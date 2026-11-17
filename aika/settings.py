import os
from pathlib import Path
from decouple import config, Csv


# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/6.1/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = config('SECRET_KEY')
# SECURITY WARNING: don't run with debug turned on in production!

DEBUG = config('DEBUG', default=False, cast=bool)

# Comma-separated list in .env, e.g. ALLOWED_HOSTS=api.example.com,localhost
ALLOWED_HOSTS = config(
    'ALLOWED_HOSTS',
    default='localhost,127.0.0.1,[::1]',
    cast=Csv(),
)

AUTH_USER_MODEL = 'accounts.CustomUser'



# Application definition

INSTALLED_APPS = [
    'daphne',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',
    
    'ninja',
    'corsheaders',
    'webpush',

    "accounts",
    "admins",
    "cart",
    "staff",
    "audit",
    "booking",
    "core",
    "customers",
    "rider",
    "marketplace",
    "merchant",
    "order",
    "wallet",
    "payment",
    "marketing",
    "webhook",
    "logistics",
    "publics",
    "messaging",
    "support",
    "aika_platform",
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'ninja.compatibility.files.fix_request_files_middleware',

    'accounts.middleware.PlatformSecurityMiddleware',
]

# django-silk records every request (including Authorization headers and bodies):
# it must never run outside local development.
if DEBUG:
    INSTALLED_APPS += ["silk"]
    MIDDLEWARE += ['silk.middleware.SilkyMiddleware']

# FRONTEND_URL = "http://172.20.10.4:8000"
FRONTEND_URL = "http://localhost:3000"

BACKEND_URL = "http://localhost:8000"

# BACKEND_URL = "http://172.20.10.4:8000"

CSRF_TRUSTED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://[::1]:3000",
    "http://172.20.10.4:8000",
    "http://localhost:8000",
    "http://127.0.0.1:8000"
]

CORS_ALLOW_CREDENTIALS = True

USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# List your specific frontend domains explicitly
CORS_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://172.20.10.4:8000",
    "http://[::1]:3000",
    "http://10.136.16.114:8000",
]

# Allow standard headers and methods
CORS_ALLOW_METHODS = [
    'DELETE',
    'GET',
    'OPTIONS',
    'PATCH',
    'POST',
    'PUT',
]

CORS_ALLOW_HEADERS = [
    'accept',
    'accept-encoding',
    'authorization',
    'content-type',
    'dnt',
    'origin',
    'user-agent',
    'x-csrftoken',
    'x-requested-with',
    'ngrok-skip-browser-warning',
    'x-chat-token',        # guest support-chat secret
    'x-captcha-token',     # Turnstile token sent on registration
    'bypass-tunnel-reminder', # Include this if using localtunnel headers
]


ROOT_URLCONF = 'aika.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'aika.wsgi.application'
ASGI_APPLICATION = 'aika.asgi.application'



CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.pubsub.RedisPubSubChannelLayer",
        "CONFIG": {
            "hosts": [("127.0.0.1", 6379)],
        },
    },
}


# Database
# https://docs.djangoproject.com/en/6.1/ref/settings/#databases

# DATABASES = {
#     'default': {
#         'ENGINE': 'django.db.backends.sqlite3',
#         'NAME': BASE_DIR / 'db.sqlite3',
#         'OPTIONS': {
#             'timeout': 20,  # Wait up to 20 seconds for locks to clear instead of failing instantly
#             'init_command': "PRAGMA journal_mode=WAL;",
#         }
#     }
# }


DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': config('DB_NAME', default='aika_db'),
        'USER': config('DB_USER', default='postgres'),
        'PASSWORD': config('DB_PASSWORD'),          # required: never commit it
        'HOST': config('DB_HOST', default='localhost'),
        'PORT': config('DB_PORT', default='5432'),
        'CONN_MAX_AGE': config('DB_CONN_MAX_AGE', default=60, cast=int),
    }
}

# Shared cache: required for the rate limiter to work across worker processes.
# Set REDIS_URL (e.g. redis://127.0.0.1:6379/1) in production.
_REDIS_URL = config('REDIS_URL', default='')
if _REDIS_URL:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.redis.RedisCache',
            'LOCATION': _REDIS_URL,
        }
    }
else:
    CACHES = {
        'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}
    }

# Password validation
# https://docs.djangoproject.com/en/6.1/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

AUTH_PASSWORD_RESET_URL = "http://localhost:3000/auth/password-reset"


# Internationalization
# https://docs.djangoproject.com/en/6.1/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'Africa/Lagos'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.1/howto/static-files/

STATIC_URL = 'static/'
STATICFILES_DIRS = [os.path.join(BASE_DIR, 'static')]
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'


# Email
# https://docs.djangoproject.com/en/6.1/topics/email/#topic-email-configuration

MAILERS = {
    'default': {
        # 'BACKEND': 'django.core.mail.backends.console.EmailBackend',
        'BACKEND': 'django.core.mail.backends.smtp.EmailBackend',
        "OPTIONS": {
            "host": config("EMAIL_HOST", default="mail.useaika.ng"),
            "port": config("EMAIL_PORT", default=465, cast=int),
            "use_tls": False,
            "use_ssl": True,
            "username": config("EMAIL_HOST_USER", default="mail.useaika.ng"),
            "password": config("EMAIL_HOST_PASSWORD"),   # required: never commit it
        },
    },
}

# Default sender address used across all outgoing platform emails
DEFAULT_FROM_EMAIL = 'Aika <welcome@useaika.ng>'
SERVER_EMAIL = ""

BOLT_MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB

PAYSTACK_SECRET_KEY=config("PAYSTACK_SECRET_KEY")
PAYSTACK_PUBLIC_KEY=config("PAYSTACK_PUBLIC_KEY")


FLW_TEST_CLIENT_ID=config("FLW_TEST_CLIENT_ID", default="")
FLW_TEST_CLIENT_SECRET=config("FLW_TEST_CLIENT_SECRET", default="")

# Captcha (Cloudflare Turnstile) and KYC provider. Leave empty to disable.
TURNSTILE_SECRET_KEY = config("TURNSTILE_SECRET_KEY", default="")
KYC_API_SECRET_KEY = config("KYC_API_SECRET_KEY", default="")

# Number of reverse proxies in front of Django that append to X-Forwarded-For.
# 0 = trust REMOTE_ADDR only (cannot be spoofed). Set to 1 behind nginx/ngrok/etc.
TRUSTED_PROXY_COUNT = config("TRUSTED_PROXY_COUNT", default=0, cast=int)

# Stale PENDING marketplace orders are released after this many minutes
# (see: python manage.py release_stale_orders).
PENDING_ORDER_TTL_MINUTES = config("PENDING_ORDER_TTL_MINUTES", default=120, cast=int)

# Self-delivered orders the customer never confirms are auto-completed (and the vendor paid)
# this many hours after the vendor marked them delivered.
SELF_DELIVERY_AUTO_CONFIRM_HOURS = config("SELF_DELIVERY_AUTO_CONFIRM_HOURS", default=48, cast=int)

# FLW_BASE_URL = "https://f4bexperience.flutterwave.com/"
FLW_BASE_URL = "https://developersandbox-api.flutterwave.com"



OPAY_SECRET_KEY = config("OPAY_SECRET_KEY")
OPAY_PUBLIC_KEY = config("OPAY_PUBLIC_KEY")
OPAY_MERCHANT_ID = config("OPAY_MERCHANT_ID")



VAPID_PRIVATE_KEY = config("VAPID_PRIVATE_KEY")
VAPID_PUBLIC_KEY = config("VAPID_PUBLIC_KEY")


GOOGLE_MAPS_API_KEY = ""


LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {name} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
        'file': {
            'level': 'INFO',
            'class': 'logging.FileHandler',
            'filename': os.path.join(BASE_DIR, 'debug.log'),
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['console', 'file'],
        'level': 'INFO',
    },
    'loggers': {
        'django': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
            'propagate': False,
        },
        'merchant.api': {
            'handlers': ['console', 'file'],
            'level': 'DEBUG' if DEBUG else 'INFO',
            'propagate': False,
        },
    },
}


WEBPUSH_SETTINGS = {
    "VAPID_PUBLIC_KEY": os.path.join(BASE_DIR, "public_key.pem"),
    "VAPID_PRIVATE_KEY": os.path.join(BASE_DIR, "private_key.pem"),
    "VAPID_ADMIN_EMAIL": config("VAPID_ADMIN_EMAIL", default="mailto:titusjesse675@gmail.com")
}


# =========================================================================
# TRANSPORT & COOKIE HARDENING (active when DEBUG is False)
# =========================================================================
SESSION_COOKIE_SECURE = config('SESSION_COOKIE_SECURE', default=not DEBUG, cast=bool)
CSRF_COOKIE_SECURE = config('CSRF_COOKIE_SECURE', default=not DEBUG, cast=bool)
SESSION_COOKIE_HTTPONLY = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'same-origin'
X_FRAME_OPTIONS = 'DENY'
SECURE_SSL_REDIRECT = config('SECURE_SSL_REDIRECT', default=False, cast=bool)
SECURE_HSTS_SECONDS = config('SECURE_HSTS_SECONDS', default=0 if DEBUG else 31536000, cast=int)
SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_HSTS_SECONDS > 0
SECURE_HSTS_PRELOAD = False
