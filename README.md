# Lautrec

> **PAUSED — indefinitely, as of 2026-09-14.** No transport is running.
> This project is not currently maintained; treat everything below as a
> description of a system that is not live.

A personal assistant you reach by texting it. An iMessage arrives, a
BlueBubbles webhook spools it to disk, a single-threaded worker runs one
`claude -p` invocation against the conversation transcript, and the reply
goes back out as a text.

Stdlib-only Python. No database, no container, no message broker, no
third-party packages. Every piece of durable state is a plain file you
can `cat`.

macOS and iMessage specific — see [Requirements](#requirements).

## How it works

```
iMessage ──▶ BlueBubbles server ──▶ webhook.py ──▶ queue/<ts>.json
                                                        │
                                          pa.py drains the queue
                                                        │
                          threads/<chat>.md  ◀── append user turn
                                                        │
                              claude -p  (cwd = workspace_root,
                              --append-system-prompt persona.md)
                                                        │
                          threads/<chat>.md  ◀── append reply
                                                        │
            BlueBubbles send API  ◀── reply ──▶ iMessage
```

Two long-lived processes, decoupled through the filesystem:

- **`webhook.py`** — a stdlib HTTP listener. Receives BlueBubbles
  `new-message` events, drops anything that isn't an inbound text from an
  allowed sender, and spools the rest to `queue/` as one JSON file each.
  It always answers 200, immediately, before doing any work — a webhook
  that 500s or hangs just makes the BlueBubbles server retry and pile up.
- **`pa.py`** — the worker. Polls `queue/`, maintains a per-conversation
  transcript in `threads/`, runs one `claude -p` per turn, sends the
  reply back through BlueBubbles.

Plus an optional third:

- **`watcher.py`** — a tick-and-exit launchd poller that lets the
  assistant reach out *first* when a long-running background job finishes
  or gets stuck. See [The watcher](#the-watcher-optional).

## Why it's built this way

**The transcript file is the memory.** Each `claude -p` run is stateless.
There is no session to resume, no conversation ID, no server-side thread.
The worker reads `threads/<chat>.md` off disk, appends the new user turn,
passes the whole thing as the prompt, and appends the reply. Restart the
worker mid-conversation and nothing is lost, because there was never
anything in memory to lose. Reasoning and tool calls inside a run are
ephemeral by construction — only the final reply is ever written down.

**One worker, single-threaded, always.** `pa.py` processes one chat at a
time and never runs two `claude -p` invocations concurrently. This is the
invariant the rest of the design leans on: no locking, no transaction
semantics on the transcript, no interleaved appends, no two agents racing
to write the same file. Concurrency would buy throughput that a single
human texting an assistant does not need, in exchange for every hard bug
in the system.

**The processes never talk to each other.** They share a directory. The
webhook writes files; the worker reads and deletes them. Either can be
restarted, killed, or run by hand in a terminal without the other
noticing. Queue writes are atomic — write to `.tmp-<name>`, then
`rename()` into place — so the worker can never observe a half-written
message.

**Transcripts are append-only.** Nothing edits a transcript mid-file.
`/clear` is a `rename()` into `threads/archive/`, not a delete — you can
always go read what the assistant was told. Compaction, when it happens,
is the model proposing a handoff summary in-band, not the code silently
truncating history.

**Everything is inspectable with `ls` and `cat`.** Debugging is reading
files. Backup is `cp -r`. The state model has no schema and no migration
path because there is nothing to migrate.

## Failure handling

Most of the interesting code is here, because most of what makes a
texting assistant feel broken is not the happy path.

**A content-policy refusal poisons the whole thread, so the thread gets
archived.** This is the sharp edge of the stateless design: the full
transcript is replayed on every turn, so if the API flags something in
it, *every future message in that conversation fails the same way* — the
thread is permanently bricked. When `pa.py` sees an `is_error` response
that reads as a usage-policy refusal, it archives the transcript, texts
back an honest explanation, and lets the next message start on a clean
thread.

**A follow-up that lands mid-invocation causes a re-invoke, not a stale
reply.** People text in fragments. If a second message arrives while a
run is in flight, shipping the first reply answers a question that is no
longer the one being asked. So after each invocation the worker re-drains
the queue: if anything new showed up, it discards the in-flight reply,
appends the follow-ups to the transcript, and runs again with the full
picture.

**Long runs report progress instead of going silent.** A real task can
legitimately take minutes, so the subprocess is polled rather than
blocked on. At two minutes the worker texts "Still on it — 2 min in," and
again every five minutes after. The wait is a UX problem, not a timeout
problem.

**The timeout is a hang backstop, not a task budget.** Because progress
pings cover the wait, `invocation_timeout_s` can stay generous (default
15 minutes) and mean only one thing: the process is stuck and should be
killed.

**An API error is parsed, not guessed at.** A usage-policy or API error
still comes back as valid JSON on stdout, sometimes alongside a non-zero
exit code. So stdout is parsed *first*, and the exit code is only
consulted when there is no parseable JSON to read.

**One bad turn never kills the worker.** The main loop wraps each chat in
a try/except: it logs the exception, texts back that something broke, and
keeps polling. The worker staying up is worth more than any single reply.

**The webhook drops before it spools.** Own-echo messages (which would
otherwise loop), attachment-only messages, non-`new-message` events, and
senders not in the owner allowlist are all discarded at the door — the
worker never sees them, and they never cost an invocation.

## Security

**The worker runs `claude -p` with `--dangerously-skip-permissions`.**
That flag disables Claude Code's permission prompts entirely. Anything
the agent decides to do — write a file, run a shell command, call a
network tool — happens without asking, because there is no interactive
terminal to ask in. This is not a harmless default; it is the load-
bearing tradeoff of the whole design. An agent that texts you back cannot
also stop and wait for you to approve a `Bash` call.

The practical consequence: **anything that can get a message into the
queue can drive an agent with your shell.** Treat the whole thing as
running with your user's privileges, because it does.

What actually limits the blast radius:

- **Sender allowlisting at the webhook.** `[owner].handles` in
  `config.toml` is checked before a message is ever spooled. A message
  from any other handle is logged and dropped; it never reaches the
  worker. If you leave `handles` empty, the webhook accepts *everyone*,
  and it warns about that at startup. Don't leave it empty.
- **`--setting-sources project,local`.** The run deliberately loads only
  project and local settings, never `~/.claude`. Your global Claude Code
  configuration — plugins, hooks, MCP servers, custom commands — is not
  inherited by the assistant. What it can reach is what this project's
  settings and the configured workspace give it, not everything your
  interactive Claude Code can reach. (OAuth/keychain authentication is
  unaffected.)
- **`workspace_root` scopes the working directory.** The agent runs with
  that as its cwd. It is not a sandbox — nothing stops an absolute path —
  but it determines what the agent naturally reaches for and what project
  settings apply.
- **Read-only-by-default posture, enforced in the persona.** The
  distinction between free reads and writes-need-an-explicit-yes lives in
  `persona.md` (see `persona.example.md`). That is a *prompt-level*
  control, not a technical one. It is a good behavioral default and it is
  not a security boundary. Do not rely on it to stop a determined or
  confused agent.
- **The webhook binds `127.0.0.1` by default.** Host-local only; the
  BlueBubbles server runs on the same Mac.

Secrets (BlueBubbles password, CalDAV/JMAP/IMAP credentials) all live in
`config.toml`, which is gitignored. `persona.md` and `manifest.md` are
gitignored too — they are personal, and the repo ships `*.example.*`
templates instead.

**If you are not comfortable with an iMessage being able to run
commands as you, do not run this.**

## Requirements

- **macOS.** The whole thing is Apple-platform specific: iMessage,
  launchd, and (for the optional tools) the Calendar and AddressBook
  databases and their Full Disk Access grants.
- **A [BlueBubbles](https://bluebubbles.app) server** running on that Mac
  and signed into iMessage. It is the only bridge to iMessage here.
- **Python 3.12+** — `tomllib` is stdlib as of 3.11, and 3.12 is what
  this is developed against. No third-party packages for the core.
- **The `claude` CLI** on `PATH`, authenticated.

## Setup

1. **Configure.**

   ```sh
   cp config.example.toml config.toml
   cp persona.example.md persona.md
   ```

   Fill in `[bluebubbles].server_url` / `.password`, `[owner].name` and
   `[owner].handles` (your iMessage phone number and/or Apple ID email),
   and `[paths].workspace_root`. Then rewrite `persona.md` in your own
   words — it is the system prompt, and it is where the assistant's
   boundaries actually live.

2. **Test it end-to-end before pointing BlueBubbles at it.** Run the two
   processes by hand and fake an inbound message:

   ```sh
   python3 webhook.py          # terminal 1
   python3 pa.py               # terminal 2

   curl -X POST http://127.0.0.1:8788 \
     -H 'Content-Type: application/json' \
     -d '{"type":"new-message","data":{
            "text":"what does my week look like?",
            "isFromMe":false,
            "handle":{"address":"+15551234567"},
            "chats":[{"guid":"iMessage;-;+15551234567"}],
            "dateCreated":1747000000000}}'
   ```

   Use one of your real `[owner].handles` values for `address` or the
   webhook will (correctly) drop it. Watch `logs/` and `threads/`: the
   message should queue, invoke `claude -p`, append a reply to the
   transcript, and — with a working BlueBubbles password — send.

3. **Supervise with launchd.**

   ```sh
   ./launchd/install.sh                  # webhook + worker
   ./launchd/install.sh --with-watcher   # also the optional watcher
   ```

   The script substitutes the `{{PYTHON}}` / `{{PROJECT_ROOT}}` /
   `{{HOME}}` placeholders in the plist templates, writes them to
   `~/Library/LaunchAgents`, and bootstraps them.

4. **Point BlueBubbles at it.** Set the webhook URL in the BlueBubbles
   server UI to `http://127.0.0.1:8788`, then text yourself.

## The watcher (optional)

The message loop is purely reactive: nothing happens until you text. The
watcher is the counterweight — a `StartInterval=60` launchd job that
ticks once and exits, so the assistant can start a conversation when
something changes.

It reads `manifest.md` (see `manifest.example.md` for the format), a
plain markdown file listing background jobs, and fires on three triggers:
a job's expected handoff file appeared, a job flagged something as
needing you, or a job's `tmux` session died without leaving a handoff.

Two things worth stealing from it:

- **Dormancy is free.** Zero entries under `## Active` means the tick
  exits in about a millisecond. There is no heartbeat, no state machine,
  no loading and unloading of agents — the manifest *is* the on/off
  switch.
- **A watcher event is not a user turn.** It goes into the same queue,
  but tagged `kind="watcher"`, and the worker labels it in the transcript
  as `Watcher` rather than as the owner. That flips the prompt footer:
  instead of "reply to the most recent message," the model is told it
  *noticed* something and is reaching out unprompted. Without that, the
  assistant answers a message you never sent, and it reads as broken.

De-duplication is a gitignored `.watcher-state.json`: each
`(entry, trigger)` pair fires exactly once, not every sixty seconds.

## Skills

`skills/*.md` are per-capability instruction files — when to invoke, the
exact command lines, error handling, and where the authorization boundary
sits. `persona.md` points the assistant at them.

The ones here are **sanitized examples of the pattern**, not a supported
integration suite: `calendar`, `contacts`, `mail`, `maps`, `reminders`,
`weather`. Read them as templates. Note that `skills/reminders.md` wraps
an external CLI that is *not* shipped in this repo, and
`skills/weather.md` is deliberately unimplemented — it is the example of
a skill that knows it isn't provisioned and says so instead of guessing.

## Tools

`tools/` holds standalone macOS bridges the skills call. They are
independent of the message loop and usable on their own.

- **`calctl`** — Apple Calendar. Reads `Calendar.sqlitedb` directly and
  expands recurrence itself (frequency/interval/specifier, minus
  `ExceptionDate` cancellations, with detached overrides substituted).
  Writes go over CalDAV. `calctl.swift` is an EventKit implementation
  kept as a validation oracle, and `compare.py` diffs the two.
- **`contactctl`** — Apple Contacts name → handle. Reads the AddressBook
  `.abcddb` files directly, unions all account sources, merges
  cross-source duplicates, normalizes phones to E.164.
- **`mailctl`** — Fastmail over JMAP (read + send) and Gmail over IMAP
  (read-only), kept as deliberately separate accounts.

**Why they read databases directly instead of using the frameworks:**
EventKit and the Contacts framework are TCC-prompt-gated. Under
`claude -p` running from launchd there is no interactive session to show
a prompt in, so they hang or fail rather than asking. Reading the backing
SQLite files works, and Full Disk Access is granted per responsible
binary — which is why `calctl` and `contactctl` ship as compiled
single-file binaries (`build.sh`) with their own code identity, so the
FDA grant lands on one narrow tool instead of on a shared Python
interpreter. Rebuilding changes the binary's cdhash and invalidates the
grant, so it has to be re-granted.

`caldav_probe.py` is the throwaway harness that proved headless CalDAV
writes work before that logic moved into `calctl`. It is the only file
here with third-party dependencies (`caldav`, `icalendar`), installed
into a temporary venv.

## Layout

```
webhook.py            BlueBubbles webhook listener
pa.py                 worker — transcript, claude -p, reply
watcher.py            optional proactive-outreach poller
persona.example.md    template for persona.md (the system prompt)
manifest.example.md   template for manifest.md (watcher input)
config.example.toml   template for config.toml (secrets + tuning)
skills/               per-capability instruction files
tools/                standalone macOS bridges (calendar, contacts, mail)
launchd/              plist templates + install.sh
threads/              per-conversation transcripts     (gitignored, runtime)
queue/                transient inbound message spool  (gitignored, runtime)
logs/                 plain-text daily logs            (gitignored, runtime)
```

## Status

Honest accounting.

**Verified working**, in daily use on one Mac:

- The full loop: inbound iMessage → queue → transcript → `claude -p` →
  reply, under launchd.
- Sender allowlisting, own-echo suppression, `/clear` archiving.
- Progress pings on long runs; the re-invoke path when a follow-up lands
  mid-invocation.
- Policy-refusal detection and automatic thread archiving.
- `calctl` reads (validated 0-diff against the EventKit oracle over a
  30-day window) and CalDAV writes; `contactctl` lookups; `mailctl`
  Fastmail read/send and Gmail IMAP reads — all under launchd.
- The watcher's tick, trigger evaluation, de-dupe, and queue injection.

**Untested or unverified:**

- Anything on a second machine. It has run on exactly one Mac, one
  macOS version, one BlueBubbles install.
- Group chats. The code keys off `chat_guid` and would technically
  function, but the persona, the allowlist model, and the reply behavior
  all assume a one-to-one thread.
- Attachments. Non-text messages are dropped at the webhook.
- `send_method = "private-api"`. Only `apple-script` has been exercised.
- Long-horizon transcript growth. `context_budget_tokens` triggers a
  nudge to summarize and archive, but the behavior at a genuinely
  exhausted context window has not been characterized.
- Concurrent chats under real load. The single-worker invariant makes it
  correct, not fast; queueing behavior with several active threads is
  unmeasured.
- `caldav_probe.py` against Google CalDAV (iCloud only).

## Authorship

I defined the architecture, the constraints (stdlib only, no database,
single worker, files as state), and the acceptance criteria, and I
verified the behavior on real hardware — including the failure modes
described above, most of which were found by using it rather than by
designing for them. Coding agents assisted with the implementation. The
design decisions, the tradeoffs, and the debugging are mine; a meaningful
share of the lines are not hand-typed.

## License

MIT — see [LICENSE](LICENSE).
