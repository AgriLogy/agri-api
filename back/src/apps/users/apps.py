from django.apps import AppConfig


class UsersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.users"
    # Historical app_label — kept so AUTH_USER_MODEL="CustomUser.CustomUser",
    # FK strings like "CustomUser.<Model>", and django_migrations entries
    # don't need a data migration after the package rename.
    label = "CustomUser"
