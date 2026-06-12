"""Coordinator for Zendure integration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import traceback
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta
from math import sqrt
from pathlib import Path
from typing import Any

from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.auth.providers import homeassistant as auth_ha
from homeassistant.components import bluetooth, persistent_notification
from homeassistant.components.number import NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.loader import async_get_integration

from .api import Api
from .const import (
    CONF_AUTO_MQTT_USER,
    CONF_INFLUX_BUCKET,
    CONF_INFLUX_ENABLE,
    CONF_INFLUX_ORG,
    CONF_INFLUX_TOKEN,
    CONF_INFLUX_URL,
    CONF_P1METER,
    DOMAIN,
    DeviceState,
    ManagerMode,
    ManagerState,
    SmartMode,
)
from .device import DeviceSettings, ZendureDevice, ZendureLegacy
from .entity import EntityDevice
from .fusegroup import FuseGroup
from .number import ZendureRestoreNumber
from .select import ZendureRestoreSelect, ZendureSelect
from .sensor import ZendureSensor

SCAN_INTERVAL = timedelta(seconds=60)

_LOGGER = logging.getLogger(__name__)

type ZendureConfigEntry = ConfigEntry[ZendureManager]


class ZendureManager(DataUpdateCoordinator[None], EntityDevice):
    """Class to regular update devices."""

    devices: list[ZendureDevice] = []
    fuseGroups: list[FuseGroup] = []
    simulation: bool = False

    def __init__(self, hass: HomeAssistant, entry: ZendureConfigEntry) -> None:
        """Initialize Zendure Manager."""
        super().__init__(hass, _LOGGER, name="Zendure Manager", update_interval=SCAN_INTERVAL, config_entry=entry)
        EntityDevice.__init__(self, hass, "Zendure Manager", "Zendure Manager")
        self.api = Api()
        self.operation: ManagerMode = ManagerMode.OFF
        self.zero_next = datetime.min
        self.zero_fast = datetime.min
        self.check_reset = datetime.min
        self.p1meterEvent: Callable[[], None] | None = None
        self.p1_history: deque[int] = deque([25, -25], maxlen=8)
        self.p1_factor = 1
        self.update_count = 0

        # MQTT staleness watchdog bookkeeping (keyed by deviceId)
        self._last_msg: dict[str, datetime] = {}  # real wall-clock time of last message
        self._resubscribe_after: dict[str, datetime] = {}  # cooldown gate per device

        self.charge: list[ZendureDevice] = []
        self.charge_limit = 0
        self.charge_optimal = 0
        self.charge_time = datetime.max
        self.charge_last = datetime.min
        self.charge_weight = 0

        self.discharge: list[ZendureDevice] = []
        self.discharge_bypass = 0
        self.discharge_produced = 0
        self.discharge_limit = 0
        self.discharge_optimal = 0
        self.discharge_weight = 0

        self.idle: list[ZendureDevice] = []
        self.idle_lvlmax = 0
        self.idle_lvlmin = 0
        self.produced = 0
        self.pwr_low = 0

        # Maintien de charge anti cloud-flicker : tant que wall-clock < charge_hold_until,
        # un creux bref (P1 repassé positif) maintient un plancher de charge au lieu de couper.
        self.charge_hold_until = datetime.min
        self.setpoint = 0  # dernier setpoint de distribution calculé (télémétrie)
        self.last_p1 = 0  # dernier P1 vu (télémétrie)

        # Exporteur InfluxDB v2 (bucket dédié HA_ZENDURE) ; None si désactivé.
        self.influx: Any = None

    async def loadDevices(self) -> None:
        # Stamp the manager device with the integration's version before
        # creating its entities, so the device card shows the right version
        # even on cold boot when the Zendure API is unreachable.
        integration = await async_get_integration(self.hass, DOMAIN)
        if integration is not None:
            self.attr_device_info["sw_version"] = integration.manifest.get("version", "unknown")
        else:
            _LOGGER.error("Integration not found for domain: %s", DOMAIN)

        # Always create the manager's own entities, BEFORE attempting any
        # network I/O against the Zendure cloud API. Previously these were
        # created after Api.Connect(), so a transient cloud outage at boot
        # caused loadDevices() to return early and left Operation Mode +
        # all manager sensors permanently marked "unavailable" until the
        # next manual reload. Now they always come up; device entities
        # still depend on a successful API call below.
        self.operationmode = ZendureRestoreSelect(
            self,
            "Operation",
            {0: "off", 1: "manual", 2: "smart", 3: "smart_discharging", 4: "smart_charging", 5: "store_solar", 6: "monitor"},
            self.update_operation,
        )
        self.operationstate = ZendureSensor(self, "operation_state")
        self.manualpower = ZendureRestoreNumber(self, "manual_power", None, None, "W", "power", 12000, -12000, NumberMode.BOX, True)
        self.availableKwh = ZendureSensor(self, "available_kwh", None, "kWh", "energy_storage", None, 1)
        self.totalKwh = ZendureSensor(self, "total_kwh", None, "kWh", "energy_storage", "measurement", 2)
        self.power = ZendureSensor(self, "power", None, "W", "power", "measurement", 0)
        # Agrégats de parc pondérés par la capacité (recalculés à chaque update()):
        #  - weighted_soc : Σ(capacité × niveau) / Σ capacité → vrai SoC moyen %
        #  - usable_energy : Σ(énergie dispo) / Σ capacité × 100 → % utilisable au-dessus du SoC mini
        self.weightedSoc = ZendureSensor(self, "weighted_soc", None, "%", "battery", "measurement", 1)
        self.usableEnergy = ZendureSensor(self, "usable_energy", None, "%", None, "measurement", 1, icon="mdi:battery-arrow-down-outline")
        self.setpointSensor = ZendureSensor(self, "setpoint", None, "W", "power", "measurement", 0)
        # Maintien de charge anti cloud-flicker — réglables en direct, persistants :
        #  - charge_floor : charge maintenue (W) pendant un creux bref ; 0 = désactivé
        #  - charge_hold_window : durée (s) du maintien avant coupure réelle ; 0 = désactivé
        self.chargeFloor = ZendureRestoreNumber(self, "charge_floor", None, None, "W", "power", 300, 0, NumberMode.BOX, True)
        self.chargeHoldWindow = ZendureRestoreNumber(self, "charge_hold_window", None, None, "s", "duration", 300, 0, NumberMode.BOX, True)
        # Défauts au 1er démarrage (50 W / 45 s) ; la valeur restaurée prend le dessus ensuite.
        self.chargeFloor._attr_native_value = 50
        self.chargeHoldWindow._attr_native_value = 45

        if self.config_entry is None:
            return
        if (data := await Api.Connect(self.hass, dict(self.config_entry.data), True)) is None:
            _LOGGER.warning(
                "Zendure API unreachable at startup; manager entities remain available, "
                "device entities will appear after the next successful refresh"
            )
            return
        if (mqtt := data.get("mqtt")) is None:
            return

        # load devices
        for dev in data["deviceList"]:
            try:
                if (deviceId := dev["deviceKey"]) is None or (prodModel := dev["productModel"]) is None:
                    continue
                _LOGGER.info("Adding device: %s %s => %s", deviceId, prodModel, dev)

                init = Api.createdevice.get(prodModel.lower().strip(), None)
                if init is None:
                    _LOGGER.info("Device %s is not supported!", prodModel)
                    continue

                # create the device and mqtt server
                device = init(self.hass, deviceId, dev.get("deviceName", prodModel), dev)
                device.discharge_start = device.discharge_limit // 10
                device.discharge_optimal = device.discharge_limit // 4
                Api.devices[deviceId] = device

                # Check if we should automatically manage MQTT users (opt-in)
                auto_mqtt = self.config_entry.data.get(CONF_AUTO_MQTT_USER, False)
                if auto_mqtt and Api.localServer is not None and Api.localServer != "":
                    try:
                        psw = hashlib.md5(deviceId.encode()).hexdigest().upper()[8:24]  # noqa: S324
                        provider: auth_ha.HassAuthProvider = auth_ha.async_get_provider(self.hass)
                        credentials = await provider.async_get_or_create_credentials({"username": deviceId.lower()})
                        user = await self.hass.auth.async_get_user_by_credentials(credentials)
                        if user is None:
                            # Enforce local_only=True for technical MQTT accounts
                            user = await self.hass.auth.async_create_user(deviceId, group_ids=[GROUP_ID_USER], local_only=True)
                            await provider.async_add_auth(deviceId.lower(), psw)
                            await self.hass.auth.async_link_user(user, credentials)
                        else:
                            await provider.async_change_password(deviceId.lower(), psw)

                        _LOGGER.info("Managed MQTT user for device: %s", deviceId)

                    except Exception as err:
                        _LOGGER.error("Failed to manage MQTT user for %s: %s", deviceId, err)
                elif auto_mqtt:
                    _LOGGER.debug("Skipping auto MQTT user creation for %s: Local server not configured.", deviceId)

            except Exception as e:
                _LOGGER.error("Unable to create device %s!", e)
                _LOGGER.error(traceback.format_exc())

        self.devices = list(Api.devices.values())
        _LOGGER.info("Loaded %s devices", len(self.devices))

        # initialize the api & p1 meter
        self.api.Init(self.config_entry.data, mqtt)
        await self.update_fusegroups()
        self.update_p1meter(self.config_entry.data.get(CONF_P1METER, "sensor.power_actual"))
        await asyncio.sleep(1)  # allow other tasks to run

    async def update_fusegroups(self) -> None:
        _LOGGER.info("Update fusegroups")

        # updateFuseGroup callback
        async def updateFuseGroup(_entity: ZendureRestoreSelect, _value: Any) -> None:
            await self.update_fusegroups()

        fuseGroups: dict[str, FuseGroup] = {}
        for device in self.devices:
            try:
                if device.fuseGroup.onchanged is None:
                    device.fuseGroup.onchanged = updateFuseGroup

                fg: FuseGroup | None = None
                match device.fuseGroup.state:
                    case "owncircuit" | "group3600":
                        fg = FuseGroup(device.name, 3600, -3600)
                    case "group800":
                        fg = FuseGroup(device.name, 800, -1200)
                    case "group800_2400":
                        fg = FuseGroup(device.name, 800, -2400)
                    case "group1200":
                        fg = FuseGroup(device.name, 1200, -1200)
                    case "group2000":
                        fg = FuseGroup(device.name, 2000, -2000)
                    case "group2400":
                        fg = FuseGroup(device.name, 2400, -2400)
                    case "unused":
                        # only switch off, if Manager is used
                        if self.operation != ManagerMode.OFF:
                            await device.power_off()
                        continue
                    case _:
                        _LOGGER.debug("Device %s has unsupported fuseGroup state: %s", device.name, device.fuseGroup.state)
                        continue

                if fg is not None:
                    fg.devices.append(device)
                    fuseGroups[device.deviceId] = fg
            except AttributeError as err:
                _LOGGER.error("Device %s missing fuseGroup attribute: %s", device.name, err)
            except Exception as err:
                _LOGGER.error("Unable to create fusegroup for device %s (%s): %s", device.name, device.deviceId, err, exc_info=True)

        # Update the fusegroups and select options for each device
        for device in self.devices:
            try:
                fusegroups: dict[Any, str] = {
                    0: "unused",
                    1: "owncircuit",
                    2: "group800",
                    3: "group800_2400",
                    4: "group1200",
                    5: "group2000",
                    6: "group2400",
                    7: "group3600",
                }
                for deviceId, fg in fuseGroups.items():
                    if deviceId != device.deviceId:
                        fusegroups[deviceId] = f"Part of {fg.name} fusegroup"
                device.fuseGroup.setDict(fusegroups)
            except AttributeError as err:
                _LOGGER.error("Device %s missing fuseGroup attribute: %s", device.name, err)
            except Exception as err:
                _LOGGER.error("Unable to update fusegroup options for device %s (%s): %s", device.name, device.deviceId, err, exc_info=True)

        # Add devices to fusegroups
        for device in self.devices:
            if fg := fuseGroups.get(device.fuseGroup.value):
                device.fuseGrp = fg
                fg.devices.append(device)
            device.setStatus()

        # check if we can split fuse groups
        self.fuseGroups.clear()
        for fg in fuseGroups.values():
            if len(fg.devices) > 1 and fg.maxpower >= sum(d.discharge_limit for d in fg.devices) and fg.minpower <= sum(d.charge_limit for d in fg.devices):
                for d in fg.devices:
                    self.fuseGroups.append(FuseGroup(d.name, d.discharge_limit, d.charge_limit, [d]))
            else:
                for d in fg.devices:
                    d.fuseGrp = fg
                self.fuseGroups.append(fg)

    async def update_operation(self, entity: ZendureSelect, _operation: Any) -> None:
        operation = ManagerMode(entity.value)
        _LOGGER.info("Update operation: %s from: %s", operation, self.operation)

        self.operation = operation
        if self.p1meterEvent is not None:
            if operation != ManagerMode.OFF and (len(self.devices) == 0 or all(not d.online for d in self.devices)):
                _LOGGER.warning("No devices online, not possible to start the operation")
                persistent_notification.async_create(self.hass, "No devices online, not possible to start the operation", "Zendure", "zendure_ha")
                return

            match self.operation:
                case ManagerMode.OFF:
                    if len(self.devices) > 0:
                        for d in self.devices:
                            await d.power_off()

    async def _async_update_data(self) -> None:

        def isBleDevice(device: ZendureDevice, si: bluetooth.BluetoothServiceInfoBleak) -> bool:
            for d in si.manufacturer_data.values():
                try:
                    if d is None or len(d) <= 1:
                        continue
                    sn = d.decode("utf8")[:-1]
                    if device.snNumber.endswith(sn):
                        _LOGGER.info("Found Zendure Bluetooth device: %s", si)
                        device.attr_device_info["connections"] = {("bluetooth", str(si.address))}
                        return True
                except Exception:  # noqa: S112
                    continue
            return False

        time = datetime.now()
        kwh = 0
        weighted_soc = 0.0  # Σ(capacité × niveau de batterie)
        usable_kwh = 0.0  # Σ énergie disponible (au-dessus du SoC mini)
        for device in self.devices:
            kwh += device.kWh
            weighted_soc += device.kWh * device.electricLevel.asNumber
            usable_kwh += device.availableKwh.asNumber
            if isinstance(device, ZendureLegacy) and device.bleMac is None:
                for si in bluetooth.async_discovered_service_info(self.hass, False):
                    if isBleDevice(device, si):
                        break

            _LOGGER.debug("Update device: %s (%s)", device.name, device.deviceId)
            await device.dataRefresh(self.update_count)
            if device.hemsState.is_on and (time - device.hemsStateUpdated).total_seconds() > SmartMode.HEMSOFF_TIMEOUT:
                device.hemsState.update_value(0)
            device.setStatus()
        self.update_count += 1
        self.totalKwh.update_value(kwh)
        # Pourcentages pondérés par la capacité (ignorés tant qu'aucune capacité n'est connue).
        if kwh > 0:
            self.weightedSoc.update_value(round(weighted_soc / kwh, 1))
            self.usableEnergy.update_value(round(max(0.0, usable_kwh) / kwh * 100, 1))

        # Trace permanente vers InfluxDB (bucket dédié HA_ZENDURE), si configuré.
        # Fire-and-forget : une panne/lenteur InfluxDB ne doit jamais retarder le pilotage.
        if self.influx is not None:
            self.hass.async_create_background_task(self._write_influx(time), "zendure_influx_write")

        # MQTT watchdog: paho's loop_start() is supposed to auto-reconnect,
        # but silent failures happen (broker drops the session, token rotates,
        # network blip the reconnect logic gives up on...). If we detect a
        # dropped connection here, force a reconnect on the executor so we
        # never stay offline for more than one scan interval (60s).
        await self.hass.async_add_executor_job(self._check_mqtt_health)

        # Manually update the timer
        if self.hass and self.hass.loop.is_running():
            self._schedule_refresh()

    def _check_mqtt_health(self) -> None:
        """Verify both MQTT clients are still connected; reconnect if not."""

        def _check(client: Any, label: str) -> None:
            if client is None:
                return
            try:
                if client.is_connected():
                    return
                _LOGGER.warning("MQTT %s client disconnected, forcing reconnect", label)
                client.reconnect()
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.error("MQTT %s reconnect failed: %s", label, err)

        _check(Api.mqttCloud, "cloud")
        if Api.localServer:
            _check(Api.mqttLocal, "local")

        # Per-device cloud relay clients (created on-demand when a legacy
        # device sends via local MQTT, to mirror the message to the Zendure
        # cloud broker with the device's own credentials). These die just
        # as silently as the shared ones.
        for device in Api.devices.values():
            if device.zendure is not None:
                _check(device.zendure, f"zendure[{device.name}]")

        self._check_mqtt_staleness()

    def _check_mqtt_staleness(self) -> None:
        """Recover from a 'connected but no data' subscription drop.

        A broker can keep the TCP session alive (is_connected()==True, so the
        reconnect checks above never fire) while silently no longer delivering
        a device's topic. The result: the Zendure app still shows data (device
        keeps publishing) but HA receives nothing.

        Zendure devices report every ~1s (day) and at worst every ~60s
        (coordinator poll), so silence past MQTT_STALE_TIMEOUT (150s) is
        clearly abnormal. We recover the *real* last-message time from
        lastseen (which is stamped at message_time + MQTT_LASTSEEN_OFFSET) so
        detection is not blinded by that 5-min future offset, and we cache it
        because power_get() resets lastseen to datetime.min on expiry.

        Two graduated actions, throttled per device by MQTT_RESUB_COOLDOWN:
        - re-subscribe the silent device's topics (light, idempotent); covers
          a per-device subscription drop without disturbing healthy devices;
        - if the WHOLE client is silent (no device fresh) past
          MQTT_RECONNECT_TIMEOUT, the socket is likely half-open: force one
          reconnect of that client.
        """
        now = datetime.now()
        offset = timedelta(seconds=SmartMode.MQTT_LASTSEEN_OFFSET)

        # Compute silence per device from the real last-message time.
        # Only consider devices actually using MQTT (device.mqtt set). ZenSdk
        # devices in zenSDK mode poll over HTTP and keep device.mqtt is None;
        # re-subscribing/reconnecting MQTT could not help them and a cloud
        # reconnect would be pointless, so they are excluded here.
        silence: dict[str, float] = {}
        for device in self.devices:
            if device.mqtt is None:
                continue
            if device.lastseen != datetime.min:
                # lastseen == real message time + offset -> recover it exactly
                self._last_msg[device.deviceId] = device.lastseen - offset
            if (msg_time := self._last_msg.get(device.deviceId)) is not None:
                silence[device.deviceId] = (now - msg_time).total_seconds()

        if not silence:
            return

        # If any device is delivering, the client is healthy -> never reconnect
        # (an isolated silent device is then a device-side problem, not a socket
        # one), only re-subscribe it.
        any_fresh = any(s < SmartMode.MQTT_STALE_TIMEOUT for s in silence.values())
        reconnected: set[int] = set()

        for device in self.devices:
            if (silent := silence.get(device.deviceId)) is None or silent < SmartMode.MQTT_STALE_TIMEOUT:
                continue
            if (until := self._resubscribe_after.get(device.deviceId)) is not None and now < until:
                continue
            client = device.mqtt or Api.mqttCloud
            if client is None or not client.is_connected():
                continue  # disconnected clients are handled by the reconnect pass above

            try:
                if not any_fresh and silent >= SmartMode.MQTT_RECONNECT_TIMEOUT:
                    if id(client) not in reconnected:
                        _LOGGER.warning(
                            "MQTT delivering no data (device %s silent %ds); forcing reconnect",
                            device.name,
                            int(silent),
                        )
                        client.reconnect()
                        reconnected.add(id(client))
                else:
                    _LOGGER.warning(
                        "Device %s silent %ds while MQTT is connected; re-subscribing topics",
                        device.name,
                        int(silent),
                    )
                    Api.subscribeDevice(client, device)
                self._resubscribe_after[device.deviceId] = now + timedelta(seconds=SmartMode.MQTT_RESUB_COOLDOWN)
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.error("MQTT staleness recovery for %s failed: %s", device.name, err)

    def update_p1meter(self, p1meter: str | None) -> None:
        """Update the P1 meter sensor."""
        _LOGGER.debug("Updating P1 meter to: %s", p1meter)
        if self.p1meterEvent:
            self.p1meterEvent()
        if p1meter:
            self.p1meterEvent = async_track_state_change_event(self.hass, [p1meter], self._p1_changed)
            if (entity := self.hass.states.get(p1meter)) is not None and entity.attributes.get("unit_of_measurement", "W") in ("kW", "kilowatt", "kilowatts"):
                self.p1_factor = 1000
        else:
            self.p1meterEvent = None

    def configure_influx(self) -> None:
        """(Re)crée le writer InfluxDB v2 depuis les options du config entry."""
        self.influx = None
        data = self.config_entry.data if self.config_entry is not None else {}
        if not data.get(CONF_INFLUX_ENABLE) or not data.get(CONF_INFLUX_URL) or not data.get(CONF_INFLUX_TOKEN):
            return
        from .influx import ZendureInflux

        bucket = data.get(CONF_INFLUX_BUCKET) or "HA_ZENDURE"
        self.influx = ZendureInflux(self.hass, data[CONF_INFLUX_URL], data.get(CONF_INFLUX_ORG, ""), data[CONF_INFLUX_TOKEN], bucket)
        _LOGGER.info("Zendure InfluxDB export enabled -> bucket %s", bucket)

    async def _write_influx(self, now: datetime) -> None:
        """Snapshot de tous les paramètres manager + appareils dans le bucket dédié."""
        from .influx import line

        points: list[str | None] = [
            line(
                "zendure_manager",
                {},
                {
                    "p1": self.last_p1,
                    "setpoint": self.setpoint,
                    "hold_active": now < self.charge_hold_until,
                    "operation": self.operation.name,
                    "charge_floor": self.chargeFloor.asInt,
                    "charge_hold_window": self.chargeHoldWindow.asInt,
                    "power": self.power.asNumber,
                    "available_kwh": self.availableKwh.asNumber,
                    "total_kwh": self.totalKwh.asNumber,
                    "weighted_soc": self.weightedSoc.asNumber,
                    "usable_energy": self.usableEnergy.asNumber,
                    "discharge_bypass": self.discharge_bypass,
                    "charge_weight": self.charge_weight,
                    "discharge_weight": self.discharge_weight,
                    "produced": self.produced,
                },
            )
        ]
        for d in self.devices:
            points.append(
                line(
                    "zendure_device",
                    {"device": d.name},
                    {
                        "soc": d.electricLevel.asInt,
                        "kwh": d.kWh,
                        "available_kwh": d.availableKwh.asNumber,
                        "pwr_battery": d.batteryOutput.asInt - d.batteryInput.asInt,
                        "solar": d.solarInput.asInt,
                        "home_out": d.homeOutput.asInt,
                        "home_in": d.homeInput.asInt,
                        "pwr_produced": d.pwr_produced,
                        "pwr_offgrid": d.pwr_offgrid,
                        "pwr_max": d.pwr_max,
                        "charge_limit": d.charge_limit,
                        "discharge_limit": d.discharge_limit,
                        "min_soc": d.minSoc.asNumber,
                        "soc_set": d.socSet.asNumber,
                        "exports_bypass": d.exports_bypass,
                        "bypass": d.byPass.asInt,
                        "state": d.state.name,
                        "connection": d.connectionStatus.asInt,
                    },
                )
            )
        await self.influx.write([p for p in points if p])

    def writeSimulation(self, time: datetime, p1: int) -> None:
        if Path("simulation.csv").exists() is False:
            with Path("simulation.csv").open("w") as f:
                f.write(
                    "Time;P1;Operation;Battery;Solar;Home;SetPoint;Hold;--;"
                    + ";".join(
                        [
                            f"bat;Prod;Home;Soc;Conn;ChLim;{
                                json.dumps(
                                    DeviceSettings(
                                        d.name,
                                        d.fuseGrp.name,
                                        d.charge_limit,
                                        d.discharge_limit,
                                        d.maxSolar,
                                        d.kWh,
                                        d.socSet.asNumber,
                                        d.minSoc.asNumber,
                                    ),
                                    default=vars,
                                )
                            }"
                            for d in self.devices
                        ]
                    )
                    + "\n"
                )

        with Path("simulation.csv").open("a") as f:
            data = ""
            tbattery = 0
            tsolar = 0
            thome = 0

            for d in self.devices:
                tbattery += (pwr_battery := d.batteryOutput.asInt - d.batteryInput.asInt)
                tsolar += (pwr_solar := d.solarInput.asInt)
                thome += (pwr_home := d.homeOutput.asInt - d.homeInput.asInt)
                # Conn = connectionStatus (10=Cloud, 11=Local, 12=zenSDK, <10=pas connecté)
                # + charge_limit pour voir un clamp rapporté par l'appareil (ex. blocage à -250).
                data += f";{pwr_battery};{pwr_solar};{pwr_home};{d.electricLevel.asInt};{d.connectionStatus.asInt};{d.charge_limit}"

            hold = 1 if time < self.charge_hold_until else 0
            f.write(f"{time};{p1};{self.operation};{tbattery};{tsolar};{thome};{self.setpoint};{hold};" + data + "\n")

    async def _p1_changed(self, event: Event[EventStateChangedData]) -> None:
        # exit if there is nothing to do
        if not self.hass.is_running or (new_state := event.data["new_state"]) is None:
            return

        try:  # convert the state to a float
            p1 = int(self.p1_factor * float(new_state.state))
        except ValueError:
            return

        # Get time & update simulation
        time = datetime.now()
        if ZendureManager.simulation:
            self.writeSimulation(time, p1)

        # Check for fast delay
        if time < self.zero_fast:
            self.p1_history.append(p1)
            return

        # calculate the standard deviation
        if len(self.p1_history) > 1:
            avg = int(sum(self.p1_history) / len(self.p1_history))
            stddev = SmartMode.P1_STDDEV_FACTOR * max(SmartMode.P1_STDDEV_MIN, sqrt(sum([pow(i - avg, 2) for i in self.p1_history]) / len(self.p1_history)))
            if isFast := abs(p1 - avg) > stddev or abs(p1 - self.p1_history[0]) > stddev:
                self.p1_history.clear()
        else:
            isFast = False
        self.p1_history.append(p1)

        # check minimal time between updates
        if isFast or time > self.zero_next:
            try:
                # prevent updates during power distribution changes
                self.zero_fast = datetime.max
                self.charge.clear()
                self.charge_limit = 0
                self.charge_optimal = 0
                self.charge_weight = 0
                self.discharge.clear()
                self.discharge_bypass = 0
                self.discharge_limit = 0
                self.discharge_optimal = 0
                self.discharge_produced = 0
                self.discharge_weight = 0
                self.idle.clear()
                self.idle_lvlmax = 0
                self.idle_lvlmin = 100
                self.produced = 0
                for fg in self.fuseGroups:
                    fg.initPower = True
                await self.powerChanged(p1, isFast, time)
            except Exception as err:
                _LOGGER.error(err)
                _LOGGER.error(traceback.format_exc())

            time = datetime.now()
            self.zero_next = time + timedelta(seconds=SmartMode.TIMEZERO)
            self.zero_fast = time + timedelta(seconds=SmartMode.TIMEFAST)

    async def powerChanged(self, p1: int, isFast: bool, time: datetime) -> None:
        """Return the distribution setpoint."""
        availableKwh = 0
        setpoint = p1
        power = 0

        for d in self.devices:
            if await d.power_get():
                # get power production
                d.pwr_produced = min(0, d.batteryOutput.asInt + d.homeInput.asInt - d.batteryInput.asInt - d.homeOutput.asInt)
                self.produced -= d.pwr_produced

                # only positive pwr_offgrid must be taken into account, negative values count a solarInput
                if (home := -d.homeInput.asInt + max(0, d.pwr_offgrid)) < 0:
                    self.charge.append(d)
                    self.charge_limit += d.fuseGrp.charge_limit(d)
                    self.charge_optimal += d.charge_optimal
                    self.charge_weight += d.pwr_max * (100 - d.electricLevel.asInt)
                    # Credit only the portion of homeInput that reaches the battery; AC
                    # drawn but not stored is real demand on the home bus, not surplus.
                    setpoint -= min(d.homeInput.asInt, d.batteryInput.asInt)
                # SOCEMPTY means, it could not discharge the battery, but it is still possible to feed into the home using solarpower or offGrid
                elif (home := d.homeOutput.asInt) > 0:
                    self.discharge.append(d)
                    self.discharge_bypass -= d.pwr_produced if d.state == DeviceState.SOCFULL and d.exports_bypass else 0
                    self.discharge_limit += d.fuseGrp.discharge_limit(d)
                    self.discharge_optimal += d.discharge_optimal
                    self.discharge_produced -= d.pwr_produced
                    self.discharge_weight += d.pwr_max * d.electricLevel.asInt
                    setpoint += home

                else:
                    self.idle.append(d)
                    self.idle_lvlmax = max(self.idle_lvlmax, d.electricLevel.asInt)
                    self.idle_lvlmin = min(self.idle_lvlmin, d.electricLevel.asInt if d.state != DeviceState.SOCFULL else 100)

                availableKwh += d.actualKwh
                power += d.pwr_offgrid + home + d.pwr_produced

        # Update the power entities
        self.power.update_value(power)
        self.availableKwh.update_value(availableKwh)

        # discharge_bypass accumulates the solar-only power produced by SOCFULL devices.
        # Subtract it from setpoint to avoid over-discharging from grid, but clamp so
        # setpoint never goes below 0 when p1 >= 0: a SOCFULL device producing solar
        # should still cover home demand, not trigger charge mode (fixes #1151 output
        # cycling to 0W with bypass forbidden + 100% SoC).
        # Ne retrancher le crédit bypass (solaire d'un device SOCFULL) QUE lorsqu'on
        # EXPORTE (p1 < 0). En import réseau réel (p1 >= 0), le retrancher masquait la
        # demande et empêchait les AUTRES batteries de décharger (bug 2e Hyper idle
        # alors que la maison importait). discharge_produced est déjà soustrait dans
        # dev_start de power_discharge : le retrancher ici aussi = double comptage.
        if self.discharge_bypass > 0 and p1 < 0:
            setpoint -= self.discharge_bypass

        # Update power distribution.
        _LOGGER.info("P1 ======> p1:%s isFast:%s, setpoint:%sW stored:%sW", p1, isFast, setpoint, self.produced)

        # Télémétrie : mémorise P1 et le setpoint réel calculé (pour InfluxDB + simulation.csv).
        self.last_p1 = p1
        self.setpoint = setpoint
        self.setpointSensor.update_value(setpoint)

        match self.operation:
            case ManagerMode.MATCHING:
                if setpoint < 0:
                    # Surplus → charge. (Ré)arme la fenêtre de maintien : un creux
                    # bref (nuage qui passe) ne coupera pas la charge tout de suite.
                    self.charge_hold_until = time + timedelta(seconds=self.chargeHoldWindow.asInt)
                    await self.power_charge(setpoint, time)
                elif (floor := self.chargeFloor.asInt) > 0 and time < self.charge_hold_until:
                    # Creux bref juste après une charge : on maintient un plancher pour
                    # garder l'appareil « chaud » (rampe immédiate au retour du soleil)
                    # et NE PAS appeler power_discharge — ce qui évite le cooldown ~60 s
                    # qui bloquerait la reprise et provoquerait de l'export réseau.
                    # power_charge garde charge_time intact → reprise instantanée.
                    await self.power_charge(-floor, time)
                else:
                    # Pas de charge récente, ou fenêtre expirée → comportement normal.
                    await self.power_discharge(setpoint)

            case ManagerMode.MATCHING_DISCHARGE:
                # Only discharge, do nothing if setpoint is negative
                await self.power_discharge(max(0, setpoint))

            case ManagerMode.MATCHING_CHARGE | ManagerMode.STORE_SOLAR:
                # Allow discharge of produced power in MATCHING_CHARGE-Mode, otherwise only charge
                # d.pwr_produced is negative, but self.produced is positive
                if setpoint > 0 and self.produced > SmartMode.POWER_START and self.operation == ManagerMode.MATCHING_CHARGE:
                    await self.power_discharge(min(self.produced, setpoint))
                # Creux bref pendant le stockage solaire : on maintient un plancher au lieu
                # de couper (power_discharge(0)), même logique anti cloud-flicker que MATCHING,
                # étendue ici car c'est en STORE_SOLAR/MATCHING_CHARGE que la charge cyclait.
                elif setpoint > 0 and (floor := self.chargeFloor.asInt) > 0 and time < self.charge_hold_until:
                    await self.power_charge(-floor, time)
                # send device into idle-mode
                elif setpoint > 0:
                    await self.power_discharge(0)
                else:
                    # Charge sur surplus → (ré)arme la fenêtre de maintien.
                    self.charge_hold_until = time + timedelta(seconds=self.chargeHoldWindow.asInt)
                    await self.power_charge(min(0, setpoint), time)

            case ManagerMode.MANUAL:
                # Manual power into or from home
                if (setpoint := int(self.manualpower.asNumber)) > 0:
                    await self.power_discharge(setpoint)
                else:
                    await self.power_charge(setpoint, time)

            case ManagerMode.MONITOR:
                # Passthrough: display MQTT data from Zendure as-is, send no commands
                self.operationstate.update_value(ManagerState.IDLE.value)

            case ManagerMode.OFF:
                self.operationstate.update_value(ManagerState.OFF.value)

    async def power_charge(self, setpoint: int, time: datetime) -> None:
        """Charge devices."""
        _LOGGER.info("Charge => setpoint %sW", setpoint)

        # stop discharging devices
        for d in self.discharge:
            # avoid stopping bypassing devices
            if d.byPass.asInt > 0:
                continue
            # avoid gridOff device to use power from the grid
            await d.power_discharge(0 if d.pwr_offgrid == 0 else -10)

        # prevent hysteria
        if self.charge_time > time:
            if self.charge_time == datetime.max:
                self.charge_time = time + timedelta(seconds=2 if (time - self.charge_last).total_seconds() > 300 else 60)
                self.charge_last = self.charge_time
                self.pwr_low = 0
            setpoint = 0
        self.operationstate.update_value(ManagerState.CHARGE.value if setpoint < 0 else ManagerState.IDLE.value)

        # distribute charging devices
        dev_start = min(0, setpoint - self.charge_optimal * 2) if setpoint < -SmartMode.POWER_START else 0
        limit = self.charge_limit
        setpoint = max(limit, setpoint)
        for i, d in enumerate(sorted(self.charge, key=lambda d: d.electricLevel.asInt, reverse=True)):
            # Weight per device: pwr_max * remaining capacity (100 - SOC%).
            # Devices with lower SOC get a larger share of the charge power.
            # Guard against division by zero: charge_weight can be 0 when all
            # remaining devices are at 100% SOC (nothing left to charge) or when
            # it drops to 0 mid-iteration after subtracting previous devices.
            device_weight = d.pwr_max * (100 - d.electricLevel.asInt)
            if self.charge_weight != 0:
                pwr = int(setpoint * device_weight / self.charge_weight)
            else:
                # all remaining devices at 100% SOC — skip charging
                pwr = 0
            self.charge_weight -= device_weight

            # adjust the limit, make sure we have 'enough' power to charge
            limit -= d.pwr_max
            pwr = max(pwr, setpoint, d.pwr_max)
            if limit > setpoint - pwr:
                pwr = max(setpoint - limit, setpoint, d.pwr_max)

            # make sure we have devices in optimal working range
            if len(self.charge) > 1 and i == 0:
                self.pwr_low = 0 if (delta := d.charge_start * 1.5 - pwr) >= 0 else self.pwr_low + int(-delta)
                pwr = 0 if self.pwr_low < d.charge_optimal else pwr

            setpoint -= await d.power_charge(pwr)
            dev_start += -1 if pwr != 0 and d.electricLevel.asInt > self.idle_lvlmin + 3 else 0

        # start idle device if needed
        if dev_start < 0 and len(self.idle) > 0:
            self.idle.sort(key=lambda d: d.electricLevel.asInt, reverse=False)
            for d in self.idle:
                # offGrid device need to be started with at least their offgrid power, otherwise they will not be recognized as charging
                # but should not be started with more than pwr_offgrid if they are full
                # if a offGrid device need to be started, the output power is set to 0 and it take all offGrid power from grid
                start_pwr = SmartMode.POWER_START
                await d.power_charge(-start_pwr - max(0, d.pwr_offgrid) if d.state != DeviceState.SOCFULL else -max(0, d.pwr_offgrid))
                if (dev_start := dev_start - d.charge_optimal * 2) >= 0:
                    break
            self.pwr_low: int = 0

    async def power_discharge(self, setpoint: int) -> None:
        """Discharge devices."""
        _LOGGER.info("Discharge => setpoint %sW", setpoint)
        self.operationstate.update_value(ManagerState.DISCHARGE.value if setpoint > 0 and self.discharge else ManagerState.IDLE.value)

        # reset hysteria time
        if self.charge_time != datetime.max:
            self.charge_time = datetime.max
            self.pwr_low = 0

        # stop charging devices
        for d in self.charge:
            # SF 2400 may show more gridInputPower than offGridPower and will be recognized as charging, so set power to 10 instead of 0
            await d.power_discharge(0 if max(0, d.pwr_offgrid) == 0 else 10)

        # distribute discharging devices, use produced power first, before adding another device
        dev_start = max(0, setpoint - self.discharge_optimal * 2 - self.discharge_produced) if setpoint > SmartMode.POWER_START else 0
        solaronly = self.discharge_produced >= setpoint
        limit = self.discharge_produced if solaronly else self.discharge_limit
        setpoint = min(limit, setpoint)
        for i, d in enumerate(sorted(self.discharge, key=lambda d: d.electricLevel.asInt, reverse=False)):
            # Weight per device: pwr_max * SOC%. Devices with higher SOC get a
            # larger share of the discharge power.
            # Guard against division by zero: discharge_weight can be 0 when all
            # remaining devices are at 0% SOC, or when it drops to 0 mid-iteration.
            # In that case, distribute the remaining setpoint evenly across the
            # remaining devices so they can still pass through solar production.
            device_weight = d.pwr_max * d.electricLevel.asInt
            if self.discharge_weight != 0:
                pwr = int(setpoint * device_weight / self.discharge_weight)
            elif len(self.discharge) > i:
                pwr = int(setpoint / (len(self.discharge) - i))
            else:
                pwr = 0
            # SOCFULL devices should only pass through solar, not drain battery
            if pwr < -d.pwr_produced and d.state == DeviceState.SOCFULL:
                pwr = -d.pwr_produced
            self.discharge_weight -= device_weight

            # adjust the limit, make sure we have 'enough' power to discharge
            limit -= -d.pwr_produced if solaronly else d.pwr_max
            if limit < setpoint - pwr:
                pwr = max(setpoint - limit, 0 if d.state != DeviceState.SOCFULL else -d.pwr_produced)
            pwr = min(pwr, setpoint, d.pwr_max)

            # make sure we have devices in optimal working range
            if len(self.discharge) > 1 and i == 0 and d.state != DeviceState.SOCFULL:
                self.pwr_low = 0 if (delta := d.discharge_start * 1.5 - pwr) <= 0 else self.pwr_low + int(delta)
                pwr = 0 if self.pwr_low > d.discharge_optimal else pwr

            setpoint -= await d.power_discharge(pwr)
            dev_start += 1 if pwr != 0 and d.electricLevel.asInt + 3 < self.idle_lvlmax else 0

        # start idle device if needed
        if dev_start > 0 and len(self.idle) > 0:
            self.idle.sort(key=lambda d: d.electricLevel.asInt, reverse=True)
            for d in self.idle:
                if d.state != DeviceState.SOCEMPTY:
                    await d.power_discharge(SmartMode.POWER_START)
                    if (dev_start := dev_start - d.discharge_optimal * 2) <= 0:
                        break
            self.pwr_low: int = 0
