#!/usr/bin/env python3
"""Système agentique de prospection de Yan : une tâche, un agent, le modèle le moins cher qui suffit.

  python3 agents.py veille [--dry] [--depuis AAAA-MM-JJ]   lit la boîte, trie, analyse, répond (16h et 17h30)
  python3 agents.py matin [--dry]                          veille, puis envoi du quota, puis tableur et bilan (10h)
  python3 agents.py lundi                                  prospection de la semaine puis analyse hebdo
  python3 agents.py --selfcheck

Qui fait quoi :
  lire les mails     script IMAP (aucun modèle)          envoyer       send.py (aucun modèle)
  trier une réponse  Haiku                               tableur       export_suivi_sheet.py (aucun modèle)
  analyse besoins    Sonnet + web                        répondre      Sonnet, Opus si le prospect est chaud
  prospection        Sonnet + web (lundi)                analyse       Sonnet (lundi)
  chef d'orchestre   ce script : planning, verrou, journal, notifications, bilan (aucun modèle)
"""
import csv, datetime as dt, email, fcntl, imaplib, json, os, re, shutil, subprocess, sys, time
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr

import send

HERE = os.path.dirname(os.path.abspath(__file__))
AGENTS = os.path.join(HERE, "agents")
REPONSES = os.path.join(HERE, "reponses.csv")
REPONSES_COLS = ["date", "message_id", "prospect", "de", "categorie", "statut", "action", "rdv", "resume", "raison", "modele"]
RDV = os.path.join(HERE, "rdv.csv")
DOSSIERS = os.path.join(HERE, "dossiers")
SYSTEME = os.path.join(HERE, "..", "startups-levees", "systeme_cold_email.md")
# ordre = priorité sur le quota partagé : les startups d'abord (décision de Yan du 16/10), les relances agents prennent le reste
CAMPAGNES = [("send_apps.env", "startups/apps"), ("send_apps_2.env", "startups/apps (boîte 2)"),
             ("send_conciergeries.env", "conciergeries"), ("send_demenageurs.env", "déménageurs"), ("send.env", "architectes")]


def boites():
    """Une config par boîte d'envoi (une par adresse d envoi), chacune avec son journal."""
    vues = {}
    for env_f, _ in CAMPAGNES:
        env = send.load_env(os.path.join(HERE, env_f))
        vues.setdefault(env["USER"], env)
    return list(vues.values())


def journal_de(env):
    """Branche send.py sur le journal de cette boîte : quota, relances et arrêts sont propres à chaque boîte."""
    send.LOG = os.path.join(HERE, env.get("LOG", "envois.csv"))

# ponytail: un modèle par tâche, à changer ici si un agent se montre trop faible ou trop cher
MODELES = {"tri": "haiku", "besoins": "sonnet", "traitement": "sonnet", "traitement_chaud": "opus",
           "prospection": "sonnet", "analyse": "sonnet"}
CHAUD = {"positive", "rdv"}
A_ANALYSER = {"positive", "question", "rdv", "redirection"}
SANS_TRAITEMENT = {"auto", "autre"}

TRI_SCHEMA = {"type": "object", "required": ["categorie", "resume", "prenom_signataire", "creneau_propose", "personne_redirigee"],
              "properties": {"categorie": {"enum": ["positive", "question", "rdv", "redirection", "refus_motive", "refus_net", "stop", "auto", "autre"]},
                             "resume": {"type": "string"}, "prenom_signataire": {"type": "string"},
                             "creneau_propose": {"type": "string"}, "personne_redirigee": {"type": "string"}}}
TRAIT_SCHEMA = {"type": "object", "required": ["action", "statut", "corps", "rdv", "raison", "apprentissage"],
                "properties": {"action": {"enum": ["repondre", "ne_pas_repondre", "brouillon_pour_yan"]},
                               "statut": {"enum": ["positive", "neutre", "rdv", "refus", "stop"]},
                               "corps": {"type": "string"}, "rdv": {"type": "string"}, "raison": {"type": "string"},
                               "apprentissage": {"anyOf": [{"type": "null"}, {"type": "object",
                                   "required": ["ce_quils_ont_dit", "ce_que_ca_apprend", "changement"],
                                   "properties": {"ce_quils_ont_dit": {"type": "string"}, "ce_que_ca_apprend": {"type": "string"},
                                                  "changement": {"type": "string"}}}]}}}

# avis de non-distribution : jamais de réponse, c'est un rebond (consigne de Yan du 19/10)
NON_DISTRIBUE = re.compile(r"undeliver|non remis|non distribu|chec de (la )?(distribution|remise)|delivery (status|failure|has failed)|"
                           r"returned mail|mail delivery (failed|subsystem)|failure notice|address not found|adresse introuvable|"
                           r"mailer-daemon|postmaster|microsoftexchange", re.I)


def non_distribue(h):
    """Vrai pour un avis de non-distribution (rebond), d'après l'expéditeur, l'objet ou le type de message."""
    sujet = str(make_header(decode_header(h.get("Subject", ""))))
    return bool(NON_DISTRIBUE.search(f'{h.get("From", "")} {sujet}')) or "report" in h.get("Content-Type", "").lower()


JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"]


# ---------- appel d'un agent ----------

def claude(prompt, modele, schema=None, outils="", timeout=1800):
    """Lance un agent Claude Code en mode automatique sur l'abonnement de Yan (jeton CLAUDE_CODE_OAUTH_TOKEN)."""
    exe = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    # hors de l'app : on retire les variables de session qui détournent l'appel vers une clé API sans crédit
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith(("ANTHROPIC_", "CLAUDE")) and k != "CLAUDE_CODE_OAUTH_TOKEN")}
    jeton = os.path.join(HERE, ".jeton_claude")  # créé une fois avec `claude setup-token`, jamais commité
    if "CLAUDE_CODE_OAUTH_TOKEN" not in env and os.path.exists(jeton):
        env["CLAUDE_CODE_OAUTH_TOKEN"] = "".join(open(jeton).read().split())  # un copier-coller du terminal coupe le jeton en deux lignes
    cmd = [exe, "-p", "--model", modele, "--no-session-persistence", "--output-format", "json", "--tools", outils]
    if outils:
        cmd += ["--allowedTools", outils]
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=HERE, env=env)
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"agent {modele} : sortie illisible ({(r.stderr or r.stdout)[:300]})")
    if d.get("is_error"):
        raise RuntimeError(f"agent {modele} : {d.get('result')}")
    return d.get("structured_output") if schema else d.get("result", "")


def consigne(nom):
    return open(os.path.join(AGENTS, nom + ".md"), encoding="utf-8").read()


# ---------- boîte mail ----------

def texte(msg):
    """Corps lisible d'un mail : la partie texte, sinon le HTML sans balises."""
    plain, html = [], []
    for part in msg.walk():
        if part.get_content_maintype() != "text" or part.get("Content-Disposition", "").startswith("attachment"):
            continue
        try:
            contenu = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:
            continue
        (plain if part.get_content_subtype() == "plain" else html).append(contenu)
    if plain:
        brut = "\n".join(plain)
    else:
        brut = re.sub(r"<br\s*/?>|</p>|</div>", "\n", "\n".join(html), flags=re.I)
        brut = re.sub(r"<[^>]+>", "", brut)
        brut = re.sub(r"&nbsp;", " ", brut)
    return re.sub(r"\n{3,}", "\n\n", brut).strip()[:5000]


def identifie(expediteur, refs, contactes, par_id):
    """Le prospect d'un mail entrant : son adresse si on lui a écrit, sinon le fil (réponse depuis une autre adresse)."""
    if expediteur in contactes:
        return expediteur
    for mid in re.findall(r"<[^>]+>", refs):
        if mid in par_id:
            return par_id[mid]
    return ""


def lire_reponses():
    return list(csv.DictReader(open(REPONSES, encoding="utf-8"))) if os.path.exists(REPONSES) else []


def boite(env, depuis):
    """Les réponses de prospects pas encore traitées. Lecture seule (BODY.PEEK) : les mails restent non lus pour Yan."""
    log = send.read_log()
    contactes = {r["email"] for r in log}
    par_id = {r["message_id"]: r["email"] for r in log if r["message_id"]}
    deja = {r["message_id"] for r in lire_reponses()}
    moi = env["USER"].lower()
    m = imaplib.IMAP4_SSL(env["IMAP_HOST"], timeout=60)  # une connexion Gmail muette bloquait le passage du 19/10
    m.login(env["USER"], env["PASSWORD"])
    m.select("INBOX", readonly=True)
    _, data = m.search(None, f"(SINCE {depuis.strftime('%d-%b-%Y')})")
    entrants = []
    nums = data[0].split()
    if not nums:
        m.logout()
        return entrants
    # 1) en-têtes seulement, en une requête (la boîte reçoit bien plus que des réponses de prospects)
    _, hd = m.fetch(b",".join(nums), "(BODY.PEEK[HEADER.FIELDS (FROM MESSAGE-ID IN-REPLY-TO REFERENCES SUBJECT CONTENT-TYPE LIST-UNSUBSCRIBE)])")
    candidats = []
    for item in hd:
        if not isinstance(item, tuple):
            continue
        h = email.message_from_bytes(item[1])
        de = parseaddr(h.get("From", ""))[1].lower()
        refs = f'{h.get("In-Reply-To", "")} {h.get("References", "")}'
        mid = (h.get("Message-ID") or "").strip()
        prospect = identifie(de, refs, contactes, par_id)
        if not mid or mid in deja or not prospect or h.get("List-Unsubscribe"):
            continue  # ponytail: une newsletter (lien de désinscription) n'est jamais une réponse, pas d'appel Haiku
        if non_distribue(h):  # rebond : on arrête la séquence, aucun agent ne le voit
            if not any(r["email"] == prospect and r["statut"] in ("rebond", "stop") for r in log):
                send.append_log({"email": prospect, "etape": "-", "message_id": "", "sujet": "",
                                 "date": dt.datetime.now().isoformat(timespec="seconds"), "statut": "rebond"})
                log.append({"email": prospect, "statut": "rebond"})
                print(f"[rebond] {prospect}")
            continue
        candidats.append(item[0].split()[0])
    # 2) corps complet uniquement pour les mails de prospects
    for num in candidats:
        _, md = m.fetch(num, "(BODY.PEEK[])")
        msg = email.message_from_bytes(md[0][1])
        mid = (msg.get("Message-ID") or "").strip()
        de = parseaddr(msg.get("From", ""))[1].lower()
        if not mid or mid in deja or de == moi or re.search(r"mailer-daemon|postmaster|no-?reply", de):
            continue  # ponytail: les rebonds restent gérés par send.py
        refs = f'{msg.get("In-Reply-To", "")} {msg.get("References", "")}'
        prospect = identifie(de, refs, contactes, par_id)
        if prospect:
            sujet = str(make_header(decode_header(msg.get("Subject", ""))))
            entrants.append({"message_id": mid, "de": de, "prospect": prospect, "sujet": sujet,
                             "date": msg.get("Date", ""), "references": refs.strip(), "corps": texte(msg)})
    m.logout()
    return entrants


# ---------- contexte d'un prospect ----------

def fiche(prospect):
    """La ligne du prospect dans les fichiers de leads, et sa campagne."""
    for env_f, campagne in CAMPAGNES:
        env = send.load_env(os.path.join(HERE, env_f))
        chemin = os.path.join(HERE, env.get("LEADS", "leads_emails.csv"))
        if os.path.exists(chemin):
            for r in csv.DictReader(open(chemin, encoding="utf-8-sig"), delimiter=";"):
                if (r.get("email") or "").strip().lower() == prospect:
                    return campagne, {k: v for k, v in r.items() if k and v}
    return "inconnue", {}


def creneaux_libres(aujourdhui, n=6):
    """Créneaux de 20 min proposables : lundi, mardi, vendredi à 10h, 14h, 16h, sans ceux déjà pris."""
    pris = {r["creneau"] for r in csv.DictReader(open(RDV, encoding="utf-8"))} if os.path.exists(RDV) else set()
    out, d = [], aujourdhui + dt.timedelta(days=1)
    while len(out) < n:
        if d.weekday() in (0, 1, 4):  # ponytail: dispo de Yan en dur (indispo mer/jeu), comme send.creneaux
            for h in (10, 14, 16):
                c = f"{JOURS[d.weekday()]} {d.day} {MOIS[d.month - 1]} à {h}h"
                if c not in pris and len(out) < n:
                    out.append(c)
        d += dt.timedelta(days=1)
    return out


def contexte(entrant, campagne, lead, tri=None, dossier=None):
    nb_mails = sum(1 for r in send.read_log() if r["email"] == entrant["prospect"] and r["etape"].isdigit())
    blocs = [f"Campagne : {campagne}. Mails envoyés par Yan à ce prospect : {nb_mails}.",
             "Fiche du prospect :\n" + json.dumps(lead, ensure_ascii=False, indent=1)]
    if tri:
        blocs.append("Tri : " + json.dumps(tri, ensure_ascii=False))
    if dossier:
        blocs.append("Dossier de l'agent d'analyse des besoins :\n" + dossier)
    blocs.append("Créneaux libres de Yan (visio de 20 minutes) : " + " ; ".join(creneaux_libres(dt.date.today())))
    blocs.append(f"Mail reçu de {entrant['de']}, objet « {entrant['sujet']} », le {entrant['date']} :\n---\n{entrant['corps']}\n---")
    return "\n\n".join(blocs)


# ---------- actions ----------

def mail_reponse(env, entrant, corps):
    msg = EmailMessage()
    msg["From"] = formataddr((env.get("FROM_NAME", ""), env["USER"]))
    msg["To"] = entrant["de"]
    sujet = entrant["sujet"] or ""
    msg["Subject"] = sujet if sujet.lower().startswith(("re:", "re :")) else "Re: " + sujet
    msg["In-Reply-To"] = entrant["message_id"]
    msg["References"] = " ".join(x for x in (entrant["references"], entrant["message_id"]) if x).strip()
    msg["Message-ID"] = make_msgid(domain=env["USER"].split("@")[1])
    msg.set_content(corps)
    return msg


def brouillon(env, msg):
    """Range le mail dans les brouillons de Yan : il le relit et l'envoie lui-même."""
    m = imaplib.IMAP4_SSL(env["IMAP_HOST"], timeout=60)  # une connexion Gmail muette bloquait le passage du 19/10
    m.login(env["USER"], env["PASSWORD"])
    _, dossiers = m.list()
    nom = next((re.search(r'"([^"]+)"$|(\S+)$', l.decode()).group(1) or "" for l in dossiers if b"\\Drafts" in l), "[Gmail]/Drafts")
    m.append(f'"{nom}"', "\\Draft", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
    m.logout()


def notifie(titre, texte_):
    """Notification sur le Mac (sur le Lenovo, elle part seulement dans le journal)."""
    print(f"[notif] {titre} : {texte_}")
    if sys.platform == "darwin":
        t = texte_.replace('"', "'")[:200]
        subprocess.run(["osascript", "-e", f'display notification "{t}" with title "{titre}"'], capture_output=True)


def note_apprentissage(nom, appr):
    """Ajoute une ligne au tableau « Ce que chaque réponse nous apprend » (la règle de Yan du 14/10)."""
    cellule = lambda s: (s or "").replace("|", "/").replace("\n", " ").strip()
    ligne = (f"| {dt.date.today().strftime('%d/%m')} | {cellule(nom)} | {cellule(appr['ce_quils_ont_dit'])} | "
             f"{cellule(appr['ce_que_ca_apprend'])} | {cellule(appr['changement'])} (proposé par l'agent) |\n")
    with open(SYSTEME, "a", encoding="utf-8") as f:
        f.write(ligne)


def journal(row):
    nouveau = not os.path.exists(REPONSES)
    with open(REPONSES, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, REPONSES_COLS)
        if nouveau:
            w.writeheader()
        w.writerow(row)


def stoppe_sequence(prospect, statut):
    """Arrête les relances de send.py, même si le prospect a répondu depuis une autre adresse."""
    if not any(r["email"] == prospect and r["statut"] in ("repondu", "stop", "rebond") for r in send.read_log()) or statut == "stop":
        send.append_log({"email": prospect, "etape": "-", "message_id": "", "sujet": "", "date": dt.datetime.now().isoformat(timespec="seconds"),
                         "statut": "stop" if statut == "stop" else "repondu"})


# ---------- les tâches ----------

def traite(env, entrant, dry):
    campagne, lead = fiche(entrant["prospect"])
    nom = lead.get("nom") or entrant["prospect"].split("@")[1]
    tri = claude(consigne("tri") + "\n\n" + contexte(entrant, campagne, lead), MODELES["tri"], TRI_SCHEMA)
    cat = tri["categorie"]
    row = {"date": dt.datetime.now().isoformat(timespec="seconds"), "message_id": entrant["message_id"], "prospect": entrant["prospect"],
           "de": entrant["de"], "categorie": cat, "statut": "", "action": "aucune", "rdv": "", "resume": tri["resume"], "raison": "", "modele": MODELES["tri"]}
    print(f"\n== {nom} ({entrant['de']}) : {cat} | {tri['resume']}")
    if cat in SANS_TRAITEMENT:
        row["raison"] = "répondeur ou mail sans rapport"
        return row
    dossier = None
    if cat in A_ANALYSER:
        dossier = claude(consigne("besoins") + "\n\n" + contexte(entrant, campagne, lead, tri), MODELES["besoins"], outils="WebFetch,WebSearch")
        if not dry:
            os.makedirs(DOSSIERS, exist_ok=True)
            open(os.path.join(DOSSIERS, entrant["prospect"].split("@")[1] + ".md"), "w", encoding="utf-8").write(dossier)
    modele = MODELES["traitement_chaud" if cat in CHAUD else "traitement"]
    res = claude(consigne("traitement") + "\n\n# Règles communes\n" + consigne("regles") + "\n\n# Contexte\n" + contexte(entrant, campagne, lead, tri, dossier),
                 modele, TRAIT_SCHEMA)
    row.update(statut=res["statut"], action=res["action"], rdv=res["rdv"], raison=res["raison"], modele=f"{MODELES['tri']}+{modele}")
    print(f"   -> {res['action']} ({res['statut']}) : {res['raison']}")
    if res["corps"]:
        print("   " + res["corps"].replace("\n", "\n   "))
    if dry:
        if res["apprentissage"]:
            print("   apprentissage :", json.dumps(res["apprentissage"], ensure_ascii=False))
        return row
    if res["action"] == "repondre" and res["corps"].strip():
        statut_envoi = send.send_one(env, mail_reponse(env, entrant, res["corps"]))
        notifie(f"Réponse envoyée à {nom}", res["corps"][:150])
        row["action"] = f"repondre ({statut_envoi})"
    elif res["action"] == "brouillon_pour_yan":
        brouillon(env, mail_reponse(env, entrant, res["corps"]))
        notifie(f"À toi de jouer : {nom}", res["raison"])
    if res["rdv"]:
        nouveau = not os.path.exists(RDV)
        with open(RDV, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if nouveau:
                w.writerow(["date", "prospect", "creneau"])
            w.writerow([dt.date.today().isoformat(), entrant["prospect"], res["rdv"]])
        notifie(f"RDV avec {nom}", f"{res['rdv']} : envoie l'invitation Google Meet")
    if res["apprentissage"]:
        note_apprentissage(nom, res["apprentissage"])
    stoppe_sequence(entrant["prospect"], res["statut"])
    return row


def veille(dry=False, depuis=None):
    depuis = depuis or dt.date.today() - dt.timedelta(days=10)
    for env in boites():  # chaque prospect reçoit la réponse depuis la boîte qui lui a écrit
        journal_de(env)
        try:
            entrants = boite(env, depuis)
        except Exception as x:  # une boîte injoignable ne bloque pas l'autre
            notifie("Boîte injoignable", f"{env['USER']} : {x}")
            continue
        print(f"Veille {dt.datetime.now():%d/%m %H:%M} {env['USER']} : {len(entrants)} réponse(s) de prospect à traiter")
        for e in entrants:
            try:
                row = traite(env, e, dry)
            except Exception as x:  # un agent en panne ne bloque pas les autres réponses ; celle-ci sera reprise à la prochaine veille
                notifie("Erreur agent", f"{e['de']} : {x}")
                continue
            if not dry:
                journal(row)
    journal_de(boites()[0])


def matin(dry=False):
    veille(dry)
    if dry:
        return
    for env_f, campagne in CAMPAGNES:
        print(f"\n=== Envoi {campagne} ===", flush=True)
        subprocess.run([sys.executable, os.path.join(HERE, "send.py"), "--env", env_f], cwd=HERE)
    tableur()
    bilan()


def tableur():
    """Régénère le suivi par prospect (le collage dans Google Sheets passera par l'API une fois le compte de service créé)."""
    dossier = os.path.join(HERE, "..", "startups-levees")
    with open(os.path.join(dossier, "suivi_sheet.tsv"), "w", encoding="utf-8") as f:
        subprocess.run([sys.executable, "export_suivi_sheet.py"], cwd=dossier, stdout=f, check=True)


def bilan():
    log = []
    for env in boites():
        journal_de(env)
        log += send.read_log()
    jour = dt.date.today().isoformat()
    du_jour = [r for r in log if r["date"].startswith(jour)]
    premiers = sum(1 for r in log if r["etape"] == "1")
    rebonds = sum(1 for r in log if r["statut"] == "rebond")
    taux = 100 * rebonds / premiers if premiers else 0
    rep = [r for r in lire_reponses() if r["date"].startswith(jour)]
    lignes = [f"{sum(r['etape'] == '1' for r in du_jour)} premiers mails, {sum(r['etape'].isdigit() and r['etape'] != '1' for r in du_jour)} relances",
              f"{len(rep)} réponse(s) traitée(s), dont {sum(r['action'].startswith('repondre') for r in rep)} répondue(s)",
              f"rebonds cumulés : {taux:.1f} % des premiers mails" + (" (AU-DESSUS DE 3 %)" if taux > 3 else "")]
    os.makedirs(os.path.join(HERE, "rapports"), exist_ok=True)
    open(os.path.join(HERE, "rapports", jour + ".md"), "a", encoding="utf-8").write("\n".join("- " + l for l in lignes) + "\n")
    notifie("Prospection du jour", " | ".join(lignes))


def prospection(n=30):
    """Lot de prospects à valider par Yan (lundi 9h, ou à la demande : python3 agents.py prospection 30)."""
    lot = os.path.join(HERE, f"lot_{dt.datetime.now():%Y-%m-%d_%Hh%M}.csv")
    open(lot, "w", encoding="utf-8").write("metier;ville;nom;site;email;autres_emails;dirigeant;segment;objet;qui;pourquoi;comprendre;preuve\n")
    resume = claude(consigne("prospection") + f"\n\nfichier_lot : {lot}\nnombre_vise : {n}",
                    MODELES["prospection"], outils="Bash,Read,Write,Edit,WebFetch,WebSearch", timeout=5400)
    notifie("Lot de prospects prêt", f"{os.path.basename(lot)} à valider")
    print(resume)
    return resume


def lundi():
    lot = os.path.join(HERE, f"lot_{dt.date.today().isoformat()}.csv")
    open(lot, "w", encoding="utf-8").write("metier;ville;nom;site;email;autres_emails;dirigeant;segment;objet;qui;pourquoi;comprendre;preuve\n")
    try:  # 23 prospects bien vérifiés ont pris 26 min le 16/10 : 90 min de marge
        resume = claude(consigne("prospection") + f"\n\nfichier_lot : {lot}\nnombre_vise : 30",
                        MODELES["prospection"], outils="Bash,Read,Write,Edit,WebFetch,WebSearch", timeout=5400)
        notifie("Lot de prospects prêt", f"{os.path.basename(lot)} à valider avant fusion dans leads_apps.csv")
    except Exception as x:  # l'analyse de la semaine doit tourner même si la prospection échoue
        resume = f"échec de la prospection : {x}"
        notifie("Prospection en échec", str(x)[:150])
    dossier = os.path.join(HERE, "..", "startups-levees")
    tableur()
    donnees = "\n\n".join([
        "Suivi par prospect (TSV) :\n" + open(os.path.join(dossier, "suivi_sheet.tsv"), encoding="utf-8").read()[:30000],
        "Réponses traitées par les agents (CSV) :\n" + (open(REPONSES, encoding="utf-8").read() if os.path.exists(REPONSES) else "aucune"),
        "Ce que chaque réponse nous apprend :\n" + open(SYSTEME, encoding="utf-8").read().split("## 9.")[-1]])
    rapport = claude(consigne("analyse") + "\n\n" + donnees, MODELES["analyse"])
    os.makedirs(os.path.join(HERE, "rapports"), exist_ok=True)
    chemin = os.path.join(HERE, "rapports", f"hebdo_{dt.date.today().isoformat()}.md")
    open(chemin, "w", encoding="utf-8").write(rapport + "\n\n---\nProspection :\n" + resume)
    notifie("Analyse de la semaine", os.path.basename(chemin))


# ---------- chef d'orchestre ----------

def main():
    args = sys.argv[1:]
    if "--selfcheck" in args:
        return selfcheck()
    cmd = args[0] if args else ""
    if cmd not in ("veille", "matin", "lundi", "prospection"):
        sys.exit(__doc__)
    if dt.date.today().weekday() >= 5 and cmd != "veille" and "--dry" not in args:
        sys.exit("Week-end : pas d'envoi.")
    verrou = open(os.path.join(HERE, ".agents.lock"), "w")
    try:
        fcntl.flock(verrou, fcntl.LOCK_EX | fcntl.LOCK_NB)  # deux passages ne se marchent jamais dessus
    except BlockingIOError:
        sys.exit("Un autre passage tourne encore : celui-ci est sauté.")
    dry = "--dry" in args
    depuis = dt.date.fromisoformat(args[args.index("--depuis") + 1]) if "--depuis" in args else None
    try:
        {"veille": lambda: veille(dry, depuis), "matin": lambda: matin(dry), "lundi": lundi,
         "prospection": lambda: prospection(int(args[1]) if len(args) > 1 and args[1].isdigit() else 30)}[cmd]()
    except Exception as x:  # le planning tourne sans personne devant l'écran : toute panne doit se voir
        notifie(f"Agents : échec du passage {cmd}", str(x))
        raise


def selfcheck():
    assert identifie("a@x.fr", "", {"a@x.fr"}, {}) == "a@x.fr"
    assert identifie("perso@gmail.com", "<1@ycasse.com> <2@ycasse.com>", {"a@x.fr"}, {"<2@ycasse.com>": "a@x.fr"}) == "a@x.fr"
    assert identifie("inconnu@y.fr", "<9@z>", {"a@x.fr"}, {}) == ""
    c = creneaux_libres(dt.date(2026, 10, 17))  # samedi
    assert c[:4] == ["lundi 19 octobre à 10h", "lundi 19 octobre à 14h", "lundi 19 octobre à 16h", "mardi 20 octobre à 10h"], c
    assert not any(j in " ".join(c) for j in ("mercredi", "jeudi"))
    msg = email.message_from_string("Content-Type: text/html\n\n<p>Bonjour&nbsp;Yan</p><div>oui</div>")
    assert texte(msg) == "Bonjour Yan\noui"
    r = mail_reponse({"USER": "yan@ycasse.com", "FROM_NAME": "Yan Casse"},
                     {"de": "p@x.fr", "sujet": "Re: L'app X", "message_id": "<m@x>", "references": "<a@y>"}, "Bonjour,\n\nOui.\n\nYan")
    assert r["Subject"] == "Re: L'app X" and r["In-Reply-To"] == "<m@x>" and r["References"] == "<a@y> <m@x>"
    assert mail_reponse({"USER": "y@z.fr"}, {"de": "p@x.fr", "sujet": "L'app", "message_id": "<m>", "references": ""}, "x")["Subject"] == "Re: L'app"
    for nom in ("tri", "besoins", "traitement", "regles", "prospection", "analyse"):
        assert len(consigne(nom)) > 300, nom
    assert set(TRI_SCHEMA["properties"]["categorie"]["enum"]) == CHAUD | A_ANALYSER | SANS_TRAITEMENT | {"refus_motive", "refus_net", "stop"}
    h = email.message_from_string("From: Microsoft Outlook <MicrosoftExchange329e@x.fr>\nSubject: Non remis : L'app X\n\n")
    assert non_distribue(h)
    assert non_distribue(email.message_from_string("From: a@b.fr\nSubject: =?utf-8?q?=C3=89chec_de_la_distribution?=\n\n"))
    assert non_distribue(email.message_from_string("From: a@b.fr\nContent-Type: multipart/report; report-type=delivery-status\n\n"))
    assert not non_distribue(email.message_from_string("From: Jean <j@example.fr>\nSubject: Re: L'app X\n\n"))
    print("selfcheck OK")


if __name__ == "__main__":
    main()
