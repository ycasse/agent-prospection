#!/usr/bin/env python3
"""Vérifie les adresses en file d'attente AVANT envoi, pour éviter les rebonds.
Interroge le serveur mail de chaque domaine (commande RCPT, aucun mail envoyé).
  morte    : le serveur refuse l'adresse (550) -> écrite dans adresses_mortes.txt, send.py la saute
  ok       : le serveur accepte l'adresse ET refuse une adresse bidon (vérification fiable)
  catchall : le serveur accepte tout, même une adresse bidon (rien à en conclure)
  inconnu  : serveur injoignable ou qui bloque la vérification
Usage: python3 verif_adresses.py
"""
import csv, os, smtplib, subprocess, sys, collections, concurrent.futures as cf

FICHIERS = ["leads_conciergeries.csv", "leads_demenageurs.csv", "leads_emails.csv", "leads_apps.csv", "leads_apps_dev.csv"]
MORTES = "adresses_mortes.txt"

def mx(domaine):
    out = subprocess.run(["dig", "+short", "+time=4", "+tries=2", "MX", domaine], capture_output=True, text=True, timeout=20).stdout.split("\n")
    hosts = sorted((int(l.split()[0]), l.split()[1].rstrip(".")) for l in out if len(l.split()) == 2 and l.split()[0].isdigit())
    return hosts[0][1] if hosts else ""

def verifie(domaine, adresses):
    try:
        hote = mx(domaine)
    except Exception:
        hote = ""
    if not hote:
        return {a: "morte" for a in adresses}  # pas de serveur mail du tout
    try:
        s = smtplib.SMTP(hote, 25, timeout=12)
        s.ehlo("mail.ycasse.com"); s.mail("verif@ycasse.com")
        bidon = s.rcpt(f"zz-inexistant-7q3x@{domaine}")[0]
        res = {}
        for a in adresses:
            code, txt = s.rcpt(a)
            # 5.7.x = refus de politique (IP résidentielle, anti-vérification), pas une boîte inexistante (faux « morte » du 16/10)
            refus_politique = b"5.7." in txt
            res[a] = "morte" if code in (550, 551, 553) and not refus_politique else "inconnu" if code != 250 else "catchall" if bidon == 250 else "ok"
        s.quit()
        return res
    except Exception:
        return {a: "inconnu" for a in adresses}

def main():
    deja = {r["email"] for r in csv.DictReader(open("envois.csv"))}
    par_domaine = collections.defaultdict(list)
    for f in FICHIERS:
        for r in csv.DictReader(open(f, encoding="utf-8-sig"), delimiter=";"):
            e = (r.get("email") or "").strip().lower()
            if "@" in e and e not in deja and e not in par_domaine[e.split("@")[1]]:
                par_domaine[e.split("@")[1]].append(e)
    res = {}
    with cf.ThreadPoolExecutor(4) as ex:  # ponytail: 8 connexions simultanées faisaient refuser Outlook à tort
        for d in ex.map(lambda kv: verifie(*kv), par_domaine.items()):
            res.update(d)
    bilan = collections.Counter(res.values())
    mortes = sorted(a for a, v in res.items() if v == "morte")
    # on cumule : un serveur qui ne répond pas aujourd'hui ne ressuscite pas une adresse refusée hier
    deja = set(open(MORTES).read().split()) if os.path.exists(MORTES) else set()
    cumul = sorted(deja | set(mortes))
    open(MORTES, "w").write("\n".join(cumul) + ("\n" if cumul else ""))
    print(f"{len(res)} adresses en file :", dict(bilan))
    for a in mortes:
        print("  morte :", a)
    return res

def selfcheck():
    assert verifie("domaine-qui-n-existe-pas-7q3x.fr", ["a@domaine-qui-n-existe-pas-7q3x.fr"]) == {"a@domaine-qui-n-existe-pas-7q3x.fr": "morte"}
    print("selfcheck ok")

if __name__ == "__main__":
    selfcheck() if "--selfcheck" in sys.argv else main()
