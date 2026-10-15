#!/usr/bin/env bash
# Bootstrap the Omarchy Hue plugin: create the Python venv that carries
# HueBLE, expose `omarchy-hue` on PATH, and (optionally) seed the first bulb.
#
#   ./install.sh                                  # just set up the runtime
#   ./install.sh AA:BB:CC:DD:EE:FF "Lounge lamp"  # also add a bulb to the config
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${OMARCHY_HUE_DATA:-$HOME/.local/share/omarchy-hue}"
VENV="${OMARCHY_HUE_VENV:-$DATA_DIR/venv}"
CONFIG="${OMARCHY_HUE_CONFIG:-$HOME/.config/omarchy/hue.json}"
BIN_DIR="$HOME/.local/bin"

echo "==> plugin:  $PLUGIN_DIR"
echo "==> venv:    $VENV"

mkdir -p "$DATA_DIR" "$BIN_DIR"

REQUIREMENTS="$PLUGIN_DIR/requirements.txt"

if [[ ! -x "$VENV/bin/python" ]]; then
  if command -v uv >/dev/null 2>&1; then
    uv venv "$VENV"
  else
    python3 -m venv "$VENV"
    "$VENV/bin/python" -m pip install --quiet --upgrade pip
  fi
fi

# Install/refresh the pinned dependency. Idempotent, so this also brings an
# existing venv up to the pinned version.
if command -v uv >/dev/null 2>&1; then
  uv pip install --python "$VENV/bin/python" --quiet -r "$REQUIREMENTS"
else
  "$VENV/bin/python" -m pip install --quiet -r "$REQUIREMENTS"
fi

"$VENV/bin/python" -c 'import importlib.metadata as m; print("==> HueBLE", m.version("HueBLE"))'

chmod +x "$PLUGIN_DIR/bin/omarchy-hue" "$PLUGIN_DIR/bin/omarchy-hue-daemon"
ln -sf "$PLUGIN_DIR/bin/omarchy-hue" "$BIN_DIR/omarchy-hue"
ln -sf "$PLUGIN_DIR/bin/omarchy-hue-daemon" "$BIN_DIR/omarchy-hue-daemon"

if [[ ! -f "$CONFIG" ]]; then
  mkdir -p "$(dirname "$CONFIG")"
  ADDRESS="${1:-}"
  LABEL="${2:-}"
  if [[ -n "$ADDRESS" ]]; then
    python3 - "$CONFIG" "$ADDRESS" "$LABEL" <<'PY'
import json, sys
config, address, label = sys.argv[1], sys.argv[2].upper(), sys.argv[3]
bulb = {"address": address}
if label:
    bulb["name"] = label
    bulb["id"] = "".join(c.lower() if c.isalnum() else "-" for c in label).strip("-") or "light"
with open(config, "w") as fh:
    json.dump({"version": 1, "idleDisconnectSec": 300, "pollIntervalSec": 30,
               "bulbs": [bulb]}, fh, indent=2)
    fh.write("\n")
print(f"==> seeded {config}")
PY
  else
    echo '{"version": 1, "idleDisconnectSec": 300, "pollIntervalSec": 30, "bulbs": []}' > "$CONFIG"
    echo "==> wrote empty config $CONFIG"
  fi
else
  echo "==> config already exists: $CONFIG"
fi

echo
echo "Done. Try:"
echo "  omarchy-hue ensure"
echo "  omarchy-hue scan"
echo "  omarchy-hue pair <ADDRESS> <Name>"
echo
echo "The bulb must be in pairing mode the first time you pair."
