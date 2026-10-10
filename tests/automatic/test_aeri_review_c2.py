"""Regression tests for the independent review of the AERI orchestrator
(levi2/recon/review-C2.md, I-1..I-5) and the coordinator's decisions on
it: exceptions never leave motion authorised; a stop during the scene
assessment never opens an episode; commands from other threads are
serialised; audit notes are bounded and batched; an early stop is a
success only when the final judgement confirms it; an episode ended by a
fault keeps a result; staying put after an operator's stop is a setting."""

import threading

import pytest
from aeri_harness import build, committed, config, fake, restart, results

from levi.automatic import state_machine as sm
from levi.automatic.adapters import events as ev
from levi.automatic.orchestrator import OrchestratorHalted, RunConfig

ACTION = [0.45, 0.0, 0.4, 0.0, 1.0, 0.0, 0.0]


def one_episode(**over) -> RunConfig:
    return config(episodes=1, **over)


def unknown_judge(clock):
    return ev.FakeGoalVerifier([], clock, "r-sm", default={"decision": "unknown"})


def fail_on(obj, name, call, exc=RuntimeError):
    """Make ``obj.name`` raise on its ``call``-th call (0-based)."""
    original = getattr(obj, name)
    seen = {"n": 0}

    def wrapper(*args, **kwargs):
        number = seen["n"]
        seen["n"] += 1
        if number == call:
            raise exc(f"injected into {name}")
        return original(*args, **kwargs)

    setattr(obj, name, wrapper)


def keep_token(r):
    """Remember the first policy-steps token the robot is given."""
    seen = {}
    original = r.robot.step

    def step(action, *, token):
        seen.setdefault("token", token)
        return original(action, token=token)

    r.robot.step = step
    return seen


# --- I-1: no exception leaves motion authorised -----------------------------------------------


SITES = [
    ("robot", "observe", 4),
    ("robot", "step", 3),
    ("robot", "hold", 0),
    ("robot", "home", 0),
    ("policy", "quiesce", 0),
    ("policy", "collect", 1),
    ("events", "poll", 2),
    ("verifier", "submit", 0),
    ("verifier", "collect", 0),
    ("recorder", "stage", 3),
]


@pytest.mark.parametrize("part, name, call", SITES)
def test_an_adapter_exception_revokes_the_token_holds_and_locks(
    tmp_path, part, name, call
):
    clock = fake.FakeClock()
    r = build(tmp_path, cfg=one_episode(), clock=clock, verifier=unknown_judge(clock))
    seen = keep_token(r)
    fail_on(getattr(r, part), name, call)
    try:
        final = r.orch.run()
    except RuntimeError:
        final = r.orch.state
    assert final == "FAULT_LOCKED"
    assert r.fence.current is None
    assert r.orch.journal.open_transaction is None
    motions = len(r.robot.motions)
    if "token" in seen:
        late = r.robot.step(ACTION, token=seen["token"])
        assert late.executed == "no" and late.detail == "token_refused"
    assert len(r.robot.motions) == motions
    assert sm.check_journal(r.orch.journal.events) == []
    assert r.orch.run() == "FAULT_LOCKED" and len(r.robot.motions) == motions


def test_an_exception_from_the_journal_halts_in_memory(tmp_path):
    clock = fake.FakeClock()
    flood = [
        {"step": s, "event_type": "gripper_open", "fault": "control_key"}
        for s in range(2, 4)
    ]
    r = build(
        tmp_path,
        cfg=one_episode(),
        clock=clock,
        verifier=unknown_judge(clock),
        events=ev.FakeEventStream({"forward": flood}, clock, "r-sm"),
    )
    seen = keep_token(r)

    def broken(*args, **kwargs):
        raise OSError("no space left on device")

    r.orch.journal.note = broken  # the next note flush fails, and so does the fault
    with pytest.raises(OSError):
        r.orch.run()
    assert r.orch.halted and r.orch.state == "FAULT_LOCKED"
    assert r.fence.current is None and r.robot.holds >= 1
    assert r.robot.step(ACTION, token=seen["token"]).detail == "token_refused"
    assert r.orch.run() == "FAULT_LOCKED"
    assert (
        r.orch.resume(
            "c-1",
            expected_seq=r.orch.journal.next_seq,
            environment_handled=True,
            health_rechecked=True,
        ).code
        == "halted"
    )
    with pytest.raises(OrchestratorHalted):
        r.orch._tx("FAULT_LOCKED", "watchdog_timeout")
    new = restart(r)
    assert new.orch.state == "FAULT_LOCKED"


def test_a_hold_that_raises_mid_transaction_closes_it_as_unknown(tmp_path):
    clock = fake.FakeClock()
    r = build(tmp_path, cfg=one_episode(), clock=clock, verifier=unknown_judge(clock))
    fail_on(r.robot, "hold", 0)
    with pytest.raises(RuntimeError):
        r.orch.run()
    events = r.orch.journal.events
    acks = [e for e in events if e.record == "acknowledged"]
    assert acks[-1].ack.executed == "unknown"
    last = [e for e in events if e.record == "committed"][-1]
    assert last.to_state == "FAULT_LOCKED"
    (result,) = results(r.orch)
    assert (result.task_outcome, result.stop_reason) == ("unknown", "watchdog_timeout")


# --- I-2: a stop before or at the start of an episode ---------------------------------------


def stop_inside(r, part, name):
    original = getattr(getattr(r, part), name)

    def wrapper(*args, **kwargs):
        r.orch.stop("stop-1")
        return original(*args, **kwargs)

    setattr(getattr(r, part), name, wrapper)


def assert_no_episode_counted(r):
    assert r.robot.step_calls == 0 and r.robot.home_calls == 0
    assert r.orch.completed_forward == 0
    for result in results(r.orch):
        assert result.task_outcome == "unknown" and result.rollout.sealed != "complete"
    assert not any(x.complete_marker for x in r.recorder.rollouts.values())
    assert sm.check_journal(r.orch.journal.events) == []


def test_a_stop_during_the_scene_assessment_opens_no_episode(tmp_path):
    r = build(tmp_path, cfg=one_episode())
    stop_inside(r, "scene", "collect")
    assert r.orch.run() == "WAIT_HUMAN"
    assert committed(r.orch)[-1] == ("VERIFY_INITIAL", "WAIT_HUMAN", "operator_stop")
    # Not even prepared: the stop is seen before the episode's transaction.
    assert not any(
        e.record == "prepared" and e.to_state == "FORWARD_ACTIVE"
        for e in r.orch.journal.events
    )
    assert r.recorder.rollouts == {} and r.policy.acquired == []
    assert_no_episode_counted(r)


def test_a_stop_while_the_episode_opens_aborts_it(tmp_path):
    r = build(tmp_path, cfg=one_episode())
    stop_inside(r, "policy", "acquire")  # inside the policy_steps transaction
    r.orch.run()
    # The stop came after the act's own check: the episode opened and the
    # loop stopped it at step 0.
    assert committed(r.orch)[-1] == ("ROBOT_HOME", "WAIT_HUMAN", "operator_stop")
    (result,) = results(r.orch)
    assert result.goal_verification == "unavailable"
    assert_no_episode_counted(r)


def test_a_stop_before_the_episode_s_act_never_opens_it(tmp_path):
    r = build(tmp_path, cfg=one_episode())
    original = r.orch.journal.prepare

    def prepare(to, *args, **kwargs):
        if to == "FORWARD_ACTIVE":
            r.orch.stop("stop-1")
        return original(to, *args, **kwargs)

    r.orch.journal.prepare = prepare
    assert r.orch.run() == "WAIT_HUMAN"
    aborted = [e for e in r.orch.journal.events if e.record == "aborted"]
    assert len(aborted) == 1
    assert committed(r.orch)[-1] == ("VERIFY_INITIAL", "WAIT_HUMAN", "operator_stop")
    assert r.recorder.rollouts == {}
    assert_no_episode_counted(r)


def test_a_zero_step_episode_is_discarded_not_homed_and_not_counted(tmp_path):
    r = build(tmp_path, cfg=one_episode())
    stop_inside(r, "events", "open")  # after the act's check, before step 0
    assert r.orch.run() == "WAIT_HUMAN"
    (rollout,) = r.recorder.rollouts.values()
    assert rollout.state == "incomplete" and rollout.staged == 0
    assert_no_episode_counted(r)


# --- I-3: commands from other threads are serialised -----------------------------------------


def wait_human(tmp_path, **over):
    r = build(tmp_path, cfg=one_episode(**over), policy={"acquire_fails": True})
    assert r.orch.run() == "WAIT_HUMAN"
    return r


def test_a_command_from_another_thread_cannot_slip_between_check_and_prepare(tmp_path):
    r = wait_human(tmp_path)
    outcome = {}

    def other():
        outcome["resume"] = r.orch.resume(
            "c-race",
            expected_seq=r.orch.journal.next_seq,
            environment_handled=True,
            health_rechecked=True,
        )

    def hook(point):
        if point == "before_prepared" and "thread" not in outcome:
            thread = threading.Thread(target=other)
            outcome["thread"] = thread
            thread.start()
            thread.join(timeout=0.3)
            outcome["blocked"] = thread.is_alive()

    r.orch.crash_hook = hook
    assert r.orch.stop("c-stop").code == "completed"
    outcome["thread"].join(timeout=5)
    assert outcome["blocked"]  # it waited for the lock
    assert not outcome["resume"].ok and outcome["resume"].code == "not_waiting"
    assert r.orch.state == "COMPLETED"
    assert sm.check_journal(r.orch.journal.events) == []


def test_stops_and_resumes_from_many_threads_keep_the_table(tmp_path):
    r = wait_human(tmp_path)
    errors = []
    done = threading.Event()

    def driver():
        try:
            for n in range(60):
                if r.orch.run() == "COMPLETED":
                    break
                r.orch.resume(
                    f"c-{n}",
                    expected_seq=r.orch.journal.next_seq,
                    environment_handled=True,
                    health_rechecked=True,
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            done.set()

    def stopper(k):
        n = 0
        while not done.is_set() and n < 400:
            try:
                r.orch.stop(f"s-{k}-{n}")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            n += 1

    threads = [threading.Thread(target=driver)] + [
        threading.Thread(target=stopper, args=(k,)) for k in range(3)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert errors == []
    assert sm.check_journal(r.orch.journal.events) == []
    assert r.orch.state in ("WAIT_HUMAN", "COMPLETED")


def test_a_command_id_used_by_a_stop_is_not_a_resume(tmp_path):
    r = wait_human(tmp_path)
    r.orch.stop("c-1")
    found = r.orch.resume(
        "c-1",
        expected_seq=r.orch.journal.next_seq,
        environment_handled=True,
        health_rechecked=True,
    )
    assert not found.ok and found.code == "command_used"


# --- I-5: bounded, batched notes; bad events count --------------------------------------------


def test_a_flood_of_bad_events_is_bounded_batched_and_waits_for_a_person(tmp_path):
    clock = fake.FakeClock()
    flood = [
        {"step": s, "event_type": "gripper_open", "fault": "control_key"}
        for s in range(40)
        for _ in range(16)
    ]
    r = build(
        tmp_path,
        cfg=one_episode(),
        clock=clock,
        verifier=unknown_judge(clock),
        events=ev.FakeEventStream({"forward": flood}, clock, "r-sm"),
    )
    written_at = []
    original = r.orch.journal.note

    def note(*args, **kwargs):
        written_at.append(r.robot.step_calls)
        return original(*args, **kwargs)

    r.orch.journal.note = note
    assert r.orch.run() == "WAIT_HUMAN"
    assert committed(r.orch)[-1][2] == "contract_violation_limit"
    lines = len(r.orch.journal.events)
    assert lines < 60, lines
    size = (r.directory / "state_journal.jsonl").stat().st_size
    assert size < 64 * 1024, size
    # No note was written while the robot was stepping: all at boundaries.
    steps = r.robot.step_calls
    assert all(at in (0, steps) for at in written_at), written_at
    # The provider was cut off after the violation limit.
    assert r.orch.note_counts["contract_violation"] == 3


def test_the_note_budget_per_episode_is_kept(tmp_path):
    clock = fake.FakeClock()
    r = build(
        tmp_path,
        cfg=one_episode(note_lines_per_episode=2),
        clock=clock,
        verifier=unknown_judge(clock),
        policy={"chunk_faults": {1: "wrong_epoch", 2: "nan", 3: "wrong_dims"}},
    )
    r.orch.run()
    notes = [e for e in r.orch.journal.events if e.record == "note"]
    codes = [e.note.code for e in notes]
    assert codes.count("notes_suppressed") <= 1
    assert (
        len([c for c in codes if c != "notes_suppressed"]) <= 2 * 2
    )  # two episodes' worth at most


# --- the coordinator's decisions --------------------------------------------------------------


def judge(in_loop, final, clock):
    return ev.FakeGoalVerifier(
        lambda request, n: (
            {"decision": final}
            if request["request_id"].endswith(":final")
            else {"decision": in_loop}
        ),
        clock,
        "r-sm",
    )


@pytest.mark.parametrize(
    "final, outcome, verification",
    [
        ("confirmed", "success", "verified"),
        ("rejected", "unknown", "undecided"),
        ("unknown", "unknown", "undecided"),
    ],
)
def test_an_early_stop_is_a_success_only_if_the_final_judgement_confirms(
    tmp_path, final, outcome, verification
):
    clock = fake.FakeClock()
    r = build(
        tmp_path,
        cfg=one_episode(),
        clock=clock,
        verifier=judge("confirmed", final, clock),
    )
    r.orch.run()
    (result,) = results(r.orch)
    assert result.stop_reason == "goal_verified"
    assert (result.task_outcome, result.goal_verification) == (outcome, verification)
    asked = [q["request_id"] for q in r.verifier.submitted]
    assert asked[-1].endswith(":final")
    if final != "confirmed":
        assert "early_stop_disputed" in r.orch.note_counts


def test_an_unavailable_final_judgement_leaves_an_early_stop_unknown(tmp_path):
    clock = fake.FakeClock()
    verifier = ev.FakeGoalVerifier(
        lambda request, n: (
            {"unavailable": "timeout"}
            if request["request_id"].endswith(":final")
            else {"decision": "confirmed"}
        ),
        clock,
        "r-sm",
    )
    r = build(tmp_path, cfg=one_episode(), clock=clock, verifier=verifier)
    r.orch.run()
    (result,) = results(r.orch)
    assert (result.task_outcome, result.goal_verification) == ("unknown", "unavailable")


def test_an_episode_ended_by_a_safety_stop_keeps_a_result(tmp_path):
    r = build(tmp_path, cfg=one_episode(), robot={"step_faults": {5: "red_light"}})
    assert r.orch.run() == "FAULT_LOCKED"
    (result,) = results(r.orch)
    assert (result.task_outcome, result.stop_reason) == ("unknown", "safety_stop")
    assert (
        result.rollout.sealed == "incomplete" and result.robot_home == "not_attempted"
    )
    last = r.orch.journal.events[-1]
    assert last.to_state == "FAULT_LOCKED" and last.episode_result == result


def test_a_reset_whose_home_fails_keeps_a_result(tmp_path):
    clock = fake.FakeClock()
    scene = ev.FakeSceneAssessor([{"decision": "reset_required"}], clock, "r-sm")
    r = build(
        tmp_path,
        cfg=one_episode(),
        clock=clock,
        scene=scene,
        robot={"home_faults": {0: "home_miss"}},
    )
    assert r.orch.run() == "FAULT_LOCKED"
    (result,) = results(r.orch)
    assert (result.stop_reason, result.robot_home) == ("home_failed", "failed")
    assert result.rollout.sealed == "complete" and result.task_outcome == "unknown"


@pytest.mark.parametrize("home", [True, False])
def test_staying_put_after_an_operator_stop_is_a_setting(tmp_path, home):
    r = build(tmp_path, cfg=one_episode(home_after_operator_stop=home))
    original = r.robot.step

    def step(action, *, token):
        if r.robot.step_calls == 3:
            r.orch.stop("stop-1")
        return original(action, token=token)

    r.robot.step = step
    assert r.orch.run() == "WAIT_HUMAN"
    path = committed(r.orch)
    if home:
        assert r.robot.home_calls == 1
        assert ("ROBOT_HOME", "SCENE_ASSESS", "robot_home_reached") in path
        assert path[-1] == ("SCENE_ASSESS", "WAIT_HUMAN", "operator_stop")
    else:
        assert r.robot.home_calls == 0
        assert path[-1] == ("ROBOT_HOME", "WAIT_HUMAN", "operator_stop")
    (result,) = results(r.orch)
    assert result.rollout.sealed == "complete" and r.orch.completed_forward == 1
    assert sm.check_journal(r.orch.journal.events) == []


def test_the_home_passes_the_safety_check_first(tmp_path):
    r = build(tmp_path, cfg=one_episode())
    original = r.policy.quiesce

    def quiesce(**kwargs):
        r.robot.red_light = True  # the arm faults while the policy stops
        return original(**kwargs)

    r.policy.quiesce = quiesce
    assert r.orch.run() == "FAULT_LOCKED"
    assert r.robot.home_calls == 0
    assert committed(r.orch)[-1] == ("ROBOT_HOME", "FAULT_LOCKED", "safety_stop")


def test_the_home_token_is_gone_as_soon_as_the_home_returns(tmp_path):
    r = build(tmp_path, cfg=one_episode())
    original = r.robot.home
    after = []

    def home(*, token):
        found = original(token=token)
        after.append(token)
        return found

    r.robot.home = home
    r.orch.run()
    assert after and r.fence.current is None
    assert r.robot.home(token=after[0]).detail == "token_refused"


def test_a_flood_of_valid_but_noisy_events_writes_few_lines_at_boundaries(tmp_path):
    """Duplicates are not contract violations: the provider is never cut off,
    so only the batching and the budget keep the journal small."""
    clock = fake.FakeClock()
    noise = [{"step": 0, "event_type": "gripper_open", "id": "ev-same"}] + [
        {"step": s, "event_type": "gripper_open", "id": "ev-same", "fault": "duplicate"}
        for s in range(40)
        for _ in range(16)
    ]
    r = build(
        tmp_path,
        cfg=one_episode(),
        clock=clock,
        verifier=unknown_judge(clock),
        events=ev.FakeEventStream({"forward": noise}, clock, "r-sm"),
    )
    written_at = []
    original = r.orch.journal.note

    def note(*args, **kwargs):
        written_at.append(r.robot.step_calls)
        return original(*args, **kwargs)

    r.orch.journal.note = note
    assert r.orch.run() == "COMPLETED"
    assert r.orch.note_counts["event_dropped_duplicate"] >= 600
    assert len(written_at) <= r.config.note_lines_per_episode + 1
    assert set(written_at) <= {r.robot.step_calls}
    assert len(r.orch.journal.events) < 40
