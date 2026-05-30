"""
Single source of truth for irrigation / agronomy math.

This module is intentionally the ONLY place where the project does FAO-56
Penman-Monteith ET0 math, derives a per-user "field snapshot" used by the
notification email, or makes an irrigation recommendation.

The aim is that an agronomy expert can later replace any function below
WITHOUT touching the rest of the codebase, as long as they keep the
return shape of `field_snapshot(user)` stable. Anything marked
``TODO(expert)`` is a known stub waiting for domain expertise.

Layout:

    1. Physical constants
    2. Pure math helpers          (FAO-56 hourly, no DB I/O — testable)
    3. Sensor aggregation helpers (DB queries, no math beyond Avg/sum)
    4. High-level entry points    (compute_et0_for_zone, field_snapshot)

Engineering notes:
- All ET0 figures are mm of equivalent water depth.
- All temperatures are °C, humidity is % RH, pressure is kPa unless
  otherwise noted in the parameter name.
- ``compute_et0_for_zone`` is PURE (no writes). The Celery task in
  ``agriBack.tasks`` is responsible for persisting Et0Calculated /
  VPDWeather rows.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.utils import timezone

# Re-exports from agri-core. All the FAO-56 constants, hourly math, the
# Dr/RAW irrigation decision, and both handlers live in
# `agri.core.agronomy` (per memory `project_agri_core_architecture`).
# Re-exported here so legacy imports like
# `from agriBack.agronomy import irrigation_decision_dr` keep working
# until a Phase 6 follow-up migrates each callsite.
from agri.core.agronomy import (
    ALBEDO_SHORT_CROP,
    CLOUD_FACTOR_MIN,
    CLOUD_RATIO_MAX,
    CLOUD_RATIO_MIN,
    CROP_STAGE_PROFILES,
    DEFAULT_CANOPY_DENSITY_KR,
    DEFAULT_CRITICAL_SOIL_MOISTURE_PCT,
    DEFAULT_DURATION_SPLIT_THRESHOLD_HR,
    DEFAULT_IRRIGATION_EFFICIENCY,
    DEFAULT_KC,
    DEFAULT_RAINFALL_EFFICIENCY,
    DEPLOYMENT_LOCAL_TZ,
    LST_DEG_MOROCCO,
    RAIN_FORECAST_TRIGGER_MM,
    SIGMA,
    SOLAR_CONSTANT_MJ_M2_MIN,
    Et0Inputs,
    FieldInputs,
    IrrigationDecision,
    SensorAggregates,
    ZoneEt0,
    ZoneParams,
    actual_vapor_pressure_kpa,
    asce_hourly_short_crop_coeffs,
    cloudiness_ratio,
    compute_zone_et0,
    cumulative_dr_after_missed_days,
    effective_rainfall_mm,
    equation_of_time_minutes,
    etc_mm,
    extraterrestrial_radiation_hourly_mjm2h,
    irrigation_decision_dr,
    is_daytime,
    net_radiation_mjm2h,
    penman_monteith_hourly_mm,
    psychrometric_constant_kpa_per_c,
    saturation_vapor_pressure_kpa,
    slope_svp_kpa_per_c,
    soil_heat_flux_mjm2h,
    solar_time_correction_hours,
    update_daily_depletion,
    vpd_kpa,
    wind_speed_at_2m,
    wperm2_to_mjm2_per_hour,
)
from agri.core.agronomy import field_snapshot as _core_field_snapshot


# ----- 2. Pure math helpers (FAO-56 hourly) --------------------------------
#
# Every helper (saturation_vapor_pressure_kpa, slope_svp_kpa_per_c,
# psychrometric_constant_kpa_per_c, wperm2_to_mjm2_per_hour,
# actual_vapor_pressure_kpa, wind_speed_at_2m, equation_of_time_minutes,
# solar_time_correction_hours, extraterrestrial_radiation_hourly_mjm2h,
# vpd_kpa, cloudiness_ratio, net_radiation_mjm2h, soil_heat_flux_mjm2h,
# asce_hourly_short_crop_coeffs, is_daytime, penman_monteith_hourly_mm)
# now lives in `agri.core.agronomy`; see the re-export block above.


# ----- 2b. Water-balance & irrigation decision -----------------------------
#
# Pure math + the Dr/RAW decision (effective_rainfall_mm, etc_mm,
# update_daily_depletion, cumulative_dr_after_missed_days,
# IrrigationDecision, irrigation_decision_dr) now live in agri-core; see
# the re-export block above.


# ----- 4. High-level entry points ------------------------------------------


def compute_et0_for_zone(zone, *, end: datetime | None = None) -> ZoneEt0 | None:
    """Thin adapter: delegate to agri-core's DB-backed ``compute_et0_for_zone``.

    Opens an ``agri.core.database`` SQLAlchemy session (its own engine on the
    same Postgres, via ``AGRI_DB_URL``) and lets agri-core fetch the previous
    full hour of weather averages for the zone and run the FAO-56 math. The
    Django-ORM fetch that used to live here now lives in agri-core. Returns
    ``None`` when a required input is missing for the slot. The persistence
    layer (writing Et0Calculated / VPDWeather rows) still lives in
    ``agriBack.tasks``.
    """
    from agri.core.agronomy import compute_et0_for_zone as _core_compute_et0
    from agri.core.database import session_scope

    with session_scope() as session:
        return _core_compute_et0(session, zone.id, end=end)


def field_snapshot(
    user,
    *,
    dr_today_mm: float | None = None,
    precipitation_forecast_mm: float = 0.0,
) -> dict[str, Any]:
    """Thin adapter: delegate to agri-core's DB-backed ``field_snapshot_for_user``.

    Opens an ``agri.core.database`` SQLAlchemy session (its own engine on the
    same Postgres, via ``AGRI_DB_URL``) and lets agri-core do the fetch +
    compute. The Django-ORM fetch that used to live here now lives in
    agri-core; the returned dict shape is unchanged.
    """
    from agri.core.agronomy import field_snapshot_for_user
    from agri.core.database import session_scope

    with session_scope() as session:
        return field_snapshot_for_user(
            session,
            user.id,
            now=timezone.now(),
            dr_today_mm=dr_today_mm,
            precipitation_forecast_mm=precipitation_forecast_mm,
        )
