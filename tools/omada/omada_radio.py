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
import urllib.request
from datetime import datetime, timedelta

HOTE = "192.168.0.171"
PORTS = [(8043, "https"), (443, "https"), (8088, "http"), (80, "http")]
CIBLES = {"94-C9-60-E0-2A-0E": "up", "94-C9-60-D8-BD-AE": "glagla", "38-44-BE-83-D3-B8": "Mr big"}

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

    # ---------------------------------------------------------------- 3. historique / evenements
    print("=== RECHERCHE DE L'HISTORIQUE (endpoints candidats) ===")
    mac_up = "94-C9-60-E0-2A-0E"
    for nom, chemin in [
        ("events (v2)",      f"/{cid}/api/v2/sites/{sid}/events?currentPage=1&currentPageSize=200"),
        ("logs/events",      f"/{cid}/api/v2/sites/{sid}/logs/events?currentPage=1&currentPageSize=200"),
        ("insight/logs",     f"/{cid}/api/v2/sites/{sid}/insight/logs?currentPage=1&currentPageSize=200"),
        ("client detail",    f"/{cid}/api/v2/sites/{sid}/clients/{mac_up}"),
        ("client insight",   f"/{cid}/api/v2/sites/{sid}/insight/clients/{mac_up}"),
        ("client timeline",  f"/{cid}/api/v2/sites/{sid}/insight/clients/{mac_up}/timeline?currentPage=1&currentPageSize=200"),
        ("client history",   f"/{cid}/api/v2/sites/{sid}/clients/{mac_up}/history?currentPage=1&currentPageSize=200"),
        ("stat client",      f"/{cid}/api/v2/sites/{sid}/stat/clients/{mac_up}"),
    ]:
        try:
            r = appel(base + chemin, entetes=h)
        except Exception as e:
            print(f"  {nom:16s} -> HTTP {type(e).__name__}")
            continue
        if r.get("errorCode") != 0:
            print(f"  {nom:16s} -> refuse ({r.get('msg')})")
            continue
        res = r.get("result")
        if isinstance(res, dict) and "data" in res:
            n = len(res["data"])
            print(f"  {nom:16s} -> OK, {n} entrees (total {res.get('totalRows','?')})")
            for e in res["data"][:6]:
                print(f"        {json.dumps(e, ensure_ascii=False)[:190]}")
        else:
            print(f"  {nom:16s} -> OK : {json.dumps(res, ensure_ascii=False)[:300]}")

    try:
        appel(f"{base}/{cid}/api/v2/logout", "POST", {}, h)
    except Exception:
        pass


main()
