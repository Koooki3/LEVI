"""The causal trigger: prefix causality, every reason, recorded delays."""

import copy
import dataclasses
import math

import numpy as np
import pytest

from levi.events.causal import REASONS, Decision, Trigger, TriggerConfig
from levi.events.signal_profiles import ChannelSpec, SignalProfile
from levi.events.stream import CausalSignals, Sample, StreamConfig

STEP_NS = 100_000_000


def make(*, height=0.045, command=False, **config):
    channels = [
        ChannelSpec(
            role="position",
            feature="s",
            index=i,
            axis=axis,
            units="m",
            frame="base",
            kind="measured",
        )
        for i, axis in enumerate("xyz")
    ]
    channels.append(
        ChannelSpec(
            role="gripper",
            feature="s",
            index=6,
            units="m",
            open_level="high",
            valid_range=(0.0, 0.08),
        )
    )
    if command:
        channels.append(
            ChannelSpec(
                role="gripper",
                feature="cmd",
                index=0,
                units="binary",
                kind="commanded",
                open_level="high",
                valid_range=(0.0, 1.0),
            )
        )
    signals = CausalSignals(
        SignalProfile(channels=channels),
        config=StreamConfig(still_ns=500_000_000),
        height_turn_m=height,
    )
    return Trigger(signals, TriggerConfig(**config))


def feed(trigger, step, row):
    values = {("s", 0): row["x"], ("s", 1): 0.0, ("s", 2): row["z"]}
    values[("s", 6)] = row["w"]
    if "cmd" in row:
        values[("cmd", 0)] = row["cmd"]
    ns = (step + 1) * STEP_NS
    return trigger.feed(
        Sample(step, ns, values),
        now_ns=ns,
        action_chunk=row.get("chunk"),
        subtask=row.get("subtask"),
        goal_distance_m=row.get("goal"),
    )


def run(trigger, rows):
    return [feed(trigger, step, row) for step, row in enumerate(rows)]


def pick_and_place():
    """Descend, close, lift, stop, open, stay still (10 Hz)."""
    rows = []
    for k in range(10):  # descend 0.28 -> 0.10 m
        rows.append({"x": 0.0, "z": round(0.28 - 0.02 * k, 6), "w": 0.08})
    for k in range(10, 15):  # still; the gripper closes at row 12
        rows.append({"x": 0.0, "z": 0.10, "w": 0.08 if k < 11 else 0.05})
    for k in range(12, 15):
        rows[k]["w"] = 0.01
    for k in range(15, 35):  # lift 1 cm per row
        rows.append({"x": 0.0, "z": round(0.10 + 0.01 * (k - 14), 6), "w": 0.01})
    for k in range(35, 90):  # still; the gripper opens at row 37
        rows.append({"x": 0.0, "z": rows[34]["z"], "w": 0.01 if k < 37 else 0.08})
    return rows


def looks(decisions):
    return [d for d in decisions if d.look]


def test_prefix_decisions_never_depend_on_later_steps():
    rows = pick_and_place()
    full = [d.as_dict() for d in run(make(), rows)]
    wild = {"x": 5.0, "z": -3.0, "w": float("nan"), "chunk": [[9.0]]}
    for k in range(1, len(rows)):
        prefix = run(make(), rows[:k] + [dict(wild)] * 5)
        assert [d.as_dict() for d in prefix[:k]] == full[:k], k


def test_pick_and_place_reasons_cues_and_delays():
    found = looks(run(make(), pick_and_place()))
    assert [(d.reason, d.cue) for d in found] == [
        ("grasp", "gripper_close"),
        ("stall", "motion_stop"),
        ("grasp", "height_low"),
        ("release", "gripper_open"),
        ("stall", "motion_stop"),
        ("maybe_done", "released_and_still"),
        ("stall", "no_progress"),
    ]
    assert all(d.reason in REASONS and d.probability is None for d in found)
    grasp, stop, lift, release, stop2, done, stall = found
    assert grasp.time_s == pytest.approx(1.2) and grasp.delay_s == 0
    # A stop is known only once the arm has been still for still_ns.
    assert stop.event_time_s == pytest.approx(0.9)
    assert stop.delay_s == pytest.approx(0.5)
    # The lift after the grasp: the low is at row 9, known at row 19.
    assert lift.event_time_s == pytest.approx(0.9)
    assert lift.time_s == pytest.approx(1.9)
    assert release.time_s == pytest.approx(3.7)
    assert stop2.event_time_s == pytest.approx(3.4)
    assert stop2.time_s == pytest.approx(3.9)
    assert done.event_time_s == pytest.approx(3.7)
    assert 2.0 - 1e-9 <= done.delay_s < 2.2
    assert stall.event_time_s == pytest.approx(3.7)
    assert 3.0 - 1e-9 <= stall.delay_s < 3.2
    assert all(d.delay_s >= 0 for d in found)
    assert all(
        math.isclose(d.delay_s, d.time_s - d.event_time_s, abs_tol=1e-9) for d in found
    )


def test_height_low_while_open_is_a_release():
    rows = [{"x": 0.0, "z": round(0.28 - 0.02 * k, 6), "w": 0.08} for k in range(10)]
    rows += [
        {"x": 0.0, "z": round(0.10 + 0.01 * k, 6), "w": 0.08} for k in range(1, 10)
    ]
    lifts = [d for d in looks(run(make(), rows)) if d.cue == "height_low"]
    assert [d.reason for d in lifts] == ["release"]


def test_long_still_without_release_is_a_stall_not_done():
    rows = [{"x": 0.0, "z": 0.1, "w": 0.08}] * 60
    found = looks(run(make(), rows))
    assert [d.cue for d in found] == ["motion_stop", "no_progress"]
    assert "maybe_done" not in {d.reason for d in found}


def test_command_and_measured_crossings_fold_into_one_grasp():
    rows = []
    for k in range(20):
        rows.append(
            {
                "x": 0.01 * k,
                "z": 0.1,
                "w": 0.08 if k < 12 else 0.01,
                "cmd": 1.0 if k < 11 else 0.0,
            }
        )
    grasps = [d for d in looks(run(make(command=True), rows)) if d.reason == "grasp"]
    assert len(grasps) == 1
    assert grasps[0].time_s == pytest.approx(1.1)
    # The measured crossing one step later is still a cue, only suppressed.
    later = run(make(command=True), rows)[12]
    assert not later.look and [c.cue for c in later.cues] == ["gripper_close"]


def test_missing_signal_is_one_anomaly_per_run():
    rows = [{"x": 0.01 * k, "z": 0.1, "w": 0.08} for k in range(20)]
    for k in (5, 6, 7):
        rows[k] = dict(rows[k], w=float("nan"))
    anomalies = [d for d in looks(run(make(), rows)) if d.reason == "anomaly"]
    assert len(anomalies) == 1
    assert anomalies[0].step == 5 and anomalies[0].cue.startswith("missing_signal")


def test_disagreeing_action_chunks_are_an_anomaly():
    rows = [{"x": 0.01 * k, "z": 0.1, "w": 0.08} for k in range(3)]
    rows[0]["chunk"] = [[0.0], [1.0], [2.0]]
    rows[1]["chunk"] = [[1.0], [2.0], [3.0]]  # the same plan, one step on
    rows[2]["chunk"] = [[5.0], [5.0], [5.0]]
    found = looks(run(make(chunk_disagreement=0.1), rows))
    assert [(d.step, d.reason, d.cue) for d in found] == [
        (2, "anomaly", "chunk_disagreement")
    ]
    assert not looks(run(make(), rows))  # off unless configured


def test_no_progress_towards_the_goal_is_a_stall():
    rows = [
        {"x": 0.01 * k, "z": 0.1, "w": 0.08, "goal": max(0.5 - 0.05 * k, 0.3)}
        for k in range(50)
    ]
    found = [d for d in looks(run(make(), rows)) if d.cue == "no_goal_progress"]
    assert len(found) == 1
    # Last improvement at row 4 (0.30 m); three seconds later.
    assert found[0].event_time_s == pytest.approx(0.4)
    assert 3.4 - 1e-9 <= found[0].time_s <= 3.5 + 1e-9


def test_a_new_subtask_restarts_the_stall_clock():
    rows = [{"x": 0.0, "z": 0.1, "w": 0.08, "subtask": "a"} for _ in range(60)]
    for k in range(25, 60):
        rows[k]["subtask"] = "b"
    stalls = [d for d in looks(run(make(), rows)) if d.cue == "no_progress"]
    assert [d.subtask for d in stalls] == ["b"]
    assert stalls[0].time_s == pytest.approx(5.5)


def test_min_score_hides_low_priority_looks():
    found = looks(run(make(min_score=0.85), pick_and_place()))
    assert {d.reason for d in found} == {"grasp", "release"}
    assert {d.cue for d in found} == {"gripper_close", "gripper_open"}


def test_rejected_inputs_change_nothing():
    rows = pick_and_place()[:20]
    clean = [d.as_dict() for d in run(make(), rows)]
    trigger = make()
    out = []
    for step, row in enumerate(rows):
        if step == 8:
            with pytest.raises(ValueError):
                trigger.feed(
                    Sample(step, (step + 1) * STEP_NS, {}), now_ns=step * STEP_NS
                )
            with pytest.raises(ValueError):
                feed(trigger, step, dict(row, chunk=[[float("nan")]]))
            with pytest.raises(ValueError):
                feed(trigger, step, dict(row, goal=-1.0))
        out.append(feed(trigger, step, row).as_dict())
    assert out == clean


@pytest.mark.parametrize(
    "bad",
    [
        {"stall_seconds": 0},
        {"done_seconds": float("inf")},
        {"refractory_seconds": -1},
        {"min_score": 1.5},
        {"chunk_disagreement": 0},
        {"goal_progress_m": True},
    ],
)
def test_config_is_validated(bad):
    with pytest.raises(ValueError):
        TriggerConfig(**bad)


def test_trigger_needs_a_causal_stream():
    with pytest.raises(TypeError):
        Trigger(object())


def test_no_decision_has_a_probability_before_calibration():
    rows = pick_and_place()
    rows[40] = dict(rows[40], w=float("nan"))
    for config in ({}, {"min_score": 0.85}):
        for d in run(make(**config), rows):
            assert d.probability is None and d.as_dict()["probability"] is None


DECISION_FIELDS = {
    "look",
    "reason",
    "score",
    "step",
    "time_s",
    "event_time_s",
    "delay_s",
    "cue",
    "probability",
    "subtask",
    "cues",
}


def test_a_decision_carries_no_action():
    """A decision can only ask to pause and look: it has no field that could
    hold an action and it cannot be edited once made. Adding a field is a
    decision for this test, and for the safety review, to make."""
    names = {f.name for f in dataclasses.fields(Decision)}
    assert names == DECISION_FIELDS
    assert not [n for n in names if "action" in n]
    d = next(d for d in run(make(), pick_and_place()) if d.look)
    assert set(d.as_dict()) == DECISION_FIELDS - {"cues"}
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.look = False


def test_feed_never_changes_the_action_chunk():
    """The chunk is the policy's plan, read to compare consecutive plans: a
    deep copy taken before ``feed`` equals the caller's object after it, in
    values, dtype and writability, whatever the container."""
    base = np.arange(20, dtype=float).reshape(10, 2) / 20
    readonly = base + 0.3
    readonly.flags.writeable = False
    chunks = [
        base.copy(),
        readonly,
        (base + 0.6).tolist(),
        tuple(tuple(row) for row in (base + 0.9).tolist()),
        np.arange(20).reshape(10, 2),
        np.asfortranarray(base),
    ]
    trigger = make(chunk_disagreement=0.1)
    cues = []
    for step, chunk in enumerate(chunks):
        before = copy.deepcopy(chunk)
        writeable = chunk.flags.writeable if isinstance(chunk, np.ndarray) else None
        d = feed(trigger, step, {"x": 0.0, "z": 0.2, "w": 0.08, "chunk": chunk})
        cues += [c.cue for c in d.cues]
        if isinstance(chunk, np.ndarray):
            assert chunk.dtype == before.dtype, step
            assert chunk.flags.writeable == writeable, step
            assert np.array_equal(chunk, before), step
        else:
            assert chunk == before, step
    # The chunks were compared, not skipped.
    assert "chunk_disagreement" in cues


@pytest.mark.parametrize("shape", [(10, 1), (10,)], ids=["2d", "1d"])
def test_a_reused_action_buffer_still_yields_chunk_disagreement(shape):
    """A policy may write every plan into one buffer, in place. The trigger
    keeps the previous plan to compare it with the next one, so it must hold
    its own copy: with one reused buffer the decisions equal those with a
    fresh array per step, and the plan going from all 0 to all 1 (and back)
    is reported at the steps where it changed."""
    levels = [0.0, 1.0, 1.0, 0.0]

    def decisions(reuse):
        trigger = make(chunk_disagreement=0.1)
        buffer = np.zeros(shape)
        out = []
        for step, level in enumerate(levels):
            buffer[...] = level
            chunk = buffer if reuse else buffer.copy()
            row = {"x": 0.0, "z": 0.2, "w": 0.08, "chunk": chunk}
            out.append(feed(trigger, step, row))
        return out

    reused, fresh = decisions(reuse=True), decisions(reuse=False)
    assert [[c.cue for c in d.cues] for d in reused] == [
        [],
        ["chunk_disagreement"],
        [],
        ["chunk_disagreement"],
    ]
    assert [d.cues for d in reused] == [d.cues for d in fresh]
    assert [d.as_dict() for d in reused] == [d.as_dict() for d in fresh]
