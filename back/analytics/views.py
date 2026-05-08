from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils.dateparse import parse_datetime
from rest_framework import status, viewsets
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

CustomUser = get_user_model()

from django.utils.dateparse import parse_date

from .models import *
from .serializers import *


class SensorDataMixin(APIView):
    sensor_model = None
    serializer_class = None

    def get_queryset(self, request):
        user = request.user
        queryset = self.sensor_model.objects.filter(user=user)

        # Apply optional filters
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")
        zone = request.query_params.get("zone")

        if start_date:
            queryset = queryset.filter(timestamp__date__gte=parse_date(start_date))
        if end_date:
            queryset = queryset.filter(timestamp__date__lte=parse_date(end_date))
        if zone:
            queryset = queryset.filter(zone_id=zone)

        return queryset

    def get(self, request, *args, **kwargs):
        queryset = self.get_queryset(request)
        serializer = self.serializer_class(queryset, many=True)
        return Response(serializer.data)

    def patch(self, request, *args, **kwargs):
        try:
            obj = self.sensor_model.objects.get(
                id=request.data["id"], user=request.user
            )
        except self.sensor_model.DoesNotExist:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

        serializer = self.serializer_class(obj, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class HeaderAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        return Response({"username": user.username}, status=status.HTTP_200_OK)


class ZonesNames(APIView):
    def get(self, request):
        user = request.user
        zone = Zone.objects.all().filter(user_id=user.id)
        serialised_data = ZonesNameSerializer(zone, many=True)
        return Response(serialised_data.data, status=status.HTTP_200_OK)


class ActiveGraphSelfAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, zone_id):
        try:
            active_graph = ActiveGraph.objects.get(user=request.user, zone_id=zone_id)
        except ActiveGraph.DoesNotExist:
            return Response(
                {"detail": "ActiveGraph not found."}, status=status.HTTP_404_NOT_FOUND
            )

        serializer = ActiveGraphSerializer(active_graph)
        return Response(serializer.data)


class ActiveZonesView(APIView):
    permission_classes = [
        IsAdminUser
    ]  # Remove this if users can access their own zones

    def get(self, request, username):
        try:
            user = CustomUser.objects.get(username=username)
        except CustomUser.DoesNotExist:
            return Response(
                {"detail": "User not found."}, status=status.HTTP_404_NOT_FOUND
            )

        zones = Zone.objects.filter(user=user)
        serializer = ZonesNameSerializer(zones, many=True)
        return Response(serializer.data)


class AlertsAPIView(APIView):
    permission_classes = [IsAuthenticated]  # Ensure the user is authenticated

    def get(self, request):
        alerts = Alert.objects.filter(user=request.user).order_by("-id")
        serializer = AlertSerializer(alerts, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def post(self, request):
        # Manually add the user to the request data before validation
        data = request.data.copy()  # Make a copy of the request data
        data["user"] = request.user.id  # Assign the authenticated user's ID

        # Pass the updated data to the serializer
        serializer = AlertSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    HumidityWeather,
    PressureWeather,
    SolarRadiation,
    TemperatureWeather,
    WindDirection,
    WindSpeed,
    Zone,
)

METRIC_KEYS = [
    "wind_speed",
    "pressure_weather",
    "temperature_weather",
    "humidity_weather",
    "solar_radiation",
    "wind_direction",
]


class WeatherIngestAPIView(APIView):
    """
    Expects a JSON OBJECT (not list), same as what Node forwards.
    If ALL metric fields are None/missing => insert nothing.
    """

    authentication_classes = []
    permission_classes = []

    def post(self, request):
        payload = request.data

        if not isinstance(payload, dict):
            return Response(
                {"error": "Expected a JSON object"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        metrics = {k: payload.get(k, None) for k in METRIC_KEYS}

        if all(v is None for v in metrics.values()):
            return Response(
                {"inserted": 0, "detail": "all_metrics_none"},
                status=status.HTTP_200_OK,
            )

        client = payload.get("client")
        if not client or not isinstance(client, str):
            return Response(
                {"error": "client is required when any metric is provided"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        client = client.strip()

        user = CustomUser.objects.filter(username=client).first()
        if not user:
            return Response(
                {"error": f"User not found for client '{client}'"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        zone = Zone.objects.filter(user=user).first()
        if not zone:
            return Response(
                {"error": f"No zone found for user '{user.username}'"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = zone.user
        now = timezone.now()

        inserted = 0

        if metrics["wind_speed"] is not None:
            WindSpeed.objects.create(
                user=user, zone=zone, value=metrics["wind_speed"], timestamp=now
            )
            inserted += 1

        if metrics["pressure_weather"] is not None:
            PressureWeather.objects.create(
                user=user, zone=zone, value=metrics["pressure_weather"], timestamp=now
            )
            inserted += 1

        if metrics["temperature_weather"] is not None:
            TemperatureWeather.objects.create(
                user=user,
                zone=zone,
                value=metrics["temperature_weather"],
                timestamp=now,
            )
            inserted += 1

        if metrics["humidity_weather"] is not None:
            HumidityWeather.objects.create(
                user=user, zone=zone, value=metrics["humidity_weather"], timestamp=now
            )
            inserted += 1

        if metrics["solar_radiation"] is not None:
            SolarRadiation.objects.create(
                user=user, zone=zone, value=metrics["solar_radiation"], timestamp=now
            )
            inserted += 1

        if metrics["wind_direction"] is not None:
            WindDirection.objects.create(
                user=user, zone=zone, value=metrics["wind_direction"], timestamp=now
            )
            inserted += 1

        return Response({"inserted": inserted}, status=status.HTTP_201_CREATED)


# -----------------------------------------------------------------------------
# Notifications surface
# -----------------------------------------------------------------------------
import logging  # noqa: E402

from django.conf import settings  # noqa: E402
from django.core.mail import send_mail  # noqa: E402

from .models import Notification  # noqa: E402

logger = logging.getLogger(__name__)


class NotificationsAndAlertsAPIView(APIView):
    """
    Read-only feed of the current user's recent notifications.

    The frontend caches rows locally and merges in zone-config / template
    rows on the client. This endpoint returns the server-persisted rows so
    cards survive a cache wipe and stay consistent across devices.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        rows = (
            Notification.objects.filter(user=request.user)
            .order_by("-notification_date")[:200]
        )

        def serialize(n: Notification) -> dict:
            return {
                "id": n.id,
                "is_read": False,
                "read_at": None,
                "zone_name": None,
                "_source": "server",
                "notification": {
                    "yesterday_temperature": str(n.yesterday_temperature),
                    "today_temperature": str(n.today_temperature),
                    "yesterday_humidity": str(n.yesterday_humidity),
                    "today_humidity": str(n.today_humidity),
                    "ET0": str(n.ET0),
                    "soil_humidity": str(n.soil_humidity),
                    "soil_temperature": str(n.soil_temperature),
                    "soil_ph": str(n.soil_ph),
                    "perfect_irrigation_period": n.perfect_irrigation_period,
                    "last_irrigation_date": n.last_irrigation_date.isoformat()
                    if n.last_irrigation_date
                    else None,
                    "last_start_irrigation_hour": n.last_start_irrigation_hour.isoformat()
                    if n.last_start_irrigation_hour
                    else None,
                    "last_finish_irrigation_hour": n.last_finish_irrigation_hour.isoformat()
                    if n.last_finish_irrigation_hour
                    else None,
                    "used_water_irrigation": str(n.used_water_irrigation),
                    "notification_date": n.notification_date.isoformat(),
                },
            }

        return Response({"notifications": [serialize(r) for r in rows]})


class ZoneNotificationOutboundAPIView(APIView):
    """
    Sends a one-shot confirmation email when a user saves a zone notification
    config with channels.email = true. SMS / WhatsApp are accepted in the
    payload for forward compatibility but are no-ops in v1.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        data = request.data or {}
        channels = (data.get("channels") or {}) if isinstance(data, dict) else {}
        if not channels.get("email"):
            # Nothing to do for non-email channels yet — accept and move on.
            return Response({"status": "noop"}, status=status.HTTP_202_ACCEPTED)

        recipient = (
            (data.get("contactEmail") or "").strip()
            or (getattr(request.user, "email", "") or "").strip()
        )
        if not recipient:
            return Response(
                {"detail": "no email address on user"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        subject = (data.get("subject") or "Agrilogy — notification").strip()
        message = (data.get("message") or "").strip() or (
            "La configuration de notification de zone a été enregistrée."
        )

        try:
            send_mail(
                subject=subject,
                message=message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[recipient],
                fail_silently=False,
            )
        except Exception as exc:
            logger.exception("zone-notification-outbound: send failed")
            return Response({"detail": str(exc)}, status=500)

        logger.info(
            "zone-notification-outbound: sent to %s zone=%s",
            recipient,
            data.get("zoneId"),
        )
        return Response({"status": "sent"}, status=status.HTTP_202_ACCEPTED)
