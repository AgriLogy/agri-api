"""Sensor-domain ORM models — extracted from analytics/models.py (Phase 5d).

Every concrete model below inherits from ``_SensorBase``, which sets
``Meta.app_label = "analytics"``. That keeps the historical
``analytics_<modelname>`` db_table and ``analytics.<Sensor>`` FK
references valid without a data migration — only the Python module
moves; the Django app registry stays unchanged.

FKs to ``Zone`` use the string form ``"analytics.Zone"`` to avoid a
circular import (analytics/models.py re-exports these classes).
"""
from typing import List

from django.conf import settings
from django.db import models

User = settings.AUTH_USER_MODEL


class _SensorBase(models.Model):
    class Meta:
        abstract = True
        app_label = "analytics"


class Et0Calculated(_SensorBase):
    zone = models.ForeignKey(
        "analytics.Zone", on_delete=models.CASCADE, related_name="et0_calculated_weather"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="et0_calculated_weather_per_user"
    )
    value = models.FloatField(
        null=True,
        blank=True,
        help_text="Evapotranspiration calculated (ET0) in mm/day.",
    )
    timestamp = models.DateTimeField()

    @property
    def default_unit(self) -> str:
        return "mm/day"

    @property
    def available_units(self) -> List[str]:
        return ["mm/day"]

    def __str__(self):
        return f"ET0 Calculated ({self.value} mm/day) at {self.timestamp} in Zone {self.zone_id}"


# receive data each : 10 minutes
# NOTE: a `@receiver(post_save, sender=Notification)` decorator was previously
# attached to this class — that's invalid (signals must call a *function*, not
# a Model constructor). It was dormant because nothing ever invoked
# `Notification.objects.create()`; the email-notifications work does invoke it,
# which surfaced the bug. If a real signal handler is needed here in the
# future, define a function below this class and decorate that.
class Et0Weather(_SensorBase):
    zone = models.ForeignKey("analytics.Zone", on_delete=models.CASCADE, related_name="et0_weather")
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="et0_weather_per_user"
    )
    value = models.FloatField(
        null=True, blank=True, help_text="Evapotranspiration (ET0) in mm/day."
    )
    timestamp = models.DateTimeField()

    @property
    def default_unit(self) -> str:
        return "mm/day"

    @property
    def available_units(self) -> List[str]:
        return ["mm/day"]

    def __str__(self):
        return f"ET0 ({self.value} mm/day) at {self.timestamp} in Zone {self.zone_id}"


# receive data each : 10 minutes
class PrecipitationRate(_SensorBase):
    zone = models.ForeignKey(
        "analytics.Zone", on_delete=models.CASCADE, related_name="precipitation_rates"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="precipitation_rates_per_user"
    )
    value = models.FloatField(
        null=True, blank=True, help_text="Precipitation rate in mm/h."
    )
    timestamp = models.DateTimeField(
        help_text="Timestamp when the sensor data was recorded."
    )
    color = models.CharField(null=True, blank=True, default="#dba800", max_length=7)
    courbe_name = models.CharField(null=True, blank=True, default="name", max_length=50)

    @property
    def default_unit(self) -> str:
        return "mm/h"

    @property
    def available_units(self) -> List[str]:
        return ["mm/h"]


class HumidityWeather(_SensorBase):
    zone = models.ForeignKey(
        "analytics.Zone", on_delete=models.CASCADE, related_name="humidity_weather"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="humidity_weather_per_user"
    )
    value = models.FloatField(
        null=True,
        blank=True,
        help_text="Humidity from the weather sensor as a percentage.",
    )
    timestamp = models.DateTimeField()

    @property
    def default_unit(self) -> str:
        return "%"

    @property
    def available_units(self) -> List[str]:
        return ["%"]


class WindSpeed(_SensorBase):
    zone = models.ForeignKey("analytics.Zone", on_delete=models.CASCADE, related_name="wind_speeds")
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="wind_speeds_per_user"
    )
    value = models.FloatField(null=True, blank=True, help_text="Wind speed in m/s.")
    timestamp = models.DateTimeField()

    @property
    def default_unit(self) -> str:
        return "m/s"

    @property
    def available_units(self) -> List[str]:
        return ["m/s", "km/h"]


class SolarRadiation(_SensorBase):
    zone = models.ForeignKey(
        "analytics.Zone", on_delete=models.CASCADE, related_name="solar_radiations"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="solar_radiations_per_user"
    )
    value = models.FloatField(
        null=True, blank=True, help_text="Solar radiation in W/m²."
    )
    timestamp = models.DateTimeField()

    @property
    def default_unit(self) -> str:
        return "W/m²"

    @property
    def available_units(self) -> List[str]:
        return ["W/m²"]  # mj/m2 add


class PressureWeather(_SensorBase):
    zone = models.ForeignKey(
        "analytics.Zone", on_delete=models.CASCADE, related_name="pressure_weather"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="pressure_weather_per_user"
    )
    value = models.FloatField(
        null=True, blank=True, help_text="Atmospheric pressure in hPa."
    )
    timestamp = models.DateTimeField()

    @property
    def default_unit(self) -> str:
        return "hPa"

    @property
    def available_units(self) -> List[str]:
        return ["hPa", "bar", "kpa"]


class WindDirection(_SensorBase):
    zone = models.ForeignKey(
        "analytics.Zone", on_delete=models.CASCADE, related_name="wind_directions"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="wind_directions_per_user"
    )
    value = models.FloatField(
        null=True, blank=True, help_text="Wind direction in degrees."
    )
    timestamp = models.DateTimeField()

    @property
    def default_unit(self) -> str:
        return "°"

    @property
    def available_units(self) -> List[str]:
        return ["°"]


class TemperatureWeather(_SensorBase):
    zone = models.ForeignKey(
        "analytics.Zone", on_delete=models.CASCADE, related_name="temperature_weather"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="temperature_weather_per_user"
    )
    value = models.FloatField(
        null=True, blank=True, help_text="Air temperature in Celsius."
    )
    timestamp = models.DateTimeField()

    @property
    def default_unit(self) -> str:
        return "°C"

    @property
    def available_units(self) -> List[str]:
        return ["°C", "°F"]


class ECSoilMedium(_SensorBase):
    zone = models.ForeignKey(
        "analytics.Zone", on_delete=models.CASCADE, related_name="ec_soil_medium"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="ec_soil_medium_per_user"
    )
    value = models.FloatField(
        null=True,
        blank=True,
        help_text="Electrical conductivity of soil at medium depth in dS/m.",
    )
    timestamp = models.DateTimeField()

    @property
    def default_unit(self) -> str:
        return "dS/m"  # default is us/cm

    @property
    def available_units(self) -> List[str]:
        return ["dS/m"]  # add ms/cm


class ECSoilHigh(_SensorBase):
    zone = models.ForeignKey(
        "analytics.Zone", on_delete=models.CASCADE, related_name="soil_ec_high"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="soil_ec_high_per_user"
    )
    value = models.FloatField(
        null=True,
        blank=True,
        help_text="Electrical conductivity of soil at high depth in dS/m.",
    )
    timestamp = models.DateTimeField()

    @property
    def default_unit(self) -> str:
        return "dS/m"  # the same of ECSoilMedium

    @property
    def available_units(self) -> List[str]:
        return ["dS/m"]  # the same


class ECSoilLow(_SensorBase):
    zone = models.ForeignKey("analytics.Zone", on_delete=models.CASCADE, related_name="ec_soil_low")
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="ec_soil_low_per_user"
    )
    value = models.FloatField(
        null=True,
        blank=True,
        help_text="Electrical conductivity of soil at low depth in dS/m.",
    )
    timestamp = models.DateTimeField()

    @property
    def default_unit(self) -> str:
        return "dS/m"  # the same

    @property
    def available_units(self) -> List[str]:
        return ["dS/m"]  # the same


class SoilMoistureMedium(_SensorBase):
    zone = models.ForeignKey(
        "analytics.Zone", on_delete=models.CASCADE, related_name="soil_moisture_medium"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="soil_moisture_medium_per_user"
    )
    value = models.FloatField(
        null=True, blank=True, help_text="Soil moisture at medium depth in percentage."
    )
    timestamp = models.DateTimeField()

    @property
    def default_unit(self) -> str:
        return "%"

    @property
    def available_units(self) -> List[str]:
        return ["%"]


class SoilMoistureHigh(_SensorBase):
    zone = models.ForeignKey(
        "analytics.Zone", on_delete=models.CASCADE, related_name="soil_moisture_high"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="soil_moisture_high_per_user"
    )
    value = models.FloatField(
        null=True, blank=True, help_text="Soil moisture at high depth in percentage."
    )
    timestamp = models.DateTimeField()

    @property
    def default_unit(self) -> str:
        return "%"

    @property
    def available_units(self) -> List[str]:
        return ["%"]


class SoilMoistureLow(_SensorBase):
    zone = models.ForeignKey(
        "analytics.Zone", on_delete=models.CASCADE, related_name="soil_moisture_low"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="soil_moisture_low_per_user"
    )
    value = models.FloatField(
        null=True, blank=True, help_text="Soil moisture at low depth in percentage."
    )
    timestamp = models.DateTimeField()

    @property
    def default_unit(self) -> str:
        return "%"

    @property
    def available_units(self) -> List[str]:
        return ["%"]


class PhSoil(_SensorBase):
    zone = models.ForeignKey("analytics.Zone", on_delete=models.CASCADE, related_name="ph_soil")
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="ph_soil_per_user"
    )
    value = models.FloatField(null=True, blank=True, help_text="Soil pH level.")
    timestamp = models.DateTimeField()

    @property
    def default_unit(self) -> str:
        return "pH"

    @property
    def available_units(self) -> List[str]:
        return ["pH"]


class SoilTemperatureLow(_SensorBase):
    zone = models.ForeignKey(
        "analytics.Zone", on_delete=models.CASCADE, related_name="soil_temperature_low"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="soil_temperature_low_per_user"
    )
    value = models.FloatField(
        null=True, blank=True, help_text="Soil temperature at low depth in Celsius."
    )
    timestamp = models.DateTimeField()

    @property
    def default_unit(self) -> str:
        return "°C"

    @property
    def available_units(self) -> List[str]:
        return ["°C", "°F"]


class SoilTemperatureMedium(_SensorBase):
    zone = models.ForeignKey(
        "analytics.Zone", on_delete=models.CASCADE, related_name="soil_temperature_medium"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="soil_temperature_medium_per_user"
    )
    value = models.FloatField(
        null=True, blank=True, help_text="Soil temperature at medium depth in Celsius."
    )
    timestamp = models.DateTimeField()

    @property
    def default_unit(self) -> str:
        return "°C"

    @property
    def available_units(self) -> List[str]:
        return ["°C", "°F"]


class SoilTemperatureHigh(_SensorBase):
    zone = models.ForeignKey(
        "analytics.Zone", on_delete=models.CASCADE, related_name="soil_temperature_high"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="soil_temperature_high_per_user"
    )
    value = models.FloatField(
        null=True, blank=True, help_text="Soil temperature at high depth in Celsius."
    )
    timestamp = models.DateTimeField()

    @property
    def default_unit(self) -> str:
        return "°C"

    @property
    def available_units(self) -> List[str]:
        return ["°C", "°F"]


class WaterFlowSensor(_SensorBase):
    zone = models.ForeignKey(
        "analytics.Zone", on_delete=models.CASCADE, related_name="water_flow_sensors"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="water_flow_sensors_per_user"
    )
    value = models.FloatField(
        null=True,
        blank=True,
        help_text="Water flow sensor reading in liters per second.",
    )
    timestamp = models.DateTimeField()

    @property
    def default_unit(self) -> str:
        return "m³/h"

    @property
    def available_units(self) -> List[str]:
        return ["L/s", "m³/h"]  # l/min hayd l/s


class WaterPressureSensor(_SensorBase):
    zone = models.ForeignKey(
        "analytics.Zone", on_delete=models.CASCADE, related_name="water_pressure_sensors"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="water_pressure_sensors_per_user"
    )
    value = models.FloatField(
        null=True,
        blank=True,
        help_text="Water pressure sensor reading in bar per second.",
    )
    timestamp = models.DateTimeField()

    @property
    def default_unit(self) -> str:
        return "Bar/s"  # bar

    @property
    def available_units(self) -> List[str]:
        return ["Bar/s"]


class WaterECSensor(_SensorBase):
    zone = models.ForeignKey(
        "analytics.Zone", on_delete=models.CASCADE, related_name="water_ec_sensors"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="water_ec_sensors_per_user"
    )
    value = models.FloatField(
        null=True, blank=True, help_text="Water EC sensor reading in μS/cm."
    )
    timestamp = models.DateTimeField()

    @property
    def default_unit(self) -> str:
        return "μS/cm"

    @property
    def available_units(self) -> List[str]:
        return ["μS/cm", "mS/cm"]


class PhWaterSensor(_SensorBase):
    zone = models.ForeignKey(
        "analytics.Zone", on_delete=models.CASCADE, related_name="ph_water_sensors"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="ph_water_sensors_per_user"
    )
    value = models.FloatField(null=True, blank=True, help_text="pH level of water.")
    timestamp = models.DateTimeField()

    @property
    def default_unit(self) -> str:
        return "pH"

    @property
    def available_units(self) -> List[str]:
        return ["pH"]


class ElectricityConsumptionSensor(_SensorBase):
    zone = models.ForeignKey(
        "analytics.Zone", on_delete=models.CASCADE, related_name="electricity_consumption_sensors"
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="electricity_consumption_sensors_per_user",
    )
    value = models.FloatField(
        null=True, blank=True, help_text="Electricity consumption reading."
    )
    timestamp = models.DateTimeField()

    @property
    def default_unit(self) -> str:
        return "kWh"

    @property
    def available_units(self) -> List[str]:
        return ["kWh", "Wh"]  # not preority don't change it


class LeafMoistureSensor(_SensorBase):
    zone = models.ForeignKey(
        "analytics.Zone", on_delete=models.CASCADE, related_name="leaf_moisture_sensors"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="leaf_moisture_sensors_per_user"
    )
    value = models.FloatField(
        null=True, blank=True, help_text="Leaf moisture percentage."
    )
    timestamp = models.DateTimeField()

    @property
    def default_unit(self) -> str:
        return "%"

    @property
    def available_units(self) -> List[str]:
        return ["%"]


class LeafTemperatureSensor(_SensorBase):
    zone = models.ForeignKey(
        "analytics.Zone", on_delete=models.CASCADE, related_name="leaf_tempeartue_sensors"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="leaf_tempeartue_sensors_per_user"
    )
    value = models.FloatField(
        null=True, blank=True, help_text="Leaf temperature in Celsius."
    )
    timestamp = models.DateTimeField()

    @property
    def default_unit(self) -> str:
        return "°C"

    @property
    def available_units(self) -> List[str]:
        return ["C", "°F"]


class MultiDepthSoilMoistureSensor(_SensorBase):
    zone = models.ForeignKey(
        "analytics.Zone", on_delete=models.CASCADE, related_name="multi_depth_soil_moisture_sensors"
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="multi_depth_soil_moisture_sensors_per_user",
    )
    value = models.FloatField(
        null=True, blank=True, help_text="Multi-depth soil moisture percentage."
    )
    timestamp = models.DateTimeField()

    @property
    def default_unit(self) -> str:
        return "%"

    @property
    def available_units(self) -> List[str]:
        return ["%"]


class LargeFruitDiameterSensor(_SensorBase):
    zone = models.ForeignKey(
        "analytics.Zone", on_delete=models.CASCADE, related_name="large_fruit_diameter_sensors"
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="large_fruit_diameter_sensors_per_user",
    )
    value = models.FloatField(
        null=True, blank=True, help_text="Large fruit diameter size."
    )
    timestamp = models.DateTimeField()

    @property
    def default_unit(self) -> str:
        return "mm"

    @property
    def available_units(self) -> List[str]:
        return ["mm", "cm"]


class WaterLevelSensor(_SensorBase):
    zone = models.ForeignKey(
        "analytics.Zone", on_delete=models.CASCADE, related_name="water_level_sensors"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="water_level_sensors_per_user"
    )
    value = models.FloatField(
        null=True, blank=True, help_text="Water level in the tank or river."
    )
    timestamp = models.DateTimeField()

    @property
    def default_unit(self) -> str:
        return "cm"

    @property
    def available_units(self) -> List[str]:
        return ["cm", "m"]


class SoilSalinitySensor(_SensorBase):
    zone = models.ForeignKey(
        "analytics.Zone", on_delete=models.CASCADE, related_name="soil_salinity_sensors"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="soil_salinity_sensors_per_user"
    )
    value = models.FloatField(null=True, blank=True, help_text="Soil salinity value.")
    timestamp = models.DateTimeField()

    @property
    def default_unit(self) -> str:
        return "mg/m"  # default mg/l

    @property
    def available_units(self) -> List[str]:
        return ["dS/m", "mS/cm"]

    def __str__(self):
        return f"Salinity at {self.timestamp} — {self.value} {self.default_unit}"


class SoilConductivitySensor(_SensorBase):
    zone = models.ForeignKey(
        "analytics.Zone", on_delete=models.CASCADE, related_name="soil_conductivity_sensors"
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="soil_conductivity_sensors_per_user",
    )
    value = models.FloatField(
        null=True, blank=True, help_text="Soil electrical conductivity value."
    )
    timestamp = models.DateTimeField()

    @property
    def default_unit(self) -> str:
        return "μS/cm"

    @property
    def available_units(self) -> List[str]:
        return ["μS/cm", "mS/cm"]

    def __str__(self):
        return f"Conductivity at {self.timestamp} — {self.value} {self.default_unit}"


class NpkSensor(_SensorBase):
    zone = models.ForeignKey("analytics.Zone", on_delete=models.CASCADE, related_name="npk_sensors")
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="npk_sensors_per_user"
    )
    timestamp = models.DateTimeField()

    # Nitrogen
    nitrogen_value = models.FloatField(
        null=True, blank=True, help_text="Nitrogen reading (N)."
    )
    nitrogen_color = models.CharField(
        null=True, blank=True, default="#dba800", max_length=7
    )
    nitrogen_courbe_name = models.CharField(
        null=True, blank=True, default="N_curve", max_length=50
    )

    # Phosphorus
    phosphorus_value = models.FloatField(
        null=True, blank=True, help_text="Phosphorus reading (P)."
    )
    phosphorus_color = models.CharField(
        null=True, blank=True, default="#00a86b", max_length=7
    )
    phosphorus_courbe_name = models.CharField(
        null=True, blank=True, default="P_curve", max_length=50
    )

    # Potassium
    potassium_value = models.FloatField(
        null=True, blank=True, help_text="Potassium reading (K)."
    )
    potassium_color = models.CharField(
        null=True, blank=True, default="#4682b4", max_length=7
    )
    potassium_courbe_name = models.CharField(
        null=True, blank=True, default="K_curve", max_length=50
    )

    @property
    def default_unit(self) -> str:
        return "mg/kg"

    @property
    def available_units(self) -> List[str]:
        return ["mg/kg", "ppm"]


class FruitSizeSensor(_SensorBase):
    zone = models.ForeignKey(
        "analytics.Zone", on_delete=models.CASCADE, related_name="fruit_size_sensors"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="fruit_size_sensors_per_user"
    )
    value = models.FloatField(
        null=True, blank=True, help_text="Fruit size measurement."
    )
    timestamp = models.DateTimeField()

    @property
    def default_unit(self) -> str:
        return "mm"

    @property
    def available_units(self) -> List[str]:
        return ["mm", "cm"]


class EcSalinitySensor(_SensorBase):
    zone = models.ForeignKey(
        "analytics.Zone", on_delete=models.CASCADE, related_name="ec_salinity_sensors"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="ec_salinity_sensors_per_user"
    )
    value = models.FloatField(
        null=True, blank=True, help_text="EC and salinity measurement."
    )
    timestamp = models.DateTimeField()

    @property
    def default_unit(self) -> str:
        return "μS/cm"

    @property
    def available_units(self) -> List[str]:
        return ["μS/cm", "dS/m"]


class SensorColor(_SensorBase):
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="user_sensor_colors"
    )
    # zone = models.ForeignKey("analytics.Zone", on_delete=models.CASCADE, related_name="zone_sensor_colors")
    zone = models.ForeignKey(
        "analytics.Zone",
        on_delete=models.CASCADE,
        related_name="_graph_names",
        null=True,
        blank=True,
    )

    # Weather-related colors
    precipitation_rate_color = models.CharField(max_length=7, default="#3D8D7A")  # Teal
    humidity_weather_color = models.CharField(max_length=7, default="#2A6F97")  # Blue
    wind_speed_color = models.CharField(
        max_length=7, default="#FFB703"
    )  # Yellow-Orange
    solar_radiation_color = models.CharField(max_length=7, default="#E63946")  # Red
    pressure_weather_color = models.CharField(
        max_length=7, default="#F4A261"
    )  # Light Orange
    wind_direction_color = models.CharField(max_length=7, default="#6A0572")  # Purple
    temperature_weather_color = models.CharField(
        max_length=7, default="#E76F51"
    )  # Warm Red

    # Soil-related colors
    et0_color = models.CharField(max_length=7, default="#497D74")  # Greenish-Blue
    ec_soil_medium_color = models.CharField(
        max_length=7, default="#2A9D8F"
    )  # Greenish-Blue
    soil_temperature_medium_color = models.CharField(
        max_length=7, default="#264653"
    )  # Dark Cyan
    soil_ec_high_color = models.CharField(
        max_length=7, default="#8A2BE2"
    )  # Blue-Violet
    ec_soil_low_color = models.CharField(
        max_length=7, default="#D4A373"
    )  # Earthy Beige
    soil_moisture_medium_color = models.CharField(
        max_length=7, default="#6D597A"
    )  # Muted Purple
    soil_moisture_high_color = models.CharField(
        max_length=7, default="#C8553D"
    )  # Deep Orange
    soil_moisture_low_color = models.CharField(
        max_length=7, default="#457B9D"
    )  # Soft Blue
    ph_soil_color = models.CharField(max_length=7, default="#023E8A")  # Deep Blue
    soil_temperature_low_color = models.CharField(
        max_length=7, default="#8D99AE"
    )  # Soft Gray-Blue
    soil_temperature_high_color = models.CharField(
        max_length=7, default="#E9C46A"
    )  # Golden Yellow

    def __str__(self):
        return f"Graph colors for {self.user.username}"


class SensorLocation(_SensorBase):
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="user_sensor_locations"
    )
    zone = models.ForeignKey(
        "analytics.Zone", on_delete=models.CASCADE, related_name="zone_sensor_locations"
    )

    # Weather-related sensor locations
    precipitation_rate_longitude = models.FloatField()
    precipitation_rate_latitude = models.FloatField()

    humidity_weather_longitude = models.FloatField()
    humidity_weather_latitude = models.FloatField()

    wind_speed_longitude = models.FloatField()
    wind_speed_latitude = models.FloatField()

    solar_radiation_longitude = models.FloatField()
    solar_radiation_latitude = models.FloatField()

    pressure_weather_longitude = models.FloatField()
    pressure_weather_latitude = models.FloatField()

    wind_direction_longitude = models.FloatField()
    wind_direction_latitude = models.FloatField()

    temperature_weather_longitude = models.FloatField()
    temperature_weather_latitude = models.FloatField()

    # Soil-related sensor locations
    et0_longitude = models.FloatField()
    et0_latitude = models.FloatField()

    ec_soil_medium_longitude = models.FloatField()
    ec_soil_medium_latitude = models.FloatField()

    soil_temperature_medium_longitude = models.FloatField()
    soil_temperature_medium_latitude = models.FloatField()

    soil_ec_high_longitude = models.FloatField()
    soil_ec_high_latitude = models.FloatField()

    ec_soil_low_longitude = models.FloatField()
    ec_soil_low_latitude = models.FloatField()

    soil_moisture_medium_longitude = models.FloatField()
    soil_moisture_medium_latitude = models.FloatField()

    soil_moisture_high_longitude = models.FloatField()
    soil_moisture_high_latitude = models.FloatField()

    soil_moisture_low_longitude = models.FloatField()
    soil_moisture_low_latitude = models.FloatField()

    ph_soil_longitude = models.FloatField()
    ph_soil_latitude = models.FloatField()

    soil_temperature_low_longitude = models.FloatField()
    soil_temperature_low_latitude = models.FloatField()

    soil_temperature_high_longitude = models.FloatField()
    soil_temperature_high_latitude = models.FloatField()

    def __str__(self):
        return f"Sensor locations for {self.user.username} in {self.zone.name}"


class VPDWeather(_SensorBase):
    zone = models.ForeignKey("analytics.Zone", on_delete=models.CASCADE, related_name="vpd_weather")
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="vpd_weather_per_user"
    )
    value = models.FloatField(
        null=True, blank=True, help_text="Vapor Pressure Deficit in kPa."
    )
    timestamp = models.DateTimeField()

    @property
    def default_unit(self) -> str:
        return "kPa"

    def __str__(self):
        return f"VPD ({self.value} kPa) at {self.timestamp} in Zone {self.zone_id}"


# ---------------------------------------------------------------------------
# Admin-side: per-user sensor unit preference + manager affirmation workflow
# ---------------------------------------------------------------------------


class UserSensorUnitPreference(_SensorBase):
    """Persisted choice of unit per sensor_key for a given user.

    Replaces the localStorage-only path in `SuperAdminUsersSettings` so
    units stay consistent across devices.
    """

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="sensor_unit_preferences",
    )
    sensor_key = models.CharField(max_length=64)
    unit = models.CharField(max_length=32)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "analytics"
        unique_together = [("user", "sensor_key")]
        indexes = [models.Index(fields=["user", "sensor_key"])]

    def __str__(self):
        return f"{self.user_id}:{self.sensor_key}={self.unit}"


