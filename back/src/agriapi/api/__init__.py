"""agri-api HTTP surface — REST-aligned, single ``NinjaAPI`` mount.

URL scheme (REST-standard, plural nouns, hierarchical):

* ``/auth/*``                       authentication only (signup, sessions)
* ``/users/me``                     caller's profile
* ``/users/me/notifications``       POST: send-notification email
* ``/users/...``                    admin user resources (list, CRUD, activate)
* ``/zones/...``                    caller's zones + active-graph
* ``/alerts/...``                   caller's alerts + sub-collections
* ``/notifications/...``            caller's notification feed
* ``/manager-affirmations/...``     workflow rows + approve/reject
* ``/sensors``                      sensor-key catalog
* ``/sensors/<slug>``               per-sensor readings + PATCH
* ``/ingest/...``                   device webhooks (Bivocom, ChirpStack, weather)
* ``/admin/overview``               KPIs
* ``/admin/alerts/<pk>``            admin alerts override
"""

from __future__ import annotations

from ninja import NinjaAPI

from agriapi.api.auth import JwtAuth
from apps.irrigation.router_admin import router as analytics_admin_router
from apps.irrigation.router_kc import router as kc_router
from apps.irrigation.router_devices import router as devices_router
from apps.irrigation.router_irrigation_automation import (
    router as irrigation_automation_router,
)
from apps.alerts.router_alerts import router as alerts_router
from apps.irrigation.router_manager_affirmation import (
    router as manager_affirmation_router,
)
from apps.alerts.router_notifications import router as notifications_router
from apps.irrigation.router_reads import router as analytics_reads_router
from apps.sensors.router_sensors import router as sensors_router
from apps.sensors.router_weather_ingest import router as weather_ingest_router
from apps.bivocom.router import router as bivocom_router
from apps.lorawan.chirpstack.router import router as chirpstack_router
from apps.users.router import router as users_auth_router
from apps.users.router_admin import router as users_router
from apps.assistant.router import router as assistant_router
from apps.users.router_technicians import router as technicians_router
from apps.irrigation.router_billing import router as billing_router
from apps.irrigation.router_audit import router as audit_router
from apps.irrigation.router_settings import router as settings_router
from apps.irrigation.router_records import router as records_router
from apps.irrigation.router_monitoring import router as monitoring_router

api = NinjaAPI(
    title="Agrilogy API",
    version="3.0.0",
    description=(
        "FastAPI-style REST surface for agri-api. Single mount at the root; "
        "routes use plural nouns, path-param identity, action sub-resources."
    ),
    auth=JwtAuth(),
    docs_url="/api/docs",
)

# Authentication only — no JWT on these routes (they issue tokens).
api.add_router("/auth", users_auth_router, tags=["auth"])

# User resources (admin + self) — mounted at /users.
api.add_router("/users", users_router, tags=["users"])

# Self-scoped reads (/users/me, /zones, /zones/{id}/active-graph) +
# the empty stub at root for any future top-level reads.
api.add_router("", analytics_reads_router, tags=["self"])

# Alerts CRUD + sub-collections (/alerts, /alerts/for-graph, /alerts/suggest).
api.add_router("/alerts", alerts_router, tags=["alerts"])
api.add_router("/kc", kc_router, tags=["kc"])
api.add_router("/irrigation", irrigation_automation_router, tags=["irrigation"])

# Notification feed + outbound trigger.
api.add_router("/notifications", notifications_router, tags=["notifications"])

# Manager-affirmation workflow.
api.add_router(
    "/manager-affirmations",
    manager_affirmation_router,
    tags=["manager-affirmation"],
)

# Sensor-key catalog (GET /sensors) + per-sensor readings (GET, PATCH /sensors/<slug>).
api.add_router("/sensors", sensors_router, tags=["sensors"])

# Owner-facing technician management (scoped read-only logins).
api.add_router("/technicians", technicians_router, tags=["technicians"])

# Admin device/router registry (CRUD).
api.add_router("/devices", devices_router, tags=["devices"])

# Device-ingest webhooks. Each declares ``auth=None`` per-route because the
# gateway authenticates with a shared-secret header (TODO).
api.add_router("/ingest/bivocom", bivocom_router, tags=["ingest"])
api.add_router("/ingest/lorawan/chirpstack", chirpstack_router, tags=["ingest"])
api.add_router("/ingest", weather_ingest_router, tags=["ingest"])

# Admin tree (KPIs, per-user resources, admin alert override).
api.add_router("", analytics_admin_router, tags=["admin"])

# Business-admin: billing/subscriptions, audit log, system settings.
api.add_router("/admin/billing", billing_router, tags=["admin-billing"])
api.add_router("/admin/audit", audit_router, tags=["admin-audit"])
api.add_router("/admin/settings", settings_router, tags=["admin-settings"])

# Monitoring/observability: task runs + schedule, delivery log, sign-in events.
api.add_router("/admin/monitoring", monitoring_router, tags=["admin-monitoring"])

# Admin records: notifications, assistant conversations, proactive notices,
# and a global view over technician grants (across all owners).
api.add_router("/admin", records_router, tags=["admin-records"])

# AI assistant — tool catalog, per-tool invoke, and the orchestrated /chat.
api.add_router("/assistant", assistant_router, tags=["assistant"])
