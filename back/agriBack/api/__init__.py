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
from apps.lorawan.chirpstack.router import router as chirpstack_router

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

# Legacy ingest webhooks migrated in place under their original paths.
# These webhook routes opt out of auth at the route level (gateway uses a
# shared-secret header today; TODO: enforce in a follow-up).
api.add_router("/api/v1/lorawan/chirpstack", chirpstack_router, tags=["lorawan"])
