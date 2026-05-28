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

from datetime import datetime, time, timedelta
from typing import Any

from django.db.models import Avg
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


# ----- 3. Sensor aggregation helpers (DB I/O) -------------------------------


def _avg(model, *, zone, start, end) -> float | None:
    """Mean of model.value over [start, end) for the given zone, or None."""
    return model.objects.filter(
        zone=zone, timestamp__gte=start, timestamp__lt=end
    ).aggregate(v=Avg("value"))["v"]


def _latest(model, *, zone, **extra):
    """Most recent row for zone (or with extra filters), or None."""
    qs = model.objects.filter(zone=zone, **extra).order_by("-timestamp")
    return qs.first()


def _day_bounds_local(now: datetime) -> tuple[datetime, datetime]:
    """Today 00:00 and 24:00 in the active timezone, returned as aware UTC."""
    tz = timezone.get_current_timezone()
    local_now = now.astimezone(tz)
    start_local = datetime.combine(local_now.date(), time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return (start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc))


# ----- 4. High-level entry points ------------------------------------------


def compute_et0_for_zone(zone, *, end: datetime | None = None) -> ZoneEt0 | None:
    """Thin Django-side adapter around ``agri.core.agronomy.compute_zone_et0``.

    Fetches the previous full hour of sensor averages for the zone via
    the Django ORM, packs them into an ``Et0Inputs`` DTO, and calls the
    agri-core handler. Returns ``None`` if any required input is missing
    for the slot. The persistence layer (writing the Et0Calculated /
    VPDWeather rows) still lives in ``agriBack.tasks``.
    """
    from analytics.models import (
        HumidityWeather,
        PressureWeather,
        SolarRadiation,
        TemperatureWeather,
        WindSpeed,
    )

    end = (end or timezone.now()).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=1)

    return compute_zone_et0(Et0Inputs(
        zone_id=zone.id,
        user_id=zone.user_id,
        timestamp=end,
        temp_c=_avg(TemperatureWeather, zone=zone, start=start, end=end),
        rh_pct=_avg(HumidityWeather, zone=zone, start=start, end=end),
        wind_ms=_avg(WindSpeed, zone=zone, start=start, end=end),
        rs_wm2=_avg(SolarRadiation, zone=zone, start=start, end=end),
        pressure_hpa=_avg(PressureWeather, zone=zone, start=start, end=end),
        latitude=getattr(zone.user, "latitude", None),
        longitude=getattr(zone.user, "longitude", None),
    ))


def field_snapshot(
    user,
    *,
    dr_today_mm: float | None = None,
    precipitation_forecast_mm: float = 0.0,
) -> dict[str, Any]:
    """Thin Django-side adapter around ``agri.core.agronomy.field_snapshot``.

    Fetches the user's zone + recent sensor aggregates via the Django ORM,
    packs them into the framework-agnostic ``FieldInputs`` DTO, and calls
    the agri-core handler. The returned dict shape is unchanged from the
    pre-lift contract (see ``agri.core.agronomy.field_snapshot``'s
    docstring for the full key list).
    """
    from analytics.models import (
        EcSalinitySensor,
        Et0Calculated,
        HumidityWeather,
        NpkSensor,
        PhSoil,
        SoilMoistureMedium,
        SoilSalinitySensor,
        SoilTemperatureMedium,
        TemperatureWeather,
        WaterFlowSensor,
        Zone,
    )

    now = timezone.now()
    today_start, today_end = _day_bounds_local(now)
    yesterday_start = today_start - timedelta(days=1)

    # TODO(expert): pick the right zone (or iterate) when a user manages
    # multiple. v1 takes the lowest-id zone, mirroring the dashboard default.
    zone = Zone.objects.filter(user=user).order_by("id").first()
    if zone is None:
        return _core_field_snapshot(FieldInputs(
            date_today=now.date(),
            zone=None,
            sensors=None,
            dr_today_mm=dr_today_mm,
            precipitation_forecast_mm=precipitation_forecast_mm,
        ))

    et0_today_rows = Et0Calculated.objects.filter(
        zone=zone, timestamp__gte=today_start, timestamp__lt=today_end
    ).values_list("value", flat=True)
    et0_today_mm = sum(et0_today_rows) if et0_today_rows else None

    sm_row = _latest(SoilMoistureMedium, zone=zone)
    st_row = _latest(SoilTemperatureMedium, zone=zone)
    ph_row = _latest(PhSoil, zone=zone)
    ec_row = _latest(EcSalinitySensor, zone=zone)
    sal_row = _latest(SoilSalinitySensor, zone=zone)
    npk_row = _latest(NpkSensor, zone=zone)
    last_flow = (
        WaterFlowSensor.objects.filter(zone=zone, value__gt=0)
        .order_by("-timestamp")
        .first()
    )

    sensors = SensorAggregates(
        yesterday_temp_c=_avg(
            TemperatureWeather, zone=zone, start=yesterday_start, end=today_start,
        ),
        today_temp_c=_avg(
            TemperatureWeather, zone=zone, start=today_start, end=today_end,
        ),
        yesterday_humidity_pct=_avg(
            HumidityWeather, zone=zone, start=yesterday_start, end=today_start,
        ),
        today_humidity_pct=_avg(
            HumidityWeather, zone=zone, start=today_start, end=today_end,
        ),
        et0_today_mm=et0_today_mm,
        soil_moisture_pct=sm_row.value if sm_row else None,
        soil_temperature_c=st_row.value if st_row else None,
        soil_ph=ph_row.value if ph_row else None,
        soil_ec=ec_row.value if ec_row else None,
        soil_salinity=sal_row.value if sal_row else None,
        npk_n=getattr(npk_row, "nitrogen_value", None),
        npk_p=getattr(npk_row, "phosphorus_value", None),
        npk_k=getattr(npk_row, "potassium_value", None),
        last_irrigation_at=last_flow.timestamp if last_flow else None,
        last_irrigation_l=(
            float(last_flow.value) * 1000.0 if last_flow else None
        ),
    )

    zone_params = ZoneParams(
        name=zone.name,
        area_m2=zone.space,
        raw_mm=getattr(zone, "soil_param_RAW", None),
        taw_mm=getattr(zone, "soil_param_TAW", None),
        pomp_flow_rate_l_per_s=getattr(zone, "pomp_flow_rate", None),
        irrigation_water_quantity_l=getattr(zone, "irrigation_water_quantity", None),
        critical_moisture_pct=getattr(zone, "critical_moisture_threshold", None),
    )

    return _core_field_snapshot(FieldInputs(
        date_today=now.date(),
        zone=zone_params,
        sensors=sensors,
        dr_today_mm=dr_today_mm,
        precipitation_forecast_mm=precipitation_forecast_mm,
    ))
