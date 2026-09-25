"""Lädt Beispieldaten (Haushalt) und die realen Müllkalender-Quelldaten in die Kùzu-Graphdatenbank."""

import csv
import json
from pathlib import Path

import kuzu

BASE = Path(__file__).resolve().parent.parent  # Projektordner
DB_PATH = BASE / "kuzu_db"
DATA = BASE / "data"


def load_haushalt(conn: kuzu.Connection) -> None:
    # Personen
    personen = [
        ("p1", "Anna", "Elternteil"),
        ("p2", "Ben", "Elternteil"),
        ("p3", "Cara", "Kind"),
    ]
    for pid, name, rolle in personen:
        conn.execute(
            "CREATE (:Person {id: $id, name: $name, rolle: $rolle})",
            {"id": pid, "name": name, "rolle": rolle},
        )

    # Struktur (Haushalt + Räume)
    struktur = [
        ("s1", "Musterfamilie Zuhause", "Haushalt"),
        ("s2", "Küche", "Zimmer"),
        ("s3", "Garten", "Zimmer"),
    ]
    for sid, name, typ in struktur:
        conn.execute(
            "CREATE (:Struktur {id: $id, name: $name, typ: $typ})",
            {"id": sid, "name": name, "typ": typ},
        )

    # Personen gehören zum Haushalt
    for pid in ("p1", "p2", "p3"):
        conn.execute(
            """
            MATCH (p:Person {id: $pid}), (s:Struktur {id: 's1'})
            CREATE (p)-[:GEHOERT_ZU]->(s)
            """,
            {"pid": pid},
        )

    # Aufgaben
    aufgaben = [
        ("a1", "Müll rausbringen", "Tonnen an den Straßenrand stellen", "Haushalt"),
        ("a2", "Küche putzen", "Arbeitsflächen und Boden wischen", "Haushalt"),
        ("a3", "Rasen mähen", "Rasen im Garten mähen", "Garten"),
    ]
    for aid, name, beschreibung, kategorie in aufgaben:
        conn.execute(
            """
            CREATE (:Aufgabe {id: $id, name: $name, beschreibung: $beschreibung,
                               kategorie: $kategorie})
            """,
            {"id": aid, "name": name, "beschreibung": beschreibung, "kategorie": kategorie},
        )

    # Aufgabe -> Struktur (Ort)
    conn.execute("""
        MATCH (a:Aufgabe {id: 'a2'}), (s:Struktur {id: 's2'})
        CREATE (a)-[:AUFGABE_AN_ORT]->(s)
    """)
    conn.execute("""
        MATCH (a:Aufgabe {id: 'a3'}), (s:Struktur {id: 's3'})
        CREATE (a)-[:AUFGABE_AN_ORT]->(s)
    """)

    # Person -> Aufgabe (mit individueller Vorlaufzeit in Tagen)
    zustaendigkeiten = [
        ("p2", "a1", 1),  # Ben, Müll rausbringen, 1 Tag vorher erinnern
        ("p1", "a2", 0),  # Anna, Küche putzen, am selben Tag
        ("p3", "a3", 2),  # Cara, Rasen mähen, 2 Tage vorher
    ]
    for pid, aid, vorlauf in zustaendigkeiten:
        conn.execute(
            """
            MATCH (p:Person {id: $pid}), (a:Aufgabe {id: $aid})
            CREATE (p)-[:VERANTWORTLICH_FUER {vorlaufzeit_tage: $vorlauf}]->(a)
            """,
            {"pid": pid, "aid": aid, "vorlauf": vorlauf},
        )

    # Prozess: Wochenroutine, die "Küche putzen" und "Rasen mähen" bündelt
    conn.execute("""
        CREATE (:Prozess {id: 'pr1', name: 'Wochenroutine',
                           beschreibung: 'Wiederkehrende wöchentliche Hausarbeiten'})
    """)
    conn.execute("""
        MATCH (a:Aufgabe {id: 'a2'}), (pr:Prozess {id: 'pr1'})
        CREATE (a)-[:TEIL_VON {reihenfolge: 1}]->(pr)
    """)
    conn.execute("""
        MATCH (a:Aufgabe {id: 'a3'}), (pr:Prozess {id: 'pr1'})
        CREATE (a)-[:TEIL_VON {reihenfolge: 2}]->(pr)
    """)


def load_muellkalender(conn: kuzu.Connection) -> None:
    # Ressourcen (Tonnenarten) aus der Legende
    with open(DATA / "tonnenarten.json", encoding="utf-8") as f:
        tonnenarten = json.load(f)

    for t in tonnenarten:
        conn.execute(
            """
            CREATE (:Ressource {id: $code, name: $name, farbe: $farbe,
                                 kalender_code: $buchstabe})
            """,
            {
                "code": t["code"],
                "name": t["name"],
                "farbe": t["farbe"],
                "buchstabe": t["kalender_buchstabe"],
            },
        )

    # Aufgabe "Müll rausbringen" bezieht sich auf jede Tonnenart
    for t in tonnenarten:
        conn.execute(
            """
            MATCH (a:Aufgabe {id: 'a1'}), (r:Ressource {id: $code})
            CREATE (a)-[:BEZIEHT_SICH_AUF]->(r)
            """,
            {"code": t["code"]},
        )

    # Konkrete Abholtermine aus dem Kalender (CSV)
    with open(DATA / "muellkalender.csv", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            termin_id = f"termin-{row['code']}-{row['datum']}"
            conn.execute(
                "CREATE (:Termin {id: $id, datum: date($datum)})",
                {"id": termin_id, "datum": row["datum"]},
            )
            conn.execute(
                """
                MATCH (r:Ressource {id: $code}), (t:Termin {id: $tid})
                CREATE (r)-[:RESSOURCE_HAT_TERMIN]->(t)
                """,
                {"code": row["code"], "tid": termin_id},
            )


def main() -> None:
    db = kuzu.Database(str(DB_PATH))
    conn = kuzu.Connection(db)

    load_haushalt(conn)
    load_muellkalender(conn)

    print("Beispieldaten und Müllkalender-Quelldaten geladen.")


if __name__ == "__main__":
    main()
