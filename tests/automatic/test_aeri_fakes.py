"""The AERI fakes: scripted event, judgement and scene providers
(levi/automatic/adapters/events.py) and the in-process robot, policy and
recorder (integrations/fr3_automatic/fake.py). Deterministic, no network."""

import ast
import json
import threading

import pytest
from aeri_harness import FAKE, fake

from levi.automatic import state_machine as sm
from levi.automatic.adapters import events as ev
from levi.domain import aeri

RUN = "r-fake"
EP = f"{RUN}.forward.0001"


@pytest.fixture
def clock():
    return fake.FakeClock()


def request(**over):
    base = {
        "request_id": f"{EP}:q0",
        "run_id": RUN,
        "episode_id": EP,
        "episode_role": "forward",
        "target": "forward_goal",
        "observed_from_step": 3,
        "observed_through_step": 12,
        "observed_through_ns": 0,
        "event_ids": [],
    }
    base.update(over)
    return ev.make_request(**base)


# --- providers ------------------------------------------------------------------------


def test_the_fake_module_needs_no_network_process_or_levi():
    tree = ast.parse(FAKE.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"math", "queue", "threading", "dataclasses"}, imported


def test_requests_never_carry_operator_or_control_keys():
    with pytest.raises(ev.RequestRefused):
        request(operator_outcome="success")
    with pytest.raises(ev.RequestRefused):
        request(robot_stop=True)
    with pytest.raises(ev.RequestRefused):
        request(eval_label="x")


def test_events_arrive_at_their_steps_with_consecutive_numbers(clock):
    stream = ev.FakeEventStream(
        {"forward": [{"step": s, "event_type": "gripper_open"} for s in (2, 5, 9)]},
        clock,
        RUN,
    )
    sid = stream.open(EP, "forward")
    assert stream.poll(sid, through_step=1, max_events=8) == []
    first = stream.poll(sid, through_step=5, max_events=8)
    later = stream.poll(sid, through_step=50, max_events=8)
    events = [aeri.parse(raw, "event") for raw in first + later]
    assert [e.step for e in events] == [2, 5, 9]
    assert [e.seq for e in events] == [0, 1, 2]
    assert all(e.episode_id == EP and e.status == "candidate" for e in events)


@pytest.mark.parametrize(
    "fault, code",
    [("control_key", "E_CONTROL_FIELD"), ("bad_json", "E_JSON")],
)
def test_broken_events_are_refused_by_the_contract(clock, fault, code):
    stream = ev.FakeEventStream(
        {"forward": [{"step": 1, "event_type": "gripper_open", "fault": fault}]},
        clock,
        RUN,
    )
    sid = stream.open(EP, "forward")
    (raw,) = stream.poll(sid, through_step=1, max_events=1)
    with pytest.raises(aeri.AeriError) as exc:
        aeri.parse(raw, "event")
    assert exc.value.code == code


def test_event_faults_gap_duplicate_clock_and_retraction(clock):
    script = [
        {"step": 1, "event_type": "gripper_open"},
        {"step": 2, "event_type": "gripper_open", "fault": "skip_seq"},
        {"step": 3, "event_type": "gripper_open", "fault": "duplicate"},
        {"step": 4, "event_type": "gripper_open", "fault": "other_clock"},
        {"step": 5, "event_type": "gripper_open", "retracts": "x-1"},
    ]
    stream = ev.FakeEventStream({EP: script}, clock, RUN)
    sid = stream.open(EP, "forward")
    events = [
        aeri.parse(r, "event") for r in stream.poll(sid, through_step=9, max_events=9)
    ]
    assert [e.seq for e in events] == [0, 2, 3, 4, 5]
    assert events[2].event_id == events[1].event_id
    assert events[3].clock_domain != clock.domain
    assert events[4].status == "retracted" and events[4].retracts == "x-1"


@pytest.mark.parametrize("decision", ["confirmed", "rejected", "unknown"])
def test_judgements_parse_with_the_registered_spec(clock, decision):
    verifier = ev.FakeGoalVerifier([{"decision": decision}], clock, RUN)
    ticket = verifier.submit(request())
    assert verifier.collect(ticket, timeout_ns=0) is None  # not yet: never blocks
    started = clock.now()
    raw = verifier.collect(ticket, timeout_ns=10**9)
    assert clock.now() - started == ev.DEFAULT_DELAY_NS  # waited exactly its delay
    message = aeri.parse(raw, "judgement", specs=ev.SPECS)
    assert message.decision == decision and message.request_id == f"{EP}:q0"
    aeri.check_fresh(message, now_ns=clock.now(), local=clock.domain)
    assert verifier.collect(ticket, timeout_ns=10**9) is None  # answered once


def test_judgement_unavailable_at_submit_and_at_collect(clock):
    verifier = ev.FakeGoalVerifier(
        [{"submit_unavailable": "busy"}, {"unavailable": "timeout"}], clock, RUN
    )
    raw = verifier.submit(request())
    first = aeri.parse(raw, "judgement")
    assert first.kind == "unavailable" and first.code == "busy" and first.retryable
    ticket = verifier.submit(request(request_id=f"{EP}:q1"))
    second = aeri.parse(verifier.collect(ticket, timeout_ns=10**9), "judgement")
    assert second.kind == "unavailable" and second.code == "timeout"
    assert not second.retryable


@pytest.mark.parametrize(
    "spec, code",
    [
        ({"minor": 1}, "E_SCHEMA_TOO_NEW"),
        ({"contradict": True}, "E_INCONSISTENT"),
        ({"bad_predicate": True}, "E_SPEC_MISMATCH"),
        ({"control_key": True}, "E_CONTROL_FIELD"),
        ({"valid_ms": 60_000}, "E_INCONSISTENT"),
    ],
)
def test_bad_judgements_are_refused_by_the_contract(clock, spec, code):
    verifier = ev.FakeGoalVerifier([{"decision": "confirmed", **spec}], clock, RUN)
    raw = verifier.collect(verifier.submit(request()), timeout_ns=10**9)
    with pytest.raises(aeri.AeriError) as exc:
        aeri.parse(raw, "judgement", specs=ev.SPECS)
    assert exc.value.code == code


@pytest.mark.parametrize(
    "spec, code",
    [
        ({"expired": True}, "E_EXPIRED"),
        ({"future": True}, "E_FUTURE"),
        ({"other_clock": True}, "E_CLOCK_DOMAIN"),
    ],
)
def test_stale_judgements_fail_the_freshness_check(clock, spec, code):
    verifier = ev.FakeGoalVerifier([{"decision": "confirmed", **spec}], clock, RUN)
    raw = verifier.collect(verifier.submit(request()), timeout_ns=10**9)
    message = aeri.parse(raw, "judgement", specs=ev.SPECS)
    with pytest.raises(aeri.AeriError) as exc:
        aeri.check_fresh(message, now_ns=clock.now(), local=clock.domain)
    assert exc.value.code == code


@pytest.mark.parametrize("decision", ["ready", "reset_required", "unknown"])
def test_scene_assessments_parse(clock, decision):
    scene = ev.FakeSceneAssessor([{"decision": decision}], clock, RUN)
    req = ev.make_request(
        request_id="s-1", run_id=RUN, episode_id=EP, target="initial_state"
    )
    raw = scene.collect(scene.submit(req), timeout_ns=10**9)
    assert aeri.parse(raw, "scene", specs=ev.SPECS).decision == decision


# --- robot ---------------------------------------------------------------------------------


def motion_token(fence, clock, kind="policy_steps", tx="r:tx3"):
    return fence.issue(sm.MotionToken(RUN, tx, 1, kind, EP, 1, clock.now() + 10**12))


ACTION = [0.45, 0.0, 0.4, 0.0, 1.0, 0.0, 0.0]


def test_robot_moves_only_with_the_current_token(clock):
    fence = sm.MotionFence()
    robot = fake.FakeRobot(clock, fence.check)
    assert robot.step(ACTION, token=None).detail == "token_refused"
    token = motion_token(fence, clock)
    assert robot.step(ACTION, token=token).executed == "yes"
    assert robot.home(token=token).detail == "token_refused"  # wrong kind
    fence.revoke()
    assert robot.step(ACTION, token=token).detail == "token_refused"
    assert len(robot.motions) == 1 and len(robot.refused) == 3


def test_robot_rejects_bad_actions(clock):
    fence = sm.MotionFence()
    robot = fake.FakeRobot(clock, fence.check)
    token = motion_token(fence, clock)
    assert robot.step([float("nan")] * 7, token=token).detail == "bad_action"
    assert robot.step([0.0] * 6, token=token).detail == "bad_action"
    assert robot.motions == []


def test_three_503s_latch_and_a_latch_refuses_every_motion(clock):
    fence = sm.MotionFence()
    robot = fake.FakeRobot(
        clock, fence.check, step_faults={0: "http_503", 1: "http_503", 2: "http_503"}
    )
    token = motion_token(fence, clock)
    assert [robot.step(ACTION, token=token).detail for _ in range(3)] == [
        "http_503"
    ] * 3
    assert robot.latch_state() == "http_503"
    assert robot.step(ACTION, token=token).detail == "latched"
    home = motion_token(fence, clock, kind="home", tx="r:tx4")
    assert robot.home(token=home).detail == "latched"
    assert not robot.preflight()["ok"]
    assert robot.motions == []


def test_stale_states_latch_and_red_light_is_red(clock):
    fence = sm.MotionFence()
    faults = {n: "stale" for n in range(6)}
    faults[7] = "red_light"
    robot = fake.FakeRobot(clock, fence.check, step_faults=faults)
    token = motion_token(fence, clock)
    for _ in range(6):
        robot.step(ACTION, token=token)
    assert robot.latch_state() == "state_stale"
    robot.latched = None
    robot.step(ACTION, token=token)
    robot.step(ACTION, token=token)
    assert robot.health()["state"] == "red" and robot.latch_state() == "red_light"


def test_no_reply_is_unknown_and_a_crash_after_send_counts_the_command(clock):
    fence = sm.MotionFence()
    robot = fake.FakeRobot(
        clock,
        fence.check,
        step_faults={0: "no_reply", 1: "crash_after_send"},
        home_faults={0: "home_miss"},
    )
    token = motion_token(fence, clock)
    assert robot.step(ACTION, token=token).executed == "unknown"
    with pytest.raises(fake.SimulatedCrash):
        robot.step(ACTION, token=token)
    assert len(robot.motions) == 2
    home = motion_token(fence, clock, kind="home", tx="r:tx9")
    found = robot.home(token=home)
    assert found.executed == "no" and found.error_mm > fake.FakeRobot.HOME_TOLERANCE_MM


def test_camera_stall_repeats_frames(clock):
    robot = fake.FakeRobot(
        clock,
        sm.MotionFence().check,
        observe_faults={2: "camera_stall"},
        stall_frames=3,
    )
    frames = [robot.observe()["frames"]["side"] for _ in range(7)]
    assert frames == [1, 2, 2, 2, 2, 3, 4]


# --- policy ----------------------------------------------------------------------------


def chunk_request(n=0, epoch=1, start=0):
    return {
        "request_id": f"{EP}:c{n}",
        "run_id": RUN,
        "episode_id": EP,
        "policy_epoch": epoch,
        "action_start_index": start,
        "clock_domain": "host-mono:fake-boot",
    }


def received(raw, clock):
    return aeri.validate({**raw, "received_ns": clock.now()}, "runtime")


def test_policy_answers_after_its_latency_on_the_fake_clock(clock):
    policy = fake.FakePolicy(clock, latency_ns=40_000_000)
    ticket = policy.request(chunk_request())
    start = clock.now()
    chunk = received(policy.collect(ticket, timeout_ns=10**9), clock)
    assert clock.now() - start == 40_000_000
    assert chunk.kind == "chunk_response" and len(chunk.actions) == 8
    assert all(len(row) == 7 for row in chunk.actions)


def test_policy_faults(clock):
    policy = fake.FakePolicy(
        clock,
        chunk_faults={
            0: "timeout",
            1: "server_error",
            2: "nan",
            3: "wrong_dims",
            4: "wrong_epoch",
            5: "valid_from_early",
        },
    )
    start = clock.now()
    assert policy.collect(policy.request(chunk_request(0)), timeout_ns=500) is None
    assert clock.now() - start == 500  # waited its timeout, no more
    assert (
        policy.collect(policy.request(chunk_request(1)), timeout_ns=10**9)["code"]
        == "server_error"
    )
    with pytest.raises(aeri.AeriError) as exc:
        received(
            policy.collect(policy.request(chunk_request(2)), timeout_ns=10**9), clock
        )
    assert exc.value.code == "E_NONFINITE"
    dims = received(
        policy.collect(policy.request(chunk_request(3)), timeout_ns=10**9), clock
    )
    assert len(dims.actions[0]) == 6
    epoch = received(
        policy.collect(policy.request(chunk_request(4)), timeout_ns=10**9), clock
    )
    assert epoch.policy_epoch == 2
    early = received(
        policy.collect(policy.request(chunk_request(5, start=4)), timeout_ns=10**9),
        clock,
    )
    assert early.valid_from_action_index == 3


def test_quiesce_cancels_inflight_and_can_fail(clock):
    policy = fake.FakePolicy(clock, chunk_faults={0: "timeout"})
    policy.request(chunk_request(0))
    ack = policy.quiesce(policy_epoch=1)
    assert ack == {"ok": True, "fenced_epoch": 1, "inflight_cancelled": 1}
    assert (
        fake.FakePolicy(clock, quiesce_fails=True).quiesce(policy_epoch=1)["ok"]
        is False
    )
    assert (
        fake.FakePolicy(clock, acquire_fails=True).acquire("forward", policy_epoch=1)[
            "kind"
        ]
        == "unavailable"
    )


def test_late_responses_are_still_chunk_responses_of_the_old_epoch(clock):
    policy = fake.FakePolicy(clock, chunk_faults={0: "timeout"})
    policy.request(chunk_request(0, epoch=3))
    (late,) = policy.late_responses()
    assert received(late, clock).policy_epoch == 3


# --- recorder --------------------------------------------------------------------------


def test_recorder_thread_writes_every_commit_before_the_seal():
    recorder = fake.FakeRecorder()
    rollout = recorder.open(task_folder="t", episode_id=EP, number=1)

    def writer():
        for _ in range(250):
            recorder.commit(rollout, None)

    threads = [threading.Thread(target=writer) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert recorder.seal(rollout, {}) == {"sealed": "complete", "demo": "demo_0001"}
    assert rollout.written == 1000 and rollout.complete_marker
    assert recorder.seal(rollout, {})["sealed"] == "complete"  # idempotent
    recorder.close()


def test_a_failed_write_never_leaves_a_complete_marker():
    recorder = fake.FakeRecorder(write_faults={3: "disk_full"})
    rollout = recorder.open(task_folder="t", episode_id=EP, number=1)
    for _ in range(5):
        try:
            recorder.commit(rollout, None)
        except fake.RecorderError:
            break
    with pytest.raises(fake.RecorderError):
        recorder.seal(rollout, {})
    assert not rollout.complete_marker
    assert recorder.abort(rollout, "recorder_failed") == {
        "sealed": "incomplete",
        "demo": "incomplete_0001",
    }
    with pytest.raises(fake.RecorderError):
        recorder.open(task_folder="t", episode_id=EP, number=1)  # never reused
    recorder.close()


def test_fidelity_list_names_the_known_differences():
    text = json.dumps(fake.FIDELITY)
    for word in ("e-stop", "dynamics", "home", "criteria.check", "network"):
        assert word in text
