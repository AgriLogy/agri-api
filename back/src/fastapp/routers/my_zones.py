"""fastapp /zones writes for the zone owner (agri-web#172).

``GET /zones`` (selfreads) lists the caller's zones; until now a farmer had no
way to change one: Settings → Farms called the long-gone Django route
``/api/mod-zone-per-user/...`` and every save 404'd. Only staff could edit
zones, through ``/users/{username}/zones/{pk}``.

* ``PATCH /zones/{zone_id}`` ``{name}`` — rename one of the caller's zones.

Same rules as the other user-owned writes (sectors): the caller must own the
zone (someone else's zone is a 404, never a 403, so ids don't leak), needs
access level "editor", and technician (read-only) logins get a 403. The
rename is audited like a staff zone edit.
"""

from __future__ import annotations

from agri.core.database import session_scope
from agri.db.analytics import AnalyticsZone
from agri.db.users import CustomUserCustomuser
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select

from fastapp.adminutil import record_audit
from fastapp.auth import AuthedUser, get_current_user, require_level
from fastapp.json import DjangoStyleJSONResponse

router = APIRouter(tags=["zones"])


class ZoneRenameIn(BaseModel):
    # analytics_zone.name is varchar(100).
    name: str = Field(min_length=1, max_length=100)

    model_config = {"extra": "forbid"}


@router.patch("/zones/{zone_id}", summary="Rename one of the caller's zones")
def rename_my_zone(
    zone_id: int,
    payload: ZoneRenameIn,
    user: AuthedUser = Depends(get_current_user),
    _: AuthedUser = Depends(require_level("editor")),
):
    name = payload.name.strip()
    if not name:
        return DjangoStyleJSONResponse({"name": ["This field may not be blank."]}, 400)
    with session_scope(commit=True) as session:
        row = session.get(CustomUserCustomuser, user.id)
        if row is not None and getattr(row, "is_technician", False):
            return DjangoStyleJSONResponse(
                {"detail": "Read-only (technician) access."}, status_code=403
            )
        zone = session.scalar(
            select(AnalyticsZone).where(
                AnalyticsZone.id == zone_id, AnalyticsZone.user_id == user.id
            )
        )
        if zone is None:
            return DjangoStyleJSONResponse({"detail": "Not found."}, status_code=404)
        old = zone.name
        zone.name = name
        session.flush()
        if old != name:
            record_audit(
                session,
                user.id,
                "zone.rename",
                "zone",
                zone.id,
                {"name": [old, name]},
            )
        return {"id": zone.id, "name": zone.name, "sector_id": zone.sector_id}
