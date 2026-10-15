#!/usr/bin/env python3
"""Omarchy Hue daemon.

Owns the Bluetooth LE connections to the configured Hue bulbs, keeps a JSON
snapshot in $XDG_RUNTIME_DIR/omarchy-hue/state.json for the shell plugin to
watch, and answers line-delimited JSON commands on a Unix socket.

Run it through `omarchy-hue ensure` (the CLI auto-starts it); it is not meant
to be launched interactively.
"""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import signal
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True  # keep the plugin tree clean for the shell watcher
sys.path.insert(0, str(Path(__file__).resolve().parent))

import huecore as core  # noqa: E402

LOG = logging.getLogger("hued")

CONNECT_TIMEOUT = 60
IDLE_TICK = 15.0


# --------------------------------------------------------------------- errors
def _chain_text(exc: BaseException) -> str:
    """Join an exception and its causes/contexts, so a specific failure
    (AuthenticationFailed) is visible even when a retry wrapper is what
    finally reaches us."""
    parts: list[str] = []
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        parts.append(str(exc))
        exc = exc.__cause__ or exc.__context__
    return " | ".join(parts)


def explain(exc: BaseException) -> str:
    text = _chain_text(exc)
    low = text.lower()
    if (
        "authenticationfailed" in low
        or "authentication failed" in low
        or "not authenticated" in low
    ):
        return (
            "Not bonded to the bulb. Put it in pairing mode: Hue app → Settings → "
            "Voice assistants → Make discoverable, then retry."
        )
    if "application-specific error" in low or "0x80" in low:
        return "The bulb rejected that command (not supported for this bulb)."
    if "unable to read" in low or "unable to write" in low or "unlikely error" in low:
        return "Lost the Bluetooth link to the bulb. Retry in a moment."
    if "not found" in low or "no device" in low:
        return "Bulb not found. Power it on and move it closer, then retry."
    if "timed out" in low or "timeout" in low:
        return "Timed out talking to the bulb. Retry in a moment."
    if "connection" in low:
        return f"Connection problem: {text}"
    return text or exc.__class__.__name__


async def find_hue_device(address: str, attempts: int = 3):
    """Find a Hue light's BLEDevice by address.

    HueBLE's `discover_lights` is the author's supported discovery path (it
    matches the Hue service UUID), so prefer it and fall back to a raw address
    lookup. Retries because a bulb that is busy or freshly paired can miss the
    first advertising window.
    """
    from bleak import BleakScanner  # type: ignore
    from HueBLE import discover_lights  # type: ignore

    address = str(address).upper()
    for attempt in range(attempts):
        if attempt:
            await asyncio.sleep(1.5)
        try:
            for device in await discover_lights(timeout=8):
                if str(device.address).upper() == address:
                    return device
        except Exception as exc:  # noqa: BLE001 - keep trying
            LOG.debug("discover_lights attempt %d failed: %s", attempt + 1, exc)
        try:
            device = await BleakScanner.find_device_by_address(address, timeout=8)
            if device is not None:
                return device
        except Exception as exc:  # noqa: BLE001
            LOG.debug("find_device_by_address attempt %d failed: %s", attempt + 1, exc)
    return None


# ----------------------------------------------------------------------- bulb
class Bulb:
    """One configured light: its cached state plus the HueBLE client."""

    def __init__(self, daemon: "Daemon", entry: dict) -> None:
        self.daemon = daemon
        self.id = str(entry.get("id") or core.slugify_name(entry.get("name")))
        self.address = str(entry["address"]).upper()
        self.label = str(entry.get("name") or entry.get("label") or self.id)
        raw_effects = entry.get("effects")
        # Optional per-bulb allowlist of effects this light actually accepts.
        # Enforced before the write so unsupported effects fail with a clear
        # message instead of a GATT application error.
        self.effects_allow = (
            [str(e).upper() for e in raw_effects if str(e).strip()]
            if isinstance(raw_effects, list) and raw_effects
            else None
        )
        self.light = None
        self._lock = asyncio.Lock()
        self._last_use = 0.0
        self._synced_once = False
        # The bulb's effect characteristic keeps reporting the last effect even
        # after a static brightness/temperature write has stopped it, so the
        # selected effect is our intent, not the (stale) reported value.
        self.effect_intent: str | None = None
        self.effect_known = False
        self.state = {
            "id": self.id,
            "address": self.address,
            "label": self.label,
            "connected": False,
            "available": False,
            "paired": None,
            "power": None,
            "brightness": None,
            "mireds": None,
            "minMireds": 153,
            "maxMireds": 500,
            "xy": None,
            "colourMode": None,
            "effect": None,
            "effectSpeed": None,
            "model": entry.get("model"),
            "firmware": None,
            "busy": None,
            "error": None,
            "updated": None,
            "capabilities": {
                "power": None,
                "brightness": None,
                "colourTemp": None,
                "colourXy": None,
                "effects": None,
                "effectList": self.effects_allow,
            },
        }

    # -- backend plumbing
    async def _get_light(self):
        if self.light is not None:
            return self.light
        if core.use_mock():
            self.light = core.MockLight(self.address, self.label)
        else:
            from HueBLE import HueBleLight  # type: ignore

            device = await find_hue_device(self.address)
            if device is None:
                raise RuntimeError(f"bulb {self.address} not found")
            self.light = HueBleLight(device)
        self.light.add_callback_on_state_changed(self._on_change)
        return self.light

    def _on_change(self) -> None:
        self._sync_from_light()
        self.daemon.mark_dirty()

    def _sync_from_light(self) -> None:
        light = self.light
        if light is None:
            return
        s = self.state
        try:
            s["connected"] = bool(light.connected)
            s["available"] = bool(getattr(light, "available", False))
            s["paired"] = getattr(light, "authenticated", None)
            if not light.connected:
                return
            s["power"] = light.power_state
            s["brightness"] = light.brightness
            s["mireds"] = light.colour_temp
            s["minMireds"] = light.minimum_mireds or 153
            s["maxMireds"] = light.maximum_mireds or 500
            xy = light.colour_xy
            s["xy"] = [round(xy[0], 4), round(xy[1], 4)] if xy else None
            effect = light.effect
            reported = effect[0].name if effect and effect[0] else None
            if not self.effect_known:
                # Adopt whatever the bulb was already doing, once.
                if reported and reported != "NONE":
                    self.effect_intent = reported
                self.effect_known = True
            if self.effect_intent:
                s["effect"] = self.effect_intent
                if effect and effect[1]:
                    s["effectSpeed"] = effect[1]
            else:
                s["effect"] = None
                s["effectSpeed"] = None
            s["colourMode"] = "temp" if light.colour_temp_mode else "xy"
            s["model"] = light.model or s["model"]
            s["firmware"] = light.firmware
            s["capabilities"] = {
                "power": bool(getattr(light, "supports_on_off", False)),
                "brightness": bool(getattr(light, "supports_brightness", False)),
                "colourTemp": bool(getattr(light, "supports_colour_temp", False)),
                "colourXy": bool(getattr(light, "supports_colour_xy", False)),
                "effects": bool(getattr(light, "supports_effects", False)),
                "effectList": self.effects_allow,
            }
            self._synced_once = True
            label = getattr(light, "name_in_app", None) or getattr(light, "name", None)
            if label and label not in ("Unknown", "None"):
                s["label"] = label
            s["error"] = None
            s["updated"] = time.time()
        except Exception as exc:  # pragma: no cover - defensive
            LOG.debug("state sync for %s failed: %s", self.id, exc)

    async def _connect_locked(self, poll: bool = True) -> None:
        light = await self._get_light()
        if not light.connected:
            await light.connect(
                connection_timeout=CONNECT_TIMEOUT, wait_timeout=CONNECT_TIMEOUT
            )
        if poll:
            await light.poll_state()
        self._last_use = time.monotonic()
        self.state["error"] = None
        self._sync_from_light()

    async def connect(self) -> None:
        async with self._lock:
            try:
                await self._connect_locked(poll=True)
            except Exception as exc:
                self.state["error"] = explain(exc)
                self.state["connected"] = False
                # Force a fresh discovery next time: a retry usually comes
                # right after the user put the bulb into pairing mode, and the
                # cached BLEDevice from the failed attempt may be stale.
                self.light = None
                self.daemon.mark_dirty()
                raise
        self.daemon.mark_dirty()

    async def refresh(self) -> None:
        async with self._lock:
            try:
                await self._connect_locked(poll=True)
            except Exception as exc:
                self.state["error"] = explain(exc)
                self.state["connected"] = False
                self.light = None
                self.daemon.mark_dirty()
                raise
        self.daemon.mark_dirty()

    async def apply(self, fields: dict) -> None:
        self.state["busy"] = "applying"
        self.daemon.mark_dirty()
        try:
            async with self._lock:
                light = await self._get_light()
                if not light.connected:
                    await light.connect(
                        connection_timeout=CONNECT_TIMEOUT, wait_timeout=CONNECT_TIMEOUT
                    )
                # Capabilities (colour? effects?) are only known after a poll;
                # do one before the first write so unsupported fields are
                # rejected with a clear message instead of a GATT error.
                if not self._synced_once:
                    await light.poll_state()
                    self._sync_from_light()
                await self._apply_locked(light, fields)
                self._last_use = time.monotonic()
                self.state["error"] = None
            self._sync_from_light()
            # HueBLE's setters do not update its cached properties, so reflect
            # the write immediately instead of letting the UI snap back until
            # the confirming poll lands a couple of seconds later.
            self._apply_optimistic(fields)
        except Exception as exc:
            self.state["error"] = explain(exc)
            self.state["connected"] = False
            self.light = None
            raise
        finally:
            self.state["busy"] = None
            self.daemon.mark_dirty()

    def _apply_optimistic(self, fields: dict) -> None:
        s = self.state
        if "power" in fields:
            value = fields["power"]
            s["power"] = (not s.get("power")) if value == "toggle" else bool(value)
        if "brightness" in fields:
            s["brightness"] = max(1, min(254, int(fields["brightness"])))
        if "mireds" in fields:
            s["mireds"] = int(fields["mireds"])
            s["colourMode"] = "temp"
        if "kelvin" in fields:
            s["mireds"] = core.kelvin_to_mireds(fields["kelvin"])
            s["colourMode"] = "temp"
        if "hex" in fields:
            s["xy"] = list(core.hex_to_xy(fields["hex"]))
            s["colourMode"] = "xy"
        if "xy" in fields and fields["xy"]:
            s["xy"] = [float(fields["xy"][0]), float(fields["xy"][1])]
            s["colourMode"] = "xy"
        if "effect" in fields:
            raw = str(fields["effect"] or "NONE").upper()
            s["effect"] = None if raw in ("NONE", "OFF", "", "CLEAR") else raw
            if raw not in ("NONE", "OFF", "", "CLEAR") and "speed" in fields:
                s["effectSpeed"] = max(1, min(254, int(fields["speed"] or 128)))
        s["updated"] = time.time()

    async def _apply_locked(self, light, fields: dict) -> None:
        supported = self.state.get("capabilities") or {}
        if ("xy" in fields or "hex" in fields) and not supported.get("colourXy"):
            raise ValueError(
                "This bulb does not support colour (it is white-ambiance only)"
            )
        if ("mireds" in fields or "kelvin" in fields) and not supported.get("colourTemp"):
            raise ValueError("This bulb does not support colour temperature")
        if "brightness" in fields and not supported.get("brightness"):
            raise ValueError("This bulb does not support brightness")
        effect_requested = "effect" in fields and str(
            fields.get("effect") or "NONE"
        ).upper() not in ("NONE", "OFF", "", "CLEAR")
        if effect_requested and not supported.get("effects"):
            raise ValueError("This bulb does not support effects")
        if (
            effect_requested
            and self.effects_allow
            and str(fields.get("effect")).upper() not in self.effects_allow
        ):
            raise ValueError(
                f"This bulb does not support the {fields.get('effect')} effect"
            )
        if "power" in fields:
            value = fields["power"]
            if isinstance(value, str) and value.lower() == "toggle":
                on = not bool(light.power_state)
            else:
                on = bool(value)
            await light.set_power(on)

        # While an effect is running, changing brightness or colour through the
        # individual characteristics stops it. Route those tweaks through the
        # combined effect write instead, so the effect keeps running with the
        # new base values.
        active_effect = None
        if "effect" not in fields:
            current = self.state.get("effect")
            if current and str(current).upper() not in ("NONE", "OFF", "CLEAR"):
                active_effect = str(current).upper()

        if active_effect and any(
            key in fields for key in ("brightness", "mireds", "kelvin", "xy", "hex")
        ):
            await self._apply_with_effect_locked(light, fields, active_effect)
        else:
            if "brightness" in fields:
                await light.set_brightness(int(fields["brightness"]))
            if "mireds" in fields:
                await light.set_colour_temp(int(fields["mireds"]))
            if "kelvin" in fields:
                await light.set_colour_temp(core.kelvin_to_mireds(fields["kelvin"]))
            if "xy" in fields and fields["xy"]:
                x, y = fields["xy"]
                await light.set_colour_xy(float(x), float(y))
            if "hex" in fields:
                x, y = core.hex_to_xy(fields["hex"])
                await light.set_colour_xy(x, y)

        if "effect" in fields:
            await self._apply_effect_locked(light, fields)

    async def _clear_effect_locked(self, light, fields: dict) -> None:
        """Stop a running effect.

        Writing the static temperature characteristic halts the animation.
        (The bulb keeps reporting the old effect id on the effects
        characteristic, which is why `effect_intent` drives what we show.)
        """
        if "mireds" in fields:
            temp = int(fields["mireds"])
        elif "kelvin" in fields:
            temp = core.kelvin_to_mireds(fields["kelvin"])
        else:
            temp = int(light.colour_temp or 366)
        await light.set_colour_temp(temp)

    async def _apply_with_effect_locked(
        self, light, fields: dict, active_effect: str
    ) -> None:
        """Adjust brightness/colour without stopping a running effect."""
        from HueBLE import EffectType  # type: ignore

        try:
            effect = EffectType[active_effect]
        except KeyError:
            return
        speed = max(
            1, min(254, int(fields.get("speed") or self.state.get("effectSpeed") or 128))
        )
        brightness = int(fields.get("brightness") or light.brightness or 128)
        colour = None
        if "xy" in fields and fields["xy"]:
            colour = (float(fields["xy"][0]), float(fields["xy"][1]))
        elif "hex" in fields:
            colour = core.hex_to_xy(fields["hex"])
        if colour is not None:
            await light.set_colour_effect(
                colour[0], colour[1], brightness, effect, speed
            )
            return
        if "mireds" in fields:
            temp = int(fields["mireds"])
        elif "kelvin" in fields:
            temp = core.kelvin_to_mireds(fields["kelvin"])
        else:
            temp = int(light.colour_temp or 366)
        await light.set_temperature_effect(temp, brightness, effect, speed)

    async def _apply_effect_locked(self, light, fields: dict) -> None:
        from HueBLE import EffectType  # type: ignore

        raw = str(fields.get("effect") or "NONE").upper()
        if raw in ("NONE", "OFF", "CLEAR", ""):
            await self._clear_effect_locked(light, fields)
            self.effect_intent = None
            self.effect_known = True
            return
        try:
            effect = EffectType[raw]
        except KeyError as exc:
            raise ValueError(f"unknown effect {raw!r}") from exc
        self.effect_intent = raw
        self.effect_known = True
        speed = max(1, min(254, int(fields.get("speed", 128) or 128)))
        brightness = int(fields.get("brightness") or light.brightness or 128)
        xy = light.colour_xy
        if xy is not None:
            await light.set_colour_effect(xy[0], xy[1], brightness, effect, speed)
        else:
            temp = light.colour_temp or 366
            await light.set_temperature_effect(temp, brightness, effect, speed)

    async def probe_effects(self) -> dict:
        """Try every effect the protocol defines and record which ones the bulb
        accepts, then persist that as this bulb's allowlist."""
        from HueBLE import EffectType  # type: ignore

        names = [e.name for e in EffectType if e is not EffectType.NONE]
        saved = self.effects_allow
        # Bypass any pinned allowlist while probing, otherwise effects we have
        # not tried yet would be rejected before reaching the bulb.
        self.effects_allow = None
        self.state["capabilities"]["effectList"] = None
        self.daemon.mark_dirty()
        results: dict[str, bool] = {}
        try:
            for name in names:
                try:
                    await self.apply({"effect": name, "speed": 128})
                    results[name] = True
                except Exception as exc:  # noqa: BLE001 - record and continue
                    LOG.info("effect probe %s rejected: %s", name, explain(exc))
                    results[name] = False
                await asyncio.sleep(0.4)
            try:
                await self.apply({"effect": "none"})
            except Exception:
                pass
        finally:
            working = [name for name, ok in results.items() if ok]
            self.effects_allow = [name.upper() for name in working] or saved
            self.state["capabilities"]["effectList"] = self.effects_allow
            self.daemon.persist_config()
            self.daemon.mark_dirty()
        return results

    async def disconnect(self) -> None:
        async with self._lock:
            if self.light is None:
                return
            try:
                await self.light.disconnect()
            except Exception as exc:  # pragma: no cover
                LOG.debug("disconnect %s failed: %s", self.id, exc)
            self.state["connected"] = False
        self.daemon.mark_dirty()

    def idle_for(self) -> float:
        if self._last_use <= 0:
            return 1e9
        return time.monotonic() - self._last_use


# --------------------------------------------------------------------- daemon
class Daemon:
    def __init__(self) -> None:
        self.cfg: dict = {}
        self.bulbs: dict[str, Bulb] = {}
        self.discovered: list[dict] = []
        self.started = time.time()
        self._dirty = asyncio.Event()
        self._shutdown = asyncio.Event()
        self._server: asyncio.AbstractServer | None = None
        self._tasks: list[asyncio.Task] = []

    # -- lifecycle
    async def run(self) -> None:
        self.reload_config()
        Path(core.SOCKET_PATH).unlink(missing_ok=True)
        self._server = await asyncio.start_unix_server(
            self._handle, path=str(core.SOCKET_PATH)
        )
        os.chmod(core.SOCKET_PATH, 0o600)
        LOG.info("daemon listening on %s", core.SOCKET_PATH)

        self._tasks = [
            asyncio.create_task(self._writer_loop()),
            asyncio.create_task(self._poll_loop()),
            asyncio.create_task(self._idle_loop()),
        ]
        for bulb in self.bulbs.values():
            self._tasks.append(asyncio.create_task(self._prime(bulb)))
        self.mark_dirty()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._shutdown.set)
            except NotImplementedError:  # pragma: no cover
                pass

        await self._shutdown.wait()
        await self.stop()

    async def stop(self) -> None:
        LOG.info("daemon stopping")
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
        for task in self._tasks:
            task.cancel()
        for bulb in self.bulbs.values():
            await bulb.disconnect()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        try:
            core.write_state(self.snapshot())
        except OSError:
            pass
        Path(core.SOCKET_PATH).unlink(missing_ok=True)

    # -- config
    def reload_config(self) -> None:
        self.cfg = core.load_config()
        entries = self.cfg.get("bulbs", [])
        seen: set[str] = set()
        rebuilt: dict[str, Bulb] = {}
        for entry in entries:
            slug = core.slugify_name(entry.get("name") or entry.get("id"), seen)
            if not entry.get("id"):
                entry["id"] = slug
            seen.add(entry["id"])
            existing = self.bulbs.get(entry["id"])
            if existing and existing.address == str(entry["address"]).upper():
                existing.label = str(entry.get("name") or existing.label)
                existing.state["label"] = existing.label
                raw_effects = entry.get("effects")
                existing.effects_allow = (
                    [str(e).upper() for e in raw_effects if str(e).strip()]
                    if isinstance(raw_effects, list) and raw_effects
                    else None
                )
                existing.state["capabilities"]["effectList"] = existing.effects_allow
                rebuilt[entry["id"]] = existing
            else:
                rebuilt[entry["id"]] = Bulb(self, entry)
        self.bulbs = rebuilt

    def persist_config(self) -> None:
        bulbs = []
        for bulb in self.bulbs.values():
            entry = {"id": bulb.id, "address": bulb.address, "name": bulb.label}
            if bulb.effects_allow:
                entry["effects"] = [e.lower() for e in bulb.effects_allow]
            bulbs.append(entry)
        self.cfg["bulbs"] = bulbs
        core.save_config(self.cfg)

    # -- state snapshot
    def snapshot(self) -> dict:
        bulbs = [b.state for b in self.bulbs.values()]
        return {
            "ts": time.time(),
            "pid": os.getpid(),
            "backend": "mock" if core.use_mock() else "hueble",
            "started": self.started,
            "busy": any(b.get("busy") for b in bulbs),
            "bulbs": bulbs,
            "discovered": self.discovered,
            "summary": {
                "count": len(bulbs),
                "on": sum(1 for b in bulbs if b.get("power")),
                "reachable": sum(1 for b in bulbs if b.get("connected")),
            },
        }

    def mark_dirty(self) -> None:
        self._dirty.set()

    def _find(self, bulb_id: str) -> Bulb:
        bulb = self.bulbs.get(bulb_id)
        if bulb is None:
            for candidate in self.bulbs.values():
                if candidate.address == str(bulb_id).upper():
                    return candidate
            raise KeyError(f"unknown bulb {bulb_id!r}")
        return bulb

    # -- background loops
    async def _writer_loop(self) -> None:
        while True:
            await self._dirty.wait()
            await asyncio.sleep(0.06)  # debounce bursts of state changes
            self._dirty.clear()
            try:
                core.write_state(self.snapshot())
            except OSError as exc:
                LOG.warning("could not write state: %s", exc)

    async def _poll_loop(self) -> None:
        while True:
            interval = max(5, int(self.cfg.get("pollIntervalSec", 30)))
            await asyncio.sleep(interval)
            for bulb in list(self.bulbs.values()):
                if bulb.light is not None and bulb.light.connected:
                    try:
                        await bulb.refresh()
                    except Exception:
                        pass

    async def _idle_loop(self) -> None:
        while True:
            await asyncio.sleep(IDLE_TICK)
            timeout = int(self.cfg.get("idleDisconnectSec", 300))
            if timeout <= 0:
                continue
            for bulb in list(self.bulbs.values()):
                if bulb.light is not None and bulb.light.connected:
                    if bulb.idle_for() > timeout:
                        LOG.info("idle disconnect %s", bulb.id)
                        await bulb.disconnect()

    async def _prime(self, bulb: Bulb) -> None:
        try:
            await bulb.connect()
        except Exception as exc:
            LOG.info("initial connect of %s failed: %s", bulb.id, explain(exc))

    def schedule_poll(self, bulb: Bulb, delay: float = 1.5) -> None:
        async def _later() -> None:
            await asyncio.sleep(delay)
            try:
                await bulb.refresh()
            except Exception:
                pass

        self._tasks.append(asyncio.create_task(_later()))

    # -- scanning / pairing
    async def scan(self, timeout: int = 8) -> list[dict]:
        if core.use_mock():
            found = [
                {"address": "AA:BB:CC:DD:EE:01", "label": "Mock ceiling"},
                {"address": "AA:BB:CC:DD:EE:02", "label": "Mock lamp"},
            ]
        else:
            from HueBLE import discover_lights  # type: ignore

            devices = await discover_lights(timeout=timeout)
            found = [
                {"address": d.address, "label": d.name or d.address}
                for d in devices
            ]
        self.discovered = found
        self.mark_dirty()
        return found

    async def pair(self, address: str, label: str | None) -> Bulb:
        address = str(address).upper()
        bulb = next(
            (b for b in self.bulbs.values() if b.address == address), None
        )
        if bulb is None:
            taken = set(self.bulbs)
            name = label or f"Hue {address[-5:]}"
            bulb_id = core.slugify_name(name, taken)
            bulb = Bulb(self, {"id": bulb_id, "address": address, "name": name})
            self.bulbs[bulb_id] = bulb
        await bulb.connect()  # triggers pairing; raises on failure
        if bulb.state.get("label"):
            bulb.label = bulb.state["label"]
            bulb.state["label"] = bulb.label
        # Persist on the existing-bulb path too: a previous failed attempt may
        # have left this entry in memory without ever reaching disk.
        self.persist_config()
        self.mark_dirty()
        return bulb

    async def remove(self, bulb_id: str) -> None:
        bulb = self._find(bulb_id)
        await bulb.disconnect()
        del self.bulbs[bulb.id]
        self.persist_config()
        self.mark_dirty()

    # -- request handling
    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        reply: dict
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
        except asyncio.TimeoutError:
            line = b""
        try:
            request = json.loads(line.decode("utf-8")) if line.strip() else {}
            reply = await self.dispatch(request)
        except KeyError as exc:
            reply = {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - surface every failure to the CLI
            LOG.exception("request failed")
            reply = {"ok": False, "error": explain(exc)}
        try:
            writer.write((json.dumps(reply) + "\n").encode("utf-8"))
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def dispatch(self, request: dict) -> dict:
        cmd = request.get("cmd") or "ping"
        if cmd == "ping":
            return {"ok": True, "pid": os.getpid()}
        if cmd == "state":
            return {"ok": True, "state": self.snapshot()}
        if cmd == "reload":
            self.reload_config()
            self.mark_dirty()
            return {"ok": True, "state": self.snapshot()}
        if cmd == "set":
            bulb = self._find(request.get("id", ""))
            fields = request.get("fields") or {}
            await bulb.apply(fields)
            return {"ok": True, "bulb": bulb.state}
        if cmd == "refresh":
            bulb = self._find(request.get("id", ""))
            await bulb.refresh()
            return {"ok": True, "bulb": bulb.state}
        if cmd == "scan":
            found = await self.scan(int(request.get("timeout", 8)))
            return {"ok": True, "discovered": found}
        if cmd == "pair":
            address = request.get("address")
            if not address:
                return {"ok": False, "error": "address required"}
            bulb = await self.pair(address, request.get("label"))
            return {"ok": True, "bulb": bulb.state}
        if cmd == "probe-effects":
            bulb = self._find(request.get("id", ""))
            results = await bulb.probe_effects()
            return {"ok": True, "results": results, "bulb": bulb.state}
        if cmd == "set-effects":
            bulb = self._find(request.get("id", ""))
            requested = request.get("effects") or []
            bulb.effects_allow = (
                [str(e).upper() for e in requested if str(e).strip()] or None
            )
            bulb.state["capabilities"]["effectList"] = bulb.effects_allow
            self.persist_config()
            self.mark_dirty()
            return {"ok": True, "bulb": bulb.state}
        if cmd == "remove":
            await self.remove(request.get("id", ""))
            return {"ok": True}
        if cmd == "stop":
            self._shutdown.set()
            return {"ok": True}
        return {"ok": False, "error": f"unknown command {cmd!r}"}


# ----------------------------------------------------------------------- main
def setup_logging() -> None:
    handler = logging.handlers.RotatingFileHandler(
        core.DAEMON_LOG, maxBytes=512_000, backupCount=2
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if os.environ.get("OMARCHY_HUE_DEBUG"):
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(handler.formatter)
        root.addHandler(stream)
        root.setLevel(logging.DEBUG)


def main() -> int:
    setup_logging()
    lock = core.acquire_daemon_lock()
    if lock is None:
        LOG.warning("another daemon already owns the lock; exiting")
        return 0
    LOG.info("starting daemon (backend=%s)", "mock" if core.use_mock() else "hueble")
    daemon = Daemon()
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        pass
    finally:
        try:
            lock.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
