"""Event-anchored review: anchors from the robot's own signals, one
schema-bound question per event, rules from a spec, one outcome proposal per
episode through the ordinary review queue. Protocol fakes only."""

import hashlib
import json
import os
from pathlib import Path

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

PLATES = anchored.builtin()["plates-release"]
FIXTURES = Path(__file__).parent / "fixtures" / "anchored"


def golden(name, observed):
    """Compare with a recorded fixture, byte for byte. The ``*-before.json``
    fixtures were written by the code before start checks and vetoes existed
    (604b248) with LEVI_ANCHORED_GOLDEN=write; never rewrite them to make a
    test pass."""
    path = FIXTURES / name
    text = json.dumps(observed, sort_keys=True, ensure_ascii=False, indent=1) + "\n"
    if os.environ.get("LEVI_ANCHORED_GOLDEN") == "write":
        path.write_text(text)
    assert path.read_text() == text


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


def test_the_plates_spec_is_the_accepted_review_frame_for_frame():
    """The shipped spec reproduces the accepted external release-anchored
    review (rule set 2): same
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
        {"kind": "review", "anchored": {"spec": "plates-release"}}
    )
    assert flow.anchored["id"] == "plates-release"
    assert flow.anchored["title"]["en"] == "Plates release-review rules"
    # Frozen as a plain dict that validates to itself.
    assert Workflow.model_validate(flow.model_dump()).model_dump() == flow.model_dump()
    with pytest.raises(ValueError, match="Unknown anchored review spec"):
        Workflow.model_validate({"kind": "review", "anchored": {"spec": "nope"}})
    with pytest.raises(ValueError, match="review workflow"):
        Workflow.model_validate(
            {"kind": "temporal", "anchored": {"spec": "plates-release"}}
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
            "workflow": {"kind": "review", "anchored": {"spec": "plates-release"}},
        }
    )
    assert [q["field"] for q in asked["questions"]] == ["cameras"]
    assert "observation.images.hand" in asked["questions"][0]["message"]


def test_the_former_plates_id_still_names_the_same_spec():
    """Approved plans, stored runs and scripts name ``plates-release-ar2``;
    it resolves to the renamed spec, question and rules unchanged."""
    assert anchored.aliases() == {"plates-release-ar2": "plates-release"}
    assert anchored.lookup("plates-release-ar2") == PLATES
    assert anchored.lookup("plates-release") == PLATES
    assert anchored.lookup("nope") is None
    old = Workflow.model_validate(
        {"kind": "review", "anchored": {"spec": "plates-release-ar2"}}
    )
    new = Workflow.model_validate(
        {"kind": "review", "anchored": {"spec": "plates-release"}}
    )
    assert old.anchored == new.anchored
    # A plan approved before the rename froze the whole spec under the old id
    # and without a title: it still validates to itself, keeps its id, and
    # is shown by the built-in spec's title.
    frozen = PLATES.model_dump(by_alias=True) | {"id": "plates-release-ar2"}
    frozen.pop("title")
    kept = Workflow.model_validate({"kind": "review", "anchored": frozen})
    assert kept.anchored["id"] == "plates-release-ar2"
    assert kept.anchored["question"] == PLATES.question
    assert anchored.title_of(frozen) == "Plates release-review rules"
    assert anchored.title_of(frozen, "zh") == "plates 释放复核规则"
    assert anchored.title_of(frozen, "fr") == "Plates release-review rules"
    # A spec of one's own without a title shows its id.
    assert anchored.title_of(spec("cam")) is None
    assert anchored.titles(spec("cam", title={"en": "Block drop"})) == {
        "en": "Block drop"
    }
    with pytest.raises(ValueError, match="title"):
        AnchoredSpec.model_validate(spec("cam", title={"english": "x"}))
    with pytest.raises(ValueError, match="title"):
        AnchoredSpec.model_validate(spec("cam", title={"en": " "}))


def test_a_run_stored_under_the_former_id_reads_with_its_title():
    """Results of a run planned before the rename (spec id
    ``plates-release-ar2``, no title frozen) come back with the title."""

    class Store:
        def __init__(self, rows):
            self.rows = rows

        def list(self, kind):
            return list(self.rows[kind].values())

        def ids(self, kind):
            return list(self.rows[kind])

        def get(self, kind, key):
            return self.rows[kind][key]

    frozen = PLATES.model_dump(by_alias=True) | {"id": "plates-release-ar2"}
    frozen.pop("title")
    run = {
        "id": "r1",
        "status": "committed",
        "dataset_key": "local/plates",
        "created_at": 1.0,
        "context": {"workflow": {"kind": "review", "anchored": frozen}},
    }
    record = {
        "run_id": "r1",
        "episode_index": 0,
        "spec": {"id": "plates-release-ar2", "version": 1},
        "outcome": "failure",
        "events": [],
    }
    store = Store({"runs": {"r1": run}, "anchored": {"r1:0": record}})
    summary = anchored.payload(store, "local/plates")
    assert summary["spec"]["id"] == "plates-release-ar2"
    assert summary["spec"]["title"] == {
        "en": "Plates release-review rules",
        "zh": "plates 释放复核规则",
    }
    one = anchored.payload(store, "local/plates", episode=0)
    assert one["spec"]["id"] == "plates-release-ar2"
    assert one["spec"]["title"]["en"] == "Plates release-review rules"


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
    listed_specs = {s["id"]: s for s in specs["specs"]}
    assert listed_specs["plates-release"]["aliases"] == ["plates-release-ar2"]
    assert listed_specs["plates-release"]["title"]["zh"] == "plates 释放复核规则"
    assert "plates-release-ar2" not in listed_specs
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


def test_a_spec_without_start_check_or_vetoes_decides_exactly_as_before():
    """Regression: the plates rules on 221 recorded answers (100 development
    episodes) give the same per-event readings, outcomes and bases, and the
    plan freezes the same spec, byte for byte, as before start checks and
    vetoes existed."""
    answers = json.loads((FIXTURES / "plates-dev-answers.json").read_text())
    observed = {}
    for ep, rows in answers["episodes"].items():
        events = []
        for answer in rows:
            checks, verdict = judge(PLATES, answer)
            events.append(
                {
                    "answer": answer,
                    "checks": checks,
                    "verdict": verdict,
                    "valid": verdict == "supported",
                }
            )
        verdict, basis = outcome(PLATES, events)
        observed[ep] = {
            "events": [[e["verdict"], e["checks"]] for e in events],
            "outcome": verdict,
            "basis": basis,
        }
    observed["frozen_spec"] = Workflow.model_validate(
        {"kind": "review", "anchored": {"spec": "plates-release"}}
    ).model_dump()
    golden("plates-dev-before.json", observed)


def scrub(value, run_id):
    """A run's record without what differs between two identical runs."""
    if isinstance(value, dict):
        return {
            k: scrub(v, run_id)
            for k, v in value.items()
            if k not in {"elapsed_seconds", "at", "created_at"}
        }
    if isinstance(value, list):
        return [scrub(v, run_id) for v in value]
    if isinstance(value, str):
        return value.replace(run_id, "<run>")
    return value


def test_a_review_without_start_check_or_vetoes_runs_exactly_as_before(
    client,
    dataset,
    server,  # noqa: F811 - the fixture imported above
):
    """Regression: the same requests (frames, question, schema), the same
    record and the same outcome proposal as before start checks and vetoes
    existed."""
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
    requests = []
    for chat in server.chats():
        (message,) = chat["messages"]
        requests.append(
            {
                "images": [
                    hashlib.sha256(p["image_url"]["url"].encode()).hexdigest()
                    for p in message["content"]
                    if p["type"] == "image_url"
                ],
                "text": message["content"][-1]["text"],
                "rest": {k: v for k, v in chat.items() if k != "messages"},
            }
        )
    change = wb.store.get("changes", result["changes"])
    observed = {
        "plan": {
            "workflow": run["context"]["workflow"],
            "estimate": run["plan"]["estimate"],
        },
        "requests": requests,
        "records": [wb.store.get("anchored", f"{run['id']}:{ep}") for ep in (0, 1)],
        "proposals": [
            {
                k: p.get(k)
                for k in (
                    "episode_index",
                    "kind",
                    "content",
                    "start",
                    "outcome",
                    "evidence_ids",
                    "evidence_note",
                    "uncertainty",
                )
            }
            for p in sorted(change["proposals"], key=lambda p: p["episode_index"])
        ],
    }
    golden("block-drop-run-before.json", scrub(observed, run["id"]))


def vetoed_spec(camera, **extra):
    """The block-drop spec with a start check (a colour with no block at the
    start is not required) and two vetoes: one read from the event's own
    answer, one with its own question."""
    return spec(
        camera,
        start={
            "views": [
                {"role": "start", "camera": camera, "offsets": [0], "at": "start"}
            ],
            "question": "Which blocks are on the table?",
            "fields": [
                {"name": "red", "enum": ["present", "absent", "unclear"]},
                {"name": "blue", "enum": ["present", "absent", "unclear"]},
            ],
            "waive": [
                {"label": "red", "when": [{"field": "red", "in": ["absent"]}]},
                {"label": "blue", "when": [{"field": "blue", "in": ["absent"]}]},
            ],
            "max_output_tokens": 32,
        },
        vetoes=[
            {
                "id": "wrong_box",
                "ask_when": [{"field": "held", "in": ["yes"]}],
                "views": [{"role": "after", "camera": camera, "offsets": [4]}],
                "question": "Did the block land in the box that was full at the start?",
                "fields": [{"name": "full_box", "enum": ["yes", "no", "unclear"]}],
                "veto_when": [{"field": "full_box", "in": ["yes"]}],
                "max_output_tokens": 16,
            },
            {
                "id": "no_block",
                "effect": "event",
                "veto_when": [{"field": "colour", "in": ["none"]}],
            },
        ],
        **extra,
    )


def test_a_start_check_and_vetoes_are_declared_in_the_spec_only():
    cam = "cam"
    parsed = AnchoredSpec.model_validate(vetoed_spec(cam))
    frozen = anchored.dump(parsed)
    assert [v["id"] for v in frozen["vetoes"]] == ["wrong_box", "no_block"]
    assert frozen["start"]["views"][0]["at"] == "start"
    # Anchor views keep their old frozen form.
    assert "at" not in frozen["views"][0]
    assert "start" not in anchored.dump(AnchoredSpec.model_validate(spec(cam)))
    assert anchored.cameras(frozen) == [cam]
    two = vetoed_spec(cam)
    two["start"]["views"][0]["camera"] = "top"
    assert anchored.cameras(two) == [cam, "top"]
    bad = [
        (
            "unknown field",
            {"vetoes": [{"id": "x", "veto_when": [{"field": "nope", "in": ["a"]}]}]},
        ),
        (
            "cannot take",
            {"vetoes": [{"id": "x", "veto_when": [{"field": "held", "in": ["a"]}]}]},
        ),
        (
            "question with its fields",
            {
                "vetoes": [
                    {
                        "id": "x",
                        "question": "q",
                        "veto_when": [{"field": "held", "in": ["yes"]}],
                    }
                ]
            },
        ),
        (
            "unique",
            {
                "vetoes": [{"id": "x", "veto_when": [{"field": "held", "in": ["yes"]}]}]
                * 2
            },
        ),
    ]
    for message, extra in bad:
        with pytest.raises(ValueError, match=message):
            AnchoredSpec.model_validate(spec(cam, **extra))
    with pytest.raises(ValueError, match="not one of episode.require_labels"):
        broken = vetoed_spec(cam)
        broken["start"]["waive"][0]["label"] = "green"
        AnchoredSpec.model_validate(broken)
    with pytest.raises(ValueError, match="start or end"):
        broken = vetoed_spec(cam)
        broken["start"]["views"][0]["at"] = "anchor"
        AnchoredSpec.model_validate(broken)


def event(answer, verdict, vetoes=None, frame=1):
    out = {
        "frame_index": frame,
        "answer": answer,
        "verdict": verdict,
        "valid": verdict == "supported",
    }
    return out | ({"vetoes": vetoes} if vetoes is not None else {})


def test_a_start_check_waives_labels_and_a_veto_fails_the_episode():
    rules = AnchoredSpec.model_validate(vetoed_spec("cam"))
    red = {"held": "yes", "colour": "red", "in_box": "yes"}
    clear = [
        {"id": "wrong_box", "effect": "episode", "verdict": "cleared"},
        {"id": "no_block", "effect": "event", "verdict": "cleared"},
    ]
    events = [event(red, "supported", clear, 7)]
    # Blue required and missing: failure, as without a start check.
    verdict, basis = outcome(rules, events, {"red": "present", "blue": "present"})
    assert (verdict, basis["missing_labels"], basis["waived_labels"]) == (
        "failure",
        ["blue"],
        [],
    )
    # No blue block at the start: not required.
    verdict, basis = outcome(rules, events, {"red": "present", "blue": "absent"})
    assert (verdict, basis["waived_labels"], basis["vetoes"]) == (
        "success",
        ["blue"],
        [],
    )
    # Cannot tell whether there was one: still required, and undecided.
    verdict, basis = outcome(rules, events, {"red": "present", "blue": "unclear"})
    assert (verdict, basis["undecided_labels"]) == ("failure", ["blue"])
    assert anchored.undecided(verdict, basis)
    # A confirmed veto at any event fails the episode.
    hit = [dict(clear[0], verdict="confirmed"), clear[1]]
    verdict, basis = outcome(
        rules,
        [*events, event(red, "supported", hit, 13)],
        {"red": "present", "blue": "absent"},
    )
    assert verdict == "failure"
    assert basis["vetoes"] == [{"veto": "wrong_box", "frame_index": 13}]
    # An undecided veto leaves a success undecided (not a failure).
    unsure = [dict(clear[0], verdict="undecided"), clear[1]]
    verdict, basis = outcome(
        rules,
        [event(red, "supported", unsure, 7)],
        {"red": "present", "blue": "absent"},
    )
    assert verdict == "success"
    assert basis["undecided_vetoes"] == [{"veto": "wrong_box", "frame_index": 7}]
    assert anchored.undecided(verdict, basis)
    assert not anchored.undecided("failure", basis | {"undecided_labels": []})
    # An event-level veto only makes its event invalid (or unknown).
    assert (
        anchored.settle("supported", [dict(clear[1], verdict="confirmed")])
        == "contradicted"
    )
    assert (
        anchored.settle("supported", [dict(clear[1], verdict="undecided")]) == "unknown"
    )
    assert (
        anchored.settle("contradicted", [dict(clear[1], verdict="undecided")])
        == "contradicted"
    )
    assert (
        anchored.settle("supported", [dict(clear[0], verdict="confirmed")])
        == "supported"
    )
    # A veto without a question reads the event's own answer.
    no_block = rules.vetoes[1]
    assert (
        anchored.veto_verdict(rules, no_block, red | {"colour": "none"})[1]
        == "confirmed"
    )
    assert (
        anchored.veto_verdict(rules, no_block, red | {"colour": "unclear"})[1]
        == "undecided"
    )
    assert anchored.veto_verdict(rules, no_block, red)[1] == "cleared"


def test_a_review_asks_the_start_check_and_the_vetoes(
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
            "anchored": vetoed_spec(camera),
            "require_human_pilot": False,
        },
        budget=Budget(max_calls=20, max_tokens=None, max_seconds=600),
    )
    run = wb.plan(context)
    basis = run["plan"]["estimate"]["basis"]
    assert "veto question asked at it (wrong_box)" in basis
    assert "one start check per episode" in basis
    approve(wb, run["id"], 1, "human")
    replies(
        server,
        [
            # Episode 0: start check, then per event its answer and the
            # question veto (asked only where a block was held).
            {"red": "present", "blue": "absent"},
            {"held": "yes", "colour": "red", "in_box": "yes"},
            {"full_box": "yes"},
            {"held": "no", "colour": "none", "in_box": "no"},
            # Episode 1 (never opens): the start check only.
            {"red": "absent", "blue": "absent"},
        ],
    )
    assert wb.store.claim(run["id"], "owner")
    wb.execute(run["id"], "owner", pilot=False)
    result = wb.store.get("runs", run["id"])
    assert result["status"] == "waiting_for_review", result["reason"]
    texts = [c["messages"][0]["content"][-1]["text"] for c in server.chats()]
    assert texts == [
        "Which blocks are on the table?",
        "Did the gripper put the block down in the box?",
        "Did the block land in the box that was full at the start?",
        "Did the gripper put the block down in the box?",
        "Which blocks are on the table?",
    ]
    assert result["requests"] == 5
    record = wb.store.get("anchored", f"{run['id']}:0")
    assert record["start"]["answer"] == {"blue": "absent", "red": "present"}
    assert [f["frame_index"] for f in record["start"]["frames"]] == [0]
    first, second = record["events"]
    assert [v["verdict"] for v in first["vetoes"]] == ["confirmed", "cleared"]
    assert [f["frame_index"] for f in first["vetoes"][0]["frames"]] == [11]
    assert [v["verdict"] for v in second["vetoes"]] == ["not_asked", "confirmed"]
    assert second["verdict"] == "contradicted"
    # Blue waived and red valid, but the full-box veto fails the episode.
    assert record["outcome"] == "failure"
    assert record["basis"]["waived_labels"] == ["blue"]
    assert record["basis"]["vetoes"] == [{"veto": "wrong_box", "frame_index": 7}]
    change = wb.store.get("changes", result["changes"])
    by_episode = {p["episode_index"]: p for p in change["proposals"]}
    assert "veto wrong_box confirmed" in by_episode[0]["content"]
    assert "start (red=present, blue=absent)" in by_episode[0]["content"]
    assert "vetoed: wrong_box" in by_episode[0]["evidence_note"]
    # The citations lead with the frame the confirmed veto asked about,
    # and the start frame the blue waiver rests on is among them.
    veto_frame = first["vetoes"][0]["frames"][0]["evidence_id"]
    start_frame = record["start"]["frames"][0]["evidence_id"]
    assert by_episode[0]["evidence_ids"][0] == veto_frame
    assert start_frame in by_episode[0]["evidence_ids"]
    # Nothing required at the start and no event: nothing left to do. The
    # episode's last frame is still cited first, then the start frame.
    assert by_episode[1]["outcome"] == "success"
    start_1 = wb.store.get("anchored", f"{run['id']}:1")["start"]["frames"][0]
    items = wb.store.get("evidence", f"{run['id']}:1")["items"]
    end = next(r["id"] for r in items if r["frame_index"] == 19)
    assert by_episode[1]["evidence_ids"] == [end, start_1["evidence_id"]]
    human = Principal("tester", human=True)
    one = invoke(wb, human, "anchored.get", {"run_id": run["id"], "episode": 0})
    assert list(one["start"]["answer"]) == ["red", "blue"]
    assert [v["id"] for v in one["spec"]["vetoes"]] == ["wrong_box", "no_block"]


def test_plates_rule_set_3_is_rule_set_2_plus_a_start_check():
    """The candidate asks exactly rule set 2's question at every release
    (so its release answers equal plates-release's) and waives a colour the
    start check finds already stacked or single."""
    three = anchored.builtin()["plates-release-3"]
    keep = ("anchor", "views", "question", "fields", "valid_when", "episode")
    assert {k: getattr(three, k) for k in keep} == {k: getattr(PLATES, k) for k in keep}
    assert three.vetoes == []
    assert [v.at for v in three.start.views] == ["start"]
    ok = {
        "held_before": "yes",
        "plate_colour": "pink",
        "landed_on": "same_colour_plate",
        "stays": "yes",
    }
    events = [{"answer": ok, "valid": True, "verdict": "supported"}]
    assert (
        outcome(
            three, events, {"pink": "two_or_more_apart", "white": "already_stacked"}
        )[0]
        == "success"
    )
    assert (
        outcome(three, events, {"pink": "two_or_more_apart", "white": "one_or_none"})[0]
        == "success"
    )
    verdict, basis = outcome(
        three, events, {"pink": "two_or_more_apart", "white": "unclear"}
    )
    assert (verdict, basis["undecided_labels"]) == ("failure", ["white"])
    assert (
        outcome(
            three, events, {"pink": "two_or_more_apart", "white": "two_or_more_apart"}
        )[0]
        == "failure"
    )


def plate(colour, verdict, **answer):
    """One plates release event: a clean same-colour stack unless ``answer``
    says otherwise."""
    return event(
        {
            "held_before": "yes",
            "plate_colour": colour,
            "landed_on": "same_colour_plate",
            "stays": "yes",
        }
        | answer,
        verdict,
    )


def test_a_waiver_its_own_events_contest_leaves_the_success_undecided():
    """The reviewer's case: the start check says at most one white plate, yet
    one white release is contradicted (on the table) and another unknown. The
    waiver still decides the outcome, but the success is not clean: it is
    named, undecided and in the proposal's uncertainty."""
    three = anchored.builtin()["plates-release-3"]
    start = {"pink": "two_or_more_apart", "white": "one_or_none"}
    events = [
        plate("pink", "supported"),
        plate("white", "contradicted", landed_on="table"),
        plate("white", "unknown", held_before="unclear"),
    ]
    verdict, basis = outcome(three, events, start)
    assert verdict == "success"
    assert basis["waived_labels"] == ["white"]
    assert basis["contested_waivers"] == ["white"]
    assert basis["redundant_waivers"] == []
    assert anchored.undecided(verdict, basis)
    # No white release at all: the waiver decides, uncontested, clean.
    verdict, basis = outcome(three, events[:1], start)
    assert (verdict, basis["waived_labels"], basis["contested_waivers"]) == (
        "success",
        ["white"],
        [],
    )
    assert not anchored.undecided(verdict, basis)
    # A valid white release anyway: the waiver changed nothing.
    both = [*events, plate("white", "supported")]
    verdict, basis = outcome(three, both, start)
    assert verdict == "success"
    assert (basis["waived_labels"], basis["redundant_waivers"]) == ([], ["white"])
    assert basis["contested_waivers"] == []
    assert not anchored.undecided(verdict, basis)
    # A failure for another reason stays a plain failure.
    verdict, basis = outcome(three, events[1:], start)
    assert (verdict, basis["missing_labels"]) == ("failure", ["pink"])
    assert not anchored.undecided(verdict, basis | {"undecided_labels": []})


def test_the_candidate_spec_is_marked_and_never_a_default(client):
    from levi import service
    from levi.agent.runtime import Workbench

    three = anchored.builtin()["plates-release-3"]
    assert (three.status, PLATES.status) == ("candidate", "stable")
    assert anchored.dump(three)["status"] == "candidate"
    assert "status" not in anchored.dump(PLATES)
    with pytest.raises(ValueError, match="status"):
        AnchoredSpec.model_validate(spec("cam", status="beta"))
    wb = Workbench(service.STATE)
    listed = invoke(wb, Principal("tester", human=True), "anchored.specs", {})
    statuses = [(s["id"], s["status"]) for s in listed["specs"]]
    # Stable first; a candidate is labelled and the instructions forbid it
    # as a default.
    assert statuses[0] == ("plates-release", "stable")
    assert ("plates-release-3", "candidate") in statuses
    assert statuses.index(("plates-release-3", "candidate")) > 0
    assert "never choose it by default" in listed["use"]


def test_a_plan_names_the_candidate_and_counts_the_start_check(
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

    def plan(anchored_spec):
        return wb.plan(
            TaskContext(
                repo_id=entry["id"],
                episodes=[0, 1],
                instruction="Judge each release",
                provider="vllm",
                cameras=[camera],
                allow_media_egress=True,
                workflow={"kind": "review", "anchored": anchored_spec},
                budget=Budget(max_calls=20, max_tokens=None, max_seconds=600),
            )
        )["plan"]

    checked = plan(vetoed_spec(camera, status="candidate"))
    assert checked["estimate"]["minimum_requests"] == 2
    assert checked["anchored_spec"] == {
        "id": "block-drop",
        "version": 1,
        "status": "candidate",
    }
    plain = plan(spec(camera))
    assert plain["estimate"]["minimum_requests"] == 0
    assert plain["anchored_spec"]["status"] == "stable"


def test_loose_spec_parts_are_refused():
    cam = "cam"
    no_question = {"id": "x", "veto_when": [{"field": "held", "in": ["yes"]}]}
    cases = [
        ("no question to ask", {"vetoes": [no_question | {"max_output_tokens": 50}]}),
        ("Veto 'x' title", {"vetoes": [no_question | {"title": {"en": " "}}]}),
        ("Veto 'x' title", {"vetoes": [no_question | {"title": {}}]}),
        (
            "views are at the anchor",
            {"views": [{"role": "front", "camera": cam, "offsets": [0], "at": "end"}]},
        ),
    ]
    for message, extra in cases:
        with pytest.raises(ValueError, match=message):
            AnchoredSpec.model_validate(spec(cam, **extra))
    twice = vetoed_spec(cam)
    twice["start"]["waive"].append(twice["start"]["waive"][0])
    with pytest.raises(ValueError, match="names a label twice"):
        AnchoredSpec.model_validate(twice)
    # A veto read from the event's answer freezes without an answer budget,
    # and its frozen form validates again.
    frozen = anchored.dump(AnchoredSpec.model_validate(vetoed_spec(cam)))
    assert "max_output_tokens" not in frozen["vetoes"][1]
    AnchoredSpec.model_validate(frozen)
    # A veto's own views may look at the episode's start or end.
    AnchoredSpec.model_validate(
        spec(
            cam,
            vetoes=[
                {
                    "id": "x",
                    "title": {"en": "Start box"},
                    "views": [
                        {"role": "s", "camera": cam, "offsets": [0], "at": "start"}
                    ],
                    "question": "q",
                    "fields": [{"name": "full", "enum": ["yes", "no", "unclear"]}],
                    "veto_when": [{"field": "full", "in": ["yes"]}],
                }
            ],
        )
    )


def test_a_contested_waiver_is_in_the_proposals_uncertainty(
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
    checked = vetoed_spec(camera)
    checked.pop("vetoes")
    context = TaskContext(
        repo_id=entry["id"],
        episodes=[0],
        instruction="Judge each release",
        provider="vllm",
        cameras=[camera],
        allow_media_egress=True,
        workflow={
            "kind": "review",
            "anchored": checked,
            "require_human_pilot": False,
        },
        budget=Budget(max_calls=20, max_tokens=None, max_seconds=600),
    )
    run = wb.plan(context)
    approve(wb, run["id"], 1, "human")
    replies(
        server,
        [
            # No blue block at the start, yet a blue block is dropped outside
            # the box.
            {"red": "present", "blue": "absent"},
            {"held": "yes", "colour": "red", "in_box": "yes"},
            {"held": "yes", "colour": "blue", "in_box": "no"},
        ],
    )
    assert wb.store.claim(run["id"], "owner")
    wb.execute(run["id"], "owner", pilot=False)
    result = wb.store.get("runs", run["id"])
    assert result["status"] == "waiting_for_review", result["reason"]
    record = wb.store.get("anchored", f"{run['id']}:0")
    assert record["outcome"] == "success"
    assert record["basis"]["contested_waivers"] == ["blue"]
    (proposal,) = wb.store.get("changes", result["changes"])["proposals"]
    assert "not valid: blue" in proposal["uncertainty"]
    assert record["start"]["frames"][0]["evidence_id"] in proposal["evidence_ids"]


def test_on_a_table_with_dropped_frames_views_in_seconds_follow_timestamps(
    client,
    dataset,
    server,  # noqa: F811 - the fixture imported above
):
    """T-A-05 through review_episode: episode 0 lost frames 9-11 (rows gone,
    video intact), so its table is uneven and every view given in seconds --
    the spec's, the start check's, a veto's -- shows the frame nearest the
    asked time, and the record says so. Episode 1 is even: as before, and
    its record has no new key."""
    from levi import catalog, service
    from levi.agent.runtime import Workbench

    camera = camera_dataset(dataset)
    gripper_dataset(dataset)
    path = dataset / "data/chunk-000/episode_000000.parquet"
    frame = pd.read_parquet(path)
    frame = frame[~frame.frame_index.isin([9, 10, 11])].reset_index(drop=True)
    frame.to_parquet(path)
    seconds = vetoed_spec(camera)
    seconds["views"] = [
        {"role": "front", "camera": camera, "offsets_seconds": [-0.2, 0.0, 0.4]}
    ]
    seconds["start"]["views"] = [
        {"role": "start", "camera": camera, "offsets_seconds": [0.0, -0.8], "at": "end"}
    ]
    seconds["vetoes"][0]["views"] = [
        {"role": "after", "camera": camera, "offsets_seconds": [0.5]}
    ]
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
        workflow={"kind": "review", "anchored": seconds, "require_human_pilot": False},
        budget=Budget(max_calls=20, max_tokens=None, max_seconds=600),
    )
    run = wb.plan(context)
    approve(wb, run["id"], 1, "human")
    replies(
        server,
        [
            {"red": "present", "blue": "absent"},
            {"held": "yes", "colour": "red", "in_box": "yes"},
            {"full_box": "no"},
            {"held": "no", "colour": "none", "in_box": "no"},
            {"red": "absent", "blue": "absent"},
        ],
    )
    assert wb.store.claim(run["id"], "owner")
    wb.execute(run["id"], "owner", pilot=False)
    result = wb.store.get("runs", run["id"])
    assert result["status"] == "waiting_for_review", result["reason"]

    def shown(frames):
        return [f["frame_index"] for f in frames]

    record = wb.store.get("anchored", f"{run['id']}:0")
    assert record["offset_timing"] == "timestamps"
    assert [e["frame_index"] for e in record["events"]] == [7, 13]
    first, second = record["events"]
    # Rows 0-8 are frames 0-8, rows 9-16 frames 12-19 (t = frame / 10).
    # At 0.7 s: 0.5 s, 0.7 s, 1.1 s -> frame 12 (by rows it was frame 14).
    assert shown(first["frames"]) == [5, 7, 12]
    # At 1.3 s: 1.1 s -> 12 (by rows: 8), 1.3 s, 1.7 s.
    assert shown(second["frames"]) == [12, 13, 17]
    # The start check from the last frame: 1.9 s and 1.1 s -> 12 (by rows: 8).
    assert shown(record["start"]["frames"]) == [19, 12]
    # The veto's own view, 0.5 s after 0.7 s: frame 12 (by rows: 15).
    assert shown(first["vetoes"][0]["frames"]) == [12]
    assert second["vetoes"][0]["verdict"] == "not_asked"
    # What was sampled: every view at every anchor (the veto's too, asked or
    # not: 1.8 s after the second anchor is frame 18), by timestamp.
    items = wb.store.get("evidence", f"{run['id']}:0")["items"]
    assert sorted(r["frame_index"] for r in items) == [5, 7, 12, 13, 17, 18, 19]
    # The offsets a record names frames by stay the nominal frame offsets.
    assert [f["offset"] for f in first["frames"]] == [-2, 0, 4]

    even = wb.store.get("anchored", f"{run['id']}:1")
    assert "offset_timing" not in even
    assert shown(even["start"]["frames"]) == [19, 11]
