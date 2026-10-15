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

if [[ ! -x "$VENV/bin/python" ]]; then
  if command -v uv >/dev/null 2>&1; then
    uv venv "$VENV"
    uv pip install --python "$VENV/bin/python" --quiet HueBLE
  else
    python3 -m venv "$VENV"
    "$VENV/bin/python" -m pip install --quiet --upgrade pip
    "$VENV/bin/python" -m pip install --quiet HueBLE
  fi
else
  echo "==> venv already present (skipping install)"
fi

"$VENV/bin/python" -c 'import HueBLE; print("==> HueBLE", HueBLE.__version__ if hasattr(HueBLE, "__version__") else "ready")' 2>/dev/null || true

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
