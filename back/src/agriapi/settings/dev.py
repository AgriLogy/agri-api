"""Development settings — local docker stack, Supabase dev Postgres, console email."""

from __future__ import annotations

import os

from .base import *  # noqa: F401, F403
from .base import BASE_DIR, _csv_env

# -----------------------------------------------------------------------------
# Core
# -----------------------------------------------------------------------------
DEBUG = os.getenv("DEBUG", "True").lower() in ("1", "true", "yes")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-not-secret-please-override")

ALLOWED_HOSTS = _csv_env(
    "ALLOWED_HOSTS",
    "localhost,127.0.0.1,0.0.0.0,agri-api-web,157.245.43.196,agrogo-datafarm.com,www.agrogo-datafarm.com",
)

# -----------------------------------------------------------------------------
# Database
# -----------------------------------------------------------------------------
# Dev defaults to Postgres (Supabase pooler via back/.env). Set USE_POSTGRES=False
# to fall back to sqlite for one-off local-only runs without Supabase creds.
USE_POSTGRES = os.getenv("USE_POSTGRES", "True").lower() in ("1", "true", "yes")

if USE_POSTGRES:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("POSTGRES_DB", "postgres"),
            "USER": os.getenv("POSTGRES_USER", "postgres"),
            "PASSWORD": os.getenv("POSTGRES_PASSWORD", ""),
            "HOST": os.getenv("POSTGRES_HOST", "localhost"),
            "PORT": int(os.getenv("POSTGRES_PORT", 5432)),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

# Let agri-core's DB-backed handlers reach the same Postgres (no-op on sqlite).
export_agri_db_url(DATABASES)

# -----------------------------------------------------------------------------
# CORS / CSRF
# -----------------------------------------------------------------------------
# Local dev ports across the app family: farmer web :3000, admin console :3001,
# identity SSO gateway :3002. Admin's browser axios calls hit the API directly
# so :3001 genuinely needs a CORS entry; the gateway talks to the API
# server-side (route handlers / server components) so :3002 is only defensive,
# kept here so any future browser-side call from it is allowed too.
CORS_ALLOWED_ORIGINS = _csv_env(
    "CORS_ALLOWED_ORIGINS",
    (
        "http://localhost:3000,"
        "http://127.0.0.1:3000,"
        "http://localhost:3001,"
        "http://127.0.0.1:3001,"
        "http://localhost:3002,"
        "http://127.0.0.1:3002,"
        "http://157.245.43.196:3000,"
        "https://157.245.43.196:3000,"
        "https://www.agrogo-datafarm.com"
    ),
)

CSRF_TRUSTED_ORIGINS = _csv_env(
    "CSRF_TRUSTED_ORIGINS",
    (
        "http://localhost:3000,"
        "http://127.0.0.1:3000,"
        "http://localhost:3001,"
        "http://127.0.0.1:3001,"
        "http://localhost:3002,"
        "http://127.0.0.1:3002,"
        "http://157.245.43.196,"
        "http://157.245.43.196:3000,"
        "https://157.245.43.196,"
        "https://157.245.43.196:3000,"
        "https://www.agrogo-datafarm.com"
    ),
)

# -----------------------------------------------------------------------------
# Email — console by default; override to Mailpit/SMTP via env if needed
# -----------------------------------------------------------------------------
_default_backend = (
    "django.core.mail.backends.console.EmailBackend"
    if DEBUG
    else "django.core.mail.backends.smtp.EmailBackend"
)
EMAIL_BACKEND = os.getenv("EMAIL_BACKEND", _default_backend)
EMAIL_HOST = os.getenv("EMAIL_HOST", "")
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587") or 587)
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "True").lower() in ("1", "true", "yes")
EMAIL_USE_SSL = os.getenv("EMAIL_USE_SSL", "False").lower() in ("1", "true", "yes")
DEFAULT_FROM_EMAIL = os.getenv(
    "DEFAULT_FROM_EMAIL", "Agrilogy <noreply@agrilogy.local>"
)
