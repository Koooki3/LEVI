"""The GPU side of the live service: may the local model run now, and its
vLLM server.

The machine has one 32 GB GPU that also runs the robot's policy server (a
JAX process that preallocates its share when it starts). Starting vLLM next
to a running policy server can make the *next* policy-server start fail, and
a vLLM request can slow the policy's action latency. So the service only
starts vLLM when it is allowed to, and gives the GPU back the moment it is
not (see ``decide`` and ``should_preempt``).

What this module never does: connect to a robot or policy port. "Is a policy
server up?" is answered from the kernel's socket table (``/proc/net/tcp``),
which a connect-probe could not do without being counted as a client.
VRAM is read with ``nvidia-smi --query-gpu``.

Standard library only.
"""

import contextlib
import fcntl
import json
import math
import os
import re
import signal
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ..own_units import OwnUnits
from . import jsonio
from .config import cpu_text, parse_cpus

HEALTH_TIMEOUT_S = 2.0


# --- what the machine is doing -------------------------------------------------


def listening_ports(tables=("/proc/net/tcp", "/proc/net/tcp6")) -> set:
    """TCP ports with a socket in LISTEN state, read from the kernel table."""
    ports = set()
    for table in tables:
        try:
            lines = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 4 or fields[3] != "0A":
                continue
            try:
                ports.add(int(fields[1].rsplit(":", 1)[1], 16))
            except (IndexError, ValueError):
                continue
    return ports


def vram(gpu_index=0, runner=subprocess.run) -> dict | None:
    """Total/used/free VRAM in MiB, or None when it cannot be read."""
    try:
        done = runner(
            [
                "nvidia-smi",
                "--query-gpu=memory.total,memory.used",
                "--format=csv,noheader,nounits",
                "-i",
                str(gpu_index),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        total, used = (
            int(float(x)) for x in done.stdout.strip().splitlines()[0].split(",")
        )
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None
    return {"total_mib": total, "used_mib": used, "free_mib": total - used}


def gpu_holders(runner=subprocess.run) -> list:
    """Compute processes on the GPU (pid, name, MiB); for status only."""
    try:
        done = runner(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,process_name,used_memory",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in done.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3 and parts[0].isdigit():
            rows.append(
                {
                    "pid": int(parts[0]),
                    "name": os.path.basename(parts[1])[:60],
                    "memory_mib": int(float(parts[2]))
                    if parts[2].replace(".", "").isdigit()
                    else None,
                }
            )
    return rows[:16]


# --- yielding the GPU (``[gpu.yield]``) -------------------------------------------
#
# Everything here only reads: the kernel's socket table, /proc/locks and
# ``nvidia-smi --query-compute-apps``. Nothing connects to a port, takes a
# lock for good or starts a process. A reading that fails is a reason to
# yield (fail closed): what cannot be seen is assumed to be in use.


def listening_ports_strict(tables=("/proc/net/tcp", "/proc/net/tcp6")) -> set:
    """Like ``listening_ports`` but ``OSError`` when the IPv4 table cannot be
    read (the IPv6 table may be absent on a machine without IPv6)."""
    Path(tables[0]).read_text()
    return listening_ports(tables)


def compute_apps(runner=subprocess.run) -> list:
    """Compute processes on the GPU as ``gpu_holders`` lists them, but every
    failure raises (``OSError`` or ``subprocess.SubprocessError``), and a row
    whose memory is not a number keeps ``memory_mib`` None. Incomplete PID
    rows raise ``ValueError`` rather than being silently discarded."""
    done = runner(
        [
            "nvidia-smi",
            "--query-compute-apps=pid,process_name,used_memory",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    if getattr(done, "returncode", 0) != 0:
        raise subprocess.CalledProcessError(
            done.returncode, done.args if hasattr(done, "args") else "nvidia-smi"
        )
    rows = []
    for line in done.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 3 or not parts[0].isdigit() or int(parts[0]) <= 0:
            raise ValueError("incomplete nvidia-smi compute process row")
        try:
            mib = int(float(parts[2]))
        except (ValueError, OverflowError):
            mib = None
        rows.append(
            {
                "pid": int(parts[0]),
                "name": os.path.basename(parts[1])[:60],
                "memory_mib": mib,
            }
        )
    return rows


def flock_holders(path, locks="/proc/locks", stat=os.stat) -> list | None:
    """Pids that hold a ``flock`` on the file ``path`` according to
    ``/proc/locks`` (no lock is taken, nothing is opened for writing); an empty
    list for a file that does not exist; None when it cannot be told from the
    table (unreadable, or an entry matches the inode but not the device)."""
    try:
        st = stat(Path(path).expanduser())
    except FileNotFoundError:
        return []
    except OSError:
        return None
    try:
        lines = Path(locks).read_text().splitlines()
    except OSError:
        return None
    found, loose = [], False
    for line in lines:
        fields = line.split()
        if len(fields) < 6 or fields[1] != "FLOCK":
            continue
        try:
            major, minor, inode = fields[5].split(":")
            if int(inode) != st.st_ino:
                continue
            same = (int(major, 16), int(minor, 16)) == (
                os.major(st.st_dev),
                os.minor(st.st_dev),
            )
            if not same:
                loose = True
                continue
            found.append(int(fields[4]))
        except ValueError:
            continue
    if not found and loose:
        return None
    return found


def flock_busy(path) -> bool:
    """Does somebody hold the lock on ``path``? A non-blocking shared lock is
    tried and let go at once (the table in ``flock_holders`` is preferred:
    it does not touch the lock at all). Only an absent file is not busy;
    unreadable files or failed probes raise so callers can fail closed."""
    try:
        fd = os.open(Path(path).expanduser(), os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


@dataclass
class Reason:
    """One reason the background labelling gives the GPU way."""

    code: str
    # deployment | robot_quiet | foreign_gpu | foreign_lock | probe_error
    text: str

    def public(self) -> dict:
        return {"code": self.code, "text": self.text[:300]}


def yield_reasons(
    config,
    *,
    ports: set | None,
    ports_error: str = "",
    sessions: dict,
    apps: list | None,
    apps_error: str = "",
    ours: set = frozenset(),
    policy_pids: set = frozenset(),
    lock_pids: list | None = None,
    lock_error: bool = False,
    check_gpu: bool = True,
) -> list:
    """Why the labelling must yield now (an empty list: no reason). Pure.

    ``ports``/``apps`` are None when they could not be read (then
    ``probe_error``); ``check_gpu`` False leaves the two GPU reasons out (the
    caller did not look: nothing is queued, no model is wanted).
    ``policy_pids`` are the GPU processes of the policy server (the one
    being evaluated, already expressed by ``deployment``): their memory is
    not someone else's. ``lock_pids`` are the holders of ``gpu.lock_file``
    other than this service (None with ``lock_error``: the table could not be read)."""
    y = config.gpu.yield_
    out: list = []
    if ports is None:
        out.append(
            Reason(
                "probe_error",
                f"the socket table cannot be read ({ports_error or 'error'}): "
                "assuming a policy server may be listening",
            )
        )
    else:
        up = sorted(int(p) for p in config.gpu.policy_ports if int(p) in ports)
        if up:
            out.append(
                Reason(
                    "deployment",
                    "a policy server is listening on port "
                    + ", ".join(str(p) for p in up)
                    + ": the policy is deployed",
                )
            )
    live = [
        s
        for s in sessions.values()
        if s.state not in ("stopped", "finished", "crashed") and not s.crashed
    ]
    if live:
        s = live[0]
        out.append(
            Reason(
                "deployment",
                f"evaluation session {s.group}/{s.task_folder} is {s.state}"
                + (f" (and {len(live) - 1} more)" if len(live) > 1 else ""),
            )
        )
    quiet = [
        s
        for s in sessions.values()
        if s.state in config.gpu.quiet_states and not s.crashed
    ]
    if quiet:
        s = quiet[0]
        out.append(
            Reason(
                "robot_quiet",
                f"{s.group}/{s.task_folder} is {s.state}: the robot moves "
                "(gpu.quiet_states)",
            )
        )
    if not check_gpu:
        return out
    if y.foreign_gpu_mib > 0:
        if apps is None:
            out.append(
                Reason(
                    "probe_error",
                    f"the GPU's processes cannot be read ({apps_error or 'nvidia-smi'}): "
                    "assuming someone else is using it",
                )
            )
        else:
            others = [
                a for a in apps if a["pid"] not in ours and a["pid"] not in policy_pids
            ]
            unknown = [a for a in others if a["memory_mib"] is None]
            used = sum(a["memory_mib"] or 0 for a in others)
            if used > y.foreign_gpu_mib or unknown:
                names = ", ".join(f"{a['name']}({a['pid']})" for a in others[:3]) + (
                    " ..." if len(others) > 3 else ""
                )
                out.append(
                    Reason(
                        "foreign_gpu",
                        f"other processes use {used} MiB of the GPU"
                        + (" (some amount unknown)" if unknown else "")
                        + f", over gpu.yield.foreign_gpu_mib ({y.foreign_gpu_mib}): "
                        + names,
                    )
                )
    if y.foreign_lock and config.gpu.lock_file:
        if lock_error:
            out.append(
                Reason(
                    "probe_error",
                    "the GPU lock's holders cannot be read: assuming it is held",
                )
            )
        elif lock_pids:
            out.append(
                Reason(
                    "foreign_lock",
                    "another process holds the GPU lock "
                    f"(pid {', '.join(str(p) for p in lock_pids[:3])})",
                )
            )
    return out


class YieldState:
    """The yield decision over time. It yields at once when a reason appears
    and carries on only after every reason has stayed gone for
    ``gpu.yield.resume_after_s`` (a deployment that restarts, a job that
    finishes and another begins, must not make the service flap)."""

    def __init__(self, resume_after_s: float):
        self.resume_after_s = resume_after_s
        self.active = False
        self.since: float | None = None
        self.reasons: list = []
        self.clear_since: float | None = None

    def update(self, now: float, reasons: list) -> bool:
        """Feed the reasons of this moment; True when ``active`` changed."""
        before = self.active
        if reasons:
            if not self.active:
                self.active, self.since = True, now
            self.clear_since = None
            self.reasons = list(reasons)
        elif self.active:
            if self.clear_since is None:
                self.clear_since = now
            waited = now - self.clear_since
            if waited >= self.resume_after_s:
                self.active, self.since = False, None
                self.reasons, self.clear_since = [], None
            else:
                self.reasons = [
                    Reason(
                        "resuming",
                        f"every reason has gone; resuming in "
                        f"{self.resume_after_s - waited:.0f} s",
                    )
                ]
        return before != self.active

    def public(self) -> dict:
        return {
            "active": self.active,
            "reasons": [r.public() for r in self.reasons],
            "since": self.since,
            "resume_at": (
                self.clear_since + self.resume_after_s
                if self.active and self.clear_since is not None
                else None
            ),
        }

    def text(self) -> str | None:
        """The reasons in one sentence (None when not yielding)."""
        if not self.active:
            return None
        return "; ".join(r.text for r in self.reasons)[:300]


# --- the decision ---------------------------------------------------------------


@dataclass
class Decision:
    allowed: bool
    reason: str
    # ok | settling | vram | lock | manual | external | external_busy | error
    code: str
    need_mib: int = 0


# vLLM's own total is this much below nvidia-smi's (context and reserve).
VLLM_TOTAL_SLACK_MIB = 500
# What a level-1-asleep vLLM still holds (measured 1.8-2.2 GB).
ASLEEP_RESIDENT_MIB = 2200


def need_mib(config, profile, total_mib, wake=False) -> int:
    """Free VRAM a start of a vLLM with this ``profile`` needs: its budget of
    vLLM's total plus ``vllm.margin_mib``. A *wake* needs the budget less what
    the sleeping vLLM still holds, plus the smaller ``gpu.wake_margin_mib``."""
    budget = profile["gpu_memory_utilization"] * (total_mib - VLLM_TOTAL_SLACK_MIB)
    if wake:
        return max(0, int(budget) + config.gpu.wake_margin_mib - ASLEEP_RESIDENT_MIB)
    return int(budget) + config.vllm.margin_mib


@dataclass
class Budget:
    ok: bool
    utilization: float = 0.0
    max_model_len: int = 0
    need_mib: int = 0
    code: str = "ok"  # ok | insufficient_vram
    reason: str = ""


def policy_loaded(config, policy_up: bool, policy_mib) -> str:
    """How the policy server counts for the budget: ``absent`` (not listening),
    ``loaded`` (holds at least ``gpu.policy_loaded_min_mib``), ``loading``
    (listens but holds less) or ``unknown`` (its memory cannot be read)."""
    if not policy_up:
        return "absent"
    if policy_mib is None:
        return "unknown"
    return "loaded" if policy_mib >= config.gpu.policy_loaded_min_mib else "loading"


def plan_budget(config, *, free_mib, total_mib, policy_up: bool) -> Budget:
    """The memory budget and context length to start vLLM with *now*.

    The budget is the share of vLLM's own total (nvidia-smi's less
    ``VLLM_TOTAL_SLACK_MIB``) that the free VRAM allows after ``margin_mib``,
    capped at ``gpu_memory_utilization_max``; with no policy server on the
    card it is the configured ``gpu_memory_utilization`` (0.74, calibrated on a
    warm compile cache: see ``config.Vllm``). The same number is what the start passes to vLLM, so the pre-check
    and the launch cannot disagree. If it is below what serves the context
    (``min_utilization_*``, less for a shorter context) the context steps down
    by 4096 to ``min_model_len``; if even that does not fit nothing is started:
    ``insufficient_vram`` with the numbers."""
    v = config.vllm
    total_v = total_mib - VLLM_TOTAL_SLACK_MIB
    cap = min(v.gpu_memory_utilization_max, (free_mib - v.margin_mib) / total_v)
    target = cap if policy_up else min(v.gpu_memory_utilization, cap)
    floor_full = v.min_utilization_with_policy if policy_up else v.min_utilization_alone
    length = v.max_model_len
    while True:
        saved = (v.max_model_len - length) * v.kv_bytes_per_token / 1048576 / total_v
        floor = max(v.gpu_memory_utilization_min, floor_full - saved)
        util = round(target - 0.0005, 4)  # a hair under what is free
        if util >= floor:
            need = int(util * total_v) + v.margin_mib
            return Budget(True, util, length, need)
        if length - 4096 < v.min_model_len:
            break
        length -= 4096
    floor = max(
        v.gpu_memory_utilization_min,
        floor_full
        - (v.max_model_len - v.min_model_len)
        * v.kv_bytes_per_token
        / 1048576
        / total_v,
    )
    need = int(floor * total_v) + v.margin_mib
    return Budget(
        False,
        target,
        0,
        need,
        "insufficient_vram",
        f"{free_mib} MiB of VRAM free allows a vLLM budget of {target:.3f}; "
        f"at least {floor:.3f} ({need} MiB free) is needed to serve even "
        f"{v.min_model_len} tokens"
        + (" beside the policy server" if policy_up else ""),
    )


def decide(
    config,
    mode,
    *,
    free_mib: int | None,
    need: int,
    since_policy_change_s: float | None,
) -> Decision:
    """May vLLM start (or wake) now? Pure: everything is passed in.

    An evaluation in progress does not forbid it: in timeshare the policy
    server and vLLM are both resident and ``gate`` decides when the model may
    *work*. What forbids a start is memory, a policy server still loading
    (it preallocates), or the mode."""
    if mode == "manual":
        return Decision(
            False,
            "gpu.mode is manual and no vLLM answers on the configured port",
            "manual",
        )
    since = since_policy_change_s
    if since is not None and since < config.gpu.settle_s:
        return Decision(
            False,
            f"a policy server appeared or went {since:.0f} s ago; settling for "
            f"{config.gpu.settle_s:.0f} s",
            "settling",
            need,
        )
    if free_mib is None:
        return Decision(False, "free VRAM cannot be read (nvidia-smi)", "vram", need)
    if free_mib < need:
        return Decision(
            False,
            f"{free_mib} MiB of VRAM free, {need} MiB needed",
            "vram",
            need,
        )
    return Decision(True, "the GPU has room for the local model", "ok", need)


@dataclass
class Gate:
    open: bool
    # open | policy_inferring | robot_quiet | episode_imminent | unknown_client
    code: str
    reason: str


def gate(
    config,
    mode,
    sessions,
    policy_up: bool,
    policy_since: float | None = None,
    *,
    now: float | None = None,
    waiting_since: dict | None = None,
) -> Gate:
    """May the model *work* (send requests) right now?

    Timeshare closes the gate while the policy infers: a session in a
    ``gpu.busy_states`` state (``running``), or -- with no session file at all
    to say otherwise -- a listening policy server, which may be called by a
    client this service knows nothing about. Homing, waiting for the reset,
    standby, fault, stopped, finished and a crashed client leave the GPU to the
    model. Coexist and manual never close it.

    ``gpu.quiet_states`` (empty by default) adds states in which the robot
    moves and the model must stay quiet (``robot_quiet``), in timeshare and
    coexist; manual still never closes the gate. Inference comes first: a
    session that is running names the gate ``policy_inferring``."""
    if mode not in ("timeshare", "coexist"):
        return Gate(True, "open", f"gpu.mode is {mode}")
    busy = [] if mode == "coexist" else _in_states(sessions, config.gpu.busy_states)
    if busy:
        s = busy[0]
        return Gate(
            False,
            "policy_inferring",
            f"{s.group}/{s.task_folder} is {s.state}: the policy is inferring",
        )
    quiet = _in_states(sessions, config.gpu.quiet_states)
    if quiet:
        s = quiet[0]
        return Gate(
            False,
            "robot_quiet",
            f"{s.group}/{s.task_folder} is {s.state}: the robot moves and "
            "gpu.quiet_states keeps the model quiet",
        )
    if mode == "coexist":
        return Gate(True, "open", f"gpu.mode is {mode}")
    # The next episode is due: a session waiting for the operator's reset
    # starts running ``reset_wait_s`` after it began waiting. Close ahead of it.
    if now is not None and waiting_since:
        lead, grace = config.gpu.lead_s, config.gpu.lead_grace_s
        for s in sessions.values():
            began = waiting_since.get(s.path)
            wait = getattr(s, "reset_wait_s", None)
            if s.state != "waiting_reset" or s.crashed or began is None or not wait:
                continue
            due = began + wait
            if due - lead <= now <= due + grace:
                return Gate(
                    False,
                    "episode_imminent",
                    f"{s.group}/{s.task_folder}: the next episode starts in "
                    f"{max(0.0, due - now):.1f} s",
                )
    if policy_up and not _witnesses(sessions, policy_since):
        return Gate(
            False,
            "unknown_client",
            "a policy server is listening and no current evaluation session says "
            "it is idle",
        )
    return Gate(True, "open", "the policy is not inferring")


def _in_states(sessions, states) -> list:
    """Sessions (not crashed) whose reported state is one of ``states``."""
    if not states:
        return []
    return [s for s in sessions.values() if s.state in states and not s.crashed]


def _witnesses(sessions, policy_since) -> bool:
    """Is there a session that can vouch for the policy server being idle?

    One that has not ended (standby, homing, waiting for the reset, fault).
    An ended one (finished, stopped, crashed) only if it ended after the
    policy server appeared: session files are never deleted, and yesterday's
    must not switch the protection off for a server some other client uses."""
    for s in sessions.values():
        if s.state not in ("finished", "stopped", "crashed"):
            return True
        ended = getattr(s, "updated_at", None)
        if policy_since is not None and ended and ended > policy_since:
            return True
    return False


def _bound_text(policy_mib) -> str:
    """What a ``ForeignEstimate`` adds to a reason that quotes it."""
    if not isinstance(policy_mib, ForeignEstimate):
        return ""
    text = (
        " (an upper bound: the listener is not one of our levi-aeri-policy-* "
        "units, so every GPU process not ours is counted"
    )
    return text + (f"; {policy_mib.note})" if policy_mib.note else ")")


def should_sleep(config, mode, *, free_mib: int | None, policy_mib: int | None):
    """``(code, why)`` a resident, awake vLLM must go to sleep, or None.

    Its memory is wanted: the free VRAM fell below ``gpu.min_free_mib``, or
    the policy server holds more than ``gpu.policy_budget_mib`` (a larger
    ``XLA_PYTHON_CLIENT_MEM_FRACTION`` than the .22 both fit with) so the two
    cannot share the card awake."""
    if mode == "manual":
        return None
    budget = config.gpu.policy_budget_mib
    if budget and policy_mib is not None and policy_mib > budget:
        return (
            "policy_large",
            (
                f"the policy server holds {policy_mib} MiB, over "
                f"gpu.policy_budget_mib ({budget} MiB): it cannot share the card "
                "with an awake vLLM; start it with "
                "XLA_PYTHON_CLIENT_MEM_FRACTION=.22 (7.6 GB)" + _bound_text(policy_mib)
            ),
        )
    if free_mib is not None and free_mib < config.gpu.min_free_mib:
        return "vram", f"free VRAM fell to {free_mib} MiB"
    return None


def policy_gpu_pids(ports, apps, *, budget_mib, view=None) -> set:
    """Pids among the GPU ``apps`` (rows with a ``pid`` and ``memory_mib``)
    that are *our* policy server, not merely something listening on one of
    ``ports``. The machine is shared: another person may serve a policy on the
    same port, so ownership is decided only from our own registry
    (``levi.own_units``): a GPU process counts only when

    * its pid is in the ``cgroup.procs`` of one of our ``levi-aeri-policy-*``
      units, and one of that unit's processes holds a socket listening on one
      of ``ports`` (the kernel's socket table and the fds of our own pids), and
    * the unit's GPU processes together hold at most ``budget_mib`` MiB
      (``gpu.policy_budget_mib``; 0: no limit); an amount that cannot be read
      fails the whole unit. Each unit is its own server with its own budget.

    No other pid's ``/proc`` entry is read. A unit whose cgroup cannot be
    read, or a systemd that cannot be asked, raises ``OSError``: the caller
    exempts nothing (the memory stays foreign). Nothing connects."""
    view = view or OwnUnits()
    inodes = view.listening_inodes(ports)
    if not inodes:
        return set()
    found = set()
    for pids in view.servers(view.policy_units(), inodes).values():
        members = [a for a in apps if a["pid"] in pids]
        amounts = [a.get("memory_mib") for a in members]
        if not members or any(m is None for m in amounts):
            continue
        if budget_mib > 0 and sum(amounts) > budget_mib:
            continue
        found.update(a["pid"] for a in members)
    return found


class ForeignEstimate(int):
    """A policy server's VRAM that is only an upper bound: something listens
    on a policy port but is not one of our ``levi-aeri-policy-*`` units (or
    their cgroups cannot be read), so its own memory cannot be told without
    reading another process's ``/proc``. The bound is the GPU memory of every
    process that is not ours (nvidia-smi's aggregate rows only). It counts for
    ``should_sleep`` (``policy_large``) and the budget like a reading; an
    online judgement refuses beside it (``Controller._online_wake``).
    ``note``: why the bound is larger than it might be (our own pids could
    not be told), said in the reasons that quote it."""

    note = ""

    @classmethod
    def of(cls, mib: int, note: str = "") -> "ForeignEstimate":
        out = cls(mib)
        out.note = note
        return out


def policy_vram_mib(
    ports, *, runner=subprocess.run, holders=None, view=None, exclude=()
):
    """VRAM (MiB) held by our policy unit(s) serving one of ``ports``: the
    GPU processes in the ``cgroup.procs`` of a ``levi-aeri-policy-*`` unit
    that listens there (``policy_gpu_pids``' rule, without the budget).

    When something listens there but no unit of ours does (a server started
    by hand, another person's, or cgroups that cannot be read): a
    ``ForeignEstimate``, the sum of the GPU memory of every process not in
    ``exclude`` (our own pids, for example our vLLM's; a callable is called
    only then, so no own-pid lookup happens while our unit serves the port, and
    may return ``(pids, note)``), a conservative upper
    bound. None when it cannot be told at all (nobody listens, no GPU row).
    Read-only: nothing connects, and no other pid's ``/proc`` entry is read.
    ``holders``: an earlier ``gpu_holders()`` result to reuse (one nvidia-smi
    for several lookups)."""
    view = view or OwnUnits()
    inodes = view.listening_inodes(ports)
    if not inodes:
        return None
    holders = {
        h["pid"]: h["memory_mib"]
        for h in (gpu_holders(runner) if holders is None else holders)
    }
    if not holders:
        return None
    try:
        servers = view.servers(view.policy_units(), inodes)
    except OSError:
        servers = {}
    if not servers:
        note = ""
        mine = exclude
        if callable(exclude):
            mine = exclude()
            if isinstance(mine, tuple):
                mine, note = mine
        mine = set(mine)
        return ForeignEstimate.of(
            sum(m or 0 for pid, m in holders.items() if pid not in mine), note
        )
    total, seen = 0, False
    for pids in servers.values():
        for pid in pids:
            mib = holders.get(pid)
            if mib is not None:
                total += mib
                seen = True
    return total if seen else None


# --- host pressure (the online judgement's optional refusal) ----------------------


PRESSURE_DIR = "/proc/pressure"


def host_pressure(kinds, root=PRESSURE_DIR) -> dict:
    """``{kind: some avg10}`` (a percentage) of each of ``kinds`` that can be
    read from ``<root>/<kind>`` (Pressure Stall Information); a kind whose
    file is missing or unreadable is left out."""
    found = {}
    for kind in kinds:
        try:
            text = (Path(root) / kind).read_text()
        except OSError:
            continue
        for line in text.splitlines():
            fields = line.split()
            if not fields or fields[0] != "some":
                continue
            for item in fields[1:]:
                key, _, value = item.partition("=")
                if key == "avg10":
                    try:
                        found[kind] = float(value)
                    except ValueError:
                        pass
            break
    return found


def pressure_over(online, reader=None) -> str | None:
    """Why an online judgement is refused for host pressure now, or None.
    ``online``: the ``[online]`` settings; off (``pressure_avg10_max`` 0)
    reads nothing. Unreadable files never refuse."""
    limit = online.pressure_avg10_max
    if not limit or limit <= 0:
        return None
    read = reader or host_pressure
    over = {
        kind: value
        for kind, value in read(online.pressure_kinds).items()
        if value > limit
    }
    if not over:
        return None
    words = ", ".join(f"{k} some avg10 {v:g}%" for k, v in sorted(over.items()))
    return f"{words} is over online.pressure_avg10_max ({limit:g}%)"


# --- the shared GPU lock -----------------------------------------------------------


class GpuLock:
    """The machine's advisory GPU lock (``gpu.lock_file``), held for as long as
    this service's vLLM holds the GPU. Other users take it with ``flock``;
    this is the same lock.

    ``state`` says what the last ``acquire`` found: ``disabled`` (no lock file
    configured), ``held``, ``busy`` (another process holds it) or
    ``unavailable`` (the file cannot be opened or created; ``detail`` says
    why). An unavailable lock is never passed over silently: ``acquire``
    returns ``allow_unavailable`` (``gpu.lock_unavailable = continue``: True,
    the old behaviour, with the state shown in the status and the doctor;
    ``wait``: False, vLLM is not started)."""

    def __init__(self, path, agent="live", allow_unavailable=True):
        self.path = path
        self.agent = agent
        self.allow_unavailable = allow_unavailable
        self._handle = None
        self.state = "disabled" if not path else "free"
        self.detail = ""

    def acquire(self) -> bool:
        if not self.path:
            self.state, self.detail = "disabled", ""
            return True
        if self._handle:
            return True
        target = Path(self.path).expanduser()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            handle = open(target, "a")  # noqa: SIM115
        except OSError as exc:
            # No usable lock file: the convention cannot apply. Say so.
            self.state = "unavailable"
            self.detail = f"{target}: {exc.strerror or type(exc).__name__}"
            return bool(self.allow_unavailable)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            self.state, self.detail = "busy", ""
            return False
        self._handle = handle
        self.state, self.detail = "held", ""
        return True

    def release(self):
        if self._handle:
            # Close, do not LOCK_UN: an explicit unlock would release the
            # lock for every process sharing the descriptor, vLLM included.
            self._handle.close()
            self._handle = None
            self.state = "free"

    @property
    def held(self) -> bool:
        return self._handle is not None

    @property
    def unavailable(self) -> bool:
        return self.state == "unavailable"

    def public(self) -> dict:
        """For the status file: the lock's path is not shown, only its state."""
        return {
            "state": self.state,
            "detail": self.detail[:200],
            "configured": bool(self.path),
            "on_unavailable": "continue" if self.allow_unavailable else "wait",
        }

    def fileno(self):
        """The descriptor of the held lock (None when not held): a vLLM
        started with it open (``pass_fds``) keeps the lock for as long as it
        lives, even if this process dies."""
        return self._handle.fileno() if self._handle else None


# --- vLLM -----------------------------------------------------------------------------


# /proc/<pid>/stat states of a process that has exited: a zombie, and "dead"
# (X, x: shown for a moment while it is being reaped). A process that dies
# can read Z and then X before it disappears, so treating X as alive made a
# stopped vLLM look alive again for an instant.
EXITED = frozenset("ZXx")


def identity_strict(pid):
    """Exact live identity, None only for a missing/exited process.

    Unreadable or malformed evidence raises; missing boot identity is unknown.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        raise ValueError("invalid process pid")
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except FileNotFoundError:
        return None
    except IndexError as exc:
        raise ValueError("malformed process stat") from exc
    if len(fields) < 20 or not fields[19].isdigit():
        raise ValueError("incomplete process stat")
    if fields[0] in EXITED:
        return None
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if not boot:
        raise ValueError("missing boot identity")
    return {"start_ticks": fields[19], "boot": boot}


def identity(pid):
    """Live identity for status; None also when the reading is unavailable."""
    try:
        return identity_strict(pid)
    except (OSError, ValueError):
        return None


def process_state(pid, recorded) -> str:
    """alive/gone/unknown for the original owner; PID reuse is gone, not ours."""
    try:
        current = identity_strict(pid)
    except (OSError, ValueError):
        return "unknown"
    if current is None:
        return "gone"
    if (
        not isinstance(recorded, dict)
        or not recorded.get("start_ticks")
        or not recorded.get("boot")
    ):
        return "unknown"
    return "alive" if current == recorded else "gone"


def same_process(pid, recorded) -> bool:
    """Is ``pid`` still the process that was recorded? A recorded identity of
    None is never a match (None == None would make a pid that is gone, or was
    never read, look alive and ours)."""
    return bool(pid) and recorded is not None and identity(pid) == recorded


def healthy(port, opener=urllib.request.urlopen) -> bool:
    """Whether a vLLM server answers /health on the loopback port."""
    try:
        with opener(f"http://127.0.0.1:{port}/health", timeout=HEALTH_TIMEOUT_S) as r:
            return r.status == 200
    except (OSError, ValueError):
        return False


def script_env(config) -> dict:
    """The environment the launch and stop scripts get (docs/VLLM.md): this
    process's, plus where the pid file goes and which model to serve under
    which name. A script of one's own may ignore the LEVI_VLLM_* variables;
    it must still write ``<vllm.pid_dir>/vllm_<PORT>.pid``."""
    v = config.vllm
    env = dict(os.environ)
    env["LEVI_VLLM_PID_DIR"] = str(config.vllm_pid_dir)
    env["LEVI_VLLM_SERVED_NAME"] = v.served_model
    if v.model:
        env["LEVI_VLLM_MODEL"] = v.model
    return env


def launch_prefix(config, current_nice=None) -> list:
    """What goes in front of the launch script for ``vllm.cpus`` (``taskset
    -c``) and ``vllm.nice`` (``nice -n``); empty with the defaults, so the
    command is exactly the script and its arguments. ``nice -n`` adds to the
    niceness the script would inherit (this process's, ``current_nice``), so
    the increment is what takes vLLM to ``vllm.nice``; none when it already
    runs there or higher (a process cannot lower it without privileges)."""
    v, prefix = config.vllm, []
    if v.cpus:
        cpus = parse_cpus(v.cpus)  # validated: a list a typo cannot reach
        prefix += ["taskset", "-c", cpu_text(cpus)]
    if v.nice != -1:
        now = (
            os.getpriority(os.PRIO_PROCESS, 0) if current_nice is None else current_nice
        )
        if v.nice > now:
            prefix += ["nice", "-n", str(v.nice - now)]
    return prefix


def launch_args(config, profile) -> list:
    """The ``serve-qwen38.sh`` arguments of the measured shared profile:
    xgrammar guided decoding, greedy sampling, the profile's context, image
    and batch limits, and (for sleeping) ``--enable-sleep-mode``."""
    v = config.vllm
    args = [
        "--structured-outputs-config",
        '{"backend":"xgrammar","disable_any_whitespace":true}',
        "--max-model-len",
        str(profile["max_model_len"]),
        "--limit-mm-per-prompt",
        '{"image":' + str(profile["max_images"]) + ',"video":0}',
        "--max-num-seqs",
        str(profile["max_num_seqs"]),
        "--max-num-batched-tokens",
        str(profile["max_num_batched_tokens"]),
        "--override-generation-config",
        '{"temperature": ' + str(v.temperature) + "}",
    ]
    if profile.get("sleep_mode"):
        args.append("--enable-sleep-mode")
    return args


def _post(port, path, timeout=30.0) -> bool:
    """POST to this service's own vLLM on the loopback port (no body)."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{int(port)}{path}", data=b"", method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (OSError, ValueError):
        return False


def is_sleeping(port) -> bool | None:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{int(port)}/is_sleeping", timeout=HEALTH_TIMEOUT_S
        ) as response:
            return bool(json.loads(response.read(1024)).get("is_sleeping"))
    except (OSError, ValueError):
        return None


def _group(pgid) -> list:
    """Processes whose process group is ``pgid`` (vLLM's engine processes
    share the group of the server serve.sh started)."""
    members = []
    try:
        for entry in os.scandir("/proc"):
            if entry.name.isdigit():
                try:
                    fields = (
                        Path(f"/proc/{entry.name}/stat")
                        .read_text()
                        .rsplit(")", 1)[1]
                        .split()
                    )
                except (OSError, IndexError):
                    continue
                if int(fields[2]) == pgid:
                    members.append(int(entry.name))
    except OSError:
        pass
    return members or [pgid]


def _last_line(path, tail=4096) -> str:
    """The last non-empty line of a log file ("" when unreadable)."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - tail))
            lines = handle.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return ""
    return next((x.strip() for x in reversed(lines) if x.strip()), "")


STOP_CONFIRM_S = 60.0  # how long a stop waits for the GPU to be really free


def _group_alive(pgid) -> list:
    """Complete live process-group scan; unreadable members/root are unknown.

    A member disappearing during the scan is gone, so ENOENT is safe to skip.
    Any other read error or malformed stat prevents an exit proof.
    """
    alive = []
    with os.scandir("/proc") as entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                fields = (
                    Path(f"/proc/{entry.name}/stat")
                    .read_text()
                    .rsplit(")", 1)[1]
                    .split()
                )
            except FileNotFoundError:
                continue
            except IndexError as exc:
                raise ValueError("malformed process-group stat") from exc
            if len(fields) < 3:
                raise ValueError("incomplete process-group stat")
            if int(fields[2]) == pgid and fields[0] not in EXITED:
                alive.append(int(entry.name))
    return alive


class Vllm:
    """The service's own vLLM server: start, watch, stop. It stops only a
    process it started (verified by its recorded identity), never another
    agent's server on the same port."""

    def __init__(self, config, *, popen=subprocess.Popen, runner=subprocess.run):
        self.config = config
        self.popen = popen
        self.runner = runner
        self.record_path = config.live_dir / "vllm.json"
        self.state = "stopped"
        self.started_at: float | None = None
        self.profile: dict | None = None
        # Remembered members and unresolved launch/stop ownership. Clear only
        # after confirmed exit. ``start()`` refuses pending ownership;
        # ``leaving()`` reads this flag without probing processes or the GPU.
        self._leaving: set = set()
        self._confirm_pending = False
        # Latest evidence from this launch survives a failed atomic record write.
        self._launch_record: dict | None = None
        # Called while a stop waits (every 0.5 s): the controller keeps the
        # gate file fresh with it, or a long stop would let it go stale.
        self.keepalive = None
        self.error = ""
        self._health = (0.0, False)
        # How long a cold start and a wake took, each ``{"s": seconds, "at":
        # when}``: the controller hands the fresh ones to the worker it starts
        # (``pending_timings``) and forgets them once that worker's batch
        # finished (``clear_timings``).
        self.timings: dict = {}
        self._cold_pending = False
        # Why the last launch through ``vllm.cpus``/``vllm.nice`` failed before
        # vLLM ran: vLLM's own log then holds only older starts' errors.
        self._launch_error = ""
        self._adopt()

    # --- bookkeeping ---------------------------------------------------------

    @property
    def port(self) -> int:
        return self.config.vllm.port

    def _pidfile(self) -> Path:
        return self.config.vllm_pid_dir / f"vllm_{self.port}.pid"

    def _pid(self):
        try:
            return int(self._pidfile().read_text().strip())
        except (OSError, ValueError):
            return None

    def _record(self):
        """Read lifecycle evidence without conflating an unreadable record with absence."""
        try:
            record = json.loads(self.record_path.read_text())
        except FileNotFoundError:
            return self._launch_record
        if not isinstance(record, dict):
            raise ValueError("invalid vLLM owner record")  # noqa: TRY004 - invalid JSON evidence
        pid = record.get("pid")
        if not (record.get("launch_pending") is True and pid is None) and (
            not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1
        ):
            raise ValueError("invalid vLLM owner pid")
        launcher = record.get("launcher")
        if launcher is not None and (
            not isinstance(launcher, dict)
            or any(
                not isinstance(launcher.get(key), int)
                or isinstance(launcher.get(key), bool)
                or launcher[key] <= 1
                for key in ("pid", "group")
            )
        ):
            raise ValueError("invalid vLLM launcher record")
        leaving = record.get("leaving", [])
        if not isinstance(leaving, list) or any(
            not isinstance(member, int) or isinstance(member, bool) or member <= 1
            for member in leaving
        ):
            raise ValueError("invalid pending vLLM members")
        return self._launch_record or record

    def _pending(self, record, reason):
        """Keep original identity and remembered members, also across supervisor restart."""
        self.state, self._confirm_pending = "error", True
        prefix = "vLLM stop unconfirmed: "
        suffix = (
            "; GPU ownership remains unresolved; no release is confirmed; "
            "see docs/VLLM.md"
        )
        budget = 300 - len(prefix) - len(suffix)
        if len(reason) > budget:
            # Keep the error kind and script basename even for a long path.
            half = (budget - 3) // 2
            reason = reason[:half] + "..." + reason[-(budget - 3 - half) :]
        self.error = prefix + reason + suffix
        if record is not None:
            if record.get("pid") is not None:
                self._leaving.add(record["pid"])
            saved = dict(record)
            saved.update(
                confirm_pending=True,
                leaving=sorted(self._leaving),
                stop_error=self.error,
            )
            if self._launch_record is not None or record.get("launch_pending"):
                self._launch_record = saved
            try:
                jsonio.write(self.record_path, saved)
            except (OSError, ValueError):
                self.error = (self.error + "; owner record could not be saved")[:300]
        return False

    def _adopt(self):
        """Only take back a proved live owner; unknown evidence retains its record."""
        try:
            record = self._record()
            if record is None:
                return
            self._leaving = set(record.get("leaving", []))
            if record.get("confirm_pending"):
                if record.get("launch_pending"):
                    self._launch_record = record
                self.state, self._confirm_pending = "error", True
                self.error = (
                    record.get("stop_error") or "vLLM stop confirmation is pending"
                )
                return
            status = process_state(record["pid"], record.get("identity"))
            if status == "alive" and self._pid() == record["pid"]:
                self.state = (
                    "starting"
                    if not healthy(self.port)
                    else ("asleep" if is_sleeping(self.port) else "ready")
                )
                self.started_at = record.get("started_at")
                self.profile = record.get("profile")
            elif status == "gone":
                # Adoption/status must not wait for a slow physical stop.
                self._pending(
                    record, "original leader exited; group/GPU confirmation pending"
                )
            else:
                self._pending(record, "owner identity or pid file is unknown")
        except (OSError, ValueError, TypeError):
            self._pending(None, "owner record is unreadable")

    def holds_lock(self, lock_file) -> bool:
        """Does the vLLM process (or a child) hold the GPU lock file open? It
        does when the service passed it the lock's descriptor at launch, which
        keeps the lock for as long as vLLM lives, even if the service dies."""
        if not lock_file or not self.mine():
            return False
        pid = (jsonio.read(self.record_path) or {}).get("pid")
        try:
            target = os.stat(Path(lock_file).expanduser())
        except OSError:
            return False
        for member in _group(pid):
            try:
                fds = os.scandir(f"/proc/{member}/fd")
            except OSError:
                continue
            with fds:
                for fd in fds:
                    try:
                        st = os.stat(fd.path)
                    except OSError:
                        continue
                    if (st.st_ino, st.st_dev) == (target.st_ino, target.st_dev):
                        return True
        return False

    def mine(self) -> bool:
        record = jsonio.read(self.record_path)
        return isinstance(record, dict) and same_process(
            record.get("pid"), record.get("identity")
        )

    def external(self) -> bool:
        """A vLLM answers on the port but was not started by this service."""
        return not self.mine() and healthy(self.port)

    # --- lifecycle -------------------------------------------------------------

    def start(self, profile, fd=None) -> bool:
        """Launch vLLM with ``profile``; False (with ``error``) if it could not."""
        if self.state in ("starting", "ready") and self.mine():
            return True
        # A new launch must never overwrite an unresolved original owner.
        try:
            record = self._record()
        except (OSError, ValueError):
            return self._pending(None, "owner record is unreadable")
        if self._confirm_pending or record is not None:
            return self._pending(
                record, "previous owner requires confirmed stop before launch"
            )
        self._leaving = set()
        self._launch_error = ""
        prefix: list = []
        # Failures here precede command execution and still use normal backoff.
        try:
            env = script_env(self.config)
            env.update(
                PORT=str(self.port),
                GPU_UTIL=str(profile["gpu_memory_utilization"]),
                VLLM_WAIT_S="0",
                LEVI_AGENT=self.config.gpu.lock_agent,
            )
            if profile.get("sleep_mode"):
                env["VLLM_SERVER_DEV_MODE"] = "1"
            prefix = launch_prefix(self.config)
            argv = [
                *prefix,
                str(self.config.vllm_script),
                *launch_args(self.config, profile),
            ]
            self.config.logs_dir.mkdir(parents=True, exist_ok=True)
            log = self.config.logs_dir / "vllm-launch.log"
            sink = log.open("ab")
        except (OSError, ValueError) as exc:
            self.state, self.error = "error", f"vLLM launch preparation failed: {exc}"
            self._launch_error = self.error
            return False

        previous_pid = self._pid()
        record = {
            "pid": None,
            "identity": None,
            "launch_pending": True,
            "confirm_pending": True,
            "port": self.port,
            "profile": profile,
            "started_at": time.time(),
        }
        # Reserve unresolved ownership before any launcher can create children.
        self._launch_record, self._confirm_pending = record, True
        process, code, failure = None, None, ""
        with sink:
            try:
                jsonio.write(self.record_path, record)
            except (OSError, ValueError):
                return self._pending(
                    record, "launch owner record could not be saved; launcher not run"
                )
            try:
                process = self.popen(
                    argv,
                    env=env,
                    stdout=sink,
                    stderr=sink,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                    pass_fds=(fd,) if fd is not None else (),
                )
                launcher_pid = getattr(process, "pid", None)
                if (
                    isinstance(launcher_pid, int)
                    and not isinstance(launcher_pid, bool)
                    and launcher_pid > 1
                ):
                    launcher = {
                        "pid": launcher_pid,
                        "identity": None,
                        "group": launcher_pid,
                    }
                    record["launcher"] = launcher
                    try:
                        launcher["identity"] = identity_strict(launcher_pid)
                        if launcher["identity"] is not None:
                            try:
                                launcher["group"] = os.getpgid(launcher_pid)
                            except ProcessLookupError:
                                if (
                                    process_state(launcher_pid, launcher["identity"])
                                    != "gone"
                                ):
                                    raise
                        if launcher["group"] != launcher_pid:
                            raise ValueError("launcher process group changed")
                        jsonio.write(self.record_path, record)
                    except (OSError, ValueError):
                        failure = "launcher identity/group or owner record is unknown"
                code = process.wait(timeout=120)
            except (OSError, subprocess.SubprocessError) as exc:
                failure = f"vLLM launch failed: {type(exc).__name__} running {self.config.vllm_script}"

        # The pid file is only an owner candidate from this launch. Never adopt
        # an unchanged previous file, or treat unreadable identity as exit.
        pid = self._pid() if process is not None else None
        if pid and pid > 1 and pid != previous_pid:
            record["pid"] = pid
            try:
                record["identity"] = identity_strict(pid)
            except (OSError, ValueError):
                failure = "launched server identity is unknown"
        else:
            failure = failure or "launched server pid file is missing or unchanged"
        if code is not None and code != 0 and prefix:
            self._launch_error = (
                f"the launch through {' '.join(prefix)} (vllm.cpus, vllm.nice) "
                f"exited {code}: {_last_line(log)}"
            )[:300]
            failure = self._launch_error
        elif code is not None and code != 0:
            self.error = f"serve script exited {code}; see vllm-launch.log"
            failure = "; ".join(filter(None, (self.failure_reason(), failure)))
        if failure or record["identity"] is None:
            self.error = (
                failure or "launched server exited before identity was recorded"
            )
            return self._pending(record, self.failure_reason())

        self.started_at = record["started_at"]
        self.profile = profile
        complete = dict(record, launch_pending=False, confirm_pending=False)
        try:
            jsonio.write(self.record_path, complete)
        except (OSError, ValueError):
            return self._pending(
                record, "launched server owner record could not be saved"
            )
        self._launch_record = complete
        self._confirm_pending, self._cold_pending = False, True
        self.state, self.error = "starting", ""
        return True

    def log_path(self) -> Path:
        return self.config.vllm_pid_dir / f"vllm_{self.port}.log"

    def failure_reason(self) -> str:
        """Why the last start failed: the last error line of vLLM's own log
        (``ValueError: ... KV cache ...``), else what the service knows. A
        launch through ``vllm.cpus``/``vllm.nice`` that failed before vLLM ran
        says that instead (vLLM's log holds only older errors then)."""
        if self._launch_error:
            return self._launch_error[:300]
        try:
            with self.log_path().open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - 65536))
                text = handle.read().decode("utf-8", "replace")
        except OSError:
            text = ""
        found = re.findall(
            r"^.*?\b(\w*(?:Error|Exception)\b[^\n]*)$", text, re.MULTILINE
        )
        if found:
            return (self._explain_kv(found[-1]) or found[-1].strip())[:300]
        return (self.error or "vLLM failed to start")[:300]

    def _explain_kv(self, line: str) -> str | None:
        """vLLM's "1.82 GiB KV cache is needed ... available (1.68 GiB)" turned
        into what it means here: the memory budget is too small, and by how
        much (budget u leaves u * 31.36 GiB less a fixed amount for the KV
        cache, so the missing GiB divided by vLLM's total is the budget to add)."""
        m = re.search(
            r"\(([\d.]+) GiB KV cache is needed.*?available KV cache memory "
            r"\(([\d.]+) GiB\)(?:.*?estimated maximum model length is (\d+))?",
            line,
        )
        if not m:
            return None
        need, have = float(m.group(1)), float(m.group(2))
        used = (self.profile or {}).get("gpu_memory_utilization")
        total_gib = (32607 - VLLM_TOTAL_SLACK_MIB) / 1024
        text = (
            f"the memory budget is too small: the KV cache needs {need:g} GiB "
            f"but only {have:g} GiB fits"
        )
        if used:
            suggested = (
                math.ceil((used + (need - have) / total_gib + 0.002) * 1000) / 1000
            )
            text += (
                f" at --gpu-memory-utilization {used:g}; use at least {suggested:g} "
                "(vllm.gpu_memory_utilization)"
            )
        if m.group(3):
            text += (
                f", or a context of at most {m.group(3)} tokens (vllm.max_model_len)"
            )
        return text

    def _healthy(self) -> bool:
        """/health, asked at most once a second (the supervisor ticks four
        times a second while a worker runs)."""
        now = time.monotonic()
        if now - self._health[0] >= 1.0:
            self._health = (now, healthy(self.port))
        return self._health[1]

    def poll(self) -> str:
        """Advance ``starting`` to ``ready`` (or ``error`` on timeout/death)."""
        if self.state == "asleep":
            if not self.mine():
                self.state, self.error = "error", "vLLM exited while asleep"
            return self.state
        if self.state == "starting":
            if self._healthy():
                self.state = "ready"
                if self._cold_pending:
                    self._cold_pending = False
                    self.timings["vllm_cold_start_s"] = {
                        "s": round(time.time() - (self.started_at or time.time()), 1),
                        "at": time.time(),
                    }
                if is_sleeping(self.port):  # adopted a server left asleep
                    self.state = "asleep"
            elif not self.mine():
                self.state, self.error = "error", "vLLM exited while starting"
                self.error = self.failure_reason()
            elif (
                time.time() - (self.started_at or 0) > self.config.vllm.start_timeout_s
            ):
                self.stop()
                self.state, self.error = "error", "vLLM did not become healthy in time"
        elif self.state == "ready" and not self._healthy():
            try:
                record = self._record()
            except (OSError, ValueError):
                self._pending(None, "owner record is unreadable")
            else:
                if (
                    record is not None
                    and process_state(record["pid"], record.get("identity")) != "alive"
                ):
                    self._pending(record, "owner identity is gone or unknown")
        return self.state

    def pending_timings(self, now, max_age_s) -> dict:
        """``{name: {"s", "at"}}`` of the cold start and wake no older than
        ``max_age_s``; older ones are dropped (they belong to nothing that is
        still running). Not cleared: see ``clear_timings``."""
        self.timings = {
            k: v for k, v in self.timings.items() if now - v["at"] <= max_age_s
        }
        return dict(self.timings)

    def clear_timings(self, given) -> None:
        """Forget what was handed over (``{name: {"at"}}``), unless a newer
        one has been measured since."""
        for key, row in given.items():
            if (self.timings.get(key) or {}).get("at") == row.get("at"):
                self.timings.pop(key, None)

    def sleep(self) -> bool:
        """Level-1 sleep: weights to host memory, KV dropped, most of the VRAM
        freed (5 s down). Only this service's own, awake server."""
        if not self.mine() or self.state != "ready":
            return False
        if not (self.profile or {}).get("sleep_mode"):
            return False
        if _post(self.port, "/sleep?level=1", timeout=60.0) and is_sleeping(self.port):
            self.state = "asleep"
            return True
        self.error = "vLLM did not go to sleep"
        return False

    def wake(self, timeout=60.0) -> bool:
        """Wake a sleeping server (under a second). The caller has checked
        that the free VRAM covers it. ``timeout``: how long to wait for the
        server's answer (the online judgement waits less)."""
        if not self.mine() or self.state != "asleep":
            return self.state == "ready"
        began = time.time()
        if (
            _post(self.port, "/wake_up", timeout=timeout)
            and is_sleeping(self.port) is False
        ):
            self.state, self.error = "ready", ""
            self.timings["vllm_wake_s"] = {
                "s": round(time.time() - began, 2),
                "at": time.time(),
            }
            return True
        self.error = "vLLM did not wake up"
        return False

    def stop(self) -> bool:
        """Stop only a proved original owner; release only on complete exit evidence."""
        try:
            record = self._record()
        except (OSError, ValueError):
            return self._pending(None, "owner record is unreadable")
        if record is None:
            if self._confirm_pending:
                return self._pending(None, "pending owner record is missing")
            self.state = "stopped"
            return True
        if record.get("pid") is None:
            return self._pending(record, "launch has no verified server owner")
        pid, recorded = record["pid"], record.get("identity")
        self._leaving.add(pid)
        status = process_state(pid, recorded)
        if status == "unknown":
            return self._pending(record, "owner identity is unknown")
        if record.get("launch_pending") and status != "gone":
            return self._pending(
                record, "incomplete launch still has a live server owner"
            )
        try:
            self._leaving.update(_group_alive(pid))
            self._launch_group(record)
        except (OSError, ValueError):
            return self._pending(record, "process group is unknown")
        self.state = "stopping"
        self._health = (0.0, False)
        if status == "alive":
            # Never call the PID-based stop script for a reused/unknown PID.
            if self._pid() == pid and process_state(pid, recorded) == "alive":
                try:
                    self.runner(
                        [str(self.config.vllm_stop_script), "--stop", str(self.port)],
                        env=script_env(self.config),
                        capture_output=True,
                        timeout=120,
                        check=False,
                    )
                except (OSError, subprocess.SubprocessError):
                    pass
            if process_state(pid, recorded) == "alive":
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(pid, signal.SIGTERM)
                deadline = time.time() + 30
                while (
                    process_state(pid, recorded) == "alive" and time.time() < deadline
                ):
                    self._beat()
                    time.sleep(0.5)
                if process_state(pid, recorded) == "alive":
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(pid, signal.SIGKILL)
        deadline = time.time() + STOP_CONFIRM_S
        while True:
            status = process_state(pid, recorded)
            try:
                group = _group_alive(pid) + self._launch_group(record)
                self._leaving.update(group)
                on_gpu = self._on_gpu(self._leaving)
            except (OSError, subprocess.SubprocessError, ValueError):
                return self._pending(record, "process group or GPU query is unknown")
            if status == "unknown":
                return self._pending(record, "owner identity is unknown")
            if status == "gone" and not group and not on_gpu:
                try:
                    self.record_path.unlink()
                except OSError:
                    return self._pending(record, "owner record could not be removed")
                self._leaving, self._confirm_pending = set(), False
                self._launch_record = None
                self.state, self.error, self.profile = "stopped", "", None
                return True
            if time.time() >= deadline:
                return self._pending(
                    record, "original process/group or GPU allocation remains"
                )
            self._beat()
            time.sleep(0.5)

    def _launch_group(self, record):
        """Keep an observed launcher/group in the exit proof, without signalling it."""
        launcher = record.get("launcher")
        if not launcher:
            return []
        self._leaving.add(launcher["pid"])
        status = process_state(launcher["pid"], launcher.get("identity"))
        if status == "unknown":
            raise ValueError("launcher identity is unknown")
        members = _group_alive(launcher["group"])
        self._leaving.update(members)
        if status == "alive":
            members = [launcher["pid"], *members]
        return members

    def _beat(self):
        if self.keepalive:
            with contextlib.suppress(Exception):
                self.keepalive()

    def leaving(self) -> bool:
        """A launch or stop still needs owner exit confirmation."""
        return self._confirm_pending and self.state == "error"

    @staticmethod
    def _on_gpu(pids) -> bool:
        """Does any of ``pids`` still appear among nvidia-smi's compute
        processes? Unreadable/incomplete evidence raises, never means empty."""
        return any(row["pid"] in pids for row in compute_apps())

    def public(self) -> dict:
        return {
            "state": self.state,
            "port": self.port,
            "profile": (self.profile or {}).get("profile"),
            "max_model_len": (self.profile or {}).get("max_model_len"),
            "started_at": self.started_at,
            "error": self.error,
            "owned": self.mine(),
        }
