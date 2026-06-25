"""Compatibility re-export surface for the former ``analytics`` god-app.

Phase 5 split the analytics models into domain apps:

* ``apps.irrigation`` — Zone, Kc, KcPeriod, KcPeriodAssignment, GraphName,
  ActiveGraph, ManagerAffirmation (+ the Zone/User post_save signals).
* ``apps.alerts`` — Notification, Alert.
* ``apps.sensors`` — the 38 sensor reading models + SensorColor,
  SensorLocation, UserSensorUnitPreference.

Every model keeps ``Meta.app_label = "analytics"`` so the historical
``analytics_<model>`` db_tables and ``analytics.<Model>`` FK references are
unchanged — only the Python modules moved. This module re-exports all of
them so existing ``from analytics.models import Zone`` / ``Alert`` / etc.
imports keep working. New code should import from the domain app directly.
"""

from apps.alerts.models import (
    Alert,
    Notification,
    NotificationZone,
    NotificationZoneSensor,
)
from apps.irrigation.models import (
    ActiveGraph,
    GraphName,
    IrrigationProgram,
    Kc,
    KcPeriod,
    KcPeriodAssignment,
    ManagerAffirmation,
    OutputCommand,
    TechnicianGrant,
    TechnicianZoneGrant,
    Zone,
)
from apps.sensors.models import (
    ECSoilHigh,
    ECSoilLow,
    ECSoilMedium,
    EcSalinitySensor,
    ElectricityConsumptionSensor,
    Et0Calculated,
    Et0Weather,
    FruitSizeSensor,
    HumidityWeather,
    LargeFruitDiameterSensor,
    LeafMoistureSensor,
    LeafTemperatureSensor,
    MultiDepthSoilMoistureSensor,
    BatterySensor,
    NpkSensor,
    PhSoil,
    PhWaterSensor,
    PrecipitationRate,
    SignalSensor,
    PressureWeather,
    SensorColor,
    SensorLocation,
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
    UserSensorUnitPreference,
    VPDWeather,
    WaterECSensor,
    WaterFlowSensor,
    WaterLevelSensor,
    WaterPressureSensor,
    WindDirection,
    WindSpeed,
)

__all__ = [
    # irrigation
    "ActiveGraph",
    "GraphName",
    "IrrigationProgram",
    "Kc",
    "KcPeriod",
    "KcPeriodAssignment",
    "ManagerAffirmation",
    "OutputCommand",
    "TechnicianGrant",
    "TechnicianZoneGrant",
    "Zone",
    # alerts
    "Alert",
    "NotificationZone",
    "NotificationZoneSensor",
    "Notification",
    # sensors
    "ECSoilHigh",
    "ECSoilLow",
    "ECSoilMedium",
    "EcSalinitySensor",
    "ElectricityConsumptionSensor",
    "Et0Calculated",
    "Et0Weather",
    "FruitSizeSensor",
    "HumidityWeather",
    "LargeFruitDiameterSensor",
    "LeafMoistureSensor",
    "LeafTemperatureSensor",
    "MultiDepthSoilMoistureSensor",
    "BatterySensor",
    "NpkSensor",
    "PhSoil",
    "PhWaterSensor",
    "PrecipitationRate",
    "SignalSensor",
    "PressureWeather",
    "SensorColor",
    "SensorLocation",
    "SoilConductivitySensor",
    "SoilMoistureHigh",
    "SoilMoistureLow",
    "SoilMoistureMedium",
    "SoilSalinitySensor",
    "SoilTemperatureHigh",
    "SoilTemperatureLow",
    "SoilTemperatureMedium",
    "SolarRadiation",
    "TemperatureWeather",
    "UserSensorUnitPreference",
    "VPDWeather",
    "WaterECSensor",
    "WaterFlowSensor",
    "WaterLevelSensor",
    "WaterPressureSensor",
    "WindDirection",
    "WindSpeed",
]
