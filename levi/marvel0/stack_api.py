"""The arm stack routes of the marvel0 page (``stack.py``):

- ``GET /api/levi/marvel0/stack``: the ``levi.marvel0.stack.v1`` document:
  the state of M, A, B, C, the policy server and the client from passive
  sources, the dry run of every start gate of A, B and C, the marker, the
  machine-wide stack lock and the health monitor's management;
- ``POST /api/levi/marvel0/stack/monitor/start`` and ``.../monitor/stop``:
  start or stop the health monitor M (read-only on the robot side);
- ``POST /api/levi/marvel0/stack/marker/clear``: remove the arm-controller
  marker, only when A's unit does not exist and the arm's sockets show no
  control connection.

**No route here starts or stops A, B or C** (``stack_act_api.py`` has them). The writes are a person's (the same
check as the policy deploy, ``deploy_api.person_only``), carry a
``request_id`` (answered again, never acted on twice) and a typed phrase,
act only in the product LEVI and hold the machine-wide stack lock. These
checks stop mistakes, not another program of the same account; the real
barriers are Franka Desk and the physical emergency stop.

Errors have the shape of ``errors.fail``. No answer names a path.
"""

import threading
import time

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from . import stack
from .deploy_api import person_only
from .errors import fail

router = APIRouter(prefix="/api/levi/marvel0", tags=["marvel0"])

CACHE_S = 2.0  # several open pages cost one read
_LOCK = threading.Lock()
_STACK: list = []
_VIEW: list = []  # [(monotonic time, document)]


def supervisor() -> stack.Stack:
    """The process's one ``Stack`` (tests replace this function), with the
    actions of ``stack_act.py`` attached so that its document carries ``act``."""
    with _LOCK:
        if not _STACK:
            from . import stack_act_api  # (the actions sit on top of this module)

            made = stack.Stack(stack.production_host())
            stack_act_api.attach(made)
            _STACK.append(made)
        return _STACK[0]


def invalidate() -> None:
    """Forget the cached view: a write has just changed the stack."""
    with _LOCK:
        _VIEW.clear()


def start_auto():
    """The automatic management of M for the service's lifespan: a running
    ``stack.Reconciler``, or None outside the product LEVI or unless
    ``LEVI_MARVEL0_MONITOR_AUTO=on``."""
    try:
        return stack.Reconciler.start(supervisor())
    except Exception:  # noqa: BLE001 - never stops the service from starting
        return None


class WriteBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str
    confirm: str


def _answer(call, body):
    try:
        answer = call(body.model_dump())
    except stack.Refuse as exc:
        raise fail(exc.status, exc.code, exc.message, **exc.extra) from exc
    invalidate()
    return answer


@router.get("/stack")
def stack_view():
    """The arm stack's state, the dry-run gates and the monitor's
    management (read-only; cached for two seconds)."""
    now = time.monotonic()
    with _LOCK:
        if _VIEW and now - _VIEW[0][0] < CACHE_S:
            return _VIEW[0][1]
    document = supervisor().view()
    with _LOCK:
        _VIEW[:] = [(now, document)]
    return document


@router.post("/stack/monitor/start", dependencies=[Depends(person_only)])
def monitor_start(body: WriteBody):
    """Start the health monitor M (person only, product LEVI only)."""
    return _answer(supervisor().start_monitor, body)


@router.post("/stack/monitor/stop", dependencies=[Depends(person_only)])
def monitor_stop(body: WriteBody):
    """Stop the page's health monitor M; refused while A or C may run."""
    return _answer(supervisor().stop_monitor, body)


@router.post("/stack/marker/clear", dependencies=[Depends(person_only)])
def marker_clear(body: WriteBody):
    """Remove the arm-controller marker; refused while A's unit exists or
    the arm's sockets show (or cannot rule out) a control connection."""
    return _answer(supervisor().clear_marker, body)
