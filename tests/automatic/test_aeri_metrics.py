"""Metrics of an automatic run (T-C-11, levi/automatic/metrics.py): values
checked against numbers worked out by hand, the four label kinds kept
apart, and the metrics of real (fake-driven) run journals."""

import json

import pytest
from aeri_harness import RUN, config, fake
from test_aeri_orchestrator import no_network  # noqa: F401
from test_aeri_recorder import FOLDERS, build_run, reset_first

from levi.automatic import metrics as M
from levi.automatic.recorder import MANIFEST
from levi.automatic.termination import TerminationConfig


def rec(n, *, early, steps=None, outcome="success", role="forward", control=False):
    return M.EpisodeRecord(
        episode_id=f"{RUN}.{role}.{n:04d}",
        role=role,
        task_outcome=outcome,
        stop_reason="goal_verified" if early else "horizon_exhausted",
        goal_verification="verified" if outcome == "success" else "contradicted",
        scene_reset="unknown",
        sealed="complete",
        steps=steps,
        control=control,
    )


def ep(n, role="forward"):
    return f"{RUN}.{role}.{n:04d}"


# --- hand-computed values ---------------------------------------------------------------------


def test_early_termination_against_hand_computed_values():
    records = [
        rec(1, early=True, steps=10),
        rec(2, early=True, steps=12),
        rec(3, early=True, steps=15),
        rec(4, early=True, steps=20),  # truly a failure: a false early stop
        rec(5, early=False, steps=40, outcome="failure"),
        rec(6, early=False, steps=40, outcome="failure"),
        rec(7, early=False, steps=40, outcome="unknown"),
        rec(8, early=False, steps=40, outcome="failure"),
        rec(9, early=False, steps=40, outcome="failure"),
        rec(10, early=False, steps=40, outcome="failure"),  # no truth label
        rec(11, early=False, steps=40, control=True),
    ]
    truth = {
        ep(1): "success",
        ep(2): "success",
        ep(3): "success",
        ep(4): "failure",
        ep(5): "success",
        ep(6): "success",
        ep(7): "failure",
        ep(8): "failure",
        ep(9): "success",  # tp != tn, so a swapped denominator shows
        ep(11): "success",
    }
    found = M.early_termination(records, truth, max_steps=40)
    assert found["confusion"] == {"tp": 3, "fp": 1, "fn": 3, "tn": 2}
    # 3/4, Wilson 95 % [0.301, 0.954]
    assert found["precision"] == {
        "n": 3,
        "of": 4,
        "rate": 0.75,
        "wilson95": [0.301, 0.954],
    }
    # 3/6, [0.188, 0.812]
    assert found["recall"] == {"n": 3, "of": 6, "rate": 0.5, "wilson95": [0.188, 0.812]}
    # The treatment group's figure is only a lower bound: 1 of 3 truly
    # failed episodes stopped early, [0.061, 0.792].
    assert found["treatment_false_early_stop_lower_bound"] == {
        "n": 1,
        "of": 3,
        "rate": 0.333,
        "wilson95": [0.061, 0.792],
    }
    # The control episode truly succeeded: no control failure, no rate.
    assert found["false_early_stop_rate"]["available"] is False
    assert found["false_early_stop_rate"]["rate"] is None
    # (40-10) + (40-12) + (40-15) + (40-20) = 103 over 4 episodes
    assert found["saved_steps"] == {"total": 103, "mean": 25.75, "episodes": 4}
    assert found["unlabeled"] == 1 and found["labelled"] == 9
    assert found["control"]["episodes"] == 1
    assert found["control"]["true_success"]["rate"] == 1.0
    # Verdicts vs truth (unknown left out): 1,2,3 agree; 4 false success;
    # 5,6,9 disagree; 8 agrees; 11 agrees -> 5 of 9.
    assert (
        found["verdict_agreement"]["n"] == 5 and found["verdict_agreement"]["of"] == 9
    )
    assert found["false_success"] == 1 and found["unknown_verdicts"] == 1


def test_small_samples_always_carry_an_interval_and_empty_ones_none():
    one = M.share(0, 3)
    assert one["rate"] == 0.0 and one["wilson95"] == [0.0, 0.562]
    assert M.share(0, 0) == {"n": 0, "of": 0, "rate": None, "wilson95": None}
    found = M.early_termination([], {})
    assert (
        found["precision"]["wilson95"] is None and found["saved_steps"]["total"] is None
    )


def test_unknown_stays_in_the_autonomous_denominator():
    records = [
        rec(1, early=True),
        rec(2, early=False, outcome="unknown"),
        rec(3, early=False, outcome="failure"),
        rec(1, early=False, role="reset"),
    ]
    found = M.autonomous(records)
    assert found["forward_episodes"] == 3
    assert found["autonomous_success_rate"]["of"] == 3
    assert found["outcomes"] == {"success": 1, "failure": 1, "unknown": 1}
    assert found["label_kind"] == "autonomous_verdict"


def test_reset_skip_accuracy_against_hand_computed_values():
    decisions = [
        (ep(1, "reset"), False),  # truly needed: right
        (ep(1), True),  # truly ready: right
        (ep(2), True),  # truly needed a reset: a wrong skip
        (ep(2, "reset"), False),  # truly ready: an unneeded reset
        (ep(3), True),  # not labelled
    ]
    truth = {
        ep(1, "reset"): "reset_required",
        ep(1): "ready",
        ep(2): "reset_required",
        ep(2, "reset"): "ready",
    }
    records = [
        M.EpisodeRecord(
            ep(1, "reset"),
            "reset",
            "success",
            "goal_verified",
            "verified",
            "succeeded",
            "complete",
        ),
        M.EpisodeRecord(
            ep(2, "reset"),
            "reset",
            "failure",
            "horizon_exhausted",
            "contradicted",
            "failed",
            "complete",
        ),
    ]
    found = M.reset(records, decisions, truth)
    assert found["skip_accuracy"] == {
        "n": 2,
        "of": 4,
        "rate": 0.5,
        "wilson95": [0.15, 0.85],
    }
    assert found["wrong_skip_rate"] == {
        "n": 1,
        "of": 2,
        "rate": 0.5,
        "wilson95": [0.095, 0.905],
    }
    assert (
        found["unneeded_reset_rate"]["n"] == 1
        and found["unneeded_reset_rate"]["of"] == 2
    )
    assert found["autonomous_reset_success_rate"]["rate"] == 0.5
    assert found["skipped"] == 3 and found["decisions"] == 5


# --- the four label kinds --------------------------------------------------------------------------


def test_label_kinds_are_kept_apart_and_never_overwritten(tmp_path):
    store = M.LabelStore(tmp_path)
    with pytest.raises(M.LabelRefused):
        store.add("autonomous_verdict", ep(1), "success", by="op-1")
    with pytest.raises(M.LabelRefused):
        store.add("operator_label", ep(1), "success", by="someone@example.org")
    with pytest.raises(M.LabelRefused):
        store.add("operator_label", ep(1), "maybe", by="op-1")
    store.add("operator_label", ep(1), "success", by="op-1")
    adjudicated = store.path("adjudicated_ground_truth")
    assert not adjudicated.exists()
    with pytest.raises(M.LabelRefused):
        store.add("operator_label", ep(1), "failure", by="op-1")
    store.add("adjudicated_ground_truth", ep(1), "failure", by="panel-1")
    before = store.path("operator_label").read_bytes()
    store.add("posthoc_verdict", ep(1), "success", by="levi-live")
    assert store.path("operator_label").read_bytes() == before
    # A correction is appended; the first label stays on file.
    store.add("operator_label", ep(1), "failure", by="op-1", supersede=True)
    lines = store.lines("operator_label")
    assert [x["value"] for x in lines] == ["success", "failure"]
    assert lines[1]["supersedes"] == 1
    assert store.truth() == {ep(1): "failure"}
    store.add("operator_label", ep(2), "success", by="op-1")
    assert store.truth() == {ep(1): "failure", ep(2): "success"}
    assert store.truth(rule="adjudicated") == {ep(1): "failure"}
    store.add("operator_label", ep(2), "ready", subject="initial_state", by="op-1")
    assert store.truth("initial_state") == {ep(2): "ready"}
    for kind in M.STORED_KINDS:
        for line in store.lines(kind):
            assert line["kind"] == kind


# --- real run journals ----------------------------------------------------------------------------


@pytest.mark.mode_matrix(
    "metrics:autonomous",
    "metrics:early_termination",
    "metrics:reset",
    "metrics:automation",
    modes=("single_reset_policy",),
)
def test_metrics_of_a_run_with_a_reset_and_two_early_stops(tmp_path):
    clock = fake.FakeClock()
    cfg = config(
        episodes=2, forward_folder=FOLDERS["forward"], reset_folder=FOLDERS["reset"]
    )
    r = build_run(tmp_path, clock=clock, cfg=cfg, scene=reset_first(clock))
    assert r.orch.run() == "COMPLETED"
    labels = M.LabelStore(r.directory)
    labels.add("operator_label", ep(1), "success", by="op-1")
    labels.add("operator_label", ep(2), "failure", by="op-1")
    labels.add(
        "operator_label",
        ep(1, "reset"),
        "reset_required",
        subject="initial_state",
        by="op-1",
    )
    labels.add("operator_label", ep(1), "ready", subject="initial_state", by="op-1")
    manifest = json.loads((r.directory / MANIFEST).read_text())
    found = M.report(
        r.orch.journal.events,
        labels=labels,
        manifest=manifest,
        termination=cfg.termination,
        max_steps=cfg.forward_max_steps,
    )
    auto = found["autonomous"]
    assert auto["forward_episodes"] == 2 and auto["outcomes"]["success"] == 2
    et = found["early_termination"]
    assert et["early_stops"] == 2 and et["confusion"] == {
        "tp": 1,
        "fp": 1,
        "fn": 0,
        "tn": 0,
    }
    steps = {e["episode_id"]: e["steps"] for e in manifest["episodes"]}
    assert et["saved_steps"]["total"] == sum(
        cfg.forward_max_steps - steps[ep(n)] for n in (1, 2)
    )
    assert et["false_success"] == 1
    rs = found["reset"]
    assert rs["resets"] == 1 and rs["decisions"] == 3 and rs["skipped"] == 2
    assert rs["skip_accuracy"]["n"] == 2 and rs["skip_accuracy"]["of"] == 2
    assert rs["reset_duration_ms"]["n"] == 1 and rs["reset_duration_ms"]["p50"] > 0
    assert found["automation"]["interventions"] == 0
    assert found["automation"]["longest_run_without_intervention"] == 2
    assert "not ground truth" in found["note"]
    json.dumps(found)  # serialisable as it is


@pytest.mark.mode_matrix("metrics:automation", modes=("single_reset_policy",))
def test_interventions_and_the_human_wait_are_counted(tmp_path):
    cfg = config(
        episodes=1, forward_folder=FOLDERS["forward"], reset_folder=FOLDERS["reset"]
    )
    r = build_run(tmp_path, cfg=cfg, policy={"acquire_fails": True})
    assert r.orch.run() == "WAIT_HUMAN"
    r.clock.advance(7_000_000_000)
    r.policy.acquire_fails = False
    assert r.orch.resume(
        "c-1",
        expected_seq=r.orch.journal.next_seq,
        environment_handled=True,
        health_rechecked=True,
    ).ok
    assert r.orch.run() == "COMPLETED"
    found = M.automation(r.orch.journal.events)
    assert found["interventions"] == 1 and found["by_reason"] == {"policy_error": 1}
    assert found["resumes"] == 1 and found["human_wait_ms"] == 7_000
    assert found["longest_run_without_intervention"] == 1


def test_control_episodes_are_reported_apart(tmp_path):
    term = TerminationConfig(
        min_steps=5, settle_steps=3, cooldown_steps=5, control_fraction=1.0
    )
    cfg = config(
        episodes=1,
        termination=term,
        forward_folder=FOLDERS["forward"],
        reset_folder=FOLDERS["reset"],
    )
    r = build_run(tmp_path, cfg=cfg)
    assert r.orch.run() == "COMPLETED"
    records = M.episodes(r.orch.journal.events, termination=term)
    assert [x.control for x in records] == [True]
    found = M.early_termination(records, {ep(1): "success"})
    assert found["early_stops"] == 0 and found["control"]["labelled"] == 1
    assert found["recall"]["of"] == 0
