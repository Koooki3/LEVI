"""Rebuild ``live/stats.jsonl`` records for demos labelled before the file
existed (``levi live stats backfill``).

For every demo the dataset's state calls ``done`` or ``failed`` that has no
record, a record is rebuilt from what survives: the dataset's state file (the
demo's times, attempts, segment count, verdict), the run journals in the
workspace's store (model requests, tokens and their time, read-only) and the
committed changeset (the segment labels). A field none of these holds is
``null``: the settings that were in force (guideline, provider, model), the
gate's waits, the vLLM wake times, the batch, the episode's length. A store
that exists but cannot be read (locked, damaged) is a failure, not an empty
journal: that demo is skipped and reported, so a later run can fill it in.
Nothing is
estimated or copied from today's configuration. An episode a person removed
(``exclusion.py``) is backfilled too, with ``excluded: true``: the cost was
real and the record stays, and the statistics leave it out by default.

The rebuilt record says ``backfilled: true``; its ``at`` is the moment it
describes (the verdict, else the commit, else the mirroring), so a filter by
time and the per-session figures still place it. It is idempotent (a demo that
has any record is skipped, so a second run adds nothing) and never changes a
line that exists. ``plan`` is the dry run: it lists the demos and, for each
field, where its value came from. Standard library only.
"""

import json
import sqlite3
from pathlib import Path

from . import exclusion, mirror, stats

NULL = "null: not recorded anywhere"
STORE = "outputs/LEVI/workbench/agent/workbench.sqlite3"


class Unreadable(Exception):
    """The store could not be read right now (locked, damaged, not openable).
    Not the same as "there is nothing to read": a record built from it would
    have its model fields empty for good, because the demo then counts as
    having a record."""


def _open(workspace):
    """The store read-only; None when the workspace has no store at all (then
    there is no journal to find); ``Unreadable`` when it exists but cannot be
    opened."""
    path = Path(workspace) / STORE
    if not path.is_file():
        return None
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        raise Unreadable(f"store cannot be opened: {exc}") from exc


def run_events(workspace, run_id) -> list:
    """A run's journal, oldest first, read straight from the store's SQLite
    file in read-only mode. ``[]`` when there is none (no such run, no store);
    ``Unreadable`` when the store could not be read."""
    if not run_id:
        return []
    db = _open(workspace)
    if db is None:
        return []
    try:
        return [
            {"seq": seq, **json.loads(body)}
            for seq, body in db.execute(
                "SELECT seq,body FROM events WHERE run_id=? ORDER BY seq", (run_id,)
            )
        ]
    except (sqlite3.Error, ValueError) as exc:
        raise Unreadable(f"run journal cannot be read: {exc}") from exc
    finally:
        db.close()


def change_labels(workspace, changeset, episode) -> dict | None:
    """``{subtask_id: count}`` of the segments a committed changeset gave one
    episode; None when there is no such changeset; ``Unreadable`` when the
    store could not be read."""
    if not changeset:
        return None
    db = _open(workspace)
    if db is None:
        return None
    try:
        row = db.execute(
            "SELECT body FROM records WHERE kind='changes' AND id=?", (changeset,)
        ).fetchone()
        proposals = json.loads(row[0]).get("proposals") if row else None
    except (sqlite3.Error, ValueError, AttributeError) as exc:
        raise Unreadable(f"changeset cannot be read: {exc}") from exc
    finally:
        db.close()
    if not isinstance(proposals, list):
        return None
    labels: dict = {}
    for item in proposals:
        if isinstance(item, dict) and item.get("episode_index") == episode:
            label = item.get("subtask_id") or "unlabeled"
            labels[label] = labels.get(label, 0) + 1
    return labels


def rebuild(workspace, name, demo, row, *, events=run_events, labels=change_labels):
    """``(record, sources)`` for one demo of dataset ``name``; ``sources``
    maps every field of the schema to where its value came from."""
    sources: dict = {}
    record = stats.normalize({})
    temporal = row.get("temporal") if isinstance(row.get("temporal"), dict) else {}
    verdict = row.get("verdict") if isinstance(row.get("verdict"), dict) else {}

    def put(path, value, source):
        if value is None:
            return
        node = record
        *head, leaf = path.split(".")
        for key in head:
            node = node[key]
        node[leaf] = value
        sources[path] = source

    state = "dataset state"
    episode = row.get("episode_index")
    base = stats.num(row.get("completed_at"))
    put("dataset", name, "dataset state")
    put("demo", demo, "dataset state")
    put("episode_index", episode, f"{state}: episode_index")
    put("session", row.get("run_id"), f"{state}: run_id (eval.run_id of the demo)")
    put("attempts", row.get("attempts"), f"{state}: attempts")
    put(
        "operator_label",
        stats.operator_brief(row.get("operator_label")),
        f"{state}: operator_label (eval.* of the demo's metadata)",
    )
    put(
        "excluded",
        exclusion.is_excluded(row),
        f"{state}: excluded (a person's removal; the record is kept, marked)",
    )
    put("backfilled", True, "constant")
    put("timeline.completed_at", base, f"{state}: completed_at")
    put(
        "timeline.to_mirror_s",
        stats.after(row.get("mirrored_at"), base),
        f"{state}: mirrored_at - completed_at",
    )
    put(
        "timeline.to_commit_s",
        stats.after(temporal.get("committed_at"), base),
        f"{state}: temporal.committed_at - completed_at",
    )
    put(
        "timeline.to_verdict_s",
        stats.after(verdict.get("at"), base),
        f"{state}: verdict.at - completed_at",
    )

    journals = []
    if temporal.get("run_id") and isinstance(episode, int):
        found = events(workspace, temporal["run_id"])
        if found:
            journals.append(("temporal", found))
    if verdict.get("run_id") and isinstance(episode, int):
        found = events(workspace, verdict["run_id"])
        if found:
            journals.append(("review", found))
    if journals:
        use = stats.usage_of(journals, episode)
        first = use.pop("first_request_at")
        where = "run journal (model_step events of this episode)"
        planned = [
            e["time"]
            for stage, found in journals
            if stage == "temporal"
            for e in found
            if e.get("type") == "planned" and e.get("time") is not None
        ]
        put("timeline.first_request_at", first and round(first, 3), where)
        put("timeline.to_first_request_s", stats.after(first, base), where)
        put(
            "timeline.to_plan_s",
            stats.after(min(planned) if planned else None, base),
            "run journal: planned event",
        )
        for key, value in use.items():
            if key == "probe_tokens":
                continue  # the batch's calibration: carried by its first demo
            if key in ("requests", "model_seconds", "tokens"):
                for kind, amount in value.items():
                    # The batch's request-cost calibrations belong to the
                    # batch, whose first demo carried them: not recoverable.
                    if kind != "probe":
                        put(f"model.{key}.{kind}", amount, where)
            else:
                put(f"model.{key}", value, where)

    put("result.state", row.get("state"), f"{state}: state")
    if not temporal:
        reason = row.get("reason")
        put("result.reason", reason and str(reason)[:200], f"{state}: reason")
    else:
        put("result.segments", temporal.get("segments"), f"{state}: temporal.segments")
        got = labels(workspace, temporal.get("changeset"), episode)
        put("result.segment_labels", got, "committed changeset: proposals")
    for key in ("outcome", "events", "valid_events", "undecided"):
        put(f"result.verdict.{key}", verdict.get(key), f"{state}: verdict.{key}")
    # Only a verdict made under a rule beyond "any valid release" has these.
    for key in ("rule", "place_outcome"):
        if key in verdict:
            put(f"result.verdict.{key}", verdict[key], f"{state}: verdict.{key}")
    put(
        "result.review",
        temporal.get("review") or verdict.get("review"),
        f"{state}: temporal.review",
    )
    if verdict.get("spec"):
        put(
            "result.spec",
            {
                "guideline": None,
                "release_review": verdict["spec"],
                "release_review_version": verdict.get("spec_version"),
                "sha256": None,
            },
            f"{state}: verdict.spec (the guideline and file hashes are not recorded)",
        )
    at = (
        stats.num(verdict.get("at"))
        or stats.num(temporal.get("committed_at"))
        or stats.num(row.get("mirrored_at"))
    )
    put(
        "at", at and round(at, 3), f"{state}: verdict.at, else commit, else mirror time"
    )
    for path in stats.leaves(stats.TEMPLATE):
        sources.setdefault(path, NULL)
    sources["schema"] = "constant"
    return record, sources


def plan(config, dataset=None, *, events=run_events, labels=change_labels) -> list:
    """What a backfill would add: ``[{dataset, demo, record, sources}]`` for
    every ``done`` or ``failed`` demo that has no record yet."""
    have = {(r.get("dataset"), r.get("demo")) for r in stats.read(config.live_dir)}
    out = []
    for name, state in mirror.list_states(config).items():
        if dataset is not None and name != dataset:
            continue
        demos = state.get("demos") or {}
        for demo in sorted(demos):
            row = demos[demo]
            if not isinstance(row, dict) or row.get("state") not in ("done", "failed"):
                continue
            if (name, demo) in have:
                continue
            try:
                record, sources = rebuild(
                    config.workspace, name, demo, row, events=events, labels=labels
                )
            except Unreadable as exc:
                # Not written, and not "no data": it is tried again next time.
                out.append(
                    {
                        "dataset": name,
                        "demo": demo,
                        "record": None,
                        "sources": {},
                        "error": str(exc)[:200],
                    }
                )
                continue
            out.append(
                {"dataset": name, "demo": demo, "record": record, "sources": sources}
            )
    return out


def failures(items) -> list:
    """The planned demos whose journal could not be read."""
    return [i for i in items if i.get("error") or i.get("record") is None]


def apply(config, items) -> int:
    """Append the planned records; returns how many were written. A demo that
    got a record since the plan was made, and a demo whose journal could not be
    read (``failures``), are skipped."""
    have = {(r.get("dataset"), r.get("demo")) for r in stats.read(config.live_dir)}
    r = config.resources
    written = 0
    for item in items:
        if item.get("error") or item.get("record") is None:
            continue
        if (item["dataset"], item["demo"]) in have:
            continue
        stats.record(
            config.live_dir, item["record"], r.log_max_mb * 1024 * 1024, r.log_backups
        )
        have.add((item["dataset"], item["demo"]))
        written += 1
    return written
