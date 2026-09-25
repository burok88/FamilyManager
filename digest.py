"""Erzeugt pro Person einen 'Digest' anstehender Aufgaben innerhalb ihrer individuellen
Vorlaufzeit. Das Ergebnis ist die Schnittstelle für eine KI (oder Home Assistant), die
Personen proaktiv an anstehende Aufgaben/Termine erinnern soll.
"""

import json
from datetime import date
from pathlib import Path

import kuzu

BASE = Path(__file__).parent
DB_PATH = BASE / "kuzu_db"
EXPORT_PATH = BASE / "export" / "digest.json"

# Cypher liefert alle Person-Aufgabe-Termin-Kombinationen (über Ressource ODER direkt an
# der Aufgabe) zusammen mit der individuellen Vorlaufzeit. Das Filtern auf das jeweilige
# Vorlauf-Fenster passiert bewusst in Python, nicht in Cypher, da jede Person eine
# unterschiedliche Vorlaufzeit hat.
QUERY = """
MATCH (p:Person)-[v:VERANTWORTLICH_FUER]->(a:Aufgabe)
OPTIONAL MATCH (a)-[:BEZIEHT_SICH_AUF]->(r:Ressource)-[:RESSOURCE_HAT_TERMIN]->(t1:Termin)
OPTIONAL MATCH (a)-[:AUFGABE_HAT_TERMIN]->(t2:Termin)
WITH p, a, r, v.vorlaufzeit_tage AS vorlauf, coalesce(t1.datum, t2.datum) AS termin_datum
WHERE termin_datum IS NOT NULL
RETURN p.name AS person, a.name AS aufgabe, r.name AS kontext,
       termin_datum AS datum, vorlauf AS vorlaufzeit_tage
"""


def build_digest(conn: kuzu.Connection, heute: date) -> dict:
    result = conn.execute(QUERY)
    digest: dict[str, list[dict]] = {}

    while result.has_next():
        person, aufgabe, kontext, termin_datum, vorlauf = result.get_next()
        tage_bis = (termin_datum - heute).days

        if 0 <= tage_bis <= vorlauf:
            eintrag = {
                "aufgabe": aufgabe,
                "kontext": kontext,
                "datum": termin_datum.isoformat(),
                "tage_bis": tage_bis,
            }
            digest.setdefault(person, []).append(eintrag)

    for eintraege in digest.values():
        eintraege.sort(key=lambda e: e["datum"])

    return {
        "generiert_am": heute.isoformat(),
        "personen": [
            {"person": person, "anstehend": eintraege}
            for person, eintraege in sorted(digest.items())
        ],
    }


def main() -> None:
    db = kuzu.Database(str(DB_PATH))
    conn = kuzu.Connection(db)

    heute = date.today()
    digest = build_digest(conn, heute)

    EXPORT_PATH.parent.mkdir(exist_ok=True)
    EXPORT_PATH.write_text(json.dumps(digest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(digest, ensure_ascii=False, indent=2))
    print(f"\nDigest geschrieben nach: {EXPORT_PATH}")


if __name__ == "__main__":
    main()
