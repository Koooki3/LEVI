"""Staging speaks only about problems (measured 2026-09-23: agents asked to
check every boundary looked 20 times and re-staged once). The checks name
where a staged episode breaks a general rule -- attempts (annotation-007),
`unknown` (annotation-003/-012) -- and stay silent otherwise. Recorded
signals raise no problem: whether the effector left the object is for the
frames to show (a signal-based check was right about half the time)."""

# ruff: noqa: F811

from test_agent_economy import bench  # noqa: F401
from test_agent_ergonomics import two_episode_run

from levi.agent import checks
from levi.agent.capabilities import invoke
from levi.agent.security import Principal


def row(start, end, subtask, outcome="success"):
    return {"start": start, "end": end, "subtask": subtask, "outcome": outcome}


def test_two_intervals_of_one_subtask_never_meet():
    (line,) = checks.staged([row(0, 2, "grasp", "failure"), row(2, 3, "grasp")])
    assert line.startswith("grasp 0.0–2.0 and 2.0–3.0 meet")
    # An approach between them is a new attempt: nothing to say.
    assert not checks.staged(
        [row(0, 2, "grasp", "failure"), row(2, 2.5, "approach"), row(2.5, 3, "grasp")]
    )


def test_unknown_followed_by_what_settles_it_is_named():
    (line,) = checks.staged([row(0, 2, "grasp", "unknown"), row(2, 3, "approach")])
    assert "is unknown, but the recording goes on" in line
    # At the end of the recording, or before another unknown, it may stand.
    assert not checks.staged([row(0, 2, "approach"), row(2, 3, "grasp", "unknown")])
    assert not checks.staged(
        [row(0, 2, "grasp", "unknown"), row(2, 3, "place", "unknown")]
    )


def test_a_class_whose_outcome_is_always_unknown_is_exempt():
    definitions = [
        {"id": "idle", "success_when": "The outcome is always unknown."},
        {"id": "grasp", "success_when": "The object is lifted."},
    ]
    exempt = checks.always_unknown(definitions)
    assert exempt == {"idle"}
    rows = [row(0, 1, "idle", "unknown"), row(1, 2, "approach")]
    assert checks.staged(rows)
    assert not checks.staged(rows, exempt=exempt)


def test_a_human_hand_is_other_and_unknown_even_without_a_plan_definition():
    # Built-in annotation-004: anything a human hand does is `other`, outcome
    # unknown -- the plan need not define `other` for that.
    rows = [
        row(0, 3, "grasp"),
        row(3, 5, "other", "unknown"),
        row(5, 8, "place"),
    ]
    assert not checks.staged(rows)
    assert not checks.staged(rows, exempt=checks.always_unknown([]))
    # No built-in rule says the same of `background`.
    (line,) = checks.staged([row(0, 1, "background", "unknown"), row(1, 2, "place")])
    assert line.startswith("background 0.0–1.0 is unknown")


def test_the_first_guidance_asks_for_boundary_checks_only_where_problems_are(
    bench,
):
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    orientation = invoke(wb, agent, "workspace.get_context", {})
    (line,) = [x for x in orientation["start_here"] if "evidence.boundaries" in x]
    assert "check every boundary" not in line
    assert "`problems` names" in line and "needs no check" in line


def test_layers_are_checked_apart():
    a = {**row(0, 2, "grasp"), "layer": "left"}
    b = {**row(2, 3, "grasp"), "layer": "right"}
    assert not checks.staged([a, b])


def seg(start, end, subtask, outcome="unknown"):
    return {
        "start": start,
        "end": end,
        "subtask": subtask,
        "outcome": outcome,
        "description": "The arm moves.",
    }


def test_a_receipt_names_problems_only_when_there_are_some(bench, dataset):
    wb, context = bench
    agent, run = two_episode_run(wb, context, dataset, require_human_pilot=False)
    clean = invoke(
        wb,
        agent,
        "annotations.propose_segments",
        {"run_id": run["id"], "segments": {"0": [seg(0.0, 1.9, "other")]}},
    )
    assert "problems" not in clean and "next" not in clean
    receipt = invoke(
        wb,
        agent,
        "annotations.propose_segments",
        {
            "run_id": run["id"],
            "segments": {"1": [seg(0.0, 0.9, "grasp"), seg(0.9, 1.9, "grasp")]},
        },
    )
    assert list(receipt["problems"]) == ["1"]
    assert any(" meet:" in line for line in receipt["problems"]["1"])
    assert "replace: true" in receipt["next"]
