<#
.SYNOPSIS
  Publie une nouvelle version du fork : bump manifest, build zip, commit, tag,
  push vers le remote 'fork', et cree la release GitHub avec le zip.

.DESCRIPTION
  C'est la partie 100% mecanique et sans risque du workflow. A lancer apres
  avoir fait tes modifs de code (ou apres import-upstream.ps1).

.EXAMPLE
  .\tools\release.ps1 -Version 1.3.1.4
  .\tools\release.ps1 -Version 1.4.0.1 -Notes "Rebase sur master 1.4.0 + nos features"
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [string]$Notes = "",
    # Adapte ces deux valeurs si un jour tu changes de depot / de PC
    [string]$ForkRepo = "GloubiMad/zendure-ha-g7k2p9",
    [string]$GhPath = "C:\Program Files\GitHub CLI\gh.exe"
)

$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
$intg = Join-Path $repo "custom_components\zendure_ha"
$manifest = Join-Path $intg "manifest.json"
$zip = Join-Path $intg "zendure_ha.zip"
if (-not $Notes) { $Notes = "Fork $Version" }

Set-Location $repo

# Garde-fou : pas de version deja taguee
if (git tag -l $Version) { throw "Le tag $Version existe deja. Choisis une autre version." }

Write-Host "== 1/5 manifest -> $Version ==" -ForegroundColor Cyan
# Remplacement cible de la ligne version (preserve le formatage JSON)
$content = [IO.File]::ReadAllText($manifest)
$content = [regex]::Replace($content, '("version":\s*")[^"]*(")', "`${1}$Version`${2}")
[IO.File]::WriteAllText($manifest, $content)

Write-Host "== 2/5 build zip ==" -ForegroundColor Cyan
Push-Location $intg
Remove-Item zendure_ha.zip -ErrorAction SilentlyContinue
Compress-Archive -Path *.py, *.json, devices, translations -DestinationPath zendure_ha.zip -Force
Pop-Location
Write-Host ("   zip: {0} octets" -f (Get-Item $zip).Length)

Write-Host "== 3/5 commit ==" -ForegroundColor Cyan
git add $manifest $zip
git commit -m "chore: bump to $Version and build zip"

Write-Host "== 4/5 tag + push fork ==" -ForegroundColor Cyan
git tag -a $Version -m "Fork $Version"
git push fork master
git push fork $Version

Write-Host "== 5/5 release GitHub ==" -ForegroundColor Cyan
& $GhPath release create $Version --repo $ForkRepo --title $Version --notes $Notes $zip

Write-Host ""
Write-Host "OK  Release $Version publiee. HACS te proposera la mise a jour." -ForegroundColor Green
