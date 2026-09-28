"""RECAP value jobs: plan, worker process, watch, collect and publish.

Modelled on the SAM3 run in backend/app.py: a plan JSON in a staging
folder, a worker process in its own session with its own log, a watch
thread that stops a worker running too long or making no progress, and a
collector that turns the worker's values into one published revision
exactly once. The ``fake`` provider runs the same worker module with LEVI's
own Python (no Torch); ``rlinf`` runs it in integrations/recap_value/.venv.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq

from .. import catalog, children, naming, paths
from ..revision import dataset_revision
from ..versions import is_dataset_v2
from . import advantage, checkpoints, store

ACTIVE = {"queued", "running"}
FINISHED = {"succeeded", "failed", "cancelled"}
PLAN_SCHEMA = "levi.recap_value.plan.v1"
WORKER_PROJECT = paths.PROJECT / "integrations" / "recap_value"
LOG_TAIL_BYTES = 4000
PUBLIC_JOB_KEYS = (
    "id",
    "repo_id",
    "checkpoint",
    "status",
    "progress",
    "error",
    "revision_id",
    "created_at",
)

# Keyed by (dataset name, job id): ids are unique per dataset only.
_PROCESSES: dict[tuple[str, str], subprocess.Popen] = {}
_WATCHERS: list[threading.Thread] = []
_LOCK = threading.RLock()


class RecapError(Exception):
    """A refusal with the HTTP status the backend answers with."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


# ---------------------------------------------------------------- dataset


@dataclass
class Dataset:
    repo_id: str
    name: str
    root: Path
    info: dict[str, Any]
    rows: dict[int, dict[str, Any]]
    tasks: dict[int, str]
    human: dict[int, str] = field(default_factory=dict)

    @property
    def fps(self) -> float:
        return float(self.info.get("fps") or 0.0)

    def outcome(self, episode: int) -> str | None:
        """A human label wins over the capture's own ``levi_outcome`` (the
        same rule as the RECAP export); an RLinf-format RECAP dataset's
        per-episode ``is_success`` (meta/episodes.jsonl) comes last."""
        if episode in self.human:
            return self.human[episode]
        row = self.rows.get(episode, {})
        value = row.get("levi_outcome")
        if value in ("success", "failure"):
            return value
        flag = row.get("is_success")
        if isinstance(flag, bool):
            return "success" if flag else "failure"
        return None

    def data_path(self, episode: int) -> Path:
        chunk = int(self.info.get("chunks_size") or 1000)
        return self.root / self.info["data_path"].format(
            episode_chunk=episode // chunk, episode_index=episode
        )

    def fingerprint(self) -> dict[str, Any]:
        view = catalog.read(self.root / "meta/levi_view.json", {})
        return {
            "source_fingerprint": view.get("source_fingerprint"),
            "dataset_revision": dataset_revision(self.root),
        }


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def dataset(repo_id: str) -> Dataset:
    """The registered local dataset behind ``local/<name>`` (a namespace
    reads its base's folder but keeps its own name, labels and results)."""
    if not repo_id or not repo_id.startswith("local/"):
        raise RecapError(
            400, "RECAP value labels are computed for registered local datasets"
        )
    try:
        name = catalog.resolve_name(repo_id)
        root = catalog.local_root(repo_id)
    except ValueError as exc:
        raise RecapError(400, str(exc)) from exc
    info = catalog.read(root / "meta/info.json", None)
    if not info:
        raise RecapError(400, "The dataset has no meta/info.json")
    if not is_dataset_v2(str(info.get("codebase_version", ""))):
        raise RecapError(
            400,
            "RECAP value labelling reads LeRobot v2.x datasets (one parquet and "
            "one video per episode); convert this dataset first",
        )
    rows = {int(r["episode_index"]): r for r in _jsonl(root / "meta/episodes.jsonl")}
    tasks = {
        int(t.get("task_index", i)): str(t.get("task", ""))
        for i, t in enumerate(_jsonl(root / "meta/tasks.jsonl"))
    }
    from ..agent.store import resolve
    from ..annotations import outcomes

    labels = outcomes.read_labels(resolve(catalog.STATE, name, "annotations"))
    human = {ep: v["outcome"] for ep, v in labels.items()}
    return Dataset(repo_id, name, root, info, rows, tasks, human)


# ---------------------------------------------------------------- records


def _jobs_dir(name: str) -> Path:
    return store.root(name) / "jobs"


def job_path(name: str, job_id: str) -> Path:
    if not naming.is_timestamp_id(job_id):
        raise RecapError(400, "Invalid RECAP job ID")
    return _jobs_dir(name) / f"{job_id}.json"


def public(job: dict[str, Any]) -> dict[str, Any]:
    return {key: job.get(key) for key in PUBLIC_JOB_KEYS}


def _save(job: dict[str, Any]) -> None:
    catalog.atomic(job_path(job["name"], job["id"]), job)


def _read(name: str, job_id: str) -> dict[str, Any] | None:
    return store.load_json(job_path(name, job_id))


def jobs(name: str) -> list[dict[str, Any]]:
    folder = _jobs_dir(name)
    if not folder.is_dir():
        return []
    rows = []
    for path in folder.glob("*.json"):
        if path.name.endswith(".progress.json") or not naming.is_timestamp_id(
            path.stem
        ):
            continue
        value = store.load_json(path)
        if value and value.get("id") == path.stem:
            rows.append(value)
    rows.sort(key=lambda j: (j.get("created_at") or 0, j["id"]))
    return rows


def find(job_id: str, name: str | None = None) -> dict[str, Any] | None:
    """A job by id, in the given dataset or (without one) any dataset."""
    if not naming.is_timestamp_id(job_id):
        raise RecapError(400, "Invalid RECAP job ID")
    if name:
        return _read(name, job_id)
    base = catalog.STATE / "recap_values"
    if not base.is_dir():
        return None
    for folder in sorted(base.iterdir()):
        path = folder / "jobs" / f"{job_id}.json"
        if path.is_file():
            return store.load_json(path)
    return None


def latest(name: str) -> dict[str, Any] | None:
    rows = jobs(name)
    return collect(rows[-1]) if rows else None


def active(name: str) -> dict[str, Any] | None:
    for job in reversed(jobs(name)):
        if job.get("status") in ACTIVE:
            job = collect(job)
            if job.get("status") in ACTIVE:
                return job
    return None


# ---------------------------------------------------------------- worker


def worker_state() -> dict[str, Any]:
    """Whether the rlinf worker environment is present (never imports Torch)."""
    python = checkpoints.worker_python()
    if not (WORKER_PROJECT / "levi_recap_worker" / "cli.py").is_file():
        return {"ready": False, "reason": "worker source is missing from this checkout"}
    if not python.is_file():
        return {
            "ready": False,
            "reason": f"worker environment not found at {python}; run "
            "integrations/recap_value/setup.sh",
        }
    return {"ready": True, "reason": None}


def worker_command(provider: str) -> list[str]:
    python = sys.executable if provider == "fake" else str(checkpoints.worker_python())
    return [python, "-m", "levi_recap_worker.cli"]


def _min_free_mib() -> int:
    return int(os.getenv("LEVI_RECAP_VALUE_MIN_FREE_MIB", "6000"))


def _batch_size() -> int:
    value = int(os.getenv("LEVI_RECAP_VALUE_BATCH_SIZE", "32"))
    if value < 1:
        raise RecapError(400, "LEVI_RECAP_VALUE_BATCH_SIZE must be at least 1")
    return value


def _device() -> str:
    value = os.getenv("LEVI_RECAP_VALUE_DEVICE", "auto")
    if value not in ("auto", "cuda", "cpu") and not re.fullmatch(r"cuda:\d+", value):
        raise RecapError(400, "LEVI_RECAP_VALUE_DEVICE is auto, cpu, cuda or cuda:N")
    return value


def _refuse_rlinf(folder: Path, manifest: checkpoints.Manifest, ds: Dataset) -> None:
    issues = checkpoints.problems(folder, manifest)
    if issues:
        raise RecapError(
            400, f"Checkpoint {manifest.name} is not ready: " + "; ".join(issues)
        )
    worker = worker_state()
    if not worker["ready"]:
        raise RecapError(400, "RECAP value worker is not ready: " + worker["reason"])
    features = ds.info.get("features") or {}
    for slot, key in manifest.views.model_dump().items():
        if key and (features.get(key) or {}).get("dtype") != "video":
            raise RecapError(
                400,
                f"Checkpoint view {slot} = {key} is not a video feature of this dataset",
            )
    if os.getenv("LEVI_CPU_ONLY") == "1":
        raise RecapError(400, "LEVI_CPU_ONLY=1 forbids running the RECAP value model")
    device = _device()
    if device == "cpu":
        return
    from ..agent.objects import gpu_headroom

    gpu = gpu_headroom()
    need = _min_free_mib()
    if gpu.get("known") and gpu["free_mib"] < need:
        raise RecapError(
            400,
            f"Only {gpu['free_mib']} MiB of GPU memory is free; the RECAP value "
            f"model needs about {need} MiB (LEVI_RECAP_VALUE_MIN_FREE_MIB)",
        )


def _levi_commit() -> str | None:
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
    return done.stdout.strip() or None if done.returncode == 0 else None


def start(
    repo_id: str,
    checkpoint: str,
    *,
    episodes: list[int] | None = None,
    lookahead: int | None = None,
    positive_quantile: float | None = None,
    threshold: float | None = None,
    dataset_type: str = "rollout",
    watch: bool = True,
) -> dict[str, Any]:
    """Validate, write the plan and start the worker; returns the job."""
    ds = dataset(repo_id)
    try:
        folder, manifest = checkpoints.load(checkpoint)
    except KeyError as exc:
        raise RecapError(400, str(exc.args[0])) from exc
    except ValueError as exc:
        raise RecapError(400, str(exc)) from exc
    if manifest.provider == "rlinf":
        _refuse_rlinf(folder, manifest, ds)
    if dataset_type not in ("rollout", "sft"):
        raise RecapError(400, "dataset_type is rollout or sft")
    if threshold is not None and not np.isfinite(threshold):
        raise RecapError(400, "threshold must be a finite number")
    known = sorted(ds.rows)
    if not known:
        raise RecapError(400, "The dataset has no episodes")
    if episodes is not None:
        unknown = sorted(set(episodes) - set(known))
        if unknown:
            raise RecapError(400, f"Unknown episode indices: {unknown}")
        chosen = sorted(set(episodes))
        if not chosen:
            raise RecapError(400, "episodes is empty")
    else:
        chosen = known
    success: dict[int, bool] = {}
    skipped: dict[str, str] = {}
    for ep in chosen:
        if dataset_type == "sft":
            success[ep] = True
            continue
        outcome = ds.outcome(ep)
        if outcome is None:
            skipped[str(ep)] = "no success/failure label"
        else:
            success[ep] = outcome == "success"
    if skipped and episodes is not None:
        raise RecapError(
            400,
            f"Episodes {sorted(int(e) for e in skipped)} have no success/failure "
            "label; label them, leave them out, or run with dataset_type sft",
        )
    if not success:
        raise RecapError(
            400,
            "No episode has a success/failure label; label outcomes first or run "
            "with dataset_type sft",
        )
    lengths = {}
    for ep in success:
        length = ds.rows[ep].get("length")
        if not isinstance(length, int) or length < 1:
            raise RecapError(400, f"Episode {ep} has no length in meta/episodes.jsonl")
        if not ds.data_path(ep).is_file():
            raise RecapError(400, f"Episode {ep} has no data parquet")
        lengths[ep] = length
    request = {
        "episodes": episodes,
        "lookahead": lookahead if lookahead is not None else manifest.lookahead,
        "positive_quantile": (
            positive_quantile
            if positive_quantile is not None
            else manifest.positive_quantile
        ),
        "threshold": threshold,
        "dataset_type": dataset_type,
    }
    if request["lookahead"] < 1:
        raise RecapError(400, "lookahead must be at least 1")
    if not 0 < request["positive_quantile"] < 1:
        raise RecapError(400, "positive_quantile must lie strictly between 0 and 1")
    with _LOCK, store.locked(ds.name):
        running = active(ds.name)
        if running is not None:
            raise RecapError(
                409,
                f"A RECAP value job ({running['id']}) is already running for this dataset",
            )
        base = store.root(ds.name)
        job_id = naming.timestamp_id(_jobs_dir(ds.name), ".json")
        plan_path = base / "plans" / f"{job_id}.json"
        result_path = base / "results" / f"{job_id}.json"
        log_path = _jobs_dir(ds.name) / f"{job_id}.log"
        progress_path = _jobs_dir(ds.name) / f"{job_id}.progress.json"
        total = int(sum(lengths.values()))
        plan = {
            "schema": PLAN_SCHEMA,
            "job_id": job_id,
            "provider": manifest.provider,
            "checkpoint": {
                "name": manifest.name,
                "dir": str(folder),
                "manifest": manifest.public(),
                "base_models": checkpoints.base_models_status(folder, manifest)
                if manifest.provider == "rlinf"
                else None,
                "dev_only_base_models": checkpoints.dev_only(folder, manifest),
            },
            "dataset": {
                "name": ds.name,
                "root": str(ds.root),
                "fps": ds.fps,
                "data_path": ds.info["data_path"],
                "video_path": ds.info.get("video_path"),
                "chunks_size": int(ds.info.get("chunks_size") or 1000),
                "tasks": {str(k): v for k, v in ds.tasks.items()},
            },
            "episodes": [
                {"episode_index": ep, "length": lengths[ep], "success": success[ep]}
                for ep in sorted(lengths)
            ],
            "batch_size": _batch_size(),
            "device": _device() if manifest.provider == "rlinf" else "cpu",
        }
        catalog.atomic(plan_path, plan)
        job = {
            "id": job_id,
            "repo_id": repo_id,
            "name": ds.name,
            "checkpoint": manifest.name,
            "provider": manifest.provider,
            "status": "queued",
            "progress": {"stage": "loading", "done": 0, "total": total},
            "error": None,
            "error_detail": None,
            "revision_id": None,
            "created_at": time.time(),
            "request": request,
            "success": {str(k): v for k, v in success.items()},
            "skipped_episodes": skipped,
            "plan_path": str(plan_path),
            "result_path": str(result_path),
            "log_path": str(log_path),
            "progress_path": str(progress_path),
        }
        _save(job)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as log_stream:
            try:
                process = subprocess.Popen(
                    [
                        *worker_command(manifest.provider),
                        "--plan",
                        str(plan_path),
                        "--output",
                        str(result_path),
                        "--progress",
                        str(progress_path),
                    ],
                    cwd=WORKER_PROJECT,
                    env={
                        **os.environ,
                        "PYTHONUNBUFFERED": "1",
                        "TOKENIZERS_PARALLELISM": "false",
                        # The worker never downloads: every model file is local.
                        "HF_HUB_OFFLINE": "1",
                        "TRANSFORMERS_OFFLINE": "1",
                    },
                    stdin=subprocess.DEVNULL,
                    stdout=log_stream,
                    stderr=subprocess.STDOUT,
                    start_new_session=(os.name == "posix"),
                )
            except OSError as exc:
                job.update(
                    status="failed",
                    error=f"Unable to start the RECAP value worker: {exc}",
                    finished_at=time.time(),
                )
                _save(job)
                raise RecapError(400, job["error"]) from exc
        _PROCESSES[(ds.name, job_id)] = process
        children.track(process, "recap_value", job_id)
        job.update(status="running", pid=process.pid, started_at=time.time())
        _save(job)
    if watch:
        thread = threading.Thread(
            target=_watch,
            args=(process, ds.name, job_id),
            name=f"recap-watch-{job_id}",
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


def _key(job: dict[str, Any]) -> tuple[str, str]:
    return (job["name"], job["id"])


def _alive(job: dict[str, Any]) -> bool:
    process = _PROCESSES.get(_key(job))
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
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        process.wait()


def _watch(process: subprocess.Popen, name: str, job_id: str) -> None:
    """Stop a worker past its time limit or without progress, and publish
    its result as soon as it exits (the page need not be polling)."""
    limit = float(os.getenv("LEVI_RECAP_VALUE_TIMEOUT_SECONDS", "21600"))
    stall = float(os.getenv("LEVI_RECAP_VALUE_STALL_SECONDS", "1800"))
    job = _read(name, job_id) or {}
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
            reason = (
                f"RECAP value worker ran longer than {limit:.0f} s and was stopped "
                "(LEVI_RECAP_VALUE_TIMEOUT_SECONDS)"
            )
        elif now - active_at > stall:
            reason = (
                f"RECAP value worker made no progress for {stall:.0f} s and was "
                "stopped (LEVI_RECAP_VALUE_STALL_SECONDS)"
            )
        else:
            continue
        _stop(process)
        break
    if reason:
        with store.locked(name):
            job = _read(name, job_id)
            if job and job.get("status") in ACTIVE:
                job.update(
                    status="failed",
                    error=reason,
                    error_detail=_log_tail(job),
                    finished_at=time.time(),
                )
                _save(job)
        _forget((name, job_id))
        return
    job = _read(name, job_id)
    if job:
        collect(job)


def wait_idle(timeout: float = 60) -> bool:
    """Join every watch thread; True if one is still running (tests)."""
    deadline = time.time() + timeout
    with _LOCK:
        threads = list(_WATCHERS)
    for thread in threads:
        thread.join(max(0.0, deadline - time.time()))
    return any(t.is_alive() for t in threads)


def _progress(job: dict[str, Any]) -> dict[str, Any] | None:
    value = store.load_json(Path(str(job.get("progress_path"))))
    if not value or value.get("stage") not in ("loading", "values"):
        return None
    try:
        return {
            "stage": value["stage"],
            "done": int(value.get("done", 0)),
            "total": int(value.get("total", 0)),
        }
    except (TypeError, ValueError):
        return None


def collect(job: dict[str, Any]) -> dict[str, Any]:
    """Reconcile a worker's result into one published revision, exactly once."""
    if job.get("status") in FINISHED:
        _forget(_key(job))
        return job
    name = job["name"]
    result_path = Path(str(job["result_path"]))
    process = _PROCESSES.get(_key(job))
    return_code = process.poll() if process is not None else None
    if not result_path.is_file() and return_code is None and _alive(job):
        progress = _progress(job)
        if progress and progress != job.get("progress"):
            with store.locked(name):
                current = _read(name, job["id"]) or job
                if current.get("status") in ACTIVE:
                    current["progress"] = progress
                    _save(current)
                job = current
        return job
    with store.locked(name):
        # Another thread or process may have published it meanwhile.
        job = _read(name, job["id"]) or job
        if job.get("status") in FINISHED:
            _forget(_key(job))
            return job
        if result_path.is_file():
            try:
                result = json.loads(result_path.read_text())
                if result.get("status") != "succeeded":
                    raise ValueError(
                        result.get("error") or "the worker reported a failure"
                    )
                revision = _publish(job, result)
                job.update(
                    status="succeeded",
                    revision_id=revision["revision_id"],
                    progress={
                        "stage": "saving",
                        "done": revision["frames"],
                        "total": revision["frames"],
                    },
                    finished_at=time.time(),
                )
            except (OSError, ValueError, KeyError, TypeError) as exc:
                job.update(
                    status="failed",
                    error=f"RECAP value worker result rejected: {exc}",
                    error_detail=_log_tail(job),
                    finished_at=time.time(),
                )
        else:
            job.update(
                status="failed",
                error=f"RECAP value worker exited without a result (code {return_code})",
                error_detail=_log_tail(job),
                finished_at=time.time(),
            )
        _save(job)
    _forget(_key(job))
    return job


def cancel(job: dict[str, Any]) -> dict[str, Any]:
    if job.get("status") in FINISHED:
        _forget(_key(job))
        return job
    process = _PROCESSES.get(_key(job))
    if process is not None and process.poll() is None:
        _stop(process, grace=5.0)
    with store.locked(job["name"]):
        current = _read(job["name"], job["id"]) or job
        if current.get("status") in ACTIVE:
            current.update(status="cancelled", finished_at=time.time())
            _save(current)
        job = current
    _forget(_key(job))
    return job


# ---------------------------------------------------------------- publish


def _set_stage(job: dict[str, Any], stage: str, done: int, total: int) -> None:
    job["progress"] = {"stage": stage, "done": done, "total": total}
    _save(job)


def _publish(job: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    plan = json.loads(Path(str(job["plan_path"])).read_text())
    manifest = checkpoints.Manifest.model_validate(plan["checkpoint"]["manifest"])
    values_file = Path(str(job["result_path"])).parent / str(result["values_file"])
    if values_file.parent != Path(str(job["result_path"])).parent:
        raise ValueError("values_file must sit next to the result")
    table = pq.read_table(values_file).to_pandas()
    for column in ("episode_index", "frame_index", "value"):
        if column not in table:
            raise ValueError(f"values table has no {column} column")
    root = Path(plan["dataset"]["root"])
    info = catalog.read(root / "meta/info.json", {})
    chunk = int(info.get("chunks_size") or 1000)
    request = job["request"]
    sft = request["dataset_type"] == "sft"
    lookahead = int(request["lookahead"])
    gamma = float(manifest.gamma)
    total = int(sum(e["length"] for e in plan["episodes"]))
    _set_stage(job, "advantages", 0, total)
    grouped = {int(k): g for k, g in table.groupby("episode_index")}
    per_episode: dict[int, dict[str, np.ndarray]] = {}
    for item in plan["episodes"]:
        ep, n = int(item["episode_index"]), int(item["length"])
        group = grouped.get(ep)
        if group is None:
            raise ValueError(f"no values for episode {ep}")
        group = group.sort_values("frame_index")
        frames = group["frame_index"].to_numpy(np.int64)
        values = group["value"].to_numpy(np.float64)
        data = pq.read_table(
            root
            / plan["dataset"]["data_path"].format(
                episode_chunk=ep // chunk, episode_index=ep
            ),
            columns=["frame_index", "timestamp"],
        )
        expected = data.column("frame_index").to_numpy().astype(np.int64)
        if (
            len(frames) != n
            or len(expected) != n
            or not np.array_equal(frames, expected)
        ):
            raise ValueError(
                f"episode {ep}: {len(frames)} values for {n} frames, or frame "
                "indices that differ from the dataset"
            )
        if not np.all(np.isfinite(values)):
            raise ValueError(f"episode {ep}: non-finite values")
        slack = 1e-3
        if (
            values.min() < manifest.v_min - slack
            or values.max() > manifest.v_max + slack
        ):
            raise ValueError(
                f"episode {ep}: values outside [{manifest.v_min}, {manifest.v_max}]"
            )
        returns, rewards = advantage.episode_rewards(
            n, bool(item["success"]), gamma, float(manifest.failure_reward)
        )
        per_episode[ep] = {
            "frame_index": frames,
            "timestamp": data.column("timestamp").to_numpy().astype(np.float64),
            "value": values,
            "return": returns,
            "reward": rewards,
        }
    ret_min, ret_max = manifest.return_min, manifest.return_max
    range_source = "checkpoint"
    if ret_min is None or ret_max is None:
        # RLinf falls back to the datasets' own return statistics.
        every = np.concatenate([e["return"] for e in per_episode.values()])
        ret_min = float(every.min()) if ret_min is None else ret_min
        ret_max = float(every.max()) if ret_max is None else ret_max
        range_source = "dataset"
    done = 0
    for cols in per_episode.values():
        cols.update(
            advantage.episode_advantages(
                cols["value"],
                cols["return"],
                cols["reward"],
                lookahead=lookahead,
                gamma=gamma,
                ret_min=float(ret_min),
                ret_max=float(ret_max),
            )
        )
        done += len(cols["value"])
    _set_stage(job, "advantages", done, total)
    everything = np.concatenate([c["advantage"] for c in per_episode.values()])
    if request["threshold"] is not None:
        threshold, source = float(request["threshold"]), "manual"
    elif manifest.unified_threshold is not None:
        threshold, source = float(manifest.unified_threshold), "checkpoint"
    else:
        threshold = advantage.quantile_threshold(
            everything, request["positive_quantile"]
        )
        source = "dataset_quantile"
    for cols in per_episode.values():
        cols["positive"] = advantage.label(cols["advantage"], threshold, sft=sft)
    _set_stage(job, "saving", 0, total)
    ds_fingerprint = catalog.read(root / "meta/levi_view.json", {}).get(
        "source_fingerprint"
    )
    meta = {
        "checkpoint": {
            "name": manifest.name,
            "sha256": manifest.sha256,
            "manifest": manifest.public(),
        },
        "provider": manifest.provider,
        "job_id": job["id"],
        "repo_id": job["repo_id"],
        "request": request,
        "dataset_type": request["dataset_type"],
        "threshold": threshold,
        "threshold_source": source,
        "positive_quantile": request["positive_quantile"],
        "lookahead": lookahead,
        "gamma": gamma,
        "failure_reward": float(manifest.failure_reward),
        "return_min": float(ret_min),
        "return_max": float(ret_max),
        "return_range_source": range_source,
        "fingerprint": {
            "source_fingerprint": ds_fingerprint,
            "dataset_revision": dataset_revision(root),
        },
        "outcomes": {
            str(k): ("success" if v else "failure") for k, v in job["success"].items()
        },
        "skipped_episodes": job.get("skipped_episodes", {}),
        "worker": result.get("provenance", {}),
        "base_models": plan["checkpoint"].get("base_models"),
        "dev_only_base_models": plan["checkpoint"].get("dev_only_base_models", []),
        "threshold_provenance": (
            manifest.provenance.get("unified_threshold")
            if source == "checkpoint"
            else None
        ),
        "fps": float(plan["dataset"]["fps"]),
        "levi_commit": _levi_commit(),
    }
    return store.publish(
        job["name"], per_episode, meta, dataset_name=plan["dataset"]["name"]
    )


# ---------------------------------------------------------------- reading


def stale_reasons(ds: Dataset, record: dict[str, Any]) -> list[str]:
    """Why a published revision no longer describes the dataset."""
    reasons = []
    then = record.get("fingerprint") or {}
    now = ds.fingerprint()
    if then.get("source_fingerprint") or now.get("source_fingerprint"):
        if then.get("source_fingerprint") != now.get("source_fingerprint"):
            reasons.append("the capture changed since the labels were computed")
    elif then.get("dataset_revision") != now.get("dataset_revision"):
        reasons.append("the dataset changed since the labels were computed")
    if record.get("dataset_type") != "sft":
        for ep, outcome in (record.get("outcomes") or {}).items():
            if ds.outcome(int(ep)) != outcome:
                reasons.append(f"episode {ep}'s outcome label changed")
                break
    return reasons


def current(ds: Dataset) -> dict[str, Any] | None:
    record = store.revision(ds.name)
    if not record:
        return None
    reasons = stale_reasons(ds, record)
    return {
        "revision_id": record["revision_id"],
        "checkpoint": record["checkpoint"]["name"],
        "provider": record["provider"],
        "created_at": record["created_at"],
        "episodes": record["episodes"],
        "frames": record["frames"],
        "threshold": record["threshold"],
        "threshold_source": record["threshold_source"],
        "positive_quantile": record["positive_quantile"],
        "lookahead": record["lookahead"],
        "positive_fraction": record["positive_fraction"],
        "stale": bool(reasons),
    }


def status(repo_id: str, *, reconcile: bool = True) -> dict[str, Any]:
    """Checkpoints, worker, the current result and the latest job.
    ``reconcile=False`` only reads (an agent's status never publishes)."""
    ds = dataset(repo_id)
    if reconcile:
        job = latest(ds.name)
    else:
        rows = jobs(ds.name)
        job = rows[-1] if rows else None
    return {
        "checkpoints": checkpoints.listing(),
        "worker": worker_state(),
        "current": current(ds),
        "job": public(job) if job else None,
    }


def episode_payload(repo_id: str, episode: int) -> dict[str, Any]:
    ds = dataset(repo_id)
    record = store.revision(ds.name)
    if not record:
        raise RecapError(404, "No advantage labels for this dataset yet")
    table = store.read_episode(ds.name, episode, record["revision_id"])
    if table is None:
        raise RecapError(404, f"No advantage labels for episode {episode}")
    columns = table.to_pydict()
    return {
        "episode_index": episode,
        "revision_id": record["revision_id"],
        "fps": float(record.get("fps") or ds.fps),
        "threshold": record["threshold"],
        "frame_index": columns["frame_index"],
        "timestamp": columns["timestamp"],
        "value": [float(v) for v in columns["value"]],
        "advantage": [float(v) for v in columns["advantage"]],
        "positive": columns["positive"],
    }


def summary_payload(repo_id: str) -> dict[str, Any]:
    ds = dataset(repo_id)
    value = store.summary(ds.name)
    if not value:
        raise RecapError(404, "No advantage labels for this dataset yet")
    return value


def episode_digest(repo_id: str, episode: int) -> dict[str, Any]:
    """An episode's labels for an agent: runs of one label with their time
    span and mean advantage, and V(o_t) about once a second."""
    full = episode_payload(repo_id, episode)
    positive = full["positive"]
    times = full["timestamp"]
    runs, start = [], 0
    for i in range(1, len(positive) + 1):
        if i == len(positive) or positive[i] != positive[start]:
            chunk = full["advantage"][start:i]
            runs.append(
                {
                    "positive": bool(positive[start]),
                    "from_frame": full["frame_index"][start],
                    "to_frame": full["frame_index"][i - 1],
                    "start": round(times[start], 3),
                    "end": round(times[i - 1], 3),
                    "mean_advantage": round(sum(chunk) / len(chunk), 5),
                }
            )
            start = i
    step = max(1, round(full["fps"] or 1))
    return {
        "episode_index": episode,
        "revision_id": full["revision_id"],
        "threshold": full["threshold"],
        "frames": len(positive),
        "positive_fraction": sum(positive) / len(positive) if positive else 0.0,
        "runs": runs,
        "value_samples": [
            {"time": round(times[i], 3), "value": round(full["value"][i], 4)}
            for i in range(0, len(positive), step)
        ],
    }


# ---------------------------------------------------------------- threshold


def unified_threshold(
    repo_ids: list[str], positive_quantile: float | None = None
) -> dict[str, Any]:
    """RLinf's unified threshold over several datasets' current revisions:
    the (1 - positive_quantile) percentile of every frame's continuous
    advantage, all datasets together (``sft`` ones included — RLinf forces
    their labels positive only after the threshold is computed). Every
    revision must come from the same checkpoint weights, lookahead and
    return range."""
    if not repo_ids:
        raise RecapError(400, "Name at least one dataset")
    parts, rows, keys = [], [], set()
    for repo_id in repo_ids:
        ds = dataset(repo_id)
        rid = store.current_id(ds.name)
        record = store.revision(ds.name, rid) if rid else None
        if not record:
            raise RecapError(400, f"{repo_id} has no computed revision")
        table = pq.read_table(
            store.root(ds.name) / "revisions" / rid / "advantages.parquet",
            columns=["advantage_continuous"],
        )
        scores = table.column("advantage_continuous").to_numpy()
        parts.append(scores)
        keys.add(
            (
                record["checkpoint"].get("sha256"),
                record.get("lookahead"),
                record.get("return_min"),
                record.get("return_max"),
                record.get("gamma"),
            )
        )
        rows.append(
            {
                "repo_id": repo_id,
                "revision_id": rid,
                "dataset_type": record.get("dataset_type"),
                "frames": int(len(scores)),
                "positive_quantile": record.get("positive_quantile"),
            }
        )
    if len(keys) != 1:
        raise RecapError(
            400,
            "The revisions differ in checkpoint, lookahead, return range or gamma: "
            f"{sorted(map(str, keys))}",
        )
    quantile = (
        positive_quantile
        if positive_quantile is not None
        else rows[0]["positive_quantile"]
    )
    if quantile is None or not 0 < float(quantile) < 1:
        raise RecapError(400, "positive_quantile must lie strictly between 0 and 1")
    combined = np.concatenate(parts)
    threshold = advantage.quantile_threshold(combined, float(quantile))
    for row, scores in zip(rows, parts):
        row["positive_at_threshold"] = int(np.count_nonzero(scores >= threshold))
    return {
        "threshold": threshold,
        "positive_quantile": float(quantile),
        "frames": int(len(combined)),
        "positive_frames": int(np.count_nonzero(combined >= threshold)),
        "advantage_min": float(combined.min()),
        "advantage_max": float(combined.max()),
        "advantage_mean": float(combined.mean()),
        "checkpoint_sha256": next(iter(keys))[0],
        "datasets": rows,
    }
