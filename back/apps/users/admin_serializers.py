"""
Admin-side serializers for the CustomUser app.

Split from `serializers.py` so the user-facing UserSerializer keeps
its narrow surface while admin endpoints get richer read shapes
(zones_count, date_joined, last_login) and strict write validation.
"""

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .models import CustomUser


class AdminUserListSerializer(serializers.ModelSerializer):
    """Compact row for the admin user list."""

    zones_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = CustomUser
        fields = [
            "id",
            "username",
            "email",
            "firstname",
            "lastname",
            "is_active",
            "is_staff",
            "payement_status",
            "zones_count",
            "date_joined",
            "last_login",
        ]
        read_only_fields = fields


class AdminUserDetailSerializer(serializers.ModelSerializer):
    """Read + write surface for a single user from the admin console."""

    zones_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = CustomUser
        fields = [
            "id",
            "username",
            "email",
            "firstname",
            "lastname",
            "phone_number",
            "latitude",
            "longitude",
            "payement_status",
            "is_active",
            "is_staff",
            "notify_every",
            "last_notified",
            "date_joined",
            "last_login",
            "zones_count",
        ]
        read_only_fields = [
            "id",
            "username",
            "date_joined",
            "last_login",
            "last_notified",
            "zones_count",
        ]

    def validate_latitude(self, value):
        if value is None:
            return value
        if value < -90 or value > 90:
            raise serializers.ValidationError("Latitude must be between -90 and 90.")
        return value

    def validate_longitude(self, value):
        if value is None:
            return value
        if value < -180 or value > 180:
            raise serializers.ValidationError("Longitude must be between -180 and 180.")
        return value

    def validate_notify_every(self, value):
        if value is None:
            return value
        if value < 1 or value > 168:
            raise serializers.ValidationError(
                "notify_every must be between 1 and 168 hours."
            )
        return value

    def validate_email(self, value):
        qs = CustomUser.objects.filter(email__iexact=value)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("This email is already in use.")
        return value


class AdminUserCreateSerializer(serializers.ModelSerializer):
    """Admin creates a user (with a chosen role + password)."""

    password = serializers.CharField(write_only=True, required=True)

    class Meta:
        model = CustomUser
        fields = [
            "username",
            "email",
            "firstname",
            "lastname",
            "phone_number",
            "latitude",
            "longitude",
            "password",
            "is_staff",
            "payement_status",
        ]

    def validate_username(self, value):
        if CustomUser.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError("This username is already in use.")
        return value

    def validate_email(self, value):
        if CustomUser.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("This email is already in use.")
        return value

    def validate_password(self, value):
        try:
            validate_password(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages)) from exc
        return value

    def validate_latitude(self, value):
        if value is None:
            return value
        if value < -90 or value > 90:
            raise serializers.ValidationError("Latitude must be between -90 and 90.")
        return value

    def validate_longitude(self, value):
        if value is None:
            return value
        if value < -180 or value > 180:
            raise serializers.ValidationError("Longitude must be between -180 and 180.")
        return value

    def create(self, validated_data):
        password = validated_data.pop("password")
        user = CustomUser(**validated_data)
        user.set_password(password)
        user.save()
        return user
