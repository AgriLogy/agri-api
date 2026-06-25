# Custom notification zones + per-alert SMS channel (agrilogy-front #57).
#
# NotificationZone / NotificationZoneSensor are managed=False (schema-of-record
# in agri-db; self-deployed via scripts/ensure_notification_zone_tables.py and
# created in the test DB by conftest). The CreateModel ops below are state-only
# — migrate skips the table for managed=False models — and exist so the new
# Alert.notification_zone FK resolves in the migration graph.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("analytics", "0062_zone_elevation_m"),
    ]

    operations = [
        migrations.CreateModel(
            name="NotificationZone",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=200)),
                ("description", models.TextField(blank=True, default="")),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True, null=True)),
                (
                    "user",
                    models.ForeignKey(
                        db_constraint=False,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="notification_zones",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "analytics_notificationzone",
                "managed": False,
            },
        ),
        migrations.CreateModel(
            name="NotificationZoneSensor",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("sensor_key", models.CharField(max_length=64)),
                ("label", models.CharField(blank=True, max_length=200, null=True)),
                (
                    "notification_zone",
                    models.ForeignKey(
                        db_constraint=False,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="sensors",
                        to="analytics.notificationzone",
                    ),
                ),
                (
                    "source_zone",
                    models.ForeignKey(
                        blank=True,
                        db_constraint=False,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="notification_sensor_links",
                        to="analytics.zone",
                    ),
                ),
            ],
            options={
                "db_table": "analytics_notificationzonesensor",
                "managed": False,
            },
        ),
        migrations.AddField(
            model_name="alert",
            name="notify_sms",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="alert",
            name="notification_zone",
            field=models.ForeignKey(
                blank=True,
                db_constraint=False,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="alerts",
                to="analytics.notificationzone",
            ),
        ),
    ]
