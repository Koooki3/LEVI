"""The whole fake AERI loop end to end (T-C-13) and the fault-injection list
(T-C-14, pipeline §11.1, the 12 rows a fake can test): the orchestrator, the
recorder layer writing real rollout folders, the run manifest and session
files, an Initial State Contract, read back with the live service's own
code (levi.live.criteria, levi.live.sessions). No socket, no robot, no GPU.

Passing proves the state-machine logic on fakes, never behaviour on the
robot (fake.FIDELITY)."""

import collections
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from aeri_harness import RUN, committed, fake, results
from aeri_world import (
    FOLDERS,
    GROUP,
    READY,
    canonical,
    make_config,
    reopen,
    world,
)
from test_aeri_orchestrator import (  # noqa: F401
    StalePolicy,
    check_invariants,
    no_network,
)

from levi.automatic import metrics
from levi.automatic import state_machine as sm
from levi.automatic.adapters import events as ev
from levi.automatic.recorder import MANIFEST, sealed_episodes
from levi.live import criteria, sessions

HERE = Path(__file__).resolve().parent
CHILD = HERE / "e2e_child.py"
POINTS = (
    "before_prepared",
    "after_prepared",
    "after_execute",
    "after_acknowledged",
    "after_committed",
)


def now():
    return time.time() + 1


def rollouts(w, role):
    folder = w.root / GROUP / FOLDERS[role]
    return sorted(p for p in folder.iterdir() if p.is_dir()) if folder.is_dir() else []


def assert_disk_matches_journal(w):
    """Every rollout folder agrees with the journal: a ``demo_*`` is sealed
    complete in the journal and accepted by the live criteria; an
    ``incomplete_*`` has no marker; every result says what the disk holds."""
    events = w.orch.journal.events
    sealed = sealed_episodes(events)
    for role in ("forward", "reset"):
        for path in rollouts(w, role):
            kind = criteria.kind_of(path.name)
            if kind == "demo":
                episode = f"{RUN}.{role}.{criteria.demo_number(path.name):04d}"
                assert episode in sealed, path
                assert (path / ".complete").exists(), path
            else:
                assert kind == "incomplete" and not (path / ".complete").exists(), path
    for result in results(w.orch):
        folder = w.root / GROUP / result.rollout.task_folder / result.rollout.demo
        if result.rollout.sealed == "complete":
            assert (folder / ".complete").exists(), folder
        else:
            assert not folder.exists() or not (folder / ".complete").exists()


def session_states(w):
    sessions._CACHE.clear()
    found = sessions.read_sessions([w.root], now=now())
    return {s.task_folder: s.raw_state for s in found.values()}


# --- T-C-13: N rounds -------------------------------------------------------------------------------


def test_n_rounds_forward_home_scene_reset_next(tmp_path):
    w = world(tmp_path)
    assert w.orch.run() == "COMPLETED"
    path = [(a, b) for a, b, _ in committed(w.orch)]
    # forward -> home -> scene -> reset -> next, then a skipped reset.
    loop = [
        ("FORWARD_FINALIZE", "ROBOT_HOME"),
        ("ROBOT_HOME", "SCENE_ASSESS"),
        ("SCENE_ASSESS", "RESET_ACTIVE"),
        ("RESET_ACTIVE", "RESET_VERIFY"),
        ("RESET_VERIFY", "RESET_FINALIZE"),
        ("RESET_FINALIZE", "VERIFY_INITIAL"),
        ("VERIFY_INITIAL", "FORWARD_ACTIVE"),
    ]
    assert any(path[i : i + len(loop)] == loop for i in range(len(path)))
    assert ("SCENE_ASSESS", "FORWARD_ACTIVE") in path
    assert [p.name for p in rollouts(w, "forward")] == [
        "demo_0001",
        "demo_0002",
        "demo_0003",
    ]
    assert [p.name for p in rollouts(w, "reset")] == ["demo_0001", "demo_0002"]
    for role in ("forward", "reset"):
        for folder in rollouts(w, role):
            assert criteria.check(folder, now=now()).state == "complete", folder
    manifest = json.loads((w.r.directory / MANIFEST).read_text())
    links = {e["episode_id"]: e for e in manifest["episodes"]}
    assert links[f"{RUN}.reset.0002"]["after_forward"] == f"{RUN}.forward.0001"
    assert links[f"{RUN}.forward.0002"]["after_resets"] == [f"{RUN}.reset.0002"]
    assert links[f"{RUN}.forward.0003"]["after_resets"] == []
    assert set(session_states(w).values()) == {"finished"}
    assert_disk_matches_journal(w)
    check_invariants(w.r)
    found = metrics.report(
        w.orch.journal.events,
        manifest=manifest,
        termination=w.r.config.termination,
        max_steps=w.r.config.forward_max_steps,
    )
    assert found["autonomous"]["forward_episodes"] == 3
    assert found["reset"]["resets"] == 2 and found["reset"]["skipped"] == 3
    assert found["automation"]["interventions"] == 0


def test_a_seeded_run_replays_byte_for_byte(tmp_path):
    first = world(tmp_path / "a", seed=11)
    second = world(tmp_path / "b", seed=11)
    other = world(tmp_path / "c", seed=12)
    for w in (first, second, other):
        assert w.orch.run() == "COMPLETED"
    assert canonical(first) == canonical(second)
    assert first.r.robot.motions == second.r.robot.motions
    # The seed reaches the bytes: another seed writes other actions.
    assert canonical(first) != canonical(other)


def crash_sites(tmp_path) -> int:
    """How many crash sites a clean run has: every transaction has three
    (before and after prepared, after committed), one with an action two
    more (after execute, after acknowledged)."""
    w = world(tmp_path / "reference", cfg=make_config(episodes=2))
    assert w.orch.run() == "COMPLETED"
    prepared = [e for e in w.orch.journal.events if e.record == "prepared"]
    acting = [e for e in prepared if e.action.kind != "none"]
    w.orch.close()
    return 3 * len(prepared) + 2 * len(acting)


def test_a_crash_at_every_point_recovers_locked_without_repeating_anything(tmp_path):
    """Two forward episodes, each after a reset: forward -> home -> scene ->
    reset -> next. The orchestrator dies at every stage of every
    transaction; a new one takes over."""
    from aeri_harness import crash_at

    expected = crash_sites(tmp_path)
    tried = resumed = 0
    for point in POINTS:
        for occurrence in range(60):
            where = tmp_path / f"{point}-{occurrence}"
            w = world(
                where,
                cfg=make_config(episodes=2),
                crash_hook=crash_at(point, occurrence),
            )
            if _crash_case(w, tried):
                tried += 1
                resumed += tried % 9 == 0 and w.orch.state != "COMPLETED"
    assert tried == expected and resumed >= 5


def _crash_case(w, tried) -> bool:
    """One crash site, checked; False when the run never reached it (this
    transaction count does not exist for this point)."""
    try:
        w.orch.run()
    except fake.SimulatedCrash:
        pass
    else:
        w.orch.close()
        return False
    completed = w.orch.state == "COMPLETED"
    old = w.r.robot
    new = reopen(w)
    assert new.orch.state == ("COMPLETED" if completed else "FAULT_LOCKED")
    assert new.orch.run() in ("FAULT_LOCKED", "COMPLETED")
    assert new.r.robot.motions == []  # nothing replayed
    assert_disk_matches_journal(new)
    check_invariants(new.r, extra_robots=(old,))
    homes = collections.Counter(ep for kind, ep, _ in old.motions if kind == "home")
    assert all(n == 1 for n in homes.values()), homes
    if not completed:
        states = session_states(new)
        assert set(states.values()) == {"fault"}, states
    if not completed and (tried + 1) % 9 == 0:
        # The operator resumes: the run goes on with new episode numbers
        # and never reuses a rollout folder.
        before = {p.name for role in FOLDERS for p in rollouts(new, role)}
        assert new.orch.resume(
            "c-1",
            expected_seq=new.orch.journal.next_seq,
            environment_handled=True,
            health_rechecked=True,
        ).ok
        assert new.orch.run() in ("COMPLETED", "WAIT_HUMAN")
        assert_disk_matches_journal(new)
        check_invariants(new.r, extra_robots=(old,))
        after = {p.name for role in FOLDERS for p in rollouts(new, role)}
        assert before <= after
    new.orch.close()
    new.recorder.close()
    return True


@pytest.mark.parametrize("point", [*POINTS, "before_marker", "after_marker"])
def test_killed_by_sigkill_while_sealing(tmp_path, point):
    root = tmp_path / "world"
    proc = subprocess.run(  # noqa: PLW1510 - the return code is checked below
        [sys.executable, str(CHILD), str(root), point],
        cwd=HERE.parents[1],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PYTHONPATH": f"{HERE.parents[1]}{os.pathsep}{HERE}"},
    )
    assert proc.returncode == -signal.SIGKILL, proc.stderr[-2000:]

    class Dead:
        pass

    from aeri_world import World

    dead = World(root, Dead(), None, None)
    dead.r.directory = root / ".aeri" / "runs" / RUN
    dead.r.config = make_config(episodes=1)
    dead.r.clock = fake.FakeClock(start_ns=10**15)
    dead.r.robot = None

    class Closed:
        def close(self):
            pass

    dead.r.orch = Closed()
    dead.recorder = Closed()
    new = reopen(dead)
    assert new.orch.state == "FAULT_LOCKED"
    assert new.orch.run() == "FAULT_LOCKED" and new.r.robot.motions == []
    assert_disk_matches_journal(new)
    forward = rollouts(new, "forward")
    assert len(forward) == 1
    if point == "after_committed":
        assert forward[0].name == "demo_0001"
        assert criteria.check(forward[0], now=now()).state == "complete"
    else:
        assert forward[0].name == "incomplete_0001"
    assert sm.check_journal(new.orch.journal.events) == []
    new.orch.close()


# --- T-C-14: the fault list (pipeline §11.1, the 12 fake-testable rows) -------------------------------


def one(**over):
    return make_config(episodes=1, **over)


class CountingPolicy(fake.FakePolicy):
    def __init__(self, clock, **kw):
        super().__init__(clock, **kw)
        self.acquire_calls = 0

    def acquire(self, role, *, policy_epoch):
        self.acquire_calls += 1
        return super().acquire(role, policy_epoch=policy_epoch)


def make_row(name, tmp):
    holder = {}

    def judge(default):
        def build(clock):
            return ev.FakeGoalVerifier([], clock, RUN, default=default)

        return build

    def run(**kw):
        verifier = kw.pop("judge", None)
        w = world(tmp, cfg=kw.pop("cfg", one()), verifier=verifier, **kw)
        holder["w"] = w
        return w

    if name == "vlm_wrong_success":
        w = run(
            judge=judge({"decision": "confirmed", "contradict": True}), scene=_ready()
        )
        state = w.orch.run()
        (result,) = results(w.orch)
        assert result.stop_reason != "goal_verified"
        assert result.task_outcome != "success"
        assert w.orch.note_counts["contract_violation"] >= 1
        return w, state, "WAIT_HUMAN"
    if name == "vlm_unknown":
        w = run(judge=judge({"decision": "unknown"}), scene=_ready())
        state = w.orch.run()
        (result,) = results(w.orch)
        assert (result.stop_reason, result.task_outcome) == (
            "horizon_exhausted",
            "unknown",
        )
        return w, state, "COMPLETED"
    if name == "vlm_timeout_or_offline":
        w = run(judge=judge({"unavailable": "timeout"}), scene=_ready())
        state = w.orch.run()
        (result,) = results(w.orch)
        assert result.goal_verification == "unavailable"
        assert w.r.robot.step_calls == w.r.config.forward_max_steps  # never blocked
        return w, state, "COMPLETED"
    if name == "flapping_events":
        flaps = [
            {"step": 5 + i, "event_type": "gripper_open", "priority": "goal_candidate"}
            for i in range(30)
        ]
        w = run(
            judge=judge({"decision": "unknown"}), scene=_ready(), events=_flaps(flaps)
        )
        state = w.orch.run()
        asked = [
            q for q in w.r.verifier.submitted if not q["request_id"].endswith(":final")
        ]
        assert 1 <= len(asked) <= w.r.config.termination.max_requests
        return w, state, "COMPLETED"
    if name == "late_forward_chunk_in_reset":
        w = run(
            cfg=make_config(episodes=2),
            policy=lambda clock: StalePolicy(clock),
            scene=_script(READY, {"decision": "reset_required"}, READY, READY),
        )
        state = w.orch.run()
        assert w.r.policy.delivered_stale == 3
        assert w.orch.note_counts["chunk_dropped_epoch"] == 3
        forward = {tx for k, ep, tx in w.r.robot.motions if ".forward." in ep}
        reset = {tx for k, ep, tx in w.r.robot.motions if ".reset." in ep}
        assert reset and not forward & reset
        # Three unusable chunks in a row stop the reset as a policy error.
        assert committed(w.orch)[-1] == ("RESET_FINALIZE", "WAIT_HUMAN", "policy_error")
        return w, state, "WAIT_HUMAN"
    if name == "policy_server_crash":
        w = run(
            judge=judge({"decision": "unknown"}),
            scene=_ready(),
            policy={"chunk_faults": {2: "crash"}},
        )
        state = w.orch.run()
        assert committed(w.orch)[-1][2] == "policy_error"
        assert [p.name for p in rollouts(w, "forward")] == ["incomplete_0001"]
        return w, state, "FAULT_LOCKED"
    if name == "camera_stall":
        w = run(
            cfg=one(camera_stall_limit=3),
            scene=_ready(),
            robot={"observe_faults": {2: "camera_stall"}, "stall_frames": 100},
        )
        state = w.orch.run()
        (result,) = results(w.orch)
        assert result.goal_verification == "unavailable"
        (folder,) = rollouts(w, "forward")
        found = criteria.check(folder, now=now())
        assert found.state == "rejected" and "camera stalled" in found.reason
        return w, state, "COMPLETED"
    if name == "no_gpu_resources":
        w = run(
            scene=_ready(),
            policy=lambda clock: CountingPolicy(clock, acquire_fails=True),
        )
        state = w.orch.run()
        assert committed(w.orch)[-1] == ("VERIFY_INITIAL", "WAIT_HUMAN", "policy_error")
        assert w.r.policy.acquire_calls == 1  # never restarted again and again
        assert w.r.robot.motions == [] and rollouts(w, "forward") == []
        return w, state, "WAIT_HUMAN"
    if name == "reset_horizon":
        w = run(
            scene=_script(
                {"decision": "reset_required"}, {"decision": "reset_required"}
            ),
            events=_flaps([]),
        )
        state = w.orch.run()
        assert committed(w.orch)[-1][2] == "reset_horizon_exhausted"
        (folder,) = rollouts(w, "reset")
        assert criteria.check(folder, now=now()).state == "complete"  # kept
        return w, state, "WAIT_HUMAN"
    if name == "recorder_disk_full":
        from test_aeri_recorder import Disk

        w = run(scene=_ready(), io_hook=Disk("write", "metadata.json", 1))
        state = w.orch.run()
        assert committed(w.orch)[-1][1:] == ("FAULT_LOCKED", "recorder_failed")
        assert [p.name for p in rollouts(w, "forward")] == ["incomplete_0001"]
        return w, state, "FAULT_LOCKED"
    if name == "orchestrator_restart":
        w = run(scene=_ready(), robot={"step_faults": {5: "crash_after_send"}})
        with pytest.raises(fake.SimulatedCrash):
            w.orch.run()
        old = w.r.robot
        w = reopen(w)
        holder["w"] = w
        assert w.orch.recovery.reason == "recovery_ambiguous"
        state = w.orch.run()
        assert w.r.robot.motions == [] and len(old.motions) == 6
        assert [p.name for p in rollouts(w, "forward")] == ["incomplete_0001"]
        return w, state, "FAULT_LOCKED"
    if name == "double_resume":
        w = run(scene=_ready(), policy={"acquire_fails": True})
        assert w.orch.run() == "WAIT_HUMAN"
        w.r.policy.acquire_fails = False
        seq = w.orch.journal.next_seq
        found = [
            w.orch.resume(
                "c-1", expected_seq=seq, environment_handled=True, health_rechecked=True
            )
            for _ in range(2)
        ]
        assert found[0].ok and found[1].repeated
        assert sum(x[2] == "human_resumed" for x in committed(w.orch)) == 1
        state = w.orch.run()
        return w, state, "COMPLETED"
    raise AssertionError(name)


def _ready():
    return lambda clock: ev.FakeSceneAssessor([], clock, RUN, default=READY)


def _script(*specs):
    return lambda clock: ev.FakeSceneAssessor(list(specs), clock, RUN, default=READY)


def _flaps(items):
    return lambda clock: ev.FakeEventStream({"forward": items}, clock, RUN)


ROWS = (
    "vlm_wrong_success",
    "vlm_unknown",
    "vlm_timeout_or_offline",
    "flapping_events",
    "late_forward_chunk_in_reset",
    "policy_server_crash",
    "camera_stall",
    "no_gpu_resources",
    "reset_horizon",
    "recorder_disk_full",
    "orchestrator_restart",
    "double_resume",
)


@pytest.mark.parametrize("name", ROWS)
def test_fault_list(tmp_path, name):
    w, state, expected = make_row(name, tmp_path)
    assert state == expected, (name, committed(w.orch)[-3:])
    # No unauthorised action: every motion ran under a token of a
    # transaction that allowed it, nothing was even tried without one, no
    # physical action was prepared twice, no unverified success.
    check_invariants(w.r)
    assert [x for x in w.r.robot.refused if x[1] != "latched"] == []
    assert_disk_matches_journal(w)
    for result in results(w.orch):
        if result.task_outcome == "success":
            assert result.goal_verification == "verified"
