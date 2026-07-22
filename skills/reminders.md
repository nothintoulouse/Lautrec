---
name: reminders
description: Read and write the owner's Apple Reminders via the remctl CLI — what's due today/upcoming/overdue, add a reminder, complete one, get a deep link. Invoke whenever a message is about tasks, to-dos, reminders, "remind me", "what's on my list", "did I finish X", or capturing something to do later.
when-to-invoke: |
  - "remind me to…", "add a task/reminder", "put X on my list".
  - "what's due / on my list / on my plate", "what's overdue", "anything today".
  - "mark X done", "I finished Y", "check off Z".
  - Any capture of a future action that belongs in Reminders.
determinism-level: high
tools-used: remctl CLI (~/.local/bin/remctl, --format json)
---

# reminders — Apple Reminders via remctl

> **Note:** `remctl` is an external Reminders CLI and is **not** shipped in
> this repo. This skill is kept as a worked example of the pattern — a skill
> file that wraps a CLI the assistant is allowed to call. Swap in whatever
> task CLI you use, or delete the skill.

`remctl` reads the local iCloud Reminders database directly (fast, full
detail) and writes through Apple's EventKit so changes sync normally to
every device.

Binary: `~/.local/bin/remctl`. Always pass `--format json` for parsing;
drop it (or use `--format plain`) only if you are echoing raw output.

## Reading (free — no authorization needed)

```
remctl today --format json          # due today + overdue (the common one)
remctl upcoming --format json        # next 7 days (append N for N days)
remctl overdue --format json         # everything past due
remctl show "<list>" --format json   # one list, grouped by section
remctl search "<query>" --format json
remctl flagged --format json
remctl lists --format json           # all iCloud lists
remctl info <id> --format json       # full detail on one reminder
```

Turn JSON into a short plain-text iMessage — never paste raw JSON to
the owner. "3 due today: call the vendor, send deposit, confirm headcount."

## Writing (needs a yes — it mutates their Reminders)

```
remctl add "<title>" --list "<list>" --due "<when>"   # natural-language due ok
remctl done "<id-or-title>"                            # complete
remctl edit "<id>" --title "…" --due "…"
remctl link "<id-or-title>"                            # deep link back to the app
```

Per the persona's authorization rule, confirm before any add/done/edit:
"About to add 'call the vendor' to your Today list — ok?" If their message
already carried the go-ahead ("remind me to call the vendor tomorrow,
yes"), that's your yes — don't make them say it twice.

## Notes

- `remctl --help` lists the full command set (sections, subtasks, tags,
  templates, smart lists). Reach for those only when the ask needs them;
  `today` / `upcoming` / `add` / `done` cover almost everything.
- If `remctl` errors with a permissions message, it needs Reminders +
  Full Disk Access granted on this Mac. Surface that to the owner; don't
  try to work around it.
- Deep links (`remctl link`) are the right way to hand the owner a tappable
  pointer into Reminders.app from an iMessage.
