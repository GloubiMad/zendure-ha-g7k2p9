# Archive de référence — moteur « smooth-ramp » (zoic21 / auto-discovery)

**Pourquoi ce dossier ?** Copie figée des fichiers-clés du nouveau moteur de régulation,
gardée comme **trace permanente** au cas où la branche source serait supprimée/réécrite
(elle a déjà bougé une fois : `zoic21/auto-discovery` → `origin/auto-discovery`).

**Source** : `origin/auto-discovery` (dépôt officiel `Zendure/Zendure-HA`)
**Commit** : `6612e409` — *Added details and comments to the first 5 chapters* — **2026-03-03**
(branche **parkée** depuis cette date ; même contenu que l'ex-`zoic21/auto-discovery`).

**Contenu**
- `zendure_distribution_functional_design.md` (361 l) — **LA spec** du pipeline : EMA → rate-limiter
  → σ → hysteresis → deadband → clamp mode → direction-guard, device-count strategy, constantes.
- `distribution.py` (170 l) — le moteur réel.
- `coordinator.py` (203 l) — coordinator associé.

**Usage** : référence pour le **portage** du pipeline dans notre `manager.py`
(cf. `tools/portage-moteur-smooth-ramp.md`). On NE rebase PAS sur cette branche
(parkée, couplée à une réécriture complète) — on **porte la spec** chez nous.

> ⚠️ Ces fichiers ne sont PAS chargés par l'intégration : `tools/` est hors zip HACS.
> Pour rafraîchir depuis l'upstream : `git show origin/auto-discovery:<chemin> > tools/ref-moteur-zoic21/<fichier>`.
