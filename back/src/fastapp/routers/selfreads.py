"""fastapp self-scoped reads — /users/me + /zones (strangler group F5-reads).

Byte-parity port of two django-ninja routers:

  * ``apps/users/router_admin.py``      — GET/PATCH ``/users/me``
  * ``apps/irrigation/router_reads.py`` — GET ``/zones`` + GET
    ``/zones/{zone_id}/active-graph``

Read-only + owner-scoped (the PATCH is a self-write of the caller's own
language). Data access is SQLAlchemy via agri-core's session (no Django ORM):
the user row, zones, and ActiveGraph come from ``agri.db``.

Parity contract (matched against the Django originals):

  * GET /users/me          → ``{"username", "preferred_language", "notify_every"}``
  * PATCH /users/me        → same shape; invalid language →
    ``{"preferred_language": "Must be 'fr' or 'ar'."}`` @ 400 (a bare field
    map, NOT the ``{"detail": ...}`` envelope).
  * GET /zones             → ``[{"id", "name"}, ...]``
  * GET /zones/{id}/active-graph → ``model_to_dict(ActiveGraph)`` minus ``id``
    (keys ``user``/``zone`` carry the FK ids, then every ``*_status`` boolean
    in the Django model's field-declaration order), 404
    ``{"detail": "ActiveGraph not found."}`` on miss/foreign zone.

Technician (scoped read-only) callers are resolved through the same
``resolve_read_scope`` logic as the Django side — ported here so a cutover
keeps parity for them too. The technician grant tables are unmanaged in Django
(``analytics_techniciangrant`` / ``analytics_technicianzonegrant``) and have no
agri.db ORM model, so they're read with parameterised raw SQL.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from agri.core.database import session_scope
from agri.db.analytics import AnalyticsActivegraph, AnalyticsZone
from agri.db.users import CustomUserCustomuser
from fastapp.auth import AuthedUser, get_current_user
from fastapp.json import DjangoStyleJSONResponse

router = APIRouter(tags=["self"])

# CustomUser.LANGUAGE_CHOICES keys, verbatim (apps/users/models.py).
_LANGUAGE_CHOICES = {"fr", "ar"}

# ActiveGraph status fields in the Django model's field-declaration order — the
# order ``model_to_dict`` emits them (``apps/irrigation/models.py``). Byte
# parity depends on this exact sequence; SQLAlchemy's column order differs.
_ACTIVE_GRAPH_STATUS_KEYS = (
    "soil_irrigation_status",
    "soil_ph_status",
    "soil_conductivity_status",
    "soil_moisture_status",
    "soil_temperature_status",
    "et0_status",
    "wind_speed_status",
    "wind_direction_status",
    "solar_radiation_status",
    "temperature_humidity_weather_status",
    "precipitation_humidity_rate_status",
    "pluviometry_status",
    "data_table_status",
    "wind_radar_status",
    "cumulative_precipitation_status",
    "precipitation_rate_status",
    "weather_temperature_humidity_status",
    "water_flow_status",
    "water_pressure_status",
    "water_ph_status",
    "water_ec_status",
    "water_level_status",
    "leaf_sensor_status",
    "fruit_size_status",
    "large_fruit_diameter_status",
    "npk_status",
    "electricity_consumption_status",
)


# ---------------------------------------------------------------------------
# Read-scope resolution (port of agriapi.api.scope.resolve_read_scope)
# ---------------------------------------------------------------------------


@dataclass
class _ReadScope:
    owner_id: int
    # None  -> all of the owner's zones (regular user)
    # set() -> a specific subset (technician); empty set = nothing visible
    zone_ids: set[int] | None
    allowed_graphs_by_zone: dict[int, set[str]] = field(default_factory=dict)
    is_read_only: bool = False

    def zone_allowed(self, zone_id: int | None) -> bool:
        if self.zone_ids is None:
            return True
        if zone_id is None:
            return False
        return zone_id in self.zone_ids

    def graph_allowed(self, zone_id: int | None, status_key: str) -> bool:
        if self.zone_ids is None:
            return True
        if zone_id is None:
            return False
        allowed = self.allowed_graphs_by_zone.get(zone_id)
        return bool(allowed) and status_key in allowed


def _resolve_read_scope(session: Session, user: CustomUserCustomuser) -> _ReadScope:
    """Resolve the caller's data scope. Mirrors ``resolve_read_scope``: regular
    users get an unrestricted scope over their own id; technicians get the
    owner's rows narrowed to the granted zones + per-zone graph whitelist."""
    if not user.is_technician:
        return _ReadScope(owner_id=user.id, zone_ids=None, is_read_only=False)

    # Technician: latest active grant (order by -id), then its per-zone scope.
    grant = session.execute(
        text(
            "SELECT id, owner_id FROM analytics_techniciangrant "
            "WHERE technician_id = :tid AND is_active = true "
            "ORDER BY id DESC LIMIT 1"
        ),
        {"tid": user.id},
    ).first()
    if grant is None:
        return _ReadScope(owner_id=user.id, zone_ids=set(), is_read_only=True)

    zone_ids: set[int] = set()
    allowed: dict[int, set[str]] = {}
    rows = session.execute(
        text(
            "SELECT zone_id, allowed_graphs FROM analytics_technicianzonegrant "
            "WHERE grant_id = :gid"
        ),
        {"gid": grant.id},
    )
    for zg in rows:
        zone_ids.add(zg.zone_id)
        allowed[zg.zone_id] = set(zg.allowed_graphs or [])

    return _ReadScope(
        owner_id=grant.owner_id,
        zone_ids=zone_ids,
        allowed_graphs_by_zone=allowed,
        is_read_only=True,
    )


# ---------------------------------------------------------------------------
# /users/me
# ---------------------------------------------------------------------------


class MePreferencesIn(BaseModel):
    preferred_language: str | None = None


@router.get("/users/me", summary="Caller's profile (identity)")
def get_me(user: AuthedUser = Depends(get_current_user)):
    with session_scope() as session:
        row = session.get(CustomUserCustomuser, user.id)
        return {
            "username": row.username,
            "preferred_language": row.preferred_language,
            "notify_every": row.notify_every,
        }


@router.patch("/users/me", summary="Caller updates their own preferences (language)")
def patch_me(
    payload: MePreferencesIn,
    user: AuthedUser = Depends(get_current_user),
):
    lang = payload.preferred_language
    # Validate BEFORE touching the DB — invalid language returns a bare field
    # map (NOT the {"detail": ...} envelope), matching ninja's
    # Response({"preferred_language": ...}, status=400).
    if lang is not None and lang not in _LANGUAGE_CHOICES:
        return DjangoStyleJSONResponse(
            {"preferred_language": "Must be 'fr' or 'ar'."}, status_code=400
        )
    with session_scope(commit=True) as session:
        row = session.get(CustomUserCustomuser, user.id)
        if lang is not None:
            row.preferred_language = lang
        session.flush()
        return {
            "username": row.username,
            "preferred_language": row.preferred_language,
            "notify_every": row.notify_every,
        }


# ---------------------------------------------------------------------------
# /zones
# ---------------------------------------------------------------------------


@router.get("/zones", summary="Caller's zones (id + name)")
def list_my_zones(user: AuthedUser = Depends(get_current_user)):
    with session_scope() as session:
        row = session.get(CustomUserCustomuser, user.id)
        scope = _resolve_read_scope(session, row)
        stmt = select(AnalyticsZone).where(AnalyticsZone.user_id == scope.owner_id)
        if scope.zone_ids is not None:
            stmt = stmt.where(AnalyticsZone.id.in_(scope.zone_ids))
        zones = session.execute(stmt).scalars().all()
        return [{"id": z.id, "name": z.name} for z in zones]


@router.get(
    "/zones/{zone_id}/active-graph",
    summary="Caller's ActiveGraph config for one zone",
)
def get_my_active_graph(zone_id: int, user: AuthedUser = Depends(get_current_user)):
    with session_scope() as session:
        row = session.get(CustomUserCustomuser, user.id)
        scope = _resolve_read_scope(session, row)
        if not scope.zone_allowed(zone_id):
            return DjangoStyleJSONResponse(
                {"detail": "ActiveGraph not found."}, status_code=404
            )
        ag = (
            session.execute(
                select(AnalyticsActivegraph)
                .where(
                    AnalyticsActivegraph.user_id == scope.owner_id,
                    AnalyticsActivegraph.zone_id == zone_id,
                )
                .order_by(AnalyticsActivegraph.id)
                .limit(1)
            )
            .scalars()
            .first()
        )
        if ag is None:
            return DjangoStyleJSONResponse(
                {"detail": "ActiveGraph not found."}, status_code=404
            )
        # model_to_dict(ag) minus id: the FK fields keep their name (value =
        # the related pk), then the status booleans in declaration order.
        d: dict[str, object] = {"user": ag.user_id, "zone": ag.zone_id}
        for key in _ACTIVE_GRAPH_STATUS_KEYS:
            d[key] = getattr(ag, key)
        if scope.is_read_only:
            # Mask any graph the technician isn't granted for this zone.
            for key in list(d.keys()):
                if key.endswith("_status") and not scope.graph_allowed(zone_id, key):
                    d[key] = False
        return d
