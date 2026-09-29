"""``/api/levi/pool/*``: the training pool over HTTP, behind the service's
UI-token and same-origin middleware (writes: recipes, scans, exports)."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from . import index, jobs, recipe, settings
from .recipe import Recipe
from .export import ExportOptions

router = APIRouter(prefix="/api/levi/pool", tags=["Training pool"])


def _filters(
    category: list[str] | None = Query(None),
    source: list[str] | None = Query(None),
    task: list[str] | None = Query(None),
    search: str | None = None,
    format: list[str] | None = Query(None),
    outcome: Literal["success", "failure"] | None = None,
    policy: list[str] | None = Query(None),
    show_heldout: bool = False,
    show_copies: bool = False,
    show_archive: bool = False,
) -> dict:
    return {
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
    category: list[str] | None = Query(None),
    source: list[str] | None = Query(None),
    search: str | None = None,
    format: list[str] | None = Query(None),
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
                show_heldout=show_heldout,
                show_copies=show_copies,
                show_archive=show_archive,
            )
        )
    }


@router.get("/episodes")
def episodes(
    category: list[str] | None = Query(None),
    source: list[str] | None = Query(None),
    task: list[str] | None = Query(None),
    search: str | None = None,
    format: list[str] | None = Query(None),
    outcome: Literal["success", "failure"] | None = None,
    policy: list[str] | None = Query(None),
    show_heldout: bool = False,
    show_copies: bool = False,
    show_archive: bool = False,
    limit: int = Query(200, ge=1, le=5000),
    offset: int = Query(0, ge=0),
):
    return index.episodes(
        limit=limit,
        offset=offset,
        **_filters(
            category, source, task, search, format, outcome, policy,
            show_heldout, show_copies, show_archive,
        ),
    )


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
