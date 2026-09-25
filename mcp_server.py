"""MCP-Server fuer die Haushalts-Ontologie: macht Ontologie-Abfragen und -Aenderungen
fuer KI-Clients (Claude Desktop, Gemini CLI, Home Assistant) als Tools verfuegbar.

Bewusst ein duenner HTTP-Client ueber die Flask-API (app.py) statt direktem
Kuzu-Zugriff: Kuzu erlaubt nur einen schreibenden Prozess, und der ist app.py.
app.py muss also laufen.

Start:
  python mcp_server.py            # stdio (Claude Desktop, Gemini CLI)
  python mcp_server.py --http     # Streamable HTTP auf http://127.0.0.1:8765/mcp
  python mcp_server.py --http --host 0.0.0.0   # im LAN erreichbar (z.B. Home Assistant)
"""

import argparse
import os

import requests
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

API = os.environ.get("ONTOFAM_API", "http://localhost:5000").rstrip("/")

# Laeuft ontoFam in Home Assistant, will die API den Zugriffsschluessel sehen. Die
# Sitzung schickt ihn bei jeder Anfrage mit; auf dem PC bleibt er leer.
_http = requests.Session()
if os.environ.get("ONTOFAM_SCHLUESSEL"):
    _http.headers["Authorization"] = f"Bearer {os.environ['ONTOFAM_SCHLUESSEL']}"

server = MCPServer(
    name="ontofam",
    instructions=(
        "Haushalts-Ontologie einer Familie: Personen, Aufgaben, Termine, Zustaendigkeiten. "
        "Datumsangaben immer als ISO-Datum (YYYY-MM-DD) uebergeben; relative Angaben wie "
        "'naechsten Dienstag' vorher selbst in ein Datum aufloesen. Personen und Aufgaben "
        "werden ueber ihren Namen angesprochen. Bei einer Anweisung wie 'Max muss Lena am "
        "22.09. zum Hautarzt bringen' ist Max die zustaendige Person und Lena die "
        "betroffene ('betrifft'), die Aufgabe heisst 'Lena zum Hautarzt bringen'. "
        "Nennt jemand einen Ort ausserhalb des Hauses ('im Allgaeu'), gehoert der in "
        "das Feld ort -- nur so ist erkennbar, dass jemand hinfahren muss. "
        "Sagt jemand, dass etwas erledigt ist ('Muell ist draussen', 'Rasen ist "
        "gemaeht'), das mit als_erledigt_markieren festhalten. "
        "Gibt der Nutzer einen Stundenplan (PDF, Foto), den Plan mit "
        "stundenplan_setzen komplett uebernehmen -- nicht Stunde fuer Stunde. "
        "Fuer Fragen, die keines der "
        "fertigen Werkzeuge abdeckt, zuerst schema_beschreiben aufrufen und dann "
        "mit graph_abfragen eine eigene Cypher-Abfrage stellen."
    ),
)


def _get(path: str, **params):
    r = _http.get(f"{API}{path}", params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def _post(path: str, body: dict):
    r = _http.post(f"{API}{path}", json=body, timeout=60)
    if not r.ok:
        raise RuntimeError(r.json().get("error", r.text))
    return r.json()


def _finde(items: list[dict], name: str, was: str) -> dict:
    gesucht = name.strip().lower()
    vorhanden = ", ".join(i["name"] for i in items) or "(keine)"
    for kandidaten in (
        [i for i in items if i["name"].strip().lower() == gesucht],
        [i for i in items if gesucht in i["name"].lower()],
        # Langform, wie sie auf Dokumenten steht: "Nicolas" im Stundenplan fuer die
        # Person "Nico". Erst nach exakt und Teilwort, damit die Kurzform gewinnt.
        [i for i in items if i["name"].strip().lower() in gesucht],
    ):
        if len(kandidaten) == 1:
            return kandidaten[0]
        if len(kandidaten) > 1:
            raise ValueError(f"{was} '{name}' ist mehrdeutig. Vorhanden: {vorhanden}")
    raise ValueError(f"{was} '{name}' nicht gefunden. Vorhanden: {vorhanden}")


@server.tool()
def personen_auflisten() -> list[dict]:
    """Alle Personen des Haushalts mit Rolle."""
    return _get("/api/personen")


@server.tool()
def person_anlegen(name: str, rolle: str = "Sonstiges") -> dict:
    """Neue Person anlegen. rolle: Elternteil, Kind oder Sonstiges."""
    return _post("/api/personen", {"name": name, "rolle": rolle})


@server.tool()
def aufgaben_auflisten() -> list[dict]:
    """Alle Aufgaben mit Ort, Prozess und Kategorie (z.B. 'Kalender-Import')."""
    return _get("/api/aufgaben")


@server.tool()
def anstehende_aufgaben(tage: int = 7, person: str | None = None) -> dict:
    """Was steht in den naechsten `tage` Tagen an? Optional auf eine Person gefiltert.
    Liefert pro Termin Aufgabe, Datum, Tage bis dahin, Kontext (z.B. Tonnenart) und
    zustaendige Personen sowie status: 'offen', 'erledigt' (mit erledigt_von) oder
    'entfaellt'. Kalendertermine sind automatisch aktuell. Ist kalender_fehler
    gesetzt, war Google zuletzt nicht erreichbar -- dann dem Nutzer sagen, dass
    Kalendertermine auf dem Stand von kalender_stand sind."""
    params = {"tage": tage}
    if person:
        params["person"] = person
    return _get("/api/anstehend", **params)


@server.tool()
def aufgabe_anlegen(
    name: str,
    datum: str,
    person: str | None = None,
    betrifft: str | None = None,
    ort: str = "",
    uhrzeit: str = "",
    dauer_minuten: int = 0,
    beschreibung: str = "",
    vorlaufzeit_tage: int = 1,
) -> dict:
    """Neue einmalige Aufgabe mit Termin anlegen.

    datum: YYYY-MM-DD. uhrzeit: HH:MM, leer lassen bei ganztaegigen Terminen.
    person: wer die Aufgabe erledigt. betrifft: um wen es geht -- bei "Max bringt
    Lena zum Hautarzt" ist person=Max und betrifft=Lena.
    ort: wo der Termin stattfindet, z.B. "Allgaeu" -- wichtig, damit erkennbar ist,
    dass jemand hinfahren muss. dauer_minuten: wie lange der Termin dauert.
    vorlaufzeit_tage: wie viele Tage vorher erinnert werden soll."""
    # Beide Personen zuerst aufloesen, damit bei einem Tippfehler nichts Halbes
    # angelegt wird.
    try:
        personen = _get("/api/personen")
        p = _finde(personen, person, "Person") if person else None
        b = _finde(personen, betrifft, "Person") if betrifft else None
    except ValueError as e:
        return {"fehler": str(e)}

    aufgabe = _post("/api/aufgaben", {"name": name, "beschreibung": beschreibung, "kategorie": "Einmalig"})
    try:
        termin = _post("/api/aufgabe-termine", {
            "aufgabe_id": aufgabe["id"], "datum": datum, "ort": ort,
            "beginn": uhrzeit, "dauer_minuten": dauer_minuten,
        })
    except RuntimeError as e:
        # Termin abgelehnt (z.B. unlesbare Uhrzeit): die leere Aufgabe wieder
        # entfernen, sonst bleibt eine Aufgabe ohne Datum zurueck.
        _http.delete(f"{API}/api/aufgaben/{aufgabe['id']}", timeout=15)
        return {"fehler": str(e)}

    ergebnis = {"aufgabe": name, "aufgabe_id": aufgabe["id"], "datum": termin["datum"],
                "uhrzeit": termin["beginn"], "ort": termin["ort"],
                "dauer_minuten": termin["dauer_minuten"]}
    if p:
        _post("/api/zustaendigkeiten", {
            "person_id": p["id"], "aufgabe_id": aufgabe["id"], "vorlaufzeit_tage": vorlaufzeit_tage,
        })
        ergebnis["erledigt_von"] = p["name"]
        ergebnis["vorlaufzeit_tage"] = vorlaufzeit_tage
    if b:
        _post("/api/betrifft", {"aufgabe_id": aufgabe["id"], "person_id": b["id"]})
        ergebnis["betrifft"] = b["name"]
    return ergebnis


@server.tool()
def betroffene_person_setzen(aufgabe: str, person: str) -> dict:
    """Festhalten, um wen es bei einer bestehenden Aufgabe geht (BETRIFFT).
    Unterscheidet sich von zustaendigkeit_setzen: das ist, wer die Aufgabe erledigt."""
    try:
        a = _finde(_get("/api/aufgaben"), aufgabe, "Aufgabe")
        p = _finde(_get("/api/personen"), person, "Person")
    except ValueError as e:
        return {"fehler": str(e)}
    _post("/api/betrifft", {"aufgabe_id": a["id"], "person_id": p["id"]})
    return {"aufgabe": a["name"], "betrifft": p["name"]}


@server.tool()
def stundenplan_abfragen(person: str | None = None, wochentag: str | None = None) -> list[dict]:
    """Stundenplan einer Person (oder aller), optional auf einen Wochentag gefiltert.
    Liefert Wochentag, Stundennummer, Fach, Raum und Uhrzeiten."""
    stunden = _get("/api/schulstunden", **({"person": person} if person else {}))
    if wochentag:
        gesucht = wochentag.strip().lower()
        stunden = [s for s in stunden if s["wochentag"].lower() == gesucht]
    return stunden


class Schulstunde(BaseModel):
    """Eine belegte Zelle im Stundenplan-Raster."""

    wochentag: str = Field(description="Montag bis Sonntag, ausgeschrieben")
    stunde: int = Field(description="Stundennummer aus dem Raster (1, 2, 3 ...). "
                                    "0 fuer Eintraege ohne Nummer, z.B. Betreuung")
    fach: str = Field(description="Fach wie im Plan; eindeutige Kuerzel ausschreiben "
                                  "(D -> Deutsch, M -> Mathematik)")
    raum: str = Field(default="", description="Raum wie im Plan")
    lehrer: str = Field(default="", description="Lehrkraft oder Kuerzel wie im Plan")
    beginn: str = Field(default="", description="Beginn HH:MM aus dem Zeitraster")
    ende: str = Field(default="", description="Ende HH:MM aus dem Zeitraster")
    woche: str = Field(default="", description="Leer = jede Woche. 'A' oder 'B' nur bei "
                                               "zweiwoechigem Rhythmus")


@server.tool()
def stundenplan_setzen(
    person: str,
    stunden: list[Schulstunde],
    quelle: str = "",
    dokument_titel: str = "",
    dokument_text: str = "",
) -> dict:
    """Kompletten Stundenplan einer Person setzen, z.B. aus einem hochgeladenen PDF.
    ERSETZT den bisherigen Plan dieser Person vollstaendig.

    Vorgehen bei einem Stundenplan (PDF oder Foto):
    1. Person bestimmen -- steht meist im Kopf des Plans, sonst nachfragen.
       Langformen werden erkannt ('Nicolas' findet 'Nico').
    2. Jede belegte Zelle des Rasters ist ein Eintrag. Doppelstunden sind zwei
       Eintraege mit aufeinanderfolgender Stundennummer. Pausen weglassen,
       Nachmittagsbetreuung und AGs mit aufnehmen.
    3. Uhrzeiten aus dem Zeitraster (meist die linke Spalte) jeder Stunde zuordnen.
    4. Bei A/B-Wochen das Feld woche setzen; was jede Woche stattfindet, bleibt leer.
    5. VOR dem Aufruf dem Nutzer die erkannte Woche als kompakte Tabelle zeigen und
       bestaetigen lassen -- Lesefehler lassen sich so noch korrigieren.

    quelle: Dateiname, z.B. 'Stundenplan Nico 7b.pdf'.
    dokument_text: alles vom Plan, das nicht in die Felder passt -- Schule, Klasse,
    Schuljahr/Halbjahr, Klassenlehrer, Hinweise. Wird als Dokument an die Person
    gehaengt und bleibt spaeter nachlesbar.

    Alles wird geprueft, bevor etwas gespeichert wird. Bei Problemen kommt
    {"fehler": [...]} mit allen Problemen auf einmal zurueck und nichts wurde
    geschrieben: korrigieren und erneut senden. Bei Erfolg kommt pro Tag die Zahl
    der Stunden und die Uhrzeit von erster bis letzter Stunde zurueck."""
    try:
        p = _finde(_get("/api/personen"), person, "Person")
    except ValueError as e:
        return {"fehler": str(e)}

    r = _http.put(f"{API}/api/stundenplan/{p['id']}", json={
        "stunden": [s.model_dump() for s in stunden],
        "quelle": quelle,
        "dokument": {"titel": dokument_titel, "text": dokument_text},
    }, timeout=60)
    daten = r.json()
    if not r.ok:
        return {"fehler": daten.get("fehler") or daten.get("error")}
    return daten


@server.tool()
def stundenplan_eintragen(
    person: str,
    wochentag: str,
    stunde: int,
    fach: str,
    raum: str = "",
    lehrer: str = "",
    beginn: str = "",
    ende: str = "",
    woche: str = "",
) -> dict:
    """Eine einzelne Schulstunde nachtragen -- fuer Korrekturen. Einen ganzen Plan
    mit stundenplan_setzen uebernehmen.
    wochentag: Montag bis Sonntag. stunde: Stundennummer (1, 2, 3 ...).
    beginn/ende als HH:MM. woche: leer = jede Woche, sonst 'A' oder 'B'."""
    try:
        p = _finde(_get("/api/personen"), person, "Person")
        return _post("/api/schulstunden", {
            "person_id": p["id"], "wochentag": wochentag, "stunde": stunde,
            "fach": fach, "raum": raum, "lehrer": lehrer,
            "beginn": beginn, "ende": ende, "woche": woche,
        })
    except (ValueError, RuntimeError) as e:
        return {"fehler": str(e)}


@server.tool()
def stundenplan_loeschen(person: str) -> dict:
    """Den kompletten Stundenplan einer Person loeschen -- z.B. beim Halbjahreswechsel,
    bevor der neue eingetragen wird."""
    try:
        p = _finde(_get("/api/personen"), person, "Person")
    except ValueError as e:
        return {"fehler": str(e)}
    r = _http.delete(f"{API}/api/schulstunden/person/{p['id']}", timeout=15)
    r.raise_for_status()
    return {"person": p["name"], **r.json()}


def _mit_id(pfad: str, name: str, was: str) -> dict:
    return _finde(_get(pfad), name, was)


@server.tool()
def als_erledigt_markieren(
    aufgabe: str,
    datum: str,
    person: str | None = None,
    status: str = "erledigt",
    notiz: str = "",
    ressource: str | None = None,
) -> dict:
    """Festhalten, dass eine Aufgabe an einem Termin erledigt wurde -- z.B. "Anna hat
    den Muell rausgebracht", "Rasen ist gemaeht", "Lena war beim Zahnarzt".

    datum: das Datum des TERMINS (YYYY-MM-DD), nicht zwingend heute. Beim Muell ist das
    der Abholtag -- Tonnen werden meist am Vorabend rausgestellt, "Muell ist draussen"
    am Abend meint also den Termin von morgen.
    person: wer es erledigt hat, falls bekannt (sonst weglassen, nicht raten).
    status: 'erledigt' oder 'entfaellt' (fand nicht statt, war nicht noetig).
    ressource: nur eine bestimmte Tonne; ohne Angabe gilt es fuer alle Termine der
    Aufgabe an diesem Tag.

    Hat die Aufgabe an dem Datum keinen Termin, kommen die Termine der Woche davor und
    danach zurueck (termine_in_der_naehe) -- dann den passenden waehlen und erneut
    aufrufen, im Zweifel nachfragen."""
    try:
        a = _mit_id("/api/aufgaben", aufgabe, "Aufgabe")
        p = _mit_id("/api/personen", person, "Person") if person else None
        r = _mit_id("/api/ressourcen", ressource, "Ressource") if ressource else None
    except ValueError as e:
        return {"fehler": str(e)}
    antwort = _http.post(f"{API}/api/erledigungen", json={
        "aufgabe_id": a["id"], "datum": datum, "status": status, "notiz": notiz,
        "person_id": p["id"] if p else "", "ressource_id": r["id"] if r else "",
    }, timeout=30)
    daten = antwort.json()
    if not antwort.ok:
        return {"fehler": daten.get("error"), **{k: v for k, v in daten.items() if k != "error"}}
    return daten


@server.tool()
def erledigung_zuruecksetzen(aufgabe: str, datum: str) -> dict:
    """Eine versehentlich festgehaltene Erledigung zuruecknehmen. Der Termin gilt danach
    wieder als offen (oder als verpasst, wenn sein Tag vorbei ist)."""
    try:
        a = _mit_id("/api/aufgaben", aufgabe, "Aufgabe")
    except ValueError as e:
        return {"fehler": str(e)}
    antwort = _http.delete(f"{API}/api/erledigungen",
                              json={"aufgabe_id": a["id"], "datum": datum}, timeout=30)
    return antwort.json() if antwort.ok else {"fehler": antwort.json().get("error")}


@server.tool()
def rueckblick(tage: int = 7, person: str | None = None) -> dict:
    """Was in den letzten `tage` Tagen anstand und was daraus wurde: erledigt (mit wer),
    entfaellt, verpasst oder -- fuer heute -- noch offen. 'unbekannt' heisst: der Termin
    lag vor Beginn der Erfassung, ob er erledigt wurde, weiss niemand. Mit Zusammenfassung.
    person filtert auf alles, wofuer die Person zustaendig war, worum es bei ihr ging
    oder was sie erledigt hat."""
    params = {"tage": tage}
    if person:
        params["person"] = person
    return _get("/api/rueckblick", **params)


@server.tool()
def erinnerungen_vorschau(tage: int = 2) -> dict:
    """Welche Erinnerungen in den naechsten `tage` Tagen an wen gehen (mit Text und
    Kanal), was zuletzt gesendet wurde und wie die Zeiten eingestellt sind.
    Erinnerungen laufen automatisch: abends vor dem Termin (je nach Vorlaufzeit der
    zustaendigen Person) und bei Terminen mit Uhrzeit ein paar Stunden vorher.
    Kanal 'protokoll' heisst: noch keine Zustellung aufs Handy eingerichtet."""
    return _get("/api/erinnerungen", tage=tage)


@server.tool()
def erinnerung_testen(person: str) -> dict:
    """Einer Person eine Testnachricht schicken, um die Zustellung zu pruefen."""
    try:
        p = _mit_id("/api/personen", person, "Person")
    except ValueError as e:
        return {"fehler": str(e)}
    antwort = _http.post(f"{API}/api/erinnerungen/testen", json={"person": p["name"]}, timeout=30)
    daten = antwort.json()
    return daten if antwort.ok else {"fehler": daten.get("error"), "kanal": daten.get("kanal")}


@server.tool()
def zustaendigkeit_setzen(person: str, aufgabe: str, vorlaufzeit_tage: int = 1) -> dict:
    """Eine bestehende Aufgabe einer Person zuordnen (oder die Vorlaufzeit aendern).
    Ersetzt eine bestehende Zuordnung dieses Paars."""
    try:
        p = _finde(_get("/api/personen"), person, "Person")
        a = _finde(_get("/api/aufgaben"), aufgabe, "Aufgabe")
    except ValueError as e:
        return {"fehler": str(e)}
    _post("/api/zustaendigkeiten", {
        "person_id": p["id"], "aufgabe_id": a["id"], "vorlaufzeit_tage": vorlaufzeit_tage,
    })
    return {"person": p["name"], "aufgabe": a["name"], "vorlaufzeit_tage": vorlaufzeit_tage}


@server.tool()
def schema_beschreiben() -> str:
    """Aufbau des Graphen: Knotentypen, Kantentypen und Beispielabfragen.
    Vor der ersten freien Abfrage mit graph_abfragen einmal aufrufen."""
    return _get("/api/schema")["text"]


@server.tool()
def graph_abfragen(cypher: str) -> dict:
    """Freie, nur lesende Cypher-Abfrage auf der Ontologie -- fuer alles, wofuer es
    kein eigenes Werkzeug gibt, z.B. 'wer hat die meisten Aufgaben?' oder
    'welche Aufgaben haengen am Garten?'. Vorher schema_beschreiben aufrufen.

    Schreibende Abfragen (CREATE, DELETE, SET ...) werden abgewiesen -- zum Anlegen
    von Daten die dafuer vorgesehenen Werkzeuge benutzen. Bei einem Fehler kommt
    {"fehler": ...} zurueck: Abfrage anhand der Meldung korrigieren und erneut senden."""
    try:
        return _post("/api/query", {"cypher": cypher})
    except RuntimeError as e:
        return {"fehler": str(e)}


@server.tool()
def kalender_synchronisieren(neu_aufbauen: bool = False) -> dict:
    """Termine aus den konfigurierten Google-Kalendern (ICS) sofort abgleichen.

    Normalerweise NICHT noetig: der Server gleicht selbst ab, vor jeder Abfrage, deren
    letzter Abgleich laenger als eine Minute zurueckliegt, und sonst alle 15 Minuten.
    Nur aufrufen, wenn der Nutzer es ausdruecklich moechte.

    Es ist ein Abgleich: bestehende Aufgaben bleiben erhalten, sodass zugeordnete
    Personen und Betroffene den Sync ueberleben. Was im Kalender nicht mehr
    vorkommt, wird entfernt.

    neu_aufbauen=True wirft den gesamten Importstand weg und legt ihn neu an --
    dabei gehen alle manuellen Zuordnungen an importierten Aufgaben verloren.
    Nur benutzen, wenn der Nutzer das ausdruecklich moechte."""
    return _post("/api/kalender/sync", {"neu_aufbauen": neu_aufbauen})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--http", action="store_true", help="Streamable HTTP statt stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    if args.http:
        server.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        server.run(transport="stdio")
