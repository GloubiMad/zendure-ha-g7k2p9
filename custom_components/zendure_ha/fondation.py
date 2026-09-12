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
from datetime import datetime, timedelta
from time import perf_counter

from homeassistant.components import persistent_notification
from homeassistant.components.number import NumberMode
from homeassistant.helpers.restore_state import RestoreEntity

from .button import ZendureButton
from .const import DeviceState, ManagerState
from .device import ZendureDevice, ZendureZenSdk
from .entity import EntityDevice
from .number import ZendureRestoreNumber
from .select import ZendureRestoreSelect
from .sensor import ZendureSensor
from .switch import ZendureSwitch

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

# Durée de validité du plafond de charge mesuré (`_chg_ceiling`), à partir du dernier REFUS confirmé.
#
# ⛔ LE CLIQUET CORRIGÉ EN 1.4.3.48. `chg_accept` est le MAXIMUM OBSERVÉ ; s'en servir en permanence
# comme PLAFOND revient à décréter qu'un appareil ne peut pas faire mieux que ce qu'on a bien voulu
# lui demander. Terrain du 08/08 : `load_max` avait bridé le SolarFlow à 2040 W pendant des jours,
# `chg_accept` a donc appris 2041, et le plafond s'est figé à `2041 + chg_probe` = **2191 W**.
# Remettre `load_max` à 100 n'a RIEN changé : pour apprendre qu'il peut aller à 2400, il aurait
# fallu le lui demander, et le moteur se l'interdisait. Le moteur avait mémorisé sa propre bride
# comme si c'était une limite du matériel.
#
# ⭐ Le tapering, lui, est CONTINU : un appareil qui refuse le refuse encore trois secondes plus
# tard, et la détection de refus (`CHG_REFUSE_N` cycles) le rattrape immédiatement. Un plafond qui
# survit dix minutes à son dernier refus ne perd donc aucune protection — il cesse simplement de
# s'appliquer quand plus rien ne le justifie. Mesuré : 75 refus confirmés sur le SolarFlow en 8
# jours, soit ~6 % du temps sous plafond au lieu de 100 %.
CHG_CAP_TTL = 600.0

# Durée au-delà de laquelle une absorption NON COMMANDÉE cesse d'être un artefact de MESURE
# et devient une charge réellement décidée par le firmware (cf. `_charge_subie`).
#
# ⛔⛔ CE N'EST PAS UNE LATENCE D'APPAREIL. Les Zendure réagissent en moins d'une seconde ; toute
# « lenteur » lue dans une trace vient de la CHAÎNE DE MESURE, jamais du matériel. Ici l'écart
# `Home − Cmd` compare deux grandeurs qui ne datent pas du même instant : `Cmd` est écrite par le
# moteur à l'instant du calcul, tandis que `Home` est la DERNIÈRE VALEUR RAPPORTÉE par l'appareil.
# MESURÉ sur la trace du 03 au 06/08 — les deux distributions sont les mêmes, ce qui le prouve :
#   `Home` du SolarFlow change toutes les 3 s en médiane, p90 6 s   (télémétrie)
#   les « écarts » qu'on mesurait : 3 s en médiane, p90 8 s, max 19 s
# Autrement dit on lisait la période de rafraîchissement, pas une réaction.
#
# La vraie charge subie, elle, se distingue par sa DURÉE : l'épisode de `up` du 06/08 a duré
# **494 s** avec `Cmd` à 0. 30 s écarte donc 100 % du bruit de mesure (max observé 19 s) tout en
# attrapant l'épisode réel dès sa 30e seconde.
SUBIE_MIN = 30.0
# ⛔ 1.4.4.2 — durée d'un export FRANC avant de forcer le routage du surplus (cf. `update`).
# 3 s = 3 cycles : assez pour écarter le bruit de P1 et les rafales d'un nuage, assez peu pour ne
# pas laisser partir un surplus installé. En dessous on ferait démarrer un onduleur pour rien.
EXPORT_DWELL = 3.0

# ⛔ 1.4.4.4 — SEUIL D'ENGAGEMENT POUR PLACER DU SOLAIRE.
# `min_engage_chg` (500 W) protège d'un démarrage d'onduleur pour une part dérisoire : juste pour
# la répartition ORDINAIRE. Mais il porte sur la PART de chaque puits, donc fractionner un surplus
# entre deux batteries pouvait n'en engager AUCUNE — et comme les producteurs ne sortent que ce qui
# a trouvé preneur (`r = alloue`), le solaire restait chez eux et partait au réseau.
# MESURÉ (20→22/08) : 14 251 cycles où glagla produisait > 300 W, n'en plaçait pas ≥ 150 W, alors
# qu'il restait ≥ 300 W de place ailleurs — **3 723 Wh** de gisement. Exemple 11:52:36 : PV 1626 W,
# sortie 697 W, batterie 0, et 2 942 W de place libre chez les deux autres.
# L'user avait dû passer la stratégie en « le plus gros d'abord » pour contourner : avec ce mode le
# SolarFlow est servi en premier, prend tout d'un coup et franchit le seuil. Ça ne doit dépendre
# d'aucun mode.
# ⇒ Pour PLACER DU SOLAIRE uniquement, le seuil par part descend au minimum utile d'un onduleur
# (60 W, cf. saga anti-cycling). Le garde-fou qui compte reste EN AMONT et est inchangé :
# `surplus_on`/`surplus_off` décident s'il vaut la peine de router (300/150 W sur le TOTAL). On ne
# démarre donc jamais pour 60 W isolés — on cesse seulement de bloquer la RÉPARTITION.
SOLAR_ENGAGE = 60.0

# ⏱️ 1.4.4.6 — TROU MAXIMAL DANS LE BESOIN pour que le dwell d'engagement reste CONTINU.
# `_engage_ok` est appelé plusieurs fois par cycle (étapes 1/2/3, charge et décharge) : effacer le
# chrono dès qu'une étape propose une part insuffisante le remettrait à zéro selon l'ORDRE des
# appels. On mémorise donc le dernier instant où le besoin a été constaté, et le chrono ne repart
# que si le besoin a disparu plus de `ENGAGE_GAP`. Cycle ~1 s, données rafraîchies toutes les 2,7 s
# (mesuré) : 5 s laisse deux rafraîchissements de marge sans jamais recoller deux besoins distincts.
ENGAGE_GAP = 5.0


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


class FondationSwitch(ZendureSwitch, RestoreEntity):
    """Interrupteur de CONFIGURATION, restaurable. `ZendureSwitch` ne restaure rien : son état est
    perdu à chaque redémarrage de Home Assistant (c'est ce qui remettait le switch de simulation à
    zéro). Inacceptable pour une propriété matérielle, dont la perte serait silencieuse.

    Même correctif de course que `FondationNumber` : `ZendureSwitch.__init__` se termine par
    `self.add([self])`, qui exécute `async_added_to_hass` AVANT que ce constructeur ait fini.
    On rétablit donc ici ce que la restauration a obtenu. Correct dans les deux sens si la course
    s'inversait un jour.
    """

    def __init__(self, device: EntityDevice, uniqueid: str, default: bool = False) -> None:
        self._default = default
        self._restored: bool | None = None  # doit exister AVANT super() (cf. course ci-dessus)
        super().__init__(device, uniqueid, self._write, None, None, default)
        self._attr_is_on = self._restored if self._restored is not None else default

    def _write(self, _entity: object, value: object) -> None:
        """Bascule depuis l'interface. `ZendureSwitch` délègue tout à `onwrite` : sans ça, cocher
        la case n'aurait aucun effet sur l'état interne."""
        self._attr_is_on = bool(value)
        if self.hass and self.hass.loop.is_running():
            self.schedule_update_ha_state()

    def learn(self) -> None:
        """COCHAGE automatique — jamais de décochage (cf. `FondationEngine._has_pv`)."""
        if not self._attr_is_on:
            _LOGGER.warning("Panneaux détectés sur %s : la case « PV raccordés » est cochée", self.device.name)
            self._write(self, True)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        state = await self.async_get_last_state()
        self._attr_is_on = state.state == "on" if state is not None and state.state in ("on", "off") else self._default
        self._restored = self._attr_is_on
        FondationNumber.trace.append(f"{self.device.name}/PV restaure={self._attr_is_on} lu={'None' if state is None else state.state}")


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
        self.chg_cap_until: dict[str, datetime] = {}  # jusqu'à quand le plafond mesuré vaut (CHG_CAP_TTL)
        self.chg_subie: dict[str, float] = {}    # absorption NON commandée, confirmée (cf. SUBIE_MIN)
        self.subie_since: dict[str, datetime] = {}  # depuis quand elle dure (écarte la latence)
        self.chg_prev: dict[str, float] = {}     # absorption du cycle précédent (détection de rampe)
        self.prod_prev: dict[str, float] = {}    # livraison du cycle précédent (détection de rampe)
        self.chg_wake: dict[str, datetime] = {}  # début de la LATENCE DE DÉMARRAGE en charge
        self.prod_wake: dict[str, datetime] = {} # idem côté production
        self.pv_ema: dict[str, float] = {}     # EMA du PV par device (distribution)
        self.lead: dict[str, bool] = {}        # hystérésis sticky par device (décharge batterie)
        self.clead: dict[str, bool] = {}       # hystérésis sticky par device (charge)
        self.floor: dict[str, bool] = {}       # hystérésis de plancher SoC (sticky jusqu'à minSoc+3)
        self.surplus_active = False            # hystérésis du routage de surplus solaire (anti flip-flap)
        self.export_since: datetime | None = None  # 1.4.4.2 : 1er cycle d'un export FRANC consécutif
        self.occ = 1.0                         # taux d'occupation autorisé du parc (cf. `load_max`)
        self.idle_since: datetime | None = None      # instant d'entrée en IDLE (gel de l'intégrale)
        self.idle_regime: ManagerState | None = None  # régime AVANT l'entrée en IDLE (anti-contamination)
        self.dir_prev: dict[str, float] = {}   # dernière consigne appliquée (dwell d'inversion)
        self.out_capped: float = 0.0  # 1.4.3.52 : W retires par le plafond de sortie au dernier cycle
        self.dir_since: dict[str, datetime | None] = {}  # instant du 1er cycle d'inversion demandée (dwell en SECONDES)
        self.cmd_applied: dict[str, int] = {}  # dernière consigne RÉELLEMENT appliquée (slew-rate)
        self.inflight_since: datetime | None = None  # 1.4.4.5 : début de l'épisode « consigne en vol »
        # 1.4.4.6 : par device, (début du besoin d'engagement, dernière fois qu'il a été constaté)
        self.engage_since: dict[str, tuple[datetime, datetime]] = {}
        # 1.4.5.3 — SONDE : pourquoi un appareil n'a-t-il pas reçu de part ? Rempli à chaque
        # cycle par `_engage_ok` et par la répartition, lu seulement par le message de debug.
        self.engage_why: dict[str, str] = {}
        self.part_why: dict[str, str] = {}
        # Exposés pour `_apply_and_report`, qui est une AUTRE fonction (même motif que `out_capped`) :
        # `_direct_control` y arrive sans passer par le calcul, d'où l'initialisation ici.
        self.inflight: float = 0.0
        self.en_vol: bool = False
        # Consigne envoyée au device (signée : + décharge / − charge). Lue par simulation.csv et par
        # le watchdog (choix d'une commande de réveil DIFFÉRENTE). Vit ici : c'est le moteur qui commande.
        self.cmd_target: dict[str, int] = {}
        self._cmd_ent: dict[str, object] = {}
        self._pv_ent: dict[str, FondationSwitch] = {}  # case « panneaux raccordés », par onduleur
        self.debug = ""                        # queue de ligne pour simulation.csv
        self._par_prev = ""                    # dernier instantané des paramètres EFFECTIFS (cf. _params)
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

    def _engage_ok(self, d: ZendureDevice, take: float, me: float, now: datetime, charge: bool = False) -> bool:
        """Faut-il vraiment réveiller CE device pour CETTE part ?

        Le seuil porte sur la part REÇUE PAR L'APPAREIL, pas sur le besoin total : mesuré le 20/07,
        un besoin médian de 345 W (donc « engagé ») donnait au SolarFlow une part médiane de 86 W,
        parfois 3 W — il démarrait pour rien 95 % du temps. Un onduleur consomme ~50 W pour
        fonctionner : en sortir 86 W est une quasi-perte.

        Un PRODUCTEUR n'est jamais soumis au seuil : son solaire est gratuit, quelle que soit la part.
        Hystérésis : un appareil déjà démarré continue jusqu'à la moitié du seuil, pour ne pas
        s'allumer et s'éteindre en boucle autour de la valeur.

        ⚠️ ANTI-VERROU MUTUEL (27/07/2026) — deux garde-fous se bloquaient l'un l'autre.
        `_chg_ceiling` plafonne sur l'acceptation MESURÉE plus une marge de re-sondage ; ce seuil-ci
        refuse les petites parts. Quand la mesure tombe à zéro, le plafond descend à la seule marge
        (`chg_probe`, 150 W) — donc SOUS le seuil d'engagement. L'appareil ne peut plus être engagé,
        donc plus rien absorber, donc sa mesure ne remonte jamais. Fermé à double tour.

        Constaté sur `up` : `chg_accept` = 0 -> plafond 150 W -> refusé par un seuil de 500 W ->
        3,84 kWh de place inutilisables, pendant que le SolarFlow tapérait à 94 % de SoC et que
        glagla encaissait tout son solaire dans SA batterie (84 % de SoC), à l'inverse de la
        stratégie voulue.

        La marge de re-sondage n'a de sens que si elle reste ATTEIGNABLE : un appareil dont
        l'acceptation mesurée est nulle est donc exempté du seuil, à condition que la part proposée
        atteigne au moins cette marge (on ne réveille pas pour 20 W, on sonde pour de bon).
        Dès qu'il absorbe, la mesure repasse au-dessus de zéro et le seuil s'applique de nouveau.

        Le test est écrit une seule fois pour les DEUX sens : `dis_probe` = 3000 (neutre) rend
        l'exemption inopérante en décharge, ce qui est exactement correct — sans plafond, pas de
        verrou possible.
        """
        if me <= 0 or take <= 0 or self._is_producer(d, now):
            return True
        running = abs(self.cmd_applied.get(d.deviceId, 0)) > 0
        acc = self.chg_accept.get(d.deviceId) if charge else self.prod_accept.get(d.deviceId)
        probe = self.chg_probe.asNumber if charge else self.dis_probe.asNumber
        # Disjonction STRICTEMENT identique aux deux tests d'avant la 1.4.4.6 (exemption anti-verrou,
        # puis seuil avec hystérésis) : elle est seulement mise en facteur pour que le dwell ci-dessous
        # s'applique aux DEUX chemins. Un sondage de 3 s ne sonde rien — mesuré à 0 % de rendement.
        if not ((not acc and take >= probe) or take >= (me / 2 if running else me)):
            # 1.4.5.3 : `seuil` = la part proposée est trop petite pour réveiller l'appareil.
            self.engage_why[d.deviceId] = f"seuil({take:.0f}<{me / 2 if running else me:.0f})"
            return False
        if running:
            self.engage_since.pop(d.deviceId, None)
            self.engage_why.pop(d.deviceId, None)
            return True
        # --- 1.4.4.6 — DWELL D'ENGAGEMENT : un RÉVEIL doit être mérité par la DURÉE du besoin ------
        # Le seuil historique porte sur le MONTANT (« ne pas réveiller pour 86 W »). Il ne dit rien de
        # la DURÉE, et c'est l'autre moitié du problème : réveiller pour 108 W pendant 2,7 s coûte un
        # démarrage à froid et ne rend rien. Mesuré le 06/09/2026 sur 40 h de `simulation.csv`, en
        # comparant les Wh COMMANDÉS aux Wh RÉELLEMENT sortis (`homeOutput`), par durée d'épisode :
        #     < 5 s   ->   0 %      (up : 3 épisodes, 0,31 Wh commandés, 0,00 sorti)
        #     5-15 s  ->   9 %      (up : 15 épisodes, 13,70 Wh commandés, 1,29 sorti)
        #     15-60 s ->  35 %
        #     > 60 s  ->  94 %
        # Ce n'est pas propre à `up` : le SolarFlow fait 47 réveils de moins de 15 s sur la période
        # (10 sous 5 s, 37 entre 5 et 15 s), à 12 % de rendement. Un onduleur à l'arrêt met 11,7 s à
        # démarrer (cf. la fiche « démarrage à froid ») : tout engagement plus court que ça est perdu
        # d'avance — on paie la mise en route et on coupe avant qu'il ait produit.
        #
        # ⭐ ÉCRIT ICI ET NULLE PART AILLEURS : `_engage_ok` est le SEUL point où l'on décide de
        # réveiller un appareil, et il est appelé aux 8 endroits (décharge 1/2/3, charge 1/2/3, et les
        # deux étapes SOLAR_ENGAGE). Le poser ici couvre les huit d'un coup — c'est exactement le
        # « une décision écrite à deux endroits, corrigée à un seul » que ce fichier collectionne.
        #
        # SEUIL = `wake_grace` (15 s), dont c'est DÉJÀ le rôle : « tant qu'il est sous `wake_grace`,
        # on ne conclut pas » qu'un appareil ne répond pas. Ici on ne conclut pas non plus qu'il faut
        # le réveiller. Aucun paramètre nouveau ; `wake_grace` = 0 rend le dwell inopérant, soit le
        # comportement d'avant la 1.4.4.6, exactement.
        #
        # COÛT RÉEL du retard : PAS 15 s. L'appareil met déjà 11,7 s à démarrer, donc on ne diffère
        # que le solde. Et un besoin qui dure vraiment est simplement servi 15 s plus tard, par un
        # appareil qui, lui, produira ses 94 %.
        wg = self.wake_grace.asNumber
        if wg <= 0:
            self.engage_why.pop(d.deviceId, None)
            return True
        since, last = self.engage_since.get(d.deviceId, (now, now))
        if (now - last).total_seconds() > ENGAGE_GAP:
            since = now                      # le besoin avait disparu : on repart de zéro
        self.engage_since[d.deviceId] = (since, now)
        attente = (now - since).total_seconds()
        if attente < wg:
            # 1.4.5.3 : `dwell` = le besoin est légitime mais n'a pas encore assez duré.
            self.engage_why[d.deviceId] = f"dwell({attente:.0f}/{wg:.0f}s)"
            return False
        self.engage_why.pop(d.deviceId, None)
        return True

    @staticmethod
    def _tag(i: int, d: ZendureDevice) -> str:
        """Étiquette courte et NON AMBIGUË d'un device pour les champs de `simulation.csv`.

        `rang dans la liste` + 4 lettres du dernier mot du nom : « 0Pro », « 1glag », « 2up ».
        Le rang lève l'ambiguïté si deux appareils portent un nom proche ; les lettres restent
        lisibles à l'œil. Indispensable parce que `acc=`/`liv=` ne listent QUE les devices déjà
        mesurés : sans préfixe, on ne sait pas à qui appartient la k-ième valeur.
        """
        mot = d.name.split()[-1] if d.name else "?"
        return f"{i}{mot[:4]}"

    def _pmax(self, d: ZendureDevice, charge: bool = False) -> float:
        """Puissance MAXIMALE qu'on s'autorise à demander à ce device (W, toujours positif).

        SEUL endroit où le plafond d'occupation `load_max` est appliqué — cf. sa déclaration pour
        les mesures. Tout ce qui décide « combien ce device peut-il travailler » passe par ici :
        `_dis_ceiling`, `_chg_ceiling`, et les capacités mobilisables `imax`/`cmin`.

        ⚠️ `imax`/`cmin` DOIVENT suivre. `imax` porte trois rôles (anti-windup, plafond de
        l'intégrale, plafond de la descente) : si les plafonds de dispatch baissent sans que la
        capacité mobilisable baisse aussi, l'intégrale s'accumule contre une puissance qu'on a
        décidé de ne pas utiliser. C'est mot pour mot le bug corrigé en 1.4.3.36/.37 — la même
        décision écrite à deux endroits, un seul corrigé.

        ⚠️ NE s'applique PAS au routage du surplus solaire (`prod_extra`, deux sites). Là on fait
        TRANSITER le PV gratuit d'un producteur vers une autre batterie : plafonner l'enverrait au
        réseau. Décision différente (éviter l'export), pas le même choix — l'exception est
        volontaire. Ni à `fuseGrp.maxpower`, qui est une limite électrique, pas un choix de charge.
        """
        lim = float(-d.charge_limit if charge else d.discharge_limit)
        return max(0.0, lim * self.occ)

    def _dis_ceiling(self, d: ZendureDevice) -> float:
        """Plafond de DÉCHARGE basé sur la livraison MESURÉE (`prod_accept`), + marge de re-sondage.

        Miroir de `_chg_ceiling`. Un device peut accepter une consigne sans la livrer : le SolarFlow
        2400 se fait brider à ~600-800 W par le cloud (`inverseMaxPower`) tout en acceptant
        `outputLimit`, et un Hyper dérate thermiquement au-delà de 62 °C. Allouer sur la limite
        NOMINALE lui donne alors tout le budget, le moteur croit la demande couverte, ne bascule pas
        sur les autres batteries — et la maison importe.

        `dis_probe` = 3000 (défaut) neutralise ce plafond : comportement historique inchangé.
        """
        if (acc := self.prod_accept.get(d.deviceId)) is None:
            return self._pmax(d)
        return min(self._pmax(d), acc + self.dis_probe.asNumber)

    def _charge_subie(self, d: ZendureDevice) -> float:
        """Absorption que l'appareil s'accorde SANS consigne — décision de son firmware, pas du moteur.

        MESURÉE le 06/08 de 11:10:00 à 11:18:13 : `up` a absorbé **1200 W avec `Cmd` à 0**, à 10 %
        de SoC (donc sous son `minSoc`), pendant que le moteur routait les 1200 W de solaire de
        glagla vers le SolarFlow. Résultat : **105 Wh d'import en 8 minutes**, et 1004 W au compteur.

        ⛔ Le moteur ne pouvait pas le voir : `house_load = P1 + Σ Home` compte cette absorption
        comme de la consommation qu'on couvre. À 11:16:59 : `P1 = +1004`, `Σ Home = -1202`,
        donc `hl = -198` — le moteur croyait avoir un SURPLUS de 155 W à absorber au moment précis
        où il importait un kilowatt. L'intégrale, seule lucide, avait bien plongé, mais elle est
        bornée par `-charge_base` et restait collée à -707.

        ⭐ La bonne réponse n'est pas d'annuler le routage (l'import viendrait quand même de `up`,
        gain nul : mesuré, il passerait de 1004 à 1002 W) mais d'ENVOYER LE SOLAIRE LÀ OÙ IL EST
        DÉJÀ CONSOMMÉ. D'où le tri des puits par absorption subie décroissante (étape 1bis).

        ⚠️ La valeur est calculée et CONFIRMÉE dans la boucle de mesure (cf. `SUBIE_MIN`) : une
        absorption non commandée n'est retenue qu'après 30 s, sinon on prendrait pour une décision
        du firmware le simple décalage entre une consigne fraîche et une télémétrie rafraîchie
        toutes les 3 s. ⛔ Ce décalage n'est PAS une lenteur des appareils — ils répondent en moins
        d'une seconde ; c'est la chaîne de mesure qui ne peut pas le montrer. Ici on ne fait que
        lire le résultat.
        """
        return self.chg_subie.get(d.deviceId, 0.0)

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

        ⛔ 1.4.3.48 — LE PLAFOND NE VAUT QUE TANT QUE LE REFUS EST D'ACTUALITÉ (cf. `CHG_CAP_TTL`).
        Sans cette péremption, `chg_accept` — qui n'est que le MAXIMUM OBSERVÉ — devient un plafond
        définitif : le moteur décrète que l'appareil ne peut pas faire mieux que ce qu'on a bien
        voulu lui demander. Le 08/08, `load_max` avait bridé le SolarFlow à 2040 W pendant des
        jours ; `chg_accept` a appris 2041 et le plafond s'est figé à 2191 W. Remettre `load_max`
        à 100 n'a rien changé — le cliquet, lui, était resté fermé.
        """
        if (acc := self.chg_accept.get(d.deviceId)) is None:
            return self._pmax(d, charge=True)
        until = self.chg_cap_until.get(d.deviceId)
        if until is None or datetime.now() >= until:
            return self._pmax(d, charge=True)   # aucun refus récent : rien ne justifie de brider
        return min(self._pmax(d, charge=True), acc + self.chg_probe.asNumber)

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
        # GEL de l'intégrale sur une TRAVERSÉE courte d'IDLE (secondes). 0 = OFF = ancien comportement.
        # Mesuré le 26/07 : 27 remises à zéro en 30 min, dont 24 sur des séjours en IDLE de MOINS DE
        # 10 s (médiane 3 s), chacune jetant ~134 W de correction accumulée. Cause : la machine à états
        # INTERDIT CHARGE↔DISCHARGE en direct (toute inversion transite par IDLE) et IDLE remet
        # l'intégrale à 0 → la seule grandeur qui corrige les biais durables (dérating, tapering, écart
        # commande↔livraison) est effacée toutes les ~70 s et n'a jamais le temps d'agir.
        self.idle_hold = FondationNumber(m, "fondation_idle_hold", 0, 0, 120, "s")
        # Marge de ré-exploration au-dessus de la DÉCHARGE réellement livrée (`prod_accept`), miroir
        # exact de `chg_probe` côté charge. 3000 = plafond DÉSACTIVÉ (défaut) = comportement historique.
        # Sert quand un device accepte une consigne mais ne la livre pas (bridage cloud du SolarFlow,
        # dérating thermique) : sans ce plafond, le moteur croit disposer de la consigne et ne bascule
        # pas la demande sur les autres batteries -> import. ⚠️ Trop petit, il s'auto-enferme : le
        # plafond ne remonte qu'en observant une livraison plus haute, qu'on ne peut observer qu'en
        # demandant plus. La marge est ce qui permet de re-sonder à la hausse.
        self.dis_probe = FondationNumber(m, "fondation_dis_probe", 3000, 50, 3000, "W")
        # PAS MINIMUM de correction à la BAISSE de l'intégrale. 0 = proportionnel pur (défaut depuis
        # la 1.4.3.35) ; mettre 120 restaure exactement le comportement antérieur.
        #
        # ⚠️ CYCLE LIMITE MESURÉ LE 26/07 À 20 h. La descente s'écrivait `max(step, min(-p1, imax))` :
        # le `max(step, …)` imposait un pas d'AU MOINS `step` (120 W) même pour un écart minuscule.
        # Sur la fenêtre 20:00-20:23, **52 % des descentes** ont été forcées à 120 W pour un écart réel
        # médian de **63 W** — sur-correction ×2, systématique. Comme la MONTÉE est plafonnée à `step`,
        # l'intégrale ne pouvait pas se poser : elle battait en dents de scie ±120 W (motif
        # −120/0/−120/0 lisible dans la trace), la consigne suivait, P1 bougeait, et on recommençait.
        # C'est la signature d'un intégrateur à pas minimum non nul : il ne converge jamais, il oscille
        # toujours d'au moins ±`step`. Mesuré : e-type de la consigne 334 W pour un e-type de P1 de
        # 206 W, alors que la maison était STABLE (sèche-linge 500-530 W, sans variation).
        #
        # `step` cumulait deux rôles contradictoires : PLAFOND de montée et PLANCHER de descente. Le
        # rapport montée/descente dépendait donc de sa valeur — d'où l'emballement du 26/07 matin
        # quand il valait 30. Les deux rôles sont désormais séparés.
        #
        # Ce qui NE change PAS : la vertu du correctif « B » (réagir vite à un gros export) est
        # intacte, la descente reste proportionnelle et non bridée à 2×step. Seule disparaît la
        # sur-correction sur le bruit.
        self.int_min = FondationNumber(m, "fondation_int_min", 0, 0, 500, "W")
        # DÉLAI DE GRÂCE AU DÉMARRAGE (s). 0 = OFF = comportement antérieur.
        #
        # ⚠️ Mesuré le 27/07 à 11:06 : un onduleur met ~10-13 s à monter en charge. Pendant ce temps
        # il absorbe/livre ZÉRO alors que la consigne est déjà là. Le détecteur de refus concluait
        # donc au refus et écrasait le plafond mesuré :
        #   11:06:24  SolarFlow consigne -381  absorbe 0   acc=1714
        #   11:06:30  SolarFlow consigne -507  absorbe 0   acc=   0   <- effondrement
        #   11:06:53  SolarFlow                  -2226     acc=2226   <- il pouvait tout prendre
        # Conséquence en cascade : `chg_room` du SolarFlow tombe à `chg_probe` (150 W) -> le moteur
        # croit à un besoin de puits supplémentaire -> il réveille `up` -> `up` met LUI AUSSI 13 s à
        # démarrer -> pendant ce temps le SolarFlow a fini sa rampe et, la stratégie de charge étant
        # « le plus gros d'abord », il reprend tout le budget -> `up` est coupé au moment précis où
        # il commençait à absorber. 26 épisodes ce matin-là, durée MÉDIANE 6 s, 69 % sous 10 s, et
        # `up` n'absorbe rien sur 75 % des cycles qu'on lui commande.
        #
        # Le garde-fou existant (`rising` + `CHG_REFUSE_N`) ne protège QUE la rampe déjà commencée.
        # Il est aveugle au cas « encore à zéro », qui est justement celui du démarrage. On chronomètre
        # donc le temps passé à ne rien prendre AVEC une consigne active : tant qu'il est sous
        # `wake_grace`, on ne conclut pas. Le chrono repart de zéro dès que l'appareil bouge ou que la
        # consigne retombe — il ne dépend PAS de la valeur de la consigne, donc le slew qui la fait
        # varier à chaque cycle ne le réarme pas indéfiniment.
        self.wake_grace = FondationNumber(m, "fondation_wake_grace", 15, 0, 60, "s")
        # CHEMIN « CONTRÔLE DIRECT ». 0 = DÉSACTIVÉ (défaut depuis la 1.4.3.37) / 1 = actif.
        #
        # ⚠️ AUDIT DU 27/07 : `_direct_control()` est un SECOND MOTEUR, atteint par un `return`
        # anticipé, et il n'a reçu AUCUN correctif de juillet — ni B, ni B-décharge, ni `int_min`,
        # ni `_chg_ceiling`, ni `_dis_ceiling`, ni `_engage_ok`, ni les limites de fusegroup, ni le
        # gel d'IDLE. Il ne s'exécute jamais dans la configuration actuelle (il exige un producteur
        # PLEIN, qui PRODUIT, en gridReverse désactivé/interdit — glagla est en autorisé, et
        # `forced=0` sur toutes les traces le confirme). C'est donc du code mort — mais qui
        # s'activerait SANS PRÉVENIR au premier changement de mode avec batterie pleine, ramenant le
        # moteur à son comportement de début juillet.
        #
        # On le neutralise plutôt que de le supprimer : le cas qu'il traite (producteur plein qui
        # OBÉIT) est aujourd'hui couvert par le chemin normal — absent de `forced`, donc commandé à
        # l'étape 1, surplus routé par l'étape 1bis, écrêtage assuré par le firmware — mais c'est un
        # raisonnement de COUVERTURE, pas une mesure : le chemin ne s'exécutant jamais, on ne peut
        # pas le comparer sur une trace réelle. Le remettre à 1 permettra de trancher le jour où le
        # cas se présentera vraiment.
        self.direct = FondationNumber(m, "fondation_direct", 0, 0, 1)
        # SEUIL D'ENGAGEMENT DES PUITS DE CHARGE (W). 0 = OFF = comportement antérieur.
        #
        # `_engage_ok` existait mais n'était appliqué QU'EN DÉCHARGE, étape 2 (constat n°5 de l'audit
        # du 27/07). Côté charge, aucun seuil : `up` était réveillé pour 37 W — un onduleur en
        # consomme ~50 rien que pour fonctionner. Mesuré le 27/07 : **66 allumages en 13 h 47**, un
        # toutes les 12 min, durée médiane 2 s, et 27 % seulement de la consigne réellement absorbée.
        #
        # VALEUR CHOISIE PAR LA MESURE, en rejouant la trace pour chaque seuil candidat :
        #     seuil    allumages   Wh perdus   Wh captés
        #        0        66          0,0        25,9
        #      250        18          2,9        17,4
        #      350        13          3,9        11,8
        #      400         7          5,0         8,4
        #      500         3          5,0         5,5      <- la courbe s'aplatit ici
        #      700         3          5,0         5,5
        # Le palier 400 -> 500 est GRATUIT (4 allumages de moins, 0 Wh de plus) et au-delà rien ne
        # bouge. L'enjeu énergétique total est dérisoire — 26 Wh sur 14 h, quand glagla produit
        # 2774 Wh dans la seule matinée — alors que le cyclage, lui, est massif. L'utilisateur a
        # tranché : préserver l'onduleur prime sur quelques watts.
        #
        # Le seuil ne mord QUE sur les petites parts. Quand le SolarFlow tapère (>95 % de SoC) il
        # n'absorbe plus rien et TOUT le surplus revient à `up` : sa part passe très au-dessus de
        # 500 W et le seuil s'efface — exactement quand `up` devient utile. L'hystérésis de
        # `_engage_ok` fait le reste : une fois démarré, il continue jusqu'à 250 W.
        # Les PRODUCTEURS en sont exemptés (`_engage_ok`) : déjà allumés, les engager ne coûte
        # aucun cycle supplémentaire.
        self.min_engage_chg = FondationNumber(m, "fondation_min_engage_chg", 500, 0, 1000, "W")
        # PLAFOND D'OCCUPATION DU PARC (% de la plaque signalétique). 100 = OFF = comportement antérieur.
        #
        # ⚠️ UN ONDULEUR TENU À FOND DÉRATE, SANS RIEN ANNONCER. Nuit du 27/07, sèche-linge puis
        # cumulus, le SolarFlow commandé à 2400 W en continu :
        #     21:30  consigne 2351  livré 2331  (−21 W)   Tmp 36,8
        #     22:20  consigne 2380  livré 2358  (−22 W)   Tmp 58,5
        #     22:30  consigne 2390  livré 2287  (−104 W)  Tmp 59,9
        #     22:50  consigne 2395  livré 2211  (−185 W)  Tmp 60,0
        #     23:20  consigne 2400  livré 2171  (−229 W)  Tmp 60,0
        # Une heure à pleine puissance suffit à atteindre 60,0 °C, où la température SE FIGE au
        # dixième — signature d'un régulateur thermique — et la livraison décroche. Le moteur, lui,
        # croit la demande couverte : import permanent, invisible. (Le SoC baisse en parallèle, donc
        # les deux effets sont confondus sur cette nuit-là ; mais à 57 °C avec SoC 62 % le ratio
        # livré/commandé vaut encore 0,99, et la cassure suit la TEMPÉRATURE, pas le SoC.)
        #
        # CE N'EST PAS PROPRE AU SolarFlow. Température médiane selon le taux d'occupation, sur les
        # 18 h de la trace :
        #     occupation     SolarFlow   glagla
        #        ~25 %          34 °C     31 °C
        #        ~58 %            —       34 °C
        #        ~79 %          48 °C     34 °C  (à 75 %)
        #        ~88 %          53 °C       —
        #        ~96 %          55 °C       —            (max 60 = plafond)
        #        ~92 %            —       61 °C          (plafond 63)
        # glagla reste à 34 °C jusqu'à 75 % d'occupation et saute à 61 °C entre 1000 et 1200 W.
        # Le seuil est au même endroit sur les deux machines : ~85 % est le dernier point froid.
        #
        # EFFET DE BORD RECHERCHÉ — LE CYCLAGE. Le partage est en CASCADE : le device au « genou »
        # encaisse 100 % de la variation de la consigne. Tant que le SolarFlow bute sur 2400, le
        # genou est à 2400 et glagla tombe à 0 W dès que la consigne passe dessous. Rejeu du partage
        # sur la vraie série de consignes du 27/07 21h36-23h (84 min) :
        #     plafond   glagla à 0 W   bascules on/off   σ(consigne glagla)
        #       100 %        16 %            471               289
        #        85 %         2 %             75               287
        #        80 %         1 %             21               247
        # Le plafond ne réduit PAS l'amplitude du balancement (σ inchangé) : il déplace le genou de
        # 2400 à 2040, et comme la consigne passe rarement sous 2040 (p5 = 2140), glagla cesse de
        # toucher la butée 0. Six fois moins d'allumages/extinctions de l'onduleur.
        #
        # « POUSSER SI ÇA NE SUFFIT PAS » — SURTOUT PAS UN INTERRUPTEUR. Lever le plafond par un
        # test binaire « besoin > capacité plafonnée » fabriquerait exactement le défaut qu'on
        # corrige depuis une semaine : un seuil que le besoin traverse fait sauter tous les plafonds
        # de 15 % d'un coup. La levée est donc CONTINUE (cf. `occ` dans `update`) : l'occupation vaut
        # le plafond, OU ce que la maison réclame si c'est plus. Monotone, sans discontinuité.
        self.load_max = FondationNumber(m, "fondation_load_max", 100, 50, 100, "%")
        # ⛔ 1.4.3.52 — PLAFOND DE SORTIE : l'export au-delà du contrat est INTERDIT.
        #
        # La pointe d'export n'est PAS réductible par une réaction, quelle qu'elle soit. Mesuré le
        # 14/08 à 19:06:49 (charge VE de ~3300 W qui s'arrête) :
        #   19:06:48  P1   -68 W   sortie 4035 W  consigne 3884   <- rien d'anormal
        #   19:06:49  P1 -3350 W   sortie 4035 W  consigne 3884   <- POINTE, 1er échantillon
        #   19:06:51                              consigne 1773   <- le moteur a déjà réagi (2 s)
        #   19:06:54  P1  -135 W                                  <- fini
        # La pointe est atteinte AVANT que la boucle ait pu voir quoi que ce soit. Un « kill switch »
        # sur P1 < seuil ne peut donc pas l'empêcher : pour se déclencher, il faut que les 3350 W
        # soient déjà passés. Il ne raccourcirait qu'une durée qui vaut déjà 3 s, et couper à zéro
        # fabrique l'import qui suit (11/08 : 7 s à 940 W après une coupure sèche).
        #
        # ⇒ On ne réagit pas, on PLAFONNE EN AMONT. La pointe d'export vaut ce que le parc SORTAIT
        # à l'instant du lâcher (vérifié sur 9 épisodes : 4035 -> 3350, 3512 -> 2752, 3070 -> 2268),
        # donc borner la sortie borne l'export — sans latence, sans détection, sans cas particulier.
        #
        # REJEU sur les 47 épisodes d'export >= 1500 W de 6 jours de trace :
        #   sans plafond  : pire pointe 3350 W
        #   à 2800        : pire pointe 2214 W   (le 14/08 tombe à 2115 W)
        #   à 2400        : pire pointe 2133 W   -> +264 Wh/j pour 81 W de mieux : non
        #
        # COÛT : 36,5 min/jour bridées, manque médian 469 W, exclusivement entre 19 h et 23 h
        # (0 Wh entre 10 h et 18 h). Mais l'énergie n'est pas PERDUE, elle est DÉCALÉE : les trois
        # batteries touchent leur plancher de 15 % toutes les nuits, et l'import qui suit la mise à
        # plat (1172 à 27946 Wh) dépasse de loin ce que le plafond retient (200 à 460 Wh) — les Wh
        # retenus sont donc rendus avant la fin de la nuit. Import total inchangé.
        #
        # ⚠️ CE PLAFOND EST DUR : si la maison demande 3300 W, le parc en sort 2800 et le reste est
        # acheté. C'est le prix de la garantie, et il n'y a pas de version « intelligente » : sortir
        # 3300 W, c'est accepter un risque d'export de 3300 W.
        # 0 = désactivé (no-op exact). Défaut 2800 : la valeur demandée, appliquée même si la
        # restauration échouait (cf. la course de construction documentée dans `FondationNumber`).
        self.out_max = FondationNumber(m, "fondation_out_max", 2800, 0, 5000, "W")
        # Bouton de DÉBLOCAGE : écrit `inverseMaxPower = sf_dismax` UNE fois sur le/les SolarFlow.
        # Bouton (et non automatisme) parce que cette propriété part en FLASH : elle doit être écrite
        # rarement et volontairement. Cf. `unlock_solarflow` pour la procédure complète.
        self.sf_unlock = ZendureButton(m, "fondation_sf_unlock", self.unlock_solarflow)
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

    async def unlock_solarflow(self, _button=None) -> None:
        """Écrit `inverseMaxPower = sf_dismax` UNE fois sur chaque SolarFlow (zenSDK local).

        POURQUOI UN BOUTON, ET PAS UN AUTOMATISME. La doc officielle zenSDK classe
        `inverseMaxPower` en écriture FLASH, « à ne pas utiliser pour du contrôle continu ». Les
        versions 1.4.3.29/.30 le réécrivaient toutes les 5 s pour lutter contre le cloud : c'était
        une erreur, ça use la mémoire du device. Le contrôle continu se fait par `outputLimit`
        (ce que le moteur envoie déjà à chaque cycle) ; `inverseMaxPower` n'est que le PLAFOND,
        à poser une fois.

        PROCÉDURE (vérifiée sur la doc zenSDK + fork Gielz1986/Zendure-HA-zenSDK, 26/07) :
          1. **HEMS désactivé dans l'app Zendure** — prérequis. Tant qu'il est actif, le cloud
             réécrit `inverseMaxPower` (mesuré : ~800 W toutes les 6 s) et écrase tout.
          2. Régler `fondation_sf_dismax` à la capacité réelle (2400 pour un SolarFlow 2400).
             Ça débride la vue INTERNE du moteur (discharge_limit + fuseGrp.maxpower).
          3. Appuyer sur ce bouton : une écriture, une seule.
          4. Vérifier que le device rapporte bien la nouvelle valeur, et qu'elle TIENT.

        ⚠️ On lève le plafond ARBITRAIRE, jamais la protection thermique : si le device dérate en
        chauffant il livrera moins que commandé, P1 le verra et l'intégrale basculera sur `up`.
        """
        # ⛔ 1.4.3.51 — CE BOUTON ÉTAIT SILENCIEUSEMENT INOPÉRANT.
        # Il commençait par `if int(self.sf_dismax.asNumber) <= 0: return`. Or `sf_dismax` vaut 0
        # par défaut — et il valait 0 sans exception du 25/07 au 10/08 sur toutes les traces. Le
        # bouton sortait donc à sa deuxième ligne, SANS envoyer la moindre requête, et sans autre
        # trace qu'un `_LOGGER.warning` que rien n'affiche. Vu de l'UI : un bouton qui ne fait rien.
        # Ni timeout, ni refus de l'appareil : AUCUNE tentative (l'écriture flash prend 87 ms
        # mesurés, très loin du délai d'1 s — j'ai failli accuser le timeout à tort).
        #
        # La garde n'était pas absurde : le déblocage a deux moitiés, l'écriture dans l'appareil et
        # la vue interne du moteur. Mais elle protégeait contre un désaccord IMPOSSIBLE : `device.py`
        # écoute déjà `inverseMaxPower` et rappelle `setLimits`, donc dès que l'appareil confirme
        # 2400 la vue interne suit toute seule. `sf_dismax` reste utile pour forcer une autre valeur,
        # il ne doit pas être un prérequis.
        #
        # ⭐ REPLI SUR LA PLAQUE DU MODÈLE. À défaut de réglage, on écrit `discharge_nominal` —
        # la valeur du constructeur du device, mémorisée AVANT tout écrasement par le cloud
        # (cf. `setLimits`). Lire `discharge_limit` à la place renverrait 800, la valeur du bridage.
        reglage = int(self.sf_dismax.asNumber)
        for d in self.manager.devices:
            if not isinstance(d, ZendureZenSdk):
                continue
            target = reglage if reglage > 0 else int(getattr(d, "discharge_nominal", 0))
            if target <= 0:
                _LOGGER.warning("SolarFlow unlock %s : aucune cible (ni sf_dismax, ni plaque)", d.name)
                continue
            imp = d.entities.get("inverseMaxPower")
            current = getattr(imp, "asInt", None) if imp is not None else None
            _LOGGER.warning("SolarFlow unlock %s : inverseMaxPower %s -> %s (écriture FLASH unique)", d.name, current, target)
            await d.doCommand({"properties": {"inverseMaxPower": target}})
            # Le bouton doit DIRE ce qu'il a fait : un warning dans les logs ne se voit pas.
            # ⛔ 1.4.3.55 — `self.hass` N'EXISTE PAS sur FondationEngine (ce n'est pas une
            # entité HA, juste un objet tenu par le manager) : la 1.4.3.51 levait donc
            # `'FondationEngine' object has no attribute 'hass'` à CHAQUE appui.
            # ⚠️ L'écriture flash, elle, avait bien eu lieu — elle est juste au-dessus — mais
            # l'utilisateur ne voyait qu'un échec et pouvait croire le déblocage raté. Vérifié
            # le 19/08 : l'appareil rapportait `inverseMaxPower = 2400` malgré le message.
            # Et le confort ne doit JAMAIS faire échouer l'action : la notification est donc
            # enveloppée. Ce qui compte est déjà fait, et déjà journalisé en warning au-dessus.
            try:
                persistent_notification.async_create(
                    self.manager.hass,
                    f"**{d.name}** — `inverseMaxPower` {current} → **{target} W** "
                    f"({'demandé' if reglage > 0 else 'plaque du modèle'}, écriture flash unique).\n\n"
                    f"Vérifier dans une minute que la valeur **tient** : si elle retombe, le cloud la "
                    f"réécrit et il faut d'abord désactiver HEMS dans l'application Zendure.",
                    "Zendure — déblocage SolarFlow",
                    f"zendure_unlock_{d.deviceId}",
                )
            except Exception as e:  # noqa: BLE001
                _LOGGER.warning("SolarFlow unlock %s : notification impossible (%s) — l'écriture, elle, est faite", d.name, e)

    def createDeviceEntities(self) -> None:
        """Capteur de consigne + case « panneaux raccordés », par onduleur. À appeler APRÈS le
        chargement des devices. `cmdTarget` est LE graphe de diagnostic : commande vs réalisé."""
        for d in self.manager.devices:
            if d.deviceId not in self._cmd_ent:
                self._cmd_ent[d.deviceId] = ZendureSensor(d, "cmdTarget", None, "W", "power", "measurement", state=0)
            if d.deviceId not in self._pv_ent:
                self._pv_ent[d.deviceId] = FondationSwitch(d, "hasSolar", False)

    def _has_pv(self, d: ZendureDevice) -> bool:
        """CET ONDULEUR A-T-IL DES PANNEAUX ? Propriété du matériel, DÉCLARÉE, pas devinée.

        ⚠️ Remplace trois définitions divergentes (audit du 27/07) : `pv_ema > 25` dans les tris de
        charge, `pv_ema - overhead <= 0` dans les filtres de puits, et la mémoire de 30 min de
        `_is_producer`. Trois écritures d'une même notion — la forme exacte de tous les bugs graves
        de juillet.

        Pourquoi une déclaration et non une mesure : un appareil sans débouché ÉCRÊTE SES PROPRES
        PANNEAUX (mesuré le 20/07 : 773 W en autorisé, 152 W en interdit, même soleil). Sa mesure de
        PV s'effondre donc À CAUSE DE NOTRE PROPRE COMMANDE, et il se faisait reclasser « sans PV »
        exactement au mauvais moment — la nuit, à l'aube, sous les nuages. Or c'est là que la
        stratégie de l'utilisateur compte le plus : un producteur doit être chargé EN DERNIER, car
        sa batterie est le SEUL débouché de son solaire au-delà de sa limite AC. Mesuré : glagla
        produit jusqu'à 1727 W pour 1200 W de sortie maximale, soit 216-236 Wh par jour qui ne
        peuvent physiquement passer que par sa batterie. S'il est plein au pic, c'est perdu.

        La case s'apprend seule (cf. `learn`) et ne se décoche jamais toute seule : si la
        restauration échouait, la première production de la journée la recoche. Panne bénigne.

        ⚠️ NE PAS confondre avec `_is_producer`, qui répond à « produit-il MAINTENANT ? » — question
        différente, légitime, utilisée pour la décharge et laissée intacte.
        """
        if (ent := self._pv_ent.get(d.deviceId)) is None:
            return self.pv_ema.get(d.deviceId, 0.0) > PV_SEEN_MIN  # avant création des entités
        return bool(ent.is_on)

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
        #    seule vue interne ne suffit donc pas si le device plafonne sa sortie réelle — MAIS ce
        #    déblocage-là se fait par le BOUTON (écriture unique, `unlock_solarflow`), JAMAIS ici :
        #    `inverseMaxPower` est en FLASH. Aucune écriture périodique dans ce bloc.
        #    Le dérating THERMIQUE reste géré par la mesure : on lève la limite arbitraire du cloud, pas
        #    la protection thermique du device.
        #
        # ⚠️ DANGER si le device n'est PAS réellement débloqué : le moteur croirait pouvoir tirer
        #    `sf_dis` alors que le device plafonne plus bas → il ne basculerait pas sur les autres
        #    batteries → IMPORT. Laisser `sf_dismax` à 0 tant que le déblocage n'est pas confirmé.
        if (sf_dis := int(self.sf_dismax.asNumber)) > 0:
            for d in devices:
                if isinstance(d, ZendureZenSdk):
                    d.discharge_limit = sf_dis
                    d.fuseGrp.maxpower = max(d.fuseGrp.maxpower, sf_dis)
                    # ⚠️ AUCUNE écriture de `inverseMaxPower` ici : la doc zenSDK officielle le dit
                    # écrit en FLASH et « à ne pas utiliser pour du contrôle continu ». L'écrire à
                    # chaque cycle (versions .29/.30) userait la mémoire. Le déblocage se fait par le
                    # bouton `fondation_sf_unlock`, UNE seule écriture, cf. `unlock_solarflow`.

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
        # --- TAUX D'OCCUPATION AUTORISÉ (cf. `load_max` pour les mesures thermiques) ---
        # Calculé AVANT tout le reste : `_pmax` s'en sert, donc `_dis_ceiling`, `_chg_ceiling`,
        # `imax` et `cmin` en dépendent tous. Un seul calcul, un seul chiffre, quatre consommateurs.
        #
        # LEVÉE CONTINUE, pas un interrupteur : l'occupation vaut le plafond réglé, OU ce que la
        # maison réclame si c'est plus. `max(plafond, besoin/capacité)` est monotone et continu en
        # `besoin` — traverser le point où le plafond cesse de mordre ne fait rien sauter.
        #     besoin 2842 W / 4800 W nominal -> 0,59 -> occ = 0,85  (le plafond mord)
        #     besoin 4500 W / 4800 W nominal -> 0,94 -> occ = 0,94  (4500 W disponibles, pile)
        #
        # BESOIN = `hl_ema`, le house_load LISSÉ, et pris tel qu'il était au cycle PRÉCÉDENT : il
        # n'est recalculé que plus bas (après `forced`), et réordonner `update` pour l'avoir ici
        # serait un changement autrement plus risqué que ce retard d'un cycle (~1 s) sur une
        # grandeur lissée qui ne sert qu'à décider si le plafond doit se relâcher.
        # Au tout premier cycle `hl_ema` est None : `occ` reste à sa valeur d'init (1.0), soit
        # le comportement historique — jamais de bridage sur une grandeur pas encore mesurée.
        if (lm := self.load_max.asNumber) < 100 and self.hl_ema is not None:
            besoin = abs(self.hl_ema)
            nominal = sum(
                float(-d.charge_limit if self.hl_ema < 0 else d.discharge_limit) for d in devices
            )
            self.occ = max(lm / 100.0, min(1.0, besoin / nominal)) if nominal > 0 else 1.0
        else:
            self.occ = 1.0
        # Capacité mobilisable — calculée UNE fois ici, car elle part aussi dans `_direct_control`.
        # Cf. le commentaire détaillé plus bas (bug de l'anti-windup, nuit du 27/07).
        # `_pmax` (et non `discharge_limit`) : le plafond d'occupation doit valoir ici AUSSI, sinon
        # l'intégrale se charge contre une puissance qu'on ne commandera pas — cf. `_pmax`.
        imax = sum(self._pmax(d) for d in devices if d.state != DeviceState.SOCEMPTY)
        # ⛔ 1.4.3.52 — le plafond de sortie entre AUSSI ici, et ce n'est pas un raffinement.
        # `imax` a trois rôles (anti-windup `sat_dis`, plafond de l'intégrale, plafond de la
        # descente) et le commentaire ci-dessous le dit : « l'intégrale ne doit jamais dépasser ce
        # qu'on peut réellement commander ». Avec `out_max`, ce qu'on peut commander devient
        # `out_max`. Sans cette ligne, l'intégrale monterait jusqu'à 4800 pendant que le plafond
        # mord (P1 reste positif = import), et au lâcher de la charge elle maintiendrait la demande
        # AU PLAFOND pendant plusieurs secondes : on aurait remplacé une pointe de 3 s par un
        # plateau de 2800 W qui dure. Poser le plafond sans corriger `imax` serait donc une
        # régression, pas une demi-mesure.
        # Bonus : `imax` part déjà en paramètre dans `_direct_control` (cf. l'appel plus bas), donc
        # le second moteur hérite de la correction — c'est précisément l'erreur classique de ce
        # fichier (une décision écrite à deux endroits, corrigée à un seul) que ça évite ici.
        if (omax := self.out_max.asNumber) > 0:
            imax = min(imax, omax)

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
                # APPRENTISSAGE de la case « panneaux raccordés » : produire prouve qu'il en a.
                # Cochage seulement — jamais l'inverse, sinon la nuit effacerait la déclaration.
                if (pvent := self._pv_ent.get(d.deviceId)) is not None:
                    pvent.learn()
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
            # LATENCE DE DÉMARRAGE (cf. `wake_grace`) : miroir exact de la branche charge. Ne pas
            # patcher les deux côtés est ce qui a produit les bugs jumeaux `chg_room`/`chg_cap`
            # (23/07) et `forced`/étape 1 (26/07).
            if out_asked < CHG_REFUSE_MIN or out_real > RAMP_RISE:
                self.prod_wake.pop(d.deviceId, None)
            else:
                self.prod_wake.setdefault(d.deviceId, now)
            grace = self.wake_grace.asNumber
            out_waking = grace > 0 and d.deviceId in self.prod_wake and (now - self.prod_wake[d.deviceId]).total_seconds() < grace
            if not out_waking and out_asked >= CHG_REFUSE_MIN and not up and out_real < min(out_asked - 100, out_asked * 0.75):
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
            # --- CHARGE SUBIE (1.4.3.47) : ce que l'appareil prend SANS qu'on le lui ait demandé ---
            # Retenue seulement si elle PERSISTE. `Home` (rapporté, rafraîchi toutes les 3 s) et
            # `asked` (écrit à l'instant du calcul) ne datent pas du même moment : leur écart passe
            # brièvement à quelques centaines de watts à chaque changement de consigne, sans que
            # l'appareil ait quoi que ce soit à se reprocher. Mesuré : ces écarts tiennent tous en
            # ≤ 19 s, contre 494 s pour l'épisode réel de `up`. Sans ce filtre, le tri des puits
            # partirait 9,5 % du temps sur du bruit de mesure au lieu de 0,2 %.
            sub = max(0.0, absorbed - asked)
            if sub >= CHG_REFUSE_MIN:
                self.subie_since.setdefault(d.deviceId, now)
            else:
                self.subie_since.pop(d.deviceId, None)
            since = self.subie_since.get(d.deviceId)
            self.chg_subie[d.deviceId] = (
                sub if since is not None and (now - since).total_seconds() >= SUBIE_MIN else 0.0
            )
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
            # LATENCE DE DÉMARRAGE — le chrono ne court que si on DEMANDE et qu'il ne prend RIEN.
            # Il se réarme dès que l'appareil bouge (`absorbed > RAMP_RISE`) ou que la consigne
            # retombe : un appareil réellement incapable dépasse `wake_grace` et retombe dans la
            # détection de refus normale, sans changement.
            if asked < CHG_REFUSE_MIN or absorbed > RAMP_RISE:
                self.chg_wake.pop(d.deviceId, None)
            else:
                self.chg_wake.setdefault(d.deviceId, now)
            waking = grace > 0 and d.deviceId in self.chg_wake and (now - self.chg_wake[d.deviceId]).total_seconds() < grace
            if not waking and asked >= CHG_REFUSE_MIN and not rising and absorbed < min(asked - 100, asked * 0.75):
                n = self.chg_refuse.get(d.deviceId, 0) + 1
                self.chg_refuse[d.deviceId] = n
                if n >= CHG_REFUSE_N:
                    self.chg_accept[d.deviceId] = min(self.chg_accept.get(d.deviceId, float("inf")), absorbed)
                    # Le plafond n'est légitime QUE tant que le refus est d'actualité (cf. CHG_CAP_TTL).
                    self.chg_cap_until[d.deviceId] = now + timedelta(seconds=CHG_CAP_TTL)
            else:
                self.chg_refuse[d.deviceId] = 0
                # ⚠️ 1.4.3.47 — une absorption RÉELLE prouve une capacité, qu'on l'ait demandée ou non.
                # `asked > 50` seul rendait la mesure aveugle aux charges décidées par le firmware :
                # le 06/08, `up` a absorbé 1200 W en continu avec `Cmd` à 0, et son `chg_accept` est
                # resté figé à 503 (lu dans `acc=` de la trace). Son plafond de charge restait donc
                # calibré sur une valeur périmée, ce qui le maintenait en queue de tout partage —
                # 27 Wh de charge commandée sur 59 h, contre 12 619 Wh pour le SolarFlow.
                # Seuil `CHG_REFUSE_MIN` (250 W) : on ne retient pas le bruit, seulement une
                # absorption franche. Le sens de `chg_accept` est inchangé (ce que l'appareil PREND).
                if asked > 50 or absorbed >= CHG_REFUSE_MIN:
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
        if self.direct.asNumber > 0 and full_prod and all(_obeys(d) for d in full_prod):
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

        db_on = self.db_on.asNumber
        db_off = self.db_off.asNumber
        ft = self.fast_track.asNumber

        # --- COMMANDE EN VOL — calculee ICI depuis la 1.4.4.7 -------------------------------
        # Elle etait calculee 100 lignes plus bas, juste pour l'integrale. Or `amt` en a besoin
        # AUSSI, et il est forme juste en dessous : le calcul remonte donc avant le split-EMA.
        # Rien d'autre ne change ; `house_net`, `db_on` et `now` sont tous disponibles ici.
        inflight = sum(float(self.cmd_applied.get(d.deviceId, 0)) for d in devices) - house_net
        if abs(inflight) > db_on:
            if self.inflight_since is None:
                self.inflight_since = now
        else:
            self.inflight_since = None
        wg = self.wake_grace.asNumber
        en_vol = wg > 0 and self.inflight_since is not None and (now - self.inflight_since).total_seconds() < wg
        self.inflight, self.en_vol = inflight, en_vol

        # --- split-EMA : le régime décide sur le LISSÉ, les montants sur le BRUT ---
        alpha = max(0.05, min(1.0, self.hl_alpha.asNumber / 100.0))
        self.hl_ema = t_raw if self.hl_ema is None else alpha * t_raw + (1.0 - alpha) * self.hl_ema
        t_reg = self.hl_ema

        # --- lissage des MONTANTS, avec BYPASS fast-track ---
        # Le régime décide sur hl_ema, les montants sur t_amt. Au-delà de ±ft (vrai gros saut
        # d'énergie), on recale le filtre sur le brut et on l'utilise tel quel : un gros saut passe
        # INTACT et immédiatement, seul le bruit est filtré. C'est la différence avec le slew-rate,
        # qui lui écrête aussi les vrais sauts.
        #
        # ⛔ 1.4.4.7 — LE FAST-TRACK NE S'ARME PLUS SUR UN SAUT QUE NOUS AVONS FABRIQUE.
        # Le bypass existe pour un VRAI gros appel de puissance (four, plaque). Mais
        # `house_load = P1 + Σhome` melange un P1 frais et des `home` en retard : a chaque
        # changement de consigne il se trompe de l'amplitude de ce changement — et ce faux pic
        # franchit `ft`, donc supprime le lissage EXACTEMENT quand il faudrait qu'il agisse.
        #
        # MESURE du 09/09/2026, 15 min, `up` seul en charge (~360 mises a jour) :
        #   · `hl` oscille de -79 a 1555 W, ecart-type 309 W, alors que la conso est stable ;
        #   · fast-track arme 78 % des cycles, dont 43 % AVEC de la commande en vol ;
        #   · |hl| median lors de ces armements : 1140 W, pour ~900 W de conso reelle ;
        #   · resultat : setpoint d'ecart-type 261 W, consigne de `up` sautant de 276 W en
        #     mediane (135 sauts de plus de 300 W en 15 min) — le yo-yo constate.
        #
        # La 1.4.4.5 gelait deja l'integrale dans ce cas : elle tient (integrale bornee entre
        # -554 et +143 sur la meme fenetre). Mais `sp = max(0, amt) + integrale` a DEUX termes,
        # et un seul etait protege. Meme cause, meme condition, second consommateur — c'est
        # la forme habituelle des bugs de ce fichier, une decision appliquee a un seul endroit.
        #
        # Le lissage reprend simplement la main : un vrai saut de conso, lui, n'a pas de
        # commande en vol en face, donc il passe toujours INTACT.
        a_amt = max(0.05, min(1.0, self.amt_alpha.asNumber / 100.0))
        if a_amt >= 1.0 or (abs(t_raw) > ft and not en_vol):
            self.amt_ema = t_raw
        else:
            self.amt_ema = t_raw if self.amt_ema is None else a_amt * t_raw + (1.0 - a_amt) * self.amt_ema
        t_amt = self.amt_ema

        # --- machine à états avec hystérésis + fast-track (brut au-delà de ±ft => immédiat) ---
        regime_before = self.regime  # mémorisé pour le gel d'intégrale sur traversée d'IDLE
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
        # Plancher de descente, DÉCOUPLÉ de `step` (qui reste le plafond de montée). Cf. `int_min`.
        imin = self.int_min.asNumber
        # ⚠️ CAPACITÉ RÉELLEMENT MOBILISABLE, pas la somme des PLAQUES SIGNALÉTIQUES (27/07).
        # `imax` sommait les limites des TROIS appareils, y compris ceux à leur plancher de SoC qui
        # ne peuvent rien donner. Nuit du 27/07 : les trois batteries SOCEMPTY (SolarFlow 10 %,
        # glagla 15 %, up 9 %), capacité réelle **0 W**, `imax` = 4800 -> l'anti-windup
        # `house_net >= imax - db_on` n'a JAMAIS pu être vrai et **l'intégrale est restée collée à
        # 4800 de 00 h à 08 h**, huit heures. L'import de 492 W était structurel (parc vide, rien à
        # corriger) — mais l'intégrale n'avait aucune raison d'empiler une correction qu'aucun
        # appareil ne pouvait appliquer.
        #
        # ⚠️ `imax` a TROIS rôles : anti-windup, plafond de l'intégrale, et plafond de la descente.
        # Les trois deviennent justes ensemble — l'intégrale ne doit jamais dépasser ce qu'on peut
        # réellement commander. C'est le même piège que `step`, qui cumulait plafond de montée et
        # plancher de descente (cf. `int_min`) : un symbole, plusieurs rôles, et une correction sur
        # l'un cassait l'autre. Quand tout est vide, `imax` = 0 : l'intégrale est écrêtée à 0, ce qui
        # est exactement le comportement voulu.
        # (`imax` est calculé plus haut : il sert aussi à `_direct_control`, qui souffrait du même
        # défaut — patcher un seul des deux chemins est l'erreur classique de ce fichier.)
        # Jumeau de `imax` côté charge : même plafond d'occupation, sinon l'anti-windup de la
        # branche CHARGE resterait calibré sur des plaques signalétiques qu'on n'utilise plus.
        cmin = -sum(self._pmax(d, charge=True) for d in devices if d.state != DeviceState.SOCFULL)
        sat_dis = house_net >= imax - db_on                             # déchargé à fond
        sat_chg = house_net <= cmin + db_on                             # chargé à fond
        # --- 1.4.4.5 — ANTI-WINDUP SUR LE TEMPS MORT (jumeau TEMPOREL de sat_dis/sat_chg) -----
        # `sat_dis`/`sat_chg` ne couvrent qu'une seule façon d'être au bout de ce qu'on peut faire :
        # la saturation en AMPLITUDE (« plus rien à mobiliser »). Il en existe une seconde, en
        # TEMPS : la consigne est PARTIE mais l'appareil ne l'a pas encore réalisée. Tant qu'elle
        # est en vol, l'écart de P1 est DÉJÀ couvert par une commande en cours — le recharger dans
        # l'intégrale, c'est commander deux fois la même chose.
        #
        # ⚠️ Ce mécanisme est déjà connu de ce fichier, et déjà neutralisé — mais à UN SEUL endroit.
        # Cf. le commentaire de `sub` (charge subie, 1.4.3.47) : « `Home` (rapporté) et `asked`
        # (écrit à l'instant du calcul) ne datent pas du même moment : leur écart passe brièvement à
        # quelques centaines de watts à chaque changement de consigne, sans que l'appareil ait quoi
        # que ce soit à se reprocher. » Exactement le même bruit, la même cause — reconnu pour le
        # tri des puits, ignoré par l'intégrale. C'est la forme que prennent tous les bugs graves de
        # ce fichier : une décision écrite à deux endroits, corrigée à un seul.
        #
        # MESURÉ le 04/09/2026 sur 259 s (simulation.csv, HA redémarré à 14:36) :
        #   · un Hyper met 2,7 s à encaisser une baisse, 10,8 à 11,7 s à démarrer depuis 0 ;
        #   · pendant ce temps mort P1 reste positif — non par manque de puissance, mais parce que
        #     celle qu'on a demandée n'est pas encore arrivée ;
        #   · l'intégrale le lit comme un manque : 74 -> 174 -> 272 -> 298 en 10 s, avec 490 W déjà
        #     commandés et non rapportés (`Σcmd`=917, `ΣHome`=427, figé) ;
        #   · puis `up` arrive : P1 saute de +108 à -399, l'intégrale plonge à -481, le moteur coupe ;
        #   · bilan : 14 bascules on/off de `up` en 4 min (1 toutes les 18 s), dont 4 extinctions de
        #     moins de 8 s, chacune payée d'un redémarrage à froid de 10-12 s.
        #   · commande en vol médiane : +148 W quand l'intégrale monte, +1 W quand elle est stable.
        #
        # EN VOL = ce qu'on a COMMANDÉ moins ce qui est RAPPORTÉ, sur le même jeu de devices que
        # `house_net` (un device retiré de la liste ne compte dans aucun des deux termes).
        # POSITIF = on attend encore de la décharge -> un P1 positif est déjà couvert.
        # NÉGATIF = on attend encore de la charge   -> un P1 négatif est déjà couvert.
        # Le test est écrit pour les DEUX sens, sinon on refait le bug jumeau du fichier.
        #
        # SEUIL = `db_on` (la zone morte du moteur) : en-dessous, ce n'est pas un écart significatif.
        # Pas de nouveau paramètre — un réglage de plus serait un réglage à désaccorder.
        #
        # BORNE DE TEMPS = `wake_grace` (15 s), dont c'est DÉJÀ le rôle exact : « tant qu'il est sous
        # `wake_grace`, on ne conclut pas » qu'un appareil n'obéit pas. Au-delà, ce n'est plus une
        # latence, c'est un refus : l'intégrale doit reprendre la main, sinon un appareil qui
        # n'obéit jamais gèlerait la correction d'un import pour toujours. Les 12 épisodes mesurés
        # durent 6,8 s en médiane et 12,6 s au maximum — la borne à 15 s ne coupe aucun cas légitime.
        # `wake_grace` = 0 -> `en_vol` toujours faux = comportement d'avant la 1.4.4.5, exactement.
        # (calcul remonte avant le split-EMA en 1.4.4.7 : `amt` en a besoin AUSSI)
        # ÉCRÊTAGE PERMANENT — et pas seulement au moment d'accumuler (trou de la 1.4.3.36).
        # `sat_dis` empêche l'intégrale de MONTER quand plus rien n'est mobilisable, mais il ne la
        # fait pas DESCENDRE : avec `imax` = 0, la vidange `max(imin, min(-p1, imax))` vaut 0 et la
        # valeur accumulée AVANT que le parc se vide restait figée. C'est exactement ce qui s'est
        # produit la nuit du 27/07 (intégrale à 4800 de 00 h à 08 h) : borner à l'entrée ne suffit
        # pas, il faut borner l'ÉTAT. Uniquement par le HAUT : le plancher reste `int_neg` (B).
        self.integral = min(self.integral, imax)
        # ⛔ 1.4.3.50 — ET PAR LE BAS, POUR LA MÊME RAISON. L'anti-inversion `-dis_base` /
        # `-charge_base` n'existait QUE dans les branches de vidange, donc uniquement TANT QUE
        # l'écart P1 persiste. Or c'est après, quand la demande s'effondre, que la borne devient
        # nécessaire : l'intégrale garde la valeur atteinte face à l'ancienne demande, et se
        # retrouve plus grande que la nouvelle. `demand = max(0, t_amt) + integral` passe alors
        # NÉGATIF et la consigne s'inverse — le moteur charge en régime DISCHARGE, ce que le
        # commentaire de la branche export dit pourtant vouloir interdire (« jamais d'inversion
        # via l'intégrale ; c'est le RÉGIME qui décide »). Même défaut de forme que l'écrêtage
        # par le haut corrigé en `.36`/`.37` : borner à l'entrée ne suffit pas, il faut borner l'ÉTAT.
        #
        # MESURÉ le 08/08 à 23:28:57, arrêt du cumulus (pince A : 2254 W → 5 W) :
        #   23:28:59  hl 3030 → 276, l'intégrale plonge à -1200 (plancher `int_neg`) — légitime,
        #             `t_amt` valait encore 3030 et la borne -3030 ne mordait pas
        #   23:29:05  P1 revient à -11, équilibre atteint
        #   23:29:15  `amt` = 588 mais l'intégrale est restée à -1200 → sp = -611 → le moteur CHARGE
        #   23:29:17  P1 = +1061, et ~30 s d'import le temps que l'intégrale remonte à +120/cycle
        # Sur 811 653 cycles (5 traces, 30/07→09/08) : 800 cycles en DISCHARGE et 2010 en CHARGE
        # au-delà de la borne (0,35 % du temps), dépassement médian 54 et 114 W, pointes à 795 W,
        # 177 Wh d'import cumulé. Peu d'énergie, mais une inversion de consigne injustifiable.
        #
        # ⚠️ Le signe de la borne DÉPEND DU RÉGIME : en DISCHARGE l'intégrale positive veut dire
        # « décharge plus », en CHARGE « charge plus ». Pas de borne en IDLE : l'intégrale y est
        # gelée pour un éventuel retour au même régime (cf. `idle_hold`) et n'agit plus (`.46`).
        if self.regime == ManagerState.DISCHARGE:
            self.integral = max(self.integral, -max(0.0, t_amt))
        elif self.regime == ManagerState.CHARGE:
            self.integral = max(self.integral, -max(0.0, -t_amt))
        # --- GEL DE L'INTÉGRALE SUR TRAVERSÉE D'IDLE (26/07) ---
        # IDLE n'est pas un état de repos : la machine à états ci-dessus interdit CHARGE↔DISCHARGE en
        # direct, donc TOUTE inversion y transite. Remettre l'intégrale à 0 à chaque passage effaçait
        # la correction accumulée 27 fois par demi-heure, sur des séjours de 3 s.
        #
        # On ne peut PAS simplement conserver l'intégrale : son signe a un sens OPPOSÉ selon le régime
        # (en DISCHARGE, positif = « décharge plus » ; en CHARGE, positif = « charge plus »). La RAZ
        # existait précisément pour éviter cette contamination. On distingue donc les deux cas :
        #   - traversée COURTE qui revient au MÊME régime  -> on GÈLE (rien n'est perdu, aucun risque) ;
        #   - bascule vers le régime OPPOSÉ, ou séjour LONG -> on remet à 0 (comportement historique).
        # `idle_hold` = 0 désactive tout et redonne l'ancien comportement à l'identique.
        hold = self.idle_hold.asNumber
        if self.regime == ManagerState.IDLE and hold > 0:
            if regime_before != ManagerState.IDLE:
                self.idle_since = now          # on vient d'entrer : on note l'instant et d'où on vient
                self.idle_regime = regime_before
            if self.idle_since is None or (now - self.idle_since).total_seconds() >= hold:
                self.integral = 0.0            # séjour trop long : la situation a vraiment changé
                self.idle_regime = None
            # sinon : on ne touche PAS à l'intégrale (gel)
        elif self.regime != ManagerState.IDLE and self.idle_regime is not None:
            # sortie d'IDLE : on ne garde l'intégrale que si on retourne d'où l'on venait
            if self.idle_regime != self.regime:
                self.integral = 0.0
            self.idle_regime = None
            self.idle_since = None

        if self.regime == ManagerState.IDLE:
            if hold <= 0:
                self.integral = 0.0
        elif self.regime == ManagerState.DISCHARGE:
            if p1 > db_off and not sat_dis and not (en_vol and inflight > db_on):
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
                    self.integral = max(floor, self.integral - max(imin, min(-p1, imax)))
                    self.integral = max(self.integral, -dis_base)
        elif p1 < -db_off and not sat_chg and not (en_vol and inflight < -db_on):
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
                self.integral = max(floor, self.integral - max(imin, min(p1, 2 * step)))
                self.integral = max(self.integral, -charge_base)

        # --- consignes (sur la demande CONTRÔLABLE t_amt, pas house_load) ---
        cmd: dict[ZendureDevice, float] = dict.fromkeys(devices, 0.0)
        # Seuil d'engagement des PUITS, distinct de celui de la décharge (`min_engage`). Il sert dans
        # les deux régimes : le routage de surplus vit en DISCHARGE/IDLE, la charge du bus en CHARGE.
        me_chg = self.min_engage_chg.asNumber
        # IDLE passe aussi par ici : house_load ≈ 0 ne signifie PAS « pas de surplus solaire », mais
        # « bus équilibré » — typiquement parce que les APsystems couvrent la maison ET que le producteur
        # encaisse tout son solaire en interne. Sans ça, en IDLE on commandait 0 -> glagla encaissait
        # 975 W et up ne recevait rien (et le régime flappait sur le bruit P1). En IDLE l'intégrale est
        # nulle et demand ≈ 0 : l'étape 1) ne prend rien, seule l'étape 1bis route le surplus.
        #
        # ⚠️ 1.4.3.46 — « EN IDLE L'INTÉGRALE EST NULLE » N'EST PLUS VRAI DEPUIS `idle_hold`.
        # La phrase ci-dessus décrit l'invariante d'origine (RAZ systématique en IDLE). Depuis la
        # 1.4.3.32, `idle_hold` GÈLE l'intégrale sur une traversée courte — et cette formule, qui
        # est celle de la DÉCHARGE, se met alors à la dépenser. Or son signe a un sens OPPOSÉ selon
        # le régime où elle a été accumulée : en CHARGE, positif = « charge plus » (cf. le bloc
        # « GEL DE L'INTÉGRALE SUR TRAVERSÉE D'IDLE (26/07) », qui documente ce piège). Une intégrale de
        # 4080 héritée de CHARGE devenait donc 4080 W de DÉCHARGE commandés dès l'entrée en IDLE,
        # que le slew montait par paliers de 500.
        # MESURÉ 30/07→01/08 (54 h) : 12 emballements (`sp` +800 W en 10 s alors que |hl| < 300 W),
        # dont **6 en IDLE venant de CHARGE** — les 6 plus gros, `sp` jusqu'à 2910 W, intégrales de
        # 1044 à 4080. Le 01/08 à 14:19:30 : `regime=IDLE hl=45 amt=-61 int=4014 sp=2713`, SolarFlow
        # et `up` déchargeant 2994 W en plein export, P1 à −2385 W (contrat = export INTERDIT).
        # ⇒ `idle_hold` doit MÉMORISER l'intégrale (pour un retour au même régime), jamais la faire
        # AGIR. On la neutralise donc ici, sans toucher au gel lui-même.
        # ✅ NO-OP EXACT quand `idle_hold` = 0 (le défaut) : l'intégrale y vaut déjà 0 (l.985-986),
        # donc aucune régression possible sur le comportement historique.
        # ⚠️ Le jumeau `rem` (l.1265+) est dans la branche `elif regime == CHARGE`, inatteignable en
        # IDLE : il n'y a rien à corriger là-bas. `_direct_control` a ses propres formules (l.1652,
        # l.1680) et reste, lui, NON corrigé — cf. l'audit de cohérence ; il est inactif (`direct`=0).
        if self.regime in (ManagerState.DISCHARGE, ManagerState.IDLE):
            integ = 0.0 if self.regime == ManagerState.IDLE else self.integral
            demand = max(0.0, t_amt) + integ
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
                # En AUTORISÉ **ET PLEIN** le firmware ignore la consigne : le commander ne sert à
                # rien, et sa production est déjà comptée dans `forced`. L'inclure ici la compterait
                # deux fois.
                #
                # ⚠️ ANGLE MORT CORRIGÉ LE 26/07 (1.4.3.34) — la condition SOCFULL manquait ICI, et
                # seulement ici. `forced` (l. 562) l'exige ; cette exclusion ne l'exigeait pas. Un
                # producteur en autorisé mais NON plein tombait donc entre les deux :
                #   - absent de `forced`  -> le moteur ne croyait pas qu'il déversait ;
                #   - exclu de l'étape 1  -> le moteur ne lui demandait jamais rien.
                # Ni compté, ni commandé. Son solaire partait dans SA batterie pendant qu'une autre
                # se vidait pour la maison. Mesuré le 26/07, 16:49-18:00 : glagla 833 W de PV pour
                # 143 W de consigne, 686 Wh encaissés en interne (SoC 43->54 %), pendant que le
                # SolarFlow descendait 14->10 % (minSoc, 349 Wh) alors que la maison demandait
                # 514 W sur 79 % de ces instants.
                #
                # La prémisse « autorisé = non pilotable » ne vaut que BATTERIE PLEINE : sans
                # débouché interne, l'appareil déverse quoi qu'on dise (mesure du 20/07 : 719 W
                # sortis pour une consigne de 0 — sur un appareil SOCFULL). Non plein, il obéit et
                # met le reste dans sa batterie : mesuré le 26/07 18:05-18:51, glagla toujours en
                # gridReverse=1, 2749 points à consigne > 200 W, **99 % de la consigne livrée**,
                # écart médian -1 W. Les deux observations se réconcilient exactement sur SOCFULL.
                #
                # Aucun risque de double comptage : `forced` vaut 0 pour un appareil non plein.
                gr = d.entities.get("gridReverse")
                if d.state == DeviceState.SOCFULL and (getattr(gr, "value", None) == 1 if gr is not None else False):
                    continue
                # Même règle qu'en contrôle direct : son solaire + au plus `me` de batterie. On
                # PLAFONNE, on ne conditionne pas sur `drain_ema` — conditionner sur une grandeur que
                # la commande elle-même détermine fabrique un bang-bang (cf. 20:28 le 20/07).
                used = fuse_p1.get(d.fuseGrp, 0.0)
                ceiling = max(0.0, self.pv_ema[d.deviceId] - ovh) + (me if me > 0 else demand)
                # `_dis_ceiling` : plafond sur la livraison MESURÉE (cloud/dérating), cf. sa docstring.
                take = max(0.0, min(demand, ceiling, self._dis_ceiling(d), float(d.fuseGrp.maxpower) - used))
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
            # ⛔ 1.4.4.2 — `demand > 0` SAUTAIT LE ROUTAGE PENDANT UN EXPORT MESURÉ.
            #
            # `demand = max(0, t_amt) + integ` est bâti sur `house_load`, et house_load est AVEUGLE
            # au déversement autonome — c'est écrit plus haut dans ce fichier : « la sortie de
            # l'appareil s'ajoute dans house_load = P1 + Σhome, qui reste petit pendant qu'on
            # exporte 1000 W. La SEULE mesure qui voit l'export, c'est P1 ».
            # En IDLE `integ` est neutralisé (1.4.3.46), donc `demand = max(0, t_amt)` : un `t_amt`
            # résiduel de 9 W suffisait à sauter TOUT le routage du surplus. En DISCHARGE
            # l'anti-inversion (1.4.3.50) borne l'intégrale à `-max(0, t_amt)`, donc `demand >= 0`
            # par construction : le routage n'était atteint que sur l'égalité exacte.
            #
            # MESURÉ le 20/08 sur 22 h — 556 cycles où P1 valait **-618 W en moyenne** pendant que
            # le SolarFlow n'était commandé à RIEN (`cmd ≈ 0`, 2045 W de marge libre), `up` à
            # -489 W seulement et glagla en DÉCHARGE à +741 W. Régimes : DISCHARGE 398, IDLE 133.
            # Moyennes du moteur à ces instants : `hl=+116  amt=+336  int=+218` — il croyait que la
            # maison DEMANDAIT 116 W pendant qu'on exportait 618 W.
            #
            # ⇒ On entre AUSSI dans le routage quand P1 voit un export FRANC et PERSISTANT.
            # ⚠️ On ne force que l'ENTRÉE : tous les garde-fous internes (`surplus_on`/`surplus_off`,
            # `_engage_ok`, `chg_room`, `prod_extra`) continuent de décider s'il y a vraiment
            # quelque chose à router et vers qui. Si le parc ne peut rien prendre, `routable` vaut 0
            # et rien ne change.
            # ⚠️ PERSISTANCE OBLIGATOIRE : sans elle on réagirait au bruit de P1 et on ferait
            # démarrer un onduleur sur un transitoire de 1 s. `EXPORT_DWELL` couvre les rafales.
            if p1 < -self.db_on.asNumber:
                if self.export_since is None:
                    self.export_since = now
            else:
                self.export_since = None
            export_persistant = self.export_since is not None and (now - self.export_since).total_seconds() >= EXPORT_DWELL

            if demand > 0 and not export_persistant:
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
                        if not self._has_pv(d) and d.state != DeviceState.SOCFULL and not self._bypass_blocks(d) and d.electricLevel.asInt < 100
                    ]
                    # ⛔ 1.4.5.1 — CE TRI IGNORAIT L'HYSTÉRÉSIS, contrairement à son JUMEAU en
                    # branche CHARGE. Encore une décision écrite à deux endroits, appliquée à un
                    # seul : le sticky `clead` existait depuis toujours dans la répartition de
                    # charge, jamais ici. Or le matin le régime est DISCHARGE ~87 % du temps
                    # (mesuré le 12/09 : 19064 cycles sur 21962), donc TOUTE la charge du parc
                    # passe par cette étape — et la stratégie choisie par l'utilisateur n'était
                    # jamais appliquée.
                    #
                    # Effet du tri sur le SoC NU, avec deux puits partis du même niveau : le
                    # premier servi monte d'un point, repasse donc derrière l'autre, qui prend la
                    # main, monte d'un point, et ainsi de suite. Les deux montent ENSEMBLE au lieu
                    # qu'un seul soit rempli. Mesuré le 12/09 : **18 alternances** de puits dans la
                    # matinée (Pro 8 épisodes, up 6, glagla 5), durée médiane 167 s, et les SoC
                    # évoluent en parallèle — Pro 15 → 21 %, up 15 → 20 % — au lieu d'un seul à 30 %.
                    #
                    # `sinks` ne contient que des appareils SANS PV : le critère `_has_pv` de la
                    # branche CHARGE est déjà satisfait par construction, seule la clé de SoC reste.
                    if self.charge_strategy.value == 2:  # fixed_order : le plus gros d'abord
                        sinks.sort(key=lambda d: -d.kWh)
                    else:  # plus vide d'abord + sticky — MÊME clé que la branche CHARGE
                        hyst = 15 if self.charge_strategy.value == 1 else self.hyst_device.asNumber
                        sinks.sort(key=lambda d: d.electricLevel.asInt - (hyst if self.clead.get(d.deviceId) else 0))
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
                        alloue = 0.0  # ⚠️ ce qui est RÉELLEMENT parti dans un puits (cf. plus bas)
                        for d in sinks:  # les batteries sans PV absorbent
                            take = min(rem, chg_room(d))
                            # 1.4.4.4 : placer du solaire n'est pas la répartition ordinaire
                            if not self._engage_ok(d, take, SOLAR_ENGAGE, now, charge=True):
                                continue
                            cmd[d] -= take
                            rem -= take
                            alloue += take
                            fuse_chg[d.fuseGrp] = fuse_chg.get(d.fuseGrp, 0.0) + take
                        # ⚠️ Les producteurs ne sortent QUE ce qui a trouvé preneur. Avec
                        # `total_charge` ici, un puits écarté par le seuil d'engagement laissait le
                        # producteur sortir la part quand même : elle ne pouvait aller nulle part et
                        # partait AU RÉSEAU. Le seuil aurait fabriqué l'export qu'il doit éviter.
                        rem = alloue
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
                # `_dis_ceiling` au lieu de `discharge_limit` : un device bridé (cloud) ou dératé
                # accepte la consigne sans la livrer ; sans ce plafond la demande ne déborde jamais
                # sur les autres batteries et la maison importe.
                used = fuse_used.get(d.fuseGrp, 0.0)
                return max(0.0, min(self._dis_ceiling(d) - cmd[d], d.fuseGrp.maxpower - used))

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
                # --- 1.4.5.3 — SONDE : POURQUOI UN APPAREIL N'A-T-IL PAS REÇU DE PART ? -------
                # Question restée sans réponse après trois versions : sur 28 % des inversions de
                # tri mesurées, l'appareil laissé au repos était présent dans la liste, connecté
                # (`Conn=11`), en état `INACT`, avec un livrable intact (1201 W médian) — et il
                # n'était quand même pas servi. Rien dans le fichier ne permettait de le savoir :
                # la boucle `continue` sans laisser de trace, et l'absence de part ressemble à
                # toutes les autres absences de part.
                #
                # On enregistre donc, par appareil, la raison de son absence de part :
                #   ok=<W>   servi
                #   dem      la demande était déjà épuisée par les précédents (cas NORMAL)
                #   cap      `dis_cap` nul : plafond device, fusegroup ou livraison mesurée
                #   seuil(..) part trop petite pour un réveil (`min_engage`)
                #   dwell(..) besoin légitime mais pas encore assez durable (1.4.4.6)
                # Le rang dans le tri est joint : il dit si l'appareil était PREMIER servi ou non,
                # donc si l'ordre de la stratégie a été respecté.
                self.part_why.clear()
                for rang, d in enumerate(batt):
                    take = min(demand, dis_cap(d))
                    if not self._engage_ok(d, take, me, now):
                        self.part_why[d.deviceId] = f"{rang}:{self.engage_why.get(d.deviceId, 'eng')}"
                        continue
                    if take <= 0:
                        # take nul : soit plus rien à distribuer, soit aucune capacité. Les deux
                        # sont légitimes mais ne veulent PAS dire la même chose quand on enquête.
                        self.part_why[d.deviceId] = f"{rang}:" + ("dem" if demand <= 0 else "cap")
                        continue
                    self.part_why[d.deviceId] = f"{rang}:ok{take:.0f}"
                    cmd[d] += take
                    demand -= take
                    fuse_used[d.fuseGrp] = fuse_used.get(d.fuseGrp, 0.0) + take
            # ⛔ 1.4.5.4 — UN SEUL LEADER, PAS « TOUS CEUX QUI BOUGENT ».
            # `lead`/`clead` valaient « cet appareil a reçu une part », donc DÈS QUE DEUX
            # appareils sont servis au même cycle — ce qui arrive à chaque fois que le montant
            # dépasse la capacité du premier — ils deviennent TOUS DEUX leaders. Le bonus
            # d'hystérésis s'applique alors des deux côtés, s'annule, et le tri retombe sur le
            # SoC NU : les deux se suivent à un point près au lieu qu'un seul soit rempli.
            #
            # MESURÉ le 12/09 : `up` et `mrbig` progressent ensemble à 2 points d'écart, loin
            # des 15 attendus. Sur les 397 cycles où `mrbig` charge alors que `up` est plus vide,
            # l'écart médian de SoC est de 1 POINT — le sticky ne mordait plus du tout. Et 70
            # cycles de charge simultanée suffisent à marquer les deux appareils.
            #
            # Le leader est donc celui qui a été servi EN TÊTE du tri, et lui seul. Les suivants
            # ne reçoivent que le débordement : ce n'est pas un choix de stratégie, c'est une
            # conséquence de la capacité, et ça ne doit pas leur donner de priorité pour la suite.
            # 1.4.5.1 : `clead` doit être entretenu ICI aussi — l'étape 1bis donne des consignes
            # de charge en régime DISCHARGE (routage du surplus vers les puits sans PV), donc un
            # appareil qui y charge est bel et bien un leader de charge.
            self._maj_lead(devices, cmd, ovh)
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
                #
                # ⚠️ `+ cmd[d]` SUR LE PLAFOND AUSSI (28/07). Cette fonction renvoie la place ENCORE
                # disponible : les deux premiers termes en tiennent compte, le troisième était
                # ABSOLU. Quand le device a déjà reçu une part, `_chg_ceiling` ne la déduisait pas —
                # sans conséquence tant qu'il n'était pas le terme contraignant et que chaque device
                # n'était visité qu'une fois, mais faux dès la 2e passe (`parallel`) et dans le
                # `room_left` recalculé après allocation. `load_max` rend justement `_chg_ceiling`
                # contraignant en permanence : la latence serait devenue un vrai dépassement de
                # plafond. Sur le chemin principal (une visite, `cmd[d]` = 0) c'est un no-op exact.
                used = fuse_used.get(d.fuseGrp, 0.0)
                return max(0.0, min(-d.charge_limit + cmd[d], -d.fuseGrp.minpower - used, self._chg_ceiling(d) + cmd[d]))

            cstrat = self.charge_strategy.value
            if cstrat == 3:  # parallel : prorata place (100−SoC)×capacité, reliquat en 2e passe
                weights = {d: max(1.0, (100 - d.electricLevel.asInt) * d.kWh) for d in cand}
                total_w = sum(weights.values())
                share = rem
                for d in cand:
                    take = min(share * weights[d] / total_w if total_w > 0 else 0.0, chg_cap(d))
                    if not self._engage_ok(d, take, me_chg, now, charge=True):
                        continue
                    cmd[d] -= take
                    rem -= take
                    fuse_used[d.fuseGrp] = fuse_used.get(d.fuseGrp, 0.0) + take
                for d in cand:
                    take = min(rem, chg_cap(d))
                    if not self._engage_ok(d, take, me_chg, now, charge=True):
                        continue
                    cmd[d] -= take
                    rem -= take
                    fuse_used[d.fuseGrp] = fuse_used.get(d.fuseGrp, 0.0) + take
            else:
                if cstrat == 2:  # fixed_order : sans-PV d'abord, puis le plus gros (garde la marge PV)
                    cand.sort(key=lambda d: (self._has_pv(d), -d.kWh))
                else:            # hysteresis / hysteresis_wide : sans-PV d'abord, puis plus-vide + sticky
                    hyst = 15 if cstrat == 1 else self.hyst_device.asNumber
                    cand.sort(key=lambda d: (self._has_pv(d), d.electricLevel.asInt - (hyst if self.clead.get(d.deviceId) else 0)))
                for d in cand:
                    take = min(rem, chg_cap(d))
                    if not self._engage_ok(d, take, me_chg, now, charge=True):
                        continue
                    cmd[d] -= take
                    rem -= take
                    fuse_used[d.fuseGrp] = fuse_used.get(d.fuseGrp, 0.0) + take

            # 1bis (CHARGE) : le surplus du bus (export des micro-onduleurs tiers) est absorbé ci-dessus.
            # S'il RESTE de la place dans les batteries sans PV, y router aussi le solaire des producteurs
            # au lieu de les laisser l'encaisser (même seuil/hystérésis qu'en décharge). Si le bus a déjà
            # saturé les batteries (rem > 0), il ne reste pas de place -> pas de routage, repli naturel.
            sinks = [d for d in cand if not self._has_pv(d) and d.electricLevel.asInt < 100]
            # ⚠️ 1.4.3.47 — SERVIR D'ABORD CELUI QUI ABSORBE DÉJÀ SANS CONSIGNE.
            # Le solaire routé doit aller là où il est DÉJÀ consommé, sinon on fabrique de l'import :
            # le 06/08 11:10-11:18, `up` prenait 1200 W au réseau de sa propre initiative pendant que
            # ce routage envoyait les 1200 W de glagla au SolarFlow — 105 Wh d'import en 8 minutes.
            # Router vers `up` équilibre exactement ; router ailleurs ajoute une charge à une charge.
            # Tri STABLE : quand personne n'absorbe spontanément (le cas ordinaire), toutes les clés
            # valent 0 et l'ordre choisi par `charge_strategy` est conservé intact — no-op exact.
            sinks.sort(key=lambda d: -self._charge_subie(d))
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
                    alloue = 0.0  # JUMEAU de l'étape 1bis en DISCHARGE : ne faire sortir que le placé
                    for d in sinks:  # les batteries sans PV absorbent en plus
                        take = min(r, chg_cap(d))
                        # 1.4.4.4 : jumeau de l'étape 1bis en DISCHARGE — même règle, même raison.
                        # ⚠️ Les deux DOIVENT bouger ensemble : c'est la forme exacte de tous les
                        # bugs graves de ce fichier (une décision écrite à deux endroits, une seule
                        # corrigée).
                        if not self._engage_ok(d, take, SOLAR_ENGAGE, now, charge=True):
                            continue
                        cmd[d] -= take
                        r -= take
                        alloue += take
                        fuse_used[d.fuseGrp] = fuse_used.get(d.fuseGrp, 0.0) + take
                    r = alloue
                    for d, cap in prod_extra.items():  # les producteurs sortent leur solaire
                        extra = min(r, cap)
                        cmd[d] += extra
                        r -= extra
            self._maj_lead(devices, cmd, ovh)
        else:
            # IDLE — ON NE TOUCHE À RIEN (1.4.5.4). Cf. `_maj_lead`.
            pass

        await self._apply_and_report(devices, cmd, hl_raw, forced, t_raw, t_reg, p1)

    def _maj_lead(self, devices: list[ZendureDevice], cmd: dict[ZendureDevice, float], ovh: float) -> None:
        """Met à jour les hystérésis sticky `lead` (décharge) et `clead` (charge).

        ⛔ 1.4.5.4 — DEUX DÉFAUTS CORRIGÉS ENSEMBLE, parce qu'ils se masquaient l'un l'autre.

        1) UN SEUL LEADER, PAS « TOUS CEUX QUI BOUGENT ».
        `lead`/`clead` valaient « cet appareil a reçu une part ». Dès que DEUX appareils sont
        servis au même cycle — ce qui arrive chaque fois que le montant dépasse la capacité du
        premier — ils devenaient TOUS DEUX leaders. Le bonus d'hystérésis s'appliquait alors des
        deux côtés, s'annulait, et le tri retombait sur le SoC NU.
        Le leader est celui qui a reçu LA PLUS GROSSE part, et lui seul : les suivants ne
        reçoivent que le débordement, conséquence d'une limite de plaque et non d'un choix de
        stratégie — ça ne doit pas leur donner de priorité pour la suite.

        2) UN SENS NE DOIT PAS EFFACER L'AUTRE, ET L'IDLE NE DOIT RIEN EFFACER DU TOUT.
        Avant, la branche CHARGE forçait `lead = False`, la branche DISCHARGE forçait
        `clead = False`, et l'IDLE remettait les DEUX à False pour tout le monde. Or le régime
        oscille en permanence : MESURÉ le 12/09, **82 transitions de régime en 52 minutes, dont
        41 entrées en IDLE — une toutes les 38 secondes**. Le sticky était donc effacé avant
        d'avoir jamais pu s'établir, alors qu'il lui faut des HEURES pour creuser les 15 points
        d'écart de `hysteresis_wide`. Résultat constaté : `up` et `mrbig` progressaient ensemble
        à 2 points d'écart au lieu de 15, et sur les 397 cycles où `mrbig` chargeait pendant que
        `up` était plus vide, l'écart médian de SoC valait 1 POINT.

        ⇒ Un sticky ne se met à jour QUE s'il y a eu une décision dans son sens à ce cycle.
        Sinon il est laissé INTACT — c'est toute sa raison d'être : se souvenir de qui menait.
        """
        dis = [d for d in devices if (cmd[d] - max(0.0, self.pv_ema[d.deviceId] - ovh)) > 5]
        chg = [d for d in devices if cmd[d] < -5]
        if dis:
            meneur = max(dis, key=lambda d: cmd[d] - max(0.0, self.pv_ema[d.deviceId] - ovh))
            for d in devices:
                self.lead[d.deviceId] = d is meneur
        if chg:
            meneur = min(chg, key=lambda d: cmd[d])
            for d in devices:
                self.clead[d.deviceId] = d is meneur

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

        # --- PLAFOND DE SORTIE (1.4.3.52) — dernier rempart avant l'envoi ---------------------
        # ICI et nulle part ailleurs : `_apply_and_report` est le SEUL point de passage des DEUX
        # moteurs (`update` et `_direct_control` y arrivent tous les deux). Le plafonner en amont,
        # dans le calcul de `demand`, ne couvrirait que le moteur principal — c'est exactement la
        # forme qu'ont pris tous les bugs graves de ce fichier.
        #
        # APRÈS le slew, volontairement : la valeur plafonnée est celle qui part dans `cmd_applied`,
        # donc le cycle suivant rampe depuis le plafond et non depuis une consigne jamais appliquée.
        #
        # SOMME ALGÉBRIQUE, pas somme des positifs : si un producteur sort 1200 W pendant qu'un puits
        # en absorbe 1200, le net entrant dans la maison est nul — il n'y a rien à brider. Seul le
        # net peut devenir de l'export.
        #
        # AU PRORATA des seules consignes de décharge : ça préserve la répartition décidée en amont
        # (cascade, usure, priorité aux producteurs) au lieu de sacrifier un appareil. On ne touche
        # jamais aux consignes de charge — `out_max` n'empêche pas d'absorber du surplus.
        #
        # PRORATA SIMPLE, et pas « batterie d'abord, PV en dernier » : brider un producteur lui fait
        # écrêter ses propres panneaux (perte sèche), alors que brider une batterie ne perd rien.
        # La priorité serait donc plus juste EN THÉORIE — sauf qu'elle ne servirait jamais : quand
        # le plafond mord, le solaire est déjà éteint. Mesuré sur 6 jours, solaire moyen des Zendure
        # selon la décharge : 2500-3000 W -> 22 W ; 3000-3500 W -> 10 W ; > 3500 W -> 14 W. Et le
        # bridage se produit exclusivement entre 19 h et 23 h (0 Wh entre 10 h et 18 h). Ajouter un
        # ordre de priorité serait de la complexité sans effet mesurable.
        if (omax := self.out_max.asNumber) > 0 and (total := sum(cmd.values())) > omax:
            exces = total - omax
            pos = {d: c for d, c in cmd.items() if c > 0}
            if (somme := sum(pos.values())) > 0:
                for d, c in pos.items():
                    cmd[d] = c - exces * c / somme
                self.out_capped = exces  # tracé dans le message, pour pouvoir le mesurer ensuite
            else:
                self.out_capped = 0.0
        else:
            self.out_capped = 0.0

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
        # 1.4.4.8 — ENVOIS MQTT PERDUS, calcule ici et non dans le f-string : le message est une
        # concatenation implicite de f-strings, on n'y insere pas une expression conditionnelle.
        perdus = [(i, d) for i, d in enumerate(devices) if getattr(d, "publish_failed", 0) > 0]
        pub = " pub=" + "/".join(f"{self._tag(i, d)}:{d.publish_failed}" for i, d in perdus) if perdus else ""
        # --- 1.4.5.3 — SONDE D'INVERSION DU TRI -----------------------------------------------
        # N'apparaît QUE lorsqu'un appareil laissé au repos est PLUS PLEIN qu'un appareil servi,
        # d'au moins l'hystérésis de la stratégie. En dessous de cet écart, le sticky explique
        # l'ordre et il n'y a rien à voir : afficher `why` à chaque cycle noierait le signal dans
        # 480 000 lignes par jour, exactement comme `cap` et `pub` qui suivent la même règle.
        #
        # Ce qu'on lit : `rang:raison` par appareil. Le RANG dit si l'ordre de la stratégie a été
        # respecté (0 = premier du tri) ; la RAISON dit pourquoi la part n'est pas venue.
        # Un `0:dwell(3/15s)` signifie « il était bien premier, mais il attendait » ; un
        # `0:cap` signifie « premier, mais aucune capacité » — deux pistes très différentes.
        why = ""
        if self.part_why:
            hy = 15.0 if self.discharge_strategy.value == 1 else self.hyst_device.asNumber
            servis = [d for d in devices if cmd.get(d, 0.0) > 50]
            repos = [d for d in devices if abs(cmd.get(d, 0.0)) <= 50]
            if any(r.electricLevel.asInt - s.electricLevel.asInt > hy for s in servis for r in repos):
                why = " why=" + "/".join(
                    f"{self._tag(i, d)}:{self.part_why[d.deviceId]}"
                    for i, d in enumerate(devices)
                    if d.deviceId in self.part_why
                )
        self.debug = (
            f"fondation regime={self.regime.name} hl={int(hl_raw)} forced={int(forced)} T={int(t_raw)}"
            f" ema={int(t_reg)} amt={int(self.amt_ema) if self.amt_ema is not None else 0}"
            f" int={int(self.integral)} sp={setpoint} occ={self.occ:.2f}"
            f" vol={int(self.inflight)}{'*' if self.en_vol else ''}"
            # 1.4.4.8 : envois MQTT perdus, par appareil. `pub` n'apparait QUE s'il y en a - meme
            # regle que `cap` plus bas : a 480 000 lignes/jour on n'ajoute pas un champ vide.
            f"{pub}{why}"
            # ⚠️ CHAQUE VALEUR EST PRÉFIXÉE DU DEVICE (28/07). Ces trois champs ne listaient que les
            # devices DÉJÀ mesurés : 1 ou 2 valeurs pour 3 appareils, sans moyen de savoir à qui
            # elles appartenaient. Un contrôle qui indexait par position sortait de FAUSSES
            # violations. Le préfixe est `rang` + 4 lettres du dernier mot du nom : non ambigu même
            # si deux appareils portent un nom proche, et stable si un device disparaît de la liste.
            f" prod={'/'.join(f'{self._tag(i, d)}:{int(self.drain_ema.get(d.deviceId, 0))}' for i, d in enumerate(devices) if self._is_producer(d, datetime.now())) or '-'}"
            f" acc={'/'.join(f'{self._tag(i, d)}:{int(self.chg_accept[d.deviceId])}' for i, d in enumerate(devices) if d.deviceId in self.chg_accept) or '-'}"
            f" liv={'/'.join(f'{self._tag(i, d)}:{int(self.prod_accept[d.deviceId])}' for i, d in enumerate(devices) if d.deviceId in self.prod_accept) or '-'}"
            # ÉTAT + consigne par device : permet de repérer une consigne de CHARGE envoyée à un
            # appareil PLEIN sans avoir à recouper les colonnes (le champ debug a un cycle de retard
            # sur elles, ce qui rend tout recoupement fragile).
            f" st={'/'.join(f'{self._tag(i, d)}:{d.state.name[:5]}{int(cmd.get(d, 0)):+d}' for i, d in enumerate(devices))}"
            # `cap` n'apparaît QUE si le plafond a mordu : 480 000 lignes/jour, on n'ajoute pas
            # un champ constamment vide. Sa présence date et chiffre chaque bridage.
            f"{f' cap={int(self.out_capped)}' if self.out_capped > 0 else ''}"
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
            f" dws{self.dwell_sec.asNumber} sfd{self.sf_dismax.asNumber} idh{self.idle_hold.asNumber}"
            f" dpr{self.dis_probe.asNumber} imin{self.int_min.asNumber} wkg{self.wake_grace.asNumber}"
            f" dir{self.direct.asNumber} engc{self.min_engage_chg.asNumber}"
            f" lmx{self.load_max.asNumber}"
            f" omx{self.out_max.asNumber}"
            f" tf{self.timefast.asNumber}/{self.timezero.asNumber}"
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
            ("sf_dismax", self.sf_dismax), ("idle_hold", self.idle_hold), ("dis_probe", self.dis_probe),
            ("int_min", self.int_min), ("wake_grace", self.wake_grace), ("direct", self.direct),
            ("eng_chg", self.min_engage_chg), ("load_max", self.load_max), ("out_max", self.out_max),
            ("timefast", self.timefast), ("timezero", self.timezero),
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
        on écrête le reste (0 export). Généralisé à N entités de stockage (parc futur).

        ⚠️⚠️ DÉSACTIVÉ PAR DÉFAUT depuis la 1.4.3.37 (`fondation_direct` = 0). NE PAS RÉACTIVER
        SANS AVOIR LU CECI. Ce chemin a divergé de `update()` — voici ce qui lui MANQUE encore :

          - correctif B : intégrale négative bornée par `int_neg`   (ici : plancher `-imax`)
          - correctif B décharge : descente proportionnelle          (ici : `min(-p1, step)`)
          - `int_min` (1.4.3.35) : plancher de descente découplé de `step`
          - `_chg_ceiling` : son `chg_cap` local n'a AUCUN plafond d'acceptation mesurée
          - `_dis_ceiling` : les producteurs sont alloués sur la limite nominale
          - `_engage_ok`   : un puits est réveillé pour n'importe quel montant
          - `fuseGrp.maxpower` : non appliqué aux producteurs
          - gel d'IDLE (`idle_hold`)
          - `load_max` : la répartition locale (`chg_cap`, l. `min(-d.charge_limit, …)` et les
            consignes producteurs) alloue toujours sur la PLAQUE, hors anti-windup

        L'anti-windup (27/07) et son plafond d'occupation (28/07) ont été réalignés. Tant que le
        reste n'est pas propagé, réactiver
        `fondation_direct` ramène le moteur au comportement de début juillet sur ce cas précis.
        """
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
        # Capacité MOBILISABLE, comme dans `update()` (27/07). Ce chemin recalculait ses propres
        # seuils sur la somme des PLAQUES : le `imax` corrigé qu'on lui passe ne servait qu'au
        # plafond de l'intégrale, et l'anti-windup restait aveugle. Corrigé ici aussi pour que la
        # réactivation de `fondation_direct` ne soit pas un piège.
        # `_pmax` comme dans `update()` : le plafond d'occupation (`load_max`) vaut ici aussi, sinon
        # ce chemin rouvrirait exactement l'écart qu'on vient de refermer entre les deux moteurs.
        sat_dis = delivered >= sum(self._pmax(d) for d in devices if d.state != DeviceState.SOCEMPTY) - db_off
        sat_chg = delivered <= -sum(self._pmax(d, charge=True) for d in devices if d.state != DeviceState.SOCFULL) + db_off
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
                sinks.sort(key=lambda d: (self._has_pv(d), -d.kWh))
            else:                                 # sans-PV d'abord, puis plus-vide
                sinks.sort(key=lambda d: (self._has_pv(d), d.electricLevel.asInt))
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
        # 1.4.5.4 : MÊME règle que le moteur principal. `_direct_control` est un second moteur,
        # désactivé par défaut (`direct` = 0), et c'est précisément parce qu'il diverge en silence
        # qu'il a accumulé tous les bugs de juillet. On l'aligne donc ici aussi, même si le chemin
        # est aujourd'hui inactif — une divergence laissée en place est une régression en attente.
        self._maj_lead(devices, cmd, ovh)
        return forced, base, base
