# Copyright (c) 2026. Erstellt via Vibe Coding. Nur für private und nicht-kommerzielle Nutzung.
"""Baut den Add-on-Ordner fuer Home Assistant: dist/homeassistant/ontofam/.

Home Assistant baut den Container aus genau diesem Ordner. Er enthaelt deshalb neben
Konfiguration und Dockerfile (aus homeassistant/ontofam/) eine Kopie der Programm-
dateien -- nach Aenderungen an app.py & Co. neu bauen und den Ordner erneut kopieren.

Aufruf:
  python build_addon.py                          # nur bauen
  python build_addon.py \\\\homeassistant\\addons   # bauen und auf die Samba-Freigabe kopieren
"""

import shutil
import sys
from pathlib import Path

BASE = Path(__file__).parent
VORLAGE = BASE / "homeassistant" / "ontofam"
ZIEL = BASE / "dist" / "homeassistant" / "ontofam"

# Was app.py zur Laufzeit braucht -- keine Datenbank, keine Einstellungen mit Geheimnissen.
PROGRAMM = ["app.py", "erinnerungen.py", "import_calendar.py", "schema.py",
            "graph_query.py", "export_graph.py"]


def main() -> None:
    if ZIEL.exists():
        shutil.rmtree(ZIEL)
    shutil.copytree(VORLAGE, ZIEL, ignore=shutil.ignore_patterns("start.py", "__pycache__"))
    # Home Assistant faellt auf Englisch zurueck, wenn es keine Uebersetzung findet.
    shutil.copy(VORLAGE / "translations" / "de.yaml", ZIEL / "translations" / "en.yaml")
    shutil.copy(BASE / "requirements.txt", ZIEL)

    app = ZIEL / "app"
    app.mkdir()
    for datei in PROGRAMM:
        shutil.copy(BASE / datei, app)
    shutil.copy(VORLAGE / "start.py", app)
    shutil.copytree(BASE / "static", app / "static")
    print(f"Add-on gebaut: {ZIEL}")

    if len(sys.argv) > 1:
        ziel = Path(sys.argv[1]) / "ontofam"
        if ziel.exists():
            shutil.rmtree(ziel)
        shutil.copytree(ZIEL, ziel)
        print(f"Kopiert nach {ziel}")


if __name__ == "__main__":
    main()
