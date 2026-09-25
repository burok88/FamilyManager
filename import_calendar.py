"""Importiert Termine aus externen Kalendern (Google Calendar per privater ICS-Adresse)
in die Ontologie: pro eindeutigem Kalender-Ereignis eine Aufgabe (kategorie
"Kalender-Import"), pro Vorkommen ein Termin-Knoten (AUFGABE_HAT_TERMIN).

Quellen stehen in calendar_sources.json (nicht in Git, siehe .example-Datei).

Read-only: schreibt nichts in den Kalender zurück.

Ein erneuter Lauf *gleicht ab*, statt alles neu anzulegen: die Aufgaben-Knoten
bleiben erhalten, solange es das Ereignis im Kalender noch gibt. Nur deshalb
überleben von Hand gesetzte Zuständigkeiten, Betroffene und Ortszuordnungen einen
Sync — die hängen an der Aufgabe, nicht am Kalender. Möglich ist das, weil die
Aufgaben-ID aus der Kalender-UID abgeleitet wird und damit über Läufe hinweg
stabil bleibt.

Vor dem Ausführen: app.py stoppen (nur ein Prozess darf gleichzeitig auf
kuzu_db zugreifen).
"""

import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import kuzu
import recurring_ical_events
import requests
from icalendar import Calendar

BASE = Path(__file__).parent
DB_PATH = BASE / "kuzu_db"
# Im Add-on liegt die Datei nicht im Programmordner, sondern bei den Einstellungen.
SOURCES_PATH = Path(os.environ.get("ONTOFAM_DATEN", BASE)) / "calendar_sources.json"

IMPORT_KATEGORIE = "Kalender-Import"
AUFGABE_PREFIX = "cal-"
TERMIN_PREFIX = "termin-cal-"
WINDOW_START = timedelta(days=-1)   # gestern, falls ein Termin heute frueh schon lief
WINDOW_END = timedelta(days=60)

# Google liefert Termine in UTC ("20260924T130000Z"). Gespeichert wird aber die
# Uhrzeit, die im Haushalt gilt -- sonst steht der Zahnarzt um 15:00 als 13:00 in
# der Ontologie. Die Zeitzone des Kalenders (X-WR-TIMEZONE) taugt dafuer nicht:
# beim Familienkalender steht dort selbst "UTC".
HAUSHALT_ZEITZONE = ZoneInfo("Europe/Berlin")

# Kanten, die ein Mensch (oder eine KI) von Hand an eine importierte Aufgabe haengt.
# Sie sind der Grund, warum der Sync abgleicht statt neu aufzubauen.
MANUELLE_KANTEN = [
    "MATCH (:Person)-[r:VERANTWORTLICH_FUER]->(a:Aufgabe {kategorie: $k}) RETURN count(r)",
    "MATCH (a:Aufgabe {kategorie: $k})-[r:BETRIFFT]->(:Person) RETURN count(r)",
    "MATCH (a:Aufgabe {kategorie: $k})-[r:AUFGABE_AN_ORT]->(:Struktur) RETURN count(r)",
    "MATCH (a:Aufgabe {kategorie: $k})-[r:TEIL_VON]->(:Prozess) RETURN count(r)",
    "MATCH (a:Aufgabe {kategorie: $k})-[r:BEZIEHT_SICH_AUF]->(:Ressource) RETURN count(r)",
]


def fetch_events(url: str) -> list[dict]:
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    return lese_ereignisse(response.content)


def massgeblicher_zeitraum(jetzt: datetime | None = None) -> tuple[date, date]:
    """Der Datumsbereich, fuer den ein Abruf vollstaendig ist.

    Nur darin darf ein fehlender Termin als geloescht gelten. Aeltere Termine fehlen im
    Abruf, weil sie vorbei sind -- sie bleiben als Verlauf stehen, samt der Angabe, ob
    sie erledigt wurden. Der Starttag selbst zaehlt nicht: dort liefert der Abruf nur
    Termine nach der Abrufuhrzeit.
    """
    jetzt = jetzt or datetime.now()
    return (jetzt + WINDOW_START).date() + timedelta(days=1), (jetzt + WINDOW_END).date()


def _ortszeit(zeitpunkt):
    """Zeitzonenbehaftete Zeitpunkte in Haushaltszeit umrechnen.

    Ohne Zeitzone ("floating time") gilt die Uhrzeit ueberall gleich und bleibt,
    wie sie ist. Ganztaegige Termine (date ohne Uhrzeit) ebenso.
    """
    if hasattr(zeitpunkt, "hour") and zeitpunkt.tzinfo is not None:
        return zeitpunkt.astimezone(HAUSHALT_ZEITZONE)
    return zeitpunkt


def lese_ereignisse(ics: bytes, jetzt: datetime | None = None) -> list[dict]:
    """ICS-Inhalt in Ereignisse zerlegen. Getrennt vom Abruf, damit es sich ohne
    Netz mit festen Beispieldaten pruefen laesst."""
    cal = Calendar.from_ical(ics)
    # recurring_ical_events wendet X-WR-TIMEZONE auf Uhrzeiten ohne Zeitzone an.
    # Beim Familienkalender steht dort "UTC" -- eine solche Uhrzeit wuerde so um
    # zwei Stunden verschoben. Ohne die Angabe gilt sie, wie im Standard
    # vorgesehen, als Ortszeit.
    cal.pop("X-WR-TIMEZONE", None)

    now = jetzt or datetime.now()
    start = now + WINDOW_START
    end = now + WINDOW_END

    events = recurring_ical_events.of(cal).between(start, end)

    result = []
    for event in events:
        uid = str(event.get("UID"))
        # Google laesst Leerzeichen am Titelende stehen ("Elternabend Nicolas ").
        summary = str(event.get("SUMMARY", "Ohne Titel")).strip()
        beschreibung = str(event.get("DESCRIPTION", "") or "")
        dtstart = event.get("DTSTART").dt
        # datetime ist eine Unterklasse von date, deckt also beides ab. Frueher
        # stand hier hasattr(dtstart, "date") -- das ist nur fuer datetime wahr, und
        # ganztaegige Termine fielen stillschweigend weg.
        if not isinstance(dtstart, date):
            continue
        # Erst umrechnen, dann Datum und Uhrzeit ablesen: ein Termin um 23:30 UTC
        # liegt bei uns schon am naechsten Tag.
        dtstart = _ortszeit(dtstart)

        # Ganztaegige Termine liefert icalendar als date (ohne .hour), terminierte
        # als datetime. Nur letztere haben Uhrzeit und Dauer.
        mit_uhrzeit = hasattr(dtstart, "hour")
        datum = dtstart.date() if mit_uhrzeit else dtstart
        beginn = dtstart.strftime("%H:%M") if mit_uhrzeit else ""

        dauer = 0
        dtend = event.get("DTEND")
        if mit_uhrzeit and dtend is not None and hasattr(dtend.dt, "hour"):
            dauer = max(0, int((_ortszeit(dtend.dt) - dtstart).total_seconds() // 60))

        result.append({
            "uid": uid,
            "summary": summary,
            "beschreibung": beschreibung,
            "datum": datum,
            "beginn": beginn,
            "dauer_minuten": dauer,
            "ort": str(event.get("LOCATION", "") or "").strip(),
        })
    return result


def clear_previous_import(conn: kuzu.Connection) -> None:
    # Erst die Termine der importierten Aufgaben loeschen, dann die Aufgaben:
    # DETACH DELETE entfernt nur Kanten, sonst blieben die Termin-Knoten als
    # Waisen zurueck und kollidierten beim naechsten Import mit ihrer festen ID.
    conn.execute(
        f"""
        MATCH (a:Aufgabe {{kategorie: '{IMPORT_KATEGORIE}'}})-[:AUFGABE_HAT_TERMIN]->(t:Termin)
        DETACH DELETE t
        """
    )
    conn.execute(
        f"MATCH (a:Aufgabe {{kategorie: '{IMPORT_KATEGORIE}'}}) DETACH DELETE a"
    )
    entferne_verwaiste_termine(conn)


def entferne_verwaiste_erledigungen(conn: kuzu.Connection) -> int:
    """Eine Erledigung ohne Termin oder ohne Aufgabe beschreibt nichts mehr."""
    anzahl = conn.execute(
        """MATCH (e:Erledigung)
           WHERE NOT EXISTS { MATCH (e)-[:ERLEDIGUNG_ZU_TERMIN]->(:Termin) }
              OR NOT EXISTS { MATCH (e)-[:ERLEDIGUNG_ZU_AUFGABE]->(:Aufgabe) }
           RETURN count(e)"""
    ).get_next()[0]
    if anzahl:
        conn.execute(
            """MATCH (e:Erledigung)
               WHERE NOT EXISTS { MATCH (e)-[:ERLEDIGUNG_ZU_TERMIN]->(:Termin) }
                  OR NOT EXISTS { MATCH (e)-[:ERLEDIGUNG_ZU_AUFGABE]->(:Aufgabe) }
               DETACH DELETE e"""
        )
    return anzahl


def entferne_verwaiste_termine(conn: kuzu.Connection) -> int:
    """Termine ohne jede eingehende Kante sind im Modell bedeutungslos."""
    alle = {row[0] for row in _rows(conn, "MATCH (t:Termin) RETURN t.id")}
    verbunden = {row[0] for row in _rows(conn, "MATCH ()-[]->(t:Termin) RETURN DISTINCT t.id")}
    verwaist = alle - verbunden
    for tid in verwaist:
        conn.execute("MATCH (t:Termin {id: $id}) DELETE t", {"id": tid})
    return len(verwaist)


def _rows(conn: kuzu.Connection, cypher: str) -> list:
    result = conn.execute(cypher)
    rows = []
    while result.has_next():
        rows.append(result.get_next())
    return rows


def _aufgabe_id(uid: str) -> str:
    """Stabil ueber Laeufe hinweg -- das ist die Voraussetzung dafuer, dass beim
    Sync dieselbe Aufgabe wiedererkannt statt neu angelegt wird."""
    return f"{AUFGABE_PREFIX}{uid}"[:120]


def _gruppiere(quelle_name: str, events: list[dict]) -> dict[str, dict]:
    """Pro Kalender-UID eine Aufgabe, darunter alle Vorkommen als Termine."""
    nach_id: dict[str, dict] = {}
    for ev in events:
        eintrag = nach_id.setdefault(
            _aufgabe_id(ev["uid"]),
            {
                "name": ev["summary"],
                "beschreibung": f"[{quelle_name}] {ev['beschreibung']}".strip(),
                "termine": [],
            },
        )
        eintrag["termine"].append(ev)
    return nach_id


def _im_zeitraum(datum, zeitraum: tuple[date, date] | None) -> bool:
    return zeitraum is None or zeitraum[0] <= datum <= zeitraum[1]


def _gleiche_termine_ab(conn: kuzu.Connection, aufgabe_id: str, vorkommen: list[dict],
                        zeitraum: tuple[date, date] | None) -> int:
    """Termine der Aufgabe an den Kalender angleichen, ohne bestehende anzufassen.

    Bestehende Termin-Knoten bleiben stehen, denn an ihnen haengt, ob der Termin
    erledigt wurde. Frueher wurden sie bei jedem Sync geloescht und neu angelegt --
    das haette jede Erledigung nach spaetestens 15 Minuten verworfen.

    Geloescht wird nur, was im massgeblichen Zeitraum liegt und im Abruf fehlt, und
    nur selbst importierte Termine (ID-Praefix): ein von Hand an dieselbe Aufgabe
    gehaengter Termin bleibt.
    """
    # Uhrzeit gehoert in die ID: derselbe Termin kann an einem Tag mehrfach vorkommen
    # (z.B. zwei Arzttermine), sonst kollidieren die IDs.
    gewuenscht = {f"termin-{aufgabe_id}-{ev['datum'].isoformat()}-{ev['beginn']}": ev
                  for ev in vorkommen}
    vorhanden = {}
    ergebnis = conn.execute(
        """MATCH (a:Aufgabe {id: $aid})-[:AUFGABE_HAT_TERMIN]->(t:Termin)
           WHERE t.id STARTS WITH $prefix RETURN t.id, t.datum""",
        {"aid": aufgabe_id, "prefix": TERMIN_PREFIX},
    )
    while ergebnis.has_next():
        termin_id, datum = ergebnis.get_next()
        vorhanden[termin_id] = datum

    for termin_id, datum in vorhanden.items():
        if termin_id not in gewuenscht and _im_zeitraum(datum, zeitraum):
            conn.execute("MATCH (t:Termin {id: $id}) DETACH DELETE t", {"id": termin_id})

    for termin_id, ev in gewuenscht.items():
        if termin_id in vorhanden:
            # Datum und Uhrzeit stecken in der ID; nachziehen koennen sich nur Ort und Dauer.
            conn.execute(
                "MATCH (t:Termin {id: $id}) SET t.ort = $ort, t.dauer_minuten = $dauer",
                {"id": termin_id, "ort": ev["ort"], "dauer": ev["dauer_minuten"]},
            )
            continue
        conn.execute(
            """CREATE (:Termin {id: $id, datum: date($datum), ort: $ort,
                                beginn: $beginn, dauer_minuten: $dauer})""",
            {"id": termin_id, "datum": ev["datum"].isoformat(), "ort": ev["ort"],
             "beginn": ev["beginn"], "dauer": ev["dauer_minuten"]},
        )
        conn.execute(
            """
            MATCH (a:Aufgabe {id: $aid}), (t:Termin {id: $tid})
            CREATE (a)-[:AUFGABE_HAT_TERMIN]->(t)
            """,
            {"aid": aufgabe_id, "tid": termin_id},
        )
    return len(gewuenscht)


def personen_im_titel(titel: str, personen: list[tuple[str, str]]) -> list[str]:
    """IDs der Personen, deren Name im Titel steht -- am Wortanfang, damit die
    Langform mitzaehlt ("Nicolas" fuer "Nico", "Lenas" fuer "Lena"), ein zufaelliges
    Vorkommen mitten im Wort aber nicht."""
    return [
        pid for pid, name in personen
        if re.search(rf"\b{re.escape(name.strip())}", titel, re.IGNORECASE)
    ]


def _verbinde_betroffene(conn: kuzu.Connection, aufgabe_id: str, titel: str,
                         personen: list[tuple[str, str]]) -> int:
    """BETRIFFT fuer jede im Titel genannte Person setzen, falls sie noch fehlt.

    Ohne diese Kante findet "was steht fuer Lena an?" ihren Zahnarzttermin nicht,
    wenn jemand anderes ihn erledigt. Es wird nur ergaenzt, nie entfernt -- eine
    von Hand gesetzte Betroffene bleibt, auch wenn ihr Name nicht im Titel steht.
    """
    neu = 0
    for pid in personen_im_titel(titel, personen):
        vorhanden = conn.execute(
            "MATCH (a:Aufgabe {id: $aid})-[:BETRIFFT]->(p:Person {id: $pid}) RETURN count(*)",
            {"aid": aufgabe_id, "pid": pid},
        ).get_next()[0]
        if not vorhanden:
            conn.execute(
                """MATCH (a:Aufgabe {id: $aid}), (p:Person {id: $pid})
                   CREATE (a)-[:BETRIFFT]->(p)""",
                {"aid": aufgabe_id, "pid": pid},
            )
            neu += 1
    return neu


def _zaehle_manuelle_kanten(conn: kuzu.Connection) -> int:
    gesamt = 0
    for cypher in MANUELLE_KANTEN:
        ergebnis = conn.execute(cypher, {"k": IMPORT_KATEGORIE})
        gesamt += ergebnis.get_next()[0] if ergebnis.has_next() else 0
    return gesamt


def synchronisiere(conn: kuzu.Connection, quellen: list[tuple[str, list[dict]]],
                   zeitraum: tuple[date, date] | None = None) -> dict:
    """Gleicht die importierten Aufgaben mit dem Stand der Kalender ab.

    Nimmt bewusst *alle* Quellen auf einmal: was im Kalender nicht mehr vorkommt,
    wird geloescht, und diese Entscheidung laesst sich nur treffen, wenn alle
    Quellen bekannt sind -- sonst wuerde die erste Quelle die Aufgaben der zweiten
    fuer entfallen halten.

    zeitraum: der Datumsbereich, fuer den der Abruf vollstaendig ist (siehe
    massgeblicher_zeitraum). Termine ausserhalb bleiben als Verlauf stehen. None heisst:
    der Abruf ist vollstaendig, alles Fehlende gilt als geloescht.
    """
    gewuenscht: dict[str, dict] = {}
    for quelle_name, events in quellen:
        gewuenscht.update(_gruppiere(quelle_name, events))

    vorhanden = {
        zeile[0]: (zeile[1], zeile[2])
        for zeile in _rows(
            conn,
            f"MATCH (a:Aufgabe {{kategorie: '{IMPORT_KATEGORIE}'}}) "
            "RETURN a.id, a.name, a.beschreibung",
        )
    }

    personen = [(z[0], z[1]) for z in _rows(conn, "MATCH (p:Person) RETURN p.id, p.name")]

    entfernt = 0
    for aufgabe_id in set(vorhanden) - set(gewuenscht):
        # Nur die Termine im massgeblichen Zeitraum sind wirklich weg. Hat die Aufgabe
        # danach noch aeltere Termine, bleibt sie als Verlauf stehen.
        _gleiche_termine_ab(conn, aufgabe_id, [], zeitraum)
        rest = conn.execute(
            "MATCH (a:Aufgabe {id: $id})-[:AUFGABE_HAT_TERMIN]->(t:Termin) RETURN count(t)",
            {"id": aufgabe_id},
        ).get_next()[0]
        if not rest:
            conn.execute("MATCH (a:Aufgabe {id: $id}) DETACH DELETE a", {"id": aufgabe_id})
            entfernt += 1

    neu = aktualisiert = unveraendert = termine = betroffene = 0
    for aufgabe_id, info in gewuenscht.items():
        if aufgabe_id not in vorhanden:
            conn.execute(
                """CREATE (:Aufgabe {id: $id, name: $name, beschreibung: $beschreibung,
                                     kategorie: $kategorie})""",
                {"id": aufgabe_id, "name": info["name"],
                 "beschreibung": info["beschreibung"], "kategorie": IMPORT_KATEGORIE},
            )
            neu += 1
        elif vorhanden[aufgabe_id] != (info["name"], info["beschreibung"]):
            # Nur Titel und Beschreibung nachziehen. Die Kanten der Aufgabe bleiben,
            # genau das ist der Sinn des Abgleichs.
            conn.execute(
                "MATCH (a:Aufgabe {id: $id}) SET a.name = $name, a.beschreibung = $beschreibung",
                {"id": aufgabe_id, "name": info["name"], "beschreibung": info["beschreibung"]},
            )
            aktualisiert += 1
        else:
            unveraendert += 1
        termine += _gleiche_termine_ab(conn, aufgabe_id, info["termine"], zeitraum)
        betroffene += _verbinde_betroffene(conn, aufgabe_id, info["name"], personen)

    verwaist = entferne_verwaiste_termine(conn)
    entferne_verwaiste_erledigungen(conn)
    return {
        "aufgaben_neu": neu,
        "aufgaben_aktualisiert": aktualisiert,
        "aufgaben_unveraendert": unveraendert,
        "aufgaben_entfernt": entfernt,
        "termine": termine,
        "betroffene_neu_erkannt": betroffene,
        "verwaiste_termine_entfernt": verwaist,
        "manuelle_verknuepfungen_erhalten": _zaehle_manuelle_kanten(conn),
    }


def main() -> None:
    if not SOURCES_PATH.exists():
        print(f"Keine {SOURCES_PATH.name} gefunden. Siehe calendar_sources.example.json.")
        return

    sources = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))

    db = kuzu.Database(str(DB_PATH))
    conn = kuzu.Connection(db)

    quellen = []
    for source in sources:
        events = fetch_events(source["url"])
        print(f"{source['name']}: {len(events)} Vorkommen gelesen")
        quellen.append((source["name"], events))

    for schluessel, wert in synchronisiere(conn, quellen, massgeblicher_zeitraum()).items():
        print(f"  {schluessel}: {wert}")


if __name__ == "__main__":
    main()
