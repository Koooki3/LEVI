"""Checking staged boundaries and re-staging an episode (measured in the
full-dataset comparison of 2026-09-23: reference annotators who looked at
every boundary densely agreed with the adjudicated reference far more than
agents who placed boundaries from 1 fps sheets; most disagreement was where a
subtask starts)."""

# ruff: noqa: F811

import pytest
from test_agent_economy import bench  # noqa: F401
from test_agent_ergonomics import two_episode_run

from levi.agent.capabilities import invoke
from levi.agent.mcp import images
from levi.agent.security import Principal
from levi.agent.store import Conflict


def seg(start, end, subtask, text="The arm moves."):
    return {
        "start": start,
        "end": end,
        "subtask": subtask,
        "outcome": "unknown",
        "description": text,
    }


def staged(wb, agent, run, episodes):
    return invoke(
        wb,
        agent,
        "annotations.propose_segments",
        {"run_id": run["id"], "segments": episodes},
    )


def test_every_boundary_is_one_row_with_the_definitions(bench, dataset):
    wb, context = bench
    agent, run = two_episode_run(wb, context, dataset, require_human_pilot=False)
    staged(wb, agent, run, {"0": [seg(0.0, 0.8, "other"), seg(0.8, 1.9, "grasp")]})
    arguments = {"run_id": run["id"], "episodes": [0]}
    value = invoke(wb, agent, "evidence.boundaries", arguments)
    row = value["episodes"]["0"]
    assert row["boundaries"] == [[0.8, "other", "grasp"]] and row["sheets"] == [0]
    assert value["definitions"]["grasp"] == {
        "starts_when": "fingers touch",
        "ends_when": "object moves",
    }
    (sheet,) = value["mosaics"]
    assert sheet["columns"] == len(value["offsets"]) == 7
    assert len(images("evidence.boundaries", arguments, value)) == 1
    with pytest.raises(ValueError, match="Stage episode 1"):
        invoke(wb, agent, "evidence.boundaries", {"run_id": run["id"], "episodes": [1]})


def test_an_agent_re_stages_its_own_episode_in_place(bench, dataset):
    wb, context = bench
    agent, run = two_episode_run(wb, context, dataset, require_human_pilot=False)
    staged(
        wb,
        agent,
        run,
        {"0": [seg(0.0, 1.9, "other")], "1": [seg(0.0, 1.9, "other", "Episode one.")]},
    )
    fixed = {"0": [seg(0.0, 0.6, "other"), seg(0.6, 1.9, "grasp", "Closes.")]}
    with pytest.raises(Conflict, match="replace: true"):
        staged(wb, agent, run, fixed)
    receipt = invoke(
        wb,
        agent,
        "annotations.propose_segments",
        {"run_id": run["id"], "segments": fixed, "replace": True},
    )
    draft = wb.store.get("changes", receipt["id"])["proposals"]
    # Episode 0 now has the two new segments, in its old place; episode 1 is
    # untouched.
    assert [(p["episode_index"], p["subtask_id"]) for p in draft] == [
        (0, "other"),
        (0, "grasp"),
        (1, "other"),
    ]
    assert draft[2]["content"] == "Episode one."


def test_what_a_person_reviewed_is_never_replaced(bench, dataset):
    wb, context = bench
    agent, run = two_episode_run(wb, context, dataset, require_human_pilot=False)
    receipt = staged(wb, agent, run, {"0": [seg(0.0, 1.9, "other")]})
    human = Principal("tester", human=True)
    invoke(
        wb,
        human,
        "changes.review",
        {
            "changeset_id": receipt["id"],
            "revision": receipt["revision"],
            "indices": [0],
            "decision": "accepted",
        },
    )
    with pytest.raises(Conflict, match="has reviewed episodes"):
        invoke(
            wb,
            agent,
            "annotations.propose_segments",
            {
                "run_id": run["id"],
                "segments": {"0": [seg(0.0, 1.9, "grasp")]},
                "replace": True,
            },
        )
