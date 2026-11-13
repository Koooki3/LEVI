"""Offline flow assertions use synthetic inputs and never open a socket."""

import json
from dataclasses import replace

import pytest

from levi.automatic.offline_harness import (
    MAX_REGRASP_ATTEMPTS,
    FakeSkillExecutor,
    Skill,
    SkillProposal,
    Target,
    run_scenario,
    scenario_catalog,
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
