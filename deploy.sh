#!/usr/bin/env bash
# Deploy this project into the Omarchy plugin directory and hot-reload the
# shell. Edit here, then run ./deploy.sh (or `make deploy` / `make watch`).
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${OMARCHY_HUE_PLUGIN_DIR:-$HOME/.config/omarchy/plugins/mylonz.hue}"

mkdir -p "$DEST"
rsync -a --delete \
  --exclude '.git' \
  --exclude '__pycache__' \
  --exclude 'venv' \
  --exclude '*.pyc' \
  --exclude '.DS_Store' \
  "$SRC"/ "$DEST"/

chmod +x "$DEST/install.sh" "$DEST/bin/"* 2>/dev/null || true
rm -rf "$DEST/daemon/__pycache__"

omarchy plugin validate "$DEST" >/dev/null
echo "deployed: $SRC -> $DEST"

# Nudge the shell to reload even if its file watcher missed the change.
omarchy-shell shell rescanPlugins >/dev/null 2>&1 || true
