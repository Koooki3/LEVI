"""The preparation card of the AERI run on the marvel0 page:

- ``GET /api/levi/marvel0/aeri``: the ``levi.marvel0.aeri.v1`` document: the
  switch, the site section, the code profile, the passive readiness rows, the
  prepared run, the live annotation service and the leftovers of an earlier run
  (``aeri_ready.py``); cached for three seconds;
- ``POST /api/levi/marvel0/aeri/prepare``: prepare one run (``aeri_prep.py``:
  the versioned port of ``new_run.py``): writes only the new run's folder;
- ``POST /api/levi/marvel0/aeri/live/stop``: stop the live annotation service
  that holds port 8100 (``systemctl --user stop`` of its unit);
- ``POST /api/levi/marvel0/aeri/leftovers/stop``: stop the units an earlier run
  left behind (``aeri_leftovers.py``).

The writes are **a person's**: the same local authentication as the policy
deploy, and any Bearer credential is refused (``deploy_api.person_only``); an
agent never calls them. Each carries a ``request_id`` (answered again, never
acted on twice, also after a restart), the typed phrase it needs and a closed
body. All need ``[aeri] enabled = true`` in the control file and the product
LEVI; the two stops also hold the machine-wide run lock for their short time and
refuse while an AERI run, a hosted evaluation client or the arm is in use, or
when any of that cannot be read. **Nothing here moves the arm, starts a service
or connects to a robot port.** These checks stop mistakes, not another program of
the same account; the real barriers are Franka Desk and the physical emergency
stop, and no control here is one.

Errors have the shape of ``errors.fail``; FastAPI's own 422 answers a malformed
body. No answer names a path except ``prepared.execute`` (the command for the
terminal fallback).
"""

import contextlib
import threading
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from . import aeri_leftovers, aeri_prep, aeri_ready, record, stack
from .aeri_prep import Refuse, http
from .deploy_api import person_only

router = APIRouter(prefix="/api/levi/marvel0", tags=["marvel0"])

LOCK_FILE = ".marvel0-run.lock"  # the run's lock (run.LOCK_FILE), in lab/levi-hub/
LOCK_OWNER = "marvel0-aeri-prep"

_LOCK = threading.Lock()
_PREPARER: list = []  # the process's one Preparer, built on first use


class Closed(BaseModel):
    """Strict (``"true"`` is not true) and closed."""

    model_config = ConfigDict(extra="forbid", strict=True)


class PrepareBody(Closed):
    request_id: str
    mode: Literal["external", "local"]
    task: str
    rounds: int
    steps: int
    gripper_confirm_s: float | None = None
    start_home: bool
    confirm: str


class ConfirmBody(Closed):
    request_id: str
    confirm: str


def _hub() -> Path:
    from . import api

    return api.marker_path().parent


class Preparer:
    """The writes of the card, one at a time in this process. ``readiness`` is the
    reader the document and the hosted run share; ``lock_path`` is the machine-wide
    run lock the two stops hold."""

    def __init__(
        self, readiness: aeri_ready.Readiness, lock_path=None, locks="/proc/locks"
    ):
        self.readiness = readiness
        self._lock_path = lock_path or (lambda: _hub() / LOCK_FILE)
        self._locks = locks
        self._guard = threading.Lock()

    # ---- what is read

    @property
    def host(self) -> aeri_ready.Host:
        return self.readiness.host

    def view(self) -> dict:
        return self.readiness.view()

    # ---- the gates every write passes

    def _gates(self, body, phrase):
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not record.REQUEST_ID.fullmatch(
            request_id
        ):
            raise Refuse("request_id_invalid")
        if body.get("confirm") != phrase:
            raise Refuse("confirm_required", phrase=phrase)
        facts = self.readiness.facts(fresh=True)
        if facts["control"]["enabled"] is not True:
            raise Refuse("aeri_disabled")
        if facts["product"] is not True:
            raise Refuse("not_product")
        aeri = facts.get("aeri")
        if aeri is None:
            raise Refuse(
                "aeri_unconfigured", errors=list(facts["site"].get("errors") or [])
            )
        return request_id, aeri, facts

    def _directory(self, aeri) -> Path:
        try:
            return aeri_prep.state_dir(
                aeri, aeri_prep.StateRoot(self.host.state_root()), create=True
            )
        except record.StateDirUnusable:
            raise Refuse("state_dir_unusable") from None

    @contextlib.contextmanager
    def _operation(self, action, hold_run_lock):
        if not self._guard.acquire(blocking=False):
            raise Refuse("busy")
        try:
            if not hold_run_lock:
                yield
                return
            lock = stack.StackLock(self._lock_path(), self._locks, self.host.now)
            try:
                with lock.hold(LOCK_OWNER, action):
                    yield
            except stack.Refuse as exc:
                raise Refuse(
                    "busy" if exc.code == "stack_busy" else "lock_unusable"
                ) from None
        finally:
            self._guard.release()

    def _replayed(self, directory, request_id, fingerprint):
        """The stored answer of this very request, None if it is new. A request
        that was accepted but never answered (the service stopped) is run again:
        every action here is safe to repeat (a fresh run name, a stop of what is
        already stopped)."""
        try:
            entry = record.get_request(directory, request_id)
        except record.RecordCorrupt:
            entry = None
        if entry is None:
            return None
        if entry.get("fingerprint") != fingerprint:
            raise Refuse("request_id_used")
        if entry.get("status") == "done" and isinstance(entry.get("answer"), dict):
            return {**entry["answer"], "replayed": True}
        return None

    def _transaction(self, action, directory, request_id, body, work, run_lock):
        fingerprint = record.fingerprint(action, body)
        stored = self._replayed(directory, request_id, fingerprint)
        if stored:
            return stored
        with self._operation(action, run_lock):
            stored = self._replayed(directory, request_id, fingerprint)
            if stored:
                return stored
            entry = {"fingerprint": fingerprint, "action": action}
            record.put_request(
                directory,
                request_id,
                {**entry, "status": "accepted", "time": record.iso(self.host.now())},
                replace=True,
            )
            try:
                answer = work()
            except Refuse as exc:
                record.drop_request(directory, request_id)
                aeri_prep.audit(
                    directory,
                    {
                        "event": f"{action}_refused",
                        "request_id": request_id,
                        "code": exc.code,
                    },
                    self.host.now(),
                )
                raise
            except BaseException:
                record.drop_request(directory, request_id)
                raise
            record.put_request(
                directory,
                request_id,
                {
                    **entry,
                    "status": "done",
                    "answer": answer,
                    "time": record.iso(self.host.now()),
                },
                replace=True,
            )
            return answer

    # ---- prepare

    def prepare(self, body: dict) -> dict:
        request_id, aeri, _ = self._gates(body, aeri_prep.PHRASE_PREPARE)
        directory = self._directory(aeri)
        form = {k: v for k, v in body.items() if k not in ("request_id", "confirm")}

        def work():
            self.readiness.forget()
            prepared = aeri_prep.prepare(aeri, form, self.host.prep, directory)
            aeri_prep.audit(
                directory,
                {
                    "event": "prepare",
                    "request_id": request_id,
                    "name": prepared["name"],
                    "mode": prepared["mode"],
                },
                self.host.now(),
            )
            return {
                "schema": aeri_prep.PREPARE_SCHEMA,
                "request_id": request_id,
                "replayed": False,
                "done": True,
                "name": prepared["name"],
                "prepared": prepared,
            }

        # Preparing writes only the new run folder and reads one process: it
        # needs no run lock, which would block a running client's keys for the
        # minute the check may take.
        answer = self._transaction(
            "prepare", directory, request_id, body, work, run_lock=False
        )
        self.readiness.forget()
        return answer

    # ---- the two stops

    def _obstacles(self, facts, with_gpu):
        got = aeri_ready._obstacles(facts)
        if not with_gpu:
            got.pop("gpu_held")
        aeri_leftovers.refuse_for(aeri_leftovers.why_not(**got))

    def live_stop(self, body: dict) -> dict:
        request_id, aeri, _ = self._gates(body, aeri_prep.PHRASE_LIVE_STOP)
        directory = self._directory(aeri)

        def work():
            facts = self.readiness.facts(fresh=True)
            if facts["live"] is None:
                raise Refuse("state_unreadable")
            if facts["live"] != "running":
                return self._action_answer(
                    "live_stop", request_id, True, [], facts, live=True
                )
            self._obstacles(facts, with_gpu=False)
            ran = aeri_leftovers.stop_live(
                facts["main_site"].live.unit, self.host.prep.environ, self.host.prep.run
            )
            after = self.readiness.facts(fresh=True)
            if after["live"] == "running" or after["live"] is None:
                raise Refuse(
                    "live_stop_failed",
                    reason="still_running"
                    if after["live"] == "running"
                    else "unreadable",
                    returncode=ran["returncode"],
                    tail=ran["tail"],
                )
            detail = None if ran["returncode"] == 0 else ran["tail"] or "nonzero exit"
            answer = self._action_answer(
                "live_stop",
                request_id,
                False,
                [{"name": "levi-live", "ok": True, "detail": detail}],
                after,
                live=True,
            )
            aeri_prep.audit(
                directory,
                {"event": "live_stop", "request_id": request_id},
                self.host.now(),
            )
            return answer

        return self._transaction(
            "live_stop", directory, request_id, body, work, run_lock=True
        )

    def leftovers_stop(self, body: dict) -> dict:
        request_id, aeri, _ = self._gates(body, aeri_prep.PHRASE_LEFTOVERS)
        directory = self._directory(aeri)

        def work():
            facts = self.readiness.facts(fresh=True)
            if facts["units"] is None:
                raise Refuse("state_unreadable")
            units = aeri_leftovers.leftover_units(facts["units"])
            if not units:
                return self._action_answer(
                    "leftovers_stop", request_id, True, [], facts, leftovers=True
                )
            self._obstacles(facts, with_gpu=True)
            results = aeri_leftovers.stop_units(
                [u["name"] for u in units], self.host.prep.run
            )
            after = self.readiness.facts(fresh=True)
            if not all(r["ok"] for r in results):
                raise Refuse("stop_failed", results=results)
            aeri_prep.audit(
                directory,
                {
                    "event": "leftovers_stop",
                    "request_id": request_id,
                    "units": [r["name"] for r in results],
                },
                self.host.now(),
            )
            return self._action_answer(
                "leftovers_stop", request_id, False, results, after, leftovers=True
            )

        return self._transaction(
            "leftovers_stop", directory, request_id, body, work, run_lock=True
        )

    def _action_answer(self, action, request_id, already, results, facts, **which):
        enabled = facts["control"]["enabled"] is True
        return {
            "schema": aeri_prep.ACTION_SCHEMA,
            "action": action,
            "request_id": request_id,
            "replayed": False,
            "done": True,
            "already": already,
            "results": results,
            "live_service": aeri_ready.live_block(facts, enabled)
            if which.get("live")
            else None,
            "leftovers": aeri_ready.leftovers_block(facts, enabled)
            if which.get("leftovers")
            else None,
        }


def preparer() -> Preparer:
    """The process's one preparer (tests replace this function)."""
    with _LOCK:
        if not _PREPARER:
            _PREPARER.append(Preparer(aeri_ready.readiness()))
        return _PREPARER[0]


def _answer(call, body: dict):
    try:
        return call(body)
    except Refuse as exc:
        raise http(exc) from exc


@router.get("/aeri")
def aeri_view():
    """The card's document: switch, site, profile, readiness rows, prepared run,
    live service and leftovers (read-only; cached for three seconds)."""
    return preparer().view()


@router.post("/aeri/prepare", dependencies=[Depends(person_only)])
def aeri_prepare(body: PrepareBody):
    """Prepare one AERI run (person only, product LEVI only); about a minute at
    most (the read-only ``aeri.py check``)."""
    return _answer(preparer().prepare, body.model_dump())


@router.post("/aeri/live/stop", dependencies=[Depends(person_only)])
def aeri_live_stop(body: ConfirmBody):
    """Stop the live annotation service (``systemctl --user stop`` of its unit),
    which holds port 8100 and the GPU lock; refused while an AERI run, a hosted evaluation client
    or the arm is in use."""
    return _answer(preparer().live_stop, body.model_dump())


@router.post("/aeri/leftovers/stop", dependencies=[Depends(person_only)])
def aeri_leftovers_stop(body: ConfirmBody):
    """Stop the model and policy units an earlier AERI run left behind (never a
    policy server started by hand)."""
    return _answer(preparer().leftovers_stop, body.model_dump())
