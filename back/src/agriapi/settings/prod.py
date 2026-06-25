"""Production settings — hard-fails on missing secrets, secure cookies, SSL redirect."""

from __future__ import annotations

import os

from .base import *  # noqa: F401, F403
from .base import _csv_env


def _required(name: str) -> str:
    """Read an env var; raise loudly if missing or empty in prod."""
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(
            f"Required env var {name} is missing in production. "
            "Set it in back/.env (or the platform's secret store) before boot."
        )
    return v


# -----------------------------------------------------------------------------
# Core (loud on missing)
# -----------------------------------------------------------------------------
DEBUG = False
SECRET_KEY = _required("SECRET_KEY")
ALLOWED_HOSTS = [h.strip() for h in _required("ALLOWED_HOSTS").split(",") if h.strip()]

# -----------------------------------------------------------------------------
# Database — Postgres (managed e.g. Supabase, or a self-hosted container).
# loud on missing creds. sslmode is configurable: "require" for managed/TLS
# endpoints (default), "disable" for a local container without TLS.
# -----------------------------------------------------------------------------
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": _required("POSTGRES_DB"),
        "USER": _required("POSTGRES_USER"),
        "PASSWORD": _required("POSTGRES_PASSWORD"),
        "HOST": _required("POSTGRES_HOST"),
        "PORT": int(os.getenv("POSTGRES_PORT", 5432)),
        "OPTIONS": {"sslmode": os.getenv("POSTGRES_SSLMODE", "require")},
    }
}

# Let agri-core's DB-backed handlers reach the same Postgres.
export_agri_db_url(DATABASES)

# -----------------------------------------------------------------------------
# Security
# -----------------------------------------------------------------------------
SECURE_SSL_REDIRECT = os.getenv("SECURE_SSL_REDIRECT", "True").lower() in (
    "1",
    "true",
    "yes",
)
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "31536000"))  # 1 year
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

# -----------------------------------------------------------------------------
# CORS / CSRF (loud on missing — prod has a specific origin list)
# -----------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = _csv_env("CORS_ALLOWED_ORIGINS", "")
if not CORS_ALLOWED_ORIGINS:
    raise RuntimeError(
        "CORS_ALLOWED_ORIGINS must be set in production (comma-separated)."
    )

CSRF_TRUSTED_ORIGINS = _csv_env("CSRF_TRUSTED_ORIGINS", "")

# -----------------------------------------------------------------------------
# Email — Resend HTTP API by default
# -----------------------------------------------------------------------------
# DigitalOcean blocks outbound SMTP (25/465/587) on the droplet, so the stock
# smtp backend hangs and 504s. Resend's REST API runs over HTTPS/443 (open), so
# it is the default transport here. Set EMAIL_BACKEND explicitly to override
# (e.g. back to smtp if the SMTP block is ever lifted).
EMAIL_BACKEND = os.getenv("EMAIL_BACKEND", "agriapi.email_backends.ResendEmailBackend")
# RESEND_API_KEY is inherited from base settings (read from env there).

# SMTP settings are only consulted (and required) when the smtp backend is
# explicitly selected via EMAIL_BACKEND.
if EMAIL_BACKEND.endswith("smtp.EmailBackend"):
    EMAIL_HOST = _required("EMAIL_HOST")
    EMAIL_HOST_USER = _required("EMAIL_HOST_USER")
    EMAIL_HOST_PASSWORD = _required("EMAIL_HOST_PASSWORD")
    EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587") or 587)
    EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "True").lower() in ("1", "true", "yes")
    EMAIL_USE_SSL = os.getenv("EMAIL_USE_SSL", "False").lower() in ("1", "true", "yes")
elif EMAIL_BACKEND.endswith("ResendEmailBackend") and not RESEND_API_KEY:
    raise RuntimeError(
        "RESEND_API_KEY is required when EMAIL_BACKEND is the Resend backend. "
        "Set it in back/.env before boot."
    )

DEFAULT_FROM_EMAIL = os.getenv(
    "DEFAULT_FROM_EMAIL", "Agrilogy <noreply@agrogo-datafarm.com>"
)
