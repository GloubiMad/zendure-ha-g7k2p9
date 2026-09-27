"""Que contient l'API que lit l'integration HA ? (lecture seule)

Interroge le controleur Omada exactement comme le fait `tplink-omada-client` :
  endpoint `insight/clients`, pagine, puis filtre `wireless == true`.

Les identifiants sont demandes au clavier, rien n'est enregistre.
Lancer :  python omada_clients.py
"""
from __future__ import annotations

import getpass
import json
import ssl
import sys
import urllib.error
import urllib.request

HOTE = "192.168.0.171"
PORTS = [(8043, "https"), (443, "https"), (8088, "http"), (80, "http")]

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
jar = urllib.request.HTTPCookieProcessor()
op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx), jar)


def appel(url, methode="GET", corps=None, entetes=None):
    d = json.dumps(corps).encode() if corps is not None else None
    r = urllib.request.Request(url, data=d, method=methode,
                               headers={"Content-Type": "application/json", **(entetes or {})})
    with op.open(r, timeout=15) as rep:
        return json.loads(rep.read().decode("utf-8", "replace"))


def trouver_base():
    for port, proto in PORTS:
        base = f"{proto}://{HOTE}:{port}"
        try:
            info = appel(f"{base}/api/info")
            cid = (info.get("result") or {}).get("omadacId")
            if cid:
                print(f"controleur trouve : {base}  (omadacId {cid})")
                return base, cid
        except Exception:
            continue
    sys.exit(f"Aucun controleur joignable sur {HOTE} (ports essayes : {[p for p, _ in PORTS]})")


def main():
    base, cid = trouver_base()
    user = input("Identifiant Omada (compte LOCAL) : ").strip()
    pwd = getpass.getpass("Mot de passe (invisible) : ")

    rep = appel(f"{base}/{cid}/api/v2/login", "POST", {"username": user, "password": pwd})
    if rep.get("errorCode") != 0:
        sys.exit(f"Connexion refusee : {rep.get('msg')} (code {rep.get('errorCode')})")
    token = rep["result"]["token"]
    h = {"Csrf-Token": token}
    print("connecte.\n")

    sites = appel(f"{base}/{cid}/api/v2/sites?currentPage=1&currentPageSize=100", entetes=h)
    liste = (sites.get("result") or {}).get("data", [])
    print(f"=== {len(liste)} SITE(S) sur ce controleur ===")
    for s in liste:
        print(f"   {s.get('name')}   (id {s.get('id')})")
    if len(liste) > 1:
        print("   ⚠️  PLUSIEURS SITES : l'integration HA n'en lit QU'UN. Il faut une instance par site.")
    print()

    for s in liste:
        sid, nom = s.get("id"), s.get("name")
        print(f"=== SITE « {nom} » — endpoint insight/clients (celui que lit HA) ===")
        page, total, tous = 1, None, []
        while True:
            u = f"{base}/{cid}/api/v2/sites/{sid}/insight/clients?currentPage={page}&currentPageSize=100"
            r = appel(u, entetes=h)
            res = r.get("result") or {}
            data = res.get("data", [])
            total = int(res.get("totalRows", len(data)))
            tous.extend(data)
            if len(tous) >= total or not data:
                break
            page += 1

        sansfil = [c for c in tous if c.get("wireless") is True]
        filaire = [c for c in tous if c.get("wireless") is False]
        inconnu = [c for c in tous if c.get("wireless") is None]

        print(f"  totalRows annonce par le controleur : {total}")
        print(f"  recuperes                           : {len(tous)}")
        print(f"  -> SANS FIL (HA cree une entite)    : {len(sansfil)}")
        print(f"  -> filaires (HA les ignore)         : {len(filaire)}")
        print(f"  -> champ 'wireless' ABSENT          : {len(inconnu)}  <-- ignores EN SILENCE par la lib")
        print()
        print("  detail des SANS FIL :")
        for c in sansfil:
            print(f"     {c.get('mac')}  {str(c.get('name') or '-')[:28]:28s} actif={c.get('active')}")
        if inconnu:
            print("\n  detail des 'wireless' ABSENT (la cause probable) :")
            for c in inconnu:
                print(f"     {c.get('mac')}  {str(c.get('name') or '-')[:28]:28s} cles={sorted(c.keys())[:8]}")
        print()

    try:
        appel(f"{base}/{cid}/api/v2/logout", "POST", {}, h)
    except Exception:
        pass


main()
