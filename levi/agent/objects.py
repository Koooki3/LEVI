"""SAM3 ToolProvider: stages existing worker results without publishing annotations.

No worker imports, checkpoint downloads or device probes in the control plane.
"""

import json
import os
import signal
import sqlite3
import subprocess
import threading
from itertools import pairwise
from pathlib import Path

from pydantic import Field

from levi.annotations.sam3_protocol import validate_annotations_for_plan
from levi.annotations.schema import ObjectAnnotation, ObjectEdit, Sam3Plan
from levi.annotations.sidecar import SidecarStore
from levi.paths import PROJECT

from .runtime import new_id
from .schema import Contract

_PROCESSES = {}


class ObjectRequest(Contract):
    run_id: str
    prompts: list[str] = Field(min_length=1, max_length=30)
    max_frames: int = Field(default=100, ge=1, le=10000)


def gpu_headroom():
    """Free VRAM without importing Torch or initialising CUDA in this process.

    Absence of nvidia-smi is not an error: it means "unknown", never "fine".
    """
    import shutil
    import subprocess

    if not shutil.which("nvidia-smi"):
        return {"known": False, "reason": "nvidia-smi not found"}
    try:
        out = (
            subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=memory.free,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            .stdout.strip()
            .splitlines()
        )
        free, total = (int(v) for v in out[0].split(","))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {"known": False, "reason": f"nvidia-smi unreadable: {exc}"}
    return {"known": True, "free_mib": free, "total_mib": total}


def minimum_free_mib():
    return int(os.getenv("LEVI_SAM3_MIN_FREE_MIB", "7000"))


def strategy(readiness_value):
    """Which object-annotation path fits the machine right now.

    The agent may annotate objects itself when the SAM3 worker is missing or
    the GPU is too contended to run it; the recommendation is advisory and
    both paths end in the same staged human review.
    """
    # Callers may pass a full readiness report or just what they know; the
    # machine is asked directly for anything missing.
    gpu = readiness_value.get("gpu") or gpu_headroom()
    need = minimum_free_mib()
    available = readiness_value.get("available")
    if available is None:
        available = bool(readiness_value.get("ready")) and bool(
            readiness_value.get("checkpoint")
        )
    if not available:
        return {
            "recommended": "agent",
            "reasons": ["SAM3 worker or checkpoint is not ready on this machine"],
            "minimum_free_mib": need,
        }
    if gpu.get("known") and gpu["free_mib"] < need:
        return {
            "recommended": "agent",
            "reasons": [
                f"only {gpu['free_mib']} MiB of GPU memory is free; SAM3 needs about {need} MiB"
            ],
            "minimum_free_mib": need,
        }
    reasons = ["SAM3 worker, checkpoint and GPU headroom are available"]
    if not gpu.get("known"):
        reasons.append(
            "GPU memory could not be read ("
            + str(gpu.get("reason"))
            + "); SAM3 may still fail to start"
        )
    return {"recommended": "sam3", "reasons": reasons, "minimum_free_mib": need}


def readiness():
    from backend import app

    checkpoint = app._sam3_checkpoint_path()
    worker = Path(
        os.getenv(
            "LEVI_SAM3_WORKER_PYTHON",
            str(PROJECT / "integrations/sam3/.venv/bin/python"),
        )
    )
    enabled = app._sam3_enabled()
    ready = app._is_sam3_checkpoint(checkpoint)
    value = {
        "enabled": enabled,
        "checkpoint_ready": ready,
        "worker_ready": worker.is_file() and os.access(worker, os.X_OK),
        "cuda_probe_performed": False,
        "available": enabled
        and ready
        and worker.is_file()
        and os.access(worker, os.X_OK),
    }
    value["gpu"] = gpu_headroom()
    value["strategy"] = strategy(value)
    return value


def plan(wb, request):
    run = wb.store.get("runs", request.run_id)
    from .planning import require

    require(wb, run)
    if not run.get("manifest"):
        raise ValueError("Complete a pilot snapshot before planning object annotation")
    if run["status"] in {
        "running",
        "queued",
        "cancelled",
        "succeeded",
        "partially_succeeded",
    }:
        raise ValueError("Finish the current operation or create a new run")
    ctx = run["context"]
    if not ctx["cameras"]:
        raise ValueError("Object annotation requires cameras frozen into the run")
    if ctx["mode"] == "read_only":
        raise ValueError("Read-only runs cannot stage object annotations")
    id = new_id(set(wb.store.ids("object_jobs")))
    root = wb.store.run_dir(run["id"]) / "input"
    workflow = ctx["workflow"]
    if workflow["kind"] == "objects" and request.prompts != workflow["object_concepts"]:
        raise ValueError("Object concepts differ from the approved plan")
    episodes = (
        ctx["episodes"]
        if (run["plan"].get("pilot_review") or {}).get("accepted")
        else [run["plan"]["pilot_episode"]]
    )
    if workflow["kind"] == "objects":
        episodes = [ep for ep in episodes if ep not in run["completed"]]
        if not episodes:
            raise ValueError("No unprocessed object episodes remain")
    model = Sam3Plan(
        local_path=str(root),
        episode_indices=episodes,
        camera_keys=ctx["cameras"],
        prompts=request.prompts,
        max_frames=request.max_frames,
    )
    value = {
        "id": id,
        "run_id": run["id"],
        "status": "planned",
        "plan": model.model_dump(),
        "dataset_root": str(root),
        "readiness": readiness(),
    }
    wb.store.put("object_jobs", id, value)
    return {k: v for k, v in value.items() if k not in {"plan", "dataset_root"}}


def _worker_failure(code, log_path):
    detail = ""
    try:
        tail = [
            line
            for line in log_path.read_text(errors="replace").splitlines()
            if line.strip()
        ][-1:]
        detail = f" Last log line: {tail[0][:200]}" if tail else ""
    except OSError:
        pass
    if code < 0:
        import signal as signals

        name = signals.Signals(-code).name
        likely = (
            " The usual cause is the GPU or host running out of memory."
            if -code in {signals.SIGKILL, signals.SIGABRT}
            else ""
        )
        return (
            f"SAM3 worker was killed by {name}.{likely} Full log: {log_path}.{detail}"
        )
    return f"SAM3 worker exited with code {code}. Full log: {log_path}.{detail}"


def launch(wb, id):
    job = wb.store.get("object_jobs", id)
    from .planning import require

    require(wb, wb.store.get("runs", job["run_id"]))
    state = readiness()
    if not state["available"]:
        raise ValueError(
            "SAM3 unavailable: configure worker and checkpoint in the SAM3 panel; no automatic download"
        )
    gpu = state["gpu"]
    if gpu.get("known") and gpu["free_mib"] < minimum_free_mib():
        # Starting anyway would usually end as a silent out-of-memory kill and
        # can take another process down with it on a shared card.
        raise ValueError(
            f"Only {gpu['free_mib']} MiB of GPU memory is free; SAM3 needs about "
            f"{minimum_free_mib()} MiB. Free the GPU, lower LEVI_SAM3_MIN_FREE_MIB "
            "deliberately, or annotate the objects with the agent instead."
        )
    if job["status"] != "planned":
        raise ValueError("Object job already submitted")
    lease = "objects:" + job["run_id"]
    if not wb.store.claim(lease, id):
        raise ValueError("An object job is already active for this run")

    def execute():
        from levi.annotations.sam3_protocol import validate_annotations_for_plan
        from levi.catalog import atomic
        from levi.jobs import WORKERS

        stopped = threading.Event()

        def heartbeat():
            while not stopped.wait(15):
                try:
                    if not wb.store.claim(lease, id):
                        cancel(wb, job["run_id"])
                        return
                except sqlite3.OperationalError:
                    continue

        ticker = threading.Thread(target=heartbeat, daemon=True)
        ticker.start()
        try:
            with WORKERS:
                parent = wb.store.get("runs", job["run_id"])
                if parent.get("control") == "cancel":
                    raise ValueError("Parent run cancelled")
                directory = wb.store.run_dir(job["run_id"]) / "objects" / id
                directory.mkdir(parents=True, exist_ok=True)
                atomic(
                    directory / "plan.json",
                    {**job["plan"], "dataset_root": job["dataset_root"]},
                )
                worker = os.getenv(
                    "LEVI_SAM3_WORKER_PYTHON",
                    str(PROJECT / "integrations/sam3/.venv/bin/python"),
                )
                with (directory / "worker.log").open("wb") as log:
                    from backend import app

                    environment = {
                        **os.environ,
                        "LEVI_SAM3_CHECKPOINT": str(app._sam3_checkpoint_path()),
                    }
                    # An explicit local path makes the worker fail closed, never download.
                    process = subprocess.Popen(
                        [
                            worker,
                            "-m",
                            "levi_sam3_worker.cli",
                            "--plan",
                            str(directory / "plan.json"),
                            "--output",
                            str(directory / "result.json"),
                        ],
                        cwd=PROJECT / "integrations/sam3",
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL,
                        start_new_session=True,
                        env=environment,
                    )
                    _PROCESSES[id] = process
                    wb.store.mutate(
                        "object_jobs", id, lambda j: j.update(status="running")
                    )
                    try:
                        code = process.wait(
                            timeout=parent["context"]["budget"]["max_seconds"]
                        )
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait()
                        raise ValueError("SAM3 job timed out") from None
                if code:
                    raise ValueError(_worker_failure(code, directory / "worker.log"))
                result = json.loads((directory / "result.json").read_text())
                if result.get("status") != "succeeded":
                    raise ValueError("SAM3 worker did not return a successful result")
                rows = [
                    ObjectAnnotation.model_validate(r)
                    for r in result.get("annotations", [])
                ]
                validate_annotations_for_plan(
                    Sam3Plan.model_validate(job["plan"]), rows
                )
                if wb.store.get("runs", job["run_id"]).get("control") == "cancel":
                    raise ValueError("Parent run cancelled; result remains unpublished")
                stage(wb, id, rows, "sam3")
        except Exception as exc:  # noqa: BLE001 - only LEVI's own text is surfaced
            # "It failed" is not actionable. Report LEVI's own classification
            # (exit code/signal, likely cause, log path) and keep the worker's
            # own output in the local log file it already wrote.
            reason = (
                str(exc)
                if isinstance(exc, ValueError)
                else "Object job stopped before the result was published"
            )
            log = wb.store.run_dir(job["run_id"]) / "objects" / id / "worker.log"
            wb.store.mutate(
                "object_jobs",
                id,
                lambda j: j.update(
                    status="blocked",
                    reason=reason,
                    log_path=str(log) if log.exists() else None,
                ),
            )
        finally:
            stopped.set()
            ticker.join(timeout=35)
            _PROCESSES.pop(id, None)
            wb.store.release(lease, id)

    wb.store.mutate("object_jobs", id, lambda j: j.update(status="queued"))
    threading.Thread(target=execute, daemon=True).start()
    return {"job_id": id, "status": "queued"}


def stage(wb, job_id, rows, provider):
    """Publish staged masks and attach them to the run's changeset.

    SAM3 results and agent-authored objects take the same path: nothing is a
    human label until the same review and commit gates have passed.
    """
    job = wb.store.get("object_jobs", job_id)
    directory = wb.store.run_dir(job["run_id"]) / "objects" / job_id
    SidecarStore(directory / "draft").publish(
        rows, model={"provider": provider, "run_id": job["run_id"]}
    )
    wb.store.mutate(
        "object_jobs",
        job_id,
        lambda j: j.update(status="waiting_for_review", count=len(rows)),
    )
    run = wb.store.get("runs", job["run_id"])
    if run["context"]["workflow"]["kind"] == "objects":
        wb.store.mutate(
            "runs",
            run["id"],
            lambda r: r.update(
                completed=sorted(set(r["completed"] + job["plan"]["episode_indices"]))
            ),
        )
        for ep in job["plan"]["episode_indices"]:
            try:
                wb.store.get("shards", f"{run['id']}:{ep}")
            except KeyError:
                wb.store.put(
                    "shards",
                    f"{run['id']}:{ep}",
                    {
                        "output": {
                            "summary": "Object-only workflow",
                            "proposals": [],
                        },
                        "usage": {"requests": 0, "tokens": 0},
                    },
                )
        wb.prepare_changes(run["id"])
    if not run.get("changes"):
        wb.prepare_changes(run["id"])
        run = wb.store.get("runs", run["id"])

    def attach(change):
        if change["status"] == "committed":
            raise ValueError("Run already committed; object result remains staged")
        change.setdefault("object_jobs", []).append(job_id)
        change.update(status="draft", revision=change["revision"] + 1)

    wb.store.mutate("changes", run["changes"], attach)
    wb.store.mutate("runs", run["id"], lambda r: r.update(status="waiting_for_review"))
    wb.store.event(run["id"], "objects_staged", job_id=job_id, count=len(rows))
    return {"job_id": job_id, "staged": len(rows), "provider": provider}


def _rle(height, width, polygon=None, bbox=None):
    """COCO column-order RLE for one filled polygon or rectangle."""
    import cv2
    import numpy as np

    mask = np.zeros((height, width), dtype=np.uint8)
    if polygon:
        points = np.array([[round(x), round(y)] for x, y in polygon], np.int32)
        cv2.fillPoly(mask, [points], 1)
    else:
        x1, y1, x2, y2 = (round(v) for v in bbox)
        cv2.rectangle(mask, (x1, y1), (max(x1, x2 - 1), max(y1, y2 - 1)), 1, -1)
    if not mask.any():
        raise ValueError("Object outline covers no pixel of the frame")
    flat = mask.flatten(order="F").astype(bool)
    edges = np.flatnonzero(np.diff(flat)) + 1
    bounds = np.concatenate([[0], edges, [flat.size]])
    counts = np.diff(bounds).tolist()
    if flat[0]:
        counts = [0] + counts  # COCO always starts with a background run
    return {"size": [height, width], "counts": [int(v) for v in counts]}


def link_tracks(rows):
    """Give one physical object one track across the frames it was seen in.

    An agent that outlines each frame independently has no identity to offer:
    numbering detections by their position in a per-frame list makes every id
    shift as soon as one object enters or leaves. Linking by overlap restores
    what the numbering lost -- a plate that stays put keeps its track when a
    new plate appears beside it.

    Rows are matched within one episode, camera and concept, from each
    annotated frame to the next, by bounding-box IoU above a threshold. An
    object that is not matched starts its own track, which is the honest
    outcome for something that genuinely appeared.
    """

    def iou(a, b):
        ax1, ay1, ax2, ay2 = a.bbox_xyxy
        bx1, by1, bx2, by2 = b.bbox_xyxy
        left, top = max(ax1, bx1), max(ay1, by1)
        right, bottom = min(ax2, bx2), min(ay2, by2)
        if right <= left or bottom <= top:
            return 0.0
        overlap = (right - left) * (bottom - top)
        union = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - overlap
        return overlap / union if union > 0 else 0.0

    groups = {}
    for row in rows:
        groups.setdefault((row.episode_index, row.camera_key, row.concept), []).append(
            row
        )
    # Overlap only carries identity when consecutive annotated frames are close
    # enough for an object to still overlap itself. Measure that before using
    # it: on evidence sampled seconds apart a moving object shares no pixels
    # with its previous outline, and "linking" would fragment the annotation
    # while claiming to repair it.
    attempts = matched = 0
    gaps = []
    for members in groups.values():
        frames = sorted({row.frame_index for row in members})
        for previous_frame, frame in pairwise(frames):
            gaps.append(frame - previous_frame)
            earlier = [r for r in members if r.frame_index == previous_frame]
            later = [r for r in members if r.frame_index == frame]
            attempts += min(len(earlier), len(later))
            matched += sum(
                1 for row in later if any(iou(other, row) >= 0.3 for other in earlier)
            )
    rate = matched / attempts if attempts else 1.0
    if attempts and rate < 0.5:
        return {
            "linked": False,
            "tracks": len({row.track_id for row in rows}),
            "identity_switches_repaired": 0,
            "match_rate": round(rate, 2),
            "median_frame_gap": sorted(gaps)[len(gaps) // 2] if gaps else 0,
            "reason": (
                f"Only {round(rate * 100)}% of detections overlap their previous "
                "annotated frame, so identity cannot be recovered from these "
                "outlines. Annotate closer frames, or keep them as per-frame "
                "detections and do not claim tracks."
            ),
        }
    switches, next_track = 0, 0
    for (_episode, _camera, concept), members in sorted(groups.items()):
        frames = sorted({row.frame_index for row in members})
        open_tracks = []  # (track_id, last row)
        for frame in frames:
            current = [row for row in members if row.frame_index == frame]
            pairs = sorted(
                (
                    (iou(previous, row), index, position)
                    for index, (_id, previous) in enumerate(open_tracks)
                    for position, row in enumerate(current)
                ),
                reverse=True,
            )
            taken_tracks, taken_rows = set(), set()
            for score, index, position in pairs:
                if score < 0.3 or index in taken_tracks or position in taken_rows:
                    continue
                track_id, _previous = open_tracks[index]
                row = current[position]
                if row.object_id != _previous.object_id:
                    switches += 1
                row.track_id = track_id
                row.object_id = _previous.object_id
                open_tracks[index] = (track_id, row)
                taken_tracks.add(index)
                taken_rows.add(position)
            for position, row in enumerate(current):
                if position in taken_rows:
                    continue
                row.track_id = next_track
                row.object_id = f"{concept.replace(' ', '-')}-{next_track}"
                open_tracks.append((next_track, row))
                next_track += 1
    return {
        "linked": True,
        "tracks": next_track,
        "identity_switches_repaired": switches,
        "match_rate": round(rate, 2),
    }


def identity_report(rows):
    """What a reviewer needs to judge whether the tracks are believable."""
    from collections import defaultdict

    per_concept = defaultdict(lambda: {"tracks": set(), "most_at_once": 0})
    counts = defaultdict(lambda: defaultdict(int))
    for row in rows:
        entry = per_concept[row.concept]
        entry["tracks"].add(row.track_id)
        counts[row.concept][(row.episode_index, row.frame_index)] += 1
    suspicious = []
    for concept, entry in per_concept.items():
        entry["most_at_once"] = max(counts[concept].values(), default=0)
        if len(entry["tracks"]) > entry["most_at_once"]:
            suspicious.append(concept)
    return {
        "concepts": {
            concept: {
                "tracks": len(entry["tracks"]),
                "most_at_once": entry["most_at_once"],
            }
            for concept, entry in sorted(per_concept.items())
        },
        "more_tracks_than_instances": sorted(suspicious),
        "reading": (
            "A concept with more tracks than were ever visible at once usually "
            "means one object was given a new identity between frames. Review "
            "those tracks before accepting them."
        ),
    }


def detect(wb, run, args):
    """Measure candidate object regions in evidence the agent already holds.

    This is the generic half of object annotation: LEVI finds and describes
    coherent regions, the agent decides which are objects and what to call
    them. Candidates are kept so a proposal can cite one by id instead of
    sending its outline back, which is the difference between a dozen tokens
    and several hundred per object.
    """
    from . import detection

    ledger = {}
    for ep in sorted(set(run.get("prepared", []) + run.get("completed", []))):
        for row in wb.store.get("evidence", f"{run['id']}:{ep}")["items"]:
            ledger[row["id"]] = row
    evidence_dir = wb.store.run_dir(run["id"]) / "evidence"
    frames, sheets = [], []
    for evidence_id in args.evidence_ids:
        source = ledger.get(evidence_id)
        if source is None:
            raise ValueError(f"Unknown evidence id: {evidence_id}")
        if not source.get("artifact"):
            raise ValueError("Detection needs image evidence, not table rows")
        value = detection.candidates(
            evidence_dir / source["artifact"],
            max_candidates=args.max_candidates,
            min_area_fraction=args.min_area_fraction,
            colours=args.colours,
        )
        wb.store.put(
            "detections",
            f"{run['id']}:{evidence_id}",
            {
                "run_id": run["id"],
                "evidence_id": evidence_id,
                "candidates": value["candidates"],
            },
        )
        frame = {
            "evidence_id": evidence_id,
            "episode_index": source["episode_index"],
            "frame_index": source["frame_index"],
            "camera_key": source["camera_key"],
            "image_size": value["image_size"],
            "candidates": value["candidates"],
        }
        if args.overlay and value["candidates"]:
            name = f"{Path(source['artifact']).stem}--candidates.png"
            detection.overlay(
                evidence_dir / source["artifact"],
                evidence_dir / name,
                value["candidates"],
            )
            frame["overlay"] = name
            sheets.append(name)
        frames.append(frame)
    if sheets:
        wb.store.mutate(
            "runs",
            run["id"],
            lambda record: record.update(
                sheets=sorted({*record.get("sheets", []), *sheets})
            ),
        )
    wb.store.event(run["id"], "objects_detected", frames=len(frames))
    return {
        "frames": frames,
        "reading": (
            "Measured regions, not recognitions. Submit the ones that are "
            "objects with objects.propose, citing candidate_id instead of a "
            "polygon; ignore the rest."
        ),
    }


def propose(wb, run, items, inspected_episodes, track_by="agent"):
    """Stage agent-authored object annotations against the frozen evidence.

    Used when SAM3 is unavailable, too costly for the scope, or simply not
    needed: the agent outlines the objects it can already see in the evidence
    frames it read, and a human reviews them exactly like worker output.
    """
    from .runtime import new_id

    evidence = {}
    for ep in sorted(set(run.get("prepared", []) + run.get("completed", []))):
        for row in wb.store.get("evidence", f"{run['id']}:{ep}")["items"]:
            evidence[row["id"]] = row
    rows, episodes, cameras, concepts, last_frame = [], set(), set(), set(), 0
    tracks = {}
    for item in items:
        source = evidence.get(item.evidence_id)
        if source is None:
            raise ValueError(f"Unknown evidence id: {item.evidence_id}")
        if not source.get("artifact"):
            raise ValueError("Object proposals need image evidence, not table rows")
        height, width = source["source_size"]
        item = _resolve(wb, run, item)
        x1, y1, x2, y2 = item.bbox_xyxy
        if x2 <= x1 or y2 <= y1 or x1 < 0 or y1 < 0 or x2 > width or y2 > height:
            raise ValueError(
                f"Bounding box {item.bbox_xyxy} is empty or outside the {width}x{height} frame"
            )
        if item.polygon and any(
            not (0 <= x <= width and 0 <= y <= height) for x, y in item.polygon
        ):
            raise ValueError("Polygon points must stay inside the frame")
        key = (
            source["episode_index"],
            source["camera_key"],
            item.object_id or item.concept,
        )
        track = tracks.setdefault(key, len(tracks))
        rows.append(
            ObjectAnnotation(
                episode_index=source["episode_index"],
                frame_index=source["frame_index"],
                timestamp=source["timestamp"],
                camera_key=source["camera_key"],
                object_id=item.object_id or f"{item.concept}-{track}",
                track_id=item.track_id if item.track_id is not None else track,
                concept=item.concept,
                category=item.category,
                bbox_xyxy=[float(v) for v in item.bbox_xyxy],
                image_size=[int(height), int(width)],
                mask_rle=_rle(int(height), int(width), item.polygon, item.bbox_xyxy),
                score=item.score,
                visible=item.visible,
                occluded=item.occluded,
                source="agent",
                prompt=item.concept,
            )
        )
        episodes.add(source["episode_index"])
        cameras.add(source["camera_key"])
        concepts.add(item.concept)
        last_frame = max(last_frame, source["frame_index"] + 1)
    if not rows:
        raise ValueError("No object proposals supplied")
    unknown = set(inspected_episodes) - set(run["context"]["episodes"])
    if unknown:
        raise ValueError(f"Episodes outside the approved scope: {sorted(unknown)}")
    job_id = new_id(set(wb.store.ids("object_jobs")))
    plan_value = Sam3Plan(
        local_path=str(wb.store.run_dir(run["id"]) / "input"),
        episode_indices=sorted(episodes),
        camera_keys=sorted(cameras),
        prompts=sorted(concepts),
        max_frames=last_frame,
        provider="agent",
    )
    wb.store.put(
        "object_jobs",
        job_id,
        {
            "id": job_id,
            "run_id": run["id"],
            "status": "planned",
            "plan": plan_value.model_dump(),
            "dataset_root": str(wb.store.run_dir(run["id"]) / "input"),
            "readiness": {"available": True, "provider": "agent"},
        },
    )
    # Identity is the part an agent annotating frame by frame cannot supply,
    # so it is either linked here or reported on, never assumed correct.
    linked = link_tracks(rows) if track_by == "overlap" else None
    result = stage(wb, job_id, rows, "agent")
    result["identity"] = identity_report(rows)
    if linked:
        result["identity"]["linked_by_overlap"] = linked
    return result


def _resolve(wb, run, item):
    """Fill an outline the agent referenced instead of repeating."""
    if not item.candidate_id:
        if len(item.bbox_xyxy) != 4:
            raise ValueError(
                "Give either a candidate_id from objects.detect or a bbox_xyxy"
            )
        return item
    try:
        found = wb.store.get("detections", f"{run['id']}:{item.evidence_id}")
    except KeyError:
        raise ValueError(
            f"No detection for evidence {item.evidence_id}; run objects.detect first"
        ) from None
    for candidate in found["candidates"]:
        if candidate["candidate_id"] == item.candidate_id:
            return item.model_copy(
                update={
                    "bbox_xyxy": item.bbox_xyxy or candidate["bbox_xyxy"],
                    "polygon": item.polygon or candidate["polygon"],
                }
            )
    raise ValueError(
        f"Candidate {item.candidate_id} is not among the ones detected for "
        f"evidence {item.evidence_id}"
    )


def result_rows(wb, job):
    if job["status"] != "waiting_for_review":
        raise ValueError("Object job is not ready for review")
    directory = wb.store.run_dir(job["run_id"]) / "objects" / job["id"]
    draft = SidecarStore(directory / "draft")
    if draft.current_revision():
        rows = [ObjectAnnotation.model_validate(r) for r in draft.read_annotations()]
    else:
        result = json.loads((directory / "result.json").read_text())
        rows = [ObjectAnnotation.model_validate(r) for r in result["annotations"]]
    validate_annotations_for_plan(Sam3Plan.model_validate(job["plan"]), rows)
    # A worker cannot invent timestamps or frames outside the frozen source ledger.
    from . import media
    from .schema import TaskContext

    ctx = TaskContext.model_validate(wb.store.get("runs", job["run_id"])["context"])
    state = media.snapshot_state(ctx, wb.store.run_dir(job["run_id"]) / "input")
    for episode in {r.episode_index for r in rows}:
        table = media.episode_table(state, episode)
        timestamps = dict(zip(table.frame_index, table.timestamp, strict=True))
        for row in rows:
            if row.episode_index == episode and (
                row.frame_index not in timestamps
                or abs(row.timestamp - timestamps[row.frame_index]) > 1e-5
            ):
                raise ValueError("Object result does not match the source frame ledger")
    return rows


def inspect_result(wb, job, offset=0):
    rows = result_rows(wb, job)
    keys = sorted({(r.episode_index, r.camera_key, r.frame_index) for r in rows})
    frame = keys[min(offset, len(keys) - 1)] if keys else None
    current = [
        r for r in rows if (r.episode_index, r.camera_key, r.frame_index) == frame
    ]
    evidence = None
    if frame:
        from . import media
        from .schema import TaskContext

        context = TaskContext.model_validate(
            wb.store.get("runs", job["run_id"])["context"]
        )
        _, refs = media.sample(
            context.model_copy(update={"cameras": [frame[1]]}),
            wb.store.run_dir(job["run_id"]) / "input",
            frame[0],
            wb.store.run_dir(job["run_id"]) / "evidence",
            frame_indices=[frame[2]],
        )
        evidence = refs[0]
        wb.store.put(
            "object_evidence",
            f"{job['run_id']}:{evidence['id']}",
            {"run_id": job["run_id"], **evidence},
        )
    draft = SidecarStore(
        wb.store.run_dir(job["run_id"]) / "objects" / job["id"] / "draft"
    )
    uncertain = {
        (r.episode_index, r.camera_key, r.frame_index)
        for r in rows
        if r.score < 0.6 or r.occluded
    }
    return {
        "frames": len(keys),
        "offset": offset,
        "problem_offsets": [i for i, key in enumerate(keys) if key in uncertain],
        "evidence": evidence,
        "rows": [r.model_dump() for r in current],
        "revision": draft.current_revision(),
        "pending": sum(r.status not in {"accepted", "rejected"} for r in rows),
        "scope": "frame range only; accepted tracks are added without deleting previous annotations",
    }


def edit_result(wb, job, edit: ObjectEdit):
    from .store import Conflict, dataset_lock

    run = wb.store.get("runs", job["run_id"])
    with dataset_lock(wb.store.state, run["dataset_key"]):
        change = wb.store.get("changes", run["changes"])
        if change["status"] == "committed":
            raise Conflict("Object changes have already been committed")
        result_rows(wb, job)
        if (
            edit.episode_index not in job["plan"]["episode_indices"]
            or edit.camera_key not in job["plan"]["camera_keys"]
        ):
            raise ValueError("Object edit escapes the selected scope")
        draft = SidecarStore(
            wb.store.run_dir(job["run_id"]) / "objects" / job["id"] / "draft"
        )
        if edit.base_revision != draft.current_revision():
            raise Conflict("Staged object revision changed; reload before editing")
        result = draft.apply_edit(edit)
        wb.store.mutate(
            "changes",
            change["id"],
            lambda c: c.update(status="draft", revision=c["revision"] + 1),
        )
        return result


def cancel(wb, run_id):
    for job in wb.store.list("object_jobs"):
        if job["run_id"] == run_id and job["status"] in {"queued", "running"}:
            process = _PROCESSES.get(job["id"])
            if process and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            wb.store.mutate(
                "object_jobs", job["id"], lambda j: j.update(cancel_requested=True)
            )


def frame_result(wb, job, episode, camera, timestamp, window_seconds=0):
    """A missing mask is empty; never interpolate an occluded/unprocessed object."""
    from . import media
    from .schema import TaskContext

    context = TaskContext.model_validate(wb.store.get("runs", job["run_id"])["context"])
    if episode not in context.episodes or camera not in context.cameras:
        raise ValueError("Preview outside approved episode/camera")
    root = wb.store.run_dir(job["run_id"]) / "input"
    table = media.episode_table(media.snapshot_state(context, root), episode)
    times = table.timestamp.to_numpy(dtype=float)
    import numpy as np

    index = int(np.searchsorted(times, timestamp, side="right")) - 1
    if index < 0 and window_seconds and timestamp + window_seconds > times[0]:
        index = 0
    if index < 0 or timestamp > times[-1]:
        return {"rows": [], "frame_index": None}
    frame = int(table.iloc[index].frame_index)
    selected = (
        table.iloc[index : index + 1]
        if not window_seconds
        else table[
            (table.timestamp >= times[index])
            & (table.timestamp < timestamp + window_seconds)
        ]
    )
    frames = set(map(int, selected.frame_index))
    rows = [
        r.model_dump()
        for r in result_rows(wb, job)
        if r.episode_index == episode
        and r.camera_key == camera
        and r.frame_index in frames
        and r.visible
        and r.status != "rejected"
    ]
    return {
        "rows": rows,
        "ledger": [
            {"frame": int(row.frame_index), "timestamp": float(row.timestamp)}
            for _, row in selected.iterrows()
        ],
        "frame_index": frame,
        "timestamp": float(times[index]),
        "source": "persistent-staging",
    }
