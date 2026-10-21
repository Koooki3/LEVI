"""The arm stack's actions on the marvel0 page (``stack_act.py``), all under
``/api/levi/marvel0/stack``:

- ``POST .../{m|a|b|c}/start`` and ``.../stop``: start or stop one member of the
  stack (M is the health monitor of ``stack.py``; the others are units this
  page names itself);
- ``POST .../marker`` with ``op`` ``touch`` | ``adopt`` | ``release``: the
  arm-controller marker;
- ``POST .../a/preflight`` (the arm's FCI port and whether a gripper stack
  runs), ``POST .../b/probe`` (only whether a gripper stack runs: allowed with
  A up, touches no FCI port) and ``POST .../c/preflight`` (QoS, gripper
  controllers, end-effector frame): read-only checks that a start needs;
- ``POST .../b/gripper`` with ``action`` ``open`` | ``close``: the gripper test;
- ``POST .../sequence/start`` and ``.../sequence/cancel``: the guided start of
  M, the marker and A, which stops before B and before C;
- ``GET .../{a|b|c}/log``: the printable tail of a unit's log (one route each).

The state is the ``act`` part of ``GET /api/levi/marvel0/stack``.

Every write is **a person's** (the same check as the policy deploy,
``deploy_api.person_only``: any Bearer credential is refused), carries a
``request_id`` (answered again, never acted on twice) and, where it moves
something, a typed phrase; it acts only in the product LEVI (the main
checkout's workspace) and takes the machine-wide stack lock for its decision and
launch (the slow part of a stop or a check runs after it, so that a stop never
waits for a check). Starting and the gripper test also need the control
file to switch them on (403 ``control_disabled`` otherwise); a stop does not.
All of this stops mistakes, not another program of the same account: the real
barriers are Franka Desk and the physical emergency stop, and nothing here is
an emergency stop.

Errors have the shape of ``errors.fail``. No answer names a path.
"""

from typing import Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict

from . import stack, stack_act, stack_api
from .deploy_api import person_only
from .errors import fail

router = APIRouter(prefix="/api/levi/marvel0", tags=["marvel0"])


def attach(stack_obj) -> stack_act.Act:
    """Give ``stack_obj`` its ``Act`` (once) and return it."""
    if stack_obj.act is None:
        stack_obj.act = stack_act.Act(
            stack_obj, stack_act.production_host(stack_obj.host)
        )
        # An earlier run of the page may have stopped A and not lived to
        # release the marker (its 90 s loop is in memory).
        stack_obj.act.resume()
    return stack_obj.act


def act() -> stack_act.Act:
    """The process's one ``Act`` (tests replace ``stack_api.supervisor``)."""
    return attach(stack_api.supervisor())


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CheckBody(Closed):
    request_id: str


class StartBody(Closed):
    request_id: str
    confirm: str
    attest: dict[str, bool] = {}


class StopBody(Closed):
    request_id: str
    confirm: str
    client_override: str = ""


class MarkerBody(Closed):
    request_id: str
    op: Literal["touch", "adopt", "release"]
    confirm: str


class GripperBody(Closed):
    request_id: str
    action: Literal["open", "close"]
    confirm: str
    attest: dict[str, bool] = {}


def _answer(call, response: Response, accepted=202):
    try:
        answer = call()
    except stack.Refuse as exc:
        raise fail(exc.status, exc.code, exc.message, **exc.extra) from exc
    stack_api.invalidate()
    done = answer.get("done")
    response.status_code = (
        200 if done in ("already_stopped", "acknowledged") else accepted
    )
    return answer


def _start_route(key):
    def route(body: StartBody, response: Response):
        return _answer(lambda: act().start(key, body.model_dump()), response)

    route.__doc__ = f"Start {key.upper()} (person only, product LEVI only)."
    return route


def _stop_route(key):
    def route(body: StopBody, response: Response):
        return _answer(lambda: act().stop(key, body.model_dump()), response)

    route.__doc__ = f"Stop {key.upper()} (person only); the order is C, A, B, then M."
    return route


for _key in ("m", "a", "b", "c"):
    router.add_api_route(
        f"/stack/{_key}/start",
        _start_route(_key),
        methods=["POST"],
        status_code=202,
        dependencies=[Depends(person_only)],
        name=f"stack_start_{_key}",
    )
    router.add_api_route(
        f"/stack/{_key}/stop",
        _stop_route(_key),
        methods=["POST"],
        status_code=202,
        dependencies=[Depends(person_only)],
        name=f"stack_stop_{_key}",
    )


@router.post("/stack/marker", status_code=200, dependencies=[Depends(person_only)])
def marker(body: MarkerBody, response: Response):
    """Set, adopt or release the arm-controller marker (person only)."""
    data = body.model_dump()
    op = data.pop("op")
    return _answer(lambda: act().marker_op(op, data), response, accepted=200)


@router.post("/stack/a/preflight", dependencies=[Depends(person_only)])
def preflight_a(body: CheckBody, response: Response):
    """Check the arm's FCI port (one TCP connection, never while A exists) and
    whether a gripper stack runs (person only)."""
    return _answer(lambda: act().preflight_a(body.model_dump()), response)


@router.post("/stack/b/probe", dependencies=[Depends(person_only)])
def probe_b(body: CheckBody, response: Response):
    """Does a gripper stack run that this page did not start? Asks B's
    controller manager only (about 20 s): no FCI contact, so it is allowed while
    A runs (person only)."""
    return _answer(lambda: act().probe_b(body.model_dump()), response)


@router.post("/stack/c/preflight", dependencies=[Depends(person_only)])
def preflight_c(body: CheckBody, response: Response):
    """The three read-only checks before the robot server (person only)."""
    return _answer(lambda: act().preflight_c(body.model_dump()), response)


@router.post("/stack/b/gripper", dependencies=[Depends(person_only)])
def gripper_test(body: GripperBody, response: Response):
    """Open or close the gripper once, through B's controller (person only)."""
    return _answer(lambda: act().gripper(body.model_dump()), response)


@router.post("/stack/sequence/start", dependencies=[Depends(person_only)])
def sequence_start(body: StartBody, response: Response):
    """Start M, set the marker, wait and start A; stops before B and C."""
    return _answer(lambda: act().sequence_start(body.model_dump()), response)


@router.post("/stack/sequence/cancel", dependencies=[Depends(person_only)])
def sequence_cancel(body: CheckBody, response: Response):
    """End the guided start; nothing that already runs is stopped."""
    return _answer(
        lambda: act().sequence_cancel(body.model_dump()), response, accepted=200
    )


def _log_route(key):
    def route(lines: int = 80):
        try:
            return act().log(key, lines)
        except stack.Refuse as exc:
            raise fail(exc.status, exc.code, exc.message, **exc.extra) from exc

    route.__doc__ = f"The last printable lines of {key.upper()}'s log (1-200)."
    return route


for _key in ("a", "b", "c"):
    router.add_api_route(
        f"/stack/{_key}/log",
        _log_route(_key),
        methods=["GET"],
        name=f"stack_log_{_key}",
    )
