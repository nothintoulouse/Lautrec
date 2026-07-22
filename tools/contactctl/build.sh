#!/bin/bash
# Build contactctl into a standalone native binary and install it to
# ~/.local/bin. Same rationale as calctl (tools/calctl/build.sh): reading the
# AddressBook abcddb needs Full Disk Access, granted per responsible-binary, so
# a self-contained binary with its OWN code identity lets FDA be scoped to one
# contacts-only tool. Framework-linked contacts CLIs can't be used
# headless — AddressBook.framework is TCC-Contacts-prompt-gated (dead under
# claude -p, the same class as EventKit).
#
# ⚠ After (re)building, the adhoc cdhash changes → RE-GRANT Full Disk Access to
# ~/.local/bin/contactctl (System Settings › Privacy & Security › Full Disk
# Access › +, ⌘⇧G to paste the path). Rebuilds should be rare.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PYBIN="${CONTACTCTL_PYTHON:-/opt/homebrew/opt/python@3.14/bin/python3.14}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "building contactctl with $PYBIN ($("$PYBIN" --version))"
"$PYBIN" -m venv "$WORK/venv"
"$WORK/venv/bin/pip" install --quiet --disable-pip-version-check pyinstaller
"$WORK/venv/bin/pyinstaller" --onefile --name contactctl \
  --distpath "$WORK/dist" --workpath "$WORK/build" --specpath "$WORK/spec" \
  "$HERE/contactctl.py"

install -m 0755 "$WORK/dist/contactctl" "$HOME/.local/bin/contactctl"
echo "installed → $HOME/.local/bin/contactctl"
echo "REMEMBER: re-grant Full Disk Access to ~/.local/bin/contactctl (cdhash changed)."
