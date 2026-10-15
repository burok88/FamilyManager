"""Packt mcp_server.py als Claude-Desktop-Extension (.mcpb) nach dist/ontofam.mcpb.

Neuere Claude-Desktop-Versionen lesen keine mcpServers mehr aus
claude_desktop_config.json; lokale MCP-Server werden als Extension installiert:
Claude Desktop -> Einstellungen -> Erweiterungen -> Erweiterte Einstellungen ->
"Erweiterung installieren" -> dist/ontofam.mcpb waehlen.

Das Bundle enthaelt nur das Skript; Python und die Pakete (mcp, requests) kommen
vom System-Python, deshalb steht dessen absoluter Pfad in der Manifest-Config.
"""

import json
import sys
import zipfile
from pathlib import Path

BASE = Path(__file__).parent
DIST = BASE.parent / "dist"
OUT = DIST / "ontofam.mcpb"

MANIFEST = {
    "manifest_version": "0.2",
    "name": "ontofam",
    "display_name": "ontoFam – Haushalts-Ontologie",
    "version": "0.10.0",
    "description": "Personen, Aufgaben und Termine der Familie abfragen und anlegen.",
    "long_description": (
        "Stellt die Haushalts-Ontologie (Kuzu-Graph hinter app.py) als Tools bereit: "
        "was steht an, Aufgabe mit Termin und Zustaendigkeit anlegen, Kalender "
        "synchronisieren, Stundenplaene der Kinder pflegen. Dazu freie, nur lesende "
        "Cypher-Abfragen fuer Fragen, die kein festes Werkzeug abdeckt. "
        "Voraussetzung: ontoFam laeuft -- auf diesem PC (http://localhost:5000) oder als "
        "Home-Assistant-Add-on (http://<Home-Assistant-Adresse>:5000 mit Zugriffsschluessel)."
    ),
    "author": {"name": "ontoFam"},
    "server": {
        "type": "python",
        "entry_point": "server/mcp_server.py",
        "mcp_config": {
            "command": sys.executable,
            "args": ["${__dirname}/server/mcp_server.py"],
            "env": {
                "ONTOFAM_API": "${user_config.adresse}",
                "ONTOFAM_SCHLUESSEL": "${user_config.zugriffsschluessel}",
            },
        },
    },
    # Fragt Claude Desktop bei der Installation ab (Einstellungen -> Erweiterungen ->
    # ontoFam -> Konfigurieren). "sensitive": der Schluessel wird verdeckt eingegeben
    # und im Schluesselspeicher des Betriebssystems abgelegt.
    "user_config": {
        "adresse": {
            "type": "string",
            "title": "Adresse von ontoFam",
            "description": "Auf diesem PC http://localhost:5000, als Home-Assistant-Add-on "
                           "z.B. http://192.168.1.10:5000",
            "default": "http://localhost:5000",
            "required": True,
        },
        "zugriffsschluessel": {
            "type": "string",
            "title": "Zugriffsschluessel",
            "description": "Derselbe wie in der Konfiguration des Add-ons. Auf diesem PC leer lassen.",
            "sensitive": True,
            "required": False,
        },
    },
    "tools": [
        {"name": "personen_auflisten", "description": "Alle Personen des Haushalts"},
        {"name": "aufgaben_auflisten", "description": "Alle Aufgaben"},
        {"name": "anstehende_aufgaben", "description": "Was steht in den naechsten Tagen an"},
        {"name": "aufgabe_anlegen", "description": "Aufgabe mit Termin, Ort, Uhrzeit und Zustaendigkeit anlegen"},
        {"name": "betroffene_person_setzen", "description": "Festhalten, um wen es bei einer Aufgabe geht"},
        {"name": "stundenplan_setzen", "description": "Kompletten Stundenplan uebernehmen, z.B. aus einem PDF"},
        {"name": "stundenplan_abfragen", "description": "Stundenplan eines Kindes abfragen"},
        {"name": "stundenplan_eintragen", "description": "Einzelne Schulstunde nachtragen oder korrigieren"},
        {"name": "stundenplan_loeschen", "description": "Kompletten Stundenplan einer Person loeschen"},
        {"name": "tonnentermine_setzen", "description": "Abholtermine einer Muelltonne uebernehmen"},
        {"name": "zustaendigkeit_setzen", "description": "Aufgabe einer Person zuordnen"},
        {"name": "als_erledigt_markieren", "description": "Festhalten, dass eine Aufgabe erledigt wurde"},
        {"name": "erledigung_zuruecksetzen", "description": "Eine Erledigung zuruecknehmen"},
        {"name": "rueckblick", "description": "Was anstand und ob es erledigt oder verpasst wurde"},
        {"name": "erinnerungen_vorschau", "description": "Welche Erinnerungen demnaechst an wen gehen"},
        {"name": "erinnerung_testen", "description": "Testnachricht an eine Person schicken"},
        {"name": "schema_beschreiben", "description": "Aufbau des Graphen als Vorlage fuer eigene Abfragen"},
        {"name": "graph_abfragen", "description": "Freie, nur lesende Cypher-Abfrage auf der Ontologie"},
        {"name": "person_anlegen", "description": "Neue Person anlegen"},
        {"name": "kalender_synchronisieren", "description": "Google-Kalender sofort abgleichen (laeuft sonst automatisch)"},
    ],
    "keywords": ["haushalt", "familie", "ontologie", "kalender"],
    "license": "SEE LICENSE IN LICENSE.txt",
    "compatibility": {"platforms": ["win32"]},
}


def main() -> None:
    DIST.mkdir(exist_ok=True)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(MANIFEST, indent=2, ensure_ascii=False))
        z.write(BASE / "mcp_server.py", "server/mcp_server.py")
    print(f"Bundle geschrieben: {OUT}")
    print("Installieren: Claude Desktop -> Einstellungen -> Erweiterungen -> Erweiterte "
          "Einstellungen -> Erweiterung installieren")


if __name__ == "__main__":
    main()
