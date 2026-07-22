---
name: calendar
description: Read AND write the owner's calendar — what's on today/this week, when he's free, next event; and add events. Covers every calendar the Mac holds (personal AND work) with recurring events expanded. Invoke for "what's on my calendar", "am I free at X", "when's my next meeting", "what's tomorrow look like", "add/put X on my calendar", "schedule …", scheduling questions.
when-to-invoke: |
  - "what's on my calendar / on today / this week", "what's my day look like".
  - "am I free at <time>", "when's my next meeting", "do I have anything at X".
  - "add / put / schedule <event>" on a calendar (see Writing below).
  - Scheduling and availability questions.
determinism-level: high
tools-used: calctl CLI (~/.local/bin/calctl, --format json) — reads local DB; writes via CalDAV
---

# calendar — Apple Calendar via calctl

`calctl` **reads** the macOS Calendar database directly (all calendars the Mac
account holds — personal AND work — with recurring events expanded), the same
way `remctl` reads Reminders; reads are validated against EventKit 0-diff over
30 days. It also **writes** (adds events) over CalDAV — see Writing below.

**Always invoke the compiled binary at `~/.local/bin/calctl`. NEVER run the
`calctl.py` source directly** (e.g. `python3 …/calctl.py`): the `caldav` and
`icalendar` dependencies the write path needs are bundled only inside the
compiled binary, so running the source throws `No module named 'icalendar'`.
If `calctl` ever reports a missing Python module, you ran the source — call
`~/.local/bin/calctl` instead; do not `pip install` anything.

Always pass `--format json` for parsing; use `--plain` only when echoing a
quick human line. First call in a while takes ~2s (self-contained binary
unpacks); that's normal.

## Reading (free — no authorization needed)

```
calctl today --format json            # today's events across all calendars
calctl day <date> --format json       # a specific day (YYYY-MM-DD, "tomorrow")
calctl week --format json             # next 7 days
calctl next --format json             # the next upcoming event
calctl range <from> <to> --format json   # events in a window
calctl free <from> <to> --format json    # busy blocks in a window (infer gaps)
calctl calendars --format json        # list calendar names
```

Dates accept `YYYY-MM-DD`, `YYYY-MM-DD HH:MM`, and `today`/`tomorrow`/
`yesterday`/`now`. Each event has `title`, `start`, `end` (local ISO),
`allDay`, `calendar`, `status`.

Turn JSON into a short plain-text iMessage — never paste raw JSON. "3 things
today: standup at 9, dentist at 10, project review at noon."
Lead with times; name the calendar only when it disambiguates (work vs
personal).

## Writing — adding events (CalDAV)

Writes go over CalDAV (iCloud), NOT EventKit — pure HTTPS, so they work in the
headless runtime where EventKit is dead. The event syncs back down to every
device within seconds.

```
calctl add --calendar "Personal" --title "Dinner with Sam" \
           --start "2026-07-20 19:00" --duration 90 --format json
calctl add --cal "Personal" --title "Flight to NYC" \
           --start "2026-07-25" --all-day --format json
```

- `--calendar` (required) — must match a calendar name exactly (use
  `calctl calendars` to see them). Only iCloud/CalDAV calendars are
  writable; subscribed and Exchange calendars are not.
- `--title`, `--start` (required). `--start` takes `YYYY-MM-DD HH:MM` (or
  `YYYY-MM-DD` with `--all-day`).
- End time: `--duration <minutes>` OR `--end "<when>"`. Neither → defaults to
  1 hour (timed) / 1 day (all-day).
- Optional: `--location`, `--notes`.
- `--alert` — repeatable, one VALARM each. Two forms:
  - **relative** (fires before start): `15m`, `2h`, `3d`, `1w` → e.g. `--alert 15m`.
  - **absolute** (fires at a wall-clock time): `at:<when>` → e.g.
    `--alert at:2026-07-20T09:00`.
- Returns JSON with `uid`, `url`, and `alerts` on success. Confirm to the owner in
  plain words: "Added Dinner with Sam to Personal, Mon Jul 20 at 7pm, alert 15m
  before."

### Alerts — translating plain language

Alerts are CalDAV-native (not Apple-app-only). Map the owner's words to `--alert`
flags, one per alert:

- "remind me 30 min before" → `--alert 30m`
- "alert an hour and a day before" → `--alert 1h --alert 1d`
- "**48 hours before and day-of at 9am**" → `--alert 48h --alert at:<START-DATE>T09:00`
  (resolve `<START-DATE>` to the event's own date, e.g. the event is Jul 28 →
  `--alert 48h --alert at:2026-07-28T09:00`). "Day-of at TIME" is always the
  absolute form anchored on the event's start date.
- "morning of" (all-day event) → `--alert at:<START-DATE>T09:00`.

**Default when the owner says nothing about alerts:** add one `--alert 15m` for
**timed** events; **no** alert for all-day events. Honor any explicit
instruction exactly instead of the default (including "no alert").

**Only iCloud/Google calendars are writable.** A Microsoft/Outlook work
calendar (if any) can't be written this way — say so and offer a reminder
instead. **Editing and deleting existing events aren't built yet** — if asked
to move or cancel, say adding works but changes don't yet, and offer to add a
corrected event or capture a reminder.

If the owner asks to move/cancel and you can't, don't pretend — offer to
capture a reminder or a note instead.

## If it errors

- `Full Disk Access needed` → the `calctl` binary lost its FDA grant (usually
  after a rebuild). Tell the owner to re-add `~/.local/bin/calctl` under System
  Settings › Privacy & Security › Full Disk Access. Don't work around it.
- `calendar writes need CalDAV creds` → the `[caldav]` section in
  `config.toml` is missing or still `REPLACE-ME`. Tell the owner; don't retry.
- `calendar '<name>' not found` (on add) → the name didn't match. Run `calctl
  calendars` and use an exact name.
- Empty result is a valid answer ("nothing on your calendar today") — say so.
