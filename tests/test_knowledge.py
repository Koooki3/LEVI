"""Built-in knowledge: what every installation knows, and how local memory
earns a place in it."""

import json
import shutil

import pytest
from test_agent_economy import bench  # noqa: F401

from levi.harness import context, knowledge, tasking
from levi.harness.layout import write_json


@pytest.fixture
def repo_copy(tmp_path, monkeypatch):
    """Promotion writes repository files: work on a copy."""
    folder = tmp_path / "knowledge"
    shutil.copytree(knowledge.KNOWLEDGE, folder)
    monkeypatch.setattr(knowledge, "KNOWLEDGE", folder)
    return folder


def note(text, count=1, workflow="temporal"):
    return {"note": text, "count": count, "workflow": workflow, "at": 0}


def test_every_shipped_entry_parses_and_is_dataset_agnostic():
    for topic in knowledge.TOPICS:
        entries = knowledge.load(topic)
        assert entries, topic
        ids = [e["id"] for e in entries]
        assert len(ids) == len(set(ids)) and all(i.startswith(topic) for i in ids)
        for entry in entries:
            specific = [w for r, w in knowledge.SPECIFIC if r.search(entry["text"])]
            assert not specific, (entry["id"], specific)


def test_candidates_recur_before_they_are_ready(tmp_path, repo_copy):
    state = tmp_path / "workbench"
    write_json(
        state / "memory/plates.json",
        {
            "teaching": [
                note("Label every regrasp as its own grasp interval.", 2),
                note("The pink plate is lifted at 26.0 s."),
                note("Say what the gripper holds."),
            ]
        },
    )
    write_json(
        state / "memory/screws.json",
        {"teaching": [note("Say what the gripper holds.")]},
    )
    write_json(
        state / "memory/workspace.json",
        {"teaching": [note("Copy episode lists exactly.", workflow="interpret")]},
    )
    items = {i["text"]: i for i in knowledge.refresh(state)["items"]}
    assert items["Label every regrasp as its own grasp interval."]["ready"]
    assert items["Say what the gripper holds."]["datasets"] == ["plates", "screws"]
    assert items["Say what the gripper holds."]["ready"], "two datasets"
    timed = items["The pink plate is lifted at 26.0 s."]
    assert not timed["ready"] and timed["specific"] == ["gives a time"]
    assert items["Copy episode lists exactly."]["topic"] == "interpretation"
    # Ids are stable across refreshes.
    again = {i["text"]: i["id"] for i in knowledge.refresh(state)["items"]}
    assert again == {text: item["id"] for text, item in items.items()}


def test_promotion_appends_an_entry_and_refuses_specific_text(tmp_path, repo_copy):
    state = tmp_path / "workbench"
    write_json(
        state / "memory/plates.json",
        {"teaching": [note("The pink plate is lifted at 26.0 s.", 2)]},
    )
    item = knowledge.refresh(state)["items"][0]
    before = len(knowledge.load("annotation"))
    with pytest.raises(ValueError, match="dataset-agnostic"):
        knowledge.promote(state, item["id"])
    done = knowledge.promote(
        state, item["id"], text="Mark the lift where the object first leaves the table."
    )
    entries = knowledge.load("annotation")
    assert len(entries) == before + 1 and entries[-1]["id"] == done["entry"]
    assert "plates" in entries[-1]["source"]
    with pytest.raises(ValueError, match="already promoted"):
        knowledge.promote(state, item["id"])
    # A promoted note is no longer offered, even while it stays in memory.
    knowledge.refresh(state)
    assert knowledge.candidates(state) == []
    assert knowledge.candidates(state, None)[0]["entry_text"].startswith("Mark")


def test_models_receive_the_rules_for_their_job(tmp_path):
    run = {
        "dataset_key": "plates",
        "context": {"episodes": [0], "workflow": {"kind": "temporal"}},
        "harness": {"memory": None, "frozen_at": None},
    }
    value = context.brief(tmp_path, run)
    assert value["levi_rules"] == knowledge.texts("annotation")
    _, user = tasking.prompt(tmp_path, "annotate the first 10 demos")
    assert json.loads(user)["rules"] == knowledge.texts("interpretation")


def test_only_a_person_promotes(bench):  # noqa: F811
    from levi.agent.capabilities import invoke
    from levi.agent.security import Principal

    wb, context_ = bench
    agent = Principal("conn", datasets=(context_.repo_id,))
    listed = invoke(wb, agent, "knowledge.list", {"topic": "harness"})
    assert listed["built_in"]["harness"]
    with pytest.raises(PermissionError):
        invoke(wb, agent, "knowledge.promote", {"candidate_id": "candidate-001"})
