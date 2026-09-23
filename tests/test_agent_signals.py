"""Recorded signals as evidence: what the robot's state and action columns
say about when it did things -- the gripper closing and opening, the arm's
height turns, the arm at rest. Measured against a reference annotation
(2026-09-23) they are facts, not boundaries: releases sat on the recorded
opening, contact and lift did not sit on the closing or the lowest point."""

# ruff: noqa: F811

import numpy as np
import pandas as pd
import pytest
from test_agent_economy import bench  # noqa: F401
from test_agent_ergonomics import run_for

from levi.agent.capabilities import invoke
from levi.agent.signals import summarize, vector_columns

ARM = ["x", "y", "z", "gripper"]


def info(names, action_names=None):
    features = {
        "observation.state": {
            "dtype": "float32",
            "shape": [len(names)],
            "names": names,
        },
        "timestamp": {"dtype": "float32", "shape": [1], "names": None},
    }
    if action_names:
        features["action"] = {
            "dtype": "float32",
            "shape": [len(action_names)],
            "names": action_names,
        }
    return {"fps": 10, "features": features}


def table(rows, action=None):
    data = {
        "timestamp": np.arange(len(rows)) / 10,
        "observation.state": [list(map(float, r)) for r in rows],
    }
    if action is not None:
        data["action"] = [list(map(float, r)) for r in action]
    return pd.DataFrame(data)


def pick(n=60):
    """Reach down, close, lift, move, lower, open, rest."""
    rows = []
    for i in range(n):
        t = i / 10
        if t < 2:
            z = 0.15 - 0.1 * min(t, 1.0)
        elif t < 4:
            z = 0.05 + 0.1 * min(t - 2, 1.0)
        else:
            z = 0.15 - 0.1 * min(t - 4, 1.0)
        grip = 1.0 if t < 1.2 or t >= 5.0 else 0.0
        x = 0.4 + 0.1 * min(t, 5.0)
        rows.append([x, 0.0, z, grip])
    return rows


def test_gripper_height_and_rest_come_back_as_lines_with_times():
    out = summarize(table(pick()), info(ARM))
    grip, height, still = out["lines"]
    assert grip == "gripper (observation.state.gripper): close 1.2, open 5.0"
    assert height.startswith(
        "height (observation.state.z): start 0.15@0.0, low 0.05@1.0"
    )
    assert "high 0.15@3.0" in height
    # The arm stops once it has put the object down.
    assert still.startswith("still: 5.") and still.endswith("5.9")
    kinds = [e["kind"] for e in out["events"]]
    assert kinds.count("close") == 1 and kinds.count("open") == 1


def test_a_grasp_on_a_wide_object_counts_and_says_how_far_it_closed():
    # Closing from 0.08 to 0.03 of a 0..0.08 gripper: never near fully
    # closed, still a grasp -- and 38% open says something is in the hand.
    rows = [[0.08 if i < 10 or i >= 20 else 0.03] for i in range(30)]
    stats = {"observation.state": {"min": [0.0], "max": [0.08]}}
    line = summarize(table(rows), info(["gripper_width"]), stats)["lines"][0]
    assert (
        line
        == "gripper (observation.state.gripper_width): close 1.0 (to 38%), open 2.0"
    )
    # Without the dataset's range there is no scale to say it on.
    bare = summarize(table(rows), info(["gripper_width"]))["lines"][0]
    assert bare.endswith("close 1.0, open 2.0")


def test_a_gripper_that_only_jitters_has_no_events():
    rows = [[0.08 - 0.001 * (i % 2)] for i in range(30)]
    stats = {"observation.state": {"min": [0.0], "max": [0.08]}}
    assert not summarize(table(rows), info(["gripper_width"]), stats)["lines"]


def test_a_command_gets_a_line_only_when_the_gripper_did_not_follow_it():
    rows = pick()
    followed = summarize(table(rows, action=rows), info(ARM, action_names=ARM))
    assert not any("command" in line for line in followed["lines"])
    # Commanded to close, the gripper never moved: an attempt the pictures
    # may not show.
    stuck = [[*r[:3], 1.0] for r in rows]
    missed = summarize(table(stuck, action=rows), info(ARM, action_names=ARM))
    assert any(
        line.startswith("gripper command (action.gripper): close 1.2")
        for line in missed["lines"]
    )


def test_unnamed_dimensions_give_only_the_arm_at_rest():
    rows = [[min(i, 20) / 20, 0.5] for i in range(40)]
    unnamed = {
        "fps": 10,
        "features": {"observation.state": {"dtype": "float32", "shape": [2]}},
    }
    (line,) = summarize(table(rows), unnamed)["lines"]
    assert line.startswith("still: 2.") and line.endswith("3.9")


def test_no_vector_columns_means_no_signals():
    video = {"observation.images.top": {"dtype": "video", "shape": [3, 8, 8]}}
    assert vector_columns({"features": video}) == {}


def test_the_first_read_carries_the_signals(bench, dataset):
    wb, context = bench
    agent, run, _ = run_for(wb, context, dataset)
    sheet = invoke(
        wb,
        agent,
        "evidence.read",
        {"run_id": run["id"], "episode": 0, "layout": "mosaic"},
    )
    assert any(
        line.startswith("gripper (observation.state.gripper.pos): close 1.3")
        for line in sheet["signals"]
    )
    bare = invoke(
        wb,
        agent,
        "evidence.read",
        {"run_id": run["id"], "episode": 0, "layout": "mosaic", "signals": False},
    )
    assert "signals" not in bare


def test_a_narrower_refinement_adds_fewer_frames_and_never_a_wider_one(bench, dataset):
    wb, context = bench
    agent, run, _ = run_for(wb, context, dataset)
    invoke(
        wb,
        agent,
        "evidence.read",
        {"run_id": run["id"], "episode": 0, "layout": "mosaic"},
    )
    narrow = invoke(
        wb,
        agent,
        "evidence.refine",
        {
            "run_id": run["id"],
            "episode": 0,
            "around_seconds": [0.6],
            "window_seconds": 0.1,
        },
    )
    wide = invoke(
        wb,
        agent,
        "evidence.refine",
        {"run_id": run["id"], "episode": 0, "around_seconds": [1.4]},
    )
    assert 0 < narrow["added"][0]["frames"] < wide["added"][0]["frames"]
    planned = run["context"]["workflow"]["boundary_window_seconds"]
    with pytest.raises(ValueError, match="at most the plan's boundary window"):
        invoke(
            wb,
            agent,
            "evidence.refine",
            {
                "run_id": run["id"],
                "episode": 0,
                "around_seconds": [1.2],
                "window_seconds": planned + 1,
            },
        )


def test_several_episodes_come_back_in_one_answer_with_every_sheet(bench, dataset):
    from test_agent_ergonomics import two_episode_run

    from levi.agent.mcp import images

    wb, context = bench
    agent, run = two_episode_run(wb, context, dataset, require_human_pilot=False)
    arguments = {"run_id": run["id"], "episodes": [1, 0], "layout": "mosaic"}
    value = invoke(wb, agent, "evidence.read", arguments)
    assert list(value["episodes"]) == ["1", "0"]
    # Each episode as a single read gives it, sheet included, in order.
    assert [value["episodes"][e]["sheet"] for e in ("1", "0")] == [0, 1]
    assert value["mosaics"][0]["artifact"].startswith("episode_000001--sheet")
    assert value["episodes"]["0"]["times"] and value["episodes"]["0"]["signals"]
    # Every sheet reaches the caller, and the first read prepared both.
    assert len(images("evidence.read", arguments, value)) == 2
    assert {0, 1} <= set(wb.store.get("runs", run["id"])["prepared"])


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"episode": 0, "episodes": [1]},
        {"episodes": [0, 0], "layout": "mosaic"},
        {"episodes": [0], "layout": "single"},
        {"episodes": [0, 1, 2, 3, 4], "layout": "mosaic"},
    ],
)
def test_a_read_names_one_episode_or_a_few_whole_ones(arguments):
    from pydantic import ValidationError

    from levi.agent.capabilities import Recall

    with pytest.raises(ValidationError):
        Recall.model_validate({"run_id": "r", **arguments})


def test_a_span_is_watched_at_the_step_asked_for(bench, dataset):
    wb, context = bench
    agent, run, _ = run_for(wb, context, dataset)
    invoke(
        wb,
        agent,
        "evidence.read",
        {"run_id": run["id"], "episode": 0, "layout": "mosaic"},
    )
    value = invoke(
        wb,
        agent,
        "evidence.refine",
        {
            "run_id": run["id"],
            "episode": 0,
            "ranges": [[0.2, 1.0]],
            "step_seconds": 0.2,
        },
    )
    (span,) = value["added"]
    assert span["span"] == [0.2, 1.0]
    times = sorted(
        round(r["timestamp"], 1)
        for r in wb.store.get("evidence", f"{run['id']}:0")["items"]
    )
    # Every instant of the span at 0.2 s is in the ledger now.
    assert {0.2, 0.4, 0.6, 0.8, 1.0} <= set(times)
    assert value["mosaics"] and value["reading"]


def test_several_episodes_are_refined_in_one_call(bench, dataset):
    from test_agent_ergonomics import two_episode_run

    from levi.agent.mcp import images

    wb, context = bench
    agent, run = two_episode_run(wb, context, dataset, require_human_pilot=False)
    invoke(
        wb,
        agent,
        "evidence.read",
        {"run_id": run["id"], "episodes": [0, 1], "layout": "mosaic"},
    )
    arguments = {
        "run_id": run["id"],
        "episodes": {
            "0": {"ranges": [[0.4, 1.2]], "step_seconds": 0.2},
            "1": {"around_seconds": [0.9], "window_seconds": 0.3},
        },
    }
    value = invoke(wb, agent, "evidence.refine", arguments)
    assert set(value["episodes"]) == {"0", "1"}
    assert value["episodes"]["0"]["added"][0]["span"] == [0.4, 1.2]
    assert value["episodes"]["1"]["added"][0]["around"] == 0.9
    assert len(images("evidence.refine", arguments, value)) == sum(
        e["sheets"] for e in value["episodes"].values()
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {"episode": 0},
        {"episode": 0, "ranges": [[2.0, 1.0]]},
        {"episode": 0, "ranges": [[0.0, 12.0]]},
        {"episodes": {"0": {}}},
        {"episodes": {"0": {"around_seconds": [1.0]}}, "around_seconds": [1.0]},
    ],
)
def test_a_refinement_names_what_to_look_at(arguments):
    from pydantic import ValidationError

    from levi.agent.capabilities import Refine

    with pytest.raises(ValidationError):
        Refine.model_validate({"run_id": "r", **arguments})
