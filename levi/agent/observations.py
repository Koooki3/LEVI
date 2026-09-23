"""Deterministic observation policy and bounded, exact evidence retrieval."""

import json
from pathlib import Path

import numpy as np

from . import media
from .formats import DATASETS
from .planning import Workflow
from .store import digest, file_hash


class Overflow(ValueError):
    """Observation coverage beyond a limit: the plan's frame cap or what the
    model's context holds."""


class ContextOverflow(Overflow):
    """More images than the model's context window holds."""


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


def frame_scope(
    context, root, episode, proposals=None, spacing=None, limit=None, cap=None
):
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
                            spacing or flow.boundary_tolerance_seconds / 2,
                        )
                    )
        if not targets:
            targets = [times[0], times[-1]]
    positions = sorted({int(np.abs(times - t).argmin()) for t in targets})
    # Never silently thin a policy that does not fit the approved resource cap.
    cap = flow.max_evidence_frames if cap is None else cap
    if len(positions) * max(1, len(context.cameras)) > cap:
        raise Overflow(
            "Observation coverage exceeds approved frame cap; revise the plan, do not silently undersample"
        )
    images = len(positions) * max(1, len(context.cameras))
    if limit is not None and images > limit:
        raise ContextOverflow(
            f"{images} images exceed the {limit} the model's context holds"
        )
    return [int(table.iloc[i].frame_index) for i in positions]


def image_limit(config, usage, evidence, draft, prompt_chars=None, costs=None):
    """How many images the next request can carry.

    With ``costs`` -- (tokens per character, tokens per image) fitted from
    this model's metered requests -- the next prompt is priced directly.
    Otherwise the last call is split: its reported tokens minus its text
    (counted at 4 characters a token, so images come out dear) over its image
    count; the next prompt's text is counted at 3 (so it comes out long).
    Without reported usage there is nothing to learn from: no limit.
    ``prompt_chars`` estimates the text of a call made before providers
    reported it.
    """
    tokens = usage.get("reported_tokens")
    images = sum(1 for row in evidence if row.get("artifact"))
    chars = usage.get("prompt_chars") or prompt_chars or 0
    text = chars + len(json.dumps(draft, ensure_ascii=False))
    from levi.inference.provider import output_allowance

    expected = len(draft.get("proposals", [])) if isinstance(draft, dict) else None
    room = config.context_tokens * 0.85 - output_allowance(config, expected)
    if costs:
        from levi.inference.provider import model_view

        per_char, per_image = costs
        # Each image also brings its row in the evidence text.
        row = len(json.dumps(model_view(evidence[:1]), ensure_ascii=False))
        return max(0, int((room - text * per_char) // (per_image + row * per_char)))
    if not tokens or not images:
        return None
    per_image = max(1.0, (tokens - chars / 4) / images)
    return max(0, int((room - text / 3) // per_image))


def refine_spacings(context):
    """Boundary sampling from finest to coarsest that still meets tolerance."""
    tolerance = Workflow.model_validate(context.workflow).boundary_tolerance_seconds
    return [tolerance / 2, tolerance, tolerance * 2]


def fit_refinement(wb, run, episode, proposals, limit, cap=None):
    """(boundaries, windows, spacing) for a refinement the model can read.

    At each spacing, finest first, change windows are trimmed (least changed
    first) to fit the plan's frame cap and the model's context; the model's
    own boundaries are never dropped, and a coarser spacing is logged. When even the
    coarsest spacing does not fit, the run stops with the numbers.
    """
    from .schema import TaskContext

    context = TaskContext.model_validate(run["context"])
    root = wb.store.run_dir(run["id"]) / "input"
    for spacing in refine_spacings(context):
        boundaries, windows = harness_windows(
            wb, run, episode, proposals, spacing, limit, cap
        )
        try:
            frame_scope(context, root, episode, boundaries, spacing, limit, cap)
        except Overflow:
            continue
        return boundaries, windows, spacing
    cap = Workflow.model_validate(context.workflow).max_evidence_frames
    raise ContextOverflow(
        f"Boundary refinement does not fit the frame cap ({cap}) or the model "
        f"context ({limit} images) even at {spacing:g} s spacing; revise the "
        "plan's cap, narrow the episode scope or use a provider with a larger "
        "context"
    )


class Batch:
    """One refinement request: the proposals starting in [start, end), the
    boundaries whose frames it carries and the spacing they are sampled at.
    ``start`` is None when a single request refines the whole draft."""

    def __init__(self, boundaries, windows, spacing, start=None, end=None):
        self.boundaries = boundaries
        self.windows = windows
        self.spacing = spacing
        self.start = start
        self.end = end


def plan_refinement(wb, run, episode, proposals, limit, observed):
    """The requests that refine a draft within the model's context and the
    plan's frame cap (less the ``observed`` coarse frames already spent).

    One request when the whole draft fits (see ``fit_refinement``). A long
    episode that does not is refined in consecutive batches: at the finest
    spacing whose frames fit the cap in total, proposals are grouped in time
    order while a group's frames fit the context. Each batch costs the prompt
    text again, so batches are as large as the context allows.
    """
    from .schema import TaskContext

    cap = Workflow.model_validate(run["context"]["workflow"]).max_evidence_frames
    cap = max(0, cap - observed)
    try:
        return [Batch(*fit_refinement(wb, run, episode, proposals, limit, cap))]
    except ContextOverflow:
        if limit is None:
            raise
    context = TaskContext.model_validate(run["context"])
    root = wb.store.run_dir(run["id"]) / "input"
    ordered = sorted(proposals, key=lambda p: p.start)
    for spacing in refine_spacings(context):
        try:
            frame_scope(context, root, episode, ordered, spacing, None, cap)
        except Overflow:
            continue
        batches, group = [], []
        for proposal in ordered:
            try:
                frame_scope(context, root, episode, [*group, proposal], spacing, limit)
                group.append(proposal)
            except ContextOverflow:
                if not group:
                    break
                batches.append(group)
                group = [proposal]
        else:
            batches.append(group)
            starts = [group[0].start for group in batches]
            ends = [*starts[1:], float("inf")]
            return [
                Batch(group, [], spacing, start, end)
                for group, start, end in zip(batches, starts, ends, strict=True)
            ]
    raise ContextOverflow(
        f"Boundary refinement does not fit the frame cap ({cap} after the "
        f"coarse pass) or, even one attempt at a time, the model context "
        f"({limit} images); revise the plan's cap or use a provider with a "
        "larger context"
    )


def seam(proposals):
    """Batches are refined apart, so an interval can run past where the next
    batch's first one now starts: within a layer the earlier one ends there."""
    last = {}
    out = []
    for p in proposals:
        before = last.get(p.layer)
        if (
            before is not None
            and out[before].end is not None
            and out[before].end > p.start > out[before].start
        ):
            out[before] = out[before].model_copy(update={"end": p.start})
        if p.end is not None:
            last[p.layer] = len(out)
        out.append(p)
    return out


def merge(draft, part, batch):
    """The draft with the proposals of one batch's window replaced by the
    refined ones."""

    def inside(p):
        return batch.start <= p.start < batch.end

    proposals = [p for p in draft.proposals if not inside(p)]
    proposals += [p for p in part.proposals if inside(p)]
    proposals = seam(sorted(proposals, key=lambda p: p.start))
    return draft.model_copy(
        update={
            "proposals": proposals,
            "warnings": list(dict.fromkeys(draft.warnings + part.warnings))[:50],
        }
    )


def observe(context, root, episode, folder, proposals=None, spacing=None):
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
    return temporal(context, root, episode, folder, proposals, spacing)


class CapExceeded(ValueError):
    """More evidence than the plan's frame cap allows for one episode."""


def persist(wb, id, episode, summary, evidence):
    try:
        old = wb.store.get("evidence", f"{id}:{episode}")["items"]
    except KeyError:
        old = []
    combined = {r["id"]: r for r in old}
    combined.update({r["id"]: r for r in evidence})
    flow = wb.store.get("runs", id)["context"]["workflow"]
    if flow["kind"] == "temporal" and len(combined) > flow["max_evidence_frames"]:
        raise CapExceeded(
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


def text_page(wb, run, episode, offset, limit):
    """A page of the evidence ledger as text only: what each frame is, never
    the frame. The MCP bridge attaches no image to it."""
    if episode not in run["context"]["episodes"]:
        raise ValueError("Episode outside approved scope")
    record = wb.store.get("evidence", f"{run['id']}:{episode}")
    rows = record["items"][offset : offset + limit]
    return {
        "items": [
            {
                "id": row["id"],
                "timestamp": round(row["timestamp"], 3),
                "frame_index": row.get("frame_index"),
                "camera_key": row.get("camera_key"),
            }
            for row in rows
        ],
        "total": len(record["items"]),
        "next_offset": offset + len(rows),
        "ledger_digest": digest(record),
        "summary": record["summary"],
        "images": False,
    }


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
            f"-w{tile_width}.jpg"
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
        # The sheet shows each tile's frame and time; the hashes and decoder
        # bookkeeping stay in the ledger (ledger_digest pins it) instead of
        # being paid for as text on every page.
        value["items"] = [
            {
                key: round(row[key], 3) if key == "timestamp" else row[key]
                for key in (
                    "id",
                    "timestamp",
                    "frame_index",
                    "camera_key",
                    "image_available",
                )
                if key in row
            }
            for row in rows
        ]
        # The sheet lays the items out in list order, ``columns`` to a row:
        # a per-tile index would say what the items list already says.
        value["mosaic"].pop("tiles", None)
        value["mosaic"]["reading"] = (
            "Tiles follow the items list, left to right, "
            f"{value['mosaic']['columns']} per row; each is labelled with its "
            "frame and time"
        )
        if len(pictures) != len(rows):
            # Rows without an image have no tile: name the tiles in order.
            value["mosaic"]["tile_ids"] = [row["id"] for row in pictures]
            value["mosaic"]["reading"] = (
                "Tiles are the frames named in tile_ids, in that order, "
                f"{value['mosaic']['columns']} per row"
            )
    return value


def refine(wb, run, episode, around, cameras=None, layout="mosaic", tile_width=320):
    """Bounded extra frames around candidate boundaries, using the approved
    window/tolerance of the plan. External agents get the same two-pass policy
    the in-process runtime uses: one coarse pass, then a narrow refinement,
    instead of sampling everything densely.

    The added frames come back as one sheet (``layout="mosaic"``): reading
    them used to mean paging through the whole episode again. When the plan's
    frame cap cannot take every requested instant, the ones that fit are
    refined and the rest are named in ``skipped_around_seconds`` -- one call
    that does most of the work beats one that is refused outright."""
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
    kept = list(around)
    try:
        held = {
            row["artifact"]
            for row in wb.store.get("evidence", f"{run['id']}:{episode}")["items"]
            if row.get("artifact")
        }
    except KeyError:
        held = set()
    while True:
        boundaries = [Candidate(value) for value in kept]
        evidence = []
        try:
            # At the plan's boundary tolerance, not half of it: a reader
            # places a boundary to the tolerance, and twice the frames per
            # instant only spent the frame cap and the reader's image budget.
            summary, evidence = DATASETS[context.dataset_adapter].sample_temporal(
                context,
                root,
                episode,
                folder,
                boundaries,
                context.workflow["boundary_tolerance_seconds"],
            )
            items = persist(wb, run["id"], episode, summary, evidence)
            break
        except (CapExceeded, Overflow):
            # Frames sampled for an attempt the cap refused are in no ledger;
            # left on disk they would count against the storage budget.
            for row in evidence:
                if row.get("artifact") and row["artifact"] not in held:
                    (folder / row["artifact"]).unlink(missing_ok=True)
            if len(kept) == 1:
                cap = context.workflow["max_evidence_frames"]
                have = len(wb.store.get("evidence", f"{run['id']}:{episode}")["items"])
                raise ValueError(
                    f"Refining around {kept[0]:g} s would pass the plan's frame "
                    f"cap ({have} of {cap} frames are used); refine fewer or "
                    "closer instants"
                ) from None
            kept = kept[:-1]
    new_ids = {row["id"] for row in evidence}
    added = [row for row in items if row["id"] in new_ids]
    window = context.workflow["boundary_window_seconds"]
    groups = []
    for at in kept:
        slack = context.workflow["boundary_tolerance_seconds"]
        near = [row for row in added if abs(row["timestamp"] - at) <= window + slack]
        groups.append((at, near))
    value = {
        "episode": episode,
        # One line per instant: how many frames it added and over what span.
        # The ids are one `evidence.read` with images false away, and a
        # proposal may leave its citations to LEVI.
        "added": [
            {
                "around": at,
                "frames": len(near),
                "from": min(r["timestamp"] for r in near) if near else None,
                "to": max(r["timestamp"] for r in near) if near else None,
            }
            for at, near in groups
        ],
        "total": len(items),
        "policy": {
            "boundary_window_seconds": window,
            "boundary_tolerance_seconds": context.workflow[
                "boundary_tolerance_seconds"
            ],
            "max_evidence_frames": context.workflow["max_evidence_frames"],
        },
    }
    if len(kept) < len(around):
        value["skipped_around_seconds"] = list(around[len(kept) :])
        value["note"] = (
            "The plan's frame cap took only the first instants; the skipped "
            "ones were not refined."
        )
    if layout == "mosaic":
        # The frames of every instant of this call on one sheet, six to a
        # row, split only past 30 tiles: one sheet per instant multiplied the
        # images an agent collects. Numbered per episode, so a later
        # refinement never replaces a sheet an earlier answer pointed to.
        prefix = f"episode_{episode:06d}--refine-"
        number = sum(
            1
            for sheet in wb.store.get("runs", run["id"]).get("sheets", [])
            if sheet.startswith(prefix)
        )
        seen, pictures = set(), []
        for _, near in groups:
            for row in near:
                if (
                    row["id"] not in seen
                    and row.get("artifact")
                    and (folder / row["artifact"]).exists()
                ):
                    seen.add(row["id"])
                    pictures.append(row)
        pictures.sort(key=lambda row: (row.get("camera_key") or "", row["timestamp"]))
        sheets = []
        for start in range(0, len(pictures), 30):
            chunk = pictures[start : start + 30]
            number += 1
            name = f"{prefix}{number:03d}-w{tile_width}.jpg"
            sheet = media.mosaic(
                chunk, folder, folder / name, tile_width=tile_width, columns=6
            )
            sheet.pop("tiles", None)
            sheet["from"] = chunk[0]["timestamp"]
            sheet["to"] = chunk[-1]["timestamp"]
            sheet["reading"] = (
                "Added frames in time order, six per row; each tile is "
                "labelled with its frame and time"
            )
            sheets.append(sheet)
        if sheets:
            value["mosaics"] = sheets
            wb.store.mutate(
                "runs",
                run["id"],
                lambda record: record.update(
                    sheets=sorted(
                        {*record.get("sheets", []), *(s["artifact"] for s in sheets)}
                    )
                ),
            )
    return value


def rank_intervals(folder, rows, step):
    """Coarse intervals ordered by how much the picture changes across them.

    Two frames a coarse step apart that look alike rarely hide an event; two
    that differ a lot are where a short grasp or release most likely fell.
    Mean absolute difference of small grayscale thumbnails, per camera, so the
    ranking measures change and recognises nothing.
    """
    import cv2

    # Rebuild the coarse pass exactly as frame_scope took it: per camera, the
    # frame nearest each grid target plus the last frame. Refinement frames
    # sit inside the intervals being ranked and must not split them.
    cameras = {}
    for row in rows:
        if row.get("artifact") and (folder / row["artifact"]).exists():
            cameras.setdefault(row.get("camera_key") or "", []).append(row)
    coarse = {}
    for camera, frames in cameras.items():
        times = np.array([row["timestamp"] for row in frames])
        targets = list(np.arange(times.min(), times.max(), step)) + [times.max()]
        picked = {int(np.abs(times - target).argmin()) for target in targets}
        coarse[camera] = {frames[i]["timestamp"]: frames[i] for i in picked}
    scores = {}
    for frames in coarse.values():
        ordered = [frames[t] for t in sorted(frames)]
        pictures = []
        for row in ordered:
            image = cv2.imread(str(folder / row["artifact"]), cv2.IMREAD_GRAYSCALE)
            pictures.append(cv2.resize(image, (160, 120), interpolation=cv2.INTER_AREA))
        for a, b, first, second in zip(pictures, pictures[1:], ordered, ordered[1:]):
            span = (first["timestamp"], second["timestamp"])
            change = float(np.abs(a.astype(float) - b.astype(float)).mean())
            scores[span] = max(scores.get(span, 0.0), change)
    return [
        {
            "from": round(span[0], 3),
            "to": round(span[1], 3),
            "change": round(score, 2),
            "around_seconds": round((span[0] + span[1]) / 2, 3),
        }
        for span, score in sorted(scores.items(), key=lambda item: -item[1])
    ]


def harness_windows(wb, run, episode, proposals, spacing=None, limit=None, cap=None):
    """Refinement targets for the in-process runtime: the model's proposals
    plus the published top-k change windows, trimmed to the frame cap.

    Returns (boundaries, window_seconds). Windows are dropped from the least
    changed first until the plan's frame cap holds; the model's own boundaries
    are never dropped. A run with no published value gets its proposals back.
    """
    from .schema import TaskContext

    top_k = (
        (run.get("harness") or {}).get("parameters", {}).get("evidence.refine_top_k", 0)
    )
    if not top_k:
        return list(proposals), []
    ranked = changes(wb, run, episode, top_k)["suggested_around_seconds"]
    context = TaskContext.model_validate(run["context"])
    root = wb.store.run_dir(run["id"]) / "input"
    while ranked:
        boundaries = list(proposals) + [Candidate(t) for t in ranked]
        try:
            frame_scope(context, root, episode, boundaries, spacing, limit, cap)
            return boundaries, ranked
        except ValueError:
            ranked = ranked[:-1]
    return list(proposals), []


def changes(wb, run, episode, top_k=None):
    """Where to refine next: the coarse intervals that changed most."""
    if episode not in run["context"]["episodes"]:
        raise ValueError("Episode outside approved scope")
    record = wb.store.get("evidence", f"{run['id']}:{episode}")
    folder = wb.store.run_dir(run["id"]) / "evidence"
    step = run["context"]["workflow"]["coarse_step_seconds"]
    ranked = rank_intervals(folder, record["items"], step)
    if top_k is None:
        top_k = (run.get("harness") or {}).get("parameters", {}).get(
            "evidence.refine_top_k", 0
        ) or 3
    return {
        "episode": episode,
        "intervals": ranked,
        "suggested_around_seconds": [row["around_seconds"] for row in ranked[:top_k]],
        "top_k": top_k,
        "method": "mean absolute difference of 160x120 grayscale coarse frames; "
        "a high score means the scene changed, not that a subtask boundary is there",
    }


def complete_external(proposals, evidence, summary):
    """What an external agent may leave to LEVI when it stages segments.

    Writing out evidence ids, repeating the description as an evidence note
    and hitting a float32 last-frame time exactly cost agents a helper script
    and a refused call each in the full-dataset runs. So, for proposals staged
    through ``annotations.propose_*`` only (never a model's own output):

    - no ``evidence_ids``: cite the observed frames inside ``[start, end)``,
      or the nearest observed frame to each boundary when none falls inside
      (at most 32, spread over the interval);
    - a ``success`` without ``evidence_note``: the description is the note;
    - a boundary within half a frame of the episode's first or last frame
      is that frame's time.
    """
    rows = sorted(
        (
            r
            for r in evidence
            if r.get("episode_index", summary["episode_index"])
            == summary["episode_index"]
        ),
        key=lambda r: r["timestamp"],
    )
    frames = summary.get("frames") or 0
    span = summary["end"] - summary["start"]
    half = span / (frames - 1) / 2 if frames > 1 and span > 0 else 0.05
    out = []
    for p in proposals:
        update = {}
        start, end = p.start, p.end
        if abs(start - summary["start"]) <= half:
            start = summary["start"]
        if end is not None and abs(end - summary["end"]) <= half:
            end = summary["end"]
        if (start, end) != (p.start, p.end):
            update.update(start=start, end=end)
        if not p.evidence_ids and rows:
            if end is None:
                inside = [r for r in rows if abs(r["timestamp"] - start) < 1e-6]
            else:
                # [start, end), and the last frame for a segment that ends there.
                closed = end >= summary["end"]
                inside = [
                    r
                    for r in rows
                    if start <= r["timestamp"] < end
                    or (closed and r["timestamp"] == end)
                ]
            stop = end if end is not None else start
            if not inside:
                inside = [
                    min(rows, key=lambda r: abs(r["timestamp"] - t))
                    for t in {start, stop}
                ]
            if len(inside) > 32:
                step = (len(inside) - 1) / 31
                inside = [inside[round(k * step)] for k in range(32)]
            update["evidence_ids"] = list(dict.fromkeys(r["id"] for r in inside))
        if p.outcome == "success" and not p.evidence_note:
            update["evidence_note"] = p.content[:1000]
        out.append(p.model_copy(update=update) if update else p)
    return out


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
