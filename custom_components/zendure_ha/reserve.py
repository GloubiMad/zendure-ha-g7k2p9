"""Réserve utilisable — module SÉPARÉ, hors amont Zendure.

Le SoC brut MENT sur ce qui reste réellement disponible : à 16 % de SoC avec minSoc = 16 %, la
batterie n'a plus rien à donner. On expose donc la réserve bornée par minSoc/socSet :

    réserve % = (SoC − minSoc) / (socSet − minSoc) × 100
    0 %  = au plancher minSoc (plus rien à donner)
    100 % = au plafond socSet

⚠️ Le PARC est pondéré par la CAPACITÉ : on additionne des kWh, jamais des pourcentages. Une moyenne
naïve des % se trompe jusqu'à ~10 points (glagla 5,76 kWh pleine + up 3,84 kWh vide → 59,7 % réel,
50 % en moyenne naïve).

CONCEPTION : entités créées et mises à jour depuis ce module, rien dans `device.py` / `manager.py`.
"""

from __future__ import annotations

import logging
from typing import Any

from .device import ZendureDevice
from .sensor import ZendureSensor

_LOGGER = logging.getLogger(__name__)


class UsableReserve:
    """Réserve exploitable par onduleur + pour le parc. Instanciée par le Manager."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager
        self._ent: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ entités
    def createManagerEntities(self) -> None:
        """Les 2 capteurs du PARC, sur le device Manager."""
        m = self.manager
        self.socParc = ZendureSensor(m, "usableSocParc", None, "%", "battery", "measurement", 1)
        self.kwhParc = ZendureSensor(m, "usableKwhParc", None, "kWh", "energy_storage", None, 2)

    def createDeviceEntities(self) -> None:
        """Les 2 capteurs par onduleur. À appeler APRÈS le chargement des devices."""
        for d in self.manager.devices:
            if d.deviceId in self._ent:
                continue
            self._ent[d.deviceId] = {
                "soc": ZendureSensor(d, "usableSoc", None, "%", "battery", "measurement", 1),
                "kwh": ZendureSensor(d, "usableKwh", None, "kWh", "energy_storage", None, 2),
            }

    # ------------------------------------------------------------------ calcul
    @staticmethod
    def _span(d: ZendureDevice) -> tuple[float, float, float]:
        """(soc, minSoc, socSet) en float ; utilitaire commun."""
        return float(d.electricLevel.asNumber), float(d.minSoc.asNumber), float(d.socSet.asNumber)

    def update(self) -> None:
        """Met à jour les capteurs par device ET ceux du parc. Appelé par le Manager à chaque cycle."""
        usable = 0.0
        usable_max = 0.0
        for d in self.manager.devices:
            try:
                soc, lo, hi = self._span(d)
                span = hi - lo
                if span <= 0 or d.kWh <= 0:
                    continue

                # --- par device ---
                if (ent := self._ent.get(d.deviceId)) is not None:
                    ent["soc"].update_value(round(max(0.0, min(100.0, (soc - lo) / span * 100.0)), 1))
                    ent["kwh"].update_value(round(max(0.0, (soc - lo) / 100.0 * d.kWh), 2))

                # --- cumul parc (en kWh, donc pondéré par la capacité) ---
                usable += max(0.0, (soc - lo) / 100.0 * d.kWh)
                usable_max += span / 100.0 * d.kWh
            except Exception as err:
                _LOGGER.debug("UsableReserve %s: %s", d.name, err)

        if hasattr(self, "kwhParc"):
            self.kwhParc.update_value(round(usable, 2))
            self.socParc.update_value(round(usable / usable_max * 100.0, 1) if usable_max > 0 else 0)
