from rest_framework import serializers

from .models import *


class GraphNameSerializer(serializers.ModelSerializer):
    class Meta:
        model = GraphName
        fields = "__all__"


class SensorColorSerializer(serializers.ModelSerializer):
    class Meta:
        model = SensorColor
        fields = "__all__"


class ActiveGraphSerializer(serializers.ModelSerializer):
    class Meta:
        model = ActiveGraph
        exclude = ["id"]


class ZonesNameSerializer(serializers.ModelSerializer):
    class Meta:
        model = Zone
        fields = ["id", "name"]


class BaseSensorSerializer(serializers.ModelSerializer):
    timestamp = serializers.DateTimeField(format="%Y-%m-%d")
    # value = serializers.SerializerMethodField()

    default_unit = serializers.SerializerMethodField()
    available_units = serializers.SerializerMethodField()

    def get_default_unit(self, obj):
        return obj.default_unit

    def get_available_units(self, obj):
        return obj.available_units

    # def get_value(self, obj):
    #     # Format float to string with 2 decimals
    #     return f"{obj.value:.2f}"

    class Meta:
        fields = "__all__"


class AlertSerializer(serializers.ModelSerializer):
    threshold = serializers.SerializerMethodField()

    class Meta:
        model = Alert
        fields = [
            "id",
            "name",
            "type",
            "description",
            "condition",
            "condition_nbr",
            "threshold",
            "sensor_key",
            "zone",
            "is_active",
            "last_triggered_at",
            "created_at",
            "updated_at",
            "user",
        ]
        read_only_fields = ["last_triggered_at", "created_at", "updated_at", "user"]

    def get_threshold(self, obj) -> float:
        return float(obj.condition_nbr) if obj.condition_nbr is not None else None

    def validate_sensor_key(self, value):
        from .alerts import SENSOR_KEY_REGISTRY

        if value and value not in SENSOR_KEY_REGISTRY:
            raise serializers.ValidationError(
                f"Unknown sensor_key '{value}'. Allowed: "
                f"{sorted(SENSOR_KEY_REGISTRY.keys())}"
            )
        return value

    def validate(self, attrs):
        # When the caller is writing a zone, make sure they own it.
        request = self.context.get("request")
        zone = attrs.get("zone")
        if request and zone and zone.user_id != request.user.id:
            raise serializers.ValidationError({"zone": "Not your zone."})
        return attrs
