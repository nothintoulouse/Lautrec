#!/usr/bin/env bash
# Install Lautrec's launchd agents. Substitutes the {{...}} placeholders
# in the plist templates, writes them to ~/Library/LaunchAgents, and
# (re)loads them. Pass --with-watcher to also load the optional
# watcher agent. Re-run after editing a plist template.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$(command -v python3)"
AGENTS_DIR="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
mkdir -p "$AGENTS_DIR" "$ROOT/logs"

if [ ! -f "$ROOT/config.toml" ]; then
    echo "error: $ROOT/config.toml missing — copy config.example.toml first" >&2
    exit 1
fi

AGENTS=(local.lautrec-webhook local.lautrec-worker)
# The watcher is optional — it only does anything if you keep a manifest.md.
if [ "${1:-}" = "--with-watcher" ]; then
    AGENTS+=(local.lautrec-watcher)
fi

for name in "${AGENTS[@]}"; do
    dst="$AGENTS_DIR/$name.plist"
    sed -e "s|{{PYTHON}}|$PYTHON|g" \
        -e "s|{{PROJECT_ROOT}}|$ROOT|g" \
        -e "s|{{HOME}}|$HOME|g" \
        "$ROOT/launchd/$name.plist" > "$dst"
    launchctl bootout "$DOMAIN/$name" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$dst"
    echo "loaded $name"
done

echo "Lautrec launchd agents installed. Logs: $ROOT/logs/"
