"""Event contracts and signal profiles (levi.events.contracts,
levi.events.signal_profiles, levi.events.facts).

A profile says what each recorded channel means. Inferred from
``meta/info.json`` it must agree with the signal lines' own rules; declared,
it overrides them -- above all which end of a gripper is open, the one thing
an episode that starts with a closed gripper cannot say about itself."""

import json
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from levi.agent.signals import summarize
from levi.counterfactual.schema import FR3_ROBOTIQ
from levi.events import facts, signal_profiles
from levi.events.contracts import (
    EventCandidate,
    SignalObservation,
    SignalSource,
    window_at,
)
from levi.events.signal_profiles import (
    ChannelSpec,
    SignalProfile,
    from_action_contract,
    infer,
    open_levels,
    positions,
    resolve,
)

FR3 = ["x", "y", "z", "rx", "ry", "rz", "gripper"]


def info(state, action=None, fps=10):
    features = {
        "observation.state": {
            "dtype": "float32",
            "shape": [len(state)],
            "names": state,
        },
        "timestamp": {"dtype": "float32", "shape": [1], "names": None},
    }
    if action:
        features["action"] = {
            "dtype": "float32",
            "shape": [len(action)],
            "names": action,
        }
    return {"fps": fps, "features": features}


def table(rows, times=None, action=None):
    data = {
        "timestamp": np.arange(len(rows)) / 10 if times is None else times,
        "observation.state": [list(map(float, r)) for r in rows],
    }
    if action is not None:
        data["action"] = [list(map(float, r)) for r in action]
    return pd.DataFrame(data)


def starts_closed(n=80):
    """An FR3-like episode (1 = open) that starts holding something: it opens
    at 2.0 s, closes again at 5.0 s."""
    rows = []
    for i in range(n):
        t = i / 10
        grip = 1.0 if 2.0 <= t < 5.0 else 0.0
        rows.append([0.4, 0.0, 0.1 + 0.05 * np.sin(t), 3.1, 0.0, 0.0, grip])
    return rows


# --- contracts ---------------------------------------------------------------


def candidate(**over):
    value = {
        "id": "candidate_001",
        "episode_index": 12,
        "event_type": "gripper_close",
        "actor_id": "arm_0",
        "center_time_s": 2.43,
        "time_window_s": (2.38, 2.55),
        "source_frame_index": 73,
        "sources": [
            {
                "feature": "observation.state",
                "dimension": "gripper_width",
                "kind": "measured",
                "units": "m",
            }
        ],
        "salience": 0.83,
    }
    value.update(over)
    return EventCandidate.model_validate(value)


def test_candidate_matches_the_design_record():
    c = candidate()
    dumped = c.model_dump(mode="json")
    assert dumped["schema_version"] == "levi.event_candidate.v1"
    assert dumped["boundary_probability"] is None
    assert dumped["status"] == "proposed"
    assert dumped["time_window_s"] == [2.38, 2.55]
    assert EventCandidate.model_validate_json(c.model_dump_json()) == c


@pytest.mark.parametrize(
    "over",
    [
        {"time_window_s": (2.6, 2.5)},  # reversed window
        {"center_time_s": 3.0},  # centre outside the window
        {"salience": 1.2},  # an ordering on [0, 1]
        {"salience": float("nan")},
        {"boundary_probability": -0.1},
        {"source_frame_index": -1},
        {"sources": []},
        {"status": "suggested"},
        {"extra": 1},
        {"actor_id": "arm 0!"},
    ],
)
def test_candidate_rejects_what_it_cannot_mean(over):
    with pytest.raises(ValidationError):
        candidate(**over)


def test_observation_keeps_row_and_time_together():
    o = SignalObservation(
        episode_index=0,
        kind="gripper_close",
        time_s=1.3,
        time_window_s=(1.2, 1.3),
        source_frame_index=13,
        sources=[SignalSource(feature="observation.state", dimension="gripper")],
    )
    assert o.model_dump()["source_frame_index"] == 13
    with pytest.raises(ValidationError):
        SignalObservation(
            episode_index=0,
            kind="x",
            time_s=1.3,
            time_window_s=(1.2, 1.3),
            source_frame_index=1,
            sources=[SignalSource(feature="a")],
            source_sha256="not-a-hash",
        )


def test_window_at_skips_missing_timestamps():
    times = np.array([0.0, np.nan, np.inf, 0.3, 0.4])
    assert window_at(times, 3) == (0.0, 0.3)
    assert window_at(times, 0) == (0.0, 0.0)


# --- inference agrees with the signal lines ------------------------------------


def test_inferred_fr3_profile():
    p = infer(info(FR3, FR3))
    roles = [(c.feature, c.dimension, c.role, c.kind) for c in p.channels]
    assert ("observation.state", "gripper", "gripper", "measured") in roles
    assert ("action", "gripper", "gripper", "commanded") in roles
    # State before the command, as the signal lines read them.
    assert p.channels[0].feature == "observation.state"
    assert positions(p) == {"arm_0": ("observation.state", [0, 1, 2])}
    assert positions(p, "rotation") == {"arm_0": ("observation.state", [3, 4, 5])}
    # Nothing in a name says which end is open.
    assert all(c.open_level is None for c in p.channels)
    assert open_levels(p) == {}


@pytest.mark.parametrize(
    "names",
    [
        ["x", "y", "z", "gripper"],
        ["ee_pos_x", "ee_pos_y", "ee_pos_z", "finger_width"],
        ["pos_x", "pos_y", "pos_z", "x", "claw"],  # first x per column wins
        ["joint_1", "joint_2", "gripper_left", "gripper_right"],
        ["a", "b", "c"],
    ],
)
def test_inference_uses_the_signal_lines_rules(names):
    from levi.agent.signals import AXIS, GRIPPER

    p = infer(info(names))
    grippers = [c.dimension for c in p.channels if c.role == "gripper"]
    assert grippers == [n for n in names if GRIPPER.search(n)]
    expected = {}
    for d, n in enumerate(names):
        m = AXIS.match(n)
        if m and not GRIPPER.search(n):
            expected.setdefault(m.group(1).lower(), d)
    found = {c.axis: c.index for c in p.channels if c.role == "position"}
    assert found == expected


def test_two_arms_get_their_own_actor():
    names = [
        "left_x", "left_y", "left_z", "left_gripper",
        "right_x", "right_y", "right_z", "right_gripper",
    ]  # fmt: skip
    p = infer(info(names))
    assert positions(p) == {
        "left": ("observation.state", [0, 1, 2]),
        "right": ("observation.state", [4, 5, 6]),
    }
    assert [(c.actor_id, c.index) for c in p.channels if c.role == "gripper"] == [
        ("left", 3),
        ("right", 7),
    ]


# --- declarations override --------------------------------------------------------


def test_declared_open_level_reads_an_episode_that_starts_closed():
    meta = info(FR3)
    rows = starts_closed()
    auto = summarize(table(rows), meta)["lines"][0]
    # Undeclared: the closed start is taken as open, so it reads upside down.
    assert auto.startswith("gripper (observation.state.gripper): close 2.0, open 5.0")
    declared = {
        "channels": [
            {"role": "gripper", "feature": "observation.state", "index": 6,
             "open_level": "high"}
        ]
    }  # fmt: skip
    p = resolve(meta, declared)
    assert p.origin == "resolved"
    assert open_levels(p) == {"observation.state.gripper": "high"}
    lines = summarize(table(rows), meta, open_levels=open_levels(p))["lines"]
    assert lines[0].startswith(
        "gripper (observation.state.gripper): open 2.0, close 5.0"
    )
    kinds = [
        (o.kind, round(o.time_s, 1))
        for o in facts.read(table(rows), meta, p)
        if o.kind.startswith("gripper")
    ]
    assert kinds == [("gripper_open", 2.0), ("gripper_close", 5.0)]


def test_action_contract_declaration_for_fr3():
    p = from_action_contract(FR3_ROBOTIQ)
    grip = next(c for c in p.channels if c.role == "gripper")
    assert (grip.feature, grip.index, grip.open_level) == ("action", 6, "high")
    assert grip.kind == "commanded"
    assert {c.units for c in p.channels if c.role == "position"} == {"m"}
    state = from_action_contract(FR3_ROBOTIQ, feature="observation.state")
    merged = resolve(info(FR3, FR3), state)
    assert open_levels(merged) == {"observation.state.gripper": "high"}


def test_ignore_removes_and_new_channel_adds():
    names = ["x", "y", "z", "gripper_force", "width"]
    declared = {
        "channels": [
            {"role": "ignore", "feature": "observation.state", "index": 3},
            {"role": "gripper", "feature": "observation.state", "index": 4,
             "open_level": "high", "units": "m"},
        ]
    }  # fmt: skip
    p = resolve(info(names), declared)
    grippers = [(c.dimension, c.open_level) for c in p.channels if c.role == "gripper"]
    assert grippers == [("width", "high")]


@pytest.mark.parametrize(
    "channel, message",
    [
        ({"role": "gripper", "feature": "observation.velocity", "index": 0}, "not a float"),
        ({"role": "gripper", "feature": "observation.state", "index": 9}, "outside"),
        (
            {"role": "gripper", "feature": "observation.state", "index": 0,
             "dimension": "gripper"},
            "is not dimension",
        ),
    ],
)  # fmt: skip
def test_bad_declarations_are_refused(channel, message):
    with pytest.raises(ValueError, match=message):
        resolve(info(["x", "y", "z", "gripper"]), {"channels": [channel]})


def test_profile_validation():
    with pytest.raises(ValidationError):
        ChannelSpec(role="position", feature="a", index=0)  # no axis
    with pytest.raises(ValidationError):
        ChannelSpec(role="position", feature="a", index=0, axis="x", open_level="high")
    with pytest.raises(ValidationError):
        ChannelSpec(role="gripper", feature="a", index=0, valid_range=(1.0, 1.0))
    with pytest.raises(ValidationError):
        SignalProfile(
            channels=[
                {"role": "gripper", "feature": "a", "index": 0},
                {"role": "gripper", "feature": "a", "index": 0},
            ]
        )


def test_dataset_declaration_file(tmp_path):
    meta = info(FR3)
    (tmp_path / "meta").mkdir()
    (tmp_path / "meta" / "info.json").write_text(json.dumps(meta))
    assert signal_profiles.for_dataset(tmp_path).origin == "inferred"
    declared = from_action_contract(FR3_ROBOTIQ, feature="observation.state")
    (tmp_path / signal_profiles.DECLARATION).write_text(declared.model_dump_json())
    assert open_levels(signal_profiles.for_dataset(tmp_path)) == {
        "observation.state.gripper": "high"
    }


# --- facts ---------------------------------------------------------------------


def test_facts_on_a_table_with_dropped_frames():
    rows = [r for i, r in enumerate(starts_closed()) if i not in range(30, 40)]
    times = np.array([i / 10 for i in range(80) if i not in range(30, 40)])
    meta = info(FR3)
    p = resolve(meta, from_action_contract(FR3_ROBOTIQ, feature="observation.state"))
    found = facts.read(table(rows, times), meta, p, episode_index=4)
    close = next(o for o in found if o.kind == "gripper_close")
    # Time and row from the table, never row / fps.
    assert close.time_s == pytest.approx(5.0)
    assert close.source_frame_index == 40
    assert close.time_window_s == pytest.approx((4.9, 5.0))
    assert close.sources[0].dimension == "gripper"
    assert all(o.episode_index == 4 for o in found)


def test_facts_on_two_arms():
    names = [
        "left_x", "left_y", "left_z", "left_gripper",
        "right_x", "right_y", "right_z", "right_gripper",
    ]  # fmt: skip
    rows = []
    for i in range(60):
        t = i / 10
        left = 1.0 if t < 2 else 0.0
        right = 1.0 if t < 4 else 0.0
        rows.append([0, 0, 0.1, left, 0, 0, 0.1, right])
    found = facts.read(table(rows), info(names))
    grips = [
        (o.actor_id, o.kind, round(o.time_s, 1)) for o in found if "gripper" in o.kind
    ]
    assert grips == [("left", "gripper_close", 2.0), ("right", "gripper_close", 4.0)]


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [[0, 0, 0, 1]],
        [[np.nan] * 4] * 5,
        [[0.1, 0.1, 0.1, 0.5]] * 20,
    ],
)
def test_facts_on_degenerate_tables(rows):
    meta = info(["x", "y", "z", "gripper"])
    data = pd.DataFrame(
        {
            "timestamp": np.arange(len(rows)) / 10,
            "observation.state": [list(map(float, r)) for r in rows],
        }
    )
    assert facts.read(data, meta) == []


def test_no_torch_and_no_import_cycle():
    code = (
        "import sys\n"
        "import levi.events.signal_profiles, levi.events.facts, levi.events.contracts\n"
        "import levi.agent.signals, levi.agent.anchored\n"
        "assert 'torch' not in sys.modules\n"
    )
    for first in ("levi.events.facts", "levi.agent.signals", "levi.events.contracts"):
        subprocess.run(
            [sys.executable, "-c", f"import {first}\n" + code], check=True, timeout=120
        )


def test_facts_skip_rows_without_a_timestamp():
    rows = starts_closed()
    times = np.arange(len(rows)) / 10
    times[50] = np.nan  # the close at 5.0 s has no timestamp
    meta = info(FR3)
    p = resolve(meta, from_action_contract(FR3_ROBOTIQ, feature="observation.state"))
    del meta["fps"]  # the rate comes from the finite steps
    found = facts.read(table(rows, times), meta, p)
    kinds = [(o.kind, round(o.time_s, 1)) for o in found if "gripper" in o.kind]
    assert kinds == [("gripper_open", 2.0)]
    assert all(np.isfinite(o.time_s) for o in found)


# --- declarations win over inference -----------------------------------------------

EIGHT = ["x", "y", "z", "ee_x", "ee_y", "ee_z", "grip_cmd", "finger_width"]
EIGHT_DECLARED = {
    "channels": [
        *[
            {"role": "position", "feature": "observation.state", "index": i,
             "axis": a, "units": "m"}
            for i, a in zip((3, 4, 5), "xyz", strict=True)
        ],
        {"role": "gripper", "feature": "observation.state", "index": 7,
         "open_level": "high"},
    ]
}  # fmt: skip


def test_declared_channels_win_where_the_index_differs():
    from levi.events import change_points
    from levi.events.signal_profiles import SignalProfileWarning, grippers

    with pytest.warns(SignalProfileWarning, match="grip_cmd"):
        p = resolve(info(EIGHT), EIGHT_DECLARED)
    assert positions(p) == {"arm_0": ("observation.state", [3, 4, 5])}
    assert [(c.index, c.open_level) for c in grippers(p)] == [(7, "high")]
    # The inferred channels the declaration replaced are recorded.
    assert sorted(c.index for c in p.overridden) == [0, 1, 2, 6]
    rows = [
        [0, 0, 0, 0.1 * np.sin(i / 10), 0, 0.2, 1.0, 0.08 if i < 50 else 0.0]
        for i in range(100)
    ]
    names, _, sources = change_points.features(table(rows), info(EIGHT), p)["arm_0"]
    assert "observation.state.finger_width" in names
    assert "observation.state.grip_cmd" not in names
    assert [s.index for s in sources[0]] == [3, 4, 5]


def test_a_declaration_that_names_one_axis_twice_is_refused():
    declared = {
        "channels": [
            {
                "role": "position",
                "feature": "observation.state",
                "index": 0,
                "axis": "x",
            },
            {
                "role": "position",
                "feature": "observation.state",
                "index": 3,
                "axis": "x",
            },
        ]
    }
    with pytest.raises(ValueError, match="twice"):
        resolve(info(EIGHT), declared)


def test_a_declaration_matching_the_inference_warns_nothing():
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        p = resolve(
            info(FR3, FR3), from_action_contract(FR3_ROBOTIQ, "observation.state")
        )
    assert p.overridden == []
    # The command's inferred channels (another feature) stay.
    assert any(c.feature == "action" and c.role == "gripper" for c in p.channels)


def test_names_shorter_than_the_shape_are_refused_clearly():
    meta = info(["x", "y", "z", "gripper"])
    meta["features"]["observation.state"]["shape"] = [6]
    with pytest.raises(ValueError, match="names"):
        resolve(
            meta,
            {
                "channels": [
                    {"role": "gripper", "feature": "observation.state", "index": 5}
                ]
            },
        )
