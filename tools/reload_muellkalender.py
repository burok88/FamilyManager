"""Gleicht die Muelltermine mit data/muellkalender.csv ab.

Fasst nur Termine an, die an einer Tonne (Ressource) haengen. Frueher hat das Skript
*alle* Termine geloescht -- auch einmalige Termine und importierte Kalendertermine.

Bestehende Muelltermine bleiben stehen, damit festgehaltene Erledigungen erhalten
bleiben. Neu angelegt wird, was fehlt; entfernt wird nur, was im Zeitraum der CSV
liegt und dort nicht mehr steht. Eine CSV nur fuer 2027 laesst 2026 also als Verlauf
stehen.

Greift direkt auf die Datenbank zu: vorher app.py beenden.
"""

import csv
import sys
from datetime import date
from pathlib import Path

import kuzu

BASE = Path(__file__).resolve().parent.parent  # Projektordner
sys.path.insert(0, str(BASE / "server"))
from import_calendar import entferne_verwaiste_erledigungen  # noqa: E402

DB_PATH = BASE / "kuzu_db"
CSV_PATH = BASE / "data" / "muellkalender.csv"


def main() -> None:
    conn = kuzu.Connection(kuzu.Database(str(DB_PATH)))

    soll: dict[str, tuple[str, str]] = {}
    with open(CSV_PATH, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            soll[f"termin-{row['code']}-{row['datum']}"] = (row["code"], row["datum"])
    daten = [date.fromisoformat(datum) for _, datum in soll.values()]
    von, bis = min(daten), max(daten)

    ist: dict[str, date] = {}
    ergebnis = conn.execute("MATCH (:Ressource)-[:RESSOURCE_HAT_TERMIN]->(t:Termin) RETURN t.id, t.datum")
    while ergebnis.has_next():
        termin_id, datum = ergebnis.get_next()
        ist[termin_id] = datum

    entfernt = [tid for tid, datum in ist.items() if tid not in soll and von <= datum <= bis]
    for termin_id in entfernt:
        conn.execute("MATCH (t:Termin {id: $id}) DETACH DELETE t", {"id": termin_id})

    neu = [tid for tid in soll if tid not in ist]
    for termin_id in neu:
        code, datum = soll[termin_id]
        conn.execute("CREATE (:Termin {id: $id, datum: date($datum)})", {"id": termin_id, "datum": datum})
        conn.execute(
            """
            MATCH (r:Ressource {id: $code}), (t:Termin {id: $tid})
            CREATE (r)-[:RESSOURCE_HAT_TERMIN]->(t)
            """,
            {"code": code, "tid": termin_id},
        )

    entferne_verwaiste_erledigungen(conn)
    print(f"CSV {von} bis {bis}: {len(neu)} neu, {len(entfernt)} entfernt, "
          f"{len(soll) - len(neu)} unveraendert.")


if __name__ == "__main__":
    main()
