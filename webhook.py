#!/usr/bin/env python3
"""webhook.py — Lautrec's BlueBubbles webhook listener.

The BlueBubbles server POSTs message events here. This process does as
little as possible: validate the event, drop anything that isn't an
inbound text from an allowed sender, and spool the message to queue/ as
one JSON file. pa.py drains the queue and does the real work.

It always answers 200 — a webhook that 500s or hangs just makes the
BlueBubbles server retry and pile up. Stdlib only; state is plain files.
See AGENTS.md.
"""

from __future__ import annotations

import json
import sys
import time
import tomllib
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.toml"
QUEUE_DIR = ROOT / "queue"
LOG_DIR = ROOT / "logs"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(
            f"webhook: no config at {CONFIG_PATH}. "
            "Copy config.example.toml to config.toml and fill it in."
        )
    with CONFIG_PATH.open("rb") as fh:
        return tomllib.load(fh)


def log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} webhook: {msg}"
    print(line, flush=True)
    with (LOG_DIR / f"{time.strftime('%Y-%m-%d')}.log").open("a") as fh:
        fh.write(line + "\n")


CONFIG = load_config()
BB = CONFIG.get("bluebubbles", {})
OWNER = CONFIG.get("owner", {})
OWNER_NAME = OWNER.get("name", "the owner")
OWNER_HANDLES = {h.strip() for h in OWNER.get("handles", []) if h.strip()}


def extract_message(data: dict) -> dict | None:
    """Pull the fields we need out of a BlueBubbles message object.
    Returns None if this isn't an inbound text Lautrec should act on."""
    if data.get("isFromMe") or data.get("fromMe"):
        return None  # our own reply echoing back — ignore, don't loop
    text = (data.get("text") or data.get("message") or "").strip()
    if not text:
        return None  # v1 is text-only; skip attachment-only messages
    chats = data.get("chats") or []
    chat = data.get("chat") or {}
    chat_guid = (
        data.get("chatGuid")
        or data.get("chat_identifier")
        or (chats[0].get("guid") if chats else None)
        or chat.get("guid")
    )
    if not chat_guid:
        return None
    handle = data.get("handle") or {}
    sender = handle.get("address") or data.get("sender") or "unknown"
    ts = data.get("dateCreated") or data.get("date") or 0
    if ts and ts < 1e12:  # BlueBubbles sometimes sends seconds, not ms
        ts *= 1000
    return {
        "chat_guid": chat_guid,
        "sender": sender,
        "text": text,
        "msg_guid": data.get("guid", ""),
        "ts_ms": int(ts) or int(time.time() * 1000),
    }


def spool(msg: dict) -> None:
    """Write the message to queue/ atomically — pa.py must never read a
    half-written file, so write to a temp name and rename into place."""
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{msg['ts_ms']}-{uuid.uuid4().hex[:8]}.json"
    tmp = QUEUE_DIR / f".tmp-{name}"
    tmp.write_text(json.dumps(msg))
    tmp.rename(QUEUE_DIR / name)


class Handler(BaseHTTPRequestHandler):
    def _ok(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args) -> None:
        pass  # silence the default access log; log() handles our logging

    def do_GET(self) -> None:
        self._ok()  # liveness probe

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        self._ok()  # answer first — never make BlueBubbles wait or retry
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            log("dropped: request body was not JSON")
            return
        if event.get("type") != "new-message":
            return  # typing indicators, read receipts, server pings, etc.
        msg = extract_message(event.get("data") or {})
        if msg is None:
            return
        if OWNER_HANDLES and msg["sender"] not in OWNER_HANDLES:
            log(f"dropped: {msg['sender']} is not {OWNER_NAME} "
                "(not in owner.handles)")
            return
        spool(msg)
        log(f"queued from {msg['sender']} in {msg['chat_guid']}: "
            f"{msg['text'][:60]!r}")


def main() -> None:
    host = CONFIG.get("webhook", {}).get("host", "127.0.0.1")
    port = int(CONFIG.get("webhook", {}).get("port", 8788))
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    if not OWNER_HANDLES:
        log("WARNING: owner.handles is empty — accepting messages from "
            "any sender. Set it in config.toml unless the iMessage "
            "number is private.")
    else:
        log(f"owner: {OWNER_NAME} ({len(OWNER_HANDLES)} handle(s))")
    server = ThreadingHTTPServer((host, port), Handler)
    log(f"listening on http://{host}:{port} — POST new-message events here")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")


if __name__ == "__main__":
    main()
