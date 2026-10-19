#!/usr/bin/env python3
"""Envoi cold email en petit volume, stdlib uniquement.

Lit leads_emails.csv (sortie de `npm run emails`), envoie l'email 1 puis une relance
dans le même fil après FOLLOWUP_DAYS jours, et arrête la séquence dès que le prospect
répond ou que l'adresse rebondit (vérifié par IMAP avant chaque passage).

  python3 send.py --dry        affiche ce qui partirait aujourd'hui, n'envoie rien
  python3 send.py              envoie le quota du jour (à lancer une fois par jour ouvré)
  python3 send.py --stats      bilan envoyés / réponses / rebonds
  python3 send.py --selfcheck
"""
import csv, datetime as dt, email, imaplib, os, random, re, smtplib, sys, time
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "envois.csv")
LOG_COLS = ["email", "etape", "message_id", "sujet", "date", "statut"]


def load_env(path):
    env = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"')
    if env.get("PASSWORD_FILE"):  # mot de passe d'application rangé à part (chmod 600), jamais dans le .env partagé
        env["PASSWORD"] = "".join(open(os.path.join(HERE, env["PASSWORD_FILE"])).read().split())
    return env


def load_sequence(path):
    """sequence.txt : 1re ligne = objet, puis les corps séparés par une ligne '---' (email 1, relance 1, relance 2...)."""
    raw = open(path, encoding="utf-8").read().strip()
    first, _, rest = raw.partition("\n")
    return first.strip(), [b.strip() for b in rest.split("\n---\n")]


LEGAL = re.compile(r"\b(SAS|SASU|SARL|EURL|SA|SELARL|SELAS|SCP|SCI|SNC|EIRL|EI)\b\.?", re.I)


def clean_company(nom):
    return re.sub(r"\s{2,}", " ", LEGAL.sub("", nom)).strip(" -,.") or nom


def first_name(dirigeant):
    # ponytail: heuristique simple, un nom de société ramassé par erreur donne juste "Bonjour,"
    parts = dirigeant.split()
    if len(parts) in (2, 3) and parts[0][:1].isupper() and not LEGAL.search(dirigeant):
        return "-".join(p.capitalize() for p in parts[0].split("-"))  # Marc-Antoine, pas Marc-antoine
    return ""


def render(tpl, lead):
    prenom = (lead.get("prenom") or "").strip() or first_name(lead.get("dirigeant", ""))
    vars_ = {k: (v or "").strip() for k, v in lead.items() if k}  # colonnes perso du CSV : {objet}, {pourquoi}...
    vars_.update({
        "prenom": prenom,
        "prenom_virgule": f"{prenom}, " if prenom else "",  # ponytail: objet avec ou sans prénom
        "bonjour": f"Bonjour {prenom}," if prenom else "Bonjour,",
        "cabinet": clean_company(lead.get("nom", "")) or "votre cabinet",
        "ville": lead.get("ville", ""),
        "metier": lead.get("metier", ""),
        "creneaux": creneaux(dt.date.today()),
    })
    return tpl.format_map(vars_)


JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi"]


def creneaux(today):
    """Deux prochains jours où Yan est dispo (lun, mar, ven), à partir du lendemain : « mardi 20 ou vendredi 23 »."""
    # ponytail: dispo de Yan en dur (indispo mer/jeu), à changer ici si son planning bouge
    d, out = today + dt.timedelta(days=1), []
    while len(out) < 2:
        if d.weekday() in (0, 1, 4):
            out.append(f"{JOURS[d.weekday()]} {d.day}")
        d += dt.timedelta(days=1)
    return " ou ".join(out)


def daily_quota(start, today, max_per_day, paliers=""):
    # PALIERS=2026-10-19:20,2026-10-26:30 : le dernier palier atteint fixe le quota (décision de Yan du 16/10)
    atteints = [int(q) for d, q in (p.split(":") for p in paliers.split(",") if p.strip()) if dt.date.fromisoformat(d) <= today]
    if atteints:
        return atteints[-1]
    # montée progressive (warmup manuel) : 5/jour la 1re semaine, +5 par semaine
    weeks = max(0, (today - start).days // 7)
    return min(max_per_day, 5 + 5 * weeks)


def read_log():
    if not os.path.exists(LOG):
        return []
    return list(csv.DictReader(open(LOG, encoding="utf-8")))


def append_log(row):
    new = not os.path.exists(LOG)
    with open(LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, LOG_COLS)
        if new:
            w.writeheader()
        w.writerow(row)


def jours_ouvres(a, b):
    """Jours ouvrés écoulés entre deux dates : lundi -> mercredi = 2, vendredi -> mardi = 2."""
    return sum(1 for i in range(1, (b - a).days + 1) if (a + dt.timedelta(days=i)).weekday() < 5)


def plan(leads, log, today, quota, followup_days, n_steps=2, relances_dabord=False, nouveaux_ok=True):
    """Retourne la liste (lead, etape, parent) à envoyer aujourd'hui.
    Une relance part followup_days jours ouvrés après l'étape précédente, dans le fil de l'email 1.
    Les contacts démarrés lundi et ceux démarrés mardi s'alternent donc tout seuls (groupes A et B)."""
    by_email = {}
    for r in log:
        by_email.setdefault(r["email"], []).append(r)
    stopped = {e for e, rows in by_email.items() if any(r["statut"] in ("repondu", "rebond", "stop") for r in rows)}
    sent_today = sum(1 for r in log if r["date"][:10] == today.isoformat() and r["etape"].isdigit())
    left = max(0, quota - sent_today)
    nouveaux = [(lead, "1", None) for e, lead in leads.items() if e not in by_email] if nouveaux_ok else []
    relances = []
    for e, rows in by_email.items():
        steps = {r["etape"]: r for r in rows if r["etape"].isdigit()}
        if e not in leads or e in stopped or "1" not in steps:  # ponytail: relance uniquement les leads de CETTE campagne
            continue
        last = max(steps, key=int)
        if int(last) < n_steps and jours_ouvres(dt.date.fromisoformat(steps[last]["date"][:10]), today) >= followup_days:
            relances.append((leads[e], str(int(last) + 1), steps["1"]))
    # ponytail: relances_dabord tient la cadence de la séquence ; sinon les nouveaux contacts passent avant (réglage du 05/10)
    return (relances + nouveaux if relances_dabord else nouveaux + relances)[:left]


def load_leads(path, blocklist):
    # adresses refusées par leur serveur lors de `python3 verif_adresses.py` : on ne leur écrit pas
    mortes_f = os.path.join(HERE, "adresses_mortes.txt")
    mortes = set(open(mortes_f).read().split()) if os.path.exists(mortes_f) else set()
    leads = {}
    for row in csv.DictReader(open(path, encoding="utf-8-sig"), delimiter=";"):
        e = row.get("email", "").strip().lower()
        if e and "@" in e and e not in leads and e not in mortes and e.split("@")[1] not in blocklist:
            row["email"] = e
            leads[e] = row
    return leads


AUTO = re.compile(r"ponse automatique|automatic reply|out of office|bureau .{0,12}ferm|accusons (bonne )?r.{1,2}ception", re.I)


def sync_imap(env, log):
    """Marque 'repondu' les prospects qui ont écrit, 'rebond' les adresses en échec."""
    contacted = {r["email"] for r in log}
    stopped = {r["email"] for r in log if r["statut"] in ("repondu", "rebond", "stop")}
    if not contacted:
        return
    since = min(dt.date.fromisoformat(r["date"][:10]) for r in log).strftime("%d-%b-%Y")
    m = imaplib.IMAP4_SSL(env["IMAP_HOST"], timeout=60)  # une connexion Gmail muette bloquait le passage du 19/10
    m.login(env["USER"], env["PASSWORD"])
    m.select("INBOX", readonly=True)
    _, data = m.search(None, f'(SINCE {since})')
    now = dt.datetime.now().isoformat(timespec="seconds")
    for num in data[0].split():
        _, msg_data = m.fetch(num, "(RFC822)")
        msg = email.message_from_bytes(msg_data[0][1])
        sender = email.utils.parseaddr(msg.get("From", ""))[1].lower()
        if msg.get("List-Unsubscribe"):  # newsletter d'un prospect auquel Yan est abonné (19/10) : pas une réponse
            continue
        if sender in contacted and sender not in stopped:
            # ponytail: un répondeur (absence, accusé de réception) n'est pas une réponse, la relance doit partir
            if msg.get("Auto-Submitted", "no") != "no" or AUTO.search(msg.get("Subject", "") + msg.as_string()[-1500:]):
                continue
            append_log({"email": sender, "etape": "-", "message_id": "", "sujet": "", "date": now, "statut": "repondu"})
            stopped.add(sender)
            print(f"[reponse] {sender}")
        elif re.search(r"mailer-daemon|postmaster", sender):
            body = msg.as_string().lower()
            for e in contacted - stopped:
                if e in body:
                    append_log({"email": e, "etape": "-", "message_id": "", "sujet": "", "date": now, "statut": "rebond"})
                    stopped.add(e)
                    print(f"[rebond] {e}")
    m.logout()


def build(env, lead, subject, body, parent):
    msg = EmailMessage()
    domain = env["USER"].split("@")[1]
    msg["From"] = formataddr((env.get("FROM_NAME", ""), env["USER"]))
    msg["To"] = lead["email"]
    msg["Message-ID"] = make_msgid(domain=domain)
    if parent:
        msg["Subject"] = "Re: " + parent["sujet"]
        msg["In-Reply-To"] = msg["References"] = parent["message_id"]
    else:
        msg["Subject"] = subject
    msg.set_content(body)  # texte brut, pas de pixel de suivi
    return msg


def send_one(env, msg):
    """Une connexion par email : les pauses de plusieurs minutes font tomber une connexion gardée ouverte."""
    for attempt in range(1, 6):
        try:
            with smtplib.SMTP_SSL(env["SMTP_HOST"], int(env.get("SMTP_PORT", "465")), timeout=60) as smtp:
                smtp.login(env["USER"], env["PASSWORD"])
                smtp.send_message(msg)
            return "envoye"
        except smtplib.SMTPRecipientsRefused:
            return "rebond"
        except (smtplib.SMTPServerDisconnected, OSError) as e:
            if attempt == 5:
                sys.exit(f"[reseau] toujours coupé après 5 essais ({e}). Relance plus tard : rien n'est envoyé en double.")
            print(f"[reseau] {e}, essai {attempt}/5, nouvel essai dans 2 min")
            time.sleep(120)


def main():
    args = sys.argv[1:]
    if "--selfcheck" in args:
        return selfcheck()
    if "--stats" in args:
        log = read_log()
        for s in sorted({r["etape"] for r in log if r["etape"].isdigit()}):
            print(f"email {s} envoyés : {sum(r['etape'] == s for r in log)}")
        for s in ("repondu", "rebond"):
            print(f"{s} : {sum(r['statut'] == s for r in log)}")
        return
    dry = "--dry" in args
    env_file = sys.argv[sys.argv.index("--env") + 1] if "--env" in sys.argv else "send.env"  # ponytail: 2e campagne = 2e fichier env
    env = load_env(os.path.join(HERE, env_file))
    global LOG
    LOG = os.path.join(HERE, env.get("LOG", "envois.csv"))  # un journal par boîte d'envoi = un quota par boîte
    subject, corps = load_sequence(os.path.join(HERE, env.get("SEQUENCE", "sequence.txt")))
    today = dt.date.today()
    if today.weekday() >= 5 and not dry:
        sys.exit("Week-end : pas d'envoi.")
    blocklist = {d.strip().lower() for d in env.get("BLOCK_DOMAINS", "").split(",") if d.strip()}
    leads = load_leads(os.path.join(HERE, env.get("LEADS", "leads_emails.csv")), blocklist)
    if not dry:
        sync_imap(env, read_log())
    quota = daily_quota(dt.date.fromisoformat(env["START_DATE"]), today, int(env.get("MAX_PER_DAY", "20")), env.get("PALIERS", ""))
    # NOUVEAUX=0 : la campagne ne fait plus que finir ses séquences en cours
    todo = plan(leads, read_log(), today, quota, int(env.get("FOLLOWUP_DAYS", "3")), len(corps),
                env.get("RELANCES_DABORD", "") == "1", env.get("NOUVEAUX", "1") != "0")
    todo = todo[:int(env.get("CAMPAIGN_MAX", "999"))]  # ponytail: part de cette campagne dans le quota partagé
    print(f"Quota du jour : {quota} | à envoyer : {len(todo)} | leads dispo : {len(leads)}")
    for i, (lead, step, parent) in enumerate(todo):
        msg = build(env, lead, render(subject, lead), render(corps[int(step) - 1], lead), parent)
        if dry:
            print(f"\n===== étape {step} -> {msg['To']} | {msg['Subject']}\n{msg.get_content()}")
            continue
        if not 8 <= dt.datetime.now().hour < 18:
            print("18h passées : arrêt, le reste partira demain.")
            break
        statut = send_one(env, msg)
        append_log({"email": lead["email"], "etape": step, "message_id": msg["Message-ID"],
                    "sujet": msg["Subject"], "date": dt.datetime.now().isoformat(timespec="seconds"), "statut": statut})
        print(f"[{statut}] étape {step} -> {lead['email']}")
        if i < len(todo) - 1:
            time.sleep(random.randint(90, 300))  # espacement humain entre deux envois


def selfcheck():
    global LOG
    import tempfile
    assert clean_company("Archi Studio SAS") == "Archi Studio"
    assert clean_company("SARL Dupont & Fils") == "Dupont & Fils"
    assert first_name("Jean Dupont") == "Jean"
    assert first_name("Marc-Antoine Seguy") == "Marc-Antoine"
    assert render("{bonjour}", {"prenom": "Ahmed", "dirigeant": ""}) == "Bonjour Ahmed,"
    assert first_name("David Alves Rénovation SARL") == ""
    assert render("{bonjour} {cabinet}", {"nom": "Atelier X EURL", "dirigeant": "marie curie"}) == "Bonjour, Atelier X"
    s = dt.date(2026, 9, 1)
    assert [daily_quota(s, s + dt.timedelta(days=d), 20) for d in (0, 7, 14, 21, 60)] == [5, 10, 15, 20, 20]
    pal = "2026-10-19:20,2026-10-26:30"
    assert [daily_quota(dt.date(2026, 9, 28), dt.date(2026, 10, d), 20, pal) for d in (16, 19, 23, 26, 30)] == [15, 20, 20, 30, 30]
    assert creneaux(dt.date(2026, 10, 16)) == "lundi 19 ou mardi 20"
    assert creneaux(dt.date(2026, 10, 20)) == "vendredi 23 ou lundi 26"
    assert creneaux(dt.date(2026, 10, 19)) == "mardi 20 ou vendredi 23"
    assert render("{objet} {creneaux}", {"objet": "L'app X"}).startswith("L'app X ")
    today = dt.date(2026, 10, 1)
    leads = {e: {"email": e} for e in ("a@x.fr", "b@x.fr", "c@x.fr", "d@x.fr", "n@x.fr")}
    log = [
        {"email": "a@x.fr", "etape": "1", "message_id": "<1>", "sujet": "q", "date": "2026-09-27", "statut": "envoye"},  # relance due
        {"email": "b@x.fr", "etape": "1", "message_id": "<2>", "sujet": "q", "date": "2026-09-30", "statut": "envoye"},  # trop tôt
        {"email": "c@x.fr", "etape": "1", "message_id": "<3>", "sujet": "q", "date": "2026-09-20", "statut": "envoye"},
        {"email": "c@x.fr", "etape": "-", "message_id": "", "sujet": "", "date": "2026-09-21", "statut": "repondu"},      # a répondu
        {"email": "d@x.fr", "etape": "1", "message_id": "<4>", "sujet": "q", "date": "2026-09-20", "statut": "envoye"},
        {"email": "d@x.fr", "etape": "2", "message_id": "<5>", "sujet": "Re: q", "date": "2026-09-24", "statut": "envoye"},  # fini
    ]
    p = plan(leads, log, today, 10, 3)
    assert [(l["email"], st) for l, st, _ in p] == [("n@x.fr", "1"), ("a@x.fr", "2")], p
    assert plan(leads, log, today, 1, 3)[0][1] == "1"  # nouveaux contacts prioritaires
    assert plan(leads, log, today, 1, 3, relances_dabord=True)[0][1] == "2"  # ou la séquence d'abord
    assert [st for _, st, _ in plan(leads, log, today, 10, 3, nouveaux_ok=False)] == ["2"]  # NOUVEAUX=0 : relances seules
    lun, mar = dt.date(2026, 10, 12), dt.date(2026, 10, 13)
    assert [jours_ouvres(lun, lun + dt.timedelta(days=d)) for d in (0, 1, 2, 4, 7)] == [0, 1, 2, 4, 5]
    assert jours_ouvres(dt.date(2026, 10, 16), dt.date(2026, 10, 20)) == 2  # vendredi -> mardi
    # séquence de 5 mails, 2 jours ouvrés d'écart : groupe A (lundi) et groupe B (mardi) s'alternent
    seq = [{"email": "a@x.fr", "etape": "1", "message_id": "<a>", "sujet": "q", "date": lun.isoformat(), "statut": "envoye"},
           {"email": "b@x.fr", "etape": "1", "message_id": "<b>", "sujet": "q", "date": mar.isoformat(), "statut": "envoye"}]
    ab = {e: leads[e] for e in ("a@x.fr", "b@x.fr")}
    vu = []
    for d in range(2, 12):
        jour = lun + dt.timedelta(days=d)
        if jour.weekday() >= 5:
            continue
        for l, st, parent in plan(ab, seq, jour, 10, 2, 5, True):
            assert parent["etape"] == "1"
            vu.append((jour.strftime("%a"), l["email"][0], st))
            seq.append({"email": l["email"], "etape": st, "message_id": "<r>", "sujet": "Re: q", "date": jour.isoformat(), "statut": "envoye"})
    assert vu == [("Wed", "a", "2"), ("Thu", "b", "2"), ("Fri", "a", "3"), ("Mon", "b", "3"), ("Tue", "a", "4"), ("Wed", "b", "4"), ("Thu", "a", "5"), ("Fri", "b", "5")], vu
    assert load_sequence.__doc__ and len("a\n---\nb\n---\nc".split("\n---\n")) == 3
    assert not [x for x in plan(leads, log + [{"email": "autre@campagne.fr", "etape": "1", "message_id": "x", "sujet": "s", "date": "2026-09-20T10:00:00", "statut": "envoye"}], today, 10, 3) if x[0]["email"] == "autre@campagne.fr"]
    msg = build({"USER": "yan@test.fr"}, {"email": "a@x.fr"}, "q", "corps", log[0])
    assert msg["Subject"] == "Re: q" and msg["In-Reply-To"] == "<1>"
    with tempfile.TemporaryDirectory() as d:
        LOG = os.path.join(d, "l.csv")
        append_log(log[0])
        assert read_log()[0]["email"] == "a@x.fr"
    print("selfcheck OK")


if __name__ == "__main__":
    main()
