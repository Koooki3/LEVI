"""The teacher/learner loop: briefs, teaching memory, grading, NL task specs."""

import json

import pytest

from levi.harness import context, grading, tasking, teaching
from levi.harness.layout import memory_path, write_json


def seg(sub, start, end, outcome="success"):
    return {
        "subtask": sub,
        "start": start,
        "end": end,
        "outcome": outcome,
        "content": sub,
    }


def test_identical_annotations_agree_and_a_wrong_outcome_does_not():
    ref = {
        "task": "t",
        "episodes": {
            "episode_000000": [seg("grasp", 0, 2), seg("place", 2, 4, "failure")]
        },
    }
    same = grading.grade(ref, {"episode_000000": ref["episodes"]["episode_000000"]})
    assert same["agreement"] and same["overall"]["segment_f1"] == 1.0
    flipped = grading.grade(
        ref,
        {"episode_000000": [seg("grasp", 0.2, 2.1), seg("place", 2.1, 4, "success")]},
    )
    assert not flipped["agreement"]
    # The subtasks cover the same time; half of it has the wrong outcome.
    assert flipped["overall"]["time_accuracy"] >= 0.9
    assert flipped["overall"]["outcome_time_accuracy"] < 0.6
    assert flipped["overall"]["boundary_mae"] < 0.2
    missing = grading.grade(ref, {})
    assert missing["overall"]["segment_f1"] == 0.0


def run(state, episodes=(0, 1), frozen_at=None):
    return {
        "dataset_key": "plates",
        "context": {"episodes": list(episodes), "workflow": {"kind": "temporal"}},
        "harness": {"memory": None, "frozen_at": frozen_at},
    }


def test_the_brief_never_hands_the_learner_its_own_episodes(tmp_path):
    state = tmp_path / "workbench"
    write_json(
        memory_path(state, "plates"),
        {
            "episodes": {
                "episode_000000": {"segments": [seg("place", 1, 2)]},
                "episode_000005": {"segments": [seg("place", 3, 4, "failure")]},
            },
            "teaching": [
                {
                    "note": "Green on pink is a failure.",
                    "workflow": "temporal",
                    "count": 2,
                    "at": 1.0,
                }
            ],
        },
    )
    value = context.brief(state, run(state))
    names = [e["episode"] for e in value["examples_from_other_episodes"]]
    assert names == ["episode_000005"]
    assert value["teacher_notes"] == ["(2x) Green on pink is a failure."]
    # A note written after the plan froze reaches the next task, not this one.
    assert "teacher_notes" not in context.brief(state, run(state, frozen_at=0.5))
    assert context.as_text(value).startswith("\n\n## LEVI local memory")


def test_feedback_becomes_a_file_and_a_deduplicated_note(tmp_path):
    class Store:
        state = tmp_path / "workbench"

    record = {
        "run_id": "temporal-1",
        "episode": 3,
        "phase": "refine",
        "mode": "supervised",
        "learner_output": {"proposals": []},
        "accepted_output": {"proposals": []},
        "evidence": [{"id": "e1"}],
        "feedback": {
            "decision": "revise",
            "note": "A hand entering the frame ends the attempt: outcome unknown.",
        },
    }
    path = teaching.record(Store, record, "plates", "temporal")
    assert path.name == "episode_000003-refine.json"
    assert path.parent.name == "temporal-1" and path.parent.parent.name == "teaching"
    teaching.record(Store, {**record, "run_id": "temporal-2"}, "plates", "temporal")
    notes = json.loads(memory_path(Store.state, "plates").read_text())["teaching"]
    assert len(notes) == 1 and notes[0]["count"] == 2
    # "Looks right" on an accepted phase teaches nothing.
    teaching.record(
        Store,
        {**record, "feedback": {"decision": "accept", "note": "ok"}},
        "plates",
        "temporal",
    )
    assert (
        len(json.loads(memory_path(Store.state, "plates").read_text())["teaching"]) == 1
    )


def test_a_task_spec_is_checked_against_the_catalog(monkeypatch):
    monkeypatch.setattr(
        tasking,
        "catalog_digest",
        lambda: [
            {
                "id": "local/plates",
                "episodes": 12,
                "fps": 10,
                "cameras": ["view1", "hand"],
                "kind": "raw",
            }
        ],
    )
    good = tasking.TaskSpec.model_validate(
        {
            "dataset": "local/plates",
            "steps": [
                {"kind": "quality"},
                {
                    "kind": "annotate",
                    "workflow": "temporal",
                    "episodes": list(range(10)),
                    "cameras": ["view1"],
                    "instruction": "Mark attempts.",
                    "definitions": [
                        {
                            "id": "place",
                            "label": "place",
                            "definition": "d",
                            "starts_when": "s",
                            "ends_when": "e",
                            "success_when": "w",
                        }
                    ],
                },
            ],
            "report": ["tokens", "time"],
        }
    )
    assert tasking.check(good)[1] == []
    bad = good.model_copy(deep=True)
    bad.steps[1].episodes = [0, 12]
    bad.steps[1].cameras = ["front"]
    problems = tasking.check(bad)[1]
    assert any("out of range" in p for p in problems)
    assert any("unknown cameras" in p for p in problems)
    with pytest.raises(ValueError):
        tasking.TaskSpec.model_validate({"dataset": "local/plates", "steps": []})


def test_review_grading_counts_task_identity_and_outcome():
    def p(ep, kind, outcome=None):
        return {"episode_index": ep, "kind": kind, "outcome": outcome, "content": kind}

    ref = {
        "task": "t",
        "episodes": grading.review_verdicts(
            [
                p(0, "outcome", "unknown"),
                p(0, "issue"),
                p(1, "outcome", "success"),
                p(2, "outcome", "failure"),
            ]
        ),
    }
    perfect = grading.grade_review(ref, ref["episodes"])
    assert perfect["agreement"] and perfect["overall"] == {
        "task_match_accuracy": 1.0,
        "outcome_accuracy": 1.0,
    }
    wrong = grading.review_verdicts(
        [
            p(0, "outcome", "failure"),
            p(1, "outcome", "failure"),
            p(2, "outcome", "failure"),
        ]
    )
    graded = grading.grade_review(ref, wrong)
    assert graded["overall"]["task_match_accuracy"] == round(2 / 3, 3)
    assert graded["overall"]["outcome_accuracy"] == 0.5 and not graded["agreement"]


def test_rebuilding_memory_keeps_the_teachers_notes(tmp_path):
    from levi.harness import memory

    class Store:
        state = tmp_path / "outputs/LEVI/workbench"

        def list(self, kind):
            return []

    key = "plates"
    write_json(
        memory_path(Store.state, key),
        {
            "teaching": [{"note": "same colour only", "count": 2}],
            "lessons": [],
            "episodes": {},
        },
    )
    memory.rebuild(Store(), key)
    assert memory.load(Store.state, key)["teaching"][0]["note"] == "same colour only"


def test_the_learner_can_only_emit_kinds_and_subtasks_the_plan_allows():
    from levi.inference.provider import learner_schema

    flow = {"kind": "temporal", "definitions": [{"id": "grasp"}, {"id": "place"}]}
    proposal = learner_schema(flow)["$defs"]["Proposal"]["properties"]
    # With subtask definitions the plan asks for intervals only.
    assert proposal["kind"]["enum"] == ["segment"]
    free = learner_schema({"kind": "temporal"})["$defs"]["Proposal"]["properties"]
    assert free["kind"]["enum"] == ["segment", "event"]
    allowed = proposal["subtask_id"]["anyOf"][0]["enum"]
    assert allowed[:2] == ["grasp", "place"] and "other" in allowed
    review = learner_schema({"kind": "review"})["$defs"]["Proposal"]["properties"]
    assert review["kind"]["enum"] == ["outcome", "issue"]
    # An empty answer with the story told in the summary cannot be decoded.
    assert learner_schema(flow)["properties"]["proposals"]["minItems"] == 1
    # The answer is decoded before the prose, and the prose is short.
    fields = learner_schema(flow)["properties"]
    assert next(iter(fields)) == "proposals" and fields["summary"]["maxLength"] <= 400
    # A segment cannot be decoded without an end and an outcome; an event
    # cannot carry an end.
    defs = learner_schema(flow)["$defs"]
    segment = defs["Proposal_segment"]
    event = learner_schema({"kind": "temporal"})["$defs"]["Proposal_event"]
    assert {"end", "outcome", "evidence_note"} <= set(segment["required"])
    assert segment["properties"]["end"]["type"] == "number"
    assert event["properties"]["end"] == {"type": "null"}
    # ...nor with a null subtask, which the plan's generic field allows.
    assert "subtask_id" in segment["required"]
    assert segment["properties"]["subtask_id"]["type"] == "string"
    assert "null" not in json.dumps(segment["properties"]["subtask_id"])
    assert list(segment["properties"])[:4] == [
        "episode_index",
        "kind",
        "subtask_id",
        "start",
    ]


def test_a_rejected_learner_answer_is_kept(tmp_path, monkeypatch):
    from levi.agent.schema import ProviderConfig
    from levi.inference.provider import InvalidAnswer, LocalProvider

    monkeypatch.setattr(
        LocalProvider,
        "_chat",
        staticmethod(
            lambda *a, **k: {
                "content": '{"summary": "x", "proposals": [{"kind": "subtask"}]}',
                "tool_calls": [],
                "usage": {"tokens": 10, "source": "reported"},
            }
        ),
    )
    cfg = ProviderConfig(
        name="q",
        kind="ollama",
        base_url="http://127.0.0.1:1",
        model="m",
        model_digest="a" * 64,
        structured_output=True,
        allow_localhost=True,
    )
    budget = type("B", (), {"max_tokens": 100, "max_seconds": 5})()
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    with pytest.raises(InvalidAnswer, match="proposals.0") as caught:
        LocalProvider().generate(
            cfg, "goal", {"episode_index": 3, "workflow": {}}, [], evidence_dir, budget
        )
    # The answer and what it cost travel with the error.
    assert caught.value.usage["tokens"] == 10 and '"subtask"' in caught.value.raw
    kept = tmp_path / "episode_000003-coarse-rejected.json"
    assert '"subtask"' in kept.read_text()


def test_an_end_written_as_a_decimal_snaps_to_the_float32_last_frame():
    from levi.agent.runtime import snap_to_episode
    from levi.agent.schema import ModelOutput

    def seg(end):
        return {
            "episode_index": 0,
            "kind": "segment",
            "content": "x",
            "start": 10.0,
            "end": end,
            "evidence_ids": ["e"],
        }

    last = 11.199999809265137
    output = ModelOutput(summary="s", proposals=[seg(11.2), seg(11.5)])
    snapped = snap_to_episode(output, {"end": last})
    assert [p.end for p in snapped.proposals] == [last, 11.5]


def test_an_edge_start_the_phases_episode_and_stray_candidates_are_snapped():
    import pytest

    from levi.agent.runtime import Workbench, snap_to_episode
    from levi.agent.schema import ModelOutput, TaskContext

    first, last = 5.0, 21.899999618530273

    def outcome(**extra):
        return {
            "episode_index": 3,
            "kind": "outcome",
            "content": "final state",
            "start": 21.9,
            "outcome": "failure",
            "evidence_ids": ["e"],
            **extra,
        }

    output = ModelOutput(
        summary="s",
        proposals=[
            outcome(),
            outcome(
                episode_index=7,
                start=4.9996,
                boundary_candidates=[6.0, 30.0, 1.0, 21.9],
            ),
        ],
    )
    summary = {"episode_index": 3, "start": first, "end": last}
    snapped = snap_to_episode(output, summary)
    # A start written as 21.9 for a last frame at 21.8999996 s, or a hair
    # before the first frame, sits on the frame; the episode is the phase's.
    assert [p.start for p in snapped.proposals] == [last, first]
    assert {p.episode_index for p in snapped.proposals} == {3}
    assert snapped.proposals[1].boundary_candidates == [6.0, last]
    context = TaskContext(
        repo_id="local/d",
        episodes=[3],
        instruction="i",
        provider="p",
        workflow={"kind": "review"},
    )
    Workbench.validate_proposals(
        context, snapped.proposals, [{"id": "e", "episode_index": 3}], summary
    )
    # Citing another episode's evidence is still refused.
    with pytest.raises(ValueError, match="from episode 4"):
        Workbench.validate_proposals(
            context, snapped.proposals, [{"id": "e", "episode_index": 4}], summary
        )


def test_a_first_temporal_pass_has_room_for_an_interval_every_two_seconds(
    monkeypatch,
):
    from types import SimpleNamespace

    from levi.agent.schema import ProviderConfig
    from levi.inference import provider

    config = ProviderConfig(
        name="q",
        kind="ollama",
        base_url="http://127.0.0.1:1",
        model="m",
        context_tokens=32768,
    )
    summary = {"workflow": {"kind": "temporal"}, "start": 0.0, "end": 16.9}
    assert provider.expected_proposals(summary, None) == 9
    assert provider.expected_proposals(summary, [{}, {}]) == 2
    review = {"workflow": {"kind": "review"}, "start": 0.0, "end": 30.0}
    assert provider.expected_proposals(review, None) is None
    seen = {}

    class Client:
        def chat(self, *args, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(provider, "client_for", lambda config, timeout: Client())
    budget = SimpleNamespace(max_seconds=60, max_tokens=100_000)
    provider.LocalProvider._chat(
        config, "", [], budget, summary["workflow"], None, None, 9
    )
    # The ten intervals a 27B model found in 16.9 s no longer hit the cut.
    assert seen["max_output_tokens"] == 400 + 260 * 9 > 2048


def test_a_refinement_keeps_the_drafts_intervals_and_subtasks():
    from levi.inference.provider import learner_schema, pinned_draft

    flow = {"kind": "temporal", "definitions": [{"id": "grasp"}, {"id": "place"}]}
    draft = {
        "proposals": [
            {"kind": "segment", "subtask_id": "grasp", "start": 0.0},
            {"kind": "segment", "subtask_id": "place", "start": 4.0},
        ]
    }
    summary = {"candidate_draft": draft}
    pinned = learner_schema(flow, pinned_draft(summary))
    proposals = pinned["properties"]["proposals"]
    assert proposals["minItems"] == proposals["maxItems"] == 2
    second = pinned["$defs"][proposals["prefixItems"][1]["$ref"].split("/")[-1]]
    assert second["properties"]["subtask_id"]["enum"] == ["place"]
    # One batch of a long episode pins only the proposals in its window.
    window = {**summary, "refine_only": {"start": 3.0, "end": 9.0}}
    assert [p["subtask_id"] for p in pinned_draft(window)] == ["place"]
    assert pinned_draft({}) is None


def test_the_answer_budget_fits_the_proposals_and_drops_default_fields():
    from levi.agent.schema import ProviderConfig
    from levi.inference.provider import learner_schema, output_allowance

    config = ProviderConfig(
        name="q",
        kind="ollama",
        base_url="http://127.0.0.1:1",
        model="m",
        context_tokens=32768,
    )
    assert output_allowance(config) == 2048
    assert output_allowance(config, 10) > 2048 + 500
    assert output_allowance(config, 200) == 32768 // 4
    flow = {"kind": "temporal", "definitions": [{"id": "grasp"}]}
    fields = learner_schema(flow)["$defs"]["Proposal_segment"]["properties"]
    assert not {"attempt", "layer", "style"} & set(fields)


def test_only_supplied_or_drafted_evidence_can_be_cited():
    from levi.inference.provider import citable, learner_schema

    summary = {"candidate_draft": {"proposals": [{"evidence_ids": ["coarse-1"]}]}}
    ids = citable(summary, [{"id": "dense-1"}, {"id": "dense-2"}])
    assert ids == ["dense-1", "dense-2", "coarse-1"]
    flow = {"kind": "temporal", "definitions": [{"id": "grasp"}]}
    shape = learner_schema(flow, None, ids)["$defs"]["Proposal_segment"]
    assert shape["properties"]["evidence_ids"]["items"]["enum"] == ids


def test_the_valid_part_of_a_rejected_answer_survives():
    import json as _json

    from levi.inference.provider import salvage

    good = {
        "episode_index": 0,
        "kind": "segment",
        "content": "a",
        "start": 0.0,
        "end": 1.0,
        "evidence_ids": ["e"],
    }
    raw = _json.dumps(
        {"summary": "s", "proposals": [good, {**good, "start": 2.0, "end": 2.0}]}
    )
    kept = salvage(raw)
    assert [p.start for p in kept.proposals] == [0.0]
    assert salvage("{not json") is None


def test_the_finished_proposals_of_a_cut_off_answer_survive():
    import json as _json

    from levi.inference.provider import salvage

    good = {
        "episode_index": 0,
        "kind": "segment",
        "content": "a",
        "start": 0.0,
        "end": 1.0,
        "evidence_ids": ["e"],
    }
    full = _json.dumps(
        {
            "proposals": [good, {**good, "start": 1.0, "end": 2.0}],
            "summary": "s",
            "warnings": ["a long warning"],
        },
        indent=2,
    )
    # Cut off in the warnings (the output allowance ran out): both kept.
    cut = full[: full.index("a long") + 3]
    assert [p.start for p in salvage(cut).proposals] == [0.0, 1.0]
    # Cut off inside the second proposal: the first is kept.
    mid = full[: full.index('"start": 1.0') + 6]
    assert [p.start for p in salvage(mid).proposals] == [0.0]
    assert salvage('{"proposals": [{"episode_ind') is None


def test_a_refined_boundary_without_evidence_keeps_the_drafts():
    from levi.agent.runtime import anchor_to_draft
    from levi.agent.schema import ModelOutput

    def seg(start, end):
        return {
            "episode_index": 0,
            "kind": "segment",
            "content": "x",
            "start": start,
            "end": end,
            "evidence_ids": ["e"],
            "subtask_id": "grasp",
        }

    summary = {
        "workflow": {"boundary_window_seconds": 1.0},
        "candidate_draft": {"proposals": [seg(0.0, 26.0), seg(26.0, 26.5)]},
    }
    refined = ModelOutput(summary="s", proposals=[seg(0.0, 25.4), seg(25.4, 33.0)])
    kept = anchor_to_draft(refined, summary)
    # 25.4 is within a second of 26.0 and stands; 33.0 is not and falls back.
    assert [(p.start, p.end) for p in kept.proposals] == [(0.0, 25.4), (25.4, 26.5)]


def test_refined_intervals_keep_order_and_share_boundaries():
    from levi.agent.runtime import anchor_to_draft
    from levi.agent.schema import ModelOutput

    def seg(start, end, sub):
        return {
            "episode_index": 0,
            "kind": "segment",
            "content": sub,
            "start": start,
            "end": end,
            "evidence_ids": ["e"],
            "subtask_id": sub,
        }

    draft = [
        seg(14.0, 14.6, "grasp"),
        seg(14.6, 15.2, "transport"),
        seg(15.2, 16.0, "place"),
    ]
    refined = [
        seg(14.0, 15.6, "grasp"),
        seg(15.6, 16.2, "transport"),
        seg(15.2, 16.0, "place"),
    ]
    summary = {
        "workflow": {"boundary_window_seconds": 1.0},
        "candidate_draft": {"proposals": draft},
    }
    kept = anchor_to_draft(ModelOutput(summary="s", proposals=refined), summary)
    spans = [(p.start, p.end) for p in kept.proposals]
    assert spans == [(14.0, 14.6), (14.6, 15.2), (15.2, 16.0)]


def test_a_task_whose_steps_are_done_closes_with_its_report(tmp_path):
    import time as _time

    from levi.agent.store import Store

    store = Store(tmp_path)
    task = {
        "id": "task-1",
        "dataset_key": "plates",
        "status": "running",
        "created_at": _time.time(),
        "spec": {"steps": [{"kind": "quality"}]},
        "steps": [{"kind": "quality", "state": "done", "seconds": 2.0}],
        "interpretation": {"tokens": 100, "token_source": "reported", "seconds": 1.0},
    }
    store.put("tasks", "task-1", task)
    wb = type("Bench", (), {"store": store})()
    closed = tasking.advance(wb, "task-1")
    assert closed["status"] == "done" and closed["report"]["tokens"] == 100


def test_uncertainty_notes_in_chinese_are_filed_like_english_ones():
    from levi.harness.memory import classify

    assert classify("人手在 11 秒进入画面") == ["human_intervention"]
    assert classify("录制结束时盘子还没放下") == ["recording_ends"]
    assert classify("夹爪遮挡了盘沿，看不清") == ["visual_ambiguity"]
    assert classify("粗采样之间可能漏掉了释放") == ["sampling_gap"]
    assert classify("盘子放歪了", "failure") == ["failure_reason"]
