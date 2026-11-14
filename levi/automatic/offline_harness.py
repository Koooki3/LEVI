"""Executable synthetic reset/regrasp contract. Never creates a real adapter.

The scripted provider selects a skill and a target, and claims no LLM calls.
The fake executor generates synthetic measurements; passing them proves only
this software flow. No result grants physical execution or calls env.reset.
"""

from __future__ import annotations

import copy
import math
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from .offline_scenarios import SCENARIOS, scenario_catalog
from .regrasp_gate import GateObservation, GateState, advance_regrasp

__all__ = ["run_scenario", "scenario_catalog"]

SCHEMA = "levi.reset_harness.run.v1"
MAX_REGRASP_ATTEMPTS = 2
MAX_OBSERVATION_AGE_MS = 100
POLICY_ID = "synthetic-reset-policy-v1"
SCENE_ID = "synthetic-table-v1"
FRAME_ID = "synthetic-world"
OBJECT_ID = "synthetic-object"
REGION_ID = "synthetic-tray"


def _xyz(value) -> bool:
    return (
        isinstance(value, tuple)
        and len(value) == 3
        and all(type(x) in (float, int) and math.isfinite(x) for x in value)
    )


class Skill(str, Enum):
    RESET = "reset"
    OPEN = "open"
    APPROACH = "approach"
    REGRASP = "regrasp"
    LIFT = "lift"
    RELEASE = "release"


@dataclass(frozen=True)
class Target:
    object_id: str
    region_id: str
    position_xyz: tuple[float, float, float]
    frame_id: str = FRAME_ID
    source: str = "seeded_synthetic"

    def __post_init__(self):
        if (
            self.object_id != OBJECT_ID
            or self.region_id != REGION_ID
            or self.frame_id != FRAME_ID
            or self.source != "seeded_synthetic"
            or not _xyz(self.position_xyz)
            or not 0.25 <= self.position_xyz[0] <= 0.45
            or not -0.12 <= self.position_xyz[1] <= 0.12
            or self.position_xyz[2] != 0.05
        ):
            raise ValueError("target is outside the synthetic scene whitelist")

    def public(self) -> dict:
        return {
            "object_id": self.object_id,
            "region_id": self.region_id,
            "position_xyz": list(self.position_xyz),
            "frame_id": self.frame_id,
            "source": self.source,
        }


@dataclass(frozen=True)
class SkillProposal:
    skill: Skill
    target: Target

    def __post_init__(self):
        if not isinstance(self.skill, Skill) or not isinstance(self.target, Target):
            raise TypeError("a typed skill and whitelisted target are required")


@dataclass(frozen=True)
class Observation:
    run_id: str
    scene_id: str
    policy_epoch: int
    seq: int
    captured_ms: int
    object_xyz: tuple[float, float, float] | None
    gripper_xyz: tuple[float, float, float] | None
    gripper_measured: float | None
    object_id: str = OBJECT_ID
    frame_id: str = FRAME_ID
    source: str = "synthetic_measurement"

    def __post_init__(self):
        if (
            not isinstance(self.run_id, str)
            or not self.run_id
            or self.scene_id != SCENE_ID
            or type(self.policy_epoch) is not int
            or self.policy_epoch < 1
            or type(self.seq) is not int
            or self.seq < 1
            or type(self.captured_ms) is not int
            or self.captured_ms < 0
            or self.object_id != OBJECT_ID
            or self.frame_id != FRAME_ID
            or self.source != "synthetic_measurement"
            or any(
                x is not None and not _xyz(x)
                for x in (self.object_xyz, self.gripper_xyz)
            )
            or (
                self.gripper_measured is not None
                and (
                    type(self.gripper_measured) not in (float, int)
                    or not math.isfinite(self.gripper_measured)
                    or not 0 <= self.gripper_measured <= 1
                )
            )
        ):
            raise ValueError("invalid synthetic observation")


def observation_problem(
    observation: Observation,
    *,
    run_id: str,
    policy_epoch: int,
    now_ms: int,
    after_seq: int = 0,
    after_ms: int = -1,
) -> str | None:
    if observation.run_id != run_id or observation.scene_id != SCENE_ID:
        return "observation_identity_mismatch"
    if observation.policy_epoch != policy_epoch:
        return "observation_epoch_mismatch"
    if observation.seq <= after_seq or observation.captured_ms <= after_ms:
        return "observation_not_new"
    if not 0 <= now_ms - observation.captured_ms <= MAX_OBSERVATION_AGE_MS:
        return "observation_stale"
    return None


def verify_holding(before: Observation, after: Observation, *, now_ms: int) -> dict:
    """Require post-lift object following AND a measured closed gripper."""
    issue = observation_problem(
        before,
        run_id=before.run_id,
        policy_epoch=before.policy_epoch,
        now_ms=now_ms,
    ) or observation_problem(
        after,
        run_id=before.run_id,
        policy_epoch=before.policy_epoch,
        now_ms=now_ms,
        after_seq=before.seq,
        after_ms=before.captured_ms,
    )
    evidence = {
        "source": "synthetic_measurement",
        "run_id": before.run_id,
        "scene_id": before.scene_id,
        "policy_epoch": before.policy_epoch,
        "object_id": before.object_id,
        "frame_id": before.frame_id,
        "before_seq": before.seq,
        "after_seq": after.seq,
        "before_captured_ms": before.captured_ms,
        "after_captured_ms": after.captured_ms,
        "object_follow": None,
        "gripper_measurement": after.gripper_measured,
        "post_lift": None,
    }
    if issue:
        return {"status": "unknown", "reason": issue, "evidence": evidence}
    if any(
        value is None
        for value in (
            before.object_xyz,
            before.gripper_xyz,
            after.object_xyz,
            after.gripper_xyz,
            after.gripper_measured,
        )
    ):
        return {
            "status": "unknown",
            "reason": "holding_measurement_missing",
            "evidence": evidence,
        }
    object_delta = tuple(b - a for a, b in zip(before.object_xyz, after.object_xyz))
    gripper_delta = tuple(b - a for a, b in zip(before.gripper_xyz, after.gripper_xyz))
    post_lift = gripper_delta[2] >= 0.03
    follows = post_lift and math.dist(object_delta, gripper_delta) <= 0.005
    closed = after.gripper_measured <= 0.3
    evidence.update(
        object_follow=follows,
        post_lift=post_lift,
        object_delta=list(object_delta),
        gripper_delta=list(gripper_delta),
        gripper_closed=closed,
    )
    passed = follows and closed
    return {
        "status": "pass" if passed else "fail",
        "reason": "post_lift_follow_and_gripper_measured"
        if passed
        else "object_not_held",
        "evidence": evidence,
    }


@dataclass(frozen=True)
class HandoffRequest:
    run_id: str
    task_policy_id: str
    previous_epoch: int
    policy_epoch: int
    after_seq: int
    after_ms: int

    def __post_init__(self):
        if (
            not isinstance(self.run_id, str)
            or not self.run_id
            or not isinstance(self.task_policy_id, str)
            or not self.task_policy_id
            or any(
                type(value) is not int or value < minimum
                for value, minimum in (
                    (self.previous_epoch, 1),
                    (self.policy_epoch, 1),
                    (self.after_seq, 1),
                    (self.after_ms, 0),
                )
            )
        ):
            raise ValueError("invalid handoff request")


def validate_handoff(
    request: HandoffRequest,
    observation: Observation,
    *,
    holding: dict,
    pending_chunks: tuple,
    now_ms: int,
) -> dict:
    proof = holding.get("evidence")
    reason = observation_problem(
        observation,
        run_id=request.run_id,
        policy_epoch=request.policy_epoch,
        now_ms=now_ms,
        after_seq=request.after_seq,
        after_ms=request.after_ms,
    )
    if request.task_policy_id != POLICY_ID:
        reason = "task_policy_mismatch"
    elif request.policy_epoch != request.previous_epoch + 1:
        reason = "handoff_epoch_not_advanced"
    elif pending_chunks:
        reason = "old_policy_chunks_present"
    elif holding.get("status") != "pass":
        reason = "holding_not_proven"
    elif (
        not isinstance(proof, dict)
        or any(
            type(proof.get(key)) is not int or proof[key] < minimum
            for key, minimum in {
                "before_seq": 1,
                "after_seq": 1,
                "before_captured_ms": 0,
                "after_captured_ms": 0,
            }.items()
        )
        or (
            proof["before_seq"] >= proof["after_seq"]
            or proof["before_captured_ms"] >= proof["after_captured_ms"]
        )
    ):
        reason = "holding_evidence_time_invalid"
    elif any(
        proof.get(key) != expected
        for key, expected in {
            "run_id": request.run_id,
            "scene_id": SCENE_ID,
            "policy_epoch": request.previous_epoch,
            "after_seq": request.after_seq,
            "after_captured_ms": request.after_ms,
            "object_id": observation.object_id,
            "frame_id": observation.frame_id,
            "source": "synthetic_measurement",
            "object_follow": True,
            "post_lift": True,
            "gripper_closed": True,
        }.items()
    ):
        reason = "holding_evidence_binding_mismatch"
    elif any(
        not 0 <= now_ms - proof[key] <= MAX_OBSERVATION_AGE_MS
        for key in ("before_captured_ms", "after_captured_ms")
    ):
        reason = "holding_evidence_stale"
    elif (
        observation.object_xyz is None
        or observation.gripper_xyz is None
        or observation.gripper_measured is None
        or observation.gripper_measured > 0.3
        or math.dist(observation.object_xyz, observation.gripper_xyz) > 0.01
    ):
        reason = "handoff_object_gripper_relation_unknown"
    return {
        "eligible": reason is None,
        "physical_allowed": False,
        "reason": reason or "offline_handoff_verified",
        "policy_epoch": request.policy_epoch,
    }


def verify_final_reset(
    observation: Observation,
    target: Target,
    *,
    run_id: str,
    policy_epoch: int,
    now_ms: int,
    after_seq: int,
    after_ms: int,
) -> dict:
    """Fresh synthetic placement and release evidence, separate from holding."""
    issue = observation_problem(
        observation,
        run_id=run_id,
        policy_epoch=policy_epoch,
        now_ms=now_ms,
        after_seq=after_seq,
        after_ms=after_ms,
    )
    evidence = {
        "source": observation.source,
        "run_id": observation.run_id,
        "scene_id": observation.scene_id,
        "policy_epoch": observation.policy_epoch,
        "observation_seq": observation.seq,
        "target_position_xyz": list(target.position_xyz),
        "measured_object_xyz": list(observation.object_xyz)
        if observation.object_xyz is not None
        else None,
        "measured_gripper_xyz": list(observation.gripper_xyz)
        if observation.gripper_xyz is not None
        else None,
        "gripper_measurement": observation.gripper_measured,
        "object_at_target": None,
        "released": None,
    }
    if issue or any(
        value is None
        for value in (
            observation.object_xyz,
            observation.gripper_xyz,
            observation.gripper_measured,
        )
    ):
        return {
            "status": "unknown",
            "reason": issue or "final_reset_measurement_missing",
            "evidence": evidence,
        }
    at_target = math.dist(observation.object_xyz, target.position_xyz) <= 0.005
    released = (
        observation.gripper_measured >= 0.9
        and math.dist(observation.object_xyz, observation.gripper_xyz) >= 0.03
    )
    evidence.update(object_at_target=at_target, released=released)
    passed = at_target and released
    return {
        "status": "pass" if passed else "fail",
        "reason": "synthetic_final_reset_verified"
        if passed
        else "synthetic_final_reset_not_verified",
        "evidence": evidence,
    }


class Planner(Protocol):
    def propose(self, skill: Skill, target: Target) -> SkillProposal: ...


class SkillExecutor(Protocol):
    def execute(self, proposal: SkillProposal) -> None: ...


class ScriptedPlanner:
    """A deterministic fixture provider, deliberately not called an LLM."""

    def propose(self, skill: Skill, target: Target) -> SkillProposal:
        return SkillProposal(skill, target)


class FakeSkillExecutor:
    """Only synthetic state. There is no injectable hardware/backend argument."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.epoch = 1
        self.seq = 0
        self.time_ms = 1000
        self.object_xyz = (0.3, 0.0, 0.05)
        self.gripper_xyz = self.object_xyz
        self.gripper_measured = 1.0
        self.held = False
        self.pending_chunks = [(1, "synthetic-old-chunk")]
        self.calls: list[str] = []
        self._reset_admitted = False
        self._placed_target: Target | None = None

    def observe(self, *, missing=False, stale=False) -> Observation:
        self.seq += 1
        self.time_ms += 10
        return Observation(
            self.run_id,
            SCENE_ID,
            self.epoch,
            self.seq,
            max(0, self.time_ms - 200) if stale else self.time_ms,
            None if missing else self.object_xyz,
            self.gripper_xyz,
            None if missing else self.gripper_measured,
        )

    def execute(self, proposal: SkillProposal) -> None:
        if not isinstance(proposal, SkillProposal):
            raise TypeError("typed proposal required")
        if proposal.skill == Skill.OPEN:
            if self.held:
                raise ValueError("cannot open while holding before reset placement")
            self.gripper_measured = 1.0
        elif proposal.skill == Skill.APPROACH:
            if self.held or self.gripper_measured < 0.9:
                raise ValueError("approach requires a verified open empty gripper")
            self.gripper_xyz = self.object_xyz
        elif proposal.skill == Skill.RESET:
            if not self._reset_admitted or not self.held:
                raise ValueError("reset placement requires policy handoff and holding")
            self.object_xyz = proposal.target.position_xyz
            self.gripper_xyz = self.object_xyz
            self._placed_target = proposal.target
        elif proposal.skill == Skill.REGRASP:
            if (
                self.gripper_measured < 0.9
                or math.dist(self.gripper_xyz, self.object_xyz) > 0.005
            ):
                raise ValueError(
                    "close requires a verified open gripper at the grasp pose"
                )
            self.held = True
            self.gripper_measured = 0.2
        elif proposal.skill == Skill.LIFT:
            self.gripper_xyz = (*self.gripper_xyz[:2], self.gripper_xyz[2] + 0.04)
            if self.held:
                self.object_xyz = self.gripper_xyz
        elif proposal.skill == Skill.RELEASE:
            if self._placed_target != proposal.target:
                raise ValueError("release requires the reset policy placement")
            self.held = False
            self.gripper_measured = 1.0
            self.gripper_xyz = (*self.gripper_xyz[:2], self.gripper_xyz[2] + 0.04)
        self.calls.append(proposal.skill.value)
        self.time_ms += 10

    def clear_policy_chunks(self) -> int:
        count = len(self.pending_chunks)
        self.pending_chunks.clear()
        return count

    def execute_policy(self, handoff: dict, target: Target) -> None:
        if (
            handoff.get("eligible") is not True
            or handoff.get("physical_allowed") is not False
        ):
            raise ValueError("offline eligible handoff required")
        if handoff.get("policy_epoch") != self.epoch or self.pending_chunks:
            raise ValueError("policy admission mismatch")
        self.calls.append("fake_reset_policy")
        self._reset_admitted = True
        try:
            self.execute(SkillProposal(Skill.RESET, target))
        finally:
            self._reset_admitted = False


def run_scenario(
    scenario_id: str,
    seed: int,
    run_id: str,
    *,
    on_event: Callable[[dict], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    if scenario_id not in SCENARIOS:
        raise ValueError("unknown offline scenario")
    if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
        raise ValueError("seed must be an integer from 0 to 4294967295")
    if not isinstance(run_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", run_id
    ):
        raise ValueError("invalid offline run id")
    scenario = SCENARIOS[scenario_id]
    rng = random.Random(seed)
    target = Target(
        OBJECT_ID,
        REGION_ID,
        (round(rng.uniform(0.25, 0.45), 6), round(rng.uniform(-0.12, 0.12), 6), 0.05),
    )
    began = time.monotonic()
    planner = ScriptedPlanner()
    executor = FakeSkillExecutor(run_id)
    # Reproduce the user's failure condition in a labelled synthetic fixture:
    # the empty gripper is closed above the object. No real observation is
    # classified empty from aperture alone.
    executor.gripper_measured = 0.0
    executor.gripper_xyz = (*executor.object_xyz[:2], executor.object_xyz[2] + 0.1)
    result = {
        "schema": SCHEMA,
        "execution_mode": "offline",
        "adapter": "fake",
        "physical_execution": False,
        "status": "running",
        "stage": "created",
        "stage_seq": 0,
        "events": [],
        "target": target.public(),
        "holding": {"status": "unknown", "reason": "not_observed", "evidence": {}},
        "final_observation": {
            "status": "unknown",
            "reason": "not_observed",
            "evidence": {},
        },
        "handoff": {
            "eligible": False,
            "physical_allowed": False,
            "reason": "not_checked",
            "policy_epoch": None,
        },
        "usage": {
            "llm_calls": 0,
            "input_tokens": None,
            "output_tokens": None,
            "token_source": "unavailable",
            "provider": "scripted",
        },
        "elapsed_ms": 0,
        "run_id": run_id,
        "scenario_id": scenario_id,
        "seed": seed,
    }

    def emit(stage, status="running", reason="", **evidence):
        result["stage"] = stage
        result["stage_seq"] += 1
        result["elapsed_ms"] = max(0, int((time.monotonic() - began) * 1000))
        result["events"].append(
            {
                "seq": result["stage_seq"],
                "stage": stage,
                "status": status,
                "reason": reason,
                "elapsed_ms": result["elapsed_ms"],
                "evidence": evidence,
            }
        )
        if on_event is not None:
            on_event(copy.deepcopy(result))

    def stopped(stage):
        if should_stop is not None and should_stop():
            result["status"] = "stopped"
            result["handoff"].update(eligible=False, reason="operator_stop")
            emit(stage, "stopped", "operator_stop")
            return True
        return False

    def blocked(stage, reason):
        result["status"] = "blocked"
        result["handoff"].update(eligible=False, reason=reason)
        emit(stage, "blocked", reason)
        return result

    def execute_skill(skill, stage):
        proposal = planner.propose(skill, target)
        # Observation and proposal preparation can outlast a stop request.
        # Recheck at dispatch; this does not interrupt an executing skill.
        if stopped(stage):
            return False
        executor.execute(proposal)
        return True

    if stopped("observe"):
        return result
    initial = executor.observe(stale=scenario.stale_observation)
    issue = observation_problem(
        initial, run_id=run_id, policy_epoch=1, now_ms=executor.time_ms
    )
    emit(
        "observe",
        reason=issue or "synthetic_observation",
        observation_seq=initial.seq,
        source=initial.source,
        initial_fixture="closed_empty_above_object",
        previous_task_terminal_object_xyz=list(initial.object_xyz)
        if initial.object_xyz is not None
        else None,
    )
    if issue:
        return blocked("observe", issue)
    if stopped("plan"):
        return result
    emit(
        "plan",
        reason="scripted_skill_target_selection",
        provider="scripted",
        target=target.public(),
    )
    if not scenario.reachable:
        return blocked("plan", "synthetic_target_unreachable")
    if scenario.stop_before_action:
        result["status"] = "stopped"
        emit("regrasp", "stopped", "scenario_stop_before_action")
        return result
    previous_attempt = initial
    for attempt in range(1, MAX_REGRASP_ATTEMPTS + 1):
        gate = GateState(
            run_id,
            f"{run_id}:attempt-{attempt}",
            executor.epoch,
            attempts=attempt - 1,
            after_seq=previous_attempt.seq,
            after_ms=previous_attempt.captured_ms,
        )

        def gate_decision(observation, *, holding_verified=None):
            nonlocal gate
            issue = observation_problem(
                observation,
                run_id=run_id,
                policy_epoch=executor.epoch,
                now_ms=executor.time_ms,
                after_seq=gate.after_seq,
                after_ms=gate.after_ms,
            )
            if issue:
                return {
                    "required_skill": None,
                    "reason": issue,
                    "physical_allowed": False,
                }
            gate, decision = advance_regrasp(
                gate,
                GateObservation(
                    observation.run_id,
                    gate.sample_id,
                    observation.policy_epoch,
                    observation.seq,
                    observation.captured_ms,
                    empty_verified=(not executor.held)
                    if gate.phase in {"verify_empty", "open", "approach"}
                    else None,
                    open_measured=None
                    if observation.gripper_measured is None
                    else observation.gripper_measured >= 0.9,
                    closed_measured=None
                    if observation.gripper_measured is None
                    else observation.gripper_measured <= 0.3,
                    approach_verified=True,  # Known synthetic geometry, unavailable in the recordings.
                    at_grasp_pose=(
                        observation.object_xyz is not None
                        and observation.gripper_xyz is not None
                        and math.dist(observation.object_xyz, observation.gripper_xyz)
                        <= 0.005
                    ),
                    holding_verified=holding_verified,
                    source=observation.source,
                ),
                [*executor.gripper_xyz, 0.0, 0.0, 0.0, 0.0],
                now_ms=executor.time_ms,
            )
            return decision

        if stopped("open_gripper"):
            return result
        decision = gate_decision(executor.observe())
        if decision["required_skill"] != "open_gripper":
            return blocked("open_gripper", decision["reason"])
        if not execute_skill(Skill.OPEN, "open_gripper"):
            return result
        opened = executor.observe()
        issue = observation_problem(
            opened,
            run_id=run_id,
            policy_epoch=executor.epoch,
            now_ms=executor.time_ms,
            after_seq=initial.seq,
            after_ms=initial.captured_ms,
        )
        if issue or opened.gripper_measured is None or opened.gripper_measured < 0.9:
            return blocked("open_gripper", issue or "opening_not_measured")
        decision = gate_decision(opened)
        if decision["required_skill"] != "approach":
            return blocked("open_gripper", decision["reason"])
        emit(
            "open_gripper",
            reason="synthetic_opening_measured",
            attempt=attempt,
            gripper_measurement=opened.gripper_measured,
            observation_seq=opened.seq,
            empty_evidence_source="synthetic_fixture",
            gate_decision=decision,
        )
        if stopped("approach"):
            return result
        if not execute_skill(Skill.APPROACH, "approach"):
            return result
        arrived = executor.observe()
        issue = observation_problem(
            arrived,
            run_id=run_id,
            policy_epoch=executor.epoch,
            now_ms=executor.time_ms,
            after_seq=opened.seq,
            after_ms=opened.captured_ms,
        )
        if (
            issue
            or arrived.object_xyz is None
            or arrived.gripper_xyz is None
            or math.dist(arrived.object_xyz, arrived.gripper_xyz) > 0.005
        ):
            return blocked("approach", issue or "grasp_pose_not_reached")
        decision = gate_decision(arrived)
        if decision["required_skill"] != "close_gripper":
            return blocked("approach", decision["reason"])
        emit(
            "approach",
            reason="synthetic_grasp_pose_measured",
            attempt=attempt,
            observation_seq=arrived.seq,
            model_pose_executed=False,
            gate_decision=decision,
        )
        if stopped("regrasp"):
            return result
        if not execute_skill(Skill.REGRASP, "regrasp"):
            return result
        if scenario.first_grasp_empty and attempt == 1:
            executor.held = False
            executor.gripper_measured = 0.0
        before = executor.observe()
        decision = gate_decision(before)
        if decision["required_skill"] != "lift_and_verify":
            return blocked("regrasp", decision["reason"])
        emit(
            "regrasp",
            reason="synthetic_grasp_attempt",
            attempt=attempt,
            gate_decision=decision,
        )
        if stopped("lift_verify"):
            return result
        if not execute_skill(Skill.LIFT, "lift_verify"):
            return result
        after = executor.observe(missing=scenario.holding_unknown)
        result["holding"] = verify_holding(before, after, now_ms=executor.time_ms)
        emit(
            "lift_verify",
            reason=result["holding"]["reason"],
            attempt=attempt,
            holding=copy.deepcopy(result["holding"]),
        )
        if result["holding"]["status"] == "pass":
            decision = gate_decision(after, holding_verified=True)
            if decision["required_skill"] != "validate_handoff":
                return blocked("lift_verify", decision["reason"])
            break
        if result["holding"]["status"] == "unknown":
            return blocked("lift_verify", result["holding"]["reason"])
        previous_attempt = after
    else:
        return blocked("regrasp", "regrasp_retry_limit")
    if stopped("handoff"):
        return result
    cleared = executor.clear_policy_chunks()
    previous_epoch = executor.epoch
    executor.epoch += 1
    handoff_observation = executor.observe()
    request = HandoffRequest(
        run_id,
        "wrong-policy" if scenario.handoff_mismatch else POLICY_ID,
        previous_epoch,
        executor.epoch,
        after.seq,
        after.captured_ms,
    )
    result["handoff"] = validate_handoff(
        request,
        handoff_observation,
        holding=result["holding"],
        pending_chunks=tuple(executor.pending_chunks),
        now_ms=executor.time_ms,
    )
    emit(
        "handoff",
        "succeeded" if result["handoff"]["eligible"] else "blocked",
        reason=result["handoff"]["reason"],
        cleared_policy_chunks=cleared,
        observation_seq=handoff_observation.seq,
        policy_epoch=executor.epoch,
    )
    if not result["handoff"]["eligible"]:
        return blocked("handoff", result["handoff"]["reason"])
    if stopped("policy"):
        return result
    executor.execute_policy(result["handoff"], target)
    emit(
        "policy",
        reason="fake_reset_policy_placed",
        policy_id=POLICY_ID,
        calls=list(executor.calls),
    )
    if stopped("release"):
        return result
    if not execute_skill(Skill.RELEASE, "release"):
        return result
    release_observation = executor.observe()
    emit(
        "release",
        reason="synthetic_release_measured",
        observation_seq=release_observation.seq,
        gripper_measurement=release_observation.gripper_measured,
    )
    if stopped("final_verify"):
        return result
    final_observation = executor.observe()
    result["final_observation"] = verify_final_reset(
        final_observation,
        target,
        run_id=run_id,
        policy_epoch=executor.epoch,
        now_ms=executor.time_ms,
        after_seq=release_observation.seq,
        after_ms=release_observation.captured_ms,
    )
    emit(
        "final_verify",
        "succeeded" if result["final_observation"]["status"] == "pass" else "blocked",
        reason=result["final_observation"]["reason"],
        final_observation=copy.deepcopy(result["final_observation"]),
    )
    if result["final_observation"]["status"] != "pass":
        return blocked("final_verify", result["final_observation"]["reason"])
    if stopped("complete"):
        return result
    result["status"] = "succeeded"
    emit("complete", "succeeded", "offline_contract_verified", physical_allowed=False)
    return result
