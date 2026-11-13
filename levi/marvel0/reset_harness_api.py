"""Independent offline-only API; never enters AERI's physical dispatcher."""

import threading

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from levi.paths import ROOT

from .deploy_api import person_only
from .errors import fail
from .reset_harness import HarnessError, HarnessManager

router = APIRouter(prefix="/api/levi/marvel0/reset-harness", tags=["marvel0"])
_LOCK = threading.Lock()
_MANAGER: HarnessManager | None = None


def manager() -> HarnessManager:
    global _MANAGER
    with _LOCK:
        if _MANAGER is None:
            _MANAGER = HarnessManager(ROOT / "offline-reset-harness")
        return _MANAGER


class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    scenario_id: str = Field(min_length=1, max_length=64)
    seed: int = Field(ge=0, lt=2**32)


class StopBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")


def _call(method, *args):
    try:
        return method(*args)
    except HarnessError as error:
        raise fail(error.status, error.code, error.code.replace("_", " ")) from None


@router.get("")
def catalog():
    return _call(manager().catalog)


@router.post("/runs", status_code=202, dependencies=[Depends(person_only)])
def start(body: RunBody):
    return _call(manager().start, body.request_id, body.scenario_id, body.seed)


@router.get("/runs/{run_id}")
def status(run_id: str):
    return _call(manager().get, run_id)


@router.post("/runs/{run_id}/stop", dependencies=[Depends(person_only)])
def stop(run_id: str, body: StopBody):
    return _call(manager().stop, run_id, body.request_id)
