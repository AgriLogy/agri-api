from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import get_connection, send_mail
from django.utils.timezone import now

from CustomUser.notification_helper import perform_calculations, should_notify

logger = logging.getLogger(__name__)


@shared_task
def send_periodic_notifications():
    """
    For every active user with an email, dispatch a personalised
    field-status notification when their cadence (notify_every hours)
    has elapsed since last_notified. Per-user failures don't abort the
    batch.
    """
    User = get_user_model()
    sent = 0
    skipped = 0
    failed = 0

    with get_connection() as connection:
        logger.info("Email connection opened at %s", now())

        for user in User.objects.filter(is_active=True).iterator(chunk_size=200):
            recipient = (getattr(user, "email", "") or "").strip()
            if not recipient:
                skipped += 1
                continue
            if not should_notify(user):
                skipped += 1
                continue

            try:
                message = perform_calculations(user)
                send_mail(
                    subject="Mise à jour de votre terrain agricole",
                    message=message,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[recipient],
                    connection=connection,
                    fail_silently=False,
                )
                user.last_notified = now()
                user.save(update_fields=["last_notified"])
                sent += 1
            except Exception:
                failed += 1
                logger.exception(
                    "Failed to send periodic notification to %s", recipient
                )

    logger.info(
        "Periodic notifications: sent=%d, skipped=%d, failed=%d",
        sent,
        skipped,
        failed,
    )
    return {"sent": sent, "skipped": skipped, "failed": failed}


######################################################################################################
######################   ET0 / VPD persistence (math lives in agriBack.agronomy)   ###################
######################################################################################################

from analytics.models import Et0Calculated, VPDWeather, Zone
from celery import shared_task
from django.db import transaction

from agriBack.agronomy import compute_et0_for_zone


@shared_task
def compute_et0_vpd_hourly():
    """
    For each Zone, ask agriBack.agronomy for one hour of ET0 + VPD and
    persist the result. All physics lives in that module.
    """
    zones = Zone.objects.all().select_related("user")
    et0_records: list[Et0Calculated] = []
    vpd_records: list[VPDWeather] = []

    for z in zones:
        result = compute_et0_for_zone(z)
        if result is None:
            continue
        et0_records.append(
            Et0Calculated(
                zone=z,
                user=z.user,
                value=result.et0_mm_per_h,
                timestamp=result.timestamp,
            )
        )
        vpd_records.append(
            VPDWeather(
                zone=z,
                user=z.user,
                value=result.vpd_kpa,
                timestamp=result.timestamp,
            )
        )

    with transaction.atomic():
        if et0_records:
            Et0Calculated.objects.bulk_create(et0_records, batch_size=500)
        if vpd_records:
            VPDWeather.objects.bulk_create(vpd_records, batch_size=500)

    return {
        "zones_processed": len(zones),
        "et0_rows": len(et0_records),
        "vpd_rows": len(vpd_records),
    }


######################################################################################################
###################   Lakher : Data insertion simulation   ###########################################
######################################################################################################

import math
import random
from datetime import timedelta
from zoneinfo import ZoneInfo

from analytics.models import (  # Weather; Soil profile; Plant; Water; Power; Nutrients; Optional combo
    ActiveGraph,
    EcSalinitySensor,
    ECSoilHigh,
    ECSoilLow,
    ECSoilMedium,
    ElectricityConsumptionSensor,
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
    WaterECSensor,
    WaterFlowSensor,
    WaterLevelSensor,
    WaterPressureSensor,
    WindDirection,
    WindSpeed,
    Zone,
)
from celery import shared_task
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

LOCAL_TZ = ZoneInfo("Africa/Casablanca")
User = get_user_model()


def clamp(v, lo, hi):
    return hi if v > hi else lo if v < lo else v


def n(mu=0.0, sigma=1.0):
    return random.gauss(mu, sigma)


def season_params(month: int):
    # Morocco-ish bands; tune per site
    if month in (12, 1, 2):  # winter
        return dict(
            t_min=6,
            t_max=19,
            rh_min=55,
            rh_max=95,
            solar_peak=650,
            wind_min=0.3,
            wind_max=3.0,
            p_base=1016,
            rain_p=0.17,
        )
    if month in (3, 4, 5):  # spring
        return dict(
            t_min=12,
            t_max=28,
            rh_min=40,
            rh_max=85,
            solar_peak=850,
            wind_min=0.4,
            wind_max=4.5,
            p_base=1015,
            rain_p=0.12,
        )
    if month in (6, 7, 8):  # summer
        return dict(
            t_min=18,
            t_max=36,
            rh_min=25,
            rh_max=70,
            solar_peak=950,
            wind_min=0.6,
            wind_max=5.5,
            p_base=1013,
            rain_p=0.04,
        )
    # autumn
    return dict(
        t_min=11,
        t_max=26,
        rh_min=45,
        rh_max=90,
        solar_peak=800,
        wind_min=0.4,
        wind_max=4.0,
        p_base=1016,
        rain_p=0.10,
    )


def day_bounds(month: int):
    if month in (12, 1, 2):
        return (7 * 60 + 45, 17 * 60 + 45)
    if month in (3, 4, 5):
        return (6 * 60 + 45, 19 * 60 + 15)
    if month in (6, 7, 8):
        return (6 * 60, 20 * 60 + 30)
    return (7 * 60, 18 * 60 + 30)


def diurnal_light(mins_local: int, sunrise: int, sunset: int) -> float:
    if mins_local <= sunrise or mins_local >= sunset:
        return 0.0
    span = sunset - sunrise
    x = (mins_local - sunrise) / span
    return max(0.0, math.sin(math.pi * x)) ** 1.15


def diurnal_heat(mins_local: int, sunrise: int, sunset: int) -> float:
    if mins_local <= sunrise or mins_local >= sunset:
        return 0.0
    span = sunset - sunrise
    x = (mins_local - sunrise) / span - 0.08
    x = min(1.0, max(0.0, x))
    return (math.sin(math.pi * x)) ** 1.25


def get_or_create_user_and_zones():
    # ensure a default user + at least one zone
    user, created = User.objects.get_or_create(
        username="user1",
        defaults=dict(email="john.doe@example.com", is_active=True, is_staff=False),
    )
    if created:
        user.set_password("MKSzak123")
        user.save()

    zones = list(Zone.objects.filter(user=user))
    if not zones:
        zones.append(
            Zone.objects.create(
                user=user,
                name="zone de marichage 1",
                space=1750.0,
                critical_moisture_threshold=18.0,
                pomp_flow_rate=1.0,
            )
        )

    for z in zones:
        ActiveGraph.objects.get_or_create(
            user=user,
            zone=z,
            defaults=dict(
                soil_irrigation_status=True,
                soil_ph_status=True,
                soil_conductivity_status=True,
                soil_moisture_status=True,
                soil_temperature_status=True,
                et0_status=True,
                wind_speed_status=True,
                wind_direction_status=True,
                solar_radiation_status=True,
                temperature_humidity_weather_status=True,
                precipitation_humidity_rate_status=True,
                pluviometry_status=True,
                data_table_status=True,
                wind_radar_status=True,
                cumulative_precipitation_status=True,
                precipitation_rate_status=True,
                weather_temperature_humidity_status=True,
                water_flow_status=True,
                water_pressure_status=True,
                water_ph_status=True,
                water_ec_status=True,
                leaf_sensor_status=True,
                fruit_size_status=True,
                large_fruit_diameter_status=True,
                npk_status=True,
                electricity_consumption_status=True,
            ),
        )
    return user, zones


# --- main simulator ---
@shared_task
def simulate_sensor_ingest(self):
    """
    Runs every 15 minutes. Writes one sample for *each sensor model* per zone,
    using quarter-hour aligned timestamps for fast-changing sensors, hourly/daily
    cadences for inherently slow sensors (still triggered by this task).
    Idempotent per (zone, timestamp, model).
    """
    user, zones = get_or_create_user_and_zones()

    schedule_mode = getattr(settings, "SCHEDULE_MODE", "test").lower()
    # align to quarter-hour UTC
    now_utc = timezone.now()
    aligned_min = (now_utc.minute // 1) * 1
    if schedule_mode == "test":
        # last full 4-minute window end-aligned to previous multiple of 4
        end = now.replace(second=0, microsecond=0)
        end = end.replace(minute=(end.minute // 4) * 4)
        start = end - timedelta(minutes=4)
    else:
        # last full hour
        end = now.replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(hours=1)
    slot_utc = now_utc.replace(minute=aligned_min, second=0, microsecond=0)
    # now_utc = timezone.now()
    # slot_utc = now_utc.replace(second=0, microsecond=0)
    results = []

    for zone in zones:
        local = slot_utc.astimezone(LOCAL_TZ)
        mins_local = local.hour * 60 + local.minute
        month = local.month

        P = season_params(month)
        sunrise, sunset = day_bounds(month)
        light = diurnal_light(mins_local, sunrise, sunset)  # 0..1
        heat = diurnal_heat(mins_local, sunrise, sunset)  # 0..1

        # Weather synthesis (quarter-hour)
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

        # Rain is sparse; small bursts when it happens
        rain_prob = P["rain_p"] * (
            0.3 if light > 0.7 else 1.0
        )  # less likely at bright peak
        precip_mmph = 0.0
        if random.random() < rain_prob:
            precip_mmph = round(max(0.5, n(3.0, 2.0)), 2)

        # Wind direction (deg) with persistence
        last_dir = (
            WindDirection.objects.filter(zone=zone)
            .order_by("-timestamp")
            .values_list("value", flat=True)
            .first()
        )
        base_dir = last_dir if last_dir is not None else random.uniform(0, 360)
        wind_dir = (base_dir + n(0, 12)) % 360

        # --- Water system heuristic windows (local irrigation) ---
        irrigating = ((6 * 60) <= mins_local <= (7 * 60)) or (
            (18 * 60) <= mins_local <= (19 * 60)
        )
        flow_m3h = clamp((0.0 if not irrigating else 12.0 + n(0, 1.5)), 0.0, 25.0)
        press_bar = 0.0 if flow_m3h == 0.0 else clamp(2.5 + n(0, 0.15), 1.6, 3.2)
        water_ec = clamp(650 + n(0, 60), 200, 1500)  # μS/cm
        water_pH = clamp(7.2 + n(0, 0.08), 6.5, 7.8)

        # --- Soil profile (use last values for continuity) ---
        def last(model):
            obj = model.objects.filter(zone=zone).order_by("-timestamp").first()
            return obj.value if obj else None

        sm_low = last(SoilMoistureLow) or 27.0
        sm_med = last(SoilMoistureMedium) or 25.0
        sm_high = last(SoilMoistureHigh) or 22.0

        evap = (
            0.03 * (0.3 + 0.7 * light) * (0.4 + 0.6 * (1 - rh_pct / 100.0))
        )  # % per 15 min
        rain_increase = 0.0 if precip_mmph == 0.0 else min(6.0, 0.7 * precip_mmph)
        irrig_increase = 0.0 if flow_m3h == 0.0 else 1.8

        delta_top = -evap + rain_increase + irrig_increase
        delta_mid = -0.6 * evap + 0.55 * rain_increase + 0.4 * irrig_increase
        delta_deep = -0.35 * evap + 0.25 * rain_increase + 0.2 * irrig_increase

        sm_low = clamp(sm_low + delta_top + n(0, 0.15), 5, 60)
        sm_med = clamp(sm_med + delta_mid + n(0, 0.10), 5, 60)
        sm_high = clamp(sm_high + delta_deep + n(0, 0.08), 5, 60)

        # soil temperature follows air with depth-damping
        st_low = clamp(temp_c + 0.2 + (light * 2.2) + n(0, 0.3), -5, 50)
        st_med = clamp(temp_c - 0.5 + (light * 1.2) + n(0, 0.25), -5, 50)
        st_high = clamp(temp_c - 1.2 + (light * 0.6) + n(0, 0.2), -5, 50)

        # soil pH and EC drift slowly; EC dilutes with wetting
        ph_soil = clamp((last(PhSoil) or 7.0) + n(0, 0.01), 5.5, 8.5)
        ec_med = clamp(
            (last(ECSoilMedium) or 1.4)
            - 0.02 * (rain_increase + irrig_increase)
            + n(0, 0.03),
            0.1,
            4.5,
        )
        ec_low = clamp(
            (last(ECSoilLow) or 1.1)
            - 0.02 * (rain_increase + irrig_increase)
            + n(0, 0.03),
            0.05,
            4.0,
        )
        ec_high = clamp(
            (last(ECSoilHigh) or 1.6)
            - 0.02 * (rain_increase + irrig_increase)
            + n(0, 0.03),
            0.1,
            5.0,
        )

        # salinity/soil conductivity “other” sensors correlated to EC
        soil_cond_uScm = clamp(
            (last(SoilConductivitySensor) or 800.0)
            + n(0, 6.0)
            - 5.0 * (rain_increase + irrig_increase),
            200,
            5000,
        )
        soil_salinity = clamp(
            (last(SoilSalinitySensor) or 1800.0)
            + n(0, 12.0)
            - 8.0 * (rain_increase + irrig_increase),
            300,
            7000,
        )
        ec_combo = clamp(
            (last(EcSalinitySensor) or 1000.0)
            + n(0, 10.0)
            - 8.0 * (rain_increase + irrig_increase),
            200,
            6000,
        )

        # leaf/plant: leaf T hotter than air under sun; leaf moisture tracks RH
        leaf_temp = clamp(temp_c + (3.0 * light) + n(0, 0.4), -5, 55)
        leaf_moist = clamp(40 + 50 * (rh_pct / 100.0) - 30 * light + n(0, 2.5), 20, 100)

        # fruit metrics: slow growth; mm per 15 min ~ tiny
        fruit_size = clamp(
            (
                FruitSizeSensor.objects.filter(zone=zone)
                .order_by("-timestamp")
                .values_list("value", flat=True)
                .first()
                or 10.0
            )
            + max(0.0, n(0.03, 0.05)),
            5.0,
            120.0,
        )
        fruit_diam = clamp(
            (
                LargeFruitDiameterSensor.objects.filter(zone=zone)
                .order_by("-timestamp")
                .values_list("value", flat=True)
                .first()
                or 15.0
            )
            + max(0.0, n(0.04, 0.06)),
            8.0,
            160.0,
        )

        # water level: decays slowly, rises with rain/irrigation
        water_level = clamp(
            (
                WaterLevelSensor.objects.filter(zone=zone)
                .order_by("-timestamp")
                .values_list("value", flat=True)
                .first()
                or 150.0
            )
            - 0.15
            + 0.8 * (precip_mmph > 2.0)
            + 0.5 * (flow_m3h > 0)
            + n(0, 0.2),
            50,
            250,
        )

        # power: pump + day demand
        base_kwh = 0.05 + 0.08 * (light > 0.1)
        pump_kwh = 0.35 if flow_m3h > 0 else 0.0
        elec_kwh = clamp(base_kwh + pump_kwh + n(0, 0.01), 0.02, 1.0)

        # water chemistry may drift with pumping
        water_ec += (50 if flow_m3h > 0 else 0) + n(0, 25)
        water_ec = clamp(water_ec, 200, 2000)
        water_pH = clamp(
            water_pH + (0.02 if flow_m3h > 0 else -0.01) + n(0, 0.01), 6.4, 7.9
        )

        # NPK: quasi-static with tiny noise (you *can* change cadence later)
        N = clamp(
            (
                NpkSensor.objects.filter(zone=zone)
                .order_by("-timestamp")
                .values_list("nitrogen_value", flat=True)
                .first()
                or 50.0
            )
            + n(0, 0.3),
            20,
            120,
        )
        P = clamp(
            (
                NpkSensor.objects.filter(zone=zone)
                .order_by("-timestamp")
                .values_list("phosphorus_value", flat=True)
                .first()
                or 25.0
            )
            + n(0, 0.2),
            5,
            80,
        )
        K = clamp(
            (
                NpkSensor.objects.filter(zone=zone)
                .order_by("-timestamp")
                .values_list("potassium_value", flat=True)
                .first()
                or 40.0
            )
            + n(0, 0.3),
            10,
            150,
        )

        # Multi-depth soil moisture as an aggregate proxy
        md_sm = clamp((sm_low + sm_med + sm_high) / 3.0 + n(0, 0.2), 5, 60)

        # ----------------- idempotent inserts -----------------
        with transaction.atomic():
            # Weather (15-min slot)
            if not TemperatureWeather.objects.filter(
                zone=zone, timestamp=slot_utc
            ).exists():
                TemperatureWeather.objects.create(
                    zone=zone, user=user, value=float(temp_c), timestamp=slot_utc
                )
            if not HumidityWeather.objects.filter(
                zone=zone, timestamp=slot_utc
            ).exists():
                HumidityWeather.objects.create(
                    zone=zone, user=user, value=float(rh_pct), timestamp=slot_utc
                )
            if not WindSpeed.objects.filter(zone=zone, timestamp=slot_utc).exists():
                WindSpeed.objects.create(
                    zone=zone, user=user, value=float(wind_ms), timestamp=slot_utc
                )
            if not WindDirection.objects.filter(zone=zone, timestamp=slot_utc).exists():
                WindDirection.objects.create(
                    zone=zone, user=user, value=float(wind_dir), timestamp=slot_utc
                )
            if not SolarRadiation.objects.filter(
                zone=zone, timestamp=slot_utc
            ).exists():
                SolarRadiation.objects.create(
                    zone=zone, user=user, value=float(solar_wm2), timestamp=slot_utc
                )
            if not PressureWeather.objects.filter(
                zone=zone, timestamp=slot_utc
            ).exists():
                PressureWeather.objects.create(
                    zone=zone, user=user, value=float(pressure_hpa), timestamp=slot_utc
                )
            if not PrecipitationRate.objects.filter(
                zone=zone, timestamp=slot_utc
            ).exists():
                PrecipitationRate.objects.create(
                    zone=zone, user=user, value=float(precip_mmph), timestamp=slot_utc
                )

            # Soil
            if not SoilMoistureLow.objects.filter(
                zone=zone, timestamp=slot_utc
            ).exists():
                SoilMoistureLow.objects.create(
                    zone=zone, user=user, value=float(sm_low), timestamp=slot_utc
                )
            if not SoilMoistureMedium.objects.filter(
                zone=zone, timestamp=slot_utc
            ).exists():
                SoilMoistureMedium.objects.create(
                    zone=zone, user=user, value=float(sm_med), timestamp=slot_utc
                )
            if not SoilMoistureHigh.objects.filter(
                zone=zone, timestamp=slot_utc
            ).exists():
                SoilMoistureHigh.objects.create(
                    zone=zone, user=user, value=float(sm_high), timestamp=slot_utc
                )

            if not SoilTemperatureLow.objects.filter(
                zone=zone, timestamp=slot_utc
            ).exists():
                SoilTemperatureLow.objects.create(
                    zone=zone, user=user, value=float(st_low), timestamp=slot_utc
                )
            if not SoilTemperatureMedium.objects.filter(
                zone=zone, timestamp=slot_utc
            ).exists():
                SoilTemperatureMedium.objects.create(
                    zone=zone, user=user, value=float(st_med), timestamp=slot_utc
                )
            if not SoilTemperatureHigh.objects.filter(
                zone=zone, timestamp=slot_utc
            ).exists():
                SoilTemperatureHigh.objects.create(
                    zone=zone, user=user, value=float(st_high), timestamp=slot_utc
                )

            if not PhSoil.objects.filter(zone=zone, timestamp=slot_utc).exists():
                PhSoil.objects.create(
                    zone=zone, user=user, value=float(ph_soil), timestamp=slot_utc
                )
            if not ECSoilLow.objects.filter(zone=zone, timestamp=slot_utc).exists():
                ECSoilLow.objects.create(
                    zone=zone, user=user, value=float(ec_low), timestamp=slot_utc
                )
            if not ECSoilMedium.objects.filter(zone=zone, timestamp=slot_utc).exists():
                ECSoilMedium.objects.create(
                    zone=zone, user=user, value=float(ec_med), timestamp=slot_utc
                )
            if not ECSoilHigh.objects.filter(zone=zone, timestamp=slot_utc).exists():
                ECSoilHigh.objects.create(
                    zone=zone, user=user, value=float(ec_high), timestamp=slot_utc
                )

            if not SoilConductivitySensor.objects.filter(
                zone=zone, timestamp=slot_utc
            ).exists():
                SoilConductivitySensor.objects.create(
                    zone=zone,
                    user=user,
                    value=float(soil_cond_uScm),
                    timestamp=slot_utc,
                )
            if not SoilSalinitySensor.objects.filter(
                zone=zone, timestamp=slot_utc
            ).exists():
                SoilSalinitySensor.objects.create(
                    zone=zone, user=user, value=float(soil_salinity), timestamp=slot_utc
                )
            if not MultiDepthSoilMoistureSensor.objects.filter(
                zone=zone, timestamp=slot_utc
            ).exists():
                MultiDepthSoilMoistureSensor.objects.create(
                    zone=zone, user=user, value=float(md_sm), timestamp=slot_utc
                )

            # Plant
            if not LeafTemperatureSensor.objects.filter(
                zone=zone, timestamp=slot_utc
            ).exists():
                LeafTemperatureSensor.objects.create(
                    zone=zone, user=user, value=float(leaf_temp), timestamp=slot_utc
                )
            if not LeafMoistureSensor.objects.filter(
                zone=zone, timestamp=slot_utc
            ).exists():
                LeafMoistureSensor.objects.create(
                    zone=zone, user=user, value=float(leaf_moist), timestamp=slot_utc
                )
            if not FruitSizeSensor.objects.filter(
                zone=zone, timestamp=slot_utc
            ).exists():
                FruitSizeSensor.objects.create(
                    zone=zone, user=user, value=float(fruit_size), timestamp=slot_utc
                )
            if not LargeFruitDiameterSensor.objects.filter(
                zone=zone, timestamp=slot_utc
            ).exists():
                LargeFruitDiameterSensor.objects.create(
                    zone=zone, user=user, value=float(fruit_diam), timestamp=slot_utc
                )

            # Water
            if not WaterFlowSensor.objects.filter(
                zone=zone, timestamp=slot_utc
            ).exists():
                WaterFlowSensor.objects.create(
                    zone=zone, user=user, value=float(flow_m3h), timestamp=slot_utc
                )
            if not WaterPressureSensor.objects.filter(
                zone=zone, timestamp=slot_utc
            ).exists():
                WaterPressureSensor.objects.create(
                    zone=zone, user=user, value=float(press_bar), timestamp=slot_utc
                )
            if not WaterECSensor.objects.filter(zone=zone, timestamp=slot_utc).exists():
                WaterECSensor.objects.create(
                    zone=zone, user=user, value=float(water_ec), timestamp=slot_utc
                )
            if not PhWaterSensor.objects.filter(zone=zone, timestamp=slot_utc).exists():
                PhWaterSensor.objects.create(
                    zone=zone, user=user, value=float(water_pH), timestamp=slot_utc
                )
            if not WaterLevelSensor.objects.filter(
                zone=zone, timestamp=slot_utc
            ).exists():
                WaterLevelSensor.objects.create(
                    zone=zone, user=user, value=float(water_level), timestamp=slot_utc
                )

            # Power
            if not ElectricityConsumptionSensor.objects.filter(
                zone=zone, timestamp=slot_utc
            ).exists():
                ElectricityConsumptionSensor.objects.create(
                    zone=zone, user=user, value=float(elec_kwh), timestamp=slot_utc
                )

            # Nutrients (written every run so charts move, but tiny noise)
            if not NpkSensor.objects.filter(zone=zone, timestamp=slot_utc).exists():
                NpkSensor.objects.create(
                    zone=zone,
                    user=user,
                    timestamp=slot_utc,
                    nitrogen_value=float(N),
                    phosphorus_value=float(P),
                    potassium_value=float(K),
                )

            # Optional combined EC/salinity sensor
            if "EcSalinitySensor" in globals():
                if not EcSalinitySensor.objects.filter(
                    zone=zone, timestamp=slot_utc
                ).exists():
                    EcSalinitySensor.objects.create(
                        zone=zone, user=user, value=float(ec_combo), timestamp=slot_utc
                    )

        results.append(dict(zone_id=zone.id, ts=slot_utc.isoformat()))

    return {"slots": results}
