"""Output-device dispatch seam (valves / pumps).

SAFETY: physical actuation is gated by ``settings.IRRIGATION_DISPATCH_ENABLED``.
When disabled (the default), a command is recorded as ``simulated`` and only
LOGGED — it never reaches hardware. The real downlink backend (ChirpStack
device-queue / Bivocom command) is a deliberate TODO seam to be wired and
hardware-tested before the flag is turned on.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.utils.timezone import now

logger = logging.getLogger(__name__)


def dispatch_command(command):
    """Dispatch (or simulate) an OutputCommand, mutating + saving its status.

    Returns the command. Never actuates hardware while
    IRRIGATION_DISPATCH_ENABLED is False (the default).
    """
    from analytics.models import OutputCommand

    if not getattr(settings, "IRRIGATION_DISPATCH_ENABLED", False):
        command.status = OutputCommand.SIMULATED
        command.detail = "Simulation mode — no hardware actuated."
        command.dispatched_at = now()
        command.save(update_fields=["status", "detail", "dispatched_at"])
        logger.info(
            "irrigation dispatch (SIMULATED): %s zone=%s cmd=%s",
            command.action,
            command.zone_id,
            command.pk,
        )
        return command

    # Real actuation path — intentionally NOT implemented. Enabling the flag
    # without wiring this raises loudly rather than silently faking a send.
    raise NotImplementedError(
        "IRRIGATION_DISPATCH_ENABLED is on but no downlink backend is wired. "
        "Implement the ChirpStack/Bivocom command path (and hardware-test it) "
        "before enabling physical irrigation dispatch."
    )
