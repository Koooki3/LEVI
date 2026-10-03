"""Take an episode out of a live dataset, and put it back.

A person can remove a mirrored episode from the live page (a robot mishap, a
reset that went wrong, a take nobody wants labelled). It is a **soft delete**:

- the episode's row in ``live/datasets/<name>.json`` gets
  ``excluded: {at, by, reason}``; nothing else about the row changes, a row
  without the key is simply not excluded (older state files need no
  migration), and restoring removes the key;
- **nothing is deleted**: not the rollout folder (never written by LEVI), not
  the mirror under ``captures/``, not the committed annotations or the
  evidence of a finished run;
- an excluded episode is not counted (``mirror.counts``, the status rows, the
  dataset's totals), is not labelled any more (the queue, the batch selection
  and the worker's filter pass over it), and is not exported by the training
  pool (``levi/pool/exclusions.py`` reads the same files);
- a review run whose episodes are *all* excluded is cancelled, so no review
  waits for a person for an episode that is gone (the verdict stays in the
  dataset state, as for the runs the cleanup cancels). A review run shared
  with episodes that are still in the dataset stays open: a run cannot give
  one episode back.

An episode that belongs to the batch in progress is refused (``Busy``): its
run is already planned with it, and cancelling work mid-flight is not what a
click on a list should do. The check and the write happen under the dataset
state's lock, and the worker's own filter (``Worker.filter_demos``) passes
over excluded rows under the same lock, so a click that lands while a batch
is being chosen is either refused or honoured, never half of each.

Only people do this: the API refuses an agent's credential (``api.py``), and
the automatic approver's call list (``auto.ALLOWED``) has no such call. Every
change is a line in ``live/audit.jsonl`` (``episode.exclude`` or
``episode.restore``). Standard library only.
"""

import logging
import time

from . import auto, jsonio, mirror

LOG = logging.getLogger("levi.live.exclusion")

REASON_MAX = 300
# Rows a person may remove: mirrored episodes in a settled state. A rejected
# or stuck demo was never taken into the dataset, so there is nothing to
# remove; an episode of the batch in progress is ``Busy``.
ELIGIBLE = ("mirrored", "done", "failed", "skipped_human")
TOOL_EXCLUDE = "episode.exclude"
TOOL_RESTORE = "episode.restore"
ACTOR = "person"
PRINCIPAL = "local-human"


class Refused(Exception):
    """A request nothing was done for. ``code`` is ``unknown``, ``busy`` or
    ``not_part``; ``demos`` names the episodes."""

    code = "refused"

    def __init__(self, demos):
        self.demos = sorted(demos)
        super().__init__(f"{self.code}: {', '.join(self.demos)}")


class Unknown(Refused):
    code = "unknown"


class Busy(Refused):
    code = "busy"


class NotPart(Refused):
    code = "not_part"


def is_excluded(row) -> bool:
    return isinstance(row, dict) and bool(row.get("excluded"))


def excluded_count(state: dict) -> int:
    return sum(1 for r in (state.get("demos") or {}).values() if is_excluded(r))


def in_batch(state: dict) -> set:
    """The episodes of the batch in progress (``current``), if any."""
    return set(((state.get("current") or {}).get("demos")) or [])


def clean_reason(reason) -> str:
    return " ".join(str(reason or "").split())[:REASON_MAX]


def _verdict_run(row):
    return (row.get("verdict") or {}).get("run_id")


def _drop_dead_reviews(value: dict, cancel_run) -> list:
    """Cancel the open review runs all of whose episodes are excluded; returns
    the run ids. ``value`` is the dataset state, changed in place."""
    demos = value.get("demos") or {}
    cancelled = []
    for run_id in list(value.get("review_runs") or []):
        members = [r for r in demos.values() if _verdict_run(r) == run_id]
        if not members or not all(is_excluded(r) for r in members):
            continue
        if cancel_run is not None:
            try:
                cancel_run(run_id)
            except (KeyError, ValueError):
                pass  # gone, or already ended: nothing is waiting any more
        cancelled.append(run_id)
    if cancelled:
        keep = [r for r in value["review_runs"] if r not in cancelled]
        value["review_runs"] = keep
        value["review_runs_open"] = len(keep)
        for row in demos.values():
            if is_excluded(row) and _verdict_run(row) in cancelled:
                row["excluded"].setdefault("cancelled_runs", []).append(
                    _verdict_run(row)
                )
    return cancelled


def exclude(
    config, name, demos, reason="", *, now=None, cancel_run=None, by=ACTOR
) -> dict:
    """Exclude ``demos`` of dataset ``name`` (all or nothing). Raises
    ``Unknown`` (a demo the dataset does not have), ``Busy`` (in the batch in
    progress) or ``NotPart`` (never taken into the dataset). Excluding an
    episode that is already excluded changes nothing. ``cancel_run(run_id)``
    cancels a review run (the API passes the workbench's)."""
    now = time.time() if now is None else now
    demos = list(dict.fromkeys(demos))
    reason = clean_reason(reason)
    outcome: dict = {}

    def change(value):
        rows = value.get("demos") or {}
        unknown = [d for d in demos if d not in rows]
        if unknown:
            raise Unknown(unknown)
        todo = [d for d in demos if not is_excluded(rows[d])]
        busy = [d for d in todo if d in in_batch(value)]
        if busy:
            raise Busy(busy)
        refused = [d for d in todo if rows[d].get("state") not in ELIGIBLE]
        if refused:
            raise NotPart(refused)
        for d in todo:
            rows[d]["excluded"] = {"at": now, "by": by, "reason": reason}
        outcome["changed"] = todo
        outcome["unchanged"] = [d for d in demos if d not in todo]
        outcome["cancelled_runs"] = _drop_dead_reviews(value, cancel_run)
        return value

    value = jsonio.update(mirror.state_path(config, name), change, default=dict)
    for demo in outcome["changed"]:
        audit(
            config,
            TOOL_EXCLUDE,
            name,
            demo,
            reason=reason,
            at=now,
            runs_cancelled=outcome["cancelled_runs"] or None,
        )
    return {**outcome, **summary(value)}


def restore(config, name, demos, *, now=None) -> dict:
    """Put ``demos`` back (all or nothing). Raises ``Unknown``; restoring an
    episode that is not excluded changes nothing. A review run cancelled when
    the episode was excluded is not reopened."""
    now = time.time() if now is None else now
    demos = list(dict.fromkeys(demos))
    outcome: dict = {}

    def change(value):
        rows = value.get("demos") or {}
        unknown = [d for d in demos if d not in rows]
        if unknown:
            raise Unknown(unknown)
        todo = [d for d in demos if is_excluded(rows[d])]
        outcome["review_cancelled"] = sorted(
            {
                run
                for d in todo
                for run in (rows[d]["excluded"].get("cancelled_runs") or [])
            }
        )
        for d in todo:
            del rows[d]["excluded"]
        outcome["changed"] = todo
        outcome["unchanged"] = [d for d in demos if d not in todo]
        return value

    value = jsonio.update(mirror.state_path(config, name), change, default=dict)
    for demo in outcome["changed"]:
        audit(config, TOOL_RESTORE, name, demo, at=now)
    return {**outcome, **summary(value)}


def summary(state: dict) -> dict:
    """The counts a client shows after a change."""
    return {
        "counts": mirror.counts(state),
        "excluded_count": excluded_count(state),
        "review_runs_open": state.get("review_runs_open", 0),
    }


def audit(config, tool, dataset, demo, *, reason=None, at=None, runs_cancelled=None):
    """One line per episode in ``live/audit.jsonl``. A log that cannot be
    written is reported but does not undo a change that was made."""
    record = {
        "time": time.time() if at is None else at,
        "principal": PRINCIPAL,
        "actor": ACTOR,
        "tool": tool,
        "dataset": dataset,
        "demo": demo,
        "decision": "completed",
    }
    if reason is not None:
        record["reason"] = reason
    if runs_cancelled:
        record["runs_cancelled"] = list(runs_cancelled)
    try:
        jsonio.append_line(
            config.live_dir / auto.AUDIT, record, max_bytes=auto.AUDIT_MAX_BYTES
        )
    except Exception as exc:  # noqa: BLE001 - the change was made
        LOG.warning("%s audit line for %s/%s not written: %s", tool, dataset, demo, exc)
