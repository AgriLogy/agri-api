from django.db import migrations, models
from django.utils import timezone


class Migration(migrations.Migration):
    dependencies = [
        ("CustomUser", "0005_customuser_last_notified_customuser_notify_every"),
    ]

    operations = [
        migrations.AddField(
            model_name="customuser",
            name="date_joined",
            field=models.DateTimeField(
                default=timezone.now,
                help_text="When the user account was created.",
            ),
        ),
        migrations.AlterModelOptions(
            name="customuser",
            options={"ordering": ["-date_joined"]},
        ),
        migrations.AddIndex(
            model_name="customuser",
            index=models.Index(
                fields=["-date_joined"],
                name="CustomUser__date_jo_5319cf_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="customuser",
            index=models.Index(
                fields=["is_active"],
                name="CustomUser__is_acti_d69f47_idx",
            ),
        ),
    ]
