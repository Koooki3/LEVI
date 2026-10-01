"""The live service's gate, seen from the core.

In timeshare mode the supervisor closes ``live/gate.json`` while the policy
server infers. The worker obeys it; this is the same rule for a *person* who
presses Run or Resume in the interface meanwhile (the core runs the model
call in its own process and knows nothing of the policy). Stdlib only: the
core imports it on every ``runs.execute`` in a live workspace.
"""

from __future__ import annotations

import time

from . import jsonio

# ``tasks.advance`` (the task console) launches runs too (levi/harness/tasking.py).
GUARDED = frozenset({"runs.execute", "runs.resume", "tasks.advance"})
# The worker process sets this: it obeys the gate by standing down (worker.py).
WORKER_ENV = "LEVI_LIVE_WORKER"
CACHE_S = 0.2  # the per-request read of gate.json is cached this long
# The worker's own principals obey the gate themselves (``worker.stand_down``).
EXEMPT = frozenset({"live-auto", "live-planner"})
STALE_S = 20.0  # no heartbeat for this long: the supervisor is gone, not gating


_CACHE: dict = {}


def closed(gate, now=None, worker=False) -> bool:
    """Is this ``gate.json`` content a closed gate? One rule for the worker and
    for people. A missing or unreadable file means no supervisor gates anyone:
    open. A fresh file says what it says. A file nobody has refreshed for
    ``STALE_S`` (the supervisor is gone) is read the careful way. The worker
    (``worker=True``), which nobody supervises any more, takes it as closed
    always. A person is let through only if its last word was ``idle`` (no
    policy server and no evaluation when it was written) *and* no policy port
    listens right now (a read of the kernel's socket table, no connection): a
    server that appeared after the supervisor died is protected too."""
    if not isinstance(gate, dict):
        return False
    now = time.time() if now is None else now
    if now - float(gate.get("updated_at") or 0) <= STALE_S:
        return not gate.get("open", True)
    if worker or not gate.get("idle", False):
        return True
    from . import gpumgr

    ports = {int(p) for p in gate.get("policy_ports") or [] if str(p).isdigit()}
    return bool(ports & gpumgr.listening_ports())


def _message(gate) -> str:
    if time.time() - float(gate.get("updated_at") or 0) > STALE_S:
        return (
            "The live service's gate file has not been refreshed for over "
            f"{STALE_S:.0f} s (is the supervisor running? `levi live status`) and "
            "a policy server or evaluation may be active: not sending model "
            "requests"
        )
    return (
        "The robot evaluation is inferring on the GPU "
        f"({gate.get('reason') or gate.get('code') or 'gate closed'}): the "
        "evaluation is inferring, try again shortly"
    )


def request_blocked(now=None) -> str | None:
    """Why the next model request must not be sent, or None. Called before
    every request of every run in a live workspace's core (a person's Run, the
    task console, a resume): a run started while the gate was open stops at
    the next request after it closes. A stat of the workspace marker (nothing
    elsewhere) and a small file read at most every ``CACHE_S``. The worker is
    exempt (it stands down itself) and a gate file nobody refreshes holds
    nothing."""
    import os

    if os.environ.get(WORKER_ENV) == "1":
        return None
    from . import auto

    live = auto.live_dir()
    stamp = time.monotonic() if now is None else now
    seen = _CACHE.get(str(live))
    if seen and stamp - seen[0] < CACHE_S:
        return seen[1]
    why = None
    if (live / auto.MARKER).is_file():
        gate = jsonio.read(live / "gate.json")
        if closed(gate):
            why = _message(gate)
    _CACHE[str(live)] = (stamp, why)
    return why


def check(principal, name: str, live_dir=None):
    """Raise ``Conflict`` when ``name`` would start model requests while the
    gate is closed. A no-op outside a live workspace."""
    if name not in GUARDED:
        return
    if getattr(principal, "auto", False) or principal.id in EXEMPT:
        return
    from . import auto

    live = live_dir or auto.live_dir()
    if not (live / auto.MARKER).is_file():
        return
    gate = jsonio.read(live / "gate.json")
    if not closed(gate):
        return
    from levi.agent.store import Conflict

    raise Conflict(_message(gate))
