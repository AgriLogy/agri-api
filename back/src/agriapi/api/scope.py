"""Read-scope resolution for technician (scoped read-only) access.

A technician is a real ``CustomUser`` whose effective read scope is *another*
user's (the owner's) rows, narrowed to a subset of zones and a subset of graph
keys per zone. Every customer-facing read path resolves its scope through
``resolve_read_scope`` so new dashboards inherit scoping for free.

Regular users (and admins acting on their own data) get an unrestricted scope
pointing at their own id.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ninja.responses import Response


@dataclass
class ReadScope:
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
            # User-wide rows (no zone) are only visible to non-technicians.
            return False
        return zone_id in self.zone_ids

    def graph_allowed(self, zone_id: int | None, status_key: str) -> bool:
        """True if the given ActiveGraph status key is visible for the zone."""
        if self.zone_ids is None:
            return True
        if zone_id is None:
            return False
        allowed = self.allowed_graphs_by_zone.get(zone_id)
        # Empty/missing whitelist for a granted zone = no graphs visible.
        return bool(allowed) and status_key in allowed


def resolve_read_scope(caller) -> ReadScope:
    """Resolve the data scope for the authenticated caller."""
    if caller is None:
        return ReadScope(owner_id=-1, zone_ids=set(), is_read_only=True)

    if not getattr(caller, "is_technician", False):
        return ReadScope(owner_id=caller.id, zone_ids=None, is_read_only=False)

    # Technician: resolve the active grant + its per-zone scope.
    from analytics.models import TechnicianGrant, TechnicianZoneGrant

    grant = (
        TechnicianGrant.objects.filter(technician_id=caller.id, is_active=True)
        .order_by("-id")
        .first()
    )
    if grant is None:
        return ReadScope(owner_id=caller.id, zone_ids=set(), is_read_only=True)

    zone_ids: set[int] = set()
    allowed: dict[int, set[str]] = {}
    for zg in TechnicianZoneGrant.objects.filter(grant=grant):
        zone_ids.add(zg.zone_id)
        allowed[zg.zone_id] = set(zg.allowed_graphs or [])

    return ReadScope(
        owner_id=grant.owner_id,
        zone_ids=zone_ids,
        allowed_graphs_by_zone=allowed,
        is_read_only=True,
    )


def block_if_technician(request) -> Response | None:
    """Return a 403 when the caller is a technician (read-only). Else None."""
    if getattr(request.auth, "is_technician", False):
        return Response({"detail": "Read-only (technician) access."}, status=403)
    return None
