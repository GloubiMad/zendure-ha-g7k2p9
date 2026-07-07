"""Moteur de régulation alternatif « fondation » — OPTION supplémentaire, n'altère pas le moteur 1.4.2.

Principes (validés en simulation matérielle 13/13 + rejeu sur 25 traces réelles, cf. tools/fondation_corrigee.md) :
  - house_load = p1 + Σ(homeOutput − homeInput) : MESURÉ (invariant = conso − solaire tiers),
    pas de solaire « théorique » (c'était le bug de sur-crédit : flap, import non couvert, grid-charge).
  - Régime IDLE/DIS/CHG avec hystérésis (db_on ≠ db_off) décidé sur house_load LISSÉ (split-EMA)
    + fast-track : au-delà de ±fast_track W sur le signal BRUT, réaction immédiate.
  - Intégrateur P1 en charge ET décharge : comble l'écart commande↔livré (overhead, auto-charge)
    quel que soit le firmware. Borné, anti-windup.
  - Distribution décharge : le PV des producteurs d'abord (lissé par pv-EMA, − overhead), puis les
    batteries plus-plein-d'abord avec hystérésis sticky (anti-permutation).
  - Distribution charge : sans-PV d'abord, puis plus vide. Les SOCFULL et bypass actifs sont exclus.

TOUS les paramètres sont réglables à chaud (entités number du Manager, restaurées au redémarrage),
pour pouvoir les faire varier selon la journée et automatiser leur réglage plus tard.
"""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberMode

from .const import DeviceState, ManagerState
from .device import ZendureDevice
from .entity import EntityDevice
from .number import ZendureRestoreNumber
from .select import ZendureRestoreSelect
from .sensor import ZendureSensor

_LOGGER = logging.getLogger(__name__)


class FondationNumber(ZendureRestoreNumber):
    """Number restaurable AVEC valeur par défaut (le parent restaure 0 quand rien n'a jamais été stocké)."""

    def __init__(self, device: EntityDevice, uniqueid: str, default: int, minimum: int, maximum: int, uom: str | None = None) -> None:
        self._default = default
        super().__init__(device, uniqueid, None, None, uom, None, maximum, minimum, NumberMode.BOX, True)
        self._attr_native_value = default

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        state = await self.async_get_last_state()
        if state is None or state.state in (None, "unknown", "unavailable"):
            self._attr_native_value = self._default


class FondationEngine:
    """Moteur fondation. Instancié par le Manager, activé par le mode d'opération « smart_fondation »."""

    def __init__(self, manager) -> None:
        self.manager = manager
        self.regime: ManagerState = ManagerState.IDLE
        self.integral = 0.0
        self.hl_ema: float | None = None       # EMA de house_load (décision de régime)
        self.pv_ema: dict[str, float] = {}     # EMA du PV par device (distribution)
        self.lead: dict[str, bool] = {}        # hystérésis sticky par device (décharge batterie)
        self.clead: dict[str, bool] = {}       # hystérésis sticky par device (charge)
        self.floor: dict[str, bool] = {}       # hystérésis de plancher SoC (sticky jusqu'à minSoc+3)
        self.debug = ""                        # queue de ligne pour simulation.csv

    def createEntities(self) -> None:
        """Paramètres à chaud + capteurs d'observabilité, sur le device Manager."""
        m = self.manager
        self.db_on = FondationNumber(m, "fondation_db_on", 80, 40, 300, "W")
        self.db_off = FondationNumber(m, "fondation_db_off", 30, 10, 100, "W")
        self.fast_track = FondationNumber(m, "fondation_fast_track", 200, 100, 1000, "W")
        self.hl_alpha = FondationNumber(m, "fondation_hl_alpha", 40, 5, 100, "%")
        self.pv_alpha = FondationNumber(m, "fondation_pv_alpha", 20, 5, 100, "%")
        self.step = FondationNumber(m, "fondation_step", 120, 20, 300, "W")
        self.hyst_device = FondationNumber(m, "fondation_hyst_device", 5, 1, 20, "%")
        self.overhead = FondationNumber(m, "fondation_overhead", 50, 0, 150, "W")
        # Stratégies de répartition (n'agissent qu'en mode smart_fondation ; le « combien » reste commun).
        # Validées au banc sur 25 traces réelles : hysteresis/wide saines ; fixed_order OK avec l'hystérésis
        # de plancher ; parallel = 0 permutation mais + de grid-charge transitoire sous bruit (expérimental).
        strategies = {0: "hysteresis", 1: "hysteresis_wide", 2: "fixed_order", 3: "parallel"}
        self.discharge_strategy = ZendureRestoreSelect(m, "fondation_discharge_strategy", dict(strategies), None)
        self.charge_strategy = ZendureRestoreSelect(m, "fondation_charge_strategy", dict(strategies), None)
        self.sensor_state = ZendureSensor(m, "fondation_state")
        self.sensor_houseload = ZendureSensor(m, "fondation_house_load", None, "W", "power", "measurement", 0)
        self.sensor_integral = ZendureSensor(m, "fondation_integral", None, "W", "power", "measurement", 0)
        self.sensor_setpoint = ZendureSensor(m, "fondation_setpoint", None, "W", "power", "measurement", 0)

    async def update(self, p1: int) -> None:
        """Un cycle de régulation. Appelé par powerChanged quand operation == FONDATION."""
        # fuseGrp n'est pas assigné pour un device hors fusegroup (annotation sans valeur
        # dans device.py) -> l'exclure du moteur au lieu de crasher sur les caps.
        devices: list[ZendureDevice] = [d for d in self.manager.devices if d.state != DeviceState.OFFLINE and getattr(d, "fuseGrp", None) is not None]
        if not devices:
            return

        # --- house_load mesuré (invariant) ---
        house_net = sum(d.homeOutput.asInt - d.homeInput.asInt for d in devices)
        hl_raw = float(p1 + house_net)

        # --- split-EMA : le régime décide sur le LISSÉ, les montants sur le BRUT ---
        alpha = max(0.05, min(1.0, self.hl_alpha.asNumber / 100.0))
        self.hl_ema = hl_raw if self.hl_ema is None else alpha * hl_raw + (1.0 - alpha) * self.hl_ema
        hl_reg = self.hl_ema
        db_on = self.db_on.asNumber
        db_off = self.db_off.asNumber
        ft = self.fast_track.asNumber

        # --- machine à états avec hystérésis + fast-track (brut au-delà de ±ft => immédiat) ---
        match self.regime:
            case ManagerState.IDLE:
                if hl_reg > db_on or hl_raw > ft:
                    self.regime = ManagerState.DISCHARGE
                elif hl_reg < -db_on or hl_raw < -ft:
                    self.regime = ManagerState.CHARGE
            case ManagerState.DISCHARGE:
                if hl_reg < db_off or hl_raw < -ft:
                    self.regime = ManagerState.IDLE
            case ManagerState.CHARGE:
                if hl_reg > -db_off or hl_raw > ft:
                    self.regime = ManagerState.IDLE

        # --- intégrateur P1 (comble l'écart commande↔livré ; borné, anti-windup) ---
        step = self.step.asNumber
        imax = 2 * sum(d.discharge_limit for d in devices)
        if self.regime == ManagerState.IDLE:
            self.integral = 0.0
        elif self.regime == ManagerState.DISCHARGE:
            if p1 > db_off:
                self.integral = min(self.integral + min(p1, step), imax)
            elif p1 < -db_off:
                self.integral = max(0.0, self.integral - step)
        elif p1 < -db_off:
            self.integral = min(self.integral + min(-p1, step), imax)
        elif p1 > db_off:
            self.integral = max(0.0, self.integral - step)

        # --- pv-EMA par device (lisse le PV utilisé dans la DISTRIBUTION, pas le total) ---
        palpha = max(0.05, min(1.0, self.pv_alpha.asNumber / 100.0))
        for d in devices:
            solar = float(d.solarInput.asInt)
            prev = self.pv_ema.get(d.deviceId)
            self.pv_ema[d.deviceId] = solar if prev is None else palpha * solar + (1.0 - palpha) * prev

        # --- consignes ---
        ovh = self.overhead.asNumber
        cmd: dict[ZendureDevice, float] = dict.fromkeys(devices, 0.0)
        if self.regime == ManagerState.DISCHARGE:
            demand = max(0.0, hl_raw) + self.integral
            # 1) le PV des producteurs d'abord (gratuit, ne vide pas les batteries)
            for d in devices:
                ns = max(0.0, self.pv_ema[d.deviceId] - ovh)
                take = min(demand, ns)
                cmd[d] = take
                demand -= take
            # 2) le reste sur les batteries selon la STRATÉGIE décharge.
            #    SOCEMPTY exclu (il passe déjà son PV à l'étape 1) avec hystérésis de PLANCHER :
            #    un device qui a touché minSoc reste exclu jusqu'à minSoc+3 (sinon fixed_order oscille
            #    au plancher : vidé -> exclu -> remonte d'un poil -> reprend tout -> re-vidé).
            #    SOCFULL participe : s'il ne livre que son solaire (firmware), l'intégrateur P1
            #    reporte le déficit sur le suivant.
            batt: list[ZendureDevice] = []
            for d in devices:
                if self.floor.get(d.deviceId):
                    if d.state != DeviceState.SOCEMPTY and d.electricLevel.asInt > d.minSoc.asNumber + 3:
                        self.floor[d.deviceId] = False
                        batt.append(d)
                elif d.state == DeviceState.SOCEMPTY:
                    self.floor[d.deviceId] = True
                else:
                    batt.append(d)
            fuse_used: dict[object, float] = {}
            for d in devices:  # la part PV (étape 1) compte dans le budget du fusegroup
                fuse_used[d.fuseGrp] = fuse_used.get(d.fuseGrp, 0.0) + cmd[d]

            def dis_cap(d: ZendureDevice) -> float:
                used = fuse_used.get(d.fuseGrp, 0.0)
                return max(0.0, min(d.discharge_limit, d.fuseGrp.maxpower - used) - cmd[d])

            dstrat = self.discharge_strategy.value
            if dstrat == 3:  # parallel : prorata SoC×capacité, reliquat en 2e passe
                weights = {d: max(1.0, d.electricLevel.asInt * d.kWh) for d in batt}
                total_w = sum(weights.values())
                share = demand
                for d in batt:
                    take = min(share * weights[d] / total_w if total_w > 0 else 0.0, dis_cap(d))
                    cmd[d] += take
                    demand -= take
                    fuse_used[d.fuseGrp] = fuse_used.get(d.fuseGrp, 0.0) + take
                for d in batt:
                    take = min(demand, dis_cap(d))
                    cmd[d] += take
                    demand -= take
                    fuse_used[d.fuseGrp] = fuse_used.get(d.fuseGrp, 0.0) + take
            else:
                if dstrat == 2:  # fixed_order : capacité décroissante (le plus gros d'abord)
                    batt.sort(key=lambda d: d.kWh, reverse=True)
                else:            # hysteresis / hysteresis_wide : plus-plein-d'abord + sticky
                    hyst = 15 if dstrat == 1 else self.hyst_device.asNumber
                    batt.sort(key=lambda d: d.electricLevel.asInt + (hyst if self.lead.get(d.deviceId) else 0), reverse=True)
                for d in batt:
                    take = min(demand, dis_cap(d))
                    cmd[d] += take
                    demand -= take
                    fuse_used[d.fuseGrp] = fuse_used.get(d.fuseGrp, 0.0) + take
            for d in devices:
                ns = max(0.0, self.pv_ema[d.deviceId] - ovh)
                self.lead[d.deviceId] = (cmd[d] - ns) > 5
                self.clead[d.deviceId] = False
        elif self.regime == ManagerState.CHARGE:
            rem = max(0.0, -hl_raw) + self.integral
            # SOCFULL exclu ; bypass actif exclu (sa production n'est pas dispatchable).
            cand = [d for d in devices if d.state != DeviceState.SOCFULL and d.byPass.asInt == 0]
            fuse_used = {}

            def chg_cap(d: ZendureDevice) -> float:
                used = fuse_used.get(d.fuseGrp, 0.0)
                return max(0.0, min(-d.charge_limit, -d.fuseGrp.minpower - used) + cmd[d])

            cstrat = self.charge_strategy.value
            if cstrat == 3:  # parallel : prorata place (100−SoC)×capacité, reliquat en 2e passe
                weights = {d: max(1.0, (100 - d.electricLevel.asInt) * d.kWh) for d in cand}
                total_w = sum(weights.values())
                share = rem
                for d in cand:
                    take = min(share * weights[d] / total_w if total_w > 0 else 0.0, chg_cap(d))
                    cmd[d] -= take
                    rem -= take
                    fuse_used[d.fuseGrp] = fuse_used.get(d.fuseGrp, 0.0) + take
                for d in cand:
                    take = min(rem, chg_cap(d))
                    cmd[d] -= take
                    rem -= take
                    fuse_used[d.fuseGrp] = fuse_used.get(d.fuseGrp, 0.0) + take
            else:
                if cstrat == 2:  # fixed_order : sans-PV d'abord, puis le plus gros (garde la marge PV)
                    cand.sort(key=lambda d: (self.pv_ema.get(d.deviceId, 0.0) > 25.0, -d.kWh))
                else:            # hysteresis / hysteresis_wide : sans-PV d'abord, puis plus-vide + sticky
                    hyst = 15 if cstrat == 1 else self.hyst_device.asNumber
                    cand.sort(key=lambda d: (self.pv_ema.get(d.deviceId, 0.0) > 25.0, d.electricLevel.asInt - (hyst if self.clead.get(d.deviceId) else 0)))
                for d in cand:
                    take = min(rem, chg_cap(d))
                    cmd[d] -= take
                    rem -= take
                    fuse_used[d.fuseGrp] = fuse_used.get(d.fuseGrp, 0.0) + take
            for d in devices:
                self.lead[d.deviceId] = False
                self.clead[d.deviceId] = cmd[d] < -5
        else:
            for d in devices:
                self.lead[d.deviceId] = False
                self.clead[d.deviceId] = False

        # --- application (gardes reprises du moteur 1.4.2 : bypass non stoppé, offgrid maintenu) ---
        setpoint = 0
        for d in devices:
            c = int(cmd[d])
            setpoint += c
            if c > 0:
                await d.power_discharge(c)
            elif c < 0:
                await d.power_charge(c)
            elif d.byPass.asInt > 0:
                continue
            else:
                await d.power_discharge(0 if max(0, d.pwr_offgrid) == 0 else 10)

        # --- observabilité ---
        self.sensor_state.update_value(self.regime.value)
        self.sensor_houseload.update_value(int(hl_raw))
        self.sensor_integral.update_value(int(self.integral))
        self.sensor_setpoint.update_value(setpoint)
        self.manager.operationstate.update_value(self.regime.value)
        self.manager.setpoint = setpoint
        self.debug = (
            f"fondation regime={self.regime.name} hl={int(hl_raw)} ema={int(hl_reg)}"
            f" int={int(self.integral)} sp={setpoint} strat={self.discharge_strategy.value}/{self.charge_strategy.value}"
        )
        _LOGGER.info(
            "Fondation => p1:%s house_load:%s (ema:%s) regime:%s integral:%s setpoint:%s",
            p1, int(hl_raw), int(hl_reg), self.regime.name, int(self.integral), setpoint,
        )
