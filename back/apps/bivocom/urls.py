"""URL conf for ``apps.bivocom`` — included under ``/api/v1/bivocom/``."""
from __future__ import annotations

from django.urls import path

from apps.bivocom.views import BivocomUplinkView

app_name = "bivocom"

urlpatterns = [
    path("uplink", BivocomUplinkView.as_view(), name="uplink"),
]
