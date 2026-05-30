"""
Backfill 7 days of realistic sensor history per zone so the dashboard
charts have something to render right after `docker compose up`.

Runs once on web-container boot from docker-entrypoint.sh, gated by
SEED_DEV_DATA (default: true). Safe to re-run — skips zones that
already have meaningful history.

Reuses the season-aware synth helpers from agriapi.tasks
(simulate_sensor_ingest) so the values are bounded the same way the
live simulator produces them. Time-step is 15 minutes which matches
the production cadence and keeps record counts reasonable
(~672 rows / sensor / zone for one week).
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta
from zoneinfo import ZoneInfo

import django

# src/ layout: make back/src/ importable when run outside the container
# (the Docker image sets PYTHONPATH=/code/src; this covers local runs).
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "agriapi.settings")
django.setup()

from django.db import transaction  # noqa: E402
from django.utils import timezone as djtz  # noqa: E402

from agriapi.tasks import (  # noqa: E402
    LOCAL_TZ,
    clamp,
    day_bounds,
    diurnal_heat,
    diurnal_light,
    n,
    season_params,
)
from analytics.models import (  # noqa: E402
    EcSalinitySensor,
    ECSoilHigh,
    ECSoilLow,
    ECSoilMedium,
    ElectricityConsumptionSensor,
    Et0Calculated,
    FruitSizeSensor,
    HumidityWeather,
    LargeFruitDiameterSensor,
    LeafMoistureSensor,
    LeafTemperatureSensor,
    MultiDepthSoilMoistureSensor,
    NpkSensor,
    PhSoil,
    PhWaterSensor,
    PrecipitationRate,
    PressureWeather,
    SoilConductivitySensor,
    SoilMoistureHigh,
    SoilMoistureLow,
    SoilMoistureMedium,
    SoilSalinitySensor,
    SoilTemperatureHigh,
    SoilTemperatureLow,
    SoilTemperatureMedium,
    SolarRadiation,
    TemperatureWeather,
    VPDWeather,
    WaterECSensor,
    WaterFlowSensor,
    WaterLevelSensor,
    WaterPressureSensor,
    WindDirection,
    WindSpeed,
    Zone,
)

# How much history to generate, and at what cadence.
DAYS_BACK = int(os.getenv("SEED_DEV_DATA_DAYS", "7"))
STEP_MINUTES = int(os.getenv("SEED_DEV_DATA_STEP_MIN", "15"))
LOCAL = ZoneInfo("Africa/Casablanca")


def _zone_already_seeded(zone) -> bool:
    """True when the zone already has at least 24 hours of fresh data
    (so we don't blow up the DB on every restart)."""
    cutoff = djtz.now() - timedelta(hours=24)
    return TemperatureWeather.objects.filter(zone=zone, timestamp__gte=cutoff).exists()


def _generate_slot(zone, slot_utc, *, last):
    """One 15-min slot of synthetic readings. `last` is a dict caching
    the previous value per metric so the series stays continuous."""
    local = slot_utc.astimezone(LOCAL_TZ)
    mins_local = local.hour * 60 + local.minute
    month = local.month
    P = season_params(month)
    sunrise, sunset = day_bounds(month)
    light = diurnal_light(mins_local, sunrise, sunset)
    heat = diurnal_heat(mins_local, sunrise, sunset)

    temp_c = clamp(
        P["t_min"] + (P["t_max"] - P["t_min"]) * (0.1 + 0.9 * heat) + n(0, 0.6),
        P["t_min"] - 1.5,
        P["t_max"] + 1.5,
    )
    rh_pct = clamp(
        P["rh_min"] + (P["rh_max"] - P["rh_min"]) * (1.0 - 0.9 * light) + n(0, 3.0),
        20,
        100,
    )
    wind_ms = clamp(
        P["wind_min"] + (P["wind_max"] - P["wind_min"]) * (light**1.4) + n(0, 0.35),
        0.0,
        P["wind_max"] + 1.0,
    )
    solar_wm2 = (
        0.0
        if light == 0.0
        else clamp(P["solar_peak"] * light + n(0, 25), 0, P["solar_peak"] + 80)
    )
    pressure_hpa = clamp(P["p_base"] + n(0, 2.5), 1005, 1025)

    irrigating = (6 * 60 <= mins_local <= 7 * 60) or (18 * 60 <= mins_local <= 19 * 60)
    flow_m3h = clamp(0.0 if not irrigating else 12.0 + n(0, 1.5), 0.0, 25.0)
    press_bar = 0.0 if flow_m3h == 0.0 else clamp(2.5 + n(0, 0.15), 1.6, 3.2)

    sm_low = clamp(last["sm_low"] - 0.03 * (1 - rh_pct / 100) + n(0, 0.15), 5, 60)
    sm_med = clamp(last["sm_med"] - 0.018 * (1 - rh_pct / 100) + n(0, 0.10), 5, 60)
    sm_high = clamp(last["sm_high"] - 0.011 * (1 - rh_pct / 100) + n(0, 0.08), 5, 60)
    if irrigating:
        sm_low += 1.8
        sm_med += 0.7
        sm_high += 0.4

    st_low = clamp(temp_c + 0.2 + light * 2.2 + n(0, 0.3), -5, 50)
    st_med = clamp(temp_c - 0.5 + light * 1.2 + n(0, 0.25), -5, 50)
    st_high = clamp(temp_c - 1.2 + light * 0.6 + n(0, 0.2), -5, 50)
    ph_soil = clamp(last["ph_soil"] + n(0, 0.01), 5.5, 8.5)
    ec_med = clamp(last["ec_med"] + n(0, 0.03), 0.1, 4.5)
    ec_low = clamp(last["ec_low"] + n(0, 0.03), 0.05, 4.0)
    ec_high = clamp(last["ec_high"] + n(0, 0.03), 0.1, 5.0)
    leaf_temp = clamp(temp_c + 3.0 * light + n(0, 0.4), -5, 55)
    leaf_moist = clamp(40 + 50 * (rh_pct / 100) - 30 * light + n(0, 2.5), 20, 100)
    md_sm = (sm_low + sm_med + sm_high) / 3.0
    elec = clamp(0.05 + 0.08 * (light > 0.1) + (0.35 if flow_m3h > 0 else 0), 0.02, 1.0)
    rain = max(0.0, n(0.2, 0.5)) if light < 0.6 else 0.0
    et0 = clamp(0.6 * light + 0.05 * wind_ms + n(0, 0.03), 0.0, 1.5)
    vpd = clamp(0.05 + 1.5 * light * (1 - rh_pct / 100), 0.0, 5.0)

    last.update(
        sm_low=sm_low,
        sm_med=sm_med,
        sm_high=sm_high,
        ph_soil=ph_soil,
        ec_med=ec_med,
        ec_low=ec_low,
        ec_high=ec_high,
    )

    return {
        TemperatureWeather: float(temp_c),
        HumidityWeather: float(rh_pct),
        WindSpeed: float(wind_ms),
        WindDirection: float((mins_local * 0.5 + n(0, 5)) % 360),
        SolarRadiation: float(solar_wm2),
        PressureWeather: float(pressure_hpa),
        PrecipitationRate: float(rain),
        SoilMoistureLow: float(sm_low),
        SoilMoistureMedium: float(sm_med),
        SoilMoistureHigh: float(sm_high),
        SoilTemperatureLow: float(st_low),
        SoilTemperatureMedium: float(st_med),
        SoilTemperatureHigh: float(st_high),
        PhSoil: float(ph_soil),
        ECSoilLow: float(ec_low),
        ECSoilMedium: float(ec_med),
        ECSoilHigh: float(ec_high),
        SoilConductivitySensor: float(ec_med * 600 + n(0, 6)),
        SoilSalinitySensor: float(ec_med * 1200 + n(0, 12)),
        EcSalinitySensor: float(ec_med * 700 + n(0, 10)),
        MultiDepthSoilMoistureSensor: float(md_sm),
        LeafTemperatureSensor: float(leaf_temp),
        LeafMoistureSensor: float(leaf_moist),
        WaterFlowSensor: float(flow_m3h),
        WaterPressureSensor: float(press_bar),
        WaterECSensor: float(clamp(650 + n(0, 60), 200, 2000)),
        PhWaterSensor: float(clamp(7.2 + n(0, 0.05), 6.4, 7.9)),
        WaterLevelSensor: float(150 + n(0, 0.2)),
        ElectricityConsumptionSensor: float(elec),
        Et0Calculated: float(et0),
        VPDWeather: float(vpd),
        # Plant sensors trend up slowly
        FruitSizeSensor: float(10.0 + 0.001 * mins_local),
        LargeFruitDiameterSensor: float(15.0 + 0.0015 * mins_local),
    }


def _seed_zone(zone) -> int:
    if _zone_already_seeded(zone):
        print(
            f"[seed-data] zone {zone.id} '{zone.name}' already has recent data — skipping."
        )
        return 0

    end = djtz.now().replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=DAYS_BACK)
    step = timedelta(minutes=STEP_MINUTES)
    user = zone.user

    last = {
        "sm_low": 27.0,
        "sm_med": 25.0,
        "sm_high": 22.0,
        "ph_soil": 7.0,
        "ec_med": 1.4,
        "ec_low": 1.1,
        "ec_high": 1.6,
    }

    inserts: dict[type, list] = {}
    npk_inserts: list[NpkSensor] = []

    slot = start
    while slot < end:
        readings = _generate_slot(zone, slot, last=last)
        for model, value in readings.items():
            inserts.setdefault(model, []).append(
                model(zone=zone, user=user, value=value, timestamp=slot)
            )
        npk_inserts.append(
            NpkSensor(
                zone=zone,
                user=user,
                timestamp=slot,
                nitrogen_value=clamp(50 + n(0, 1.5), 20, 120),
                phosphorus_value=clamp(25 + n(0, 1.0), 5, 80),
                potassium_value=clamp(40 + n(0, 1.5), 10, 150),
            )
        )
        slot += step

    rows = 0
    with transaction.atomic():
        for model, batch in inserts.items():
            model.objects.bulk_create(batch, batch_size=500, ignore_conflicts=True)
            rows += len(batch)
        NpkSensor.objects.bulk_create(
            npk_inserts, batch_size=500, ignore_conflicts=True
        )
        rows += len(npk_inserts)
    print(
        f"[seed-data] zone {zone.id} '{zone.name}': inserted {rows} rows "
        f"({DAYS_BACK} days, every {STEP_MINUTES} min)."
    )
    return rows


def main() -> int:
    if os.getenv("SEED_DEV_DATA", "true").lower() not in ("1", "true", "yes"):
        print("[seed-data] SEED_DEV_DATA disabled — skipping.")
        return 0

    zones = list(Zone.objects.select_related("user"))
    if not zones:
        print("[seed-data] no zones — nothing to seed.")
        return 0

    total = 0
    for zone in zones:
        total += _seed_zone(zone)
    print(f"[seed-data] done — {total} rows inserted across {len(zones)} zones.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
