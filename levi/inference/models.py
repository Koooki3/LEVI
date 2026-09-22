"""Workspace-persisted Ollama inventory and explicit download jobs.

Uses the existing SQLite store and event journal. External Ollama instances own
model storage; LEVI never guesses their paths or stops their processes.
"""

import threading
import time

from levi.agent.runtime import new_id
from levi.agent.schema import ProviderConfig
from levi.agent.store import Conflict, digest

from .ollama import OllamaError
from .provider import client_for

_ACTIVE: dict[tuple[str, str], threading.Thread] = {}
_LOCK = threading.Lock()


def configuration(store, name):
    try:
        config = ProviderConfig.model_validate(store.get("providers", name))
    except KeyError:
        raise ValueError("Unknown model connection") from None
    if config.kind != "ollama" or not config.enabled:
        raise ValueError("An enabled Ollama connection is required")
    return config


def inspect(store, name):
    config = configuration(store, name)
    client = client_for(config, timeout=15)
    models = client.models()
    installed = next((m for m in models if m.name == config.model), None)
    details = client.show(config.model) if installed else {}
    # Only declared capabilities are returned. This does not execute a probe.
    capabilities = details.get("capabilities", [])
    return {
        "version": client.version(),
        "model": installed.model_dump() if installed else None,
        "models": [model.model_dump() for model in models],
        "capabilities": capabilities if isinstance(capabilities, list) else [],
        "capability_evidence": "service_declared",
        "quality_verified": False,
        "digest_matches": bool(installed and installed.digest == config.model_digest),
        "storage": {"ownership": "external", "path": None},
    }


def bind(store, name, expected_digest, *, vision, structured_output):
    config = configuration(store, name)
    client = client_for(config, timeout=15)
    model = client.require_model(config.model, expected_digest)
    details = client.show(config.model)
    if vision and "vision" not in details.get("capabilities", []):
        raise ValueError("The installed model does not declare vision capability")
    if not structured_output:
        raise ValueError(
            "Explicit structured-output capability confirmation is required"
        )
    service_version = client.version()
    candidate = ProviderConfig.model_validate(
        {
            **config.model_dump(),
            "model_digest": model.digest,
            "vision": vision,
            "structured_output": True,
        }
    )
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if configuration(store, name) != config:
            raise Conflict(
                "Model configuration changed while inspecting; refresh before binding"
            )
        store.save(db, "providers", name, candidate.model_dump())
        store.save(
            db,
            "model_manifests",
            name,
            {
                "model": config.model,
                "digest": model.digest,
                "service_version": service_version,
                "template_digest": digest(details.get("template", "")),
                "details": details.get("details", {}),
                "capabilities": details.get("capabilities", []),
                "evidence": "service_declared_and_user_confirmed",
                "quality_verified": False,
                "recorded_at": time.time(),
            },
        )
    return candidate.model_dump()


def _lease(config):
    return "ollama-download:" + config.base_url.rstrip("/")


def start_download(store, name, request_id):
    config = configuration(store, name)
    request = digest(config.model_dump(exclude={"model_digest"}))
    # The request key is scoped to this provider, never used as a filename.
    key = f"{name}:{request_id}"
    with _LOCK:
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = store.get("model_download_requests", key)
            except KeyError:
                existing = None
            if existing:
                if existing["request"] != request:
                    raise Conflict(
                        "Download request ID reused for a different model configuration"
                    )
                return download(store, existing["job_id"])
            job_id = "model-" + new_id(
                {key.removeprefix("model-") for key in store.ids("model_downloads")}
            )
            lease = _lease(config)
            current = db.execute(
                "SELECT expires FROM leases WHERE id=?", (lease,)
            ).fetchone()
            if current and current[0] > time.time():
                raise Conflict(
                    "Another model download is active on this Ollama service"
                )
            db.execute(
                "INSERT OR REPLACE INTO leases VALUES(?,?,?)",
                (lease, job_id, time.time() + 90),
            )
            item = {
                "id": job_id,
                "provider": name,
                "model": config.model,
                "config": config.model_dump(),
                "status": "queued",
                "cancel_requested": False,
                "created_at": time.time(),
                "updated_at": time.time(),
                "progress": None,
            }
            store.save(db, "model_downloads", job_id, item)
            store.save(
                db,
                "model_download_requests",
                key,
                {"request": request, "job_id": job_id},
            )
            store.event(
                job_id, "model.download.queued", provider=name, model=config.model
            )
        thread = threading.Thread(target=_download, args=(store, job_id), daemon=True)
        _ACTIVE[(str(store.state), job_id)] = thread
        thread.start()
    return download(store, job_id)


def download(store, job_id):
    try:
        item = store.get("model_downloads", job_id)
    except KeyError:
        raise ValueError("Unknown model download") from None
    public = {key: value for key, value in item.items() if key != "config"}
    if item["status"] in {"running", "queued"}:
        with store.connect() as db:
            lease = db.execute(
                "SELECT expires FROM leases WHERE id=? AND owner=?",
                (_lease(ProviderConfig.model_validate(item["config"])), job_id),
            ).fetchone()
        if not lease or lease[0] <= time.time():
            public["status"] = "interrupted"
    return public


def cancel(store, job_id):
    def update(item):
        if item["status"] in {"queued", "running"}:
            item["cancel_requested"] = True

    store.mutate("model_downloads", job_id, update)
    return download(store, job_id)


def _download(store, job_id):
    item = store.get("model_downloads", job_id)
    config = ProviderConfig.model_validate(item["config"])
    lease = _lease(config)

    def cancelled():
        if not store.claim(lease, job_id, ttl=90):
            return True
        current = store.get("model_downloads", job_id)
        if current["cancel_requested"]:
            return True
        try:
            return configuration(store, config.name) != config
        except ValueError:
            return True

    try:
        store.mutate(
            "model_downloads", job_id, lambda job: job.update(status="running")
        )
        client = client_for(config, timeout=3600)
        for progress in client.pull(
            config.model, authorize=lambda: None, cancelled=cancelled
        ):
            store.mutate(
                "model_downloads",
                job_id,
                lambda job, progress=progress: job.update(
                    progress=progress, updated_at=time.time()
                ),
            )
        _finish(store, job_id, lease, config.name, "succeeded")
    except (OllamaError, ValueError, OSError):
        was_cancelled = cancelled()
        _finish(
            store,
            job_id,
            lease,
            config.name,
            "cancelled" if was_cancelled else "failed",
            "DOWNLOAD_CANCELLED" if was_cancelled else "DOWNLOAD_FAILED",
        )
    finally:
        store.release(lease, job_id)
        with _LOCK:
            _ACTIVE.pop((str(store.state), job_id), None)


def _finish(store, job_id, lease, provider, status, error_code=None):
    # A terminal job and its released slot must become visible together: a
    # user retrying immediately after cancellation must not hit the old lease.
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        item = store.get("model_downloads", job_id)
        item.update(status=status, updated_at=time.time())
        if error_code:
            item["error_code"] = error_code
        store.save(db, "model_downloads", job_id, item)
        store.release(lease, job_id)
        store.event(job_id, "model.download." + status, provider=provider)
