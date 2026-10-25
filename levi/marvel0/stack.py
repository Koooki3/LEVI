"""The arm stack as the marvel0 page sees it (S4-0): read-only state of the
health monitor M, the arm controller A, the gripper stack B, the robot server
C, the policy server and the demo client; a dry run of every start gate of A,
B and C; and the page's management of M, the one read-only member.

What this module may do, and nothing more:

* **Read passively.** ``systemctl --user show`` / ``list-units`` and the
  manager's own limits, ``loginctl show-user`` (Linger), the kernel's
  machine-wide socket tables (``levi.marvel0.net``: LISTEN rows of the robot
  server and policy ports, ESTABLISHED rows to the policy port; and the lab's
  ``arm_guard.py`` for the sockets to the arm), the health file the monitor
  writes, the arm-controller marker (``lstat`` only), the evaluation session
  files and this process's own ``/proc/self/cgroup``. **No ``/proc/<pid>`` of
  any other process is read, here or through anything this module calls**
  (RIG-01): the setup probes' process scan (``guard.robot``) is not used, so
  a layer without a passive source is ``unknown`` and says why (B always;
  the client until it connects). No port is connected to, the arm's
  included. Rule R: while any layer is unknown the stack's verdict is never
  ``idle`` and every start gate refuses (``gate.layers_known``).
* **Start and stop M only**, as the transient unit ``levi-fr3-health-monitor``
  (the name in use since 2026-10-20 12:43), through ``systemd-run --user`` and
  ``systemctl --user stop``; a unit of that name is ours only when ``systemctl
  show`` reports exactly that ``Id`` and exactly the monitor script as its
  command (it is adopted then, never replaced). A, B and C have **no start or
  stop route** in this module: their gates are evaluated and shown, nothing
  more.
* **Delete one file**: the arm-controller marker, on a person's request, only
  when A's unit does not exist and the arm's sockets show no control
  connection (``arm_guard``'s verdict with the marker left out).
* **Act for the real machine only inside ``levi-product.service``**
  (``profile``): a worktree or temporary instance gets profile ``fake``, and
  every write refuses ``not_product``. Every write also holds the machine-wide
  stack lock (``StackLock``, ``lab/levi-hub/.marvel0-stack.lock`` of the main
  checkout), so two LEVI instances cannot act on the stack at once.

**What the gates are worth.** Every gate here stops a mistake, not another
program of the same account: anything that runs as this user can send the
same request, forge the page's headers or edit the files read here. The real
barriers are Franka Desk and the physical emergency stop.

All of the machine is reached through ``Host``, which tests replace whole.
"""

import contextlib
import fcntl
import importlib.util
import json
import os
import re
import stat
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from levi.automatic.campaign.switch import exec_argv
from levi.setup import probes

from . import net, status

SCHEMA = "levi.marvel0.stack.v1"
MONITOR_UNIT = "levi-fr3-health-monitor"
MONITOR_SERVICE = f"{MONITOR_UNIT}.service"
# Relative to the franka_control checkout, which is the parent of the health
# file's ``run/`` folder (``fr3.health_file`` of the live configuration).
MONITOR_SCRIPT = ("scripts", "fr3_health_monitor.sh")
# The planned unit names of A, B and C (no route here starts them; their
# absence is what the marker release needs).
STACK_UNITS = {
    "A": "levi-aeri-stack-marvel0-a.service",
    "B": "levi-aeri-stack-marvel0-b.service",
    "C": "levi-aeri-stack-marvel0-c.service",
}
PRODUCT_UNIT = "levi-product.service"
LOCK_FILE = ".marvel0-stack.lock"  # next to the marker, lab/levi-hub/
# The robot servers' ports (C, and a second robot server); only whether a
# LISTEN row exists is read, never who owns it.
ROBOT_SERVER_PORTS = (5000, 5001)
# arm_guard.status() counts the marker as "active"; for the socket verdict
# alone it is given a marker path that can never exist.
NO_MARKER = Path("/proc/self/levi-no-marker")
RTPRIO_MIN = 98  # the control node asks for SCHED_FIFO 98
SHOW_TIMEOUT_S = 3.0
START_TIMEOUT_S = 15.0
STOP_TIMEOUT_S = 25.0  # the monitor's TimeoutStopSec plus margin
MONITOR_PROPS = (
    "Id",
    "LoadState",
    "ActiveState",
    "SubState",
    "Result",
    "ExecStart",
    "MainPID",
    "NRestarts",
)
# The automatic management of M (``Reconciler``).
AUTO_ENV = "LEVI_MARVEL0_MONITOR_AUTO"
AUTO_INTERVAL_S = 5.0
STOP_GRACE_S = 120.0  # the stack must look down this long before M is stopped
RESTART_WINDOW_S = 600.0
RESTART_MAX = 3  # automatic starts of M within RESTART_WINDOW_S
PHRASES = {
    "monitor_start": "start health monitor",
    "monitor_stop": "stop health monitor",
    "marker_clear": "clear marker",
    # The writes of ``stack_act.py`` (A, B, C, the marker's creation and the
    # gripper test); the phrase that overrides the client rule of C's stop is
    # a constant there.
    "arm_start": "start arm stack",
    "sequence_start": "start arm stack",
    "sequence_stop": "stop whole stack",
    "arm_stop": "stop arm stack",
    "gripper_start": "start gripper stack",
    "gripper_stop": "stop gripper stack",
    "robot_server_start": "start robot server",
    "robot_server_stop": "stop robot server",
    "marker_touch": "set marker",
    "marker_adopt": "adopt marker",
    "gripper_open": "gripper open",
    "gripper_close": "gripper close",
    "gripper_position": "gripper position",
}
REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{8,80}")
ANSWERS_KEPT = 64
ALLOWED = ("systemctl", "systemd-run", "loginctl")

# code -> (HTTP status, message); no message holds a path.
REFUSALS = {
    "request_id_invalid": (
        400,
        "request_id is 8-80 characters of A-Z, a-z, 0-9 and . _ : -",
    ),
    "confirm_required": (400, "Type the confirmation phrase exactly: {phrase}"),
    "request_id_used": (409, "That request id was already used for another request"),
    "not_product": (
        409,
        (
            "Only the product LEVI (levi-product.service in the main checkout) may "
            "act on the real arm stack; this instance only shows it"
        ),
    ),
    "stack_busy": (423, "Another action on the arm stack is running"),
    "lock_unusable": (503, "The machine-wide stack lock could not be taken"),
    "systemd_unreadable": (
        503,
        "systemd could not be asked, so nothing is assumed stopped",
    ),
    "monitor_script_missing": (
        412,
        "The health monitor's script is not known or not an executable file",
    ),
    "monitor_foreign_unit": (
        409,
        (
            "A unit of the monitor's name runs another command; the page does not "
            "touch it"
        ),
    ),
    "monitor_elsewhere": (
        409,
        (
            "A health monitor outside the page is writing the health file (a "
            "terminal?); a second one is not started"
        ),
    ),
    "monitor_running": (409, "The health monitor is already running"),
    "monitor_not_ours": (409, "The page has no health monitor to stop"),
    "stack_running": (
        409,
        (
            "The arm stack is (or may be) running; the health monitor stops last, "
            "after A and C"
        ),
    ),
    "start_failed": (502, "systemd-run could not start the health monitor"),
    "stop_failed": (502, "systemctl could not stop the health monitor"),
    "marker_absent": (409, "There is no marker to clear"),
    "marker_unknown": (503, "The marker could not be examined"),
    "marker_a_unit": (409, "The arm controller's unit exists; the marker stays"),
    "marker_arm_active": (
        409,
        (
            "The arm's sockets show a control connection, or it cannot be ruled "
            "out; the marker stays"
        ),
    ),
    "marker_clear_failed": (502, "The marker could not be removed"),
}


class Refuse(Exception):
    """A write that does not go on: stable ``code``, HTTP ``status``, English
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


# --- commands ------------------------------------------------------------------------


def run_command(argv, timeout_s) -> tuple:
    """``(exit code, stdout[+stderr on failure])`` without a shell; 124 on a
    timeout, 125 when it cannot run (as ``deploy.run_command``)."""
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return 124, "did not finish in time"
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return 125, str(exc)
    text = done.stdout or ""
    if done.returncode != 0 and done.stderr:
        text += done.stderr
    return done.returncode, text


def guarded(run) -> Callable:
    """``run`` that refuses every command but ``systemctl --user``,
    ``systemd-run --user`` and ``loginctl show-user``: no ``kill``, no shell,
    no ``ros2`` can be built here, whatever a bug does."""

    def call(argv, timeout_s):
        command = argv[0] if argv else None
        if command not in ALLOWED:
            raise ValueError(f"not a command this page may run: {command!r}")
        if command == "loginctl":
            if argv[1:2] != ["show-user"]:
                raise ValueError("loginctl may only show a user")
        elif "--user" not in argv[1:3]:
            raise ValueError(f"{command} without --user")
        return run(argv, timeout_s)

    return call


def parse_show(text) -> dict:
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key:
            out[key] = value
    return out


# --- product-only --------------------------------------------------------------------


def profile(cgroup_text, in_main_checkout) -> dict:
    """``{"profile": "real"|"fake", "code"}``: ``real`` only for a process of
    ``levi-product.service`` (its own cgroup, from ``/proc/self/cgroup``)
    running from the main checkout. Anything else, or anything unreadable, is
    ``fake``: it shows the stack and acts on nothing."""
    if cgroup_text is None:
        return {"profile": "fake", "code": "profile.cgroup_unreadable"}
    paths = [
        line.split(":", 2)[2].strip()
        for line in cgroup_text.splitlines()
        if line.count(":") >= 2
    ]
    if not any(p.rstrip("/").endswith("/" + PRODUCT_UNIT) for p in paths):
        return {"profile": "fake", "code": "profile.not_product_unit"}
    if not in_main_checkout:
        return {"profile": "fake", "code": "profile.not_main_checkout"}
    return {"profile": "real", "code": "profile.product"}


def _read_self_cgroup():
    try:
        return Path("/proc/self/cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _in_main_checkout() -> bool:
    # ``levi.paths.ROOT`` is the WORKSPACE (``<checkout>/.state``), never the
    # checkout: comparing it with the main checkout made every product instance
    # read ``fake`` and refuse every write (found on the first deploy, 21 Oct).
    # The checkout this code runs from is ``locate.checkout_root()``.
    from levi.live import locate

    from . import site

    try:
        return Path(locate.checkout_root()).resolve() == site.main_checkout().resolve()
    except (OSError, IndexError, RuntimeError):
        return False


# --- the machine-wide lock -----------------------------------------------------------


class StackLock:
    """The one lock every action on the real arm stack holds, whichever LEVI
    instance takes it: a ``flock`` on ``<main checkout>/lab/levi-hub/
    .marvel0-stack.lock`` (``LOCK_FILE``). Never waited for: a second action
    is refused ``stack_busy``. The file says who holds it (owner, action,
    pid, since) for the page; whether it is held comes from ``/proc/locks``,
    never from trying it."""

    def __init__(self, path, locks_path="/proc/locks", now=time.time):
        self.path = Path(path)
        self.locks_path = locks_path
        self.now = now

    @contextlib.contextmanager
    def hold(self, owner, action):
        try:
            fd = os.open(
                self.path,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
        except OSError as exc:
            raise Refuse("lock_unusable") from exc
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Refuse("stack_busy") from exc
            except OSError as exc:
                raise Refuse("lock_unusable") from exc
            note = {
                "owner": str(owner)[:80],
                "action": str(action)[:40],
                "pid": os.getpid(),
                "since": round(self.now(), 3),
            }
            with contextlib.suppress(OSError):
                os.ftruncate(fd, 0)
                os.pwrite(fd, (json.dumps(note) + "\n").encode(), 0)
            yield note
        finally:
            os.close(fd)  # closing drops the flock

    def state(self) -> dict:
        """``{"state": free|held|unknown, "holder"}`` without taking it."""
        from levi.live import gpumgr

        holders = gpumgr.flock_holders(self.path, locks=self.locks_path)
        if holders is None:
            return {"state": "unknown", "holder": None}
        if not holders:
            return {"state": "free", "holder": None}
        holder = None
        try:
            raw, _, _ = probes.read_regular(self.path, 4096)
            data = json.loads(raw.decode("utf-8", "replace"))
            if isinstance(data, dict):
                holder = {
                    "owner": status._text(data.get("owner", ""), 80),
                    "action": status._text(data.get("action", ""), 40),
                    "since": data.get("since")
                    if isinstance(data.get("since"), (int, float))
                    else None,
                }
        except (OSError, ValueError):
            holder = None
        return {"state": "held", "holder": holder}


# --- the host ------------------------------------------------------------------------


def _hub() -> Path:
    from . import api

    return api.marker_path().parent


def _health_file():
    context = probes.context_from_environment()
    return context.health_file, float(context.health_stale_s)


def _load_arm_guard(hub):
    spec = importlib.util.spec_from_file_location(
        "levi_lab_arm_guard", Path(hub) / "arm_guard.py"
    )
    if spec is None or spec.loader is None:
        raise OSError("arm_guard.py cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _arm_sockets() -> dict:
    """``arm_guard``'s verdict over the socket tables with the marker left
    out: ``{"state": active|inactive|unknown, "detail"}``. The detail names
    the arm's address, which is not a path."""
    try:
        guard = _load_arm_guard(_hub())
        state, detail = guard.status(marker=NO_MARKER)
    except Exception as exc:  # noqa: BLE001 - an unloadable guard is "unknown"
        return {
            "state": "unknown",
            "detail": f"arm_guard failed ({type(exc).__name__})",
        }
    if state not in ("active", "inactive", "unknown"):
        state = "unknown"
    return {"state": state, "detail": status._text(detail, 200)}


def _sessions(now) -> dict:
    from . import api

    roots, why = api.rollout_roots()
    return status.sessions_part(roots, now, why)


@dataclass
class Host:
    run: Callable  # (argv, timeout_s) -> (exit code, text)
    marker: Callable = lambda: _hub() / ".arm-controller-active"
    lock_path: Callable = lambda: _hub() / LOCK_FILE
    health_file: Callable = _health_file  # -> (path or None, stale seconds)
    arm_sockets: Callable = _arm_sockets
    sessions: Callable = _sessions  # (now) -> status.sessions_part
    net_table: Callable = net.read
    cgroup: Callable = _read_self_cgroup
    in_main_checkout: Callable = _in_main_checkout
    policy_port: Callable = lambda: _site_port()
    environ: dict = field(default_factory=lambda: os.environ)
    locks_path: str = "/proc/locks"
    uid: int = field(default_factory=os.getuid)
    now: Callable = time.time
    monotonic: Callable = time.monotonic
    unlink: Callable = os.unlink


def _site_port():
    from . import site

    loaded = site.load()
    return loaded.site.port if loaded.state == "ok" else 8000


def production_host() -> Host:
    return Host(run=guarded(run_command))


# --- passive reads -------------------------------------------------------------------


def monitor_script(health_file):
    """The monitor's script, ``<franka_control>/scripts/fr3_health_monitor.sh``,
    when the health file sits in ``<franka_control>/run/``; else None."""
    if not health_file:
        return None
    path = Path(health_file)
    if path.parent.name != "run":
        return None
    return str(path.parent.parent.joinpath(*MONITOR_SCRIPT))


def read_health(path, stale_s, now) -> dict:
    """The health file: ``{"state": ok|red|offline|missing|unknown, "age_s",
    "monitor_pid", "hardware_active", "controller_active",
    "robot_mode_name"}``. Read with ``probes.read_regular`` (no link, no
    FIFO, a size cap)."""
    out = {
        "state": "unknown",
        "age_s": None,
        "monitor_pid": None,
        "hardware_active": None,
        "controller_active": None,
        "robot_mode_name": None,
    }
    if not path:
        return {**out, "code": "health.not_configured"}
    try:
        raw, truncated, st = probes.read_regular(path, 1 << 16)
    except FileNotFoundError:
        return {**out, "state": "missing", "code": "health.missing"}
    except (OSError, probes.NotRegular):
        return {**out, "code": "health.unreadable"}
    try:
        data = None if truncated else json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return {**out, "code": "health.unreadable"}
    stamp = data.get("updated_at_epoch")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        stamp = st.st_mtime
    age = max(0.0, now - float(stamp))
    pid = data.get("monitor_pid")

    def flag(key):
        value = data.get(key)
        return value if isinstance(value, bool) else None

    mode = data.get("robot_mode_name")
    out.update(
        age_s=age,
        monitor_pid=pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
        hardware_active=flag("hardware_active"),
        controller_active=flag("controller_active"),
        robot_mode_name=status._text(mode, 40) if isinstance(mode, str) else None,
    )
    if age > stale_s:
        return {**out, "state": "offline", "code": "health.stale"}
    return {
        **out,
        "state": "red" if data.get("red_light") else "ok",
        "code": "health.fresh",
    }


def show_unit(run, unit) -> dict | None:
    """``systemctl --user show`` of one unit, or None when systemd did not
    answer."""
    argv = [
        "systemctl",
        "--user",
        "show",
        unit,
        "--property=" + ",".join(MONITOR_PROPS),
    ]
    try:
        code, text = run(argv, SHOW_TIMEOUT_S)
    except (OSError, ValueError):
        return None
    if code != 0:
        return None
    shown = parse_show(text)
    return shown if shown.get("Id") else None


def monitor_unit(shown, script) -> dict:
    """``{"state", "owned", "main_pid", "restarts", "result"}`` of the
    monitor's unit. ``state``: ``absent`` (no such unit), ``running``,
    ``starting``, ``stopping``, ``stopped``, ``failed`` (ours: exactly
    ``MONITOR_SERVICE`` running exactly ``script``), ``foreign`` (that name
    with another command) or ``unknown``."""
    blank = {"owned": False, "main_pid": None, "restarts": None, "result": None}
    if shown is None:
        return {"state": "unknown", **blank}
    if shown.get("LoadState") == "not-found" and shown.get("ActiveState") in (
        "inactive",
        "",
        None,
    ):
        return {"state": "absent", **blank}
    if shown.get("Id") != MONITOR_SERVICE or not script:
        return {"state": "unknown" if not script else "foreign", **blank}
    if exec_argv(shown.get("ExecStart", "")) != [script]:
        return {"state": "foreign", **blank}
    active = shown.get("ActiveState")
    state = {
        "active": "running",
        "reloading": "running",
        "activating": "starting",
        "deactivating": "stopping",
        "inactive": "stopped",
        "failed": "failed",
    }.get(active, "unknown")
    try:
        pid = int(shown.get("MainPID") or 0) or None
    except ValueError:
        pid = None
    try:
        restarts = int(shown.get("NRestarts") or 0)
    except ValueError:
        restarts = None
    return {
        "state": state,
        "owned": True,
        "main_pid": pid,
        "restarts": restarts,
        "result": status._text(shown.get("Result") or "", 40) or None,
    }


def manager_limits(run) -> dict:
    """The user manager's default ``RLIMIT_RTPRIO`` and ``RLIMIT_MEMLOCK``,
    which its units get: ``ok`` when FIFO 98 and an unlimited memlock fit."""
    try:
        code, text = run(
            [
                "systemctl",
                "--user",
                "show",
                "--property=DefaultLimitRTPRIO,DefaultLimitMEMLOCK",
            ],
            SHOW_TIMEOUT_S,
        )
    except (OSError, ValueError):
        code, text = 125, ""
    if code != 0:
        return {"state": "unknown", "rtprio": None, "memlock": None}
    shown = parse_show(text)
    rtprio = shown.get("DefaultLimitRTPRIO", "")
    memlock = shown.get("DefaultLimitMEMLOCK", "")
    try:
        prio = int(rtprio)
    except ValueError:
        prio = None
    ok = prio is not None and prio >= RTPRIO_MIN and memlock == "infinity"
    return {
        "state": "ok" if ok else ("unknown" if prio is None else "low"),
        "rtprio": prio,
        "memlock": status._text(memlock, 24) or None,
    }


def linger(run, uid) -> dict:
    """``loginctl show-user``'s Linger: ``yes`` keeps the user's units after
    the last logout, ``no`` stops them (the arm stack too, within 5 s)."""
    try:
        code, text = run(
            ["loginctl", "show-user", str(uid), "--property=Linger"], SHOW_TIMEOUT_S
        )
    except (OSError, ValueError):
        code, text = 125, ""
    value = parse_show(text).get("Linger") if code == 0 else None
    return {"state": value if value in ("yes", "no") else "unknown"}


def _evidence(code, value) -> dict:
    return {"code": code, "value": value}


# --- the document --------------------------------------------------------------------


def _terminal(state, evidence, code) -> dict:
    return {"state": state, "evidence": evidence, "code": code}


def terminals(m, health, sockets, table, policy_port, sessions):
    """The state of each member of the stack, from what was read; every
    ``unknown`` says why (``code``)."""
    fresh = health["state"] in ("ok", "red")
    # M
    if m["state"] in ("running", "starting", "stopping", "failed", "foreign"):
        m_state, m_code = m["state"], f"m.{m['state']}"
    elif m["state"] in ("absent", "stopped"):
        if fresh:
            m_state, m_code = "elsewhere", "m.elsewhere"
        else:
            # Said as it is: which of the two is missing, the unit or a fresh
            # health file (and how old the file is, ``health.age_s``).
            unit = "absent" if m["state"] == "absent" else "stopped"
            file = "no_health" if health["state"] == "missing" else "health_stale"
            m_state, m_code = "stopped", f"m.{unit}_{file}"
    else:
        m_state, m_code = "unknown", "m.unknown"
    m_evidence = [
        _evidence("health.state", health["state"]),
        _evidence(
            "health.age_s",
            round(health["age_s"], 1) if health["age_s"] is not None else None,
        ),
    ]
    if m["main_pid"] and health["monitor_pid"] is not None:
        m_evidence.append(
            _evidence("m.pid_matches", m["main_pid"] == health["monitor_pid"])
        )
    out = {"M": _terminal(m_state, m_evidence, m_code)}

    # A: a control connection to the arm (arm_guard over /proc/net, which
    # counts every non-Desk TCP port and any UDP) or a fresh health file
    # saying the hardware interface is active. No connection means A is not
    # controlling the arm; an unreadable table is unknown.
    a_sockets = sockets["state"]
    a_health = health["hardware_active"] if fresh else None
    a_ev = [
        _evidence("a.arm_sockets", a_sockets),
        _evidence("a.hardware_active", a_health),
    ]
    if a_sockets == "active" or a_health:
        out["A"] = _terminal("running", a_ev, "a.running")
    elif a_sockets == "inactive":
        out["A"] = _terminal("stopped", a_ev, "a.stopped")
    else:
        out["A"] = _terminal("unknown", a_ev, "a.unknown")

    # B has no known passive evidence (no socket of its own; the monitor does
    # not ask the gripper's controller manager): always unknown, and said so.
    out["B"] = _terminal("unknown", [], "b.not_observable")

    # C: a LISTEN row on a robot-server port (who owns it is not looked up);
    # the impedance controller, which outlives C, from fresh health.
    c_listen = (
        None
        if not table.ok
        else any(net.listeners(table, port) for port in ROBOT_SERVER_PORTS)
    )
    c_ev = [
        _evidence("c.listening", c_listen),
        _evidence("c.impedance_active", health["controller_active"] if fresh else None),
    ]
    if c_listen:
        out["C"] = _terminal("running", c_ev, "c.running")
    elif c_listen is False:
        code = (
            "c.stopped_impedance"
            if fresh and health["controller_active"]
            else "c.stopped"
        )
        out["C"] = _terminal("stopped", c_ev, code)
    else:
        out["C"] = _terminal("unknown", c_ev, "c.unknown")

    # The policy server: a LISTEN row on its port.
    p_listen = None if not table.ok else bool(net.listeners(table, policy_port))
    out["policy"] = _terminal(
        {True: "running", False: "stopped", None: "unknown"}[p_listen],
        [_evidence("policy.listening", p_listen)],
        {True: "policy.running", False: "policy.stopped", None: "policy.unknown"}[
            p_listen
        ],
    )

    # The demo client: an ESTABLISHED connection to the policy port, or an
    # evaluation session that has not ended. A client that has not connected
    # yet cannot be seen, so "no connection" is unknown, never stopped.
    links = None if not table.ok else bool(net.established(table, policy_port))
    open_session = None
    if sessions.get("state") == "ok":
        open_session = any(
            row.get("status") not in status.ENDED_STATES
            and (row.get("age_s") or 0) <= status.SESSION_BUSY_S
            for row in sessions.get("sessions") or []
        )
    e_ev = [
        _evidence("client.connected", links),
        _evidence("client.open_session", open_session),
    ]
    if links or open_session:
        out["client"] = _terminal("running", e_ev, "client.running")
    elif links is False:
        out["client"] = _terminal("unknown", e_ev, "client.not_connected")
    else:
        out["client"] = _terminal("unknown", e_ev, "client.unknown")
    return out


def _check(code, ok, level="required", params=None) -> dict:
    """One gate check: ``ok`` True (passes), False (refuses) or None (cannot
    be told: refuses too). ``level``: ``required``, ``warn`` (shown, never
    refuses) or ``person`` (asked of the person at start; not evaluated)."""
    return {"code": code, "ok": ok, "level": level, "params": params or {}}


def _verdict(checks) -> dict:
    refusing = [
        c["code"] for c in checks if c["level"] == "required" and c["ok"] is not True
    ]
    return {
        "verdict": "would_refuse" if refusing else "would_start",
        "reasons": refusing,
        "checks": checks,
        # S4-0 builds no route that starts A, B or C: this is a dry run.
        "route": False,
    }


def gates(prof, lock, marker, sockets, health, rt, lingering, units, terms) -> dict:
    """The dry run of the planned start gates of A, B and C against what was
    read now. Nothing here can start anything."""
    real = prof["profile"] == "real"
    lock_free = {"free": True, "held": False}.get(lock["state"])
    fresh = health["state"] in ("ok", "red")
    m_ok = terms["M"]["state"] in ("running", "elsewhere") and fresh
    a, b, c = terms["A"]["state"], terms["B"]["state"], terms["C"]["state"]
    exists = units
    unknown = unknown_layers(terms)
    common = [
        _check("gate.product", real),
        _check("gate.lock_free", lock_free),
        _check("gate.monitor_fresh", m_ok),
        # Rule R: while any layer is unknown nothing robot-side may change (a
        # gripper stack started in a terminal is invisible: B is always
        # unknown until the page starts B itself).
        _check(
            "gate.layers_known",
            not unknown,
            params={"unknown": ", ".join(unknown)},
        ),
    ]
    marker_age = marker.get("ctime_age_s")
    a_checks = [
        *common,
        _check("gate.a_absent", {"stopped": True, "running": False}.get(a)),
        _check("gate.a_unit_absent", None if exists["A"] is None else not exists["A"]),
        _check(
            "gate.marker_set",
            {"present": True, "absent": False}.get(marker["state"]),
            params={"age_s": round(marker_age, 1) if marker_age is not None else None},
        ),
        _check(
            "gate.arm_sockets_clear",
            {"inactive": True, "active": False}.get(sockets["state"]),
        ),
        _check(
            "gate.rt_limits",
            {"ok": True, "low": False}.get(rt["state"]),
            params={"rtprio": rt["rtprio"], "memlock": rt["memlock"]},
        ),
        # Owner decision 3 (one TCP connection to the arm's FCI port before A)
        # is open: no probe is made, so this cannot pass.
        _check("gate.fci_probe", None),
        _check("gate.linger", lingering["state"] == "yes", level="warn"),
        _check("gate.person_desk", None, level="person"),
    ]
    b_checks = [
        *common,
        _check("gate.a_running", {"running": True, "stopped": False}.get(a)),
        _check("gate.b_absent", {"stopped": True, "running": False}.get(b)),
        _check("gate.b_unit_absent", None if exists["B"] is None else not exists["B"]),
        _check("gate.person_jaws_clear", None, level="person"),
    ]
    client = terms["client"]["state"]
    c_checks = [
        *common,
        _check("gate.a_running", {"running": True, "stopped": False}.get(a)),
        _check("gate.b_known", b != "unknown"),
        _check("gate.b_running", {"running": True, "stopped": False}.get(b)),
        _check("gate.c_absent", {"stopped": True, "running": False}.get(c)),
        _check("gate.c_unit_absent", None if exists["C"] is None else not exists["C"]),
        _check("gate.no_client", {"stopped": True, "running": False}.get(client)),
        _check("gate.person_estop", None, level="person"),
        # The client cannot be seen before it connects to the policy port, so
        # its absence is a person's answer, never a machine's.
        _check("gate.person_no_client", None, level="person"),
    ]
    return {"A": _verdict(a_checks), "B": _verdict(b_checks), "C": _verdict(c_checks)}


LAYERS = ("M", "A", "B", "C", "policy", "client")
# The layers a start gate needs known. The client is left out: it can only
# connect after C and the policy server are up, so requiring it would refuse
# every start for ever; C's gate judges it with gate.no_client instead.
GATE_LAYERS = ("M", "A", "B", "C")


def unknown_layers(terms, layers=GATE_LAYERS) -> list:
    """The layers (of ``layers``) whose state is unknown, in stack order."""
    return [k for k in layers if terms[k]["state"] == "unknown"]


def _counted(terms, k) -> str:
    """A layer's state as the verdict counts it: a client that is not
    connected counts as stopped only while C and the policy server are both
    seen stopped (nothing it could connect to); otherwise it stays unknown."""
    state = terms[k]["state"]
    if (
        k == "client"
        and terms[k].get("code") == "client.not_connected"
        and terms["C"]["state"] == "stopped"
        and terms["policy"]["state"] == "stopped"
    ):
        return "stopped"
    return state


def stack_verdict(terms) -> dict:
    """Rule R: ``busy`` when any layer runs (or starts, stops, failed, runs
    elsewhere), else ``unknown`` when any layer is unknown, else ``idle``.
    Never ``idle`` while a layer is unknown."""
    counted = {k: _counted(terms, k) for k in LAYERS}
    unknown = [k for k in LAYERS if counted[k] == "unknown"]
    active = [k for k in LAYERS if counted[k] not in ("stopped", "unknown")]
    if active:
        verdict = "busy"
    elif unknown:
        verdict = "unknown"
    else:
        verdict = "idle"
    return {"verdict": verdict, "active": active, "unknown": unknown}


def marker_view(path, now) -> dict:
    """The marker: ``status.marker_part`` plus the age of its ``ctime``
    (which ``touch -d`` cannot set back)."""
    part = status.marker_part(path, now)
    ctime_age = None
    if part["state"] == "present":
        with contextlib.suppress(OSError):
            ctime_age = max(0.0, now - os.lstat(path).st_ctime)
    return {
        "state": part["state"],
        "age_s": part["age_s"],
        "ctime_age_s": ctime_age,
        "code": None if part["state"] != "unknown" else "marker.unknown",
    }


def marker_blockers(prof, marker, units, sockets, terms) -> list:
    """Why the marker cannot be cleared now (empty: it can)."""
    out = []
    if prof["profile"] != "real":
        out.append("not_product")
    if marker["state"] == "absent":
        out.append("marker_absent")
    elif marker["state"] != "present":
        out.append("marker_unknown")
    if units["A"] is not False:
        out.append("marker_a_unit")
    if sockets["state"] != "inactive" or terms["A"]["state"] != "stopped":
        out.append("marker_arm_active")
    return out


def monitor_blockers(prof, m_unit, terms, script_ok) -> dict:
    """``{"start": [codes], "stop": [codes]}`` for the monitor's two routes."""
    real = prof["profile"] == "real"
    start, stop = [], []
    if not real:
        start.append("not_product")
        stop.append("not_product")
    state = m_unit["state"]
    if state == "unknown":
        start.append("systemd_unreadable")
        stop.append("systemd_unreadable")
    if state == "foreign":
        start.append("monitor_foreign_unit")
    if state in ("running", "starting", "stopping"):
        start.append("monitor_running")
    if terms["M"]["state"] == "elsewhere":
        start.append("monitor_elsewhere")
    if not script_ok:
        start.append("monitor_script_missing")
    if not (m_unit["owned"] and state in ("running", "starting", "failed")):
        stop.append("monitor_not_ours")
    if terms["A"]["state"] != "stopped" or terms["C"]["state"] != "stopped":
        stop.append("stack_running")
    return {"start": start, "stop": stop}


def auto_enabled(environ) -> bool:
    """``LEVI_MARVEL0_MONITOR_AUTO``: ``on`` turns the automatic management of
    M on; anything else (unset included) leaves it off, until it has been
    checked on the real robot with the user present (user decision
    2026-10-20). It acts only in profile ``real``."""
    return (environ.get(AUTO_ENV) or "off").strip().lower() in ("on", "1", "yes")


# --- the supervisor ------------------------------------------------------------------


class Stack:
    """Reads the stack (``view``), manages M (``start_monitor``,
    ``stop_monitor``, ``reconcile``) and clears the marker. One per process."""

    def __init__(self, host: Host):
        self.host = host
        self._lock = threading.Lock()  # one write of this process at a time
        self._answers: dict = {}  # request_id -> (fingerprint, answer)
        self._order: deque = deque()
        self._auto_starts: deque = deque()  # monotonic times of automatic starts
        self._down_since = None  # monotonic time the stack was first seen down
        self.last_auto: dict | None = None
        # ``stack_act.Act`` (start and stop of A, B, C, the gripper test): when
        # set, its part of the document is ``act``. Set by whoever builds it.
        self.act = None

    # -- reading
    def _read(self) -> dict:
        h = self.host
        now = h.now()
        prof = profile(h.cgroup(), h.in_main_checkout())
        lock = StackLock(h.lock_path(), h.locks_path, h.now).state()
        try:
            health_path, stale_s = h.health_file()
        except Exception:  # noqa: BLE001 - no configuration: unknown, not absent
            health_path, stale_s = None, 3.0
        script = monitor_script(health_path)
        script_ok = _executable(script)
        health = read_health(health_path, stale_s, now)
        m_unit = monitor_unit(show_unit(h.run, MONITOR_SERVICE), script)
        units, shown_units = {}, {}
        for key, name in STACK_UNITS.items():
            shown = show_unit(h.run, name)
            shown_units[key] = shown
            units[key] = (
                None if shown is None else shown.get("LoadState") != "not-found"
            )
        sockets = h.arm_sockets()
        table = h.net_table()
        try:
            sessions = h.sessions(now)
        except Exception:  # noqa: BLE001 - unreadable sessions are unknown
            sessions = status.unknown_sessions("the sessions could not be read")
        terms = terminals(m_unit, health, sockets, table, h.policy_port(), sessions)
        marker = marker_view(h.marker(), now)
        rt = manager_limits(h.run)
        lingering = linger(h.run, h.uid)
        return {
            "now": now,
            "profile": prof,
            "lock": lock,
            "health": health,
            "m_unit": m_unit,
            "script_ok": script_ok,
            "script": script,
            "units": units,
            "shown": shown_units,  # ``systemctl show`` of A, B, C (None: unreadable)
            "sockets": sockets,
            "terms": terms,
            "marker": marker,
            "rt": rt,
            "linger": lingering,
        }

    def view(self) -> dict:
        """The ``levi.marvel0.stack.v1`` document (no path in it)."""
        r = self._read()
        return self._document(r)

    def _document(self, r) -> dict:
        prof, terms = r["profile"], r["terms"]
        blockers = monitor_blockers(prof, r["m_unit"], terms, r["script_ok"])
        health = {k: v for k, v in r["health"].items()}
        if health["age_s"] is not None:
            health["age_s"] = round(health["age_s"], 1)
        marker = dict(r["marker"])
        for key in ("age_s", "ctime_age_s"):
            if marker[key] is not None:
                marker[key] = round(marker[key], 1)
        marker_block = marker_blockers(
            prof, r["marker"], r["units"], r["sockets"], terms
        )
        auto = auto_enabled(self.host.environ)
        return {
            "schema": SCHEMA,
            "sampled_at": round(r["now"], 3),
            "profile": prof["profile"],
            "profile_code": prof["code"],
            "lock": {**r["lock"], "file": LOCK_FILE},
            "linger": r["linger"],
            "rt_limits": r["rt"],
            "arm_sockets": r["sockets"],
            "health": health,
            "marker": {
                **marker,
                "can_clear": not marker_block,
                "clear_blockers": marker_block,
            },
            "terminals": terms,
            "verdict": stack_verdict(terms),
            "gates": gates(
                prof,
                r["lock"],
                r["marker"],
                r["sockets"],
                r["health"],
                r["rt"],
                r["linger"],
                r["units"],
                terms,
            ),
            "monitor": {
                "unit": MONITOR_SERVICE,
                "unit_state": r["m_unit"]["state"],
                "owned": r["m_unit"]["owned"],
                "restarts": r["m_unit"]["restarts"],
                "can_start": not blockers["start"],
                "start_blockers": blockers["start"],
                "can_stop": not blockers["stop"],
                "stop_blockers": blockers["stop"],
                "auto": auto and prof["profile"] == "real",
                "auto_setting": "on" if auto else "off",
                "plan": plan_monitor(
                    r, auto, self._auto_starts, self._down_since, self.host.monotonic()
                ),
                "last_auto": self.last_auto,
            },
            "phrases": dict(PHRASES),
            "act": None if self.act is None else self.act.document(r),
            "notices": [
                # Said on the page as it is: the gates stop mistakes only.
                {"code": "notice.gates_scope"},
                *(
                    [{"code": "notice.logout_stops_stack"}]
                    if r["linger"]["state"] != "yes"
                    and terms["A"]["state"] != "stopped"
                    else []
                ),
            ],
        }

    # -- writes
    def _remember(self, request_id, fingerprint):
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise Refuse("request_id_invalid")
        seen = self._answers.get(request_id)
        if seen is not None:
            if seen[0] != fingerprint:
                raise Refuse("request_id_used")
            return seen[1]
        return None

    def _store(self, request_id, fingerprint, answer):
        self._answers[request_id] = (fingerprint, answer)
        self._order.append(request_id)
        while len(self._order) > ANSWERS_KEPT:
            self._answers.pop(self._order.popleft(), None)

    def _confirm(self, body, action):
        if body.get("confirm") != PHRASES[action]:
            raise Refuse("confirm_required", phrase=PHRASES[action])

    def _write(self, body, action, act):
        request_id = body.get("request_id")
        with self._lock:
            done = self._remember(request_id, action)
            if done is not None:
                return {**done, "repeated": True}
            self._confirm(body, action)
            answer = self._locked(action, act)
            self._store(request_id, action, answer)
            return answer

    def _locked(self, action, act):
        r = self._read()
        if r["profile"]["profile"] != "real":
            raise Refuse("not_product")
        with StackLock(self.host.lock_path(), self.host.locks_path, self.host.now).hold(
            "marvel0-page", action
        ):
            return act(self._read())

    def start_monitor(self, body) -> dict:
        return self._write(body, "monitor_start", self._start)

    def stop_monitor(self, body) -> dict:
        return self._write(body, "monitor_stop", self._stop)

    def clear_marker(self, body) -> dict:
        return self._write(body, "marker_clear", self._clear)

    def _start(self, r) -> dict:
        blockers = monitor_blockers(
            r["profile"], r["m_unit"], r["terms"], r["script_ok"]
        )
        if blockers["start"]:
            raise Refuse(blockers["start"][0])
        if r["m_unit"]["state"] == "failed":
            # A failed unit of ours keeps its name until it is reset.
            self.host.run(
                ["systemctl", "--user", "reset-failed", MONITOR_SERVICE], SHOW_TIMEOUT_S
            )
        code, _ = self.host.run(monitor_argv(r["script"]), START_TIMEOUT_S)
        if code != 0:
            raise Refuse("start_failed")
        return {"done": "monitor_started", "unit": MONITOR_SERVICE}

    def _stop(self, r) -> dict:
        blockers = monitor_blockers(
            r["profile"], r["m_unit"], r["terms"], r["script_ok"]
        )
        if blockers["stop"]:
            raise Refuse(blockers["stop"][0])
        return self._stop_unit()

    def _stop_unit(self) -> dict:
        code, _ = self.host.run(
            ["systemctl", "--user", "stop", MONITOR_SERVICE], STOP_TIMEOUT_S
        )
        if code != 0:
            raise Refuse("stop_failed")
        return {"done": "monitor_stopped", "unit": MONITOR_SERVICE}

    def _clear(self, r) -> dict:
        blockers = marker_blockers(
            r["profile"], r["marker"], r["units"], r["sockets"], r["terms"]
        )
        if blockers:
            raise Refuse(blockers[0])
        path = self.host.marker()
        try:
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode):
                raise Refuse("marker_unknown")
            self.host.unlink(path)
        except FileNotFoundError as exc:
            raise Refuse("marker_absent") from exc
        except OSError as exc:
            raise Refuse("marker_clear_failed") from exc
        return {"done": "marker_cleared"}

    # -- automatic management of M
    def reconcile(self) -> dict:
        """One step of the automatic management of M: read, plan, act. The
        plan is ``plan_monitor``'s; an action takes the stack lock like a
        person's would, and a refusal is recorded, never raised."""
        with self._lock:
            r = self._read()
            mono = self.host.monotonic()
            self._track_down(r, mono)
            auto = auto_enabled(self.host.environ)
            plan = plan_monitor(r, auto, self._auto_starts, self._down_since, mono)
            if plan["action"] == "none":
                return plan
            try:
                with StackLock(
                    self.host.lock_path(), self.host.locks_path, self.host.now
                ).hold("marvel0-auto", f"monitor_{plan['action']}"):
                    fresh = self._read()
                    again = plan_monitor(
                        fresh, auto, self._auto_starts, self._down_since, mono
                    )
                    if again["action"] != plan["action"]:
                        return again
                    if plan["action"] == "start":
                        self._auto_starts.append(mono)
                        self._start(fresh)
                    else:
                        self._stop_unit()
                outcome = {"ok": True}
            except Refuse as exc:
                outcome = {"ok": False, "code": exc.code}
            self.last_auto = {
                **plan,
                **outcome,
                "at": round(self.host.now(), 3),
            }
            return self.last_auto

    def _track_down(self, r, mono):
        down = (
            r["terms"]["A"]["state"] == "stopped"
            and r["terms"]["C"]["state"] == "stopped"
        )
        if not down:
            self._down_since = None
        elif self._down_since is None:
            self._down_since = mono


def plan_monitor(r, auto, auto_starts, down_since, mono) -> dict:
    """What the automatic management of M would do now:
    ``{"action": start|stop|none, "code"}``. M runs only while the stack
    runs (A or C seen running), comes up first and goes down last (after A
    and C have looked stopped for ``STOP_GRACE_S``); a monitor that died
    while the stack runs is started again, at most ``RESTART_MAX`` times in
    ``RESTART_WINDOW_S``. A foreign unit, a monitor outside the page and
    anything unknown are left alone."""
    if r["profile"]["profile"] != "real":
        return {"action": "none", "code": "auto.not_product"}
    if not auto:
        return {"action": "none", "code": "auto.off"}
    m = r["m_unit"]["state"]
    terms = r["terms"]
    if m in ("foreign", "unknown") or terms["M"]["state"] == "elsewhere":
        return {"action": "none", "code": f"auto.leave_{terms['M']['state']}"}
    if m in ("starting", "stopping"):
        return {"action": "none", "code": "auto.settling"}
    a, c = terms["A"]["state"], terms["C"]["state"]
    up = a == "running" or c == "running"
    down = a == "stopped" and c == "stopped"
    if up and m in ("absent", "stopped", "failed"):
        recent = [t for t in auto_starts if mono - t < RESTART_WINDOW_S]
        if len(recent) >= RESTART_MAX:
            return {"action": "none", "code": "auto.backoff"}
        if not r["script_ok"]:
            return {"action": "none", "code": "auto.script_missing"}
        code = "auto.restart_failed" if m == "failed" else "auto.start_for_stack"
        return {"action": "start", "code": code}
    if down and r["m_unit"]["owned"] and m == "running":
        if down_since is not None and mono - down_since >= STOP_GRACE_S:
            return {"action": "stop", "code": "auto.stop_stack_down"}
        return {"action": "none", "code": "auto.wait_stack_down"}
    if up:
        return {"action": "none", "code": "auto.running"}
    return {"action": "none", "code": "auto.idle" if down else "auto.stack_unknown"}


def monitor_argv(script) -> list:
    """``systemd-run --user`` for M, as the policy-server unit does it
    (``deploy.systemd_run_argv``): SIGINT is the terminal's Ctrl+C, ten
    seconds to stop, niced, collected when it ends. ``Restart=no``: a
    monitor that dies is started again by ``Stack.reconcile``, which knows
    whether the stack still runs; systemd does not."""
    return [
        "systemd-run",
        "--user",
        "--quiet",
        f"--unit={MONITOR_UNIT}",
        "--description=FR3 health monitor (terminal M), managed by the marvel0 page",
        "--collect",
        "--property=Type=exec",
        "--property=KillMode=control-group",
        "--property=KillSignal=SIGINT",
        "--property=TimeoutStopSec=10",
        "--property=Restart=no",
        "--property=Nice=10",
        "--property=StandardInput=null",
        "--setenv=PYTHONUNBUFFERED=1",
        "--",
        script,
    ]


def _executable(path) -> bool:
    if not path:
        return False
    try:
        st = os.stat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and os.access(path, os.X_OK)


# --- the background loop -------------------------------------------------------------


class Reconciler:
    """Calls ``Stack.reconcile`` every ``AUTO_INTERVAL_S`` in a daemon thread.
    ``start`` gives None (and no thread) outside profile ``real`` or with the
    setting off."""

    def __init__(self, stack: Stack, interval=AUTO_INTERVAL_S):
        self.stack = stack
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None

    @classmethod
    def start(cls, stack: Stack):
        h = stack.host
        if profile(h.cgroup(), h.in_main_checkout())["profile"] != "real":
            return None
        if not auto_enabled(h.environ):
            return None
        loop = cls(stack)
        loop._thread = threading.Thread(
            target=loop._run, name="levi-marvel0-monitor", daemon=True
        )
        loop._thread.start()
        return loop

    def _run(self):
        while not self._stop.wait(self.interval):
            with contextlib.suppress(Exception):
                self.stack.reconcile()

    def close(self):
        self._stop.set()
