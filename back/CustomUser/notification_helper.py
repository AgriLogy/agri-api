"""
Notification helpers — gating + email body composition.

Domain math (ET0, VPD, irrigation decisions, sensor aggregations) lives in
``agriBack.agronomy``. This module only:
  - decides whether a user is due for a notification (`should_notify`);
  - turns a `field_snapshot` dict into a French email body
    (`perform_calculations`).

If you're an agronomist and want to change *what* gets sent, edit
``agriBack.agronomy.field_snapshot``. If you want to change *how* it's
phrased, edit `_format_message` here.
"""

from __future__ import annotations

from typing import Any

from django.utils.timezone import now

from agriBack.agronomy import field_snapshot


def should_notify(user) -> bool:
    """True when the user's cadence (notify_every hours) has elapsed."""
    if not getattr(user, "last_notified", None):
        return True
    elapsed = now() - user.last_notified
    return elapsed.total_seconds() >= getattr(user, "notify_every", 4) * 3600


def _fmt(value: Any, suffix: str = "", precision: int = 1) -> str:
    """Render numeric values; render '—' when missing."""
    if value is None:
        return "—"
    if isinstance(value, (int, float)):
        return f"{value:.{precision}f}{suffix}"
    return f"{value}{suffix}"


def _format_message(user, snapshot: dict[str, Any]) -> str:
    user_name = user.firstname or user.username
    zone_label = snapshot["zone_name"] or "votre zone"

    last_irrig_at = snapshot.get("last_irrigation_at")
    last_irrig_str = (
        last_irrig_at.strftime("%d/%m/%Y %H:%M") if last_irrig_at else "—"
    )
    last_irrig_volume = (
        f"{snapshot['last_irrigation_l']:.0f} L"
        if snapshot.get("last_irrigation_l") is not None
        else "—"
    )

    npk = (
        f"{_fmt(snapshot['npk_n'], precision=0)}/"
        f"{_fmt(snapshot['npk_p'], precision=0)}/"
        f"{_fmt(snapshot['npk_k'], precision=0)} mg/kg"
    )

    return f"""\
Bonjour {user_name},

Voici le rapport quotidien pour {zone_label} — {snapshot["date_today"].strftime("%d/%m/%Y")}.

Prévisions / météo (derniers 2 jours) :
🌡 Température moyenne — hier : {_fmt(snapshot["yesterday_temp_c"], "°C")} ; aujourd'hui : {_fmt(snapshot["today_temp_c"], "°C")}
💧 Humidité de l'air moyenne — hier : {_fmt(snapshot["yesterday_humidity_pct"], "%", precision=0)} ; aujourd'hui : {_fmt(snapshot["today_humidity_pct"], "%", precision=0)}
🌞 ET0 cumulée aujourd'hui : {_fmt(snapshot["et0_today_mm"], " mm", precision=2)}  (Kc utilisé : {snapshot["kc_used"]:.2f})

Dernière irrigation enregistrée :
🚰 {last_irrig_str} — volume : {last_irrig_volume}

État actuel du sol :
🌱 Humidité (couche moyenne) : {_fmt(snapshot["soil_moisture_pct"], "%", precision=0)}
🌡 Température du sol : {_fmt(snapshot["soil_temperature_c"], "°C")}
⚖️ pH : {_fmt(snapshot["soil_ph"], precision=2)}
⚡ Conductivité (EC) : {_fmt(snapshot["soil_ec"], " µS/cm", precision=0)}
🧂 Salinité (proxy) : {_fmt(snapshot["soil_salinity"], " mg/L", precision=0)}
🌿 N-P-K : {npk}

Recommandation pour aujourd'hui :
{snapshot["irrigation_decision"]}
Fenêtre d'irrigation suggérée : {snapshot["perfect_irrigation_window"]}
"""


def perform_calculations(user) -> str:
    """Build the French notification email body for ``user``."""
    snapshot = field_snapshot(user)
    return _format_message(user, snapshot)
