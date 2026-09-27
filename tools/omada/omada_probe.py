"""Trouve la bonne facon d'appeler le journal d'evenements Omada (lecture seule).

`logs/events` repond « General error » (et non « Unsupported request path ») : l'endpoint
EXISTE, il manque des parametres. On teste systematiquement les variantes et on affiche
errorCode + msg pour chacune, puis le contenu de celle qui passe.

Lancer :  python omada_probe.py
"""
from __future__ import annotations

import getpass
import json
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

HOTE = "192.168.0.171"
PORTS = [(8043, "https"), (443, "https"), (8088, "http"), (80, "http")]

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx),
                                 urllib.request.HTTPCookieProcessor())


def appel(url, methode="GET", corps=None, entetes=None):
    d = json.dumps(corps).encode() if corps is not None else None
    r = urllib.request.Request(url, data=d, method=methode,
                               headers={"Content-Type": "application/json", **(entetes or {})})
    try:
        with op.open(r, timeout=20) as rep:
            return json.loads(rep.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return {"errorCode": f"HTTP {e.code}", "msg": e.reason}
    except Exception as e:
        return {"errorCode": type(e).__name__, "msg": str(e)}


def main():
    base = cid = None
    for port, proto in PORTS:
        b = f"{proto}://{HOTE}:{port}"
        r = appel(f"{b}/api/info")
        c = (r.get("result") or {}).get("omadacId")
        if c:
            base, cid = b, c
            break
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

    fin = int(time.time() * 1000)
    deb = fin - 24 * 3600 * 1000          # 24 h
    S = f"{base}/{cid}/api/v2/sites/{sid}"

    essais = [
        # (libelle, methode, url, corps)
        ("GET page seule",        "GET",  f"{S}/logs/events?currentPage=1&currentPageSize=50", None),
        ("GET + plage 24h",       "GET",  f"{S}/logs/events?currentPage=1&currentPageSize=50"
                                          f"&filters.timeStart={deb}&filters.timeEnd={fin}", None),
        ("GET + plage + level",   "GET",  f"{S}/logs/events?currentPage=1&currentPageSize=50"
                                          f"&filters.timeStart={deb}&filters.timeEnd={fin}&filters.level=0", None),
        ("GET + plage + module",  "GET",  f"{S}/logs/events?currentPage=1&currentPageSize=50"
                                          f"&filters.timeStart={deb}&filters.timeEnd={fin}&filters.module=1", None),
        ("GET + start/end nus",   "GET",  f"{S}/logs/events?currentPage=1&currentPageSize=50"
                                          f"&start={deb}&end={fin}", None),
        ("GET + searchKey vide",  "GET",  f"{S}/logs/events?currentPage=1&currentPageSize=50"
                                          f"&filters.timeStart={deb}&filters.timeEnd={fin}&filters.searchKey=", None),
        ("POST body plage",       "POST", f"{S}/logs/events",
                                          {"currentPage": 1, "currentPageSize": 50, "filters": {"timeStart": deb, "timeEnd": fin}}),
        ("POST body simple",      "POST", f"{S}/logs/events", {"currentPage": 1, "currentPageSize": 50}),
        # autres familles de journaux
        ("alerts + plage",        "GET",  f"{S}/logs/alerts?currentPage=1&currentPageSize=50"
                                          f"&filters.timeStart={deb}&filters.timeEnd={fin}", None),
        ("auditlogs + plage",     "GET",  f"{S}/logs/auditlogs?currentPage=1&currentPageSize=50"
                                          f"&filters.timeStart={deb}&filters.timeEnd={fin}", None),
        # au niveau CONTROLEUR et non site
        ("ctrl logs/events",      "GET",  f"{base}/{cid}/api/v2/logs/events?currentPage=1&currentPageSize=50"
                                          f"&filters.timeStart={deb}&filters.timeEnd={fin}", None),
        # variantes de chemin
        ("insight/events",        "GET",  f"{S}/insight/events?currentPage=1&currentPageSize=50", None),
        ("setting/logs/events",   "GET",  f"{S}/setting/logs/events?currentPage=1&currentPageSize=50", None),
    ]

    gagnants = []
    print(f"{'essai':<22} {'code':>10}  message / resultat")
    print("-" * 78)
    for lib, meth, url, corps in essais:
        r = appel(url, meth, corps, h)
        code = r.get("errorCode")
        if code == 0:
            res = r.get("result") or {}
            n = len(res.get("data", [])) if isinstance(res, dict) else 0
            tot = res.get("totalRows", "?") if isinstance(res, dict) else "?"
            print(f"{lib:<22} {str(code):>10}  OK — {n} entrees (total {tot})")
            if n:
                gagnants.append((lib, meth, url, corps, res["data"]))
        else:
            print(f"{lib:<22} {str(code):>10}  {str(r.get('msg'))[:44]}")

    if not gagnants:
        print("\nAucune variante ne renvoie de donnees.")
        print("=> le journal n'est pas expose par cette version de l'API : rester sur l'interface web.")
        return

    lib, meth, url, corps, data = gagnants[0]
    print(f"\n=== ÇA MARCHE : « {lib} » ({meth}) ===")
    print(f"URL : {url[:160]}")
    if corps:
        print(f"corps : {json.dumps(corps)}")
    print(f"\nles 10 premieres entrees :\n")
    for e in data[:10]:
        ts = e.get("time") or e.get("timestamp") or e.get("date")
        try:
            ts = datetime.fromtimestamp(int(ts) / 1000).strftime("%d/%m %H:%M:%S")
        except (TypeError, ValueError):
            pass
        msg = e.get("content") or e.get("msg") or e.get("description") or json.dumps(e, ensure_ascii=False)
        print(f"  {str(ts):>14}  {str(msg)[:140]}")
    print(f"\ncles disponibles : {sorted(data[0].keys())}")

    appel(f"{base}/{cid}/api/v2/logout", "POST", {}, h)


main()
