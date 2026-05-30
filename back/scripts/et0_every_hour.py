import os
import sys

import django

# src/ layout: make back/src/ importable when run outside the container
# (the Docker image sets PYTHONPATH=/code/src; this covers local runs).
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "agriapi.settings")
django.setup()

from datetime import datetime, timedelta

from analytics.models import (
    HumidityWeather,
    SolarRadiation,
    TemperatureWeather,
    WaterPressureSensor,
    WindSpeed,
)
from apps.users.models import CustomUser
from django.db.models import Avg
from django.utils import timezone

one_hour_ago = timezone.now() - timedelta(hours=1)
day_of_the_year = datetime.now().timetuple().tm_yday
print(f"Day of the year: {day_of_the_year}")
users = CustomUser.objects.all()


def get_last_hour_avg_sensor_data(sensor_model, user):
    return (
        sensor_model.objects.filter(
            user=user, timestamp__gte=one_hour_ago, timestamp__lte=timezone.now()
        ).aggregate(avg_value=Avg("value"))["avg_value"]
        or 0
    )


import math
from datetime import datetime


def calculate_et0(
    lat,
    lon,
    avg_temp,
    avg_air_pressure,
    humidity,
    avg_solar_radiation,
    avg_wind_speed,
    day_of_year=None,
):
    """
    Calculates ET0 (mm/day) using FAO-56 Penman-Monteith method.
    lat: latitude in decimal degrees
    lon: longitude in decimal degrees (not used directly here)
    avg_temp: mean air temperature (°C)
    avg_air_pressure: kPa
    humidity: relative humidity (%)
    avg_solar_radiation: MJ/m²/day (or W/m² if converted)
    avg_wind_speed: m/s at 2m height
    day_of_year: integer (1-366); if None, will take today's day of year
    """
    if day_of_year is None:
        day_of_year = datetime.now().timetuple().tm_yday

    # Convert latitude to radians
    phi = math.radians(lat)

    # Extraterrestrial radiation Ra (MJ/m²/day)
    dr = 1 + 0.033 * math.cos(2 * math.pi / 365 * day_of_year)
    delta = 0.409 * math.sin(2 * math.pi / 365 * day_of_year - 1.39)
    omega_s = math.acos(-math.tan(phi) * math.tan(delta))
    Gsc = 0.0820  # Solar constant (MJ/m²/min)
    Ra = (
        (24 * 60 / math.pi)
        * Gsc
        * dr
        * (
            omega_s * math.sin(phi) * math.sin(delta)
            + math.cos(phi) * math.cos(delta) * math.sin(omega_s)
        )
    )  # MJ/m²/day

    # Convert solar radiation if in W/m²
    Rs = avg_solar_radiation * 0.0864  # W/m² → MJ/m²/day

    # Net shortwave radiation
    albedo = 0.23
    Rns = (1 - albedo) * Rs

    # Saturation vapor pressure (kPa)
    es = 0.6108 * math.exp((17.27 * avg_temp) / (avg_temp + 237.3))
    # Actual vapor pressure
    ea = es * (humidity / 100.0)

    # Slope of saturation vapor pressure curve
    delta_svp = (4098 * es) / ((avg_temp + 237.3) ** 2)
    # Psychrometric constant
    gamma = 0.000665 * avg_air_pressure

    # Soil heat flux (daily) ≈ 0   //need to clarify
    G = 0

    # Net radiation approximation
    Rn = Rns  # ignoring longwave for simplicity

    # Penman-Monteith ET0
    ET0 = (
        (0.408 * delta_svp * (Rn - G))
        + (gamma * (900 / (avg_temp + 273)) * avg_wind_speed * (es - ea))
    ) / (delta_svp + gamma * (1 + 0.34 * avg_wind_speed))

    return ET0


for user in users:
    longitude = user.longitude
    latitude = user.latitude
    avg_temp = get_last_hour_avg_sensor_data(TemperatureWeather, user)
    avg_water_pressure = get_last_hour_avg_sensor_data(WaterPressureSensor, user)
    humidity = get_last_hour_avg_sensor_data(HumidityWeather, user)
    avg_solar_radiation = get_last_hour_avg_sensor_data(SolarRadiation, user)
    avg_wind_speed = get_last_hour_avg_sensor_data(WindSpeed, user)
    if longitude is not None and latitude is not None:
        et0 = calculate_et0(
            lat=latitude,
            lon=longitude,
            avg_temp=avg_temp,
            avg_air_pressure=avg_water_pressure,
            humidity=humidity,
            avg_solar_radiation=avg_solar_radiation,
            avg_wind_speed=avg_wind_speed,
            day_of_year=day_of_the_year,
        )
        print(f"ET0 for user {user.username}: {et0:.2f} mm/day")
    else:
        print(f"User {user.username} does not have valid coordinates.")
