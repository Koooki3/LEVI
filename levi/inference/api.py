"""Human-controlled local-model management (Ollama, and inspect/bind for a
local OpenAI-compatible server); never exposed as self-approval MCP tools."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field

from levi.agent.api import principal, workbench
from levi.agent.schema import Contract

from . import models
from .ollama import OllamaError

router = APIRouter(prefix="/api/levi/agent/v1", tags=["Local models"])


def human(request):
    who = principal(request)
    who.require("configure")
    if not who.human:
        raise PermissionError("Model management requires a human control session")
    return workbench().store


class BindModel(Contract):
    digest: str = Field(pattern=r"^(sha256:)?[a-f0-9]{64}$")
    vision: bool = False
    structured_output: Literal[True]


class DownloadModel(Contract):
    request_id: str = Field(min_length=1, max_length=120, pattern=r"^[a-zA-Z0-9_-]+$")
    approve_download: Literal[True]


@router.get("/providers/{name}/ollama")
def inspect_model(name: str, request: Request):
    try:
        return models.inspect(human(request), name)
    except OllamaError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.post("/providers/{name}/ollama/bind")
def bind_model(name: str, payload: BindModel, request: Request):
    try:
        return models.bind(
            human(request),
            name,
            payload.digest,
            vision=payload.vision,
            structured_output=payload.structured_output,
        )
    except OllamaError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.post("/providers/{name}/ollama/download", status_code=202)
def download_model(name: str, payload: DownloadModel, request: Request):
    store = human(request)
    if models.configuration(store, name).kind != "ollama":
        raise HTTPException(
            409, "A local model server loads its own weights; LEVI does not download"
        )
    return models.start_download(store, name, payload.request_id)


@router.get("/model-downloads")
def downloads(request: Request):
    store = human(request)
    return [models.download(store, job_id) for job_id in store.ids("model_downloads")]


@router.get("/model-downloads/{job_id}")
def download_status(job_id: str, request: Request):
    return models.download(human(request), job_id)


@router.post("/model-downloads/{job_id}/cancel")
def cancel_download(job_id: str, request: Request):
    return models.cancel(human(request), job_id)


def runtime_manager():
    from levi import service

    from .runtime import OllamaRuntimeManager

    return OllamaRuntimeManager(service.ROOT, service.STATE)


class StartRuntime(Contract):
    port: int = Field(default=11435, ge=1024, le=65535)
    approve_start: Literal[True]


@router.get("/ollama/runtime")
def runtime_status(request: Request):
    human(request)
    return runtime_manager().status()


@router.post("/ollama/runtime/start")
def runtime_start(payload: StartRuntime, request: Request):
    human(request)
    from .gpu import require_free

    # Starting `ollama serve` initialises the GPU too: same off-peak rule.
    require_free()
    return runtime_manager().start(payload.port)


@router.post("/ollama/runtime/stop")
def runtime_stop(request: Request):
    store = human(request)
    manager = runtime_manager()
    url = manager.status()["url"]
    if any(
        run.get("provider_config", {}).get("base_url", "").rstrip("/") == url
        and run["status"] in {"running", "queued"}
        for run in store.list("runs")
    ):
        raise HTTPException(409, "Pause active model tasks before stopping the service")
    if any(
        job["config"]["base_url"].rstrip("/") == url
        and models.download(store, job["id"])["status"] in {"running", "queued"}
        for job in store.list("model_downloads")
    ):
        raise HTTPException(409, "Cancel active downloads before stopping the service")
    return manager.stop()


class ModelMemory(Contract):
    operation: Literal["load", "unload"]
    approve_hardware_use: Literal[True]


@router.post("/providers/{name}/ollama/memory")
def model_memory(name: str, payload: ModelMemory, request: Request):
    store = human(request)
    config = models.configuration(store, name)
    if config.kind != "ollama":
        raise HTTPException(
            409,
            "A local model server keeps its model loaded while it runs; start or "
            "stop the server itself",
        )
    if not config.model_digest:
        raise HTTPException(409, "Bind an installed model before managing its memory")
    if any(
        run.get("provider_config", {}).get("base_url", "").rstrip("/")
        == config.base_url.rstrip("/")
        and run["status"] in {"running", "queued"}
        for run in store.list("runs")
    ):
        raise HTTPException(409, "Pause active tasks before changing model residency")
    try:
        client = models.client_for(config)
        client.require_model(config.model, config.model_digest)
        response = client._request(
            "POST",
            "/api/generate",
            {
                "model": config.model,
                "prompt": "",
                "stream": False,
                "keep_alive": "5m" if payload.operation == "load" else 0,
            },
        )
        if response.get("done") is not True:
            raise OllamaError("Model residency request did not complete")
        return {
            "operation": payload.operation,
            "status": "completed",
            "ownership": "service",
        }
    except OllamaError as exc:
        raise HTTPException(502, str(exc)) from exc
