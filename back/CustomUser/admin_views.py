"""
Admin-side views for the CustomUser app.

All routes are gated behind `IsAuthenticated + IsAdminUser`.
Endpoints follow REST conventions (one URL, one HTTP verb per
operation) and the project's serializer/permission norms.
"""

import logging
import secrets

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Count
from rest_framework import generics, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .admin_serializers import (
    AdminUserCreateSerializer,
    AdminUserDetailSerializer,
    AdminUserListSerializer,
)
from .models import CustomUser

logger = logging.getLogger(__name__)


def _annotate_users(qs):
    return qs.annotate(zones_count=Count("zones", distinct=True))


class AdminUserListCreateAPIView(generics.ListCreateAPIView):
    """GET /auth/admin/users/  →  paginated list
    POST /auth/admin/users/  →  create
    """

    permission_classes = [IsAuthenticated, IsAdminUser]

    def get_queryset(self):
        qs = _annotate_users(CustomUser.objects.exclude(pk=self.request.user.pk))
        search = self.request.query_params.get("search")
        if search:
            qs = qs.filter(username__icontains=search) | qs.filter(
                email__icontains=search
            )
        return qs.order_by("-date_joined")

    def get_serializer_class(self):
        if self.request.method == "POST":
            return AdminUserCreateSerializer
        return AdminUserListSerializer

    def create(self, request, *args, **kwargs):
        write_serializer = self.get_serializer(data=request.data)
        write_serializer.is_valid(raise_exception=True)
        user = write_serializer.save()
        user = _annotate_users(CustomUser.objects.filter(pk=user.pk)).first()
        read_serializer = AdminUserDetailSerializer(user)
        return Response(read_serializer.data, status=status.HTTP_201_CREATED)


class AdminUserDetailAPIView(generics.RetrieveUpdateDestroyAPIView):
    """GET / PATCH / DELETE /auth/admin/users/<username>/"""

    permission_classes = [IsAuthenticated, IsAdminUser]
    serializer_class = AdminUserDetailSerializer
    lookup_field = "username"
    http_method_names = ["get", "patch", "delete", "head", "options"]

    def get_queryset(self):
        return _annotate_users(CustomUser.objects.all())

    def perform_destroy(self, instance):
        if instance.pk == self.request.user.pk:
            raise ValidationError("You cannot delete your own admin account.")
        # Soft-delete: deactivate rather than drop the row so historical
        # zones/alerts stay attached. Hard delete remains the manual
        # ops path.
        instance.is_active = False
        instance.save(update_fields=["is_active"])


class AdminUserActivateAPIView(APIView):
    """POST /auth/admin/users/<username>/activate/  →  toggle is_active"""

    permission_classes = [IsAuthenticated, IsAdminUser]

    def post(self, request, username):
        try:
            user = CustomUser.objects.get(username=username)
        except CustomUser.DoesNotExist as exc:
            raise NotFound("User not found.") from exc

        is_active = request.data.get("is_active")
        if is_active is None:
            user.is_active = not user.is_active
        else:
            if not isinstance(is_active, bool):
                raise ValidationError({"is_active": "Must be a boolean."})
            user.is_active = is_active

        if user.pk == request.user.pk and not user.is_active:
            raise ValidationError("You cannot deactivate your own admin account.")

        user.save(update_fields=["is_active"])
        user = _annotate_users(CustomUser.objects.filter(pk=user.pk)).first()
        return Response(AdminUserDetailSerializer(user).data, status=status.HTTP_200_OK)


class AdminUserResetPasswordAPIView(APIView):
    """POST /auth/admin/users/<username>/reset-password/

    Body: `{ "password": "..." }`  (admin sets it directly)
    or empty: a random 16-char password is generated and returned once.
    """

    permission_classes = [IsAuthenticated, IsAdminUser]

    def post(self, request, username):
        try:
            user = CustomUser.objects.get(username=username)
        except CustomUser.DoesNotExist as exc:
            raise NotFound("User not found.") from exc

        new_password = request.data.get("password") or secrets.token_urlsafe(12)

        try:
            validate_password(new_password, user=user)
        except DjangoValidationError as exc:
            raise ValidationError({"password": list(exc.messages)}) from exc

        user.set_password(new_password)
        user.save(update_fields=["password"])

        logger.info(
            "admin %s reset password for %s", request.user.username, user.username
        )

        return Response(
            {"username": user.username, "password": new_password},
            status=status.HTTP_200_OK,
        )
