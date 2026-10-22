"""The policy-server deploy of the marvel0:

- ``GET /api/levi/marvel0/deploy``: the state of the one policy server this page may
  run (``deploy.py``), derived afresh from systemd, the socket tables, the log
  and the record; checks, VRAM and what the page may offer;
- ``POST /api/levi/marvel0/deploy``: start it (a transient ``systemd --user`` unit);
- ``POST /api/levi/marvel0/deploy/stop``: stop that unit;
- ``GET /api/levi/marvel0/deploy/log``: the printable tail of its log.

The two writes are **a person's**: the same local authentication as the other
``/api/levi`` routes, and any Bearer credential is refused (the only ones LEVI
knows are agents'), as ``levi.automatic.api`` does. Every write carries a
``request_id`` (answered again, never acted on twice) and a typed confirmation
phrase naming the action and the model. Nothing here starts the arm stack
(``stack_act_api.py`` does) or the demo client (``run_api.py`` does), and no
control of the page is an emergency stop: a hand stays on the physical one.

Errors have the shape of ``errors.fail``; FastAPI's own 422 answers a malformed
body. The site file is needed first (412 without it); no answer names a path.
"""

import os
import secrets
import threading

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict

from levi.stt import manager as stt_manager

from . import deploy
from .errors import fail

router = APIRouter(prefix="/api/levi/marvel0", tags=["marvel0"])

_LOCK = threading.Lock()
_SUPERVISOR: list = []  # the process's one Supervisor, built on first use


def supervisor() -> deploy.Supervisor:
    """The process's supervisor (tests replace this function)."""
    with _LOCK:
        if not _SUPERVISOR:
            _SUPERVISOR.append(deploy.Supervisor(deploy.production_host()))
        return _SUPERVISOR[0]


def person_only(request: Request) -> None:
    """Only a person at the page may do this. The service's middleware has
    already turned an agent's Bearer credential away and demanded the UI token;
    this refuses again, so the rule does not depend on how the router is
    mounted (a copy of ``levi.automatic.api._person``)."""
    if request.headers.get("authorization", "").lower().startswith("bearer "):
        raise fail(403, "person_only", "This is a person's action; agents cannot do it")
    secret = os.getenv("LEVI_UI_TOKEN")
    if secret and not secrets.compare_digest(
        request.headers.get("x-levi-ui-token", ""), secret
    ):
        raise fail(401, "ui_token", "Use the LEVI Web UI")


class DeployBody(BaseModel):
    """Strict (``"true"`` is not true, ``"1"`` is not 1) and closed."""

    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str
    model_id: str
    rtc: bool
    fraction: float
    cfg_guidance_scale: float | None = None
    accept_unverified: bool = False
    confirm: str


class StopBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str
    confirm: str
    client_override: str = ""


@router.get("/deploy")
def deploy_view():
    """The policy server's state, what can be done and why not."""
    return supervisor().status()


@router.post("/deploy", status_code=202, dependencies=[Depends(person_only)])
def deploy_start(body: DeployBody):
    """Start the policy server for a model (person only). Refused, with the
    reason, while anything else holds the port, the arm, the GPU lock or the
    memory it needs."""
    # A speech worker still loaded would hold GPU memory the checks below count
    # against the deploy: stop it, wait until its process is gone, and let none
    # start until the deploy has answered (by then its status says `starting`,
    # which the speech gate refuses). Best effort: a failed release is logged
    # and never fails the deploy; it can delay it by about two seconds.
    with stt_manager.held_off():
        return supervisor().deploy(body.model_dump())


@router.post("/deploy/stop", dependencies=[Depends(person_only)])
def deploy_stop(body: StopBody, response: Response):
    """Stop the unit this page started (person only); never a server started
    by hand, never while a client is connected unless forced by the phrase."""
    answer = supervisor().stop(body.model_dump())
    response.status_code = 200 if answer.get("already") else 202
    return answer


@router.get("/deploy/log")
def deploy_log(lines: int = 80):
    """The last printable lines of the current deploy's log (1-200)."""
    return supervisor().log(lines)
