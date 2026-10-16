# ontoFam

Haushalts-Ontologie der Familie: Personen, Aufgaben, Termine, Stundenpläne und
Erinnerungen. Die Oberfläche steht in der Seitenleiste, Claude Desktop greift über
Port 5000 mit dem Zugriffsschlüssel zu.

## Einrichten

1. **Konfiguration:** Kalender (Name und private iCal-Adresse), Zugriffsschlüssel und
   die Empfänger der Erinnerungen eintragen, speichern. Optional **In diesen Kalender
   eintragen** (siehe unten).
2. **Info:** „In Seitenleiste anzeigen“ einschalten, starten.
3. Das Protokoll zeigt, welche Kalender und Empfänger ontoFam erkannt hat — ohne die
   geheimen Adressen.

## Assist mit Gemini (oder einem anderen KI-Agenten)

Mit der Option **Werkzeuge für Assist** (Standard: an) startet ontoFam einen MCP-Server,
den nur Home Assistant selbst erreicht. Die Adresse steht beim Start im Protokoll, z.B.
`Werkzeuge fuer Assist: … http://abcd1234-ontofam:8765/sse`.

1. **Einstellungen → Geräte & Dienste → Integration hinzufügen → „Model Context
   Protocol“**, die Adresse aus dem Protokoll eintragen, ohne OAuth.
2. **Integration „Google Generative AI“** hinzufügen (API-Schlüssel aus Google AI Studio).
3. Beim Gemini-Gesprächsagenten unter **Konfigurieren**: bei „Home Assistant steuern“ die
   ontoFam-Werkzeuge auswählen (zusätzlich zu „Assist“, wenn Gemini auch Geräte steuern
   soll) und die Anweisungen unten einfügen.
4. **Einstellungen → Sprachassistenten → Assistent hinzufügen**, Gesprächsagent Gemini,
   Sprache Deutsch. In der Companion-App diesen Assistenten wählen.

Anweisungen für den Agenten (Vorlagen wie `{{ now() }}` wertet Home Assistant aus):

```
Heute ist {{ ['Montag','Dienstag','Mittwoch','Donnerstag','Freitag','Samstag','Sonntag'][now().weekday()] }}, der {{ now().strftime('%d.%m.%Y') }}, {{ now().strftime('%H:%M') }} Uhr.
Du hilfst unserer Familie mit ontoFam, unserer Haushalts-Ontologie: Personen, Aufgaben,
Termine, Zuständigkeiten, Stundenpläne. Nutze dafür die ontoFam-Werkzeuge.
- Datumsangaben immer als ISO-Datum (JJJJ-MM-TT) übergeben; "morgen", "nächsten Dienstag"
  vorher selbst ausrechnen.
- Personen und Aufgaben über ihren Namen ansprechen.
- "Max muss Lena am 22.09. zum Hautarzt bringen": Max ist zuständig (person), Lena
  betroffen (betrifft), die Aufgabe heißt "Lena zum Hautarzt bringen".
- Ein Ort außerhalb des Hauses ("im Allgäu") gehört ins Feld ort.
- Sagt jemand, dass etwas erledigt ist ("Müll ist draußen"), mit als_erledigt_markieren
  festhalten.
- Mülltermine mit tonnentermine_setzen eintragen, ein Aufruf pro Tonne mit allen Terminen,
  nicht als einzelne Aufgaben.
- Neue Termine kommen von selbst in den Familienkalender; soll etwas nicht hinein,
  aus_kalender_entfernen.
- Für Fragen, die kein Werkzeug abdeckt: erst schema_beschreiben, dann graph_abfragen.
- Ist etwas unklar (welche Person, welcher Tag), kurz nachfragen; sonst direkt ausführen.
- Antworte kurz und auf Deutsch, die Antwort wird oft vorgelesen.
```

Das Gespräch mit Gemini läuft über Google. Mit einem API-Schlüssel ohne Abrechnung darf
Google die Inhalte laut seinen Bedingungen zur Verbesserung seiner Produkte verwenden —
außer für Nutzer in der EU, der Schweiz und Großbritannien: dort gelten auch ohne
Abrechnung die Datenregeln der kostenpflichtigen Nutzung.

## In den Familienkalender eintragen

ontoFam trägt neue Termine von selbst in einen Kalender ein, den Home Assistant kennt,
und zieht Änderungen nach. Die Anmeldung bei Google übernimmt Home Assistant — am
sichersten mit einem eigenen Google-Konto für ontoFam statt dem eigenen.

1. **Google-Konto für ontoFam** anlegen. Im eigenen Google-Kalender den Familienkalender
   für dieses Konto freigeben (Einstellungen des Kalenders → *Für bestimmte Personen
   freigeben*) mit **„Änderungen an Terminen vornehmen“** — nicht „Änderungen vornehmen
   und Freigaben verwalten“. Im ontoFam-Konto die Einladung annehmen.
2. **Integration „Google Calendar“** einrichten und dabei mit dem **ontoFam-Konto**
   anmelden (Anleitung in der Integration: Projekt in der Google Cloud Console, Calendar
   API aktivieren, OAuth-Client vom Typ *Webanwendung*). Die App unter *Zielgruppe*
   veröffentlichen, sonst läuft die Anmeldung alle 7 Tage ab. **Lese- und
   Schreibzugriff** wählen.
3. Unter **Entwicklerwerkzeuge → Zustände** den Familienkalender suchen, z.B.
   `calendar.familie`. Das Attribut `supported_features` sollte mindestens 3 sein
   (anlegen und löschen).
4. In der Konfiguration von ontoFam bei **In diesen Kalender eintragen** die Kennung
   eintragen, speichern, App neu starten.

ontoFam löscht oder ersetzt nur Einträge, die es selbst angelegt hat (Markierung
`ontoFam-Termin:` am Ende der Beschreibung). Müll- und importierte Termine trägt es nie
ein. Was jemand direkt im Kalender verschiebt oder löscht, bleibt so. Fehler stehen im
Protokoll und werden beim nächsten Lauf nachgeholt.

## Datenbank

Die Datenbank liegt in `addon_configs/<kennung>_ontofam/kuzu_db` (Samba-Freigabe). Zum
Ersetzen die App **stoppen**, `kuzu_db` überschreiben, eine vorhandene
`kuzu_db.wal` löschen, wieder starten. Backups von Home Assistant enthalten die
Datenbank; die App wird dafür kurz gestoppt.

## Fehler

„Der Prozessor der VM kennt Befehle nicht …“: In Proxmox die VM → Hardware →
Prozessoren → Typ auf `host` stellen und die VM neu starten.
