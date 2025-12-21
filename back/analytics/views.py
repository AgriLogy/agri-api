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
