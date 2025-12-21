# notification_helper.py
from datetime import timedelta

from django.utils.timezone import now

from .models import CustomUser  # ou ton modèle utilisateur


def should_notify(user):
    if not user.last_notified:
        return True
    elapsed = now() - user.last_notified
    return elapsed.total_seconds() >= user.notify_every * 3600


from django.utils.timezone import now


def perform_calculations(user):
    """
    Génère un message fictif pour l'utilisateur, à personnaliser
    une fois que les données agricoles sont disponibles.
    """
    date_today = now().strftime("%d/%m/%y")
    user_name = user.firstname or user.username

    # Données fictives simulées pour l'exemple
    temp_avg = 26
    humidity_avg = 45  # HumidityWeather : moyenne de l'humidité de l'air d'une journée
    evap_mm = 3.2
    last_irrigation_date = "05/06/2025"
    last_irrigation_amount = "50L/1h"
    soil_moisture = 38  # dyal lahda li katjik notification
    soil_temp = 23  # dyal lahda li katjik notification
    ec = 1.1  # dyal lahda li katjik notification
    npk = "80/40/20"  # dyal lahda li katjik notification
    salinity = "2.3 dS/m / 1600 mg/L"  # dyal lahda li katjik notification
    irrigation_decision = "500 litres → 50 minutes"

    message = f"""\
Bonjour {user_name}
Les informations concernant votre terrain agricole
💡 Alerte d’irrigation quotidienne – zone 1
📅 Date : {date_today}

Prévisions météo (2 jours) :
🌡 Température moyenne d’hier à aujourd’hui : {temp_avg}°C
💧 Humidité moyenne de l’air d’hier à aujourd’hui : {humidity_avg}%
🌞 Quantité d’eau évaporée hier : {evap_mm} mm

Dernière opération d’irrigation :
🚰 Dernier arrosage le {last_irrigation_date} : {last_irrigation_amount}

Informations d’aujourd’hui :
🌱 Humidité actuelle du sol : {soil_moisture}%
🌡 Température actuelle du sol : {soil_temp}°C
⚡ Conductivité électrique du sol (EC) : {ec} dS/m
N.P.K – Quantité d’engrais : {npk} mg/kg
Salinité et conductivité du sol : {salinity}

💧 Décision d’irrigation pour aujourd’hui :
{irrigation_decision}
"""
    return message
