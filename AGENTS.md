# AGENTS.md

Notes for a coding agent (or a human) working on this repo. Not the
runtime persona — that is `persona.md`, and it is a different thing
entirely.

## Invariants — do not break these

1. **Stdlib only** in `webhook.py`, `pa.py`, `watcher.py`. No
   dependencies, no `requirements.txt`, no venv needed to run the loop.
   (`tools/calctl` may lazily import `caldav`/`icalendar` on its write
   path only; `tools/calctl/caldav_probe.py` is a standalone harness.)
2. **One worker, single-threaded.** Never two `claude -p` invocations at
   once. Do not add threading, multiprocessing, or async to `pa.py`. A
   large amount of the correctness here is downstream of this.
3. **State is plain files.** No database, no cache, no in-memory session
   that has to survive a restart. If something must persist, it is a file
   under `queue/`, `threads/`, or `logs/`.
4. **Queue writes are atomic.** Write `.tmp-<name>`, then `rename()`.
   A reader must never see a partial file.
5. **Transcripts are append-only.** `/clear` is a `rename()` into
   `threads/archive/`. Nothing edits or truncates a transcript in place.
6. **The webhook always answers 200, first.** Respond before parsing.
   Never let BlueBubbles wait on work or retry into a pile-up.
7. **One bad turn must not kill the worker.** Every per-chat failure is
   caught, logged, and reported to the user; the loop keeps polling.

## Where things belong

- **Behavior, tone, boundaries** → `persona.md` (read fresh every turn,
  no restart needed). Never hardcode personality into the `.py` files.
- **Per-capability instructions** → `skills/<name>.md`.
- **Secrets and tuning** → `config.toml` (gitignored;
  `config.example.toml` is the template).
- **Anything owner-specific** → config, not source. The owner's name
  comes from `[owner].name` and is substituted into the prompt footers;
  do not reintroduce a literal name anywhere in the code.

## Conventions

- Comments explain *why*, especially where the code looks odd — the
  parse-stdout-before-checking-exit-code path, the re-invoke loop, the
  policy-refusal archive. Those comments are load-bearing; keep them
  current rather than deleting them.
- Log lines are `<time> <component>: <message>`, written to stdout and to
  `logs/YYYY-MM-DD.log`.
- Before committing: `python3 -m py_compile` on every `.py` file, and
  `bash -n` on the shell scripts.

## Security

The worker invokes `claude -p` with `--dangerously-skip-permissions`.
Read the Security section of the README before changing anything about
how the subprocess is constructed — in particular, do not remove
`--setting-sources project,local`, and do not weaken the owner-handle
allowlist in `webhook.py`.
