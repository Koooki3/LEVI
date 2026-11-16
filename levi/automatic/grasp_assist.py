"""The bounded, human-confirmed grasp takeover before the reset policy (``GRASP_ASSIST``).

Design: STEP5-AERI-RESET-DESIGN R2-A and R3, user decisions of 2026-11-16. After the
forward loop ended, the arm is home and the reset service is ready, the reverse loop
needs the object in the gripper. This module is the takeover that puts it there: a
person moves the tool with a handful of keys, closes the gripper, lifts, and hands the
arm to the reset policy where it stands (no home). Nothing here moves a robot by
itself: every key is a person's, every motion is small, bounded and refused when it
leaves its limits.

Keys (``KEY_CHARS``; the console and the page send these bytes, nothing else reaches
this module): ``x X y Y z Z`` move the tool one ``step_m`` (at most 0.02 m) along that
axis, upper case the negative direction; ``c`` close; ``u`` lift 2 cm; ``o`` open
(retry); ``g`` hand back to the reset policy; ``a`` abort (hold, no home, a person).

Limits, all from the job and the binding, none negotiable at run time:

- one move is at most ``MAX_STEP_M`` (0.02 m) per axis and is sent as ``SUBSTEPS`` small
  steps; the orientation is the one measured at the state's entry, never changed;
- the target stays at or above the binding's ``min_z``, inside its ``max_xy``, within
  ``max_travel_m`` of the entry point, and at most ``max_steps`` keys are executed;
- after each move the tool is within ``follow_error_m`` of its target (default 5 mm,
  uncalibrated) and its orientation within ``ORIENTATION_TOL_RAD`` of the entry's.

A key that breaks a limit is refused before anything is sent and nothing latches
(``AssistRefused``: a person's mistake). A comms or measurement anomaly (an
unreadable or inconsistent observation, a robot that latched, a command the server
did not acknowledge) is ``AssistFault``: the run locks. A move that was sent and did
not reach its target is reported (``ok: false``) and one setpoint at the measured pose
is sent in the same window (the arm does not lean on an obstacle at the old target);
nothing latches, but ``max_follow_misses`` of them in a row lock the run. The sub-steps
of a move are ``SUBSTEP_PERIOD_S`` apart. A stop (``AssistStopped``: an operator's
stop by any route, a signal) is checked before every command: what was not sent is not
sent and the takeover is abandoned (not a fault). A gripper command is confirmed by the
client's own ``state[6]`` and sent again if the client dropped it.

Holding. After ``close`` and after ``lift`` the session produces the facts of
``holding.py`` from one ``ObservationReader`` per grasp (the measured opening, never
the command): ``assess_close`` on a settled series after the close command, then
``assess_lift`` on two readings after the lift. ``lift`` first re-measures the closed
gripper so the candidate the lift is compared with is seconds old (``max_gap_ms``),
and refuses to lift unless that candidate is ``holding_candidate``. The lift distance
is the commanded 2 cm, so the measured lift counted is ``LIFT_M - 2 * follow_error_m``
(``lift_min_m``, at least 1 cm): a tool that follows up to the allowed error short
still proves a lift. The fact is about the object staying in the gripper, not about
the exact height. Both numbers are uncalibrated defaults for the real-robot check V2.

Hand-back (``g``). Refused unless a fresh re-measurement of the closed gripper (taken at
the key, not remembered) is a ``holding_candidate`` and the latest fact is neither
``empty`` nor ``open`` (nor unproven: fail closed); a person can still abort. With the
job's ``handoff: operator`` (the only one enabled) the key is the person's decision;
the planned level ``handoff: holding_fact`` hands over by itself on a
``holding_verified`` fact whose source is ``verified_measurement`` (``holding.
require_real``) and is refused in jobs until ``aeri.UNENABLED_HANDOFFS`` stops listing it
(the code path and its tests are in place). The policy's ``state[6]`` is the COMMANDED
gripper (0.0 after a close, as ``fr3_env._pose_state`` reports it), never the measured
opening: the hand-back reads a fresh observation and refuses unless its ``state[6]``
equals the command this session issued. ``env.reset()`` is never called and nothing
homes: the reset episode starts from the pose the person left.

Pure logic over a port (``AssistPort``): the real one is ``fr3_assist.Fr3AssistPort``
over ``fr3_bridge.Robot``; tests pass fakes. This module opens no connection.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from levi.domain import aeri

from . import holding as hd

# --- keys -----------------------------------------------------------------------------

KEYS = (
    "x+",
    "x-",
    "y+",
    "y-",
    "z+",
    "z-",
    "close",
    "lift",
    "open",
    "handback",
    "abort",
)
MOVE_KEYS = frozenset({"x+", "x-", "y+", "y-", "z+", "z-"})
# Executed keys (counted against ``max_steps``): the others decide, they do not act.
ACTING_KEYS = MOVE_KEYS | {"close", "lift", "open"}
KEY_CHARS = {
    "x": "x+",
    "X": "x-",
    "y": "y+",
    "Y": "y-",
    "z": "z+",
    "Z": "z-",
    "c": "close",
    "u": "lift",
    "o": "open",
    "g": "handback",
    "a": "abort",
}

# --- limits and defaults --------------------------------------------------------------

MAX_STEP_M = aeri.GRASP_ASSIST_MAX_STEP_M  # per axis, per key: hard, not configurable
DEFAULT_STEP_M = 0.01
LIFT_M = 0.02
SUBSTEPS = 4
DEFAULT_MAX_STEPS = 40
DEFAULT_MAX_TRAVEL_M = 0.35
DEFAULT_FOLLOW_ERROR_M = 0.005
ORIENTATION_TOL_RAD = (
    0.05  # as the home verification (``fr3_bridge.HOME_ROTATION_RAD``)
)
# The lift's candidate is at most this old when the lift readings are taken
# (holding.HoldingConfig.max_gap_ms; uncalibrated, V2).
LIFT_MAX_GAP_MS = 10_000
SETTLE_S = 3.0  # most a move or a gripper waits to settle
# The sub-steps of one move are sent this far apart (as the lab takeover tool did),
# not back to back: four sub-steps sent at once are one 2 cm jump for the controller.
SUBSTEP_PERIOD_S = 0.1
# A move that did not arrive holds the arm where it is; this many such moves in a
# row lock the run (the arm is pressing on something). Uncalibrated.
DEFAULT_MAX_FOLLOW_MISSES = 2
# A gripper command the client did not take (its own rate limit, ``gripper_sleep``,
# drops one without recording it) is sent again after this wait, this many times.
GRIPPER_TRIES = 3
GRIPPER_RETRY_S = 0.8
POLL_S = 0.06  # between two readings (holding.DEFAULT_MIN_INTERVAL_MS is 30 ms)
CLOSE_WAIT_S = 6.0  # most a settled series after a close or open is waited for
# Orientation of the server pose and of the euler the client reports must agree.
POSE_AGREE_M = 1e-3
POSE_AGREE_RAD = 1e-3

# A series that is not a fact yet: the gripper is still moving, the window is not
# long enough, or a reading did not come with its proof. Waited for, bounded.
WAIT_REASONS = (
    "gripper_not_steady",
    "settle_window_too_short",
    "settle_window_too_long",
    "readings_too_close",
    "gripper_measurement_not_distinct",
    "gripper_reading_missing",
)
READ_STATUSES = ("holding_candidate", "holding_verified")


class AssistRefused(Exception):
    """A key refused before anything was sent (a limit, a precondition). Nothing
    latches: the person pressed the wrong key."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


class AssistStopped(Exception):
    """A stop (an operator's stop registered anywhere, a signal) was seen while a key
    was being carried out: what was not sent is not sent, the takeover is abandoned
    (hold, no home, a person). Not a fault."""


class AssistFault(Exception):
    """A comms or measurement anomaly (or a robot that latched): the run locks."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


# --- the job's settings ---------------------------------------------------------------


def _number(value: object) -> bool:
    return (
        type(value) in (int, float)
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


@dataclass(frozen=True)
class GraspAssistConfig:
    """``subtask.grasp_assist`` of the job (default: absent, off). A known value that
    is not enabled yet (``aeri.UNENABLED_HANDOFFS``) is refused with its own code."""

    enabled: bool = False
    grasp_source: str = "operator"
    handoff: str = "operator"
    step_m: float = DEFAULT_STEP_M
    max_steps: int = DEFAULT_MAX_STEPS
    max_travel_m: float = DEFAULT_MAX_TRAVEL_M
    follow_error_m: float = DEFAULT_FOLLOW_ERROR_M
    # Moves that did not arrive, in a row, before the run locks.
    max_follow_misses: int = DEFAULT_MAX_FOLLOW_MISSES
    # ``<object words>|<lo>|<hi>`` entries, sorted (the job's list).
    holding_band: tuple = ()

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("grasp_assist.enabled is true or false")
        if self.grasp_source not in aeri.GRASP_ASSIST_SOURCES:
            raise ValueError(
                f"grasp_assist.grasp_source: grasp_source_not_known: "
                f"{', '.join(aeri.GRASP_ASSIST_SOURCES)}"
            )
        if self.grasp_source in aeri.UNENABLED_GRASP_SOURCES:
            raise ValueError(
                "grasp_assist.grasp_source: grasp_source_not_enabled: "
                f"{self.grasp_source!r} is planned but not enabled yet"
            )
        if self.handoff not in aeri.GRASP_ASSIST_HANDOFFS:
            raise ValueError(
                f"grasp_assist.handoff: handoff_not_known: "
                f"{', '.join(aeri.GRASP_ASSIST_HANDOFFS)}"
            )
        if self.handoff in aeri.UNENABLED_HANDOFFS:
            raise ValueError(
                f"grasp_assist.handoff: handoff_not_enabled: {self.handoff!r} is "
                "planned (level H1) but not enabled until the real-robot "
                "calibration V2; use handoff: operator"
            )
        if not _number(self.step_m) or not 0 < self.step_m <= MAX_STEP_M:
            raise ValueError(f"grasp_assist.step_m is above 0 and at most {MAX_STEP_M}")
        if type(self.max_steps) is not int or not 1 <= self.max_steps <= 200:
            raise ValueError("grasp_assist.max_steps is a whole number from 1 to 200")
        if not _number(self.max_travel_m) or not 0 < self.max_travel_m <= 1.0:
            raise ValueError("grasp_assist.max_travel_m is above 0 and at most 1.0 m")
        if not _number(self.follow_error_m) or not 0 < self.follow_error_m <= 0.01:
            raise ValueError(
                "grasp_assist.follow_error_m is above 0 and at most 0.01 m"
            )
        if type(self.max_follow_misses) is not int or not (
            1 <= self.max_follow_misses <= 5
        ):
            raise ValueError(
                "grasp_assist.max_follow_misses is a whole number from 1 to 5"
            )
        band = self.holding_band
        if isinstance(band, list):
            band = tuple(sorted(band))
            object.__setattr__(self, "holding_band", band)
        if not isinstance(band, tuple) or not all(isinstance(b, str) for b in band):
            raise ValueError("grasp_assist.holding_band is a list of text entries")
        if self.enabled:
            self.bands()  # refused whole when malformed; none, none to use

    def bands(self) -> dict[str, hd.HoldingConfig]:
        """``{object words: HoldingConfig}`` from the entries (``holding.
        parse_holding_bands``, the lift limits of this takeover)."""
        raw: dict = {}
        for entry in self.holding_band:
            parts = entry.split("|")
            if len(parts) != 3 or parts[0] in raw:
                raise ValueError(
                    "grasp_assist.holding_band: one '<words>|<lo>|<hi>' entry per object"
                )
            try:
                raw[parts[0]] = [float(parts[1]), float(parts[2])]
            except ValueError:
                raise ValueError(
                    "grasp_assist.holding_band: lo and hi are numbers"
                ) from None
        return hd.parse_holding_bands(
            raw,
            lift_min_m=max(LIFT_M / 2, LIFT_M - 2 * self.follow_error_m),
            max_gap_ms=LIFT_MAX_GAP_MS,
        )

    def describe(self) -> dict:
        """The plan's form of the settings (every resolved value)."""
        return {
            "enabled": self.enabled,
            "grasp_source": self.grasp_source,
            "handoff": self.handoff,
            "step_m": self.step_m,
            "max_steps": self.max_steps,
            "max_travel_m": self.max_travel_m,
            "follow_error_m": self.follow_error_m,
            "max_follow_misses": self.max_follow_misses,
            "holding_band": list(self.holding_band),
        }


@dataclass(frozen=True)
class Limits:
    """The binding's workspace (``binding.safety``): a floor and a square. Both are
    needed: a job without a floor cannot run a takeover."""

    min_z: float
    max_xy: float

    def __post_init__(self):
        if not (_number(self.min_z) and self.min_z > 0):
            raise ValueError("assist needs a positive min_z (a floor)")
        if not (_number(self.max_xy) and self.max_xy > 0):
            raise ValueError("assist needs a positive max_xy")

    @classmethod
    def from_safety(cls, safety: dict) -> Limits:
        return cls(float(safety["min_z"]), float(safety["max_xy"]))


# --- orientation -----------------------------------------------------------------------
# The robot server's pose is [x, y, z, qx, qy, qz, qw]: the quaternion is (x, y, z, w),
# scalar LAST (``fr3_env._pose_state`` hands pose[3:] to ``Rotation.from_quat``). The
# client's ``cartesian_position`` and its action carry the same rotation as extrinsic
# "xyz" Euler angles (R = Rz(rz) Ry(ry) Rx(rx)) with the commanded gripper as the 7th
# value. Pure math, no scipy.


def _qmul(a, b):
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    )


def euler_to_quat(rx: float, ry: float, rz: float) -> tuple:
    """``(qx, qy, qz, qw)`` of extrinsic xyz Euler angles."""

    def axis(angle, i):
        half = angle / 2
        q = [math.cos(half), 0.0, 0.0, 0.0]
        q[i] = math.sin(half)
        return tuple(q)

    w, x, y, z = _qmul(axis(rz, 3), _qmul(axis(ry, 2), axis(rx, 1)))
    if w < 0:
        w, x, y, z = -w, -x, -y, -z
    return (x, y, z, w)


def quat_to_euler(q) -> tuple:
    """Extrinsic xyz Euler angles ``(rx, ry, rz)`` of ``q = (qx, qy, qz, qw)``."""
    if len(q) != 4 or not all(_number(v) for v in q):
        raise ValueError("a quaternion is four finite numbers (qx, qy, qz, qw)")
    x, y, z, w = (float(v) for v in q)
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    if not 0.99 < norm < 1.01:
        raise ValueError("quaternion is not a unit quaternion")
    x, y, z, w = x / norm, y / norm, z / norm, w / norm
    r20 = 2 * (x * z - w * y)
    if abs(r20) > 0.999999:
        raise ValueError("gimbal lock: the Euler angles are not defined")
    rx = math.atan2(2 * (y * z + w * x), 1 - 2 * (x * x + y * y))
    ry = math.asin(-r20)
    rz = math.atan2(2 * (x * y + w * z), 1 - 2 * (y * y + z * z))
    return (rx, ry, rz)


def quat_angle(a, b) -> float:
    """The rotation angle between two unit quaternions (radians, sign-free)."""
    dot = abs(sum(float(p) * float(q) for p, q in zip(a, b, strict=True)))
    return 2 * math.acos(min(1.0, dot))


@dataclass(frozen=True)
class Pose:
    """One observation of the arm as this takeover reads it: the tool position, its
    orientation both ways, the COMMANDED gripper (``state[6]``) and the measured
    opening (``None`` without the bridge's proof)."""

    xyz: tuple
    euler: tuple
    quat: tuple  # (qx, qy, qz, qw)
    gripper_cmd: float
    opening: float | None
    observed_ns: int | None


def pose_of(observation: dict) -> Pose:
    """The ``Pose`` of a ``fr3_bridge.Robot.observe()`` result. A malformed or
    inconsistent observation is a fault (``AssistFault``). When the observation
    carries the server's own pose (``server_pose``), its quaternion is the measured
    orientation and must agree with the client's Euler angles: a quaternion read in
    the wrong order cannot."""
    try:
        pose = [float(v) for v in observation["pose"]]
    except (KeyError, TypeError, ValueError):
        raise AssistFault("observation_invalid", "no pose") from None
    if len(pose) != 7 or not all(math.isfinite(v) for v in pose):
        raise AssistFault("observation_invalid", "the pose is seven finite numbers")
    if observation.get("state_fresh") is not True:
        raise AssistFault("observation_stale", "the robot state is not fresh")
    xyz, euler, cmd = tuple(pose[:3]), tuple(pose[3:6]), pose[6]
    if not 0.0 <= cmd <= 1.0:
        raise AssistFault("observation_invalid", f"state[6]={cmd!r} is not 0..1")
    from_euler = euler_to_quat(*euler)
    server = observation.get("server_pose")
    if server is None:
        quat = from_euler
    else:
        try:
            values = [float(v) for v in server]
            if len(values) != 7 or not all(math.isfinite(v) for v in values):
                raise ValueError
            quat = tuple(values[3:7])
            q_norm = math.sqrt(sum(v * v for v in quat))
            if not 0.99 < q_norm < 1.01:
                raise ValueError
        except (TypeError, ValueError):
            raise AssistFault(
                "pose_inconsistent", "the server pose is not [x y z qx qy qz qw]"
            ) from None
        if (
            math.dist(values[:3], xyz) > POSE_AGREE_M
            or quat_angle(quat, from_euler) > POSE_AGREE_RAD
        ):
            raise AssistFault(
                "pose_inconsistent",
                "the server pose and the client's Euler angles disagree",
            )
    measured = observation.get("robot_state", {})
    opening = measured.get("gripper_position") if isinstance(measured, dict) else None
    if not (_number(opening) and 0.0 <= opening <= 1.0):
        opening = None
    observed = observation.get("observed_ns")
    return Pose(
        xyz,
        euler,
        quat,
        cmd,
        None if opening is None else float(opening),
        observed if type(observed) is int else None,
    )


# --- the port ---------------------------------------------------------------------------


class AssistPort(Protocol):
    """What the takeover needs of a robot (``fr3_assist.Fr3AssistPort`` is the real
    one). ``window`` is one key's permission to move: a token, one per key, with its
    expiry; the other calls move only inside it."""

    clock: Any  # ``now()`` ns and ``domain``; ``advance(ns)`` waits

    def stopped(self) -> bool: ...  # a stop was registered (checked before each send)

    def begin(self) -> None: ...  # the policy admission gate closes

    def end(
        self, *, reopen: bool
    ) -> None: ...  # hold; the gate reopens or stays closed

    def observe(self) -> dict: ...

    def window(self, label: str): ...  # context manager, one key's token

    def pose_step(self, xyz, euler, gripper_cmd: float) -> dict: ...

    def hold_step(self, xyz, euler, gripper_cmd: float) -> bool: ...  # ignores a stop

    def gripper_step(self, command: float, xyz, euler) -> dict: ...


@dataclass(frozen=True)
class AssistRig:
    """What a run hands the orchestrator for the takeover: the robot port and the
    binding's workspace limits."""

    port: AssistPort
    limits: Limits


# --- the session -------------------------------------------------------------------------


@dataclass(frozen=True)
class Proposal:
    """What a key would do, computed and shown before anything is sent."""

    key: str
    kind: str  # move | close | lift | open | handback | abort
    target: tuple | None = None
    delta: tuple | None = None
    base: tuple | None = None
    checks: dict = field(default_factory=dict)

    def describe(self) -> dict:
        return {
            "key": self.key,
            "kind": self.kind,
            "base": None if self.base is None else [round(v, 5) for v in self.base],
            "target": None
            if self.target is None
            else [round(v, 5) for v in self.target],
            "delta": None if self.delta is None else [round(v, 5) for v in self.delta],
            "checks": dict(self.checks),
        }


def _dist(a, b) -> float:
    return math.dist(a, b)


class AssistSession:
    """One takeover. ``propose(key)`` checks a key against every limit and returns
    what it would do (``AssistRefused`` when it breaks one); ``execute(proposal)``
    sends it. ``handback`` and ``abort`` are decisions: their proposals are returned
    and the orchestrator acts on them."""

    def __init__(
        self,
        port: AssistPort,
        config: GraspAssistConfig,
        limits: Limits,
        band: hd.HoldingConfig,
        *,
        run_id: str,
        sample_id: str,
        policy_epoch: int,
        emit: Callable[..., Any] | None = None,
        object_words: str = "",
    ):
        if not config.enabled:
            raise ValueError("grasp_assist is not enabled")
        self.port, self.config, self.limits, self.band = port, config, limits, band
        self.run_id, self.sample_id, self.policy_epoch = run_id, sample_id, policy_epoch
        self.object_words = object_words
        self._emit = emit or (lambda code, detail, sidecar=None: None)
        entry = pose_of(port.observe())
        self.entry = entry
        # The orientation every command carries: the one measured now, never changed.
        self.euler = entry.euler
        self.quat = entry.quat
        # What this session last commanded: the position it holds, and the gripper.
        self.held_xyz = entry.xyz
        self.commanded = entry.gripper_cmd
        self.steps = 0
        self.misses = 0  # moves in a row that did not arrive
        self.keys = 0  # keys pressed, refused ones too
        self.refused = 0
        self.reader: hd.ObservationReader | None = None
        self.close_ack: tuple[int, int] | None = None  # (reader seq, ms) of the command
        self.candidate: hd.HoldingFact | None = None
        self.last_fact: hd.HoldingFact | None = None
        self.travel_m = 0.0
        self._emit(
            "grasp_assist_step",
            {
                "key": "enter",
                "entry": [round(v, 5) for v in entry.xyz],
                "orientation_quat": [round(v, 6) for v in entry.quat],
                "gripper_cmd": entry.gripper_cmd,
                "opening": entry.opening,
                "config": config.describe(),
            },
        )

    # ------------------------------------------------------------------ views

    def view(self) -> dict:
        """What the console and the page show: where the tool is, how much of the
        budget is left, the measured opening and the fact status."""
        now = pose_of(self.port.observe())
        fact = self.last_fact
        return {
            "xyz": [round(v, 4) for v in now.xyz],
            "entry": [round(v, 4) for v in self.entry.xyz],
            "travel_m": round(_dist(now.xyz, self.entry.xyz), 4),
            "max_travel_m": self.config.max_travel_m,
            "steps": self.steps,
            "max_steps": self.config.max_steps,
            "opening": now.opening,
            "gripper_cmd": self.commanded,
            "fact": None if fact is None else fact.status,
            "fact_stage": None if fact is None else fact.stage,
            "fact_reason": None if fact is None else fact.reason,
            "handoff": self.config.handoff,
        }

    def _clock_ms(self) -> int:
        return self.port.clock.now() // 1_000_000

    def _wait(self, seconds: float) -> None:
        self.port.clock.advance(int(seconds * 1e9))

    # ----------------------------------------------------------------- propose

    def propose(self, key: str) -> Proposal:
        self.keys += 1
        try:
            return self._propose(key)
        except AssistRefused as exc:
            self.refused += 1
            self._emit(
                "grasp_assist_step",
                {"key": key, "kind": "refused", "code": exc.code, "detail": exc.detail},
            )
            raise

    def _propose(self, key: str) -> Proposal:
        if key not in KEYS:
            raise AssistRefused("unknown_key", repr(key)[:40])
        if key == "abort":
            return Proposal(key, "abort")
        if key == "handback":
            return self._propose_handback()
        if self.steps >= self.config.max_steps:
            raise AssistRefused(
                "max_steps_reached", f"{self.steps} of {self.config.max_steps} keys"
            )
        if key == "close":
            return Proposal(key, "close", checks={"command": 0.0})
        if key == "open":
            return Proposal(key, "open", checks={"command": 1.0})
        if key == "lift":
            return self._propose_lift()
        axis = "xyz".index(key[0])
        sign = 1.0 if key[1] == "+" else -1.0
        return self._propose_move(key, "move", axis, sign * self.config.step_m)

    def _propose_move(self, key: str, kind: str, axis: int, delta: float) -> Proposal:
        if not _number(delta) or abs(delta) > MAX_STEP_M + 1e-12:
            raise AssistRefused(
                "step_too_large",
                f"{abs(delta):.4f} m on one axis, the limit is {MAX_STEP_M} m",
            )
        now = pose_of(self.port.observe())
        if now.gripper_cmd != self.commanded:
            # The gripper command the policy would see is not the one this takeover
            # issued (a dropped command): every sub-step would re-send it.
            raise AssistRefused(
                "gripper_state_mismatch",
                f"the client's gripper command is {now.gripper_cmd!r}, this takeover "
                f"commanded {self.commanded!r}: open and close again",
            )
        base = now.xyz
        target = list(base)
        target[axis] += delta
        target = tuple(target)
        floor_ok = target[2] >= self.limits.min_z
        xy_ok = all(abs(v) <= self.limits.max_xy for v in target[:2])
        travel = _dist(target, self.entry.xyz)
        travel_ok = travel <= self.config.max_travel_m
        checks = {
            "z_floor": self.limits.min_z,
            "z_floor_ok": floor_ok,
            "max_xy": self.limits.max_xy,
            "xy_ok": xy_ok,
            "travel_m": round(travel, 4),
            "max_travel_m": self.config.max_travel_m,
            "travel_ok": travel_ok,
            "step_m": abs(delta),
        }
        if not floor_ok:
            raise AssistRefused(
                "z_below_floor",
                f"target z {target[2]:.4f} m is below the floor {self.limits.min_z} m",
            )
        if not xy_ok:
            raise AssistRefused(
                "xy_outside_workspace",
                f"target x {target[0]:.4f} y {target[1]:.4f} is outside "
                f"+-{self.limits.max_xy} m",
            )
        if not travel_ok:
            raise AssistRefused(
                "travel_exceeded",
                f"{travel:.4f} m from the entry point, the limit is "
                f"{self.config.max_travel_m} m",
            )
        return Proposal(
            key,
            kind,
            target=target,
            delta=tuple(t - b for t, b in zip(target, base, strict=True)),
            base=base,
            checks=checks,
        )

    def _propose_lift(self) -> Proposal:
        if self.commanded != 0.0 or self.reader is None or self.close_ack is None:
            raise AssistRefused("lift_needs_close", "close the gripper first")
        return self._propose_move("lift", "lift", 2, LIFT_M)

    def _propose_handback(self) -> Proposal:
        fact = self.last_fact
        if fact is None:
            raise AssistRefused(
                "no_holding_fact", "close the gripper and read its opening first"
            )
        if fact.status in ("empty", "open"):
            raise AssistRefused(
                f"holding_fact_{fact.status}",
                f"the measured opening says the gripper is {fact.status} "
                f"({fact.reason})",
            )
        if fact.status not in READ_STATUSES:
            raise AssistRefused(
                "holding_fact_unproven",
                f"the latest fact is {fact.status} ({fact.reason}): close again",
            )
        if self.config.handoff == "holding_fact" and not self.auto_ready():
            raise AssistRefused(
                "holding_not_verified",
                "this hand-over needs a holding_verified measured fact",
            )
        fresh = self._measure_closed("handback")
        if fresh.status != "holding_candidate":
            raise AssistRefused(
                f"holding_fact_{fresh.status}",
                f"the fresh reading before the hand-back says {fresh.status} "
                f"({fresh.reason})",
            )
        now = pose_of(self.port.observe())
        if self.commanded != 0.0 or now.gripper_cmd != self.commanded:
            raise AssistRefused(
                "handback_state_mismatch",
                f"the policy's state[6] is {now.gripper_cmd!r}, this takeover "
                f"commanded {self.commanded!r}: open and close again",
            )
        state6 = now.gripper_cmd  # the command, never the measured opening
        return Proposal(
            "handback",
            "handback",
            target=now.xyz,
            base=now.xyz,
            checks={
                "fact": fresh.status,
                "opening": now.opening,
                "policy_state6": state6,
                "handoff": self.config.handoff,
            },
        )

    # ----------------------------------------------------------------- execute

    def execute(self, proposal: Proposal) -> dict:
        """Send what ``propose`` returned. A dict with ``ok`` (the key reached what it
        meant to) and the facts; ``AssistFault`` for an anomaly."""
        if proposal.kind in ("handback", "abort"):
            return {"ok": True, "kind": proposal.kind}
        if proposal.key == "lift":
            return self._execute_lift(proposal)
        if proposal.kind == "move":
            result = self._move(proposal)
        elif proposal.kind == "close":
            result = self._close()
        elif proposal.kind == "open":
            result = self._open()
        else:  # pragma: no cover - propose never builds another kind
            raise AssistFault("unknown_proposal", proposal.kind)
        self._after(proposal, result)
        return result

    def _after(self, proposal: Proposal, result: dict) -> None:
        self.steps += 1
        now = pose_of(self.port.observe())
        self.travel_m = _dist(now.xyz, self.entry.xyz)
        self._emit(
            "grasp_assist_step",
            {
                "key": proposal.key,
                "kind": "executed",
                "ok": result.get("ok"),
                "code": result.get("code"),
                "target": None
                if proposal.target is None
                else [round(v, 5) for v in proposal.target],
                "measured": [round(v, 5) for v in now.xyz],
                "follow_error_m": result.get("follow_error_m"),
                "steps": self.steps,
                "travel_m": round(self.travel_m, 4),
                "opening": now.opening,
                "gripper_cmd": now.gripper_cmd,
            },
        )

    def _move(self, proposal: Proposal) -> dict:
        base, target = proposal.base, proposal.target
        with self.port.window(proposal.key):
            try:
                for k in range(1, SUBSTEPS + 1):
                    if k > 1:
                        self._wait(SUBSTEP_PERIOD_S)
                    point = tuple(
                        b + (t - b) * k / SUBSTEPS
                        for b, t in zip(base, target, strict=True)
                    )
                    self.port.pose_step(point, self.euler, self.commanded)
                    self.held_xyz = point
                result = self._settled(target)
            except AssistStopped:
                # Stopped part-way: what is not sent stays unsent, and the tool is
                # held where it is, not at a setpoint it has not reached.
                self._hold_here()
                raise
            if not result["ok"]:
                # Not there: stop pushing. One setpoint where the tool is, in the
                # same window, so an obstacle is not leaned on at the old target.
                # Sent whatever a stop says: it is the safe action.
                self._hold_here()
        if result["ok"]:
            self.misses = 0
        else:
            self.misses += 1
            if self.misses >= self.config.max_follow_misses:
                raise AssistFault(
                    "follow_error_repeated",
                    f"{self.misses} moves in a row did not arrive "
                    f"({result.get('code')}): the arm may be pressing on something",
                )
        return result

    def _hold_here(self) -> None:
        """One setpoint at the measured pose (clamped to the floor and the square),
        not stop-checked: holding where the tool is can only make it safer. A failure
        to send it is never hidden by a stop that is already being handled."""
        here = pose_of(self.port.observe()).xyz
        hold = self._clip(here)
        if self.port.hold_step(hold, self.euler, self.commanded):
            self.held_xyz = hold

    def _clip(self, xyz) -> tuple:
        """A hold setpoint inside the workspace (a tool pressed below the floor is
        held at the floor, never commanded under it)."""
        x, y, z = xyz
        lim = self.limits.max_xy
        return (
            max(-lim, min(lim, x)),
            max(-lim, min(lim, y)),
            max(self.limits.min_z, z),
        )

    def _settled(self, target) -> dict:
        """Wait until the tool is within ``follow_error_m`` of ``target`` (bounded),
        then check the orientation. ``ok: False`` when it did not get there."""
        limit = self.port.clock.now() + int(SETTLE_S * 1e9)
        while True:
            now = pose_of(self.port.observe())
            error = _dist(now.xyz, target)
            if error <= self.config.follow_error_m:
                break
            if self.port.clock.now() >= limit:
                return {
                    "ok": False,
                    "code": "follow_error",
                    "follow_error_m": round(error, 5),
                    "detail": f"{error * 1000:.1f} mm from the target after "
                    f"{SETTLE_S:.0f} s (limit {self.config.follow_error_m * 1000:.1f} mm)",
                }
            self._wait(POLL_S)
        rotated = quat_angle(now.quat, self.quat)
        if rotated > ORIENTATION_TOL_RAD:
            return {
                "ok": False,
                "code": "orientation_drift",
                "follow_error_m": round(error, 5),
                "detail": f"the tool turned {rotated:.3f} rad from the entry's",
            }
        return {"ok": True, "follow_error_m": round(error, 5)}

    def _gripper(self, command: float) -> None:
        """Send the command and wait for the client's own confirmation: a fresh
        observation whose ``state[6]`` is the command. A command the client dropped
        (its rate limit does not record it) is sent again; if it is never taken the
        key is refused and the takeover does not pretend it commanded it."""
        label = "close" if command == 0.0 else "open"
        seen = None
        for attempt in range(GRIPPER_TRIES):
            if self.port.stopped():
                raise AssistStopped("gripper")
            if attempt:
                self._wait(GRIPPER_RETRY_S)
            with self.port.window(label):
                self.port.gripper_step(command, self.held_xyz, self.euler)
            seen = pose_of(self.port.observe()).gripper_cmd
            if seen == command:
                self.commanded = command
                return
        raise AssistRefused(
            "gripper_command_not_taken",
            f"the client's gripper command is still {seen!r} after {GRIPPER_TRIES} "
            f"tries (asked {command!r}); a gripper already past its closed limit "
            "never takes a close: open it, then close",
        )

    def _close(self) -> dict:
        # One reader per grasp: its sequence numbers only grow within it.
        self.reader = hd.ObservationReader(self.port.observe, self.port.clock)
        before = self.reader.read()
        self._gripper(0.0)
        self.close_ack = (before.seq, self._clock_ms())
        fact = self._measure(self.close_ack)
        return {"ok": True, "fact": fact.status}

    def _open(self) -> dict:
        self.reader = hd.ObservationReader(self.port.observe, self.port.clock)
        before = self.reader.read()
        self._gripper(1.0)
        self.candidate = None
        self._measure((before.seq, self._clock_ms()))
        return {"ok": True, "fact": self.last_fact.status}

    @staticmethod
    def _brief(fact: hd.HoldingFact) -> dict:
        """The fact's short form for a journal note (the sidecar has all of it)."""
        return {
            "stage": fact.stage,
            "status": fact.status,
            "reason": fact.reason,
            "source": fact.source,
            "seq": fact.seq,
        }

    def _measure(self, ack: tuple[int, int]) -> hd.HoldingFact:
        """A settled series of readings after the command acknowledged at ``ack`` and
        its fact (``holding.assess_close``); waits while the gripper is still moving,
        for at most ``CLOSE_WAIT_S``."""
        fact = self._series(ack)
        if fact.status == "holding_candidate":
            self.candidate = fact
        else:
            self.candidate = None
        self.last_fact = fact
        self._emit("holding_fact", self._brief(fact), fact.record())
        return fact

    def _series(self, ack: tuple[int, int]) -> hd.HoldingFact:
        assert self.reader is not None
        readings: list[hd.Reading] = []
        limit = self.port.clock.now() + int(CLOSE_WAIT_S * 1e9)
        fact = None
        while True:
            readings.append(self.reader.read())
            # The newest readings that fit one settle window; older ones fall off.
            while (
                len(readings) > 2
                and readings[-1].captured_ms - readings[1].captured_ms
                >= self.band.settle_ms
            ):
                readings.pop(0)
            if len(readings) >= 2:
                fact = hd.assess_close(
                    tuple(readings),
                    self.band,
                    run_id=self.run_id,
                    sample_id=self.sample_id,
                    policy_epoch=self.policy_epoch,
                    now_ms=self._clock_ms(),
                    after_seq=ack[0],
                    after_ms=ack[1],
                )
                waiting = (
                    fact.status == "unknown" and fact.reason in WAIT_REASONS
                ) or (fact.status == "refused" and fact.reason == "observation_not_new")
                if not waiting:
                    return fact
            if self.port.clock.now() >= limit:
                if fact is None:
                    raise AssistFault(
                        "measurement_unavailable",
                        "no gripper reading after the command",
                    )
                return fact
            self._wait(POLL_S)

    def _measure_closed(self, label: str) -> hd.HoldingFact:
        """A fresh settled series of the closed gripper (the candidate of a lift, the
        proof of a hand-back): readings newer than the last command's
        acknowledgement and than the last reading taken."""
        if self.reader is None or self.close_ack is None:
            raise AssistRefused("no_holding_fact", "close the gripper first")
        before = self.reader.read()
        ack = (before.seq, self._clock_ms())
        fact = self._series(ack)
        self.last_fact = fact
        if fact.status == "holding_candidate":
            self.candidate = fact
        self._emit("holding_fact", {**self._brief(fact), "check": label}, fact.record())
        return fact

    def _execute_lift(self, proposal: Proposal) -> dict:
        # The candidate the lift is compared with is taken now, seconds before it.
        candidate = self._measure_closed("lift")
        if candidate.status != "holding_candidate":
            raise AssistRefused(
                "lift_needs_holding_candidate",
                f"the closed gripper reads {candidate.status} ({candidate.reason}): "
                "nothing was lifted",
            )
        result = self._move(proposal)
        if not result["ok"]:
            self._after(proposal, result)
            return result
        assert self.reader is not None
        first = self.reader.read()
        self._wait(POLL_S)
        second = self.reader.read()
        fact = hd.assess_lift(
            candidate, first, second, self.band, now_ms=self._clock_ms()
        )
        self.last_fact = fact
        self._emit("holding_fact", self._brief(fact), fact.record())
        result = {**result, "fact": fact.status}
        self._after(proposal, result)
        return result

    # --------------------------------------------------------------- hand-over

    def auto_ready(self) -> bool:
        """The planned ``handoff: holding_fact`` level: the latest fact is a
        ``holding_verified`` one whose source is ``verified_measurement``
        (``holding.require_real``). Never True with ``handoff: operator``."""
        if self.config.handoff != "holding_fact":
            return False
        fact = self.last_fact
        if fact is None or fact.status != "holding_verified":
            return False
        try:
            hd.require_real(fact)
        except ValueError:
            return False
        return fact.holding_verified is True

    def finalize_handback(self, proposal: Proposal) -> dict:
        """Right before the gate reopens: a fresh observation says the policy's
        ``state[6]`` is the command this takeover issued (0.0) and not anything
        measured. ``AssistRefused`` otherwise (the takeover goes on)."""
        now = pose_of(self.port.observe())
        state6 = now.gripper_cmd  # ``state[6]``: the command the client last issued
        if self.commanded != 0.0 or state6 != self.commanded:
            raise AssistRefused(
                "handback_state_mismatch",
                f"the policy's state[6] is {state6!r}, this takeover commanded "
                f"{self.commanded!r}",
            )
        checks = {**proposal.checks, "opening": now.opening, "policy_state6": state6}
        return self.handback_record(
            Proposal(
                proposal.key,
                proposal.kind,
                target=now.xyz,
                base=now.xyz,
                checks=checks,
            )
        )

    def handback_record(self, proposal: Proposal) -> dict:
        """The journal's ``grasp_assist_handoff`` facts of this takeover."""
        fact = self.last_fact
        return {
            "by": "holding_fact"
            if self.config.handoff == "holding_fact"
            else "operator",
            "handoff": self.config.handoff,
            "steps": self.steps,
            "keys": self.keys,
            "travel_m": round(self.travel_m, 4),
            "fact_stage": None if fact is None else fact.stage,
            "fact_status": None if fact is None else fact.status,
            "fact_seq": None if fact is None else fact.seq,
            "fact_captured_ms": None if fact is None else fact.captured_ms,
            "fact_source": None if fact is None else fact.source,
            "opening": proposal.checks.get("opening"),
            "policy_state6": proposal.checks.get("policy_state6"),
            "policy_epoch_before": self.policy_epoch,
            "env_reset": False,
            "homed": False,
        }
