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

from levi.agent import anchored
from levi.agent.anchored import ViewSpec, crossings, view_rows
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
    # signals takes the first level as open, so every event is reversed.
    assert out["lines"] == ["gripper (observation.state.gripper): close 0.3, open 0.5"]
    # anchored reads the declared open level (high) and gets them right.
    assert crossings(np.array(values, float), (0.0, 1.0)) == [(3, "open"), (5, "close")]


def test_spike_first_sample_flips_polarity_current_behavior():
    values = np.array([0.0] + [1.0] * 4 + [0.0] * 4 + [1.0] * 3)
    times = np.arange(len(values)) / 10
    # One stray first sample makes the whole episode read upside down.
    assert gripper_events(times, values) == [
        {"t": 0.1, "kind": "close"},
        {"t": 0.5, "kind": "open"},
        {"t": 0.9, "kind": "close"},
    ]
    assert crossings(values, (0.0, 1.0)) == [(1, "open"), (5, "close"), (9, "open")]


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


def test_still_uses_the_declared_rate_not_timestamps_current_behavior():
    times, positions = _slow_drift_with_a_drop()
    # The 0.6 s gap is read as one frame, so its step looks six times faster
    # and splits the rest in two.
    assert still_spans(times, positions, 10) == [(0.0, 0.9), (1.9, 2.9)]


def test_view_rows_ignore_dropped_frames_current_behavior():
    # 10 fps, frames 10-14 missing: the row 1.0 s before the anchor (row 15,
    # t = 2.0 s) is 10 rows back, which is t = 0.5 s.
    times = np.round(np.array([*range(10), *range(15, 30)]) / 10, 6)
    view = ViewSpec(role="side", camera="cam", offsets_seconds=[-1.0, 0.0])
    rows = view_rows(view, 10.0, 15, len(times) - 1)
    assert rows == [5, 15]
    assert times[rows[0]] == 0.5  # 1.5 s back, not 1.0 s


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
