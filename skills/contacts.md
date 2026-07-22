---
name: contacts
description: Resolve a person's name to an iMessage handle / phone / email from Apple Contacts. Invoke when a message names a person to reach — "text so-and-so", "email <name>", "what's <name>'s number" — and you need to turn the name into an address.
when-to-invoke: |
  - "text / message / email <person>" — resolve the name to a handle first.
  - "what's <person>'s number / email".
  - Any action that needs a contact's address before it can run.
determinism-level: high
tools-used: contactctl CLI (~/.local/bin/contactctl, --format json) — read-only
---

# contacts — Apple Contacts name → handle via contactctl

The lookup layer that unlocks the messaging archetype: before Lautrec can
"text Alex" it resolves "Alex" to a phone/email. `contactctl` reads the
AddressBook DBs directly (all account sources unioned, cross-source duplicates
merged, phones normalized to E.164), the same direct-DB pattern as `calctl`.

Binary: `~/.local/bin/contactctl`. First call in a while takes ~2s (the
self-contained binary unpacks); that's normal.

## Resolving (free — a read)

```
contactctl find "<name>" --format json     # matches with phones/emails/handle
contactctl find "<name>" --plain           # quick human lines
```

Match is a case-insensitive substring over first/last/org/nickname, so
`find "alex"` or `find "Alex Rivera"` both work. Each result has `name`,
`org`, `phones` (E.164), `emails`, and `imessage` (the preferred handle — first
phone, else first email).

- **One match** → use its `imessage` handle for the messaging/mail path.
- **Multiple matches** → ask the owner which one; never pick for them. **Never
  send to a number you inferred rather than looked up.**
- **No match** → say so; don't guess an address.

## Boundary

Read-only. Resolving is free; *sending* to the resolved handle is a write and
needs a yes (persona rule). Adding/editing contacts is not supported (that
needs the Contacts framework, which is dead in the headless runtime — same as
EventKit; see `project_lautrec_eventkit_headless`).

## If it errors

`Full Disk Access needed` / empty for everyone → the `contactctl` binary lost
its FDA grant (usually after a rebuild). Tell the owner to re-add
`~/.local/bin/contactctl` under System Settings › Privacy & Security › Full
Disk Access. Don't work around it.
