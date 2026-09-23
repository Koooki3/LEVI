"""Staging speaks only about problems (measured 2026-09-23: agents asked to
check every boundary looked 20 times and re-staged once). The checks name
where a staged episode breaks a general rule -- attempts (annotation-007),
`unknown` (annotation-003/-012) -- and stay silent otherwise."""

# ruff: noqa: F811

from test_agent_economy import bench  # noqa: F401
from test_agent_ergonomics import two_episode_run

from levi.agent import checks
from levi.agent.capabilities import invoke


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
        {"id": "other", "success_when": "The outcome is always unknown."},
        {"id": "grasp", "success_when": "The object is lifted."},
    ]
    exempt = checks.always_unknown(definitions)
    assert exempt == {"other"}
    rows = [row(0, 1, "other", "unknown"), row(1, 2, "approach")]
    assert checks.staged(rows)
    assert not checks.staged(rows, exempt=exempt)


def test_release_and_rise_inside_one_interval_are_two_engagements():
    events = [
        {"t": 1.0, "kind": "close"},
        {"t": 2.0, "kind": "open"},
        {"t": 2.4, "kind": "high"},
        {"t": 3.2, "kind": "close"},
    ]
    (line,) = checks.staged([row(0.8, 4.0, "grasp", "failure")], events)
    assert "opened at 2.0, the arm rose at 2.4 and it closed again at 3.2" in line
    # Re-closing without rising is the same engagement.
    same = [e for e in events if e["kind"] != "high"]
    assert not checks.staged([row(0.8, 4.0, "grasp", "failure")], same)
    # Split at the release, with an approach between, nothing is left to say.
    assert not checks.staged(
        [
            row(0.8, 2.0, "grasp", "failure"),
            row(2.0, 3.0, "approach"),
            row(3.0, 4.0, "grasp"),
        ],
        events,
    )


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
