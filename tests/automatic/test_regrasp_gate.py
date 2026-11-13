"""Re-grasp decisions never treat closure, an acknowledgement or width as holding."""

import json
from dataclasses import replace

import pytest

from levi.automatic.regrasp_gate import GateObservation, GateState, advance_regrasp

CLOSE = (0.3, 0.0, 0.05, 3.14, 0.0, 0.0, 0.0)
OPEN = (*CLOSE[:6], 1.0)


def initial():
    return GateState("run", "object", 1)


def measured(sample_seq, **changes):
    fields = {
        "run_id": "run",
        "sample_id": "object",
        "policy_epoch": 1,
        "seq": sample_seq,
        "captured_ms": 1000 + 10 * sample_seq,
        "source": "synthetic_measurement",
        "empty_verified": True,
        "open_measured": False,
        "approach_verified": True,
        "at_grasp_pose": False,
        "holding_verified": False,
    }
    fields.update(changes)
    # The default synthetic fixture uses fully open/closed endpoints only.
    fields.setdefault(
        "closed_measured",
        None if fields["open_measured"] is None else not fields["open_measured"],
    )
    return GateObservation(**fields)


def step(state, obs, action=CLOSE, **limits):
    return advance_regrasp(state, obs, action, now_ms=obs.captured_ms, **limits)


def at_close_request():
    state, _ = step(initial(), measured(1))
    state, _ = step(state, measured(2, open_measured=True))
    return step(state, measured(3, open_measured=True, at_grasp_pose=True))


def test_closed_empty_starts_with_open_not_raw_policy_close():
    state, decision = step(initial(), measured(1))
    assert state.phase == "open"
    assert decision["required_skill"] == "open_gripper"
    assert decision["model_proposal_allowed"] is False
    assert decision["physical_allowed"] is False
    assert decision["evidence"]["raw_proposes_close"] is True


def test_commanded_open_without_measured_open_does_not_approach():
    state, _ = step(initial(), measured(1))
    state, answer = step(state, measured(2), OPEN)
    assert state.phase == "open"
    assert answer["status"] == "waiting"
    assert answer["reason"] == "waiting_measured_open"
    assert answer["required_skill"] is None


def test_new_closure_field_preserves_original_positional_observation_arguments():
    observation = GateObservation(
        "run",
        "object",
        1,
        1,
        1000,
        True,
        False,
        True,
        False,
        False,
        "synthetic_measurement",
    )
    assert observation.source == "synthetic_measurement"
    assert observation.closed_measured is None


@pytest.mark.parametrize("closed_measured", [None, False])
@pytest.mark.parametrize("action", [CLOSE, OPEN])
def test_not_open_is_not_closed_and_raw_command_cannot_allow_lift(
    closed_measured, action
):
    state, _ = at_close_request()
    updated, answer = step(
        state,
        measured(
            4,
            at_grasp_pose=True,
            empty_verified=None,
            holding_verified=None,
            open_measured=False,
            closed_measured=closed_measured,
        ),
        action,
    )
    assert updated.phase == "close"
    assert answer["status"] == ("unknown" if closed_measured is None else "waiting")
    assert answer["reason"] == "waiting_measured_close"
    assert answer["required_skill"] is None
    assert answer["model_proposal_allowed"] is False
    assert answer["physical_allowed"] is False


def test_measured_closure_allows_lift_even_with_open_raw_proposal():
    state, _ = at_close_request()
    _, answer = step(
        state,
        measured(4, at_grasp_pose=True, closed_measured=True),
        OPEN,
    )
    assert answer["required_skill"] == "lift_and_verify"
    assert answer["model_proposal_allowed"] is False
    assert answer["physical_allowed"] is False


def test_verified_sequence_requests_independent_skills_then_handoff():
    state = initial()
    inputs = [
        measured(1),
        measured(2, open_measured=True),
        measured(3, open_measured=True, at_grasp_pose=True),
        measured(4, at_grasp_pose=True, empty_verified=None, holding_verified=None),
        measured(5, empty_verified=False, holding_verified=True),
    ]
    decisions = []
    for obs in inputs:
        state, decision = step(state, obs)
        decisions.append(decision)
    assert [x["required_skill"] for x in decisions] == [
        "open_gripper",
        "approach",
        "close_gripper",
        "lift_and_verify",
        "validate_handoff",
    ]
    assert state.phase == "handoff" and state.attempts == 1
    assert all(
        not x["model_proposal_allowed"] and not x["physical_allowed"] for x in decisions
    )
    assert json.loads(json.dumps(decisions, allow_nan=False)) == decisions


def test_close_before_grasp_pose_is_waiting_and_not_counted_as_attempt():
    state, _ = step(initial(), measured(1))
    state, _ = step(state, measured(2, open_measured=True))
    state, answer = step(state, measured(3, open_measured=True))
    assert answer["reason"] == "premature_close_not_at_grasp_pose"
    assert answer["required_skill"] is None
    assert state.attempts == 0


def test_gripper_closed_while_approaching_requires_reopen_only_when_verified_empty():
    state, _ = step(initial(), measured(1))
    state, _ = step(state, measured(2, open_measured=True))
    updated, answer = step(state, measured(3))
    assert updated.phase == "open" and answer["required_skill"] == "open_gripper"
    _, unknown = step(state, measured(3, empty_verified=None))
    assert unknown["status"] == "unknown" and unknown["required_skill"] is None


@pytest.mark.parametrize("open_measured", [False, True, None])
def test_recorded_gripper_measurement_without_independent_empty_evidence_blocks(
    open_measured,
):
    observation = GateObservation(
        "run", "object", 1, 1, 1000, open_measured=open_measured
    )
    _, decision = step(initial(), observation)
    assert decision["status"] == "unknown"
    assert decision["reason"] == "empty_not_verified"
    assert decision["required_skill"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", "other"),
        ("sample_id", "other"),
        ("policy_epoch", 2),
        ("seq", 1),
        ("captured_ms", 1010),
    ],
)
def test_foreign_or_old_measurements_never_advance(field, value):
    state, _ = step(initial(), measured(1))
    obs = replace(measured(2, open_measured=True), **{field: value})
    updated, answer = step(state, obs)
    assert answer["status"] == "blocked" and answer["required_skill"] is None
    assert updated == state


@pytest.mark.parametrize("now_ms", [900, 1200])
def test_stale_or_future_measurement_blocks(now_ms):
    state = initial()
    updated, answer = advance_regrasp(state, measured(1), CLOSE, now_ms=now_ms)
    assert answer["reason"] == "observation_stale" and updated == state


def test_verified_holding_never_requests_open_even_for_an_open_model_action():
    _, answer = step(
        initial(), measured(1, empty_verified=False, holding_verified=True), OPEN
    )
    assert answer["required_skill"] == "validate_handoff"
    assert answer["model_proposal_allowed"] is False


def test_unknown_holding_after_lift_blocks_without_auto_retry():
    state, _ = at_close_request()
    state, _ = step(state, measured(4, at_grasp_pose=True))
    state, answer = step(state, measured(5, empty_verified=None, holding_verified=None))
    assert state.phase == "lift_verify"
    assert answer["status"] == "unknown" and answer["required_skill"] is None


def test_retry_requires_verified_empty_and_never_exceeds_two_close_requests():
    state, _ = at_close_request()
    state, _ = step(state, measured(4, at_grasp_pose=True))
    state, answer = step(state, measured(5))
    assert answer["required_skill"] == "open_gripper"
    state, _ = step(state, measured(6, open_measured=True))
    state, _ = step(state, measured(7, open_measured=True, at_grasp_pose=True))
    assert state.attempts == 2
    state, _ = step(state, measured(8, at_grasp_pose=True))
    _, answer = step(state, measured(9))
    assert answer["reason"] == "regrasp_retry_limit"
    assert answer["required_skill"] is None


def test_waiting_has_bounded_phase_timeout():
    state, _ = step(initial(), measured(1))
    _, answer = step(state, measured(300))
    assert answer["status"] == "blocked" and answer["reason"] == "regrasp_phase_timeout"


@pytest.mark.parametrize(
    "changes",
    [
        {"empty_verified": True, "holding_verified": True},
        {"open_measured": True, "empty_verified": False, "holding_verified": True},
        {"at_grasp_pose": True, "approach_verified": False},
        {"open_measured": True, "closed_measured": True},
    ],
)
def test_contradictory_evidence_never_issues_a_skill(changes):
    _, answer = step(initial(), measured(1, **changes))
    assert answer["status"] == "blocked" and answer["required_skill"] is None


@pytest.mark.parametrize("bad", [True, float("nan"), float("inf"), "0"])
def test_bad_action_values_fail_closed(bad):
    with pytest.raises(ValueError):
        step(initial(), measured(1), (*CLOSE[:6], bad))


@pytest.mark.parametrize(
    "field",
    [
        "empty_verified",
        "open_measured",
        "closed_measured",
        "approach_verified",
        "at_grasp_pose",
        "holding_verified",
    ],
)
def test_truthy_integer_is_not_verified_evidence(field):
    with pytest.raises(ValueError):
        measured(1, **{field: 1})


@pytest.mark.parametrize(
    "changes",
    [
        {"seq": True},
        {"policy_epoch": False},
        {"captured_ms": float("nan")},
        {"source": "model_guess"},
    ],
)
def test_invalid_observation_identity_clock_or_source(changes):
    with pytest.raises(ValueError):
        measured(1, **changes)
