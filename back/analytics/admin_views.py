"""
Admin-side views for the analytics app.

All routes are gated behind `IsAuthenticated + IsAdminUser`.
Replaces the legacy `adminviews.py` patterns with REST-conformant
endpoints under `/api/admin/*`.
"""

import logging

from django.contrib.auth import get_user_model
from django.db.models import Count
from rest_framework import generics, status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .admin_serializers import (
    AdminAlertSerializer,
    AdminZoneParamsSerializer,
    AdminZoneSerializer,
)
from .models import ActiveGraph, Alert, Zone

logger = logging.getLogger(__name__)
User = get_user_model()


def _resolve_user(username):
    try:
        return User.objects.get(username=username)
    except User.DoesNotExist as exc:
        raise NotFound(f"User '{username}' not found.") from exc


# ---------------------------------------------------------------------------
# Overview / KPIs
# ---------------------------------------------------------------------------


class AdminOverviewAPIView(APIView):
    """GET /api/admin/overview/  →  global KPIs for the admin dashboard."""

    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request):
        from django.utils import timezone

        since = timezone.now() - timezone.timedelta(hours=24)
        data = {
            "users_total": User.objects.count(),
            "users_active": User.objects.filter(is_active=True).count(),
            "staff_total": User.objects.filter(is_staff=True).count(),
            "zones_total": Zone.objects.count(),
            "alerts_24h": Alert.objects.filter(last_triggered_at__gte=since).count(),
        }
        return Response(data, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Zone CRUD per user
# ---------------------------------------------------------------------------


class AdminUserZonesAPIView(generics.ListCreateAPIView):
    """GET / POST /api/admin/users/<username>/zones/"""

    permission_classes = [IsAuthenticated, IsAdminUser]
    serializer_class = AdminZoneSerializer

    def get_queryset(self):
        user = _resolve_user(self.kwargs["username"])
        return Zone.objects.filter(user=user).order_by("id")

    def perform_create(self, serializer):
        user = _resolve_user(self.kwargs["username"])
        serializer.save(user=user)


class AdminUserZoneDetailAPIView(generics.RetrieveUpdateDestroyAPIView):
    """GET / PUT / PATCH / DELETE /api/admin/users/<username>/zones/<pk>/"""

    permission_classes = [IsAuthenticated, IsAdminUser]
    serializer_class = AdminZoneSerializer

    def get_queryset(self):
        user = _resolve_user(self.kwargs["username"])
        return Zone.objects.filter(user=user)


class AdminUserZoneParamsAPIView(generics.RetrieveUpdateAPIView):
    """GET / PUT / PATCH /api/admin/users/<username>/zones/<pk>/params/"""

    permission_classes = [IsAuthenticated, IsAdminUser]
    serializer_class = AdminZoneParamsSerializer
    http_method_names = ["get", "put", "patch", "head", "options"]

    def get_queryset(self):
        user = _resolve_user(self.kwargs["username"])
        return Zone.objects.filter(user=user)


# ---------------------------------------------------------------------------
# Active-graph admin (replaces legacy ActiveGraphAdminAPIView with REST shape)
# ---------------------------------------------------------------------------


class AdminUserZoneActiveGraphAPIView(APIView):
    """GET / PATCH /api/admin/users/<username>/zones/<zone_id>/active-graph/

    PATCH body: partial dict of `<field>_status: bool`.
    The row is created with defaults if missing.
    """

    permission_classes = [IsAuthenticated, IsAdminUser]

    def _get_or_create(self, username, zone_id):
        user = _resolve_user(username)
        try:
            zone = Zone.objects.get(pk=zone_id, user=user)
        except Zone.DoesNotExist as exc:
            raise NotFound("Zone not found for this user.") from exc
        active_graph, _ = ActiveGraph.objects.get_or_create(user=user, zone=zone)
        return active_graph

    def _serialize(self, active_graph):
        from .serializers import ActiveGraphSerializer

        return ActiveGraphSerializer(active_graph).data

    def get(self, request, username, zone_id):
        active_graph = self._get_or_create(username, zone_id)
        return Response(self._serialize(active_graph), status=status.HTTP_200_OK)

    def patch(self, request, username, zone_id):
        from .serializers import ActiveGraphSerializer

        active_graph = self._get_or_create(username, zone_id)
        serializer = ActiveGraphSerializer(
            active_graph, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Alerts admin override
# ---------------------------------------------------------------------------


class AdminUserAlertsAPIView(generics.ListAPIView):
    """GET /api/admin/users/<username>/alerts/  →  user's alerts."""

    permission_classes = [IsAuthenticated, IsAdminUser]
    serializer_class = AdminAlertSerializer

    def get_queryset(self):
        user = _resolve_user(self.kwargs["username"])
        return Alert.objects.filter(user=user).order_by("-id")


class AdminAlertDetailAPIView(generics.RetrieveUpdateDestroyAPIView):
    """PATCH (is_active only) / DELETE /api/admin/alerts/<pk>/"""

    permission_classes = [IsAuthenticated, IsAdminUser]
    serializer_class = AdminAlertSerializer
    queryset = Alert.objects.all()
    http_method_names = ["get", "patch", "delete", "head", "options"]


# ---------------------------------------------------------------------------
# Per-user activity timeline
# ---------------------------------------------------------------------------


class AdminUserActivityAPIView(APIView):
    """GET /api/admin/users/<username>/activity/

    Returns a compact list of `{ kind, label, at }` rows the front
    renders as a `<Timeline>`.
    """

    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request, username):
        user = _resolve_user(username)
        events = []
        if user.date_joined:
            events.append(
                {
                    "kind": "joined",
                    "label": "Compte créé",
                    "at": user.date_joined.isoformat(),
                }
            )
        if user.last_login:
            events.append(
                {
                    "kind": "login",
                    "label": "Dernière connexion",
                    "at": user.last_login.isoformat(),
                }
            )
        if user.last_notified:
            events.append(
                {
                    "kind": "notified",
                    "label": "Dernière notification envoyée",
                    "at": user.last_notified.isoformat(),
                }
            )

        zone_count = Zone.objects.filter(user=user).count()
        events.append(
            {
                "kind": "zones",
                "label": f"{zone_count} zone(s) actives",
                "at": None,
            }
        )

        triggered = (
            Alert.objects.filter(user=user, last_triggered_at__isnull=False)
            .order_by("-last_triggered_at")
            .values("name", "last_triggered_at")[:5]
        )
        for row in triggered:
            events.append(
                {
                    "kind": "alert",
                    "label": f"Alerte déclenchée : {row['name']}",
                    "at": row["last_triggered_at"].isoformat(),
                }
            )

        events.sort(key=lambda e: e["at"] or "", reverse=True)
        return Response({"events": events}, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Per-user sensor units (preferences)
# ---------------------------------------------------------------------------


class AdminUserSensorUnitsAPIView(APIView):
    """GET / PATCH /api/admin/users/<username>/sensor-units/

    Body shape (PATCH): `{ "<sensor_key>": "<unit>", ... }`.
    Persists to the `UserSensorUnitPreference` model (lazy import to
    avoid module load before the model's migration has run).
    """

    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request, username):
        from .models import UserSensorUnitPreference

        user = _resolve_user(username)
        prefs = {
            row.sensor_key: row.unit
            for row in UserSensorUnitPreference.objects.filter(user=user)
        }
        return Response(prefs, status=status.HTTP_200_OK)

    def patch(self, request, username):
        from .models import UserSensorUnitPreference

        user = _resolve_user(username)
        if not isinstance(request.data, dict):
            return Response(
                {"detail": "Body must be a JSON object of sensor_key → unit."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        for sensor_key, unit in request.data.items():
            if not isinstance(sensor_key, str) or not isinstance(unit, str):
                return Response(
                    {"detail": "All keys and values must be strings."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            UserSensorUnitPreference.objects.update_or_create(
                user=user,
                sensor_key=sensor_key,
                defaults={"unit": unit},
            )

        prefs = {
            row.sensor_key: row.unit
            for row in UserSensorUnitPreference.objects.filter(user=user)
        }
        return Response(prefs, status=status.HTTP_200_OK)
