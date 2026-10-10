"""Read-only status probes for the setup wizard (``GET /api/levi/setup/status``).

Everything here reads files and kernel tables; nothing connects to a port,
takes a lock, writes a file or starts a process other than ``nvidia-smi``
with query options (NVML; it creates no CUDA context):

- listening ports: the LISTEN rows of ``/proc/net/tcp`` and ``tcp6``, their
  socket inode matched to ``/proc/<pid>/fd`` (processes of the same user;
  others are reported without an owner). **No connection is made**, not even
  to LEVI's own ports;
- GPU memory, utilisation and compute processes: ``nvidia-smi --query-gpu``
  and ``--query-compute-apps``; a process is named by the port it (or a
  parent) listens on, else ``other``;
- GPU lock holders: ``/proc/locks`` matched to the lock files' device and
  inode (the lock is never taken);
- vLLM asleep/awake, the GPU gate and the admission decision: the live
  service's ``status.json`` (no request to vLLM's port);
- CPU, memory, swap and pressure (PSI): ``/proc/loadavg``, ``/proc/stat``,
  ``/proc/meminfo``, ``/proc/pressure/*``;
- ROS control alarms: counts in the newest ``ros2_control_node_*.log`` files,
  with the categories of the lab's FR3 diagnostics recorder (communication
  constraints violation, reflexes, overruns, other errors);
- the FR3 health file, summarised as ``levi.live.sessions.read_fr3`` does;
- free disk space: ``shutil.disk_usage``;
- the diagnostics recorder: its ``recorder.pid`` (checked against the
  process start time) and ``status.json``;
- the real-robot quiet guard: the same rule as the lab's ``longrun.py guard
  --robot-quiet`` (robot-side process names in a command line, pressure
  avg10 and swap thresholds), implemented here on ``/proc`` alone, plus the
  live service's gate and the product's own running jobs.

**Nothing can hang a request.** Every file is read through ``read_regular``:
``lstat`` first, then ``O_NONBLOCK | O_NOFOLLOW``, ``fstat`` again, a size
cap; a FIFO, device, socket or symbolic link is ``unknown`` ("not a regular
file") and is never opened for reading. Each probe runs in its own daemon
thread with a deadline (``PROBE_TIMEOUT_S``, ``GPU_TIMEOUT_S`` for
``nvidia-smi``); one that misses it is ``unknown`` ("timed out"), and while
it is still stuck (a dead network mount) later samples answer ``unknown``
for it at once instead of starting another. Building the context has its
own deadline too.

**Sampling is lazy and bounded.** Nothing samples in the background: a
sample is taken when a request arrives, and a request within ``ttl_s`` of
the last sample gets that sample again. One request samples at a time; the
others wait for it, never longer than ``WAIT_S``, and then get the last
sample (``stale``) or ``{"state": "sampling"}``. No lock is held while
sampling. The expensive parts have their own longer intervals (a full scan
for socket owners, the robot process scan, the ROS logs, the disks, the GPU
while the arm controller is active), and every scan has a cap; a cap that
cut a scan short says ``truncated``. A probe that fails reports
``"state": "unknown"`` and never fails the others. The response holds no
command line, no environment, no token and no path.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TTL_S = 2.0
MIN_TTL_S = 1.0
CONTEXT_TTL_S = 10.0
OWNER_RESCAN_S = 30.0  # a full /proc/*/fd scan for sockets still without an owner
ROBOT_SCAN_S = 5.0
ROS_SCAN_S = 5.0
DISK_SCAN_S = 10.0
MAX_PIDS = 4096
MAX_FDS = 4096
MAX_STATUS_BYTES = 1 << 20
MAX_GPU_APPS = 16
PROBE_TIMEOUT_S = 1.5  # each probe's deadline
GPU_TIMEOUT_S = 2.5  # the GPU probe's (two nvidia-smi queries)
NVIDIA_TIMEOUT_S = 2.0  # one nvidia-smi query (it is killed after this)
WAIT_S = GPU_TIMEOUT_S + 1.0  # how long a request waits for another's sample
CONTEXT_TIMEOUT_S = 1.5
GPU_ACTIVE_S = 10.0  # the GPU interval while the arm controller is active
ROS_FILES = 12
ROS_TAIL_BYTES = 256 << 10
ROS_WINDOW_S = 3600.0
ROS_EVENTS = 6
ALIVE_S = 15.0

# Ports the wizard shows, by role. 5000/5001/5100/7470/8000 are robot-side:
# they are only looked up in the kernel's table, never connected to.
PORTS = (
    (5000, "robot_server"),
    (5001, "robot_server_2"),
    (5100, "robot_tunnel"),
    (7470, "learner"),
    (7860, "product_ui"),
    (7861, "product_core"),
    (7880, "live_viewer"),
    (7881, "live_core"),
    (7882, "online_judge"),
    (8000, "policy_server"),
    (8100, "vllm"),
)

# The real-robot quiet guard: process names and thresholds as in the lab's
# ``longrun.py guard --robot-quiet`` (the 2026-10-09 communication-constraint
# faults came with a strained host while the controller was active).
ROBOT_PROCESSES = (
    "ros2_control_node",
    "franka_server",
    "franka_server_ros2",
    "run_robotiq_client",
    "policy_server",
    "serve_policy",
)
INTERPRETERS = re.compile(
    r"^(python[0-9.]*|pypy[0-9.]*|bash|sh|dash|zsh|env|uv|ros2|node|nice|ionice|"
    r"taskset|setsid|stdbuf|timeout)$"
)
PSI_LIMITS = {"cpu": 25.0, "memory": 5.0, "io": 50.0}
SWAP_PCT = 50.0

ROS_NAME = re.compile(r"^ros2_control_node_\d+_(\d{13})\.log$")
ROS_LINE = re.compile(
    r"^\[(?P<lvl>[A-Z]+)\]\s+\[(?P<ts>\d+(?:\.\d+)?)\]\s+\[(?P<node>[^\]]*)\]:\s?(?P<msg>.*)$"
)

ENV_ROS_LOG_DIR = "LEVI_ROS_LOG_DIR"
ENV_RECORDER_DIR = "LEVI_FR3_RECORDER_DIR"
ENV_GPU_LOCK = "LEVI_GPU_LOCK_FILE"


@dataclass(frozen=True)
class Context:
    """Where to look. Built from the environment and the live configuration
    (``context_from_environment``); tests build it by hand."""

    proc: str = "/proc"
    ports: tuple = PORTS
    live_status_file: str | None = None
    gpu_lock_files: tuple = ()  # (label, path)
    health_file: str | None = None
    health_stale_s: float = 3.0
    ros_log_dir: str | None = None
    recorder_dir: str | None = None
    disks: tuple = ()  # (label, path)
    quiet_states: tuple = ()
    pressure_avg10_max: float = 0.0
    # Why the configuration could not be read (empty: it was).
    context_detail: str = ""


class NotRegular(OSError):
    """Not a regular file (a FIFO, device, socket, directory or link)."""


def read_regular(path, limit=1 << 16, tail=False):
    """``(bytes, truncated, stat)`` of a regular file, at most ``limit``
    bytes (the last ones with ``tail``). Never blocks on what is not a
    regular file: ``lstat`` decides before anything is opened, the open is
    ``O_NONBLOCK | O_NOFOLLOW`` and ``fstat`` checks it is still the same
    file. Raises NotRegular, or OSError when it cannot be read."""
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode):
        raise NotRegular(f"{path}: not a regular file")
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            raise NotRegular(f"{path}: replaced while opened")
        truncated = False
        if tail and st.st_size > limit:
            os.lseek(fd, st.st_size - limit, os.SEEK_SET)
            truncated = True
        chunks, left = [], limit
        while left > 0:
            chunk = os.read(fd, min(left, 1 << 16))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        if left <= 0 and not tail and os.read(fd, 1):
            truncated = True
        return b"".join(chunks), truncated, st
    finally:
        os.close(fd)


def _read_why(path, limit=1 << 16):
    """``(text or None, why)``; ``why`` is ``missing``, ``not a regular
    file`` or ``unreadable`` when there is no text."""
    try:
        data, _, _ = read_regular(path, limit)
    except NotRegular:
        return None, "not a regular file"
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "unreadable"
    return data.decode("utf-8", "replace"), ""


def _read(path, limit=1 << 16) -> str | None:
    return _read_why(path, limit)[0]


def _stat_fields(proc, pid) -> list | None:
    text = _read(f"{proc}/{pid}/stat", 4096)
    if not text or ")" not in text:
        return None
    return text.rsplit(")", 1)[1].split()


def _start_ticks(proc, pid) -> int | None:
    fields = _stat_fields(proc, pid)
    try:
        return int(fields[19]) if fields else None
    except (IndexError, ValueError):
        return None


def _parent(proc, pid) -> int | None:
    fields = _stat_fields(proc, pid)
    try:
        return int(fields[1]) if fields else None
    except (IndexError, ValueError):
        return None


def _ancestors(proc, pid, depth=6) -> list:
    """``pid`` and its parents, nearest first, at most ``depth``."""
    out = []
    while pid and pid > 1 and len(out) < depth:
        out.append(pid)
        pid = _parent(proc, pid)
    return out


def _comm(proc, pid) -> str | None:
    text = _read(f"{proc}/{pid}/comm", 256)
    return text.strip()[:64] if text else None


def _pids(proc) -> tuple:
    """``(pids, truncated)``: at most ``MAX_PIDS``, lowest first; a cut is
    reported, never silent."""
    try:
        names = os.listdir(proc)
    except OSError:
        return [], False
    pids = sorted(int(n) for n in names if n.isdigit())
    return pids[:MAX_PIDS], len(pids) > MAX_PIDS


# ------------------------------------------------------------------ ports


def _address(hexaddr: str) -> str | None:
    try:
        raw = bytes.fromhex(hexaddr)
    except ValueError:
        return None
    if len(raw) == 4:
        return str(ipaddress.IPv4Address(raw[::-1]))
    if len(raw) == 16:
        words = b"".join(raw[i : i + 4][::-1] for i in range(0, 16, 4))
        return str(ipaddress.IPv6Address(words))
    return None


def _scope(address: str | None) -> str:
    if address is None:
        return "unknown"
    ip = ipaddress.ip_address(address)
    mapped = getattr(ip, "ipv4_mapped", None)
    ip = mapped or ip
    if ip.is_loopback:
        return "loopback"
    if ip.is_unspecified:
        return "any"
    return "other"


def listening(proc="/proc") -> dict | None:
    """``{port: [(scope, inode), ...]}`` of LISTEN sockets, or None when no
    table can be read."""
    found: dict = {}
    readable = False
    for table in ("tcp", "tcp6"):
        text = _read(f"{proc}/net/{table}", 8 << 20)
        if text is None:
            continue
        readable = True
        for line in text.splitlines()[1:]:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A":
                continue
            host, _, port = fields[1].rpartition(":")
            try:
                number = int(port, 16)
            except ValueError:
                continue
            found.setdefault(number, []).append((_scope(_address(host)), fields[9]))
    return found if readable else None


def _socket_inodes(proc, pid) -> set | None:
    try:
        entries = os.scandir(f"{proc}/{pid}/fd")
    except OSError:
        return None
    out = set()
    with entries:
        for count, entry in enumerate(entries):
            if count >= MAX_FDS:
                break
            try:
                link = os.readlink(entry.path)
            except OSError:
                continue
            if link.startswith("socket:["):
                out.add(link[8:-1])
    return out


# ------------------------------------------------------------------ the sampler


@dataclass
class _Every:
    """A sub-probe result kept for ``seconds``."""

    seconds: float
    at: float = -1e18
    value: object = None
    key: object = None


class _Task:
    """One probe running in its own daemon thread."""

    __slots__ = ("done", "value")

    def __init__(self):
        self.done = threading.Event()
        self.value = None


STILL_BLOCKED = (
    "an earlier read has not finished (a slow or blocked file system?); "
    "not started again"
)


@dataclass
class Probes:
    runner: object = None  # None: subprocess.run, looked up at each call
    clock: object = time.monotonic
    wall: object = time.time
    ttl_s: float = DEFAULT_TTL_S
    jobs: object = None  # callable -> running product jobs (levi.activity)
    samples: int = 0
    _cond: threading.Condition = field(default_factory=threading.Condition)
    _sampling: bool = False
    _cached: tuple | None = None
    _inflight: dict = field(default_factory=dict)  # probe name -> _Task
    _cpu_prev: tuple | None = None
    _owners: dict = field(default_factory=dict)  # inode -> pid
    _owner_scan_at: float = -1e18
    _owner_unresolved: set = field(default_factory=set)
    _owner_truncated: bool = False
    _owner_proc: str = ""
    _every: dict = field(default_factory=dict)
    _ros_files: dict = field(default_factory=dict)  # path -> (key, events, truncated)
    _controller_active: bool = False
    _gpu_last: tuple | None = None  # (clock, value)

    def status(self, context: Context) -> dict:
        """The cached sample when it is younger than ``ttl_s``, else a new one.
        One request samples at a time and no lock is held while it does; a
        request arriving meanwhile waits at most ``WAIT_S`` for that sample,
        then gets the previous one (``stale``) or ``{"state": "sampling"}``."""
        ttl = max(MIN_TTL_S, float(self.ttl_s))
        give_up = time.monotonic() + WAIT_S
        with self._cond:
            while True:
                now = self.clock()
                cached = self._cached
                if cached and cached[0] == context and now - cached[1] < ttl:
                    return {
                        **cached[2],
                        "cached": True,
                        "age_s": round(now - cached[1], 3),
                    }
                if not self._sampling:
                    self._sampling = True
                    break
                left = give_up - time.monotonic()
                if left <= 0:
                    if cached:
                        return {
                            **cached[2],
                            "cached": True,
                            "stale": True,
                            "age_s": round(now - cached[1], 3),
                        }
                    return {
                        "state": "sampling",
                        "detail": "another request is sampling; ask again shortly",
                        "cached": False,
                    }
                self._cond.wait(left)
        value = None
        try:
            started = time.monotonic()
            value = self._sample(context)
            value["sample_ms"] = round((time.monotonic() - started) * 1000, 1)
        finally:
            with self._cond:
                self._sampling = False
                if value is not None:
                    self.samples += 1
                    self._cached = (context, self.clock(), value)
                self._cond.notify_all()
        return {**value, "cached": False, "age_s": 0.0}

    # -- helpers
    def _periodic(self, name, seconds, key, fn):
        slot = self._every.setdefault(name, _Every(seconds))
        now = self.clock()
        if slot.key != key or now - slot.at >= seconds:
            slot.value, slot.at, slot.key = fn(), now, key
        return slot.value

    def _safe(self, fn, *args):
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001 - one failing probe never fails the others
            return {"state": "unknown", "detail": f"probe failed: {type(exc).__name__}"}

    def _start(self, name, fn, *args) -> _Task | None:
        """Run one probe in a daemon thread; None while the same probe from
        an earlier sample is still stuck (it is not started twice)."""
        old = self._inflight.get(name)
        if old is not None and not old.done.is_set():
            return None
        task = _Task()

        def run():
            try:
                task.value = self._safe(fn, *args)
            finally:
                task.done.set()

        self._inflight[name] = task
        threading.Thread(target=run, name=f"levi-setup-{name}", daemon=True).start()
        return task

    def _sample(self, c: Context) -> dict:
        now = self.wall()
        plan = {
            "ports": (self._ports, (c,), PROBE_TIMEOUT_S),
            "gpu_locks": (self._locks, (c,), PROBE_TIMEOUT_S),
            "live": (self._live, (c, now), PROBE_TIMEOUT_S),
            "host": (self._host, (c,), PROBE_TIMEOUT_S),
            "ros": (self._ros, (c, now), PROBE_TIMEOUT_S),
            "fr3_health": (self._health, (c, now), PROBE_TIMEOUT_S),
            "disks": (self._disks, (c,), PROBE_TIMEOUT_S),
            "recorder": (self._recorder, (c, now), PROBE_TIMEOUT_S),
            "robot": (self._robot, (c,), PROBE_TIMEOUT_S),
            "jobs": (self._jobs, (), PROBE_TIMEOUT_S),
        }
        gpu_reused = None
        if (
            self._controller_active
            and self._gpu_last
            and self.clock() - self._gpu_last[0] < GPU_ACTIVE_S
        ):
            # The arm controller is active: nvidia-smi every GPU_ACTIVE_S only.
            gpu_reused = {**self._gpu_last[1], "reused": True}
        else:
            plan["gpu"] = (self._gpu, (c,), GPU_TIMEOUT_S)
        began = time.monotonic()
        tasks = {
            name: self._start(name, fn, *args) for name, (fn, args, _) in plan.items()
        }
        out = {}
        for name, task in tasks.items():
            limit = plan[name][2]
            if task is None:
                out[name] = {"state": "unknown", "detail": STILL_BLOCKED}
            elif task.done.wait(max(0.0, began + limit - time.monotonic())):
                out[name] = task.value
            else:
                out[name] = {
                    "state": "unknown",
                    "detail": f"timed out after {limit:g} s",
                }
        if gpu_reused is not None:
            out["gpu"] = gpu_reused
        elif out["gpu"].get("state") == "ok":
            self._gpu_last = (self.clock(), out["gpu"])
        out["gpu"] = self._name_gpu_processes(out["gpu"], out["ports"])
        robot = out.pop("robot")
        jobs = out.pop("jobs")
        out["guard"] = self._safe(
            self._guard, c, out["host"], out["fr3_health"], out["live"], robot, jobs
        )
        self._controller_active = bool(out["guard"].get("controller_active"))
        return {
            "sampled_at": now,
            "context": {"state": "unknown", "detail": c.context_detail}
            if c.context_detail
            else {"state": "ok"},
            **out,
        }

    # -- ports
    def _resolve_owners(self, proc, inodes) -> None:
        # Keep the owners still holding their socket; find the rest with a
        # full scan: at once for a socket not seen before, else at most every
        # OWNER_RESCAN_S (another user's sockets never resolve and must not
        # cost a scan per request).
        if proc != self._owner_proc:
            self._owners.clear()
            self._owner_unresolved = set()
            self._owner_scan_at = -1e18
            self._owner_proc = proc
        for inode in list(self._owners):
            if inode not in inodes:
                del self._owners[inode]
        checked: dict = {}
        for inode, pid in list(self._owners.items()):
            held = checked.get(pid)
            if held is None:
                held = checked[pid] = _socket_inodes(proc, pid) or set()
            if inode not in held:
                del self._owners[inode]
        missing = inodes - set(self._owners)
        self._owner_unresolved &= inodes
        now = self.clock()
        new = missing - self._owner_unresolved
        if not missing or (not new and now - self._owner_scan_at < OWNER_RESCAN_S):
            return
        self._owner_scan_at = now
        pids, self._owner_truncated = _pids(proc)
        for pid in pids:
            held = _socket_inodes(proc, pid)
            if not held:
                continue
            for inode in missing & held:
                self._owners[inode] = pid
            missing -= held
            if not missing:
                break
        self._owner_unresolved = missing

    def _ports(self, c: Context) -> dict:
        table = listening(c.proc)
        if table is None:
            return {"state": "unknown", "detail": "/proc/net/tcp cannot be read"}
        wanted = {port for port, _ in c.ports}
        inodes = {inode for port in wanted for _, inode in table.get(port, [])}
        self._resolve_owners(c.proc, inodes)
        rows = []
        for port, role in c.ports:
            sockets = table.get(port, [])
            pid = next((self._owners[i] for _, i in sockets if i in self._owners), None)
            rows.append(
                {
                    "port": port,
                    "role": role,
                    "listening": bool(sockets),
                    "scope": sorted({s for s, _ in sockets}),
                    "pid": pid,
                    "process": _comm(c.proc, pid) if pid else None,
                    "owner_known": pid is not None or not sockets,
                }
            )
        return {"state": "ok", "ports": rows, "truncated": self._owner_truncated}

    # -- GPU
    def _nvidia(self, query: str) -> list | None:
        try:
            done = (self.runner or subprocess.run)(
                ["nvidia-smi", query, "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=NVIDIA_TIMEOUT_S,
                check=True,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return [
            [p.strip() for p in line.split(",")]
            for line in (done.stdout or "").splitlines()
            if line.strip()
        ]

    def _gpu(self, c: Context) -> dict:
        rows = self._nvidia(
            "--query-gpu=index,name,memory.total,memory.used,utilization.gpu"
        )
        if rows is None:
            return {"state": "unknown", "detail": "nvidia-smi cannot be read"}

        def num(text):
            try:
                return int(float(text))
            except (TypeError, ValueError):
                return None

        gpus = [
            {
                "index": num(r[0]),
                "name": r[1][:64],
                "total_mib": num(r[2]),
                "used_mib": num(r[3]),
                "free_mib": (num(r[2]) - num(r[3]))
                if num(r[2]) is not None and num(r[3]) is not None
                else None,
                "util_pct": num(r[4]),
            }
            for r in rows
            if len(r) >= 5
        ]
        apps = []
        for r in (
            self._nvidia("--query-compute-apps=pid,process_name,used_memory") or []
        )[:MAX_GPU_APPS]:
            if len(r) < 3 or not r[0].isdigit():
                continue
            pid = int(r[0])
            apps.append(
                {
                    "pid": pid,
                    "name": os.path.basename(r[1])[:60],
                    "memory_mib": num(r[2]),
                    "_chain": _ancestors(c.proc, pid),
                }
            )
        return {"state": "ok", "gpus": gpus, "processes": apps}

    @staticmethod
    def _name_gpu_processes(gpu: dict, ports: dict) -> dict:
        """Each GPU process named by the port it or a parent listens on
        (from this sample's port table; nothing is read here)."""
        if gpu.get("state") != "ok":
            return gpu
        roles = {
            row["pid"]: row["role"]
            for row in (ports or {}).get("ports", [])
            if row.get("pid")
        }
        named = []
        for app in gpu.get("processes", []):
            chain = app.get("_chain") or [app["pid"]]
            role = next((roles[p] for p in chain if p in roles), "other")
            named.append(
                {k: v for k, v in app.items() if k != "_chain"} | {"role": role}
            )
        return {**gpu, "processes": named}

    # -- GPU locks
    def _locks(self, c: Context) -> dict:
        if not c.gpu_lock_files:
            return {
                "state": "unknown",
                "detail": "no GPU lock file configured",
                "locks": [],
            }
        table = _read(f"{c.proc}/locks", 4 << 20)
        out = []
        for label, path in c.gpu_lock_files:
            try:
                st = os.stat(path)
            except OSError:
                out.append({"label": label, "state": "absent", "holders": []})
                continue
            if table is None:
                out.append({"label": label, "state": "unknown", "holders": []})
                continue
            key = (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)
            holders = []
            for line in table.splitlines():
                fields = line.split()
                if len(fields) < 6 or fields[1] == "->":
                    continue
                try:
                    major, minor, inode = fields[5].split(":")
                    match = (int(major, 16), int(minor, 16), int(inode)) == key
                except ValueError:
                    continue
                if not match:
                    continue
                try:
                    pid = int(fields[4])
                except ValueError:
                    pid = None
                holders.append(
                    {
                        "pid": pid if pid and pid > 0 else None,
                        "process": _comm(c.proc, pid) if pid and pid > 0 else None,
                        "kind": fields[1],
                        "mode": fields[3],
                    }
                )
            out.append(
                {
                    "label": label,
                    "state": "held" if holders else "free",
                    "holders": holders[:8],
                }
            )
        return {"state": "ok", "locks": out}

    # -- live service
    def _live(self, c: Context, now: float) -> dict:
        if not c.live_status_file:
            return {"state": "unknown", "detail": "no live workspace found"}
        text, why = _read_why(c.live_status_file, MAX_STATUS_BYTES)
        if text is None:
            detail = {
                "missing": "the live service has no status file",
                "not a regular file": "the live status file is not a regular file",
            }.get(why, "the live status file cannot be read")
            return {"state": "unknown", "detail": detail}
        try:
            value = json.loads(text)
        except ValueError:
            return {
                "state": "unknown",
                "detail": "the live status file cannot be parsed",
            }
        if not isinstance(value, dict):
            return {
                "state": "unknown",
                "detail": "the live status file is not an object",
            }
        gpu = value.get("gpu") or {}
        pid = value.get("pid")
        try:
            updated = float(value.get("updated_at") or 0)
        except (TypeError, ValueError):
            updated = 0.0
        alive = bool(
            isinstance(pid, int)
            and os.path.exists(f"{c.proc}/{pid}")
            and now - updated <= ALIVE_S
            and value.get("state") != "stopped"
        )
        gate = gpu.get("gate") or {}
        decision = gpu.get("decision") or {}
        vllm = gpu.get("vllm") or {}
        return {
            "state": "ok",
            "alive": alive,
            "service_state": value.get("state"),
            "age_s": round(max(0.0, now - updated), 1) if updated else None,
            "vllm_state": gpu.get("vllm_state") or vllm.get("state"),
            "gpu_mode": gpu.get("mode"),
            "gate": {
                "open": gate.get("open"),
                "code": gate.get("code"),
                # Free text the live service wrote (at most 200 characters).
                "reason": str(gate.get("reason") or "")[:200],
            },
            "decision": {
                "allowed": decision.get("allowed"),
                "code": decision.get("code"),
            },
            "labelling_paused": value.get("labelling_paused"),
            "lock": gpu.get("lock"),
            "prewarm": gpu.get("prewarm"),
            "free_mib": gpu.get("free_mib"),
            "policy_server_seen": gpu.get("policy_server_seen"),
        }

    # -- host
    def _host(self, c: Context) -> dict:
        out: dict = {"state": "ok"}
        load = (_read(f"{c.proc}/loadavg", 256) or "").split()
        out["load"] = [float(x) for x in load[:3]] if len(load) >= 3 else None
        stat = _read(f"{c.proc}/stat", 1 << 20) or ""
        first = stat.splitlines()[0].split() if stat else []
        cpu = None
        if first and first[0] == "cpu":
            ticks = [int(x) for x in first[1:9]]
            idle = ticks[3] + (ticks[4] if len(ticks) > 4 else 0)
            total = sum(ticks)
            prev, self._cpu_prev = self._cpu_prev, (total, idle)
            if prev and total > prev[0]:
                cpu = round(100.0 * (1 - (idle - prev[1]) / (total - prev[0])), 1)
        out["cpu_busy_pct"] = cpu
        info = dict(
            re.findall(
                r"^(\w+):\s+(\d+)", _read(f"{c.proc}/meminfo") or "", re.MULTILINE
            )
        )
        if "MemTotal" in info:
            kib = {k: int(v) for k, v in info.items()}
            swap_total = kib.get("SwapTotal", 0)
            swap_used = swap_total - kib.get("SwapFree", 0)
            out["memory"] = {
                "total_gib": round(kib["MemTotal"] / 2**20, 2),
                "available_gib": round(kib.get("MemAvailable", 0) / 2**20, 2),
                "swap_total_gib": round(swap_total / 2**20, 2),
                "swap_used_mib": round(swap_used / 1024, 1),
                "swap_used_pct": round(100.0 * swap_used / swap_total, 1)
                if swap_total
                else 0.0,
            }
        else:
            out["memory"] = None
        psi = {}
        for kind in ("cpu", "memory", "io"):
            text = _read(f"{c.proc}/pressure/{kind}", 1024)
            if text is None:
                psi[kind] = None
                continue
            row = {}
            for line in text.splitlines():
                parts = line.split()
                if parts and parts[0] in ("some", "full"):
                    for item in parts[1:]:
                        k, _, v = item.partition("=")
                        if k in ("avg10", "avg60"):
                            try:
                                row[f"{parts[0]}_{k}"] = float(v)
                            except ValueError:
                                pass
            psi[kind] = row or None
        out["pressure"] = psi
        if out["load"] is None and out["memory"] is None and not any(psi.values()):
            return {"state": "unknown", "detail": "/proc cannot be read"}
        return out

    # -- ROS control logs
    def _ros_events(self, path):
        """``(events, truncated)`` of one log, re-read only when it changed;
        raises OSError (NotRegular for a FIFO and the like)."""
        st = os.lstat(path)
        key = (st.st_size, st.st_mtime_ns, st.st_ino)
        cached = self._ros_files.get(path)
        if cached and cached[0] == key:
            return cached[1], cached[2]
        data, truncated, _ = read_regular(path, ROS_TAIL_BYTES, tail=True)
        text = data.decode("utf-8", "replace")
        if truncated:
            text = text.partition("\n")[2]  # a partial first line
        events = []
        for line in text.split("\n"):
            m = ROS_LINE.match(line.rstrip("\r"))
            if not m:
                continue
            category = _ros_category(m["lvl"], m["msg"])
            if category:
                events.append((float(m["ts"]), category))
        self._ros_files[path] = (key, events, truncated)
        return events, truncated

    def _ros(self, c: Context, now: float) -> dict:
        def scan():
            folder = c.ros_log_dir
            if not folder:
                return {"state": "unknown", "detail": "no ROS log folder configured"}
            try:
                names = [n for n in os.listdir(folder) if ROS_NAME.match(n)]
            except OSError:
                return {
                    "state": "unknown",
                    "detail": "the ROS log folder cannot be read",
                }
            names.sort(key=lambda n: int(ROS_NAME.match(n)[1]), reverse=True)
            counts: dict = {}
            latest: dict = {}
            used, skipped, truncated = [], {}, False
            for name in names[:ROS_FILES]:
                path = os.path.join(folder, name)
                try:
                    if os.lstat(path).st_mtime < now - ROS_WINDOW_S:
                        continue
                    events, cut = self._ros_events(path)
                except NotRegular:
                    skipped["not a regular file"] = (
                        skipped.get("not a regular file", 0) + 1
                    )
                    continue
                except OSError:
                    skipped["unreadable"] = skipped.get("unreadable", 0) + 1
                    continue
                used.append(path)
                truncated = truncated or cut
                for t, category in events:
                    if t < now - ROS_WINDOW_S:
                        continue
                    counts[category] = counts.get(category, 0) + 1
                    latest[category] = max(latest.get(category, 0.0), t)
            for path in list(self._ros_files):
                if path not in used:
                    del self._ros_files[path]
            if skipped and not used:
                return {
                    "state": "unknown",
                    "detail": "no ROS log could be read: "
                    + ", ".join(f"{n} {why}" for why, n in skipped.items()),
                }
            return {
                "state": "ok",
                "window_s": ROS_WINDOW_S,
                "files": len(used),
                "skipped": sum(skipped.values()),
                # Only the last ROS_TAIL_BYTES of a file are read: counts of a
                # log that grew faster than that in the window are a floor.
                "truncated": truncated,
                "counts": counts,
                "latest": {k: round(v, 3) for k, v in latest.items()},
            }

        return self._periodic("ros", ROS_SCAN_S, c.ros_log_dir, scan)

    # -- FR3 health file
    def _health(self, c: Context, now: float) -> dict:
        """The health file summarised as ``levi.live.sessions.read_fr3``
        does (ok, red, offline, missing), read with ``read_regular``."""
        if not c.health_file:
            return {
                "state": "unknown",
                "detail": "no health file configured (fr3.health_file)",
            }
        from levi.live.sessions import parse_time

        try:
            data, _, st = read_regular(c.health_file, MAX_STATUS_BYTES)
        except NotRegular:
            return {
                "state": "unknown",
                "detail": "the health file is not a regular file",
            }
        except FileNotFoundError:
            return {
                "state": "missing",
                "detail": "no health file: the monitor is not running",
            }
        except OSError:
            return {"state": "offline", "detail": "health file unreadable"}
        try:
            value = json.loads(data.decode("utf-8", "replace"))
        except ValueError:
            value = None
        if not isinstance(value, dict):
            return {"state": "offline", "detail": "health file unreadable"}
        updated = (
            parse_time(value.get("updated_at_epoch"))
            or parse_time(value.get("updated_at"))
            or st.st_mtime
        )
        age = max(0.0, now - updated)
        summary = {
            "age_s": round(age, 1),
            "robot_mode": value.get("robot_mode"),
            "robot_mode_name": value.get("robot_mode_name"),
            "current_errors": list(value.get("current_errors") or [])[:10],
            "last_motion_errors": list(value.get("last_motion_errors") or [])[:10],
            "hardware_active": value.get("hardware_active"),
            "controller_active": value.get("controller_active"),
            "command_success_rate": value.get("command_success_rate"),
            "reasons": [str(r)[:200] for r in (value.get("reasons") or [])[:10]],
        }
        if age > c.health_stale_s:
            return {
                "state": "offline",
                "detail": "the monitor stopped updating",
                **summary,
            }
        return {"state": "red" if value.get("red_light") else "ok", **summary}

    # -- disks
    def _disks(self, c: Context) -> dict:
        def scan():
            rows = []
            for label, path in c.disks[:8]:
                try:
                    usage = shutil.disk_usage(path)
                except OSError:
                    rows.append({"label": label, "state": "unknown"})
                    continue
                rows.append(
                    {
                        "label": label,
                        "state": "ok",
                        "free_gib": round(usage.free / 2**30, 1),
                        "total_gib": round(usage.total / 2**30, 1),
                        "used_pct": round(100.0 * usage.used / usage.total, 1)
                        if usage.total
                        else None,
                    }
                )
            return {"state": "ok" if rows else "unknown", "disks": rows}

        return self._periodic("disks", DISK_SCAN_S, c.disks, scan)

    # -- diagnostics recorder
    def _recorder(self, c: Context, now: float) -> dict:
        if not c.recorder_dir:
            return {"state": "unknown", "detail": "no recorder folder configured"}
        base = Path(c.recorder_dir).expanduser()
        if not os.path.lexists(base / "recorder.pid") and not os.path.lexists(
            base / "status.json"
        ):
            base = base / "data"
        running, pid = False, None
        text, why = _read_why(base / "recorder.pid", 4096)
        if why == "not a regular file":
            return {"state": "unknown", "detail": "recorder.pid is not a regular file"}
        if text:
            try:
                record = json.loads(text)
                pid = int(record["pid"])
                running = record.get("start") is not None and _start_ticks(
                    c.proc, pid
                ) == record.get("start")
            except (ValueError, KeyError, TypeError):
                pid = None
        status = {}
        text, why = _read_why(base / "status.json", 1 << 16)
        if why == "not a regular file":
            return {"state": "unknown", "detail": "status.json is not a regular file"}
        if text:
            try:
                status = json.loads(text)
            except ValueError:
                status = {}
        if not isinstance(status, dict):
            status = {}
        if pid is None and not status:
            return {"state": "unknown", "detail": "no recorder files"}
        updated = status.get("updated")
        return {
            "state": "running" if running else "stopped",
            "pid": pid if running else None,
            "age_s": round(max(0.0, now - float(updated)), 1)
            if isinstance(updated, (int, float))
            else None,
            **{
                k: status.get(k)
                for k in (
                    "snapshots",
                    "last_trigger",
                    "disk_ok",
                    "errors",
                    "self_cpu_avg_pct",
                    "vllm",
                )
                if k in status
            },
        }

    # -- the real-robot quiet guard
    def _robot_processes(self, proc) -> dict:
        """``{"found": [...] or None, "truncated": bool}``; None means it
        cannot be told (``hidepid``, unreadable processes, or the scan cap
        cut the list and nothing was found in what was read)."""
        mount = _read(f"{proc}/self/mountinfo", 1 << 20)
        for line in (mount or "").splitlines():
            parts = line.split()
            hidden = re.search(r"hidepid=(\w+)", line)
            if (
                len(parts) > 4
                and parts[4] == "/proc"
                and hidden
                and hidden[1] not in ("0", "off")
            ):
                return {"found": None, "truncated": False}
        pids, truncated = _pids(proc)
        if not pids:
            return {"found": None, "truncated": truncated}
        mine = set(_ancestors(proc, os.getpid(), 64)) if proc == "/proc" else set()
        found, unreadable = [], 0
        for pid in pids:
            if pid in mine:
                continue
            try:
                raw, _, _ = read_regular(f"{proc}/{pid}/cmdline", 4096)
            except (FileNotFoundError, ProcessLookupError):
                continue
            except OSError:
                unreadable += 1
                continue
            argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
            for name in command_names(argv):
                if any(pattern in name for pattern in ROBOT_PROCESSES):
                    found.append({"pid": pid, "name": name[:64]})
                    break
        if (unreadable or truncated) and not found:
            return {"found": None, "truncated": truncated}
        return {"found": found[:12], "truncated": truncated}

    def _robot(self, c: Context) -> dict:
        return self._periodic(
            "robot", ROBOT_SCAN_S, c.proc, lambda: self._robot_processes(c.proc)
        )

    def _jobs(self) -> dict:
        if self.jobs is None:
            return {"state": "unknown", "jobs": []}
        return {
            "state": "ok",
            "jobs": [
                {"kind": j.get("kind"), "id": j.get("id")} for j in (self.jobs() or [])
            ][:12],
        }

    def _guard(
        self, c: Context, host: dict, health: dict, live: dict, robot: dict, jobs: dict
    ) -> dict:
        robot = (
            robot
            if isinstance(robot, dict) and "found" in robot
            else {"found": None, "truncated": False}
        )
        found = robot["found"]
        strained, readings = [], {}
        pressure = (host or {}).get("pressure") or {}
        for kind, limit in PSI_LIMITS.items():
            value = (pressure.get(kind) or {}).get("some_avg10")
            readings[kind] = value
            if value is not None and value > limit:
                strained.append(f"{kind} pressure some avg10={value:g} > {limit:g}")
        memory = (host or {}).get("memory") or {}
        swap_pct = memory.get("swap_used_pct")
        readings["swap_pct"] = swap_pct
        if swap_pct is not None and swap_pct > SWAP_PCT:
            strained.append(f"swap {swap_pct:.0f}% used > {SWAP_PCT:g}%")
        unknown = found is None or all(readings.get(k) is None for k in PSI_LIMITS)
        # A robot process or a strained host is a definite "not quiet" (the
        # lab guard would say "cannot tell" when /proc is only partly
        # readable even then); anything unreadable otherwise is unknown.
        if found or strained:
            quiet = False
        else:
            quiet = None if unknown else True
        running = list((jobs or {}).get("jobs") or [])
        gate = (live or {}).get("gate") or {}
        controller = (health or {}).get("controller_active")
        if (health or {}).get("state") not in ("ok", "red"):
            controller = None  # a stale or missing file says nothing about now
        reasons = []
        if controller:
            if running:
                reasons.append(
                    "product jobs run while the arm controller is active: "
                    + ", ".join(str(j["kind"]) for j in running)
                )
            if memory.get("swap_used_mib"):
                reasons.append(
                    f"swap in use ({memory['swap_used_mib']:.0f} MiB) while the arm controller is active"
                )
            reasons += [
                f"{text} while the arm controller is active" for text in strained
            ]
        return {
            "state": "ok",
            "quiet": quiet,
            "robot": found or [],
            "truncated": bool(robot.get("truncated")),
            "strained": strained,
            "readings": readings,
            "controller_active": controller,
            "product_jobs": running,
            "live_gate": {
                "code": gate.get("code"),
                "robot_quiet": gate.get("code") == "robot_quiet",
            },
            "quiet_states": list(c.quiet_states),
            "pressure_avg10_max": c.pressure_avg10_max,
            "banner": {"level": "warn", "reasons": reasons} if reasons else None,
        }


def _ros_category(level: str, msg: str) -> str | None:
    """The FR3 diagnostics recorder's categories for one ROS log line."""
    if "communication_constraints_violation" in msg:
        return "comm_violation"
    if "aborted by reflex" in msg:
        if "cartesian_reflex" in msg:
            return "cartesian_reflex"
        if "motion_generator" in msg:
            return "motion_generator"
        return "reflex_other"
    if "Overrun detected" in msg:
        return "overrun"
    if "Overrun might occur" in msg:
        return "overrun_warn"
    if level in ("ERROR", "FATAL"):
        return "error"
    return None


def command_names(argv) -> list:
    """Executable, script and module names of one command line: an
    interpreter's script or ``-m`` module counts, an ordinary program's
    arguments do not (``grep franka_server`` is not the robot). The command
    line itself is never kept or returned."""
    names, i = [], 0
    while i < len(argv):
        base = os.path.basename(argv[i])
        names.append(re.sub(r"\.(py|sh)$", "", base))
        if not INTERPRETERS.match(base):
            break
        i += 1
        while i < len(argv) and argv[i].startswith("-"):
            if argv[i] == "-m" and i + 1 < len(argv):
                names.append(argv[i + 1].rsplit(".", 1)[-1])
                return names
            if argv[i] in ("-c", "-e"):
                return names
            i += 1
            if (
                base in ("nice", "ionice", "taskset", "timeout")
                and i < len(argv)
                and not argv[i].startswith("-")
            ):
                i += 1
        if base == "timeout" and i < len(argv) and not argv[i].startswith("-"):
            i += 1  # timeout DURATION command
        if i < len(argv) and base in ("uv", "ros2") and argv[i] in ("run", "launch"):
            i += 1
            while i < len(argv) and argv[i].startswith("-"):
                i += 1
            if base == "ros2" and i + 1 < len(argv):
                i += 1
    return names


# ------------------------------------------------------------------ the service's view


_CONTEXT: list = [None, -1e18, None]  # context, when built, the build in flight
_CONTEXT_LOCK = threading.Lock()
PROBES: Probes | None = None
_PROBES_LOCK = threading.Lock()


def context_from_environment() -> Context:
    """Where this LEVI looks: the live workspace ``locate.find`` names (never
    remembered anywhere: this route writes nothing), its configuration in
    force, ``LEVI_GPU_LOCK_FILE``, ``LEVI_ROS_LOG_DIR`` (else ``ROS_LOG_DIR``,
    else ``~/.ros/log``) and ``LEVI_FR3_RECORDER_DIR``. Kept for
    ``CONTEXT_TTL_S``. Built in a daemon thread with a deadline: a
    configuration on a stuck file system, or one that cannot be read, gives
    the previous context or a minimal one that says why (``context``)."""
    with _CONTEXT_LOCK:
        context, built, task = _CONTEXT
        if context is not None and time.monotonic() - built < CONTEXT_TTL_S:
            return context
        if task is None:
            task = _Task()

            def run():
                try:
                    task.value = _guarded_build()
                finally:
                    task.done.set()

            _CONTEXT[2] = task
            threading.Thread(target=run, name="levi-setup-context", daemon=True).start()
    if task.done.wait(CONTEXT_TIMEOUT_S):
        with _CONTEXT_LOCK:
            if _CONTEXT[2] is task:
                _CONTEXT[:] = [task.value, time.monotonic(), None]
        return task.value
    return context or _minimal_context(
        f"the configuration could not be read within {CONTEXT_TIMEOUT_S:g} s"
    )


def _minimal_context(detail: str) -> Context:
    return Context(disks=(("tmp", _tmp_dir()),), context_detail=detail)


def _tmp_dir() -> str:
    # Not tempfile.gettempdir(): its first call creates and deletes a file.
    return os.environ.get("TMPDIR") or tempfile.tempdir or "/tmp"


def _guarded_build() -> Context:
    try:
        return _build_context()
    except Exception as exc:  # noqa: BLE001 - a bad configuration must not fail the route
        return _minimal_context(
            f"the configuration could not be read ({type(exc).__name__})"
        )


def _build_context() -> Context:
    from levi.live import config as live_config
    from levi.live import locate
    from levi.paths import ROOT

    locks, disks = [], [("product_workspace", str(ROOT))]
    lock = os.environ.get(ENV_GPU_LOCK, "").strip()
    if lock:
        locks.append(("levi", lock))
    fields: dict = {}
    ports = dict(PORTS)
    found = locate.find(ROOT)
    if found.workspace is not None and locate.is_live(found.workspace):
        root = found.workspace
        written = next(
            (
                p
                for p in (root / "live" / "effective.toml", root / "live.toml")
                if p.is_file()
            ),
            None,
        )
        try:
            config = live_config.load(written)
        except ValueError:
            config = live_config.Config()
        config.service.workspace = str(root)
        home = os.environ.get(locate.ENV_HOME)
        if home:
            config.service.home = home
        fields["live_status_file"] = str(config.status_file)
        if config.gpu.lock_file and config.gpu.lock_file != lock:
            locks.append(("live", str(Path(config.gpu.lock_file).expanduser())))
        if config.fr3.health_file:
            fields["health_file"] = str(Path(config.fr3.health_file).expanduser())
            fields["health_stale_s"] = float(config.fr3.stale_s)
        fields["quiet_states"] = tuple(config.gpu.quiet_states)
        fields["pressure_avg10_max"] = float(config.online.pressure_avg10_max)
        disks.append(("live_workspace", str(root)))
        for n, rollout in enumerate(config.watch.roots[:4], start=1):
            disks.append((f"rollout_root_{n}", str(Path(rollout).expanduser())))
        for port in config.gpu.policy_ports:
            ports.setdefault(int(port), "policy_server")
        ports.setdefault(int(config.vllm.port), "vllm")
        ports.setdefault(int(config.online.port), "online_judge")
    disks.append(("tmp", _tmp_dir()))
    ros = (
        os.environ.get(ENV_ROS_LOG_DIR, "").strip()
        or os.environ.get("ROS_LOG_DIR", "").strip()
        or str(Path("~/.ros/log").expanduser())
    )
    recorder = os.environ.get(ENV_RECORDER_DIR, "").strip() or None
    return Context(
        ports=tuple(sorted(ports.items())),
        gpu_lock_files=tuple(locks),
        ros_log_dir=ros,
        recorder_dir=recorder,
        disks=tuple(disks),
        **fields,
    )


def status() -> dict:
    """``GET /api/levi/setup/status``: one (possibly cached) sample."""
    global PROBES
    with _PROBES_LOCK:
        if PROBES is None:
            from levi.activity import running_jobs

            PROBES = Probes(jobs=running_jobs)
        probes = PROBES
    return probes.status(context_from_environment())
