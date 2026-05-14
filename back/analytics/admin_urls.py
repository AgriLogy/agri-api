"""URL conf for /api/admin/* — admin-only analytics management."""

from django.urls import path

from .admin_views import (
    AdminAlertDetailAPIView,
    AdminOverviewAPIView,
    AdminUserActivityAPIView,
    AdminUserAlertsAPIView,
    AdminUserSensorUnitsAPIView,
    AdminUserZoneActiveGraphAPIView,
    AdminUserZoneDetailAPIView,
    AdminUserZoneParamsAPIView,
    AdminUserZonesAPIView,
)
from .manager_affirmation import (
    ManagerAffirmationDecisionAPIView,
    ManagerAffirmationListCreateAPIView,
)

urlpatterns = [
    path("overview/", AdminOverviewAPIView.as_view(), name="admin-overview"),
    path(
        "users/<str:username>/zones/",
        AdminUserZonesAPIView.as_view(),
        name="admin-user-zones",
    ),
    path(
        "users/<str:username>/zones/<int:pk>/",
        AdminUserZoneDetailAPIView.as_view(),
        name="admin-user-zone-detail",
    ),
    path(
        "users/<str:username>/zones/<int:pk>/params/",
        AdminUserZoneParamsAPIView.as_view(),
        name="admin-user-zone-params",
    ),
    path(
        "users/<str:username>/zones/<int:zone_id>/active-graph/",
        AdminUserZoneActiveGraphAPIView.as_view(),
        name="admin-user-zone-active-graph",
    ),
    path(
        "users/<str:username>/alerts/",
        AdminUserAlertsAPIView.as_view(),
        name="admin-user-alerts",
    ),
    path(
        "alerts/<int:pk>/",
        AdminAlertDetailAPIView.as_view(),
        name="admin-alert-detail",
    ),
    path(
        "users/<str:username>/activity/",
        AdminUserActivityAPIView.as_view(),
        name="admin-user-activity",
    ),
    path(
        "users/<str:username>/sensor-units/",
        AdminUserSensorUnitsAPIView.as_view(),
        name="admin-user-sensor-units",
    ),
]
