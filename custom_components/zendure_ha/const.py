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
CONF_TELEGRAM_CONFIG_ENTRY_ID = "telegram_config_entry_id"
CONF_TELEGRAM_ENTITY_ID = "telegram_entity_id"

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

    # On every MQTT message a device's lastseen is stamped at now + this many
    # seconds; once wall-clock passes it the device is treated as offline.
    # The real time of the last message is therefore lastseen - this offset.
    MQTT_LASTSEEN_OFFSET = 300  # 5 min liveness window (shared device/manager)

    # MQTT staleness watchdog: a client can report is_connected()==True while
    # the broker has silently dropped a device's subscription, so no data
    # reaches HA. Zendure devices normally report every ~1s (day) and at worst
    # every ~60s (coordinator poll), so 150s of silence is already clearly
    # abnormal. We then re-subscribe the device's topics; if the WHOLE client
    # delivers nothing past MQTT_RECONNECT_TIMEOUT the socket is likely
    # half-open and we force a reconnect.
    MQTT_STALE_TIMEOUT = 150  # seconds of silence before re-subscribing a device
    MQTT_RECONNECT_TIMEOUT = 420  # total-blackout silence before forcing a reconnect
    MQTT_RESUB_COOLDOWN = 120  # minimum seconds between recovery actions per device
