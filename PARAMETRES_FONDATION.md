# Les paramètres du moteur « fondation » — guide complet

Tous ces réglages sont des entités du device **Zendure Manager** dans Home Assistant.
Ils sont **modifiables à chaud** : pris en compte au cycle suivant (~5 secondes), sans redémarrer, et **sauvegardés** au redémarrage.

> ⚠️ **Les valeurs par défaut ont été validées** sur des simulations et sur des traces réelles.
> Ne change **qu'un seul paramètre à la fois**, et observe une journée avant de conclure.

---

## Sommaire

1. [Le moteur en une page](#1-le-moteur-en-une-page)
2. [Glossaire — les mots que j'emploie](#2-glossaire--les-mots-que-jemploie)
3. [Les 11 paramètres, un par un](#3-les-11-paramètres-un-par-un)
4. [Les 2 sélecteurs de stratégie](#4-les-2-sélecteurs-de-stratégie)
5. [Les capteurs pour observer](#5-les-capteurs-pour-observer)
6. [Recettes : « j'observe X, je touche à quoi ? »](#6-recettes--jobserve-x-je-touche-à-quoi-)
7. [Pièges et interdits](#7-pièges-et-interdits)

---

## 1. Le moteur en une page

Ton installation :

| | rôle |
|---|---|
| **glagla** | Hyper 2000, 5,76 kWh, **avec panneaux solaires** |
| **up** | Hyper 2000, 3,84 kWh, **sans panneaux** |
| **APsystems ×2** | micro-onduleurs, produisent du solaire, **hors contrôle** du moteur |
| **Shelly P1** | mesure l'échange avec le réseau (+ = j'achète, − = j'exporte) |

Le boulot du moteur, 12 fois par minute : **amener P1 à zéro**. Ni acheter, ni exporter.

Pour ça il ne dispose que de deux leviers : dire à glagla **combien sortir**, et dire à up **combien charger ou décharger**.

Trois situations :
- la maison consomme plus que le solaire → **on décharge** les batteries ;
- le solaire dépasse la consommation → **on charge** les batteries ;
- c'est à peu près équilibré → **on ne fait rien** (mais on continue de ranger le surplus solaire, voir plus bas).

---

## 2. Glossaire — les mots que j'emploie

### Cycle
Un tour de boucle du moteur : il mesure, il décide, il commande. **Environ 5 secondes.**
Quand je dis « 3 cycles », lis « ~15 secondes ».

### Seuil (ou *deadband*, « zone morte »)
Une valeur en dessous de laquelle on ne fait rien.
*Exemple : si le déséquilibre est de 20 W, ça ne vaut pas la peine de réveiller une batterie. On laisse couler.*

### Hystérésis
**Deux seuils différents** : un pour **démarrer**, un plus bas pour **arrêter**.

C'est le principe du **thermostat de chauffage** : il déclenche à 19 °C, mais il ne coupe qu'à 21 °C. Sinon, à 19,0 °C pile, il s'allumerait et s'éteindrait 50 fois par minute.

Dans le moteur, c'est pareil : `seuil d'engagement` = 80 W pour démarrer, `seuil de coupure` = 30 W pour s'arrêter. Entre les deux, on **reste dans l'état où on est**.

### Flip-flap (ou battement)
Le défaut qu'on cherche à éviter : un appareil qui **démarre et s'arrête sans arrêt**. Ça use le matériel et ça ne régule rien. L'hystérésis existe pour ça.

### Lissage / EMA (« moyenne mobile exponentielle »)
Une façon de **calmer une mesure qui gigote**. On ne prend pas la mesure brute, mais un mélange : *un peu de la nouvelle valeur, beaucoup de l'ancienne.*

C'est ton **compteur de vitesse** de voiture : l'aiguille ne saute pas à chaque à-coup du moteur, elle est amortie.

`alpha = 40 %` veut dire : la nouvelle mesure pèse 40 %, l'historique 60 %.
- **alpha bas (10 %)** = très lisse, très lent à réagir.
- **alpha haut (90 %)** = suit le bruit, nerveux.

### Fast-track (« voie rapide »)
Le lissage a un défaut : il **retarde** aussi les vrais événements. Si le cumulus démarre (+2000 W d'un coup), on ne veut pas attendre.

Le fast-track dit : *« si le changement brut dépasse ±200 W, on ne lisse pas, on réagit tout de suite. »*
Le lissage ne s'applique donc qu'aux petites variations (le bruit), pas aux vrais événements.

### Feedforward (« anticipation »)
Le moteur **calcule directement** ce qu'il faut sortir : *« la maison demande 800 W, glagla en donne 800 »*. Il n'attend pas de voir l'erreur pour corriger. C'est l'essentiel du travail, et c'est instantané.

### Intégrateur
Le feedforward est bon, mais pas parfait : un Hyper qui reçoit l'ordre « sors 800 W » n'en sort peut-être que 780 (pertes, rampe, overhead).

L'intégrateur est un **correcteur d'écart cumulé** : à chaque cycle, il regarde P1, et il **ajoute un petit peu** dans le bon sens.

C'est le geste que tu fais **en douche** : l'eau est un peu froide, tu tournes un chouïa le robinet. Toujours un peu froide, tu tournes encore un chouïa. Jusqu'à ce que ce soit bon.

### Windup (« emballement de l'intégrateur ») et **anti-windup**
Le piège de l'intégrateur : imagine que ta douche soit **déjà à fond** sur le chaud, et que l'eau reste froide (chauffe-eau vide). Tu continues à tourner le robinet… dans le vide. Le robinet est en butée.

Puis le chauffe-eau se remplit : **tu te brûles**, parce que ton robinet est vissé à fond depuis 5 minutes.

C'est **exactement** ce qui arrivait au moteur : les deux Hyper étaient au maximum (2400 W), la maison demandait 2800 W, alors l'intégrateur gonflait pour obtenir plus… ce qu'aucun Hyper ne pouvait donner. Puis la conso chutait → **export de 1700 W pendant 90 secondes**, le temps que l'intégrateur se dévisse.

L'**anti-windup** est la règle : *« si le robinet est déjà en butée, arrête de tourner. »* C'est en place depuis la 1.4.2.9.

### Saturation
Quand un appareil est **à sa limite** et ne peut plus rien donner. Tes deux Hyper saturent à **1200 W chacun, soit 2400 W au total**. Une consommation de 2900 W provoquera **toujours** 500 W d'import — aucun réglage n'y changera rien.

### Dwell (« temps de maintien », « on tient la position »)
Une **obligation d'attendre avant de changer d'avis**.

Imagine un **conducteur qui hésite entre accélérer et freiner** à chaque nid-de-poule. Le dwell lui dit : *« tu ne passes de l'accélérateur au frein que si le besoin persiste 3 secondes. »*

Dans le moteur : up ne passe de **charge** à **décharge** que si la demande **persiste 3 cycles (~15 s)**.

Pourquoi ? Parce que ton **micro-ondes en décongélation** pulse ~5 secondes toutes les ~25 secondes. Le moteur voyait le pic **un cycle trop tard**, inversait up… juste quand l'impulsion s'arrêtait. Résultat : glagla poussait 1200 W + up déchargeait 400 W dans une maison **vide** → **1700 W exportés**. Le moteur fabriquait lui-même l'export qu'il devait éviter.

### Régime
L'état du moteur : **repos (IDLE)** / **décharge** / **charge**. Il est affiché par le capteur `Fondation : régime`.

### Surplus solaire, « encaisser », « routage »
Quand glagla produit plus que ce qu'on lui demande, son **firmware met le reste dans sa propre batterie** : on dit qu'elle **encaisse**.

Le problème : glagla se remplit toute seule, et à midi elle est pleine → elle écrête ou exporte son solaire. Pendant ce temps, **up reste vide** (il n'a pas de panneaux, il ne peut se remplir *que* depuis le bus).

Le **routage** consiste à dire à glagla : *« sors ce surplus »*, et à up : *« absorbe-le »*. On garde ainsi la place de glagla pour le gros soleil de midi.

Mais attention, ça **coûte deux conversions** (glagla continu→alternatif, puis up alternatif→continu). D'où un seuil : on ne le fait que si ça vaut le coup.

### Overhead
Ce qu'un Hyper consomme **juste pour être allumé** : environ **50 W**. Si le panneau donne 700 W, seuls ~650 W sont réellement utilisables.

### Fusegroup (« groupe de fusible »)
La limite électrique du circuit sur lequel est branché un Hyper. Chez toi, **chaque Hyper est seul sur son circuit** (« owncircuit »), avec une limite de 1200 W.

### Churn (« brassage »)
Importer **et** exporter dans la même minute. De l'énergie qui fait des allers-retours pour rien. C'est le symptôme d'un moteur qui court après des transitoires.

---

## 3. Les 11 paramètres, un par un

### 🟦 Groupe A — Quand agir (le régime)

---

#### `Fondation : seuil d'engagement`
**Défaut : 80 W** · plage 40 → 300 W

**À quoi ça sert.** En dessous de ce déséquilibre, le moteur **reste au repos**. Il faut dépasser ±80 W pour qu'il décide de décharger (ou de charger).

**Si tu montes (ex. 200 W)** — le moteur devient **tolérant** : il laisse passer les petits déséquilibres.
> *Concret : ta box, ton frigo en veille et tes LED tirent 120 W. Avec 80 W, le moteur les couvre. Avec 200 W, il ne bouge pas et tu achètes ces 120 W au réseau.*
> Avantage : moins de sollicitation des batteries. Inconvénient : petits achats permanents.

**Si tu descends (ex. 40 W)** — le moteur devient **pointilleux** : il réagit au moindre écart.
> Avantage : P1 plus proche de zéro. Inconvénient : les batteries travaillent en permanence pour 40 W, et tu t'approches du bruit de mesure.

**Quand y toucher.** Rarement. Si tu vois un import résiduel constant de ~100 W que tu voudrais couvrir → baisse un peu. Si tes batteries s'agitent pour trois fois rien → monte.

---

#### `Fondation : seuil de coupure`
**Défaut : 30 W** · plage 10 → 100 W

**À quoi ça sert.** Une fois le moteur en action, il ne revient au repos que si le déséquilibre **redescend sous 30 W**. C'est la moitié basse de l'**hystérésis** (le thermostat).

**La règle absolue : il doit rester nettement en dessous du seuil d'engagement.**
L'écart entre les deux (80 − 30 = 50 W) **est** ta marge anti-flip-flap.

**Si tu le montes trop près de 80** (ex. 70) — le moteur démarre à 80, s'arrête à 70… puis redémarre à 80. **Battement garanti.**

**Si tu le descends (ex. 10 W)** — une fois lancé, le moteur s'accroche très longtemps. Il ne lâche qu'à 10 W près.

**Quand y toucher.** Si tu vois le capteur `régime` sauter sans arrêt entre repos et décharge → **descends-le** (ou monte le seuil d'engagement).

---

#### `Fondation : seuil fast-track`
**Défaut : 200 W** · plage 100 → 1000 W

**À quoi ça sert.** Au-delà de ±200 W de changement **brut**, le moteur court-circuite le lissage et réagit **immédiatement**.

> *Concret : le cumulus démarre, +2000 W d'un coup. Le lissage aurait mis 3-4 cycles (~20 s) à s'en apercevoir. Le fast-track le voit tout de suite. Le lissage ne sert donc qu'à ignorer le bruit entre 80 et 200 W.*

**⚠️ Contrainte dure : il doit rester au-dessus du seuil d'engagement (80).**
Si tu le mets à 50, tout dépasse le fast-track en permanence → le lissage ne sert plus à rien → tu retrouves le battement.

**Si tu montes (ex. 500 W)** — seuls les très gros événements sont traités en urgence ; les changements moyens (300 W) sont lissés, donc réagis avec ~15 s de retard.

**Quand y toucher.** Testé de 200 à 400 W : quasi aucune différence. Laisse-le tranquille.

---

### 🟩 Groupe B — Stabilité (calmer le bruit)

---

#### `Fondation : lissage house load (alpha %)`
**Défaut : 40 %** · plage 5 → 100 %

**À quoi ça sert.** C'est le **compteur de vitesse** du moteur. Il lisse la mesure de la demande *pour la décision de régime uniquement* — les **montants** commandés, eux, restent calculés sur la mesure brute (donc pas de retard sur la puissance).

**Si tu descends (ex. 15 %)** — très lisse. Le régime devient stable comme un roc.
> Inconvénient : les vraies transitions passent avec du retard (sauf celles au-dessus du fast-track).

**Si tu montes (ex. 80 %)** — nerveux, suit le bruit. Le régime se met à sauter.

**Quand y toucher.** Si le capteur `régime` s'agite un jour de nuages passagers → **baisse à 25-30 %**.
C'est **le** paramètre à monter (plus de lissage = valeur plus basse) quand un appareil « pulseur » tourne (voir aussi le dwell).

---

#### `Fondation : lissage solaire (alpha %)`
**Défaut : 20 %** · plage 5 → 100 %

**À quoi ça sert.** Lisse la mesure du **solaire de glagla**, utilisée pour répartir entre les batteries.

> *Concret : au coucher du soleil, la production de glagla chute en dents de scie. Sans lissage, glagla et up se renvoyaient la balle à chaque oscillation — « c'est toi qui décharges », « non, toi ». Le lissage à 20 % coupe court.*

**Il est déjà très bas (donc très lisse).** Ça ne coûte rien : le solaire évolue lentement, on peut se permettre de le lisser fort.

**Quand y toucher.** Quasiment jamais. Si un jour très nuageux tu vois les batteries permuter souvent → descends à 10 %.

---

### 🟨 Groupe C — Précision

---

#### `Fondation : pas de l'intégrateur`
**Défaut : 120 W** · plage 20 → 300 W

**À quoi ça sert.** De combien l'intégrateur corrige **à chaque cycle** (le « chouïa de robinet »).

**Si tu montes (ex. 250 W)** — rattrape l'erreur résiduelle deux fois plus vite.
> Inconvénient : risque de **dépasser** la cible et d'osciller autour.

**Si tu descends (ex. 60 W)** — très doux, mais un écart de 500 W met ~8 cycles (40 s) à être comblé.

**Quand y toucher.** Presque jamais depuis l'anti-windup (1.4.2.9). Avant, on le montait pour vider l'intégrateur plus vite — c'était soigner le symptôme.
> ⚠️ Le plafond de l'intégrateur est fixé automatiquement à la capacité totale (2400 W). Il ne peut plus s'emballer.

---

#### `Fondation : overhead onduleur`
**Défaut : 50 W** · plage 0 → 150 W

**À quoi ça sert.** Ce que le moteur **retranche** du solaire mesuré pour connaître le solaire réellement utilisable.

> *Concret : glagla annonce 700 W de panneaux. Le moteur compte 650 W disponibles, parce que l'onduleur en consomme 50 pour tourner.*

**Si tu le mets trop bas (0)** — le moteur surestime le solaire et commande à glagla plus qu'elle ne peut sortir → petit import résiduel permanent (que l'intégrateur rattrape, mais mieux vaut être juste).

**Si tu le mets trop haut (150)** — il sous-estime → il laisse glagla encaisser 100 W qu'elle aurait pu router vers up.

**Quand y toucher.** Si tu ajoutes un **SolarFlow 2400 ou 4000** : leur consommation propre n'est pas la même que celle d'un Hyper. C'est le seul cas.
*Méthode de mesure : quand glagla produit sans décharger, compare `Prod` et `Home` dans le simulation.csv — l'écart, c'est l'overhead.*

---

### 🟪 Groupe D — Répartition entre batteries

---

#### `Fondation : hystérésis device`
**Défaut : 5 %** · plage 1 → 20 %

**À quoi ça sert.** Empêche les deux batteries de **permuter** sans arrêt.

> *Concret : glagla est à 51 % et up à 50 %. La règle dit « la plus pleine décharge » → glagla. Elle se vide, passe à 49 % → maintenant c'est up la plus pleine → up prend le relais → glagla remonte… et ça permute toutes les 10 secondes.*
> Avec 5 % d'hystérésis : la batterie **en cours** de décharge garde un bonus de 5 points. Elle ne cède la main que quand l'autre a vraiment 5 % de plus.

**Si tu montes (ex. 15 %)** — une batterie va beaucoup plus loin avant de passer la main. Moins de permutations, mais les SoC divergent davantage.

**Si tu descends (ex. 2 %)** — les SoC restent très proches, mais ça permute souvent.

**Quand y toucher.** Si tu vois les deux batteries se relayer plusieurs fois par heure → monte à 8-10 %.

---

### 🟥 Groupe E — Le surplus solaire

---

#### `Fondation : seuil routage surplus (engagement)`
**Défaut : 300 W** · plage 50 → 1500 W

**À quoi ça sert.** Le surplus solaire de glagla n'est envoyé vers up **que s'il dépasse 300 W**. En dessous, on laisse glagla l'encaisser elle-même.

**Pourquoi un seuil ?** Envoyer le surplus vers up coûte **deux conversions** (~10 % de pertes). Pour 80 W de surplus, ça ne vaut pas la peine : autant que glagla le range directement dans sa batterie (une seule conversion).

**Si tu montes (ex. 600 W)** — on ne route que les gros surplus. glagla se remplit davantage elle-même.
> Risque : glagla sature dès 14 h et se met à écrêter (solaire perdu) alors que up était encore vide.

**Si tu descends (ex. 150 W)** — on route très tôt. up se remplit vite, glagla garde sa place.
> Coût : plus de pertes de conversion sur les petits surplus.

**Quand y toucher — c'est le paramètre à surveiller.**
- Tu vois **glagla pleine à 100 % dès 14 h** et de l'écrêtage l'après-midi ? → **baisse le seuil** (200 W), tu routeras plus tôt.
- Tu vois **up charger par petites bouffées** de 300 W, sans arrêt, pour rien ? → **monte le seuil** (400-500 W).

---

#### `Fondation : seuil routage surplus (coupure)`
**Défaut : 150 W** · plage 20 → 1200 W

**À quoi ça sert.** L'autre moitié de l'hystérésis. Une fois le routage engagé, il **continue** tant que le surplus reste au-dessus de 150 W.

> *Concret : le surplus oscille entre 280 et 320 W à cause des nuages. Sans hystérésis, up démarrerait à 320 et s'arrêterait à 280, dix fois par minute. Avec la coupure à 150, up **reste** en charge : 280 est largement au-dessus.*

**Règle : garde un écart franc avec le seuil d'engagement.** 300/150 = un rapport de 2. Si ton ciel est très nuageux, élargis encore (par exemple 350/120).

**Si tu le montes trop près de 300** (ex. 280) — battement du routage garanti.

---

#### `Fondation : dwell d'inversion (cycles)`
**Défaut : 3 cycles (~15 s)** · plage 0 → 10 · **0 = désactivé**

**À quoi ça sert.** Une batterie ne passe de **charge** à **décharge** (ou l'inverse) que si le besoin **persiste** ce nombre de cycles.

> *Concret, mesuré chez toi le 08/07 : ton micro-ondes en décongélation pulse ~5 s toutes les ~25 s. Le moteur voyait le pic un cycle trop tard et inversait up… au moment où l'impulsion s'arrêtait. glagla poussait 1200 W, up déchargeait 400 W, la maison ne demandait plus rien → **1700 W exportés**. Sur 10 minutes : 62 Wh exportés dont **59 % fabriqués par le moteur lui-même**, et **28 inversions de up**.*
> Avec le dwell : up **garde sa consigne** pendant le pulse. Le réseau absorbe l'impulsion de 5 s (quelques Wh, inévitable — ni la cadence de 5 s ni la rampe du Hyper ne peuvent suivre). Résultat mesuré : **inversions 36 → 4**, **export −21 %**.

**Si tu montes (ex. 6 cycles ≈ 30 s)** — encore moins d'inversions, encore moins d'export fabriqué.
> Coût : quand une **vraie** charge durable démarre (cumulus), la batterie met 30 s à s'y mettre. Pendant ce temps tu achètes au réseau.

**Si tu descends à 0** — comportement d'avant : réactif, mais il fabrique de l'export sur chaque appareil qui pulse.

**Quand y toucher — c'est ton levier « appareil qui pulse ».**
- Lave-linge / sèche-linge / micro-ondes / lave-vaisselle en marche (charges qui cyclent) → **monte à 5-6**.
- Cumulus ou voiture électrique (charges **soutenues**) → **descends à 1-2** : la charge dure des heures, autant s'y mettre tout de suite.

> 💡 **C'est exactement ce que peut faire une automation HA** : « prise du lave-linge à ON → mets le dwell à 6 ; à OFF → remets 3 ». Aucun code à modifier dans le moteur.

---

## 4. Les 2 sélecteurs de stratégie

Ils décident **quelle batterie** travaille en premier. Ils ne changent pas *combien* — seulement *qui*.

### `Fondation : stratégie décharge`

| choix | qui décharge en premier | pour quoi faire |
|---|---|---|
| **Hystérésis** *(défaut)* | la plus **pleine** (+ hystérésis device) | équilibre les SoC, peu de permutations |
| **Hystérésis large** | idem, mais avec 15 % fixe | encore moins de permutations |
| **Ordre fixe** | la plus **grosse** (glagla) | comportement prévisible |
| **Parallèle** ⚠️ | les deux **au prorata** | zéro permutation… mais **deux fois l'overhead** (100 W au lieu de 50) et davantage d'achats transitoires. **Expérimental.** |

### `Fondation : stratégie charge`

| choix | qui charge en premier | pour quoi faire |
|---|---|---|
| **Hystérésis** *(défaut)* | la plus **vide** | équilibre les SoC |
| **Hystérésis large** | idem, 15 % fixe | moins de permutations |
| **Ordre fixe** | **celle sans panneaux** (up) d'abord | ⭐ **préserve la place de glagla** pour le solaire de midi |
| **Parallèle** ⚠️ | les deux au prorata | expérimental, voir ci-dessus |

> **Ton réglage actuel : Ordre fixe / Ordre fixe.** C'est cohérent avec ta stratégie « remplir up d'abord ».
> ⚠️ **Limite connue** : le test « sans panneaux » regarde le solaire **de l'instant**, pas « possède des panneaux ». La **nuit**, glagla est donc classée « sans panneaux ». Sans conséquence aujourd'hui (la nuit, il n'y a pas de surplus à router), mais c'est un correctif en attente.

---

## 5. Les capteurs pour observer

Avant de toucher à un réglage, **regarde**. Ces capteurs sont en lecture seule.

| capteur | ce qu'il te dit |
|---|---|
| **Fondation : régime** | repos / charge / décharge. **S'il saute sans arrêt → problème de stabilité** (voir `lissage house load`, `seuil de coupure`) |
| **Fondation : house load** | la demande **nette** de la maison (conso − solaire des APsystems). C'est l'invariant que le moteur doit couvrir |
| **Fondation : déversement forcé** | le solaire qu'un onduleur **plein** pousse malgré tout. Non nul = glagla est à 100 % |
| **Fondation : intégrateur** | la correction accumulée. **Doit rester proche de 0.** S'il monte à plusieurs centaines et y reste → tes Hyper sont saturés (conso > 2400 W), c'est normal, l'anti-windup l'empêche de s'emballer |
| **Fondation : setpoint** | la consigne totale envoyée aux onduleurs |

Et dans le fichier `simulation.csv` (option « simulation » cochée dans *Configurer*) :
la colonne **`bat` de glagla** doit rester **proche de 0** tant que up a de la place et que le surplus dépasse 300 W.
Si elle est très négative, c'est que glagla **encaisse** au lieu de router.

---

## 6. Recettes : « j'observe X, je touche à quoi ? »

| Ce que tu observes | Cause probable | Ce que tu changes |
|---|---|---|
| Le capteur `régime` saute repos↔décharge sans arrêt | bruit sur la mesure | `lissage house load` **40 → 25 %**, ou `seuil de coupure` **30 → 20** |
| Les deux batteries se relaient plusieurs fois par heure | hystérésis trop faible | `hystérésis device` **5 → 10 %** |
| glagla est **pleine à 14 h** et écrête l'après-midi | on route trop tard | `seuil routage surplus (engagement)` **300 → 200 W** |
| up charge par **petites bouffées** inutiles | on route trop tôt | `seuil routage (engagement)` **300 → 450 W** |
| Le routage démarre/s'arrête sans arrêt | hystérésis de routage trop serrée | `seuil routage (coupure)` **150 → 100 W** |
| Gros **export** quand un appareil pulse (micro-ondes) | le moteur inverse up trop vite | `dwell d'inversion` **3 → 6** |
| Le cumulus démarre et la batterie met du temps à suivre | dwell trop long | `dwell d'inversion` **3 → 1** (le temps du cumulus) |
| Import résiduel permanent de ~100 W | zone morte trop large | `seuil d'engagement` **80 → 50 W** |
| Import de 500 W quand la maison tire 2900 W | **saturation matérielle** | ❌ **aucun réglage.** Deux Hyper = 2400 W max. Il faut un onduleur de plus |
| `bat` de glagla très négatif alors que up est vide | le surplus n'atteint pas le seuil | `seuil routage (engagement)` à baisser |
| L'intégrateur reste bloqué haut | Hyper saturés (normal) | ❌ rien. L'anti-windup fait déjà le travail |

---

## 7. Pièges et interdits

**❌ Ne mets jamais `seuil fast-track` en dessous du `seuil d'engagement`.**
Tout dépasserait le fast-track → le lissage ne servirait plus à rien → battement.

**❌ Ne rapproche jamais `seuil de coupure` de `seuil d'engagement`.**
L'écart entre les deux **est** ta protection anti-battement. Idem pour le couple `routage engagement` / `routage coupure`.

**❌ Ne mets pas glagla en gridReverse « autorisé ».**
En *autorisé*, elle **ignore la consigne** et déverse tout son solaire au réseau. Garde **« désactivé »** ou **« interdit »** : dans ces modes elle obéit, et c'est ce qui permet au moteur de la piloter.

**❌ Ne mets pas un Hyper en fusegroup « unused ».**
Il devient **invisible** pour le moteur. Chaque Hyper doit être sur **« owncircuit »**.

**⚠️ La stratégie « Parallèle » est expérimentale.**
Zéro permutation, mais deux onduleurs allumés en permanence = **100 W d'overhead** au lieu de 50, et davantage d'achats transitoires sous bruit.

**⚠️ Une modification à la fois.**
Change un paramètre, observe **une journée entière** (le comportement du matin, de midi et du soir n'a rien à voir), puis conclus. Deux changements simultanés = tu ne sauras jamais lequel a agi.

**✅ Tout est réversible.** Les valeurs par défaut sont dans le tableau ci-dessous.

| paramètre | défaut |
|---|---|
| seuil d'engagement | 80 W |
| seuil de coupure | 30 W |
| seuil fast-track | 200 W |
| lissage house load | 40 % |
| lissage solaire | 20 % |
| pas de l'intégrateur | 120 W |
| hystérésis device | 5 % |
| overhead onduleur | 50 W |
| seuil routage surplus (engagement) | 300 W |
| seuil routage surplus (coupure) | 150 W |
| dwell d'inversion | 3 cycles |
| stratégie décharge / charge | Hystérésis *(chez toi : Ordre fixe)* |
