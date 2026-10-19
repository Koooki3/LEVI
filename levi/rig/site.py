"""The rig site file: the machine values behind the rig console's policy
deploy and client command, read-only for every page and route.

A TOML file found through ``LEVI_RIG_SITE`` or, without it, at
``lab/rig/site.toml`` of the main checkout (also when LEVI runs from a
worktree). It holds the home pose, both camera serials, the lowest ``--min-z``,
the caps of the form, the rollout root, the openpi checkout and its python, the
checkpoints folder, the GPU fractions, the policy port and the CPU settings of
the policy server unit. No machine value is written in this repository: without
the file every route that needs it answers 412 ``site_missing`` with the path it
looked at and ``TEMPLATE`` to start from.

The schema is closed (an unknown key is an error, a missing one too, except
``server.port``), every error is listed as ``{"field", "code"}`` (never the
value), and nothing on the file system is checked here; whether a path exists
is a deploy-time check. The file must be a regular file of the current user
that no one else can write; it is read through ``probes.read_regular`` (``lstat``
first, ``O_NOFOLLOW``, a size cap) and parsed again only when its identity,
modification time or size changes.

The ``Site`` value keeps the keys of ``[paths]`` as its own fields
(``site.python``, ``site.rollout_root``) and every other section as a nested
value (``site.safety.max_xy``, ``site.unit.nice``); ``site.origin`` says where
the file came from (``env`` or ``main_checkout``), ``site.source`` is the
``[source]`` section.
"""

import contextlib
import math
import os
import posixpath
import re
import stat
import threading
import tomllib
import unicodedata
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from levi.setup import probes

from .errors import SITE_INVALID, SITE_MISSING, fail

ENV = "LEVI_RIG_SITE"
SCHEMA = "levi.rig.site.v1"
VIEW_SCHEMA = "levi.rig.site_view.v1"
RELATIVE = ("lab", "rig", "site.toml")
MAX_BYTES = 64 * 1024
MAX_PROMPT_CHARS = 300  # a task instruction is never longer, whatever the file says
# The lock an AERI run holds while it uses the arm (levi/automatic/fr3_run.py
# ARM_LOCK_FILE; a test compares the two). Not a site value: one wrong letter in
# a file would blind the deploy's "the arm is in use" check without any error.
ARM_LOCK_FILE = "/tmp/levi-aeri-fr3-arm.lock"
# The only task instructions a model of the restricted family was trained on
# (TASKS of examples/fr3_local/run_robotiq_client.py in the openpi checkout, which
# lies outside this repository; copied, and named in the setup docs). The client
# refuses every other one before it moves, so the page does not offer them.
RESTRICTED_PROMPTS = (
    "pick screws of same size into the empty slot of screw box",
    "stack the plates of same color together",
)
# The ports of the robot server, the gripper side and the learner, and LEVI's own
# (root AGENTS.md): the policy client is never pointed at them from this page.
ROBOT_SIDE_PORTS = (5000, 5001, 5100, 7470)

# What systemd would expand or split, what a shell would treat specially at the
# start of a word, and control characters (the class of
# ``fr3_policy_swap.UNSAFE_PATH``): none of it may be in a path or unit value.
_UNSAFE = re.compile(r"[$%\s\x00-\x1f\x7f]")
# Characters a task instruction or a note never holds (the command is text to
# copy into a shell; quoting is the second line of defence, this the first).
FORBIDDEN_TEXT = frozenset("'\"\\$`!")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
_UNIT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,80}\.service")
_HOST = re.compile(r"[A-Za-z0-9:][A-Za-z0-9.:-]{0,252}")
_URL = re.compile(r"https?://[A-Za-z0-9.:\[\]-]+(?:/[A-Za-z0-9._~/-]*)?")
_SERIAL = re.compile(r"[0-9]{6,16}")
_CPUS = re.compile(r"[0-9]{1,4}(?:-[0-9]{1,4})?(?:,[0-9]{1,4}(?:-[0-9]{1,4})?)*")
_RELATIVE = re.compile(r"[A-Za-z0-9._/-]+")

TEMPLATE = """\
# LEVI rig site file. Machine values only; the page can read it and can never write it.
# Point LEVI_RIG_SITE at it, or save it as lab/rig/site.toml in the main LEVI checkout.
schema = "levi.rig.site.v1"

[paths]
openpi_checkout  = "/path/to/openpi"
python           = "/path/to/openpi/.venv/bin/python"   # run directly, not through 'uv run'
checkpoints_root = "/path/to/openpi/checkpoints"
rollout_root     = "/path/to/rollouts"                  # where the demo client writes episodes
gpu_lock_file    = "/path/to/levi-hub/.gpu.lock"        # the physical GPU lock file

[live]
unit = "levi-live.service"          # the owner of the GPU lock while its vision model server exists

[ros]
path_prefixes = ["/path/to/ros", "/path/to/franka_control"]   # LD_LIBRARY_PATH entries under these are removed from the policy unit

[server]
port = 8000
remote_host = "127.0.0.1"
robot_server_url = "http://127.0.0.1:5000/"

[cameras]
view  = "262622274256"   # side D405
wrist = "262622275490"   # wrist D405

[home]
pose = [0.481902, -0.011145, 0.235951, 3.119139, 0.008288, -0.883962]
duration_s = 3.0               # 1 to 30 seconds

[safety]
min_z_floor = 0.025            # the form can only raise it
min_z_default = 0.025          # the rig owner decides: the client's own floors are 0.075 (screws) and 0.05325 (plates), so this default warns
max_xy = 0.85
max_step_m = 0.10
max_step_rad = 0.35
rx_range = [1.50, 4.03]
ry_range = [-0.74, 0.96]
rz_range = [-2.44, 0.77]
binarize_gripper = true
require_matching_server = true
allow_unknown_prompt = false

[caps]
max_steps_default = 500
max_steps_cap = 3000
eval_num_default = 5
eval_num_cap = 50
rtc_horizon_default = 6
rtc_horizon_max = 8            # the preview warns above 7 (execute horizon + 3 beats of latency must stay <= 10)
open_loop_default = 6
open_loop_max = 10
prompt_max_chars = 300
note_max_chars = 200
cfg_scale_default = 1.0
cfg_scale_max = 1.5

[gpu]
fraction_presets = [0.22, 0.30, 0.45]   # 0.05 to 0.45, ascending
margin_mib = 1024              # at least 1024
lock_hold_max_s = 600

[unit]
nice = 10
cpu_affinity = "8-15"          # a taskset list; choose cores the arm controller does not use (the rig owner confirms them before the first deploy)
cpu_quota_percent = 0          # 0 = no CPUQuota property
stop_timeout_s = 90
start_timeout_s = 600

[catalog]
known_configs = ["pi05_fr3_all_state", "pi05_fr3_all_state_cfg"]
cfg_configs = ["pi05_fr3_all_state_cfg"]
restricted_prefixes = ["pi05_fr3_sp"]
verified = ["pi05_fr3_all_step49999"]    # checkpoints with a recorded real-GPU serving check
excluded = ["reset_r1_step20000"]

[prompts]
default = "pick the eggplant in the blue plate"
known = [
  "pick screws of same size into the empty slot of screw box",
  "stack the plates of same color together",
  "pick the eggplant in the blue plate",
  "stack red plate on white plate then place eggplant on blue plate",
  "place tomato on red plate then eggplant on blue plate",
]

[source]
hash_files = [
  "scripts/serve_policy_rtc.py",
  "scripts/serve_policy.py",
  "src/openpi/policies/rtc_policy.py",
  "src/openpi/policies/policy_config.py",
  "src/openpi/training/config.py",
]
manifest = ""     # '' = none: the hashes are only recorded in the deploy record
"""


# --- the value ----------------------------------------------------------------------


@dataclass(frozen=True)
class Live:
    unit: str


@dataclass(frozen=True)
class Ros:
    path_prefixes: tuple


@dataclass(frozen=True)
class Server:
    port: int
    remote_host: str
    robot_server_url: str


@dataclass(frozen=True)
class Cameras:
    view: str
    wrist: str


@dataclass(frozen=True)
class Home:
    pose: tuple
    duration_s: float


@dataclass(frozen=True)
class Safety:
    min_z_floor: float
    min_z_default: float
    max_xy: float
    max_step_m: float
    max_step_rad: float
    rx_range: tuple
    ry_range: tuple
    rz_range: tuple
    binarize_gripper: bool
    require_matching_server: bool
    allow_unknown_prompt: bool


@dataclass(frozen=True)
class Caps:
    max_steps_default: int
    max_steps_cap: int
    eval_num_default: int
    eval_num_cap: int
    rtc_horizon_default: int
    rtc_horizon_max: int
    open_loop_default: int
    open_loop_max: int
    prompt_max_chars: int
    note_max_chars: int
    cfg_scale_default: float
    cfg_scale_max: float


@dataclass(frozen=True)
class Gpu:
    fraction_presets: tuple
    margin_mib: int
    lock_hold_max_s: float


@dataclass(frozen=True)
class Unit:
    nice: int
    cpu_affinity: str
    cpu_quota_percent: int
    stop_timeout_s: int
    start_timeout_s: int


@dataclass(frozen=True)
class Catalog:
    known_configs: tuple
    cfg_configs: tuple
    restricted_prefixes: tuple
    verified: tuple
    excluded: tuple


@dataclass(frozen=True)
class Prompts:
    default: str
    known: tuple


@dataclass(frozen=True)
class Source:
    hash_files: tuple
    manifest: str


@dataclass(frozen=True)
class Site:
    origin: str  # "env" | "main_checkout": where the file was found
    openpi_checkout: str
    python: str
    checkpoints_root: str
    rollout_root: str
    gpu_lock_file: str
    live: Live
    ros: Ros
    server: Server
    cameras: Cameras
    home: Home
    safety: Safety
    caps: Caps
    gpu: Gpu
    unit: Unit
    catalog: Catalog
    prompts: Prompts
    source: Source

    @property
    def port(self) -> int:
        """The policy server's port, the one the client connects to."""
        return self.server.port

    @property
    def ros_prefixes(self) -> tuple:
        return self.ros.path_prefixes

    @property
    def arm_lock_file(self) -> str:
        """The AERI runner's arm lock (the same for every site)."""
        return ARM_LOCK_FILE


class SiteInvalid(ValueError):
    """The site data breaks the schema; ``errors`` lists ``{"field", "code"}``."""

    def __init__(self, errors):
        super().__init__(f"{len(errors)} problem(s) in the rig site")
        self.errors = errors


# --- checks: each returns the value to keep or raises _Bad(code) -------------------------


class _Bad(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def unsafe_text(text) -> bool:
    """True when ``text`` holds `$`, `%`, whitespace or a control character:
    nothing that reaches systemd, a path or an argv may."""
    return bool(_UNSAFE.search(text))


def _path(value):
    if not isinstance(value, str):
        raise _Bad("type")
    if not value.startswith("/"):
        raise _Bad("not_absolute")
    if unsafe_text(value):
        raise _Bad("unsafe_chars")
    if value.startswith("//") or posixpath.normpath(value) != value:
        raise _Bad("not_normalized")
    return value


def _relative(empty_ok=False):
    def check(value):
        if not isinstance(value, str):
            raise _Bad("type")
        if empty_ok and value == "":
            return value
        if (
            not _RELATIVE.fullmatch(value)
            or value.startswith("/")
            or ".." in value.split("/")
        ):
            raise _Bad("unsafe_chars")
        return value

    return check


def _integer(lo, hi):
    def check(value):
        if isinstance(value, bool) or not isinstance(value, int):
            raise _Bad("type")
        if not lo <= value <= hi:
            raise _Bad("range")
        return value

    return check


def _number(lo, hi, above=False):
    """A finite number in ``[lo, hi]`` (``(lo, hi]`` with ``above``), kept as a float."""

    def check(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _Bad("type")
        try:
            number = float(value)
        except OverflowError:  # an integer too big for a float
            raise _Bad("range") from None
        if (
            not math.isfinite(number)
            or number > hi
            or not (lo < number if above else lo <= number)
        ):
            raise _Bad("range")
        return number

    return check


def _port(value):
    port = _integer(1024, 65535)(value)
    if port in ROBOT_SIDE_PORTS:
        raise _Bad("range")
    return port


def _boolean(value):
    if not isinstance(value, bool):
        raise _Bad("type")
    return value


def _text(pattern):
    def check(value):
        if not isinstance(value, str):
            raise _Bad("type")
        if not pattern.fullmatch(value):
            raise _Bad("range")
        return value

    return check


def _items(item, lo, hi):
    def check(value):
        if not isinstance(value, list):
            raise _Bad("type")
        if not lo <= len(value) <= hi:
            raise _Bad("range")
        return tuple(item(v) for v in value)

    return check


def _numbers(count, lo, hi):
    """Exactly ``count`` numbers: a wrong count is a ``type`` error."""
    one = _number(lo, hi)

    def check(value):
        if not isinstance(value, list) or len(value) != count:
            raise _Bad("type")
        return tuple(one(v) for v in value)

    return check


def _pair(value):
    low, high = _numbers(2, -7.0, 7.0)(value)
    if not low < high:
        raise _Bad("range")
    return low, high


# The most of the card a policy server may reserve (user 2026-10-14: up to about
# 15 GB at 0.45) and the least the deploy keeps free beyond its fraction.
MAX_FRACTION = 0.45
MIN_MARGIN_MIB = 1024


def _fractions(value):
    kept = _items(_number(0.05, MAX_FRACTION), 1, 4)(value)
    if any(a >= b for a, b in pairwise(kept)):
        raise _Bad("range")
    return kept


def _cpus(value):
    if not isinstance(value, str):
        raise _Bad("type")
    if not _CPUS.fullmatch(value):
        raise _Bad("range")
    for part in value.split(","):
        low, _, high = part.partition("-")
        if high and int(low) > int(high):
            raise _Bad("range")
    return value


def _prompt(value):
    if not isinstance(value, str):
        raise _Bad("type")
    reason = prompt_reason(value, MAX_PROMPT_CHARS)
    if reason in ("empty", "too_long"):
        raise _Bad("range")
    if reason:
        raise _Bad("unsafe_chars")
    return value


def _prompts(value):
    if not isinstance(value, list):
        raise _Bad("type")
    if not 1 <= len(value) <= 32:
        raise _Bad("range")
    return tuple(_prompt(v) for v in value)


def _names(lo, hi):
    return _items(_text(NAME), lo, hi)


# section -> key -> check. A section's keys are the fields of its dataclass.
SCHEMA_CHECKS = {
    "paths": {
        "openpi_checkout": _path,
        "python": _path,
        "checkpoints_root": _path,
        "rollout_root": _path,
        "gpu_lock_file": _path,
    },
    "live": {"unit": _text(_UNIT)},
    "ros": {"path_prefixes": _items(_path, 0, 8)},
    "server": {
        "port": _port,
        "remote_host": _text(_HOST),
        "robot_server_url": _text(_URL),
    },
    "cameras": {"view": _text(_SERIAL), "wrist": _text(_SERIAL)},
    "home": {"pose": _numbers(6, -10.0, 10.0), "duration_s": _number(1.0, 30.0)},
    "safety": {
        "min_z_floor": _number(0.0, 1.0, True),
        "min_z_default": _number(0.0, 1.0, True),
        # Upper bounds only a little above the client's defaults (0.85, 0.10,
        # 0.35), so a mistyped value cannot widen the fixed limits unnoticed.
        "max_xy": _number(0.0, 1.0, True),
        "max_step_m": _number(0.0, 0.2, True),
        "max_step_rad": _number(0.0, 0.5, True),
        "rx_range": _pair,
        "ry_range": _pair,
        "rz_range": _pair,
        "binarize_gripper": _boolean,
        "require_matching_server": _boolean,
        "allow_unknown_prompt": _boolean,
    },
    "caps": {
        "max_steps_default": _integer(1, 100000),
        "max_steps_cap": _integer(1, 100000),
        "eval_num_default": _integer(1, 1000),
        "eval_num_cap": _integer(1, 1000),
        "rtc_horizon_default": _integer(1, 10),
        "rtc_horizon_max": _integer(1, 10),
        "open_loop_default": _integer(1, 10),
        "open_loop_max": _integer(1, 10),
        "prompt_max_chars": _integer(1, MAX_PROMPT_CHARS),
        "note_max_chars": _integer(1, 200),
        "cfg_scale_default": _number(0.0, 10.0, True),
        "cfg_scale_max": _number(0.0, 10.0, True),
    },
    "gpu": {
        "fraction_presets": _fractions,
        "margin_mib": _integer(MIN_MARGIN_MIB, 8192),
        "lock_hold_max_s": _number(10.0, 3600.0),
    },
    "unit": {
        "nice": _integer(0, 19),
        "cpu_affinity": _cpus,
        "cpu_quota_percent": _integer(0, 6400),
        "stop_timeout_s": _integer(10, 600),
        "start_timeout_s": _integer(30, 1800),
    },
    "catalog": {
        "known_configs": _names(1, 16),
        "cfg_configs": _names(0, 16),
        "restricted_prefixes": _names(0, 8),
        "verified": _names(0, 64),
        "excluded": _names(0, 64),
    },
    "prompts": {"default": _prompt, "known": _prompts},
    "source": {"hash_files": _items(_relative(), 0, 32), "manifest": _relative(True)},
}
SCHEMA_KEYS = {section: tuple(keys) for section, keys in SCHEMA_CHECKS.items()}
DEFAULTS = {("server", "port"): 8000}
SECTIONS = {
    "live": Live,
    "ros": Ros,
    "server": Server,
    "cameras": Cameras,
    "home": Home,
    "safety": Safety,
    "caps": Caps,
    "gpu": Gpu,
    "unit": Unit,
    "catalog": Catalog,
    "prompts": Prompts,
    "source": Source,
}


def _cross_checks(values, bad):
    """Relations between keys that are valid on their own. A pair with a key
    that already failed is left to that failure."""

    def pair(a, b):
        (sa, ka), (sb, kb) = a, b
        return values.get(sa, {}).get(ka), values.get(sb, {}).get(kb)

    view, wrist = pair(("cameras", "view"), ("cameras", "wrist"))
    if view is not None and view == wrist:
        bad("cameras.wrist", "inconsistent")
    pose = values.get("home", {}).get("pose")
    if pose is not None:
        height = pose[2]
        floor = values.get("safety", {}).get("min_z_floor")
        default = values.get("safety", {}).get("min_z_default")
        if floor is not None and floor >= height:
            bad("safety.min_z_floor", "inconsistent")
        if default is not None and default >= height:
            bad("safety.min_z_default", "inconsistent")
    floor, default = pair(("safety", "min_z_floor"), ("safety", "min_z_default"))
    if floor is not None and default is not None and default < floor:
        bad("safety.min_z_default", "inconsistent")
    for low, high in (
        ("max_steps_default", "max_steps_cap"),
        ("eval_num_default", "eval_num_cap"),
        ("rtc_horizon_default", "rtc_horizon_max"),
        ("open_loop_default", "open_loop_max"),
        ("cfg_scale_default", "cfg_scale_max"),
    ):
        a, b = pair(("caps", low), ("caps", high))
        if a is not None and b is not None and a > b:
            bad(f"caps.{low}", "inconsistent")
    known, cfg = pair(("catalog", "known_configs"), ("catalog", "cfg_configs"))
    if known is not None and cfg is not None and not set(cfg) <= set(known):
        bad("catalog.cfg_configs", "inconsistent")


def parse(data, origin: str) -> Site:
    """The ``Site`` of the site-file data; raises ``SiteInvalid`` listing every
    problem. ``origin`` says where the file was found."""
    errors = []

    def bad(field, code):
        errors.append({"field": field, "code": code})

    if not isinstance(data, dict):
        raise SiteInvalid([{"field": "file", "code": "type"}])
    if "schema" not in data:
        bad("schema", "missing")
    elif data["schema"] != SCHEMA:
        bad("schema", "range")
    for key in data:
        if key != "schema" and key not in SCHEMA_CHECKS:
            bad(key, "unknown_key")
    values = {}
    for section, checks in SCHEMA_CHECKS.items():
        table = data.get(section)
        if section not in data:
            bad(section, "missing")
            continue
        if not isinstance(table, dict):
            bad(section, "type")
            continue
        for key in table:
            if key not in checks:
                bad(f"{section}.{key}", "unknown_key")
        kept = values[section] = {}
        for key, check in checks.items():
            if key not in table:
                if (section, key) in DEFAULTS:
                    kept[key] = DEFAULTS[(section, key)]
                else:
                    bad(f"{section}.{key}", "missing")
                continue
            try:
                kept[key] = check(table[key])
            except _Bad as exc:
                bad(f"{section}.{key}", exc.code)
    _cross_checks(values, bad)
    if errors:
        raise SiteInvalid(errors)
    return Site(
        origin=origin,
        **values["paths"],
        **{name: cls(**values[name]) for name, cls in SECTIONS.items()},
    )


# --- where it is and how it is read -------------------------------------------------------


def main_checkout(checkouts=None) -> Path:
    """The main checkout: the first one with a ``.git`` directory (a linked
    worktree has a ``.git`` file), else the first one. ``checkouts`` is a
    function returning the candidates (``levi.live.locate.checkouts``)."""
    if checkouts is None:
        from levi.live import locate

        checkouts = locate.checkouts
    found = checkouts()
    return next((c for c in found if (c / ".git").is_dir()), found[0])


def find_path(environ=None, checkouts=None):
    """``(path, origin)``: ``LEVI_RIG_SITE`` when set (origin ``env``), else
    the file in the main checkout (``main_checkout``)."""
    environ = os.environ if environ is None else environ
    named = (environ.get(ENV) or "").strip()
    if named:
        return _named(named), "env"
    return main_checkout(checkouts).joinpath(*RELATIVE), "main_checkout"


def _named(text) -> Path:
    """``~`` and ``~/…`` are this user's home. ``~name`` is not expanded (it
    would reach another user's folder, or raise for a name that does not exist):
    it stays a relative name, which ``load`` reports as not absolute."""
    if text == "~" or text.startswith("~/"):
        with contextlib.suppress(RuntimeError):
            return Path.home() / text[2:]
    return Path(text)


@dataclass(frozen=True)
class SiteLoad:
    state: str  # "ok" | "missing" | "invalid"
    site: Site | None
    looked_at: Path
    origin: str
    errors: list


_LOCK = threading.Lock()
_CACHE: dict = {}


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def _file_error(code):
    return [{"field": "file", "code": code}]


def _read(path: Path, origin: str, before) -> SiteLoad:
    def result(state, site=None, errors=()):
        return SiteLoad(state, site, path, origin, list(errors))

    if not stat.S_ISREG(before.st_mode):
        return result("invalid", errors=_file_error("not_regular"))
    if before.st_uid != os.geteuid():
        return result("invalid", errors=_file_error("not_owner"))
    if before.st_mode & 0o022:
        return result("invalid", errors=_file_error("writable"))
    try:
        data, truncated, _ = probes.read_regular(path, MAX_BYTES)
    except probes.NotRegular:
        return result("invalid", errors=_file_error("not_regular"))
    except OSError:
        return result("invalid", errors=_file_error("unreadable"))
    if truncated:
        return result("invalid", errors=_file_error("too_large"))
    try:
        table = tomllib.loads(data.decode("utf-8"))
    except (
        UnicodeDecodeError,
        tomllib.TOMLDecodeError,
        RecursionError,  # nesting deeper than the parser's stack
        ValueError,  # an integer longer than Python converts
    ):
        return result("invalid", errors=_file_error("not_toml"))
    try:
        return result("ok", site=parse(table, origin))
    except SiteInvalid as exc:
        return result("invalid", errors=exc.errors)


def load(environ=None, checkouts=None) -> SiteLoad:
    """Look for the site file and read it. Never raises. The file is parsed
    again only when its identity, modification time or size changed."""
    path, origin = find_path(environ, checkouts)
    if not path.is_absolute():
        return SiteLoad(
            "invalid", None, path, origin, [{"field": ENV, "code": "not_absolute"}]
        )
    try:
        before = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return SiteLoad("missing", None, path, origin, [])
    except (OSError, ValueError):  # (ValueError: a NUL byte in the name)
        return SiteLoad("invalid", None, path, origin, _file_error("unreadable"))
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


def ensure(loaded: SiteLoad) -> Site:
    """The site of a successful load; else the 412 every route answers."""
    if loaded.state == "ok":
        return loaded.site
    if loaded.state == "missing":
        raise fail(
            412,
            SITE_MISSING,
            "rig site file missing",
            looked_at=str(loaded.looked_at),
            template=TEMPLATE,
        )
    raise fail(412, SITE_INVALID, "rig site file is invalid", errors=loaded.errors)


def require(environ=None, checkouts=None) -> Site:
    """The site, or the 412 answer when it is missing or invalid."""
    return ensure(load(environ, checkouts))


# --- text, numbers and the fixed client flags -------------------------------------------------


def number(value) -> str:
    """A float as the demo commands print it: ``3.0``, ``0.1``, ``-0.011145``."""
    text = format(float(value), ".10g")
    return text if any(c in text for c in ".en") else text + ".0"


def prompt_reason(text, max_chars):
    """Why ``text`` cannot be a task instruction (or note) on a command line,
    or ``None``: ``empty``, ``too_long``, ``control_char`` (control, format and
    line-separator characters, any whitespace but a plain space),
    ``leading_dash`` or ``forbidden_char`` (quotes, backslash, `$`, backtick, `!`)."""
    if not text.strip():
        return "empty"
    if len(text) > max_chars:
        return "too_long"
    for char in text:
        if unicodedata.category(char) in {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"} or (
            char.isspace() and char != " "
        ):
            return "control_char"
    if text.startswith("-"):
        return "leading_dash"
    if FORBIDDEN_TEXT & set(text):
        return "forbidden_char"
    return None


def fixed_items(site: Site) -> dict:
    """The client flags that come from the site file alone and that no form can
    change: ``{flag: (kind, value)}`` in display order. ``kind`` is ``value``
    (one ``--flag=value`` token), ``switch`` (``--flag`` or ``--no-flag``) or
    ``tuple`` (the flag, then one token per number)."""
    safety = site.safety
    return {
        "--max-xy": ("value", number(safety.max_xy)),
        "--max-step-m": ("value", number(safety.max_step_m)),
        "--max-step-rad": ("value", number(safety.max_step_rad)),
        "--rx-range": ("tuple", tuple(number(v) for v in safety.rx_range)),
        "--ry-range": ("tuple", tuple(number(v) for v in safety.ry_range)),
        "--rz-range": ("tuple", tuple(number(v) for v in safety.rz_range)),
        "--binarize-gripper": ("switch", safety.binarize_gripper),
        "--require-matching-server": ("switch", safety.require_matching_server),
        "--allow-unknown-prompt": ("switch", safety.allow_unknown_prompt),
        "--home-duration-s": ("value", number(site.home.duration_s)),
    }


def flag_tokens(flag: str, kind: str, value) -> list:
    """The argv tokens of one flag (see ``fixed_items``)."""
    if kind == "value":
        return [f"{flag}={value}"]
    if kind == "switch":
        return [flag if value else "--no-" + flag[2:]]
    return [flag, *value]


def fixed_flags(site: Site) -> list:
    """The fixed client flags as the page shows them."""
    return [" ".join(flag_tokens(f, k, v)) for f, (k, v) in fixed_items(site).items()]


def public_view(site: Site) -> dict:
    """``GET /api/levi/rig/site``: what the page may know, with no path (the
    folders appear by their last name only)."""
    caps = site.caps
    return {
        "schema": VIEW_SCHEMA,
        "source": site.origin,
        "server": {
            "port": site.server.port,
            "remote_host": site.server.remote_host,
            "robot_server_url": site.server.robot_server_url,
        },
        "cameras": {"view": site.cameras.view, "wrist": site.cameras.wrist},
        "home": {"pose": list(site.home.pose), "duration_s": site.home.duration_s},
        "min_z": {
            "floor": site.safety.min_z_floor,
            "default": site.safety.min_z_default,
        },
        "caps": {
            "max_steps": {"default": caps.max_steps_default, "cap": caps.max_steps_cap},
            "eval_num": {"default": caps.eval_num_default, "cap": caps.eval_num_cap},
            "rtc_horizon": {
                "default": caps.rtc_horizon_default,
                "max": caps.rtc_horizon_max,
            },
            "open_loop_horizon": {
                "default": caps.open_loop_default,
                "max": caps.open_loop_max,
            },
            "prompt_max_chars": caps.prompt_max_chars,
            "note_max_chars": caps.note_max_chars,
            "cfg_scale": {
                "default": caps.cfg_scale_default,
                "max": caps.cfg_scale_max,
            },
        },
        "fixed_flags": fixed_flags(site),
        "gpu": {
            "fraction_presets": list(site.gpu.fraction_presets),
            "margin_mib": site.gpu.margin_mib,
        },
        "unit": {
            "nice": site.unit.nice,
            "cpu_affinity": site.unit.cpu_affinity,
            "stop_timeout_s": site.unit.stop_timeout_s,
            "start_timeout_s": site.unit.start_timeout_s,
        },
        "names": {
            "openpi_checkout": posixpath.basename(site.openpi_checkout),
            "rollout_root": posixpath.basename(site.rollout_root),
        },
        "prompts": {
            "default": site.prompts.default,
            "known": list(site.prompts.known),
            "restricted": list(RESTRICTED_PROMPTS),
            "restricted_prefixes": list(site.catalog.restricted_prefixes),
        },
        "source_hash": {
            "manifest": bool(site.source.manifest),
            "files": len(site.source.hash_files),
        },
    }
