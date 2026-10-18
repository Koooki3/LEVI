"""event_pause_judge: a paused forward episode is sealed, then homed under the
closed admission gate (fake robot, in-process; no GPU, socket or real robot)."""

from types import SimpleNamespace

import pytest
from aeri_harness import config, fake, results, rig
from test_event_pause_judge import build

from levi.automatic import state_machine as sm
from levi.automatic.orchestrator import ConfigError, Orchestrator


def committed(r):
    return [e for e in r.orch.journal.events if e.record == "committed"]


def edges(r):
    return [(e.from_state, e.to_state) for e in committed(r)]


def notes(r, code):
    return [
        e.note.detail
        for e in r.orch.journal.events
        if e.record == "note" and e.note.code == code
    ]


def test_success_path_finalizes_seals_then_homes_with_one_goal_request(tmp_path):
    r = build(tmp_path)
    r.orch._final_judgement = lambda *a, **k: pytest.fail("no second goal request")
    order = []
    seal, home = r.recorder.seal, r.robot.home_under_closed_gate

    def record_seal(*a, **k):
        order.append("seal")
        return seal(*a, **k)

    def record_home(*, token):
        order.append(("home", r.robot.admission))
        return home(token=token)

    r.recorder.seal = record_seal
    r.robot.home_under_closed_gate = record_home
    assert r.orch.run() == "COMPLETED"
    # seal first, then the home with the gate shut for the policy
    assert order == ["seal", ("home", "closed")]
    found = edges(r)
    i = found.index(("FORWARD_JUDGING", "FORWARD_FINALIZE"))
    assert found[i : i + 3] == [
        ("FORWARD_JUDGING", "FORWARD_FINALIZE"),
        ("FORWARD_FINALIZE", "ROBOT_HOME"),
        ("ROBOT_HOME", "SCENE_ASSESS"),
    ]
    result = results(r.orch)[0]
    assert (result.task_outcome, result.goal_verification) == (
        "success",
        "verified",
    )
    assert (result.stop_reason, result.rollout.sealed) == ("goal_verified", "complete")
    assert result.robot_home == "not_attempted"  # written at the seal, before the home
    # initial scene + the one paused goal judgement; nothing else asked
    judges = [c for c in r.authority.calls if c[:2] == ("begin", "judge")]
    assert len(judges) == 2
    assert r.robot.home_calls == 1 and r.robot.gate_windows == 1
    assert r.robot.admission == "closed" and r.orch.fence.current is None
    assert r.orch.completed_forward == 1
    assert sm.check_journal(r.orch.journal.events) == []


def test_the_journal_says_the_arm_is_home(tmp_path):
    r = build(tmp_path)
    r.orch.run()
    detail = notes(r, "robot_homed")
    assert len(detail) == 1
    assert ".forward.0001" in detail[0]
    assert "sealed=complete" in detail[0] and "next=scene_assess" in detail[0]
    home = [
        e
        for e in committed(r)
        if (e.from_state, e.to_state) == ("ROBOT_HOME", "SCENE_ASSESS")
    ]
    assert [e.reason for e in home] == ["robot_home_reached"]


def test_horizon_without_a_goal_is_sealed_and_homed_too(tmp_path):
    r = build(
        tmp_path, max_steps=400, event_step=None, script=[{"decision": "rejected"}]
    )
    assert r.orch.run() == "COMPLETED"
    result = results(r.orch)[0]
    assert (result.stop_reason, result.task_outcome) == ("horizon_exhausted", "failure")
    assert result.goal_verification == "contradicted"
    assert r.robot.home_calls == 1 and r.robot.step_calls == 400


def test_a_scene_that_needs_a_reset_waits_for_a_person_after_the_home(tmp_path):
    from levi.automatic import metrics

    r = build(
        tmp_path,
        episodes=2,
        scene_script=[{"decision": "ready"}, {"decision": "reset_required"}],
    )
    assert r.orch.run() == "WAIT_HUMAN"
    found = edges(r)
    home = found.index(("ROBOT_HOME", "SCENE_ASSESS"))
    assert found[home + 1] == ("SCENE_ASSESS", "WAIT_HUMAN")
    assert committed(r)[-1].reason == "scene_reset_required"
    assert notes(r, "robot_homed")  # "homed, now waiting for the scene to be put back"
    assert r.robot.home_calls == 1 and r.robot.admission == "closed"
    # sealed forward counted; the wait is the planned scene check of human_assisted
    assert any(metrics._sealed_forward(e) for e in committed(r))
    kinds = [
        metrics.intervention_kind(e, "human_assisted")
        for e in committed(r)
        if e.to_state in ("WAIT_HUMAN", "FAULT_LOCKED")
    ]
    assert kinds == ["planned"]
    assert sm.check_journal(r.orch.journal.events) == []


@pytest.mark.parametrize("fault", ["home_miss", "no_reply"])
def test_a_failed_home_locks_keeps_the_sealed_result_and_never_retries(tmp_path, fault):
    r = build(tmp_path)
    r.robot.homes = fake.FaultScript({0: fault})
    assert r.orch.run() == "FAULT_LOCKED"
    assert r.orch.fault_reason == "home_failed"
    assert r.robot.home_calls == 1  # no retry
    result = results(r.orch)[0]
    assert (result.task_outcome, result.rollout.sealed) == ("success", "complete")
    assert notes(r, "home_failed_detail")
    assert r.robot.admission == "closed"
    assert sm.check_journal(r.orch.journal.events) == []


def test_a_stop_after_the_seal_stays_where_the_arm_is(tmp_path):
    r = build(tmp_path)
    seal = r.recorder.seal

    def seal_then_stop(*a, **k):
        out = seal(*a, **k)
        r.orch.stop("stop-after-seal")
        return out

    r.recorder.seal = seal_then_stop
    assert r.orch.run() == "WAIT_HUMAN"
    assert r.robot.home_calls == 0 and r.robot.gate_windows == 0
    last = committed(r)[-1]
    assert (last.from_state, last.to_state, last.reason) == (
        "ROBOT_HOME",
        "WAIT_HUMAN",
        "operator_stop",
    )
    result = results(r.orch)[0]
    assert (result.task_outcome, result.rollout.sealed) == ("success", "complete")
    assert sm.check_journal(r.orch.journal.events) == []


def test_a_stop_after_the_seal_ignores_home_after_operator_stop(tmp_path):
    r = build(tmp_path)
    assert r.orch.config.home_after_operator_stop is True
    seal = r.recorder.seal

    def seal_then_stop(*a, **k):
        out = seal(*a, **k)
        r.orch.stop("stop-x")
        return out

    r.recorder.seal = seal_then_stop
    r.orch.run()
    assert r.robot.home_calls == 0


def test_a_stop_between_the_checks_and_the_token_moves_nothing(tmp_path):
    r = build(tmp_path)
    safe = r.orch._safe_to_home

    def safe_then_stop():
        ok = safe()
        r.orch.stop("stop-before-token")
        return ok

    r.orch._safe_to_home = safe_then_stop
    assert r.orch.run() == "WAIT_HUMAN"  # not a home failure: the arm stays put
    assert r.robot.home_calls == 0 and r.robot.gate_windows == 0
    assert r.orch.fault_reason is None
    assert committed(r)[-1].reason == "operator_stop"
    assert sm.check_journal(r.orch.journal.events) == []


@pytest.mark.parametrize("case", ["failed", "proof_failed", "request_open"])
def test_a_failed_cycle_or_open_request_never_gets_a_home_token(tmp_path, case):
    r = build(tmp_path)
    safe = r.orch._safe_to_home

    def safe_then_break():
        ok = safe()
        if case == "failed":
            r.cycle.failed = True
        elif case == "proof_failed":
            r.cycle.proof_failed = True
        else:
            r.cycle._request = object()
        return ok

    r.orch._safe_to_home = safe_then_break
    assert r.orch.run() == "FAULT_LOCKED"
    assert r.orch.fault_reason == "home_failed"
    assert r.robot.home_calls == 0 and r.robot.motions[-1][0] == "policy_steps"
    assert r.orch.fence.current is None


def test_the_fake_gate_refuses_an_open_gate_like_the_bridge(tmp_path):
    r = build(tmp_path)
    r.orch.run()
    token = r.orch.fence.issue(
        sm.MotionToken("r", "tx", 1, "home", "r.forward.0009", 1, r.clock.now() + 10**9)
    )
    r.robot.reopen_admission()
    refused = r.robot.home_under_closed_gate(token=token)
    assert refused.executed == "no" and r.robot.gate_refused[-1] == "admission_open"
    r.robot.stop_admission()
    assert r.robot.home(token=token).executed == "no"  # the old home needs it open


def test_a_robot_without_the_gated_home_is_refused_up_front(tmp_path):
    r = build(tmp_path)
    r.orch.close()
    robot = r.robot

    class WithoutEntry:
        def __getattr__(self, name):
            if name == "home_under_closed_gate":
                raise AttributeError(name)
            return getattr(robot, name)

    parts = r.parts()
    parts["robot"] = WithoutEntry()
    with pytest.raises(ConfigError, match="home_under_closed_gate"):
        Orchestrator.restore(r.directory, r.config, intervention=r.cycle, **parts)
    parts["robot"] = SimpleNamespace()
    with pytest.raises(ConfigError, match="home_under_closed_gate"):
        Orchestrator.restore(r.directory, r.config, intervention=r.cycle, **parts)


def test_the_legacy_async_path_never_calls_the_gated_home(tmp_path):
    r = rig(tmp_path / "legacy", cfg=config(episodes=1))
    r.robot.home_under_closed_gate = lambda **k: pytest.fail("legacy uses home()")
    assert r.orch.run() in ("COMPLETED", "WAIT_HUMAN")
    assert r.robot.home_calls >= 1 and r.robot.gate_windows == 0


def test_a_crash_during_the_home_recovers_to_a_lock_without_a_second_result(tmp_path):
    r = build(tmp_path)
    r.robot.homes = fake.FaultScript({0: "crash_after_send"})
    with pytest.raises(fake.SimulatedCrash):
        r.orch.run()
    r.orch.close()
    new = Orchestrator.restore(r.directory, r.config, intervention=r.cycle, **r.parts())
    last = new.journal.events[-1]
    assert (last.to_state, last.reason) == ("FAULT_LOCKED", "recovery_ambiguous")
    # the result was committed at the seal and is not written again
    assert sum(e.episode_result is not None for e in new.journal.events) == 1
    sealed = next(e for e in new.journal.events if e.episode_result is not None)
    assert sealed.episode_result.rollout.sealed == "complete"
    assert sm.check_journal(new.journal.events) == []
    new.close()


def test_a_ready_scene_after_the_home_reopens_admission_for_the_next_episode(tmp_path):
    r = build(tmp_path, episodes=2)  # the fake scene says ready every time
    assert r.orch.run() == "COMPLETED"
    assert r.robot.step_calls == 16 and r.robot.home_calls == 2
    assert r.robot.gate_windows == 2 and r.robot.admission == "closed"
    # the second episode could only step because the resume hook reopened the gate
    resumes = [h for h in r.hooks_log if h[0] == "resume"]
    assert len(resumes) == 2
    assert [e.reason for e in committed(r) if e.to_state == "FORWARD_ACTIVE"] == [
        "scene_ready",
        "scene_ready",
    ]
    assert sm.check_journal(r.orch.journal.events) == []


def test_a_stop_after_the_token_but_before_any_post_stays_in_place(tmp_path):
    r = build(tmp_path)
    gated = r.robot.home_under_closed_gate

    def stopped_home(*, token):
        r.orch.stop("stop-after-token")
        return fake.HomeResult("no", "operator stop or signal", None)

    r.robot.home_under_closed_gate = stopped_home
    assert r.orch.run() == "WAIT_HUMAN"
    assert r.orch.fault_reason is None
    last = committed(r)[-1]
    assert (last.from_state, last.to_state, last.reason) == (
        "ROBOT_HOME",
        "WAIT_HUMAN",
        "operator_stop",
    )
    assert gated  # the real entry was never reached
    assert results(r.orch)[0].rollout.sealed == "complete"
    assert sm.check_journal(r.orch.journal.events) == []


def test_the_home_token_is_revoked_as_soon_as_the_home_returns(tmp_path):
    r = build(tmp_path, episodes=2)  # a scene check follows the home
    seen = {}
    gated, hold = r.robot.home_under_closed_gate, r.robot.hold

    def home(*, token):
        seen["token"] = token
        return gated(token=token)

    def hold_after_home():
        if "token" in seen and "current" not in seen:
            seen["current"] = r.orch.fence.current  # first hold after the home
        return hold()

    r.robot.home_under_closed_gate = home
    r.robot.hold = hold_after_home
    r.orch.run()
    assert seen["token"].kind == "home" and seen["current"] is None
    with pytest.raises(sm.MotionRefused) as exc:
        r.orch.fence.check(seen["token"], "home", r.clock.now())
    assert exc.value.code in ("revoked", "no_token")


def test_the_fake_robot_refuses_a_policy_step_on_a_closed_gate(tmp_path):
    r = build(tmp_path)
    token = r.orch.fence.issue(
        sm.MotionToken(
            "r", "tx", 1, "policy_steps", "r.forward.0009", 1, r.clock.now() + 10**9
        )
    )
    r.robot.stop_admission()
    before = r.robot.step_calls
    found = r.robot.step([0.0] * 7, token=token)
    assert found.executed == "no" and found.detail == "admission_closed"
    assert r.robot.refused[-1] == ("policy_steps", "admission_closed")
    assert r.robot.motions == []
    assert r.robot.step_calls == before + 1


def test_no_policy_step_gets_through_while_the_run_is_paused(tmp_path):
    r = build(tmp_path)
    submit = r.verifier.submit
    attempts = []

    def submit_and_try_a_step(request):
        token = r.orch.fence.issue(
            sm.MotionToken(
                "r",
                "tx-late",
                9,
                "policy_steps",
                request["episode_id"],
                1,
                r.clock.now() + 10**9,
            )
        )
        attempts.append(r.robot.step([0.0] * 7, token=token))
        return submit(request)

    r.verifier.submit = submit_and_try_a_step
    steps = 8
    r.orch.run()
    assert attempts and all(a.executed == "no" for a in attempts)
    assert r.robot.step_calls == steps + len(attempts)
    assert len(r.robot.motions) == steps + 1  # eight policy steps and the one home


def _count_gate(r, per_window=(4, 1, 2)):
    """Give the fake robot a snapshot with running totals that grow per home."""
    from levi.automatic import fr3_bridge as B

    totals = [0, 0, 0]
    gated = r.robot.home_under_closed_gate

    def home(*, token):
        found = gated(token=token)
        for i, n in enumerate(per_window):
            totals[i] += n
        return found

    r.robot.home_under_closed_gate = home
    r.robot.admission_snapshot = lambda: B.AdmissionSnapshot(
        True, 5, 77, 88, "d", 3, 1, 0, 0, "closed", 1234, *totals
    )


def test_robot_homed_note_carries_this_windows_gate_facts(tmp_path):
    r = build(tmp_path, episodes=2)
    _count_gate(r)
    r.orch.run()
    first, second = notes(r, "robot_homed")
    for detail in (first, second):  # totals are 4/8: the note shows the window
        for text in (
            "gate=closed",
            "home_only_from_ns=1234",
            "home_pose_released=4",
            "home_gripper_released=1",
            "home_gate_rejected=2",
        ):
            assert text in detail, detail
        assert "/" not in detail


def test_robot_homed_note_says_unreadable_when_the_snapshot_fails(tmp_path):
    r = build(tmp_path)
    _count_gate(r)
    good, calls = r.robot.admission_snapshot, []

    def snapshot():
        calls.append(1)
        if len(calls) > 1:  # readable before the home, broken after it
            raise RuntimeError("no snapshot")
        return good()

    r.robot.admission_snapshot = snapshot
    r.orch.run()
    (detail,) = notes(r, "robot_homed")
    assert "gate=unreadable" in detail and detail.endswith("next=scene_assess")


def test_robot_homed_note_without_a_snapshot_keeps_its_old_wording(tmp_path):
    r = build(tmp_path)
    assert not hasattr(r.robot, "admission_snapshot")
    r.orch.run()
    (detail,) = notes(r, "robot_homed")
    assert detail.endswith("gate=closed next=scene_assess")
    assert "home_only_from_ns" not in detail


def test_after_the_home_a_reset_episode_runs_and_waits_for_a_person(tmp_path):
    r = build(
        tmp_path,
        episodes=2,
        strategy="single_reset_policy",
        scene_script=[{"decision": "ready"}, {"decision": "reset_required"}],
    )
    assert r.orch.run() == "WAIT_HUMAN"
    found = edges(r)
    home = found.index(("ROBOT_HOME", "SCENE_ASSESS"))
    assert found[home : home + 4] == [
        ("ROBOT_HOME", "SCENE_ASSESS"),
        ("SCENE_ASSESS", "RESET_ACTIVE"),
        ("RESET_ACTIVE", "RESET_JUDGING"),
        ("RESET_JUDGING", "WAIT_HUMAN"),
    ]
    assert r.robot.home_calls == 1  # the reset episode is not homed
    assert sm.check_journal(r.orch.journal.events) == []


@pytest.mark.parametrize(
    "kw",
    [
        {},  # confirmed
        {"max_steps": 400, "event_step": None, "script": [{"decision": "rejected"}]},
    ],
    ids=["confirmed", "contradicted"],
)
def test_robot_homed_note_does_not_reveal_the_automatic_verdict(tmp_path, kw):
    r = build(tmp_path, **kw)
    r.orch.run()
    (detail,) = notes(r, "robot_homed")
    for word in (
        "verification",
        "verified",
        "contradicted",
        "undecided",
        "unavailable",
        "success",
        "failure",
        "goal_verified",
    ):
        assert word not in detail, detail
    assert "sealed=complete" in detail and detail.endswith("next=scene_assess")
    assert results(r.orch)[0].goal_verification in ("verified", "contradicted")
    # the transition reason says nothing about the outcome either
    home = [e for e in committed(r) if e.to_state == "SCENE_ASSESS"]
    assert [e.reason for e in home] == ["robot_home_reached"]
