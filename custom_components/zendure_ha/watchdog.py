"""Watchdog MQTT — module SÉPARÉ, hors amont Zendure.

But : détecter un device qui ne publie plus de PROPRIÉTÉS et l'escalader progressivement pour le
réveiller, en MESURANT ce qui marche réellement.

⚠️ Le PINGREQ keepalive ne met à jour aucune des deux fraîcheurs : « muet » ≠ « déconnecté ».
L'intégration ne peut donc distinguer veille et plantage qu'en SONDANT le device.

⚠️ CE MODULE SURVEILLE `lastreport`, PAS `lastseen` (29/07). Les deux ne répondent pas à la même
question : `lastseen` dit « la liaison répond » (posé aussi par les accusés `*/reply`, qui arrivent
en 184 ms après chaque consigne), `lastreport` dit « l'état est rapporté » (posé uniquement par
`properties/report`). Le watchdog doit se fier au SECOND, sinon un appareil qui accuse encore
réception mais ne publie plus son état passerait pour sain — c'est exactement le mode de panne du
bug TLS (cf. `hyper-firmware-cipher-mqtt`), où glagla répondait au réseau tout en restant 47 s muet.

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
from .const import ManagerMode
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

# --- 1.4.4.9 — RÉÉMISSION DE L'ARRÊT ---------------------------------------------------------
# `power_off` était un « envoie et oublie » déclenché sur un ÉVÉNEMENT UNIQUE : le changement de
# mode. Si l'appareil était injoignable à cet instant précis, rien ne rattrapait. Mesuré le
# 09/09/2026 à 21:36 : le moteur passe en `off`, `up` est muet depuis 545 s, son ordre part dans
# le vide et il continue à décharger 855 W sur sa dernière consigne. Sur 127 h, `up` est
# injoignable 1,9 % du temps (23 épisodes, jusqu'à 28 min) — autant d'occasions de rater l'arrêt.
WD_OFF_RETRY = 30      # s entre deux réémissions ; l'appareil accuse en 184 ms, 30 s est large
WD_OFF_TOL = 20        # W ; en dessous on considère l'appareil arrêté (bruit de mesure)
WD_OFF_ALERT = 10      # tentatives avant de prévenir : ~5 min sans obtenir l'arrêt


@dataclass
class _WdState:
    """État d'escalade d'UN device (vit ici, pas sur l'objet device)."""

    stale_since: datetime | None = None
    stage: int = 0  # 0=OK, 1=getAll, 2=puissance, 3=ble, 4=plantage
    wake_by: str = ""
    probe_at: datetime | None = None
    probe_kind: str = ""
    ble_running: bool = False  # single-flight du toggle BLE
    # 1.4.4.9 — suivi de l'arrêt demandé mais pas constaté
    off_since: datetime | None = None
    off_last: datetime | None = None
    off_tries: int = 0
    off_alerted: bool = False


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
                # 1.4.4.9 : reste à 0 tant que l'arrêt est obtenu du premier coup — donc aucun
                # churn de recorder en fonctionnement normal.
                "offretry": ZendureSensor(d, "mqttOffRetry", None, None, None, "measurement", 0, state=0),
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

        self.reemettre_arret(now)

        for d in self.manager.devices:
            st = self.state(d)

            if d.lastreport == datetime.min:
                if st.stage != 0:  # jamais vu / marqué hors-ligne ailleurs -> reset état
                    st.stage = 0
                    st.stale_since = None
                    st.probe_at = None
                    st.probe_kind = ""
                    self._set(d, "stalled", 0)
                continue

            stale = int((now - (d.lastreport - timedelta(minutes=5))).total_seconds())
            self._set(d, "silence", stale if stale > t_getall else 0)
            self._set(d, "broker", "cloud" if getattr(d.connection, "value", 1) == 0 else "local")

            # --- cadence normale / reprise ---
            if stale <= t_getall:
                if st.stage != 0:
                    # attribution HONNÊTE : un probe n'est crédité que si le device a republié
                    # dans les WD_RESPONSE s qui l'ont suivi ; sinon reprise spontanée (ou reset manuel).
                    last_msg = d.lastreport - timedelta(minutes=5)
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
    def reemettre_arret(self, now: datetime) -> None:
        """Réémet `power_off` tant qu'un appareil débite alors que le moteur est arrêté.

        Séparée de l'escalade de silence, volontairement : un appareil peut être parfaitement
        joignable et n'avoir quand même pas reçu l'ordre (message perdu, cf. 1.4.4.8). Les deux
        problèmes n'ont ni la même cause ni le même critère de sortie. Et une méthode à part se
        teste seule.

        CRITÈRE DE SORTIE = LA MESURE, PAS L'ENVOI. On ne s'arrête pas quand on a « réussi à
        publier » — la 1.4.4.8 rappelle qu'un `rc == 0` ne prouve pas l'exécution — mais quand
        l'appareil ne débite plus. C'est la seule preuve qui vaille.

        Ne dépend PAS de `lastreport` : la boucle d'escalade s'arrête sur `datetime.min` (appareil
        jamais vu), or c'est précisément un appareil dont on ne sait rien qu'il faut continuer
        d'essayer d'arrêter.
        """
        if getattr(self.manager, "operation", None) != ManagerMode.OFF:
            return

        for d in self.manager.devices:
            st = self.state(d)
            debit = abs(d.homeOutput.asInt - d.homeInput.asInt)
            if debit <= WD_OFF_TOL:
                if st.off_tries and not st.off_alerted:
                    _LOGGER.info("Watchdog %s: arrêt obtenu après %d réémission(s)", d.name, st.off_tries)
                st.off_since = None
                st.off_last = None
                st.off_tries = 0
                st.off_alerted = False
                self._set(d, "offretry", 0)
                continue

            if st.off_since is None:
                st.off_since = now
            if st.off_last is not None and (now - st.off_last).total_seconds() < WD_OFF_RETRY:
                continue

            st.off_last = now
            st.off_tries += 1
            self._set(d, "offretry", st.off_tries)
            _LOGGER.warning(
                "Watchdog %s: moteur à l'arrêt mais l'appareil débite encore %d W — "
                "réémission de power_off (tentative %d)", d.name, debit, st.off_tries,
            )
            self.manager.hass.async_create_task(d.power_off())

            # PRÉVENIR UNE FOIS, puis continuer d'essayer. Renoncer laisserait l'appareil
            # débiter sans limite ; alerter à chaque tentative noierait le journal.
            if st.off_tries >= WD_OFF_ALERT and not st.off_alerted:
                st.off_alerted = True
                persistent_notification.async_create(
                    self.manager.hass,
                    f"**{d.name}** débite encore **{debit} W** alors que le moteur est à l'arrêt, "
                    f"après **{st.off_tries}** tentatives d'arrêt sur "
                    f"{int((now - st.off_since).total_seconds() / 60)} min.\n\n"
                    f"L'appareil ne reçoit pas ses commandes ou ne les applique pas. "
                    f"Vérifier `mqttOffRetry`, `mqttBroker` et le sélecteur de connexion.",
                    "Zendure — arrêt non obtenu",
                    f"zendure_offstuck_{d.deviceId}",
                )
            self.manager.hass.bus.async_fire(
                "zendure_power_off_retry",
                {"device": d.name, "device_id": d.deviceId, "watts": debit, "tries": st.off_tries},
            )

    async def wake(self, d: ZendureDevice, stage: int) -> None:
        """Tentative de réveil. Seuls les devices MQTT+BLE (Legacy) savent le faire aujourd'hui ;
        les ZenSDK (HTTP local) seront traités plus tard avec leurs propres actes."""
        from .api import Api

        if not isinstance(d, ZendureLegacy):
            return  # pas de chemin de réveil connu pour ce type de device

        if stage == 1:
            # ⛔ 1.4.4.3 — ON REFAIT CE QUE FAIT UN REDÉMARRAGE DE HA, SANS REDÉMARRER.
            #
            # Le 22/08, `up` est resté muet 4 h 33 : `Age` 16 611 s, valeurs FIGÉES au watt près
            # (SoC 21, bat -203), retiré de la liste du moteur. Il fonctionnait pourtant — il a
            # exécuté sa dernière consigne (-238 W) et chargé de 21 % à 46 %, soit ~960 Wh absorbés
            # à l'insu du moteur, pendant que P1 montrait des exports jusqu'à -1055 W.
            # Le log du broker le prouve : `r2wPe1KW` ABSENT du `.178` sur toute la plage, alors que
            # glagla y encaissait une coupure « Protocol error » toutes les ~13 min et se
            # reconnectait à chaque fois. Et `up` est revenu en `Conn = 10` — CLOUD, pas local.
            #
            # ⛔ Le firmware n'avait PAS abandonné : un simple redémarrage de HA l'a récupéré
            # instantanément, et HA ne touche pas l'appareil. C'est donc l'INTÉGRATION qui était
            # sourde. Ce qu'un redémarrage change et qu'un `getAll` ne change pas :
            #   1. il RECONNECTE les clients MQTT (`Api.Connect` -> `mqttInit`) ;
            #   2. `mqttConnect` (on_connect) RE-SOUSCRIT aux topics de tous les devices ;
            #   3. `dataRefresh` interroge alors sur les DEUX brokers.
            # Aucun des 4 paliers du watchdog ne faisait ces trois choses — d'où 4 h 33 de silence
            # qu'aucune escalade ne pouvait lever.
            #
            # ⚠️ Une publication sur un client déconnecté échoue SANS ERREUR (paho renvoie un code
            # que `mqttPublish` ne regarde pas) : les 273 `getAll` de ces 4 h 33 sont partis dans le
            # vide sans laisser une ligne. D'où la reconnexion explicite AVANT de republier.
            for client in (Api.mqttLocal, Api.mqttCloud):
                if client is None:
                    continue
                try:
                    if not client.is_connected():
                        _LOGGER.warning("Watchdog %s : client MQTT déconnecté -> reconnexion", d.name)
                        client.reconnect()
                    # re-souscription : exactement ce que fait `mqttConnect` au démarrage
                    client.subscribe(f"/{d.prodkey}/{d.deviceId}/#")
                    client.subscribe(f"iot/{d.prodkey}/{d.deviceId}/#")
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("Watchdog %s : remise en liaison impossible (%s)", d.name, err)
            # ⚠️ Sur les DEUX brokers, pas seulement celui où il parlait avant : `up` avait basculé
            # sur le cloud, on l'interrogeait sur le local. C'est ce que fait `dataRefresh` quand
            # `lastseen` a expiré, et le watchdog ne le faisait pas.
            for client in (Api.mqttLocal, Api.mqttCloud):
                if client is not None:
                    d.mqttPublish(d.topic_read, {"properties": ["getAll"]}, client)

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
            if d.lastreport != datetime.min:
                stale = (datetime.now() - (d.lastreport - timedelta(minutes=5))).total_seconds()
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
