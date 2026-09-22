"""The one bounded wrap-up every finished task gets (plan §6.6).

Committed, cancelled, abandoned or failed: the task is closed exactly once.
Closing writes the ledger, folds committed work into the dataset's memory and
triages the run for improvement candidates. It makes no model call and never
changes the run's result. A usage report that arrives after closing refreshes
the ledger only; it does not triage again.
"""

import time

from . import improvements, ledger, memory
from .layout import harness_lock

MAX_ATTEMPTS = 2


def close(store, run_id, reason):
    claimed = []

    def claim(run):
        # Claimed in one transaction, so two processes noticing the same
        # finished run cannot both close it. A failed closing may be retried
        # (by sweep) a bounded number of times.
        previous = run.get("closure") or {}
        retry = previous.get("state") == "failed" and (
            previous.get("attempts", 1) < MAX_ATTEMPTS
        )
        if not previous or retry:
            run["closure"] = {
                "reason": reason,
                "state": "closing",
                "at": time.time(),
                "attempts": previous.get("attempts", 0) + 1,
            }
            claimed.append(True)

    run = store.mutate("runs", run_id, claim)
    if not claimed:
        return run["closure"]
    with harness_lock(store.state, run["dataset_key"]):
        facts, path = ledger.write(store, run_id)
        _, remembered = memory.update(store, facts)
        before = memory.record_cost(store, facts)
        from .evaluation import tile_floor

        triage = improvements.triage(
            store.state, facts, before, tile_floor(store, run["dataset_key"])
        )
        # What this run taught may now recur across tasks or datasets: offer
        # it for built-in knowledge (a person decides).
        from . import knowledge

        knowledge.refresh(store.state)
    closure = {
        "state": "closed",
        "reason": reason,
        "at": time.time(),
        "ledger": str(path),
        "memory_updated": remembered,
        "triage": triage,
        "evaluation": _evaluate(store, run, facts),
        "attempts": run["closure"]["attempts"],
    }
    store.mutate("runs", run_id, lambda r: r.update(closure=closure))
    # The ledger names its own closure, so it is written once more with it.
    ledger.write(store, run_id)
    store.event(run_id, "closed", reason=reason, triage=triage)
    return closure


def _evaluate(store, run, facts):
    """A full-dataset subtask annotation gets an evaluation record in
    ``<workspace>/eval``. Bookkeeping: a failure is noted, never raised."""
    from levi.eval import record

    try:
        run = store.get("runs", run["id"])
        if not record.eligible(run):
            return None
        return str(record.agent(store, run, facts))
    except Exception as exc:  # noqa: BLE001 - reported on the closure
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}


def safe_close(store, run_id, reason):
    """Closing is bookkeeping: a failure here must not undo the task's result."""
    try:
        return close(store, run_id, reason)
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        error = f"{type(exc).__name__}: {exc}"[:300]
        # Left visible on the run rather than retried: closing is bounded to
        # one attempt, and a person can see why it did not complete.
        store.mutate(
            "runs",
            run_id,
            lambda r: r.update(
                closure={
                    "state": "failed",
                    "reason": reason,
                    "error": error,
                    "attempts": (r.get("closure") or {}).get("attempts", 1),
                }
            ),
        )
        store.event(run_id, "closure_failed", error=error)
        return None


FINISHED = {"succeeded", "partially_succeeded", "cancelled", "failed"}


def close_if_finished(store, run_id):
    """Called after every capability and when a worker stops; cheap when open."""
    try:
        run = store.get("runs", run_id)
    except KeyError:
        return None
    if run.get("closure") or run["status"] not in FINISHED:
        return None
    return safe_close(store, run_id, run["status"])


def sweep(store):
    """Close every finished run that has not been closed, and retry a failed
    closing once. Runs at service start, so cost and memory never silently
    miss a run that ended while nothing was listening."""
    done = []
    for run in store.list("runs"):
        closure = run.get("closure") or {}
        if run["status"] not in FINISHED:
            continue
        if closure.get("state") == "closed":
            continue
        if (
            closure.get("state") == "failed"
            and closure.get("attempts", 1) >= MAX_ATTEMPTS
        ):
            continue
        if closure.get("state") == "closing":
            # A process died mid-close; the claim is stale.
            store.mutate(
                "runs",
                run["id"],
                lambda r: r.update(closure={**r["closure"], "state": "failed"}),
            )
        if safe_close(store, run["id"], run["status"]):
            done.append(run["id"])
    return done


def refresh(store, run_id):
    run = store.get("runs", run_id)
    if run.get("closure"):
        with harness_lock(store.state, run["dataset_key"]):
            facts, _ = ledger.write(store, run_id)
            memory.refresh_cost(store, facts)
