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
    assert sheet["next_offset"] == sheet["total"], "one call, the whole pass"
    assert len(sheet["items"]) >= len(single["items"])
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
