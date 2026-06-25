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
    elevation_m = models.FloatField(
        default=0,
        help_text="Elevation above sea level in metres (for clear-sky radiation Rso).",
    )


class TechnicianGrant(_IrrigationBase):
    """Links a technician login to the farm owner whose data it may read."""

    technician = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="technician_grants",
        db_constraint=False,
    )
    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="granted_technicians",
        db_constraint=False,
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)

    class Meta:
        app_label = "analytics"
        db_table = "analytics_techniciangrant"
        # Schema-of-record lives in agri-db; this table self-deploys on prod via
        # scripts/ensure_technician_tables.py and is created session-wide in the
        # test DB by conftest. managed=False keeps it out of the migration graph
        # (a real FK table broke the postgres CI flush — see the assistant model);
        # db_constraint=False above drops the DB-level FK so flush can TRUNCATE.
        managed = False
        constraints = [
            models.UniqueConstraint(
                fields=["technician", "owner"], name="uniq_technician_owner"
            )
        ]

    def __str__(self):
        return f"grant {self.technician_id}->{self.owner_id}"


class TechnicianZoneGrant(_IrrigationBase):
    """Per-zone scope under a grant: which zone + which graph keys are visible.

    ``allowed_graphs`` is a whitelist of ActiveGraph status keys (e.g.
    ``water_flow_status``); effective visibility = granted ∩ owner-enabled.
    """

    grant = models.ForeignKey(
        TechnicianGrant,
        on_delete=models.CASCADE,
        related_name="zone_grants",
        db_constraint=False,
    )
    zone = models.ForeignKey(
        "analytics.Zone",
        on_delete=models.CASCADE,
        related_name="technician_grants",
        db_constraint=False,
    )
    allowed_graphs = models.JSONField(default=list)

    class Meta:
        app_label = "analytics"
        db_table = "analytics_technicianzonegrant"
        # See TechnicianGrant.Meta — unmanaged, self-deployed, FK constraints off.
        managed = False
        constraints = [
            models.UniqueConstraint(fields=["grant", "zone"], name="uniq_grant_zone")
        ]

    def __str__(self):
        return f"zonegrant grant={self.grant_id} zone={self.zone_id}"


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


# ---------------------------------------------------------------------------
# Business-admin models (billing / audit / settings).
#
# Schema-of-record lives in agri-db; Django doesn't run migrate on boot, so
# these tables are unmanaged and self-deploy on prod via
# scripts/ensure_admin_tables.py (created session-wide in the test DB by the
# root conftest). managed=False keeps them out of the migration graph and
# db_constraint=False on every FK lets the postgres CI flush TRUNCATE freely
# (the assistant/technician lesson).
# ---------------------------------------------------------------------------


class Plan(_IrrigationBase):
    """A billable subscription plan."""

    name = models.CharField(max_length=100)
    price_dh = models.FloatField(default=0)
    interval = models.CharField(max_length=16, default="monthly")  # monthly|yearly
    features = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)

    class Meta:
        app_label = "analytics"
        db_table = "analytics_plan"
        managed = False


class Subscription(_IrrigationBase):
    """A customer's subscription to a plan."""

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="subscriptions",
        db_constraint=False,
    )
    plan = models.ForeignKey(
        "analytics.Plan",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="subscriptions",
        db_constraint=False,
    )
    status = models.CharField(
        max_length=16, default="active"
    )  # active|cancelled|expired
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)

    class Meta:
        app_label = "analytics"
        db_table = "analytics_subscription"
        managed = False


class Invoice(_IrrigationBase):
    """An invoice issued against a subscription."""

    subscription = models.ForeignKey(
        "analytics.Subscription",
        on_delete=models.CASCADE,
        related_name="invoices",
        db_constraint=False,
    )
    amount_dh = models.FloatField(default=0)
    status = models.CharField(max_length=16, default="unpaid")  # paid|unpaid
    issued_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "analytics"
        db_table = "analytics_invoice"
        managed = False


class AuditEvent(_IrrigationBase):
    """An admin-action audit record (who changed what, when)."""

    actor = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_events",
        db_constraint=False,
    )
    action = models.CharField(max_length=64)
    target_type = models.CharField(max_length=64, blank=True, default="")
    target_id = models.CharField(max_length=64, blank=True, default="")
    changes = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)

    class Meta:
        app_label = "analytics"
        db_table = "analytics_auditevent"
        managed = False


class SystemSetting(_IrrigationBase):
    """A categorized key/value system setting."""

    key = models.CharField(max_length=100, unique=True)
    value = models.JSONField(default=dict, blank=True)
    category = models.CharField(max_length=64, blank=True, default="general")
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        app_label = "analytics"
        db_table = "analytics_systemsetting"
        managed = False


class Device(_IrrigationBase):
    """A registered hardware router/gateway/node.

    Maps a hardware identifier (LoRaWAN DevEUI, Bivocom device_id, …) to the
    owner + zone its readings belong to, so the fleet view and health alerts
    can attribute uplinks to a customer.
    """

    DRAGON = "dragon"
    LORA = "lora"
    BIVOCOM = "bivocom"
    DEVICE_TYPE_CHOICES = [
        (DRAGON, "Dragon"),
        (LORA, "LoRa"),
        (BIVOCOM, "Bivocom"),
    ]

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="devices",
        db_constraint=False,
    )
    zone = models.ForeignKey(
        "analytics.Zone",
        on_delete=models.SET_NULL,
        related_name="devices",
        null=True,
        blank=True,
        db_constraint=False,
    )
    device_type = models.CharField(max_length=20, choices=DEVICE_TYPE_CHOICES)
    # Hardware identifier: LoRaWAN DevEUI / Bivocom device_id / serial number.
    serial = models.CharField(max_length=128, unique=True)
    name = models.CharField(max_length=120, blank=True, default="")
    is_active = models.BooleanField(default=True)
    # Last time the owner was emailed about a health issue for this device, so
    # the device-health beat task doesn't re-notify every tick (see tasks.py).
    last_health_notified = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)

    class Meta:
        app_label = "analytics"
        db_table = "analytics_device"
        # Schema-of-record in agri-db; self-deploys on prod via
        # scripts/ensure_device_tables.py and is created session-wide in the
        # test DB by conftest. managed=False keeps it out of the migration
        # graph; db_constraint=False on the FKs drops the DB-level constraint
        # so the postgres CI flush can TRUNCATE (see the technician models).
        managed = False

    def __str__(self):
        return f"{self.device_type}:{self.serial}"


class IrrigationProgram(_IrrigationBase):
    """A scheduled irrigation program for a zone: fire on the chosen weekdays at
    a start time, for a duration (or target volume). The beat task
    ``run_due_irrigation_programs`` turns due programs into OutputCommands.
    """

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="irrigation_programs",
        db_constraint=False,
    )
    zone = models.ForeignKey(
        "analytics.Zone",
        on_delete=models.CASCADE,
        related_name="irrigation_programs",
        db_constraint=False,
    )
    name = models.CharField(max_length=120)
    enabled = models.BooleanField(default=True)
    start_time = models.TimeField()
    # Comma-separated ISO weekday numbers the program runs on (1=Mon … 7=Sun),
    # e.g. "1,3,5". Empty = every day.
    weekdays = models.CharField(max_length=32, blank=True, default="")
    duration_min = models.PositiveIntegerField(null=True, blank=True)
    target_volume_m3 = models.FloatField(null=True, blank=True)
    # Last window this program fired in, so the scan doesn't re-fire it within
    # the same start-time window (dedup, see tasks.py).
    last_run_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        app_label = "analytics"
        db_table = "analytics_irrigationprogram"
        # Schema-of-record in agri-db; self-deploys on prod via
        # scripts/ensure_irrigation_tables.py and is created session-wide in the
        # test DB by conftest. managed=False + db_constraint=False mirror the
        # device/technician models so the postgres CI flush can TRUNCATE.
        managed = False

    def __str__(self):
        return f"IrrigationProgram({self.name} · zone {self.zone_id})"


class OutputCommand(_IrrigationBase):
    """A command to an output device (valve/pump) for a zone, created manually
    (Open/Close button) or by the scheduler. Physical dispatch is gated by
    settings.IRRIGATION_DISPATCH_ENABLED — when off (the default) commands are
    recorded as ``simulated`` and never actuate hardware.
    """

    OPEN = "open"
    CLOSE = "close"
    ACTION_CHOICES = [(OPEN, "Open"), (CLOSE, "Close")]

    MANUAL = "manual"
    SCHEDULED = "scheduled"
    SOURCE_CHOICES = [(MANUAL, "Manual"), (SCHEDULED, "Scheduled")]

    PENDING = "pending"
    SIMULATED = "simulated"
    SENT = "sent"
    FAILED = "failed"
    STATUS_CHOICES = [
        (PENDING, "Pending"),
        (SIMULATED, "Simulated"),
        (SENT, "Sent"),
        (FAILED, "Failed"),
    ]

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="output_commands",
        db_constraint=False,
    )
    zone = models.ForeignKey(
        "analytics.Zone",
        on_delete=models.CASCADE,
        related_name="output_commands",
        db_constraint=False,
    )
    device = models.ForeignKey(
        "analytics.Device",
        on_delete=models.SET_NULL,
        related_name="output_commands",
        null=True,
        blank=True,
        db_constraint=False,
    )
    action = models.CharField(max_length=8, choices=ACTION_CHOICES)
    source = models.CharField(max_length=12, choices=SOURCE_CHOICES, default=MANUAL)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=PENDING)
    detail = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "analytics"
        db_table = "analytics_outputcommand"
        managed = False

    def __str__(self):
        return f"OutputCommand({self.action} zone {self.zone_id} · {self.status})"


# ---------------------------------------------------------------------------
# Monitoring / observability (Wave 1 back-office)
# ---------------------------------------------------------------------------
# All three tables are unmanaged (self-deployed on prod via
# scripts/ensure_monitoring_tables.py) and live in the ``analytics`` namespace
# like the rest of the back-office models. They give the admin console history
# for automated actions (TaskRun), notification deliveries
# (NotificationDeliveryLog) and user sign-ins (LoginEvent) — none of which were
# persisted anywhere before.


class TaskRun(_IrrigationBase):
    """One Celery task execution. Written centrally from Celery signals
    (agriapi.celery), so every task is captured without per-task edits."""

    SUCCESS = "success"
    FAILURE = "failure"

    task_name = models.CharField(max_length=128)
    status = models.CharField(max_length=16, default=SUCCESS)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    runtime_ms = models.IntegerField(null=True, blank=True)
    result = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)

    class Meta:
        app_label = "analytics"
        db_table = "analytics_taskrun"
        managed = False

    def __str__(self):
        return f"TaskRun({self.task_name} · {self.status})"


class NotificationDeliveryLog(_IrrigationBase):
    """One notification delivery attempt (email / SMS / WhatsApp), recorded by
    the senders in agriapi.tasks via agriapi.delivery_log.record_delivery."""

    EMAIL = "email"
    SMS = "sms"
    WHATSAPP = "whatsapp"

    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"

    channel = models.CharField(max_length=16)
    kind = models.CharField(max_length=24, blank=True, default="")
    recipient = models.CharField(max_length=255, blank=True, default="")
    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="delivery_logs",
        db_constraint=False,
    )
    status = models.CharField(max_length=12, default=SENT)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)

    class Meta:
        app_label = "analytics"
        db_table = "analytics_notificationdeliverylog"
        managed = False

    def __str__(self):
        return f"Delivery({self.channel}->{self.recipient} · {self.status})"


class LoginEvent(_IrrigationBase):
    """One user sign-in attempt (success or failure), recorded in the
    /auth/sessions sign-in path so the admin can monitor user behaviour."""

    username = models.CharField(max_length=150, blank=True, default="")
    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="login_events",
        db_constraint=False,
    )
    success = models.BooleanField(default=False)
    ip = models.CharField(max_length=64, blank=True, default="")
    user_agent = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)

    class Meta:
        app_label = "analytics"
        db_table = "analytics_loginevent"
        managed = False

    def __str__(self):
        state = "ok" if self.success else "fail"
        return f"LoginEvent({self.username} · {state})"
