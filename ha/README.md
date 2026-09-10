# `zendure_reserve_matin` — garder de quoi tenir le matin en HP

Package Home Assistant. Pendant les **heures creuses**, il coupe le moteur Zendure pour
préserver dans les batteries l'énergie nécessaire au **matin en heures pleines**, jusqu'à ce
que le solaire prenne le relais.

Installation : déposer `zendure_reserve_matin.yaml` dans `config/packages/`, vérifier la
configuration, redémarrer HA, puis mettre `input_boolean.zendure_reserve_matin_active` sur
**on**. Tant qu'il est à `off`, les capteurs calculent mais rien ne touche au moteur.

---

## Pourquoi

| tarif | prix | plage |
|---|---|---|
| HC | 0,1376 €/kWh | 21:36 → 05:36 |
| HP | 0,1727 €/kWh | 05:36 → 21:36 |

Vider les batteries en HC pour économiser 0,1376 €/kWh, puis manquer d'énergie le matin et
acheter à 0,1727 €/kWh, c'est perdre l'écart sur chaque kWh. Deux cas où ça arrive :

1. **le cumulus chauffe** — 2400 W non modulables, que le moteur essaie de couvrir ;
2. **la nuit est longue** — le stock s'épuise avant l'aube.

---

## Ce qui est mesuré, et ce qui en découle

Toutes les valeurs viennent de `simulation.csv`, du 04 au 09/09/2026, sur la grandeur
`house_load` du moteur (elle est **déjà nette du solaire APsystems**).

### La conso nocturne ne dépend pas du moment de la nuit

Hors chauffe cumulus, médiane / moyenne / p90 en W :

| fenêtre | | | |
|---|---|---|---|
| 20:06-21:36 (soirée, HP) | 538 | 565 | 667 |
| 21:36-23:36 (début HC) | 531 | 454 | 563 |
| 23:36-01:36 | 529 | 537 | 616 |
| 00:00-05:35 | 521 | 527 | 575 |
| 01:36-05:36 (cœur de nuit) | 519 | 523 | 564 |

⇒ **On mesure la nuit en cours** sur une fenêtre glissante de 2 h, et on décide dès 21:36.
Une version antérieure relevait la conso à 05:35 pour la nuit *suivante* : inutile, ça
n'ajoutait qu'un délai de 22 h. Une autre attendait 30 min de HC avant de décider : retiré
aussi, ça coûtait **262 Wh** prélevés sur la réserve quand le stock est déjà bas à l'entrée
en HC et que le cumulus ne chauffe pas.

### Le cumulus fausse tout, il faut l'écarter

Moyenne de `house_load` sur 00:00-05:35 : **2294 W** la nuit du 05/09 (il chauffait) contre
**~530 W** les autres nuits — facteur 4,3. Le capteur
`sensor.zendure_house_load_hors_cumulus` renvoie `unavailable` dès que
`sensor.cave_cumulus_puissance` dépasse 200 W ; le capteur `statistics` écarte alors
l'échantillon.

Le cumulus démarre **à l'entrée en HC** quand la chauffe est demandée : il est actif **75 %
du temps entre 21:36 et 22:06**, et 62,7 % entre 21:36 et 23:36. D'où une automatisation qui
coupe sur l'**intention** (`input_boolean.cumulus_a_chauffer`) au basculement tarifaire, sans
attendre de mesurer sa puissance.

### Le matin ne consomme pas plus, il consomme par pointes

| créneau | médiane | moyenne | p90 |
|---|---|---|---|
| 00:00-05:30 (HC) | 470-540 | 525 | 550-600 |
| 06:00-08:30 (HP) | 488-541 | **569** | **800-900** |

La médiane est identique, la moyenne monte de 8 % et le p90 de 50 %. D'où un **facteur
matin de 1,15** plutôt qu'un second niveau de consommation.

### Énergie réellement consommée entre 05:36 et 09:00

**1632 · 1789 · 1932 · 2441 Wh** selon les jours. C'est le repère qui valide la réserve
calculée : `520 W × 1,15 × 3,40 h = 2033 Wh`.

---

## Le calcul

```
heure_solaire   = 1re demi-heure où la prévision Solcast dépasse le seuil (520 W)
durée           = heure_solaire − 05:36
réserve         = conso_retenue × facteur_matin × durée
couper si         énergie_disponible ≤ réserve
reprendre si      énergie_disponible > réserve × (1 + marge)
```

`conso_retenue` = moyenne glissante 2 h hors cumulus si elle dépasse 100 W, sinon la valeur
de repli. L'attribut `source` dit laquelle a servi.

> ⚠️ Ne jamais écrire `states('sensor.x') | float(520)` en espérant un repli : en Jinja le
> défaut ne joue **que si la conversion échoue**. Une valeur `"0.0"` se convertit très bien
> en `0.0` — la réserve vaudrait 0, la condition `dispo ≤ réserve` serait toujours fausse, et
> le moteur ne serait **jamais** coupé. C'est le premier défaut trouvé sur ce package.

> ⚠️ **Un `state:` qui renvoie la chaîne `"unavailable"` ne rend pas le capteur indisponible.**
> Avec une `unit_of_measurement` ou un `device_class`, HA attend un nombre, rejette la valeur
> et journalise `Received invalid sensor state: unavailable ... expected a number` — 11 fois
> en 7 minutes en production. La clé dédiée est **`availability:`** : quand elle rend `false`,
> l'entité passe proprement à `unavailable` et le capteur `statistics` écarte l'échantillon,
> ce qui était le but. Corrigé le 10/09 sur `zendure_house_load_hors_cumulus`.

---

## Les réglages

Aucun `initial:` : les valeurs survivent aux redémarrages. Le `min` fait office de valeur au
tout premier chargement.

| réglage | min = valeur nominale | effet d'une hausse |
|---|---|---|
| `zendure_conso_nuit_defaut` | 520 W | +1 W → +3,9 Wh de réserve |
| `zendure_facteur_matin` | 1,15 | +0,05 → +88 Wh |
| `zendure_solaire_seuil_w` | 520 W | heure solaire plus tard → réserve plus grande |
| `zendure_marge_reprise` | 5 % | reprise plus difficile |

> ⚠️ **Les `min` sont les valeurs nominales, et ce n'est pas un détail.** Tous ces paramètres
> poussent la réserve dans le **même sens** : une borne basse donne une réserve trop petite,
> donc une décharge trop longue et un manque le matin — l'inverse de la prudence. Constaté en
> production : `reserve_matin` affichait **1020 Wh** (= 300 × 1,00 × 3,40) contre 2033
> attendus, soit 62 % du besoin réel le plus faible. On ne peut désormais régler que vers le
> haut, donc que vers plus de marge.

---

## Les six automatisations

| déclencheur | action |
|---|---|
| entrée en HC **et** chauffe demandée | moteur `off` immédiatement |
| puissance cumulus > 200 W pendant 20 s, en HC | moteur `off` |
| puissance cumulus < 200 W pendant 1 min, réserve non atteinte | moteur `smart_fondation` |
| chaque minute en HC, `dispo ≤ réserve` | moteur `off` + notification |
| chaque minute en HC, `dispo > réserve × 1,15`, ≥ 15 min après la coupure | moteur `smart_fondation` |
| passage HC → HP (capteur Linky) | moteur `smart_fondation`, verrou relâché |

> ⛔ **Le moteur est `smart_fondation`, jamais `smart`.** `smart` est l'ancien moteur
> MATCHING de l'amont, sans aucun correctif. Les automatisations existantes
> `Zendure demarage HP up` et `Stop culumus HC , Start zendure` font cette erreur — elles
> sont désactivées depuis juin, ne pas les réactiver telles quelles. Au passage,
> `Zendure OFF HC` appelait `scene.zendure_arret_hc`, **qui n'existe pas** : elle ne coupait
> rien.

L'hystérésis et le délai de 15 min entre deux bascules ne sont pas décoratifs : décider
chaque minute sur un seuil nu ferait battre le moteur. C'est la leçon directe de la
`1.4.4.6`, où un engagement de moins de 15 s s'est révélé produire **0 %** de ce qui était
commandé.

---

## Nuit rejouée, minute par minute

Réserve 2053 Wh, conso 525 W :

| stock à 21:36 | déroulement | reste à 05:36 |
|---|---|---|
| 2000 Wh, chauffe 21:36→23:36 | coupe (cumulus) → reste coupé, réserve atteinte | 2000 |
| 2000 Wh, sans chauffe | coupe (réserve) à 21:36 | 2000 |
| 4000 Wh, chauffe | coupe → reprend à 23:36 → coupe (réserve) à 03:19 | 2049 |
| 9000 Wh, chauffe | coupe → reprend à 23:36, plus de coupure | 5850 |

---

## Entités créées

**Capteurs** — `zendure_house_load_hors_cumulus`, `zendure_conso_nuit_statistique`,
`zendure_conso_nuit_retenue`, `zendure_heure_solaire_suffisant`, `zendure_reserve_matin`,
`zendure_energie_disponible_wh`, `zendure_heure_coupure_hc_prevue`,
`zendure_autonomie_restante`.

**Réglages** — les quatre `input_number` ci-dessus, plus
`input_boolean.zendure_reserve_matin_active` (interrupteur général) et
`input_boolean.zendure_reserve_atteinte` (verrou de nuit).

## Dépendances

`sensor.zendure_manager_fondation_house_load` · `sensor.zendure_manager_available_kwh` ·
`select.zendure_manager_operation` · `sensor.cave_cumulus_puissance` ·
`input_boolean.cumulus_a_chauffer` · `input_boolean.cumulus_force` ·
`binary_sensor.linky_02330101298371_heures_creuses_actives` ·
`sensor.solcast_pv_forecast_previsions_pour_aujourd_hui` / `_demain`

> Solcast n'expose **pas d'entité** par demi-heure : le détail est l'attribut
> `detailedForecast`, produit tant que l'option `attr_brk_halfhourly` est active. Il est
> exclu du *recorder*, donc absent des états sauvegardés — ce qui ne l'empêche pas d'être lu
> par un template. L'option `attr_brk_detailed`, elle, ne contrôle que la ventilation **par
> site**. Repli sur `detailedHourly`, puis sur une durée de 3,5 h.
> Vérifié en production : `source_prevision: detailedForecast (30 min)`.
