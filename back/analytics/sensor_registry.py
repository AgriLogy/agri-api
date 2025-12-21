from .models import (
    EcSalinitySensor,
    ECSoilHigh,
    ECSoilLow,
    ECSoilMedium,
    ElectricityConsumptionSensor,
    Et0Calculated,
    Et0Weather,
    FruitSizeSensor,
    HumidityWeather,
    LargeFruitDiameterSensor,
    LeafMoistureSensor,
    LeafTemperatureSensor,
    MultiDepthSoilMoistureSensor,
    NpkSensor,
    PhSoil,
    PhWaterSensor,
    PrecipitationRate,
    PressureWeather,
    SoilConductivitySensor,
    SoilMoistureHigh,
    SoilMoistureLow,
    SoilMoistureMedium,
    SoilSalinitySensor,
    SoilTemperatureHigh,
    SoilTemperatureLow,
    SoilTemperatureMedium,
    SolarRadiation,
    TemperatureWeather,
    WaterECSensor,
    WaterFlowSensor,
    WaterLevelSensor,
    WaterPressureSensor,
    WindDirection,
    WindSpeed,
)

SENSOR_MODELS = [
    PrecipitationRate,
    HumidityWeather,
    WindSpeed,
    SolarRadiation,
    PressureWeather,
    WindDirection,
    TemperatureWeather,
    ECSoilMedium,
    SoilTemperatureMedium,
    ECSoilHigh,
    ECSoilLow,
    SoilMoistureMedium,
    SoilMoistureHigh,
    SoilMoistureLow,
    PhSoil,
    SoilTemperatureLow,
    SoilTemperatureHigh,
    WaterFlowSensor,
    WaterECSensor,
    PhWaterSensor,
    ElectricityConsumptionSensor,
    LeafMoistureSensor,
    MultiDepthSoilMoistureSensor,
    LargeFruitDiameterSensor,
    WaterLevelSensor,
    SoilConductivitySensor,
    Et0Weather,
    Et0Calculated,
    LeafTemperatureSensor,
    SoilSalinitySensor,
    NpkSensor,
    FruitSizeSensor,
    EcSalinitySensor,
    WaterPressureSensor,
]

# Auto-generate serializers and views
generated_views = []
generated_urls = []

from django.urls import path
from rest_framework import serializers

from .serializers import BaseSensorSerializer
from .views import SensorDataMixin

for model in SENSOR_MODELS:
    model_name = model.__name__
    serializer_name = f"{model_name}Serializer"
    view_name = f"{model_name}View"

    # Create serializer class
    serializer_class = type(
        serializer_name,
        (BaseSensorSerializer,),
        {"Meta": type("Meta", (BaseSensorSerializer.Meta,), {"model": model})},
    )

    # Create view class
    view_class = type(
        view_name,
        (SensorDataMixin,),
        {"sensor_model": model, "serializer_class": serializer_class},
    )

    # Store the view for use in urls
    generated_views.append((model_name, view_class))


from rest_framework import serializers


class BaseSensorSerializer(serializers.ModelSerializer):
    default_unit = serializers.SerializerMethodField()
    available_units = serializers.SerializerMethodField()

    def get_default_unit(self, obj):
        return obj.default_unit

    def get_available_units(self, obj):
        return obj.available_units

    class Meta:
        fields = [
            "id",
            "value",
            "timestamp",
            "color",
            "default_unit",
            "available_units",
        ]
