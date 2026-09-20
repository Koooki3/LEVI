"""Human control, streamed audit and Pilot lifecycle over existing Workbench."""

import asyncio
import json
import os
import signal

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .api import principal, workbench
from .grants import create, public
from .pilot_contracts import GrantRequest, PilotMessage, PilotStart

router = APIRouter(prefix="/api/levi/agent/v1")


def human(request):
    who = principal(request)
    who.require("configure")
    return who


@router.post("/core/stop")
async def stop_core(request: Request):
    human(request)
    if not os.getenv("LEVI_CORE_INSTANCE"):
        raise ValueError("This API is not owned by Core Host")
    asyncio.get_running_loop().call_later(0.3, os.kill, os.getpid(), signal.SIGTERM)
    return {"status": "stopping"}


@router.get("/grants")
def grants(request: Request):
    human(request)
    return [public(g) for g in workbench().store.list("grants")]


@router.post("/grants")
def grant(spec: GrantRequest, request: Request):
    human(request)
    from levi.catalog import DEMOS, datasets

    known = {
        entry.get("id", "local/" + name) for name, entry in datasets().items()
    } | set(DEMOS)
    if any(d not in known for d in spec.datasets):
        raise ValueError("Unknown dataset")
    return create(workbench().store, spec)


@router.post("/grants/{id}/revoke")
def revoke(id: str, request: Request):
    human(request)
    return public(
        workbench().store.mutate("grants", id, lambda g: g.update(enabled=False))
    )


@router.get("/runtimes")
def runtimes(request: Request):
    human(request)
    from .pilot import profiles

    return profiles()


@router.get("/pilot/sessions")
def sessions(request: Request):
    human(request)
    return workbench().store.list("pilot_sessions")


@router.post("/pilot/sessions")
def start(spec: PilotStart, request: Request):
    human(request)
    from .pilot import start

    return start(workbench(), spec)


@router.post("/pilot/sessions/{id}/message")
def message(id: str, spec: PilotMessage, request: Request):
    human(request)
    from .pilot import message

    return message(workbench(), id, spec.text)


@router.post("/pilot/sessions/{id}/{action}")
def control(id: str, action: str, request: Request):
    human(request)
    from .pilot import control

    return control(workbench(), id, action)


@router.get("/runs/{id}/manifest")
def manifest(id: str, request: Request):
    wb = workbench()
    who = principal(request)
    who.require("read", wb.store.get("runs", id)["context"]["repo_id"])
    from .grants import require_run

    require_run(wb.store, who, id)
    from .tracking import manifest

    return manifest(wb, id)


@router.get("/runs/{id}/stream")
async def stream(id: str, request: Request, after: int = 0):
    wb = workbench()
    who = principal(request)
    who.require("read", wb.store.get("runs", id)["context"]["repo_id"])
    from .grants import require_run

    require_run(wb.store, who, id)
    cursor = max(0, after, int(request.headers.get("last-event-id", "0")))

    async def events():
        nonlocal cursor
        while not await request.is_disconnected():
            # Re-evaluate revocation while the connection remains open.
            principal(request).require(
                "read", wb.store.get("runs", id)["context"]["repo_id"]
            )
            rows = wb.store.events(id, cursor)
            for event in rows:
                cursor = event["seq"]
                yield f"id: {cursor}\ndata: {json.dumps(event)}\n\n"
            if not rows:
                yield ": heartbeat\n\n"
                await asyncio.sleep(0.5)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.get("/pilot/permissions")
def permissions(request: Request):
    human(request)
    return [
        p
        for p in workbench().store.list("pilot_permissions")
        if p["status"] == "pending"
    ]


from .pilot_contracts import PermissionDecision


@router.post("/pilot/permissions/{id}")
def decision(id: str, spec: PermissionDecision, request: Request):
    human(request)

    def update(value):
        if value["status"] != "pending" or spec.option_id not in {
            o["optionId"] for o in value["options"]
        }:
            raise ValueError("Permission is stale or option is invalid")
        value.update(status="answered", option_id=spec.option_id)

    return workbench().store.mutate("pilot_permissions", id, update)
