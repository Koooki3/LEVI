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
axis, upper case the negative direction; ``r R`` turn the tool about the vertical axis
one ``yaw_step_rad`` (at most 0.35 rad), ``R`` the negative direction; ``c`` close; ``u``
lift 2 cm; ``o`` open (retry); ``g`` hand back to the reset policy; ``a`` abort (hold,
no home, a person). With ``grasp_source: auto`` the keys come from
``auto_grasp.AutoGraspGate`` (perception) instead of a person; every limit below is the
same.

Limits, all from the job and the binding, none negotiable at run time:

- one move is at most ``MAX_STEP_M`` (0.02 m) per axis and is sent as ``SUBSTEPS`` small
  steps; the orientation is the one measured at the state's entry, except for its yaw
  (rz), which only the yaw keys change (``r`` / ``R``: ``yaw_step_rad`` per key, at most
  ``max_yaw_rad`` in all from the entry yaw, always inside the binding's ``rz_range``;
  rx and ry stay at their entry values);
- the target stays at or above the binding's ``min_z``, inside its ``max_xy``, within
  ``max_travel_m`` of the entry point, and at most ``max_steps`` keys are executed;
- after each move the tool is within ``follow_error_m`` of its target (default 8 mm,
  uncalibrated) and its orientation within ``ORIENTATION_TOL_RAD`` of the CURRENT
  target orientation (the entry's until a yaw key turns it).

A key that breaks a limit is refused before anything is sent and nothing latches
(``AssistRefused``: a person's mistake). A comms or measurement anomaly (an
unreadable or inconsistent observation, a robot that latched, a command the server
did not acknowledge) is ``AssistFault``: the run locks. A move that was sent and did
not reach its target is reported (``ok: false``) and one setpoint at the measured pose
is sent in the same window (the arm does not lean on an obstacle at the old target);
nothing latches, and ``max_follow_misses`` of them in a row end the takeover with the
arm held for a person (``AssistHeld``, ``follow_error_repeated``: WAIT_HUMAN, no lock). The sub-steps
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
from .fr3_bridge import clip_gripper_reading

# --- keys -----------------------------------------------------------------------------

KEYS = (
    "x+",
    "x-",
    "y+",
    "y-",
    "z+",
    "z-",
    "r+",
    "r-",
    "close",
    "lift",
    "open",
    "handback",
    "abort",
)
MOVE_KEYS = frozenset({"x+", "x-", "y+", "y-", "z+", "z-"})
# Turn the tool about the vertical axis (rz of the Euler angles): ``r`` +, ``R`` -.
YAW_KEYS = frozenset({"r+", "r-"})
# Executed keys (counted against ``max_steps``): the others decide, they do not act.
ACTING_KEYS = MOVE_KEYS | YAW_KEYS | {"close", "lift", "open"}
KEY_CHARS = {
    "x": "x+",
    "X": "x-",
    "y": "y+",
    "Y": "y-",
    "z": "z+",
    "Z": "z-",
    "r": "r+",
    "R": "r-",
    "c": "close",
    "u": "lift",
    "o": "open",
    "g": "handback",
    "a": "abort",
}

# --- limits and defaults --------------------------------------------------------------

MAX_STEP_M = aeri.GRASP_ASSIST_MAX_STEP_M  # per axis, per key: hard, not configurable
DEFAULT_STEP_M = 0.01
# One yaw key turns the tool this far at most (hard) and by default 0.15 rad (8.6
# degrees); the whole takeover turns at most ``max_yaw_rad`` from the entry yaw (hard
# cap 1.6, default 1.57 = 90 degrees: the gripper is symmetric). Sent as SUBSTEPS.
MAX_YAW_STEP_RAD = aeri.GRASP_ASSIST_MAX_YAW_STEP_RAD
MAX_YAW_RAD = aeri.GRASP_ASSIST_MAX_YAW_RAD
DEFAULT_YAW_STEP_RAD = 0.15
DEFAULT_MAX_YAW_RAD = 1.57
DEFAULT_AUTO_MAX_RETRIES = 2
DEFAULT_LOCATOR_COORDS = "norm1000"
LIFT_M = 0.02
SUBSTEPS = 4
DEFAULT_MAX_STEPS = 40
DEFAULT_MAX_TRAVEL_M = 0.35
# RELAXED[2026-11-17] (user: "要求不要太严格"; was: 0.005). The domain's own
# bound for the job key stays 0.01 (levi/domain/aeri.py).
DEFAULT_FOLLOW_ERROR_M = 0.008
# How far the tool may turn from its entry orientation during a takeover move.
# Not the home tolerance: it stays 0.05 whatever ``safety.home_rotation_rad`` says
# (decided 2026-11-17; the home relaxation is about verifying the home pose).
ORIENTATION_TOL_RAD = 0.05
# The lift's candidate is at most this old when the lift readings are taken
# (holding.HoldingConfig.max_gap_ms; uncalibrated, V2).
LIFT_MAX_GAP_MS = 10_000
# Most a move or a gripper waits to settle. RELAXED[2026-11-17] (was: 3.0).
SETTLE_S = 5.0
# The sub-steps of one move are sent this far apart (as the lab takeover tool did),
# not back to back: four sub-steps sent at once are one 2 cm jump for the controller.
SUBSTEP_PERIOD_S = 0.1
# A move that did not arrive holds the arm where it is; this many such moves in a
# row lock the run (the arm is pressing on something). Uncalibrated.
# RELAXED[2026-11-17] (was: 2).
DEFAULT_MAX_FOLLOW_MISSES = 3
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


class AssistHeld(Exception):
    """Moves kept missing their targets (``follow_error_repeated``): the arm is
    held where it is (``_hold_here`` ran before this is raised) and the takeover
    is abandoned for a person, like a stop: no home, not a fault, nothing locks.
    RELAXED[2026-11-17] (was: ``AssistFault``, which locked the run)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


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
    # The yaw keys (r / R) are off unless the job turns them on: a new motion path is
    # never available to a job that was reviewed without it. One key's turn (rad) and
    # the most the takeover turns in all are the settings below.
    yaw_enabled: bool = False
    yaw_step_rad: float = DEFAULT_YAW_STEP_RAD
    max_yaw_rad: float = DEFAULT_MAX_YAW_RAD
    # ``grasp_source: auto`` only: ``<object words>|<z>`` entries, sorted (the tool
    # height at which the gripper closes on that object), and how many times a grasp
    # that did not hold is retried before the takeover is abandoned.
    grasp_height: tuple = ()
    auto_max_retries: int = DEFAULT_AUTO_MAX_RETRIES
    # ``grasp_source: auto``: how the locator's coordinates are read (pixel, or the
    # 0..1000 grid Qwen3-VL answers in): the 11-18 offline replay of real wrist frames
    # answered on the grid in 11 of 11 frames, so the grid is the default.
    locator_coords: str = DEFAULT_LOCATOR_COORDS
    # ``grasp_source: auto``: the pixel in the wrist image where the object must be
    # before the descent = the midpoint between the fingertips at the registered grasp
    # height, measured offline (None: the image centre).
    grasp_pixel: tuple | None = None

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
        if type(self.yaw_enabled) is not bool:
            raise ValueError("grasp_assist.yaw_enabled is true or false")
        if not _number(self.yaw_step_rad) or not 0 < self.yaw_step_rad <= (
            MAX_YAW_STEP_RAD
        ):
            raise ValueError(
                f"grasp_assist.yaw_step_rad is above 0 and at most {MAX_YAW_STEP_RAD}"
            )
        if not _number(self.max_yaw_rad) or not 0 < self.max_yaw_rad <= MAX_YAW_RAD:
            raise ValueError(
                f"grasp_assist.max_yaw_rad is above 0 and at most {MAX_YAW_RAD}"
            )
        if type(self.auto_max_retries) is not int or not (
            0 <= self.auto_max_retries <= 5
        ):
            raise ValueError(
                "grasp_assist.auto_max_retries is a whole number from 0 to 5"
            )
        if self.locator_coords not in aeri.LOCATOR_COORDS:
            raise ValueError(
                "grasp_assist.locator_coords is one of "
                + ", ".join(aeri.LOCATOR_COORDS)
            )
        if isinstance(self.grasp_pixel, list):
            object.__setattr__(self, "grasp_pixel", tuple(self.grasp_pixel))
        if self.grasp_pixel is not None and not (
            isinstance(self.grasp_pixel, tuple)
            and len(self.grasp_pixel) == 2
            and all(_number(v) and 0 <= v <= 10000 for v in self.grasp_pixel)
        ):
            raise ValueError(
                "grasp_assist.grasp_pixel is [x, y], two pixel coordinates from 0 to 10000"
            )
        height = self.grasp_height
        if isinstance(height, list):
            height = tuple(sorted(height))
            object.__setattr__(self, "grasp_height", height)
        if not isinstance(height, tuple) or not all(isinstance(b, str) for b in height):
            raise ValueError("grasp_assist.grasp_height is a list of text entries")
        if self.grasp_source == "auto" and self.handoff != "holding_fact":
            raise ValueError(
                "grasp_assist.grasp_source: auto_needs_holding_fact: the automatic "
                "grasp hands over by the measured holding fact (handoff: "
                "holding_fact)"
            )
        band = self.holding_band
        if isinstance(band, list):
            band = tuple(sorted(band))
            object.__setattr__(self, "holding_band", band)
        if not isinstance(band, tuple) or not all(isinstance(b, str) for b in band):
            raise ValueError("grasp_assist.holding_band is a list of text entries")
        if self.enabled:
            self.bands()  # refused whole when malformed; none, none to use
            heights = self.grasp_heights()
            if self.grasp_source == "auto" and not heights:
                raise ValueError(
                    "grasp_assist.grasp_height: the automatic grasp needs the tool "
                    "height to close at, one '<object words>|<z>' entry per object"
                )

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

    def grasp_heights(self) -> dict[str, float]:
        """``{object words: z}`` from the ``grasp_height`` entries (metres, above 0
        and below 1; one entry per object: ``aeri.parse_grasp_heights``)."""
        return aeri.parse_grasp_heights(self.grasp_height)

    def describe(self) -> dict:
        """The plan's form of the settings (every resolved value). The settings
        added with the yaw keys and the automatic source appear only when they differ
        from their defaults: a job that never used them keeps its plan digest."""
        extra: dict = {}
        if self.yaw_enabled:
            extra["yaw_enabled"] = True
        if self.yaw_step_rad != DEFAULT_YAW_STEP_RAD:
            extra["yaw_step_rad"] = self.yaw_step_rad
        if self.max_yaw_rad != DEFAULT_MAX_YAW_RAD:
            extra["max_yaw_rad"] = self.max_yaw_rad
        if self.grasp_height:
            extra["grasp_height"] = list(self.grasp_height)
        if self.auto_max_retries != DEFAULT_AUTO_MAX_RETRIES:
            extra["auto_max_retries"] = self.auto_max_retries
        if self.locator_coords != DEFAULT_LOCATOR_COORDS:
            extra["locator_coords"] = self.locator_coords
        if self.grasp_pixel is not None:
            extra["grasp_pixel"] = list(self.grasp_pixel)
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
            **extra,
        }


@dataclass(frozen=True)
class Limits:
    """The binding's workspace (``binding.safety``): a floor and a square. Both are
    needed: a job without a floor cannot run a takeover."""

    min_z: float
    max_xy: float
    # The binding's rz interval (``safety.rz_range``): a yaw key that would leave it
    # is refused. None: no interval known (a yaw key then only has ``max_yaw_rad``).
    rz_range: tuple | None = None

    def __post_init__(self):
        if not (_number(self.min_z) and self.min_z > 0):
            raise ValueError("assist needs a positive min_z (a floor)")
        if not (_number(self.max_xy) and self.max_xy > 0):
            raise ValueError("assist needs a positive max_xy")
        if self.rz_range is not None and not (
            len(self.rz_range) == 2
            and all(_number(v) for v in self.rz_range)
            and self.rz_range[0] < self.rz_range[1]
        ):
            raise ValueError("assist rz_range is an increasing pair of angles")

    def rz_ok(self, rz: float) -> bool:
        """``rz`` is inside the binding's rz interval, compared as the binding does
        (modulo 2 pi: ``(rz - lo) % 2pi <= hi - lo``, ``fr3_run`` home check), so an
        interval across +-pi works."""
        if self.rz_range is None:
            return True
        lo, hi = self.rz_range
        return (rz - lo) % (2 * math.pi) <= hi - lo + 1e-6

    def rz_nearest(self, rz: float) -> float:
        """The point of the binding's rz interval nearest ``rz``, unwrapped to the turn
        of ``rz`` (``rz`` itself when it is inside or there is no interval)."""
        if self.rz_range is None or self.rz_ok(rz):
            return rz
        turn = 2 * math.pi
        edges = [e + turn * round((rz - e) / turn) for e in self.rz_range]
        return min(edges, key=lambda e: abs(e - rz))

    @classmethod
    def from_safety(cls, safety: dict) -> Limits:
        rz = safety.get("rz_range")
        return cls(
            float(safety["min_z"]),
            float(safety["max_xy"]),
            None if rz is None else (float(rz[0]), float(rz[1])),
        )


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
    # RELAXED[2026-11-17] (was: exactly 0..1): a value a hair outside is clipped.
    clipped_cmd = clip_gripper_reading(cmd)
    if clipped_cmd is None:
        raise AssistFault("observation_invalid", f"state[6]={cmd!r} is not 0..1")
    cmd = clipped_cmd
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
    # RELAXED[2026-11-17] (was: exactly 0..1): clipped when a hair outside.
    opening = clip_gripper_reading(opening) if _number(opening) else None
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
    # The orientation (Euler angles) the key leaves the tool at: set by a yaw key only
    # (the session's current target orientation otherwise).
    euler: tuple | None = None

    def describe(self) -> dict:
        out = {
            "key": self.key,
            "kind": self.kind,
            "base": None if self.base is None else [round(v, 5) for v in self.base],
            "target": None
            if self.target is None
            else [round(v, 5) for v in self.target],
            "delta": None if self.delta is None else [round(v, 5) for v in self.delta],
            "checks": dict(self.checks),
        }
        if self.euler is not None:
            out["euler"] = [round(v, 5) for v in self.euler]
        return out


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
        # The orientation every command carries: the one measured now. Only a yaw key
        # changes it (rz), and then it is the CURRENT target orientation the drift
        # check compares with; ``entry`` keeps the orientation measured at the start.
        self.euler = entry.euler
        self.quat = entry.quat
        # The furthest |rz - entry rz| ever SENT: the yaw budget. It is kept apart from
        # the target (a hold at the measured yaw moves the target back, never this).
        self.yaw_reach = 0.0
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
            "yaw_rad": round(self.euler[2] - self.entry.euler[2], 4),
            "object": self.object_words,
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
        if key in YAW_KEYS:
            if not self.config.yaw_enabled:
                raise AssistRefused(
                    "yaw_not_enabled",
                    "this job did not turn the yaw keys on (grasp_assist.yaw_enabled)",
                )
            sign = 1.0 if key[1] == "+" else -1.0
            return self._propose_yaw(key, sign * self.config.yaw_step_rad)
        axis = "xyz".index(key[0])
        sign = 1.0 if key[1] == "+" else -1.0
        return self._propose_move(key, "move", axis, sign * self.config.step_m)

    def _propose_move(self, key: str, kind: str, axis: int, delta: float) -> Proposal:
        if not _number(delta) or abs(delta) > MAX_STEP_M + 1e-12:
            raise AssistRefused(
                "step_too_large",
                f"{abs(delta):.4f} m on one axis, the limit is {MAX_STEP_M} m",
            )
        now = self._observe_for_move()
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

    def _observe_for_move(self) -> Pose:
        now = pose_of(self.port.observe())
        if now.gripper_cmd != self.commanded:
            # The gripper command the policy would see is not the one this takeover
            # issued (a dropped command): every sub-step would re-send it.
            raise AssistRefused(
                "gripper_state_mismatch",
                f"the client's gripper command is {now.gripper_cmd!r}, this takeover "
                f"commanded {self.commanded!r}: open and close again",
            )
        return now

    def _propose_yaw(self, key: str, delta: float) -> Proposal:
        """Turn the tool ``delta`` rad about the vertical axis (rz of the Euler
        angles), the position held. Refused before anything is sent when the turn is
        larger than the hard cap, or would take the total turn from the entry yaw past
        ``max_yaw_rad`` or rz outside the binding's ``rz_range``."""
        if not _number(delta) or abs(delta) > MAX_YAW_STEP_RAD + 1e-12:
            raise AssistRefused(
                "yaw_step_too_large",
                f"{abs(delta):.4f} rad in one key, the limit is {MAX_YAW_STEP_RAD} rad",
            )
        now = self._observe_for_move()
        rx, ry, rz = self.euler
        target_rz = rz + delta
        total = target_rz - self.entry.euler[2]
        yaw_ok = max(self.yaw_reach, abs(total)) <= self.config.max_yaw_rad + 1e-9
        rng = self.limits.rz_range
        range_ok = self.limits.rz_ok(target_rz)
        checks = {
            "yaw_target_rad": round(target_rz, 5),
            "yaw_from_entry_rad": round(total, 5),
            "max_yaw_rad": self.config.max_yaw_rad,
            "yaw_ok": yaw_ok,
            "rz_range": None if rng is None else list(rng),
            "rz_range_ok": range_ok,
            "yaw_step_rad": abs(delta),
        }
        if not yaw_ok:
            raise AssistRefused(
                "yaw_exceeded",
                f"{abs(total):.4f} rad from the entry yaw, the limit is "
                f"{self.config.max_yaw_rad} rad",
            )
        if not range_ok:
            raise AssistRefused(
                "rz_outside_range",
                f"target rz {target_rz:.4f} rad is outside the binding's rz range "
                f"{list(rng)}",
            )
        return Proposal(
            key,
            "yaw",
            target=now.xyz,
            base=now.xyz,
            delta=(0.0, 0.0, 0.0),
            checks=checks,
            euler=(rx, ry, target_rz),
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
        if proposal.kind in ("move", "yaw"):
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
                **(
                    {"yaw_from_entry_rad": proposal.checks.get("yaw_from_entry_rad")}
                    if proposal.kind == "yaw"
                    else {}
                ),
            },
        )

    def _move(self, proposal: Proposal) -> dict:
        base, target = proposal.base, proposal.target
        turn = proposal.euler is not None and proposal.euler != self.euler
        start_euler = self.euler
        with self.port.window(proposal.key):
            try:
                for k in range(1, SUBSTEPS + 1):
                    if k > 1:
                        self._wait(SUBSTEP_PERIOD_S)
                    point = tuple(
                        b + (t - b) * k / SUBSTEPS
                        for b, t in zip(base, target, strict=True)
                    )
                    euler = self.euler
                    if turn:
                        euler = tuple(
                            a + (b - a) * k / SUBSTEPS
                            for a, b in zip(start_euler, proposal.euler, strict=True)
                        )
                    self.port.pose_step(point, euler, self.commanded)
                    self.held_xyz = point
                    if turn:
                        # What was sent is the target now (a stop part-way leaves
                        # the arm held at the orientation it was last told).
                        self.euler, self.quat = euler, euler_to_quat(*euler)
                        self.yaw_reach = max(
                            self.yaw_reach, abs(euler[2] - self.entry.euler[2])
                        )
                result = (
                    self._settled(target, wait_orientation=True)
                    if turn
                    else self._settled(target)
                )
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
                raise AssistHeld(
                    "follow_error_repeated",
                    f"{self.misses} moves in a row did not arrive "
                    f"({result.get('code')}): the arm may be pressing on something",
                )
        return result

    def _hold_here(self) -> None:
        """One setpoint at the measured pose (clamped to the floor and the square),
        not stop-checked: holding where the tool is can only make it safer. A failure
        to send it is never hidden by a stop that is already being handled."""
        now = pose_of(self.port.observe())
        hold = self._clip(now.xyz)
        if self.yaw_reach > 0:
            self._adopt_measured_yaw(now)
        if self.port.hold_step(hold, self.euler, self.commanded):
            self.held_xyz = hold

    def _adopt_measured_yaw(self, now: Pose) -> None:
        """The orientation of a hold: the entry's rx and ry and the MEASURED rz, which
        becomes the session's target. A yaw that did not arrive (a finger on the rim)
        or was stopped part-way must not leave its target as the arm's setpoint: the
        impedance controller keeps the last setpoint, so the tool would keep twisting
        against the obstacle after the takeover was abandoned. The measured rz is
        taken at the turn nearest the target and kept inside the entry yaw plus or
        minus ``max_yaw_rad`` and the binding's ``rz_range``; ``yaw_reach`` (the budget)
        is not touched."""
        rx, ry, target_rz = self.euler
        rz = now.euler[2]
        rz += 2 * math.pi * round((target_rz - rz) / (2 * math.pi))
        entry_rz = self.entry.euler[2]
        rz = max(
            entry_rz - self.config.max_yaw_rad,
            min(entry_rz + self.config.max_yaw_rad, rz),
        )
        if not self.limits.rz_ok(rz):
            # Measured just outside the binding's interval: hold at the NEAREST edge,
            # never jump back to the entry yaw (that was one unsubdivided turn of up to
            # the whole budget, sent even after a stop). The hold is then at most as far
            # from the measured yaw as the measured yaw is outside the interval.
            rz = self.limits.rz_nearest(rz)
        self.euler = (rx, ry, rz)
        self.quat = euler_to_quat(*self.euler)

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

    def _settled(self, target, *, wait_orientation: bool = False) -> dict:
        """Wait until the tool is within ``follow_error_m`` of ``target`` (bounded),
        then check the orientation against the CURRENT target orientation
        (``self.quat``: the entry's until a yaw key turns it). After a yaw key
        (``wait_orientation``) the orientation is waited for too: the arm is still
        turning when the position is already there. ``ok: False`` when it did not
        get there."""
        limit = self.port.clock.now() + int(SETTLE_S * 1e9)
        while True:
            now = pose_of(self.port.observe())
            error = _dist(now.xyz, target)
            rotated = quat_angle(now.quat, self.quat)
            if error <= self.config.follow_error_m and (
                not wait_orientation or rotated <= ORIENTATION_TOL_RAD
            ):
                break
            if self.port.clock.now() >= limit:
                if error > self.config.follow_error_m:
                    return {
                        "ok": False,
                        "code": "follow_error",
                        "follow_error_m": round(error, 5),
                        "detail": f"{error * 1000:.1f} mm from the target after "
                        f"{SETTLE_S:.0f} s (limit "
                        f"{self.config.follow_error_m * 1000:.1f} mm)",
                    }
                break  # on target; the orientation check below says what is wrong
            self._wait(POLL_S)
        if rotated > ORIENTATION_TOL_RAD:
            return {
                "ok": False,
                "code": "orientation_drift",
                "follow_error_m": round(error, 5),
                "detail": f"the tool is {rotated:.3f} rad from its target "
                f"orientation (limit {ORIENTATION_TOL_RAD} rad)",
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
            "by": "auto"
            if self.config.grasp_source == "auto"
            else "holding_fact"
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
