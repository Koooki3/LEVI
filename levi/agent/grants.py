"""Revocable, per-connection grants. Stored credentials are one-way digests."""

import hashlib
import secrets
import time

from .runtime import new_id
from .security import Principal


def create(store, spec):
    token = secrets.token_urlsafe(32)
    value = {
        **spec.model_dump(),
        "id": new_id(),
        "enabled": True,
        "expires": time.time() + spec.hours * 3600,
        "calls": 0,
        "digest": hashlib.sha256(token.encode()).hexdigest(),
    }
    if spec.run_id:
        run = store.get("runs", spec.run_id)
        if run["context"]["repo_id"] not in spec.datasets:
            raise ValueError("Run is outside connection scope")
    store.put("grants", value["id"], value)
    return {**public(value), "token": token}


def public(value):
    return {k: v for k, v in value.items() if k != "digest"}


def authenticate(store, token):
    key = hashlib.sha256(token.encode()).hexdigest()
    for item in store.list("grants"):
        if secrets.compare_digest(key, item["digest"]):
            if not item["enabled"] or item["expires"] <= time.time():
                raise PermissionError("Connection expired or disconnected")
            return Principal(
                item["id"],
                datasets=tuple(item["datasets"]),
                operations=("read", "draft", "execute"),
            )
    return None


def check_call(store, principal, run_id):
    try:
        grant = store.get("grants", principal.id)
    except KeyError:
        return

    def reserve(value):
        if not value["enabled"] or value["expires"] <= time.time():
            raise PermissionError("Connection expired or disconnected")
        if value["run_id"] and value["run_id"] != run_id:
            raise PermissionError("Connection is limited to its approved run")
        if value["calls"] >= value["max_tool_calls"]:
            raise PermissionError("Connection tool budget exhausted")
        value["calls"] += 1

    store.mutate("grants", grant["id"], reserve)


def require_run(store, principal, run_id):
    if principal.human:
        return
    try:
        value = store.get("grants", principal.id)
    except KeyError:
        return
    if not value["enabled"] or value["expires"] <= time.time():
        raise PermissionError("Connection expired or disconnected")
    if value["run_id"] and value["run_id"] != run_id:
        raise PermissionError("Connection is limited to its approved run")
