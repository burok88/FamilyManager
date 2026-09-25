"""Das Schema der Ontologie an einer Stelle -- als wiederholbare Migration.

Alle Anweisungen sind idempotent ("CREATE ... IF NOT EXISTS", "ALTER TABLE ... ADD
IF NOT EXISTS"). migriere() laeuft deshalb bei jedem Start von app.py mit: eine
frisch angelegte und eine gewachsene Datenbank landen beim gleichen Schema, ohne
dass Bestandsdaten angefasst werden. Fruehere Erweiterungen hiessen "Datenbank
loeschen und setup_schema.py neu laufen lassen" -- damit waeren Muellkalender und
Kalender-Import jedes Mal weg gewesen.

Neue Spalten brauchen ein DEFAULT: bestehende Zeilen bekommen diesen Wert.
"""

WOCHENTAGE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]

# "" = jede Woche. A/B fuer Schulen mit zweiwoechigem Rhythmus -- dort ist derselbe
# Platz im Raster (z.B. Montag 3. Stunde) je nach Woche mit anderem Fach belegt.
WOCHEN = ("", "A", "B")

# Gespeichert wird nur, was jemand festhaelt. "offen" und "verpasst" werden abgeleitet:
# ein Termin ohne Erledigung ist offen, solange sein Tag nicht vorbei ist, danach
# verpasst. So muss kein Hintergrundprozess um Mitternacht Termine umstempeln.
ERLEDIGUNG_STATUS = ("erledigt", "entfaellt")

KNOTEN = [
    """CREATE NODE TABLE IF NOT EXISTS Person(
        id STRING, name STRING, rolle STRING, PRIMARY KEY (id))""",
    """CREATE NODE TABLE IF NOT EXISTS Struktur(
        id STRING, name STRING, typ STRING, PRIMARY KEY (id))""",
    """CREATE NODE TABLE IF NOT EXISTS Aufgabe(
        id STRING, name STRING, beschreibung STRING, kategorie STRING, PRIMARY KEY (id))""",
    """CREATE NODE TABLE IF NOT EXISTS Prozess(
        id STRING, name STRING, beschreibung STRING, PRIMARY KEY (id))""",
    """CREATE NODE TABLE IF NOT EXISTS Termin(
        id STRING, datum DATE, PRIMARY KEY (id))""",
    """CREATE NODE TABLE IF NOT EXISTS Ressource(
        id STRING, name STRING, farbe STRING, kalender_code STRING, PRIMARY KEY (id))""",
    # Eine Schulstunde ist ein wiederkehrender Wochenplan-Eintrag, kein Termin:
    # sie hat kein Datum, sondern Wochentag und Stundennummer.
    """CREATE NODE TABLE IF NOT EXISTS Schulstunde(
        id STRING,
        wochentag STRING,
        wochentag_nr INT64,
        stunde INT64,
        fach STRING,
        raum STRING,
        beginn STRING,
        ende STRING,
        PRIMARY KEY (id))""",
    # Die Quelle, aus der eine KI Wissen in den Graphen uebernommen hat -- z.B. der
    # Stundenplan als PDF. Der Text bleibt erhalten: was sich nicht in Felder pressen
    # liess (Klassenlehrer, Hinweise, Schuljahr), ist so trotzdem nachlesbar.
    # Dass eine Aufgabe an einem Termin erledigt wurde (oder entfiel). Ein eigener
    # Knoten statt einer Eigenschaft am Termin: ein Muelltermin gehoert der Tonne,
    # nicht der Aufgabe, und wer es erledigt hat, soll als Person im Graphen haengen
    # -- nur so laesst sich spaeter fragen, wer wie viel im Haushalt macht.
    """CREATE NODE TABLE IF NOT EXISTS Erledigung(
        id STRING,
        status STRING,
        zeitpunkt TIMESTAMP,
        notiz STRING,
        PRIMARY KEY (id))""",
    # Protokoll gesendeter Erinnerungen, damit nichts doppelt kommt. Bewusst ohne
    # Kanten: das ist Betriebszustand, kein Wissen ueber den Haushalt.
    """CREATE NODE TABLE IF NOT EXISTS Erinnerung(
        id STRING,
        art STRING,
        person STRING,
        termin_id STRING,
        faellig TIMESTAMP,
        gesendet TIMESTAMP,
        kanal STRING,
        text STRING,
        PRIMARY KEY (id))""",
    """CREATE NODE TABLE IF NOT EXISTS Dokument(
        id STRING,
        titel STRING,
        typ STRING,
        quelle STRING,
        datum DATE,
        text STRING,
        PRIMARY KEY (id))""",
]

KANTEN = [
    "CREATE REL TABLE IF NOT EXISTS GEHOERT_ZU(FROM Person TO Struktur)",
    """CREATE REL TABLE IF NOT EXISTS VERANTWORTLICH_FUER(
        FROM Person TO Aufgabe, vorlaufzeit_tage INT64)""",
    "CREATE REL TABLE IF NOT EXISTS AUFGABE_AN_ORT(FROM Aufgabe TO Struktur)",
    "CREATE REL TABLE IF NOT EXISTS TEIL_VON(FROM Aufgabe TO Prozess, reihenfolge INT64)",
    "CREATE REL TABLE IF NOT EXISTS BEZIEHT_SICH_AUF(FROM Aufgabe TO Ressource)",
    "CREATE REL TABLE IF NOT EXISTS RESSOURCE_HAT_TERMIN(FROM Ressource TO Termin)",
    "CREATE REL TABLE IF NOT EXISTS AUFGABE_HAT_TERMIN(FROM Aufgabe TO Termin)",
    # BETRIFFT trennt "um wen geht es" von "wer erledigt es" (VERANTWORTLICH_FUER).
    # Bei "Max bringt Lena zum Hautarzt" ist Max verantwortlich, Lena betroffen.
    "CREATE REL TABLE IF NOT EXISTS BETRIFFT(FROM Aufgabe TO Person)",
    "CREATE REL TABLE IF NOT EXISTS HAT_STUNDE(FROM Person TO Schulstunde)",
    # Die Bruecke zwischen Textwelt und Ontologie: ueber sie fuehrt der Weg vom
    # Stundenplan-PDF zu Nico und von dort zu seinen Stunden, Aufgaben und Terminen.
    "CREATE REL TABLE IF NOT EXISTS DOKUMENT_BETRIFFT(FROM Dokument TO Person)",
    "CREATE REL TABLE IF NOT EXISTS ERLEDIGUNG_ZU_TERMIN(FROM Erledigung TO Termin)",
    "CREATE REL TABLE IF NOT EXISTS ERLEDIGUNG_ZU_AUFGABE(FROM Erledigung TO Aufgabe)",
    "CREATE REL TABLE IF NOT EXISTS ERLEDIGT_DURCH(FROM Erledigung TO Person)",
]

SPALTEN = [
    # Ohne Ort und Dauer laesst sich aus "Handwerkertermin am 02.10. im Allgaeu"
    # nicht ableiten, dass jemand hinfahren muss und wie lange er weg ist.
    "ALTER TABLE Termin ADD IF NOT EXISTS ort STRING DEFAULT ''",
    # Uhrzeit als Text "HH:MM", leer = ganztaegig. Bewusst nicht als TIMESTAMP:
    # datum bleibt so der eine Schluessel, ueber den alle Zeitfenster abgefragt
    # werden, und Bestandstermine muessen nicht umgeschrieben werden.
    "ALTER TABLE Termin ADD IF NOT EXISTS beginn STRING DEFAULT ''",
    "ALTER TABLE Termin ADD IF NOT EXISTS dauer_minuten INT64 DEFAULT 0",
    # Woher stammt diese Stunde -- z.B. "Stundenplan Nico 2. Halbjahr.pdf". Gleicher
    # Wert wie Dokument.quelle, darueber lassen sich Stunde und Quelle verbinden.
    "ALTER TABLE Schulstunde ADD IF NOT EXISTS quelle STRING DEFAULT ''",
    # Beides steht auf fast jedem Stundenplan. Ohne woche wuerde ein A/B-Plan so
    # aussehen, als haette Nico zwei Faecher gleichzeitig.
    "ALTER TABLE Schulstunde ADD IF NOT EXISTS lehrer STRING DEFAULT ''",
    "ALTER TABLE Schulstunde ADD IF NOT EXISTS woche STRING DEFAULT ''",
]


def _zustand(conn) -> set[str]:
    """Tabellen und Spalten als Menge -- Grundlage fuer die Meldung, was neu ist."""
    vorhanden = set()
    for zeile in conn.execute("CALL show_tables() RETURN name").get_as_df().itertuples():
        vorhanden.add(zeile.name)
        spalten = conn.execute(f"CALL table_info('{zeile.name}') RETURN *").get_as_df()
        for spalte in spalten.itertuples():
            vorhanden.add(f"{zeile.name}.{spalte.name}")
    return vorhanden


def migriere(conn) -> list[str]:
    """Bringt die Datenbank auf den aktuellen Stand. Gibt zurueck, was neu dazukam."""
    vorher = _zustand(conn)
    for anweisung in KNOTEN + KANTEN + SPALTEN:
        conn.execute(anweisung)
    return sorted(_zustand(conn) - vorher)


def wochentag_nummer(name: str) -> int:
    """1 = Montag. Wird serverseitig aus dem Namen abgeleitet, damit Name und
    Nummer nicht auseinanderlaufen koennen."""
    try:
        return WOCHENTAGE.index(name.strip().capitalize()) + 1
    except ValueError:
        raise ValueError(f"Unbekannter Wochentag '{name}'. Erlaubt: {', '.join(WOCHENTAGE)}")
