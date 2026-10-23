"""Deploy, watch and stop the policy server from the marvel0 page.

What the page may do is deliberately small: start **one** policy server as a
transient ``systemd --user`` unit it names itself
(``levi-aeri-policy-manual-marvel0-<model>``), say what state that server is in,
and stop that unit. The arm controller, the robot server and the gripper stack
are started and stopped by ``stack_act.py`` and the demo client by ``run.py``,
never from here, and nothing here is an emergency stop: a hand stays on the
physical one.

The rules that hold everywhere below:

* **Passive.** Whether something listens on the policy port comes from the
  kernel's socket tables (``levi.marvel0.net``); whether the server is ready comes
  from what it wrote to its log. Nothing connects to the port, nothing is
  signalled, killed or looked up by process id, name or port by this module
  (``systemctl --user stop`` makes systemd send the unit's own ``KillSignal``,
  SIGINT, and end it after ``TimeoutStopSec``), and no ``/proc/<pid>`` of
  anybody else is read. The only things asked of the system
  are ``systemctl --user``, ``systemd-run --user`` and ``nvidia-smi``'s
  aggregate memory, through one injectable runner (``Host.run``).
* **One server.** A deploy is refused while anything listens on the port (a
  server started by hand is never touched: the person presses Ctrl+C in its
  terminal), while any ``levi-aeri-policy-*`` unit exists in any state, while
  the arm lock is held, while a standalone AERI run lives, or when the GPU
  lock has a holder that is not LEVI's own live model server.
* **Never under a client.** Neither a deploy nor a stop goes on while a
  connection to the port is established (a stop may be forced with a typed
  phrase); the stop checks twice, 0.3 s apart.
* **Only our unit.** A stop names the unit recorded at deploy time, after
  ``systemctl show`` reports that very ``Id`` and ``ExecStart``.
* **Written before done.** The deploy record is written before
  ``systemd-run`` runs, and every request is stored, so a request sent again
  is answered and never acted on twice, also after a restart.
* **Every state is derived** (``derive``) from the unit's state, the socket
  tables, the log and the record; a source that cannot be read gives
  ``unknown``, never ``idle``.

All of the machine is reached through ``Host``, which tests replace whole.
"""

import contextlib
import copy
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from levi.automatic.campaign.switch import CGROUP_ROOT, ProcView, exec_argv
from levi.live import gpumgr
from levi.own_units import OwnUnits
from levi.setup import probes

from . import inference_mode, net, record
from .errors import fail

# --- refusals ------------------------------------------------------------------------

# code -> (HTTP status, message). The messages hold no path; a few take the
# facts of the refusal (``Refuse.extra``) through ``{}`` fields.
REFUSALS = {
    "inference_mode_invalid": (422, "Choose plain, rtc or vlash"),
    "inference_mode_conflict": (422, "The RTC flag conflicts with inference_mode"),
    "invalid_type": (422, "The inference mode flags have invalid types"),
    "vlash_overlap_out_of_range": (
        422,
        "VLASH overlap must be an integer greater than zero and below action_horizon",
    ),
    "vlash_overlap_not_allowed": (422, "VLASH overlap belongs only to VLASH"),
    "vlash_model_incompatible": (
        422,
        "VLASH requires an audited state-conditioned FR3 absolute pose/gripper config",
    ),
    "request_id_invalid": (
        400,
        "request_id is 8-80 characters of A-Z, a-z, 0-9 and . _ : -",
    ),
    "confirm_required": (400, "Type the confirmation phrase exactly: {phrase}"),
    "model_unknown": (422, "That model is not in the validated catalog"),
    "fraction_not_allowed": (
        422,
        "That VRAM fraction is not one of the presets of the site file",
    ),
    "cfg_scale_not_allowed": (
        422,
        "A guidance scale belongs only to a model that uses one",
    ),
    "cfg_scale_out_of_range": (422, "The guidance scale is out of range"),
    "unverified_not_accepted": (
        409,
        "This model has no recorded real-GPU serving check; accept that to deploy it",
    ),
    "start_unconfirmed": (
        409,
        (
            "An earlier start did not answer in time and may still create its unit; "
            "wait until the state shows it or has settled, then try again"
        ),
    ),
    "busy": (423, "Another deploy or stop is running"),
    "request_id_used": (409, "That request id was already used for another request"),
    "state_dir_unusable": (412, "The folder for the deploy record cannot be used"),
    "checkpoint_invalid": (
        412,
        (
            "The checkpoint folder is missing, lies outside the checkpoints folder, "
            "or has no params/ or no top-level norm_stats.json"
        ),
    ),
    "runtime_files_missing": (
        412,
        "The Python of the openpi checkout or the serving script is missing",
    ),
    "source_file_missing": (412, "A source file listed in the site file is missing"),
    "source_changed": (
        409,
        "A source file differs from the manifest named in the site file",
    ),
    "net_table_unreadable": (
        503,
        "The socket tables could not be read, so the port is not assumed free",
    ),
    "port_taken": (
        409,
        (
            "Something already listens on port {port}. If it is a server started by "
            "hand, press Ctrl+C in the terminal that runs it"
        ),
    ),
    "already_running": (
        409,
        "The policy server of this page is already running; stop it first",
    ),
    "client_connected": (
        409,
        "A client is connected to the policy port; end it first",
    ),
    "systemd_unreadable": (
        503,
        "systemd could not be asked, so nothing is assumed stopped",
    ),
    "policy_unit_exists": (
        409,
        "A policy server unit already exists (in any state); only one may",
    ),
    "arm_lock_held": (409, "The arm lock is held: a run is using the arm"),
    "arm_lock_unknown": (
        503,
        "Who holds the arm lock could not be read, so it is not assumed free",
    ),
    "aeri_run_live": (409, "A standalone AERI run is registered and alive"),
    "aeri_registry_unreadable": (
        503,
        "The AERI run registry could not be read, so no run is ruled out",
    ),
    "gpu_lock_foreign": (
        409,
        "The GPU lock is held by something other than LEVI's own live model server",
    ),
    "env_unreadable": (
        503,
        (
            "The user manager's environment could not be read, so ROS variables "
            "cannot be ruled out of the unit"
        ),
    ),
    "gpu_unreadable": (503, "The GPU's memory use could not be read"),
    "gpu_insufficient": (
        409,
        (
            "Fraction {fraction:.2f} of the {total_mib} MiB card needs {required_mib} "
            "MiB free ({reserved_mib} + {margin_mib}); {free_mib} MiB are free. "
            "Put the live vLLM to sleep or stop it (/live), then try again."
        ),
    ),
    "start_failed": (502, "systemd-run could not start the unit; nothing runs"),
    "nothing_to_stop": (409, "This page has no policy server to stop"),
    "foreign_listener_not_stoppable": (
        409,
        (
            "The listener is not a unit of this page. This page does not touch it: "
            "press Ctrl+C in the terminal that runs it"
        ),
    ),
    "unit_mismatch": (
        409,
        (
            "The unit systemd reports is not the one recorded at deploy time; "
            "nothing was stopped"
        ),
    ),
    "stop_failed": (502, "systemctl could not stop the unit"),
    "stop_incomplete": (
        502,
        "The unit did not finish stopping. Run 'systemctl --user stop {unit}' yourself",
    ),
}
UNREADABLE = frozenset(c for c, (status, _) in REFUSALS.items() if status == 503)


class Refuse(Exception):
    """A deploy or stop that does not go on: a stable ``code``, its HTTP
    ``status``, an English ``message`` and the facts as ``extra``."""

    def __init__(self, code, message=None, **extra):
        status, text = REFUSALS[code]
        self.code = code
        self.status = status
        self.extra = extra
        if message is None:
            try:
                message = text.format(**extra)
            except (KeyError, IndexError):
                message = text
        self.message = message
        super().__init__(f"{code}: {message}")


# --- the host ------------------------------------------------------------------------

ALLOWED_COMMANDS = ("systemctl", "systemd-run", "nvidia-smi")
# Exit codes of run_command for a command that did not run to its end: as
# timeout(1) says them. A timed-out systemd-run may still have started its unit.
TIMED_OUT = 124
COULD_NOT_RUN = 125
HASH_MAX_BYTES = 8 << 20
MANIFEST_MAX_BYTES = 1 << 20


def run_command(argv, timeout_s) -> tuple:
    """``(exit code, text)`` of one command, without a shell. ``125`` when it
    cannot run, ``124`` when it does not finish in time. The text is stdout; a command that
    failed has its stderr appended, because that is where systemd-run says
    which property it did not accept."""
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except subprocess.TimeoutExpired:
        return TIMED_OUT, "did not finish in time"
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return COULD_NOT_RUN, str(exc)
    text = done.stdout or ""
    if done.returncode != 0 and done.stderr:
        text += done.stderr
    return done.returncode, text


def guarded(run) -> Callable:
    """``run`` that refuses everything but ``systemctl --user``,
    ``systemd-run --user`` and ``nvidia-smi``: whatever a bug builds, no other
    command (a ``kill``, a ``pkill``, an ``ss``, a shell) can be run here."""

    def call(argv, timeout_s):
        command = argv[0] if argv else None
        if command not in ALLOWED_COMMANDS or (
            command != "nvidia-smi" and "--user" not in argv[1:3]
        ):
            raise ValueError(f"not a command this page may run: {command!r}")
        return run(argv, timeout_s)

    return call


def _gpu_lock(path):
    return gpumgr.GpuLock(path, "marvel0-deploy", allow_unavailable=False)


def _aeri_runs() -> list:
    from levi.automatic import launch

    return launch.runs()


def _hosted_aeri():
    """The AERI terminal D that this page hosts as a job row (``id``), or None.
    It is in no registry before its assembly, and the arm lock is its own only
    after its phrase: from the click that starts it, only the page knows."""
    from . import aeri_run_api

    return aeri_run_api.active()


def _robot_context() -> dict:
    """What the keep-a-hand-on-the-e-stop notice needs: the arm-controller
    marker, and whether an evaluation session has not ended (None: unknown)."""
    from . import api, status

    now = time.time()
    marker = status.marker_part(api.marker_path(), now)["state"]
    roots, why = api.rollout_roots()
    part = status.sessions_part(roots, now, why)
    open_session = None
    if part.get("state") == "ok":
        open_session = any(
            row.get("status") not in status.ENDED_STATES
            for row in part.get("sessions") or []
        )
    return {"marker": marker, "open_session": open_session}


def _read_file(path, limit=MANIFEST_MAX_BYTES) -> bytes:
    data, truncated, _ = probes.read_regular(path, limit)
    if truncated:
        raise OSError("file too large")
    return data


def _hash_file(path) -> str:
    return hashlib.sha256(_read_file(path, HASH_MAX_BYTES)).hexdigest()


@dataclass
class Host:
    """Everything the deploy reaches outside Python. Tests build one from
    fakes; ``production_host()`` builds the real one."""

    run: Callable  # (argv, timeout_s) -> (exit code, text)
    proc_net: Path | None = None  # None: /proc/net
    proc_root: Path | None = None  # None: /proc (only our own units' pids are opened)
    cgroup_root: str = CGROUP_ROOT
    locks_path: str = "/proc/locks"
    gpu_lock: Callable = _gpu_lock
    aeri_runs: Callable = _aeri_runs
    hosted_aeri: Callable = lambda: _hosted_aeri()
    robot_context: Callable = _robot_context
    hash_file: Callable = _hash_file
    read_file: Callable = _read_file
    monotonic: Callable = time.monotonic
    now: Callable = time.time
    sleep: Callable = time.sleep
    # Run the lock watcher in a thread of its own (tests call it by hand).
    background: bool = False


def production_host() -> Host:
    return Host(run=guarded(run_command), background=True)


# --- the GPU -------------------------------------------------------------------------

SMI = [
    "nvidia-smi",
    "--query-gpu=memory.used,memory.total",
    "--format=csv,noheader,nounits",
]
SMI_TIMEOUT_S = 10


@dataclass(frozen=True)
class Gpu:
    total_mib: int
    used_mib: int
    free_mib: int


def read_gpu(run) -> Gpu:
    """The card's memory in use and in total, from the one aggregate line
    ``nvidia-smi`` gives. Anything else is ``gpu_unreadable``; no process is
    asked about."""
    code, text = run(SMI, SMI_TIMEOUT_S)
    lines = [line for line in text.splitlines() if line.strip()]
    if code != 0 or len(lines) != 1:
        raise Refuse("gpu_unreadable")
    fields = [part.strip() for part in lines[0].split(",")]
    if len(fields) != 2 or not all(re.fullmatch(r"\d+", f) for f in fields):
        raise Refuse("gpu_unreadable")
    used, total = int(fields[0]), int(fields[1])
    if total <= 0 or used > total:
        raise Refuse("gpu_unreadable")
    return Gpu(total, used, total - used)


def reserved_mib(fraction, total_mib) -> int:
    """What a server started with ``XLA_PYTHON_CLIENT_MEM_FRACTION=fraction``
    reserves."""
    return math.ceil(round(fraction * total_mib, 6))


def required_mib(fraction, total_mib, margin_mib) -> int:
    """The free memory a start needs: the reserved share plus a margin."""
    return reserved_mib(fraction, total_mib) + margin_mib


# --- the unit ------------------------------------------------------------------------

PORT_FLAG = "--port"
CFG_FLAG = "--cfg-guidance-scale"
UNSET_NAMES = re.compile(
    r"AMENT_.*|ROS_.*|RMW_.*|COLCON_.*|CMAKE_PREFIX_PATH|CONDA_.*|VIRTUAL_ENV"
    r"|PYTHONPATH|PYTHONHOME"
)
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
FIXED_ENV = {"PYTHONUNBUFFERED": "1", "no_proxy": "localhost,127.0.0.1,::1"}
SHOW_ENVIRONMENT = ["systemctl", "--user", "show-environment"]


def serve_script(rtc, mode=None) -> str:
    if mode == "vlash":
        return inference_mode.VLASH_SCRIPT
    return "scripts/serve_policy_rtc.py" if rtc else "scripts/serve_policy.py"


def server_argv(
    site, model, rtc, cfg_scale, checkpoint_dir, *, mode=None, vlash_overlap_steps=None
) -> list:
    """The command the unit runs: the checkout's own Python (not ``uv run``),
    the script relative to the unit's working directory, the top-level flags
    before ``policy:checkpoint`` (tyro), the checkpoint by absolute path. The
    guidance flag goes with a model that uses one, always, and with no other.
    No token holds a character systemd would expand or split on."""
    if bool(model.cfg_capable) != (cfg_scale is not None):
        raise ValueError("a guidance scale goes with a CFG model and with no other")
    mode = inference_mode.resolve(
        {"rtc": rtc, "inference_mode": mode}, default_rtc=False
    )
    if mode == "vlash" and not inference_mode.capable(model.config):
        raise ValueError("VLASH requires an audited FR3 state/action contract")
    argv = [site.python, serve_script(rtc, mode), PORT_FLAG, str(site.port)]
    if mode == "vlash":
        argv += [
            "--vlash-overlap-steps",
            str(inference_mode.overlap(vlash_overlap_steps)),
        ]
    if cfg_scale is not None:
        argv += [CFG_FLAG, str(float(cfg_scale))]
    argv += [
        "policy:checkpoint",
        "--policy.config",
        model.config,
        "--policy.dir",
        str(checkpoint_dir),
    ]
    if any(record.UNSAFE.search(token) for token in argv):
        raise ValueError("a token of the server command has an unsafe character")
    return argv


@dataclass(frozen=True)
class UnitEnv:
    unset: list  # names taken out of the user manager's environment
    setenv: dict  # name -> value the unit gets


def _unquote(value) -> str:
    if value.startswith("$'") and value.endswith("'") and len(value) >= 3:
        return value[2:-1]
    if value.startswith("'") and value.endswith("'") and len(value) >= 2:
        return value[1:-1]
    return value


def _manager_env(text) -> dict:
    out = {}
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep and ENV_NAME.fullmatch(name):
            out[name] = _unquote(value)
    return out


def _under(entry, prefixes) -> bool:
    return any(
        entry == p.rstrip("/") or entry.startswith(p.rstrip("/") + "/")
        for p in prefixes
    )


def unit_env(manager_env_text, site) -> UnitEnv:
    """What the unit's environment must lose. A ``systemd --user`` unit starts
    with the user manager's environment, not the caller's, and the policy
    server terminal has no ROS and no conda: those variables are unset, the
    library path loses its ROS entries (and any entry systemd would expand),
    and three fixed variables are set."""
    env = _manager_env(manager_env_text)
    unset = sorted(name for name in env if UNSET_NAMES.fullmatch(name))
    setenv = dict(FIXED_ENV)
    if "LD_LIBRARY_PATH" in env:
        entries = [e for e in env["LD_LIBRARY_PATH"].split(":") if e]
        kept = [
            e
            for e in entries
            if not _under(e, site.ros_prefixes) and not record.UNSAFE.search(e)
        ]
        if len(kept) != len(entries):
            if kept:
                setenv["LD_LIBRARY_PATH"] = ":".join(kept)
            else:
                unset.append("LD_LIBRARY_PATH")
    unset.sort()
    return UnitEnv(unset, setenv)


def systemd_run_argv(
    site,
    unit,
    model,
    rtc,
    fraction,
    cfg_scale,
    log_path,
    env,
    checkpoint_dir,
    *,
    mode=None,
    vlash_overlap_steps=None,
) -> list:
    """The ``systemd-run --user`` command for the unit; ``exec_argv`` is the
    part after ``--``. ``KillSignal=SIGINT`` is the Ctrl+C of the terminal; the
    unit has no restart, no stop command and no ROS."""
    run = [
        "systemd-run",
        "--user",
        "--quiet",
        f"--unit={unit}",
        f"--description=LEVI marvel0 policy server {model.id}",
        "--collect",
        f"--working-directory={site.openpi_checkout}",
        "--property=KillMode=control-group",
        "--property=KillSignal=SIGINT",
        f"--property=TimeoutStopSec={site.unit.stop_timeout_s}",
        "--property=Restart=no",
        f"--property=Nice={site.unit.nice}",
        f"--property=CPUAffinity={site.unit.cpu_affinity}",
    ]
    if site.unit.cpu_quota_percent > 0:
        run.append(f"--property=CPUQuota={site.unit.cpu_quota_percent}%")
    run += [
        f"--property=StandardOutput=append:{log_path}",
        f"--property=StandardError=append:{log_path}",
    ]
    if env.unset:
        run.append("--property=UnsetEnvironment=" + " ".join(env.unset))
    run.append(f"--setenv=XLA_PYTHON_CLIENT_MEM_FRACTION={float(fraction)!r}")
    run += [f"--setenv={name}={value}" for name, value in env.setenv.items()]
    run.append("--")
    run += server_argv(
        site,
        model,
        rtc,
        cfg_scale,
        checkpoint_dir,
        mode=mode,
        vlash_overlap_steps=vlash_overlap_steps,
    )
    return run


def parse_show(text) -> dict:
    """``Key=Value`` lines of ``systemctl show`` (the last of a key wins)."""
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key:
            out[key] = value
    return out


def same_unit(show_text, rec) -> bool:
    """Does ``systemctl show`` report the unit this record deployed: the same
    ``Id`` and exactly the same command (not merely the same config)?"""
    unit, wanted = rec.get("unit"), rec.get("exec_argv")
    if not unit or not wanted:
        return False
    shown = parse_show(show_text)
    if shown.get("Id") != f"{unit}.service":
        return False
    return exec_argv(shown.get("ExecStart", "")) == wanted


# --- the log -------------------------------------------------------------------------

HEAD_BYTES = 256 * 1024
TAIL_BYTES = 256 * 1024
LINE_CHARS = 300
TAIL_LINES = 40
MAX_PAGE_LINES = 200
# The lines a ready server wrote (serve_policy.py, rtc_policy.py,
# serve_policy_rtc.py, policy_config.py and the websockets server).
READY_RE = {
    "vlash": re.compile(r"VLASH enabled: protocol=1(?:\s|$|,)"),
    "norm": re.compile(r"Using norm stats shipped with the checkpoint: (\S+)"),
    "rtc": re.compile(
        r"RTC enabled: max_guidance_weight=\S+, schedule=\S+, (plain|advantage-CFG) policy"
    ),
    "warmup": re.compile(r"RTC warm-up: plain \d+ ms, rtc \d+ ms"),
    "warmup_skipped": re.compile(r"RTC warm-up skipped"),
    "cfg": re.compile(r"Advantage-CFG policy, guidance scale w=(\S+)"),
    "listening": re.compile(r"server listening on 0\.0\.0\.0:(\d+)"),
}
# In the order they win over each other.
FAIL_RE = (
    # Only the fatal markers. XLA's allocator also logs "ran out of memory trying
    # to allocate ... this is not a failure" and carries on, so a bare "out of
    # memory" is not an out-of-memory failure.
    (
        "xla_oom",
        re.compile(r"RESOURCE_EXHAUSTED|XlaRuntimeError.*(?:OOM|memory)"),
    ),
    (
        "argparse",
        re.compile(
            r"^usage: |error: (?:unrecognized arguments|the following arguments are "
            r"required|argument )|Unrecognized (?:options|arguments)|Required options"
        ),
    ),
    ("address_in_use", re.compile(r"Address already in use|\[Errno 98\]")),
    ("traceback", re.compile(r"^Traceback \(most recent call last\)")),
)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_ABSOLUTE = re.compile(
    r"(?<![\w/.:-])/(?:[A-Za-z0-9_.~+@%=,-]+/)+([A-Za-z0-9_.~+@%=,-]+)"
)


def _printable(data, *, truncate=True) -> list:
    out = []
    for line in data.decode("utf-8", "replace").splitlines():
        line = _CONTROL.sub("", _ANSI.sub("", line))
        if line.strip():
            out.append(line[:LINE_CHARS] if truncate else line)
    return out


def _read_lines(path, limit, tail, *, truncate=True) -> list:
    # read_regular decides from lstat, so a link or a FIFO is never opened.
    try:
        data, _, _ = probes.read_regular(path, limit, tail=tail)
    except OSError:
        return []
    return _printable(data, truncate=truncate)


def read_head(path, limit=HEAD_BYTES) -> list:
    """The first ``limit`` bytes of the log as printable lines. The startup
    lines are here; on a long run the connection lines push them out of the
    tail."""
    return _read_lines(path, limit, False)


def read_tail(path, limit=TAIL_BYTES) -> list:
    return _read_lines(path, limit, True)


@dataclass(frozen=True)
class Scan:
    norm: bool = False  # the norm stats of THIS checkpoint
    rtc_enabled: bool = False
    vlash_enabled: bool = False
    warmup: bool = False
    warmup_skipped: bool = False
    cfg: bool = False
    listening: bool = False  # "server listening" on the deploy port
    failure: str | None = None
    tail: list = field(default_factory=list)


def scan(path, checkpoint_dir, port) -> Scan:
    """What the log says: the readiness lines (head and tail), a classified
    failure (xla_oom, argparse, address_in_use, traceback) and the last
    printable lines."""
    # Readiness needs the complete checkpoint path; the page's line limit
    # must not truncate evidence. Both reads retain their existing byte caps.
    head = _read_lines(path, HEAD_BYTES, False, truncate=False)
    tail = _read_lines(path, TAIL_BYTES, True, truncate=False)
    lines = head + tail
    wanted = str(checkpoint_dir).rstrip("/") + "/norm_stats.json"

    def seen(name, check=None):
        for line in lines:
            found = READY_RE[name].search(line)
            if found and (check is None or check(found)):
                return True
        return False

    failure = next(
        (
            kind
            for kind, pattern in FAIL_RE
            if any(pattern.search(line) for line in lines)
        ),
        None,
    )
    return Scan(
        norm=seen("norm", lambda m: m.group(1) == wanted),
        rtc_enabled=seen("rtc"),
        vlash_enabled=seen("vlash"),
        warmup=seen("warmup"),
        warmup_skipped=seen("warmup_skipped"),
        cfg=seen("cfg"),
        listening=seen("listening", lambda m: int(m.group(1)) == port),
        failure=failure,
        tail=[line[:LINE_CHARS] for line in tail[-TAIL_LINES:]],
    )


def complete(result, rtc, cfg_capable, mode=None) -> bool:
    """Has the server logged everything its configuration promises?"""
    return bool(
        result.norm
        and result.listening
        and (not rtc or (result.rtc_enabled and result.warmup))
        and (mode != "vlash" or result.vlash_enabled)
        and (not cfg_capable or result.cfg)
    )


def page_line(line) -> str:
    """``line`` with every absolute path reduced to its last segment."""
    return _ABSOLUTE.sub(r"\1", line)


def page_lines(path, count) -> list:
    """The last ``count`` (1..``MAX_PAGE_LINES``) printable lines of the log,
    for the page."""
    count = max(1, min(int(count), MAX_PAGE_LINES))
    # Redact full paths before shortening the display, preserving the basename.
    lines = _read_lines(path, TAIL_BYTES, True, truncate=False)
    return [page_line(line)[:LINE_CHARS] for line in lines][-count:]


# --- the state -----------------------------------------------------------------------

STATES = (
    "idle",
    "starting",
    "ready",
    "failed",
    "dead",
    "unknown",
    "foreign_listener",
    "stopping",
)
RUNNING = ("active", "activating", "reloading", "deactivating")
SERVING = ("active", "reloading")
OWN_PHASES = ("intent", "started")
INTENT_GRACE_S = 30.0  # systemd-run is still in flight, or the core died
LISTEN_GRACE_S = 5.0  # a listening server's other log lines are due by now


@dataclass
class Facts:
    """What ``derive`` decides from; nothing in it is read there."""

    record: dict | None
    record_error: bool
    # active|activating|reloading|deactivating|inactive|failed|absent|unknown
    unit: str
    other_units: list | None  # other levi-aeri-policy-* units; None: unreadable
    net_ok: bool
    # One entry per LISTEN socket on the port: True ours, False not ours, None
    # (our unit's processes could not be read) owner unknown.
    listeners: list
    established: int
    scan: Scan | None
    age_s: float | None
    start_timeout_s: float
    # Seconds since the "server listening" line was first seen (None: not yet).
    listening_age_s: float | None = None
    # A stop of this page is going on in this process right now.
    stop_in_flight: bool = False


@dataclass(frozen=True)
class Derived:
    state: str
    reason: str | None = None  # a code of REASONS
    failure: str | None = None  # a code of FAILURES
    stoppable: bool = False


def _failed(kind, running) -> Derived:
    return Derived("failed", failure=kind, stoppable=running)


def _own(f, rec) -> Derived:
    """A deploy of this page that has not been stopped: intent or started."""
    result = f.scan or Scan()
    rtc = bool(rec.get("rtc"))
    cfg = rec.get("cfg_guidance_scale") is not None
    running = f.unit in RUNNING
    ours = bool(f.listeners)
    mode = inference_mode.recorded(rec)
    finished = complete(result, rtc, cfg, mode)
    kind = result.failure
    # A traceback or an out-of-memory error of one request on a server that came
    # up is not a failed start.
    if kind in ("argparse", "address_in_use") or (
        kind in ("traceback", "xla_oom") and not finished
    ):
        return _failed(kind, running)
    if ours and result.listening and (f.listening_age_s or 0.0) >= LISTEN_GRACE_S:
        if mode == "vlash" and not result.vlash_enabled:
            return _failed("vlash_not_ready", True)
        if rtc and not (result.rtc_enabled and result.warmup):
            return _failed("rtc_not_ready", True)
        if cfg and not result.cfg:
            return _failed("cfg_not_confirmed", True)
    if f.unit in SERVING and ours and finished:
        return Derived("ready", stoppable=True)
    age = f.age_s or 0.0
    if f.unit in ("activating", "active", "reloading"):
        if age > f.start_timeout_s:
            return _failed("start_timeout", True)
        return Derived("starting", stoppable=True)
    if f.unit == "deactivating":
        return Derived("stopping", "stopped_elsewhere")
    if ours:  # a listener of ours with no unit of ours: cannot be
        return Derived("unknown", "unclassified")
    if result.listening:
        return Derived("dead", "exited_after_ready")
    if rec.get("phase") == "intent" and f.unit == "absent":
        if age < INTENT_GRACE_S:
            return Derived("starting")
        return _failed("intent_only", False)
    return _failed(kind or "exited", False)


def derive(f) -> Derived:
    """The named state of the policy server, from the facts alone. The rules
    run in this order and the first that applies wins; a source that could not
    be read is ``unknown``, never taken for an absent server."""
    rec = f.record
    phase = rec.get("phase") if rec else None
    running = f.unit in RUNNING
    ours = bool(f.listeners) and all(x is True for x in f.listeners)
    if not f.net_ok:
        return Derived("unknown", "net_unreadable")
    if f.record_error:
        return Derived("unknown", "record_unreadable")
    if (rec or f.listeners) and (f.unit == "unknown" or f.other_units is None):
        return Derived("unknown", "systemd_unreadable")
    foreign = [x for x in f.listeners if x is not True]
    if foreign:
        if phase not in (*OWN_PHASES, "stopping") or not running:
            return Derived("foreign_listener", "hand_started")
        if any(x is None for x in foreign):
            return Derived("foreign_listener", "owner_unknown")
        return Derived("foreign_listener", "listener_not_ours")
    if phase == "stopping" and (running or ours):
        # A unit still up with no stop going on is a stop that never ran (the
        # core stopped after the mark): it can be stopped again.
        again = f.unit in ("active", "activating", "reloading") and not f.stop_in_flight
        return Derived("stopping", stoppable=again)
    if phase in OWN_PHASES:
        return _own(f, rec)
    if phase == "start_failed":
        return _failed("start_failed", False)
    if phase in (None, "stopped", "aborted", "stopping") and not running and not ours:
        return Derived("idle")
    return Derived("unknown", "unclassified")


# --- reasons, failures and the panel's words ------------------------------------------

REASONS = {
    "net_unreadable": "The kernel's socket tables could not be read, so nothing is "
    "assumed free.",
    "systemd_unreadable": "systemd could not be asked, so nothing is assumed stopped.",
    "record_unreadable": "The deploy record could not be read, so the state of the "
    "server is not known.",
    "hand_started": "Something that is not a unit of this page listens on port "
    "{port} (for example a server started by hand). This page does not touch it: "
    "press Ctrl+C in the terminal that runs it.",
    "listener_not_ours": "The unit of this page is running, but something else "
    "holds port {port}. This page does not touch it: press Ctrl+C in the terminal "
    "that runs it.",
    "owner_unknown": "Who holds port {port} could not be read from the unit of this "
    "page, so the listener is treated as not ours and is not touched.",
    "exited_after_ready": "The server was ready and then exited (stopped from "
    "outside this page, or it crashed). The log shows how it ended.",
    "stopped_elsewhere": "The unit is shutting down without a stop from this page.",
    "unclassified": "The state could not be classified from what was read.",
}
FAILURES = {
    "xla_oom": "The GPU ran out of memory while loading the model. Free memory, or "
    "pick a larger fraction.",
    "argparse": "The server did not accept its command line; the log shows why.",
    "address_in_use": "The port was taken when the server tried to listen.",
    "traceback": "The server stopped with an error; the log shows it.",
    "vlash_not_ready": "The server listens but never confirmed VLASH protocol=1.",
    "rtc_not_ready": "The server listens but never logged that RTC is enabled and "
    "warmed up. An RTC deploy without that is a failed deploy: stop it and read "
    "the log.",
    "cfg_not_confirmed": "The server listens but never logged its guidance scale; "
    "it may not be using the CFG policy. Stop it and read the log.",
    "start_timeout": "The server did not become ready within the start timeout.",
    "exited": "The server exited before it listened; the log shows why.",
    "start_failed": "systemd-run could not start the unit; nothing runs.",
    "intent_only": "The record was written but no unit was started (the core "
    "stopped in between).",
}
CHECK_OK = {
    "state_dir": "The folder for the deploy record is usable.",
    "net_table": "The socket tables can be read.",
    "port_free": "Nothing listens on the policy port.",
    "no_client": "No client is connected to the policy port.",
    "policy_units": "No policy server unit exists.",
    "arm_lock": "The arm lock is free.",
    "gpu_lock": "The GPU lock is free or held by the live vision model server.",
    "gpu_readable": "The GPU's memory use can be read.",
    "vram": "There is room for the smallest fraction.",
}
ESTOP_TEXT = (
    "Keep a hand on the physical e-stop for every session. This panel only starts "
    "and stops the policy server unit it created; it cannot stop the robot."
)
NOTE_UNMANIFESTED = {
    "code": "source_hash_unmanifested",
    "text": "No source-hash manifest is named in the site file; the sha256 of the "
    "listed openpi files is recorded in the deploy record instead.",
}
KEEP_HAND_STATES = ("failed", "dead", "stopping", "foreign_listener", "unknown")
FORCED_STOP_WARNING = (
    "A client was connected and has lost its server. Keep a hand on the "
    "physical e-stop."
)
NEXT_DEPLOY = "Watch the deploy state; the server is ready when the state is ready."
NEXT_STOP = "Watch the deploy state; the port is free when the state is idle."

# --- the supervisor ------------------------------------------------------------------

CACHE_S = 2.0
SYSTEMCTL_TIMEOUT_S = 5
START_TIMEOUT_S = 15
STOP_RECHECK_S = 0.3
STOP_POLL_S = 0.5
STOP_EXTRA_S = 30
WATCH_EVERY_S = 2.0
WATCH_UNKNOWN_READS = 3
STOP_PHRASE = "stop policy server"
CLIENT_OVERRIDE = "stop with client connected"
VIEW_SCHEMA = "levi.marvel0.deploy.v1"
RESULT_SCHEMA = "levi.marvel0.deploy_result.v1"
STOP_SCHEMA = "levi.marvel0.stop_result.v1"
LOG_SCHEMA = "levi.marvel0.deploy_log.v1"
LIVE_HINT = "Put the live vLLM to sleep or stop it (/live), then try again."


class _Systemd:
    """The runner ``ProcView`` wants, over ``Host.run``."""

    def __init__(self, host):
        self.host = host

    def run(self, argv):
        return self.host.run(argv, SYSTEMCTL_TIMEOUT_S)


def _http(exc):
    extra = {k: v for k, v in exc.extra.items() if k != "unknown"}
    return fail(exc.status, exc.code, exc.message, **extra)


def _attempt(check, *args):
    try:
        check(*args)
    except Refuse as exc:
        return exc
    return None


def _row(code, refusal) -> dict:
    if refusal is None:
        return {"code": code, "level": "ok", "text": CHECK_OK[code], "params": {}}
    unknown = refusal.code in UNREADABLE or refusal.extra.get("unknown")
    params = {
        k: v
        for k, v in refusal.extra.items()
        if k != "unknown" and isinstance(v, (str, int, float, bool, list))
    }
    return {
        "code": code,
        "level": "unknown" if unknown else "refuse",
        "text": refusal.message,
        "params": params,
    }


def _sub(value, state):
    return value if state not in ("absent", "unknown") else None


class Supervisor:
    """Deploy, state, stop and log of the one policy server this page may run.
    ``site_loader`` gives the site (or raises the 412 answer), ``catalog`` has
    ``get(site, model_id)`` for the validated models, ``workspace`` is the
    product workspace the deploy folder lives in (default: the process-wide
    one)."""

    def __init__(self, host, site_loader=None, catalog=None, workspace=None):
        if site_loader is None:
            from . import site

            site_loader = site.require
        if catalog is None:
            from . import models

            catalog = models
        self.host = host
        self.site_loader = site_loader
        self.catalog = catalog
        self.workspace = workspace
        self._guard = threading.Lock()  # one operation at a time, in this process
        self._mutex = threading.Lock()  # the fields below
        self._op = None
        self._cache = None
        self._holder = None
        self._listening = {}
        self._watcher = None

    # ---- small things

    def invalidate(self) -> None:
        with self._mutex:
            self._cache = None

    def _iso(self) -> str:
        return record.iso(self.host.now())

    def _state_dir(self, site, create) -> Path:
        avoid = (site.rollout_root, site.checkpoints_root, site.openpi_checkout)
        try:
            return record.state_dir(avoid, self.workspace, create=create)
        except record.StateDirUnusable as exc:
            raise Refuse("state_dir_unusable", f"{exc}") from None

    def _units(self) -> OwnUnits:
        host = self.host
        return OwnUnits(
            host.proc_root,
            cgroup_root=host.cgroup_root,
            systemctl=lambda argv: host.run(argv, SYSTEMCTL_TIMEOUT_S),
        )

    def _procs(self) -> ProcView:
        host = self.host
        return ProcView(
            host.proc_root, systemd=_Systemd(host), cgroup_root=host.cgroup_root
        )

    # ---- what is read (each raises Refuse when it cannot be read or must refuse)

    def _read_net(self):
        table = net.read(self.host.proc_net)
        if not table.ok:
            raise Refuse("net_table_unreadable")
        return table

    def _read_units(self) -> list:
        try:
            return self._units().policy_units()
        except OSError:
            raise Refuse("systemd_unreadable") from None

    def _unit_state(self, unit) -> tuple:
        """``(state, sub state)`` of our unit; ``absent`` once systemd no longer
        knows it, ``unknown`` when systemd cannot be asked."""
        code, text = self.host.run(
            [
                "systemctl",
                "--user",
                "show",
                "-p",
                "LoadState",
                "-p",
                "ActiveState",
                "-p",
                "SubState",
                f"{unit}.service",
            ],
            SYSTEMCTL_TIMEOUT_S,
        )
        if code != 0:
            return "unknown", None
        shown = parse_show(text)
        if shown.get("LoadState") == "not-found":
            return "absent", None
        state = shown.get("ActiveState")
        if state not in (*RUNNING, "inactive", "failed", "reloading"):
            return "unknown", None
        return state, shown.get("SubState")

    def _ownership(self, rec, socks) -> list:
        """Per listening socket: True when a process of our recorded unit holds
        it, False when it is someone else's, None when our unit's processes
        could not be read (then it is treated as not ours)."""
        if not socks:
            return []
        unit = rec.get("unit") if rec else None
        if not isinstance(unit, str) or not record.UNIT_RE.fullmatch(unit):
            return [False] * len(socks)
        owners, unreadable = self._procs().owners([s.inode for s in socks], (unit,))
        return [
            True if owners.get(s.inode) else (None if unreadable else False)
            for s in socks
        ]

    # ---- the checks of a deploy (the same ones the page lists)

    def _check_port(self, site, table, rec):
        socks = net.listeners(table, site.port)
        if not socks:
            return
        owners = self._ownership(rec, socks)
        if all(o is True for o in owners):
            raise Refuse("already_running")
        raise Refuse(
            "port_taken",
            port=site.port,
            listeners=len(socks),
            ours=any(o is True for o in owners),
        )

    def _check_clients(self, site, table):
        count = len(net.established(table, site.port))
        if count:
            raise Refuse("client_connected", established=count)

    def _check_units(self, names):
        if not names:
            return
        if all(record.UNIT_RE.fullmatch(n.removesuffix(".service")) for n in names):
            raise Refuse("already_running", units=names)
        raise Refuse("policy_unit_exists", units=names)

    def _check_unsettled(self, rec, now):
        """A start whose ``systemd-run`` did not answer (or whose core stopped
        before it ran) may still create its unit: until the intent is older than
        ``INTENT_GRACE_S`` (the very moment the state turns from ``starting`` to
        ``failed``) another deploy would put a second server on the port. A unit
        that has appeared is refused by the checks before this one."""
        if not rec or rec.get("phase") != "intent":
            return
        began = rec.get("created_epoch")
        age = now - began if isinstance(began, (int, float)) else 0.0
        if age < INTENT_GRACE_S:
            raise Refuse("start_unconfirmed", age_s=round(age, 1))

    def _check_arm(self, site):
        holders = gpumgr.flock_holders(site.arm_lock_file, locks=self.host.locks_path)
        if holders is None:
            raise Refuse("arm_lock_unknown")
        if holders:
            raise Refuse("arm_lock_held")

    def _check_aeri(self):
        # The page's own terminal D first: a policy server deployed beside it
        # would take port 8000 (its 1/7 fails) or swap under it.
        try:
            hosted = self.host.hosted_aeri()
        except Exception:  # noqa: BLE001 - any failure reading it fails closed
            raise Refuse("aeri_registry_unreadable") from None
        if hosted:
            raise Refuse("aeri_run_live", run_id=str(hosted.get("id")))
        try:
            runs = self.host.aeri_runs()
        except Exception:  # noqa: BLE001 - any failure reading the registry fails closed
            raise Refuse("aeri_registry_unreadable") from None
        for run in runs:
            if run.get("backend") == "standalone" and run.get("runner_alive"):
                raise Refuse("aeri_run_live", run_id=str(run.get("run_id")))

    def _gpu_lock_mode(self, site) -> str:
        """``take`` (nobody holds the GPU lock) or ``live_holder`` (only LEVI's
        own live vision model server does: its lock is left alone and the
        memory check decides). Anything else is foreign."""
        holders = gpumgr.flock_holders(site.gpu_lock_file, locks=self.host.locks_path)
        if holders is None:
            raise Refuse("gpu_lock_foreign", unknown=True)
        if not holders:
            return "take"
        try:
            live = self._units().unit_pids(site.live.unit)
        except OSError:
            raise Refuse("gpu_lock_foreign", unknown=True) from None
        if all(pid in live for pid in holders):
            return "live_holder"
        raise Refuse("gpu_lock_foreign")

    def _read_gpu(self) -> Gpu:
        return read_gpu(self.host.run)

    def _check_vram(self, site, gpu, fraction):
        margin = site.gpu.margin_mib
        needed = required_mib(fraction, gpu.total_mib, margin)
        if gpu.free_mib < needed:
            raise Refuse(
                "gpu_insufficient",
                fraction=fraction,
                total_mib=gpu.total_mib,
                free_mib=gpu.free_mib,
                required_mib=needed,
                reserved_mib=needed - margin,
                margin_mib=margin,
                hint=LIVE_HINT,
            )

    def _unit_env(self, site) -> UnitEnv:
        code, text = self.host.run(SHOW_ENVIRONMENT, SYSTEMCTL_TIMEOUT_S)
        if code != 0:
            raise Refuse("env_unreadable")
        return unit_env(text, site)

    def _checkpoint(self, site, model) -> str:
        """The absolute checkpoint folder, when it lies inside the checkpoints
        folder and has ``params/`` and a top-level ``norm_stats.json`` (the one
        the server reads)."""
        root = Path(os.path.realpath(site.checkpoints_root))
        target = Path(os.path.realpath(Path(site.checkpoints_root) / model.id))
        if target == root or not target.is_relative_to(root):
            raise Refuse("checkpoint_invalid")
        try:
            params = os.lstat(target / "params")
            norm = os.lstat(target / "norm_stats.json")
        except OSError:
            raise Refuse("checkpoint_invalid") from None
        if not stat.S_ISDIR(params.st_mode) or not stat.S_ISREG(norm.st_mode):
            raise Refuse("checkpoint_invalid")
        return str(target)

    def _runtime_files(self, site, rtc, mode=None):
        script = Path(site.openpi_checkout) / serve_script(rtc, mode)
        python = site.python
        if not (os.path.isfile(python) and os.access(python, os.X_OK)):
            raise Refuse("runtime_files_missing")
        if not script.is_file():
            raise Refuse("runtime_files_missing")

    def _source(self, site, mode=None) -> dict:
        """The sha256 of the openpi files the site lists. A manifest named in
        the site file must agree with them; with none they are only recorded."""
        base = Path(site.openpi_checkout)
        files = {}
        paths = list(site.source.hash_files)
        if mode == "vlash":
            paths += [
                inference_mode.VLASH_SCRIPT,
                inference_mode.VLASH_CLIENT,
                "lab/vlash/runtime.py",
                "examples/fr3_local/run_robotiq_client.py",
                "examples/fr3_local/run_robotiq_client_collect_data.py",
            ]
        for rel in dict.fromkeys(paths):
            try:
                files[rel] = self.host.hash_file(str(base / rel))
            except OSError:
                raise Refuse("source_file_missing") from None
        manifest = bool(site.source.manifest)
        if manifest:
            path = Path(site.source.manifest)
            path = path if path.is_absolute() else base / path
            try:
                wanted = json.loads(self.host.read_file(str(path)))["files"]
            except (OSError, ValueError, KeyError, TypeError):
                raise Refuse("source_file_missing") from None
            for rel, digest in files.items():
                if not isinstance(wanted, dict) or wanted.get(rel) != digest:
                    raise Refuse("source_changed", file=rel)
        return {"manifest": manifest, "files": files}

    # ---- the form

    def _request_id(self, body) -> str:
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not record.REQUEST_ID.fullmatch(
            request_id
        ):
            raise Refuse("request_id_invalid")
        return request_id

    def _form(self, site, body):
        model = self.catalog.get(site, body.get("model_id"))
        if model is None:
            raise Refuse("model_unknown")
        try:
            wanted = round(float(body.get("fraction")), 4)
        except (TypeError, ValueError, OverflowError):
            wanted = None
        fraction = next(
            (p for p in site.gpu.fraction_presets if round(p, 4) == wanted), None
        )
        if fraction is None:
            raise Refuse("fraction_not_allowed")
        scale = body.get("cfg_guidance_scale")
        if not model.cfg_capable:
            if scale is not None:
                raise Refuse("cfg_scale_not_allowed")
        else:
            try:
                scale = site.caps.cfg_scale_default if scale is None else float(scale)
            except (TypeError, ValueError):
                raise Refuse("cfg_scale_out_of_range") from None
            if not 0 < scale <= site.caps.cfg_scale_max:
                raise Refuse("cfg_scale_out_of_range")
        if not model.verified and not body.get("accept_unverified"):
            raise Refuse("unverified_not_accepted")
        try:
            mode = inference_mode.resolve(body, default_rtc=False)
            if mode == "vlash":
                inference_mode.overlap(body.get("vlash_overlap_steps"))
                if not inference_mode.capable(model.config):
                    raise Refuse("vlash_model_incompatible")
            elif body.get("vlash_overlap_steps") is not None:
                raise Refuse("vlash_overlap_not_allowed")
        except inference_mode.ModeError as exc:
            raise Refuse(exc.code) from exc
        return model, mode == "rtc", fraction, scale

    # ---- requests: stored, replayed, one at a time

    def _op_view(self):
        with self._mutex:
            return dict(self._op) if self._op else None

    @contextlib.contextmanager
    def _operation(self, action, request_id, directory):
        if not self._guard.acquire(blocking=False):
            raise Refuse("busy", operation=self._op_view())
        fd = None
        try:
            try:
                fd = os.open(
                    directory / "op.lock",
                    os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                )
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Refuse("busy", operation=None) from None
            except OSError:
                raise Refuse("state_dir_unusable") from None
            with self._mutex:
                self._op = {
                    "action": action,
                    "request_id": request_id,
                    "started_at": self._iso(),
                }
            try:
                yield
            finally:
                with self._mutex:
                    self._op = None
        finally:
            if fd is not None:
                os.close(fd)
            self._guard.release()

    def _replayed(self, directory, request_id, fingerprint, locked):
        """The stored answer of this very request, ``None`` if it is new. A
        request that was accepted but never answered (the core stopped) is
        answered ``unknown``: nothing is started a second time."""
        try:
            entry = record.get_request(directory, request_id)
        except record.RecordCorrupt:
            entry = {"status": "accepted", "fingerprint": fingerprint}
        if entry is None:
            return None
        if entry.get("fingerprint") != fingerprint:
            raise Refuse("request_id_used")
        if entry.get("status") == "done" and isinstance(entry.get("answer"), dict):
            return {**entry["answer"], "replayed": True}
        if not locked:
            return None
        deploy_action = entry.get("action", "deploy") == "deploy"
        return {
            "schema": RESULT_SCHEMA if deploy_action else STOP_SCHEMA,
            "created": False,
            "replayed": True,
            "request_id": request_id,
            "state": "unknown",
            "reason": {
                "code": "request_incomplete",
                "text": "This request was accepted but never answered (the core "
                "stopped). Nothing was started again; read the state below.",
            },
        }

    def _transaction(self, action, directory, request_id, body, work, model_id=None):
        """Run ``work`` once for this request, remembering the answer."""
        fingerprint = record.fingerprint(action, body)
        stored = self._replayed(directory, request_id, fingerprint, locked=False)
        if stored:
            return stored
        with self._operation(action, request_id, directory):
            stored = self._replayed(directory, request_id, fingerprint, locked=True)
            if stored:
                return stored
            entry = {"fingerprint": fingerprint, "action": action}
            record.put_request(
                directory,
                request_id,
                {**entry, "status": "accepted", "time": self._iso()},
            )
            try:
                answer = work()
            except Refuse as exc:
                if not getattr(exc, "keep_request", False):
                    record.drop_request(directory, request_id)
                record.audit(
                    directory,
                    {
                        "action": action,
                        "phase": "refused",
                        "request_id": request_id,
                        "model_id": model_id,
                        "code": exc.code,
                        "detail": exc.message,
                    },
                    now=self.host.now(),
                )
                raise
            record.put_request(
                directory,
                request_id,
                {**entry, "status": "done", "answer": answer, "time": self._iso()},
                replace=True,
            )
            self.invalidate()
            return answer

    # ---- deploy

    def deploy(self, body) -> dict:
        try:
            return self._deploy(body)
        except Refuse as exc:
            raise _http(exc) from None

    def _deploy(self, body) -> dict:
        request_id = self._request_id(body)
        model_id = str(body.get("model_id", ""))
        phrase = f"deploy {model_id}"
        if body.get("confirm") != phrase:
            raise Refuse("confirm_required", phrase=phrase)
        site = self.site_loader()
        directory = self._state_dir(site, create=True)
        return self._transaction(
            "deploy",
            directory,
            request_id,
            body,
            lambda: self._start(site, directory, request_id, body),
            model_id,
        )

    def _start(self, site, directory, request_id, body):
        """The form, then steps 6-17 of a deploy: files, source, the readings,
        the intent record, the final checks under the GPU lock, ``systemd-run``.
        (The form is checked after the request was looked up, so a request that
        is sent again is answered even if the catalog changed meanwhile.)"""
        host = self.host
        model, rtc, fraction, scale = self._form(site, body)
        inference = inference_mode.resolve(body, default_rtc=False)
        overlap = (
            inference_mode.overlap(body.get("vlash_overlap_steps"))
            if inference == "vlash"
            else None
        )
        checkpoint = self._checkpoint(site, model)
        self._runtime_files(site, rtc, inference)
        source = self._source(site, inference)
        try:
            old = record.read_record(directory)
        except record.RecordCorrupt:
            old = None
        table = self._read_net()
        self._check_port(site, table, old)
        self._check_clients(site, table)
        self._check_units(self._read_units())
        self._check_unsettled(old, host.now())
        self._check_arm(site)
        self._check_aeri()
        # A lock still held for an earlier deploy (its watcher has not looked yet)
        # would make the GPU lock look foreign. Nothing of that deploy runs: the
        # checks above found no unit and no listener.
        self._drop_holder()
        mode = self._gpu_lock_mode(site)
        env = self._unit_env(site)
        gpu = self._read_gpu()
        self._check_vram(site, gpu, fraction)

        created = host.now()
        unit = record.unit_name(model.id)
        log_path = record.log_path(directory, unit, created)
        executed = server_argv(
            site,
            model,
            rtc,
            scale,
            checkpoint,
            mode=inference,
            vlash_overlap_steps=overlap,
        )
        command = systemd_run_argv(
            site,
            unit,
            model,
            rtc,
            fraction,
            scale,
            log_path,
            env,
            checkpoint,
            mode=inference,
            vlash_overlap_steps=overlap,
        )
        needed = required_mib(fraction, gpu.total_mib, site.gpu.margin_mib)
        notes = [] if source["manifest"] else ["source_hash_unmanifested"]
        intent = {
            "schema": record.SCHEMA,
            "request_id": request_id,
            "unit": unit,
            "model_id": model.id,
            "config": model.config,
            "rtc": rtc,
            "inference_mode": inference,
            "vlash_overlap_steps": overlap,
            "fraction": fraction,
            "cfg_guidance_scale": scale,
            "checkpoint_dir": checkpoint,
            "exec_argv": executed,
            "systemd_run_argv": command,
            "log_file": log_path.name,
            "phase": "intent",
            "created_at": record.iso(created),
            "created_epoch": created,
            "gpu": {
                "total_mib": gpu.total_mib,
                "free_mib": gpu.free_mib,
                "required_mib": needed,
            },
            "gpu_lock": "taken" if mode == "take" else mode,
            "source_hash": source,
            "notes": notes,
        }
        record.write_record(directory, intent)
        self._audit(directory, "accepted", request_id, model.id)

        lock = None
        try:
            if mode == "take":
                try:
                    lock = host.gpu_lock(site.gpu_lock_file)
                    taken = lock.acquire()
                except OSError:
                    taken = False
                if not taken:
                    lock = None
                    raise Refuse("gpu_lock_foreign")
            table = self._read_net()
            self._check_port(site, table, None)
            self._check_clients(site, table)
            self._check_units(self._read_units())
            self._check_arm(site)
            self._check_aeri()
            final = self._read_gpu()
            self._check_vram(site, final, fraction)
        except Refuse as exc:
            self._release(lock)
            self._abort(directory, "aborted", exc.code)
            raise
        except Exception:
            self._release(lock)
            self._abort(directory, "aborted", "error")
            raise
        return self._launch(
            site, directory, request_id, model, intent, final, mode, lock, command
        )

    def _launch(
        self, site, directory, request_id, model, intent, final, mode, lock, command
    ):
        host = self.host
        unit = intent["unit"]
        try:
            created = record.create_log(directory, unit, intent["created_epoch"])
            if created.name != intent["log_file"]:
                raise FileExistsError("the log name was taken meanwhile")
        except OSError as exc:  # nothing was started: the log is made first
            self._release(lock)
            self._abort(directory, "start_failed", type(exc).__name__)
            raise Refuse(
                "start_failed", exit_code=None, detail="the log file could not be made"
            ) from None
        try:
            code, text = host.run(command, START_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - it may have started
            code, text = TIMED_OUT, type(exc).__name__
        if code == TIMED_OUT:
            self._unconfirmed(site, directory, request_id, lock, text)
        if code != 0:
            self._release(lock)
            last = next((x for x in reversed(text.splitlines()) if x.strip()), "")
            detail = record.scrub(last)[: record.DETAIL_CHARS]
            self._abort(directory, "start_failed", detail, exit_code=code)
            raise Refuse("start_failed", exit_code=code, detail=detail)
        started = host.now()
        merged = record.update_record(
            directory,
            phase="started",
            started_at=record.iso(started),
            started_epoch=started,
        )
        self._audit(directory, "started", intent["request_id"], model.id)
        if lock is not None:
            self._hold(site, directory, lock, request_id)
        record.prune(directory, keep={intent["log_file"]})
        self.invalidate()
        return {
            "schema": RESULT_SCHEMA,
            "created": True,
            "replayed": False,
            "request_id": request_id,
            "phase": "started",
            "deploy": self._deploy_view(merged, started),
            "state": "starting",
            "gpu": {
                "total_mib": final.total_mib,
                "free_mib_at_final_check": final.free_mib,
                "required_mib": intent["gpu"]["required_mib"],
                "lock": merged["gpu_lock"],
            },
            "next": NEXT_DEPLOY,
        }

    def _unconfirmed(self, site, directory, request_id, lock, text):
        """``systemd-run`` did not answer in time (or failed in a way that
        proves nothing): the unit may have started. The record stays at
        ``intent``, so the state follows the unit if it appears and says
        ``failed`` if it does not; the GPU lock stays held meanwhile (the
        watcher lets go when the server is resident or the state settles); the
        request is kept, so sending it again starts nothing."""
        with contextlib.suppress(OSError, record.RecordCorrupt):
            record.update_record(
                directory, start_unconfirmed=record.scrub(text)[: record.DETAIL_CHARS]
            )
        if lock is not None:
            self._hold(site, directory, lock, request_id)
        self.invalidate()
        refusal = Refuse(
            "start_failed",
            (
                "systemd-run did not answer in time; the unit may or may not have "
                "started. Read the state before trying again."
            ),
            exit_code=None,
            detail=record.scrub(text)[: record.DETAIL_CHARS],
        )
        refusal.keep_request = True
        raise refusal

    def _release(self, lock):
        if lock is not None:
            with contextlib.suppress(Exception):
                lock.release()

    def _abort(self, directory, phase, code, **fields):
        with contextlib.suppress(OSError, record.RecordCorrupt):
            record.update_record(
                directory,
                phase=phase,
                abort_code=str(code)[: record.DETAIL_CHARS],
                aborted_at=self._iso(),
                **fields,
            )

    def _audit(self, directory, phase, request_id, model_id, code=None, detail=None):
        record.audit(
            directory,
            {
                "action": "deploy",
                "phase": phase,
                "request_id": request_id,
                "model_id": model_id,
                "code": code,
                "detail": detail,
            },
            now=self.host.now(),
        )

    # ---- the GPU lock, held only until the server is resident

    def _hold(self, site, directory, lock, request_id):
        with self._mutex:
            self._holder = {
                "lock": lock,
                "dir": directory,
                "request_id": request_id,
                "since": self.host.monotonic(),
                "limit": float(site.gpu.lock_hold_max_s),
                "unknown": 0,
            }
        if self.host.background:
            self._start_watcher()

    def _start_watcher(self):
        with self._mutex:
            if self._watcher is not None and self._watcher.is_alive():
                return
            self._watcher = threading.Thread(
                target=self._watch_loop, name="levi-marvel0-deploy-watch", daemon=True
            )
            self._watcher.start()

    def _watch_loop(self):
        while True:
            if self.watch_once():
                self.host.sleep(WATCH_EVERY_S)
                continue
            # Leaving is decided under the mutex that ``_start_watcher`` tests
            # under: a hold that landed meanwhile is looked at, not stranded.
            with self._mutex:
                if self._holder is None:
                    self._watcher = None
                    return

    def watch_once(self) -> bool:
        """One look at the server while the GPU lock is held. The lock is
        released as soon as the server is no longer loading (ready, failed,
        gone), when the state stays unreadable, or after ``lock_hold_max_s``.
        Returns True while the lock is still held."""
        with self._mutex:
            holder = self._holder
        if holder is None:
            return False
        try:
            state = self._snapshot(self.site_loader())[0].state
        except Exception:  # noqa: BLE001 - a read that fails is an unreadable state
            state = "unknown"
        held = self.host.monotonic() - holder["since"]
        if state == "starting" and held < holder["limit"]:
            holder["unknown"] = 0
            return True
        if state == "unknown":
            holder["unknown"] += 1
            if holder["unknown"] < WATCH_UNKNOWN_READS and held < holder["limit"]:
                return True
        self._release(holder["lock"])
        with self._mutex:
            mine = self._holder is holder  # (a newer hold is not ours to drop)
            if mine:
                self._holder = None
        if mine:
            with contextlib.suppress(OSError, record.RecordCorrupt):
                record.update_record(holder["dir"], lock_released_at=self._iso())
            self._audit(
                holder["dir"], "lock_released", holder["request_id"], None, code=state
            )
        self.invalidate()
        return False

    # ---- what the page shows

    def _deploy_view(self, rec, now) -> dict:
        began = rec.get("started_epoch") or rec.get("created_epoch")
        return {
            "request_id": rec.get("request_id"),
            "unit": rec.get("unit"),
            "model_id": rec.get("model_id"),
            "config": rec.get("config"),
            "rtc": rec.get("rtc"),
            "inference_mode": inference_mode.recorded(rec),
            "vlash_overlap_steps": rec.get("vlash_overlap_steps"),
            "fraction": rec.get("fraction"),
            "cfg_guidance_scale": rec.get("cfg_guidance_scale"),
            "phase": rec.get("phase"),
            "started_at": rec.get("started_at") or rec.get("created_at"),
            "age_s": round(now - began, 1) if began else None,
            "ready_at": rec.get("ready_at"),
            "gpu_lock": rec.get("gpu_lock"),
            "source_hash": rec.get("source_hash"),
        }

    def _listening_age(self, rec, result, mono):
        """Seconds the "server listening" line has been in the log (this
        process's own clock; None until it is seen)."""
        request_id = rec.get("request_id") if rec else None
        with self._mutex:
            if not request_id or result is None or not result.listening:
                self._listening = {}
                return None
            first = self._listening.setdefault(request_id, mono)
            return mono - first

    def status(self) -> dict:
        """The ``levi.marvel0.deploy.v1`` document. The site answers 412 as
        everywhere; no other unreadable source raises: it makes the state
        ``unknown`` or a check ``unknown``. Read at most every ``CACHE_S``."""
        site = self.site_loader()
        mono = self.host.monotonic()
        with self._mutex:
            cached = self._cache
        if cached is not None and mono - cached[0] < CACHE_S:
            body = copy.deepcopy(cached[1])
        else:
            try:
                body = self._snapshot(site)[1]
            except Exception:  # noqa: BLE001 - the panel is told, never a 500
                body = self._degraded(site)
            with self._mutex:
                self._cache = (mono, copy.deepcopy(body))
        body["operation"] = self._op_view()
        return body

    def _degraded(self, site) -> dict:
        """The answer when a read failed in a way no part accounts for: the
        state is unknown, nothing is offered, and the hand stays on the e-stop."""
        return {
            "schema": VIEW_SCHEMA,
            "sampled_at": self._iso(),
            "state": "unknown",
            "reason": {"code": "unclassified", "text": REASONS["unclassified"]},
            "deploy": None,
            "unit": {"state": "unknown", "sub": None},
            "listener": {
                "port": site.port,
                "present": None,
                "ours": None,
                "established": None,
            },
            "readiness": self._readiness(None, None),
            "failure": None,
            "gpu": {
                "state": "unknown",
                "total_mib": None,
                "used_mib": None,
                "free_mib": None,
                "server_holds_mib": None,
                "server_holds_basis": "reserved_fraction",
            },
            "vram": {"margin_mib": site.gpu.margin_mib, "presets": [], "hint": None},
            "checks": [],
            "can_deploy": False,
            "can_stop": False,
            "stop_blockers": [],
            "live_annotation": None,
            "estop": {"always": True, "keep_hand": True, "text": ESTOP_TEXT},
            "operation": None,
            "notes": [] if site.source.manifest else [NOTE_UNMANIFESTED],
            "confirm": {
                "deploy": "deploy {model_id}",
                "stop": STOP_PHRASE,
                "client_override": CLIENT_OVERRIDE,
            },
        }

    def _snapshot(self, site) -> tuple:
        """``(Derived, view)``: everything read afresh, nothing cached."""
        host = self.host
        now, mono, port = host.now(), host.monotonic(), site.port
        directory, rec, dir_problem, rec_error = None, None, None, False
        try:
            directory = self._state_dir(site, create=False)
            rec = record.read_record(directory)
        except Refuse as exc:
            dir_problem, rec_error = exc, True
        except record.RecordCorrupt:
            rec_error = True
        unit = rec.get("unit") if rec else None
        if rec is not None and not (
            isinstance(unit, str) and record.UNIT_RE.fullmatch(unit)
        ):
            rec, unit, rec_error = None, None, True

        table, table_error = None, None
        try:
            table = self._read_net()
        except Refuse as exc:
            table_error = exc
        socks = net.listeners(table, port) if table is not None else []
        clients = len(net.established(table, port)) if table is not None else 0
        names, units_error = None, None
        try:
            names = self._read_units()
        except Refuse as exc:
            units_error = exc
        state, sub = self._unit_state(unit) if unit else ("absent", None)
        result = None
        if rec and rec.get("log_file") and rec.get("checkpoint_dir"):
            result = scan(
                directory / "logs" / rec["log_file"], rec["checkpoint_dir"], port
            )
        began = rec and (rec.get("started_epoch") or rec.get("created_epoch"))
        facts = Facts(
            record=rec,
            record_error=rec_error,
            unit=state,
            other_units=(
                None if names is None else [n for n in names if n != f"{unit}.service"]
            ),
            net_ok=table is not None,
            listeners=self._ownership(rec, socks),
            established=clients,
            scan=result,
            age_s=(now - began) if began else None,
            start_timeout_s=float(site.unit.start_timeout_s),
            listening_age_s=self._listening_age(rec, result, mono),
            stop_in_flight=(self._op_view() or {}).get("action") == "stop",
        )
        derived = derive(facts)
        if derived.state == "ready" and rec and not rec.get("ready_at"):
            with contextlib.suppress(OSError, record.RecordCorrupt):
                rec = record.update_record(directory, ready_at=record.iso(now))
        problems = {
            "state_dir": dir_problem,
            "net_table": table_error,
            "units": units_error,
        }
        view = self._view(
            site, now, derived, facts, rec, sub, result, socks, table, names, problems
        )
        return derived, view

    def _checks(self, site, rec, gpu, gpu_error, table, names, problems) -> list:
        """The model-independent checks of a deploy, as the page lists them
        (the very ``_check_*`` a deploy refuses with). The run registry of the
        automatic evaluation (``_check_aeri``) is not among them: it reads LEVI's
        key and every run's journal, too much for a page that polls every few
        seconds. A deploy reads it, twice; the arm lock, which a run holds while
        it uses the arm, is the check a poll shows."""
        net_problem = problems["net_table"]
        if table is not None:
            port_problem = _attempt(self._check_port, site, table, rec)
            client_problem = _attempt(self._check_clients, site, table)
        else:
            port_problem = client_problem = net_problem
        if names is not None:
            unit_problem = _attempt(self._check_units, names)
        else:
            unit_problem = problems["units"]
        if gpu is not None:
            vram_problem = _attempt(
                self._check_vram, site, gpu, site.gpu.fraction_presets[0]
            )
        else:
            vram_problem = gpu_error
        return [
            _row("state_dir", problems["state_dir"]),
            _row("net_table", net_problem),
            _row("port_free", port_problem),
            _row("no_client", client_problem),
            _row("policy_units", unit_problem),
            _row("arm_lock", _attempt(self._check_arm, site)),
            _row("gpu_lock", _attempt(self._gpu_lock_mode, site)),
            _row("gpu_readable", gpu_error),
            _row("vram", vram_problem),
        ]

    def _holds(self, rec, gpu, derived, facts):
        """The memory the running server reserves (None: not running, or not
        ours so not known) and whether anything holds the card's memory."""
        state = derived.state
        holding = state in ("starting", "ready", "stopping", "foreign_listener") or (
            state == "failed" and facts.unit in RUNNING
        )
        total = (
            gpu.total_mib if gpu else ((rec or {}).get("gpu") or {}).get("total_mib")
        )
        holds = None
        if (
            holding
            and state != "foreign_listener"
            and rec
            and total
            and rec.get("fraction")
        ):
            holds = reserved_mib(rec["fraction"], total)
        return holding, holds

    def _view(
        self, site, now, derived, facts, rec, sub, result, socks, table, names, problems
    ):
        state = derived.state
        gpu, gpu_error = None, None
        try:
            gpu = self._read_gpu()
        except Refuse as exc:
            gpu_error = exc
        checks = self._checks(site, rec, gpu, gpu_error, table, names, problems)
        holding, holds = self._holds(rec, gpu, derived, facts)
        running = facts.unit in RUNNING

        owners = facts.listeners
        ours = None
        if socks:
            ours = all(o is True for o in owners)
            if not ours and all(o is None or o is True for o in owners):
                ours = None  # unreadable, not known to be someone else's

        presets = []
        for fraction in site.gpu.fraction_presets:
            needed = (
                required_mib(fraction, gpu.total_mib, site.gpu.margin_mib)
                if gpu
                else None
            )
            presets.append(
                {
                    "fraction": fraction,
                    "required_mib": needed,
                    "enough": None if gpu is None else gpu.free_mib >= needed,
                }
            )
        larger = [f"{p:.2f}" for p in site.gpu.fraction_presets[1:]]
        hint = (
            f"Fractions {' and '.join(larger)} need the live vLLM asleep or stopped"
            if larger
            else None
        )

        blockers = []
        if facts.established:
            blockers.append("client_connected")
        if state == "foreign_listener":
            blockers.append("foreign_listener")
        if not facts.net_ok:
            blockers.append("net_unreadable")

        # A stop that cut a client off leaves the notice until the arm controller
        # is gone and no session is open, also after the server has stopped.
        cut_off = state == "idle" and bool(rec and rec.get("stopped_with_client"))
        watch = state in KEEP_HAND_STATES or cut_off
        context = {}
        if watch:
            try:
                context = self.host.robot_context()
            except Exception:  # noqa: BLE001 - unknown is the cautious answer
                context = {}
        keep_hand = watch and (
            context.get("marker", "unknown") in ("present", "unknown")
            or context.get("open_session") in (True, None)
        )

        failure = None
        if derived.failure:
            failure = {"kind": derived.failure, "text": FAILURES[derived.failure]}
        reason = None
        if derived.reason:
            reason = {
                "code": derived.reason,
                "text": REASONS[derived.reason].format(port=site.port),
            }
        live = None
        if holding:
            amount = holds if holds is not None else "?"
            live = {
                "blocked": True,
                "text": f"server holds {amount} MiB, live annotation cannot start",
            }

        return {
            "schema": VIEW_SCHEMA,
            "sampled_at": record.iso(now),
            "state": state,
            "reason": reason,
            "deploy": self._deploy_view(rec, now) if rec else None,
            "unit": {"state": facts.unit, "sub": _sub(sub, facts.unit)},
            "listener": {
                "port": site.port,
                "present": bool(socks),
                "ours": ours,
                "established": facts.established,
            },
            "readiness": self._readiness(rec, result),
            "failure": failure,
            "gpu": {
                "state": "ok" if gpu else "unknown",
                "total_mib": gpu.total_mib if gpu else None,
                "used_mib": gpu.used_mib if gpu else None,
                "free_mib": gpu.free_mib if gpu else None,
                "server_holds_mib": holds,
                "server_holds_basis": "reserved_fraction",
            },
            "vram": {
                "margin_mib": site.gpu.margin_mib,
                "presets": presets,
                "hint": hint,
            },
            "checks": checks,
            "can_deploy": state in ("idle", "failed", "dead")
            and all(c["level"] == "ok" for c in checks),
            "can_stop": derived.stoppable and running,
            "stop_blockers": blockers,
            "live_annotation": live,
            "estop": {"always": True, "keep_hand": keep_hand, "text": ESTOP_TEXT},
            "operation": None,
            "notes": [] if site.source.manifest else [NOTE_UNMANIFESTED],
            "confirm": {
                "deploy": "deploy {model_id}",
                "stop": STOP_PHRASE,
                "client_override": CLIENT_OVERRIDE,
            },
        }

    def _readiness(self, rec, result) -> dict:
        rtc = bool(rec and rec.get("rtc"))
        cfg = bool(rec and rec.get("cfg_guidance_scale") is not None)
        if result is None:
            return {
                "norm_stats": None,
                "rtc_enabled": None,
                "vlash_enabled": None,
                "rtc_warmup": None,
                "cfg": None,
                "listening": None,
            }
        return {
            "norm_stats": result.norm,
            "rtc_enabled": result.rtc_enabled if rtc else None,
            "vlash_enabled": result.vlash_enabled
            if rec and inference_mode.recorded(rec) == "vlash"
            else None,
            "rtc_warmup": result.warmup if rtc else None,
            "cfg": result.cfg if cfg else None,
            "listening": result.listening,
        }

    # ---- the log

    def log(self, lines) -> dict:
        """The tail of the current deploy's log (``GET /deploy/log``)."""
        site = self.site_loader()
        rec, directory = None, None
        with contextlib.suppress(Refuse, record.RecordCorrupt):
            directory = self._state_dir(site, create=False)
            rec = record.read_record(directory)
        name = rec.get("log_file") if rec else None
        path = None
        if (
            directory is not None
            and isinstance(name, str)
            and record.LOG_RE.fullmatch(name)
        ):
            path = directory / "logs" / name
        empty = {
            "norm_stats": None,
            "rtc_enabled": None,
            "rtc_warmup": None,
            "cfg": None,
            "listening": None,
            "failure": None,
        }
        if path is None:
            return {
                "schema": LOG_SCHEMA,
                "log_id": None,
                "size_bytes": 0,
                "lines": [],
                "classification": empty,
            }
        try:
            size = os.lstat(path).st_size
        except OSError:
            size = 0
        result = scan(path, rec.get("checkpoint_dir", ""), site.port)
        return {
            "schema": LOG_SCHEMA,
            "log_id": name,
            "size_bytes": size,
            "lines": page_lines(path, lines),
            "classification": {
                **self._readiness(rec, result),
                "failure": result.failure,
            },
        }

    # ---- stop

    def stop(self, body) -> dict:
        try:
            return self._stop(body)
        except Refuse as exc:
            raise _http(exc) from None

    def _stop(self, body) -> dict:
        request_id = self._request_id(body)
        if body.get("confirm") != STOP_PHRASE:
            raise Refuse("confirm_required", phrase=STOP_PHRASE)
        override = body.get("client_override") or ""
        if override not in ("", CLIENT_OVERRIDE):
            raise Refuse("confirm_required", phrase=CLIENT_OVERRIDE)
        site = self.site_loader()
        directory = self._state_dir(site, create=True)
        return self._transaction(
            "stop",
            directory,
            request_id,
            body,
            lambda: self._stop_unit(site, directory, request_id, override),
        )

    def _wait_until_gone(self, site, unit) -> tuple:
        """Poll until the unit is gone and nothing listens, for the stop
        timeout. ``(unit state, listeners left)`` as last seen."""
        host = self.host
        deadline = host.monotonic() + site.unit.stop_timeout_s + STOP_EXTRA_S
        while True:
            state, _ = self._unit_state(unit)
            try:
                left = len(net.listeners(self._read_net(), site.port))
            except Refuse:
                left = None
            if state in ("inactive", "failed", "absent") and left == 0:
                return state, left
            if host.monotonic() >= deadline:
                return state, left
            host.sleep(STOP_POLL_S)

    def _stop_answer(self, request_id, unit, state, stopped, used, **extra) -> dict:
        if used:
            extra["warning"] = FORCED_STOP_WARNING
        return {
            "schema": STOP_SCHEMA,
            "created": True,
            "replayed": False,
            "request_id": request_id,
            "state": state,
            "unit": unit,
            "stopped": stopped,
            "client_override_used": used,
            "next": NEXT_STOP,
            **extra,
        }

    def _stop_unit(self, site, directory, request_id, override) -> dict:
        """Stop the unit this page deployed, and nothing else: its recorded
        name, after systemd reports that ``Id`` and ``ExecStart``, with no
        client connected (checked twice) unless forced by the typed phrase."""
        host = self.host
        try:
            rec = record.read_record(directory)
        except record.RecordCorrupt:
            raise Refuse(
                "nothing_to_stop",
                "The deploy record could not be read; nothing is stopped",
            ) from None
        table = self._read_net()
        socks = net.listeners(table, site.port)
        if rec is None:
            if socks:
                raise Refuse("foreign_listener_not_stoppable")
            raise Refuse("nothing_to_stop")
        unit = rec.get("unit")
        if (
            not isinstance(unit, str)
            or not record.UNIT_RE.fullmatch(unit)
            or unit != record.unit_name(rec.get("model_id"))
        ):
            raise Refuse("unit_mismatch")
        code, text = host.run(
            [
                "systemctl",
                "--user",
                "show",
                "-p",
                "Id",
                "-p",
                "LoadState",
                "-p",
                "ActiveState",
                "-p",
                "SubState",
                "-p",
                "ExecStart",
                f"{unit}.service",
            ],
            SYSTEMCTL_TIMEOUT_S,
        )
        if code != 0:
            raise Refuse("systemd_unreadable")
        shown = parse_show(text)
        if shown.get("LoadState") == "not-found" or shown.get("ActiveState") in (
            "inactive",
            "failed",
        ):
            if socks:
                raise Refuse("foreign_listener_not_stoppable")
            if rec.get("phase") in ("intent", "started", "stopping"):
                record.update_record(directory, phase="stopped", stopped_at=self._iso())
            return self._stop_answer(
                request_id, unit, "idle", False, False, already=True
            )
        if not same_unit(text, rec):
            raise Refuse("unit_mismatch")
        if socks and not all(o is True for o in self._ownership(rec, socks)):
            raise Refuse("foreign_listener_not_stoppable")
        forced = False
        clients = len(net.established(table, site.port))
        if clients:
            if override != CLIENT_OVERRIDE:
                raise Refuse("client_connected", established=clients)
            forced = True

        again = rec.get("phase") == "stopping"
        previous = (
            (rec.get("stopping_from") or "started") if again else rec.get("phase")
        )
        record.update_record(
            directory,
            phase="stopping",
            stopping_from=previous,
            stop_requested_at=self._iso(),
        )
        self.invalidate()
        record.audit(
            directory,
            {"action": "stop", "phase": "accepted", "request_id": request_id},
            now=host.now(),
        )
        try:
            host.sleep(STOP_RECHECK_S)
            clients = len(net.established(self._read_net(), site.port))
            if clients:
                if override != CLIENT_OVERRIDE:
                    raise Refuse("client_connected", established=clients)
                forced = True
            code, _ = host.run(
                ["systemctl", "--user", "stop", f"{unit}.service"],
                site.unit.stop_timeout_s + STOP_EXTRA_S,
            )
            # (A stop that outlasts the command's own timeout is still going on:
            # the wait below decides.)
            if code not in (0, TIMED_OUT):
                raise Refuse("stop_failed", exit_code=code)
            if forced:  # remembered, so the notice survives a reload of the page
                record.update_record(directory, stopped_with_client=True)
        except Refuse:
            record.update_record(directory, phase=previous)
            self.invalidate()
            raise

        state, left = self._wait_until_gone(site, unit)
        if state in ("inactive", "failed", "absent") and left == 0:
            record.update_record(directory, phase="stopped", stopped_at=self._iso())
            self._drop_holder()
            record.audit(
                directory,
                {"action": "stop", "phase": "stopped", "request_id": request_id},
                now=host.now(),
            )
            return self._stop_answer(request_id, unit, "idle", True, forced)
        if again or state in ("inactive", "failed", "absent"):
            # Still deactivating after a second wait, or the unit is gone but
            # something still listens: the person finishes it, nothing is killed.
            raise Refuse("stop_incomplete", unit=unit)
        return self._stop_answer(request_id, unit, "stopping", False, forced)

    def _drop_holder(self):
        """Let go of the GPU lock held for the last deploy, if any."""
        with self._mutex:
            holder = self._holder
            self._holder = None
        if holder is not None:
            self._release(holder["lock"])
