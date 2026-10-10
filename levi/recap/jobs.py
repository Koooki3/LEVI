"""RECAP value jobs: plan, worker process, watch, collect and publish.

Modelled on the SAM3 run in backend/app.py: a plan JSON in a staging
folder, a worker process in its own session with its own log, a watch
thread that stops a worker running too long or making no progress, and a
collector that turns the worker's values into one published revision
exactly once. The ``fake`` provider runs the same worker module with LEVI's
own Python (no Torch); ``rlinf`` runs it in integrations/recap_value/.venv.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import shutil
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
from . import advantage, checkpoints, signature, store

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


# ---------------------------------------------------------------- dataset type

# What a run may ask for, and what it resolves to. "auto" (the default)
# decides from the dataset setting, the export's metadata or the outcomes and
# otherwise falls back to the old default "rollout"; "value_only" (values, no
# labels) is asked for or set, so unlabelled rollouts are never silently
# turned into all-positive demonstrations.
REQUESTED_TYPES = ("auto", "rollout", "sft", "value_only")
SETTING_TYPES = ("rollout", "sft", "value_only")
RESOLVED_TYPES = ("rollout", "sft", "value_only")
SETTINGS_SCHEMA = "levi.recap_value.dataset.v1"


def _settings_path(name: str) -> Path:
    return store.root(name) / "dataset.json"


def dataset_setting(name: str) -> dict[str, Any] | None:
    """The person's dataset-level choice (``rollout`` / ``sft``), if any."""
    value = store.load_json(_settings_path(name))
    if isinstance(value, dict) and value.get("dataset_type") in SETTING_TYPES:
        return value
    return None


def _manifest_type(ds: Dataset) -> str | None:
    """The type a LEVI ``recap_value`` export wrote into its own metadata."""
    value = catalog.read(ds.root / "meta/levi_recap.json", {})
    kind = value.get("dataset_type") if isinstance(value, dict) else None
    return kind if kind in ("rollout", "sft") else None


def resolve_dataset_type(ds: Dataset, requested: str = "auto") -> dict[str, Any]:
    """``{"dataset_type", "source", "reason"}`` for a run.

    An explicit type is used as asked (``request``). ``auto`` takes, in
    order: the dataset setting (``user``), the export's own
    ``meta/levi_recap.json`` (``manifest``), ``rollout`` when any episode has
    an outcome (``outcomes``), else the old default ``rollout``
    (``fallback``; it refuses a dataset without outcomes as before)."""
    if requested not in REQUESTED_TYPES:
        raise RecapError(400, "dataset_type is auto, rollout, sft or value_only")
    if requested != "auto":
        return {
            "dataset_type": requested,
            "source": "request",
            "reason": "asked for in the request",
        }
    setting = dataset_setting(ds.name)
    if setting:
        return {
            "dataset_type": setting["dataset_type"],
            "source": "user",
            "reason": "the dataset setting (recap_values/<name>/dataset.json)",
        }
    kind = _manifest_type(ds)
    if kind:
        return {
            "dataset_type": kind,
            "source": "manifest",
            "reason": "the export's meta/levi_recap.json",
        }
    if any(ds.outcome(ep) is not None for ep in ds.rows):
        return {
            "dataset_type": "rollout",
            "source": "outcomes",
            "reason": "episodes have success/failure labels",
        }
    return {
        "dataset_type": "rollout",
        "source": "fallback",
        "reason": "no dataset setting, export metadata or outcome label: "
        "fell back to rollout (the old default)",
    }


def dataset_type_payload(repo_id: str) -> dict[str, Any]:
    """The setting and what ``auto`` resolves to now (read-only)."""
    ds = dataset(repo_id)
    setting = dataset_setting(ds.name)
    return {
        "setting": setting["dataset_type"] if setting else "auto",
        **resolve_dataset_type(ds, "auto"),
    }


def set_dataset_type(repo_id: str, dataset_type: str) -> dict[str, Any]:
    """Store the dataset-level type; ``auto`` removes the setting."""
    ds = dataset(repo_id)
    if dataset_type not in REQUESTED_TYPES:
        raise RecapError(400, "dataset_type is auto, rollout, sft or value_only")
    with store.locked(ds.name):
        path = _settings_path(ds.name)
        if dataset_type == "auto":
            path.unlink(missing_ok=True)
        else:
            store.write_json(
                path,
                {
                    "schema": SETTINGS_SCHEMA,
                    "dataset_type": dataset_type,
                    "source": "user",
                    "updated_at": time.time(),
                },
            )
    return dataset_type_payload(repo_id)


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


def _static_filter(
    ds: Dataset,
    manifest: checkpoints.Manifest,
    mode: str,
    lengths: dict[int, int],
) -> tuple[dict[str, Any], dict[int, list[int]]]:
    """Which frames of each episode the training filter keeps.

    Returns the run's filter record and ``{episode: kept positions}`` (empty
    when the filter is not applied). Only a raw-capture view converted one
    row per captured frame (``timing: retime``, no static filter of its own)
    can be filtered: the decision reads the capture's pose and gripper CSVs,
    exactly like the training pipeline."""
    if mode not in ("auto", "on", "off"):
        raise RecapError(400, "static_filter is auto, on or off")
    params = manifest.static_filter.model_dump() if manifest.static_filter else None
    record: dict[str, Any] = {"mode": mode, "applied": False, "params": params}
    if params is None:
        if mode == "on":
            raise RecapError(
                400,
                f"Checkpoint {manifest.name} names no training static filter "
                "(manifest static_filter)",
            )
        record["reason"] = "the checkpoint's training data was not static-filtered"
        return record, {}
    if mode == "off":
        record["reason"] = "turned off for this run"
        return record, {}
    view = catalog.read(ds.root / "meta/levi_view.json", {})
    conversion = catalog.read(ds.root / "meta/levi_conversion.json", {})
    options = conversion.get("options") or {}
    source_root = view.get("source_root")
    problem = None
    if not source_root or view.get("input_format") != "robot_capture":
        problem = (
            "the dataset is not a raw robot-capture view (a LeRobot dataset "
            "is taken as already filtered like the training data)"
        )
    elif options.get("timing") != "retime" or options.get("filter_static"):
        problem = (
            "the view was not converted one row per captured frame "
            "(timing retime, no static filter of its own)"
        )
    elif not Path(source_root).is_dir():
        problem = f"the raw capture {source_root} is missing"
    if problem:
        if mode == "on":
            raise RecapError(400, f"Cannot apply the static filter: {problem}")
        record["reason"] = problem
        return record, {}
    from . import static_filter as sf

    keeps: dict[int, list[int]] = {}
    skipped: dict[int, str] = {}
    kept_total = frames_total = 0
    for ep, length in lengths.items():
        demo = ds.rows[ep].get("source_demo")
        if not demo:
            skipped[ep] = "static filter: no source_demo for this episode"
            continue
        decision = sf.kept_positions(Path(source_root) / str(demo), params)
        if decision["frames"] != length:
            skipped[ep] = (
                f"static filter: {decision['frames']} capture rows vs {length} "
                "view frames"
            )
            continue
        if decision.get("skipped"):
            skipped[ep] = f"static filter: {decision['skipped']} (training drops it)"
            continue
        keeps[ep] = decision["keep"]
        kept_total += len(decision["keep"])
        frames_total += length
    record.update(
        applied=True,
        rule=sf.RULE,
        source=sf.SOURCE,
        frames=frames_total,
        kept_frames=kept_total,
        dropped_fraction=(1 - kept_total / frames_total) if frames_total else 0.0,
    )
    if skipped:
        record["skipped"] = skipped
    return record, keeps


def start(
    repo_id: str,
    checkpoint: str,
    *,
    episodes: list[int] | None = None,
    lookahead: int | None = None,
    positive_quantile: float | None = None,
    threshold: float | None = None,
    dataset_type: str = "auto",
    static_filter: str = "auto",
    watch: bool = True,
) -> dict[str, Any]:
    """Validate, write the plan and start the worker; returns the job.

    ``static_filter`` (auto / on / off): when the checkpoint's training data
    was static-filtered, keep only the frames that filter keeps (values,
    returns and advantages over the kept sequence; dropped frames stay
    unlabelled). ``auto`` applies it to raw-capture views and leaves other
    datasets as they are."""
    ds = dataset(repo_id)
    try:
        folder, manifest = checkpoints.load(checkpoint)
    except KeyError as exc:
        raise RecapError(400, str(exc.args[0])) from exc
    except ValueError as exc:
        raise RecapError(400, str(exc)) from exc
    if manifest.provider == "rlinf":
        _refuse_rlinf(folder, manifest, ds)
    kind = resolve_dataset_type(ds, dataset_type)
    resolved = kind["dataset_type"]
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
    success: dict[int, bool | None] = {}
    skipped: dict[str, str] = {}
    for ep in chosen:
        if resolved == "sft":
            success[ep] = True
            continue
        if resolved == "value_only":
            # No outcome anywhere: V(o_t) does not read it, labels need it.
            success[ep] = None
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
            "No episode has a success/failure label; label outcomes first, run "
            "with dataset_type value_only (values, no labels) or sft "
            "(demonstrations), or set the dataset's type"
            + (
                f" (dataset_type auto {kind['reason']})"
                if kind["source"] == "fallback"
                else ""
            ),
        )
    lengths = {}
    for ep in success:
        length = ds.rows[ep].get("length")
        if not isinstance(length, int) or length < 1:
            raise RecapError(400, f"Episode {ep} has no length in meta/episodes.jsonl")
        if not ds.data_path(ep).is_file():
            raise RecapError(400, f"Episode {ep} has no data parquet")
        lengths[ep] = length
    filtering, keeps = _static_filter(ds, manifest, static_filter, lengths)
    for ep, reason in filtering.pop("skipped", {}).items():
        skipped[str(ep)] = reason
        success.pop(ep, None)
        lengths.pop(ep, None)
        keeps.pop(ep, None)
    if not success:
        raise RecapError(400, "No episode is left to label after the static filter")
    request = {
        "episodes": episodes,
        "lookahead": lookahead if lookahead is not None else manifest.lookahead,
        "positive_quantile": (
            positive_quantile
            if positive_quantile is not None
            else manifest.positive_quantile
        ),
        "threshold": threshold,
        "dataset_type": resolved,
        "dataset_type_requested": dataset_type,
        "dataset_type_source": kind["source"],
        "dataset_type_reason": kind["reason"],
        "static_filter": static_filter,
    }
    if request["lookahead"] < 1:
        raise RecapError(400, "lookahead must be at least 1")
    if not 0 < request["positive_quantile"] < 1:
        raise RecapError(400, "positive_quantile must lie strictly between 0 and 1")
    try:
        writing = store.layout()
    except ValueError as exc:
        raise RecapError(400, str(exc)) from exc
    with _LOCK, store.locked(ds.name):
        running = active(ds.name)
        if running is not None:
            raise RecapError(
                409,
                f"A RECAP value job ({running['id']}) is already running for this dataset",
            )
        if writing == store.MODELS and episodes is not None:
            _refuse_unmergeable(ds, manifest, request, filtering, folder)
        base = store.root(ds.name)
        job_id = naming.timestamp_id(_jobs_dir(ds.name), ".json")
        plan_path = base / "plans" / f"{job_id}.json"
        result_path = base / "results" / f"{job_id}.json"
        log_path = _jobs_dir(ds.name) / f"{job_id}.log"
        progress_path = _jobs_dir(ds.name) / f"{job_id}.progress.json"
        total = int(
            sum(len(keeps[ep]) if ep in keeps else n for ep, n in lengths.items())
        )
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
                {
                    "episode_index": ep,
                    "length": lengths[ep],
                    "success": success[ep],
                    **({"keep": keeps[ep]} if ep in keeps else {}),
                }
                for ep in sorted(lengths)
            ],
            "static_filter": filtering,
            "batch_size": _batch_size(),
            "device": _device() if manifest.provider == "rlinf" else "cpu",
            # The storage layout is fixed when the job starts: a restart with
            # another LEVI_RECAP_STORE_LAYOUT must not move its result.
            "layout": writing,
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
            "success": {str(k): v for k, v in success.items() if v is not None},
            "skipped_episodes": skipped,
            "static_filter": filtering,
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
                    version=revision.get("version"),
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
    # Record the cancellation before stopping the worker: the watch thread
    # collects as soon as the process exits, and would otherwise report the
    # killed worker as "exited without a result" (failed) first.
    with store.locked(job["name"]):
        current = _read(job["name"], job["id"]) or job
        if current.get("status") in ACTIVE:
            current.update(status="cancelled", finished_at=time.time())
            _save(current)
        job = current
    process = _PROCESSES.get(_key(job))
    stopping = job.get("status") == "cancelled" and process is not None
    if stopping and process.poll() is None:
        _stop(process, grace=5.0)
    _forget(_key(job))
    return job


# ---------------------------------------------------------------- merging


def _refuse_unmergeable(
    ds: Dataset,
    manifest: checkpoints.Manifest,
    request: dict[str, Any],
    filtering: dict[str, Any],
    folder: Path | None = None,
) -> None:
    """Before a subset run starts (and spends GPU time), refuse it when its
    episodes could not be merged into the model's stored result. The meta
    this run will publish is assembled from what is known now and compared
    with the same ``signature`` the publication checks; only the worker's
    software versions are left to the publication (they are known once the
    worker reports them). A threshold or return range that would come from
    this subset's own statistics never matches."""
    if not store.head(ds.name, manifest.name):
        return
    old = store.revision(ds.name, manifest.name)
    if not old or old.get("layout") != store.MODELS:
        return
    value_only = request["dataset_type"] == "value_only"
    if value_only:
        threshold = source = ret_min = ret_max = range_source = None
    else:
        if request["threshold"] is not None:
            threshold, source = float(request["threshold"]), "manual"
        elif manifest.unified_threshold is not None:
            threshold, source = float(manifest.unified_threshold), "checkpoint"
        else:
            threshold = "a quantile of this subset's advantages"
            source = "dataset_quantile"
        if manifest.return_min is not None and manifest.return_max is not None:
            ret_min, ret_max = float(manifest.return_min), float(manifest.return_max)
            range_source = "checkpoint"
        else:
            ret_min = ret_max = "this subset's returns"
            range_source = "dataset"
    fingerprint = ds.fingerprint()
    expected = {
        "provider": manifest.provider,
        "checkpoint": {
            "name": manifest.name,
            "sha256": manifest.sha256,
            "manifest": manifest.public(),
        },
        "dataset_type": request["dataset_type"],
        "labels": not value_only,
        "fingerprint": {
            "source_fingerprint": fingerprint.get("source_fingerprint"),
            "dataset_revision": fingerprint.get("dataset_revision"),
        },
        "static_filter": {k: v for k, v in filtering.items() if k != "skipped"},
        "base_models": checkpoints.base_models_status(folder, manifest)
        if manifest.provider == "rlinf" and folder is not None
        else None,
        "fps": float(ds.fps),
        "compute_version": advantage.RECAP_COMPUTE_VERSION,
        **_run_parameters(manifest, request, threshold, source),
        "return_min": ret_min,
        "return_max": ret_max,
        "return_range_source": range_source,
    }
    differs = signature.differences(old, expected, worker=False)
    if differs:
        raise RecapError(
            409,
            f"The stored {manifest.name} result on this dataset was computed with "
            f"different settings ({', '.join(differs)}); recompute the whole "
            "dataset instead of a subset",
        )


def _run_parameters(manifest, request, threshold, source) -> dict[str, Any]:
    """The run parameters a result records (shared by the publication and
    the subset pre-check, so both see the same values)."""
    return {
        "threshold": threshold,
        "threshold_source": source,
        "positive_quantile": request["positive_quantile"],
        "lookahead": int(request["lookahead"]),
        "gamma": float(manifest.gamma),
        "failure_reward": float(manifest.failure_reward),
    }


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
    # Values only: no outcomes, so no returns, advantages or labels.
    value_only = request["dataset_type"] == "value_only"
    lookahead = int(request["lookahead"])
    gamma = float(manifest.gamma)
    total = int(sum(len(e.get("keep", ())) or e["length"] for e in plan["episodes"]))
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
        timestamps = data.column("timestamp").to_numpy().astype(np.float64)
        if len(expected) != n:
            raise ValueError(
                f"episode {ep}: the dataset has {len(expected)} rows, not {n}"
            )
        if "keep" in item:
            # Static-filtered: the kept sequence is the episode.
            keep = np.asarray(item["keep"], dtype=np.int64)
            expected, timestamps = expected[keep], timestamps[keep]
        if len(frames) != len(expected) or not np.array_equal(frames, expected):
            raise ValueError(
                f"episode {ep}: {len(frames)} values for {len(expected)} frames, "
                "or frame indices that differ from the dataset"
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
        if value_only:
            blank = np.full(len(frames), np.nan)
            per_episode[ep] = {
                "frame_index": frames,
                "timestamp": timestamps,
                "value": values,
                "value_next": blank,
                "reward_sum": blank,
                "reward_sum_raw": blank,
                "return": blank,
                "advantage": blank,
                "num_valid_rewards": np.zeros(len(frames), dtype=np.int64),
                "positive": None,
                "episode_frames": n,
            }
            continue
        returns, rewards = advantage.episode_rewards(
            len(frames), bool(item["success"]), gamma, float(manifest.failure_reward)
        )
        per_episode[ep] = {
            "frame_index": frames,
            "timestamp": timestamps,
            "value": values,
            "return": returns,
            "reward": rewards,
            "episode_frames": n,
        }
    if value_only:
        return _publish_values_only(job, plan, manifest, result, per_episode)
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
    meta = {
        **_common_meta(job, plan, manifest, result),
        **_run_parameters(manifest, request, threshold, source),
        "return_min": float(ret_min),
        "return_max": float(ret_max),
        "return_range_source": range_source,
        "outcomes": {
            str(k): ("success" if v else "failure") for k, v in job["success"].items()
        },
        "threshold_provenance": (
            manifest.provenance.get("unified_threshold")
            if source == "checkpoint"
            else None
        ),
    }
    return store.publish(
        job["name"],
        per_episode,
        meta,
        dataset_name=plan["dataset"]["name"],
        subset=request["episodes"] is not None,
        layout=plan.get("layout"),
    )


def _publish_values_only(job, plan, manifest, result, per_episode):
    """Publish V(o_t) alone: no threshold, return range or labels."""
    request = job["request"]
    _set_stage(job, "saving", 0, sum(len(c["value"]) for c in per_episode.values()))
    meta = {
        **_common_meta(job, plan, manifest, result),
        **_run_parameters(manifest, request, None, None),
        "return_min": None,
        "return_max": None,
        "return_range_source": None,
        "outcomes": {},
        "threshold_provenance": None,
        "labels": False,
    }
    return store.publish(
        job["name"],
        per_episode,
        meta,
        dataset_name=plan["dataset"]["name"],
        subset=request["episodes"] is not None,
        layout=plan.get("layout"),
    )


def _common_meta(job, plan, manifest, result) -> dict[str, Any]:
    request = job["request"]
    root = Path(plan["dataset"]["root"])
    ds_fingerprint = catalog.read(root / "meta/levi_view.json", {}).get(
        "source_fingerprint"
    )
    return {
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
        "dataset_type_source": request.get("dataset_type_source", "request"),
        "dataset_type_reason": request.get("dataset_type_reason"),
        "labels": True,
        "fingerprint": {
            "source_fingerprint": ds_fingerprint,
            "dataset_revision": dataset_revision(root),
        },
        "skipped_episodes": job.get("skipped_episodes", {}),
        "static_filter": {
            k: v for k, v in (plan.get("static_filter") or {}).items() if k != "skipped"
        },
        "worker": result.get("provenance", {}),
        "base_models": plan["checkpoint"].get("base_models"),
        "dev_only_base_models": plan["checkpoint"].get("dev_only_base_models", []),
        "fps": float(plan["dataset"]["fps"]),
        "compute_version": advantage.RECAP_COMPUTE_VERSION,
        "levi_commit": _levi_commit(),
    }


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
    if record.get("dataset_type") not in ("sft", "value_only"):
        for ep, outcome in (record.get("outcomes") or {}).items():
            if ds.outcome(int(ep)) != outcome:
                reasons.append(f"episode {ep}'s outcome label changed")
                break
    return reasons


def has_labels(record: dict[str, Any]) -> bool:
    """False for a value-only result (no advantages, thresholds or labels)."""
    return record.get("dataset_type") != "value_only"


def _floats(values) -> list[float | None]:
    """JSON-safe: a value-only result stores NaN advantages."""
    return [None if v is None or math.isnan(v) else float(v) for v in values]


def current(ds: Dataset) -> dict[str, Any] | None:
    record = store.revision(ds.name)
    if not record:
        return None
    reasons = stale_reasons(ds, record)
    return {
        "revision_id": record["revision_id"],
        "model": record.get("model"),
        "version": record["version"],
        "layout": record["layout"],
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
        "dataset_type": record.get("dataset_type", "rollout"),
        "labels": has_labels(record),
        "stale": bool(reasons),
        # Frames labelled / frames in the labelled episodes when the
        # training static filter was applied (else null).
        "static_filter": _filter_brief(record),
        "dev_only_base_models": record.get("dev_only_base_models") or [],
    }


def _filter_brief(record: dict[str, Any]) -> dict[str, Any] | None:
    info = record.get("static_filter") or {}
    if not info.get("applied"):
        return None
    return {
        "rule": info.get("rule"),
        "kept_frames": info.get("kept_frames"),
        "frames": info.get("frames"),
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
    setting = dataset_setting(ds.name)
    return {
        "checkpoints": checkpoints.listing(),
        "worker": worker_state(),
        "current": current(ds),
        "job": public(job) if job else None,
        # What "auto" resolves to now, and the person's setting.
        "dataset_type": {
            "setting": setting["dataset_type"] if setting else "auto",
            **resolve_dataset_type(ds, "auto"),
        },
    }


def _published(
    name: str, revision_id: str | None, version: str | None = None
) -> dict[str, Any]:
    """The result a read asks for: ``revision_id`` (a value model's name or
    an original-layout revision id), else the current one. ``version`` pins
    the version the reader saw: a result recomputed since answers 409."""
    if revision_id is not None and not (
        naming.is_timestamp_id(revision_id) or store.is_model_name(revision_id)
    ):
        raise RecapError(400, f"{revision_id!r} is not a revision id or model name")
    if version is not None and not naming.is_timestamp_id(version):
        raise RecapError(400, f"{version!r} is not a result version")
    record = store.revision(name, revision_id)
    if record and version is not None and record["version"] != version:
        raise RecapError(
            409,
            f"The {record['revision_id']} result was recomputed (version "
            f"{record['version']}, not {version}); reload it",
        )
    if not record:
        raise RecapError(
            404,
            "No advantage labels for this dataset yet"
            if revision_id is None
            else f"No revision {revision_id} for this dataset",
        )
    return record


def episode_payload(
    repo_id: str,
    episode: int,
    revision_id: str | None = None,
    version: str | None = None,
) -> dict[str, Any]:
    ds = dataset(repo_id)
    record = _published(ds.name, revision_id, version)
    # Read inside the version just resolved: a recomputation switching the
    # head meanwhile leaves this version on disk for the grace period.
    table = store.read_episode(
        ds.name, episode, record["revision_id"], record["version"]
    )
    if table is None:
        raise RecapError(404, f"No advantage labels for episode {episode}")
    columns = table.to_pydict()
    return {
        "episode_index": episode,
        "revision_id": record["revision_id"],
        "model": record.get("model"),
        "version": record["version"],
        "computed_at": record.get("created_at"),
        "fps": float(record.get("fps") or ds.fps),
        "threshold": record["threshold"],
        "frame_index": columns["frame_index"],
        "timestamp": columns["timestamp"],
        "value": [float(v) for v in columns["value"]],
        # Empty for a value-only result (no outcome, so no advantages or
        # labels): a viewer shows "no advantage labels", never a row of nulls
        # drawn as negative.
        "advantage": _floats(columns["advantage"]) if has_labels(record) else [],
        "positive": columns["positive"] if has_labels(record) else [],
        "labels": has_labels(record),
        # With the training static filter only kept frames are listed: the
        # frame indices skip over the dropped (unlabelled) ones.
        "static_filter": bool((record.get("static_filter") or {}).get("applied")),
        "episode_frames": ds.rows.get(episode, {}).get("length"),
    }


def summary_payload(
    repo_id: str, revision_id: str | None = None, version: str | None = None
) -> dict[str, Any]:
    ds = dataset(repo_id)
    record = _published(ds.name, revision_id, version)
    value = store.summary(ds.name, record["revision_id"], record["version"])
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
    if not full["labels"]:
        positive = []  # values only: no runs of labels
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
        "frames": len(times),
        "labels": full["labels"],
        "positive_fraction": (sum(positive) / len(positive) if positive else 0.0)
        if full["labels"]
        else None,
        "runs": runs,
        "value_samples": [
            {"time": round(times[i], 3), "value": round(full["value"][i], 4)}
            for i in range(0, len(times), step)
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
        record = store.revision(ds.name)
        rid = record["revision_id"] if record else None
        if not record:
            raise RecapError(400, f"{repo_id} has no computed revision")
        if not has_labels(record):
            raise RecapError(
                400,
                f"{repo_id} has values only (no outcomes, so no advantages); "
                "set its dataset type or label outcomes and recompute",
            )
        table = pq.read_table(
            store.advantages_path(ds.name, rid, record["version"]),
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
                "version": record["version"],
                "dataset_type": record.get("dataset_type"),
                "frames": len(scores),
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
        "frames": len(combined),
        "positive_frames": int(np.count_nonzero(combined >= threshold)),
        "advantage_min": float(combined.min()),
        "advantage_max": float(combined.max()),
        "advantage_mean": float(combined.mean()),
        "checkpoint_sha256": next(iter(keys))[0],
        "datasets": rows,
    }


# ---------------------------------------------------------------- clearing


def _running(name: str) -> list[str]:
    """Jobs of a dataset whose worker is alive (read-only: never publishes)."""
    return [
        job["id"] for job in jobs(name) if job.get("status") in ACTIVE and _alive(job)
    ]


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            pass
    return total


def _clear_items(name: str, include_jobs: bool) -> list[dict[str, Any]]:
    folder = store.root(name)
    items: list[dict[str, Any]] = []
    for model in sorted(p.name for p in (folder / store.MODELS).glob("*")):
        path = folder / store.MODELS / model
        record = store.revision(name, model) if store.head(name, model) else None
        items.append(
            {
                "kind": "model_result",
                "ref": model,
                "version": record["version"] if record else None,
                "episodes": record.get("episodes") if record else None,
                "path": path,
            }
        )
    legacy = folder / store.LEGACY
    for rid in sorted(p.name for p in legacy.glob("*")) if legacy.is_dir() else []:
        record = store.revision(name, rid) if naming.is_timestamp_id(rid) else None
        items.append(
            {
                "kind": "revision",
                "ref": rid,
                "version": rid if record else None,
                "episodes": record.get("episodes") if record else None,
                "path": legacy / rid,
            }
        )
    if (folder / "current.json").is_file():
        items.append({"kind": "current", "ref": None, "path": folder / "current.json"})
    if include_jobs:
        for sub in ("jobs", "plans", "results"):
            if (folder / sub).is_dir():
                items.append({"kind": sub, "ref": None, "path": folder / sub})
    for item in items:
        item["bytes"] = _size(item["path"])
    return items


def _symlinks(name: str, items: list[dict[str, Any]]) -> list[str]:
    """Symbolic links on the way to anything ``clear`` would remove (the
    dataset folder itself, ``models``/``revisions``, an item): deleting
    through one would reach data outside ``recap_values``."""
    folder = store.root(name)
    found: list[str] = []
    if folder.is_symlink():
        return ["."]
    for item in items:
        path = item["path"]
        relative = path.relative_to(folder)
        step = folder
        for part in relative.parts:
            step = step / part
            label = str(step.relative_to(folder))
            if step.is_symlink() and label not in found:
                found.append(label)
                break
    return found


def clear(
    names: list[str] | None = None,
    *,
    include_jobs: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    """Remove RECAP results (both layouts) and ``current.json``; with
    ``include_jobs`` also the job records, plans and worker outputs.

    A dry run (the default) only lists what would go and its size, and
    writes nothing (no lock is taken). ``names`` are ``local/<name>`` ids or
    folder names under ``recap_values`` (results of a dataset no longer
    registered can be cleared too); ``None`` means every folder. Refused
    while a job of the dataset runs, and when a symbolic link lies on the
    way to anything it would remove. Checkpoints, datasets and the dataset
    setting (``dataset.json``) are never touched. Each removed item is
    appended to the folder's ``cleared.jsonl`` as soon as it is gone (a
    failure is logged too)."""
    base = catalog.STATE / "recap_values"
    if names is None:
        names = (
            sorted(p.name for p in base.iterdir() if p.is_dir() and not p.is_symlink())
            if base.is_dir()
            else []
        )
    resolved = []
    for value in names:
        if value.startswith("local/"):
            try:
                value = catalog.resolve_name(value)
            except ValueError as exc:
                raise RecapError(400, str(exc)) from exc
        try:
            folder = store.root(value)
        except ValueError as exc:
            raise RecapError(400, str(exc)) from exc
        if not folder.is_dir():
            raise RecapError(404, f"No RECAP results folder for {value}")
        resolved.append(value)
    busy = {name: ids for name in resolved if (ids := _running(name))}
    if busy:
        raise RecapError(
            409,
            "RECAP value jobs are running ("
            + "; ".join(f"{n}: {', '.join(i)}" for n, i in busy.items())
            + "); wait for them or cancel them first",
        )
    plans = {}
    for name in resolved:
        links = _symlinks(name, [])
        items = [] if links else _clear_items(name, include_jobs)
        plans[name] = (items, links or _symlinks(name, items))
    if apply:
        linked = {n: links for n, (_, links) in plans.items() if links}
        if linked:
            raise RecapError(
                409,
                "Symbolic links in the RECAP results folders ("
                + "; ".join(f"{n}: {', '.join(links)}" for n, links in linked.items())
                + "); nothing was removed — move the data back or remove the "
                "links by hand",
            )
    report: dict[str, Any] = {"applied": apply, "datasets": [], "bytes": 0}
    for name in resolved:
        items, links = plans[name]
        if apply:
            with store.locked(name):
                if _running(name):
                    raise RecapError(409, f"A RECAP value job started on {name}")
                items = _clear_items(name, include_jobs)
                links = _symlinks(name, items)
                if links:
                    raise RecapError(
                        409, f"Symbolic links appeared in {name}: {', '.join(links)}"
                    )
                _remove(name, items)
        size = sum(i["bytes"] for i in items)
        report["bytes"] += size
        report["datasets"].append(
            {
                "name": name,
                "bytes": size,
                "symlinks": links,
                "items": [
                    {
                        **{k: v for k, v in i.items() if k != "path"},
                        "path": str(i["path"].relative_to(base)),
                    }
                    for i in items
                ],
            }
        )
    return report


def _remove(name: str, items: list[dict[str, Any]]) -> None:
    """Delete one item after another, logging each as soon as it is gone."""
    folder = store.root(name)
    with (folder / "cleared.jsonl").open("a") as log:

        def note(entry: dict[str, Any]) -> None:
            log.write(json.dumps(entry, ensure_ascii=False) + "\n")
            log.flush()
            os.fsync(log.fileno())

        for item in items:
            path = item["path"]
            entry = {k: v for k, v in item.items() if k != "path"}
            entry["path"] = str(path.relative_to(folder))
            try:
                if path.is_symlink():
                    raise OSError(f"{entry['path']} became a symbolic link")
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
            except OSError as exc:
                note({**entry, "failed_at": time.time(), "error": str(exc)})
                raise RecapError(
                    500,
                    f"{name} was partly cleared: {entry['path']} failed ({exc}); "
                    "see cleared.jsonl",
                ) from exc
            note({**entry, "cleared_at": time.time()})
    for empty in (store.MODELS, store.LEGACY):
        with contextlib.suppress(OSError):
            (folder / empty).rmdir()
