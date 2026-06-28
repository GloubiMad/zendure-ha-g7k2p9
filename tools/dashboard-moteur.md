# Dashboard « Moteur » (branche moteur-gielz)

Cartes à coller en **carte manuelle** (Modifier le dashboard → Ajouter une carte → Manuel → coller).
⚠️ **Adapte les `entity:`** à TES noms réels (sélecteur d'entité). Préfixe probable du manager :
`sensor.zendure_manager_*`, `select.zendure_manager_operation`, `number.zendure_manager_*`.
Remplace `sensor.shellypro3em_..._phase_c_active_power` par ton vrai P1.

## 1. Le moteur en direct — consigne vs cible vs réalisé
Le cœur de la visualisation : ce que le manager **décide** (setpoint), ce que le moteur **vise**
(smart_target, signé : + décharge / − charge), ce qui est **réalisé** (power), et le **compteur** (P1).
```yaml
type: history-graph
title: Moteur — décision vs réalisé
hours_to_show: 2
entities:
  - entity: sensor.zendure_manager_setpoint
    name: SetPoint (demande nette)
  - entity: sensor.zendure_manager_smart_target
    name: Cible moteur (smart_target)
  - entity: sensor.zendure_manager_power
    name: Réalisé parc
  - entity: sensor.shellypro3em_9454c5b8aaf0_phase_c_active_power
    name: P1 compteur
```

## 2. Pilotage & réglages du moteur
Mode actif + état + les 4 réglages live (seuils d'activation + buffers).
```yaml
type: entities
title: Moteur — pilotage & réglages
entities:
  - entity: select.zendure_manager_operation
    name: Mode (choisis « smart_buffer »)
  - entity: sensor.zendure_manager_operation_state
    name: État
  - type: divider
  - entity: number.zendure_manager_start_discharging_at
    name: Démarrer décharge au-dessus de (W import)
  - entity: number.zendure_manager_start_charging_at
    name: Démarrer charge en-dessous de (W export)
  - entity: number.zendure_manager_discharge_buffer
    name: Buffer décharge (W)
  - entity: number.zendure_manager_charge_buffer
    name: Buffer charge (W)
```

## 3. Hyper — puissances (solaire / batterie / sortie maison)
Adapte les clés (`solar_input_power`, `output_home_power`, `pack_input_power`) à tes entités device.
```yaml
type: history-graph
title: Hyper — puissances
hours_to_show: 2
entities:
  - sensor.hyper_2000_glagla_solar_input_power
  - sensor.hyper_2000_glagla_output_home_power
  - sensor.hyper_2000_glagla_pack_input_power
  - sensor.hyper_2000_up_output_home_power
  - sensor.hyper_2000_up_pack_input_power
```

## 4. SoC du parc + par device
```yaml
type: gauge
name: SoC parc pondéré
entity: sensor.zendure_manager_weighted_soc
min: 0
max: 100
severity:
  green: 50
  yellow: 20
  red: 0
```
```yaml
type: entities
title: SoC & énergie
entities:
  - sensor.zendure_manager_usable_energy
  - sensor.hyper_2000_glagla_electric_level
  - sensor.hyper_2000_up_electric_level
```

---

## Mirror du dashboard Gielz (à faire)
Le `dashboard_global.yaml` de Gielz est riche (jauges, gros boutons de mode, courbes prix). Reste à
le récupérer et **remapper ses entités** (il vise les noms SolarFlow 2400AC / ses helpers) vers les
nôtres. À faire en suivant : on garde leurs idées de cartes (boutons de mode, jauge SoC, courbe
power), on branche sur `select.zendure_manager_operation` + nos capteurs ci-dessus.

## Reste à faire (branche moteur-gielz)
- [ ] Traductions fr/en des nouveaux modes (`smart_buffer`/`quick_charge`/`quick_discharge`) et des
      réglages (sinon HA affiche les clés « humanisées », lisible mais brut).
- [ ] (option) Ajouter `smart_target` en colonne du `simulation.csv` (changement de format → supprimer
      l'ancien fichier).
- [ ] Mirror du dashboard Gielz (ci-dessus).
- [ ] Valider le moteur sur le terrain (mode `smart_buffer`, régler les seuils/buffers, comparer
      `smart_target` vs réalisé sur la carte 1).
