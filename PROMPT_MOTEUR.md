# Prompt — concevoir un moteur de pilotage photovoltaïque multi-batteries

> Ce document est un **cahier des charges** destiné à une IA. Il ne décrit pas seulement
> ce que le moteur doit faire : il intègre dans le raisonnement de conception tout ce
> qui a été appris **en corrigeant** une première implémentation. Chaque contrainte
> ci-dessous vient d'une mesure de terrain, pas d'une intuition. Les respecter dès la
> conception évite de réécrire quatorze fois le même correctif.

---

## Mission

Conçois le moteur de régulation d'une installation photovoltaïque résidentielle
comportant plusieurs batteries-onduleurs hétérogènes. Objectif : **maintenir la
puissance au compteur (P1) au plus près de zéro**, en évitant à la fois l'import
réseau et l'export non rémunéré, tout en préservant la longévité des batteries.

Tu produiras : l'architecture, les structures de données, la boucle de régulation,
et la justification de chaque choix au regard des contraintes ci-dessous.

---

## 1. Le principe fondateur — à graver avant d'écrire une ligne

> **Ne dimensionne JAMAIS une décision sur une valeur que tu viens d'écrire toi-même.
> Chaque grandeur qui sert à répartir la puissance doit être une MESURE que la commande
> ne peut pas altérer.**

C'est l'unique cause profonde des quatorze correctifs de la première implémentation.
Elle s'est manifestée sous quatre formes différentes, toutes du même schéma :

| Ce qui était dimensionné sur une intention | Conséquence mesurée |
|---|---|
| capacité d'un puits sur sa limite **nominale** | 900 W exportés pendant 50 s alors qu'un autre puits était libre |
| production d'un producteur sur son **PV mesuré** | 220 W importés en permanence |
| seuil d'engagement sur une grandeur **que la commande détermine** | oscillation bang-bang, consignes de 0 à 1000 W toutes les 5 s |
| absorption d'un puits sur la **consigne** d'un producteur | import permanent, incorrigible |

### Mesures FIABLES (non suppressibles par la commande)

- **P1**, la puissance au compteur — la seule vérité du système.
- **Le flux batterie** d'un appareil : `batteryOutput − batteryInput`.
- **Ce qui est réellement livré ou absorbé** : `homeOutput − homeInput`.

### Mesures TRAÎTRES (altérées par ta propre commande)

- **La production solaire déclarée.** Un appareil sans débouché **écrête ses propres
  panneaux**. Mesure : 773 W en mode « export autorisé » contre **152 W en mode
  interdit**, même soleil, même appareil, même journée. Si tu commandes 0, la
  production s'effondre, tu en déduis « rien à prendre », et tu commandes 0.
  **Boucle qui se mord la queue.**
- **Les limites nominales déclarées** (`ChLim`, `inverseMaxPower`). Un appareil annonce
  −2400 W de capacité de charge alors qu'il n'accepte plus que 292 W. Le refus n'est
  **jamais annoncé, seulement constatable**.

### Corollaire de conception

Pour savoir ce qu'un appareil *peut* faire, il faut **lui en demander plus et observer**.
D'où le patron à appliquer partout :

```
plafond[device] = mesure_observée + marge_de_ré-exploration
    il suit    -> retenir le max observé (remontée immédiate)
    il refuse  -> après N cycles consécutifs de confirmation, descendre D'UN COUP
```

**La marge de ré-exploration est asymétrique**, et c'est essentiel :
- offrir trop à un **puits** est gratuit — il refuse, c'est tout → marge large (~150 W) ;
- demander trop à un **producteur** fabrique **directement** de l'import → marge étroite (~40 W).

**Pas de moyenne glissante sur ce plafond.** Une EMA à α=0,10 met ~50 s à descendre,
soit exactement le délai qu'on cherche à supprimer. Confirmation sur N cycles puis
bascule immédiate.

---

## 2. Les appareils ne « décident » rien — mais ils subissent leur physique

Distinction cruciale : un appareil qui livre moins que demandé **n'est pas désobéissant**.
Il rencontre une limite physique que rien n'annonce. Ne conçois pas de logique
« punitive » ; conçois une logique qui **mesure et s'adapte**.

### Dérating thermique (mesuré sur 47 875 points)

| Température | Sortie AC | % du nominal |
|---|---|---|
| 43-60 °C | 1199 W | **100 %** |
| 61 °C | 1094 W | 91 % |
| 62 °C | 994 W | 83 % |
| 63 °C | 925 W | 77 % |
| 64 °C | 878 W | **73 %** |

Palier parfaitement plat jusqu'à 60 °C, effondrement au-delà, plafonnement de la
température à 64 °C. `corr(écart, température) = −0,927`.

**L'appareil ne perd rien** : il *redirige* vers sa batterie ce qu'il ne peut plus
sortir en AC. C'est l'étage onduleur qui est bridé, pas le chemin DC.

⚠️ **Piège d'analyse à ne pas refaire** : une première conclusion attribuait ce bridage
à une *conjonction* température + état de charge. Faux — le SoC montait simplement en
même temps que la température. **Toujours tester la variable confondante** en isolant
des sous-populations à SoC constant.

### Tapering de charge en fin de remplissage

| SoC | Charge acceptée max |
|---|---|
| ≤ 93 % | 1300-1600 W |
| 94 % | 1167 W |
| 96 % | 808 W |
| 97 % | **292 W** |

La limite déclarée reste à −2400 W **quel que soit le SoC**. L'état interne ne passe pas
en « plein ». Il existe donc un **angle mort entre 95 % et 100 %** où le moteur croit
disposer de toute la capacité.

Attention : le tapering plafonne le **débit**, pas la destination. L'appareil monte bien
à 100 % si on ne lui offre pas plus que son plafond du moment.

### Modes d'export (comportements radicalement différents)

| Mode | Comportement | Traitement moteur |
|---|---|---|
| **autorisé** | déverse tout son solaire, **ignore la consigne** (719 W sortis pour une consigne de 0) | non pilotable : le comptabiliser, l'exclure de la répartition |
| **désactivé** | obéit à la consigne, **donne accès à sa batterie** (~1 min de latence avant qu'elle démarre) | seul mode réellement pilotable |
| **interdit** | obéit, mais **bloque aussi la sortie batterie** | pas de réserve mobilisable |

### Autres comportements à intégrer

- Un appareil plein active son **mode bypass**. Ne l'exclus pas des puits pour autant :
  s'il n'a pas de panneaux, il reste un puits parfaitement valide.
- **Une consigne nulle doit être réellement transmise.** Si elle est filtrée, l'appareil
  reste figé sur sa dernière consigne non nulle — observé : commandé 0, il continue de
  sortir 155 W indéfiniment.
- Latence d'actionneur réelle : **2 à 6 s** (médiane 4 s) sur un échelon de consigne stable.

---

## 3. La boucle de régulation

### Cadence

- Le compteur publie **toutes les 1 s**.
- Le moteur recalcule toutes les **4-5 s** (temporisation + chemin rapide sur gros écart).
- Le traitement d'un cycle prend **~600 ms**, essentiellement des entrées/sorties réseau
  vers les appareils. C'est un appel bloquant au milieu de la boucle : prévois-le.

⚠️ **Piège de quantification** : une échéance de 4 s n'est évaluée qu'à l'arrivée d'une
mesure. Avec un compteur à 2 s, le cycle réel devient 6 s ; à 1 s, il devient 5 s. Un
seuil « chemin rapide » à 2,2 s est **inatteignable** si les mesures arrivent toutes les
2 s. Vérifie toujours la cadence réelle, pas la valeur théorique.

### Grandeur pivot — et son angle mort

```
charge_maison = P1 + Σ(sorties_appareils)
```

Invariant censé être indépendant des batteries — mais **il ne l'est que partiellement** :
mesuré, une variation de commande de 600-1200 W provoque un rebond de ~160 W sur cette
grandeur au cycle suivant. Contamination faible mais réelle.

⚠️ **Bien plus grave — cette grandeur peut mentir complètement.** Situation mesurée :

```
charge_maison = -62 W   (le moteur conclut : « maison équilibrée », régime repos)
P1            = -749 W  (le compteur exporte 750 W depuis 3 minutes)
```

Les deux sont arithmétiquement cohérents : le moteur commandait 850 W de sortie à un
producteur, cette sortie **entre dans le calcul de `charge_maison`**, et l'équilibre
apparent est donc **fabriqué par la commande elle-même**. C'est le principe fondateur
violé sur la grandeur pivot.

Conséquence en cascade : le régime reste au repos, or le repos remet l'intégrateur à
zéro — **la seule grandeur qui voit le problème (P1) est neutralisée toutes les quelques
secondes**, et l'erreur peut durer indéfiniment.

**Conception à retenir** : la machine à états ne doit pas décider sur `charge_maison`
seul. Prévois un garde-fou sur **P1 directement** — si |P1| reste élevé alors que la
grandeur pivot dit « équilibré », c'est la grandeur pivot qui a tort.

### Machine à états

Trois régimes (charge / repos / décharge) avec **hystérésis sur le signal lissé** et
**chemin rapide sur le signal brut** pour les gros sauts.

⚠️ **Le signe de l'intégrateur est OPPOSÉ selon le régime.** En charge, intégrale
positive = « charge plus ». En décharge, intégrale positive = « décharge plus ». Ne
transporte jamais la valeur d'un régime à l'autre sans la remettre à zéro — et si tu
écris un simulateur, c'est la première erreur que tu feras.

### Intégrateur — les deux pièges

**Piège 1 : le plancher à zéro.** Si l'intégrale ne peut pas devenir négative en régime
charge, **un import permanent est structurellement incorrigible** : le moteur le voit,
ne peut rien en faire, et l'erreur dure des heures. Mesuré : +266 W pendant 86 min,
intégrale à zéro sur 92 % des cycles. Autorise le signe négatif, avec anti-windup
symétrique.

**Piège 2 : l'emballement pendant une rampe.** Si un limiteur de vitesse borne la
progression de la consigne, l'intégrale continue de gonfler pendant toute la rampe —
elle interprète l'erreur persistante comme « il faut demander plus », alors que la
commande est simplement *en route*. Mesuré : 787 W accumulés en 8 cycles, puis
dépassement à −623 W d'export. **Gèle l'intégrale tant que la consigne appliquée n'a pas
rejoint la consigne calculée.**

### Anti-windup

Ne pas accumuler quand la sortie est saturée. ⚠️ Mais **teste la saturation sur la
capacité réelle, pas nominale** : un appareil en tapering n'est pas « saturé » selon ses
limites déclarées, alors qu'il refuse tout supplément.

---

## 4. Cascade de répartition

Ordre demandé (personnalisable) :

1. **Le producteur d'abord** : son solaire est gratuit, une seule conversion.
   Demande-lui ce dont la maison a besoin — **c'est lui qui arbitre** entre son solaire
   et sa batterie.
2. **Sa batterie ensuite**, jusqu'à un seuil (~300 W). Au-delà, il n'a plus d'avantage
   sur les autres : rends la main à la stratégie normale.
3. **Les batteries sans panneaux** en dernier recours (deux conversions, ~10-15 % de
   pertes).

⚠️ **Le seuil doit PLAFONNER, pas CONDITIONNER.** Écrire
`si soutirage ≥ seuil alors 0 sinon montant` produit un **bang-bang** : on coupe, la
grandeur retombe, on réengage, elle remonte. Mesuré : consignes 867 / 292 / 0 / 1000 /
500 / 0 en 30 s. Écrire `montant = min(demande, seuil)` rend le garde-fou inutile **par
construction** — le soutirage ne peut plus dépasser le seuil.

Règle générale : **ne conditionne jamais une décision sur une grandeur que cette
décision produit.** C'est la même erreur que le point 1, sous forme de seuil.

### Seuil d'engagement minimum

Un onduleur consomme ~50 W pour fonctionner. Le réveiller pour en sortir 50 W est une
perte nette. Seuil ~300 W — mais **appliqué à la part reçue par l'appareil**, pas au
besoin total. Mesuré : un besoin de 345 W passait le seuil, puis la répartition
attribuait 86 W (parfois 3 W) à un appareil, qui démarrait pour rien 95 % du temps.

---

## 5. Architecture — trois règles

### Le même calcul ne doit exister qu'à UN endroit

⚠️ **Piège mesuré, et le plus insidieux de tous.** La première implémentation avait deux
branches (charge / repos-décharge) contenant chacune sa propre fonction de calcul de la
capacité d'un puits — sous des **noms différents**, `chg_cap` et `chg_room`, ainsi que
`spare` et `unused`. Un plafond de sécurité ajouté dans l'une n'existait pas dans l'autre.

Résultat observé, à quelques secondes d'intervalle sur la même perturbation :

```
17:34:35  régime CHARGE   consigne du 2e puits  -323 W   <- plafond appliqué
17:34:45  régime IDLE     consigne du 2e puits     0 W   <- plafond absent
17:35:00  régime IDLE     consigne du 2e puits     0 W   <- 750 W exportés
17:35:45  régime CHARGE   consigne du 2e puits  -471 W   <- ça remarche
```

Trois minutes d'export à 750 W, parce qu'un correctif ne couvrait qu'un régime sur deux.

**Factorise toute règle de plafonnement dans une fonction unique**, appelée depuis chaque
branche. Si tu dois vraiment dupliquer, écris un test qui vérifie que les deux chemins
donnent le même résultat sur les mêmes entrées.

### Un seul chemin d'exécution

⚠️ **Le piège le plus coûteux de la première implémentation.** Un raccourci en début de
boucle sortait vers un chemin de contrôle alternatif dès qu'une condition était remplie.
**Quatre versions de correctifs ont été écrites, publiées et testées sans jamais être
exécutées** — elles se trouvaient après ce raccourci.

Si tu dois avoir plusieurs chemins, **expose lequel s'exécute** dans la trace de
diagnostic. Et avant de corriger quoi que ce soit : **vérifie quel chemin tourne**.

### Isolation du code

Sépare ta logique de la base amont dans des modules distincts. Cela permet de suivre les
mises à jour amont sans conflit — validé : un portage majeur s'est fait sans un seul
conflit grâce à cette séparation.

---

## 6. Observabilité — la conception, pas l'après-coup

**Tout ce qui n'est pas tracé devra être deviné.** Plusieurs diagnostics ont pris des
heures parce qu'une grandeur n'était pas dans la trace ; il fallait la déduire du
comportement au lieu de la lire.

Trace obligatoirement, à chaque cycle :

- la grandeur pivot, brute **et** lissée ;
- l'intégrale, le point de consigne, le régime ;
- **le chemin d'exécution emprunté** ;
- pour chaque appareil : consigne, livraison, flux batterie, production, état, **et tous
  les plafonds calculés** (acceptation, livraison, capacité).

⚠️ **Ne donne jamais des noms voisins à deux notions différentes.** Une confusion entre
deux champs de nom proche a rendu un bug indéductible pendant une journée entière.

Prévois aussi un **module de diagnostic** qui compare en continu consigne et réalité, et
**nomme** l'appareil qui s'écarte. Réglage validé : seuil bas (80 W) + persistance longue
(10 min) → 6 épisodes réels en 23 h, aucun faux positif. Un seuil « évident » de 300 W
manquerait complètement les écarts de 125 W, qui sont pourtant sous le bruit apparent :
**c'est la persistance qui sépare le signal du bruit, pas l'amplitude.**

---

## 7. Méthode de travail — les règles qui ont coûté le plus cher

1. **Une modification à la fois**, testée et réversible. Deux changements simultanés
   rendent le coupable inidentifiable. Expose chaque nouveau comportement en paramètre
   réglable à chaud, avec une valeur par défaut **neutre**.

2. **Valide ton modèle avant de conclure.** Si tu simules un correctif hors ligne,
   vérifie d'abord que **la simulation sans le correctif reproduit le réel**. Deux
   simulations successives concluaient que le correctif était catastrophique ; elles
   étaient fausses (signe de l'intégrale inversé, anti-windup omis). Le test de
   validation les a rejetées. Sans lui, un bon correctif aurait été abandonné.

3. **Une corrélation n'est pas une cause** — en boucle fermée elle est même attendue.

4. **On ne chronomètre pas un actionneur sur une cible mobile.** Une latence mesurée à
   « 7-24 s » valait en réalité 2-6 s : la consigne bougeait pendant la mesure.

5. **Deux signaux périodiques de même période paraissent toujours alignés.** Leur
   décalage est constant par construction — cela ne prouve **aucune** causalité.

6. **Filtre sur des conditions atteignables.** Juger l'obéissance d'un appareil sur des
   consignes supérieures à sa limite physique fabrique un déficit imaginaire.

7. **Quand tu ne peux pas trancher, dis-le** et ajoute l'observabilité manquante. Ne
   produis pas une hypothèse de plus.

8. **Après avoir corrigé, cherche le jumeau.** Chaque fois qu'un correctif a été posé sur
   une branche, la même règle manquait ailleurs — trois fois de suite sur cette
   implémentation. Avant de publier : `grep` le nom de la grandeur corrigée et vérifie
   **tous** ses points d'usage.

---

## Livrable attendu

1. L'architecture (modules, responsabilités, flux de données).
2. Les structures d'état, en distinguant explicitement **mesures fiables** et **valeurs
   dérivées de tes propres commandes**.
3. La boucle de régulation complète, avec les signes de l'intégrateur explicités par
   régime.
4. La liste des plafonds mesurés et leur règle de mise à jour (montée / descente /
   marge).
5. Le format de la trace de diagnostic.
6. Pour chaque garde-fou : **quelle mesure de terrain le justifie**, et **comment il
   pourrait produire une oscillation** si mal conçu.

**Ne propose aucun mécanisme qui repose sur une grandeur que le moteur produit lui-même.
Si tu en écris un, signale-le explicitement et justifie pourquoi il ne peut pas boucler.**
