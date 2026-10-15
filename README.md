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
  cosmos, sunbeam, enchant) — full-colour bulbs only
- Multiple bulbs: a chip row appears when more than one is configured

The daemon polls each bulb's capabilities and the popup only shows the controls
that bulb actually has. A white-ambiance filament bulb, for example, exposes
brightness and colour temperature but no colour, so the swatch row and the
effects row stay hidden.

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
    { "id": "lounge", "address": "AA:BB:CC:DD:EE:FF", "name": "Lounge" }
  ]
}
```

- `idleDisconnectSec` — drop the BLE link after this much inactivity to save
  bulb/radio power; `0` keeps it connected.
- `pollIntervalSec` — how often to poll a connected bulb (push updates also
  arrive from the bulb).

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
- Effects are restricted to full-colour bulbs: on white-ambiance bulbs HueBLE's
  temperature-effect path is unreliable (some effects are rejected with a GATT
  application error, and an active effect is not always cleared), so the row is
  hidden there rather than offering buttons that fail.
- The daemon holds an exclusive lock, so only one instance ever owns the
  Bluetooth link; `omarchy-hue stop` waits for it to exit before returning.

## Packaging, git, and redeploy

The plugin directory is already a complete, self-contained project (manifest
at the root), and that is exactly the layout `omarchy plugin add` expects — so
it can be a git repo directly.

Version it:

```bash
cd ~/.config/omarchy/plugins/mylonz.hue
git init -b main
git add -A
git commit -m "Hue Bluetooth plugin"
git remote add origin git@github.com:<you>/mylonz-hue.git
git push -u origin main
```

Intentionally not committed: `__pycache__/`, the venv, this machine's
`~/.config/omarchy/hue.json`, and runtime state. They live outside the plugin
directory or are covered by `.gitignore`.

Redeploy on another machine:

```bash
omarchy plugin add https://github.com/<you>/mylonz-hue.git --enable
cd ~/.config/omarchy/plugins/mylonz.hue && ./install.sh
omarchy-hue pair <ADDRESS> "<Name>"     # or the popup's "Pair a light…" section
```

That machine pairs the bulb once (see
[Pairing](#pairing-the-bulb-first-time-only)). Update later with
`omarchy plugin update mylonz.hue`.

If you would rather keep the source outside a config directory, develop in
e.g. `~/Work/mylonz-hue/` and copy it into
`~/.config/omarchy/plugins/mylonz.hue/`; the shell hot-reloads on save either
way.
