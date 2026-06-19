# Hand-written to mirror agri-db Alembic (CustomUser_customuser.is_technician).
# Schema is owned by agri-db; this file only keeps Django ORM state + the
# CI/test DB in sync. Never run makemigrations/migrate against prod here.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('CustomUser', '0006_customuser_date_joined_and_meta'),
    ]

    operations = [
        migrations.AddField(
            model_name='customuser',
            name='is_technician',
            field=models.BooleanField(default=False),
        ),
    ]
