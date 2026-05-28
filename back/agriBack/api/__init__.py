"""agri-api HTTP surface — single django-ninja ``NinjaAPI`` mounted at
the URL root.

Per memory ``agri-api-fastapi-style``, new endpoints land here as
FastAPI-style routers (function handlers + pydantic schemas + DI).
Legacy DRF views co-exist as the fall-through under
``agriBack/urls.py`` (they keep matching paths the ninja routes don't
register).

We use a **single** ``NinjaAPI`` mounted at the root so each router can
carry its full URL path: the new endpoints live under ``/api/v2/...``,
legacy ingest webhooks stay at their original ``/api/v1/...`` paths,
and migrated read endpoints take over ``/api/...`` slots in place.
That keeps the frontend contract intact during the DRF → ninja
migration.

Layout::

    back/agriBack/api/
    ├── __init__.py     # NinjaAPI instance + router includes (this file)
    ├── auth.py         # HttpBearer JWT authenticator
    └── routers/
        └── sensors.py  # /api/v2/sensors/...

Plus per-app routers under ``back/apps/<x>/router.py``.
"""
from __future__ import annotations

from ninja import NinjaAPI

from agriBack.api.auth import JwtAuth
from agriBack.api.routers.sensors import router as sensors_router
from analytics.router_alerts import router as alerts_router
from analytics.router_manager_affirmation import (
    router as manager_affirmation_router,
)
from analytics.router_notifications import router as notifications_router
from analytics.router_reads import router as analytics_reads_router
from analytics.router_sensors import router as sensors_auto_router
from apps.bivocom.router import router as bivocom_router
from apps.lorawan.chirpstack.router import router as chirpstack_router
from apps.users.router import router as users_router

api = NinjaAPI(
    title="Agrilogy API",
    version="2.0.0",
    description=(
        "FastAPI-style surface for agri-api. New endpoints (django-ninja, "
        "pydantic schemas, JWT auth default) co-exist with the legacy DRF "
        "views; routes migrate in place per app."
    ),
    auth=JwtAuth(),
    docs_url="/api/docs",
)

# v2 new endpoints — JWT auth applies by default.
api.add_router("/api/v2/sensors", sensors_router, tags=["sensors"])

# /auth — signup/signin/admin-signup, modify-user, send-notification.
# Each route declares its own auth (mostly auth=None for public auth flows;
# admin ops apply JwtAuth + an inline IsAdminUser check).
api.add_router("/auth", users_router, tags=["auth"])

# /api — analytics reads (header, zones list, active-graph config).
# Each route is JWT-authed; admin-only endpoints apply an inline check.
api.add_router("/api", analytics_reads_router, tags=["analytics"])

# /api/alert(s) — alerts CRUD + the for-graph / sensor-keys / suggest helpers.
api.add_router("/api", alerts_router, tags=["alerts"])

# /api/notifications-and-alerts/, /api/zone-notification-outbound/
api.add_router("/api", notifications_router, tags=["notifications"])

# /api/manager-affirmations/, /api/manager-affirmations/<pk>/<action>/
api.add_router("/api", manager_affirmation_router, tags=["manager-affirmation"])

# /api/sensors/<slug>/ — 34 dynamically registered read endpoints
# (one GET + one PATCH per entry in SENSOR_MODELS).
api.add_router("/api", sensors_auto_router, tags=["sensors-data"])

# Legacy ingest webhooks migrated in place under their original paths.
# These webhook routes opt out of auth at the route level (gateway uses a
# shared-secret header today; TODO: enforce in a follow-up).
api.add_router("/api/v1/bivocom", bivocom_router, tags=["bivocom"])
api.add_router("/api/v1/lorawan/chirpstack", chirpstack_router, tags=["lorawan"])
