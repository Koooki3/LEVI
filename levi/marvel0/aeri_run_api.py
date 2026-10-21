"""The hosted AERI terminal D of the marvel0 page (``aeri_run.py``):

- ``GET /api/levi/marvel0/aeri/run``: the ``levi.marvel0.aeri_run.v1`` document:
  the state of the terminal D this page started, the keys of the prompt that
  waits (each with the reason it is not offered), Stop, End, what the journal and
  the screen say;
- ``POST /api/levi/marvel0/aeri/run/challenge``: a one-time 30-second token for
  the start of a prepared run;
- ``POST /api/levi/marvel0/aeri/run/start``: start terminal D for a prepared run;
- ``POST /api/levi/marvel0/aeri/run/key``: press one key of the prompt the person
  looked at;
- ``POST /api/levi/marvel0/aeri/run/stop``: Ctrl+C (once per run, always while D
  lives);
- ``POST /api/levi/marvel0/aeri/run/end``: force D to end (only when its wrap-up
  is stuck);
- ``POST /api/levi/marvel0/aeri/run/presence``: this tab is looking (a key that
  moves the arm is only taken from a tab that said so a moment ago);
- ``GET /api/levi/marvel0/aeri/run/log``: the last printable lines of D's screen.

The writes are **a person's**: the same local authentication as the policy
deploy, any Bearer credential is refused (``deploy_api.person_only``), and agents
never call them (in external mode an agent only answers the VLM's queue files).
The bodies are closed. Every write carries a ``request_id`` (answered again,
never acted on twice) except Stop's, which is idempotent by its own rule, and
presence. **A click is a key of terminal D; nothing here is an emergency stop;
the gates stop mistakes, not another program of the same account.** The real
barriers are Franka Desk and the physical emergency stop.

Errors have the shape of ``errors.fail``; FastAPI's own 422 answers a malformed
body. No answer names a path.
"""

import threading
from typing import Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict

from . import aeri_run, aeri_state
from .aeri_state import Refuse
from .deploy_api import person_only

router = APIRouter(prefix="/api/levi/marvel0", tags=["marvel0"])

_LOCK = threading.Lock()
_SUPERVISOR: list = []  # the process's one Supervisor, built on first use


def supervisor() -> aeri_run.Supervisor:
    """The process's supervisor (tests replace this function)."""
    with _LOCK:
        if not _SUPERVISOR:
            _SUPERVISOR.append(aeri_run.Supervisor(aeri_run.production_host()))
        return _SUPERVISOR[0]


def active() -> dict | None:
    """The running terminal D as a job row (``kind``, ``id``, ``running_seconds``)
    for ``levi stop``, the deploy and the arm stack, or None. Takes over what a
    restart left first, so the answer is right before anyone opened the card. A
    supervisor that cannot answer raises, and the asker treats that as active."""
    sup = supervisor()
    sup.adopt()
    return sup.active()


active_job = active


def adopt_on_start() -> None:
    """Take over what a restarted service finds: a D that outlived it is sent one
    SIGINT and watched. Never stops the service from starting."""
    try:
        supervisor().adopt()
    except Exception:  # noqa: BLE001
        return


class Closed(BaseModel):
    """Strict (``"true"`` is not true) and closed."""

    model_config = ConfigDict(extra="forbid", strict=True)


class ChallengeBody(Closed):
    purpose: Literal["start"]
    name: str


class Attest(Closed):
    estop: bool
    scene_ok: bool
    area_clear: bool
    arm_home: bool


class StartBody(Closed):
    request_id: str
    name: str
    challenge: str
    confirm: str
    attest: Attest


# One tick of a window: a code of ``aeri_state.KEY_ATTEST`` (a closed set; the
# route refuses any other word before the supervisor sees it).
Tick = Literal[
    "scene",
    "limits",
    "checkpoints",
    "providers",
    "hardware",
    "switch_units",
    "switch_ports",
    "switch_gpu",
    "switch_home",
    "estop_hand",
    "hands_clear",
    "no_client",
    "scene_ready",
]


class Seen(Closed):
    run_id: str
    state_seq: int
    prompt_id: str


class KeyBody(Closed):
    request_id: str
    action: Literal[
        "execute",
        "execute_variant",
        "inventory_yes",
        "inventory_no",
        "home_o",
        "home_p",
        "home_h",
        "release_round",
        "verdict_success",
        "verdict_failure",
        "verdict_discard",
        "resume_ask",
        "resume_confirm",
        "resume_cancel",
        "quit",
    ]
    seen: Seen
    token: str
    tab_id: str
    confirm: str = ""
    # The ticks of the key's window, exactly those of ``aeri_state.KEY_ATTEST[action]``
    # (none for a key not in it): the server holds the key to them.
    attest: list[Tick] = []


class StopBody(Closed):
    request_id: str


class EndBody(Closed):
    request_id: str
    confirm: str
    attest: list[Tick] = []


class PresenceBody(Closed):
    tab_id: str
    visible: bool


# A route's body model and the code table must name the same actions and ticks.
assert set(KeyBody.model_fields["action"].annotation.__args__) == set(
    aeri_state.ACTIONS
)
assert set(Tick.__args__) == set(aeri_state.ATTEST_CODES)


def _answer(call, body: dict):
    try:
        return call(body)
    except Refuse as exc:
        raise aeri_run.http(exc) from exc


@router.get("/aeri/run")
def aeri_run_view():
    """The terminal D's state, the keys it offers and why not (polled once a second)."""
    try:
        return supervisor().view()
    except Refuse as exc:
        raise aeri_run.http(exc) from exc


@router.post("/aeri/run/challenge", dependencies=[Depends(person_only)])
def aeri_run_challenge(body: ChallengeBody):
    """A one-time token for the start of a prepared run (person only)."""
    return _answer(supervisor().challenge, body.model_dump())


@router.post("/aeri/run/start", status_code=202, dependencies=[Depends(person_only)])
def aeri_run_start(body: StartBody):
    """Start terminal D for a prepared run (person only, product LEVI only)."""
    return _answer(supervisor().start, body.model_dump())


@router.post("/aeri/run/key", status_code=202, dependencies=[Depends(person_only)])
def aeri_run_key(body: KeyBody):
    """Press the key of one action; only the prompt the person saw accepts it."""
    return _answer(supervisor().key, body.model_dump())


@router.post("/aeri/run/stop", status_code=202, dependencies=[Depends(person_only)])
def aeri_run_stop(body: StopBody):
    """Ctrl+C in terminal D: no more actions, the arm stays where it is, the
    run wraps up. Once per run (one retry when D did not acknowledge it)."""
    return _answer(supervisor().stop, body.model_dump())


@router.post("/aeri/run/end", status_code=202, dependencies=[Depends(person_only)])
def aeri_run_end(body: EndBody):
    """Force D to end (SIGKILL); only when its wrap-up is stuck."""
    return _answer(supervisor().end, body.model_dump())


@router.post("/aeri/run/presence", status_code=204, dependencies=[Depends(person_only)])
def aeri_run_presence(body: PresenceBody) -> Response:
    """This tab is (or no longer is) looking at the card."""
    _answer(lambda b: supervisor().presence(b), body.model_dump())
    return Response(status_code=204)


@router.get("/aeri/run/log")
def aeri_run_log(lines: int = 80):
    """The last printable lines of terminal D's screen (1-200)."""
    return supervisor().log(max(1, min(int(lines), 200)))
