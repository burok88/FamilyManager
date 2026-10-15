"""Freie, nur lesende Cypher-Abfragen auf dem Graphen -- die Grundlage fuer GraphRAG.

Bisher konnte eine KI nur das fragen, wofuer es eine fertige Funktion gab
("was steht an?"). Hier bekommt sie stattdessen das Schema und darf selbst
Abfragen formulieren -- damit sind auch Fragen moeglich, an die beim Bauen
niemand gedacht hat ("wer hat die meisten Aufgaben?").

Zwei Bausteine:
    schema_als_text(conn)          -> Schema in Textform, Vorlage fuer die KI
    fuehre_lesend_aus(conn, query) -> Abfrage in einer READ-ONLY-Transaktion

Die Schreibsperre kommt von Kuzu selbst: innerhalb von "BEGIN TRANSACTION
READ ONLY" scheitert jedes CREATE/DELETE an der Datenbank. Die Stichwortpruefung
davor ist nur die zweite Verteidigungslinie -- sie liefert vor allem eine
verstaendliche Meldung statt eines rohen Datenbankfehlers.
"""

import re
from datetime import date

# Obergrenzen, damit eine unglueckliche Abfrage weder den Server blockiert
# noch das Kontextfenster der KI mit tausenden Zeilen flutet.
MAX_ZEILEN = 200
TIMEOUT_MS = 10_000

SCHREIB_WOERTER = frozenset(
    {
        "CREATE", "MERGE", "DELETE", "DETACH", "SET", "REMOVE", "DROP", "ALTER",
        "COPY", "INSTALL", "LOAD", "ATTACH", "USE", "IMPORT", "EXPORT",
        "BEGIN", "COMMIT", "ROLLBACK", "CHECKPOINT",
    }
)

# Als Funktion, damit das Datum im Beispiel nicht veraltet -- und die KI nebenbei
# erfaehrt, welcher Tag heute ist.
def _beispiele() -> str:
    return f"""\nHeute ist der {date.today().isoformat()}.

Beispielabfragen:
  Alle Personen:
    MATCH (p:Person) RETURN p.name, p.rolle
  Wer ist fuer wie viele Aufgaben zustaendig:
    MATCH (p:Person)-[:VERANTWORTLICH_FUER]->(a:Aufgabe)
    RETURN p.name, count(a) AS anzahl ORDER BY anzahl DESC
  Naechste Muelltermine mit Tonnenart und zustaendiger Person:
    MATCH (a:Aufgabe)-[:BEZIEHT_SICH_AUF]->(r:Ressource)-[:RESSOURCE_HAT_TERMIN]->(t:Termin)
    OPTIONAL MATCH (p:Person)-[:VERANTWORTLICH_FUER]->(a)
    WHERE t.datum >= date('{date.today().isoformat()}')
    RETURN t.datum, r.name, p.name ORDER BY t.datum LIMIT 10
  Aufgaben an einem bestimmten Ort:
    MATCH (a:Aufgabe)-[:AUFGABE_AN_ORT]->(s:Struktur {{name: 'Garten'}}) RETURN a.name
  Wann hat Nico dienstags Schluss (Unterricht und Betreuung getrennt):
    MATCH (p:Person {{name: 'Nico'}})-[:HAT_STUNDE]->(s:Schulstunde {{wochentag: 'Dienstag'}})
    RETURN s.stunde > 0 AS unterricht, max(s.ende) AS ende
  Was nicht in den Feldern steht (Klassenlehrer, Hinweise): im Quelldokument nachlesen:
    MATCH (d:Dokument)-[:DOKUMENT_BETRIFFT]->(p:Person {{name: 'Nico'}})
    RETURN d.titel, d.text

Hinweise:
  - Dialekt ist openCypher (Kuzu). Datumswerte als date('YYYY-MM-DD').
  - Termine haengen auf zwei Wegen an einer Aufgabe: direkt ueber
    AUFGABE_HAT_TERMIN (einmalige Termine, Kalender-Import) oder indirekt ueber
    BEZIEHT_SICH_AUF -> RESSOURCE_HAT_TERMIN (wiederkehrende Muelltermine).
    Wer "alle Termine" sucht, muss beide Wege abfragen.
  - Nur Lesen. CREATE, DELETE, SET und aehnliches werden abgewiesen.
  - Importierte Kalendertermine tragen kategorie = 'Kalender-Import'.
  - Erledigung haengt an Aufgabe UND Termin. Gespeichert ist nur status 'erledigt'
    oder 'entfaellt'. Ein Termin ohne Erledigung ist offen, nach seinem Tag verpasst --
    ausser bei Aufgaben mit rueckmeldung = false (z.B. Muell): dort gibt es kein verpasst.
    Wer hat wie viel erledigt:
    MATCH (e:Erledigung {{status: 'erledigt'}})-[:ERLEDIGT_DURCH]->(p:Person)
    RETURN p.name, count(e) AS anzahl ORDER BY anzahl DESC
  - Schulstunde: stunde = 0 heisst Betreuung o.ae. ohne Stundennummer. woche ist
    leer (jede Woche), 'A' oder 'B'. Faecher sind ausgeschrieben (Mathematik, nicht M).
  - Importierte Kalendertermine tragen kategorie = 'Kalender-Import'."""


def _ohne_text(cypher: str) -> str:
    """Kommentare und Zeichenketten entfernen, bevor nach Schreibwoertern gesucht wird.

    Beides darf die Pruefung nicht ausloesen: die Kategorie importierter Termine
    heisst 'Kalender-Import' und enthaelt damit das Wort IMPORT, und ein Kommentar
    darf ohnehin jedes Wort enthalten. Fuer die Sicherheit ist das unerheblich --
    die haengt an der READ-ONLY-Transaktion, nicht an dieser Wortliste.
    """
    ohne = re.sub(r"/\*.*?\*/", " ", cypher, flags=re.DOTALL)
    ohne = re.sub(r"//[^\n]*", " ", ohne)
    ohne = re.sub(r"'(?:[^'\\]|\\.)*'", " ", ohne)
    return re.sub(r'"(?:[^"\\]|\\.)*"', " ", ohne)


def pruefe_lesend(cypher: str) -> None:
    """Wirft ValueError, wenn die Abfrage offensichtlich schreiben will."""
    woerter = set(re.findall(r"[A-Za-z_]+", _ohne_text(cypher).upper()))
    verboten = sorted(woerter & SCHREIB_WOERTER)
    if verboten:
        raise ValueError(
            f"Nur lesende Abfragen erlaubt, gefunden: {', '.join(verboten)}. "
            "Zum Anlegen von Daten die dafuer vorgesehenen Werkzeuge benutzen."
        )


def _json_wert(wert):
    """Kuzu-Werte in etwas verwandeln, das json.dumps versteht."""
    if isinstance(wert, dict):
        # Knoten kommen als dict mit interner _id (Tabellen-Offsets) -- die ist
        # fuer eine KI nur Rauschen, das Label dagegen hilfreich.
        return {k: _json_wert(v) for k, v in wert.items() if k != "_id"}
    if isinstance(wert, (list, tuple)):
        return [_json_wert(v) for v in wert]
    if isinstance(wert, (str, int, float, bool)) or wert is None:
        return wert
    if hasattr(wert, "isoformat"):
        return wert.isoformat()
    return str(wert)


def beschreibe_schema(conn) -> dict:
    """Liest Knoten- und Kantentypen aus dem Katalog der Datenbank."""
    tabellen = conn.execute("CALL show_tables() RETURN name, type").get_as_df()

    knoten, kanten = [], []
    for _, zeile in tabellen.iterrows():
        name, typ = zeile["name"], zeile["type"]
        spalten = conn.execute(f"CALL table_info('{name}') RETURN *").get_as_df()
        eigenschaften = [
            {"name": s["name"], "typ": s["type"]} for _, s in spalten.iterrows()
        ]
        if typ == "NODE":
            knoten.append({"name": name, "eigenschaften": eigenschaften})
        else:
            verbindung = conn.execute(
                f"CALL show_connection('{name}') RETURN *"
            ).get_as_df().iloc[0]
            kanten.append(
                {
                    "name": name,
                    "von": verbindung["source table name"],
                    "nach": verbindung["destination table name"],
                    "eigenschaften": eigenschaften,
                }
            )

    knoten.sort(key=lambda k: k["name"])
    kanten.sort(key=lambda k: k["name"])
    return {"knoten": knoten, "kanten": kanten}


def schema_als_text(conn) -> str:
    """Schema als kompakter Text -- so liest es eine KI zuverlaessiger als JSON."""
    schema = beschreibe_schema(conn)

    zeilen = ["Knotentypen:"]
    for k in schema["knoten"]:
        felder = ", ".join(f"{e['name']}: {e['typ']}" for e in k["eigenschaften"])
        zeilen.append(f"  ({k['name']}) {felder}")

    zeilen.append("")
    zeilen.append("Kantentypen:")
    for k in schema["kanten"]:
        felder = ", ".join(f"{e['name']}: {e['typ']}" for e in k["eigenschaften"])
        zusatz = f" {{{felder}}}" if felder else ""
        zeilen.append(f"  ({k['von']})-[:{k['name']}{zusatz}]->({k['nach']})")

    zeilen.append("")
    zeilen.append(_beispiele())
    return "\n".join(zeilen)


def fuehre_lesend_aus(conn, cypher: str) -> dict:
    """Fuehrt eine Abfrage in einer Read-Only-Transaktion aus.

    conn sollte eine eigene Verbindung sein, nicht die der Web-Oberflaeche:
    Transaktionen gelten pro Verbindung, sonst wuerde ein offenes READ ONLY
    parallele Schreibzugriffe der Oberflaeche abweisen.
    """
    cypher = cypher.strip().rstrip(";").strip()
    if not cypher:
        raise ValueError("Leere Abfrage.")
    pruefe_lesend(cypher)

    conn.execute("BEGIN TRANSACTION READ ONLY")
    try:
        ergebnis = conn.execute(cypher)
        spalten = ergebnis.get_column_names()
        zeilen = []
        while ergebnis.has_next() and len(zeilen) < MAX_ZEILEN:
            werte = [_json_wert(w) for w in ergebnis.get_next()]
            zeilen.append(dict(zip(spalten, werte)))
        abgeschnitten = ergebnis.has_next()
    finally:
        # Immer zurueckrollen: eine offen gelassene Transaktion wuerde jede
        # weitere Abfrage auf dieser Verbindung blockieren.
        try:
            conn.execute("ROLLBACK")
        except RuntimeError:
            pass

    return {
        "spalten": spalten,
        "zeilen": zeilen,
        "anzahl": len(zeilen),
        "abgeschnitten": abgeschnitten,
    }
