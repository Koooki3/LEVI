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
GATE_GRACE_S = 8.0
MAX_EVENTS = 10
STATES_WITH_WORK = ("mirrored",)


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


RUN_ENDED = ("cancelled", "failed", "succeeded", "partially_succeeded")


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


def peek_record(workspace, kind, record_id) -> dict | None:
    """Any store record (``changes`` for a draft) read the same way."""
    path = Path(workspace) / "outputs/LEVI/workbench/agent/workbench.sqlite3"
    if not path.is_file():
        return None
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            row = db.execute(
                "SELECT body FROM records WHERE kind=? AND id=?", (kind, record_id)
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
        # Datasets waiting for a person (plan not approved, draft not
        # committed): remembered in the dataset state across restarts.
        self.awaiting: dict = {}
        for name, state in mirror.list_states(config).items():
            if isinstance(state.get("awaiting"), dict):
                self.awaiting[name] = {**state["awaiting"], "at": 0.0}
        self.policy_changed_at: float | None = None
        self.gate = gpumgr.Gate(True, "open", "no evaluation")
        self.gate_closed_at: float | None = None
        self._gate_written = (None, 0.0)
        self._policy_mib = (0.0, None)
        self.idle_since: float | None = None
        self._waiting_since: dict = {}
        self._status_at = 0.0
        self.loop_at: float | None = None  # the last tick (the thread beats on)
        self.start_failures = 0
        self.next_start_at = 0.0
        self.attention: dict | None = None
        self._paused: dict | None = None  # the status' labelling_paused
        self._gate_code_since: tuple = (None, 0.0)
        self._resume_seen = None
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
        # Callable returning the page/core state for the status file (set by
        # the CLI when it starts them); None when they are not ours.
        self.frontend = None
        self._adopt_orphan()
        self._adopt_vllm()

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
        # When each session began waiting for the operator's reset (the gate
        # closes shortly before the episode that follows).
        # The client says when the wait began (``waiting_reset_since``); an older
        # client does not: then it is when this service first saw it.
        waiting = {
            s.path: s.waiting_reset_since
            for s in self.sessions.values()
            if s.state == "waiting_reset"
        }
        self._waiting_since = {
            p: told or self._waiting_since.get(p, now) for p, told in waiting.items()
        }
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
        """Has the person done what the worker waited for? A plan: approved (or
        moved on). A draft: committed or rejected -- approval alone is not
        enough, the worker cannot commit without the approver."""
        info = self.awaiting[name]
        if time.time() - info["at"] < self.config.pipeline.human_recheck_s:
            return False
        ws = self.config.workspace
        if info.get("kind") == "changes" and info.get("changeset"):
            change = peek_record(ws, "changes", info["changeset"])
            run = peek_run(ws, info.get("run_id")) if info.get("run_id") else None
            acted = bool(
                (change and change.get("status") in ("committed", "rejected"))
                # The person dealt with the run itself (cancelled it, it
                # ended) or the draft is gone: nothing is left to wait for.
                or (run and run.get("status") in RUN_ENDED)
                or (change is None and run)
            )
        else:
            run = peek_run(ws, info.get("run_id")) if info.get("run_id") else None
            acted = bool(
                run and (run["plan"].get("approval") or run["status"] != "planned")
            )
        if acted:
            self.awaiting.pop(name, None)
            with contextlib.suppress(Exception):
                jsonio.update(
                    mirror.state_path(self.config, name),
                    lambda v: (v.pop("awaiting", None), v)[1],
                    default=dict,
                )
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

    def _evaluation_unfinished(self) -> bool:
        """A session is on the robot or between episodes (running, homing,
        waiting for the reset): the policy answers at any moment and a cold
        start's effect on its latency is unmeasured. Standby, finished,
        stopped, crashed and fault sessions do not count."""
        return any(
            s.state in ("running", "homing", "waiting_reset")
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
            now=now,
            waiting_since=self._waiting_since,
        )
        mine = self.vllm.mine()
        state = (
            self.vllm.poll()
            if (mine or self.vllm.state in ("starting", "ready", "asleep"))
            else self.vllm.state
        )
        self._check_resume(now)
        if state == "error":
            # A failed start (or a vLLM that died): note it, back off, and
            # after a few in a row stop and ask for a person.
            if not self.vllm.mine():
                self.lock.release()
            self._vllm_failed(now)
            self.vllm.state = "stopped"
            state = "stopped"
        if self.attention:
            self.decision = gpumgr.Decision(
                False,
                f"vLLM failed to start {self.start_failures} times: "
                f"{self.attention['reason']}; `levi live resume` clears it",
                "needs_attention",
            )
            return False
        if mine:
            return self._resident_step(now, want, state, mode, profile)
        # Only look at :8100 when there is work for it (an idle tick opens no
        # socket, and the port may be another agent's vLLM).
        if want and self.vllm.external():
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
        if now < self.next_start_at:
            self.decision = gpumgr.Decision(
                False,
                f"the last vLLM start failed; trying again in "
                f"{self.next_start_at - now:.0f} s ({self.vllm.error})",
                "backoff",
            )
            return False
        if not self.gate.open and mode != "manual":
            # A cold start is 45 s of heavy GPU load: never while the policy infers.
            self.decision = gpumgr.Decision(
                False, "waiting for the gate: " + self.gate.reason, "gate_closed"
            )
            return False
        if mode != "manual" and self._evaluation_unfinished():
            self.decision = gpumgr.Decision(
                False,
                "an evaluation is under way: no cold start (45-70 s of GPU load, "
                "effect on the policy's latency unmeasured) until it is between "
                "runs; `levi live start --prewarm` starts vLLM before",
                "evaluation_active",
            )
            return False
        free = None
        need = 0
        if mode != "manual":
            free = self.free_mib(now)
            total = (self._vram[1] or {}).get("total_mib") or 32607
            if free is not None:
                # Count the policy only when its memory is really held, read
                # now (not the 30 s cache): a listening port may still be loading.
                held = gpumgr.policy_loaded(c, self.policy_up, self.policy_mib(now, 0))
                if held == "loading":
                    waited = now - (self.policy_changed_at or self.started_at)
                    if waited < c.gpu.policy_load_wait_s:
                        self.decision = gpumgr.Decision(
                            False,
                            "the policy server is listening but holds only "
                            f"{self._policy_mib[1]} MiB: still loading, waiting for "
                            "its memory before planning vLLM's budget",
                            "settling",
                        )
                        return False
                plan = gpumgr.plan_budget(
                    c, free_mib=free, total_mib=total, policy_up=held == "loaded"
                )
                if not plan.ok:
                    self.decision = gpumgr.Decision(
                        False, plan.reason, plan.code, plan.need_mib
                    )
                    return False
                profile = {
                    **profile,
                    "gpu_memory_utilization": plan.utilization,
                    "max_model_len": plan.max_model_len,
                }
                need = plan.need_mib
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
        self.event(
            f"starting vLLM (budget {profile['gpu_memory_utilization']}, "
            f"max_model_len {profile['max_model_len']}, {free} MiB free, "
            f"{'beside' if self.policy_up else 'no'} policy server; "
            "a cold start takes 45-70 s)"
        )
        if not self.vllm.start(profile, fd=self.lock.fileno()):
            if not self.vllm.mine():
                self.lock.release()
            self._vllm_failed(now)
            self.vllm.state = "stopped"
        return False

    def _vllm_failed(self, now):
        """One failed start: wait 60 s doubling to 600 s; after
        ``vllm.max_start_failures`` in a row stop and need a person."""
        v = self.config.vllm
        self.start_failures += 1
        reason = self.vllm.failure_reason()
        self.vllm.error = reason
        wait = min(
            v.start_backoff_max_s, v.start_backoff_s * 2 ** (self.start_failures - 1)
        )
        self.next_start_at = now + wait
        self.event(
            f"vLLM failed to start ({self.start_failures} of {v.max_start_failures}): {reason}",
            "error",
        )
        if self.start_failures >= v.max_start_failures:
            self.attention = {"code": "vllm_failed", "reason": reason, "since": now}
            self.event(
                "vLLM keeps failing: labelling is paused until `levi live resume`",
                "error",
            )

    def _check_resume(self, now):
        """``levi live resume`` leaves ``live/resume.json``: forget the failures."""
        path = self.config.live_dir / "resume.json"
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            return
        if stamp != self._resume_seen:
            self._resume_seen = stamp
            if self.start_failures or self.attention:
                self.event("resumed: vLLM may be started again")
            self.start_failures, self.attention, self.next_start_at = 0, None, 0.0

    def _resident_step(self, now, want, state, mode, profile) -> bool:
        """vLLM is ours and up: keep it awake only while there is room."""
        c = self.config
        if state == "ready":
            self.start_failures = 0
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
        need = self._need(self.vllm.profile or profile, now, asleep=True)
        self.decision = (
            gpumgr.Decision(False, blocked[1], blocked[0], need)
            if blocked
            else gpumgr.decide(
                c, mode, free_mib=free, need=need, since_policy_change_s=None
            )
        )
        if not self.decision.allowed:
            return False
        if mode != "manual" and not self.gate.open:
            # ...and the policy is not inferring or about to (a wake is quick
            # but not free): never during the evaluation's inference.
            self.decision = gpumgr.Decision(
                False, "waiting for the gate: " + self.gate.reason, "gate_closed"
            )
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
            self._stop_vllm()
        self.idle_since = None

    def preempt(self, code, reason):
        """Give the GPU back entirely: stop the worker, then vLLM."""
        self.event(f"giving the GPU back: {reason}")
        self.decision = gpumgr.Decision(False, reason, code)
        self._stop_worker()
        self._stop_vllm()
        self.idle_since = None

    def _stop_vllm(self) -> bool:
        """Stop vLLM and let go of the GPU lock *only once the process is
        gone*: the lock says "vLLM is on the GPU"."""
        gone = self.vllm.stop()
        if gone:
            self.lock.release()
        return gone

    def _adopt_vllm(self):
        """A vLLM this service started before a restart is still running. Its
        GPU lock must still be held: by the vLLM process itself (it inherited
        the descriptor) or, failing that, by us now. If somebody else holds
        the lock while it runs, take no part in it and say so."""
        if not self.vllm.mine():
            return
        if self.lock.acquire() or self.vllm.holds_lock(self.config.gpu.lock_file):
            self.event("took back the vLLM that was running")
            return
        pid = (jsonio.read(self.vllm.record_path) or {}).get("pid")
        with contextlib.suppress(OSError):
            self.vllm.record_path.unlink()
        self.vllm.state = "stopped"
        self.event(
            f"a vLLM (pid {pid}) is running but another agent holds the GPU lock: "
            "not taking it over; it is treated as someone else's server",
            "error",
        )

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
        if state == "asleep" and (live or v.prewarm):
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
            and (
                v.prewarm
                or v.idle_action == "sleep"
                or (v.idle_action == "auto" and live)
            )
        )
        if sleeps and self.vllm.sleep():
            self.event("no work: vLLM sleeps (the evaluation is live)")
        elif v.prewarm:
            pass  # a prewarmed vLLM is never stopped before the service is
        else:
            self.event("no work: stopping vLLM to free the GPU")
            self._stop_vllm()
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
            gate = (jsonio.read(self.config.live_dir / "worker.json") or {}).get(
                "awaiting"
            ) or {}
            self.awaiting[name] = {"at": now, **gate}
            what = (
                "approve the plan" if gate.get("kind") == "plan" else "commit the draft"
            )
            self.event(f"{name}: waiting for a person to {what}")
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

    def _update_paused(self, now, wanted):
        """``labelling_paused`` for the status: why nothing is being labelled
        that a person (or the robot client's operator) can see and act on.
        Not set for ordinary waits (the gate closing while the policy infers,
        a settling policy server): those pass by themselves."""
        c = self.config
        code = reason = since = None
        code_now = None if self.gate.open else self.gate.code
        if self._gate_code_since[0] != code_now:
            self._gate_code_since = (code_now, now)
        if self.attention:
            code = self.attention.get("code") or "vllm_failed"
            reason = (
                f"vLLM failed to start repeatedly: {self.attention.get('reason')}; "
                "`levi live resume` clears it"
            )
            since = self.attention.get("since")
        elif wanted and self.decision.code == "insufficient_vram":
            code, reason = "insufficient_vram", self.decision.reason
        elif wanted and self.decision.code in ("backoff", "error"):
            code = "vllm_error"
            reason = self.vllm.error or self.decision.reason
        elif (
            code_now == "unknown_client"
            and now - self._gate_code_since[1] >= c.gpu.unknown_client_pause_s
        ):
            code = "unknown_client"
            reason = (
                "a policy server is running that no evaluation session of this "
                f"service vouches for: {self.gate.reason}"
            )
            since = self._gate_code_since[1]
        if code is None:
            self._paused = None
            return
        if self._paused is None or self._paused["code"] != code:
            self._paused = {"code": code, "since": since or now}
        self._paused["reason"] = str(reason)[:300]

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
        prewarm = (
            self.config.vllm.prewarm and self.worker is None and not self.vllm.mine()
        )
        wanted = want or self.worker is not None or prewarm
        ready = self._gpu_step(now, wanted)
        self._update_paused(now, wanted)
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
        self.loop_at = now
        if now - self._status_at >= min(1.0, c.service.heartbeat_s):
            self._status_at = now
            self.write_status(now)
        if self.worker is not None and self.sessions:
            return c.service.gate_poll_s
        if self._evaluation_unfinished():
            # The gate's lead (episode_imminent) is a few seconds: look at least
            # once a second while a session is on the robot or between episodes.
            return min(1.0, c.service.heartbeat_s)
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
        for name in mirror.list_states(self.config):
            changed = mirror.verify_sources(self.config, name)
            if changed:
                self.event(
                    f"{name}: the source of {len(changed)} mirrored demo(s) changed"
                )
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
            for (_r, g, t), s in self.sessions.items()
            if s.fault
        }
        rows = {}
        for name in sorted(set(states) | set(scans)):
            state = states.get(name) or {}
            scan = scans.get(name)
            counts = mirror.counts(state) if state else {}
            ready = len(scan.ready) if scan else 0
            current = state.get("current")
            demos = (state.get("demos") or {}).values()
            row = {
                "review_runs_open": state.get("review_runs_open", 0),
                "stuck": counts.get("stuck", 0),
                "source_changed": sum(1 for d in demos if d.get("source_changed")),
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
                row["awaiting"] = self.awaiting[name].get("kind")
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
        session_rows = []
        for s in list(self.sessions.values())[:16]:
            row = s.public()
            # When it began waiting for the operator's reset (the page can
            # count down to the next episode with reset_wait_s).
            row["waiting_reset_since"] = self._waiting_since.get(s.path)
            session_rows.append(row)
        pid = os.getpid()
        return {
            "schema": SCHEMA,
            "pid": pid,
            "started_at": self.started_at,
            "updated_at": now,
            # The last time the main loop ticked: the heartbeat thread keeps
            # updated_at fresh even when the loop is stuck.
            "loop_at": self.loop_at,
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
                "lock_held": self.lock.held
                or self.vllm.holds_lock(self.config.gpu.lock_file),
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
            # Set when the service gave up starting vLLM and needs a person
            # (`levi live resume`); labelling is paused, sessions still welcome.
            "attention": self.attention,
            # null, or {code, reason, since} when nothing is being labelled for a
            # reason that does not pass by itself (vllm_failed, vllm_error,
            # insufficient_vram, unknown_client). Sessions are still accepted.
            "labelling_paused": dict(self._paused) if self._paused else None,
            "frontend": self.frontend() if self.frontend else None,
            "events": list(self.events),
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
        beat = self._start_heartbeat()
        try:
            return self._loop(once, max_seconds, started, idle_rounds)
        finally:
            beat.set()

    def _start_heartbeat(self):
        """status.json keeps its heartbeat while the loop is busy: stopping a
        worker, putting vLLM to sleep or stopping it can take a minute or two,
        and a client that finds the file stale falls back to manual labelling.
        A thread rewrites it every ``heartbeat_s`` whatever the loop is doing."""
        stop = threading.Event()

        def beat():
            while not stop.wait(self.config.service.heartbeat_s):
                with contextlib.suppress(Exception):
                    self.write_status()

        threading.Thread(target=beat, daemon=True, name="levi-live-heartbeat").start()
        return stop

    def _loop(self, once, max_seconds, started, idle_rounds):
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
            self._stop_vllm()
        self.lock.release()
        self.state = "stopped"
        with contextlib.suppress(Exception):
            self.write_status()


# --- single instance -----------------------------------------------------------------------


class Instance:
    """The one running service per home directory *and* per workspace: two
    flocks (``<home>/live.lock``, ``<workspace>/live/service.lock``) and a pid
    file. Two homes cannot both run on one workspace."""

    def __init__(self, home, workspace=None):
        self.home = Path(home)
        self.workspace = Path(workspace) if workspace else None
        self.lock_path = self.home / "live.lock"
        self.pid_path = self.home / "live.pid"
        self._handle = None
        self._ws_handle = None

    @staticmethod
    def _lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+")  # noqa: SIM115
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return None
        return handle

    def acquire(self) -> bool:
        handle = self._lock(self.lock_path)
        if handle is None:
            return False
        if self.workspace is not None:
            self._ws_handle = self._lock(self.workspace / "live" / "service.lock")
            if self._ws_handle is None:
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
        for name in ("_handle", "_ws_handle"):
            handle = getattr(self, name)
            if handle:
                with contextlib.suppress(OSError):
                    fcntl.flock(handle, fcntl.LOCK_UN)
                handle.close()
                setattr(self, name, None)

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
