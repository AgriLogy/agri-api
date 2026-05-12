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

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from math import acos, cos, exp, log, pi, radians, sin, sqrt, tan
from typing import Any
from zoneinfo import ZoneInfo

from django.db.models import Avg
from django.utils import timezone

# ----- 1. Physical constants ------------------------------------------------

ALBEDO_SHORT_CROP = 0.23  # FAO-56 reference grass
# FAO-56 Stefan-Boltzmann constant in HOURLY MJ units (Annex 4, eq. 39).
# The legacy code in agriBack.tasks used 4.903e-9 here, which is the DAILY
# value — that overestimated longwave radiation by 24×, drove Rn negative,
# and silently clamped ET0 to 0 even at noon. Tests now guard against
# regressing to the daily value.
SIGMA = 2.043e-10  # MJ K^-4 m^-2 h^-1

# Default crop coefficient when none is configured for the zone.
# TODO(expert): replace with a per-crop, per-stage Kc lookup.
DEFAULT_KC = 1.0

# Default critical soil-moisture threshold (%) below which irrigation is
# recommended. TODO(expert): pull from per-zone configuration.
DEFAULT_CRITICAL_SOIL_MOISTURE_PCT = 20.0

# FAO-56 solar constant for the hourly Ra computation (eq. 28).
SOLAR_CONSTANT_MJ_M2_MIN = 0.0820

# Standard meridian for the deployment time zone, in degrees east-positive.
# Morocco is permanent UTC+1 since 2018 -> Lst = 15 deg E. Exposed as a
# kwarg on the helpers below so a future operator can override it per site.
LST_DEG_MOROCCO = 15.0
DEPLOYMENT_LOCAL_TZ = ZoneInfo("Africa/Casablanca")

# Per the 2026-05-10 agronomist review:
#   "il faudra s'assurer que le rapport Rs/Rso est borne entre 0.3 et 1.0,
#    avec une valeur minimale de 0.3 la nuit, afin d'eviter un facteur de
#    nebulosite negatif."
CLOUD_RATIO_MIN = 0.3
CLOUD_RATIO_MAX = 1.0

# Defensive floor on the cloudiness function 1.35*ratio - 0.35. At the
# 0.3 ratio floor this evaluates to 0.055; the explicit MIN guards future
# callers that pass a non-physical ratio.
CLOUD_FACTOR_MIN = 0.05


# ----- 2. Pure math helpers (FAO-56 hourly) --------------------------------


def saturation_vapor_pressure_kpa(temp_c: float) -> float:
    """es(T) — Tetens, FAO-56 eq. 11."""
    return 0.6108 * exp((17.27 * temp_c) / (temp_c + 237.3))


def slope_svp_kpa_per_c(temp_c: float) -> float:
    """Δ — slope of the saturation vapor-pressure curve (FAO-56 eq. 13)."""
    es = saturation_vapor_pressure_kpa(temp_c)
    return 4098.0 * es / ((temp_c + 237.3) ** 2)


def psychrometric_constant_kpa_per_c(pressure_kpa: float) -> float:
    """γ ≈ 0.000665 · P, FAO-56 eq. 8 (λ ≈ 2.45 MJ/kg)."""
    return 0.000665 * pressure_kpa


def wperm2_to_mjm2_per_hour(wperm2: float) -> float:
    """Mean W/m² → hourly MJ/m²/h."""
    return wperm2 * 0.0036


def actual_vapor_pressure_kpa(temp_c: float, rh_pct: float) -> float:
    """
    ea = es(T) * RH/100, with RH clamped to [1, 100] %.

    The lower clamp at 1% is the agronomist's explicit ask (review
    2026-05-10): a sensor that reports 0% RH is physically impossible and
    drives ea = 0, which inflates VPD and ET0. 1% keeps the formula in
    the valid range without claiming a perfectly dry atmosphere.
    """
    rh = max(1.0, min(100.0, rh_pct))
    return saturation_vapor_pressure_kpa(temp_c) * (rh / 100.0)


def wind_speed_at_2m(u_z_ms: float, sensor_height_m: float = 2.0) -> float:
    """
    Project a measured wind speed at height ``sensor_height_m`` down to
    2 m. FAO-56 eq. 47. If the sensor is already at 2 m the formula
    reduces to identity, so we short-circuit to avoid floating-point noise.
    """
    if sensor_height_m == 2.0:
        return max(0.0, u_z_ms)
    return max(0.0, u_z_ms * 4.87 / log(67.8 * sensor_height_m - 5.42))


# ----- Solar geometry (FAO-56 Annex 2) --------------------------------------


def equation_of_time_minutes(day_of_year: int) -> float:
    """EoT in minutes (Spencer, FAO-56 Annex 2)."""
    B = 2.0 * pi * (day_of_year - 81) / 364.0
    return 9.87 * sin(2.0 * B) - 7.53 * cos(B) - 1.5 * sin(B)


def solar_time_correction_hours(
    day_of_year: int,
    lon_deg: float,
    lst_deg: float = LST_DEG_MOROCCO,
) -> float:
    """
    Conversion from local civil time to local solar time, in hours:
        t_solar = t_local + solar_time_correction_hours(...)

    The factor 4 represents the 4 minutes per degree of longitude
    correction; EoT supplies the seasonal correction. FAO-56 writes the
    longitude term as ``4 * (Lst - Lloc)`` but with both longitudes
    POSITIVE WEST OF GREENWICH. This codebase carries longitudes the GIS
    way (east-positive, matching ``CustomUser.longitude``), so the
    equivalent formula is ``4 * (lon_deg - lst_deg)``. We document the
    equivalence so the discrepancy with the agronomist's source text is
    not flagged as a regression in a future review.
    """
    return (4.0 * (lon_deg - lst_deg) + equation_of_time_minutes(day_of_year)) / 60.0


def extraterrestrial_radiation_hourly_mjm2h(
    lat_deg: float,
    lon_deg: float,
    day_of_year: int,
    local_clock_hour: float,
    lst_deg: float = LST_DEG_MOROCCO,
) -> float:
    """
    Hourly extraterrestrial radiation Ra (MJ/m^2/h) for the hour centered
    on ``local_clock_hour``. FAO-56 eq. 28.
    """
    phi = radians(lat_deg)
    declination = 0.409 * sin(2.0 * pi * day_of_year / 365.0 - 1.39)
    dr = 1.0 + 0.033 * cos(2.0 * pi * day_of_year / 365.0)

    # Sunset hour angle; argument clamped because at high latitudes
    # arccos can otherwise be called with > 1 or < -1.
    sunset_arg = max(-1.0, min(1.0, -tan(phi) * tan(declination)))
    omega_s = acos(sunset_arg)

    t_solar = local_clock_hour + solar_time_correction_hours(
        day_of_year, lon_deg, lst_deg
    )
    omega = (pi / 12.0) * (t_solar - 12.0)
    omega1 = omega - pi / 24.0
    omega2 = omega + pi / 24.0

    # The whole hour falls outside the day -> Ra is zero.
    if omega2 <= -omega_s or omega1 >= omega_s:
        return 0.0

    # Partial-night hours: clamp integration bounds to [-ws, ws].
    omega1 = max(omega1, -omega_s)
    omega2 = min(omega2, omega_s)

    return (
        (12.0 * 60.0 / pi)
        * SOLAR_CONSTANT_MJ_M2_MIN
        * dr
        * (
            (omega2 - omega1) * sin(phi) * sin(declination)
            + cos(phi) * cos(declination) * (sin(omega2) - sin(omega1))
        )
    )


def vpd_kpa(temp_c: float, rh_pct: float) -> float:
    """Vapor-pressure deficit, never negative."""
    es = saturation_vapor_pressure_kpa(temp_c)
    ea = actual_vapor_pressure_kpa(temp_c, rh_pct)
    return max(0.0, es - ea)


def cloudiness_ratio(rs_mjm2h: float, rso_mjm2h: float) -> float:
    """
    Rs/Rso clamped to [CLOUD_RATIO_MIN, CLOUD_RATIO_MAX].

    At night Rso ~= 0 so the ratio is undefined: FAO-56 (and the
    agronomist's review) prescribe using the lower bound so the
    cloudiness function ``1.35*ratio - 0.35`` stays positive.
    """
    if rso_mjm2h <= 0.0:
        return CLOUD_RATIO_MIN
    return max(CLOUD_RATIO_MIN, min(CLOUD_RATIO_MAX, rs_mjm2h / rso_mjm2h))


def net_radiation_mjm2h(
    rs_mjm2h: float,
    ea_kpa: float,
    temp_c: float,
    *,
    ra_mjm2h: float | None = None,
    elevation_m: float = 0.0,
) -> float:
    """
    FAO-56 Rn for an hourly slot. When extraterrestrial radiation Ra is
    provided the cloudiness ratio Rs/Rso uses the agronomist-mandated
    [0.3, 1.0] clamp with a 0.3 floor at night; otherwise we fall back
    to a 0.75 heuristic in the FAO-56 valid range.
    """
    rns = (1 - ALBEDO_SHORT_CROP) * rs_mjm2h
    tk = temp_c + 273.16
    emiss_term = 0.34 - 0.14 * sqrt(max(0.0, ea_kpa))

    if ra_mjm2h is not None and ra_mjm2h > 0:
        rso = (0.75 + 2e-5 * elevation_m) * ra_mjm2h
        rs_over_rso = cloudiness_ratio(rs_mjm2h, rso)
    else:
        rs_over_rso = 0.75  # heuristic - mid-band of the FAO valid range

    cloud_term = max(CLOUD_FACTOR_MIN, 1.35 * rs_over_rso - 0.35)
    rnl = SIGMA * (tk**4) * emiss_term * cloud_term
    return rns - rnl


def soil_heat_flux_mjm2h(rn_mjm2h: float, *, daytime: bool) -> float:
    """ASCE/FAO hourly G ~= 0.1*Rn daytime, 0.5*Rn nighttime."""
    return 0.1 * rn_mjm2h if daytime else 0.5 * rn_mjm2h


def asce_hourly_short_crop_coeffs(daytime: bool) -> tuple[float, float]:
    """ASCE standardized hourly reference (short crop): (Cn, Cd)."""
    return (37.0, 0.24 if daytime else 0.96)


def is_daytime(rs_mjm2h: float) -> bool:
    """
    Daytime when there is incoming shortwave radiation, per the
    agronomist's review. Using Rs (not Rn) avoids flipping the regime on
    cool humid days where the longwave overshoot briefly drives Rn < 0.
    """
    return rs_mjm2h > 0.0


def penman_monteith_hourly_mm(
    *,
    temp_c: float,
    rh_pct: float,
    wind_ms: float,
    pressure_kpa: float,
    rs_mjm2h: float,
    ra_mjm2h: float | None = None,
    elevation_m: float = 0.0,
    wind_height_m: float = 2.0,
) -> dict[str, float]:
    """
    Compute one hour of FAO-56 Penman-Monteith ET0 (mm/h) and VPD.

    Returns a dict with the inputs derived along the way so callers /
    tests can inspect intermediate quantities.

    Pass ``ra_mjm2h`` (computed via ``extraterrestrial_radiation_hourly_mjm2h``)
    whenever lat/lon are known so the cloudiness ratio Rs/Rso uses the
    correct denominator. With ``ra_mjm2h=None`` the function falls back
    to the 0.75 heuristic from FAO-56.
    """
    es = saturation_vapor_pressure_kpa(temp_c)
    ea = actual_vapor_pressure_kpa(temp_c, rh_pct)
    delta = slope_svp_kpa_per_c(temp_c)
    gamma = psychrometric_constant_kpa_per_c(pressure_kpa)

    rn = net_radiation_mjm2h(
        rs_mjm2h, ea, temp_c, ra_mjm2h=ra_mjm2h, elevation_m=elevation_m
    )
    daytime = is_daytime(rs_mjm2h)
    g = soil_heat_flux_mjm2h(rn, daytime=daytime)
    cn, cd = asce_hourly_short_crop_coeffs(daytime)

    u2 = wind_speed_at_2m(wind_ms, wind_height_m)
    numerator = 0.408 * delta * (rn - g) + gamma * (cn / (temp_c + 273.0)) * u2 * (
        es - ea
    )
    denominator = delta + gamma * (1.0 + cd * u2)
    et0_mm_per_h = max(0.0, numerator / max(denominator, 1e-6))

    return {
        "et0_mm_per_h": et0_mm_per_h,
        "vpd_kpa": max(0.0, es - ea),
        "delta": delta,
        "gamma": gamma,
        "rn_mjm2h": rn,
        "g_mjm2h": g,
        "daytime": daytime,
        "u2_ms": u2,
    }


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


@dataclass
class ZoneEt0:
    """Per-zone ET0 / VPD result of compute_et0_for_zone()."""

    zone_id: int
    user_id: int
    timestamp: datetime
    et0_mm_per_h: float
    vpd_kpa: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "zone_id": self.zone_id,
            "user_id": self.user_id,
            "timestamp": self.timestamp.isoformat(),
            "et0_mm_per_h": self.et0_mm_per_h,
            "vpd_kpa": self.vpd_kpa,
        }


def compute_et0_for_zone(zone, *, end: datetime | None = None) -> ZoneEt0 | None:
    """
    Compute one hour of ET0 + VPD for a zone using the previous full hour
    of sensor data. Pure (no DB writes) - the persistence layer lives in
    ``agriBack.tasks.compute_et0_vpd_hourly``.

    Returns None if any required input is missing for the slot.

    If ``zone.user.latitude`` and ``zone.user.longitude`` are set, the
    extraterrestrial radiation Ra is computed for the hour window using
    the deployment-local civil time. With lat/lon missing we fall back
    to the 0.75 heuristic so legacy zones still produce a value.
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

    inputs = {
        "temp_c": _avg(TemperatureWeather, zone=zone, start=start, end=end),
        "rh_pct": _avg(HumidityWeather, zone=zone, start=start, end=end),
        "wind_ms": _avg(WindSpeed, zone=zone, start=start, end=end),
        "rs_wm2": _avg(SolarRadiation, zone=zone, start=start, end=end),
        "pressure_hpa": _avg(PressureWeather, zone=zone, start=start, end=end),
    }
    if any(v is None for v in inputs.values()):
        return None

    lat_deg = getattr(zone.user, "latitude", None)
    lon_deg = getattr(zone.user, "longitude", None)
    ra_mjm2h: float | None = None
    if lat_deg is not None and lon_deg is not None:
        midpoint_local = (start + (end - start) / 2).astimezone(DEPLOYMENT_LOCAL_TZ)
        ra_mjm2h = extraterrestrial_radiation_hourly_mjm2h(
            lat_deg=lat_deg,
            lon_deg=lon_deg,
            day_of_year=midpoint_local.timetuple().tm_yday,
            local_clock_hour=midpoint_local.hour + midpoint_local.minute / 60.0,
        )

    result = penman_monteith_hourly_mm(
        temp_c=inputs["temp_c"],
        rh_pct=inputs["rh_pct"],
        wind_ms=inputs["wind_ms"],
        pressure_kpa=inputs["pressure_hpa"] * 0.1,  # hPa -> kPa
        rs_mjm2h=wperm2_to_mjm2_per_hour(inputs["rs_wm2"]),
        ra_mjm2h=ra_mjm2h,
    )

    return ZoneEt0(
        zone_id=zone.id,
        user_id=zone.user_id,
        timestamp=end,
        et0_mm_per_h=result["et0_mm_per_h"],
        vpd_kpa=result["vpd_kpa"],
    )


def _format_decision(et0_kc_mm: float | None, soil_moisture_pct: float | None) -> str:
    """
    Translate the ET0×Kc figure and current soil moisture into a one-line
    recommendation in French.

    TODO(expert): replace the threshold-based rule below with a proper
    irrigation model (water balance, root-zone depletion, RAW/TAW, …).
    """
    if et0_kc_mm is None or soil_moisture_pct is None:
        return "Données insuffisantes — recommandation indisponible."

    if soil_moisture_pct < DEFAULT_CRITICAL_SOIL_MOISTURE_PCT:
        return (
            f"Irrigation recommandée maintenant — humidité du sol "
            f"{soil_moisture_pct:.0f} % < seuil "
            f"{DEFAULT_CRITICAL_SOIL_MOISTURE_PCT:.0f} %, "
            f"ETo×Kc ≈ {et0_kc_mm:.2f} mm."
        )

    return (
        f"Pas d'irrigation requise — humidité du sol "
        f"{soil_moisture_pct:.0f} % au-dessus du seuil, "
        f"ETo×Kc ≈ {et0_kc_mm:.2f} mm."
    )


def field_snapshot(user) -> dict[str, Any]:
    """
    Return everything the notification email needs to render a status
    update for ``user``. This is the function an agronomy expert is
    expected to evolve over time.

    Output contract — keys the email template depends on:

        zone_name              str | None
        date_today             date
        yesterday_temp_c       float | None
        today_temp_c           float | None
        yesterday_humidity_pct float | None
        today_humidity_pct     float | None
        et0_today_mm           float | None      total mm so far today
        soil_moisture_pct      float | None      latest reading
        soil_temperature_c     float | None
        soil_ph                float | None
        soil_ec                float | None      µS/cm
        soil_salinity          float | None      mg/L proxy
        npk_n / npk_p / npk_k  float | None      mg/kg
        last_irrigation_at     datetime | None
        last_irrigation_l      float | None      litres delivered
        perfect_irrigation_window str            "06:00 – 07:00" (placeholder)
        kc_used                float
        irrigation_decision    str               French sentence
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
        return {
            "zone_name": None,
            "date_today": now.date(),
            "yesterday_temp_c": None,
            "today_temp_c": None,
            "yesterday_humidity_pct": None,
            "today_humidity_pct": None,
            "et0_today_mm": None,
            "soil_moisture_pct": None,
            "soil_temperature_c": None,
            "soil_ph": None,
            "soil_ec": None,
            "soil_salinity": None,
            "npk_n": None,
            "npk_p": None,
            "npk_k": None,
            "last_irrigation_at": None,
            "last_irrigation_l": None,
            "perfect_irrigation_window": "06:00 – 07:00",
            "kc_used": DEFAULT_KC,
            "irrigation_decision": (
                "Aucune zone configurée pour ce compte — créez une zone pour"
                " activer les recommandations."
            ),
        }

    yesterday_temp = _avg(
        TemperatureWeather, zone=zone, start=yesterday_start, end=today_start
    )
    today_temp = _avg(TemperatureWeather, zone=zone, start=today_start, end=today_end)
    yesterday_rh = _avg(
        HumidityWeather, zone=zone, start=yesterday_start, end=today_start
    )
    today_rh = _avg(HumidityWeather, zone=zone, start=today_start, end=today_end)

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

    soil_moisture_pct = sm_row.value if sm_row else None
    kc_used = DEFAULT_KC
    et0_kc_mm = (
        et0_today_mm * kc_used
        if et0_today_mm is not None and kc_used is not None
        else None
    )

    return {
        "zone_name": zone.name,
        "date_today": now.date(),
        "yesterday_temp_c": yesterday_temp,
        "today_temp_c": today_temp,
        "yesterday_humidity_pct": yesterday_rh,
        "today_humidity_pct": today_rh,
        "et0_today_mm": et0_today_mm,
        "soil_moisture_pct": soil_moisture_pct,
        "soil_temperature_c": st_row.value if st_row else None,
        "soil_ph": ph_row.value if ph_row else None,
        "soil_ec": ec_row.value if ec_row else None,
        "soil_salinity": sal_row.value if sal_row else None,
        "npk_n": getattr(npk_row, "nitrogen_value", None),
        "npk_p": getattr(npk_row, "phosphorus_value", None),
        "npk_k": getattr(npk_row, "potassium_value", None),
        "last_irrigation_at": last_flow.timestamp if last_flow else None,
        "last_irrigation_l": (float(last_flow.value) * 1000.0 if last_flow else None),
        # TODO(expert): derive from solar / morning vs. evening windows.
        "perfect_irrigation_window": "06:00 – 07:00",
        "kc_used": kc_used,
        "irrigation_decision": _format_decision(et0_kc_mm, soil_moisture_pct),
    }
