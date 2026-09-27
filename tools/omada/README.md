# Outils Omada (lecture seule)

Controleur `192.168.0.171`. Les identifiants (compte **LOCAL**, sans 2FA) sont demandes au
clavier a chaque lancement : rien n'est stocke. Aucun de ces scripts n'ecrit quoi que ce soit.

| script | a quoi il sert |
|---|---|
| **`omada_radio.py`** | **le principal** : pour chaque Zendure, son AP, son RSSI, son SSID, son canal et surtout son **`uptime` de session** — le seul indicateur fiable de stabilite. Donne aussi l'uptime des AP. |
| `omada_clients.py` | ce que l'integration HA peut voir : endpoint `insight/clients`, filtre `wireless`, nombre de sites. |
| `omada_probe.py` | explore les endpoints du journal et affiche `errorCode` + `msg` pour chaque variante. A garder : les chemins changent selon la version du controleur. |

## API — ce qui marche (v5)

Connexion : `GET /api/info` -> `omadacId`, puis `POST /{cid}/api/v2/login` -> `token`
(a mettre dans le header `Csrf-Token`).

**Journal d'evenements — la plage de dates est OBLIGATOIRE** (sans elle : « General error ») :

    GET /{cid}/api/v2/sites/{sid}/logs/events
        ?currentPage=1&currentPageSize=50
        &filters.timeStart={ms}&filters.timeEnd={ms}

Cles renvoyees : `time` (ms), `content`, `client`, `device`, `ssid`, `channel`, `module`, `opt`.

Marchent aussi : `sites/{sid}/clients?filters.active=true` (apName, rssi, ssid, channel,
**uptime**), `sites/{sid}/devices`, `sites/{sid}/insight/clients`.

N'existent PAS : `logs`, `setting/logs`, `insight/logs`, `clients/{mac}/history`,
`insight/clients/{mac}`, `stat/clients/{mac}`, et POST sur `logs/events`.

## Prerequis cote controleur

- **Settings > Site Settings > History Data Retention > Client History Retention** doit etre
  **COCHE** (decoche par defaut) — sinon l'onglet *Connection Logs* est vide et l'API renvoie 0.
- **Logs Setting > Content Settings > Client** : activer *Client Online/Offline (Wireless)*,
  *Client Roaming*, *Client Connection Failed*. Laisser EMAIL et WEBHOOK decoches.

## Pieges de lecture

- **L'`uptime` de l'ecran *Overview* d'un client NE MONTRE PAS les reassociations.** Il affichait
  « CONNECTED 1 h 50 min » pendant que l'appareil se reconnectait toutes les 32 s. Seuls les
  *Connection Logs* / `logs/events` font foi, et le champ `uptime` de `clients?filters.active=true`.
- **« blocked by MAC » dans le journal n'est PAS forcement un filtre MAC** : ca recouvre aussi
  **« Lock to Wireless Device »** (fiche du client > onglet Config), qui fut la cause racine des
  decrochages de `up` le 27/09/2026.
- Le controleur peut etre en **UTC+1 sans heure d'ete** : son interface decale l'affichage d'une
  heure. L'API, elle, renvoie des epoch corrects.
