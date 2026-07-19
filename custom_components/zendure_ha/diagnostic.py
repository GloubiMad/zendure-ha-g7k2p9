"""Détection de désaccord consigne/réalité — module SÉPARÉ, hors amont Zendure.

Le moteur dimensionne toujours sur ce qu'il COMMANDE, jamais sur ce qui est LIVRÉ. Quand un
device n'obéit pas, l'écart part au compteur et rien ne le signale. Trois mécanismes distincts
ont été mesurés le 19/07/2026, tous invisibles au moteur :

  - glagla bride sa sortie AC de ~125 W quand température ≥61 °C ET SoC ≳60 (conjonction) ;
  - le SolarFlow tapère en fin de charge : plafond d'acceptation à 1167 W à 94 % de SoC, mais
    808 W à 96 % et 292 W à 97 % — alors que `ChLim` annonce -2400 W en permanence et que
    `St` reste à 2 (jamais SOCFULL avant 100 %) ;
  - `up`, écarté des puits par le filtre bypass, restait à 0 W avec 4,5 kWh de place libre.

Ce module ne CORRIGE rien : il mesure `livré − commandé` par device, l'expose, et signale les
désaccords PERSISTANTS en nommant le coupable.

    somme des écarts ≈ − erreur de P1

RÉGLAGE (validé hors ligne sur 23 h de trace réelle, 52278 lignes) : EMA α=0.05 (~90 s) pour
absorber la latence d'actuateur (2-6 s mesurés) et les rampes de démarrage, puis seuil 80 W
avec persistance 10 min. Résultat : 6 épisodes en 23 h, tous réels, aucun faux positif — et un
épisode de 107 min qui était passé inaperçu à l'analyse manuelle.

⚠️ Un seuil « évident » de 300 W d'un coup RATERAIT glagla : son écart (−125 W) est sous le p90
du bruit normal du désaccord total (192 W). C'est la PERSISTANCE qui sépare le signal du bruit,
pas l'amplitude. Ne pas remonter le seuil sans rallonger le dwell.

CONCEPTION : entités créées et mises à jour depuis ce module, rien dans `device.py` /
`manager.py` au-delà de l'instanciation et de l'appel.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from .device import ZendureDevice
from .fondation import FondationNumber
from .sensor import ZendureSensor

_LOGGER = logging.getLogger(__name__)

# EMA des écarts : ~90 s à 5 s de cycle. Assez long pour ignorer la latence d'actuateur et les
# rampes, assez court pour qu'un vrai décrochage soit visible en une poignée de minutes.
ECART_ALPHA = 0.05


class EngineDiagnostic:
    """Compare consigne et réalité, expose l'écart, nomme le coupable. Instancié par le Manager."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager
        self.ema: dict[str, float] = {}          # écart lissé par deviceId
        self.since: dict[str, datetime | None] = {}  # début du dépassement en cours
        self.alerted: set[str] = set()           # épisodes déjà journalisés (anti-spam)
        self._ent: dict[str, ZendureSensor] = {}

    # ------------------------------------------------------------------ entités
    def createManagerEntities(self) -> None:
        """Capteurs de synthèse + paramètres à chaud, sur le device Manager."""
        m = self.manager
        self.sensor_total = ZendureSensor(m, "diag_desaccord", None, "W", "power", "measurement", 0)
        self.sensor_culprit = ZendureSensor(m, "diag_coupable")
        # Seuil BAS + dwell LONG : c'est la persistance qui discrimine, pas l'amplitude (cf. docstring).
        self.seuil = FondationNumber(m, "diag_seuil", 80, 20, 500, "W")
        self.dwell = FondationNumber(m, "diag_dwell", 600, 60, 3600, "s")

    def createDeviceEntities(self) -> None:
        """Un capteur d'écart par onduleur. À appeler APRÈS le chargement des devices."""
        for d in self.manager.devices:
            if d.deviceId not in self._ent:
                self._ent[d.deviceId] = ZendureSensor(d, "diagEcart", None, "W", "power", "measurement", 0)

    # ------------------------------------------------------------------ calcul
    def update(self, devices: list[ZendureDevice], p1: int, now: datetime) -> None:
        """Un cycle. À appeler AVANT le calcul des nouvelles consignes : la mesure courante se
        compare ainsi à la consigne réellement en vigueur jusqu'à maintenant, pas à celle qu'on
        s'apprête à envoyer (qui n'a encore rien produit)."""
        try:
            seuil = self.seuil.asNumber
            dwell = self.dwell.asNumber
        except Exception:
            return  # entités pas encore créées

        total = 0.0
        worst_id, worst_val, worst_name = None, 0.0, ""

        for d in devices:
            try:
                livre = float(d.homeOutput.asInt - d.homeInput.asInt)
                consigne = float(self.manager.fondation.cmd_of(d))
                ecart = livre - consigne
                prev = self.ema.get(d.deviceId)
                e = ecart if prev is None else ECART_ALPHA * ecart + (1.0 - ECART_ALPHA) * prev
                self.ema[d.deviceId] = e
                total += e

                if (ent := self._ent.get(d.deviceId)) is not None:
                    ent.update_value(int(e))

                # --- persistance ---
                if abs(e) > seuil:
                    start = self.since.get(d.deviceId)
                    if start is None:
                        self.since[d.deviceId] = now
                    elif (now - start).total_seconds() >= dwell:
                        if d.deviceId not in self.alerted:
                            self.alerted.add(d.deviceId)
                            _LOGGER.warning(
                                "Diagnostic => %s n'obeit pas depuis %.0f min : livre %+d W pour une consigne de %+d W "
                                "(ecart %+d W) ; P1 %+d W. L'ecart part au reseau.",
                                d.name, (now - start).total_seconds() / 60, int(livre), int(consigne), int(e), p1,
                            )
                        if abs(e) > abs(worst_val):
                            worst_id, worst_val, worst_name = d.deviceId, e, d.name
                else:
                    if d.deviceId in self.alerted:
                        _LOGGER.info("Diagnostic => %s est revenu en accord avec sa consigne.", d.name)
                        self.alerted.discard(d.deviceId)
                    self.since[d.deviceId] = None
            except Exception as err:
                _LOGGER.debug("EngineDiagnostic %s: %s", getattr(d, "name", "?"), err)

        if hasattr(self, "sensor_total"):
            self.sensor_total.update_value(int(total))
            self.sensor_culprit.update_value(f"{worst_name} ({worst_val:+.0f} W)" if worst_id else "aucun")
