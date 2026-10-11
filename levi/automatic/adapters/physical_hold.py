"""Injected measured stability observation, never a hardware safety claim.

The trusted robot and continuous admission observers must be bound externally.
No factory, physical tolerance default, motion command or scheduler is supplied.
"""

import math
from dataclasses import dataclass
from numbers import Real

import numpy as np

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
