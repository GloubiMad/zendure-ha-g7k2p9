"""Sur quel AP sont les Hyper, avec quel signal, et l'AP lui-meme est-il stable ?
(lecture seule)

Repond a trois questions :
  1. up et glagla sont-ils sur le MEME point d'acces ?
  2. quel RSSI chacun ? (la comparaison qui manque depuis le debut)
  3. les AP ont-ils redemarre / perdu leur liaison filaire ? (hypothese du mauvais cordon)

Explore aussi plusieurs endpoints d'historique pour trouver celui que sert l'interface.

Lancer :  python omada_radio.py
"""
from __future__ import annotations

import getpass
import json
import ssl
import sys
import re
import time
import urllib.request
from datetime import datetime, timedelta

HOTE = "192.168.0.171"
PORTS = [(8043, "https"), (443, "https"), (8088, "http"), (80, "http")]
HEURES = 6          # profondeur du journal a recuperer
APS = {"A8-29-48-E6-AB-C0": "EAP770", "20-E1-5D-1F-E1-94": "EAP610-out"}
CIBLES = {"94-C9-60-E0-2A-0E": "up", "94-C9-60-D8-BD-AE": "glagla", "38-44-BE-83-D3-B8": "Mr big"}

# ⛔ 30/09 — LE POINT AVEUGLE. Le filtre du journal ne connaissait que les 3 MAC ci-dessus : sur un
# relevé de 252 évènements il en jetait 226, dont tous ceux des AUTRES clients. On a donc passé
# trois jours à analyser les Zendure sans voir que d'autres appareils décrochaient aussi.
# Désormais : les noms sont appris depuis `insight/clients` (Known Clients — inclut les appareils
# actuellement HORS LIGNE, contrairement à `clients?filters.active=true`), et `SURVEILLE` choisit
# qui détailler. Vide = tout le monde.
SURVEILLE: list[str] = []   # ex. ["up", "Advisen"] ; vide -> tous les clients du journal

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx),
                                 urllib.request.HTTPCookieProcessor())


def appel(url, methode="GET", corps=None, entetes=None):
    d = json.dumps(corps).encode() if corps is not None else None
    r = urllib.request.Request(url, data=d, method=methode,
                               headers={"Content-Type": "application/json", **(entetes or {})})
    with op.open(r, timeout=20) as rep:
        return json.loads(rep.read().decode("utf-8", "replace"))


def duree(s):
    try:
        return str(timedelta(seconds=int(s)))
    except (TypeError, ValueError):
        return str(s)


def horo(v):
    try:
        v = int(v)
        return datetime.fromtimestamp(v / 1000 if v > 10_000_000_000 else v).strftime("%d/%m %H:%M:%S")
    except (TypeError, ValueError):
        return str(v)


def main():
    base = cid = None
    for port, proto in PORTS:
        b = f"{proto}://{HOTE}:{port}"
        try:
            c = (appel(f"{b}/api/info").get("result") or {}).get("omadacId")
            if c:
                base, cid = b, c
                break
        except Exception:
            continue
    if not base:
        sys.exit(f"Aucun controleur joignable sur {HOTE}")
    print(f"controleur : {base}")
    user = input("Identifiant Omada (compte LOCAL) : ").strip()
    pwd = getpass.getpass("Mot de passe (invisible) : ")
    rep = appel(f"{base}/{cid}/api/v2/login", "POST", {"username": user, "password": pwd})
    if rep.get("errorCode") != 0:
        sys.exit(f"Connexion refusee : {rep.get('msg')}")
    h = {"Csrf-Token": rep["result"]["token"]}
    sid = ((appel(f"{base}/{cid}/api/v2/sites?currentPage=1&currentPageSize=10", entetes=h)
            .get("result") or {}).get("data", [{}])[0].get("id"))
    print(f"site : {sid}\n")

    # ---------------------------------------------------------------- 1. les AP
    print("=== POINTS D'ACCES (stabilite de l'AP lui-meme) ===")
    try:
        dev = appel(f"{base}/{cid}/api/v2/sites/{sid}/devices", entetes=h).get("result", [])
        for d in dev:
            if str(d.get("type", "")).lower() != "ap":
                continue
            print(f"  {d.get('name')}  ({d.get('mac')})  modele {d.get('showModel') or d.get('model')}")
            print(f"     statut={d.get('statusStr') or d.get('status')}  uptime={d.get('uptimeLong') or duree(d.get('uptime'))}"
                  f"  clients={d.get('clientNum')}  version={d.get('version')}")
            print(f"     liaison : vitesse={d.get('wiredUplink', {}).get('linkSpeed', '?') if isinstance(d.get('wiredUplink'), dict) else '?'}"
                  f"  lastSeen={horo(d.get('lastSeen'))}")
    except Exception as e:
        print(f"  (indisponible : {e})")
    print()

    # ---------------------------------------------------------------- 2. les clients actifs
    print("=== CLIENTS ACTIFS : sur quel AP, avec quel signal ===")
    page, tous = 1, []
    while True:
        try:
            r = appel(f"{base}/{cid}/api/v2/sites/{sid}/clients?currentPage={page}&currentPageSize=100"
                      f"&filters.active=true", entetes=h)
        except Exception as e:
            print(f"  (indisponible : {e})")
            break
        res = r.get("result") or {}
        data = res.get("data", [])
        tous.extend(data)
        if len(tous) >= int(res.get("totalRows", 0)) or not data:
            break
        page += 1
    print(f"  {len(tous)} clients actifs\n")
    print(f"  {'nom':<22} {'AP':<20} {'RSSI':>6} {'SSID':<14} {'canal':>6} {'uptime':>12} {'debit':>10}")
    for c in tous:
        mac = (c.get("mac") or "").upper()
        nom = CIBLES.get(mac) or (c.get("name") or mac)
        marque = " <<<" if mac in CIBLES else ""
        print(f"  {str(nom)[:21]:<22} {str(c.get('apName') or '-')[:19]:<20} {str(c.get('rssi') or c.get('signalLevel') or '?'):>6}"
              f" {str(c.get('ssid') or '-')[:13]:<14} {str(c.get('channel') or '-'):>6}"
              f" {duree(c.get('uptime')):>12} {str(c.get('trafficDown') or '-'):>10}{marque}")
    print()

    # --- carte MAC -> nom lisible, pour TOUT le monde.
    # `insight/clients` (Known Clients) est le seul endpoint qui liste aussi les appareils hors
    # ligne : un client qui décroche en boucle est souvent absent de la liste des ACTIFS au moment
    # du relevé, et c'est précisément celui qu'on cherche.
    NOMS: dict[str, str] = {}
    page = 1
    while True:
        try:
            r = appel(f"{base}/{cid}/api/v2/sites/{sid}/insight/clients?currentPage={page}"
                      f"&currentPageSize=100", entetes=h)
        except Exception:
            break
        res = r.get("result") or {}
        data = res.get("data", [])
        for c in data:
            m = (c.get("mac") or "").upper()
            if m:
                NOMS[m] = c.get("name") or m
        if len(NOMS) >= int(res.get("totalRows", 0)) or not data:
            break
        page += 1
    for c in tous:                       # les actifs complètent (noms parfois plus à jour)
        m = (c.get("mac") or "").upper()
        if m:
            NOMS[m] = c.get("name") or NOMS.get(m, m)
    NOMS.update(CIBLES)                  # nos noms courts l'emportent
    print(f"  ({len(NOMS)} clients connus du controleur, actifs ou non)\n")

    # ---------------------------------------------------------------- 3. journal d'evenements
    # ⭐ L'ENDPOINT QUI MARCHE (trouve le 27/09/2026) : la PLAGE DE DATES est OBLIGATOIRE.
    # Sans `filters.timeStart` / `filters.timeEnd` en millisecondes, le controleur repond
    # « General error ». Les 7 autres chemins essayes auparavant (logs, setting/logs,
    # insight/logs, clients/{mac}/history, insight/clients/{mac}, stat/clients/{mac}, et POST
    # sur logs/events) n'existent pas sur cette version : ne PAS les reessayer.
    fin_ms = int(time.time() * 1000)
    deb_ms = fin_ms - HEURES * 3600 * 1000
    print(f"=== JOURNAL D'EVENEMENTS (dernieres {HEURES} h) ===")
    evts, page = [], 1
    while True:
        u = (f"{base}/{cid}/api/v2/sites/{sid}/logs/events?currentPage={page}&currentPageSize=100"
             f"&filters.timeStart={deb_ms}&filters.timeEnd={fin_ms}")
        r = appel(u, entetes=h)
        if r.get("errorCode") != 0:
            print(f"  refuse : {r.get('msg')}")
            break
        res = r.get("result") or {}
        data = res.get("data", [])
        evts.extend(data)
        if len(evts) >= int(res.get("totalRows", 0)) or not data:
            break
        page += 1
    print(f"  {len(evts)} evenements au total")

    def lisible(e):
        txt = e.get("content") or ""
        for mac, nom in NOMS.items():
            txt = txt.replace(f"[client:{mac}]", f"<{nom}>")
        for mac, nom in APS.items():
            txt = txt.replace(f"[ap:{mac}]", nom)
        return txt

    def qui(e) -> str | None:
        """Quel client concerne cet evenement ? (par son MAC, ou None si aucun)"""
        brut = json.dumps(e).upper()
        for mac, nom in NOMS.items():
            if mac in brut:
                return nom
        return None

    for e in evts:
        e["_qui"] = qui(e)
    if SURVEILLE:
        interessant = [e for e in evts if e["_qui"] in SURVEILLE]
        print(f"  dont {len(interessant)} concernant {', '.join(SURVEILLE)}\n")
    else:
        interessant = [e for e in evts if e["_qui"]]
        print(f"  dont {len(interessant)} rattaches a un client connu\n")
    for e in sorted(interessant, key=lambda x: x.get("time", 0)):
        print(f"  {horo(e.get('time')):>14}  {lisible(e)[:150]}")

    # --- resume : c'est la DUREE DE SESSION qui sert de mesure de controle.
    # Elle valait ~32 s tant que « Lock to Wireless Device » etait actif sur `up`.
    #
    # ⛔ PIEGE CORRIGE le 27/09 : la 1re version cherchait `(\d+)s connected`, donc elle
    # ne voyait QUE les sessions libellees en secondes. Omada ecrit « (3m connected »,
    # « (2h2m connected » des qu'une session depasse la minute ⇒ toutes les BONNES sessions
    # etaient exclues du calcul et la mediane restait collee aux echecs. Le resume annoncait
    # « 32 s » alors que l'appareil tenait deja 3 puis 7 minutes.
    def secondes(txt: str) -> int | None:
        m2 = re.search(r"\(((?:\d+[hms])+) connected", txt)
        if not m2:
            return None
        return sum(int(n) * {"h": 3600, "m": 60, "s": 1}[u]
                   for n, u in re.findall(r"(\d+)([hms])", m2.group(1)))

    def octets(txt: str) -> float | None:
        """Volume transfere pendant la session. Une session qui ne porte que 2-3 KB n'a jamais
        servi a rien : c'est de l'association pure, donc une boucle (DHCP, authentification,
        redemarrage du client) et non un usage reel interrompu."""
        m2 = re.search(r"([\d.]+)(KB|MB|GB|B)\)", txt)
        if not m2:
            return None
        return float(m2.group(1)) * {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9}[m2.group(2)]

    print("\n  --- resume par appareil ---")
    noms_vus = sorted({e["_qui"] for e in interessant if e["_qui"]})
    for nom in noms_vus:
        lignes = [e for e in interessant if e["_qui"] == nom]
        off = [e for e in lignes if "went offline" in (e.get("content") or "")
               or "is disconnected from" in (e.get("content") or "")]
        refus = [e for e in lignes if "failed to connect" in (e.get("content") or "")]
        roam = [e for e in lignes if "oaming" in (e.get("content") or "")]
        # (fin de session, duree) dans l'ORDRE du temps : c'est ainsi qu'on voit un
        # changement de regime, qu'une mediane globale noierait.
        sess = [(e.get("time"), secondes(e.get("content") or "")) for e in off]
        sess = sorted([(t, d) for t, d in sess if d is not None])
        tri = sorted(d for _, d in sess)          # par DUREE, pour la mediane et les bornes
        med = tri[len(tri) // 2] if tri else None
        detail = (f" | mediane {duree(med)} (min {duree(tri[0])}, max {duree(tri[-1])})"
                  ) if med is not None else ""
        vol = [o for o in (octets(e.get("content") or "") for e in off) if o is not None]
        if vol:
            vol.sort()
            med_o = vol[len(vol) // 2]
            detail += f" | octets/session : mediane {med_o/1000:.1f} KB"
            if med_o < 10_000:
                detail += "  <-- ASSOCIATION PURE, la session n'a jamais servi"
        print(f"  {str(nom)[:18]:18s} {len(off):3d} deconnexions | {len(refus):3d} refus | {len(roam):3d} roamings{detail}")
        if sess:
            suite = "  ".join(f"{horo(t)[-8:]}:{duree(d)}" for t, d in sess[-14:])
            print(f"           sessions (fin:duree, chronologique) {suite}")

    # --- un changement de canal de l'AP se lit comme un « roaming » de CHACUN de ses
    # clients, a quelques secondes d'intervalle. Deux clients DIFFERENTS qui « roament »
    # ensemble du meme AP/canal vers le meme AP/canal, ce ne sont pas eux qui bougent :
    # c'est l'AP. Un roaming solitaire, lui, est un vrai deplacement de client.
    #
    # ⛔ NE PAS regrouper par tranches fixes (`time // 2000`) : une frontiere de tranche
    # separe deux evenements voisins une fois sur deux. On trie puis on agrege par ECART.
    # 30 s : les clients d'un meme AP ne constatent pas son changement de canal en meme
    # temps. Mesure du 27/09 : 1 s d'ecart sur l'EAP610 mais 15 s sur l'EAP770 (8->11),
    # donc une fenetre de 5 s ratait la moitie des cas.
    ECART_AP = 30.0                                 # secondes
    mouv: dict[tuple, list] = {}
    for e in evts:
        m2 = re.search(r"roaming from (.+?) to (.+?) with", e.get("content") or "")
        if not (m2 and e.get("time")):
            continue
        c = e.get("client")
        if isinstance(c, dict):                     # `client` est parfois un objet, pas une MAC
            c = c.get("mac") or c.get("name")
        mouv.setdefault((m2.group(1), m2.group(2)), []).append(
            (int(e["time"]), str(c or json.dumps(e, sort_keys=True)[:40])))

    collectif = []
    for (dep, arr), liste in mouv.items():
        grappe: list = []
        for t_ms, qui in sorted(liste):
            if grappe and t_ms - grappe[-1][0] > ECART_AP * 1000:
                if len({q for _, q in grappe}) > 1:
                    collectif.append((grappe[0][0], dep, arr, {q for _, q in grappe}))
                grappe = []
            grappe.append((t_ms, qui))
        if len({q for _, q in grappe}) > 1:
            collectif.append((grappe[0][0], dep, arr, {q for _, q in grappe}))

    if collectif:
        print("\n  --- changements de canal de l'AP (et non des clients) ---")
        for t_ms, dep, arr, qui in sorted(collectif):
            print(f"  {horo(t_ms)}  {dep} -> {arr}  ({len(qui)} clients deplaces ensemble)")
        print("  => changement de canal de l'AP (auto-RF ou manuel) : il coute une coupure")
        print("     a CHACUN de ses clients. Figer le canal a la main supprime les coupures auto.")
    print()

    try:
        appel(f"{base}/{cid}/api/v2/logout", "POST", {}, h)
    except Exception:
        pass


main()
