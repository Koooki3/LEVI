"""Read-only HTTP views for the live page (``/api/levi/live/*``).

Every route only reads files the supervisor and the robot side write; none
changes anything, and none returns a token or a path outside the live
workspace's own state. In any workspace that is not a live workspace (no
``live/workspace.json``) the routes answer ``{"enabled": false}``.

The JSON shapes are documented in ``docs/LIVE.md``.
"""

import os
import re
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException

from . import config as live_config
from . import jsonio, mirror, sessions

router = APIRouter(prefix="/api/levi/live", tags=["Live annotation"])

NAME = re.compile(r"^[A-Za-z0-9._-]{1,140}$")
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
        row["dataset"] = mirror.dataset_name(group, task)
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
    if not NAME.match(name):
        raise HTTPException(404, "Unknown live dataset")
    state = mirror.load_state(config, name)
    if not state:
        raise HTTPException(404, "Unknown live dataset")
    demos = state.get("demos") or {}
    rows = [_demo_row(d, demos[d]) for d in sorted(demos, reverse=True)[:MAX_DEMOS]]
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
        "total_demos": len(demos),
        "demos": rows,
        "incomplete": state.get("incomplete"),
        "discarded": state.get("discarded"),
        "current": state.get("current"),
        # The review runs still open for a person (their ids; the status row
        # carries the count as ``review_runs_open``).
        "review_runs": list(state.get("review_runs") or [])[:50],
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
