<#
.SYNOPSIS
  Reimporte le dernier master Zendure et rejoue nos features par-dessus.

.DESCRIPTION
  Telecharge le master officiel, le tague (sauvegarde permanente), puis rebase
  nos commits dessus via 'git rebase --onto'. S'arrete proprement en cas de
  conflit pour que tu resolves a la main, puis tu relances avec -Finalize.

  Rien n'est detruit : un tag de sauvegarde de master est cree avant tout,
  et chaque master importe est tague upstream-<date>.

.EXAMPLE
  # Etape 1 : importer + rejouer
  .\tools\import-upstream.ps1

  # (si conflits) tu resous, puis :
  #   git add <fichiers>
  #   git rebase --continue      # repeter jusqu'a la fin du rebase
  # puis :
  .\tools\import-upstream.ps1 -Finalize

  # Etape finale : publier
  .\tools\release.ps1 -Version 1.4.0.1
#>
param(
    [switch]$Finalize,
    [string]$MasterZipUrl = "https://github.com/Zendure/Zendure-HA/archive/refs/heads/master.zip"
)

$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
$intg = Join-Path $repo "custom_components\zendure_ha"
$stamp = Get-Date -Format "yyyy-MM-dd"
$upstreamTag = "upstream-$stamp"
$tmpBranch = "import-tmp"

Set-Location $repo

# ---------- MODE FINALIZE : apres resolution manuelle des conflits ----------
if ($Finalize) {
    # Verifie qu'on n'est plus en plein rebase
    if (Test-Path (Join-Path $repo ".git\rebase-merge")) {
        throw "Rebase encore en cours. Termine-le (git rebase --continue) avant -Finalize."
    }
    if (-not (git tag -l $upstreamTag)) {
        Write-Warning "Tag $upstreamTag introuvable (import fait un autre jour ?). Verifie a la main."
    }
    # upstream-base -> le commit d'import (HEAD du tmpBranch)
    if (git branch --list $tmpBranch) {
        git tag -f upstream-base (git rev-parse $tmpBranch)
        git branch -D $tmpBranch
    }
    Write-Host "OK  Rebase finalise. upstream-base mis a jour." -ForegroundColor Green
    Write-Host "Prochaine etape : .\tools\release.ps1 -Version <x.y.z.n>" -ForegroundColor Yellow
    return
}

# ---------- VERIFS ----------
if ((git branch --show-current) -ne "master") { throw "Place-toi sur la branche master d'abord." }
$dirty = git status --porcelain | Where-Object { $_ -notmatch "CLAUDE.md" }
if ($dirty) { throw "Working tree non propre. Commit/stash d'abord :`n$($dirty -join "`n")" }
if (git tag -l $upstreamTag) { throw "Le tag $upstreamTag existe deja (import deja fait aujourd'hui ?)." }

# ---------- 1. SAUVEGARDE de master avant tout ----------
$backupTag = "backup-before-$stamp"
git tag -f $backupTag master
Write-Host "== Sauvegarde master -> tag $backupTag ==" -ForegroundColor Cyan

# ---------- 2. TELECHARGEMENT du master ----------
Write-Host "== Telechargement du master Zendure ==" -ForegroundColor Cyan
$dl = Join-Path $env:TEMP "zendure_master_$stamp"
if (Test-Path $dl) { Remove-Item $dl -Recurse -Force }
New-Item -ItemType Directory -Path $dl | Out-Null
$zipFile = Join-Path $dl "master.zip"
Invoke-WebRequest -Uri $MasterZipUrl -OutFile $zipFile
Expand-Archive -Path $zipFile -DestinationPath $dl -Force
$masterIntg = Join-Path $dl "Zendure-HA-master\custom_components\zendure_ha"
if (-not (Test-Path $masterIntg)) { throw "Structure inattendue dans le zip : $masterIntg introuvable." }

# ---------- 3. BRANCHE d'import depuis upstream-base ----------
Write-Host "== Creation de la base d'import ($tmpBranch depuis upstream-base) ==" -ForegroundColor Cyan
if (git branch --list $tmpBranch) { git branch -D $tmpBranch }
git switch -c $tmpBranch upstream-base | Out-Null
# Ecrase l'integration par le nouveau master (le master n'ayant jamais supprime de fichier, copie -Force suffit)
Copy-Item (Join-Path $masterIntg "*") -Destination $intg -Recurse -Force
git add custom_components/zendure_ha
git commit -m "Import upstream master ($stamp)" | Out-Null
git tag $upstreamTag      # <-- sauvegarde permanente de CE master brut
Write-Host "   master importe et tague $upstreamTag" -ForegroundColor Green

# ---------- 4. REJEU de nos features par rebase --onto ----------
Write-Host "== Rejeu de nos commits sur le nouveau master ==" -ForegroundColor Cyan
git switch master | Out-Null
$rebaseOk = $true
git rebase --onto $tmpBranch upstream-base master
if ($LASTEXITCODE -ne 0) { $rebaseOk = $false }

if (-not $rebaseOk) {
    Write-Host ""
    Write-Host "CONFLIT pendant le rebase." -ForegroundColor Yellow
    Write-Host "1) Ouvre les fichiers en conflit, resous les marqueurs <<<< ==== >>>>" -ForegroundColor Yellow
    Write-Host "2) git add <fichiers resolus>" -ForegroundColor Yellow
    Write-Host "3) git rebase --continue   (repete jusqu'a la fin)" -ForegroundColor Yellow
    Write-Host "4) .\tools\import-upstream.ps1 -Finalize" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Pour tout annuler : git rebase --abort ; git switch master ; git reset --hard $backupTag" -ForegroundColor DarkGray
    return
}

# ---------- 5. SUCCES : finalisation ----------
git tag -f upstream-base (git rev-parse $tmpBranch)
git branch -D $tmpBranch
Write-Host ""
Write-Host "OK  Rebase reussi sans conflit. Masters sauvegardes : $upstreamTag" -ForegroundColor Green
Write-Host "Verifie le code, puis publie : .\tools\release.ps1 -Version <x.y.z.n>" -ForegroundColor Yellow
Write-Host "(pense a pousser les tags de sauvegarde : git push fork --tags)" -ForegroundColor DarkGray
