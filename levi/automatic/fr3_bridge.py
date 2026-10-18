"""FR3 adapter over the existing guarded openpi client. Imports do not connect.

Only the separate operator entry point constructs hardware. The launch runner's
real-mode refusal stays in place. Every command is fenced, including each
interpolated home step; uncertain delivery locks motion until a fresh process.
"""

import hashlib
import json
import math
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from levi.automatic import camera_meta
from levi.automatic.state_machine import MotionRefused
from levi.domain import aeri

# Home verification thresholds, unchanged since the reviewed bridge. The
# position tolerance is the binding's ``home_tolerance_mm``.
HOME_ROTATION_RAD = 0.05
HOME_GRIPPER_MIN = 0.9  # gripper_position >= this counts as open

# Optional binding.safety keys (L2-REAL-HOME). Absent keys use these defaults;
# they bound a wait, they never relax a threshold.
DEFAULT_HOME_SETTLE_TIMEOUT_S = 5.0  # total wait for the arm to settle at home
DEFAULT_SETTLE_POLL_S = 0.1  # interval between two readings while waiting
DEFAULT_POLICY_WARMUP_TIMEOUT_S = 30.0
DEFAULT_GRIPPER_CONFIRM_TIMEOUT_S = 2.0  # wait to confirm a queued gripper reply
GRIPPER_STABLE_DELTA = 0.02  # two readings this close count as a steady gripper


class AdmissionRefused(ValueError):
    """A pose/gripper POST was refused locally because admission is closed."""


# Admission gate modes. ``home_only`` exists only inside
# ``Robot.home_under_closed_gate``: the gate stays shut for every policy command
# and passes just the routes a home needs, under the home token of one
# transaction.
GATE_OPEN, GATE_CLOSED, GATE_HOME_ONLY = "open", "closed", "home_only"
HOME_ONLY_ROUTES = frozenset({"pose", "clearerr", "open_gripper"})


# Command families the admission gate counts. clearerr is only issued as part of
# one pose step, so it is counted with the pose family.
ADMISSION_FAMILY = {
    "pose": "pose",
    "clearerr": "pose",
    "open_gripper": "gripper",
    "close_gripper": "gripper",
    "close_gripper_slow": "gripper",
    "move_gripper": "gripper",
}


@dataclass(frozen=True)
class AdmissionSnapshot:
    """Atomic view of the admission gate and its monotonic command counters.

    ``*_attempts`` counts every POST that reached the gate, ``*_gate_rejected``
    those the closed gate refused locally, so ``*_released`` is the number that
    passed the gate (an upper bound of what was sent: the fence or the server may
    still have refused it later). A released command is counted when it STARTS;
    nothing here says it was delivered, finished, or that the arm stopped moving.
    ``generation`` increments on every transition of the gate's mode.

    ``mode`` is "open", "closed" or "home_only". ``closed`` is true only in the
    "closed" mode, so a consumer that reads just ``closed`` takes a home-only
    window for "not closed". The ``home_*`` counters belong to home-only windows
    and never touch the pose/gripper counters above: ``home_*_released``
    commands passed the window's whitelist when they STARTED (nothing says they
    were delivered or that the arm arrived), ``home_gate_rejected`` were refused
    inside a window. ``home_only_from_ns`` is the start of the latest window.
    """

    closed: bool
    generation: int
    closed_from_ns: int | None
    read_ns: int
    clock_domain: str
    pose_attempts: int
    pose_gate_rejected: int
    gripper_attempts: int
    gripper_gate_rejected: int
    mode: str = GATE_OPEN
    home_only_from_ns: int | None = None
    home_pose_released: int = 0
    home_gripper_released: int = 0
    home_gate_rejected: int = 0

    @property
    def pose_released(self):
        return self.pose_attempts - self.pose_gate_rejected

    @property
    def gripper_released(self):
        return self.gripper_attempts - self.gripper_gate_rejected


@dataclass(frozen=True)
class StepResult:
    executed: str
    detail: str
    step: int = 0
    notes: tuple = ()  # (code, text) facts for the journal, raw text included


@dataclass(frozen=True)
class HomeResult:
    executed: str
    detail: str
    error_mm: float | None = None
    notes: tuple = ()


def _terminal(text):
    print(text, file=sys.stderr, flush=True)


class Clock:
    def __init__(self):
        self.domain = aeri.host_clock_domain()

    def now(self):
        return time.monotonic_ns()

    def __call__(self):
        return self.now()

    def advance(self, ns):
        time.sleep(max(0, ns) / 1e9)


def checkpoint_path(value):
    if not isinstance(value, str) or not os.path.isabs(value):
        raise ValueError("deployment.checkpoint_dir must be absolute")
    return os.path.realpath(value)


def check_identity(expected, metadata):
    if not isinstance(metadata, dict):
        raise TypeError("policy metadata unavailable")
    if expected["config"] != metadata.get("config") or checkpoint_path(
        expected["checkpoint_dir"]
    ) != checkpoint_path(metadata.get("checkpoint_dir")):
        raise ValueError("policy deployment identity mismatch")


def acknowledged(route, response):
    """Only the reviewed server's explicit success shapes establish delivery."""
    if isinstance(response, dict):
        return (
            (response.get("ok") is True or response.get("success") is True)
            and not response.get("error")
            and response.get("ok") is not False
            and response.get("success") is not False
        )
    known = {
        "clearerr": {"Clear"},
        "pose": {"Moved"},
        "open_gripper": {"Opened", "open gripper target already satisfied"},
        "close_gripper": {"Closed", "close gripper target already satisfied"},
        "close_gripper_slow": {"Closed", "close gripper target already satisfied"},
        "move_gripper": {"Moved Gripper"},
    }
    return isinstance(response, str) and response in known.get(route, set())


# The only non-success gripper replies that are not failures: the reviewed
# server (robotiq_gripper_server_ros2.py _send_width) accepted the request but
# it is still running ("in_flight"), is held behind an opposite goal
# ("retained") or was merged into a recent identical one ("debounced"). HTTP 200
# with exactly this text, for the route's own kind. Delivery is not proven by
# the text; the measured gripper position must confirm it before the next motion.
_GRIPPER_KIND = {
    "open_gripper": "open",
    "close_gripper": "close",
    "close_gripper_slow": "close",
}
_QUEUE_TEXTS = {
    "goal already in flight": "in_flight",
    "target retained while busy": "retained",
    "request debounced": "debounced",
}


def reply_status(route, response):
    """ "ack" (explicit success), "pending" (reviewed queue text, needs a
    measurement) or "bad" (negative, unknown or malformed: lock)."""
    if acknowledged(route, response):
        return "ack"
    kind = _GRIPPER_KIND.get(route)
    if kind is not None and isinstance(response, str):
        for tail in _QUEUE_TEXTS:
            if response == f"{kind} gripper {tail}":
                return "pending"
    return "bad"


class Robot:
    """Requires Fr3Guard.attach(env) before construction; never calls reset()."""

    def __init__(
        self,
        env,
        guard,
        client_module,
        args,
        clock,
        fence,
        *,
        tolerance_mm,
        home_settle_timeout_s=DEFAULT_HOME_SETTLE_TIMEOUT_S,
        settle_poll_s=DEFAULT_SETTLE_POLL_S,
        gripper_confirm_timeout_s=DEFAULT_GRIPPER_CONFIRM_TIMEOUT_S,
        emit=_terminal,
        camera_stale_steps=camera_meta.DEFAULT_STALE_STEPS,
        require_image_meta=False,
    ):
        self.env, self.guard, self.client = env, guard, client_module
        # Frame bus (CAMERA-FRAME-BUS.md): the orchestrator's control loop
        # reads this limit for its same-seq check; with max_frame_age_ms
        # configured the observation must carry image_meta (an older openpi
        # ignores the setting silently, so it is refused, never trusted).
        self.camera_stale_steps = camera_meta.stale_steps(camera_stale_steps)
        self.require_image_meta = require_image_meta is True
        self.args, self.clock, self.fence = args, clock, fence
        self.tolerance_mm = tolerance_mm
        for name, value in (
            ("home_settle_timeout_s", home_settle_timeout_s),
            ("settle_poll_s", settle_poll_s),
            ("gripper_confirm_timeout_s", gripper_confirm_timeout_s),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(name + " must be positive finite")
        self.home_settle_timeout_s = float(home_settle_timeout_s)
        self.settle_poll_s = float(settle_poll_s)
        self.gripper_confirm_timeout_s = float(gripper_confirm_timeout_s)
        self._grip_queued = None  # set by the reply that arrived during a step
        self._grip_pending = None  # a queued reply awaiting its measurement
        self._step_notes = []
        self.emit = emit
        self.raw = None
        self.latched = None
        self.held = False
        self.steps = 0
        self.stop_event = threading.Event()
        self.stop_probe = lambda: False
        self._sending = False
        self._motion = None
        self._http_acks = []
        self._state_generation = 0
        self._gripper_measurement = None
        # Admission gate (opt-in): open by default, so legacy behaviour is
        # unchanged. Gate state and counters change only under this lock, which
        # is also held while a command passes the gate.
        self._admission_lock = threading.Lock()
        self._admission_mode = GATE_OPEN
        self._admission_generation = 0
        self._admission_closed_from = None
        self._admission_counts = {
            family: {"attempts": 0, "rejected": 0} for family in ("pose", "gripper")
        }
        # Home-only windows: their own counters, token and start time.
        self._home_only_from = None
        self._home_gate_token = None
        self._home_thread = None  # the thread that runs the window's home
        self._home_active = False  # a home_under_closed_gate call is in progress
        self._home_marker = None  # identifies the call that opened the window
        self._motion_posts = (
            0  # motion POSTs that passed the fence (a count, not a flag)
        )
        self._home_counts = {"pose": 0, "gripper": 0, "rejected": 0}
        original_post = env._post

        def fenced_post(route, payload=None):
            family = ADMISSION_FAMILY.get(route)
            if family is not None:
                with self._admission_lock:
                    if self._admission_mode == GATE_HOME_ONLY:
                        # Only the whitelisted routes, only while the home's own
                        # send path holds the window's home token.
                        if route in HOME_ONLY_ROUTES and self._home_motion_current():
                            self._home_counts[family] += 1
                        else:
                            self._home_counts["rejected"] += 1
                            raise AdmissionRefused(
                                "home-only admission: no " + route + " POST was sent"
                            )
                    else:
                        counts = self._admission_counts[family]
                        counts["attempts"] += 1
                        if self._admission_mode == GATE_CLOSED:
                            counts["rejected"] += 1
                            raise AdmissionRefused(
                                "admission closed: no " + route + " POST was sent"
                            )
            if route in (
                "pose",
                "open_gripper",
                "close_gripper",
                "close_gripper_slow",
                "move_gripper",
                "clearerr",
            ):
                if self._motion is None or self.stopped() or self.latch_state():
                    raise ValueError("motion HTTP route has no authority")
                token, kind = self._motion
                self.fence.check(token, kind, self.clock.now())
                # Not under the admission lock: a concurrent step() can only make the
                # count lag or make a home look as if it had sent (the safe side).
                self._motion_posts += 1  # the command may now reach the server
            elif route in (
                "jointreset",
                "startimp",
                "activate_gripper",
                "reset_gripper",
            ):
                raise ValueError("recovery/setup motion is not part of AERI")
            read_from = self.clock.now() if route == "getstate" else None
            response = original_post(route, payload)
            if route == "getstate":
                self._state_generation += 1
                self._gripper_measurement = None
                value = (
                    response.get("gripper_pos") if isinstance(response, dict) else None
                )
                if (
                    type(value) in (float, int)
                    and math.isfinite(value)
                    and 0 <= value <= 1
                ):
                    self._gripper_measurement = {
                        "route": "getstate",
                        "field": "gripper_pos",
                        "generation": self._state_generation,
                        "read_from_ns": read_from,
                        "read_through_ns": self.clock.now(),
                        "clock_domain": self.clock.domain,
                        "value": value,
                    }
            if route in (
                "clearerr",
                "pose",
                "open_gripper",
                "close_gripper",
                "close_gripper_slow",
                "move_gripper",
            ):
                status = reply_status(route, response)
                if status == "bad":
                    # The reply text is kept verbatim: it is the evidence.
                    self.latched = (
                        "unknown or negative HTTP acknowledgement: "
                        f"{route} reply={response!r}"[:300]
                    )
                    raise ValueError(self.latched)
                if status == "pending":
                    self._grip_queued = {
                        "route": route,
                        "reply": response,
                        "kind": _QUEUE_TEXTS[
                            response.removeprefix(_GRIPPER_KIND[route] + " gripper ")
                        ],
                        "target": _GRIPPER_KIND[route],
                    }
                self._http_acks.append((route, response))
            if (
                route == "getstate"
                and isinstance(response, dict)
                and response.get("state_fresh") is False
            ):
                self.latched = "robot state explicitly stale"
                raise ValueError(self.latched)
            return response

        env._post = fenced_post

    def admission_snapshot(self):
        with self._admission_lock:
            return AdmissionSnapshot(
                self._admission_mode == GATE_CLOSED,
                self._admission_generation,
                self._admission_closed_from,
                self.clock.now(),
                self.clock.domain,
                self._admission_counts["pose"]["attempts"],
                self._admission_counts["pose"]["rejected"],
                self._admission_counts["gripper"]["attempts"],
                self._admission_counts["gripper"]["rejected"],
                self._admission_mode,
                self._home_only_from,
                self._home_counts["pose"],
                self._home_counts["gripper"],
                self._home_counts["rejected"],
            )

    def stop_admission(self):
        """Close the gate: later pose/gripper POSTs are refused locally.

        Idempotent; a second call keeps the original closure time and
        generation. Returns the snapshot taken under the lock at closure, the
        mark an admission witness proves continuity from. A POST that passed the
        gate before this call is counted in the mark and may still be in flight.
        Called inside a home-only window it ends the window at once (the home's
        next command is refused) and starts a new closure interval.
        """
        with self._admission_lock:
            if self._admission_mode != GATE_CLOSED:
                self._admission_mode = GATE_CLOSED
                self._admission_generation += 1
                self._admission_closed_from = self.clock.now()
                self._home_gate_token = None
        return self.admission_snapshot()

    def close_admission_if_open(self):
        """Claim a new startup closure atomically; never adopt another closure.

        Returns its generation/time, without refreshing them after the lock.
        No command is sent. Existing stop_admission semantics are unchanged.
        """
        with self._admission_lock:
            if self._admission_mode != GATE_OPEN or self._home_active:
                raise AdmissionRefused("startup home requires unowned open admission")
            self._admission_mode = GATE_CLOSED
            self._admission_generation += 1
            self._admission_closed_from = self.clock.now()
            self._home_gate_token = None
            return self._admission_generation, self._admission_closed_from

    def reopen_admission(self):
        """Reopen the gate (e.g. an approved resume); breaks any closure proof.
        Refused while a home-only window is open."""
        with self._admission_lock:
            if self._home_active or self._admission_mode == GATE_HOME_ONLY:
                raise AdmissionRefused(
                    "home-only window open: admission is not reopened"
                )
            if self._admission_mode == GATE_CLOSED:
                self._admission_mode = GATE_OPEN
                self._admission_generation += 1
                self._admission_closed_from = None

    def reopen_admission_if(self, *, generation, closed_from_ns):
        """Reopen the gate only if it is still the closed interval named here.

        The specific counterpart of ``reopen_admission``: the caller names the
        closure (generation and closing time) it made and wants to end. Any other
        state raises AdmissionRefused and changes nothing: the gate is open, in a
        home-only window, or closed as a different interval (reopened and
        closed again, or closed by a home-only window). Not a general resume.
        """
        with self._admission_lock:
            if self._home_active or self._admission_mode == GATE_HOME_ONLY:
                raise AdmissionRefused(
                    "home-only window open: admission is not reopened"
                )
            if (
                self._admission_mode != GATE_CLOSED
                or type(generation) is not int
                or type(closed_from_ns) is not int
                or self._admission_generation != generation
                or self._admission_closed_from != closed_from_ns
            ):
                raise AdmissionRefused(
                    "admission is not the closed interval named by the caller"
                )
            self._admission_mode = GATE_OPEN
            self._admission_generation += 1
            self._admission_closed_from = None

    def _home_motion_current(self):
        """Under the admission lock: the send in progress is a gated home under
        this window's token, made by the thread that opened the window."""
        motion = self._motion
        return (
            self._home_gate_token is not None
            and threading.get_ident() == self._home_thread
            and motion is not None
            and motion[1] == "home"
            and motion[0] == self._home_gate_token
        )

    def _require_admission(self, token, kind, gated):
        with self._admission_lock:
            mode = self._admission_mode
            if mode == GATE_OPEN:
                return
            if (
                mode == GATE_HOME_ONLY
                and gated
                and kind == "home"
                and self._home_gate_token is not None
                and threading.get_ident() == self._home_thread
                and token == self._home_gate_token
            ):
                return
            if mode == GATE_HOME_ONLY:
                raise AdmissionRefused("home-only admission: command not started")
            raise AdmissionRefused("admission closed: command not started")

    def latch_state(self):
        return self.latched or (str(self.guard.tripped) if self.guard.tripped else None)

    def health(self):
        try:
            reading = self.guard.check(force=True)
            # An offline C3 is insufficient for this entry point's preflight.
            if reading.status != "ok":
                return {"state": "red", "reasons": list(reading.reasons)}
        except Exception as exc:  # noqa: BLE001 - hardware failure must lock motion  # a guard fault is a latched refusal
            self.latched = str(exc)
            return {"state": "red", "reasons": [str(exc)]}
        return {"state": "ok", "reasons": []}

    def preflight(self):
        health = self.health()
        return {
            "ok": health["state"] == "ok" and not self.latch_state(),
            "reasons": health["reasons"],
        }

    def observe(self):
        generation, began = self._state_generation, self.clock.now()
        try:
            raw = self.env.get_observation()
        except Exception as exc:
            # openpi's StaleFrameError (max_frame_age_ms): the frames are not
            # current, so this observation may not drive anything.
            if camera_meta.is_stale_frame_error(exc):
                raise camera_meta.CameraStale(str(exc)[:300]) from exc
            raise
        pose = [float(x) for x in raw["robot_state"]["cartesian_position"]]
        if len(pose) != 7 or not all(math.isfinite(x) for x in pose):
            raise ValueError("invalid robot observation")
        self.raw = raw
        measured = raw["robot_state"].get("gripper_position")
        proof = self._gripper_measurement
        if not (
            proof is not None
            and proof["generation"] > generation
            and proof["clock_domain"] == self.clock.domain
            and began
            <= proof["read_from_ns"]
            <= proof["read_through_ns"]
            <= self.clock.now()
            and type(measured) in (float, int)
            and math.isfinite(measured)
            and measured == proof["value"]
        ):
            proof = None
        images = {"side": raw["image"]["view1"], "wrist": raw["image"]["hand"]}
        signatures = {
            k: hashlib.sha256(v.tobytes()).hexdigest() for k, v in images.items()
        }
        meta = camera_meta.view_meta(raw.get("image_meta"))
        if meta is None and self.require_image_meta:
            raise ValueError(
                "max_frame_age_ms is configured but the observation has no "
                "image_meta: this openpi cannot enforce it"
            )
        found = {
            "observed_ns": self.clock.now(),
            "pose": pose,
            "frames": signatures,
            "state_fresh": True,
            "images_bgr": images,
            "robot_state": raw["robot_state"],
            "gripper_measurement": dict(proof) if proof is not None else None,
            "raw": raw,
        }
        if meta is not None:
            # Only with a frame-bus openpi: an older one keeps the old keys.
            found["image_meta"] = meta
        return found

    def recent_frames(self, view, n):
        """Up to ``n`` most recent frames of one view (oldest first) as
        ``{"seq", "t_capture_ns", "t_arrival_ns", "image"}``; empty when this
        openpi has no ``frame_history``. Read only: no robot I/O."""
        names = {v: k for k, v in camera_meta.VIEW_OF.items()}
        history = getattr(self.env, "frame_history", None)
        if view not in names or not callable(history) or type(n) is not int or n < 1:
            return []
        out = []
        for frame in history(names[view], n=n) or []:
            out.append(
                {
                    "seq": getattr(frame, "seq", None),
                    "t_capture_ns": getattr(frame, "t_capture_ns", None),
                    "t_arrival_ns": getattr(frame, "t_arrival_ns", None),
                    "image": getattr(frame, "image", None),
                }
            )
        return out

    def stopped(self):
        return self.stop_event.is_set() or self.stop_probe()

    def hold(self):
        # For the impedance server, hold means cease all new pose commands.
        self.held = True

    def emergency_hold(self):
        self.stop_event.set()
        self.hold()

    def _send(self, action, token, kind, gated=False):
        self._require_admission(token, kind, gated)
        self.fence.check(token, kind, self.clock.now())
        if self.stopped() or self.latch_state() or self.health()["state"] != "ok":
            raise ValueError("motion is stopped or fault locked")
        self._confirm_gripper()
        state = self.env.get_observation()["robot_state"]
        current = state["cartesian_position"]
        before = state["gripper_position"]
        original = [float(x) for x in action]
        fixed, notes = self.client._clamp_action(action, current, self.args)
        expected = list(original)
        if len(expected) == 7 and getattr(self.args, "binarize_gripper", False):
            expected[6] = 1.0 if expected[6] >= 0.5 else 0.0
        changed = (
            len(expected) != 7
            or len(fixed) != 7
            or any(abs(float(a) - b) > 1e-6 for a, b in zip(fixed, expected))
        )
        reason = None
        if changed or len(original) != 7 or not 0 <= original[6] <= 1:
            reason = "action was rewritten outside the reviewed normalization"
        # Reject rather than change the frozen motion or home target silently.
        elif notes or not all(math.isfinite(float(x)) for x in fixed):
            reason = "action refused by workspace/step guard"
        if reason is not None:
            # Diagnostics must never turn a rejected action into a send, even
            # when the client's diagnostic values cannot be serialized.
            try:
                # Bound inputs before serialization, and output before notes.
                # No arbitrary client object is iterated or stringified here.
                if (
                    len(original) > 7
                    or len(fixed) > 7
                    or type(notes) not in (list, tuple)
                    or len(notes) > 8
                    or any(type(note) is not str or len(note) > 240 for note in notes)
                ):
                    raise ValueError(
                        "diagnostic inputs exceed bounds or are not JSON values"
                    )
                detail = json.dumps(
                    {
                        "original": original,
                        "normalized": expected,
                        "clamped": [float(x) for x in fixed],
                        "changed_components": [
                            i
                            for i in range(max(len(expected), len(fixed)))
                            if i >= len(expected)
                            or i >= len(fixed)
                            or not math.isfinite(float(fixed[i]))
                            or abs(float(fixed[i]) - expected[i]) > 1e-6
                        ],
                        "clamp_notes": list(notes),
                    },
                    allow_nan=False,
                )
                if len(detail) > 8 * 240:
                    detail = json.dumps(
                        {
                            "status": "truncated",
                            "reason": "diagnostic exceeds 1920 characters",
                        }
                    )
            except Exception:  # noqa: BLE001 - diagnostics cannot permit motion
                detail = json.dumps(
                    {
                        "status": "unavailable",
                        "reason": "diagnostic inputs exceed bounds or cannot be serialized",
                    }
                )
            self._step_notes.append(("action_guard_refused", reason))
            # The journal truncates each note at 300 characters. Numbered
            # at most eight chunks carry JSON, or an explicit omission summary.
            for offset in range(0, len(detail), 240):
                self._step_notes.append(
                    (
                        f"action_guard_detail_{offset // 240:04d}",
                        detail[offset : offset + 240],
                    )
                )
            raise ValueError(reason + ": " + detail)
        self.fence.check(token, kind, self.clock.now())
        if self.stopped():
            raise ValueError("operator stop")
        self.held = False
        # env is already guarded. Any exception/None after this call may have
        # delivered a pose: report unknown, never retry the command.
        self._sending = True
        self._http_acks = []
        self._grip_queued = None
        self._motion = (token, kind)
        try:
            result = self.env.step(fixed)
        finally:
            self._motion = None
        if not isinstance(result, dict) or any(
            name not in result
            for name in ("clearerr_response", "pose_response", "gripper_response")
        ):
            raise RuntimeError("guarded step has no complete delivery acknowledgement")
        if not acknowledged(
            "clearerr", result["clearerr_response"]
        ) or not acknowledged("pose", result["pose_response"]):
            raise RuntimeError("guarded step nested motion acknowledgement is unknown")
        routes = [route for route, _ in self._http_acks]
        if routes.count("clearerr") != 1 or routes.count("pose") != 1:
            raise RuntimeError("guarded step did not expose exactly one fenced pose")
        gripper = result["gripper_response"]
        grip_routes = [
            route
            for route in routes
            if route.endswith("gripper") or route == "close_gripper_slow"
        ]
        if gripper is None:
            if grip_routes:
                raise RuntimeError("sent gripper has no acknowledgement")
        elif (
            isinstance(gripper, dict)
            and gripper.get("command") == "rate_limited"
            and not grip_routes
        ):
            pass  # The client explicitly sent no new gripper command.
        elif (
            not isinstance(gripper, dict)
            or gripper.get("command") not in grip_routes
            or reply_status(gripper["command"], gripper.get("response")) == "bad"
        ):
            raise RuntimeError("guarded step nested gripper acknowledgement is unknown")
        self._sending = False
        self.steps += 1
        if self._grip_queued is not None:
            try:
                before_pos = float(before)
            except (TypeError, ValueError):
                before_pos = math.nan  # unknown: treated as "was on target"
            self._grip_pending = {**self._grip_queued, "before": before_pos}
            self._grip_queued = None
            queued = self._grip_pending
            text = (
                f"{queued['route']} reply={queued['reply']!r}; delivery to be "
                "confirmed by the measured gripper position before the next motion"
            )
            self._step_notes.append(("gripper_reply_queued", text))
        return result

    def _confirm_gripper(self):
        """Before any new motion: a queued gripper reply must be confirmed by
        the measured position within a bounded wait, else lock (unknown)."""
        pending = self._grip_pending
        if pending is None:
            return
        want_open = pending["target"] == "open"
        before = pending["before"]
        # A target held behind an opposite goal proves nothing while the
        # gripper has not moved from the target side: the opposite goal may
        # still be running and the retained target not dispatched yet.
        needs_motion = pending["kind"] == "retained" and not (
            before < HOME_GRIPPER_MIN if want_open else before >= HOME_GRIPPER_MIN
        )
        began = self.clock.now()
        limit = began + int(self.gripper_confirm_timeout_s * 1e9)
        last, moved = None, False
        while True:
            position = float(
                self.env.get_observation()["robot_state"]["gripper_position"]
            )
            sampled_ns = self.clock.now()
            steady = last is not None and abs(position - last) <= GRIPPER_STABLE_DELTA
            if last is not None and not steady:
                moved = True
            last = position
            on_target = (
                position >= HOME_GRIPPER_MIN
                if want_open
                else position < HOME_GRIPPER_MIN
            )
            waited = (sampled_ns - began) / 1e9
            detail = (
                f"{pending['route']} reply={pending['reply']!r} "
                f"measured gripper={position:.4g} "
                f"(need {'>=' if want_open else '<'} {HOME_GRIPPER_MIN}, steady, "
                f"{'seen moving, ' if needs_motion else ''}"
                f"before={before:.4g}) waited_s={waited:.1f}"
            )
            why = None
            if self.stopped():
                why = "operator stop or signal while confirming"
            elif self.latch_state():
                why = "fault latch while confirming"
            elif self.health()["state"] != "ok":
                why = "health red while confirming"
            elif not math.isfinite(position) or not 0 <= position <= 1:
                why = "invalid measured gripper position"
            elif sampled_ns > limit:
                why = "not confirmed within the budget"
            if why is None and steady and on_target and (moved or not needs_motion):
                self._grip_pending = None
                self._step_notes.append(("gripper_reply_confirmed", detail))
                self.emit("Gripper reply confirmed: " + detail)
                return
            if why is None and sampled_ns >= limit:
                why = "not confirmed within the budget"
            if why is not None:
                self.latched = f"queued gripper reply not confirmed ({why}): {detail}"
                self.emit("Gripper reply NOT confirmed: " + self.latched)
                raise ValueError(self.latched)
            self.clock.advance(int(self.settle_poll_s * 1e9))

    def confirm_pending_gripper(self):
        """Confirm the final queued reply after hold, without sending motion."""
        if not self.held:
            raise ValueError("gripper terminal confirmation requires hold")
        before = len(self._step_notes)
        self._confirm_gripper()
        return tuple(self._step_notes[before:])

    def step(self, action, *, token):
        self._sending = False
        self._step_notes = []
        try:
            self._send(action, token, "policy_steps")
        except AdmissionRefused as exc:
            if not self._sending:  # refused before any HTTP: nothing to latch
                return StepResult("no", str(exc), self.steps)
            self.latched = str(exc)
            return StepResult("unknown", str(exc), self.steps)
        except Exception as exc:  # noqa: BLE001 - hardware failure must lock motion
            self.latched = str(exc)
            return StepResult(
                "unknown" if self._sending else "no",
                str(exc),
                self.steps,
                tuple(self._step_notes),
            )
        return StepResult("yes", "guarded_step", self.steps, tuple(self._step_notes))

    def home(self, *, token):
        return self._run_home(token, gated=False)

    def home_under_closed_gate(self, *, token):
        """Home while the admission gate stays shut for the policy.

        The one way to move the arm while admission is closed. It starts only
        from the closed mode, with the current unexpired ``home`` token of the
        fence, no stop, no latch and green health; otherwise ``no`` is returned
        before anything is sent or latched. Inside the window only pose, clearerr
        and open_gripper POSTs sent by this home, under this token, from the
        thread that called this method, pass the gate; policy steps,
        close_gripper and move_gripper, and every command from another thread,
        are refused. The gate does not constrain other processes. Whatever
        happens, the gate returns to closed with a new generation and closure
        time, so no earlier closure proof carries over, and the arm is held.
        """
        marker = object()
        try:
            refusal = self._enter_home_only(token, marker)
            if refusal is not None:
                return HomeResult("no", "home-only entry refused: " + refusal)
            return self._run_home(token, gated=True)
        finally:
            # Only the call that opened the window closes it: a refused or nested
            # call holds no marker.
            if self._home_marker is marker:
                try:
                    self._leave_home_only()
                finally:
                    self.hold()

    def _enter_home_only(self, token, marker):
        """Why the window cannot open, or None once it is open. A refused call
        changes nothing: the open-window check comes before any other."""
        with self._admission_lock:
            if self._home_active:
                return "a home-only window is already open"
        try:
            if getattr(token, "kind", None) != "home":
                return "token is not a home token"
            now = self.clock.now()
            self.fence.check(token, "home", now)
            opened_at = self.clock.now()  # read before any state changes
            if self.fence.current != token:
                return "token is not the fence's current token"
            if self.stopped():
                return "operator stop or signal"
            if self.latch_state():
                return "fault latch"
            if self.health()["state"] != "ok":
                return "health is not ok"
        except Exception as exc:  # noqa: BLE001 - any doubt keeps the gate shut
            return f"{type(exc).__name__}: {exc}"
        with self._admission_lock:
            if self._home_active:
                return "a home-only window is already open"
            if self._admission_mode != GATE_CLOSED:
                return f"admission is {self._admission_mode}, not closed"
            # The marker first: from here on the caller's finally closes the
            # window, however the rest of this block is interrupted.
            self._home_marker = marker
            self._home_active = True
            self._admission_mode = GATE_HOME_ONLY
            self._admission_generation += 1
            # The earlier closure ended here: a stop whose clock read fails inside
            # the window must not leave that older time as the interval start.
            self._admission_closed_from = None
            self._home_only_from = opened_at
            self._home_gate_token = token
            self._home_thread = threading.get_ident()
        return None

    def _leave_home_only(self):
        """Close the window. Whatever interrupts it (also a BaseException such as
        KeyboardInterrupt), the closing and the cleanup finish first and the first
        interrupt is raised afterwards."""
        interrupt = None
        try:
            now = self.clock.now()
        except BaseException as exc:  # noqa: BLE001 - the gate must close even without a clock
            now = None  # no closure time: a witness cannot be built on this interval
            if not isinstance(exc, Exception):
                interrupt = exc
        # An interrupt that is not an Exception (KeyboardInterrupt, SystemExit) may
        # land half-way: run the idempotent cleanup again, at most three times. An
        # ordinary Exception is not retried: it ends the loop at once so the
        # caller's finally still reaches hold().
        for _attempt in range(3):
            try:
                with self._admission_lock:
                    # Closed whatever the mode is now: a stop closed it already
                    # (keep that interval); any other mode is forced shut as a new
                    # interval. Every step is safe to run again after an interrupt.
                    if self._admission_mode != GATE_CLOSED:
                        self._admission_closed_from = now
                        self._admission_generation += 1
                        self._admission_mode = GATE_CLOSED
                    elif self._admission_closed_from is None:
                        # closed inside the window by a stop whose clock failed
                        self._admission_closed_from = now
                    self._home_gate_token = None
                    self._home_thread = None
                    self._home_marker = None
                    self._home_active = False
                break
            except Exception as exc:  # noqa: BLE001 - give up; the caller holds
                interrupt = interrupt or exc
                break
            except BaseException as exc:  # noqa: BLE001 - run the cleanup again
                interrupt = interrupt or exc
        if interrupt is not None:
            raise interrupt

    def _run_home(self, token, *, gated):
        self._sending = False
        self._step_notes = []
        moved = False
        adapter = self
        posts_before = self._motion_posts

        def sent():
            # _sending is shared with step() and may be cleared by another
            # thread; a gated home also counts the POSTs that passed the fence.
            return (
                moved or self._sending or (gated and self._motion_posts != posts_before)
            )

        class HomingEnv:
            def get_observation(self):
                return adapter.env.get_observation()

            def step(self, action):
                nonlocal moved
                result = adapter._send(action, token, "home", gated)
                moved = True
                return result

        try:
            self.client._go_home(HomingEnv(), self.args)
            return self._settle_home(token)
        except AdmissionRefused as exc:
            if not sent():
                return HomeResult("no", str(exc))
            self.latched = str(exc)
            return HomeResult("unknown", str(exc))
        except MotionRefused as exc:
            # A stop revoked the token before the first command of a gated home:
            # nothing was sent, so nothing is locked. After a command, or on the
            # old home(), it is a lock as before.
            if gated and not sent():
                return HomeResult("no", str(exc))
            self.latched = str(exc)
            return HomeResult("unknown" if sent() else "no", str(exc))
        except Exception as exc:  # noqa: BLE001 - hardware failure must lock motion
            self.latched = str(exc)
            return HomeResult("unknown" if sent() else "no", str(exc))

    def _home_reading(self, expected, *, state=None):
        if state is None:
            state = self.env.get_observation()["robot_state"]

        def finite_pose(values, name):
            try:
                if (
                    isinstance(values, (str, bytes, dict))
                    or getattr(values, "ndim", 1) != 1
                    or len(values) not in (6, 7)
                ):
                    raise ValueError(f"{name} pose must have six or seven components")
                checked = tuple(float(value) for value in values)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"invalid {name} pose: {exc}") from exc
            if not all(math.isfinite(value) for value in checked):
                raise ValueError(f"{name} pose has a non-finite component")
            return checked

        # max([0, NaN, 0]) can return zero: validate every raw component first.
        observed = finite_pose(state["cartesian_position"], "observed")
        expected = finite_pose(expected, "expected home")
        error = 1000 * math.sqrt(
            sum(
                (float(a) - float(b)) ** 2
                for a, b in zip(observed[:3], expected[:3], strict=True)
            )
        )
        rotation = max(
            abs(
                math.atan2(math.sin(float(a) - float(b)), math.cos(float(a) - float(b)))
            )
            for a, b in zip(observed[3:6], expected[3:6], strict=True)
        )
        return error, rotation, float(state["gripper_position"])

    def _home_within(self, reading):
        error, rotation, gripper = reading
        # Non-finite or out-of-range normalized measurements prove nothing.
        return (
            all(math.isfinite(value) for value in reading)
            and 0 <= gripper <= 1
            and error <= self.tolerance_mm
            and rotation <= HOME_ROTATION_RAD
            and gripper >= HOME_GRIPPER_MIN
        )

    def _pose_text(self, reading):
        error, rotation, gripper = reading
        return (
            f"error_mm={error:.1f} tolerance_mm={self.tolerance_mm} "
            f"rotation_rad={rotation:.4g} rotation_tol_rad={HOME_ROTATION_RAD} "
            f"gripper={gripper:.4g} gripper_min={HOME_GRIPPER_MIN}"
        )

    def _home_text(self, reading, waited_s):
        return f"{self._pose_text(reading)} waited_s={waited_s:.1f}"

    def start_pose(self):
        """Read-only check of the arm against the bound home pose before the
        first episode, with the same position and rotation tolerance as the
        home verification, including the measured open gripper. Never moves the
        arm. A reading that cannot be taken or is not a number is a refusal."""
        result = {
            "ok": False,
            "target_pose": None,
            "measured_pose": None,
            "error_mm": None,
            "rotation_rad": None,
            "gripper": None,
            "tolerance_mm": self.tolerance_mm,
            "rotation_tol_rad": HOME_ROTATION_RAD,
            "gripper_min": HOME_GRIPPER_MIN,
            "unmet": [],
            "unreadable_reason": None,
        }

        def finite_number(value):
            try:
                value = float(value)
                return value if math.isfinite(value) else None
            except (TypeError, ValueError, OverflowError):
                return None

        def pose_values(value):
            if (
                isinstance(value, (str, bytes, dict))
                or getattr(value, "ndim", 1) != 1
                or len(value) not in (6, 7)
            ):
                return None
            return [finite_number(item) for item in value]

        try:
            expected = self.client._resolve_home(self.args)
            result["target_pose"] = pose_values(expected)
            state = self.env.get_observation()["robot_state"]
            result["measured_pose"] = pose_values(state.get("cartesian_position", ()))
            result["gripper"] = finite_number(state.get("gripper_position"))
            # One observation supplies both the decision and its diagnostic.
            reading = self._home_reading(expected, state=state)
        except Exception as exc:  # noqa: BLE001 - unreadable pose: do not start
            result.update(
                text=f"pose unreadable: {exc}",
                unmet=["unreadable"],
                unreadable_reason=str(exc),
            )
            return result
        error, rotation, gripper = reading
        unmet = result["unmet"]
        if not math.isfinite(error) or error > self.tolerance_mm:
            unmet.append("position_tolerance")
        if not math.isfinite(rotation) or rotation > HOME_ROTATION_RAD:
            unmet.append("rotation_tolerance")
        if not math.isfinite(gripper) or not 0 <= gripper <= 1:
            unmet.append("gripper_invalid")
        elif gripper < HOME_GRIPPER_MIN:
            unmet.append("gripper_not_open")
        result.update(
            ok=self._home_within(reading),
            text=self._pose_text(reading),
            error_mm=finite_number(error),
            rotation_rad=finite_number(rotation),
        )
        return result

    def _settle_home(self, token):
        """The arm lags the last command: read until it is inside the reviewed
        thresholds or the bounded budget ends. A stop, a latch or a red guard
        ends the wait at once. Nothing is sent while waiting."""
        expected = self.client._resolve_home(self.args)
        began = self.clock.now()
        limit = began + int(self.home_settle_timeout_s * 1e9)
        expires = getattr(token, "expires_mono_ns", None)
        if type(expires) is int:
            limit = min(limit, max(expires, began))
        while True:
            reading = self._home_reading(expected)
            text = self._home_text(reading, (self.clock.now() - began) / 1e9)
            notes = (*self._step_notes, ("home_measured", text))
            why = None
            if self.stopped():
                why = "operator stop or signal while waiting"
            elif self.latch_state():
                why = "fault latch while waiting"
            elif (health := self.health())["state"] != "ok":
                why = "health red while waiting: " + ", ".join(
                    str(x) for x in health["reasons"]
                )
            if why is None:
                self.fence.check(token, "home", self.clock.now())
                if self._home_within(reading):
                    self.emit("Home verified: " + text)
                    return HomeResult("yes", "home_verified", reading[0], notes)
                if self.clock.now() >= limit:
                    why = "not within the thresholds after the settle budget"
            if why is not None:
                self.latched = (
                    f"home tolerance or open gripper not verified ({why}): {text}"
                )
                self.emit("Home NOT verified: " + self.latched)
                error = reading[0]
                reported = error if error > self.tolerance_mm else self.tolerance_mm + 1
                return HomeResult("unknown", self.latched, reported, notes)
            self.clock.advance(int(self.settle_poll_s * 1e9))


class Policy:
    """One worker at a time; a still-running query prevents quiesce ack."""

    def __init__(
        self,
        clients,
        deployments,
        prompts,
        robot,
        client_module,
        args,
        clock,
        *,
        prompt_allowed=None,
    ):
        self.clients, self.deployments, self.prompts = clients, deployments, prompts
        # subtask_loop: an episode may bring its own prompt (``acquire(prompt=)``)
        # only when ``prompt_allowed(text)`` says yes (template texts from the
        # run's frozen whitelist); without it every episode prompt is refused.
        self.prompt_allowed = prompt_allowed
        # The prompt of the episode acquired now (None between episodes).
        self.prompt = None
        self.robot, self.client, self.args, self.clock = (
            robot,
            client_module,
            args,
            clock,
        )
        self.role = None
        self.epoch = -1
        self.ticket = None
        self.lock = threading.Lock()

    def warmup(self, role, *, raw, timeout_s=DEFAULT_POLICY_WARMUP_TIMEOUT_S):
        """Warm the same frozen client without granting motion or retaining actions.

        A timeout leaves the query slot occupied until the actual worker ends;
        it never cancels backend work, retries, or permits another query.
        """
        if (
            type(timeout_s) not in (int, float)
            or not math.isfinite(timeout_s)
            or timeout_s <= 0
        ):
            raise ValueError("policy_warmup_timeout_s must be positive finite")
        with self.lock:
            if self.ticket is not None and not self.ticket.done.is_set():
                return {"ok": False, "detail": "query_still_running"}
            if self.role is not None:
                return {"ok": False, "detail": "policy_role_already_acquired"}
            ticket = SimpleNamespace(done=threading.Event(), result=None)
            self.ticket = ticket
        began = time.monotonic()

        def work():
            try:
                metadata = self.clients[role].get_server_metadata()
                check_identity(self.deployments[role], metadata)
                self.client._check_server(metadata, self.args)
                self.client._check_prompt(self.prompts[role], self.args, metadata)
                self.client._query_policy(
                    self.clients[role],
                    raw,
                    raw["robot_state"]["cartesian_position"],
                    self.prompts[role],
                )
                ticket.result = {"ok": True}
            except (Exception, SystemExit) as exc:  # noqa: BLE001 - legacy client may refuse with SystemExit
                ticket.result = {"ok": False, "detail": str(exc)}
            finally:
                ticket.done.set()

        threading.Thread(target=work, name="aeri-policy-warmup", daemon=True).start()
        if not ticket.done.wait(timeout_s):
            return {
                "ok": False,
                "detail": "policy warm-up timed out",
                "duration_s": time.monotonic() - began,
            }
        return {**ticket.result, "duration_s": time.monotonic() - began}

    def acquire(self, role, *, policy_epoch, prompt=None):
        with self.lock:
            if self.ticket is not None and not self.ticket.done.is_set():
                return {"kind": "unavailable", "code": "query_still_running"}
            text = self.prompts[role] if prompt is None else prompt
            if prompt is not None:
                allowed = self.prompt_allowed
                try:
                    ok = allowed is not None and allowed(prompt) is True
                except Exception:  # noqa: BLE001 - a failing check refuses
                    ok = False
                if not ok:
                    return {
                        "kind": "unavailable",
                        "code": "prompt_refused",
                        "detail": "the episode prompt is not a template text "
                        "of the frozen whitelist",
                    }
            try:
                metadata = self.clients[role].get_server_metadata()
                check_identity(self.deployments[role], metadata)
                self.client._check_server(metadata, self.args)
                self.client._check_prompt(text, self.args, metadata)
            except (Exception, SystemExit) as exc:  # noqa: BLE001 - legacy client refuses with SystemExit
                return {
                    "kind": "unavailable",
                    "code": "deployment_mismatch",
                    "detail": str(exc),
                }
            self.role, self.epoch, self.prompt = role, policy_epoch, text
            return {
                "handle_id": f"real-{role}-{policy_epoch}",
                "policy_epoch": policy_epoch,
                "role": role,
            }

    def request(self, request):
        with self.lock:
            if (
                self.role is None
                or request["policy_epoch"] != self.epoch
                or self.robot.raw is None
            ):
                return {"kind": "unavailable", "code": "not_acquired"}
            if self.ticket is not None and not self.ticket.done.is_set():
                return {"kind": "unavailable", "code": "busy"}
            ticket = SimpleNamespace(
                request=dict(request), done=threading.Event(), result=None
            )
            self.ticket = ticket
            role, raw, prompt = self.role, self.robot.raw, self.prompt

        def work():
            began = time.monotonic()
            try:
                actions = self.client._query_policy(
                    self.clients[role],
                    raw,
                    raw["robot_state"]["cartesian_position"],
                    prompt,
                )
                ticket.result = {
                    "schema": aeri.SCHEMAS["runtime"],
                    "minor": 0,
                    "run_id": request["run_id"],
                    "emitted_wall_ns": time.time_ns(),
                    "kind": "chunk_response",
                    "request_id": request["request_id"],
                    "episode_id": request["episode_id"],
                    "policy_epoch": request["policy_epoch"],
                    "action_contract": "fr3-robotiq@1",
                    "actions": actions.tolist(),
                    "valid_from_action_index": request["action_start_index"],
                    "prefix_conditioning_applied": "none",
                    "model_version": {
                        "config": self.deployments[role]["config"],
                        "checkpoint_dir_name": Path(
                            self.deployments[role]["checkpoint_dir"]
                        ).name,
                    },
                    "timing": {"total_ms": int((time.monotonic() - began) * 1000)},
                    "clock_domain": request["clock_domain"],
                }
            except (Exception, SystemExit) as exc:  # noqa: BLE001 - legacy client refuses with SystemExit
                ticket.result = {
                    "kind": "unavailable",
                    "code": "policy_error",
                    "detail": str(exc),
                }
            finally:
                ticket.done.set()

        threading.Thread(target=work, name="aeri-policy-query", daemon=True).start()
        return ticket

    def collect(self, ticket, *, timeout_ns):
        if not ticket.done.wait(timeout_ns / 1e9):
            return None
        return ticket.result

    def quiesce(self, *, policy_epoch):
        with self.lock:
            busy = self.ticket is not None and not self.ticket.done.is_set()
            self.role = None
            self.prompt = None
            self.epoch = max(policy_epoch, self.epoch)
            return {
                "ok": not busy,
                "fenced_epoch": policy_epoch,
                "inflight_cancelled": 0,
            }
