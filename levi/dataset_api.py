"""Human-only local episode removal, including linked live workspaces."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, StrictInt

from . import dataset_management as management
from .live.api import _person

router = APIRouter(prefix="/api/levi/datasets", tags=["Local data management"])


class Selection(BaseModel):
    episodes: list[StrictInt] = Field(min_length=1, max_length=10000)


class Deletion(Selection):
    confirmation: str = Field(min_length=1, max_length=64)


def _call(fn, *args):
    try:
        return fn(*args)
    except management.ManagementError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@router.get("/{name}/episodes")
def episode_indices(name: str, live_session: str | None = None):
    return _call(management.episodes, name, live_session)


@router.post("/{name}/episodes/deletion-plan")
def preview(name: str, body: Selection, request: Request):
    _person(request)
    return _call(management.deletion_plan, name, body.episodes)


@router.delete("/{name}/episodes")
def remove(name: str, body: Deletion, request: Request):
    _person(request)
    return _call(management.delete_episodes, name, body.episodes, body.confirmation)
