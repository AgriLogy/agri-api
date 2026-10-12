# Borehole (forage) cylinder: depth-to-water display, no volume computation.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("analytics", "0068_zone_basin_geometry_restore"),
    ]

    operations = [
        migrations.CreateModel(
            name="ForageSensor",
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
                ("device_id", models.BigIntegerField(blank=True, null=True)),
                (
                    "value",
                    models.FloatField(
                        blank=True,
                        null=True,
                        help_text="Depth to the water surface in metres.",
                    ),
                ),                ("timestamp", models.DateTimeField()),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="forage_sensors_per_user",
                        to="CustomUser.customuser",
                    ),
                ),
                (
                    "zone",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="forage_sensors",
                        to="analytics.zone",
                    ),
                ),
            ],
        ),
        migrations.AddField(
            model_name="zone",
            name="forage_diameter_m",
            field=models.FloatField(
                null=True,
                blank=True,
                help_text="Borehole interior diameter in metres.",
            ),
        ),
        migrations.AddField(
            model_name="zone",
            name="forage_total_depth_m",
            field=models.FloatField(
                null=True,
                blank=True,
                help_text="Borehole total depth in metres (H for the visual).",
            ),
        ),
        migrations.AddField(
            model_name="zone",
            name="forage_high_m",
            field=models.FloatField(
                null=True,
                blank=True,
                help_text="Depth at or above which the level counts as HIGH (metres).",
            ),
        ),
        migrations.AddField(
            model_name="zone",
            name="forage_low_m",
            field=models.FloatField(
                null=True,
                blank=True,
                help_text="Depth at or below which the level counts as LOW (metres).",
            ),
        ),
        migrations.AddField(
            model_name="activegraph",
            name="forage_level_status",
            field=models.BooleanField(default=True, help_text="Niveau du forage"),
        ),
    ]
