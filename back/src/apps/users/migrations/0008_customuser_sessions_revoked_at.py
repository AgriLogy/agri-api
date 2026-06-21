# Hand-written to mirror agri-db Alembic (CustomUser_customuser.sessions_revoked_at).
# Schema is owned by agri-db; this file only keeps Django ORM state + the
# CI/test DB in sync. Never run makemigrations/migrate against prod here.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('CustomUser', '0007_customuser_is_technician'),
    ]

    operations = [
        migrations.AddField(
            model_name='customuser',
            name='sessions_revoked_at',
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text='Tokens issued before this time are rejected (forced logout).',
            ),
        ),
    ]
