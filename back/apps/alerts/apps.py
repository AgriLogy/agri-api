from django.apps import AppConfig


class AlertsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.alerts"
    # Every model in this package declares Meta.app_label = "analytics"
    # (via the abstract _AlertBase), so the ORM keeps them under the
    # historical analytics label — the analytics_<model> db_tables and
    # analytics.<Model> FK references stay valid with no data migration.
    # This AppConfig's label stays "alerts" so it's distinct in
    # INSTALLED_APPS. NB: the alert-evaluation engine + ingest dispatch
    # logic lives in analytics/alerts.py (Django adapter around
    # agri.core.alerts); only the ORM rows move here.
    label = "alerts"
