# Outils de maintenance du fork

Scripts pour suivre l'upstream Zendure tout en gardant nos features, et pour
publier les versions. **Ne sont pas inclus dans le zip HACS** (hors de
`custom_components/`).

## Workflow quand un nouveau master Zendure sort

```powershell
# 1. Importer le nouveau master + rejouer nos features
.\tools\import-upstream.ps1

#    -> si conflits : resous, "git add", "git rebase --continue" (repeter),
#       puis : .\tools\import-upstream.ps1 -Finalize

# 2. Verifier le code (lance HA en test si possible)

# 3. Publier (bump + zip + commit + tag + push + release GitHub)
.\tools\release.ps1 -Version 1.4.0.1

# 4. Pousser les tags de sauvegarde dans le cloud
git push fork --tags
```

## Convention de version : `UPSTREAM.FORK`

`1.4.0.1` = base Zendure 1.4.0, revision 1 de notre fork. Voir le bump precedent.

## Sauvegardes / retour arriere

Chaque etape laisse un **tag** (point de restauration permanent) :

| Tag | Cree par | Contenu |
|-----|----------|---------|
| `upstream-<date>` | import-upstream | le master Zendure brut importe ce jour-la |
| `backup-before-<date>` | import-upstream | l'etat de master juste avant l'import |
| `1.x.y.z` | release | chaque version publiee du fork |
| `upstream-base` | (repere mobile) | le master actuellement a la base de nos features |

### Revenir en arriere

- **Le plus simple (HACS)** : Zendure -> Redownload -> choisir une version anterieure.
- **En git, version du fork** : `git reset --hard 1.3.1.3 ; git push fork master --force`
- **En git, repartir d'un master brut** : `git switch -c essai upstream-2026-06-06`

> Astuce : ne jamais supprimer les tags `upstream-*` ni `backup-*` — ce sont tes filets.

## Adapter si besoin

Si tu changes de depot ou de PC, ajuste en haut de `release.ps1` :
`-ForkRepo` et `-GhPath`.
