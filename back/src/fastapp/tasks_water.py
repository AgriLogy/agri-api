"""Water level beat job + alert email (agrilogy-front#261).

* ``check_water_level_offline`` (beat, every 10 min): a source that stops
  reporting produces no reading, so nothing on the ingest path can notice.
  The sweep re-runs :func:`fastapp.water_level.sync_alerts` for every source,
  which opens OFFLINE once the last reading is older than the source's
  ``offline_after_min`` (and resolves it when data resumes).
* ``send_water_level_alert_email``: one email to the owner per newly opened
  alert, in French like the other alert emails. Skips silently when the alert
  was resolved before the worker picked it up.
"""

from __future__ import annotations

import logging

from agri.core.database import session_scope
from agri.db.users import CustomUserCustomuser
from agri.db.water import WaterLevelAlert, WaterSource
from sqlalchemy import select

from fastapp import water_level as svc
from fastapp.tasks_comms import _record_delivery, _send_email

logger = logging.getLogger(__name__)

_KIND_TEXT = {
    "LOW": "niveau bas",
    "CRITICAL": "niveau critique",
    "HIGH": "niveau haut",
    "OFFLINE": "aucune donnée reçue",
}


def check_water_level_offline() -> dict:
    now = svc.utcnow()
    opened = 0
    with session_scope(commit=True) as session:
        for source in session.scalars(select(WaterSource)).all():
            try:
                opened += len(svc.sync_alerts(session, source, now))
            except Exception as exc:  # one bad source must not stop the sweep
                logger.warning("water level sweep: source %s: %s", source.id, exc)
    return {"opened": opened}


def _fmt(value: float | None, unit: str, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f} {unit}"


def send_water_level_alert_email(*, alert_id: int) -> dict:
    with session_scope(commit=True) as session:
        alert = session.get(WaterLevelAlert, alert_id)
        if alert is None:
            return {"sent": 0, "reason": "alert_missing"}
        if alert.resolved_at is not None:
            return {"sent": 0, "reason": "alert_resolved"}
        source = session.get(WaterSource, alert.water_source_id)
        user = session.get(CustomUserCustomuser, alert.user_id)
        recipient = (getattr(user, "email", "") or "").strip() if user else ""
        if source is None or not recipient:
            return {"sent": 0, "reason": "no_recipient"}

        what = _KIND_TEXT.get(alert.kind, alert.kind)
        subject = f"Alerte eau — {source.name} : {what}"
        if alert.kind == "OFFLINE":
            detail = (
                f"Aucune mesure de niveau n'a été reçue depuis plus de "
                f"{source.offline_after_min} minutes.\n"
            )
        else:
            detail = (
                f"Niveau        : {_fmt(alert.level_m, 'm')}\n"
                f"Remplissage   : {_fmt(alert.fill_percent, '%', 1)}\n"
                f"Seuils        : critique {source.critical_pct:g} % · "
                f"bas {source.low_warning_pct:g} % · haut {source.high_pct:g} %\n"
            )
        body = (
            f"Bonjour {getattr(user, 'firstname', '') or user.username},\n\n"
            f"Alerte « {what} » sur la source d'eau « {source.name} ».\n\n"
            f"{detail}"
            f"Horodatage    : {alert.opened_at.isoformat()}\n\n"
            f"Vous pouvez ajuster les seuils depuis la Station d'eau.\n"
        )
        ok = _send_email(to=recipient, subject=subject, body=body)
        _record_delivery(
            session,
            channel="email",
            kind="water_level_alert",
            recipient=recipient,
            user_id=user.id,
            status="sent" if ok else "failed",
            **({} if ok else {"error": "send_error"}),
        )
        return {"sent": 1 if ok else 0, "alert_id": alert_id}
