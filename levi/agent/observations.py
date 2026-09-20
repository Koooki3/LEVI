"""Deterministic observation policy and bounded, exact evidence retrieval."""

import json
from pathlib import Path

import numpy as np

from . import media
from .planning import Workflow
from .store import digest, file_hash


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
    from .formats import DATASETS

    adapter = DATASETS[context.dataset_adapter]
    if context.workflow["kind"] != "temporal":
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
    wb.store.put(
        "evidence",
        f"{id}:{episode}",
        {"summary": summary, "items": list(combined.values())},
    )
    # Full exact evidence ledger stays outside the model context.
    path = wb.store.run_dir(id) / f"episode_{episode:06d}-observations.json"
    path.write_text(
        json.dumps(
            {"summary": summary, "items": list(combined.values())}, ensure_ascii=False
        )
    )
    return list(combined.values())


def recall(wb, run, episode, offset, limit):
    if episode not in run["context"]["episodes"]:
        raise ValueError("Episode outside approved scope")
    record = wb.store.get("evidence", f"{run['id']}:{episode}")
    rows = record["items"][offset : offset + limit]
    folder = wb.store.run_dir(run["id"]) / "evidence"
    for row in rows:
        if row["artifact"] and file_hash(folder / row["artifact"]) != row["sha256"]:
            raise ValueError("Evidence was modified")
    return {
        "items": rows,
        "total": len(record["items"]),
        "next_offset": offset + len(rows),
        "ledger_digest": digest(record),
        "summary": record["summary"],
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
                raise ValueError(
                    "Unknown subtask identity; use unknown/other/background for unclassified behavior"
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
    gaps = []
    cursor = summary["start"]
    for a, b in sorted(coverage):
        if a > cursor:
            gaps.append([cursor, a])
        cursor = max(cursor, b)
    if cursor < summary["end"]:
        gaps.append([cursor, summary["end"]])
    return {
        "warnings": warnings,
        "uncovered_intervals": gaps,
        "coverage_claim": "sampled; gaps are not evidence of inactivity",
        "human_review_required": bool(warnings or gaps),
    }
