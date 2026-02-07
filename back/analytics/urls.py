from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .adminviews import *
from .sensor_registry import generated_views
from .views import *

urlpatterns = [
    path("header/", HeaderAPIView.as_view(), name="header"),
    path("zones-names-per-user/", ZonesNames.as_view(), name="zones-names-per-user"),
    path(
        "active-graph/self/<int:zone_id>/",
        ActiveGraphSelfAPIView.as_view(),
        name="active-graph-self",
    ),
    # Admin
    path(
        "active-graph/<str:username>/<int:zone_id>/",
        ActiveGraphAdminAPIView.as_view(),
        name="active-graph-admin",
    ),
    path(
        "active-zones/<str:username>/", ActiveZonesView.as_view(), name="active-zones"
    ),
    path("alert/", AlertsAPIView.as_view(), name="user-alert"),
    path("sensors/weather/ingest/", WeatherIngestAPIView.as_view()),
]

urlpatterns += [
    path(
        f"sensors/{name.lower().replace('sensor', '').replace('_', '-')}/",
        view.as_view(),
    )
    for name, view in generated_views
]
