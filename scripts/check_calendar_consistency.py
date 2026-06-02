#!/usr/bin/env python3
"""
check_calendar_consistency.py
=============================

Frühwarn-Wächter für den externen Kalender (termin.frankburkert-daf.de).

Hintergrund: Der öffentliche Kalender wird aus Microsofts veröffentlichtem
ICS-Feed gespeist. Dieser Feed kann einzelne echte Termine verschlucken
(real passiert mit dem Eintrag "Julia" am 2026-06-01). Folge: Ein belegter
Slot erscheint öffentlich als FREI -> jemand bucht darauf -> Doppelbuchung.

Dieses Skript vergleicht die WAHRHEIT (Live-Kalender aus Microsoft Graph,
als JSON übergeben) gegen das, was der Feed zeigt (calendar.ics, korrekt
mit Wiederholungen/Ausnahmen expandiert), und meldet zwei Gefahren:

  1. UNTERDECKUNG ("Julia-Fall"): ein im Live-Kalender belegter Slot, der
     im Feed NICHT als belegt/Vorbehalt erscheint -> öffentlich fälschlich frei.
  2. ECHTE ÜBERLAPPUNG: zwei belegte Live-Termine, die sich zeitlich
     überschneiden -> tatsächliche Doppelbuchung im Kalender selbst.

Das Skript verändert nichts. Es liest nur und meldet. Exit-Code 2, wenn es
Funde gibt (damit der Aufrufer einen Push auslösen kann), sonst 0.

Aufruf:
  python3 check_calendar_consistency.py \
      --ics /pfad/calendar.ics \
      --busy-json /pfad/live_busy.json \
      [--days 21] [--report /pfad/report.json]

Format von live_busy.json: Liste von Objekten mit ISO-Zeiten in UTC, z.B.
  [{"subject": "Julia", "start": "2026-06-01T12:00:00Z",
    "end": "2026-06-01T13:30:00Z"}]
(subject ist optional und dient nur der lesbaren Meldung.)
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

# Abhängigkeiten werden vom Aufrufer (Scheduled Task) zur Laufzeit
# installiert: pip install icalendar recurring_ical_events python-dateutil
import icalendar
import recurring_ical_events
from dateutil import parser as dtparser

# Mindest-Überlappung (Sekunden), ab der eine Unterdeckung als echt gilt.
# Verhindert Fehlalarme durch Sekunden-Rundungen an Slot-Rändern.
MIN_GAP_SECONDS = 120


def to_utc(dt):
    """Normalisiere ein datetime/date auf timezone-aware UTC."""
    if not isinstance(dt, datetime):
        # reines date (Ganztags) -> Mitternacht UTC
        dt = datetime(dt.year, dt.month, dt.day)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def feed_busy_intervals(ics_path, window_start, window_end):
    """Expandiere den Feed und liefere belegte Intervalle (UTC).

    'Belegt' = exakt das, was die öffentliche Seite als NICHT-frei zeichnet:
    Einträge mit TRANSP != TRANSPARENT und STATUS != CANCELLED. TENTATIVE
    ('mit Vorbehalt') zählt als belegt, da der Slot dann nicht als frei gilt.
    """
    with open(ics_path, "rb") as fh:
        cal = icalendar.Calendar.from_ical(fh.read())

    events = recurring_ical_events.of(cal).between(window_start, window_end)
    intervals = []
    for ev in events:
        status = str(ev.get("STATUS", "")).upper()
        transp = str(ev.get("TRANSP", "OPAQUE")).upper()
        if status == "CANCELLED":
            continue
        if transp == "TRANSPARENT":
            continue
        start = ev.get("DTSTART")
        end = ev.get("DTEND")
        if start is None or end is None:
            continue
        s = to_utc(start.dt)
        e = to_utc(end.dt)
        if e > s:
            intervals.append((s, e))
    intervals.sort()
    return intervals


def merge_intervals(intervals):
    """Überlappende/aneinandergrenzende Intervalle verschmelzen."""
    if not intervals:
        return []
    merged = [list(intervals[0])]
    for s, e in intervals[1:]:
        if s <= merged[-1][1]:
            if e > merged[-1][1]:
                merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def uncovered_portions(busy_start, busy_end, covered):
    """Teile von [busy_start, busy_end], die von 'covered' NICHT gedeckt sind."""
    gaps = []
    cursor = busy_start
    for cs, ce in covered:
        if ce <= cursor:
            continue
        if cs >= busy_end:
            break
        if cs > cursor:
            gaps.append((cursor, min(cs, busy_end)))
        cursor = max(cursor, ce)
        if cursor >= busy_end:
            break
    if cursor < busy_end:
        gaps.append((cursor, busy_end))
    return [(s, e) for s, e in gaps if (e - s).total_seconds() >= MIN_GAP_SECONDS]


def load_live_busy(busy_json_path):
    with open(busy_json_path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    out = []
    for item in raw:
        s = to_utc(dtparser.isoparse(item["start"]))
        e = to_utc(dtparser.isoparse(item["end"]))
        if e > s:
            out.append({
                "subject": item.get("subject", "(ohne Titel)"),
                "start": s,
                "end": e,
            })
    out.sort(key=lambda x: x["start"])
    return out


def find_overlaps(live):
    """Echte Doppelbuchungen: zwei belegte Live-Termine, die sich
    zeitlich überschneiden (Berührung Ende==Anfang gilt nicht)."""
    overlaps = []
    for i in range(len(live)):
        for j in range(i + 1, len(live)):
            a, b = live[i], live[j]
            if b["start"] >= a["end"]:
                break  # sortiert -> keine weiteren Überlappungen mit a
            latest_start = max(a["start"], b["start"])
            earliest_end = min(a["end"], b["end"])
            if (earliest_end - latest_start).total_seconds() >= MIN_GAP_SECONDS:
                overlaps.append({
                    "a": a["subject"], "b": b["subject"],
                    "overlap_start": latest_start.isoformat(),
                    "overlap_end": earliest_end.isoformat(),
                })
    return overlaps


def fmt_local(dt):
    """ISO-UTC -> lesbares CET/CEST-Label (Europe/Berlin)."""
    try:
        from zoneinfo import ZoneInfo
        local = dt.astimezone(ZoneInfo("Europe/Berlin"))
        return local.strftime("%a %d.%m. %H:%M %Z")
    except Exception:
        return dt.strftime("%Y-%m-%d %H:%M UTC")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ics", required=True)
    ap.add_argument("--busy-json", required=True)
    ap.add_argument("--days", type=int, default=21)
    ap.add_argument("--report", default=None)
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    window_start = now
    window_end = now + timedelta(days=args.days)

    live = load_live_busy(args.busy_json)
    live_in_window = [x for x in live if x["end"] > window_start and x["start"] < window_end]

    feed = feed_busy_intervals(args.ics, window_start, window_end)
    feed_merged = merge_intervals(feed)

    # 1) Unterdeckung (Julia-Fall)
    missing = []
    for ev in live_in_window:
        gaps = uncovered_portions(ev["start"], ev["end"], feed_merged)
        if gaps:
            missing.append({
                "subject": ev["subject"],
                "start": ev["start"].isoformat(),
                "end": ev["end"].isoformat(),
                "uncovered": [
                    {"start": s.isoformat(), "end": e.isoformat()} for s, e in gaps
                ],
                "label": fmt_local(ev["start"]),
            })

    # 2) Echte Überlappungen
    overlaps = find_overlaps(live_in_window)

    report = {
        "checked_at": now.isoformat(),
        "window_days": args.days,
        "live_busy_count": len(live_in_window),
        "feed_busy_intervals": len(feed_merged),
        "missing_from_feed": missing,
        "real_overlaps": overlaps,
        "ok": (not missing and not overlaps),
    }

    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)

    # Menschlich lesbare Zusammenfassung auf stdout
    if report["ok"]:
        print(f"OK — {len(live_in_window)} belegte Live-Termine, alle im Feed gedeckt, keine Überlappungen.")
    else:
        if missing:
            print(f"WARNUNG: {len(missing)} belegte(r) Termin(e) NICHT im öffentlichen Kalender (erscheinen fälschlich frei):")
            for m in missing:
                print(f"  - {m['subject']}: {m['label']}")
        if overlaps:
            print(f"WARNUNG: {len(overlaps)} echte Überlappung(en) im Live-Kalender:")
            for o in overlaps:
                print(f"  - {o['a']} <> {o['b']} ({o['overlap_start']} – {o['overlap_end']})")

    sys.exit(2 if not report["ok"] else 0)


if __name__ == "__main__":
    main()
