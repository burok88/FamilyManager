# Copyright (c) 2026. Erstellt via Vibe Coding. Nur für private und nicht-kommerzielle Nutzung.
"""Termine in den Familienkalender schreiben -- ueber Home Assistant.

ontoFam meldet sich nicht selbst bei Google an. Das erledigt die Integration
"Google Calendar" in Home Assistant, am besten mit einem eigenen Google-Konto fuer
ontoFam, dem der Familienkalender freigegeben ist. ontoFam ruft dort nur Aktionen auf,
mit demselben Zugang wie fuer die Erinnerungen:
  anlegen  -- Aktion calendar.create_event (REST)
  finden   -- GET /api/calendars/<kalender> liefert die Eintraege samt uid
  loeschen -- calendar/event/delete ueber die Websocket-Schnittstelle; als Aktion gibt
              es das nicht

Jeder Eintrag traegt am Ende der Beschreibung eine Markierung mit der Termin-ID. An ihr
erkennt ontoFam seine Eintraege wieder: nur sie werden je geloescht oder ersetzt, alles
andere im Familienkalender bleibt unberuehrt. Und der ICS-Import laesst sie aus -- sie
gehoeren schon zu einer Aufgabe in ontoFam.

Aendern kann Home Assistant einen Google-Eintrag nicht. Aendert sich ein Termin in
ontoFam, wird der alte Eintrag geloescht und ein neuer angelegt.
"""

import hashlib
import json
import re
from datetime import date, datetime, timedelta

import requests
from websockets.sync.client import connect

MARKIERUNG = "ontoFam-Termin:"
_MARKIERUNG_RE = re.compile(re.escape(MARKIERUNG) + r"\s*([A-Za-z0-9_\-]+)")
# Ohne Dauer braucht ein Termin mit Uhrzeit trotzdem ein Ende.
STANDARD_DAUER_MINUTEN = 60
# Bit "Eintraege loeschen" in supported_features einer Kalender-Entitaet.
KANN_LOESCHEN = 2


def termin_aus_beschreibung(text: str) -> str | None:
    """Termin-ID aus der Markierung, sonst None."""
    treffer = _MARKIERUNG_RE.search(text or "")
    return treffer.group(1) if treffer else None


def ereignis(eintrag: dict) -> dict:
    """Felder fuer calendar.create_event aus einem Termin samt Aufgabe.

    eintrag: aufgabe, termin_id, datum (ISO), beginn ("HH:MM" oder leer), dauer_minuten,
    ort, beschreibung, personen (zustaendig), betroffene.
    """
    zeilen = [eintrag.get("beschreibung") or ""]
    if eintrag.get("personen"):
        zeilen.append("Zuständig: " + ", ".join(eintrag["personen"]))
    if eintrag.get("betroffene"):
        zeilen.append("Betrifft: " + ", ".join(eintrag["betroffene"]))
    # Die Markierung als letzte Zeile, abgesetzt -- im Kalender stoert sie so am wenigsten.
    beschreibung = "\n".join(z for z in zeilen if z) + f"\n\n{MARKIERUNG} {eintrag['termin_id']}"

    felder = {"summary": eintrag["aufgabe"], "description": beschreibung.strip()}
    if eintrag.get("ort"):
        felder["location"] = eintrag["ort"]
    tag = date.fromisoformat(eintrag["datum"])
    if eintrag.get("beginn"):
        # Ohne Zeitzone: Home Assistant nimmt seine eigene, also die des Haushalts.
        beginn = datetime.combine(tag, datetime.strptime(eintrag["beginn"], "%H:%M").time())
        ende = beginn + timedelta(minutes=eintrag.get("dauer_minuten") or STANDARD_DAUER_MINUTEN)
        felder["start_date_time"] = beginn.strftime("%Y-%m-%d %H:%M:%S")
        felder["end_date_time"] = ende.strftime("%Y-%m-%d %H:%M:%S")
    else:
        # Ganztaegig: das Ende ist ausschliesslich, also der Folgetag.
        felder["start_date"] = tag.isoformat()
        felder["end_date"] = (tag + timedelta(days=1)).isoformat()
    return felder


def fingerabdruck(felder: dict) -> str:
    """Kurzform dessen, was eingetragen wurde. Weicht sie spaeter ab, hat sich der Termin
    in ontoFam geaendert und der Eintrag wird ersetzt. Aenderungen, die jemand direkt im
    Kalender macht, beruehren ihn nicht -- die bleiben stehen."""
    return hashlib.sha1(json.dumps(felder, sort_keys=True).encode()).hexdigest()[:16]


def _kopf(ha: dict) -> dict:
    return {"Authorization": f"Bearer {ha['token']}"}


def kalender_liste(ha: dict) -> list[dict]:
    """Kalender, die Home Assistant kennt -- fuer die Meldung, welcher gemeint sein kann."""
    antwort = requests.get(f"{ha['url'].rstrip('/')}/api/calendars", headers=_kopf(ha), timeout=10)
    antwort.raise_for_status()
    return [{"entity_id": k["entity_id"], "name": k.get("name", "")} for k in antwort.json()]


def kann_loeschen(ha: dict, kalender: str) -> bool:
    antwort = requests.get(f"{ha['url'].rstrip('/')}/api/states/{kalender}", headers=_kopf(ha), timeout=10)
    antwort.raise_for_status()
    return bool(int(antwort.json().get("attributes", {}).get("supported_features") or 0) & KANN_LOESCHEN)


def _beginn_datum(ereignis_ha: dict) -> date | None:
    beginn = ereignis_ha.get("start") or {}
    if isinstance(beginn, dict):
        beginn = beginn.get("dateTime") or beginn.get("date")
    try:
        return date.fromisoformat(str(beginn)[:10])
    except ValueError:
        return None


def eigene_eintraege(ha: dict, kalender: str, von: date, bis: date) -> dict[str, list[str]]:
    """Eintraege mit ontoFam-Markierung, die von..bis beginnen: Termin-ID -> uids.

    Was vor von beginnt, zaehlt nicht, auch wenn es noch hineinragt: Vergangenes bleibt
    im Kalender stehen, wie es ist."""
    antwort = requests.get(
        f"{ha['url'].rstrip('/')}/api/calendars/{kalender}",
        params={"start": f"{von.isoformat()}T00:00:00", "end": f"{bis.isoformat()}T00:00:00"},
        headers=_kopf(ha), timeout=20)
    antwort.raise_for_status()
    gefunden: dict[str, list[str]] = {}
    for ev in antwort.json():
        termin_id = termin_aus_beschreibung(ev.get("description") or "")
        beginn = _beginn_datum(ev)
        if termin_id and ev.get("uid") and beginn and von <= beginn < bis:
            gefunden.setdefault(termin_id, []).append(ev["uid"])
    return gefunden


def trage_ein(ha: dict, kalender: str, felder: dict) -> None:
    """Legt den Eintrag an. Wirft bei Fehlern, damit der Termin nicht als eingetragen gilt."""
    antwort = requests.post(
        f"{ha['url'].rstrip('/')}/api/services/calendar/create_event",
        headers=_kopf(ha), json={"entity_id": kalender, **felder}, timeout=20)
    antwort.raise_for_status()


def _websocket_adresse(ha: dict) -> str:
    url = ha["url"].rstrip("/")
    ws = "ws" + url.removeprefix("http")  # http -> ws, https -> wss
    # Als Add-on ueber den Supervisor (http://supervisor/core), sonst Home Assistant direkt.
    return ws + ("/websocket" if url.endswith("/core") else "/api/websocket")


def loesche(ha: dict, kalender: str, uid: str) -> None:
    """Loescht einen Eintrag. Wirft bei Fehlern."""
    with connect(_websocket_adresse(ha), open_timeout=10, close_timeout=5) as ws:
        if json.loads(ws.recv(timeout=10)).get("type") != "auth_required":
            raise RuntimeError("Unerwartete Begruessung von Home Assistant")
        ws.send(json.dumps({"type": "auth", "access_token": ha["token"]}))
        if json.loads(ws.recv(timeout=10)).get("type") != "auth_ok":
            raise RuntimeError("Home Assistant hat die Anmeldung abgelehnt")
        ws.send(json.dumps({"id": 1, "type": "calendar/event/delete",
                            "entity_id": kalender, "uid": uid}))
        antwort = json.loads(ws.recv(timeout=20))
    if not antwort.get("success"):
        raise RuntimeError(f"Loeschen abgelehnt: {(antwort.get('error') or {}).get('message', antwort)}")


def fehlertext(fehler: Exception) -> str:
    """Verstaendliche Meldung ohne Token. Home Assistant schreibt den Grund in den Text
    der Antwort, z.B. wenn der Kalender nur lesbar eingebunden ist."""
    if isinstance(fehler, requests.HTTPError) and fehler.response is not None:
        grund = (fehler.response.text or "").strip()[:300]
        return f"Home Assistant antwortet mit HTTP {fehler.response.status_code}" + (
            f": {grund}" if grund else "")
    if isinstance(fehler, requests.RequestException):
        return f"Home Assistant nicht erreichbar ({type(fehler).__name__})"
    return f"{type(fehler).__name__}: {fehler}"
