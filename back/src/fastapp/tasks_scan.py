"""fastapp scan task bodies (F8b) — the device/insight/irrigation beat jobs.

Django-free ports of the scan Celery tasks in ``agriapi/tasks.py``. Plain
functions; the native Celery app (F10) wraps them under the SAME names. Additive
until then. DB access via the agri-core SQLAlchemy session; the unmanaged
``lora_uplink`` table is read with raw SQL (as the admin routers do).
"""

from __future__ import annotations

import datetime
import logging

from agri.core.database import session_scope
from agri.db.devices import AnalyticsDevice
from agri.db.users import CustomUserCustomuser
from sqlalchemy import or_, select, text, update

from fastapp.tasks_comms import _record_delivery, _send_email

logger = logging.getLogger(__name__)

DEVICE_LOW_BATTERY_V = 3.4
DEVICE_OFFLINE_HOURS = 24
DEVICE_HEALTH_COOLDOWN_HOURS = 24

_PROBLEM_FR = {"offline": "hors ligne", "low_battery": "batterie faible"}


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def classify_device_health(
    last_seen,
    battery_v,
    *,
    reference,
    low_battery_v: float = DEVICE_LOW_BATTERY_V,
    offline_hours: int = DEVICE_OFFLINE_HOURS,
) -> list[str]:
    """Pure health check for one device — ``["offline"]`` / ``["low_battery"]``
    (empty = healthy). Byte-identical logic to ``agriapi.tasks``."""
    issues: list[str] = []
    if last_seen is None or (reference - last_seen) > datetime.timedelta(
        hours=offline_hours
    ):
        issues.append("offline")
    if battery_v is not None and battery_v < low_battery_v:
        issues.append("low_battery")
    return issues


def scan_device_health() -> dict:
    """Scan active devices for offline / low-battery problems and email the
    owner at most once per cooldown window (atomic dedup claim on
    ``last_health_notified``). Health derives from the latest ``lora_uplink``
    matching the device serial. Django-free port."""
    reference = _utcnow()
    cooldown_cutoff = reference - datetime.timedelta(hours=DEVICE_HEALTH_COOLDOWN_HOURS)
    scanned = notified = healthy = skipped = 0

    with session_scope(commit=True) as session:
        # Column-scoped select — NOT ``select(AnalyticsDevice)``. The health scan
        # only ever reads these five columns, and selecting the whole entity
        # emits every mapped column, including ``latitude``/``longitude`` (the
        # device-map feature) which only exist once the agri-db migration that
        # adds them has been applied. A beat job must not be coupled to an
        # unrelated, not-yet-applied migration: naming its own columns keeps it
        # working on both schemas, before and after.
        devices = session.execute(
            select(
                AnalyticsDevice.id,
                AnalyticsDevice.serial,
                AnalyticsDevice.name,
                AnalyticsDevice.user_id,
                AnalyticsDevice.last_health_notified,
            )
            .where(AnalyticsDevice.is_active.is_(True))
            .order_by(AnalyticsDevice.id)
        ).all()
        for device in devices:
            scanned += 1
            row = session.execute(
                text(
                    "SELECT received_at, battery_v FROM lora_uplink "
                    "WHERE dev_eui = :s ORDER BY received_at DESC LIMIT 1"
                ),
                {"s": device.serial},
            ).first()
            last_seen = row[0] if row else None
            battery_v = row[1] if row else None
            issues = classify_device_health(last_seen, battery_v, reference=reference)
            if not issues:
                healthy += 1
                continue

            user = (
                session.get(CustomUserCustomuser, device.user_id)
                if device.user_id
                else None
            )
            recipient = (getattr(user, "email", "") or "").strip() if user else ""
            if not recipient:
                skipped += 1
                continue

            # Atomic dedup claim: only the UPDATE that flips last_health_notified
            # from never/expired wins → one email per cooldown window.
            prev = device.last_health_notified
            claimed = (
                session.execute(
                    update(AnalyticsDevice)
                    .where(
                        AnalyticsDevice.id == device.id,
                        or_(
                            AnalyticsDevice.last_health_notified.is_(None),
                            AnalyticsDevice.last_health_notified < cooldown_cutoff,
                        ),
                    )
                    .values(last_health_notified=reference)
                ).rowcount
                == 1
            )
            if not claimed:
                skipped += 1
                continue

            label = device.name or device.serial
            problems = ", ".join(_PROBLEM_FR[i] for i in issues)
            subject = f"Alerte appareil : {label}"
            body = (
                f"Votre appareil « {label} » présente un problème : {problems}.\n\n"
                f"Dernière communication : "
                f"{last_seen.isoformat() if last_seen else 'jamais'}."
            )
            ok = _send_email(to=recipient, subject=subject, body=body)
            if ok:
                notified += 1
                _record_delivery(
                    session,
                    channel="email",
                    kind="device_health",
                    recipient=recipient,
                    user_id=user.id,
                    status="sent",
                )
            else:
                # roll back the claim so a later tick can retry the send.
                session.execute(
                    update(AnalyticsDevice)
                    .where(AnalyticsDevice.id == device.id)
                    .values(last_health_notified=prev)
                )
                skipped += 1
                _record_delivery(
                    session,
                    channel="email",
                    kind="device_health",
                    recipient=recipient,
                    user_id=user.id,
                    status="failed",
                    error="send_error",
                )

    return {
        "scanned": scanned,
        "notified": notified,
        "healthy": healthy,
        "skipped": skipped,
    }


# ---------------------------------------------------------------------------
# scan_proactive_insights — one high-value irrigation nudge per customer
# ---------------------------------------------------------------------------
PROACTIVE_COOLDOWN_HOURS = 24


def scan_proactive_insights() -> dict:
    """Per active customer, compute one irrigation insight via the assistant's
    agronomy tool and email an actionable nudge when a zone needs water. Deduped
    to once per cooldown window per user (atomic claim on
    ``AssistantProactiveNotice.last_sent``). Django-free port."""
    from agri.db.assistant import AssistantProactiveNotice

    from fastapp.assistant.tools import _get_irrigation_advice
    from fastapp.auth import AuthedUser

    reference = _utcnow()
    cooldown_cutoff = reference - datetime.timedelta(hours=PROACTIVE_COOLDOWN_HOURS)
    scanned = notified = quiet = skipped = 0

    with session_scope() as session:
        customers = session.scalars(
            select(CustomUserCustomuser)
            .where(
                CustomUserCustomuser.is_active.is_(True),
                CustomUserCustomuser.is_staff.is_(False),
                CustomUserCustomuser.is_technician.is_(False),
            )
            .order_by(CustomUserCustomuser.id)
        ).all()
        rows = [
            (
                u.id,
                u.username,
                (getattr(u, "email", "") or ""),
                getattr(u, "preferred_language", "fr") or "fr",
            )
            for u in customers
        ]

    for uid, uname, email, lang in rows:
        scanned += 1
        recipient = email.strip()
        if not recipient:
            skipped += 1
            continue
        authed = AuthedUser(
            id=uid,
            username=uname,
            email=email,
            is_staff=False,
            is_technician=False,
            preferred_language=lang,
        )
        try:
            advice = _get_irrigation_advice(authed, {})
        except Exception:
            skipped += 1
            logger.exception("proactive: advice failed for %s", recipient)
            continue
        if advice.get("recommendation") != "irrigate":
            quiet += 1
            continue

        # Atomic dedup claim: ensure a ledger row, capture the prior stamp, then
        # only the UPDATE that flips last_sent from never/expired wins.
        prev_sent = None
        with session_scope(commit=True) as session:
            notice = session.scalar(
                select(AssistantProactiveNotice).where(
                    AssistantProactiveNotice.user_id == uid
                )
            )
            if notice is None:
                session.add(AssistantProactiveNotice(user_id=uid, last_sent=None))
                session.flush()
            else:
                prev_sent = notice.last_sent
            claimed = (
                session.execute(
                    update(AssistantProactiveNotice)
                    .where(
                        AssistantProactiveNotice.user_id == uid,
                        or_(
                            AssistantProactiveNotice.last_sent.is_(None),
                            AssistantProactiveNotice.last_sent < cooldown_cutoff,
                        ),
                    )
                    .values(last_sent=reference)
                ).rowcount
                == 1
            )
        if not claimed:
            skipped += 1
            continue

        zone = advice.get("zone_name") or "votre parcelle"
        reason = advice.get("reason") or ""
        water = advice.get("estimated_water_m3")
        extra = f" (~{water} m³ recommandés)" if water else ""
        subject = f"Conseil d'irrigation : {zone}"
        body = (
            f"Votre assistant Agrilogy recommande d'irriguer "
            f"« {zone} »{extra}.\n\n{reason}\n\n"
            f"Ouvrez l'application pour le détail."
        )
        ok = _send_email(to=recipient, subject=subject, body=body)
        with session_scope(commit=True) as session:
            if ok:
                notified += 1
                _record_delivery(
                    session,
                    channel="email",
                    kind="proactive",
                    recipient=recipient,
                    user_id=uid,
                    status="sent",
                )
            else:
                # roll back the claim so a later tick can retry.
                session.execute(
                    update(AssistantProactiveNotice)
                    .where(AssistantProactiveNotice.user_id == uid)
                    .values(last_sent=prev_sent)
                )
                skipped += 1
                _record_delivery(
                    session,
                    channel="email",
                    kind="proactive",
                    recipient=recipient,
                    user_id=uid,
                    status="failed",
                    error="send_error",
                )

    return {
        "scanned": scanned,
        "notified": notified,
        "quiet": quiet,
        "skipped": skipped,
    }


# ---------------------------------------------------------------------------
# run_due_irrigation_programs — fire scheduled irrigation programs
# ---------------------------------------------------------------------------
IRRIGATION_RUN_WINDOW_MIN = 15
_SIMULATED_DETAIL = "Simulation mode — no hardware actuated."


def run_due_irrigation_programs() -> dict:
    """Fire enabled irrigation programs whose start_time falls in the current
    window today, once per window. Each due program creates a scheduled
    OutputCommand and dispatches it (simulated unless IRRIGATION_DISPATCH_ENABLED).
    Django-free port of ``agriapi.tasks.run_due_irrigation_programs``."""
    from agri.db.irrigation import (
        AnalyticsIrrigationprogram,
        AnalyticsOutputcommand,
    )

    from fastapp.settings import get_settings

    reference = _utcnow()
    today_iso = reference.isoweekday()  # 1=Mon … 7=Sun
    window_start = reference - datetime.timedelta(minutes=IRRIGATION_RUN_WINDOW_MIN)
    dispatch_enabled = get_settings().irrigation_dispatch_enabled
    fired = skipped = 0

    with session_scope(commit=True) as session:
        programs = session.scalars(
            select(AnalyticsIrrigationprogram)
            .where(AnalyticsIrrigationprogram.enabled.is_(True))
            .order_by(AnalyticsIrrigationprogram.id)
        ).all()
        for program in programs:
            # Weekday filter (empty weekdays = every day).
            wd = (program.weekdays or "").strip()
            if wd:
                allowed = {int(d) for d in wd.split(",") if d.strip().isdigit()}
                if today_iso not in allowed:
                    continue

            # Is start_time within the just-passed window?
            start_dt = datetime.datetime.combine(
                reference.date(), program.start_time, tzinfo=reference.tzinfo
            )
            if not (window_start <= start_dt <= reference):
                continue

            # Per-window dedup: skip if already fired since the window opened.
            if program.last_run_at and program.last_run_at >= window_start:
                skipped += 1
                continue

            # Atomic claim: last_run_at never/expired → this tick wins.
            claimed = session.execute(
                update(AnalyticsIrrigationprogram)
                .where(
                    AnalyticsIrrigationprogram.id == program.id,
                    AnalyticsIrrigationprogram.last_run_at.is_(None),
                )
                .values(last_run_at=reference)
            ).rowcount
            if not claimed:
                claimed = session.execute(
                    update(AnalyticsIrrigationprogram)
                    .where(
                        AnalyticsIrrigationprogram.id == program.id,
                        AnalyticsIrrigationprogram.last_run_at < window_start,
                    )
                    .values(last_run_at=reference)
                ).rowcount
            if not claimed:
                skipped += 1
                continue

            cmd = AnalyticsOutputcommand(
                user_id=program.user_id,
                zone_id=program.zone_id,
                action="open",
                source="scheduled",
                status="pending",
                detail=f"Programme « {program.name} »",
                created_at=reference,
            )
            session.add(cmd)
            session.flush()
            # Dispatch (simulated unless enabled) — port of dispatch_command.
            try:
                if dispatch_enabled:
                    raise NotImplementedError(
                        "IRRIGATION_DISPATCH_ENABLED is on but no downlink backend "
                        "is wired."
                    )
                cmd.status = "simulated"
                cmd.detail = _SIMULATED_DETAIL
                cmd.dispatched_at = _utcnow()
                fired += 1
            except Exception:
                cmd.status = "failed"
                skipped += 1
                logger.exception(
                    "irrigation: dispatch failed for program %s", program.id
                )

    return {"fired": fired, "skipped": skipped}
