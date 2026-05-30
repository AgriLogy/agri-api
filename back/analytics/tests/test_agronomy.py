"""
Tests for agriBack.agronomy.

Cover both the pure math (no DB) and the high-level entry points
(field_snapshot, compute_et0_for_zone) against synthetic sensor rows.
"""

import datetime as dt
from datetime import timedelta
from unittest import mock, skipUnless

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

# The field_snapshot adapter now delegates to agri-core's DB-backed handler,
# which reads via its own SQLAlchemy connection (AGRI_DB_URL). Those tests
# therefore need a real Postgres both ORMs share, and must commit (hence
# TransactionTestCase) so the separate connection sees the data. Gate on the
# configured DB engine (not an env var, which back/.env can leak).
_REQUIRES_PG = skipUnless(
    settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    "dual-ORM field_snapshot requires Postgres",
)

from agriBack.agronomy import (
    CLOUD_FACTOR_MIN,
    CLOUD_RATIO_MAX,
    CLOUD_RATIO_MIN,
    CROP_STAGE_PROFILES,
    DEFAULT_KC,
    DEFAULT_RAINFALL_EFFICIENCY,
    LST_DEG_MOROCCO,
    RAIN_FORECAST_TRIGGER_MM,
    actual_vapor_pressure_kpa,
    cloudiness_ratio,
    compute_et0_for_zone,
    cumulative_dr_after_missed_days,
    effective_rainfall_mm,
    equation_of_time_minutes,
    etc_mm,
    extraterrestrial_radiation_hourly_mjm2h,
    field_snapshot,
    irrigation_decision_dr,
    is_daytime,
    penman_monteith_hourly_mm,
    psychrometric_constant_kpa_per_c,
    saturation_vapor_pressure_kpa,
    slope_svp_kpa_per_c,
    solar_time_correction_hours,
    update_daily_depletion,
    vpd_kpa,
    wind_speed_at_2m,
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

    def test_actual_vapor_pressure_clamps_rh_to_one_to_hundred(self):
        # Agronomist review (2026-05-10) asks for a [1, 100] RH clamp so
        # bad inputs (sensor stuck at 0, or above 100) stay in physical
        # range. RH=0 maps to 1% of es, not zero, to avoid an inflated VPD.
        es = saturation_vapor_pressure_kpa(20.0)
        self.assertAlmostEqual(
            actual_vapor_pressure_kpa(20.0, 0.0), es * 0.01, places=4
        )
        self.assertAlmostEqual(
            actual_vapor_pressure_kpa(20.0, -50.0), es * 0.01, places=4
        )
        self.assertAlmostEqual(actual_vapor_pressure_kpa(20.0, 100.0), es, places=4)
        self.assertAlmostEqual(actual_vapor_pressure_kpa(20.0, 150.0), es, places=4)

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
        # Daytime now derived from Rs, not Rn: Rs=0 -> nighttime regime.
        self.assertFalse(result["daytime"])


# ---------------------------------------------------------------------------
# 1b. Agronomist-review corrections (2026-05-10)
# ---------------------------------------------------------------------------


class AgronomistReviewCorrectionsTests(TestCase):
    """One named test per clause of the agronomist's review, so a future
    refactor that drops a clause surfaces as a single, attributable failure."""

    def test_wind_at_2m_is_identity_when_sensor_already_at_2m(self):
        self.assertEqual(wind_speed_at_2m(3.0, 2.0), 3.0)

    def test_wind_at_2m_clamps_negative(self):
        self.assertEqual(wind_speed_at_2m(-1.0, 2.0), 0.0)

    def test_wind_at_10m_projects_via_fao_eq_47(self):
        # FAO eq. 47: u2 = u_z * 4.87 / ln(67.8*z - 5.42); at z=10 m the
        # factor is approximately 0.748.
        from math import log

        expected = 5.0 * 4.87 / log(67.8 * 10.0 - 5.42)
        self.assertAlmostEqual(wind_speed_at_2m(5.0, 10.0), expected, places=6)

    def test_equation_of_time_stays_within_seventeen_minutes(self):
        for doy in range(1, 367):
            self.assertLess(abs(equation_of_time_minutes(doy)), 17.0)

    def test_solar_time_correction_at_zone_meridian_is_just_eot(self):
        # At Lloc == Lst the 4*(Lloc - Lst) term vanishes and only EoT
        # contributes; converting min -> hours gives EoT/60.
        correction = solar_time_correction_hours(80, lon_deg=15.0, lst_deg=15.0)
        self.assertAlmostEqual(
            correction, equation_of_time_minutes(80) / 60.0, places=9
        )

    def test_solar_time_correction_negative_for_casablanca(self):
        # Casablanca lon=-7.6 in UTC+1 (Lst=+15): solar noon happens
        # ~1.5 h AFTER local clock noon, so the local->solar correction
        # must be roughly -1.5 h. Pick April 15 (EoT ~= 0) to isolate
        # the longitude term.
        correction = solar_time_correction_hours(
            105, lon_deg=-7.6, lst_deg=LST_DEG_MOROCCO
        )
        self.assertAlmostEqual(correction, -1.507, delta=0.05)

    def test_extraterrestrial_radiation_zero_at_local_midnight(self):
        ra = extraterrestrial_radiation_hourly_mjm2h(
            lat_deg=33.57, lon_deg=-7.6, day_of_year=172, local_clock_hour=0.0
        )
        self.assertEqual(ra, 0.0)

    def test_extraterrestrial_radiation_symmetric_around_solar_noon(self):
        # One hour before vs. one hour after solar noon yields equal Ra,
        # to numerical precision, when integrated symmetrically.
        lat, lon, doy = 33.57, -7.6, 172
        solar_noon = 12.0 - solar_time_correction_hours(
            doy, lon_deg=lon, lst_deg=LST_DEG_MOROCCO
        )
        ra_before = extraterrestrial_radiation_hourly_mjm2h(
            lat_deg=lat,
            lon_deg=lon,
            day_of_year=doy,
            local_clock_hour=solar_noon - 1,
        )
        ra_after = extraterrestrial_radiation_hourly_mjm2h(
            lat_deg=lat,
            lon_deg=lon,
            day_of_year=doy,
            local_clock_hour=solar_noon + 1,
        )
        self.assertAlmostEqual(ra_before, ra_after, places=6)

    def test_cloudiness_ratio_clamped_to_03_to_10(self):
        # Heavy overcast: physical Rs/Rso might dip below 0.3 but the
        # cloudiness function needs the floor to stay positive.
        self.assertEqual(cloudiness_ratio(0.1, 5.0), CLOUD_RATIO_MIN)
        # Normal mid-day.
        self.assertAlmostEqual(cloudiness_ratio(2.5, 5.0), 0.5, places=6)
        # Sensor bias or snow can push above 1.
        self.assertEqual(cloudiness_ratio(12.0, 5.0), CLOUD_RATIO_MAX)

    def test_cloudiness_ratio_uses_night_floor_when_rso_zero(self):
        # Rso==0 (deep night) -> ratio collapses to the 0.3 floor so
        # the cloud function 1.35*ratio - 0.35 stays positive.
        self.assertEqual(cloudiness_ratio(0.0, 0.0), CLOUD_RATIO_MIN)
        # The floor produces a cloud_term of 0.055, comfortably above the
        # defensive minimum.
        self.assertGreater(1.35 * CLOUD_RATIO_MIN - 0.35, CLOUD_FACTOR_MIN - 1e-9)

    def test_daytime_detected_by_solar_radiation(self):
        # Day iff Rs>0, not Rn>0. A cool, humid hour with no sun must
        # stay in the nighttime regime regardless of what Rn does.
        self.assertTrue(is_daytime(0.1))
        self.assertFalse(is_daytime(0.0))
        self.assertFalse(is_daytime(-1.0))

    def test_penman_monteith_uses_wired_ra_for_cloudiness(self):
        # With Ra provided, Rs/Rso falls inside the [0.3, 1.0] band and
        # cloud_term shifts. We don't pin the exact ET0 value but assert
        # that supplying Ra changes the result vs. the heuristic.
        common = dict(
            temp_c=28.0,
            rh_pct=45.0,
            wind_ms=2.0,
            pressure_kpa=101.0,
            rs_mjm2h=2.5,
        )
        with_ra = penman_monteith_hourly_mm(**common, ra_mjm2h=3.5)
        no_ra = penman_monteith_hourly_mm(**common, ra_mjm2h=None)
        self.assertNotAlmostEqual(
            with_ra["et0_mm_per_h"], no_ra["et0_mm_per_h"], places=6
        )

    def test_penman_monteith_negative_numerator_clamped_to_zero(self):
        # Cold, humid, no sun, slow wind: the bare formula would go
        # negative; the agronomist asks for a hard zero floor.
        result = penman_monteith_hourly_mm(
            temp_c=5.0,
            rh_pct=98.0,
            wind_ms=0.5,
            pressure_kpa=101.0,
            rs_mjm2h=0.0,
        )
        self.assertEqual(result["et0_mm_per_h"], 0.0)


# ---------------------------------------------------------------------------
# 2. compute_et0_for_zone (pulls from DB, no writes)
# ---------------------------------------------------------------------------


@_REQUIRES_PG
class ComputeEt0ForZoneTests(TransactionTestCase):
    # compute_et0_for_zone now delegates to agri-core's DB-backed handler,
    # which reads through its own SQLAlchemy connection. So the seeded rows
    # must be COMMITTED (TransactionTestCase) on a shared Postgres for that
    # separate connection to see them — same dual-ORM contract as
    # FieldSnapshotTests below.
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


@_REQUIRES_PG
class FieldSnapshotTests(TransactionTestCase):
    def setUp(self):
        self.user = _user()
        # Freeze "now" to a stable mid-day. field_snapshot derives the local
        # "today" window from now; the et0 rows seeded at now and now-1h must
        # both land in today regardless of when the suite runs. Without this
        # the test flaked near local midnight (et0_today_mm dropped to 0.6).
        self._now_patch = mock.patch(
            "django.utils.timezone.now",
            return_value=dt.datetime(2026, 5, 15, 12, 0, tzinfo=dt.timezone.utc),
        )
        self._now_patch.start()
        self.addCleanup(self._now_patch.stop)
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
        PhSoil.objects.create(zone=zone, user=self.user, value=6.7, timestamp=self.now)
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


# ---------------------------------------------------------------------------
# 4. Water-balance pure math (doc § 3–§ 4)
# ---------------------------------------------------------------------------


class WaterBalanceMathTests(TestCase):
    def test_effective_rainfall_applies_alpha(self):
        self.assertAlmostEqual(effective_rainfall_mm(10.0, 0.8), 8.0)
        self.assertAlmostEqual(effective_rainfall_mm(0.0), 0.0)

    def test_effective_rainfall_never_negative(self):
        # Defensive — α and P are physically >= 0 but a buggy caller might
        # pass a negative number; the helper must clamp.
        self.assertEqual(effective_rainfall_mm(-5.0, 0.8), 0.0)

    def test_etc_with_kc_and_permeability_loss(self):
        # ETc = ET0·Kc + loss_due_to_permeability — doc § 4.1
        self.assertAlmostEqual(etc_mm(5.0, 1.2), 6.0)
        self.assertAlmostEqual(etc_mm(5.0, 1.2, permeability_loss_mm=0.5), 6.5)

    def test_update_daily_depletion_basic(self):
        # Dr,i = max(0, Dr,(i-1) + ETc - Pe - In)
        dr = update_daily_depletion(
            dr_yesterday_mm=10.0,
            etc_today_mm=5.0,
            pe_today_mm=2.0,
            irrigation_applied_mm=3.0,
        )
        self.assertAlmostEqual(dr, 10.0 + 5.0 - 2.0 - 3.0)

    def test_update_daily_depletion_clamps_to_zero(self):
        # Heavy rain + irrigation > Dr+ETc → soil is back to FC, depletion 0.
        dr = update_daily_depletion(
            dr_yesterday_mm=2.0,
            etc_today_mm=1.0,
            pe_today_mm=10.0,
            irrigation_applied_mm=0.0,
        )
        self.assertEqual(dr, 0.0)

    def test_cumulative_dr_after_missed_days(self):
        # Two days skipped — ETc and Pe cumulate per doc § 4.3.
        dr = cumulative_dr_after_missed_days(
            dr_baseline_mm=0.0,
            et0_per_day_mm=[4.0, 5.0],
            rain_per_day_mm=[0.0, 1.0],
            kc=1.0,
            alpha=DEFAULT_RAINFALL_EFFICIENCY,
        )
        # ETc cumul = 9, Pe cumul = 0.8 → effective ETc = 8.2
        self.assertAlmostEqual(dr, 8.2)

    def test_crop_stage_profile_table(self):
        # Sanity-check the doc § 2.2 table — RAW is always p·TAW with the
        # typical p ≈ 0.5 the agronomist used.
        for stage, prof in CROP_STAGE_PROFILES.items():
            self.assertAlmostEqual(
                prof["raw_mm"],
                0.5 * prof["taw_mm"],
                places=2,
                msg=f"stage={stage}",
            )


# ---------------------------------------------------------------------------
# 5. Dr/RAW decision (doc § 4.1 + § 4.2)
# ---------------------------------------------------------------------------


class IrrigationDecisionDrTests(TestCase):
    """Each test exercises one branch of irrigation_decision_dr."""

    DEFAULT_ARGS = dict(
        raw_mm=40.0,
        soil_moisture_pct=30.0,
        critical_moisture_pct=20.0,
        zone_area_m2=1000.0,
        flow_rate_m3h=3.6,
        max_water_per_day_m3=0.0,
    )

    def test_no_stress_when_dr_below_raw_and_soil_ok(self):
        d = irrigation_decision_dr(dr_today_mm=10.0, **self.DEFAULT_ARGS)
        self.assertFalse(d.irrigate)
        self.assertEqual(d.reason, "no_stress")
        self.assertEqual(d.volume_m3, 0.0)

    def test_stress_when_dr_at_or_above_raw(self):
        d = irrigation_decision_dr(dr_today_mm=45.0, **self.DEFAULT_ARGS)
        self.assertTrue(d.irrigate)
        self.assertEqual(d.reason, "stress")
        self.assertGreater(d.volume_m3, 0.0)
        self.assertGreater(d.duration_hr, 0.0)

    def test_soil_moisture_low_triggers_even_below_raw(self):
        args = {**self.DEFAULT_ARGS, "soil_moisture_pct": 10.0}
        d = irrigation_decision_dr(dr_today_mm=5.0, **args)
        self.assertTrue(d.irrigate)
        self.assertEqual(d.reason, "soil_moisture_low")

    def test_rain_will_suffice_when_forecast_above_threshold_and_dr_below_raw(self):
        d = irrigation_decision_dr(
            dr_today_mm=10.0,
            precipitation_forecast_mm=RAIN_FORECAST_TRIGGER_MM + 0.1,
            **self.DEFAULT_ARGS,
        )
        self.assertFalse(d.irrigate)
        self.assertEqual(d.reason, "rain_will_suffice")

    def test_complementary_when_forecast_above_threshold_and_dr_above_raw(self):
        d = irrigation_decision_dr(
            dr_today_mm=45.0,
            precipitation_forecast_mm=10.0,
            **self.DEFAULT_ARGS,
        )
        self.assertTrue(d.irrigate)
        self.assertEqual(d.reason, "complementary")
        # net_mm = max(0, 45 - α·10) = 45 - 8 = 37 (with α=0.8 default)
        self.assertAlmostEqual(d.net_mm, 37.0, places=2)

    def test_complementary_zero_when_rain_covers_everything(self):
        d = irrigation_decision_dr(
            dr_today_mm=40.0,
            precipitation_forecast_mm=100.0,
            **self.DEFAULT_ARGS,
        )
        self.assertEqual(d.reason, "complementary")
        # net = max(0, 40 - 80) = 0 → returns False / 0 volume
        self.assertFalse(d.irrigate)
        self.assertEqual(d.net_mm, 0.0)

    def test_daily_volume_cap_applied(self):
        d = irrigation_decision_dr(
            dr_today_mm=100.0,
            raw_mm=40.0,
            soil_moisture_pct=30.0,
            critical_moisture_pct=20.0,
            zone_area_m2=1000.0,
            flow_rate_m3h=3.6,
            max_water_per_day_m3=5.0,
        )
        self.assertTrue(d.capped_to_daily_max)
        self.assertEqual(d.volume_m3, 5.0)

    def test_morning_evening_split_for_long_durations(self):
        # zone is small but flow_rate is tiny → big duration → triggers split
        d = irrigation_decision_dr(
            dr_today_mm=50.0,
            raw_mm=40.0,
            soil_moisture_pct=30.0,
            critical_moisture_pct=20.0,
            zone_area_m2=10000.0,
            flow_rate_m3h=0.1,  # 100 L/h — extremely slow
            max_water_per_day_m3=0.0,
        )
        self.assertTrue(d.irrigate)
        self.assertIsNotNone(d.morning_volume_m3)
        self.assertIsNotNone(d.evening_volume_m3)
        self.assertAlmostEqual(d.morning_volume_m3 + d.evening_volume_m3, d.volume_m3)


# ---------------------------------------------------------------------------
# 6. field_snapshot — Dr branch integration
# ---------------------------------------------------------------------------


@_REQUIRES_PG
class FieldSnapshotDrIntegrationTests(TransactionTestCase):
    def setUp(self):
        self.user = _user()
        self.zone = _zone(self.user)
        # Give the zone a real soil profile so the Dr branch lights up.
        self.zone.soil_param_RAW = 40.0
        self.zone.soil_param_TAW = 80.0
        self.zone.irrigation_water_quantity = 50_000.0  # 50 m³ cap
        self.zone.save()
        self.now = timezone.now()
        SoilMoistureMedium.objects.create(
            zone=self.zone, user=self.user, value=30.0, timestamp=self.now
        )

    def test_dr_keys_absent_when_dr_not_provided(self):
        snap = field_snapshot(self.user)
        self.assertIsNone(snap["dr_today_mm"])
        self.assertIsNone(snap["decision_reason"])
        self.assertIsNone(snap["recommended_volume_m3"])
        # legacy decision message still produced
        self.assertIsInstance(snap["irrigation_decision"], str)

    def test_no_stress_path_populated_when_dr_below_raw(self):
        snap = field_snapshot(self.user, dr_today_mm=10.0)
        self.assertEqual(snap["decision_reason"], "no_stress")
        self.assertEqual(snap["recommended_volume_m3"], 0.0)
        self.assertEqual(snap["raw_mm"], 40.0)
        self.assertEqual(snap["taw_mm"], 80.0)

    def test_stress_path_produces_volume_and_duration(self):
        snap = field_snapshot(self.user, dr_today_mm=45.0)
        self.assertEqual(snap["decision_reason"], "stress")
        self.assertGreater(snap["recommended_volume_m3"], 0.0)
        self.assertGreater(snap["recommended_duration_min"], 0.0)
        self.assertIn("Irrigation recommandée", snap["irrigation_decision"])

    def test_rain_branch_suspends_when_forecast_exceeds_threshold(self):
        snap = field_snapshot(
            self.user,
            dr_today_mm=10.0,
            precipitation_forecast_mm=5.0,
        )
        self.assertEqual(snap["decision_reason"], "rain_will_suffice")
        self.assertEqual(snap["recommended_volume_m3"], 0.0)

    def test_rain_branch_complementary_when_dr_above_raw(self):
        snap = field_snapshot(
            self.user,
            dr_today_mm=45.0,
            precipitation_forecast_mm=5.0,
        )
        self.assertEqual(snap["decision_reason"], "complementary")
        self.assertGreater(snap["recommended_volume_m3"], 0.0)
