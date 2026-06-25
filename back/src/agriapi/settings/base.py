"""Common Django settings shared by dev/prod/test.

Things that LIVE here:
  - INSTALLED_APPS, MIDDLEWARE, ROOT_URLCONF, TEMPLATES, WSGI_APPLICATION
  - AUTH (password validators, AUTH_USER_MODEL)
  - I18N / TZ
  - Static / Media URL conventions
  - DRF + SIMPLE_JWT
  - Celery base + CELERY_BEAT_SCHEDULE (both schedule modes)
  - Alert throttling table (domain config, env-invariant)
  - LOGGING skeleton

Things that LIVE in dev/prod/test instead:
  - DEBUG, SECRET_KEY, ALLOWED_HOSTS
  - DATABASES
  - CORS / CSRF origin lists
  - EMAIL_BACKEND + EMAIL_*
  - Production-only security flags (SECURE_SSL_REDIRECT, etc.)
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

from celery.schedules import crontab
from corsheaders.defaults import default_headers
from dotenv import load_dotenv

# -----------------------------------------------------------------------------
# Paths & .env
# -----------------------------------------------------------------------------
# back/agriapi/settings/base.py → back/
BASE_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(BASE_DIR / ".env")  # loads back/.env


def _csv_env(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default) or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


def export_agri_db_url(databases: dict) -> None:
    """Point ``agri.core.database`` (AGRI_DB_URL) at the same Postgres Django
    uses, so the DB-backed agri-core handlers connect without separate config.

    Called from dev/prod after ``DATABASES`` is built. No-op when AGRI_DB_URL
    is already set (CI/tests bind it explicitly) or the DB is not Postgres.
    """
    if os.getenv("AGRI_DB_URL"):
        return
    cfg = databases.get("default", {})
    if cfg.get("ENGINE", "").endswith("postgresql"):
        os.environ["AGRI_DB_URL"] = (
            "postgresql+psycopg://"
            f"{cfg['USER']}:{cfg['PASSWORD']}@{cfg['HOST']}:{cfg['PORT']}/{cfg['NAME']}"
        )


# -----------------------------------------------------------------------------
# Apps
# -----------------------------------------------------------------------------
INSTALLED_APPS = [
    "corsheaders",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party
    "drf_yasg",
    "rest_framework",
    "django_extensions",
    "django_celery_beat",
    # Local apps. "analytics" stays installed as the app_label anchor: every
    # extracted model keeps Meta.app_label = "analytics", so this app owns the
    # analytics_* tables + migration history while its models.py is now just a
    # compatibility re-export surface.
    "analytics",
    "apps.users.apps.UsersConfig",
    # Hardware-family ingest apps (vertical-slice)
    "apps.bivocom",
    "apps.lorawan.chirpstack",
    # Domain apps extracted from the analytics god-app (models tagged
    # app_label="analytics"; AppConfig labels stay distinct).
    "apps.sensors.apps.SensorsConfig",
    "apps.irrigation.apps.IrrigationConfig",
    "apps.alerts.apps.AlertsConfig",
    "apps.assistant.apps.AssistantConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise serves static files directly (so gunicorn handles admin/DRF
    # assets under DEBUG=False without an nginx static block).
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    # CORS before CommonMiddleware
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Blocks mutations made with a read-only "view-as" impersonation token.
    "agriapi.middleware.ImpersonationReadOnlyMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "agriapi.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "agriapi.wsgi.application"

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# -----------------------------------------------------------------------------
# CORS / CSRF headers (origins live in per-env files)
# -----------------------------------------------------------------------------
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_HEADERS = list(default_headers)

# -----------------------------------------------------------------------------
# Auth
# -----------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
AUTH_USER_MODEL = "CustomUser.CustomUser"

# -----------------------------------------------------------------------------
# I18N / TZ
# -----------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = os.getenv("DJANGO_TIME_ZONE", "UTC")  # e.g. "Africa/Casablanca"
USE_I18N = True
USE_TZ = True

# -----------------------------------------------------------------------------
# Twilio WhatsApp (per-alert WhatsApp delivery). Set on the droplet; empty =
# WhatsApp sends are a logged no-op (see agriapi.whatsapp). Sandbox FROM is
# usually whatsapp:+14155238886.
# -----------------------------------------------------------------------------
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "")

# -----------------------------------------------------------------------------
# Static / Media
# -----------------------------------------------------------------------------
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# -----------------------------------------------------------------------------
# DRF / JWT
# -----------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        # Honours CustomUser.sessions_revoked_at (admin force-logout / disable).
        "agriapi.api.auth.RevocationAwareJWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    # Maps agriapi.errors.AgriError subclasses → {error:{code,message}} JSON.
    # See agriapi.exception_handler for the mapping.
    "EXCEPTION_HANDLER": "agriapi.exception_handler.agri_exception_handler",
}
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(days=5),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=10),
    "ROTATE_REFRESH_TOKENS": False,
    "BLACKLIST_AFTER_ROTATION": False,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "AUTH_HEADER_NAME": "HTTP_AUTHORIZATION",
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
    "AUTH_TOKEN_CLASSES": ("rest_framework_simplejwt.tokens.AccessToken",),
    "TOKEN_TYPE_CLAIM": "token_type",
    "JTI_CLAIM": "jti",
}

# -----------------------------------------------------------------------------
# Celery
# -----------------------------------------------------------------------------
CELERY_ENABLE_UTC = True
CELERY_TIMEZONE = os.getenv("DJANGO_TIME_ZONE", "UTC")
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", CELERY_BROKER_URL)

# Run tasks inline (no broker) when CELERY_TASK_ALWAYS_EAGER is truthy. Off by
# default so dev/prod keep dispatching to the real worker; CI sets it so the
# pytest suite — which runs against Postgres in dev settings, not the sqlite
# `test` settings — can exercise `.delay()` code paths without a redis service.
CELERY_TASK_ALWAYS_EAGER = os.getenv("CELERY_TASK_ALWAYS_EAGER", "False").lower() in (
    "1",
    "true",
    "yes",
)
CELERY_TASK_EAGER_PROPAGATES = CELERY_TASK_ALWAYS_EAGER

# Ensure Celery imports project-level tasks
CELERY_IMPORTS = ("agriapi.tasks",)

# Queue isolation. This worker shares a redis broker with the legacy agriback
# monolith stack (the demo simulator), and both used to consume the default
# `celery` queue — so each worker grabbed ~half the other's tasks and rejected
# them as "unregistered", and our own scheduled tasks were silently dropped
# whenever they landed on the legacy worker. Route everything we own onto a
# dedicated `agriapi` queue (the worker is started with `-Q agriapi`); unrouted
# legacy `agriBack.*` tasks keep flowing to the default queue for their own
# worker, so the demo simulator is untouched.
CELERY_TASK_ROUTES = {"agriapi.*": {"queue": "agriapi"}}

# Cap how long a single SMTP send may block. Without this Django uses the
# socket default (can hang ~60s on an unreachable mail host, 504-ing requests
# and stalling Celery workers). Applies to every environment's SMTP backend.
EMAIL_TIMEOUT = int(os.getenv("EMAIL_TIMEOUT", "10") or 10)

# Resend HTTP-API key, read by agriapi.email_backends.ResendEmailBackend.
# Defined here (not just prod) so every settings module exposes it — the
# deployed container can run under dev settings depending on DJANGO_ENV.
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")

# ── AI assistant (LLM orchestrator) ──────────────────────────────────────────
# Provider-agnostic: any OpenAI-compatible chat-completions API with tool
# calling works (Groq, OpenRouter, Together, OpenAI, …). When AI_API_KEY is set
# the assistant uses the LLM orchestrator; otherwise it falls back to the
# deterministic rule-based one. Defaults point at Groq's free tier.
AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_API_BASE_URL = os.getenv("AI_API_BASE_URL", "https://api.groq.com/openai/v1")
AI_MODEL = os.getenv("AI_MODEL", "llama-3.3-70b-versatile")
AI_TIMEOUT = int(os.getenv("AI_TIMEOUT", "20") or 20)

SCHEDULE_MODE = os.getenv("CELERY_SCHEDULE_MODE", "test").lower()

# The sensor SIMULATOR fabricates random readings — a local-dev aid for working
# without hardware. It is OFF by default and must be explicitly opted into
# (ENABLE_SENSOR_SIMULATOR=True), so the deployed server NEVER fabricates data:
# graphs / ET0 / notifications run on the REAL ingested readings from the
# Bivocom / LoRaWAN endpoints. (It previously ran every 2-15 min in every
# environment, polluting the sensor tables with dummy data.)
ENABLE_SENSOR_SIMULATOR = os.getenv("ENABLE_SENSOR_SIMULATOR", "False").lower() in (
    "1",
    "true",
    "yes",
)

# SCHEDULE_MODE only controls cadence (fast in dev, hourly in prod).
if SCHEDULE_MODE == "test":
    _et0_schedule = crontab(minute="*/4")
    _email_schedule = crontab(minute="*/4")
    _sim_schedule = crontab(minute="*/2")
    _device_health_schedule = crontab(minute="*/10")
    _proactive_schedule = crontab(minute="*/10")
    _irrigation_run_schedule = crontab(minute="*/2")
else:
    _et0_schedule = crontab(minute=0)
    # Every 5 min so per-user notify_every cadences down to 10 min are
    # honoured (should_notify gates each user; this is just the check tick).
    _email_schedule = crontab(minute="*/5")
    _sim_schedule = crontab(minute="*/15")
    # Device health is slow-moving (offline > hours); an hourly scan is plenty.
    _device_health_schedule = crontab(minute=15)
    # Proactive irrigation nudges: a couple of times a day is plenty (deduped
    # per user with a 24h cooldown anyway).
    _proactive_schedule = crontab(minute=30, hour="*/8")
    # Scheduled irrigation programs: check every 5 min so a program's start_time
    # window is caught promptly.
    _irrigation_run_schedule = crontab(minute="*/5")

# SAFETY: physical valve/pump actuation is OFF by default. While False, output
# commands are recorded as "simulated" and never reach hardware. Turn on only
# after a real downlink backend is wired + hardware-tested (see
# apps/irrigation/output_dispatch.py).
IRRIGATION_DISPATCH_ENABLED = os.environ.get(
    "IRRIGATION_DISPATCH_ENABLED", "false"
).lower() in ("1", "true", "yes")

CELERY_BEAT_SCHEDULE = {
    "compute_et0": {
        "task": "agriapi.tasks.compute_et0_vpd_hourly",
        "schedule": _et0_schedule,
    },
    "email_ping": {
        "task": "agriapi.tasks.send_periodic_notifications",
        "schedule": _email_schedule,
    },
    # NOTE: prod beat runs the DatabaseScheduler, so this entry is a no-op there
    # until a matching django_celery_beat PeriodicTask row exists (same as
    # email_ping). It IS used under the default scheduler + in CI.
    "device_health_scan": {
        "task": "agriapi.tasks.scan_device_health",
        "schedule": _device_health_schedule,
    },
    # Same DatabaseScheduler caveat as above: needs a PeriodicTask row on prod.
    "proactive_scan": {
        "task": "agriapi.tasks.scan_proactive_insights",
        "schedule": _proactive_schedule,
    },
    # Same DatabaseScheduler caveat: needs a PeriodicTask row on prod. Fires due
    # irrigation programs (simulated dispatch unless IRRIGATION_DISPATCH_ENABLED).
    "irrigation_run": {
        "task": "agriapi.tasks.run_due_irrigation_programs",
        "schedule": _irrigation_run_schedule,
    },
}
if ENABLE_SENSOR_SIMULATOR:
    CELERY_BEAT_SCHEDULE["simulate_sensors"] = {
        "task": "agriapi.tasks.simulate_sensor_ingest",
        "schedule": _sim_schedule,
    }

# -----------------------------------------------------------------------------
# Alert email throttling (domain config — env-invariant)
# -----------------------------------------------------------------------------
# Per-sensor_key grace period (in seconds) between consecutive alert emails
# for the same alert row. Tuned per sensor family — water needs minute-scale
# reactions, fruit-size readings drift over hours, soil chemistry is slow.
# Any sensor_key not listed here falls back to DEFAULT_ALERT_GRACE_PERIOD.
DEFAULT_ALERT_GRACE_PERIOD = 1800  # 30 minutes

ALERT_GRACE_PERIODS: dict[str, int] = {
    # Water — high-stakes, fast-changing
    "water_flow": 5 * 60,
    "water_level": 5 * 60,
    "water_pressure": 5 * 60,
    "water_ec": 5 * 60,
    "ph_water": 5 * 60,
    # Wind — volatile but worth knowing quickly
    "wind_speed": 15 * 60,
    "wind_direction": 15 * 60,
    # Weather — moderate cadence
    "temperature_weather": 30 * 60,
    "humidity_weather": 30 * 60,
    "pressure_weather": 30 * 60,
    "solar_radiation": 30 * 60,
    "precipitation_rate": 30 * 60,
    # ET0 — derived hourly anyway
    "et0_weather": 60 * 60,
    "et0_calculated": 60 * 60,
    # Soil moisture — slow-changing series
    "soil_moisture_low": 60 * 60,
    "soil_moisture_medium": 60 * 60,
    "soil_moisture_high": 60 * 60,
    "multi_depth_soil_moisture_sensor": 60 * 60,
    # Soil temperature
    "soil_temperature_low": 2 * 60 * 60,
    "soil_temperature_medium": 2 * 60 * 60,
    "soil_temperature_high": 2 * 60 * 60,
    # Soil chemistry — drift on the hour scale
    "ec_soil_low": 2 * 60 * 60,
    "ec_soil_medium": 2 * 60 * 60,
    "ec_soil_high": 2 * 60 * 60,
    "ph_soil": 2 * 60 * 60,
    "soil_conductivity": 2 * 60 * 60,
    "soil_salinity": 2 * 60 * 60,
    "ec_salinity": 2 * 60 * 60,
    # NPK — very slow
    "npk": 4 * 60 * 60,
    # Leaf
    "leaf_moisture": 30 * 60,
    "leaf_temperature": 30 * 60,
    # Fruit — multi-hour growth signals
    "fruit_size": 6 * 60 * 60,
    "large_fruit_diameter": 6 * 60 * 60,
    # Energy
    "electricity_consumption": 30 * 60,
}

# -----------------------------------------------------------------------------
# Logging skeleton (handlers/loggers shared; per-env may tweak levels)
# -----------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {"level": "INFO", "class": "logging.StreamHandler"},
    },
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": True},
        "sensors": {"handlers": ["console"], "level": "INFO", "propagate": True},
    },
}
