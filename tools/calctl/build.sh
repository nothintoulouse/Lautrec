#!/bin/bash
# Build calctl into a standalone native binary and install it to ~/.local/bin.
#
# Why compiled: reading the protected Calendar.sqlitedb needs Full Disk Access,
# which macOS grants per responsible-binary. A plain `.py` runs via a shared
# interpreter (system/homebrew python) that we'd have to grant FDA to broadly.
# A PyInstaller onefile gives calctl its OWN code identity, so FDA is granted to
# exactly one calendar-only binary — surgical, and independent of `claude`'s
# grant / child-inheritance.
#
# ⚠ After (re)building, the adhoc code identity (cdhash) changes, so the macOS
# Full Disk Access grant must be RE-DONE. Grant it to: ~/.local/bin/calctl
# (System Settings → Privacy & Security → Full Disk Access → +, ⌘⇧G to paste
# the path). Rebuilds should be rare — the reader is validated and stable.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PYBIN="${CALCTL_PYTHON:-/opt/homebrew/opt/python@3.14/bin/python3.14}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "building calctl with $PYBIN ($("$PYBIN" --version))"
"$PYBIN" -m venv "$WORK/venv"
# caldav + icalendar power the write path (calctl add) over CalDAV; pyinstaller
# freezes them into the binary alongside the read path.
"$WORK/venv/bin/pip" install --quiet --disable-pip-version-check \
  pyinstaller caldav icalendar
"$WORK/venv/bin/pyinstaller" --onefile --name calctl \
  --hidden-import caldav --hidden-import icalendar \
  --collect-submodules caldav --collect-submodules icalendar \
  --distpath "$WORK/dist" --workpath "$WORK/build" --specpath "$WORK/spec" \
  "$HERE/calctl.py"

install -m 0755 "$WORK/dist/calctl" "$HOME/.local/bin/calctl"
echo "installed → $HOME/.local/bin/calctl"
echo "REMEMBER: re-grant Full Disk Access to ~/.local/bin/calctl (cdhash changed)."
