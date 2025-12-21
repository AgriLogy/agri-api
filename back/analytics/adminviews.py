from django.contrib.auth import get_user_model
from django.db.models import Q
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import *
from .serializers import *

User = get_user_model()


class AdminHeaderAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request):
        user = request.user
        return Response({"username": user.username}, status=status.HTTP_200_OK)


# Admin Part

CustomUser = get_user_model()


class ActiveGraphAdminAPIView(APIView):
    permission_classes = [IsAdminUser]  # Admins only

    def get_object(self, username, zone_id):
        try:
            user = CustomUser.objects.get(username=username)
        except CustomUser.DoesNotExist:
            return None

        try:
            return ActiveGraph.objects.get(user=user, zone_id=zone_id)
        except ActiveGraph.DoesNotExist:
            return None

    def get(self, request, username, zone_id):
        active_graph = self.get_object(username, zone_id)
        if not active_graph:
            return Response(
                {"detail": "ActiveGraph not found."}, status=status.HTTP_404_NOT_FOUND
            )

        serializer = ActiveGraphSerializer(active_graph)
        return Response(serializer.data)

    def put(self, request, username, zone_id):
        active_graph = self.get_object(username, zone_id)
        if not active_graph:
            return Response(
                {"detail": "ActiveGraph not found."}, status=status.HTTP_404_NOT_FOUND
            )

        serializer = ActiveGraphSerializer(
            active_graph, data=request.data, partial=True
        )
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
