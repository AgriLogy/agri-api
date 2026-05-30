import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "agriapi.settings")

app = Celery("agriapi")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks(["agriapi"])
