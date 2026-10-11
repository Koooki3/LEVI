"""Post-evaluation analysis records: the time segments of every labelled
episode next to its labels, so an evaluation's result (a low success rate on a
task, a checkpoint that fails) can be read together with what the subtasks
did.

``live/analysis.jsonl`` is an append-only file, schema ``levi.live.analysis.v1``:
one line per episode whose labelling finished (the time segments committed
and/or an automatic verdict exists). A re-labelled episode adds a new line;
readers keep the newest line of each ``(dataset, demo)``. The line holds the
operator label (ground truth, when the operator gave one), the online
judgement, the background verdict (always ``review: auto``, never reviewed),
the time segments (ordered subtask ids, start, end, duration, gaps and
repeats) and, when the writer knows it, where the labelling came from
(``capability``: opaque, written by whoever configured the run).

Everything here is plain code over the live workspace's own files: the
dataset state files, ``stats.jsonl`` and the run store (opened read-only).
No model is called, no number is estimated; a summary is computed from the
records by ``summarize``. The statistics' own definitions are reused
(``stats.agreement``, ``stats.wilson``), not redefined.

``append`` is crash-safe: one ``O_APPEND`` write under an ``flock``, then
``fsync``; a torn last line (a crash in the middle of a write) is skipped by
``read`` and the next ``append`` starts on a new line. ``sync`` is idempotent:
it writes a line only for an episode whose content changed.

Standard library only (plus the live package's own modules).
"""

import contextlib
import fcntl
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

from . import backfill, exclusion, jsonio, mirror, sessions, stats, statsview

SCHEMA = "levi.live.analysis.v1"
SUMMARY_SCHEMA = "levi.live.analysis.summary.v1"
FILE = "analysis.jsonl"
# The built-in vocabulary's transfer stages in order (levi/live/specs/
# generic-vocabulary.v1.json). A task with another vocabulary passes its own.
STAGES = ("approach", "grasp", "transport", "place", "retreat")
# Below this many episodes in a group, a rate is shown with a warning, not as a
# finding; below this share of coverage, the numbers lean on a minority.
MIN_EPISODES = 10
MIN_FAILURES = 5
MIN_COVERAGE = 0.8
OUTCOMES = ("success", "failure", "unknown")
CAPABILITY_MAX_BYTES = 4096
TEXT_MAX = 200

AUTO = "auto"  # the review state of every figure that comes from a model

# --- the file -----------------------------------------------------------------


def path(live_dir) -> Path:
    return Path(live_dir) / FILE


def append(live_dir, record) -> None:
    """Append one record: a single ``O_APPEND`` write under an ``flock``,
    then ``fsync``. A last line torn by a crash is closed first, so the new
    record is never glued to it. Raises ``OSError`` when it cannot write."""
    target = path(live_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, allow_nan=False, default=str) + "\n"
    data = line.encode("utf-8")
    fd = os.open(target, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            size = os.fstat(fd).st_size
            if size and os.pread(fd, 1, size - 1) != b"\n":
                data = b"\n" + data
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _reject(constant):
    raise ValueError(constant)


def read(live_dir, since=None) -> list:
    """Every readable record in file order (damaged, torn and foreign-schema
    lines skipped). ``since``: epoch seconds, drops older ``labelled_at``."""
    rows = []
    try:
        with path(live_dir).open("rb") as handle:
            raw = handle.read()
    except OSError:
        return rows
    for line in raw.split(b"\n"):
        if not line.strip():
            continue
        try:
            value = json.loads(line.decode("utf-8"), parse_constant=_reject)
        except (ValueError, UnicodeDecodeError):
            continue
        row = normalize(value)
        if row is None:
            continue
        if since is not None and (row["labelled_at"] or row["at"] or 0) < since:
            continue
        rows.append(row)
    return rows


def normalize(value):
    """A record in the current shape or None. Older or partial records read
    with the missing fields as ``None``."""
    if not isinstance(value, dict):
        return None
    if not str(value.get("schema", "")).startswith("levi.live.analysis."):
        return None
    out = {
        "schema": value.get("schema"),
        "at": stats.num(value.get("at")),
        "labelled_at": stats.num(value.get("labelled_at")),
        "dataset": value.get("dataset"),
        "group": value.get("group"),
        "task": value.get("task"),
        "demo": value.get("demo"),
        "episode_index": value.get("episode_index"),
        "session": value.get("session"),
        "operator": value.get("operator")
        if isinstance(value.get("operator"), dict)
        else None,
        "online": value.get("online")
        if isinstance(value.get("online"), dict)
        else None,
        "agent": value.get("agent") if isinstance(value.get("agent"), dict) else None,
        "segments": value.get("segments")
        if isinstance(value.get("segments"), dict)
        else None,
        "spec": value.get("spec") if isinstance(value.get("spec"), dict) else None,
        "capability": value.get("capability")
        if isinstance(value.get("capability"), dict)
        else None,
        "signature": value.get("signature"),
    }
    if not isinstance(out["dataset"], str) or not isinstance(out["demo"], str):
        return None
    out.update({k: v for k, v in value.items() if k not in out})
    return out


def latest(rows) -> list:
    """The newest record of each ``(dataset, demo)``: the last line of the
    file wins (a re-labelled episode only ever appends)."""
    found: dict = {}
    for row in rows:
        found[(row["dataset"], row["demo"])] = row
    return list(found.values())


# --- building a record ----------------------------------------------------------


def _text(value):
    return None if value is None else str(value)[:TEXT_MAX]


def segment_items(proposals, episode, decisions=None) -> list:
    """The time segments of one episode in a committed change set, in time
    order: ``{id, start, end, duration, outcome}``. A segment a person
    rejected is left out; only ``segment`` proposals of the ``subtask`` style
    count. ``duration`` is None when a bound is missing."""
    rejected = {k for k, v in (decisions or {}).items() if v == "rejected"}
    found = []
    for index, item in enumerate(proposals or []):
        if (
            not isinstance(item, dict)
            or str(index) in rejected
            or item.get("kind") != "segment"
            or item.get("episode_index") != episode
            or item.get("style", "subtask") != "subtask"
        ):
            continue
        start, end = stats.num(item.get("start")), stats.num(item.get("end"))
        duration = (
            round(end - start, 3) if start is not None and end is not None else None
        )
        if duration is not None and duration < 0:
            duration = None
        outcome = item.get("outcome")
        found.append(
            (
                start if start is not None else 0.0,
                index,
                {
                    "id": _text(item.get("subtask_id")) or "unlabeled",
                    "start": start,
                    "end": end,
                    "duration": duration,
                    "outcome": outcome if outcome in OUTCOMES else None,
                },
            )
        )
    found.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in found]


def gaps(sequence, stages=STAGES) -> list:
    """The stages missing from ``sequence`` that lie before the latest stage
    it reaches ("a place without a grasp"). Ids outside ``stages`` are not
    judged."""
    index = {s: i for i, s in enumerate(stages)}
    top = max((index[s] for s in sequence if s in index), default=-1)
    if top < 0:
        return []
    return [s for s in stages[:top] if s not in sequence]


def repeats(sequence) -> list:
    """Ids that occur more than once (a regrasp is a repeat, not an error)."""
    seen: set = set()
    again: list = []
    for item in sequence:
        if item in seen and item not in again:
            again.append(item)
        seen.add(item)
    return again


def segment_block(items, stages=STAGES) -> dict:
    sequence = [i["id"] for i in items]
    return {
        "count": len(items),
        "items": items,
        "sequence": sequence,
        "missing": gaps(sequence, stages),
        "duplicated": repeats(sequence),
    }


def _capability(*sources):
    for source in sources:
        value = source.get("capability") if isinstance(source, dict) else None
        if isinstance(value, dict) and value:
            try:
                text = json.dumps(value, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError):
                continue
            if len(text.encode("utf-8")) <= CAPABILITY_MAX_BYTES:
                return json.loads(text)
    return None


def _online(row):
    online = row.get("online")
    if not isinstance(online, dict):
        return None
    verdict = online.get("verdict") if isinstance(online.get("verdict"), dict) else {}
    return {
        "status": _text(online.get("status")),
        "outcome": verdict.get("outcome"),
        "undecided": verdict.get("undecided"),
        "timing": _text(online.get("timing")),
    }


def _agent(verdict):
    if not isinstance(verdict, dict):
        return None
    return {
        "outcome": verdict.get("outcome")
        if verdict.get("outcome") in stats.LABELS
        else None,
        "undecided": verdict.get("undecided") is True,
        "rule": _text(verdict.get("rule")),
        "source": _text(verdict.get("source")) or "background",
        "review": AUTO,
        "reviewed": False,
    }


def signature(record) -> str:
    """A hash of what the record says (not of when it was written)."""
    body = {
        k: record.get(k)
        for k in (
            "dataset",
            "demo",
            "session",
            "operator",
            "online",
            "agent",
            "segments",
            "spec",
            "capability",
        )
    }
    text = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]


def build_record(
    name, demo, row, *, state=None, stats_row=None, items=None, now=None, stages=STAGES
) -> dict:
    """The record of one episode from its row in the dataset state file
    (``mirror``), its newest ``stats.jsonl`` record and its committed time
    segments (``items``; None when the labelling made none)."""
    state = state or {}
    stats_row = stats_row or {}
    now = time.time() if now is None else now
    temporal = row.get("temporal") if isinstance(row.get("temporal"), dict) else {}
    verdict = row.get("verdict") if isinstance(row.get("verdict"), dict) else None
    spec = stats.dig(stats_row, "result", "spec")
    if not isinstance(spec, dict):
        spec = None
    else:
        spec = {
            k: spec.get(k)
            for k in ("guideline", "release_review", "release_review_version")
        }
    labelled = (
        stats.num(temporal.get("committed_at"))
        or stats.num((verdict or {}).get("at"))
        or stats.num(stats_row.get("at"))
        or now
    )
    record = {
        "schema": SCHEMA,
        "at": round(now, 3),
        "labelled_at": round(labelled, 3),
        "dataset": name,
        "group": _text(state.get("group")),
        "task": _text(state.get("task_folder")),
        "demo": demo,
        "episode_index": row.get("episode_index"),
        "session": _text(row.get("run_id")),
        "operator": stats.operator_brief(row.get("operator_label")),
        "online": _online(row),
        "agent": _agent(verdict),
        "segments": segment_block(items, stages) if items is not None else None,
        "spec": spec,
        "capability": _capability(row, stats_row, state),
    }
    record["signature"] = signature(record)
    return record


def change_proposals(workspace, changeset):
    """``(proposals, decisions)`` of a committed change set, read straight
    from the run store in read-only mode; None when the store or the change
    set does not exist; ``backfill.Unreadable`` when it cannot be read now."""
    db = backfill._open(workspace)
    if db is None:
        return None
    try:
        row = db.execute(
            "SELECT body FROM records WHERE kind='changes' AND id=?", (changeset,)
        ).fetchone()
        body = json.loads(row[0]) if row else None
    except (sqlite3.Error, ValueError) as exc:
        raise backfill.Unreadable(f"changeset cannot be read: {exc}") from exc
    finally:
        db.close()
    if not isinstance(body, dict) or not isinstance(body.get("proposals"), list):
        return None
    decisions = body.get("decisions")
    return body["proposals"], decisions if isinstance(decisions, dict) else {}


# --- keeping the file up to date --------------------------------------------------


def sync(config, dataset=None, *, now=None, stages=STAGES) -> dict:
    """Append a record for every finished episode that has none, or whose
    content changed since its newest record. Idempotent and safe to call from
    two processes: the read-compare-append runs under one lock. It reads the
    dataset state files, ``stats.jsonl`` and the run store (read-only) and
    writes only ``analysis.jsonl``. An episode whose run store cannot be read
    now is skipped and counted under ``unreadable`` (a later call fills it
    in); nothing partial is written. Returns ``{"written", "unchanged",
    "unreadable", "skipped"}``."""
    live_dir = Path(config.live_dir)
    out = {"written": 0, "unchanged": 0, "unreadable": 0, "skipped": 0}
    if not mirror.datasets_dir(config).is_dir():
        return out  # not a live workspace (yet): leave no file behind
    newest = {
        s["dataset"] + "\0" + s["demo"]: s
        for s in stats.latest(statsview.records(live_dir))
    }
    with jsonio.locked(path(live_dir)):
        known = {
            (r["dataset"], r["demo"]): r.get("signature")
            for r in latest(read(live_dir))
        }
        for name, state in mirror.list_states(config).items():
            if dataset is not None and name != dataset:
                continue
            for demo, row in sorted((state.get("demos") or {}).items()):
                if (
                    not isinstance(row, dict)
                    or row.get("state") != "done"
                    or row.get("deleted")
                    or exclusion.is_excluded(row)
                ):
                    out["skipped"] += 1
                    continue
                temporal = (
                    row.get("temporal") if isinstance(row.get("temporal"), dict) else {}
                )
                items = None
                if temporal.get("changeset"):
                    try:
                        found = change_proposals(
                            config.workspace, temporal["changeset"]
                        )
                    except backfill.Unreadable:
                        out["unreadable"] += 1
                        continue
                    if found is None:
                        # Committed, but the change set is not in the store
                        # (yet): wait rather than record "no segments".
                        out["unreadable"] += 1
                        continue
                    items = segment_items(found[0], row.get("episode_index"), found[1])
                record = build_record(
                    name,
                    demo,
                    row,
                    state=state,
                    stats_row=newest.get(name + "\0" + demo),
                    items=items,
                    now=now,
                    stages=stages,
                )
                if (
                    record["segments"] is None
                    and record["agent"] is None
                    and record["online"] is None
                ):
                    out["skipped"] += 1
                    continue
                if known.get((name, demo)) == record["signature"]:
                    out["unchanged"] += 1
                    continue
                append(live_dir, record)
                known[(name, demo)] = record["signature"]
                out["written"] += 1
    return out


# --- the summary ----------------------------------------------------------------------


def _operator(record):
    return stats.operator_of(record.get("operator"))


def _agent_outcome(record):
    return stats.agent_of(record.get("agent"))


def _count(k, n) -> dict:
    return {"n": k, "of": n, "rate": stats.ratio(k, n)}


def _has_segments(record) -> bool:
    seg = record.get("segments")
    return isinstance(seg, dict) and (seg.get("count") or 0) > 0


def order_stages(records, stages=STAGES) -> list:
    """The ids the group's episodes use, in a fixed order: the given stages
    first (only those that occur), then any other id in the order it first
    appears."""
    seen: list = []
    for record in records:
        for sid in (record.get("segments") or {}).get("sequence") or []:
            if sid not in seen:
                seen.append(sid)
    return [s for s in stages if s in seen] + [s for s in seen if s not in stages]


def funnel(records, stages=STAGES) -> dict:
    """The share of episodes (with time segments) that reached each subtask.
    For a given stage, "reached" means the episode has that stage or a later
    one (a missing middle stage does not hide a later one); another id counts
    when it occurs."""
    with_seg = [r for r in records if _has_segments(r)]
    order = order_stages(with_seg, stages)
    index = {s: i for i, s in enumerate(stages)}
    rows = []
    for sid in order:
        reached = present = 0
        for record in with_seg:
            sequence = record["segments"]["sequence"]
            if sid in sequence:
                present += 1
            if sid in index:
                reached += any(index.get(x, -1) >= index[sid] for x in sequence)
            else:
                reached += sid in sequence
        rows.append(
            {"id": sid, "reached": _count(reached, len(with_seg)), "present": present}
        )
    return {"of": len(with_seg), "stages": rows}


def durations(records, stages=STAGES) -> dict:
    """Per subtask id: the durations (seconds) of its segments, as count,
    median and quartiles (linear interpolation)."""
    pool: dict = {}
    for record in records:
        for item in (record.get("segments") or {}).get("items") or []:
            value = stats.num(item.get("duration"))
            if value is not None:
                pool.setdefault(item["id"], []).append(value)
    out = {}
    for sid in order_stages(records, stages):
        values = pool.get(sid, [])
        out[sid] = {
            "n": len(values),
            "median_s": stats._round(stats.quantile(values, 0.5)),
            "q1_s": stats._round(stats.quantile(values, 0.25)),
            "q3_s": stats._round(stats.quantile(values, 0.75)),
        }
    return out


def last_completed(record) -> str:
    """The id of the last time segment the labelling did not mark as failed
    ("completed" = ended without a failure outcome), or ``none``."""
    done = [
        i["id"]
        for i in (record.get("segments") or {}).get("items") or []
        if i.get("outcome") != "failure"
    ]
    return done[-1] if done else "none"


def landing_tally(failed, records, stages=STAGES) -> dict:
    """Where the ``failed`` episodes stopped: ``of`` them, ``with_segments``
    of those have time segments, and ``counts`` of the last completed
    subtask (``none``: nothing completed). ``records`` fixes the order of
    the ids (the group's whole vocabulary)."""
    have = [r for r in failed if _has_segments(r)]
    counts: dict = {}
    for record in have:
        sid = last_completed(record)
        counts[sid] = counts.get(sid, 0) + 1
    order = [*order_stages(records, stages), "none"]
    return {
        "of": len(failed),
        "with_segments": len(have),
        "counts": {s: counts[s] for s in order if s in counts},
    }


def landing(records, stages=STAGES) -> dict:
    """Where failed episodes stopped: the distribution of the last completed
    subtask. Operator failures are the primary count; failures only the
    background called (no operator success/failure) are counted apart and
    marked automatic and unreviewed."""
    primary = [r for r in records if _operator(r) == "failure"]
    auto = [
        r for r in records if _operator(r) is None and _agent_outcome(r) == "failure"
    ]
    return {
        "operator": landing_tally(primary, records, stages),
        "background": {**landing_tally(auto, records, stages), "review": AUTO},
    }


def _warnings(cell) -> list:
    out = []
    n = cell["episodes"]
    if n < MIN_EPISODES:
        out.append({"code": "small_sample", "n": n, "min": MIN_EPISODES})
    for code, key in (
        ("low_operator_coverage", "operator"),
        ("low_background_coverage", "background"),
        ("low_segment_coverage", "segments"),
    ):
        rate = cell["coverage"][key]["rate"]
        if rate is not None and rate < MIN_COVERAGE:
            out.append({"code": code, "rate": rate, "min": MIN_COVERAGE})
    if cell["coverage"]["operator"]["n"] == 0 and n:
        out.append({"code": "no_operator_labels"})
    failed = cell["landing"]["operator"]["with_segments"]
    if cell["landing"]["operator"]["of"] and failed < MIN_FAILURES:
        out.append({"code": "few_failures", "n": failed, "min": MIN_FAILURES})
    missing = cell.get("missing") or {}
    if missing.get("done_without_record"):
        out.append({"code": "records_pending", "n": missing["done_without_record"]})
    return out


def cell(records, *, missing=None, stages=STAGES) -> dict:
    """The analysis of one group of episodes (one checkpoint on one task)."""
    n = len(records)
    op = [r for r in records if _operator(r) is not None]
    ag = [r for r in records if _agent_outcome(r) in ("success", "failure")]
    decided = sum(1 for r in records if r.get("agent") is not None)
    wins = sum(1 for r in op if _operator(r) == "success")
    agent_wins = sum(1 for r in ag if _agent_outcome(r) == "success")
    pairs = [(r.get("operator"), r.get("agent")) for r in records]
    agree = stats.agreement(pairs)
    found = {
        "episodes": n,
        "coverage": {
            "operator": _count(len(op), n),
            "background": _count(decided, n),
            "segments": _count(sum(1 for r in records if _has_segments(r)), n),
        },
        "success": {
            **_count(wins, len(op)),
            "wilson95": stats.wilson(wins, len(op)),
            "basis": "operator",
            "small_sample": len(op) < MIN_EPISODES,
        },
        "background_success": {
            **_count(agent_wins, len(ag)),
            "wilson95": stats.wilson(agent_wins, len(ag)),
            "review": AUTO,
            "small_sample": len(ag) < MIN_EPISODES,
        },
        "agreement": {
            "review": AUTO,
            **{
                k: agree[k]
                for k in (
                    "pairs",
                    "judged",
                    "agree",
                    "rate",
                    "false_success",
                    "missed_success",
                    "undecided",
                    "no_agent",
                )
            },
        },
        "funnel": {**funnel(records, stages), "review": AUTO},
        "durations": {"by_subtask": durations(records, stages), "review": AUTO},
        "sequence": {
            "with_segments": sum(1 for r in records if _has_segments(r)),
            "gaps": sum(1 for r in records if (r.get("segments") or {}).get("missing")),
            "repeats": sum(
                1 for r in records if (r.get("segments") or {}).get("duplicated")
            ),
            "review": AUTO,
        },
        "landing": {**landing(records, stages), "review": AUTO},
        "missing": missing,
    }
    found["warnings"] = _warnings(found)
    return found


def missing_counts(config, records, group=None, task=None, include_excluded=False):
    """``{(group, task): {...}}`` of episodes the records cannot show, from
    the dataset state files: ``incomplete`` (stopped early), ``fr3_fault``
    (a robot fault, part of ``incomplete``), ``discarded`` (voided by the
    operator), ``failed`` (the labelling failed), ``excluded`` (a person
    removed them), ``unlabelled`` (waiting, running, rejected or stuck),
    ``done_without_record`` (labelled, record not written yet)."""
    have = {(r["dataset"], r["demo"]) for r in records}
    out: dict = {}
    for name, state in mirror.list_states(config).items():
        key = (state.get("group"), state.get("task_folder"))
        if (group is not None and key[0] != group) or (
            task is not None and key[1] != task
        ):
            continue
        slot = out.setdefault(
            key,
            dict.fromkeys(
                (
                    "incomplete",
                    "fr3_fault",
                    "discarded",
                    "failed",
                    "excluded",
                    "unlabelled",
                    "done_without_record",
                ),
                0,
            ),
        )
        inc = state.get("incomplete")
        if isinstance(inc, dict):
            slot["incomplete"] += int(stats.num_or_zero(inc.get("count")))
            slot["fr3_fault"] += int(stats.num_or_zero(inc.get("fr3_fault")))
        slot["discarded"] += int(stats.num_or_zero(state.get("discarded")))
        for demo, row in (state.get("demos") or {}).items():
            if not isinstance(row, dict) or row.get("deleted"):
                continue
            if exclusion.is_excluded(row):
                slot["excluded"] += 1
                continue
            kind = row.get("state")
            if kind == "failed":
                slot["failed"] += 1
            elif kind == "done":
                if (name, demo) not in have:
                    slot["done_without_record"] += 1
            elif kind in ("mirrored", "annotating", "stuck", "rejected"):
                slot["unlabelled"] += 1
    return out


def summarize(records, *, missing=None, stages=STAGES, scope=None) -> dict:
    """The summary of ``records`` (any iterable of records; the newest of
    each episode is used), one cell per ``(group, task)`` plus ``all``."""
    rows = latest(records)
    groups: dict = {}
    for record in rows:
        groups.setdefault((record.get("group"), record.get("task")), []).append(record)
    missing = missing or {}
    keys = sorted({*groups, *missing}, key=lambda k: (str(k[0] or ""), str(k[1] or "")))
    cells = []
    for key in keys:
        cells.append(
            {
                "group": key[0],
                "task": key[1],
                **cell(groups.get(key, []), missing=missing.get(key), stages=stages),
            }
        )
    return {
        "schema": SUMMARY_SCHEMA,
        "review": AUTO,
        "scope": scope or {},
        "stages": list(stages),
        "cells": cells,
        "all": cell(rows, stages=stages),
    }


def build(config, *, group=None, task=None, since=None, stages=STAGES) -> dict:
    """The summary of a live workspace, read-only: ``analysis.jsonl`` for the
    records, the dataset state files for what is missing. Episodes a person
    removed, and datasets that were archived, are left out."""
    since_s = sessions.parse_time(since)
    if since is not None and since != "" and since_s is None:
        raise ValueError("since is an ISO time or epoch seconds")
    live_dir = Path(config.live_dir)
    states = mirror.list_states(config)
    removed, _known = statsview.excluded_demos(config)
    rows = [
        r
        for r in latest(read(live_dir, since=since_s))
        if (r["dataset"], r["demo"]) not in removed
        and (not states or r["dataset"] in states)
        and (group is None or r.get("group") == group)
        and (task is None or r.get("task") == task)
    ]
    missing = missing_counts(config, rows, group, task)
    found = summarize(
        rows,
        missing=missing,
        stages=stages,
        scope={"group": group, "task": task, "since": since_s},
    )
    return found


# --- text ---------------------------------------------------------------------------------

TAG = {"en": "automatic, unreviewed", "zh": "自动、未复核"}
TEXT = {
    "en": {
        "title": "Evaluation analysis: results and subtasks",
        "intro": "Operator labels are the ground truth. Every figure marked "
        "(%s) comes from the background labelling and has not been reviewed "
        "by a person. Numbers are computed from the records, not written by a model.",
        "none": "No analysis records in this scope.",
        "episodes": "episodes",
        "coverage": "Coverage",
        "operator": "operator label",
        "background": "background verdict",
        "segments": "time segments",
        "success": "Success rate (operator labels)",
        "auto_success": "Success rate (background verdicts)",
        "agreement": "Background verdict against operator label",
        "agree_line": "agree %s of %s judged (%s); false success %s of %s; missed success %s of %s; undecided %s; no verdict %s",
        "funnel": "Subtask funnel (episodes that reached the subtask)",
        "durations": "Subtask duration (seconds)",
        "landing": "Where failed episodes stopped (last completed subtask)",
        "landing_operator": "operator-labelled failures",
        "landing_background": "failures only the background called",
        "missing": "Not in the numbers",
        "stage": "subtask",
        "reached": "reached",
        "present": "present",
        "n": "n",
        "median": "median",
        "q1": "Q1",
        "q3": "Q3",
        "count": "episodes",
        "none_id": "none (nothing completed)",
        "seq": "Sequences: %s of %s with gaps, %s with repeated subtasks",
        "warn": "Warnings",
        "interval": "95% Wilson interval",
        "small": "small sample",
        "labels": {
            "incomplete": "stopped early",
            "fr3_fault": "of which robot faults",
            "discarded": "voided",
            "failed": "labelling failed",
            "excluded": "removed by a person",
            "unlabelled": "not labelled yet",
            "done_without_record": "labelled, record pending",
        },
        "warnings": {
            "small_sample": "Only {n} episodes (fewer than {min}): read the rates as a description, not a finding.",
            "low_operator_coverage": "Operator labels cover {rate:.0%} of the episodes (below {min:.0%}).",
            "low_background_coverage": "Background verdicts cover {rate:.0%} of the episodes (below {min:.0%}).",
            "low_segment_coverage": "Time segments cover {rate:.0%} of the episodes (below {min:.0%}).",
            "no_operator_labels": "No episode has an operator label: no success rate.",
            "few_failures": "Only {n} failed episodes have time segments (fewer than {min}): the stopping points are anecdotal.",
            "records_pending": "{n} labelled episodes have no analysis record yet (run `levi live analysis --sync`).",
        },
    },
    "zh": {
        "title": "评测分析：结果与子任务",
        "intro": "操作员标签是真值。凡标注（%s）的数字来自后台标注，没有人复核过。"
        "所有数字由代码从记录算出，不含模型生成的文字。",
        "none": "此范围内没有分析记录。",
        "episodes": "片段",
        "coverage": "覆盖率",
        "operator": "操作员标签",
        "background": "后台判定",
        "segments": "时间片段",
        "success": "成功率（操作员标签）",
        "auto_success": "成功率（后台判定）",
        "agreement": "后台判定与操作员标签对照",
        "agree_line": "一致 %s/%s 已判定（%s）；误判成功 %s/%s；漏判成功 %s/%s；未决 %s；无判定 %s",
        "funnel": "子任务漏斗（到达该子任务的片段）",
        "durations": "子任务时长（秒）",
        "landing": "失败片段的落点（最后完成的子任务）",
        "landing_operator": "操作员标为失败",
        "landing_background": "仅后台判为失败",
        "missing": "未计入的片段",
        "stage": "子任务",
        "reached": "到达",
        "present": "出现",
        "n": "个数",
        "median": "中位数",
        "q1": "下四分位",
        "q3": "上四分位",
        "count": "片段数",
        "none_id": "无（没有完成任何子任务）",
        "seq": "序列：%s/%s 有缺环，%s 有重复子任务",
        "warn": "警告",
        "interval": "95% Wilson 区间",
        "small": "样本小",
        "labels": {
            "incomplete": "提前中止",
            "fr3_fault": "其中机械臂故障",
            "discarded": "作废",
            "failed": "标注失败",
            "excluded": "被人移除",
            "unlabelled": "尚未标注",
            "done_without_record": "已标注、记录待写",
        },
        "warnings": {
            "small_sample": "只有 {n} 个片段（少于 {min}）：比率只是描述，不是结论。",
            "low_operator_coverage": "操作员标签只覆盖 {rate:.0%} 的片段（低于 {min:.0%}）。",
            "low_background_coverage": "后台判定只覆盖 {rate:.0%} 的片段（低于 {min:.0%}）。",
            "low_segment_coverage": "时间片段只覆盖 {rate:.0%} 的片段（低于 {min:.0%}）。",
            "no_operator_labels": "没有片段带操作员标签：算不出成功率。",
            "few_failures": "带时间片段的失败片段只有 {n} 个（少于 {min}）：停止位置只能当个例看。",
            "records_pending": "有 {n} 个已标注片段还没有分析记录（运行 `levi live analysis --sync`）。",
        },
    },
}


def _pct(value) -> str:
    return "-" if value is None else f"{100 * value:.1f}%"


def _share(item, lang, flag=False) -> str:
    text = f"{item['n']}/{item['of']} ({_pct(item['rate'])})"
    if item.get("wilson95"):
        low, high = item["wilson95"]
        text += f", {TEXT[lang]['interval']} [{_pct(low)}, {_pct(high)}]"
    if flag and item.get("small_sample"):
        text += f", {TEXT[lang]['small']}"
    return text


def _num(value) -> str:
    return "-" if value is None else f"{value:g}"


def _cell_lines(c, lang, title) -> list:
    t = TEXT[lang]
    tag = TAG[lang]
    lines = [f"### {title}", ""]
    cov = c["coverage"]
    lines.append(
        f"- {t['coverage']}: {c['episodes']} {t['episodes']}; "
        f"{t['operator']} {_pct(cov['operator']['rate'])}, "
        f"{t['background']} {_pct(cov['background']['rate'])} ({tag}), "
        f"{t['segments']} {_pct(cov['segments']['rate'])} ({tag})"
    )
    lines.append(f"- {t['success']}: {_share(c['success'], lang, True)}")
    lines.append(
        f"- {t['auto_success']} ({tag}): {_share(c['background_success'], lang, True)}"
    )
    a = c["agreement"]
    if a["pairs"]:
        lines.append(
            f"- {t['agreement']} ({tag}): "
            + t["agree_line"]
            % (
                a["agree"],
                a["judged"],
                _pct(a["rate"]),
                a["false_success"]["n"],
                a["false_success"]["of"],
                a["missed_success"]["n"],
                a["missed_success"]["of"],
                a["undecided"],
                a["no_agent"],
            )
        )
    miss = c.get("missing")
    if miss:
        shown = [
            f"{t['labels'][k]} {v}" for k, v in miss.items() if v and k in t["labels"]
        ]
        if shown:
            lines.append(f"- {t['missing']}: " + ", ".join(shown))
    f = c["funnel"]
    if f["stages"]:
        lines += ["", f"**{t['funnel']}** ({tag}, {f['of']} {t['episodes']})", ""]
        lines.append(f"| {t['stage']} | {t['reached']} | {t['present']} |")
        lines.append("| --- | --- | --- |")
        for s in f["stages"]:
            lines.append(
                f"| {s['id']} | {s['reached']['n']}/{s['reached']['of']} "
                f"({_pct(s['reached']['rate'])}) | {s['present']} |"
            )
        lines += ["", f"**{t['durations']}** ({tag})", ""]
        lines.append(
            f"| {t['stage']} | {t['n']} | {t['median']} | {t['q1']} | {t['q3']} |"
        )
        lines.append("| --- | --- | --- | --- | --- |")
        for sid, d in c["durations"]["by_subtask"].items():
            lines.append(
                f"| {sid} | {d['n']} | {_num(d['median_s'])} | {_num(d['q1_s'])} | {_num(d['q3_s'])} |"
            )
        q = c["sequence"]
        lines += ["", "- " + t["seq"] % (q["gaps"], q["with_segments"], q["repeats"])]
    for key in ("operator", "background"):
        block = c["landing"][key]
        if not block["of"]:
            continue
        label = t["landing_" + key]
        lines += [
            "",
            f"**{t['landing']}**: {label} ({tag}), {block['with_segments']}/{block['of']} {t['episodes']}",
            "",
        ]
        lines.append(f"| {t['stage']} | {t['count']} |")
        lines.append("| --- | --- |")
        for sid, n in block["counts"].items():
            lines.append(f"| {t['none_id'] if sid == 'none' else sid} | {n} |")
    if c["warnings"]:
        lines += ["", f"**{t['warn']}**", ""]
        for w in c["warnings"]:
            lines.append("- " + t["warnings"][w["code"]].format(**w))
    return lines + [""]


def to_markdown(summary, lang="en") -> str:
    lang = "zh" if lang == "zh" else "en"
    t = TEXT[lang]
    lines = [f"# {t['title']}", "", t["intro"] % TAG[lang], ""]
    if not summary["cells"]:
        lines.append(t["none"])
    for c in summary["cells"]:
        title = f"{c['group'] or '-'} / {c['task'] or '-'}"
        lines += _cell_lines(c, lang, title)
    return "\n".join(lines).rstrip() + "\n"


def to_json(summary) -> str:
    return json.dumps(summary, ensure_ascii=False, indent=1, allow_nan=False) + "\n"


def sync_quiet(config, dataset=None) -> None:
    """``sync`` that never raises: for a caller (the worker after a batch)
    that must not fail because the analysis record could not be written."""
    with contextlib.suppress(Exception):
        sync(config, dataset)
    # Export already ended, explicitly identified sessions without blocking
    # annotation recovery when one source/report is unavailable.
    with contextlib.suppress(Exception):
        from levi.evaluation_results import sync_live_catalog

        sync_live_catalog(config)
