"""fastapp /manager-affirmations — approval-workflow rows + decide.

Strangler port of ``apps/irrigation/router_manager_affirmation.py`` (django-
ninja). Byte-parity with the Django version:

* ``GET  /manager-affirmations`` — own rows (admin sees all), optional
  ``status`` filter, ``-created_at`` order.
* ``POST /manager-affirmations`` — create (unknown action → 400 ``{"action": ...}``).
* ``POST /manager-affirmations/{pk}/approve`` — admin-only; applies the payload
  then flips to ``approved`` atomically (apply failure → row stays pending).
* ``POST /manager-affirmations/{pk}/reject`` — admin-only; flips to ``rejected``.

The apply step (``apply_affirmation``) is ported to SQLAlchemy here so the whole
prefix can cut over: it mutates zones / Kc calendars / user activation through
the agri-core session, mirroring ``apps/irrigation/affirmation_appliers.py``.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from agri.core.database import session_scope
from agri.db.analytics import AnalyticsKc, AnalyticsManageraffirmation, AnalyticsZone
from agri.db.users import CustomUserCustomuser
from fastapp.auth import AuthedUser, get_current_user
from fastapp.json import DjangoStyleJSONResponse
from fastapp.routers.kc import KcPeriodIn, _replace_periods

log = logging.getLogger(__name__)

router = APIRouter(tags=["manager-affirmation"])

# Mirror the Django ``ManagerAffirmation`` model constants.
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
ACTION_PARAM_CHANGE = "zone_params_change"
ACTION_USER_REACTIVATE = "user_reactivate"
ACTION_KC_PERIODS = "kc_periods_change"
ACTION_CHOICES = (ACTION_PARAM_CHANGE, ACTION_KC_PERIODS, ACTION_USER_REACTIVATE)

# Zone params a user may request to change (writable subset).
ZONE_PARAM_WRITABLE = {
    "soil_param_TAW",
    "soil_param_FC",
    "soil_param_WP",
    "soil_param_RAW",
    "critical_moisture_threshold",
    "pomp_flow_rate",
    "irrigation_water_quantity",
}


class AffirmationApplyError(Exception):
    """Raised when an approved affirmation's payload cannot be applied."""

    def __init__(self, detail: dict[str, Any] | str):
        self.detail: dict[str, Any] = (
            {"detail": detail} if isinstance(detail, str) else detail
        )
        super().__init__(str(detail))


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ManagerAffirmationIn(BaseModel):
    action: str
    payload: dict[str, Any] | None = None


class DecisionIn(BaseModel):
    note: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _username(session, user_id: int | None) -> str | None:
    if not user_id:
        return None
    row = session.get(CustomUserCustomuser, user_id)
    return row.username if row is not None else None


def _serialize(session, a: AnalyticsManageraffirmation) -> dict[str, Any]:
    return {
        "id": a.id,
        "action": a.action,
        "payload": a.payload,
        "status": a.status,
        "requested_by": a.requested_by_id,
        "requested_by_username": _username(session, a.requested_by_id),
        "decided_by": a.decided_by_id,
        "decided_by_username": _username(session, a.decided_by_id),
        "decided_at": a.decided_at.isoformat() if a.decided_at else None,
        "decision_note": a.decision_note,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
    }


def _validate_zone(payload: dict[str, Any]) -> dict | None:
    if "space" in payload and payload["space"] is not None and payload["space"] <= 0:
        return {"space": "Space must be strictly positive."}
    cmt = payload.get("critical_moisture_threshold")
    if cmt is not None and (cmt < 0 or cmt > 100):
        return {"critical_moisture_threshold": "Threshold must be between 0 and 100."}
    pfr = payload.get("pomp_flow_rate")
    if pfr is not None and pfr < 0:
        return {"pomp_flow_rate": "Flow rate must be non-negative."}
    fc = payload.get("soil_param_FC")
    wp = payload.get("soil_param_WP")
    if fc is not None and wp is not None and fc < wp:
        return {
            "non_field_errors": (
                "Field capacity (FC) cannot be lower than wilting point (WP)."
            )
        }
    return None


def _apply_zone_params_change(session, a: AnalyticsManageraffirmation) -> None:
    payload = a.payload or {}
    fields = payload.get("fields")
    if not fields:
        log.info("affirmation #%s: zone_params_change with no fields — no-op", a.id)
        return
    if not isinstance(fields, dict):
        raise AffirmationApplyError({"fields": "Must be an object of {param: value}."})

    zone_id = payload.get("zone_id")
    if not isinstance(zone_id, int):
        raise AffirmationApplyError({"zone_id": "Required (int) when 'fields' is set."})

    unknown = set(fields) - ZONE_PARAM_WRITABLE
    if unknown:
        raise AffirmationApplyError(
            {
                "fields": (
                    f"Not writable: {sorted(unknown)}. "
                    f"Allowed: {sorted(ZONE_PARAM_WRITABLE)}."
                )
            }
        )

    err = _validate_zone(fields)
    if err is not None:
        raise AffirmationApplyError(err)

    zone = session.scalars(
        select(AnalyticsZone).where(
            AnalyticsZone.id == zone_id, AnalyticsZone.user_id == a.requested_by_id
        )
    ).first()
    if zone is None:
        raise AffirmationApplyError(
            {"zone_id": "Zone not found for the requesting user."}
        )

    for field, value in fields.items():
        setattr(zone, field, value)
    session.flush()
    log.info("affirmation #%s applied zone %s params %s", a.id, zone_id, sorted(fields))


def _apply_kc_periods_change(session, a: AnalyticsManageraffirmation) -> None:
    payload = a.payload or {}
    raw_periods = payload.get("periods")
    if not raw_periods:
        log.info("affirmation #%s: kc_periods_change with no periods — no-op", a.id)
        return
    if not isinstance(raw_periods, list):
        raise AffirmationApplyError({"periods": "Must be a list of period objects."})

    kc_id = payload.get("kc_id")
    if not isinstance(kc_id, int):
        raise AffirmationApplyError({"kc_id": "Required (int) when 'periods' is set."})

    try:
        periods = [KcPeriodIn(**p) for p in raw_periods]
    except Exception as exc:  # pydantic shape / date-parse error
        raise AffirmationApplyError({"periods": f"Invalid period: {exc}"})

    kc = session.scalars(
        select(AnalyticsKc).where(
            AnalyticsKc.id == kc_id, AnalyticsKc.user_id == a.requested_by_id
        )
    ).first()
    if kc is None:
        raise AffirmationApplyError(
            {"kc_id": "Crop calendar not found for the requesting user."}
        )

    _replace_periods(session, kc, periods)
    log.info("affirmation #%s replaced %s periods on kc %s", a.id, len(periods), kc_id)


def _apply_user_reactivate(session, a: AnalyticsManageraffirmation) -> None:
    payload = a.payload or {}
    user_id = payload.get("user_id")
    if user_id is None:
        log.info("affirmation #%s: user_reactivate with no user_id — no-op", a.id)
        return
    if not isinstance(user_id, int):
        raise AffirmationApplyError({"user_id": "Must be an int."})

    user = session.get(CustomUserCustomuser, user_id)
    if user is None:
        raise AffirmationApplyError({"user_id": "User not found."})
    if not user.is_active:
        user.is_active = True
        session.flush()
    log.info("affirmation #%s reactivated user %s", a.id, user_id)


_HANDLERS = {
    ACTION_PARAM_CHANGE: _apply_zone_params_change,
    ACTION_KC_PERIODS: _apply_kc_periods_change,
    ACTION_USER_REACTIVATE: _apply_user_reactivate,
}


def apply_affirmation(session, a: AnalyticsManageraffirmation) -> None:
    handler = _HANDLERS.get(a.action)
    if handler is None:
        raise AffirmationApplyError(f"No applier registered for action '{a.action}'.")
    handler(session, a)


# ---------------------------------------------------------------------------
# /manager-affirmations
# ---------------------------------------------------------------------------


@router.get(
    "/manager-affirmations",
    summary="List manager affirmations (own; admin sees all)",
)
def list_affirmations(
    status: str | None = None, user: AuthedUser = Depends(get_current_user)
):
    criteria = []
    if not user.is_staff:
        criteria.append(AnalyticsManageraffirmation.requested_by_id == user.id)
    if status:
        criteria.append(AnalyticsManageraffirmation.status == status)
    with session_scope() as session:
        rows = session.scalars(
            select(AnalyticsManageraffirmation)
            .where(*criteria)
            .order_by(AnalyticsManageraffirmation.created_at.desc())
        ).all()
        return [_serialize(session, a) for a in rows]


@router.post("/manager-affirmations", summary="Create a manager affirmation")
def create_affirmation(
    payload: ManagerAffirmationIn, user: AuthedUser = Depends(get_current_user)
):
    if payload.action not in ACTION_CHOICES:
        return DjangoStyleJSONResponse(
            {"action": f"Unknown action. Allowed: {sorted(ACTION_CHOICES)}."},
            status_code=400,
        )
    now = datetime.datetime.now(datetime.timezone.utc)
    with session_scope(commit=True) as session:
        a = AnalyticsManageraffirmation(
            action=payload.action,
            payload=payload.payload or {},
            status=STATUS_PENDING,
            decision_note="",
            requested_by_id=user.id,
            created_at=now,
            updated_at=now,
        )
        session.add(a)
        session.flush()
        return DjangoStyleJSONResponse(_serialize(session, a), status_code=201)


# ---------------------------------------------------------------------------
# /manager-affirmations/{pk}/{action}
# ---------------------------------------------------------------------------


def _decide(pk: int, action: str, note: str | None, user: AuthedUser):
    if not user.is_staff:
        raise HTTPException(status_code=403, detail="Admin access required")

    now = datetime.datetime.now(datetime.timezone.utc)
    with session_scope(commit=True) as session:
        a = session.get(AnalyticsManageraffirmation, pk)
        if a is None:
            raise HTTPException(status_code=404, detail="Affirmation not found.")
        if a.status != STATUS_PENDING:
            raise HTTPException(
                status_code=400, detail=f"Already decided ({a.status})."
            )

        def _stamp(status: str) -> None:
            a.status = status
            a.decided_by_id = user.id
            a.decided_at = now
            a.decision_note = note or ""
            a.updated_at = now
            session.flush()

        if action == "approve":
            try:
                apply_affirmation(session, a)
                _stamp(STATUS_APPROVED)
            except AffirmationApplyError as exc:
                session.rollback()
                return DjangoStyleJSONResponse(
                    {"detail": "Could not apply affirmation.", "errors": exc.detail},
                    status_code=400,
                )
        else:
            _stamp(STATUS_REJECTED)

        log.info("admin %s %sd affirmation #%s", user.username, action, a.id)
        return _serialize(session, a)


@router.post(
    "/manager-affirmations/{pk}/approve",
    summary="Admin: approve a manager affirmation",
)
def approve_affirmation(
    pk: int, payload: DecisionIn, user: AuthedUser = Depends(get_current_user)
):
    return _decide(pk, "approve", payload.note, user)


@router.post(
    "/manager-affirmations/{pk}/reject",
    summary="Admin: reject a manager affirmation",
)
def reject_affirmation(
    pk: int, payload: DecisionIn, user: AuthedUser = Depends(get_current_user)
):
    return _decide(pk, "reject", payload.note, user)
