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
        "id": new_id(set(store.ids("grants"))),
        "enabled": True,
        # No expiry and no call cap unless one was asked for.
        "expires": time.time() + spec.hours * 3600 if spec.hours else None,
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
            if not item["enabled"]:
                raise PermissionError("Connection disconnected")
            if item["expires"] and item["expires"] <= time.time():
                raise PermissionError("Connection expired")
            # Every authenticated request marks the connection as heard from,
            # including the discovery calls that are exempt from the call
            # count. Liveness is "did this client speak to this LEVI", not
            # "did it spend budget".
            store.mutate(
                "grants", item["id"], lambda value: value.update(last_seen=time.time())
            )
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
        if not value["enabled"]:
            raise PermissionError("Connection disconnected")
        if value["expires"] and value["expires"] <= time.time():
            raise PermissionError("Connection expired")
        if value["run_id"] and value["run_id"] != run_id:
            raise PermissionError("Connection is limited to its approved run")
        cap = value.get("max_tool_calls")
        if cap and value["calls"] >= cap:
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
    if not value["enabled"] or (value["expires"] and value["expires"] <= time.time()):
        raise PermissionError("Connection expired or disconnected")
    if value["run_id"] and value["run_id"] != run_id:
        raise PermissionError("Connection is limited to its approved run")
