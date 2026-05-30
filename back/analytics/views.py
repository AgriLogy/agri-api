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
    """List + create user alerts."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = Alert.objects.filter(user=request.user).order_by("-id")
        sensor_key = request.query_params.get("sensor_key")
        zone_id = request.query_params.get("zone_id")
        if sensor_key:
            qs = qs.filter(sensor_key=sensor_key)
        if zone_id:
            qs = qs.filter(zone_id=zone_id)
        serializer = AlertSerializer(qs, many=True, context={"request": request})
        return Response(serializer.data, status=status.HTTP_200_OK)

    def post(self, request):
        data = request.data.copy()
        data["user"] = request.user.id
        serializer = AlertSerializer(data=data, context={"request": request})
        if serializer.is_valid():
            serializer.save(user=request.user)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class AlertDetailAPIView(APIView):
    """Retrieve / update / delete a single alert (owned by the caller)."""

    permission_classes = [IsAuthenticated]

    def _get_object(self, request, pk):
        try:
            return Alert.objects.get(pk=pk, user=request.user)
        except Alert.DoesNotExist:
            return None

    def get(self, request, pk):
        alert = self._get_object(request, pk)
        if alert is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(
            AlertSerializer(alert, context={"request": request}).data,
            status=status.HTTP_200_OK,
        )

    def put(self, request, pk):
        return self._update(request, pk, partial=False)

    def patch(self, request, pk):
        return self._update(request, pk, partial=True)

    def _update(self, request, pk, *, partial: bool):
        alert = self._get_object(request, pk)
        if alert is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = AlertSerializer(
            alert, data=request.data, partial=partial, context={"request": request}
        )
        if serializer.is_valid():
            serializer.save(user=request.user)
            return Response(serializer.data, status=status.HTTP_200_OK)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        alert = self._get_object(request, pk)
        if alert is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        alert.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AlertsForGraphAPIView(APIView):
    """
    Plug-and-play endpoint a chart calls to learn which alerts apply to a
    sensor_key (+ optional zone) and what their current state is. The
    payload is shaped for direct consumption by recharts ReferenceLines.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .alerts import SENSOR_KEY_REGISTRY, recent_triggers_for_user

        sensor_key = request.query_params.get("sensor_key") or None
        if sensor_key and sensor_key not in SENSOR_KEY_REGISTRY:
            return Response(
                {"detail": f"Unknown sensor_key '{sensor_key}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        zone_param = request.query_params.get("zone_id")
        zone_id = int(zone_param) if zone_param and zone_param.isdigit() else None
        rows = recent_triggers_for_user(
            request.user, sensor_key=sensor_key, zone_id=zone_id
        )
        return Response({"alerts": rows}, status=status.HTTP_200_OK)


class AlertSuggestAPIView(APIView):
    """
    GET /api/alerts/suggest/?sensor_key=<key>&zone_id=<id>

    Returns a prefilled alert payload (name, threshold = mean of recent
    readings, condition picked per sensor type, etc.) so the front-end
    can drop the user into a half-completed create form when they click
    "Ajouter une alerte" next to a graph.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .alerts import SENSOR_KEY_REGISTRY, suggest_alert

        sensor_key = request.query_params.get("sensor_key")
        if not sensor_key:
            return Response(
                {"detail": "sensor_key is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if sensor_key not in SENSOR_KEY_REGISTRY:
            return Response(
                {"detail": f"Unknown sensor_key '{sensor_key}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        zone_param = request.query_params.get("zone_id")
        zone_id = int(zone_param) if zone_param and zone_param.isdigit() else None

        payload = suggest_alert(request.user, sensor_key=sensor_key, zone_id=zone_id)
        if payload is None:
            return Response(
                {"detail": "Unable to build suggestion."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(payload, status=status.HTTP_200_OK)


class AlertSensorKeysAPIView(APIView):
    """Returns the registry of sensor keys the front can attach alerts to."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .alerts import SENSOR_KEY_REGISTRY

        return Response(
            {
                "keys": [
                    {"key": key, "unit": meta["unit"], "label": meta["label"]}
                    for key, meta in sorted(SENSOR_KEY_REGISTRY.items())
                ]
            }
        )


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

# NPK is intentionally excluded — NpkSensor has three value fields and
# needs per-key field routing. Tracked separately.
_INGEST_SKIP_KEYS: set[str] = {"npk"}


class WeatherIngestAPIView(APIView):
    """
    Expects a JSON OBJECT (not list), same as what the Node bridge forwards.

    Registry-driven: any key in ``SENSOR_KEY_REGISTRY`` that appears in the
    payload with a non-None value is written to the matching model and
    handed to ``dispatch_alerts_for_reading`` so alert emails can fire.

    Keys outside the registry are silently dropped — this matches the
    legacy ``METRIC_KEYS`` behaviour for unknown fields while letting new
    sensors come online by adding a registry entry, no view change needed.
    """

    authentication_classes = []
    permission_classes = []

    def post(self, request):
        from analytics.alerts import (
            SENSOR_KEY_REGISTRY,
            dispatch_alerts_for_reading,
            get_sensor_model,
        )

        payload = request.data

        if not isinstance(payload, dict):
            return Response(
                {"error": "Expected a JSON object"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Collect known sensor keys present in the payload with a usable
        # value. Two keys can resolve to the same model (back-compat aliases:
        # ``soil_ph`` → ``PhSoil``, ``et0`` → ``Et0Calculated``); we accept
        # both as the device may emit either.
        metrics = {
            key: payload[key]
            for key in SENSOR_KEY_REGISTRY
            if key in payload
            and payload[key] is not None
            and key not in _INGEST_SKIP_KEYS
        }

        if not metrics:
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
        for sensor_key, value in metrics.items():
            model_cls = get_sensor_model(sensor_key)
            model_cls.objects.create(user=user, zone=zone, value=value, timestamp=now)
            inserted += 1
            # Alert dispatch must never abort the ingest loop: the sensor row
            # is already persisted, and a downstream alerts bug (schema drift,
            # broker outage, etc.) shouldn't drop the remaining keys in the
            # payload — we hit exactly that on prod with a missing column on
            # analytics_alert.
            try:
                dispatch_alerts_for_reading(
                    sensor_key=sensor_key,
                    zone=zone,
                    user=user,
                    value=value,
                    timestamp=now,
                )
            except Exception:
                logger.exception(
                    "alert dispatch failed for sensor_key=%s user=%s zone=%s",
                    sensor_key,
                    getattr(user, "username", user),
                    zone.id,
                )

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
        rows = Notification.objects.filter(user=request.user).order_by(
            "-notification_date"
        )[:200]

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

        recipient = (data.get("contactEmail") or "").strip() or (
            getattr(request.user, "email", "") or ""
        ).strip()
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
