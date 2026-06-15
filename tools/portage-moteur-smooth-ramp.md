# Plan de portage — moteur de régulation « smooth-ramp »

> Objectif : remplacer notre régulation actuelle (style fireson : `P1_STDDEV`/`isFast`
> + plancher/maintien) par le **pipeline documenté de `zoic21/auto-discovery`**
> (`distribution.py` + `docs/zendure_distribution_functional_design.md`), porté dans
> **notre** `manager.py` (on NE rebase PAS sur leur fork — cf. mémoire projet).
>
> Doc de référence amont : `git show zoic21/auto-discovery:docs/zendure_distribution_functional_design.md`
> Code de référence amont : `git show zoic21/auto-discovery:custom_components/zendure_ha/distribution.py`

## 0. Cible chiffrée (baseline cloud mesuré 14-15/06, à reproduire)
Capture 24 h, 2 Hyper en cloud + intégration MONITOR (17 k lignes) :
- **P1 médiane = 0 W**, moyenne −12 W ; **88 % dans ±100 W** ; export < −100 = 6,5 %.
- **Rampe douce** : médiane `|Δbat|/cycle` = 0 ; **83 % des cycles ≤ 20 W** ; > 200 W = 1,6 %.
- **Granularité ~10 W** (charge ET décharge), pas de plancher dur à 50.

C'est le **critère de succès** : le moteur porté doit s'en approcher (mesuré via
l'option `simulation` → mêmes stats que `tools/` d'analyse, ou InfluxDB).

## 1. Le pipeline cible (du doc zoic21)
```
P1 brut
  → correction sortie device + prises off-grid        (on GARDE : c'est notre setpoint calc actuel, steps 1-2)
  → EMA (α=0.3)                                        (remplace moyenne 5-points / stddev)
  → Rate Limiter (seuil 250 W, facteur 0.75)          (LA rampe douce — cœur anti-yo-yo)
  → σ sur 60 s (30 échantillons @2 s)                 (alimente deadband + hysteresis)
  → Hysteresis : filtre de pics 2σ + seuil de durée   (anti micro-pics / appareils cycliques)
  → Deadband (shift saisonnier + largeur adaptative)  (viser P1≈0 ; REMPLACE plancher/maintien)
  → clamp selon mode (MATCHING / CHARGE / DISCHARGE…) (on GARDE notre logique de modes)
  → Direction Change Guard (gel 8 s sur grosse chute) (REMPLACE le sign-change reset)
  → algo de distribution                              (on GARDE l'essentiel, à réconcilier)
```

## 2. Correspondance avec NOTRE code actuel (`manager.py`)
| Notre code aujourd'hui | Devient |
|---|---|
| `_p1_changed` : calcul stddev, `isFast`, `zero_next`/`zero_fast` | EMA + Rate Limiter + σ (cadence ~2 s) |
| `SmartMode.P1_STDDEV_*`, `TIMEFAST`, `TIMEZERO` | `α`, `rate_limit_threshold/factor`, params deadband |
| `charge_floor` / `charge_hold_window` + `charge_hold_until` (anti cloud-flicker) | **Deadband + hysteresis** (le maintien propre) → à **retirer** une fois le deadband en place |
| reset sur changement de signe / vidage d'historique (`isFast` clear) | **Direction Change Guard** |
| `powerChanged` steps « setpoint = p1 - sorties + … » | **GARDÉ** (correction device = step 1-2 du pipeline) |
| `power_charge`/`power_discharge` (distribution + clamps SOCFULL, dev_start) | **GARDÉ**, à brancher sur le setpoint lissé |

À **préserver absolument** (nos features, orthogonales au moteur) :
- télémétrie : `setpoint` sensor, `simulation.csv` (Conn/ChLim), exporteur InfluxDB ;
- `home_power_net`, `weighted_soc`, `usable_energy` ;
- watchdog MQTT + cold-start + watchdog de commande ;
- modes (MONITOR, etc.), notifications, select `soc_full_strategy` (forbidden no_export).

## 3. Constantes (valeurs zoic21, à mettre en `const.py`)
| Param | Défaut | Note |
|---|---|---|
| EMA α | 0.3 | Slow 0.1 / Medium 0.3 / Fast 0.5 |
| Rate limit seuil | 250 W | au-delà → amortit |
| Rate limit facteur | 0.75 | fraction du delta appliquée |
| σ fenêtre | 60 s (30 éch.) | informe deadband + hysteresis |
| Deadband shift | Hiver −50 / Été +50 / Mini ±150 | saisonnier |
| Deadband gentle drift | **15 W/cycle** | ≈ notre baseline cloud (≤20 W) |
| Hysteresis pic | 2σ | seuil de détection |
| Hysteresis durée | 30 s (Moderate) | Precise/Moderate/Relaxed |
| Direction guard seuil | 250 W | déclenche le gel |
| Direction guard durée max | 8 s | |
| Cycle | 2 s | recalcul distribution |
| `POWER_START` | 50 W | impulsion démarrage (gardé, AMONT) |
| Mini physique | 55 W | en-dessous = inefficace |

## 4. Découpage incrémental (1 couche = 1 release = 1 validation terrain)
Chaque couche : implémenter → activer `simulation` → comparer aux stats baseline → release.

- **P0 — Préparation** : feature flag (cf. §5) ; structurer le pipeline dans une méthode dédiée sans changer le comportement (passthrough).
- **P1 — EMA** : remplace le lissage moyenne/stddev. Critère : pas de régression, signal plus propre.
- **P2 — Rate Limiter** : **la pièce anti-yo-yo**. Critère : `|Δbat|/cycle` chute vers le profil cloud (≤20 W majoritaire).
- **P3 — σ + Deadband** : viser P1≈0, **retirer `charge_floor`/`charge_hold`**. Critère : % dans ±100 W qui grimpe vers 88 %.
- **P4 — Hysteresis** : filtre pics + durée. Critère : moins de réactions aux appareils cycliques.
- **P5 — Direction Change Guard** : anti-bascule charge↔décharge. Critère : plus d'oscillation de signe.
- **P6 — Nettoyage** : retirer constantes mortes (`P1_STDDEV_*`, `TIMEFAST/ZERO`, plancher/hold), maj traductions, doc.

## 5. Dé-risquage : feature flag « ancien / nouveau moteur »
Ajouter un select manager `regulation_engine` (`legacy` défaut / `smooth`) :
- permet de **basculer en direct** entre l'ancien moteur et le nouveau sur le système réel ;
- **A/B terrain** : comparer les stats `simulation.csv` des deux sur les mêmes conditions ;
- rollback instantané sans toucher HACS. **À garder tant que le smooth n'est pas validé**, puis retirer.

## 6. Points de décision (à trancher en chemin)
- **Nos divergences vs fireson** (1.3.1.14 compensation `p1<0`, 1.3.1.17 `discharge_optimal` SOCFULL) : le nouveau pipeline les rend peut-être caduques → réévaluer couche par couche, ne PAS les jeter aveuglément.
- **#1151 / discharge_bypass** : réconcilier avec patch-2 (cap + inconditionnel) **dans ce cadre** (cf. mémoire : passage A→B = 1 ligne + re-valider import). Le forbidden `no_export` doit survivre.
- **Cadence** : zoic21 = 2 s fixe ; nous = event-driven sur changement de P1. Choisir (probablement timer 2 s + dernier P1).
- **Deadband saisonnier** : exposer en option (comme `charge_floor`) ou auto.

## 6bis. Décisions de design (réflexion 15/06 — structurantes)
Mesure clé : sur la capture cloud 24 h, le cloud **récupère d'un sur-débit en ~5-6 s** (médiane 5, max 6) et ne laisse **jamais l'export dépasser ~280 W** (≈ ~4 s de réponse matérielle + 1-2 cycles → **hardware-limited**). Un EMA symétrique naïf ferait ~12-16 s / jusqu'à 2400 W → **inacceptable**. D'où :

1. **Réponse ASYMÉTRIQUE (magnitude)** : **reculer (vers 0) = RAPIDE** (1-2 cycles, fast-path, on ne fait pas mieux que le ~4 s matériel) ; **avancer (loin de 0) = LENT/lissé** (EMA + rate-limiter, anti-yo-yo). Vaut dans les **deux modes** (réduire un débit OU une charge = rapide).
2. **Loi à 2 vitesses + flip relais GARDÉ** : le **flip de mode charge↔décharge** (acMode 1↔2 = claquement relais) ne se fait **que sur demande opposée CONFIRMÉE soutenue** (hysteresis/direction-guard), **jamais sur un transitoire**, **symétrique** dans les deux sens. → subsume notre fast-down décharge **et** le patch relais zoic21 (côté charge) en **une seule règle** : *recule vite, avance doucement, ne flippe que sur confirmation.*
3. **État « neutre armé » = rester dans le mode courant à magnitude mini** (`discharge(x)` ou `charge(x)`, acMode tenu) → ré-engagement **instantané**, **zéro claquement**, recalcul tranquille depuis ce point. **Vérifier** : à `power==0` le code force `smartMode:0` (device.py:798/791) — si ça **ouvre** le relais, armer à **~50 W** (`POWER_START`) comme keep-alive, ou commande de maintien `smartMode:1, acMode:<courant>, outputLimit/inputLimit:0`.
4. **Biais ANTI-EXPORT (deadband shift)** : cible de régulation = **P1 = +ε (léger import)**, pas 0 → le bruit oscille dans le positif, **ne franchit jamais 0 vers l'export**. = le **deadband shift** zoic21, **configurable**. NB : le cloud mesuré biaisait **−12 W** (léger *export*, toléré par Zendure) ; **nous on veut l'inverse** (biais **+**, no-export, pas de tarif rachat) → coût = petit import permanent de quelques W, **assumé**. Complète le select `no_export` (forbidden) : 2 garde-fous (cible de régul + redirection surplus).

## 7. Validation finale
- `simulation` ON sur plusieurs jours, recalculer les stats baseline (P1 distrib, `|Δ|/cycle`, granularité) et **comparer au cloud**.
- Vérifier les cas limites : glagla full + forbidden (no_export), up qui décroche (le moteur ne doit pas s'emballer), faible soleil (plus de yo-yo 0↔50).
- Garder `legacy` accessible jusqu'à validation complète.

---
*Réf. mémoire projet : « PRIORITÉ zoic21/auto-discovery », « BASELINE CLOUD mesuré », « EXPORT EDF forbidden ».*
*Rappel : `git fetch zoic21 --prune` avant de relire leur code (miroir local périme vite).*
