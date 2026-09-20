"""Deterministic observation policy and bounded, exact evidence retrieval."""

import json
from pathlib import Path

import numpy as np

from . import media
from .formats import DATASETS
from .planning import Workflow
from .store import digest, file_hash


class Candidate:
    """Boundary-only stand-in for a proposal, used by the refinement policy."""

    def __init__(self, seconds: float):
        self.start = float(seconds)
        self.end = None
        self.boundary_candidates: list[float] = []


def skills(context):
    flow = Workflow.model_validate(context.workflow)
    names = [
        "levi-overview",
        "annotation-validation",
        {
            "review": "dataset-review",
            "temporal": "temporal-annotation",
            "objects": "object-annotation",
        }[flow.kind],
    ]
    return {
        name: (Path(__file__).parent / "skills" / name / "SKILL.md").read_text()
        for name in names
    }


def frame_scope(context, root, episode, proposals=None):
    flow = Workflow.model_validate(context.workflow)
    table = media.episode_table(media.snapshot_state(context, root), episode)
    times = table.timestamp.to_numpy(dtype=float)
    if len(times) > 1 and (np.diff(times) <= 0).any():
        raise ValueError(
            "Non-monotonic or duplicate timestamps require a corrected source ledger"
        )
    step = flow.coarse_step_seconds
    targets = list(np.arange(times[0], times[-1], step)) + [times[-1]]
    if proposals is not None:
        targets = []
        for p in proposals:
            for boundary in [p.start, p.end, *p.boundary_candidates]:
                if boundary is not None:
                    targets += list(
                        np.arange(
                            max(times[0], boundary - flow.boundary_window_seconds),
                            min(times[-1], boundary + flow.boundary_window_seconds)
                            + 1e-8,
                            flow.boundary_tolerance_seconds / 2,
                        )
                    )
        if not targets:
            targets = [times[0], times[-1]]
    positions = sorted({int(np.abs(times - t).argmin()) for t in targets})
    # Never silently thin a policy that does not fit the approved resource cap.
    if len(positions) * max(1, len(context.cameras)) > flow.max_evidence_frames:
        raise ValueError(
            "Observation coverage exceeds approved frame cap; revise the plan, do not silently undersample"
        )
    return [int(table.iloc[i].frame_index) for i in positions]


def observe(context, root, episode, folder, proposals=None):
    """Evidence for one episode, following the plan's declared policy.

    Temporal and object work both state a coarse step in their workflow, so
    both get frames at that cadence (and, for temporal refinement, around the
    proposed boundaries). Only a plain review run falls back to the plan's
    sample count.
    """
    from .formats import DATASETS

    adapter = DATASETS[context.dataset_adapter]
    if context.workflow["kind"] not in {"temporal", "objects"}:
        return adapter.sample(context, root, episode, folder)
    temporal = getattr(adapter, "sample_temporal", None)
    if temporal is None:
        raise ValueError(
            "Dataset adapter has no declared temporal-window observation support"
        )
    return temporal(context, root, episode, folder, proposals)


def persist(wb, id, episode, summary, evidence):
    try:
        old = wb.store.get("evidence", f"{id}:{episode}")["items"]
    except KeyError:
        old = []
    combined = {r["id"]: r for r in old}
    combined.update({r["id"]: r for r in evidence})
    flow = wb.store.get("runs", id)["context"]["workflow"]
    if flow["kind"] == "temporal" and len(combined) > flow["max_evidence_frames"]:
        raise ValueError(
            "Combined observation/refinement coverage exceeds the approved frame cap"
        )
    # Readers page through this ledger: keep it in playback order, whatever
    # order coarse passes and refinements arrived in.
    ordered = sorted(
        combined.values(),
        key=lambda row: (row.get("camera_key") or "", row["timestamp"], row["id"]),
    )
    wb.store.put("evidence", f"{id}:{episode}", {"summary": summary, "items": ordered})
    # Full exact evidence ledger stays outside the model context.
    path = wb.store.run_dir(id) / f"episode_{episode:06d}-observations.json"
    path.write_text(
        json.dumps({"summary": summary, "items": ordered}, ensure_ascii=False)
    )
    return ordered


def recall(wb, run, episode, offset, limit, layout="single", tile_width=320):
    if episode not in run["context"]["episodes"]:
        raise ValueError("Episode outside approved scope")
    record = wb.store.get("evidence", f"{run['id']}:{episode}")
    # Copied: this page is annotated for the reader, and the stored ledger --
    # what was read and its hashes -- must stay exactly as it was written.
    rows = [dict(row) for row in record["items"][offset : offset + limit]]
    folder = wb.store.run_dir(run["id"]) / "evidence"
    # The ledger outlives the images it describes: workspace.clean deletes the
    # frames of a finished run and keeps what was read and its hashes. So a
    # missing file is reported, not treated as tampering, and only a file whose
    # content changed fails closed.
    gone = []
    for row in rows:
        if not row["artifact"]:
            continue
        path = folder / row["artifact"]
        if not path.exists():
            gone.append(row["artifact"])
            row["image_available"] = False
            continue
        if file_hash(path) != row["sha256"]:
            raise ValueError("Evidence was modified")
    value = {
        "items": rows,
        "total": len(record["items"]),
        "next_offset": offset + len(rows),
        "ledger_digest": digest(record),
        "summary": record["summary"],
    }
    if gone:
        value["images_removed"] = len(gone)
        value["note"] = (
            "This run's evidence images were cleaned up; the ledger, its "
            "hashes and any committed annotations are intact. Run runs.prepare "
            "on this episode to regenerate the frames before reading them."
            if run.get("evidence_cleaned")
            else "Evidence images are missing from the workspace and were not "
            "removed by a recorded cleanup; regenerate with runs.prepare and "
            "check the workspace before trusting new reads."
        )
    if layout == "mosaic":
        # One sheet instead of N images: same frames, same ids, far fewer tokens.
        pictures = [
            row
            for row in rows
            if row.get("artifact") and (folder / row["artifact"]).exists()
        ]
        if not pictures:
            raise ValueError(
                "This page has no image evidence to lay out"
                + (" (its frames were cleaned up)" if gone else "")
            )
        # One readable name per page of a given episode, so a reviewer can
        # find the sheet that backs a suggestion without a lookup table.
        name = (
            f"episode_{episode:06d}--sheet-{offset:04d}-{len(pictures):03d}"
            f"-w{tile_width}.png"
        )
        value["mosaic"] = media.mosaic(
            pictures, folder, folder / name, tile_width=tile_width
        )
        # Sheets are served through the same explicit artifact allowlist as
        # single frames; nothing is readable by name pattern alone.
        wb.store.mutate(
            "runs",
            run["id"],
            lambda record: record.update(
                sheets=sorted({*record.get("sheets", []), name})
            ),
        )
    return value


def refine(wb, run, episode, around, cameras=None):
    """Bounded extra frames around candidate boundaries, using the approved
    window/tolerance of the plan. External agents get the same two-pass policy
    the in-process runtime uses: one coarse pass, then a narrow refinement,
    instead of sampling everything densely."""
    from .schema import TaskContext

    if episode not in run["context"]["episodes"]:
        raise ValueError("Episode outside approved scope")
    if episode not in (run.get("prepared") or []) + run.get("completed", []):
        raise ValueError("Prepare this episode before refining its evidence")
    context = TaskContext.model_validate(run["context"])
    if cameras:
        unknown = set(cameras) - set(context.cameras)
        if unknown:
            raise ValueError(f"Cameras outside approved scope: {sorted(unknown)}")
        context = context.model_copy(update={"cameras": list(cameras)})
    root = wb.store.run_dir(run["id"]) / "input"
    folder = wb.store.run_dir(run["id"]) / "evidence"
    boundaries = [Candidate(value) for value in around]
    summary, evidence = DATASETS[context.dataset_adapter].sample_temporal(
        context, root, episode, folder, boundaries
    )
    items = persist(wb, run["id"], episode, summary, evidence)
    added = {row["id"] for row in evidence}
    return {
        "episode": episode,
        "added": [row for row in items if row["id"] in added],
        "total": len(items),
        "policy": {
            "boundary_window_seconds": context.workflow["boundary_window_seconds"],
            "boundary_tolerance_seconds": context.workflow[
                "boundary_tolerance_seconds"
            ],
            "max_evidence_frames": context.workflow["max_evidence_frames"],
        },
    }


def quality(context, proposals, evidence, summary):
    flow = Workflow.model_validate(context.workflow)
    warnings = []
    coverage = []
    known = {r["id"]: r for r in evidence}
    definitions = {d.id for d in flow.definitions}
    intervals = {}
    for i, p in enumerate(proposals):
        if flow.kind == "temporal" and p.kind == "segment":
            if p.subtask_id not in definitions and p.subtask_id not in {
                "unknown",
                "other",
                "background",
            }:
                # Name the rejected id and the approved set: an agent that has
                # to guess pays for another round trip, and the plan is the
                # only place these ids are defined.
                allowed = ", ".join(
                    sorted(definitions) + ["unknown", "other", "background"]
                )
                raise ValueError(
                    f"Unknown subtask identity {p.subtask_id!r} in proposal "
                    f"{i}; this plan defines: {allowed}. Use unknown/other/"
                    "background for unclassified behaviour, or plan the "
                    "subtask before proposing it."
                )
            if p.outcome is None:
                raise ValueError(
                    "Temporal attempts require success/failure/unknown outcome"
                )
            if p.outcome == "success" and not p.evidence_note:
                raise ValueError(
                    "Success requires a brief observable evidence statement"
                )
        if any(
            not np.isfinite(t) or t < summary["start"] or t > summary["end"]
            for t in p.boundary_candidates
        ):
            raise ValueError("Candidate boundary outside source time scope")
        if p.end is not None:
            coverage.append((p.start, p.end))
            for a, b in intervals.setdefault(p.layer, []):
                if (
                    flow.kind == "temporal"
                    and not flow.overlapping_layers
                    and p.start < b
                    and p.end > a
                ):
                    raise ValueError(
                        "Overlapping segments in an exclusive annotation layer"
                    )
            intervals[p.layer].append((p.start, p.end))
        if flow.kind == "temporal":
            points = [known[r]["timestamp"] for r in p.evidence_ids if r in known]
            for boundary in [p.start, p.end]:
                if boundary is not None and not any(
                    abs(t - boundary) <= flow.boundary_tolerance_seconds for t in points
                ):
                    warnings.append(
                        {
                            "proposal": i,
                            "reason": "boundary lacks nearby observed frame",
                            "boundary": boundary,
                        }
                    )
        if p.uncertainty:
            warnings.append({"proposal": i, "reason": p.uncertainty})
    # Dataset timestamps are float32 while these bounds are computed in
    # float64, so an interval that ends exactly at the episode's last frame can
    # leave a gap of a millionth of a second. A gap nobody could annotate is
    # not a gap; anything a single frame could cover is ignored.
    negligible = min(flow.boundary_tolerance_seconds, 1e-3)
    gaps = []
    cursor = summary["start"]
    for a, b in sorted(coverage):
        if a - cursor > negligible:
            gaps.append([cursor, a])
        cursor = max(cursor, b)
    if summary["end"] - cursor > negligible:
        gaps.append([cursor, summary["end"]])
    return {
        "warnings": warnings,
        "uncovered_intervals": gaps,
        "coverage_claim": "sampled; gaps are not evidence of inactivity",
        "human_review_required": bool(warnings or gaps),
    }
