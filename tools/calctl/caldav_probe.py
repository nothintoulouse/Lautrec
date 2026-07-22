#!/usr/bin/env python3
"""caldav_probe — proof harness for headless CalDAV writes to iCloud/Google.

The point: CalDAV is pure HTTPS with NO macOS TCC involvement, so — unlike
EventKit — it does not depend on launch context. If this round-trips from any
shell, it round-trips under launchd. Once green, this connect/create/delete
logic becomes the core of calctl's write path.

Credentials come from the environment (never hard-coded):
  CALDAV_URL       e.g. https://caldav.icloud.com  (default)
  CALDAV_USERNAME  the Apple ID email (iCloud) or account email (Google)
  CALDAV_PASSWORD  an APP-SPECIFIC password (appleid.apple.com → Sign-In & Security)
  CALDAV_CALENDAR  (optional) name of the calendar to write the test event to;
                   if unset, uses the first writable calendar discovered

Run inside an ephemeral venv:
  python3 -m venv /tmp/caldav-venv && /tmp/caldav-venv/bin/pip -q install caldav icalendar
  CALDAV_USERNAME=... CALDAV_PASSWORD=... /tmp/caldav-venv/bin/python caldav_probe.py
"""

import os
import sys
from datetime import datetime, timedelta

import caldav
from caldav.elements import dav
from icalendar import Calendar, Event


def env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        sys.exit(f"missing env {name}")
    return v


def main():
    url = env("CALDAV_URL", "https://caldav.icloud.com")
    username = env("CALDAV_USERNAME", required=True)
    password = env("CALDAV_PASSWORD", required=True)
    target_name = env("CALDAV_CALENDAR")

    print(f"connecting to {url} as {username} ...")
    client = caldav.DAVClient(url=url, username=username, password=password)
    principal = client.principal()
    calendars = principal.calendars()
    if not calendars:
        sys.exit("no calendars discovered — check credentials / URL")

    print(f"discovered {len(calendars)} calendars:")
    named = {}
    for c in calendars:
        try:
            name = c.get_display_name()
        except Exception:
            name = "(unnamed)"
        named[name] = c
        print(f"  - {name}")

    # pick the target calendar
    if target_name:
        cal = named.get(target_name)
        if cal is None:
            sys.exit(f"calendar {target_name!r} not found among: {list(named)}")
    else:
        cal = calendars[0]
        target_name = next((n for n, c in named.items() if c is cal), "?")
    print(f"\nwriting test event to: {target_name}")

    # build a throwaway event ~1h from now
    start = datetime.now().replace(microsecond=0) + timedelta(hours=1)
    end = start + timedelta(minutes=30)
    vcal = Calendar()
    vcal.add("prodid", "-//lautrec caldav_probe//EN")
    vcal.add("version", "2.0")
    ev = Event()
    ev.add("summary", "LAUTREC CALDAV PROBE — safe to delete")
    ev.add("dtstart", start)
    ev.add("dtend", end)
    ev.add("uid", "lautrec-caldav-probe-DO-NOT-KEEP@lautrec")
    vcal.add_component(ev)

    created = cal.save_event(vcal.to_ical().decode())
    print(f"  created: {created.url}")

    try:
        # read it back by its own URL (iCloud's UID-query is unreliable; the
        # object URL is the load-bearing handle we'll persist for edits/deletes)
        found = caldav.Event(client=client, url=str(created.url))
        found.load()
        assert "LAUTREC CALDAV PROBE" in found.data
        print(f"  read-back OK: {found.url}")
    finally:
        # always clean up, even if read-back asserts
        created.delete()
        print("  deleted OK — round-trip clean")

    print("\nPROOF GREEN: headless CalDAV create/read/delete works.")


if __name__ == "__main__":
    main()
