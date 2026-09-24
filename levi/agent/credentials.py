"""Process-local, endpoint-bound secrets. Never serialized into artifacts/logs."""

import os
import threading

_KEYS: dict[str, tuple[str, str]] = {}
_LOCK = threading.RLock()


def binding(config):
    from .store import digest

    return digest(config.model_dump(exclude={"enabled"}))


def get(config):
    if config.kind == "ollama":
        return None
    with _LOCK:
        entry = _KEYS.get(config.name)
        if entry and entry[0] == binding(config):
            return entry[1]
        return os.getenv(config.key_env)


def set_session(name, key, config=None):
    with _LOCK:
        if key:
            if config is None or config.name != name:
                raise ValueError(
                    "Session credentials must be bound to a provider configuration"
                )
            _KEYS[name] = (binding(config), key)
        else:
            _KEYS.pop(name, None)


def rebind(name, old, new):
    """Keep a session key bound to ``old`` for ``new``: a change that does
    not move it anywhere else (binding records the served model's digest)."""
    with _LOCK:
        entry = _KEYS.get(name)
        if entry and entry[0] == binding(old):
            _KEYS[name] = (binding(new), entry[1])


def status(config):
    if config.kind == "ollama":
        return "not_required"
    with _LOCK:
        entry = _KEYS.get(config.name)
        return (
            "session"
            if entry and entry[0] == binding(config)
            else "environment"
            if os.getenv(config.key_env)
            # A local server's key is optional: sent when set, never required.
            else "not_required"
            if config.kind == "openai-local"
            else "missing"
        )
