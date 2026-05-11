"""
Tests for agriBack.agronomy.

Cover both the pure math (no DB) and the high-level entry points
(field_snapshot, compute_et0_for_zone) against synthetic sensor rows.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from agriBack.agronomy import (
    DEFAULT_KC,
    actual_vapor_pressure_kpa,
    compute_et0_for_zone,
    field_snapshot,
    penman_monteith_hourly_mm,
    psychrometric_constant_kpa_per_c,
    saturation_vapor_pressure_kpa,
    slope_svp_kpa_per_c,
    vpd_kpa,
    wperm2_to_mjm2_per_hour,
)
from analytics.models import (
    Et0Calculated,
    HumidityWeather,
    NpkSensor,
    PhSoil,
    PressureWeather,
    SoilMoistureMedium,
    SoilTemperatureMedium,
    SolarRadiation,
    TemperatureWeather,
    WaterFlowSensor,
    WindSpeed,
    Zone,
)

User = get_user_model()


def _user():
    u = User.objects.create(
        username="agro",
        email="agro@example.com",
        firstname="Agro",
        lastname="Test",
        is_active=True,
    )
    u.set_password("pw")
    u.save()
    return u


def _zone(user, name="zone-1"):
    return Zone.objects.create(
        user=user,
        name=name,
        space=1000.0,
        critical_moisture_threshold=20.0,
        pomp_flow_rate=1.0,
    )


# ---------------------------------------------------------------------------
# 1. Pure math (no DB)
# ---------------------------------------------------------------------------


class PureMathTests(TestCase):
    def test_saturation_vapor_pressure_at_known_temps(self):
        # FAO-56 reference values within tolerance
        self.assertAlmostEqual(saturation_vapor_pressure_kpa(0.0), 0.6108, places=3)
        self.assertAlmostEqual(saturation_vapor_pressure_kpa(20.0), 2.339, places=2)
        self.assertAlmostEqual(saturation_vapor_pressure_kpa(40.0), 7.384, places=1)

    def test_actual_vapor_pressure_clips_rh(self):
        es = saturation_vapor_pressure_kpa(20.0)
        self.assertEqual(actual_vapor_pressure_kpa(20.0, 0), 0.0)
        self.assertAlmostEqual(actual_vapor_pressure_kpa(20.0, 100), es, places=4)
        # >100% RH clamped, not a programming error
        self.assertAlmostEqual(
            actual_vapor_pressure_kpa(20.0, 150), es, places=4
        )
        self.assertAlmostEqual(actual_vapor_pressure_kpa(20.0, -10), 0.0, places=4)

    def test_vpd_never_negative(self):
        self.assertGreaterEqual(vpd_kpa(15.0, 80.0), 0.0)
        self.assertGreaterEqual(vpd_kpa(35.0, 100.0), 0.0)
        self.assertGreater(vpd_kpa(35.0, 30.0), vpd_kpa(15.0, 95.0))

    def test_slope_and_gamma_are_positive(self):
        self.assertGreater(slope_svp_kpa_per_c(20.0), 0)
        self.assertGreater(psychrometric_constant_kpa_per_c(101.3), 0)

    def test_radiation_unit_conversion(self):
        # 1000 W/m^2 over an hour -> 3.6 MJ/m^2/h
        self.assertAlmostEqual(wperm2_to_mjm2_per_hour(1000.0), 3.6, places=2)

    def test_penman_monteith_typical_summer_hour_is_plausible(self):
        result = penman_monteith_hourly_mm(
            temp_c=28.0,
            rh_pct=45.0,
            wind_ms=2.0,
            pressure_kpa=101.0,
            rs_mjm2h=2.5,
        )
        # Hourly ET0 in summer afternoon should be positive and bounded
        self.assertGreater(result["et0_mm_per_h"], 0.05)
        self.assertLess(result["et0_mm_per_h"], 1.5)
        self.assertGreater(result["vpd_kpa"], 1.0)

    def test_penman_monteith_night_hour_is_low(self):
        result = penman_monteith_hourly_mm(
            temp_c=18.0,
            rh_pct=85.0,
            wind_ms=1.0,
            pressure_kpa=101.0,
            rs_mjm2h=0.0,  # no sun
        )
        # Should not be negative and should be small
        self.assertGreaterEqual(result["et0_mm_per_h"], 0.0)
        self.assertLess(result["et0_mm_per_h"], 0.2)


# ---------------------------------------------------------------------------
# 2. compute_et0_for_zone (pulls from DB, no writes)
# ---------------------------------------------------------------------------


class ComputeEt0ForZoneTests(TestCase):
    def setUp(self):
        self.user = _user()
        self.zone = _zone(self.user)
        self.end = timezone.now().replace(minute=0, second=0, microsecond=0)
        self.mid = self.end - timedelta(minutes=30)

    def _seed_full_hour(self):
        TemperatureWeather.objects.create(
            zone=self.zone, user=self.user, value=28.0, timestamp=self.mid
        )
        HumidityWeather.objects.create(
            zone=self.zone, user=self.user, value=45.0, timestamp=self.mid
        )
        WindSpeed.objects.create(
            zone=self.zone, user=self.user, value=2.0, timestamp=self.mid
        )
        SolarRadiation.objects.create(
            zone=self.zone, user=self.user, value=700.0, timestamp=self.mid
        )
        PressureWeather.objects.create(
            zone=self.zone, user=self.user, value=1010.0, timestamp=self.mid
        )

    def test_returns_none_when_inputs_missing(self):
        self.assertIsNone(compute_et0_for_zone(self.zone, end=self.end))

    def test_returns_dataclass_with_plausible_values(self):
        self._seed_full_hour()
        out = compute_et0_for_zone(self.zone, end=self.end)
        self.assertIsNotNone(out)
        self.assertEqual(out.zone_id, self.zone.id)
        self.assertEqual(out.user_id, self.user.id)
        self.assertEqual(out.timestamp, self.end)
        self.assertGreater(out.et0_mm_per_h, 0.05)
        self.assertLess(out.et0_mm_per_h, 1.5)
        self.assertGreater(out.vpd_kpa, 1.0)

    def test_does_not_persist(self):
        self._seed_full_hour()
        compute_et0_for_zone(self.zone, end=self.end)
        # Pure function — never writes Et0Calculated rows
        self.assertEqual(Et0Calculated.objects.count(), 0)


# ---------------------------------------------------------------------------
# 3. field_snapshot (high-level — what notifications consume)
# ---------------------------------------------------------------------------


class FieldSnapshotTests(TestCase):
    def setUp(self):
        self.user = _user()
        self.now = timezone.now()

    def test_returns_placeholder_dict_when_no_zone(self):
        snap = field_snapshot(self.user)
        self.assertIsNone(snap["zone_name"])
        self.assertEqual(snap["kc_used"], DEFAULT_KC)
        self.assertIn("Aucune zone", snap["irrigation_decision"])

    def test_has_documented_keys(self):
        zone = _zone(self.user)
        snap = field_snapshot(self.user)
        for key in (
            "zone_name",
            "date_today",
            "yesterday_temp_c",
            "today_temp_c",
            "yesterday_humidity_pct",
            "today_humidity_pct",
            "et0_today_mm",
            "soil_moisture_pct",
            "soil_temperature_c",
            "soil_ph",
            "soil_ec",
            "soil_salinity",
            "npk_n",
            "npk_p",
            "npk_k",
            "last_irrigation_at",
            "last_irrigation_l",
            "perfect_irrigation_window",
            "kc_used",
            "irrigation_decision",
        ):
            self.assertIn(key, snap)
        self.assertEqual(snap["zone_name"], zone.name)

    def test_uses_real_sensor_data(self):
        zone = _zone(self.user)
        # today: warmer + drier
        TemperatureWeather.objects.create(
            zone=zone, user=self.user, value=30.0, timestamp=self.now
        )
        HumidityWeather.objects.create(
            zone=zone, user=self.user, value=35.0, timestamp=self.now
        )
        # yesterday
        yest = self.now - timedelta(days=1)
        TemperatureWeather.objects.create(
            zone=zone, user=self.user, value=20.0, timestamp=yest
        )
        HumidityWeather.objects.create(
            zone=zone, user=self.user, value=70.0, timestamp=yest
        )
        SoilMoistureMedium.objects.create(
            zone=zone, user=self.user, value=15.0, timestamp=self.now
        )
        SoilTemperatureMedium.objects.create(
            zone=zone, user=self.user, value=22.0, timestamp=self.now
        )
        PhSoil.objects.create(
            zone=zone, user=self.user, value=6.7, timestamp=self.now
        )
        NpkSensor.objects.create(
            zone=zone,
            user=self.user,
            timestamp=self.now,
            nitrogen_value=80.0,
            phosphorus_value=40.0,
            potassium_value=120.0,
        )
        WaterFlowSensor.objects.create(
            zone=zone,
            user=self.user,
            value=12.0,
            timestamp=self.now - timedelta(hours=2),
        )
        # ET0 today — two hourly rows summed
        Et0Calculated.objects.create(
            zone=zone,
            user=self.user,
            value=0.4,
            timestamp=self.now - timedelta(hours=1),
        )
        Et0Calculated.objects.create(
            zone=zone, user=self.user, value=0.6, timestamp=self.now
        )

        snap = field_snapshot(self.user)

        self.assertAlmostEqual(snap["today_temp_c"], 30.0)
        self.assertAlmostEqual(snap["yesterday_temp_c"], 20.0)
        self.assertAlmostEqual(snap["today_humidity_pct"], 35.0)
        self.assertAlmostEqual(snap["yesterday_humidity_pct"], 70.0)
        self.assertAlmostEqual(snap["soil_moisture_pct"], 15.0)
        self.assertAlmostEqual(snap["soil_temperature_c"], 22.0)
        self.assertAlmostEqual(snap["soil_ph"], 6.7)
        self.assertEqual(snap["npk_n"], 80.0)
        self.assertEqual(snap["npk_k"], 120.0)
        self.assertIsNotNone(snap["last_irrigation_at"])
        # 12 m3 → 12_000 L
        self.assertAlmostEqual(snap["last_irrigation_l"], 12_000.0)
        self.assertAlmostEqual(snap["et0_today_mm"], 1.0)
        self.assertIn("Irrigation recommandée", snap["irrigation_decision"])

    def test_irrigation_decision_when_soil_above_threshold(self):
        zone = _zone(self.user)
        SoilMoistureMedium.objects.create(
            zone=zone, user=self.user, value=40.0, timestamp=self.now
        )
        Et0Calculated.objects.create(
            zone=zone, user=self.user, value=0.5, timestamp=self.now
        )
        snap = field_snapshot(self.user)
        self.assertIn("Pas d'irrigation requise", snap["irrigation_decision"])

    def test_irrigation_decision_with_no_signal(self):
        _zone(self.user)
        snap = field_snapshot(self.user)
        self.assertIn("Données insuffisantes", snap["irrigation_decision"])
