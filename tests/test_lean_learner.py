"""The lean learner: a local model gets the plan's instruction as text and a
frame list, answers without citations, and LEVI cites the frames itself."""

import json

from levi.agent.schema import ModelOutput, ProviderConfig
from levi.inference.provider import (
    LEAN_FILLED,
    LEAN_REFINED,
    LocalProvider,
    cite_frames,
    lean_content,
    lean_fill,
    learner_schema,
    parse_answer,
    salvage,
)

FLOW = {
    "kind": "temporal",
    "definitions": [
        {
            "id": sid,
            "label": sid,
            "definition": sid,
            "starts_when": "a",
            "ends_when": "b",
            "success_when": "c",
        }
        for sid in ("grasp", "place")
    ],
    "coarse_step_seconds": 0.5,
    "boundary_window_seconds": 1.0,
}


def rows(episode=0, times=(0.0, 0.5, 1.0, 1.5)):
    return [
        {
            "id": f"episode_{episode:06d}--cam--frame_{int(t * 10):06d}",
            "episode_index": episode,
            "timestamp": t,
            "artifact": f"f{int(t * 10)}.png",
        }
        for t in times
    ]


def test_a_lean_schema_asks_for_intervals_without_citations():
    full = learner_schema(FLOW)["$defs"]["Proposal_segment"]
    lean = learner_schema(FLOW, lean=True)["$defs"]["Proposal_segment"]
    assert {"evidence_ids", "evidence_note"} <= set(full["required"])
    assert not {"evidence_ids", "evidence_note"} & set(lean["required"])
    assert {"subtask_id", "start", "end", "outcome"} <= set(lean["required"])
    assert lean["properties"]["content"]["maxLength"] == 120
    # What LEVI fills in is not decoded at all: every field costs output
    # tokens on every proposal. The plan allows segments only: no kind either.
    assert not set(LEAN_FILLED) & set(lean["properties"])
    assert "kind" not in lean["properties"]
    assert lean["additionalProperties"] is False
    # Without definitions both kinds stay possible, so the kind is written.
    free = learner_schema({"kind": "temporal"}, lean=True)["$defs"]
    assert "kind" in free["Proposal_segment"]["properties"]


def test_a_lean_refinement_writes_only_boundaries_and_outcome():
    draft = [
        {
            "kind": "segment",
            "subtask_id": "grasp",
            "start": 0.0,
            "end": 1.0,
            "outcome": "success",
            "content": "grasps it",
            "episode_index": 4,
        },
        {
            "kind": "segment",
            "subtask_id": "place",
            "start": 1.0,
            "end": 1.5,
            "outcome": "unknown",
            "content": "puts it down",
            "episode_index": 4,
        },
    ]
    schema = learner_schema(FLOW, draft, lean=True)
    items = schema["properties"]["proposals"]["prefixItems"]
    for number in range(2):
        shape = schema["$defs"][f"Draft_{number}"]
        assert set(shape["properties"]) == set(LEAN_REFINED)
        assert set(shape["required"]) == set(LEAN_REFINED)
    assert len(items) == 2
    full = learner_schema(FLOW, draft)["$defs"]["Draft_0"]["properties"]
    assert {"subtask_id", "content"} <= set(full)
    summary = {"episode_index": 4, "workflow": FLOW}
    answer = json.dumps(
        {
            "proposals": [
                {"start": 0.2, "end": 1.1, "outcome": "success"},
                {"start": 1.1, "end": 1.5, "outcome": "failure"},
            ],
            "summary": "s",
            "warnings": [],
        }
    )
    out = parse_answer(answer, lean_fill(summary, FLOW, draft))
    first, second = out.proposals
    assert (first.subtask_id, first.content, first.kind) == (
        "grasp",
        "grasps it",
        "segment",
    )
    assert (first.start, first.end, first.episode_index) == (0.2, 1.1, 4)
    assert (second.subtask_id, second.outcome) == ("place", "failure")
    # A cut-off lean answer is completed the same way before salvage.
    cut = answer[: answer.index('{"start": 1.1')]
    kept = salvage(cut, lean_fill(summary, FLOW, draft))
    assert [(p.subtask_id, p.start) for p in kept.proposals] == [("grasp", 0.2)]


def test_what_the_learner_wrote_is_kept():
    fill = lean_fill({"episode_index": 2}, FLOW)
    [item] = fill([{"episode_index": 5, "kind": "segment", "start": 0.0}])
    assert item["episode_index"] == 5
    [item] = fill([{"start": 0.0}])
    assert (item["episode_index"], item["kind"]) == (2, "segment")


def test_a_lean_request_is_the_instruction_and_a_frame_list():
    summary = {"episode_index": 3, "start": 0.0, "end": 1.5, "workflow": FLOW}
    text = lean_content("# Guideline\n| id | when |", summary, rows(3))
    assert text.startswith(
        "# Guideline\n| id | when |"
    )  # markdown as written, not escaped
    assert "image 1 = 0.00 s, image 2 = 0.50 s" in text
    assert "Subtask ids: grasp, place, unknown" in text
    assert '"goal"' not in text and "Draft to refine" not in text
    draft = {
        "candidate_draft": {
            "proposals": [
                {
                    "kind": "segment",
                    "subtask_id": "grasp",
                    "start": 0.0,
                    "end": 1.0,
                    "outcome": "success",
                }
            ]
        }
    }
    refine = lean_content("# G", {**summary, **draft}, rows(3))
    assert "## Draft to refine" in refine and "1. grasp 0-1.0 s success" in refine


def test_levi_cites_the_frames_inside_each_interval():
    out = ModelOutput(
        summary="s",
        proposals=[
            {
                "episode_index": 0,
                "kind": "segment",
                "subtask_id": "grasp",
                "content": "grasps the pink plate",
                "start": 0.4,
                "end": 1.2,
                "outcome": "success",
            },
            {
                "episode_index": 0,
                "kind": "segment",
                "subtask_id": "place",
                "content": "lowers it",
                "start": 1.6,
                "end": 1.7,
                "outcome": "unknown",
            },
        ],
    )
    cited = cite_frames(out, rows()).proposals
    assert cited[0].evidence_ids == [
        "episode_000000--cam--frame_000005",
        "episode_000000--cam--frame_000010",
    ]
    assert cited[0].evidence_note == "grasps the pink plate"
    # No frame inside: the one nearest its start.
    assert cited[1].evidence_ids == ["episode_000000--cam--frame_000015"]
    assert cited[1].evidence_note == "lowers it"


def test_a_lean_provider_sends_no_overview_and_no_json_document(tmp_path, monkeypatch):
    for name in ("f0.png", "f5.png"):
        (tmp_path / name).write_bytes(b"\x89PNG")
    config = ProviderConfig(
        name="v",
        kind="openai-local",
        base_url="http://127.0.0.1:8100",
        model="m",
        allow_localhost=True,
        structured_output=True,
        vision=True,
        model_digest="d" * 64,
        prompt_style="lean",
    )
    sent = {}

    def chat(
        self,
        config,
        content,
        messages,
        budget,
        workflow=None,
        draft=None,
        evidence_ids=None,
        expected=None,
        lean=False,
    ):
        sent.update(messages=messages, lean=lean)
        answer = {
            "summary": "s",
            "warnings": [],
            "proposals": [
                {
                    "subtask_id": "grasp",
                    "content": "grasp",
                    "start": 0.0,
                    "end": 0.5,
                    "outcome": "success",
                }
            ],
        }
        return {
            "content": json.dumps(answer),
            "tool_calls": [],
            "usage": {"tokens": 10, "source": "reported", "prompt_tokens": 8},
        }

    monkeypatch.setattr(LocalProvider, "_chat", chat)
    monkeypatch.setattr("levi.inference.gpu.require_free", lambda config: None)
    monkeypatch.setattr(
        "levi.inference.provider.encode_image", lambda path, config: "img"
    )
    from levi.agent.schema import Budget

    output, _ = LocalProvider().generate(
        config,
        "# Guideline",
        {"episode_index": 0, "start": 0.0, "end": 0.5, "workflow": FLOW},
        rows(times=(0.0, 0.5)),
        tmp_path,
        Budget(max_calls=1),
    )
    system, user = sent["messages"][0]["content"], sent["messages"][1]["content"]
    assert sent["lean"] is True
    assert "LEVI overview" not in system and user.startswith("# Guideline")
    assert output.proposals[0].evidence_ids == ["episode_000000--cam--frame_000000"]
    assert (output.proposals[0].episode_index, output.proposals[0].kind) == (
        0,
        "segment",
    )
    assert output.proposals[0].evidence_note == "grasp"


def test_auto_refinement_skips_a_dense_coarse_pass():
    from levi.agent.runtime import refines

    assert refines({"coarse_step_seconds": 1.0})  # default: always
    assert not refines(
        {"refine": "auto", "coarse_step_seconds": 0.5, "boundary_window_seconds": 1.0}
    )
    assert refines(
        {"refine": "auto", "coarse_step_seconds": 1.0, "boundary_window_seconds": 1.0}
    )
    assert refines({"refine": "always", "coarse_step_seconds": 0.5})
