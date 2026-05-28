"""URL conf for the ChirpStack webhook — included under
``/api/v1/lorawan/chirpstack/``."""
from __future__ import annotations

from django.urls import path

from apps.lorawan.chirpstack.views import ChirpStackUplinkView

app_name = "lorawan_chirpstack"

urlpatterns = [
    path("uplink", ChirpStackUplinkView.as_view(), name="uplink"),
]
