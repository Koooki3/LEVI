"""Take up again the runs the live gate stopped.

In a live workspace a person's run (started in the LEVI page) stops at its next
model request while the robot's policy infers: the request fails as ``GpuBusy``
with ``gate="live"`` and the run is left ``blocked`` with ``blocked_by: gpu``
and ``blocked_gate: live`` (``levi/agent/runtime.py``). Nothing else resumed it
before; the person had to press Resume once the gate opened. This module does
that, in the core process, as a small daemon thread.

**Where.** The core is where the run lives (its store, its lease, its executor
threads), the same place ``inference.gpu.Watch`` resumes runs the GPU guardian
stopped, and the one that reads ``live/gate.json`` already for every request.
No new principal, no call through the dispatcher: the thread calls
``Workbench.launch``, exactly what a press on Resume ends in, so every check a
launch makes (an approved plan, a free lease, a draft not waiting to be
committed) still applies. The supervisor is not involved (it is another
process with no access to the run store, and it must stay free of anything
that can act on a person's run).

**What it may resume, and nothing else** (a whitelist, each point audited):

- the workspace has ``live/workspace.json`` (otherwise the thread never
  starts, and a tick returns at once);
- the run is ``blocked`` with ``blocked_by == "gpu"`` and ``blocked_gate ==
  "live"`` -- the gate's own mark. A model error, a failed validation, a
  teacher's pending decision, the GPU guardian's block and a person's pause or
  cancel (``paused``/``cancelled``) carry no such mark;
- the run was planned by a person (``local-human``, the principal of the LEVI
  page). The worker's runs (``live-auto``/``live-planner``) are the worker's
  business (it stands down and continues by itself), a connected agent's runs
  are its own, and the automatic approver never gets to resume a run it did
  not plan (``levi/live/auto.py``): this thread is not that principal;
- no control (pause/cancel) is pending, and a pilot episode still waiting for a
  person's review is not resumed (the same rule as the guardian's).

**When.** The gate file is fresh (the supervisor lives; ``gating.closed``'s
careful reading of a stale file is for letting a person through, not for
starting work unattended) and open, and it has stayed open for
``resume_stable_s`` (3 s by default) so a window that is about to close
(``episode_imminent``) is not used. Each run is resumed at most once per
opening of the gate. A run that was resumed and stopped again by the gate
``resume_max_bounces`` (3) times without finishing an episode in between is
left alone (``given_up`` in ``live/blocked_runs.json``, which the status
shows as ``blocked_runs``): a person presses Resume for it.

Every resume, every refusal to go on and every failure is a line in
``live/audit.jsonl`` (principal ``live-resume``).
"""

import logging
import threading
import time

from . import auto, config, gating, jsonio

LOG = logging.getLogger("levi.live.resumer")

PRINCIPAL_ID = "live-resume"
# The principal the LEVI page acts as (levi/agent/api.py): the runs a person
# planned and started. Nothing else is resumed by this module.
HUMAN_PRINCIPALS = frozenset({"local-human"})
TICK_S = 1.0
CONFLICT_TRIES = 5  # looks at a run whose launch is refused, per opening
FILE = "blocked_runs.json"
FILE_REFRESH_S = 5.0  # rewritten at least this often while runs are blocked
FILE_FRESH_S = 30.0  # the status ignores a file older than this

_PENDING: set = set()
_LOCK = threading.Lock()


def note_blocked(run_id):
    """A run in this process was just left blocked by the live gate. Cheap
    and harmless in any workspace: the thread that reads it exists only in a
    live one."""
    with _LOCK:
        _PENDING.add(run_id)


def _gate_snapshot(gate, open_for):
    return {
        "open": bool((gate or {}).get("open", True)),
        "code": (gate or {}).get("code"),
        "updated_at": (gate or {}).get("updated_at"),
        "open_for_s": None if open_for is None else round(open_for, 1),
    }


class GateResumer:
    """One per core process. ``tick`` is the whole logic (tests drive it with
    a fake clock); ``start`` runs it every second in a daemon thread."""

    def __init__(
        self,
        store,
        workbench,
        live_dir,
        *,
        stable_s=3.0,
        max_bounces=3,
        clock=time.monotonic,
        wall=time.time,
    ):
        self.store = store
        self.workbench = workbench
        self.live = live_dir
        self.stable_s = stable_s
        self.max_bounces = max_bounces
        self.clock = clock
        self.wall = wall
        self.open_since = None
        self.done: set = set()  # resumed (or tried) during this opening
        self.conflicts: dict = {}  # launches refused for a lease or a draft
        self.reported: set = set()  # given-up runs already audited
        self.pending: set = set()
        self.written = (None, 0.0)
        self.stop = threading.Event()
        self.thread = None

    # -- the gate -----------------------------------------------------------

    def gate(self):
        """The gate file when it says "go" *now*, else None: it must be fresh
        (a supervisor that is gone gates nobody and vouches for nothing) and
        open."""
        gate = jsonio.read(self.live / "gate.json")
        if not isinstance(gate, dict):
            return None
        if self.wall() - float(gate.get("updated_at") or 0) > gating.STALE_S:
            return None
        return gate if gate.get("open", True) else None

    # -- the runs -----------------------------------------------------------

    def scan(self):
        """Once, at start: the runs a previous core left blocked by the gate
        (the in-process notes are lost with it). One pass over the records,
        never repeated."""
        found = set()
        for run in self.store.list("runs"):
            if self._gated(run):
                found.add(run["id"])
        with _LOCK:
            _PENDING.update(found)

    @staticmethod
    def _gated(run):
        return (
            run.get("status") == "blocked"
            and run.get("blocked_by") == "gpu"
            and run.get("blocked_gate") == "live"
        )

    def _refresh(self):
        """The runs still blocked by the gate, by id (a few single reads)."""
        with _LOCK:
            ids = set(_PENDING) | self.pending
        keep = {}
        for run_id in ids:
            try:
                run = self.store.get("runs", run_id)
            except KeyError:
                run = None
            if run and self._gated(run):
                keep[run_id] = run
        with _LOCK:
            _PENDING.difference_update(ids - set(keep))
        self.pending = set(keep)
        return keep

    def _verdict(self, run):
        """Why this run is not resumed by us, or None when it may be."""
        if run.get("principal") not in HUMAN_PRINCIPALS:
            return "not_a_person_s_run"
        if run.get("control"):
            return "control_pending"
        plan = run.get("plan") or {}
        accepted = (plan.get("pilot_review") or {}).get("accepted")
        if not accepted and plan.get("pilot_episode") in set(run.get("completed", [])):
            return "pilot_awaits_review"
        record = run.get("live_resume") or {}
        done = len(run.get("completed", []))
        if done > record.get("completed", -1):
            return None  # it got somewhere since the last time
        if record.get("bounces", 0) >= self.max_bounces:
            return "given_up"
        return None

    # -- the tick -----------------------------------------------------------

    def tick(self):
        """One look: returns the run ids resumed now."""
        if not (self.live / auto.MARKER).is_file():
            return []
        now = self.clock()
        gate = self.gate()
        if gate is None:
            self.open_since = None
            self.done.clear()  # the next opening is a new one
            self.conflicts.clear()
        elif self.open_since is None:
            self.open_since = now
        runs = self._refresh()
        verdicts = {run_id: self._verdict(run) for run_id, run in runs.items()}
        self._report(runs, verdicts)
        if gate is None or now - self.open_since < self.stable_s:
            return []
        resumed = []
        for run_id, run in runs.items():
            if run_id in self.done or verdicts[run_id] == "not_a_person_s_run":
                continue
            if verdicts[run_id]:
                self._audit_skip(run_id, verdicts[run_id], run)
                continue
            if self._resume(run_id, run, gate, now - self.open_since):
                resumed.append(run_id)
            break  # one run per tick: a resume starts model requests
        return resumed

    def _resume(self, run_id, run, gate, open_for):
        from levi.agent.store import Conflict

        plan = run.get("plan") or {}
        accepted = (plan.get("pilot_review") or {}).get("accepted")
        record = run.get("live_resume") or {}
        done = len(run.get("completed", []))
        bounces = (
            1 + record.get("bounces", 0) if done <= record.get("completed", -1) else 1
        )
        base = {
            "tool": "runs.resume",
            "run_id": run_id,
            "reason": run.get("reason"),
            "gate": _gate_snapshot(gate, open_for),
            "bounces": bounces,
        }
        try:
            self.workbench.launch(run_id, pilot=not accepted)
        except Conflict as exc:
            # Usually the lease is still being let go of (the executor that
            # blocked the run has not finished): look again next tick, a few
            # times. A conflict that stays (an approved draft waiting to be
            # committed) is a person's to settle.
            tries = self.conflicts[run_id] = self.conflicts.get(run_id, 0) + 1
            if tries < CONFLICT_TRIES:
                return False
            self.done.add(run_id)
            self._audit({**base, "decision": "failed", "error": str(exc)[:300]})
            return False
        except Exception as exc:  # noqa: BLE001 - leave it blocked, say why
            self.done.add(run_id)
            self._audit({**base, "decision": "failed", "error": str(exc)[:300]})
            LOG.warning("live resume of %s failed: %s", run_id, exc)
            return False
        self.done.add(run_id)
        self.store.mutate(
            "runs",
            run_id,
            lambda r: r.update(
                live_resume={"bounces": bounces, "completed": done, "at": self.wall()}
            ),
        )
        self._audit({**base, "decision": "auto_resumed"})
        return True

    # -- what a person can see ------------------------------------------------

    def _audit(self, record):
        try:
            jsonio.append_line(
                self.live / auto.AUDIT,
                {"time": self.wall(), "principal": PRINCIPAL_ID, **record},
                max_bytes=auto.AUDIT_MAX_BYTES,
            )
        except Exception as exc:  # noqa: BLE001 - never fail the resume itself
            LOG.warning("live resume audit could not be written: %s", exc)

    def _audit_skip(self, run_id, why, run):
        if why == "given_up" and run_id not in self.reported:
            self.reported.add(run_id)
            self._audit(
                {
                    "tool": "runs.resume",
                    "run_id": run_id,
                    "decision": "given_up",
                    "reason": f"stopped by the live gate {self.max_bounces} times "
                    "without finishing an episode in between: a person presses Resume",
                    "bounces": (run.get("live_resume") or {}).get("bounces"),
                }
            )

    def _report(self, runs, verdicts):
        """``live/blocked_runs.json``: what the status shows as
        ``blocked_runs``. Written when it changes, and now and then while runs
        are blocked; one last time (empty) when the last one goes."""
        rows = [
            {"id": run_id, "auto": verdicts[run_id] is None, "why": verdicts[run_id]}
            for run_id in sorted(runs)
        ]
        now = self.wall()
        if rows == self.written[0] and (
            not rows or now - self.written[1] < FILE_REFRESH_S
        ):
            return
        self.written = (rows, now)
        jsonio.write(self.live / FILE, {"updated_at": now, "runs": rows})

    # -- the thread -----------------------------------------------------------

    def run(self):
        while not self.stop.wait(TICK_S):
            try:
                self.tick()
            except Exception:
                LOG.exception("live gate resumer tick failed")

    def start(self):
        self.thread = threading.Thread(
            target=self.run, name="live-gate-resumer", daemon=True
        )
        self.thread.start()
        return self

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=5)


def read_blocked(live_dir, now=None):
    """The summary the status shows: ``{count, auto, manual}`` (ids), or zeros
    when there is no fresh file."""
    now = time.time() if now is None else now
    value = jsonio.read(live_dir / FILE) or {}
    rows = value.get("runs") or []
    if now - float(value.get("updated_at") or 0) > FILE_FRESH_S:
        rows = []
    return {
        "count": len(rows),
        "waiting": [r["id"] for r in rows if r.get("auto")],
        "needs_person": [r["id"] for r in rows if not r.get("auto")],
    }


def start(store, workbench, root):
    """Start the thread in a live workspace; None (and nothing running) in any
    other. Configuration comes from the file the supervisor wrote."""
    live = root / "live"
    if not (live / auto.MARKER).is_file():
        return None
    settings = config.Gpu()
    written = next(
        (p for p in (live / "effective.toml", root / "live.toml") if p.is_file()), None
    )
    try:
        settings = config.load(written).gpu
    except (ValueError, OSError):
        pass
    resumer = GateResumer(
        store,
        workbench,
        live,
        stable_s=settings.resume_stable_s,
        max_bounces=settings.resume_max_bounces,
    )
    try:
        resumer.scan()
    except Exception:
        LOG.exception("live gate resumer: the first scan failed")
    return resumer.start()
