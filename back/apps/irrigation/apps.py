from django.apps import AppConfig


class IrrigationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.irrigation"
    # Every model in this package declares Meta.app_label = "analytics"
    # (via the abstract _IrrigationBase), so the ORM keeps them under the
    # historical analytics label — the analytics_<model> db_tables and
    # analytics.<Model> FK references stay valid with no data migration.
    # Only the Python module moves; the Django app registry is unchanged.
    # This AppConfig's label stays "irrigation" so it's distinct in
    # INSTALLED_APPS.
    label = "irrigation"
