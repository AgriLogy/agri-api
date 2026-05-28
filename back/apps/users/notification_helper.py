"""Notification helpers — Django adapter around ``agri.core.notifications``.

Domain math (ET0, VPD, irrigation decisions, sensor aggregations) lives
in ``agri.core.agronomy``. The French email body composition lives in
``agri.core.notifications``. This module only:

  - Decides whether a user is due for a notification (``should_notify``).
  - Picks ``user_name`` from the Django profile and calls the agri-core
    renderer (``perform_calculations``).

If you're an agronomist and want to change *what* gets sent, edit
``agri.core.agronomy.field_snapshot``. If you want to change *how* it's
phrased, edit ``agri.core.notifications.compose_notification_email``.
"""
from __future__ import annotations

from django.utils.timezone import now

from agri.core.notifications import compose_notification_email
from agriBack.agronomy import field_snapshot


def should_notify(user) -> bool:
    """True when the user's cadence (notify_every hours) has elapsed."""
    if not getattr(user, "last_notified", None):
        return True
    elapsed = now() - user.last_notified
    return elapsed.total_seconds() >= getattr(user, "notify_every", 4) * 3600


def _format_message(user, snapshot: dict) -> str:
    """Backward-compat shim for callers that previously imported this from
    here (notably ``analytics/tests/test_notification_helper.py``). New code
    should call ``compose_notification_email`` directly.
    """
    user_name = user.firstname or user.username
    return compose_notification_email(user_name, snapshot)


def perform_calculations(user) -> str:
    """Build the French notification email body for ``user``."""
    return _format_message(user, field_snapshot(user))
