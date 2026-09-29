"""Fast instance segmentation endpoints (``/api/segmentation/*``).

A distilled student (RF-DETR-Seg + ByteTrack, ``integrations/segmentation``)
follows the episode player live, labels whole datasets offline, and is
distilled from the SAM3 teacher inside LEVI. See docs/SEGMENTATION.md.

Everything that writes object annotations goes through
``SidecarStore.publish_merged`` inside the dataset's annotation transaction
(``editor``), so a run on some episodes replaces only those episodes'
cameras and keeps every other episode's objects.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from levi.agent.legacy import editor
from levi.annotations import ObjectAnnotation
from levi.segmentation import jobs as seg_jobs
from levi.segmentation import live as seg_live
from levi.segmentation import media, models

router = APIRouter()

_ANNOTATION_FIELDS = set(ObjectAnnotation.model_fields)


def _app():
    from backend import app

    return app


def _call(call):
    try:
        return call()
    except seg_jobs.SegError as exc:
        raise HTTPException(exc.status, exc.detail) from exc


def _state(repo_id: str | None, revision: str | None = None, local_path: str | None = None):
    app = _app()
    return app._ensure_state(app.DatasetRef(repo_id=repo_id, revision=revision, local_path=local_path))


def _dataset_name(state) -> str:
    return state.display_slug


def _publisher(state) -> seg_jobs.Publisher:
    """Publish worker rows into this dataset's sidecar, replacing only the
    (episode, camera) pairs the run produced."""

    def publish(rows: list[dict[str, Any]], pairs: set[tuple[int, str]], model: dict[str, Any]):
        annotations = [
            ObjectAnnotation.model_validate({k: v for k, v in row.items() if k in _ANNOTATION_FIELDS})
            for row in rows
        ]
        return _app()._sidecar(state).publish_merged(annotations, pairs, model=model)

    return publish


def _episode_indices(state) -> list[int]:
    frame = state.episodes_df
    if frame is None or "episode_index" not in frame:
        return []
    return sorted(int(v) for v in frame["episode_index"].tolist())


def _cameras(state, wanted: list[str] | None) -> list[str]:
    available = media.camera_keys(state.info)
    if not available:
        raise HTTPException(400, "This dataset has no video cameras")
    if not wanted:
        return available
    unknown = [c for c in wanted if c not in available]
    if unknown:
        raise HTTPException(400, f"Unknown camera(s): {', '.join(unknown)}")
    return list(dict.fromkeys(wanted))


def _videos(state, episodes: list[int], cameras: list[str]) -> list[dict[str, Any]]:
    known = set(_episode_indices(state))
    missing = [e for e in episodes if e not in known]
    if missing:
        raise HTTPException(400, f"Unknown episode(s): {', '.join(map(str, missing[:10]))}")
    _app()._prepare_sam3_media(state)
    items = []
    for episode in episodes:
        for camera in cameras:
            try:
                items.append(media.resolve(state.root, state.info, episode, camera).plan())
            except FileNotFoundError as exc:
                raise HTTPException(404, str(exc)) from exc
    return items


def _teacher_status() -> dict[str, Any]:
    app = _app()
    python = seg_jobs.teacher_python()
    checkpoint = app._sam3_checkpoint_path()
    ready = python.is_file() and app._is_sam3_checkpoint(checkpoint)
    reason = None
    if not python.is_file():
        reason = f"SAM3 worker environment not found at {python}"
    elif not app._is_sam3_checkpoint(checkpoint):
        reason = "SAM3 checkpoint is not downloaded"
    return {"ready": ready, "reason": reason, "enabled": app._sam3_enabled()}


# ---------------------------------------------------------------- requests


class Ref(BaseModel):
    repo_id: str | None = None
    revision: str | None = None
    local_path: str | None = None


class LabelRequest(Ref):
    model: str | None = Field(default=None, max_length=96)
    provider: Literal["student", "fake"] = "student"
    episodes: list[int] | None = None
    cameras: list[str] | None = None
    batch: int = Field(default=8, ge=1, le=64)

    @field_validator("episodes")
    @classmethod
    def _episodes(cls, value):
        if value is not None and (not value or any(v < 0 for v in value)):
            raise ValueError("episodes must be non-negative (omit for every episode)")
        return None if value is None else sorted(set(value))


class DistilSource(Ref):
    train: list[int] = Field(default_factory=list)
    valid: list[int] = Field(default_factory=list)
    test: list[int] = Field(default_factory=list)
    stride: int | None = Field(default=None, ge=1, le=100)


class DistilRequest(Ref):
    """``repo_id`` is the dataset the model is for; ``sources`` may add
    episodes of other datasets (for example teleoperated demonstrations with
    a human hand in view)."""

    name: str = Field(min_length=1, max_length=96)
    concepts: list[str] = Field(min_length=1, max_length=32)
    provider: Literal["student", "fake"] = "student"
    sources: list[DistilSource] = Field(min_length=1, max_length=16)
    cameras: list[str] | None = None
    architecture: Literal["rf-detr-seg-nano", "rf-detr-seg-small", "rf-detr-seg-medium"] = "rf-detr-seg-small"
    epochs: int = Field(default=20, ge=1, le=200)
    batch_size: int = Field(default=8, ge=1, le=64)
    stride: int = Field(default=3, ge=1, le=100)
    confidence: float = Field(default=0.4, gt=0, lt=1)
    threshold: float = Field(default=0.5, gt=0, lt=1)
    note: str | None = Field(default=None, max_length=2000)

    @field_validator("concepts")
    @classmethod
    def _concepts(cls, value):
        cleaned = [c.strip() for c in value]
        if any(not c for c in cleaned) or len(set(cleaned)) != len(cleaned):
            raise ValueError("concepts must be distinct, non-empty names")
        return cleaned


class LiveRequest(Ref):
    episode_index: int = Field(ge=0)
    model: str | None = Field(default=None, max_length=96)
    provider: Literal["student", "fake"] = "student"
    cameras: list[str] | None = None
    save: bool = True
    lead_frames: int = Field(default=1, ge=0, le=10)


class ClockRequest(BaseModel):
    playing: bool
    time: float = Field(ge=0)
    rate: float = Field(default=1.0, gt=0, le=16)
    # Unix seconds when the player read ``time``: the worker removes the
    # request's transport delay (a proxy can add hundreds of milliseconds).
    sent_at: float | None = None
    # Each camera's own episode-local time (cameras of one player drift).
    cameras: dict[str, float] | None = None


# ---------------------------------------------------------------- status


def _collected_jobs(state) -> list[dict[str, Any]]:
    name = _dataset_name(state)
    publish = _publisher(state)
    return [seg_jobs.public(seg_jobs.collect(job, publish)) for job in seg_jobs.jobs(name)][-20:]


def _live_public(state) -> list[dict[str, Any]]:
    publish = _publisher(state)
    out = []
    for session in seg_live.sessions(_dataset_name(state)):
        seg_live.finish(session, publish)
        out.append(session.public())
    seg_live.forget_finished()
    return out


@router.get("/api/segmentation/status")
@editor(internal=True)
def segmentation_status(
    repo_id: str | None = None,
    revision: str | None = None,
    local_path: str | None = None,
) -> JSONResponse:
    state = _state(repo_id, revision, local_path)
    name = _dataset_name(state)
    return JSONResponse(
        {
            "worker": seg_jobs.worker_state(),
            "teacher": _teacher_status(),
            "gpu_lock": bool(seg_jobs.gpu_lock_file()),
            "models": models.listing(name),
            "cameras": media.camera_keys(state.info),
            "fps": state.info.get("fps"),
            "episodes": len(_episode_indices(state)),
            "jobs": _collected_jobs(state),
            "live": _live_public(state),
        }
    )


@router.get("/api/segmentation/models")
def segmentation_models(repo_id: str | None = None) -> JSONResponse:
    name = _dataset_name(_state(repo_id)) if repo_id else None
    return JSONResponse({"models": models.listing(name)})


@router.delete("/api/segmentation/models/{name}")
def segmentation_delete_model(name: str) -> JSONResponse:
    try:
        models.delete(name)
    except KeyError as exc:
        raise HTTPException(404, str(exc.args[0])) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return JSONResponse({"ok": True, "models": models.listing()})


# ---------------------------------------------------------------- jobs


@router.post("/api/segmentation/label")
def segmentation_label(request: LabelRequest) -> JSONResponse:
    """Label episodes (every one by default) with a student, in one job."""
    state = _state(request.repo_id, request.revision, request.local_path)
    if request.provider == "student":
        if not request.model:
            raise HTTPException(400, "Choose a student model")
        try:
            spec = models.worker_spec(request.model)
        except KeyError as exc:
            raise HTTPException(404, str(exc.args[0])) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        warnings = _call(lambda: seg_jobs.check_gpu("label"))
    else:
        spec = {"name": request.model or "fake", "provider": "fake"}
        warnings = []
    episodes = request.episodes or _episode_indices(state)
    if not episodes:
        raise HTTPException(400, "This dataset has no episodes")
    cameras = _cameras(state, request.cameras)
    items = _videos(state, episodes, cameras)
    plan = {
        "schema": "levi.segmentation.label.v1",
        "model": spec,
        "items": items,
        "batch": request.batch,
    }
    job = _call(
        lambda: seg_jobs.start(
            _dataset_name(state),
            "label",
            request.provider,
            plan,
            request={"episodes": len(episodes), "cameras": cameras, "model": spec.get("name")},
            model=spec.get("name"),
            warnings=warnings,
        )
    )
    return JSONResponse({"ok": True, **seg_jobs.public(job)}, status_code=202)


@router.post("/api/segmentation/distil")
def segmentation_distil(request: DistilRequest) -> JSONResponse:
    """Distil a student: SAM3 pseudo-labels -> RF-DETR-Seg -> held-out
    scores against the teacher -> a listed model with its manifest."""
    state = _state(request.repo_id, request.revision, request.local_path)
    if not models.valid_name(request.name):
        raise HTTPException(400, "Model names use letters, digits, '.', '_' or '-'")
    directory = models.folder(request.name)
    if (directory / models.MANIFEST).exists():
        raise HTTPException(409, f"A model named {request.name} already exists")
    if request.provider == "student":
        teacher = _teacher_status()
        if not teacher["ready"]:
            raise HTTPException(409, "The SAM3 teacher is not ready: " + str(teacher["reason"]))
        warnings = _call(lambda: seg_jobs.check_gpu("distil"))
    else:
        warnings = []
    items: list[dict[str, Any]] = []
    datasets: list[dict[str, Any]] = []
    for source in request.sources:
        source_state = (
            _state(source.repo_id, source.revision, source.local_path)
            if (source.repo_id or source.local_path)
            else state
        )
        cameras = _cameras(source_state, request.cameras) if request.cameras else media.camera_keys(source_state.info)
        split_of: dict[int, str] = {}
        for split in ("train", "valid", "test"):
            for episode in getattr(source, split):
                if episode in split_of:
                    raise HTTPException(400, f"Episode {episode} is in both {split_of[episode]} and {split}")
                split_of[episode] = split
        if not split_of:
            continue
        name = _dataset_name(source_state)
        for item in _videos(source_state, sorted(split_of), cameras):
            items.append(
                {
                    **item,
                    "split": split_of[item["episode_index"]],
                    "dataset": name,
                    **({"stride": source.stride} if source.stride else {}),
                }
            )
        datasets.append(
            {
                "name": name,
                "train": sorted(source.train),
                "valid": sorted(source.valid),
                "test": sorted(source.test),
                "cameras": cameras,
                "stride": source.stride or request.stride,
            }
        )
    splits = {item["split"] for item in items}
    if "train" not in splits:
        raise HTTPException(400, "Choose at least one training episode")
    if request.provider == "student" and "valid" not in splits:
        raise HTTPException(400, "Choose at least one validation episode")
    app = _app()
    plan = {
        "schema": "levi.segmentation.distil.v1",
        "concepts": request.concepts,
        "items": items,
        "teacher": {
            "python": str(seg_jobs.teacher_python()),
            "project": str(seg_jobs.SAM3_PROJECT),
            "stride": request.stride,
            "threshold": request.threshold,
            "env": {"LEVI_SAM3_CHECKPOINT_DIR": str(app._sam3_checkpoint_dir())},
        },
        "model": {
            "dir": str(directory),
            "architecture": request.architecture,
            "epochs": request.epochs,
            "batch_size": request.batch_size,
            "confidence": request.confidence,
        },
        "manifest": {
            "schema": models.SCHEMA,
            "name": request.name,
            "provider": "fake" if request.provider == "fake" else "student",
            "architecture": request.architecture,
            "concepts": request.concepts,
            "class_names": request.concepts,
            "confidence": request.confidence,
            "tracker": dict(models.DEFAULT_TRACKER),
            "teacher": {
                "name": "SAM3 image model (text prompts)",
                "checkpoint_repo": app.SAM3_MODEL_REPO,
                "checkpoint_file": app.SAM3_MODEL_FILENAME,
                "stride": request.stride,
                "threshold": request.threshold,
            },
            "datasets": datasets,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "licence": {
                **models.LICENCES,
                "model": "Apache-2.0",
            },
            "levi_commit": seg_jobs.levi_commit(),
            "note": request.note,
        },
    }
    job = _call(
        lambda: seg_jobs.start(
            _dataset_name(state),
            "distil",
            request.provider,
            plan,
            request={
                "name": request.name,
                "concepts": request.concepts,
                "episodes": {s: sum(1 for i in items if i["split"] == s) for s in ("train", "valid", "test")},
                "architecture": request.architecture,
                "epochs": request.epochs,
            },
            model=request.name,
            warnings=warnings,
        )
    )
    return JSONResponse({"ok": True, **seg_jobs.public(job)}, status_code=202)


@router.get("/api/segmentation/jobs/{job_id}")
@editor(internal=True)
def segmentation_job(
    job_id: str,
    repo_id: str | None = None,
    revision: str | None = None,
    local_path: str | None = None,
) -> JSONResponse:
    state = _state(repo_id, revision, local_path)
    job = _call(lambda: seg_jobs.find(_dataset_name(state), job_id))
    return JSONResponse(seg_jobs.public(seg_jobs.collect(job, _publisher(state))))


@router.post("/api/segmentation/jobs/{job_id}/cancel")
def segmentation_cancel(
    job_id: str,
    repo_id: str | None = None,
    revision: str | None = None,
    local_path: str | None = None,
) -> JSONResponse:
    state = _state(repo_id, revision, local_path)
    job = _call(lambda: seg_jobs.find(_dataset_name(state), job_id))
    return JSONResponse(seg_jobs.public(seg_jobs.cancel(job)))


# ---------------------------------------------------------------- live


def _session(state, session_id: str) -> seg_live.LiveSession:
    session = _call(lambda: seg_live.get(session_id))
    if session.dataset != _dataset_name(state):
        raise HTTPException(404, "Live segmentation session not found")
    return session


@router.post("/api/segmentation/live")
def segmentation_live_start(request: LiveRequest) -> JSONResponse:
    """Start a live overlay for one episode (all its cameras by default).
    Answers once the model is loaded, or with the reason it could not."""
    state = _state(request.repo_id, request.revision, request.local_path)
    cameras = _cameras(state, request.cameras)
    videos = _videos(state, [request.episode_index], cameras)
    session = _call(
        lambda: seg_live.start(
            _dataset_name(state),
            episode_videos=videos,
            episode_index=request.episode_index,
            model=request.model,
            provider=request.provider,
            save=request.save,
            lead_frames=request.lead_frames,
        )
    )
    seg_live.wait_ready(session, timeout=180)
    if session.state == "failed":
        raise HTTPException(503, session.error or "The live worker failed to start")
    return JSONResponse(session.public(), status_code=201)


@router.get("/api/segmentation/live/{session_id}")
@editor(internal=True)
def segmentation_live_get(
    session_id: str,
    repo_id: str | None = None,
    revision: str | None = None,
    local_path: str | None = None,
) -> JSONResponse:
    state = _state(repo_id, revision, local_path)
    session = _session(state, session_id)
    seg_live.finish(session, _publisher(state))
    return JSONResponse(session.public())


@router.post("/api/segmentation/live/{session_id}/clock")
def segmentation_live_clock(
    session_id: str,
    clock: ClockRequest,
    repo_id: str | None = None,
    revision: str | None = None,
    local_path: str | None = None,
) -> JSONResponse:
    session = _session(_state(repo_id, revision, local_path), session_id)
    if session.stopped_at is not None:
        raise HTTPException(409, "The live session has stopped")
    sent = session.send({"op": "clock", **clock.model_dump(exclude_none=True)})
    return JSONResponse({"ok": sent, "state": session.state})


@router.get("/api/segmentation/live/{session_id}/events")
def segmentation_live_events(
    session_id: str,
    request: Request,
    repo_id: str | None = None,
    revision: str | None = None,
    local_path: str | None = None,
    after: int = 0,
) -> StreamingResponse:
    """Server-sent events: ``result`` (one camera frame), ``stats``,
    ``ready``, ``stopped``, ``error``, ``closed``. A slow reader gets only
    the newest result per camera."""
    session = _session(_state(repo_id, revision, local_path), session_id)
    resume = request.headers.get("last-event-id")
    if resume and resume.isdigit():
        after = max(after, int(resume))
    return StreamingResponse(
        session.stream(after),
        media_type="text/event-stream",
        # no-transform: a compressing proxy (Next.js) must not buffer events.
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.post("/api/segmentation/live/{session_id}/stop")
@editor(internal=True)
def segmentation_live_stop(
    session_id: str,
    repo_id: str | None = None,
    revision: str | None = None,
    local_path: str | None = None,
) -> JSONResponse:
    """Stop a session and save what it showed (unless it was started with
    ``save: false``) as one revision that replaces only this episode's
    cameras."""
    state = _state(repo_id, revision, local_path)
    session = _session(state, session_id)
    session.stop()
    seg_live.finish(session, _publisher(state))
    return JSONResponse(session.public())


def stop_all() -> None:
    """Service shutdown: stop live sessions and running jobs' workers."""
    seg_live.stop_all()

