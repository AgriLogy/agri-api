"""fastapp /technicians — owner-facing technician management.

Strangler port of ``apps/users/router_technicians.py`` (django-ninja). A farm
owner (a regular, non-technician user) creates read-only technician logins and
grants them a subset of their zones + graphs. Byte-parity with the Django
version on every response and error envelope.

None of the three tables involved have an agri.db ORM model
(``CustomUser_customuser`` does, and is used for the owned-zone lookups via
``AnalyticsZone``): ``analytics_techniciangrant`` and
``analytics_technicianzonegrant`` are Django-managed / unmirrored, so grant
reads/writes are parameterised raw SQL. Creating a technician writes the
password as a Django-compatible ``pbkdf2_sha256`` hash (see fastapp.passwords)
so the login works against Django's ``check_password`` unchanged.
"""

from __future__ import annotations

import datetime
import json
import logging
import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, text

from agri.core.database import session_scope
from agri.db.analytics import AnalyticsZone
from fastapp import passwords
from fastapp.auth import AuthedUser, get_current_user
from fastapp.json import DjangoStyleJSONResponse

log = logging.getLogger(__name__)

router = APIRouter(tags=["technicians"])


# ── schemas ──────────────────────────────────────────────────────────────────
class ZoneScopeIn(BaseModel):
    zone_id: int
    allowed_graphs: list[str] = []


class TechnicianCreateIn(BaseModel):
    username: str
    password: str
    firstname: str | None = None
    lastname: str | None = None
    email: str | None = None
    scope: list[ZoneScopeIn] = []


class ScopeIn(BaseModel):
    scope: list[ZoneScopeIn] = []


class ResetPwIn(BaseModel):
    password: str | None = None


# ── helpers ──────────────────────────────────────────────────────────────────
def _require_owner(user: AuthedUser) -> None:
    """Only a regular (non-technician) user may manage technicians."""
    if user.is_technician:
        raise HTTPException(status_code=403, detail="Owner access required.")


def _record_audit(
    session,
    actor_id: int,
    action: str,
    target_type: str,
    target_id: Any,
    changes: dict | None = None,
) -> None:
    """Best-effort audit row (analytics_auditevent). Never raises — mirrors
    ``apps.irrigation.audit.record_audit``."""
    try:
        session.execute(
            text(
                "INSERT INTO analytics_auditevent "
                "(actor_id, action, target_type, target_id, changes, created_at) "
                "VALUES (:actor, :action, :tt, :tid, CAST(:changes AS jsonb), :now)"
            ),
            {
                "actor": actor_id,
                "action": action[:64],
                "tt": (target_type or "")[:64],
                "tid": str(target_id or "")[:64],
                "changes": json.dumps(changes or {}),
                "now": datetime.datetime.now(datetime.timezone.utc),
            },
        )
    except Exception as exc:  # pragma: no cover - fail-soft
        log.warning("audit record failed for %s: %s", action, exc)


def _grant_row(session, owner_id: int, technician_id: int):
    """The owner's grant over one technician (unique per owner+technician)."""
    return session.execute(
        text(
            "SELECT id, technician_id, owner_id, is_active "
            "FROM analytics_techniciangrant "
            "WHERE owner_id = :o AND technician_id = :t LIMIT 1"
        ),
        {"o": owner_id, "t": technician_id},
    ).first()


def _serialize(session, grant) -> dict[str, Any]:
    """Match the Django ``_serialize(TechnicianGrant)`` shape + field order."""
    tech = session.execute(
        text(
            "SELECT id, username, firstname, lastname, email, is_active "
            'FROM "CustomUser_customuser" WHERE id = :id'
        ),
        {"id": grant.technician_id},
    ).first()
    zone_grants = session.execute(
        text(
            "SELECT zone_id, allowed_graphs FROM analytics_technicianzonegrant "
            "WHERE grant_id = :g ORDER BY id"
        ),
        {"g": grant.id},
    ).all()
    return {
        "id": tech.id,
        "username": tech.username,
        "firstname": tech.firstname,
        "lastname": tech.lastname,
        "email": tech.email,
        "is_active": grant.is_active and tech.is_active,
        "scope": [
            {"zone_id": zg.zone_id, "allowed_graphs": zg.allowed_graphs or []}
            for zg in zone_grants
        ],
    }


def _apply_scope(session, owner_id: int, grant_id: int, scope: list[ZoneScopeIn]):
    """Replace the grant's per-zone scope; validate every zone is the owner's.
    Returns a 400 response on failure, else None."""
    owned_ids = set(
        session.scalars(
            select(AnalyticsZone.id).where(AnalyticsZone.user_id == owner_id)
        ).all()
    )
    for item in scope:
        if item.zone_id not in owned_ids:
            return DjangoStyleJSONResponse(
                {"detail": f"zone {item.zone_id} is not yours."}, status_code=400
            )
    session.execute(
        text("DELETE FROM analytics_technicianzonegrant WHERE grant_id = :g"),
        {"g": grant_id},
    )
    for item in scope:
        session.execute(
            text(
                "INSERT INTO analytics_technicianzonegrant "
                "(grant_id, zone_id, allowed_graphs) "
                "VALUES (:g, :z, CAST(:ag AS jsonb))"
            ),
            {
                "g": grant_id,
                "z": item.zone_id,
                "ag": json.dumps(list(item.allowed_graphs or [])),
            },
        )
    return None


# ── routes ───────────────────────────────────────────────────────────────────
@router.get("/technicians", summary="List the caller's technicians")
def list_technicians(user: AuthedUser = Depends(get_current_user)):
    _require_owner(user)
    with session_scope() as session:
        grants = session.execute(
            text(
                "SELECT id, technician_id, owner_id, is_active "
                "FROM analytics_techniciangrant WHERE owner_id = :o ORDER BY id DESC"
            ),
            {"o": user.id},
        ).all()
        return [_serialize(session, g) for g in grants]


@router.post("/technicians", summary="Create a technician login + grant")
def create_technician(
    payload: TechnicianCreateIn, user: AuthedUser = Depends(get_current_user)
):
    _require_owner(user)
    if not payload.username or not payload.password:
        return DjangoStyleJSONResponse(
            {"detail": "username and password are required."}, status_code=400
        )
    with session_scope(commit=True) as session:
        taken = session.execute(
            text('SELECT 1 FROM "CustomUser_customuser" WHERE username = :u LIMIT 1'),
            {"u": payload.username},
        ).first()
        if taken is not None:
            return DjangoStyleJSONResponse(
                {"detail": "username already taken."}, status_code=400
            )
        errs = passwords.validate_password(payload.password)
        if errs:
            return DjangoStyleJSONResponse({"detail": " ".join(errs)}, status_code=400)
        now = datetime.datetime.now(datetime.timezone.utc)
        tech_id = session.execute(
            text(
                'INSERT INTO "CustomUser_customuser" '
                "(password, is_superuser, username, firstname, lastname, email, "
                "payement_status, is_active, is_staff, is_technician, notify_every, "
                "preferred_language, date_joined) "
                "VALUES (:password, false, :username, :firstname, :lastname, :email, "
                "'actif', true, false, true, 240, 'fr', :now) RETURNING id"
            ),
            {
                "password": passwords.make_password(payload.password),
                "username": payload.username,
                "firstname": payload.firstname or "",
                "lastname": payload.lastname or "",
                "email": payload.email or f"{payload.username}@technician.local",
                "now": now,
            },
        ).scalar_one()
        grant_id = session.execute(
            text(
                "INSERT INTO analytics_techniciangrant "
                "(technician_id, owner_id, is_active, created_at) "
                "VALUES (:t, :o, true, :now) RETURNING id"
            ),
            {"t": tech_id, "o": user.id, "now": now},
        ).scalar_one()
        err = _apply_scope(session, user.id, grant_id, payload.scope)
        if err is not None:
            return err
        grant = _grant_row(session, user.id, tech_id)
        _record_audit(
            session,
            user.id,
            "technician.create",
            "technician",
            tech_id,
            {"username": payload.username},
        )
        return DjangoStyleJSONResponse(_serialize(session, grant), status_code=201)


@router.get("/technicians/{technician_id}", summary="Technician detail")
def get_technician(technician_id: int, user: AuthedUser = Depends(get_current_user)):
    _require_owner(user)
    with session_scope() as session:
        grant = _grant_row(session, user.id, technician_id)
        if grant is None:
            raise HTTPException(status_code=404, detail="Not found.")
        return _serialize(session, grant)


@router.put(
    "/technicians/{technician_id}/scope",
    summary="Replace a technician's zone + graph scope",
)
def set_scope(
    technician_id: int, payload: ScopeIn, user: AuthedUser = Depends(get_current_user)
):
    _require_owner(user)
    with session_scope(commit=True) as session:
        grant = _grant_row(session, user.id, technician_id)
        if grant is None:
            raise HTTPException(status_code=404, detail="Not found.")
        err = _apply_scope(session, user.id, grant.id, payload.scope)
        if err is not None:
            return err
        return _serialize(session, _grant_row(session, user.id, technician_id))


@router.post(
    "/technicians/{technician_id}/reset-password",
    summary="Reset a technician's password",
)
def reset_password(
    technician_id: int, payload: ResetPwIn, user: AuthedUser = Depends(get_current_user)
):
    _require_owner(user)
    with session_scope(commit=True) as session:
        grant = _grant_row(session, user.id, technician_id)
        if grant is None:
            raise HTTPException(status_code=404, detail="Not found.")
        new_pw = payload.password or secrets.token_urlsafe(9)
        errs = passwords.validate_password(new_pw)
        if errs:
            return DjangoStyleJSONResponse({"detail": " ".join(errs)}, status_code=400)
        session.execute(
            text('UPDATE "CustomUser_customuser" SET password = :p WHERE id = :id'),
            {"p": passwords.make_password(new_pw), "id": grant.technician_id},
        )
        return {"status": "reset", "password": new_pw}


@router.delete("/technicians/{technician_id}", summary="Revoke a technician")
def revoke_technician(technician_id: int, user: AuthedUser = Depends(get_current_user)):
    _require_owner(user)
    with session_scope(commit=True) as session:
        grant = _grant_row(session, user.id, technician_id)
        if grant is None:
            raise HTTPException(status_code=404, detail="Not found.")
        session.execute(
            text(
                "UPDATE analytics_techniciangrant SET is_active = false WHERE id = :g"
            ),
            {"g": grant.id},
        )
        session.execute(
            text('UPDATE "CustomUser_customuser" SET is_active = false WHERE id = :id'),
            {"id": grant.technician_id},
        )
        _record_audit(
            session, user.id, "technician.revoke", "technician", technician_id
        )
        return {"status": "revoked"}
