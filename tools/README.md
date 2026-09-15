# tools/

`calctl`, `contactctl`, and `mailctl` — the external binaries Lautrec's
skills (`skills/calendar.md`, `skills/contacts.md`, `skills/mail.md`) shell
out to — used to be vendored here. They have been removed from this repo.

These utilities live in separate, private repositories and are not
published here. In particular, the copy of `calctl` that was previously
vendored in this directory shipped a build script that ad-hoc signs the
binary, which changes its code identity (`cdhash`) on every rebuild and
silently revokes the macOS Full Disk Access grant it depends on — a
known, already-fixed problem in the canonical tool. Publishing that build
script here was a real footgun for anyone who cloned this repo and
followed its own instructions.

If you need these tools, they are not distributed from this repository.
