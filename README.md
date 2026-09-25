# Haushalts-Ontologie (Graphdatenbank)

Ontologie für Struktur, Prozesse, Personen und Aufgaben im Haushalt — mit einem echten
Müllkalender als Beispiel. Details zum Datenmodell: [ontologie-design.md](ontologie-design.md).

> **Nur für private, nicht-kommerzielle Nutzung.** Jede kommerzielle Nutzung ist strikt
> untersagt. Das Projekt ist *source-available*, nicht Open Source — siehe [Lizenz](#lizenz).

## Setup

```bash
pip install -r requirements.txt mcp pydantic
python setup_schema.py   # NUR für den Neuanfang: löscht die Datenbank
python load_data.py      # lädt Beispieldaten + Müllkalender-Quelldaten
```

Für eine bestehende Datenbank ist `setup_schema.py` **nicht** nötig: `app.py` führt beim
Start die Migration aus `schema.py` aus. Alle Anweisungen dort sind idempotent
(`CREATE ... IF NOT EXISTS`, `ALTER TABLE ... ADD IF NOT EXISTS`), neue Tabellen und
Felder kommen also ohne Datenverlust dazu. Was neu angelegt wurde, steht beim Start im
Log.

## Live-Modellierung (Web-App)

```bash
python app.py             # startet lokal auf http://localhost:5000
```

Web-Oberfläche zum Anlegen/Löschen aller Bausteine (Personen, Struktur, Prozesse,
Ressourcen, Aufgaben, Zuständigkeiten, Termine) direkt gegen die echte Kùzu-Datenbank,
inkl. Live-Graph. **`app.py` ist der einzige Prozess, der die Datenbank hält** — Kùzu
erlaubt nur einen Schreiber. Kalender-Import und MCP-Server laufen deshalb *über* die
Flask-API, nicht direkt gegen die DB. Nur die alten CLI-Skripte (`queries.py`, `digest.py`,
`reload_muellkalender.py`, `import_calendar.py`) brauchen einen gestoppten Server.

## Betrieb als Home-Assistant-Add-on

Ziel ist, dass ontoFam rund um die Uhr läuft — auf dem Home-Assistant-Rechner statt auf
dem PC.

**Installieren:** In Home Assistant *Einstellungen → Apps → App-Store → ⋮ → Repositories*
die URL `https://github.com/burok88/FamilyManager` eintragen, dann ontoFam installieren.
Home Assistant lädt das fertige Image `ghcr.io/burok88/ontofam-amd64` — gebaut und
geprüft von `.github/workflows/app-image.yml` bei jedem Push auf `main`. **Nach jeder
Änderung die `version` in `homeassistant/ontofam/config.yaml` erhöhen**, sonst bietet Home
Assistant kein Update an.

`homeassistant/ontofam/` ist die Vorlage; `python build_addon.py` baut daraus
`dist/homeassistant/ontofam/` (Vorlage plus Kopie der Programmdateien, keine Datenbank,
keine Geheimnisse). Das nutzt der GitHub-Ablauf, und so lässt sich die App zur Not auch
lokal über die Samba-Freigabe `addons` installieren.

| | PC | Add-on |
|---|---|---|
| Datenbank | `kuzu_db` im Projektordner | `/config/kuzu_db` = Samba `addon_configs/<kennung>_ontofam/` |
| Kalender, Erinnerungs-Zeiten | `calendar_sources.json`, `erinnerungen.json` | Reiter *Konfiguration* (`start.py` schreibt daraus dieselben Dateien nach `/data`) |
| Zustellung der Erinnerungen | langlebiges Token in `erinnerungen.json` | Token des Add-ons (`homeassistant_api`) — nichts einzutragen |
| Oberfläche | http://localhost:5000 | Seitenleiste (Ingress) |
| Claude Desktop | `http://localhost:5000` | `http://<HA-Adresse>:5000` + Zugriffsschlüssel |

**Zugriffsschutz nach Herkunft.** Anfragen von localhost und von der Seitenleiste
(Supervisor, `172.30.32.2`) sind schon angemeldet. Alles andere braucht
`Authorization: Bearer <Zugriffsschlüssel>`, sonst 401 — auch die Oberfläche. Ohne
eingetragenen Schlüssel ist Port 5000 damit zu. Der Schlüssel geht im Heimnetz
unverschlüsselt über HTTP; für das Heimnetz reicht das, nach außen gehört Port 5000 nie.

**Beim Stoppen** (auch vor jedem Backup, `backup: cold`) schreibt `app.py` das
Write-Ahead-Log in die Datenbankdatei. Danach ist `kuzu_db` allein vollständig.

Umgebungsvariablen, über die `start.py` das steuert (auf dem PC alle ungesetzt):
`ONTOFAM_DB`, `ONTOFAM_DATEN`, `ONTOFAM_HOST`, `ONTOFAM_PORT`, `ONTOFAM_SCHLUESSEL`,
`ONTOFAM_VERTRAUTE_ADRESSEN`, `ONTOFAM_PUFFER_MB` (Kùzu nimmt sich sonst bis zu 80 % des
Arbeitsspeichers).

Getestet unter Linux (WSL, glibc): Die Datenbankdatei vom Windows-PC öffnet dort
unverändert, Kalender-Abgleich in Ortszeit, Zugriffsschutz, Stoppen mit Checkpoint.
Kùzu braucht neuere Prozessorbefehle; läuft Home Assistant in einer Proxmox-VM mit
CPU-Typ `kvm64`, meldet `start.py` das und rät zu CPU-Typ `host`.

## KI-Anbindung per MCP (Claude Desktop, Gemini CLI, Home Assistant)

`mcp_server.py` stellt die Ontologie als 19 Tools bereit (u.a. `erinnerungen_vorschau`,
`erinnerung_testen`): `personen_auflisten`,
`person_anlegen`, `aufgaben_auflisten`, `anstehende_aufgaben(tage, person)`,
`aufgabe_anlegen(name, datum, person, betrifft, ort, uhrzeit, dauer_minuten, …)`,
`zustaendigkeit_setzen`, `betroffene_person_setzen`, `als_erledigt_markieren`,
`erledigung_zuruecksetzen`, `rueckblick(tage, person)`, `stundenplan_setzen`,
`stundenplan_abfragen`, `stundenplan_eintragen`, `stundenplan_loeschen`,
`kalender_synchronisieren`, `schema_beschreiben`, `graph_abfragen(cypher)`.
Damit funktioniert z.B. *"Max muss Lena am Dienstag den 22. September zum Hautarzt
bringen"* → die KI legt Aufgabe + Termin an und ordnet Max zu; *"Was steht diese Woche
an?"* → Abfrage. Voraussetzung: `app.py` läuft.

- **Claude Desktop**: `python build_mcpb.py` baut `dist/ontofam.mcpb`; installiert wird
  es über Einstellungen → Erweiterungen → Erweiterte Einstellungen → "Erweiterung
  installieren". Der Weg über `claude_desktop_config.json` funktioniert in aktuellen
  Versionen nicht mehr — die App überschreibt die Datei. Wichtig: die Erweiterung enthält
  eine **Kopie** von `mcp_server.py`; nach Änderungen daran neu bauen und neu installieren.
- **Claude Code**: `.mcp.json` im Projektverzeichnis registriert den Server automatisch.
- **Gemini CLI**: gleicher stdio-Aufruf in den Gemini-CLI-Settings (`mcpServers`).
- **Handy / Home Assistant**: Die Gemini-Handy-App kann keine eigenen MCP-Server
  einbinden. Der gangbare Weg ist Home Assistant: `python mcp_server.py --http --host 0.0.0.0`
  startet den Server per Streamable HTTP (Port 8765); HA verbindet sich über die
  "Model Context Protocol"-Integration und stellt die Tools seinem KI-Gesprächsagenten
  (z.B. Google Generative AI / Gemini) zur Verfügung — nutzbar per Assist in der
  HA-Companion-App. Claude-Handy-App ginge alternativ über einen Remote-Connector, dann
  müsste der Server aber aus dem Internet erreichbar und abgesichert sein.

## Termine mit Ort und Dauer, Betroffene, Stundenpläne

Drei Erweiterungen, damit alltägliche Sätze überhaupt abbildbar sind:

- **`Termin` hat `ort`, `beginn` und `dauer_minuten`.** Ohne sie ließe sich aus
  "Handwerkertermin am 02.10. im Allgäu" nicht ableiten, dass jemand hinfahren muss und
  wie lange er weg ist. `beginn` ist Text im Format `HH:MM` (leer = ganztägig) und
  bewusst kein `TIMESTAMP`: `datum` bleibt so der eine Schlüssel für alle Zeitfenster,
  und Bestandstermine mussten nicht umgeschrieben werden.
- **`BETRIFFT` (Aufgabe → Person)** trennt *um wen es geht* von *wer es erledigt*
  (`VERANTWORTLICH_FUER`). Bei "Max bringt Lena zum Hautarzt" ist Max zuständig und
  Lena betroffen. `GET /api/anstehend?person=Lena` findet den Termin deshalb auch dann,
  wenn Lena selbst nichts erledigt.
- **`Schulstunde` + `HAT_STUNDE` (Person → Schulstunde)** für die Stundenpläne der
  Kinder. Eine Schulstunde ist bewusst **kein** `Termin`: sie hat kein Datum, sondern
  Wochentag und Stundennummer. Als Termine modelliert wären das ~1200 Knoten pro
  Schuljahr und Kind.

Der Kalender-Import übernimmt Uhrzeit, Dauer (aus `DTSTART`/`DTEND`) und `LOCATION`
automatisch mit.

## Erinnerungen, die von selbst kommen

Der Server erinnert selbst — niemand muss fragen. Eine Schleife in `app.py` sieht jede
Minute nach, was fällig ist; die Planung steckt in `erinnerungen.py` (reine Logik, ohne
Datenbank testbar).

| | wann | Beispiel |
|---|---|---|
| **Vorlauf** | am Tag *Termin minus Vorlaufzeit* um 19:00, bei Vorlauf 0 um 07:00 am Tag selbst | "Morgen: Müll rausbringen (Papiertonne)" |
| **bald** | bei Terminen mit Uhrzeit 2 Stunden vorher | "In 2 Stunden: Zahnarzt Lena um 15:00" |

- **Wer:** die Zuständigen mit ihrer Vorlaufzeit. Ohne Zuständige die betroffenen
  Erwachsenen, sonst alle Elternteile — ein Elternabend, für den niemand eingetragen ist,
  soll nicht untergehen. Kinder nur, wenn sie selbst zuständig sind.
- **Nicht:** an Erledigtes, Entfallenes und an nichts, das schon begonnen hat.
- **Eine Nachricht je Person und Anlass**, eine Zeile je Termin. "Morgen"/"In 2 Stunden"
  wird beim Senden berechnet — eine nachgeholte Erinnerung sagt nicht "Morgen", wenn es
  schon heute ist.
- **Nachholen:** lief der Rechner um 19 Uhr nicht, kommt die Erinnerung beim nächsten
  Start, solange der Termin noch nicht begonnen hat. Sind dann Vorabend- und
  "bald"-Erinnerung zugleich fällig, gewinnt die genauere; die andere wird verbucht, aber
  nicht gesendet.
- **Nichts doppelt:** gesendete Erinnerungen stehen im Protokoll (`Erinnerung`-Knoten,
  90 Tage). Scheitert die Zustellung, gilt sie nicht als gesendet und wird nach 10 Minuten
  erneut versucht.

**Zustellung über Home Assistant.** ontoFam schickt an Home Assistant
(`POST /api/services/notify/<dienst>`), nicht umgekehrt — ontoFam muss dafür im
Heimnetz nicht erreichbar sein. Einrichten: `erinnerungen.example.json` nach
`erinnerungen.json` kopieren (nicht in Git) und Adresse, langlebiges Zugriffstoken und je
Person den Benachrichtigungsdienst der Companion-App eintragen. Die Datei wird bei jedem
Lauf neu gelesen, ein Neustart ist nicht nötig. Ohne Datei — oder für Personen ohne
Eintrag — landen Erinnerungen im Serverprotokoll (`flask.log`).

Vorschau: `GET /api/erinnerungen?tage=2`; Testnachricht: `POST /api/erinnerungen/testen`
mit `{"person": "Max"}`. Zeiten einstellbar in `erinnerungen.json` (`abend`, `morgen`,
`stunden_vorher`).

Solange ontoFam auf dem PC läuft, gibt es Erinnerungen nur, wenn der Server läuft.

## Erledigt, entfallen, verpasst

*"Anna hat den Müll rausgebracht"* → `als_erledigt_markieren`; *"Was wurde diese Woche
verpasst?"* → `rueckblick`. `anstehende_aufgaben` zeigt den Status mit.

- **Gespeichert wird nur, was jemand festhält:** `erledigt` oder `entfaellt`, als eigener
  Knoten `Erledigung` an Aufgabe **und** Termin, optional mit der Person (`ERLEDIGT_DURCH`).
  Ein Mülltermin gehört der Tonne, nicht der Aufgabe — deshalb beides.
- **Der Rest wird abgeleitet:** ohne Erledigung ist ein Termin `offen`, solange sein Tag
  nicht vorbei ist, danach `verpasst` — oder `unbekannt`, wenn er vor Beginn der
  Erfassung lag (`ONTOFAM_ERFASSUNG_SEIT`, Standard 24.09.2026). Sonst stünde beim ersten
  Rückblick die ganze Vergangenheit als verpasst da. Kein Hintergrundprozess muss um
  Mitternacht etwas umstempeln.
- **Müll:** Tonnen werden am Vorabend rausgestellt. Gibt es am genannten Tag keinen
  Termin, liefert der Server die Termine der Woche davor und danach mit, damit die KI den
  richtigen findet.
- **Erledigungen überleben den Kalender-Sync.** Dafür legt der Sync Termine nicht mehr
  bei jedem Abgleich neu an, sondern gleicht sie ab. Und er ist nur für den abgerufenen
  Zeitraum maßgeblich: ein gestriger Termin fehlt im Abruf, weil er vorbei ist, nicht
  weil er gelöscht wurde — er bleibt samt Erledigung als Verlauf stehen.
- **Freie Abfrage "wer macht wie viel":**
  `MATCH (e:Erledigung {status: 'erledigt'})-[:ERLEDIGT_DURCH]->(p:Person) RETURN p.name, count(e)`

`POST /api/erledigungen`, `DELETE /api/erledigungen`, `GET /api/rueckblick?tage=7&person=`.

## Stundenplan aus einem PDF übernehmen

In Claude Desktop den Stundenplan (PDF oder Foto) in den Chat ziehen und z.B. sagen:
*"Das ist Nicos Stundenplan, übernimm ihn."* Claude liest das Raster, zeigt die erkannte
Woche zur Kontrolle und ruft dann `stundenplan_setzen` auf.

- **Das Lesen macht das Modell, nicht der Server.** Claude Desktop sieht das PDF als
  Bild und als Text. Beides ist nötig: in der Textebene gehen leere Zellen verloren,
  sodass sich aus "SP, E, BI" nicht ablesen lässt, ob Biologie am Mittwoch oder
  Donnerstag liegt — das Raster zeigt es. Ein eigener PDF-Parser im Server hätte genau
  dieses Problem.
- **Ein Aufruf für den ganzen Plan.** `PUT /api/stundenplan/<person_id>` prüft alle
  Einträge, bevor irgendetwas geschrieben wird, und meldet *alle* Fehler auf einmal
  (unbekannter Wochentag, Ende vor Beginn, doppelt belegter Platz im Raster). Geschrieben
  wird in einer Transaktion — ein Fehler in Eintrag 17 hinterlässt keinen halben Plan.
  Der neue Plan ersetzt den alten vollständig.
- **Die Quelle wird Teil des Graphen.** Das PDF hängt als `Dokument` an der Person
  (`DOKUMENT_BETRIFFT`), jede Stunde trägt den Dateinamen in `quelle`. Was nicht in die
  Felder passt — Klasse, Klassenlehrer, Hinweise wie "Sportzeug montags" — steht im Text
  des Dokuments und bleibt nachlesbar. Das ist der GraphRAG-Kern: der Graph findet über
  die Person das richtige Dokument, der Text beantwortet, was die Struktur nicht kann.
- **A/B-Wochen und Betreuung.** `woche` ist leer (jede Woche), `A` oder `B`. Einträge
  ohne Stundennummer (`stunde = 0`) sind Betreuung/OGS; die Rückmeldung nennt pro Tag
  Unterrichtsende und Ende inklusive Betreuung getrennt, weil das die Abholzeit ist.
- **Namen aus Dokumenten.** Auf Plänen steht meist die Langform. "Nicolas Muster" findet
  die Person "Nico".

## Freie Abfragen (Stufe 1 Richtung GraphRAG)

Die festen Tools beantworten nur, wofür jemand eine Funktion geschrieben hat. Mit
`schema_beschreiben` bekommt die KI den Aufbau des Graphen (aus dem Katalog der Datenbank
erzeugt, veraltet also nie) und formuliert mit `graph_abfragen` selbst Cypher. Damit gehen
auch Fragen wie *"wer hat die meisten Aufgaben?"* oder *"welche Aufgaben hängen am Garten?"*.

Schreibschutz auf zwei Ebenen (`graph_query.py`):

1. Eine Stichwortprüfung weist `CREATE`, `DELETE`, `SET` … mit einer verständlichen
   Meldung ab. Kommentare **und Zeichenketten** werden vorher entfernt — sonst würde die
   Kategorie `'Kalender-Import'` die Prüfung auslösen, weil `IMPORT` darin vorkommt.
2. Die eigentliche Sicherung ist die Datenbank: jede Abfrage läuft in
   `BEGIN TRANSACTION READ ONLY`, dort scheitert ein Schreibversuch an Kùzu selbst.

Dazu ein Limit von 200 Zeilen und 10 Sekunden Laufzeit. Die freien Abfragen nutzen eine
**eigene** Kùzu-Verbindung — Transaktionen gelten pro Verbindung, sonst würde eine offene
Lese-Transaktion die Schreibzugriffe der Weboberfläche abweisen.

Direkt per HTTP: `GET /api/schema` und `POST /api/query` mit `{"cypher": "MATCH …"}`.

## Kalender-Import (Google Calendar, ICS)

`calendar_sources.json` (gitignored, Vorlage: `calendar_sources.example.json`) enthält die
privaten ICS-Adressen.

**Der Server hält den Kalender selbst aktuell** — die KI muss nicht daran denken. Die
ICS-Adresse ist ein reiner Abo-Feed, Google meldet Änderungen nicht aktiv; also fragt
`app.py` selbst nach, auf zwei Wegen:

| Auslöser | wann | wofür |
|---|---|---|
| bei einer Abfrage | letzter Abgleich älter als 60 s | Fragen sehen praktisch den aktuellen Google-Stand |
| im Hintergrund | spätestens alle 15 min | Erinnerungen, die ohne Frage entstehen |

Ein Abgleich dauert etwa eine halbe Sekunde. Einstellbar über die Umgebungsvariablen
`ONTOFAM_SYNC_ABFRAGE_SEKUNDEN` und `ONTOFAM_SYNC_INTERVALL_MINUTEN`. Stand und letzter
Fehler: `GET /api/kalender/status`; `GET /api/anstehend` liefert `kalender_stand` und
`kalender_fehler` mit, damit eine KI sagen kann, wenn Google zuletzt nicht erreichbar war.

Worauf es dabei ankommt:

- **Kùzu erlaubt nur einen Schreibvorgang gleichzeitig — und ein zweiter wartet nicht,
  sondern scheitert sofort.** Deshalb laufen alle schreibenden Anfragen und der Sync unter
  einer gemeinsamen Sperre. Der Sync schreibt auf einer eigenen Verbindung in einer
  Transaktion; Leser sehen bis zum Abschluss den alten, vollständigen Stand.
- **Google wird vor dem Sperren abgefragt**, damit kein Schreibzugriff auf das Netz wartet.
- **Hängt Google, warten Fragen nicht.** Ein laufender Sync gilt schon als frisch; die Frage
  antwortet mit dem letzten Stand. Nach einem Fehler wird erst nach Ablauf der Frist erneut
  versucht, nicht bei jeder Frage.
- **Fehlermeldungen enthalten keine Kalenderadresse.** Die Meldungen der Netzwerkbibliothek
  nennen die vollständige URL; weitergegeben werden nur Fehlerart und HTTP-Status.

Sofort abgleichen, unabhängig vom Takt: Tool `kalender_synchronisieren` oder
`curl -X POST http://localhost:5000/api/kalender/sync`. CLI ohne laufenden Server:
`python import_calendar.py`.

Importierte Termine werden als Aufgaben mit Kategorie `Kalender-Import` angelegt (60-Tage-
Fenster, wiederkehrende Ereignisse werden aufgelöst, Uhrzeit/Dauer/`LOCATION` kommen mit,
ganztägige Termine ebenso).

**Uhrzeiten in Haushaltszeit.** Google liefert alle Zeiten in UTC (`20260924T130000Z`).
Der Import rechnet sie nach `Europe/Berlin` um, bevor Datum und Uhrzeit abgelesen werden —
sonst stünde ein Termin um 15:00 als 13:00 in der Ontologie, und einer um 00:30 am
Vortag. Die Zeitzone des Kalenders (`X-WR-TIMEZONE`) wird dafür bewusst ignoriert: beim
Familienkalender steht dort selbst `UTC`.

**Betroffene aus dem Titel.** Steht ein Personenname am Wortanfang im Titel ("Zahnarzt
Lena", "Elternabend Nicolas"), setzt der Sync `BETRIFFT` zu dieser Person. Ohne diese
Kante findet "was steht für Lena an?" ihren Termin nicht, wenn jemand anderes ihn
erledigt. Es wird nur ergänzt, nie entfernt. Bekannte Grenze: "Nicole" würde auch
Nico treffen — die Langform "Nicolas" zu erkennen ist wichtiger.

**Ein Sync gleicht ab, er baut nicht neu auf.** Die Aufgaben-ID wird aus der Kalender-UID
abgeleitet und ist damit über Läufe hinweg stabil. Ein bestehendes Ereignis wird deshalb
wiedererkannt: Titel und Termine werden nachgezogen, der Knoten selbst bleibt stehen — und
mit ihm alles, was von Hand daran hängt (Zuständigkeit samt Vorlaufzeit, Betroffene, Ort,
Prozess, Ressource). Was im Kalender nicht mehr vorkommt, wird entfernt.

Zwei Feinheiten:

- Beim Ersetzen der Termine werden nur die selbst importierten gelöscht (ID-Präfix
  `termin-cal-`). Ein von Hand an dieselbe Aufgabe gehängter Termin bleibt erhalten.
- `synchronisiere()` bekommt **alle** Quellen auf einmal. "Was ist entfallen?" lässt sich
  nur beantworten, wenn alle Kalender bekannt sind — sonst hielte die erste Quelle die
  Aufgaben der zweiten für gelöscht.

Notnagel, falls der Importstand kaputt ist: `POST /api/kalender/sync` mit
`{"neu_aufbauen": true}` wirft alles weg und legt es neu an — dabei gehen manuelle
Zuordnungen verloren.

Der Kalender ist reine **Lesequelle**: Schreiben in den Google-Kalender bräuchte die
Calendar-API mit OAuth (bewusst noch nicht umgesetzt).

## Ausprobieren

```bash
python queries.py        # Beispiel-Abfragen (u.a. "wer ist wann für welche Tonne zuständig")
python digest.py         # Erinnerungs-Digest pro Person -> export/digest.json
python export_graph.py   # Graph-Export für die Visualisierungs-Seite -> export/graph.json
```

Die Visualisierungs-Seite (`visualisierung.html`) ist als Artifact veröffentlicht und bettet
den Export als statischen Snapshot ein. Nach Änderungen an den Daten: `export_graph.py` und
`digest.py` neu ausführen und die JSON-Ausgaben erneut in `visualisierung.html` einbetten
(`__INSTANCE_GRAPH__` / `__DIGEST__` ersetzen), dann erneut veröffentlichen.

## Müllkalender-Daten

`data/muellkalender.csv` wird **regelbasiert generiert** (`generate_muellkalender_csv.py`),
nicht aus dem Kalenderbild abgetippt — Pixelfarben ließen sich nicht zuverlässig genug
unterscheiden. Bestätigte Rhythmen (Stand 2026):

| Tonnenart | Rhythmus |
|---|---|
| Restmüll (schwarzes C) | alle 2 Wochen, Montag, ab 13.07.2026 |
| Gelber Sack (gelbes C) | wie Restmüll |
| Biotonne (braunes B) | alle 2 Wochen, Donnerstag, ab 09.07.2026 (Ausnahme: 24.12. → 23.12.) |
| Papiertonne (grünes C) | alle 4 Wochen, Mittwoch, ab 08.07.2026 |

Nach einer Regeländerung: `python generate_muellkalender_csv.py` (schreibt die CSV neu) und
`python reload_muellkalender.py` (ersetzt nur die Termin-Knoten in der laufenden DB, lässt
Personen/Struktur/Aufgaben unangetastet — dafür vorher `app.py` stoppen, da Kùzu nur einen
schreibenden Prozess gleichzeitig zulässt).

## Geplante spätere Schritte

- **Schreiben in den Google-Kalender** (Calendar-API + OAuth), damit KI-Einträge auch im
  Kalender landen.
- **Assist in Home Assistant** (Gemini oder Claude als Gesprächsagent): `mcp_server.py
  --http` im Add-on mitlaufen lassen und über die MCP-Integration von Home Assistant
  einbinden — dieselben Werkzeuge wie in Claude Desktop, auch unterwegs per Handy.
- **Müllkalender im Add-on**: `reload_muellkalender.py` greift direkt auf die Datei zu;
  bis zum nächsten Jahreswechsel über die API umbauen.
- **GraphRAG weiter**: Längere Freitexte (Elternbriefe, Anleitungen) als `Dokument`
  mit Zerlegung in Abschnitte und Volltextsuche über die `fts`-Erweiterung (deutscher
  Stemmer), danach optional Embeddings über die `vector`-Erweiterung von Kùzu — beide
  laufen in derselben Datenbankdatei. Für Stundenpläne ist das nicht nötig: dort landet
  das Wissen strukturiert im Graphen, und der kurze Dokumenttext lässt sich direkt lesen.

## Lizenz

**Die kommerzielle Nutzung ist strikt untersagt.** ontoFam steht unter einer eigenen,
nicht-kommerziellen Lizenz: [LICENSE.txt](LICENSE.txt). Der Quelltext ist öffentlich
einsehbar (*source-available*), aber keine Open-Source-Software — es gilt keine MIT-,
Apache- oder GPL-Lizenz. Nutzung nur privat und nicht-kommerziell.
