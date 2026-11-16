"""Measured holding fact and drop detector for a real gripper (no I/O, no robot).

The re-grasp gate (``regrasp_gate``) never infers holding from a gripper width;
the caller must hand it independently measured facts. This module is that
producer for a real gripper. It reads *measured* openings only (robot-server
``gripper_pos``, 1.0 = open), never the commanded value (the policy's
``state[6]`` is the command and cannot notice an empty grasp or a drop).

A fact is self-contained evidence a gate can act on: the readings, the band and
thresholds used, the identity, the capture time and sequence, and a ``source``
that tells ``verified_measurement`` (a real robot, a configured per-object
band) from ``synthetic_measurement`` (offline fixtures). Stages:

1. ``assess_close``: two measured readings after a confirmed close, both fresh
   (at most ``max_age_ms`` before use, the offline handoff contract's 100 ms),
   steady (differ by at most ``GRIPPER_STABLE_DELTA``) and inside the object's
   holding band -> ``holding_candidate``. Below the band -> ``empty``; at or
   above ``open_min`` -> ``open``; jitter, a missing value or a value between
   the band and ``open_min`` -> ``unknown``; stale or not-newer -> ``refused``.
2. ``assess_lift``: after a measured lift of at least ``lift_min_m`` a newer,
   fresh reading still inside the band -> ``holding_verified``. This is signal
   level evidence: it does not see the object, and a person (or a later level
   of the full-auto ladder) still decides the hand back.

The band is per object and comes from configuration (job or site); no object's
numbers are built in. Synthetic thresholds (open >= 0.9, closed <= 0.3 of the
offline harness) exist only in ``synthetic_config`` and a real source refuses
them: 0.2986 (an eggplant held, 2026-11-14) differs from 0.3 by 0.0014.

``DropDetector`` turns the same measured opening into an ``object_dropped``
signal: the command says closed, the measured opening was inside the holding
band and has fallen below it for a few consecutive samples. It only informs; it
never triggers motion.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .fr3_bridge import GRIPPER_STABLE_DELTA, HOME_GRIPPER_MIN
from .regrasp_gate import SOURCES, GateObservation

SCHEMA = "levi.holding_fact.v1"
SOURCE_VERIFIED = "verified_measurement"
SOURCE_SYNTHETIC = "synthetic_measurement"
# Same bound as ``offline_harness.MAX_OBSERVATION_AGE_MS`` and the gate's
# ``max_age_ms`` default (a test keeps the three equal); a config may only
# tighten it.
MAX_AGE_MS = 100
DEFAULT_LIFT_MIN_M = 0.02
# Offline-harness thresholds. Used by ``synthetic_config`` and nowhere else.
SYNTHETIC_OPEN_MIN = 0.9
SYNTHETIC_CLOSED_MAX = 0.3

STATUSES = frozenset(
    {"holding_candidate", "holding_verified", "empty", "open", "unknown", "refused"}
)


def _identity(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 256


def _number(value: object) -> bool:
    return (
        type(value) in (int, float)
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _opening(value: object) -> bool:
    return _number(value) and 0 <= value <= 1


@dataclass(frozen=True)
class HoldingConfig:
    """One object's holding band and the limits of the assessment.

    ``lo``/``hi``: measured openings (1.0 = open) at which this object counts
    as held; below ``lo`` the gripper is empty. A real source needs
    ``0 < lo < hi < open_min`` and may not reuse the synthetic closed limit.
    ``open_min`` defaults to the bridge's reviewed home-open limit.
    """

    object_id: str
    lo: float
    hi: float
    source: str = SOURCE_VERIFIED
    open_min: float = HOME_GRIPPER_MIN
    stable_delta: float = GRIPPER_STABLE_DELTA
    lift_min_m: float = DEFAULT_LIFT_MIN_M
    max_age_ms: int = MAX_AGE_MS

    def __post_init__(self):
        synthetic = self.source == SOURCE_SYNTHETIC
        if (
            not _identity(self.object_id)
            or self.source not in SOURCES
            or not all(
                _opening(x)
                for x in (self.lo, self.hi, self.open_min)  # type: ignore[arg-type]
            )
            or not self.lo < self.hi < self.open_min
            or (not synthetic and self.lo <= 0)
            or not _number(self.stable_delta)
            or not 0 < self.stable_delta <= 0.1
            or not _number(self.lift_min_m)
            or not 0 < self.lift_min_m <= 0.2
            or type(self.max_age_ms) is not int
            or not 1 <= self.max_age_ms <= MAX_AGE_MS
        ):
            raise ValueError("invalid holding configuration")
        if not synthetic and self.hi == SYNTHETIC_CLOSED_MAX:
            raise ValueError(
                "a real holding band may not reuse the synthetic closed limit"
            )

    @property
    def band(self) -> tuple[float, float]:
        return (self.lo, self.hi)

    def classify(self, value: float) -> str:
        """``open`` / ``empty`` / ``holding`` / ``between`` for one opening."""
        if value >= self.open_min:
            return "open"
        if value < self.lo:
            return "empty"
        if value <= self.hi:
            return "holding"
        return "between"


def synthetic_config(object_id: str = "synthetic-object") -> HoldingConfig:
    """The offline harness's thresholds (open >= 0.9, closed <= 0.3). Source is
    always ``synthetic_measurement``; no real source may carry these."""
    return HoldingConfig(
        object_id,
        0.0,
        SYNTHETIC_CLOSED_MAX,
        source=SOURCE_SYNTHETIC,
        open_min=SYNTHETIC_OPEN_MIN,
    )


def parse_holding_bands(
    raw: object, *, source: str = SOURCE_VERIFIED, **limits: Any
) -> dict[str, HoldingConfig]:
    """``{object_id: [lo, hi]}`` from a job or site file -> configs. Anything
    else is refused whole; an empty mapping is refused too (no band, no fact)."""
    if not isinstance(raw, Mapping) or not raw or len(raw) > 64:
        raise ValueError("holding_band is a nonempty {object: [lo, hi]} mapping")
    out = {}
    for name, pair in raw.items():
        if (
            not isinstance(pair, (list, tuple))
            or len(pair) != 2
            or not all(_number(x) for x in pair)
        ):
            raise ValueError(f"holding_band[{name!r}] is [lo, hi]")
        out[name] = HoldingConfig(
            name, float(pair[0]), float(pair[1]), source=source, **limits
        )
    return out


@dataclass(frozen=True)
class Reading:
    """One measured sample: opening (None = missing) and tool height in metres
    (None = missing), with its sequence number and capture time."""

    seq: int
    captured_ms: int
    gripper: float | None
    z_m: float | None = None

    def __post_init__(self):
        if (
            type(self.seq) is not int
            or self.seq < 1
            or type(self.captured_ms) is not int
            or self.captured_ms < 0
        ):
            raise ValueError("invalid reading identity")


@dataclass(frozen=True)
class HoldingFact:
    """Self-contained evidence of one stage; ``record()`` is its JSON form."""

    run_id: str
    sample_id: str
    policy_epoch: int
    source: str
    object_id: str
    band: tuple[float, float]
    open_min: float
    stable_delta: float
    lift_min_m: float
    stage: str  # "close" or "lift"
    status: str
    reason: str
    seq: int
    captured_ms: int
    readings: tuple[Reading, ...]
    lift_m: float | None = None
    empty_verified: bool | None = None
    open_measured: bool | None = None
    closed_measured: bool | None = None
    holding_verified: bool | None = None

    @property
    def is_real(self) -> bool:
        return self.source == SOURCE_VERIFIED

    @property
    def usable(self) -> bool:
        return self.status != "refused"

    def record(self) -> dict:
        return {
            "schema": SCHEMA,
            "run_id": self.run_id,
            "sample_id": self.sample_id,
            "policy_epoch": self.policy_epoch,
            "source": self.source,
            "object_id": self.object_id,
            "band": list(self.band),
            "open_min": self.open_min,
            "stable_delta": self.stable_delta,
            "lift_min_m": self.lift_min_m,
            "stage": self.stage,
            "status": self.status,
            "reason": self.reason,
            "seq": self.seq,
            "captured_ms": self.captured_ms,
            "readings": [
                {
                    "seq": r.seq,
                    "captured_ms": r.captured_ms,
                    "gripper": r.gripper,
                    "z_m": r.z_m,
                }
                for r in self.readings
            ],
            "lift_m": self.lift_m,
            "empty_verified": self.empty_verified,
            "open_measured": self.open_measured,
            "closed_measured": self.closed_measured,
            "holding_verified": self.holding_verified,
            "physical_allowed": False,
        }

    def gate_observation(
        self,
        *,
        approach_verified: bool | None = None,
        at_grasp_pose: bool | None = None,
    ) -> GateObservation | None:
        """The ``regrasp_gate`` observation of this fact, or None for a refused
        one (a stale or not-newer reading must not reach the gate at all). The
        approach/grasp-pose facts are not measured here: the caller supplies
        them from its own verified geometry (or a person's confirmation)."""
        if not self.usable:
            return None
        return GateObservation(
            run_id=self.run_id,
            sample_id=self.sample_id,
            policy_epoch=self.policy_epoch,
            seq=self.seq,
            captured_ms=self.captured_ms,
            empty_verified=self.empty_verified,
            open_measured=self.open_measured,
            approach_verified=approach_verified,
            at_grasp_pose=at_grasp_pose,
            holding_verified=self.holding_verified,
            source=self.source,
            closed_measured=self.closed_measured,
        )


def require_real(fact: HoldingFact) -> HoldingFact:
    """For a gate that hands authority to the fact: only a real measurement."""
    if not fact.is_real:
        raise ValueError(f"holding fact source {fact.source!r} is not a real robot")
    return fact


def _fact(
    config: HoldingConfig,
    ids: tuple[str, str, int],
    stage: str,
    status: str,
    reason: str,
    readings: tuple[Reading, ...],
    **flags: Any,
) -> HoldingFact:
    last = readings[-1]
    return HoldingFact(
        ids[0],
        ids[1],
        ids[2],
        config.source,
        config.object_id,
        config.band,
        config.open_min,
        config.stable_delta,
        config.lift_min_m,
        stage,
        status,
        reason,
        last.seq,
        last.captured_ms,
        readings,
        **flags,
    )


def _newer(readings: tuple[Reading, ...], after_seq: int, after_ms: int) -> bool:
    previous_seq, previous_ms = after_seq, after_ms
    for r in readings:
        if r.seq <= previous_seq or r.captured_ms <= previous_ms:
            return False
        previous_seq, previous_ms = r.seq, r.captured_ms
    return True


def _fresh(readings: tuple[Reading, ...], now_ms: int, max_age_ms: int) -> bool:
    return all(0 <= now_ms - r.captured_ms <= max_age_ms for r in readings)


def _by_class(config: HoldingConfig, cls: str) -> dict:
    return {
        "empty": {
            "empty_verified": True,
            "closed_measured": True,
            "open_measured": False,
            "holding_verified": False,
        },
        "open": {
            "open_measured": True,
            "closed_measured": False,
            "holding_verified": False,
        },
        "holding": {
            "empty_verified": False,
            "closed_measured": True,
            "open_measured": False,
        },
        "between": {},
    }[cls]


def assess_close(
    first: Reading,
    second: Reading,
    config: HoldingConfig,
    *,
    run_id: str,
    sample_id: str,
    policy_epoch: int,
    now_ms: int,
    after_seq: int = 0,
    after_ms: int = -1,
) -> HoldingFact:
    """Two measured readings after a confirmed close -> a close-stage fact.

    Order of refusal: not newer than ``after_seq``/``after_ms`` or not strictly
    advancing, then stale (either reading more than ``max_age_ms`` before
    ``now_ms``) -> ``refused``; missing or out-of-range -> ``unknown``; the two
    differ by more than ``stable_delta`` -> ``unknown``; the two fall in
    different classes (one edge of the band) -> ``unknown``; then the class.
    """
    ids = (run_id, sample_id, policy_epoch)
    if (
        not _identity(run_id)
        or not _identity(sample_id)
        or not (type(policy_epoch) is int and policy_epoch >= 1)
    ):
        raise ValueError("invalid holding identity")
    if type(now_ms) is not int or now_ms < 0:
        raise ValueError("invalid clock")
    readings = (first, second)

    def done(status, reason, **flags):
        return _fact(config, ids, "close", status, reason, readings, **flags)

    if not _newer(readings, after_seq, after_ms):
        return done("refused", "observation_not_new")
    if not _fresh(readings, now_ms, config.max_age_ms):
        return done("refused", "observation_stale")
    if not (_opening(first.gripper) and _opening(second.gripper)):
        return done("unknown", "gripper_reading_missing")
    if abs(second.gripper - first.gripper) > config.stable_delta:
        return done("unknown", "gripper_not_steady")
    a, b = config.classify(first.gripper), config.classify(second.gripper)
    if a != b:
        return done("unknown", "readings_straddle_band_edge")
    flags = _by_class(config, b)
    if b == "holding":
        return done("holding_candidate", "steady_inside_holding_band", **flags)
    if b == "empty":
        return done("empty", "steady_below_holding_band", **flags)
    if b == "open":
        return done("open", "steady_open", **flags)
    return done("unknown", "between_band_and_open")


def assess_lift(
    candidate: HoldingFact,
    reading: Reading,
    config: HoldingConfig,
    *,
    now_ms: int,
) -> HoldingFact:
    """A fresh reading after a measured lift -> a lift-stage fact.

    ``holding_verified`` needs: ``candidate`` is a usable ``holding_candidate``
    of the same object and source; ``reading`` is newer than the candidate's
    last reading and fresh; the measured tool height rose by at least
    ``lift_min_m`` since the candidate's last reading; and the opening is still
    inside the band. An opening below the band is ``empty`` (the object fell
    out) at any height; at or above ``open_min`` it is ``open``.
    """
    ids = (candidate.run_id, candidate.sample_id, candidate.policy_epoch)
    if type(now_ms) is not int or now_ms < 0:
        raise ValueError("invalid clock")
    readings = (candidate.readings[-1], reading)

    def done(status, reason, **flags):
        return _fact(config, ids, "lift", status, reason, readings, **flags)

    if (
        candidate.stage != "close"
        or candidate.status != "holding_candidate"
        or candidate.object_id != config.object_id
        or candidate.source != config.source
        or candidate.band != config.band
    ):
        return done("unknown", "holding_candidate_required")
    if not _newer((reading,), candidate.seq, candidate.captured_ms):
        return done("refused", "observation_not_new")
    if not _fresh((reading,), now_ms, config.max_age_ms):
        return done("refused", "observation_stale")
    if not _opening(reading.gripper):
        return done("unknown", "gripper_reading_missing")
    cls = config.classify(reading.gripper)
    flags = _by_class(config, cls)
    if cls == "empty":
        return done("empty", "fell_below_holding_band_after_close", **flags)
    if cls == "open":
        return done("open", "opened_after_close", **flags)
    if cls == "between":
        return done("unknown", "between_band_and_open")
    before = candidate.readings[-1].z_m
    if not (_number(before) and _number(reading.z_m)):
        return done("unknown", "lift_height_missing", **flags)
    lift = float(reading.z_m) - float(before)
    if lift < config.lift_min_m:
        return done("unknown", "lift_too_small", lift_m=lift, **flags)
    return done(
        "holding_verified",
        "inside_band_after_lift",
        lift_m=lift,
        holding_verified=True,
        **flags,
    )


class ObservationReader:
    """Reads one ``Reading`` from a robot observation (``robot_state.
    gripper_position`` and ``pose[2]``). ``observe`` is the caller's existing
    read (for FR3, ``CachedObserver.observe`` or the bridge's observation);
    ``now_ms`` is the same clock the gate uses. The capture time is read just
    BEFORE the observation call, so a reading is never fresher than it is. A
    missing field becomes ``None`` (``unknown``), never a guess. The module
    opens no connection: tests pass fakes."""

    def __init__(self, observe: Callable[[], dict], now_ms: Callable[[], int]):
        if not callable(observe) or not callable(now_ms):
            raise TypeError("observe and now_ms must be callable")
        self._observe, self._now_ms, self._seq = observe, now_ms, 0

    def read(self) -> Reading:
        captured = self._now_ms()
        value = self._observe()
        self._seq += 1
        state = value.get("robot_state") if isinstance(value, dict) else None
        grip = state.get("gripper_position") if isinstance(state, dict) else None
        pose = value.get("pose") if isinstance(value, dict) else None
        try:
            z = pose[2]
        except (TypeError, IndexError, KeyError):
            z = None
        return Reading(
            self._seq,
            captured,
            float(grip) if _number(grip) else None,
            float(z) if _number(z) else None,
        )


@dataclass(frozen=True)
class DropConfig:
    """``object_dropped`` detector settings (opt-in, per object).

    ``lo``/``hi``: the object's holding band. ``samples``: consecutive control
    steps below ``lo`` that make a drop; ``arm_samples``: consecutive steps
    inside the band (command closed) before a drop can count, so that a close
    on nothing or a normal release does not read as a drop.
    ``command_closed_below``: a commanded opening under this counts as closed
    (the gate's own 0.5). ``roles``: episode roles the detector runs in.
    """

    lo: float
    hi: float
    samples: int = 3
    arm_samples: int = 3
    command_closed_below: float = 0.5
    roles: tuple = ("forward", "reset")

    def __post_init__(self):
        if isinstance(self.roles, list):
            object.__setattr__(self, "roles", tuple(self.roles))
        if (
            not all(_opening(x) for x in (self.lo, self.hi))  # type: ignore[arg-type]
            or not 0 < self.lo < self.hi < 1
            or not _opening(self.command_closed_below)
            or not 0 < self.command_closed_below < 1
            or any(
                type(n) is not int or not 1 <= n <= 1000
                for n in (self.samples, self.arm_samples)
            )
            or not self.roles
            or any(r not in ("forward", "reset") for r in self.roles)
            or len(set(self.roles)) != len(self.roles)
        ):
            raise ValueError("invalid drop detector configuration")

    @classmethod
    def from_holding(cls, config: HoldingConfig, **options: Any) -> DropConfig:
        if config.source == SOURCE_SYNTHETIC:
            raise ValueError("the drop detector needs a real holding band")
        return cls(config.lo, config.hi, **options)


class DropDetector:
    """Pure per-episode state; feed one sample per control step.

    ``feed(step, ns, commanded, measured)`` returns ``(first_step, first_ns)``
    of the run of low samples exactly once when ``samples`` consecutive steps
    have the command closed and the MEASURED opening below the band after the
    detector was armed; otherwise None. A missing or invalid value, an open
    command, or ``reset()`` (an observation gap) clears the run and the arming.
    Nothing here reads a command as if it were a measurement: a closed command
    with the opening still in the band never fires."""

    def __init__(self, config: DropConfig):
        self.config = config
        self.reset()

    def reset(self) -> None:
        self._in_band = 0
        self._armed = False
        self._low = 0
        self._first: tuple[int, int] | None = None

    def feed(self, step: int, ns: int, commanded, measured):
        c = self.config
        if not _opening(commanded) or not _opening(measured):
            self.reset()
            return None
        if commanded >= c.command_closed_below:
            self.reset()
            return None
        if measured < c.lo:
            self._in_band = 0
            if not self._armed:
                return None
            if self._low == 0:
                self._first = (step, ns)
            self._low += 1
            if self._low >= c.samples:
                first = self._first
                self.reset()
                return first
            return None
        self._low = 0
        self._first = None
        if measured <= c.hi:
            self._in_band += 1
            if self._in_band >= c.arm_samples:
                self._armed = True
        else:
            self._in_band = 0
        return None
