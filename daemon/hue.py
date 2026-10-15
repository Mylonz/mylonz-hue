#!/usr/bin/env python3
"""CLI for the Omarchy Hue BLE plugin.

Every command talks to the `hued.py` daemon over its Unix socket, starting it
on demand. QML calls this; humans can too.

    omarchy-hue state --json
    omarchy-hue set lounge power toggle
    omarchy-hue set lounge brightness 200
    omarchy-hue set lounge temp 2700
    omarchy-hue set lounge color '#ff8800'
    omarchy-hue set lounge effect candle
    omarchy-hue scan
    omarchy-hue pair AA:BB:CC:DD:EE:FF Lounge
    omarchy-hue stop
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import huecore as core  # noqa: E402

EFFECTS = [
    "none",
    "candle",
    "fireplace",
    "prism",
    "sparkle",
    "opal",
    "glisten",
    "underwater",
    "cosmos",
    "sunbeam",
    "enchant",
]


def fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def call(payload: dict, timeout: float = 60.0) -> dict:
    if not core.ensure_daemon():
        raise RuntimeError(
            "daemon did not start; check " + str(core.DAEMON_LOG)
        )
    reply = core.send_request(payload, timeout=timeout)
    if not reply.get("ok"):
        raise RuntimeError(reply.get("error") or "daemon error")
    return reply


def parse_power(value: str):
    low = value.lower()
    if low in ("toggle", "switch"):
        return "toggle"
    if low in ("on", "true", "1", "yes"):
        return True
    if low in ("off", "false", "0", "no"):
        return False
    raise ValueError(f"bad power value {value!r}")


def build_fields(field: str, value: list[str]) -> dict:
    field = field.lower()
    if field == "power":
        return {"power": parse_power(value[0])}
    if field in ("brightness", "bri"):
        raw = value[0].rstrip("%")
        bright = float(raw)
        if value[0].endswith("%"):
            bright = bright / 100.0 * 254.0
        return {"brightness": max(1, min(254, int(round(bright))))}
    if field in ("temp", "temperature", "colour_temp", "color_temp"):
        number = float(value[0].rstrip("k").rstrip("m"))
        if number >= 1000:  # Kelvin
            return {"kelvin": int(number)}
        return {"mireds": int(number)}
    if field in ("color", "colour", "hex"):
        return {"hex": value[0]}
    if field == "xy":
        return {"xy": [float(value[0]), float(value[1])]}
    if field == "effect":
        fields = {"effect": value[0]}
        if len(value) > 1:
            fields["speed"] = int(value[1])
        return fields
    if field == "speed":
        return {"effect": "none", "speed": int(value[0])}
    raise ValueError(f"unknown field {field!r}")


# ------------------------------------------------------------------- commands
def cmd_state(args) -> int:
    reply = call({"cmd": "state"})
    state = reply["state"]
    if args.json:
        print(json.dumps(state))
        return 0
    bulbs = state.get("bulbs", [])
    if not bulbs:
        print("no bulbs configured")
        return 0
    for bulb in bulbs:
        power = "on" if bulb.get("power") else "off"
        bright = bulb.get("brightness")
        mireds = bulb.get("mireds")
        kelvin = core.mireds_to_kelvin(mireds) if mireds else None
        line = f"{bulb['id']:<16} {power:<3} {bulb.get('label') or ''}"
        if bright is not None:
            line += f"  brightness={bright}"
        if kelvin:
            line += f"  {kelvin}K"
        if bulb.get("effect"):
            line += f"  effect={bulb['effect']}"
        if bulb.get("error"):
            line += f"  error={bulb['error']}"
        print(line)
    return 0


def cmd_list(args) -> int:
    reply = call({"cmd": "state"})
    for bulb in reply["state"].get("bulbs", []):
        print(f"{bulb['id']}\t{bulb['address']}\t{bulb.get('label') or ''}")
    return 0


def cmd_set(args) -> int:
    fields = build_fields(args.field, args.value)
    reply = call({"cmd": "set", "id": args.id, "fields": fields})
    if args.json:
        print(json.dumps(reply.get("bulb")))
    return 0


def cmd_toggle(args) -> int:
    call({"cmd": "set", "id": args.id, "fields": {"power": "toggle"}})
    return 0


def cmd_refresh(args) -> int:
    if args.id:
        call({"cmd": "refresh", "id": args.id})
    else:
        call({"cmd": "state"})
        for bulb in call({"cmd": "state"})["state"].get("bulbs", []):
            try:
                call({"cmd": "refresh", "id": bulb["id"]})
            except RuntimeError as exc:
                print(f"{bulb['id']}: {exc}", file=sys.stderr)
    return 0


def cmd_scan(args) -> int:
    reply = call({"cmd": "scan", "timeout": args.timeout}, timeout=args.timeout + 30)
    for light in reply.get("discovered", []):
        print(f"{light['address']}\t{light.get('label') or ''}")
    return 0


def cmd_pair(args) -> int:
    reply = call(
        {"cmd": "pair", "address": args.address, "label": args.label},
        timeout=120,
    )
    bulb = reply.get("bulb") or {}
    print(f"{bulb.get('id', '')}\t{bulb.get('address', '')}\t{bulb.get('label', '')}")
    return 0


def cmd_pair_watch(args) -> int:
    import time

    deadline = time.time() + args.timeout
    last = ""
    while True:
        try:
            reply = call(
                {"cmd": "pair", "address": args.address, "label": args.label},
                timeout=120,
            )
            bulb = reply.get("bulb") or {}
            print(f"paired\t{bulb.get('id', '')}\t{bulb.get('address', '')}\t{bulb.get('label', '')}")
            return 0
        except (RuntimeError, TimeoutError, OSError) as exc:
            last = str(exc)
            print(f"still waiting: {last}", file=sys.stderr, flush=True)
        if time.time() >= deadline:
            return fail(f"gave up after {args.timeout}s; last error: {last}")
        time.sleep(max(2, args.interval))


def cmd_remove(args) -> int:
    call({"cmd": "remove", "id": args.id})
    return 0


def cmd_ensure(args) -> int:
    if core.ensure_daemon():
        return 0
    return fail("daemon did not start; check " + str(core.DAEMON_LOG))


def cmd_stop(args) -> int:
    if core.ping():
        core.send_request({"cmd": "stop"}, timeout=5)
        # Wait for the process to actually leave so a following `ensure`
        # cannot race it (the daemon holds a lifetime lock).
        import time

        deadline = time.time() + 8
        while time.time() < deadline and core.ping(timeout=0.3):
            time.sleep(0.15)
    return 0


def cmd_effects(args) -> int:
    if not args.id:
        print("\n".join(EFFECTS))
        return 0
    state = call({"cmd": "state"})["state"]
    needle = args.id.upper()
    bulb = next(
        (
            b
            for b in state.get("bulbs", [])
            if b["id"].upper() == needle or b["address"].upper() == needle
        ),
        None,
    )
    if bulb is None:
        return fail(f"unknown bulb {args.id!r}")
    allow = (bulb.get("capabilities") or {}).get("effectList")
    if allow:
        for name in allow:
            print(name.lower())
    else:
        print("# not pinned - full catalogue offered:")
        print("\n".join(EFFECTS))
    return 0


def cmd_effects_probe(args) -> int:
    reply = call({"cmd": "probe-effects", "id": args.id}, timeout=1200)
    results = reply.get("results") or {}
    for name, ok in results.items():
        print(f"{'ok  ' if ok else 'fail'}  {name.lower()}")
    allow = (reply.get("bulb", {}).get("capabilities") or {}).get("effectList")
    print("effects-set:", " ".join(e.lower() for e in allow) if allow else "(all)")
    return 0


def cmd_effects_set(args) -> int:
    reply = call({"cmd": "set-effects", "id": args.id, "effects": args.effects})
    bulb = reply.get("bulb") or {}
    allow = (bulb.get("capabilities") or {}).get("effectList")
    print(f"{args.id}: effects = {allow if allow else '(all)'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="omarchy-hue", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ensure", help="start the daemon if it is not running")
    p.set_defaults(func=cmd_ensure)

    p = sub.add_parser("state", help="show the current state")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_state)

    p = sub.add_parser("list", help="list configured bulbs")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("set", help="set a bulb field")
    p.add_argument("id")
    p.add_argument("field")
    p.add_argument("value", nargs="+")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("toggle", help="toggle power")
    p.add_argument("id")
    p.set_defaults(func=cmd_toggle)

    p = sub.add_parser("refresh", help="poll one or all bulbs")
    p.add_argument("id", nargs="?")
    p.set_defaults(func=cmd_refresh)

    p = sub.add_parser("scan", help="scan for Hue lights")
    p.add_argument("--timeout", type=int, default=8)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("pair", help="pair and add a light")
    p.add_argument("address")
    p.add_argument("label", nargs="?")
    p.set_defaults(func=cmd_pair)

    p = sub.add_parser(
        "pair-watch", help="keep retrying pairing until the bulb is discoverable"
    )
    p.add_argument("address")
    p.add_argument("label", nargs="?")
    p.add_argument("--timeout", type=int, default=300, help="give up after N seconds")
    p.add_argument("--interval", type=int, default=8, help="seconds between attempts")
    p.set_defaults(func=cmd_pair_watch)

    p = sub.add_parser("remove", help="forget a light")
    p.add_argument("id")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("stop", help="stop the daemon")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("effects", help="list effect names, or a bulb's effects")
    p.add_argument("id", nargs="?", help="bulb id or address; omit for the catalogue")
    p.set_defaults(func=cmd_effects)

    p = sub.add_parser(
        "effects-probe", help="test every effect against a bulb and record the ones it accepts"
    )
    p.add_argument("id")
    p.set_defaults(func=cmd_effects_probe)

    p = sub.add_parser(
        "effects-set", help="limit a bulb to the effects it actually supports"
    )
    p.add_argument("id")
    p.add_argument("effects", nargs="*", help="effect names; omit to allow all")
    p.set_defaults(func=cmd_effects_set)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (RuntimeError, ValueError, TimeoutError, OSError) as exc:
        return fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
