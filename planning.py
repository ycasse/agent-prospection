#!/usr/bin/env python3
"""Installe le planning des agents dans launchd (macOS) : tourne même quand l'app Claude est fermée.
  python3 planning.py installer     écrit et charge les tâches
  python3 planning.py retirer       les décharge et les supprime
Sur le Lenovo (Linux), même planning en crontab :
  0 10 * * 1-5  cd <dossier> && python3 agents.py matin
  0 16 * * *    cd <dossier> && python3 agents.py veille
  30 17 * * *   cd <dossier> && python3 agents.py veille
  0 9 * * 1     cd <dossier> && python3 agents.py lundi
"""
import os, plistlib, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
AGENTS_DIR = os.path.expanduser("~/Library/LaunchAgents")
PY = "/opt/homebrew/bin/python3"
LUN_VEN = range(1, 6)

# nom : (commande, créneaux launchd)
TACHES = {
    "matin": ("matin", [{"Weekday": j, "Hour": 10, "Minute": 0} for j in LUN_VEN]),
    "veille": ("veille", [{"Hour": 16, "Minute": 0}, {"Hour": 17, "Minute": 30}]),  # 7 jours sur 7 : on répond aussi le week-end
    "lundi": ("lundi", [{"Weekday": 1, "Hour": 9, "Minute": 0}]),
}


def label(nom):
    return f"com.ycasse.prospection.{nom}"


def installer():
    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    for nom, (cmd, quand) in TACHES.items():
        chemin = os.path.join(AGENTS_DIR, label(nom) + ".plist")
        plist = {"Label": label(nom), "ProgramArguments": ["/usr/bin/caffeinate", "-i", PY, os.path.join(HERE, "agents.py"), cmd],
                 "WorkingDirectory": HERE, "StartCalendarInterval": quand,
                 "StandardOutPath": os.path.join(HERE, "logs", f"{nom}.log"),
                 "StandardErrorPath": os.path.join(HERE, "logs", f"{nom}.log"),
                 "EnvironmentVariables": {"PATH": "~/.local/bin:/opt/homebrew/bin:/usr/bin:/bin", "LANG": "fr_FR.UTF-8",
                                          "PYTHONUNBUFFERED": "1"}}
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", chemin], capture_output=True)
        with open(chemin, "wb") as f:
            plistlib.dump(plist, f)
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", chemin], capture_output=True, text=True)
        print(f"{label(nom)} : {'chargé' if r.returncode == 0 else 'ERREUR ' + r.stderr.strip()}")


def retirer():
    for nom in TACHES:
        chemin = os.path.join(AGENTS_DIR, label(nom) + ".plist")
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", chemin], capture_output=True)
        if os.path.exists(chemin):
            os.remove(chemin)
        print(f"{label(nom)} : retiré")


if __name__ == "__main__":
    {"installer": installer, "retirer": retirer}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: sys.exit(__doc__))()
