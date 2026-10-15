"""Shared core for the Omarchy Hue BLE plugin.

Holds paths, config/state persistence, the line-delimited JSON protocol used
between the daemon and the CLI, and a mock light backend so the whole
pipeline can be exercised without a bonded bulb.

Only the standard library is imported here; HueBLE is imported lazily by the
daemon so the CLI stays fast and works even before the venv is complete.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

APP = "omarchy-hue"


# --------------------------------------------------------------------- paths
def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/{APP}-{os.getuid()}"
    d = Path(base) / APP
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def state_dir() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    d = base / APP
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    override = os.environ.get("OMARCHY_HUE_CONFIG")
    if override:
        return Path(override)
    base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "omarchy" / "hue.json"


SOCKET_PATH = runtime_dir() / "sock"
STATE_PATH = runtime_dir() / "state.json"
DAEMON_LOG = state_dir() / "daemon.log"

DEFAULT_CONFIG: dict = {
    "version": 1,
    "idleDisconnectSec": 300,
    "pollIntervalSec": 30,
    "bulbs": [],
}


def use_mock() -> bool:
    return os.environ.get("OMARCHY_HUE_BACKEND", "").lower() == "mock"


# ------------------------------------------------------------------- storage
def atomic_write_json(path: Path, obj) -> None:
    """Atomic replace. Safe for the config (nobody watches its inode)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    tmp.write_text(json.dumps(obj, indent=2) + "\n")
    os.replace(tmp, path)


def write_state(obj) -> None:
    """Write the state file *in place* so the shell's file watcher keeps its
    inode. Readers tolerate a torn read by retrying."""
    data = json.dumps(obj, separators=(",", ":"))
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def load_config() -> dict:
    path = config_path()
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if not path.exists():
        return cfg
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return cfg
    if not isinstance(raw, dict):
        return cfg
    cfg["version"] = 1
    for key in ("idleDisconnectSec", "pollIntervalSec"):
        value = raw.get(key)
        if isinstance(value, (int, float)) and value >= 0:
            cfg[key] = int(value)
    bulbs = raw.get("bulbs")
    if isinstance(bulbs, list):
        cfg["bulbs"] = [b for b in bulbs if isinstance(b, dict) and b.get("address")]
    return cfg


def save_config(cfg: dict) -> None:
    atomic_write_json(config_path(), cfg)


def slugify_name(name: str, taken: set[str] | None = None) -> str:
    taken = taken or set()
    base = "".join(c.lower() if c.isalnum() else "-" for c in str(name or "light"))
    base = "-".join(part for part in base.split("-") if part) or "light"
    candidate = base
    i = 2
    while candidate in taken:
        candidate = f"{base}-{i}"
        i += 1
    return candidate


# --------------------------------------------------------------------- color
def hex_to_xy(value: str) -> tuple[float, float]:
    """Approximate Hue XY from a #RRGGBB string (sRGB, gamma 2.4, D65)."""
    s = str(value).strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        raise ValueError(f"expected a hex colour, got {value!r}")
    r, g, b = (int(s[i : i + 2], 16) / 255.0 for i in (0, 2, 4))

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = lin(r), lin(g), lin(b)
    x = r * 0.664511 + g * 0.154324 + b * 0.162028
    y = r * 0.283881 + g * 0.668433 + b * 0.047685
    z = r * 0.000088 + g * 0.072310 + b * 0.986039
    total = x + y + z
    if total <= 0:
        return (0.3127, 0.3290)
    return (round(x / total, 4), round(y / total, 4))


def kelvin_to_mireds(kelvin: float) -> int:
    kelvin = max(1000.0, min(10000.0, float(kelvin)))
    return int(round(1_000_000 / kelvin))


def mireds_to_kelvin(mireds: float) -> int:
    mireds = max(1.0, float(mireds))
    return int(round(1_000_000 / mireds))


# ------------------------------------------------------------------- protocol
def connect_socket(timeout: float = 2.0) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(str(SOCKET_PATH))
    return sock


def send_request(payload: dict, timeout: float = 60.0) -> dict:
    sock = connect_socket()
    try:
        sock.settimeout(timeout)
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        sock.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while True:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                raise TimeoutError("daemon timed out")
            if not chunk:
                break
            chunks.append(chunk)
        body = b"".join(chunks).decode("utf-8").strip()
        if not body:
            return {"ok": False, "error": "empty response from daemon"}
        return json.loads(body.splitlines()[-1])
    finally:
        sock.close()


def ping(timeout: float = 1.0) -> bool:
    try:
        reply = send_request({"cmd": "ping"}, timeout=timeout)
        return bool(reply.get("ok"))
    except (OSError, ValueError, TimeoutError):
        return False


def spawn_daemon() -> None:
    import subprocess

    script = Path(__file__).resolve().with_name("hued.py")
    log = open(DAEMON_LOG, "ab", buffering=0)
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    subprocess.Popen(
        [sys.executable, str(script)],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,
        env=env,
        cwd=str(script.parent),
    )


def acquire_daemon_lock():
    """Take the lifetime lock for the single daemon instance.

    Returns the open lock handle on success, or None when another daemon is
    already running. The handle must be kept alive for the process lifetime.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-Linux
        return True
    handle = open(runtime_dir() / "daemon.lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def _wait_for_socket(wait: float) -> bool:
    deadline = time.time() + wait
    while time.time() < deadline:
        if ping():
            return True
        time.sleep(0.15)
    return False


def ensure_daemon(wait: float = 10.0) -> bool:
    """Start the daemon if needed. Safe to call from many shell instances at
    once: a lock file serialises the spawn so only one daemon ever races to
    bind the socket."""
    if ping():
        return True
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-Linux
        fcntl = None

    lock_file = open(runtime_dir() / "spawn.lock", "w")
    try:
        if fcntl is not None:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
        if ping():
            return True
        # Drop a stale socket left behind by a crash so bind() can succeed.
        try:
            if SOCKET_PATH.exists():
                SOCKET_PATH.unlink(missing_ok=True)
        except OSError:
            pass
        spawn_daemon()
        return _wait_for_socket(wait)
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
        except OSError:
            pass
        lock_file.close()


# ---------------------------------------------------------------- mock light
class MockLight:
    """A stand-in for HueBLE.HueBleLight used with OMARCHY_HUE_BACKEND=mock."""

    def __init__(self, address: str, name: str = "Mock Hue") -> None:
        self._address = address
        self._name = name
        self._connected = False
        self._callbacks: list = []
        self._power_on = False
        self._brightness = 128
        self._colour_temp = 366
        self._colour_xy = (0.45, 0.41)
        self._effect = None
        self._effect_speed = 0
        self._mode = "temp"
        self._manufacturer = "Signify Netherlands B.V. (mock)"
        self._model = "LCA006"
        self._firmware = "1.104.2"

    # -- plumbing
    def add_callback_on_state_changed(self, function) -> None:
        self._callbacks.append(function)

    def remove_callback(self, function) -> None:
        if function in self._callbacks:
            self._callbacks.remove(function)

    def _notify(self) -> None:
        for cb in list(self._callbacks):
            try:
                cb()
            except Exception:
                pass

    async def connect(self, **kwargs) -> None:
        self._connected = True
        self._notify()

    async def disconnect(self) -> None:
        self._connected = False

    async def poll_state(self, **kwargs) -> None:
        self._connected = True

    # -- properties
    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def authenticated(self) -> bool:
        return True

    @property
    def available(self) -> bool:
        return True

    @property
    def address(self) -> str:
        return self._address

    @property
    def name(self) -> str:
        return self._name

    @property
    def manufacturer(self) -> str:
        return self._manufacturer

    @property
    def model(self) -> str:
        return self._model

    @property
    def firmware(self) -> str:
        return self._firmware

    @property
    def power_state(self) -> bool:
        return self._power_on

    @property
    def brightness(self) -> int:
        return self._brightness

    @property
    def colour_temp(self) -> int:
        return self._colour_temp

    @property
    def minimum_mireds(self) -> int:
        return 153

    @property
    def maximum_mireds(self) -> int:
        return 500

    @property
    def colour_xy(self):
        return self._colour_xy

    @property
    def colour_temp_mode(self) -> bool:
        return self._mode == "temp"

    @property
    def effect(self):
        return self._effect

    @property
    def supports_effects(self) -> bool:
        return True

    # -- setters
    async def set_power(self, on: bool) -> None:
        self._power_on = bool(on)
        self._notify()

    async def set_brightness(self, brightness: int) -> None:
        self._brightness = max(1, min(254, int(brightness)))
        self._notify()

    async def set_colour_temp(self, colour_temp: int) -> None:
        self._colour_temp = max(153, min(500, int(colour_temp)))
        self._effect = None
        self._mode = "temp"
        self._notify()

    async def set_colour_xy(self, x: float, y: float) -> None:
        self._colour_xy = (float(x), float(y))
        self._effect = None
        self._mode = "xy"
        self._notify()

    async def set_colour_effect(self, x, y, brightness, effect, effect_speed) -> None:
        self._colour_xy = (float(x), float(y))
        self._brightness = int(brightness)
        self._effect = effect
        self._effect_speed = int(effect_speed)
        self._notify()

    async def set_temperature_effect(
        self, colour_temp, brightness, effect, effect_speed
    ) -> None:
        self._colour_temp = max(153, min(500, int(colour_temp)))
        self._brightness = int(brightness)
        self._effect = effect
        self._effect_speed = int(effect_speed)
        self._notify()

    async def set_light_name(self, name: str) -> None:
        self._name = str(name)
