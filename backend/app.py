# Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
# LEVI modifications: v2 annotations, workspace boundaries, durable sidecars, non-overwriting complete exports.
"""LeRobot dataset visualizer — annotation backend.

A small FastAPI service that lets the Next.js visualizer write the v3.1
language schema introduced in lerobot#3467 (PR1) and used by the steerable
annotation pipeline in lerobot#3471 (PR2). Specifically it owns:

- per-episode annotation state, persisted to ``meta/lerobot_annotations.json``
- snapping event-style atom timestamps to exact source-frame timestamps
  (the writer in lerobot#3471 enforces exact match)
- exporting the annotated dataset by rewriting ``data/chunk-*/file-*.parquet``
  with two new columns:
    * ``language_persistent`` — broadcast per-episode (subtask/plan/memory)
    * ``language_events``     — per-frame (interjection/vqa, plus speech
      tool-call atoms with style=None)
  and a dataset-level ``tools`` column carrying the JSON schema for ``say``.
- pushing the result back to the Hugging Face Hub.

The frontend can run without this backend (read-only browsing). Annotation
write paths only light up when ``NEXT_PUBLIC_ANNOTATE_BACKEND_URL`` points to
an instance of this service.

Run locally:

    cd backend && pip install -r requirements.txt
    uvicorn app:app --port 7861 --reload

Then in another terminal:

    NEXT_PUBLIC_ANNOTATE_BACKEND_URL=http://127.0.0.1:7861 bun run dev
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from huggingface_hub import HfApi, hf_hub_download, snapshot_download
from pydantic import BaseModel

from levi.annotations import ObjectAnnotation, ObjectEdit, Sam3Plan, SidecarStore
from levi.annotations.sam3_protocol import (
    fake_annotations,
    validate_annotations_for_plan,
)
from levi.auth import credential_scope, hub_token, token
from levi.catalog import atomic, local_root, read
from levi.paths import CACHE, EXPORTS, SAM3_CHECKPOINT_DIR, STATE, inside

logger = logging.getLogger("lerobot-annotate")
logging.basicConfig(level=logging.INFO)

CACHE_ROOT = CACHE
EXPORT_ROOT = EXPORTS

# The mirror contains the PyTorch checkpoint layout expected by the pinned
# official SAM3 adapter. Keep these values configurable for future model
# revisions, while making the supported default explicit for every workspace.
SAM3_MODEL_REPO = os.getenv("LEVI_SAM3_MODEL_REPO", "1038lab/sam3")
SAM3_MODEL_FILENAME = os.getenv("LEVI_SAM3_MODEL_FILENAME", "sam3.pt")
SAM3_MODEL_REVISION = os.getenv("LEVI_SAM3_MODEL_REVISION", "main")
SAM3_PROGRESS_FILENAME = "download-progress.json"
_SAM3_AUTH_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}

# --- Schema mirrors src/lerobot/datasets/language.py --------------------------

PERSISTENT_STYLES = {"task_aug", "subtask", "plan", "memory", "motion"}
EVENT_ONLY_STYLES = {"interjection", "vqa", "trace"}
KNOWN_STYLES = PERSISTENT_STYLES | EVENT_ONLY_STYLES
LANGUAGE_PERSISTENT = "language_persistent"
LANGUAGE_EVENTS = "language_events"

SAY_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "say",
        "description": "Speak a short utterance to the user via the TTS executor.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The verbatim text to speak.",
                },
            },
            "required": ["text"],
        },
    },
}


def column_for_style(style: str | None) -> str:
    if style is None:
        return LANGUAGE_EVENTS
    if style in PERSISTENT_STYLES:
        return LANGUAGE_PERSISTENT
    if style in EVENT_ONLY_STYLES:
        return LANGUAGE_EVENTS
    raise ValueError(f"Unknown language style: {style!r}")


# --- Pydantic models ----------------------------------------------------------


class DatasetRef(BaseModel):
    repo_id: str | None = None
    revision: str | None = None
    local_path: str | None = None


class LoadRequest(DatasetRef):
    pass


class LanguageAtom(BaseModel):
    role: str
    content: str | None = None
    style: str | None = None
    timestamp: float
    # ``observation.images.*`` feature key for view-dependent atoms
    # (vqa / trace). ``None`` for camera-agnostic atoms. Mirrors the
    # row-level ``camera`` field added in lerobot PR 3467.
    camera: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class EpisodeAtomsPayload(BaseModel):
    repo_id: str | None = None
    local_path: str | None = None
    episode_index: int
    atoms: list[LanguageAtom] = []


class Sam3PlanRequest(Sam3Plan):
    """A model-neutral SAM3 plan accepted by the annotation control plane."""


class Sam3RunRequest(Sam3PlanRequest):
    """Request used by the CPU fake provider and the optional real worker."""


class Sam3EditRequest(ObjectEdit):
    """A revision-checked human edit to an object sidecar."""


class Sam3PromptPresetRequest(BaseModel):
    """A named, reusable set of SAM3 text prompts, shared across datasets."""

    name: str
    prompts: list[str]


class ExportRequest(DatasetRef):
    output_dir: str | None = None
    copy_videos: bool = False


class PushToHubRequest(DatasetRef):
    hf_token: str
    push_in_place: bool = True
    new_repo_id: str | None = None
    private: bool = False
    commit_message: str = "Add language annotations"


@dataclass
class EpisodeAnnotations:
    atoms: list[dict[str, Any]] = field(default_factory=list)


# --- Per-dataset state cache --------------------------------------------------


@dataclass
class DatasetState:
    repo_id: str | None
    local_path: str | None
    revision: str | None
    credential_scope: str
    root: Path
    info: dict[str, Any]
    episodes_df: pd.DataFrame
    annotations: dict[int, EpisodeAnnotations] = field(default_factory=dict)
    frame_ts_cache: dict[int, list[float]] = field(default_factory=dict)

    @property
    def annotations_path(self) -> Path:
        return (
            STATE
            / "annotations"
            / (
                __import__("hashlib").sha256(str(self.root).encode()).hexdigest()
                + ".json"
            )
        )

    @property
    def object_annotations_path(self) -> Path:
        """Workspace sidecar root, independent from the source dataset tree."""
        identity_value = {
            "repo_id": self.repo_id,
            "revision": self.revision or "main",
            "local_path": self.local_path,
        }
        if self.repo_id:
            identity_value["credential_scope"] = self.credential_scope
        identity = json.dumps(identity_value, sort_keys=True, ensure_ascii=False)
        return (
            STATE / "object_annotations" / hashlib.sha256(identity.encode()).hexdigest()
        )


_states: dict[str, DatasetState] = {}


def _state_key(req: DatasetRef) -> str:
    if req.local_path:
        return f"local::{Path(req.local_path).expanduser().resolve()}"
    if req.repo_id:
        # Hub permissions affect the resolved dataset. Namespace the in-memory
        # state and sidecar by a one-way token digest so account changes cannot
        # reuse a previous account's private cache.
        return (
            f"hf::{req.repo_id}@{req.revision or 'main'}"
            f"::{credential_scope()}"
        )
    raise HTTPException(status_code=400, detail="need repo_id or local_path")


def _ensure_state(req: DatasetRef) -> DatasetState:
    if req.repo_id and req.repo_id.startswith("local/"):
        req = DatasetRef(local_path=str(local_root(req.repo_id)))
    if req.local_path:
        req.local_path = str(inside(req.local_path))
    key = _state_key(req)
    if key in _states:
        return _states[key]
    return _load_state(req, key)


def _sidecar(state: DatasetState) -> SidecarStore:
    identity = {
        "repo_id": state.repo_id,
        "revision": state.revision or "main",
        "local_path": state.local_path,
        "codebase_version": state.info.get("codebase_version"),
        "fps": state.info.get("fps"),
    }
    if state.repo_id:
        identity["credential_scope"] = state.credential_scope
    return SidecarStore(state.object_annotations_path, identity=identity)


def _validate_sam3_plan(state: DatasetState, request: Sam3PlanRequest) -> None:
    available_episodes = {
        int(value) for value in state.episodes_df["episode_index"].tolist()
    }
    missing = set(request.episode_indices) - available_episodes
    if missing:
        raise HTTPException(400, f"Unknown episode indices: {sorted(missing)}")
    feature_keys = set((state.info.get("features") or {}).keys())
    camera_keys = {key for key in feature_keys if key.startswith("observation.images.")}
    if camera_keys and not set(request.camera_keys).issubset(camera_keys):
        raise HTTPException(
            400, "camera_keys must reference observation.images.* features"
        )
    if request.accept_threshold < request.review_threshold:
        raise HTTPException(400, "accept_threshold must be >= review_threshold")


_SAM3_ENABLED_VALUES = {"1", "true", "yes", "on"}
_SAM3_PROCESSES: dict[str, subprocess.Popen[bytes]] = {}


def _sam3_enabled() -> bool:
    return os.environ.get("LEVI_SAM3_ENABLED", "1").lower() in _SAM3_ENABLED_VALUES


def _public_sam3_plan(payload: dict[str, Any]) -> dict[str, Any]:
    """Return plan metadata without exposing managed filesystem paths."""
    plan_keys = (
        "repo_id",
        "revision",
        "episode_indices",
        "camera_keys",
        "prompts",
        "start_frame",
        "max_frames",
        "review_threshold",
        "accept_threshold",
        "provider",
    )
    dataset = payload.get("dataset") or {}
    return {
        "plan_id": payload.get("plan_id"),
        "status": payload.get("status"),
        "dataset": {
            "repo_id": dataset.get("repo_id"),
            "revision": dataset.get("revision"),
        },
        **{key: payload[key] for key in plan_keys if key in payload},
    }


def _public_sam3_job(job: dict[str, Any]) -> dict[str, Any]:
    """Return job state without leaking worker paths, PIDs or command details."""
    public_keys = (
        "job_id",
        "status",
        "provider",
        "plan_id",
        "created_at",
        "started_at",
        "finished_at",
        "revision_id",
        "annotation_count",
        "error",
        "error_detail",
        "progress",
        "item_errors",
    )
    return {key: job[key] for key in public_keys if key in job}


def _sam3_plan_payload(
    state: DatasetState, request: Sam3PlanRequest
) -> tuple[SidecarStore, Path, dict[str, Any]]:
    store = _sidecar(state)
    store.initialize()
    plan_id = uuid.uuid4().hex
    plan_path = store.root / "staging" / "plans" / f"{plan_id}.json"
    payload = {
        "plan_id": plan_id,
        "status": "planned",
        "dataset": {
            "repo_id": state.repo_id,
            "local_path": state.local_path,
            "revision": state.revision or "main",
        },
        "dataset_root": str(state.root),
        **request.model_dump(),
    }
    atomic(plan_path, payload)
    return store, plan_path, payload


def _sam3_job_path(store: SidecarStore, job_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{16,64}", job_id):
        raise HTTPException(400, "Invalid SAM3 job ID")
    return store.root / "staging" / "jobs" / f"{job_id}.json"


_SAM3_LOG_TAIL_BYTES = 4000


def _sam3_log_tail(job: dict[str, Any]) -> str | None:
    """Read the last lines of a crashed worker's stdout/stderr log.

    The worker never prints credentials (progress messages already redact
    Hub URLs), but we redact defensively since this text reaches the browser.
    """
    log_path = job.get("log_path")
    if not log_path:
        return None
    try:
        data = Path(str(log_path)).read_bytes()
    except OSError:
        return None
    if not data:
        return None
    tail = data[-_SAM3_LOG_TAIL_BYTES:].decode("utf-8", errors="replace")
    if len(data) > _SAM3_LOG_TAIL_BYTES:
        tail = "...(truncated)...\n" + tail
    return re.sub(
        r"(?i)(token|authorization|x-amz-signature|x-amz-credential)=[^&\s]+",
        r"\1=<redacted>",
        tail,
    ).strip()


def _read_sam3_batch_progress(job: dict[str, Any]) -> dict[str, Any] | None:
    """Read the worker's episode/camera batch progress while a job runs."""
    progress_path = job.get("progress_path")
    if not progress_path:
        return None
    try:
        return json.loads(Path(str(progress_path)).read_text())
    except (OSError, ValueError):
        return None


def _sam3_process_alive(job: dict[str, Any]) -> bool:
    process = _SAM3_PROCESSES.get(str(job.get("job_id")))
    if process is not None:
        return process.poll() is None
    pid = job.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _forget_sam3_process(job_id: str) -> None:
    process = _SAM3_PROCESSES.pop(job_id, None)
    if process is not None:
        # poll() reaps an already finished child without blocking.
        process.poll()


def _collect_sam3_job(state: DatasetState, job: dict[str, Any]) -> dict[str, Any]:
    """Reconcile a worker result into a sidecar revision exactly once."""
    if job.get("status") in {"succeeded", "failed", "cancelled"}:
        _forget_sam3_process(str(job.get("job_id", "")))
        return job
    result_path = Path(str(job["result_path"]))
    process = _SAM3_PROCESSES.get(str(job.get("job_id")))
    return_code = process.poll() if process is not None else None
    if result_path.is_file():
        try:
            result = json.loads(result_path.read_text())
            if result.get("status") != "succeeded":
                raise ValueError(
                    result.get("error") or "worker returned an unsuccessful result"
                )
            annotations = [
                ObjectAnnotation.model_validate(item)
                for item in result.get("annotations", [])
            ]
            plan_payload = json.loads(Path(str(job["plan_path"])).read_text())
            plan_fields = set(Sam3Plan.model_fields)
            plan = Sam3Plan.model_validate(
                {key: plan_payload[key] for key in plan_fields if key in plan_payload}
            )
            validate_annotations_for_plan(plan, annotations)
            sidecar = _sidecar(state)
            revision = sidecar.publish(
                annotations,
                parent_revision=sidecar.current_revision(),
                model=result.get("model") or {"provider": "sam3"},
            )
            job.update(
                status="succeeded",
                finished_at=pd.Timestamp.utcnow().isoformat(),
                revision_id=revision["revision_id"],
                annotation_count=len(annotations),
                item_errors=result.get("item_errors", []),
            )
        except (OSError, ValueError, TypeError) as exc:
            job.update(status="failed", error=f"Invalid SAM3 worker result: {exc}")
    elif return_code is not None or not _sam3_process_alive(job):
        detail = _sam3_log_tail(job)
        job.update(
            status="failed",
            error=f"SAM3 worker exited without a result (code {return_code})",
            error_detail=detail,
        )
    else:
        job["status"] = "running"
        progress = _read_sam3_batch_progress(job)
        if progress is not None:
            job["progress"] = progress
    atomic(_sam3_job_path(_sidecar(state), str(job["job_id"])), job)
    return job


def _load_state(req: DatasetRef, key: str) -> DatasetState:
    if req.local_path:
        root = Path(req.local_path).expanduser().resolve()
        if not root.exists():
            raise HTTPException(
                status_code=404, detail=f"Dataset path not found: {root}"
            )
    elif req.repo_id:
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", req.repo_id):
            raise HTTPException(400, "Invalid Hub dataset ID")
        revision_key = (
            __import__("hashlib").sha256(req.revision.encode()).hexdigest()[:12]
            if req.revision
            else "main"
        )
        slug = (
            req.repo_id.replace("/", "__")
            + "@"
            + revision_key
            + "--"
            + credential_scope()
        )
        root = inside(CACHE_ROOT / slug)
        root.mkdir(parents=True, exist_ok=True)
        snapshot_download(
            req.repo_id,
            repo_type="dataset",
            token=token(),
            revision=req.revision,
            local_dir=root,
            # v3 stores episode metadata in nested meta/episodes shards.
            allow_patterns=["meta/**"],
        )
    else:
        raise HTTPException(status_code=400, detail="need repo_id or local_path")

    info_path = inside("meta/info.json", root)
    if not info_path.exists():
        raise HTTPException(status_code=404, detail=f"Missing meta/info.json at {root}")
    info = json.loads(info_path.read_text())

    episodes_root = root / "meta" / "episodes"
    if str(info.get("codebase_version", "")).startswith("v2."):
        metadata = root / "meta/episodes.jsonl"
        if metadata.exists():
            episodes_df = pd.read_json(metadata, lines=True)
        else:
            episodes_df = pd.DataFrame(
                {"episode_index": range(int(info["total_episodes"]))}
            )
    else:
        files = sorted(episodes_root.rglob("*.parquet"))
        if not files:
            raise HTTPException(
                status_code=404, detail="No episodes parquet files found"
            )
        episodes_df = (
            pd.concat([pd.read_parquet(p) for p in files], ignore_index=True)
            .sort_values("episode_index")
            .reset_index(drop=True)
        )

    state = DatasetState(
        repo_id=req.repo_id,
        local_path=str(root) if req.local_path else None,
        revision=req.revision,
        credential_scope=credential_scope() if req.repo_id else "local",
        root=root,
        info=info,
        episodes_df=episodes_df,
    )
    _load_existing_annotations(state)
    _states[key] = state
    return state


def _load_existing_annotations(state: DatasetState) -> None:
    path = state.annotations_path
    if not path.exists():
        return
    data = json.loads(path.read_text())
    for ep_str, payload in data.get("episodes", {}).items():
        ep_idx = int(ep_str)
        atoms = payload.get("atoms")
        if atoms is None:
            # v1 format from older lerobot-annotate (legacy)
            atoms = []
            for seg in payload.get("subtasks", []):
                if "label" in seg and "start" in seg:
                    atoms.append(
                        {
                            "role": "assistant",
                            "content": str(seg["label"]),
                            "style": "subtask",
                            "timestamp": float(seg["start"]),
                            "tool_calls": None,
                        }
                    )
            for seg in payload.get("high_levels", []):
                ts = float(seg.get("start", 0.0))
                if seg.get("user_prompt"):
                    atoms.append(
                        {
                            "role": "user",
                            "content": str(seg["user_prompt"]),
                            "style": "interjection",
                            "timestamp": ts,
                            "tool_calls": None,
                        }
                    )
                if seg.get("robot_utterance"):
                    atoms.append(
                        {
                            "role": "assistant",
                            "content": None,
                            "style": None,
                            "timestamp": ts,
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": "say",
                                        "arguments": {
                                            "text": str(seg["robot_utterance"])
                                        },
                                    },
                                }
                            ],
                        }
                    )
        state.annotations[ep_idx] = EpisodeAnnotations(atoms=[dict(a) for a in atoms])


def _save_annotations(state: DatasetState) -> None:
    path = state.annotations_path
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 2,
        "schema": {
            "persistent_styles": sorted(PERSISTENT_STYLES),
            "event_styles": sorted(EVENT_ONLY_STYLES),
        },
        "episodes": {
            str(ep): {"atoms": ann.atoms} for ep, ann in state.annotations.items()
        },
    }
    atomic(path, payload)


# --- Frame-timestamp helpers --------------------------------------------------


def _episode_data_path(state: DatasetState, episode_index: int) -> Path | None:
    rows = state.episodes_df[state.episodes_df["episode_index"] == episode_index]
    if rows.empty:
        return None
    row = rows.iloc[0]
    if str(state.info.get("codebase_version", "")).startswith("v2."):
        rel = state.info["data_path"].format(
            episode_index=episode_index,
            episode_chunk=episode_index // int(state.info.get("chunks_size", 1000)),
        )
    else:
        chunk_col, file_col = "data/chunk_index", "data/file_index"
        if chunk_col not in row or file_col not in row:
            return None
        rel = (
            state.info.get("data_path")
            or "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet"
        ).format(chunk_index=int(row[chunk_col]), file_index=int(row[file_col]))
    full = inside(rel, state.root)
    if full.exists():
        return full
    if state.repo_id:
        try:
            hf_hub_download(
                repo_id=state.repo_id,
                repo_type="dataset",
                token=token(),
                filename=rel,
                revision=state.revision,
                local_dir=state.root,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("frame_ts download failed for ep %s: %s", episode_index, e)
            return None
    return full if full.exists() else None


def _frame_timestamps(state: DatasetState, episode_index: int) -> list[float]:
    if episode_index in state.frame_ts_cache:
        return state.frame_ts_cache[episode_index]
    path = _episode_data_path(state, episode_index)
    if path is None:
        return []
    try:
        df = pd.read_parquet(path, columns=["episode_index", "timestamp"])
    except Exception as e:  # noqa: BLE001
        logger.warning("frame_ts read failed for ep %s: %s", episode_index, e)
        return []
    ts = (
        df.loc[df["episode_index"] == episode_index, "timestamp"].astype(float).tolist()
    )
    ts.sort()
    state.frame_ts_cache[episode_index] = ts
    return ts


def _coerce_existing_atom(
    raw: Any, fallback_ts: float | None = None
) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        try:
            raw = dict(raw)
        except Exception:  # noqa: BLE001
            return None
    if not raw.get("role"):
        return None
    tool_calls = raw.get("tool_calls")
    if tool_calls is not None and not isinstance(tool_calls, list):
        tool_calls = [tool_calls]
    camera = raw.get("camera")
    if isinstance(camera, str) and not camera:
        camera = None
    raw_ts = raw.get("timestamp")
    if raw_ts is None:
        # v3.1 event rows don't carry a ``timestamp`` field in the struct —
        # the writer drops it because the parquet row's frame timestamp is
        # already the event's firing time. Use the caller-provided fallback
        # so dedup doesn't collapse every event atom into one (timestamp=0.0)
        # entry.
        timestamp = float(fallback_ts) if fallback_ts is not None else 0.0
    else:
        timestamp = float(raw_ts)
    return {
        "role": str(raw["role"]),
        "content": None if raw.get("content") is None else str(raw.get("content")),
        "style": raw.get("style"),
        "timestamp": timestamp,
        "camera": camera if isinstance(camera, str) else None,
        "tool_calls": tool_calls or None,
    }


def _extract_existing_atoms_from_table(
    table: pa.Table, episode_index: int
) -> list[dict[str, Any]]:
    if "episode_index" not in table.column_names:
        return []

    episode_col = table.column("episode_index").to_pylist()
    persistent_col = (
        table.column(LANGUAGE_PERSISTENT).to_pylist()
        if LANGUAGE_PERSISTENT in table.column_names
        else None
    )
    events_col = (
        table.column(LANGUAGE_EVENTS).to_pylist()
        if LANGUAGE_EVENTS in table.column_names
        else None
    )
    # Event rows don't carry their own ``timestamp`` in the v3.1 struct;
    # the parquet row's frame timestamp IS the event's firing time. Read
    # the timestamp column so we can pass it as a fallback to
    # ``_coerce_existing_atom`` — without this, every event row defaults
    # to timestamp=0.0 and dedup collapses them all into one.
    ts_col = (
        table.column("timestamp").to_pylist()
        if "timestamp" in table.column_names
        else None
    )

    atoms: list[dict[str, Any]] = []
    seen: set[str] = set()
    persistent_loaded = False

    def add_many(raw_atoms: Any, fallback_ts: float | None = None) -> None:
        if not raw_atoms:
            return
        for raw in raw_atoms:
            atom = _coerce_existing_atom(raw, fallback_ts=fallback_ts)
            if atom is None:
                continue
            key = json.dumps(atom, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            atoms.append(atom)

    for row_idx, ep_value in enumerate(episode_col):
        if int(ep_value) != int(episode_index):
            continue
        if persistent_col is not None and not persistent_loaded:
            add_many(persistent_col[row_idx])
            persistent_loaded = True
        if events_col is not None:
            row_ts = float(ts_col[row_idx]) if ts_col is not None else None
            add_many(events_col[row_idx], fallback_ts=row_ts)

    atoms.sort(
        key=lambda a: (a["timestamp"], a.get("style") or "", a.get("role") or "")
    )
    return atoms


def _snap(ts: float, frame_ts: list[float]) -> float:
    if not frame_ts:
        return float(ts)
    return float(min(frame_ts, key=lambda f: abs(f - ts)))


VIEW_DEPENDENT_STYLES = {"vqa", "trace"}


def _validate_atom(atom: dict[str, Any]) -> None:
    style = atom.get("style")
    if style is not None and style not in KNOWN_STYLES:
        raise HTTPException(
            status_code=400, detail=f"Unknown language style: {style!r}"
        )
    has_content = atom.get("content") is not None
    has_tools = bool(atom.get("tool_calls"))
    if not (has_content or has_tools):
        raise HTTPException(
            status_code=400, detail="atom must have content or tool_calls"
        )
    if style is None and not has_tools:
        raise HTTPException(
            status_code=400, detail="style=None requires tool_calls (speech atom)"
        )
    camera = atom.get("camera")
    if camera is not None and not isinstance(camera, str):
        raise HTTPException(status_code=400, detail="camera must be a string or null")
    # Mirror lerobot's row-level invariant: camera is set iff the style is
    # view-dependent. We don't enforce camera-required here because the
    # visualizer accepts in-progress edits where the user hasn't picked a
    # camera yet — the writer (or the next save round-trip) will surface
    # the missing tag. We DO reject camera-on-non-view-dependent so the
    # field can't drift onto task_aug/subtask/plan/memory rows.
    if camera is not None and style is not None and style not in VIEW_DEPENDENT_STYLES:
        raise HTTPException(
            status_code=400,
            detail=f"camera must be null for style={style!r} (only vqa/trace are view-dependent)",
        )


def _normalize_atom(atom: dict[str, Any], *, with_timestamp: bool) -> dict[str, Any]:
    """Coerce an atom into a language-column struct row.

    Field order matches the canonical schema in ``lerobot.datasets.language``
    (``PERSISTENT_ROW_FIELDS`` / ``EVENT_ROW_FIELDS``); pyarrow infers the
    struct schema from insertion order. Persistent rows carry their own
    ``timestamp`` (the moment the state became active); event rows do NOT —
    the parquet frame's ``timestamp`` column IS the event's firing time, so a
    per-row ``timestamp`` field would be redundant (matches lerobot#3471's
    ``language_event_row_arrow_type``, which omits it).
    """
    camera = atom.get("camera")
    if isinstance(camera, str) and not camera:
        camera = None
    row: dict[str, Any] = {
        "role": str(atom["role"]),
        "content": None if atom.get("content") is None else str(atom["content"]),
        "style": atom.get("style"),
    }
    if with_timestamp:
        row["timestamp"] = float(atom.get("timestamp", 0.0))
    row["camera"] = camera if isinstance(camera, str) else None
    row["tool_calls"] = list(atom["tool_calls"]) if atom.get("tool_calls") else None
    return row


# --- Export -------------------------------------------------------------------


def _materialize_table(
    table: pa.Table, atoms_by_ep: dict[int, list[dict[str, Any]]]
) -> tuple[pa.Table, int, int]:
    if (
        "episode_index" not in table.column_names
        or "timestamp" not in table.column_names
    ):
        raise HTTPException(
            status_code=400,
            detail="data parquet missing 'episode_index' or 'timestamp' columns",
        )

    episode_col = table.column("episode_index").to_pylist()
    ts_col = [float(x) for x in table.column("timestamp").to_pylist()]
    n_rows = table.num_rows

    persistent_by_ep: dict[int, list[dict[str, Any]]] = {}
    events_by_ep_ts: dict[int, dict[float, list[dict[str, Any]]]] = {}

    n_persistent_total = 0
    n_event_total = 0

    unique_eps = sorted(set(episode_col))
    for ep_idx in unique_eps:
        atoms = atoms_by_ep.get(int(ep_idx))
        if atoms is None:
            atoms = _extract_existing_atoms_from_table(table, int(ep_idx))
        persistent_rows: list[dict[str, Any]] = []
        frame_ts = sorted(
            {ts_col[i] for i in range(n_rows) if episode_col[i] == ep_idx}
        )

        buckets: dict[float, list[dict[str, Any]]] = {}
        for atom in atoms:
            col = column_for_style(atom.get("style"))
            if col == LANGUAGE_PERSISTENT:
                persistent_rows.append(_normalize_atom(atom, with_timestamp=True))
            else:
                # The event row's firing time lives in the parquet frame's
                # ``timestamp`` column, so we bucket by the snapped timestamp
                # but do NOT store it inside the event struct (matches the
                # lerobot#3471 writer / canonical schema).
                ts = float(atom.get("timestamp", 0.0))
                if frame_ts:
                    ts = _snap(ts, frame_ts)
                buckets.setdefault(ts, []).append(
                    _normalize_atom(atom, with_timestamp=False)
                )

        persistent_rows.sort(
            key=lambda r: (r["timestamp"], r.get("style") or "", r.get("role") or "")
        )
        persistent_by_ep[ep_idx] = persistent_rows

        for rows in buckets.values():
            rows.sort(key=lambda r: (r.get("style") or "", r.get("role") or ""))
        events_by_ep_ts[ep_idx] = buckets

        n_persistent_total += len(persistent_rows)
        n_event_total += sum(len(v) for v in buckets.values())

    per_row_persistent = [
        persistent_by_ep.get(episode_col[i], []) for i in range(n_rows)
    ]
    per_row_events = [
        events_by_ep_ts.get(episode_col[i], {}).get(ts_col[i], [])
        for i in range(n_rows)
    ]

    keep_names: list[str] = []
    keep_cols: list[Any] = []
    for name in table.column_names:
        if name == "subtask_index":
            continue
        if name in {LANGUAGE_PERSISTENT, LANGUAGE_EVENTS, "tools"}:
            continue
        keep_names.append(name)
        keep_cols.append(table.column(name))

    persistent_arr = pa.array(per_row_persistent)
    events_arr = pa.array(per_row_events)

    # NOTE: we deliberately do NOT add a per-row ``tools`` column. The ``say``
    # tool *schema* is dataset-level metadata and lives in
    # ``meta/info.json["tools"]`` (written in ``_do_export``), exactly as the
    # lerobot#3471 pipeline does. Tool *calls* travel per-row inside the
    # ``tool_calls`` field of the language structs. Any pre-existing ``tools``
    # column is stripped in the keep-loop above.
    new_names = keep_names + [LANGUAGE_PERSISTENT, LANGUAGE_EVENTS]
    new_cols = keep_cols + [persistent_arr, events_arr]
    return (
        pa.Table.from_arrays(new_cols, names=new_names),
        n_persistent_total,
        n_event_total,
    )


def _materialize_tree(src: Path, dst: Path, *, force_copy: bool) -> None:
    """Recreate ``src`` under ``dst`` as real files (no symlinks).

    Hardlinks each file when ``force_copy`` is False and the source/target sit
    on the same filesystem (cheap, self-contained, uploadable); otherwise
    falls back to a byte copy. The result is always a standalone tree that
    survives being moved and is uploaded verbatim by ``upload_folder``.
    """

    def _copy_file(s: str, d: str) -> None:
        if not force_copy:
            try:
                os.link(s, d)
                return
            except OSError:
                pass
        shutil.copy2(s, d)

    shutil.copytree(src, dst, copy_function=_copy_file)


def _copy_object_sidecar(store: SidecarStore, destination: Path) -> None:
    """Copy publishable sidecar records while excluding staging/job caches."""
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("meta.json", "current.json"):
        source = store.root / name
        if source.is_file():
            shutil.copy2(source, destination / name)
    revisions = store.root / "revisions"
    if revisions.is_dir():
        shutil.copytree(revisions, destination / "revisions")


def _do_export(
    state: DatasetState, output_dir: str | None, copy_videos: bool
) -> dict[str, Any]:
    for folder in ("meta", "data", "videos"):
        for entry in (state.root / folder).rglob("*"):
            if entry.is_symlink():
                inside(entry, state.root)
    if output_dir:
        out_root = inside(output_dir)
    else:
        EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
        name = (state.repo_id or Path(state.root).name or "dataset").replace("/", "__")
        out_root = EXPORT_ROOT / f"{name}_annotated_{uuid.uuid4().hex[:10]}"

    if out_root.exists() or out_root.is_relative_to(state.root):
        raise HTTPException(
            409, "Export must use a new directory outside the source dataset"
        )
    out_root.mkdir(parents=True, exist_ok=False)

    # Copy meta/
    src_meta = state.root / "meta"
    dst_meta = out_root / "meta"
    if dst_meta.exists():
        shutil.rmtree(dst_meta)
    shutil.copytree(src_meta, dst_meta)

    info_path = dst_meta / "info.json"
    info = json.loads(info_path.read_text())
    info.setdefault("features", {})
    info["features"].pop("subtask_index", None)
    info["features"][LANGUAGE_PERSISTENT] = {
        "dtype": "language",
        "shape": [1],
        "names": None,
    }
    info["features"][LANGUAGE_EVENTS] = {
        "dtype": "language",
        "shape": [1],
        "names": None,
    }
    # The ``say`` tool schema is dataset-level metadata, stored at the top of
    # info.json under "tools" (NOT as a per-frame feature). Mirrors the
    # lerobot#3471 pipeline's ``_ensure_annotation_metadata_in_info``: merge
    # additively so any user-declared tools are preserved, and stop emitting
    # the stray ``tools`` feature older exports added.
    info["features"].pop("tools", None)
    existing_tools = info.get("tools") or []
    tool_names = {
        (t.get("function") or {}).get("name")
        for t in existing_tools
        if isinstance(t, dict)
    }
    if SAY_TOOL_SCHEMA["function"]["name"] not in tool_names:
        info["tools"] = [*existing_tools, SAY_TOOL_SCHEMA]
    info_path.write_text(json.dumps(info, indent=2))

    # Drop legacy meta files if present
    for legacy in ("subtasks.parquet", "tasks_high_level.parquet"):
        p = dst_meta / legacy
        if p.exists():
            p.unlink()

    # Make sure data AND videos are downloaded for HF datasets. The export
    # must be a self-contained, loadable dataset (the writer only rewrites
    # the parquet shards; videos are carried over untouched), so we pull the
    # video shards too — otherwise the exported folder is missing the
    # observation videos and won't load. Mirrors the lerobot#3471 pipeline,
    # which annotates a full local snapshot in place.
    data_dir = state.root / "data"
    data_files = sorted(data_dir.rglob("*.parquet"))
    if state.repo_id:
        snapshot_download(
            state.repo_id,
            repo_type="dataset",
            token=token(),
            revision=state.revision,
            local_dir=state.root,
            allow_patterns=["data/**/*.parquet", "videos/**"],
        )
        data_files = sorted(data_dir.rglob("*.parquet"))
    if not data_files:
        raise HTTPException(status_code=404, detail="No data parquet files found")

    atoms_by_ep = {ep: ann.atoms for ep, ann in state.annotations.items()}

    n_persistent = 0
    n_events = 0
    for src_path in data_files:
        rel_path = src_path.relative_to(state.root)
        dst_path = out_root / rel_path
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        table = pq.read_table(src_path)
        new_table, np_n, ne_n = _materialize_table(table, atoms_by_ep)
        n_persistent += np_n
        n_events += ne_n
        pq.write_table(new_table, dst_path)

    # Carry over the video shards so the export is self-contained. We
    # materialize *real* files (hardlink where the filesystem allows it, else
    # copy) rather than symlinking the source tree: a symlinked ``videos/``
    # breaks as soon as the folder is moved and is not uploaded by
    # ``HfApi.upload_folder``, which is exactly the "downloaded dataset isn't
    # usable" problem. ``copy_videos=True`` forces a full byte copy (used by
    # the push-to-hub path, where the upload reads the bytes anyway).
    src_videos = state.root / "videos"
    dst_videos = out_root / "videos"
    if src_videos.exists():
        if dst_videos.exists() or dst_videos.is_symlink():
            if dst_videos.is_symlink():
                dst_videos.unlink()
            else:
                shutil.rmtree(dst_videos)
        _materialize_tree(src_videos, dst_videos, force_copy=copy_videos)

    # Carry the current object sidecar into the exported dataset without
    # changing the source tree.  The sidecar is optional, so language-only
    # exports remain backwards compatible.
    object_store = _sidecar(state)
    object_revision = object_store.current_revision()
    if object_revision:
        dst_sidecar = out_root / "annotations" / "sam3"
        _copy_object_sidecar(object_store, dst_sidecar)

    return {
        "output_dir": str(out_root),
        "persistent_rows": n_persistent,
        "event_rows": n_events,
        "object_annotation_revision": object_revision,
    }


# --- FastAPI app --------------------------------------------------------------

app = FastAPI(title="LeRobot dataset visualizer — annotation backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "service": "lerobot-visualizer-annotate",
            "persistent_styles": sorted(PERSISTENT_STYLES),
            "event_styles": sorted(EVENT_ONLY_STYLES),
        }
    )


@app.post("/api/dataset/load")
def load_dataset(req: LoadRequest) -> JSONResponse:
    state = _ensure_state(req)
    return JSONResponse(
        {
            "repo_id": state.repo_id,
            "local_path": state.local_path,
            "revision": state.revision,
            "root": str(state.root),
            "fps": float(state.info.get("fps", 30)),
            "num_episodes": int(state.episodes_df["episode_index"].nunique()),
            "persistent_styles": sorted(PERSISTENT_STYLES),
            "event_styles": sorted(EVENT_ONLY_STYLES),
        }
    )


@app.get("/api/episodes/{episode_index}/atoms")
def get_episode_atoms(
    episode_index: int,
    repo_id: str | None = None,
    revision: str | None = None,
    local_path: str | None = None,
) -> JSONResponse:
    state = _ensure_state(
        DatasetRef(repo_id=repo_id, revision=revision, local_path=local_path)
    )
    ann = state.annotations.get(episode_index)
    if ann is None:
        path = _episode_data_path(state, episode_index)
        atoms: list[dict[str, Any]] = []
        if path is not None:
            try:
                schema = pq.read_schema(path)
                columns = ["episode_index"]
                # Always pull the row timestamp — needed as a fallback for
                # event rows whose v3.1 struct intentionally omits it.
                if "timestamp" in schema.names:
                    columns.append("timestamp")
                if LANGUAGE_PERSISTENT in schema.names:
                    columns.append(LANGUAGE_PERSISTENT)
                if LANGUAGE_EVENTS in schema.names:
                    columns.append(LANGUAGE_EVENTS)
                if LANGUAGE_PERSISTENT in columns or LANGUAGE_EVENTS in columns:
                    atoms = _extract_existing_atoms_from_table(
                        pq.read_table(path, columns=columns),
                        episode_index,
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "language column read failed for ep %s: %s", episode_index, e
                )
        ann = EpisodeAnnotations(atoms=atoms)
        if atoms:
            state.annotations[episode_index] = ann
    return JSONResponse({"episode_index": episode_index, "atoms": ann.atoms})


@app.post("/api/episodes/{episode_index}/atoms")
def set_episode_atoms(episode_index: int, payload: EpisodeAtomsPayload) -> JSONResponse:
    if episode_index != payload.episode_index:
        raise HTTPException(status_code=400, detail="episode index mismatch")
    state = _ensure_state(
        DatasetRef(repo_id=payload.repo_id, local_path=payload.local_path)
    )
    atoms = [a.model_dump() for a in payload.atoms]
    for atom in atoms:
        _validate_atom(atom)
    # Snap event timestamps to exact frame timestamps (matches lerobot#3471).
    frame_ts = _frame_timestamps(state, episode_index)
    for atom in atoms:
        if column_for_style(atom.get("style")) == LANGUAGE_EVENTS and frame_ts:
            atom["timestamp"] = _snap(float(atom["timestamp"]), frame_ts)
    state.annotations[episode_index] = EpisodeAnnotations(atoms=atoms)
    _save_annotations(state)
    return JSONResponse(
        {"ok": True, "saved": len(atoms), "path": str(state.annotations_path)}
    )


def _prepare_sam3_media(state: DatasetState) -> None:
    """Ensure a Hub-backed worker has video assets beside its metadata."""
    if not state.repo_id or state.local_path:
        return
    if os.environ.get("LEVI_SAM3_DOWNLOAD_VIDEOS", "1").lower() not in _SAM3_ENABLED_VALUES:
        return
    try:
        # A Hub state is initially metadata-only so browsing remains cheap.
        # Real SAM3 needs local video bytes; keep the snapshot in the
        # account/revision-scoped cache selected by _load_state.
        snapshot_download(
            state.repo_id,
            repo_type="dataset",
            token=token(),
            revision=state.revision,
            local_dir=state.root,
            allow_patterns=["videos/**"],
        )
    except Exception as exc:
        logger.warning("SAM3 Hub video preparation failed: %s", exc)
        raise HTTPException(
            502,
            "Unable to prepare Hub video assets for SAM3; check dataset access and the current Hugging Face account.",
        ) from exc


def _sam3_checkpoint_dir() -> Path:
    configured = os.environ.get("LEVI_SAM3_CHECKPOINT_DIR")
    if not configured:
        return SAM3_CHECKPOINT_DIR
    try:
        return inside(configured)
    except ValueError:
        logger.warning("Ignoring SAM3 checkpoint directory outside LEVI_WORKSPACE")
        return SAM3_CHECKPOINT_DIR


def _sam3_checkpoint_path() -> Path:
    configured = os.environ.get("LEVI_SAM3_CHECKPOINT")
    if configured:
        return Path(configured).expanduser()
    return _sam3_checkpoint_dir() / SAM3_MODEL_FILENAME


def _read_sam3_download() -> dict[str, Any]:
    path = _sam3_checkpoint_dir() / SAM3_PROGRESS_FILENAME
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _sam3_auth_status() -> dict[str, Any]:
    """Return account identity without returning a token or profile payload."""
    active = token()
    scope = credential_scope(active)
    now = time.monotonic()
    cached = _SAM3_AUTH_CACHE.get(scope)
    if cached and now - cached[0] < 30:
        return dict(cached[1])
    result: dict[str, Any] = {
        "authenticated": False,
        "username": None,
        "source": "none",
    }
    try:
        user = HfApi(token=active).whoami()
        username = (
            (user.get("name") or user.get("username"))
            if isinstance(user, dict)
            else None
        )
        if username:
            result.update(
                authenticated=True,
                username=str(username),
                source=("browser" if hub_token.get() else "environment/cache"),
            )
    except Exception as exc:  # noqa: BLE001
        # Public model metadata remains usable when a token is absent or expired.
        logger.debug("Hugging Face account status unavailable: %s", exc)
    _SAM3_AUTH_CACHE[scope] = (now, result)
    return dict(result)


def _sam3_status_payload() -> dict[str, Any]:
    checkpoint = _sam3_checkpoint_path()
    download = _read_sam3_download()
    checkpoint_ready = checkpoint.is_file()
    if checkpoint_ready and download.get("phase") not in {"downloading", "error"}:
        download = {
            **download,
            "phase": "ready",
            "bytes": checkpoint.stat().st_size,
            "path": str(checkpoint),
        }
    elif not download:
        download = {
            "phase": "ready" if checkpoint_ready else "idle",
            "bytes": checkpoint.stat().st_size if checkpoint_ready else 0,
            "total_bytes": checkpoint.stat().st_size if checkpoint_ready else None,
            "percent": 100.0 if checkpoint_ready else 0.0,
            "path": str(checkpoint),
        }
    else:
        download.setdefault("path", str(checkpoint))
    size = checkpoint.stat().st_size if checkpoint_ready else 0
    worker_project = (
        Path(__file__).resolve().parents[1] / "integrations" / "sam3" / "pyproject.toml"
    )
    worker_python = Path(
        os.environ.get(
            "LEVI_SAM3_WORKER_PYTHON",
            str(worker_project.parent / ".venv/bin/python"),
        )
    ).expanduser()
    return {
        "provider": "sam3",
        "enabled": _sam3_enabled(),
        "worker_project_present": worker_project.is_file(),
        "worker_python_present": worker_python.is_file(),
        "requires_user_checkpoint_access": True,
        "gpu_probe_performed": False,
        "manual_annotation_available": True,
        "model_repo": SAM3_MODEL_REPO,
        "model_filename": SAM3_MODEL_FILENAME,
        "model_revision": SAM3_MODEL_REVISION,
        "checkpoint_dir": str(_sam3_checkpoint_dir()),
        "checkpoint_path": str(checkpoint),
        "checkpoint_cached": checkpoint_ready,
        "checkpoint_size_bytes": size,
        "download": download,
        "hf_auth": _sam3_auth_status(),
        "message": (
            "SAM3 is disabled; set LEVI_SAM3_ENABLED=1 to enable it."
            if not _sam3_enabled()
            else "SAM3 is globally available; install the CUDA worker and sign in to Hugging Face before the first run."
        ),
    }


@app.get("/api/sam3/status")
def sam3_status() -> JSONResponse:
    """Report model/account/download state without importing Torch or probing CUDA."""
    return JSONResponse(_sam3_status_payload())


@app.get("/api/sam3/capabilities")
def sam3_capabilities() -> JSONResponse:
    """Backward-compatible alias for the global SAM3 status payload."""
    return JSONResponse(_sam3_status_payload())


_SAM3_PROMPT_PRESETS_PATH = STATE / "sam3_prompt_presets.json"
_SAM3_PROMPT_PRESET_LIMIT = 200
_SAM3_PROMPT_PRESET_LOCK = threading.Lock()


def _sam3_prompt_presets() -> list[dict[str, Any]]:
    """Reusable named prompt sets, shared across every dataset in this workspace."""
    data = read(_SAM3_PROMPT_PRESETS_PATH, {"presets": []})
    presets = data.get("presets") if isinstance(data, dict) else None
    return presets if isinstance(presets, list) else []


@app.get("/api/sam3/prompt-presets")
def sam3_list_prompt_presets() -> JSONResponse:
    return JSONResponse({"presets": _sam3_prompt_presets()})


@app.post("/api/sam3/prompt-presets")
def sam3_save_prompt_preset(request: Sam3PromptPresetRequest) -> JSONResponse:
    name = request.name.strip()
    if not name:
        raise HTTPException(400, "Preset name must not be empty")
    prompts = list(dict.fromkeys(item.strip() for item in request.prompts if item.strip()))
    if not prompts:
        raise HTTPException(400, "Preset must include at least one non-empty prompt")
    with _SAM3_PROMPT_PRESET_LOCK:
        presets = [item for item in _sam3_prompt_presets() if item.get("name") != name]
        if len(presets) >= _SAM3_PROMPT_PRESET_LIMIT:
            raise HTTPException(
                400, f"Prompt preset limit reached ({_SAM3_PROMPT_PRESET_LIMIT})"
            )
        presets.append({"name": name, "prompts": prompts})
        atomic(_SAM3_PROMPT_PRESETS_PATH, {"presets": presets})
    return JSONResponse({"ok": True, "presets": presets})


@app.delete("/api/sam3/prompt-presets/{name}")
def sam3_delete_prompt_preset(name: str) -> JSONResponse:
    with _SAM3_PROMPT_PRESET_LOCK:
        presets = [item for item in _sam3_prompt_presets() if item.get("name") != name]
        atomic(_SAM3_PROMPT_PRESETS_PATH, {"presets": presets})
    return JSONResponse({"ok": True, "presets": presets})


@app.post("/api/sam3/plan")
def sam3_plan(request: Sam3PlanRequest) -> JSONResponse:
    state = _ensure_state(DatasetRef.model_validate(request.model_dump()))
    _validate_sam3_plan(state, request)
    _, _, payload = _sam3_plan_payload(state, request)
    return JSONResponse(_public_sam3_plan(payload))


@app.post("/api/sam3/run")
def sam3_run(request: Sam3RunRequest) -> JSONResponse:
    state = _ensure_state(DatasetRef.model_validate(request.model_dump()))
    _validate_sam3_plan(state, request)
    store, plan_path, plan_payload = _sam3_plan_payload(state, request)
    if request.provider == "fake":
        annotations = fake_annotations(request)
        revision = store.publish(
            annotations,
            parent_revision=store.current_revision(),
            model={"provider": "fake", "model_version": "fixture", "cpu_only": True},
        )
        return JSONResponse(
            {
                "ok": True,
                "provider": "fake",
                "revision_id": revision["revision_id"],
                "count": len(annotations),
                "review_status": "suggested",
                "plan_id": plan_payload["plan_id"],
            }
        )
    if not _sam3_enabled():
        raise HTTPException(
            503,
            "SAM3 is disabled; set LEVI_SAM3_ENABLED=1 to enable it",
        )
    worker_project = Path(__file__).resolve().parents[1] / "integrations" / "sam3"
    worker_python = Path(
        os.environ.get(
            "LEVI_SAM3_WORKER_PYTHON", str(worker_project / ".venv/bin/python")
        )
    ).expanduser()
    if not (worker_project / "levi_sam3_worker" / "worker.py").is_file():
        raise HTTPException(
            503, "SAM3 worker source is missing from this LEVI checkout"
        )
    if not worker_python.is_file():
        raise HTTPException(
            503,
            f"SAM3 worker environment not found at {worker_python}; see integrations/sam3/README.md",
        )
    _prepare_sam3_media(state)
    job_id = uuid.uuid4().hex
    result_path = store.root / "staging" / "results" / f"{job_id}.json"
    log_path = store.root / "staging" / "jobs" / f"{job_id}.log"
    progress_path = store.root / "staging" / "jobs" / f"{job_id}.progress.json"
    job = {
        "job_id": job_id,
        "status": "queued",
        "provider": "sam3",
        "plan_id": plan_payload["plan_id"],
        "plan_path": str(plan_path),
        "result_path": str(result_path),
        "log_path": str(log_path),
        "progress_path": str(progress_path),
        "created_at": pd.Timestamp.utcnow().isoformat(),
    }
    atomic(_sam3_job_path(store, job_id), job)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_stream = log_path.open("ab")
    try:
        process = subprocess.Popen(
            [
                str(worker_python),
                "-m",
                "levi_sam3_worker.cli",
                "--plan",
                str(plan_path),
                "--output",
                str(result_path),
                "--progress",
                str(progress_path),
            ],
            cwd=worker_project,
            env={
                **os.environ,
                "LEVI_SAM3_ENABLED": "1",
                "LEVI_SAM3_CHECKPOINT_DIR": str(_sam3_checkpoint_dir()),
                # Pass the current browser account only for this child process.
                # It is never written to the plan, job record, or log.
                **({"HF_TOKEN": token()} if token() else {}),
            },
            stdin=subprocess.DEVNULL,
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            start_new_session=(os.name == "posix"),
        )
    except OSError as exc:
        log_stream.close()
        job.update(status="failed", error=f"Unable to start SAM3 worker: {exc}")
        atomic(_sam3_job_path(store, job_id), job)
        raise HTTPException(503, job["error"]) from exc
    finally:
        if "process" in locals():
            log_stream.close()
    _SAM3_PROCESSES[job_id] = process
    job.update(status="running", pid=process.pid)
    atomic(_sam3_job_path(store, job_id), job)
    return JSONResponse({"ok": True, **_public_sam3_job(job)}, status_code=202)


@app.get("/api/sam3/revisions")
def sam3_revisions(
    repo_id: str | None = None,
    revision: str | None = None,
    local_path: str | None = None,
) -> JSONResponse:
    state = _ensure_state(
        DatasetRef(repo_id=repo_id, revision=revision, local_path=local_path)
    )
    store = _sidecar(state)
    return JSONResponse(
        {"current": store.current_revision(), "revisions": store.list_revisions()}
    )


@app.get("/api/sam3/episodes/{episode_index}/objects")
def sam3_episode_objects(
    episode_index: int,
    repo_id: str | None = None,
    revision: str | None = None,
    local_path: str | None = None,
    camera_key: str | None = None,
    frame_index: int | None = None,
    annotation_revision: str | None = None,
) -> JSONResponse:
    state = _ensure_state(
        DatasetRef(repo_id=repo_id, revision=revision, local_path=local_path)
    )
    store = _sidecar(state)
    rows = store.read_episode(episode_index, annotation_revision)
    if camera_key:
        rows = [row for row in rows if row["camera_key"] == camera_key]
    if frame_index is not None:
        rows = [row for row in rows if row["frame_index"] == frame_index]
    return JSONResponse(
        {
            "episode_index": episode_index,
            "revision": annotation_revision or store.current_revision(),
            "objects": rows,
        }
    )


@app.get("/api/sam3/jobs/{job_id}")
def sam3_job_status(
    job_id: str,
    repo_id: str | None = None,
    revision: str | None = None,
    local_path: str | None = None,
) -> JSONResponse:
    state = _ensure_state(
        DatasetRef(repo_id=repo_id, revision=revision, local_path=local_path)
    )
    store = _sidecar(state)
    path = _sam3_job_path(store, job_id)
    if not path.is_file():
        raise HTTPException(404, "SAM3 job not found")
    job = _collect_sam3_job(state, json.loads(path.read_text()))
    return JSONResponse(_public_sam3_job(job))


@app.post("/api/sam3/jobs/{job_id}/cancel")
def sam3_job_cancel(
    job_id: str,
    repo_id: str | None = None,
    revision: str | None = None,
    local_path: str | None = None,
) -> JSONResponse:
    state = _ensure_state(
        DatasetRef(repo_id=repo_id, revision=revision, local_path=local_path)
    )
    store = _sidecar(state)
    path = _sam3_job_path(store, job_id)
    if not path.is_file():
        raise HTTPException(404, "SAM3 job not found")
    job = json.loads(path.read_text())
    if job.get("status") in {"succeeded", "failed", "cancelled"}:
        _forget_sam3_process(job_id)
        return JSONResponse(_public_sam3_job(job))
    process = _SAM3_PROCESSES.get(job_id)
    if process is not None and process.poll() is None:
        if os.name == "posix":
            os.killpg(process.pid, __import__("signal").SIGTERM)
        else:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
    _forget_sam3_process(job_id)
    job.update(status="cancelled", finished_at=pd.Timestamp.utcnow().isoformat())
    atomic(path, job)
    return JSONResponse(_public_sam3_job(job))


@app.post("/api/sam3/edits")
def sam3_edit(
    request: Sam3EditRequest,
    repo_id: str | None = None,
    revision: str | None = None,
    local_path: str | None = None,
) -> JSONResponse:
    state = _ensure_state(
        DatasetRef(repo_id=repo_id, revision=revision, local_path=local_path)
    )
    store = _sidecar(state)
    if request.base_revision and request.base_revision != store.current_revision():
        raise HTTPException(409, "annotation revision is stale; reload before editing")
    result = store.apply_edit(request)
    return JSONResponse({"ok": True, **result})


@app.get("/api/episodes/{episode_index}/frame_timestamps")
def episode_frame_timestamps(
    episode_index: int,
    repo_id: str | None = None,
    revision: str | None = None,
    local_path: str | None = None,
) -> JSONResponse:
    state = _ensure_state(
        DatasetRef(repo_id=repo_id, revision=revision, local_path=local_path)
    )
    ts = _frame_timestamps(state, episode_index)
    return JSONResponse({"episode_index": episode_index, "timestamps": ts})


@app.post("/api/export")
def export_dataset(req: ExportRequest) -> JSONResponse:
    state = _ensure_state(req)
    return JSONResponse(_do_export(state, req.output_dir, req.copy_videos))


@app.post("/api/push_to_hub")
def push_to_hub(req: PushToHubRequest) -> JSONResponse:
    state = _ensure_state(req)
    if not state.repo_id and not req.new_repo_id:
        raise HTTPException(status_code=400, detail="repo_id or new_repo_id required")

    # Ensure data + videos are present locally before exporting.
    if state.repo_id:
        snapshot_download(
            state.repo_id,
            repo_type="dataset",
            token=token(),
            revision=state.revision,
            local_dir=state.root,
            allow_patterns=["data/**/*.parquet", "videos/**/*.mp4"],
        )
    export_result = _do_export(state, output_dir=None, copy_videos=True)
    export_dir = Path(export_result["output_dir"])

    target_repo = state.repo_id if req.push_in_place else req.new_repo_id
    if not target_repo:
        raise HTTPException(status_code=400, detail="No target repo")

    api = HfApi(token=req.hf_token)
    if not req.push_in_place:
        api.create_repo(
            repo_id=target_repo,
            repo_type="dataset",
            private=req.private,
            exist_ok=True,
        )
    api.upload_folder(
        folder_path=str(export_dir),
        repo_id=target_repo,
        repo_type="dataset",
        commit_message=req.commit_message,
    )
    return JSONResponse(
        {
            "ok": True,
            "repo_id": target_repo,
            "url": f"https://huggingface.co/datasets/{target_repo}",
            "message": f"Pushed annotated dataset to {target_repo}",
        }
    )
