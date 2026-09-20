"""Versioned Agent REST endpoints; browser and external requests share policy."""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from .capabilities import invoke, public_run
from .runtime import Workbench
from .schema import ProviderConfig, ToolCall
from .security import Principal, external_principal
from .store import Conflict

router = APIRouter(prefix="/api/levi/agent/v1", tags=["Agent Workbench (experimental)"])


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
        raise HTTPException(404, "Run or changeset not found") from exc


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
    return {
        "external": {
            "configured": bool(os.getenv("LEVI_AGENT_TOKEN")),
            "enabled": enabled,
            "datasets": list(
                filter(None, os.getenv("LEVI_AGENT_DATASETS", "").split(","))
            ),
        }
    }


class ExternalAccess(Contract):
    enabled: bool


@router.post("/connections/external")
def external_access(payload: ExternalAccess, request: Request):
    principal(request).require("configure")
    workbench().store.put("settings", "external-access", payload.model_dump())
    return {"ok": True}
