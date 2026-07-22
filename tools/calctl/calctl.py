#!/usr/bin/env python3
"""calctl — Apple Calendar CLI, runtime reader.

Reads ~/Library/Group Containers/group.com.apple.calendar/Calendar.sqlitedb
DIRECTLY (read-only) under Full Disk Access — the ONLY path proven to work in
Lautrec's `claude -p`/launchd runtime (EventKit hangs / grants don't carry
down the launchd chain — see the macOS/TCC notes in the README).

Because the DB stores recurring events as MASTERS (not expanded occurrences),
this expands recurrence itself: frequency/interval/specifier from the
Recurrence table, minus ExceptionDate cancellations, with detached overrides
(orig_item_id) substituted. The Swift EventKit build (calctl-ek) is the
validation oracle — run it in an interactive Terminal (where it's granted) and
diff against this.

Reads are local-DB only. WRITES (`add`) go over CalDAV (iCloud/Google) — pure
HTTPS, zero TCC, so they work headlessly where EventKit is dead. Writes branch
before the DB connect(), read credentials from the environment, and never need
Full Disk Access.
"""

import calendar as _cal
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

CD_EPOCH = 978307200  # seconds between 1970-01-01 and 2001-01-01 (Core Data)
DB_PATH = os.path.expanduser(
    "~/Library/Group Containers/group.com.apple.calendar/Calendar.sqlitedb"
)
WEEKDAYS = {"SU": 6, "MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5}
# Python weekday(): Mon=0..Sun=6. Map iCal codes to that.

# ---------------------------------------------------------------- dates

def cd_to_dt(x, all_day=False, is_end=False):
    """Core Data epoch (float seconds) → local naive datetime.

    Timed events are stored as a real UTC instant → convert to local time.
    All-day events are stored at UTC-date boundaries (start = 00:00 UTC of the
    first day; end = 23:59:59 UTC of the last day). EventKit surfaces them at
    LOCAL midnight of those dates. So floor to the UTC date and return local
    midnight — and for the END, add a day to make it the exclusive next-midnight
    so a single-day all-day event has a 1-day span (flooring both ends to the
    same date would give duration 0 and the event would fall through the
    strict window-overlap check)."""
    ts = x + CD_EPOCH
    if all_day:
        d = datetime.fromtimestamp(ts, timezone.utc)
        base = datetime(d.year, d.month, d.day)
        return base + timedelta(days=1) if is_end else base
    return datetime.fromtimestamp(ts)

def dt_to_iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")

def parse_when(s, end_of_day=False):
    s = (s or "").strip().lower()
    now = datetime.now()
    if s in ("", "today"):
        base = now
    elif s == "tomorrow":
        base = now + timedelta(days=1)
    elif s == "yesterday":
        base = now - timedelta(days=1)
    elif s == "now":
        return now
    else:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s, fmt)
                if fmt == "%Y-%m-%d" and end_of_day:
                    return dt.replace(hour=23, minute=59, second=59)
                return dt
            except ValueError:
                continue
        raise SystemExit(json.dumps({"status": "error", "message": f"cannot parse date: {s}"}))
    if end_of_day:
        return base.replace(hour=23, minute=59, second=59, microsecond=0)
    return base.replace(hour=0, minute=0, second=0, microsecond=0)

def add_months(dt, n):
    """dt plus n calendar months, clamping day to month length."""
    m = dt.month - 1 + n
    y = dt.year + m // 12
    m = m % 12 + 1
    d = min(dt.day, _cal.monthrange(y, m)[1])
    return dt.replace(year=y, month=m, day=d)

def nth_weekday_of_month(year, month, weekday, ordinal):
    """ordinal: +1..+5 = 1st..5th; -1 = last. weekday: Python Mon=0."""
    if ordinal > 0:
        first = datetime(year, month, 1)
        offset = (weekday - first.weekday()) % 7
        day = 1 + offset + (ordinal - 1) * 7
        if day > _cal.monthrange(year, month)[1]:
            return None
        return datetime(year, month, day)
    else:
        last = _cal.monthrange(year, month)[1]
        d = datetime(year, month, last)
        offset = (d.weekday() - weekday) % 7
        return d - timedelta(days=offset)

# ---------------------------------------------------------------- recurrence

def parse_specifier(spec):
    """Return dict: bydays [(ordinal|None, py_weekday)], monthdays [int], month (O=)."""
    out = {"bydays": [], "monthdays": [], "month": None}
    for part in (spec or "").split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k == "D":
            for tok in v.split(","):
                tok = tok.strip()
                if not tok:
                    continue
                # forms: 0TU (every TU), +1FR (first FR), -1FR (last FR)
                if tok[:2].lstrip("+-").isdigit() or tok[0] in "+-0":
                    sign = 1
                    i = 0
                    if tok[0] in "+-":
                        sign = -1 if tok[0] == "-" else 1
                        i = 1
                    num = ""
                    while i < len(tok) and tok[i].isdigit():
                        num += tok[i]
                        i += 1
                    code = tok[i:]
                    if code in WEEKDAYS:
                        ordinal = None if num in ("", "0") else sign * int(num)
                        out["bydays"].append((ordinal, WEEKDAYS[code]))
                elif tok in WEEKDAYS:
                    out["bydays"].append((None, WEEKDAYS[tok]))
        elif k == "M":
            for tok in v.split(","):
                if tok.strip().lstrip("+-").isdigit():
                    out["monthdays"].append(int(tok))
        elif k == "O":
            try:
                out["month"] = int(v)
            except ValueError:
                pass
    return out

def expand(master_start, freq, interval, spec, count, until, win_start, win_end, cap=2000):
    """Yield occurrence start datetimes (local naive) within [win_start, win_end]."""
    interval = interval or 1
    p = parse_specifier(spec)
    emitted = 0
    occ_count = 0

    def within(dt):
        return win_start <= dt <= win_end

    def past_limits(dt, n):
        if until and dt > until:
            return True
        if count and n >= count:
            return True
        if dt > win_end:
            return True
        return False

    if freq == 1:  # DAILY
        dt = master_start
        n = 0
        while not past_limits(dt, n) and emitted < cap:
            if dt >= win_start and within(dt):
                yield dt
            dt = master_start + timedelta(days=interval * (n + 1))
            n += 1
            emitted += 1

    elif freq == 2:  # WEEKLY
        bydays = [wd for (_, wd) in p["bydays"]] or [master_start.weekday()]
        # walk week-by-week from the master's week start (Sunday-based like Apple)
        week0 = master_start - timedelta(days=(master_start.weekday() + 1) % 7)
        w = 0
        while emitted < cap:
            week_start = week0 + timedelta(weeks=interval * w)
            if week_start > win_end + timedelta(days=7):
                break
            any_future = False
            for dow in sorted(set(bydays)):
                # day within this week matching dow, preserving master time
                delta_days = ((dow + 1) % 7) - ((week_start.weekday() + 1) % 7)
                day = week_start + timedelta(days=delta_days)
                day = day.replace(hour=master_start.hour, minute=master_start.minute,
                                  second=master_start.second)
                if day < master_start:
                    continue
                any_future = True
                if until and day > until:
                    return
                if count and occ_count >= count:
                    return
                occ_count += 1
                if within(day):
                    yield day
            w += 1
            emitted += 1
            if week_start > win_end and any_future:
                break

    elif freq == 3:  # MONTHLY
        n = 0
        while emitted < cap:
            base = add_months(master_start, interval * n)
            if until and base > until + timedelta(days=31):
                break
            if base > win_end + timedelta(days=31):
                break
            cands = []
            if p["monthdays"]:
                for md in p["monthdays"]:
                    dim = _cal.monthrange(base.year, base.month)[1]
                    day = md if md > 0 else dim + md + 1
                    if 1 <= day <= dim:
                        cands.append(base.replace(day=day))
            elif p["bydays"]:
                for ordinal, wd in p["bydays"]:
                    d = nth_weekday_of_month(base.year, base.month, wd, ordinal or 1)
                    if d:
                        cands.append(d.replace(hour=master_start.hour, minute=master_start.minute,
                                               second=master_start.second))
            else:
                cands.append(base)
            for dt in sorted(cands):
                if dt < master_start:
                    continue
                if until and dt > until:
                    return
                if count and occ_count >= count:
                    return
                occ_count += 1
                if within(dt):
                    yield dt
            n += 1
            emitted += 1

    elif freq == 4:  # YEARLY
        n = 0
        while emitted < cap:
            year = master_start.year + interval * n
            if year > win_end.year + 1:
                break
            month = p["month"] or master_start.month
            cands = []
            if p["bydays"]:
                for ordinal, wd in p["bydays"]:
                    d = nth_weekday_of_month(year, month, wd, ordinal or 1)
                    if d:
                        cands.append(d.replace(hour=master_start.hour, minute=master_start.minute,
                                               second=master_start.second))
            elif p["monthdays"]:
                for md in p["monthdays"]:
                    try:
                        cands.append(master_start.replace(year=year, month=month, day=md))
                    except ValueError:
                        pass
            else:
                try:
                    cands.append(master_start.replace(year=year))
                except ValueError:
                    pass
            for dt in sorted(cands):
                if dt < master_start:
                    continue
                if until and dt > until:
                    return
                if count and occ_count >= count:
                    return
                occ_count += 1
                if within(dt):
                    yield dt
            n += 1
            emitted += 1

# ---------------------------------------------------------------- db

def connect():
    """Open the live Calendar DB reliably from any context.

    A `mode=ro` open of a WAL database still needs to touch the `-shm` shared-
    memory file, and creating that mmap in the protected Group Container fails
    intermittently under launchd ("authorization denied"). So copy the DB and
    its `-wal`/`-shm` sidecars to a private temp dir and open the COPY normally
    — WAL is applied (we see current data) and no write happens in the
    protected container. This is why it works under `claude -p` where a bare
    read-only open flakes."""
    if not os.path.exists(DB_PATH):
        raise SystemExit(json.dumps({"status": "error", "message": f"Calendar DB not found at {DB_PATH}"}))
    import shutil, tempfile, atexit
    try:
        tmp = tempfile.mkdtemp(prefix="calctl-")
        atexit.register(lambda: shutil.rmtree(tmp, ignore_errors=True))
        dst = os.path.join(tmp, "Calendar.sqlitedb")
        for suffix in ("", "-wal", "-shm"):
            src = DB_PATH + suffix
            if os.path.exists(src):
                shutil.copy2(src, dst + suffix)
        con = sqlite3.connect(dst, timeout=5)
        con.row_factory = sqlite3.Row
        return con
    except (sqlite3.Error, OSError) as e:
        raise SystemExit(json.dumps({"status": "error",
            "message": f"cannot open Calendar DB (Full Disk Access needed?): {e}"}))

def load_calendars(con):
    """Event calendars only — exclude the Reminders store (type 6), which
    EventKit's event query also excludes (those are reminders surfaced into
    Calendar.app as 'Scheduled Reminders')."""
    out = {}
    for r in con.execute(
        "SELECT c.ROWID, c.title FROM Calendar c "
        "LEFT JOIN Store s ON c.store_id = s.ROWID "
        "WHERE s.type IS NULL OR s.type != 6"
    ):
        out[r["ROWID"]] = r["title"] or ""
    return out

def collect(con, win_start, win_end):
    cals = load_calendars(con)
    events = []
    # Query a day wider each side so all-day events (stored at UTC midnight,
    # i.e. offset from their local date) near the boundary aren't dropped by
    # the SQL/expansion window; a precise local-overlap pass trims at the end.
    q_start = win_start - timedelta(days=1)
    q_end = win_end + timedelta(days=1)

    # Detached overrides: keyed by (master_id, orig_occurrence_epoch) → row.
    detached = {}
    for r in con.execute(
        "SELECT ROWID, summary, start_date, end_date, all_day, calendar_id, "
        "orig_item_id, orig_date, status, location_id "
        "FROM CalendarItem WHERE orig_item_id IS NOT NULL AND orig_item_id != 0 "
        "AND (hidden IS NULL OR hidden = 0) AND start_date IS NOT NULL"
    ):
        detached[(r["orig_item_id"], round(r["orig_date"] or 0))] = r

    # Exception (cancelled) occurrences per master.
    exceptions = {}
    for r in con.execute("SELECT owner_id, date FROM ExceptionDate"):
        exceptions.setdefault(r["owner_id"], set()).add(round(r["date"]))

    ws_cd = (q_start.timestamp() - CD_EPOCH)
    we_cd = (q_end.timestamp() - CD_EPOCH)

    # 1) Non-recurring (and non-detached) events overlapping the window.
    for r in con.execute(
        "SELECT ci.ROWID, ci.summary, ci.start_date, ci.end_date, ci.all_day, "
        "ci.calendar_id, ci.status, ci.location_id "
        "FROM CalendarItem ci "
        "LEFT JOIN Recurrence rc ON rc.owner_id = ci.ROWID "
        "WHERE rc.owner_id IS NULL AND (ci.orig_item_id IS NULL OR ci.orig_item_id = 0) "
        "AND (ci.hidden IS NULL OR ci.hidden = 0) "
        "AND ci.start_date IS NOT NULL AND ci.end_date IS NOT NULL "
        "AND ci.start_date < ? AND ci.end_date > ?",
        (we_cd, ws_cd),
    ):
        if r["calendar_id"] not in cals:
            continue
        ad = bool(r["all_day"])
        events.append(_row_to_event(r, cd_to_dt(r["start_date"], ad), cd_to_dt(r["end_date"], ad, is_end=True), cals))

    # 2) Recurring masters → expand.
    for r in con.execute(
        "SELECT ci.ROWID, ci.summary, ci.start_date, ci.end_date, ci.all_day, "
        "ci.calendar_id, ci.status, ci.location_id, "
        "rc.frequency, rc.interval, rc.specifier, rc.count, rc.end_date AS until "
        "FROM CalendarItem ci JOIN Recurrence rc ON rc.owner_id = ci.ROWID "
        "WHERE ci.start_date IS NOT NULL AND ci.end_date IS NOT NULL "
        "AND (ci.hidden IS NULL OR ci.hidden = 0)"
    ):
        if r["calendar_id"] not in cals:
            continue
        ad = bool(r["all_day"])
        m_start = cd_to_dt(r["start_date"], ad)
        m_end = cd_to_dt(r["end_date"], ad, is_end=True)
        duration = m_end - m_start
        until = cd_to_dt(r["until"]) if r["until"] and r["until"] > 0 else None
        exset = exceptions.get(r["ROWID"], set())
        # Expand from (window - duration) so a long occurrence that STARTS
        # before the window but overlaps into it is still generated.
        exp_start = q_start - duration if duration.days > 0 else q_start
        for occ in expand(m_start, r["frequency"], r["interval"], r["specifier"],
                          r["count"], until, exp_start, q_end):
            occ_cd = round(occ.timestamp() - CD_EPOCH)
            # cancelled?
            if _near(occ_cd, exset):
                continue
            # detached override?
            key = _find_detached(detached, r["ROWID"], occ_cd)
            if key is not None:
                continue  # the detached row is emitted separately below
            events.append(_row_to_event(r, occ, occ + duration, cals))

    # 3) Detached overrides that land in the window (as their moved selves).
    for (mid, odate), r in detached.items():
        if r["calendar_id"] not in cals:
            continue
        ad = bool(r["all_day"])
        s = cd_to_dt(r["start_date"], ad)
        e = cd_to_dt(r["end_date"], ad, is_end=True) if r["end_date"] else s
        if r["status"] == 2:  # cancelled detachment
            continue
        if s <= q_end and e >= q_start:
            events.append(_row_to_event(r, s, e, cals))

    # Precise local-space overlap trim to the real window.
    def overlaps(e):
        s = datetime.fromisoformat(e["start"])
        en = datetime.fromisoformat(e["end"])
        return s < win_end and en > win_start
    events = [e for e in events if overlaps(e)]
    events.sort(key=lambda ev: ev["start"])
    return events

def _near(cd, exset, tol=90):
    return any(abs(cd - x) <= tol for x in exset)

def _find_detached(detached, master_id, occ_cd, tol=90):
    for (mid, odate) in detached:
        if mid == master_id and abs(odate - occ_cd) <= tol:
            return (mid, odate)
    return None

def _row_to_event(r, start_dt, end_dt, cals):
    cal_name = cals.get(r["calendar_id"], "")
    title = r["summary"] or "(untitled)"
    # Auto-generated birthday calendars store just the contact name; EventKit
    # renders "<name>'s Birthday" (+ age, which lives in Contacts, not here).
    # Match the readable base form so Lautrec speaks it naturally.
    if cal_name.endswith("Birthdays") and "birthday" not in title.lower():
        title = f"{title}’s Birthday"
    return {
        "title": title,
        "start": dt_to_iso(start_dt),
        "end": dt_to_iso(end_dt),
        "allDay": bool(r["all_day"]) if "all_day" in r.keys() else False,
        "calendar": cal_name,
        "status": r["status"] if "status" in r.keys() else 0,
    }

# ---------------------------------------------------------------- commands

def out(obj, plain_fn=None, plain=False):
    if plain and plain_fn:
        plain_fn(obj)
    else:
        print(json.dumps(obj, indent=2, sort_keys=True))

def plain_events(obj):
    evs = obj.get("events", [])
    if not evs:
        print("Nothing on the calendar.")
        return
    for e in evs:
        when = "all-day" if e["allDay"] else e["start"][11:16]
        day = e["start"][:10]
        print(f"{day} {when}  {e['title']}  [{e['calendar']}]")

# ---------------------------------------------------------------- writes (CalDAV)
# Writes go over CalDAV (iCloud/Google), NOT the local DB: CalDAV is pure HTTPS
# with zero macOS TCC involvement, so it works headlessly under launchd where
# EventKit is dead (see the macOS/TCC notes in the README). Credentials
# come from the environment (set in the launchd plist), never hard-coded. The
# caldav/icalendar imports are lazy so the read path never depends on them.

CONFIG_PATH = os.environ.get(
    "LAUTREC_CONFIG",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "config.toml"),
)

def _caldav_creds():
    """CalDAV creds, house convention: config.toml [caldav] (same as mailctl's
    [gmail]/[fastmail]), with CALDAV_* env vars as an override for testing."""
    url = os.environ.get("CALDAV_URL")
    user = os.environ.get("CALDAV_USERNAME")
    pw = os.environ.get("CALDAV_PASSWORD")
    if not (user and pw):
        try:
            import tomllib
            with open(CONFIG_PATH, "rb") as f:
                cd = tomllib.load(f).get("caldav", {})
            url = url or cd.get("url")
            user = user or cd.get("username")
            pw = pw or cd.get("password")
        except (OSError, ValueError, ImportError):
            pass
    return (url or "https://caldav.icloud.com"), user, pw

def _caldav_connect():
    import caldav  # lazy: the reader must not require write deps
    url, user, pw = _caldav_creds()
    if not (user and pw) or "REPLACE-ME" in str(pw):
        raise SystemExit(json.dumps({"status": "error", "message":
            "calendar writes need CalDAV creds — set [caldav] username/password "
            f"in {CONFIG_PATH} (iCloud username = your Apple ID email; password = "
            "an app-specific password from appleid.apple.com). CALDAV_USERNAME/"
            "CALDAV_PASSWORD env vars override."}))
    client = caldav.DAVClient(url=url, username=user, password=pw)
    return client, client.principal()

def _find_calendar(principal, name):
    for c in principal.calendars():
        try:
            if (c.get_display_name() or "") == name:
                return c
        except Exception:
            continue
    return None

def _calendar_names(principal):
    names = []
    for c in principal.calendars():
        try:
            n = c.get_display_name()
            if n:
                names.append(n)
        except Exception:
            continue
    return sorted(names)

def _parse_alert(spec):
    """Alert spec → an iCalendar VALARM trigger. Relative forms fire BEFORE the
    event start (negative timedelta): '15m', '2h', '3d', '1w', '48h'. Absolute
    form fires at a wall-clock time: 'at:2026-07-20T09:00' (any parse_when input)
    — this is how 'day-of 9am' is expressed (the caller resolves the date)."""
    import re
    spec = spec.strip()
    if spec.startswith("at:"):
        dt = parse_when(spec[3:])
        return dt if dt.tzinfo else dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    m = re.fullmatch(r"(\d+)\s*([mhdw])", spec, re.IGNORECASE)
    if not m:
        raise SystemExit(json.dumps({"status": "error", "message":
            f"bad --alert {spec!r}: use e.g. 15m, 2h, 3d, 1w, or at:2026-07-20T09:00"}))
    n, unit = int(m.group(1)), m.group(2).lower()
    return -{"m": timedelta(minutes=n), "h": timedelta(hours=n),
             "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]

def cmd_add(rest, plain):
    """Create an event over CalDAV. High-confidence, no cross-system identity
    problem (unlike edit/delete of pre-existing events)."""
    import argparse
    import uuid
    from icalendar import Calendar as ICal, Event as IEvent, Alarm as IAlarm

    p = argparse.ArgumentParser(prog="calctl add", add_help=False)
    p.add_argument("--calendar", "--cal", dest="calendar", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end")
    p.add_argument("--duration", type=int, help="minutes; alternative to --end")
    p.add_argument("--all-day", dest="all_day", action="store_true")
    p.add_argument("--location")
    p.add_argument("--notes")
    p.add_argument("--alert", action="append", default=[],
                   help="repeatable; '15m'/'2h'/'3d'/'1w' before, or 'at:<when>' absolute")
    try:
        o = p.parse_args(rest)
    except SystemExit:
        raise SystemExit(json.dumps({"status": "error", "message":
            "usage: calctl add --calendar NAME --title T --start 'YYYY-MM-DD HH:MM' "
            "[--end ... | --duration MIN] [--all-day] [--location L] [--notes N] "
            "[--alert 15m] [--alert at:2026-07-20T09:00] ..."}))

    _client, principal = _caldav_connect()
    cal = _find_calendar(principal, o.calendar)
    if cal is None:
        raise SystemExit(json.dumps({"status": "error", "message":
            f"calendar {o.calendar!r} not found. Available: {_calendar_names(principal)}"}))

    uid = f"lautrec-{uuid.uuid4()}@lautrec"  # 'lautrec-' prefix marks our events
    vcal = ICal()
    vcal.add("prodid", "-//lautrec calctl//EN")
    vcal.add("version", "2.0")
    ev = IEvent()
    ev.add("uid", uid)
    ev.add("summary", o.title)
    if o.location:
        ev.add("location", o.location)
    if o.notes:
        ev.add("description", o.notes)

    if o.all_day:
        # all-day events are DATE-valued (no time, no tz)
        start = parse_when(o.start).date()
        end = parse_when(o.end).date() if o.end else start + timedelta(days=1)
        ev.add("dtstart", start)
        ev.add("dtend", end)
        s_iso, e_iso = start.isoformat(), end.isoformat()
    else:
        # attach the current local offset so the instant is unambiguous on the
        # server (icalendar emits aware datetimes correctly; iCloud renders in
        # the viewer's zone)
        tz = datetime.now().astimezone().tzinfo
        start = parse_when(o.start).replace(tzinfo=tz)
        if o.duration:
            end = start + timedelta(minutes=o.duration)
        elif o.end:
            end = parse_when(o.end).replace(tzinfo=tz)
        else:
            end = start + timedelta(hours=1)
        ev.add("dtstart", start)
        ev.add("dtend", end)
        s_iso, e_iso = start.isoformat(), end.isoformat()

    alerts = []
    for spec in o.alert:
        trigger = _parse_alert(spec)
        al = IAlarm()
        al.add("action", "DISPLAY")
        al.add("description", o.title)  # DISPLAY alarms require a description
        if hasattr(trigger, "isoformat"):     # absolute datetime → emit as UTC
            trigger = trigger.astimezone(timezone.utc)  # unambiguous across clients
            al.add("trigger", trigger)
            alerts.append(trigger.isoformat())
        else:                                 # relative timedelta before start
            al.add("trigger", trigger)
            alerts.append(spec)
        ev.add_component(al)

    vcal.add_component(ev)
    created = cal.save_event(vcal.to_ical().decode())

    obj = {"status": "ok", "action": "add", "uid": uid, "url": str(created.url),
           "calendar": o.calendar, "title": o.title,
           "start": s_iso, "end": e_iso, "allDay": bool(o.all_day),
           "alerts": alerts}
    out(obj, lambda x: print(
        f"Added “{x['title']}” to {x['calendar']} "
        f"({'all day ' + x['start'] if x['allDay'] else x['start'][:16]})."), plain)

def main():
    args = sys.argv[1:]
    plain = "--plain" in args
    args = [a for a in args if a not in ("--plain",)]
    if "--format" in args:
        i = args.index("--format")
        plain = args[i + 1].lower() != "json"
        del args[i:i + 2]
    cmd = args[0] if args else "today"
    rest = args[1:]

    # Write commands travel over CalDAV — no local DB / Full Disk Access needed,
    # so branch BEFORE connect() (which would demand FDA).
    if cmd == "add":
        return cmd_add(rest, plain)

    now = datetime.now()
    con = connect()

    if cmd in ("calendars", "cals"):
        cals = load_calendars(con)
        obj = {"status": "ok", "calendars": sorted(cals.values())}
        out(obj, lambda o: print("\n".join(o["calendars"])), plain)
        return

    if cmd == "today":
        ws = now.replace(hour=0, minute=0, second=0, microsecond=0)
        we = ws + timedelta(days=1)
    elif cmd == "day":
        base = parse_when(rest[0] if rest else "today")
        ws = base
        we = base + timedelta(days=1)
    elif cmd == "week":
        ws = now.replace(hour=0, minute=0, second=0, microsecond=0)
        we = ws + timedelta(days=7)
    elif cmd in ("range", "free"):
        if len(rest) < 2:
            raise SystemExit(json.dumps({"status": "error", "message": f"usage: calctl {cmd} <from> <to>"}))
        ws = parse_when(rest[0])
        we = parse_when(rest[1], end_of_day=True)
    elif cmd == "next":
        ws = now
        we = now + timedelta(days=60)
    else:
        raise SystemExit(json.dumps({"status": "error",
            "message": f"unknown command: {cmd}. Commands: today, day, week, next, range, free, calendars, add"}))

    events = collect(con, ws, we)

    if cmd == "next":
        # the next event that STARTS ahead (an already-ongoing multi-day event
        # isn't "next"); events are already sorted by start.
        upcoming = [e for e in events if e["start"] >= dt_to_iso(now)]
        e = upcoming[0] if upcoming else None
        out({"status": "ok", "event": e},
            lambda o: print(f"{o['event']['start'][:16]}  {o['event']['title']}" if o["event"] else "Nothing coming up."),
            plain)
        return

    if cmd == "free":
        busy = [e for e in events if not e["allDay"]]
        out({"status": "ok", "window": {"start": dt_to_iso(ws), "end": dt_to_iso(we)}, "busy": busy},
            lambda o: print("Free the whole window." if not o["busy"]
                            else "Busy:\n" + "\n".join(f"  {b['start'][:16]}–{b['end'][11:16]}  {b['title']}" for b in o["busy"])),
            plain)
        return

    out({"status": "ok", "count": len(events), "events": events}, plain_events, plain)

if __name__ == "__main__":
    main()
