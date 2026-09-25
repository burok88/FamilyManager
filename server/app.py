"""Web-App und HTTP-API der Haushalts-Ontologie -- der einzige Prozess, der die
Kùzu-Graphdatenbank oeffnet.

Auf dem PC: `python server/app.py`, dann http://localhost:5000 (nur vom eigenen Rechner
erreichbar). Als Home-Assistant-Add-on steuern Umgebungsvariablen Pfade, Adresse und
Zugriffsschutz (siehe ha-app/start.py).
"""

import hmac
import json
import os
import signal
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import kuzu
import requests
from flask import Flask, g, jsonify, request, send_from_directory

from export_graph import export_edges, export_nodes
from graph_query import TIMEOUT_MS, beschreibe_schema, fuehre_lesend_aus, schema_als_text
from import_calendar import (
    SOURCES_PATH,
    clear_previous_import,
    entferne_verwaiste_erledigungen,
    fetch_events,
    massgeblicher_zeitraum,
    synchronisiere,
)
from erinnerungen import faellige, kanal_fuer, lade_einstellungen, nachricht, plane, sende
from schema import ERLEDIGUNG_STATUS, WOCHEN, WOCHENTAGE, migriere, wochentag_nummer

# Projektordner (eine Ebene ueber server/): dort liegen Datenbank und Einstellungen,
# ausserhalb von Git. Im Container setzt start.py die Pfade per Umgebungsvariable.
PROJEKT = Path(__file__).resolve().parent.parent
# Ueberschreibbar, damit sich die App gegen eine Kopie der Datenbank testen laesst --
# und damit die Datenbank im Add-on in einem Ordner liegt, der Updates ueberlebt.
DB_PATH = Path(os.environ.get("ONTOFAM_DB", PROJEKT / "kuzu_db"))
# Kuzu reserviert sich ohne Grenze bis zu 80 % des Arbeitsspeichers als Puffer. Auf dem
# Home-Assistant-Rechner teilt es sich den Speicher mit Home Assistant. 0 = Kuzu-Standard.
PUFFER_MB = int(os.environ.get("ONTOFAM_PUFFER_MB", 0))

# Zugriffsschutz. Auf dem PC hoert der Server nur auf localhost, jede Anfrage kommt also
# vom eigenen Rechner. Im Heimnetz (Add-on) braucht jede Anfrage den Zugriffsschluessel
# -- ausser sie kommt ueber die Seitenleiste von Home Assistant, die ihre Nutzer selbst
# anmeldet und von einer festen Adresse aus weiterreicht.
SCHLUESSEL = os.environ.get("ONTOFAM_SCHLUESSEL", "")
VERTRAUTE_ADRESSEN = {"127.0.0.1", "::1",
                      *filter(None, os.environ.get("ONTOFAM_VERTRAUTE_ADRESSEN", "").split(","))}

# Kalender-Sync: bei einer Abfrage, wenn der letzte Sync laenger zurueckliegt als
# SYNC_BEI_ABFRAGE, und im Hintergrund spaetestens nach SYNC_INTERVALL.
SYNC_BEI_ABFRAGE = timedelta(seconds=float(os.environ.get("ONTOFAM_SYNC_ABFRAGE_SEKUNDEN", 60)))
SYNC_INTERVALL = timedelta(minutes=float(os.environ.get("ONTOFAM_SYNC_INTERVALL_MINUTEN", 15)))

# Seit wann Erledigungen festgehalten werden. Davor konnte es niemand -- ein Termin ohne
# Erledigung ist dort "unbekannt", nicht "verpasst". Sonst stuende beim ersten
# Rueckblick die ganze Vergangenheit als verpasst da.
ERFASSUNG_SEIT = date.fromisoformat(os.environ.get("ONTOFAM_ERFASSUNG_SEIT", "2026-09-24"))

app = Flask(__name__, static_folder="static")


# Als erster before_request-Haken registriert: ohne Schluessel kein Kalender-Sync und
# keine Schreibsperre.
@app.before_request
def _zugriff_pruefen():
    if request.remote_addr in VERTRAUTE_ADRESSEN:
        return None
    angegeben = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if SCHLUESSEL and hmac.compare_digest(angegeben.encode(), SCHLUESSEL.encode()):
        return None
    return jsonify({"error": "Zugriffsschluessel fehlt oder ist falsch."}), 401


_db = kuzu.Database(str(DB_PATH), buffer_pool_size=PUFFER_MB * 1024 * 1024)
_conn = kuzu.Connection(_db)

# Schema bei jedem Start angleichen. Die Anweisungen in schema.py sind idempotent,
# neue Tabellen und Felder kommen also im laufenden Betrieb dazu, ohne dass jemand
# die Datenbank neu aufbauen muss.
for _neu in migriere(_conn):
    print(f"Schema erweitert: {_neu}")

# Eigene Verbindung für die freien Abfragen: die laufen in einer READ-ONLY-
# Transaktion, und Transaktionen gelten pro Verbindung. Auf _conn würde eine
# offene Lese-Transaktion die Schreibzugriffe der Oberfläche abweisen.
_read_conn = kuzu.Connection(_db)
_read_conn.set_query_timeout(TIMEOUT_MS)
_read_lock = threading.Lock()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "modell.html")


@app.get("/api/graph")
def get_graph():
    return jsonify({"nodes": export_nodes(_conn), "edges": export_edges(_conn)})


@app.get("/api/personen")
def list_personen():
    result = _conn.execute("MATCH (p:Person) RETURN p.id, p.name, p.rolle ORDER BY p.name")
    personen = []
    while result.has_next():
        pid, name, rolle = result.get_next()
        personen.append({"id": pid, "name": name, "rolle": rolle})
    return jsonify(personen)


@app.post("/api/personen")
def create_person():
    body = request.get_json(force=True)
    name = (body.get("name") or "").strip()
    rolle = (body.get("rolle") or "").strip()

    if not name:
        return jsonify({"error": "Name darf nicht leer sein."}), 400

    pid = new_id("p")
    _conn.execute(
        "CREATE (:Person {id: $id, name: $name, rolle: $rolle})",
        {"id": pid, "name": name, "rolle": rolle},
    )

    # Neue Person automatisch dem Haushalt zuordnen, falls genau ein
    # Struktur-Knoten vom Typ "Haushalt" existiert.
    haushalt = _conn.execute(
        "MATCH (s:Struktur {typ: 'Haushalt'}) RETURN s.id"
    )
    if haushalt.has_next():
        haushalt_id = haushalt.get_next()[0]
        _conn.execute(
            """
            MATCH (p:Person {id: $pid}), (s:Struktur {id: $sid})
            CREATE (p)-[:GEHOERT_ZU]->(s)
            """,
            {"pid": pid, "sid": haushalt_id},
        )

    return jsonify({"id": pid, "name": name, "rolle": rolle}), 201


@app.delete("/api/personen/<person_id>")
def delete_person(person_id: str):
    # Schulstunden gehoeren der Person -- ein DETACH DELETE auf die Person allein
    # wuerde sie als Waisen zuruecklassen.
    _entferne_stundenplan(person_id)
    _conn.execute("MATCH (p:Person {id: $id}) DETACH DELETE p", {"id": person_id})
    return "", 204


# --- Struktur, Prozess, Ressource: gleiches Muster (Name + ein Zusatzfeld) ---


def _list_simple(label: str, extra_field: str) -> list[dict]:
    result = _conn.execute(f"MATCH (n:{label}) RETURN n.id, n.name, n.{extra_field} ORDER BY n.name")
    items = []
    while result.has_next():
        nid, name, extra = result.get_next()
        items.append({"id": nid, "name": name, extra_field: extra})
    return items


@app.get("/api/struktur")
def list_struktur():
    return jsonify(_list_simple("Struktur", "typ"))


@app.post("/api/struktur")
def create_struktur():
    body = request.get_json(force=True)
    name = (body.get("name") or "").strip()
    typ = (body.get("typ") or "").strip()
    if not name:
        return jsonify({"error": "Name darf nicht leer sein."}), 400

    sid = new_id("s")
    _conn.execute(
        "CREATE (:Struktur {id: $id, name: $name, typ: $typ})",
        {"id": sid, "name": name, "typ": typ},
    )
    return jsonify({"id": sid, "name": name, "typ": typ}), 201


@app.delete("/api/struktur/<struktur_id>")
def delete_struktur(struktur_id: str):
    _conn.execute("MATCH (s:Struktur {id: $id}) DETACH DELETE s", {"id": struktur_id})
    return "", 204


@app.get("/api/prozesse")
def list_prozesse():
    return jsonify(_list_simple("Prozess", "beschreibung"))


@app.post("/api/prozesse")
def create_prozess():
    body = request.get_json(force=True)
    name = (body.get("name") or "").strip()
    beschreibung = (body.get("beschreibung") or "").strip()
    if not name:
        return jsonify({"error": "Name darf nicht leer sein."}), 400

    prid = new_id("pr")
    _conn.execute(
        "CREATE (:Prozess {id: $id, name: $name, beschreibung: $beschreibung})",
        {"id": prid, "name": name, "beschreibung": beschreibung},
    )
    return jsonify({"id": prid, "name": name, "beschreibung": beschreibung}), 201


@app.delete("/api/prozesse/<prozess_id>")
def delete_prozess(prozess_id: str):
    _conn.execute("MATCH (pr:Prozess {id: $id}) DETACH DELETE pr", {"id": prozess_id})
    return "", 204


@app.get("/api/ressourcen")
def list_ressourcen():
    result = _conn.execute(
        "MATCH (r:Ressource) RETURN r.id, r.name, r.farbe, r.kalender_code ORDER BY r.name"
    )
    items = []
    while result.has_next():
        rid, name, farbe, code = result.get_next()
        items.append({"id": rid, "name": name, "farbe": farbe, "kalender_code": code})
    return jsonify(items)


@app.post("/api/ressourcen")
def create_ressource():
    body = request.get_json(force=True)
    name = (body.get("name") or "").strip()
    farbe = (body.get("farbe") or "").strip()
    kalender_code = (body.get("kalender_code") or "").strip()
    if not name:
        return jsonify({"error": "Name darf nicht leer sein."}), 400

    rid = new_id("res")
    _conn.execute(
        "CREATE (:Ressource {id: $id, name: $name, farbe: $farbe, kalender_code: $code})",
        {"id": rid, "name": name, "farbe": farbe, "code": kalender_code},
    )
    return jsonify({"id": rid, "name": name, "farbe": farbe, "kalender_code": kalender_code}), 201


@app.delete("/api/ressourcen/<ressource_id>")
def delete_ressource(ressource_id: str):
    _conn.execute("MATCH (r:Ressource {id: $id}) DETACH DELETE r", {"id": ressource_id})
    return "", 204


# --- Aufgabe: kann direkt bei Anlage mit Struktur (Ort) und Prozess verknüpft werden ---


@app.get("/api/aufgaben")
def list_aufgaben():
    result = _conn.execute(
        """
        MATCH (a:Aufgabe)
        OPTIONAL MATCH (a)-[:AUFGABE_AN_ORT]->(s:Struktur)
        OPTIONAL MATCH (a)-[tv:TEIL_VON]->(pr:Prozess)
        RETURN a.id, a.name, a.beschreibung, a.kategorie, s.name, pr.name, tv.reihenfolge
        ORDER BY a.name
        """
    )
    items = []
    while result.has_next():
        aid, name, beschreibung, kategorie, ort, prozess, reihenfolge = result.get_next()
        items.append(
            {
                "id": aid,
                "name": name,
                "beschreibung": beschreibung,
                "kategorie": kategorie,
                "ort": ort,
                "prozess": prozess,
                "reihenfolge": reihenfolge,
            }
        )
    return jsonify(items)


@app.post("/api/aufgaben")
def create_aufgabe():
    body = request.get_json(force=True)
    name = (body.get("name") or "").strip()
    beschreibung = (body.get("beschreibung") or "").strip()
    kategorie = (body.get("kategorie") or "").strip()
    struktur_id = (body.get("struktur_id") or "").strip()
    prozess_id = (body.get("prozess_id") or "").strip()
    reihenfolge = body.get("reihenfolge")

    if not name:
        return jsonify({"error": "Name darf nicht leer sein."}), 400

    aid = new_id("a")
    _conn.execute(
        """
        CREATE (:Aufgabe {id: $id, name: $name, beschreibung: $beschreibung,
                           kategorie: $kategorie})
        """,
        {"id": aid, "name": name, "beschreibung": beschreibung, "kategorie": kategorie},
    )

    if struktur_id:
        _conn.execute(
            """
            MATCH (a:Aufgabe {id: $aid}), (s:Struktur {id: $sid})
            CREATE (a)-[:AUFGABE_AN_ORT]->(s)
            """,
            {"aid": aid, "sid": struktur_id},
        )

    if prozess_id:
        _conn.execute(
            """
            MATCH (a:Aufgabe {id: $aid}), (pr:Prozess {id: $prid})
            CREATE (a)-[:TEIL_VON {reihenfolge: $reihenfolge}]->(pr)
            """,
            {"aid": aid, "prid": prozess_id, "reihenfolge": int(reihenfolge) if reihenfolge else 1},
        )

    return jsonify({"id": aid, "name": name}), 201


@app.delete("/api/aufgaben/<aufgabe_id>")
def delete_aufgabe(aufgabe_id: str):
    # Direkte Termine der Aufgabe mitloeschen (Ressource-Termine gehoeren der Ressource).
    _conn.execute(
        "MATCH (a:Aufgabe {id: $id})-[:AUFGABE_HAT_TERMIN]->(t:Termin) DETACH DELETE t",
        {"id": aufgabe_id},
    )
    _conn.execute("MATCH (a:Aufgabe {id: $id}) DETACH DELETE a", {"id": aufgabe_id})
    entferne_verwaiste_erledigungen(_conn)
    return "", 204


# --- Zuständigkeiten: Person <-> Aufgabe, mit individueller Vorlaufzeit ---


@app.get("/api/zustaendigkeiten")
def list_zustaendigkeiten():
    result = _conn.execute(
        """
        MATCH (p:Person)-[v:VERANTWORTLICH_FUER]->(a:Aufgabe)
        RETURN p.id, p.name, a.id, a.name, v.vorlaufzeit_tage
        ORDER BY p.name, a.name
        """
    )
    items = []
    while result.has_next():
        pid, pname, aid, aname, vorlauf = result.get_next()
        items.append(
            {"person_id": pid, "person": pname, "aufgabe_id": aid, "aufgabe": aname,
             "vorlaufzeit_tage": vorlauf}
        )
    return jsonify(items)


@app.post("/api/zustaendigkeiten")
def create_zustaendigkeit():
    body = request.get_json(force=True)
    person_id = (body.get("person_id") or "").strip()
    aufgabe_id = (body.get("aufgabe_id") or "").strip()
    vorlaufzeit_tage = int(body.get("vorlaufzeit_tage") or 0)

    if not person_id or not aufgabe_id:
        return jsonify({"error": "Person und Aufgabe sind erforderlich."}), 400

    # Bestehende Zuordnung für dieses Paar ersetzen (Upsert), statt Duplikate anzulegen.
    _conn.execute(
        """
        MATCH (p:Person {id: $pid})-[v:VERANTWORTLICH_FUER]->(a:Aufgabe {id: $aid})
        DELETE v
        """,
        {"pid": person_id, "aid": aufgabe_id},
    )
    _conn.execute(
        """
        MATCH (p:Person {id: $pid}), (a:Aufgabe {id: $aid})
        CREATE (p)-[:VERANTWORTLICH_FUER {vorlaufzeit_tage: $vorlauf}]->(a)
        """,
        {"pid": person_id, "aid": aufgabe_id, "vorlauf": vorlaufzeit_tage},
    )
    return jsonify({"person_id": person_id, "aufgabe_id": aufgabe_id,
                     "vorlaufzeit_tage": vorlaufzeit_tage}), 201


@app.delete("/api/zustaendigkeiten/<person_id>/<aufgabe_id>")
def delete_zustaendigkeit(person_id: str, aufgabe_id: str):
    _conn.execute(
        """
        MATCH (p:Person {id: $pid})-[v:VERANTWORTLICH_FUER]->(a:Aufgabe {id: $aid})
        DELETE v
        """,
        {"pid": person_id, "aid": aufgabe_id},
    )
    return "", 204


# --- Aufgabe <-> Ressource (z.B. "Müll rausbringen" bezieht sich auf "Gelber Sack") ---


@app.get("/api/aufgabe-ressource")
def list_aufgabe_ressource():
    result = _conn.execute(
        """
        MATCH (a:Aufgabe)-[:BEZIEHT_SICH_AUF]->(r:Ressource)
        RETURN a.id, a.name, r.id, r.name
        ORDER BY a.name, r.name
        """
    )
    items = []
    while result.has_next():
        aid, aname, rid, rname = result.get_next()
        items.append({"aufgabe_id": aid, "aufgabe": aname, "ressource_id": rid, "ressource": rname})
    return jsonify(items)


@app.post("/api/aufgabe-ressource")
def create_aufgabe_ressource():
    body = request.get_json(force=True)
    aufgabe_id = (body.get("aufgabe_id") or "").strip()
    ressource_id = (body.get("ressource_id") or "").strip()
    if not aufgabe_id or not ressource_id:
        return jsonify({"error": "Aufgabe und Ressource sind erforderlich."}), 400

    _conn.execute(
        """
        MATCH (a:Aufgabe {id: $aid}), (r:Ressource {id: $rid})
        CREATE (a)-[:BEZIEHT_SICH_AUF]->(r)
        """,
        {"aid": aufgabe_id, "rid": ressource_id},
    )
    return jsonify({"aufgabe_id": aufgabe_id, "ressource_id": ressource_id}), 201


@app.delete("/api/aufgabe-ressource/<aufgabe_id>/<ressource_id>")
def delete_aufgabe_ressource(aufgabe_id: str, ressource_id: str):
    _conn.execute(
        """
        MATCH (a:Aufgabe {id: $aid})-[b:BEZIEHT_SICH_AUF]->(r:Ressource {id: $rid})
        DELETE b
        """,
        {"aid": aufgabe_id, "rid": ressource_id},
    )
    return "", 204


# --- Einmalige Termine direkt an einer Aufgabe (z.B. "Rasen mähen am 20.09.") ---
# Unabhängig vom Ressource-Weg (Müllkalender): AUFGABE_HAT_TERMIN statt
# RESSOURCE_HAT_TERMIN, für Aufgaben ohne zugehörige Tonnenart.


def _pruefe_uhrzeit(wert: str) -> str:
    """Leer = ganztaegig. Sonst muss es HH:MM sein, sonst wird spaeter irgendwo
    sortiert oder gerechnet und liefert stillschweigend Unsinn."""
    if not wert:
        return ""
    try:
        return datetime.strptime(wert, "%H:%M").strftime("%H:%M")
    except ValueError:
        raise ValueError(f"Uhrzeit '{wert}' nicht lesbar, erwartet wird HH:MM (z.B. 14:30).")


@app.get("/api/aufgabe-termine")
def list_aufgabe_termine():
    result = _conn.execute(
        """
        MATCH (a:Aufgabe)-[:AUFGABE_HAT_TERMIN]->(t:Termin)
        RETURN a.id, a.name, t.id, t.datum, t.ort, t.beginn, t.dauer_minuten
        ORDER BY t.datum, t.beginn
        """
    )
    items = []
    while result.has_next():
        aid, aname, tid, datum, ort, beginn, dauer = result.get_next()
        items.append({"aufgabe_id": aid, "aufgabe": aname, "termin_id": tid,
                      "datum": str(datum), "ort": ort, "beginn": beginn,
                      "dauer_minuten": dauer})
    return jsonify(items)


@app.post("/api/aufgabe-termine")
def create_aufgabe_termin():
    body = request.get_json(force=True)
    aufgabe_id = (body.get("aufgabe_id") or "").strip()
    datum = (body.get("datum") or "").strip()
    ort = (body.get("ort") or "").strip()
    if not aufgabe_id or not datum:
        return jsonify({"error": "Aufgabe und Datum sind erforderlich."}), 400
    try:
        beginn = _pruefe_uhrzeit((body.get("beginn") or "").strip())
    except ValueError as fehler:
        return jsonify({"error": str(fehler)}), 400
    dauer = int(body.get("dauer_minuten") or 0)

    tid = new_id("t")
    _conn.execute(
        """CREATE (:Termin {id: $id, datum: date($datum), ort: $ort,
                            beginn: $beginn, dauer_minuten: $dauer})""",
        {"id": tid, "datum": datum, "ort": ort, "beginn": beginn, "dauer": dauer},
    )
    _conn.execute(
        """
        MATCH (a:Aufgabe {id: $aid}), (t:Termin {id: $tid})
        CREATE (a)-[:AUFGABE_HAT_TERMIN]->(t)
        """,
        {"aid": aufgabe_id, "tid": tid},
    )
    return jsonify({"aufgabe_id": aufgabe_id, "termin_id": tid, "datum": datum,
                    "ort": ort, "beginn": beginn, "dauer_minuten": dauer}), 201


@app.delete("/api/aufgabe-termine/<termin_id>")
def delete_aufgabe_termin(termin_id: str):
    _conn.execute("MATCH (t:Termin {id: $id}) DETACH DELETE t", {"id": termin_id})
    entferne_verwaiste_erledigungen(_conn)
    return "", 204


# --- Wer ist betroffen? (BETRIFFT) -- getrennt von "wer erledigt es" ---
# Bei "Max bringt Lena zum Hautarzt" ist Max verantwortlich und Lena betroffen.
# Ohne diese Trennung stuende Lena als Erledigende im Modell.


@app.get("/api/betrifft")
def list_betrifft():
    result = _conn.execute(
        """
        MATCH (a:Aufgabe)-[:BETRIFFT]->(p:Person)
        RETURN a.id, a.name, p.id, p.name
        ORDER BY a.name, p.name
        """
    )
    items = []
    while result.has_next():
        aid, aname, pid, pname = result.get_next()
        items.append({"aufgabe_id": aid, "aufgabe": aname, "person_id": pid, "person": pname})
    return jsonify(items)


@app.post("/api/betrifft")
def create_betrifft():
    body = request.get_json(force=True)
    aufgabe_id = (body.get("aufgabe_id") or "").strip()
    person_id = (body.get("person_id") or "").strip()
    if not aufgabe_id or not person_id:
        return jsonify({"error": "Aufgabe und Person sind erforderlich."}), 400

    # Vorhandene Kante zuerst entfernen, sonst entstehen Duplikate bei Doppelklick.
    _conn.execute(
        "MATCH (a:Aufgabe {id: $aid})-[b:BETRIFFT]->(p:Person {id: $pid}) DELETE b",
        {"aid": aufgabe_id, "pid": person_id},
    )
    _conn.execute(
        """
        MATCH (a:Aufgabe {id: $aid}), (p:Person {id: $pid})
        CREATE (a)-[:BETRIFFT]->(p)
        """,
        {"aid": aufgabe_id, "pid": person_id},
    )
    return jsonify({"aufgabe_id": aufgabe_id, "person_id": person_id}), 201


@app.delete("/api/betrifft/<aufgabe_id>/<person_id>")
def delete_betrifft(aufgabe_id: str, person_id: str):
    _conn.execute(
        "MATCH (a:Aufgabe {id: $aid})-[b:BETRIFFT]->(p:Person {id: $pid}) DELETE b",
        {"aid": aufgabe_id, "pid": person_id},
    )
    return "", 204


# --- Stundenplan: wiederkehrende Wochenstruktur, bewusst kein Termin ---
# Eine Schulstunde hat kein Datum, sondern Wochentag und Stundennummer. Sie als
# Termin zu modellieren haette pro Schuljahr ~1200 Termin-Knoten erzeugt.


@app.get("/api/schulstunden")
def list_schulstunden():
    person_filter = (request.args.get("person") or "").strip().lower()
    # Eintraege ohne Stundennummer (Betreuung) nach dem Unterricht einsortieren,
    # nicht vor die erste Stunde.
    result = _conn.execute(
        """
        MATCH (p:Person)-[:HAT_STUNDE]->(s:Schulstunde)
        RETURN p.id, p.name, s.id, s.wochentag, s.wochentag_nr, s.stunde,
               s.fach, s.raum, s.beginn, s.ende, s.lehrer, s.woche, s.quelle
        ORDER BY p.name, s.wochentag_nr,
                 CASE WHEN s.stunde = 0 THEN 99 ELSE s.stunde END, s.woche
        """
    )
    items = []
    while result.has_next():
        (pid, pname, sid, tag, tag_nr, stunde, fach, raum, beginn, ende,
         lehrer, woche, quelle) = result.get_next()
        if person_filter and pname.lower() != person_filter:
            continue
        items.append({"person_id": pid, "person": pname, "id": sid, "wochentag": tag,
                      "wochentag_nr": tag_nr, "stunde": stunde, "fach": fach,
                      "raum": raum, "beginn": beginn, "ende": ende,
                      "lehrer": lehrer, "woche": woche, "quelle": quelle})
    return jsonify(items)


def _schulstunde_aus(eintrag: dict) -> dict:
    """Prueft und normalisiert eine Schulstunde, wirft ValueError mit lesbarer Meldung.

    Gemeinsam fuer die einzelne Stunde aus der Oberflaeche und den ganzen Plan aus
    einem PDF -- sonst wuerden zwei Wege unterschiedlich streng pruefen.
    """
    fach = (eintrag.get("fach") or "").strip()
    if not fach:
        raise ValueError("Fach fehlt.")
    wochentag = (eintrag.get("wochentag") or "").strip().capitalize()
    tag_nr = wochentag_nummer(wochentag)
    woche = (eintrag.get("woche") or "").strip().upper()
    if woche not in WOCHEN:
        raise ValueError(f"Woche '{woche}' unbekannt, erlaubt: leer (jede Woche), A oder B.")
    try:
        stunde = int(eintrag.get("stunde") or 0)
    except (TypeError, ValueError):
        raise ValueError(f"Stunde '{eintrag.get('stunde')}' ist keine Zahl.")
    beginn = _pruefe_uhrzeit((eintrag.get("beginn") or "").strip())
    ende = _pruefe_uhrzeit((eintrag.get("ende") or "").strip())
    # HH:MM laesst sich als Text vergleichen. Ein vertauschtes Paar ist ein typischer
    # Lesefehler aus einem PDF-Raster.
    if beginn and ende and ende <= beginn:
        raise ValueError(f"Ende {ende} liegt nicht nach Beginn {beginn}.")
    return {"wochentag": wochentag, "wochentag_nr": tag_nr, "stunde": stunde, "fach": fach,
            "raum": (eintrag.get("raum") or "").strip(),
            "lehrer": (eintrag.get("lehrer") or "").strip(),
            "woche": woche, "beginn": beginn, "ende": ende}


def _lege_schulstunde_an(person_id: str, stunde: dict, quelle: str) -> str:
    sid = new_id("sst")
    _conn.execute(
        """CREATE (:Schulstunde {id: $id, wochentag: $wochentag, wochentag_nr: $wochentag_nr,
                                 stunde: $stunde, fach: $fach, raum: $raum,
                                 lehrer: $lehrer, woche: $woche,
                                 beginn: $beginn, ende: $ende, quelle: $quelle})""",
        {"id": sid, "quelle": quelle, **stunde},
    )
    _conn.execute(
        """
        MATCH (p:Person {id: $pid}), (s:Schulstunde {id: $sid})
        CREATE (p)-[:HAT_STUNDE]->(s)
        """,
        {"pid": person_id, "sid": sid},
    )
    return sid


@app.post("/api/schulstunden")
def create_schulstunde():
    body = request.get_json(force=True)
    person_id = (body.get("person_id") or "").strip()
    if not person_id:
        return jsonify({"error": "Person ist erforderlich."}), 400
    try:
        stunde = _schulstunde_aus(body)
    except ValueError as fehler:
        return jsonify({"error": str(fehler)}), 400

    sid = _lege_schulstunde_an(person_id, stunde, (body.get("quelle") or "").strip())
    return jsonify({"id": sid, "person_id": person_id, **stunde}), 201


@app.delete("/api/schulstunden/<schulstunde_id>")
def delete_schulstunde(schulstunde_id: str):
    _conn.execute("MATCH (s:Schulstunde {id: $id}) DETACH DELETE s", {"id": schulstunde_id})
    return "", 204


def _entferne_stundenplan(person_id: str) -> int:
    """Stunden einer Person und ihre Stundenplan-Quelle loeschen. Gibt die Zahl der
    geloeschten Stunden zurueck.

    Erst die Kante zur Person, dann jedes Stundenplan-Dokument, das danach niemanden
    mehr betrifft. So bleibt ein Dokument, das an mehreren Personen haengt, fuer die
    anderen erhalten.
    """
    result = _conn.execute(
        "MATCH (p:Person {id: $pid})-[:HAT_STUNDE]->(s:Schulstunde) RETURN count(s)",
        {"pid": person_id},
    )
    anzahl = result.get_next()[0] if result.has_next() else 0
    _conn.execute(
        "MATCH (p:Person {id: $pid})-[:HAT_STUNDE]->(s:Schulstunde) DETACH DELETE s",
        {"pid": person_id},
    )
    _conn.execute(
        """MATCH (d:Dokument {typ: 'Stundenplan'})-[r:DOKUMENT_BETRIFFT]->(p:Person {id: $pid})
           DELETE r""",
        {"pid": person_id},
    )
    _conn.execute(
        """MATCH (d:Dokument {typ: 'Stundenplan'})
           WHERE NOT EXISTS { MATCH (d)-[:DOKUMENT_BETRIFFT]->(:Person) }
           DELETE d"""
    )
    return anzahl


@app.delete("/api/schulstunden/person/<person_id>")
def delete_schulstunden_von_person(person_id: str):
    """Ganzen Stundenplan einer Person loeschen -- fuer den Wechsel zum Halbjahr."""
    return jsonify({"geloescht": _entferne_stundenplan(person_id)})


def _in_transaktion(schritte, conn=None):
    """Fuehrt schritte() atomar aus: alles oder nichts.

    Kuzu rollt bei einem Datenbankfehler selbst zurueck -- ein anschliessendes
    ROLLBACK findet dann keine offene Transaktion mehr und scheitert. Das ist kein
    zweiter Fehler, sondern bestaetigt nur, dass schon zurueckgerollt wurde.
    """
    conn = conn or _conn
    conn.execute("BEGIN TRANSACTION")
    try:
        ergebnis = schritte()
        conn.execute("COMMIT")
        return ergebnis
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except RuntimeError:
            pass
        raise


def _pruefe_stundenplan(eintraege: list) -> tuple[list[dict], list[str]]:
    """Alle Eintraege pruefen, bevor irgendetwas geschrieben wird.

    Sammelt *alle* Fehler statt beim ersten abzubrechen: eine KI, die ein PDF
    gelesen hat, kann so alle Lesefehler in einem Durchgang korrigieren.
    """
    gueltig, fehler = [], []
    for nr, eintrag in enumerate(eintraege, start=1):
        try:
            gueltig.append(_schulstunde_aus(eintrag))
        except ValueError as e:
            fach = (eintrag.get("fach") or "?") if isinstance(eintrag, dict) else "?"
            fehler.append(f"Eintrag {nr} ({fach}): {e}")

    # Doppelt belegte Plaetze im Raster sind fast immer Lesefehler. "Jede Woche"
    # belegt den Platz in Woche A und B. Stunde 0 heisst "ohne Nummer" (z.B.
    # Betreuung) und wird nicht geprueft.
    belegt: dict[tuple, str] = {}
    for nr, st in enumerate(gueltig, start=1):
        if st["stunde"] <= 0:
            continue
        for woche in (("A", "B") if st["woche"] == "" else (st["woche"],)):
            platz = (st["wochentag_nr"], st["stunde"], woche)
            if platz in belegt:
                fehler.append(
                    f"{st['wochentag']} {st['stunde']}. Stunde ist doppelt belegt: "
                    f"{belegt[platz]} und {st['fach']}"
                    + ("" if st["woche"] else " (fuer A/B-Wochen das Feld woche setzen)")
                )
                break
            belegt[platz] = st["fach"]
    return gueltig, fehler


def _tagesuebersicht(stunden: list[dict]) -> list[dict]:
    """Pro Tag: Zahl der Stunden, Beginn, Unterrichtsende und Ende inkl. Betreuung.

    Unterrichtsende und Betreuungsende getrennt, weil genau das die Abholzeit
    bestimmt. Eintraege ohne Stundennummer (Betreuung, OGS) zaehlen nicht als
    Unterricht; A- und B-Stunde auf demselben Platz zaehlen als eine Stunde.
    """
    tage: dict[int, dict] = {}
    plaetze: dict[int, set] = {}
    for st in stunden:
        tag = tage.setdefault(st["wochentag_nr"], {
            "wochentag": st["wochentag"], "stunden": 0, "von": "",
            "unterricht_bis": "", "bis": ""})
        if st["stunde"] > 0:
            plaetze.setdefault(st["wochentag_nr"], set()).add(st["stunde"])
            if st["ende"] > tag["unterricht_bis"]:
                tag["unterricht_bis"] = st["ende"]
        if st["beginn"] and (not tag["von"] or st["beginn"] < tag["von"]):
            tag["von"] = st["beginn"]
        if st["ende"] > tag["bis"]:
            tag["bis"] = st["ende"]
    for nr, tag in tage.items():
        tag["stunden"] = len(plaetze.get(nr, ()))
    return [tage[nr] for nr in sorted(tage)]


@app.put("/api/stundenplan/<person_id>")
def setze_stundenplan(person_id: str):
    """Ersetzt den kompletten Stundenplan einer Person -- gedacht fuer einen Plan, den
    eine KI aus einem PDF gelesen hat.

    Body: {"stunden": [...], "quelle": "Dateiname.pdf",
           "dokument": {"titel": ..., "text": ...}}   (quelle und dokument optional)

    Erst wird alles geprueft, dann in einer Transaktion geschrieben. Ein Fehler in
    Eintrag 17 hinterlaesst also keinen halben Plan.
    """
    body = request.get_json(force=True)
    person = _conn.execute("MATCH (p:Person {id: $id}) RETURN p.name", {"id": person_id})
    if not person.has_next():
        return jsonify({"error": f"Person {person_id} nicht gefunden."}), 404
    person_name = person.get_next()[0]

    eintraege = body.get("stunden") or []
    if not eintraege:
        return jsonify({"error": "Keine Stunden uebergeben. Zum Loeschen "
                                 "DELETE /api/schulstunden/person/<id> benutzen."}), 400

    gueltig, fehler = _pruefe_stundenplan(eintraege)
    if fehler:
        return jsonify({"error": f"{len(fehler)} Problem(e), nichts gespeichert.",
                        "fehler": fehler}), 400

    dokument = body.get("dokument") or {}
    quelle = (body.get("quelle") or "").strip()
    text = (dokument.get("text") or "").strip()
    titel = (dokument.get("titel") or "").strip() or f"Stundenplan {person_name}"

    def schreiben():
        ersetzt = _entferne_stundenplan(person_id)
        for st in gueltig:
            _lege_schulstunde_an(person_id, st, quelle)
        dokument_id = None
        # Ein Dokument nur anlegen, wenn es etwas festzuhalten gibt -- ein Plan, der
        # von Hand eingegeben wurde, hat keine Quelle.
        if quelle or text:
            dokument_id = new_id("dok")
            _conn.execute(
                """CREATE (:Dokument {id: $id, titel: $titel, typ: 'Stundenplan',
                                      quelle: $quelle, datum: date($datum), text: $text})""",
                {"id": dokument_id, "titel": titel, "quelle": quelle,
                 "datum": date.today().isoformat(), "text": text},
            )
            _conn.execute(
                """MATCH (d:Dokument {id: $did}), (p:Person {id: $pid})
                   CREATE (d)-[:DOKUMENT_BETRIFFT]->(p)""",
                {"did": dokument_id, "pid": person_id},
            )
        return ersetzt, dokument_id

    ersetzt, dokument_id = _in_transaktion(schreiben)
    return jsonify({
        "person": person_name,
        "gespeichert": len(gueltig),
        "ersetzt": ersetzt,
        "quelle": quelle,
        "dokument_id": dokument_id,
        "tage": _tagesuebersicht(gueltig),
    })


@app.get("/api/dokumente")
def list_dokumente():
    result = _conn.execute(
        """
        MATCH (d:Dokument)
        OPTIONAL MATCH (d)-[:DOKUMENT_BETRIFFT]->(p:Person)
        RETURN d.id, d.titel, d.typ, d.quelle, d.datum, collect(p.name)
        ORDER BY d.datum DESC, d.titel
        """
    )
    items = []
    while result.has_next():
        did, titel, typ, quelle, datum, personen = result.get_next()
        items.append({"id": did, "titel": titel, "typ": typ, "quelle": quelle,
                      "datum": str(datum) if datum else "",
                      "personen": [n for n in (personen or []) if n]})
    return jsonify(items)


@app.get("/api/wochentage")
def list_wochentage():
    return jsonify(WOCHENTAGE)


# --- Kalender aktuell halten: der Server synchronisiert selbst, nicht die KI ---
# Zwei Ausloeser: bei einer Abfrage, wenn der letzte Sync laenger als SYNC_BEI_ABFRAGE
# zurueckliegt -- Fragen sehen so praktisch den aktuellen Google-Stand --, und im
# Hintergrund spaetestens nach SYNC_INTERVALL, weil Erinnerungen ohne Frage entstehen.
#
# Kuzu erlaubt nur einen Schreibvorgang gleichzeitig, und ein zweiter wartet nicht,
# sondern scheitert sofort. Deshalb laufen alle Schreibzugriffe unter _schreib_lock.
# Der Sync schreibt auf einer eigenen Verbindung in einer Transaktion: Leser sehen bis
# zum Abschluss den alten, vollstaendigen Stand. Google fragt er vor dem Sperren ab,
# damit kein Schreibzugriff auf das Netz warten muss.
#
# Sperren immer in dieser Reihenfolge: erst _sync_lock, dann _schreib_lock. Wer
# _schreib_lock haelt, startet keinen Sync -- sonst koennten sich zwei Threads
# gegenseitig blockieren.

_schreib_lock = threading.RLock()
_sync_lock = threading.Lock()
_sync_conn = kuzu.Connection(_db)
_sync_zustand: dict = {"letzter_versuch": None, "letzter_erfolg": None,
                       "letzter_fehler": None, "ergebnis": None}


def _fehlertext(fehler: Exception) -> str:
    """Fehlermeldungen von requests enthalten die Kalenderadresse -- und die ist so
    geheim wie ein Passwort. Deshalb nur die Art des Fehlers und ggf. den HTTP-Status."""
    if isinstance(fehler, requests.HTTPError) and fehler.response is not None:
        return f"Kalender antwortet mit HTTP {fehler.response.status_code}"
    if isinstance(fehler, requests.RequestException):
        return f"Kalender nicht erreichbar ({type(fehler).__name__})"
    return f"{type(fehler).__name__}: {fehler}"


def kalender_abgleichen(max_alter: timedelta | None = None, neu_aufbauen: bool = False):
    """Gleicht die Kalender ab, wenn der letzte Versuch aelter als max_alter ist.

    max_alter=None erzwingt den Sync und meldet Fehler als Exception (manueller
    Aufruf). Sonst wird ein Fehler nur vermerkt: eine Abfrage soll mit dem letzten
    Stand antworten, statt zu scheitern, weil Google gerade nicht erreichbar ist.
    """
    if not SOURCES_PATH.exists():
        return None

    def noetig() -> bool:
        letzter = _sync_zustand["letzter_versuch"]
        return max_alter is None or letzter is None or datetime.now() - letzter >= max_alter

    # Schneller Weg ohne Sperre. Laeuft gerade ein Sync, gilt sein Start schon als
    # frisch: die Abfrage antwortet sofort mit dem letzten vollstaendigen Stand, statt
    # zu warten -- haengt Google, waeren das bis zu zehn Sekunden.
    if not noetig():
        return None
    # Nach dem Warten auf die Sperre noch einmal pruefen: kommen zehn Anfragen
    # gleichzeitig, synchronisiert nur die erste.
    with _sync_lock:
        if not noetig():
            return None
        jetzt = datetime.now()
        # Der Versuch zaehlt, nicht der Erfolg: ist Google nicht erreichbar, soll nicht
        # jede Abfrage erneut bis zum Timeout warten.
        _sync_zustand["letzter_versuch"] = jetzt
        try:
            quellen = [(q["name"], fetch_events(q["url"]))
                       for q in json.loads(SOURCES_PATH.read_text(encoding="utf-8"))]

            def schritte():
                if neu_aufbauen:
                    clear_previous_import(_sync_conn)
                return synchronisiere(_sync_conn, quellen, massgeblicher_zeitraum())

            with _schreib_lock:
                ergebnis = _in_transaktion(schritte, _sync_conn)
        except Exception as fehler:
            _sync_zustand["letzter_fehler"] = f"{datetime.now():%d.%m. %H:%M} {_fehlertext(fehler)}"
            print(f"Kalender-Sync fehlgeschlagen: {_sync_zustand['letzter_fehler']}", flush=True)
            if max_alter is None:
                raise
            return None

        ergebnis["quellen"] = [name for name, _ in quellen]
        ergebnis["neu_aufgebaut"] = neu_aufbauen
        _sync_zustand.update(letzter_erfolg=jetzt, letzter_fehler=None, ergebnis=ergebnis)
        if ergebnis["aufgaben_neu"] or ergebnis["aufgaben_aktualisiert"] or ergebnis["aufgaben_entfernt"]:
            print(f"{jetzt:%H:%M:%S} Kalender: {ergebnis['aufgaben_neu']} neu, "
                  f"{ergebnis['aufgaben_aktualisiert']} geaendert, "
                  f"{ergebnis['aufgaben_entfernt']} entfernt", flush=True)
        return ergebnis


def _kalender_stand() -> dict:
    """Wie aktuell die Kalendertermine sind -- damit eine KI ehrlich sagen kann,
    wenn Google zuletzt nicht erreichbar war."""
    erfolg = _sync_zustand["letzter_erfolg"]
    return {"kalender_stand": erfolg.isoformat(timespec="seconds") if erfolg else None,
            "kalender_fehler": _sync_zustand["letzter_fehler"]}


def _hintergrund_sync() -> None:
    """Prueft regelmaessig, ob der letzte Sync aelter als SYNC_INTERVALL ist.

    Jede Minute pruefen statt alle 15 Minuten schlafen: ein Sync bei einer Abfrage
    verschiebt sonst den Takt, und der Stand koennte fast doppelt so alt werden.
    """
    while True:
        kalender_abgleichen(max_alter=SYNC_INTERVALL)
        time.sleep(min(60.0, SYNC_INTERVALL.total_seconds()))


# Anfragen, die zwar per POST kommen, aber nur lesen.
LESENDE_POSTS = {"/api/query"}


@app.before_request
def _vor_anfrage():
    # Die Kalender-Endpunkte kuemmern sich selbst um Sperren und Sync.
    if not request.path.startswith("/api/") or request.path.startswith("/api/kalender/"):
        return
    if request.method == "GET" or request.path in LESENDE_POSTS:
        kalender_abgleichen(max_alter=SYNC_BEI_ABFRAGE)
    else:
        _schreib_lock.acquire()
        g.haelt_schreib_lock = True


@app.teardown_request
def _nach_anfrage(_fehler):
    if g.pop("haelt_schreib_lock", False):
        _schreib_lock.release()


@app.post("/api/kalender/sync")
def sync_kalender():
    """Sofort abgleichen, unabhaengig vom Takt. neu_aufbauen ist der Notnagel, wenn der
    Importstand kaputt ist -- er wirft alle importierten Aufgaben samt manueller
    Zuordnungen weg."""
    if not SOURCES_PATH.exists():
        return jsonify({"error": f"{SOURCES_PATH.name} fehlt (siehe calendar_sources.example.json)."}), 400
    neu_aufbauen = bool((request.get_json(silent=True) or {}).get("neu_aufbauen"))
    try:
        return jsonify(kalender_abgleichen(neu_aufbauen=neu_aufbauen))
    except Exception as fehler:
        return jsonify({"error": _fehlertext(fehler)}), 502


@app.get("/api/kalender/status")
def kalender_status():
    versuch = _sync_zustand["letzter_versuch"]
    return jsonify({
        **_kalender_stand(),
        "letzter_versuch": versuch.isoformat(timespec="seconds") if versuch else None,
        "letztes_ergebnis": _sync_zustand["ergebnis"],
        "abgleich_bei_abfrage_nach_sekunden": SYNC_BEI_ABFRAGE.total_seconds(),
        "hintergrund_intervall_minuten": SYNC_INTERVALL.total_seconds() / 60,
    })


# --- "Was steht an?": alle Termine im Zeitfenster, mit zustaendigen Personen ---
# Anders als digest.py (individuelle Vorlaufzeit) nimmt dieser Endpunkt ein festes
# Fenster in Tagen -- das ist die natuerliche Frage einer KI ("was ist diese Woche?").

# Die Datumsgrenze steht in der Abfrage, nicht erst in Python: sonst liest jede Frage
# alle Termine aller Jahre.
_TERMIN_ZUSATZ = """
    WHERE t.datum >= $von AND t.datum <= $bis
    OPTIONAL MATCH (p:Person)-[v:VERANTWORTLICH_FUER]->(a)
    OPTIONAL MATCH (a)-[:BETRIFFT]->(b:Person)
    OPTIONAL MATCH (a)<-[:ERLEDIGUNG_ZU_AUFGABE]-(e:Erledigung)-[:ERLEDIGUNG_ZU_TERMIN]->(t)
    OPTIONAL MATCH (e)-[:ERLEDIGT_DURCH]->(d:Person)
    RETURN a.id AS aufgabe_id, t.id AS termin_id, a.name AS aufgabe, a.kategorie AS kategorie,
           t.datum AS datum, t.ort AS ort, t.beginn AS beginn, t.dauer_minuten AS dauer_minuten,
           p.name AS verantwortlich, v.vorlaufzeit_tage AS vorlauf,
           b.name AS betroffen, {kontext} AS kontext,
           e.status AS status, d.name AS erledigt_von
"""

TERMIN_QUERIES = [
    # Direkte Termine einer Aufgabe (einmalige Termine, Kalender-Import)
    "MATCH (a:Aufgabe)-[:AUFGABE_HAT_TERMIN]->(t:Termin)" + _TERMIN_ZUSATZ.format(kontext="null"),
    # Termine, die ueber eine Ressource haengen (Muellkalender)
    "MATCH (a:Aufgabe)-[:BEZIEHT_SICH_AUF]->(r:Ressource)-[:RESSOURCE_HAT_TERMIN]->(t:Termin)"
    + _TERMIN_ZUSATZ.format(kontext="r.name"),
]


def _abgeleiteter_status(datum: date, heute: date) -> str:
    if datum >= heute:
        return "offen"
    return "verpasst" if datum >= ERFASSUNG_SEIT else "unbekannt"


def _termine_im_zeitraum(von: date, bis: date, person: str = "") -> list[dict]:
    """Alle Termine zwischen von und bis, je Aufgabe und Termin ein Eintrag, mit Status.

    Gespeichert ist nur "erledigt" oder "entfaellt". Alles andere wird hier abgeleitet:
    ohne Erledigung ist ein Termin "offen", solange sein Tag nicht vorbei ist, danach
    "verpasst" -- oder "unbekannt", wenn er vor ERFASSUNG_SEIT lag.
    """
    heute = date.today()
    eintraege: dict[tuple, dict] = {}
    for cypher in TERMIN_QUERIES:
        result = _conn.execute(cypher, {"von": von, "bis": bis})
        spalten = result.get_column_names()
        while result.has_next():
            zeile = dict(zip(spalten, result.get_next()))
            eintrag = eintraege.setdefault(
                (zeile["aufgabe_id"], zeile["termin_id"]),
                {
                    "aufgabe": zeile["aufgabe"],
                    "aufgabe_id": zeile["aufgabe_id"],
                    "termin_id": zeile["termin_id"],
                    "kategorie": zeile["kategorie"],
                    "datum": zeile["datum"].isoformat(),
                    "tage_bis": (zeile["datum"] - heute).days,
                    "ort": zeile["ort"] or "",
                    "beginn": zeile["beginn"] or "",
                    "dauer_minuten": zeile["dauer_minuten"] or 0,
                    "kontext": zeile["kontext"],
                    "status": zeile["status"] or _abgeleiteter_status(zeile["datum"], heute),
                    "erledigt_von": zeile["erledigt_von"],
                    "personen": [],
                    "vorlauf": {},
                    "betroffene": [],
                },
            )
            if zeile["verantwortlich"]:
                eintrag["vorlauf"][zeile["verantwortlich"]] = zeile["vorlauf"]
            # Die OPTIONAL MATCH erzeugen ein Kreuzprodukt aus Verantwortlichen und
            # Betroffenen -- deshalb jeden Namen nur einmal aufnehmen.
            for feld, liste in (("verantwortlich", "personen"), ("betroffen", "betroffene")):
                name = zeile[feld]
                if name and name not in eintrag[liste]:
                    eintrag[liste].append(name)

    liste = list(eintraege.values())
    if person:
        # Eine Person gehoert dazu, wenn sie zustaendig ist, wenn es um sie geht oder wenn
        # sie es erledigt hat -- "was hat Anna gemacht" soll auch Aufgaben zeigen, die
        # eigentlich jemand anderem zugeordnet waren.
        gesucht = person.strip().lower()
        liste = [e for e in liste
                 if any(n.lower() == gesucht
                        for n in e["personen"] + e["betroffene"] + [e["erledigt_von"] or ""])]
    liste.sort(key=lambda e: (e["datum"], e["beginn"], e["aufgabe"]))
    return liste


@app.get("/api/anstehend")
def anstehend():
    tage = int(request.args.get("tage", 7))
    heute = date.today()
    bis = heute + timedelta(days=tage)
    liste = _termine_im_zeitraum(heute, bis, request.args.get("person") or "")
    return jsonify({"von": heute.isoformat(), "bis": bis.isoformat(), "eintraege": liste,
                    **_kalender_stand()})


@app.get("/api/rueckblick")
def rueckblick():
    """Was in den letzten Tagen anstand -- und ob es erledigt, entfallen oder verpasst ist."""
    tage = int(request.args.get("tage", 7))
    heute = date.today()
    von = heute - timedelta(days=tage)
    liste = _termine_im_zeitraum(von, heute, request.args.get("person") or "")
    zusammenfassung = {status: sum(1 for e in liste if e["status"] == status)
                       for status in ("erledigt", "entfaellt", "verpasst", "offen", "unbekannt")}
    return jsonify({"von": von.isoformat(), "bis": heute.isoformat(), "eintraege": liste,
                    "zusammenfassung": zusammenfassung, **_kalender_stand()})


def _termine_der_aufgabe_am(aufgabe_id: str, datum: str, ressource_id: str = "") -> list[str]:
    """IDs aller Termine einer Aufgabe an einem Tag -- direkt oder ueber eine Tonne."""
    ids = []
    for cypher in (
        """MATCH (a:Aufgabe {id: $aid})-[:AUFGABE_HAT_TERMIN]->(t:Termin)
           WHERE t.datum = date($datum) AND $rid = '' RETURN t.id""",
        """MATCH (a:Aufgabe {id: $aid})-[:BEZIEHT_SICH_AUF]->(r:Ressource)
                 -[:RESSOURCE_HAT_TERMIN]->(t:Termin)
           WHERE t.datum = date($datum) AND ($rid = '' OR r.id = $rid) RETURN t.id""",
    ):
        result = _conn.execute(cypher, {"aid": aufgabe_id, "datum": datum, "rid": ressource_id})
        while result.has_next():
            ids.append(result.get_next()[0])
    return ids


def _naechste_termine(aufgabe_id: str, datum: str) -> list[str]:
    """Die Termine rund um ein Datum -- damit eine KI, die den Tag verfehlt hat (Muell
    wird am Vorabend rausgestellt), den richtigen findet, statt aufzugeben."""
    naechste = set()
    for weg in ("(a)-[:AUFGABE_HAT_TERMIN]->(t:Termin)",
                "(a)-[:BEZIEHT_SICH_AUF]->(:Ressource)-[:RESSOURCE_HAT_TERMIN]->(t:Termin)"):
        result = _conn.execute(
            f"""MATCH (a:Aufgabe {{id: $aid}}), {weg}
                WHERE t.datum >= date($datum) - INTERVAL('7 days')
                  AND t.datum <= date($datum) + INTERVAL('7 days')
                RETURN DISTINCT t.datum""",
            {"aid": aufgabe_id, "datum": datum},
        )
        while result.has_next():
            naechste.add(result.get_next()[0].isoformat())
    return sorted(naechste)


@app.post("/api/erledigungen")
def setze_erledigung():
    """Festhalten, dass eine Aufgabe an einem Termin erledigt wurde (oder entfiel).

    Body: {"aufgabe_id", "datum", "status": "erledigt"|"entfaellt", "person_id"?,
           "notiz"?, "ressource_id"?}
    Ohne ressource_id gilt es fuer alle Termine der Aufgabe an diesem Tag -- "Muell ist
    draussen" betrifft alle Tonnen, die an dem Tag abgeholt werden.
    """
    body = request.get_json(force=True)
    aufgabe_id = (body.get("aufgabe_id") or "").strip()
    datum = (body.get("datum") or "").strip()
    status = (body.get("status") or "erledigt").strip().lower()
    person_id = (body.get("person_id") or "").strip()
    ressource_id = (body.get("ressource_id") or "").strip()
    if not aufgabe_id or not datum:
        return jsonify({"error": "Aufgabe und Datum sind erforderlich."}), 400
    if status not in ERLEDIGUNG_STATUS:
        return jsonify({"error": f"Status '{status}' unbekannt, erlaubt: {', '.join(ERLEDIGUNG_STATUS)}."}), 400
    try:
        date.fromisoformat(datum)
    except ValueError:
        return jsonify({"error": f"Datum '{datum}' nicht lesbar, erwartet YYYY-MM-DD."}), 400

    aufgabe = _conn.execute("MATCH (a:Aufgabe {id: $id}) RETURN a.name", {"id": aufgabe_id})
    if not aufgabe.has_next():
        return jsonify({"error": f"Aufgabe {aufgabe_id} nicht gefunden."}), 404
    aufgabe_name = aufgabe.get_next()[0]
    person_name = None
    if person_id:
        person = _conn.execute("MATCH (p:Person {id: $id}) RETURN p.name", {"id": person_id})
        if not person.has_next():
            return jsonify({"error": f"Person {person_id} nicht gefunden."}), 404
        person_name = person.get_next()[0]

    termine = _termine_der_aufgabe_am(aufgabe_id, datum, ressource_id)
    if not termine:
        return jsonify({
            "error": f"'{aufgabe_name}' hat am {datum} keinen Termin.",
            "termine_in_der_naehe": _naechste_termine(aufgabe_id, datum),
        }), 404

    zeitpunkt = datetime.now()

    def schreiben():
        for termin_id in termine:
            # Eine Erledigung je Aufgabe und Termin: erneutes Festhalten ersetzt die alte.
            erl_id = f"erl-{aufgabe_id}-{termin_id}"
            _conn.execute("MATCH (e:Erledigung {id: $id}) DETACH DELETE e", {"id": erl_id})
            _conn.execute(
                """CREATE (:Erledigung {id: $id, status: $status, zeitpunkt: $zeit, notiz: $notiz})""",
                {"id": erl_id, "status": status, "zeit": zeitpunkt,
                 "notiz": (body.get("notiz") or "").strip()},
            )
            _conn.execute(
                """MATCH (e:Erledigung {id: $eid}), (t:Termin {id: $tid}), (a:Aufgabe {id: $aid})
                   CREATE (e)-[:ERLEDIGUNG_ZU_TERMIN]->(t), (e)-[:ERLEDIGUNG_ZU_AUFGABE]->(a)""",
                {"eid": erl_id, "tid": termin_id, "aid": aufgabe_id},
            )
            if person_id:
                _conn.execute(
                    """MATCH (e:Erledigung {id: $eid}), (p:Person {id: $pid})
                       CREATE (e)-[:ERLEDIGT_DURCH]->(p)""",
                    {"eid": erl_id, "pid": person_id},
                )

    _in_transaktion(schreiben)
    return jsonify({"aufgabe": aufgabe_name, "datum": datum, "status": status,
                    "erledigt_von": person_name, "termine": len(termine)}), 201


@app.delete("/api/erledigungen")
def loesche_erledigung():
    """Eine Erledigung zuruecknehmen -- der Termin gilt dann wieder als offen/verpasst."""
    body = request.get_json(force=True)
    aufgabe_id = (body.get("aufgabe_id") or "").strip()
    datum = (body.get("datum") or "").strip()
    if not aufgabe_id or not datum:
        return jsonify({"error": "Aufgabe und Datum sind erforderlich."}), 400
    termine = _termine_der_aufgabe_am(aufgabe_id, datum, (body.get("ressource_id") or "").strip())
    for termin_id in termine:
        _conn.execute("MATCH (e:Erledigung {id: $id}) DETACH DELETE e",
                      {"id": f"erl-{aufgabe_id}-{termin_id}"})
    return jsonify({"zurueckgesetzt": len(termine)})


# --- Erinnerungen: der Server erinnert von selbst, zugestellt ueber Home Assistant ---
# Die Planung steckt in erinnerungen.py. Hier: welche Termine, was schon gesendet wurde,
# und die Schleife, die jede Minute nachsieht.

_erinnerungs_zustand: dict = {"letzter_lauf": None, "letzter_fehler": None, "aufgeschoben": {}}
# Scheitert die Zustellung (Home Assistant nicht erreichbar, Dienstname falsch), wird es
# erst nach dieser Pause erneut versucht -- sonst jede Minute derselbe Fehler.
ERINNERUNG_WIEDERHOLEN_NACH = timedelta(minutes=10)
ERINNERUNG_VORAUS_TAGE = 14


def _rollen() -> dict[str, str]:
    result = _conn.execute("MATCH (p:Person) RETURN p.name, p.rolle")
    rollen = {}
    while result.has_next():
        name, rolle = result.get_next()
        rollen[name] = rolle
    return rollen


def _erinnerungsplan(jetzt: datetime, einstellungen: dict) -> list[dict]:
    heute = jetzt.date()
    eintraege = _termine_im_zeitraum(heute, heute + timedelta(days=ERINNERUNG_VORAUS_TAGE))
    return plane(eintraege, _rollen(), einstellungen)


def _schon_gesendet(ids: list[str]) -> set[str]:
    if not ids:
        return set()
    result = _conn.execute("MATCH (e:Erinnerung) WHERE e.id IN $ids RETURN e.id", {"ids": ids})
    gesendet = set()
    while result.has_next():
        gesendet.add(result.get_next()[0])
    return gesendet


def erinnerungen_senden(jetzt: datetime | None = None) -> list[dict]:
    """Sendet, was jetzt faellig und noch nicht gesendet ist. Gibt das Gesendete zurueck."""
    jetzt = jetzt or datetime.now()
    einstellungen = lade_einstellungen()
    aufgeschoben = _erinnerungs_zustand["aufgeschoben"]

    def offene():
        dran = faellige(_erinnerungsplan(jetzt, einstellungen), jetzt)
        gesendet = _schon_gesendet([x["id"] for x in dran])
        return [x for x in dran if x["id"] not in gesendet and aufgeschoben.get(x["id"], jetzt) <= jetzt]

    if not offene():
        return []
    # Vor dem Senden den Kalender auffrischen: ein gerade verschobener Termin soll nicht
    # mit der alten Uhrzeit angekuendigt werden. Nur hier, nicht jede Minute.
    kalender_abgleichen(max_alter=SYNC_BEI_ABFRAGE)

    gruppen: dict[tuple, list] = {}
    for x in offene():
        gruppen.setdefault((x["person"], x["art"]), []).append(x)

    protokoll = []
    for (person, _art), gruppe in gruppen.items():
        titel, text = nachricht(gruppe, jetzt)
        try:
            # Nur Ueberholtes in der Gruppe: nichts zu senden, trotzdem verbuchen.
            kanal = sende(person, titel, text, einstellungen) if text else "ueberholt"
        except Exception as fehler:
            _erinnerungs_zustand["letzter_fehler"] = (
                f"{jetzt:%d.%m. %H:%M} an {person}: {type(fehler).__name__} {fehler}")
            print(f"Erinnerung nicht zugestellt: {_erinnerungs_zustand['letzter_fehler']}", flush=True)
            for x in gruppe:
                aufgeschoben[x["id"]] = jetzt + ERINNERUNG_WIEDERHOLEN_NACH
            continue
        with _schreib_lock:
            for x in gruppe:
                _conn.execute(
                    """CREATE (:Erinnerung {id: $id, art: $art, person: $person, termin_id: $tid,
                                            faellig: $faellig, gesendet: $gesendet,
                                            kanal: $kanal, text: $text})""",
                    {"id": x["id"], "art": x["art"], "person": person,
                     "tid": x["eintrag"]["termin_id"], "faellig": x["faellig"], "gesendet": jetzt,
                     "kanal": "ueberholt" if x.get("ueberholt") else kanal, "text": text},
                )
                aufgeschoben.pop(x["id"], None)
        protokoll.append({"person": person, "kanal": kanal, "text": text})
    return protokoll


def _erinnerungs_schleife() -> None:
    """Jede Minute nachsehen. Einmal am Tag alte Protokolleintraege aufraeumen."""
    zuletzt_aufgeraeumt = None
    while True:
        jetzt = datetime.now()
        try:
            erinnerungen_senden(jetzt)
            if zuletzt_aufgeraeumt != jetzt.date():
                with _schreib_lock:
                    _conn.execute("MATCH (e:Erinnerung) WHERE e.gesendet < $grenze DELETE e",
                                  {"grenze": jetzt - timedelta(days=90)})
                zuletzt_aufgeraeumt = jetzt.date()
        except Exception as fehler:
            _erinnerungs_zustand["letzter_fehler"] = f"{jetzt:%d.%m. %H:%M} {type(fehler).__name__}: {fehler}"
            print(f"Erinnerungsschleife: {_erinnerungs_zustand['letzter_fehler']}", flush=True)
        _erinnerungs_zustand["letzter_lauf"] = jetzt
        time.sleep(60)


@app.get("/api/erinnerungen")
def erinnerungen_vorschau():
    """Was demnaechst an wen geht und was zuletzt gesendet wurde."""
    tage = int(request.args.get("tage", 2))
    jetzt = datetime.now()
    einstellungen = lade_einstellungen()
    plan = _erinnerungsplan(jetzt, einstellungen)
    gesendet_ids = _schon_gesendet([x["id"] for x in plan])
    bis = jetzt + timedelta(days=tage)
    geplant = [
        {"faellig": x["faellig"].isoformat(timespec="minutes"), "person": x["person"],
         "art": x["art"], "kanal": kanal_fuer(x["person"], einstellungen),
         "text": nachricht([x], max(jetzt, x["faellig"]))[1]}
        for x in plan
        if x["id"] not in gesendet_ids and x["faellig"] <= bis and jetzt < x["relevant_bis"]
    ]
    result = _conn.execute(
        """MATCH (e:Erinnerung) WHERE e.kanal <> 'ueberholt'
           RETURN e.gesendet, e.person, e.kanal, e.text ORDER BY e.gesendet DESC LIMIT 20""")
    zuletzt = []
    while result.has_next():
        gesendet, person, kanal, text = result.get_next()
        eintrag = {"gesendet": gesendet.isoformat(timespec="minutes"), "person": person,
                   "kanal": kanal, "text": text}
        # Eine Nachricht mit mehreren Zeilen erzeugt mehrere Protokolleintraege.
        if eintrag not in zuletzt:
            zuletzt.append(eintrag)
    lauf = _erinnerungs_zustand["letzter_lauf"]
    return jsonify({
        "geplant": geplant,
        "zuletzt_gesendet": zuletzt,
        "zeiten": {k: einstellungen[k] for k in ("abend", "morgen", "stunden_vorher")},
        "empfaenger": {name: kanal_fuer(name, einstellungen) for name in _rollen()},
        "letzter_lauf": lauf.isoformat(timespec="seconds") if lauf else None,
        "letzter_fehler": _erinnerungs_zustand["letzter_fehler"],
    })


@app.post("/api/erinnerungen/testen")
def erinnerung_testen():
    """Schickt einer Person eine Testnachricht -- zum Pruefen der Home-Assistant-Anbindung."""
    person = (request.get_json(force=True).get("person") or "").strip()
    if person not in _rollen():
        return jsonify({"error": f"Person '{person}' nicht gefunden."}), 404
    einstellungen = lade_einstellungen()
    try:
        kanal = sende(person, "ontoFam", "Test: Erinnerungen kommen an.", einstellungen)
    except Exception as fehler:
        return jsonify({"error": f"{type(fehler).__name__}: {fehler}",
                        "kanal": kanal_fuer(person, einstellungen)}), 502
    return jsonify({"person": person, "kanal": kanal})


@app.get("/api/schema")
def get_schema():
    """Schema des Graphen -- die Vorlage, aus der eine KI ihre Abfragen baut."""
    with _read_lock:
        return jsonify({"text": schema_als_text(_read_conn), **beschreibe_schema(_read_conn)})


@app.post("/api/query")
def run_query():
    cypher = (request.get_json(force=True).get("cypher") or "").strip()
    try:
        with _read_lock:
            return jsonify(fuehre_lesend_aus(_read_conn, cypher))
    except (ValueError, RuntimeError) as fehler:
        # Syntaxfehler und abgewiesene Schreibversuche kommen hier an. Die
        # Originalmeldung weitergeben: für die KI ist sie die beste Korrekturhilfe.
        return jsonify({"error": str(fehler)}), 400


if __name__ == "__main__":
    # Daemon: der Thread endet mit dem Server, statt ihn am Beenden zu hindern.
    threading.Thread(target=_hintergrund_sync, daemon=True, name="kalender-sync").start()
    threading.Thread(target=_erinnerungs_schleife, daemon=True, name="erinnerungen").start()
    def _beenden(*_):
        # Home Assistant stoppt das Add-on mit SIGTERM (auch vor jedem Backup). Vorher
        # das Write-Ahead-Log in die Datenbankdatei schreiben: dann ist kuzu_db allein
        # vollstaendig und laesst sich kopieren oder sichern.
        if _schreib_lock.acquire(timeout=10):
            _conn.execute("CHECKPOINT")
        os._exit(0)

    signal.signal(signal.SIGTERM, _beenden)
    host = os.environ.get("ONTOFAM_HOST", "127.0.0.1")
    port = int(os.environ.get("ONTOFAM_PORT", 5000))
    try:
        from waitress import serve
    except ImportError:
        # Auf dem PC reicht der Entwicklungsserver. Kein debug=True: dessen Konsole
        # fuehrt beliebigen Python-Code aus. use_reloader=False: der Reloader startet
        # den Prozess doppelt -- nur ein Prozess darf die Kuzu-Datenbank oeffnen.
        app.run(host=host, port=port, use_reloader=False)
    else:
        # Im Add-on: waitress statt Entwicklungsserver.
        serve(app, host=host, port=port, threads=8)
