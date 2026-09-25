"""Beispiel-Abfragen gegen die Ontologie-Graphdatenbank."""

from pathlib import Path

import kuzu

DB_PATH = Path(__file__).resolve().parent.parent / "kuzu_db"


def run(conn: kuzu.Connection, title: str, cypher: str) -> None:
    print(f"\n=== {title} ===")
    result = conn.execute(cypher)
    while result.has_next():
        print(result.get_next())


def main() -> None:
    db = kuzu.Database(str(DB_PATH))
    conn = kuzu.Connection(db)

    run(
        conn,
        "Alle Aufgaben pro Person",
        """
        MATCH (p:Person)-[:VERANTWORTLICH_FUER]->(a:Aufgabe)
        RETURN p.name AS person, a.name AS aufgabe
        ORDER BY person
        """,
    )

    run(
        conn,
        "Nächster Müll-Termin je Tonnenart",
        """
        MATCH (r:Ressource)-[:RESSOURCE_HAT_TERMIN]->(t:Termin)
        WHERE t.datum >= current_date()
        RETURN r.name AS tonnenart, r.farbe AS farbe, min(t.datum) AS naechster_termin
        ORDER BY naechster_termin
        """,
    )

    run(
        conn,
        "Aufgaben eines Prozesses in Reihenfolge",
        """
        MATCH (a:Aufgabe)-[tv:TEIL_VON]->(pr:Prozess {id: 'pr1'})
        RETURN pr.name AS prozess, a.name AS aufgabe, tv.reihenfolge AS reihenfolge
        ORDER BY reihenfolge
        """,
    )

    run(
        conn,
        "Aufgaben nach Struktur/Raum",
        """
        MATCH (a:Aufgabe)-[:AUFGABE_AN_ORT]->(s:Struktur)
        RETURN s.name AS ort, a.name AS aufgabe
        ORDER BY ort
        """,
    )

    run(
        conn,
        "Wer ist wann für welche Tonne zuständig?",
        """
        MATCH (p:Person)-[:VERANTWORTLICH_FUER]->(a:Aufgabe)-[:BEZIEHT_SICH_AUF]->(r:Ressource)
              -[:RESSOURCE_HAT_TERMIN]->(t:Termin)
        WHERE t.datum >= current_date()
        RETURN p.name AS person, r.name AS tonnenart, t.datum AS termin
        ORDER BY termin
        """,
    )


if __name__ == "__main__":
    main()
