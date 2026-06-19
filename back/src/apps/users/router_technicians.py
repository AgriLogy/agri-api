"""Owner-facing technician management — /technicians.

A farm owner (a regular, non-technician user) creates technician logins and
grants them read-only access to a subset of their zones + graphs. Enforcement
lives in ``agriapi.api.scope``; this router is only the CRUD surface.
"""

from __future__ import annotations

import secrets
from typing import Any

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from ninja import Router, Schema
from ninja.responses import Response

from agriapi.api.auth import JwtAuth
from analytics.models import TechnicianGrant, TechnicianZoneGrant, Zone
from apps.users.models import CustomUser

router = Router()


def _require_owner(request) -> Response | None:
    """Only a regular (non-technician) user may manage technicians."""
    user = request.auth
    if user is None or getattr(user, "is_technician", False):
        return Response({"detail": "Owner access required."}, status=403)
    return None


class ZoneScopeIn(Schema):
    zone_id: int
    allowed_graphs: list[str] = []


class TechnicianCreateIn(Schema):
    username: str
    password: str
    firstname: str | None = None
    lastname: str | None = None
    email: str | None = None
    scope: list[ZoneScopeIn] = []


class ScopeIn(Schema):
    scope: list[ZoneScopeIn] = []


class ResetPwIn(Schema):
    password: str | None = None


def _serialize(grant: TechnicianGrant) -> dict[str, Any]:
    tech = grant.technician
    zone_grants = [
        {
            "zone_id": zg.zone_id,
            "allowed_graphs": zg.allowed_graphs or [],
        }
        for zg in grant.zone_grants.all()
    ]
    return {
        "id": tech.id,
        "username": tech.username,
        "firstname": getattr(tech, "firstname", ""),
        "lastname": getattr(tech, "lastname", ""),
        "email": getattr(tech, "email", ""),
        "is_active": grant.is_active and tech.is_active,
        "scope": zone_grants,
    }


def _grant_for(owner, technician_id: int) -> TechnicianGrant | None:
    return (
        TechnicianGrant.objects.select_related("technician")
        .prefetch_related("zone_grants")
        .filter(owner=owner, technician_id=technician_id)
        .first()
    )


def _apply_scope(
    owner, grant: TechnicianGrant, scope: list[ZoneScopeIn]
) -> Response | None:
    """Replace the grant's per-zone scope; validates every zone is the owner's."""
    owned_ids = set(Zone.objects.filter(user=owner).values_list("id", flat=True))
    for item in scope:
        if item.zone_id not in owned_ids:
            return Response(
                {"detail": f"zone {item.zone_id} is not yours."}, status=400
            )
    grant.zone_grants.all().delete()
    TechnicianZoneGrant.objects.bulk_create(
        [
            TechnicianZoneGrant(
                grant=grant,
                zone_id=item.zone_id,
                allowed_graphs=list(item.allowed_graphs or []),
            )
            for item in scope
        ]
    )
    return None


@router.get("", auth=JwtAuth(), summary="List the caller's technicians")
def list_technicians(request):
    guard = _require_owner(request)
    if guard is not None:
        return guard
    grants = (
        TechnicianGrant.objects.select_related("technician")
        .prefetch_related("zone_grants")
        .filter(owner=request.auth)
        .order_by("-id")
    )
    return [_serialize(g) for g in grants]


@router.post("", auth=JwtAuth(), summary="Create a technician login + grant")
def create_technician(request, payload: TechnicianCreateIn):
    guard = _require_owner(request)
    if guard is not None:
        return guard
    if not payload.username or not payload.password:
        return Response({"detail": "username and password are required."}, status=400)
    if CustomUser.objects.filter(username=payload.username).exists():
        return Response({"detail": "username already taken."}, status=400)
    try:
        validate_password(payload.password)
    except DjangoValidationError as e:
        return Response({"detail": " ".join(e.messages)}, status=400)

    tech = CustomUser(
        username=payload.username,
        firstname=payload.firstname or "",
        lastname=payload.lastname or "",
        email=payload.email or f"{payload.username}@technician.local",
        is_staff=False,
        is_technician=True,
    )
    tech.set_password(payload.password)
    tech.save()

    grant = TechnicianGrant.objects.create(technician=tech, owner=request.auth)
    err = _apply_scope(request.auth, grant, payload.scope)
    if err is not None:
        return err
    grant = _grant_for(request.auth, tech.id)
    return Response(_serialize(grant), status=201)


@router.get("/{technician_id}", auth=JwtAuth(), summary="Technician detail")
def get_technician(request, technician_id: int):
    guard = _require_owner(request)
    if guard is not None:
        return guard
    grant = _grant_for(request.auth, technician_id)
    if grant is None:
        return Response({"detail": "Not found."}, status=404)
    return _serialize(grant)


@router.put(
    "/{technician_id}/scope",
    auth=JwtAuth(),
    summary="Replace a technician's zone + graph scope",
)
def set_scope(request, technician_id: int, payload: ScopeIn):
    guard = _require_owner(request)
    if guard is not None:
        return guard
    grant = _grant_for(request.auth, technician_id)
    if grant is None:
        return Response({"detail": "Not found."}, status=404)
    err = _apply_scope(request.auth, grant, payload.scope)
    if err is not None:
        return err
    return _serialize(_grant_for(request.auth, technician_id))


@router.post(
    "/{technician_id}/reset-password",
    auth=JwtAuth(),
    summary="Reset a technician's password",
)
def reset_password(request, technician_id: int, payload: ResetPwIn):
    guard = _require_owner(request)
    if guard is not None:
        return guard
    grant = _grant_for(request.auth, technician_id)
    if grant is None:
        return Response({"detail": "Not found."}, status=404)
    new_pw = payload.password or secrets.token_urlsafe(9)
    try:
        validate_password(new_pw)
    except DjangoValidationError as e:
        return Response({"detail": " ".join(e.messages)}, status=400)
    tech = grant.technician
    tech.set_password(new_pw)
    tech.save(update_fields=["password"])
    return {"status": "reset", "password": new_pw}


@router.delete("/{technician_id}", auth=JwtAuth(), summary="Revoke a technician")
def revoke_technician(request, technician_id: int):
    guard = _require_owner(request)
    if guard is not None:
        return guard
    grant = _grant_for(request.auth, technician_id)
    if grant is None:
        return Response({"detail": "Not found."}, status=404)
    grant.is_active = False
    grant.save(update_fields=["is_active"])
    tech = grant.technician
    tech.is_active = False
    tech.save(update_fields=["is_active"])
    return {"status": "revoked"}
