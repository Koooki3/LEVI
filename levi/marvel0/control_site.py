"""The marvel0 control file: the switches and machine values behind the page's
buttons that start and stop the arm stack (``stack_act.py``) and, later, the
demo client (``run.py``).

An optional TOML file found through ``LEVI_MARVEL0_CONTROL`` or, without it, at
``lab/marvel0/control.toml`` of the main checkout (also when LEVI runs from a
worktree, as the site file is). **A missing file is a normal state, not an
error**: every write route of the stack and the client answers 403
``control_disabled`` with a plain sentence and the views report ``enabled:
false``, while every other route of the page works as it did. The switches
default to off, and the person who owns the rig turns them on, in the product
checkout, with the owner present.

Closed schema: an unknown key is an error, and every error is listed as
``{"field", "code"}`` (never the value). The machine values (the franka_control
checkout, the robot's address, the gripper's serial device, the reset pose) are
required only when ``[stack] enabled`` is true: no machine value is written in
this repository, so a file that switches the stack on without them is invalid.
The file must be a regular file of the current user that no one else can
write, and it is read through ``probes.read_regular`` and parsed again only
when its identity, modification time or size changes.

What the switches are worth: they stop a page that was never meant to act (a
worktree instance, a fresh checkout, a second LEVI). They do not stop another
program of the same account, which can edit this file; the real barriers are
Franka Desk and the physical emergency stop.
"""

import ipaddress
import math
import os
import re
import stat
import threading
import tomllib
from dataclasses import dataclass
from pathlib import Path

from levi.setup import probes

from . import site

ENV = "LEVI_MARVEL0_CONTROL"
SCHEMA = "levi.marvel0.control.v1"
RELATIVE = ("lab", "marvel0", "control.toml")
MAX_BYTES = 16 * 1024
DEFAULT_SERVER_PORT = 5000
# The least time the marker must have existed before A may start. It used to be
# a fixed wait of 45-60 s, the time a heavy job of ``heavy_gate.py`` needs to
# yield (grace 30 s + kill wait 10 s). The page now waits for the jobs
# themselves (``stack_act.heavy_jobs_running``), so what is left here is only
# the time a gate needs to *see* the marker (``heavy_gate.MARKER_MIN_AGE`` is
# 2 s). When the page cannot tell whether a heavy job runs, it falls back to
# ``LEGACY_MARKER_WAIT_S``, the fixed wait of old.
DEFAULT_MARKER_WAIT_S = 3.0
MARKER_WAIT_FLOOR_S = 2.0
MARKER_WAIT_CEIL_S = 600.0
LEGACY_MARKER_WAIT_S = 45.0
JOINTS = 7
JOINT_LIMIT = 3.2  # radians; no FR3 joint goes further
# The ports nothing of this page may be configured to bind or serve on: the
# policy server, the learner and the tunnel (root AGENTS.md); 5001 is the
# second robot server.
FORBIDDEN_PORTS = (5001, 5100, 7470, 8000)

# What systemd would expand or split on, in a path that reaches a unit.
_UNSAFE = re.compile(r"[$%\s\x00-\x1f\x7f]")

TEMPLATE = """\
# LEVI marvel0 control file. Switches and machine values only; the page can read
# it and can never write it. Missing file or enabled = false: the page shows the
# arm stack and changes nothing. Point LEVI_MARVEL0_CONTROL at it, or save it as
# lab/marvel0/control.toml in the main LEVI checkout. Keep it writable by you only.
schema = "levi.marvel0.control.v1"

[stack]
enabled            = false                      # true: the page may start and stop A, B, C and run the gripper test
franka_control     = "/path/to/franka_control"   # the checkout with env/fr3_env.sh, scripts/, robot_server/, tools/
robot_ip           = "10.42.0.157"               # the arm's address (Desk and FCI)
com_port           = "/dev/serial/by-id/usb-FTDI_USB_TO_RS-485_XXXXXXXX-if00-port0"
server_port        = 5000                        # the robot server's loopback port
reset_joint_target = [-0.8063, -0.0623, 0.9130, -2.4036, 0.0580, 2.3011, 1.5844]
marker_wait_s      = 3                           # 2 to 600: the least age of the marker before A starts (the page also waits for heavy jobs to end)

[client]
enabled            = false                      # true: the page may host the evaluation client

[aeri]
enabled            = false                      # true: the page may prepare and host an AERI run (the site file's [aeri] names the code)
"""


class ControlSiteError(Exception):
    """The control file exists but is not valid; ``errors`` lists every
    problem as ``{"field", "code"}``."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__("; ".join(f"{e['field']}: {e['code']}" for e in errors))


@dataclass(frozen=True)
class StackControl:
    enabled: bool = False
    franka_control: str | None = None
    robot_ip: str | None = None
    com_port: str | None = None
    server_port: int = DEFAULT_SERVER_PORT
    reset_joint_target: tuple = ()
    marker_wait_s: float = DEFAULT_MARKER_WAIT_S

    @property
    def health_file(self) -> str | None:
        """Where the monitor writes (the checkout's ``run/``); the page reads
        it through the setup probes' context and refuses to act when the two
        differ (``gate.control_health_file``)."""
        if not self.franka_control:
            return None
        return str(Path(self.franka_control) / "run" / "fr3_health.json")


@dataclass(frozen=True)
class ClientControl:
    enabled: bool = False


@dataclass(frozen=True)
class AeriControl:
    enabled: bool = False


@dataclass(frozen=True)
class ControlSite:
    stack: StackControl
    client: ClientControl
    origin: str = "main_checkout"
    # The AERI switch. A malformed ``[aeri]`` section leaves it off and lists
    # ``(field, code)`` in ``aeri_errors``; it never switches the stack or the
    # client off, and nothing here can switch it on.
    aeri: AeriControl = AeriControl()
    aeri_errors: tuple = ()


@dataclass(frozen=True)
class ControlLoad:
    """What a read of the file found: ``state`` is ``ok``, ``missing`` or
    ``invalid`` (``errors`` says why)."""

    state: str
    site: ControlSite | None
    errors: tuple = ()


class _Bad(Exception):
    def __init__(self, code):
        self.code = code


def _flag(value):
    if not isinstance(value, bool):
        raise _Bad("type")
    return value


def _path(value):
    if not isinstance(value, str) or not value:
        raise _Bad("type")
    if _UNSAFE.search(value):
        raise _Bad("unsafe")
    if not os.path.isabs(value) or os.path.normpath(value) != value:
        raise _Bad("not_absolute")
    return value


def _device(value):
    value = _path(value)
    if not value.startswith("/dev/"):
        raise _Bad("range")
    return value


def _address(value):
    if not isinstance(value, str):
        raise _Bad("type")
    try:
        ip = ipaddress.IPv4Address(value)
    except ValueError as exc:
        raise _Bad("range") from exc
    if ip.is_loopback or ip.is_unspecified or ip.is_multicast:
        raise _Bad("range")
    return value


def _port(value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise _Bad("type")
    if not 1024 <= value <= 65535 or value in FORBIDDEN_PORTS:
        raise _Bad("range")
    return value


def _pose(value):
    if not isinstance(value, list) or len(value) != JOINTS:
        raise _Bad("type")
    out = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise _Bad("type")
        if not math.isfinite(item) or abs(item) > JOINT_LIMIT:
            raise _Bad("range")
        out.append(float(item))
    return tuple(out)


def _seconds(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _Bad("type")
    if not MARKER_WAIT_FLOOR_S <= value <= MARKER_WAIT_CEIL_S:
        raise _Bad("range")
    return float(value)


STACK_CHECKS = {
    "enabled": _flag,
    "franka_control": _path,
    "robot_ip": _address,
    "com_port": _device,
    "server_port": _port,
    "reset_joint_target": _pose,
    "marker_wait_s": _seconds,
}
# The machine values a switched-on stack cannot do without.
STACK_NEEDED = ("franka_control", "robot_ip", "com_port", "reset_joint_target")
CLIENT_CHECKS = {"enabled": _flag}
AERI_CHECKS = {"enabled": _flag}


def _aeri_section(data):
    """``(AeriControl, errors)`` of ``[aeri]``. Absent means off; anything
    that is not exactly ``enabled = <bool>`` means off with the errors listed."""
    if "aeri" not in data:
        return AeriControl(), ()
    table = data["aeri"]
    if not isinstance(table, dict):
        return AeriControl(), (("aeri", "type"),)
    errors = []
    out = {}
    for key, value in table.items():
        check = AERI_CHECKS.get(key)
        if check is None:
            errors.append((f"aeri.{key}", "unknown_key"))
            continue
        try:
            out[key] = check(value)
        except _Bad as exc:
            errors.append((f"aeri.{key}", exc.code))
    if errors:
        return AeriControl(), tuple(errors)
    return AeriControl(**out), ()


def _section(data, name, checks, errors):
    """The checked keys of ``[name]`` (an absent section is empty)."""
    table = data.get(name)
    if name not in data:
        return {}
    if not isinstance(table, dict):
        errors.append({"field": name, "code": "type"})
        return {}
    out = {}
    for key, value in table.items():
        check = checks.get(key)
        if check is None:
            errors.append({"field": f"{name}.{key}", "code": "unknown_key"})
            continue
        try:
            out[key] = check(value)
        except _Bad as exc:
            errors.append({"field": f"{name}.{key}", "code": exc.code})
    return out


def parse(data, origin="main_checkout") -> ControlSite:
    """The ``ControlSite`` of the file's data; raises ``ControlSiteError``
    listing every problem."""
    errors: list = []
    if not isinstance(data, dict):
        raise ControlSiteError([{"field": "file", "code": "type"}])
    if "schema" not in data:
        errors.append({"field": "schema", "code": "missing"})
    elif data["schema"] != SCHEMA:
        errors.append({"field": "schema", "code": "range"})
    for key in data:
        if key not in ("schema", "stack", "client", "aeri"):
            errors.append({"field": key, "code": "unknown_key"})
    stack = _section(data, "stack", STACK_CHECKS, errors)
    client = _section(data, "client", CLIENT_CHECKS, errors)
    if stack.get("enabled"):
        errors.extend(
            {"field": f"stack.{key}", "code": "missing"}
            for key in STACK_NEEDED
            if key not in stack
        )
    if errors:
        raise ControlSiteError(errors)
    aeri, aeri_errors = _aeri_section(data)
    return ControlSite(
        StackControl(**stack),
        ClientControl(**client),
        origin,
        aeri=aeri,
        aeri_errors=aeri_errors,
    )


# --- where it is and how it is read ------------------------------------------------


def find_path(environ=None, checkouts=None):
    """``(path, origin)``: ``LEVI_MARVEL0_CONTROL`` when set (origin ``env``),
    else the file in the main checkout."""
    environ = os.environ if environ is None else environ
    named = (environ.get(ENV) or "").strip()
    if named:
        return site._named(named), "env"
    return site.main_checkout(checkouts).joinpath(*RELATIVE), "main_checkout"


_LOCK = threading.Lock()
_CACHE: dict = {}


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def _invalid(code) -> ControlLoad:
    return ControlLoad("invalid", None, ({"field": "file", "code": code},))


def _read(path: Path, origin, before) -> ControlLoad:
    if not stat.S_ISREG(before.st_mode):
        return _invalid("not_regular")
    if before.st_uid != os.geteuid():
        return _invalid("not_owner")
    if before.st_mode & 0o022:
        return _invalid("writable")
    try:
        data, truncated, _ = probes.read_regular(path, MAX_BYTES)
    except probes.NotRegular:
        return _invalid("not_regular")
    except OSError:
        return _invalid("unreadable")
    if truncated:
        return _invalid("too_large")
    try:
        table = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, RecursionError, ValueError):
        return _invalid("not_toml")
    try:
        return ControlLoad("ok", parse(table, origin))
    except ControlSiteError as exc:
        return ControlLoad("invalid", None, tuple(exc.errors))


def read(environ=None, checkouts=None) -> ControlLoad:
    """Look for the control file and read it. Never raises. The file is parsed
    again only when its identity, modification time or size changed."""
    path, origin = find_path(environ, checkouts)
    if not path.is_absolute():
        return ControlLoad("invalid", None, ({"field": ENV, "code": "not_absolute"},))
    try:
        before = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return ControlLoad("missing", None)
    except (OSError, ValueError):
        return _invalid("unreadable")
    key = (
        str(path),
        origin,
        before.st_dev,
        before.st_ino,
        before.st_mtime_ns,
        before.st_ctime_ns,
        before.st_size,
        before.st_mode,
        before.st_uid,
    )
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
    loaded = _read(path, origin, before)
    with _LOCK:
        _CACHE.clear()  # one file is in use at a time
        _CACHE[key] = loaded
    return loaded


def load(environ=None, checkouts=None) -> ControlSite | None:
    """The control file's values; None when there is no file; raises
    ``ControlSiteError`` when it is there and not valid."""
    loaded = read(environ, checkouts)
    if loaded.state == "missing":
        return None
    if loaded.state == "invalid":
        raise ControlSiteError(list(loaded.errors))
    return loaded.site


def view(loaded: ControlLoad) -> dict:
    """What the page may see of the file: whether each switch is on and why
    not. Holds no path and no value of the file."""
    if loaded.state == "ok":
        stack_on = loaded.site.stack.enabled
        client_on = loaded.site.client.enabled
        code = "control.enabled" if stack_on else "control.disabled"
    elif loaded.state == "missing":
        stack_on = client_on = False
        code = "control.missing"
    else:
        stack_on = client_on = False
        code = "control.invalid"
    return {
        "state": loaded.state,
        "code": code,
        "stack_enabled": stack_on,
        "client_enabled": client_on,
        "errors": [dict(e) for e in loaded.errors],
        "env": ENV,
    }


def aeri_view(loaded: ControlLoad) -> dict:
    """What the AERI card may see of the file: ``{"state", "code", "enabled",
    "errors"}``. ``enabled`` is true only for a valid file whose ``[aeri]``
    section is valid and says ``enabled = true``; a missing or invalid file, or a
    malformed section, is off. No path, no value."""
    if loaded.state == "missing":
        return {
            "state": "missing",
            "code": "aeri.missing",
            "enabled": False,
            "errors": [],
        }
    if loaded.state != "ok" or loaded.site is None:
        return {
            "state": "invalid",
            "code": "aeri.invalid",
            "enabled": False,
            "errors": [dict(e) for e in loaded.errors],
        }
    errors = [{"field": f, "code": c} for f, c in loaded.site.aeri_errors]
    if errors:
        return {
            "state": "invalid",
            "code": "aeri.invalid",
            "enabled": False,
            "errors": errors,
        }
    on = loaded.site.aeri.enabled is True
    return {
        "state": "ok",
        "code": "aeri.enabled" if on else "aeri.disabled",
        "enabled": on,
        "errors": [],
    }
