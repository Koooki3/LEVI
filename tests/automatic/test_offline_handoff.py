from dataclasses import replace

import pytest

from levi.automatic.offline_harness import (
    POLICY_ID,
    FakeSkillExecutor,
    HandoffRequest,
    Observation,
    Skill,
    SkillProposal,
    Target,
    run_scenario,
    validate_handoff,
    verify_final_reset,
    verify_holding,
)


def measured_pair():
    executor = FakeSkillExecutor("handoff-test")
    target = Target("synthetic-object", "synthetic-tray", (0.3, 0.0, 0.05))
    executor.execute(SkillProposal(Skill.REGRASP, target))
    before = executor.observe()
    executor.execute(SkillProposal(Skill.LIFT, target))
    after = executor.observe()
    return executor, before, after


def handoff_inputs():
    executor, before, after = measured_pair()
    holding = verify_holding(before, after, now_ms=executor.time_ms)
    executor.clear_policy_chunks()
    executor.epoch += 1
    observation = executor.observe()
    request = HandoffRequest(
        executor.run_id, POLICY_ID, 1, 2, after.seq, after.captured_ms
    )
    return executor, request, observation, holding


@pytest.mark.parametrize(
    "mutation",
    [
        "no_follow",
        "open_gripper",
        "missing_gripper",
        "missing_object",
        "old_seq",
        "wrong_run",
        "wrong_epoch",
        "stale",
    ],
)
def test_holding_requires_bound_fresh_follow_and_measured_gripper(mutation):
    executor, before, after = measured_pair()
    after = {
        "no_follow": lambda: replace(after, object_xyz=before.object_xyz),
        "open_gripper": lambda: replace(after, gripper_measured=1.0),
        "missing_gripper": lambda: replace(after, gripper_measured=None),
        "missing_object": lambda: replace(after, object_xyz=None),
        "old_seq": lambda: replace(after, seq=before.seq),
        "wrong_run": lambda: replace(after, run_id="another-run"),
        "wrong_epoch": lambda: replace(after, policy_epoch=2),
        "stale": lambda: replace(after, captured_ms=0),
    }[mutation]()
    now_ms = 1000 if mutation == "stale" else executor.time_ms
    assert verify_holding(before, after, now_ms=now_ms)["status"] != "pass"


@pytest.mark.parametrize(
    "mutation",
    [
        "pending",
        "epoch",
        "policy",
        "old_seq",
        "old_time",
        "stale",
        "run",
        "unknown_holding",
        "foreign_proof",
        "open_gripper",
        "object_far",
    ],
)
def test_handoff_refuses_ambiguous_or_cross_context_inputs(mutation):
    executor, request, observation, holding = handoff_inputs()
    pending = ()
    now_ms = executor.time_ms
    if mutation == "pending":
        pending = ((1, "old"),)
    elif mutation == "epoch":
        request = replace(request, policy_epoch=1)
    elif mutation == "policy":
        request = replace(request, task_policy_id="another-policy")
    elif mutation == "old_seq":
        observation = replace(observation, seq=request.after_seq)
    elif mutation == "old_time":
        observation = replace(observation, captured_ms=request.after_ms)
    elif mutation == "stale":
        now_ms += 1000
    elif mutation == "run":
        observation = replace(observation, run_id="another-run")
    elif mutation == "unknown_holding":
        holding["status"] = "unknown"
    elif mutation == "foreign_proof":
        holding["evidence"]["run_id"] = "another-run"
    elif mutation == "open_gripper":
        observation = replace(observation, gripper_measured=1.0)
    elif mutation == "object_far":
        observation = replace(observation, object_xyz=(0.7, 0.0, 0.05))
    result = validate_handoff(
        request, observation, holding=holding, pending_chunks=pending, now_ms=now_ms
    )
    assert result["eligible"] is False
    assert result["physical_allowed"] is False


def test_handoff_clears_old_chunks_and_uses_new_epoch_observation():
    executor, request, observation, holding = handoff_inputs()
    result = validate_handoff(
        request,
        observation,
        holding=holding,
        pending_chunks=tuple(executor.pending_chunks),
        now_ms=executor.time_ms,
    )
    assert result["eligible"] is True
    assert result["physical_allowed"] is False
    assert observation.seq > request.after_seq
    assert observation.policy_epoch == request.previous_epoch + 1
    executor.execute_policy(
        result, Target("synthetic-object", "synthetic-tray", (0.4, 0.0, 0.05))
    )
    assert executor.calls[-2:] == ["fake_reset_policy", "reset"]
    run = run_scenario("normal_random", 0, "cleared")
    event = next(event for event in run["events"] if event["stage"] == "handoff")
    assert event["evidence"]["cleared_policy_chunks"] == 1


def test_schema_rejects_nonfinite_measurements_and_boolean_epoch():
    with pytest.raises(ValueError):
        Observation(
            "run",
            "synthetic-table-v1",
            True,
            1,
            1,
            (0.3, 0.0, 0.05),
            (0.3, 0.0, 0.05),
            0.2,
        )
    with pytest.raises(ValueError):
        Observation(
            "run",
            "synthetic-table-v1",
            1,
            1,
            1,
            (float("nan"), 0.0, 0.05),
            (0.3, 0.0, 0.05),
            0.2,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_run",
        "old_epoch",
        "old_seq",
        "old_time",
        "stale",
        "missing_object",
        "missing_gripper",
        "still_closed",
        "wrong_target",
    ],
)
def test_final_reset_requires_new_bound_placement_and_release_observation(mutation):
    executor, request, _, _ = handoff_inputs()
    target = Target("synthetic-object", "synthetic-tray", (0.4, 0.0, 0.05))
    handoff = {
        "eligible": True,
        "physical_allowed": False,
        "policy_epoch": executor.epoch,
    }
    executor.execute_policy(handoff, target)
    executor.execute(SkillProposal(Skill.RELEASE, target))
    released = executor.observe()
    final = executor.observe()
    now_ms = executor.time_ms
    final = {
        "wrong_run": lambda: replace(final, run_id="foreign-run"),
        "old_epoch": lambda: replace(final, policy_epoch=1),
        "old_seq": lambda: replace(final, seq=released.seq),
        "old_time": lambda: replace(final, captured_ms=released.captured_ms),
        "stale": lambda: replace(final, captured_ms=0),
        "missing_object": lambda: replace(final, object_xyz=None),
        "missing_gripper": lambda: replace(final, gripper_measured=None),
        "still_closed": lambda: replace(final, gripper_measured=0.2),
        "wrong_target": lambda: replace(final, object_xyz=(0.8, 0.0, 0.05)),
    }[mutation]()
    result = verify_final_reset(
        final,
        target,
        run_id=request.run_id,
        policy_epoch=executor.epoch,
        now_ms=now_ms,
        after_seq=released.seq,
        after_ms=released.captured_ms,
    )
    assert result["status"] != "pass"
