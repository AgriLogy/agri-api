"""fastapp ASGI application.

Served by uvicorn on :8001 (``docker-entrypoint.sh fast``) next to the
Django process on :8000. nginx (deploy/nginx/back.conf) strangles routes
over one path prefix at a time; until a prefix is cut over, this app only
answers its own new paths (/healthz, /api/fast/docs).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from fastapp.auth import AuthedUser, get_current_user
from fastapp.errors import register_exception_handlers
from fastapp.json import DjangoStyleJSONResponse, register_django_style_json
from fastapp.routers import (
    admin_analytics,
    admin_audit,
    admin_backfill,
    admin_billing,
    admin_db,
    admin_impersonation,
    admin_kc,
    admin_monitoring,
    admin_records,
    admin_sensor_data,
    admin_settings,
    alerts,
    assistant,
    auth_issuance,
    devices,
    feedback,
    ingest,
    irrigation,
    kc,
    manager_affirmations,
    notification_zones,
    notifications,
    selfreads,
    sensors,
    technicians,
    users,
    weather,
)
from fastapp.settings import get_settings

# django-cors-headers' corsheaders.defaults.default_headers, verbatim — the
# Django side runs CORS_ALLOW_HEADERS = list(default_headers), so the sidecar
# must accept exactly the same request headers.
_CORS_ALLOW_HEADERS = [
    "accept",
    "authorization",
    "content-type",
    "user-agent",
    "x-csrftoken",
    "x-requested-with",
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Dispose the shared agri-core SQLAlchemy engine on shutdown so pooled
    connections don't linger past the process (mirrors what the test suite's
    conftest does after rebinding AGRI_DB_URL)."""
    yield
    from agri.core.database.session import dispose_engine

    dispose_engine()


settings = get_settings()

app = FastAPI(
    title="Agrilogy API (fastapp)",
    version=settings.version,
    docs_url="/api/fast/docs",
    openapi_url="/api/fast/openapi.json",
    lifespan=lifespan,
    # Match django-ninja's JSON wire format (spaced separators, ascii) so a
    # cut-over route is byte-identical, not just parse-identical.
    default_response_class=DjangoStyleJSONResponse,
)

# Same policy as the Django app: explicit origin list + credentials allowed
# (django-cors-headers: CORS_ALLOWED_ORIGINS / CORS_ALLOW_CREDENTIALS=True).
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=_CORS_ALLOW_HEADERS,
)

register_exception_handlers(app)
# Django-style JSON for the framework's own error envelopes (HTTPException /
# validation), so 404/401/422 bodies match ninja byte-for-byte too.
register_django_style_json(app)

# --- Strangler cutover routers (mirror the nginx location blocks) ----------
# Each prefix here must have a matching `location /<prefix>/ → :8001` block in
# deploy/nginx/back.conf, or the route is unreachable in prod (Django still
# answers it). See the phase notes in that file.
app.include_router(weather.router)  # F2: /weather/*
app.include_router(feedback.router)  # F2c: /feedback
app.include_router(sensors.router)  # F2b: /sensors + /sensors/*
app.include_router(alerts.router)  # F3: /alerts + /alerts/*
app.include_router(kc.router)  # F3: /kc + /kc/*
app.include_router(manager_affirmations.router)  # F3: /manager-affirmations + /*
app.include_router(notifications.router)  # F3: /notifications + /notifications/*
app.include_router(
    notification_zones.router
)  # F3: /notification-zones + /notification-zones/*
app.include_router(selfreads.router)  # F5: /users/me + /zones self-reads
# F9: /auth issuance — signup / sessions / admin-sessions / logout + the legacy
# DRF token + token/refresh endpoints. Django keeps serving /auth until the
# nginx cutover; this is the login path, flipped LAST with careful live A/B.
app.include_router(auth_issuance.router)  # /auth/* (signup, sessions, token)
app.include_router(devices.router)  # F5b: /devices + /devices/*
app.include_router(technicians.router)  # F5b: /technicians + /technicians/*
app.include_router(irrigation.router)  # F5b: /irrigation + /irrigation/*
app.include_router(assistant.router)  # F7: /assistant/* (tools + chat + convos)
app.include_router(ingest.router)  # F9: /ingest/* device webhooks (auth=None)
# F6 business-admin (staff-only) — each has a matching nginx location block.
app.include_router(admin_billing.router)  # /admin/billing/*
app.include_router(admin_audit.router)  # /admin/audit
app.include_router(admin_settings.router)  # /admin/settings + /admin/settings/{key}
app.include_router(admin_kc.router)  # /admin/kc + /admin/kc/{id}
app.include_router(admin_monitoring.router)  # /admin/monitoring/*
app.include_router(admin_records.router)  # /admin/{notifications,conversations,...}
app.include_router(admin_db.router)  # /admin/db/* generic schema-driven CRUD
# F6-admin-rest: the remaining admin routers (analytics tree + sensor-data
# explorer + backfill + read-only impersonation). Only the /admin/* paths of
# admin_analytics are cut over in nginx; its /users/{username}/* paths share
# the /users prefix with the still-Django users-admin router.
app.include_router(admin_analytics.router)  # /admin/overview + /users/{u}/* admin
app.include_router(
    admin_sensor_data.router
)  # /admin/sensor-data + /admin/sensor-data/*
app.include_router(admin_backfill.router)  # /admin/users/{u}/zones/{z}/backfill*
app.include_router(admin_impersonation.router)  # /admin/impersonate/{username}
# F5c: users-admin console + the caller's on-demand notification email. Included
# AFTER selfreads so GET/PATCH /users/me stay with selfreads, and its /users/
# {username} (exact) + admin-op subpaths don't collide with admin_analytics'
# /users/{username}/{zones,alerts,activity,sensor-units}.
app.include_router(users.router)  # /users, /users/{username}, /users/me/notifications


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness probe — no DB, no auth. Also the smoke check the deploy
    pipeline (and nginx cutovers) can hit to prove the sidecar booted."""
    return {"status": "ok", "app": "fastapp", "version": settings.version}


@app.get("/fast/whoami", response_model=AuthedUser)
def whoami(user: AuthedUser = Depends(get_current_user)) -> AuthedUser:
    """Auth-parity probe (F1): proves a Django-minted simplejwt access token
    authenticates against the sidecar via the shared user table. Harmless
    new path — nothing on the Django side serves /fast/*."""
    return user
