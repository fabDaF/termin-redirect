#!/usr/bin/env python3
"""
flatten_ics.py — Wurzel-Fix gegen den Render-Bug der Terminseite.

Problem: index.html verarbeitet jeden VEVENT einzeln und verknüpft
wiederkehrende Serien nicht sauber mit ihren verschobenen Ausnahmen.
Dadurch entstehen fälschliche Frei-Löcher mitten in belegten Stunden
(Vorfall "Julia", 2026-06-01) -> Doppelbuchungsgefahr.

Lösung: Den von Microsoft gelieferten Feed VOR der Auslieferung mit einer
korrekten Bibliothek in lauter EINZELTERMINE expandieren. Danach gibt es
keine RRULE/Ausnahmen mehr, die die Seite falsch zusammensetzen könnte —
die naive Pro-Event-Logik der Seite rendert flache Einzeltermine korrekt.

Aufruf:  python3 flatten_ics.py QUELLE.ics ZIEL.ics
Exit 0 bei Erfolg; != 0 (ohne Zieldatei zu schreiben) wenn das Ergebnis
unplausibel ist — damit die GitHub-Action keinen kaputten Feed committet.
"""

import sys
from datetime import datetime, timedelta, timezone

import icalendar
import recurring_ical_events

# Zeitfenster: so weit zurück/voraus expandieren. Voraus großzügig genug
# zum Buchen, aber begrenzt, damit die Datei schlank bleibt.
DAYS_BACK = 14
DAYS_AHEAD = 140

# Untergrenze plausibler Termine — schützt vor leerem/kaputtem Feed.
MIN_EVENTS = 10


def to_utc(dt):
    if not isinstance(dt, datetime):
        dt = datetime(dt.year, dt.month, dt.day)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def ics_dt(dt):
    return to_utc(dt).strftime("%Y%m%dT%H%M%SZ")


def main():
    if len(sys.argv) != 3:
        print("Aufruf: flatten_ics.py QUELLE.ics ZIEL.ics", file=sys.stderr)
        sys.exit(64)
    src, dst = sys.argv[1], sys.argv[2]

    with open(src, "rb") as fh:
        cal = icalendar.Calendar.from_ical(fh.read())

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=DAYS_BACK)
    end = now + timedelta(days=DAYS_AHEAD)

    occurrences = recurring_ical_events.of(cal).between(start, end)

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//fabDaF//flatten_ics//DE",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Termine (flach)",
    ]

    count = 0
    for ev in occurrences:
        status = str(ev.get("STATUS", "")).upper()
        transp = str(ev.get("TRANSP", "OPAQUE")).upper()
        # Nur das ausgeben, was die Seite ohnehin als belegt zeichnet.
        if status == "CANCELLED" or transp == "TRANSPARENT":
            continue
        dtstart = ev.get("DTSTART")
        dtend = ev.get("DTEND")
        if dtstart is None or dtend is None:
            continue
        s = to_utc(dtstart.dt)
        e = to_utc(dtend.dt)
        if e <= s:
            continue
        summary = str(ev.get("SUMMARY", "Gebucht")) or "Gebucht"
        out_status = "TENTATIVE" if status == "TENTATIVE" else "CONFIRMED"
        uid = f"flat-{count}-{ics_dt(s)}@termin.frankburkert-daf.de"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{ics_dt(now)}",
            f"DTSTART:{ics_dt(s)}",
            f"DTEND:{ics_dt(e)}",
            f"SUMMARY:{summary}",
            "TRANSP:OPAQUE",
            f"STATUS:{out_status}",
            "END:VEVENT",
        ]
        count += 1

    lines.append("END:VCALENDAR")

    if count < MIN_EVENTS:
        print(f"FEHLER: nur {count} Termine expandiert (< {MIN_EVENTS}) — "
              f"Feed sieht kaputt aus, ZIEL nicht geschrieben.", file=sys.stderr)
        sys.exit(1)

    with open(dst, "w", encoding="utf-8") as fh:
        fh.write("\r\n".join(lines) + "\r\n")

    print(f"OK: {count} Einzeltermine geschrieben ({start.date()} .. {end.date()}).")
    sys.exit(0)


if __name__ == "__main__":
    main()
