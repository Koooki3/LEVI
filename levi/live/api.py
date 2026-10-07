"""HTTP views for the live page (``/api/levi/live/*``).

The GET routes only read files the supervisor and the robot side write, and
none returns a token or a path outside the live workspace's own state.

Which live workspace: the one this LEVI runs on when it is a live workspace
(the live service's own core), else the one ``locate.py`` finds
(``LEVI_LIVE_WORKSPACE``, or the live home's ``started.json``). That is how the
product LEVI shows the live page without sharing a workspace with the service:
it reads the live workspace's files, and nothing of the live service (the
approver, its runs, the mirror) enters the product workspace. With no live
workspace found the routes answer ``{"enabled": false, "reason": ...}``.

One thing a GET does write, and only in the product LEVI's own workspace:
the first time a request finds a live workspace, its path is added to
``<product workspace>/pool/live_workspaces.json`` (the training pool keeps
reading the removals made there; ``levi/pool/exclusions.py``, at most
``MAX_REMEMBERED``; ``levi pool live-workspaces list|forget``). Nothing is
ever written into the live workspace by a GET.

The only routes that change anything take an episode out of a dataset and put
it back (``exclusion.py``; a soft delete that keeps every file). They are a
person's action: the UI token of the LEVI serving the page is required (the
same check as every other write of the page), a LEVI Agent credential is
refused, and there is no capability an agent or the automatic approver could
call instead. On the product LEVI the change is made by its own process on
the live workspace's state file (under the lock the live worker takes) and
audited with ``via: "product"``.

The JSON shapes are documented in ``docs/LIVE.md``.
"""

import contextvars
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import (
    auto,
    exclusion,
    jsonio,
    locate,
    mirror,
    resumer,
    sessions,
    stats,
    statsfmt,
    statsview,
)
from . import config as live_config

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


def _own() -> Path:
    """This LEVI's own workspace."""
    from levi.paths import ROOT

    return ROOT


# Why there is nothing to show, for this request (``_disabled``).
_REASON: contextvars.ContextVar[str] = contextvars.ContextVar(
    "live_disabled_reason", default="not_configured"
)


def _workspace() -> Path | None:
    """The live workspace this page shows: this LEVI's own when it is the live
    one, else the one ``locate.find`` names (``LEVI_LIVE_WORKSPACE``, or the
    live home's ``started.json``), else None. One that another LEVI shows is
    remembered by that LEVI's training pool (``pool/exclusions.py``), so a
    removal made there keeps counting after the page finds another one."""
    found = locate.find(_own())
    _REASON.set(found.problem or "not_live")
    if found.embedded:
        from levi.pool import exclusions as pool_exclusions

        pool_exclusions.remember(found.workspace, _own() / "pool")
    return found.workspace


def _linked_id(workspace: Path, name: str) -> str | None:
    if not _embedded(workspace):
        return None
    from levi import links

    return links.product_id(workspace, name)


def _embedded(root: Path) -> bool:
    """Shown by a LEVI that is not the live workspace itself (the product
    LEVI's page)."""
    try:
        return Path(root).resolve() != _own().resolve()
    except OSError:
        return True


def _config():
    """The live configuration in force (the file the supervisor wrote), or
    None when no live workspace is found."""
    root = _workspace()
    if root is None:
        return None
    if not locate.is_live(root):
        _REASON.set("not_live")
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
    home = os.environ.get(locate.ENV_HOME)
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
    """No live workspace to show, and why (a reason code, never a path):
    ``not_configured``, ``not_live`` or ``product_workspace`` (``locate.py``)."""
    return {"enabled": False, "reason": _REASON.get()}


def _where(config, value: dict | None, alive: bool) -> dict:
    """Which page shows this: ``embedded`` when it is not the live
    workspace's own LEVI (the product LEVI), and the live workspace's own
    page when the service runs one (``levi live start --ui``)."""
    front = (value or {}).get("frontend") or {}
    page = (
        f"http://{config.service.host}:{config.service.ui_port}"
        if alive and front.get("ui") and front.get("state") == "ok"
        else None
    )
    return {
        "embedded": _embedded(config.workspace),
        "live_ui": page,
        # Its folder's name, never the whole path.
        "workspace_name": config.workspace.name,
        # The product's training pool remembers no more live workspaces
        # (``pool/exclusions.MAX_REMEMBERED``): removals made in this one keep
        # counting only while the page shows it.
        "pool_memory_full": _embedded(config.workspace)
        and _pool_memory_full(config.workspace),
    }


def _pool_memory_full(workspace) -> bool:
    from levi.pool import exclusions as pool_exclusions

    return pool_exclusions.is_full(workspace, _own() / "pool")


# Fields of the status file that name folders of this machine: not given out
# by another LEVI's page (the product LEVI).
PATH_FIELDS = ("workspace", "config")


def _shown(config, value):
    if value and _embedded(config.workspace):
        return {k: v for k, v in value.items() if k not in PATH_FIELDS}
    return value


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
        **_where(config, value, alive),
        "alive": alive,
        "age_s": None
        if not value
        else round(max(0.0, now - float(value.get("updated_at") or 0)), 1),
        "service": _shown(config, value),
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
                "rule",
                "place_outcome",
                "closes_after_last_valid",
                "min_valid",
                "spec",
                "spec_version",
                "review",
                "evaluated",
                "at",
            )
        }
        # Where an automatic verdict came from when it is not the background
        # review: ``online`` (the online judgement the client relayed).
        | ({"source": verdict["source"]} if verdict.get("source") else {})
        if verdict
        else None,
        # The operator label (ground truth, from the rollout's metadata) and
        # whether the agent's automatic, unreviewed verdict agrees with it.
        "operator_label": _operator(row),
        "agreement": stats.agree_of(row.get("operator_label"), row.get("verdict")),
    }


def _operator(row):
    label = row.get("operator_label")
    if not isinstance(label, dict):
        return None
    out = {k: label.get(k) for k in ("outcome", "by", "source")}
    if label.get("ended_by"):
        out["ended_by"] = label["ended_by"]
    return out


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
    # The live workspace's own catalog (not this LEVI's: the product LEVI may
    # hold a dataset of the same name that is another one).
    catalog = jsonio.read(config.workspace / "outputs/LEVI/workbench/datasets.json", {})
    entry = catalog.get(name) if isinstance(catalog, dict) else None
    repo_id = entry.get("id") if isinstance(entry, dict) else None
    value = jsonio.read(config.status_file)
    where = _where(config, value, _alive(value, time.time()))
    return {
        "enabled": True,
        "name": name,
        "repo_id": repo_id,
        # Where this LEVI opens the dataset when it is not the live workspace
        # itself: its read-only link (``levi/links.py``), if it has one.
        "linked_repo_id": _linked_id(config.workspace, name),
        **where,
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
        # Over every kept demo (not only the listed ones), from the state.
        "agreement": stats.agreement(
            [(demos[d].get("operator_label"), demos[d].get("verdict")) for d in kept]
        ),
        "pipeline": {"temporal": config.pipeline.temporal},
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
        raise HTTPException(404, "No live workspace is set up for this LEVI")
    if not DATASET.fullmatch(name) or not mirror.load_state(config, name):
        raise HTTPException(404, "Unknown live dataset")
    # Before writing: the state file and the audit log must really lie inside
    # the live workspace (a ``live/`` linked to elsewhere is refused).
    state = mirror.state_path(config, name)
    for path in (
        config.live_dir,
        state,
        state.with_name(state.name + ".lock"),  # jsonio.locked's lock file
        config.live_dir / auto.AUDIT,
    ):
        if not locate.confined(path, config.workspace):
            raise HTTPException(
                409, "The live workspace's state lies outside it; nothing was changed"
            )
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


def _via(config):
    """``"product"`` when the person acts on the product LEVI's page: the
    change is made right here on the live workspace's state files, under the
    same lock the live worker takes (``exclusion.py``), so the live core
    need not run and no key of it is read."""
    return "product" if _embedded(config.workspace) else None


def _exclude(name, demos, reason):
    config = _live_config(name)
    _demo_names(demos)
    try:
        done = exclusion.exclude(config, name, demos, reason, via=_via(config))
    except exclusion.Refused as exc:
        raise _refusal(exc) from exc
    return {"enabled": True, "dataset": name, **done}


def _restore(name, demos):
    config = _live_config(name)
    _demo_names(demos)
    try:
        done = exclusion.restore(config, name, demos, via=_via(config))
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


def _attachment(stem: str, extension: str) -> str:
    """A ``Content-Disposition`` that is always valid: the plain ``filename``
    holds only safe ASCII (a quote, a newline or a non-Latin-1 letter in a
    dataset name must not break a header), and when the real name differs it
    follows as RFC 5987 ``filename*``."""
    plain = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("_") or "live-stats"
    value = f'attachment; filename="{plain}.{extension}"'
    if plain != stem:
        value += f"; filename*=UTF-8''{quote(f'{stem}.{extension}', safe='')}"
    return value


def _scope(dataset, session, since):
    if dataset is not None and not DATASET.fullmatch(dataset):
        raise HTTPException(400, "Not a live dataset name")
    if session is not None and not SESSION_ID.fullmatch(session):
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
            "Content-Disposition": _attachment(stem, format),
            "Cache-Control": "no-store",
        },
    )
