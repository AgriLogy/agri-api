"""Django app config for the Bivocom HTTP ingest endpoint."""
from __future__ import annotations

from django.apps import AppConfig


class BivocomConfig(AppConfig):
    name = "apps.bivocom"
    label = "bivocom"  # short label used by Django admin/migrations
    verbose_name = "Bivocom Ingest"
