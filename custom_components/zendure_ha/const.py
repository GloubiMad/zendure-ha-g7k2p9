"""Constants for Zendure."""

from datetime import timedelta
from enum import Enum

DOMAIN = "zendure_ha"

CONF_APPTOKEN = "token"
CONF_P1METER = "p1meter"
CONF_PRICE = "price"
CONF_MQTTLOG = "mqttlog"
CONF_MQTTLOCAL = "mqttlocal"
CONF_MQTTSERVER = "mqttserver"
CONF_SIM = "simulation"
CONF_MQTTPORT = "mqttport"
CONF_MQTTUSER = "mqttuser"
CONF_MQTTPSW = "mqttpsw"
CONF_WIFISSID = "wifissid"
CONF_WIFIPSW = "wifipsw"
CONF_AUTO_MQTT_USER = "auto_mqtt_user"

# InfluxDB v2 exporter (dedicated bucket telemetry trace)
CONF_INFLUX_ENABLE = "influx_enable"
CONF_INFLUX_URL = "influx_url"
CONF_INFLUX_ORG = "influx_org"
CONF_INFLUX_TOKEN = "influx_token"
CONF_INFLUX_BUCKET = "influx_bucket"
CONF_MQTT_INFLUX = "mqtt_influx"  # journaliser les messages MQTT bruts -> bucket dédié zendure_mqtt

CONF_HAKEY = "C*dafwArEOXK"


class AcMode:
    INPUT = 1
    OUTPUT = 2


class DeviceState(Enum):
    OFFLINE = 0
    SOCEMPTY = 1
    INACTIVE = 2
    SOCFULL = 3
    ACTIVE = 4


class ManagerMode(Enum):
    OFF = 0
    MANUAL = 1
    MATCHING = 2
    MATCHING_DISCHARGE = 3
    MATCHING_CHARGE = 4
    STORE_SOLAR = 5
    MONITOR = 6
    SMART_BUFFER = 7  # moteur "buffers + rampe" (inspiré Gielz) : seuils start + buffer + facteur 0.75->1.0
    QUICK_CHARGE = 8  # charge à fond (limite device)
    QUICK_DISCHARGE = 9  # décharge à fond (limite device)


class ManagerState(Enum):
    IDLE = 0
    CHARGE = 1
    DISCHARGE = 2
    OFF = 3


class SmartMode:
    SOCFULL = 1
    SOCEMPTY = 2
    ZENSDK = 2
    CONNECTED = 10

    TIMEFAST = 2.2  # Fast update interval after significant change
    TIMEZERO = 4  # Normal update interval

    # Standard deviation thresholds for detecting significant changes
    P1_STDDEV_FACTOR = 3.5  # Multiplier for P1 meter stddev calculation
    P1_STDDEV_MIN = 15  # Minimum stddev value for P1 changes (watts)
    P1_MIN_UPDATE = timedelta(milliseconds=400)
    SETPOINT_STDDEV_FACTOR = 5.0  # Multiplier for power average stddev calculation
    SETPOINT_STDDEV_MIN = 50  # Minimum stddev value for power average (watts)

    HEMSOFF_TIMEOUT = 60  # Seconds before HEMS state is set to OFF if no updates are received

    POWER_START = 50  # Minimum Power (W) for starting a device
    POWER_TOLERANCE = 5  # Device-level power tolerance (W) before updating

    # lastseen est stampé à message_time + cet offset ; le watchdog le retranche pour
    # retrouver l'heure réelle du dernier message (5 min = timedelta(minutes=5) côté device).
    MQTT_LASTSEEN_OFFSET = 300
    # Watchdog de fraîcheur MQTT : un broker peut garder la session TCP vivante
    # (is_connected()==True) tout en ne livrant plus le topic d'un device → l'appli Zendure
    # voit les données mais HA ne reçoit rien (cas vécu : up muet après maj firmware).
    # Les Hyper publient ~toutes les 1s (jour) et au pire au poll coordinateur (60s) → seuils
    # AU-DESSUS de 60s pour ne pas re-souscrire à tort à chaque poll. Re-souscrire est idempotent
    # et léger (on peut être réactif) ; la reconnexion est lourde → elle reste prudente.
    MQTT_STALE_TIMEOUT = 90  # s de silence avant de re-souscrire un device (action légère)
    MQTT_RECONNECT_TIMEOUT = 300  # silence TOTAL du client avant reconnexion forcée (action lourde)
    MQTT_RESUB_COOLDOWN = 90  # s minimum entre 2 actions de récupération par device
