"""Irrigation-domain ORM models — extracted from analytics/models.py (Phase 5).

Holds the zone + crop-coefficient + dashboard-config + approval-workflow
models. Every concrete model inherits from ``_IrrigationBase``, which sets
``Meta.app_label = "analytics"``. That keeps the historical
``analytics_<modelname>`` db_table and ``analytics.<Model>`` FK references
valid without a data migration — only the Python module moves; the Django
app registry stays unchanged.

``analytics/models.py`` re-exports these classes, so existing
``from analytics.models import Zone`` imports keep working.
"""

from datetime import datetime

from django.conf import settings
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver

User = settings.AUTH_USER_MODEL


class _IrrigationBase(models.Model):
    class Meta:
        abstract = True
        app_label = "analytics"


class Zone(_IrrigationBase):
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


class KcPeriod(_IrrigationBase):
    period_name = models.CharField(max_length=100)
    start_date = models.DateField()
    end_date = models.DateField()
    kc_value = models.FloatField(help_text="Kc value for this period.")

    def __str__(self):
        return f"{self.period_name} ({self.start_date} to {self.end_date})"


class Kc(_IrrigationBase):
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


class KcPeriodAssignment(_IrrigationBase):
    kc = models.ForeignKey(Kc, on_delete=models.CASCADE, related_name="periods")
    period = models.ForeignKey(
        KcPeriod, on_delete=models.CASCADE, related_name="kc_assignments"
    )

    def __str__(self):
        return (
            f"Assignment of Period '{self.period.period_name}' to KC '{self.kc.name}'"
        )


class GraphName(_IrrigationBase):
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="user_graph_names"
    )
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


class ActiveGraph(_IrrigationBase):
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


class ManagerAffirmation(_IrrigationBase):
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
        app_label = "analytics"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["requested_by"]),
        ]

    def __str__(self):
        return f"Affirmation #{self.pk} ({self.action}/{self.status})"


# ----- Signals -------------------------------------------------------------
#
# SensorColor lives in apps/sensors; import it lazily inside the handlers
# is unnecessary (apps.sensors loads with the app registry), but keep the
# import here at module level since apps.sensors has no dependency back on
# this module (no import cycle).
from apps.sensors.models import SensorColor


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
    create. GraphName is owned by this app; SensorColor lives in
    apps/sensors.
    """
    if created:
        GraphName.objects.create(user=instance)
        SensorColor.objects.create(user=instance)
