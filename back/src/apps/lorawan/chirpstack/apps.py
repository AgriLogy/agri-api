"""Django app config for the ChirpStack LoRaWAN webhook."""

from __future__ import annotations

from django.apps import AppConfig


class ChirpStackConfig(AppConfig):
    name = "apps.lorawan.chirpstack"
    label = "lorawan_chirpstack"
    verbose_name = "LoRaWAN (ChirpStack) Ingest"
