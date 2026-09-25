"""Legt eine LEERE Datenbank mit dem aktuellen Schema an -- loescht dabei alles.

Fuer den Normalfall ist das hier nicht noetig: app.py fuehrt beim Start die
Migration aus schema.py aus, neue Tabellen und Felder kommen also ohne Datenverlust
dazu. Dieses Skript ist nur fuer den bewussten Neuanfang gedacht.
"""

from pathlib import Path

import kuzu

from schema import migriere

DB_PATH = Path(__file__).parent / "kuzu_db"


def main() -> None:
    # Kuzu legt die Datenbank als einzelne Datei an (plus Write-Ahead-Log),
    # nicht als Verzeichnis.
    for pfad in (DB_PATH, DB_PATH.with_name(DB_PATH.name + ".wal")):
        pfad.unlink(missing_ok=True)

    db = kuzu.Database(str(DB_PATH))
    conn = kuzu.Connection(db)
    angelegt = migriere(conn)

    print(f"Schema angelegt in: {DB_PATH}")
    print(f"{len(angelegt)} Tabellen und Felder erzeugt.")


if __name__ == "__main__":
    main()
