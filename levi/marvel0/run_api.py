"""The hosted evaluation run of the marvel0 page (``run.py``):

- ``GET /api/levi/marvel0/run``: the ``levi.marvel0.run.v1`` document: the state
  of the client this page started, what each action would do and why it is not
  offered, the start checks, the open confirmation token;
- ``POST /api/levi/marvel0/run/challenge``: a one-time 30-second token for the
  next action (``start``, ``enter``, ``label``, ``exit_home``, ``end_at_gate``);
- ``POST /api/levi/marvel0/run/start``: start the client (off mode) for a form;
- ``POST /api/levi/marvel0/run/key``: press one key the state allows;
- ``POST /api/levi/marvel0/run/hold``: freeze the client (SIGSTOP of its group);
- ``POST /api/levi/marvel0/run/end``: end a held, lost or still starting client;
- ``GET /api/levi/marvel0/run/log``: the printable tail of the client's log.

The writes are **a person's**: the same local authentication as the policy
deploy, and any Bearer credential is refused (``deploy_api.person_only``). Each
carries a ``request_id`` (answered again, never acted on twice), the typed
phrase it needs, and (start, keys) the token the page asked for a moment
before. Every write acts only in the product LEVI and holds the machine-wide
run lock; hold waits for neither. **These checks stop mistakes, not another
program of the same account**; the real barriers are Franka Desk and the
physical emergency stop, and no control here is one.

Errors have the shape of ``errors.fail``; FastAPI's own 422 answers a malformed
body. No answer names a path.
"""

import threading
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from . import run, stack_act
from .deploy_api import person_only

router = APIRouter(prefix="/api/levi/marvel0", tags=["marvel0"])

_LOCK = threading.Lock()
_SUPERVISOR: list = []  # the process's one Supervisor, built on first use


def supervisor() -> run.Supervisor:
    """The process's supervisor (tests replace this function)."""
    with _LOCK:
        if not _SUPERVISOR:
            _SUPERVISOR.append(run.Supervisor(run.production_host()))
        return _SUPERVISOR[0]


def active_job() -> dict | None:
    """The running client as a job row (``kind``, ``id``, ``running_seconds``)
    for ``levi stop`` and the deploy's stop, or None. Reads memory only."""
    return supervisor().active()


def adopt_on_start() -> None:
    """Take over what a restarted service finds: a client that outlived it is
    frozen at once. Never stops the service from starting."""
    try:
        supervisor().adopt()
    except Exception:  # noqa: BLE001
        return


def run_active() -> bool:
    """Whether a hosted client is on, for ``stack_act`` (it refuses to stop or
    start the arm stack meanwhile). Takes over what a restart left first, so
    the answer is right before anyone opened the run card. A supervisor that
    cannot answer raises, and the asker treats that as active."""
    sup = supervisor()
    sup.adopt()
    if sup.active() is not None:
        return True
    from . import aeri_run_api  # the hosted AERI terminal D shares the arm

    return aeri_run_api.active() is not None


def register() -> None:
    """Tell the arm stack how to ask whether a client is on (once, at import)."""
    stack_act.register_run_probe(run_active)


register()


class Closed(BaseModel):
    """Strict (``"true"`` is not true) and closed."""

    model_config = ConfigDict(extra="forbid", strict=True)


class ChallengeBody(Closed):
    purpose: Literal["start", "enter", "label", "exit_home", "end_at_gate"]
    form: dict | None = None


class Attest(Closed):
    estop: bool
    scene_reset: bool
    area_clear: bool


class StartBody(Closed):
    request_id: str
    form: dict
    challenge: str
    confirm: str
    attest: Attest


class KeyBody(Closed):
    request_id: str
    action: Literal[
        "enter", "end_at_gate", "success", "failure", "discard", "exit_home"
    ]
    challenge: str
    confirm: str = ""


class HoldBody(Closed):
    request_id: str


class EndBody(Closed):
    request_id: str
    confirm: str


def _answer(call, body: dict):
    try:
        return call(body)
    except run.Refuse as exc:
        raise run.http(exc) from exc


@router.get("/run")
def run_view():
    """The run's state, what can be done and why not (polled once a second)."""
    return supervisor().view()


@router.post("/run/challenge", dependencies=[Depends(person_only)])
def run_challenge(body: ChallengeBody):
    """A one-time token for the next action of this purpose (person only)."""
    return _answer(supervisor().challenge, body.model_dump())


@router.post("/run/start", status_code=202, dependencies=[Depends(person_only)])
def run_start(body: StartBody):
    """Start the client for the form (person only, product LEVI only)."""
    return _answer(supervisor().start, body.model_dump())


@router.post("/run/key", status_code=202, dependencies=[Depends(person_only)])
def run_key(body: KeyBody):
    """Press the key of one action; only the state that allows it accepts it."""
    return _answer(supervisor().key, body.model_dump())


@router.post("/run/hold", dependencies=[Depends(person_only)])
def run_hold(body: HoldBody):
    """Freeze the client. Always allowed, idempotent, nothing resumes it."""
    return _answer(supervisor().hold, body.model_dump())


@router.post("/run/end", status_code=202, dependencies=[Depends(person_only)])
def run_end(body: EndBody):
    """End a held, lost or still starting client (SIGKILL; no further motion)."""
    return _answer(supervisor().end, body.model_dump())


@router.get("/run/log")
def run_log(lines: int = 80):
    """The last printable lines of the client's log (1-200)."""
    return supervisor().log(lines)
