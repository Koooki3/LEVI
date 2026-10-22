"""Pure injected manual-session engine; the existing Supervisor owns the arm.

Construction requires prior parent authorization/arm-lock ownership. This module
creates no competing owner registry, file lock, background thread or transport.
Targets are relative base-frame translations with the measured anchor's exact
quaternion. Only pump() can release one pose to the injected worker. Production
construction is refused by manual_worker.production_factory.
"""

import math
import threading
from collections.abc import Callable
from dataclasses import dataclass

from .manual_worker import PoseWorker, WorkerRefused

SCHEMA = "levi.marvel0.manual.v1"
_TERMINAL = frozenset({"held", "fault_locked", "ended"})


class ManualRefused(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _Cancelled(Exception):
    """Preparation was superseded or revoked before any worker release."""


def _number(value):
    if type(value) not in (float, int):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _vector(value, length):
    return (
        type(value) in (tuple, list)
        and len(value) == length
        and all(_number(item) for item in value)
    )


def _identifier(value):
    return type(value) is str and bool(value.strip()) and len(value) <= 128


@dataclass(frozen=True)
class Pose:
    position: tuple
    quaternion: tuple


@dataclass(frozen=True)
class Reading:
    reading_id: str
    sampled_at_mono: float
    position: tuple
    quaternion: tuple
    joints_rad: tuple
    ready: bool
    tool_ok: bool
    consistent: bool


@dataclass(frozen=True)
class Limits:
    """Required injected numbers, never presumed physical safety defaults."""

    box_min_m: tuple
    box_max_m: tuple
    max_delta_m: float
    max_step_m: float
    max_velocity_m_s: float
    period_s: float
    max_reading_age_s: float
    max_anchor_age_s: float
    deadman_timeout_s: float
    target_timeout_s: float
    max_orientation_drift_rad: float
    quaternion_norm_tolerance: float
    max_settle_age_s: float

    def __post_init__(self):
        if not _vector(self.box_min_m, 3) or not _vector(self.box_max_m, 3):
            raise ManualRefused("limits_invalid")
        if any(a >= b for a, b in zip(self.box_min_m, self.box_max_m, strict=True)):
            raise ManualRefused("limits_invalid")
        for name in (
            "max_delta_m",
            "max_step_m",
            "max_velocity_m_s",
            "period_s",
            "max_reading_age_s",
            "max_anchor_age_s",
            "deadman_timeout_s",
            "target_timeout_s",
            "max_orientation_drift_rad",
            "quaternion_norm_tolerance",
            "max_settle_age_s",
        ):
            value = getattr(self, name)
            if not _number(value) or value <= 0:
                raise ManualRefused("limits_invalid")
        if (
            self.quaternion_norm_tolerance >= 1
            or self.max_orientation_drift_rad > math.pi
        ):
            raise ManualRefused("limits_invalid")
        budget = self.max_velocity_m_s * self.period_s
        if (
            not _number(budget)
            or budget <= 0
            or not math.isfinite(math.dist(self.box_min_m, self.box_max_m))
        ):
            raise ManualRefused("limits_invalid")
        # Freeze nested inputs too: a caller must not widen a live box in place.
        object.__setattr__(self, "box_min_m", tuple(self.box_min_m))
        object.__setattr__(self, "box_max_m", tuple(self.box_max_m))


@dataclass(frozen=True)
class Dependencies:
    # reading() returns trusted cached data; validate_path() is a pure injected
    # full-path check. Neither is an instruction to contact a robot here.
    now: Callable[[], float]
    reading: Callable[[], Reading]
    validate_path: Callable[[Reading, Pose], bool]
    audit: Callable[[dict], None]


@dataclass(frozen=True)
class SettledEvidence:
    """Synthetic-only receipt, never a hardware stop proof.

    The observer establishes the engine's post-closure clock bound with tick()
    before collecting its sample. A blocked/missing observer cannot end a run.
    """

    session_id: str
    generation: int
    closed_generation: int
    observed_at_mono: float
    synthetic_settled: bool


@dataclass(frozen=True)
class _Target:
    pose: Pose
    sequence: int
    expires_at: float


class Engine:
    def __init__(
        self,
        *,
        session_id: str,
        generation: int,
        anchor: Reading,
        limits: Limits,
        dependencies: Dependencies,
        worker: PoseWorker,
    ):
        if not _identifier(session_id) or type(generation) is not int or generation < 1:
            raise ManualRefused("binding_invalid")
        if not isinstance(limits, Limits) or not isinstance(worker, PoseWorker):
            raise ManualRefused("configuration_invalid")
        if not isinstance(dependencies, Dependencies) or not all(
            callable(getattr(dependencies, key))
            for key in ("now", "reading", "validate_path", "audit")
        ):
            raise ManualRefused("configuration_invalid")
        if worker.snapshot()["closed"] or worker.snapshot()["sent"]:
            raise ManualRefused("worker_not_pristine")
        self.session_id, self.generation = session_id, generation
        self.limits, self.dependencies, self.worker = limits, dependencies, worker
        self._lock = threading.RLock()
        self._clock_lock = threading.Lock()
        self._reading_lock = threading.Lock()
        self._state, self._reason = "owned_idle", "awaiting_deadman"
        self._pending = None
        self._last_target_seq = self._last_deadman_seq = -1
        self._deadman = False
        self._deadman_until = None
        self._last_target_until = None
        self._in_flight = False
        self._epoch = 0
        self._closed_generation = 0
        self._closed_at = None
        self._stop_evidence = "unproven"
        self._accepted = self._acknowledged = self._settled = 0
        self._last_now = None
        self._last_reading = None
        now = self._now()
        try:
            anchor = self._checked_reading(anchor, now, initial=True)
        except ManualRefused as exc:
            self._close("fault_locked", exc.code)
            raise
        self.anchor = anchor
        self._last_pose = Pose(anchor.position, anchor.quaternion)
        self._last_release_at = None
        self._emit("created")

    def _now(self):
        # Serialize the actual sample with its comparison/commit. A thread that
        # sampled first cannot pause and commit after a newer sample, falsely
        # reporting a clock regression. Stop never acquires this lock.
        with self._clock_lock:
            with self._lock:
                closed_before_sample = (
                    self._closed_generation
                    if self._closed_generation > 0
                    and self._stop_evidence == "admission_closed"
                    else None
                )
            try:
                now = self.dependencies.now()
            except Exception:  # noqa: BLE001 - injected callback failures must close admission
                now = None
            with self._lock:
                invalid = (
                    not _number(now)
                    or now < 0
                    or (self._last_now is not None and now < self._last_now)
                )
                if not invalid:
                    self._last_now = now
                    if (
                        closed_before_sample is not None
                        and self._closed_generation == closed_before_sample
                        and self._closed_at is None
                        and self._stop_evidence == "admission_closed"
                    ):
                        # Both sides of this sample name the same already
                        # closed interval. A pre-stop clock value that commits
                        # after stop cannot become a post-closure bound. Stop
                        # never acquires the clock lock or calls the clock.
                        self._closed_at = now
        if invalid:
            self._close("fault_locked", "clock_invalid")
            raise ManualRefused("clock_invalid")
        return now

    def _binding(self, session_id, generation):
        if (
            type(session_id) is not str
            or session_id != self.session_id
            or type(generation) is not int
            or generation != self.generation
        ):
            raise ManualRefused("binding_mismatch")

    def _live(self):
        if self._state in _TERMINAL:
            raise ManualRefused("session_closed")

    def _inside(self, point):
        return all(
            low <= value <= high
            for value, low, high in zip(
                point, self.limits.box_min_m, self.limits.box_max_m, strict=True
            )
        )

    def _checked_reading(self, reading, now, *, initial=False):
        if not isinstance(reading, Reading) or not (
            _identifier(reading.reading_id)
            and _number(reading.sampled_at_mono)
            and _vector(reading.position, 3)
            and _vector(reading.quaternion, 4)
            and _vector(reading.joints_rad, 7)
            and reading.ready is True
            and reading.tool_ok is True
            and reading.consistent is True
        ):
            raise ManualRefused("reading_invalid")
        if not 0 <= now - reading.sampled_at_mono <= self.limits.max_reading_age_s:
            raise ManualRefused("reading_stale")
        norm = math.hypot(*reading.quaternion)
        if abs(norm - 1) > self.limits.quaternion_norm_tolerance:
            raise ManualRefused("quaternion_invalid")
        if not self._inside(reading.position):
            raise ManualRefused("reading_outside_box")
        # A frozen copy keeps caller-owned lists from changing an anchor.
        reading = Reading(
            reading.reading_id,
            reading.sampled_at_mono,
            tuple(reading.position),
            tuple(reading.quaternion),
            tuple(reading.joints_rad),
            reading.ready,
            reading.tool_ok,
            reading.consistent,
        )
        with self._lock:
            previous = self._last_reading
            if previous is not None:
                if reading.sampled_at_mono < previous.sampled_at_mono:
                    raise ManualRefused("reading_regressed")
                if reading.reading_id == previous.reading_id and reading != previous:
                    raise ManualRefused("reading_identity_reused")
            if not initial:
                if now - self.anchor.sampled_at_mono > self.limits.max_anchor_age_s:
                    raise ManualRefused("anchor_expired")
                a = self.anchor.quaternion
                # Normalize only the scalar comparison, never the sent pose.
                dot = abs(
                    sum(x * y for x, y in zip(a, reading.quaternion, strict=True))
                )
                dot /= math.hypot(*a) * norm
                angle = 2 * math.acos(min(1.0, max(0.0, dot)))
                if angle > self.limits.max_orientation_drift_rad:
                    raise ManualRefused("orientation_changed")
            self._last_reading = reading
        return reading

    def _read(self, now):
        # Source reads and commits are serialized separately from stop. A stale
        # source snapshot really returned after a newer one is a source fault;
        # mere thread scheduling cannot reorder their commits. Sample the clock
        # after the source returns, so a wait for this lock does not manufacture
        # a future-dated reading.
        with self._reading_lock:
            try:
                reading = self.dependencies.reading()
                sampled_through = self._now()
                return self._checked_reading(reading, sampled_through)
            except ManualRefused:
                raise
            except Exception:  # noqa: BLE001 - injected callback failures must close admission
                raise ManualRefused("reading_unavailable") from None

    def _validate_path(self, reading, pose):
        try:
            valid = self.dependencies.validate_path(reading, pose)
        except Exception:  # noqa: BLE001 - injected callback failures must close admission
            valid = False
        if valid is not True:
            raise ManualRefused("path_refused")

    def _emit(self, event, **fields):
        try:
            self.dependencies.audit(
                {
                    "event": event,
                    "session_id": self.session_id,
                    "generation": self.generation,
                    **fields,
                }
            )
        except Exception:  # noqa: BLE001 - injected callback failures must close admission
            self._close("fault_locked", "audit_failed")
            raise ManualRefused("audit_failed") from None

    def _close(self, state, reason):
        with self._lock:
            if self._state == "ended":
                return
            first_close = self._closed_generation == 0
            if first_close:
                self._closed_generation += 1
            self._epoch += 1  # revokes any pump already preparing a send
            if self._state != "fault_locked":
                self._state = state
                self._reason = reason
            self._pending = None
            self._deadman = False
        try:
            self.worker.close_admission()
        except Exception:  # noqa: BLE001 - injected callback failures must close admission
            with self._lock:
                if self._state != "fault_locked":
                    self._state, self._reason = "fault_locked", "close_unproven"
                self._stop_evidence = "unproven"
        else:
            with self._lock:
                self._stop_evidence = "admission_closed"

    def target(
        self,
        *,
        session_id,
        generation,
        anchor_id,
        sequence,
        delta_position_m,
    ):
        with self._lock:
            self._binding(session_id, generation)
            self._live()
            if anchor_id != self.anchor.reading_id:
                raise ManualRefused("anchor_mismatch")
            if (
                type(sequence) is not int
                or sequence < 0
                or sequence <= self._last_target_seq
            ):
                raise ManualRefused("sequence_replayed")
            epoch = self._epoch
        self.tick()  # expired leases cannot be refreshed by a late target
        now = self._now()
        with self._lock:
            self._live()
        try:
            if not _vector(delta_position_m, 3):
                raise ManualRefused("target_invalid")
            if math.hypot(*delta_position_m) > self.limits.max_delta_m:
                raise ManualRefused("delta_exceeded")
            position = tuple(
                x + d
                for x, d in zip(self.anchor.position, delta_position_m, strict=True)
            )
            if not _vector(position, 3) or not self._inside(position):
                raise ManualRefused("target_outside_box")
            pose = Pose(position, self.anchor.quaternion)
            reading = self._read(now)
            self._validate_path(reading, pose)
        except ManualRefused as exc:
            self._close("fault_locked", exc.code)
            raise
        self._emit("target_validated", sequence=sequence)
        with self._lock:
            self._live()
            if self._epoch != epoch:
                raise ManualRefused("session_closed")
            if sequence <= self._last_target_seq:
                raise ManualRefused("sequence_replayed")
            self._pending = _Target(pose, sequence, now + self.limits.target_timeout_s)
            self._last_target_until = self._pending.expires_at
            self._last_target_seq = sequence
            self._accepted += 1
        self._emit("target_accepted", sequence=sequence)
        return self.view()

    def deadman(self, *, session_id, generation, sequence, pressed):
        with self._lock:
            self._binding(session_id, generation)
            self._live()
            if (
                type(sequence) is not int
                or sequence < 0
                or sequence <= self._last_deadman_seq
            ):
                raise ManualRefused("sequence_replayed")
            if type(pressed) is not bool:
                raise ManualRefused("deadman_invalid")
            self._last_deadman_seq = sequence
        if not pressed:
            return self.stop("deadman_released")
        now = self._now()
        self.tick()
        with self._lock:
            self._live()
            self._deadman = True
            self._deadman_until = now + self.limits.deadman_timeout_s
        self._emit("deadman", sequence=sequence, pressed=True)
        return self.view()

    def tick(self):
        now = self._now()
        with self._lock:
            if self._state in _TERMINAL:
                return self.view()
            reason = None
            if self._deadman and now >= self._deadman_until:
                reason = "deadman_expired"
            elif self._last_target_until is not None and now >= self._last_target_until:
                reason = "target_expired"
            elif now - self.anchor.sampled_at_mono > self.limits.max_anchor_age_s:
                reason = "anchor_expired"
        if reason:
            self._close("fault_locked", reason)
        else:
            try:
                self._read(now)
            except ManualRefused as exc:
                self._close("fault_locked", exc.code)
        return self.view()

    def pump(self):
        self.tick()
        now = self._now()
        with self._lock:
            if (
                self._state in _TERMINAL
                or not self._deadman
                or self._pending is None
                or self._in_flight
            ):
                return self.view()
            if (
                self._last_release_at is not None
                and now - self._last_release_at < self.limits.period_s
            ):
                return self.view()
            target, epoch = self._pending, self._epoch
            remaining = math.dist(self._last_pose.position, target.pose.position)
            if remaining == 0:
                self._pending = None
                self._state, self._reason = "owned_idle", "target_reached"
                return self.view()
            # Budget is one period even after idle/blocked time, never banked.
            budget = min(
                self.limits.max_step_m,
                self.limits.max_velocity_m_s * self.limits.period_s,
            )
            scale = min(1.0, budget / remaining)
            position = tuple(
                a + (b - a) * scale
                for a, b in zip(
                    self._last_pose.position, target.pose.position, strict=True
                )
            )
            pose = Pose(position, self.anchor.quaternion)
            deadline = min(target.expires_at, self._deadman_until)
            self._in_flight = True
        try:
            reading = self._read(now)
            self._validate_path(reading, pose)
            self._emit("pose_prepared", sequence=target.sequence)
            release_at = self._now()
            with self._lock:
                if self._epoch != epoch or self._state in _TERMINAL:
                    raise _Cancelled()
                if self._pending is not target:
                    raise _Cancelled()  # newer target replaced this preparation
                expired = (
                    not self._deadman
                    or release_at >= self._deadman_until
                    or release_at >= target.expires_at
                    or release_at - self.anchor.sampled_at_mono
                    > self.limits.max_anchor_age_s
                    or release_at - reading.sampled_at_mono
                    > self.limits.max_reading_age_s
                )
                if not expired:
                    if (
                        self._last_release_at is not None
                        and release_at - self._last_release_at < self.limits.period_s
                    ):
                        raise _Cancelled()
                    self._last_release_at = release_at
                    self._state, self._reason = "moving", "pose_released"
            if expired:
                self._close("fault_locked", "prepare_expired")
                raise _Cancelled()
            answer = self.worker.send(pose)
            finished = self._now()
            with self._lock:
                failure = None
                if self._epoch != epoch or self._state in _TERMINAL:
                    failure = "ack_after_stop"
                elif finished >= deadline:
                    failure = "ack_expired"
                elif answer.acknowledged is not True:
                    failure = "delivery_unknown"
                else:
                    # Validate and count the ACK in the same transaction as the
                    # stop epoch check. A stop cannot slip between the two.
                    self._acknowledged += 1
                    self._last_pose = pose
                    if (
                        self._pending is target
                        and pose.position == target.pose.position
                    ):
                        self._pending = None
                        self._state, self._reason = "owned_idle", "target_reached"
            if failure:
                self._close("fault_locked", failure)
            else:
                self._emit("pose_acknowledged", sequence=target.sequence)
        except _Cancelled:
            pass
        except WorkerRefused as exc:
            self._close("fault_locked", exc.code)
        except ManualRefused as exc:
            self._close("fault_locked", exc.code)
        finally:
            with self._lock:
                self._in_flight = False
        return self.view()

    def stop(self, reason="person_stop"):
        # No clock/reading/path/audit callback is needed before revocation.
        self._close("held", reason)
        self._emit("stopped", reason=reason)
        return self.view()

    def end(self, evidence):
        now = self._now()
        with self._lock:
            if self._state == "ended":
                return self.view()
            valid = (
                isinstance(evidence, SettledEvidence)
                and evidence.session_id == self.session_id
                and type(evidence.generation) is int
                and evidence.generation == self.generation
                and type(evidence.closed_generation) is int
                and evidence.closed_generation == self._closed_generation
                and evidence.synthetic_settled is True
                and _number(evidence.observed_at_mono)
                and self._closed_at is not None
                and self._closed_at <= evidence.observed_at_mono <= now
                and now - evidence.observed_at_mono <= self.limits.max_settle_age_s
                and self._state in ("held", "fault_locked")
                and self._stop_evidence == "admission_closed"
                and not self._in_flight
                and not self.worker.snapshot()["in_flight"]
            )
            if not valid:
                raise ManualRefused("settle_unproven")
            self._settled += 1
            self._stop_evidence = "synthetic_settled"
            self._state, self._reason = "ended", "synthetic_settled"
        self._emit("ended_synthetic")
        return self.view()

    def view(self):
        with self._lock:
            worker = self.worker.snapshot()
            anchor = getattr(self, "anchor", None)
            return {
                "schema": SCHEMA,
                "session_id": self.session_id,
                "generation": self.generation,
                "anchor_id": None if anchor is None else anchor.reading_id,
                "state": self._state,
                "admission": "closed" if worker["closed"] else "open",
                "reason_code": self._reason,
                "stop_evidence": self._stop_evidence,
                "closed_generation": self._closed_generation,
                "counters": {
                    "accepted": self._accepted,
                    "sent": worker["sent"],
                    "acknowledged": self._acknowledged,
                    "settled": self._settled,
                },
                "anchor": None
                if anchor is None
                else {
                    "position": list(anchor.position),
                    "quaternion": list(anchor.quaternion),
                    "joints_rad": list(anchor.joints_rad),
                    "sampled_at_mono": anchor.sampled_at_mono,
                },
                "target": None
                if self._pending is None
                else {
                    "position": list(self._pending.pose.position),
                    "quaternion": list(self._pending.pose.quaternion),
                    "sequence": self._pending.sequence,
                },
                "deadman": {
                    "pressed": self._deadman,
                    "sequence": self._last_deadman_seq,
                },
                "in_flight": self._in_flight or worker["in_flight"],
                "capabilities": {"real_motion": False, "synthetic_only": True},
            }
