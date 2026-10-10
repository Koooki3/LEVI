"""The AERI orchestrator end to end on fakes (levi/automatic/orchestrator.py):
the normal loop, the fault-injection list of the design (pipeline §11.1),
crash recovery at every transaction stage, operator commands, and random
runs checked against the state machine's invariants."""

import itertools
import os
import random
import signal
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from aeri_harness import (
    build,
    committed,
    config,
    crash_at,
    fake,
    restart,
    results,
    rig,
)

from levi.automatic import state_machine as sm
from levi.automatic.adapters import events as ev
from levi.automatic.journal import JournalRefused
from levi.automatic.orchestrator import Orchestrator
from levi.automatic.termination import TerminationConfig

HERE = Path(__file__).resolve().parent
CHILD = HERE / "sm_child.py"
POINTS = (
    "before_prepared",
    "after_prepared",
    "after_execute",
    "after_acknowledged",
    "after_committed",
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Nothing in these tests may open a connection."""

    def refuse(*args, **kwargs):
        raise AssertionError("an AERI test tried to use the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def check_invariants(r, extra_robots=()):
    """What must hold for any run: the journal keeps the table; every motion
    the robots accepted ran under a token of a transaction that allowed it;
    no unverified success; no physical action prepared twice."""
    events = r.orch.journal.events
    assert sm.check_journal(events) == []
    by_tx = {}
    for e in events:
        by_tx.setdefault(e.transaction_id, []).append(e)
    for robot in (r.robot, *extra_robots):
        for kind, episode, tx in robot.motions:
            lines = by_tx[tx]
            prepared = lines[0]
            assert prepared.record == "prepared" and prepared.episode_id == episode
            if kind == "policy_steps":
                assert prepared.to_state in sm.MOTION_STATES
                assert any(e.record == "committed" for e in lines), tx
            else:
                assert prepared.action.kind == "home"
    keys = [
        e.action.idempotency_key
        for e in events
        if e.record == "prepared" and e.action.non_idempotent
    ]
    assert len(keys) == len(set(keys))
    for result in results(r.orch):
        if result.task_outcome == "success":
            assert result.goal_verification == "verified"
    with_result = [
        e.episode_id for e in events if e.record == "committed" and e.episode_result
    ]
    assert len(with_result) == len(set(with_result)), "one result per episode"


def one_episode(**over):
    return config(episodes=1, **over)


# --- the normal loop ---------------------------------------------------------------------


def test_two_episodes_with_early_stops_complete(tmp_path):
    r = build(tmp_path)
    assert r.orch.run() == "COMPLETED"
    path = [(a, b) for a, b, _ in committed(r.orch)]
    assert path[:7] == [
        ("PREFLIGHT", "VERIFY_INITIAL"),
        ("VERIFY_INITIAL", "FORWARD_ACTIVE"),
        ("FORWARD_ACTIVE", "FORWARD_STOPPING"),
        ("FORWARD_STOPPING", "FORWARD_FINALIZE"),
        ("FORWARD_FINALIZE", "ROBOT_HOME"),
        ("ROBOT_HOME", "SCENE_ASSESS"),
        ("SCENE_ASSESS", "FORWARD_ACTIVE"),
    ]
    assert path[-1] == ("SCENE_ASSESS", "COMPLETED")
    found = results(r.orch)
    assert [x.task_outcome for x in found] == ["success", "success"]
    assert all(
        x.stop_reason == "goal_verified" and x.rollout.sealed == "complete"
        for x in found
    )
    assert {x.rollout.demo for x in found} == {"demo_0001", "demo_0002"}
    assert r.robot.refused == [] and r.robot.home_calls == 2
    assert all(rollout.complete_marker for rollout in r.recorder.rollouts.values())
    check_invariants(r)


def test_a_seeded_run_replays_identically(tmp_path):
    def run(where):
        r = build(where, policy={"rng": random.Random(7)})
        r.orch.run()
        lines = [
            (e.record, e.from_state, e.to_state, e.reason, e.mono_ns)
            for e in r.orch.journal.events
        ]
        return lines, list(r.robot.motions), r.robot.pose

    assert run(tmp_path / "a") == run(tmp_path / "b")


def test_scene_needing_a_reset_runs_the_reset_policy_then_checks_again(tmp_path):
    clock = fake.FakeClock()
    scene = ev.FakeSceneAssessor(
        [{"decision": "reset_required"}, {"decision": "ready"}, {"decision": "ready"}],
        clock,
        "r-sm",
    )
    events = ev.FakeEventStream(
        {
            "forward": [{"step": 8, "event_type": "object_settled"}],
            "reset": [{"step": 6, "event_type": "object_settled"}],
        },
        clock,
        "r-sm",
    )
    r = build(tmp_path, cfg=one_episode(), clock=clock, scene=scene, events=events)
    assert r.orch.run() == "COMPLETED"
    path = [(a, b, why) for a, b, why in committed(r.orch)]
    assert path[1] == ("VERIFY_INITIAL", "RESET_ACTIVE", "scene_reset_required")
    assert ("RESET_FINALIZE", "VERIFY_INITIAL", "reset_verified") in path
    reset_result = results(r.orch)[0]
    assert (
        reset_result.scene_reset == "succeeded"
        and reset_result.robot_home == "succeeded"
    )
    assert reset_result.rollout.task_folder == "reset"
    check_invariants(r)


# --- pipeline §11.1, one test per row ---------------------------------------------------------


def test_row1_a_wrong_success_does_not_stop_early(tmp_path):
    clock = fake.FakeClock()
    verifier = ev.FakeGoalVerifier(
        [], clock, "r-sm", default={"decision": "confirmed", "contradict": True}
    )
    r = build(tmp_path, cfg=one_episode(), clock=clock, verifier=verifier)
    r.orch.run()
    (result,) = results(r.orch)
    assert result.stop_reason == "horizon_exhausted"
    assert (
        result.task_outcome == "unknown" and result.goal_verification == "unavailable"
    )
    assert "contract_violation" in {code for code, _ in r.orch.notes}
    check_invariants(r)


@pytest.mark.parametrize("decision", ["unknown", "rejected"])
def test_row2_unknown_runs_to_the_horizon_and_is_never_a_success(tmp_path, decision):
    clock = fake.FakeClock()
    verifier = ev.FakeGoalVerifier([], clock, "r-sm", default={"decision": decision})
    r = build(tmp_path, cfg=one_episode(), clock=clock, verifier=verifier)
    r.orch.run()
    (result,) = results(r.orch)
    assert result.stop_reason == "horizon_exhausted"
    expected = {
        "unknown": ("unknown", "undecided"),
        "rejected": ("failure", "contradicted"),
    }
    assert (result.task_outcome, result.goal_verification) == expected[decision]
    assert r.robot.step_calls == r.config.forward_max_steps
    check_invariants(r)


@pytest.mark.parametrize(
    "default",
    [
        {"delay_ns": 60_000_000_000},
        {"unavailable": "timeout"},
        {"submit_unavailable": "gate_closed"},
        {"unavailable": "model_error"},
    ],
)
def test_row3_a_slow_or_offline_judge_never_blocks_the_control_loop(tmp_path, default):
    clock = fake.FakeClock()
    verifier = ev.FakeGoalVerifier([], clock, "r-sm", default=default)
    r = build(tmp_path, cfg=one_episode(), clock=clock, verifier=verifier)
    began = clock.now()
    r.orch.run()
    (result,) = results(r.orch)
    assert result.stop_reason == "horizon_exhausted"
    assert (
        result.task_outcome == "unknown" and result.goal_verification == "unavailable"
    )
    assert r.robot.step_calls == r.config.forward_max_steps
    # The loop only waited for its own steps and chunks, plus the bounded
    # final judgement and scene waits after the policy was quiesced.
    cfg = r.config
    bound = (
        cfg.forward_max_steps * (cfg.control_period_ns + r.policy.latency_ns)
        + cfg.final_judge_timeout_ns
        + 3 * cfg.scene_timeout_ns
    )
    assert clock.now() - began <= bound
    check_invariants(r)


@pytest.mark.parametrize("horizon, expected", [(40, (1, 2)), (100, (3, 3))])
def test_row4_a_flapping_detector_is_merged_spaced_and_capped(
    tmp_path, horizon, expected
):
    """15 goal candidates flapping over 30 steps: merged into the latest one,
    so a request waits for the flapping to settle; with a long horizon the
    unknown answers are retried only up to ``max_requests``."""
    clock = fake.FakeClock()
    flaps = [
        {
            "step": 5 + i,
            "event_type": "gripper_open",
            "priority": "goal_candidate" if i % 2 == 0 else "routine",
        }
        for i in range(30)
    ]
    events = ev.FakeEventStream({"forward": flaps}, clock, "r-sm")
    verifier = ev.FakeGoalVerifier([], clock, "r-sm", default={"decision": "unknown"})
    termination = TerminationConfig(
        min_steps=5, settle_steps=3, cooldown_steps=5, max_requests=3
    )
    r = build(
        tmp_path,
        cfg=one_episode(termination=termination, forward_max_steps=horizon),
        clock=clock,
        events=events,
        verifier=verifier,
    )
    r.orch.run()
    asked = [q for q in verifier.submitted if not q["request_id"].endswith(":final")]
    low, high = expected
    assert low <= len(asked) <= high
    steps = [q["observed_through_step"] for q in asked]
    assert all(b - a >= 5 for a, b in itertools.pairwise(steps))
    check_invariants(r)


class StalePolicy(fake.FakePolicy):
    """A broker that hands a reset request the forward policy's last chunk."""

    def __init__(self, clock, **kw):
        super().__init__(clock, **kw)
        self.forward_chunk = None
        self.delivered_stale = 0

    def collect(self, ticket_id, *, timeout_ns):
        found = super().collect(ticket_id, timeout_ns=timeout_ns)
        if isinstance(found, dict) and found.get("kind") == "chunk_response":
            if ".forward." in found["episode_id"]:
                self.forward_chunk = found
            elif self.forward_chunk is not None and self.delivered_stale < 3:
                self.delivered_stale += 1
                return dict(self.forward_chunk)
        return found


def test_row5_a_late_forward_chunk_never_runs_in_the_reset_phase(tmp_path):
    clock = fake.FakeClock()
    scene = ev.FakeSceneAssessor(
        [{"decision": "ready"}, {"decision": "reset_required"}, {"decision": "ready"}],
        clock,
        "r-sm",
    )
    policy = StalePolicy(clock)
    r = build(
        tmp_path,
        cfg=config(episodes=2, max_reset_attempts=1),
        clock=clock,
        scene=scene,
        policy=policy,
    )
    r.orch.run()
    assert policy.delivered_stale == 3
    dropped = [d for code, d in r.orch.notes if code == "chunk_dropped_epoch"]
    assert len(dropped) == 3 and all(".forward." in d for d in dropped)
    # The reset ran only reset actions, under reset tokens.
    path = [(a, b) for a, b, _ in committed(r.orch)]
    assert ("SCENE_ASSESS", "RESET_ACTIVE") in path
    reset_motions = [m for m in r.robot.motions if ".reset." in m[1]]
    assert reset_motions
    forward_txs = {tx for kind, ep, tx in r.robot.motions if ".forward." in ep}
    assert not forward_txs & {tx for _, _, tx in reset_motions}
    check_invariants(r)


def test_an_old_forward_token_cannot_move_the_robot_after_the_episode(tmp_path):
    r = build(tmp_path, cfg=one_episode())
    seen = {}
    original = r.robot.step

    def spy(action, *, token):
        seen.setdefault("token", token)
        return original(action, token=token)

    r.robot.step = spy
    r.orch.run()
    late = r.robot.step([0.45, 0, 0.4, 0, 1, 0, 0], token=seen["token"])
    assert late.executed == "no" and late.detail == "token_refused"


def test_row6_a_crashed_policy_server_stops_and_locks(tmp_path):
    clock = fake.FakeClock()
    verifier = ev.FakeGoalVerifier([], clock, "r-sm", default={"decision": "unknown"})
    r = build(
        tmp_path,
        cfg=one_episode(),
        clock=clock,
        verifier=verifier,
        policy={"chunk_faults": {2: "crash"}},
    )
    assert r.orch.run() == "FAULT_LOCKED"
    path = committed(r.orch)
    assert ("FORWARD_ACTIVE", "FORWARD_STOPPING", "policy_error") in path
    assert path[-1] == ("FORWARD_STOPPING", "FAULT_LOCKED", "policy_error")
    # pipeline §4.3: an episode ended by a fault is recorded as unknown.
    (result,) = results(r.orch)
    assert (result.task_outcome, result.stop_reason) == ("unknown", "policy_error")
    assert result.rollout.sealed == "incomplete"
    check_invariants(r)


def test_row7_camera_stall_makes_evidence_unavailable(tmp_path):
    r = build(
        tmp_path,
        cfg=one_episode(camera_stall_limit=3),
        robot={"observe_faults": {2: "camera_stall"}, "stall_frames": 100},
    )
    r.orch.run()
    (result,) = results(r.orch)
    assert result.stop_reason == "horizon_exhausted"
    assert (
        result.goal_verification == "unavailable" and result.task_outcome == "unknown"
    )
    assert "evidence_unavailable" in {code for code, _ in r.orch.notes}
    check_invariants(r)


def test_row8_no_policy_resources_before_an_episode_waits_for_a_person(tmp_path):
    r = build(tmp_path, cfg=one_episode(), policy={"acquire_fails": True})
    assert r.orch.run() == "WAIT_HUMAN"
    assert committed(r.orch)[-1] == ("VERIFY_INITIAL", "WAIT_HUMAN", "policy_error")
    assert r.robot.motions == []
    check_invariants(r)


def test_row9_a_reset_at_its_horizon_saves_the_rollout_and_waits(tmp_path):
    clock = fake.FakeClock()
    scene = ev.FakeSceneAssessor(
        [{"decision": "reset_required"}, {"decision": "reset_required"}], clock, "r-sm"
    )
    r = build(tmp_path, cfg=one_episode(), clock=clock, scene=scene)
    assert r.orch.run() == "WAIT_HUMAN"
    path = committed(r.orch)
    assert ("RESET_ACTIVE", "RESET_VERIFY", "reset_horizon_exhausted") in path
    assert path[-1] == ("RESET_FINALIZE", "WAIT_HUMAN", "reset_horizon_exhausted")
    (result,) = results(r.orch)
    assert result.scene_reset == "failed" and result.rollout.sealed == "complete"
    motions = len(r.robot.motions)
    assert r.orch.run() == "WAIT_HUMAN" and len(r.robot.motions) == motions
    check_invariants(r)


def test_row10_a_failed_home_locks_and_nothing_moves_again(tmp_path):
    r = build(tmp_path, robot={"home_faults": {0: "home_miss"}})
    assert r.orch.run() == "FAULT_LOCKED"
    assert committed(r.orch)[-1] == ("ROBOT_HOME", "FAULT_LOCKED", "home_failed")
    motions = len(r.robot.motions)
    r.orch.run()
    assert len(r.robot.motions) == motions
    check_invariants(r)


@pytest.mark.parametrize("fault", ["red_light", "latch"])
def test_row11_estop_or_fr3_fault_locks_by_the_safety_guard(tmp_path, fault):
    r = build(tmp_path, cfg=one_episode(), robot={"step_faults": {5: fault}})
    assert r.orch.run() == "FAULT_LOCKED"
    last = r.orch.journal.events[-1]
    assert (last.from_state, last.to_state, last.reason) == (
        "FORWARD_ACTIVE",
        "FAULT_LOCKED",
        "safety_stop",
    )
    assert last.authority.principal_kind == "safety_guard"
    (rollout,) = r.recorder.rollouts.values()
    assert rollout.state == "incomplete" and not rollout.complete_marker
    # Resuming does not clear the hardware: the preflight locks again.
    seq = r.orch.journal.next_seq
    assert r.orch.resume(
        "cmd-1", expected_seq=seq, environment_handled=True, health_rechecked=True
    ).ok
    assert r.orch.run() == "FAULT_LOCKED"
    assert committed(r.orch)[-1][:2] == ("PREFLIGHT", "FAULT_LOCKED")
    check_invariants(r)


def test_row12_a_recorder_write_failure_never_marks_complete(tmp_path):
    r = build(tmp_path, cfg=one_episode(), recorder={"write_faults": {3: "disk_full"}})
    assert r.orch.run() == "FAULT_LOCKED"
    assert committed(r.orch)[-1][2] == "recorder_failed"
    (rollout,) = r.recorder.rollouts.values()
    assert rollout.state == "incomplete" and not rollout.complete_marker
    check_invariants(r)


def test_a_seal_failure_locks_without_a_verdict(tmp_path):
    r = build(tmp_path, cfg=one_episode(), recorder={"threaded": False})
    original = r.recorder.seal

    def broken(rollout, meta):
        rollout.errors.append("fsync failed")
        return original(rollout, meta)

    r.recorder.seal = broken
    assert r.orch.run() == "FAULT_LOCKED"
    assert committed(r.orch)[-1] == (
        "FORWARD_FINALIZE",
        "FAULT_LOCKED",
        "recorder_failed",
    )
    (result,) = results(r.orch)
    assert (result.task_outcome, result.stop_reason) == ("unknown", "recorder_failed")
    assert result.rollout.sealed == "incomplete" and r.orch.completed_forward == 0
    check_invariants(r)


# --- row 13: orchestrator restart, at every stage of every transaction --------------------------


def crash_cases():
    cases = []
    for point in POINTS:
        for occurrence in range(40):
            cases.append((point, occurrence))
    return cases


def test_row13_a_crash_at_any_stage_recovers_to_fault_locked_without_replay(tmp_path):
    clock_scene = [
        {"decision": "reset_required"},
        {"decision": "ready"},
        {"decision": "ready"},
    ]
    tried = 0
    for point, occurrence in crash_cases():
        where = tmp_path / f"{point}-{occurrence}"
        clock = fake.FakeClock()
        scene = ev.FakeSceneAssessor(list(clock_scene), clock, "r-sm")
        events = ev.FakeEventStream(
            {
                "forward": [{"step": 8, "event_type": "object_settled"}],
                "reset": [{"step": 6, "event_type": "object_settled"}],
            },
            clock,
            "r-sm",
        )
        r = build(
            where,
            cfg=one_episode(),
            clock=clock,
            scene=scene,
            events=events,
            crash_hook=crash_at(point, occurrence),
        )
        try:
            r.orch.run()
        except fake.SimulatedCrash:
            pass
        else:
            continue  # this transaction count does not exist for this point
        tried += 1
        before = r.orch.journal.events
        had_completed = r.orch.state == "COMPLETED"
        old_robot = r.robot
        new = restart(r)
        assert new.orch.state == ("COMPLETED" if had_completed else "FAULT_LOCKED")
        if not had_completed:
            assert new.orch.recovery.reason == "recovery_ambiguous"
        assert new.orch.run() in ("FAULT_LOCKED", "COMPLETED")
        assert new.robot.motions == []  # nothing replayed after the restart
        check_invariants(new)
        # The crashed transaction's motion was sent at most once.
        assert old_robot.home_calls <= 2
        new.orch.close()
        assert sm.check_journal(before) == []
    # 11 transactions, 9 of them with an action: 11 * 3 + 9 * 2 crash sites.
    assert tried == 51


def test_after_recovery_the_operator_resumes_into_a_new_episode(tmp_path):
    r = build(
        tmp_path, cfg=one_episode(), robot={"step_faults": {6: "crash_after_send"}}
    )
    with pytest.raises(fake.SimulatedCrash):
        r.orch.run()
    old_motions = list(r.robot.motions)
    new = restart(r)
    assert new.orch.state == "FAULT_LOCKED"
    episode_before = old_motions[0][1]
    # The same physical action can never be prepared again, in any epoch.
    with pytest.raises(JournalRefused):
        new.orch.journal.prepare(
            "PREFLIGHT",
            "human_resumed",
            kind="policy_steps",
            non_idempotent=True,
            step=0,
            episode_id=episode_before,
            episode_role="forward",
            authority=new.orch._auth("operator", "c-x"),
            control_epoch=new.orch.journal.control_epoch + 5,
        )
    seq = new.orch.journal.next_seq
    assert new.orch.resume(
        "c-1", expected_seq=seq, environment_handled=True, health_rechecked=True
    ).ok
    assert new.orch.run() == "COMPLETED"
    episodes = {ep for _, ep, _ in new.robot.motions}
    assert episode_before not in episodes and episodes == {"r-sm.forward.0002"}
    check_invariants(new)


@pytest.mark.parametrize("point", POINTS)
def test_row13_killed_by_sigkill_at_each_stage(tmp_path, point):
    counter = tmp_path / "commands"
    directory = tmp_path / "run"
    proc = subprocess.run(  # noqa: PLW1510 - the return code is checked below
        [sys.executable, str(CHILD), str(directory), point, str(counter)],
        cwd=HERE.parents[1],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PYTHONPATH": f"{HERE.parents[1]}{os.pathsep}{HERE}"},
    )
    assert proc.returncode == -signal.SIGKILL, proc.stderr[-2000:]
    sent = counter.read_bytes() if counter.exists() else b""
    clock = fake.FakeClock(start_ns=10**15)  # later than anything the child wrote
    r = rig(directory, cfg=one_episode(), clock=clock, create=False)
    r.orch = Orchestrator.restore(directory, r.config, **r.parts())
    assert r.orch.state == "FAULT_LOCKED"
    assert r.orch.run() == "FAULT_LOCKED" and r.robot.motions == []
    assert (counter.read_bytes() if counter.exists() else b"") == sent
    assert len(sent) <= 1  # the home was sent once at most
    assert sm.check_journal(r.orch.journal.events) == []
    r.orch.close()


# --- row 14 and other operator commands -------------------------------------------------------


def wait_human(tmp_path):
    r = build(tmp_path, cfg=one_episode(), policy={"acquire_fails": True})
    assert r.orch.run() == "WAIT_HUMAN"
    r.policy.acquire_fails = False
    return r


def test_row14_a_double_resume_resumes_once(tmp_path):
    r = wait_human(tmp_path)
    seq = r.orch.journal.next_seq
    first = r.orch.resume(
        "c-1", expected_seq=seq, environment_handled=True, health_rechecked=True
    )
    again = r.orch.resume(
        "c-1", expected_seq=seq, environment_handled=True, health_rechecked=True
    )
    assert first.ok and again.ok and again.repeated
    assert again.sequence_no == first.sequence_no
    other = r.orch.resume(
        "c-2", expected_seq=seq, environment_handled=True, health_rechecked=True
    )
    assert not other.ok and other.code == "not_waiting"
    resumes = [x for x in committed(r.orch) if x[2] == "human_resumed"]
    assert len(resumes) == 1


def test_a_stale_sequence_number_is_refused(tmp_path):
    r = wait_human(tmp_path)
    stale = r.orch.journal.next_seq - 1
    found = r.orch.resume(
        "c-1", expected_seq=stale, environment_handled=True, health_rechecked=True
    )
    assert not found.ok and found.code == "stale_sequence"
    assert r.orch.state == "WAIT_HUMAN"


@pytest.mark.parametrize("env, health", [(True, False), (False, True), (1, True)])
def test_resume_needs_both_confirmations(tmp_path, env, health):
    r = wait_human(tmp_path)
    found = r.orch.resume(
        "c-1",
        expected_seq=r.orch.journal.next_seq,
        environment_handled=env,
        health_rechecked=health,
    )
    assert not found.ok and found.code == "confirmations_missing"


def test_an_operator_stop_finishes_the_episode_then_waits_then_ends(tmp_path):
    r = build(tmp_path, cfg=one_episode(), verifier=None)
    original = r.robot.step
    calls = {"n": 0}

    def step(action, *, token):
        calls["n"] += 1
        if calls["n"] == 4:
            r.orch.stop("stop-1")
        return original(action, token=token)

    r.robot.step = step
    assert r.orch.run() == "WAIT_HUMAN"
    path = committed(r.orch)
    assert ("FORWARD_ACTIVE", "FORWARD_STOPPING", "operator_stop") in path
    stopping = next(
        e
        for e in r.orch.journal.events
        if e.record == "committed" and e.to_state == "FORWARD_STOPPING"
    )
    assert stopping.authority.principal_kind == "operator"
    assert stopping.authority.command_id == "stop-1"
    assert path[-1] == ("SCENE_ASSESS", "WAIT_HUMAN", "operator_stop")
    assert r.orch.stop("stop-2").code == "completed"
    assert r.orch.state == "COMPLETED"
    assert r.orch.stop("stop-2").repeated
    check_invariants(r)


def test_a_corrupt_journal_is_locked_and_refuses_commands(tmp_path):
    r = build(tmp_path, cfg=one_episode())
    r.orch.run()
    r.orch.close()
    path = r.directory / "state_journal.jsonl"
    lines = path.read_bytes().split(b"\n")
    lines[3] = lines[3].replace(b'"record":"', b'"record" :"')
    path.write_bytes(b"\n".join(lines))
    new = restart(r)
    assert (
        new.orch.state == "FAULT_LOCKED"
        and new.orch.recovery.reason == "journal_corrupt"
    )
    assert new.orch.run() == "FAULT_LOCKED" and new.robot.motions == []
    found = new.orch.resume(
        "c-1", expected_seq=0, environment_handled=True, health_rechecked=True
    )
    assert not found.ok and found.code == "journal_corrupt"


# --- random runs ---------------------------------------------------------------------------


def random_rig(where, seed):
    rng = random.Random(seed)
    clock = fake.FakeClock()
    decisions = ["confirmed", "rejected", "unknown"]
    judge_faults = [
        {},
        {},
        {"expired": True},
        {"other_clock": True},
        {"contradict": True},
        {"minor": 1},
        {"delay_ns": 9 * 10**9},
        {"episode_id": "r-sm.forward.0099"},
    ]
    verifier = ev.FakeGoalVerifier(
        lambda req, n: (
            {"decision": rng.choice(decisions), **rng.choice(judge_faults)}
            if rng.random() < 0.8
            else {"unavailable": rng.choice(["timeout", "busy"])}
        ),
        clock,
        "r-sm",
    )
    scene = ev.FakeSceneAssessor(
        lambda req, n: rng.choice(
            [{"decision": "ready"}] * 3
            + [
                {"decision": "reset_required"},
                {"decision": "unknown"},
                {"unavailable": "timeout"},
                {"decision": "ready", "expired": True},
            ]
        ),
        clock,
        "r-sm",
    )
    events = ev.FakeEventStream(
        {
            role: [
                {
                    "step": rng.randrange(0, 40),
                    "event_type": rng.choice(
                        ["object_settled", "gripper_open", "gripper_close"]
                    ),
                    "fault": rng.choice(
                        [None] * 6 + ["duplicate", "skip_seq", "control_key"]
                    ),
                }
                for _ in range(rng.randrange(0, 12))
            ]
            for role in ("forward", "reset")
        },
        clock,
        "r-sm",
    )
    step_faults = {
        rng.randrange(0, 400): rng.choice(
            ["http_503", "no_reply", "red_light", "stale", "frozen"]
        )
        for _ in range(rng.randrange(0, 3))
    }
    robot_kw = {
        "step_faults": step_faults,
        "home_faults": {rng.randrange(0, 6): "home_miss"}
        if rng.random() < 0.15
        else {},
    }
    policy_kw = {
        "chunk_faults": {
            rng.randrange(0, 80): rng.choice(
                [
                    "timeout",
                    "server_error",
                    "nan",
                    "wrong_dims",
                    "wrong_epoch",
                    "valid_from_early",
                ]
            )
            for _ in range(rng.randrange(0, 4))
        },
        "rng": rng,
    }
    recorder_kw = {
        "write_faults": {rng.randrange(0, 600): "disk_full"}
        if rng.random() < 0.1
        else {}
    }
    termination = TerminationConfig(
        min_steps=rng.randrange(0, 8),
        settle_steps=rng.randrange(0, 5),
        cooldown_steps=rng.randrange(1, 8),
        confirmations=rng.choice([1, 1, 2]),
        control_fraction=rng.choice([0.0, 0.0, 0.5]),
    )
    cfg = config(
        episodes=rng.randrange(1, 4),
        forward_max_steps=rng.randrange(20, 60),
        reset_max_steps=rng.randrange(5, 20),
        termination=termination,
        on_scene_unknown=rng.choice(["reset", "wait_human"]),
        max_reset_attempts=rng.choice([0, 1, 2]),
    )
    crash = None
    if rng.random() < 0.3:
        crash = crash_at(rng.choice(POINTS), rng.randrange(0, 20))
    return rng, build(
        where,
        cfg=cfg,
        clock=clock,
        verifier=verifier,
        scene=scene,
        events=events,
        robot=robot_kw,
        policy=policy_kw,
        recorder=recorder_kw,
        crash_hook=crash,
    )


def test_random_runs_keep_every_invariant(tmp_path):
    states, steps = set(), 0
    for seed in range(150):
        _rng, r = random_rig(tmp_path / f"s{seed}", seed)
        robots = []
        for attempt in range(6):
            try:
                final = r.orch.run()
                if final in ("WAIT_HUMAN", "FAULT_LOCKED"):
                    # A person deals with it: no hardware fault is left.
                    r.robot.latched, r.robot.red_light = None, False
                    r.policy.crashed = False
                    found = r.orch.resume(
                        f"c-{seed}-{attempt}",
                        expected_seq=r.orch.journal.next_seq,
                        environment_handled=True,
                        health_rechecked=True,
                    )
                    assert found.ok or found.code == "journal_corrupt"
            except fake.SimulatedCrash:
                robots.append(r.robot)
                r = restart(r)
                final = r.orch.state
                assert final in ("FAULT_LOCKED", "COMPLETED")
            states |= {e.to_state for e in r.orch.journal.events if e.to_state}
            if final == "COMPLETED":
                break
        steps += sum(robot.step_calls for robot in (*robots, r.robot))
        check_invariants(r, extra_robots=robots)
        r.orch.close()
        r.recorder.close()
    assert states >= set(sm.STATES) - {"PREFLIGHT"}
    assert steps >= 10_000


def test_a_refused_transition_still_revokes_the_motion_token(tmp_path):
    clock = fake.FakeClock()
    verifier = ev.FakeGoalVerifier([], clock, "r-sm", default={"decision": "unknown"})
    r = build(tmp_path, cfg=one_episode(), clock=clock, verifier=verifier)
    original = r.robot.step
    seen = {"n": 0, "refused": None}

    def step(action, *, token):
        seen["n"] += 1
        if seen["n"] == 3:
            with pytest.raises(sm.TransitionRefused):
                r.orch._tx("COMPLETED", "run_completed")  # illegal from FORWARD_ACTIVE
        return original(action, token=token)

    r.robot.step = step
    r.orch.run()
    assert r.robot.refused[0] == ("policy_steps", "revoked")
    assert [m[0] for m in r.robot.motions] == ["policy_steps", "policy_steps", "home"]
    assert ("FORWARD_ACTIVE", "FORWARD_STOPPING", "policy_error") in committed(r.orch)
    check_invariants(r)


def test_a_failing_recorder_abort_does_not_prevent_the_lock(tmp_path):
    r = build(tmp_path, cfg=one_episode(), recorder={"write_faults": {2: "disk_full"}})

    def broken(rollout, reason):
        raise OSError("read-only file system")

    r.recorder.abort = broken
    assert r.orch.run() == "FAULT_LOCKED"
    assert committed(r.orch)[-1][2] == "recorder_failed"
    assert "recorder_error" in {code for code, _ in r.orch.notes}
