"""Event-anchored review: anchors from the robot's own signals, one
schema-bound question per event, rules from a spec, one outcome proposal per
episode through the ordinary review queue. Protocol fakes only."""

import json

import numpy as np
import pandas as pd
import pytest
from test_openai_local import (  # noqa: F401
    DIGEST,
    camera_dataset,
    completion,
    config,
    guard,
    server,
)

from levi.agent import anchored
from levi.agent.anchored import AnchoredSpec, crossings, judge, outcome
from levi.agent.capabilities import invoke
from levi.agent.planning import Workflow, approve, clarify
from levi.agent.schema import Budget, TaskContext
from levi.agent.security import Principal

PLATES = anchored.builtin()["plates-release-ar2"]


def spec(camera, **extra):
    """A small generic spec: did the gripper let go of the block?"""
    return {
        "id": "block-drop",
        "anchor": {"signal": "gripper", "event": "open"},
        "views": [{"role": "front", "camera": camera, "offsets": [-2, 0, 3]}],
        "question": "Did the gripper put the block down in the box?",
        "fields": [
            {"name": "held", "enum": ["yes", "no", "unclear"]},
            {"name": "colour", "enum": ["red", "blue", "none", "unclear"]},
            {"name": "in_box", "enum": ["yes", "no", "unclear"]},
        ],
        "valid_when": [
            {"field": "held", "in": ["yes"]},
            {"field": "in_box", "in": ["yes"]},
        ],
        "episode": {"label_field": "colour", "require_labels": ["red", "blue"]},
        "max_output_tokens": 64,
    } | extra


def test_the_plates_spec_is_ar2_frame_for_frame():
    """The shipped spec reproduces the accepted external AR2 review: same
    frames, same field order (the decoding order), same rules."""
    side, wrist = PLATES.views
    assert (side.camera, side.offsets) == (
        "observation.images.view1",
        [-25, -15, -8, -3, -1, 4, 12],
    )
    assert (wrist.camera, wrist.offsets) == (
        "observation.images.hand",
        [-15, -6, -1, 4],
    )
    schema = PLATES.answer_schema()
    assert list(schema["properties"]) == [
        "held_before",
        "plate_colour",
        "landed_on",
        "stays",
    ]
    assert schema["required"] == list(schema["properties"])
    assert PLATES.max_output_tokens == 200
    assert PLATES.question.startswith("A robot arm with a two-finger gripper")
    ok = {
        "held_before": "yes",
        "plate_colour": "pink",
        "landed_on": "same_colour_plate",
        "stays": "unclear",
    }
    assert judge(PLATES, ok)[1] == "supported"
    assert judge(PLATES, ok | {"stays": "no"})[1] == "contradicted"
    assert judge(PLATES, ok | {"plate_colour": "green"})[1] == "contradicted"
    assert judge(PLATES, ok | {"held_before": "unclear"})[1] == "unknown"
    events = [
        {"answer": ok, "valid": True, "verdict": "supported"},
        {
            "answer": ok | {"plate_colour": "white", "held_before": "unclear"},
            "valid": False,
            "verdict": "unknown",
        },
    ]
    verdict, basis = outcome(PLATES, events)
    assert verdict == "failure"
    assert basis["missing_labels"] == ["white"]
    assert basis["undecided_labels"] == ["white"]
    events[1] |= {"answer": ok | {"plate_colour": "white"}, "valid": True}
    assert outcome(PLATES, events)[0] == "success"


def test_a_plan_names_a_built_in_spec_or_gives_its_own_and_freezes_it():
    flow = Workflow.model_validate(
        {"kind": "review", "anchored": {"spec": "plates-release-ar2"}}
    )
    assert flow.anchored["id"] == "plates-release-ar2"
    # Frozen as a plain dict that validates to itself.
    assert Workflow.model_validate(flow.model_dump()).model_dump() == flow.model_dump()
    with pytest.raises(ValueError, match="Unknown anchored review spec"):
        Workflow.model_validate({"kind": "review", "anchored": {"spec": "nope"}})
    with pytest.raises(ValueError, match="review workflow"):
        Workflow.model_validate(
            {"kind": "temporal", "anchored": {"spec": "plates-release-ar2"}}
        )
    with pytest.raises(ValueError, match="cannot take"):
        AnchoredSpec.model_validate(
            spec("cam", valid_when=[{"field": "held", "in": ["maybe"]}])
        )
    with pytest.raises(ValueError, match="offsets"):
        AnchoredSpec.model_validate(
            spec(
                "cam",
                views=[
                    {
                        "role": "front",
                        "camera": "cam",
                        "offsets": [0],
                        "offsets_seconds": [0.0],
                    }
                ],
            )
        )
    # The cameras the spec shows must be in the plan's scope.
    asked = clarify(
        {
            "repo_id": "local/x",
            "episodes": [0],
            "instruction": "Judge releases",
            "provider": "vllm",
            "cameras": ["observation.images.view1"],
            "allow_media_egress": True,
            "workflow": {"kind": "review", "anchored": {"spec": "plates-release-ar2"}},
        }
    )
    assert [q["field"] for q in asked["questions"]] == ["cameras"]
    assert "observation.images.hand" in asked["questions"][0]["message"]


def test_anchors_are_the_channels_transitions_to_the_frame():
    command = np.array([1, 1, 0, 0, 0, 1, 1, 0, 1, 1], dtype=float)
    assert crossings(command, (0.0, 1.0)) == [
        (2, "close"),
        (5, "open"),
        (7, "close"),
        (8, "open"),
    ]
    # An episode that starts closed opens on its first rise.
    assert crossings(np.array([0, 0, 1, 1.0]), (0.0, 1.0)) == [(2, "open")]
    # A measured aperture crosses with hysteresis; jitter is no event.
    width = np.array([0.08, 0.079, 0.05, 0.02, 0.021, 0.02, 0.05, 0.07, 0.08])
    assert crossings(width, None) == [(3, "close"), (7, "open")]
    # A gripper recording closure: open is low.
    closure = 1 - command
    assert crossings(closure, (0.0, 1.0), "low") == crossings(command, (0.0, 1.0))


def gripper_dataset(dataset):
    """Episode 0 opens at frames 7 and 13; episode 1 never closes."""
    pattern = [1.0] * 4 + [0.0] * 3 + [1.0] * 3 + [0.0] * 3 + [1.0] * 7
    for ep in range(2):
        path = dataset / f"data/chunk-000/episode_{ep:06d}.parquet"
        frame = pd.read_parquet(path)
        grip = pattern if ep == 0 else [1.0] * 20
        frame["observation.state"] = [[0.1 * i, g] for i, g in enumerate(grip)]
        frame.to_parquet(path)


def replies(fake, answers):
    def reply(payload):
        return completion(json.dumps(answers.pop(0)))

    fake.reply = reply


def test_a_review_asks_once_per_opening_and_proposes_the_outcome(
    client,
    dataset,
    server,  # noqa: F811 - the fixture imported above
):
    from levi import catalog, service
    from levi.agent.runtime import Workbench

    camera = camera_dataset(dataset)
    gripper_dataset(dataset)
    entry = catalog.register(str(dataset))
    wb = Workbench(service.STATE)
    wb.store.put("providers", "vllm", config().model_dump())
    context = TaskContext(
        repo_id=entry["id"],
        episodes=[0, 1],
        instruction="Judge each release",
        provider="vllm",
        cameras=[camera],
        allow_media_egress=True,
        workflow={
            "kind": "review",
            "anchored": spec(camera),
            "require_human_pilot": False,
        },
        budget=Budget(max_calls=20, max_tokens=None, max_seconds=600),
    )
    run = wb.plan(context)
    assert "gripper open" in run["plan"]["estimate"]["basis"]
    approve(wb, run["id"], 1, "human")
    replies(
        server,
        [
            {"held": "yes", "colour": "red", "in_box": "yes"},
            {"held": "unclear", "colour": "blue", "in_box": "yes"},
        ],
    )
    assert wb.store.claim(run["id"], "owner")
    wb.execute(run["id"], "owner", pilot=False)
    result = wb.store.get("runs", run["id"])
    assert result["status"] == "waiting_for_review", result["reason"]
    chats = server.chats()
    assert len(chats) == 2
    for chat in chats:
        # One user turn: the spec's frames in order, then its question.
        (message,) = chat["messages"]
        parts = message["content"]
        assert [p["type"] for p in parts] == ["image_url"] * 3 + ["text"]
        assert parts[-1]["text"] == "Did the gripper put the block down in the box?"
        schema = chat["response_format"]["json_schema"]["schema"]
        assert list(schema["properties"]) == ["held", "colour", "in_box"]
        assert chat["max_completion_tokens"] == 64
        assert chat["chat_template_kwargs"] == {"enable_thinking": False}
    assert result["requests"] == 2 and result["tokens"] == 240
    record = wb.store.get("anchored", f"{run['id']}:0")
    assert [e["frame_index"] for e in record["events"]] == [7, 13]
    assert [e["verdict"] for e in record["events"]] == ["supported", "unknown"]
    assert [f["frame_index"] for f in record["events"][0]["frames"]] == [5, 7, 10]
    assert record["channel"] == "observation.state.gripper.pos"
    assert record["outcome"] == "failure"
    assert record["basis"]["undecided_labels"] == ["blue"]
    change = wb.store.get("changes", result["changes"])
    by_episode = {p["episode_index"]: p for p in change["proposals"]}
    assert by_episode[0]["kind"] == "outcome"
    assert by_episode[0]["outcome"] == "failure"
    assert by_episode[0]["uncertainty"] == "undecided for blue"
    assert "valid open at 0.7 s" in by_episode[0]["content"]
    # Episode 1 never opened: no call, a failure citing its last frame.
    assert by_episode[1]["outcome"] == "failure"
    assert by_episode[1]["evidence_ids"][0].endswith("frame_000019")
    # A resumed episode reads its answers from the cache.
    assert (
        wb.store.get("model_cache", f"{run['id']}:0:anchor-000007")["answer"]["held"]
        == "yes"
    )
    # Results are readable by capability and by the viewer's route.
    human = Principal("tester", human=True)
    summary = invoke(wb, human, "anchored.get", {"repo_id": entry["id"]})
    assert summary["episodes"]["0"] == {"outcome": "failure", "events": 2, "valid": 1}
    one = invoke(wb, human, "anchored.get", {"run_id": run["id"], "episode": 0})
    assert one["events"][1]["checks"][0] == {
        "field": "held",
        "value": "unclear",
        "result": "unknown",
    }
    specs = invoke(wb, human, "anchored.specs", {})
    assert "plates-release-ar2" in [s["id"] for s in specs["specs"]]
    viewer = client.get(
        "/annotations/api/anchored/episodes/0", params={"repo_id": entry["id"]}
    )
    assert viewer.status_code == 200, viewer.text
    assert viewer.json()["events"][0]["answer"]["colour"] == "red"
    assert list(viewer.json()["events"][0]["answer"]) == ["held", "colour", "in_box"]
    assert viewer.json()["spec"]["fields"][0]["name"] == "held"
    empty = client.get(
        "/annotations/api/anchored/episodes/1", params={"repo_id": entry["id"]}
    )
    assert empty.json()["events"] == []
    listed = client.get(
        "/annotations/api/anchored/summary", params={"repo_id": entry["id"]}
    )
    assert listed.json()["episodes"]["1"]["outcome"] == "failure"
    missing = client.get(
        "/annotations/api/anchored/episodes/5", params={"repo_id": entry["id"]}
    )
    assert missing.status_code == 404


def test_an_answer_outside_the_spec_sets_the_episode_aside(
    client,
    dataset,
    server,  # noqa: F811 - the fixture imported above
):
    from levi import catalog, service
    from levi.agent.runtime import Workbench

    camera = camera_dataset(dataset)
    gripper_dataset(dataset)
    entry = catalog.register(str(dataset))
    wb = Workbench(service.STATE)
    wb.store.put("providers", "vllm", config().model_dump())
    context = TaskContext(
        repo_id=entry["id"],
        episodes=[0, 1],
        instruction="Judge each release",
        provider="vllm",
        cameras=[camera],
        allow_media_egress=True,
        workflow={
            "kind": "review",
            "anchored": spec(camera),
            "require_human_pilot": False,
        },
        budget=Budget(max_calls=20, max_tokens=None, max_seconds=600),
    )
    run = wb.plan(context)
    approve(wb, run["id"], 1, "human")
    replies(server, [{"held": "perhaps"}])
    assert wb.store.claim(run["id"], "owner")
    wb.execute(run["id"], "owner", pilot=False)
    result = wb.store.get("runs", run["id"])
    assert result["status"] == "waiting_for_review"
    assert [f["episode"] for f in result["failed"]] == [0]
    assert result["completed"] == [1]
    # The tokens it cost are settled, not lost.
    assert result["tokens"] == 120


def test_only_a_local_model_runs_an_anchored_review(client, dataset):
    from levi import catalog, service
    from levi.agent.runtime import Workbench

    camera = camera_dataset(dataset)
    entry = catalog.register(str(dataset))
    wb = Workbench(service.STATE)
    wb.store.put(
        "providers",
        "api",
        {
            "name": "api",
            "kind": "openai-compatible",
            "base_url": "https://example.com/v1",
            "model": "m",
            "tools": True,
            "vision": True,
        },
    )
    with pytest.raises(ValueError, match="local model"):
        wb.plan(
            TaskContext(
                repo_id=entry["id"],
                episodes=[0],
                instruction="Judge each release",
                provider="api",
                cameras=[camera],
                allow_media_egress=True,
                workflow={"kind": "review", "anchored": spec(camera)},
            )
        )
    assert DIGEST
