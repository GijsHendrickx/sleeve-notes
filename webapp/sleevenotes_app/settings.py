"""Django settings for sleevenotes_app.

All environment-driven config goes through django-environ. See .env.example
at the repo root for the variables this app reads.
"""
from pathlib import Path

import environ

# BASE_DIR is the webapp/ directory (one level above sleevenotes_app/).
BASE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BASE_DIR.parent

env = environ.Env()
environ.Env.read_env(REPO_ROOT / ".env")

# ─── Identity / branding ─────────────────────────────────────────────────────
APP_NAME = env("APP_NAME", default="Sleeve Notes")
ENVIRONMENT = env("ENVIRONMENT", default="local")  # local | staging | production

# ─── Core security ───────────────────────────────────────────────────────────
SECRET_KEY = env("DJANGO_SECRET_KEY")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

# ─── Applications ────────────────────────────────────────────────────────────
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "users",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "sleevenotes_app.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "sleevenotes_app.context_processors.app_branding",
            ],
        },
    },
]

WSGI_APPLICATION = "sleevenotes_app.wsgi.application"

# ─── Database ────────────────────────────────────────────────────────────────
# DATABASE_URL points at Postgres locally (via docker-compose) and on Render.
DATABASES = {"default": env.db("DATABASE_URL")}
CONN_MAX_AGE = env.int("DJANGO_CONN_MAX_AGE", default=60)

# ─── Authentication ──────────────────────────────────────────────────────────
# Custom user model from day 1: retrofitting AUTH_USER_MODEL onto an existing
# database is famously painful in Django.
AUTH_USER_MODEL = "users.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ─── Internationalization ────────────────────────────────────────────────────
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# ─── Static files ────────────────────────────────────────────────────────────
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# ─── Email (placeholder; provider wiring in fase 1C) ─────────────────────────
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="noreply@example.com")

# ─── Default primary-key type ────────────────────────────────────────────────
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
