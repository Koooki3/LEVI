"""Evidence-gated re-grasp sequencing for offline fixtures and recorded inputs.

This module issues *skill requests*, never robot commands or new coordinates.
It does not infer emptiness/holding from gripper width or from a command. The
caller must supply independently verified facts; missing recorded geometry or
holding evidence stays ``None``. Existing holding and policy-handoff validators
remain responsible for proving object following and clearing old policy chunks.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

PHASES = frozenset(
    {"verify_empty", "open", "approach", "close", "lift_verify", "handoff"}
)
SOURCES = frozenset(
    {"synthetic_measurement", "recorded_measurement", "verified_measurement"}
)


def _identity(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 256


def _integer(value: object, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


@dataclass(frozen=True)
class GateState:
    run_id: str
    sample_id: str
    policy_epoch: int
    phase: str = "verify_empty"
    after_seq: int = 0
    after_ms: int = -1
    attempts: int = 0
    phase_started_ms: int | None = None

    def __post_init__(self):
        if (
            not _identity(self.run_id)
            or not _identity(self.sample_id)
            or not _integer(self.policy_epoch, 1)
            or self.phase not in PHASES
            or not _integer(self.after_seq)
            or not _integer(self.after_ms, -1)
            or not _integer(self.attempts)
            or (
                self.phase_started_ms is not None
                and not _integer(self.phase_started_ms)
            )
        ):
            raise ValueError("invalid re-grasp state")


@dataclass(frozen=True)
class GateObservation:
    run_id: str
    sample_id: str
    policy_epoch: int
    seq: int
    captured_ms: int
    empty_verified: bool | None = None
    open_measured: bool | None = None
    approach_verified: bool | None = None
    at_grasp_pose: bool | None = None
    holding_verified: bool | None = None
    source: str = "recorded_measurement"
    closed_measured: bool | None = None

    def __post_init__(self):
        if (
            not _identity(self.run_id)
            or not _identity(self.sample_id)
            or not _integer(self.policy_epoch, 1)
            or not _integer(self.seq, 1)
            or not _integer(self.captured_ms)
            or self.source not in SOURCES
            or any(
                value is not None and type(value) is not bool
                for value in (
                    self.empty_verified,
                    self.open_measured,
                    self.closed_measured,
                    self.approach_verified,
                    self.at_grasp_pose,
                    self.holding_verified,
                )
            )
        ):
            raise ValueError("invalid re-grasp observation")


def advance_regrasp(
    state: GateState,
    observation: GateObservation,
    raw_action: list[float] | tuple[float, ...],
    *,
    now_ms: int,
    max_age_ms: int = 100,
    phase_timeout_ms: int = 2000,
    max_attempts: int = 2,
) -> tuple[GateState, dict]:
    """Return the next pure state and a JSON-safe offline decision.

    ``open_measured`` and ``closed_measured`` must come from measurement, not
    ``raw_action[6]``. A gripper that is not fully open is not necessarily closed.
    ``at_grasp_pose`` and ``approach_verified`` refer to independently checked
    grasp geometry, not merely the model's requested position. Each request
    requires a newer observation before advancing. All phases reject the whole
    raw model action: even a legal close permits only an independent close skill.
    ``handoff`` requests the existing handoff validator, not direct policy use.
    Thresholds/timeouts are offline contract values, not reviewed robot limits.
    """
    if not isinstance(state, GateState) or not isinstance(observation, GateObservation):
        raise TypeError("typed state and observation required")
    if (
        not _integer(now_ms)
        or not _integer(max_age_ms, 1)
        or not _integer(phase_timeout_ms, 1)
        or not _integer(max_attempts, 1)
    ):
        raise ValueError("invalid gate limits or clock")
    if (
        not isinstance(raw_action, (list, tuple))
        or len(raw_action) != 7
        or any(
            type(value) not in (int, float) or not math.isfinite(value)
            for value in raw_action
        )
    ):
        raise ValueError("raw policy proposal must be seven finite real numbers")
    obs = observation
    base = {
        "run_id": obs.run_id,
        "sample_id": obs.sample_id,
        "policy_epoch": obs.policy_epoch,
        "seq": obs.seq,
        "captured_ms": obs.captured_ms,
        "source": obs.source,
        "empty_verified": obs.empty_verified,
        "open_measured": obs.open_measured,
        "closed_measured": obs.closed_measured,
        "approach_verified": obs.approach_verified,
        "at_grasp_pose": obs.at_grasp_pose,
        "holding_verified": obs.holding_verified,
        "raw_gripper": raw_action[6],
        "raw_proposes_close": raw_action[6] < 0.5,
        "raw_gripper_out_of_range": not 0 <= raw_action[6] <= 1,
        "evidence_kind": "synthetic"
        if obs.source == "synthetic_measurement"
        else "recorded_or_verified",
    }

    def answer(
        status: str,
        reason: str,
        skill: str | None = None,
        *,
        phase=None,
        increment_attempt=False,
        consume=True,
    ):
        updated = state
        if consume:
            changes = {"after_seq": obs.seq, "after_ms": obs.captured_ms}
            if phase is not None and phase != state.phase:
                changes.update(phase=phase, phase_started_ms=obs.captured_ms)
            if increment_attempt:
                changes["attempts"] = state.attempts + 1
            updated = replace(state, **changes)
        return updated, {
            "schema": "levi.regrasp_gate.decision.v1",
            "execution_mode": "offline",
            "status": status,
            "reason": reason,
            "required_skill": skill,
            "model_proposal_allowed": False,
            "physical_allowed": False,
            "phase": updated.phase,
            "attempts": updated.attempts,
            "evidence": dict(base),
        }

    if (obs.run_id, obs.sample_id, obs.policy_epoch) != (
        state.run_id,
        state.sample_id,
        state.policy_epoch,
    ):
        return answer("blocked", "observation_identity_mismatch", consume=False)
    if obs.seq <= state.after_seq or obs.captured_ms <= state.after_ms:
        return answer("blocked", "observation_not_new", consume=False)
    if not 0 <= now_ms - obs.captured_ms <= max_age_ms:
        return answer("blocked", "observation_stale", consume=False)
    if obs.open_measured is True and obs.closed_measured is True:
        return answer("blocked", "contradictory_gripper_evidence")
    if obs.holding_verified is True and (
        obs.empty_verified is True or obs.open_measured is True
    ):
        return answer("blocked", "contradictory_holding_evidence")
    if obs.at_grasp_pose is True and obs.approach_verified is False:
        return answer("blocked", "contradictory_geometry_evidence")
    if state.attempts > max_attempts:
        return answer("blocked", "regrasp_retry_limit")
    if (
        state.phase in {"open", "approach", "close", "lift_verify"}
        and state.phase_started_ms is not None
        and obs.captured_ms - state.phase_started_ms > phase_timeout_ms
    ):
        return answer("blocked", "regrasp_phase_timeout")

    # A proven held object must never be opened by a re-grasp retry. It may
    # request the separate handoff validator, which still checks its proof.
    if obs.holding_verified is True:
        return answer(
            "ready",
            "holding_verified_requires_handoff_check",
            "validate_handoff",
            phase="handoff",
        )
    if state.phase == "handoff":
        return answer(
            "unknown" if obs.holding_verified is None else "blocked",
            "holding_not_verified",
        )
    if state.phase == "lift_verify":
        if obs.holding_verified is None:
            return answer("unknown", "holding_not_verified")
        if state.attempts >= max_attempts:
            return answer("blocked", "regrasp_retry_limit")
        if obs.empty_verified is not True:
            return answer("unknown", "empty_not_verified_for_retry")
        return answer(
            "request",
            "verified_empty_retry_requires_open",
            "open_gripper",
            phase="open",
        )

    if (
        state.phase in {"verify_empty", "open", "approach"}
        and obs.empty_verified is not True
    ):
        return answer(
            "unknown" if obs.empty_verified is None else "blocked", "empty_not_verified"
        )
    if state.phase == "verify_empty":
        if state.attempts >= max_attempts:
            return answer("blocked", "regrasp_retry_limit")
        return answer(
            "request",
            "verified_empty_requires_open_confirmation",
            "open_gripper",
            phase="open",
        )
    if state.phase == "open":
        if obs.open_measured is not True:
            return answer(
                "unknown" if obs.open_measured is None else "waiting",
                "waiting_measured_open",
            )
        if obs.approach_verified is not True:
            return answer(
                "unknown" if obs.approach_verified is None else "blocked",
                "approach_geometry_not_verified",
            )
        return answer(
            "request",
            "measured_open_allows_verified_approach",
            "approach",
            phase="approach",
        )
    if state.phase == "approach":
        if obs.open_measured is not True:
            if obs.open_measured is False:
                return answer(
                    "request",
                    "gripper_closed_before_contact",
                    "open_gripper",
                    phase="open",
                )
            return answer("unknown", "open_measurement_missing")
        if obs.approach_verified is not True:
            return answer(
                "unknown" if obs.approach_verified is None else "blocked",
                "approach_geometry_not_verified",
            )
        if obs.at_grasp_pose is not True:
            reason = (
                "premature_close_not_at_grasp_pose"
                if raw_action[6] < 0.5
                else "not_at_grasp_pose"
            )
            return answer("unknown" if obs.at_grasp_pose is None else "waiting", reason)
        if state.attempts >= max_attempts:
            return answer("blocked", "regrasp_retry_limit")
        return answer(
            "request",
            "measured_open_and_grasp_pose_verified",
            "close_gripper",
            phase="close",
            increment_attempt=True,
        )
    # The close request is not proof of contact. First require a new measured
    # closure, then request a bounded lift and the existing following verifier.
    if obs.closed_measured is not True:
        return answer(
            "unknown" if obs.closed_measured is None else "waiting",
            "waiting_measured_close",
        )
    if obs.approach_verified is not True or obs.at_grasp_pose is not True:
        return answer("unknown", "grasp_geometry_not_verified_after_close")
    return answer(
        "request",
        "closure_requires_lift_holding_verification",
        "lift_and_verify",
        phase="lift_verify",
    )
