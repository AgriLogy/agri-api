from django.apps import AppConfig


class SensorsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.sensors"
    # NB: every model in this package declares Meta.app_label = "analytics"
    # (via the abstract _SensorBase), so the ORM keeps them under the
    # historical analytics label — no data migration needed for the move.
    # This AppConfig's label stays "sensors" so it's distinct in
    # INSTALLED_APPS.
    label = "sensors"
