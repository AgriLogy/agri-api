# Generated for agri-api #31 — per-user notification language.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("CustomUser", "0008_customuser_sessions_revoked_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="customuser",
            name="preferred_language",
            field=models.CharField(
                choices=[("fr", "Français"), ("ar", "العربية")],
                default="fr",
                help_text="Language for notification emails (and UI): 'fr' or 'ar'.",
                max_length=8,
            ),
        ),
    ]
