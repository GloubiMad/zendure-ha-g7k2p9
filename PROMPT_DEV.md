# Prompt — reprendre le développement du moteur Zendure (branche `port-143`)

> Tu reprends le développement d'un fork de l'intégration Home Assistant **Zendure**, et
> plus précisément de son moteur de régulation `smart_fondation`. Ce document te donne ce
> qu'il faut pour travailler **sur le code existant** sans refaire les erreurs déjà payées.
>
> Il complète deux documents du dépôt, à lire aussi :
> - **`PROMPT_MOTEUR.md`** — les principes de conception du moteur (pourquoi il est fait
>   comme ça). Ce fichier-ci ne les recopie pas.
> - **`PARAMETRES_FONDATION.md`** — les réglages exposés dans l'interface.
>
> L'utilisateur est francophone. **Réponds en français, raisonne en français** — il relit
> ton raisonnement pour le vérifier.

---

## 0. Les six règles qu'il ne faut jamais enfreindre

1. **Mesure avant d'affirmer.** `simulation.csv` est la vérité du système. Un chiffre non
   mesuré s'étiquette `SUPPOSÉ`. « Je ne sais pas » est une réponse acceptable ; une
   hypothèse présentée comme un fait ne l'est pas.
2. **Vérifie que ton correctif mord sur les données réelles AVANT de l'écrire.** Rejoue-le
   sur le CSV. Et valide ton modèle de rejeu : **sans** le correctif, il doit reproduire ce
   qui a été observé — sinon c'est le modèle qui est faux, pas le correctif.
3. **N'incrimine jamais le matériel en premier.** Le contrôleur Wi-Fi Omada voit les trois
   appareils connectés **100 % du temps**. Les Zendure accusent chaque consigne en 184 ms.
   Toute « lenteur » ou « déconnexion » vue dans le CSV a jusqu'ici été **fabriquée par le
   code**. Cherche-la dans le code.
4. **Aucun trailer `Co-Authored-By`** dans les messages de commit. C'est la règle du
   `CLAUDE.md` de ce dépôt.
5. **Publie sur le fork, sans demander**, dans la foulée d'un correctif — voir §8. Mais ne
   touche **jamais** au dépôt officiel Zendure.
6. **Ne supprime rien sans accord** : ni entité HA, ni fichier, ni historique git.

---

## 1. Le dépôt

| | |
|---|---|
| worktree local | `E:\git\zendure_142` |
| branche | `port-143` |
| remote **`fork`** | `GloubiMad/zendure-ha-g7k2p9` — **le seul où tu pousses** |
| remote `origin` | `Zendure/Zendure-HA` — dépôt **officiel**, lecture seule |
| remote `zoic21` | un autre fork, lecture seule |

⚠️ `origin` n'est pas le fork. Toute commande qui publie **nomme le fork explicitement** :
`git push fork port-143`, `gh … --repo GloubiMad/zendure-ha-g7k2p9`. C'est voulu et connu
de l'utilisateur : ne le commente pas, applique-le.

Portage des versions amont : **merge, jamais rebase**. Le code du moteur est isolé dans des
modules séparés, ce qui a rendu le portage de la 1.4.4 trivial (2 conflits pour 103 commits
amont). ⛔ `power_get` ne doit **jamais** être réaligné sur l'amont.

`zendure_ha.zip` à la racine du dépôt est **périmé** (dernier commit en 1.4.3.45). Ce n'est
pas l'asset de release ; ne le prends pas pour référence.

---

## 2. L'installation

| appareil | nom | IP | capacité | particularité |
|---|---|---|---|---|
| SolarFlow 2400 Pro | `mrbig` / `Pro` | `.230` | 8,16 kWh | zenSDK, HTTP local ; sn `EEB4AEP4P161131` |
| Hyper 2000 | `glagla` / `gl` | `.232` | 5,76 kWh | **producteur** (2000 Wc de PV), MQTT |
| Hyper 2000 | `up` | `.231` | 3,84 kWh | **sans PV**, MQTT |
| 2 micro-onduleurs APsystems | | | | injectent au tableau principal |

Réseau `192.168.0.x`. Monophasé : les « phases » A/B/C du Shelly 3EM sont **trois
sous-tableaux**, pas trois phases.

| hôte | rôle |
|---|---|
| `.174` | Home Assistant **de PRODUCTION** — porte les Zendure |
| `.173` | Home Assistant de **test** — sans aucun matériel réel (tout y est `unavailable`, c'est normal) |
| `.175` | InfluxDB |
| `.178` | broker MQTT de production, **OpenSSL recompilé** dans `/opt/openssl-noverify` |
| `.177` | broker MQTT de test |

Tarif : HC **21:36 → 05:36** à 0,1376 €/kWh, HP à 0,1727 €/kWh.

---

## 3. Accès — ce qui est permis, ce qui ne l'est pas

| action | règle |
|---|---|
| lire `\\192.168.0.174\config\simulation.csv` | ✅ **libre**, c'est ton outil de travail |
| lire le reste de `\\192.168.0.174\config\` (registre, `automations.yaml`…) | ✅ en lecture, quand la tâche l'exige |
| **modifier quoi que ce soit sur `.174`** | ⛔ **demander d'abord** |
| appeler l'API REST de HA `.174` | ⛔ jamais, même pour tester |
| créer un `.bak` sur le HA | ⛔ jamais |
| InfluxDB `.175` | ✅ lecture seule ; identifiants dans `E:\git\influxdb claude.txt`, ne jamais les afficher |
| HA de test `.173` | ✅ libre, pour explorer la **structure** des entités |

Le broker `.178` : `ldd` et `dpkg -V` **ne peuvent pas voir** le patch OpenSSL. Seul
`/proc/<pid>/maps` fait foi. 60 à 115 `bad record mac` par jour y sont **normaux** ; le
vrai critère de santé est `malformed packet = 0`.

---

## 4. Carte du code — `custom_components/zendure_ha/`

Cherche **par nom de fonction**, jamais par numéro de ligne : les commentaires sont
abondants et les lignes bougent à chaque version.

| fichier | ce qui compte |
|---|---|
| `fondation.py` | **le moteur** (~2300 lignes). `update()` = un cycle ; `_apply_and_report()` = seul point de sortie des consignes, commun aux deux moteurs ; `_engage_ok()` = seul point de décision d'un réveil ; `_maj_lead()` = hystérésis sticky ; `_dis_ceiling()` / `_chg_ceiling()` = plafonds sur la livraison mesurée ; `_direct_control()` = **second moteur**, désactivé (`direct` = 0) |
| `device.py` | appareils. `setStatus()`, `online`, `power_get()`, `mqttPublish()`, `mqttInvoke()` |
| `devices/hyper2000.py` | `power_off()` réel des Hyper (MQTT `function/invoke`) |
| `manager.py` | `update_operation()` (changement de mode), `_async_update_data()` (appelle le watchdog) |
| `watchdog.py` | surveillance des muets : `tick()`, `reemettre_arret()`, `wake()`, `_ble_toggle()` |
| `api.py` | clients MQTT local et cloud |
| `simulation.py` | écriture de `simulation.csv` |
| `const.py` | `ManagerMode`, `DeviceState`, `SmartMode` |

Le moteur tourne en mode **`smart_fondation`**. ⛔ `smart` est l'ancien moteur MATCHING de
l'amont, sans aucun de nos correctifs — des automatisations HA désactivées y renvoient
encore par erreur, ne les réactive pas.

Hors intégration : `ha/zendure_reserve_matin.yaml` + `ha/README.md` — un package HA qui
coupe le moteur en heures creuses pour garder une réserve pour le matin.

---

## 5. `simulation.csv` — mode d'emploi

Environ **480 000 lignes par jour**, une par cycle (~1 s). **Il n'est écrit que lorsque le
moteur tourne** : un trou dans le fichier peut signifier « moteur coupé », pas « HA planté ».
Il peut être vidé (bouton `button.zendure_manager_simulation_rotate`) : **vérifie toujours la
plage de dates réellement couverte** avant d'en tirer une conclusion sur plusieurs jours.

### Colonnes

Séparateur `;`. ⚠️ L'**en-tête a 53 colonnes, les données 51** : l'en-tête intercale un
bloc JSON par appareil, les données non. Indexe sur les données :

| index | champ |
|---|---|
| 0 | horodatage ISO |
| 1 | **P1** (+ import, − export) |
| 2-7 | mode, totaux, setpoint, séparateur |
| 8-21 | **SolarFlow** |
| 22-35 | **glagla** |
| 36-49 | **up** |
| 50 | **message de debug du moteur** |

Dans chaque bloc de 14, dans l'ordre : `bat, Prod, Home, Cmd, Soc, Conn, ChLim, St, Age,
Grid, Byp, Tmp, CTmp, Byp2`. Donc `up.Cmd` = index `36 + 3`.

### Ce que les champs veulent vraiment dire

- `bat` = flux batterie (− charge, + décharge). `Home` = sortie AC. `Cmd` = consigne.
  ⚠️ Un `Cmd` positif ne prouve pas que l'appareil décharge : regarde `bat`.
- `Age` vient de **`lastreport`** (dernier `properties/report`), pas de `lastseen`.
  `Age = -1` = jamais rapporté depuis le chargement.
- `St` = `DeviceState` : 0 OFFLINE, 1 SOCEMPTY, 2 INACTIVE, 3 SOCFULL, 4 ACTIVE.
  ⚠️ **`OFFLINE` n'est pas une panne** — c'est une conclusion du code, souvent fausse.
- `Conn` = `connectionStatus`. ⚠️ **Ce n'est PAS un état de connexion** : `setStatus()` y
  encode aussi 1 = `socStatus`, 2 = HEMS, 3 = fusegroup, **avant** 10/11/12 = cloud /
  local / zenSDK. Et `socStatus` n'est pas « batterie pleine » : c'est un état de
  calibration de jauge.

### Le message de debug (index 50)

```
fondation regime=DISCHARGE hl=747 forced=0 T=747 ema=588 amt=747 int=74 sp=820 occ=1.00
          vol=393* prod=1glag:64 acc=0Pro:1967/2up:1201 liv=0Pro:577/1glag:655/2up:198
          st=0Pro:INACT+0/1glag:INACT+418/2up:INACT+402 ms=24cyc/0.2calc/0.4io
```

| champ | sens |
|---|---|
| `hl` | `house_load = P1 + Σ(homeOutput − homeInput)` — **aveugle à l'export**, et faux de l'amplitude de toute consigne encore en vol |
| `ema` / `amt` | `hl` lissé pour le régime / pour les montants |
| `int`, `sp` | intégrale, setpoint |
| `vol` | commande en vol (commandé − rapporté) ; `*` = gel d'intégrale actif |
| `acc` / `liv` | acceptation de charge / livraison de décharge **mesurées** |
| `st` | état + consigne par appareil, préfixé du rang (`0Pro`, `1glag`, `2up`) |
| `pub=` | envois MQTT perdus — **n'apparaît que s'il y en a** |
| `why=` | raison pour laquelle un appareil n'a pas reçu de part — **n'apparaît que sur une inversion du tri** |
| `cap=` | watts retirés par le plafond de sortie — n'apparaît que s'il mord |

⚠️ Le champ debug a **un cycle de retard** sur les colonnes : ne recoupe pas les deux au
cycle près.

---

## 6. Méthode — la boucle qui a fonctionné

1. **Copier le CSV** localement, jamais le traiter en place sur le partage.
2. **Mesurer le symptôme** sur les données, avec un chiffre et une fenêtre datée.
3. **Lire le code** du chemin concerné — et vérifier que c'est bien ce chemin qui s'exécute.
4. **Chercher le jumeau** (§9) avant même de coder.
5. **Rejouer le correctif** sur le CSV. Valider le modèle : sans correctif, il doit
   reproduire l'observé.
6. **Coder**, avec un commentaire qui cite la mesure qui justifie la ligne. Ce fichier est
   commenté ainsi partout ; respecte la densité.
7. **Tester par exécution** (§7) et lancer le contrôle des noms non définis.
8. **Publier** (§8), puis **retélécharger l'asset** pour vérifier son contenu.

**Une modification de dynamique à la fois.** Deux changements du comportement de régulation
publiés ensemble rendent le coupable inidentifiable. ⚠️ Mais cette règle ne justifie **pas**
de repousser un défaut d'**observabilité** connu, isolé et testable hors ligne : c'est comme
ça que `mqttPublish` a ignoré son code retour pendant des mois alors que le défaut était
identifié.

Toute nouveauté est **désactivable** : réutilise un paramètre existant dont une valeur
rétablit l'ancien comportement (`wake_grace` = 0 a servi trois fois), plutôt que d'en
créer un nouveau.

---

## 7. Tester sans `pytest`

`pytest` n'est pas installé sur ce poste ; les tests de `tests/` viennent de l'amont et ne
couvrent pas le moteur. On teste donc **la vraie méthode**, extraite du fichier par AST,
avec un harnais minimal :

```python
import ast, io
src  = io.open(r'E:\git\zendure_142\custom_components\zendure_ha\fondation.py', encoding='utf-8').read()
tree = ast.parse(src)
cls  = [n for n in tree.body if isinstance(n, ast.ClassDef)
        and any(isinstance(f, ast.FunctionDef) and f.name == '_engage_ok' for f in n.body)][0]
meth = [f for f in cls.body if isinstance(f, ast.FunctionDef) and f.name == '_engage_ok'][0]
ns = {}
exec(compile(ast.Module(body=[meth], type_ignores=[]), '<x>', 'exec'), ns)
# puis appeler ns['_engage_ok'](faux_self, ...) avec des objets factices
```

⚠️ **Quand un test échoue, vérifie d'abord que le test reproduit les conditions réelles.**
Cela a été le cas quatre fois : fonction appelée toutes les 15 s alors qu'elle tourne chaque
seconde, coroutine fermée sans être exécutée, `lastreport` figé qui rendait l'appareil
réellement muet, mock incomplet.

**Contrôle obligatoire des noms non définis** avant toute publication : un nom utilisé
dans une fonction où il n'existe pas ne se voit qu'à l'exécution — chez l'utilisateur.
C'est arrivé (`self.hass` sur `FondationEngine`, une variable locale lue depuis
`_apply_and_report`). Parcours chaque fonction par AST et liste les `Name` en lecture qui
ne sont ni paramètres, ni assignés localement, ni globaux du module. Faux positifs connus
et attendus : les closures `dis_cap`, `chg_cap`, `chg_room` (qui capturent `cmd`, `self`,
`fuse_chg`) et les paramètres de lambda.

---

## 8. Publier

Un correctif du moteur se termine **toujours** par commit + push + release, sans demander.

```bash
# 1. version
sed -i 's/"version": "1.4.5.4"/"version": "1.4.5.5"/' custom_components/zendure_ha/manifest.json
# 2. commit — PAS de trailer Co-Authored-By
git add -A custom_components/ && git commit -F message.txt
# 3. push sur le FORK
git push fork port-143
```

4. **Construire l'archive** — elle doit s'appeler exactement `zendure_ha.zip`, avec
   `manifest.json` **à la racine**. Si tu zippes le dossier avec son parent, HACS refuse
   l'installation (« unable to get manifest ») :

```python
import zipfile, os
src = r'E:\git\zendure_142\custom_components\zendure_ha'
with zipfile.ZipFile('zendure_ha.zip', 'w', zipfile.ZIP_DEFLATED) as z:
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d != '__pycache__']
        for f in files:
            if not f.endswith('.pyc'):
                p = os.path.join(root, f)
                z.write(p, os.path.relpath(p, src))
```

5. `gh release create 1.4.5.5 --repo GloubiMad/zendure-ha-g7k2p9 --target port-143 --title "…" --notes "…" zendure_ha.zip`
6. **Retélécharger** l'asset avec `gh release download` et vérifier la version et la
   présence du correctif dans le fichier téléchargé.

Notes de release : le symptôme, la cause, les **mesures**, le correctif, ce qu'il ne fait
pas. En markdown, avec des tableaux. ⚠️ Dans le shell, échappe les backticks du texte des
notes, sinon bash les exécute comme des commandes.

---

## 9. La forme de presque tous les bugs

> **Une décision écrite à deux endroits, corrigée à un seul.**

C'est arrivé une douzaine de fois. Après chaque correctif, `grep` le nom de la grandeur
corrigée et examine **tous** ses points d'usage. Les jumeaux connus :

| décision | endroits | état |
|---|---|---|
| gel sur commande en vol | intégrale / `amt` / **machine à états du régime** | 2 sur 3 (le régime reste) |
| hystérésis sticky | répartition CHARGE / étape 1bis DISCHARGE | corrigé 1.4.5.1 |
| mise à jour des meneurs | branche DISCHARGE / CHARGE / `_direct_control` | centralisé dans `_maj_lead` (1.4.5.4) |
| place de charge d'un puits | `chg_cap` / `chg_room` | noms différents, même rôle — attention |
| `SOLAR_ENGAGE` | étape 1bis DISCHARGE / CHARGE | corrigé 1.4.4.4 |
| tout | moteur principal / `_direct_control` | le second n'a reçu **aucun** correctif de juillet |

Un sticky ou un état ne se remet à zéro **que** s'il y a eu une décision dans son sens.
L'effacer « par défaut » dans une branche inactive l'efface en réalité à chaque
transition — et le régime en fait une toutes les 38 secondes.

---

## 10. Pièges d'analyse déjà payés

| piège | la bonne mesure |
|---|---|
| la corrélation croisée consigne → sortie donne un pic à 7,4 s | c'était la **demi-période de l'oscillation**. Mesure l'identité valeur par valeur (`Home[i] == Cmd[i-lag]` au watt près) : 2,7 s |
| « au bout de combien de temps `Home` bouge-t-il ? » donne 0,9 s | ça détecte le **rattrapage de la consigne précédente**. Cherche quand la grandeur **atteint la valeur demandée** |
| une médiane de 126 W présentée comme « des miettes » | elle noyait 51 épisodes de plus de 300 W. Regarde la distribution, jamais la seule médiane |
| `Conn = 1` lu comme « déconnecté » | c'est `socStatus`. L'âge médian à `Conn = 1` était de **1 seconde** |
| `socStatus = 1` lu comme « batterie pleine » | c'est la calibration de jauge. Aucune batterie n'était pleine |
| `glagla` sort de la batterie « au lieu du plus plein » | glagla est **productrice** : retire sa part PV avant de juger la répartition |
| l'état d'une automatisation HA lu dans `automations.yaml` | il est dans `.storage/core.restore_state` |
| « le patch OpenSSL du broker a sauté » | `ldd` ne peut pas le voir ; `/proc/<pid>/maps` |
| « le matériel est lent » | un Hyper à l'arrêt met 11,7 s à démarrer, 3,6 s s'il tourne — c'est une contrainte à intégrer, jamais une explication d'un défaut |

Et en YAML / Jinja pour le package HA :
- `states('x') | float(520)` **ne** replie **pas** sur 520 si l'état vaut `"0.0"` : le
  défaut ne joue que si la conversion échoue.
- Un `state:` qui renvoie `"unavailable"` ne rend pas un capteur indisponible — c'est la
  clé `availability:`.
- Un `input_number` sans `initial:` prend sa valeur `min` au tout premier chargement.

---

## 11. Où en est le code (au 19/09/2026)

Version courante : **1.4.5.4**.

| version | ce qu'elle corrige |
|---|---|
| 1.4.3.52 | plafond de sortie `out_max` = 2800 W (l'export est interdit au-delà de 3 kW) |
| 1.4.3.53 | repli mDNS quand l'IP d'un appareil change |
| 1.4.3.54 | le client MQTT se reconnecte (`connect_async`) |
| 1.4.4.1 | portage sur la 1.4.4 officielle |
| 1.4.4.2 | le surplus est routé pendant un export mesuré sur P1 |
| 1.4.4.3 | le watchdog refait ce qu'un redémarrage de HA fait |
| 1.4.4.4 | `SOLAR_ENGAGE` : le solaire sort dans tous les modes |
| 1.4.4.5 | l'intégrale ne se charge plus pendant qu'une consigne est en vol |
| 1.4.4.6 | un réveil doit être mérité par la **durée** du besoin (sous 15 s : 0 % de rendement) |
| 1.4.4.7 | le fast-track ne s'arme plus sur un saut que nous avons fabriqué |
| 1.4.4.8 | `mqttPublish` lit son code retour ; une commande perdue est signalée |
| 1.4.4.9 | le watchdog réémet `power_off` tant que l'arrêt n'est pas constaté |
| 1.4.5.0 | un appareil jamais vu depuis le chargement n'est plus abandonné par le watchdog |
| 1.4.5.1 | l'étape 1bis en décharge appliquait le SoC nu, sans hystérésis |
| 1.4.5.2 | `online` = `lastseen`, plus `connectionStatus` — un appareil joignable n'est plus exclu |
| 1.4.5.3 | sonde `why=` |
| 1.4.5.4 | un seul meneur, et l'IDLE n'efface plus l'hystérésis |

Le détail de chacun est dans son message de commit et dans ses notes de release.

---

## 12. Chantiers ouverts, par priorité

1. **Valider la 1.4.5.4 sur le terrain.** Attendu avec `hysteresis_wide` : un seul appareil
   se remplit, l'autre ne prend la main qu'après 15 points d'écart. Rejeu : écart 0,3 → 8
   points sur 52 min. Si les SoC se suivent encore, le mécanisme est ailleurs.
2. **Analyser `why=`.** Sur 28 % des inversions de tri en décharge, `up` était présent,
   connecté, livrable intact, et pas servi. La sonde dit désormais pourquoi.
3. **La machine à états du régime** teste encore `t_raw > ft` brut, sans le gel sur
   commande en vol : 258 des 898 transitions de régime mesurées l'étaient avec une commande
   en vol, et 31 % des régimes durent moins de 10 s. Troisième jumeau non corrigé (§9).
4. **`_ble_toggle`** (étape 3 du watchdog) lit `connection.value`, qui peut renvoyer `up`
   au cloud alors que son sélecteur dit local.
5. `_direct_control` : le cas `_socfull_block` remet encore `lead`/`clead` à zéro. Moteur
   désactivé, donc sans effet aujourd'hui.
6. `db_on`, `db_off` et `ft` sont lus deux fois dans `update()`. Inoffensif, mêmes valeurs.
7. Test Encrypt-then-MAC sur le broker de test : protocole prêt dans `tools/test_etm.md`.

Écartés volontairement, avec leurs raisons dans les commentaires : étendre `dwell_sec` à
l'extinction (les extinctions courtes ont lieu pendant un export réel), et un « kill
switch » sur P1 (la pointe d'export est atteinte au premier échantillon, avant toute
réaction possible — c'est pour ça qu'on plafonne en amont avec `out_max`).

---

## 13. Travailler avec l'utilisateur

- Il connaît son installation mieux que les données. **Ses observations sont des mesures** :
  quand il dit « les trois étaient à 15 % ce matin » ou « Omada les voit connectés à 100 % »,
  c'est un fait à intégrer, et plusieurs fois c'est ce qui a débloqué le diagnostic.
- Quand il te reprend, vérifie **tout de suite** dans les données et corrige sans détour.
  Ne te défends pas, ne rumine pas.
- Il ne supporte pas le sur-questionnement. Ne pose que les questions qui changent le
  travail ; pour le reste, décide et dis ce que tu as supposé.
- Le critère est la **propreté** du moteur, pas l'argent. Ne clos jamais une piste par
  « ça ne rapporte que X € ».
- Le moteur s'adapte à l'usage de la maison, jamais l'inverse : ne propose pas de décaler
  une machine à laver.
- Il est débutant en ligne de commande Linux : quand il doit taper une commande, explique-la
  pas à pas.
