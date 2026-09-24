"""LEVI's built-in manipulation vocabulary: the default of every dataset,
extended by an annotator only with a defined subtask that is not a synonym
of an existing one, and kept once a person commits the work."""

# ruff: noqa: F811

import pytest
from test_agent_economy import bench  # noqa: F401
from test_agent_ergonomics import two_episode_run

from levi.agent import media
from levi.agent.capabilities import invoke
from levi.agent.security import Principal
from levi.annotations import vocabulary


def test_the_builtin_vocabulary_is_complete_and_unambiguous():
    entries = vocabulary.builtin()["subtasks"]
    ids = [s["id"] for s in entries]
    every = ids + [a for s in entries for a in s.get("aliases", [])]
    assert len(every) == len(set(every)), "an id or alias names two entries"
    assert not set(every) & set(vocabulary.SPECIAL)
    for s in entries:
        assert vocabulary.ID.match(s["id"])
        assert all(s.get(f) for f in vocabulary.DEFINED), s["id"]
    # The object-transfer phases keep the ids earlier annotations use.
    assert ids[:5] == ["approach", "grasp", "transport", "place", "retreat"]
    assert len(ids) <= vocabulary.LIMIT


def test_a_dataset_without_its_own_vocabulary_uses_the_builtin(tmp_path):
    value = vocabulary.read(tmp_path)
    assert value["source"] == "builtin" and value["subtasks"][0]["id"] == "approach"
    vocabulary.write(
        tmp_path, [{"id": "grasp", "label": "grasp", "definition": "Hold it."}]
    )
    own = vocabulary.read(tmp_path)
    assert own["source"] == "dataset" and [s["id"] for s in own["subtasks"]] == [
        "grasp"
    ]
    # A plan definition from a sparse entry borrows the built-in fields.
    (grasp,) = vocabulary.definitions(tmp_path)
    assert grasp["definition"] == "Hold it." and "first touch" in grasp["starts_when"]


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"id": "pick"}, "an alias of 'grasp' in the vocabulary; use 'grasp'"),
        ({"id": "grasp"}, "already in the vocabulary"),
        ({"id": "peel", "definition": "Peel it."}, "needs starts_when"),
        ({"id": "Peel"}, "lowercase"),
    ],
)
def test_a_new_subtask_is_never_a_synonym_or_undefined(entry, message):
    assert message in vocabulary.check_new(entry, [])


def test_a_plan_without_definitions_gets_the_builtin_vocabulary(bench, dataset):
    wb, context = bench
    agent, run = two_episode_run(wb, context, dataset, require_human_pilot=False)
    from levi.agent.schema import TaskContext

    bare = TaskContext.model_validate(
        {**run["context"], "workflow": {"kind": "temporal"}}
    )
    planned = wb.plan(bare, agent)
    ids = [d["id"] for d in planned["context"]["workflow"]["definitions"]]
    assert ids[:5] == ["approach", "grasp", "transport", "place", "retreat"]


def seg(start, end, subtask, text="The arm moves."):
    return {
        "start": start,
        "end": end,
        "subtask": subtask,
        "outcome": "success",
        "description": text,
    }


PEEL = {
    "id": "peel",
    "definition": "A layer is pulled off an object's surface.",
    "starts_when": "The layer first separates from the surface.",
    "ends_when": "The layer is free of the surface.",
    "success_when": "The layer is off.",
}


def test_an_annotator_adds_a_subtask_and_a_commit_keeps_it(bench, dataset):
    wb, context = bench
    agent, run = two_episode_run(wb, context, dataset, require_human_pilot=False)
    rid = run["id"]
    with pytest.raises(ValueError, match="Unknown subtask identity 'peel'"):
        invoke(
            wb,
            agent,
            "annotations.propose_segments",
            {"run_id": rid, "segments": {"0": [seg(0.0, 1.9, "peel")]}},
        )
    with pytest.raises(ValueError, match="use 'grasp'"):
        invoke(
            wb,
            agent,
            "annotations.propose_segments",
            {
                "run_id": rid,
                "new_subtasks": [{**PEEL, "id": "pick"}],
                "segments": {"0": [seg(0.0, 1.9, "pick")]},
            },
        )
    receipt = invoke(
        wb,
        agent,
        "annotations.propose_segments",
        {
            "run_id": rid,
            "new_subtasks": [PEEL],
            "segments": {"0": [seg(0.0, 1.9, "peel")]},
        },
    )
    # Later stagings of the run may use it without defining it again.
    receipt = invoke(
        wb,
        agent,
        "annotations.propose_segments",
        {"run_id": rid, "segments": {"1": [seg(0.0, 1.9, "peel")]}},
    )
    assert [e["id"] for e in wb.store.get("runs", rid)["vocabulary_extensions"]] == [
        "peel"
    ]
    human = Principal("tester", human=True)
    invoke(
        wb,
        human,
        "changes.approve",
        {"changeset_id": receipt["id"], "revision": receipt["revision"]},
    )
    committed = invoke(
        wb,
        human,
        "changes.commit",
        {"changeset_id": receipt["id"], "revision": receipt["revision"]},
        "vocabulary",
    )
    folder = media.state_for(
        __import__(
            "levi.agent.schema", fromlist=["TaskContext"]
        ).TaskContext.model_validate(run["context"])
    ).annotations_dir
    kept = {s["id"]: s for s in vocabulary.read(folder)["subtasks"]}
    assert kept["peel"]["source"] == "agent" and kept["peel"]["added_by"] == rid
    assert "grasp" in kept  # it joined the vocabulary in force, not replaced it
    assert committed
