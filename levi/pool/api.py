"""``/api/levi/pool/*``: the training pool over HTTP, behind the service's
UI-token and same-origin middleware (writes: recipes, scans, exports)."""

import json
import os
import secrets
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from . import (
    cleanup,
    corrections,
    deletion,
    index,
    jobs,
    journal,
    recipe,
    remote,
    settings,
)
from .export import ExportOptions
from .recipe import Recipe
from .reset import profile as reset_profile
from .reset.schema import ResetOptions

Strings = Annotated[list[str] | None, Query()]
Outcome = Literal[
    "success",
    "failure",
    "robot_flag_success",
    "verified_success",
    "human_verified_success",
]

router = APIRouter(prefix="/api/levi/pool", tags=["Training pool"])


def _filters(
    category=None,
    source=None,
    task=None,
    search=None,
    format=None,
    outcome=None,
    policy=None,
    policy_model=None,
    policy_checkpoint=None,
    policy_method=None,
    robot=None,
    gripper=None,
    show_heldout=False,
    show_copies=False,
    show_archive=False,
    date_from=None,
    date_to=None,
) -> dict:
    return {
        "date_from": date_from,
        "date_to": date_to,
        "categories": category,
        "sources": source,
        "tasks": task,
        "search": search,
        "formats": format,
        "outcome": outcome,
        "policies": policy,
        "policy_models": policy_model,
        "policy_checkpoints": policy_checkpoint,
        "policy_methods": policy_method,
        "robots": robot,
        "grippers": gripper,
        "show_heldout": show_heldout,
        "show_copies": show_copies,
        "show_archive": show_archive,
    }


def _status_warnings() -> list[dict]:
    """Held-out setup problems, before any recipe is looked at."""
    if not settings.enabled() or not index.summary():
        return []
    try:
        return recipe.find_warnings(recipe.Recipe(name="status"), [])
    except (ValueError, OSError):
        return []


@router.get("/status")
def status():
    """Settings, the last scan's summary and the running jobs."""
    return {
        "enabled": settings.enabled(),
        "roots": [str(p) for p in settings.pool_roots()],
        "export_roots": [str(p) for p in settings.export_roots()],
        "heldout_lists": [str(p) for p in settings.heldout_files()],
        "heldout_disabled": settings.heldout_disabled(),
        "warnings": _status_warnings(),
        "disk": cleanup.disk(),
        "last_scan": index.summary() or None,
        "jobs": jobs.listing(10),
    }


@router.get("/sources")
def sources(category: str | None = None, show_archive: bool = False):
    return {"sources": index.sources(category, show_archive)}


@router.get("/tasks")
def tasks(
    category: Strings = None,
    source: Strings = None,
    search: str | None = None,
    format: Strings = None,
    outcome: Outcome | None = None,
    policy: Strings = None,
    policy_model: Strings = None,
    policy_checkpoint: Strings = None,
    policy_method: Strings = None,
    robot: Strings = None,
    gripper: Strings = None,
    date_from: str | None = None,
    date_to: str | None = None,
    show_heldout: bool = False,
    show_copies: bool = False,
    show_archive: bool = False,
):
    return {
        "tasks": index.tasks(
            **_filters(
                category=category,
                source=source,
                search=search,
                format=format,
                outcome=outcome,
                policy=policy,
                policy_model=policy_model,
                policy_checkpoint=policy_checkpoint,
                policy_method=policy_method,
                robot=robot,
                gripper=gripper,
                show_heldout=show_heldout,
                show_copies=show_copies,
                show_archive=show_archive,
                date_from=date_from,
                date_to=date_to,
            )
        )
    }


@router.get("/facets")
def facets(
    category: Strings = None,
    show_heldout: bool = False,
    show_copies: bool = False,
    show_archive: bool = False,
):
    """Counts for the page's facets, and what the visibility toggles hide."""
    return index.facets(
        categories=category,
        show_heldout=show_heldout,
        show_copies=show_copies,
        show_archive=show_archive,
    )


def _viewer_links(rows: list[dict]) -> None:
    """The LEVI episode viewer for rows whose source is in the catalog."""
    from .. import catalog

    names: dict[str, str | None] = {}
    for row in rows:
        path = row.get("source_path")
        if path not in names:
            try:
                names[path] = catalog.name_for_path(path)
            except (OSError, ValueError):
                names[path] = None
        name = names[path]
        row["catalog_id"] = f"local/{name}" if name else None
        index_ = row.get("episode_index")
        row["viewer"] = (
            (
                f"/local/{name}/episode_{index_}"
                if row.get("format") == "lerobot" and (index_ or 0) >= 0
                else f"/local/{name}"
            )
            if name
            else None
        )


@router.get("/episodes")
def episodes(
    category: Strings = None,
    source: Strings = None,
    task: Strings = None,
    search: str | None = None,
    format: Strings = None,
    outcome: Outcome | None = None,
    policy: Strings = None,
    policy_model: Strings = None,
    policy_checkpoint: Strings = None,
    policy_method: Strings = None,
    robot: Strings = None,
    gripper: Strings = None,
    date_from: str | None = None,
    date_to: str | None = None,
    show_heldout: bool = False,
    show_copies: bool = False,
    show_archive: bool = False,
    limit: Annotated[int, Query(ge=1, le=5000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    page = index.episodes(
        limit=limit,
        offset=offset,
        **_filters(
            category=category,
            source=source,
            task=task,
            search=search,
            format=format,
            outcome=outcome,
            policy=policy,
            policy_model=policy_model,
            policy_checkpoint=policy_checkpoint,
            policy_method=policy_method,
            robot=robot,
            gripper=gripper,
            show_heldout=show_heldout,
            show_copies=show_copies,
            show_archive=show_archive,
            date_from=date_from,
            date_to=date_to,
        ),
    )
    _viewer_links(page["episodes"])
    return page


@router.post("/scan")
def scan(rehash: bool = False):
    """Start a scan job; poll ``GET /api/levi/pool/jobs/{id}``."""
    job = jobs.plan_scan(rehash)
    return jobs.launch(job["id"])


@router.get("/jobs")
def job_list():
    return {"jobs": jobs.listing()}


@router.get("/jobs/{job_id}")
def job(job_id: str):
    try:
        return jobs.get(job_id)
    except KeyError:
        raise HTTPException(404, "Pool job not found") from None


@router.post("/jobs/{job_id}/cancel")
def job_cancel(job_id: str):
    """Stop a running job for good; an export's partial folder is removed.
    A job that already stopped (interrupted, failed) becomes cancelled."""
    try:
        return jobs.cancel(job_id)
    except KeyError:
        raise HTTPException(404, "Pool job not found") from None


@router.post("/jobs/{job_id}/resume")
def job_resume(job_id: str):
    """Continue an interrupted or failed job: an export from its journal (409
    with the reason when the unfinished output cannot be trusted: run it
    again), a push with rsync's kept files, a scan as a fresh one."""
    try:
        return jobs._brief(jobs.resume(job_id))
    except KeyError:
        raise HTTPException(404, "Pool job not found") from None
    except journal.ResumeRefused as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/jobs/{job_id}/rerun")
def job_rerun(job_id: str):
    """Plan the job again from its saved recipe and options and start it (an
    unfinished output of the old export is removed first)."""
    try:
        return jobs._brief(jobs.rerun(job_id))
    except KeyError:
        raise HTTPException(404, "Pool job not found") from None


@router.get("/jobs/{job_id}/log")
def job_log(job_id: str, kb: Annotated[int, Query(ge=1, le=2048)] = 64):
    """The last ``kb`` KB of the job's timestamped log."""
    try:
        text = jobs.log_tail(job_id, kb)
    except KeyError:
        raise HTTPException(404, "Pool job not found") from None
    return {"id": job_id, "text": text, "bytes": len(text.encode())}


@router.get("/jobs/{job_id}/error-report")
def job_error_report(job_id: str):
    """State, structured error, failed episodes and log tail: what to paste
    into a bug report."""
    try:
        return jobs.error_report(job_id)
    except KeyError:
        raise HTTPException(404, "Pool job not found") from None


@router.get("/jobs/{job_id}/delete-preview")
def job_delete_preview(job_id: str, files: bool = True):
    """What clearing (``files=false``) or deleting (``files=true``) this job
    would remove: the export directory with its size, episodes, format, time
    and pushes, whether the job owns it, and what would refuse or need a
    second confirmation (``needs_force``)."""
    try:
        return deletion.plan(job_id, files)
    except KeyError:
        raise HTTPException(404, "Pool job not found") from None


@router.delete("/jobs/{job_id}")
def job_delete(job_id: str, files: bool = False, force: bool = False):
    """Clear a finished job's record; with ``files=true`` also the export
    directory (and leftover partial) that job produced. Refused with 409
    while the job runs, when a safety rule forbids the directory, or when it
    was changed since the export and ``force`` is not set. Returns the bytes
    freed."""
    try:
        return deletion.delete(job_id, files, force, how="api")
    except KeyError:
        raise HTTPException(404, "Pool job not found") from None
    except deletion.DeleteRefused as exc:
        raise HTTPException(409, str(exc)) from None


class DeleteJobs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[str] = Field(min_length=1, max_length=500)
    files: bool = False
    force: bool = False


@router.post("/jobs/delete")
def jobs_delete(payload: DeleteJobs):
    """The same for several jobs; each is answered on its own (refused ones
    are listed with the reason, the rest go)."""
    return deletion.delete_many(
        payload.ids, payload.files, payload.force, how="api bulk"
    )


@router.post("/jobs/clear-failed")
def jobs_clear_failed():
    """Clear every failed, interrupted and cancelled job and its leftovers;
    finished exports and running jobs are never touched."""
    return deletion.clear_failed(how="api clear-failed")


@router.get("/deleted")
def deleted_log():
    """The tail of ``pool/deleted.jsonl``: every deletion, with who and how."""
    return {"deleted": deletion.read_deleted(200)}


@router.get("/cleanup")
def cleanup_inventory():
    """Partials and old jobs with sizes and ages, what is reclaimable and the
    free space of the volumes."""
    return cleanup.inventory()


class CleanupBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    partials: list[str] = Field(default_factory=list, max_length=200)
    jobs: list[str] = Field(default_factory=list, max_length=500)
    sweep: bool = False
    all_partials: bool = False


@router.post("/cleanup")
def cleanup_apply(payload: CleanupBody):
    """Delete the named partials and old jobs (never a live job's, never a
    finished export), or run the sweeper (``sweep``: expired items only;
    ``all_partials``: every stopped partial)."""
    if payload.sweep:
        return cleanup.sweep(all_partials=payload.all_partials)
    return cleanup.delete(payload.partials, payload.jobs)


@router.get("/jobs/{job_id}/summary")
def job_summary(job_id: str):
    """The ``pool_export.json`` of a finished export."""
    try:
        job = jobs.get(job_id)
    except KeyError:
        raise HTTPException(404, "Pool job not found") from None
    folder = (job.get("result") or {}).get("dataset_path")
    if (
        job.get("kind") != "export"
        or job.get("status") not in ("done", "done_with_errors")
        or not folder
    ):
        raise HTTPException(404, "No finished export for this job")
    path = Path(folder) / "pool_export.json"
    if not path.is_file():
        raise HTTPException(404, "pool_export.json is gone")
    return json.loads(path.read_text())


@router.get("/recipes")
def recipes():
    return {"recipes": recipe.listing()}


@router.get("/recipes/{name}")
def recipe_get(name: str):
    try:
        return recipe.load(name).model_dump()
    except KeyError:
        raise HTTPException(404, "Recipe not found") from None


@router.put("/recipes/{name}")
def recipe_put(name: str, payload: Recipe):
    if payload.name != name:
        raise ValueError("The recipe's name must match the URL")
    return recipe.save(payload)


@router.delete("/recipes/{name}")
def recipe_delete(name: str):
    if not recipe.delete(name):
        raise HTTPException(404, "Recipe not found")
    return {"deleted": name}


class Preview(BaseModel):
    recipe: Recipe
    format: Literal["lerobot_v21", "recap_value", "raw_capture"] | None = None
    human_as_success: bool = False
    # The export's frame rate and timing mode, for the notes on how raw
    # captures meet it (timing None: the format's default).
    fps: float | None = Field(None, ge=1, le=240)
    timing: Literal["resample", "retime"] | None = None


@router.post("/preview")
def preview(payload: Preview):
    """Counts for a recipe (saved or not): episodes, frames, per task, and
    what is excluded and why."""
    return recipe.preview(
        payload.recipe,
        payload.format,
        human_as_success=payload.human_as_success,
        fps=payload.fps,
        timing=payload.timing,
    )


class TaskQuery(BaseModel):
    recipe: Recipe
    task: str = Field(min_length=1, max_length=1000)
    format: Literal["lerobot_v21", "recap_value", "raw_capture"] | None = None
    human_as_success: bool = False


@router.post("/selection")
def selection(payload: TaskQuery):
    """The episodes the recipe picks for one task, each with its quality
    score, stratum and reasons (what preview counts and export writes)."""
    from .rules import normalize_task

    try:
        result = recipe.selected_episodes(
            payload.recipe,
            normalize_task(payload.task),
            payload.format,
            human_as_success=payload.human_as_success,
        )
    except KeyError:
        raise HTTPException(404, "The recipe has no such task") from None
    _viewer_links(result["episodes"])
    return result


@router.post("/suggest")
def suggest(payload: TaskQuery):
    """What adding a task offers: available episodes (successes, failures)
    under the recipe's filters and a default count that keeps the
    composition balanced."""
    return recipe.suggest(
        payload.recipe,
        payload.task,
        payload.format,
        human_as_success=payload.human_as_success,
    )


class Export(BaseModel):
    recipe: Recipe | None = None
    recipe_name: str | None = None
    options: ExportOptions
    dry_run: bool = False


@router.post("/export")
def export(payload: Export):
    """Plan (and, unless ``dry_run``, start) an export job."""
    if (payload.recipe is None) == (payload.recipe_name is None):
        raise ValueError("Give either recipe or recipe_name")
    try:
        chosen = payload.recipe or recipe.load(payload.recipe_name)
    except KeyError:
        raise HTTPException(404, "Recipe not found") from None
    job = jobs.plan_export(chosen, payload.options)
    if payload.dry_run:
        jobs.discard(job["id"])
        return jobs._brief(job)
    return jobs._brief(jobs.launch(job["id"]))


class ResetAnalysis(BaseModel):
    """What a reset export would do with a recipe's episodes, measured now."""

    model_config = ConfigDict(extra="forbid")
    recipe: Recipe | None = None
    recipe_name: str | None = None
    reset: ResetOptions = Field(
        default_factory=lambda: ResetOptions(direction="reset_only")
    )
    # Episodes looked at (the first ones of the selection, in export order);
    # each costs a few seconds of video decoding.
    limit: int = Field(12, ge=1, le=100)
    cameras: dict[str, str] | None = None
    camera_map: dict[str, str] = Field(default_factory=dict)


@router.post("/reset/analyze")
def reset_analyze(payload: ResetAnalysis):
    """Which of the selected episodes can be reversed, which release is the
    problem and the measures behind it. Nothing is written."""
    from ..conversion.options import Options
    from .recipe import select_detailed
    from .reset import preview as reset_preview

    if (payload.recipe is None) == (payload.recipe_name is None):
        raise ValueError("Give either recipe or recipe_name")
    try:
        chosen = payload.recipe or recipe.load(payload.recipe_name)
    except KeyError:
        raise HTTPException(404, "Recipe not found") from None
    rows = select_detailed(chosen, target="lerobot_v21").chosen
    conversion = Options(filter_static=False, timing="retime")
    if payload.cameras:
        conversion = conversion.model_copy(update={"cameras": payload.cameras})
    result = reset_preview.analyze_rows(
        rows[: payload.limit], payload.reset, conversion, payload.camera_map
    )
    return {
        **result,
        "selected": len(rows),
        "analyzed": min(len(rows), payload.limit),
        "profile": reset_profile.VERSION,
    }


# ------------------------------------------------------------------ remote


@router.get("/remotes")
def remotes():
    """Registered remote targets (``[user@]host:/path``; SSH keys only)."""
    return {"targets": remote.listing()}


class RemoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spec: str | None = Field(None, max_length=1200)
    host: str | None = None
    user: str | None = None
    path: str | None = None
    port: int | None = None
    description: str = ""


@router.put("/remotes/{name}")
def remote_put(name: str, payload: RemoteBody):
    """Register or replace a target. There is no password field: any other
    key is refused."""
    return remote.save(remote.target_from(name, payload.model_dump(exclude_none=True)))


@router.delete("/remotes/{name}")
def remote_delete(name: str):
    if not remote.delete(name):
        raise HTTPException(404, "Remote target not found")
    return {"deleted": name}


class Push(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str
    source: str | None = None
    export_job: str | None = None
    dry_run: bool = False


@router.post("/push")
def push(payload: Push):
    """Send a finished export (``source`` folder, or the export job that made
    it) to a registered target with rsync over SSH, as a cancellable job."""
    if (payload.source is None) == (payload.export_job is None):
        raise ValueError("Give either source or export_job")
    source = payload.source
    if payload.export_job:
        try:
            job = jobs.get(payload.export_job)
        except KeyError:
            raise HTTPException(404, "Pool job not found") from None
        source = (job.get("result") or {}).get("dataset_path")
        if (
            job.get("kind") != "export"
            or job.get("status") not in ("done", "done_with_errors")
            or not source
        ):
            raise ValueError("That export has not finished")
    try:
        planned = jobs.plan_push(source, payload.target, payload.dry_run)
    except KeyError:
        raise HTTPException(404, "Remote target not found") from None
    return jobs._brief(jobs.launch(planned["id"]))


# ------------------------------------------------------------------ task corrections


def _person(request: Request) -> None:
    """Approving or rejecting a task correction is a person's action. The
    service's middleware already turns an agent's Bearer credential away from
    every route but the Agent API and demands the UI token; this refuses
    again here so the rule does not depend on how the router is mounted."""
    if request.headers.get("authorization", "").lower().startswith("bearer "):
        raise HTTPException(
            403, "Reviewing a task correction is a person's action; agents only propose"
        )
    secret = os.getenv("LEVI_UI_TOKEN")
    if secret and not secrets.compare_digest(
        request.headers.get("x-levi-ui-token", ""), secret
    ):
        raise HTTPException(
            401, "A person's key is needed: `levi pool corrections approve|reject`"
        )


@router.get("/corrections")
def correction_versions():
    """The task correction versions of this pool, with counts per status."""
    return {"versions": corrections.listing()}


@router.get("/corrections/copies")
def copy_candidates():
    """Copies whose task texts differ, for a person to decide."""
    return {"candidates": corrections.copy_candidates()}


@router.get("/corrections/{version}")
def correction_entries(
    version: str, status: str | None = None, batch: str | None = None
):
    """One version's proposals, their status and how they match the index."""
    try:
        return corrections.show(version, status, batch)
    except KeyError:
        raise HTTPException(404, "Task correction version not found") from None


@router.post("/corrections/{version}/review")
def correction_review(version: str, payload: corrections.Review, request: Request):
    """Approve or reject proposals (``ids``, a ``batch`` or ``all``, less
    ``exclude``): a person only."""
    _person(request)
    try:
        return corrections.review(version, payload, principal="local-human")
    except KeyError:
        raise HTTPException(404, "Task correction version not found") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
