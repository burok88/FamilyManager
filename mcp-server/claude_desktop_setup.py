"""Traegt den ontofam-MCP-Server in die Claude-Desktop-Konfiguration ein.

Claude Desktop schreibt claude_desktop_config.json laufend aus dem eigenen Speicher
zurueck -- Aenderungen bei laufender App gehen verloren. Deshalb:

  1. Claude Desktop komplett beenden (auch im System-Tray: Rechtsklick -> Beenden)
  2. python mcp-server/claude_desktop_setup.py
  3. Claude Desktop starten, im Chat-Tab unter "+" / "Search and tools" erscheint "ontofam"
"""

import json
import os
import shutil
import sys
from pathlib import Path

CONFIG = Path(os.environ["APPDATA"]) / "Claude" / "claude_desktop_config.json"
SERVER = Path(__file__).resolve().parent / "mcp_server.py"


def main() -> None:
    # Claude Desktop loescht die Datei beim Beenden und legt sie beim Start neu an --
    # bei geschlossener App existiert sie also oft nicht. Dann neu anlegen.
    if CONFIG.exists():
        shutil.copy(CONFIG, CONFIG.with_suffix(".json.bak"))
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
    else:
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        print("Konfiguration existierte nicht (App beendet) -- wird neu angelegt.")
    data.setdefault("mcpServers", {})["ontofam"] = {
        "command": sys.executable,
        "args": [str(SERVER)],
    }
    CONFIG.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    print("Eingetragen in", CONFIG)
    print(json.dumps(data["mcpServers"]["ontofam"], indent=2))
    print("\nJetzt Claude Desktop starten. Vorher sicherstellen, dass app.py laeuft.")


if __name__ == "__main__":
    main()
