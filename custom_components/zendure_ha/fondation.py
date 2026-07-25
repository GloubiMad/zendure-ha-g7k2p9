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

⚠️ La restauration au redémarrage a été prise en défaut le 24/07/2026 : l'entité affichait la
valeur réglée, le moteur appliquait le DÉFAUT. Rien n'est corrigé à ce stade — voir
`FondationEngine._params`, le champ `par=` de simulation.csv, qui rend la config EFFECTIVE lisible
et doit d'abord servir à caractériser le phénomène.
"""

from __future__ import annotations

import logging
from datetime import datetime
from time import perf_counter

from homeassistant.components.number import NumberMode

from .const import DeviceState, ManagerState
from .device import ZendureDevice, ZendureZenSdk
from .entity import EntityDevice
from .number import ZendureRestoreNumber
from .select import ZendureRestoreSelect
from .sensor import ZendureSensor

_LOGGER = logging.getLogger(__name__)

# Un device reste considéré comme « producteur » ce délai après sa dernière production réelle.
# Assez long pour survivre à un passage nuageux ou à une période où on lui commande 0 (ce qui
# écrête ses panneaux et ferait disparaître la preuve), assez court pour qu'un onduleur dont on
# débranche les panneaux finisse par sortir de la catégorie.
PV_MEMORY = 1800.0  # s
PV_SEEN_MIN = 50.0  # W — au-dessus, on considère qu'il produit vraiment

# Nombre de cycles consécutifs de refus avant de plafonner un puits sur ce qu'il accepte
# réellement. 3 cycles ≈ 15 s : assez pour écarter une rampe de démarrage, assez court pour que
# le reliquat déborde sur le puits suivant bien avant les 50 s observées le 23/07.
CHG_REFUSE_N = 3

# En dessous de cette consigne, on ne conclut JAMAIS à un refus : la rampe de démarrage représente
# alors une part trop grande du montant pour que la mesure ait un sens (bang-bang observé le 24/07
# au lever du soleil, quand les consignes valaient 150-300 W).
CHG_REFUSE_MIN = 250.0

# Un appareil qui progresse d'au moins ceci d'un cycle à l'autre est en RAMPE, pas en refus.
RAMP_RISE = 25.0

# Marge de ré-exploration côté PRODUCTION. Volontairement bien plus petite que `chg_probe` :
# offrir trop à un puits est gratuit (il refuse), demander trop à un producteur crée de l'import.
# 40 W laissent la production remonter d'elle-même quand l'appareil refroidit, pour un biais
# d'import résiduel négligeable.
PROD_PROBE = 40.0


class FondationNumber(ZendureRestoreNumber):
    """Number restaurable AVEC valeur par défaut (le parent restaure 0 quand rien n'a jamais été stocké)."""

    def __init__(self, device: EntityDevice, uniqueid: str, default: int, minimum: int, maximum: int, uom: str | None = None) -> None:
        self._default = default
        # Doit exister AVANT super(), car la restauration s'exécute pendant celui-ci (cf. ci-dessous).
        self._restored: float | None = None
        super().__init__(device, uniqueid, None, None, uom, None, maximum, minimum, NumberMode.BOX, True)
        # ⚠️ COURSE DE CONSTRUCTION — bug trouvé le 24/07/2026, actif depuis le 20/07.
        #
        # `ZendureNumber.__init__` se TERMINE par `self.add([self])`, qui ajoute l'entité à Home
        # Assistant. Mesuré : l'ajout et `async_added_to_hass` s'exécutent DANS cet appel, donc
        # avant que les constructeurs des classes filles aient fini. Ordre réel, prouvé par
        # identité mémoire sur les 22 paramètres :
        #
        #   1. add()  -> async_added_to_hass -> restauration à 700, état publié à 700
        #   2. retour dans ZendureRestoreNumber.__init__ : `_attr_native_value = 0`
        #   3. retour ici                                : `_attr_native_value = default` (200)
        #
        # Les étapes 2 et 3 écrasent la valeur restaurée SANS republier l'état : l'interface
        # continuait d'afficher 700 pendant que le moteur appliquait 200. Quatre jours de réglages
        # (lissage des montants, slew, overhead, seuils du watchdog) n'ont jamais été appliqués.
        #
        # On rétablit donc ici ce que la restauration avait obtenu. `_restored` est renseigné en
        # fin de `async_added_to_hass`, quel que soit le moment où celui-ci s'exécute : si la course
        # s'inverse un jour (ajout devenu asynchrone), la ligne ci-dessous pose le défaut et
        # `async_added_to_hass` repassera derrière avec la bonne valeur. Correct dans les deux sens.
        self._attr_native_value = self._restored if self._restored is not None else default

    # Journal de restauration, écrit une fois par chargement dans simulation.csv par
    # `FondationEngine._params`. On ne garde que les paramètres RÉGLÉS (valeur ≠ défaut) : c'est la
    # liste de ce qui a été effectivement repris au démarrage, donc la preuve que le correctif de
    # course ci-dessus tient. Une entité qui disparaîtrait de cette liste serait une régression.
    trace: list[str] = []

    async def async_added_to_hass(self) -> None:
        name = str(self.propertyName).replace("fondation_", "")
        try:
            await super().async_added_to_hass()
        except Exception as err:  # noqa: BLE001 - on veut SAVOIR si la restauration lève
            FondationNumber.trace.append(f"{name} EXC-parent={type(err).__name__}:{err}")
            raise
        restored = self._attr_native_value
        state = await self.async_get_last_state()
        seen = "None" if state is None else str(state.state)
        if state is None or state.state in (None, "unknown", "unavailable"):
            self._attr_native_value = self._default
        # Mémorise le résultat : le constructeur, qui n'a pas encore fini de s'exécuter, le
        # rétablira après avoir posé ses valeurs par défaut par-dessus (cf. `__init__`).
        self._restored = self._attr_native_value
        if restored != self._default or state is None:
            FondationNumber.trace.append(f"{name} restaure={restored} defaut={self._default} lu={seen} retenu={self._attr_native_value}")


class FondationEngine:
    """Moteur fondation. Instancié par le Manager, activé par le mode d'opération « smart_fondation »."""

    def __init__(self, manager) -> None:
        self.manager = manager
        self.regime: ManagerState = ManagerState.IDLE
        self.integral = 0.0
        self.hl_ema: float | None = None       # EMA de house_load (décision de régime)
        self.amt_ema: float | None = None      # EMA de house_load (montants des consignes)
        self.pv_seen: dict[str, datetime] = {}   # dernier instant où ce device a VRAIMENT produit
        self.drain_ema: dict[str, float] = {}    # EMA du soutirage batterie par device (+ = se vide)
        self.chg_accept: dict[str, float] = {}   # charge réellement ACCEPTÉE par device (plafond)
        self.chg_refuse: dict[str, int] = {}     # cycles consécutifs de refus de charge
        self.prod_accept: dict[str, float] = {}  # production réellement LIVRÉE par device (plafond)
        self.prod_refuse: dict[str, int] = {}    # cycles consécutifs de sous-livraison
        self.chg_prev: dict[str, float] = {}     # absorption du cycle précédent (détection de rampe)
        self.prod_prev: dict[str, float] = {}    # livraison du cycle précédent (détection de rampe)
        self.pv_ema: dict[str, float] = {}     # EMA du PV par device (distribution)
        self.lead: dict[str, bool] = {}        # hystérésis sticky par device (décharge batterie)
        self.clead: dict[str, bool] = {}       # hystérésis sticky par device (charge)
        self.floor: dict[str, bool] = {}       # hystérésis de plancher SoC (sticky jusqu'à minSoc+3)
        self.surplus_active = False            # hystérésis du routage de surplus solaire (anti flip-flap)
        self.dir_prev: dict[str, float] = {}   # dernière consigne appliquée (dwell d'inversion)
        self.dir_since: dict[str, datetime | None] = {}  # instant du 1er cycle d'inversion demandée (dwell en SECONDES)
        self.cmd_applied: dict[str, int] = {}  # dernière consigne RÉELLEMENT appliquée (slew-rate)
        # Consigne envoyée au device (signée : + décharge / − charge). Lue par simulation.csv et par
        # le watchdog (choix d'une commande de réveil DIFFÉRENTE). Vit ici : c'est le moteur qui commande.
        self.cmd_target: dict[str, int] = {}
        self._cmd_ent: dict[str, object] = {}
        self.debug = ""                        # queue de ligne pour simulation.csv
        self._par_prev = ""                    # dernier instantané des paramètres EFFECTIFS (cf. _params)
        self._imp_written: dict[str, datetime] = {}  # dernier envoi inverseMaxPower par device (anti-flash)
        # Comportement producteur plein : False=STORE (stocke le surplus dans les batteries, écrête
        # le reste) ; True=BLOCK (rien ne sort, maison sur réseau). Le mapping vers un mode gridReverse
        # sera câblé plus tard (#1 = nommer les modes). Défaut STORE (le cas utile, validé).
        self._socfull_block = False

    def _is_producer(self, d: ZendureDevice, now: datetime) -> bool:
        """Ce device a-t-il des panneaux ? Détection ROBUSTE, indépendante de la consigne courante.

        On ne peut pas répondre avec `solarInput` à l'instant t : un device sans débouché écrête ses
        panneaux, donc la mesure tombe à presque rien précisément quand on lui commande 0. On mémorise
        donc le dernier moment où il a RÉELLEMENT produit, et on considère qu'il reste un producteur
        pendant `PV_MEMORY`. Un onduleur sans PV ne franchira jamais le seuil et ne sera jamais
        promu producteur, quelle que soit la durée.
        """
        return (now - self.pv_seen.get(d.deviceId, datetime.min)).total_seconds() < PV_MEMORY

    def _engage_ok(self, d: ZendureDevice, take: float, me: float, now: datetime) -> bool:
        """Faut-il vraiment réveiller CE device pour CETTE part ?

        Le seuil porte sur la part REÇUE PAR L'APPAREIL, pas sur le besoin total : mesuré le 20/07,
        un besoin médian de 345 W (donc « engagé ») donnait au SolarFlow une part médiane de 86 W,
        parfois 3 W — il démarrait pour rien 95 % du temps. Un onduleur consomme ~50 W pour
        fonctionner : en sortir 86 W est une quasi-perte.

        Un PRODUCTEUR n'est jamais soumis au seuil : son solaire est gratuit, quelle que soit la part.
        Hystérésis : un appareil déjà démarré continue jusqu'à la moitié du seuil, pour ne pas
        s'allumer et s'éteindre en boucle autour de la valeur.
        """
        if me <= 0 or take <= 0 or self._is_producer(d, now):
            return True
        return take >= (me / 2 if abs(self.cmd_applied.get(d.deviceId, 0)) > 0 else me)

    def _chg_ceiling(self, d: ZendureDevice) -> float:
        """Plafond de charge basé sur l'ACCEPTATION MESURÉE, + une marge de ré-exploration.

        Un device peut refuser durablement la charge sans que rien ne l'annonce : le SolarFlow
        tapère au-dessus de ~94 % de SoC et son `ChLim` continue d'afficher −2400 W. Allouer sur
        la limite nominale lui donne tout le budget, et le reliquat part au réseau au lieu d'aller
        au puits suivant.

        ANTI-CLIQUET — le piège de ce genre de plafond : si on ne demande plus que ce qu'il accepte,
        on ne découvre jamais qu'il peut reprendre. D'où la marge `chg_probe` : on offre toujours un
        peu plus que le mesuré. S'il peut, il absorbe plus, l'EMA monte, le plafond suit — remontée
        continue, sans le bang-bang qu'un seuil binaire fabriquerait (cf. 1.4.3.11).
        """
        if (acc := self.chg_accept.get(d.deviceId)) is None:
            return float(-d.charge_limit)
        return min(float(-d.charge_limit), acc + self.chg_probe.asNumber)

    def _bypass_blocks(self, d: ZendureDevice) -> bool:
        """Le bypass disqualifie de l'ABSORPTION les seuls PRODUCTEURS.

        En bypass le firmware arbitre lui-même son PV : sa sortie n'est pas pilotable, donc on
        ne peut pas compter dessus pour distribuer. Mais un device SANS PV n'a rien à arbitrer —
        l'écarter revient à jeter sa capacité de stockage.

        Mesuré le 19/07 : `up` est en bypass sur 51375/51375 lignes avec PV = 0 W. Il est donc
        resté à 0 W, 57 % de place libre (≈4,5 kWh) et 1200 W de capacité, pendant que 446 W
        partaient au réseau en moyenne — le SolarFlow étant saturé à 96 % de SoC (tapering) et
        glagla continuant de produire. Il accepte pourtant une consigne de charge en bypass
        (92 observations, suivi 74 %, l'écart s'expliquant par la rampe de démarrage).

        NB : là où ce test cohabite avec `pv_ema <= 0` (sinks), la garantie « pas de production
        non pilotable » était déjà assurée ; le test bypass n'y retirait qu'un puits valide.
        """
        return d.byPass.asInt > 0 and self.pv_ema.get(d.deviceId, 0.0) > 0

    # Nombre de fois que createEntities() a été exécuté depuis le démarrage du processus HA.
    # Compteur de CLASSE, donc partagé par toutes les instances : c'est le test direct de
    # l'hypothèse « les entités sont créées deux fois et le moteur garde les mauvaises ».
    # Attendu = 1 par chargement d'intégration. Si un simple démarrage affiche déjà 2, la mise en
    # place a été rejouée (retour ConfigEntryNotReady, par exemple) et tout s'explique.
    loads: int = 0

    def createEntities(self) -> None:
        """Paramètres à chaud + capteurs d'observabilité, sur le device Manager."""
        m = self.manager
        FondationEngine.loads += 1
        FondationNumber.trace = []  # le journal de restauration ne concerne que CE chargement
        self._par_prev = ""  # force une ligne `par=` neuve à chaque (re)chargement de l'intégration
        self.db_on = FondationNumber(m, "fondation_db_on", 80, 40, 300, "W")
        self.db_off = FondationNumber(m, "fondation_db_off", 30, 10, 100, "W")
        self.fast_track = FondationNumber(m, "fondation_fast_track", 200, 100, 1000, "W")
        self.hl_alpha = FondationNumber(m, "fondation_hl_alpha", 40, 5, 100, "%")
        # Lissage des MONTANTS (pas seulement du régime). 100 = brut = comportement historique,
        # donc NEUTRE à l'installation. Mesuré 19/07 sur 231 min : 74 % du mouvement de consigne du
        # SolarFlow est de l'aller-retour, et 98 % des échelons >200 W sont annulés par le moteur
        # avant que l'actuateur (2-6 s, mesuré) ait pu les suivre. À 25 %, l'écart-type des variations
        # tombe de 130 W à 35 W pour ~6 s de retard. Le fast-track court-circuite ce filtre.
        self.amt_alpha = FondationNumber(m, "fondation_amt_alpha", 100, 5, 100, "%")
        self.pv_alpha = FondationNumber(m, "fondation_pv_alpha", 20, 5, 100, "%")
        self.step = FondationNumber(m, "fondation_step", 120, 20, 300, "W")
        self.hyst_device = FondationNumber(m, "fondation_hyst_device", 5, 1, 20, "%")
        self.overhead = FondationNumber(m, "fondation_overhead", 50, 0, 150, "W")
        # Routage du surplus solaire d'un producteur vers une batterie sans PV : ça coûte 2 conversions
        # (producteur DC->AC, bus, batterie AC->DC). Sous le seuil d'engagement, on laisse le producteur
        # encaisser lui-même (1 conversion, plus efficace). Hystérésis on/off pour ne pas faire flip-flap.
        # Seuil d'ENGAGEMENT en décharge, symétrique de `surplus_on` côté charge. Un onduleur sans PV
        # consomme ~50 W pour fonctionner : le réveiller pour sortir 50 W est une perte nette. Sert
        # aussi de bascule producteur -> stratégie : tant qu'un producteur soutire moins que ce seuil
        # de SA batterie, on le préfère (son solaire est gratuit) ; au-delà, il n'a plus rien de
        # gratuit à offrir et on repasse par la stratégie de décharge normale. 0 = désactivé.
        self.min_engage = FondationNumber(m, "fondation_min_engage", 300, 0, 1000, "W")
        # Profondeur NÉGATIVE autorisée pour l'intégrale en régime CHARGE (correctif « B »).
        # 0 = ancien comportement (plancher à 0, import permanent incorrigible).
        self.int_neg = FondationNumber(m, "fondation_int_neg", 1200, 0, 3000, "W")
        # Marge de ré-exploration au-dessus de l'acceptation mesurée (cf. `_chg_ceiling`).
        # Très grand (3000) = plafond désactivé, on retombe sur la limite nominale.
        self.chg_probe = FondationNumber(m, "fondation_chg_probe", 150, 50, 3000, "W")
        self.surplus_on = FondationNumber(m, "fondation_surplus_on", 300, 50, 1500, "W")
        self.surplus_off = FondationNumber(m, "fondation_surplus_off", 150, 20, 1200, "W")
        # Dwell d'INVERSION (en SECONDES) : une batterie ne passe pas charge<->décharge sur un
        # transitoire court (micro-ondes, résistance) — elle garde sa consigne tant que le signe
        # opposé n'a pas persisté `dwell_sec` secondes. Évite de fabriquer un export en réagissant
        # un cycle trop tard. 0 = désactivé. Réglable à chaud (ex. automation : lave-linge ON -> monter).
        # ⚠️ Exprimé en TEMPS (25/07), plus en cycles : le compteur de cycles dérivait avec la cadence
        # (timefast/timezero) — 3 cycles valaient 13,5 s à 4,5 s de cadence, 8,1 s à 2,7 s. Défaut 8 s =
        # comportement historique à la cadence courante, désormais STABLE quand la cadence change.
        # (Ancienne entité `fondation_dwell_invert`, en cycles, retirée : elle devient orpheline.)
        self.dwell_sec = FondationNumber(m, "fondation_dwell_sec", 8, 0, 30, "s")
        # DÉBRIDAGE de la limite de décharge d'un SolarFlow (ZenSdk). Le SolarFlow 2400 s'init à
        # 2400 W mais rapporte parfois inverseMaxPower=1200 (device.py:256 rabaisse alors discharge_limit
        # à la moitié) → le moteur ne commandait que 1200 W et importait pendant les pics couvrables.
        # On ré-impose ici la vraie capacité, À CHAQUE cycle (inverseMaxPower la rabaisse entre-temps).
        # 0 = OFF (défaut, aucun changement). ⚠️ On NE se fie PAS à cette valeur : le dérating THERMIQUE
        # éventuel est rattrapé par la MESURE (P1 → intégrale → bascule sur up), jamais par ce nombre.
        self.sf_dismax = FondationNumber(m, "fondation_sf_dismax", 0, 0, 3000, "W")
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
        # SLEW-RATE ASYMÉTRIQUE (« B1 ») : borne la vitesse de variation de la consigne d'un device
        # (W par cycle). Complémentaire du dwell (qui bloque l'INVERSION) : ici on RAMPE l'amplitude.
        #
        # `slew` (MONTÉE, vers +) = vitesse max pour AUGMENTER une décharge. On la bride : sur-décharger
        # sur un transitoire bref (micro-ondes 5 s) créerait un export au relâchement. Anti-overshoot.
        #
        # `slew_down` (DESCENTE, vers −) = vitesse max pour RÉDUIRE une décharge OU AUGMENTER une charge.
        # Les deux réduisent l'export. Mesuré le 25/07 : sur un export de 1500 W (expresso qui s'arrête,
        # glagla livre 1200 W de PV), le SolarFlow devait absorber ~1200 W mais `slew`=500 bridait sa
        # commande à 500 W/cycle → 3 cycles pour engager le puits. L'export (surplus gaspillé au réseau)
        # est plus grave que l'import (payé mais utile) ET réagir vite dans ce sens ne peut pas créer
        # d'overshoot d'export. On libère donc la descente. 0 = illimité (défaut) : la commande atteint
        # sa cible en un cycle, le device reste lissé par sa propre inertie (2-6 s mesurés).
        # L'inversion charge↔décharge reste protégée par `dwell_sec`, donc pas de flip-flop.
        #
        # DÉFAUT = slew (500) => descente = montée = SYMÉTRIQUE = comportement historique EXACT :
        # B1 est livré INACTIF, on l'active en réglant `slew_down` (0 = illimité) depuis l'UI, pour
        # comparer avant/après soi-même. Une modif réversible, activée volontairement.
        self.slew = FondationNumber(m, "fondation_slew", 500, 0, 1200, "W")
        self.slew_down = FondationNumber(m, "fondation_slew_down", 500, 0, 1200, "W")
        # Cadence du moteur (en ms). Ces deux valeurs bridaient le déclenchement en AMONT via des
        # constantes en dur (SmartMode.TIMEFAST/TIMEZERO) partagées avec le moteur smart-matching :
        #   - `timezero` (4000) : intervalle MINIMUM entre deux calculs, même si P1 est stable ;
        #   - `timefast` (2200) : fenêtre après un calcul pendant laquelle AUCUN nouveau calcul n'a
        #     lieu (les P1 sont accumulés puis ignorés) — c'est un plancher, PAS lié à la fréquence
        #     du Shelly. Baisser ces valeurs accélère la boucle, à condition que le temps mort
        #     (commande → P1 réagit) et le temps de cycle (cf. champ `ms=` du debug) le permettent.
        # En mode fondation, `manager._p1_changed` lit CES valeurs au lieu des constantes amont.
        self.timefast = FondationNumber(m, "fondation_timefast", 2200, 300, 10000, "ms")
        self.timezero = FondationNumber(m, "fondation_timezero", 4000, 300, 10000, "ms")

    def createDeviceEntities(self) -> None:
        """Capteur de consigne par onduleur. À appeler APRÈS le chargement des devices.
        C'est LE graphe de diagnostic : commande vs réalisé (outputHomePower)."""
        for d in self.manager.devices:
            if d.deviceId not in self._cmd_ent:
                self._cmd_ent[d.deviceId] = ZendureSensor(d, "cmdTarget", None, "W", "power", "measurement", state=0)

    def cmd_of(self, d) -> int:
        """Dernière consigne envoyée à ce device (0 si le moteur ne l'a pas encore piloté)."""
        return self.cmd_target.get(d.deviceId, 0)

    async def update(self, p1: int) -> None:
        """Un cycle de régulation. Appelé par powerChanged quand operation == FONDATION."""
        # CHRONO : temps de CALCUL pur (jusqu'à _apply_and_report) vs temps d'I/O (envoi des
        # commandes). Répond à « d'où viennent les ~600 ms ? » : le calcul doit être <10 ms, l'I/O
        # (httpGet du SolarFlow en amont + envoi des consignes) est le vrai coût. Affiché dans `ms=`.
        self._t0 = perf_counter()
        # fuseGrp n'est pas assigné pour un device hors fusegroup (annotation sans valeur
        # dans device.py) -> l'exclure du moteur au lieu de crasher sur les caps.
        devices: list[ZendureDevice] = [d for d in self.manager.devices if d.state != DeviceState.OFFLINE and getattr(d, "fuseGrp", None) is not None]
        if not devices:
            return

        now = datetime.now()

        # DÉBRIDAGE SolarFlow (cf. déclaration du paramètre). DEUX niveaux :
        # 1) VUE INTERNE du moteur, ré-imposée à chaque cycle (un message inverseMaxPower a pu rabaisser
        #    discharge_limit entre-temps) : discharge_limit (aussi le clamp de power_discharge) ET
        #    fuseGrp.maxpower (le fusegroup solo plafonne à min(maxpower, discharge_limit)).
        # 2) LE DEVICE lui-même : le cloud lui re-pousse inverseMaxPower (~800 W) TOUTES LES ~6 s, vu
        #    sur le MQTT (le HEMS l'éviterait mais entrerait en conflit avec l'intégration). Débrider la
        #    seule vue interne ne sert à rien si le device plafonne sa sortie réelle. On RÉ-ÉCRIT donc
        #    inverseMaxPower sur le device quand il le rapporte sous la cible — throttle 5 s pour suivre
        #    le cloud (6 s) sans spammer. Le fait que le cloud réécrive à 6 s prouve que la propriété est
        #    VOLATILE (RAM), pas du flash : la réécrire souvent est donc sans risque d'usure.
        #    Le dérating THERMIQUE reste géré par la mesure : on lève la limite arbitraire du cloud, pas
        #    la protection thermique du device.
        if (sf_dis := int(self.sf_dismax.asNumber)) > 0:
            for d in devices:
                if isinstance(d, ZendureZenSdk):
                    d.discharge_limit = sf_dis
                    d.fuseGrp.maxpower = max(d.fuseGrp.maxpower, sf_dis)
                    imp = d.entities.get("inverseMaxPower")
                    if imp is not None and getattr(imp, "asInt", sf_dis) < sf_dis:
                        last = self._imp_written.get(d.deviceId)
                        if last is None or (now - last).total_seconds() > 5:
                            self._imp_written[d.deviceId] = now
                            await d.doCommand({"properties": {"inverseMaxPower": sf_dis}})

        # --- diagnostic : AVANT de recalculer, on confronte la mesure courante à la consigne
        # encore en vigueur. Ne corrige rien, se contente de nommer un device qui n'obéit pas.
        if (diag := getattr(self.manager, "diag", None)) is not None:
            diag.update(devices, p1, now)

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
            # Mémoire « ce device a des panneaux » : une production réelle fait foi durablement,
            # car l'absence de production ne prouve rien (elle peut venir de notre propre consigne).
            if solar > PV_SEEN_MIN:
                self.pv_seen[d.deviceId] = now
            # Soutirage batterie (+ = se vide). Seule mesure NON suppressible par la consigne, donc
            # le seul critère fiable pour savoir si un producteur donne du gratuit ou puise sa réserve.
            drain = float(d.batteryOutput.asInt - d.batteryInput.asInt)
            dprev = self.drain_ema.get(d.deviceId)
            self.drain_ema[d.deviceId] = drain if dprev is None else palpha * drain + (1.0 - palpha) * dprev

            # ACCEPTATION EN CHARGE : ce que le device absorbe VRAIMENT quand on lui demande de
            # charger. PAS d'EMA ici — une moyenne mettrait ~50 s à descendre, soit exactement le
            # délai qu'on cherche à supprimer (mesuré le 23/07 : 50 s avant que up prenne le relais).
            #
            #   il SUIT   -> on retient le max observé : le plafond remonte tout de suite ;
            #   il REFUSE -> après CHG_REFUSE_N cycles consécutifs (le temps d'écarter une simple
            #                rampe de démarrage), on descend D'UN COUP sur le mesuré.
            #
            # Confirmer sur plusieurs cycles est ce qui évite de brimer un device en pleine montée ;
            # snapper ensuite est ce qui rend le débordement immédiat.
            # PRODUCTION RÉELLEMENT LIVRÉE (miroir de chg_accept, côté sortie). Même règle : on
            # retient le max quand il suit, on descend d'un coup après CHG_REFUSE_N refus confirmés.
            out_asked = float(max(0, self.cmd_applied.get(d.deviceId, 0)))
            out_real = float(max(0, d.homeOutput.asInt - d.homeInput.asInt))
            # Même discriminant rampe/refus que côté charge (cf. commentaire ci-dessous).
            up = out_real > self.prod_prev.get(d.deviceId, 0.0) + RAMP_RISE
            self.prod_prev[d.deviceId] = out_real
            if out_asked >= CHG_REFUSE_MIN and not up and out_real < min(out_asked - 100, out_asked * 0.75):
                n = self.prod_refuse.get(d.deviceId, 0) + 1
                self.prod_refuse[d.deviceId] = n
                if n >= CHG_REFUSE_N:
                    self.prod_accept[d.deviceId] = min(self.prod_accept.get(d.deviceId, float("inf")), out_real)
            else:
                self.prod_refuse[d.deviceId] = 0
                if out_asked > 50:
                    self.prod_accept[d.deviceId] = max(self.prod_accept.get(d.deviceId, 0.0), out_real)

            asked = float(max(0, -self.cmd_applied.get(d.deviceId, 0)))
            absorbed = float(max(0, d.homeInput.asInt - d.homeOutput.asInt))
            rising = absorbed > self.chg_prev.get(d.deviceId, 0.0) + RAMP_RISE
            self.chg_prev[d.deviceId] = absorbed
            # ⚠️ UNE RAMPE MONTE, UN REFUS STAGNE. C'est le seul discriminant fiable.
            # Version précédente : `absorbed < asked - 100`, un seuil ABSOLU. Réglé sur des consignes
            # de 1000-2400 W, il devient absurde à 200 W : au petit matin un appareil en pleine
            # montée est forcément 100 W sous la consigne, donc lu comme « refus ». On snappait le
            # plafond, la consigne s'effondrait, la marge de 150 W la faisait remonter, l'appareil
            # suivait, le plafond remontait — et on recommençait. Bang-bang de période 15-30 s,
            # exactement celui que ce plafond était censé éviter.
            # On exige donc trois conditions pour conclure au refus : une consigne assez GRANDE pour
            # que la mesure ait un sens, un manque à la fois absolu ET relatif, et surtout une
            # absorption qui NE MONTE PLUS.
            if asked >= CHG_REFUSE_MIN and not rising and absorbed < min(asked - 100, asked * 0.75):
                n = self.chg_refuse.get(d.deviceId, 0) + 1
                self.chg_refuse[d.deviceId] = n
                if n >= CHG_REFUSE_N:
                    self.chg_accept[d.deviceId] = min(self.chg_accept.get(d.deviceId, float("inf")), absorbed)
            else:
                self.chg_refuse[d.deviceId] = 0
                if asked > 50:
                    self.chg_accept[d.deviceId] = max(self.chg_accept.get(d.deviceId, 0.0), absorbed)

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
        # ⚠️ `forced` ne vaut QUE pour un device qu'on ne pilote PAS.
        #
        # AUTORISÉ (gridReverse=1) : le firmware ignore la consigne et déverse tout — mesuré, 719 W
        # sortis pour une consigne de 0. On ne peut que le constater : il compte dans `forced` et il
        # est EXCLU de l'étape 1 (le commander ne servirait à rien).
        #
        # DÉSACTIVÉ (0) / INTERDIT (2) : depuis la 1.4.3.5 la consigne est réellement transmise et
        # l'appareil OBÉIT. Sa production n'est donc plus « forcée » : elle est commandée à l'étape 1.
        # La compter ici AUSSI la comptait DEUX FOIS — mesuré le 20/07 : forced 386 W + consigne
        # 407 W = 793 W attendus de glagla, pour 365 W réellement sortis. Les 429 W de trop étaient
        # retirés de la demande pilotable, si bien que la consigne de glagla restait TOUJOURS sous son
        # PV : sa batterie ne pouvait structurellement jamais servir, et c'est le SolarFlow qui vidait
        # la sienne à sa place.
        forced = 0.0
        for d in devices:
            if d.state != DeviceState.SOCFULL or self.pv_ema[d.deviceId] <= 0:
                continue
            gr = d.entities.get("gridReverse")
            if (getattr(gr, "value", None) == 1) if gr is not None else False:
                forced += max(0.0, self.pv_ema[d.deviceId] - ovh)
        t_raw = hl_raw - forced

        # --- split-EMA : le régime décide sur le LISSÉ, les montants sur le BRUT ---
        alpha = max(0.05, min(1.0, self.hl_alpha.asNumber / 100.0))
        self.hl_ema = t_raw if self.hl_ema is None else alpha * t_raw + (1.0 - alpha) * self.hl_ema
        t_reg = self.hl_ema
        db_on = self.db_on.asNumber
        db_off = self.db_off.asNumber
        ft = self.fast_track.asNumber

        # --- lissage des MONTANTS, avec BYPASS fast-track ---
        # Le régime décide sur hl_ema, les montants sur t_amt. Au-delà de ±ft (vrai gros saut
        # d'énergie), on recale le filtre sur le brut et on l'utilise tel quel : un gros saut passe
        # INTACT et immédiatement, seul le bruit est filtré. C'est la différence avec le slew-rate,
        # qui lui écrête aussi les vrais sauts.
        a_amt = max(0.05, min(1.0, self.amt_alpha.asNumber / 100.0))
        if a_amt >= 1.0 or abs(t_raw) > ft:
            self.amt_ema = t_raw
        else:
            self.amt_ema = t_raw if self.amt_ema is None else a_amt * t_raw + (1.0 - a_amt) * self.amt_ema
        t_amt = self.amt_ema

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
        # VIDANGE : modérée (2×step) sur un import résiduel ; RAPIDE (proportionnelle à l'export) sur
        # un EXPORT en décharge, où réagir vite prime (cf. correctif B étendu à la décharge, plus bas).
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
                # CORRECTIF « B » ÉTENDU À LA DÉCHARGE (25/07) : sur un export, l'intégrale peut
                # devenir NÉGATIVE (= « décharge moins »). Avant, plancher à 0 : quand la conso
                # retombe plus vite que la boucle (expresso qui s'arrête, device qui revient en
                # déversant), l'intégrale tapait 0 et seul le feed-forward house_load lissé faisait
                # redescendre la décharge. Or house_load est AVEUGLE à un déversement autonome —
                # la sortie de l'appareil s'ajoute dans house_load = P1 + Σhome, qui reste petit
                # pendant qu'on exporte 1000 W. La SEULE mesure qui voit l'export, c'est P1 ;
                # l'intégrale est la seule à en dépendre. Mesuré le 25/07 : retour en 32-35 s sur
                # −1094/−1407 W, sans saturation du slew (0 %) → c'est bien ce plancher qui bloque.
                #
                # DESCENTE PROPORTIONNELLE à l'export (≠ montée, bridée à 2×step) : réduire une
                # décharge qui part au réseau est urgent. À 2×step (60 W/cycle) il faudrait 15-25
                # cycles sur un export franc — inutile. Ici l'intégrale peut suivre l'export en
                # un cycle. ANTI-WINDUP `-dis_base` : elle ne descend pas sous ce qui annule la
                # demande (demand = max(0,t_amt)+integral ≥ 0, jamais d'inversion via l'intégrale ;
                # c'est le RÉGIME qui décide charge/décharge, pas elle). Plancher `int_neg`.
                # int_neg = 0 → plancher à 0 = ancien comportement exact (désactivation propre).
                floor = -self.int_neg.asNumber
                dis_base = max(0.0, t_amt)
                if self.integral > floor:
                    self.integral = max(floor, self.integral - max(step, min(-p1, imax)))
                    self.integral = max(self.integral, -dis_base)
        elif p1 < -db_off and not sat_chg:
            self.integral = min(self.integral + min(-p1, step), imax)
        elif p1 > db_off:
            # CORRECTIF « B » : en CHARGE, l'intégrale peut devenir NÉGATIVE (= « charge moins »).
            # Avant, le plancher à 0 rendait un import PERMANENT structurellement incorrigible : le
            # moteur voyait P1 positif, ne pouvait rien en faire, et l'erreur durait des heures.
            # Mesuré le 23/07 : P1 +266 W pendant 86 min, intégrale à 0 sur 92 % des cycles.
            # Cause de l'import : le moteur dimensionne le puits sur la CONSIGNE d'un producteur,
            # alors que glagla dérate thermiquement au-delà de 62 °C et livre ~300 W de moins.
            # B ne dépend d'aucun diagnostic : la boucle sur P1 rattrape l'écart quelle qu'en soit
            # l'origine (dérating, tapering, décrochage…).
            # Rejeu hors ligne sur 126 min de régime CHARGE (modèle validé à 2 W près sur le réel) :
            #   P1 moyen +168 -> -26 W | import 414 -> 88 Wh | net 353 -> -55 Wh | ±100 W : 27 -> 67 %
            #   intégrale min -584 W, jamais saturée -> pas d'emballement.
            # ANTI-WINDUP symétrique : on n'accumule pas plus bas quand la charge est déjà annulée
            # (l'intégrale ne peut rien de plus), sinon elle plongerait sans effet et mettrait un
            # temps fou à remonter au retour du surplus.
            floor = -self.int_neg.asNumber
            if self.integral > floor and (charge_base := max(0.0, -t_amt)) + self.integral > 0:
                self.integral = max(floor, self.integral - max(step, min(p1, 2 * step)))
                self.integral = max(self.integral, -charge_base)

        # --- consignes (sur la demande CONTRÔLABLE t_amt, pas house_load) ---
        cmd: dict[ZendureDevice, float] = dict.fromkeys(devices, 0.0)
        # IDLE passe aussi par ici : house_load ≈ 0 ne signifie PAS « pas de surplus solaire », mais
        # « bus équilibré » — typiquement parce que les APsystems couvrent la maison ET que le producteur
        # encaisse tout son solaire en interne. Sans ça, en IDLE on commandait 0 -> glagla encaissait
        # 975 W et up ne recevait rien (et le régime flappait sur le bruit P1). En IDLE l'intégrale est
        # nulle et demand ≈ 0 : l'étape 1) ne prend rien, seule l'étape 1bis route le surplus.
        if self.regime in (ManagerState.DISCHARGE, ManagerState.IDLE):
            demand = max(0.0, t_amt) + self.integral
            # 1) LES PRODUCTEURS D'ABORD — on leur demande CE DONT LA MAISON A BESOIN.
            #
            # ⚠️ On ne dimensionne PLUS sur `pv_ema`. `solarInput` ment : un device sans débouché
            # ÉCRÊTE SES PROPRES PANNEAUX. Mesuré le 20/07 sur glagla, même soleil, même appareil :
            # 773 W de PV en gridReverse « autorisé », 152 W en « interdit ». La mesure est donc
            # écrasée par la commande qui s'en servait — le moteur commandait 0, la production
            # s'effondrait, il en déduisait qu'il n'y avait rien à prendre, et il commandait 0.
            # Boucle qui se mord la queue, et du solaire gratuit jeté pendant qu'on vidait une
            # batterie pour compenser.
            #
            # On demande donc au producteur ce dont on a besoin et c'EST LUI qui arbitre : il sort
            # son solaire, et complète avec sa batterie s'il n'a pas assez. Le flux batterie, lui,
            # n'est PAS suppressible par la consigne — c'est la seule mesure fiable ici. Tant qu'il
            # soutire moins que `min_engage`, son apport reste majoritairement gratuit et on le
            # préfère ; au-delà, il n'a plus d'avantage sur les autres et on rend la main à l'étape 2.
            #
            # Les SOCFULL participent désormais : les exclure était valable tant que le firmware
            # déversait tout seul (garde-fou du `continue` supprimé en 1.4.3.5), plus maintenant.
            me = self.min_engage.asNumber
            fuse_p1: dict[object, float] = {}  # budget fusegroup consommé dès l'étape 1
            for d in devices:
                if not self._is_producer(d, now):
                    continue
                # En AUTORISÉ le firmware ignore la consigne : le commander ne sert à rien, et sa
                # production est déjà comptée dans `forced`. L'inclure ici la compterait deux fois.
                gr = d.entities.get("gridReverse")
                if (getattr(gr, "value", None) == 1) if gr is not None else False:
                    continue
                # Même règle qu'en contrôle direct : son solaire + au plus `me` de batterie. On
                # PLAFONNE, on ne conditionne pas sur `drain_ema` — conditionner sur une grandeur que
                # la commande elle-même détermine fabrique un bang-bang (cf. 20:28 le 20/07).
                used = fuse_p1.get(d.fuseGrp, 0.0)
                ceiling = max(0.0, self.pv_ema[d.deviceId] - ovh) + (me if me > 0 else demand)
                take = max(0.0, min(demand, ceiling, float(d.discharge_limit), float(d.fuseGrp.maxpower) - used))
                cmd[d] = take
                demand -= take
                fuse_p1[d.fuseGrp] = used + take

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
                # Même plafond de LIVRAISON MESURÉE que dans la branche CHARGE (`prod_accept`) :
                # `pv_ema` seul surestime ce qu'un producteur dérate thermiquement va sortir.
                unused = {
                    d: min(
                        max(0.0, self.pv_ema[d.deviceId] - ovh - cmd[d]),
                        max(0.0, acc + PROD_PROBE - cmd[d]) if (acc := self.prod_accept.get(d.deviceId)) is not None else float("inf"),
                    )
                    for d in devices
                    if d.state != DeviceState.SOCFULL
                }
                total_unused = sum(unused.values())
                if total_unused <= 0:
                    self.surplus_active = False
                else:
                    # sinks = batteries SANS solaire (donc pas les producteurs) qui ont de la place
                    sinks = [
                        d
                        for d in devices
                        if (self.pv_ema[d.deviceId] - ovh) <= 0 and d.state != DeviceState.SOCFULL and not self._bypass_blocks(d) and d.electricLevel.asInt < 100
                    ]
                    if self.charge_strategy.value == 2:  # fixed_order : le plus gros d'abord
                        sinks.sort(key=lambda d: -d.kWh)
                    else:  # plus vide d'abord
                        sinks.sort(key=lambda d: d.electricLevel.asInt)
                    fuse_chg: dict[object, float] = {}

                    def chg_room(d: ZendureDevice) -> float:
                        # JUMEAU de `chg_cap` (branche CHARGE) : même plafond d'ACCEPTATION MESURÉE.
                        # Le 23/07 à 17:33 il manquait ICI, et seulement ici : le SolarFlow remonté
                        # à 98 % de SoC n'acceptait plus que ~150 W, mais `-d.charge_limit` valait
                        # toujours 2400. Trié en premier (le plus gros), il vidait `rem` sur le
                        # papier ; up, avec 55 % de place libre, ne recevait rien et 750 W partaient
                        # au réseau pendant 3 minutes. En régime CHARGE le même épisode se corrigeait
                        # (`upCmd -323`), en IDLE non (`upCmd 0`) — la trace montrait l'alternance.
                        # Deux fonctions font le même travail dans deux branches : patcher l'une sans
                        # l'autre ne corrige qu'un régime sur deux.
                        used = fuse_chg.get(d.fuseGrp, 0.0)
                        return max(0.0, min(-d.charge_limit, -d.fuseGrp.minpower - used, self._chg_ceiling(d)))

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
            # Le SEUIL D'ENGAGEMENT est appliqué plus bas, sur la PART de chaque appareil
            # (cf. `_engage_ok`) — le tester ici sur le besoin total était le bug de la 1.4.3.6.
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
                    if not self._engage_ok(d, take, me, now):
                        continue
                    cmd[d] += take
                    demand -= take
                    fuse_used[d.fuseGrp] = fuse_used.get(d.fuseGrp, 0.0) + take
                for d in batt:
                    take = min(demand, dis_cap(d))
                    if not self._engage_ok(d, take, me, now):
                        continue
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
                    if not self._engage_ok(d, take, me, now):
                        continue
                    cmd[d] += take
                    demand -= take
                    fuse_used[d.fuseGrp] = fuse_used.get(d.fuseGrp, 0.0) + take
            for d in devices:
                ns = max(0.0, self.pv_ema[d.deviceId] - ovh)
                self.lead[d.deviceId] = (cmd[d] - ns) > 5
                self.clead[d.deviceId] = False
        elif self.regime == ManagerState.CHARGE:
            # Une intégrale négative RÉDUIT la charge, elle ne doit jamais l'INVERSER en décharge :
            # `rem` est un montant de charge, un `rem` négatif ferait `cmd[d] -= rem` donc sortir.
            rem = max(0.0, max(0.0, -t_amt) + self.integral)
            # SOCFULL exclu (c'est LUI qui déverse le surplus qu'on absorbe) ;
            # bypass exclu SEULEMENT s'il produit (cf. _bypass_blocks) : un device sans PV
            # en bypass reste un puits parfaitement valide.
            cand = [d for d in devices if d.state != DeviceState.SOCFULL and not self._bypass_blocks(d)]
            fuse_used = {}

            def chg_cap(d: ZendureDevice) -> float:
                # place de charge ENCORE disponible : min(marge device, marge fusegroup, ACCEPTATION
                # MESURÉE). BUG corrigé (même famille que dis_cap) : `used` inclut déjà -cmd[d] ;
                # l'ancienne formule `min(limit, minpower-used) + cmd[d]` resoustrayait -> place
                # sous-estimée dès la 2e passe (parallel) et pour tout usage après le bus.
                #
                # `chg_accept` est le 3e plafond : ce que le device ABSORBE réellement. Sans lui, le
                # moteur alloue sur `charge_limit` NOMINAL (-2400 pour le SolarFlow) alors qu'en
                # tapering il n'accepte plus que ~380 W. Mesuré le 23/07 à 16:18 : SolarFlow à 94 %
                # de SoC, commandé 2296 W, il en absorbe 374 ; les 900 W de surplus sont partis au
                # réseau pendant 50 s alors que up avait 77 % de place libre et 1200 W de capacité.
                # Le plafonner ici fait DÉBORDER le reliquat sur le device suivant dès le 1er cycle
                # (`rem` n'est décrémenté que du `take` réellement alloué).
                used = fuse_used.get(d.fuseGrp, 0.0)
                return max(0.0, min(-d.charge_limit + cmd[d], -d.fuseGrp.minpower - used, self._chg_ceiling(d)))

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
                    # ⚠️ On plafonne sur ce que le producteur LIVRE, pas sur son `pv_ema`.
                    # Mesuré le 23/07 à 16:45 : pv_ema 1298 W -> le moteur route 1200 W vers up,
                    # mais glagla n'en livre que 847 (dérating thermique à 64 °C + charge de sa
                    # propre batterie à 37 % de SoC). up absorbe bien 1200, les 220 W manquants
                    # sont pris au RÉSEAU. `overhead` à 150 (son maximum) n'en corrigeait que la
                    # moitié — et une constante ne peut pas convenir, l'écart valant 0 W à froid
                    # et ~320 W à 64 °C.
                    #
                    # ASYMÉTRIE VOULUE avec `_chg_ceiling` : offrir 150 W de trop à un PUITS est
                    # sans conséquence (il ne les prend pas), alors qu'en demander 150 de trop à un
                    # PRODUCTEUR fabrique directement de l'import. D'où une marge de ré-exploration
                    # bien plus petite ici (PROD_PROBE).
                    spare = max(0.0, self.pv_ema[d.deviceId] - ovh - cmd[d])
                    if (acc := self.prod_accept.get(d.deviceId)) is not None:
                        spare = min(spare, max(0.0, acc + PROD_PROBE - cmd[d]))
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
        # Fin du CALCUL, début de la préparation + I/O (envoi des consignes plus bas).
        self._ms_calc = (perf_counter() - self._t0) * 1000.0
        t_io = perf_counter()
        # --- DWELL D'INVERSION (en SECONDES) : pas de charge<->décharge sur un transitoire court ---
        # Une charge qui pulse ~5-10 s (micro-ondes en décongélation, résistance) est plus rapide que
        # la boucle : la correction arrive quand l'impulsion est finie -> on FABRIQUE un export (mesuré
        # terrain : 59 % de l'export l'était pendant que up déchargeait, 28 inversions en 10 min). On
        # tient donc la consigne courante tant que le signe opposé n'a pas persisté `dwell_sec` secondes.
        # DÉLAI EN TEMPS (et non en cycles) : on mémorise l'INSTANT de la première demande d'inversion,
        # ce qui rend le comportement indépendant de la cadence (cf. la déclaration du paramètre).
        dwell_s = self.dwell_sec.asNumber
        if dwell_s > 0:
            now_d = datetime.now()
            for d in devices:
                prev = self.dir_prev.get(d.deviceId, 0.0)
                if prev * cmd[d] < 0:  # inversion demandée
                    since = self.dir_since.get(d.deviceId)
                    if since is None:
                        self.dir_since[d.deviceId] = now_d
                        cmd[d] = prev  # on tient la consigne courante (début du dwell)
                    elif (now_d - since).total_seconds() < dwell_s:
                        cmd[d] = prev  # on tient encore
                    else:
                        self.dir_since[d.deviceId] = None  # délai écoulé -> inversion autorisée
                else:
                    self.dir_since[d.deviceId] = None
                self.dir_prev[d.deviceId] = cmd[d]

        # --- SLEW-RATE ASYMÉTRIQUE : rampe l'amplitude de la consigne (W/cycle) vs la dernière appliquée.
        # MONTÉE (vers + = plus de décharge) bridée par `slew` (anti-overshoot d'export sur transitoire
        # bref). DESCENTE (vers − = moins de décharge / plus de charge, donc RÉDUIRE l'export) bridée par
        # `slew_down`, plus rapide voire illimitée (0). N'empêche pas l'inversion (rôle du dwell).
        slew_up = int(self.slew.asNumber)
        slew_dn = int(self.slew_down.asNumber)
        for d in devices:
            prev = self.cmd_applied.get(d.deviceId)
            if prev is None:
                continue
            hi = prev + slew_up if slew_up > 0 else float("inf")   # plafond de MONTÉE
            lo = prev - slew_dn if slew_dn > 0 else float("-inf")  # plancher de DESCENTE (0 = illimité)
            cmd[d] = max(lo, min(hi, cmd[d]))

        # --- application (gardes reprises du moteur 1.4.2 : bypass non stoppé, offgrid maintenu) ---
        setpoint = 0
        for d in devices:
            c = int(cmd[d])
            self.cmd_applied[d.deviceId] = c  # mémorise pour le slew-rate du prochain cycle
            self.cmd_target[d.deviceId] = c
            if (ent := self._cmd_ent.get(d.deviceId)) is not None:
                ent.update_value(c)
            setpoint += c
            if c > 0:
                await d.power_discharge(c)
            elif c < 0:
                await d.power_charge(c)
            elif d.byPass.asInt > 0 and max(0, d.pwr_offgrid) > 0:
                # On ne saute l'envoi QUE si une sortie AC de secours en dépend : la couper serait
                # pire que de laisser passer le bypass. C'était l'intention du garde-fou d'origine.
                continue
            else:
                # BUG (mesuré 20/07) : l'ancien garde-fou sautait l'envoi dès que byPass > 0, donc la
                # consigne 0 n'était JAMAIS transmise à un device en bypass. Il restait figé sur sa
                # dernière consigne non nulle — glagla obéissait à 344 puis 156, puis gardait 155 W
                # indéfiniment pendant que son PV variait, en exportant. Le code n'a pas changé : un
                # Hyper active son bypass en SOCFULL, situation devenue permanente quand tout le parc
                # a atteint 100 %. Le zéro doit partir.
                await d.power_discharge(0 if max(0, d.pwr_offgrid) == 0 else 10)

        # --- observabilité ---
        self.sensor_state.update_value(self.regime.value)
        self.sensor_houseload.update_value(int(hl_raw))
        self.sensor_forced.update_value(int(forced))
        self.sensor_integral.update_value(int(self.integral))
        self.sensor_setpoint.update_value(setpoint)
        self.manager.operationstate.update_value(self.regime.value)
        self.manager.setpoint = setpoint
        # CHRONO : calc = calcul pur du moteur ; io = envoi des consignes (I/O) ; cyc = cycle
        # complet mesuré côté manager (inclut le httpGet du SolarFlow, donc get ≈ cyc − calc − io).
        # `cyc` a un cycle de retard (mesuré après le retour de update). Tous en ms, arrondis.
        ms_io = (perf_counter() - t_io) * 1000.0
        ms = f" ms={getattr(self.manager, 'ms_cycle', 0.0):.0f}cyc/{self._ms_calc:.1f}calc/{ms_io:.1f}io"
        self.debug = (
            f"fondation regime={self.regime.name} hl={int(hl_raw)} forced={int(forced)} T={int(t_raw)}"
            f" ema={int(t_reg)} amt={int(self.amt_ema) if self.amt_ema is not None else 0}"
            f" int={int(self.integral)} sp={setpoint}"
            f" prod={'/'.join(f'{int(self.drain_ema.get(d.deviceId, 0))}' for d in devices if self._is_producer(d, datetime.now())) or '-'}"
            f" acc={'/'.join(f'{int(self.chg_accept[d.deviceId])}' for d in devices if d.deviceId in self.chg_accept) or '-'}"
            f" liv={'/'.join(f'{int(self.prod_accept[d.deviceId])}' for d in devices if d.deviceId in self.prod_accept) or '-'}"
            f" strat={self.discharge_strategy.value}/{self.charge_strategy.value}{ms}"
            f"{self._params()}"
        )
        _LOGGER.info(
            "Fondation => p1:%s house_load:%s regime:%s forced:%s integral:%s setpoint:%s",
            p1, int(hl_raw), self.regime.name, int(forced), int(self.integral), setpoint,
        )

    def _params(self) -> str:
        """Paramètres EFFECTIFS — ceux que le moteur applique vraiment, émis UNIQUEMENT s'ils changent.

        Le CSV est le seul journal durable de cette installation (pas de home-assistant.log conservé).
        Sans ce champ, un paramètre affiché dans HA mais non appliqué par le moteur est INDÉTECTABLE
        autrement qu'en déduisant sa valeur du comportement — c'est ce qui a coûté quatre jours en
        juillet 2026. On ne l'écrit qu'au changement : une ligne complète à chaque cycle
        représenterait ~1,5 Mo par jour pour une information quasi constante.

        ⚠️ C'est une SONDE, pas un correctif. Elle est posée en premier, seule, pour établir ce que
        le moteur applique réellement en fonction de ce qu'on lui fait subir (redémarrage de HA,
        rechargement de l'intégration, modification en direct depuis l'UI). Tant que ce comportement
        n'est pas caractérisé, aucun contournement ne doit être écrit : le mécanisme de restauration
        de `FondationNumber` est censé faire ce travail, et on ne sait pas encore pourquoi il échoue.
        """
        snap = (
            f"db{self.db_on.asNumber}/{self.db_off.asNumber} ft{self.fast_track.asNumber}"
            f" hla{self.hl_alpha.asNumber} amta{self.amt_alpha.asNumber} pva{self.pv_alpha.asNumber}"
            f" stp{self.step.asNumber} hyd{self.hyst_device.asNumber} ovh{self.overhead.asNumber}"
            f" slw{self.slew.asNumber}/{self.slew_down.asNumber} eng{self.min_engage.asNumber} ing{self.int_neg.asNumber}"
            f" cpr{self.chg_probe.asNumber} sur{self.surplus_on.asNumber}/{self.surplus_off.asNumber}"
            f" dws{self.dwell_sec.asNumber} sfd{self.sf_dismax.asNumber} tf{self.timefast.asNumber}/{self.timezero.asNumber}"
            f" load{FondationEngine.loads}/{id(self) & 0xFFFF:04x}"
        )
        split = self._split()
        # ⚠️ Le journal de restauration force l'émission. Sans ça il restait invisible : il se
        # remplit APRÈS le premier cycle du moteur (les entités sont ajoutées dans une tâche, plus
        # tard), et comme la restauration échoue les valeurs ne bougent pas — donc l'instantané ne
        # changeait jamais et la ligne n'était jamais écrite. Bogue de la sonde du 24/07, 21h.
        if snap + split == self._par_prev and not FondationNumber.trace:
            return ""
        self._par_prev = snap + split
        # Journal de restauration : écrit UNE fois puis vidé (il ne concerne que le chargement).
        rest = " restore=[" + " | ".join(FondationNumber.trace) + "]" if FondationNumber.trace else ""
        FondationNumber.trace = []
        _LOGGER.warning("Fondation paramètres effectifs : %s%s%s", snap, split, rest)
        return f" par=[{snap}]{split}{rest}"

    def _param_entities(self) -> list[tuple[str, FondationNumber]]:
        """Les paramètres réglables, sous forme (étiquette courte, entité)."""
        return [
            ("db_on", self.db_on), ("db_off", self.db_off), ("ft", self.fast_track),
            ("hl_a", self.hl_alpha), ("amt_a", self.amt_alpha), ("pv_a", self.pv_alpha),
            ("step", self.step), ("hyst", self.hyst_device), ("ovh", self.overhead),
            ("slew", self.slew), ("slew_dn", self.slew_down), ("eng", self.min_engage),
            ("int_neg", self.int_neg), ("chg_probe", self.chg_probe), ("sur_on", self.surplus_on),
            ("sur_off", self.surplus_off), ("dwell", self.dwell_sec),
            ("sf_dismax", self.sf_dismax), ("timefast", self.timefast), ("timezero", self.timezero),
        ]

    def _split(self) -> str:
        """DIAGNOSTIC : l'objet que lit le moteur est-il celui que Home Assistant affiche ?

        Constat du 24/07 : après un rechargement, l'UI montrait fast_track 700 / amt_alpha 30 /
        step 30 pendant que le moteur appliquait 200 / 100 / 120. Trois sources (UI, moteur,
        core.restore_state) donnaient trois réponses différentes, sans qu'aucune lecture de fichier
        ne puisse les départager. On fait donc poser la question au code lui-même, à chaque cycle.

        Pour chaque paramètre en désaccord on écrit :
          `nom moteur=X ui=Y plat=ÉTAT id=XXXX`
          - `moteur` : ce que renvoie `asNumber`, donc ce qui pilote réellement les batteries ;
          - `ui`     : l'état publié sous l'`entity_id` de CETTE instance ;
          - `plat`   : `NOT_ADDED` prouverait que l'objet du moteur n'a jamais été rattaché à une
                       plateforme — donc qu'un second objet, fantôme, sert l'interface ;
          - `id`     : identité mémoire (16 bits), pour voir si elle change d'un chargement à l'autre.

        Champ vide = les deux coïncident, il n'y a PAS de dédoublement et la cause est ailleurs.
        """
        out = []
        for name, ent in self._param_entities():
            eid = getattr(ent, "entity_id", None)
            st = self.manager.hass.states.get(eid) if eid else None
            shown = st.state if st is not None else "-"
            try:
                if st is not None and float(shown) == float(ent.asNumber):
                    continue  # d'accord : rien à signaler
            except (TypeError, ValueError):
                pass  # "unknown"/"unavailable" : on signale aussi
            plat = getattr(getattr(ent, "_platform_state", None), "name", "?")
            # `restored=True` = état FANTÔME fabriqué par Home Assistant à partir du registre pour
            # une entité pas encore fournie, et non une valeur publiée par notre objet. Ça
            # expliquerait le ui=700 impossible : personne ne l'aurait écrit, il survivrait au
            # redémarrage parce qu'il vient du registre.
            ghost = bool(st.attributes.get("restored")) if st is not None else False
            out.append(f"{name} moteur={ent.asNumber} ui={shown} fantome={ghost} plat={plat} id={id(ent) & 0xFFFF:04x}")
        return " split=[" + " | ".join(out) + "]" if out else " split=[]"

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
            sinks = [d for d in devices if d.state != DeviceState.SOCFULL and not self._bypass_blocks(d) and d.electricLevel.asInt < 100]
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
            # DÉFICIT : le producteur sort son solaire PUIS puise dans sa batterie jusqu'à
            # `min_engage`, et seulement au-delà les autres batteries prennent le relais.
            #
            # ⚠️ On ne plafonne PLUS au solaire mesuré. On ne peut pas connaître le PV disponible
            # d'un appareil tant qu'il n'a pas puisé dans sa batterie : `solarInput` ne mesure que ce
            # qu'on lui permet de produire. Se limiter à `pv_ema - ovh` était donc circulaire, et
            # rendait le soutirage batterie du producteur STRUCTURELLEMENT impossible — mesuré le
            # 20/07 : glagla plafonné à 221 W alors qu'il pouvait fournir 285 W (60 W de solaire
            # perdus), batterie à 0 W, et le SolarFlow vidant la sienne à sa place.
            #
            # On demande donc solaire + une part de batterie ; c'est le firmware qui arbitre. Le
            # soutirage réel (`drain_ema`, non suppressible par la consigne) sert de garde-fou :
            # au-delà de `min_engage` le producteur n'a plus rien de gratuit et on rend la main.
            self.regime = ManagerState.DISCHARGE
            fuse_used = {}
            me = self.min_engage.asNumber
            deficit = max(0.0, base - forced + self.integral)
            for d in full_prod:
                solar = max(0.0, self.pv_ema[d.deviceId] - ovh)
                # Le soutirage batterie est PLAFONNÉ, pas conditionné. Version précédente :
                #     extra = 0 if drain_ema >= me else min(deficit, me)
                # C'était un interrupteur binaire sur une grandeur QUE NOTRE PROPRE COMMANDE
                # DÉTERMINE : au-dessus du seuil on coupait le producteur à son solaire, tout
                # basculait sur les autres batteries, son soutirage retombait, on le réengageait,
                # il repuisait... Bang-bang mesuré le 20/07 à 20:28 : consignes 867/292/0/1000/500/0
                # en 30 s, prod= oscillant 139-366, et le slew courant après une cible qui changeait
                # de camp toutes les 5 s.
                # En plafonnant simplement l'extra à `me`, le soutirage ne peut pas dépasser `me` par
                # construction : le garde-fou devient inutile, et la boucle disparaît avec lui.
                extra = min(deficit, me) if me > 0 else deficit
                cmd[d] = min(solar + extra, float(d.discharge_limit), float(d.fuseGrp.maxpower))
                deficit = max(0.0, deficit - extra)
                fuse_used[d.fuseGrp] = fuse_used.get(d.fuseGrp, 0.0) + cmd[d]
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
