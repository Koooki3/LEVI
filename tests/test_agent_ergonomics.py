"""What an external agent needs from LEVI to annotate a whole dataset without
helper scripts and refused calls (measured in the full-dataset comparison
of 2026-09-23: paging, refinement re-reads and citation bookkeeping)."""

# ruff: noqa: F811

import io
import json
from argparse import Namespace

import pytest
from test_agent_economy import bench, video_run  # noqa: F401
from test_harness import GRASP

from levi.agent.capabilities import invoke
from levi.agent.security import Principal


def run_for(wb, context, dataset, **workflow):
    agent = Principal("conn", datasets=(context.repo_id,))
    run, camera = video_run(
        wb,
        context,
        agent,
        dataset,
        kind="temporal",
        coarse_step_seconds=0.5,
        definitions=[GRASP],
        **workflow,
    )
    return agent, run, camera


def test_a_mosaic_page_holds_a_whole_coarse_pass_by_default(bench, dataset):
    wb, context = bench
    agent, run, _ = run_for(wb, context, dataset)
    single = invoke(wb, agent, "evidence.read", {"run_id": run["id"], "episode": 0})
    sheet = invoke(
        wb,
        agent,
        "evidence.read",
        {"run_id": run["id"], "episode": 0, "layout": "mosaic"},
    )
    # One call, the whole pass: no next page to ask for.
    assert "next_offset" not in sheet and len(sheet["times"]) == sheet["total"]
    assert len(sheet["times"]) >= len(single["items"])
    text = invoke(
        wb,
        agent,
        "evidence.read",
        {"run_id": run["id"], "episode": 0, "images": False},
    )
    assert len(text["items"]) == text["total"]


def test_refinement_answers_with_a_sheet_of_the_added_frames(bench, dataset):
    wb, context = bench
    agent, run, _ = run_for(wb, context, dataset)
    value = invoke(
        wb,
        agent,
        "evidence.refine",
        {"run_id": run["id"], "episode": 0, "around_seconds": [0.8]},
    )
    (sheet,) = value["mosaics"]
    assert sheet["artifact"] == "episode_000000--refine-001-w320.jpg"
    assert sheet["columns"] == 6 and sheet["from"] <= 0.8 <= sheet["to"]
    assert value["added"][0]["frames"] > 0
    # The sheet is served like any other evidence artifact.
    assert sheet["artifact"] in wb.store.get("runs", run["id"])["sheets"]
    quiet = invoke(
        wb,
        agent,
        "evidence.refine",
        {"run_id": run["id"], "episode": 0, "around_seconds": [1.2], "layout": "none"},
    )
    assert "mosaics" not in quiet


def test_several_instants_give_one_sheet_each():
    from levi.agent.mcp import images

    value = {"mosaics": [{"artifact": "a.png"}, {"artifact": "b.png"}]}
    assert images("evidence.refine", {"run_id": "r"}, value) == [
        "runs/r/artifacts/a.png",
        "runs/r/artifacts/b.png",
    ]


def test_refinement_beyond_the_cap_keeps_what_fits(bench, dataset, monkeypatch):
    """Each instant brings three new frames here; the cap takes two instants."""
    from levi.agent import formats

    wb, context = bench
    agent, run, _ = run_for(wb, context, dataset)
    coarse = len(wb.store.get("evidence", f"{run['id']}:0")["items"])
    wb.store.mutate(
        "runs",
        run["id"],
        lambda r: r["context"]["workflow"].update(max_evidence_frames=coarse + 6),
    )
    adapter = formats.DATASETS[run["context"]["dataset_adapter"]]
    summary = wb.store.get("evidence", f"{run['id']}:0")["summary"]

    def sample(context, root, episode, folder, boundaries, spacing=None):
        rows = [
            {
                "id": f"extra-{b.start}-{k}",
                "episode_index": 0,
                "timestamp": b.start + k / 100,
                "frame_index": 0,
                "camera_key": context.cameras[0],
                "artifact": None,
                "sha256": "0",
            }
            for b in boundaries
            for k in range(3)
        ]
        return summary, rows

    monkeypatch.setattr(adapter, "sample_temporal", sample)
    value = invoke(
        wb,
        agent,
        "evidence.refine",
        {"run_id": run["id"], "episode": 0, "around_seconds": [0.3, 0.8, 1.3, 1.7]},
    )
    assert value["total"] == coarse + 6
    assert value["skipped_around_seconds"] == [1.3, 1.7], "named, not refused"
    with pytest.raises(ValueError, match="would pass the plan's frame cap"):
        invoke(
            wb,
            agent,
            "evidence.refine",
            {"run_id": run["id"], "episode": 0, "around_seconds": [1.9]},
        )


def test_an_agent_may_leave_citations_notes_and_the_last_frame_to_levi(bench, dataset):
    wb, context = bench
    agent, run, _ = run_for(wb, context, dataset)
    last = wb.store.get("evidence", f"{run['id']}:0")["summary"]["end"]
    receipt = invoke(
        wb,
        agent,
        "annotations.propose_segments",
        {
            "run_id": run["id"],
            "inspected_episodes": [0],
            "proposals": [
                {
                    "episode_index": 0,
                    "kind": "segment",
                    "subtask_id": "grasp",
                    "content": "The gripper closes on the plate and lifts it.",
                    "start": 0.0,
                    "end": 1.0,
                    "outcome": "success",
                },
                {
                    "episode_index": 0,
                    "kind": "segment",
                    "subtask_id": "other",
                    "content": "The arm rests.",
                    "start": 1.0,
                    # Rounded past the float32 last frame by less than half a frame.
                    "end": round(last, 1) + 0.02,
                    "outcome": "unknown",
                },
            ],
        },
    )
    assert receipt["accepted"] == {"0": 2}
    staged = wb.store.get("changes", receipt["id"])["proposals"]
    first, second = staged
    assert first["evidence_ids"] and first["evidence_note"].startswith("The gripper")
    times = {
        r["id"]: r["timestamp"]
        for r in wb.store.get("evidence", f"{run['id']}:0")["items"]
    }
    assert all(0.0 <= times[i] <= 1.0 for i in first["evidence_ids"])
    assert second["end"] == last


def test_a_boundary_well_past_the_last_frame_is_still_refused(bench, dataset):
    wb, context = bench
    agent, run, _ = run_for(wb, context, dataset)
    last = wb.store.get("evidence", f"{run['id']}:0")["summary"]["end"]
    with pytest.raises(ValueError, match="after the episode's last frame"):
        invoke(
            wb,
            agent,
            "annotations.propose_segments",
            {
                "run_id": run["id"],
                "inspected_episodes": [0],
                "proposals": [
                    {
                        "episode_index": 0,
                        "kind": "segment",
                        "subtask_id": "other",
                        "content": "x",
                        "start": 0.0,
                        "end": last + 1.0,
                        "outcome": "unknown",
                    }
                ],
            },
        )


def test_a_model_answer_without_citations_is_refused(bench, dataset):
    """Only an external agent's staged proposals are completed; a model that
    cites nothing is told so."""
    from levi.agent.schema import Proposal, TaskContext

    wb, context = bench
    _, run, _ = run_for(wb, context, dataset)
    saved = wb.store.get("evidence", f"{run['id']}:0")
    proposal = Proposal(
        episode_index=0,
        kind="segment",
        subtask_id="grasp",
        content="x",
        start=0.0,
        end=1.0,
        outcome="unknown",
    )
    with pytest.raises(ValueError, match="cites no evidence"):
        wb.validate_proposals(
            TaskContext.model_validate(run["context"]),
            [proposal],
            saved["items"],
            saved["summary"],
        )


def test_levi_agent_call_reads_arguments_from_stdin_and_saves_images(
    tmp_path, monkeypatch, capsys
):
    from levi.agent import control, mcp

    calls = []

    def fake(path, payload=None, *, binary=False):
        calls.append(path)
        if binary:
            return b"PNG"
        return {"items": [], "mosaic": {"artifact": "episode_000001--sheet.png"}}

    monkeypatch.setattr(mcp, "request", fake)
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps({"run_id": "r1", "episode": 1}))
    )
    control.call(Namespace(capability="evidence.read", arguments="@-", images=tmp_path))
    out = capsys.readouterr().out.splitlines()
    assert json.loads(out[0])["mosaic"]
    assert out[1] == f"IMAGE: {tmp_path / 'episode_000001--sheet.png'}"
    assert (tmp_path / "episode_000001--sheet.png").read_bytes() == b"PNG"
    assert calls[1] == "runs/r1/artifacts/episode_000001--sheet.png"


def test_refused_calls_become_a_cost_hint():
    from levi.harness import cost

    events = [
        {
            "type": "action.failed",
            "tool": "annotations.propose_segments",
            "channel": "agent",
        }
    ] * 3 + [{"type": "action.failed", "tool": "runs.get", "channel": "local-human"}]
    record = {
        "waste": {
            "duplicate_evidence_reads": 0,
            "refused_calls": cost._refused(events),
        },
        "breakdown": [],
        "tokens": {"per_episode": None},
    }
    assert record["waste"]["refused_calls"] == {"annotations.propose_segments": 3}
    assert any("refused" in line for line in cost.hints(record, {}))


def test_only_an_external_caller_stages_imported_annotation():
    from levi.agent.schema import TaskContext

    base = {
        "repo_id": "local/x",
        "episodes": [0],
        "instruction": "i",
        "imported_from": "native-external",
    }
    assert TaskContext(**base, provider="external").imported_from
    with pytest.raises(ValueError, match="external caller"):
        TaskContext(**base, provider="qwen-local")
    assert TaskContext(**base, provider="external", imported_window=[1.0, 2.0])
    with pytest.raises(ValueError, match="imported_window"):
        TaskContext(**base, provider="external", imported_window=[2.0, 1.0])


def test_a_sheet_of_mixed_cameras_pads_tiles_and_names_the_camera(tmp_path):
    import cv2
    import numpy as np

    from levi.agent import media

    items = []
    for i, (w, h) in enumerate([(64, 48), (64, 64), (64, 32)]):
        cv2.imwrite(str(tmp_path / f"f{i}.png"), np.full((h, w, 3), 100, np.uint8))
        items.append(
            {
                "id": f"e{i}",
                "artifact": f"f{i}.png",
                "frame_index": i,
                "timestamp": i / 10,
                "camera_key": "observation.images." + ("a" if i % 2 else "b"),
            }
        )
    sheet = media.mosaic(items, tmp_path, tmp_path / "s.png", tile_width=64, columns=2)
    assert sheet["width"] == 128 and (tmp_path / "s.png").exists()


def test_refinement_sheets_are_never_replaced(bench, dataset):
    wb, context = bench
    agent, run, _ = run_for(wb, context, dataset)
    names = [
        invoke(
            wb,
            agent,
            "evidence.refine",
            {"run_id": run["id"], "episode": 0, "around_seconds": [0.8]},
        )["mosaics"][0]["artifact"]
        for _ in range(2)
    ]
    assert names[0] != names[1] and "--refine-002-" in names[1]


def test_a_refused_episode_leaves_the_others_unwritten(bench, dataset):
    """Two episodes in one call, the second invalid: neither is staged."""
    from test_agent_economy import with_video

    from levi.agent.planning import approve
    from levi.agent.schema import TaskContext

    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    camera = with_video(dataset)
    ctx = TaskContext(
        **{
            **context.model_dump(),
            "cameras": [camera],
            "allow_media_egress": True,
            "episodes": [0, 1],
            "workflow": {
                "kind": "temporal",
                "definitions": [GRASP],
                "pilot_episode": 0,
            },
        }
    )
    run = wb.plan(ctx, agent)
    approve(wb, run["id"], 1, "fixture-human")
    invoke(wb, agent, "runs.prepare", {"run_id": run["id"]})

    def seg(ep, end):
        return {
            "episode_index": ep,
            "kind": "segment",
            "subtask_id": "other",
            "content": "x",
            "start": 0.0,
            "end": end,
            "outcome": "unknown",
        }

    # The real pilot path; then episode 0 is put back as not yet staged, so
    # one call can carry both episodes.
    receipt = invoke(
        wb,
        agent,
        "annotations.propose_segments",
        {"run_id": run["id"], "inspected_episodes": [0], "proposals": [seg(0, 1.0)]},
    )
    invoke(
        wb,
        Principal("operator", human=True),
        "plans.review_pilot",
        {
            "run_id": run["id"],
            "revision": receipt["revision"],
            "accepted": True,
            "note": "pilot fine",
        },
    )
    invoke(wb, agent, "runs.prepare", {"run_id": run["id"]})
    wb.store.mutate("runs", run["id"], lambda r: r.update(completed=[]))
    wb.store.drop("shards", [f"{run['id']}:0"])
    with pytest.raises(ValueError):
        invoke(
            wb,
            agent,
            "annotations.propose_segments",
            {
                "run_id": run["id"],
                "inspected_episodes": [0, 1],
                "proposals": [seg(0, 1.0), seg(1, 99.0)],
            },
        )
    assert not wb.store.ids("shards"), "episode 0 was not written either"
    assert wb.store.get("runs", run["id"])["completed"] == []


def test_citations_follow_the_half_open_interval(bench, dataset):
    from levi.agent.observations import complete_external
    from levi.agent.schema import Proposal

    wb, context = bench
    _, run, _ = run_for(wb, context, dataset)
    saved = wb.store.get("evidence", f"{run['id']}:0")
    (p,) = complete_external(
        [
            Proposal(
                episode_index=0,
                kind="segment",
                subtask_id="grasp",
                content="x",
                start=0.0,
                end=1.0,
                outcome="unknown",
            )
        ],
        saved["items"],
        saved["summary"],
    )
    times = {r["id"]: r["timestamp"] for r in saved["items"]}
    assert all(0.0 <= times[i] < 1.0 for i in p.evidence_ids)


def test_one_call_gives_one_small_jpeg_sheet(bench, dataset):
    """Instants of one call share a sheet; sheets are JPEG, a fraction of the
    size of the PNG sheets that overflowed an agent's request."""
    wb, context = bench
    agent, run, _ = run_for(wb, context, dataset)
    value = invoke(
        wb,
        agent,
        "evidence.refine",
        {"run_id": run["id"], "episode": 0, "around_seconds": [0.4, 1.4]},
    )
    assert len(value["mosaics"]) == 1 and len(value["added"]) == 2
    page = invoke(
        wb,
        agent,
        "evidence.read",
        {"run_id": run["id"], "episode": 0, "layout": "mosaic"},
    )
    folder = wb.store.run_dir(run["id"]) / "evidence"
    for name in (value["mosaics"][0]["artifact"], page["mosaic"]["artifact"]):
        assert name.endswith(".jpg")
        assert (folder / name).read_bytes()[:3] == b"\xff\xd8\xff"


def test_an_edit_replaces_only_the_episodes_it_names(bench, dataset):
    """An agent correcting one episode once erased a full-dataset draft."""
    wb, context = bench
    agent, run, _ = run_for(wb, context, dataset)

    def seg(ep, content):
        return {
            "episode_index": ep,
            "kind": "segment",
            "subtask_id": "other",
            "content": content,
            "start": 0.0,
            "end": 1.0,
            "outcome": "unknown",
        }

    receipt = invoke(
        wb,
        agent,
        "annotations.propose_segments",
        {
            "run_id": run["id"],
            "inspected_episodes": [0],
            "proposals": [seg(0, "first")],
        },
    )
    change = wb.store.get("changes", receipt["id"])
    # Unscoped and dropping episode 0: refused, nothing lost.
    with pytest.raises(ValueError, match="would remove every staged proposal"):
        invoke(
            wb,
            agent,
            "changes.edit",
            {
                "changeset_id": change["id"],
                "revision": change["revision"],
                "proposals": [],
            },
        )
    edited = invoke(
        wb,
        agent,
        "changes.edit",
        {
            "changeset_id": change["id"],
            "revision": change["revision"],
            "proposals": [seg(0, "second")],
            "episodes": [0],
        },
    )
    assert edited["edited_episodes"] == [0] and edited["staged_proposals"] == 1
    assert "proposals" not in edited, "an agent gets a receipt"
    stored = wb.store.get("changes", change["id"])["proposals"]
    assert [p["content"] for p in stored] == ["second"]
    with pytest.raises(ValueError, match="outside `episodes`"):
        invoke(
            wb,
            agent,
            "changes.edit",
            {
                "changeset_id": change["id"],
                "revision": edited["revision"],
                "proposals": [seg(1, "x")],
                "episodes": [0],
            },
        )


# ---- streamlined for a capable external agent (round-3 comparison) --------


def two_episode_run(wb, context, dataset, **workflow):
    from test_agent_economy import with_video

    from levi.agent.planning import approve
    from levi.agent.schema import TaskContext

    agent = Principal("conn", datasets=(context.repo_id,))
    ctx = TaskContext(
        **{
            **context.model_dump(),
            "provider": "external",
            "cameras": [with_video(dataset)],
            "allow_media_egress": True,
            "episodes": [0, 1],
            "workflow": {"kind": "temporal", "definitions": [GRASP], **workflow},
        }
    )
    run = wb.plan(ctx, agent)
    approve(wb, run["id"], 1, "fixture-human")
    return agent, wb.store.get("runs", run["id"])


def test_an_external_plan_reads_one_frame_per_second_unless_it_chooses():
    from levi.agent.schema import TaskContext

    base = {"repo_id": "local/x", "episodes": [0], "instruction": "i"}
    external = TaskContext(**base, provider="external", workflow={"kind": "temporal"})
    model = TaskContext(**base, provider="qwen-local", workflow={"kind": "temporal"})
    chosen = TaskContext(
        **base,
        provider="external",
        workflow={"kind": "temporal", "coarse_step_seconds": 2.0},
    )
    assert external.workflow["coarse_step_seconds"] == 1.0
    assert model.workflow["coarse_step_seconds"] == 2.0
    assert chosen.workflow["coarse_step_seconds"] == 2.0
    # Stable across revalidation (a stored context is validated again).
    again = TaskContext.model_validate(external.model_dump())
    assert again.workflow["coarse_step_seconds"] == 1.0


def test_the_first_read_prepares_an_episode_and_the_pilot_still_gates(bench, dataset):
    wb, context = bench
    agent, run = two_episode_run(wb, context, dataset)
    page = invoke(
        wb,
        agent,
        "evidence.read",
        {"run_id": run["id"], "episode": 0, "layout": "mosaic"},
    )
    assert page["times"] and 0 in wb.store.get("runs", run["id"])["prepared"]
    with pytest.raises(ValueError, match="open after the pilot is accepted"):
        invoke(
            wb,
            agent,
            "evidence.read",
            {"run_id": run["id"], "episode": 1, "layout": "mosaic"},
        )


def test_a_waived_pilot_opens_every_episode_after_plan_approval(bench, dataset):
    wb, context = bench
    agent, run = two_episode_run(wb, context, dataset, require_human_pilot=False)
    assert run["plan"]["pilot_review"]["waived"] is True
    receipt = invoke(
        wb,
        agent,
        "annotations.propose_segments",
        {
            "run_id": run["id"],
            "segments": {
                "0": [
                    {
                        "start": 0.0,
                        "end": 1.0,
                        "subtask": "grasp",
                        "outcome": "success",
                        "description": "Closes on the plate.",
                    }
                ],
                "1": [
                    {
                        "start": 0.0,
                        "end": 1.9,
                        "subtask": "other",
                        "outcome": "unknown",
                        "description": "The arm rests.",
                    }
                ],
            },
        },
    )
    assert receipt["accepted"] == {"0": 1, "1": 1}
    staged = wb.store.get("changes", receipt["id"])["proposals"]
    assert {p["episode_index"] for p in staged} == {0, 1}
    assert all(p["evidence_ids"] and p["kind"] == "segment" for p in staged)
    assert staged[0]["content"] == "Closes on the plate."


def test_a_call_names_its_episodes_or_gives_segments():
    from levi.agent.capabilities import Propose

    with pytest.raises(ValueError, match="inspected episodes"):
        Propose(run_id="r", proposals=[])


# ---- review of the streamlining ------------------------------------------


def test_one_frame_per_second_comes_with_room_for_long_episodes():
    from levi.agent.schema import TaskContext

    base = {"repo_id": "local/x", "episodes": [0], "instruction": "i"}
    ctx = TaskContext(**base, provider="external", workflow={"kind": "temporal"})
    assert ctx.workflow["max_evidence_frames"] == 240
    chosen = TaskContext(
        **base,
        provider="external",
        workflow={"kind": "temporal", "max_evidence_frames": 50},
    )
    assert chosen.workflow["max_evidence_frames"] == 50


def test_object_masks_keep_their_pilot():
    from levi.agent.planning import Workflow

    with pytest.raises(ValueError, match="cannot be waived"):
        Workflow(kind="objects", object_concepts=["plate"], require_human_pilot=False)


def test_a_waived_pilot_cannot_be_reviewed(bench, dataset):
    from levi.agent.planning import pilot_review
    from levi.agent.store import Conflict

    wb, context = bench
    _, run = two_episode_run(wb, context, dataset, require_human_pilot=False)
    with pytest.raises(Conflict, match="waived"):
        pilot_review(wb, run["id"], 0, True, "x", "human")


def test_edits_keep_other_episodes_in_place_and_people_may_empty_one(bench, dataset):
    wb, context = bench
    agent, run = two_episode_run(wb, context, dataset, require_human_pilot=False)

    def seg(text, start=0.0, end=1.0):
        return {
            "start": start,
            "end": end,
            "subtask": "other",
            "outcome": "unknown",
            "description": text,
        }

    receipt = invoke(
        wb,
        agent,
        "annotations.propose_segments",
        {
            "run_id": run["id"],
            "segments": {"0": [seg("a0"), seg("b0", 1.0, 1.9)], "1": [seg("a1")]},
        },
    )
    change = wb.store.get("changes", receipt["id"])
    human = Principal("reviewer", human=True)
    invoke(
        wb,
        human,
        "changes.review",
        {
            "changeset_id": change["id"],
            "revision": change["revision"],
            "indices": [2],
            "decision": "rejected",
        },
    )
    change = wb.store.get("changes", receipt["id"])
    # An agent replaces episode 0; episode 1 keeps its place and decision.
    edited = invoke(
        wb,
        agent,
        "changes.edit",
        {
            "changeset_id": change["id"],
            "revision": change["revision"],
            "episodes": [0],
            "proposals": [
                {
                    "episode_index": 0,
                    "kind": "segment",
                    "subtask_id": "other",
                    "content": "c0",
                    "start": 0.0,
                    "end": 1.9,
                    "outcome": "unknown",
                }
            ],
        },
    )
    after = wb.store.get("changes", change["id"])
    assert [p["content"] for p in after["proposals"]] == ["c0", "a1"]
    assert after["decisions"] == {"1": "rejected"}
    with pytest.raises(ValueError, match="not staged yet"):
        invoke(
            wb,
            agent,
            "changes.edit",
            {
                "changeset_id": change["id"],
                "revision": edited["revision"],
                "episodes": [5],
                "proposals": [],
            },
        )
    # A person may empty an episode through the review screen's full save.
    kept = [p for p in after["proposals"] if p["episode_index"] == 1]
    saved = invoke(
        wb,
        human,
        "changes.edit",
        {
            "changeset_id": change["id"],
            "revision": edited["revision"],
            "proposals": kept,
        },
    )
    assert [p["content"] for p in saved["proposals"]] == ["a1"]


def test_reading_never_prepares_a_model_run(bench, dataset):
    from test_agent_economy import with_video

    from levi.agent.planning import approve
    from levi.agent.schema import ProviderConfig, TaskContext

    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    wb.store.put(
        "providers",
        "fixture",
        ProviderConfig(
            name="fixture",
            base_url="https://example.invalid/v1",
            model="fixture",
            tools=True,
            vision=True,
        ).model_dump(),
    )
    ctx = TaskContext(
        **{
            **context.model_dump(),
            "provider": "fixture",
            "cameras": [with_video(dataset)],
            "allow_media_egress": True,
            "episodes": [0, 1],
            "workflow": {"kind": "temporal", "definitions": [GRASP]},
        }
    )
    run = wb.plan(ctx, agent)
    approve(wb, run["id"], 1, "fixture-human")
    with pytest.raises(KeyError):
        invoke(wb, agent, "evidence.read", {"run_id": run["id"], "episode": 0})
    assert not wb.store.get("runs", run["id"]).get("prepared")


def test_prepare_large_scope_in_bounded_batches(bench, dataset):
    """Prepared episodes accumulate after pilot instead of requiring one long call."""
    from test_agent_economy import with_video

    from levi.agent.planning import approve
    from levi.agent.schema import TaskContext

    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    camera = with_video(dataset)
    ctx = TaskContext(
        **{
            **context.model_dump(),
            "cameras": [camera],
            "allow_media_egress": True,
            "episodes": [0, 1],
            "workflow": {
                "kind": "temporal",
                "definitions": [GRASP],
                "pilot_episode": 0,
            },
        }
    )
    run = wb.plan(ctx, agent)
    approve(wb, run["id"], 1, "fixture-human")
    with pytest.raises(ValueError, match="pilot/scope"):
        invoke(wb, agent, "runs.prepare", {"run_id": run["id"], "episodes": [1]})
    pilot = invoke(wb, agent, "runs.prepare", {"run_id": run["id"], "episodes": [0]})
    assert pilot["episodes"] == [0]
    draft = invoke(
        wb,
        agent,
        "annotations.propose_segments",
        {
            "run_id": run["id"],
            "inspected_episodes": [0],
            "proposals": [
                {
                    "episode_index": 0,
                    "kind": "segment",
                    "subtask_id": "grasp",
                    "content": "Visible motion",
                    "start": 0.0,
                    "end": 1.0,
                    "outcome": "unknown",
                }
            ],
        },
    )
    invoke(
        wb,
        Principal("operator", human=True),
        "plans.review_pilot",
        {
            "run_id": run["id"],
            "revision": draft["revision"],
            "accepted": True,
            "note": "Fixture pilot reviewed",
        },
    )
    with pytest.raises(ValueError, match="approved pilot/scope"):
        invoke(wb, agent, "runs.prepare", {"run_id": run["id"], "episodes": [2]})
    with pytest.raises(ValueError, match="Completed episodes"):
        invoke(wb, agent, "runs.prepare", {"run_id": run["id"], "episodes": [0]})
    remaining = invoke(
        wb, agent, "runs.prepare", {"run_id": run["id"], "episodes": [1]}
    )
    assert remaining["episodes"] == [1]
    assert wb.store.get("runs", run["id"])["prepared"] == [0, 1]
