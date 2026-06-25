from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("analytics", "0063_notification_zones_and_alert_notify_sms"),
    ]

    operations = [
        migrations.AddField(
            model_name="alert",
            name="grace_override_seconds",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
