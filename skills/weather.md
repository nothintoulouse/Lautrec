---
name: weather
description: Answer a quick weather question — current conditions or the forecast for a place. Invoke for "what's the weather", "will it rain", "how cold is it", "forecast for <place/this weekend>".
when-to-invoke: |
  - "what's the weather", "is it going to rain", "how hot/cold is it".
  - "forecast for <place>", "weather <day/weekend>".
determinism-level: medium
tools-used: Apple WeatherKit or a light weather API — SEE PROVISIONING below
---

# weather — current conditions + forecast

Low-stakes retrieve-and-answer. Apple-native-first (WeatherKit) per the
cross-cutting principle, with a light HTTP API as the easy fallback.

## ⚠ PROVISIONING — not live yet (surface, don't guess)

No weather tool is wired. Two paths:

1. **WeatherKit** (apple-native) — needs an Apple Developer key (JWT) and
   a small caller. Most native, more setup.
2. **A light API** (e.g. Open-Meteo, no key) — fastest to wire; good
   enough for "will it rain." Lowest friction for a low-stakes feature.

Until one is wired, say you don't have weather hooked up yet rather than
guessing conditions. This is low priority — fine to leave for last.

## Intended shape

A place (default the owner's locale once one is available) →
current temp + conditions, or a day/range forecast. One or two plain-text
sentences back. Never a wall of hourly rows in an iMessage.
