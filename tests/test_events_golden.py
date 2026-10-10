"""Golden regression for the robot-signal readers before they share a kernel.

``signals.summarize`` (the signal lines an external agent reads beside the
pictures) and ``anchored.crossings`` / ``anchored.view_rows`` (where an
anchored review looks) are about to be rebuilt on one gripper-crossing kernel
(``levi/events``). Two things are pinned here:

* a seeded corpus of clean, evenly sampled episodes whose every output is
  recorded in ``fixtures/events/signals-golden.json``. The refactor and the
  fixes that follow must leave these outputs byte for byte as they are. The
  file was written by the code at main d68f656 with LEVI_EVENTS_GOLDEN=write;
  never rewrite it to make a test pass.
* the known defects (recon R1 2.1-2.4), each in a test whose name ends in
  ``current_behavior``: what the code does today, not what it should do. A
  fix changes such a test on purpose and says so in its commit.
"""

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from levi.agent import anchored
from levi.agent.anchored import ViewSpec, crossings, uneven_times, view_rows
from levi.agent.signals import gripper_events, still_spans, summarize

FIXTURE = Path(__file__).parent / "fixtures" / "events" / "signals-golden.json"

NAME_SETS = [
    ["x", "y", "z", "gripper"],
    ["ee_pos_x", "ee_pos_y", "ee_pos_z", "finger_width"],
    ["joint_0", "joint_1", "joint_2", "gripper_position"],
    None,  # unnamed
]
SCALES = [1.0, 0.08, 255.0]


def _episode(seed):
    """One deterministic, evenly sampled episode with a clean start: the
    gripper rests at its open level for the first samples (so the start says
    which end is open), then 0-3 grasps; the arm reaches down and up with
    pauses."""
    rng = np.random.default_rng(seed)
    fps = float(rng.choice([10, 15, 30]))
    n = int(rng.integers(40, 160))
    times = np.arange(n) / fps
    scale = float(rng.choice(SCALES))
    open_high = bool(rng.integers(0, 2))
    binary = bool(rng.integers(0, 4) == 0)
    share = np.ones(n)  # 1 = open, as a share of the range
    start = 8
    for _ in range(int(rng.integers(0, 4))):
        if start >= n - 6:
            break
        a = int(rng.integers(start, max(start + 1, n - 6)))
        b = int(min(n, a + rng.integers(4, 30)))
        closed = 0.0 if binary else float(rng.uniform(0.0, 0.5))
        ramp = 0 if binary else int(rng.integers(0, 4))
        for i in range(a, b):
            k = i - a
            share[i] = closed if k >= ramp else 1 - (1 - closed) * (k + 1) / (ramp + 1)
        start = b + 4
    if not binary and rng.integers(0, 2):
        share = share + rng.normal(0, 0.01, n)
        share[:8] = 1.0 + rng.normal(0, 0.005, 8)
    level = share if open_high else 1 - share
    gripper = level * scale
    # Height: a few linear moves between rests; x, y drift.
    z = np.empty(n)
    value = float(rng.uniform(0.1, 0.4))
    i = 0
    while i < n:
        length = int(rng.integers(3, 25))
        target = float(rng.uniform(0.02, 0.4)) if rng.integers(0, 3) else value
        for k in range(min(length, n - i)):
            z[i + k] = value + (target - value) * (k + 1) / length
        value = float(z[min(i + length, n) - 1])
        i += length
    x = np.cumsum(rng.normal(0, 0.002, n)) + 0.4
    y = np.cumsum(rng.normal(0, 0.002, n))
    state = np.stack([x, y, z, gripper], axis=1)
    names = NAME_SETS[int(rng.integers(0, len(NAME_SETS)))]
    features = {
        "observation.state": {"dtype": "float32", "shape": [4], "names": names},
        "timestamp": {"dtype": "float32", "shape": [1], "names": None},
    }
    data = {"timestamp": times, "observation.state": [list(r) for r in state]}
    with_action = bool(rng.integers(0, 2))
    if with_action:
        lag = int(rng.integers(0, 4))
        action = np.roll(state, -lag, axis=0)
        if rng.integers(0, 3) == 0:
            action[:, 3] = gripper[0]  # a command that never moves
        features["action"] = {"dtype": "float32", "shape": [4], "names": names}
        data["action"] = [list(r) for r in action]
    info = {"fps": fps if rng.integers(0, 5) else None, "features": features}
    stats = None
    if rng.integers(0, 2):
        lo, hi = (0.0, scale) if rng.integers(0, 2) else (-0.1 * scale, 1.1 * scale)
        stats = {
            "observation.state": {
                "min": [-1.0, -1.0, 0.0, lo],
                "max": [1.0, 1.0, 0.5, hi],
            }
        }
    return pd.DataFrame(data), info, stats, gripper, open_high


CASES = range(48)


def _observe():
    out = {}
    for seed in CASES:
        table, info, stats, gripper, open_high = _episode(seed)
        bounds = (
            tuple(
                float(v[3])
                for v in (
                    stats["observation.state"]["min"],
                    stats["observation.state"]["max"],
                )
            )
            if stats
            else None
        )
        level = "high" if open_high else "low"
        out[str(seed)] = {
            "summarize": summarize(table, info, stats),
            "crossings_dataset": [list(c) for c in crossings(gripper, bounds, level)],
            "crossings_episode": [list(c) for c in crossings(gripper, None, level)],
        }
    return out


def test_signals_and_crossings_corpus_is_unchanged():
    text = json.dumps(_observe(), sort_keys=True, indent=1) + "\n"
    if os.environ.get("LEVI_EVENTS_GOLDEN") == "write":
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(text)
        pytest.fail("fixture rewritten; rerun without LEVI_EVENTS_GOLDEN")
    assert FIXTURE.read_text() == text


def test_the_corpus_exercises_every_kind_of_line():
    found = json.loads(FIXTURE.read_text())
    lines = [line for case in found.values() for line in case["summarize"]["lines"]]
    for prefix in ("gripper (", "gripper command (", "height (", "still: "):
        assert any(line.startswith(prefix) for line in lines), prefix
    assert any("(to " in line for line in lines)
    assert any(case["crossings_dataset"] for case in found.values())


# --- the shipped review frames -------------------------------------------------


def test_view_rows_of_every_shipped_spec_on_an_even_table():
    """Every view of every shipped spec (and the live service's), at a few
    anchors and frame rates, as rows -- the frames an anchored review shows."""
    from levi.live import generic

    specs = dict(anchored.builtin())
    for name in sorted(p.name for p in generic.SPEC_DIR.glob("generic-*.json")):
        raw = json.loads(generic.text(name))
        if "anchor" in raw:  # the live service's anchored specs
            specs[f"live:{name}"] = anchored.AnchoredSpec.model_validate(raw)
    observed = {}
    for key in sorted(specs):
        spec = specs[key]
        views = list(spec.views) + list(spec.start.views if spec.start else [])
        for veto in spec.vetoes:
            views += list(veto.views or [])
        for fps in (10.0, 15.0, 30.0):
            for n, last in ((0, 0), (40, 200), (199, 200)):
                observed[f"{key}@{fps}:{n}/{last}"] = [
                    view_rows(v, fps, n, last) for v in views
                ]
    path = FIXTURE.parent / "view-rows-golden.json"
    text = json.dumps(observed, sort_keys=True, indent=1) + "\n"
    if os.environ.get("LEVI_EVENTS_GOLDEN") == "write":
        path.write_text(text)
        pytest.fail("fixture rewritten; rerun without LEVI_EVENTS_GOLDEN")
    assert path.read_text() == text


# --- known defects, as they are today ------------------------------------------


def _info(names, fps=10):
    return {
        "fps": fps,
        "features": {
            "observation.state": {
                "dtype": "float32",
                "shape": [len(names)],
                "names": names,
            }
        },
    }


def _table(rows, times=None):
    times = np.arange(len(rows)) / 10 if times is None else np.asarray(times)
    return pd.DataFrame(
        {"timestamp": times, "observation.state": [list(map(float, r)) for r in rows]}
    )


def test_initially_closed_gripper_reads_reversed_current_behavior():
    # 0 = closed: the episode starts holding, opens at 0.3, closes at 0.5.
    values = [0, 0, 0, 1, 1, 0]
    out = summarize(_table([[v] for v in values]), _info(["gripper"]))
    # signals takes the first level as open, so every event is reversed: a
    # channel that starts closed cannot say which end is open (T-A-03 keeps
    # this; only a declared open level reads it right, see below).
    assert out["lines"] == ["gripper (observation.state.gripper): close 0.3, open 0.5"]
    # anchored reads the declared open level (high) and gets them right.
    assert crossings(np.array(values, float), (0.0, 1.0)) == [(3, "open"), (5, "close")]


def test_a_declared_open_level_reads_an_initially_closed_gripper():
    values = [0, 0, 0, 1, 1, 0]
    declared = {"observation.state.gripper": "high"}
    out = summarize(_table([[v] for v in values]), _info(["gripper"]), None, declared)
    assert out["lines"] == ["gripper (observation.state.gripper): open 0.3, close 0.5"]
    times = np.arange(6) / 10
    assert gripper_events(times, np.array(values, float), open_level="high") == [
        {"t": 0.3, "kind": "open"},
        {"t": 0.5, "kind": "close"},
    ]
    # A closure channel (1 = closed) declared low reads the same way.
    closure = 1 - np.array(values, float)
    assert gripper_events(times, closure, open_level="low") == [
        {"t": 0.3, "kind": "open"},
        {"t": 0.5, "kind": "close"},
    ]


SPIKE = np.array([0.0] + [1.0] * 4 + [0.0] * 4 + [1.0] * 3)


def test_a_stray_first_sample_flips_the_signal_lines_current_behavior():
    # The default reads the starting level from the first sample, as main
    # does: one stray first sample turns the episode upside down.
    times = np.arange(len(SPIKE)) / 10
    assert gripper_events(times, SPIKE) == [
        {"t": 0.1, "kind": "close"},
        {"t": 0.5, "kind": "open"},
        {"t": 0.9, "kind": "close"},
    ]


def test_a_robust_start_is_an_explicit_choice():
    # start="robust": the median of the first three samples sets the start,
    # so a stray first sample neither sets the polarity nor makes an event.
    times = np.arange(len(SPIKE)) / 10
    assert gripper_events(times, SPIKE, start="robust") == [
        {"t": 0.5, "kind": "close"},
        {"t": 0.9, "kind": "open"},
    ]
    assert gripper_events(times, SPIKE, open_level="high", start="robust") == [
        {"t": 0.5, "kind": "close"},
        {"t": 0.9, "kind": "open"},
    ]
    out = summarize(_table([[v] for v in SPIKE]), _info(["gripper"]), start="robust")
    assert out["lines"] == ["gripper (observation.state.gripper): close 0.5, open 0.9"]


def _close_at_the_start():
    """The shape of a recorded policy rollout (a live eggplant pick, 10 fps,
    200 rows): open for one row, closed for 0.8 s, open, then two grasps
    (1 = open), the arm lowest at each close; the command leads by a row."""
    grip = np.ones(200)
    for a, b in ((1, 9), (41, 137), (165, 197)):
        grip[a:b] = 0.0
    knots = [(0, 0.233), (41, 0.065), (90, 0.266), (137, 0.16), (165, 0.078)]
    knots += [(197, 0.14), (199, 0.14)]
    z = np.interp(np.arange(200), *zip(*knots, strict=True))
    state = np.stack([np.full(200, 0.5), np.zeros(200), z, grip], axis=1)
    action = state.copy()
    action[:-1, 3] = grip[1:]
    action[0, 3] = 0.0
    names = ["x", "y", "z", "gripper"]
    info = {
        "fps": 10,
        "features": {
            "observation.state": {"dtype": "float32", "shape": [4], "names": names},
            "action": {"dtype": "float32", "shape": [4], "names": names},
        },
    }
    table = pd.DataFrame(
        {
            "timestamp": np.arange(200) / 10,
            "observation.state": list(state),
            "action": list(action),
        }
    )
    return table, info


# summarize(*_close_at_the_start()) at main d68f656, verbatim. (Its command
# line is upside down -- the command starts closed -- a separate defect.)
MAIN_CLOSE_AT_THE_START = [
    (
        "gripper (observation.state.gripper): "
        "close 0.1, open 0.9, close 4.1, open 13.7, close 16.5, open 19.7"
    ),
    (
        "gripper command (action.gripper): "
        "close 0.8, open 4.0, close 13.6, open 16.4, close 19.6"
    ),
    (
        "height (observation.state.z): start 0.233@0.0, low 0.065@4.1, "
        "high 0.266@9.0, low 0.078@16.5, end 0.14@19.9"
    ),
]


def test_a_close_and_open_at_the_start_reads_as_main_reads_it():
    """Review A1, important 1: with the first-three-samples start this
    episode read upside down (the grasp at 4.1 s as an open). The default
    reads it as main d68f656 does -- every line below is main's output."""
    table, info = _close_at_the_start()
    assert summarize(table, info)["lines"] == MAIN_CLOSE_AT_THE_START
    # The robust start reads the 0.8 s close as a stale first sample and
    # the whole state channel upside down; the command (one row earlier,
    # closed from its first row) is then reversed the same way, so it looks
    # followed and gets no line.
    robust = summarize(table, info, start="robust")["lines"]
    assert robust[0] == (
        "gripper (observation.state.gripper): "
        "close 0.9, open 4.1, close 13.7, open 16.5, close 19.7"
    )
    assert not any(line.startswith("gripper command") for line in robust)


def test_anchored_spike_first_sample_sets_the_initial_state_current_behavior():
    # anchored.crossings keeps its frozen semantics: the first sample is the
    # initial state, so a stray closed first sample makes an "open" anchor.
    assert crossings(SPIKE, (0.0, 1.0)) == [(1, "open"), (5, "close"), (9, "open")]


def test_nan_first_sample_is_skipped_current_behavior():
    values = np.array([np.nan, 1, 1, 0, 0, 1.0])
    times = np.arange(6) / 10
    assert gripper_events(times, values) == [
        {"t": 0.3, "kind": "close"},
        {"t": 0.5, "kind": "open"},
    ]
    assert crossings(values, (0.0, 1.0)) == [(3, "close"), (5, "open")]


def test_signals_and_anchored_ranges_differ_on_a_wide_grasp_current_behavior():
    # A 0..0.08 gripper closing only to 0.03 on a wide object.
    values = np.array([0.08] * 10 + [0.03] * 10 + [0.08] * 10)
    times = np.arange(30) / 10
    # signals: the episode's own range, so it is a grasp (to 38% open).
    assert gripper_events(times, values, (0.0, 0.08)) == [
        {"t": 1.0, "kind": "close", "level": 0.375},
        {"t": 2.0, "kind": "open"},
    ]
    # anchored: the dataset's range, so 0.375 never crosses LOW -- no anchor.
    assert crossings(values, (0.0, 0.08)) == []
    assert crossings(values, None) == [(10, "close"), (20, "open")]


def _slow_drift_with_a_drop():
    """The arm creeps at 5% of its fast speed from 0 to 3 s, frames 1.1-1.6 s
    are missing, then moves fast. Real speed never leaves 'still' before 3 s."""
    times = [i / 10 for i in range(11)] + [i / 10 for i in range(17, 31)]
    times += [3.0 + i / 10 for i in range(1, 11)]
    times = np.round(np.array(times), 6)
    x = np.where(times <= 3.0, 0.05 * times, 0.15 + 1.0 * (times - 3.0))
    positions = np.stack([x, np.zeros_like(x), np.zeros_like(x)], axis=1)
    return times, positions


def test_still_reads_dropped_frames_on_their_timestamps():
    times, positions = _slow_drift_with_a_drop()
    # T-A-03: the 0.6 s gap is a 0.6 s step, so the slow creep stays still.
    # (Before, it was read as one frame, six times too fast, and split the
    # rest in two: (0.0, 0.9), (1.9, 2.9).)
    assert still_spans(times, positions, 10) == [(0.0, 2.9)]


def test_speed_on_an_even_table_is_the_step_times_the_rate_to_the_bit():
    from levi.events.motion import speeds

    rng = np.random.default_rng(7)
    for fps in (10.0, 15.0, 30.0, 29.97):
        times = np.arange(200) / fps
        positions = np.cumsum(rng.normal(0, 0.01, (200, 3)), axis=0)
        expected = np.linalg.norm(np.diff(positions, axis=0), axis=1) * fps
        assert np.array_equal(speeds(times, positions, fps), expected)
        # float32 timestamps, as LeRobot stores them, are still "even".
        t32 = times.astype(np.float32).astype(float)
        assert np.array_equal(speeds(t32, positions, fps), expected)


def test_a_repeated_timestamp_counts_as_one_frame():
    from levi.events.motion import speeds

    times = np.array([0.0, 0.1, 0.1, 0.3])
    positions = np.array([[0.0], [0.1], [0.2], [0.4]])
    out = speeds(times, positions, 10.0)
    assert out.tolist() == [0.1 / 0.1, 0.1 * 10, 0.2 / 0.2]
    # So a rest with a repeated timestamp in it is not cut there.
    times = np.round(np.r_[np.arange(10) / 10, 0.9, np.arange(10, 20) / 10], 6)
    x = np.r_[np.zeros(11), np.linspace(0.1, 1.0, 10)]
    rest = still_spans(times, np.stack([x, 0 * x, 0 * x], axis=1), 10)
    assert rest == [(0.0, 0.9)]


def test_speed_on_a_long_float32_table_is_the_step_times_the_rate_to_the_bit():
    from levi.events.motion import speeds, uneven

    rng = np.random.default_rng(11)
    for fps, seconds in ((30.0, 900), (60.0, 600), (200.0, 300)):
        n = int(fps * seconds)
        times = (np.arange(n) / fps).astype(np.float32).astype(float)
        assert uneven(times, fps) is None
        positions = np.cumsum(rng.normal(0, 0.01, (n, 3)), axis=0)
        expected = np.linalg.norm(np.diff(positions, axis=0), axis=1) * fps
        assert np.array_equal(speeds(times, positions, fps), expected)


def test_view_rows_ignore_dropped_frames_current_behavior():
    # 10 fps, frames 10-14 missing: the row 1.0 s before the anchor (row 15,
    # t = 2.0 s) is 10 rows back, which is t = 0.5 s. Still so without the
    # table's timestamps; a review passes them on an uneven table (T-A-05,
    # next test).
    times = np.round(np.array([*range(10), *range(15, 30)]) / 10, 6)
    view = ViewSpec(role="side", camera="cam", offsets_seconds=[-1.0, 0.0])
    rows = view_rows(view, 10.0, 15, len(times) - 1)
    assert rows == [5, 15]
    assert times[rows[0]] == 0.5  # 1.5 s back, not 1.0 s


DROPPED = np.round(np.array([*range(10), *range(15, 30)]) / 10, 6)


def test_view_rows_on_dropped_frames_look_up_timestamps():
    # T-A-05: on an uneven table, an offset in seconds is the row nearest
    # that time: 1.0 s before t = 2.0 s is t = 1.0 s, nearest row 9 (0.9 s).
    times = uneven_times(DROPPED, 10.0)
    assert times is not None
    view = ViewSpec(role="side", camera="cam", offsets_seconds=[-1.0, 0.0, 0.25])
    assert view_rows(view, 10.0, 15, len(DROPPED) - 1, times) == [9, 15, 17]
    # Clamped to the episode at both ends.
    far = ViewSpec(role="side", camera="cam", offsets_seconds=[-9.0, 9.0])
    assert view_rows(far, 10.0, 15, len(DROPPED) - 1, times) == [0, 24]
    # From the episode's start or end.
    start = ViewSpec(role="s", camera="cam", offsets_seconds=[0.0, 1.2], at="start")
    assert view_rows(start, 10.0, 15, len(DROPPED) - 1, times) == [0, 9]
    end = ViewSpec(role="e", camera="cam", offsets_seconds=[-1.0, 0.0], at="end")
    assert view_rows(end, 10.0, 15, len(DROPPED) - 1, times) == [14, 24]
    # Offsets in frames stay frames.
    frames = ViewSpec(role="f", camera="cam", offsets=[-10, 0])
    assert view_rows(frames, 10.0, 15, len(DROPPED) - 1, times) == [5, 15]


def test_a_tie_between_two_rows_takes_the_earlier():
    times = np.array([0.0, 0.1, 0.3])
    view = ViewSpec(role="side", camera="cam", offsets_seconds=[-0.1])
    assert view_rows(view, 10.0, 2, 2, times) == [1]


def test_only_an_uneven_table_is_read_by_timestamp():
    for fps in (10.0, 15.0, 30.0, 29.97):
        even = np.arange(3000) / fps
        assert uneven_times(even, fps) is None
        # float32, as LeRobot stores timestamps, and a late start: still even.
        assert uneven_times((even + 4.2).astype(np.float32), fps) is None
    jitter = np.arange(50) / 10 + np.r_[0, 0.004, np.zeros(48)]
    assert uneven_times(jitter, 10.0) is not None
    assert uneven_times(DROPPED, 10.0) is not None
    # Declared at another rate than recorded: read by timestamp.
    assert uneven_times(np.arange(50) / 10, 15.0) is not None
    # Nothing to go by: as before.
    assert uneven_times(np.array([0.0]), 10.0) is None
    assert uneven_times(np.array([0.0, np.nan, 0.2]), 10.0) is None
    assert uneven_times(np.array([0.0, 0.2, 0.1]), 10.0) is None
    assert uneven_times(np.arange(5) / 10, 0.0) is None


def test_two_grippers_each_get_a_line_and_mixed_events_current_behavior():
    names = ["left_gripper", "right_gripper"]
    rows = [
        [1.0 if i < 5 or i >= 15 else 0.0, 1.0 if i < 8 else 0.0] for i in range(20)
    ]
    out = summarize(_table(rows), _info(names))
    assert out["lines"] == [
        "gripper (observation.state.left_gripper): close 0.5, open 1.5",
        "gripper (observation.state.right_gripper): close 0.8",
    ]
    # Events carry no channel: the two arms are one list.
    assert out["events"] == [
        {"t": 0.5, "kind": "close"},
        {"t": 0.8, "kind": "close"},
        {"t": 1.5, "kind": "open"},
    ]
    # anchored looks at the first gripper only.
    spec = anchored.AnchorSpec()
    stats = None
    key, name, _, _ = anchored._gripper_channel(_table(rows), _info(names), stats, spec)
    assert (key, name) == ("observation.state", "left_gripper")
