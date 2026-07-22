#!/usr/bin/env python3
"""pa.py — Lautrec's worker.

Drains queue/ (filled by webhook.py), keeps a per-conversation transcript
in threads/, and runs one stateless `claude -p` invocation per turn.
Replies go back out through the BlueBubbles send API.

One worker, single-threaded — never two claude invocations at once.
Each invocation is stateless: the transcript file IS the memory; thinking
and tool calls inside the run are ephemeral and never persisted. Stdlib
only, state is plain files. See AGENTS.md.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.toml"
PERSONA_PATH = ROOT / "persona.md"
QUEUE_DIR = ROOT / "queue"
THREADS_DIR = ROOT / "threads"
ARCHIVE_DIR = THREADS_DIR / "archive"
LOG_DIR = ROOT / "logs"
CLAUDE_BIN = os.environ.get("LAUTREC_CLAUDE_BIN", "claude")


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(
            f"pa: no config at {CONFIG_PATH}. "
            "Copy config.example.toml to config.toml and fill it in."
        )
    with CONFIG_PATH.open("rb") as fh:
        return tomllib.load(fh)


def log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} pa: {msg}"
    print(line, flush=True)
    with (LOG_DIR / f"{time.strftime('%Y-%m-%d')}.log").open("a") as fh:
        fh.write(line + "\n")


CONFIG = load_config()
BB = CONFIG.get("bluebubbles", {})
WORKER = CONFIG.get("worker", {})
OWNER_NAME = CONFIG.get("owner", {}).get("name", "the owner")
# A watcher-originated queue item carries kind="watcher" and is labelled in
# the transcript as this speaker — NOT the owner. watcher.py spools these;
# the worker just has to read them differently. See append_turn / the footer.
WATCHER_SPEAKER = "Watcher"
WORKSPACE_ROOT = CONFIG.get("paths", {}).get("workspace_root", str(ROOT))
POLL_INTERVAL = float(WORKER.get("poll_interval_s", 1.0))
TOKEN_BUDGET = int(WORKER.get("context_budget_tokens", 100_000))
INVOKE_TIMEOUT = int(WORKER.get("invocation_timeout_s", 900))
PING_FIRST = int(WORKER.get("progress_first_ping_s", 120))
PING_EVERY = int(WORKER.get("progress_ping_interval_s", 300))
MODEL = WORKER.get("model", "").strip()


# --- threads --------------------------------------------------------------

def thread_path(chat_guid: str) -> Path:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", chat_guid).strip("_") or "chat"
    return THREADS_DIR / f"{slug}.md"


def read_thread(chat_guid: str) -> str:
    path = thread_path(chat_guid)
    return path.read_text() if path.exists() else ""


def append_turn(chat_guid: str, speaker: str, text: str) -> None:
    """Append one turn to the transcript. Files are append-only — /clear
    archives, it never edits mid-file."""
    path = thread_path(chat_guid)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(f"# Thread: {chat_guid}\n")
    with path.open("a") as fh:
        fh.write(f"\n## {time.strftime('%Y-%m-%d %H:%M')} — {speaker}\n"
                 f"{text.strip()}\n")


def archive_thread(chat_guid: str) -> bool:
    """Roll the active transcript into threads/archive/. This is what
    /clear does — a filesystem move, never a delete."""
    path = thread_path(chat_guid)
    if not path.exists():
        return False
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    dst = ARCHIVE_DIR / f"{path.stem}-{time.strftime('%Y%m%d-%H%M%S')}.md"
    path.rename(dst)
    return True


def estimate_tokens(text: str) -> int:
    """Rough — about 4 chars per token. Only used to decide when to nudge
    Lautrec toward proposing a handoff; nothing depends on it being exact."""
    return len(text) // 4


def speaker_for(msg: dict) -> str:
    """Transcript label for a queued message. Watcher events are NOT the
    owner — they must read as 'you noticed this', never as a turn the owner
    typed. webhook.py never sets kind, so inbound texts default to owner."""
    return WATCHER_SPEAKER if msg.get("kind") == "watcher" else OWNER_NAME


# --- queue ----------------------------------------------------------------

def _load_queue() -> list[tuple[Path, dict]]:
    out: list[tuple[Path, dict]] = []
    for path in QUEUE_DIR.glob("*.json"):
        try:
            out.append((path, json.loads(path.read_text())))
        except (json.JSONDecodeError, OSError):
            continue
    return out


def pending_chats() -> list[str]:
    """Chat GUIDs that have queued messages, oldest-waiting chat first."""
    first_seen: dict[str, int] = {}
    for _, msg in _load_queue():
        guid, ts = msg.get("chat_guid"), msg.get("ts_ms", 0)
        if guid and (guid not in first_seen or ts < first_seen[guid]):
            first_seen[guid] = ts
    return sorted(first_seen, key=lambda g: first_seen[g])


def drain_queue(chat_guid: str) -> list[dict]:
    """Return and delete every queued message for one chat, oldest first.
    Once a message is in the transcript the queue file has done its job."""
    items = [(p, m) for p, m in _load_queue()
             if m.get("chat_guid") == chat_guid]
    items.sort(key=lambda pm: pm[1].get("ts_ms", 0))
    for path, _ in items:
        path.unlink(missing_ok=True)
    return [msg for _, msg in items]


# --- claude + bluebubbles -------------------------------------------------

def invoke_claude(
    transcript: str, near_limit: bool, on_progress=None, watcher: bool = False
) -> tuple[str, str]:
    """One stateless `claude -p` run. persona.md is read fresh each time,
    so editing it takes effect on the next message with no restart.

    A real task can legitimately take minutes, so the run is polled, not
    blocked on: while it is still going, on_progress(elapsed_seconds) fires
    at PING_FIRST then every PING_EVERY seconds so the worker can text a
    "still working" note. INVOKE_TIMEOUT is only a true-hang backstop.

    `watcher=True` means the most recent turn came from the watcher, not
    the owner — the footer tells Lautrec to reach out proactively instead
    of replying as if answering a message."""
    persona = PERSONA_PATH.read_text() if PERSONA_PATH.exists() else ""
    if watcher:
        footer = (
            "\n\n---\n"
            "Above is the conversation so far. The most recent turn is "
            f"labelled from your {WATCHER_SPEAKER} — a background process that "
            "monitors background work and flags state changes. It is NOT a "
            f"message from {OWNER_NAME}; they did not text you. You just "
            "NOTICED this and are reaching out about it. Open the conversation "
            "naturally, as a message you are initiating — do not reply as if "
            "answering something they said. Sent as a plain-text iMessage — no "
            "markdown. Keep it short."
        )
    else:
        footer = (
            "\n\n---\n"
            "Above is the conversation so far. Reply to the most recent "
            f"message from {OWNER_NAME}. Your reply is sent as a plain-text "
            "iMessage — no markdown."
        )
    if near_limit:
        footer += (
            "\n\nNOTE: this thread is approaching its context limit. If "
            "the conversation is at a natural pause, offer in your reply "
            "to summarize and archive it (a handoff)."
        )
    # --setting-sources project,local loads only project/local settings,
    # never ~/.claude (user) — keeping the run clear of the operator's
    # global plugins and their hooks. OAuth/keychain auth is unaffected.
    cmd = [
        CLAUDE_BIN, "-p", transcript + footer,
        "--output-format", "json",
        "--dangerously-skip-permissions",
        "--setting-sources", "project,local",
    ]
    if persona:
        cmd += ["--append-system-prompt", persona]
    if MODEL:
        cmd += ["--model", MODEL]

    try:
        proc = subprocess.Popen(
            cmd, cwd=WORKSPACE_ROOT, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
    except OSError as e:
        log(f"claude -p failed to start: {e!r}")
        return ("error", "(Lautrec couldn't start that one — check the logs.)")

    # Poll the run: emit a progress ping at PING_FIRST, then every
    # PING_EVERY; kill it only if it crosses the INVOKE_TIMEOUT backstop.
    start = time.monotonic()
    next_ping = PING_FIRST
    while True:
        elapsed = time.monotonic() - start
        if elapsed >= INVOKE_TIMEOUT:
            proc.kill()
            try:
                proc.communicate(timeout=10)
            except Exception:
                pass
            log(f"claude -p hit the {INVOKE_TIMEOUT}s backstop — killed")
            return ("error", "(That one ran past Lautrec's time limit — "
                              "try a smaller ask, or check the logs.)")
        wait = max(min(next_ping, INVOKE_TIMEOUT) - elapsed, 1.0)
        try:
            stdout, stderr = proc.communicate(timeout=wait)
            break  # run finished
        except subprocess.TimeoutExpired:
            if time.monotonic() - start >= next_ping:
                if on_progress:
                    try:
                        on_progress(int(time.monotonic() - start))
                    except Exception as e:
                        log(f"progress ping failed: {e!r}")
                next_ping += PING_EVERY

    # An API or Usage-Policy error still comes back as valid JSON with
    # is_error set — sometimes alongside a non-zero exit — so parse first.
    data = None
    if stdout and stdout.strip():
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            pass
    if data is None:
        log(f"claude -p exited {proc.returncode}, no JSON: "
            f"{((stderr or stdout) or '').strip()[:300]}")
        return ("error", "(Lautrec hit an error processing that — check the logs.)")

    reply = (data.get("result") or "").strip()
    if data.get("is_error"):
        log(f"claude -p returned is_error: {reply[:200]}")
        if "usage policy" in reply.lower() or "unable to respond" in reply.lower():
            # The flagged content is in the transcript itself — process_chat
            # archives the thread, since replaying it would fail every turn.
            return ("policy", reply)
        return ("error", "(Lautrec hit an API error on that one — check the logs.)")
    return ("ok", reply or "(Lautrec returned an empty reply.)")


def send_imessage(chat_guid: str, text: str) -> bool:
    base = BB["server_url"].rstrip("/")
    url = (f"{base}/api/v1/message/text"
           f"?password={urllib.parse.quote(BB['password'])}")
    body = json.dumps({
        "chatGuid": chat_guid,
        "tempGuid": str(uuid.uuid4()),
        "message": text,
        "method": BB.get("send_method", "apple-script"),
    }).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status >= 300:
                log(f"send failed: BlueBubbles returned {resp.status}")
                return False
    except urllib.error.URLError as e:
        log(f"send failed: {e}")
        return False
    return True


# --- main turn loop -------------------------------------------------------

def process_chat(chat_guid: str) -> None:
    pending = drain_queue(chat_guid)
    if not pending:
        return

    # /clear is a filesystem op, never a claude invocation — archive the
    # transcript and stop. It's a hard reset; anything batched with it is
    # dropped intentionally.
    if any(m["text"].strip() == "/clear" for m in pending):
        archived = archive_thread(chat_guid)
        send_imessage(chat_guid,
                      "Cleared — fresh thread." if archived
                      else "Nothing to clear.")
        log(f"{chat_guid}: /clear ({'archived' if archived else 'no-op'})")
        return

    for m in pending:
        append_turn(chat_guid, speaker_for(m), m["text"])
    # Watcher-originated batches flip the worker into proactive-outreach
    # mode for this invocation (see invoke_claude's footer).
    watcher_turn = any(m.get("kind") == "watcher" for m in pending)

    # Progress pings: a long run is otherwise silent — say it is still
    # going at PING_FIRST, then every PING_EVERY.
    def ping(elapsed_s: int) -> None:
        mins = max(1, round(elapsed_s / 60))
        send_imessage(chat_guid, f"Still on it — {mins} min in.")
        log(f"{chat_guid}: progress ping ({mins} min)")

    # Re-invoke loop: if a follow-up lands mid-invocation, fold it into the
    # transcript and run again rather than shipping a reply that ignored it.
    while True:
        transcript = read_thread(chat_guid)
        tokens = estimate_tokens(transcript)
        near = tokens > TOKEN_BUDGET * 0.9
        log(f"{chat_guid}: invoking claude -p (~{tokens} tok"
            f"{' · near limit' if near else ''}"
            f"{' · watcher' if watcher_turn else ''})")
        kind, reply = invoke_claude(transcript, near, ping, watcher=watcher_turn)

        # A policy flag poisons the whole thread: the stateless design
        # replays the full transcript every turn, so every future message
        # would fail too. Archive it so the next message starts clean.
        if kind == "policy":
            archive_thread(chat_guid)
            send_imessage(chat_guid,
                          "That thread tripped a content filter, so I "
                          "archived it. Send that again and we'll pick up "
                          "on a clean thread.")
            log(f"{chat_guid}: policy-flagged — thread archived")
            return

        followups = drain_queue(chat_guid)
        if followups:
            log(f"{chat_guid}: {len(followups)} follow-up(s) arrived — "
                "discarding in-flight reply, re-invoking with both")
            for m in followups:
                append_turn(chat_guid, speaker_for(m), m["text"])
            watcher_turn = any(m.get("kind") == "watcher" for m in followups)
            continue

        append_turn(chat_guid, "Lautrec", reply)
        if send_imessage(chat_guid, reply):
            log(f"{chat_guid}: replied ({len(reply)} chars)")
        return


def main() -> None:
    if not BB.get("server_url") or not BB.get("password") \
            or BB.get("password") == "REPLACE-ME":
        sys.exit("pa: set bluebubbles.server_url and .password in config.toml")
    for d in (QUEUE_DIR, THREADS_DIR, ARCHIVE_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
    log(f"worker up — workspace={WORKSPACE_ROOT}, model={MODEL or 'default'}, "
        f"budget={TOKEN_BUDGET} tok, poll={POLL_INTERVAL}s")
    while True:
        chats = pending_chats()
        if not chats:
            time.sleep(POLL_INTERVAL)
            continue
        for chat_guid in chats:
            try:
                process_chat(chat_guid)
            except Exception as e:  # one bad turn must not kill the worker
                log(f"{chat_guid}: ERROR {e!r}")
                try:
                    send_imessage(
                        chat_guid,
                        "(Lautrec hit an unexpected error — check the logs.)")
                except Exception:
                    pass


if __name__ == "__main__":
    main()
