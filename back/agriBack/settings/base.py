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
# back/agriBack/settings/base.py → back/
BASE_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(BASE_DIR / ".env")  # loads back/.env


def _csv_env(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default) or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


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
    # Local apps
    "analytics",
    "CustomUser",
    # Hardware-family ingest apps (vertical-slice)
    "apps.bivocom",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    # CORS before CommonMiddleware
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "agriBack.urls"

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

WSGI_APPLICATION = "agriBack.wsgi.application"

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
# Static / Media
# -----------------------------------------------------------------------------
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# -----------------------------------------------------------------------------
# DRF / JWT
# -----------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    # Maps agriBack.errors.AgriError subclasses → {error:{code,message}} JSON.
    # See agriBack.exception_handler for the mapping.
    "EXCEPTION_HANDLER": "agriBack.exception_handler.agri_exception_handler",
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

# Ensure Celery imports project-level tasks
CELERY_IMPORTS = ("agriBack.tasks",)

SCHEDULE_MODE = os.getenv("CELERY_SCHEDULE_MODE", "test").lower()

if SCHEDULE_MODE == "test":
    CELERY_BEAT_SCHEDULE = {
        "simulate_sensors_every_2min": {
            "task": "agriBack.tasks.simulate_sensor_ingest",
            "schedule": crontab(minute="*/2"),
        },
        "compute_et0_every_4min": {
            "task": "agriBack.tasks.compute_et0_vpd_hourly",
            "schedule": crontab(minute="*/4"),
        },
        "email_ping_every_4min": {
            "task": "agriBack.tasks.send_periodic_notifications",
            "schedule": crontab(minute="*/4"),
        },
    }
else:
    CELERY_BEAT_SCHEDULE = {
        "simulate_sensors_every_15min": {
            "task": "agriBack.tasks.simulate_sensor_ingest",
            "schedule": crontab(minute="*/15"),
        },
        "compute_et0_every_hour": {
            "task": "agriBack.tasks.compute_et0_vpd_hourly",
            "schedule": crontab(minute=0),
        },
        "email_ping_every_hour": {
            "task": "agriBack.tasks.send_periodic_notifications",
            "schedule": crontab(minute=0),
        },
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
    "water_flow":                 5 * 60,
    "water_level":                5 * 60,
    "water_pressure":             5 * 60,
    "water_ec":                   5 * 60,
    "ph_water":                   5 * 60,
    # Wind — volatile but worth knowing quickly
    "wind_speed":                15 * 60,
    "wind_direction":            15 * 60,
    # Weather — moderate cadence
    "temperature_weather":      30 * 60,
    "humidity_weather":         30 * 60,
    "pressure_weather":         30 * 60,
    "solar_radiation":          30 * 60,
    "precipitation_rate":       30 * 60,
    # ET0 — derived hourly anyway
    "et0_weather":              60 * 60,
    "et0_calculated":           60 * 60,
    # Soil moisture — slow-changing series
    "soil_moisture_low":        60 * 60,
    "soil_moisture_medium":     60 * 60,
    "soil_moisture_high":       60 * 60,
    "multi_depth_soil_moisture_sensor": 60 * 60,
    # Soil temperature
    "soil_temperature_low":   2 * 60 * 60,
    "soil_temperature_medium":2 * 60 * 60,
    "soil_temperature_high":  2 * 60 * 60,
    # Soil chemistry — drift on the hour scale
    "ec_soil_low":            2 * 60 * 60,
    "ec_soil_medium":         2 * 60 * 60,
    "ec_soil_high":           2 * 60 * 60,
    "ph_soil":                2 * 60 * 60,
    "soil_conductivity":      2 * 60 * 60,
    "soil_salinity":          2 * 60 * 60,
    "ec_salinity":            2 * 60 * 60,
    # NPK — very slow
    "npk":                    4 * 60 * 60,
    # Leaf
    "leaf_moisture":             30 * 60,
    "leaf_temperature":          30 * 60,
    # Fruit — multi-hour growth signals
    "fruit_size":             6 * 60 * 60,
    "large_fruit_diameter":   6 * 60 * 60,
    # Energy
    "electricity_consumption":   30 * 60,
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
