"""The live service's gate, seen from the core.

In timeshare mode the supervisor closes ``live/gate.json`` while the policy
server infers. The worker obeys it; this is the same rule for a *person* who
presses Run or Resume in the interface meanwhile (the core runs the model
call in its own process and knows nothing of the policy). Stdlib only: the
core imports it on every ``runs.execute`` in a live workspace.
"""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path

from . import jsonio

# ``tasks.advance`` (the task console) launches runs too (levi/harness/tasking.py).
GUARDED = frozenset({"runs.execute", "runs.resume", "tasks.advance"})
# The worker process sets this: it obeys the gate by standing down (worker.py).
WORKER_ENV = "LEVI_LIVE_WORKER"
CACHE_S = 0.2  # the per-request read of gate.json is cached this long
# The worker's own principals obey the gate themselves (``worker.stand_down``).
EXEMPT = frozenset({"live-auto", "live-planner"})
STALE_S = 20.0  # no heartbeat for this long: the supervisor is gone, not gating


HISTORY = "gate.jsonl"  # one line per gate transition, next to gate.json
HISTORY_TAIL = 64 * 1024  # the most that ``history`` reads from the end of the file

_CACHE: dict = {}


def record_transition(live_dir, row: dict, max_bytes=None, keep=3) -> bool:
    """Append one gate transition (a dict) to ``<live>/gate.jsonl``; the file
    rotates to ``.1``, ``.2``... like the other logs. Never raises: the history
    is a record, not part of the gate. True when the line was written."""
    try:
        jsonio.append_line(
            Path(live_dir) / HISTORY, row, max_bytes=max_bytes, keep=keep
        )
    except (OSError, TypeError, ValueError):
        return False
    return True


def brief(row) -> dict:
    """A transition reduced to what the status file shows. A value of the
    wrong type (a hand-edited or damaged line) reads as ``None``."""
    to = row.get("to") if isinstance(row.get("to"), dict) else {}
    at = row.get("at")
    return {
        "at": float(at) if type(at) in (int, float) else None,
        "open": to.get("open") if isinstance(to.get("open"), bool) else None,
        "code": str(to["code"]) if to.get("code") is not None else None,
        "reason": str(row.get("reason") or "")[:120],
    }


def history(live_dir, limit=5, tail_bytes=HISTORY_TAIL) -> list:
    """The last ``limit`` transitions, oldest first. Lines that do not parse
    (a torn write, a hand edit) are skipped; a missing file is an empty list.
    Reads only the last ``tail_bytes`` of the file, and of ``.1`` when the file
    is short."""
    path = Path(live_dir) / HISTORY
    rows: list = []
    for candidate in (path, path.with_name(path.name + ".1")):
        try:
            with candidate.open("rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                handle.seek(max(0, size - tail_bytes))
                data = handle.read()
        except OSError:
            continue
        lines = data.splitlines()
        if size > tail_bytes:
            lines = lines[1:]  # the first line of a tail is cut
        found = []
        for line in lines:
            with contextlib.suppress(ValueError):
                value = json.loads(line)
                if isinstance(value, dict):
                    found.append(value)
        rows = found + rows
        if len(rows) >= limit:
            break
    return rows[-limit:]


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


def _message(gate, run=False) -> str:
    """``run``: the words for a run that stops at a request (it continues by
    itself, ``resumer.py``); otherwise for a person's refused click."""
    if time.time() - float(gate.get("updated_at") or 0) > STALE_S:
        return (
            "The live service's gate file has not been refreshed for over "
            f"{STALE_S:.0f} s (is the supervisor running? `levi live status`) and "
            "a policy server or evaluation may be active: not sending model "
            "requests"
            + (
                "; the run is blocked until the gate is fresh and open again"
                if run
                else ""
            )
        )
    why = gate.get("reason") or gate.get("code") or "gate closed"
    if run:
        return (
            f"The robot evaluation is inferring on the GPU ({why}): the run "
            "stopped here and continues by itself once the gate has stayed open"
        )
    return (
        "The robot evaluation is inferring on the GPU "
        f"({why}): the "
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
            why = _message(gate, run=True)
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
