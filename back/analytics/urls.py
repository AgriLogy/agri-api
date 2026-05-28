from django.urls import include, path

from .adminviews import *
from .manager_affirmation import (
    ManagerAffirmationDecisionAPIView,
    ManagerAffirmationListCreateAPIView,
)
from .sensor_registry import generated_views
from .views import *

urlpatterns = [
    # header/, zones-names-per-user/, active-graph/self/<zone_id>/, and
    # active-zones/<username>/ have migrated to django-ninja
    # (analytics.router_reads). They keep their URL paths via the
    # NinjaAPI mounted at root in agriBack.urls.
    # Legacy admin endpoint (kept for the in-flight frontend migration)
    path(
        "active-graph/<str:username>/<int:zone_id>/",
        ActiveGraphAdminAPIView.as_view(),
        name="active-graph-admin",
    ),
    # New admin tree
    path("admin/", include("analytics.admin_urls")),
    # Manager affirmations
    path(
        "manager-affirmations/",
        ManagerAffirmationListCreateAPIView.as_view(),
        name="manager-affirmations",
    ),
    path(
        "manager-affirmations/<int:pk>/<str:action>/",
        ManagerAffirmationDecisionAPIView.as_view(),
        name="manager-affirmation-decision",
    ),
    # Alerts CRUD + for-graph / sensor-keys / suggest migrated to
    # django-ninja in analytics.router_alerts.
    path("sensors/weather/ingest/", WeatherIngestAPIView.as_view()),
    path(
        "notifications-and-alerts/",
        NotificationsAndAlertsAPIView.as_view(),
        name="notifications-and-alerts",
    ),
    path(
        "zone-notification-outbound/",
        ZoneNotificationOutboundAPIView.as_view(),
        name="zone-notification-outbound",
    ),
]

urlpatterns += [
    path(
        f"sensors/{name.lower().replace('sensor', '').replace('_', '-')}/",
        view.as_view(),
    )
    for name, view in generated_views
]
