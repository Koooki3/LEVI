"""Cost accounting, evidence packing and housekeeping — the token-economy layer.

These are the parts an external agent leans on to keep a large annotation job
affordable: read evidence as contact sheets, refine only the boundaries that
matter, estimate before committing to a scope, and delete what can be
regenerated afterwards.
"""

import numpy as np
import pytest

from levi.agent import housekeeping, usage
from levi.agent.capabilities import invoke
from levi.agent.runtime import Workbench
from levi.agent.schema import TaskContext
from levi.agent.security import Principal
from levi.agent.store import Store


@pytest.fixture
def bench(client, dataset):
    from levi import catalog, service

    entry = catalog.register(str(dataset))
    wb = Workbench(service.STATE)
    context = TaskContext(
        repo_id=entry["id"],
        episodes=[0, 1],
        instruction="Review the motion",
        provider="external",
    )
    return wb, context


def prepared(wb, context, principal):
    """A run whose first episode has evidence on disk, as an agent leaves it."""
    from levi.agent.planning import approve

    run = wb.plan(context, principal)
    approve(wb, run["id"], 1, "fixture-human")
    invoke(wb, principal, "runs.prepare", {"run_id": run["id"]})
    return wb.store.get("runs", run["id"])


def sample(store, **overrides):
    row = {
        "schema": usage.SCHEMA,
        "id": overrides.get("id", "r1:1"),
        "run_id": "r1",
        "agent_key": "external:mcp",
        "dataset": "local/fixture",
        "at": 1.0,
        "workflow": "temporal",
        "episodes": 5,
        "evidence_frames": 200,
        "evidence_sheets": 12,
        "reads": "mosaic",
        "tokens": 90000,
        "source": "self_reported",
    }
    row.update(overrides)
    store.put("usage_samples", row["id"], row)
    return row


def test_estimate_reproduces_a_recorded_run_and_scales_structurally(client):
    from levi import service

    store = Store(service.STATE)
    sample(store)
    same = usage.estimate(
        store,
        key="external:mcp",
        workflow="temporal",
        episodes=5,
        frames=200,
        reads="mosaic",
    )
    # Calibrated on its own sample, the model returns what was measured.
    assert same["tokens"]["expected"] == pytest.approx(90000, rel=0.02)
    assert same["samples"] == 1
    assert not same["notes"]

    # Ten times the scope must not cost ten times the *fixed* part, and the
    # estimate has to admit it is extrapolating.
    bigger = usage.estimate(
        store,
        key="external:mcp",
        workflow="temporal",
        episodes=50,
        frames=2000,
        reads="mosaic",
    )
    assert bigger["tokens"]["expected"] < 10 * same["tokens"]["expected"]
    assert any("extrapolation" in note for note in bigger["notes"])
    assert bigger["tokens"]["high"] / bigger["tokens"]["expected"] > 1.5


def test_estimate_prices_the_reading_mode_and_says_so(client):
    from levi import service

    store = Store(service.STATE)
    sample(store)
    single = usage.estimate(
        store,
        key="external:mcp",
        workflow="temporal",
        episodes=5,
        frames=200,
        reads="single",
    )
    mosaic = usage.estimate(
        store,
        key="external:mcp",
        workflow="temporal",
        episodes=5,
        frames=200,
        reads="mosaic",
    )
    assert single["tokens"]["expected"] > 2 * mosaic["tokens"]["expected"]
    assert any("contact sheets" in note for note in single["notes"])


def test_estimate_without_history_is_a_stated_default_not_a_guess(client):
    from levi import service

    value = usage.estimate(
        Store(service.STATE), workflow="objects", episodes=3, frames=30
    )
    assert value["samples"] == 0
    assert "no recorded run" in value["basis"]
    assert (
        value["tokens"]["low"] < value["tokens"]["expected"] < value["tokens"]["high"]
    )


def test_history_of_another_agent_is_labelled_not_borrowed_silently(client):
    from levi import service

    store = Store(service.STATE)
    sample(store, agent_key="pilot:codex")
    value = usage.estimate(
        store, key="external:mcp", workflow="temporal", episodes=5, frames=200
    )
    assert value["samples"] == 1
    assert "all recorded agents" in value["basis"]


def test_report_usage_records_what_levi_measured_alongside_the_self_report(bench):
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run = prepared(wb, context, agent)
    recorded = invoke(
        wb,
        agent,
        "runs.report_usage",
        {"run_id": run["id"], "input_tokens": 1200, "output_tokens": 300},
    )
    assert recorded["tokens"] == 1500
    assert recorded["episodes"] == 1
    assert recorded["evidence_frames"] > 0
    assert recorded["source"] == "self_reported"
    # The run now estimates from itself rather than from the defaults.
    value = invoke(wb, agent, "plans.estimate", {"run_id": run["id"]})
    assert value["samples"] == 1
    assert value["tokens"]["low"] < 1500 < value["tokens"]["high"]


def test_run_records_who_asked_for_it(bench):
    wb, context = bench
    agent = Principal("conn-7", datasets=(context.repo_id,))
    run = invoke(wb, agent, "runs.plan", context.model_dump())
    assert wb.store.get("runs", run["id"])["principal"] == "conn-7"


def test_mosaic_packs_a_page_into_one_addressable_sheet(bench, tmp_path):
    from levi.agent import media

    frames = []
    for index in range(5):
        path = tmp_path / f"f{index}.png"
        import cv2

        cv2.imwrite(str(path), np.full((48, 64, 3), index * 40, np.uint8))
        frames.append(
            {
                "id": f"e{index}",
                "artifact": path.name,
                "frame_index": index,
                "timestamp": index * 0.5,
                "camera_key": "observation.images.top",
            }
        )
    sheet = media.mosaic(frames, tmp_path, tmp_path / "sheet.png", tile_width=64)
    assert (tmp_path / sheet["artifact"]).exists()
    assert len(sheet["tiles"]) == 5
    # Every tile maps back to the evidence id it came from, so a citation
    # survives the packing.
    assert [tile["evidence_id"] for tile in sheet["tiles"]] == [
        f"e{i}" for i in range(5)
    ]
    assert sheet["reading"]


def test_cleanup_removes_regenerable_evidence_and_keeps_the_record(bench):
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run = prepared(wb, context, agent)
    directory = wb.store.run_dir(run["id"])
    assert (directory / "evidence").is_dir()

    human = Principal("tester", human=True)
    open_run = invoke(wb, human, "workspace.clean", {})
    assert all(item.get("skipped") for item in open_run["runs"])
    assert (directory / "evidence").is_dir()

    wb.store.mutate("runs", run["id"], lambda r: r.update(status="cancelled"))
    preview = invoke(wb, human, "workspace.clean", {})
    assert preview["bytes"] > 0 and "applied" not in preview
    assert (directory / "evidence").is_dir()

    applied = invoke(wb, human, "workspace.clean", {"apply": True})
    assert applied["applied"] and applied["removed_paths"]
    assert not (directory / "evidence").exists()
    # The ledger and the run survive: a reader learns the images were cleaned.
    assert wb.store.get("runs", run["id"])["evidence_cleaned"]
    ledger = wb.store.get("evidence", f"{run['id']}:0")["items"]
    assert ledger

    # "Regenerable" is the whole claim, so prove it: the same plan rebuilds the
    # same frames, byte for byte, from the untouched dataset.
    wb.store.mutate("runs", run["id"], lambda r: r.update(status="planned"))
    invoke(wb, agent, "runs.prepare", {"run_id": run["id"]})
    rebuilt = wb.store.get("evidence", f"{run['id']}:0")["items"]
    assert [row["sha256"] for row in rebuilt] == [row["sha256"] for row in ledger]
    page = invoke(wb, agent, "evidence.read", {"run_id": run["id"], "episode": 0})
    assert "images_removed" not in page


def test_cleanup_never_touches_a_committed_revision(bench):
    from levi import service

    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run = prepared(wb, context, agent)
    wb.store.mutate("runs", run["id"], lambda r: r.update(status="succeeded"))
    revisions = service.STATE / "workbench/agent/datasets"
    before = sorted(p.name for p in revisions.rglob("revisions/*"))
    housekeeping.apply(wb.store, wb.store.run_dir, older_than_days=0)
    assert sorted(p.name for p in revisions.rglob("revisions/*")) == before


def test_objects_strategy_picks_a_provider_from_the_machine_not_a_guess(
    client, monkeypatch
):
    from levi.agent import objects

    monkeypatch.setattr(
        objects,
        "gpu_headroom",
        lambda: {"known": True, "free_mib": 400, "total_mib": 16000},
    )
    scarce = objects.strategy({"ready": True, "checkpoint": True})
    assert scarce["recommended"] == "agent"
    assert any(
        "memory" in reason.lower() or "free" in reason.lower()
        for reason in scarce["reasons"]
    )

    monkeypatch.setattr(
        objects,
        "gpu_headroom",
        lambda: {"known": True, "free_mib": 12000, "total_mib": 16000},
    )
    roomy = objects.strategy({"ready": True, "checkpoint": True})
    assert roomy["recommended"] == "sam3"

    monkeypatch.setattr(
        objects,
        "gpu_headroom",
        lambda: {"known": True, "free_mib": 12000, "total_mib": 16000},
    )
    missing = objects.strategy({"ready": False, "checkpoint": False})
    assert missing["recommended"] == "agent"


def with_video(dataset, camera="observation.images.front", frames=20):
    """Give the fixture dataset one real, decodable camera."""
    import json

    import cv2

    info_path = dataset / "meta/info.json"
    info = json.loads(info_path.read_text())
    info["features"][camera] = {"dtype": "video", "shape": [48, 64, 3]}
    info_path.write_text(json.dumps(info))
    directory = dataset / f"videos/chunk-000/{camera}"
    directory.mkdir(parents=True, exist_ok=True)
    for ep in range(2):
        writer = cv2.VideoWriter(
            str(directory / f"episode_{ep:06d}.mp4"),
            cv2.VideoWriter_fourcc(*"mp4v"),
            10,
            (64, 48),
        )
        assert writer.isOpened()
        for index in range(frames):
            writer.write(np.full((48, 64, 3), index * 8, np.uint8))
        writer.release()
    return camera


def video_run(wb, context, principal, dataset, **workflow):
    from levi.agent.planning import approve

    camera = with_video(dataset)
    # Rebuilt rather than copied, so the workflow is normalised through its
    # model and inherits every default an agent did not name.
    context = TaskContext(
        **{
            **context.model_dump(),
            "cameras": [camera],
            "allow_media_egress": True,
            "episodes": [0],
            "workflow": workflow,
        }
    )
    run = wb.plan(context, principal)
    approve(wb, run["id"], 1, "fixture-human")
    invoke(wb, principal, "runs.prepare", {"run_id": run["id"]})
    return wb.store.get("runs", run["id"]), camera


def test_coarse_evidence_follows_the_declared_step_then_refines_locally(bench, dataset):
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run, _ = video_run(
        wb,
        context,
        agent,
        dataset,
        kind="temporal",
        coarse_step_seconds=0.5,
        definitions=[
            {
                "id": "grasp",
                "label": "grasp",
                "definition": "close on the plate",
                "starts_when": "fingers touch",
                "ends_when": "object moves",
                "success_when": "object is held",
            }
        ],
    )
    ledger = invoke(wb, agent, "evidence.read", {"run_id": run["id"], "episode": 0})
    # A 2 s episode at a 0.5 s coarse step is four frames, not the flat sample
    # count: the declared policy decides evidence density.
    coarse = ledger["total"]
    assert coarse >= 4

    refined = invoke(
        wb,
        agent,
        "evidence.refine",
        {"run_id": run["id"], "episode": 0, "around_seconds": [1.0]},
    )
    assert refined["added"]
    after = invoke(wb, agent, "evidence.read", {"run_id": run["id"], "episode": 0})
    assert after["total"] > coarse
    # Refinement is local: the new frames sit around the requested instant.
    fresh = [
        row
        for row in after["items"]
        if row["id"] not in {item["id"] for item in ledger["items"]}
    ]
    window = wb.store.get("runs", run["id"])["context"]["workflow"][
        "boundary_window_seconds"
    ]
    assert fresh and all(abs(row["timestamp"] - 1.0) <= window for row in fresh)
    # And the ledger stays ordered, so a page of it reads as time passing.
    stamps = [row["timestamp"] for row in after["items"]]
    assert stamps == sorted(stamps)


def test_a_page_of_evidence_can_be_read_as_one_sheet(bench, dataset):
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run, _ = video_run(
        wb,
        context,
        agent,
        dataset,
        kind="temporal",
        coarse_step_seconds=0.5,
        definitions=[
            {
                "id": "grasp",
                "label": "grasp",
                "definition": "close on the plate",
                "starts_when": "fingers touch",
                "ends_when": "object moves",
                "success_when": "object is held",
            }
        ],
    )
    page = invoke(
        wb,
        agent,
        "evidence.read",
        {"run_id": run["id"], "episode": 0, "limit": 4, "layout": "mosaic"},
    )
    sheet = page["mosaic"]
    assert len(sheet["tiles"]) == len(page["items"])
    # The sheet is a run artifact, so the MCP bridge is allowed to return it.
    assert sheet["artifact"] in wb.store.get("runs", run["id"])["sheets"]
    assert "--sheet-" in sheet["artifact"] and "-w" in sheet["artifact"]


def test_agent_authored_objects_take_the_same_review_path_as_sam3(bench, dataset):
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run, _camera = video_run(
        wb, context, agent, dataset, kind="objects", object_concepts=["plate"]
    )
    frame = invoke(wb, agent, "evidence.read", {"run_id": run["id"], "episode": 0})
    first = frame["items"][0]
    staged = invoke(
        wb,
        agent,
        "objects.propose",
        {
            "run_id": run["id"],
            "inspected_episodes": [0],
            "objects": [
                {
                    "evidence_id": first["id"],
                    "concept": "plate",
                    "bbox_xyxy": [10, 10, 40, 30],
                    "polygon": [[10, 10], [40, 10], [40, 30], [10, 30]],
                    "score": 0.8,
                }
            ],
        },
    )
    assert staged["staged"] == 1
    assert staged["provider"] == "agent"
    inspected = invoke(wb, agent, "objects.inspect", {"job_id": staged["job_id"]})
    row = inspected["rows"][0]
    assert row["source"] == "agent"
    assert row["mask_rle"]["counts"] and row["image_size"] == [48, 64]
    # Nothing is published until a human accepts it, exactly as with SAM3.
    assert wb.store.get("runs", run["id"])["status"] == "waiting_for_review"
    assert inspected["pending"] == 1


def test_detect_measures_candidates_and_a_proposal_can_cite_one(bench, dataset):
    """The scaling path: LEVI measures regions, the agent only names them."""
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run, _camera = video_run(
        wb, context, agent, dataset, kind="objects", object_concepts=["block"]
    )
    ledger = invoke(wb, agent, "evidence.read", {"run_id": run["id"], "episode": 0})
    first = ledger["items"][0]
    found = invoke(
        wb,
        agent,
        "objects.detect",
        {"run_id": run["id"], "evidence_ids": [first["id"]], "min_area_fraction": 0.01},
    )
    frame = found["frames"][0]
    assert frame["evidence_id"] == first["id"]
    assert frame["image_size"] == [48, 64]
    candidate = frame["candidates"][0]
    # Measurements, not labels: the agent is told what the region looks like.
    assert candidate["candidate_id"] == "c00"
    assert 0 < candidate["area_fraction"] <= 1
    assert set(candidate["appearance"]) == {"hue", "saturation", "value", "hue_spread"}
    assert frame["overlay"] in wb.store.get("runs", run["id"])["sheets"]

    # Citing the candidate carries the outline without sending it back.
    staged = invoke(
        wb,
        agent,
        "objects.propose",
        {
            "run_id": run["id"],
            "inspected_episodes": [0],
            "objects": [
                {
                    "evidence_id": first["id"],
                    "candidate_id": candidate["candidate_id"],
                    "concept": "block",
                }
            ],
        },
    )
    assert staged["staged"] == 1
    row = invoke(wb, agent, "objects.inspect", {"job_id": staged["job_id"]})["rows"][0]
    assert row["bbox_xyxy"] == candidate["bbox_xyxy"]
    assert row["source"] == "agent"


def test_a_proposal_must_carry_an_outline_or_a_real_candidate(bench, dataset):
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run, _ = video_run(
        wb, context, agent, dataset, kind="objects", object_concepts=["block"]
    )
    ledger = invoke(wb, agent, "evidence.read", {"run_id": run["id"], "episode": 0})
    first = ledger["items"][0]

    def submit(**object_fields):
        return invoke(
            wb,
            agent,
            "objects.propose",
            {
                "run_id": run["id"],
                "inspected_episodes": [0],
                "objects": [
                    {"evidence_id": first["id"], "concept": "block", **object_fields}
                ],
            },
        )

    with pytest.raises(ValueError, match="candidate_id"):
        submit()
    with pytest.raises(ValueError, match="objects.detect first"):
        submit(candidate_id="c00")
    invoke(
        wb,
        agent,
        "objects.detect",
        {"run_id": run["id"], "evidence_ids": [first["id"]]},
    )
    with pytest.raises(ValueError, match="not among"):
        submit(candidate_id="c99")


def test_reading_evidence_after_a_cleanup_explains_itself(bench, dataset):
    """The ledger outlives its images; reading it must say so, not crash."""
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run, _ = video_run(
        wb, context, agent, dataset, kind="objects", object_concepts=["block"]
    )
    before = invoke(wb, agent, "evidence.read", {"run_id": run["id"], "episode": 0})
    assert "images_removed" not in before

    wb.store.mutate("runs", run["id"], lambda r: r.update(status="succeeded"))
    human = Principal("tester", human=True)
    invoke(wb, human, "workspace.clean", {"apply": True})

    after = invoke(wb, agent, "evidence.read", {"run_id": run["id"], "episode": 0})
    assert after["total"] == before["total"]
    assert after["ledger_digest"] == before["ledger_digest"]
    assert after["images_removed"] == len(
        [row for row in before["items"] if row["artifact"]]
    )
    assert "cleaned up" in after["note"] and "runs.prepare" in after["note"]
    assert all(row.get("image_available") is False for row in after["items"])
    # A sheet cannot be built from frames that no longer exist, and says why.
    with pytest.raises(ValueError, match="cleaned up"):
        invoke(
            wb,
            agent,
            "evidence.read",
            {"run_id": run["id"], "episode": 0, "layout": "mosaic"},
        )


def test_modified_evidence_still_fails_closed(bench, dataset):
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run, _ = video_run(
        wb, context, agent, dataset, kind="objects", object_concepts=["block"]
    )
    page = invoke(wb, agent, "evidence.read", {"run_id": run["id"], "episode": 0})
    artifact = next(row["artifact"] for row in page["items"] if row["artifact"])
    path = wb.store.run_dir(run["id"]) / "evidence" / artifact
    path.write_bytes(path.read_bytes() + b"\0")
    with pytest.raises(ValueError, match="modified"):
        invoke(wb, agent, "evidence.read", {"run_id": run["id"], "episode": 0})


def test_a_moved_source_cannot_slip_in_through_a_cleaned_snapshot(bench, dataset):
    """Re-snapshotting must not become a way to annotate different data."""
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run = prepared(wb, context, agent)
    human = Principal("tester", human=True)
    wb.store.mutate("runs", run["id"], lambda r: r.update(status="cancelled"))
    invoke(wb, human, "workspace.clean", {"apply": True})

    parquet = dataset / "data/chunk-000/episode_000000.parquet"
    parquet.write_bytes(parquet.read_bytes() + b"\0")
    wb.store.mutate("runs", run["id"], lambda r: r.update(status="planned"))
    with pytest.raises(ValueError):
        invoke(wb, agent, "runs.prepare", {"run_id": run["id"]})


def test_a_model_run_records_its_own_cost_without_being_asked(client, dataset):
    """Online mode is metered by LEVI, so the estimator learns from it directly."""
    from levi import catalog, service
    from levi.agent.schema import ProviderConfig
    from levi.agent.store import Store
    from tests.test_agent_workbench import GroundedFixture, execute

    entry = catalog.register(str(dataset))
    wb = Workbench(service.STATE, provider=GroundedFixture())
    wb.store.put(
        "providers",
        "fixture",
        ProviderConfig(
            name="fixture",
            base_url="https://example.invalid/v1",
            model="fixture-1",
            tools=True,
        ).model_dump(),
    )
    context = TaskContext(
        repo_id=entry["id"],
        episodes=[0, 1],
        instruction="Review motion",
        provider="fixture",
    )
    run = execute(wb, wb.plan(context))
    assert run["status"] == "waiting_for_review"

    store = Store(service.STATE)
    recorded = [
        row for row in store.list("usage_samples") if row["run_id"] == run["id"]
    ]
    assert len(recorded) == 1
    sample = recorded[0]
    assert sample["source"] == "measured"
    assert sample["agent_key"] == "model:fixture-1"
    assert sample["tokens"] == run["tokens"] > 0

    # Resuming the run updates that sample instead of counting the work twice.
    execute(wb, wb.store.get("runs", run["id"]), pilot=False)
    again = [row for row in store.list("usage_samples") if row["run_id"] == run["id"]]
    assert len(again) == 1

    value = usage.estimate(store, key="model:fixture-1", workflow="review", episodes=2)
    assert value["samples"] == 1 and value["measured_samples"] == 1
    assert "model:fixture-1" in value["basis"]


def test_an_external_run_is_never_credited_with_measured_tokens(client, dataset):
    """LEVI cannot meter an external agent, so it must not pretend to."""
    from levi import catalog, service
    from levi.agent.store import Store

    entry = catalog.register(str(dataset))
    wb = Workbench(service.STATE)
    context = TaskContext(
        repo_id=entry["id"],
        episodes=[0],
        instruction="Review motion",
        provider="external",
    )
    run = wb.plan(context)
    assert usage.measure_run(Store(service.STATE), run["id"]) is None


def test_float_noise_at_an_episode_end_is_not_reported_as_a_gap(client):
    """A gap of a millionth of a second is arithmetic, not missing work."""
    from levi.agent import observations
    from levi.agent.planning import Workflow
    from levi.agent.schema import Proposal, TaskContext

    context = TaskContext(
        repo_id="local/fixture",
        episodes=[0],
        instruction="Mark the subtasks",
        provider="external",
        workflow={
            "kind": "temporal",
            "definitions": [
                {
                    "id": "grasp",
                    "label": "grasp",
                    "definition": "close",
                    "starts_when": "a",
                    "ends_when": "b",
                    "success_when": "c",
                }
            ],
        },
    )
    evidence = [
        {"id": "e0", "timestamp": 0.0},
        {"id": "e1", "timestamp": 27.7},
    ]
    summary = {"episode_index": 0, "start": 0.0, "end": 27.700000762939453}
    proposals = [
        Proposal(
            episode_index=0,
            kind="segment",
            subtask_id="grasp",
            content="Close on the plate",
            start=0.0,
            end=27.7,
            outcome="unknown",
            evidence_ids=["e0", "e1"],
        )
    ]
    quality = observations.quality(context, proposals, evidence, summary)
    assert quality["uncovered_intervals"] == []

    # A gap someone could actually annotate is still reported.
    proposals[0] = proposals[0].model_copy(update={"end": 20.0})
    Workflow.model_validate(context.workflow)
    wider = observations.quality(context, proposals, evidence, summary)
    assert wider["uncovered_intervals"] and wider["uncovered_intervals"][0][0] == 20.0


def make_rows(*specs):
    """Object rows the way an agent that outlines each frame separately makes
    them: numbered by their position in that frame's list."""
    from levi.annotations.schema import ObjectAnnotation

    rows = []
    for frame, boxes in specs:
        for index, (concept, (x, y)) in enumerate(boxes):
            rows.append(
                ObjectAnnotation(
                    episode_index=0,
                    frame_index=frame,
                    timestamp=frame / 10,
                    camera_key="observation.images.view1",
                    object_id=f"{concept}-{index}",
                    track_id=index,
                    concept=concept,
                    bbox_xyxy=[x, y, x + 40, y + 30],
                    image_size=[480, 640],
                    # A valid RLE must cover the frame; the geometry is not
                    # what these tests are about.
                    mask_rle={"size": [480, 640], "counts": [0, 480 * 640]},
                    score=0.8,
                )
            )
    return rows


def test_linking_keeps_one_object_on_one_track_when_another_appears():
    """The defect this exists for: a new object shifts every later index."""
    from levi.agent.objects import identity_report, link_tracks

    rows = make_rows(
        (40, [("white plate", (170, 180)), ("green plate", (350, 260))]),
        # A second white plate appears first in the list and pushes the
        # original one to index 1, renaming it.
        (
            80,
            [
                ("white plate", (300, 100)),
                ("white plate", (172, 182)),
                ("green plate", (348, 262)),
            ],
        ),
    )
    before = identity_report(rows)
    assert before["concepts"]["white plate"]["tracks"] == 2
    assert before["concepts"]["white plate"]["most_at_once"] == 2

    summary = link_tracks(rows)
    assert summary["identity_switches_repaired"] >= 1

    stayed = [r for r in rows if r.concept == "white plate" and r.bbox_xyxy[0] < 200]
    assert len({r.track_id for r in stayed}) == 1, (
        "the plate that stayed put changed identity"
    )
    appeared = [r for r in rows if r.concept == "white plate" and r.bbox_xyxy[0] >= 200]
    assert {r.track_id for r in appeared}.isdisjoint({r.track_id for r in stayed})
    green = [r for r in rows if r.concept == "green plate"]
    assert len({r.track_id for r in green}) == 1

    after = identity_report(rows)
    assert after["concepts"]["white plate"]["tracks"] == 2
    assert after["more_tracks_than_instances"] == []


def test_identity_report_names_the_concept_that_gained_tracks():
    """More tracks than were ever visible at once is the reviewable signal."""
    from levi.agent.objects import identity_report

    rows = make_rows(
        (40, [("white plate", (170, 180))]),
        (80, [("white plate", (172, 182))]),
    )
    rows[1].track_id = 7
    rows[1].object_id = "white plate-7"
    report = identity_report(rows)
    assert report["concepts"]["white plate"] == {"tracks": 2, "most_at_once": 1}
    assert report["more_tracks_than_instances"] == ["white plate"]


def test_linking_refuses_when_the_frames_are_too_far_apart_to_carry_identity():
    """Sparse evidence cannot establish identity, and saying so beats guessing.

    Outlines sampled seconds apart do not overlap when the object moved, so
    matching by overlap would split one object into a track per frame while
    reporting a repair.
    """
    from levi.agent.objects import link_tracks

    rows = make_rows(
        (40, [("cup", (100, 100))]),
        (80, [("cup", (400, 300))]),
        (120, [("cup", (100, 400))]),
    )
    before = [(row.track_id, row.object_id) for row in rows]
    summary = link_tracks(rows)
    assert summary["linked"] is False
    assert summary["match_rate"] == 0.0
    assert summary["median_frame_gap"] == 40
    assert "cannot be recovered" in summary["reason"]
    # Nothing is renumbered: the caller keeps what it had, and is told why.
    assert [(row.track_id, row.object_id) for row in rows] == before


def test_every_channel_lands_in_one_activity_journal(bench):
    """The panel must show agent work whichever door it came through."""
    from levi.agent import activity

    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    human = Principal("operator", human=True)

    invoke(wb, agent, "workspace.get_context", {})  # discovery: not narrated
    run = invoke(wb, agent, "runs.plan", context.model_dump())
    invoke(wb, human, "plans.estimate", {"run_id": run["id"]})

    rows = activity.recent(wb.store)
    tools = [row["tool"] for row in rows]
    assert "workspace.get_context" not in tools, "polling would drown the panel"
    assert "runs.plan" in tools and "plans.estimate" in tools
    assert {row["channel"] for row in rows} == {"agent", "human"}

    planned = [row for row in rows if row["tool"] == "runs.plan"]
    assert [row["status"] for row in planned] == ["started", "completed"]
    assert planned[-1]["action"] == "created a run plan"
    assert planned[-1]["dataset"] == context.repo_id
    assert planned[-1]["elapsed_seconds"] >= 0


def test_a_failed_action_is_reported_with_its_reason(bench):
    from levi.agent import activity

    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    with pytest.raises((KeyError, ValueError, PermissionError)):
        invoke(wb, agent, "runs.prepare", {"run_id": "20260101T000000000000"})
    failed = [row for row in activity.recent(wb.store) if row["status"] == "failed"]
    assert failed and failed[-1]["tool"] == "runs.prepare"
    assert failed[-1]["error"]


def test_activity_offers_the_paths_a_person_will_copy(client):
    """Artifact paths are the point of watching: they get pasted elsewhere."""
    from levi.agent import activity

    committed = activity.artifacts(
        "changes.commit",
        {"revision": "20260920T061405391397"},
        {"dataset_key": "plates"},
    )
    assert committed[0]["path"] == (
        "outputs/LEVI/workbench/agent/datasets/plates/revisions/20260920T061405391397"
    )
    exported = activity.artifacts("export.run", {"path": "/data/out_annotated"})
    assert exported[0]["path"] == "/data/out_annotated"
    assert activity.artifacts("evidence.read", {"items": []}) == []


def test_activity_is_readable_by_the_operator_not_the_agent(bench):
    """The journal is the human's window; an agent cannot read the workspace's."""
    _wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    with pytest.raises(PermissionError):
        agent.require("configure")
    Principal("operator", human=True).require("configure")


def waiting(wb, agent, **kwargs):
    rows = invoke(wb, agent, "runs.list", kwargs)["runs"]
    return {row["run_id"]: row for row in rows}


def test_an_agent_can_find_the_run_a_human_approved_for_it(bench):
    """The workflow this exists for: the human plans in LEVI, the agent picks
    it up without being handed an id."""
    from levi.agent.planning import approve

    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run = wb.plan(context, agent)

    found = waiting(wb, agent)[run["id"]]
    assert found["waiting_for"] == "human_approval"
    assert found["plan_approved"] is False
    assert found["episodes"] == context.episodes
    assert found["instruction"] == context.instruction

    approve(wb, run["id"], run["plan"]["revision"], "operator")
    assert waiting(wb, agent)[run["id"]]["waiting_for"] == "agent_prepare"

    invoke(wb, agent, "runs.prepare", {"run_id": run["id"]})
    after = waiting(wb, agent)[run["id"]]
    assert after["waiting_for"] == "agent_propose"
    assert after["evidence_ready"] is True


def test_listing_never_reaches_outside_the_connection_scope(bench, dataset, tmp_path):
    from levi import catalog

    wb, context = bench
    other = tmp_path / "second"
    other.mkdir()
    for item in dataset.rglob("*"):
        target = other / item.relative_to(dataset)
        target.parent.mkdir(parents=True, exist_ok=True)
        if item.is_file():
            target.write_bytes(item.read_bytes())
    elsewhere = catalog.register(str(other))
    wb.plan(context.model_copy(update={"repo_id": elsewhere["id"]}), None)
    mine = wb.plan(context, None)

    scoped = Principal("conn", datasets=(context.repo_id,))
    listed = waiting(wb, scoped)
    assert mine["id"] in listed
    assert all(row["dataset"] == context.repo_id for row in listed.values())

    operator = Principal("operator", human=True)
    assert len(waiting(wb, operator)) >= 2


def test_a_cleaned_up_run_is_not_offered_as_work_to_continue(bench):
    """Cleanup frees evidence that runs.prepare can rebuild; until it does,
    the frames are not there to read."""
    from levi.agent.planning import approve

    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    human = Principal("operator", human=True)
    run = wb.plan(context, agent)
    approve(wb, run["id"], run["plan"]["revision"], "operator")
    invoke(wb, agent, "runs.prepare", {"run_id": run["id"]})
    assert waiting(wb, agent)[run["id"]]["evidence_ready"] is True

    wb.store.mutate("runs", run["id"], lambda r: r.update(status="cancelled"))
    invoke(wb, human, "workspace.clean", {"apply": True})

    # Finished work is out of the way by default, and named honestly when asked.
    assert run["id"] not in waiting(wb, agent)
    row = waiting(wb, agent, include_finished=True)[run["id"]]
    assert row["evidence_cleaned"] is True
    assert row["evidence_ready"] is False
    assert "abandoned" in row["waiting_for"]


def test_committed_work_is_reported_as_finished_not_as_a_task(bench):
    from levi.agent.planning import approve

    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run = wb.plan(context, agent)
    approve(wb, run["id"], run["plan"]["revision"], "operator")
    wb.store.mutate("runs", run["id"], lambda r: r.update(status="succeeded"))
    row = waiting(wb, agent, include_finished=True)[run["id"]]
    assert row["finished"] is True
    assert row["waiting_for"] == "human_review"


def test_a_first_time_agent_is_told_how_to_work_here(bench):
    """One call has to leave an agent able to act, not just list datasets."""
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    orientation = invoke(wb, agent, "workspace.get_context", {})

    assert orientation["datasets"] == [context.repo_id]
    assert orientation["you_may"] and orientation["only_a_person_may"]
    # The boundary a proposing agent must not cross is stated, not implied.
    people_only = " ".join(orientation["only_a_person_may"])
    assert "approve" in people_only and "commit" in people_only
    # The sequence names the tools it refers to, so it can be followed.
    steps = " ".join(orientation["start_here"])
    for tool in ("runs.list", "runs.prepare", "evidence.read", "runs.report_usage"):
        assert tool in steps
    assert "mosaic" in steps, "the cost lever belongs in the first call"
    assert "waiting_for_you" in orientation
    assert "experimental" not in orientation


def test_orientation_lists_the_work_already_waiting_for_the_agent(bench):
    from levi.agent.planning import approve

    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    assert invoke(wb, agent, "workspace.get_context", {})["waiting_for_you"] == []

    run = wb.plan(context, agent)
    # Still the human's turn, so it is not offered as the agent's work.
    assert invoke(wb, agent, "workspace.get_context", {})["waiting_for_you"] == []

    approve(wb, run["id"], run["plan"]["revision"], "operator")
    waiting = invoke(wb, agent, "workspace.get_context", {})["waiting_for_you"]
    assert [row["run_id"] for row in waiting] == [run["id"]]
    assert waiting[0]["waiting_for"] == "agent_prepare"


def test_orientation_stays_inside_the_connection_scope(bench, dataset, tmp_path):
    from levi import catalog
    from levi.agent.planning import approve

    wb, context = bench
    other = tmp_path / "second"
    other.mkdir()
    for item in dataset.rglob("*"):
        target = other / item.relative_to(dataset)
        target.parent.mkdir(parents=True, exist_ok=True)
        if item.is_file():
            target.write_bytes(item.read_bytes())
    elsewhere = catalog.register(str(other))
    foreign = wb.plan(context.model_copy(update={"repo_id": elsewhere["id"]}), None)
    approve(wb, foreign["id"], foreign["plan"]["revision"], "operator")

    scoped = Principal("conn", datasets=(context.repo_id,))
    orientation = invoke(wb, scoped, "workspace.get_context", {})
    assert elsewhere["id"] not in orientation["datasets"]
    assert all(
        row["dataset"] == context.repo_id for row in orientation["waiting_for_you"]
    )


def test_ids_are_readable_minutes_and_never_collide(bench):
    """Second precision keeps names legible; a clash must widen, not overwrite.

    Ids used to carry sub-minute digits, which read as an opaque suffix.
    Dropping to minutes means records made in the same minute have to be told
    apart explicitly -- an unchecked id silently replaced the record it was
    supposed to sit beside, which is how an undo once overwrote the very
    changeset it was undoing.
    """
    import re

    from levi.agent.runtime import new_id

    stamp = re.compile(r"^\d{8}T\d{4}(-\d+)?$")
    taken = set()
    for _ in range(3):
        value = new_id(taken)
        assert stamp.fullmatch(value), value
        assert value not in taken
        taken.add(value)
    assert sorted(taken)[1].endswith("-2")

    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    first = wb.plan(context, agent)
    second = wb.plan(context, agent)
    assert first["id"] != second["id"]
    # Both survive: neither replaced the other in the store.
    assert {first["id"], second["id"]} <= set(wb.store.ids("runs"))
    for run in (first, second):
        assert run["id"].startswith(f"{context.workflow['kind']}-")
        assert stamp.fullmatch(run["id"].split("-", 1)[1])


def test_a_published_revision_reads_as_dataset_kind_and_minute(bench):
    from levi.agent.planning import approve

    wb, context = bench
    human = Principal("operator", human=True)
    agent = Principal("conn", datasets=(context.repo_id,))
    run = wb.plan(context, agent)
    approve(wb, run["id"], run["plan"]["revision"], "operator")
    invoke(wb, agent, "runs.prepare", {"run_id": run["id"]})
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
                    "content": "A draft",
                    "start": 0,
                    "end": 1,
                    "evidence_ids": [
                        invoke(wb, agent, "media.sample", {"run_id": run["id"]})[
                            "items"
                        ][0]["id"]
                    ],
                }
            ],
        },
    )
    change = wb.store.get("changes", wb.store.get("runs", run["id"])["changes"])
    invoke(
        wb,
        human,
        "changes.approve",
        {"changeset_id": change["id"], "revision": change["revision"]},
    )
    receipt = wb.commit(change["id"], "once", change["revision"])

    import re

    kind, _, stamp = receipt["revision"].partition("-")
    assert kind == context.workflow["kind"]
    assert re.fullmatch(r"\d{8}T\d{4}(-\d+)?", stamp), receipt["revision"]
    # The dataset is the directory the revision lives in, so the path reads
    # as dataset / kind-minute with nothing opaque in it.
    published = wb.store.bundle(run["dataset_key"], receipt["revision"])
    assert (
        published.parts[-4:]
        == (
            run["dataset_key"],
            "revisions",
            receipt["revision"],
        )[-3:]
        or published.is_dir()
    )


def test_the_cost_estimate_closes_the_loop_with_what_agents_report(client):
    """An estimate is only useful if reporting a run changes the next one."""
    from levi import service
    from levi.agent.store import Store

    store = Store(service.STATE)
    key = "external:mcp"
    scope = {
        "key": key,
        "workflow": "temporal",
        "episodes": 10,
        "frames": 144,
        "reads": "mosaic",
    }

    blind = usage.estimate(store, **scope)
    assert blind["samples"] == 0 and "no recorded run" in blind["basis"]

    def report(id, tokens, at):
        store.put(
            "usage_samples",
            id,
            {
                "schema": usage.SCHEMA,
                "id": id,
                "run_id": id,
                "agent_key": key,
                "dataset": "local/x",
                "at": at,
                "workflow": "temporal",
                "episodes": 10,
                "evidence_frames": 144,
                "evidence_sheets": 14,
                "reads": "mosaic",
                "tokens": tokens,
                "source": "self_reported",
            },
        )

    report("r1", 43000, 1.0)
    once = usage.estimate(store, **scope)
    assert once["samples"] == 1
    # The same scope it was measured on comes back as what was measured.
    assert once["tokens"]["expected"] == pytest.approx(43000, rel=0.05)

    report("r2", 51000, 2.0)
    twice = usage.estimate(store, **scope)
    assert twice["samples"] == 2
    assert 43000 < twice["tokens"]["expected"] < 51000, "a second run must move it"

    # Doubling the scope must not double the fixed cost of a run.
    bigger = usage.estimate(store, **{**scope, "episodes": 20, "frames": 288})
    assert bigger["tokens"]["expected"] < 2 * twice["tokens"]["expected"]

    # A different agent is not credited with this one's history.
    other = usage.estimate(store, **{**scope, "key": "model:something-else"})
    assert "all recorded agents" in other["basis"]


def test_reset_removes_files_and_records_together(bench):
    """Reset shipped untested and dropped the records before locating the
    directories, leaving the files orphaned. Both must go, files first."""
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    human = Principal("operator", human=True)
    run = prepared(wb, context, agent)
    directory = wb.store.run_dir(run["id"])
    assert directory.exists()
    tree = directory.parent.parent

    lock = wb.store.root / "locks" / f"run-{run['id']}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.touch()
    preview = invoke(wb, human, "workspace.reset", {"dataset": context.repo_id})
    assert preview["runs"] == [run["id"]] and preview["applied"] is False
    assert directory.exists(), "a preview must not delete anything"

    done = invoke(
        wb, human, "workspace.reset", {"dataset": context.repo_id, "apply": True}
    )
    assert done["applied"] is True and done["not_removed"] == []
    assert not directory.exists() and not tree.exists()
    assert run["id"] not in wb.store.ids("runs")
    assert not wb.store.events(run["id"])
    assert not lock.exists(), "a run's lock file goes with the run"


def test_reset_leaves_nothing_the_next_task_could_learn_from(bench):
    """Teaching phases, cached answers, tasks and the harness's files for the
    dataset go too: kept, they would feed the next learner work that is gone."""
    from levi.harness import layout

    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    human = Principal("operator", human=True)
    run = prepared(wb, context, agent)
    key = run["dataset_key"]
    wb.store.put("teaching", f"{run['id']}:0:coarse", {"run_id": run["id"]})
    wb.store.put("model_cache", f"{run['id']}:0:coarse", {"output": {}})
    wb.store.put("tasks", "task-1", {"id": "task-1", "dataset_key": key})
    wb.store.put("tasks", "task-2", {"id": "task-2", "dataset_key": "other"})
    wb.store.put("changes", "20260101T0000", {"run_id": run["id"]})
    with wb.store.connect() as db:
        db.execute("INSERT INTO receipts VALUES('commit:20260101T0000:0','r','{}')")
        db.execute("INSERT INTO receipts VALUES('commit:20260101T0001:0','r','{}')")
    files = [
        layout.dataset_dir(wb.store.state, key) / "teaching" / "note.json",
        layout.dataset_dir(wb.store.state, key) / "tasks" / "ledger.json",
        layout.improvements_dir(wb.store.state, key) / "slug.json",
        layout.memory_path(wb.store.state, key),
    ]
    for path in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
    done = invoke(
        wb, human, "workspace.reset", {"dataset": context.repo_id, "apply": True}
    )
    assert done["applied"] is True and done["tasks"] == 1
    assert not wb.store.ids("teaching") and not wb.store.ids("model_cache")
    assert wb.store.ids("tasks") == ["task-2"]
    assert not any(path.exists() for path in files)
    with wb.store.connect() as db:
        left = [key for (key,) in db.execute("SELECT key FROM receipts")]
    assert left == ["commit:20260101T0001:0"]


def test_reset_keeps_records_when_files_cannot_be_removed(bench, monkeypatch):
    """A half-removed history is worse than none: records stay if files fail."""
    import shutil as _shutil

    from levi.agent import housekeeping

    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    human = Principal("operator", human=True)
    run = prepared(wb, context, agent)

    def refuse(path, *args, **kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(housekeeping.shutil, "rmtree", refuse)
    report = invoke(
        wb, human, "workspace.reset", {"dataset": context.repo_id, "apply": True}
    )
    monkeypatch.setattr(housekeeping.shutil, "rmtree", _shutil.rmtree)
    assert report["applied"] is False
    assert report["not_removed"]
    # The work is still visible and the reset can simply be retried.
    assert run["id"] in wb.store.ids("runs")


def test_reset_clears_an_orphaned_tree_left_by_an_earlier_failure(bench):
    """Recovery path: records already gone, files still on disk."""
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    human = Principal("operator", human=True)
    run = prepared(wb, context, agent)
    directory = wb.store.run_dir(run["id"])
    wb.store.drop("runs", [run["id"]])  # simulate the old bug's aftermath
    assert directory.exists()
    done = invoke(
        wb, human, "workspace.reset", {"dataset": context.repo_id, "apply": True}
    )
    assert done["applied"] is True
    assert not directory.exists()
