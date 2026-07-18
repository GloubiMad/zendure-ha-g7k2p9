"""Watchdog MQTT — module SÉPARÉ, hors amont Zendure.

But : détecter un device qui ne publie plus de PROPRIÉTÉS et l'escalader progressivement pour le
réveiller, en MESURANT ce qui marche réellement.

⚠️ Le PINGREQ keepalive ne met PAS à jour `lastseen` (seul `mqttProperties` le fait) : « muet » ≠
« déconnecté ». L'intégration ne peut donc distinguer veille et plantage qu'en SONDANT le device.

Escalade (seuils en secondes, entités number À CHAUD du Manager) — chaque probe DIFFÈRE du précédent,
car un firmware bugué peut ignorer une commande identique à la précédente (no-op) :
  getAll  -> commande de puissance ≠ dernière consigne -> toggle broker BLE -> notification + event

Attribution du réveil : un probe n'est crédité QUE si le device republie dans les WD_RESPONSE s qui
suivent ; sinon la reprise est déclarée « spontané ». (Le simple « dernier palier atteint » ne prouve
PAS la causalité — cf. cas mesuré : reprise 74 s après un getAll, donc sans rapport avec lui.)

CONCEPTION : tout l'état et toutes les entités vivent ICI (dicts indexés par deviceId), pour ne rien
laisser dans `device.py` / `manager.py` (fichiers amont). Le Manager n'appelle que createEntities()
et tick().
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components import persistent_notification

from .binary_sensor import ZendureBinarySensor
from .device import ZendureDevice, ZendureLegacy
from .sensor import ZendureSensor

_LOGGER = logging.getLogger(__name__)

# --- seuils par DÉFAUT (s) ; ajustables à chaud via les number du Manager ---
# Cadence de veille mesurée (nuit 08→09/07) : p95 ~60 s, max 118 s (up) / 181 s (glagla).
# Le getAll (lecture inoffensive) peut être bas pour la visibilité ; les ACTES (puissance/BLE)
# doivent rester AU-DESSUS du plafond de veille pour ne pas agir sur un device simplement oisif.
WD_GETALL = 45
WD_POWER = 120
WD_BLE = 160
WD_ALERT = 200
WD_WAKE_NUDGE = 60  # W, écart de réveil quand la dernière consigne était nulle (≈ mini utile Hyper)
WD_RECENT = 45  # s, fenêtre « a reparlé récemment » (garde d'entrée du toggle BLE)
WD_RESPONSE = 8  # s, fenêtre de réponse pour créditer un probe


@dataclass
class _WdState:
    """État d'escalade d'UN device (vit ici, pas sur l'objet device)."""

    stale_since: datetime | None = None
    stage: int = 0  # 0=OK, 1=getAll, 2=puissance, 3=ble, 4=plantage
    wake_by: str = ""
    probe_at: datetime | None = None
    probe_kind: str = ""
    ble_running: bool = False  # single-flight du toggle BLE


class MqttWatchdog:
    """Surveillance + réveil des devices muets. Instancié par le Manager."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager
        self._state: dict[str, _WdState] = {}
        self._ent: dict[str, dict[str, Any]] = {}

    def state(self, d: ZendureDevice) -> _WdState:
        return self._state.setdefault(d.deviceId, _WdState())

    # ------------------------------------------------------------------ entités
    def createManagerEntities(self) -> None:
        """Les 4 seuils À CHAUD, sur le device Manager. À appeler avec les autres entités manager."""
        from .fondation import FondationNumber  # helper « number restaurable avec défaut »

        m = self.manager
        self.tGetall = FondationNumber(m, "watchdog_getall", WD_GETALL, 20, 300, "s")
        self.tPower = FondationNumber(m, "watchdog_power", WD_POWER, 30, 400, "s")
        self.tBle = FondationNumber(m, "watchdog_ble", WD_BLE, 40, 500, "s")
        self.tAlert = FondationNumber(m, "watchdog_alert", WD_ALERT, 60, 600, "s")

    def createDeviceEntities(self) -> None:
        """Les 4 entités d'observabilité par device. À appeler APRÈS le chargement des devices."""
        for d in self.manager.devices:
            if d.deviceId in self._ent:
                continue
            self._ent[d.deviceId] = {
                # 0 tant que le silence est normal -> pas de churn recorder ; ne monte qu'en silence ANORMAL
                "silence": ZendureSensor(d, "mqttSilence", None, "s", "duration", "measurement", 0, state=0),
                "stalled": ZendureBinarySensor(d, "mqttStalled", None, "problem"),
                "lastwake": ZendureSensor(d, "mqttLastWake", state="—"),
                "broker": ZendureSensor(d, "mqttBroker", state="local"),
            }

    def _e(self, d: ZendureDevice, key: str) -> Any:
        return self._ent.get(d.deviceId, {}).get(key)

    def _set(self, d: ZendureDevice, key: str, value: Any) -> None:
        if (ent := self._e(d, key)) is not None:
            ent.update_value(value)

    # ------------------------------------------------------------------ boucle
    def tick(self, now: datetime) -> None:
        """Un passage de surveillance. Appelé par le Manager à chaque cycle de coordinateur."""
        t_getall = int(getattr(self, "tGetall", None).asNumber or WD_GETALL) if hasattr(self, "tGetall") else WD_GETALL
        t_power = int(getattr(self, "tPower", None).asNumber or WD_POWER) if hasattr(self, "tPower") else WD_POWER
        t_ble = int(getattr(self, "tBle", None).asNumber or WD_BLE) if hasattr(self, "tBle") else WD_BLE
        t_alert = int(getattr(self, "tAlert", None).asNumber or WD_ALERT) if hasattr(self, "tAlert") else WD_ALERT

        for d in self.manager.devices:
            st = self.state(d)

            if d.lastseen == datetime.min:
                if st.stage != 0:  # jamais vu / marqué hors-ligne ailleurs -> reset état
                    st.stage = 0
                    st.stale_since = None
                    st.probe_at = None
                    st.probe_kind = ""
                    self._set(d, "stalled", 0)
                continue

            stale = int((now - (d.lastseen - timedelta(minutes=5))).total_seconds())
            self._set(d, "silence", stale if stale > t_getall else 0)
            self._set(d, "broker", "cloud" if getattr(d.connection, "value", 1) == 0 else "local")

            # --- cadence normale / reprise ---
            if stale <= t_getall:
                if st.stage != 0:
                    # attribution HONNÊTE : un probe n'est crédité que si le device a republié
                    # dans les WD_RESPONSE s qui l'ont suivi ; sinon reprise spontanée (ou reset manuel).
                    last_msg = d.lastseen - timedelta(minutes=5)
                    if st.probe_at is not None and 0 <= (last_msg - st.probe_at).total_seconds() <= WD_RESPONSE:
                        wake_by = st.probe_kind
                    else:
                        wake_by = "spontané"
                    _LOGGER.warning("Zendure %s de nouveau actif après %ds muet (réveil: %s)", d.name, stale, wake_by)
                    st.wake_by = wake_by
                    self._set(d, "lastwake", wake_by)
                    self._set(d, "stalled", 0)
                    if st.stage >= 4:  # lever la notif de plantage désormais résolu
                        persistent_notification.async_dismiss(self.manager.hass, f"zendure_stalled_{d.deviceId}")
                    st.stage = 0
                    st.stale_since = None
                    st.probe_at = None
                    st.probe_kind = ""
                continue

            # --- silence anormal : escalade (chaque palier UNE fois) ---
            if st.stale_since is None:
                st.stale_since = now
            self._set(d, "stalled", 1)

            if stale > t_alert and st.stage < 4:
                st.stage = 4
                _LOGGER.error("Zendure %s muet depuis %ds -> PROBABLE PLANTAGE (reset physique requis)", d.name, stale)
                persistent_notification.async_create(
                    self.manager.hass,
                    f"{d.name} ne répond plus depuis {stale}s malgré les tentatives de réveil "
                    f"(getAll, commande, bascule BLE). Un redémarrage manuel (interrupteur) est probablement nécessaire.",
                    title="Zendure : onduleur figé",
                    notification_id=f"zendure_stalled_{d.deviceId}",
                )
                self.manager.hass.bus.async_fire("zendure_device_stalled", {"device": d.name, "device_id": d.deviceId, "stale": stale})
            elif stale > t_ble and st.stage < 3:
                st.stage = 3
                st.probe_at = now
                st.probe_kind = "ble"
                _LOGGER.warning("Zendure %s muet %ds -> toggle broker BLE", d.name, stale)
                self.manager.hass.async_create_task(self.wake(d, 3))
            elif stale > t_power and st.stage < 2:
                st.stage = 2
                st.probe_at = now
                st.probe_kind = "puissance"
                _LOGGER.warning("Zendure %s muet %ds -> commande de réveil", d.name, stale)
                self.manager.hass.async_create_task(self.wake(d, 2))
            elif stale > t_getall and st.stage < 1:
                st.stage = 1
                st.probe_at = now
                st.probe_kind = "getall"
                _LOGGER.info("Zendure %s muet %ds -> getAll", d.name, stale)
                self.manager.hass.async_create_task(self.wake(d, 1))

    # ------------------------------------------------------------------ probes
    async def wake(self, d: ZendureDevice, stage: int) -> None:
        """Tentative de réveil. Seuls les devices MQTT+BLE (Legacy) savent le faire aujourd'hui ;
        les ZenSDK (HTTP local) seront traités plus tard avec leurs propres actes."""
        from .api import Api

        if not isinstance(d, ZendureLegacy):
            return  # pas de chemin de réveil connu pour ce type de device

        if stage == 1:  # sonde légère : demande d'état
            d.mqttPublish(d.topic_read, {"properties": ["getAll"]}, d.mqtt or Api.mqttLocal)

        elif stage == 2:  # commande de réveil DIFFÉRENTE de la dernière consigne (anti no-op)
            if abs(self.manager.fondation.cmd_of(d)) > 10:
                # dernière consigne non nulle (ex. export bloqué) -> couper : différent ET sûr
                _LOGGER.warning("Watchdog %s: réveil power_off (dernière consigne %dW)", d.name, self.manager.fondation.cmd_of(d))
                await d.power_off()
            else:
                # dernière consigne nulle -> petit écart non nul (write reçu même si non réalisable)
                _LOGGER.warning("Watchdog %s: réveil discharge %dW (dernière consigne 0)", d.name, WD_WAKE_NUDGE)
                await d.discharge(WD_WAKE_NUDGE)

        elif stage == 3:  # toggle broker via BLE (contourne le réseau figé), en tâche de fond
            self.manager.hass.async_create_task(self._ble_toggle(d))

    async def _ble_toggle(self, d: ZendureDevice) -> None:
        """Force une re-provision réseau par Bluetooth : bascule vers l'AUTRE broker puis revient au
        COURANT. Le BLE est indépendant du réseau figé, c'est ce qui en fait le dernier recours.
        Gardes : annule si le device a reparlé, retour au broker courant GARANTI (jamais d'orphelin),
        single-flight."""
        from .api import Api

        st = self.state(d)
        if st.ble_running:
            return
        st.ble_running = True
        try:
            # garde d'entrée : si le device a reparlé récemment, ne rien toucher
            if d.lastseen != datetime.min:
                stale = (datetime.now() - (d.lastseen - timedelta(minutes=5))).total_seconds()
                if stale < WD_RECENT:
                    _LOGGER.info("Watchdog %s: a reparlé avant le toggle BLE -> annulé", d.name)
                    return

            current = getattr(d.connection, "value", 1)  # 0=cloud, 1=local
            if current == 0:  # sur cloud : cloud -> local -> cloud
                other, home = Api.mqttLocal, Api.mqttCloud
            else:  # sur local : local -> cloud -> local
                other, home = Api.mqttCloud, Api.mqttLocal
            _LOGGER.warning("Watchdog %s: toggle BLE (courant=%s)", d.name, "cloud" if current == 0 else "local")
            try:
                if other is not None:
                    await d.bleMqtt(other)
                    await asyncio.sleep(10)
            finally:
                # retour au broker COURANT garanti, même si l'aller a échoué
                if home is not None:
                    await d.bleMqtt(home)
        except Exception as err:
            _LOGGER.error("Watchdog %s: toggle BLE échec: %s", d.name, err)
        finally:
            st.ble_running = False
