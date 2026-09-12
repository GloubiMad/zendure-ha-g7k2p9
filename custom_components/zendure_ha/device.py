"""Zendure Integration device."""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from aiohttp import ClientTimeout
from bleak import BleakClient
from bleak.exc import BleakError

try:
    from bleak_retry_connector import establish_connection
except ImportError:
    establish_connection = None

from homeassistant.components import bluetooth, persistent_notification
from homeassistant.components.number import NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from paho.mqtt import client as mqtt_client

from .binary_sensor import ZendureBinarySensor
from .button import ZendureButton
from .const import DeviceState, SmartMode
from .entity import EntityDevice, EntityZendure
from .number import ZendureNumber
from .select import ZendureRestoreSelect, ZendureSelect
from .sensor import ZendureRestoreSensor, ZendureSensor

_LOGGER = logging.getLogger(__name__)

CONST_HEADER = {"content-type": "application/json; charset=UTF-8"}
# DÉLAI D'ATTENTE HTTP — 4 s à l'amont, ramené à 1 s le 28/07/2026 sur mesure.
#
# ⚠️ Ces deux appels (`httpGet`/`httpPost`) ne concernent QUE le SolarFlow (`ZendureZenSdk`), mais
# ils sont AWAIT dans le cycle commun : tant qu'ils n'ont pas rendu la main, le moteur ne commande
# RIEN — y compris les Hyper, qui sont pourtant joignables en MQTT et accusent réception en 184 ms.
# Un seul appareil injoignable gelait donc la régulation des deux autres.
#
# Mesuré sur 21 h de trace (champ `ms=` de simulation.csv) :
#   - fonctionnement normal : calcul moteur 0,2 ms, cycle complet (httpGet compris) médiane 19 ms,
#     p99 118 ms ;
#   - 19 épisodes de non-réponse du SolarFlow, 159 s cumulées (0,21 % du temps), les plus longs
#     durant 22 à 31 s ;
#   - pendant ceux-ci, valeurs EXACTEMENT 4001 ms (httpPost seul) et 8003 ms (httpGet + httpPost),
#     signature d'un délai d'attente, pas d'une surcharge.
#
# 1 s laisse ~8× de marge sur le pire cas normal (p99 = 118 ms) et ramène le gel maximal de 8 s à
# 2 s. On ne descend pas plus bas : une requête HTTP sur un réseau chargé peut légitimement prendre
# 300-400 ms, et un délai trop court fabriquerait des échecs là où il n'y en avait pas.
#
# ⚠️ Effet de bord à connaître : sur exception, `httpGet`/`httpPost` posent `lastseen = datetime.min`,
# ce qui fait passer le champ `Age` du CSV à −1 (« jamais vu »). Ce −1 est donc la CONSÉQUENCE du
# délai dépassé, jamais la preuve que l'appareil a décroché.
# ⚠️ ÉCART ASSUMÉ AVEC L'AMONT : la 1.4.4 est passée de 4 s à 2 s pour la raison exacte
# décrite ci-dessus (« An unreachable device stalls that loop for the full timeout »).
# On reste à 1 s : mesuré ICI, p99 = 118 ms, donc 8× de marge — et le 18/08 un appareil
# injoignable a gelé le cycle à 1002 ms pendant 19 h ; à 2 s ç'aurait été 2002 ms.
CONST_TIMEOUT = ClientTimeout(total=1)
# Échecs consécutifs avant d'essayer l'autre adresse (cf. `_http_ko`). 3 = ~3 s de détection,
# assez pour ne pas basculer sur un incident réseau passager, assez peu pour ne rien coûter.
HTTP_FAILS_SWAP = 3
SF_COMMAND_CHAR = "0000c304-0000-1000-8000-00805f9b34fb"


class ZendureBattery(EntityDevice):
    """Zendure Battery class for devices."""

    @staticmethod
    def get_battery_type(sn: str, pack_type: int | None = None) -> tuple[str, str, float]:
        model = "???"
        match sn[0]:
            case "A":
                if sn[3] == "3":
                    model = "AIO2400"
                    kWh = 2.4
                else:
                    model = "AB1000"
                    kWh = 0.96
            case "B":
                # packType 70 is the SF4000 Mix AC+'s internal 8 kWh pack, which shares
                # its serial prefix with the unrelated 0.96 kWh AB1000S.
                if pack_type == 70:
                    model = "I8000"
                    kWh = 8.0
                else:
                    model = "AB1000S"
                    kWh = 0.96
            case "C":
                # External AB2000X and internal AB2000X of SF800+/SF800Pro/SF1600AC+ starting with CO4A. They are also described as additional battery in the Zendure App, even when they are integrated into the device.
                model = "AB2000" + ("S" if sn[3] == "F" else "X" if sn[3] == "E" else "")
                kWh = 1.92
            case "F":
                model = "AB3000"
                kWh = 2.88
            case "G":
                model = "AB3000L"
                kWh = 2.88
            case "J":
                # JO2A => internal battery of SF2400AC pro
                # JO4A => internal battery of SF2400AC+
                model = "I2400"
                kWh = 2.4
            case _:
                model = "Unknown"
                kWh = 0.0

        name = f"{model} {sn[-5:]}".strip()
        return name, model, kWh

    def __init__(self, hass: HomeAssistant, sn: str, parent: EntityDevice, pack_type: int | None = None) -> None:
        """Initialize Device."""
        name, model, self.kWh = ZendureBattery.get_battery_type(sn, pack_type)
        super().__init__(hass, sn, name, model, "", sn, parent.sn)
        self.attr_device_info["serial_number"] = sn
        self.deltaVoltage = ZendureSensor(self, "deltaVoltage", None, "V", "voltage", "measurement", 3)

    def entityUpdate(self, key: Any, value: Any) -> bool:
        """Update entity state and recalculate deltaVoltage when maxVol or minVol changes."""
        changed = super().entityUpdate(key, value)
        if changed and key in {"maxVol", "minVol"}:
            max_vol = self.entities.get("maxVol")
            min_vol = self.entities.get("minVol")
            if max_vol is not None and min_vol is not None:
                max_val = max_vol.asNumber
                min_val = min_vol.asNumber
                if max_val != 0 and min_val != 0:
                    self.deltaVoltage.update_value(round(max_val - min_val, 3))
        return changed


class ZendureDevice(EntityDevice):
    """Zendure Device class for devices integration."""

    def __init__(self, hass: HomeAssistant, deviceId: str, name: str, model: str, definition: dict[str, str], parent: str | None = None) -> None:
        """Initialize Device."""
        from .fusegroup import FuseGroup

        """Initialize Device."""
        self.prodkey = definition["productKey"]
        super().__init__(hass, deviceId, name, model, self.prodkey, definition["snNumber"], parent)
        self.snNumber = definition["snNumber"]
        self.definition = definition
        self.fuseGrp: FuseGroup

        self.mqtt: mqtt_client.Client | None = None
        # 1.4.4.8 : envois MQTT perdus. `publish_broken` evite de journaliser a chaque cycle.
        self.publish_failed: int = 0
        self.publish_broken: bool = False
        self.zendure: mqtt_client.Client | None = None
        self.ipAddress = definition.get("ip", "") if definition.get("ip", "") != "" else f"zendure-{definition['productModel'].replace(' ', '')}-{self.snNumber}.local"
        # ⛔ 1.4.3.53 — L'ADRESSE PEUT CHANGER, ET RIEN NE LE REMARQUAIT.
        # `ip` vient de l'API cloud et n'est relue qu'au chargement de l'intégration. Le 17/08/2026
        # le SolarFlow s'est reconnecté au WiFi avec une nouvelle adresse (.221 -> .230) : le cloud
        # a continué d'annoncer l'ancienne, et TOUT s'est arrêté pendant 25 heures.
        #   - chaque `httpGet` partait dans le vide -> `lastseen = datetime.min` -> OFFLINE
        #     -> `fondation.py` retirait l'appareil de sa liste : 28 % de batterie inutilisables ;
        #   - pire, ces appels sont AWAIT dans le cycle COMMUN (cf. le bloc CONST_TIMEOUT) : le
        #     cycle est passé de 22-68 ms à 1002 ms, soit 60 cycles/minute au lieu de 882 à 2727.
        #     Comme `slew` et `step` sont PAR CYCLE, c'est toute la dynamique de régulation des
        #     DEUX Hyper — parfaitement joignables, eux — qui s'est effondrée.
        # Le nom mDNS était déjà construit ici, mais seulement comme DÉFAUT quand le cloud ne donne
        # aucune IP : jamais comme SECOURS. Vérifié depuis HA le 18/08, il répond (`ping` et
        # `properties/report` complet) — que la résolution vienne d'Avahi ou d'Unbound/OPNsense,
        # qui enregistre les baux DHCP, elle suit l'appareil.
        self.hostName = f"zendure-{definition['productModel'].replace(' ', '')}-{self.snNumber}.local"
        # ⚠️ ON ALTERNE, ON NE CUMULE PAS. Essayer les deux adresses à chaque appel doublerait le
        # gel du cycle (1 s -> 2 s) précisément quand l'appareil est déjà en difficulté. On bascule
        # donc d'un candidat à l'autre après HTTP_FAILS_SWAP échecs : le coût par cycle reste d'un
        # seul délai d'attente, et on retrouve l'appareil dès que l'un des deux chemins redevient
        # valide. Sur le cas du 17/08 : récupération en 3 cycles au lieu de 25 heures.
        self._hosts = [self.ipAddress] + ([self.hostName] if self.hostName != self.ipAddress else [])
        self._host_idx = 0
        self._http_fails = 0

        self.topic_read = f"iot/{self.prodkey}/{self.deviceId}/properties/read"
        self.topic_write = f"iot/{self.prodkey}/{self.deviceId}/properties/write"
        self.topic_function = f"iot/{self.prodkey}/{self.deviceId}/function/invoke"

        self.batteries: dict[str, ZendureBattery | None] = {}
        # DEUX FRAÎCHEURS DISTINCTES (29/07) — elles ne répondent pas à la même question :
        #   `lastseen`   : « la LIAISON répond » — posé par `properties/report` ET par les accusés
        #                  (`function/invoke/reply`, `properties/read/reply`). Décide si le device
        #                  est OFFLINE, donc s'il reste dans la liste du moteur.
        #   `lastreport` : « l'ÉTAT est rapporté » — posé UNIQUEMENT par `properties/report`.
        #                  C'est ce que doit surveiller le watchdog.
        # Les confondre laisserait passer le mode de panne du bug TLS : un appareil qui répond
        # encore mais ne publie plus son état paraîtrait sain. Même convention pour les deux :
        # la valeur stockée est « instant du dernier message + 5 min ».
        self.lastseen = datetime.min
        self.lastreport = datetime.min
        self._messageid = 0
        self.kWh = 0.0

        self.charge_limit: int = 0
        self.charge_optimal: int = 0
        self.charge_start: int = 0
        self.discharge_limit: int = 0
        self.discharge_optimal: int = 0
        self.discharge_start: int = 0
        self.maxSolar = 0
        self.pwr_max: int = 0
        self.pwr_produced: int = 0
        self.actualKwh: float = 0.0
        self.state: DeviceState = DeviceState.OFFLINE
        self.exports_bypass: bool = True

        self.create_entities()

    def create_entities(self) -> None:
        """Create the device entities."""
        self.limitOutput = ZendureNumber(self, "outputLimit", self.entityWrite, None, "W", "power", self.discharge_limit, 0, NumberMode.SLIDER)
        self.limitInput = ZendureNumber(self, "inputLimit", self.entityWrite, None, "W", "power", self.charge_limit, 0, NumberMode.SLIDER)
        self.minSoc = ZendureNumber(self, "minSoc", self.entityWrite, None, "%", "soc", 100, 0, NumberMode.SLIDER, 10)
        self.socSet = ZendureNumber(self, "socSet", self.entityWrite, None, "%", "soc", 100, 0, NumberMode.SLIDER, 10)
        self.socStatus = ZendureSensor(self, "socStatus", state=0)
        self.socLimit = ZendureSensor(self, "socLimit", state=0)
        self.byPass = ZendureSensor(self, "pass", state=0)

        fuseGroups = {
            0: "unused",
            1: "owncircuit",
            2: "group800",
            3: "group800_2400",
            4: "group1200",
            5: "group2000",
            6: "group2400",
            7: "group3600",
            8: "group4000",
            9: "group5000",
        }
        self.fuseGroup = ZendureRestoreSelect(self, "fuseGroup", fuseGroups, None)
        self.acMode = ZendureSelect(self, "acMode", {1: "input", 2: "output"}, self.entityWrite, 1)
        self.electricLevel = ZendureSensor(self, "electricLevel", None, "%", "battery", "measurement")
        # set homeInput to 0 for devices, which have no AC charge capability
        self.homeInput = ZendureSensor(self, "gridInputPower", None, "W", "power", "measurement", state = 0)
        self.solarInput = ZendureSensor(self, "solarInputPower", None, "W", "power", "measurement", icon="mdi:solar-panel")
        self.batteryInput = ZendureSensor(self, "outputPackPower", None, "W", "power", "measurement")
        self.batteryOutput = ZendureSensor(self, "packInputPower", None, "W", "power", "measurement")
        self.homeOutput = ZendureSensor(self, "outputHomePower", None, "W", "power", "measurement")
        self.batInOut = ZendureSensor(self, "batInOut", None, "W", "power", "measurement", 0)
        self.heatState = ZendureBinarySensor(self, "heatState")
        self.hemsState = ZendureBinarySensor(self, "hemsState")
        self.hemsStateUpdated = datetime.min
        self.availableKwh = ZendureSensor(self, "available_kwh", None, "kWh", "energy_storage", None, 1)
        self.totalKwh = ZendureSensor(self, "total_kwh", None, "kWh", "energy_storage", "measurement", 2)
        self.connectionStatus = ZendureSensor(self, "connectionStatus")
        self.connection: ZendureRestoreSelect
        self.bleAdapter: ZendureRestoreSelect | None = None
        self.remainingTime = ZendureSensor(self, "remainingTime", None, "h", "duration", "measurement")
        self.nextCalibration = ZendureRestoreSensor(self, "nextCalibration", None, None, "timestamp", None)

        self.aggrCharge = ZendureRestoreSensor(self, "aggrCharge", None, "kWh", "energy", "total_increasing", 2)
        self.aggrDischarge = ZendureRestoreSensor(self, "aggrDischarge", None, "kWh", "energy", "total_increasing", 2)
        # Round-trip efficiency: ratio of total energy discharged to total energy charged, expressed as a percentage
        self.roundtripEfficiency = ZendureSensor(self, "roundtripEfficiency", None, "%", None, "measurement", 1)
        self.aggrHomeInput = ZendureRestoreSensor(self, "aggrGridInputPower", None, "kWh", "energy", "total_increasing", 2)
        self.aggrHomeOut = ZendureRestoreSensor(self, "aggrOutputHome", None, "kWh", "energy", "total_increasing", 2)
        self.aggrSolar = ZendureRestoreSensor(self, "aggrSolar", None, "kWh", "energy", "total_increasing", 2)
        self.aggrSwitchCount = ZendureRestoreSensor(self, "switchCount", None, None, None, "total_increasing", 0)

    def setLimits(self, charge: int, discharge: int) -> None:
        """Set the device limits.

        ⭐ `discharge_nominal` = la PLAQUE DU MODÈLE, mémorisée au tout PREMIER appel — celui du
        constructeur du device (`SolarFlow2400Pro` : `setLimits(-3200, 2400)`). Tous les appels
        suivants viennent de l'APPAREIL (`inverseMaxPower`, `chargeMaxLimit`), donc du cloud, et
        écrasent `discharge_limit` : le SolarFlow rapporte `inverseMaxPower = 800` et la limite
        tombe de 2400 à 800 en quelques secondes.
        ⛔ Sans cette copie, le bouton de déblocage n'aurait aucune référence : lire
        `discharge_limit` au moment du clic renverrait **800**, c'est-à-dire la valeur du bridage
        qu'on cherche justement à lever. C'est le même piège que le cliquet de `chg_accept`
        (cf. `.48`) — prendre pour une limite du matériel une limite qu'on lui a imposée.
        """
        try:
            if getattr(self, "discharge_nominal", 0) <= 0:
                self.discharge_nominal = discharge
            self.charge_limit = charge
            self.charge_optimal = charge // 4
            self.charge_start = charge // 10
            self.limitInput.update_range(0, abs(charge))

            self.discharge_limit = discharge
            self.discharge_optimal = discharge // 4
            self.discharge_start = discharge // 10
            self.limitOutput.update_range(0, discharge)
        except Exception:
            _LOGGER.error("SetLimits error %s %s %s!", self.name, charge, discharge)

    def setStatus(self) -> None:
        from .api import Api

        try:
            if self.lastseen == datetime.min:
                self.connectionStatus.update_value(0)
            elif self.socStatus.asInt == 1:
                self.connectionStatus.update_value(1)
            elif self.hemsState.is_on:
                self.connectionStatus.update_value(2)
            elif self.fuseGroup.value == 0:
                self.connectionStatus.update_value(3)
            elif self.connection.value == SmartMode.ZENSDK:
                self.connectionStatus.update_value(12)
            elif self.mqtt is not None and self.mqtt.host == Api.localServer:
                self.connectionStatus.update_value(11)
            else:
                self.connectionStatus.update_value(10)
        except Exception:
            self.connectionStatus.update_value(0)

    def entityUpdate(self, key: Any, value: Any) -> bool:
        # update entity state
        if key in {"remainOutTime", "remainInputTime"}:
            self.remainingTime.update_value(self.calcRemainingTime())
            return True

        changed = super().entityUpdate(key, value)
        try:
            if changed:
                match key:
                    case "packState":
                        if value == 0:
                            self.aggrSwitchCount.update_value(1 + self.aggrSwitchCount.asNumber)
                    case "outputPackPower":
                        if not self.heatState.is_on:
                            self.aggrCharge.aggregate(dt_util.now(), value)
                        self.aggrDischarge.aggregate(dt_util.now(), 0)
                        self.batInOut.update_value(self.batteryOutput.asInt - self.batteryInput.asInt)
                        self.roundtripEfficiency.update_value(round(self.aggrDischarge.asNumber / charge * 100, 1) if (charge := self.aggrCharge.asNumber) > 0 else 0)
                    case "packInputPower":
                        self.aggrCharge.aggregate(dt_util.now(), 0)
                        self.aggrDischarge.aggregate(dt_util.now(), value)
                        self.batInOut.update_value(self.batteryOutput.asInt - self.batteryInput.asInt)
                        self.roundtripEfficiency.update_value(round(self.aggrDischarge.asNumber / charge * 100, 1) if (charge := self.aggrCharge.asNumber) > 0 else 0)
                    case "solarInputPower":
                        self.aggrSolar.aggregate(dt_util.now(), value)
                    case "gridInputPower":
                        self.aggrHomeInput.aggregate(dt_util.now(), value)
                    case "outputHomePower":
                        self.aggrHomeOut.aggregate(dt_util.now(), value)
                    case "gridOffPower":
                        self.aggrOffGrid.aggregate(dt_util.now(), value)
                    case "inverseMaxPower":
                        self.setLimits(self.charge_limit, value)
                    case "chargeLimit" | "chargeMaxLimit":
                        self.setLimits(-value, self.discharge_limit)
                    case "hemsState" | "socStatus":
                        self.setStatus()
                        if key == "socStatus" and self.socStatus.asInt == 0:
                            self.nextCalibration.update_value(dt_util.now() + timedelta(days=30))
                    case "electricLevel" | "minSoc" | "socLimit":
                        if self.electricLevel.asInt == 100:
                            self.nextCalibration.update_value(dt_util.now() + timedelta(days=30))
                        self.availableKwh.update_value((self.electricLevel.asNumber - self.minSoc.asNumber) / 100 * self.kWh)
                    case "gridReverse":
                        self.exports_bypass = value == 1
        except Exception as e:
            _LOGGER.error("EntityUpdate error %s %s %s!", self.name, key, e)
            _LOGGER.error(traceback.format_exc())

        return changed

    def calcRemainingTime(self) -> float:
        """Calculate the remaining time."""
        level = self.electricLevel.asInt
        power = self.batteryOutput.asInt - self.batteryInput.asInt

        if power == 0:
            return 0

        if power < 0:
            soc = self.socSet.asNumber
            return 0 if level >= soc else min(999, self.kWh * 10 / -power * (soc - level))

        soc = self.minSoc.asNumber
        return 0 if level <= soc else min(999, self.kWh * 10 / power * (level - soc))

    async def entityWrite(self, entity: EntityZendure, value: Any) -> None:
        if entity.translation_key is None:
            _LOGGER.error("Entity %s has no translation_key, cannot write property %s", entity.name, self.name)
            return

        _LOGGER.info("Writing property %s %s => %s", self.name, entity.propertyName, value)
        self._messageid += 1
        payload = json.dumps(
            {
                "deviceId": self.deviceId,
                "messageId": self._messageid,
                "timestamp": int(datetime.now().timestamp()),
                "properties": {entity.propertyName: value},
            },
            default=lambda o: o.__dict__,
        )
        if self.mqtt is not None:
            self.mqtt.publish(self.topic_write, payload)

    async def button_press(self, _key: str) -> None:
        return

    def mqttPublish(self, topic: str, command: Any, client: mqtt_client.Client | None = None) -> bool:
        """Publie une commande, et DIT si elle n'est pas partie.

        ⛔ 1.4.4.8 — AVANT, LE CODE RETOUR ÉTAIT JETÉ. Le corps tenait en quatre lignes :
        `client.publish(...)` sans lire ce que paho renvoyait, et — pire — si les DEUX clients
        étaient `None`, la fonction ne publiait rien et n'en disait pas un mot. Un ordre pouvait
        donc être perdu en silence, et tout le reste du code continuait comme s'il était passé.

        Ce n'est pas théorique. Le 09/09/2026 à 21:36, le moteur passe en `off` et appelle
        `power_off()` sur les trois appareils. `up` était muet depuis 545 s : son message est parti
        dans le vide, personne ne l'a su, et il a continué à décharger 855 W sur sa dernière
        consigne. Mesuré sur 127 h, `up` est injoignable 1,9 % du temps (23 épisodes, jusqu'à
        28 min d'affilée) — autant d'occasions de perdre un ordre sans trace.

        `paho.publish()` renvoie un `MQTTMessageInfo` dont `rc` vaut `MQTT_ERR_SUCCESS` (0) quand le
        message est accepté, et `MQTT_ERR_NO_CONN` (4) quand le client n'est pas connecté. Le lire
        coûte une comparaison ; ne pas le lire coûte des kWh invisibles.

        ⚠️ CE QUE CE RETOUR PROUVE, ET CE QU'IL NE PROUVE PAS. `rc == 0` dit que le message a été
        remis à la file du client, pas que l'appareil l'a exécuté ni même que le broker l'a reçu.
        C'est une condition NÉCESSAIRE, pas suffisante — la confirmation, elle, reste le
        `properties/report` qui suit. Ne pas retomber dans le travers inverse en croyant qu'un
        `True` ici garantit l'exécution.

        JOURNALISATION SUR TRANSITION, jamais à chaque cycle : un broker coupé produirait sinon des
        centaines de lignes par minute — l'user a déjà des logs de plusieurs centaines de Mo. On
        journalise le passage marche→panne et panne→marche, et le compteur `publish_failed` porte
        le reste.
        """
        command["messageId"] = self._messageid
        command["deviceId"] = self.deviceId
        command["timestamp"] = int(datetime.now().timestamp())
        payload = json.dumps(command, default=lambda o: o.__dict__)

        target = client if client is not None else self.mqtt
        if target is None:
            self._publish_fail(topic, "aucun client MQTT disponible")
            return False

        try:
            info = target.publish(topic, payload)
        except Exception as err:  # noqa: BLE001
            self._publish_fail(topic, f"exception {type(err).__name__}: {err}")
            return False

        rc = getattr(info, "rc", 0)
        if rc != 0:
            self._publish_fail(topic, f"refus du client, rc={rc}" + (" (pas de connexion)" if rc == 4 else ""))
            return False

        if self.publish_broken:
            _LOGGER.warning("%s : publication MQTT rétablie après %s échec(s)", self.name, self.publish_failed)
            self.publish_broken = False
        return True

    def _publish_fail(self, topic: str, raison: str) -> None:
        """Comptabilise un envoi perdu, et le journalise UNE fois par épisode."""
        self.publish_failed += 1
        if not self.publish_broken:
            self.publish_broken = True
            _LOGGER.warning(
                "%s : commande MQTT PERDUE sur %s — %s. L'appareil garde sa consigne précédente.",
                self.name, topic, raison,
            )

    def mqttInvoke(self, command: Any) -> bool:
        """Envoie une commande `function/invoke`. Retourne False si elle n'est pas partie.

        1.4.4.8 : le retour est propagé jusqu'ici pour que les appelants — `power_off` en tête —
        puissent savoir que leur ordre n'a pas quitté la machine.
        """
        self._messageid += 1
        command["messageId"] = self._messageid
        command["deviceKey"] = self.deviceId
        command["timestamp"] = int(datetime.now().timestamp())
        return self.mqttPublish(self.topic_function, command)

    async def mqttProperties(self, payload: Any) -> None:
        # Un `properties/report` prouve les DEUX : la liaison répond ET l'état est rapporté.
        # Les accusés (`*/reply`), eux, ne posent que `lastseen` — cf. `mqttMessage`.
        self.lastreport = datetime.now() + timedelta(minutes=5)
        if self.lastseen == datetime.min:
            self.lastseen = datetime.now() + timedelta(minutes=5)
            self.setStatus()
        else:
            self.lastseen = datetime.now() + timedelta(minutes=5)

        if (properties := payload.get("properties", None)) and len(properties) > 0:
            for key, value in properties.items():
                self.entityUpdate(key, value)

        # update the battery properties
        if batprops := payload.get("packData", None):
            for b in batprops:
                if (sn := b.get("sn", None)) is None:
                    continue

                if (bat := self.batteries.get(sn, None)) is None:
                    bat = ZendureBattery(self.hass, sn, self, b.get("packType"))
                    self.batteries[sn] = bat

                # Always apply properties — including for newly created batteries.
                # With elif, a new battery received no entityUpdate on its first packData
                # message, so HA entities were never created until the *next* poll cycle
                # (every 60 s).  This caused batteries to be invisible after a failed
                # initial httpGet (e.g. brief WiFi outage at startup).
                if bat and b:
                    for key, value in b.items():
                        if key != "sn":
                            bat.entityUpdate(key, value)

            # Recalculate total capacity after every packData update
            # (covers both new batteries and potential pack changes)
            self.kWh = sum(0 if b is None else b.kWh for b in self.batteries.values())
            self.totalKwh.update_value(self.kWh)
            self.availableKwh.update_value((self.electricLevel.asNumber - self.minSoc.asNumber) / 100 * self.kWh)

    def mqttMessage(self, topic: str, payload: Any) -> bool:
        try:
            match topic:
                case "properties/report":
                    asyncio.run_coroutine_threadsafe(self.mqttProperties(payload), self.hass.loop)
                    # self.mqttProperties(payload)

                # ACCUSÉS DE RÉCEPTION — traités depuis le 29/07, ils étaient JETÉS.
                #
                # Mesuré au sniff du broker : le device accuse CHAQUE consigne en **184 ms** sur
                # `function/invoke/reply` (30 réponses pour 30 consignes) et répond au `getAll` en
                # 230 ms sur `properties/read/reply`. Ces messages ne portent pas d'état, mais ils
                # prouvent deux choses : la liaison fonctionne, et la commande est arrivée.
                #
                # ⚠️ POURQUOI C'EST IMPORTANT — LE CLIQUET DU SILENCE. Un Hyper ne publie QUE
                # lorsqu'une valeur change : à l'arrêt il n'a rien à dire, donc il se tait. Au bout
                # de 5 min `lastseen` expire (`power_get`), il passe OFFLINE, et `fondation.py`
                # l'ÉCARTE de la liste des devices. Plus aucune consigne ne lui est envoyée, donc
                # plus rien ne change chez lui, donc il reste muet : la boucle est fermée.
                # Mesuré le 29/07 sur `up` : OFFLINE 26 % de la journée, acceptation de charge figée
                # à 130 W (min = max sur 29 100 cycles) pour une capacité de 1200 W.
                #
                # On rafraîchit donc `lastseen` sur les accusés. Le `getAll` périodique suffit alors
                # à le maintenir dans le moteur, même parfaitement immobile.
                case "function/invoke/reply" | "properties/read/reply":
                    self.lastseen = datetime.now() + timedelta(minutes=5)
                    return True

                case "register/replay":
                    _LOGGER.info("Register replay for %s => %s", self.name, payload)
                    if self.mqtt is not None:
                        self.mqtt.publish(f"iot/{self.prodkey}/{self.deviceId}/register/replay", None, 1, True)

                case "time-sync":
                    return True

                case "properties/energy":
                    self.hemsState.update_value(1)
                    self.hemsStateUpdated = datetime.now()
                    self.setStatus()
                    return True

                case "event/device" | "event/error":
                    return True

                case "properties/read" | "function/invoke/reply" | "properties/read/reply" | "config" | "log" | "function/invoke":
                    return False

                # case "firmware/report":
                #     _LOGGER.info("Firmware report for %s => %s", self.name, payload)
                case _:
                    return False
        except Exception as err:
            _LOGGER.error(err)

        return True

    async def mqttSelect(self, _select: ZendureRestoreSelect, _value: Any) -> None:
        from .api import Api

        self.mqtt = None
        if self.lastseen != datetime.min:
            if self.connection.value == 0:
                await self.bleMqtt(Api.mqttCloud)
            elif self.connection.value == 1:
                await self.bleMqtt(Api.mqttLocal)

        _LOGGER.debug("Mqtt selected %s", self.name)

    @property
    def bleMac(self) -> str | None:
        if (conn := self.attr_device_info.get("connections", None)) is not None:
            for connection_type, mac_address in conn:
                if connection_type == "bluetooth":
                    return mac_address
        return None

    @staticmethod
    def _scanner_source(scanner_device: Any) -> str | None:
        """Extract scanner source identifier from a BluetoothScannerDevice-like object."""
        source = getattr(scanner_device, "source", None)
        if source:
            return str(source)

        if scanner := getattr(scanner_device, "scanner", None):
            source = getattr(scanner, "source", None)
            if source:
                return str(source)

        if service_info := getattr(scanner_device, "service_info", None):
            source = getattr(service_info, "source", None)
            if source:
                return str(source)

        return None

    @staticmethod
    def _scanner_ble_device(scanner_device: Any) -> Any | None:
        """Extract BLEDevice from a BluetoothScannerDevice-like object."""
        device = getattr(scanner_device, "ble_device", None)
        if device is not None:
            return device

        device = getattr(scanner_device, "device", None)
        if device is not None:
            return device

        if service_info := getattr(scanner_device, "service_info", None):
            device = getattr(service_info, "device", None)
            if device is not None:
                return device

        return None

    def ble_sources(self) -> list[str]:
        """Get available Bluetooth source identifiers from Home Assistant."""
        sources: set[str] = set()
        ble_mac = self.bleMac

        # Prefer scanner sources for this specific device.
        try:
            if ble_mac and (scanner_devices_by_address := getattr(bluetooth, "async_scanner_devices_by_address", None)):
                for scanner_device in scanner_devices_by_address(self.hass, ble_mac, True):
                    if source := self._scanner_source(scanner_device):
                        sources.add(source)
        except Exception as err:
            _LOGGER.debug("Could not read bluetooth scanner sources for %s: %s", self.name, err)

        # Fallback: derive sources from all discovered connectable advertisements.
        try:
            if discovered_service_info := getattr(bluetooth, "async_discovered_service_info", None):
                for info in discovered_service_info(self.hass, True):
                    if source := getattr(info, "source", None):
                        sources.add(str(source))
        except Exception as err:
            _LOGGER.debug("Could not derive bluetooth sources for %s: %s", self.name, err)

        return sorted(sources)

    def ble_device_from_source(self, ble_mac: str, source: str) -> Any | None:
        """Return a BLEDevice for an address constrained to a specific scanner source."""
        if scanner_devices_by_address := getattr(bluetooth, "async_scanner_devices_by_address", None):
            try:
                for scanner_device in scanner_devices_by_address(self.hass, ble_mac, True):
                    if self._scanner_source(scanner_device) != source:
                        continue
                    if device := self._scanner_ble_device(scanner_device):
                        return device
            except Exception as err:
                _LOGGER.debug("Could not get BLE device for %s on source %s: %s", self.name, source, err)

        return None

    def ble_adapter_options(self) -> dict[int, str]:
        """Build selectable BLE adapter/source options for this device."""
        options = {0: "auto"}
        for idx, source in enumerate(self.ble_sources(), start=1):
            options[idx] = source
        return options

    def selected_ble_source(self) -> str | None:
        """Return configured BLE source for this device or None for auto selection."""
        if self.bleAdapter is None:
            return None

        self.bleAdapter.setDict(self.ble_adapter_options())
        source = self.bleAdapter.current_option
        return None if source in (None, "", "auto") else str(source)

    async def bleMqtt(self, mqtt: mqtt_client.Client) -> bool:
        """Set the MQTT server for the device via BLE."""
        from .api import Api

        msg: str | None = None
        try:
            if Api.wifipsw == "" or Api.wifissid == "":
                msg = "No WiFi credentials or connections found"
                return False

            if (ble_mac := self.bleMac) is None:
                msg = "No BLE MAC address available"
                return False

            # get the bluetooth device
            ble_source = self.selected_ble_source()
            device = None
            if ble_source is not None:
                device = self.ble_device_from_source(ble_mac, ble_source)

            if device is None:
                device = bluetooth.async_ble_device_from_address(self.hass, ble_mac, True)

            if device is None:
                msg = f"BLE device {ble_mac} not found"
                if ble_source is not None:
                    msg += f" on source {ble_source}"
                return False

            try:
                _LOGGER.info("Set mqtt %s to %s", self.name, mqtt.host)
                if establish_connection is not None:
                    client = await establish_connection(BleakClient, device, self.name)
                else:
                    client = BleakClient(device)
                    await client.connect()

                try:
                    await self.bleCommand(
                        client,
                        {
                            "iotUrl": mqtt.host,
                            "messageId": 1002,
                            "method": "token",
                            "password": Api.wifipsw,
                            "ssid": Api.wifissid,
                            "timeZone": "GMT+01:00",
                            "token": "abcdefgh",
                        },
                    )

                    await self.bleCommand(
                        client,
                        {
                            "messageId": 1003,
                            "method": "station",
                        },
                    )
                finally:
                    # Ensure stale BLE sessions do not leak if command execution fails unexpectedly.
                    if client.is_connected:
                        await client.disconnect()
            except TimeoutError:
                msg = "Timeout when trying to connect to the BLE device"
                _LOGGER.warning(msg)
            except (AttributeError, BleakError) as err:
                msg = f"Could not connect to {self.name}: {err}"
                _LOGGER.warning(msg)
            except Exception as err:
                msg = f"BLE error: {err}"
                _LOGGER.warning(msg)
            else:
                self.mqtt = mqtt
                if self.zendure is not None:
                    self.zendure.loop_stop()
                    self.zendure.disconnect()
                    self.zendure = None

                self.mqttPublish(self.topic_read, {"properties": ["getAll"]}, self.mqtt)
                self.setStatus()

                return True
            return False

        finally:
            if msg is not None:
                msg = f"Error setting the MQTT server on {self.name} to {mqtt.host}, {msg}"
            else:
                msg = f"Changing the MQTT server on {self.name} to {mqtt.host} was successful"

            persistent_notification.async_create(self.hass, (msg), "Zendure", "zendure_ha")

            _LOGGER.info("BLE update ready")

    async def bleCommand(self, client: BleakClient, command: Any) -> None:
        try:
            self._messageid += 1
            payload = json.dumps(command, default=lambda o: o.__dict__)
            b = bytearray()
            b.extend(map(ord, payload))
            _LOGGER.info("BLE command: %s => %s", self.name, payload)
            await client.write_gatt_char(SF_COMMAND_CHAR, b, response=False)
        except Exception as err:
            _LOGGER.warning("BLE error: %s", err)

    async def power_get(self) -> bool:
        if self.lastseen < datetime.now():
            self.lastseen = datetime.min
            self.setStatus()

        self.actualKwh = self.availableKwh.asNumber

        if not self.online or self.socSet.asNumber == 0 or self.kWh == 0:
            self.state = DeviceState.OFFLINE
        elif self.socLimit.asInt == SmartMode.SOCFULL or self.electricLevel.asInt >= self.socSet.asNumber:
            self.state = DeviceState.SOCFULL
        elif self.socLimit.asInt == SmartMode.SOCEMPTY or self.electricLevel.asInt <= self.minSoc.asNumber:
            self.state = DeviceState.SOCEMPTY
        else:
            self.state = DeviceState.INACTIVE

        return self.state != DeviceState.OFFLINE

    async def charge(self, _power: int) -> int:
        """Set the power output/input."""
        return 0

    async def power_charge(self, power: int) -> int:
        """Set charge power."""
        power = min(0, max(power, self.charge_limit))
        """power is here a negative value, but homeInput and homeOutput are always positive"""
        if abs(power + self.homeInput.asInt - self.homeOutput.asInt) <= SmartMode.POWER_TOLERANCE:
            _LOGGER.info("Power charge %s => no action [power %s]", self.name, power)
            return - self.homeInput.asInt
        return await self.charge(power)

    async def discharge(self, _power: int) -> int:
        """Set the power output/input."""
        return 0

    async def power_discharge(self, power: int) -> int:
        """Set discharge power."""
        power = max(0, min(power, self.discharge_limit))
        if abs(power - self.homeOutput.asInt + self.homeInput.asInt) <= SmartMode.POWER_TOLERANCE:
            _LOGGER.info("Power discharge %s => no action [power %s]", self.name, power)
            return self.homeOutput.asInt
        return await self.discharge(power)

    async def power_off(self) -> None:
        """Set the power off."""

    @property
    def online(self) -> bool:
        """JOIGNABLE = on a reçu un message récemment. Et rien d'autre.

        ⛔ 1.4.5.2 — AVANT : `connectionStatus.asInt >= SmartMode.CONNECTED` (soit ≥ 10).
        Or `connectionStatus` n'est PAS un état de connexion : `setStatus()` y encode aussi
        des états de BATTERIE et de CONFIGURATION, en les faisant passer AVANT le test de
        liaison —
            0  = jamais vu           (vrai : pas de liaison)
            1  = socStatus == 1      (état de la JAUGE, rapporté par le firmware)
            2  = hemsState actif     (configuration)
            3  = fuseGroup == 0      (configuration)
            10/11/12 = cloud / local / zenSDK   (vrai état de liaison)
        Les quatre premières valeurs échouent au test `>= 10`, et `power_get` en conclut
        `DeviceState.OFFLINE` — ce qui fait RETIRER l'appareil de la liste du moteur par
        `fondation.py`. Il ne peut alors plus être commandé, donc son état ne change pas,
        donc il reste exclu : le verrou se referme sur lui-même.

        ⚠️ `socStatus` N'EST PAS « batterie pleine » — ne pas refaire cette erreur. C'est un
        état de calibration de jauge : `entityUpdate` repousse `nextCalibration` de 30 jours
        quand il RETOMBE à 0. Les SoC observés à `Conn=1` vont de 43 à 99 % (médiane 83),
        et l'utilisateur confirme qu'aucune batterie n'était pleine sur la période — la
        corrélation avec un SoC haut est réelle mais ce n'est PAS la cause.
        Ce qui compte ici ne dépend pas de sa sémantique exacte : quelle qu'elle soit, elle
        ne décrit pas la LIAISON, et n'a donc rien à faire dans le test de joignabilité.

        MESURÉ sur 535 443 cycles (04 au 12/09/2026), device `up` :
            Conn=11  498 213 cycles   SoC médian 16 %   âge médian   8 s   actif
            Conn=0    10 052 cycles   SoC médian 26 %   âge médian 517 s   VRAI silence
            Conn=1     9 058 cycles   SoC médian 83 %   âge médian   1 s   déclaré OFFLINE
        Les 9058 cycles à `Conn=1` ont un âge d'UNE SECONDE : l'appareil venait de parler.
        C'est le seul fait qui compte — la liaison était parfaite, la conclusion était fausse.
        Confirmé indépendamment côté réseau — le contrôleur Omada voit les trois appareils
        connectés 100 % du temps, sans une seule coupure Wi-Fi.

        `lastseen` est le bon critère et le seul : `power_get` le remet à `datetime.min` dès
        qu'il a expiré (l'amont le stampe à « message + 5 min »), donc `!= datetime.min`
        signifie exactement « vu dans les cinq dernières minutes ». C'est la définition de
        joignable, et elle ne dépend d'aucun état de batterie.
        """
        return self.lastseen != datetime.min

    @property
    def pwr_offgrid(self) -> int:
        """Get the offgrid power."""
        return 0


class ZendureLegacy(ZendureDevice):
    """Zendure Legacy class for devices."""

    def __init__(self, hass: HomeAssistant, deviceId: str, name: str, model: str, definition: dict[str, str], parent: str | None = None) -> None:
        """Initialize Device."""
        super().__init__(hass, deviceId, name, model, definition, parent)
        self.connection = ZendureRestoreSelect(self, "connection", {0: "cloud", 1: "local"}, self.mqttSelect, 0)
        self.mqttReset = ZendureButton(self, "mqttReset", self.button_press)
        self.bleAdapter = ZendureRestoreSelect(self, "bleAdapter", self.ble_adapter_options(), self.bleAdapterSelect, 0)

    async def bleAdapterSelect(self, _select: ZendureRestoreSelect, _value: Any) -> None:
        # Refresh available sources whenever selection changes or is restored.
        if self.bleAdapter is not None:
            self.bleAdapter.setDict(self.ble_adapter_options())

    async def button_press(self, button: ZendureButton) -> None:
        from .api import Api

        match button.translation_key:
            case "mqtt_reset":
                _LOGGER.info("Resetting MQTT for %s", self.name)
                await self.bleMqtt(Api.mqttCloud if self.connection.value == 0 else Api.mqttLocal)

    async def dataRefresh(self, _update_count: int) -> None:
        """Refresh the device data."""
        from .api import Api

        if self.lastseen != datetime.min:
            self.mqttPublish(self.topic_read, {"properties": ["getAll"]}, self.mqtt)
        else:
            self.mqttPublish(self.topic_read, {"properties": ["getAll"]}, Api.mqttCloud)
            self.mqttPublish(self.topic_read, {"properties": ["getAll"]}, Api.mqttLocal)

    def mqttMessage(self, topic: str, payload: Any) -> bool:
        if topic == "register/replay":
            _LOGGER.info("Register replay for %s => %s", self.name, payload)
            return True

        return super().mqttMessage(topic, payload)


class ZendureZenSdk(ZendureDevice):
    """Zendure Zen SDK class for devices."""

    def __init__(self, hass: HomeAssistant, deviceId: str, name: str, model: str, definition: dict[str, str], parent: str | None = None) -> None:
        """Initialize Device."""
        self.session = async_get_clientsession(hass, verify_ssl=False)
        super().__init__(hass, deviceId, name, model, definition, parent)
        self.connection = ZendureRestoreSelect(self, "connection", {0: "cloud", 2: "zenSDK"}, self.mqttSelect, 0)
        self.httpid = 0

    async def mqttSelect(self, select: Any, _value: Any) -> None:
        from .api import Api

        self.mqtt = None
        match select.value:
            case 0:
                Api.mqttCloud.unsubscribe(f"/{self.prodkey}/{self.deviceId}/#")
                Api.mqttCloud.unsubscribe(f"iot/{self.prodkey}/{self.deviceId}/#")

            case 2:
                Api.mqttCloud.unsubscribe(f"/{self.prodkey}/{self.deviceId}/#")
                Api.mqttCloud.unsubscribe(f"iot/{self.prodkey}/{self.deviceId}/#")

        _LOGGER.debug("Mqtt selected %s", self.name)

    async def entityWrite(self, entity: EntityZendure, value: Any) -> None:
        if entity.translation_key is None:
            _LOGGER.error("Entity %s has no translation_key, cannot write property %s", entity.name, self.name)
            return

        if self.online and self.connection.value == 0:
            await super().entityWrite(entity, value)
        else:
            _LOGGER.info("Writing property %s %s => %s", self.name, entity.propertyName, value)
            await self.httpPost("properties/write", {"properties": {entity.propertyName: value}})

    async def dataRefresh(self, update_count: int) -> None:
        # 1.4.4 amont : en zenSDK on interroge AUSSI au poll 60 s, pas seulement hors ligne.
        # Mesuré avant de l'accepter : `dataRefresh` tourne 1×/min (SCAN_INTERVAL=60 s) quand
        # `power_get` interroge ~1×/s -> +1,7 % de requêtes HTTP. Négligeable.
        if (update_count == 0 and not self.online) or self.connection.value == SmartMode.ZENSDK:
            if json := await self.httpGet("properties/report"):
                await self.mqttProperties(json)

    async def power_get(self) -> bool:
        """Get the current power.

        ⛔ 1.4.3.49 — UN ÉCHEC HTTP N'EST PAS UNE RÉPONSE. `httpGet` renvoie `{}` quand la requête
        a levé, et pose `lastseen = datetime.min` pour le signaler. Or `mqttProperties` commence par
        remettre `lastseen`/`lastreport` à `now + 5 min` AVANT tout contrôle du contenu : appelée
        avec `{}`, elle EFFAÇAIT ce signal d'échec, puis ne mettait à jour aucune entité faute de
        données. L'appareil était donc déclaré frais (`Age` = 0) avec des valeurs GELÉES à leur
        dernier état connu, et le moteur calculait dessus.

        MESURÉ le 08/08 de 12:57:31 à 15:17 (2 h 20) : le SolarFlow rapportait `Home = -1863 W`
        figé au watt près, `Age` entre 0 et 3 s — pendant que la **pince B mesurait 75 W**, donc
        aucune absorption. Interrogé directement sur son IP, l'appareil répondait normalement.
        `house_load = P1 + Σ Home` était donc faux de 1863 W : le moteur croyait avoir placé cette
        puissance et laissait filer le reste. **2,19 kWh d'export en 2 h, zéro import.** Le blocage
        s'est levé au redémarrage de l'intégration, ce qui confirme qu'il venait d'ici.

        ⚠️ Le test est mis chez l'APPELANT HTTP, pas dans `mqttProperties` : celle-ci sert aussi le
        chemin MQTT, où recevoir un message — même pauvre — prouve que la liaison vit et justifie
        de rafraîchir `lastseen`. Les deux cas n'ont pas le même sens, on ne les traite pas au même
        endroit. Sans réponse, l'appareil retombe sur la détection normale (`setStatus` →
        `connectionStatus` 0 → OFFLINE → retiré de la liste par `fondation.py`), et le moteur cesse
        de compter une valeur morte au lieu de bâtir sa régulation dessus.
        """
        if self.connection.value != 0:
            if json := await self.httpGet("properties/report"):
                await self.mqttProperties(json)

        return await super().power_get()

    async def charge(self, power: int, _off: bool = False) -> int:
        """Set charge power."""
        _LOGGER.info("Power charge %s => %s", self.name, power)
        if power == -SmartMode.POWER_START and self.limitInput.asInt >= SmartMode.POWER_START and self.homeInput.asInt == 0:
            power = -min(self.limitInput.asInt + 4, 2 * SmartMode.POWER_START)
            _LOGGER.info("Power charge kickstart %s => %s", self.name, power)
        await self.doCommand({"properties": {"smartMode": 0 if power == 0 and self.pwr_offgrid == 0 else 1, "acMode": 1, "outputLimit": 0, "inputLimit": -power}})
        return power

    async def discharge(self, power: int) -> int:
        _LOGGER.info("Power discharge %s => %s", self.name, power)
        if power == SmartMode.POWER_START and self.limitOutput.asInt >= SmartMode.POWER_START and self.homeOutput.asInt == 0:
            power = min(self.limitOutput.asInt + 4, 2 * SmartMode.POWER_START)
            _LOGGER.info("Power discharge kickstart %s => %s", self.name, power)
        await self.doCommand({"properties": {"smartMode": 0 if power == 0 and self.pwr_offgrid == 0 else 1, "acMode": 2, "outputLimit": power, "inputLimit": 0}})
        return power

    async def power_off(self) -> None:
        """Set the power off."""
        await self.doCommand({"properties": {"smartMode": 0 if self.pwr_offgrid == 0 else 1, "acMode": 2, "outputLimit": 0, "inputLimit": 0}})

    async def doCommand(self, command: Any) -> None:
        if self.connection.value != 0:
            await self.httpPost("properties/write", command)
        else:
            self.mqttPublish(self.topic_write, command, self.mqtt)

    def _http_host(self) -> str:
        """Adresse à interroger : le candidat courant (IP du cloud ou nom mDNS)."""
        return self._hosts[self._host_idx] if self._hosts else self.ipAddress

    def _http_ok(self) -> None:
        self._http_fails = 0

    def _http_ko(self) -> None:
        """Un échec de plus ; au bout de HTTP_FAILS_SWAP on essaie l'autre adresse."""
        self._http_fails += 1
        if self._http_fails >= HTTP_FAILS_SWAP and len(self._hosts) > 1:
            self._host_idx = (self._host_idx + 1) % len(self._hosts)
            self._http_fails = 0
            self.ipAddress = self._hosts[self._host_idx]  # reflète l'adresse RÉELLEMENT utilisée
            _LOGGER.warning("%s injoignable : bascule sur %s", self.name, self.ipAddress)

    async def httpGet(self, url: str, key: str | None = None) -> dict[str, Any]:
        try:
            url = f"http://{self._http_host()}/{url}"
            response = await self.session.get(url, headers=CONST_HEADER, timeout=CONST_TIMEOUT)
            payload = json.loads(await response.text())
            self.lastseen = datetime.now()
            self._http_ok()
            return payload if key is None else payload.get(key, {})
        except Exception as e:
            _LOGGER.error("%s for %s during httpGet%s", type(e).__name__, self.name, f": {e}" if str(e) else "!")
            self.lastseen = datetime.min
            self._http_ko()
        return {}

    async def httpPost(self, url: str, command: Any) -> bool:
        try:
            self.httpid += 1
            command["id"] = self.httpid
            command["sn"] = self.snNumber
            url = f"http://{self._http_host()}/{url}"
            await self.session.post(url, json=command, headers=CONST_HEADER, timeout=CONST_TIMEOUT)
            self._http_ok()
        except Exception as e:
            _LOGGER.error("%s for %s during httpPost%s", type(e).__name__, self.name, f": {e}" if str(e) else "!")
            self.lastseen = datetime.min
            self._http_ko()
            return False
        return True


@dataclass
class DeviceSettings:
    device_id: str
    fuseGroup: str
    limitCharge: int
    limitDischarge: int
    maxSolar: int
    kWh: float = 0.0
    socSet: float = 100
    minSoc: float = 0
