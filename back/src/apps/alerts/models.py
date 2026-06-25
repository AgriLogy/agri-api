"""Alert-domain ORM models — extracted from analytics/models.py (Phase 5).

Holds the user-facing notification record and the plug-and-play alert
rule. Every concrete model inherits from ``_AlertBase``, which sets
``Meta.app_label = "analytics"``. That keeps the historical
``analytics_<modelname>`` db_table and ``analytics.<Model>`` FK references
valid without a data migration — only the Python module moves; the Django
app registry stays unchanged.

The alert *engine* (threshold evaluation, ingest-time dispatch, the
sensor-key registry) lives in ``analytics/alerts.py`` — a Django adapter
around ``agri.core.alerts``. Only the ORM rows live here.

``analytics/models.py`` re-exports these classes, so existing
``from analytics.models import Alert`` imports keep working.
"""

from datetime import datetime

from django.conf import settings
from django.db import models

User = settings.AUTH_USER_MODEL


class _AlertBase(models.Model):
    class Meta:
        abstract = True
        app_label = "analytics"


class Notification(_AlertBase):
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


class Alert(_AlertBase):
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
        "analytics.Zone",
        on_delete=models.CASCADE,
        related_name="zone_alerts",
        null=True,
        blank=True,
    )
    is_active = models.BooleanField(default=True)
    # Per-alert delivery channels. Email is the legacy default; WhatsApp (via
    # Twilio) is opt-in. Schema lives in agri-db (migration
    # c4d8e1f02a37_add_alert_notify_channels).
    notify_email = models.BooleanField(default=True)
    notify_whatsapp = models.BooleanField(default=False)
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
