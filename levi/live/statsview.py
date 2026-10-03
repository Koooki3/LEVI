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

from . import exclusion, gating, mirror, stats

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


def session_ends(config) -> dict:
    """``{(dataset, session): latest completed_at}`` over every demo the
    datasets' state files know, labelled or not yet: the end of a session as
    far as the service has seen it."""
    ends: dict = {}
    for name, state in mirror.list_states(config).items():
        for row in (state.get("demos") or {}).values():
            if not isinstance(row, dict):
                continue
            run, stamp = row.get("run_id"), stats.num(row.get("completed_at"))
            if run and stamp is not None:
                key = (name, str(run))
                ends[key] = max(ends.get(key, stamp), stamp)
    return ends


def excluded_demos(config) -> set:
    """``{(dataset, demo)}`` a person has removed (``exclusion.py``): the state
    files are the truth, a record only says what was so when it was written."""
    found = set()
    for name, state in mirror.list_states(config).items():
        for demo, row in (state.get("demos") or {}).items():
            if isinstance(row, dict) and exclusion.is_excluded(row):
                found.add((name, demo))
    return found


def gate_rows(config) -> list:
    return gating.history(config.live_dir, limit=10**6, tail_bytes=GATE_TAIL_BYTES)


def scope_of(dataset=None, session=None, since=None) -> dict:
    return {"dataset": dataset or None, "session": session or None, "since": since}


def scoped_rows(config, dataset=None, session=None, since=None, include_excluded=False):
    """``(records, hidden)``: the records of a scope, without the episodes a
    person removed unless ``include_excluded``, and how many removed demos the
    scope holds."""
    everything = stats.mark_excluded(records(config.live_dir), excluded_demos(config))
    scoped = stats.select(everything, dataset, session, since)
    hidden = {
        (r.get("dataset"), r.get("demo")) for r in scoped if r.get("excluded") is True
    }
    rows = scoped if include_excluded else stats.select(scoped, include_excluded=False)
    return rows, len(hidden)


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
    how many demos that hid)."""
    now = time.time() if now is None else now
    rows, hidden = scoped_rows(config, dataset, session, since, include_excluded)
    ends = session_ends(config)
    gate = gate_rows(config)
    episodes = stats.episode_rows(rows, ends)
    episodes.sort(key=lambda r: -(stats.num(r.get("at")) or 0))
    total = len(episodes)
    if limit is not None:
        episodes = episodes[offset : offset + limit]
    sessions = stats.session_rows(rows, gate=gate, session_ends=ends)
    return {
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


def last_modified(config) -> float | None:
    """When the statistics last changed (epoch seconds), for a cheap check."""
    stamps = [item[3] / 1e9 for item in _stamp(config.live_dir)]
    return max(stamps) if stamps else None
