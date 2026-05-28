from django.contrib import admin
from django.urls import include, path
from drf_yasg import openapi
from drf_yasg.views import get_schema_view
from rest_framework import permissions

from agriBack.api import api as v2_api

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
    path("admin/", admin.site.urls),
    # django-ninja surface — mounted at root so each router can carry
    # its full URL path (new /api/v2/* + migrated-in-place /api/v1/*
    # and /api/* + /auth/* slots). Per memory `agri-api-fastapi-style`.
    # Listed BEFORE the legacy DRF includes so ninja routes match first;
    # routes that haven't migrated yet fall through to the DRF urls.
    path("", v2_api.urls),
    path("api/", include("analytics.urls")),
    path("auth/", include("apps.users.urls")),
    # Hardware-family ingest endpoints (one app per device family)
    path("api/v1/bivocom/", include("apps.bivocom.urls")),
    # apps.lorawan.chirpstack migrated to django-ninja — served from
    # `v2_api.urls` under `/api/v1/lorawan/chirpstack/uplink`.
]
