# Restores the dropped 0062_zone_basin_geometry migration (source lost, .pyc
# only) plus the new rectangular-basin dimensions (L x W x H_tot).
#
# The prod DB already carries basin_max_depth_m / basin_area_m2 /
# sensor_mount_offset_m (applied by the lost 0062 / alembic e8a1c7f4d2b9),
# but a FRESH database (the pytest test DB, CI) does not. A plain AddField
# would fail on prod (column exists); a state-only AddField leaves fresh DBs
# without the columns (every Zone query then 500s). So the historical three
# are added in state unconditionally and in the database only when missing
# (``_add_legacy_basin_columns``). The three NEW columns are plain AddFields.

from django.db import migrations, models

_LEGACY_BASIN_FIELDS = ("basin_max_depth_m", "basin_area_m2", "sensor_mount_offset_m")


def _add_legacy_basin_columns(apps, schema_editor):
    """Add each legacy basin column only if the table doesn't have it yet.

    Introspection-based (not ``ADD COLUMN IF NOT EXISTS``) so it works on both
    Postgres and the sqlite test settings.
    """
    Zone = apps.get_model("analytics", "Zone")
    table = Zone._meta.db_table
    with schema_editor.connection.cursor() as cursor:
        existing = {
            col.name
            for col in schema_editor.connection.introspection.get_table_description(
                cursor, table
            )
        }
    for name in _LEGACY_BASIN_FIELDS:
        if name not in existing:
            schema_editor.add_field(Zone, Zone._meta.get_field(name))


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
        # Runs after the state ops above, so the historical model has the
        # fields; reverse is a no-op (never drop columns prod relies on).
        migrations.RunPython(_add_legacy_basin_columns, migrations.RunPython.noop),
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
