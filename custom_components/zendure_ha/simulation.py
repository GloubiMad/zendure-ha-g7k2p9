"""Journal de simulation (`simulation.csv`) — module SÉPARÉ, hors amont Zendure.

L'amont possède un `writeSimulation` minimal ; on écrit ici un format ÉTENDU, compatible avec nos
visualiseurs et nos outils de rejeu.

Colonnes : `Time;P1;Operation;Battery;Solar;Home;SetPoint;--;` puis, PAR DEVICE, un bloc de 14 :
`bat;Prod;Home;Cmd;Soc;Conn;ChLim;St;Age;Grid;Byp;Tmp;CTmp;Byp2` — et une colonne finale de debug moteur.

⚠️ `Byp` (= `exports_bypass`, dérivé de gridReverse) et `Byp2` (= `d.byPass`, l'état réel publié par
l'appareil) sont DEUX choses différentes. C'est `Byp2` qui compte pour le pilotage.

⚠️ PIÈGES connus (à respecter côté outils d'analyse) :
  - l'en-tête contient un blob JSON PAR DEVICE que les lignes de données n'ont PAS
    -> toujours construire la table de colonnes depuis l'en-tête, JAMAIS d'indices en dur ;
  - l'en-tête n'est écrit QU'À LA CRÉATION du fichier : si l'ordre des devices change ensuite
    (rechargement de l'intégration), les colonnes décrochent silencieusement ;
  - `Age` = secondes depuis le dernier message réel (l'amont stampe `lastseen = message + 5 min`),
    `-1` si le device n'a jamais été vu ;
  - le debug moteur a UN CYCLE DE RETARD : `writeSimulation` s'exécute AVANT `powerChanged`.
"""

from __future__ import annotations

import json
import logging
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from homeassistant.helpers.restore_state import RestoreEntity

from .button import ZendureButton
from .const import ManagerMode
from .device import DeviceSettings
from .switch import ZendureSwitch

_LOGGER = logging.getLogger(__name__)


class SimulationSwitch(ZendureSwitch, RestoreEntity):
    """Switch du journal qui SURVIT aux rechargements.

    `__init__.py` fait `ZendureManager.simulation = entry.data.get(CONF_SIM, False)` au démarrage
    ET à chaque rechargement. Le switch live, lui, n'écrit que le drapeau de classe : toute mise à
    jour HACS, tout redémarrage de HA ou tout changement d'option le remettait donc à zéro, et le
    journal s'arrêtait sans prévenir — en pleine campagne de mesure.

    On restaure ici l'état précédent APRÈS cette réinitialisation (l'entité est ajoutée plus tard
    dans la séquence de démarrage), et on réaligne le drapeau de classe dessus.
    """

    def __init__(self, manager: Any, uniqueid: str, onwrite: Any, value: bool) -> None:
        self._manager = manager
        super().__init__(manager, uniqueid, onwrite, None, None, value)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            on = last.state == "on"
            self._attr_is_on = on
            type(self._manager).simulation = on
            _LOGGER.info("Simulation log restauré => %s", on)


class SimulationLog:
    """Écriture du CSV de télémétrie + switch live et bouton de rotation. Instancié par le Manager."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager

    # ------------------------------------------------------------------ entités
    def createEntities(self) -> None:
        """Switch LIVE (sans reload, en plus de l'option de config) + bouton de rotation."""
        m = self.manager
        self.switch = SimulationSwitch(m, "simulation_log", self.update_switch, type(m).simulation)
        self.button = ZendureButton(m, "simulation_rotate", self.rotate)

    async def update_switch(self, entity: ZendureSwitch, value: Any) -> None:
        """Active/désactive le log À CHAUD. Le drapeau vit sur la CLASSE du manager (gate de l'amont)."""
        type(self.manager).simulation = bool(value)
        entity.update_value(int(bool(value)))
        _LOGGER.info("Simulation log => %s", bool(value))

    async def rotate(self, _button: ZendureButton) -> None:
        """Renomme simulation.csv en simulation_AAAAMMJJ_HHMMSS.csv ; un neuf est créé au prochain cycle."""
        try:
            path = self._path()
            if path.exists():
                newname = path.with_name(f"simulation_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
                path.rename(newname)
                _LOGGER.info("Simulation log rotated => %s", newname)
        except Exception as err:
            _LOGGER.error("rotate_simulation: %s", err)

    # ------------------------------------------------------------------ écriture
    def _path(self) -> Path:
        # Chemin explicite dans /config : le CWD de HA n'est pas garanti.
        return Path(self.manager.hass.config.path("simulation.csv"))

    def _header(self) -> str:
        return (
            "Time;P1;Operation;Battery;Solar;Home;SetPoint;--;"
            + ";".join([
                f"bat;Prod;Home;Cmd;Soc;Conn;ChLim;St;Age;Grid;Byp;Tmp;CTmp;Byp2;{
                    json.dumps(
                        DeviceSettings(
                            d.name,
                            # fuseGrp n'est PAS assigné pour un device hors fusegroup
                            # (annotation sans valeur dans device.py) -> ne pas crasher.
                            fg.name if (fg := getattr(d, 'fuseGrp', None)) is not None else '-',
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
                for d in self.manager.devices
            ])
            + "\n"
        )

    def write(self, time: datetime, p1: int) -> None:
        """Une ligne de télémétrie. Appelé par le Manager depuis `_p1_changed`."""
        m = self.manager
        try:
            path = self._path()
            if path.exists() is False:
                _LOGGER.info("Creating simulation log: %s", path)
                with path.open("w") as f:
                    f.write(self._header())

            with path.open("a") as f:
                data = ""
                tbattery = 0
                tsolar = 0
                thome = 0

                for d in m.devices:
                    tbattery += (pwr_battery := d.batteryOutput.asInt - d.batteryInput.asInt)
                    tsolar += (pwr_solar := d.solarInput.asInt)
                    thome += (pwr_home := d.homeOutput.asInt - d.homeInput.asInt)
                    # St = DeviceState (0=OFFLINE 1=SOCEMPTY 2=INACTIVE 3=SOCFULL 4=ACTIVE)
                    # ⚠️ `Age` suit `lastreport` (dernier `properties/report`), PAS `lastseen`.
                    # Depuis le 29/07 `lastseen` est aussi rafraîchi par les accusés `*/reply`, qui
                    # arrivent 184 ms après chaque consigne : l'y brancher rendrait `Age` toujours
                    # nul et CHANGERAIT le sens de la colonne sans prévenir, faussant toute
                    # comparaison avec les traces antérieures. `Age` garde donc exactement sa
                    # définition d'origine : secondes depuis la dernière remontée d'ÉTAT.
                    age = int((time - (d.lastreport - timedelta(minutes=5))).total_seconds()) if d.lastreport != datetime.min else -1
                    # Grid = gridReverse (0=disabled 1=allow 2=forbidden ; -1 si non reçu).
                    # ⚠️ DEUX notions distinctes sous des noms voisins, ne pas les confondre :
                    #   Byp  = `exports_bypass`, simple booléen dérivé de gridReverse (== allow) ;
                    #   Byp2 = `d.byPass`, l'ÉTAT RÉEL de bypass publié par l'appareil.
                    # C'est Byp2 qui pilote le saut d'envoi de la consigne 0 dans fondation.py, et son
                    # absence du CSV a rendu un bug indéductible le 20/07 (il fallait le déduire du
                    # comportement au lieu de le lire).
                    gr = d.entities.get("gridReverse")
                    grv = getattr(gr, "value", None) if gr is not None else None
                    grid = grv if grv is not None else -1
                    # Tmp = température onduleur, CTmp = température cellule max (BMS) ; "" si non publié.
                    te = d.entities.get("hyperTmp")
                    ce = d.entities.get("maxTemp")
                    tmp = te.native_value if te is not None and getattr(te, "native_value", None) is not None else ""
                    ctmp = ce.native_value if ce is not None and getattr(ce, "native_value", None) is not None else ""
                    data += (
                        f";{pwr_battery};{pwr_solar};{pwr_home};{m.fondation.cmd_of(d)};{d.electricLevel.asInt}"
                        f";{d.connectionStatus.asInt};{d.charge_limit};{d.state.value};{age};{grid};{int(d.exports_bypass)}"
                        f";{tmp};{ctmp};{d.byPass.asInt}"
                    )

                # Queue de ligne debug du moteur fondation (colonne finale, ignorée par les parseurs).
                tail = f";{m.fondation.debug}" if m.operation == ManagerMode.FONDATION else ""
                f.write(f"{time};{p1};{m.operation};{tbattery};{tsolar};{thome};{m.setpoint};" + data + tail + "\n")
        except Exception as err:
            _LOGGER.error("writeSimulation: %s", err)
            _LOGGER.error(traceback.format_exc())
