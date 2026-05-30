"""Legacy analytics URL conf.

Every endpoint that used to be wired here has migrated to
django-ninja and is now served by the NinjaAPI in ``agriapi.api``
(routers: ``router_reads``, ``router_alerts``, ``router_notifications``,
``router_manager_affirmation``, ``router_sensors``,
``router_weather_ingest``, ``router_admin``).

The empty ``urlpatterns`` is kept so the agriapi/urls.py include
(``path("api/", include("analytics.urls"))``) still resolves cleanly;
Django will fall through to the ninja routes registered at the URL
root via the NinjaAPI mount.
"""

urlpatterns: list = []
