"""Measured holding fact and drop detector for a real gripper (no I/O, no robot).

The re-grasp gate (``regrasp_gate``) never infers holding from a gripper width;
the caller must hand it independently measured facts. This module is that
producer for a real gripper. It reads *measured* openings only (robot-server
``gripper_pos``, 1.0 = open), never the commanded value (the policy's
``state[6]`` is the command and cannot notice an empty grasp or a drop).

What counts as measured. ``ObservationReader`` accepts ``robot_state.
gripper_position`` only with the same proof the bridge and ``SignalEvents``
demand: this observation's ``gripper_measurement`` (route ``getstate``, field
``gripper_pos``, a strictly advancing generation, a read window inside the call,
the clock's domain, a value equal to the position) and ``state_fresh`` true. An
SDK-filled zero without that proof is *no reading* (``unknown``), never "empty".
The capture time is the proof's ``read_through_ns``, and a repeated generation
is not a new measurement.

A fact is self-contained evidence a gate can act on: the readings, the band and
thresholds used, the identity, the capture time and sequence, and a ``source``.
``source`` is not a label the caller picks: ``verified_measurement`` is stamped
only when every reading carries the reader's proof (a generation), otherwise
``recorded_measurement``; ``synthetic_measurement`` only for the synthetic
configuration. Stages:

1. ``assess_close``: two proven, distinct, fresh readings after the close
   command (``after_seq``/``after_ms`` are required), at least ``min_interval_ms``
   apart, steady (differ by at most ``GRIPPER_STABLE_DELTA``) and inside the
   object's holding band -> ``holding_candidate``. Below the band -> ``empty``;
   at or above ``open_min`` -> ``open``; jitter or a missing value -> ``unknown``;
   stale (more than ``max_age_ms``, at most the offline handoff contract's
   100 ms) or not newer -> ``refused``.
2. ``assess_lift``: after a measured lift of at least ``lift_min_m``, TWO newer
   fresh readings, steady, each within ``max_drift`` of the candidate's value and
   inside the band, the first no later than ``max_gap_ms`` after the candidate
   -> ``holding_verified``. A slow close on air that was still moving at the
   candidate readings fails the drift check. This is signal-level evidence: it
   does not see the object, and a person (or a later level of the full-auto
   ladder) still decides the hand back.

The band is per object and comes from configuration (job or site); no object's
numbers are built in. Synthetic thresholds (open >= 0.9, closed <= 0.3 of the
offline harness) exist only in ``synthetic_config``, and a real configuration
refuses a band edge within ``SYNTHETIC_MARGIN`` of them: 0.2986 (an eggplant
held, 2026-11-14) differs from 0.3 by 0.0014.

``DropDetector`` turns the same measured opening into an ``object_dropped``
signal: the command says closed, the measured opening held steady inside the
holding band and has fallen below it for a few consecutive samples. It only
informs; it never triggers motion.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .fr3_bridge import GRIPPER_STABLE_DELTA, HOME_GRIPPER_MIN
from .regrasp_gate import GateObservation

SCHEMA = "levi.holding_fact.v1"
SOURCE_VERIFIED = "verified_measurement"
SOURCE_RECORDED = "recorded_measurement"
SOURCE_SYNTHETIC = "synthetic_measurement"
# Same bound as ``offline_harness.MAX_OBSERVATION_AGE_MS`` and the gate's
# ``max_age_ms`` default (a test keeps the three equal); a config may only
# tighten it.
MAX_AGE_MS = 100
DEFAULT_LIFT_MIN_M = 0.02
DEFAULT_MIN_INTERVAL_MS = 30
DEFAULT_MAX_GAP_MS = 5000
# Offline-harness thresholds. Used by ``synthetic_config`` and nowhere else; a
# real band edge may not lie within SYNTHETIC_MARGIN of the closed limit.
SYNTHETIC_OPEN_MIN = 0.9
SYNTHETIC_CLOSED_MAX = 0.3
SYNTHETIC_MARGIN = 0.02

STATUSES = frozenset(
    {"holding_candidate", "holding_verified", "empty", "open", "unknown", "refused"}
)
_PROOF_FIELDS = {
    "route",
    "field",
    "generation",
    "read_from_ns",
    "read_through_ns",
    "clock_domain",
    "value",
}


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
    as held; below ``lo`` the gripper is empty. A real configuration needs
    ``0 < lo < hi < open_min`` and no band edge within ``SYNTHETIC_MARGIN`` of
    the synthetic closed limit; ``synthetic`` is only valid with exactly the
    synthetic thresholds (use ``synthetic_config``). ``open_min`` defaults to the
    bridge's reviewed home-open limit. ``max_drift``: how far a lifted reading
    may differ from the candidate's. ``min_interval_ms``: least time between two
    readings of a stage. ``max_gap_ms``: longest wait between the candidate and
    the lift readings.
    """

    object_id: str
    lo: float
    hi: float
    synthetic: bool = False
    open_min: float = HOME_GRIPPER_MIN
    stable_delta: float = GRIPPER_STABLE_DELTA
    max_drift: float = GRIPPER_STABLE_DELTA
    lift_min_m: float = DEFAULT_LIFT_MIN_M
    max_age_ms: int = MAX_AGE_MS
    min_interval_ms: int = DEFAULT_MIN_INTERVAL_MS
    max_gap_ms: int = DEFAULT_MAX_GAP_MS

    def __post_init__(self):
        if (
            not _identity(self.object_id)
            or type(self.synthetic) is not bool
            or not all(
                _opening(x)
                for x in (self.lo, self.hi, self.open_min)  # type: ignore[arg-type]
            )
            or not self.lo < self.hi < self.open_min
            or not _number(self.stable_delta)
            or not 0 < self.stable_delta <= 0.1
            or not _number(self.max_drift)
            or not 0 < self.max_drift <= 0.1
            or not _number(self.lift_min_m)
            or not 0 < self.lift_min_m <= 0.2
            or type(self.max_age_ms) is not int
            or not 1 <= self.max_age_ms <= MAX_AGE_MS
            or type(self.min_interval_ms) is not int
            or not 1 <= self.min_interval_ms < self.max_age_ms
            or type(self.max_gap_ms) is not int
            or not 1 <= self.max_gap_ms <= 60_000
        ):
            raise ValueError("invalid holding configuration")
        if self.synthetic:
            if (self.lo, self.hi, self.open_min) != (
                0.0,
                SYNTHETIC_CLOSED_MAX,
                SYNTHETIC_OPEN_MIN,
            ):
                raise ValueError("synthetic means exactly the synthetic thresholds")
        else:
            if self.lo <= 0:
                raise ValueError("a real holding band needs lo > 0")
            if any(
                abs(x - SYNTHETIC_CLOSED_MAX) < SYNTHETIC_MARGIN
                for x in (self.lo, self.hi)
            ):
                raise ValueError(
                    "a real holding band edge may not lie within "
                    f"{SYNTHETIC_MARGIN} of the synthetic closed limit "
                    f"{SYNTHETIC_CLOSED_MAX}"
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
    """The offline harness's thresholds (open >= 0.9, closed <= 0.3). Facts
    made with it are always ``synthetic_measurement``."""
    return HoldingConfig(
        object_id,
        0.0,
        SYNTHETIC_CLOSED_MAX,
        synthetic=True,
        open_min=SYNTHETIC_OPEN_MIN,
    )


def parse_holding_bands(raw: object, **limits: Any) -> dict[str, HoldingConfig]:
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
        out[name] = HoldingConfig(name, float(pair[0]), float(pair[1]), **limits)
    return out


@dataclass(frozen=True)
class Reading:
    """One measured sample: opening (None = missing or unproven) and tool height
    in metres (None = missing), with its sequence number and capture time.
    ``generation`` is the ``getstate`` proof's generation, set only by
    ``ObservationReader`` for a proven measurement (None = not proven)."""

    seq: int
    captured_ms: int
    gripper: float | None
    z_m: float | None = None
    generation: int | None = None

    def __post_init__(self):
        if (
            type(self.seq) is not int
            or self.seq < 1
            or type(self.captured_ms) is not int
            or self.captured_ms < 0
            or (
                self.generation is not None
                and (type(self.generation) is not int or self.generation < 1)
            )
        ):
            raise ValueError("invalid reading identity")

    @property
    def proven(self) -> bool:
        return self.generation is not None


def source_of(config: HoldingConfig, readings: tuple[Reading, ...]) -> str:
    """The only place a source is chosen: synthetic by configuration, verified
    when every reading carries the reader's proof, else recorded."""
    if config.synthetic:
        return SOURCE_SYNTHETIC
    return SOURCE_VERIFIED if all(r.proven for r in readings) else SOURCE_RECORDED


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
                    "generation": r.generation,
                }
                for r in self.readings
            ],
            "lift_m": self.lift_m,
            "empty_verified": self.empty_verified,
            "open_measured": self.open_measured,
            "closed_measured": self.closed_measured,
            "holding_verified": self.holding_verified,
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
    """For a gate that hands authority to the fact: only a fact whose readings
    all carry the reader's measurement proof (``verified_measurement``)."""
    if not fact.is_real:
        raise ValueError(f"holding fact source {fact.source!r} is not verified")
    return fact


def _fact(
    config: HoldingConfig,
    ids: tuple[str, str, int],
    stage: str,
    status: str,
    reason: str,
    readings: tuple[Reading, ...],
    source: str,
    **flags: Any,
) -> HoldingFact:
    last = readings[-1]
    return HoldingFact(
        ids[0],
        ids[1],
        ids[2],
        source,
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


def _by_class(cls: str) -> dict:
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


def _pair_problem(
    first: Reading, second: Reading, config: HoldingConfig
) -> tuple[str, str] | None:
    """(status, reason) when two readings of one stage cannot be compared."""
    if not (_opening(first.gripper) and _opening(second.gripper)):
        return "unknown", "gripper_reading_missing"
    if (
        first.proven and second.proven and second.generation <= first.generation  # type: ignore[operator]
    ):
        return "unknown", "gripper_measurement_not_distinct"
    if second.captured_ms - first.captured_ms < config.min_interval_ms:
        return "unknown", "readings_too_close"
    if abs(second.gripper - first.gripper) > config.stable_delta:  # type: ignore[operator]
        return "unknown", "gripper_not_steady"
    if config.classify(first.gripper) != config.classify(second.gripper):  # type: ignore[arg-type]
        return "unknown", "readings_straddle_band_edge"
    return None


def assess_close(
    first: Reading,
    second: Reading,
    config: HoldingConfig,
    *,
    run_id: str,
    sample_id: str,
    policy_epoch: int,
    now_ms: int,
    after_seq: int,
    after_ms: int,
) -> HoldingFact:
    """Two measured readings after a confirmed close -> a close-stage fact.

    ``after_seq``/``after_ms`` (required): the sequence number of the last
    reading taken before the close command was confirmed and the time it was
    confirmed; both readings must be newer. Order: not newer or not strictly
    advancing, then stale (either reading more than ``max_age_ms`` before
    ``now_ms``) -> ``refused``; missing, a repeated measurement, closer than
    ``min_interval_ms``, jitter above ``stable_delta`` or two classes (one edge
    of the band) -> ``unknown``; then the class.
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
    if type(after_seq) is not int or after_seq < 0 or type(after_ms) is not int:
        raise ValueError("after_seq and after_ms of the close command are required")
    readings = (first, second)
    source = source_of(config, readings)

    def done(status, reason, **flags):
        return _fact(config, ids, "close", status, reason, readings, source, **flags)

    if not _newer(readings, after_seq, after_ms):
        return done("refused", "observation_not_new")
    if not _fresh(readings, now_ms, config.max_age_ms):
        return done("refused", "observation_stale")
    problem = _pair_problem(first, second, config)
    if problem:
        return done(*problem)
    cls = config.classify(second.gripper)  # type: ignore[arg-type]
    flags = _by_class(cls)
    if cls == "holding":
        return done("holding_candidate", "steady_inside_holding_band", **flags)
    if cls == "empty":
        return done("empty", "steady_below_holding_band", **flags)
    if cls == "open":
        return done("open", "steady_open", **flags)
    return done("unknown", "between_band_and_open")


def assess_lift(
    candidate: HoldingFact,
    first: Reading,
    second: Reading,
    config: HoldingConfig,
    *,
    now_ms: int,
) -> HoldingFact:
    """Two fresh readings after a measured lift -> a lift-stage fact.

    ``holding_verified`` needs: ``candidate`` is a usable ``holding_candidate``
    of the same object and source; both readings are newer than the candidate's
    last reading, strictly advancing, fresh, the first at most ``max_gap_ms``
    after it; they are distinct measurements at least ``min_interval_ms`` apart
    and steady; the measured tool height of both rose by at least
    ``lift_min_m`` over the candidate's; and both openings are inside the band
    and within ``max_drift`` of the candidate's. Two readings below the band
    are ``empty`` (the object fell out) and at or above ``open_min`` are
    ``open``, at any height.
    """
    ids = (candidate.run_id, candidate.sample_id, candidate.policy_epoch)
    if type(now_ms) is not int or now_ms < 0:
        raise ValueError("invalid clock")
    base = candidate.readings[-1]
    readings = (base, first, second)
    source = source_of(config, (first, second))

    def done(status, reason, **flags):
        return _fact(config, ids, "lift", status, reason, readings, source, **flags)

    if (
        candidate.stage != "close"
        or candidate.status != "holding_candidate"
        or candidate.object_id != config.object_id
        or candidate.band != config.band
    ):
        return done("unknown", "holding_candidate_required")
    if candidate.source != source:
        return done("unknown", "source_changed_between_stages")
    if not _newer((first, second), base.seq, base.captured_ms):
        return done("refused", "observation_not_new")
    if not _fresh((first, second), now_ms, config.max_age_ms):
        return done("refused", "observation_stale")
    if first.captured_ms - base.captured_ms > config.max_gap_ms:
        return done("unknown", "lift_readings_too_late")
    problem = _pair_problem(first, second, config)
    if problem:
        return done(*problem)
    cls = config.classify(second.gripper)  # type: ignore[arg-type]
    flags = _by_class(cls)
    if cls == "empty":
        return done("empty", "fell_below_holding_band_after_close", **flags)
    if cls == "open":
        return done("open", "opened_after_close", **flags)
    if cls == "between":
        return done("unknown", "between_band_and_open")
    if any(
        abs(r.gripper - base.gripper) > config.max_drift  # type: ignore[operator]
        for r in (first, second)
    ):
        return done("unknown", "gripper_drifted_after_close", **flags)
    if not all(_number(r.z_m) for r in (base, first, second)):
        return done("unknown", "lift_height_missing", **flags)
    lift = min(first.z_m, second.z_m) - base.z_m  # type: ignore[operator]
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
    """Reads proven ``Reading``s from the robot observation.

    ``observe`` is the caller's existing read (``fr3_bridge.Robot.observe`` or
    ``CachedObserver.observe``); ``clock`` has ``now()`` (ns) and ``domain`` and
    is the bridge's own clock. A reading carries a gripper opening only when
    the observation is ``state_fresh`` and its ``gripper_measurement`` is this
    call's ``getstate`` ``gripper_pos`` proof (as ``SignalEvents._measured``
    demands: strictly advancing generation, a read window inside this call and
    before the observation time, the clock's domain, a normalized value equal to
    ``robot_state.gripper_position``). Anything else (no proof, an SDK-filled
    zero, a stale state, a repeated generation) gives ``gripper=None``: unknown,
    never empty. The capture time is the proof's ``read_through_ns`` in
    milliseconds (the unproven case uses the time before the call). The module
    opens no connection: tests pass fakes."""

    def __init__(self, observe: Callable[[], dict], clock: Any):
        if not callable(observe) or not callable(getattr(clock, "now", None)):
            raise TypeError("observe must be callable and clock needs now()")
        if not _identity(getattr(clock, "domain", None)):
            raise TypeError("clock needs a domain")
        self._observe, self._clock = observe, clock
        self._seq, self._generation = 0, 0

    def read(self) -> Reading:
        began = self._clock.now()
        value = self._observe()
        ended = self._clock.now()
        self._seq += 1
        state = value.get("robot_state") if isinstance(value, dict) else None
        position = state.get("gripper_position") if isinstance(state, dict) else None
        pose = value.get("pose") if isinstance(value, dict) else None
        try:
            z = pose[2]
        except (TypeError, IndexError, KeyError):
            z = None
        z = float(z) if _number(z) else None
        proof = value.get("gripper_measurement") if isinstance(value, dict) else None
        observed = value.get("observed_ns") if isinstance(value, dict) else None
        generation = proof.get("generation") if isinstance(proof, dict) else None
        if not (
            isinstance(proof, dict)
            and set(proof) == _PROOF_FIELDS
            and value.get("state_fresh") is True
            and proof["route"] == "getstate"
            and proof["field"] == "gripper_pos"
            and type(generation) is int
            and generation > self._generation
            and type(proof["read_from_ns"]) is int
            and type(proof["read_through_ns"]) is int
            and began <= proof["read_from_ns"] <= proof["read_through_ns"] <= ended
            and (observed is None or proof["read_through_ns"] <= observed)
            and proof["clock_domain"] == self._clock.domain
            and _opening(proof["value"])
            and _opening(position)
            and position == proof["value"]
        ):
            return Reading(self._seq, began // 1_000_000, None, z)
        self._generation = generation
        return Reading(
            self._seq,
            proof["read_through_ns"] // 1_000_000,
            float(position),
            z,
            generation,
        )


@dataclass(frozen=True)
class DropConfig:
    """``object_dropped`` detector settings (opt-in, per object).

    ``lo``/``hi``: the object's holding band. ``samples``: consecutive control
    steps below ``lo`` that make a drop; ``arm_samples``: consecutive steps
    inside the band, each within ``stable_delta`` of the one before (command
    closed), before a drop can count. A close that sweeps through the band on
    its way to an empty gripper never arms. ``command_closed_below``: a
    commanded opening under this counts as closed (the gate's own 0.5).
    ``roles``: episode roles the detector runs in.
    """

    lo: float
    hi: float
    samples: int = 3
    arm_samples: int = 3
    command_closed_below: float = 0.5
    roles: tuple = ("forward", "reset")
    stable_delta: float = GRIPPER_STABLE_DELTA

    def __post_init__(self):
        if isinstance(self.roles, list):
            object.__setattr__(self, "roles", tuple(self.roles))
        if (
            not all(_opening(x) for x in (self.lo, self.hi))  # type: ignore[arg-type]
            or not 0 < self.lo < self.hi < 1
            or not _opening(self.command_closed_below)
            or not 0 < self.command_closed_below < 1
            or not _number(self.stable_delta)
            or not 0 < self.stable_delta <= 0.1
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
        if config.synthetic:
            raise ValueError("the drop detector needs a real holding band")
        return cls(config.lo, config.hi, **options)

    def describe(self) -> str:
        """The parameters, for the event's own evidence."""
        return (
            f"object_dropped:lo={self.lo},hi={self.hi},samples={self.samples},"
            f"arm_samples={self.arm_samples},stable_delta={self.stable_delta},"
            f"command_closed_below={self.command_closed_below}"
        )


class DropDetector:
    """Pure per-episode state; feed one sample per control step.

    ``feed(step, ns, commanded, measured)`` returns ``(first_step, first_ns)``
    of the run of low samples exactly once when ``samples`` consecutive steps
    have the command closed and the MEASURED opening below the band after the
    detector was armed (``arm_samples`` consecutive in-band steps, adjacent
    ones within ``stable_delta``); otherwise None. A missing or invalid value,
    an open command, or ``reset()`` (an observation gap) clears the run and the
    arming. Nothing here reads a command as if it were a measurement: a closed
    command with the opening still in the band never fires."""

    def __init__(self, config: DropConfig):
        self.config = config
        self.reset()

    def reset(self) -> None:
        self._in_band = 0
        self._prev: float | None = None
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
            self._prev = None
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
            if self._prev is not None and abs(measured - self._prev) <= c.stable_delta:
                self._in_band += 1
            else:
                self._in_band = 1
            self._prev = measured
            if self._in_band >= c.arm_samples:
                self._armed = True
        else:
            self._in_band = 0
            self._prev = None
        return None
