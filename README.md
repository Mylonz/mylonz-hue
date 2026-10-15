# Hue Bluetooth

An Omarchy shell plugin for controlling **Philips Hue Bluetooth bulbs with no
Hue bridge and no Zigbee dongle**. It uses [`HueBLE`](https://hueble.readthedocs.io/)
over BlueZ for the Bluetooth work and shows up as one bar widget with a popup.

The bar glyph doubles as the state: a plain bulb when everything is off, a
glowing bulb when a light is on. Left-click opens the popup, right-click toggles
the selected bulb.

## What it controls

- On / off
- Brightness (0–100 %)
- Colour temperature (2000–6500 K, per-bulb mireds range)
- Colour (XY, via a row of presets) — full-colour bulbs only
- Effects (candle, fireplace, prism, sparkle, opal, glisten, underwater,
  cosmos, sunbeam, enchant) — for bulbs that support them
- Multiple bulbs: a chip row appears when more than one is configured

The daemon polls each bulb's capabilities and the popup only shows the controls
that bulb actually has. A white-ambiance filament bulb, for example, exposes
brightness and colour temperature but no colour, so the swatch row stays hidden.

Not every effect works on every bulb — the bulb itself rejects unsupported ones
with a GATT application error. The optional per-bulb `effects` allowlist (below)
pins the list a light actually accepts, and the picker shows only those chips
plus **None**.

## Requirements

- Omarchy 4.x (Quickshell shell) with Bluetooth running
- A Philips Hue **Bluetooth** bulb (the bridge/Zigbee models are not BLE)
- `uv` **or** Python 3.11+ with `venv` (the installer creates its own venv)

## Install

```bash
cd ~/.config/omarchy/plugins/mylonz.hue
./install.sh                                   # runtime only
# or, to seed a bulb at the same time:
./install.sh AA:BB:CC:DD:EE:FF "Lounge lamp"
```

`install.sh` creates `~/.local/share/omarchy-hue/venv`, installs HueBLE into
it, and symlinks `omarchy-hue` into `~/.local/bin`.

Then enable the widget and restart/reload the shell:

```bash
omarchy plugin enable mylonz.hue
# the shell hot-reloads plugin code; if the bar does not pick it up:
omarchy-shell shell rescanPlugins
```

## Pairing the bulb (first time only)

A Hue bulb only accepts a new pairing while it is **discoverable**. If the bulb
is already paired to a phone's Hue app, it will reject pairing with
`AuthenticationFailed` until you do one of:

1. **Hue app → Settings → Voice assistants → Amazon Alexa / Google Home →
   Make discoverable** (keep the app open, then pair from the popup), or
2. **Factory-reset the bulb** (not recommended — the Bluetooth address changes).

Then in the popup press **Retry** on the error, or click **Pair a light… →
Scan for lights** and hit **Pair** next to the bulb. You can also do it from the
CLI:

```bash
omarchy-hue scan
omarchy-hue pair AA:BB:CC:DD:EE:FF Lounge
```

Pairing is remembered by BlueZ, so it is a one-time step.

## Usage

Click the bar icon to open the popup. Keyboard while it is open:

| Key | Action |
|-----|--------|
| `Esc` | close |
| `Tab` / `Shift+Tab` | move to the neighbouring bar panel |
| `Space` / `Enter` | toggle power |
| `p` | toggle power |
| `+` / `-` | brightness ±16 |
| `r` | re-read state |

IPC: `omarchy-shell mylonz.hue open` / `close` / `toggle`.

### CLI

```bash
omarchy-hue state                     # one line per bulb
omarchy-hue state --json
omarchy-hue set lounge power toggle   # on | off | toggle
omarchy-hue set lounge brightness 200 # 1–254 or "50%"
omarchy-hue set lounge temp 2700      # Kelvin, or <1000 as mireds
omarchy-hue set lounge color '#ff8800'
omarchy-hue set lounge xy 0.45 0.41
omarchy-hue set lounge effect candle 128
omarchy-hue effects                   # list every effect name
omarchy-hue effects-set lounge candle sparkle glisten   # limit a bulb's picker
omarchy-hue scan
omarchy-hue pair <ADDRESS> [Name]
omarchy-hue pair-watch <ADDRESS> [Name] --timeout 600   # retry until discoverable
omarchy-hue remove lounge
omarchy-hue ensure | stop
```

## Configuration

`~/.config/omarchy/hue.json`:

```json
{
  "version": 1,
  "idleDisconnectSec": 300,
  "pollIntervalSec": 30,
  "bulbs": [
    {
      "id": "lounge",
      "address": "AA:BB:CC:DD:EE:FF",
      "name": "Lounge",
      "effects": ["candle", "sparkle", "glisten"]
    }
  ]
}
```

- `idleDisconnectSec` — drop the BLE link after this much inactivity to save
  bulb/radio power; `0` keeps it connected.
- `pollIntervalSec` — how often to poll a connected bulb (push updates also
  arrive from the bulb).
- `effects` (optional, per bulb) — the effects the picker offers for this light.
  Omit it to offer the full catalogue. Write it with
  `omarchy-hue effects-set <id> <effect...>` (or edit the file and reload the
  daemon); the daemon enforces it before the GATT write, so unsupported effects
  are rejected with a clear message instead of a Bluetooth error.

The widget's own layout entry lives in `~/.config/omarchy/shell.json` like every
other bar widget; move it with `omarchy bar move mylonz.hue --section right`.

## How it works

```
bar widget (Panel.qml)                 BLE + HueBLE
  watches state.json  ──►  hued.py  ◄──────────────►  Hue bulb
  runs omarchy-hue    ──►  (daemon, unix socket)
```

QML cannot talk to Bluetooth, so a small daemon owns the LE connections:

- `daemon/huecore.py` — paths, config/state, the socket protocol, a mock backend.
- `daemon/hued.py` — the daemon: connections, polling, idle disconnect, scanning.
- `daemon/hue.py` — the `omarchy-hue` CLI the panel shells out to.
- `Panel.qml` — the bar widget and popup.

The daemon writes `$XDG_RUNTIME_DIR/omarchy-hue/state.json` in place on every
change; the panel watches it and also re-reads it every four seconds as a
safety net. Actions run the CLI, which auto-starts the daemon if needed
(guarded by a lock file so several `ensure` calls cannot spawn duplicates).

## Troubleshooting

- **"Not bonded / put it in pairing mode"** — the bulb is not discoverable; see
  [Pairing](#pairing-the-bulb-first-time-only).
- **"Bulb not found"** — power it on and move it closer; discovery retries a
  few times before giving up.
- Daemon log: `~/.local/state/omarchy-hue/daemon.log`
- Restart: `omarchy-hue stop && omarchy-hue ensure`
- Dry-run the whole UI with a fake bulb:
  `OMARCHY_HUE_BACKEND=mock omarchy-hue ensure`

## Notes

- HueBLE caps brightness writes at 1–254; the UI shows a percentage.
- Effects use HueBLE's `set_colour_effect`/`set_temperature_effect`, which also
  set brightness and colour in the same write.
- Effect support is per bulb: the bulb rejects effects it does not implement
  with a GATT application error, so the picker shows the bulb's `effects`
  allowlist (or the full catalogue when none is set) and always offers **None**
  to clear an active effect.
- The daemon holds an exclusive lock, so only one instance ever owns the
  Bluetooth link; `omarchy-hue stop` waits for it to exit before returning.

## Project layout, git, and redeploy

The source of truth is the project directory `~/Projects/mylonz-hue`; the
Omarchy plugin directory is a deployed copy. `deploy.sh` rsyncs the project
into place and nudges the shell to reload.

```
~/Projects/mylonz-hue/                  git repo — edit here
    Panel.qml  manifest.json
    daemon/  bin/  install.sh
    deploy.sh  watch.sh  Makefile
        |  ./deploy.sh   (or `make deploy` / `make watch`)
        v
~/.config/omarchy/plugins/mylonz.hue/   deployed copy the shell loads
```

Common commands:

```bash
make deploy     # copy into the plugin dir + reload the shell
make watch      # deploy on every save (Ctrl-C to stop)
make install    # deploy, then build the venv and ~/.local/bin shims
make enable     # deploy, then enable the bar widget
make status     # show the plugin's registration
```

Intentionally not committed: `__pycache__/`, the venv, this machine's
`~/.config/omarchy/hue.json`, and runtime state. They live outside the plugin
directory or are covered by `.gitignore`.

Push it and redeploy elsewhere:

```bash
cd ~/Projects/mylonz-hue
git remote add origin git@github.com:<you>/mylonz-hue.git
git push -u origin main

# on the other machine:
omarchy plugin add https://github.com/<you>/mylonz-hue.git --enable --yes
cd ~/.config/omarchy/plugins/mylonz.hue && ./install.sh
omarchy-hue pair <ADDRESS> "<Name>"     # or the popup's "Pair a light…" section
```

`omarchy plugin add` checks the repo out under the manifest id, so it always
lands at `.../plugins/mylonz.hue/`. That machine pairs the bulb once (see
[Pairing](#pairing-the-bulb-first-time-only)). Afterwards, update your own
machine with `git pull && make deploy` in the project, or a machine that
installed it directly with `omarchy plugin update mylonz.hue`.
