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
    # Stamped every time an alert email is actually dispatched. Distinct from
    # ``last_triggered_at`` (which is "first fire ever, for chart overlays")
    # so the per-sensor grace-period gate in ``dispatch_alerts_for_reading``
    # has a dedicated cursor it can bump on every notification.
    last_emailed_at = models.DateTimeField(null=True, blank=True)
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



# Re-exports of sensor models (Phase 5d). The classes now live in
# apps/sensors/models.py with app_label="analytics". This re-export keeps
# `from analytics.models import <Sensor>` working until callers migrate
# in the Phase 5d follow-up PR.
from apps.sensors.models import (
    ECSoilHigh,
    ECSoilLow,
    ECSoilMedium,
    EcSalinitySensor,
    ElectricityConsumptionSensor,
    Et0Calculated,
    Et0Weather,
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
    SensorColor,
    SensorLocation,
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
    UserSensorUnitPreference,
    VPDWeather,
    WaterECSensor,
    WaterFlowSensor,
    WaterLevelSensor,
    WaterPressureSensor,
    WindDirection,
    WindSpeed,
)
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


@receiver(post_save, sender=User)
def create_graph_names(sender, instance, created, **kwargs):
    """Auto-create the per-user GraphName and SensorColor rows on user
    create. Lives here (not in apps/sensors/models.py) because GraphName
    is owned by analytics; SensorColor is re-exported above.
    """
    if created:
        GraphName.objects.create(user=instance)
        SensorColor.objects.create(user=instance)


from django.conf import settings
from django.db import models

from .models import Zone  # if inside same app

User = settings.AUTH_USER_MODEL


class ManagerAffirmation(models.Model):
    """A pending decision that a non-admin user needs an admin to approve.

    `payload` carries the action-specific data (zone id, new param
    values, etc.) — JSON, so new action types add without a migration.
    """

    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
    ]

    ACTION_PARAM_CHANGE = "zone_params_change"
    ACTION_USER_REACTIVATE = "user_reactivate"
    ACTION_KC_PERIODS = "kc_periods_change"
    ACTION_CHOICES = [
        (ACTION_PARAM_CHANGE, "Zone params change"),
        (ACTION_KC_PERIODS, "Kc periods change"),
        (ACTION_USER_REACTIVATE, "User reactivate"),
    ]

    requested_by = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="affirmations_requested",
    )
    action = models.CharField(max_length=64, choices=ACTION_CHOICES)
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING
    )
    decided_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name="affirmations_decided",
        null=True,
        blank=True,
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["requested_by"]),
        ]

    def __str__(self):
        return f"Affirmation #{self.pk} ({self.action}/{self.status})"
