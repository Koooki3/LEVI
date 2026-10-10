"""Regression tests for the independent review of C3
(levi2/recon/review-C3.md): B1 a late stop at the end of a reset, the stop
left over in WAIT_HUMAN (C2 leftover), I1 stale scene observations, I2
evidence counted twice or from one view, I3 no contract, I4 a torn label
file, I5 false early stops from the control group, I6 the marker last."""

import json

import pytest
from aeri_harness import RUN, build, committed, config, fake, results
from test_aeri_orchestrator import check_invariants, no_network  # noqa: F401
from test_aeri_recorder import Disk, build_run

from levi.automatic import metrics as M
from levi.automatic import reset_manager as rm
from levi.automatic import scene_assessment as sa
from levi.automatic.adapters import events as ev
from levi.automatic.recorder import MANIFEST, RolloutRecorder
from levi.automatic.termination import TerminationConfig


def both_roles(clock):
    return ev.FakeEventStream(
        {
            "forward": [{"step": 8, "event_type": "object_settled"}],
            "reset": [{"step": 6, "event_type": "object_settled"}],
        },
        clock,
        RUN,
    )


# --- B1: a late stop at the end of a reset ----------------------------------------------------


@pytest.mark.parametrize("after", ["ready", "reset_required"])
@pytest.mark.parametrize("home", [True, False])
def test_a_late_stop_at_the_end_of_a_reset_waits_for_a_person(tmp_path, after, home):
    clock = fake.FakeClock()
    scene = ev.FakeSceneAssessor(
        [{"decision": "reset_required"}, {"decision": after}], clock, RUN
    )
    r = build(
        tmp_path,
        cfg=config(episodes=1, home_after_operator_stop=home),
        clock=clock,
        scene=scene,
        events=both_roles(clock),
    )
    original = scene.submit

    def submit(request):
        if request["target"] == "post_reset":
            r.orch.stop("s-late")  # during the reset's own scene check
        return original(request)

    scene.submit = submit
    assert r.orch.run() == "WAIT_HUMAN"  # no exception, not FAULT_LOCKED
    assert not any(b == "FAULT_LOCKED" for _, b, _ in committed(r.orch))
    (result,) = results(r.orch)
    assert result.stop_reason == "goal_verified"
    if after == "ready":
        assert (result.task_outcome, result.scene_reset) == ("success", "succeeded")
    else:
        assert (result.task_outcome, result.scene_reset) == ("failure", "failed")
    reset_homes = [m for m in r.robot.motions if m[0] == "home"]
    assert len(reset_homes) == (1 if home else 0)
    assert result.robot_home == ("succeeded" if home else "not_attempted")
    assert not any(b == "FORWARD_ACTIVE" for _, b, _ in committed(r.orch))
    # The stop took effect: the same id again changes nothing, a new one ends.
    again = r.orch.stop("s-late")
    assert again.ok and again.repeated and r.orch.state == "WAIT_HUMAN"
    assert r.orch.stop("s-end").code == "completed"
    check_invariants(r)


def test_check_after_lets_a_verified_reset_go_on_to_the_scene_check():
    s = rm.SingleResetPolicy()
    assert rm.check_after(s, "reset_verified", 1, True) == "VERIFY_INITIAL"
    assert rm.check_after(s, "scene_reset_required", 0, True) == "WAIT_HUMAN"


def test_a_stop_left_over_in_wait_human_can_still_end_the_run(tmp_path):
    """Review suggestion 1: a stop registered while the lock was busy in
    WAIT_HUMAN must not block a later stop from ending the run."""
    import threading

    r = build(tmp_path, cfg=config(episodes=1), policy={"acquire_fails": True})
    assert r.orch.run() == "WAIT_HUMAN"
    r.orch._lock.acquire()
    try:
        holder = threading.Thread(target=lambda: r.orch.stop("s-old"))
        holder.start()
        holder.join(5)
    finally:
        r.orch._lock.release()
    found = r.orch.stop("s-new")
    assert found.code == "completed" and r.orch.state == "COMPLETED"


# --- I1: stale scene observations ---------------------------------------------------------------


@pytest.mark.parametrize("before_ns", [1, 1_000_000_000])
def test_a_ready_observed_before_the_request_never_skips_a_reset(tmp_path, before_ns):
    clock = fake.FakeClock()
    scene = ev.FakeSceneAssessor(
        [
            {"decision": "ready"},  # initial check
            {"decision": "ready", "observed_before_ns": before_ns},  # after forward 1
            {"decision": "ready"},
        ],
        clock,
        RUN,
    )
    r = build(
        tmp_path,
        cfg=config(episodes=2),
        clock=clock,
        scene=scene,
        events=both_roles(clock),
    )
    r.orch.run()
    path = committed(r.orch)
    assert ("SCENE_ASSESS", "FORWARD_ACTIVE", "scene_ready") not in path
    assert ("SCENE_ASSESS", "RESET_ACTIVE", "scene_unknown") in path
    assert r.orch.note_counts["scene_dropped_stale"] == 1


# --- I2: evidence counted twice or from one view ------------------------------------------------


VIEWS = sa.contract_from(
    {
        "id": "fake-initial-state",
        "version": "1",
        "predicates": {"required": ["object_at_source", "gripper_open"]},
        "observations": {"preferred": ["cam_a", "cam_b"], "min_evidence_refs": 2},
    }
)


def scene_message(refs):
    clock = fake.FakeClock()
    provider = ev.FakeSceneAssessor([{"decision": "ready", "refs": refs}], clock, RUN)
    request = ev.make_request(
        request_id="q1",
        run_id=RUN,
        episode_id=f"{RUN}.forward.0001",
        target="initial_state",
    )
    from levi.domain import aeri

    raw = provider.collect(provider.submit(request), timeout_ns=10**10)
    return aeri.parse(raw, "scene", specs=ev.SPECS)


@pytest.mark.parametrize(
    "refs, decision",
    [
        (["cam_a:0", "cam_a:0"], "unknown"),  # one reference written twice
        (["cam_a:0", "cam_a:1"], "unknown"),  # one view only
        (["cam_a:0", "cam_b:0"], "ready"),
        (["cam_a:0", "cam_b:0", "cam_b:0"], "ready"),
        (["cam_a:0", "other:0"], "unknown"),  # not a view the task asks for
    ],
)
def test_evidence_counts_distinct_references_from_every_view(refs, decision):
    found = sa.arbitrate(scene_message(refs), VIEWS)
    assert found.decision == decision, found


# --- I3: no contract ------------------------------------------------------------------------------


def resume(r, command="c-1"):
    return r.orch.resume(
        command,
        expected_seq=r.orch.journal.next_seq,
        environment_handled=True,
        health_rechecked=True,
    )


def test_without_a_contract_single_policy_never_runs_a_useless_reset(tmp_path):
    """Fix review I-b: without a contract no reset can make a scene ready,
    so none runs: the run waits for a person, with the reason, every time."""
    clock = fake.FakeClock()
    r = build(
        tmp_path,
        cfg=config(episodes=1, initial_state=None),
        clock=clock,
        scene=ev.FakeSceneAssessor([], clock, RUN, default={"decision": "ready"}),
        events=both_roles(clock),
    )
    for n in range(3):
        assert r.orch.run() == "WAIT_HUMAN"
        assert committed(r.orch)[-1] == (
            "VERIFY_INITIAL",
            "WAIT_HUMAN",
            "scene_unknown",
        )
        assert resume(r, f"c-{n}").ok
    assert r.robot.motions == [] and r.policy.acquired == []
    assert r.orch.note_counts["scene_no_contract"] == 3


# Fix review N1: after an operator's resume, only a ready scene starts the
# forward episode in the human-assisted mode; each of these is held.
SECOND = {
    "unknown": {"decision": "unknown"},
    "contradicting": {"decision": "ready", "contradict": True},
    "other_contract": {"decision": "ready", "contract_id": "another-contract"},
    "observed_before": {"decision": "ready", "observed_before_ns": 1},
    "no_evidence": {"decision": "ready", "evidence": 0},
    "unavailable": {"unavailable": "model_error"},
    "other_episode": {"decision": "ready", "episode_id": f"{RUN}.forward.0009"},
}
CAUSE = {
    "unknown": None,
    "contradicting": "contract_violation",
    "other_contract": "contract_violation",
    "observed_before": "scene_dropped_stale",
    "no_evidence": "scene_insufficient_evidence",
    "unavailable": "scene_unavailable",
    "other_episode": "scene_dropped_stale",
}


@pytest.mark.parametrize("second", sorted(SECOND))
def test_after_a_resume_only_a_ready_scene_starts_an_episode(tmp_path, second):
    clock = fake.FakeClock()
    scene = ev.FakeSceneAssessor(
        [{"decision": "reset_required"}, SECOND[second]],
        clock,
        RUN,
        default={"decision": "ready"},
    )
    r = build(
        tmp_path,
        cfg=config(episodes=1, reset_strategy="human_assisted"),
        clock=clock,
        scene=scene,
    )
    assert r.orch.run() == "WAIT_HUMAN"
    assert resume(r).ok
    assert r.orch.run() == "WAIT_HUMAN"
    last = committed(r.orch)[-1]
    assert last == ("VERIFY_INITIAL", "WAIT_HUMAN", "scene_unknown"), last
    assert r.robot.motions == [] and r.policy.acquired == []
    if CAUSE[second]:
        assert r.orch.note_counts[CAUSE[second]] >= 1, r.orch.note_counts
    reasons = {e.reason for e in r.orch.journal.events if e.reason}
    assert "operator_confirmed_scene" not in reasons
    # A third check that is ready starts it.
    assert resume(r, "c-2").ok
    assert r.orch.run() == "COMPLETED"
    check_invariants(r)


def test_the_reset_interface_does_not_depend_on_single_policy():
    for strategy in (rm.SingleResetPolicy(), rm.HumanAssistedReset()):
        assert rm.check_plan(strategy, "ready", 0).action == "forward"
        for decision in ("unknown", "unavailable", "reset_required"):
            assert rm.check_plan(rm.HumanAssistedReset(), decision, 0).action == (
                "wait_human"
            )


def test_operator_confirmed_scene_is_not_a_transition_reason():
    from levi.domain import aeri

    assert "operator_confirmed_scene" not in aeri.TRANSITION_REASONS


# --- I4: a torn label file -----------------------------------------------------------------------


def test_a_torn_label_line_never_swallows_the_next_label(tmp_path):
    store = M.LabelStore(tmp_path)
    store.add("operator_label", f"{RUN}.forward.0001", "success", by="op-1")
    path = store.path("operator_label")
    with path.open("ab") as handle:
        handle.write(b'{"kind": "operator_label", "episode_id": "r-sm.forw')
    store.add("operator_label", f"{RUN}.forward.0002", "failure", by="op-1")
    assert store.latest("operator_label") == {
        f"{RUN}.forward.0001": "success",
        f"{RUN}.forward.0002": "failure",
    }
    with pytest.raises(M.LabelRefused):
        store.add("operator_label", f"{RUN}.forward.0002", "success", by="op-1")
    torn = list((tmp_path / "labels" / "torn").iterdir())
    assert len(torn) == 1 and torn[0].read_bytes().endswith(b"forw")


def test_a_bad_line_before_the_last_is_refused_not_skipped(tmp_path):
    store = M.LabelStore(tmp_path)
    store.add("operator_label", f"{RUN}.forward.0001", "success", by="op-1")
    path = store.path("operator_label")
    path.write_bytes(b"garbage\n" + path.read_bytes())
    with pytest.raises(M.LabelRefused):
        store.lines("operator_label")


# --- I5: false early stops from the control group ---------------------------------------------------


def ctl(n, *, would_stop, outcome="success"):
    return M.EpisodeRecord(
        episode_id=f"{RUN}.forward.{n:04d}",
        role="forward",
        task_outcome=outcome,
        stop_reason="horizon_exhausted",
        goal_verification="verified" if outcome == "success" else "contradicted",
        scene_reset="unknown",
        sealed="complete",
        steps=40,
        control=True,
        would_stop_step=12 if would_stop else None,
    )


def test_the_false_early_stop_rate_comes_from_the_control_group():
    records = [
        ctl(1, would_stop=True),  # would have stopped, truly a success
        ctl(2, would_stop=True),  # would have stopped, truly a failure: false stop
        ctl(3, would_stop=False),  # truly a failure, would not have stopped
        ctl(4, would_stop=False),
        ctl(5, would_stop=True),  # truly a failure: false stop
        ctl(6, would_stop=False),  # no truth label
    ]
    truth = {
        f"{RUN}.forward.0001": "success",
        f"{RUN}.forward.0002": "failure",
        f"{RUN}.forward.0003": "failure",
        f"{RUN}.forward.0004": "failure",
        f"{RUN}.forward.0005": "failure",
    }
    found = M.early_termination(records, truth)
    # 2 false stops among 4 truly failed control episodes: 0.5 [0.15, 0.85].
    assert found["false_early_stop_rate"] == {
        "available": True,
        "source": "control",
        "n": 2,
        "of": 4,
        "rate": 0.5,
        "wilson95": [0.15, 0.85],
        "left_out": {"cut_short": 0, "not_recorded": 0},
    }
    # Of 3 stops the detector would have made, 2 were false: 2/3.
    assert found["control"]["would_stop_false"]["n"] == 2
    assert found["control"]["would_stop_false"]["of"] == 3


def test_without_a_control_group_the_false_early_stop_rate_is_unavailable():
    records = [
        M.EpisodeRecord(
            f"{RUN}.forward.000{n}",
            "forward",
            "success",
            "goal_verified" if n < 3 else "horizon_exhausted",
            "verified",
            "unknown",
            "complete",
            steps=10,
        )
        for n in range(1, 6)
    ]
    truth = {
        r.episode_id: ("failure" if n in (1, 3, 4) else "success")
        for n, r in enumerate(records)
    }
    found = M.early_termination(records, truth)
    assert found["false_early_stop_rate"]["available"] is False
    assert found["false_early_stop_rate"]["rate"] is None
    # The treatment group's own figure is a lower bound, named so.
    bound = found["treatment_false_early_stop_lower_bound"]
    tp, fp, tn = (found["confusion"][k] for k in ("tp", "fp", "tn"))
    assert tp != tn and bound["n"] == fp and bound["of"] == fp + tn


def test_recall_misses_only_episodes_the_detector_could_have_stopped():
    records = [
        M.EpisodeRecord(
            f"{RUN}.forward.0001",
            "forward",
            "unknown",
            "operator_stop",
            "unavailable",
            "unknown",
            "complete",
        ),
        M.EpisodeRecord(
            f"{RUN}.forward.0002",
            "forward",
            "unknown",
            "horizon_exhausted",
            "undecided",
            "unknown",
            "complete",
        ),
    ]
    truth = {r.episode_id: "success" for r in records}
    found = M.early_termination(records, truth)
    assert found["confusion"]["fn"] == 1


def test_control_episodes_record_where_they_would_have_stopped(tmp_path):
    term = TerminationConfig(
        min_steps=5, settle_steps=3, cooldown_steps=5, control_fraction=1.0
    )
    from aeri_world import FOLDERS

    cfg = config(
        episodes=1,
        termination=term,
        forward_folder=FOLDERS["forward"],
        reset_folder=FOLDERS["reset"],
    )
    r = build_run(tmp_path, cfg=cfg)
    assert r.orch.run() == "COMPLETED"
    manifest = json.loads((r.directory / MANIFEST).read_text())
    (entry,) = manifest["episodes"]
    assert entry["control"] is True and entry["would_stop_step"] == 8
    (record,) = M.episodes(r.orch.journal.events, manifest=manifest)
    assert record.control and record.would_stop_step == 8


# --- I6: the marker is created last -----------------------------------------------------------------


def test_the_complete_marker_is_the_last_write_of_a_seal(tmp_path):
    ops = []
    rec = RolloutRecorder(
        tmp_path,
        run_id=RUN,
        run_dir=tmp_path / "run",
        group="g",
        io_hook=lambda op, path: ops.append((op, path.name)),
    )
    rollout = rec.open(task_folder="f", episode_id=f"{RUN}.forward.0001", number=1)
    rec.stage(rollout, {"pose": [0.0]})
    rec.commit(rollout, fake.StepResult("yes", "ok", 0))
    del ops[:]
    rec.seal(rollout, {})
    names = [name for _, name in ops if name != "manifest.json"]
    assert names[-1] == ".complete", names
    first = names.index(".complete")  # the marker's own first operation
    assert names.index("events.csv") < first and names.index("metadata.json") < first
    assert set(names[first:]) == {".complete"}, names
    # Only the manifest (a derived view) follows the marker.
    assert all(
        name == "manifest.json"
        for _, name in ops[ops.index(("marker", ".complete")) + 2 :]
    )


def test_a_failure_between_metadata_and_marker_leaves_no_marker(tmp_path):
    r = build_run(tmp_path, io_hook=Disk("marker", ".complete", 0))
    assert r.orch.run() == "FAULT_LOCKED"
    (result,) = results(r.orch)
    assert result.rollout.sealed == "incomplete"


def test_control_failures_count_only_episodes_the_detector_could_have_stopped():
    """Fix review I-c: the denominator holds control episodes that ran to
    their end, were sealed and carry the control record; the rest are
    counted apart by reason."""
    records = [
        ctl(1, would_stop=True, outcome="failure"),  # counts: a false stop
        ctl(2, would_stop=False, outcome="failure"),  # counts
        M.EpisodeRecord(  # stopped by the operator: no chance to stop
            f"{RUN}.forward.0003",
            "forward",
            "unknown",
            "operator_stop",
            "unavailable",
            "unknown",
            "complete",
            control=True,
        ),
        M.EpisodeRecord(  # a fault, never sealed
            f"{RUN}.forward.0004",
            "forward",
            "unknown",
            "policy_error",
            "unavailable",
            "unknown",
            "incomplete",
            control=True,
        ),
        M.EpisodeRecord(  # ran out, but its seal (and control record) failed
            f"{RUN}.forward.0005",
            "forward",
            "unknown",
            "horizon_exhausted",
            "unavailable",
            "unknown",
            "incomplete",
            control=True,
            control_recorded=False,
        ),
    ]
    truth = {r.episode_id: "failure" for r in records}
    found = M.early_termination(records, truth)
    rate = found["false_early_stop_rate"]
    assert (rate["n"], rate["of"], rate["rate"]) == (1, 2, 0.5)
    assert rate["left_out"] == {"cut_short": 2, "not_recorded": 1}
