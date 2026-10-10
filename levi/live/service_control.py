"""Start and stop the live service from the product LEVI's page.

``/api/levi/live/service`` (``api.py``) shows how the background live service
runs and, when a person asks on the page, starts or stops it. Everything that
decides lives here; the routes only check who asks.

How it starts: the product core runs ``systemctl --user start <unit>`` (fixed
arguments, no shell; the unit is ``LEVI_LIVE_UNIT``, default
``levi-live.service``, the one ``docs/SUPERVISION.md`` installs). systemd then
starts the service in the unit's own cgroup. It never runs ``levi live start
--daemon`` itself: a process the product core starts lives in the product
unit's cgroup (``start_new_session`` changes the session, not the cgroup), and
``KillMode=control-group`` would stop the live service, and its vLLM, every
time the product LEVI restarts. The product core is only the control plane,
never the parent.

How it stops: a service the unit runs is stopped with ``systemctl --user stop
<unit>`` (whose ``ExecStop`` is ``levi live stop``); one started from a
terminal (``levi live start [--daemon]``) gets the same SIGTERM ``levi live
stop`` sends, after its identity (pid, start time, boot) is checked.

Which service runs, three ways (``discover``): the holder of the live home's
instance lock (``controller.Instance.holder``) is the unit's ``MainPID`` or
lies in the unit's cgroup (``unit``), or it does not (``terminal``); a
process this service does not own that listens on one of its ports is
``foreign`` (read from ``/proc/net/tcp*`` and ``/proc/<pid>/fd``: nothing
connects to a port). The GPU lock's holder comes from ``/proc/locks`` (no lock
is taken), the compute processes from ``nvidia-smi --query-compute-apps``,
the vLLM state from the service's status file (no request to vLLM).

Before a start (``preflight``): the unit is installed and not running or
failed (``reset_failed`` asks for ``systemctl --user reset-failed`` first), no
instance runs from a terminal, the core, online-judgement and vLLM ports are
not held by anyone else, the live home, the live workspace and this LEVI's
workspace are writable, and when another process computes on the GPU the
request says ``confirm_gpu_shared`` (the unit runs ``--prewarm``: starting it
cold-starts vLLM next to that process). Before a stop: no evaluation session
is running, homing or waiting for its reset, unless the request carries
``FORCE_PHRASE``.

One operation at a time per live home (a thread lock in this process and an
``flock`` on ``<home>/ui-op.lock`` across processes): a second request while
one runs gets 423 and the running operation's id; the same ``request_id``
gets the same operation back. Every start and stop (accepted, finished,
refused) is one line in ``<live workspace>/live/service-control.jsonl``
(at most ``AUDIT_MAX_BYTES``, one older file kept), with no path, token or
key in it.

Off unless ``LEVI_LIVE_SERVICE_CONTROL=1``: the status route always answers,
the start and stop routes answer 403 ``disabled`` until it is set.
"""

import contextlib
import fcntl
import os
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from . import config as live_config
from . import gpumgr, jsonio, locate, sessions
from .controller import Instance

ENV_UNIT = "LEVI_LIVE_UNIT"
ENV_ENABLED = "LEVI_LIVE_SERVICE_CONTROL"
DEFAULT_UNIT = "levi-live.service"
# ``levi-live.service`` or a variant of it (``levi-live-cold.service``,
# ``levi-live@x.service``): never another unit, never the product's own.
UNIT_NAME = re.compile(r"^levi-live(?:[-@_.:][A-Za-z0-9@._:-]{0,100})?\.service$")
FORBIDDEN_IN_UNIT = "levi-product"
REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{8,80}$")

# The fixed words a request carries to say it means it.
START_CONFIRM = "start-live"
STOP_CONFIRM = "stop-live"
FORCE_PHRASE = "stop live during evaluation"

# Resolved once from the system folders, not from PATH (``levi`` puts its own
# tool folders first).
SYSTEMCTL = shutil.which("systemctl", path="/usr/bin:/bin") or "/usr/bin/systemctl"
PROC = Path("/proc")
SHOW_TIMEOUT_S = 10.0
# The status route answers within about this much: a slower systemctl,
# nvidia-smi or /proc scan is shown as unknown.
SHOW_STATUS_TIMEOUT_S = 3.0
PROBE_TIMEOUT_S = 3.0
# How long the status route reuses the last nvidia-smi answer and /proc scan
# (a start or a stop always looks again).
PROBE_CACHE_S = 3.0
START_TIMEOUT_S = 60.0
# The unit's TimeoutStopSec is 180 s; systemctl waits for the stop job.
STOP_TIMEOUT_S = 210.0
# After a start: how long to wait for the supervisor to take its instance lock.
SETTLE_S = 30.0
# A terminal-started service: how long ``levi live stop`` waits too.
SIGNAL_STOP_S = 150.0
POLL_S = 0.5

AUDIT = "service-control.jsonl"
AUDIT_MAX_BYTES = 256 * 1024
AUDIT_KEEP = 1
OP_LOCK = "ui-op.lock"
MAX_OPS = 50
DETAIL_MAX = 300

# The unit's states during which it is running or about to.
RUNNING_STATES = ("active", "activating", "reloading")

sleep = time.sleep  # replaced by the tests


class Refused(Exception):
    """A request this module will not carry out: an HTTP status, a stable
    code the page can translate, a sentence, and extra fields."""

    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra
        self.audited = False

    def public(self) -> dict:
        return {"code": self.code, "message": self.message, **self.extra}


class Busy(Refused):
    def __init__(self, operation_id=None):
        super().__init__(
            423,
            "busy",
            "Another start or stop of the live service is in progress",
            operation_id=operation_id,
        )


class SystemdError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


def enabled() -> bool:
    return (os.environ.get(ENV_ENABLED) or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def unit_name() -> tuple:
    """``(name, problem)``: the unit ``LEVI_LIVE_UNIT`` names (``.service``
    added when left out). ``unit_name_invalid`` for anything but a
    ``levi-live*.service`` name or one that names ``levi-product`` (the page
    must never stop the product LEVI itself; the name is passed to systemctl
    as one argument, never to a shell); ``unit_is_self`` for the unit this
    process runs in."""
    name = (os.environ.get(ENV_UNIT) or DEFAULT_UNIT).strip()
    if name and not name.endswith(".service"):
        name += ".service"
    if not UNIT_NAME.fullmatch(name) or FORBIDDEN_IN_UNIT in name:
        return None, "unit_name_invalid"
    if cgroup_unit(os.getpid()) == name:
        return None, "unit_is_self"
    return name, None


# --- small helpers ------------------------------------------------------------------


_PATH = re.compile(r"(?<![\w.~-])(?:~|\.{1,2})?/(?:[^\s/:;,'\"()\[\]{}]+/)*")


def scrub(text, limit=DETAIL_MAX) -> str:
    """A message for the page and the audit: folders cut off (a path shows
    only its last name), one line, bounded."""
    text = _PATH.sub("", str(text or ""))
    return " ".join(text.split())[:limit]


def _read(path) -> str | None:
    try:
        return Path(path).read_text()
    except (OSError, UnicodeDecodeError):
        return None


def comm(pid, proc=None) -> str | None:
    text = _read((proc or PROC) / str(pid) / "comm")
    return text.strip()[:32] if text else None


def parent(pid, proc=None) -> int | None:
    text = _read((proc or PROC) / str(pid) / "stat")
    try:
        return int(text.rsplit(")", 1)[1].split()[1]) if text else None
    except (IndexError, ValueError):
        return None


def lineage(pid, proc=None, depth=16) -> list:
    """``pid`` and its ancestors (nearest first, up to ``depth``)."""
    out = []
    while pid and pid > 1 and len(out) < depth and pid not in out:
        out.append(pid)
        pid = parent(pid, proc)
    return out


def cgroup_unit(pid, proc=None) -> str | None:
    """The systemd service whose cgroup holds ``pid`` (the last
    ``*.service`` of its cgroup path), None when it is in none or cannot be
    read."""
    text = _read((proc or PROC) / str(pid) / "cgroup")
    if not text:
        return None
    for line in text.splitlines():
        path = line.split(":", 2)[-1]
        names = [p for p in path.split("/") if p.endswith(".service")]
        if names:
            return names[-1]
    return None


# --- systemd ------------------------------------------------------------------------


_BUS = ("failed to connect to bus", "no medium found", "transport endpoint")


def systemctl(*args, timeout: float) -> tuple:
    """Run ``systemctl --user <args>`` (a fixed argument list, no shell):
    ``(returncode, stdout, stderr)``. ``SystemdError`` with
    ``systemd_unavailable`` (no systemctl, no user bus), ``systemd_timeout``
    or ``permission_denied``."""
    argv = [SYSTEMCTL, "--user", *args]
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SystemdError("systemd_unavailable", "systemctl was not found") from exc
    except PermissionError as exc:
        raise SystemdError("permission_denied", "systemctl cannot be run") from exc
    except subprocess.TimeoutExpired as exc:
        raise SystemdError(
            "systemd_timeout",
            f"systemctl {args[0]} did not answer within {timeout:g} s",
        ) from exc
    except OSError as exc:
        raise SystemdError("systemd_unavailable", type(exc).__name__) from exc
    err = done.stderr or ""
    if done.returncode != 0 and any(m in err.lower() for m in _BUS):
        raise SystemdError("systemd_unavailable", scrub(err))
    return done.returncode, done.stdout or "", err


SHOW = (
    "LoadState",
    "ActiveState",
    "SubState",
    "MainPID",
    "ExecMainStatus",
    "NRestarts",
    "ExecStart",
)


def _number(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def exec_argv(value: str) -> list:
    """The command line of ``ExecStart=`` as ``systemctl show`` prints it
    (``{ path=… ; argv[]=a b c ; … }``)."""
    found = re.search(r"argv\[\]=(.*?)(?: ;|$)", value or "")
    return found.group(1).split() if found else []


def live_verb(argv) -> str | None:
    """The ``levi live`` subcommand a command line runs (``start``,
    ``once``…), None when it is not ``levi live``."""
    for i, token in enumerate(argv[:-1]):
        if token in ("live", "levi.live"):
            return argv[i + 1]
    return None


def cmdline(pid, proc=None) -> list | None:
    try:
        raw = ((proc or PROC) / str(pid) / "cmdline").read_bytes()
    except OSError:
        return None
    return [a.decode(errors="replace") for a in raw.split(b"\0") if a] or None


def unit_state(unit, timeout=SHOW_TIMEOUT_S) -> dict:
    """What systemd says of the unit. Never raises: a systemd that cannot be
    asked gives ``problem``. ``runs_live``: its ``ExecStart`` is ``levi
    live start``; ``flapping``: it is waiting to be started again after a
    crash (``auto-restart``)."""
    out = {
        "name": unit,
        "installed": False,
        "load_state": None,
        "state": None,
        "sub_state": None,
        "main_pid": None,
        "exec_main_status": None,
        "n_restarts": None,
        "ui": False,
        "runs_live": False,
        "flapping": False,
        "workspace": None,
        "problem": None,
    }
    try:
        code, text, err = systemctl(
            "show", unit, "--property=" + ",".join(SHOW), timeout=timeout
        )
    except SystemdError as exc:
        out["problem"] = exc.code
        return out
    if code != 0:
        out["problem"] = "systemd_unavailable"
        out["detail"] = scrub(err)
        return out
    values = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    argv = exec_argv(values.get("ExecStart", ""))
    workspace = None
    if "--workspace" in argv[:-1]:
        workspace = argv[argv.index("--workspace") + 1]
    out.update(
        load_state=values.get("LoadState") or None,
        installed=values.get("LoadState") == "loaded",
        state=values.get("ActiveState") or None,
        sub_state=values.get("SubState") or None,
        main_pid=_number(values.get("MainPID")) or None,
        exec_main_status=_number(values.get("ExecMainStatus")),
        n_restarts=_number(values.get("NRestarts")),
        ui="--ui" in argv and "--no-ui" not in argv,
        runs_live=live_verb(argv) == "start",
        flapping=values.get("SubState") == "auto-restart",
        workspace=workspace,
    )
    return out


# --- what the machine is doing ------------------------------------------------------


def listening(proc=None) -> dict:
    """``{port: {socket inode, …}}`` of every TCP socket in LISTEN state."""
    proc = proc or PROC
    found = {}
    for table in ("net/tcp", "net/tcp6"):
        text = _read(proc / table)
        for line in (text or "").splitlines()[1:]:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A":
                continue
            try:
                port = int(fields[1].rsplit(":", 1)[1], 16)
            except (IndexError, ValueError):
                continue
            found.setdefault(port, set()).add(fields[9])
    return found


def socket_owners(inodes, proc=None) -> dict:
    """``{inode: pid}`` for the sockets whose holder this user can see
    (another user's ``/proc/<pid>/fd`` cannot be read: no entry)."""
    proc = proc or PROC
    wanted = {f"socket:[{i}]": i for i in inodes}
    owners = {}
    if not wanted:
        return owners
    try:
        pids = [int(e.name) for e in os.scandir(proc) if e.name.isdigit()]
    except OSError:
        return owners
    for pid in sorted(pids):
        try:
            entries = list(os.scandir(proc / str(pid) / "fd"))
        except OSError:
            continue
        for fd in entries:
            try:
                link = os.readlink(fd.path)
            except OSError:
                continue
            inode = wanted.get(link)
            if inode is not None and inode not in owners:
                owners[inode] = pid
        if len(owners) == len(wanted):
            break
    return owners


def lock_holders(lock_file, proc=None) -> list:
    """Pids that hold a lock on ``lock_file`` (``/proc/locks``; blocked
    waiters, the ``->`` lines, are not holders). Nothing is locked here."""
    try:
        st = os.stat(Path(lock_file).expanduser())
    except OSError:
        return []
    exact = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
    text = _read((proc or PROC) / "locks") or ""
    same, inode_only = [], []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 6 or "->" in fields:
            continue
        pid, where = _number(fields[4]), fields[5]
        if pid is None:
            continue
        if where == exact:
            same.append(pid)
        elif where.rsplit(":", 1)[-1] == str(st.st_ino):
            # Some file systems (btrfs, overlay) report another device number
            # in /proc/locks than stat() does.
            inode_only.append(pid)
    return same or inode_only


def gpu_processes() -> list | None:
    """Compute processes on the GPU (``nvidia-smi``, read-only), None when
    they cannot be read."""
    failed = []

    def runner(*args, **kwargs):
        try:
            return subprocess.run(*args, check=kwargs.pop("check", False), **kwargs)
        except (OSError, subprocess.SubprocessError):
            failed.append(True)
            raise

    rows = gpumgr.gpu_holders(runner)
    return None if failed else rows


def scan_ports(wanted) -> tuple:
    """``({port: inodes}, {inode: pid})`` for the ``wanted`` ports: the
    kernel's socket table and a walk over every visible ``/proc/<pid>/fd``."""
    table = {p: i for p, i in listening().items() if p in wanted}
    return table, socket_owners(set().union(*table.values()) if table else set())


class Probe:
    """One slow look (nvidia-smi, the /proc walk) shared by every caller:
    at most one runs at a time (in a thread of its own), its answer is
    reused for ``max_age`` seconds, and a caller waits at most ``timeout``
    seconds, after which it gets ``(False, None)`` (shown as unknown) while
    the look goes on for the next caller."""

    def __init__(self):
        self.lock = threading.Lock()
        self.result = None  # (key, started, ok, value)
        self.thread = None

    def _run(self, fn, key, started):
        try:
            ok, value = True, fn()
        except Exception:  # noqa: BLE001 - an unreadable look is unknown
            ok, value = False, None
        with self.lock:
            self.result = (key, started, ok, value)
            self.thread = None

    def get(self, fn, *, key=None, max_age=0.0, timeout=None) -> tuple:
        timeout = PROBE_TIMEOUT_S if timeout is None else timeout
        need = time.monotonic() - max_age
        deadline = time.monotonic() + timeout
        while True:
            with self.lock:
                found = self.result
                if found and found[0] == key and found[1] >= need:
                    return found[2], found[3]
                if self.thread is None:
                    self.thread = threading.Thread(
                        target=self._run,
                        args=(fn, key, time.monotonic()),
                        name="live-service-probe",
                        daemon=True,
                    )
                    self.thread.start()
                thread = self.thread
            left = deadline - time.monotonic()
            if left <= 0:
                return False, None
            thread.join(left)
            if thread.is_alive():
                return False, None


GPU_PROBE = Probe()
PORT_PROBE = Probe()


# --- discovery ------------------------------------------------------------------------


@dataclass
class Target:
    """What one request is about: the live configuration in force (ports,
    GPU lock, rollout roots, home), the live workspace found (None: none),
    whether this LEVI is that workspace's own core, and this LEVI's own
    workspace."""

    config: live_config.Config
    workspace: Path | None
    served_by_live: bool = False
    product_workspace: Path | None = None
    unit: str | None = None
    unit_problem: str | None = None

    def __post_init__(self):
        if self.unit is None and self.unit_problem is None:
            self.unit, self.unit_problem = unit_name()


def holder(target) -> dict | None:
    return Instance(target.config.home).holder()


def _ours(pid, holder_pid, unit, proc=None) -> bool:
    """Is ``pid`` part of the live service: the supervisor or a process it
    started, or anything in the unit's cgroup?"""
    if not pid:
        return False
    if holder_pid and holder_pid in lineage(pid, proc):
        return True
    return bool(unit) and cgroup_unit(pid, proc) == unit


def ports(target, holder_pid, max_age=0.0) -> list:
    c = target.config
    wanted = [
        (c.service.core_port, "core"),
        (c.online.port, "online"),
        (c.vllm.port, "vllm"),
        (c.service.ui_port, "ui"),
    ]
    key = tuple(sorted(p for p, _ in wanted))
    ok, scan = PORT_PROBE.get(lambda: scan_ports(key), key=key, max_age=max_age)
    table, owners = scan if ok else ({}, {})
    rows = []
    for port, role in wanted:
        held = table.get(port)
        row = {"port": port, "role": role, "state": "free", "pid": None, "name": None}
        if not ok:
            row["state"] = "unknown"
        elif held:
            pids = sorted({owners[i] for i in held if i in owners})
            mine = [p for p in pids if _ours(p, holder_pid, target.unit)]
            if pids and len(mine) == len(pids):
                row.update(state="ours", pid=pids[0], name=comm(pids[0]))
            else:
                other = next((p for p in pids if p not in mine), None)
                row.update(
                    state="foreign", pid=other, name=comm(other) if other else None
                )
        rows.append(row)
    return rows


def gpu(target, holder_pid, alive_status, max_age=0.0) -> dict:
    lock_file = target.config.gpu.lock_file
    lock = {
        "configured": bool(lock_file),
        "state": "disabled",
        "pid": None,
        "name": None,
    }
    if lock_file:
        pids = lock_holders(lock_file)
        if not pids:
            lock["state"] = "free"
        else:
            mine = [p for p in pids if _ours(p, holder_pid, target.unit)]
            if mine:
                lock.update(state="live", pid=mine[0])
            else:
                lock.update(state="other", pid=pids[0], name=comm(pids[0]))
    ok, found = GPU_PROBE.get(lambda: gpu_processes(), max_age=max_age)
    state = "ok" if ok and found is not None else "unreadable" if ok else "unknown"
    others = None
    if state == "ok":
        others = [
            {"pid": r["pid"], "name": r.get("name"), "memory_mib": r.get("memory_mib")}
            for r in found
            if not _ours(r["pid"], holder_pid, target.unit)
        ]
    vllm = "stopped"
    if alive_status:
        vllm = ((alive_status.get("gpu") or {}).get("vllm_state")) or None
    return {"state": state, "lock": lock, "others": others, "vllm_state": vllm}


def evaluation(target) -> dict:
    """The evaluation sessions running, homing or waiting for their reset.
    ``active`` is None when it cannot be told (no live workspace found, or
    no rollout roots in its configuration): a stop then treats it as an
    evaluation in progress."""
    if target.workspace is None or not target.config.watch.roots:
        return {"active": None, "count": None, "sessions": []}
    found = sessions.read_sessions(target.config.watch.roots)
    active = [s for s in found.values() if s.active()]
    return {
        "active": bool(active),
        "count": len(active),
        "sessions": [
            {"group": s.group, "task_folder": s.task_folder, "state": s.state}
            for s in active[:10]
        ],
    }


def _writable(path) -> bool:
    """``path``, or the nearest folder above it that exists, is writable."""
    path = Path(path).expanduser()
    for candidate in (path, *path.parents):
        if candidate.exists():
            return os.access(candidate, os.W_OK | os.X_OK)
    return False


def _check(code, level, message):
    return {"code": code, "level": level, "message": message}


def classify(record, unit, target) -> str:
    """Who holds the live home: ``unit`` (the unit's ``MainPID`` or in its
    cgroup), ``terminal`` (a resident ``levi live start`` started from a
    terminal), ``terminal_once`` (a ``levi live once`` batch: possibly
    another session's GPU window) or ``other`` (cannot be told)."""
    pid = record["pid"]
    if (unit.get("main_pid") and unit["main_pid"] == pid) or (
        target.unit and cgroup_unit(pid) == target.unit
    ):
        return "unit"
    verb = live_verb(cmdline(pid) or [])
    return {"start": "terminal", "once": "terminal_once"}.get(verb, "other")


def discover(target, fresh=False) -> dict:
    """Everything the page shows and a start or stop decides on. Reads only:
    systemd's view of the unit, the live home's pid record, the kernel's
    socket, lock and cgroup tables, ``nvidia-smi``, the status file and the
    evaluation sessions' files. ``fresh`` (a start or a stop): nothing
    cached, the longer systemctl timeout."""
    max_age = 0.0 if fresh else PROBE_CACHE_S
    if target.unit:
        unit = unit_state(
            target.unit, timeout=SHOW_TIMEOUT_S if fresh else SHOW_STATUS_TIMEOUT_S
        )
    else:
        unit = {"name": None, "installed": False, "state": None, "problem": None}
    record = holder(target)
    holder_pid = record["pid"] if record else None
    started_by = None
    cgroup = None
    if record:
        cgroup = cgroup_unit(holder_pid)
        started_by = classify(record, unit, target)
    status = jsonio.read(target.config.status_file)
    alive_status = (
        status
        if isinstance(status, dict) and record and status.get("pid") == holder_pid
        else None
    )
    return {
        "unit": unit,
        "holder": {
            "pid": holder_pid,
            "alive": bool(record),
            "started_by": started_by,
            "cgroup_unit": cgroup,
            "name": comm(holder_pid) if record else None,
            "started_at": (record or {}).get("started_at"),
        },
        "ports": ports(target, holder_pid, max_age),
        "gpu": gpu(target, holder_pid, alive_status, max_age),
        "evaluation": evaluation(target),
    }


# --- what may be done ------------------------------------------------------------


UNIT_PROBLEMS = {
    "unit_name_invalid": f"{ENV_UNIT} must name a levi-live*.service unit, never levi-product",
    "unit_is_self": f"{ENV_UNIT} names the unit this LEVI itself runs in",
}


def _unit_checks(target, seen, out) -> None:
    """Refusals shared by a start and a stop: the page is the live service's
    own core; the unit is not one this page may drive."""
    unit = seen["unit"]
    if target.served_by_live:
        out.append(
            _check(
                "served_by_live_service",
                "refuse",
                "This page is served by the live service itself: start and stop it from the product LEVI",
            )
        )
    if target.unit_problem:
        out.append(
            _check(target.unit_problem, "refuse", UNIT_PROBLEMS[target.unit_problem])
        )
    elif unit.get("problem"):
        out.append(
            _check(
                unit["problem"],
                "refuse",
                "systemd's user instance cannot be asked: use `levi live` in a terminal",
            )
        )
    elif not unit.get("installed"):
        out.append(
            _check(
                "unit_not_installed",
                "refuse",
                f"The unit {target.unit} is not installed (docs/SUPERVISION.md): use `levi live` in a terminal",
            )
        )
    elif not unit.get("runs_live"):
        out.append(
            _check(
                "unit_not_live",
                "refuse",
                f"The unit {target.unit} does not run `levi live start`",
            )
        )


def _holder_checks(held, out) -> None:
    """A holder of the live home that is not a resident live service is
    never signalled: a ``levi live once`` (possibly another session's GPU
    window) or a process that cannot be told."""
    who = f"pid {held['pid']} ({held.get('name') or '?'})"
    if held["started_by"] == "terminal_once":
        out.append(
            _check(
                "once_instance",
                "refuse",
                f"The live home is held by a `levi live once` batch, {who}: it is not the service and is left alone",
            )
        )
    elif held["started_by"] == "other":
        out.append(
            _check(
                "holder_unidentified",
                "refuse",
                f"The live home is held by {who}, which is not `levi live start`: it is left alone",
            )
        )


def start_checks(target, seen) -> list:
    """The preflight of a start, as checks: ``refuse`` (a start would be
    refused), ``confirm`` (a start needs the request's confirmation),
    ``warn`` and ``ok``."""
    out = []
    unit, held = seen["unit"], seen["holder"]
    _unit_checks(target, seen, out)
    if target.workspace is None:
        out.append(
            _check(
                "no_live_workspace",
                "refuse",
                "No live workspace is known to this LEVI (LEVI_LIVE_WORKSPACE, or one `levi live start` from a terminal)",
            )
        )
    if held["alive"]:
        if held["started_by"] in ("terminal_once", "other"):
            _holder_checks(held, out)
        elif held["started_by"] == "terminal":
            out.append(
                _check(
                    "terminal_instance",
                    "refuse",
                    f"The live service runs, started from a terminal (pid {held['pid']}): stop it before starting the unit",
                )
            )
        else:
            out.append(
                _check("already_running", "refuse", "The live service already runs")
            )
    elif unit.get("flapping"):
        out.append(
            _check(
                "unit_flapping",
                "refuse",
                f"The unit keeps failing and is restarted by systemd ({unit.get('n_restarts')} restarts): "
                f"see journalctl --user -u {target.unit}, then stop it",
            )
        )
    elif unit.get("state") in RUNNING_STATES:
        out.append(
            _check(
                "already_running", "refuse", "The unit is already starting or running"
            )
        )
    elif unit.get("state") == "deactivating":
        out.append(
            _check("unit_busy", "refuse", "The unit is stopping: try again in a moment")
        )
    elif unit.get("state") == "failed":
        out.append(
            _check(
                "unit_failed",
                "confirm",
                "The unit failed last time: a start resets it first (reset_failed)",
            )
        )
    if any(row["state"] == "unknown" for row in seen["ports"]):
        out.append(
            _check(
                "ports_unknown",
                "refuse",
                "Who listens on the service's ports could not be read in time: try again",
            )
        )
    for row in seen["ports"]:
        if row["state"] != "foreign":
            continue
        if row["role"] == "ui" and not unit.get("ui"):
            continue
        who = (
            f"pid {row['pid']} ({row['name'] or '?'})"
            if row["pid"]
            else "another user's process"
        )
        out.append(
            _check(
                "port_foreign",
                "refuse",
                f"Port {row['port']} ({row['role']}) is held by {who}, not by the live service",
            )
        )
    for name, path in (
        ("live home", target.config.home),
        ("live workspace", target.workspace / "live" if target.workspace else None),
        ("LEVI workspace", target.product_workspace),
    ):
        if path is not None and not _writable(path):
            out.append(_check("not_writable", "refuse", f"The {name} is not writable"))
    gpu_seen = seen["gpu"]
    if gpu_seen["others"] is None:
        out.append(
            _check(
                "gpu_shared",
                "confirm",
                "What runs on the GPU cannot be read (nvidia-smi): starting cold-starts vLLM",
            )
        )
    elif gpu_seen["others"]:
        names = ", ".join(
            f"{r['name'] or '?'} (pid {r['pid']})" for r in gpu_seen["others"][:4]
        )
        out.append(
            _check(
                "gpu_shared",
                "confirm",
                f"Other processes compute on the GPU ({names}): starting cold-starts vLLM next to them",
            )
        )
    if gpu_seen["lock"]["state"] == "other":
        out.append(
            _check(
                "gpu_lock_held",
                "warn",
                f"The GPU lock is held by pid {gpu_seen['lock']['pid']}: the service starts, vLLM waits for the lock",
            )
        )
    if (
        target.workspace is not None
        and unit.get("workspace")
        and Path(unit["workspace"]).expanduser().resolve() != target.workspace.resolve()
    ):
        out.append(
            _check(
                "unit_workspace_mismatch",
                "warn",
                "The unit runs another live workspace than the one this page shows",
            )
        )
    return out


def stop_checks(target, seen) -> list:
    out = []
    unit, held = seen["unit"], seen["holder"]
    running = held["alive"] or unit.get("state") in (*RUNNING_STATES, "deactivating")
    if not running:
        out.append(_check("not_running", "refuse", "The live service is not running"))
        return out
    _unit_checks(target, seen, out)
    if held["alive"]:
        _holder_checks(held, out)
    if seen["evaluation"]["active"] is None:
        out.append(
            _check(
                "evaluation_unknown",
                "confirm",
                "Whether an evaluation session is running cannot be told (no live workspace or no rollout roots "
                "known): stopping during one ends the online judgement and the automatic labels",
            )
        )
    elif seen["evaluation"]["active"]:
        out.append(
            _check(
                "evaluation_active",
                "confirm",
                "An evaluation session is running: stopping ends the online judgement and the automatic labels; "
                "the client falls back to manual labels",
            )
        )
    return out


def status(target) -> dict:
    """``GET /api/levi/live/service``."""
    seen = discover(target)
    starts = start_checks(target, seen)
    stops = stop_checks(target, seen)
    op = OPERATIONS.current()
    return {
        "enabled": enabled(),
        "served_by_live_service": target.served_by_live,
        "workspace_found": target.workspace is not None,
        "workspace_name": target.workspace.name if target.workspace else None,
        **{k: v for k, v in seen.items() if k != "unit"},
        "unit": {k: v for k, v in seen["unit"].items() if k != "workspace"},
        "start_checks": starts,
        "stop_checks": stops,
        "can_start": not any(c["level"] == "refuse" for c in starts),
        "can_stop": not any(c["level"] == "refuse" for c in stops),
        "refusals": sorted({c["code"] for c in starts if c["level"] == "refuse"}),
        "needs_confirmation": {
            "start": sorted({c["code"] for c in starts if c["level"] == "confirm"}),
            "stop": sorted({c["code"] for c in stops if c["level"] == "confirm"}),
        },
        "operation": op.public() if op else None,
        "confirm": {
            "start": START_CONFIRM,
            "stop": STOP_CONFIRM,
            "force_phrase": FORCE_PHRASE,
        },
    }


# --- the audit -----------------------------------------------------------------------


def audit_path(target) -> Path:
    """``<live workspace>/live/service-control.jsonl``; the live home's when
    no live workspace is known (only refusals are written then)."""
    if target.workspace is not None:
        path = target.workspace / "live" / AUDIT
        if locate.confined(path.parent, target.workspace):
            return path
    return target.config.home / AUDIT


def audit(target, record: dict) -> None:
    """One line, never a path or a secret; a log that cannot be written does
    not undo or block the action."""
    row = {
        "time": time.time(),
        "actor": "person",
        "via": "product",
        **{k: (scrub(v) if isinstance(v, str) else v) for k, v in record.items()},
    }
    with contextlib.suppress(OSError, ValueError):
        jsonio.append_line(
            audit_path(target), row, max_bytes=AUDIT_MAX_BYTES, keep=AUDIT_KEEP
        )


# --- operations ----------------------------------------------------------------------


@dataclass
class Operation:
    id: str
    action: str
    request_id: str
    # preparing (the preflight runs), refused, running, done, failed
    state: str = "preparing"
    result: str | None = None
    detail: str = ""
    method: str | None = None
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    # The preflight's refusal, given again to the same request id.
    refusal: "Refused | None" = None

    def public(self) -> dict:
        return {
            "operation_id": self.id,
            "action": self.action,
            "request_id": self.request_id,
            "state": self.state,
            "result": self.result,
            "detail": self.detail,
            "method": self.method,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }


def _flock(home: Path):
    """The cross-process operation lock ``<home>/ui-op.lock`` (another LEVI
    core showing the same live home): an open handle, held until closed."""
    try:
        home.mkdir(parents=True, exist_ok=True)
        handle = open(home / OP_LOCK, "a")  # noqa: SIM115
    except OSError as exc:
        raise Refused(
            412, "not_writable", "The live home is not writable; nothing was done"
        ) from exc
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise Busy(None) from exc
    return handle


class Operations:
    """The operations of this process: at most one running; the last
    ``MAX_OPS`` kept for ``GET …/operations/{id}``."""

    def __init__(self):
        self.lock = threading.Lock()
        self.ops: OrderedDict = OrderedDict()
        self.by_request: dict = {}
        self.active: Operation | None = None
        self.threads: list = []

    def current(self) -> Operation | None:
        with self.lock:
            return self.active

    def get(self, operation_id) -> Operation | None:
        with self.lock:
            return self.ops.get(operation_id)

    def submit(self, target, action, request_id, prepare):
        """Reserve the one slot (``Busy`` when taken), run ``prepare`` (the
        preflight: returns ``(method, work)`` or raises ``Refused``), then
        run ``work`` in a thread. The same ``request_id`` returns the
        operation it started: ``(operation, created)``."""
        with self.lock:
            old = self.by_request.get(request_id)
            if old is not None:
                if old.action != action:
                    raise Refused(
                        409,
                        "request_reused",
                        "This request id was used for another action",
                    )
                if old.refusal is not None:
                    raise old.refusal
                return old, False
            if self.active is not None:
                raise Busy(self.active.id)
            handle = _flock(target.config.home)
            op = Operation(secrets.token_hex(8), action, request_id)
            self.active = op
            # Known from now on: the same request id during the preflight
            # gets this operation, and GET …/operations/{id} finds it.
            self.ops[op.id] = op
            self.by_request[request_id] = op
            while len(self.ops) > MAX_OPS:
                gone = self.ops.popitem(last=False)[1]
                self.by_request.pop(gone.request_id, None)
        try:
            method, work = prepare()
        except BaseException as exc:
            refused = isinstance(exc, Refused)
            with self.lock:
                op.state = "refused" if refused else "failed"
                op.result = exc.code if refused else "error"
                op.detail = scrub(exc.message if refused else type(exc).__name__)
                op.refusal = exc if refused else None
                op.ended_at = time.time()
                self.active = None
            handle.close()
            if refused:
                audit(
                    target,
                    {
                        "action": action,
                        "phase": "refused",
                        "request_id": request_id,
                        "operation_id": op.id,
                        "code": exc.code,
                        "detail": exc.message,
                    },
                )
                exc.audited = True
            raise
        with self.lock:
            op.method = method
            op.state = "running"
        audit(
            target,
            {
                "action": action,
                "phase": "accepted",
                "request_id": request_id,
                "operation_id": op.id,
                "method": method,
            },
        )
        thread = threading.Thread(
            target=self._run,
            args=(target, op, work, handle),
            name=f"live-service-{action}",
            daemon=True,
        )
        self.threads = [t for t in self.threads if t.is_alive()] + [thread]
        try:
            thread.start()
        except RuntimeError as exc:  # no thread could be started: free the slot
            op.state, op.result, op.detail = "failed", "error", "no thread"
            op.ended_at = time.time()
            with self.lock:
                self.active = None
            handle.close()
            raise Refused(
                503, "busy_host", "The core cannot start the operation now"
            ) from exc
        return op, True

    def _run(self, target, op, work, handle):
        try:
            ok, result, detail = work()
            op.state = "done" if ok else "failed"
            op.result, op.detail = result, scrub(detail)
        except SystemdError as exc:
            op.state, op.result, op.detail = "failed", exc.code, scrub(exc.detail)
        except Exception as exc:  # noqa: BLE001 - reported, never lost
            op.state, op.result, op.detail = (
                "failed",
                "error",
                scrub(type(exc).__name__),
            )
        finally:
            op.ended_at = time.time()
            audit(
                target,
                {
                    "action": op.action,
                    "phase": "finished",
                    "request_id": op.request_id,
                    "operation_id": op.id,
                    "method": op.method,
                    "state": op.state,
                    "result": op.result,
                    "detail": op.detail,
                },
            )
            with self.lock:
                if self.active is op:
                    self.active = None
            handle.close()

    def wait_idle(self, timeout=10.0) -> bool:
        """For the tests: every operation thread has ended."""
        deadline = time.monotonic() + timeout
        for thread in list(self.threads):
            thread.join(max(0.0, deadline - time.monotonic()))
        return not any(t.is_alive() for t in self.threads)


OPERATIONS = Operations()


def _request_id(value) -> str:
    if not isinstance(value, str) or not REQUEST_ID.fullmatch(value):
        raise Refused(
            400, "request_id_invalid", "request_id: 8-80 letters, digits or ._:-"
        )
    return value


def _refuse_on(checks, confirmed: set):
    """Raise for the first ``refuse`` check, then for a ``confirm`` check the
    request did not confirm."""
    statuses = {
        "served_by_live_service": 409,
        "unit_name_invalid": 412,
        "unit_not_installed": 412,
        "no_live_workspace": 412,
        "not_writable": 412,
        "unit_is_self": 412,
        "unit_not_live": 412,
        "systemd_unavailable": 503,
        "systemd_timeout": 503,
        "permission_denied": 503,
        "ports_unknown": 503,
    }
    for c in checks:
        if c["level"] == "refuse":
            raise Refused(statuses.get(c["code"], 409), c["code"], c["message"])
    for c in checks:
        if c["level"] == "confirm" and c["code"] not in confirmed:
            code = {"gpu_shared": "gpu_shared_unconfirmed"}.get(c["code"], c["code"])
            raise Refused(409, code, c["message"])


def _settle(target, unit, restarts_before) -> tuple:
    """After ``systemctl start``: wait (bounded) until the supervisor holds
    its instance lock, or the unit gave up, or systemd restarts it in a loop
    (``auto-restart``, or more restarts than before the start): that is a
    failed start, not one in progress."""
    deadline = time.monotonic() + SETTLE_S
    while True:
        record = holder(target)
        state = unit_state(unit)
        restarts = state.get("n_restarts") or 0
        if state.get("flapping") or restarts > (restarts_before or 0):
            return (
                False,
                "flapping",
                (
                    f"the unit fails and systemd restarts it ({state.get('state')}/"
                    f"{state.get('sub_state')}, {restarts} restarts); "
                    f"see journalctl --user -u {unit}"
                ),
            )
        if record:
            pid = record["pid"]
            if state.get("main_pid") == pid or cgroup_unit(pid) == unit:
                return True, "started", f"pid {pid}"
            # Someone started one from a terminal meanwhile: the unit's own
            # start finds the live home taken and fails.
            return (
                False,
                "start_failed",
                f"another instance took the live home (pid {pid}, started from a terminal)",
            )
        if state.get("state") in ("failed", "inactive"):
            return (
                False,
                "start_failed",
                f"the unit is {state.get('state')} ({state.get('sub_state')}); see journalctl --user -u {unit}",
            )
        if time.monotonic() >= deadline:
            return True, "starting", "the unit runs; the service is still starting"
        sleep(POLL_S)


def check_enabled() -> None:
    """403 ``disabled`` unless ``LEVI_LIVE_SERVICE_CONTROL`` is on: checked
    before anything is read or written."""
    if not enabled():
        raise Refused(
            403,
            "disabled",
            f"Starting and stopping from the page is off ({ENV_ENABLED})",
        )


def _submit(target, action, request_id, confirm, word, prepare):
    """The checks every start and stop makes before its preflight, then the
    operation. Every refusal but ``disabled`` is audited."""
    check_enabled()
    try:
        if confirm != word:
            raise Refused(400, "confirm_required", f'confirm must be "{word}"')
        request_id = _request_id(request_id)
        return OPERATIONS.submit(target, action, request_id, prepare)
    except Refused as exc:
        if not exc.audited:
            audit(
                target,
                {
                    "action": action,
                    "phase": "refused",
                    "request_id": request_id if isinstance(request_id, str) else None,
                    "code": exc.code,
                    "detail": exc.message,
                    **(
                        {"operation_id": exc.extra["operation_id"]}
                        if exc.extra.get("operation_id")
                        else {}
                    ),
                },
            )
            exc.audited = True
        raise


def start(target, *, request_id, confirm, confirm_gpu_shared=False, reset_failed=False):
    """``POST …/service/start``: ``(operation, created)``."""
    unit = target.unit

    def prepare():
        seen = discover(target, fresh=True)
        confirmed = set()
        if confirm_gpu_shared:
            confirmed.add("gpu_shared")
        if reset_failed:
            confirmed.add("unit_failed")
        _refuse_on(start_checks(target, seen), confirmed)
        failed = seen["unit"].get("state") == "failed"
        restarts = seen["unit"].get("n_restarts") or 0

        def work():
            before = restarts
            if failed:
                code, _, err = systemctl("reset-failed", unit, timeout=SHOW_TIMEOUT_S)
                if code != 0:
                    return False, "reset_failed_failed", err
                before = unit_state(unit).get("n_restarts") or 0
            code, _, err = systemctl("start", unit, timeout=START_TIMEOUT_S)
            if code != 0:
                return False, "start_failed", err
            return _settle(target, unit, before)

        return "systemctl", work

    return _submit(target, "start", request_id, confirm, START_CONFIRM, prepare)


def _signal_stop(record) -> tuple:
    """What ``levi live stop`` does to a service started from a terminal:
    SIGTERM once its identity still matches, then wait."""
    pid, identity = record["pid"], record.get("identity")
    if not gpumgr.same_process(pid, identity):
        return True, "stopped", "it had already ended"
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True, "stopped", "it had already ended"
    except PermissionError:
        return False, "permission_denied", f"pid {pid} cannot be signalled"
    deadline = time.monotonic() + SIGNAL_STOP_S
    while time.monotonic() < deadline:
        if not gpumgr.same_process(pid, identity):
            return True, "stopped", f"pid {pid}"
        sleep(POLL_S)
    return (
        False,
        "stop_incomplete",
        f"pid {pid} is still shutting down (worker or vLLM)",
    )


def stop(target, *, request_id, confirm, force_phrase=""):
    """``POST …/service/stop``: ``(operation, created)``. Only the unit, or a
    resident ``levi live start`` from a terminal, is ever stopped."""
    unit = target.unit

    def prepare():
        seen = discover(target, fresh=True)
        confirmed = (
            {"evaluation_active", "evaluation_unknown"}
            if force_phrase == FORCE_PHRASE
            else set()
        )
        _refuse_on(stop_checks(target, seen), confirmed)
        record = holder(target)
        by_unit = bool(unit) and (
            seen["holder"]["started_by"] == "unit"
            or seen["unit"].get("state") in (*RUNNING_STATES, "deactivating")
        )
        terminal = (
            record
            if record
            and seen["holder"]["started_by"] == "terminal"
            and record["pid"] == seen["holder"]["pid"]
            else None
        )

        def work():
            outcome = (True, "stopped", "")
            if terminal:
                outcome = _signal_stop(terminal)
                if not outcome[0]:
                    return outcome
            if by_unit:
                code, _, err = systemctl("stop", unit, timeout=STOP_TIMEOUT_S)
                if code != 0:
                    return False, "stop_failed", err
            left = holder(target)
            if left:
                return (
                    False,
                    "stop_incomplete",
                    f"pid {left['pid']} still holds the live home",
                )
            return outcome

        method = "+".join(
            m for m, on in (("signal", terminal), ("systemctl", by_unit)) if on
        )
        return method or "none", work

    return _submit(target, "stop", request_id, confirm, STOP_CONFIRM, prepare)


def operation(operation_id) -> dict:
    op = OPERATIONS.get(operation_id)
    if op is None:
        raise Refused(
            404, "unknown_operation", "No such operation (they are kept in memory only)"
        )
    return op.public()
