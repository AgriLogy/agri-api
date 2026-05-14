"""
Manager affirmation workflow.

Webapp user submits a sensitive change → a `ManagerAffirmation` row
is created in `pending`. An admin then approves or rejects it from
the backoffice.
"""

import logging

from django.utils import timezone
from rest_framework import generics, serializers, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import ManagerAffirmation

logger = logging.getLogger(__name__)


class ManagerAffirmationSerializer(serializers.ModelSerializer):
    """Read shape; status transitions go through dedicated endpoints."""

    requested_by_username = serializers.CharField(
        source="requested_by.username", read_only=True
    )
    decided_by_username = serializers.SerializerMethodField()

    class Meta:
        model = ManagerAffirmation
        fields = [
            "id",
            "action",
            "payload",
            "status",
            "requested_by",
            "requested_by_username",
            "decided_by",
            "decided_by_username",
            "decided_at",
            "decision_note",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "status",
            "requested_by",
            "requested_by_username",
            "decided_by",
            "decided_by_username",
            "decided_at",
            "decision_note",
            "created_at",
            "updated_at",
        ]

    def get_decided_by_username(self, obj):
        return obj.decided_by.username if obj.decided_by else None

    def validate_action(self, value):
        allowed = dict(ManagerAffirmation.ACTION_CHOICES)
        if value not in allowed:
            raise serializers.ValidationError(
                f"Unknown action. Allowed: {sorted(allowed.keys())}."
            )
        return value


class ManagerAffirmationListCreateAPIView(generics.ListCreateAPIView):
    """GET / POST /api/manager-affirmations/

    User can create; user can list their own. Admin sees all.
    Optional `?status=pending|approved|rejected` filter.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = ManagerAffirmationSerializer

    def get_queryset(self):
        qs = ManagerAffirmation.objects.all()
        if not self.request.user.is_staff:
            qs = qs.filter(requested_by=self.request.user)
        status_filter = self.request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        return qs.order_by("-created_at")

    def perform_create(self, serializer):
        serializer.save(requested_by=self.request.user)


class ManagerAffirmationDecisionAPIView(APIView):
    """POST /api/manager-affirmations/<pk>/approve|reject/

    Admin-only. Body: `{ "note": "optional reason" }`.
    """

    permission_classes = [IsAuthenticated, IsAdminUser]

    def post(self, request, pk, action):
        if action not in {"approve", "reject"}:
            raise ValidationError({"detail": "Action must be approve or reject."})

        try:
            affirmation = ManagerAffirmation.objects.get(pk=pk)
        except ManagerAffirmation.DoesNotExist as exc:
            raise NotFound("Affirmation not found.") from exc

        if affirmation.status != ManagerAffirmation.STATUS_PENDING:
            raise ValidationError(
                {"detail": f"Already decided ({affirmation.status})."}
            )

        affirmation.status = (
            ManagerAffirmation.STATUS_APPROVED
            if action == "approve"
            else ManagerAffirmation.STATUS_REJECTED
        )
        affirmation.decided_by = request.user
        affirmation.decided_at = timezone.now()
        affirmation.decision_note = request.data.get("note", "") or ""
        affirmation.save(
            update_fields=[
                "status",
                "decided_by",
                "decided_at",
                "decision_note",
                "updated_at",
            ]
        )

        logger.info(
            "admin %s %sd affirmation #%s",
            request.user.username,
            action,
            affirmation.pk,
        )
        return Response(
            ManagerAffirmationSerializer(affirmation).data,
            status=status.HTTP_200_OK,
        )
