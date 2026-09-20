"""SAM3 ToolProvider: stages existing worker results without publishing annotations.

No worker imports, checkpoint downloads or device probes in the control plane.
"""

import json
import os
import signal
import sqlite3
import subprocess
import threading
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
    return {
        "enabled": enabled,
        "checkpoint_ready": ready,
        "worker_ready": worker.is_file() and os.access(worker, os.X_OK),
        "cuda_probe_performed": False,
        "available": enabled
        and ready
        and worker.is_file()
        and os.access(worker, os.X_OK),
    }


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
    id = new_id()
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


def launch(wb, id):
    job = wb.store.get("object_jobs", id)
    from .planning import require

    require(wb, wb.store.get("runs", job["run_id"]))
    if not readiness()["available"]:
        raise ValueError(
            "SAM3 unavailable: configure worker and checkpoint in the SAM3 panel; no automatic download"
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
                    raise ValueError("SAM3 worker failed; review the local worker log")
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
                draft_store = SidecarStore(directory / "draft")
                draft_store.publish(
                    rows, model={"provider": "sam3", "run_id": job["run_id"]}
                )
                wb.store.mutate(
                    "object_jobs",
                    id,
                    lambda j: j.update(status="waiting_for_review", count=len(rows)),
                )
                run = wb.store.get("runs", job["run_id"])
                if run["context"]["workflow"]["kind"] == "objects":
                    wb.store.mutate(
                        "runs",
                        run["id"],
                        lambda r: r.update(
                            completed=sorted(
                                set(r["completed"] + job["plan"]["episode_indices"])
                            )
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
                        raise ValueError(
                            "Run already committed; object result remains staged"
                        )
                    change.setdefault("object_jobs", []).append(id)
                    change.update(status="draft", revision=change["revision"] + 1)

                wb.store.mutate("changes", run["changes"], attach)
                wb.store.mutate(
                    "runs", run["id"], lambda r: r.update(status="waiting_for_review")
                )
                wb.store.event(run["id"], "objects_staged", job_id=id, count=len(rows))
        except Exception:  # noqa: BLE001 - never expose worker exception payloads
            wb.store.mutate(
                "object_jobs",
                id,
                lambda j: j.update(
                    status="blocked",
                    reason="Worker failed or cancelled; result not published",
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


def result_rows(wb, job):
    if job["status"] != "waiting_for_review":
        raise ValueError("Object job is not ready for review")
    directory = wb.store.run_dir(job["run_id"]) / "objects" / job["id"]
    draft = SidecarStore(directory / "draft")
    if draft.current_revision():
        rows = [
            ObjectAnnotation.model_validate(draft._from_mask_row(r))
            for r in draft.read_annotations()
        ]
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
