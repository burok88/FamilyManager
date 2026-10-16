# Copyright (c) 2026. Erstellt via Vibe Coding. Nur für private und nicht-kommerzielle Nutzung.
"""Startet ontoFam als Home-Assistant-Add-on.

Uebersetzt die Add-on-Optionen (/data/options.json, gepflegt im Reiter
"Konfiguration") in die Dateien, die ontoFam auch auf dem PC liest, und startet dann
app.py. Geheimnisse (Kalenderadresse, Zugriffsschluessel) erscheinen nie im Protokoll.
"""

import json
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

OPTIONEN = Path("/data/options.json")
# Einstellungen in /data: nur fuer das Add-on sichtbar, nicht auf der Samba-Freigabe.
EINSTELLUNGEN = Path("/data")
# Datenbank in /config (addon_config): per Samba erreichbar und im Add-on-Backup.
DATENBANK = Path("/config/kuzu_db")
# Von dieser Adresse reicht Home Assistant die Seitenleiste (Ingress) weiter. Wer dort
# ankommt, ist schon bei Home Assistant angemeldet.
SUPERVISOR = "172.30.32.2"
# MCP-Server fuer Assist. Der Port ist nicht nach aussen freigegeben: erreichbar nur im
# internen Netz von Home Assistant, unter dem Hostnamen dieser App.
MCP_PORT = 8765


def kuzu_pruefen() -> None:
    """Kuzu braucht neuere Prozessorbefehle. Eine Proxmox-VM mit CPU-Typ kvm64 bietet
    sie nicht an -- dann stirbt der Prozess ohne Fehlermeldung. Lieber vorher pruefen
    und sagen, was zu tun ist."""
    probe = subprocess.run([sys.executable, "-c", "import kuzu; kuzu.Connection(kuzu.Database())"])
    if probe.returncode == -signal.SIGILL:
        print("FEHLER: Der Prozessor der VM kennt Befehle nicht, die die Datenbank braucht.\n"
              "Proxmox: VM -> Hardware -> Prozessoren -> Typ auf 'host' stellen, VM neu starten.",
              flush=True)
        sys.exit(1)
    if probe.returncode != 0:
        sys.exit(f"FEHLER: Kuzu laesst sich nicht starten (Rueckgabewert {probe.returncode}).")


def mcp_starten() -> None:
    """Startet den MCP-Server als Nebenprozess. Er ruft die API ueber localhost auf und
    braucht deshalb keinen Zugriffsschluessel. Er laeuft weiter, wenn sich dieser Prozess
    gleich per exec durch app.py ersetzt, und endet mit dem Container."""
    subprocess.Popen([sys.executable, str(Path(__file__).parent / "mcp_server.py"),
                      "--sse", "--host", "0.0.0.0", "--port", str(MCP_PORT)],
                     env=dict(os.environ, ONTOFAM_API="http://127.0.0.1:5000"))
    print(f"Werkzeuge fuer Assist: Integration 'Model Context Protocol' mit "
          f"http://{socket.gethostname()}:{MCP_PORT}/sse", flush=True)


def main() -> None:
    optionen = json.loads(OPTIONEN.read_text(encoding="utf-8"))

    kalender = [{"name": k["name"], "url": k["url"]} for k in optionen["kalender"] if k.get("url")]
    quellen = EINSTELLUNGEN / "calendar_sources.json"
    if kalender:
        quellen.write_text(json.dumps(kalender, ensure_ascii=False), encoding="utf-8")
    else:
        quellen.unlink(missing_ok=True)
    print(f"Kalender: {', '.join(k['name'] for k in kalender) or 'keiner eingerichtet'}")

    empfaenger = {e["person"]: e["dienst"] for e in optionen["empfaenger"]}
    (EINSTELLUNGEN / "erinnerungen.json").write_text(json.dumps({
        "abend": optionen["abend"],
        "morgen": optionen["morgen"],
        "stunden_vorher": optionen["stunden_vorher"],
        "empfaenger": empfaenger,
        "kalender_schreiben": (optionen.get("kalender_schreiben") or "").strip(),
        "kalender_automatisch": optionen.get("kalender_automatisch", True),
    }, ensure_ascii=False), encoding="utf-8")
    print(f"Erinnerungen per Home Assistant an: {', '.join(empfaenger) or 'niemanden (nur Protokoll)'}")
    ziel = optionen.get("kalender_schreiben") or ""
    wie = "neue automatisch" if optionen.get("kalender_automatisch", True) else "nur auf Wunsch"
    print(f"Termine eintragen in: {f'{ziel} ({wie})' if ziel else 'keinen Kalender'}")

    schluessel = optionen.get("zugriffsschluessel") or ""
    if not schluessel:
        print("Kein Zugriffsschluessel: Claude Desktop kann ontoFam nicht erreichen, "
              "nur die Seitenleiste von Home Assistant.")
    elif len(schluessel) < 20:
        print("WARNUNG: Zugriffsschluessel ist kuerzer als 20 Zeichen.")

    if not DATENBANK.exists():
        print(f"Keine Datenbank unter {DATENBANK} -- ontoFam legt eine leere an.")

    kuzu_pruefen()
    os.environ.update({
        "ONTOFAM_DB": str(DATENBANK),
        "ONTOFAM_DATEN": str(EINSTELLUNGEN),
        "ONTOFAM_HOST": "0.0.0.0",
        "ONTOFAM_PORT": "5000",
        "ONTOFAM_SCHLUESSEL": schluessel,
        "ONTOFAM_VERTRAUTE_ADRESSEN": SUPERVISOR,
        "ONTOFAM_PUFFER_MB": str(optionen["speicher_mb"]),
    })
    if optionen.get("assist", True):
        mcp_starten()
    sys.stdout.flush()
    # exec statt Unterprozess: app.py bekommt das SIGTERM von Home Assistant direkt.
    os.execv(sys.executable, [sys.executable, str(Path(__file__).parent / "app.py")])


if __name__ == "__main__":
    main()
