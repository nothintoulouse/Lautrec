#!/usr/bin/env python3
"""watcher.py — Lautrec's proactive eye on long-running work.

Lautrec only wakes on inbound iMessage, so it cannot notice that a
background session finished, needs the owner, or died without a handoff —
those happen *outside* the message loop. This process is that eye: a
launchd poller (StartInterval=60) that reads manifest.md's Active list
each tick and pushes when an entry crosses a trigger.

manifest.md is yours to write: a plain markdown file with an `## Active`
section and one `## <name> — <summary>` entry per running job, each
carrying `- session:`, `- expected handoff:`, and `- open-for-owner:`
lines. See the manifest section of the README for the exact shape.

Dormancy is free: zero Active entries -> exit immediately (~0ms). No
load/unload churn, no heartbeat. The manifest IS the on/off switch.

On a trigger it does two things:
  1. optionally shells out to a notification CLI (config
     [watcher].notify_bin — anything that takes
     `<topic> <title> <message>`), and
  2. spools a *watcher-labelled* event into queue/, so pa.py wakes
     Lautrec to compose a natural-language outreach iMessage.

The queue event self-identifies (kind="watcher", speaker not the owner):
Lautrec must read it as "you noticed this, reach out" — never as a
message the owner sent. See pa.py's speaker_for / invoke_claude footer.

De-dupe is a gitignored sidecar (.watcher-state.json): each
(entry, trigger) fires once, not every 60s. Stdlib only; state is plain
files. See AGENTS.md.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import tomllib
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.toml"
MANIFEST_PATH = ROOT / "manifest.md"
QUEUE_DIR = ROOT / "queue"
THREADS_DIR = ROOT / "threads"
LOG_DIR = ROOT / "logs"
STATE_PATH = ROOT / ".watcher-state.json"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("rb") as fh:
        return tomllib.load(fh)


CONFIG = load_config()
WATCHER_CFG = CONFIG.get("watcher", {})
OWNER_NAME = CONFIG.get("owner", {}).get("name", "the owner")
# Optional push-notification hook. Any CLI taking
# `<topic> <title> <message> --priority <p> --tag <t>` works. Unset means
# no push, leaving the iMessage outreach as the only surface.
_notify_bin = WATCHER_CFG.get("notify_bin", "").strip()
NOTIFY_BIN = Path(_notify_bin).expanduser() if _notify_bin else None
NOTIFY_TOPIC = WATCHER_CFG.get("notify_topic", "lautrec")

# Manifest h2 headings that delimit sections rather than name an entry.
# Entry headings always carry the " — " separator; section headings never
# do, so the parser keys off both signals.
SECTION_NAMES = {
    "rules", "entry format",
    "active", "recent", "next (queued)", "known breakage",
}
# open-for-owner values that mean "nothing pending" — not a trigger.
EMPTY_TOKENS = {"", "_none_", "none", "-", "—", "n/a", "tbd", "_tbd_"}


def log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} watcher: {msg}"
    print(line, flush=True)
    with (LOG_DIR / f"{time.strftime('%Y-%m-%d')}.log").open("a") as fh:
        fh.write(line + "\n")


# --- manifest parsing -----------------------------------------------------

def _section_name(heading: str) -> str | None:
    """If this h2 heading names a section, return its lowercased key. Entry
    headings (which contain ' — ') are never section names."""
    if "—" in heading or " - " in heading:
        return None
    key = heading.strip().lower()
    for name in SECTION_NAMES:
        if key.startswith(name):
            return name
    return None


def parse_active_entries(text: str) -> list[dict]:
    """Return one dict per entry under '## Active'. Each dict has 'id' (the
    heading line) plus every '- key: value' pair, keys lowercased. Multi-
    line values (continuation lines that aren't a new '- key') are joined."""
    entries: list[dict] = []
    current = None  # current section key
    # Split on h2 boundaries; the entry-format code fence also contains a
    # line beginning '## ', but it lands under the 'entry format' section,
    # so current != 'active' and it is correctly ignored.
    for block in re.split(r"(?m)^## ", text):
        if not block.strip():
            continue
        lines = block.splitlines()
        heading = lines[0].strip()
        section = _section_name(heading)
        if section is not None:
            current = section
            continue
        if current != "active":
            continue
        entry: dict = {"id": heading}
        key = None
        for raw in lines[1:]:
            line = raw.strip()
            m = re.match(r"^-\s*([^:]+):\s*(.*)$", line)
            if m:
                key = m.group(1).strip().lower()
                entry[key] = m.group(2).strip()
            elif key and line and not line.startswith(("##", "<!--")):
                entry[key] = (entry.get(key, "") + " " + line).strip()
        entries.append(entry)
    return entries


# --- triggers -------------------------------------------------------------

def _expand(p: str) -> Path:
    return Path(p.strip()).expanduser()


def tmux_alive(session: str) -> bool:
    try:
        r = subprocess.run(
            ["tmux", "has-session", "-t", session],
            capture_output=True, timeout=10,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        # No tmux / can't tell → don't cry failure on a maybe.
        return True


def evaluate(entry: dict) -> tuple[str, str] | None:
    """Return (trigger, human_message) for the first fired trigger, else
    None. Triggers: 'done' (handoff landed), 'needs-owner'
    (open-for-owner populated), 'failed' (tmux dead, no handoff)."""
    handoff = entry.get("expected handoff", "")
    handoff_exists = bool(handoff) and _expand(handoff).exists()
    scope = entry["id"]

    if handoff_exists:
        return ("done",
                f"Background work wrapped: {scope}. Handoff landed at "
                f"{handoff}. The manifest still says status "
                f"'{entry.get('status', '?')}'. Reach out to {OWNER_NAME} — "
                f"give the short version and ask what they want next.")

    open_for = entry.get("open-for-owner", "")
    if open_for.strip().lower() not in EMPTY_TOKENS:
        return ("needs-owner",
                f"Background work needs {OWNER_NAME}: {scope}. "
                f"open-for-owner: {open_for}. Flag it and ask how they want "
                f"to clear it.")

    session = entry.get("session", "").strip()
    if session and session.lower() not in EMPTY_TOKENS and not tmux_alive(session):
        return ("failed",
                f"Loud failure: the session '{session}' for {scope} is gone "
                f"and NO handoff landed at {handoff or '(no path set)'}. "
                f"Tell {OWNER_NAME} the session died without its handoff. You "
                f"may inspect the repo read-only (git diff / git status) and "
                f"report the real delta — offer to do that.")
    return None


# --- de-dupe state --------------------------------------------------------

def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.rename(STATE_PATH)


# --- outbound: notify + queue injection -----------------------------------

def fire_notify(trigger: str, scope: str, message: str) -> None:
    if NOTIFY_BIN is None:
        return  # no notify CLI configured — iMessage is the only surface
    if not NOTIFY_BIN.exists():
        log(f"notify bin missing at {NOTIFY_BIN} — skipped push")
        return
    titles = {"done": "Work done", "needs-owner": "Needs you",
              "failed": "Work FAILED"}
    prio = "max" if trigger == "failed" else "high"
    try:
        subprocess.run(
            [str(NOTIFY_BIN), NOTIFY_TOPIC,
             f"{titles.get(trigger, 'Lautrec')} — {scope}", message,
             "--priority", prio, "--tag", "lautrec"],
            capture_output=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log(f"notify failed: {e!r}")


def target_chat_guid(cfg: dict) -> str | None:
    """Where Lautrec reaches the owner. Prefer an explicit config override;
    else the most recently active thread, reading its real GUID from the
    '# Thread: <guid>' header (the slugified filename is lossy)."""
    override = cfg.get("watcher", {}).get("chat_guid", "").strip()
    if override:
        return override
    if not THREADS_DIR.exists():
        return None
    threads = [p for p in THREADS_DIR.glob("*.md") if p.is_file()]
    if not threads:
        return None
    newest = max(threads, key=lambda p: p.stat().st_mtime)
    first = newest.read_text().splitlines()[:1]
    if first:
        m = re.match(r"#\s*Thread:\s*(.+)$", first[0].strip())
        if m:
            return m.group(1).strip()
    return None


def inject_watcher_event(chat_guid: str, text: str) -> None:
    """Spool a watcher-labelled event for pa.py — same atomic pattern as
    webhook.py's spool(). kind='watcher' is what flips pa.py into
    proactive-outreach mode (see pa.speaker_for / invoke_claude)."""
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    ts_ms = int(time.time() * 1000)
    msg = {
        "chat_guid": chat_guid,
        "sender": "watcher",
        "text": text,
        "kind": "watcher",
        "msg_guid": f"watcher-{uuid.uuid4().hex}",
        "ts_ms": ts_ms,
    }
    name = f"{ts_ms}-{uuid.uuid4().hex[:8]}.json"
    tmp = QUEUE_DIR / f".tmp-{name}"
    tmp.write_text(json.dumps(msg))
    tmp.rename(QUEUE_DIR / name)


# --- tick -----------------------------------------------------------------

def tick() -> None:
    if not MANIFEST_PATH.exists():
        return
    entries = parse_active_entries(MANIFEST_PATH.read_text())
    if not entries:
        return  # dormancy is free — nothing running, nothing to do.

    cfg = CONFIG
    state = load_state()
    chat_guid = None
    changed = False

    for entry in entries:
        try:
            result = evaluate(entry)
        except Exception as e:  # one bad entry must not stop the sweep
            log(f"evaluate failed for {entry.get('id')!r}: {e!r}")
            continue
        if result is None:
            continue
        trigger, message = result
        key = f"{entry['id']}::{trigger}"
        if state.get(key):
            continue  # already pushed once — de-duped.

        scope = entry["id"]
        fire_notify(trigger, scope, message)
        if chat_guid is None:
            chat_guid = target_chat_guid(cfg)
        if chat_guid:
            inject_watcher_event(chat_guid, message)
            log(f"fired {trigger} for {scope!r} → notify + queue")
        else:
            log(f"fired {trigger} for {scope!r} → notify only "
                "(no target thread for an iMessage)")
        state[key] = int(time.time())
        changed = True

    if changed:
        save_state(state)


def main() -> None:
    try:
        tick()
    except Exception as e:
        log(f"tick ERROR {e!r}")
        sys.exit(1)


if __name__ == "__main__":
    main()
