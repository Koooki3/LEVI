"""HTTP views for the live page (``/api/levi/live/*``).

The GET routes only read files the supervisor and the robot side write, and
none returns a token or a path outside the live workspace's own state. In any
workspace that is not a live workspace (no ``live/workspace.json``) they
answer ``{"enabled": false}``.

The only routes that change anything take an episode out of a dataset and put
it back (``exclusion.py``; a soft delete that keeps every file). They are a
person's action: the UI token is required (the same check as every other
write of the page), a LEVI Agent credential is refused, and there is no
capability an agent or the automatic approver could call instead.

The JSON shapes are documented in ``docs/LIVE.md``.
"""

import os
import re
import secrets
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import config as live_config
from . import exclusion, jsonio, mirror, resumer, sessions, statsfmt, statsview

router = APIRouter(prefix="/api/levi/live", tags=["Live annotation"])

NAME = re.compile(r"^[A-Za-z0-9._-]{1,140}$")
# A dataset's name is whatever ``mirror.dataset_name`` made of it (a clash of
# two roots adds ``__at__<root mark>``, see ``mirror.dataset_name``): no assumption about the
# characters beyond "one file name under live/datasets" -- no separator, no
# leading dot, no control character. Whether it exists is the state file's
# say.
DATASET = re.compile(r"^(?![.])[^/\\\x00-\x1f\x7f]{1,200}$")
SESSION_ID = re.compile(r"^[A-Za-z0-9._:+-]{1,160}$")
EXPORTS = {
    "csv": "text/csv; charset=utf-8",
    "json": "application/json",
    "md": "text/markdown; charset=utf-8",
}
MAX_EXPORT_ROWS = 20000
ALIVE_S = 15.0
MAX_DEMOS = 200
MAX_AUDIT = 100


def _workspace() -> Path:
    from levi.paths import ROOT

    return ROOT


def _config():
    """The live configuration in force (the file the supervisor wrote), or
    None outside a live workspace."""
    root = _workspace()
    if not (root / "live" / "workspace.json").is_file():
        return None
    live = root / "live"
    written = next(
        (p for p in (live / "effective.toml", root / "live.toml") if p.is_file()), None
    )
    try:
        config = live_config.load(written)
    except ValueError:
        config = live_config.Config()
    config.service.workspace = str(root)
    home = os.environ.get("LEVI_LIVE_HOME")
    if home:
        config.service.home = home
    return config


def _alive(status: dict | None, now: float) -> bool:
    if not status:
        return False
    pid = status.get("pid")
    return bool(
        isinstance(pid, int)
        and os.path.exists(f"/proc/{pid}")
        and now - float(status.get("updated_at") or 0) <= ALIVE_S
        and status.get("state") != "stopped"
    )


def _disabled():
    return {"enabled": False}


@router.get("/status")
def status():
    """The service's status file, plus whether it is alive and which datasets
    an FR3 fault interrupted."""
    config = _config()
    if config is None:
        return _disabled()
    now = time.time()
    value = jsonio.read(config.status_file)
    alive = _alive(value, now)
    faults = []
    for name, row in ((value or {}).get("datasets") or {}).items():
        if row.get("fault"):
            faults.append({"dataset": name, "reasons": row.get("fault_reasons") or []})
    return {
        "enabled": True,
        "alive": alive,
        "age_s": None
        if not value
        else round(max(0.0, now - float(value.get("updated_at") or 0)), 1),
        "service": value,
        "faults": faults,
        "fr3_red": bool(((value or {}).get("fr3") or {}).get("state") == "red"),
        # Runs the gate stopped that a person started: ``waiting`` go on by
        # themselves once the gate has stayed open, ``needs_person`` do not
        # (``levi/live/resumer.py``).
        "blocked_runs": resumer.read_blocked(config.live_dir, now),
    }


@router.get("/sessions")
def sessions_view():
    """Evaluation sessions (fresh from the robot side's files) and the FR3
    health monitor."""
    config = _config()
    if config is None:
        return _disabled()
    now = time.time()
    found = sessions.read_sessions(config.watch.roots, now)
    # When each began waiting for the reset: only the supervisor sees that.
    since = {
        (r.get("root"), r.get("group"), r.get("task_folder")): r.get(
            "waiting_reset_since"
        )
        for r in ((jsonio.read(config.status_file) or {}).get("sessions") or [])
    }
    rows = []
    for (_root, group, task), session in sorted(found.items())[:64]:
        row = session.public()
        row["dataset"] = mirror.resolve_name(config, _root, group, task)
        row["fault"] = session.fault
        row["waiting_reset_since"] = (
            (session.waiting_reset_since or since.get((_root, group, task)))
            if session.state == "waiting_reset"
            else None
        )
        rows.append(row)
    fr3 = sessions.read_fr3(config.fr3.health_file, config.fr3.stale_s, now)
    return {
        "enabled": True,
        "sessions": rows,
        "fr3": fr3,
        "active": sessions.any_active(found),
    }


@router.get("/datasets")
def datasets_view():
    config = _config()
    if config is None:
        return _disabled()
    value = jsonio.read(config.status_file) or {}
    return {"enabled": True, "datasets": value.get("datasets") or {}}


def _demo_row(name, row):
    temporal = row.get("temporal") or {}
    verdict = row.get("verdict") or {}
    return {
        "demo": name,
        "excluded": row.get("excluded") or None,
        "state": row.get("state"),
        "episode_index": row.get("episode_index"),
        "run_id": row.get("run_id"),
        "completed_at": row.get("completed_at"),
        "attempts": row.get("attempts", 0),
        "reason": row.get("reason"),
        "segments": temporal.get("segments"),
        "committed_at": temporal.get("committed_at"),
        "verdict": {
            k: verdict.get(k)
            for k in (
                "outcome",
                "events",
                "valid_events",
                "undecided",
                "spec",
                "review",
                "evaluated",
                "at",
            )
        }
        if verdict
        else None,
    }


@router.get("/datasets/{name}")
def dataset_view(name: str):
    """One dataset: its demos (newest first, bounded), the batch in progress,
    the last batch and its automatic verdicts."""
    config = _config()
    if config is None:
        return _disabled()
    if not DATASET.fullmatch(name):
        raise HTTPException(404, "Unknown live dataset")
    state = mirror.load_state(config, name)
    if not state:
        raise HTTPException(404, "Unknown live dataset")
    demos = state.get("demos") or {}
    names = sorted(demos, reverse=True)
    # Removed episodes (``exclusion.py``) are listed apart, so the list a
    # person works with and every number on the page leave them out.
    kept = [d for d in names if not exclusion.is_excluded(demos[d])]
    removed = [d for d in names if exclusion.is_excluded(demos[d])]
    rows = [_demo_row(d, demos[d]) for d in kept[:MAX_DEMOS]]
    # Not cut at MAX_DEMOS: a person removes few, and cutting the list would
    # hide episodes that could not then be restored from the page.
    removed_rows = [_demo_row(d, demos[d]) for d in removed]
    repo_id = None
    try:
        from levi import catalog

        entry = catalog.datasets().get(name)
        repo_id = entry["id"] if entry else None
    except Exception:  # noqa: BLE001 - the catalog is optional here
        repo_id = None
    return {
        "enabled": True,
        "name": name,
        "repo_id": repo_id,
        "group": state.get("group"),
        "task_folder": state.get("task_folder"),
        "task_text": state.get("task_text"),
        "counts": mirror.counts(state),
        "total_demos": len(kept),
        "demos": rows,
        "excluded_count": len(removed),
        "excluded_demos": removed_rows,
        "incomplete": state.get("incomplete"),
        "discarded": state.get("discarded"),
        "current": state.get("current"),
        # The review runs still open for a person (their ids; the status row
        # carries the count as ``review_runs_open``).
        "review_runs": exclusion.open_reviews(state)[:50],
        "last_batch": state.get("last_batch"),
        "last_processed_at": state.get("last_processed_at"),
        "last_error": state.get("last_error"),
        "review": "auto",
        "evaluated": False,
    }


@router.get("/audit")
def audit_view(limit: int = 50):
    """The automatic approver's audit log, newest first."""
    config = _config()
    if config is None:
        return _disabled()
    limit = max(1, min(int(limit), MAX_AUDIT))
    path = config.live_dir / "audit.jsonl"
    rows = []
    try:
        lines = path.read_text().splitlines()[-limit:]
    except OSError:
        lines = []
    for line in reversed(lines):
        try:
            rows.append(__import__("json").loads(line))
        except ValueError:
            continue
    return {"enabled": True, "audit": rows}


# --- removing an episode (a person's action) --------------------------------------


class Removal(BaseModel):
    demos: list[str] = Field(min_length=1, max_length=500)
    reason: str = Field("", max_length=exclusion.REASON_MAX)


class Restoration(BaseModel):
    demos: list[str] = Field(min_length=1, max_length=500)


class OneRemoval(BaseModel):
    reason: str = Field("", max_length=exclusion.REASON_MAX)


def _person(request: Request) -> None:
    """Only a person at the page may do this. The service's middleware has
    already turned an agent's Bearer credential away from every route but the
    Agent API and demanded the UI token; this refuses again here, so the rule
    does not depend on how the router is mounted."""
    # Refusing *any* Bearer is deliberate: the only Bearer credentials LEVI
    # knows are agents' (the page's own requests carry the UI token in a
    # header of their own), and an agent must never act as the person.
    if request.headers.get("authorization", "").lower().startswith("bearer "):
        raise HTTPException(
            403, "Removing an episode is a person's action; agents cannot do it"
        )
    secret = os.getenv("LEVI_UI_TOKEN")
    if secret and not secrets.compare_digest(
        request.headers.get("x-levi-ui-token", ""), secret
    ):
        raise HTTPException(401, "Use the LEVI Web UI")


def _live_config(name: str):
    config = _config()
    if config is None:
        raise HTTPException(404, "This is not a live workspace")
    if not DATASET.fullmatch(name) or not mirror.load_state(config, name):
        raise HTTPException(404, "Unknown live dataset")
    return config


def _demo_names(demos):
    bad = [d for d in demos if not NAME.match(d)]
    if bad:
        raise HTTPException(404, f"Unknown episode: {', '.join(bad[:5])}")


def _refusal(exc: exclusion.Refused) -> HTTPException:
    names = ", ".join(exc.demos[:10]) + (" ..." if len(exc.demos) > 10 else "")
    if isinstance(exc, exclusion.Unknown):
        return HTTPException(404, f"Unknown episode: {names}")
    if isinstance(exc, exclusion.Busy):
        return HTTPException(
            409,
            f"{names}: being labelled (part of the batch in progress); "
            "try again after the batch",
        )
    return HTTPException(
        409, f"{names}: was never taken into the dataset, nothing to remove"
    )


def _exclude(name, demos, reason):
    config = _live_config(name)
    _demo_names(demos)
    try:
        done = exclusion.exclude(config, name, demos, reason)
    except exclusion.Refused as exc:
        raise _refusal(exc) from exc
    return {"enabled": True, "dataset": name, **done}


def _restore(name, demos):
    config = _live_config(name)
    _demo_names(demos)
    try:
        done = exclusion.restore(config, name, demos)
    except exclusion.Refused as exc:
        raise _refusal(exc) from exc
    return {"enabled": True, "dataset": name, **done}


@router.post("/datasets/{name}/exclude")
def exclude_demos(name: str, body: Removal, request: Request):
    """Remove episodes from the dataset (restorable). All or nothing: 404 for
    an episode the dataset does not have, 409 for one in the batch in
    progress. An episode already removed is reported in ``unchanged``."""
    _person(request)
    return _exclude(name, body.demos, body.reason)


@router.post("/datasets/{name}/restore")
def restore_demos(name: str, body: Restoration, request: Request):
    """Put removed episodes back. An episode that is not removed is reported
    in ``unchanged``."""
    _person(request)
    return _restore(name, body.demos)


@router.post("/datasets/{name}/demos/{demo}/exclude")
def exclude_demo(
    name: str, demo: str, request: Request, body: OneRemoval | None = None
):
    _person(request)
    return _exclude(name, [demo], body.reason if body else "")


@router.post("/datasets/{name}/demos/{demo}/restore")
def restore_demo(name: str, demo: str, request: Request):
    _person(request)
    return _restore(name, [demo])


def _scope(dataset, session, since):
    if dataset is not None and not DATASET.fullmatch(dataset):
        raise HTTPException(400, "Not a live dataset name")
    if session is not None and not SESSION_ID.match(session):
        raise HTTPException(400, "Not a session id")
    if since is not None and not (0 <= since < 4e9):
        raise HTTPException(400, "since is epoch seconds")
    return dataset or None, session or None, since


@router.get("/stats")
def stats_view(
    dataset: str | None = None,
    session: str | None = None,
    since: float | None = None,
    limit: int = 100,
    offset: int = 0,
    include_excluded: bool = False,
):
    """The quantitative statistics of the labelling (docs/LIVE.md, "Statistics
    and reports"): aggregates, one row per evaluation session and a page of
    per-episode rows (newest first), for everything or one dataset, session
    or time range. Read from ``live/stats.jsonl``; no path, token or key."""
    config = _config()
    if config is None:
        return _disabled()
    dataset, session, since = _scope(dataset, session, since)
    return statsview.build(
        config,
        dataset=dataset,
        session=session,
        since=since,
        limit=max(1, min(int(limit), statsview.MAX_PAGE)),
        offset=max(0, int(offset)),
        include_excluded=include_excluded,
    )


@router.get("/stats/export")
def stats_export(
    format: str = "json",
    dataset: str | None = None,
    session: str | None = None,
    since: float | None = None,
    lang: str = "en",
    include_excluded: bool = False,
):
    """The same statistics as a download: ``csv`` (one row per episode),
    ``json`` (everything) or ``md`` (a readable report)."""
    config = _config()
    if config is None:
        return _disabled()
    if format not in EXPORTS:
        raise HTTPException(400, "format is csv, json or md")
    dataset, session, since = _scope(dataset, session, since)
    payload = statsview.build(
        config,
        dataset=dataset,
        session=session,
        since=since,
        limit=MAX_EXPORT_ROWS,
        include_excluded=include_excluded,
    )
    if format == "csv":
        body = statsfmt.to_csv(payload)
    elif format == "md":
        body = statsfmt.to_markdown(payload, "zh" if lang == "zh" else "en")
    else:
        body = statsfmt.to_json(payload)
    stem = "-".join(["live-stats", *[x for x in (dataset, session) if x]])
    return Response(
        body,
        media_type=EXPORTS[format],
        headers={
            "Content-Disposition": f'attachment; filename="{stem}.{format}"',
            "Cache-Control": "no-store",
        },
    )
