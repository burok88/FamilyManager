"""Generiert data/muellkalender.csv regelbasiert aus den vom Nutzer bestätigten
Abfuhr-Rhythmen (statt Termine aus dem Kalenderbild abzulesen, was bei Pixel-Farben
nicht zuverlässig genug wäre).

Regeln (Stand Nutzerangabe):
- Restmüll:      alle 2 Wochen, Montag, ab 2026-07-13
- Gelber Sack:   wie Restmüll (alle 2 Wochen, Montag, ab 2026-07-13)
- Biotonne:      alle 2 Wochen, Donnerstag, ab 2026-07-09 — Ausnahme: 24.12. verschiebt
                 sich auf 23.12. (Weihnachten)
- Papiertonne:   alle 4 Wochen, Mittwoch, ab 2026-07-08
"""

import csv
from datetime import date, timedelta
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "muellkalender.csv"
END = date(2026, 12, 31)

WEEKDAY_NAMES = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def every_n_weeks(start: date, weeks: int, end: date):
    d = start
    step = timedelta(weeks=weeks)
    while d <= end:
        yield d
        d += step


def check_weekday(d: date, expected: str, label: str) -> None:
    actual = WEEKDAY_NAMES[d.weekday()]
    if actual != expected:
        raise ValueError(f"{label}: {d.isoformat()} ist {actual}, erwartet {expected}")


def main() -> None:
    rows: list[tuple[date, str]] = []

    restmuell_start = date(2026, 7, 13)
    check_weekday(restmuell_start, "Mo", "Restmüll-Start")
    for d in every_n_weeks(restmuell_start, 2, END):
        rows.append((d, "restmuell-schwarz"))
        rows.append((d, "gelber-sack-gelb"))  # gleicher Rhythmus wie Restmüll

    bio_start = date(2026, 7, 9)
    check_weekday(bio_start, "Do", "Biotonne-Start")
    for d in every_n_weeks(bio_start, 2, END):
        if d == date(2026, 12, 24):
            d = date(2026, 12, 23)  # Weihnachten: Vorverlegung
        rows.append((d, "biotonne-braun"))

    papier_start = date(2026, 7, 8)
    check_weekday(papier_start, "Mi", "Papiertonne-Start")
    for d in every_n_weeks(papier_start, 4, END):
        rows.append((d, "papier-gruen"))

    rows.sort()

    with open(OUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["datum", "code"])
        for d, code in rows:
            writer.writerow([d.isoformat(), code])

    print(f"{len(rows)} Termine geschrieben nach {OUT}")


if __name__ == "__main__":
    main()
