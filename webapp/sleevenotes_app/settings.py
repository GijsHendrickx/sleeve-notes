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
CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])

# HTTPS-only cookies in any non-local environment.
SESSION_COOKIE_SECURE = ENVIRONMENT != "local"
CSRF_COOKIE_SECURE = ENVIRONMENT != "local"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https") if ENVIRONMENT != "local" else None

# ─── Applications ────────────────────────────────────────────────────────────
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.sites",
    "django.contrib.staticfiles",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "discogs_provider",
    "core",
    "users",
    "records",
    "print_runs",
    "audit",
    "django_q",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "middleware.staging_gate.StagingGateMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# Staging gate credentials (only required when ENVIRONMENT=staging).
STAGING_BASIC_AUTH_USER = env("STAGING_BASIC_AUTH_USER", default="")
STAGING_BASIC_AUTH_PASSWORD = env("STAGING_BASIC_AUTH_PASSWORD", default="")

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

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

SITE_ID = 1

# ─── allauth / Discogs OAuth1 ────────────────────────────────────────────────
LOGIN_REDIRECT_URL = "/"
ACCOUNT_LOGOUT_REDIRECT_URL = "/"
ACCOUNT_EMAIL_VERIFICATION = "none"
ACCOUNT_SIGNUP_FIELDS = ["username*"]  # Discogs doesn't return email; only username required
SOCIALACCOUNT_AUTO_SIGNUP = True
SOCIALACCOUNT_LOGIN_ON_GET = True
SOCIALACCOUNT_ADAPTER = "discogs_provider.adapter.DiscogsSocialAccountAdapter"
# Persist the Discogs OAuth1 access token + secret so background jobs can call
# the API on behalf of the user. Without this, allauth discards the token after
# the auth handshake and the sync command can't authenticate.
SOCIALACCOUNT_STORE_TOKENS = True
SOCIALACCOUNT_PROVIDERS = {
    "discogs": {
        "APP": {
            "client_id": env("DISCOGS_CONSUMER_KEY", default=""),
            "secret": env("DISCOGS_CONSUMER_SECRET", default=""),
        },
    },
}

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

# ─── Background jobs (Django-Q2) ─────────────────────────────────────────────
# Postgres-as-broker keeps us at one external service (the DB). Single Q
# cluster across the app; per-user job locks live in records.UserJobLock.
Q_CLUSTER = {
    "name": "sleeve_notes",
    "orm": "default",
    "workers": env.int("Q_CLUSTER_WORKERS", default=2),
    "timeout": env.int("Q_CLUSTER_TIMEOUT", default=900),   # 15 min hard kill
    "retry": env.int("Q_CLUSTER_RETRY", default=1200),      # must exceed timeout
    "max_attempts": 1,                                      # no auto-retry; surfaces errors loudly
    "save_limit": 250,
    "ack_failures": True,
    "catch_up": False,
    "bulk": 10,
    "label": "Sleeve Notes Q",
}

# ─── Error & performance monitoring (Sentry) ─────────────────────────────────
SENTRY_DSN = env("SENTRY_DSN", default="")
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=ENVIRONMENT,
        integrations=[DjangoIntegration()],
        send_default_pii=True,
        traces_sample_rate=1.0 if ENVIRONMENT in ("local", "staging") else 0.1,
    )
