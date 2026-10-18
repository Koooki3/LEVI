"""Switching the FR3 policy service between the forward and reset roles.

Opt-in through the binding's ``policy_swap`` block (``fr3_run``); a binding
without it keeps the original contract (every role's server already running).
With it, at most one policy service runs at a time, so one 32 GB GPU holds a
policy service and the awake vision model of the live service.

The block's ``services`` name exactly the binding's policy roles: ``forward``
and ``reset`` (``single_reset_policy``), or ``forward`` alone (``human_assisted``
reset). With only ``forward`` there is no switch: ``startup`` starts the one
unit before any arm I/O, the first ``ensure`` warms it once, ``shutdown``
stops it; the leftover, port, GPU-budget and release checks are the same.

The unit's stdout and stderr go to a private file under the rollout root
(``StandardOutput=append:``, mode 0600), which nobody reads while things work.
When a start, handshake, warm-up or running service fails, the last lines of
what this attempt wrote are printed to the terminal and kept in the switch log.

The switch happens only inside ``policy.acquire(role)``, which the
orchestrator calls in the action of the move into ``FORWARD_ACTIVE`` or
``RESET_ACTIVE``. ``_tx`` revoked the motion fence before that action, and the
episode's token is issued only after the move commits, so no motion token
exists while services change. One switch:

0. preconditions: not latched, no token, the arm is at the reviewed home pose
   and still (two reads ``still_window_ms`` apart);
1. ``prepared`` in the switch log (fsync);
2. ``systemctl --user stop`` of the old unit, then wait until the unit is not
   active, nothing listens on its port, its processes (the unit's own
   ``cgroup.procs``, read before the stop; ``/proc`` is never listed) are gone, and ``nvidia-smi`` lists none
   of them; only the binding's own ``levi-aeri-policy-*`` units are stopped,
   no process is ever signalled by PID;
3. GPU budget: free memory >= ``mem_fraction * total + gpu_margin_mib``;
4. ``systemd-run --user`` with the binding's fixed argv (no shell; the
   validator refuses ``$``, ``%`` and control characters, so systemd expands
   nothing), at most ``start_retries`` (0 or 1) retries after a start that
   definitely failed;
5. passive readiness (``campaign/switch.py``: the unit is active, every
   listener on the port is held by a process in the unit's ``cgroup.procs``,
   and the unit's ``ExecStart`` (systemd's record) names the frozen config and
   checkpoint; nothing connects);
6. only then the client handshake (identity + client contract checks);
7. a warm-up query on a real current observation whose actions are dropped
   (a cold JAX server compiles on its first query, longer than the chunk
   timeout);
8. postconditions: no token, no motion step counted, the arm did not move;
9. ``committed``.

Any failure, timeout, operator stop or uncertainty aborts the switch, stops
the new unit when it was touched, records whether the service state is known,
and latches this switcher: every later ``acquire`` is refused without calling
systemd, and the orchestrator moves to ``FAULT_LOCKED``. A log whose last
transaction never ended (a crash or power loss) or whose final service state
is not ``none`` needs an operator acknowledgement before the next run in the
same rollout root (``pending`` / ``acknowledge``).
"""

import json
import math
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..own_units import POLICY_UNIT
from . import fr3_bridge as bridge
from .campaign.conductor import ArmRef
from .campaign.switch import (
    CGROUP_ROOT,
    ProcView,
    SystemdPolicyHost,
    UserSystemd,
    runs_arm,
)

SCHEMA = "levi.aeri.fr3.policy_swap.v1"
LOG_SCHEMA = "levi.aeri.fr3.policy_swap_log.v1"
ROLES = ("forward", "reset")
TAIL_LINES = 40
TAIL_LINE_CHARS = 300
TAIL_READ_BYTES = 256 * 1024
# systemd expands % in properties; a path with whitespace is split by its parser.
UNSAFE_PATH = re.compile(r"[$%\s\x00-\x1f\x7f]")
UNIT = POLICY_UNIT  # one definition, in levi.own_units
FORBIDDEN = re.compile(r"[$%\x00-\x1f\x7f]")
WHITESPACE = re.compile(r"\s")
# The reviewed home check of fr3_bridge.Robot.home uses the same rotation bound.
HOME_ROTATION_RAD = 0.05
ACTION_DIMS = 7
KEYS = {
    "schema",
    "services",
    "vision_mem_fraction",
    "max_total_fraction",
    "gpu_margin_mib",
    "stop_timeout_s",
    "start_timeout_s",
    "handshake_timeout_s",
    "warmup_timeout_s",
    "start_retries",
    "still_window_ms",
    "still_tolerance_mm",
    "still_tolerance_rad",
}
SERVICE_KEYS = {"unit", "argv", "working_directory", "mem_fraction"}
TIMEOUT_LIMITS = {
    "stop_timeout_s": 600,
    "start_timeout_s": 1800,
    "handshake_timeout_s": 300,
    "warmup_timeout_s": 600,
}
QUIET_UNIT = ("inactive", "failed")
LOG_DIR = Path(".aeri") / "policy-swap"


class SwapError(ValueError):
    """``code`` names the refusal; the switcher is latched after any of them."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def as_swap_error(exc: BaseException) -> SwapError:
    """Any failure inside a switch as a ``SwapError``: an adapter error (a
    stale robot read, a camera fault) is ``adapter_error``; a process-level
    interruption (``KeyboardInterrupt``) is ``interrupted``."""
    if isinstance(exc, SwapError):
        return exc
    code = "adapter_error" if isinstance(exc, Exception) else "interrupted"
    return SwapError(code, f"{type(exc).__name__}: {exc}"[:300])


def _reraise(exc: BaseException):
    """Re-raise a switch failure: an ordinary exception as its ``SwapError``
    (so the policy adapter reports a fault), a process interruption as is."""
    if isinstance(exc, Exception) and not isinstance(exc, SwapError):
        raise as_swap_error(exc) from exc
    raise exc


@dataclass(frozen=True)
class Service:
    role: str
    unit: str
    argv: tuple
    working_directory: str
    mem_fraction: float
    port: int
    config: str
    checkpoint_dir: str

    def arm(self) -> ArmRef:
        return ArmRef(
            "fr3", self.role, self.unit, self.checkpoint_dir, self.config, self.port
        )


@dataclass(frozen=True)
class SwapConfig:
    services: dict
    vision_mem_fraction: float
    max_total_fraction: float
    gpu_margin_mib: int
    stop_timeout_s: float
    start_timeout_s: float
    handshake_timeout_s: float
    warmup_timeout_s: float
    start_retries: int
    still_window_ms: int
    still_tolerance_mm: float
    still_tolerance_rad: float


# --- the binding block (pure: reads files, never runs or connects) -----------------


def _number(value, name, *, low, high, low_open=True):
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or (value <= low if low_open else value < low)
        or value > high
    ):
        raise ValueError(f"policy_swap.{name} must be a number in the reviewed range")
    return float(value)


def _values(argv):
    out = []
    for arg in argv:
        out.append(arg)
        if arg.startswith("-") and "=" in arg:
            out.append(arg.split("=", 1)[1])
    return out


def _ports(argv):
    found = []
    for index, arg in enumerate(argv):
        if arg == "--port":
            found.append(argv[index + 1] if index + 1 < len(argv) else "")
        elif arg.startswith("--port="):
            found.append(arg.split("=", 1)[1])
    return found


def roles_of(policies) -> tuple:
    """The policy roles a binding runs: ``("forward",)`` or forward and reset."""
    if set(policies) == {"forward"}:
        return ("forward",)
    if set(policies) == {"forward", "reset"}:
        return ROLES
    raise ValueError(
        "policy_swap needs the forward and reset roles of single_reset_policy, "
        "or the forward role alone of human_assisted"
    )


def _service(role, raw, *, endpoint, deployment):
    if not isinstance(raw, dict) or set(raw) != SERVICE_KEYS:
        raise ValueError(
            "policy_swap.services."
            + role
            + " requires exactly "
            + ", ".join(sorted(SERVICE_KEYS))
        )
    unit = raw["unit"]
    if not isinstance(unit, str) or not UNIT.fullmatch(unit):
        raise ValueError("policy_swap unit names must match levi-aeri-policy-<name>")
    argv = raw["argv"]
    if (
        not isinstance(argv, list)
        or not 2 <= len(argv) <= 64
        or any(not isinstance(arg, str) or not arg or len(arg) > 4096 for arg in argv)
    ):
        raise ValueError("policy_swap argv must be 2-64 nonempty strings")
    if any(FORBIDDEN.search(arg) for arg in argv):
        raise ValueError(
            "policy_swap argv refuses $, % and control characters (no expansion)"
        )
    if any(WHITESPACE.search(arg) for arg in argv):
        raise ValueError(
            "policy_swap argv refuses whitespace: systemd records ExecStart "
            "space-separated, so the passive identity check could never match"
        )
    program = argv[0]
    if (
        not os.path.isabs(program)
        or not os.path.isfile(program)
        or not os.access(program, os.X_OK)
    ):
        raise ValueError("policy_swap argv[0] must be an absolute executable file")
    directory = raw["working_directory"]
    if (
        not isinstance(directory, str)
        or FORBIDDEN.search(directory)
        or not os.path.isabs(directory)
        or not os.path.isdir(directory)
    ):
        raise ValueError("policy_swap working_directory must be an absolute directory")
    fraction = _number(raw["mem_fraction"], role + ".mem_fraction", low=0, high=0.5)
    port = endpoint["port"]
    ports = _ports(argv)
    if not ports or any(found != str(port) for found in ports):
        raise ValueError(
            "policy_swap argv must pass --port " + str(port) + " for " + role
        )
    config = deployment.get("config")
    if not isinstance(config, str) or not config:
        raise ValueError(role + " deployment config must be a frozen string")
    checkpoint = bridge.checkpoint_path(deployment.get("checkpoint_dir"))
    values = _values(argv)
    if config not in values:
        raise ValueError("policy_swap argv must name the frozen " + role + " config")
    named = deployment["checkpoint_dir"].rstrip("/")
    if not any(
        value.rstrip("/") == named
        or (os.path.isabs(value) and os.path.realpath(value) == checkpoint)
        for value in values
    ):
        raise ValueError(
            "policy_swap argv must name the absolute frozen " + role + " checkpoint_dir"
        )
    service = Service(
        role,
        unit,
        tuple(argv),
        directory,
        fraction,
        port,
        config,
        named,
    )
    if not runs_arm(list(argv), service.arm()):
        raise ValueError("policy_swap argv does not pass the passive identity check")
    return service


def parse(value, *, policies, deployments, safety) -> SwapConfig:
    """Validate the binding's ``policy_swap`` block; every field is explicit."""
    if not isinstance(value, dict) or set(value) != KEYS or value["schema"] != SCHEMA:
        raise ValueError("policy_swap requires exactly " + ", ".join(sorted(KEYS)))
    roles = roles_of(policies)
    services = value["services"]
    if not isinstance(services, dict) or set(services) != set(roles):
        raise ValueError("policy_swap.services requires exactly " + " and ".join(roles))
    parsed = {
        role: _service(
            role,
            services[role],
            endpoint=policies[role],
            deployment=deployments[role],
        )
        for role in roles
    }
    if len({service.unit for service in parsed.values()}) != len(parsed):
        raise ValueError("policy_swap needs two distinct unit names")
    vision = _number(value["vision_mem_fraction"], "vision_mem_fraction", low=0, high=1)
    total = _number(value["max_total_fraction"], "max_total_fraction", low=0, high=1)
    for service in parsed.values():
        if service.mem_fraction + vision > total + 1e-9:
            raise ValueError(
                "policy_swap: "
                + service.role
                + " mem_fraction + vision_mem_fraction exceeds max_total_fraction"
            )
    margin = value["gpu_margin_mib"]
    if type(margin) is not int or not 0 <= margin <= 32768:
        raise ValueError("policy_swap.gpu_margin_mib must be an integer MiB")
    timeouts = {
        name: _number(value[name], name, low=0, high=limit)
        for name, limit in TIMEOUT_LIMITS.items()
    }
    retries = value["start_retries"]
    if type(retries) is not int or retries not in (0, 1):
        raise ValueError("policy_swap.start_retries must be 0 or 1")
    window = value["still_window_ms"]
    if type(window) is not int or not 50 <= window <= 10_000:
        raise ValueError("policy_swap.still_window_ms must be an integer 50-10000")
    still_mm = _number(
        value["still_tolerance_mm"],
        "still_tolerance_mm",
        low=0,
        high=float(safety["home_tolerance_mm"]),
    )
    still_rad = _number(
        value["still_tolerance_rad"],
        "still_tolerance_rad",
        low=0,
        high=HOME_ROTATION_RAD,
    )
    return SwapConfig(
        parsed,
        vision,
        total,
        margin,
        timeouts["stop_timeout_s"],
        timeouts["start_timeout_s"],
        timeouts["handshake_timeout_s"],
        timeouts["warmup_timeout_s"],
        retries,
        window,
        still_mm,
        still_rad,
    )


def summary(cfg: SwapConfig) -> dict:
    """What ``--check`` prints: the reviewed switch, nothing measured."""
    return {
        "units": {role: s.unit for role, s in cfg.services.items()},
        "ports": {role: s.port for role, s in cfg.services.items()},
        "mem_fraction": {role: s.mem_fraction for role, s in cfg.services.items()},
        "vision_mem_fraction": cfg.vision_mem_fraction,
        "max_total_fraction": cfg.max_total_fraction,
        "timeouts_s": {
            "stop": cfg.stop_timeout_s,
            "start": cfg.start_timeout_s,
            "handshake": cfg.handshake_timeout_s,
            "warmup": cfg.warmup_timeout_s,
        },
        "start_retries": cfg.start_retries,
        "at_most_one_policy_service": True,
        "services_started_by_check": False,
    }


def start_argv(cfg: SwapConfig, service: Service, output=None) -> list:
    """The exact ``systemd-run`` command: fixed options, then the argv.
    ``output``, a file path, receives the unit's stdout and stderr."""
    capture = (
        []
        if output is None
        else [
            f"--property=StandardOutput=append:{output}",
            f"--property=StandardError=append:{output}",
        ]
    )
    return [
        "systemd-run",
        "--user",
        f"--unit={service.unit}",
        "--collect",
        "--property=KillMode=control-group",
        f"--property=TimeoutStopSec={max(1, math.ceil(cfg.stop_timeout_s))}",
        f"--working-directory={service.working_directory}",
        f"--setenv=XLA_PYTHON_CLIENT_MEM_FRACTION={service.mem_fraction!r}",
        *capture,
        "--",
        *service.argv,
    ]


# --- read-only GPU view ----------------------------------------------------------------


class Gpu:
    """``nvidia-smi`` queries only; an unreadable answer is never "free"."""

    def __init__(self, runner=None):
        self.runner = runner or UserSystemd(timeout_s=15.0)

    def memory(self) -> tuple[int, int]:
        code, out = self.runner.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.total,memory.free",
                "--format=csv,noheader,nounits",
            ]
        )
        lines = [line for line in out.splitlines() if line.strip()]
        try:
            if code != 0 or len(lines) != 1:
                raise ValueError(out[:200])
            total, free = (int(part.strip()) for part in lines[0].split(","))
        except ValueError as exc:
            raise SwapError(
                "gpu_unreadable", f"nvidia-smi exit {code}: {exc}"
            ) from None
        return total, free

    def compute_pids(self) -> set:
        code, out = self.runner.run(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,used_memory",
                "--format=csv,noheader,nounits",
            ]
        )
        if code != 0:
            raise SwapError("gpu_unreadable", f"nvidia-smi exit {code}")
        pids = set()
        for line in out.splitlines():
            if not line.strip():
                continue
            try:
                pids.add(int(line.split(",", 1)[0].strip()))
            except ValueError:
                raise SwapError("gpu_unreadable", line[:200]) from None
        return pids


# --- the service output ---------------------------------------------------------------


def tail_lines(path, start=0, count=TAIL_LINES) -> list:
    """The last ``count`` lines written to ``path`` since byte ``start``, made
    printable: undecodable bytes replaced, control characters (terminal
    escapes) dropped, each line cut to ``TAIL_LINE_CHARS``. Empty when the
    file is missing or unreadable: a missing log never hides the real error."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError:
        return []
    try:
        size = os.fstat(fd).st_size
        begin = max(start, size - TAIL_READ_BYTES, 0)
        os.lseek(fd, begin, os.SEEK_SET)
        data = os.read(fd, TAIL_READ_BYTES)
    except OSError:
        return []
    finally:
        os.close(fd)
    out = []
    for line in data.decode("utf-8", "replace").splitlines():
        line = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", line)
        if line.strip():
            out.append(line[:TAIL_LINE_CHARS])
    return out[-count:]


def _print_stderr(text) -> None:
    print(text, file=sys.stderr, flush=True)


# --- the switch log ----------------------------------------------------------------------


def service_log_dir(rollout_root) -> Path:
    return Path(rollout_root) / LOG_DIR


def log_path(rollout_root, run_id) -> Path:
    return Path(rollout_root) / LOG_DIR / f"{run_id}.jsonl"


def read_log(path) -> tuple[list, bool]:
    """``(records, torn)``. A line that does not parse (power loss) stays in
    place as ``{"record": "torn"}`` so its order against an acknowledgement
    is kept."""
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return [], False
    records, torn = [], False
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            record = None
        if not isinstance(record, dict) or "record" not in record:
            torn = True
            record = {"record": "torn"}
        records.append(record)
    return records, torn


def open_transaction(records) -> dict | None:
    """The oldest prepared transaction without its own committed/aborted line
    (a later transaction that ended does not close an earlier one)."""
    opened: dict = {}
    for record in records:
        if record["record"] == "prepared":
            opened[record.get("txn")] = record
        elif record["record"] in ("committed", "aborted"):
            opened.pop(record.get("txn"), None)
    return next(iter(opened.values()), None)


def unclean(records, torn=False) -> str | None:
    """Why a run's log needs an operator's check before the next run, or None.

    Only what follows the last operator acknowledgement counts. Clean means:
    no torn line, no transaction left open, and the last known service state
    is ``none`` (the run stopped its own unit) or was never set."""
    last = max(
        (i for i, r in enumerate(records) if r["record"] == "acknowledged"),
        default=-1,
    )
    recent = records[last + 1 :]
    if any(r["record"] == "torn" for r in recent) or (
        torn and not any(r["record"] == "torn" for r in records)
    ):
        return "torn log line"
    if open_transaction(recent) is not None:
        return "a switch was prepared and never ended"
    state = None
    for record in recent:
        if "service_state" in record:
            state = record["service_state"]
    if state not in (None, "none"):
        return f"last known policy service state: {state}"
    return None


def pending(rollout_root) -> list:
    """Logs under the rollout root that need an operator's acknowledgement."""
    folder = Path(rollout_root) / LOG_DIR
    try:
        paths = sorted(p for p in folder.iterdir() if p.suffix == ".jsonl")
    except FileNotFoundError:
        return []
    out = []
    for path in paths:
        if path.is_symlink() or not path.is_file():
            out.append((str(path), "not a regular file"))
            continue
        why = unclean(*read_log(path))
        if why is not None:
            out.append((str(path), why))
    return out


def _append(path, record) -> None:
    line = json.dumps(record, sort_keys=True) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        os.write(fd, line.encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def acknowledge(paths, *, by_run) -> None:
    for path in paths:
        _append(
            path,
            {
                "record": "acknowledged",
                "by_run": by_run,
                "wall_ns": time.time_ns(),
            },
        )


class SwapLog:
    """One run's switch transactions, appended and fsynced line by line."""

    def __init__(self, path):
        self.path = Path(path)
        self.records, self.torn = read_log(self.path)

    @classmethod
    def create(cls, path, *, run_id, units) -> "SwapLog":
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        os.close(fd)
        log = cls(path)
        log.append(
            {"record": "header", "schema": LOG_SCHEMA, "run_id": run_id, "units": units}
        )
        return log

    @classmethod
    def open_for_run(cls, path, *, run_id, units) -> "SwapLog":
        """This run's log: new, or one left clean by an attempt of the same
        run id that never reached the arm (for example a refused start)."""
        path = Path(path)
        if not path.exists() and not path.is_symlink():
            return cls.create(path, run_id=run_id, units=units)
        if path.is_symlink() or not path.is_file():
            raise ValueError(
                "the policy switch log is not a regular file: " + str(path)
            )
        log = cls(path)
        why = unclean(log.records)
        if why is not None:
            raise ValueError(
                "the policy switch log of this run id is not clean ("
                + why
                + "); confirm it through --execute or use a new run id"
            )
        log.append(
            {"record": "header", "schema": LOG_SCHEMA, "run_id": run_id, "units": units}
        )
        return log

    def append(self, record) -> None:
        record = {**record, "wall_ns": time.time_ns()}
        _append(self.path, record)
        self.records.append(record)

    def prepared_count(self) -> int:
        return sum(1 for r in self.records if r["record"] == "prepared")


# --- the switcher ------------------------------------------------------------------------


@dataclass
class RobotSide:
    """What the switch reads from the arm; bound once the robot exists."""

    pose: object  # () -> list[float] (x, y, z, rx, ry, rz)
    home_pose: list
    home_tolerance_mm: float
    fence: object  # state_machine.MotionFence
    hold: object  # () -> None
    motion_count: object  # () -> int
    warmup: object  # (role, client) -> actions
    cancelled: object  # () -> bool


def _rotation(a, b) -> float:
    return max(
        abs(math.remainder(float(x) - float(y), 2 * math.pi))
        for x, y in zip(a[3:6], b[3:6], strict=True)
    )


def _translation_mm(a, b) -> float:
    return 1000 * math.dist([float(x) for x in a[:3]], [float(x) for x in b[:3]])


class PolicySwitcher:
    def __init__(
        self,
        cfg: SwapConfig,
        *,
        log: SwapLog,
        handshake,
        run_id: str,
        systemd=None,
        proc_root=None,
        cgroup_root=CGROUP_ROOT,
        gpu: Gpu | None = None,
        clock=time.monotonic,
        sleep=time.sleep,
        poll_s: float = 0.5,
        service_log_dir=None,
        emit=None,
    ):
        if service_log_dir is not None and UNSAFE_PATH.search(str(service_log_dir)):
            raise ValueError(
                "the service log directory must not contain whitespace, $, % "
                "or control characters: " + str(service_log_dir)
            )
        self.service_log_dir = (
            None if service_log_dir is None else Path(service_log_dir)
        )
        self.emit = emit or _print_stderr
        # unit -> byte offset where this attempt's output begins
        self.output_marks: dict = {}
        # The last failure's service output, for the fault note.
        self.failure_tail: list = []
        self.cfg = cfg
        self.log = log
        self.handshake = handshake
        self.run_id = run_id
        self.systemd = systemd or UserSystemd()
        # Our units' own processes only: never a listing of /proc (root
        # AGENTS.md execution boundary, user 2026-10-14).
        self.proc = ProcView(proc_root, systemd=self.systemd, cgroup_root=cgroup_root)
        self.host = SystemdPolicyHost(
            systemd=self.systemd, proc_root=proc_root, cgroup_root=cgroup_root
        )
        self.gpu = gpu or Gpu()
        self.clock = clock
        self.sleep = sleep
        self.poll_s = poll_s
        self.robot: RobotSide | None = None
        self.active: str | None = None
        self.client = None
        self.warm = False
        self.latched: str | None = None
        # Units this switcher ran systemd-run for: the only ones it ever stops
        # at the run's end (a leftover of an earlier run is the operator's).
        self.started: set = set()
        # The unit the current transaction ran systemd-run for (None: none
        # yet). Only that unit is stopped when the transaction fails.
        self.launched: Service | None = None
        self.transactions = log.prepared_count()
        # A log with anything but its header belongs to an earlier process of
        # this run: its services may be in any state, so nothing is assumed.
        why = unclean(log.records, log.torn)
        if why is not None:
            self.latched = "earlier switch state of this run: " + why

    # --- small helpers -------------------------------------------------------------

    def bind_robot(self, robot: RobotSide) -> None:
        self.robot = robot

    def _cancelled(self) -> bool:
        return self.robot is not None and bool(self.robot.cancelled())

    def _record(self, record, **fields) -> None:
        self.log.append({"record": record, **fields})

    def _next_txn(self) -> str:
        self.transactions += 1
        return f"{self.run_id}:swap:{self.transactions:04d}"

    def _latch(self, exc: SwapError) -> None:
        if self.latched is None:
            self.latched = exc.code + (": " + exc.detail if exc.detail else "")

    # --- the service output ----------------------------------------------------------

    def _output(self, service: Service):
        if self.service_log_dir is None:
            return None
        return self.service_log_dir / f"{self.run_id}-{service.role}.service.log"

    def _prepare_output(self, service: Service):
        """Create (never follow a link) the private file the unit writes to
        and remember where this attempt's output begins."""
        path = self._output(service)
        if path is None:
            return None
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
            try:
                self.output_marks[service.unit] = os.fstat(fd).st_size
            finally:
                os.close(fd)
        except OSError as exc:
            raise SwapError("service_log_unusable", f"{path}: {exc}"[:300]) from None
        return path

    def _show_tail(self, service: Service, why: str) -> list:
        """Print the last lines this attempt's unit wrote to the terminal and
        return them for the switch log. Only ever called after a failure."""
        path = self._output(service)
        if path is None:
            return []
        lines = tail_lines(path, self.output_marks.get(service.unit, 0))
        self.failure_tail = lines
        head = f"[AERI] policy service {service.unit} ({why}); log {path}"
        if lines:
            head += f", last {len(lines)} lines:\n" + "\n".join(
                "  | " + line for line in lines
            )
        else:
            head += ": the unit wrote nothing"
        try:
            self.emit(head)
        except Exception as exc:  # noqa: BLE001 - a broken terminal never hides the fault
            self.failure_tail = [*lines, f"(terminal print failed: {exc})"]
        return lines

    def _tail_fields(self, service: Service, lines: list) -> dict:
        path = self._output(service)
        if path is None:
            return {}
        return {"service_log": str(path), "service_log_tail": lines}

    def _no_token(self) -> None:
        if self.robot is not None and self.robot.fence.current is not None:
            raise SwapError(
                "motion_token_present", "a motion token exists during a switch"
            )

    def unit_pids(self, unit: str) -> list:
        """The unit's own processes: its ``cgroup.procs``. Unreadable: refuse."""
        try:
            return self.proc.unit_pids(unit)
        except OSError:
            raise SwapError("proc_unreadable") from None

    def _foreign(self, port: int, unit: str) -> list:
        """Listeners on ``port`` not held by ``unit``'s processes (foreign or
        unknown: never looked up further)."""
        return [lsn for lsn in self.proc.listeners(port, (unit,)) if not lsn.pids]

    def _bounded(self, fn, timeout_s: float, code: str):
        box: dict = {}
        done = threading.Event()

        def work():
            try:
                box["value"] = fn()
            except BaseException as exc:  # noqa: BLE001 - the legacy client exits with SystemExit
                box["error"] = exc
            finally:
                done.set()

        threading.Thread(target=work, daemon=True, name=f"aeri-swap-{code}").start()
        deadline = self.clock() + timeout_s
        while not done.wait(self.poll_s):
            if self._cancelled():
                raise SwapError("operator_stop", code)
            if self.clock() >= deadline:
                raise SwapError(code + "_timeout", f"{timeout_s} s")
        if "error" in box:
            raise SwapError(code + "_refused", str(box["error"])[:300])
        return box["value"]

    # --- the arm --------------------------------------------------------------------

    def _home_and_still(self) -> list:
        robot = self.robot
        first = [float(x) for x in robot.pose()]
        if len(first) < 6 or not all(math.isfinite(x) for x in first):
            raise SwapError("arm_pose_unreadable")
        if (
            _translation_mm(first, robot.home_pose) > robot.home_tolerance_mm
            or _rotation(first, robot.home_pose) > HOME_ROTATION_RAD
        ):
            raise SwapError("arm_not_home", "a switch needs the arm at the home pose")
        self.sleep(self.cfg.still_window_ms / 1000)
        second = [float(x) for x in robot.pose()]
        self._still(first, second, "arm_moving")
        return second

    def _still(self, before, after, code) -> None:
        if (
            len(after) < 6
            or not all(math.isfinite(x) for x in after)
            or _translation_mm(before, after) > self.cfg.still_tolerance_mm
            or _rotation(before, after) > self.cfg.still_tolerance_rad
        ):
            raise SwapError(code, "the arm moved")

    def _warm(self, role: str, client) -> None:
        robot = self.robot
        before = robot.motion_count()
        self._no_token()
        actions = self._bounded(
            lambda: robot.warmup(role, client), self.cfg.warmup_timeout_s, "warmup"
        )
        rows = actions.tolist() if hasattr(actions, "tolist") else actions
        if (
            not isinstance(rows, list)
            or not rows
            or any(
                not isinstance(row, list)
                or len(row) != ACTION_DIMS
                or any(
                    isinstance(x, bool)
                    or not isinstance(x, int | float)
                    or not math.isfinite(x)
                    for x in row
                )
                for row in rows
            )
        ):
            raise SwapError("warmup_invalid", "warm-up actions are not finite (k, 7)")
        if robot.motion_count() != before:
            raise SwapError("motion_during_switch", "a motion step was counted")
        self._no_token()

    # --- services --------------------------------------------------------------------

    def _released(self, service: Service, pids) -> str | None:
        """Why the service is not fully gone yet (None: gone)."""
        state = self.host.unit_state(service.unit)
        if state not in QUIET_UNIT:
            return f"unit {state}"
        if self.proc.listeners(service.port):
            return f"port {service.port} still listening"
        try:
            alive = sorted(set(pids) & set(self.proc.unit_pids(service.unit)))
        except OSError:
            return f"{service.unit} cgroup unreadable"
        if alive:
            return f"processes {alive} still in {service.unit}"
        on_gpu = sorted(self.gpu.compute_pids() & set(pids))
        if on_gpu:
            return f"processes {on_gpu} still hold GPU memory"
        return None

    def _stop(self, service: Service, pids) -> None:
        self.systemd.run(["systemctl", "--user", "stop", f"{service.unit}.service"])
        deadline = self.clock() + self.cfg.stop_timeout_s
        while True:
            foreign = self._foreign(service.port, service.unit)
            if foreign:
                raise SwapError("foreign_listener", foreign[0].not_ours())
            why = self._released(service, pids)
            if why is None:
                return
            if self.clock() >= deadline:
                raise SwapError("stop_timeout", why)
            self.sleep(self.poll_s)

    def _gpu_budget(self, service: Service) -> dict:
        total, free = self.gpu.memory()
        need = math.ceil(service.mem_fraction * total) + self.cfg.gpu_margin_mib
        if free < need:
            raise SwapError(
                "gpu_budget", f"{free} MiB free, {need} MiB needed for {service.role}"
            )
        return {"total_mib": total, "free_mib": free, "need_mib": need}

    def _quiet(self, service: Service) -> None:
        state = self.host.unit_state(service.unit)
        if state not in QUIET_UNIT:
            raise SwapError(
                "unit_active",
                f"{service.unit} is {state}; check it, then: "
                f"systemctl --user stop {service.unit}.service",
            )
        if self.proc.listeners(service.port):
            raise SwapError("port_taken", f"port {service.port} is already in use")

    def _start(self, service: Service, txn: str) -> list:
        attempt = 0
        while True:
            self._quiet(service)
            self.started.add(service.unit)
            self.launched = service
            output = self._prepare_output(service)
            code, _ = self.systemd.run(start_argv(self.cfg, service, output))
            outcome = "start_failed"
            if code == 0:
                outcome = self._wait_ready(service)
                if outcome == "ready":
                    return self.unit_pids(service.unit)
            # A start that definitely failed: make sure it is gone first.
            self._stop(service, self.unit_pids(service.unit))
            if attempt >= self.cfg.start_retries:
                # ``_fail`` of the transaction shows the tail once.
                raise SwapError(outcome, f"{service.unit} did not become ready")
            tail = self._show_tail(service, outcome + ", retrying")
            attempt += 1
            self._record(
                "step",
                txn=txn,
                step="start_retry",
                detail=outcome,
                **self._tail_fields(service, tail),
            )

    def _wait_ready(self, service: Service) -> str:
        deadline = self.clock() + self.cfg.start_timeout_s
        arm = service.arm()
        while True:
            if self._cancelled():
                raise SwapError("operator_stop", "while the policy service started")
            ready = self.host.passive_ready(arm)
            if ready.status == "ready":
                return "ready"
            if ready.status == "mismatch":
                raise SwapError("not_our_service", ready.detail)
            if self.host.unit_state(service.unit) in QUIET_UNIT:
                return "exited_before_ready"
            if self.clock() >= deadline:
                raise SwapError("start_timeout", ready.detail)
            self.sleep(self.poll_s)

    def _abandon(self, service: Service) -> bool:
        """Stop a unit this run started; True when it is confirmed gone. A
        unit this run never started (a leftover, someone else's) is not ours
        to stop: nothing of ours runs under it."""
        if service.unit not in self.started:
            return True
        try:
            self._stop(service, self.unit_pids(service.unit))
            return True
        except SwapError:
            return False

    # --- the transactions -------------------------------------------------------------

    def _declared(self, role: str) -> Service:
        service = self.cfg.services.get(role)
        if service is None:
            error = SwapError(
                "role_not_configured",
                f"the binding has no {role} service (it declares "
                + ", ".join(self.cfg.services)
                + ")",
            )
            self._latch(error)
            self._record("refused", code=error.code, detail=error.detail)
            raise error
        return service

    def startup(self, role: str):
        """From no policy service to ``role`` before any arm I/O exists."""
        if self.latched:
            raise SwapError("latched", self.latched)
        service = self._declared(role)
        txn = self._next_txn()
        try:
            for other in self.cfg.services.values():
                self._quiet(other)
        except BaseException as exc:  # noqa: BLE001 - logged, latched, then re-raised
            error = as_swap_error(exc)
            self._latch(error)
            self._record(
                "refused", txn=txn, to_role=role, code=error.code, detail=error.detail
            )
            _reraise(exc)
        self._record(
            "prepared", txn=txn, from_role=None, to_role=role, unit=service.unit
        )
        began = self.clock()
        self.launched = None
        try:
            budget = self._gpu_budget(service)
            self._record("step", txn=txn, step="gpu_ok", **budget)
            pids = self._start(service, txn)
            self._record("step", txn=txn, step="ready", pids=pids)
            client = self._bounded(
                lambda: self.handshake(role), self.cfg.handshake_timeout_s, "handshake"
            )
            self._record("step", txn=txn, step="handshake")
        except BaseException as exc:  # noqa: BLE001 - logged, latched, then re-raised
            self._fail(txn, exc, self.launched, "none")
            _reraise(exc)
        self.active, self.client, self.warm = role, client, False
        self._record(
            "committed",
            txn=txn,
            service_state=role,
            elapsed_s=round(self.clock() - began, 3),
        )
        return client

    def _fail(self, txn, exc, touched: Service | None, state_if_untouched) -> None:
        # Latch first: neither the cleanup nor the log write can skip it.
        error = as_swap_error(exc)
        self._latch(error)
        state = state_if_untouched
        tail: dict = {}
        if touched is not None:
            state = "none" if self._abandon(touched) else "unknown"
            tail = self._tail_fields(touched, self._show_tail(touched, error.code))
        if state != self.active:
            self.active, self.client, self.warm = None, None, False
        self._record(
            "aborted",
            txn=txn,
            code=error.code,
            detail=error.detail[:300],
            service_state=state,
            **tail,
        )

    def ensure(self, role: str):
        """Make ``role`` the one running service; the new client, or None when
        it already was (then only read-only checks run: idempotent)."""
        if self.latched:
            raise SwapError("latched", self.latched)
        self._declared(role)
        if self.robot is None:
            error = SwapError("robot_not_bound", "a switch needs the arm's state")
            self._latch(error)
            raise error
        if self.active == role:
            service = self.cfg.services[role]
            ready = self.host.passive_ready(service.arm())
            if ready.status != "ready":
                error = SwapError("service_lost", ready.detail)
                self._latch(error)
                self.active = None
                tail = self._tail_fields(service, self._show_tail(service, error.code))
                self._record(
                    "fault",
                    code=error.code,
                    detail=error.detail,
                    service_state="unknown",
                    **tail,
                )
                raise error
            if not self.warm:
                try:
                    self._warm(role, self.client)
                except BaseException as exc:  # noqa: BLE001 - logged, latched, then re-raised
                    txn = self._next_txn()
                    self._record(
                        "prepared",
                        txn=txn,
                        from_role=role,
                        to_role=None,
                        unit=service.unit,
                    )
                    self._fail(txn, exc, service, "none")
                    _reraise(exc)
                self.warm = True
                self._record("warm", role=role)
            return None
        return self._switch(role)

    def _switch(self, role: str):
        old = self.cfg.services[self.active] if self.active else None
        new = self.cfg.services[role]
        txn = self._next_txn()
        try:
            self._no_token()
            self.robot.hold()
            before_pose = self._home_and_still()
            before_motion = self.robot.motion_count()
            self._no_token()
        except BaseException as exc:  # noqa: BLE001 - logged, latched, then re-raised
            error = as_swap_error(exc)
            self._latch(error)
            self._record(
                "refused",
                txn=txn,
                from_role=self.active,
                to_role=role,
                code=error.code,
                detail=error.detail,
                service_state=self.active or "none",
            )
            _reraise(exc)
        self._record(
            "prepared",
            txn=txn,
            from_role=self.active,
            to_role=role,
            from_unit=old.unit if old else None,
            unit=new.unit,
        )
        began = self.clock()
        state = self.active or "none"
        self.launched = None
        try:
            if old is not None:
                pids = self.unit_pids(old.unit)
                state = "unknown"
                self._stop(old, pids)
                state = "none"
                self.active, self.client, self.warm = None, None, False
                self._record("step", txn=txn, step="stopped", pids=pids)
            self._no_token()
            if self._cancelled():
                raise SwapError("operator_stop", "before the new service started")
            budget = self._gpu_budget(new)
            self._record("step", txn=txn, step="gpu_ok", **budget)
            pids = self._start(new, txn)
            self._record("step", txn=txn, step="ready", pids=pids)
            client = self._bounded(
                lambda: self.handshake(role), self.cfg.handshake_timeout_s, "handshake"
            )
            self._record("step", txn=txn, step="handshake")
            self._warm(role, client)
            self._record("step", txn=txn, step="warm")
            self._no_token()
            if self.robot.motion_count() != before_motion:
                raise SwapError("motion_during_switch", "a motion step was counted")
            self._still(before_pose, [float(x) for x in self.robot.pose()], "arm_moved")
        except BaseException as exc:  # noqa: BLE001 - logged, latched, then re-raised
            self._fail(txn, exc, self.launched, state)
            _reraise(exc)
        self.active, self.client, self.warm = role, client, True
        self._record(
            "committed",
            txn=txn,
            service_state=role,
            elapsed_s=round(self.clock() - began, 3),
        )
        return client

    def shutdown(self) -> None:
        """Stop the units this run started and confirm they released port,
        processes and GPU memory. A unit it never started is left alone."""
        mine = [s for s in self.cfg.services.values() if s.unit in self.started]
        if not mine:
            return
        txn = self._next_txn()
        self._record("prepared", txn=txn, from_role=self.active, to_role=None)
        try:
            for service in mine:
                pids = self.unit_pids(service.unit)
                if pids or self.host.unit_state(service.unit) not in QUIET_UNIT:
                    self._stop(service, pids)
        except BaseException as exc:  # noqa: BLE001 - logged, latched, then re-raised
            self.active = None
            error = as_swap_error(exc)
            self._latch(error)
            for service in mine:
                self._show_tail(service, "shutdown " + error.code)
            self._record(
                "aborted",
                txn=txn,
                code=error.code,
                detail=error.detail[:300],
                service_state="unknown",
            )
            _reraise(exc)
        self.active, self.client, self.warm = None, None, False
        self._record("committed", txn=txn, service_state="none")


class SwitchingPolicy(bridge.Policy):
    """``fr3_bridge.Policy`` whose ``acquire`` first makes the role's service
    the one running. A switch failure is a fault, never a human wait."""

    def __init__(self, switcher: PolicySwitcher, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.switcher = switcher

    def _fault_detail(self, exc) -> str:
        """The note keeps 300 characters: the cause first, then as much of the
        service's last output as fits (the full tail is on the terminal and in
        the switch log)."""
        head = str(exc)[:150]
        tail = " / ".join(line[:80] for line in self.switcher.failure_tail[-3:])
        return (head + (" | service: " + tail if tail else ""))[:300]

    def acquire(self, role, *, policy_epoch, prompt=None):
        if self.ticket is not None and not self.ticket.done.is_set():
            return {"kind": "unavailable", "code": "query_still_running"}
        try:
            client = self.switcher.ensure(role)
        except SwapError as exc:
            return {
                "kind": "unavailable",
                "code": "policy_switch_fault",
                "detail": self._fault_detail(exc),
                "fault": True,
            }
        if client is not None:
            self.clients.clear()
            self.clients[role] = client
        return super().acquire(role, policy_epoch=policy_epoch, prompt=prompt)
