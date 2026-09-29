"""``/api/levi/pool/*``: the training pool over HTTP, behind the service's
UI-token and same-origin middleware (writes: recipes, scans, exports)."""

import json
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from . import index, jobs, recipe, remote, settings
from .export import ExportOptions
from .recipe import Recipe

Strings = Annotated[list[str] | None, Query()]
Outcome = Literal["success", "failure", "robot_flag_success", "verified_success"]

router = APIRouter(prefix="/api/levi/pool", tags=["Training pool"])


def _filters(
    category=None,
    source=None,
    task=None,
    search=None,
    format=None,
    outcome=None,
    policy=None,
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
        "show_heldout": show_heldout,
        "show_copies": show_copies,
        "show_archive": show_archive,
    }


@router.get("/status")
def status():
    """Settings, the last scan's summary and the running jobs."""
    return {
        "enabled": settings.enabled(),
        "roots": [str(p) for p in settings.pool_roots()],
        "export_roots": [str(p) for p in settings.export_roots()],
        "heldout_lists": [str(p) for p in settings.heldout_files()],
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
            category,
            source,
            task,
            search,
            format,
            outcome,
            policy,
            show_heldout,
            show_copies,
            show_archive,
            date_from,
            date_to,
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
    """Stop a running scan, export or push."""
    return jobs.cancel(job_id)


@router.get("/jobs/{job_id}/summary")
def job_summary(job_id: str):
    """The ``pool_export.json`` of a finished export."""
    try:
        job = jobs.get(job_id)
    except KeyError:
        raise HTTPException(404, "Pool job not found") from None
    folder = (job.get("result") or {}).get("dataset_path")
    if job.get("kind") != "export" or job.get("status") != "succeeded" or not folder:
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


@router.post("/preview")
def preview(payload: Preview):
    """Counts for a recipe (saved or not): episodes, frames, per task, and
    what is excluded and why."""
    return recipe.preview(payload.recipe, payload.format)


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
        return jobs._brief(job)
    return jobs._brief(jobs.launch(job["id"]))


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
            or job.get("status") != "succeeded"
            or not source
        ):
            raise ValueError("That export has not finished")
    try:
        planned = jobs.plan_push(source, payload.target, payload.dry_run)
    except KeyError:
        raise HTTPException(404, "Remote target not found") from None
    return jobs._brief(jobs.launch(planned["id"]))
