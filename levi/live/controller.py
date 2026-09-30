"""The supervisor: a small loop that decides, every few seconds, whether
anything needs doing, and starts the heavy parts only when it does.

Idle, it is a few megabytes and a few file-system stats: list the rollout
folders, read the (tiny) evaluation-session files, rewrite ``status.json``.
It imports no numerical library and opens no video. When a dataset has
finished rollouts to label it

1. asks ``gpumgr.decide`` whether the local model may use the GPU now;
2. starts vLLM (once per burst of work) and waits for it to answer;
3. starts one worker process (``levi.live.worker``) for one dataset, which
   exits when its batch is done;
4. stops vLLM after ``vllm.idle_timeout_s`` without work, and immediately when
   the GPU policy says an evaluation needs the GPU back (``should_preempt``).

One dataset is handled at a time (LEVI's baseline check makes two writers to
one dataset conflict, and a single GPU has no use for two); datasets queue by
"longest since last processed".

Standard library only.
"""

import contextlib
import fcntl
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import auto, gpumgr, jsonio, mirror, resources, sessions
from . import config as live_config

SCHEMA = "levi.live.status.v1"
WORKER_STALL_S = 600.0
ERROR_WAIT_S = 60.0
GATE_GRACE_S = 8.0
MAX_EVENTS = 10
STATES_WITH_WORK = ("mirrored",)


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def peek_run(workspace, run_id) -> dict | None:
    """A run record read straight from the store's SQLite file, read-only, so
    the idle supervisor need not import LEVI's agent package."""
    path = Path(workspace) / "outputs/LEVI/workbench/agent/workbench.sqlite3"
    if not path.is_file():
        return None
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            row = db.execute(
                "SELECT body FROM records WHERE kind='runs' AND id=?", (run_id,)
            ).fetchone()
        finally:
            db.close()
    except sqlite3.Error:
        return None
    return json.loads(row[0]) if row else None


def release_leases(workspace, run_ids) -> int:
    """Delete the execution leases of runs whose worker is known to be dead.

    A worker killed without warning leaves its runs' leases to expire (three
    minutes), and the next worker would wait for them. The supervisor knows the
    worker is gone, so it lets go of them at once (the same stdlib-only SQLite
    access as ``peek_run``)."""
    path = Path(workspace) / "outputs/LEVI/workbench/agent/workbench.sqlite3"
    if not run_ids or not path.is_file():
        return 0
    try:
        db = sqlite3.connect(path, timeout=10)
        try:
            with db:
                cursor = db.executemany(
                    "DELETE FROM leases WHERE id=?", [(r,) for r in run_ids]
                )
            return cursor.rowcount
        finally:
            db.close()
    except sqlite3.Error:
        return 0


class Probes:
    """What the controller asks the machine; replaceable in tests."""

    def __init__(
        self,
        vram=gpumgr.vram,
        ports=gpumgr.listening_ports,
        holders=gpumgr.gpu_holders,
        policy_vram=gpumgr.policy_vram_mib,
    ):
        self.vram = vram
        self.ports = ports
        self.holders = holders
        self.policy_vram = policy_vram


class Controller:
    def __init__(
        self, config, *, probes=None, vllm=None, popen=subprocess.Popen, log=None
    ):
        self.config = config
        self.probes = probes or Probes()
        self.popen = popen
        self.log = log or (lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True))
        self.scanner = mirror.Scanner(config)
        self.vllm = vllm or gpumgr.Vllm(config)
        self.lock = gpumgr.GpuLock(config.gpu.lock_file, config.gpu.lock_agent)
        self.started_at = time.time()
        self.running = True
        self.wake = threading.Event()
        self.worker = None
        self.worker_dataset = None
        self.worker_started = 0.0
        self.backoff: dict = {}
        self.failures: dict = {}
        self.awaiting: dict = {}
        self.policy_changed_at: float | None = None
        self.gate = gpumgr.Gate(True, "open", "no evaluation")
        self.gate_closed_at: float | None = None
        self._gate_written = (None, 0.0)
        self._policy_mib = (0.0, None)
        self.idle_since: float | None = None
        self._error_at: float | None = None
        self.decision = gpumgr.Decision(True, "no work", "ok")
        self.state = "starting"
        self.last_error = ""
        self.events: list = []
        self.sessions: dict = {}
        self.tasks: list = []
        self.fr3: dict = {}
        self.policy_up = False
        self._scan_at = 0.0
        self._vram = (0.0, None)
        self._meter = resources.Meter()
        self._cpu = None
        self._cache_at = time.time()
        # Callables taking the time; a returned message is logged as an error
        # (the CLI registers the page/core watchdog here).
        self.hooks: list = []
        self._adopt_orphan()

    # --- events ------------------------------------------------------------------

    def event(self, text, level="info"):
        self.log(text)
        self.events.insert(0, {"time": time.time(), "level": level, "text": text[:300]})
        del self.events[MAX_EVENTS:]
        if level == "error":
            self.last_error = text[:300]

    # --- probes --------------------------------------------------------------------

    def free_mib(self, now, ttl=10.0):
        if now - self._vram[0] > ttl:
            value = self.probes.vram()
            self._vram = (now, value)
        return (self._vram[1] or {}).get("free_mib")

    def _refresh(self, now, full):
        c = self.config
        self.sessions = sessions.read_sessions(c.watch.roots, now)
        ports = self.probes.ports()
        up = any(int(p) in ports for p in c.gpu.policy_ports)
        if up != self.policy_up and self.state != "starting":
            self.policy_changed_at = now
        self.policy_up = up
        self.fr3 = sessions.read_fr3(c.fr3.health_file, c.fr3.stale_s, now)
        if full:
            self.tasks = self.scanner.scan(self.sessions, now)
            self._scan_at = now

    # --- the work queue ---------------------------------------------------------------

    def _queue(self, now) -> list:
        """Dataset names with something to label, longest-waiting first."""
        states = mirror.list_states(self.config)
        scans = {t.name: t for t in self.tasks if t.available}
        p = self.config.pipeline
        names = []
        for name in set(states) | set(scans):
            state = states.get(name) or {}
            scan = scans.get(name)
            todo = [
                d
                for d, row in (state.get("demos") or {}).items()
                if row.get("state") == "mirrored"
                and row.get("attempts", 0) < p.max_attempts
            ]
            if not (state.get("current") or todo or (scan and scan.ready)):
                continue
            if self.backoff.get(name, 0) > now:
                continue
            if name in self.awaiting and not self._human_acted(name, state):
                continue
            names.append((state.get("last_processed_at") or 0.0, name))
        return [n for _, n in sorted(names)]

    def _human_acted(self, name, state) -> bool:
        """After a person's gate: has the run the worker waited on moved?"""
        info = self.awaiting[name]
        if time.time() - info["at"] < self.config.pipeline.human_recheck_s:
            return False
        batch = (state or {}).get("current") or {}
        ids = [r["run_id"] for r in (batch.get("temporal") or {}).values()]
        if batch.get("anchored"):
            ids.append(batch["anchored"]["run_id"])
        for run_id in ids:
            run = peek_run(self.config.workspace, run_id)
            if run and (run["plan"].get("approval") or run["status"] != "planned"):
                self.awaiting.pop(name, None)
                return True
        info["at"] = time.time()
        return False

    # --- GPU ---------------------------------------------------------------------------

    def _evaluating(self) -> bool:
        """An evaluation session is live (not stopped, finished or crashed)."""
        return any(
            s.state not in ("stopped", "finished", "crashed")
            for s in self.sessions.values()
        )

    def policy_mib(self, now, ttl=30.0):
        """VRAM the policy server holds (cached; None if not running/unknown)."""
        if not self.policy_up:
            return None
        if now - self._policy_mib[0] > ttl:
            value = self.probes.policy_vram(
                [int(p) for p in self.config.gpu.policy_ports]
            )
            self._policy_mib = (now, value)
        return self._policy_mib[1]

    def _need(self, profile, now, asleep=False) -> int:
        total = (self._vram[1] or {}).get("total_mib") or 32607
        need = gpumgr.need_mib(self.config, profile, total)
        return max(0, need - gpumgr.ASLEEP_RESIDENT_MIB) if asleep else need

    def _gpu_step(self, now, want: bool) -> bool:
        """Advance the GPU side; True when the model is awake and answering."""
        c = self.config
        mode = c.effective_gpu_mode()
        profile = c.vllm_profile()
        self.gate = gpumgr.gate(
            c,
            mode,
            self.sessions,
            self.policy_up,
            self.policy_changed_at or self.started_at,
        )
        mine = self.vllm.mine()
        state = (
            self.vllm.poll()
            if (mine or self.vllm.state in ("starting", "ready", "asleep"))
            else self.vllm.state
        )
        if state == "error":
            # A failed start (or a vLLM that died) is waited out, not retried
            # in a loop.
            self.lock.release()
            if self._error_at is None:
                self._error_at = now
                self.event("vLLM failed: " + (self.vllm.error or "unknown"), "error")
            if now - self._error_at > ERROR_WAIT_S:
                self.vllm.state, self._error_at = "stopped", None
            self.decision = gpumgr.Decision(
                False, self.vllm.error or "vLLM failed", "error"
            )
            return False
        self._error_at = None
        if mine:
            return self._resident_step(now, want, state, mode, profile)
        if self.vllm.external():
            if mode == "manual" or c.vllm.adopt_external:
                self.decision = gpumgr.Decision(
                    True, "using the vLLM already serving", "external"
                )
                return True
            self.decision = gpumgr.Decision(
                False,
                "a vLLM this service did not start is running on the port",
                "external_busy",
            )
            return False
        if not want:
            return False
        free = None
        need = 0
        if mode != "manual":
            free = self.free_mib(now)
            need = self._need(profile, now)
        since = None if self.policy_changed_at is None else now - self.policy_changed_at
        self.decision = gpumgr.decide(
            c, mode, free_mib=free, need=need, since_policy_change_s=since
        )
        if not self.decision.allowed:
            return False
        if not self.lock.acquire():
            self.decision = gpumgr.Decision(
                False, "another agent holds the GPU lock", "lock", need
            )
            return False
        self.event(f"starting vLLM ({mode}; a cold start takes 45-70 s)")
        if not self.vllm.start(profile):
            self.lock.release()
            self._error_at = now
            self.event("vLLM did not start: " + self.vllm.error, "error")
        return False

    def _resident_step(self, now, want, state, mode, profile) -> bool:
        """vLLM is ours and up: keep it awake only while there is room."""
        c = self.config
        if state == "starting":
            self.decision = gpumgr.Decision(True, "vLLM is starting", "ok")
            return False
        if state == "asleep" and not want:
            # Nothing to decide: no nvidia-smi call while it sleeps unneeded.
            self.decision = gpumgr.Decision(True, "vLLM is asleep", "asleep")
            return False
        free = self.free_mib(now)
        policy = self.policy_mib(now)
        if state == "ready":
            why = gpumgr.should_sleep(c, mode, free_mib=free, policy_mib=policy)
            if why:
                self.sleep_vllm(*why)
                return False
            self.decision = gpumgr.Decision(True, "vLLM is ready", "ok")
            return True
        # asleep: wake when there is work and room for it
        blocked = gpumgr.should_sleep(c, mode, free_mib=None, policy_mib=policy)
        need = self._need(profile, now, asleep=True)
        self.decision = (
            gpumgr.Decision(False, blocked[1], blocked[0], need)
            if blocked
            else gpumgr.decide(
                c, mode, free_mib=free, need=need, since_policy_change_s=None
            )
        )
        if not self.decision.allowed:
            return False
        if self.vllm.wake():
            self.event("vLLM woke up")
            return True
        self.decision = gpumgr.Decision(False, self.vllm.error, "error")
        return False

    def sleep_vllm(self, code, reason):
        """Put vLLM to sleep (its memory is wanted): pause the worker first."""
        self.event(f"vLLM goes to sleep: {reason}")
        self.decision = gpumgr.Decision(False, reason, code)
        self._stop_worker()
        if not self.vllm.sleep():
            # No sleeping (not enabled, or it failed): free the GPU the hard way.
            self.vllm.stop()
            self.lock.release()
        self.idle_since = None

    def preempt(self, code, reason):
        """Give the GPU back entirely: stop the worker, then vLLM."""
        self.event(f"giving the GPU back: {reason}")
        self.decision = gpumgr.Decision(False, reason, code)
        self._stop_worker()
        self.vllm.stop()
        self.lock.release()
        self.idle_since = None

    def _release_if_idle(self, now, work: bool):
        """After ``vllm.idle_timeout_s`` without work: sleep (while an
        evaluation is live: waking is under a second) or stop vLLM; a sleeping
        one is stopped once nothing evaluates any more."""
        v = self.config.vllm
        state = self.vllm.state
        if work or self.worker is not None or not self.vllm.mine():
            self.idle_since = None
            return
        live = self.policy_up or self._evaluating()
        if state == "asleep" and live:
            self.idle_since = None
            return
        if self.idle_since is None:
            self.idle_since = now
            return
        if now - self.idle_since < v.idle_timeout_s:
            return
        sleeps = (
            v.sleep_mode
            and state == "ready"
            and (v.idle_action == "sleep" or (v.idle_action == "auto" and live))
        )
        if sleeps and self.vllm.sleep():
            self.event("no work: vLLM sleeps (the evaluation is live)")
        else:
            self.event("no work: stopping vLLM to free the GPU")
            self.vllm.stop()
            self.lock.release()
        self.idle_since = None

    # --- the worker ----------------------------------------------------------------------

    def provider_spec(self):
        c = self.config
        profile = (
            (self.vllm.profile or c.vllm_profile())
            if self.vllm.mine()
            else c.vllm_profile()
        )
        return {
            "name": c.provider.name,
            "base_url": f"http://127.0.0.1:{c.vllm.port}",
            "port": c.vllm.port,
            "model": c.vllm.served_model,
            "context_tokens": profile["max_model_len"],
            "max_images": profile["max_images"],
        }

    def _spawn(self, name):
        c = self.config
        effective = c.live_dir / "effective.toml"
        effective.parent.mkdir(parents=True, exist_ok=True)
        effective.write_text(live_config.render(c))
        env = resources.service_env(c)
        if c.pipeline.auto_approve:
            env[auto.ENABLE_ENV] = "1"
        c.logs_dir.mkdir(parents=True, exist_ok=True)
        log = c.logs_dir / "worker.log"
        resources.rotate_file(log, c.resources.log_max_mb, c.resources.log_backups)
        argv = [
            sys.executable,
            "-m",
            "levi.live.worker",
            "--config",
            str(effective),
            "--dataset",
            name,
            "--provider-json",
            json.dumps(self.provider_spec()),
        ]
        with log.open("ab") as sink:
            self.worker = self.popen(
                argv,
                cwd=project_root(),
                env=env,
                stdout=sink,
                stderr=sink,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        self.worker_dataset, self.worker_started = name, time.time()
        self.event(f"batch started for {name}")

    def _stop_worker(self, grace=30.0):
        proc = self.worker
        if proc is None:
            return
        if proc.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGTERM)
            deadline = time.time() + grace
            while proc.poll() is None and time.time() < deadline:
                time.sleep(0.2)
            if proc.poll() is None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        # Stopped on purpose (a pause for the GPU), not a failure: no back-off.
        self._reaped(proc.returncode, requested=True)

    def _poll_worker(self, now):
        proc = self.worker
        if proc is None:
            return
        code = proc.poll()
        if code is None:
            progress = jsonio.read(self.config.live_dir / "worker.json") or {}
            stale = now - float(progress.get("updated_at") or self.worker_started)
            if progress.get("pid") == proc.pid and stale > WORKER_STALL_S:
                self.event(
                    f"worker for {self.worker_dataset} stalled {stale:.0f} s: stopping it",
                    "error",
                )
                self._stop_worker(grace=10.0)
            return
        self._reaped(code)

    def _reaped(self, code, requested=False):
        name = self.worker_dataset
        self.worker = None
        self.worker_dataset = None
        now = time.time()
        if requested and code not in (0, 14):
            self._let_go_of_runs(name)
            self.event(f"batch for {name} paused")
        elif code == 0:
            self.failures.pop(name, None)
            self.event(f"batch finished for {name}")
        elif code == 14:
            self.failures.pop(name, None)
        elif code == 13:
            self.event(f"batch for {name} paused")
        elif code == 11:
            self.awaiting[name] = {"at": now}
            self.event(f"{name}: waiting for a person to approve")
        elif code == 10:
            self.backoff[name] = now + 20
            self.event(f"{name}: waiting for the model server")
        else:
            self._let_go_of_runs(name)
            n = self.failures.get(name, 0) + 1
            self.failures[name] = n
            self.backoff[name] = now + min(600.0, 30.0 * 2 ** (n - 1))
            self.event(
                f"batch for {name} failed (exit {code}); retry in {self.backoff[name] - now:.0f} s",
                "error",
            )

    def _let_go_of_runs(self, name):
        state = mirror.load_state(self.config, name) or {}
        batch = state.get("current") or {}
        ids = [r["run_id"] for r in (batch.get("temporal") or {}).values()]
        if batch.get("anchored"):
            ids.append(batch["anchored"]["run_id"])
        release_leases(self.config.workspace, ids)

    def _adopt_orphan(self):
        """A worker started by an earlier supervisor that is still running:
        wait for it rather than starting a second one."""
        progress = jsonio.read(self.config.live_dir / "worker.json") or {}
        pid = progress.get("pid")
        if (
            pid
            and progress.get("phase") != "exited"
            and gpumgr.identity(pid)
            and pid != os.getpid()
        ):
            self.orphan = (pid, progress.get("dataset"))
        else:
            self.orphan = None

    def _orphan_running(self) -> bool:
        if not self.orphan:
            return False
        if gpumgr.identity(self.orphan[0]) is None:
            # It died while we were away (or was killed): its leases are dead too.
            self._let_go_of_runs(self.orphan[1])
            self.orphan = None
            return False
        return True

    # --- the tick ---------------------------------------------------------------------------

    def _write_gate(self, now):
        """``live/gate.json``: the worker's permission to send model requests.
        Rewritten on change and at least every 4 s (a stale gate reads as
        closed, so a dead supervisor cannot leave it open)."""
        key = (self.gate.open, self.gate.code)
        if key == self._gate_written[0] and now - self._gate_written[1] < 4.0:
            return
        self._gate_written = (key, now)
        jsonio.write(
            self.config.live_dir / "gate.json",
            {
                "open": self.gate.open,
                "code": self.gate.code,
                "reason": self.gate.reason,
                "updated_at": now,
            },
        )

    def _police_worker(self, now):
        """A worker that does not stand down while the gate is closed is
        stopped (its runs pause): the policy must not wait for it."""
        if self.gate.open:
            self.gate_closed_at = None
            return
        if self.gate_closed_at is None:
            self.gate_closed_at = now
        if self.worker is None or now - self.gate_closed_at < GATE_GRACE_S:
            return
        progress = jsonio.read(self.config.live_dir / "worker.json") or {}
        if progress.get("pid") == self.worker.pid and progress.get("phase") == "gated":
            return
        self.event("the worker did not stand down while the policy infers: stopping it")
        self._stop_worker()

    def tick(self, now=None) -> float:
        """One pass; returns seconds to wait before the next."""
        now = time.time() if now is None else now
        c = self.config
        evaluating = self._evaluating()
        busy = (
            self.worker is not None
            or self.vllm.mine()
            or evaluating
            or self.state in ("annotating", "gpu_wait", "active")
        )
        interval = c.service.poll_active_s if busy else c.service.poll_idle_s
        full = now - self._scan_at >= interval or not self.tasks
        self._refresh(now, full)
        self._poll_worker(now)
        queue = self._queue(now)
        orphan = self._orphan_running()
        want = bool(queue) and not orphan
        ready = self._gpu_step(now, want or self.worker is not None)
        self._write_gate(now)
        self._police_worker(now)
        if self.worker is None and want and ready and not orphan and self.gate.open:
            self._spawn(queue[0])
        self._release_if_idle(now, bool(queue) or orphan)
        waiting = any(t.waiting for t in self.tasks)
        if self.worker is not None or orphan:
            self.state = "annotating"
        elif want and (not ready or not self.gate.open):
            self.state = "gpu_wait"
        elif evaluating or waiting or queue or self.awaiting:
            self.state = "active"
        else:
            self.state = "idle"
        if (
            self.last_error
            and now - (self.events[0]["time"] if self.events else now) > 600
        ):
            self.last_error = ""
        self._maintain(now)
        for hook in self.hooks:
            message = hook(now)
            if message:
                self.event(message, "error")
        self.write_status(now)
        if self.worker is not None and self.sessions:
            return c.service.gate_poll_s
        return (
            c.service.heartbeat_s
            if not busy
            else min(c.service.poll_active_s, c.service.heartbeat_s)
        )

    def _maintain(self, now):
        """Cache cap, no more often than every ten minutes and never while a
        batch runs."""
        if self.worker is not None or now - self._cache_at < 600:
            return
        self._cache_at = now
        keep = []
        for state in mirror.list_states(self.config).values():
            batch = state.get("current") or {}
            keep += [r["run_id"] for r in (batch.get("temporal") or {}).values()]
            if batch.get("anchored"):
                keep.append(batch["anchored"]["run_id"])
        workbench = self.config.workspace / "outputs/LEVI/workbench"
        result = resources.trim_cache(
            workbench, self.config.resources.cache_max_gib, keep=keep
        )
        if result["removed"]:
            self.event(
                f"cache trimmed: {result['before'] >> 20} -> {result['after'] >> 20} MiB"
            )

    # --- status ------------------------------------------------------------------------------

    def dataset_rows(self, now) -> dict:
        states = mirror.list_states(self.config)
        scans = {t.name: t for t in self.tasks}
        fault_sessions = {
            mirror.dataset_name(g, t): s
            for (g, t), s in self.sessions.items()
            if s.fault
        }
        rows = {}
        for name in sorted(set(states) | set(scans)):
            state = states.get(name) or {}
            scan = scans.get(name)
            counts = mirror.counts(state) if state else {}
            ready = len(scan.ready) if scan else 0
            current = state.get("current")
            row = {
                "episodes": sum(counts.values()) + ready,
                "pending": counts.get("mirrored", 0) + ready,
                "annotating": len((current or {}).get("demos") or []),
                "done": counts.get("done", 0),
                "failed": counts.get("failed", 0),
                "skipped": counts.get("skipped", 0),
                "rejected": counts.get("rejected", 0),
                "waiting": len(scan.waiting) if scan else 0,
                "backlog": scan.backlog if scan else 0,
                "incomplete": (
                    scan.incomplete
                    if scan
                    else (state.get("incomplete") or {}).get("count", 0)
                ),
                "fr3_fault": (
                    scan.fr3_fault
                    if scan
                    else (state.get("incomplete") or {}).get("fr3_fault", 0)
                ),
                "discarded": scan.discarded if scan else state.get("discarded", 0),
                "available": scan.available if scan else False,
                "last_processed_at": state.get("last_processed_at") or None,
                "last_error": (state.get("last_error") or "")[:200],
                "fault": False,
            }
            reasons = []
            if name in fault_sessions:
                reasons.append(
                    "evaluation session reports a fault: "
                    + (fault_sessions[name].reason or "no reason given")[:150]
                )
            if row["fr3_fault"]:
                reasons.append(
                    f"{row['fr3_fault']} rollout(s) aborted by an FR3 fault (incomplete_*)"
                )
            row["fault"] = bool(reasons)
            row["fault_reasons"] = reasons
            if name == self.worker_dataset:
                row["state"] = "annotating"
            elif name in self.awaiting:
                row["state"] = "awaiting_approval"
            elif name in self.backoff and self.backoff[name] > now:
                row["state"] = "error"
            elif row["pending"] or row["waiting"]:
                row["state"] = "pending"
            else:
                row["state"] = "idle"
            rows[name] = row
        cap = self.config.resources.status_max_datasets
        if len(rows) > cap:
            keep = sorted(rows, key=lambda n: -(rows[n]["last_processed_at"] or 0))[
                :cap
            ]
            rows = {n: rows[n] for n in sorted(keep)}
        return rows

    def status(self, now=None) -> dict:
        now = time.time() if now is None else now
        c = self.config
        rows = self.dataset_rows(now)
        progress = jsonio.read(c.live_dir / "worker.json") or {}
        worker = None
        if self.worker is not None:
            worker = {
                "pid": self.worker.pid,
                "dataset": self.worker_dataset,
                "phase": progress.get("phase"),
                "note": progress.get("note"),
                "started_at": self.worker_started,
            }
        cpu = self._meter.percent()
        if cpu is not None:
            self._cpu = cpu
        session_rows = [s.public() for s in list(self.sessions.values())[:16]]
        pid = os.getpid()
        return {
            "schema": SCHEMA,
            "pid": pid,
            "started_at": self.started_at,
            "updated_at": now,
            "state": self.state,
            "accepts_sessions": self.state not in ("starting", "stopped", "error"),
            "ui_url": f"http://{c.service.host}:{c.service.ui_port}",
            "core_port": c.service.core_port,
            "workspace": str(c.workspace),
            "config": c.path,
            "auto_approve": c.pipeline.auto_approve,
            # Absolute: the client checks that one covers its --rollout-root.
            "watch_roots": [str(Path(r).expanduser().resolve()) for r in c.watch.roots],
            "gpu": {
                "mode": c.effective_gpu_mode(),
                "configured_mode": c.gpu.mode,
                "vllm_state": self.vllm.state,
                "vllm": self.vllm.public(),
                "policy_server_seen": self.policy_up,
                "gate": {
                    "open": self.gate.open,
                    "code": self.gate.code,
                    "reason": self.gate.reason[:200],
                },
                "decision": {
                    "allowed": self.decision.allowed,
                    "code": self.decision.code,
                    "reason": self.decision.reason[:200],
                },
                "free_mib": (self._vram[1] or {}).get("free_mib"),
                "lock_held": self.lock.held,
            },
            "datasets": rows,
            "queue_depth": sum(1 for r in rows.values() if r["pending"]),
            "worker": worker,
            "sessions": session_rows,
            "fr3": {
                k: v
                for k, v in self.fr3.items()
                if k
                in (
                    "state",
                    "detail",
                    "age_s",
                    "robot_mode_name",
                    "current_errors",
                    "reasons",
                )
            },
            "events": self.events,
            "last_error": self.last_error,
            "resources": {
                "rss_mb": resources.rss_mb(pid),
                "threads": resources.thread_count(pid),
                "cpu_percent": self._cpu,
            },
        }

    def write_status(self, now=None):
        jsonio.write(self.config.status_file, self.status(now))

    # --- lifecycle ---------------------------------------------------------------------------

    def run(self, *, once=False, max_seconds=None):
        """The loop. ``once``: return when nothing is left to do."""
        started = time.time()
        idle_rounds = 0
        while self.running:
            wait = self.tick()
            if once:
                busy = self.worker is not None or self._orphan_running()
                queue = self._queue(time.time())
                if not busy and not queue and self.state in ("idle", "active"):
                    waiting = any(t.waiting for t in self.tasks)
                    idle_rounds += 1
                    if not waiting or idle_rounds > 3:
                        break
                else:
                    idle_rounds = 0
                if self.state == "gpu_wait" and (
                    not self.gate.open
                    or self.decision.code in ("manual", "lock", "vram", "external_busy")
                ):
                    break
            if max_seconds and time.time() - started > max_seconds:
                break
            self.wake.wait(min(wait, 1.0) if once else wait)
            self.wake.clear()
        return self.state

    def shutdown(self):
        self.running = False
        self.wake.set()
        self._stop_worker(grace=30.0)
        if self.vllm.mine():
            self.vllm.stop()
        self.lock.release()
        self.state = "stopped"
        with contextlib.suppress(Exception):
            self.write_status()


# --- single instance -----------------------------------------------------------------------


class Instance:
    """The one running service per home directory: a flock and a pid file."""

    def __init__(self, home):
        self.home = Path(home)
        self.lock_path = self.home / "live.lock"
        self.pid_path = self.home / "live.pid"
        self._handle = None

    def acquire(self) -> bool:
        self.home.mkdir(parents=True, exist_ok=True)
        handle = open(self.lock_path, "a+")  # noqa: SIM115
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        jsonio.write(
            self.pid_path,
            {
                "pid": os.getpid(),
                "identity": gpumgr.identity(os.getpid()),
                "started_at": time.time(),
            },
        )
        return True

    def release(self):
        with contextlib.suppress(OSError):
            self.pid_path.unlink()
        if self._handle:
            with contextlib.suppress(OSError):
                fcntl.flock(self._handle, fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None

    def holder(self) -> dict | None:
        """The running service's pid record, if its process is alive."""
        record = jsonio.read(self.pid_path)
        if (
            isinstance(record, dict)
            and record.get("pid")
            and gpumgr.identity(record["pid"]) == record.get("identity")
        ):
            return record
        return None
