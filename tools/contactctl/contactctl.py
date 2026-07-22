#!/usr/bin/env python3
"""contactctl — Apple Contacts name→handle resolver, runtime reader.

Reads the AddressBook SQLite DBs directly (read-only) — the calctl pattern,
the ONLY approach proven to work in Lautrec's headless `claude -p`/launchd
runtime. Any CNContactStore/AddressBook.framework tool is TCC-Contacts-prompt-gated
(the same dead-headless class as EventKit), so this reads the backing
`AddressBook-v22.abcddb` files instead and ships as a standalone binary with
its own Full Disk Access grant (see build.sh).

Contacts are split across account "sources" (iCloud, on-my-Mac, …), each its
own abcddb — this unions all of them. Read-only: resolving a name is free;
SENDING to the resolved handle is a write and needs a yes (persona rule).

    contactctl find "<name>" [--format json|plain]
"""
import glob
import json
import os
import re
import sqlite3
import sys

AB_DIR = os.path.expanduser("~/Library/Application Support/AddressBook")

def source_dbs():
    dbs = glob.glob(os.path.join(AB_DIR, "Sources", "*", "AddressBook-v22.abcddb"))
    top = os.path.join(AB_DIR, "AddressBook-v22.abcddb")
    if os.path.exists(top):
        dbs.append(top)
    return dbs

def open_ro(path):
    """Copy the DB (+wal/shm) to temp and open — avoids the protected-container
    `-shm` write that flakes under launchd (same fix as calctl)."""
    import shutil, tempfile, atexit
    tmp = tempfile.mkdtemp(prefix="contactctl-")
    atexit.register(lambda: shutil.rmtree(tmp, ignore_errors=True))
    dst = os.path.join(tmp, "ab.abcddb")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            shutil.copy2(path + suffix, dst + suffix)
    con = sqlite3.connect(dst, timeout=5)
    con.row_factory = sqlite3.Row
    return con

def norm_phone(raw):
    """Normalize to E.164 for iMessage where possible; keep as-is otherwise."""
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("+"):
        return "+" + re.sub(r"\D", "", s)
    digits = re.sub(r"\D", "", s)
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return s  # short codes / international without + — leave verbatim

def full_name(r):
    parts = [r["ZFIRSTNAME"], r["ZLASTNAME"]]
    name = " ".join(p for p in parts if p).strip()
    return name or (r["ZORGANIZATION"] or "").strip()

def collect(query):
    q = query.lower().strip()
    out = {}  # key → contact dict, deduped across sources
    for dbpath in source_dbs():
        try:
            con = open_ro(dbpath)
        except (sqlite3.Error, OSError):
            continue
        try:
            recs = {}
            for r in con.execute(
                "SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION, ZNICKNAME "
                "FROM ZABCDRECORD "
                "WHERE ZFIRSTNAME IS NOT NULL OR ZLASTNAME IS NOT NULL OR ZORGANIZATION IS NOT NULL"
            ):
                name = full_name(r)
                hay = " ".join(x for x in (r["ZFIRSTNAME"], r["ZLASTNAME"],
                              r["ZORGANIZATION"], r["ZNICKNAME"], name) if x).lower()
                if q and q not in hay:
                    continue
                recs[r["Z_PK"]] = {"name": name, "org": r["ZORGANIZATION"] or "",
                                   "phones": [], "emails": []}
            if not recs:
                continue
            pks = tuple(recs.keys())
            marks = ",".join("?" * len(pks))
            for p in con.execute(
                f"SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER WHERE ZOWNER IN ({marks})", pks):
                n = norm_phone(p["ZFULLNUMBER"])
                if n and n not in recs[p["ZOWNER"]]["phones"]:
                    recs[p["ZOWNER"]]["phones"].append(n)
            for e in con.execute(
                f"SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS WHERE ZOWNER IN ({marks})", pks):
                addr = (e["ZADDRESS"] or "").strip().lower()
                if addr and addr not in recs[e["ZOWNER"]]["emails"]:
                    recs[e["ZOWNER"]]["emails"].append(addr)
            for pk, c in recs.items():
                out[(dbpath, pk)] = c
        finally:
            con.close()
    return _merge(list(out.values()))

def _merge(contacts):
    """Collapse the same person duplicated across account sources: drop records
    with no handle at all, then merge same-name records that share ≥1 handle
    (union their phones/emails). Same name but disjoint handles stay separate —
    could be different people, so let the caller disambiguate rather than
    guess a wrong number."""
    contacts = [c for c in contacts if c["phones"] or c["emails"]]
    merged = []
    for c in contacts:
        handles = set(c["phones"]) | set(c["emails"])
        hit = None
        for m in merged:
            if m["name"].lower() == c["name"].lower() and (
                    set(m["phones"]) | set(m["emails"])) & handles:
                hit = m
                break
        if hit:
            for p in c["phones"]:
                if p not in hit["phones"]:
                    hit["phones"].append(p)
            for e in c["emails"]:
                if e not in hit["emails"]:
                    hit["emails"].append(e)
        else:
            merged.append(dict(c))
    for c in merged:
        c["imessage"] = (c["phones"][0] if c["phones"]
                         else (c["emails"][0] if c["emails"] else None))
        c.pop("_", None)
    return sorted(merged, key=lambda c: c["name"].lower())

def main():
    args = sys.argv[1:]
    plain = "--plain" in args
    args = [a for a in args if a != "--plain"]
    if "--format" in args:
        i = args.index("--format")
        plain = args[i + 1].lower() != "json"
        del args[i:i + 2]
    cmd = args[0] if args else ""
    if cmd != "find" or len(args) < 2:
        print(json.dumps({"status": "error", "message": 'usage: contactctl find "<name>"'}))
        sys.exit(2)
    matches = collect(args[1])
    if plain:
        if not matches:
            print("No contact found.")
        for c in matches:
            handle = c["imessage"] or "(no handle)"
            extra = f"  +{len(c['phones'])+len(c['emails'])-1} more" if (len(c['phones'])+len(c['emails'])) > 1 else ""
            print(f"{c['name']}  →  {handle}{extra}")
    else:
        print(json.dumps({"status": "ok", "count": len(matches), "contacts": matches},
                         indent=2, ensure_ascii=False))
    sys.exit(0 if matches else 1)

if __name__ == "__main__":
    main()
