# Restores the dropped 0062_zone_basin_geometry migration (source lost, .pyc
# only) plus the new rectangular-basin dimensions (L x W x H_tot).
#
# The prod DB already carries basin_max_depth_m / basin_area_m2 /
# sensor_mount_offset_m (applied by the lost 0062 / alembic e8a1c7f4d2b9),
# so those three use idempotent AddField semantics via separate state-only
# handling: plain AddField is safe because Django issues ALTER TABLE ADD
# COLUMN which fails if the column exists. To stay re-runnable we keep the
# historical three as state-only (SeparateDatabaseAndState) and only the
# three NEW columns hit the database.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("analytics", "0067_add_device_id_to_readings"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.AddField(
                    model_name="zone",
                    name="basin_max_depth_m",
                    field=models.FloatField(
                        null=True, blank=True,
                        help_text="Basin maximum depth in metres.",
                    ),
                ),
                migrations.AddField(
                    model_name="zone",
                    name="basin_area_m2",
                    field=models.FloatField(
                        null=True, blank=True,
                        help_text="Basin surface area in square metres.",
                    ),
                ),
                migrations.AddField(
                    model_name="zone",
                    name="sensor_mount_offset_m",
                    field=models.FloatField(
                        null=True, blank=True,
                        help_text="Distance from the level sensor to the full level, in metres.",
                    ),
                ),
            ],
        ),
        migrations.AddField(
            model_name="zone",
            name="basin_length_m",
            field=models.FloatField(
                null=True, blank=True,
                help_text="Rectangular basin interior length in metres.",
            ),
        ),
        migrations.AddField(
            model_name="zone",
            name="basin_width_m",
            field=models.FloatField(
                null=True, blank=True,
                help_text="Rectangular basin interior width in metres.",
            ),
        ),
        migrations.AddField(
            model_name="zone",
            name="basin_height_m",
            field=models.FloatField(
                null=True, blank=True,
                help_text="Sensor plane to basin bottom in metres (H_tot).",
            ),
        ),
    ]
