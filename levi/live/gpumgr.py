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
import os
import re
import signal
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import jsonio

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


def need_mib(config, profile, total_mib) -> int:
    """Free VRAM a start or a wake of a vLLM with this ``profile`` needs: its
    budget of vLLM's total plus the configured margin."""
    budget = profile["gpu_memory_utilization"] * (total_mib - VLLM_TOTAL_SLACK_MIB)
    return int(budget) + config.vllm.margin_mib


@dataclass
class Budget:
    ok: bool
    utilization: float = 0.0
    max_model_len: int = 0
    need_mib: int = 0
    code: str = "ok"  # ok | insufficient_vram
    reason: str = ""


def plan_budget(config, *, free_mib, total_mib, policy_up: bool) -> Budget:
    """The memory budget and context length to start vLLM with *now*.

    The budget is the share of vLLM's own total (nvidia-smi's less
    ``VLLM_TOTAL_SLACK_MIB``) that the free VRAM allows after ``margin_mib``,
    capped at ``gpu_memory_utilization_max``; with no policy server on the
    card it is the configured ``gpu_memory_utilization`` (room for one to start
    later). The same number is what the start passes to vLLM, so the pre-check
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
    code: str  # open | policy_inferring | unknown_client
    reason: str


def gate(
    config, mode, sessions, policy_up: bool, policy_since: float | None = None
) -> Gate:
    """May the model *work* (send requests) right now?

    Timeshare closes the gate while the policy infers: a session in a
    ``gpu.busy_states`` state (``running``), or -- with no session file at all
    to say otherwise -- a listening policy server, which may be called by a
    client this service knows nothing about. Homing, waiting for the reset,
    standby, fault, stopped, finished and a crashed client leave the GPU to the
    model. Coexist and manual never close it."""
    if mode != "timeshare":
        return Gate(True, "open", f"gpu.mode is {mode}")
    busy = [
        s
        for s in sessions.values()
        if s.state in config.gpu.busy_states and not s.crashed
    ]
    if busy:
        s = busy[0]
        return Gate(
            False,
            "policy_inferring",
            f"{s.group}/{s.task_folder} is {s.state}: the policy is inferring",
        )
    if policy_up and not _witnesses(sessions, policy_since):
        return Gate(
            False,
            "unknown_client",
            "a policy server is listening and no current evaluation session says "
            "it is idle",
        )
    return Gate(True, "open", "the policy is not inferring")


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
            f"the policy server holds {policy_mib} MiB (over {budget})",
        )
    if free_mib is not None and free_mib < config.gpu.min_free_mib:
        return "vram", f"free VRAM fell to {free_mib} MiB"
    return None


def policy_vram_mib(
    ports, *, tables=("/proc/net/tcp", "/proc/net/tcp6"), runner=subprocess.run
):
    """VRAM (MiB) held by the process that listens on one of ``ports``, from
    the kernel's socket table, /proc/<pid>/fd and ``nvidia-smi``; None when
    it cannot be told. Read-only: nothing connects to the port."""
    inodes = set()
    for table in tables:
        try:
            lines = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A":
                continue
            try:
                port = int(fields[1].rsplit(":", 1)[1], 16)
            except (IndexError, ValueError):
                continue
            if port in ports:
                inodes.add(fields[9])
    if not inodes:
        return None
    holders = {h["pid"]: h["memory_mib"] for h in gpu_holders(runner)}
    if not holders:
        return None
    total, seen = 0, False
    for pid, mib in holders.items():
        if mib is None:
            continue
        if _serves(pid, inodes):
            total += mib
            seen = True
    return total if seen else None


def _serves(pid, inodes, depth=6) -> bool:
    """True when ``pid`` or an ancestor holds a listening socket in ``inodes``."""
    for _ in range(depth):
        if not pid or pid <= 1:
            return False
        try:
            for fd in os.scandir(f"/proc/{pid}/fd"):
                try:
                    link = os.readlink(fd.path)
                except OSError:
                    continue
                if link.startswith("socket:[") and link[8:-1] in inodes:
                    return True
        except OSError:
            pass
        try:
            pid = int(
                Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1]
            )
        except (OSError, ValueError, IndexError):
            return False
    return False


# --- the shared GPU lock -----------------------------------------------------------


class GpuLock:
    """The workspace's advisory GPU lock (``levi-hub/.gpu.lock``), held for as
    long as this service's vLLM holds the GPU. Other users take it with
    ``flock``; this is the same lock."""

    def __init__(self, path, agent="live"):
        self.path = path
        self.agent = agent
        self._handle = None

    def acquire(self) -> bool:
        if not self.path:
            return True
        if self._handle:
            return True
        try:
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            handle = open(Path(self.path).expanduser(), "a")  # noqa: SIM115
        except OSError:
            return True  # no usable lock file: the convention cannot apply
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self):
        if self._handle:
            # Close, do not LOCK_UN: an explicit unlock would release the
            # lock for every process sharing the descriptor, vLLM included.
            self._handle.close()
            self._handle = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def fileno(self):
        """The descriptor of the held lock (None when not held): a vLLM
        started with it open (``pass_fds``) keeps the lock for as long as it
        lives, even if this process dies."""
        return self._handle.fileno() if self._handle else None


# --- vLLM -----------------------------------------------------------------------------


def identity(pid):
    """(start ticks, boot id) of a process, None once it is gone or a zombie."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if stat[0] == "Z":
            return None
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return {"start_ticks": stat[19], "boot": boot}
    except (OSError, IndexError):
        return None


def healthy(port, opener=urllib.request.urlopen) -> bool:
    """Whether a vLLM server answers /health on the loopback port."""
    try:
        with opener(f"http://127.0.0.1:{port}/health", timeout=HEALTH_TIMEOUT_S) as r:
            return r.status == 200
    except (OSError, ValueError):
        return False


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
        self.error = ""
        self._adopt()

    # --- bookkeeping ---------------------------------------------------------

    @property
    def port(self) -> int:
        return self.config.vllm.port

    def _pidfile(self) -> Path:
        return Path(self.config.vllm.pid_dir).expanduser() / f"vllm_{self.port}.pid"

    def _pid(self):
        try:
            return int(self._pidfile().read_text().strip())
        except (OSError, ValueError):
            return None

    def _adopt(self):
        """After a supervisor restart: take back a vLLM this service started."""
        record = jsonio.read(self.record_path)
        if not isinstance(record, dict):
            return
        pid = record.get("pid")
        if pid and identity(pid) == record.get("identity") and self._pid() == pid:
            if not healthy(self.port):
                self.state = "starting"
            else:
                self.state = "asleep" if is_sleeping(self.port) else "ready"
            self.started_at = record.get("started_at")
            self.profile = record.get("profile")
        else:
            with contextlib.suppress(OSError):
                self.record_path.unlink()

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
        return bool(
            isinstance(record, dict)
            and record.get("pid")
            and identity(record["pid"]) == record.get("identity")
        )

    def external(self) -> bool:
        """A vLLM answers on the port but was not started by this service."""
        return not self.mine() and healthy(self.port)

    # --- lifecycle -------------------------------------------------------------

    def start(self, profile, fd=None) -> bool:
        """Launch vLLM with ``profile``; False (with ``error``) if it could not."""
        if self.state in ("starting", "ready") and self.mine():
            return True
        c = self.config.vllm
        env = dict(os.environ)
        env.update(
            PORT=str(self.port),
            GPU_UTIL=str(profile["gpu_memory_utilization"]),
            VLLM_WAIT_S="0",
            LEVI_AGENT=self.config.gpu.lock_agent,
        )
        if profile.get("sleep_mode"):
            # Dev endpoints (/sleep, /wake_up, /is_sleeping), bound to the
            # loopback interface only.
            env["VLLM_SERVER_DEV_MODE"] = "1"
        self.config.logs_dir.mkdir(parents=True, exist_ok=True)
        log = self.config.logs_dir / "vllm-launch.log"
        try:
            with log.open("ab") as sink:
                process = self.popen(
                    [
                        str(Path(c.script).expanduser()),
                        *launch_args(self.config, profile),
                    ],
                    env=env,
                    stdout=sink,
                    stderr=sink,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                    pass_fds=(fd,) if fd is not None else (),
                )
                code = process.wait(timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            self.state, self.error = (
                "error",
                f"vLLM launch failed: {type(exc).__name__}",
            )
            return False
        pid = self._pid()
        if code != 0 or not pid or identity(pid) is None:
            self.state, self.error = (
                "error",
                f"serve script exited {code}; see vllm-launch.log",
            )
            self.error = self.failure_reason()
            return False
        self.started_at = time.time()
        self.profile = profile
        self.state, self.error = "starting", ""
        jsonio.write(
            self.record_path,
            {
                "pid": pid,
                "identity": identity(pid),
                "started_at": self.started_at,
                "profile": profile,
                "port": self.port,
            },
        )
        return True

    def log_path(self) -> Path:
        return Path(self.config.vllm.pid_dir).expanduser() / f"vllm_{self.port}.log"

    def failure_reason(self) -> str:
        """Why the last start failed: the last error line of vLLM's own log
        (``ValueError: ... KV cache ...``), else what the service knows."""
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
            return found[-1].strip()[:300]
        return (self.error or "vLLM failed to start")[:300]

    def poll(self) -> str:
        """Advance ``starting`` to ``ready`` (or ``error`` on timeout/death)."""
        if self.state == "asleep":
            if not self.mine():
                self.state, self.error = "error", "vLLM exited while asleep"
            return self.state
        if self.state == "starting":
            if healthy(self.port):
                self.state = "ready"
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
        elif (
            self.state == "ready"
            and self.mine()
            and not healthy(self.port)
            and identity(self._pid() or 0) is None
        ):
            # Died after it was ready.
            self.state, self.error = "error", "vLLM exited"
            with contextlib.suppress(OSError):
                self.record_path.unlink()
        return self.state

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

    def wake(self) -> bool:
        """Wake a sleeping server (under a second). The caller has checked
        that the free VRAM covers it."""
        if not self.mine() or self.state != "asleep":
            return self.state == "ready"
        if (
            _post(self.port, "/wake_up", timeout=60.0)
            and is_sleeping(self.port) is False
        ):
            self.state, self.error = "ready", ""
            return True
        self.error = "vLLM did not wake up"
        return False

    def stop(self) -> bool:
        """Stop this service's vLLM (only if it is ours). True when gone."""
        if not self.mine():
            self.state = "stopped"
            return True
        self.state = "stopping"
        record = jsonio.read(self.record_path) or {}
        pid = record.get("pid")
        try:
            self.runner(
                [
                    str(Path(self.config.vllm.stop_script).expanduser()),
                    "--stop",
                    str(self.port),
                ],
                capture_output=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        if pid and identity(pid) == record.get("identity"):
            # The script could not (missing pidfile): signal the group directly.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, signal.SIGTERM)
            deadline = time.time() + 30
            while identity(pid) == record.get("identity") and time.time() < deadline:
                time.sleep(0.5)
            if identity(pid) == record.get("identity"):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(pid, signal.SIGKILL)
        gone = not pid or identity(pid) != record.get("identity")
        if gone:
            with contextlib.suppress(OSError):
                self.record_path.unlink()
            self.state = "stopped"
            self.profile = None
        else:
            self.state, self.error = "error", "vLLM did not stop"
        return gone

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
