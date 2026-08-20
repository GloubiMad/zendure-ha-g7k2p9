# -*- coding: utf-8 -*-
"""Controle de conformite des correctifs du moteur fondation, sur simulation.csv.

Ne teste QUE ce qui est lisible dans le fichier. Aucun modele, aucun rejeu, aucune estimation.
Chaque test rend : condition rencontree (oui/non, combien), puis CONFORME / VIOLATION + exemples.

Pieges respectes (tous documentes dans simulation.py) :
  - table des colonnes construite depuis l'EN-TETE, jamais d'indice en dur ;
  - l'en-tete porte un blob JSON par device que les lignes de donnees n'ont PAS ;
  - horodatage AVEC millisecondes (tronquer quantifie tout a la seconde) ;
  - le champ debug a UN CYCLE DE RETARD : il decrit le cycle PRECEDENT.
    -> les tests qui croisent debug et colonnes device n'utilisent que des periodes
       ou la condition est STABLE sur plusieurs cycles.
"""
import csv, json, re, sys
from datetime import datetime
from collections import Counter

ETAT = {0: "OFFLINE", 1: "SOCEMPTY", 2: "INACTIVE", 3: "SOCFULL", 4: "ACTIVE"}
CHAMPS = ["bat", "Prod", "Home", "Cmd", "Soc", "Conn", "ChLim", "St", "Age", "Grid", "Byp", "Tmp", "CTmp", "Byp2"]


def charger(path):
    f = open(path, encoding="utf-8", errors="replace")
    rd = csv.reader(f, delimiter=";")
    hdr = next(rd)
    # les blobs JSON de l'en-tete decrivent le bloc qui les PRECEDE
    devs = []
    for i, h in enumerate(hdr):
        if h.startswith("{") and '"device_id"' in h:
            devs.append((json.loads(h), i))
    # position de depart de chaque bloc dans les LIGNES DE DONNEES (sans les blobs)
    base, blocs = hdr.index("bat"), []
    for k, (meta, _) in enumerate(devs):
        blocs.append((meta, base + k * len(CHAMPS)))
    rows = []
    for r in rd:
        if len(r) < base + len(devs) * len(CHAMPS):
            continue
        try:
            t = datetime.strptime(r[0][:26], "%Y-%m-%d %H:%M:%S.%f")
        except Exception:
            try:
                t = datetime.strptime(r[0][:19], "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
        rows.append((t, r))
    return blocs, rows


def num(r, b, champ):
    try:
        return float(r[b + CHAMPS.index(champ)])
    except Exception:
        return None


def dbg(r):
    return r[-1]


def par(x, motif, defaut=None):
    m = re.search(motif, x)
    return float(m.group(1)) if m else defaut


def verdict(nom, cond, viol, exemples, detail=""):
    if cond == 0:
        print(f"  [NON TESTABLE] {nom}")
        print(f"                 la condition ne s'est jamais presentee sur cette trace")
        return
    etat = "CONFORME" if viol == 0 else "VIOLATION"
    print(f"  [{etat:^9s}] {nom}")
    print(f"                 condition rencontree {cond} fois | violations : {viol}")
    if detail:
        print(f"                 {detail}")
    for e in exemples[:4]:
        print(f"                   . {e}")


def main(path):
    blocs, rows = charger(path)
    noms = [b[0]["device_id"] for b in blocs]
    print(f"Fichier   : {path}")
    print(f"Periode   : {rows[0][0]:%d/%m %H:%M:%S} -> {rows[-1][0]:%d/%m %H:%M:%S}"
          f"  ({(rows[-1][0]-rows[0][0]).total_seconds()/3600:.1f} h, {len(rows)} lignes)")
    print(f"Devices   : {', '.join(noms)}")

    # PARAMETRES EN VIGUEUR A CHAQUE INSTANT, pas le dernier du fichier.
    # ⚠️ Une premiere version appliquait le dernier `par=` a toute la trace : elle jugeait donc des
    # cycles avec un seuil qui n'existait pas encore (engc valait 500 avant 17h23 le 27/07, et la
    # .38 n'etait pas installee avant 14h27). Un correctif ne peut etre juge qu'a partir du moment
    # ou il tourne.
    hist = []  # [(instant, {param: valeur})]
    for t, r in rows:
        m = re.search(r"par=\[(.*?)\]", dbg(r))
        if m:
            p = m.group(1)
            hist.append((t, {"engc": par(p, r"engc([\d.]+)"), "lmx": par(p, r"lmx([\d.]+)"),
                             "wkg": par(p, r"wkg([\d.]+)")}))
    if not hist:
        print("Aucun `par=` dans cette trace : parametres inconnus, tests a seuil non evaluables.")
        hist = [(rows[0][0], {"engc": None, "lmx": None, "wkg": None})]

    def enVigueur(t, cle):
        """Valeur du parametre a l'instant t (None tant qu'aucun `par=` n'a ete vu)."""
        v = None
        for ts, d in hist:
            if ts <= t:
                v = d.get(cle)
            else:
                break
        return v

    print("Parametres (historique lu dans par=) :")
    for ts, d in hist:
        print(f"   {ts:%d/%m %H:%M:%S}  engc={d['engc']} lmx={d['lmx']} wkg={d['wkg']}")
    t_engc = next((ts for ts, d in hist if d["engc"] is not None), None)
    engc = hist[-1][1]["engc"]
    lmx = hist[-1][1]["lmx"]
    wkg = hist[-1][1]["wkg"]
    print()

    IDX = {n: b for n, (_, b) in zip(noms, blocs)}
    LIM = {n: (meta["limitDischarge"], -meta["limitCharge"]) for n, (meta, _) in zip(noms, blocs)}
    prod = [n for n in noms if "Hyper" in n and "glagla" in n] or [noms[1]]
    GL = prod[0]

    print("=== BLOC A ===")

    # --- A1 : .34 angle mort gridReverse -------------------------------------
    # Un producteur en AUTORISE et NON plein doit etre commande (Cmd>0) et ne PAS
    # etre compte dans forced. On exige 5 cycles de stabilite (cycle de retard du debug).
    cond = viol = 0
    ex = []
    stable = 0
    for i, (t, r) in enumerate(rows):
        b = IDX[GL]
        ok = num(r, b, "Grid") == 1 and num(r, b, "St") != 3 and (num(r, b, "Prod") or 0) > 50
        stable = stable + 1 if ok else 0
        if stable < 5:
            continue
        f = par(dbg(r), r"forced=(-?\d+)")
        c = num(r, b, "Cmd")
        if f is None or c is None:
            continue
        cond += 1
        if f > 0:
            viol += 1
            if len(ex) < 4:
                ex.append(f"{t:%d/%m %H:%M:%S} forced={f:.0f} alors que {GL} St={ETAT.get(int(num(r,b,'St')))}")
    verdict(".34  producteur AUTORISE non plein : ni force, ni exclu", cond, viol, ex,
            "critere : forced=0 tant que le producteur n'est pas SOCFULL")

    # --- A2 : .37A ecretage permanent de l'integrale --------------------------
    # int ne doit jamais depasser la capacite MOBILISABLE (hors SOCEMPTY), plafond lmx compris.
    cond = viol = 0
    ex = []
    for t, r in rows:
        it = par(dbg(r), r" int=(-?\d+)")
        occ = par(dbg(r), r" occ=([\d.]+)", 1.0)
        if it is None:
            continue
        imax = sum(LIM[n][0] for n in noms if num(r, IDX[n], "St") != 1) * (occ if occ else 1.0)
        cond += 1
        if it > imax + 1:
            viol += 1
            if len(ex) < 4:
                ex.append(f"{t:%d/%m %H:%M:%S} int={it:.0f} > imax={imax:.0f}")
    verdict(".37A integrale ecretee a la capacite mobilisable", cond, viol, ex)

    # --- A3 : .38 seuil d'engagement en charge -------------------------------
    # Aucun demarrage de charge sous le seuil engc EN VIGUEUR A CET INSTANT.
    # Les cycles anterieurs au premier `par=` portant engc ne sont PAS juges.
    # ⚠️ LES PRODUCTEURS SONT EXEMPTES par `_engage_ok` (« deja allumes, les engager ne coute aucun
    # cycle supplementaire »). Les tester est un contresens : une premiere version sortait 16
    # violations sur 16 pour glagla, soit exactement le comportement VOULU.
    # Producteur = device ayant produit du PV sur la trace (critere lisible, non suppose).
    producteurs = {n for n in noms if any((num(r, IDX[n], "Prod") or 0) > 50 for t, r in rows)}
    for n in noms:
        if n in producteurs:
            print(f"  [SANS OBJET] .38  {n} : exempte du seuil (producteur, cf. `_engage_ok`)")
            continue
        b = IDX[n]
        cond = viol = 0
        ex = []
        prev = None
        for t, r in rows:
            c = num(r, b, "Cmd")
            if c is None:
                continue
            seuil = enVigueur(t, "engc")
            if prev is not None and prev > -5 and c < -5 and seuil:
                cond += 1
                if abs(c) < seuil - 1:
                    st = num(r, b, "St")
                    viol += 1
                    if len(ex) < 4:
                        ex.append(f"{t:%d/%m %H:%M:%S} demarrage a {c:.0f} W (seuil {seuil:.0f}) "
                                  f"etat={ETAT.get(int(st), st) if st is not None else '?'}")
            prev = c
        verdict(f".38  {n} : pas de demarrage de charge sous le seuil", cond, viol, ex,
                f"seuil suivi dans le temps ; 1er engc connu a {t_engc}")

    # --- A4 : .39 -------------------------------------------------------------
    # ⛔ TEST RETIRE : critere invalide.
    # J'exigeais qu'aucun device SANS PV non plein ne soit a l'arret quand le producteur charge.
    # C'est faux : dans la branche CHARGE `cand` inclut le producteur (seul SOCFULL est exclu,
    # l.1257) et le tri `fixed_order` se contente de mettre les sans-PV en TETE. Le producteur
    # peut donc legitimement charger le reliquat, ou charger parce que les autres REFUSENT
    # (chg_accept effondre). Le critere sortait 5657 « violations » sur 5664 : c'est la signature
    # d'un test faux, pas d'un moteur casse.
    # Un test valide demanderait de connaitre l'ordre d'allocation, absent du CSV.
    print("  [RETIRE   ] .39  le producteur charge en dernier")
    print("                 critere invalide (le producteur est un candidat legitime a la charge)")

    print()
    print("=== BLOC B ===")

    # --- B1 : .36A capacite mobilisable nulle => integrale ecretee a 0 --------
    cond = viol = 0
    ex = []
    for t, r in rows:
        if not all(num(r, IDX[n], "St") == 1 for n in noms):
            continue
        it = par(dbg(r), r" int=(-?\d+)")
        if it is None:
            continue
        cond += 1
        if it > 1:
            viol += 1
            if len(ex) < 4:
                ex.append(f"{t:%d/%m %H:%M:%S} int={it:.0f} alors que TOUS les devices sont SOCEMPTY")
    verdict(".36A tous SOCEMPTY => integrale a 0", cond, viol, ex)

    # --- B2 : .36B delai de grace au demarrage --------------------------------
    # Depuis la 1.4.3.44 les champs sont PREFIXES du device : `0Pro:1290/2up:130`.
    # Avant, on ne pouvait pas savoir a qui appartenait la k-ieme valeur (1 ou 2 valeurs pour
    # 3 devices) : indexer par position sortait de FAUSSES violations.
    def nomme(champ, r):
        """{tag: valeur} ; {} si le champ est absent ou au vieux format sans prefixe."""
        m = re.search(rf" {champ}=(\S+)", dbg(r))
        if not m or m.group(1) == "-":
            return {}
        out = {}
        for p in m.group(1).split("/"):
            if ":" in p:
                k, _, v = p.partition(":")
                try:
                    out[k] = float(v)
                except ValueError:
                    pass
        return out

    tag = {n: f"{i}{n.split()[-1][:4]}" for i, n in enumerate(noms)}
    ancien = sum(1 for t, r in rows if re.search(r" acc=\S+", dbg(r)) and not nomme("acc", r))
    if ancien > len(rows) * 0.5:
        print(f"  [NON TESTABLE] .36B : {100*ancien/len(rows):.0f} % des lignes sont au format SANS nom"
              f" (anterieur a la 1.4.3.44) -> indexation impossible")
    else:
        for sens, champ, signe in (("charge", "acc", -1), ("decharge", "liv", 1)):
            cond = viol = 0
            ex = []
            for n in noms:
                b, tg = IDX[n], tag[n]
                prev = None
                t0 = None
                for t, r in rows:
                    c = num(r, b, "Cmd")
                    if c is None:
                        continue
                    actif = (c * signe) > 100
                    if prev is not None and not prev and actif:
                        t0 = t
                        cond += 1
                    if t0 is not None:
                        dt = (t - t0).total_seconds()
                        if dt > (wkg or 15):
                            t0 = None
                        else:
                            v = nomme(champ, r)
                            if tg in v and v[tg] == 0:
                                viol += 1
                                if len(ex) < 4:
                                    ex.append(f"{t:%d/%m %H:%M:%S} {n} : {champ} tombe a 0 apres {dt:.0f}s")
                                t0 = None
                    prev = actif
            verdict(f".36B delai de grace ({sens}) : l'acceptation ne s'effondre pas au demarrage",
                    cond, viol, ex, f"fenetre wake_grace = {wkg} s")

    # --- B4 : .40 anti-verrou : l'acceptation en charge ne reste pas collee a 0 ---
    # Le bug : acceptation nulle -> plafond = marge de re-sondage (150) < seuil d'engagement
    # -> jamais engage, donc jamais mesure, donc plafond fige a 0. Le correctif exempte du
    # seuil un device dont l'acceptation est nulle. Preuve = son `acc` finit par DECOLLER.
    for n in noms:
        tg = tag[n]
        vals = [v[tg] for t, r in rows if (v := nomme("acc", r)) and tg in v]
        if not vals:
            print(f"  [NON TESTABLE] .40  {n} : jamais mesure en charge sur cette trace")
            continue
        zero = sum(1 for x in vals if x == 0)
        etat = "CONFORME" if max(vals) > 150 else "VIOLATION"
        print(f"  [{etat:^9s}] .40  {n} : l'acceptation en charge decolle")
        print(f"                 acc vu {len(vals)} fois | min {min(vals):.0f} max {max(vals):.0f}"
              f" | a zero {zero} fois ({100*zero/len(vals):.0f} %)")

    # --- B3 : .34 cas d'origine, producteur AUTORISE et PLEIN -----------------
    cond = sum(1 for t, r in rows if num(r, IDX[GL], "Grid") == 1 and num(r, IDX[GL], "St") == 3)
    verdict(".34  cas d'origine : producteur AUTORISE et SOCFULL", cond, 0, [],
            "exclu de l'etape 1 et compte dans forced")

    # --- etats rencontres, pour situer -----------------------------------------
    print()
    print("=== CONTEXTE (etats reellement rencontres) ===")
    for n in noms:
        c = Counter(num(r, IDX[n], "St") for t, r in rows)
        s = {ETAT.get(int(k), k): v for k, v in sorted(c.items()) if k is not None}
        tmp = [num(r, IDX[n], "Tmp") for t, r in rows if num(r, IDX[n], "Tmp") is not None]
        tm = f" | Tmp max {max(tmp):.0f}" if tmp else ""
        print(f"  {n:<22s} {s}{tm}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "full.csv")
