"""In-process stand-ins for the FR3 robot, the policy server and the
rollout recorder, for the automatic pipeline (AERI) tests.

Deterministic: one ``FakeClock`` that only moves when someone advances it,
fault scripts keyed by command or step numbers, no randomness unless a test
passes a seeded ``random.Random``. Nothing here opens a socket, starts a
process, imports a robot SDK or touches the GPU. Every robot command is
counted, so a test can prove that a command was never sent (or sent once).

Fidelity: these fakes are not the real robot. ``FIDELITY`` lists what they
do differently; nothing tested against them counts as verified on the real
robot.
"""

import math
import queue
import threading
from dataclasses import dataclass, field

FIDELITY = (
    (
        "FakeRobot reaches the commanded pose at once: no dynamics, no stopping "
        "distance after hold, no tracking error, no reflex."
    ),
    (
        "FakeRobot's home ends inside the tolerance unless a script says "
        "otherwise; the real `_go_home` path does not check where it ended."
    ),
    (
        "An emergency stop is visible to FakeRobot (red light, latch); on the "
        "real FR3 the software staleness interlock does NOT see an e-stop "
        "(franka_control §9.4, not passed)."
    ),
    (
        "FakeRobot's latch imitates openpi's Fr3Guard counters (3 x 503, 6 "
        "stale states) in a simplified form; the frozen-pose and frozen-camera "
        "detectors are not modelled byte for byte."
    ),
    (
        "Health is read in the same step as the command; the real C3 file is "
        "written at 2 Hz and may be up to 3 s old."
    ),
    (
        "FakePolicy answers after a fixed latency on the fake clock: no GPU "
        "contention, no cold start, no websocket."
    ),
    (
        "FakeRecorder keeps steps in memory and writes no video; it does not "
        "produce files that `criteria.check` reads. The recorder layer "
        "(levi/automatic/recorder.py) writes real rollout folders, but its "
        "default media sink writes no video: the frame-count and camera-stall "
        "facts it seals are those of a sink, not of real cameras."
    ),
    "Camera frames are counters; a stall repeats the counter, no pixels.",
    (
        "The scene and goal fakes answer by script: a passing dry run or test "
        "says nothing about how often a real model is right."
    ),
    (
        "Session files (C2) are rewritten at each state change; nothing drives "
        "a 2 s heartbeat during a long wait as the real client does."
    ),
    (
        "There is no network: 503s, timeouts and dropped replies are scripted, "
        "never produced by an HTTP stack."
    ),
)


class FakeClock:
    """A monotonic nanosecond clock that moves only when advanced."""

    def __init__(
        self, start_ns: int = 1_000_000_000, domain: str = "host-mono:fake-boot"
    ):
        self._now = int(start_ns)
        self.domain = domain
        self._lock = threading.Lock()

    def __call__(self) -> int:
        return self.now()

    def now(self) -> int:
        with self._lock:
            return self._now

    def advance(self, ns: int) -> int:
        if ns < 0:
            raise ValueError("a monotonic clock does not go back")
        with self._lock:
            self._now += int(ns)
            return self._now


class FaultScript:
    """``{number: [injection, ...]}``: what goes wrong at the Nth call of a
    kind (counted from 0). Unused entries stay visible in ``pending``."""

    def __init__(self, faults=None):
        self._faults = {}
        for key, value in (faults or {}).items():
            self._faults[int(key)] = list(
                value if isinstance(value, list | tuple) else [value]
            )

    def take(self, number: int) -> list:
        return self._faults.pop(number, [])

    @property
    def pending(self) -> dict:
        return dict(self._faults)


# --- the robot ------------------------------------------------------------------------


@dataclass
class StepResult:
    executed: str  # yes | no | unknown
    detail: str
    step: int


@dataclass
class HomeResult:
    executed: str
    detail: str
    error_mm: float | None


class SimulatedCrash(BaseException):
    """Raised by a fake to stand in for the orchestrator process dying."""


HOME_POSE = (0.45, 0.0, 0.40, 0.0, 1.0, 0.0, 0.0)


class FakeRobot:
    """The only fake with motion: ``step`` and ``home`` ask ``authorize``
    (the orchestrator's motion fence) first and refuse without a valid token,
    so a late thread or a stale chunk cannot move it.

    Step faults (keyed by step command number): ``http_503``, ``stale``
    (state not fresh), ``frozen`` (pose stops changing), ``red_light``,
    ``latch``, ``no_reply`` (sent, no answer: executed unknown),
    ``crash_after_send`` (sent, then the orchestrator dies). Home faults
    (keyed by home number): ``home_miss`` (ends outside the tolerance:
    executed no), ``no_reply``, ``crash_after_send``. Observation faults
    (keyed by observation number): ``camera_stall`` (the frame counters stop
    for ``stall_frames`` observations)."""

    LATCH_503 = 3
    LATCH_STALE = 6
    HOME_TOLERANCE_MM = 5.0

    def __init__(
        self,
        clock: FakeClock,
        authorize,
        *,
        step_faults=None,
        home_faults=None,
        observe_faults=None,
        preflight_ok=True,
        action_dims=7,
        stall_frames=40,
    ):
        self.clock = clock
        self.authorize = authorize  # (token, kind, now_ns) -> None or raises
        self.steps = FaultScript(step_faults)
        self.homes = FaultScript(home_faults)
        self.observes = FaultScript(observe_faults)
        self.preflight_ok = preflight_ok
        self.action_dims = action_dims
        self.stall_frames = stall_frames
        self.pose = list(HOME_POSE)
        self.latched: str | None = None
        self.red_light = False
        self.motions: list = []  # (kind, episode_id, token transaction) sent
        self.refused: list = []  # (kind, code) refused before sending
        self.step_calls = 0
        self.home_calls = 0
        self.observe_calls = 0
        self.holds = 0
        # The admission gate, as the real bridge keeps it: "open" (default),
        # "closed" (stop_admission), "home_only" (inside home_under_closed_gate).
        self.admission = "open"
        self.gate_windows = 0  # home-only windows opened
        self.gate_refused: list = []  # why home_under_closed_gate said no
        self._503 = 0
        self._stale = 0
        self._frozen = False
        self._stall_left = 0
        self._frame = 0
        self._lock = threading.Lock()

    # read-only
    def preflight(self) -> dict:
        reasons = []
        if not self.preflight_ok:
            reasons.append("preflight_scripted_failure")
        if self.latched:
            reasons.append(f"latched:{self.latched}")
        if self.red_light:
            reasons.append("red_light")
        return {"ok": not reasons, "reasons": reasons}

    def health(self) -> dict:
        if self.red_light:
            return {"state": "red", "reasons": ["red_light"]}
        return {"state": "ok", "reasons": []}

    def latch_state(self) -> str | None:
        return self.latched

    def observe(self) -> dict:
        with self._lock:
            number = self.observe_calls
            self.observe_calls += 1
            for fault in self.observes.take(number):
                if fault == "camera_stall":
                    self._stall_left = self.stall_frames
            if self._stall_left > 0:
                self._stall_left -= 1
            else:
                self._frame += 1
            return {
                "observed_ns": self.clock.now(),
                "pose": list(self.pose),
                "frames": {"side": self._frame, "wrist": self._frame},
                "state_fresh": True,
            }

    def hold(self) -> None:
        """Stop sending: nothing to undo in the fake (the real arm may drift)."""
        with self._lock:
            self.holds += 1

    def _latch(self, why: str) -> None:
        self.latched = self.latched or why

    def stop_admission(self) -> None:
        """Close the gate (a pause); a home-only window ends at once."""
        self.admission = "closed"

    def reopen_admission(self) -> None:
        """Reopen the gate (a resume); refused inside a home-only window."""
        if self.admission == "home_only":
            raise RuntimeError("home-only window open: admission is not reopened")
        self.admission = "open"

    def step(self, action, *, token) -> StepResult:
        with self._lock:
            number = self.step_calls
            self.step_calls += 1
            faults = self.steps.take(number)
            try:
                self.authorize(token, "policy_steps", self.clock.now())
            except Exception as exc:  # noqa: BLE001 - the fence's refusal
                self.refused.append(("policy_steps", getattr(exc, "code", str(exc))))
                return StepResult("no", "token_refused", number)
            if self.admission != "open":
                self.refused.append(("policy_steps", "admission_" + self.admission))
                return StepResult("no", "admission_" + self.admission, number)
            if "red_light" in faults:
                self.red_light = True
                self._latch("red_light")
            if "latch" in faults:
                self._latch("scripted")
            if self.latched:
                self.refused.append(("policy_steps", "latched"))
                return StepResult("no", "latched", number)
            if len(action) != self.action_dims or not all(
                isinstance(x, int | float) and math.isfinite(x) for x in action
            ):
                self.refused.append(("policy_steps", "bad_action"))
                return StepResult("no", "bad_action", number)
            if "http_503" in faults:
                self._503 += 1
                if self._503 >= self.LATCH_503:
                    self._latch("http_503")
                return StepResult("no", "http_503", number)
            self._503 = 0
            if "stale" in faults:
                self._stale += 1
                if self._stale >= self.LATCH_STALE:
                    self._latch("state_stale")
            else:
                self._stale = 0
            if "frozen" in faults:
                self._frozen = True
            self.motions.append(
                ("policy_steps", token.episode_id, token.transaction_id)
            )
            if not self._frozen:
                self.pose = list(action)
            if "crash_after_send" in faults:
                raise SimulatedCrash("crash after a step was sent")
            if "no_reply" in faults:
                return StepResult("unknown", "no_reply", number)
            return StepResult("yes", "ok", number)

    def home(self, *, token) -> HomeResult:
        return self._home(token, gated=False)

    def home_under_closed_gate(self, *, token) -> HomeResult:
        """The home that runs while admission is closed: it needs the closed
        mode, a current home token, no latch and no red light; it opens a
        home-only window for its own duration and always leaves it closed."""
        return self._home(token, gated=True)

    def _home(self, token, *, gated: bool) -> HomeResult:
        with self._lock:
            number = self.home_calls
            self.home_calls += 1
            faults = self.homes.take(number)
            try:
                self.authorize(token, "home", self.clock.now())
            except Exception as exc:  # noqa: BLE001
                self.refused.append(("home", getattr(exc, "code", str(exc))))
                if gated:
                    self.gate_refused.append("token_refused")
                return HomeResult("no", "token_refused", None)
            # The old home needs an open gate (the real bridge refuses it on a
            # closed one); the gated home needs a closed gate.
            if self.admission != ("closed" if gated else "open"):
                self.refused.append(("home", "admission_" + self.admission))
                if gated:
                    self.gate_refused.append("admission_" + self.admission)
                return HomeResult("no", "admission_" + self.admission, None)
            if self.latched or (gated and self.red_light):
                self.refused.append(("home", "latched"))
                if gated:
                    self.gate_refused.append("latched")
                return HomeResult("no", "latched", None)
            if gated:
                self.admission = "home_only"
                self.gate_windows += 1
            try:
                return self._home_motion(token, faults)
            finally:
                if gated:
                    self.admission = "closed"

    def _home_motion(self, token, faults) -> HomeResult:
        self.motions.append(("home", token.episode_id, token.transaction_id))
        if "crash_after_send" in faults:
            raise SimulatedCrash("crash after home was sent")
        if "no_reply" in faults:
            return HomeResult("unknown", "no_reply", None)
        if "home_miss" in faults:
            return HomeResult("no", "home_out_of_tolerance", 12.0)
        self.pose = list(HOME_POSE)
        return HomeResult("yes", "ok", 0.4)


class GateFakeRobot(FakeRobot):
    """FakeRobot that also keeps the bridge's admission-gate record: generation,
    closure time, pose counters and a snapshot, and a specific reopen of one named
    interval that raises the bridge's refusal type. A closed-gate home is a
    home-only window as in ``Robot.home_under_closed_gate``: entering is +1, a stop
    inside it is +1 and starts the closure at the stop, leaving a gate that is not
    yet closed is +1 with the leaving time; so a window ends as a new closed
    interval (generation +2). For the tests of the FR3 intervention hooks; the
    plain FakeRobot has no snapshot, and tests rely on that."""

    def __init__(self, *args, snapshot_type, refused_type, **kwargs):
        # The snapshot and refusal classes are injected (fr3_bridge.AdmissionSnapshot
        # and AdmissionRefused): this module imports nothing from levi.
        super().__init__(*args, **kwargs)
        self._snapshot_type = snapshot_type
        self._refused_type = refused_type
        self._gate_lock = threading.Lock()
        self._generation = 0
        self._closed_from = None
        self._pose_attempts = self._pose_rejected = self._home_count = 0
        self._home_only_from = None

    def stop_admission(self):
        with self._gate_lock:
            if self.admission != "closed":
                super().stop_admission()
                self._generation += 1
                self._closed_from = self.clock.now()
        return self.admission_snapshot()

    def reopen_admission(self) -> None:
        was = self.admission
        super().reopen_admission()  # refused inside a home-only window
        with self._gate_lock:
            if was == "closed":
                self._generation += 1
                self._closed_from = None

    def reopen_admission_if(self, *, generation, closed_from_ns) -> None:
        with self._gate_lock:
            if (
                self.admission != "closed"
                or type(generation) is not int
                or type(closed_from_ns) is not int
                or self._generation != generation
                or self._closed_from != closed_from_ns
            ):
                raise self._refused_type("admission is not the closed interval named")
            self.admission = "open"
            self._generation += 1
            self._closed_from = None

    def admission_snapshot(self):
        return self._snapshot_type(
            self.admission == "closed",
            self._generation,
            self._closed_from,
            self.clock.now(),
            self.clock.domain,
            self._pose_attempts,
            self._pose_rejected,
            0,
            0,
            self.admission,
            self._home_only_from,
            self._home_count,
            0,
            0,
        )

    def step(self, action, *, token) -> StepResult:
        result = super().step(action, token=token)
        with self._gate_lock:
            self._pose_attempts += 1
            if result.detail.startswith("admission_"):
                self._pose_rejected += 1
        return result

    def _home_motion(self, token, faults) -> HomeResult:
        if self.admission == "home_only":  # the window opened: entering is +1
            with self._gate_lock:
                self._generation += 1
                self._closed_from = None
                self._home_only_from = self.clock.now()
                self._home_count += 1  # the home's commands pass the window
        return super()._home_motion(token, faults)

    def _home(self, token, *, gated: bool) -> HomeResult:
        windows = self.gate_windows
        try:
            return super()._home(token, gated=gated)
        finally:
            if gated and self.gate_windows != windows:
                with self._gate_lock:
                    # Leaving: a stop inside the window closed it already (keep
                    # that interval); otherwise a new closure starts now.
                    if self._closed_from is None:
                        self._generation += 1
                        self._closed_from = self.clock.now()


# --- the policy server ------------------------------------------------------------------


@dataclass
class _Ticket:
    request: dict
    ready_ns: int
    faults: list
    number: int


class FakePolicy:
    """Policy serving plus the chunk broker, in process. Chunks are
    ``levi.aeri.runtime.v1`` ``chunk_response`` dicts without ``received_ns``
    (the receiver fills it from its own clock).

    Chunk faults (keyed by request number): ``timeout`` (never answers),
    ``server_error``, ``nan``, ``wrong_dims``, ``wrong_epoch``,
    ``valid_from_early``, ``slow`` (answers after ``slow_ns``). Other
    switches: ``acquire_fails``, ``quiesce_fails``, ``crashed`` (every call
    errors from then on)."""

    def __init__(
        self,
        clock: FakeClock,
        *,
        latency_ns=40_000_000,
        horizon=8,
        action_dims=7,
        chunk_faults=None,
        acquire_fails=False,
        quiesce_fails=False,
        slow_ns=2_000_000_000,
        rng=None,
    ):
        self.clock = clock
        self.latency_ns = latency_ns
        self.horizon = horizon
        self.action_dims = action_dims
        self.faults = FaultScript(chunk_faults)
        self.acquire_fails = acquire_fails
        self.quiesce_fails = quiesce_fails
        self.crashed = False
        self.slow_ns = slow_ns
        self.rng = rng
        self.requests = 0
        self.fenced_epoch = -1
        self.inflight: dict[str, _Ticket] = {}
        self.acquired: list = []
        # The prompt of the episode acquired last (None: the run's fixed one),
        # and (request_id, prompt) of every chunk request.
        self.prompt = None
        self.prompts: list = []
        self._lock = threading.Lock()

    def acquire(self, role: str, *, policy_epoch: int, prompt: str | None = None):
        with self._lock:
            if self.acquire_fails or self.crashed:
                return {"kind": "unavailable", "code": "server_error"}
            handle = {
                "handle_id": f"h-{role}-{policy_epoch}",
                "role": role,
                "policy_epoch": policy_epoch,
                "prompt": prompt,
            }
            self.prompt = prompt
            self.acquired.append(handle)
            return handle

    def request(self, req: dict):
        with self._lock:
            number = self.requests
            self.requests += 1
            self.prompts.append((req.get("request_id"), self.prompt))
            if self.crashed:
                return {"kind": "unavailable", "code": "server_error"}
            faults = self.faults.take(number)
            if "crash" in faults:
                self.crashed = True
                return {"kind": "unavailable", "code": "server_error"}
            delay = self.slow_ns if "slow" in faults else self.latency_ns
            ticket = _Ticket(dict(req), self.clock.now() + delay, faults, number)
            self.inflight[req["request_id"]] = ticket
            return req["request_id"]

    def _actions(self, start: int):
        rows = []
        for i in range(self.horizon):
            base = [0.45 + 0.001 * (start + i), 0.0, 0.40, 0.0, 1.0, 0.0, 0.0]
            if self.rng is not None:
                base[1] = round(self.rng.uniform(-0.01, 0.01), 6)
            rows.append(base[: self.action_dims] + [0.0] * (self.action_dims - 7))
        return rows

    def collect(self, ticket_id: str, *, timeout_ns: int):
        """The response, ``None`` when it is not there within ``timeout_ns``
        (the fake clock is advanced by the wait), or an unavailable dict."""
        with self._lock:
            ticket = self.inflight.get(ticket_id)
        if ticket is None:
            return {"kind": "unavailable", "code": "cancelled"}
        if self.crashed or "server_error" in ticket.faults:
            with self._lock:
                self.inflight.pop(ticket_id, None)
            return {"kind": "unavailable", "code": "server_error"}
        now = self.clock.now()
        if "timeout" in ticket.faults or ticket.ready_ns > now + timeout_ns:
            self.clock.advance(timeout_ns)
            return None
        if ticket.ready_ns > now:
            self.clock.advance(ticket.ready_ns - now)
        with self._lock:
            self.inflight.pop(ticket_id, None)
        return self.response(ticket)

    def response(self, ticket: _Ticket) -> dict:
        req = ticket.request
        actions = self._actions(req["action_start_index"])
        if "nan" in ticket.faults:
            actions[0][0] = float("nan")
        if "wrong_dims" in ticket.faults:
            actions = [row[:-1] for row in actions]
        epoch = req["policy_epoch"] + (1 if "wrong_epoch" in ticket.faults else 0)
        start = req["action_start_index"]
        valid_from = max(0, start - 1) if "valid_from_early" in ticket.faults else start
        return {
            "schema": "levi.aeri.runtime.v1",
            "minor": 0,
            "run_id": req["run_id"],
            "emitted_wall_ns": 0,
            "kind": "chunk_response",
            "request_id": req["request_id"],
            "episode_id": req["episode_id"],
            "policy_epoch": epoch,
            "action_contract": "fr3-robotiq@1",
            "actions": actions,
            "valid_from_action_index": valid_from,
            "prefix_conditioning_applied": "none",
            "model_version": {"config": "fake", "checkpoint_dir_name": "fake-ckpt"},
            "timing": {"total_ms": self.latency_ns // 1_000_000},
            "clock_domain": req["clock_domain"],
        }

    def late_responses(self):
        """Answers to requests still in flight, delivered anyway (as a slow
        server would after a fence): the receiver must drop them."""
        with self._lock:
            tickets = list(self.inflight.values())
            self.inflight.clear()
        return [self.response(t) for t in tickets]

    def quiesce(self, *, policy_epoch: int) -> dict:
        with self._lock:
            cancelled = len(self.inflight)
            self.inflight.clear()
            self.fenced_epoch = max(self.fenced_epoch, policy_epoch)
            ok = not (self.quiesce_fails or self.crashed)
            return {
                "ok": ok,
                "fenced_epoch": policy_epoch,
                "inflight_cancelled": cancelled,
            }


# --- the recorder --------------------------------------------------------------------


@dataclass
class FakeRollout:
    task_folder: str
    demo: str
    episode_id: str
    staged: int = 0
    written: int = 0
    state: str = "open"  # open | complete | incomplete
    complete_marker: bool = False
    errors: list = field(default_factory=list)


class RecorderError(Exception):
    pass


class FakeRecorder:
    """A recorder with a writer thread, like the real one's video writers:
    ``commit`` queues a step, the thread "writes" it. A write failure in the
    thread is reported at the next ``commit`` or at ``seal``; ``seal`` waits
    for the queue and creates the ``.complete`` marker last, only when every
    write succeeded; ``abort`` renames to ``incomplete_*`` without a marker.

    Faults (keyed by write number across the recorder): ``disk_full``
    (the write fails). ``open_fails`` refuses to open."""

    def __init__(self, *, write_faults=None, open_fails=False, threaded=True):
        self.faults = FaultScript(write_faults)
        self.open_fails = open_fails
        self.threaded = threaded
        self.rollouts: dict[str, FakeRollout] = {}
        self.writes = 0
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._thread = None
        self._numbers: dict[str, int] = {}
        # episode id -> what the orchestrator said of it (subtask_loop).
        self.described: dict[str, dict] = {}
        if threaded:
            self._thread = threading.Thread(
                target=self._writer, name="fake-recorder", daemon=True
            )
            self._thread.start()

    def close(self) -> None:
        if self._thread is not None:
            self._queue.put(None)
            self._thread.join(timeout=5)
            self._thread = None

    def _writer(self):
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                self._write(item)
            finally:
                self._queue.task_done()

    def _write(self, rollout: FakeRollout):
        with self._lock:
            number = self.writes
            self.writes += 1
            if "disk_full" in self.faults.take(number):
                rollout.errors.append(f"write {number}: no space left on device")
                return
            rollout.written += 1

    def describe_episode(self, episode_id: str, info: dict) -> None:
        self.described[episode_id] = dict(info)

    def open(self, *, task_folder: str, episode_id: str, number: int) -> FakeRollout:
        if self.open_fails:
            raise RecorderError("cannot open the rollout folder")
        demo = f"demo_{number:04d}"
        with self._lock:
            key = f"{task_folder}/{demo}"
            if key in self.rollouts:
                raise RecorderError(f"{key} exists")
            rollout = FakeRollout(task_folder, demo, episode_id)
            self.rollouts[key] = rollout
            return rollout

    def stage(self, rollout: FakeRollout, obs) -> None:
        self._raise(rollout)
        rollout.staged += 1

    def commit(self, rollout: FakeRollout, result) -> None:
        self._raise(rollout)
        if self.threaded:
            self._queue.put(rollout)
        else:
            self._write(rollout)

    def _raise(self, rollout):
        if rollout.state != "open":
            raise RecorderError(f"{rollout.demo} is {rollout.state}")
        if rollout.errors:
            raise RecorderError(rollout.errors[0])

    def seal(self, rollout: FakeRollout, meta: dict) -> dict:
        if rollout.state == "complete":
            return {"sealed": "complete", "demo": rollout.demo}  # idempotent
        if self.threaded:
            self._queue.join()
        if rollout.errors:
            raise RecorderError(rollout.errors[0])
        rollout.state = "complete"
        rollout.complete_marker = True  # last, after every write
        return {"sealed": "complete", "demo": rollout.demo}

    def abort(self, rollout: FakeRollout, reason: str) -> dict:
        if self.threaded:
            self._queue.join()
        if rollout.state == "open":
            rollout.state = "incomplete"
            rollout.demo = rollout.demo.replace("demo_", "incomplete_")
        return {"sealed": "incomplete", "demo": rollout.demo}
