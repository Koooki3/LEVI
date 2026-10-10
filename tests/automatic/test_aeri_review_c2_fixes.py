"""Regression tests for the second review of the AERI orchestrator
(levi2/recon/review-C2-fixes.md, N1-N5, N7): no home after a stop when
the episode ran no step or staying put is configured, also on the reset
path and for late stops; stop() never waits for an adapter; the final
judgement must cover the end of the episode; a stop with a used command id
is refused; holds before notes, important notes beyond the budget; an
episode cut short by a crash gets its result on recovery."""

import threading
import time

import pytest
from aeri_harness import build, committed, config, crash_at, fake, restart, results

from levi.automatic import state_machine as sm
from levi.automatic.adapters import events as ev


def one_episode(**over):
    return config(episodes=1, **over)


def reset_first(clock, *more):
    return ev.FakeSceneAssessor(
        [{"decision": "reset_required"}, *more],
        clock,
        "r-sm",
        default={"decision": "ready"},
    )


def stop_when(r, part, name, *, at=0):
    obj = getattr(r, part)
    original = getattr(obj, name)
    seen = {"n": 0}

    def wrapper(*args, **kwargs):
        if seen["n"] == at:
            r.orch.stop("stop-1")
        seen["n"] += 1
        return original(*args, **kwargs)

    setattr(obj, name, wrapper)


def reset_steps_stop(r, at):
    original = r.robot.step

    def step(action, *, token):
        if ".reset." in token.episode_id and r.robot.step_calls == at:
            r.orch.stop("stop-1")
        return original(action, token=token)

    r.robot.step = step


# --- N1: no home after a stop on the reset path, nor after a late stop ---------------------------


def test_a_reset_stopped_before_its_first_step_is_not_homed(tmp_path):
    clock = fake.FakeClock()
    r = build(tmp_path, cfg=one_episode(), clock=clock, scene=reset_first(clock))
    stop_when(r, "events", "open")  # the reset episode opens, the loop stops at step 0
    assert r.orch.run() == "WAIT_HUMAN"
    assert r.robot.home_calls == 0 and r.robot.step_calls == 0
    assert committed(r.orch)[-1] == ("RESET_FINALIZE", "WAIT_HUMAN", "operator_stop")
    (result,) = results(r.orch)
    assert result.robot_home == "not_attempted" and result.task_outcome == "unknown"
    assert sm.check_journal(r.orch.journal.events) == []


def test_a_reset_stopped_mid_way_stays_put_when_configured(tmp_path):
    clock = fake.FakeClock()
    r = build(
        tmp_path,
        cfg=one_episode(home_after_operator_stop=False),
        clock=clock,
        scene=reset_first(clock),
    )
    reset_steps_stop(r, 3)
    assert r.orch.run() == "WAIT_HUMAN"
    assert r.robot.home_calls == 0
    assert committed(r.orch)[-1] == ("RESET_FINALIZE", "WAIT_HUMAN", "operator_stop")
    (result,) = results(r.orch)
    assert result.robot_home == "not_attempted" and result.rollout.sealed == "complete"


def test_by_default_a_reset_stopped_mid_way_is_still_homed(tmp_path):
    clock = fake.FakeClock()
    r = build(tmp_path, cfg=one_episode(), clock=clock, scene=reset_first(clock))
    reset_steps_stop(r, 3)
    assert r.orch.run() == "WAIT_HUMAN"
    assert r.robot.home_calls == 1
    assert committed(r.orch)[-1] == ("RESET_FINALIZE", "WAIT_HUMAN", "operator_stop")


def test_a_late_stop_after_the_forward_horizon_stays_put_when_configured(tmp_path):
    clock = fake.FakeClock()
    verifier = ev.FakeGoalVerifier([], clock, "r-sm", default={"decision": "unknown"})
    r = build(
        tmp_path,
        cfg=one_episode(home_after_operator_stop=False),
        clock=clock,
        verifier=verifier,
    )
    original = verifier.submit

    def submit(request):
        if request["request_id"].endswith(":final"):
            r.orch.stop("stop-late")  # during the final judgement
        return original(request)

    verifier.submit = submit
    assert r.orch.run() == "WAIT_HUMAN"
    assert r.robot.home_calls == 0
    last = r.orch.journal.events[-1]
    assert (last.from_state, last.to_state, last.reason) == (
        "ROBOT_HOME",
        "WAIT_HUMAN",
        "operator_stop",
    )
    assert last.authority.command_id == "stop-late"
    (result,) = results(r.orch)
    assert result.stop_reason == "horizon_exhausted"


def test_a_late_stop_during_the_reset_quiesce_stays_put_when_configured(tmp_path):
    clock = fake.FakeClock()
    r = build(
        tmp_path,
        cfg=one_episode(home_after_operator_stop=False),
        clock=clock,
        scene=reset_first(clock),
    )
    stop_when(r, "policy", "quiesce")
    assert r.orch.run() == "WAIT_HUMAN"
    assert r.robot.home_calls == 0
    assert committed(r.orch)[-1] == ("RESET_FINALIZE", "WAIT_HUMAN", "operator_stop")


# --- N2: stop() never waits for an adapter ------------------------------------------------------


@pytest.mark.parametrize(
    "part, name", [("robot", "home"), ("policy", "quiesce"), ("policy", "acquire")]
)
def test_stop_returns_at_once_while_an_adapter_is_slow(tmp_path, part, name):
    r = build(tmp_path, cfg=one_episode())
    entered, release = threading.Event(), threading.Event()
    obj = getattr(r, part)
    original = getattr(obj, name)

    def slow(*args, **kwargs):
        entered.set()
        release.wait(5)  # blocked until the test lets go
        return original(*args, **kwargs)

    setattr(obj, name, slow)
    runner = threading.Thread(target=r.orch.run)
    runner.start()
    assert entered.wait(10)
    began = time.monotonic()
    found = r.orch.stop("stop-1")
    took = time.monotonic() - began
    assert took < 0.2, took
    assert found.ok and found.code == "stop_requested"
    assert r.orch.stop("stop-1").code == "stop_requested"  # repeated, still pending
    release.set()
    runner.join(10)
    assert not runner.is_alive()
    assert r.orch.state == "WAIT_HUMAN"
    assert sm.check_journal(r.orch.journal.events) == []


def test_stop_in_wait_human_still_ends_the_run(tmp_path):
    r = build(tmp_path, cfg=one_episode(), policy={"acquire_fails": True})
    assert r.orch.run() == "WAIT_HUMAN"
    assert r.orch.stop("stop-1").code == "completed"
    assert r.orch.state == "COMPLETED"


# --- N3: the final judgement must cover the end of the episode ----------------------------------


@pytest.mark.parametrize(
    "bad",
    [{"observed_through_step": 0}, {"observed_before_ns": 500_000_000}],
)
@pytest.mark.parametrize("in_loop", ["confirmed", "unknown"])
def test_a_final_judgement_not_covering_the_end_is_never_a_success(
    tmp_path, bad, in_loop
):
    clock = fake.FakeClock()
    verifier = ev.FakeGoalVerifier(
        lambda request, n: (
            {"decision": "confirmed", **bad}
            if request["request_id"].endswith(":final")
            else {"decision": in_loop}
        ),
        clock,
        "r-sm",
    )
    r = build(tmp_path, cfg=one_episode(), clock=clock, verifier=verifier)
    r.orch.run()
    (result,) = results(r.orch)
    assert result.task_outcome == "unknown"
    assert result.goal_verification == "unavailable"
    assert "judgement_dropped_stale" in r.orch.note_counts


# --- N4: a stop with a command id already used --------------------------------------------------


def test_a_stop_with_a_command_id_a_resume_used_is_refused(tmp_path):
    r = build(tmp_path, cfg=one_episode(), policy={"acquire_fails": True})
    assert r.orch.run() == "WAIT_HUMAN"
    r.policy.acquire_fails = False
    assert r.orch.resume(
        "cmd-7",
        expected_seq=r.orch.journal.next_seq,
        environment_handled=True,
        health_rechecked=True,
    ).ok
    original = r.robot.step
    seen = {}

    def step(action, *, token):
        if r.robot.step_calls == 3:
            seen["reused"] = r.orch.stop("cmd-7")
            seen["fresh"] = r.orch.stop("cmd-8")
        return original(action, token=token)

    r.robot.step = step
    assert r.orch.run() == "WAIT_HUMAN"
    assert not seen["reused"].ok and seen["reused"].code == "command_used"
    assert seen["fresh"].ok
    (result,) = results(r.orch)
    assert result.stop_reason == "operator_stop"
    assert r.robot.step_calls < r.config.forward_max_steps


def test_a_stop_repeated_after_it_took_effect_is_repeated(tmp_path):
    r = build(tmp_path, cfg=one_episode(), policy={"acquire_fails": True})
    r.orch.run()
    assert r.orch.stop("s-1").code == "completed"
    again = r.orch.stop("s-1")
    assert again.ok and again.repeated


# --- N5: holds before notes; important notes beyond the budget ----------------------------------


def test_the_safety_hold_comes_before_the_backlog_of_notes(tmp_path):
    clock = fake.FakeClock()
    noise = [
        {
            "step": s,
            "event_type": "gripper_open",
            "id": "ev-same",
            "fault": "duplicate" if s else None,
        }
        for s in range(6)
    ]
    r = build(
        tmp_path,
        cfg=one_episode(),
        clock=clock,
        events=ev.FakeEventStream({"forward": noise}, clock, "r-sm"),
        robot={"step_faults": {5: "red_light"}},
    )
    order = []
    hold, note = r.robot.hold, r.orch.journal.note

    def spy_hold():
        order.append("hold")
        return hold()

    def spy_note(*args, **kwargs):
        order.append("note")
        return note(*args, **kwargs)

    r.robot.hold, r.orch.journal.note = spy_hold, spy_note
    assert r.orch.run() == "FAULT_LOCKED"
    assert "note" in order and order.index("hold") < order.index("note")


def test_important_notes_are_kept_beyond_the_budget(tmp_path):
    clock = fake.FakeClock()
    noise = [
        {
            "step": s,
            "event_type": "gripper_open",
            "id": "ev-same",
            "fault": "duplicate" if s else None,
        }
        for s in range(10)
    ]
    r = build(
        tmp_path,
        cfg=one_episode(note_lines_per_episode=1),
        clock=clock,
        verifier=ev.FakeGoalVerifier(
            [], clock, "r-sm", default={"decision": "unknown"}
        ),
        events=ev.FakeEventStream({"forward": noise}, clock, "r-sm"),
        policy={"chunk_faults": {1: "wrong_epoch"}},
    )
    original = r.robot.observe
    calls = {"n": 0}

    def observe():
        calls["n"] += 1
        if calls["n"] == 12:
            raise RuntimeError("camera driver")
        return original()

    r.robot.observe = observe
    with pytest.raises(RuntimeError):
        r.orch.run()
    codes = [e.note.code for e in r.orch.journal.events if e.record == "note"]
    assert "orchestrator_exception" in codes
    assert "notes_suppressed" in codes


# --- N7: an episode cut short by a crash gets its result on recovery -----------------------------


def test_a_crash_inside_an_episode_records_it_as_unknown_on_recovery(tmp_path):
    r = build(
        tmp_path, cfg=one_episode(), robot={"step_faults": {6: "crash_after_send"}}
    )
    with pytest.raises(fake.SimulatedCrash):
        r.orch.run()
    new = restart(r)
    last = new.orch.journal.events[-1]
    assert (last.to_state, last.reason) == ("FAULT_LOCKED", "recovery_ambiguous")
    result = last.episode_result
    assert result is not None
    assert (result.task_outcome, result.stop_reason) == (
        "unknown",
        "orchestrator_crash",
    )
    assert (
        result.rollout.sealed == "incomplete" and result.robot_home == "not_attempted"
    )
    assert last.episode_id == "r-sm.forward.0001"
    assert sm.check_journal(new.orch.journal.events) == []


def test_a_crash_during_the_reset_home_records_the_home_as_failed(tmp_path):
    clock = fake.FakeClock()
    r = build(
        tmp_path,
        cfg=one_episode(),
        clock=clock,
        scene=reset_first(clock),
        robot={"home_faults": {0: "crash_after_send"}},
    )
    with pytest.raises(fake.SimulatedCrash):
        r.orch.run()
    new = restart(r)
    result = new.orch.journal.events[-1].episode_result
    assert (result.stop_reason, result.robot_home) == ("orchestrator_crash", "failed")
    assert result.rollout.sealed == "complete"


def test_a_crash_after_the_result_adds_no_second_result(tmp_path):
    r = build(
        tmp_path, cfg=one_episode(), crash_hook=crash_at("after_committed", 4)
    )  # FINALIZE -> ROBOT_HOME
    with pytest.raises(fake.SimulatedCrash):
        r.orch.run()
    assert r.orch.state == "ROBOT_HOME"
    new = restart(r)
    assert new.orch.journal.events[-1].episode_result is None
    assert len(results(new.orch)) == 1
