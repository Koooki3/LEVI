"""Terminal D's own VLM service of a subtask_loop run, and the numbered start
and shutdown around it (user 2026-10-17: terminals A, B, C, M and terminal D
only; D starts, supervises and stops the policy service and the local VLM).

**Wired and tested on fakes only (systemd, /proc, nvidia-smi, the lock): not
verified with a real vLLM, a real policy service or the real GPU.**

The steps, each printed as ``n/7 what happens / what to do / how to stop``
and kept (``records``) for the run's journal (``subtask_service_step`` notes)
and the unit file:

1. preflight: the GPU lock (LEVI's real ``.gpu.lock`` flock, held to the end),
   free GPU memory for the policy plus the VLM (the numbers are printed),
   MemAvailable, the policy and VLM ports free (only port numbers from the
   kernel's socket tables; a port held by anything that is not our recorded
   unit: refused, nothing is stopped, the operator is told to stop the live
   service, ours by design but the operator's step);
2. start the VLM as our unit (``systemd-run --user``), wait until it is
   active, listens from inside its own cgroup and answers ``/health``; its
   identity (unit, pid, starttime) is recorded before anything else;
3. put the VLM to sleep (MemAvailable first: level 1 pins the weights in host
   memory);
4. start the policy service (fr3_run's forward-only ``policy_swap``; this
   module only prints the step and its GPU use);
5. the inventory and its confirmation (the orchestrator, terminal D's ``y``);
6. the rounds;
7. shutdown: stop the VLM (only after its identity still matches; a mismatch
   stops nothing and the operator decides), stop the policy (fr3_run), then the
   release check: our pids gone from nvidia-smi and the memory back; the lock
   is released only after that check, and a failed check is reported.

The inventory comes after the policy start (step 5), not before it: the
inventory needs the cameras, and the bridge opens the robot environment only
after the policy service is up and checked ("no arm I/O before the policy"),
an ordering this change does not touch. The peak is the same (the VLM is
asleep while the policy loads).

A failure at any step stops only the units this run started, prints what was
stopped, and raises ``ServiceFailed``. Nothing is ever stopped by name pattern
or by port, and no other user's ``/proc`` is read.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from levi.domain import aeri

STEPS = 7
VLM_UNIT = re.compile(r"levi-aeri-vlm-[a-z0-9][a-z0-9-]{0,46}")
UNIT_FILE = "subtask-vlm-unit.json"
# Released when it is back within this of the use before our VLM started.
RELEASE_TOLERANCE_MIB = 512


class UnitRefused(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class ServiceFailed(RuntimeError):
    """A start step failed; ``stopped`` names the units of ours stopped."""

    def __init__(self, step: int, code: str, detail: str, stopped=()):
        super().__init__(f"step {step}/{STEPS} {code}: {detail}")
        self.step, self.code, self.detail = step, code, detail
        self.stopped = list(stopped)


@dataclass(frozen=True)
class Identity:
    unit: str
    pid: int
    starttime: int


def _starttime(proc, pid) -> int | None:
    """Field 22 of our own pid's ``stat`` (``ProcView.path``: never listed)."""
    try:
        text = proc.path(pid, "stat").read_text()
        return int(text.rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


class SystemdUnit:
    """One service we start ourselves. ``systemd`` runs ``systemctl --user`` /
    ``systemd-run --user``; ``proc`` is ``campaign.switch.ProcView`` (the
    kernel's socket tables and our unit's own cgroup and pids only);
    ``health(port)`` asks our server on its loopback port."""

    def __init__(
        self,
        unit: str,
        argv: list,
        port: int,
        *,
        systemd,
        proc,
        health,
        env: dict | None = None,
        clock=time.monotonic,
        sleep=time.sleep,
        poll_s: float = 0.5,
    ):
        if not VLM_UNIT.fullmatch(unit):
            raise ValueError(f"unit name {unit!r} is not a levi-aeri-vlm-* unit")
        if not argv or any(
            not isinstance(a, str) or re.search(r"[\s$%]", a) for a in argv
        ):
            raise ValueError("the unit's argv has an empty or unsafe argument")
        if port in aeri.ROBOT_PORTS:
            raise ValueError("the VLM port is a robot or policy port")
        self.unit, self.argv, self.port = unit, list(argv), port
        self.systemd, self.proc, self.health = systemd, proc, health
        self.env = dict(env or {})
        self.clock, self.sleep, self.poll_s = clock, sleep, poll_s

    def state(self) -> str:
        code, out = self.systemd.run(
            [
                "systemctl",
                "--user",
                "show",
                "-p",
                "ActiveState",
                "--value",
                f"{self.unit}.service",
            ]
        )
        return out.strip() if code == 0 and out.strip() else "unknown"

    def pids(self) -> list:
        try:
            return self.proc.unit_pids(self.unit)
        except OSError:
            return []

    def foreign_listeners(self) -> list:
        return [
            lsn for lsn in self.proc.listeners(self.port, (self.unit,)) if not lsn.pids
        ]

    def identity(self) -> Identity | None:
        pids = self.pids()
        if not pids:
            return None
        pid = pids[0]
        start = _starttime(self.proc, pid)
        return None if start is None else Identity(self.unit, pid, start)

    def start(self) -> None:
        if self.proc.listeners(self.port, (self.unit,)):
            raise UnitRefused("port_held", f"port {self.port} is held")
        command = [
            "systemd-run",
            "--user",
            f"--unit={self.unit}",
            "--collect",
            "--property=KillMode=control-group",
            *(f"--setenv={k}={v}" for k, v in sorted(self.env.items())),
            "--",
            *self.argv,
        ]
        code, _ = self.systemd.run(command)
        if code != 0:
            raise UnitRefused("start_failed", f"systemd-run exit {code}")

    def wait_ready(self, timeout_s: float) -> Identity:
        deadline = self.clock() + timeout_s
        why = "not started"
        while True:
            state = self.state()
            held = self.proc.listeners(self.port, (self.unit,))
            if any(not lsn.pids for lsn in held):
                raise UnitRefused("foreign_listener", f"port {self.port}")
            if state in ("failed", "inactive") and not self.pids():
                raise UnitRefused("exited", f"unit {state}")
            if state == "active" and held:
                found = self.identity()
                if found is not None and self.health(self.port) is True:
                    return found
                why = "not healthy yet"
            else:
                why = f"unit {state}, listening: {bool(held)}"
            if self.clock() >= deadline:
                raise UnitRefused("not_ready", why)
            self.sleep(self.poll_s)

    def matches(self, expected: Identity) -> bool:
        if expected is None or expected.unit != self.unit:
            return False
        if expected.pid not in self.pids():
            return False
        return _starttime(self.proc, expected.pid) == expected.starttime

    def stop(self, expected: Identity, timeout_s: float = 60.0) -> None:
        """Stop the unit only when it is still the one we started."""
        if not self.matches(expected):
            raise UnitRefused(
                "identity_mismatch", f"{self.unit} is not the recorded one"
            )
        code, _ = self.systemd.run(
            ["systemctl", "--user", "stop", f"{self.unit}.service"]
        )
        if code != 0:
            raise UnitRefused("stop_failed", f"systemctl stop exit {code}")
        deadline = self.clock() + timeout_s
        while self.pids() or self.proc.listeners(self.port, (self.unit,)):
            if self.clock() >= deadline:
                raise UnitRefused("still_running", self.unit)
            self.sleep(self.poll_s)


class Services:
    """The numbered start and the shutdown of terminal D (module docstring).
    Everything is injected: ``vlm`` (``SystemdUnit``), ``power``
    (``subtask_vlm.PortVlmPower``), ``lock`` (``acquire``/``release``/``held``,
    ``gpumgr.GpuLock``), ``gpu()`` -> ``{total_mib, used_mib, free_mib}``,
    ``apps()`` -> nvidia-smi compute rows (``pid``), ``mem()`` -> GiB,
    ``listeners(port)`` -> the kernel's listeners on a port, ``say`` and ``t``
    (console texts), ``record_dir`` (where the unit identity is kept)."""

    def __init__(
        self,
        *,
        vlm,
        power,
        lock,
        gpu,
        apps,
        mem,
        listeners,
        say,
        t,
        record_dir,
        policy_port: int,
        need_policy_mib: int,
        need_vlm_mib: int,
        min_mem_gib: float,
        ready_timeout_s: float = 1800.0,
        clock=time.monotonic,
        sleep=time.sleep,
        poll_s: float = 0.5,
        release_timeout_s: float = 60.0,
        log_path=None,
    ):
        self.log_path = None if log_path is None else Path(log_path)
        self.vlm, self.power, self.lock = vlm, power, lock
        self.gpu, self.apps, self.mem, self.listeners = gpu, apps, mem, listeners
        self.say, self.t = say, t
        self.record_dir = Path(record_dir)
        self.policy_port = policy_port
        self.need_policy_mib, self.need_vlm_mib = need_policy_mib, need_vlm_mib
        self.min_mem_gib = min_mem_gib
        self.ready_timeout_s = ready_timeout_s
        self.clock, self.sleep, self.poll_s = clock, sleep, poll_s
        self.release_timeout_s = release_timeout_s
        self.records: list = []
        self.started: Identity | None = None
        self.baseline_used: int | None = None
        self.our_pids: set = set()
        self.done = False

    # --- the screen and the record ----------------------------------------------

    def step(self, n: int, key: str, *, ok=None, **fields) -> None:
        line = self.t(f"svc_{key}", n=n, total=STEPS, **fields)
        if ok is not None:
            line += " " + self.t("svc_pass" if ok else "svc_fail")
        self.say(line)
        record = {"step": n, "key": key, "ok": ok, **fields, "wall_ns": time.time_ns()}
        self.records.append(record)
        if self.log_path is not None:
            # Every step on disk at once: the run journal exists only from
            # step 5 on, and the shutdown comes after it is closed.
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("a") as handle:
                    handle.write(json.dumps(record, default=str) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError:
                pass

    def owns_vlm(self) -> bool:
        """R15: the VLM port's listener is our unit as recorded (pid and
        starttime), and every listener there is held by its processes."""
        started = self.started
        if started is None or not self.vlm.matches(started):
            return False
        held = self.vlm.proc.listeners(self.vlm.port, (self.vlm.unit,))
        return bool(held) and all(lsn.pids for lsn in held)

    def _record_path(self) -> Path:
        return self.record_dir / UNIT_FILE

    def _keep_identity(self, found: Identity | None) -> None:
        path = self._record_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(asdict(found) if found else {}))
        os.replace(temporary, path)

    def _recorded(self) -> Identity | None:
        try:
            value = json.loads(self._record_path().read_text())
            return Identity(**value) if value else None
        except (OSError, ValueError, TypeError):
            return None

    def fail(self, n: int, code: str, detail: str):
        self.done = True  # the shutdown below is this failure's
        stopped = self._stop_ours()
        self.step(n, "failed", ok=False, code=code, detail=str(detail)[:200])
        self.say(self.t("svc_stopped", units=", ".join(stopped) or "-"))
        self._release_lock()
        raise ServiceFailed(n, code, detail, stopped)

    # --- 1/7 ------------------------------------------------------------------

    def preflight(self) -> None:
        self.step(1, "preflight")
        if not self.lock.acquire():
            self.fail(1, "gpu_lock_busy", "the GPU lock is held by another job")
        # Our own leftover first: it holds memory and the port.
        self._leftover()
        reading = self.gpu()
        if not reading:
            self.fail(1, "gpu_unreadable", "nvidia-smi could not be read")
        need = self.need_policy_mib + self.need_vlm_mib
        self.baseline_used = reading["used_mib"]
        self.step(
            1,
            "gpu",
            ok=reading["free_mib"] >= need,
            free=reading["free_mib"],
            need=need,
            policy=self.need_policy_mib,
            vlm=self.need_vlm_mib,
        )
        if reading["free_mib"] < need:
            self.fail(1, "gpu_memory_short", f"{reading['free_mib']} < {need} MiB")
        mem = self.mem()
        self.step(
            1,
            "mem",
            ok=mem is not None and mem >= self.min_mem_gib,
            mem=mem,
            need=self.min_mem_gib,
        )
        if mem is None or mem < self.min_mem_gib:
            self.fail(1, "host_memory_short", f"MemAvailable {mem} GiB")
        for port in (self.policy_port, self.vlm.port):
            if self.listeners(port):
                self.step(1, "port_held", ok=False, port=port)
                self.fail(
                    1, "port_held", f"port {port} is held (stop the live service first)"
                )
        self.step(1, "ports", ok=True, ports=f"{self.policy_port}, {self.vlm.port}")

    def _leftover(self) -> None:
        """A VLM unit of an earlier run of ours: stopped only when the
        recorded identity still matches; otherwise left as it is."""
        recorded = self._recorded()
        if recorded is None:
            return
        if self.vlm.matches(recorded):
            self.vlm.stop(recorded)
            self.step(1, "leftover_stopped", ok=True, unit=recorded.unit)
        self._keep_identity(None)

    # --- 2/7, 3/7, 4/7 ----------------------------------------------------------

    def start_vlm(self) -> None:
        self.step(2, "vlm_start")
        before = (self.gpu() or {}).get("used_mib")
        try:
            self.vlm.start()
        except UnitRefused as exc:
            self.fail(2, exc.code, exc.detail)
        # Recorded before anything else (R15): a failed readiness still stops
        # exactly this unit, and the next start finds it.
        self.started = self.vlm.identity()
        self._keep_identity(self.started)
        try:
            self.started = self.vlm.wait_ready(self.ready_timeout_s)
        except UnitRefused as exc:
            self.started = self.started or self.vlm.identity()
            self.fail(2, exc.code, exc.detail)
        self._keep_identity(self.started)
        self.our_pids |= set(self.vlm.pids())
        after = (self.gpu() or {}).get("used_mib")
        use = None if before is None or after is None else after - before
        self.step(
            2,
            "vlm_ready",
            ok=True,
            unit=self.started.unit,
            pid=self.started.pid,
            mib=use,
        )

    def sleep_vlm(self, level: int = 1) -> None:
        self.step(3, "vlm_sleep")
        mem = self.mem()
        if level == 1 and (mem is None or mem < self.min_mem_gib):
            self.fail(3, "host_memory_short", f"MemAvailable {mem} GiB")
        if self.power.sleep(level) is not True:
            self.fail(3, "sleep_failed", "the VLM did not go to sleep")
        self.step(3, "vlm_asleep", ok=True, level=level, mem=mem)

    def policy_ready(self, policy_pids=()) -> None:
        reading = self.gpu() or {}
        self.our_pids |= set(policy_pids)
        self.step(4, "policy_ready", ok=True, used=reading.get("used_mib"))

    def rounds(self) -> None:
        self.step(5, "inventory")
        self.step(6, "rounds")

    # --- 7/7 --------------------------------------------------------------------

    def _stop_ours(self) -> list:
        stopped = []
        if self.started is not None:
            try:
                self.vlm.stop(self.started)
                stopped.append(self.started.unit)
                self.started = None
                self._keep_identity(None)
            except UnitRefused as exc:
                self.say(self.t("svc_not_stopped", unit=self.vlm.unit, why=exc.code))
        return stopped

    def _release_lock(self) -> None:
        if getattr(self.lock, "held", False):
            self.lock.release()

    def shutdown(self, *, policy_stopped: bool = True) -> dict:
        """Stop our VLM (identity checked), then the release check; the lock
        is released after the check. Returns what was found."""
        if self.done:
            return {}
        self.done = True
        self.step(7, "shutdown")
        mismatch = self.started is not None and not self.vlm.matches(self.started)
        stopped = self._stop_ours()
        deadline = self.clock() + self.release_timeout_s
        left, back = None, False
        while True:
            try:
                left = sorted(self.our_pids & {row["pid"] for row in self.apps()})
            except Exception:  # noqa: BLE001 - unreadable: not released
                left = None
            reading = self.gpu() or {}
            used = reading.get("used_mib")
            back = (
                self.baseline_used is not None
                and used is not None
                and used <= self.baseline_used + RELEASE_TOLERANCE_MIB
            )
            if left == [] and back:
                break
            if self.clock() >= deadline:
                break
            self.sleep(self.poll_s)
        released = left == [] and back and not mismatch and policy_stopped
        self.step(
            7,
            "released" if released else "not_released",
            ok=released,
            stopped=", ".join(stopped) or "-",
            left=left,
            used=(self.gpu() or {}).get("used_mib"),
            baseline=self.baseline_used,
        )
        self._release_lock()
        return {
            "released": released,
            "stopped": stopped,
            "left": left,
            "mismatch": mismatch,
        }


def _healthy(port: int) -> bool:
    """Our own VLM's ``/health`` on its loopback port."""
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{int(port)}/health", timeout=5
        ) as r:
            return 200 <= r.status < 300
    except (OSError, ValueError):
        return False


def for_run(
    vlm_binding: dict,
    *,
    policy_unit,
    policy_port,
    say,
    t,
    record_dir,
    log_path,
    profile,
    min_mem_gib,
):
    """The real ``Services`` of a binding with ``subtask_vlm.service``
    (unverified on hardware): user systemd, the kernel's socket tables and our
    units' own cgroups, nvidia-smi aggregates, LEVI's GPU flock."""
    from levi.live import gpumgr
    from levi.live.config import Vllm as LiveVllm

    from .campaign.switch import ProcView, UserSystemd
    from .subtask_vlm import PortVlmPower, mem_available_gib

    service = vlm_binding["service"]
    systemd = UserSystemd()
    proc = ProcView(None, systemd=systemd)
    port = vlm_binding["port"]
    units = (service["unit"],) + ((policy_unit,) if policy_unit else ())
    vlm = SystemdUnit(
        service["unit"],
        service["argv"],
        port,
        systemd=systemd,
        proc=proc,
        health=_healthy,
        env=service["env"],
    )
    reading = gpumgr.vram(vlm_binding["gpu_index"]) or {}
    total = reading.get("total_mib")
    util = profile.get("gpu_memory_utilization") or 0.70
    need_vlm = (
        int(util * (total - gpumgr.VLLM_TOTAL_SLACK_MIB)) + LiveVllm().margin_mib
        if total
        else 10**9
    )
    holder: list = []
    services = Services(
        vlm=vlm,
        power=PortVlmPower(
            port,
            gpu_index=vlm_binding["gpu_index"],
            gpu_memory_utilization=vlm_binding.get("gpu_memory_utilization"),
            wake_timeout_s=vlm_binding.get("wake_timeout_s", 60),
            verify=lambda: bool(holder) and holder[0].owns_vlm(),
        ),
        lock=gpumgr.GpuLock(
            service["gpu_lock"], agent="aeri-subtask", allow_unavailable=False
        ),
        gpu=lambda: gpumgr.vram(vlm_binding["gpu_index"]),
        apps=gpumgr.compute_apps,
        mem=mem_available_gib,
        # A listener held by one of our own units is not "held": the policy
        # switcher checks its own leftovers.
        listeners=lambda p: [lsn for lsn in proc.listeners(p, units) if not lsn.pids],
        say=say,
        t=t,
        record_dir=record_dir,
        policy_port=policy_port,
        need_policy_mib=service["need_policy_mib"],
        need_vlm_mib=need_vlm,
        min_mem_gib=min_mem_gib,
        ready_timeout_s=service["ready_timeout_s"],
        log_path=log_path,
    )
    holder.append(services)
    return services
