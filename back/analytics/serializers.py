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
    class Meta:
        model = Alert
        fields = "__all__"
