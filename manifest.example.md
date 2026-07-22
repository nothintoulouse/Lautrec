# Work manifest (TEMPLATE)

Copy to `manifest.md` if you want to run `watcher.py`. It is the only
input the watcher has, and it is also its on/off switch: with no entries
under `## Active`, a tick exits in about a millisecond and does nothing.
`manifest.md` is gitignored — it describes your work, not the project's.

The parser is deliberately dumb (see `parse_active_entries` in
`watcher.py`): it splits on `##` headings, treats a heading that contains
` — ` as an *entry* and any other heading as a *section*, and reads
`- key: value` lines under each entry. Keys are lowercased. Continuation
lines are joined onto the previous key.

## Rules

- Only entries under `## Active` are evaluated. Move an entry to
  `## Recent` when you are done with it.
- Every entry heading needs the ` — ` separator, or it will be read as a
  section name and skipped.
- Each `(entry, trigger)` pair fires exactly once. The de-dupe ledger is
  `.watcher-state.json` (gitignored). Delete it to re-arm everything.

## Entry format

```
## <short name> — <one-line summary>
- session: <tmux session name, or leave blank>
- expected handoff: <path to the file the job writes when it finishes>
- open-for-owner: _none_
- status: <free text, echoed back to you>
```

Three triggers, checked in this order:

1. **done** — the `expected handoff` path now exists.
2. **needs-owner** — `open-for-owner` is set to anything other than an
   empty token (`_none_`, `none`, `-`, `n/a`, `tbd`, blank).
3. **failed** — a `session` is named, `tmux has-session` says it is gone,
   and no handoff landed. A missing `tmux` is treated as "can't tell",
   never as failure.

## Active

_Nothing running. Add entries here; remove them when the work lands._

## Recent

## Example (delete this section, it is under Recent so it never fires)

## build-indexer — rebuild the search index job
- session: indexer
- expected handoff: ~/work/handoffs/2026-01-01-indexer.md
- open-for-owner: _none_
- status: running
