"""Fast-segmentation jobs: label episodes with a student, distil a student.

Modelled on the SAM3 and RECAP jobs: an immutable plan in a staging folder,
one worker process in its own session and log, a watch thread that stops a
worker running too long or without progress, and a collector that turns a
finished worker's output into exactly one result (a sidecar revision for a
labelling job, a listed model for a distillation). ``provider="fake"`` runs
the same worker module with LEVI's own Python (no Torch, no GPU).

Records live in ``outputs/LEVI/workbench/segmentation/<dataset>/``:
``jobs/<id>.json`` (+ ``.log``, ``.progress.json``), ``plans/``,
``results/`` and ``staging/<id>/`` (worker output, removed once published).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from .. import catalog, children, naming, paths
from . import models

ACTIVE = {"queued", "running"}
FINISHED = {"succeeded", "failed", "cancelled"}
KINDS = ("label", "distil")
WORKER_PROJECT = paths.PROJECT / "integrations" / "segmentation"
SAM3_PROJECT = paths.PROJECT / "integrations" / "sam3"
LOG_TAIL_BYTES = 4000
PUBLIC_KEYS = (
    "id",
    "kind",
    "provider",
    "dataset",
    "model",
    "status",
    "progress",
    "error",
    "error_detail",
    "revision_id",
    "annotation_count",
    "item_errors",
    "created_at",
    "finished_at",
    "warnings",
    "timing",
    "request",
)

_PROCESSES: dict[tuple[str, str], subprocess.Popen] = {}
_WATCHERS: list[threading.Thread] = []
_LOCK = threading.RLock()


class SegError(Exception):
    """A refusal carrying the HTTP status the backend answers with."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


# ---------------------------------------------------------------- settings


def worker_python() -> Path:
    return Path(
        os.getenv("LEVI_SEG_WORKER_PYTHON", str(WORKER_PROJECT / ".venv/bin/python"))
    ).expanduser()


def teacher_python() -> Path:
    return Path(
        os.getenv("LEVI_SAM3_WORKER_PYTHON", str(SAM3_PROJECT / ".venv/bin/python"))
    ).expanduser()


def worker_state() -> dict[str, Any]:
    """Is the student environment present? Never imports Torch."""
    if not (WORKER_PROJECT / "levi_seg_worker" / "cli.py").is_file():
        return {"ready": False, "reason": "worker source is missing from this checkout"}
    python = worker_python()
    if not python.is_file():
        return {
            "ready": False,
            "reason": f"worker environment not found at {python}; run integrations/segmentation/setup.sh",
        }
    return {"ready": True, "reason": None, "python": str(python)}


def command(provider: str) -> list[str]:
    python = sys.executable if provider == "fake" else str(worker_python())
    return [python, "-m", "levi_seg_worker.cli"]


def _min_free(kind: str) -> int:
    defaults = {"label": 3000, "distil": 14000, "live": 2500}
    names = {
        "label": "LEVI_SEG_MIN_FREE_MIB",
        "distil": "LEVI_SEG_DISTIL_MIN_FREE_MIB",
        "live": "LEVI_SEG_LIVE_MIN_FREE_MIB",
    }
    return int(os.getenv(names[kind], str(defaults[kind])))


def gpu_lock_file() -> str | None:
    value = os.getenv("LEVI_GPU_LOCK_FILE", "").strip()
    return value or None


def check_gpu(kind: str) -> list[str]:
    """Refuse (or, with a GPU lock configured, only warn) when the GPU lacks
    the memory ``kind`` needs. With a lock the worker queues for the GPU
    instead, so contention is expected, not an error."""
    if os.getenv("LEVI_CPU_ONLY") == "1":
        raise SegError(400, "LEVI_CPU_ONLY=1 forbids running a segmentation model")
    from ..agent.objects import gpu_headroom

    gpu = gpu_headroom()
    need = _min_free(kind)
    if gpu.get("known") and gpu["free_mib"] < need:
        message = f"Only {gpu['free_mib']} MiB of GPU memory is free; this needs about {need} MiB"
        if gpu_lock_file() and kind != "live":
            return [message + "; the job waits for the GPU lock"]
        raise SegError(409, message)
    if not gpu.get("known"):
        return [f"GPU memory could not be read ({gpu.get('reason')})"]
    return []


def _env(extra: dict[str, str] | None = None) -> dict[str, str]:
    base = models.base_weights_dir()
    base.mkdir(parents=True, exist_ok=True)
    return {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
        "PYTHONWARNINGS": "ignore",
        # RF-DETR keeps its COCO starting weights in the workspace.
        "RF_HOME": str(base),
        "TOKENIZERS_PARALLELISM": "false",
        **(extra or {}),
    }


# ---------------------------------------------------------------- records


def root(dataset: str) -> Path:
    return paths.STATE / "segmentation" / naming.catalog_name(dataset)


def _jobs_dir(dataset: str) -> Path:
    return root(dataset) / "jobs"


def job_path(dataset: str, job_id: str) -> Path:
    if not naming.is_timestamp_id(job_id):
        raise SegError(400, "Invalid segmentation job ID")
    return _jobs_dir(dataset) / f"{job_id}.json"


_HELD = threading.local()


@contextlib.contextmanager
def locked(dataset: str) -> Iterator[None]:
    """One writer per dataset across threads and LEVI processes; re-entrant
    within a thread (the same process flocking twice would deadlock)."""
    with _LOCK:
        depth = _HELD.__dict__.setdefault("depth", {})
        if depth.get(dataset):
            depth[dataset] += 1
            try:
                yield
            finally:
                depth[dataset] -= 1
            return
        folder = root(dataset)
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / ".lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            depth[dataset] = 1
            try:
                yield
            finally:
                depth[dataset] = 0
                fcntl.flock(handle, fcntl.LOCK_UN)


def public(job: dict[str, Any]) -> dict[str, Any]:
    return {key: job.get(key) for key in PUBLIC_KEYS}


def _save(job: dict[str, Any]) -> None:
    catalog.atomic(job_path(job["dataset"], job["id"]), job)


def _read(dataset: str, job_id: str) -> dict[str, Any] | None:
    return catalog.read(job_path(dataset, job_id), None)


def jobs(dataset: str) -> list[dict[str, Any]]:
    folder = _jobs_dir(dataset)
    if not folder.is_dir():
        return []
    rows = []
    for path in folder.glob("*.json"):
        if not naming.is_timestamp_id(path.stem):
            continue
        value = catalog.read(path, None)
        if value and value.get("id") == path.stem:
            rows.append(value)
    rows.sort(key=lambda j: (j.get("created_at") or 0, j["id"]))
    return rows


def find(dataset: str, job_id: str) -> dict[str, Any]:
    job = _read(dataset, job_id)
    if job is None:
        raise SegError(404, "Segmentation job not found")
    return job


# ---------------------------------------------------------------- start


def start(
    dataset: str,
    kind: str,
    provider: str,
    plan: dict[str, Any],
    *,
    request: dict[str, Any],
    model: str | None = None,
    warnings: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
    watch: bool = True,
) -> dict[str, Any]:
    """Write the plan and start the worker; returns the job record."""
    if kind not in KINDS:
        raise SegError(400, f"Unknown job kind {kind}")
    if provider != "fake":
        worker = worker_state()
        if not worker["ready"]:
            raise SegError(
                503, "Fast segmentation worker is not ready: " + worker["reason"]
            )
    with locked(dataset):
        for job in reversed(jobs(dataset)):
            if job.get("status") in ACTIVE and job.get("kind") == kind:
                job = collect(job)
                if job.get("status") in ACTIVE:
                    raise SegError(
                        409,
                        f"A {kind} job ({job['id']}) is already running for this dataset",
                    )
        base = root(dataset)
        job_id = naming.timestamp_id(_jobs_dir(dataset), ".json")
        plan_path = base / "plans" / f"{job_id}.json"
        result_path = base / "results" / f"{job_id}.json"
        log_path = _jobs_dir(dataset) / f"{job_id}.log"
        progress_path = _jobs_dir(dataset) / f"{job_id}.progress.json"
        staging = base / "staging" / job_id
        plan = {**plan, "job_id": job_id, "provider": provider}
        if kind == "label":
            plan["output_dir"] = str(staging)
        else:
            plan["work_dir"] = str(staging)
        catalog.atomic(plan_path, plan)
        job = {
            "id": job_id,
            "kind": kind,
            "provider": provider,
            "dataset": dataset,
            "model": model,
            "status": "queued",
            "progress": {"stage": "queued", "done": 0, "total": 0},
            "error": None,
            "error_detail": None,
            "revision_id": None,
            "created_at": time.time(),
            "request": request,
            "warnings": warnings or [],
            "plan_path": str(plan_path),
            "result_path": str(result_path),
            "log_path": str(log_path),
            "progress_path": str(progress_path),
            "staging": str(staging),
        }
        _save(job)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as log:
            try:
                process = subprocess.Popen(
                    [
                        *command(provider),
                        kind,
                        "--plan",
                        str(plan_path),
                        "--output",
                        str(result_path),
                        "--progress",
                        str(progress_path),
                    ],
                    cwd=WORKER_PROJECT,
                    env=_env(extra_env),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=(os.name == "posix"),
                )
            except OSError as exc:
                job.update(
                    status="failed",
                    error=f"Unable to start the segmentation worker: {exc}",
                    finished_at=time.time(),
                )
                _save(job)
                raise SegError(503, job["error"]) from exc
        _PROCESSES[(dataset, job_id)] = process
        children.track(process, f"segmentation-{kind}", job_id)
        job.update(status="running", pid=process.pid, started_at=time.time())
        _save(job)
    if watch:
        thread = threading.Thread(
            target=_watch,
            args=(process, dataset, job_id),
            name=f"seg-watch-{job_id}",
            daemon=True,
        )
        with _LOCK:
            _WATCHERS[:] = [t for t in _WATCHERS if t.is_alive()] + [thread]
        thread.start()
    return job


# ---------------------------------------------------------------- lifecycle


def _log_tail(job: dict[str, Any]) -> str | None:
    try:
        data = Path(str(job.get("log_path"))).read_bytes()
    except OSError:
        return None
    if not data:
        return None
    tail = data[-LOG_TAIL_BYTES:].decode("utf-8", errors="replace")
    if len(data) > LOG_TAIL_BYTES:
        tail = "...(truncated)...\n" + tail
    return re.sub(r"(?i)(token|authorization)=[^&\s]+", r"\1=<redacted>", tail).strip()


def _alive(job: dict[str, Any]) -> bool:
    process = _PROCESSES.get((job["dataset"], job["id"]))
    if process is not None:
        return process.poll() is None
    pid = job.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    return children.identity(pid) is not None


def _forget(key: tuple[str, str]) -> None:
    process = _PROCESSES.pop(key, None)
    if process is not None:
        process.poll()
        children.untrack(process.pid)


def _stop(process: subprocess.Popen, grace: float = 10.0) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=grace)
    except ProcessLookupError:
        pass
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        process.wait()


def _watch(process: subprocess.Popen, dataset: str, job_id: str) -> None:
    """Stop a worker past its time limit or without progress."""
    limit = float(os.getenv("LEVI_SEG_TIMEOUT_SECONDS", "43200"))
    stall = float(os.getenv("LEVI_SEG_STALL_SECONDS", "1800"))
    job = _read(dataset, job_id) or {}
    watched = [Path(job.get("progress_path", "")), Path(job.get("log_path", ""))]
    started = time.time()
    reason = None
    while True:
        try:
            process.wait(timeout=1)
            break
        except subprocess.TimeoutExpired:
            pass
        now = time.time()
        active_at = max([started] + [p.stat().st_mtime for p in watched if p.is_file()])
        if now - started > limit:
            reason = f"Segmentation worker ran longer than {limit:.0f} s and was stopped (LEVI_SEG_TIMEOUT_SECONDS)"
        elif now - active_at > stall:
            reason = f"Segmentation worker made no progress for {stall:.0f} s and was stopped (LEVI_SEG_STALL_SECONDS)"
        else:
            continue
        _stop(process)
        break
    if reason:
        with locked(dataset):
            current = _read(dataset, job_id)
            if current and current.get("status") in ACTIVE:
                current.update(
                    status="failed",
                    error=reason,
                    error_detail=_log_tail(current),
                    finished_at=time.time(),
                )
                _save(current)
        _forget((dataset, job_id))
        return
    current = _read(dataset, job_id)
    # A distillation needs no dataset transaction to finish: register it now.
    if current and current.get("kind") == "distil":
        collect(current)


def wait_idle(timeout: float = 60) -> bool:
    """Join every watch thread; True if one is still running (tests)."""
    deadline = time.time() + timeout
    with _LOCK:
        threads = list(_WATCHERS)
    for thread in threads:
        thread.join(max(0.0, deadline - time.time()))
    return any(t.is_alive() for t in threads)


def _progress(job: dict[str, Any]) -> dict[str, Any] | None:
    value = catalog.read(Path(str(job.get("progress_path"))), None)
    if not isinstance(value, dict) or "stage" not in value:
        return None
    return {k: v for k, v in value.items() if k != "updated_at"}


Publisher = Callable[
    [list[dict[str, Any]], set[tuple[int, str]], dict[str, Any]], dict[str, Any]
]


def _read_rows(path: Path) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    rows = []
    for row in pq.read_table(path).to_pylist():
        row["mask_rle"] = {"size": row.pop("rle_size"), "counts": row.pop("rle_counts")}
        rows.append(row)
    return rows


def awaiting_publish(job: dict[str, Any]) -> bool:
    """A finished labelling job whose rows still need a sidecar publish
    (the only case a caller has to open an annotation transaction for)."""
    return (
        job.get("kind") == "label"
        and job.get("status") in ACTIVE
        and Path(str(job.get("result_path"))).is_file()
    )


def collect(job: dict[str, Any], publish: Publisher | None = None) -> dict[str, Any]:
    """Reconcile a finished worker into its result, exactly once.

    A labelling job needs ``publish`` (the backend passes one bound to the
    dataset's sidecar inside its annotation transaction); without it a
    finished labelling job stays ``running`` until a caller that can
    publish looks at it."""
    if job.get("status") in FINISHED:
        _forget((job["dataset"], job["id"]))
        return job
    dataset = job["dataset"]
    result_path = Path(str(job["result_path"]))
    process = _PROCESSES.get((dataset, job["id"]))
    return_code = process.poll() if process is not None else None
    if not result_path.is_file() and return_code is None and _alive(job):
        progress = _progress(job)
        if progress and progress != job.get("progress"):
            with locked(dataset):
                current = _read(dataset, job["id"]) or job
                if current.get("status") in ACTIVE:
                    current["progress"] = progress
                    _save(current)
                job = current
        return job
    if result_path.is_file() and job.get("kind") == "label" and publish is None:
        return job
    with locked(dataset):
        job = _read(dataset, job["id"]) or job
        if job.get("status") in FINISHED:
            _forget((dataset, job["id"]))
            return job
        if result_path.is_file():
            try:
                result = json.loads(result_path.read_text())
                if result.get("status") != "succeeded":
                    raise ValueError(
                        result.get("error") or "the worker reported a failure"
                    )
                if job["kind"] == "label":
                    _finish_label(job, result, publish)
                else:
                    _finish_distil(job, result)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                job.update(
                    status="failed",
                    error=f"Segmentation worker result rejected: {exc}",
                    error_detail=_log_tail(job),
                    finished_at=time.time(),
                )
        else:
            job.update(
                status="failed",
                error=f"Segmentation worker exited without a result (code {return_code})",
                error_detail=_log_tail(job),
                finished_at=time.time(),
            )
        _save(job)
    _forget((dataset, job["id"]))
    return job


def _finish_label(
    job: dict[str, Any], result: dict[str, Any], publish: Publisher | None
) -> None:
    # TODO(S2, pre-merge review): this loads every row of the run into the
    # core process and publishes them in one revision inside the request
    # that found the job finished. Before labelling large datasets, publish
    # the worker's per-(episode, camera) Parquet files as they are (or in
    # batches of episodes) instead of round-tripping them through Python.
    assert publish is not None
    rows: list[dict[str, Any]] = []
    pairs: set[tuple[int, str]] = set()
    for item in result.get("files", []):
        pairs.add((int(item["episode_index"]), str(item["camera_key"])))
        rows.extend(_read_rows(Path(item["path"])))
    engine = result.get("engine") or {}
    revision = publish(
        rows,
        pairs,
        {
            "provider": "student" if job["provider"] != "fake" else "fake",
            "model_version": engine.get("model") or job.get("model"),
            "precision": engine.get("precision"),
            "job_id": job["id"],
        },
    )
    job.update(
        status="succeeded",
        finished_at=time.time(),
        revision_id=revision["revision_id"],
        annotation_count=len(rows),
        item_errors=result.get("item_errors", []),
        timing=result.get("timing"),
        progress={"stage": "saved", "done": 1, "total": 1},
    )
    shutil.rmtree(job["staging"], ignore_errors=True)


def _finish_distil(job: dict[str, Any], result: dict[str, Any]) -> None:
    directory = Path(result["model_dir"])
    name = directory.name
    _, manifest = models.load(name)
    job.update(
        status="succeeded",
        finished_at=time.time(),
        model=name,
        timing={
            "seconds": manifest.get("seconds"),
            "stages": manifest.get("stage_seconds"),
        },
        progress={"stage": "registered", "done": 1, "total": 1},
    )


def cancel(job: dict[str, Any]) -> dict[str, Any]:
    if job.get("status") in FINISHED:
        _forget((job["dataset"], job["id"]))
        return job
    process = _PROCESSES.get((job["dataset"], job["id"]))
    if process is not None and process.poll() is None:
        _stop(process, grace=5.0)
    elif isinstance(job.get("pid"), int) and children.identity(job["pid"]) is not None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(job["pid"], signal.SIGTERM)
    with locked(job["dataset"]):
        current = _read(job["dataset"], job["id"]) or job
        if current.get("status") in ACTIVE:
            current.update(status="cancelled", finished_at=time.time())
            _save(current)
        job = current
    _forget((job["dataset"], job["id"]))
    shutil.rmtree(job.get("staging") or "", ignore_errors=True)
    if job.get("kind") == "distil" and job.get("model"):
        # The model folder is only listed with a complete manifest; a
        # cancelled run leaves at most a partial one behind.
        with contextlib.suppress(ValueError):
            folder = models.folder(job["model"])
            if not (folder / models.MANIFEST).is_file():
                shutil.rmtree(folder, ignore_errors=True)
    return job


def levi_commit() -> str | None:
    try:
        done = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=paths.PROJECT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return (done.stdout.strip() or None) if done.returncode == 0 else None
