"""Apply a ``ManagerAffirmation`` payload to the underlying resource on approval.

A non-admin user submits a ``ManagerAffirmation`` (action + payload) via
``POST /api/manager-affirmations/``; nothing is mutated yet. When an admin
approves, :func:`apply_affirmation` dispatches on ``action`` and applies the
payload. Appliers run inside the approving request's ``transaction.atomic``
block (see ``router_manager_affirmation._decide``): raising
:class:`AffirmationApplyError` rolls the transaction back and leaves the
affirmation ``pending``.

A recognised action whose payload carries no actionable data (e.g. a legacy
or placeholder affirmation) is treated as a **no-op** — the affirmation is
still approved, nothing is changed. A *non-empty but malformed* payload is an
error.

Payload contracts
-----------------
``zone_params_change``: ``{"zone_id": int, "fields": {<writable zone param>: value}}``
``kc_periods_change``:  ``{"kc_id": int, "periods": [{"period_name","start_date","end_date","kc_value"}, ...]}``
``user_reactivate``:    ``{"user_id": int}``
"""

from __future__ import annotations

import logging
from typing import Any

from analytics.models import ManagerAffirmation

log = logging.getLogger(__name__)

# Zone params a user may request to change (writable subset; "id"/"name"/"space"
# are not user-tunable agronomy params and stay admin-only).
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
    """Raised when an approved affirmation's payload cannot be applied.

    ``detail`` is a JSON-serialisable dict surfaced to the admin caller.
    """

    def __init__(self, detail: dict[str, Any] | str):
        self.detail: dict[str, Any] = (
            {"detail": detail} if isinstance(detail, str) else detail
        )
        super().__init__(str(detail))


def apply_affirmation(a: ManagerAffirmation) -> None:
    """Apply ``a``'s payload. Raises :class:`AffirmationApplyError` on failure."""
    handler = _HANDLERS.get(a.action)
    if handler is None:
        raise AffirmationApplyError(f"No applier registered for action '{a.action}'.")
    handler(a)


def _apply_zone_params_change(a: ManagerAffirmation) -> None:
    from apps.irrigation.models import Zone
    from apps.irrigation.router_admin import _validate_zone

    payload = a.payload or {}
    fields = payload.get("fields")
    if not fields:
        log.info("affirmation #%s: zone_params_change with no fields — no-op", a.pk)
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

    zone = Zone.objects.filter(id=zone_id, user=a.requested_by).first()
    if zone is None:
        raise AffirmationApplyError(
            {"zone_id": "Zone not found for the requesting user."}
        )

    for field, value in fields.items():
        setattr(zone, field, value)
    zone.save(update_fields=list(fields))
    log.info("affirmation #%s applied zone %s params %s", a.pk, zone_id, sorted(fields))


def _apply_kc_periods_change(a: ManagerAffirmation) -> None:
    from apps.irrigation.models import Kc
    from apps.irrigation.router_kc import KcPeriodIn, _replace_periods

    payload = a.payload or {}
    raw_periods = payload.get("periods")
    if not raw_periods:
        log.info("affirmation #%s: kc_periods_change with no periods — no-op", a.pk)
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

    kc = Kc.objects.filter(id=kc_id, user=a.requested_by).first()
    if kc is None:
        raise AffirmationApplyError(
            {"kc_id": "Crop calendar not found for the requesting user."}
        )

    _replace_periods(kc, periods)
    log.info("affirmation #%s replaced %s periods on kc %s", a.pk, len(periods), kc_id)


def _apply_user_reactivate(a: ManagerAffirmation) -> None:
    from django.contrib.auth import get_user_model

    payload = a.payload or {}
    user_id = payload.get("user_id")
    if user_id is None:
        log.info("affirmation #%s: user_reactivate with no user_id — no-op", a.pk)
        return
    if not isinstance(user_id, int):
        raise AffirmationApplyError({"user_id": "Must be an int."})

    User = get_user_model()
    user = User.objects.filter(pk=user_id).first()
    if user is None:
        raise AffirmationApplyError({"user_id": "User not found."})
    if not user.is_active:
        user.is_active = True
        user.save(update_fields=["is_active"])
    log.info("affirmation #%s reactivated user %s", a.pk, user_id)


_HANDLERS = {
    ManagerAffirmation.ACTION_PARAM_CHANGE: _apply_zone_params_change,
    ManagerAffirmation.ACTION_KC_PERIODS: _apply_kc_periods_change,
    ManagerAffirmation.ACTION_USER_REACTIVATE: _apply_user_reactivate,
}
