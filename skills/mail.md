---
name: mail
description: Read, triage, and (personal Fastmail only) send the owner's email. Two accounts: personal (Fastmail via JMAP, read+send) and work (Gmail via IMAP, read-only). Invoke for "any new email", "what's in my inbox", "did <person> email me", "search my mail", "reply to X", "email <person> …".
when-to-invoke: |
  - "any new / unread email", "what's in my inbox", "check my mail".
  - "did <person/company> email me", "search my email for X".
  - Triage / summarize an email thread.
  - "reply to <that email> …", "email <person> …" (personal Fastmail — a WRITE).
determinism-level: medium
tools-used: mailctl CLI (~/.local/bin/mailctl) — personal Fastmail JMAP (read+SEND); work Gmail IMAP (--account work, read-only).
---

# mail — Fastmail (JMAP) via mailctl + work Gmail (later)

Two surfaces, deliberately separate (the right work-data posture). **Not
Apple Mail** — skip the AppleScript Mail.app bridge. Mail is an HTTPS API, so
no Full-Disk-Access/binary-compile needed (unlike calctl/contactctl).

- **Personal: Fastmail via JMAP** through `mailctl` (default account).
- **Work: Gmail via IMAP** through `mailctl … --account work` — a SEPARATE
  source (App Password, not forwarded into Fastmail). Kept distinct on purpose.

## mailctl (read-only)

```
mailctl unread [N] --format json      # unread in Inbox (default 10)
mailctl recent [N] --format json      # most recent N in Inbox
mailctl search "<query>" [N]          # full-text search
mailctl thread <email-id>             # one thread (Fastmail only)
mailctl <cmd> --account work          # same commands against work Gmail
```
Each email: `from`, `fromEmail`, `subject`, `received` (local), `unread`,
`preview` (Fastmail; empty for Gmail), `id`. Summarize into a short iMessage —
never paste raw JSON. **Label the source when it matters** — "2 work, 1
personal unread." Default (no flag) is personal Fastmail.

## Sending — personal Fastmail only, and a WRITE (needs a yes)

```
mailctl send --to "a@b.com,c@d.com" --subject "…" --body "…" [--cc "…"]
mailctl reply <email-id> --body "…"      # replies in-thread to that message
```
Sends from the owner's Fastmail identity, files to Sent. **Work Gmail is
read-only — there is no send for `--account work`.**

**Authorization (non-negotiable):** sending is a write. **Confirm the exact
recipient, subject, and body with the owner and get an explicit yes BEFORE
calling `send`/`reply`.** Never auto-send; never infer a recipient you didn't
resolve (use the `contacts` skill to turn a name into an address). If their
message already carries the go-ahead ("reply to Dana, yes — tell her I'll be
there"), that's your yes. Show the draft first when there's any doubt.

## Provisioning

- **Fastmail — LIVE.** JMAP token set in `config.toml [fastmail].api_token`
  (gitignored). If `mailctl` ever errors that it's unprovisioned, the token was
  lost/rotated — tell the owner to regenerate it in Fastmail settings.
- **Work Gmail — LIVE.** IMAP + App Password in `config.toml [gmail]`
  (gitignored). If `mailctl --account work` errors on login, the App Password
  was revoked/rotated or IMAP got disabled — tell the owner to regenerate it.

Never invent a token or scrape a logged-in session.

## Posture

- **Read/triage is the common case.** Summaries, unread counts, "X emailed
  about Y." Keep work and personal clearly labelled when you summarize across both.
- **Sending (personal Fastmail) is live but gated** — a write, explicit-yes
  only, per the Sending section above. Never automatic.
- **Work Gmail is read-only.** No send path there.
