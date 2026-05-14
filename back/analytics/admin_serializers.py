"""Admin-side serializers for analytics (zones, params, units, alerts)."""

from rest_framework import serializers

from .models import Alert, Zone


class AdminZoneSerializer(serializers.ModelSerializer):
    """Full zone surface for admin CRUD."""

    class Meta:
        model = Zone
        fields = [
            "id",
            "name",
            "space",
            "soil_param_TAW",
            "soil_param_FC",
            "soil_param_WP",
            "soil_param_RAW",
            "critical_moisture_threshold",
            "pomp_flow_rate",
            "irrigation_water_quantity",
        ]
        read_only_fields = ["id"]

    def validate_space(self, value):
        if value is None or value <= 0:
            raise serializers.ValidationError("Space must be strictly positive.")
        return value

    def validate_critical_moisture_threshold(self, value):
        if value is None or value < 0 or value > 100:
            raise serializers.ValidationError(
                "Threshold must be between 0 and 100 (percent)."
            )
        return value

    def validate_pomp_flow_rate(self, value):
        if value is None or value < 0:
            raise serializers.ValidationError("Flow rate must be non-negative.")
        return value


class AdminZoneParamsSerializer(serializers.ModelSerializer):
    """Soil + irrigation params subset — used by ParamsTab."""

    class Meta:
        model = Zone
        fields = [
            "id",
            "soil_param_TAW",
            "soil_param_FC",
            "soil_param_WP",
            "soil_param_RAW",
            "critical_moisture_threshold",
            "pomp_flow_rate",
            "irrigation_water_quantity",
        ]
        read_only_fields = ["id"]

    def validate(self, attrs):
        fc = attrs.get("soil_param_FC")
        wp = attrs.get("soil_param_WP")
        if fc is not None and wp is not None and fc < wp:
            raise serializers.ValidationError(
                "Field capacity (FC) cannot be lower than wilting point (WP)."
            )
        return attrs


class AdminAlertSerializer(serializers.ModelSerializer):
    """Admin can read + flip is_active + delete; cannot change owner."""

    class Meta:
        model = Alert
        fields = [
            "id",
            "name",
            "type",
            "description",
            "condition",
            "condition_nbr",
            "sensor_key",
            "zone",
            "is_active",
            "last_triggered_at",
            "created_at",
            "updated_at",
            "user",
        ]
        read_only_fields = [
            "id",
            "name",
            "type",
            "description",
            "condition",
            "condition_nbr",
            "sensor_key",
            "zone",
            "last_triggered_at",
            "created_at",
            "updated_at",
            "user",
        ]
