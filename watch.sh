#!/usr/bin/env bash
# Deploy on every save while developing. Ctrl-C to stop.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "watching $SRC (Ctrl-C to stop)"
while true; do
  inotifywait -qr -e close_write,create,delete,move \
    --exclude '(\.git/|__pycache__|\.pyc$|\.swp$|~$)' \
    "$SRC" >/dev/null
  sleep 0.2
  "$SRC/deploy.sh"
done
