"""Test settings — in-memory sqlite, locmem email, eager Celery.

Used by `pytest` and `python manage.py test`.
"""

from __future__ import annotations

from .base import *  # noqa: F401, F403

# -----------------------------------------------------------------------------
# Core
# -----------------------------------------------------------------------------
DEBUG = False
SECRET_KEY = "test-not-secret"
ALLOWED_HOSTS = ["*"]

# -----------------------------------------------------------------------------
# Database — in-memory sqlite (fast, isolated)
# -----------------------------------------------------------------------------
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# -----------------------------------------------------------------------------
# Email — locmem so tests can assert via django.core.mail.outbox
# -----------------------------------------------------------------------------
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
DEFAULT_FROM_EMAIL = "test@agrilogy.local"

# -----------------------------------------------------------------------------
# Celery — run tasks inline
# -----------------------------------------------------------------------------
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# -----------------------------------------------------------------------------
# CORS / CSRF — permissive (tests don't care about origins)
# -----------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS: list[str] = []
CORS_ALLOW_ALL_ORIGINS = True
CSRF_TRUSTED_ORIGINS: list[str] = []
