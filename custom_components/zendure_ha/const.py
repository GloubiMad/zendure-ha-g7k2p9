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
    FONDATION = 6


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

    MQTT_STALE = 40  # (legacy) Secondes de silence MQTT avant reconnexion — remplacé par les paliers WD_* ci-dessous

    # Watchdog MQTT v2 : paliers d'escalade (s) — DÉFAUTS, ajustables À CHAUD via les number du Manager
    # (watchdog_getall / _power / _ble / _alert). Le PINGREQ keepalive k60 ne met PAS à jour lastseen
    # -> muet ≠ déconnecté. Cadence de veille mesurée nuit 08→09/07 : p95 ~60s, max 118s (up) / 181s (glagla)
    # -> les ACTES (puissance/BLE) doivent rester > ~120s pour ne pas partir sur de la veille normale ;
    # le getAll (lecture inoffensive) peut être bas pour la visibilité. Chaque probe DIFFÈRE du précédent.
    WD_GETALL = 45  # sonde légère (getAll) — bas = visibilité de tout silence, se reset si le device répond
    WD_POWER = 120  # commande de réveil (puissance ≠ dernière consigne) — au-dessus du plafond de veille de up
    WD_BLE = 160  # toggle broker BLE (autre puis retour au COURANT) — au-dessus du plafond de glagla (181s)
    WD_ALERT = 200  # notification + event : probable plantage firmware, reset physique requis
    WD_WAKE_NUDGE = 60  # W, écart de réveil quand la dernière consigne était nulle (≈ mini utile Hyper)
    WD_RECENT = 45  # s, fenêtre "a reparlé récemment" pour la garde d'entrée du toggle BLE
    WD_RESPONSE = 8  # s, si le device republie dans cette fenêtre après un probe -> le probe l'a réveillé ;
    #                  au-delà -> reprise SPONTANÉE (le "dernier palier atteint" ne prouve PAS la causalité)

    # BATTEMENT DE RÉGULATION (fix racine des états figés) : la régulation est 100% event-driven sur le
    # capteur P1 -> un P1 STABLE (même mauvais, ex. export) n'émet plus d'événement et ne se corrige jamais.
    # Le battement relance un cycle avec le dernier P1 ; au-delà de P1_STALE_MAX il s'abstient (P1 mort).
    P1_STALE_MAX = 120  # s, borne haute : ne pas réguler sur un P1 périmé de plus de 2 min
