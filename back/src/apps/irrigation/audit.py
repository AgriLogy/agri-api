"""Best-effort admin audit recording.

``record_audit`` writes an :class:`AuditEvent` row for an admin mutation. It is
deliberately fail-soft: a logging failure must never break the host endpoint,
so every error is swallowed. The table is unmanaged (self-deployed on prod via
scripts/ensure_admin_tables.py).
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def record_audit(
    actor,
    action: str,
    target_type: str = "",
    target_id: Any = "",
    changes: dict | None = None,
) -> None:
    """Record an admin action. Never raises."""
    try:
        from apps.irrigation.models import AuditEvent

        AuditEvent.objects.create(
            actor=actor if getattr(actor, "pk", None) else None,
            action=action[:64],
            target_type=(target_type or "")[:64],
            target_id=str(target_id or "")[:64],
            changes=changes or {},
        )
    except Exception as exc:  # pragma: no cover - fail-soft
        log.warning("audit record failed for %s: %s", action, exc)
