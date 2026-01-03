import os
import random
from datetime import datetime, timedelta

import django
from faker import Faker

# Set up Django environment
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "agriBack.settings")
django.setup()

from analytics.models import *
from CustomUser.models import CustomUser
from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist

# Initialize Faker
fake = Faker()

# Define date range for data
BEGIN_DATE = datetime(year=2025, month=4, day=30)  # Change as needed
END_DATE = datetime(year=2025, month=6, day=22)  # Change as needed


def create_user1_zone1():
    try:
        user, created = CustomUser.objects.get_or_create(
            username="user1",
            defaults={
                "firstname": "John",
                "lastname": "Doe",
                "email": "john.doe@example.com",
                "phone_number": "1234567890",
                "payement_status": "actif",
                "is_active": True,
                "is_staff": False,
                "is_superuser": False,
            },
        )

        if created:
            user.set_password("@Agrogo321")
            user.save()
    except Exception as e:
        print(f"❌ An error occurred while creating or getting the user: {e}")
        exit()

    print(f"✅ Found user: {user.username}")

    zone, _ = Zone.objects.get_or_create(
        user=user,
        name="zone de marichage 1",
        space=1750.0,
        defaults={
            "space": 1750.0,
            "critical_moisture_threshold": 18.0,
            "pomp_flow_rate": 1.0,
        },
    )
    print(f"✅ Found or created zone: {zone.name}")
    return user, zone


def generate_random_datetimes(start: datetime, end: datetime, count: int):
    """Return a list of evenly spaced datetime points between start and end."""
    delta = (end - start) / count
    return [start + i * delta for i in range(count)]


def SensorDataGenerator(model_class, sensor_name: str, color="#543141"):
    print(f"📊 Generating {sensor_name} data...")
    try:
        data_points = 50  # Number of dummy records
        timestamps = generate_random_datetimes(BEGIN_DATE, END_DATE, data_points)

        for timestamp in timestamps:
            model_class.objects.create(
                zone=zone,
                user=user,
                value=round(random.uniform(10.0, 360.0), 2),
                timestamp=timestamp,
            )
        print(f"✅ Created {data_points} {sensor_name} records.")
    except Exception as e:
        print(f"❌ Failed to create {sensor_name} data: {e}")

    print(f"🎉 Successfully created {sensor_name} records for {user.username}!")


def SensorDataDeletor(model_class, sensor_name: str, user=None, zone=None):
    print(f"🗑️ Deleting {sensor_name} data...")

    try:
        queryset = model_class.objects.all()

        if user:
            queryset = queryset.filter(user=user)
        if zone:
            queryset = queryset.filter(zone=zone)

        count = queryset.count()
        queryset.delete()

        print(f"✅ Deleted {count} {sensor_name} records.")
    except Exception as e:
        print(f"❌ Failed to delete {sensor_name} data: {e}")

    print(f"🧹 Cleanup completed for {sensor_name} records.")


def NpkSensorGenerator():
    NpkSensor.objects.filter(user=user, zone=zone).delete()
    print("🧹 Cleared existing NpkSensor records.")

    # Generate new NPK sensor data
    print("📊 Generating NpkSensor data...")

    try:
        data_points = 50
        timestamps = generate_random_datetimes(BEGIN_DATE, END_DATE, data_points)

        for timestamp in timestamps:
            NpkSensor.objects.create(
                zone=zone,
                user=user,
                timestamp=timestamp,
                nitrogen_value=round(random.uniform(30.0, 70.0), 2),
                nitrogen_color="#dba800",
                nitrogen_courbe_name="N_curve",
                phosphorus_value=round(random.uniform(10.0, 40.0), 2),
                phosphorus_color="#00a86b",
                phosphorus_courbe_name="P_curve",
                potassium_value=round(random.uniform(20.0, 60.0), 2),
                potassium_color="#4682b4",
                potassium_courbe_name="K_curve",
            )

        print(f"✅ Created {data_points} NpkSensor records.")
    except Exception as e:
        print(f"❌ Failed to create NpkSensor data: {e}")


user, zone = create_user1_zone1()


def create_active_graph_for_user_zone(user, zone):
    try:
        active_graph, created = ActiveGraph.objects.get_or_create(
            user=user,
            zone=zone,
            defaults={
                # Soil
                "soil_irrigation_status": True,
                "soil_ph_status": True,
                "soil_conductivity_status": True,
                "soil_moisture_status": True,
                "soil_temperature_status": True,
                # Weather
                "et0_status": True,
                "wind_speed_status": True,
                "wind_direction_status": True,
                "solar_radiation_status": True,
                "temperature_humidity_weather_status": True,
                "precipitation_humidity_rate_status": True,
                "pluviometry_status": True,
                "data_table_status": True,
                # Added missing weather fields
                "wind_radar_status": True,
                "cumulative_precipitation_status": True,
                "precipitation_rate_status": True,
                "weather_temperature_humidity_status": True,
                # Water
                "water_flow_status": True,
                "water_pressure_status": True,
                "water_ph_status": True,
                "water_ec_status": True,
                # Plant Sensors
                "leaf_sensor_status": True,
                "fruit_size_status": True,
                "large_fruit_diameter_status": True,
                # Fertilizer/Nutrients
                "npk_status": True,
                # Other
                "electricity_consumption_status": True,
            },
        )
        if created:
            print(
                f"✅ Created ActiveGraph for user '{user.username}' and zone '{zone.name}'."
            )
        else:
            print(
                f"ℹ️ ActiveGraph already exists for user '{user.username}' and zone '{zone.name}'."
            )
        return active_graph

    except Exception as e:
        print(f"❌ Error while creating/getting ActiveGraph: {e}")
        raise


active_graph = create_active_graph_for_user_zone(user, zone)

SensorDataDeletor(WindSpeed, "Wind Speed", user=user, zone=zone)
SensorDataDeletor(WindDirection, "Wind Direction", user=user, zone=zone)


SensorDataGenerator(WindSpeed, "WindSpeed")
SensorDataGenerator(WindDirection, "WindDirection")

NpkSensorGenerator()

SensorDataGenerator(Notification, "Notification")
SensorDataGenerator(Alert, "Alert")
SensorDataGenerator(Zone, "Zone")
SensorDataGenerator(KcPeriod, "KcPeriod")
SensorDataGenerator(Kc, "Kc")
SensorDataGenerator(KcPeriodAssignment, "KcPeriodAssignment")
SensorDataGenerator(Et0Calculated, "Et0Calculated")
SensorDataGenerator(Et0Weather, "Et0Weather")
SensorDataGenerator(PrecipitationRate, "PrecipitationRate")
SensorDataGenerator(HumidityWeather, "HumidityWeather")
SensorDataGenerator(SolarRadiation, "SolarRadiation")
SensorDataGenerator(PressureWeather, "PressureWeather")
SensorDataGenerator(TemperatureWeather, "TemperatureWeather")
SensorDataGenerator(ECSoilMedium, "ECSoilMedium")
SensorDataGenerator(SoilTemperatureMedium, "SoilTemperatureMedium")
SensorDataGenerator(ECSoilHigh, "ECSoilHigh")
SensorDataGenerator(ECSoilLow, "ECSoilLow")
SensorDataGenerator(SoilMoistureMedium, "SoilMoistureMedium")
SensorDataGenerator(SoilMoistureHigh, "SoilMoistureHigh")
SensorDataGenerator(SoilMoistureLow, "SoilMoistureLow")
SensorDataGenerator(PhSoil, "PhSoil")
SensorDataGenerator(SoilTemperatureLow, "SoilTemperatureLow")
SensorDataGenerator(SoilTemperatureHigh, "SoilTemperatureHigh")
SensorDataGenerator(WaterFlowSensor, "WaterFlowSensor")
SensorDataGenerator(WaterPressureSensor, "WaterPressureSensor")
SensorDataGenerator(WaterECSensor, "WaterECSensor")
SensorDataGenerator(PhWaterSensor, "PhWaterSensor")
SensorDataGenerator(ElectricityConsumptionSensor, "ElectricityConsumptionSensor")
SensorDataGenerator(LeafMoistureSensor, "LeafMoistureSensor")
SensorDataGenerator(LeafTemperatureSensor, "LeafTemperatureSensor")
SensorDataGenerator(MultiDepthSoilMoistureSensor, "MultiDepthSoilMoistureSensor")
SensorDataGenerator(LargeFruitDiameterSensor, "LargeFruitDiameterSensor")
SensorDataGenerator(WaterLevelSensor, "WaterLevelSensor")
SensorDataGenerator(SoilSalinitySensor, "SoilSalinitySensor")
SensorDataGenerator(SoilConductivitySensor, "SoilConductivitySensor")
SensorDataGenerator(NpkSensor, "NpkSensor")
SensorDataGenerator(FruitSizeSensor, "FruitSizeSensor")
SensorDataGenerator(EcSalinitySensor, "EcSalinitySensor")
SensorDataGenerator(SensorColor, "SensorColor")
SensorDataGenerator(SensorLocation, "SensorLocation")
SensorDataGenerator(GraphName, "GraphName")
SensorDataGenerator(ActiveGraph, "ActiveGraph")
