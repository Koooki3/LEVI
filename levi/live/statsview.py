"""The statistics of a live workspace as one JSON-able answer.

``build`` reads ``live/stats.jsonl`` and ``live/gate.jsonl`` and the datasets'
state files, filters to a scope (everything, one dataset, one evaluation
session, records since a time) and returns the aggregates, the per-session
table and the per-episode table. The API, the CLI and the reports all call it,
so a figure on the page and in a report are the same figure.

Read-only. The answer holds no path, token or key: records carry none, and
the free-text ``reason`` of a failed demo is not copied into it.
"""

import time
from pathlib import Path

from . import exclusion, gating, management, mirror, stats

SCHEMA = "levi.live.stats.v1"
MAX_PAGE = 500
MAX_SESSIONS = 200
GATE_TAIL_BYTES = 4 * 1024 * 1024

_CACHE: dict = {}


def _stamp(live_dir) -> tuple:
    out = []
    base = Path(live_dir) / stats.FILE
    for path in [base, *sorted(base.parent.glob(stats.FILE + ".*"))]:
        try:
            st = path.stat()
        except OSError:
            continue
        out.append((path.name, st.st_ino, st.st_size, st.st_mtime_ns))
    return tuple(out)


def records(live_dir) -> list:
    """Every readable record, oldest first. Cached while the files do not
    change, so a page that polls costs a stat per file, not a re-parse."""
    key = str(live_dir)
    stamp = _stamp(live_dir)
    cached = _CACHE.get(key)
    if cached and cached[0] == stamp:
        return cached[1]
    rows = stats.read(live_dir)
    _CACHE[key] = (stamp, rows)
    return rows


def session_ends(config, include_excluded=False) -> dict:
    """``{(dataset, session): latest completed_at}`` over every demo the
    datasets' state files know, labelled or not yet: the end of a session as
    far as the service has seen it. Episodes a person removed do not move it
    unless ``include_excluded``."""
    ends: dict = {}
    for name, state in mirror.list_states(config).items():
        for row in (state.get("demos") or {}).values():
            if not isinstance(row, dict):
                continue
            if exclusion.is_excluded(row) and not include_excluded:
                continue
            run, stamp = row.get("run_id"), stats.num(row.get("completed_at"))
            if run and stamp is not None:
                key = (name, str(run))
                ends[key] = max(ends.get(key, stamp), stamp)
    return ends


def excluded_demos(config) -> tuple:
    """``({(dataset, demo)} a person has removed, {datasets whose state could
    be read})`` (``exclusion.py``). The state files are the truth: a record
    only says what was so when it was written, and an episode may have been
    restored since."""
    found: set = set()
    known: set = set()
    for name, state in mirror.list_states(config).items():
        known.add(name)
        for demo, row in (state.get("demos") or {}).items():
            if isinstance(row, dict) and exclusion.is_excluded(row):
                found.add((name, demo))
    return found, known


def marked_records(config) -> list:
    """Every record, with ``excluded`` decided by the state files."""
    removed, known = excluded_demos(config)
    states = mirror.list_states(config, include_archived=True)
    tombstones = management.removed_sessions(config)
    rows = []
    for row in stats.mark_excluded(records(config.live_dir), removed, known):
        state = states.get(row.get("dataset")) or {}
        if state.get("archived"):
            continue
        if any(
            state.get("group") == tombstone.get("group")
            and state.get("task_folder") == tombstone.get("task_folder")
            and mirror.same_root(state.get("root"), tombstone.get("root"))
            and row.get("session")
            in {tombstone.get("session_id"), tombstone.get("run_id")}
            and (stats.num(row.get("at")) or 0) <= (stats.num(tombstone.get("at")) or 0)
            for tombstone in tombstones
            if isinstance(tombstone, dict)
        ):
            continue
        rows.append(row)
    return rows


def signature(rows) -> dict:
    """What a report is built from, cheaply: the records of a scope that
    count (removed episodes left out), when the newest was written, and how
    many removed demos the scope holds. Equal signature, equal report."""
    kept = [r for r in rows if r.get("excluded") is not True]
    stamps = [t for t in (stats.num(r.get("at")) for r in kept) if t is not None]
    hidden = {
        (r.get("dataset"), r.get("demo")) for r in rows if r.get("excluded") is True
    }
    return {
        "records": len(kept),
        "last_at": max(stamps) if stamps else None,
        "excluded_demos": len(hidden),
    }


def signatures(config) -> dict:
    """``{(dataset, session): signature}`` for every session with records, in
    one pass over the records."""
    groups: dict = {}
    for row in marked_records(config):
        groups.setdefault((row.get("dataset"), row.get("session")), []).append(row)
    return {key: signature(rows) for key, rows in groups.items()}


def gate_rows(config) -> list:
    return gating.history(config.live_dir, limit=10**6, tail_bytes=GATE_TAIL_BYTES)


def scope_of(dataset=None, session=None, since=None) -> dict:
    return {"dataset": dataset or None, "session": session or None, "since": since}


def scoped_rows(config, dataset=None, session=None, since=None, include_excluded=False):
    """``(records, hidden)``: the records of a scope, without the episodes a
    person removed unless ``include_excluded``, and how many removed demos the
    scope holds."""
    scoped = stats.select(marked_records(config), dataset, session, since)
    hidden = {
        (r.get("dataset"), r.get("demo")) for r in scoped if r.get("excluded") is True
    }
    rows = scoped if include_excluded else stats.select(scoped, include_excluded=False)
    return rows, len(hidden)


def _stamps(paths) -> tuple:
    out = []
    for path in paths:
        try:
            st = Path(path).stat()
        except OSError:
            out.append((str(path), None))
        else:
            out.append((str(path), st.st_ino, st.st_size, st.st_mtime_ns))
    return tuple(out)


def _inputs(config) -> tuple:
    """A stamp of every file ``build`` reads: when none changed, neither did
    its answer."""
    live = config.live_dir
    gate = [live / gating.HISTORY, live / (gating.HISTORY + ".1")]
    try:
        states = sorted(mirror.datasets_dir(config).glob("*.json"))
    except OSError:
        states = []
    return (
        _stamp(live),
        _stamps(gate),
        _stamps(states),
        _stamps([live / management.FILE]),
    )


_BUILT: dict = {}
BUILT_MAX = 32


def build(
    config,
    *,
    dataset=None,
    session=None,
    since=None,
    limit=100,
    offset=0,
    include_excluded=False,
    now=None,
) -> dict:
    """The statistics of a scope. ``limit`` and ``offset`` page the episode
    table (newest first); ``limit=None`` returns every episode. Episodes a
    person removed are left out unless ``include_excluded`` (the answer says
    how many demos that hid). The answer is kept while no input file changes,
    so a page that polls does not recompute it."""
    now = time.time() if now is None else now
    key = (
        str(config.live_dir),
        _inputs(config),
        dataset,
        session,
        since,
        limit,
        offset,
        bool(include_excluded),
    )
    cached = _BUILT.get(key)
    if cached is not None:
        return {**cached, "generated_at": round(now, 3)}
    rows, hidden = scoped_rows(config, dataset, session, since, include_excluded)
    ends = session_ends(config, include_excluded)
    gate = gate_rows(config)
    episodes = stats.episode_rows(rows, ends)
    episodes.sort(key=lambda r: -(stats.num(r.get("at")) or 0))
    total = len(episodes)
    if limit is not None:
        episodes = episodes[offset : offset + limit]
    sessions = stats.session_rows(rows, session_ends=ends)
    payload = {
        "enabled": True,
        "schema": SCHEMA,
        "generated_at": round(now, 3),
        "scope": scope_of(dataset, session, since),
        "include_excluded": bool(include_excluded),
        "excluded_demos": hidden,
        "datasets": sorted(
            {r.get("dataset") for r in records(config.live_dir) if r.get("dataset")}
        ),
        "summary": stats.summarize(rows, gate=gate, session_ends=ends),
        "sessions": sessions[:MAX_SESSIONS],
        "sessions_total": len(sessions),
        "episodes": {
            "total": total,
            "offset": offset,
            "limit": limit,
            "rows": episodes,
        },
    }
    if len(_BUILT) >= BUILT_MAX:
        _BUILT.clear()
    _BUILT[key] = payload
    return payload


def last_modified(config) -> float | None:
    """When the statistics last changed (epoch seconds), for a cheap check."""
    stamps = [item[3] / 1e9 for item in _stamp(config.live_dir)]
    return max(stamps) if stamps else None
