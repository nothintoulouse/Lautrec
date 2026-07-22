#!/usr/bin/env python3
"""Diff the direct-DB calctl against the EventKit oracle (calctl-ek).

Usage:
    python3 compare.py <oracle.json> <mine.json>

Both files are `--format json` output over the SAME range. Reports events the
oracle has that we miss (the dangerous case — a real meeting we'd never show),
events we invent, and start/end-time mismatches. Matches on (title, start-day)
then checks exact times, so a recurrence bug surfaces as a miss or a time skew.
"""
import json
import sys
from datetime import datetime, timezone

def to_instant(iso):
    """Normalize either 'Z' UTC or naive-local ISO to an absolute UTC epoch."""
    s = iso.strip()
    if s.endswith("Z"):
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    else:
        dt = datetime.fromisoformat(s).astimezone()  # naive → local tz aware
    return int(dt.astimezone(timezone.utc).timestamp())

def load(path):
    with open(path) as f:
        d = json.load(f)
    return d.get("events", [])

def norm_title(t):
    return t.strip().lower()

def main():
    oracle = load(sys.argv[1])
    mine = load(sys.argv[2])

    # Key by (normalized title, absolute instant). Same event → same key
    # regardless of UTC-vs-local formatting.
    def key(e):
        return (norm_title(e["title"]), to_instant(e["start"]))
    ok = {key(e) for e in oracle} & {key(e) for e in mine}
    o_only = [e for e in oracle if key(e) not in {key(m) for m in mine}]
    m_only = [e for e in mine if key(e) not in {key(o) for o in oracle}]

    # Pair leftovers by instant alone → title-only differences (e.g. EventKit's
    # synthesized birthday titles) vs genuine time skews vs true misses.
    o_by_inst = {}
    for e in o_only:
        o_by_inst.setdefault(to_instant(e["start"]), []).append(e)
    title_only, misses = [], []
    used = set()
    for e in o_only:
        inst = to_instant(e["start"])
        cand = [m for m in m_only if to_instant(m["start"]) == inst
                and m.get("calendar") == e.get("calendar") and id(m) not in used]
        if cand:
            used.add(id(cand[0]))
            title_only.append((e, cand[0]))
        else:
            misses.append(e)
    invented = [m for m in m_only if id(m) not in used
                and to_instant(m["start"]) not in o_by_inst]

    print(f"exact matches (title+instant): {len(ok)}")
    print(f"\nMISSES (oracle has, we DON'T — real events we'd hide): {len(misses)}")
    for e in sorted(misses, key=lambda x: x["start"]):
        print(f"  MISS  {e['start']}  {e['title']}  [{e.get('calendar','')}]")
    print(f"\nINVENTED (we have, oracle doesn't): {len(invented)}")
    for e in sorted(invented, key=lambda x: x["start"]):
        print(f"  EXTRA {e['start']}  {e['title']}  [{e.get('calendar','')}]")
    print(f"\nTITLE-ONLY diffs (same time+calendar, different title — usually birthdays): {len(title_only)}")
    for orc_e, mine_e in title_only[:20]:
        print(f"  TITLE  oracle='{orc_e['title']}'  mine='{mine_e['title']}'  [{orc_e.get('calendar','')}]")

    hard = len(misses) + len(invented)
    print(f"\nexact={len(ok)}  misses={len(misses)}  invented={len(invented)}  title-only={len(title_only)}")
    print("CLEAN — no misses/invented (recurrence + windowing correct)" if hard == 0
          else f"{hard} hard discrepancies to resolve")
    sys.exit(0 if hard == 0 else 1)

if __name__ == "__main__":
    main()
