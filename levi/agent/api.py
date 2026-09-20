"""Versioned Agent REST endpoints; browser and external requests share policy."""

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from .capabilities import invoke, public_run
from .runtime import Workbench
from .schema import ProviderConfig, ToolCall
from .security import Principal, external_principal
from .store import Conflict

router = APIRouter(prefix="/api/levi/agent/v1", tags=["Agent Workbench"])

# When this service process started. A connection is reported as live only
# once its client has called since then: LEVI cannot detect an MCP client
# that never speaks to it, and claiming a connection it has not heard from
# would be a guess.
import time as _time

STARTED_AT = _time.time()


def workbench():
    from levi import service

    return Workbench(service.STATE)


def principal(request):
    authorization = request.headers.get("authorization", "")
    if authorization.startswith("Bearer "):
        return external_principal(authorization[7:])
    # Authentication middleware controls access; the built-in UI is local-human.
    return Principal("local-human", human=True)


@router.get("/capabilities")
def capabilities(request: Request):
    return invoke(workbench(), principal(request), "capabilities.list", {})


@router.post("/tools")
def call(payload: ToolCall, request: Request):
    try:
        return invoke(
            workbench(),
            principal(request),
            payload.name,
            payload.arguments,
            payload.idempotency_key,
        )
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except KeyError as exc:
        # Name the key: this used to report every internal KeyError as a
        # missing run, which hid real faults behind a plausible 404.
        raise HTTPException(404, f"Not found: {exc}") from exc
    except HTTPException:
        raise
    except Exception as exc:
        # A tool that raises anything else must still answer with a readable
        # error: an empty body leaves an MCP client guessing.
        logging.getLogger(__name__).exception("capability %s failed", payload.name)
        raise HTTPException(
            500, f"{payload.name} failed: {type(exc).__name__}: {exc}"
        ) from exc


@router.get("/providers")
def providers(request: Request):
    principal(request).require("configure")
    from .credentials import status

    return [
        {
            **p,
            "credential_ready": status(ProviderConfig.model_validate(p)) != "missing",
            "credential_source": status(ProviderConfig.model_validate(p)),
        }
        for p in workbench().store.list("providers")
    ]


@router.post("/providers")
def configure_provider(payload: ProviderConfig, request: Request):
    principal(request).require("configure")
    if payload.name in {"external", "local-tools"}:
        raise HTTPException(
            422, "This provider name is reserved for a built-in runtime"
        )
    from .security import endpoint_addresses

    endpoint_addresses(payload.base_url, payload.allow_localhost)
    wb = workbench()
    try:
        previous = ProviderConfig.model_validate(
            wb.store.get("providers", payload.name)
        )
        if previous.model_dump(exclude={"enabled"}) != payload.model_dump(
            exclude={"enabled"}
        ):
            from .credentials import set_session

            set_session(payload.name, None)
    except KeyError:
        pass
    wb.store.put("providers", payload.name, payload.model_dump())
    return {"ok": True, "name": payload.name}


@router.get("/runs")
def runs(request: Request):
    who = principal(request)
    wb = workbench()
    wb.store.recover()
    from .grants import require_run

    result = []
    for run in wb.store.list("runs"):
        try:
            who.require("read", run["context"]["repo_id"])
            require_run(wb.store, who, run["id"])
        except PermissionError:
            continue
        result.append(public_run(run))
    return result


@router.get("/runs/{run_id}/artifacts/{name}")
def artifact(run_id: str, name: str, request: Request):
    wb = workbench()
    try:
        run = wb.store.get("runs", run_id)
    except KeyError as exc:
        raise HTTPException(404) from exc
    who = principal(request)
    who.require("read", run["context"]["repo_id"])
    from .grants import require_run

    require_run(wb.store, who, run_id)
    if not who.human and not run["context"]["allow_media_egress"]:
        raise HTTPException(403, "External media access not authorized")
    allowed = set()
    for ep in sorted(set(run["completed"] + run.get("prepared", []))):
        allowed.update(
            e["artifact"] for e in wb.store.get("evidence", f"{run_id}:{ep}")["items"]
        )
    allowed.update(
        e["artifact"] for e in wb.store.list("object_evidence") if e["run_id"] == run_id
    )
    allowed.update(run.get("sheets", []))  # generated evidence contact sheets
    if name not in allowed or Path(name).name != name:
        raise HTTPException(404)
    return FileResponse(
        wb.store.run_dir(run_id) / "evidence" / name,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


from pydantic import SecretStr

from .schema import Contract


class SessionCredential(Contract):
    key: SecretStr


@router.post("/providers/{name}/session")
def session_credential(name: str, payload: SessionCredential, request: Request):
    principal(request).require("configure")
    config = ProviderConfig.model_validate(workbench().store.get("providers", name))
    from .credentials import set_session

    value = payload.key.get_secret_value()
    if not value or len(value) > 16384:
        raise HTTPException(400, "Credential must be non-empty and bounded")
    set_session(name, value, config)
    return {"ok": True, "source": "session"}


@router.post("/providers/{name}/disconnect")
def disconnect_provider(name: str, request: Request):
    principal(request).require("configure")
    from .credentials import set_session

    set_session(name, None)
    workbench().store.mutate("providers", name, lambda p: p.update(enabled=False))
    return {"ok": True}


@router.post("/providers/{name}/activate")
def activate_provider(name: str, request: Request):
    principal(request).require("configure")
    workbench().store.mutate("providers", name, lambda p: p.update(enabled=True))
    return {"ok": True}


@router.delete("/providers/{name}")
def delete_provider(name: str, request: Request):
    principal(request).require("configure")
    wb = workbench()
    if any(
        r["context"]["provider"] == name and r["status"] in {"running", "queued"}
        for r in wb.store.list("runs")
    ):
        raise HTTPException(
            409, "Pause or cancel active tasks before removing their provider"
        )
    from .credentials import set_session

    set_session(name, None)
    with wb.store.connect() as db:
        db.execute("DELETE FROM records WHERE kind='providers' AND id=?", (name,))
    return {"ok": True}


@router.get("/formats")
def formats(request: Request):
    principal(request).require("read")
    from .formats import describe

    return describe()


@router.get("/connections")
def connection_state(request: Request):
    import os

    principal(request).require("configure")
    try:
        enabled = workbench().store.get("settings", "external-access")["enabled"]
    except KeyError:
        enabled = True
    import time

    # A scoped connection created with `levi agent connect` lives in the
    # workspace, not in this process's environment. Reporting only the
    # environment showed a live MCP client as disconnected, with no scope.
    store = workbench().store
    now = time.time()
    grants = [
        {
            "id": grant["id"],
            "label": grant.get("label") or grant["id"],
            "datasets": list(grant.get("datasets", [])),
            "expires": grant["expires"],
            "expires_in_seconds": (
                max(0, int(grant["expires"] - now)) if grant["expires"] else None
            ),
            "last_seen": grant.get("last_seen"),
            "live": bool(grant.get("last_seen", 0) >= STARTED_AT),
            "idle_seconds": (
                int(now - grant["last_seen"]) if grant.get("last_seen") else None
            ),
            "calls": grant.get("calls", 0),
            "max_calls": grant.get("max_calls"),
            "run_id": grant.get("run_id"),
        }
        for grant in store.list("grants")
        if grant.get("enabled") and (not grant["expires"] or grant["expires"] > now)
    ]
    datasets = sorted(
        {name for grant in grants for name in grant["datasets"]}
        | set(filter(None, os.getenv("LEVI_AGENT_DATASETS", "").split(",")))
    )
    return {
        "external": {
            "configured": bool(os.getenv("LEVI_AGENT_TOKEN")) or bool(grants),
            "live": any(row["live"] for row in grants),
            "service_started_at": STARTED_AT,
            "enabled": enabled,
            "datasets": datasets,
            "grants": sorted(grants, key=lambda row: row["expires"] or float("inf")),
            "shared_token": bool(os.getenv("LEVI_AGENT_TOKEN")),
        }
    }


class ExternalAccess(Contract):
    enabled: bool


@router.post("/connections/external")
def external_access(payload: ExternalAccess, request: Request):
    principal(request).require("configure")
    workbench().store.put("settings", "external-access", payload.model_dump())
    return {"ok": True}


@router.get("/activity")
def activity_log(request: Request, after: int = 0, limit: int = 120):
    """Recent agent actions, newest last, for the first paint of the panel."""
    from . import activity

    principal(request).require("configure")
    rows = activity.recent(workbench().store, after, limit)
    return {"events": rows, "cursor": rows[-1]["seq"] if rows else after}


@router.get("/activity/stream")
async def activity_stream(request: Request, after: int = 0):
    """Agent actions as they happen, for the human watching the workbench."""
    import asyncio
    import json as _json

    from fastapi.responses import StreamingResponse

    from . import activity

    principal(request).require("configure")
    store = workbench().store
    cursor = max(0, after, int(request.headers.get("last-event-id", "0")))

    async def events():
        nonlocal cursor
        idle = 0
        while not await request.is_disconnected():
            # Re-check on every pass: a revoked operator must stop receiving.
            principal(request).require("configure")
            rows = store.events(activity.STREAM, cursor)
            for row in rows:
                cursor = row["seq"]
                yield f"id: {cursor}\ndata: {_json.dumps(row)}\n\n"
            if rows:
                idle = 0
            else:
                idle += 1
                # A comment keeps proxies from closing an idle stream.
                if idle % 10 == 0:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.4)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.get("/activity/tasks")
def activity_tasks(request: Request, limit: int = 12):
    """Runs as tasks: what each is waiting for, how far it got, what it made.

    The activity stream shows individual calls; this is the same work seen as
    the thing a person actually tracks. State comes from the run record, not
    from the stream, so a task is correct even when the panel was closed while
    it ran.
    """
    from . import activity
    from .tracking import waiting_for

    principal(request).require("configure")
    store = workbench().store
    recent = {}
    for row in activity.recent(store, 0, activity.KEEP):
        if row.get("run"):
            recent[row["run"]] = row

    tasks = []
    # Newest first, by the time the run was created: the store's own order is
    # an implementation detail and reversing it is not "most recent".
    ordered = sorted(
        store.list("runs"), key=lambda record: record.get("created_at", 0), reverse=True
    )
    for run in ordered:
        state = waiting_for(store, run)
        context = run["context"]
        episodes = context["episodes"] or []
        done = [ep for ep in run.get("completed", []) if ep in episodes]
        revision = None
        if state["committed"]:
            change = store.get("changes", run["changes"])
            revision = change.get("revision_id") or change.get("published_revision")
        artifacts = []
        if state["committed"]:
            published = store.head(run["dataset_key"])
            if published:
                artifacts.append(
                    {
                        "label": "committed revision",
                        "path": "outputs/LEVI/workbench/agent/datasets/"
                        f"{run['dataset_key']}/revisions/{published}",
                        "revision": published,
                    }
                )
        # What this task cost, from the sample recorded for it: LEVI meters
        # its own model runs and external agents report their own.
        spent = next(
            (
                row
                for row in store.list("usage_samples")
                if row["run_id"] == run["id"] and row.get("tokens")
            ),
            None,
        )
        last = recent.get(run["id"])
        tasks.append(
            {
                "run_id": run["id"],
                "dataset": context["repo_id"],
                "workflow": context["workflow"]["kind"],
                "instruction": context["instruction"],
                "episodes": episodes,
                "completed": done,
                "progress": round(len(done) / len(episodes), 3) if episodes else 0.0,
                "status": run["status"],
                "created_at": run["created_at"],
                "provider": run["provider_config"]["name"],
                "artifacts": artifacts,
                "revision": revision,
                "tokens": (spent or {}).get("tokens"),
                # An external agent spends tokens LEVI cannot see. When a
                # finished run has no sample, say so: the cost model only
                # improves if someone notices the gap and asks for a report.
                "usage_missing": bool(
                    state["finished"]
                    and not spent
                    and run["provider_config"]["kind"] == "external"
                ),
                "token_source": (spent or {}).get("source"),
                "evidence_frames": (spent or {}).get("evidence_frames"),
                "requests": run.get("requests") or (spent or {}).get("requests"),
                "last_action": (
                    {
                        "action": last["action"],
                        "status": last["status"],
                        "at": last["at"],
                        "tool": last["tool"],
                    }
                    if last
                    else None
                ),
                **state,
            }
        )
        if len(tasks) >= limit:
            break
    return {"tasks": tasks}
