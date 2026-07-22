---
name: maps
description: Hand the owner a tappable Apple Maps link that starts directions to a destination on their phone. Invoke when a message asks to navigate, get directions, "send me directions to X", "how do I get to Y", or start a drive/route to a place.
when-to-invoke: |
  - "directions to X", "navigate to / take me to X", "send me a route to X".
  - "start driving to X on my phone".
  - Any ask to put a place into Maps on their phone for turn-by-turn.
determinism-level: high
tools-used: URL construction only (Apple Maps universal link); delivery via iMessage link or the notify CLI --click
---

# maps — Apple Maps deep links

No API, no credentials, no tool to install — Apple Maps takes a universal
link that opens straight into directions. You build the URL and deliver it;
tapping it on the owner's phone launches Maps with the route loaded.

## Build the link

```
https://maps.apple.com/?daddr=<destination>&dirflg=d
```

- `daddr` — the destination. URL-encode it: an address, a place name, or
  `lat,lon`. (e.g. `daddr=1%20Infinite%20Loop,%20Cupertino`)
- `dirflg` — travel mode: `d` driving (default), `w` walking, `r` transit.
- Add `saddr=<origin>` only if the owner names a start point; omit it and
  Maps uses their current location.

## Deliver it

- **Simplest:** put the link in the iMessage reply. iMessage makes it
  tappable. "Here's the route — tap to start: <url>"
- **Richer / when it should land as a notification:** fire it through the
  notify CLI with `--click` so the push itself opens Maps:
  `notify lautrec "Directions to <place>" "Tap to start driving" --click "<url>"`

## Scope

This covers "send a link to start directions" — the stated case. It does
NOT compute ETA, travel time, or do place search in-chat; that needs the
Google Maps Platform Directions API (key + billing) and is out of scope
until the owner asks for in-chat ETAs. Don't reach for it speculatively.
