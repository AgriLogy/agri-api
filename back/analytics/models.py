from datetime import datetime

from django.conf import settings
from django.contrib.auth.models import User
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver

User = settings.AUTH_USER_MODEL

from typing import List


class Notification(models.Model):
    yesterday_temperature = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        help_text="Temperature recorded yesterday in Celsius.",
    )
    today_temperature = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        help_text="Temperature recorded today in Celsius.",
    )
    yesterday_humidity = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        help_text="Humidity recorded yesterday as a percentage.",
    )
    today_humidity = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        help_text="Humidity recorded today as a percentage.",
    )
    ET0 = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        help_text="Reference evapotranspiration in mm/day.",
    )
    soil_humidity = models.DecimalField(
        max_digits=5, decimal_places=2, help_text="Soil humidity percentage."
    )
    soil_temperature = models.DecimalField(
        max_digits=5, decimal_places=2, help_text="Soil temperature in Celsius."
    )
    soil_ph = models.DecimalField(
        max_digits=4, decimal_places=2, help_text="Soil pH level."
    )
    perfect_irrigation_period = models.CharField(
        max_length=100, help_text="Ideal time period for irrigation."
    )
    last_irrigation_date = models.DateField(help_text="Date of the last irrigation.")
    last_start_irrigation_hour = models.TimeField(
        help_text="Start time of the last irrigation."
    )
    last_finish_irrigation_hour = models.TimeField(
        help_text="Finish time of the last irrigation."
    )
    used_water_irrigation = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        help_text="Water used in the last irrigation in liters.",
    )
    notification_date = models.DateTimeField(
        default=datetime.now,
        help_text="Date and time when the notification was created.",
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="user_notifications",
        null=True,
        blank=True,
    )

    def __str__(self):
        return f"Alert on {self.last_irrigation_date} (Notification sent on {self.notification_date})"


class Alert(models.Model):
    GREATER_THAN = ">"
    LESS_THAN = "<"
    EQUAL_TO = "="

    CONDITION_CHOICES = [
        (GREATER_THAN, "Greater Than"),
        (LESS_THAN, "Less Than"),
        (EQUAL_TO, "Equal To"),
    ]

    A_P = "Pressure"
    A_F = "Flow"
    A_WT = "Weather Temperature"
    A_WS = "Wind Speed"
    A_RF = "Rain Fall"
    A_EC = "EC (Electrical Conductivity)"
    A_PH = "pH Level"
    A_H = "Humidity"
    A_ST = "Soil Temperature"
    A_PM = "Periodic maintenance"

    ALERT_CHOICES = [
        (A_P, "Pressure"),
        (A_F, "Flow"),
        (A_WT, "Weather Temperature"),
        (A_WS, "Wind Speed"),
        (A_RF, "Rain Fall"),
        (A_PM, "Periodic maintenance"),
        (A_EC, "EC (Electrical Conductivity)"),
        (A_PH, "pH Level"),
        (A_H, "Humidity"),
        (A_ST, "Soil Temperature"),
    ]

    name = models.CharField(max_length=200, help_text="A brief name for the alert.")
    type = models.CharField(
        max_length=50,
        choices=ALERT_CHOICES,
    )
    description = models.TextField(
        help_text="Detailed description of the alert.", blank=True, default=""
    )

    condition = models.CharField(
        max_length=1,
        choices=CONDITION_CHOICES,
        help_text="The condition for this alert (>, <, =)",
    )
    condition_nbr = models.DecimalField(max_digits=10, decimal_places=2)

    # Plug-and-play fields: a stable sensor_key lets any chart on the front
    # look up the alerts that apply to it without caring about the legacy
    # `type` enum. zone is optional — when null the alert is user-wide.
    sensor_key = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Stable sensor identifier for chart overlays (e.g. 'temperature_weather').",
    )
    zone = models.ForeignKey(
        "Zone",
        on_delete=models.CASCADE,
        related_name="zone_alerts",
        null=True,
        blank=True,
    )
    is_active = models.BooleanField(default=True)
    last_triggered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="user_alerts",
        null=True,
        blank=True,
    )

    def __str__(self):
        return f"{self.name} - {self.condition}"


class Zone(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="zones")

    name = models.CharField(max_length=100)
    space = models.FloatField(help_text="Area in square meters.")

    # soil parameters
    soil_param_TAW = models.FloatField(
        help_text="Total Available Water (TAW) in mm.", default=50
    )
    soil_param_FC = models.FloatField(help_text="Field Capacity (FC) in %.", default=50)
    soil_param_WP = models.FloatField(help_text="Wilting Point (WP) in %.", default=50)
    soil_param_RAW = models.FloatField(
        help_text="Readily Available Water (RAW) in mm.", default=50
    )

    critical_moisture_threshold = models.FloatField(
        help_text="Critical soil moisture threshold in %."
    )

    # irrigation parameters [pomp flow rate auto or manual ?????]
    pomp_flow_rate = models.FloatField(
        help_text="Pump flow rate in liters per second.", default=100
    )
    irrigation_water_quantity = models.FloatField(
        help_text="Irrigation water quantity in liters.", default=100
    )


class KcPeriod(models.Model):
    period_name = models.CharField(max_length=100)
    start_date = models.DateField()
    end_date = models.DateField()
    kc_value = models.FloatField(help_text="Kc value for this period.")

    def __str__(self):
        return f"{self.period_name} ({self.start_date} to {self.end_date})"


class Kc(models.Model):
    name = models.CharField(
        max_length=100, help_text="Name of the KC data set.", default=""
    )
    plant_name = models.CharField(max_length=100)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="kc_per_user",
    )
    zone = models.ForeignKey(
        Zone, on_delete=models.CASCADE, related_name="kc_values", null=True, blank=True
    )
    number_of_periods = models.IntegerField(
        help_text="Number of periods for which KC values are provided.", default=2
    )

    def __str__(self):
        return f"KC '{self.name}' for {self.plant_name} in Zone {self.zone.name} ({self.user.username})"


class KcPeriodAssignment(models.Model):
    kc = models.ForeignKey(Kc, on_delete=models.CASCADE, related_name="periods")
    period = models.ForeignKey(
        KcPeriod, on_delete=models.CASCADE, related_name="kc_assignments"
    )

    def __str__(self):
        return (
            f"Assignment of Period '{self.period.period_name}' to KC '{self.kc.name}'"
        )


class Et0Calculated(models.Model):
    zone = models.ForeignKey(
        Zone, on_delete=models.CASCADE, related_name="et0_calculated_weather"
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
class Et0Weather(models.Model):
    zone = models.ForeignKey(Zone, on_delete=models.CASCADE, related_name="et0_weather")
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
class PrecipitationRate(models.Model):
    zone = models.ForeignKey(
        Zone, on_delete=models.CASCADE, related_name="precipitation_rates"
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


class HumidityWeather(models.Model):
    zone = models.ForeignKey(
        Zone, on_delete=models.CASCADE, related_name="humidity_weather"
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


class WindSpeed(models.Model):
    zone = models.ForeignKey(Zone, on_delete=models.CASCADE, related_name="wind_speeds")
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


class SolarRadiation(models.Model):
    zone = models.ForeignKey(
        Zone, on_delete=models.CASCADE, related_name="solar_radiations"
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


class PressureWeather(models.Model):
    zone = models.ForeignKey(
        Zone, on_delete=models.CASCADE, related_name="pressure_weather"
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


class WindDirection(models.Model):
    zone = models.ForeignKey(
        Zone, on_delete=models.CASCADE, related_name="wind_directions"
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


class TemperatureWeather(models.Model):
    zone = models.ForeignKey(
        Zone, on_delete=models.CASCADE, related_name="temperature_weather"
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


class ECSoilMedium(models.Model):
    zone = models.ForeignKey(
        Zone, on_delete=models.CASCADE, related_name="ec_soil_medium"
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


class ECSoilHigh(models.Model):
    zone = models.ForeignKey(
        Zone, on_delete=models.CASCADE, related_name="soil_ec_high"
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


class ECSoilLow(models.Model):
    zone = models.ForeignKey(Zone, on_delete=models.CASCADE, related_name="ec_soil_low")
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


class SoilMoistureMedium(models.Model):
    zone = models.ForeignKey(
        Zone, on_delete=models.CASCADE, related_name="soil_moisture_medium"
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


class SoilMoistureHigh(models.Model):
    zone = models.ForeignKey(
        Zone, on_delete=models.CASCADE, related_name="soil_moisture_high"
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


class SoilMoistureLow(models.Model):
    zone = models.ForeignKey(
        Zone, on_delete=models.CASCADE, related_name="soil_moisture_low"
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


class PhSoil(models.Model):
    zone = models.ForeignKey(Zone, on_delete=models.CASCADE, related_name="ph_soil")
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


class SoilTemperatureLow(models.Model):
    zone = models.ForeignKey(
        Zone, on_delete=models.CASCADE, related_name="soil_temperature_low"
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


class SoilTemperatureMedium(models.Model):
    zone = models.ForeignKey(
        Zone, on_delete=models.CASCADE, related_name="soil_temperature_medium"
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


class SoilTemperatureHigh(models.Model):
    zone = models.ForeignKey(
        Zone, on_delete=models.CASCADE, related_name="soil_temperature_high"
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


class WaterFlowSensor(models.Model):
    zone = models.ForeignKey(
        Zone, on_delete=models.CASCADE, related_name="water_flow_sensors"
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


class WaterPressureSensor(models.Model):
    zone = models.ForeignKey(
        Zone, on_delete=models.CASCADE, related_name="water_pressure_sensors"
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


class WaterECSensor(models.Model):
    zone = models.ForeignKey(
        Zone, on_delete=models.CASCADE, related_name="water_ec_sensors"
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


class PhWaterSensor(models.Model):
    zone = models.ForeignKey(
        Zone, on_delete=models.CASCADE, related_name="ph_water_sensors"
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


class ElectricityConsumptionSensor(models.Model):
    zone = models.ForeignKey(
        Zone, on_delete=models.CASCADE, related_name="electricity_consumption_sensors"
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


class LeafMoistureSensor(models.Model):
    zone = models.ForeignKey(
        Zone, on_delete=models.CASCADE, related_name="leaf_moisture_sensors"
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


class LeafTemperatureSensor(models.Model):
    zone = models.ForeignKey(
        Zone, on_delete=models.CASCADE, related_name="leaf_tempeartue_sensors"
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


class MultiDepthSoilMoistureSensor(models.Model):
    zone = models.ForeignKey(
        Zone, on_delete=models.CASCADE, related_name="multi_depth_soil_moisture_sensors"
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


class LargeFruitDiameterSensor(models.Model):
    zone = models.ForeignKey(
        Zone, on_delete=models.CASCADE, related_name="large_fruit_diameter_sensors"
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


class WaterLevelSensor(models.Model):
    zone = models.ForeignKey(
        Zone, on_delete=models.CASCADE, related_name="water_level_sensors"
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


class SoilSalinitySensor(models.Model):
    zone = models.ForeignKey(
        Zone, on_delete=models.CASCADE, related_name="soil_salinity_sensors"
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


class SoilConductivitySensor(models.Model):
    zone = models.ForeignKey(
        Zone, on_delete=models.CASCADE, related_name="soil_conductivity_sensors"
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


class NpkSensor(models.Model):
    zone = models.ForeignKey(Zone, on_delete=models.CASCADE, related_name="npk_sensors")
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


class FruitSizeSensor(models.Model):
    zone = models.ForeignKey(
        Zone, on_delete=models.CASCADE, related_name="fruit_size_sensors"
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


class EcSalinitySensor(models.Model):
    zone = models.ForeignKey(
        Zone, on_delete=models.CASCADE, related_name="ec_salinity_sensors"
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


class SensorColor(models.Model):
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="user_sensor_colors"
    )
    # zone = models.ForeignKey(Zone, on_delete=models.CASCADE, related_name="zone_sensor_colors")
    zone = models.ForeignKey(
        Zone,
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


@receiver(post_save, sender=User)
def create_graph_names(sender, instance, created, **kwargs):
    if created:
        GraphName.objects.create(user=instance)
        SensorColor.objects.create(user=instance)


class SensorLocation(models.Model):
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="user_sensor_locations"
    )
    zone = models.ForeignKey(
        Zone, on_delete=models.CASCADE, related_name="zone_sensor_locations"
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


class GraphName(models.Model):
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="user_graph_names"
    )
    # zone = models.ForeignKey(Zone, on_delete=models.CASCADE, related_name="zone_graph_names")
    zone = models.ForeignKey(
        Zone,
        on_delete=models.CASCADE,
        related_name="zone_graph_names",
        null=True,
        blank=True,
    )

    soil_irrigation = models.CharField(max_length=40, default="Irrigation du sol")
    soil_ph = models.CharField(max_length=40, default="pH du sol")
    soil_conductivity = models.CharField(max_length=40, default="Conductivité du sol")
    soil_moisture = models.CharField(max_length=40, default="Humidité du sol")
    soil_temperature = models.CharField(max_length=40, default="Température du sol")

    et0 = models.CharField(max_length=40, default="Taux d'évapotranspiration")
    precipitation_rate = models.CharField(
        max_length=40, default="Taux de précipitation"
    )
    wind_speed = models.CharField(max_length=40, default="Vitesse du vent")
    solar_radiation = models.CharField(max_length=40, default="Rayonnement solaire")
    pressure_weather = models.CharField(max_length=40, default="Pression atmosphérique")
    wind_direction = models.CharField(max_length=40, default="Direction du vent")
    humidity_weather = models.CharField(max_length=40, default="Humidité de l'air")
    temperature_weather = models.CharField(
        max_length=40, default="Température de l'air"
    )
    temperature_humidity_weather = models.CharField(
        max_length=40, default="Température et humidité de l'air"
    )
    precipitation_humidity_rate = models.CharField(
        max_length=40, default="Taux de précipitation et humidité"
    )
    pluviometrie = models.CharField(max_length=40, default="Cumule de pluie tombée")
    data_table = models.CharField(max_length=40, default="Tableau de données")

    def __str__(self):
        return f"Noms des graphiques pour {self.user.username}"


class ActiveGraph(models.Model):
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="user_active_graph"
    )
    zone = models.ForeignKey(
        Zone, on_delete=models.CASCADE, related_name="zone_active_graph"
    )

    # --- Soil ---
    soil_irrigation_status = models.BooleanField(
        default=True, help_text="Statut d'irrigation du sol"
    )
    soil_ph_status = models.BooleanField(default=True, help_text="pH du sol")
    soil_conductivity_status = models.BooleanField(
        default=True, help_text="Conductivité du sol"
    )
    soil_moisture_status = models.BooleanField(
        default=True, help_text="Humidité du sol"
    )
    soil_temperature_status = models.BooleanField(
        default=True, help_text="Température du sol"
    )

    # --- Weather ---
    et0_status = models.BooleanField(
        default=True, help_text="Taux d'évapotranspiration (ET0)"
    )
    wind_speed_status = models.BooleanField(default=True, help_text="Vitesse du vent")
    wind_direction_status = models.BooleanField(
        default=True, help_text="Direction du vent"
    )
    solar_radiation_status = models.BooleanField(
        default=True, help_text="Rayonnement solaire"
    )
    temperature_humidity_weather_status = models.BooleanField(
        default=True, help_text="Température et humidité de l'air"
    )
    precipitation_humidity_rate_status = models.BooleanField(
        default=True, help_text="Taux de précipitation et humidité"
    )
    pluviometry_status = models.BooleanField(
        default=True, help_text="Cumul de précipitations"
    )
    data_table_status = models.BooleanField(
        default=True, help_text="Affichage du tableau de données"
    )

    # Missing weather fields added here:
    wind_radar_status = models.BooleanField(default=True, help_text="Radar du vent")
    cumulative_precipitation_status = models.BooleanField(
        default=True, help_text="Précipitations cumulatives"
    )
    precipitation_rate_status = models.BooleanField(
        default=True, help_text="Taux de précipitations"
    )
    weather_temperature_humidity_status = models.BooleanField(
        default=True, help_text="Température et humidité météo"
    )

    # --- Water ---
    water_flow_status = models.BooleanField(default=True, help_text="Débit d'eau")
    water_pressure_status = models.BooleanField(
        default=True, help_text="Pression d'eau"
    )
    water_ph_status = models.BooleanField(default=True, help_text="pH de l'eau")
    water_ec_status = models.BooleanField(
        default=True, help_text="Conductivité électrique de l'eau"
    )

    # --- Plant Sensors ---
    leaf_sensor_status = models.BooleanField(
        default=True, help_text="Capteur de feuille"
    )
    fruit_size_status = models.BooleanField(default=True, help_text="Taille des fruits")
    large_fruit_diameter_status = models.BooleanField(
        default=True, help_text="Diamètre des gros fruits"
    )

    # --- Fertilizer/Nutrients ---
    npk_status = models.BooleanField(default=True, help_text="Statut NPK")

    # --- Other ---
    electricity_consumption_status = models.BooleanField(
        default=True, help_text="Consommation électrique"
    )

    def __str__(self):
        return f"ActiveGraph for User {self.user.username} - Zone: {self.zone.name}"


# Auto-toggle every chart ON for any newly-created Zone so the dashboard
# renders out-of-the-box. Without this row, ActiveGraphSelfAPIView 404s
# and the front shows an empty page until someone manually flips the
# fields in /admin/. ActiveGraph fields all default to True, so simply
# creating the row is enough.
@receiver(post_save, sender=Zone)
def create_active_graph_for_zone(sender, instance, created, **kwargs):
    if not created:
        return
    ActiveGraph.objects.get_or_create(user=instance.user, zone=instance)
    GraphName.objects.get_or_create(user=instance.user, zone=instance)
    SensorColor.objects.get_or_create(user=instance.user, zone=instance)


from django.conf import settings
from django.db import models

from .models import Zone  # if inside same app

User = settings.AUTH_USER_MODEL


class VPDWeather(models.Model):
    zone = models.ForeignKey(Zone, on_delete=models.CASCADE, related_name="vpd_weather")
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
