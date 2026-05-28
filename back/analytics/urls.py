from django.urls import include, path

from .adminviews import *
from .manager_affirmation import (
    ManagerAffirmationDecisionAPIView,
    ManagerAffirmationListCreateAPIView,
)
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
    # Manager affirmations migrated to django-ninja
    # (analytics.router_manager_affirmation).
    # Alerts CRUD + for-graph / sensor-keys / suggest migrated to
    # django-ninja in analytics.router_alerts.
    path("sensors/weather/ingest/", WeatherIngestAPIView.as_view()),
    # notifications-and-alerts/ and zone-notification-outbound/ migrated
    # to django-ninja (analytics.router_notifications).
]

# The 34 dynamically-generated DRF sensor routes have migrated to
# django-ninja (analytics.router_sensors). The `generated_views`
# loop in sensor_registry still runs because alerts.py imports the
# SENSOR_MODELS list, but the resulting view classes are unused.
