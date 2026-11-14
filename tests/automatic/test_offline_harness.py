"""Offline flow assertions use synthetic inputs and never open a socket."""

import json
from dataclasses import replace

import pytest

from levi.automatic.offline_harness import (
    MAX_OBSERVATION_AGE_MS,
    MAX_REGRASP_ATTEMPTS,
    POLICY_ID,
    FakeSkillExecutor,
    HandoffRequest,
    ScriptedPlanner,
    Skill,
    SkillProposal,
    Target,
    run_scenario,
    scenario_catalog,
    validate_handoff,
    verify_holding,
)


def test_seeded_success_and_snapshots_are_offline_and_json_safe():
    snapshots = []
    result = run_scenario(
        "normal_random", 17, "offline-test", on_event=snapshots.append
    )
    assert result["status"] == "succeeded"
    assert result["execution_mode"] == "offline"
    assert result["adapter"] == "fake"
    assert result["physical_execution"] is False
    assert result["handoff"]["eligible"] is True
    assert result["handoff"]["physical_allowed"] is False
    assert result["holding"]["evidence"]["object_follow"] is True
    assert result["holding"]["evidence"]["gripper_measurement"] == 0.2
    final = result["final_observation"]
    assert final["status"] == "pass"
    assert final["evidence"]["released"] is True
    assert final["evidence"]["object_at_target"] is True
    assert final["evidence"]["measured_object_xyz"] == result["target"]["position_xyz"]
    assert final["evidence"]["gripper_measurement"] == 1.0
    assert [event["stage"] for event in result["events"]] == [
        "observe",
        "plan",
        "open_gripper",
        "approach",
        "regrasp",
        "lift_verify",
        "handoff",
        "policy",
        "release",
        "final_verify",
        "complete",
    ]
    policy = next(event for event in result["events"] if event["stage"] == "policy")
    assert policy["evidence"]["policy_id"] == "synthetic-reset-policy-v1"
    assert policy["evidence"]["calls"] == [
        "open",
        "approach",
        "regrasp",
        "lift",
        "fake_reset_policy",
        "reset",
    ]
    assert (
        result["events"][0]["evidence"]["previous_task_terminal_object_xyz"]
        != result["target"]["position_xyz"]
    )
    assert result["usage"] == {
        "llm_calls": 0,
        "input_tokens": None,
        "output_tokens": None,
        "token_source": "unavailable",
        "provider": "scripted",
    }
    assert len(snapshots[0]["events"]) == 1
    assert snapshots[-1] == result
    assert [event["seq"] for event in result["events"]] == list(
        range(1, result["stage_seq"] + 1)
    )
    assert result["target"] == run_scenario("normal_random", 17, "other-run")["target"]
    assert result["target"] != run_scenario("normal_random", 18, "other-run")["target"]
    assert json.loads(json.dumps(result, allow_nan=False)) == result


def test_first_empty_grasp_is_retried_only_after_failed_lift_evidence():
    result = run_scenario("empty_grasp_retry", 1, "retry")
    assert result["status"] == "succeeded"
    checks = [event for event in result["events"] if event["stage"] == "lift_verify"]
    assert [event["evidence"]["holding"]["status"] for event in checks] == [
        "fail",
        "pass",
    ]
    assert checks[0]["evidence"]["holding"]["evidence"]["object_follow"] is False
    assert len(checks) <= MAX_REGRASP_ATTEMPTS


@pytest.mark.parametrize(
    ("scenario", "status", "reason"),
    [
        ("held_unknown", "blocked", "holding_measurement_missing"),
        ("stale_obs", "blocked", "observation_stale"),
        ("handoff_mismatch", "blocked", "task_policy_mismatch"),
        ("unreachable", "blocked", "synthetic_target_unreachable"),
        ("stop", "stopped", "scenario_stop_before_action"),
    ],
)
def test_refusal_scenarios_never_reach_reset_policy(
    scenario, status, reason, monkeypatch
):
    calls = []
    original = FakeSkillExecutor.execute

    def tracked(executor, proposal):
        calls.append(proposal.skill)
        return original(executor, proposal)

    monkeypatch.setattr(FakeSkillExecutor, "execute", tracked)
    result = run_scenario(scenario, 3, "refused")
    assert result["status"] == status
    assert result["events"][-1]["reason"] == reason
    assert not any(event["stage"] == "policy" for event in result["events"])
    assert result["handoff"]["eligible"] is False
    assert result["handoff"]["physical_allowed"] is False
    assert Skill.RESET not in calls
    assert Skill.RELEASE not in calls
    assert result["final_observation"]["status"] == "unknown"


@pytest.mark.parametrize(
    "stop_after",
    [
        "observe",
        "plan",
        "open_gripper",
        "approach",
        "regrasp",
        "lift_verify",
        "handoff",
        "policy",
        "release",
        "final_verify",
    ],
)
def test_stop_between_phases_revokes_handoff(stop_after):
    stop = {"requested": False}

    def emitted(snapshot):
        if snapshot["stage"] == stop_after:
            stop["requested"] = True

    result = run_scenario(
        "normal_random",
        1,
        "stopped",
        on_event=emitted,
        should_stop=lambda: stop["requested"],
    )
    assert result["status"] == "stopped"
    assert result["handoff"]["eligible"] is False
    assert result["events"][-1]["reason"] == "operator_stop"
    if stop_after in ("observe", "plan", "regrasp", "lift_verify", "handoff"):
        assert not any(
            event["reason"] == "fake_reset_policy_placed" for event in result["events"]
        )
    if stop_after == "policy":
        assert not any(
            event["reason"] == "synthetic_release_measured"
            for event in result["events"]
        )


@pytest.mark.parametrize(
    "skill", [Skill.OPEN, Skill.APPROACH, Skill.REGRASP, Skill.LIFT, Skill.RELEASE]
)
def test_stop_during_proposal_prevents_the_prepared_skill(monkeypatch, skill):
    stop = {"requested": False}
    calls = []
    propose = ScriptedPlanner.propose
    execute = FakeSkillExecutor.execute

    def request_stop(planner, proposed, target):
        proposal = propose(planner, proposed, target)
        if proposed == skill:
            stop["requested"] = True
        return proposal

    def track(executor, proposal):
        calls.append((proposal.skill, stop["requested"]))
        return execute(executor, proposal)

    monkeypatch.setattr(ScriptedPlanner, "propose", request_stop)
    monkeypatch.setattr(FakeSkillExecutor, "execute", track)
    result = run_scenario(
        "normal_random", 17, "stop-proposal", should_stop=lambda: stop["requested"]
    )
    assert stop["requested"]
    assert not any(requested for _, requested in calls)
    assert skill not in [called for called, _ in calls]
    assert result["status"] == "stopped"
    assert result["events"][-1]["reason"] == "operator_stop"
    assert result["handoff"]["eligible"] is False
    assert result["handoff"]["physical_allowed"] is False


def test_stop_during_fresh_observation_does_not_open_gripper(monkeypatch):
    state = {"requested": False, "observations": 0}
    calls = []
    observe = FakeSkillExecutor.observe
    execute = FakeSkillExecutor.execute

    def observe_then_stop(executor, **kwargs):
        result = observe(executor, **kwargs)
        state["observations"] += 1
        if state["observations"] == 2:
            state["requested"] = True
        return result

    def track(executor, proposal):
        calls.append(proposal.skill)
        return execute(executor, proposal)

    monkeypatch.setattr(FakeSkillExecutor, "observe", observe_then_stop)
    monkeypatch.setattr(FakeSkillExecutor, "execute", track)
    result = run_scenario(
        "normal_random", 17, "stop-observation", should_stop=lambda: state["requested"]
    )
    assert state["requested"] and calls == []
    assert result["status"] == "stopped"
    assert result["stage"] == "open_gripper"
    assert result["handoff"]["eligible"] is False


def test_regrasp_retry_limit_blocks_without_policy(monkeypatch):
    original = FakeSkillExecutor.execute

    def always_empty(executor, proposal):
        original(executor, proposal)
        if proposal.skill == Skill.REGRASP:
            executor.held = False
            executor.gripper_measured = 0.0

    monkeypatch.setattr(FakeSkillExecutor, "execute", always_empty)
    result = run_scenario("normal_random", 0, "retry-limit")
    assert result["status"] == "blocked"
    assert result["events"][-1]["reason"] == "regrasp_retry_limit"
    assert (
        sum(
            event["stage"] == "regrasp" and event["status"] == "running"
            for event in result["events"]
        )
        == MAX_REGRASP_ATTEMPTS
    )
    assert not any(event["stage"] == "policy" for event in result["events"])


def test_reset_ack_without_reaching_target_is_not_success(monkeypatch):
    original = FakeSkillExecutor.execute

    def wrong_reset(executor, proposal):
        original(executor, proposal)
        if proposal.skill == Skill.RESET:
            executor.object_xyz = (0.9, 0.0, 0.05)

    monkeypatch.setattr(FakeSkillExecutor, "execute", wrong_reset)
    result = run_scenario("normal_random", 0, "wrong-reset")
    assert result["status"] == "blocked"
    assert result["events"][-1]["reason"] == "synthetic_final_reset_not_verified"
    assert result["final_observation"]["evidence"]["object_at_target"] is False
    assert (
        result["final_observation"]["evidence"]["measured_object_xyz"]
        != result["target"]["position_xyz"]
    )


def test_release_ack_without_open_gripper_blocks_final_success(monkeypatch):
    original = FakeSkillExecutor.execute

    def no_release(executor, proposal):
        if proposal.skill != Skill.RELEASE:
            return original(executor, proposal)
        return None

    monkeypatch.setattr(FakeSkillExecutor, "execute", no_release)
    result = run_scenario("normal_random", 0, "no-release")
    assert result["status"] == "blocked"
    assert result["final_observation"]["evidence"]["released"] is False
    assert not any(event["stage"] == "complete" for event in result["events"])


def test_reset_placement_cannot_be_called_before_policy_handoff():
    executor = FakeSkillExecutor("no-pre-reset")
    target = Target("synthetic-object", "synthetic-tray", (0.4, 0.0, 0.05))
    with pytest.raises(ValueError, match="policy handoff"):
        executor.execute(SkillProposal(Skill.RESET, target))
    assert executor.object_xyz == (0.3, 0.0, 0.05)


@pytest.mark.parametrize(
    "failure,stage", [("open", "open_gripper"), ("approach", "approach")]
)
def test_command_ack_is_not_enough_to_close(failure, stage, monkeypatch):
    original = FakeSkillExecutor.execute
    calls = []

    def ignored(executor, proposal):
        calls.append(proposal.skill.value)
        if proposal.skill.value == failure:
            return None
        return original(executor, proposal)

    monkeypatch.setattr(FakeSkillExecutor, "execute", ignored)
    result = run_scenario("normal_random", 17, "ignored-command")
    assert result["status"] == "blocked"
    assert result["stage"] == stage
    assert "regrasp" not in calls
    assert result["handoff"]["eligible"] is False


def test_regrasp_cannot_teleport_or_close_before_arrival():
    executor = FakeSkillExecutor("early-close")
    target = Target("synthetic-object", "synthetic-tray", (0.4, 0.0, 0.05))
    executor.gripper_xyz = (0.3, 0.0, 0.15)
    with pytest.raises(ValueError, match="at the grasp pose"):
        executor.execute(SkillProposal(Skill.REGRASP, target))
    assert executor.held is False
    assert executor.gripper_measured == 1.0


@pytest.mark.parametrize("aperture", [0.31, 0.5, 0.89])
def test_partial_closure_blocks_before_lift(aperture, monkeypatch):
    original = FakeSkillExecutor.execute
    calls = []

    def partial_close(executor, proposal):
        calls.append(proposal.skill)
        original(executor, proposal)
        if proposal.skill == Skill.REGRASP:
            executor.gripper_measured = aperture

    monkeypatch.setattr(FakeSkillExecutor, "execute", partial_close)
    result = run_scenario("normal_random", 17, "partial-close")
    assert result["status"] == "blocked"
    assert result["stage"] == "regrasp"
    assert result["events"][-1]["reason"] == "waiting_measured_close"
    assert Skill.LIFT not in calls and Skill.RESET not in calls
    assert result["handoff"]["eligible"] is False
    assert result["handoff"]["physical_allowed"] is False


@pytest.mark.parametrize("aperture", [0.0, 0.3])
def test_confirmed_closure_boundary_can_complete_synthetic_flow(aperture, monkeypatch):
    original = FakeSkillExecutor.execute

    def measured_close(executor, proposal):
        original(executor, proposal)
        if proposal.skill == Skill.REGRASP:
            executor.gripper_measured = aperture

    monkeypatch.setattr(FakeSkillExecutor, "execute", measured_close)
    result = run_scenario("normal_random", 17, "closed-boundary")
    assert result["status"] == "succeeded"
    assert result["holding"]["evidence"]["gripper_closed"] is True
    assert result["handoff"]["physical_allowed"] is False


def fresh_holding_handoff():
    executor = FakeSkillExecutor("proof-time")
    target = Target("synthetic-object", "synthetic-tray", (0.3, 0.0, 0.05))
    executor.execute(SkillProposal(Skill.REGRASP, target))
    before = executor.observe()
    executor.execute(SkillProposal(Skill.LIFT, target))
    after = executor.observe()
    holding = verify_holding(before, after, now_ms=executor.time_ms)
    executor.clear_policy_chunks()
    executor.epoch += 1
    observation = executor.observe()
    request = HandoffRequest(
        executor.run_id, POLICY_ID, 1, 2, after.seq, after.captured_ms
    )
    return request, observation, holding


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_before_time",
        "missing_after_time",
        "boolean_before_time",
        "boolean_after_time",
        "float_after_time",
        "before_after_time_order",
        "before_after_seq_order",
        "request_time_mismatch",
        "future_proof",
    ],
)
def test_handoff_requires_bound_integer_ordered_proof_times(mutation):
    request, observation, holding = fresh_holding_handoff()
    proof = holding["evidence"]
    if mutation == "missing_before_time":
        proof.pop("before_captured_ms")
    elif mutation == "missing_after_time":
        proof.pop("after_captured_ms")
    elif mutation == "boolean_before_time":
        proof["before_captured_ms"] = True
    elif mutation == "boolean_after_time":
        proof["after_captured_ms"] = True
    elif mutation == "float_after_time":
        proof["after_captured_ms"] = float(proof["after_captured_ms"])
    elif mutation == "before_after_time_order":
        proof["before_captured_ms"] = proof["after_captured_ms"]
    elif mutation == "before_after_seq_order":
        proof["before_seq"] = proof["after_seq"]
    elif mutation == "request_time_mismatch":
        request = replace(request, after_ms=request.after_ms - 1)
    elif mutation == "future_proof":
        proof["after_captured_ms"] = observation.captured_ms + 1
        request = replace(request, after_ms=proof["after_captured_ms"])
    result = validate_handoff(
        request,
        observation,
        holding=holding,
        pending_chunks=(),
        now_ms=observation.captured_ms,
    )
    assert result["eligible"] is False
    assert result["physical_allowed"] is False


@pytest.mark.parametrize("stale_endpoint", ["before", "after"])
def test_fresh_handoff_observation_cannot_refresh_old_holding_proof(stale_endpoint):
    request, observation, holding = fresh_holding_handoff()
    proof = holding["evidence"]
    now_ms = proof[f"{stale_endpoint}_captured_ms"] + MAX_OBSERVATION_AGE_MS + 1
    observation = replace(observation, captured_ms=now_ms)
    result = validate_handoff(
        request, observation, holding=holding, pending_chunks=(), now_ms=now_ms
    )
    assert result["eligible"] is False
    assert result["reason"] == "holding_evidence_stale"
    assert result["physical_allowed"] is False


def test_holding_proof_freshness_accepts_inclusive_age_boundary():
    request, observation, holding = fresh_holding_handoff()
    proof = holding["evidence"]
    assert proof["before_captured_ms"] < proof["after_captured_ms"] == request.after_ms
    now_ms = proof["before_captured_ms"] + MAX_OBSERVATION_AGE_MS
    observation = replace(observation, captured_ms=now_ms)
    result = validate_handoff(
        request, observation, holding=holding, pending_chunks=(), now_ms=now_ms
    )
    assert result["eligible"] is True
    assert result["physical_allowed"] is False


@pytest.mark.parametrize("identity", ["run_id", "policy_epoch"])
def test_foreign_preopen_observation_cannot_be_relabelled_as_current(
    identity, monkeypatch
):
    original = FakeSkillExecutor.observe
    calls = []
    execute = FakeSkillExecutor.execute

    def foreign(executor, **kwargs):
        observation = original(executor, **kwargs)
        if observation.seq == 2:
            return replace(
                observation, **{identity: "foreign-run" if identity == "run_id" else 7}
            )
        return observation

    def tracked(executor, proposal):
        calls.append(proposal.skill)
        return execute(executor, proposal)

    monkeypatch.setattr(FakeSkillExecutor, "observe", foreign)
    monkeypatch.setattr(FakeSkillExecutor, "execute", tracked)
    result = run_scenario("normal_random", 17, "current-run")
    assert result["status"] == "blocked"
    assert result["stage"] == "open_gripper"
    assert calls == []


def test_retry_preserves_previous_attempt_observation_boundary(monkeypatch):
    original = FakeSkillExecutor.observe

    def repeat_old(executor, **kwargs):
        observation = original(executor, **kwargs)
        if observation.seq == 7:
            return replace(observation, seq=2, captured_ms=1010)
        return observation

    monkeypatch.setattr(FakeSkillExecutor, "observe", repeat_old)
    result = run_scenario("empty_grasp_retry", 17, "retry-old")
    assert result["status"] == "blocked"
    assert result["events"][-1]["reason"] == "observation_not_new"
    assert sum(event["stage"] == "regrasp" for event in result["events"]) == 1


@pytest.mark.parametrize("seed", [True, -1, 2**32, 1.5])
def test_invalid_seed_is_refused(seed):
    with pytest.raises(ValueError):
        run_scenario("normal_random", seed, "invalid")


def test_closed_skill_target_and_scenario_inputs():
    with pytest.raises(TypeError):
        SkillProposal(
            "shell", Target("synthetic-object", "synthetic-tray", (0.3, 0.0, 0.05))
        )
    with pytest.raises(ValueError):
        Target("synthetic-object", "synthetic-tray", (100.0, 0.0, 0.05))
    with pytest.raises(ValueError):
        run_scenario("real_robot", 0, "invalid")
    with pytest.raises(ValueError):
        run_scenario("normal_random", 0, "../product")
    assert len(scenario_catalog()) == 7
    assert all(set(item) == {"id", "label_key"} for item in scenario_catalog())
