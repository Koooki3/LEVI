"""Injected measured stability observation, never a hardware safety claim.

The trusted robot and continuous admission observers must be bound externally.
No factory, physical tolerance default, motion command or scheduler is supplied.
"""

import math
from dataclasses import dataclass
from numbers import Real

import numpy as np

from levi.automatic.fr3_bridge import AdmissionSnapshot
from levi.automatic.intervention import (
    InterventionUnproven,
    PhysicalHoldProof,
    hold_contract_sha256,
)
from levi.automatic.reset_manager import SettleConfig
from levi.domain import aeri


@dataclass(frozen=True)
class AdmissionClosedProof:
    """Injected continuous closure of original admission and motion authority.

    no_new_commands covers closed_from_ns through observed_through_ns, rather
    than a point-in-time Robot.held flag. Producing this receipt needs a reviewed
    owned backend/fence binding; constructing the dataclass proves no hardware.
    """

    run_id: str
    episode_id: str
    policy_epoch: int
    plan_sha256: str
    clock_domain: str
    closed_from_ns: int
    observed_through_ns: int
    expires_ns: int
    admission_closed: bool | None
    no_new_commands: bool | None


def _numbers(value, length):
    if not isinstance(value, (list, tuple, np.ndarray)) or len(value) != length:
        raise InterventionUnproven("finite measured pose dimensions required")
    if any(
        not isinstance(v, Real)
        or isinstance(v, (bool, np.bool_))
        or not math.isfinite(v)
        for v in value
    ):
        raise InterventionUnproven("finite non-boolean measured pose required")
    return tuple(float(v) for v in value)


class MeasuredPhysicalHold:
    """observe_hold hook over explicit measured Robot.observe and frozen limits.

    Samples share the reset-settle XYZ distance and wrapped Euler vector metric.
    The measured gripper is separate from pose[6], which may be a command.
    A window passes only if every pair is within all frozen tolerances, its span
    is complete, and continuous closure still holds at the final observation.
    Errors irreversibly fail this observer. The existing InferenceCycle retains
    model authority on failure; this observer creates or releases no owner slot.
    """

    def __init__(
        self,
        robot,
        clock,
        config,
        *,
        run_id,
        plan_sha256,
        observe_admission,
        interrupted,
    ):
        if (
            not isinstance(config, SettleConfig)
            or not callable(getattr(robot, "observe", None))
            or not callable(observe_admission)
            or not callable(interrupted)
            or not isinstance(plan_sha256, str)
            or len(plan_sha256) != 64
            or any(c not in "0123456789abcdef" for c in plan_sha256)
        ):
            raise InterventionUnproven(
                "frozen hold limits and trusted observers required"
            )
        self.robot, self.clock, self.config = robot, clock, config
        self.run_id, self.plan_sha256 = run_id, plan_sha256
        self.observe_admission, self.interrupted = observe_admission, interrupted
        self.domain = clock.domain
        if not isinstance(self.domain, str) or not self.domain:
            raise InterventionUnproven("explicit monotonic clock identity required")
        self.condition_sha256 = hold_contract_sha256(config)
        self.failed = False
        self._last_clock = -1
        self._last_generation = self._last_stamp = -1
        self._now()

    def _now(self):
        now = self.clock.now()
        if (
            self.clock.domain != self.domain
            or type(now) is not int
            or now < self._last_clock
        ):
            raise InterventionUnproven("hold clock identity/time changed")
        self._last_clock = now
        return now

    def _ready(self):
        if self.failed or self.interrupted() is not False:
            raise InterventionUnproven("hold stopped or observer previously failed")

    def _closed(self, episode, epoch, began, deadline):
        self._ready()
        before = self._now()
        proof = self.observe_admission(episode, epoch, deadline)
        after = self._now()
        if not isinstance(proof, AdmissionClosedProof) or not (
            proof.run_id == self.run_id
            and proof.episode_id == episode
            and type(proof.policy_epoch) is int
            and proof.policy_epoch == epoch
            and proof.plan_sha256 == self.plan_sha256
            and proof.clock_domain == self.domain
            and type(proof.closed_from_ns) is int
            and type(proof.observed_through_ns) is int
            and type(proof.expires_ns) is int
            and 0 <= proof.closed_from_ns <= began
            and before
            <= proof.observed_through_ns
            <= after
            < proof.expires_ns
            <= deadline
            and proof.admission_closed is True
            and proof.no_new_commands is True
        ):
            raise InterventionUnproven(
                "continuous original admission/motion closure required"
            )
        self._ready()

    def _sample(self, before, after, observed):
        if not isinstance(observed, dict) or observed.get("state_fresh") is not True:
            raise InterventionUnproven("fresh measured robot observation required")
        stamp = observed.get("observed_ns")
        if (
            type(stamp) is not int
            or not before <= stamp <= after
            or stamp <= self._last_stamp
        ):
            raise InterventionUnproven(
                "fresh strictly increasing measurement time required"
            )
        pose = _numbers(observed.get("pose"), 7)
        state = observed.get("robot_state")
        if not isinstance(state, dict):
            raise InterventionUnproven("measured robot state required")
        measured_pose = _numbers(state.get("cartesian_position"), 7)
        gripper, proof = (
            state.get("gripper_position"),
            observed.get("gripper_measurement"),
        )
        if (
            pose[:6] != measured_pose[:6]
            or not 0 <= pose[6] <= 1
            or not isinstance(gripper, Real)
            or isinstance(gripper, (bool, np.bool_))
            or not math.isfinite(gripper)
            or not 0 <= gripper <= 1
        ):
            raise InterventionUnproven(
                "pose source and measured normalized gripper required"
            )
        fields = {
            "route",
            "field",
            "generation",
            "read_from_ns",
            "read_through_ns",
            "clock_domain",
            "value",
        }
        if (
            not isinstance(proof, dict)
            or set(proof) != fields
            or not (
                proof["route"] == "getstate"
                and proof["field"] == "gripper_pos"
                and type(proof["generation"]) is int
                and proof["generation"] > self._last_generation
                and proof["generation"] > 0
                and proof["clock_domain"] == self.domain
                and type(proof["read_from_ns"]) is int
                and type(proof["read_through_ns"]) is int
                and before <= proof["read_from_ns"] <= proof["read_through_ns"] <= stamp
                and isinstance(proof["value"], Real)
                and not isinstance(proof["value"], (bool, np.bool_))
                and math.isfinite(proof["value"])
                and proof["value"] == gripper
            )
        ):
            raise InterventionUnproven("fresh getstate/gripper_pos provenance required")
        self._last_generation, self._last_stamp = proof["generation"], stamp
        return stamp, pose[:6], float(gripper)

    def _within(self, current, previous):
        _, pose, gripper = current
        _, old, old_gripper = previous
        return (
            math.dist(pose[:3], old[:3]) <= self.config.position_tolerance_m
            and math.sqrt(
                sum(
                    math.remainder(a - b, 2 * math.pi) ** 2
                    for a, b in zip(pose[3:], old[3:], strict=True)
                )
            )
            <= self.config.rotation_tolerance_rad
            and abs(gripper - old_gripper) <= self.config.gripper_tolerance
        )

    def __call__(self, episode, epoch, deadline):
        try:
            self._ready()
            run, _, _ = aeri.episode_parts(episode)
            if (
                run != self.run_id
                or type(epoch) is not int
                or epoch < 0
                or type(deadline) is not int
            ):
                raise InterventionUnproven(
                    "hold run/episode/policy epoch/deadline mismatch"
                )
            interval = self.config.sample_interval_ns
            if self.config.stable_window_ns > 999 * interval:
                raise InterventionUnproven(
                    "frozen hold window exceeds 999 maximum sample gaps"
                )
            # Capture earlier than the maximum gap, reserving closure/clock/IO
            # time. Integer ceiling preserves enough span in 1000 samples for
            # long windows; no scheduling promise overrides actual gap checks.
            cadence = max(1, interval // 2, (self.config.stable_window_ns + 998) // 999)
            began = self._now()
            if deadline <= began:
                raise InterventionUnproven("hold deadline expired")
            end = min(deadline - 1, began + self.config.max_wait_ns)
            window, previous_started, previous_stamp = [], None, None
            for _ in range(1000):
                self._closed(episode, epoch, began, deadline)
                before = self._now()
                if before > end or (
                    previous_started is not None
                    and before - previous_started > self.config.sample_interval_ns
                ):
                    raise InterventionUnproven(
                        "frozen hold time/sample interval exceeded"
                    )
                observed = self.robot.observe()
                after = self._now()
                self._ready()
                if after > end:
                    raise InterventionUnproven("hold measurement deadline exceeded")
                sample = self._sample(before, after, observed)
                if (
                    previous_stamp is not None
                    and sample[0] - previous_stamp > self.config.sample_interval_ns
                ):
                    raise InterventionUnproven(
                        "frozen measured sample interval exceeded"
                    )
                self._closed(episode, epoch, began, deadline)
                window = (
                    [*window, sample]
                    if all(self._within(sample, old) for old in window)
                    else [sample]
                )
                if (
                    len(window) >= 2
                    and sample[0] - window[0][0] >= self.config.stable_window_ns
                ):
                    self._closed(episode, epoch, began, deadline)
                    self._ready()
                    if self._now() > end:
                        raise InterventionUnproven(
                            "hold final proof exceeded frozen wait"
                        )
                    return PhysicalHoldProof(
                        self.run_id,
                        episode,
                        epoch,
                        self.plan_sha256,
                        self.condition_sha256,
                        self.domain,
                        window[0][0],
                        sample[0],
                        deadline,
                        True,
                        True,
                        True,
                    )
                previous_started, previous_stamp = before, sample[0]
                # Cadence is a target within the frozen maximum, not a relaxed
                # limit. The next capture still checks actual start/stamp gaps.
                due = before + cadence
                while True:
                    now = self._now()
                    if now >= due:
                        break
                    self._ready()
                    remaining = min(due, end) - now
                    if remaining <= 0:
                        raise InterventionUnproven(
                            "complete hold window unavailable before deadline"
                        )
                    self.clock.advance(min(remaining, 50_000_000))
            raise InterventionUnproven("bounded complete hold window unavailable")
        except BaseException as exc:
            self.failed = True
            if isinstance(exc, Exception) and not isinstance(exc, InterventionUnproven):
                raise InterventionUnproven(
                    "measured hold observer unavailable"
                ) from exc
            raise


class RobotAdmissionWitness:
    """observe_admission source backed by the bridge admission gate.

    Built from ``mark``, the snapshot ``Robot.stop_admission()`` returned. Each
    call takes a fresh atomic ``Robot.admission_snapshot()`` and issues an
    AdmissionClosedProof only if ALL of these hold; otherwise it raises
    InterventionUnproven and stays failed (no default allow, no repair):

    * the gate is closed (not open, not in a home-only window) and is the very
      closure of ``mark`` (same generation and closure time), so it was never
      reopened, not even reopened and re-closed, and no home-only window has
      opened since: a window changes the generation, so a witness made before
      it fails for good even though the gate is closed again afterwards;
    * the pose, gripper and home-only released-command counters equal the mark's,
      so no command passed the gate inside [closed_from, observed_through];
    * the MotionFence holds no current token (revoked). MotionFence keeps no
      revocation time or generation, so this is a point-in-time fact at
      observed_through; the gate, not the fence, covers the interval;
    * the robot clock domain equals this clock's and time never goes backwards;
    * the episode and policy epoch stay those of the first call.

    What this does NOT prove: that the last released action finished or that the
    arm is still. Counters mark when a command STARTED through the gate. A command
    released just before ``stop_admission`` may still be executing in the
    controller or reach the arm later, and ``Robot.hold()`` only sets ``held``, a
    flag that is no evidence either. ``Robot.observe()["observed_ns"]`` is the
    host time the read finished, not the physical capture time of pose or images.
    Settling is therefore established only by MeasuredPhysicalHold's measured
    window; this witness proves only that no NEW command could have started.
    """

    def __init__(self, robot, fence, clock, mark, *, run_id, plan_sha256, validity_ns):
        if (
            not callable(getattr(robot, "admission_snapshot", None))
            or not callable(getattr(fence, "check", None))
            or not hasattr(fence, "current")
            or not isinstance(mark, AdmissionSnapshot)
            or mark.closed is not True
            or mark.closed_from_ns is None
            or mark.generation < 1
            or type(validity_ns) is not int
            or validity_ns < 1
            or not isinstance(run_id, str)
            or not run_id
            or not isinstance(plan_sha256, str)
            or len(plan_sha256) != 64
            or any(c not in "0123456789abcdef" for c in plan_sha256)
            or not isinstance(getattr(clock, "domain", None), str)
            or not clock.domain
            or mark.clock_domain != clock.domain
        ):
            raise InterventionUnproven(
                "closed admission mark, fence, matching clock and validity required"
            )
        self.robot, self.fence, self.clock, self.mark = robot, fence, clock, mark
        self.run_id, self.plan_sha256 = run_id, plan_sha256
        self.validity_ns = validity_ns
        self.failed = False
        self._binding = None
        self._last_through = mark.read_ns

    def _fail(self, why):
        self.failed = True
        raise InterventionUnproven("admission closure unproven: " + why)

    def __call__(self, episode, epoch, deadline):
        if self.failed:
            raise InterventionUnproven("admission witness previously failed")
        try:
            return self._observe(episode, epoch, deadline)
        except InterventionUnproven:
            self.failed = True
            raise
        except BaseException as exc:
            self.failed = True
            raise InterventionUnproven("admission witness unavailable") from exc

    def _observe(self, episode, epoch, deadline):
        if type(epoch) is not int or type(deadline) is not int:
            self._fail("episode/epoch/deadline types")
        if self._binding is None:
            self._binding = (episode, epoch)
        elif self._binding != (episode, epoch):
            self._fail("episode or policy epoch changed")
        snap, mark = self.robot.admission_snapshot(), self.mark
        through = snap.read_ns
        if (
            type(through) is not int
            or snap.clock_domain != self.clock.domain
            or through < self._last_through
        ):
            self._fail("clock domain changed or time went backwards")
        if not snap.closed or snap.mode != "closed":
            self._fail("gate is open or in a home-only window")
        if (
            snap.generation != mark.generation
            or snap.closed_from_ns != mark.closed_from_ns
        ):
            self._fail("gate was reopened; closure interval is not continuous")
        if (
            snap.pose_released != mark.pose_released
            or snap.gripper_released != mark.gripper_released
        ):
            self._fail("a pose/gripper command was released inside the interval")
        if (
            snap.home_pose_released != mark.home_pose_released
            or snap.home_gripper_released != mark.home_gripper_released
        ):
            self._fail("a home command was released inside the interval")
        if self.fence.current is not None:
            self._fail("motion fence token is not revoked")
        expires = min(deadline, through + self.validity_ns)
        if expires <= through:
            self._fail("no validity left before the deadline")
        self._last_through = through
        return AdmissionClosedProof(
            self.run_id,
            episode,
            epoch,
            self.plan_sha256,
            snap.clock_domain,
            mark.closed_from_ns,
            through,
            expires,
            True,
            True,
        )


def assemble_measured_hold(
    robot,
    bridge_robot,
    fence,
    clock,
    config,
    mark,
    *,
    run_id,
    plan_sha256,
    validity_ns,
    interrupted,
):
    """MeasuredPhysicalHold whose observe_admission is a RobotAdmissionWitness.

    ``robot`` supplies observe() (the measured source); ``bridge_robot`` is the
    fr3_bridge.Robot that owns the gate (they may be the same object). Config,
    validity, identities and the interrupt callback are all caller-injected, with
    no defaults. The witness is ``hold.observe_admission``.
    """
    witness = RobotAdmissionWitness(
        bridge_robot,
        fence,
        clock,
        mark,
        run_id=run_id,
        plan_sha256=plan_sha256,
        validity_ns=validity_ns,
    )
    return MeasuredPhysicalHold(
        robot,
        clock,
        config,
        run_id=run_id,
        plan_sha256=plan_sha256,
        observe_admission=witness,
        interrupted=interrupted,
    )
