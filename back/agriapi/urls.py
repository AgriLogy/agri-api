from django.contrib import admin
from django.urls import include, path
from drf_yasg import openapi
from drf_yasg.views import get_schema_view
from rest_framework import permissions

from agriapi.api import api as v2_api

schema_view = get_schema_view(
    openapi.Info(
        title="Snippets API",
        default_version="v1",
        description="Test description",
        terms_of_service="https://www.google.com/policies/terms/",
        contact=openapi.Contact(email="contact@snippets.local"),
        license=openapi.License(name="BSD License"),
    ),
    public=True,
    permission_classes=(permissions.AllowAny,),
)


urlpatterns = [
    path(
        "swagger<format>/", schema_view.without_ui(cache_timeout=0), name="schema-json"
    ),
    path(
        "swagger/",
        schema_view.with_ui("swagger", cache_timeout=0),
        name="schema-swagger-ui",
    ),
    path("redoc/", schema_view.with_ui("redoc", cache_timeout=0), name="schema-redoc"),
    # django-ninja surface — mounted at root, BEFORE django admin so the
    # ninja ``/admin/overview`` route is not shadowed by Django's
    # ``/admin/login/`` etc. Django admin keeps working at /admin/ for any
    # path the ninja API doesn't claim.
    path("", v2_api.urls),
    path("admin/", admin.site.urls),
    path("auth/", include("apps.users.urls")),
    # Hardware-family ingest endpoints migrated to django-ninja — served
    # from `v2_api.urls` (mounted at root above) at:
    #   POST /api/v1/bivocom/uplink
    #   POST /api/v1/lorawan/chirpstack/uplink
]
