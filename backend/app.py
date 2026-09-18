# Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
# LEVI modifications: v2 annotations, workspace boundaries, durable sidecars,
# idempotent per-dataset exports (one directory per source dataset, reused
# and refreshed in place — never a directory LEVI didn't create itself).
"""LeRobot dataset visualizer — annotation backend.

A small FastAPI service that lets the Next.js visualizer write the v3.1
language schema introduced in lerobot#3467 (PR1) and used by the steerable
annotation pipeline in lerobot#3471 (PR2). Specifically it owns:

- per-episode annotation state, persisted to a workbench-side sidecar
  directory named after the dataset itself (not an opaque hash — see
  ``dataset_display_slug``/``DatasetState.display_slug``), one JSON file
  per episode named ``episode_{index:06d}.json`` to match that episode's
  own data/video file (``DatasetState.annotations_dir`` / ``annotation_file``).
  The directory is deliberately name-only, not hash-suffixed, so several
  LEVI processes on one host annotating the same dataset share the exact
  same files (see ``_lookup_episode_annotations``).
- snapping event-style atom timestamps to exact source-frame timestamps
  (the writer in lerobot#3471 enforces exact match)
- exporting the annotated dataset by rewriting ``data/chunk-*/file-*.parquet``
  with two new columns:
    * ``language_persistent`` — broadcast per-episode (subtask/plan/memory)
    * ``language_events``     — per-frame (interjection/vqa, plus speech
      tool-call atoms with style=None)
  and a dataset-level ``tools`` column carrying the JSON schema for ``say``,
  plus a write-only ``annotations/language/`` per-episode JSON mirror for
  portability/audit (LEVI itself never reads it back — see ``_do_export``).
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
import math
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from huggingface_hub import HfApi, hf_hub_download, snapshot_download
from pydantic import BaseModel

from levi import naming
from levi.annotations import (
    ObjectAnnotation,
    ObjectEdit,
    Sam3Plan,
    SidecarStore,
    outcomes,
)
from levi.annotations.sam3_protocol import (
    fake_annotations,
    validate_annotations_for_plan,
)
from levi.auth import credential_scope, hub_token, token
from levi.catalog import atomic, display_name, local_root, read
from levi.paths import CACHE, EXPORTS, SAM3_CHECKPOINT_DIR, STATE, inside
from levi.versions import is_dataset_v2, is_dataset_v3, normalize_dataset_version

logger = logging.getLogger("lerobot-annotate")
logging.basicConfig(level=logging.INFO)

CACHE_ROOT = CACHE
EXPORT_ROOT = EXPORTS


def _is_v2_version(value: object) -> bool:
    """Accept both ``v2.1`` and the unprefixed aliases found in old exports."""
    if not isinstance(value, str):
        return False
    return is_dataset_v2(value)


def _finite_integer(value: object) -> int | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or not number.is_integer():
        return None
    return int(number)


# The mirror contains the PyTorch checkpoint layout expected by the pinned
# official SAM3 adapter. Keep these values configurable for future model
# revisions, while making the supported default explicit for every workspace.
SAM3_MODEL_REPO = os.getenv("LEVI_SAM3_MODEL_REPO", "1038lab/sam3")
SAM3_MODEL_FILENAME = os.getenv("LEVI_SAM3_MODEL_FILENAME", "sam3.pt")
SAM3_MODEL_REVISION = os.getenv("LEVI_SAM3_MODEL_REVISION", "main")
SAM3_PROGRESS_FILENAME = "download-progress.json"
# New ids are timestamps (levi.naming); legacy hex ids still validate.
SAM3_ID_PATTERN = r"[a-f0-9]{16,64}|" + naming.TIMESTAMP_PATTERN
_SAM3_AUTH_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_SAM3_DOWNLOAD_LOCK = threading.Lock()
_SAM3_DOWNLOAD_THREADS: dict[str, threading.Thread] = {}

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


def dataset_display_slug(repo_id: str | None, local_path: str | None) -> str:
    """On-disk key for a dataset's sidecars and exports: its unique catalog
    name when registered (see ``levi.catalog.display_name``), otherwise its
    folder name or ``org__name``. Never a hash."""
    return display_name(repo_id, local_path)


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
    # Optional end of an explicitly authored range (drag-to-select in the
    # timeline). LEVI-only editorial metadata for humans reviewing the
    # timeline — deliberately excluded from the exported struct by
    # ``_normalize_atom``'s explicit field allow-list, so it never touches
    # the lerobot#3467/#3471 schema. See that function for why.
    to: float | None = None
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


class OutcomePayload(BaseModel):
    repo_id: str | None = None
    local_path: str | None = None
    outcome: Literal["success", "failure"] | None = None


class Sam3PlanRequest(Sam3Plan):
    """A model-neutral SAM3 plan accepted by the annotation control plane."""


class Sam3RunRequest(Sam3PlanRequest):
    """Request used by the CPU fake provider and the optional real worker.

    plan_id lets the UI reuse the exact staged plan that passed preflight.
    It is deliberately kept out of the worker's model-neutral Sam3Plan.
    """

    plan_id: str | None = None


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
    # True when this came from the on-disk per-episode file, which must be
    # dropped (not resurrected) once that file disappears; False when
    # derived from the exported parquet's language columns instead, which
    # has no backing file to go stale against and stays safe to cache for
    # the rest of the process's life. See ``_lookup_episode_annotations``.
    from_file: bool = False


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
    info_signature: tuple[int, int, int] | None = None

    def _identity_hash(self, *, short: bool = False) -> str:
        """Deterministic hash of this dataset's identity — shared by every
        on-disk path below so they all key off the exact same notion of
        "which dataset is this" (repo/revision/local path, plus a
        credential scope for private Hub repos, so an account change can't
        reuse a previous account's private cache)."""
        identity_value = {
            "repo_id": self.repo_id,
            "revision": self.revision or "main",
            "local_path": self.local_path,
        }
        if self.repo_id:
            identity_value["credential_scope"] = self.credential_scope
        identity = json.dumps(identity_value, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(identity.encode()).hexdigest()
        return digest[:10] if short else digest

    @property
    def display_slug(self) -> str:
        """Dataset-name key for the *live, shared* annotation sidecars
        (``annotations_dir`` / ``object_annotations_path``), exports and
        reviews: the unique catalog name, never a hash. Several LEVI
        processes on the same host must resolve the same dataset to the exact
        same directory so their edits land in the same per-episode files (see
        ``_lookup_episode_annotations``). Uniqueness comes from the catalog,
        which disambiguates two datasets sharing a folder basename."""
        return dataset_display_slug(self.repo_id, self.local_path)

    @property
    def annotations_dir(self) -> Path:
        """Per-episode language-annotation sidecar directory — one JSON
        file per episode, named to match that episode's own file identifier
        (``episode_{index:06d}``, exactly like the corresponding
        ``data/.../episode_{index:06d}.parquet``/``.mp4``) rather than one
        opaque blob for the whole dataset. Lets a user (or another tool) add,
        replace or delete a single episode's annotations directly on disk.
        """
        return STATE / "annotations" / self.display_slug

    def annotation_file(self, episode_index: int) -> Path:
        return self.annotations_dir / f"episode_{episode_index:06d}.json"

    @property
    def legacy_annotations_path(self) -> Path:
        """Pre-refactor single-file sidecar (every episode in one JSON,
        keyed by index, named by a bare identity hash) — read once to
        migrate into ``annotations_dir`` and never written again."""
        return STATE / "annotations" / (self._identity_hash() + ".json")

    @property
    def object_annotations_path(self) -> Path:
        """Workspace sidecar root, independent from the source dataset tree."""
        return STATE / "object_annotations" / self.display_slug


_states: dict[str, DatasetState] = {}


def _state_key(req: DatasetRef) -> str:
    if req.local_path:
        return f"local::{Path(req.local_path).expanduser().resolve()}"
    if req.repo_id:
        # Hub permissions affect the resolved dataset. Namespace the in-memory
        # state and sidecar by a one-way token digest so account changes cannot
        # reuse a previous account's private cache.
        return f"hf::{req.repo_id}@{req.revision or 'main'}::{credential_scope()}"
    raise HTTPException(status_code=400, detail="need repo_id or local_path")


def _ensure_state(req: DatasetRef) -> DatasetState:
    if req.repo_id and req.repo_id.startswith("local/"):
        req = DatasetRef(local_path=str(local_root(req.repo_id)))
    if req.local_path:
        req.local_path = str(inside(req.local_path))
    key = _state_key(req)
    cached = _states.get(key)
    # A raw capture's browsing view is rebuilt in place when the capture
    # changes (levi/views.py); a replaced meta/info.json means the cached
    # episode table is stale. Inode + mtime + size, not mtime alone: a
    # rebuilt file is a new inode even within one mtime tick.
    if (
        cached is not None
        and cached.local_path
        and _info_signature(cached.root) != cached.info_signature
    ):
        _states.pop(key, None)
        cached = None
    if cached is not None:
        return cached
    return _load_state(req, key)


def _info_signature(root: Path) -> tuple[int, int, int] | None:
    try:
        st = (root / "meta/info.json").stat()
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_size)


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


def _reuse_sam3_plan(
    state: DatasetState,
    store: SidecarStore,
    request: Sam3RunRequest,
) -> tuple[Path, dict[str, Any]]:
    """Load a preflighted plan and reject edits between plan and run."""
    plan_id = request.plan_id
    if not plan_id:
        raise ValueError("plan_id is required")
    if not re.fullmatch(SAM3_ID_PATTERN, plan_id):
        raise HTTPException(400, "Invalid SAM3 plan ID")
    plan_path = store.root / "staging" / "plans" / f"{plan_id}.json"
    if not plan_path.is_file():
        raise HTTPException(404, "SAM3 plan not found")
    try:
        payload = json.loads(plan_path.read_text())
        plan_fields = set(Sam3Plan.model_fields)
        staged = Sam3Plan.model_validate(
            {key: payload[key] for key in plan_fields if key in payload}
        )
        current = Sam3Plan.model_validate(request.model_dump(exclude={"plan_id"}))
    except (OSError, ValueError, TypeError) as exc:
        raise HTTPException(400, "Invalid staged SAM3 plan") from exc
    if staged.model_dump() != current.model_dump():
        raise HTTPException(
            409,
            "SAM3 plan changed after preflight; create a new plan before running",
        )
    payload["status"] = "queued"
    atomic(plan_path, payload)
    return plan_path, payload


def _sam3_plan_payload(
    state: DatasetState, request: Sam3PlanRequest
) -> tuple[SidecarStore, Path, dict[str, Any]]:
    store = _sidecar(state)
    store.initialize()
    plan_id = naming.timestamp_id(store.root / "staging" / "plans", ".json")
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
        **request.model_dump(exclude={"plan_id"}),
    }
    atomic(plan_path, payload)
    return store, plan_path, payload


def _sam3_job_path(store: SidecarStore, job_id: str) -> Path:
    if not re.fullmatch(SAM3_ID_PATTERN, job_id):
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
        # The revision itself (a branch, tag or commit), made path-safe — not
        # a hash of it. The trailing credential-scope digest stays: it keeps
        # one HF account's private download from being reused by another.
        revision_key = naming.catalog_name(req.revision) if req.revision else "main"
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
    try:
        info = json.loads(info_path.read_text())
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid meta/info.json") from exc
    if not isinstance(info, dict):
        raise HTTPException(status_code=400, detail="meta/info.json must be an object")
    version = normalize_dataset_version(info.get("codebase_version"))
    if version is None:
        raise HTTPException(
            status_code=400,
            detail="Unsupported or missing codebase_version in meta/info.json",
        )

    episodes_root = root / "meta" / "episodes"
    declared_total_episodes = max(
        0, _finite_integer(info.get("total_episodes", 0)) or 0
    )

    try:
        fps = float(info.get("fps", 0))
    except (TypeError, ValueError, OverflowError) as exc:
        raise HTTPException(status_code=400, detail="Invalid dataset fps") from exc
    if not math.isfinite(fps) or fps <= 0:
        raise HTTPException(status_code=400, detail="Dataset fps must be positive")
    info = {**info, "codebase_version": version, "fps": fps}
    if is_dataset_v2(version):
        metadata = root / "meta/episodes.jsonl"
        if metadata.exists():
            try:
                episodes_df = pd.read_json(metadata, lines=True)
            except (OSError, ValueError, TypeError) as exc:
                raise HTTPException(
                    status_code=400, detail="Invalid v2 episode metadata"
                ) from exc
        else:
            episodes_df = pd.DataFrame(
                {"episode_index": range(declared_total_episodes)}
            )
    elif is_dataset_v3(version):
        files = sorted(episodes_root.rglob("*.parquet"))
        if not files:
            raise HTTPException(
                status_code=404, detail="No episodes parquet files found"
            )
        try:
            episodes_df = (
                pd.concat([pd.read_parquet(p) for p in files], ignore_index=True)
                .sort_values("episode_index")
                .reset_index(drop=True)
            )
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise HTTPException(
                status_code=400, detail="Invalid v3 episode metadata"
            ) from exc

    if "episode_index" not in episodes_df:
        raise HTTPException(
            status_code=400,
            detail="Episode metadata is missing episode_index",
        )
    episodes_df = episodes_df.copy()
    episode_values = pd.to_numeric(episodes_df["episode_index"], errors="coerce")
    valid_episode = (
        episode_values.notna()
        & (np.isfinite(episode_values.to_numpy(dtype=float)))
        & (episode_values % 1 == 0)
        & (episode_values >= 0)
    )
    episodes_df = episodes_df.loc[valid_episode].copy()
    episodes_df["episode_index"] = episode_values.loc[valid_episode].astype("int64")
    episodes_df = (
        episodes_df.drop_duplicates(subset=["episode_index"])
        .sort_values("episode_index")
        .reset_index(drop=True)
    )
    if declared_total_episodes > 0 and episodes_df.empty:
        raise HTTPException(status_code=400, detail="Episode metadata is empty")
    state = DatasetState(
        repo_id=req.repo_id,
        local_path=str(root) if req.local_path else None,
        revision=req.revision,
        credential_scope=credential_scope() if req.repo_id else "local",
        root=root,
        info=info,
        episodes_df=episodes_df,
        info_signature=_info_signature(root) if req.local_path else None,
    )
    _load_existing_annotations(state)
    _states[key] = state
    return state


def _coerce_v1_atoms(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """v1 format from older lerobot-annotate (subtasks/high_levels segments,
    predating the v3.1 atom schema) — used only by the legacy migration."""
    atoms: list[dict[str, Any]] = []
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
                                "arguments": {"text": str(seg["robot_utterance"])},
                            },
                        }
                    ],
                }
            )
    return atoms


def _write_episode_annotations(
    state: DatasetState, episode_index: int, atoms: list[dict[str, Any]]
) -> Path:
    """Write (or overwrite) one episode's annotation file. An empty atoms
    list still writes a file — recording "reviewed, nothing to annotate" is
    a different, deliberate state from "never touched". To remove the
    record entirely, use ``_delete_episode_annotations``.
    """
    state.annotations_dir.mkdir(parents=True, exist_ok=True)
    path = state.annotation_file(episode_index)
    atomic(path, {"episode_index": episode_index, "atoms": atoms})
    state.annotations[episode_index] = EpisodeAnnotations(atoms=atoms, from_file=True)
    return path


def _delete_episode_annotations(state: DatasetState, episode_index: int) -> bool:
    """Delete one episode's annotation file entirely. Returns whether a file
    actually existed to delete."""
    path = state.annotation_file(episode_index)
    existed = path.exists()
    if existed:
        path.unlink()
    state.annotations.pop(episode_index, None)
    return existed


def _migrate_legacy_annotations(state: DatasetState) -> None:
    """One-time upgrade from the old single-blob sidecar (every episode in
    one JSON file, named by a bare identity hash) to the new per-episode
    file layout. The legacy file is left in place afterward — inert, never
    read again — rather than deleted: it's user data, and there's no reason
    to remove it once migrated.
    """
    legacy_path = state.legacy_annotations_path
    if not legacy_path.exists() or state.annotations_dir.exists():
        return
    try:
        data = json.loads(legacy_path.read_text())
    except (OSError, ValueError) as e:
        logger.warning("legacy annotations migration failed for %s: %s", legacy_path, e)
        return
    for ep_str, payload in data.get("episodes", {}).items():
        try:
            ep_idx = int(ep_str)
        except ValueError:
            continue
        atoms = payload.get("atoms")
        if atoms is None:
            atoms = _coerce_v1_atoms(payload)
        _write_episode_annotations(state, ep_idx, [dict(a) for a in atoms])


def _reload_annotations_from_disk(state: DatasetState) -> None:
    """Full resync of every per-episode file into ``state.annotations``.
    Cheap enough to call again right before export (``_do_export``) so the
    exported dataset always reflects the very latest state written by *any*
    LEVI process sharing this workspace — not just this process's own edits
    plus whatever a handful of GETs happened to warm into its cache."""
    ann_dir = state.annotations_dir
    if not ann_dir.is_dir():
        return
    for path in sorted(ann_dir.glob("episode_*.json")):
        try:
            ep_idx = int(path.stem.removeprefix("episode_"))
        except ValueError:
            continue
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError) as e:
            logger.warning("annotation file read failed for %s: %s", path, e)
            continue
        atoms = payload.get("atoms", [])
        state.annotations[ep_idx] = EpisodeAnnotations(
            atoms=[dict(a) for a in atoms], from_file=True
        )


def _load_existing_annotations(state: DatasetState) -> None:
    _migrate_legacy_annotations(state)
    _reload_annotations_from_disk(state)


def _lookup_episode_annotations(
    state: DatasetState, episode_index: int
) -> EpisodeAnnotations | None:
    """Always-fresh read of one episode's per-episode annotation file.

    Multiple LEVI processes on the same host may share this workspace (a
    second collaborator's own ``levi serve``, or the same person on two
    ports) and write to the same ``annotations_dir`` (see
    ``DatasetState.display_slug``). A plain in-memory cache would keep
    serving whatever this process last saw, hiding another process's saves.

    An earlier version tried to short-circuit this with an mtime check
    (skip the re-read when the file's mtime matches what was last cached).
    That is unsound on this filesystem: two back-to-back writes — exactly
    the close-together-edits case this function exists to handle — can
    land on the *identical* mtime (confirmed empirically; see
    ``test_episode_atoms_stay_in_sync_across_concurrent_processes``), which
    would silently keep serving the first write's content forever. These
    files are at most a few KB, so just re-reading on every call is both
    simpler and actually correct — there is no meaningful cost to trade
    away the shortcut for.

    Returns ``None`` only when there is no on-disk file for this episode
    and nothing was previously derived from the exported parquet's
    language columns either — the caller should then compute that
    fallback itself.
    """
    path = state.annotation_file(episode_index)
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError:
        cached = state.annotations.get(episode_index)
        # A cached entry derived from the parquet fallback has no backing
        # file to go stale against and is still valid; one that came from
        # an actual file is stale now that the file is gone (deleted by
        # this or another process) and must not be resurrected.
        if cached is not None and not cached.from_file:
            return cached
        state.annotations.pop(episode_index, None)
        return None
    except (OSError, ValueError) as e:
        logger.warning("annotation file read failed for %s: %s", path, e)
        return state.annotations.get(episode_index)
    return EpisodeAnnotations(
        atoms=[dict(a) for a in payload.get("atoms", [])], from_file=True
    )


# --- Frame-timestamp helpers --------------------------------------------------


def _episode_data_path(state: DatasetState, episode_index: int) -> Path | None:
    rows = state.episodes_df[state.episodes_df["episode_index"] == episode_index]
    if rows.empty:
        return None
    row = rows.iloc[0]
    if _is_v2_version(state.info.get("codebase_version")):
        data_template = state.info.get("data_path")
        if not isinstance(data_template, str) or not data_template.strip():
            return None
        chunk_size = _finite_integer(state.info.get("chunks_size", 1000)) or 1000
        if chunk_size <= 0:
            chunk_size = 1000
        chunk_index = episode_index // chunk_size
        try:
            rel = data_template.format(
                episode_index=episode_index,
                episode_chunk=chunk_index,
                chunk_index=chunk_index,
                data_chunk_index=chunk_index,
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
    else:
        chunk_col, file_col = "data/chunk_index", "data/file_index"
        if chunk_col not in row or file_col not in row:
            return None
        chunk_index = _finite_integer(row[chunk_col])
        file_index = _finite_integer(row[file_col])
        if chunk_index is None or file_index is None:
            return None
        data_template = state.info.get("data_path")
        if not isinstance(data_template, str) or not data_template.strip():
            data_template = "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet"
        try:
            rel = data_template.format(
                chunk_index=chunk_index,
                file_index=file_index,
                data_chunk_index=chunk_index,
                data_file_index=file_index,
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
    try:
        full = inside(rel, state.root)
    except ValueError:
        return None
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
        schema = pq.read_schema(path)
        columns = ["timestamp"]
        if "episode_index" in schema.names:
            columns.insert(0, "episode_index")
        df = pd.read_parquet(path, columns=columns)
    except Exception as e:  # noqa: BLE001
        logger.warning("frame_ts read failed for ep %s: %s", episode_index, e)
        return []
    if "episode_index" in df:
        episode_values = pd.to_numeric(df["episode_index"], errors="coerce")
        df = df[episode_values == episode_index]
    values = pd.to_numeric(df["timestamp"], errors="coerce")
    values = values[np.isfinite(values.to_numpy(dtype=float))]
    ts = sorted(set(values.astype(float).tolist()))
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
    try:
        timestamp = (
            float(fallback_ts)
            if raw_ts is None and fallback_ts is not None
            else 0.0
            if raw_ts is None
            else float(raw_ts)
        )
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(timestamp):
        return None
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
        current_episode = _finite_integer(ep_value)
        if current_episode is None:
            continue
        if current_episode != int(episode_index):
            continue
        if persistent_col is not None and not persistent_loaded:
            add_many(persistent_col[row_idx])
            persistent_loaded = True
        if events_col is not None:
            row_ts = None
            if ts_col is not None:
                try:
                    candidate = float(ts_col[row_idx])
                    row_ts = candidate if math.isfinite(candidate) else None
                except (TypeError, ValueError, OverflowError):
                    pass
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
    try:
        timestamp = float(atom.get("timestamp", 0.0))
        to_value = float(atom["to"]) if atom.get("to") is not None else None
    except (TypeError, ValueError, OverflowError) as exc:
        raise HTTPException(
            status_code=400, detail="atom timestamps must be numeric"
        ) from exc
    if not math.isfinite(timestamp) or (
        to_value is not None and (not math.isfinite(to_value) or to_value < timestamp)
    ):
        raise HTTPException(
            status_code=400, detail="atom timestamps must be finite and ordered"
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

    Deliberately an explicit field allow-list, not a dict spread: this is
    what keeps LEVI-only editorial fields (e.g. ``to``, the timeline's
    optional range end) from ever leaking into the exported struct. Do not
    change this to spread ``atom`` — that would silently widen the schema
    lerobot#3467/#3471 pin down.
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
) -> tuple[pa.Table, int, int, dict[int, list[dict[str, Any]]]]:
    """Returns the rewritten table plus the raw (un-normalized, as-authored —
    including LEVI-only fields like ``to``) atoms actually used per episode,
    for the caller's write-only ``annotations/language/`` export mirror.
    """
    if (
        "episode_index" not in table.column_names
        or "timestamp" not in table.column_names
    ):
        raise HTTPException(
            status_code=400,
            detail="data parquet missing 'episode_index' or 'timestamp' columns",
        )

    try:
        episode_col = [
            _finite_integer(x) for x in table.column("episode_index").to_pylist()
        ]
        if any(value is None for value in episode_col):
            raise HTTPException(
                status_code=400, detail="data parquet has invalid episode indices"
            )
        ts_col = [float(x) for x in table.column("timestamp").to_pylist()]
    except (TypeError, ValueError, OverflowError) as exc:
        raise HTTPException(
            status_code=400, detail="data parquet has invalid indices or timestamps"
        ) from exc
    if not all(math.isfinite(value) for value in ts_col):
        raise HTTPException(
            status_code=400, detail="data parquet has non-finite timestamps"
        )
    n_rows = table.num_rows

    persistent_by_ep: dict[int, list[dict[str, Any]]] = {}
    events_by_ep_ts: dict[int, dict[float, list[dict[str, Any]]]] = {}

    n_persistent_total = 0
    n_event_total = 0
    atoms_used_by_ep: dict[int, list[dict[str, Any]]] = {}

    unique_eps = sorted(set(episode_col))
    for ep_idx in unique_eps:
        atoms = atoms_by_ep.get(int(ep_idx))
        if atoms is None:
            atoms = _extract_existing_atoms_from_table(table, int(ep_idx))
        atoms_used_by_ep[int(ep_idx)] = atoms
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
        atoms_used_by_ep,
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


EXPORT_MARKER_NAME = ".levi-export.json"


def _is_levi_export(path: Path) -> bool:
    """True only for a directory carrying the marker this function itself
    writes on a successful export — the one case where re-exporting into an
    already-existing directory in place is safe. Never treat a directory as
    reusable just because it happens to already exist."""
    return (path / EXPORT_MARKER_NAME).is_file()


def _export_source(path: Path) -> str | None:
    """``source_root`` recorded in an export's marker, or None if unmarked."""
    try:
        return json.loads((path / EXPORT_MARKER_NAME).read_text()).get("source_root")
    except (OSError, ValueError):
        return None


def _do_export(
    state: DatasetState, output_dir: str | None, copy_videos: bool
) -> dict[str, Any]:
    if (state.root / "meta/levi_view.json").is_file():
        raise HTTPException(
            409,
            "This is a browsing view of a raw capture, not a dataset to "
            "export. Convert the capture in the Workbench instead — its "
            "annotations carry over to the converted dataset.",
        )
    for folder in ("meta", "data", "videos"):
        for entry in (state.root / folder).rglob("*"):
            if entry.is_symlink():
                inside(entry, state.root)
    reserved = False
    if output_dir:
        out_root = inside(output_dir)
    else:
        EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
        # Deterministic, not random: the same source dataset always resolves
        # to the same export directory, so repeated "导出标注数据集" clicks
        # update one dataset copy in place instead of piling up abandoned
        # full copies (videos + parquet + meta) on every click. Only if that
        # name is already owned by a *different* source does it get a
        # timestamp suffix.
        name = dataset_display_slug(state.repo_id, state.local_path)
        out_root = EXPORT_ROOT / f"{name}_annotated"
        if out_root.exists() and _export_source(out_root) != str(state.root):
            prefix = f"{name}_annotated_"
            stamp = naming.timestamp_id(EXPORT_ROOT, prefix=prefix, create_dir=True)
            out_root = EXPORT_ROOT / f"{prefix}{stamp}"
            reserved = True

    if out_root.is_relative_to(state.root):
        raise HTTPException(
            409, "Export must use a new directory outside the source dataset"
        )
    reuse = out_root.exists() and not reserved
    if reuse and not _is_levi_export(out_root):
        raise HTTPException(
            409,
            "Export directory already exists and wasn't created by a LEVI "
            "export — refusing to overwrite it",
        )
    out_root.mkdir(parents=True, exist_ok=True)
    created_at = (
        json.loads((out_root / EXPORT_MARKER_NAME).read_text())["created_at"]
        if reuse
        else pd.Timestamp.utcnow().isoformat()
    )

    # Copy meta/ (always refreshed on every export — cheap relative to video,
    # and keeps the feature/tool declarations authoritative even on reuse).
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

    # Human outcome labels travel with the export as dataset metadata
    # (``levi_outcome`` + ``levi_outcome_source``), where RECAP and the
    # viewer read them. v2 only: v3 episode metadata is parquet.
    labels = outcomes.read_labels(state.annotations_dir)
    episodes_path = dst_meta / "episodes.jsonl"
    if labels and episodes_path.exists():
        rows = [
            json.loads(line)
            for line in episodes_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        for row in rows:
            label = labels.get(int(row.get("episode_index", -1)))
            if label:
                row["levi_outcome"] = label["outcome"]
                row["levi_outcome_source"] = "human"
        episodes_path.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
            encoding="utf-8",
        )

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

    # Force a fresh full resync right before materializing — other LEVI
    # processes sharing this workspace (a collaborator's own instance, or
    # this same person on another port) may have saved episodes this
    # process's cache never saw, and the export must reflect every one of
    # them, not just what happened to already be cached.
    _reload_annotations_from_disk(state)
    atoms_by_ep = {ep: ann.atoms for ep, ann in state.annotations.items()}

    n_persistent = 0
    n_events = 0
    atoms_used: dict[int, list[dict[str, Any]]] = {}
    for src_path in data_files:
        rel_path = src_path.relative_to(state.root)
        dst_path = out_root / rel_path
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        table = pq.read_table(src_path)
        new_table, np_n, ne_n, per_ep_atoms = _materialize_table(table, atoms_by_ep)
        n_persistent += np_n
        n_events += ne_n
        atoms_used.update(per_ep_atoms)
        pq.write_table(new_table, dst_path)

    # Write-only per-episode language annotation mirror + manifest — a
    # portability/audit artifact with the exact same "one file per episode"
    # convention SAM3 already uses (``annotations/sam3/masks/episode-*``).
    # LEVI never reads this back: reload correctness already comes from the
    # ``language_persistent``/``language_events`` columns just baked into
    # data/*.parquet above (``get_episode_atoms``'s parquet-column fallback),
    # so there is exactly one source of truth and nothing here can drift out
    # of sync with it. Includes LEVI-only fields (e.g. ``to``) that the
    # exported struct itself deliberately omits.
    lang_dir = out_root / "annotations" / "language"
    if lang_dir.exists():
        shutil.rmtree(lang_dir)
    annotated_eps = {ep: atoms for ep, atoms in atoms_used.items() if atoms}
    if annotated_eps:
        lang_dir.mkdir(parents=True, exist_ok=True)
        manifest: dict[str, Any] = {
            "version": 1,
            "source_root": str(state.root),
            "episodes": {},
        }
        for ep_idx in sorted(annotated_eps):
            ep_atoms = annotated_eps[ep_idx]
            rel = f"episode_{ep_idx:06d}.json"
            (lang_dir / rel).write_text(
                json.dumps({"episode_index": ep_idx, "atoms": ep_atoms}, indent=2)
            )
            n_persistent_ep = sum(
                1
                for a in ep_atoms
                if column_for_style(a.get("style")) == LANGUAGE_PERSISTENT
            )
            manifest["episodes"][str(ep_idx)] = {
                "path": rel,
                "persistent_count": n_persistent_ep,
                "event_count": len(ep_atoms) - n_persistent_ep,
            }
        (lang_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    # Carry over the video shards so the export is self-contained. We
    # materialize *real* files (hardlink where the filesystem allows it, else
    # copy) rather than symlinking the source tree: a symlinked ``videos/``
    # breaks as soon as the folder is moved and is not uploaded by
    # ``HfApi.upload_folder``, which is exactly the "downloaded dataset isn't
    # usable" problem. ``copy_videos=True`` forces a full byte copy (used by
    # the push-to-hub path, where the upload reads the bytes anyway).
    #
    # Skipped entirely once ``dst_videos`` already exists: source captures
    # are immutable (see CLAUDE.md — "Preserve source captures"), so a
    # reused export directory's videos never need refreshing, and video is
    # the dominant cost this whole reuse scheme exists to avoid repeating.
    src_videos = state.root / "videos"
    dst_videos = out_root / "videos"
    if src_videos.exists() and not dst_videos.exists():
        _materialize_tree(src_videos, dst_videos, force_copy=copy_videos)

    # Carry the current object sidecar into the exported dataset without
    # changing the source tree.  The sidecar is optional, so language-only
    # exports remain backwards compatible.
    object_store = _sidecar(state)
    object_revision = object_store.current_revision()
    if object_revision:
        dst_sidecar = out_root / "annotations" / "sam3"
        _copy_object_sidecar(object_store, dst_sidecar)

    (out_root / EXPORT_MARKER_NAME).write_text(
        json.dumps(
            {
                "source_root": str(state.root),
                "repo_id": state.repo_id,
                "revision": state.revision,
                "created_at": created_at,
                "updated_at": pd.Timestamp.utcnow().isoformat(),
            },
            indent=2,
        )
    )

    return {
        "output_dir": str(out_root),
        "persistent_rows": n_persistent,
        "event_rows": n_events,
        "object_annotation_revision": object_revision,
        "reused_existing_export": reuse,
    }


# --- FastAPI app --------------------------------------------------------------

app = FastAPI(title="LeRobot dataset visualizer — annotation backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def sam3_request_credentials(request: Request, call_next):
    """Keep direct and mounted backend deployments on the same auth path."""
    cookie = request.cookies.get("hf_access_token")
    authorization = request.headers.get("authorization", "")
    bearer = (
        authorization[7:].strip()
        if authorization.lower().startswith("bearer ")
        else None
    )
    context = hub_token.set(cookie or bearer)
    try:
        return await call_next(request)
    finally:
        hub_token.reset(context)


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


@app.get("/api/episodes/annotation-summary")
def episode_annotation_summary(
    repo_id: str | None = None,
    revision: str | None = None,
    local_path: str | None = None,
) -> JSONResponse:
    """Per-episode language/vision annotation presence for the sidebar's
    annotated-vs-unannotated indicator. Language reuses the exact same
    parquet-column fallback as ``get_episode_atoms`` (batched by shared v3
    file so a multi-episode chunk is read once, not once per episode) and
    caches results into ``state.annotations`` as a side effect, same as that
    endpoint. Vision only stats the SAM3 sidecar's per-episode mask
    directory — cheap, and avoids ``SidecarStore.read_annotations`` re-globbing
    + re-reading the whole revision once per episode.
    """
    state = _ensure_state(
        DatasetRef(repo_id=repo_id, revision=revision, local_path=local_path)
    )
    episode_indices = sorted(
        int(i) for i in state.episodes_df["episode_index"].tolist()
    )

    language: dict[str, bool] = {}
    by_path: dict[Path, list[int]] = {}
    for ep in episode_indices:
        ann = _lookup_episode_annotations(state, ep)
        if ann is not None:
            language[str(ep)] = bool(ann.atoms)
            continue
        path = _episode_data_path(state, ep)
        if path is None:
            language[str(ep)] = False
            continue
        by_path.setdefault(path, []).append(ep)

    for path, eps in by_path.items():
        table: pa.Table | None = None
        try:
            schema = pq.read_schema(path)
            columns = ["episode_index"]
            if "timestamp" in schema.names:
                columns.append("timestamp")
            if LANGUAGE_PERSISTENT in schema.names:
                columns.append(LANGUAGE_PERSISTENT)
            if LANGUAGE_EVENTS in schema.names:
                columns.append(LANGUAGE_EVENTS)
            if LANGUAGE_PERSISTENT in columns or LANGUAGE_EVENTS in columns:
                table = pq.read_table(path, columns=columns)
        except Exception as e:  # noqa: BLE001
            logger.warning("annotation summary read failed for %s: %s", path, e)
        for ep in eps:
            atoms = _extract_existing_atoms_from_table(table, ep) if table else []
            if atoms:
                state.annotations[ep] = EpisodeAnnotations(atoms=atoms)
            language[str(ep)] = bool(atoms)

    vision: dict[str, bool] = {}
    store = _sidecar(state)
    revision_id = store.current_revision()
    masks_root = store.revision_path(revision_id) / "masks" if revision_id else None
    for ep in episode_indices:
        ep_dir = masks_root / f"episode-{ep:06d}" if masks_root else None
        vision[str(ep)] = bool(ep_dir and ep_dir.is_dir() and any(ep_dir.iterdir()))

    return JSONResponse({"language": language, "vision": vision})


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
    ann = _lookup_episode_annotations(state, episode_index)
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
    path = _write_episode_annotations(state, episode_index, atoms)
    return JSONResponse({"ok": True, "saved": len(atoms), "path": str(path)})


@app.delete("/api/episodes/{episode_index}/atoms")
def delete_episode_atoms(
    episode_index: int,
    repo_id: str | None = None,
    revision: str | None = None,
    local_path: str | None = None,
) -> JSONResponse:
    """Delete an episode's annotation file entirely — distinct from saving
    an empty atoms list (which still records "reviewed, nothing to
    annotate"). Reverts the episode to its pristine, never-annotated state.
    """
    state = _ensure_state(
        DatasetRef(repo_id=repo_id, revision=revision, local_path=local_path)
    )
    existed = _delete_episode_annotations(state, episode_index)
    return JSONResponse({"ok": True, "deleted": existed})


@app.get("/api/episodes/outcomes")
def get_outcome_labels(
    repo_id: str | None = None,
    revision: str | None = None,
    local_path: str | None = None,
) -> JSONResponse:
    """Human success/failure labels; they override ``levi_outcome``."""
    state = _ensure_state(
        DatasetRef(repo_id=repo_id, revision=revision, local_path=local_path)
    )
    labels = outcomes.read_labels(state.annotations_dir)
    return JSONResponse({"labels": {str(k): v for k, v in labels.items()}})


@app.post("/api/episodes/{episode_index}/outcome")
def set_outcome_label(episode_index: int, payload: OutcomePayload) -> JSONResponse:
    state = _ensure_state(
        DatasetRef(repo_id=payload.repo_id, local_path=payload.local_path)
    )
    known = {int(i) for i in state.episodes_df["episode_index"].tolist()}
    if episode_index not in known:
        raise HTTPException(status_code=404, detail="Unknown episode")
    value = outcomes.write_label(state.annotations_dir, episode_index, payload.outcome)
    return JSONResponse({"ok": True, "label": value})


def _prepare_sam3_media(state: DatasetState) -> None:
    """Ensure a Hub-backed worker has video assets beside its metadata."""
    if not state.repo_id or state.local_path:
        return
    if (
        os.environ.get("LEVI_SAM3_DOWNLOAD_VIDEOS", "1").lower()
        not in _SAM3_ENABLED_VALUES
    ):
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


def _is_sam3_checkpoint(path: Path) -> bool:
    """Treat only a non-empty regular file as a usable checkpoint."""
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _read_sam3_download() -> dict[str, Any]:
    path = _sam3_checkpoint_dir() / SAM3_PROGRESS_FILENAME
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _safe_sam3_download_message(value: object) -> str:
    """Keep signed Hub URLs and raw credentials out of persisted status."""
    return re.sub(
        r"(?i)(token|authorization|x-amz-signature|x-amz-credential)=[^&\s]+",
        r"\1=<redacted>",
        str(value),
    )


def _write_sam3_download(
    phase: str,
    *,
    bytes_downloaded: int = 0,
    total_bytes: int | None = None,
    percent: float | None = None,
    path: Path | None = None,
    message: str | None = None,
) -> None:
    """Persist the status consumed by the browser while a download runs."""
    payload: dict[str, Any] = {
        "phase": phase,
        "repo_id": SAM3_MODEL_REPO,
        "filename": SAM3_MODEL_FILENAME,
        "revision": SAM3_MODEL_REVISION,
        "bytes": max(0, int(bytes_downloaded)),
        "total_bytes": total_bytes,
        "percent": percent,
        "path": str(path or (_sam3_checkpoint_dir() / SAM3_MODEL_FILENAME)),
        "updated_at": time.time(),
    }
    if message:
        payload["message"] = _safe_sam3_download_message(message)
    try:
        atomic(_sam3_checkpoint_dir() / SAM3_PROGRESS_FILENAME, payload)
    except OSError:
        # Telemetry must never turn a valid Hub download into a failed one.
        return


def _sam3_incomplete_bytes(root: Path, target: Path) -> int:
    """Return the largest partial file size visible in the local download dir."""
    current = 0
    try:
        for path in root.rglob("*.incomplete"):
            if path.is_file():
                current = max(current, path.stat().st_size)
        if target.is_file():
            current = max(current, target.stat().st_size)
    except OSError:
        pass
    return current


def _sam3_remote_checkpoint_size(active_token: str) -> int | None:
    """Read Hub metadata for a determinate progress bar without model bytes."""
    try:
        api = HfApi(token=active_token)
        try:
            info = api.model_info(
                SAM3_MODEL_REPO,
                revision=SAM3_MODEL_REVISION,
                files_metadata=True,
            )
        except TypeError:
            info = api.model_info(SAM3_MODEL_REPO, revision=SAM3_MODEL_REVISION)
        siblings = (
            info.get("siblings", [])
            if isinstance(info, dict)
            else getattr(info, "siblings", [])
        ) or []
        for sibling in siblings:
            name = (
                sibling.get("rfilename")
                if isinstance(sibling, dict)
                else getattr(sibling, "rfilename", None)
            )
            size = (
                sibling.get("size")
                if isinstance(sibling, dict)
                else getattr(sibling, "size", None)
            )
            if name == SAM3_MODEL_FILENAME and isinstance(size, int):
                return size
    except Exception as exc:  # noqa: BLE001
        # Metadata is optional; the actual download remains authoritative.
        logger.debug("SAM3 checkpoint metadata unavailable: %s", exc)
    return None


def _sam3_download_thread_alive(key: str) -> bool:
    thread = _SAM3_DOWNLOAD_THREADS.get(key)
    return bool(thread and thread.is_alive())


def _download_sam3_checkpoint(active_token: str, target: Path) -> None:
    """Download one checkpoint into the configured workspace path."""
    key = str(target.expanduser().resolve())
    target_dir = target.parent
    stop = threading.Event()
    monitor_thread: threading.Thread | None = None
    temporary: Path | None = None
    total_bytes: int | None = None
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        total_bytes = _sam3_remote_checkpoint_size(active_token)
        _write_sam3_download(
            "downloading",
            total_bytes=total_bytes,
            percent=0.0 if total_bytes else None,
            path=target,
            message=f"Downloading {SAM3_MODEL_REPO}/{SAM3_MODEL_FILENAME}",
        )

        def monitor() -> None:
            while not stop.is_set():
                current = _sam3_incomplete_bytes(target_dir, target)
                percent = (
                    min(100.0, current * 100.0 / total_bytes) if total_bytes else None
                )
                _write_sam3_download(
                    "downloading",
                    bytes_downloaded=current,
                    total_bytes=total_bytes,
                    percent=percent,
                    path=target,
                )
                stop.wait(0.5)

        monitor_thread = threading.Thread(
            target=monitor,
            name="sam3-checkpoint-progress",
            daemon=True,
        )
        monitor_thread.start()
        downloaded = Path(
            hf_hub_download(
                repo_id=SAM3_MODEL_REPO,
                filename=SAM3_MODEL_FILENAME,
                revision=SAM3_MODEL_REVISION,
                token=active_token,
                local_dir=str(target_dir),
            )
        )
        if not _is_sam3_checkpoint(downloaded):
            raise FileNotFoundError(
                f"Hugging Face returned no usable checkpoint at {downloaded}"
            )
        # ``local_dir`` normally returns target directly. Keep an atomic copy
        # fallback for Hub/cache versions that return a different local path.
        if downloaded.resolve() != target.resolve():
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            with downloaded.open("rb") as source, temporary.open("wb") as dest:
                shutil.copyfileobj(source, dest, length=8 * 1024 * 1024)
                dest.flush()
                os.fsync(dest.fileno())
            os.replace(temporary, target)
            temporary = None
        size = target.stat().st_size
        _write_sam3_download(
            "ready",
            bytes_downloaded=size,
            total_bytes=total_bytes or size,
            percent=100.0,
            path=target,
        )
    except Exception as exc:  # noqa: BLE001
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
        _write_sam3_download(
            "error",
            bytes_downloaded=_sam3_incomplete_bytes(target_dir, target),
            total_bytes=total_bytes,
            percent=None,
            path=target,
            message=str(exc),
        )
    finally:
        stop.set()
        if monitor_thread is not None:
            monitor_thread.join(timeout=2)
        with _SAM3_DOWNLOAD_LOCK:
            current = _SAM3_DOWNLOAD_THREADS.get(key)
            if current is threading.current_thread():
                _SAM3_DOWNLOAD_THREADS.pop(key, None)


def _start_sam3_checkpoint_download(active_token: str) -> bool:
    """Start a resumable background download; return whether a new thread ran."""
    target = _sam3_checkpoint_path()
    key = str(target.expanduser().resolve())
    with _SAM3_DOWNLOAD_LOCK:
        if _is_sam3_checkpoint(target):
            return False
        if _sam3_download_thread_alive(key):
            return False
        _write_sam3_download("downloading", path=target, percent=0.0)
        thread = threading.Thread(
            target=_download_sam3_checkpoint,
            args=(active_token, target),
            name="sam3-checkpoint-download",
            daemon=True,
        )
        _SAM3_DOWNLOAD_THREADS[key] = thread
        thread.start()
    return True


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
    checkpoint_ready = _is_sam3_checkpoint(checkpoint)
    size = 0
    if checkpoint_ready:
        try:
            size = checkpoint.stat().st_size
        except OSError:
            checkpoint_ready = False
    if checkpoint_ready:
        try:
            recorded_total = max(0, int(download.get("total_bytes") or 0))
        except (TypeError, ValueError):
            recorded_total = 0
        download = {
            **download,
            "phase": "ready",
            "bytes": size,
            "total_bytes": max(size, recorded_total) or size,
            "percent": 100.0,
            "path": str(checkpoint),
        }
    elif not download or download.get("phase") == "ready":
        download = {
            **download,
            "phase": "idle",
            "bytes": 0,
            "total_bytes": download.get("total_bytes") if download else None,
            "percent": 0.0,
            "path": str(checkpoint),
        }
    else:
        download.setdefault("path", str(checkpoint))
    download_in_progress = download.get("phase") == "downloading"
    if download_in_progress:
        key = str(checkpoint.expanduser().resolve())
        if not _sam3_download_thread_alive(key):
            # A daemon thread cannot survive a service restart. Do not leave
            # the UI disabled for a stale progress record; expose retry now.
            download = {
                **download,
                "phase": "error",
                "percent": None,
                "message": "Checkpoint download stopped; retry from the annotation page.",
            }
            download_in_progress = False
    worker_project = (
        Path(__file__).resolve().parents[1] / "integrations" / "sam3" / "pyproject.toml"
    )
    worker_python = Path(
        os.environ.get(
            "LEVI_SAM3_WORKER_PYTHON",
            str(worker_project.parent / ".venv/bin/python"),
        )
    ).expanduser()
    auth = _sam3_auth_status()
    checkpoint_override = bool(os.environ.get("LEVI_SAM3_CHECKPOINT"))
    download_available = not checkpoint_override
    if not _sam3_enabled():
        message = "SAM3 is disabled; set LEVI_SAM3_ENABLED=1 to enable it."
    elif checkpoint_ready:
        message = "SAM3 checkpoint is ready in the active LEVI workspace."
    elif checkpoint_override:
        message = "LEVI_SAM3_CHECKPOINT is configured but the file is not available."
    elif not auth.get("authenticated"):
        message = "Sign in to Hugging Face to download the SAM3 checkpoint."
    elif download_in_progress:
        message = "SAM3 checkpoint download is in progress."
    else:
        message = "SAM3 checkpoint is not cached; start the download from the annotation page."
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
        "checkpoint_download_available": download_available,
        "checkpoint_download_requires_auth": not bool(auth.get("authenticated")),
        "checkpoint_download_in_progress": download_in_progress,
        "download": download,
        "hf_auth": auth,
        "message": message,
    }


@app.get("/api/sam3/status")
def sam3_status() -> JSONResponse:
    """Report model/account/download state without importing Torch or probing CUDA."""
    return JSONResponse(_sam3_status_payload())


@app.get("/api/sam3/capabilities")
def sam3_capabilities() -> JSONResponse:
    """Backward-compatible alias for the global SAM3 status payload."""
    return JSONResponse(_sam3_status_payload())


@app.post("/api/sam3/checkpoint/download")
def sam3_checkpoint_download() -> JSONResponse:
    """Start or resume the global SAM3 checkpoint download.

    The browser session token is read from the request-scoped context and is
    passed only to the background Hub call. It is never persisted in the
    progress file, job plan or response.
    """
    if not _sam3_enabled():
        raise HTTPException(
            503,
            "SAM3 is disabled; set LEVI_SAM3_ENABLED=1 to enable it",
        )
    if os.environ.get("LEVI_SAM3_CHECKPOINT"):
        if _is_sam3_checkpoint(_sam3_checkpoint_path()):
            return JSONResponse({**_sam3_status_payload(), "download_started": False})
        raise HTTPException(
            409,
            "LEVI_SAM3_CHECKPOINT points to a missing local file; place the checkpoint there or unset the override",
        )
    active_token = token()
    if not active_token:
        raise HTTPException(
            401,
            "Sign in to Hugging Face before downloading the SAM3 checkpoint",
        )
    auth = _sam3_auth_status()
    if not auth.get("authenticated"):
        raise HTTPException(
            401,
            "The current Hugging Face session cannot access the configured SAM3 model",
        )
    started = _start_sam3_checkpoint_download(active_token)
    status = _sam3_status_payload()
    return JSONResponse(
        {**status, "download_started": started},
        status_code=202
        if started or status["download"].get("phase") == "downloading"
        else 200,
    )


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
    prompts = list(
        dict.fromkeys(item.strip() for item in request.prompts if item.strip())
    )
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
    store = _sidecar(state)
    if request.plan_id:
        plan_path, plan_payload = _reuse_sam3_plan(state, store, request)
    else:
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
    if not _is_sam3_checkpoint(_sam3_checkpoint_path()):
        raise HTTPException(
            409,
            "SAM3 checkpoint is not ready; download it from the annotation page before running",
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
    job_id = naming.timestamp_id(store.root / "staging" / "jobs", ".json")
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
