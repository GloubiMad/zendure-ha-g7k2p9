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
from datetime import datetime, timedelta

from homeassistant.components.number import NumberMode
from homeassistant.helpers.event import async_track_time_interval

from .const import DeviceState, ManagerState, SmartMode
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
        self.surplus_active = False            # hystérésis du routage de surplus solaire (anti flip-flap)
        self.dir_prev: dict[str, float] = {}   # dernière consigne appliquée (dwell d'inversion)
        self.dir_pend: dict[str, int] = {}     # cycles consécutifs de signe opposé demandé
        self.cmd_applied: dict[str, int] = {}  # dernière consigne RÉELLEMENT appliquée (slew-rate)
        self._heartbeat_unsub = None           # unsub du timer de battement de régulation
        self.debug = ""                        # queue de ligne pour simulation.csv
        # Comportement producteur plein : False=STORE (stocke le surplus dans les batteries, écrête
        # le reste) ; True=BLOCK (rien ne sort, maison sur réseau). Le mapping vers un mode gridReverse
        # sera câblé plus tard (#1 = nommer les modes). Défaut STORE (le cas utile, validé).
        self._socfull_block = False

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
        # Routage du surplus solaire d'un producteur vers une batterie sans PV : ça coûte 2 conversions
        # (producteur DC->AC, bus, batterie AC->DC). Sous le seuil d'engagement, on laisse le producteur
        # encaisser lui-même (1 conversion, plus efficace). Hystérésis on/off pour ne pas faire flip-flap.
        self.surplus_on = FondationNumber(m, "fondation_surplus_on", 300, 50, 1500, "W")
        self.surplus_off = FondationNumber(m, "fondation_surplus_off", 150, 20, 1200, "W")
        # Dwell d'INVERSION (nb de cycles) : une batterie ne passe pas charge<->décharge sur un
        # transitoire court (micro-ondes, résistance) — elle garde sa consigne tant que le signe
        # opposé n'a pas persisté. Évite de fabriquer un export en réagissant un cycle trop tard.
        # 0 = désactivé. Réglable à chaud (ex. automation : lave-linge ON -> monter le dwell).
        self.dwell_invert = FondationNumber(m, "fondation_dwell_invert", 3, 0, 10, None)
        # Stratégies de répartition (n'agissent qu'en mode smart_fondation ; le « combien » reste commun).
        # Validées au banc sur 25 traces réelles : hysteresis/wide saines ; fixed_order OK avec l'hystérésis
        # de plancher ; parallel = 0 permutation mais + de grid-charge transitoire sous bruit (expérimental).
        strategies = {0: "hysteresis", 1: "hysteresis_wide", 2: "fixed_order", 3: "parallel"}
        self.discharge_strategy = ZendureRestoreSelect(m, "fondation_discharge_strategy", dict(strategies), None)
        self.charge_strategy = ZendureRestoreSelect(m, "fondation_charge_strategy", dict(strategies), None)
        self.sensor_state = ZendureSensor(m, "fondation_state")
        self.sensor_houseload = ZendureSensor(m, "fondation_house_load", None, "W", "power", "measurement", 0)
        self.sensor_forced = ZendureSensor(m, "fondation_forced", None, "W", "power", "measurement", 0)
        self.sensor_integral = ZendureSensor(m, "fondation_integral", None, "W", "power", "measurement", 0)
        self.sensor_setpoint = ZendureSensor(m, "fondation_setpoint", None, "W", "power", "measurement", 0)
        # SLEW-RATE (C) : borne la vitesse de variation de la consigne d'un device (W par cycle) — anti
        # overshoot sur transitoire (ex. up qui saute de -744 charge à +1012 décharge d'un coup -> export).
        # Complémentaire du dwell (qui bloque l'INVERSION) : ici on RAMPE l'amplitude. 0 = désactivé.
        self.slew = FondationNumber(m, "fondation_slew", 500, 0, 1200, "W")
        # BATTEMENT (A) : période où l'on RELANCE la régulation avec le dernier P1 si le capteur P1 est
        # resté silencieux plus longtemps (état figé qui ne se corrige pas, la régulation étant 100 %
        # event-driven sur le P1). 0 = désactivé (retour au comportement pur event-driven).
        self.heartbeat = FondationNumber(m, "fondation_heartbeat", 3, 0, 30, "s")
        # timer fixe 2 s ; le number ci-dessus décide À CHAUD quand agir. Nettoyé à l'unload de l'entrée.
        self._heartbeat_unsub = async_track_time_interval(m.hass, self._heartbeat_tick, timedelta(seconds=2))
        if getattr(m, "config_entry", None) is not None:
            m.config_entry.async_on_unload(self._heartbeat_unsub)

    async def _heartbeat_tick(self, _now) -> None:
        """Battement : si le capteur P1 n'a rien émis depuis `fondation_heartbeat` s (état stable) et
        pas trop longtemps (< P1_STALE_MAX, sinon P1 probablement mort), relance un cycle de régulation
        avec le dernier P1. C'est le fix racine des états figés (voir manager.regulate_now)."""
        gap = int(self.heartbeat.asNumber)
        if gap <= 0:
            return  # désactivé -> comportement pur event-driven
        m = self.manager
        if m._reg_last_p1_ts == datetime.min:
            return  # jamais reçu de P1
        elapsed = (datetime.now() - m._reg_last_p1_ts).total_seconds()
        if gap <= elapsed <= SmartMode.P1_STALE_MAX:
            await m.regulate_now()

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
        ovh = self.overhead.asNumber
        db_on = self.db_on.asNumber
        db_off = self.db_off.asNumber
        ft = self.fast_track.asNumber
        step = self.step.asNumber
        imax = sum(d.discharge_limit for d in devices)

        # --- pv-EMA par device (lisse le PV utilisé partout, sans lisser le total) ---
        palpha = max(0.05, min(1.0, self.pv_alpha.asNumber / 100.0))
        for d in devices:
            solar = float(d.solarInput.asInt)
            prev = self.pv_ema.get(d.deviceId)
            self.pv_ema[d.deviceId] = solar if prev is None else palpha * solar + (1.0 - palpha) * prev

        cmd: dict[ZendureDevice, float] = dict.fromkeys(devices, 0.0)

        # === CONTRÔLE DIRECT (modèle 1.3, confirmé terrain) ===
        # Sans CT, un Hyper OBÉIT à la consigne de sortie en mode désactivé(0)/interdit(2) : il écrête
        # son solaire pour sortir exactement ce qu'on demande (obs. terrain : 311 W). En autorisé(1) il
        # IGNORE la consigne et déverse tout (là on retombe sur l'anti-flap ci-dessous). Donc quand un
        # producteur est PLEIN et pilotable, on ne « déverse+absorbe » pas : on COMMANDE sa sortie =
        # conso + ce que les batteries peuvent encaisser, et on ÉCRÊTE le reste (0 export). Validé en
        # simu (tools/fondation3.py) : sur la vraie trace, |P1| moyen 0 W, export 0 %.
        def _obeys(d: ZendureDevice) -> bool:
            gr = d.entities.get("gridReverse")
            return getattr(gr, "value", None) in (0, 2)  # désactivé/interdit obéissent ; autorisé déverse

        full_prod = [d for d in devices if d.state == DeviceState.SOCFULL and (self.pv_ema[d.deviceId] - ovh) > 0]
        if full_prod and all(_obeys(d) for d in full_prod):
            forced, t_raw, t_reg = self._direct_control(devices, full_prod, hl_raw, ovh, p1, cmd, db_off, step, imax)
            await self._apply_and_report(devices, cmd, hl_raw, forced, t_raw, t_reg, p1)
            return

        # === sinon : ANTI-FLAP (producteur non plein, ou plein en AUTORISÉ qui déverse) ===
        # --- DÉVERSEMENT FORCÉ des producteurs PLEINS (gestion SOCFULL) ---
        # Un producteur SOCFULL ne peut pas stocker son solaire : il le pousse de force vers la
        # maison (et au réseau si non absorbé), quelle que soit la consigne. On le retire de la
        # demande CONTRÔLABLE : T* = house_load − déversement forcé. Si T* < 0, ce surplus doit
        # être ABSORBÉ en CHARGEANT les batteries non-pleines (au lieu d'exporter). C'était LE
        # trou du moteur : sans ça, house_load reste positif (invariant) → décharge en boucle → export.
        # forced : selon le MODE gridReverse envoyé au firmware du device (0=désactivé 1=autorisé 2=interdit).
        #  - AUTORISÉ (1, dump libre) : le firmware sort TOUT le solaire -> forced = passthrough THÉORIQUE
        #    (instantané, pas de lag). up absorbe le surplus ; export seulement si up saturé/plein.
        #  - DÉSACTIVÉ (0) ou INTERDIT (2) : le firmware ÉCRÊTE le solaire pour ne PAS exporter
        #    -> forced = déversement MESURÉ (suit l'écrêtage). Pas de sur-charge depuis le réseau ;
        #    le surplus est écrêté au lieu d'être stocké. (par défaut = mesuré, prudent, si mode inconnu.)
        forced = 0.0
        for d in devices:
            if d.state != DeviceState.SOCFULL or self.pv_ema[d.deviceId] <= 0:
                continue
            passthrough = max(0.0, self.pv_ema[d.deviceId] - ovh)
            gr = d.entities.get("gridReverse")
            allow = getattr(gr, "value", None) == 1 if gr is not None else False
            if allow:
                forced += passthrough
            else:
                forced += min(passthrough, float(max(0, d.homeOutput.asInt - d.homeInput.asInt)))
        t_raw = hl_raw - forced

        # --- split-EMA : le régime décide sur le LISSÉ, les montants sur le BRUT ---
        alpha = max(0.05, min(1.0, self.hl_alpha.asNumber / 100.0))
        self.hl_ema = t_raw if self.hl_ema is None else alpha * t_raw + (1.0 - alpha) * self.hl_ema
        t_reg = self.hl_ema
        db_on = self.db_on.asNumber
        db_off = self.db_off.asNumber
        ft = self.fast_track.asNumber

        # --- machine à états avec hystérésis + fast-track (brut au-delà de ±ft => immédiat) ---
        match self.regime:
            case ManagerState.IDLE:
                if t_reg > db_on or t_raw > ft:
                    self.regime = ManagerState.DISCHARGE
                elif t_reg < -db_on or t_raw < -ft:
                    self.regime = ManagerState.CHARGE
            case ManagerState.DISCHARGE:
                if t_reg < db_off or t_raw < -ft:
                    self.regime = ManagerState.IDLE
            case ManagerState.CHARGE:
                if t_reg > -db_off or t_raw > ft:
                    self.regime = ManagerState.IDLE

        # --- intégrateur P1 avec ANTI-WINDUP ---
        # ANTI-WINDUP : ne PAS accumuler l'intégrale quand la sortie est SATURÉE (tous les Hyper au
        # max/min). Sinon elle gonfle inutilement (1560 W observé terrain) et, quand la conso chute
        # d'un coup, met ~90 s à se vider = gros export transitoire. Gelée si saturé => la consigne
        # (feedforward) suit house_load instantanément. Validé en simu : pic 110 s -> 0, sans flip-flap.
        # VIDANGE modérée (jusqu'à 2×step) sur l'écart opposé pour effacer tout résidu.
        step = self.step.asNumber
        imax = sum(d.discharge_limit for d in devices)
        sat_dis = house_net >= imax - db_on                             # déchargé à fond
        sat_chg = house_net <= sum(d.charge_limit for d in devices) + db_on  # chargé à fond
        if self.regime == ManagerState.IDLE:
            self.integral = 0.0
        elif self.regime == ManagerState.DISCHARGE:
            if p1 > db_off and not sat_dis:
                self.integral = min(self.integral + min(p1, step), imax)
            elif p1 < -db_off:
                self.integral = max(0.0, self.integral - max(step, min(-p1, 2 * step)))
        elif p1 < -db_off and not sat_chg:
            self.integral = min(self.integral + min(-p1, step), imax)
        elif p1 > db_off:
            self.integral = max(0.0, self.integral - max(step, min(p1, 2 * step)))

        # --- consignes (sur la demande CONTRÔLABLE t_raw, pas house_load) ---
        cmd: dict[ZendureDevice, float] = dict.fromkeys(devices, 0.0)
        # IDLE passe aussi par ici : house_load ≈ 0 ne signifie PAS « pas de surplus solaire », mais
        # « bus équilibré » — typiquement parce que les APsystems couvrent la maison ET que le producteur
        # encaisse tout son solaire en interne. Sans ça, en IDLE on commandait 0 -> glagla encaissait
        # 975 W et up ne recevait rien (et le régime flappait sur le bruit P1). En IDLE l'intégrale est
        # nulle et demand ≈ 0 : l'étape 1) ne prend rien, seule l'étape 1bis route le surplus.
        if self.regime in (ManagerState.DISCHARGE, ManagerState.IDLE):
            demand = max(0.0, t_raw) + self.integral
            # 1) le PV des producteurs NON pleins d'abord (gratuit, ne vide pas les batteries).
            #    Les SOCFULL sont exclus : leur PV est déjà déversé de force (compté dans forced),
            #    le re-commander ici le compterait deux fois.
            for d in devices:
                if d.state == DeviceState.SOCFULL:
                    continue
                ns = max(0.0, self.pv_ema[d.deviceId] - ovh)
                take = min(demand, ns)
                cmd[d] = take
                demand -= take

            # 1bis) SURPLUS SOLAIRE -> le stocker dans les batteries SANS PV (« sans-PV d'abord »).
            # Sinon le firmware du producteur encaisse l'excédent dans SA propre batterie (observé :
            # glagla solaire 744, maison 506, elle sort 506 et met 238 dans sa batterie ; up, vide,
            # ne reçoit rien et la stratégie de charge n'est jamais appelée car house_load reste > 0 :
            # le surplus n'atteint jamais le bus). On commande donc le producteur à sortir son solaire
            # utilisable en plus (base + charge), et la batterie sans PV à l'absorber. Ce que up ne peut
            # pas prendre reste encaissé par le producteur (repli naturel, aucune consigne).
            if demand > 0:
                self.surplus_active = False  # pas de surplus : on relâche l'hystérésis
            else:
                unused = {d: max(0.0, self.pv_ema[d.deviceId] - ovh - cmd[d]) for d in devices if d.state != DeviceState.SOCFULL}
                total_unused = sum(unused.values())
                if total_unused <= 0:
                    self.surplus_active = False
                else:
                    # sinks = batteries SANS solaire (donc pas les producteurs) qui ont de la place
                    sinks = [
                        d
                        for d in devices
                        if (self.pv_ema[d.deviceId] - ovh) <= 0 and d.state != DeviceState.SOCFULL and d.byPass.asInt == 0 and d.electricLevel.asInt < 100
                    ]
                    if self.charge_strategy.value == 2:  # fixed_order : le plus gros d'abord
                        sinks.sort(key=lambda d: -d.kWh)
                    else:  # plus vide d'abord
                        sinks.sort(key=lambda d: d.electricLevel.asInt)
                    fuse_chg: dict[object, float] = {}

                    def chg_room(d: ZendureDevice) -> float:
                        used = fuse_chg.get(d.fuseGrp, 0.0)
                        return max(0.0, min(-d.charge_limit, -d.fuseGrp.minpower - used))

                    # Capacité de sortie SUPPLÉMENTAIRE des producteurs (limite device ET fusegroup).
                    # Sans ce plafond, on commanderait la charge au-delà de ce que le producteur peut
                    # sortir -> la batterie tirerait la différence du RÉSEAU (import). Cf. cas
                    # maison 200 / solaire 1500 : glagla plafonne à 1200, up ne doit charger que 1000.
                    prod_extra = {
                        d: max(0.0, min(spare, min(float(d.discharge_limit), float(d.fuseGrp.maxpower)) - cmd[d]))
                        for d, spare in unused.items()
                        if spare > 0
                    }
                    # Montant réellement routable, puis HYSTÉRÉSIS d'engagement : on ne paie les
                    # 2 conversions que si le surplus en vaut la peine (>= surplus_on) ; une fois
                    # engagé on continue jusqu'à surplus_off, pour ne pas faire démarrer/arrêter up.
                    routable = min(total_unused, sum(prod_extra.values()), sum(chg_room(d) for d in sinks))
                    seuil = self.surplus_off.asNumber if self.surplus_active else self.surplus_on.asNumber
                    self.surplus_active = routable >= seuil
                    total_charge = routable if self.surplus_active else 0.0
                    if total_charge > 0:
                        rem = total_charge
                        for d in sinks:  # les batteries sans PV absorbent
                            take = min(rem, chg_room(d))
                            cmd[d] -= take
                            rem -= take
                            fuse_chg[d.fuseGrp] = fuse_chg.get(d.fuseGrp, 0.0) + take
                        rem = total_charge
                        for d, extra_cap in prod_extra.items():  # les producteurs sortent ce surplus en plus
                            extra = min(rem, extra_cap)
                            cmd[d] += extra
                            rem -= extra

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
                # capacité de décharge ENCORE disponible pour d : min(marge device, marge fusegroup).
                # BUG corrigé : `used` inclut DÉJÀ cmd[d] (le PV de l'étape 1) -> l'ancienne formule
                # `min(limit, maxpower-used) - cmd[d]` soustrayait le PV DEUX FOIS quand le fusegroup
                # était contraint (maxpower≈limit), bloquant la décharge batterie d'un producteur.
                used = fuse_used.get(d.fuseGrp, 0.0)
                return max(0.0, min(d.discharge_limit - cmd[d], d.fuseGrp.maxpower - used))

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
            rem = max(0.0, -t_raw) + self.integral
            # SOCFULL exclu (c'est LUI qui déverse le surplus qu'on absorbe) ;
            # bypass actif exclu (sa production n'est pas dispatchable).
            cand = [d for d in devices if d.state != DeviceState.SOCFULL and d.byPass.asInt == 0]
            fuse_used = {}

            def chg_cap(d: ZendureDevice) -> float:
                # place de charge ENCORE disponible : min(marge device, marge fusegroup).
                # BUG corrigé (même famille que dis_cap) : `used` inclut déjà -cmd[d] ; l'ancienne
                # formule `min(limit, minpower-used) + cmd[d]` resoustrayait -> place sous-estimée
                # dès la 2e passe (parallel) et pour tout usage après la distribution du bus.
                used = fuse_used.get(d.fuseGrp, 0.0)
                return max(0.0, min(-d.charge_limit + cmd[d], -d.fuseGrp.minpower - used))

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

            # 1bis (CHARGE) : le surplus du bus (export des micro-onduleurs tiers) est absorbé ci-dessus.
            # S'il RESTE de la place dans les batteries sans PV, y router aussi le solaire des producteurs
            # au lieu de les laisser l'encaisser (même seuil/hystérésis qu'en décharge). Si le bus a déjà
            # saturé les batteries (rem > 0), il ne reste pas de place -> pas de routage, repli naturel.
            sinks = [d for d in cand if (self.pv_ema[d.deviceId] - ovh) <= 0 and d.electricLevel.asInt < 100]
            room_left = sum(chg_cap(d) for d in sinks)
            if room_left <= 0:
                self.surplus_active = False
            else:
                prod_extra: dict[ZendureDevice, float] = {}
                for d in devices:
                    if d.state == DeviceState.SOCFULL or cmd[d] < 0:
                        continue  # plein, ou déjà en charge depuis le bus : on ne lui demande pas de sortir
                    spare = max(0.0, self.pv_ema[d.deviceId] - ovh - cmd[d])
                    if spare > 0:
                        prod_extra[d] = min(spare, min(float(d.discharge_limit), float(d.fuseGrp.maxpower)) - cmd[d])
                routable = min(sum(prod_extra.values()), room_left)
                seuil = self.surplus_off.asNumber if self.surplus_active else self.surplus_on.asNumber
                self.surplus_active = routable >= seuil
                if self.surplus_active and routable > 0:
                    r = routable
                    for d in sinks:  # les batteries sans PV absorbent en plus
                        take = min(r, chg_cap(d))
                        cmd[d] -= take
                        r -= take
                        fuse_used[d.fuseGrp] = fuse_used.get(d.fuseGrp, 0.0) + take
                    r = routable
                    for d, cap in prod_extra.items():  # les producteurs sortent leur solaire
                        extra = min(r, cap)
                        cmd[d] += extra
                        r -= extra
            for d in devices:
                self.lead[d.deviceId] = False
                self.clead[d.deviceId] = cmd[d] < -5
        else:
            for d in devices:
                self.lead[d.deviceId] = False
                self.clead[d.deviceId] = False

        await self._apply_and_report(devices, cmd, hl_raw, forced, t_raw, t_reg, p1)

    async def _apply_and_report(self, devices, cmd, hl_raw, forced, t_raw, t_reg, p1) -> None:
        # --- DWELL D'INVERSION : pas de charge<->décharge sur un transitoire court ---
        # Une charge qui pulse ~5 s (micro-ondes en décongélation, résistance) est plus rapide que le
        # cycle du moteur : la correction arrive quand l'impulsion est finie -> on FABRIQUE un export
        # (mesuré terrain : 59 % de l'export exporté l'était pendant que up déchargeait, 28 inversions
        # en 10 min). On garde donc la consigne courante tant que le signe opposé n'a pas persisté.
        dwell = int(self.dwell_invert.asNumber)
        if dwell > 0:
            for d in devices:
                prev = self.dir_prev.get(d.deviceId, 0.0)
                if prev * cmd[d] < 0:  # inversion demandée
                    pend = self.dir_pend.get(d.deviceId, 0) + 1
                    if pend < dwell:
                        cmd[d] = prev  # on tient la consigne courante
                        self.dir_pend[d.deviceId] = pend
                    else:
                        self.dir_pend[d.deviceId] = 0
                else:
                    self.dir_pend[d.deviceId] = 0
                self.dir_prev[d.deviceId] = cmd[d]

        # --- SLEW-RATE : rampe l'amplitude de la consigne (W/cycle) par rapport à la dernière appliquée.
        # Avec le battement (cycles réguliers ~2-3 s), ça borne la vitesse réelle et écrête les overshoots
        # de transitoire. 0 = désactivé. N'empêche pas l'inversion (c'est le rôle du dwell) mais la lisse.
        slew = int(self.slew.asNumber)
        if slew > 0:
            for d in devices:
                prev = self.cmd_applied.get(d.deviceId)
                if prev is not None:
                    cmd[d] = max(prev - slew, min(prev + slew, cmd[d]))

        # --- application (gardes reprises du moteur 1.4.2 : bypass non stoppé, offgrid maintenu) ---
        setpoint = 0
        for d in devices:
            c = int(cmd[d])
            self.cmd_applied[d.deviceId] = c  # mémorise pour le slew-rate du prochain cycle
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
        self.sensor_forced.update_value(int(forced))
        self.sensor_integral.update_value(int(self.integral))
        self.sensor_setpoint.update_value(setpoint)
        self.manager.operationstate.update_value(self.regime.value)
        self.manager.setpoint = setpoint
        self.debug = (
            f"fondation regime={self.regime.name} hl={int(hl_raw)} forced={int(forced)} T={int(t_raw)}"
            f" ema={int(t_reg)} int={int(self.integral)} sp={setpoint}"
            f" strat={self.discharge_strategy.value}/{self.charge_strategy.value}"
        )
        _LOGGER.info(
            "Fondation => p1:%s house_load:%s regime:%s forced:%s integral:%s setpoint:%s",
            p1, int(hl_raw), self.regime.name, int(forced), int(self.integral), setpoint,
        )

    def _direct_control(self, devices, full_prod, base, ovh, p1, cmd, db_off, step, imax):
        """Producteur(s) plein(s) qui OBÉISSENT : on commande leur sortie = conso + charge encaissable,
        on écrête le reste (0 export). Généralisé à N entités de stockage (parc futur)."""
        forced = sum(max(0.0, self.pv_ema[d.deviceId] - ovh) for d in full_prod)  # solaire dispo des pleins
        if self._socfull_block:
            # BLOCK (#3) : producteur plein -> RIEN ne sort (maison sur réseau, 0 export). Peu utile.
            self.regime = ManagerState.IDLE
            self.integral = 0.0
            for d in devices:
                self.lead[d.deviceId] = False
                self.clead[d.deviceId] = False
            return forced, base, base
        # intégrateur P1 signé (résiduel overhead/rampe) avec ANTI-WINDUP : P1>0 import -> +sortie ;
        # P1<0 export -> -sortie ; mais on n'accumule PAS dans une direction déjà saturée.
        delivered = sum(d.homeOutput.asInt - d.homeInput.asInt for d in devices)
        sat_dis = delivered >= sum(d.discharge_limit for d in devices) - db_off
        sat_chg = delivered <= sum(d.charge_limit for d in devices) + db_off
        if p1 > db_off and not sat_dis:
            self.integral = min(self.integral + min(p1, step), imax)
        elif p1 < -db_off and not sat_chg:
            self.integral = max(-imax, self.integral - min(-p1, step))
        surplus = forced - base
        if surplus > 0:
            # STORE : stocker le surplus dans les batteries non-pleines (charge_strategy), écrêter le reste
            self.regime = ManagerState.CHARGE
            sinks = [d for d in devices if d.state != DeviceState.SOCFULL and d.byPass.asInt == 0 and d.electricLevel.asInt < 100]
            if self.charge_strategy.value == 2:  # fixed_order : sans-PV d'abord, puis le plus gros
                sinks.sort(key=lambda d: (self.pv_ema.get(d.deviceId, 0.0) > 25.0, -d.kWh))
            else:                                 # sans-PV d'abord, puis plus-vide
                sinks.sort(key=lambda d: (self.pv_ema.get(d.deviceId, 0.0) > 25.0, d.electricLevel.asInt))
            fuse_used: dict[object, float] = {}

            def chg_cap(d: ZendureDevice) -> float:
                used = fuse_used.get(d.fuseGrp, 0.0)
                return max(0.0, min(-d.charge_limit, -d.fuseGrp.minpower - used))

            cap = sum(chg_cap(d) for d in sinks)
            total_charge = max(0.0, min(surplus, cap))   # feedforward : ce que les batteries encaissent
            out = max(0.0, base + total_charge + self.integral)  # sortie producteurs + trim résiduel (P1>0 import -> +)
            for d in full_prod:
                take = min(out, max(0.0, self.pv_ema[d.deviceId] - ovh), float(d.discharge_limit))
                cmd[d] = take
                out -= take
            rem = total_charge
            for d in sinks:
                take = min(rem, chg_cap(d))
                cmd[d] = -take
                rem -= take
                fuse_used[d.fuseGrp] = fuse_used.get(d.fuseGrp, 0.0) + take
        else:
            # DÉFICIT : les pleins sortent tout leur solaire, les batteries fournissent le reste
            self.regime = ManagerState.DISCHARGE
            fuse_used = {}
            for d in full_prod:
                cmd[d] = min(max(0.0, self.pv_ema[d.deviceId] - ovh), float(d.discharge_limit))
                fuse_used[d.fuseGrp] = fuse_used.get(d.fuseGrp, 0.0) + cmd[d]
            deficit = max(0.0, base - forced + self.integral)
            batt = [d for d in devices if d not in full_prod and d.electricLevel.asInt > d.minSoc.asNumber]
            batt.sort(key=lambda d: d.electricLevel.asInt, reverse=True)
            for d in batt:
                used = fuse_used.get(d.fuseGrp, 0.0)
                cap = max(0.0, min(d.discharge_limit - cmd[d], d.fuseGrp.maxpower - used))
                take = min(deficit, cap)
                cmd[d] += take
                deficit -= take
                fuse_used[d.fuseGrp] = used + take
        for d in devices:
            self.lead[d.deviceId] = False
            self.clead[d.deviceId] = cmd[d] < -5
        return forced, base, base
