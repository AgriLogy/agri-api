"""agri-api v2 surface — django-ninja routers.

Per memory ``agri-api-fastapi-style``, new endpoints land here as
FastAPI-style routers (function handlers + pydantic schemas + DI).
Legacy DRF views under ``/api/...`` and ``/auth/...`` keep running
until each endpoint is migrated.

Layout::

    back/agriBack/api/
    ├── __init__.py     # NinjaAPI instance + router includes (this file)
    ├── auth.py         # HttpBearer JWT authenticator
    └── routers/
        ├── __init__.py
        └── sensors.py  # first router (sensor-key registry)

Mounted at ``/api/v2/`` in ``agriBack.urls``.
"""
from __future__ import annotations

from ninja import NinjaAPI

from agriBack.api.auth import JwtAuth
from agriBack.api.routers.sensors import router as sensors_router

api = NinjaAPI(
    title="Agrilogy API v2",
    version="2.0.0",
    description=(
        "FastAPI-style surface for agri-api. New endpoints land here; "
        "legacy DRF routes remain under /api and /auth."
    ),
    auth=JwtAuth(),
    docs_url="/docs",
)

api.add_router("/sensors", sensors_router, tags=["sensors"])
