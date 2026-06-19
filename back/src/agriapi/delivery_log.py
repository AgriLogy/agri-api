"""Best-effort notification-delivery recording.

``record_delivery`` writes a :class:`NotificationDeliveryLog` row for one
outbound notification attempt (email / SMS / WhatsApp). Like
``apps.irrigation.audit.record_audit`` it is deliberately fail-soft: a logging
failure must never break the host task, so every error is swallowed. The table
is unmanaged (self-deployed on prod via scripts/ensure_monitoring_tables.py).
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def record_delivery(
    *,
    channel: str,
    kind: str = "",
    recipient: str = "",
    user: Any = None,
    status: str = "sent",
    error: str = "",
) -> None:
    """Record one delivery attempt. Never raises."""
    try:
        from apps.irrigation.models import NotificationDeliveryLog

        NotificationDeliveryLog.objects.create(
            channel=(channel or "")[:16],
            kind=(kind or "")[:24],
            recipient=(recipient or "")[:255],
            user=user if getattr(user, "pk", None) else None,
            status=(status or "")[:12],
            error=(error or "")[:2000],
        )
    except Exception as exc:  # pragma: no cover - fail-soft
        log.warning("delivery record failed for %s/%s: %s", channel, kind, exc)
