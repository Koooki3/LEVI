"""Prepare one AERI run for the marvel0 page: the versioned port of
``lab/aeri-external/tools/new_run.py`` and the things every part of the AERI
card shares (refusals, the machine seam, the code pin, the templates, run names).

What this module may do, and nothing more:

* **Write only ``<runs_root>/<name>/``** (``job.yaml``, ``binding.json``,
  ``RUN.json``, ``check.json``, ``queue/`` in external mode) and the page's own
  folder (``<workspace state>/marvel0/aeri_prep``: ``record.json``,
  ``requests/``, ``history.jsonl``). It never writes into a rollout root, a
  worktree, a template or the AERI registry.
* **Run one read-only process**: ``aeri.py check`` of the pinned worktree
  (``nice -n 19``, 60 s, an environment of three variables), and ``git`` to read
  the worktree's HEAD and cleanliness. Nothing connects to a port or the robot,
  nothing starts a service, nothing here can make the arm move.
* **Refuse what it does not know.** The AERI code must be at the commit the
  site file pins, with a clean tree and a known prompt profile; the two
  templates must hash to the lines of ``SOURCES.sha256``; a profile without
  startup homing cannot be asked for it. An unknown version is refused, never
  guessed.

The port is behaviour-equivalent to ``new_run.py`` (same substitutions, same
files, same ``execute`` string for the same worktree); what it adds is the
checks above, a name that is unique across the runs folder, the rollout's AERI
runs, the AERI registry and this page's record, the removal of a half-made
folder when the check fails, and digests of what it wrote (``files_sha256``) so
the page can tell later that nothing changed between prepare and start.

**What the gates are worth.** They stop mistakes, not another program of the
same account, which can write these files itself. The real barriers are Franka
Desk and the physical emergency stop.
"""

import contextlib
import datetime
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from levi import paths
from levi.live import jsonio
from levi.setup import probes
from levi.workspace import Workspace

from . import record
from .errors import fail

REL = ("marvel0", "aeri_prep")
RECORD_SCHEMA = "levi.marvel0.aeri_prep_record.v1"
PREPARE_SCHEMA = "levi.marvel0.aeri_prepare.v1"
ACTION_SCHEMA = "levi.marvel0.aeri_action.v1"
HISTORY_MAX_BYTES = 256 * 1024

TEMPLATE_FILES = ("job.base.yaml", "binding.base.json")
# The site's optional third template, read (and hashed against SOURCES.sha256)
# only when the form asks for the reset policy, for an early check on events or
# for the takeover: the reset service block, the reset policy's training
# sentences, the ``signal_events`` declaration and the takeover's limits
# (``STEP5_SCHEMA``).
STEP5_FILE = "step5.base.json"
STEP5_SCHEMA = "levi.marvel0.aeri_step5_template.v1"
SOURCES_FILE = "SOURCES.sha256"
TEMPLATE_MAX_BYTES = 1 << 18
RUN_JSON_MAX_BYTES = 1 << 17
CHECK_TIMEOUT_S = 60.0
GIT_TIMEOUT_S = 5.0
CHECK_TAIL_CHARS = 400
CHECK_ENV_PATH = "/usr/bin:/bin"
NAME_TRIES = 50
EXECUTE_OVERLAY = "pyrealsense2==2.55.1.6486"  # the overlay new_run.py asks uv for

PHRASE_PREPARE = "prepare aeri run"
PHRASE_LIVE_STOP = "stop live service"
PHRASE_LEFTOVERS = "stop leftover aeri units"

# What the form may ask for (the owner's decisions of 21 Oct: up to 3000 steps;
# 4.0 s is the gripper confirmation he approved on 18 Oct).
ROUNDS = {"min": 1, "max": 50, "default": 5}
STEPS = {"min": 1, "max": 3000, "default": 200}
GRIPPER_CONFIRM_S = {"min": 1.0, "max": 10.0, "default": 4.0}
# ``subtask.early_check`` as the form may ask for it (design STEP5-AERI-RESET R4 b):
# the defaults of a switched-on check. The event types the form offers are the
# gripper's opening only; the measured gripper position is the only source
# (``check_step5_signal_events``). ``every_steps`` is the fallback without an event.
EARLY_CHECK_FORM = {
    "on_events": True,
    "event_types": ["gripper_open"],
    "settle_steps": 8,
    "every_steps": 60,
    "max_checks": 3,
}
EARLY_FORM_EVENT_TYPES = ("gripper_open",)
NAME = re.compile(r"aeri-\d{8}-(?:ext|loc)-r\d{2,}")
TASK = re.compile(r"[a-z0-9 ,.'-]{3,120}")  # new_run.py's pattern
TASK_MAX_CHARS = 120
MODES = {
    "external": {
        "tag": "ext",
        "model": "agent-bridge",
        "served": "agent",
        "util": "0.01",
        "timeout": 600,
    },
    "local": {
        "tag": "loc",
        "model": None,  # the site's local_model
        "served": "qwen3-vl-4b",
        "util": "0.34",
        "timeout": 120,
    },
}
BRIDGE = "agent_vlm_bridge.py"
PROXY = "harness_proxy.py"
BRIDGE_PYTHON = "/usr/bin/python3"
BRIDGE_ENV_PATH = "/usr/local/bin:/usr/bin:/bin"
# The lowest the rest of the AERI stack needs (AERI.md section 2).
GPU_NEED_MIB = {"external": 9 * 1024, "local": 19 * 1024}

# code -> (HTTP status, message). No message holds a path.
REFUSALS = {
    "aeri_disabled": (403, "AERI hosting is off in the control file"),
    "aeri_unconfigured": (412, "The site file has no valid [aeri] section"),
    "not_product": (
        409,
        "Only the product LEVI may act on the real machine; this instance only shows",
    ),
    "profile_unknown": (412, "The pinned AERI code has no known prompt profile"),
    "code_pin_mismatch": (
        412,
        "AERI worktree is not at the pinned commit or is dirty",
    ),
    "templates_changed": (412, "A template does not match SOURCES.sha256"),
    "run_name_taken": (409, "The run name exists in a registry"),
    "check_failed": (422, "aeri.py check refused"),
    "form_invalid": (422, "The form is not valid"),
    "start_home_unsupported": (422, "This code version has no startup homing"),
    "confirm_required": (400, "Type the confirmation phrase exactly: {phrase}"),
    "request_id_invalid": (
        400,
        "request_id is 8-80 characters of A-Z, a-z, 0-9 and . _ : -",
    ),
    "request_id_used": (409, "That request id was already used for another request"),
    "busy": (423, "Another AERI preparation action is going on"),
    "lock_unusable": (503, "The machine-wide run lock could not be taken"),
    "state_dir_unusable": (412, "The folder for the AERI records cannot be used"),
    "d_active": (409, "An AERI run is alive"),
    "e_active": (409, "A hosted evaluation client or session is on"),
    "arm_in_use": (409, "The arm lock is held"),
    "gpu_in_use": (409, "The GPU lock is held"),
    "state_unreadable": (
        409,
        "A source could not be read, so nothing is done (an unknown state refuses)",
    ),
    "live_stop_failed": (502, "The live annotation service did not stop"),
    "stop_failed": (502, "A unit did not stop"),
}


class Refuse(Exception):
    """A request that does not go on: stable ``code``, HTTP ``status``, English
    ``message`` and the facts as ``extra``."""

    def __init__(self, code, **extra):
        self.status, text = REFUSALS[code]
        self.code = code
        self.extra = extra
        try:
            self.message = text.format(**extra)
        except (KeyError, IndexError):
            self.message = text
        super().__init__(f"{code}: {self.message}")


def http(exc: Refuse):
    """``exc`` as the answer of a route (``errors.fail``)."""
    return fail(exc.status, exc.code, exc.message, **exc.extra)


# --- the machine seam -----------------------------------------------------------------------


@dataclass
class Ran:
    returncode: int
    stdout: str
    stderr: str


def run_capture(argv, *, cwd=None, env=None, timeout=GIT_TIMEOUT_S) -> Ran:
    """Run ``argv`` (never through a shell), capture its text output. Raises
    ``subprocess.TimeoutExpired``, ``OSError``."""
    done = subprocess.run(
        list(argv),
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    return Ran(done.returncode, done.stdout, done.stderr)


@dataclass
class Host:
    """What the AERI card reaches outside Python that a test replaces:
    the clock, a process runner, the AERI registry folder, where the page keeps
    its records. ``production_host()`` is the real one."""

    now: Callable = time.time
    run: Callable = run_capture
    registry: Callable = lambda: _registry_dir()
    workspace: Callable = lambda: None  # a Workspace, or None for the process-wide one
    environ: Mapping = field(default_factory=lambda: os.environ)


def production_host() -> Host:
    return Host()


def _registry_dir() -> Path:
    from levi.automatic import launch

    return launch.home() / launch.RUNS


# --- the site's [aeri] section and the profile table ----------------------------------------


def scrub(text) -> str:
    return record.scrub(text)


CAPABILITIES = (
    "start_home",
    "early_check",
    "reset_policy",
    "grasp_assist",
    "auto_grasp",
)


def known_profiles():
    """``{profile id: {capability: bool}}`` (``CAPABILITIES``) from the
    hosted-run module's table (``aeri_state.PROFILES``), or None when it cannot
    be read: an unreadable table is "unknown", never an empty list that would
    look like "none". A capability the table does not say is False."""
    try:
        from . import aeri_state
    except ImportError:
        return None
    except Exception:  # noqa: BLE001 - a broken table is not a known one
        return None
    table = getattr(aeri_state, "PROFILES", None)
    if not isinstance(table, Mapping):
        return None
    out = {}
    for key, value in table.items():
        if not isinstance(key, str):
            continue

        def read(name, value=value):
            return (
                value.get(name)
                if isinstance(value, Mapping)
                else getattr(value, name, None)
            )

        if not isinstance(read("start_home"), bool):
            continue
        out[key] = {name: read(name) is True for name in CAPABILITIES}
    return out


def profile_info(profile_id):
    """``{"known": bool|None, <capability>: bool|None}`` (``CAPABILITIES``).
    ``known`` None means the table could not be read."""
    table = known_profiles()
    if table is None:
        return {"known": None, **dict.fromkeys(CAPABILITIES)}
    if profile_id not in table:
        return {"known": False, **dict.fromkeys(CAPABILITIES)}
    return {"known": True, **table[profile_id]}


# --- the code pin ---------------------------------------------------------------------------


def git_state(worktree, run=run_capture) -> dict:
    """``{"head": 40 hex | None, "dirty": bool | None}`` of the worktree, read
    with ``git`` and no optional lock (nothing is written there). None says it
    could not be told. The folder must itself be the top of a git work tree: a
    folder inside some other repository would otherwise answer with that
    repository's HEAD."""
    env = {"PATH": CHECK_ENV_PATH, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}
    head = dirty = None
    try:
        top = run(
            ["git", "-C", worktree, "rev-parse", "--show-toplevel"],
            env=env,
            timeout=GIT_TIMEOUT_S,
        )
        if top.returncode != 0 or os.path.realpath(top.stdout.strip()) != (
            os.path.realpath(worktree)
        ):
            return {"head": None, "dirty": None}
        done = run(
            ["git", "-C", worktree, "rev-parse", "HEAD"], env=env, timeout=GIT_TIMEOUT_S
        )
        text = done.stdout.strip()
        if done.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", text):
            head = text
        done = run(
            ["git", "-C", worktree, "status", "--porcelain"],
            env=env,
            timeout=GIT_TIMEOUT_S,
        )
        if done.returncode == 0:
            dirty = bool(done.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return {"head": head, "dirty": dirty}


def pin_matches(pin, head) -> bool:
    """HEAD starts with the pinned digits (8 to 40, lower case)."""
    return (
        isinstance(pin, str)
        and isinstance(head, str)
        and re.fullmatch(r"[0-9a-f]{8,40}", pin) is not None
        and head.startswith(pin)
    )


def require_code_pin(aeri, run=run_capture) -> dict:
    """The git state, or ``Refuse("code_pin_mismatch")`` when HEAD is not the
    pinned commit, the tree is dirty, or either cannot be told."""
    state = git_state(aeri.worktree, run)
    if state["head"] is None or state["dirty"] is None:
        raise Refuse("code_pin_mismatch", head=None, pin=aeri.pin_sha, dirty=None)
    if not pin_matches(aeri.pin_sha, state["head"]) or state["dirty"]:
        raise Refuse(
            "code_pin_mismatch",
            head=state["head"][:8],
            pin=aeri.pin_sha[:8],
            dirty=state["dirty"],
        )
    return state


# --- the templates --------------------------------------------------------------------------


def _read_file(path, limit=TEMPLATE_MAX_BYTES) -> bytes | None:
    try:
        data, truncated, _ = probes.read_regular(path, limit)
    except (OSError, probes.NotRegular):
        return None
    return None if truncated else data


def sources_table(text) -> dict:
    """``{file name: sha256}`` of a ``SOURCES.sha256`` (``sha256sum`` format; the
    paths in the lines are from where the files once were, only the name counts)."""
    out = {}
    for line in text.splitlines():
        fields = line.split(None, 1)
        if len(fields) != 2 or not re.fullmatch(r"[0-9a-f]{64}", fields[0]):
            continue
        out[os.path.basename(fields[1].strip().lstrip("*"))] = fields[0]
    return out


def templates_state(templates_root) -> dict:
    """``{"ok": bool | None, "which": [names that differ or are unreadable],
    "digests": {name: sha256}, "data": {name: bytes}}``. ``ok`` is None when
    ``SOURCES.sha256`` itself cannot be read."""
    root = Path(templates_root)
    sources = _read_file(root / SOURCES_FILE, 1 << 16)
    if sources is None:
        return {"ok": None, "which": [SOURCES_FILE], "digests": {}, "data": {}}
    wanted = sources_table(sources.decode("utf-8", "replace"))
    which, digests, data = [], {}, {}
    for name in TEMPLATE_FILES:
        blob = _read_file(root / name)
        if blob is None:
            which.append(name)
            continue
        digests[name] = hashlib.sha256(blob).hexdigest()
        data[name] = blob
        if wanted.get(name) != digests[name]:
            which.append(name)
    return {"ok": not which, "which": which, "digests": digests, "data": data}


def require_templates(aeri) -> dict:
    state = templates_state(aeri.templates_root)
    if state["ok"] is not True:
        raise Refuse("templates_changed", which=state["which"])
    return state


# --- the step-5 template: the reset service and the event declaration -------------------------
#
# ``step5.base.json`` is the site's optional third template (next to
# ``job.base.yaml`` and ``binding.base.json``, and, like them, hashed in
# ``SOURCES.sha256``). It is read only for a form that asks for ``reset_policy``,
# an ``early_check`` on events or ``grasp_assist``, so a site without it prepares
# every other run exactly as before. Exactly these keys, each block optional:
#
#   schema        "levi.marvel0.aeri_step5_template.v1"
#   reset         deployment {checkpoint_id, config, checkpoint_dir}  -> job policies.reset
#                 endpoint   {host, port}                             -> binding policies.reset
#                 service    {unit, argv, working_directory, mem_fraction}
#                                                    -> binding policy_swap.services.reset
#                 prompts    ["<pick words>|<place words>|<task>", ...] -> job subtask.reset_prompts
#   signal_events the binding's ``signal_events`` object, whole
#                 (``levi.aeri.signal_events.binding.v1``) -> binding signal_events
#   grasp_assist  {step_m, max_steps, max_travel_m, follow_error_m,
#                 max_follow_misses, holding_band} (required) and, optional, the
#                 automatic grasp's {grasp_height, yaw_enabled, yaw_step_rad,
#                 max_yaw_rad, auto_max_retries, locator_coords, grasp_pixel}, the
#                 formats of the job's ``subtask.grasp_assist`` -> job
#                 subtask.grasp_assist (with ``enabled: true`` and the form's
#                 ``grasp_source``: ``operator`` with ``handoff: operator``, or
#                 ``auto`` with ``handoff: holding_fact``: never the template's)
#
# The reset service is ``integrations/policy_loader`` (docs/POLICY_LOADER.md).

CHECKPOINT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}")
CHECKPOINT_DIR = re.compile(r"/[A-Za-z0-9_.+-]+(?:/[A-Za-z0-9_.+-]+)*")
RESET_UNIT = "levi-aeri-policy-reset"
ROBOT_SIDE_PORTS = (5000, 5001, 5100, 7470)
VLM_PORT = 8100  # the subtask VLM service render_binding writes
GRIPPER_PATH = ["robot_state", "gripper_position"]  # measured, never ["pose", 6]


def _template_bad(field_name, detail):
    return _bad(field_name, "template", detail=detail)


def _only(value, keys, field_name, what):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise _template_bad(field_name, f"{what} must have exactly {sorted(keys)}")
    return value


def check_step5_reset(block) -> dict:
    """The ``reset`` block of the step-5 template, shape-checked (the AERI
    ``check`` of the pinned code checks the rest). Returns it."""
    from levi.domain import aeri

    f = "reset_policy"
    _only(block, ("deployment", "endpoint", "service", "prompts"), f, "reset")
    dep = _only(
        block["deployment"],
        ("checkpoint_id", "config", "checkpoint_dir"),
        f,
        "deployment",
    )
    if not all(
        isinstance(dep[k], str) and CHECKPOINT_ID.fullmatch(dep[k])
        for k in ("checkpoint_id", "config")
    ):
        raise _template_bad(f, "deployment checkpoint_id and config are plain names")
    if not (
        isinstance(dep["checkpoint_dir"], str)
        and CHECKPOINT_DIR.fullmatch(dep["checkpoint_dir"])
        and ".." not in dep["checkpoint_dir"].split("/")
    ):
        raise _template_bad(f, "deployment checkpoint_dir is an absolute path")
    end = _only(block["endpoint"], ("host", "port"), f, "endpoint")
    port = end["port"]
    if (
        end["host"] != "127.0.0.1"
        or type(port) is not int
        or not 1024 <= port <= 65535
        or port in ROBOT_SIDE_PORTS
    ):
        raise _template_bad(f, "endpoint is 127.0.0.1 and a port that is not a robot's")
    svc = _only(
        block["service"],
        ("unit", "argv", "working_directory", "mem_fraction"),
        f,
        "service",
    )
    argv = svc["argv"]
    if (
        svc["unit"] != RESET_UNIT
        or not isinstance(argv, list)
        or not all(isinstance(a, str) for a in argv)
        or argv.count("--port") != 1
        or argv[argv.index("--port") + 1 : argv.index("--port") + 2] != [str(port)]
    ):
        raise _template_bad(
            f, f"service unit is {RESET_UNIT} and its argv has --port {port}"
        )
    fraction = svc["mem_fraction"]
    if (
        isinstance(fraction, bool)
        or not isinstance(fraction, (int, float))
        or not 0 < fraction <= 0.5
    ):
        raise _template_bad(f, "service mem_fraction is a number in (0, 0.5]")
    prompts = block["prompts"]
    if (
        not isinstance(prompts, list)
        or not 1 <= len(prompts) <= 64
        or not all(
            isinstance(e, str)
            and re.fullmatch(aeri.RESET_PROMPT_ENTRY, e)
            # the job writes each entry as a plain YAML scalar, which drops
            # trailing spaces: the frozen sentence must survive unchanged
            and e == e.strip()
            for e in prompts
        )
        or len({e.rpartition("|")[0] for e in prompts}) != len(prompts)
    ):
        raise _template_bad(f, "prompts are 1 to 64 entries, one per pick and place")
    return block


GRASP_ASSIST_KEYS = (
    "step_m",
    "max_steps",
    "max_travel_m",
    "follow_error_m",
    "max_follow_misses",
    "holding_band",
)
# Optional keys of the template's block, for the automatic grasp (the form's
# ``grasp_source: auto``); ``grasp_height`` is required for it.
GRASP_ASSIST_AUTO_KEYS = (
    "grasp_height",
    "yaw_enabled",
    "yaw_step_rad",
    "max_yaw_rad",
    "auto_max_retries",
    "locator_coords",
    "grasp_pixel",
)


def check_step5_grasp_assist(block) -> dict:
    """The ``grasp_assist`` block of the step-5 template, checked as strictly as
    the reset block: the six keys and, optional, the automatic grasp's
    (``GRASP_ASSIST_AUTO_KEYS``; nothing else), real numbers and whole numbers
    (never a bool or a string),
    then the job's own bounds (``levi.domain.aeri.JobSubtaskGraspAssist``) and the
    takeover's own parse of the opening bands (``grasp_assist.GraspAssistConfig``).
    ``grasp_source`` and ``handoff`` are not keys of it: the page always writes
    ``operator`` for both. Returns the block."""
    from levi.automatic import grasp_assist as ga
    from levi.domain import aeri

    f = "grasp_assist"
    if (
        not isinstance(block, dict)
        or not set(GRASP_ASSIST_KEYS) <= set(block)
        or not set(block) <= set(GRASP_ASSIST_KEYS + GRASP_ASSIST_AUTO_KEYS)
    ):
        raise _template_bad(
            f,
            f"grasp_assist must have {sorted(GRASP_ASSIST_KEYS)} and only, "
            f"optional, {sorted(GRASP_ASSIST_AUTO_KEYS)}",
        )

    def number(name):
        value = block[name]
        if type(value) not in (int, float) or isinstance(value, bool):
            raise _template_bad(f, f"{name} is a number")
        if not math.isfinite(value):
            raise _template_bad(f, f"{name} is a finite number")

    def whole(name):
        if type(block[name]) is not int:
            raise _template_bad(f, f"{name} is a whole number")

    for name in ("step_m", "max_travel_m", "follow_error_m"):
        number(name)
    for name in ("max_steps", "max_follow_misses"):
        whole(name)
    for name in GRASP_ASSIST_KEYS[:-1]:
        if not _yaml_exact(block[name]):
            raise _template_bad(f, f"{name} does not survive being written as text")
    band = block["holding_band"]
    if not isinstance(band, list) or not all(isinstance(e, str) for e in band):
        raise _template_bad(f, "holding_band is a list of text entries")
    if any(e != e.strip() for e in band):
        raise _template_bad(f, "holding_band entries have no edge spaces")
    _check_auto_keys(block, f)
    auto = "grasp_height" in block
    source, handoff = ("auto", "holding_fact") if auto else ("operator", "operator")
    try:
        aeri.JobSubtaskGraspAssist(
            enabled=True, grasp_source=source, handoff=handoff, **block
        )
        ga.GraspAssistConfig(
            enabled=True,
            grasp_source=source,
            handoff=handoff,
            **{
                **block,
                "holding_band": tuple(band),
                **(
                    {"grasp_height": tuple(block["grasp_height"])}
                    if "grasp_height" in block
                    else {}
                ),
            },
        )
    except ValueError as exc:  # pydantic's ValidationError is one
        raise _template_bad(f, "grasp_assist: " + scrub(str(exc))[:160]) from None
    return block


def _check_auto_keys(block, f) -> None:
    """The template's optional automatic-grasp keys, by type (the job's own bounds
    are checked after, by the job model and the takeover's config)."""
    for name in ("yaw_step_rad", "max_yaw_rad"):
        if name in block:
            value = block[name]
            if type(value) not in (int, float) or not math.isfinite(value):
                raise _template_bad(f, f"{name} is a number")
            if not _yaml_exact(value):
                raise _template_bad(f, f"{name} does not survive being written as text")
    if "auto_max_retries" in block and type(block["auto_max_retries"]) is not int:
        raise _template_bad(f, "auto_max_retries is a whole number")
    if "yaw_enabled" in block and type(block["yaw_enabled"]) is not bool:
        raise _template_bad(f, "yaw_enabled is true or false")
    if "locator_coords" in block and block["locator_coords"] not in (
        "pixel",
        "norm1000",
    ):
        raise _template_bad(f, "locator_coords is pixel or norm1000")
    if "grasp_pixel" in block:
        px = block["grasp_pixel"]
        if not (
            isinstance(px, list)
            and len(px) == 2
            and all(
                type(v) in (int, float) and math.isfinite(v) and _yaml_exact(v)
                for v in px
            )
        ):
            raise _template_bad(f, "grasp_pixel is [x, y], two numbers")
    if "grasp_height" in block:
        hs = block["grasp_height"]
        if (
            not isinstance(hs, list)
            or not hs
            or not all(isinstance(e, str) and e == e.strip() for e in hs)
        ):
            raise _template_bad(f, "grasp_height is a list of text entries")


def check_step5_signal_events(block) -> dict:
    """The ``signal_events`` block of the step-5 template: the binding's own
    validation (``fr3_run.signal_binding``, no I/O) and, on top, the one rule of
    the early check on events: a gripper channel must be the **measured**
    gripper position (``robot_state.gripper_position``), never the commanded
    value (``pose[6]``), so an event is something the gripper did, not something
    that was asked of it. A declaration without such a channel is refused."""
    f = "early_check"
    from levi.automatic import fr3_run

    try:
        fr3_run.signal_binding(block)
    except ValueError as exc:
        raise _template_bad(f, "signal_events: " + scrub(str(exc))[:160]) from None
    channels = [c for c in block["profile"]["channels"] if c["role"] == "gripper"]
    if not channels:
        raise _template_bad(f, "signal_events has no gripper channel")
    for channel in channels:
        bound = [
            b
            for b in block["bindings"]
            if (b["feature"], b["index"]) == (channel["feature"], channel["index"])
        ]
        if (
            channel["kind"] != "measured"
            or not bound
            or any(b["kind"] != "measured" or b["path"] != GRIPPER_PATH for b in bound)
        ):
            raise _template_bad(
                f, "the gripper channel must be the measured gripper position"
            )
    return block


def check_reset_port(binding_template: dict, step5: dict) -> None:
    """The reset service's port is neither the forward policy's nor the subtask
    VLM's (both from the site's binding template; the VLM port is the one
    ``render_binding`` serves on). Refused before a folder is made, not at run
    time when the bind or the identity check would fail."""
    port = step5["value"]["reset"]["endpoint"]["port"]
    taken = {VLM_PORT}
    forward = binding_template.get("policies", {}).get("forward", {})
    if isinstance(forward.get("port"), int):
        taken.add(forward["port"])
    base = str(binding_template.get("subtask_vlm", {}).get("base_url", ""))
    tail = base.rsplit(":", 1)[-1].split("/", 1)[0]
    if tail.isdigit():
        taken.add(int(tail))
    if port in taken:
        raise _template_bad(
            "reset_policy", "the reset port is the forward policy's or the VLM's"
        )


def step5_needs(form) -> tuple:
    """``(reset, events, assist)``: which blocks of the step-5 template this form
    uses."""
    events = form.early_check is not None and form.early_check.on_events
    return bool(form.reset_policy), bool(events), bool(form.grasp_assist)


def step5_state(templates_root) -> dict:
    """``{"state": "absent" | "ok" | "changed" | "unreadable", "sha256", "value"}``
    of the step-5 template against ``SOURCES.sha256``."""
    root = Path(templates_root)
    sources = _read_file(root / SOURCES_FILE, 1 << 16)
    blob = _read_file(root / STEP5_FILE)
    if sources is None:
        return {"state": "unreadable", "sha256": None, "value": None}
    if blob is None:
        path = root / STEP5_FILE
        # Only a file that is not there is absent; one that is there but cannot
        # be read as a regular file (a link, too large, no permission) changed.
        gone = not (path.exists() or path.is_symlink())
        state = "absent" if gone else "unreadable"
        return {"state": state, "sha256": None, "value": None}
    digest = hashlib.sha256(blob).hexdigest()
    wanted = sources_table(sources.decode("utf-8", "replace")).get(STEP5_FILE)
    if wanted != digest:
        return {"state": "changed", "sha256": digest, "value": None}
    try:
        value = json.loads(blob)
    except ValueError:
        return {"state": "unreadable", "sha256": digest, "value": None}
    return {"state": "ok", "sha256": digest, "value": value}


def require_step5(aeri, form):
    """The checked step-5 template for a form that needs it, else None. A site
    without the file, or without the block the form needs, refuses (``form_invalid``,
    reason ``template``); a file that does not match ``SOURCES.sha256`` is
    ``templates_changed``. Nothing is guessed or left out."""
    need_reset, need_events, need_assist = step5_needs(form)
    if not (need_reset or need_events or need_assist):
        return None
    field_name = "reset_policy" if need_reset else "early_check"
    state = step5_state(aeri.templates_root)
    if state["state"] == "absent":
        raise _template_bad(field_name, f"the site has no {STEP5_FILE}")
    if state["state"] != "ok":
        raise Refuse("templates_changed", which=[STEP5_FILE])
    value = state["value"]
    if (
        not isinstance(value, dict)
        or value.get("schema") != STEP5_SCHEMA
        or not set(value) <= {"schema", "reset", "signal_events", "grasp_assist"}
    ):
        raise _template_bad(field_name, f"{STEP5_FILE} is not a {STEP5_SCHEMA} file")
    if need_reset:
        if "reset" not in value:
            raise _template_bad("reset_policy", f"{STEP5_FILE} has no reset block")
        check_step5_reset(value["reset"])
    if need_events:
        if "signal_events" not in value:
            raise _template_bad(
                "early_check", f"{STEP5_FILE} has no signal_events block"
            )
        check_step5_signal_events(value["signal_events"])
    if need_assist:
        if "grasp_assist" not in value:
            raise _template_bad(
                "grasp_assist", f"{STEP5_FILE} has no grasp_assist block"
            )
        check_step5_grasp_assist(value["grasp_assist"])
    return {"sha256": state["sha256"], "value": value}


# --- run names ------------------------------------------------------------------------------


def _listing(folder, pattern) -> set:
    try:
        return {
            entry.name
            for entry in os.scandir(folder)
            if pattern.fullmatch(entry.name.removesuffix(".json"))
        }
    except OSError:
        return set()


def taken_names(aeri, registry, record_names=(), day=None, tag=None) -> dict:
    """``{name: [where, ...]}``: the AERI run names in use, across the runs
    folder (``runs_root``), the AERI runs of the rollout root, the AERI registry
    (``~/.levi-aeri/runs``) and this page's record. ``day`` and ``tag`` narrow
    it to one family."""
    pattern = re.compile(
        "aeri-" + (day or r"\d{8}") + "-" + (tag or "(?:ext|loc)") + r"-r\d+"
    )
    found: dict = {}
    for where, folder in (
        ("runs_root", Path(aeri.runs_root)),
        ("rollout", Path(aeri.rollout_root) / ".aeri" / "runs"),
        ("registry", Path(registry)),
    ):
        for name in _listing(folder, pattern):
            found.setdefault(name.removesuffix(".json"), []).append(where)
    for name in record_names:
        if pattern.fullmatch(name):
            found.setdefault(name, []).append("page")
    return found


def name_where(aeri, registry, name, record_names=()) -> list:
    """Where ``name`` is already used, ``[]`` when nowhere: the same four places,
    asked about one name."""
    where = []
    for place, path in (
        ("runs_root", Path(aeri.runs_root) / name),
        ("rollout", Path(aeri.rollout_root) / ".aeri" / "runs" / name),
        ("registry", Path(registry) / f"{name}.json"),
    ):
        if os.path.lexists(path):
            where.append(place)
    if name in record_names:
        where.append("page")
    return where


def next_name(taken, day, tag) -> str:
    n = 1
    while f"aeri-{day}-{tag}-r{n:02d}" in taken:
        n += 1
    return f"aeri-{day}-{tag}-r{n:02d}"


# --- the form -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Form:
    mode: str
    task: str
    rounds: int
    steps: int
    gripper_confirm_s: float | None
    start_home: bool
    # The reverse loop on the reset policy service (off) and the early done
    # check (None: off), both written into the job only when asked for.
    reset_policy: bool = False
    early_check: object = None  # a subtask_loop.EarlyCheck
    # The bounded takeover before the reset policy (subtask.grasp_assist, the
    # step-5 template's block); needs ``reset_policy``.
    grasp_assist: bool = False
    # Who grasps in the takeover: ``operator`` (keys) or ``auto`` (the wrist camera
    # and the VLM, handed back by the measured holding fact); needs a profile with
    # ``auto_grasp`` and a template with ``grasp_height``.
    grasp_source: str = "operator"
    # subtask.unattended: the inventory taken as the VLM read it and every round
    # released without a key (stops and waits still need a person).
    unattended: bool = False


def _bad(field_name, reason, **extra):
    return Refuse("form_invalid", field=field_name, reason=reason, **extra)


def _early_check(raw, profile):
    """The form's ``early_check`` (None or absent: off; an object: on, each key
    that is left out at ``EARLY_CHECK_FORM``'s value) as a validated
    ``subtask_loop.EarlyCheck``. Refused, not dropped, when the pinned code
    version has no early check."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _bad("early_check", "type")
    if not set(raw) <= set(EARLY_CHECK_FORM):
        raise _bad("early_check", "type")
    if profile.get("early_check") is not True:
        raise _bad("early_check", "unsupported")
    given = {**EARLY_CHECK_FORM, **raw}
    if type(given["on_events"]) is not bool:
        raise _bad("early_check.on_events", "type")
    types = given["event_types"]
    if not isinstance(types, list) or not all(isinstance(t, str) for t in types):
        raise _bad("early_check.event_types", "type")
    if (
        not types
        or len(set(types)) != len(types)
        or not set(types) <= set(EARLY_FORM_EVENT_TYPES)
    ):
        raise _bad("early_check.event_types", "range")
    for name in ("settle_steps", "max_checks"):
        if type(given[name]) is not int:
            raise _bad(f"early_check.{name}", "type")
    if given["every_steps"] is not None and type(given["every_steps"]) is not int:
        raise _bad("early_check.every_steps", "type")
    from levi.automatic.subtask_loop import EarlyCheck

    try:
        return EarlyCheck(**given)
    except ValueError as exc:  # the job's own bounds, and one trigger at least
        named = re.search(r"early_check\.(\w+)", str(exc))
        raise _bad(
            f"early_check.{named.group(1)}" if named else "early_check", "range"
        ) from None


def _whole(value, field_name, bounds):
    if type(value) is not int:  # a bool is an int in Python, and is not a count
        raise _bad(field_name, "type")
    if not bounds["min"] <= value <= bounds["max"]:
        raise _bad(field_name, "range")
    return value


def validate_form(body, aeri, profile) -> Form:
    """The form as a ``Form``, or ``Refuse``: ``form_invalid`` (``field`` and
    ``reason`` say which) or ``start_home_unsupported``."""
    mode = body.get("mode")
    if mode not in MODES:
        raise _bad("mode", "range")
    if mode == "local" and not aeri.local_model:
        raise _bad("mode", "unavailable")
    task = body.get("task")
    if not isinstance(task, str):
        raise _bad("task", "type")
    if not TASK.fullmatch(task):
        raise _bad("task", "pattern")
    # Sent to the policy verbatim: no edge spaces, and some words.
    if task != task.strip() or not re.search(r"[a-z]", task):
        raise _bad("task", "pattern")
    rounds = _whole(body.get("rounds"), "rounds", ROUNDS)
    steps = _whole(body.get("steps"), "steps", STEPS)
    gripper = body.get("gripper_confirm_s")
    if gripper is not None:
        if isinstance(gripper, bool) or not isinstance(gripper, (int, float)):
            raise _bad("gripper_confirm_s", "type")
        gripper = float(gripper)
        if not (
            GRIPPER_CONFIRM_S["min"] <= gripper <= GRIPPER_CONFIRM_S["max"]
        ):  # also false for NaN
            raise _bad("gripper_confirm_s", "range")
    start_home = body.get("start_home")
    if type(start_home) is not bool:
        raise _bad("start_home", "type")
    if start_home and profile.get("start_home") is not True:
        raise Refuse("start_home_unsupported", profile=aeri.profile)
    # The reverse loop on the reset policy (subtask.reverse_policy_role reset,
    # docs/AUTOMATIC_PIPELINE.md): an optional switch, off when absent or false.
    reset_policy = body.get("reset_policy", False)
    if type(reset_policy) is not bool:
        raise _bad("reset_policy", "type")
    # Only a code version that declares the capability (the job keys exist in
    # it) may be asked: the profile table says so, nothing is dropped silently.
    if reset_policy and profile.get("reset_policy") is not True:
        raise _bad("reset_policy", "unsupported")
    early_check = _early_check(body.get("early_check"), profile)
    # The takeover before the reset policy (subtask.grasp_assist): off when absent
    # or false; asked, it needs a code version that has it **and** the reset
    # policy it hands the object to. Refused, never dropped.
    grasp_assist = body.get("grasp_assist", False)
    if type(grasp_assist) is not bool:
        raise _bad("grasp_assist", "type")
    if grasp_assist and profile.get("grasp_assist") is not True:
        raise _bad("grasp_assist", "unsupported")
    if grasp_assist and not reset_policy:
        raise _bad("grasp_assist", "unsupported", detail="needs_reset_policy")
    # RELAXED[2026-11-19] (user: "要充分自动化"): the automatic grasp and the
    # unattended run, each refused (never dropped) by a code version without them.
    grasp_source = body.get("grasp_source", "operator")
    if grasp_source not in ("operator", "auto"):
        raise _bad("grasp_source", "type")
    if grasp_source == "auto":
        if not grasp_assist:
            raise _bad("grasp_source", "unsupported", detail="needs_grasp_assist")
        if profile.get("auto_grasp") is not True:
            raise _bad("grasp_source", "unsupported")
    unattended = body.get("unattended", False)
    if type(unattended) is not bool:
        raise _bad("unattended", "type")
    if unattended and profile.get("auto_grasp") is not True:
        raise _bad("unattended", "unsupported")
    return Form(
        mode,
        task,
        rounds,
        steps,
        gripper,
        start_home,
        reset_policy,
        early_check,
        grasp_assist,
        grasp_source,
        unattended,
    )


# --- the files: new_run.py, as a function ---------------------------------------------------


def render_job(
    template: str, form: Form, name: str, local_model, when, step5=None
) -> str:
    """``job.yaml`` as ``new_run.py`` writes it (only the header line differs).
    ``step5`` (the checked step-5 template, ``require_step5``) is given only for
    a form that asks for the reset policy; an early check needs none for the job."""
    # The page owns these job keys: a template that already carries one would
    # have its value written while RUN.json says the switch is off (or the
    # reverse), so such a template is a changed template, whatever the form asks.
    for key in PAGE_OWNED_SUBTASK_KEYS:
        if re.search(rf"(?m)^  {key}:", template):
            raise Refuse(
                "templates_changed", which=["job.base.yaml"], reason=f"has_{key}"
            )
    m = MODES[form.mode]
    model = m["model"] or local_model
    slug = re.sub(r"[^a-z0-9]+", "_", form.task).strip("_")[:48]
    job = template
    job = re.sub(r"(?m)^  name: .*$", lambda _: f"  name: {name}", job)
    job = re.sub(r"(?m)^  episodes: .*$", lambda _: f"  episodes: {form.rounds}", job)
    job = re.sub(
        r"(?m)^  instruction: .*$", lambda _: f'  instruction: "{form.task}"', job
    )
    job = re.sub(r"(?m)^  max_steps: .*$", lambda _: f"  max_steps: {form.steps}", job)
    job = re.sub(
        r"(?m)^  forward_folder: .*$",
        lambda _: f"  forward_folder: {slug}__{name}",
        job,
    )
    job = re.sub(r"(?m)^    model: .*$", lambda _: f"    model: {model}", job)
    job = re.sub(
        r"(?m)^    served_name: .*$", lambda _: f"    served_name: {m['served']}", job
    )
    job = re.sub(
        r"(?m)^    gpu_memory_utilization: .*$",
        lambda _: (
            f"    gpu_memory_utilization: {m['util']}\n"
            f"    request_timeout_s: {m['timeout']}"
        ),
        job,
    )
    if form.mode == "external":
        # The bridge holds no GPU memory: a 0.01 budget computes to 0, which AERI
        # reads as "unknown" and replaces with the 27B default (23000 MiB), so
        # every wake would be refused (ext-r01, 18 Oct).
        job = re.sub(
            r"(?m)^(  max_retries: .*)$",
            lambda hit: hit.group(1) + "\n  vlm_wake_min_free_gpu_mib: 1",
            job,
        )
    if form.start_home:
        job = re.sub(
            r"(?m)^(  max_retries: .*)$",
            lambda hit: hit.group(1) + "\n  start_home: true",
            job,
        )
    if form.reset_policy:
        job = _with_reset_policy(job, step5["value"]["reset"])
    if form.early_check is not None:
        job = _with_early_check(job, form.early_check)
    if form.grasp_assist:
        block = step5["value"]["grasp_assist"]
        if form.grasp_source == "auto" and "grasp_height" not in block:
            raise _bad("grasp_source", "unsupported", detail="no_grasp_height")
        job = _with_grasp_assist(job, block, form.grasp_source)
    if form.unattended:
        job = re.sub(
            r"(?m)^(  max_retries: .*)$",
            lambda hit: hit.group(1) + "\n  unattended: true",
            job,
        )
    header = (
        f"# {form.mode.upper()} mode run, written by the marvel0 page "
        f"(aeri_prep.py) on {when:%Y-%m-%d %H:%M}\n"
    )
    return header + job


def _insert(job: str, anchor: str, text: str, what: str, *, before=False) -> str:
    """``text`` placed after (or before) the one line of ``job`` that matches
    ``anchor``; a template without exactly one is not the one this was written
    for (``templates_changed``, nothing is guessed)."""
    hits = list(re.finditer(anchor, job))
    if len(hits) != 1:
        raise Refuse("templates_changed", which=["job.base.yaml"], reason=what)
    at = hits[0].start() if before else hits[0].end()
    return job[:at] + text + job[at:]


def _with_reset_policy(job: str, reset: dict) -> str:
    """The job's ``policies.reset.deployment``, ``subtask.reverse_policy_role: reset``
    and ``subtask.reset_prompts`` (the template's sentences, one per list entry)."""
    dep = reset["deployment"]
    block = (
        "  reset:\n    deployment:\n"
        f"      checkpoint_id: {dep['checkpoint_id']}\n"
        f"      config: {dep['config']}\n"
        f"      checkpoint_dir: {dep['checkpoint_dir']}\n"
    )
    job = _insert(job, r"(?m)^task:$", block, "policies", before=True)
    keys = "\n  reverse_policy_role: reset\n  reset_prompts:\n" + "".join(
        f"    - {entry}\n" for entry in reset["prompts"]
    )
    return _insert(job, r"(?m)^  max_retries: .*$", keys.rstrip("\n"), "subtask")


# ``subtask.*`` keys only the form writes (``reset_policy``, ``early_check``,
# ``grasp_assist``): a job template must not hold them.
PAGE_OWNED_SUBTASK_KEYS = (
    "grasp_assist",
    "reverse_policy_role",
    "reset_prompts",
    "early_check",
)


def _yaml_number(value) -> str:
    """A number as the plain scalar of the job: ``repr`` (the shortest text that
    reads back as the same float, ``1e-13`` included; the AERI job parser takes
    the exponent form). ``_yaml_exact`` is the check that it does."""
    if isinstance(value, int):
        return str(value)
    return repr(float(value))


def _yaml_exact(value) -> bool:
    """The rendered scalar, read back by the AERI job's own parser
    (``scene_assessment.parse_document``), is the same number."""
    from levi.automatic import scene_assessment as sa

    try:
        back = sa.parse_document(f"k: {_yaml_number(value)}")["k"]
    except (sa.ContractError, KeyError, ValueError):
        return False
    return type(back) in (int, float) and not isinstance(back, bool) and back == value


def _with_grasp_assist(job: str, block: dict, source: str = "operator") -> str:
    """The job's ``subtask.grasp_assist``: the template's limits and opening bands,
    and the page's own ``enabled: true`` and the form's source: ``operator`` with
    ``handoff: operator``, or ``auto`` with ``handoff: holding_fact`` (the hand-back
    by the measured holding fact, no person) and the template's automatic keys."""
    handoff = "holding_fact" if source == "auto" else "operator"
    lines = [
        "\n  grasp_assist:",
        "    enabled: true",
        f"    grasp_source: {source}",
        f"    handoff: {handoff}",
    ]
    for name in GRASP_ASSIST_KEYS[:-1]:
        lines.append(f"    {name}: {_yaml_number(block[name])}")
    lines.append("    holding_band:")
    lines += [f"      - {entry}" for entry in block["holding_band"]]
    if source == "auto":
        for name in GRASP_ASSIST_AUTO_KEYS:
            if name not in block:
                continue
            value = block[name]
            if name == "grasp_height":
                lines.append("    grasp_height:")
                lines += [f"      - {entry}" for entry in value]
            elif name == "grasp_pixel":
                lines.append(
                    f"    grasp_pixel: [{_yaml_number(value[0])}, "
                    f"{_yaml_number(value[1])}]"
                )
            elif type(value) is bool:
                lines.append(f"    {name}: {'true' if value else 'false'}")
            elif isinstance(value, str):
                lines.append(f"    {name}: {value}")
            else:
                lines.append(f"    {name}: {_yaml_number(value)}")
    return _insert(
        job, r"(?m)^  max_retries: .*$", "\n".join(lines).rstrip("\n"), "subtask"
    )


def _with_early_check(job: str, early) -> str:
    """The job's ``subtask.early_check``: the five keys of the form, the rest at
    AERI's own defaults."""
    lines = ["\n  early_check:", f"    on_events: {str(early.on_events).lower()}"]
    lines += ["    event_types:", *(f"      - {t}" for t in early.event_types)]
    lines.append(f"    settle_steps: {early.settle_steps}")
    if early.every_steps is not None:
        lines.append(f"    every_steps: {early.every_steps}")
    lines.append(f"    max_checks: {early.max_checks}")
    return _insert(
        job, r"(?m)^  max_retries: .*$", "\n".join(lines).rstrip("\n"), "subtask"
    )


def render_binding(
    template: dict, form: Form, run_dir, tools_root, local_model, step5=None
):
    """``binding.json``'s content as ``new_run.py`` writes it. With
    ``form.reset_policy`` the reset endpoint and service block of the step-5
    template are added (``policies.reset``, ``policy_swap.services.reset``);
    with an early check on events its ``signal_events`` declaration."""
    m = MODES[form.mode]
    model = m["model"] or local_model
    b = json.loads(json.dumps(template))
    if form.gripper_confirm_s is not None:
        b["safety"]["gripper_confirm_timeout_s"] = form.gripper_confirm_s
    if form.reset_policy:
        reset = step5["value"]["reset"]
        b["policies"]["reset"] = json.loads(json.dumps(reset["endpoint"]))
        b["policy_swap"]["services"]["reset"] = json.loads(json.dumps(reset["service"]))
    if form.early_check is not None and form.early_check.on_events:
        b["signal_events"] = json.loads(json.dumps(step5["value"]["signal_events"]))
    sv = b["subtask_vlm"]
    sv["model"] = m["served"]
    common = [
        "--host",
        "127.0.0.1",
        "--port",
        str(VLM_PORT),
        "--served-model-name",
        m["served"],
        "--max-model-len",
        "16384",
        "--gpu-memory-utilization",
        m["util"],
    ]
    run_dir, tools = Path(run_dir), Path(tools_root)
    if form.mode == "external":
        sv["service"]["argv"] = [
            BRIDGE_PYTHON,
            str(tools / BRIDGE),
            "serve",
            model,
            *common,
            "--queue",
            str(run_dir / "queue"),
            "--answer-timeout-s",
            "540",
        ]
        sv["service"]["env"] = {"PATH": BRIDGE_ENV_PATH}
    else:
        sv["service"]["argv"] = [
            BRIDGE_PYTHON,
            str(tools / PROXY),
            "serve",
            model,
            *common,
            "--log-dir",
            str(run_dir / "proxy-log"),
            "--max-num-seqs",
            "2",
            "--limit-mm-per-prompt",
            '{"image":64,"video":0}',
            "--default-chat-template-kwargs",
            '{"enable_thinking":false}',
            "--enable-sleep-mode",
        ]
    return b


def execute_argv(aeri, run_dir, plan, binding) -> list:
    """The terminal D command without its ``cd``: the one string a person types
    and the one the page hosts (a test asserts the two are equal). Every path in
    it is of the characters ``shlex`` leaves alone (the site file checks them)."""
    wt = aeri.worktree
    return [
        "env",
        "-u",
        "VIRTUAL_ENV",
        f"PYTHONPATH={wt}",
        "uv",
        "run",
        "--no-sync",
        "--with",
        EXECUTE_OVERLAY,
        "python",
        "-B",
        f"{wt}/scripts/aeri.py",
        "execute",
        "--config",
        f"{run_dir}/job.yaml",
        "--binding",
        f"{run_dir}/binding.json",
        "--expect-plan",
        plan,
        "--expect-binding",
        binding,
    ]


def execute_command(aeri, run_dir, plan, binding) -> str:
    """``cd <openpi> && <argv>``, as ``new_run.py`` prints it."""
    text = shlex.join(execute_argv(aeri, run_dir, plan, binding))
    return f"cd {aeri.openpi_root} && {text}"


# --- the check ------------------------------------------------------------------------------


def run_check(aeri, run_dir, run=run_capture) -> dict:
    """``aeri.py check`` of the pinned worktree on the run's job and binding,
    read-only, ``nice -n 19``, 60 s. Returns ``{"plan_sha256", "binding_sha256",
    "output"}`` or raises ``Refuse("check_failed", reason=...)``."""
    argv = [
        "nice",
        "-n",
        "19",
        aeri.levi_python,
        "scripts/aeri.py",
        "check",
        "--config",
        f"{run_dir}/job.yaml",
        "--binding",
        f"{run_dir}/binding.json",
    ]
    env = {
        "PYTHONPATH": aeri.worktree,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PATH": CHECK_ENV_PATH,
    }
    try:
        done = run(argv, cwd=aeri.worktree, env=env, timeout=CHECK_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise Refuse("check_failed", reason="timeout") from None
    except (OSError, subprocess.SubprocessError, ValueError):
        raise Refuse("check_failed", reason="not_run") from None
    tail = scrub((done.stdout + done.stderr).strip())[-CHECK_TAIL_CHARS:]
    if done.returncode != 0:
        raise Refuse(
            "check_failed", reason="returncode", returncode=done.returncode, tail=tail
        )
    try:
        value = json.loads(done.stdout)
    except ValueError:
        raise Refuse("check_failed", reason="output", tail=tail) from None
    if not isinstance(value, dict):
        raise Refuse("check_failed", reason="output", tail=tail)
    digests = {}
    for key in ("plan_sha256", "binding_sha256"):
        text = value.get(key)
        if not isinstance(text, str) or not re.fullmatch(r"[0-9a-f]{64}", text):
            raise Refuse("check_failed", reason="digest", tail=tail)
        digests[key] = text
    if value.get("reset_strategy") != "subtask_loop":
        raise Refuse("check_failed", reason="strategy", tail=tail)
    if value.get("hardware_verified") is not False:
        raise Refuse("check_failed", reason="hardware_verified", tail=tail)
    return {**digests, "output": done.stdout + done.stderr}


# --- the state folder -----------------------------------------------------------------------


class StateRoot:
    """What ``state_dir`` takes besides a ``Workspace``: anything with ``.state``."""

    def __init__(self, state):
        self.state = Path(state)


def state_dir(aeri, workspace=None, create=True) -> Path:
    """``<workspace state>/marvel0/aeri_prep`` (``0700``, no link in it, not
    inside source data), like the deploy and run folders next to it."""
    base = (workspace or Workspace.at(paths.workspace())).state
    target = base / Path(*REL)
    if record.UNSAFE.search(str(target)):
        raise record.StateDirUnusable("the folder path has characters systemd misreads")
    avoid = ()
    if aeri is not None:
        avoid = (aeri.rollout_root, aeri.openpi_root, aeri.runs_root)
    for root in avoid:
        if target.is_relative_to(Path(root).expanduser().resolve()):
            raise record.StateDirUnusable("the folder would lie inside source data")
    if create:
        base.mkdir(parents=True, exist_ok=True)
    for folder in (target.parent, target, target / "requests"):
        record._check_dir(folder, create)
    return target


def read_page_record(directory):
    """The newest prepared run this page knows (``record.json``), or None."""
    try:
        value = record.read_record(directory)
    except record.RecordCorrupt:
        return None
    if value is None or value.get("schema") != RECORD_SCHEMA:
        return None
    name = value.get("name")
    return value if isinstance(name, str) and NAME.fullmatch(name) else None


def audit(directory, event, now) -> None:
    """One history line: the person, through the product, what and when. No
    output text, no path. Never raises."""
    line = {
        "time": record.iso(now),
        "actor": "person",
        "via": "product",
        **{k: v for k, v in event.items() if v is not None},
    }
    with contextlib.suppress(Exception):
        jsonio.append_line(
            Path(directory) / record.HISTORY,
            line,
            max_bytes=HISTORY_MAX_BYTES,
            keep=1,
        )


# --- the prepared run -----------------------------------------------------------------------


def _sha256_file(path) -> str | None:
    blob = _read_file(path, 1 << 20)
    return None if blob is None else hashlib.sha256(blob).hexdigest()


def read_prepared(aeri, name, head=None) -> dict | None:
    """The prepared run ``name`` as the document shows it, with ``intact`` and
    the ``problems`` found by reading it again (RUN.json, the two files'
    digests, the AERI head, the profile), or None when there is no such folder.
    ``head`` is the worktree's current HEAD (None when unknown: not compared)."""
    if not isinstance(name, str) or not NAME.fullmatch(name):
        return None
    run_dir = Path(aeri.runs_root) / name
    try:
        if not stat.S_ISDIR(os.lstat(run_dir).st_mode):
            return None
    except OSError:
        return None
    problems = []
    blob = _read_file(run_dir / "RUN.json", RUN_JSON_MAX_BYTES)
    try:
        run_json = json.loads(blob) if blob is not None else None
    except ValueError:
        run_json = None
    if not isinstance(run_json, dict) or run_json.get("name") != name:
        return {
            "name": name,
            "intact": False,
            "problems": ["run_json_missing"],
            **{
                k: None
                for k in (
                    "mode",
                    "task",
                    "rounds",
                    "steps",
                    "gripper_confirm_s",
                    "start_home",
                    "reset_policy",
                    "grasp_assist",
                    "early_check",
                    "profile",
                    "aeri_head",
                    "prepared_at",
                    "plan_sha256",
                    "binding_sha256",
                    "files_sha256",
                    "execute",
                    "confirm",
                )
            },
        }
    recorded = run_json.get("files_sha256")
    names = {"job": "job.yaml", "binding": "binding.json"}
    if not (
        isinstance(recorded, dict)
        and all(
            isinstance(recorded.get(k), str)
            and re.fullmatch(r"[0-9a-f]{64}", recorded[k])
            for k in names
        )
    ):
        problems.append("digest_missing")
    else:
        for key, file in names.items():
            if _sha256_file(run_dir / file) != recorded[key]:
                problems.append("files_changed")
                break
    for key in ("plan_sha256", "binding_sha256"):
        text = run_json.get(key)
        if not isinstance(text, str) or not re.fullmatch(r"[0-9a-f]{64}", text):
            problems.append("digest_missing")
            break
    seen = run_json.get("aeri_head")
    if head is not None and not (isinstance(seen, str) and head.startswith(seen)):
        problems.append("code_changed")
    if run_json.get("profile") != aeri.profile:
        problems.append("profile_changed")
    return {
        "name": name,
        "mode": run_json.get("mode"),
        "task": run_json.get("task"),
        "rounds": run_json.get("rounds"),
        "steps": run_json.get("steps"),
        "gripper_confirm_s": run_json.get("gripper_confirm_s"),
        "start_home": run_json.get("start_home"),
        "reset_policy": run_json.get("reset_policy") is True,
        "grasp_assist": run_json.get("grasp_assist") is True,
        "early_check": run_json.get("early_check")
        if isinstance(run_json.get("early_check"), dict)
        else None,
        "profile": run_json.get("profile"),
        "aeri_head": seen,
        "prepared_at": run_json.get("prepared_at"),
        "plan_sha256": run_json.get("plan_sha256"),
        "binding_sha256": run_json.get("binding_sha256"),
        "files_sha256": recorded if isinstance(recorded, dict) else None,
        "intact": not problems,
        "problems": sorted(set(problems)),
        "execute": run_json.get("execute"),
        "confirm": run_json.get("confirm"),
    }


# --- prepare --------------------------------------------------------------------------------


def _write_new(path: Path, text: str) -> None:
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o644,
    )
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def _remove_made(run_dir: Path, runs_root: Path) -> None:
    """Remove the folder this call made, and only that: a plain directory directly
    under the runs folder whose name is one of ours."""
    try:
        info = os.lstat(run_dir)
    except OSError:
        return
    if (
        stat.S_ISDIR(info.st_mode)
        and run_dir.parent == runs_root
        and NAME.fullmatch(run_dir.name)
    ):
        shutil.rmtree(run_dir, ignore_errors=True)


def prepare(aeri, body, host: Host, page_dir: Path) -> dict:
    """Prepare one run from the form in ``body``. Returns the document's
    ``prepared`` value; raises ``Refuse``. Writes only the new run folder and the
    page's record; a failure leaves no folder behind."""
    profile = profile_info(aeri.profile)
    if profile["known"] is not True:
        raise Refuse("profile_unknown", profile=aeri.profile)
    form = validate_form(body, aeri, profile)
    state = require_code_pin(aeri, host.run)
    templates = require_templates(aeri)
    step5 = require_step5(aeri, form)
    if form.reset_policy:
        check_reset_port(json.loads(templates["data"]["binding.base.json"]), step5)
    runs_root = Path(aeri.runs_root)
    if not runs_root.is_dir():
        raise Refuse("aeri_unconfigured", errors=[], reason="runs_root")
    page_record = read_page_record(page_dir)
    record_names = [page_record["name"]] if page_record else []
    when = datetime.datetime.fromtimestamp(host.now(), tz=datetime.UTC).astimezone()
    day = when.strftime("%Y%m%d")
    tag = MODES[form.mode]["tag"]
    registry = host.registry()
    run_dir = None
    for _ in range(NAME_TRIES):
        taken = taken_names(aeri, registry, record_names, day, tag)
        name = next_name(taken, day, tag)
        candidate = runs_root / name
        try:
            os.mkdir(candidate, 0o755)
        except FileExistsError:
            record_names = [*record_names, name]
            continue
        except OSError:
            raise Refuse("aeri_unconfigured", errors=[], reason="runs_root") from None
        run_dir = candidate
        break
    if run_dir is None:
        raise Refuse("run_name_taken", name=name, where=taken.get(name, ["runs_root"]))
    try:
        return _prepare_into(
            aeri, form, name, run_dir, state, templates, step5, host, page_dir, when
        )
    except BaseException:
        _remove_made(run_dir, runs_root)
        raise


def _prepare_into(
    aeri, form, name, run_dir, state, templates, step5, host, page_dir, when
):
    tools = aeri.tools_root
    job_text = render_job(
        templates["data"]["job.base.yaml"].decode(),
        form,
        name,
        aeri.local_model,
        when,
        step5,
    )
    binding = render_binding(
        json.loads(templates["data"]["binding.base.json"]),
        form,
        run_dir,
        tools,
        aeri.local_model,
        step5,
    )
    binding_text = json.dumps(binding, indent=2) + "\n"
    _write_new(run_dir / "job.yaml", job_text)
    _write_new(run_dir / "binding.json", binding_text)
    if form.mode == "external":
        os.mkdir(run_dir / "queue", 0o755)
    checked = run_check(aeri, run_dir, host.run)
    _write_new(run_dir / "check.json", checked["output"])
    head = state["head"][:8]
    command = execute_command(
        aeri, run_dir, checked["plan_sha256"], checked["binding_sha256"]
    )
    files = {
        "job": hashlib.sha256(job_text.encode()).hexdigest(),
        "binding": hashlib.sha256(binding_text.encode()).hexdigest(),
    }
    prepared_at = record.iso(host.now())
    run_json = {
        "name": name,
        "mode": form.mode,
        "task": form.task,
        "rounds": form.rounds,
        "steps": form.steps,
        "aeri_head": head,
        "plan_sha256": checked["plan_sha256"],
        "binding_sha256": checked["binding_sha256"],
        "execute": command,
        "confirm": f"execute {name}",
        "profile": aeri.profile,
        "start_home": form.start_home,
        "gripper_confirm_s": form.gripper_confirm_s,
        "prepared_at": prepared_at,
        "prepared_by": "marvel0-page",
        "files_sha256": files,
        "templates_sha256": {
            "job": templates["digests"]["job.base.yaml"],
            "binding": templates["digests"]["binding.base.json"],
        },
    }
    # What a default form leaves out stays out: RUN.json of a run that asks for
    # neither is what it was before these keys existed.
    if form.reset_policy:
        run_json["reset_policy"] = True
    if form.early_check is not None:
        run_json["early_check"] = form.early_check.describe()
    if form.grasp_assist:
        run_json["grasp_assist"] = True
    if step5 is not None:
        run_json["templates_sha256"]["step5"] = step5["sha256"]
    # RUN.json goes last: a folder without it is not a prepared run.
    _write_new(run_dir / "RUN.json", json.dumps(run_json, indent=1) + "\n")
    record.write_record(
        page_dir,
        {
            "schema": RECORD_SCHEMA,
            "name": name,
            "prepared_at": prepared_at,
            "mode": form.mode,
            "files_sha256": files,
        },
    )
    prepared = read_prepared(aeri, name, state["head"])
    if prepared is None:  # cannot happen: it was just written
        raise Refuse("check_failed", reason="unreadable")
    return prepared
