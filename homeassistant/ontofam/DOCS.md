# ontoFam

Haushalts-Ontologie der Familie: Personen, Aufgaben, Termine, Stundenpläne und
Erinnerungen. Die Oberfläche steht in der Seitenleiste, Claude Desktop greift über
Port 5000 mit dem Zugriffsschlüssel zu.

## Einrichten

1. **Konfiguration:** Kalender (Name und private iCal-Adresse), Zugriffsschlüssel und
   die Empfänger der Erinnerungen eintragen, speichern.
2. **Info:** „In Seitenleiste anzeigen“ einschalten, starten.
3. Das Protokoll zeigt, welche Kalender und Empfänger ontoFam erkannt hat — ohne die
   geheimen Adressen.

## Datenbank

Die Datenbank liegt in `addon_configs/<kennung>_ontofam/kuzu_db` (Samba-Freigabe). Zum
Ersetzen das Add-on **stoppen**, `kuzu_db` überschreiben, eine vorhandene
`kuzu_db.wal` löschen, wieder starten. Backups von Home Assistant enthalten die
Datenbank; das Add-on wird dafür kurz gestoppt.

## Fehler

„Der Prozessor der VM kennt Befehle nicht …“: In Proxmox die VM → Hardware →
Prozessoren → Typ auf `host` stellen und die VM neu starten.
