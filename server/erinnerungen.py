"""Erinnerungen: wer wann woran erinnert wird -- und wie die Nachricht ankommt.

Die Planung ist reine Logik ohne Datenbank. Sie bekommt die Termine (so, wie
app._termine_im_zeitraum sie liefert) und die Rollen der Personen und rechnet aus,
welche Erinnerung wann faellig ist. Dadurch laesst sie sich mit festen Uhrzeiten pruefen.

Zwei Arten:
  vorlauf -- am Tag "Termin minus Vorlaufzeit": abends ("Morgen: Papiertonne"), bei
             Vorlaufzeit 0 morgens am Tag selbst ("Heute: ...")
  bald    -- bei Terminen mit Uhrzeit ein paar Stunden vorher ("In 2 Stunden: ...")

Wer: die Zustaendigen, jeweils mit ihrer Vorlaufzeit. Ohne Zustaendige die betroffenen
Erwachsenen, sonst alle Elternteile -- ein Elternabend, fuer den niemand eingetragen
ist, soll nicht unbemerkt vorbeigehen. Kinder werden nur erinnert, wenn sie selbst
zustaendig sind.

Nicht erinnert wird an Erledigtes, Entfallenes und an nichts, das schon begonnen hat.
"""

import json
import os
from datetime import date, datetime, time, timedelta
from pathlib import Path

import requests

from schema import WOCHENTAGE

EINSTELLUNGEN_PATH = Path(os.environ.get("ONTOFAM_DATEN", Path(__file__).resolve().parent.parent)) / "erinnerungen.json"
STANDARD = {
    "abend": "19:00",
    "morgen": "07:00",
    "stunden_vorher": 2,
    "homeassistant": None,
    "empfaenger": {},
}
# Vorlaufzeit fuer Personen, die nicht zustaendig sind, aber trotzdem erinnert werden.
STANDARD_VORLAUF = 1


def lade_einstellungen(pfad: Path = EINSTELLUNGEN_PATH) -> dict:
    """erinnerungen.json (nicht in Git, enthaelt das Home-Assistant-Token) ueber die
    Standardwerte legen. Fehlt die Datei, gehen Erinnerungen ins Serverprotokoll."""
    einstellungen = dict(STANDARD)
    if pfad.exists():
        einstellungen.update(json.loads(pfad.read_text(encoding="utf-8")))
    return einstellungen


def _uhrzeit(text: str) -> time:
    return datetime.strptime(text, "%H:%M").time()


def _beginnt(eintrag: dict) -> datetime | None:
    if not eintrag["beginn"]:
        return None
    return datetime.combine(date.fromisoformat(eintrag["datum"]), _uhrzeit(eintrag["beginn"]))


def _relevant_bis(eintrag: dict) -> datetime:
    """Bis wann eine Erinnerung noch Sinn ergibt: bis zum Beginn, bei ganztaegigen
    Terminen bis zum Ende des Tages."""
    return _beginnt(eintrag) or datetime.combine(
        date.fromisoformat(eintrag["datum"]) + timedelta(days=1), time.min)


def empfaenger(eintrag: dict, rollen: dict[str, str]) -> dict[str, int]:
    """Wer erinnert wird, jeweils mit Vorlaufzeit in Tagen."""
    if eintrag["personen"]:
        vorlauf = eintrag.get("vorlauf") or {}
        return {name: vorlauf[name] if vorlauf.get(name) is not None else STANDARD_VORLAUF
                for name in eintrag["personen"]}
    erwachsene = [name for name, rolle in rollen.items() if rolle == "Elternteil"]
    betroffene = [name for name in eintrag["betroffene"] if name in erwachsene]
    return {name: STANDARD_VORLAUF for name in (betroffene or erwachsene)}


def plane(eintraege: list[dict], rollen: dict[str, str], einstellungen: dict) -> list[dict]:
    """Alle Erinnerungen zu diesen Terminen, mit Faelligkeit. Erledigtes faellt weg."""
    plan = []
    for eintrag in eintraege:
        if eintrag["status"] in ("erledigt", "entfaellt"):
            continue
        beginnt = _beginnt(eintrag)
        relevant_bis = _relevant_bis(eintrag)
        tag = date.fromisoformat(eintrag["datum"])
        for person, vorlauf in empfaenger(eintrag, rollen).items():
            bald = None
            if beginnt:
                bald = beginnt - timedelta(hours=einstellungen["stunden_vorher"])
                plan.append(_erinnerung("bald", person, eintrag, bald, relevant_bis))
            uhr = einstellungen["morgen"] if vorlauf == 0 else einstellungen["abend"]
            faellig = datetime.combine(tag - timedelta(days=vorlauf), _uhrzeit(uhr))
            # Kaeme die Vorlauf-Erinnerung erst nach der "bald"-Erinnerung, waere sie
            # ueberfluessig (z.B. Vorlauf 0 und Termin um 08:00).
            if bald is None or faellig < bald:
                plan.append(_erinnerung("vorlauf", person, eintrag, faellig, relevant_bis))
    plan.sort(key=lambda x: (x["faellig"], x["person"]))
    return plan


def _erinnerung(art: str, person: str, eintrag: dict, faellig: datetime,
                relevant_bis: datetime) -> dict:
    return {
        # Je Art, Person, Aufgabe und Termin genau eine Erinnerung. Aendert sich die
        # Uhrzeit eines Kalendertermins, aendert sich seine ID -- dann wird neu erinnert.
        "id": f"{art}|{person}|{eintrag['aufgabe_id']}|{eintrag['termin_id']}",
        "art": art,
        "person": person,
        "faellig": faellig,
        "relevant_bis": relevant_bis,
        "eintrag": eintrag,
    }


def faellige(plan: list[dict], jetzt: datetime) -> list[dict]:
    """Was jetzt dran ist. Auch Verpasstes wird nachgeholt, solange der Termin noch
    nicht begonnen hat -- lief der Rechner um 19 Uhr nicht, kommt die Erinnerung eben
    beim naechsten Start.

    Sind fuer denselben Termin Vorabend- und "bald"-Erinnerung zugleich faellig (der
    Rechner war am Vorabend aus), gewinnt die genauere. Die andere wird als "ueberholt"
    markiert: sie wird nicht gesendet, muss aber als erledigt verbucht werden -- sonst
    kaeme sie eine Minute spaeter allein hinterher.
    """
    dran = [dict(x, ueberholt=False) for x in plan if x["faellig"] <= jetzt < x["relevant_bis"]]
    bald = {(x["person"], x["eintrag"]["termin_id"]) for x in dran if x["art"] == "bald"}
    for x in dran:
        if x["art"] == "vorlauf" and (x["person"], x["eintrag"]["termin_id"]) in bald:
            x["ueberholt"] = True
    return dran


def _wann(tag: date, heute: date) -> str:
    tage = (tag - heute).days
    if tage == 0:
        return "Heute"
    if tage == 1:
        return "Morgen"
    if tage == 2:
        return "Übermorgen"
    if 0 < tage < 7:
        return f"Am {WOCHENTAGE[tag.weekday()]}"
    return f"Am {tag:%d.%m.}"


def _zeile(eintrag: dict, person: str) -> str:
    text = eintrag["aufgabe"]
    if eintrag["kontext"]:
        text += f" ({eintrag['kontext']})"
    if eintrag["beginn"]:
        text += f" um {eintrag['beginn']}"
    if eintrag["ort"]:
        text += f" in {eintrag['ort']}"
    # "fuer Lena" nur, wenn es nicht ohnehin im Titel steht ("Zahnarzt Lena").
    andere = [n for n in eintrag["betroffene"] if n != person and n not in eintrag["aufgabe"]]
    if andere:
        text += f" (für {', '.join(andere)})"
    return text


def nachricht(gruppe: list[dict], jetzt: datetime) -> tuple[str, str]:
    """Eine Nachricht je Person und Anlass, eine Zeile je Termin. Relative Angaben
    ("Morgen", "In 2 Stunden") werden beim Senden berechnet, nicht beim Planen --
    eine nachgeholte Erinnerung soll nicht "Morgen" sagen, wenn es schon heute ist."""
    zeilen = []
    for x in gruppe:
        if x.get("ueberholt"):
            continue
        eintrag = x["eintrag"]
        if x["art"] == "bald":
            minuten = max(1, int((x["relevant_bis"] - jetzt).total_seconds() // 60))
            wann = f"In {round(minuten / 60)} Stunden" if minuten >= 90 else f"In {minuten} Minuten"
        else:
            wann = _wann(date.fromisoformat(eintrag["datum"]), jetzt.date())
        zeilen.append(f"{wann}: {_zeile(eintrag, x['person'])}")
    return "ontoFam", "\n".join(zeilen)


def _homeassistant(einstellungen: dict) -> dict | None:
    ha = einstellungen.get("homeassistant") or {}
    # Als Add-on: Home Assistant gibt jedem Add-on mit homeassistant_api ein eigenes
    # Token und erreicht sich selbst ueber den Supervisor. Kein langlebiges Token noetig.
    if not ha and os.environ.get("SUPERVISOR_TOKEN"):
        ha = {"url": "http://supervisor/core", "token": os.environ["SUPERVISOR_TOKEN"]}
    token = ha.get("token") or ""
    # Die Beispieldatei enthaelt einen Platzhalter -- der zaehlt nicht als eingerichtet.
    if not ha.get("url") or not token or token.startswith("HIER"):
        return None
    return ha


def kanal_fuer(person: str, einstellungen: dict) -> str:
    ziel = (einstellungen.get("empfaenger") or {}).get(person)
    if ziel and _homeassistant(einstellungen):
        return f"homeassistant:{ziel.removeprefix('notify.')}"
    return "protokoll"


def sende(person: str, titel: str, text: str, einstellungen: dict) -> str:
    """Stellt die Nachricht zu und gibt den benutzten Kanal zurueck. Wirft bei Fehlern,
    damit die Erinnerung nicht als gesendet gilt.

    Richtung ontoFam -> Home Assistant: ontoFam muss dafuer im Heimnetz nicht
    erreichbar sein. Das Token steht im Kopf der Anfrage, nie in der Adresse -- so
    taucht es auch in keiner Fehlermeldung auf.
    """
    kanal = kanal_fuer(person, einstellungen)
    if kanal == "protokoll":
        print(f"[Erinnerung an {person}] {titel}: {text}".replace("\n", " | "), flush=True)
        return kanal
    ha = _homeassistant(einstellungen)
    dienst = kanal.split(":", 1)[1]
    antwort = requests.post(
        f"{ha['url'].rstrip('/')}/api/services/notify/{dienst}",
        headers={"Authorization": f"Bearer {ha['token']}"},
        json={"title": titel, "message": text},
        timeout=10,
    )
    antwort.raise_for_status()
    return kanal
